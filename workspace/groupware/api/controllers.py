# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import hashlib
import urllib.parse
import uuid as sys_uuid

import orjson
import webob
from restalchemy.api import actions as ra_actions
from restalchemy.api import packers as ra_packers
from restalchemy.api import resources as ra_resources
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants as oa_c
from restalchemy.openapi import utils as oa_utils

from workspace.groupware.dm import models
from workspace.workspace_api import events as workspace_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import controllers as workspace_controllers
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models as messenger_models
from workspace.provider_api import commands as provider_commands


class GroupwareJSONPacker(ra_packers.JSONPacker):
    json_list_fields = frozenset()

    def pack_resource(self, obj):
        result = super().pack_resource(obj)
        for name in self.json_list_fields:
            value = result.get(name)
            if isinstance(value, str):
                result[name] = orjson.loads(value)
        return workspace_events.add_provider_delivery_payload(result, obj)


class MailMessageJSONPacker(GroupwareJSONPacker):
    json_list_fields = frozenset(
        {"to_addresses", "cc_addresses", "bcc_addresses"},
    )


class CalendarEventJSONPacker(GroupwareJSONPacker):
    json_list_fields = frozenset({"attendees", "alarms"})


class ExternalAccountOwnershipMixin:
    def _get_owned_external_account(self, account_uuid, account_type):
        if account_uuid is None:
            return None
        return messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
                "account_type": dm_filters.EQ(account_type),
            },
        )


class MailFolderController(
    ExternalAccountOwnershipMixin,
    workspace_controllers.WorkspaceBaseResourceControllerPaginated,
):
    __packer__ = GroupwareJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailFolder,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "provider_uuid",
            "provider_external_id",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "sync_cursor",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def get_autofilters(self):
        filters = super().get_autofilters()
        filters["deleted"] = dm_filters.EQ(False)
        return filters

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        account_uuid = values.get("external_user_uuid")
        account = self._get_owned_external_account(
            account_uuid,
            messenger_models.ExternalAccountType.MAIL.value,
        )
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        values["source_name"] = models.MailSource.NATIVE.value
        values["source"] = {}
        values["sync_cursor"] = None
        values["sync_status"] = (
            models.SyncStatus.PENDING.value
            if account_uuid is not None
            else models.SyncStatus.SYNCED.value
        )
        values["sync_error"] = None
        values["deleted"] = False
        values["provider_uuid"] = None if account is None else account.provider_uuid
        folder = models.MailFolder(**values)
        folder.insert()
        provider_commands.create_mail_command(folder, "folder.create")
        workspace_events.create_groupware_event(
            folder,
            workspace_events.MAIL_FOLDER_CREATED_EVENT,
        )
        return folder

    def update(self, uuid, **kwargs):
        folder = self.get(uuid=uuid)
        values = kwargs.copy()
        for name in (
            "source_name",
            "source",
            "sync_cursor",
            "sync_status",
            "sync_error",
            "deleted",
        ):
            values.pop(name, None)
        if "external_user_uuid" in values:
            self._get_owned_external_account(
                values["external_user_uuid"],
                messenger_models.ExternalAccountType.MAIL.value,
            )
        if (
            folder.external_user_uuid is not None
            or values.get("external_user_uuid") is not None
        ):
            values["sync_status"] = models.SyncStatus.PENDING.value
            values["sync_error"] = None
        folder.update_dm(values=values)
        folder.update()
        provider_commands.create_mail_command(folder, "folder.update")
        workspace_events.create_groupware_event(
            folder,
            workspace_events.MAIL_FOLDER_UPDATED_EVENT,
        )
        return folder

    def delete(self, uuid):
        folder = self.get(uuid=uuid)
        project_id = folder.project_id
        user_uuid = folder.user_uuid
        folder_uuid = folder.uuid
        if folder.external_user_uuid is None:
            folder.delete()
        else:
            folder.update_dm(
                values={
                    "deleted": True,
                    "sync_status": models.SyncStatus.PENDING.value,
                    "sync_error": None,
                },
            )
            folder.update()
        provider_commands.create_mail_command(folder, "folder.delete")
        workspace_events.create_groupware_deleted_event(
            project_id,
            user_uuid,
            folder_uuid,
            workspace_events.MAIL_FOLDER_DELETED_EVENT,
        )


