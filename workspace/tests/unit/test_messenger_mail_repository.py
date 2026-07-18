# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import collections
import concurrent.futures
import contextlib
import dataclasses
import datetime
import re
import types
import uuid as sys_uuid

import pytest

from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository
from workspace.messenger_api.api import sql_store
from workspace.messenger_api.api import store as api_store


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
ACTOR_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
OTHER_USER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")
STREAM_UUID = sys_uuid.UUID("40000000-0000-0000-0000-000000000004")
BINDING_UUID = sys_uuid.UUID("50000000-0000-0000-0000-000000000005")
TOPIC_UUID = sys_uuid.UUID("60000000-0000-0000-0000-000000000006")
MESSAGE_UUID = sys_uuid.UUID("70000000-0000-0000-0000-000000000007")
REACTION_UUID = sys_uuid.UUID("80000000-0000-0000-0000-000000000008")
FOLDER_UUID = sys_uuid.UUID("90000000-0000-0000-0000-000000000009")
FOLDER_ITEM_UUID = sys_uuid.UUID("a0000000-0000-0000-0000-00000000000a")
FILE_UUID = sys_uuid.UUID("b0000000-0000-0000-0000-00000000000b")
NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)


def _operation(
    name,
    entity_uuid,
    payload,
    operation_uuid=None,
    second=0,
):
    return repository.OperationRecord(
        project_uuid=PROJECT_UUID,
        operation_uuid=operation_uuid or sys_uuid.uuid4(),
        actor_uuid=ACTOR_UUID,
        operation=name,
        entity_uuid=entity_uuid,
        payload=payload,
        occurred_at=NOW + datetime.timedelta(seconds=second),
    )


def _event(event_uuid, action, second=0):
    return repository.EventRecord(
        project_uuid=PROJECT_UUID,
        event_uuid=event_uuid,
        operation_uuid=sys_uuid.uuid4(),
        user_uuid=OTHER_USER_UUID,
        object_type="message",
        action=action,
        payload={
            "kind": f"message.{action}",
            "uuid": str(MESSAGE_UUID),
        },
        occurred_at=NOW + datetime.timedelta(seconds=second),
    )


class FakeImapClient:
    def __init__(self):
        self.mailboxes = collections.defaultdict(list)
        self.uid_validities = collections.defaultdict(lambda: 321)
        self.next_uids = collections.defaultdict(lambda: 1)
        self.current_mailbox = None
        self.append_calls = []
        self.ensure_calls = []
        self.select_calls = []
        self.search_calls = []
        self.fetch_calls = []

    def ensure_mailbox(self, path):
        self.ensure_calls.append(path)
        self.mailboxes[path]
        return len(self.mailboxes[path]) == 0

    def append(self, path, raw_message, flags=(), keywords=()):
        uid = self.next_uids[path]
        self.next_uids[path] += 1
        self.mailboxes[path].append(
            protocol.FetchedMessage(uid, frozenset(flags + keywords), raw_message)
        )
        self.append_calls.append((path, raw_message, flags, keywords))
        return protocol.AppendUid(self.uid_validities[path], uid)

    def select(self, path, readonly=True):
        assert readonly is True
        self.select_calls.append(path)
        self.current_mailbox = path
        return protocol.MailboxMetadata(
            self.uid_validities[path],
            self.next_uids[path],
            len(self.mailboxes[path]),
        )

    def search(self, criteria="ALL"):
        self.search_calls.append((self.current_mailbox, criteria))
        messages = self.mailboxes[self.current_mailbox]
        if criteria == "ALL":
            return [message.uid for message in messages]
        match = re.fullmatch(r"UID (\d+):\*", criteria)
        assert match is not None
        start_uid = int(match.group(1))
        return [message.uid for message in messages if message.uid >= start_uid]

    def fetch(self, uids):
        self.fetch_calls.append((self.current_mailbox, tuple(uids)))
        requested = set(uids)
        return [
            message
            for message in self.mailboxes[self.current_mailbox]
            if message.uid in requested
        ]

    def delete_uids(self, path, uids):
        expired = set(uids)
        self.mailboxes[path] = [
            message for message in self.mailboxes[path] if message.uid not in expired
        ]


