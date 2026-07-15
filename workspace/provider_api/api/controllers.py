# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import orjson
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

from workspace.common import urns
from workspace.groupware.dm import models as groupware_models
from workspace.workspace_api import events as workspace_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import base as messenger_base
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_api.dm import models as messenger_models
from workspace.provider_api.dm import models


class ServiceWorkspaceProvider(models.WorkspaceProvider):
    pass


class ServiceExternalAccount(messenger_models.ExternalAccount):
    pass


class ServiceMailFolder(groupware_models.MailFolder):
    pass


class ServiceMailMessage(groupware_models.MailMessage):
    pass


class ServiceCalendar(groupware_models.Calendar):
    pass


class ServiceCalendarEvent(groupware_models.CalendarEvent):
    pass


class ServiceMessengerUser(messenger_models.WorkspaceUser):
    pass


class ServiceMessengerStream(messenger_models.WorkspaceStream):
    pass


class ServiceMessengerTopic(messenger_models.WorkspaceStreamTopic):
    pass


class ServiceMessengerMessage(messenger_models.WorkspaceMessage):
    pass


class ServiceMessengerReaction(messenger_models.WorkspaceMessageReactions):
    pass


class ServiceProviderCommand(models.ProviderCommand):
    pass


class ServiceProviderBlob(messenger_models.WorkspaceFile):
    pass


class ServiceCalendarProviderCommand(models.ProviderCommand):
    pass


class ServiceMessengerProviderCommand(models.ProviderCommand):
    pass


class ProviderJSONPacker(ra_packers.JSONPacker):
    json_list_fields = frozenset()

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        for name in self.json_list_fields:
            value = result.get(name)
            if isinstance(value, str):
                result[name] = orjson.loads(value)
        return result


class WorkspaceProviderJSONPacker(ProviderJSONPacker):
    json_list_fields = frozenset({"supported_kinds"})


class ProviderMailFolderJSONPacker(ProviderJSONPacker):
    def unpack(self, value):
        data = orjson.loads(value)
        data["external_user_uuid"] = data.pop("external_account_uuid")
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["external_account_uuid"] = result.pop("external_user_uuid")
        result["urn"] = urns.build(urns.MAIL_FOLDER, obj.uuid)
        result.pop("provider_uuid", None)
        result.pop("project_id", None)
        result.pop("user_uuid", None)
        return result


class ProviderMailMessageJSONPacker(ProviderMailFolderJSONPacker):
    json_list_fields = frozenset(
        {"to_addresses", "cc_addresses", "bcc_addresses"},
    )

    def unpack(self, value):
        data = orjson.loads(value)
        attachments = data.pop("attachments", None)
        _entity_type, folder_uuid = urns.parse(
            data.pop("folder_urn"),
            expected_type=urns.MAIL_FOLDER,
        )
        data["folder_uuid"] = str(folder_uuid)
        result = super().unpack(orjson.dumps(data))
        if attachments is not None:
            result["_provider_attachments"] = attachments
        return result

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["urn"] = urns.build(urns.MAIL_MESSAGE, obj.uuid)
        result["folder_urn"] = urns.build(urns.MAIL_FOLDER, obj.folder_uuid)
        result["attachments"] = _mail_attachment_payloads(obj)
        result.pop("folder_uuid", None)
        return result


class ProviderCalendarJSONPacker(ProviderJSONPacker):
    def unpack(self, value):
        data = orjson.loads(value)
        data["external_user_uuid"] = data.pop("external_account_uuid")
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["external_account_uuid"] = result.pop("external_user_uuid")
        result["urn"] = urns.build(urns.CALENDAR, obj.uuid)
        result.pop("provider_uuid", None)
        result.pop("project_id", None)
        result.pop("user_uuid", None)
        return result


class ProviderCalendarEventJSONPacker(ProviderCalendarJSONPacker):
    json_list_fields = frozenset({"attendees", "alarms"})

    def unpack(self, value):
        data = orjson.loads(value)
        _entity_type, calendar_uuid = urns.parse(
            data.pop("calendar_urn"),
            expected_type=urns.CALENDAR,
        )
        data["calendar_uuid"] = str(calendar_uuid)
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["urn"] = urns.build(urns.CALENDAR_EVENT, obj.uuid)
        result["calendar_urn"] = urns.build(urns.CALENDAR, obj.calendar_uuid)
        result.pop("calendar_uuid", None)
        return result


class ProviderMessengerEntityJSONPacker(ProviderJSONPacker):
    entity_type = None

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["urn"] = urns.build(self.entity_type, obj.uuid)
        for name in (
            "provider_uuid",
            "project_id",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "source_name",
            "source",
        ):
            result.pop(name, None)
        return result


class ProviderMessengerUserJSONPacker(ProviderMessengerEntityJSONPacker):
    entity_type = urns.MESSENGER_USER


class ProviderMessengerStreamJSONPacker(ProviderMessengerEntityJSONPacker):
    entity_type = urns.MESSENGER_STREAM

    def unpack(self, value):
        data = orjson.loads(value)
        _entity_type, entity_uuid = urns.parse(
            data.pop("owner_urn"),
            expected_type=urns.MESSENGER_USER,
        )
        data["user_uuid"] = str(entity_uuid)
        direct_user_urn = data.pop("direct_user_urn", None)
        if direct_user_urn is not None:
            _entity_type, direct_user_uuid = urns.parse(
                direct_user_urn,
                expected_type=urns.MESSENGER_USER,
            )
            data["direct_user_uuid"] = str(direct_user_uuid)
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["owner_urn"] = urns.build(urns.MESSENGER_USER, obj.user_uuid)
        result.pop("user_uuid", None)
        if obj.direct_user_uuid is not None:
            result["direct_user_urn"] = urns.build(
                urns.MESSENGER_USER,
                obj.direct_user_uuid,
            )
        result.pop("direct_user_uuid", None)
        return result


class ProviderMessengerTopicJSONPacker(ProviderMessengerEntityJSONPacker):
    entity_type = urns.MESSENGER_TOPIC

    def unpack(self, value):
        data = orjson.loads(value)
        _entity_type, entity_uuid = urns.parse(
            data.pop("stream_urn"),
            expected_type=urns.MESSENGER_STREAM,
        )
        data["stream_uuid"] = str(entity_uuid)
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["stream_urn"] = urns.build(urns.MESSENGER_STREAM, obj.stream_uuid)
        result.pop("stream_uuid", None)
        return result


class ProviderMessengerMessageJSONPacker(ProviderMessengerEntityJSONPacker):
    entity_type = urns.MESSENGER_MESSAGE
    urn_fields = (
        ("stream_urn", "stream_uuid", urns.MESSENGER_STREAM),
        ("topic_urn", "topic_uuid", urns.MESSENGER_TOPIC),
        ("author_urn", "user_uuid", urns.MESSENGER_USER),
    )

    def unpack(self, value):
        data = orjson.loads(value)
        for urn_field, uuid_field, entity_type in self.urn_fields:
            if urn_field not in data:
                continue
            _parsed_type, entity_uuid = urns.parse(
                data.pop(urn_field),
                expected_type=entity_type,
            )
            data[uuid_field] = str(entity_uuid)
        return super().unpack(orjson.dumps(data))

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        for urn_field, uuid_field, entity_type in self.urn_fields:
            result[urn_field] = urns.build(entity_type, getattr(obj, uuid_field))
            result.pop(uuid_field, None)
        return result