class MailMessageController(
    workspace_controllers.WorkspaceBaseResourceControllerPaginated,
):
    __packer__ = MailMessageJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailMessage,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "provider_uuid",
            "provider_external_id",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )
    __default_sort__ = {"sent_at": "desc"}

    def get_autofilters(self):
        filters = super().get_autofilters()
        filters["deleted"] = dm_filters.EQ(False)
        return filters

    def _get_owned_folder(self, folder_uuid):
        return models.MailFolder.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(folder_uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
            },
        )

    def _account_sender(self, account_uuid):
        if account_uuid is None:
            return None
        account = messenger_models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(account_uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
                "account_type": dm_filters.EQ(
                    messenger_models.ExternalAccountType.MAIL.value,
                ),
            },
        )
        settings = account.account_settings
        credentials = settings.get("credentials") or {}
        return settings.get("email") or credentials.get("username")

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        folder = self._get_owned_folder(values["folder_uuid"])
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        values["source_name"] = models.MailSource.NATIVE.value
        values["source"] = {}
        values["external_uid"] = None
        values["external_user_uuid"] = folder.external_user_uuid
        sender = self._account_sender(folder.external_user_uuid)
        if sender is not None:
            values["from_address"] = sender
        values["sync_status"] = (
            models.SyncStatus.PENDING.value
            if folder.external_user_uuid is not None
            else models.SyncStatus.SYNCED.value
        )
        values["sync_error"] = None
        message = models.MailMessage(**values)
        message.insert()
        provider_commands.create_mail_command(message, "message.create")
        workspace_events.create_groupware_event(
            message,
            workspace_events.MAIL_MESSAGE_CREATED_EVENT,
        )
        return message

    def update(self, uuid, **kwargs):
        message = self.get(uuid=uuid)
        values = kwargs.copy()
        values.pop("external_uid", None)
        values.pop("source_name", None)
        values.pop("source", None)
        values.pop("sync_status", None)
        values.pop("sync_error", None)
        values.pop("external_user_uuid", None)
        if "folder_uuid" in values:
            folder = self._get_owned_folder(values["folder_uuid"])
            values["external_user_uuid"] = folder.external_user_uuid
        if message.external_user_uuid is not None:
            values["sync_status"] = models.SyncStatus.PENDING.value
            values["sync_error"] = None
        message.update_dm(values=values)
        message.update()
        provider_commands.create_mail_command(message, "message.update")
        workspace_events.create_groupware_event(
            message,
            workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
        )
        return message

    def delete(self, uuid):
        message = self.get(uuid=uuid)
        if message.external_user_uuid is None:
            message.delete()
        else:
            message.update_dm(
                values={
                    "deleted": True,
                    "sync_status": models.SyncStatus.PENDING.value,
                    "sync_error": None,
                },
            )
            message.update()
        provider_commands.create_mail_command(message, "message.delete")
        workspace_events.create_groupware_deleted_event(
            message.project_id,
            message.user_uuid,
            message.uuid,
            workspace_events.MAIL_MESSAGE_DELETED_EVENT,
        )
        return None

    @ra_actions.post
    def send(self, resource, *args, **kwargs):
        resource.update_dm(
            values={
                "draft": False,
                "sync_status": models.SyncStatus.PENDING.value,
                "sync_error": None,
            },
        )
        resource.update()
        provider_commands.create_mail_command(resource, "message.send")
        workspace_events.create_groupware_event(
            resource,
            workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
        )
        return resource

    @ra_actions.post
    def move(self, resource, *args, **kwargs):
        folder = self._get_owned_folder(kwargs["folder_uuid"])
        resource.update_dm(
            values={
                "folder_uuid": folder.uuid,
                "external_user_uuid": folder.external_user_uuid,
                "sync_status": (
                    models.SyncStatus.PENDING.value
                    if folder.external_user_uuid is not None
                    else models.SyncStatus.SYNCED.value
                ),
                "sync_error": None,
            },
        )
        resource.update()
        provider_commands.create_mail_command(resource, "message.move")
        workspace_events.create_groupware_event(
            resource,
            workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
        )
        return resource


