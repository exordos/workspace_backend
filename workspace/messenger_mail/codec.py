# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import datetime
import email
import email.message
import email.policy
import email.utils
import json
import typing
import uuid as sys_uuid


SCHEMA_VERSION = 1
MESSAGE_ID_DOMAIN = "messenger.workspace.invalid"
HEADER_SCHEMA_VERSION = "X-Workspace-Schema-Version"
HEADER_PROJECT_UUID = "X-Workspace-Project-UUID"
HEADER_MESSAGE_UUID = "X-Workspace-Message-UUID"
HEADER_STREAM_UUID = "X-Workspace-Stream-UUID"
HEADER_TOPIC_UUID = "X-Workspace-Topic-UUID"
HEADER_AUTHOR_UUID = "X-Workspace-Author-UUID"
HEADER_OPERATION_UUID = "X-Workspace-Operation-UUID"
HEADER_SOURCE_NAME = "X-Workspace-Source-Name"
HEADER_SOURCE = "X-Workspace-Source"


class InvalidMessengerMessage(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class MessengerEnvelope:
    from_address: str
    to_addresses: tuple[str, ...]
    project_uuid: sys_uuid.UUID
    message_uuid: sys_uuid.UUID
    stream_uuid: sys_uuid.UUID
    topic_uuid: sys_uuid.UUID
    author_uuid: sys_uuid.UUID
    operation_uuid: sys_uuid.UUID
    markdown: str
    sent_at: datetime.datetime
    schema_version: int = SCHEMA_VERSION
    source_name: str = "native"
    source: dict[str, typing.Any] = dataclasses.field(
        default_factory=lambda: {"kind": "native"}
    )


def message_id(message_uuid: sys_uuid.UUID) -> str:
    return f"<{message_uuid}@{MESSAGE_ID_DOMAIN}>"


def _utc_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        raise ValueError("Messenger message timestamp must be timezone-aware")
    return value.astimezone(datetime.timezone.utc)


def build_message(envelope: MessengerEnvelope) -> email.message.EmailMessage:
    message = email.message.EmailMessage(policy=email.policy.SMTP)
    message["From"] = envelope.from_address
    message["To"] = ", ".join(envelope.to_addresses)
    message["Date"] = email.utils.format_datetime(_utc_datetime(envelope.sent_at))
    message["Message-ID"] = message_id(envelope.message_uuid)
    message[HEADER_SCHEMA_VERSION] = str(envelope.schema_version)
    message[HEADER_PROJECT_UUID] = str(envelope.project_uuid)
    message[HEADER_MESSAGE_UUID] = str(envelope.message_uuid)
    message[HEADER_STREAM_UUID] = str(envelope.stream_uuid)
    message[HEADER_TOPIC_UUID] = str(envelope.topic_uuid)
    message[HEADER_AUTHOR_UUID] = str(envelope.author_uuid)
    message[HEADER_OPERATION_UUID] = str(envelope.operation_uuid)
    message[HEADER_SOURCE_NAME] = envelope.source_name
    message[HEADER_SOURCE] = json.dumps(
        envelope.source,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    message.set_type("text/markdown")
    message.set_payload(envelope.markdown, charset="utf-8")
    return message


def _required_header(message: email.message.Message, name: str) -> str:
    value = message.get(name)
    if value is None:
        raise InvalidMessengerMessage(f"Missing required {name} header")
    return str(value)


def _uuid_header(message: email.message.Message, name: str) -> sys_uuid.UUID:
    try:
        return sys_uuid.UUID(_required_header(message, name))
    except ValueError as exc:
        raise InvalidMessengerMessage(f"Invalid {name} header") from exc


def _addresses(message: email.message.Message, name: str) -> tuple[str, ...]:
    return tuple(
        email.utils.formataddr((display_name, address))
        for display_name, address in email.utils.getaddresses(message.get_all(name, []))
    )


def _sent_at(message: email.message.Message) -> datetime.datetime:
    try:
        value = email.utils.parsedate_to_datetime(_required_header(message, "Date"))
    except (TypeError, ValueError) as exc:
        raise InvalidMessengerMessage("Invalid Date header") from exc
    if value is None:
        raise InvalidMessengerMessage("Invalid Date header")
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def parse_message(raw_message: bytes) -> MessengerEnvelope:
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    if message.is_multipart():
        raise InvalidMessengerMessage("MIME multipart messages are not supported")
    if (
        message.get_content_disposition() == "attachment"
        or message.get_filename() is not None
    ):
        raise InvalidMessengerMessage("MIME attachments are not supported")
    if message.get_content_type() != "text/markdown":
        raise InvalidMessengerMessage("Messenger body must be UTF-8 markdown")
    charset = message.get_content_charset()
    if charset is None or charset.lower() != "utf-8":
        raise InvalidMessengerMessage("Messenger body must be UTF-8 markdown")

    try:
        schema_version = int(_required_header(message, HEADER_SCHEMA_VERSION))
    except ValueError as exc:
        raise InvalidMessengerMessage("Invalid messenger schema version") from exc
    if schema_version != SCHEMA_VERSION:
        raise InvalidMessengerMessage("Unsupported messenger schema version")

    message_uuid = _uuid_header(message, HEADER_MESSAGE_UUID)
    if _required_header(message, "Message-ID") != message_id(message_uuid):
        raise InvalidMessengerMessage("Message-ID does not match message UUID")
    markdown = message.get_content()
    if not isinstance(markdown, str):
        raise InvalidMessengerMessage("Messenger body must be UTF-8 markdown")

    try:
        source = json.loads(_required_header(message, HEADER_SOURCE))
    except json.JSONDecodeError as exc:
        raise InvalidMessengerMessage("Invalid message source header") from exc
    if not isinstance(source, dict):
        raise InvalidMessengerMessage("Invalid message source header")

    return MessengerEnvelope(
        from_address=_required_header(message, "From"),
        to_addresses=_addresses(message, "To"),
        project_uuid=_uuid_header(message, HEADER_PROJECT_UUID),
        message_uuid=message_uuid,
        stream_uuid=_uuid_header(message, HEADER_STREAM_UUID),
        topic_uuid=_uuid_header(message, HEADER_TOPIC_UUID),
        author_uuid=_uuid_header(message, HEADER_AUTHOR_UUID),
        operation_uuid=_uuid_header(message, HEADER_OPERATION_UUID),
        markdown=markdown,
        sent_at=_sent_at(message),
        schema_version=schema_version,
        source_name=_required_header(message, HEADER_SOURCE_NAME),
        source=source,
    )