class ProviderMessengerReactionJSONPacker(ProviderMessengerMessageJSONPacker):
    entity_type = urns.MESSENGER_REACTION
    urn_fields = (
        ("message_urn", "message_uuid", urns.MESSENGER_MESSAGE),
        ("author_urn", "user_uuid", urns.MESSENGER_USER),
    )


class ProviderExternalAccountJSONPacker(ra_packers.JSONPacker):
    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        settings = dict(result["account_settings"])
        settings["server_url"] = result["server_url"]
        return {
            "uuid": result["uuid"],
            "kind": result["account_type"],
            "settings": settings,
            "updated_at": result["updated_at"],
            "status": result["access_status"],
        }


class ProviderBlobJSONPacker(ra_packers.MultipartPacker):
    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        result["urn"] = urns.build(urns.FILE, obj.uuid)
        for name in (
            "project_id",
            "user_uuid",
            "stream_uuid",
            "provider_uuid",
            "external_account_uuid",
            "storage_type",
            "storage_id",
            "storage_object_id",
            "description",
        ):
            result.pop(name, None)
        return result


def _mail_attachment_payloads(message, session=None):
    attachments = groupware_models.MailAttachment.objects.get_all(
        filters={
            "message_uuid": dm_filters.EQ(message.uuid),
            "project_id": dm_filters.EQ(message.project_id),
            "user_uuid": dm_filters.EQ(message.user_uuid),
        },
        order_by={"created_at": "asc"},
        session=session,
    )
    return [
        {
            "urn": urns.build(urns.FILE, attachment.uuid),
            "name": attachment.name,
            "content_type": attachment.content_type,
            "content_id": attachment.content_id,
            "size_bytes": attachment.size_bytes,
            "hash": attachment.hash,
        }
        for attachment in attachments
    ]


def _matches_values(resource, values):
    return all(getattr(resource, name) == value for name, value in values.items())


class ProviderController(ra_controllers.BaseResourceControllerPaginated):
    __packer__ = WorkspaceProviderJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceWorkspaceProvider,
        convert_underscore=False,
        process_filters=True,
    )

    def update(self, uuid, **kwargs):
        provider = models.WorkspaceProvider.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(uuid)},
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        values = kwargs.copy()
        values.pop("uuid", None)
        values["last_seen_at"] = now
        values["registered_at"] = now
        values["enabled"] = True
        if provider is None:
            provider = models.WorkspaceProvider(uuid=uuid, **values)
            provider.insert()
            return provider
        provider.update_dm(values=values)
        provider.update()
        return provider


class ProviderExternalAccountController(
    ra_controllers.BaseResourceControllerPaginated,
):
    __packer__ = ProviderExternalAccountJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceExternalAccount,
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _provider_filter(provider):
        return {
            "provider_uuid": dm_filters.EQ(provider.uuid),
        }

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(
            filters,
            self._provider_filter(parent_resource),
        )
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    @ra_actions.post
    def status(self, resource, *args, **kwargs):
        values = {
            "access_status": kwargs["status"],
            "access_checked_at": datetime.datetime.now(datetime.timezone.utc),
            "access_last_error": kwargs.get("safe_error"),
        }
        if values["access_status"] == (
            messenger_models.ExternalAccountAccessStatus.CONFIRMED.value
        ):
            values["status"] = messenger_models.ExternalAccountStatus.ACTIVE.value
            values["access_confirmed_at"] = values["access_checked_at"]
        resource.update_dm(values=values)
        resource.update()
        workspace_events.create_external_account_updated_event(resource)
        return resource


class ProviderBlobController(ra_controllers.BaseResourceControllerPaginated):
    __packer__ = ProviderBlobJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceProviderBlob,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "stream_uuid",
            "provider_uuid",
            "external_account_uuid",
            "storage_type",
            "storage_id",
            "storage_object_id",
            "description",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _multipart_value(part, default=None):
        if part is None:
            return default
        return getattr(part, "value", part)

    def process_result(self, result, *args, **kwargs):
        if isinstance(result, webob.Response):
            return result
        return super().process_result(result, *args, **kwargs)

    @staticmethod
    def _owned_filters(provider):
        return {"provider_uuid": dm_filters.EQ(provider.uuid)}

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(filters, self._owned_filters(parent_resource))
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return messenger_models.WorkspaceFile.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def create(self, parent_resource=None, **kwargs):
        if not kwargs.pop("multipart", False):
            raise ra_exc.ValidationErrorException()
        parts = kwargs["parts"]
        file_part = parts.get("file")
        account_uuid = self._multipart_value(parts.get("external_account_uuid"))
        if file_part is None or account_uuid is None:
            raise ra_exc.ValidationErrorException()
        account = messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )
        file_part.file.seek(0)
        data = file_part.file.read()
        file_part.file.seek(0)
        digest = hashlib.sha256(data).hexdigest()
        supplied_hash = self._multipart_value(parts.get("hash"))
        if supplied_hash is not None and supplied_hash != digest:
            raise ra_exc.ValidationErrorException()

        file_uuid = sys_uuid.uuid4()
        storage_info = file_storage.save_workspace_file(file_uuid, data)
        try:
            blob = messenger_models.WorkspaceFile(
                uuid=file_uuid,
                project_id=account.project_id,
                user_uuid=account.user_uuid,
                stream_uuid=None,
                provider_uuid=parent_resource.uuid,
                external_account_uuid=account.uuid,
                name=self._multipart_value(parts.get("name"), file_part.filename),
                description="",
                content_type=(
                    self._multipart_value(parts.get("content_type"))
                    or file_part.type
                    or "application/octet-stream"
                ),
                size_bytes=len(data),
                hash=digest,
                storage_type=storage_info.storage_type,
                storage_id=storage_info.storage_id,
                storage_object_id=storage_info.storage_object_id,
            )
            blob.insert()
            messenger_helpers.get_or_create_workspace_file_access(
                project_id=account.project_id,
                file_uuid=blob.uuid,
                user_uuid=account.user_uuid,
            )
            return blob
        except Exception:
            file_storage.delete_workspace_file(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
                storage_object_id=storage_info.storage_object_id,
            )
            raise

    def delete(self, uuid, parent_resource=None):
        blob = self.get(uuid=uuid, parent_resource=parent_resource)
        file_storage.delete_workspace_file(
            file_uuid=blob.uuid,
            storage_type=blob.storage_type,
            storage_object_id=blob.storage_object_id,
        )
        blob.delete()
        return blob

    @staticmethod
    def _content_disposition(resource):
        quoted_name = resource.name.replace("\\", "\\\\").replace('"', '\\"')
        encoded_name = urllib.parse.quote(resource.name)
        return (
            f"attachment; filename=\"{quoted_name}\"; filename*=UTF-8''{encoded_name}"
        )

    @ra_actions.get
    def download(self, resource, *args, **kwargs):
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
                "Content-Disposition": self._content_disposition(resource),
            },
        )


