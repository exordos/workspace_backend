# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy
import dataclasses
import datetime
import email
import email.message
import email.policy
import email.utils
import json
import typing
import uuid as sys_uuid

from workspace.messenger_mail import protocol


SCHEMA_VERSION = 1
STATE_MAILBOX = "Workspace/State"
EVENT_MAILBOX_PREFIX = "Workspace/Events"
JOURNAL_MESSAGE_ID_DOMAIN = "journal.messenger.workspace.invalid"
EVENT_MESSAGE_ID_DOMAIN = "events.messenger.workspace.invalid"
JOURNAL_EMAIL_POLICY = email.policy.SMTP.clone(max_line_length=998)
DM_NAMESPACE = sys_uuid.UUID("26f42131-4503-5f59-8aa6-32b20f551751")
RECORD_KIND_HEADER = "X-Workspace-Record-Kind"
PROJECT_UUID_HEADER = "X-Workspace-Project-UUID"
OPERATION_UUID_HEADER = "X-Workspace-Operation-UUID"
EVENT_UUID_HEADER = "X-Workspace-Event-UUID"
USER_UUID_HEADER = "X-Workspace-User-UUID"
OPERATION_KEYWORD = "$WorkspaceOperation"
EVENT_KEYWORD = "$WorkspaceEvent"
EVENT_RETENTION = datetime.timedelta(days=7)
MESSAGE_DELETE_BODY_FIELDS = frozenset({"body", "content", "markdown", "payload"})
UPSERT_OPERATIONS = {
    "stream.create": "streams",
    "stream.update": "streams",
    "binding.create": "bindings",
    "binding.update": "bindings",
    "stream_binding.create": "bindings",
    "stream_binding.update": "bindings",
    "topic.create": "topics",
    "topic.update": "topics",
    "reaction.create": "reactions",
    "reaction.update": "reactions",
    "folder.create": "folders",
    "folder.update": "folders",
    "folder_item.create": "folder_items",
    "folder_item.update": "folder_items",
    "file.create": "files",
    "file.update": "files",
}
DELETE_OPERATIONS = {
    "stream.delete": "streams",
    "binding.delete": "bindings",
    "stream_binding.delete": "bindings",
    "topic.delete": "topics",
    "reaction.delete": "reactions",
    "folder.delete": "folders",
    "folder_item.delete": "folder_items",
    "file.delete": "files",
}
MESSAGE_STATE_OPERATIONS = frozenset(
    {
        "message.state",
        "message.read",
        "message.unread",
        "message.pin",
        "message.unpin",
        "message.star",
        "message.unstar",
    }
)
FOLDER_ITEM_PIN_OPERATIONS = frozenset({"folder_item.pin", "folder_item.unpin"})
MESSAGE_OPERATIONS = frozenset(
    {"message.create", "message.edit", "message.update", "message.delete"}
)
SUPPORTED_OPERATIONS = frozenset(UPSERT_OPERATIONS) | frozenset(DELETE_OPERATIONS)
SUPPORTED_OPERATIONS |= MESSAGE_STATE_OPERATIONS | FOLDER_ITEM_PIN_OPERATIONS
SUPPORTED_OPERATIONS |= MESSAGE_OPERATIONS


class InvalidJournalRecord(ValueError):
    pass


class MissingAppendUid(RuntimeError):
    pass


