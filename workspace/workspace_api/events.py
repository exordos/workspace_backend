# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.common import urns
from workspace.groupware.dm import models as groupware_models
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import event_payloads
from workspace.provider_api import payloads as provider_payloads


MAIL_FOLDER_CREATED_EVENT = "mail.folder.created"
MAIL_FOLDER_UPDATED_EVENT = "mail.folder.updated"
MAIL_FOLDER_DELETED_EVENT = "mail.folder.deleted"
MAIL_MESSAGE_CREATED_EVENT = "mail.message.created"
MAIL_MESSAGE_UPDATED_EVENT = "mail.message.updated"
MAIL_MESSAGE_DELETED_EVENT = "mail.message.deleted"
CALENDAR_CREATED_EVENT = "calendar.calendar.created"
CALENDAR_UPDATED_EVENT = "calendar.calendar.updated"
CALENDAR_DELETED_EVENT = "calendar.calendar.deleted"
CALENDAR_EVENT_CREATED_EVENT = "calendar.event.created"
CALENDAR_EVENT_UPDATED_EVENT = "calendar.event.updated"
CALENDAR_EVENT_DELETED_EVENT = "calendar.event.deleted"
EXTERNAL_ACCOUNT_UPDATED_EVENT = "external_account.updated"
MESSAGE_DELETED_EVENT = messenger_events.MESSAGE_DELETED_EVENT
MESSAGE_REACTION_DELETED_EVENT = messenger_events.MESSAGE_REACTION_DELETED_EVENT
STREAM_DELETED_EVENT = messenger_events.STREAM_DELETED_EVENT
TOPIC_DELETED_EVENT = messenger_events.TOPIC_DELETED_EVENT

GROUPWARE_EVENT_METADATA = {
    MAIL_FOLDER_CREATED_EVENT: ("mail_folder", messenger_events.CREATED_ACTION),
    MAIL_FOLDER_UPDATED_EVENT: ("mail_folder", messenger_events.UPDATED_ACTION),
    MAIL_FOLDER_DELETED_EVENT: ("mail_folder", messenger_events.DELETED_ACTION),
    MAIL_MESSAGE_CREATED_EVENT: ("mail_message", messenger_events.CREATED_ACTION),
    MAIL_MESSAGE_UPDATED_EVENT: ("mail_message", messenger_events.UPDATED_ACTION),
    MAIL_MESSAGE_DELETED_EVENT: ("mail_message", messenger_events.DELETED_ACTION),
    CALENDAR_CREATED_EVENT: ("calendar", messenger_events.CREATED_ACTION),
    CALENDAR_UPDATED_EVENT: ("calendar", messenger_events.UPDATED_ACTION),
    CALENDAR_DELETED_EVENT: ("calendar", messenger_events.DELETED_ACTION),
    CALENDAR_EVENT_CREATED_EVENT: (
        "calendar_event",
        messenger_events.CREATED_ACTION,
    ),
    CALENDAR_EVENT_UPDATED_EVENT: (
        "calendar_event",
        messenger_events.UPDATED_ACTION,
    ),
    CALENDAR_EVENT_DELETED_EVENT: (
        "calendar_event",
        messenger_events.DELETED_ACTION,
    ),
    EXTERNAL_ACCOUNT_UPDATED_EVENT: (
        "external_account",
        messenger_events.UPDATED_ACTION,
    ),
}
messenger_events.EVENT_METADATA.update(GROUPWARE_EVENT_METADATA)

_create_workspace_event = messenger_events._create_workspace_event
_event_payload_value = messenger_events._event_payload_value
create_message_reaction_updated_event = (
    messenger_events.create_message_reaction_updated_event
)
create_stream_event = messenger_events.create_stream_event
create_stream_updated_event = messenger_events.create_stream_updated_event
create_topic_event = messenger_events.create_topic_event
create_topic_updated_event = messenger_events.create_topic_updated_event
create_user_updated_events = messenger_events.create_user_updated_events


def _model_to_event_payload_value(value):
    return {
        name: _event_payload_value(name, prop.value)
        for name, prop in value.properties.items()
    }


def _event_payload_value(name, value):
    if value is None:
        return None
    if name in ("created_at", "updated_at", "last_ping_at"):
        value = event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.from_simple_type(value)
        return event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return (
            value.astimezone(datetime.timezone.utc)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, sys_uuid.UUID):
        return str(value).lower()
    if name == "uuid" or name.endswith("uuid") or name == "project_id":
        return str(value).lower()
    if hasattr(value, "properties") and hasattr(value.properties, "items"):
        return _model_to_event_payload_value(value)
    if isinstance(value, list):
        return [_event_payload_value(name, item) for item in value]
    if isinstance(value, dict):
        return {
            item_name: _event_payload_value(item_name, item_value)
            for item_name, item_value in value.items()
        }
    return value


def add_provider_delivery_payload(payload, resource, session=None):
    return provider_payloads.add_provider_delivery_payload(
        payload,
        resource,
        session=session,
    )


def groupware_resource_payload(resource, session=None):
    payload = _model_to_event_payload_value(resource)
    for name in (
        "provider_uuid",
        "provider_external_id",
        "delivery_status",
        "delivery_error",
        "delivery_updated_at",
        "sync_cursor",
        "sync_token",
        "sync_status",
        "sync_error",
        "source_name",
        "source",
        "ctag",
        "etag",
        "ics",
    ):
        payload.pop(name, None)
    if isinstance(resource, groupware_models.MailMessage):
        attachments = groupware_models.MailAttachment.objects.get_all(
            filters={
                "message_uuid": dm_filters.EQ(resource.uuid),
                "project_id": dm_filters.EQ(resource.project_id),
                "user_uuid": dm_filters.EQ(resource.user_uuid),
            },
            order_by={"created_at": "asc"},
            session=session,
        )
        payload["attachments"] = [
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
    return add_provider_delivery_payload(payload, resource, session=session)


def create_groupware_event(resource, kind, session=None):
    return messenger_events._create_workspace_event(
        project_id=resource.project_id,
        user_uuid=resource.user_uuid,
        kind=kind,
        payload=groupware_resource_payload(resource, session=session),
        session=session,
    )


def create_groupware_deleted_event(
    project_id, user_uuid, resource_uuid, kind, session=None
):
    return messenger_events._create_workspace_event(
        project_id=project_id,
        user_uuid=user_uuid,
        kind=kind,
        payload={"uuid": str(resource_uuid)},
        session=session,
    )


def create_external_account_updated_event(account, session=None):
    payload = _model_to_event_payload_value(account)
    settings = payload["account_settings"]
    if isinstance(settings, dict):
        settings["credentials"] = None
    return messenger_events._create_workspace_event(
        project_id=account.project_id,
        user_uuid=account.user_uuid,
        kind=EXTERNAL_ACCOUNT_UPDATED_EVENT,
        payload=payload,
        session=session,
    )