ProviderBlobController.create.openapi_schema = oa_utils.Schema(
    summary="Upload provider blob",
    parameters=(),
    responses=oa_c.build_openapi_create_response("ProviderBlob_Create"),
    request_body={
        "description": "Upload a provider-owned Workspace blob",
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["external_account_uuid", "file"],
                    "properties": {
                        "external_account_uuid": {
                            "format": "uuid",
                            "type": "string",
                        },
                        "file": {"format": "binary", "type": "string"},
                        "name": {"type": "string"},
                        "content_type": {"type": "string"},
                        "hash": {"type": "string"},
                    },
                },
            },
        },
    },
)

ProviderBlobController.download.openapi_schema = oa_utils.Schema(
    summary="Download provider blob",
    parameters=(),
    responses=oa_c.build_openapi_response_octet_stream("Download blob"),
)


class ProviderMessengerBaseController(
    ra_controllers.BaseResourceControllerPaginated,
):
    model_class = None

    def _get_account(self, provider, account_uuid):
        return messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "account_type": dm_filters.EQ(
                    messenger_models.ExternalAccountType.ZULIP.value,
                ),
            },
        )

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(
            filters,
            {"provider_uuid": dm_filters.EQ(parent_resource.uuid)},
        )
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return self.model_class.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def _find_identity(self, provider, account, provider_external_id):
        candidates = self.model_class.objects.get_all(
            filters={
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "provider_external_id": dm_filters.EQ(provider_external_id),
            },
        )
        account_scope = account.source_scope or account.server_url
        for candidate in candidates:
            candidate_account = (
                messenger_models.ExternalAccount.objects.get_one_or_none(
                    filters={
                        "uuid": dm_filters.EQ(candidate.external_account_uuid),
                        "provider_uuid": dm_filters.EQ(provider.uuid),
                    },
                )
            )
            if candidate_account is None:
                continue
            candidate_scope = (
                candidate_account.source_scope or candidate_account.server_url
            )
            if candidate_scope == account_scope:
                return candidate
        return None

    @staticmethod
    def _preserve_identity_account(values, existing):
        if existing is not None:
            values["external_account_uuid"] = existing.external_account_uuid

    @staticmethod
    def _same_account_scope(left, right):
        return (left.source_scope or left.server_url) == (
            right.source_scope or right.server_url
        )

    def _get_scoped_entity(self, model_class, provider, account, entity_uuid):
        entity = model_class.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
            },
        )
        entity_account = messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(entity.external_account_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
            },
        )
        if not self._same_account_scope(account, entity_account):
            raise ra_exc.ValidationErrorException()
        return entity

    @staticmethod
    def _delivery_values(provider, account, include_timestamp=True):
        values = {
            "provider_uuid": provider.uuid,
            "external_account_uuid": account.uuid,
            "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
            "delivery_error": None,
        }
        if include_timestamp:
            values["delivery_updated_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            )
        return values

    @staticmethod
    def _validate_identity(existing, uuid):
        if existing is not None and existing.uuid != uuid:
            raise ra_exc.ValidationErrorException()

    @staticmethod
    def _zulip_id(provider_external_id):
        try:
            return int(provider_external_id)
        except (TypeError, ValueError):
            raise ra_exc.ValidationErrorException() from None


class ProviderMessengerUserController(ProviderMessengerBaseController):
    model_class = messenger_models.WorkspaceUser
    __packer__ = ProviderMessengerUserJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerUser,
        hidden_fields=["provider_uuid", "source", "last_ping_at"],
        convert_underscore=False,
        process_filters=True,
    )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_account_uuid"],
        )
        existing = self._find_identity(
            parent_resource,
            account,
            values["provider_external_id"],
        )
        self._validate_identity(existing, uuid)
        values.update(
            {
                "provider_uuid": parent_resource.uuid,
                "external_account_uuid": account.uuid,
                "source": messenger_models.WorkspaceUserSource.ZULIP.value,
            },
        )
        self._preserve_identity_account(values, existing)
        if existing is None:
            values["last_ping_at"] = datetime.datetime.now(datetime.timezone.utc)
            user = messenger_models.WorkspaceUser(uuid=uuid, **values)
            user.insert()
        else:
            if _matches_values(existing, values):
                return existing
            existing.update_dm(values=values)
            existing.update_dm(
                values={
                    "last_ping_at": datetime.datetime.now(datetime.timezone.utc),
                },
            )
            existing.update()
            user = existing
        workspace_events.create_user_updated_events(
            user,
            account.project_id,
            [account.user_uuid],
        )
        return user

    def delete(self, uuid, parent_resource=None):
        user = self.get(uuid=uuid, parent_resource=parent_resource)
        user.update_dm(
            values={"status": messenger_models.WorkspaceUserStatus.OFFLINE.value},
        )
        user.update()
        account = self._get_account(parent_resource, user.external_account_uuid)
        workspace_events.create_user_updated_events(
            user,
            account.project_id,
            [account.user_uuid],
        )
        return user