class FakeRuntimeFactory:
    def __init__(self, imap):
        self.imap = imap

    @contextlib.contextmanager
    def messenger_service(self, project_uuid):
        mail_repository = repository.MessengerMailRepository(
            self.imap,
            project_uuid,
        )
        yield types.SimpleNamespace(repository=mail_repository)


def test_operation_and_event_json_codec_round_trip_utf8_payloads():
    operation = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(STREAM_UUID),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(ACTOR_UUID),
            "payload": {
                "kind": "markdown",
                "content": "Привет [file](urn:file:a1000000-0000-0000-0000-00000000000a)",
            },
        },
    )
    event = _event(sys_uuid.uuid4(), "created")

    assert (
        repository.decode_operation(repository.encode_operation(operation)) == operation
    )
    assert repository.decode_event(repository.encode_event(event)) == event


@pytest.mark.parametrize(
    ("record", "encoder", "decoder", "uuid_field", "domain"),
    (
        (
            _operation("message.delete", MESSAGE_UUID, {}),
            repository.encode_operation,
            repository.decode_operation,
            "operation_uuid",
            repository.JOURNAL_MESSAGE_ID_DOMAIN,
        ),
        (
            _event(sys_uuid.uuid4(), "created"),
            repository.encode_event,
            repository.decode_event,
            "event_uuid",
            repository.EVENT_MESSAGE_ID_DOMAIN,
        ),
    ),
)
def test_message_id_codec_accepts_legacy_folding_but_rejects_tampering(
    record,
    encoder,
    decoder,
    uuid_field,
    domain,
):
    record_uuid = getattr(record, uuid_field)
    expected_message_id = f"<{record_uuid}@{domain}>"
    message_id_header = f"Message-ID: {expected_message_id}\r\n".encode()
    raw_message = encoder(record)

    assert message_id_header in raw_message
    folded_message = raw_message.replace(
        message_id_header,
        f"Message-ID:\r\n {expected_message_id}\r\n".encode(),
        1,
    )
    assert decoder(folded_message) == record

    tampered_message = raw_message.replace(
        expected_message_id.encode(),
        f"<{sys_uuid.UUID(int=0)}@{domain}>".encode(),
        1,
    )
    with pytest.raises(repository.InvalidJournalRecord, match="Message-ID"):
        decoder(tampered_message)


def test_deterministic_dm_uuid_is_order_independent_and_project_scoped():
    first = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        ACTOR_UUID,
        OTHER_USER_UUID,
    )
    reverse = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        OTHER_USER_UUID,
        ACTOR_UUID,
    )
    another_project = repository.deterministic_dm_uuid(
        sys_uuid.UUID("f0000000-0000-0000-0000-00000000000f"),
        ACTOR_UUID,
        OTHER_USER_UUID,
    )

    assert first == reverse
    assert first != another_project
    assert first.version == 5
    with pytest.raises(ValueError, match="two distinct IAM users"):
        repository.deterministic_dm_uuid(PROJECT_UUID, ACTOR_UUID, ACTOR_UUID)


