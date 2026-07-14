# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import enum

import orjson
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm

from workspace.common import file_storage_opts


class JsonList(types.List):
    def to_simple_type(self, value):
        return orjson.dumps(value).decode("utf-8")


class IsoUtcDateTime(types.UTCDateTimeZ):
    def from_simple_type(self, value):
        try:
            return super().from_simple_type(value)
        except ValueError:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.astimezone(datetime.timezone.utc)


class SyncStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SYNCED = "synced"
    FAILED = "failed"


class MailSource(str, enum.Enum):
    NATIVE = "native"
    IMAP = "imap"


class CalendarSource(str, enum.Enum):
    NATIVE = "native"
    CALDAV = "caldav"


class MailFolder(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_mail_folders"

    user_uuid = properties.property(types.UUID(), required=True)
    external_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    path = properties.property(
        types.String(min_length=1, max_length=1024),
        required=True,
    )
    name = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    delimiter = properties.property(
        types.String(min_length=1, max_length=8),
        default="/",
    )
    special_use = properties.property(
        types.AllowNone(types.String(max_length=64)),
        default=None,
    )
    unread_count = properties.property(types.Integer(min_value=0), default=0)
    total_count = properties.property(types.Integer(min_value=0), default=0)
    source_name = properties.property(
        types.Enum([source.value for source in MailSource]),
        default=MailSource.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=dict)
    sync_cursor = properties.property(
        types.AllowNone(types.String(max_length=512)),
        default=None,
    )
    sync_status = properties.property(
        types.Enum([status.value for status in SyncStatus]),
        default=SyncStatus.SYNCED.value,
    )
    sync_error = properties.property(types.AllowNone(types.String()), default=None)
    deleted = properties.property(types.Boolean(), default=False)


class MailMessage(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_mail_messages"

    user_uuid = properties.property(types.UUID(), required=True)
    folder_uuid = properties.property(types.UUID(), required=True)
    external_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    external_uid = properties.property(
        types.AllowNone(types.Integer(min_value=1, max_value=2**63 - 1)),
        default=None,
    )
    from_address = properties.property(types.String(max_length=2048), default="")
    to_addresses = properties.property(JsonList(), default=list)
    cc_addresses = properties.property(JsonList(), default=list)
    bcc_addresses = properties.property(JsonList(), default=list)
    reply_to = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    subject = properties.property(types.String(max_length=2048), default="")
    snippet = properties.property(types.String(max_length=4096), default="")
    body_html = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    body_text = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    message_id = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    references = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    sent_at = properties.property(
        IsoUtcDateTime(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    seen = properties.property(types.Boolean(), default=False)
    flagged = properties.property(types.Boolean(), default=False)
    draft = properties.property(types.Boolean(), default=False)
    deleted = properties.property(types.Boolean(), default=False)
    source_name = properties.property(
        types.Enum([source.value for source in MailSource]),
        default=MailSource.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=dict)
    sync_status = properties.property(
        types.Enum([status.value for status in SyncStatus]),
        default=SyncStatus.SYNCED.value,
    )
    sync_error = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )


class MailAttachment(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_mail_attachments"

    name = properties.property(
        types.String(min_length=1, max_length=1024),
        required=True,
    )
    user_uuid = properties.property(types.UUID(), required=True)
    message_uuid = properties.property(types.UUID(), required=True)
    content_id = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    content_type = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    size_bytes = properties.property(
        types.Integer(min_value=0, max_value=2**63 - 1),
        required=True,
    )
    hash = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )
    storage_type = properties.property(
        types.Enum(file_storage_opts.STORAGE_TYPES),
        default=file_storage_opts.STORAGE_TYPE_FILE,
    )
    storage_id = properties.property(types.String(max_length=255), default="")
    storage_object_id = properties.property(
        types.String(min_length=1, max_length=255),
        required=True,
    )


class Calendar(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_calendars"

    name = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    user_uuid = properties.property(types.UUID(), required=True)
    external_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    color = properties.property(
        types.AllowNone(types.String(max_length=32)),
        default=None,
    )
    ctag = properties.property(
        types.AllowNone(types.String(max_length=512)),
        default=None,
    )
    source_name = properties.property(
        types.Enum([source.value for source in CalendarSource]),
        default=CalendarSource.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=dict)
    sync_token = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    sync_status = properties.property(
        types.Enum([status.value for status in SyncStatus]),
        default=SyncStatus.SYNCED.value,
    )
    sync_error = properties.property(types.AllowNone(types.String()), default=None)
    deleted = properties.property(types.Boolean(), default=False)


class CalendarEvent(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_calendar_events"

    user_uuid = properties.property(types.UUID(), required=True)
    calendar_uuid = properties.property(types.UUID(), required=True)
    external_user_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    uid = properties.property(
        types.String(min_length=1, max_length=1024),
        required=True,
    )
    summary = properties.property(types.String(max_length=2048), default="")
    description = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    location = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    starts_at = properties.property(IsoUtcDateTime(), required=True)
    ends_at = properties.property(IsoUtcDateTime(), required=True)
    all_day = properties.property(types.Boolean(), default=False)
    recurrence = properties.property(types.AllowNone(types.Dict()), default=None)
    attendees = properties.property(JsonList(), default=list)
    alarms = properties.property(JsonList(), default=list)
    recurrence_id = properties.property(
        types.AllowNone(types.String(max_length=1024)),
        default=None,
    )
    ics = properties.property(types.AllowNone(types.String()), default=None)
    etag = properties.property(
        types.AllowNone(types.String(max_length=512)),
        default=None,
    )
    source_name = properties.property(
        types.Enum([source.value for source in CalendarSource]),
        default=CalendarSource.NATIVE.value,
    )
    source = properties.property(types.Dict(), default=dict)
    sync_status = properties.property(
        types.Enum([status.value for status in SyncStatus]),
        default=SyncStatus.SYNCED.value,
    )
    sync_error = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    deleted = properties.property(types.Boolean(), default=False)