class ProviderMessengerStreamController(ProviderMessengerBaseController):
    model_class = messenger_models.WorkspaceStream
    __packer__ = ProviderMessengerStreamJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerStream,
        hidden_fields=[
            "provider_uuid",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "source_name",
            "source",
            "private_index",
            "default_topic_uuid",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _events(stream, created):
        for user_stream in messenger_models.WorkspaceUserStream.objects.get_all(
            filters={
                "uuid": dm_filters.EQ(stream.uuid),
                "project_id": dm_filters.EQ(stream.project_id),
            },
        ):
            if created:
                workspace_events.create_stream_event(user_stream)
            else:
                workspace_events.create_stream_updated_event(user_stream)

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_account_uuid"],
        )
        owner = messenger_models.WorkspaceUser.objects.get_one(
            filters={"uuid": dm_filters.EQ(values["user_uuid"])},
        )
        existing = self._find_identity(
            parent_resource,
            account,
            values["provider_external_id"],
        )
        self._validate_identity(existing, uuid)
        values.update(
            self._delivery_values(
                parent_resource,
                account,
                include_timestamp=existing is None,
            ),
        )
        values.update(
            {
                "project_id": account.project_id,
                "user_uuid": owner.uuid,
                "source_name": messenger_base.SourceName.ZULIP.value,
                "source": messenger_base.ZulipSource(
                    stream_id=self._zulip_id(values["provider_external_id"]),
                    server_url=account.server_url,
                ),
            },
        )
        self._preserve_identity_account(values, existing)
        created = existing is None
        if created:
            stream = messenger_models.WorkspaceStream(uuid=uuid, **values)
            stream.insert()
            messenger_helpers.get_or_create_workspace_stream_binding(
                project_id=account.project_id,
                stream_uuid=stream.uuid,
                user_uuid=account.user_uuid,
                who_uuid=account.user_uuid,
                role=messenger_models.WorkspaceStreamRole.OWNER.value,
            )
        else:
            if _matches_values(existing, values):
                stream = existing
            else:
                existing.update_dm(values=values)
                existing.update_dm(
                    values={
                        "delivery_updated_at": datetime.datetime.now(
                            datetime.timezone.utc,
                        ),
                    },
                )
                existing.update()
                stream = existing
                self._events(stream, created=False)
            messenger_helpers.get_or_create_workspace_stream_binding(
                project_id=account.project_id,
                stream_uuid=stream.uuid,
                user_uuid=account.user_uuid,
                who_uuid=account.user_uuid,
                role=messenger_models.WorkspaceStreamRole.OWNER.value,
            )
        return stream

    def delete(self, uuid, parent_resource=None):
        stream = self.get(uuid=uuid, parent_resource=parent_resource)
        account = self._get_account(parent_resource, stream.external_account_uuid)
        stream.update_dm(values=self._delivery_values(parent_resource, account))
        stream.update()
        return messenger_helpers.delete_workspace_user_stream(
            project_id=account.project_id,
            user_uuid=account.user_uuid,
            stream_uuid=stream.uuid,
        )


class ProviderMessengerTopicController(ProviderMessengerBaseController):
    model_class = messenger_models.WorkspaceStreamTopic
    __packer__ = ProviderMessengerTopicJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerTopic,
        hidden_fields=[
            "provider_uuid",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _events(topic, created):
        user_topics = messenger_models.WorkspaceUserTopic.objects.get_all(
            filters={
                "uuid": dm_filters.EQ(topic.uuid),
                "project_id": dm_filters.EQ(topic.project_id),
            },
        )
        for user_topic in user_topics:
            if created:
                workspace_events.create_topic_event(user_topic)
            else:
                workspace_events.create_topic_updated_event(user_topic)

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_account_uuid"],
        )
        stream = self._get_scoped_entity(
            messenger_models.WorkspaceStream,
            parent_resource,
            account,
            values["stream_uuid"],
        )
        existing = self._find_identity(
            parent_resource,
            account,
            values["provider_external_id"],
        )
        self._validate_identity(existing, uuid)
        values.update(
            self._delivery_values(
                parent_resource,
                account,
                include_timestamp=existing is None,
            ),
        )
        values.update(
            {
                "project_id": account.project_id,
                "stream_uuid": stream.uuid,
                "source_name": messenger_base.SourceName.ZULIP.value,
                "source": messenger_base.ZulipSource(
                    stream_id=self._zulip_id(stream.provider_external_id),
                    server_url=account.server_url,
                    topic_name=values["name"],
                ),
            },
        )
        self._preserve_identity_account(values, existing)
        if existing is None:
            topic = messenger_helpers.create_workspace_stream_topic_with_flags(
                uuid=uuid,
                **values,
            )
            self._events(topic, created=True)
        else:
            if _matches_values(existing, values):
                return existing
            existing.update_dm(values=values)
            existing.update_dm(
                values={
                    "delivery_updated_at": datetime.datetime.now(
                        datetime.timezone.utc,
                    ),
                },
            )
            existing.update()
            topic = existing
            self._events(topic, created=False)
        return topic

    def delete(self, uuid, parent_resource=None):
        topic = self.get(uuid=uuid, parent_resource=parent_resource)
        account = self._get_account(parent_resource, topic.external_account_uuid)
        topic.update_dm(values=self._delivery_values(parent_resource, account))
        topic.update()
        return messenger_helpers.delete_workspace_user_stream_topic(
            project_id=account.project_id,
            user_uuid=account.user_uuid,
            topic_uuid=topic.uuid,
        )


class ProviderMessengerMessageController(ProviderMessengerBaseController):
    model_class = messenger_models.WorkspaceMessage
    __packer__ = ProviderMessengerMessageJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerMessage,
        hidden_fields=[
            "provider_uuid",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_account_uuid"],
        )
        author = self._get_scoped_entity(
            messenger_models.WorkspaceUser,
            parent_resource,
            account,
            values["user_uuid"],
        )
        stream = self._get_scoped_entity(
            messenger_models.WorkspaceStream,
            parent_resource,
            account,
            values["stream_uuid"],
        )
        topic = self._get_scoped_entity(
            messenger_models.WorkspaceStreamTopic,
            parent_resource,
            account,
            values["topic_uuid"],
        )
        if topic.stream_uuid != stream.uuid:
            raise ra_exc.ValidationErrorException()
        existing = self._find_identity(
            parent_resource,
            account,
            values["provider_external_id"],
        )
        self._validate_identity(existing, uuid)
        values.update(
            self._delivery_values(
                parent_resource,
                account,
                include_timestamp=existing is None,
            ),
        )
        values.update(
            {
                "project_id": account.project_id,
                "user_uuid": author.uuid,
                "stream_uuid": stream.uuid,
                "topic_uuid": topic.uuid,
                "source_name": messenger_base.SourceName.ZULIP.value,
                "source": messenger_base.ZulipSource(
                    stream_id=self._zulip_id(stream.provider_external_id),
                    server_url=account.server_url,
                    topic_name=topic.name,
                    message_id=self._zulip_id(values["provider_external_id"]),
                ),
            },
        )
        self._preserve_identity_account(values, existing)
        if existing is None:
            messenger_helpers.get_or_create_workspace_stream_binding(
                project_id=account.project_id,
                stream_uuid=stream.uuid,
                user_uuid=author.uuid,
                who_uuid=account.user_uuid,
                role=messenger_models.WorkspaceStreamRole.MEMBER.value,
            )
            return messenger_helpers.create_workspace_user_message(
                uuid=uuid,
                return_visible=False,
                enforce_visibility=False,
                **values,
            )
        if _matches_values(existing, values):
            return existing
        existing.update_dm(values=values)
        messenger_helpers.get_or_create_workspace_stream_binding(
            project_id=account.project_id,
            stream_uuid=stream.uuid,
            user_uuid=author.uuid,
            who_uuid=account.user_uuid,
            role=messenger_models.WorkspaceStreamRole.MEMBER.value,
        )
        existing.update_dm(
            values={
                "delivery_updated_at": datetime.datetime.now(
                    datetime.timezone.utc,
                ),
            },
        )
        existing.update()
        messenger_helpers._create_workspace_message_updated_events(
            project_id=existing.project_id,
            message_uuid=existing.uuid,
        )
        return existing

    def delete(self, uuid, parent_resource=None):
        message = self.get(uuid=uuid, parent_resource=parent_resource)
        account = self._get_account(parent_resource, message.external_account_uuid)
        message.update_dm(values=self._delivery_values(parent_resource, account))
        message.update()
        return messenger_helpers.delete_workspace_user_message(
            project_id=account.project_id,
            user_uuid=message.user_uuid,
            message_uuid=message.uuid,
        )

    @ra_actions.post
    def flags(self, resource, *args, **kwargs):
        provider = models.WorkspaceProvider.objects.get_one(
            filters={"uuid": dm_filters.EQ(resource.provider_uuid)},
        )
        account = self._get_account(
            provider,
            kwargs["external_account_uuid"],
        )
        resource_account = messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(resource.external_account_uuid),
                "provider_uuid": dm_filters.EQ(resource.provider_uuid),
            },
        )
        if not self._same_account_scope(account, resource_account):
            raise ra_exc.ValidationErrorException()
        values = {name: kwargs[name] for name in ("read", "starred") if name in kwargs}
        if not values:
            raise ra_exc.ValidationErrorException()
        messenger_helpers.sync_workspace_user_message_flags(
            project_id=account.project_id,
            user_uuid=account.user_uuid,
            message_uuid=resource.uuid,
            values=values,
        )
        return resource