def test_projection_covers_messenger_entities_roles_states_and_idempotency():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    records = [
        _operation(
            "stream.create",
            STREAM_UUID,
            {"name": "Engineering", "chat_type": "stream"},
            second=1,
        ),
        _operation(
            "stream_binding.create",
            BINDING_UUID,
            {
                "stream_uuid": str(STREAM_UUID),
                "user_uuid": str(OTHER_USER_UUID),
                "role": "moderator",
            },
            second=2,
        ),
        _operation(
            "topic.create",
            TOPIC_UUID,
            {"stream_uuid": str(STREAM_UUID), "name": "Deployments"},
            second=3,
        ),
        _operation(
            "message.create",
            MESSAGE_UUID,
            {
                "stream_uuid": str(STREAM_UUID),
                "topic_uuid": str(TOPIC_UUID),
                "author_uuid": str(ACTOR_UUID),
                "payload": {"kind": "markdown", "content": "original"},
            },
            second=4,
        ),
        _operation(
            "message.update",
            MESSAGE_UUID,
            {"payload": {"kind": "markdown", "content": "edited"}},
            second=5,
        ),
        _operation(
            "reaction.create",
            REACTION_UUID,
            {
                "message_uuid": str(MESSAGE_UUID),
                "user_uuid": str(OTHER_USER_UUID),
                "emoji_name": "thumbs_up",
            },
            second=6,
        ),
        _operation(
            "message.state",
            MESSAGE_UUID,
            {
                "user_uuid": str(OTHER_USER_UUID),
                "read": True,
                "pinned": True,
                "starred": True,
            },
            second=7,
        ),
        _operation(
            "folder.create",
            FOLDER_UUID,
            {"user_uuid": str(OTHER_USER_UUID), "name": "Important"},
            second=8,
        ),
        _operation(
            "folder_item.create",
            FOLDER_ITEM_UUID,
            {
                "folder_uuid": str(FOLDER_UUID),
                "stream_uuid": str(STREAM_UUID),
                "pinned": False,
            },
            second=9,
        ),
        _operation(
            "folder_item.pin",
            FOLDER_ITEM_UUID,
            {"user_uuid": str(OTHER_USER_UUID)},
            second=10,
        ),
    ]
    for record in records:
        mail_repository.append_operation(record)

    projection = mail_repository.projection
    assert projection.streams[STREAM_UUID]["name"] == "Engineering"
    assert projection.bindings[BINDING_UUID]["role"] == "moderator"
    assert projection.topics[TOPIC_UUID]["name"] == "Deployments"
    assert projection.messages[MESSAGE_UUID]["payload"]["content"] == "edited"
    assert projection.messages[MESSAGE_UUID]["created_at"] == records[3].occurred_at
    assert projection.reactions[REACTION_UUID]["emoji_name"] == "thumbs_up"
    assert projection.message_states[(OTHER_USER_UUID, MESSAGE_UUID)] == {
        "read": True,
        "pinned": True,
        "starred": True,
    }
    assert projection.folders[FOLDER_UUID]["name"] == "Important"
    assert projection.folder_items[FOLDER_ITEM_UUID]["pinned"] is True

    call_count = len(imap.append_calls)
    duplicate_operation = dataclasses.replace(
        records[0],
        payload={"name": "must not overwrite"},
    )
    assert (
        mail_repository.append_operation(duplicate_operation)
        == (projection.operation_positions[records[0].operation_uuid])
    )
    duplicate_message = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(STREAM_UUID),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(OTHER_USER_UUID),
            "payload": {"kind": "markdown", "content": "duplicate"},
        },
        second=11,
    )
    mail_repository.append_operation(duplicate_message)

    assert len(imap.append_calls) == call_count
    assert projection.streams[STREAM_UUID]["name"] == "Engineering"
    assert projection.messages[MESSAGE_UUID]["payload"]["content"] == "edited"
    rebuilt = mail_repository.rebuild()
    assert rebuilt.messages[MESSAGE_UUID]["created_at"] == records[3].occurred_at


