# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import typing

from restalchemy.common import exceptions
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm


class EventTimestamp(types.UTCDateTimeZ):
    def from_simple_type(self, value: str | datetime.datetime) -> datetime.datetime:
        if isinstance(value, str) and ("T" not in value or not value.endswith("Z")):
            raise ValueError("Expected a UTC timestamp ending in Z")
        return super().from_simple_type(value)

    def to_simple_type(self, value: datetime.datetime) -> str:
        return self.dump_value(value)


class EventPayload(types_dynamic.AbstractKindModel):
    KIND = "event"

    title = properties.property(
        types.String(min_length=1, max_length=1024), required=True
    )
    description = properties.property(types.AllowNone(types.String()), default=None)
    location = properties.property(
        types.AllowNone(types.String(max_length=2048)), default=None
    )
    starts_at = properties.property(EventTimestamp(), required=True)
    ends_at = properties.property(EventTimestamp(), required=True)

    def validate(self) -> None:
        super().validate()
        if self.ends_at <= self.starts_at:
            raise exceptions.ValidationErrorException()


EVENT_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(EventPayload),
)


class OwnedResource(
    models.ModelWithUUID, models.ModelWithProject, models.ModelWithTimestamp
):
    owner_user_uuid = properties.property(types.UUID(), required=True, read_only=True)


class Calendar(OwnedResource, orm.SQLStorableMixin):
    __tablename__ = "m_workspace_calendars"

    external_account_uuid = properties.property(
        types.AllowNone(types.UUID()),
        default=None,
    )
    name = properties.property(
        types.String(min_length=1, max_length=256), required=True
    )
    primary = properties.property(types.Boolean(), default=False, read_only=True)
    read_only = properties.property(types.Boolean(), default=False, read_only=True)


class CalendarEvent(OwnedResource, orm.SQLStorableWithJSONFieldsMixin):
    __tablename__ = "m_workspace_calendar_events"
    __jsonfields__ = {"payload"}

    calendar_uuid = properties.property(types.UUID(), required=True)
    payload = properties.property(EVENT_PAYLOAD_TYPE, required=True)
    starts_at = properties.property(types.UTCDateTimeZ(), required=True, read_only=True)
    ends_at = properties.property(types.UTCDateTimeZ(), required=True, read_only=True)


def event_times(payload: EventPayload) -> dict[str, typing.Any]:
    return {"starts_at": payload.starts_at, "ends_at": payload.ends_at}
