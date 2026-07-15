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


class JsonList(types.List):
    def to_simple_type(self, value):
        return orjson.dumps(value).decode("utf-8")


class ProviderKind(str, enum.Enum):
    ZULIP = "zulip"
    MAIL = "mail"
    CALENDAR = "calendar"


class ProviderDomain(str, enum.Enum):
    MESSENGER = "messenger"
    MAIL = "mail"
    CALENDAR = "calendar"


class ProviderCommandStatus(str, enum.Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WorkspaceProvider(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_providers"

    name = properties.property(
        types.String(min_length=1, max_length=256),
        required=True,
    )
    supported_kinds = properties.property(JsonList(), default=list)
    version = properties.property(
        types.AllowNone(types.String(max_length=128)),
        default=None,
    )
    enabled = properties.property(types.Boolean(), default=True)
    registered_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    last_seen_at = properties.property(
        types.UTCDateTimeZ(),
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    def validate(self):
        super().validate()
        allowed_kinds = {kind.value for kind in ProviderKind}
        if not self.supported_kinds or not set(self.supported_kinds) <= allowed_kinds:
            raise ValueError("Unsupported provider kind")


class ProviderCommand(
    models.ModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_provider_commands"

    user_uuid = properties.property(types.UUID(), required=True)
    provider_uuid = properties.property(types.UUID(), required=True)
    external_account_uuid = properties.property(types.UUID(), required=True)
    domain = properties.property(
        types.Enum([domain.value for domain in ProviderDomain]),
        required=True,
    )
    operation = properties.property(
        types.String(min_length=1, max_length=64),
        required=True,
    )
    entity_uuid = properties.property(types.UUID(), required=True)
    entity_urn = properties.property(
        types.String(min_length=1, max_length=2300),
        required=True,
    )
    payload = properties.property(types.Dict(), required=True)
    status = properties.property(
        types.Enum([status.value for status in ProviderCommandStatus]),
        default=ProviderCommandStatus.PENDING.value,
    )
    safe_error = properties.property(
        types.AllowNone(types.String()),
        default=None,
    )
    completed_at = properties.property(
        types.AllowNone(types.UTCDateTimeZ()),
        default=None,
    )