class MailAttachmentController(
    workspace_controllers.WorkspaceBaseResourceControllerPaginated,
):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.MailAttachment,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "storage_id",
            "storage_object_id",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    @staticmethod
    def _get_content_disposition(attachment):
        quoted_name = attachment.name.replace("\\", "\\\\").replace('"', '\\"')
        encoded_name = urllib.parse.quote(attachment.name)
        return (
            f"attachment; filename=\"{quoted_name}\"; filename*=UTF-8''{encoded_name}"
        )

    def process_result(self, result, *args, **kwargs):
        if isinstance(result, webob.Response):
            return result
        return super().process_result(result, *args, **kwargs)

    def create(self, **kwargs):
        parts = kwargs["parts"]
        file_part = parts["file"]
        message_uuid_part = parts["message_uuid"]
        message_uuid = sys_uuid.UUID(
            getattr(message_uuid_part, "value", message_uuid_part),
        )
        message = self._get_owned_message(message_uuid)
        file_part.file.seek(0)
        data = file_part.file.read()
        attachment_uuid = sys_uuid.uuid4()
        storage = file_storage.save_workspace_file(attachment_uuid, data)
        workspace_blob = None
        try:
            digest = hashlib.sha256(data).hexdigest()
            if getattr(message, "provider_uuid", None) is not None:
                workspace_blob = messenger_models.WorkspaceFile(
                    uuid=attachment_uuid,
                    project_id=self._get_project_id(),
                    user_uuid=self._get_user_uuid(),
                    stream_uuid=None,
                    provider_uuid=message.provider_uuid,
                    external_account_uuid=message.external_user_uuid,
                    name=file_part.filename,
                    description="",
                    content_type=file_part.type or "application/octet-stream",
                    size_bytes=len(data),
                    hash=digest,
                    storage_type=storage.storage_type,
                    storage_id=storage.storage_id,
                    storage_object_id=storage.storage_object_id,
                )
                workspace_blob.insert()
                messenger_dm_helpers.get_or_create_workspace_file_access(
                    project_id=self._get_project_id(),
                    file_uuid=workspace_blob.uuid,
                    user_uuid=self._get_user_uuid(),
                )
            attachment = models.MailAttachment(
                uuid=attachment_uuid,
                project_id=self._get_project_id(),
                user_uuid=self._get_user_uuid(),
                message_uuid=message_uuid,
                name=file_part.filename,
                content_type=file_part.type or "application/octet-stream",
                size_bytes=len(data),
                hash=digest,
                storage_type=storage.storage_type,
                storage_id=storage.storage_id,
                storage_object_id=storage.storage_object_id,
            )
            attachment.insert()
            message = self._get_owned_message(message_uuid)
            provider_commands.create_mail_command(message, "message.update")
            workspace_events.create_groupware_event(
                message,
                workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
            )
            return attachment
        except Exception:
            if workspace_blob is not None:
                workspace_blob.delete()
            file_storage.delete_workspace_file(
                attachment_uuid,
                storage_type=storage.storage_type,
                storage_object_id=storage.storage_object_id,
            )
            raise

    def _get_owned_message(self, message_uuid):
        return models.MailMessage.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(message_uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
            },
        )

    def delete(self, uuid):
        attachment = self.get(uuid=uuid)
        message = self._get_owned_message(attachment.message_uuid)
        attachment.delete()
        workspace_blob = messenger_models.WorkspaceFile.objects.get_one_or_none(
            filters={"uuid": dm_filters.EQ(attachment.uuid)},
        )
        if workspace_blob is not None:
            workspace_blob.delete()
        provider_commands.create_mail_command(message, "message.update")
        file_storage.delete_workspace_file(
            attachment.uuid,
            storage_type=attachment.storage_type,
            storage_object_id=attachment.storage_object_id,
        )
        workspace_events.create_groupware_event(
            message,
            workspace_events.MAIL_MESSAGE_UPDATED_EVENT,
        )
        return None

    @ra_actions.get
    def download(self, resource, *args, **kwargs):
        data = file_storage.read_workspace_file(
            resource.uuid,
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


MailAttachmentController.create.openapi_schema = oa_utils.Schema(
    summary="Upload mail attachment",
    parameters=(),
    responses=oa_c.build_openapi_create_response("MailAttachment_Create"),
    request_body={
        "description": "Upload a file for a locally stored mail message",
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["message_uuid", "file"],
                    "properties": {
                        "message_uuid": {"format": "uuid", "type": "string"},
                        "file": {"format": "binary", "type": "string"},
                    },
                },
            },
        },
    },
)


