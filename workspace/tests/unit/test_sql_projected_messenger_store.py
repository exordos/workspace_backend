# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import types
import uuid as sys_uuid

import pytest

from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exceptions

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import sql_store
from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
PEER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")


@pytest.fixture(autouse=True)
def isolate_projection_event_sync(monkeypatch):
    """These canonical-ordering tests do not configure SQL event storage."""
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_latest_projection_event_epoch",
        lambda self: 0,
    )
    monkeypatch.setattr(
        sql_store.SQLProjectedMessengerStore,
        "_sync_projection_events",
        lambda self: None,
    )


class FakeRepository:
    def __init__(self, order):
        self.order = order
        self.projection = repository.Projection()
        self.next_uid = 1
        self.entries = []

    def rebuild(self):
        return self.projection

    def append_operation(self, record):
        self.order.append(("mail", record.operation, record.entity_uuid))
        position = protocol.AppendUid(1, self.next_uid)
        self.next_uid += 1
        self.projection.apply(record, position)
        self.entries.append(
            repository.JournalEntry(position, frozenset(), record)
        )
        return position

    def read_operations(self, after_uid=0):
        return repository.JournalReplay(
            protocol.MailboxMetadata(1, self.next_uid, None),
            tuple(
                entry for entry in self.entries if entry.position.uid > after_uid
            ),
        )


class FakeMailService:
    def __init__(self, order):
        self.order = order
        self.repository = FakeRepository(order)

    def deliver_message(self, record):
        previous = self.repository.projection.message_positions.get(
            record.entity_uuid
        )
        if previous is not None:
            return previous
        self.order.append(("smtp", record.operation, record.entity_uuid))
        return self.repository.append_operation(record)

    def delete_message(self, record):
        self.order.append(("expunge", record.operation, record.entity_uuid))
        return self.repository.append_operation(record)

    def validate_stream_participants(self, stream_uuid, participants):
        stream = self.repository.projection.streams[stream_uuid]
        if stream.get("kind") != "direct":
            return
        if len(participants) != 2 or participants[0] == participants[1]:
            raise repository.InvalidJournalRecord(
                "Direct streams require exactly two distinct participants"
            )
        expected_uuid = repository.deterministic_dm_uuid(
            self.repository.project_uuid,
            participants[0],
            participants[1],
        )
        if stream_uuid != expected_uuid:
            raise repository.InvalidJournalRecord(
                "Direct stream UUID must be deterministic for its participants"
            )


