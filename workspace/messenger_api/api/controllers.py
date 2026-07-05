#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import hashlib
import urllib.parse
import uuid as sys_uuid

import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils
from webob import multidict

from workspace.common.clients import zulip as zulip_client
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models


class ApiEndpointController(ra_controllers.RoutesListController):
    """Controller for /v1/ endpoint."""

    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/"


class WorkspaceBaseResourceControllerPaginated(
    ra_controllers.BaseResourceControllerPaginated,
):
    _filter_operator_suffixes = (
        ("=>", dm_filters.GE),
        ("=<", dm_filters.LE),
        (">", dm_filters.GT),
        ("<", dm_filters.LT),
    )

    def _get_user_uuid(self):
        return self.get_context().user_uuid

    def _get_project_id(self):
        ctx = self.get_context()
        project_id = getattr(ctx, "project_id", None) if ctx is not None else None
        if project_id is None:
            raise ra_exc.ValidationErrorException()
        return project_id

    @classmethod
    def _split_filter_operator(cls, name):
        for suffix, operator in cls._filter_operator_suffixes:
            if name.endswith(suffix):
                return name[: -len(suffix)], operator
        return name, None

    def _prepare_filters(self, params):
        self._conditional_filters = []
        cleaned_params = []
        for name, value in params.items():
            field_name, operator = self._split_filter_operator(name)
            if operator is None:
                cleaned_params.append((name, value))
                continue
            field_name, field_value = self._prepare_filter(field_name, value)
            self._conditional_filters.append(
                {field_name: operator(field_value)}
            )
        return super()._prepare_filters(multidict.MultiDict(cleaned_params))

    def _apply_autofilters(self, filters):
        filters = super()._apply_autofilters(filters)
        conditional_filters = getattr(self, "_conditional_filters", [])
        if conditional_filters:
            return dm_filters.AND(filters, *conditional_filters)
        return filters

    def get_autofilters(self):
        return {
            "project_id": dm_filters.EQ(self._get_project_id()),
            "user_uuid": dm_filters.EQ(self._get_user_uuid()),
        }

    def get_autovalues(self):
        return {
            "project_id": self._get_project_id(),
            "user_uuid": self._get_user_uuid(),
        }