class ProviderMessengerReactionController(ProviderMessengerBaseController):
    model_class = messenger_models.WorkspaceMessageReactions
    __packer__ = ProviderMessengerReactionJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerReaction,
        hidden_fields=[
            "provider_uuid",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_account_uuid"],
        )
        author = self._get_scoped_entity(
            messenger_models.WorkspaceUser,
            parent_resource,
            account,
            values["user_uuid"],
        )
        message = self._get_scoped_entity(
            messenger_models.WorkspaceMessage,
            parent_resource,
            account,
            values["message_uuid"],
        )
        existing = self._find_identity(
            parent_resource,
            account,
            values["provider_external_id"],
        )
        self._validate_identity(existing, uuid)
        values.update(
            self._delivery_values(
                parent_resource,
                account,
                include_timestamp=existing is None,
            ),
        )
        values.update(
            {
                "project_id": account.project_id,
                "user_uuid": author.uuid,
                "message_uuid": message.uuid,
            },
        )
        self._preserve_identity_account(values, existing)
        if existing is None:
            return messenger_helpers.create_workspace_message_reaction(
                uuid=uuid,
                enforce_visibility=False,
                **values,
            )
        if _matches_values(existing, values):
            return existing
        old_message_uuid = existing.message_uuid
        old_emoji_name = existing.emoji_name
        existing.update_dm(values=values)
        old_message = messenger_models.WorkspaceMessage.objects.get_one(
            filters={"uuid": dm_filters.EQ(old_message_uuid)},
        )
        existing.update_dm(
            values={
                "delivery_updated_at": datetime.datetime.now(
                    datetime.timezone.utc,
                ),
            },
        )
        existing.update()
        workspace_events.create_message_reaction_updated_event(
            existing,
            message,
            old_message,
            old_emoji_name,
        )
        messenger_helpers._create_workspace_message_updated_events(
            account.project_id,
            message.uuid,
        )
        return existing

    def delete(self, uuid, parent_resource=None):
        reaction = self.get(uuid=uuid, parent_resource=parent_resource)
        account = self._get_account(
            parent_resource,
            reaction.external_account_uuid,
        )
        reaction.update_dm(values=self._delivery_values(parent_resource, account))
        reaction.update()
        return messenger_helpers.delete_workspace_message_reaction(
            project_id=reaction.project_id,
            user_uuid=reaction.user_uuid,
            reaction_uuid=reaction.uuid,
            enforce_visibility=False,
        )


class ProviderMailBaseController(
    ra_controllers.BaseResourceControllerPaginated,
):
    def _prepare_filter(self, param_name, value):
        if param_name == "external_account_uuid":
            param_name = "external_user_uuid"
        return super()._prepare_filter(param_name, value)

    def _get_account(self, provider, account_uuid):
        return messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "account_type": dm_filters.EQ(
                    messenger_models.ExternalAccountType.MAIL.value,
                ),
            },
        )

    @staticmethod
    def _owned_filters(provider):
        return {"provider_uuid": dm_filters.EQ(provider.uuid)}


