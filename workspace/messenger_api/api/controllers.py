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

import datetime
import hashlib
import urllib.parse
import uuid as sys_uuid

import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils
from webob import multidict

from workspace.common import urns
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import versions
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models
from workspace.provider_api import commands as provider_commands
from workspace.provider_api import payloads as provider_payloads
from workspace.provider_api.dm import models as provider_models


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
            self._conditional_filters.append({field_name: operator(field_value)})
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
            f"attachment; filename=\"{quoted_name}\"; filename*=UTF-8''{encoded_name}"
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
    ACCESS_FIELDS = (
        "access_status",
        "access_checked_at",
        "access_confirmed_at",
        "access_next_check_at",
        "access_last_error",
    )

    class Packer(ra_packers.JSONPacker):
        def pack_resource(self, obj):
            result = super().pack_resource(obj)
            settings = result.get("account_settings")
            if isinstance(settings, dict) and "credentials" in settings:
                settings["credentials"] = None
            return result

    __packer__ = Packer
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.ExternalAccount,
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _reject_user_info_from_api(account_settings):
        if (
            account_settings.KIND == models.ExternalAccountType.ZULIP.value
            and account_settings.user_info is not None
        ):
            raise ra_exc.ValidationErrorException()

    @staticmethod
    def _reject_iam_account_from_api(account_settings):
        if account_settings.KIND == models.ExternalAccountType.IAM.value:
            raise ra_exc.ValidationErrorException()

    @staticmethod
    def _validate_provider(provider_uuid, account_settings):
        if provider_uuid is None:
            raise ra_exc.ValidationErrorException()
        provider = provider_models.WorkspaceProvider.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(provider_uuid),
                "enabled": dm_filters.EQ(True),
            },
        )
        if account_settings.KIND not in provider.supported_kinds:
            raise ra_exc.ValidationErrorException()

    @classmethod
    def _reject_access_fields_from_api(cls, values):
        for name in cls.ACCESS_FIELDS:
            values.pop(name, None)

    @staticmethod
    def _normalize_server_url(server_url):
        return server_url.rstrip("/")

    def _normalize_server_url_value(self, values):
        if "server_url" in values:
            values["server_url"] = self._normalize_server_url(
                values["server_url"],
            )

    @staticmethod
    def _set_source_scope(values):
        values.pop("source_scope", None)
        if "server_url" in values:
            values["source_scope"] = values["server_url"]

    @staticmethod
    def _set_pending_access(values):
        values.update(
            {
                "access_status": models.ExternalAccountAccessStatus.PENDING.value,
                "access_checked_at": None,
                "access_confirmed_at": None,
                "access_last_error": None,
            }
        )

    @staticmethod
    def _set_missing_credentials_access(values):
        now = datetime.datetime.now(datetime.timezone.utc)
        values.update(
            {
                "access_status": (
                    models.ExternalAccountAccessStatus.MISSING_CREDENTIALS.value
                ),
                "access_checked_at": now,
                "access_confirmed_at": None,
                "access_last_error": "External account credentials are missing",
            }
        )

    def _set_access_values(self, values, account=None):
        if "account_settings" not in values and "server_url" not in values:
            return
        account_settings = values.get("account_settings")
        if account_settings is None and account is not None:
            account_settings = account.account_settings
        if account_settings is None:
            return
        if account_settings.credentials is None:
            self._set_missing_credentials_access(values)
            return
        self._set_pending_access(values)

    def create(self, **kwargs):
        values = kwargs.copy()
        self._reject_access_fields_from_api(values)
        self._normalize_server_url_value(values)
        self._set_source_scope(values)
        account_settings = values["account_settings"]
        self._validate_provider(values.get("provider_uuid"), account_settings)
        self._reject_iam_account_from_api(account_settings)
        self._reject_user_info_from_api(account_settings)
        values["account_type"] = account_settings.KIND
        values["status"] = models.ExternalAccountStatus.NEW.value
        self._set_access_values(values)
        return super().create(**values)

    def update(self, uuid, **kwargs):
        account = self.get(uuid=uuid)
        values = self._apply_autovalues(kwargs)
        self._reject_access_fields_from_api(values)
        self._normalize_server_url_value(values)
        self._set_source_scope(values)
        account_settings = values.get(
            "account_settings",
            account.account_settings,
        )
        self._validate_provider(
            values.get("provider_uuid", account.provider_uuid),
            account_settings,
        )
        self._reject_iam_account_from_api(account_settings)
        self._reject_user_info_from_api(account_settings)
        self._set_access_values(values, account=account)
        account.update_dm(values=values)
        account.update()
        return account