def test_message_delete_is_bodyless_hard_tombstone_and_rebuilds_from_imap():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    create = _operation(
        "message.create",
        MESSAGE_UUID,
        {
            "stream_uuid": str(STREAM_UUID),
            "topic_uuid": str(TOPIC_UUID),
            "author_uuid": str(ACTOR_UUID),
            "payload": {"kind": "markdown", "content": "secret body"},
        },
        second=1,
    )
    reaction = _operation(
        "reaction.create",
        REACTION_UUID,
        {
            "message_uuid": str(MESSAGE_UUID),
            "user_uuid": str(OTHER_USER_UUID),
            "emoji_name": "eyes",
        },
        second=2,
    )
    state = _operation(
        "message.state",
        MESSAGE_UUID,
        {"user_uuid": str(OTHER_USER_UUID), "starred": True},
        second=3,
    )
    delete = _operation("message.delete", MESSAGE_UUID, {}, second=4)
    for record in (create, reaction, state, delete):
        mail_repository.append_operation(record)

    projection = mail_repository.projection
    assert MESSAGE_UUID not in projection.messages
    assert REACTION_UUID not in projection.reactions
    assert (OTHER_USER_UUID, MESSAGE_UUID) not in projection.message_states
    assert projection.message_tombstones[MESSAGE_UUID] == {
        "uuid": str(MESSAGE_UUID),
        "operation_uuid": str(delete.operation_uuid),
        "deleted_at": "2026-07-15T12:00:04Z",
        "source_name": "native",
        "source": {"kind": "native"},
    }
    raw_tombstone = imap.mailboxes[repository.STATE_MAILBOX][-1].raw_message
    assert repository.decode_operation(raw_tombstone).payload == {
        "source_name": "native",
        "source": {"kind": "native"},
    }

    rebuilt_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    rebuilt = rebuilt_repository.rebuild()
    assert MESSAGE_UUID not in rebuilt.messages
    assert rebuilt.message_tombstones == projection.message_tombstones
    assert rebuilt.uid_validity == 321
    assert rebuilt.uid_next == 5
    assert rebuilt.highest_modseq == 4
    replay = rebuilt_repository.read_operations(after_uid=1)
    assert replay.metadata.uid_validity == 321
    assert [entry.position.uid for entry in replay.entries] == [2, 3, 4]
    assert [entry.record.operation for entry in replay.entries] == [
        "reaction.create",
        "message.state",
        "message.delete",
    ]
    assert replay.entries[-1].record.payload == {
        "source_name": "native",
        "source": {"kind": "native"},
    }

    with pytest.raises(
        repository.InvalidJournalRecord,
        match="must not retain body data",
    ):
        _operation(
            "message.delete",
            MESSAGE_UUID,
            {"payload": {"kind": "markdown", "content": "secret body"}},
        )


def test_events_use_append_uid_as_epoch_and_strict_uidvalidity_cursor():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    assert mail_repository.event_mailbox(OTHER_USER_UUID) == (
        f"Workspace/Events/{OTHER_USER_UUID}"
    )
    first_record = _event(sys_uuid.uuid4(), "created", second=1)
    second_record = _event(sys_uuid.uuid4(), "updated", second=2)

    first = mail_repository.append_event(first_record)
    second = mail_repository.append_event(second_record)
    duplicate = mail_repository.append_event(first_record)

    assert first.cursor == repository.EpochCursor(321, 1)
    assert second.cursor == repository.EpochCursor(321, 2)
    assert duplicate.cursor == first.cursor
    assert len(imap.mailboxes[mail_repository.event_mailbox(OTHER_USER_UUID)]) == 2
    events = mail_repository.events_after(
        OTHER_USER_UUID,
        repository.EpochCursor(321, 0),
    )
    assert [event.cursor.epoch_version for event in events] == [1, 2]
    assert events[0].as_dict()["epoch_version"] == 1
    assert events[0].as_dict()["payload"]["kind"] == "message.created"
    assert mail_repository.events_after(OTHER_USER_UUID, events[0].cursor) == [
        events[1]
    ]
    assert mail_repository.current_epoch(OTHER_USER_UUID) == (
        repository.EpochCursor(321, 2)
    )

    with pytest.raises(repository.UidValidityChanged, match="UIDVALIDITY changed"):
        mail_repository.events_after(
            OTHER_USER_UUID,
            repository.EpochCursor(999, 0),
        )


def test_events_after_limits_imap_fetch_before_materializing_catchup():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    records = [
        _event(sys_uuid.uuid4(), "created", second=second) for second in range(1, 11)
    ]
    for record in records:
        mail_repository.append_event(record)

    fetched_uids = []
    original_fetch = imap.fetch

    def recording_fetch(uids):
        fetched_uids.extend(uids)
        return original_fetch(uids)

    imap.fetch = recording_fetch
    result = mail_repository.events_after(
        OTHER_USER_UUID,
        repository.EpochCursor(321, 0),
        limit=3,
    )

    assert fetched_uids == [1, 2, 3]
    assert [event.cursor.epoch_version for event in result] == [1, 2, 3]