class ProviderMailFolderController(ProviderMailBaseController):
    __packer__ = ProviderMailFolderJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMailFolder,
        hidden_fields=[
            "sync_cursor",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(filters, self._owned_filters(parent_resource))
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return groupware_models.MailFolder.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_user_uuid"],
        )
        existing = groupware_models.MailFolder.objects.get_one_or_none(
            filters={
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "provider_external_id": dm_filters.EQ(
                    values["provider_external_id"],
                ),
            },
        )
        values.update(
            {
                "provider_uuid": parent_resource.uuid,
                "project_id": account.project_id,
                "user_uuid": account.user_uuid,
                "external_user_uuid": account.uuid,
                "source_name": groupware_models.MailSource.IMAP.value,
                "source": {},
                "sync_status": groupware_models.SyncStatus.SYNCED.value,
                "sync_error": None,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "deleted": False,
            },
        )
        if existing is not None and existing.uuid != uuid:
            raise ra_exc.ValidationErrorException()
        if existing is None:
            values["delivery_updated_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            )
            folder = groupware_models.MailFolder(
                uuid=uuid,
                **values,
            )
            folder.insert()
            kind = workspace_events.MAIL_FOLDER_CREATED_EVENT
        else:
            if _matches_values(existing, values):
                return existing
            existing.update_dm(values=values)
            existing.delivery_updated_at = datetime.datetime.now(
                datetime.timezone.utc,
            )
            existing.update()
            folder = existing
            kind = workspace_events.MAIL_FOLDER_UPDATED_EVENT
        workspace_events.create_groupware_event(folder, kind)
        return folder

    def delete(self, uuid, parent_resource=None):
        folder = self.get(uuid=uuid, parent_resource=parent_resource)
        folder.update_dm(values={"deleted": True})
        folder.update()
        workspace_events.create_groupware_event(
            folder,
            workspace_events.MAIL_FOLDER_DELETED_EVENT,
        )
        return folder


class ProviderMailMessageController(ProviderMailBaseController):
    __packer__ = ProviderMailMessageJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMailMessage,
        hidden_fields=[
            "sync_status",
            "sync_error",
            "source_name",
            "source",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(filters, self._owned_filters(parent_resource))
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return groupware_models.MailMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def _get_folder(self, provider, folder_uuid, account_uuid):
        return groupware_models.MailFolder.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(folder_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "external_user_uuid": dm_filters.EQ(account_uuid),
            },
        )

    @staticmethod
    def _sync_attachments(message, provider, account, attachment_values):
        if attachment_values is None:
            return
        if not isinstance(attachment_values, list):
            raise ra_exc.ValidationErrorException()
        expected_uuids = set()
        for value in attachment_values:
            if not isinstance(value, dict) or "urn" not in value:
                raise ra_exc.ValidationErrorException()
            _entity_type, blob_uuid = urns.parse(
                value["urn"],
                expected_type=urns.FILE,
            )
            blob = messenger_models.WorkspaceFile.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(blob_uuid),
                    "provider_uuid": dm_filters.EQ(provider.uuid),
                    "external_account_uuid": dm_filters.EQ(account.uuid),
                },
            )
            expected_uuids.add(blob.uuid)
            attachment = groupware_models.MailAttachment.objects.get_one_or_none(
                filters={"uuid": dm_filters.EQ(blob.uuid)},
            )
            values = {
                "project_id": message.project_id,
                "user_uuid": message.user_uuid,
                "message_uuid": message.uuid,
                "name": blob.name,
                "content_id": value.get("content_id"),
                "content_type": blob.content_type,
                "size_bytes": blob.size_bytes,
                "hash": blob.hash,
                "storage_type": blob.storage_type,
                "storage_id": blob.storage_id,
                "storage_object_id": blob.storage_object_id,
            }
            if attachment is None:
                groupware_models.MailAttachment(uuid=blob.uuid, **values).insert()
            else:
                if (
                    attachment.project_id != message.project_id
                    or attachment.user_uuid != message.user_uuid
                ):
                    raise ra_exc.ValidationErrorException()
                attachment.update_dm(values=values)
                attachment.update()
        for attachment in groupware_models.MailAttachment.objects.get_all(
            filters={
                "message_uuid": dm_filters.EQ(message.uuid),
                "project_id": dm_filters.EQ(message.project_id),
                "user_uuid": dm_filters.EQ(message.user_uuid),
            },
        ):
            if attachment.uuid not in expected_uuids:
                attachment.delete()

    @staticmethod
    def _normalized_attachments(message, provider, account, attachment_values):
        if attachment_values is None:
            return None
        if not isinstance(attachment_values, list):
            raise ra_exc.ValidationErrorException()
        result = []
        seen_uuids = set()
        for value in attachment_values:
            if not isinstance(value, dict) or "urn" not in value:
                raise ra_exc.ValidationErrorException()
            _entity_type, blob_uuid = urns.parse(
                value["urn"],
                expected_type=urns.FILE,
            )
            if blob_uuid in seen_uuids:
                raise ra_exc.ValidationErrorException()
            seen_uuids.add(blob_uuid)
            blob = messenger_models.WorkspaceFile.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(blob_uuid),
                    "provider_uuid": dm_filters.EQ(provider.uuid),
                    "external_account_uuid": dm_filters.EQ(account.uuid),
                },
            )
            result.append(
                {
                    "urn": urns.build(urns.FILE, blob.uuid),
                    "name": blob.name,
                    "content_type": blob.content_type,
                    "content_id": value.get("content_id"),
                    "size_bytes": blob.size_bytes,
                    "hash": blob.hash,
                },
            )
        return sorted(result, key=lambda item: item["urn"])

    @classmethod
    def _attachments_changed(
        cls,
        message,
        provider,
        account,
        attachment_values,
    ):
        normalized = cls._normalized_attachments(
            message,
            provider,
            account,
            attachment_values,
        )
        if normalized is None:
            return False
        current = sorted(
            _mail_attachment_payloads(message),
            key=lambda item: item["urn"],
        )
        return normalized != current

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        attachment_values = values.pop("_provider_attachments", None)
        account = self._get_account(
            parent_resource,
            values["external_user_uuid"],
        )
        folder = self._get_folder(
            parent_resource,
            values["folder_uuid"],
            account.uuid,
        )
        existing = groupware_models.MailMessage.objects.get_one_or_none(
            filters={
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "provider_external_id": dm_filters.EQ(
                    values["provider_external_id"],
                ),
            },
        )
        values.update(
            {
                "provider_uuid": parent_resource.uuid,
                "project_id": account.project_id,
                "user_uuid": account.user_uuid,
                "external_user_uuid": account.uuid,
                "folder_uuid": folder.uuid,
                "source_name": groupware_models.MailSource.IMAP.value,
                "source": {},
                "sync_status": groupware_models.SyncStatus.SYNCED.value,
                "sync_error": None,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "deleted": False,
            },
        )
        if existing is not None and existing.uuid != uuid:
            raise ra_exc.ValidationErrorException()
        if existing is None:
            values["delivery_updated_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            )
            message = groupware_models.MailMessage(
                uuid=uuid,
                **values,
            )
            message.insert()
            kind = workspace_events.MAIL_MESSAGE_CREATED_EVENT
            attachments_changed = attachment_values is not None
        else:
            attachments_changed = self._attachments_changed(
                existing,
                parent_resource,
                account,
                attachment_values,
            )
            if _matches_values(existing, values) and not attachments_changed:
                return existing
            existing.update_dm(values=values)
            existing.delivery_updated_at = datetime.datetime.now(
                datetime.timezone.utc,
            )
            existing.update()
            message = existing
            kind = workspace_events.MAIL_MESSAGE_UPDATED_EVENT
        if attachments_changed:
            self._sync_attachments(
                message,
                parent_resource,
                account,
                attachment_values,
            )
        workspace_events.create_groupware_event(message, kind)
        return message

    def delete(self, uuid, parent_resource=None):
        message = self.get(uuid=uuid, parent_resource=parent_resource)
        message.update_dm(values={"deleted": True})
        message.update()
        workspace_events.create_groupware_event(
            message,
            workspace_events.MAIL_MESSAGE_DELETED_EVENT,
        )
        return message


class ProviderCalendarBaseController(
    ra_controllers.BaseResourceControllerPaginated,
):
    def _prepare_filter(self, param_name, value):
        if param_name == "external_account_uuid":
            param_name = "external_user_uuid"
        return super()._prepare_filter(param_name, value)

    def _get_account(self, provider, account_uuid):
        return messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "account_type": dm_filters.EQ(
                    messenger_models.ExternalAccountType.CALENDAR.value,
                ),
            },
        )

    @staticmethod
    def _owned_filters(provider):
        return {"provider_uuid": dm_filters.EQ(provider.uuid)}