class WorkspaceStreamController(
    WorkspaceBaseResourceControllerPaginated,
):
    class Packer(ra_packers.JSONPacker):
        def pack_resource(self, obj):
            result = super().pack_resource(obj)
            stream = models.WorkspaceStream.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(obj.uuid),
                    "project_id": dm_filters.EQ(obj.project_id),
                },
            )
            return provider_payloads.add_provider_delivery_payload(
                result,
                stream,
            )

    __packer__ = Packer
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
        project_id = self._get_project_id()
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.mark_messenger_delivery_pending(stream)
        result = messenger_dm_helpers.update_workspace_user_stream(
            project_id=project_id,
            user_uuid=self._get_user_uuid(),
            stream_uuid=uuid,
            values=kwargs,
        )
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            stream,
            "stream.update",
            urns.MESSENGER_STREAM,
            update_projection=False,
        )
        return result

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
    class Packer(ra_packers.JSONPacker):
        def pack_resource(self, obj):
            result = super().pack_resource(obj)
            message = models.WorkspaceMessage.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(obj.uuid),
                    "project_id": dm_filters.EQ(obj.project_id),
                },
            )
            return provider_payloads.add_provider_delivery_payload(
                result,
                message,
            )

    __packer__ = Packer
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.WorkspaceUserMessage,
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        project_id = values.pop("project_id", self._get_project_id())
        user_uuid = values.pop("user_uuid")
        stream = models.WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(values["stream_uuid"]),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        if stream.provider_uuid is not None:
            values.update(
                {
                    "provider_uuid": stream.provider_uuid,
                    "external_account_uuid": stream.external_account_uuid,
                    "delivery_status": (
                        provider_models.ProviderCommandStatus.PENDING.value
                    ),
                    "delivery_error": None,
                    "delivery_updated_at": datetime.datetime.now(
                        datetime.timezone.utc,
                    ),
                },
            )
        visible_message = messenger_dm_helpers.create_workspace_user_message(
            project_id=project_id,
            user_uuid=user_uuid,
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            enforce_visibility=True,
            **values,
        )
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(visible_message.uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            message,
            "message.create",
            urns.MESSENGER_MESSAGE,
            update_projection=False,
        )
        return visible_message

    def update(self, uuid, **kwargs):
        project_id = self._get_project_id()
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.mark_messenger_delivery_pending(message)
        result = messenger_dm_helpers.update_workspace_user_message(
            project_id=project_id,
            user_uuid=self._get_user_uuid(),
            message_uuid=uuid,
            values=kwargs,
        )
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            message,
            "message.update",
            urns.MESSENGER_MESSAGE,
            update_projection=False,
        )
        return result

    def delete(self, uuid):
        project_id = self._get_project_id()
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            message,
            "message.delete",
            urns.MESSENGER_MESSAGE,
        )
        return messenger_dm_helpers.delete_workspace_user_message(
            project_id=project_id,
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
    class Packer(ra_packers.JSONPacker):
        def pack_resource(self, obj):
            result = super().pack_resource(obj)
            return provider_payloads.add_provider_delivery_payload(
                result,
                obj,
            )

    __packer__ = Packer
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
        project_id = values.pop("project_id", self._get_project_id())
        message = models.WorkspaceMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(values["message_uuid"]),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        if message.provider_uuid is not None:
            values.update(
                {
                    "provider_uuid": message.provider_uuid,
                    "external_account_uuid": message.external_account_uuid,
                    "delivery_status": (
                        provider_models.ProviderCommandStatus.PENDING.value
                    ),
                    "delivery_error": None,
                    "delivery_updated_at": datetime.datetime.now(
                        datetime.timezone.utc,
                    ),
                },
            )
        reaction = messenger_dm_helpers.create_workspace_message_reaction(
            project_id=project_id,
            user_uuid=values.pop("user_uuid", self._get_user_uuid()),
            uuid=values.pop("uuid", None) or sys_uuid.uuid4(),
            **values,
        )
        provider_commands.create_messenger_command(
            reaction,
            "reaction.create",
            urns.MESSENGER_REACTION,
            update_projection=False,
        )
        return reaction

    def update(self, uuid, **kwargs):
        project_id = self._get_project_id()
        reaction = models.WorkspaceMessageReactions.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.mark_messenger_delivery_pending(reaction)
        result = messenger_dm_helpers.update_workspace_message_reaction(
            project_id=project_id,
            user_uuid=self._get_user_uuid(),
            reaction_uuid=uuid,
            values=kwargs,
        )
        reaction = models.WorkspaceMessageReactions.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            reaction,
            "reaction.update",
            urns.MESSENGER_REACTION,
            update_projection=False,
        )
        return result

    def delete(self, uuid):
        project_id = self._get_project_id()
        reaction = models.WorkspaceMessageReactions.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            reaction,
            "reaction.delete",
            urns.MESSENGER_REACTION,
        )
        return messenger_dm_helpers.delete_workspace_message_reaction(
            project_id=project_id,
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
    class Packer(ra_packers.JSONPacker):
        def pack_resource(self, obj):
            result = super().pack_resource(obj)
            topic = models.WorkspaceStreamTopic.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(obj.uuid),
                    "project_id": dm_filters.EQ(obj.project_id),
                },
            )
            return provider_payloads.add_provider_delivery_payload(
                result,
                topic,
            )

    __packer__ = Packer
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
        project_id = self._get_project_id()
        topic = models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.mark_messenger_delivery_pending(topic)
        result = messenger_dm_helpers.update_workspace_user_stream_topic(
            project_id=project_id,
            user_uuid=self._get_user_uuid(),
            topic_uuid=uuid,
            values=kwargs,
        )
        topic = models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "project_id": dm_filters.EQ(project_id),
            },
        )
        provider_commands.create_messenger_command(
            topic,
            "topic.update",
            urns.MESSENGER_TOPIC,
            update_projection=False,
        )
        return result

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
    def set_default(self, resource, *args, **kwargs):
        return messenger_dm_helpers.set_workspace_user_stream_topic_default(
            project_id=self._get_project_id(),
            user_uuid=self._get_user_uuid(),
            topic_uuid=resource.uuid,
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