class FolderController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolder,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.create_workspace_user_folder(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )

    def update(self, uuid, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.update_workspace_user_folder(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            folder_uuid=uuid,
            **values,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_user_folder(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            folder_uuid=uuid,
        )


class FolderItemController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.UserFolderItem,
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.create_workspace_user_folder_item(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )

    def get(self, uuid):
        return messenger_dm_helpers.get_workspace_user_folder_item(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            item_uuid=uuid,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_user_folder_item(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            item_uuid=uuid,
        )

    @ra_actions.post
    def pin(self, resource, *args, **kwargs):
        return messenger_dm_helpers.pin_workspace_user_folder_item(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            item_uuid=resource.uuid,
        )

    @ra_actions.post
    def unpin(self, resource, *args, **kwargs):
        return messenger_dm_helpers.unpin_workspace_user_folder_item(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            item_uuid=resource.uuid,
        )


class WorkspaceFileController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceFile,
        hidden_fields=["project_id", "storage_id", "storage_object_id"],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _get_multipart_value(part):
        return getattr(part, "value", part)

    @classmethod
    def _get_optional_multipart_value(cls, parts, name, default=None):
        part = parts.get(name)
        if part is None:
            return default
        return cls._get_multipart_value(part)

    @staticmethod
    def _get_content_disposition(file):
        quoted_name = file.name.replace("\\", "\\\\").replace('"', '\\"')
        encoded_name = urllib.parse.quote(file.name)
        return (
            f'attachment; filename="{quoted_name}"; '
            f"filename*=UTF-8''{encoded_name}"
        )

    def process_result(self, result, *args, **kwargs):
        if isinstance(result, webob.Response):
            return result
        return super().process_result(result, *args, **kwargs)

    def get_autofilters(self):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        file_uuids = messenger_dm_helpers.get_workspace_user_file_uuids(
            project_id=project_id,
            user_uuid=user_uuid,
        )
        return {
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.In(file_uuids),
        }

    def _apply_autofilters(self, filters):
        filter_parts = [filters, self.get_autofilters()]
        filter_parts.extend(getattr(self, "_conditional_filters", []))
        return dm_filters.AND(*filter_parts)

    def _create_from_multipart(self, parts):
        file_part = parts["file"]
        stream_uuid_part = parts["stream_uuid"]
        stream_uuid = sys_uuid.UUID(
            self._get_multipart_value(stream_uuid_part),
        )

        file_part.file.seek(0)
        data = file_part.file.read()
        file_part.file.seek(0)

        file_uuid = sys_uuid.uuid4()
        storage_info = file_storage.save_workspace_file(
            file_uuid=file_uuid,
            data=data,
            storage_type=self._get_optional_multipart_value(
                parts,
                "storage_type",
            ),
        )
        try:
            return messenger_dm_helpers.create_workspace_file(
                project_id=self._get_project_id(),
                user_uuid=self._get_user_uuid(),
                uuid=file_uuid,
                stream_uuid=stream_uuid,
                name=self._get_optional_multipart_value(
                    parts,
                    "name",
                    file_part.filename,
                ),
                description=self._get_optional_multipart_value(
                    parts,
                    "description",
                    "",
                ),
                content_type=file_part.type,
                size_bytes=len(data),
                hash=hashlib.sha256(data).hexdigest(),
                storage_type=storage_info.storage_type,
                storage_id=storage_info.storage_id,
                storage_object_id=storage_info.storage_object_id,
            )
        except Exception:
            file_storage.delete_workspace_file(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
                storage_object_id=storage_info.storage_object_id,
            )
            raise

    def create(self, **kwargs):
        if kwargs.pop("multipart", False):
            parts = kwargs["parts"]
            return self._create_from_multipart(parts)

        values = self._apply_autovalues(kwargs)
        values.pop("storage_id", None)
        values.pop("storage_object_id", None)
        file_uuid = values.pop("uuid", None) or sys_uuid.uuid4()
        storage_info = file_storage.get_workspace_file_storage_info(
            file_uuid=file_uuid,
            storage_type=values.pop("storage_type", None),
        )
        return messenger_dm_helpers.create_workspace_file(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            uuid=file_uuid,
            storage_type=storage_info.storage_type,
            storage_id=storage_info.storage_id,
            storage_object_id=storage_info.storage_object_id,
            **values,
        )

    def update(self, uuid, **kwargs):
        values = kwargs.copy()
        values.pop("project_id", None)
        values.pop("user_uuid", None)
        values.pop("storage_type", None)
        values.pop("storage_id", None)
        values.pop("storage_object_id", None)
        return messenger_dm_helpers.update_workspace_file(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            file_uuid=uuid,
            values=values,
        )

    def delete(self, uuid):
        file = messenger_dm_helpers.get_workspace_owned_file(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            file_uuid=uuid,
        )
        result = messenger_dm_helpers.delete_workspace_file(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            file_uuid=uuid,
        )
        file_storage.delete_workspace_file(
            file_uuid=uuid,
            storage_type=file.storage_type,
            storage_object_id=file.storage_object_id,
        )
        return result

    def _download_file_response(self, resource):
        data = file_storage.read_workspace_file(
            file_uuid=resource.uuid,
            storage_type=resource.storage_type,
            storage_object_id=resource.storage_object_id,
        )
        return webob.Response(
            body=data,
            status=200,
            headers={
                "Content-Type": resource.content_type,
                "Content-Disposition": self._get_content_disposition(resource),
            },
        )

    @ra_actions.get
    def download(self, resource, *args, **kwargs):
        return self._download_file_response(resource)


WorkspaceFileController.create.openapi_schema = oa_utils.Schema(
    summary="Upload file",
    parameters=(),
    responses=oa_c.build_openapi_create_response("WorkspaceFile_Create"),
    request_body=oa_c.build_openapi_req_body_multipart(
        description="Upload workspace file",
        properties={
            "file": {"format": "binary", "type": "string"},
            "stream_uuid": {"format": "uuid", "type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "storage_type": {
                "enum": ["file", "s3"],
                "type": "string",
            },
        },
    ),
)

WorkspaceFileController.download.openapi_schema = oa_utils.Schema(
    summary="Download file",
    parameters=(),
    responses=oa_c.build_openapi_response_octet_stream("Download file"),
)


class ExternalAccountController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.ExternalAccount,
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _ensure_user_sync(account):
        user_sync = models.ExternalAccountUserSync.objects.get_one_or_none(
            filters={
                "account_type": dm_filters.EQ(account.account_type),
                "server_url": dm_filters.EQ(account.server_url),
            },
        )
        if user_sync is not None:
            if user_sync.external_account_uuid is None:
                user_sync.update_dm(
                    values={"external_account_uuid": account.uuid},
                )
                user_sync.update()
            return user_sync

        user_sync = models.ExternalAccountUserSync(
            project_id=account.project_id,
            account_type=account.account_type,
            server_url=account.server_url,
            external_account_uuid=account.uuid,
        )
        user_sync.insert()
        return user_sync

    def create(self, **kwargs):
        values = kwargs.copy()
        values["status"] = models.ExternalAccountStatus.NEW.value
        account_settings = values["account_settings"]
        credentials = account_settings.credentials
        server_url = values["server_url"]
        client = zulip_client.ZulipClient(endpoint=server_url)
        user_info = client.get_current_user_with_api_key(
            login=credentials.login,
            token=credentials.token,
        )
        account_settings.user_info = account_settings._get_zulip_user_info(
            user=user_info,
        )
        account = super().create(**values)
        self._ensure_user_sync(account)
        return account


class WorkspaceStreamController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserStream,
        hidden_fields=["private_index"],
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        values.pop("project_id", None)
        values.pop("user_uuid", None)
        return messenger_dm_helpers.get_or_create_workspace_user_stream(
            project_id=project_id,
            user_uuid=user_uuid,
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )

    def update(self, uuid, **kwargs):
        return messenger_dm_helpers.update_workspace_user_stream(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=uuid,
            values=kwargs,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_user_stream(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=uuid,
        )

    @ra_actions.post
    def archive(self, resource, *args, **kwargs):
        return messenger_dm_helpers.update_workspace_user_stream(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=resource.uuid,
            values={"is_archived": True},
        )

    @ra_actions.post
    def unarchive(self, resource, *args, **kwargs):
        return messenger_dm_helpers.update_workspace_user_stream(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=resource.uuid,
            values={"is_archived": False},
        )

    @ra_actions.post
    def notifications(self, resource, *args, **kwargs):
        return messenger_dm_helpers.update_workspace_user_stream_notifications(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=resource.uuid,
            notification_mode=kwargs["notification_mode"],
        )

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
        return messenger_dm_helpers.read_workspace_user_stream_messages(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            stream_uuid=resource.uuid,
        )


class WorkspaceStreamBindingController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceStreamBinding,
        convert_underscore=False,
        process_filters=True,
    )

    def get_autofilters(self):
        return {
            "project_id": dm_filters.EQ(self._get_project_id()),
        }

    def get_autovalues(self):
        return {
            "project_id": self._get_project_id(),
        }

    @ra_actions.post
    def add_users(self, resource, *args, **kwargs):
        return messenger_dm_helpers.get_or_create_workspace_stream_bindings(
            project_id=resource.project_id,
            stream_uuid=resource.uuid,
            who_uuid=self._get_user_uuid(),
            role_user_uuids=kwargs,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_stream_binding(
            project_id=self._get_project_id(),
            binding_uuid=uuid,
        )


class WorkspaceMessageController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserMessage,
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.create_workspace_user_message(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid"),
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )

    def update(self, uuid, **kwargs):
        return messenger_dm_helpers.update_workspace_user_message(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            message_uuid=uuid,
            values=kwargs,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_user_message(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            message_uuid=uuid,
        )

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
        return messenger_dm_helpers.read_workspace_user_message(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            message_uuid=resource.uuid,
        )

    @ra_actions.post
    def read_up_to(self, resource, *args, **kwargs):
        return messenger_dm_helpers.read_workspace_user_topic_messages_to_message(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            message_uuid=resource.uuid,
        )


class WorkspaceMessageReactionController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceMessageReactions,
        convert_underscore=False,
        process_filters=True,
    )

    def get_autofilters(self):
        project_id = self._get_project_id()
        user_uuid = self._get_user_uuid()
        message_uuids = messenger_dm_helpers.get_workspace_user_message_uuids(
            project_id=project_id,
            user_uuid=user_uuid,
        )
        return {
            "project_id": dm_filters.EQ(project_id),
            "message_uuid": dm_filters.In(message_uuids),
        }

    def _apply_autofilters(self, filters):
        filter_parts = [filters, self.get_autofilters()]
        filter_parts.extend(getattr(self, "_conditional_filters", []))
        return dm_filters.AND(*filter_parts)

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.create_workspace_message_reaction(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )

    def update(self, uuid, **kwargs):
        return messenger_dm_helpers.update_workspace_message_reaction(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            reaction_uuid=uuid,
            values=kwargs,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_message_reaction(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            reaction_uuid=uuid,
        )


class WorkspaceEventController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = messenger_events.WORKSPACE_EVENT_RESOURCE
    __default_sort__ = {"epoch_version": "asc"}


class WorkspaceEpochController(
    WorkspaceBaseResourceControllerPaginated,
):
    def filter(self, filters, order_by=None):
        return {
            "epoch_version": messenger_events.get_current_epoch_version(
                project_id=self._get_project_id(),
                user_uuid=self._get_user_uuid(),
            )
        }


class WorkspaceStreamTopicController(
    WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserTopic,
        convert_underscore=False,
        process_filters=True,
    )

    def get_autovalues(self):
        return {
            "project_id": self._get_project_id(),
        }

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        return messenger_dm_helpers.create_workspace_user_stream_topic(
            project_id=values.pop("project_id", self._get_project_id()),
            user_uuid=self._get_user_uuid(),
            values=values,
        )

    def update(self, uuid, **kwargs):
        return messenger_dm_helpers.update_workspace_user_stream_topic(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=uuid,
            values=kwargs,
        )

    def delete(self, uuid):
        return messenger_dm_helpers.delete_workspace_user_stream_topic(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=uuid,
        )

    @ra_actions.post
    def toggle_done(self, resource, *args, **kwargs):
        return messenger_dm_helpers.toggle_workspace_user_stream_topic_done(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=resource.uuid,
        )

    @ra_actions.post
    def notifications(self, resource, *args, **kwargs):
        update_notifications = (
            messenger_dm_helpers.update_workspace_user_stream_topic_notifications
        )
        return update_notifications(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=resource.uuid,
            notification_mode=kwargs["notification_mode"],
        )

    @ra_actions.post
    def read(self, resource, *args, **kwargs):
        return messenger_dm_helpers.read_workspace_user_stream_topic_messages(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=resource.uuid,
        )


class WorkspaceUserController(
    ra_controllers.BaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUser,
        convert_underscore=False,
        process_filters=True,
    )

    def _get_user_uuid(self):
        return self.get_context().user_uuid

    def _get_project_id(self):
        return self.get_context().project_id

    @ra_actions.post
    def presence(self, resource, *args, **kwargs):
        values = {"status": kwargs["status"]}
        if "emoji" in kwargs:
            values["status_emoji"] = kwargs["emoji"]
        if "text" in kwargs:
            values["status_text"] = kwargs["text"]
        return messenger_dm_helpers.update_workspace_user_presence(
            project_id=self._get_project_id(),
            user_uuid=resource.uuid,
            current_user_uuid=self._get_user_uuid(),
            values=values,
        )


class MeController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = f"/{versions.API_VERSION_1_0}/me/"
