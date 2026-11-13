# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Poll wire types shared by message payloads, actions, and OpenAPI."""

import datetime
import typing

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from workspace.common import types as common_types
from workspace.messenger_api import exceptions


MAX_POLL_QUESTIONS = 20
MAX_POLL_OPTIONS_PER_QUESTION = 20
VOTER_PREVIEW_LIMIT = 20


class PollUUID(types.UUID):
    def from_simple_type(self, value: typing.Any) -> typing.Any:
        if not isinstance(value, str):
            raise ra_exc.ValidationErrorException()
        return super().from_simple_type(value)


class PollText(types.String):
    def validate(self, value: typing.Any) -> bool:
        return super().validate(value) and bool(value.strip())


class PollTimestamp(types.UTCDateTimeZ):
    def from_simple_type(self, value: typing.Any) -> datetime.datetime:
        if not isinstance(value, str):
            raise ra_exc.ValidationErrorException()
        timestamp = datetime.datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
        if timestamp.tzinfo is None:
            raise ra_exc.ValidationErrorException()
        try:
            return timestamp.astimezone(datetime.timezone.utc)
        except OverflowError as exc:
            raise ra_exc.ValidationErrorException() from exc

    def to_simple_type(self, value: datetime.datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")


class Array(types.TypedList):
    def __init__(
        self, item: types.BaseType, minimum: int = 0, maximum: int | None = None
    ) -> None:
        super().__init__(item)
        self.minimum = minimum
        self.maximum = maximum

    def _has_allowed_item_count(self, value: typing.Any) -> bool:
        return (
            isinstance(value, list)
            and len(value) >= self.minimum
            and (self.maximum is None or len(value) <= self.maximum)
        )

    def validate(self, value: typing.Any) -> bool:
        return self._has_allowed_item_count(value) and super().validate(value)

    def from_simple_type(self, value: typing.Any) -> list:
        if not self._has_allowed_item_count(value):
            raise ra_exc.ValidationErrorException()
        return super().from_simple_type(value)

    def to_openapi_spec(self, prop_kwargs: dict) -> dict:
        result = super().to_openapi_spec(prop_kwargs)
        result.pop("example", None)
        if self.minimum:
            result["minItems"] = self.minimum
        if self.maximum is not None:
            result["maxItems"] = self.maximum
        return result


class Fields(types.SoftSchemeDict):
    """A closed JSON object with required keys and optional typed fields."""

    def __init__(self, fields: dict, optional: tuple[str, ...] = ()) -> None:
        super().__init__(fields)
        self.required = [name for name in fields if name not in optional]

    def validate(self, value: typing.Any) -> bool:
        return super().validate(value) and all(name in value for name in self.required)

    def from_simple_type(self, value: typing.Any) -> dict:
        if not isinstance(value, dict) or value.keys() - self._scheme.keys():
            raise ra_exc.ValidationErrorException()
        result = super().from_simple_type(value)
        if not self.validate(result):
            raise ra_exc.ValidationErrorException()
        return result

    def to_openapi_spec(self, prop_kwargs: dict) -> dict:
        spec = super().to_openapi_spec({})
        spec.pop("example", None)
        spec["additionalProperties"] = False
        if self.required:
            spec["required"] = list(self.required)
        return spec


POLL_OPTION = Fields(
    {"uuid": PollUUID(), "text": PollText(min_length=1, max_length=200)}
)
POLL_QUESTION = Fields(
    {
        "uuid": PollUUID(),
        "text": PollText(min_length=1, max_length=1000),
        "options": Array(POLL_OPTION, minimum=2),
        "correct_option_uuids": Array(PollUUID()),
    },
    optional=("correct_option_uuids",),
)
NULLABLE_TIME = types.AllowNone(PollTimestamp())
ACTION_BODIES = {
    "vote": Fields(
        {
            "question_uuid": PollUUID(),
            "option_uuid": PollUUID(),
            "selected": common_types.StrictBoolean(),
        }
    ),
    "close_poll": Fields({}),
    "update_settings": Fields(
        {
            "settings": Fields(
                {
                    "closes_at": NULLABLE_TIME,
                    "allow_adding_options": common_types.StrictBoolean(),
                }
            )
        }
    ),
    "add_poll_option": Fields({"question_uuid": PollUUID(), "option": POLL_OPTION}),
}


class PollCreatePayload(types_dynamic.AbstractKindModel):
    KIND = "poll"
    questions = properties.property(Array(POLL_QUESTION, minimum=1), required=True)
    multiple_answers = properties.property(common_types.StrictBoolean(), required=True)
    is_anonymous = properties.property(common_types.StrictBoolean(), required=True)
    allow_revote = properties.property(common_types.StrictBoolean(), required=True)
    results_visibility = properties.property(
        types.Enum(["always", "after_vote"]), required=True
    )
    allow_adding_options = properties.property(
        common_types.StrictBoolean(), default=False
    )
    closes_at = properties.property(NULLABLE_TIME, default=None)

    def validate(self) -> None:
        if len({question["uuid"] for question in self.questions}) != len(
            self.questions
        ):
            raise ra_exc.ValidationErrorException()
        for question in self.questions:
            options = {option["uuid"] for option in question["options"]}
            correct = question.get("correct_option_uuids", [])
            if (
                len(options) != len(question["options"])
                or len(set(correct)) != len(correct)
                or set(correct) - options
            ):
                raise ra_exc.ValidationErrorException()

    def validate_creation(self, now: datetime.datetime) -> None:
        if (
            (self.closes_at is not None and self.closes_at <= now)
            or len(self.questions) > MAX_POLL_QUESTIONS
            or any(
                len(q["options"]) > MAX_POLL_OPTIONS_PER_QUESTION
                for q in self.questions
            )
        ):
            raise ra_exc.ValidationErrorException()


class PollPayload(PollCreatePayload):
    closed_at = properties.property(NULLABLE_TIME, default=None)

    def is_closed(self, now: datetime.datetime) -> bool:
        return self.closed_at is not None or (
            self.closes_at is not None and self.closes_at <= now
        )

    def question(self, identifier: str) -> dict:
        for question in self.questions:
            if str(question["uuid"]) == identifier:
                return question
        raise ra_exc.ValidationErrorException()

    def select_options(
        self,
        question_uuid: str,
        option_uuid: str,
        selected: bool,
        current: set[str],
        now: datetime.datetime,
    ) -> set[str]:
        question = self.question(question_uuid)
        if not any(
            str(option["uuid"]) == option_uuid for option in question["options"]
        ):
            raise ra_exc.ValidationErrorException()
        if selected == (option_uuid in current):
            return current
        if self.is_closed(now):
            raise exceptions.PollClosedError()
        replace = selected and bool(current) and not self.multiple_answers
        if not self.allow_revote and (replace or not selected):
            raise exceptions.PollRevoteForbiddenError()
        if replace:
            return {option_uuid}
        return current | {option_uuid} if selected else current - {option_uuid}

    def close(self, now: datetime.datetime) -> bool:
        if self.is_closed(now):
            return False
        self.closed_at = now
        return True

    def append_option(
        self, question_uuid: str, option: dict, now: datetime.datetime
    ) -> bool:
        question = self.question(question_uuid)
        value = POLL_OPTION.from_simple_type(option)
        for existing in question["options"]:
            if existing["uuid"] == value["uuid"]:
                if existing["text"] != value["text"]:
                    raise exceptions.PollConflictError()
                return False
        if self.is_closed(now):
            raise exceptions.PollClosedError()
        if not self.allow_adding_options:
            raise exceptions.PollConflictError()
        if len(question["options"]) >= MAX_POLL_OPTIONS_PER_QUESTION:
            raise ra_exc.ValidationErrorException()
        question["options"].append(value)
        return True

    def update_settings(self, settings: dict, now: datetime.datetime) -> bool:
        if self.is_closed(now):
            raise exceptions.PollClosedError()
        deadline = NULLABLE_TIME.from_simple_type(settings["closes_at"])
        if deadline is not None and deadline <= now:
            raise ra_exc.ValidationErrorException()
        if (
            self.closes_at == deadline
            and self.allow_adding_options == settings["allow_adding_options"]
        ):
            return False
        self.closes_at = deadline
        self.allow_adding_options = settings["allow_adding_options"]
        return True


POLL_CREATE_TYPE = types_dynamic.KindModelType(PollCreatePayload)
POLL_TYPE = types_dynamic.KindModelType(PollPayload)


def normalize_create(raw_payload: typing.Any, *, retry: bool = False) -> dict:
    if isinstance(raw_payload, PollPayload) and raw_payload.closed_at is not None:
        raise ra_exc.ValidationErrorException()
    payload = (
        raw_payload
        if isinstance(raw_payload, PollCreatePayload)
        else POLL_CREATE_TYPE.from_simple_type(raw_payload)
    )
    if not retry:
        payload.validate_creation(datetime.datetime.now(datetime.timezone.utc))
    return {**POLL_CREATE_TYPE.to_simple_type(payload), "closed_at": None}


def action_body(action: str, values: dict) -> dict:
    wire_type = ACTION_BODIES[action]
    try:
        return wire_type.to_simple_type(wire_type.from_simple_type(values))
    except (ValueError, TypeError) as error:
        raise ra_exc.ValidationErrorException() from error


class PollVoter(models.ModelWithUUID):
    """Public identity returned by a non-anonymous poll voter page."""