def test_file_operations_rebuild_metadata_without_storing_object_bytes():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    create = _operation(
        "file.create",
        FILE_UUID,
        {
            "stream_uuid": str(STREAM_UUID),
            "user_uuid": str(ACTOR_UUID),
            "object_key": f"files/{FILE_UUID}",
            "name": "report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4096,
            "hash": "sha256:abc",
            "deleted": False,
        },
        second=1,
    )
    update = _operation(
        "file.update",
        FILE_UUID,
        {"description": "Final report", "size_bytes": 4100},
        second=2,
    )
    mail_repository.append_operation(create)
    mail_repository.append_operation(update)

    assert mail_repository.projection.files[FILE_UUID]["object_key"] == (
        f"files/{FILE_UUID}"
    )
    assert mail_repository.projection.files[FILE_UUID]["description"] == (
        "Final report"
    )
    rebuilt_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    assert rebuilt_repository.rebuild().files == mail_repository.projection.files

    delete = _operation(
        "file.delete",
        FILE_UUID,
        {"deleted": True},
        second=3,
    )
    mail_repository.append_operation(delete)
    assert FILE_UUID not in mail_repository.projection.files
    assert FILE_UUID not in rebuilt_repository.rebuild().files
    replay = rebuilt_repository.read_operations(after_uid=2)
    assert [entry.record.operation for entry in replay.entries] == ["file.delete"]

    with pytest.raises(
        repository.InvalidJournalRecord,
        match="store metadata only",
    ):
        _operation(
            "file.create",
            FILE_UUID,
            {"name": "forbidden.bin", "data": b"binary payload"},
        )


def test_event_retention_prunes_only_the_expired_contiguous_uid_prefix():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    now = NOW + datetime.timedelta(days=8)
    expired_uuid = sys_uuid.uuid4()
    boundary_uuid = sys_uuid.uuid4()
    backdated_uuid = sys_uuid.uuid4()
    mail_repository.append_event(_event(expired_uuid, "created", second=0))
    mail_repository.append_event(_event(boundary_uuid, "updated", second=24 * 60 * 60))
    mail_repository.append_event(_event(backdated_uuid, "deleted", second=1))

    deleted_uids = mail_repository.prune_events(OTHER_USER_UUID, now=now)
    event_path = mail_repository.event_mailbox(OTHER_USER_UUID)

    assert deleted_uids == (1,)
    assert [
        repository.decode_event(message.raw_message).event_uuid
        for message in imap.mailboxes[event_path]
    ] == [boundary_uuid, backdated_uuid]
    assert imap.mailboxes[repository.STATE_MAILBOX] == []
    assert mail_repository.event_cursor_state(OTHER_USER_UUID) == (
        repository.EventCursorState("321", 3, 2)
    )


def test_fresh_project_account_creates_empty_state_mailbox_before_replay():
    imap = FakeImapClient()
    mail_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)

    projection = mail_repository.rebuild()

    assert projection.streams == {}
    assert projection.uid_validity == 321
    assert projection.uid_next == 1
    assert imap.ensure_calls == [repository.STATE_MAILBOX]

    mail_repository.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "Fresh project"},
        )
    )
    assert STREAM_UUID in mail_repository.projection.streams
    assert imap.ensure_calls == [
        repository.STATE_MAILBOX,
        repository.STATE_MAILBOX,
    ]


def test_incremental_refresh_fetches_only_new_bodies_and_detects_expunge():
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.rebuild()
    first = _operation(
        "stream.create",
        STREAM_UUID,
        {"kind": "stream", "name": "Initial"},
    )
    writer.append_operation(first)

    reader = repository.MessengerMailRepository(imap, PROJECT_UUID)
    reader.rebuild()
    initial_fetch = imap.fetch_calls[-1]
    reader.refresh()
    no_change_fetch = imap.fetch_calls[-1]

    second = _operation(
        "stream.update",
        STREAM_UUID,
        {"name": "Incremental"},
        second=1,
    )
    writer.append_operation(second)
    reader.refresh()
    incremental_fetch = imap.fetch_calls[-1]

    assert initial_fetch == (repository.STATE_MAILBOX, (1,))
    assert no_change_fetch == (repository.STATE_MAILBOX, ())
    assert incremental_fetch == (repository.STATE_MAILBOX, (2,))
    assert reader.projection.streams[STREAM_UUID]["name"] == "Incremental"

    imap.mailboxes[repository.STATE_MAILBOX] = [
        message
        for message in imap.mailboxes[repository.STATE_MAILBOX]
        if message.uid != 1
    ]
    reader.refresh()

    assert imap.fetch_calls[-1] == (repository.STATE_MAILBOX, (2,))
    assert reader.projection.journal_uids == {2}


