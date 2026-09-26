#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Immutable provider-native history batches, independent of SQL and storage."""

import typing

import dataclasses
import datetime
import hashlib
import json
import re
import urllib.parse
import uuid as sys_uuid

from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import message_payloads


BATCH_SIZE = 5000
MAX_SOURCE_BODY_BYTES = 64 * 1024 * 1024
# Schema v2 carries the immutable provider source and its Workspace projection.
# Admission is acquired before this bounded body is allocated.
MAX_BODY_BYTES = 2 * MAX_SOURCE_BODY_BYTES
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_PART_MESSAGES = 100
MAX_PART_ROWS = 2000
MAX_PART_BYTES = 1024 * 1024
MAX_SOURCES = 512
PATH = "/v1/history-imports"
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
FILE_SLOT_PREFIX = "urn:workspace-external-file:"
FILE_SLOT_PATTERN = re.compile(
    re.escape(FILE_SLOT_PREFIX)
    + r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
)


class ImportError(ValueError):
    """An expected import failure with a safe, body-free public code."""

    def __init__(self, code: str, status: int = 422) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def uuid_field(value: object) -> sys_uuid.UUID:
    if not isinstance(value, str):
        raise ImportError("invalid_history_uuid")
    try:
        return sys_uuid.UUID(value)
    except ValueError:
        raise ImportError("invalid_history_uuid") from None


def encode(value: object) -> bytes:
    # Matches bridge.history.digest exactly, including unchanged Unicode form.
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def identifier(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise ImportError("invalid_history_id")
    return value


def text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ImportError("invalid_history_text")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ImportError("invalid_history_text") from error
    return value


def markdown_content(value: object) -> str:
    content = text(value, message_payloads.MARKDOWN_CONTENT_MAX_LENGTH)
    if not (
        message_payloads.MarkdownPayload.properties.properties["content"]
        .get_property_type()
        .validate(content)
    ):
        raise ImportError("invalid_history_message_content")
    return content


def attachment_path(value: object) -> str:
    """Validate an authenticated provider upload path at the trust boundary."""

    path = text(value, 4096)
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ImportError("invalid_history_attachment_path")
    try:
        parsed = urllib.parse.urlsplit(path)
    except ValueError as error:
        raise ImportError("invalid_history_attachment_path") from error
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/user_uploads/")
        or not urllib.parse.urljoin("/", parsed.path).startswith("/user_uploads/")
    ):
        raise ImportError("invalid_history_attachment_path")
    for raw_segment in parsed.path.split("/"):
        segment = raw_segment
        for _ in range(8):
            if segment in {".", ".."} or any(
                character in "/\\" or ord(character) < 32 or ord(character) == 127
                for character in segment
            ):
                raise ImportError("invalid_history_attachment_path")
            try:
                decoded = urllib.parse.unquote(segment, errors="strict")
            except UnicodeError as error:
                raise ImportError("invalid_history_attachment_path") from error
            if decoded == segment:
                break
            segment = decoded
        else:
            raise ImportError("invalid_history_attachment_path")
    return path