class ProviderCalendarController(ProviderCalendarBaseController):
    __packer__ = ProviderCalendarJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceCalendar,
        hidden_fields=[
            "ctag",
            "sync_token",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(filters, self._owned_filters(parent_resource))
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return groupware_models.Calendar.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_user_uuid"],
        )
        existing = groupware_models.Calendar.objects.get_one_or_none(
            filters={
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "provider_external_id": dm_filters.EQ(
                    values["provider_external_id"],
                ),
            },
        )
        values.update(
            {
                "provider_uuid": parent_resource.uuid,
                "project_id": account.project_id,
                "user_uuid": account.user_uuid,
                "external_user_uuid": account.uuid,
                "ctag": None,
                "source_name": groupware_models.CalendarSource.CALDAV.value,
                "source": {},
                "sync_token": None,
                "sync_status": groupware_models.SyncStatus.SYNCED.value,
                "sync_error": None,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "deleted": False,
            },
        )
        if existing is not None and existing.uuid != uuid:
            raise ra_exc.ValidationErrorException()
        if existing is None:
            values["delivery_updated_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            )
            calendar = groupware_models.Calendar(uuid=uuid, **values)
            calendar.insert()
            kind = workspace_events.CALENDAR_CREATED_EVENT
        else:
            if _matches_values(existing, values):
                return existing
            existing.update_dm(values=values)
            existing.delivery_updated_at = datetime.datetime.now(
                datetime.timezone.utc,
            )
            existing.update()
            calendar = existing
            kind = workspace_events.CALENDAR_UPDATED_EVENT
        workspace_events.create_groupware_event(calendar, kind)
        return calendar

    def delete(self, uuid, parent_resource=None):
        calendar = self.get(uuid=uuid, parent_resource=parent_resource)
        calendar.update_dm(
            values={
                "deleted": True,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "delivery_updated_at": datetime.datetime.now(
                    datetime.timezone.utc,
                ),
            },
        )
        calendar.update()
        workspace_events.create_groupware_event(
            calendar,
            workspace_events.CALENDAR_DELETED_EVENT,
        )
        return calendar


class ProviderCalendarEventController(ProviderCalendarBaseController):
    __packer__ = ProviderCalendarEventJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceCalendarEvent,
        hidden_fields=[
            "ics",
            "etag",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = dm_filters.AND(filters, self._owned_filters(parent_resource))
        return super().filter(filters=filters, order_by=order_by)

    def get(self, uuid, parent_resource=None):
        return groupware_models.CalendarEvent.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
            },
        )

    def _get_calendar(self, provider, calendar_uuid, account_uuid):
        return groupware_models.Calendar.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(calendar_uuid),
                "provider_uuid": dm_filters.EQ(provider.uuid),
                "external_user_uuid": dm_filters.EQ(account_uuid),
            },
        )

    def update(self, uuid, parent_resource=None, **kwargs):
        values = kwargs.copy()
        account = self._get_account(
            parent_resource,
            values["external_user_uuid"],
        )
        calendar = self._get_calendar(
            parent_resource,
            values["calendar_uuid"],
            account.uuid,
        )
        existing = groupware_models.CalendarEvent.objects.get_one_or_none(
            filters={
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "external_user_uuid": dm_filters.EQ(account.uuid),
                "provider_external_id": dm_filters.EQ(
                    values["provider_external_id"],
                ),
            },
        )
        values.update(
            {
                "provider_uuid": parent_resource.uuid,
                "project_id": account.project_id,
                "user_uuid": account.user_uuid,
                "external_user_uuid": account.uuid,
                "calendar_uuid": calendar.uuid,
                "ics": None,
                "etag": None,
                "source_name": groupware_models.CalendarSource.CALDAV.value,
                "source": {},
                "sync_status": groupware_models.SyncStatus.SYNCED.value,
                "sync_error": None,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "deleted": False,
            },
        )
        if existing is not None and existing.uuid != uuid:
            raise ra_exc.ValidationErrorException()
        if existing is None:
            values["delivery_updated_at"] = datetime.datetime.now(
                datetime.timezone.utc,
            )
            event = groupware_models.CalendarEvent(uuid=uuid, **values)
            event.insert()
            kind = workspace_events.CALENDAR_EVENT_CREATED_EVENT
        else:
            if _matches_values(existing, values):
                return existing
            existing.update_dm(values=values)
            existing.delivery_updated_at = datetime.datetime.now(
                datetime.timezone.utc,
            )
            existing.update()
            event = existing
            kind = workspace_events.CALENDAR_EVENT_UPDATED_EVENT
        workspace_events.create_groupware_event(event, kind)
        return event

    def delete(self, uuid, parent_resource=None):
        event = self.get(uuid=uuid, parent_resource=parent_resource)
        event.update_dm(
            values={
                "deleted": True,
                "delivery_status": models.ProviderCommandStatus.DELIVERED.value,
                "delivery_error": None,
                "delivery_updated_at": datetime.datetime.now(
                    datetime.timezone.utc,
                ),
            },
        )
        event.update()
        workspace_events.create_groupware_event(
            event,
            workspace_events.CALENDAR_EVENT_DELETED_EVENT,
        )
        return event


class ProviderMailCommandController(ProviderMailBaseController):
    __default_sort__ = {"created_at": "asc"}
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceProviderCommand,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = filters.copy()
        filters.update(
            {
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.MAIL.value),
            },
        )
        return ra_controllers.BaseResourceControllerPaginated.filter(
            self,
            filters=filters,
            order_by=order_by,
        )

    def get(self, uuid, parent_resource=None):
        return models.ProviderCommand.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.MAIL.value),
            },
        )

    @staticmethod
    def _get_entity(command):
        entity_type, entity_uuid = urns.parse(command.entity_urn)
        if entity_type == urns.MAIL_FOLDER:
            return groupware_models.MailFolder.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(entity_uuid),
                    "provider_uuid": dm_filters.EQ(command.provider_uuid),
                },
            )
        if entity_type == urns.MAIL_MESSAGE:
            return groupware_models.MailMessage.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(entity_uuid),
                    "provider_uuid": dm_filters.EQ(command.provider_uuid),
                },
            )
        raise ra_exc.ValidationErrorException()

    @ra_actions.post
    def result(self, resource, *args, **kwargs):
        status = kwargs["status"]
        if status not in (
            models.ProviderCommandStatus.DELIVERED.value,
            models.ProviderCommandStatus.FAILED.value,
        ):
            raise ra_exc.ValidationErrorException()
        now = datetime.datetime.now(datetime.timezone.utc)
        safe_error = kwargs.get("safe_error")
        if status == models.ProviderCommandStatus.DELIVERED.value:
            safe_error = None
        resource.update_dm(
            values={
                "status": status,
                "safe_error": safe_error,
                "completed_at": now,
            },
        )
        resource.update()
        entity = self._get_entity(resource)
        entity_values = {
            "delivery_status": status,
            "delivery_error": safe_error,
            "delivery_updated_at": now,
        }
        if kwargs.get("provider_external_id") is not None:
            entity_values["provider_external_id"] = kwargs["provider_external_id"]
        entity.update_dm(values=entity_values)
        entity.update()
        event_kind = workspace_events.MAIL_MESSAGE_UPDATED_EVENT
        if isinstance(entity, groupware_models.MailFolder):
            event_kind = workspace_events.MAIL_FOLDER_UPDATED_EVENT
        workspace_events.create_groupware_event(entity, event_kind)
        return resource