def test_store_factory_bootstrap_is_single_flight_and_ws_poll_is_incremental(
    monkeypatch,
):
    monkeypatch.setattr(
        api_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: object()),
    )
    monkeypatch.setattr(
        api_store.writer_gate,
        "acknowledge",
        lambda *args, **kwargs: None,
    )
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.rebuild()
    writer.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "Canonical"},
        )
    )
    event_uuid = sys_uuid.uuid4()
    writer.append_event(_event(event_uuid, "created"))
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    factory = sql_store.SQLProjectedMessengerStoreFactory(FakeRuntimeFactory(imap))

    with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
        first = store.events_after(
            {"epoch_version": sql_store.dm_filters.GT(0)},
        )
        epoch_generation = store.event_cursor()["epoch_generation"]
    with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
        second = store.events_after(
            {"epoch_version": sql_store.dm_filters.GT(first[-1]["epoch_version"])},
            epoch_generation=epoch_generation,
        )

    state_fetches = [
        uids
        for mailbox, uids in imap.fetch_calls
        if mailbox == repository.STATE_MAILBOX
    ]
    event_fetches = [
        uids
        for mailbox, uids in imap.fetch_calls
        if mailbox == writer.event_mailbox(OTHER_USER_UUID)
    ]
    assert state_fetches == [(), (1,), ()]
    assert event_fetches == [(1,), ()]
    assert [event["uuid"] for event in first] == [str(event_uuid)]
    assert second == []


def test_event_store_never_reads_project_state_mailbox():
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "Must remain untouched"},
        )
    )
    event_uuid = sys_uuid.uuid4()
    writer.append_event(_event(event_uuid, "created"))
    imap.ensure_calls.clear()
    imap.select_calls.clear()
    imap.search_calls.clear()
    imap.fetch_calls.clear()
    factory = sql_store.SQLProjectedMessengerStoreFactory(FakeRuntimeFactory(imap))

    with factory.event_store(PROJECT_UUID, OTHER_USER_UUID) as store:
        first = store.events_after(
            {"epoch_version": sql_store.dm_filters.GT(0)},
        )
        cursor = store.event_cursor()
    with factory.event_store(PROJECT_UUID, OTHER_USER_UUID) as store:
        second = store.events_after(
            {"epoch_version": sql_store.dm_filters.GT(first[-1]["epoch_version"])},
            epoch_generation=cursor["epoch_generation"],
        )

    event_mailbox = writer.event_mailbox(OTHER_USER_UUID)
    assert [event["uuid"] for event in first] == [str(event_uuid)]
    assert second == []
    assert repository.STATE_MAILBOX not in imap.ensure_calls
    assert repository.STATE_MAILBOX not in imap.select_calls
    assert all(
        mailbox != repository.STATE_MAILBOX for mailbox, _criteria in imap.search_calls
    )
    assert all(
        mailbox != repository.STATE_MAILBOX for mailbox, _uids in imap.fetch_calls
    )
    assert event_mailbox in imap.select_calls


def test_store_factory_concurrent_bootstrap_fetches_journal_bodies_once(monkeypatch):
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.rebuild()
    writer.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "Single flight"},
        )
    )
    imap.fetch_calls.clear()
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    factory = sql_store.SQLProjectedMessengerStoreFactory(FakeRuntimeFactory(imap))

    def open_store():
        with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
            return store.mail_service.repository.projection.streams[STREAM_UUID]["name"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: open_store(), range(8)))

    state_fetches = [
        uids
        for mailbox, uids in imap.fetch_calls
        if mailbox == repository.STATE_MAILBOX
    ]
    assert results == ["Single flight"] * 8
    assert state_fetches.count((1,)) == 1
    assert state_fetches.count(()) == 7


