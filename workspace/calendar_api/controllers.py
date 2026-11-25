# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import typing
import uuid as sys_uuid

from restalchemy.api import actions
from restalchemy.api import resources
from restalchemy.common import contexts
from restalchemy.common import exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.openapi import constants
from restalchemy.openapi import utils
from webob import multidict

from workspace.calendar_api import models
from workspace.common import account_capabilities
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.dm import external_models
from workspace.services import groupware_caldav_write


class CalendarResourceController(messenger_controllers.ExternalResourceController):
    event_prefix: str
    hidden_fields: tuple[str, ...] = ("owner_user_uuid",)

    def _owner_filters(self, filters: typing.Any) -> typing.Any:
        result = super()._owner_filters(filters)
        result["project_id"] = dm_filters.EQ(self._get_project_id())
        return result

    def get(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        return self.model.objects.get_one(
            filters=self._owner_filters({"uuid": dm_filters.EQ(uuid)}),
            session=contexts.Context().get_session(),
            locked=self.request.method in ("POST", "PUT", "DELETE"),
        )

    def _emit(
        self,
        resource: typing.Any,
        action: str,
        session: typing.Any,
    ) -> None:
        hidden_fields = self.hidden_fields
        if action == "deleted":
            hidden_fields = tuple(
                name
                for name in resource.properties
                if name not in {"uuid", "calendar_uuid"}
            )
        self._emit_event(
            resource,
            f"{self.event_prefix}.{action}",
            hidden_fields=hidden_fields,
            session=session,
        )


class CalendarController(CalendarResourceController):
    event_prefix = "calendar.calendar"
    __resource__ = resources.ResourceByRAModel(
        model_class=models.Calendar,
        hidden_fields=list(CalendarResourceController.hidden_fields),
        convert_underscore=False,
        process_filters=True,
    )

    def create(self, **kwargs: typing.Any) -> typing.Any:
        allowed = {"uuid", "name", "external_account_uuid"}
        if kwargs.keys() - allowed:
            raise exceptions.ValidationErrorException()
        session = contexts.Context().get_session()
        account_uuid = kwargs.get("external_account_uuid")
        if account_uuid is not None:
            account = external_models.ExternalAccount.objects.get_one(
                filters=self._owner_filters(
                    {
                        "uuid": dm_filters.EQ(account_uuid),
                        "disabled": dm_filters.EQ(False),
                        "provider": dm_filters.In(
                            account_capabilities.providers_with("calendar")
                        ),
                    }
                ),
                session=session,
            )
            if account.provider == "caldav":
                raise exceptions.ValidationErrorException()
        calendar = models.Calendar(
            **kwargs,
            project_id=self._get_project_id(),
            owner_user_uuid=self._get_user_uuid(),
        )
        calendar.insert(session=session)
        self._emit(calendar, "created", session)
        return calendar

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        calendar = self.get(uuid)
        if calendar.read_only or kwargs.keys() != {"name"}:
            raise exceptions.ValidationErrorException()
        session = contexts.Context().get_session()
        if groupware_caldav_write.binding(session, calendar.uuid) is not None:
            raise exceptions.ValidationErrorException()
        calendar.update_dm(values=kwargs)
        calendar.update(session=session)
        self._emit(calendar, "updated", session)
        return calendar

    def delete(self, uuid: object) -> None:
        calendar = self.get(uuid)
        if calendar.read_only:
            raise exceptions.ValidationErrorException()
        session = contexts.Context().get_session()
        if groupware_caldav_write.binding(session, calendar.uuid) is not None:
            raise exceptions.ValidationErrorException()
        if models.CalendarEvent.objects.get_all(
            filters=self._owner_filters(
                {"calendar_uuid": dm_filters.EQ(calendar.uuid)}
            ),
            session=session,
            limit=1,
        ):
            raise exceptions.ValidationErrorException()
        calendar.delete(session=session)
        self._emit(calendar, "deleted", session)


class CalendarEventController(CalendarResourceController):
    event_prefix = "calendar.event"
    hidden_fields = ("owner_user_uuid", "starts_at", "ends_at")
    __resource__ = resources.ResourceByRAModel(
        model_class=models.CalendarEvent,
        hidden_fields=list(hidden_fields),
        convert_underscore=False,
        process_filters=True,
    )

    def _prepare_filters(self, params: typing.Any) -> typing.Any:
        bounds = {
            "starts_before": ("starts_at", dm_filters.LT),
            "ends_after": ("ends_at", dm_filters.GT),
        }
        result = super()._prepare_filters(
            multidict.MultiDict(
                (key, value) for key, value in params.items() if key not in bounds
            )
        )
        timestamp = models.EventTimestamp()
        for key, (field, clause) in bounds.items():
            values = params.getall(key)
            if not values:
                continue
            if len(values) != 1:
                raise exceptions.ValidationErrorException()
            try:
                result[field] = clause(timestamp.from_simple_type(values[0]))
            except ValueError as error:
                raise exceptions.ValidationErrorException() from error
        return result

    def _writable_calendar(self, uuid: object, session: typing.Any) -> typing.Any:
        calendar = models.Calendar.objects.get_one(
            filters=self._owner_filters({"uuid": dm_filters.EQ(uuid)}),
            session=session,
        )
        if calendar.read_only:
            raise exceptions.ValidationErrorException()
        return calendar

    def create(self, **kwargs: typing.Any) -> typing.Any:
        if kwargs.keys() - {"uuid", "calendar_uuid", "payload"} or not (
            {"calendar_uuid", "payload"} <= kwargs.keys()
        ):
            raise exceptions.ValidationErrorException()
        session = contexts.Context().get_session()
        calendar = self._writable_calendar(kwargs["calendar_uuid"], session)
        event = models.CalendarEvent(
            **kwargs,
            **models.event_times(kwargs["payload"]),
            owner_user_uuid=self._get_user_uuid(),
            project_id=self._get_project_id(),
        )
        if (
            "uuid" in kwargs
            and groupware_caldav_write.binding(session, calendar.uuid) is not None
        ):
            existing = models.CalendarEvent.objects.get_one_or_none(
                filters=self._owner_filters(
                    {
                        "uuid": dm_filters.EQ(event.uuid),
                        "calendar_uuid": dm_filters.EQ(calendar.uuid),
                    }
                ),
                session=session,
            )
            if existing is not None and models.EVENT_PAYLOAD_TYPE.to_simple_type(
                existing.payload
            ) == models.EVENT_PAYLOAD_TYPE.to_simple_type(event.payload):
                return existing
        event.insert(session=session)
        groupware_caldav_write.create(session, calendar, event)
        self._emit(event, "created", session)
        return event

    def update(self, uuid: object, **kwargs: typing.Any) -> typing.Any:
        if kwargs.keys() != {"payload"}:
            raise exceptions.ValidationErrorException()
        event = self.get(uuid)
        session = contexts.Context().get_session()
        calendar = self._writable_calendar(event.calendar_uuid, session)
        event.update_dm(values=kwargs)
        for field, value in models.event_times(event.payload).items():
            event.properties[field].set_value_force(value)
        event.update(session=session)
        groupware_caldav_write.update(session, calendar, event)
        self._emit(event, "updated", session)
        return event

    def delete(self, uuid: object) -> None:
        event = self.get(uuid)
        session = contexts.Context().get_session()
        calendar = self._writable_calendar(event.calendar_uuid, session)
        groupware_caldav_write.delete(session, calendar, event)
        event.delete(session=session)
        self._emit(event, "deleted", session)

    @actions.post
    def move(self, resource: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if kwargs.keys() != {"calendar_uuid"}:
            raise exceptions.ValidationErrorException()
        try:
            calendar_uuid = sys_uuid.UUID(kwargs["calendar_uuid"])
        except (AttributeError, TypeError, ValueError) as error:
            raise exceptions.ValidationErrorException() from error
        session = contexts.Context().get_session()
        for uuid in sorted({resource.calendar_uuid, calendar_uuid}):
            self._writable_calendar(uuid, session)
        if resource.calendar_uuid != calendar_uuid:
            if any(
                groupware_caldav_write.binding(session, uuid) is not None
                for uuid in (resource.calendar_uuid, calendar_uuid)
            ):
                raise exceptions.ValidationErrorException()
            resource.calendar_uuid = calendar_uuid
            resource.update(session=session)
            self._emit(resource, "updated", session)
        return resource


CalendarEventController.move.openapi_schema = utils.Schema(
    summary="Move a calendar event",
    parameters=(),
    responses=constants.build_openapi_get_update_response("CalendarEvent_Get"),
    request_body=constants.build_openapi_req_body(
        "Destination calendar",
        "application/json",
        {
            "type": "object",
            "required": ["calendar_uuid"],
            "additionalProperties": False,
            "properties": {"calendar_uuid": {"type": "string", "format": "uuid"}},
        },
    ),
)