class ProviderCalendarCommandController(ProviderCalendarBaseController):
    __default_sort__ = {"created_at": "asc"}
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceCalendarProviderCommand,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = filters.copy()
        filters.update(
            {
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.CALENDAR.value),
            },
        )
        return ra_controllers.BaseResourceControllerPaginated.filter(
            self,
            filters=filters,
            order_by=order_by,
        )

    def get(self, uuid, parent_resource=None):
        return models.ProviderCommand.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.CALENDAR.value),
            },
        )

    @staticmethod
    def _get_entity(command):
        entity_type, entity_uuid = urns.parse(command.entity_urn)
        if entity_type == urns.CALENDAR:
            return groupware_models.Calendar.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(entity_uuid),
                    "provider_uuid": dm_filters.EQ(command.provider_uuid),
                },
            )
        if entity_type == urns.CALENDAR_EVENT:
            return groupware_models.CalendarEvent.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(entity_uuid),
                    "provider_uuid": dm_filters.EQ(command.provider_uuid),
                },
            )
        raise ra_exc.ValidationErrorException()

    @ra_actions.post
    def result(self, resource, *args, **kwargs):
        status = kwargs["status"]
        if status not in (
            models.ProviderCommandStatus.DELIVERED.value,
            models.ProviderCommandStatus.FAILED.value,
        ):
            raise ra_exc.ValidationErrorException()
        now = datetime.datetime.now(datetime.timezone.utc)
        safe_error = kwargs.get("safe_error")
        if status == models.ProviderCommandStatus.DELIVERED.value:
            safe_error = None
        resource.update_dm(
            values={
                "status": status,
                "safe_error": safe_error,
                "completed_at": now,
            },
        )
        resource.update()
        entity = self._get_entity(resource)
        entity_values = {
            "delivery_status": status,
            "delivery_error": safe_error,
            "delivery_updated_at": now,
        }
        if kwargs.get("provider_external_id") is not None:
            entity_values["provider_external_id"] = kwargs["provider_external_id"]
        entity.update_dm(values=entity_values)
        entity.update()
        event_kind = workspace_events.CALENDAR_EVENT_UPDATED_EVENT
        if isinstance(entity, groupware_models.Calendar):
            event_kind = workspace_events.CALENDAR_UPDATED_EVENT
        workspace_events.create_groupware_event(entity, event_kind)
        return resource


class ProviderMessengerCommandController(ProviderMessengerBaseController):
    __default_sort__ = {"created_at": "asc"}
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=ServiceMessengerProviderCommand,
        hidden_fields=["project_id", "user_uuid"],
        convert_underscore=False,
        process_filters=True,
    )

    def filter(self, parent_resource, filters, order_by=None):
        filters = filters.copy()
        filters.update(
            {
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.MESSENGER.value),
            },
        )
        return ra_controllers.BaseResourceControllerPaginated.filter(
            self,
            filters=filters,
            order_by=order_by,
        )

    def get(self, uuid, parent_resource=None):
        return models.ProviderCommand.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(uuid),
                "provider_uuid": dm_filters.EQ(parent_resource.uuid),
                "domain": dm_filters.EQ(models.ProviderDomain.MESSENGER.value),
            },
        )

    @staticmethod
    def _get_entity(command):
        entity_type, entity_uuid = urns.parse(command.entity_urn)
        model_by_type = {
            urns.MESSENGER_STREAM: messenger_models.WorkspaceStream,
            urns.MESSENGER_TOPIC: messenger_models.WorkspaceStreamTopic,
            urns.MESSENGER_MESSAGE: messenger_models.WorkspaceMessage,
            urns.MESSENGER_REACTION: messenger_models.WorkspaceMessageReactions,
        }
        try:
            model_class = model_by_type[entity_type]
        except KeyError:
            raise ra_exc.ValidationErrorException()
        return model_class.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(entity_uuid),
                "provider_uuid": dm_filters.EQ(command.provider_uuid),
            },
        )

    @staticmethod
    def _create_entity_events(entity):
        if isinstance(entity, messenger_models.WorkspaceStream):
            ProviderMessengerStreamController._events(entity, created=False)
        elif isinstance(entity, messenger_models.WorkspaceStreamTopic):
            ProviderMessengerTopicController._events(entity, created=False)
        elif isinstance(entity, messenger_models.WorkspaceMessage):
            messenger_helpers._create_workspace_message_updated_events(
                entity.project_id,
                entity.uuid,
            )
        elif isinstance(entity, messenger_models.WorkspaceMessageReactions):
            message = messenger_models.WorkspaceMessage.objects.get_one(
                filters={"uuid": dm_filters.EQ(entity.message_uuid)},
            )
            workspace_events.create_message_reaction_updated_event(
                entity,
                message,
                message,
                entity.emoji_name,
            )

    @staticmethod
    def _create_deleted_result_event(command, status, safe_error, now):
        entity_type, _entity_uuid = urns.parse(command.entity_urn)
        event_by_type = {
            urns.MESSENGER_STREAM: workspace_events.STREAM_DELETED_EVENT,
            urns.MESSENGER_TOPIC: workspace_events.TOPIC_DELETED_EVENT,
            urns.MESSENGER_MESSAGE: workspace_events.MESSAGE_DELETED_EVENT,
            urns.MESSENGER_REACTION: (workspace_events.MESSAGE_REACTION_DELETED_EVENT),
        }
        try:
            kind = event_by_type[entity_type]
        except KeyError:
            raise ra_exc.ValidationErrorException()
        payload = command.payload.copy()
        payload["delivery"] = {
            "status": status,
            "safe_error": safe_error,
            "updated_at": workspace_events._event_payload_value(
                "updated_at",
                now,
            ),
        }
        workspace_events._create_workspace_event(
            project_id=command.project_id,
            user_uuid=command.user_uuid,
            kind=kind,
            payload=payload,
        )

    @ra_actions.post
    def result(self, resource, *args, **kwargs):
        status = kwargs["status"]
        if status not in (
            models.ProviderCommandStatus.DELIVERED.value,
            models.ProviderCommandStatus.FAILED.value,
        ):
            raise ra_exc.ValidationErrorException()
        now = datetime.datetime.now(datetime.timezone.utc)
        safe_error = kwargs.get("safe_error")
        if status == models.ProviderCommandStatus.DELIVERED.value:
            safe_error = None
        resource.update_dm(
            values={
                "status": status,
                "safe_error": safe_error,
                "completed_at": now,
            },
        )
        resource.update()
        entity = self._get_entity(resource)
        if entity is None:
            self._create_deleted_result_event(resource, status, safe_error, now)
            return resource
        values = {
            "delivery_status": status,
            "delivery_error": safe_error,
            "delivery_updated_at": now,
        }
        if kwargs.get("provider_external_id") is not None:
            values["provider_external_id"] = kwargs["provider_external_id"]
        entity.update_dm(values=values)
        entity.update()
        self._create_entity_events(entity)
        return resource


class ProviderApiEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/"


class ProviderMailEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/providers/{provider_uuid}/mail/"


class ProviderCalendarEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/providers/{provider_uuid}/calendar/"


class ProviderMessengerEndpointController(ra_controllers.RoutesListController):
    __TARGET_PATH__ = "/v1/providers/{provider_uuid}/messenger/"