def test_direct_stream_membership_and_topic_are_canonical_before_sql(monkeypatch):
    order = []
    service = FakeMailService(order)
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )

    def project_stream(**values):
        order.append(("sql", "stream.create", values["uuid"]))
        assert values["canonical_default_topic_uuid"] in (
            service.repository.projection.topics
        )
        for binding_uuid in values["canonical_binding_uuids"].values():
            assert binding_uuid in service.repository.projection.bindings
        return values

    monkeypatch.setattr(
        sql_store.helpers,
        "get_or_create_workspace_user_stream",
        project_stream,
    )

    stream_uuid = repository.deterministic_dm_uuid(
        PROJECT_UUID,
        USER_UUID,
        PEER_UUID,
    )
    result = store.create_resource(
        "streams",
        {
            "uuid": stream_uuid,
            "project_id": PROJECT_UUID,
            "user_uuid": USER_UUID,
            "name": "Direct chat",
            "description": "",
            "direct_user_uuid": PEER_UUID,
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )

    assert [item[1] for item in order] == [
        "stream.create",
        "binding.create",
        "binding.create",
        "topic.create",
        "stream.create",
    ]
    assert result["uuid"] == str(stream_uuid)
    assert service.repository.projection.streams[stream_uuid]["kind"] == "direct"
    assert len(service.repository.projection.bindings) == 2


def test_message_smtp_and_expunge_happen_before_sql_projection(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    service.repository.projection.streams[stream_uuid] = {
        "uuid": str(stream_uuid),
        "kind": "direct",
    }
    for participant in (USER_UUID, PEER_UUID):
        binding_uuid = sys_uuid.uuid4()
        service.repository.projection.bindings[binding_uuid] = {
            "uuid": str(binding_uuid),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(participant),
        }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )

    def project_create(**values):
        order.append(("sql", "message.create", values["uuid"]))
        assert (
            values["created_at"]
            == (service.repository.projection.messages[message_uuid]["created_at"])
        )
        return values

    def project_delete(project_id, user_uuid, uuid):
        del project_id, user_uuid
        order.append(("sql", "message.delete", uuid))

    monkeypatch.setattr(
        sql_store.helpers,
        "get_workspace_user_message",
        lambda **kwargs: (_ for _ in ()).throw(
            storage_exceptions.RecordNotFound(model="message", filters=kwargs)
        ),
    )
    monkeypatch.setattr(
        sql_store.helpers,
        "create_workspace_user_message",
        project_create,
    )
    monkeypatch.setattr(
        sql_store.helpers,
        "delete_workspace_user_message",
        project_delete,
    )

    store.create_message(
        {
            "uuid": message_uuid,
            "project_id": PROJECT_UUID,
            "user_uuid": USER_UUID,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "author_uuid": USER_UUID,
            "payload": {"kind": "markdown", "content": "hello"},
            "source_name": "native",
            "source": {"kind": "native"},
        }
    )
    store.delete_message(message_uuid)

    assert [item[0:2] for item in order] == [
        ("smtp", "message.create"),
        ("mail", "message.create"),
        ("sql", "message.create"),
        ("expunge", "message.delete"),
        ("mail", "message.delete"),
        ("sql", "message.delete"),
    ]


def test_message_retry_reuses_existing_sql_projection(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    service.repository.projection.streams[stream_uuid] = {
        "uuid": str(stream_uuid),
        "kind": "stream",
    }
    binding_uuid = sys_uuid.uuid4()
    service.repository.projection.bindings[binding_uuid] = {
        "uuid": str(binding_uuid),
        "stream_uuid": str(stream_uuid),
        "user_uuid": str(USER_UUID),
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )
    projected = None

    def get_projected_message(project_id, user_uuid, message_uuid):
        assert (project_id, user_uuid, message_uuid) == (
            PROJECT_UUID,
            USER_UUID,
            values["uuid"],
        )
        if projected is None:
            raise storage_exceptions.RecordNotFound(
                model="message",
                filters={"uuid": message_uuid},
            )
        return projected

    def project_create(**values):
        nonlocal projected
        assert projected is None
        projected = values.copy()
        return projected

    monkeypatch.setattr(
        sql_store.helpers,
        "get_workspace_user_message",
        get_projected_message,
    )
    monkeypatch.setattr(
        sql_store.helpers,
        "create_workspace_user_message",
        project_create,
    )
    sync_attempts = 0

    def sync_projection_events():
        nonlocal sync_attempts
        sync_attempts += 1
        if sync_attempts == 1:
            raise ConnectionRefusedError("event journal unavailable")

    monkeypatch.setattr(store, "_sync_projection_events", sync_projection_events)
    values = {
        "uuid": message_uuid,
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "payload": {"kind": "markdown", "content": "retry safely"},
    }

    with pytest.raises(ConnectionRefusedError, match="journal unavailable"):
        store.create_message(values)
    retry = store.create_message(values)

    assert retry["uuid"] == str(message_uuid)
    assert retry["payload"] == {"kind": "markdown", "content": "retry safely"}
    assert sync_attempts == 2
    assert [item[0:2] for item in order] == [
        ("smtp", "message.create"),
        ("mail", "message.create"),
    ]


def test_file_read_acl_uses_canonical_stream_bindings():
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    service.repository.projection.bindings[binding_uuid] = {
        "stream_uuid": str(stream_uuid),
        "user_uuid": str(USER_UUID),
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )
    file = types.SimpleNamespace(
        user_uuid=PEER_UUID,
        stream_uuid=stream_uuid,
    )

    assert store._can_read_file(file) is True
    service.repository.projection.bindings.clear()
    assert store._can_read_file(file) is False


def test_event_cursor_rejects_missing_changed_future_and_pruned_resumes():
    class EventRepository:
        def event_cursor_state(self, user_uuid):
            assert user_uuid == USER_UUID
            return repository.EventCursorState("91", 20, 10)

        def events_after(self, user_uuid, cursor):
            assert user_uuid == USER_UUID
            assert cursor == repository.EpochCursor(91, 9)
            return []

    service = types.SimpleNamespace(repository=EventRepository())
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
    )

    cases = (
        (3, None, "epoch_generation_required"),
        (9, "90", "epoch_generation_changed"),
        (21, "91", "future_epoch"),
        (8, "91", "epoch_pruned"),
    )
    for after, generation, reason in cases:
        with pytest.raises(
            messenger_exceptions.EventsCursorExpiredError
        ) as error:
            store.events_after(
                {"epoch_version": dm_filters.GT(after)},
                epoch_generation=generation,
            )
        assert error.value.reason == reason
        assert error.value.minimum_epoch_version == 10

    assert store.events_after(
        {"epoch_version": dm_filters.GT(9)},
        epoch_generation="91",
    ) == []