class CalendarController(
    ExternalAccountOwnershipMixin,
    workspace_controllers.WorkspaceBaseResourceControllerPaginated,
):
    __packer__ = GroupwareJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.Calendar,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "provider_uuid",
            "provider_external_id",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "ctag",
            "sync_token",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )

    def get_autofilters(self):
        filters = super().get_autofilters()
        filters["deleted"] = dm_filters.EQ(False)
        return filters

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        account_uuid = values.get("external_user_uuid")
        account = self._get_owned_external_account(
            account_uuid,
            messenger_models.ExternalAccountType.CALENDAR.value,
        )
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        values["source_name"] = models.CalendarSource.NATIVE.value
        values["source"] = {}
        values["sync_token"] = None
        values["sync_status"] = (
            models.SyncStatus.PENDING.value
            if account_uuid is not None
            else models.SyncStatus.SYNCED.value
        )
        values["sync_error"] = None
        values["deleted"] = False
        values["provider_uuid"] = None if account is None else account.provider_uuid
        calendar = models.Calendar(**values)
        calendar.insert()
        provider_commands.create_calendar_command(calendar, "calendar.create")
        workspace_events.create_groupware_event(
            calendar,
            workspace_events.CALENDAR_CREATED_EVENT,
        )
        return calendar

    def update(self, uuid, **kwargs):
        calendar = self.get(uuid=uuid)
        values = kwargs.copy()
        for name in (
            "ctag",
            "source_name",
            "source",
            "sync_token",
            "sync_status",
            "sync_error",
            "deleted",
        ):
            values.pop(name, None)
        if "external_user_uuid" in values:
            self._get_owned_external_account(
                values["external_user_uuid"],
                messenger_models.ExternalAccountType.CALENDAR.value,
            )
        if (
            calendar.external_user_uuid is not None
            or values.get("external_user_uuid") is not None
        ):
            values["sync_status"] = models.SyncStatus.PENDING.value
            values["sync_error"] = None
        calendar.update_dm(values=values)
        calendar.update()
        provider_commands.create_calendar_command(calendar, "calendar.update")
        workspace_events.create_groupware_event(
            calendar,
            workspace_events.CALENDAR_UPDATED_EVENT,
        )
        return calendar

    def delete(self, uuid):
        calendar = self.get(uuid=uuid)
        project_id = calendar.project_id
        user_uuid = calendar.user_uuid
        calendar_uuid = calendar.uuid
        if calendar.external_user_uuid is None:
            calendar.delete()
        else:
            calendar.update_dm(
                values={
                    "deleted": True,
                    "sync_status": models.SyncStatus.PENDING.value,
                    "sync_error": None,
                },
            )
            calendar.update()
        provider_commands.create_calendar_command(calendar, "calendar.delete")
        if calendar.external_user_uuid is None:
            workspace_events.create_groupware_deleted_event(
                project_id,
                user_uuid,
                calendar_uuid,
                workspace_events.CALENDAR_DELETED_EVENT,
            )
        else:
            workspace_events.create_groupware_event(
                calendar,
                workspace_events.CALENDAR_DELETED_EVENT,
            )