def test_store_factory_refreshes_acl_after_external_canonical_change(monkeypatch):
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.rebuild()
    writer.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "ACL"},
        )
    )
    writer.append_operation(
        _operation(
            "binding.create",
            BINDING_UUID,
            {
                "stream_uuid": str(STREAM_UUID),
                "user_uuid": str(OTHER_USER_UUID),
                "role": "member",
            },
            second=1,
        )
    )
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    factory = sql_store.SQLProjectedMessengerStoreFactory(FakeRuntimeFactory(imap))
    file = types.SimpleNamespace(
        user_uuid=ACTOR_UUID,
        stream_uuid=STREAM_UUID,
    )

    with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
        assert store._can_read_file(file) is True

    writer.append_operation(
        _operation(
            "binding.delete",
            BINDING_UUID,
            {},
            second=2,
        )
    )
    with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
        assert store._can_read_file(file) is False

    state_fetches = [
        uids
        for mailbox, uids in imap.fetch_calls
        if mailbox == repository.STATE_MAILBOX
    ]
    assert state_fetches[-1] == (3,)


def test_store_factory_invalidates_projection_after_failed_request(monkeypatch):
    imap = FakeImapClient()
    writer = repository.MessengerMailRepository(imap, PROJECT_UUID)
    writer.rebuild()
    writer.append_operation(
        _operation(
            "stream.create",
            STREAM_UUID,
            {"kind": "stream", "name": "Retry from canonical"},
        )
    )
    imap.fetch_calls.clear()
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    factory = sql_store.SQLProjectedMessengerStoreFactory(FakeRuntimeFactory(imap))

    with pytest.raises(RuntimeError, match="projection failed"):
        with factory(PROJECT_UUID, OTHER_USER_UUID):
            raise RuntimeError("projection failed")
    with factory(PROJECT_UUID, OTHER_USER_UUID) as store:
        assert (
            store.mail_service.repository.projection.streams[STREAM_UUID]["name"]
            == "Retry from canonical"
        )

    state_fetches = [
        uids
        for mailbox, uids in imap.fetch_calls
        if mailbox == repository.STATE_MAILBOX
    ]
    assert state_fetches == [(1,), (1,)]


def test_event_cache_incrementally_observes_another_process_before_sync(monkeypatch):
    imap = FakeImapClient()
    shared_event_states = {}
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )

    first_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    first_store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        OTHER_USER_UUID,
        types.SimpleNamespace(repository=first_repository),
        canonical_event_states=shared_event_states,
    )
    assert first_store._known_event_uuids(OTHER_USER_UUID) == set()

    event_uuid = sys_uuid.uuid4()
    second_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    second_repository.append_event(_event(event_uuid, "created"))
    append_count = len(imap.append_calls)

    retry_repository = repository.MessengerMailRepository(imap, PROJECT_UUID)
    retry_store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        OTHER_USER_UUID,
        types.SimpleNamespace(repository=retry_repository),
        canonical_event_states=shared_event_states,
    )
    row = types.SimpleNamespace(
        uuid=event_uuid,
        user_uuid=OTHER_USER_UUID,
        epoch_version=1,
        object_type="message",
        action="created",
        payload={"kind": "message.created"},
        created_at=NOW,
        schema_version=repository.SCHEMA_VERSION,
    )
    monkeypatch.setattr(
        sql_store.models,
        "WorkspaceEvent",
        types.SimpleNamespace(
            objects=types.SimpleNamespace(get_all=lambda **kwargs: [row])
        ),
    )

    retry_store._sync_projection_events()

    assert event_uuid in retry_store._known_event_uuids(OTHER_USER_UUID)
    assert len(imap.append_calls) == append_count
    assert imap.fetch_calls[-1] == (
        retry_repository.event_mailbox(OTHER_USER_UUID),
        (),
    )