class UidValidityChanged(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class OperationRecord:
    project_uuid: sys_uuid.UUID
    operation_uuid: sys_uuid.UUID
    actor_uuid: sys_uuid.UUID
    operation: str
    entity_uuid: sys_uuid.UUID
    payload: dict[str, typing.Any]
    occurred_at: datetime.datetime
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise InvalidJournalRecord("Unsupported operation schema version")
        if self.operation not in SUPPORTED_OPERATIONS:
            raise InvalidJournalRecord(
                f"Unsupported messenger operation {self.operation}"
            )
        if self.operation == "message.create":
            payload = copy.deepcopy(self.payload)
            payload.setdefault("source_name", "native")
            payload.setdefault("source", {"kind": payload["source_name"]})
            object.__setattr__(self, "payload", payload)
        if self.operation.startswith("file.") and _contains_binary(self.payload):
            raise InvalidJournalRecord("File journal records store metadata only")
        if (
            self.operation == "message.delete"
            and MESSAGE_DELETE_BODY_FIELDS.intersection(self.payload)
        ):
            raise InvalidJournalRecord("Message tombstones must not retain body data")


@dataclasses.dataclass(frozen=True)
class EventRecord:
    project_uuid: sys_uuid.UUID
    event_uuid: sys_uuid.UUID
    operation_uuid: sys_uuid.UUID
    user_uuid: sys_uuid.UUID
    object_type: str
    action: str
    payload: dict[str, typing.Any]
    occurred_at: datetime.datetime
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise InvalidJournalRecord("Unsupported event schema version")


@dataclasses.dataclass(frozen=True)
class EpochCursor:
    uid_validity: int
    epoch_version: int


@dataclasses.dataclass(frozen=True)
class EventCursorState:
    epoch_generation: str
    current_epoch_version: int
    minimum_epoch_version: int

    def as_dict(self) -> dict[str, typing.Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class EpochEvent:
    cursor: EpochCursor
    record: EventRecord

    def as_dict(self) -> dict[str, typing.Any]:
        timestamp = _datetime_text(self.record.occurred_at)
        return {
            "uuid": str(self.record.event_uuid),
            "project_id": str(self.record.project_uuid),
            "user_uuid": str(self.record.user_uuid),
            "schema_version": self.record.schema_version,
            "epoch_version": self.cursor.epoch_version,
            "object_type": self.record.object_type,
            "action": self.record.action,
            "payload": copy.deepcopy(self.record.payload),
            "created_at": timestamp,
            "updated_at": timestamp,
        }


@dataclasses.dataclass(frozen=True)
class JournalEntry:
    position: protocol.AppendUid
    flags: frozenset[str]
    record: OperationRecord


@dataclasses.dataclass(frozen=True)
class JournalReplay:
    metadata: protocol.MailboxMetadata
    entries: tuple[JournalEntry, ...]


def deterministic_dm_uuid(
    project_uuid: sys_uuid.UUID,
    first_user_uuid: sys_uuid.UUID,
    second_user_uuid: sys_uuid.UUID,
) -> sys_uuid.UUID:
    if first_user_uuid == second_user_uuid:
        raise ValueError("A direct chat requires two distinct IAM users")
    user_values = sorted((str(first_user_uuid), str(second_user_uuid)))
    return sys_uuid.uuid5(
        DM_NAMESPACE,
        f"{project_uuid}:{user_values[0]}:{user_values[1]}",
    )


def _utc_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        raise InvalidJournalRecord("Journal timestamps must be timezone-aware")
    return value.astimezone(datetime.timezone.utc)


def _datetime_text(value: datetime.datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc_datetime(parsed)


def _message_id(record_uuid: sys_uuid.UUID, domain: str) -> str:
    return f"<{record_uuid}@{domain}>"


def _message_id_header(message: email.message.Message) -> str:
    value = message.get("Message-ID")
    if value is None:
        raise InvalidJournalRecord("Journal record has no Message-ID")
    return str(value).strip()


def _contains_binary(value: typing.Any) -> bool:
    if isinstance(value, bytes):
        return True
    if isinstance(value, dict):
        return any(_contains_binary(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_binary(item) for item in value)
    return False


def _operation_data(record: OperationRecord) -> dict[str, typing.Any]:
    return {
        "schema_version": record.schema_version,
        "record_kind": "operation",
        "project_uuid": str(record.project_uuid),
        "operation_uuid": str(record.operation_uuid),
        "actor_uuid": str(record.actor_uuid),
        "operation": record.operation,
        "entity_uuid": str(record.entity_uuid),
        "payload": copy.deepcopy(record.payload),
        "occurred_at": _datetime_text(record.occurred_at),
    }


def _event_data(record: EventRecord) -> dict[str, typing.Any]:
    return {
        "schema_version": record.schema_version,
        "record_kind": "event",
        "project_uuid": str(record.project_uuid),
        "event_uuid": str(record.event_uuid),
        "operation_uuid": str(record.operation_uuid),
        "user_uuid": str(record.user_uuid),
        "object_type": record.object_type,
        "action": record.action,
        "payload": copy.deepcopy(record.payload),
        "occurred_at": _datetime_text(record.occurred_at),
    }


def _json_message(
    data: dict[str, typing.Any],
    record_uuid: sys_uuid.UUID,
    project_uuid: sys_uuid.UUID,
    operation_uuid: sys_uuid.UUID,
    kind: str,
    domain: str,
    occurred_at: datetime.datetime,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    message = email.message.EmailMessage(policy=JOURNAL_EMAIL_POLICY)
    message["Date"] = email.utils.format_datetime(_utc_datetime(occurred_at))
    message["Message-ID"] = _message_id(record_uuid, domain)
    message[RECORD_KIND_HEADER] = kind
    message[PROJECT_UUID_HEADER] = str(project_uuid)
    message[OPERATION_UUID_HEADER] = str(operation_uuid)
    for name, value in (extra_headers or {}).items():
        message[name] = value
    message.set_type("application/json")
    message.set_payload(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        charset="utf-8",
    )
    return message.as_bytes()


def encode_operation(record: OperationRecord) -> bytes:
    return _json_message(
        _operation_data(record),
        record.operation_uuid,
        record.project_uuid,
        record.operation_uuid,
        "operation",
        JOURNAL_MESSAGE_ID_DOMAIN,
        record.occurred_at,
    )


def encode_event(record: EventRecord) -> bytes:
    return _json_message(
        _event_data(record),
        record.event_uuid,
        record.project_uuid,
        record.operation_uuid,
        "event",
        EVENT_MESSAGE_ID_DOMAIN,
        record.occurred_at,
        {
            EVENT_UUID_HEADER: str(record.event_uuid),
            USER_UUID_HEADER: str(record.user_uuid),
        },
    )


def _decode_json_message(
    raw_message: bytes, expected_kind: str
) -> dict[str, typing.Any]:
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    if message.is_multipart() or message.get_content_type() != "application/json":
        raise InvalidJournalRecord("Journal records must be JSON MIME messages")
    if message.get(RECORD_KIND_HEADER) != expected_kind:
        raise InvalidJournalRecord("Unexpected journal record kind")
    body = message.get_payload(decode=True)
    if not isinstance(body, bytes):
        raise InvalidJournalRecord("Journal record has no JSON body")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidJournalRecord("Invalid journal JSON") from exc
    if not isinstance(data, dict):
        raise InvalidJournalRecord("Journal JSON must be an object")
    if data["record_kind"] != expected_kind:
        raise InvalidJournalRecord("Unexpected journal JSON record kind")
    if data["schema_version"] != SCHEMA_VERSION:
        raise InvalidJournalRecord("Unsupported journal schema version")
    return data


def decode_operation(raw_message: bytes) -> OperationRecord:
    data = _decode_json_message(raw_message, "operation")
    record = OperationRecord(
        project_uuid=sys_uuid.UUID(data["project_uuid"]),
        operation_uuid=sys_uuid.UUID(data["operation_uuid"]),
        actor_uuid=sys_uuid.UUID(data["actor_uuid"]),
        operation=data["operation"],
        entity_uuid=sys_uuid.UUID(data["entity_uuid"]),
        payload=data["payload"],
        occurred_at=_parse_datetime(data["occurred_at"]),
        schema_version=data["schema_version"],
    )
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    if message.get(PROJECT_UUID_HEADER) != str(record.project_uuid):
        raise InvalidJournalRecord("Operation project header does not match JSON")
    if message.get(OPERATION_UUID_HEADER) != str(record.operation_uuid):
        raise InvalidJournalRecord("Operation UUID header does not match JSON")
    expected_message_id = _message_id(
        record.operation_uuid,
        JOURNAL_MESSAGE_ID_DOMAIN,
    )
    if _message_id_header(message) != expected_message_id:
        raise InvalidJournalRecord("Operation Message-ID does not match UUID")
    return record


def decode_event(raw_message: bytes) -> EventRecord:
    data = _decode_json_message(raw_message, "event")
    record = EventRecord(
        project_uuid=sys_uuid.UUID(data["project_uuid"]),
        event_uuid=sys_uuid.UUID(data["event_uuid"]),
        operation_uuid=sys_uuid.UUID(data["operation_uuid"]),
        user_uuid=sys_uuid.UUID(data["user_uuid"]),
        object_type=data["object_type"],
        action=data["action"],
        payload=data["payload"],
        occurred_at=_parse_datetime(data["occurred_at"]),
        schema_version=data["schema_version"],
    )
    message = email.message_from_bytes(raw_message, policy=email.policy.default)
    header_values = {
        PROJECT_UUID_HEADER: str(record.project_uuid),
        OPERATION_UUID_HEADER: str(record.operation_uuid),
        EVENT_UUID_HEADER: str(record.event_uuid),
        USER_UUID_HEADER: str(record.user_uuid),
    }
    if any(message.get(name) != value for name, value in header_values.items()):
        raise InvalidJournalRecord("Event headers do not match JSON")
    expected_message_id = _message_id(record.event_uuid, EVENT_MESSAGE_ID_DOMAIN)
    if _message_id_header(message) != expected_message_id:
        raise InvalidJournalRecord("Event Message-ID does not match UUID")
    return record


class Projection:
    def __init__(self) -> None:
        self.streams: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.bindings: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.topics: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.messages: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.message_tombstones: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.reactions: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.message_states: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID], dict[str, bool]
        ] = {}
        self.folders: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.folder_items: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.files: dict[sys_uuid.UUID, dict[str, typing.Any]] = {}
        self.operation_positions: dict[sys_uuid.UUID, protocol.AppendUid] = {}
        # Keep the physical journal UID set separately from operation UUIDs.
        # Operation UUIDs are idempotency keys and duplicate records can therefore
        # collapse in ``operation_positions``.  Physical UIDs let incremental
        # refresh detect an expunged canonical record without fetching every body.
        self.journal_uids: set[int] = set()
        self.message_positions: dict[sys_uuid.UUID, protocol.AppendUid] = {}
        self.uid_validity: int | None = None
        self.uid_next: int | None = None
        self.highest_modseq: int | None = None

    def apply(self, record: OperationRecord, position: protocol.AppendUid) -> bool:
        if record.operation_uuid in self.operation_positions:
            return False

        operation = record.operation
        if operation in UPSERT_OPERATIONS:
            self._upsert(UPSERT_OPERATIONS[operation], record)
        elif operation in DELETE_OPERATIONS:
            self._delete(DELETE_OPERATIONS[operation], record)
        elif operation == "message.create":
            self._create_message(record, position)
        elif operation in {"message.edit", "message.update"}:
            self._update_message(record)
        elif operation == "message.delete":
            self._delete_message(record)
        elif operation in MESSAGE_STATE_OPERATIONS:
            self._update_message_state(record)
        elif operation in FOLDER_ITEM_PIN_OPERATIONS:
            self._update_folder_item_pin(record)
        else:
            raise InvalidJournalRecord(f"Unsupported messenger operation {operation}")

        self.operation_positions[record.operation_uuid] = position
        return True

    def _collection(
        self,
        name: str,
    ) -> dict[sys_uuid.UUID, dict[str, typing.Any]]:
        return typing.cast(
            dict[sys_uuid.UUID, dict[str, typing.Any]],
            getattr(self, name),
        )

    def _upsert(self, collection_name: str, record: OperationRecord) -> None:
        collection = self._collection(collection_name)
        previous = collection.get(record.entity_uuid, {})
        value = copy.deepcopy(previous)
        value.update(copy.deepcopy(record.payload))
        value["uuid"] = str(record.entity_uuid)
        collection[record.entity_uuid] = value

    def _delete(self, collection_name: str, record: OperationRecord) -> None:
        collection = self._collection(collection_name)
        collection.pop(record.entity_uuid, None)
        if collection_name == "streams":
            stream_uuid = str(record.entity_uuid)
            self.bindings = _without_reference(
                self.bindings, "stream_uuid", stream_uuid
            )
            self.topics = _without_reference(self.topics, "stream_uuid", stream_uuid)
            self.folder_items = _without_reference(
                self.folder_items, "stream_uuid", stream_uuid
            )
        elif collection_name == "folders":
            self.folder_items = _without_reference(
                self.folder_items,
                "folder_uuid",
                str(record.entity_uuid),
            )

    def _create_message(
        self,
        record: OperationRecord,
        position: protocol.AppendUid,
    ) -> None:
        if record.entity_uuid in self.message_positions:
            return
        value = copy.deepcopy(record.payload)
        value["uuid"] = str(record.entity_uuid)
        value["created_at"] = record.occurred_at
        self.messages[record.entity_uuid] = value
        self.message_positions[record.entity_uuid] = position

    def _update_message(self, record: OperationRecord) -> None:
        if record.entity_uuid not in self.messages:
            return
        self.messages[record.entity_uuid].update(copy.deepcopy(record.payload))

    def _delete_message(self, record: OperationRecord) -> None:
        if MESSAGE_DELETE_BODY_FIELDS.intersection(record.payload):
            raise InvalidJournalRecord("Message tombstones must not retain body data")
        message = self.messages.pop(record.entity_uuid, None)
        tombstone = copy.deepcopy(record.payload)
        if message is not None:
            for name in ("source_name", "source"):
                if name not in tombstone:
                    tombstone[name] = copy.deepcopy(message[name])
        tombstone.update(
            {
                "uuid": str(record.entity_uuid),
                "operation_uuid": str(record.operation_uuid),
                "deleted_at": _datetime_text(record.occurred_at),
            }
        )
        self.message_tombstones[record.entity_uuid] = tombstone
        message_uuid = str(record.entity_uuid)
        self.reactions = _without_reference(
            self.reactions,
            "message_uuid",
            message_uuid,
        )
        self.message_states = {
            key: value
            for key, value in self.message_states.items()
            if key[1] != record.entity_uuid
        }

    def _update_message_state(self, record: OperationRecord) -> None:
        user_uuid = sys_uuid.UUID(record.payload["user_uuid"])
        message_uuid = sys_uuid.UUID(
            record.payload.get("message_uuid", str(record.entity_uuid))
        )
        if message_uuid not in self.messages:
            return
        key = (user_uuid, message_uuid)
        state = copy.deepcopy(
            self.message_states.get(
                key,
                {"read": False, "pinned": False, "starred": False},
            )
        )
        for name in ("read", "pinned", "starred"):
            if name in record.payload:
                state[name] = bool(record.payload[name])
        aliases = {
            "message.read": ("read", True),
            "message.unread": ("read", False),
            "message.pin": ("pinned", True),
            "message.unpin": ("pinned", False),
            "message.star": ("starred", True),
            "message.unstar": ("starred", False),
        }
        if record.operation in aliases:
            name, value = aliases[record.operation]
            state[name] = value
        self.message_states[key] = state

    def _update_folder_item_pin(self, record: OperationRecord) -> None:
        item = self.folder_items.get(record.entity_uuid)
        if item is not None:
            item["pinned"] = record.operation == "folder_item.pin"


def _without_reference(
    collection: dict[sys_uuid.UUID, dict[str, typing.Any]],
    name: str,
    value: str,
) -> dict[sys_uuid.UUID, dict[str, typing.Any]]:
    return {key: item for key, item in collection.items() if item.get(name) != value}


class MessengerMailRepository:
    def __init__(
        self,
        imap_client: protocol.ImapClient,
        project_uuid: sys_uuid.UUID,
        state_mailbox: str = STATE_MAILBOX,
        event_mailbox_prefix: str = EVENT_MAILBOX_PREFIX,
    ) -> None:
        self.imap_client = imap_client
        self.project_uuid = project_uuid
        self.state_mailbox = state_mailbox
        self.event_mailbox_prefix = event_mailbox_prefix
        self.projection = Projection()
        self.event_positions: dict[
            tuple[sys_uuid.UUID, sys_uuid.UUID], protocol.AppendUid
        ] = {}

    def event_mailbox(self, user_uuid: sys_uuid.UUID) -> str:
        return f"{self.event_mailbox_prefix}/{user_uuid}"

    def append_operation(self, record: OperationRecord) -> protocol.AppendUid:
        self._check_project(record.project_uuid)
        previous = self.projection.operation_positions.get(record.operation_uuid)
        if previous is not None:
            return previous
        if record.operation == "message.create":
            previous = self.projection.message_positions.get(record.entity_uuid)
            if previous is not None:
                return previous
        elif record.operation == "message.delete":
            message = self.projection.messages.get(record.entity_uuid)
            if message is not None:
                payload = copy.deepcopy(record.payload)
                payload.setdefault("source_name", message["source_name"])
                payload.setdefault("source", copy.deepcopy(message["source"]))
                record = dataclasses.replace(record, payload=payload)
        self.imap_client.ensure_mailbox(self.state_mailbox)
        position = self.imap_client.append(
            self.state_mailbox,
            encode_operation(record),
            keywords=(OPERATION_KEYWORD,),
        )
        if position is None:
            raise MissingAppendUid("Operation APPEND completed without APPENDUID")
        self.projection.apply(record, position)
        self.projection.journal_uids.add(position.uid)
        self.projection.uid_validity = position.uid_validity
        self.projection.uid_next = position.uid + 1
        return position

    def rebuild(self) -> Projection:
        replay = self.read_operations()
        metadata = replay.metadata
        if metadata.uid_validity is None:
            raise UidValidityChanged("State mailbox has no UIDVALIDITY")
        projection = Projection()
        projection.uid_validity = metadata.uid_validity
        projection.uid_next = metadata.uid_next
        projection.highest_modseq = metadata.highest_modseq
        for entry in replay.entries:
            projection.apply(entry.record, entry.position)
            projection.journal_uids.add(entry.position.uid)
        self.projection = projection
        return projection

    def refresh(self) -> Projection:
        """Incrementally refresh the in-memory canonical projection.

        Journal message bodies are immutable.  A cheap UID search is enough to
        distinguish append-only progress from a destructive journal change.  New
        bodies are fetched once; UIDVALIDITY changes or expunged known UIDs force
        a full rebuild so ACL decisions never use a stale membership snapshot.
        """
        projection = self.projection
        if projection.uid_validity is None:
            return self.rebuild()

        self.imap_client.ensure_mailbox(self.state_mailbox)
        metadata = self.imap_client.select(self.state_mailbox)
        if metadata.uid_validity is None:
            raise UidValidityChanged("State mailbox has no UIDVALIDITY")
        journal_uids = set(self.imap_client.search("ALL"))
        if (
            metadata.uid_validity != projection.uid_validity
            or not projection.journal_uids.issubset(journal_uids)
        ):
            return self.rebuild()

        new_uids = sorted(journal_uids - projection.journal_uids)
        for entry in self._entries(metadata, new_uids):
            projection.apply(entry.record, entry.position)
            projection.journal_uids.add(entry.position.uid)
        projection.uid_next = metadata.uid_next
        projection.highest_modseq = metadata.highest_modseq
        return projection

    def read_operations(self, after_uid: int = 0) -> JournalReplay:
        self.imap_client.ensure_mailbox(self.state_mailbox)
        metadata = self.imap_client.select(self.state_mailbox)
        if metadata.uid_validity is None:
            raise UidValidityChanged("State mailbox has no UIDVALIDITY")
        criteria = "ALL" if after_uid == 0 else f"UID {after_uid + 1}:*"
        uids = self.imap_client.search(criteria)
        return JournalReplay(metadata, self._entries(metadata, uids))

    def _entries(
        self,
        metadata: protocol.MailboxMetadata,
        uids: typing.Iterable[int],
    ) -> tuple[JournalEntry, ...]:
        uid_validity = metadata.uid_validity
        if uid_validity is None:
            raise UidValidityChanged("State mailbox has no UIDVALIDITY")
        entries = []
        for message in sorted(
            self.imap_client.fetch(list(uids)), key=lambda item: item.uid
        ):
            record = decode_operation(message.raw_message)
            self._check_project(record.project_uuid)
            entries.append(
                JournalEntry(
                    protocol.AppendUid(uid_validity, message.uid),
                    message.flags,
                    record,
                )
            )
        return tuple(entries)

    def append_event(self, record: EventRecord) -> EpochEvent:
        self._check_project(record.project_uuid)
        key = (record.user_uuid, record.event_uuid)
        position = self.event_positions.get(key)
        if position is None:
            self.imap_client.ensure_mailbox(self.event_mailbox(record.user_uuid))
            position = self.imap_client.append(
                self.event_mailbox(record.user_uuid),
                encode_event(record),
                keywords=(EVENT_KEYWORD,),
            )
            if position is None:
                raise MissingAppendUid("Event APPEND completed without APPENDUID")
            self.event_positions[key] = position
        return EpochEvent(
            EpochCursor(position.uid_validity, position.uid),
            record,
        )

    def events_after(
        self,
        user_uuid: sys_uuid.UUID,
        cursor: EpochCursor,
        limit: int | None = None,
    ) -> list[EpochEvent]:
        self.imap_client.ensure_mailbox(self.event_mailbox(user_uuid))
        metadata = self.imap_client.select(self.event_mailbox(user_uuid))
        if metadata.uid_validity != cursor.uid_validity:
            raise UidValidityChanged("Event mailbox UIDVALIDITY changed")
        uids = self.imap_client.search(f"UID {cursor.epoch_version + 1}:*")
        if limit is not None:
            uids = sorted(uids)[:limit]
        events = []
        for message in sorted(self.imap_client.fetch(uids), key=lambda item: item.uid):
            record = decode_event(message.raw_message)
            self._check_project(record.project_uuid)
            if record.user_uuid != user_uuid:
                raise InvalidJournalRecord("Event belongs to another user")
            position = protocol.AppendUid(cursor.uid_validity, message.uid)
            self.event_positions[(user_uuid, record.event_uuid)] = position
            events.append(
                EpochEvent(
                    EpochCursor(cursor.uid_validity, message.uid),
                    record,
                )
            )
        return events

    def event_cursor_state(self, user_uuid: sys_uuid.UUID) -> EventCursorState:
        self.imap_client.ensure_mailbox(self.event_mailbox(user_uuid))
        metadata = self.imap_client.select(self.event_mailbox(user_uuid))
        if metadata.uid_validity is None:
            raise UidValidityChanged("Event mailbox has no UIDVALIDITY")
        current_epoch_version = max(0, (metadata.uid_next or 1) - 1)
        uids = self.imap_client.search("ALL")
        minimum_epoch_version = min(uids) if uids else current_epoch_version + 1
        return EventCursorState(
            epoch_generation=str(metadata.uid_validity),
            current_epoch_version=current_epoch_version,
            minimum_epoch_version=minimum_epoch_version,
        )

    def prune_events(
        self,
        user_uuid: sys_uuid.UUID,
        *,
        now: datetime.datetime | None = None,
    ) -> tuple[int, ...]:
        """Delete only event-journal records older than the seven-day policy."""
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - EVENT_RETENTION
        path = self.event_mailbox(user_uuid)
        self.imap_client.ensure_mailbox(path)
        self.imap_client.select(path)
        expired_uids = []
        for message in sorted(
            self.imap_client.fetch(self.imap_client.search("ALL")),
            key=lambda value: value.uid,
        ):
            event = decode_event(message.raw_message)
            self._check_project(event.project_uuid)
            if event.user_uuid != user_uuid:
                raise InvalidJournalRecord("Event belongs to another user")
            if event.occurred_at >= cutoff:
                break
            expired_uids.append(message.uid)
        self.imap_client.delete_uids(path, expired_uids)
        expired = set(expired_uids)
        self.event_positions = {
            key: value
            for key, value in self.event_positions.items()
            if value.uid not in expired
        }
        return tuple(expired_uids)

    def current_epoch(self, user_uuid: sys_uuid.UUID) -> EpochCursor:
        state = self.event_cursor_state(user_uuid)
        return EpochCursor(
            int(state.epoch_generation),
            state.current_epoch_version,
        )

    def _check_project(self, project_uuid: sys_uuid.UUID) -> None:
        if project_uuid != self.project_uuid:
            raise InvalidJournalRecord("Journal record belongs to another project")