def exact_fields(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ImportError("invalid_history_fields")
    return value


def sorted_unique(values: list, key: typing.Callable) -> None:
    keys = [key(value) for value in values]
    if keys != sorted(set(keys)):
        raise ImportError("noncanonical_history_order")


def verify_hash(value: dict) -> None:
    expected = value["hash"]
    if (
        not isinstance(expected, str)
        or HASH_PATTERN.fullmatch(expected) is None
        or digest({key: item for key, item in value.items() if key != "hash"})
        != expected
    ):
        raise ImportError("history_hash_mismatch")


def chat_key(message: dict) -> str:
    if message["channel_id"] is not None:
        return f"channel:{message['channel_id']}"
    participants = message["recipient_ids"]
    return f"direct-conversation:v1:{len(participants)}:" + ",".join(
        str(value) for value in participants
    )


@dataclasses.dataclass(frozen=True)
class Batch:
    project_uuid: sys_uuid.UUID
    provider_realm_uuid: sys_uuid.UUID
    generation: int
    sources: tuple[dict, ...]
    body: dict
    schema_version: int = 1
    converter_version: str | None = None
    projections: tuple[dict, ...] = ()

    @classmethod
    def parse(cls: typing.Any, envelope: object) -> "Batch":
        if not isinstance(envelope, dict):
            raise ImportError("invalid_history_fields")
        schema_version = envelope.get("schema_version")
        if type(schema_version) is not int or schema_version not in {1, 2}:
            raise ImportError("unsupported_history_version")
        fields = {
            "schema_version",
            "project_uuid",
            "provider_realm_uuid",
            "generation",
            "sources",
            "batch",
        }
        if schema_version == 2:
            fields.update({"converter_version", "projections"})
        envelope = exact_fields(envelope, fields)
        source_envelope = {
            key: value
            for key, value in envelope.items()
            if key not in {"converter_version", "projections"}
        }
        source_envelope["schema_version"] = 1
        if len(encode(source_envelope)) > MAX_SOURCE_BODY_BYTES:
            raise ImportError("history_source_body_too_large", 413)
        generation = identifier(envelope["generation"])
        sources = envelope["sources"]
        if not isinstance(sources, list) or not sources:
            raise ImportError("invalid_history_sources")
        if len(sources) > MAX_SOURCES:
            raise ImportError("history_source_limit_exceeded")
        for source in sources:
            exact_fields(
                source,
                {
                    "account_uuid",
                    "account_generation",
                    "chat_uuid",
                    "assignment_generation",
                },
            )
            uuid_field(source["account_uuid"])
            uuid_field(source["chat_uuid"])
            identifier(source["account_generation"])
            identifier(source["assignment_generation"])
        sorted_unique(sources, lambda source: source["chat_uuid"])
        body = exact_fields(
            envelope["batch"],
            {
                "from_id",
                "to_id",
                "users",
                "messages",
                "hash",
            },
        )
        start = identifier(body["from_id"])
        if (start - 1) % BATCH_SIZE or body["to_id"] != start + BATCH_SIZE - 1:
            raise ImportError("invalid_history_range")
        users, messages = body["users"], body["messages"]
        if not isinstance(users, list) or not isinstance(messages, list):
            raise ImportError("invalid_history_records")
        if len(messages) > BATCH_SIZE:
            raise ImportError("history_range_overflow")
        for user in users:
            exact_fields(user, {"id", "name"})
            identifier(user["id"])
            text(user["name"], 1024)
            if (
                not models.WorkspaceUser.properties.properties["first_name"]
                .get_property_type()
                .validate(user["name"])
            ):
                raise ImportError("invalid_history_user_name")
        sorted_unique(users, lambda user: user["id"])
        user_ids = {user["id"] for user in users}
        for message in messages:
            fields = {
                "id",
                "sender_id",
                "channel_id",
                "channel_name",
                "topic",
                "sent_at",
                "content",
                "reactions",
                "access",
                "hash",
            }
            if isinstance(message, dict) and message.get("channel_id") is None:
                fields.add("recipient_ids")
            exact_fields(message, fields)
            if not start <= identifier(message["id"]) <= body["to_id"]:
                raise ImportError("history_message_outside_range")
            identifier(message["sender_id"])
            identifier(message["sent_at"])
            datetime.datetime.fromtimestamp(message["sent_at"], datetime.timezone.utc)
            markdown_content(message["content"])
            referenced = {message["sender_id"]}
            if message["channel_id"] is None:
                if message["channel_name"] is not None or message["topic"] is not None:
                    raise ImportError("invalid_history_direct_message")
                recipients = message["recipient_ids"]
                if not isinstance(recipients, list) or not recipients:
                    raise ImportError("invalid_history_recipients")
                for recipient in recipients:
                    identifier(recipient)
                sorted_unique(recipients, lambda value: value)
                referenced.update(recipients)
            else:
                identifier(message["channel_id"])
                text(message["channel_name"], 1024)
                text(message["topic"], 1024)
                source_properties = models.ZulipSource.properties.properties
                if not source_properties["stream_id"].get_property_type().validate(
                    message["channel_id"]
                ) or not source_properties["topic_name"].get_property_type().validate(
                    message["topic"]
                ):
                    raise ImportError("invalid_history_channel_metadata")
            access, reactions = message["access"], message["reactions"]
            if (
                not isinstance(access, list)
                or not access
                or not isinstance(reactions, list)
            ):
                raise ImportError("invalid_history_observations")
            for observation in access:
                exact_fields(observation, {"user_id", "read", "starred"})
                identifier(observation["user_id"])
                if (
                    type(observation["read"]) is not bool
                    or type(observation["starred"]) is not bool
                ):
                    raise ImportError("invalid_history_flags")
                referenced.add(observation["user_id"])
            sorted_unique(access, lambda item: item["user_id"])
            for reaction in reactions:
                exact_fields(
                    reaction, {"user_id", "reaction_type", "emoji_code", "emoji_name"}
                )
                identifier(reaction["user_id"])
                for field in ("reaction_type", "emoji_code", "emoji_name"):
                    text(reaction[field], 128)
                referenced.add(reaction["user_id"])
            sorted_unique(
                reactions,
                lambda item: (
                    item["user_id"],
                    item["reaction_type"],
                    item["emoji_code"],
                ),
            )
            if not referenced.issubset(user_ids):
                raise ImportError("history_user_missing_from_directory")
            if message_row_cost(message) > MAX_PART_ROWS:
                raise ImportError("history_message_fanout_limit")
            if len(encode(message)) > MAX_PART_BYTES:
                raise ImportError("history_message_bytes_limit")
            verify_hash(message)
        sorted_unique(messages, lambda message: message["id"])
        verify_hash(body)
        converter_version = None
        projections: tuple[dict, ...] = ()
        if schema_version == 2:
            converter_version = text(envelope["converter_version"], 128)
            if not converter_version:
                raise ImportError("invalid_history_converter_version")
            raw_projections = envelope["projections"]
            if not isinstance(raw_projections, list):
                raise ImportError("invalid_history_projections")
            message_by_id = {message["id"]: message for message in messages}
            for projection in raw_projections:
                exact_fields(
                    projection,
                    {
                        "message_id",
                        "source_hash",
                        "converter_version",
                        "workspace_content",
                        "attachments",
                        "projection_sha256",
                    },
                )
                message_id = identifier(projection["message_id"])
                source = message_by_id.get(message_id)
                if (
                    source is None
                    or projection["source_hash"] != source["hash"]
                    or projection["converter_version"] != converter_version
                ):
                    raise ImportError("history_projection_source_mismatch")
                content = markdown_content(projection["workspace_content"])
                attachments = projection["attachments"]
                if not isinstance(attachments, list):
                    raise ImportError("invalid_history_projection_attachments")
                for attachment in attachments:
                    exact_fields(attachment, {"slot_uuid", "source_path"})
                    slot_uuid = uuid_field(attachment["slot_uuid"])
                    attachment_path(attachment["source_path"])
                    if f"{FILE_SLOT_PREFIX}{slot_uuid}" not in content:
                        raise ImportError("history_projection_slot_missing")
                sorted_unique(attachments, lambda value: value["slot_uuid"])
                slots = {
                    str(sys_uuid.UUID(value)).lower()
                    for value in FILE_SLOT_PATTERN.findall(content)
                }
                if slots != {
                    str(sys_uuid.UUID(item["slot_uuid"])) for item in attachments
                }:
                    raise ImportError("history_projection_slot_mismatch")
                if (
                    1
                    + 2 * len(source["access"])
                    + len(source["reactions"])
                    + len(attachments) * (1 + len(source["access"]))
                    > MAX_PART_ROWS
                    or len(encode(projection)) > MAX_PART_BYTES
                ):
                    raise ImportError("history_projection_fanout_limit")
                projection_sha256 = projection["projection_sha256"]
                if (
                    not isinstance(projection_sha256, str)
                    or HASH_PATTERN.fullmatch(projection_sha256) is None
                    or digest({"content": content, "attachments": attachments})
                    != projection_sha256
                ):
                    raise ImportError("history_projection_hash_mismatch")
            sorted_unique(raw_projections, lambda value: value["message_id"])
            if [value["message_id"] for value in raw_projections] != [
                value["id"] for value in messages
            ]:
                raise ImportError("history_projection_coverage_mismatch")
            projections = tuple(raw_projections)
        return cls(
            uuid_field(envelope["project_uuid"]),
            uuid_field(envelope["provider_realm_uuid"]),
            generation,
            tuple(sources),
            body,
            schema_version,
            converter_version,
            projections,
        )

    def job_uuid(
        self, bridge_uuid: sys_uuid.UUID, identity_generation: int = 1
    ) -> sys_uuid.UUID:
        components = [
            f"history:v{self.schema_version}",
            str(identity_generation),
            str(self.project_uuid),
            str(self.provider_realm_uuid),
            str(self.generation),
            str(self.body["from_id"]),
            self.body["hash"],
            digest(list(self.sources)),
        ]
        if self.schema_version == 2:
            components.append(
                digest(
                    {
                        "converter_version": self.converter_version,
                        "projections": list(self.projections),
                    }
                )
            )
        return sys_uuid.uuid5(
            bridge_uuid,
            ":".join(components),
        )

    def projection_for(self, message_id: int) -> dict | None:
        if self.schema_version == 1:
            return None
        return next(
            (
                projection
                for projection in self.projections
                if projection["message_id"] == message_id
            ),
            None,
        )


def message_row_cost(message: dict, attachment_count: int | None = None) -> int:
    # Conservative attachment accounting also bounds file lookups and ACL fanout.
    files = (
        message["content"].count("/user_uploads/")
        if attachment_count is None
        else attachment_count
    )
    return (
        1
        + 2 * len(message["access"])
        + len(message["reactions"])
        + files * (1 + len(message["access"]))
    )


def message_parts(
    messages: list[dict],
    max_messages: int = MAX_PART_MESSAGES,
    attachment_counts: dict[int, int] | None = None,
) -> typing.Iterator[list[dict]]:
    """Bound work by message fanout and bytes, not by the total history size."""
    part: list[dict] = []
    rows, size = 0, 0
    for message in messages:
        message_rows = message_row_cost(
            message,
            None
            if attachment_counts is None
            else attachment_counts.get(message["id"], 0),
        )
        message_size = len(encode(message))
        if part and (
            len(part) >= max_messages
            or rows + message_rows > MAX_PART_ROWS
            or size + message_size > MAX_PART_BYTES
        ):
            yield part
            part, rows, size = [], 0, 0
        part.append(message)
        rows += message_rows
        size += message_size
    if part:
        yield part
