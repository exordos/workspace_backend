# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import enum
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class MailSource(str, enum.Enum):
    NATIVE = "native"
    PROVIDER = "provider"


class MailDeliveryStatus(str, enum.Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENT = "sent"
    RECEIVED = "received"
    FAILED = "failed"


class MailFolder(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_mail_folders"

    owner_user_uuid = properties.property(
        types.UUID(),
        required=True,
        read_only=True,
    )
    external_account_uuid = properties.property(
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
    unread_count = properties.property(
        types.Integer(min_value=0),
        default=0,
        read_only=True,
    )
    total_count = properties.property(
        types.Integer(min_value=0),
        default=0,
        read_only=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in MailSource]),
        default=MailSource.NATIVE.value,
        read_only=True,
    )
    provider_identity = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=2048)),
        default=None,
        read_only=True,
    )
    deleted = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )


class MailMessage(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableWithJSONFieldsMixin,
):
    __tablename__ = "m_mail_messages"
    __jsonfields__ = {
        "attachment_urns",
        "bcc_addresses",
        "cc_addresses",
        "references",
        "to_addresses",
    }

    owner_user_uuid = properties.property(
        types.UUID(),
        required=True,
        read_only=True,
    )
    folder_uuid = properties.property(types.UUID(), required=True)
    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
        read_only=True,
    )
    from_address = properties.property(types.String(max_length=2048), default="")
    to_addresses = properties.property(
        types.TypedList(types.String(min_length=1, max_length=2048)),
        default=list,
    )
    cc_addresses = properties.property(
        types.TypedList(types.String(min_length=1, max_length=2048)),
        default=list,
    )
    bcc_addresses = properties.property(
        types.TypedList(types.String(min_length=1, max_length=2048)),
        default=list,
    )
    reply_to = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    subject = properties.property(types.String(max_length=2048), default="")
    snippet = properties.property(types.String(max_length=4096), default="")
    body_html = properties.property(types.AllowNone(types.String()), default=None)
    body_text = properties.property(types.AllowNone(types.String()), default=None)
    attachment_urns = properties.property(
        types.TypedList(types.String(min_length=1, max_length=4096)),
        default=list,
    )
    message_id = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
        read_only=True,
    )
    in_reply_to = properties.property(
        types.AllowNone(types.String(max_length=2048)),
        default=None,
    )
    references = properties.property(
        types.TypedList(types.String(min_length=1, max_length=2048)),
        default=list,
    )
    sent_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
        read_only=True,
    )
    received_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
        read_only=True,
    )
    seen = properties.property(types.Boolean(), default=False)
    flagged = properties.property(types.Boolean(), default=False)
    draft = properties.property(types.Boolean(), default=True, read_only=True)
    delivery_status = properties.property(
        types.Enum([status.value for status in MailDeliveryStatus]),
        default=MailDeliveryStatus.DRAFT.value,
        read_only=True,
    )
    delivery_error = properties.property(
        types.AllowNone(types.String()),
        default=None,
        read_only=True,
    )
    source_name = properties.property(
        types.Enum([source.value for source in MailSource]),
        default=MailSource.NATIVE.value,
        read_only=True,
    )
    provider_identity = properties.property(
        types.AllowNone(types.String(min_length=1, max_length=2048)),
        default=None,
        read_only=True,
    )
    deleted = properties.property(
        types.Boolean(),
        default=False,
        read_only=True,
    )