class CalendarEventController(
    workspace_controllers.WorkspaceBaseResourceControllerPaginated,
):
    __packer__ = CalendarEventJSONPacker
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=models.CalendarEvent,
        hidden_fields=[
            "project_id",
            "user_uuid",
            "provider_uuid",
            "provider_external_id",
            "delivery_status",
            "delivery_error",
            "delivery_updated_at",
            "ics",
            "etag",
            "sync_status",
            "sync_error",
            "source_name",
            "source",
        ],
        convert_underscore=False,
        process_filters=True,
    )
    __default_sort__ = {"starts_at": "asc"}

    def get_autofilters(self):
        filters = super().get_autofilters()
        filters["deleted"] = dm_filters.EQ(False)
        return filters

    def _get_owned_calendar(self, calendar_uuid):
        return models.Calendar.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(calendar_uuid),
                "project_id": dm_filters.EQ(self._get_project_id()),
                "user_uuid": dm_filters.EQ(self._get_user_uuid()),
            },
        )

    def create(self, **kwargs):
        values = self._apply_autovalues(kwargs)
        calendar = self._get_owned_calendar(values["calendar_uuid"])
        values["uuid"] = values.get("uuid") or sys_uuid.uuid4()
        values["uid"] = values.get("uid") or str(values["uuid"])
        values["source_name"] = models.CalendarSource.NATIVE.value
        values["source"] = {}
        values["external_user_uuid"] = calendar.external_user_uuid
        values["sync_status"] = (
            models.SyncStatus.PENDING.value
            if calendar.external_user_uuid is not None
            else models.SyncStatus.SYNCED.value
        )
        values["sync_error"] = None
        event = models.CalendarEvent(**values)
        event.insert()
        provider_commands.create_calendar_command(event, "event.create")
        workspace_events.create_groupware_event(
            event,
            workspace_events.CALENDAR_EVENT_CREATED_EVENT,
        )
        return event

    def update(self, uuid, **kwargs):
        event = self.get(uuid=uuid)
        values = kwargs.copy()
        for name in (
            "ics",
            "source_name",
            "source",
            "sync_status",
            "sync_error",
            "etag",
            "external_user_uuid",
        ):
            values.pop(name, None)
        if "calendar_uuid" in values:
            calendar = self._get_owned_calendar(values["calendar_uuid"])
            values["external_user_uuid"] = calendar.external_user_uuid
        if event.external_user_uuid is not None:
            values["sync_status"] = models.SyncStatus.PENDING.value
            values["sync_error"] = None
        event.update_dm(values=values)
        event.update()
        provider_commands.create_calendar_command(event, "event.update")
        workspace_events.create_groupware_event(
            event,
            workspace_events.CALENDAR_EVENT_UPDATED_EVENT,
        )
        return event

    def delete(self, uuid):
        event = self.get(uuid=uuid)
        if event.external_user_uuid is None:
            event.delete()
        else:
            event.update_dm(
                values={
                    "deleted": True,
                    "sync_status": models.SyncStatus.PENDING.value,
                    "sync_error": None,
                },
            )
            event.update()
        provider_commands.create_calendar_command(event, "event.delete")
        if event.external_user_uuid is None:
            workspace_events.create_groupware_deleted_event(
                event.project_id,
                event.user_uuid,
                event.uuid,
                workspace_events.CALENDAR_EVENT_DELETED_EVENT,
            )
        else:
            workspace_events.create_groupware_event(
                event,
                workspace_events.CALENDAR_EVENT_DELETED_EVENT,
            )
        return None

    @ra_actions.post
    def move(self, resource, *args, **kwargs):
        calendar = self._get_owned_calendar(kwargs["calendar_uuid"])
        resource.update_dm(
            values={
                "calendar_uuid": calendar.uuid,
                "external_user_uuid": calendar.external_user_uuid,
                "sync_status": (
                    models.SyncStatus.PENDING.value
                    if calendar.external_user_uuid is not None
                    else models.SyncStatus.SYNCED.value
                ),
                "sync_error": None,
            },
        )
        resource.update()
        provider_commands.create_calendar_command(resource, "event.move")
        workspace_events.create_groupware_event(
            resource,
            workspace_events.CALENDAR_EVENT_UPDATED_EVENT,
        )
        return resource
