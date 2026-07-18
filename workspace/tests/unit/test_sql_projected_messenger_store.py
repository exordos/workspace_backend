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
        self.entries.append(repository.JournalEntry(position, frozenset(), record))
        return position

    def read_operations(self, after_uid=0):
        return repository.JournalReplay(
            protocol.MailboxMetadata(1, self.next_uid, None),
            tuple(entry for entry in self.entries if entry.position.uid > after_uid),
        )


class FakeMailService:
    def __init__(self, order):
        self.order = order
        self.repository = FakeRepository(order)

    def deliver_message(self, record):
        previous = self.repository.projection.message_positions.get(record.entity_uuid)
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


def test_bridge_outbox_reuses_request_database_session(monkeypatch):
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
    bridge_config = types.SimpleNamespace(
        realm_uuid=sys_uuid.uuid4(),
        bridge_instance_uuid=sys_uuid.uuid4(),
        identity_generation=1,
        enrollment_secret="opaque enrollment secret",
    )
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=bridge_config,
    )
    request_session = object()
    context = types.SimpleNamespace(get_session=lambda: request_session)
    monkeypatch.setattr(sql_store.contexts, "Context", lambda: context)
    queued = []

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: (session, values),
    )

    def queue_message_create(session, **values):
        queued.append((session, values))

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_message_create",
        queue_message_create,
    )
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
        lambda **values: values,
    )

    result = store.create_message(
        {
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "hello bridge"},
        }
    )

    assert result["uuid"] == str(message_uuid)
    assert len(queued) == 1
    assert queued[0][0] is request_session
    assert queued[0][1]["message"]["uuid"] == message_uuid


def test_external_topic_create_publishes_mapping_without_rename_operation(monkeypatch):
    class Topic(dict):
        __getattr__ = dict.__getitem__

    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    request_session = object()
    bridge_instance_uuid = sys_uuid.uuid4()
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_instance_uuid,
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.helpers,
        "create_workspace_user_stream_topic",
        lambda **values: Topic(values["values"]),
    )
    published = []
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "ensure_topic_projection_mapping",
        lambda session, **values: published.append((session, values)),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("topic creation must not queue topic.upsert")
        ),
    )

    result = store.create_resource(
        "stream_topics",
        {
            "uuid": topic_uuid,
            "stream_uuid": stream_uuid,
            "name": "new topic",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )

    assert result["uuid"] == str(topic_uuid)
    assert len(published) == 1
    assert published[0][0] is request_session
    assert published[0][1] == {
        "project_uuid": PROJECT_UUID,
        "owner_user_uuid": USER_UUID,
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "topic_name": "new topic",
        "bridge_instance_uuid": bridge_instance_uuid,
    }
    assert [entry.record.operation for entry in service.repository.entries] == [
        "topic.create"
    ]


def test_external_preflight_rejects_before_canonical_message_mutation(monkeypatch):
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
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )

    def reject(session, **values):
        assert session is request_session
        assert values["operation_kind"] == "message.create"
        raise ValueError("external_operation_unavailable")

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        reject,
    )

    with pytest.raises(sql_store.ra_exceptions.ValidationErrorException):
        store.create_message(
            {
                "uuid": message_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "must not persist"},
            }
        )

    assert order == []
    assert message_uuid not in service.repository.projection.messages


@pytest.mark.parametrize(
    ("resource", "action"),
    (
        ("streams", "read"),
        ("stream_topics", "read"),
        ("messages", "read"),
        ("messages", "read_up_to"),
    ),
)
def test_external_read_preflight_rejects_before_canonical_mutation(
    monkeypatch,
    resource,
    action,
):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    service.repository.projection.messages[message_uuid] = {
        "uuid": str(message_uuid),
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "created_at": "2026-07-18T00:00:00+00:00",
    }
    service.repository.projection.topics[topic_uuid] = {
        "uuid": str(topic_uuid),
        "stream_uuid": str(stream_uuid),
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )

    def reject(session, **values):
        assert session is request_session
        assert values["project_uuid"] == PROJECT_UUID
        assert values["owner_user_uuid"] == USER_UUID
        assert values["operation_kind"] == "read_state.set"
        assert sys_uuid.UUID(str(values["target_stream_uuid"])) == stream_uuid
        raise ValueError("external_operation_unavailable")

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        reject,
    )

    resource_uuid = {
        "streams": stream_uuid,
        "stream_topics": topic_uuid,
        "messages": message_uuid,
    }[resource]
    with pytest.raises(sql_store.ra_exceptions.ValidationErrorException):
        store.perform_action(resource, resource_uuid, action, {})

    assert order == []
    assert service.repository.entries == []


def test_empty_external_stream_read_does_not_queue_empty_exact_selector(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    service.repository.projection.streams[stream_uuid] = {
        "uuid": str(stream_uuid),
        "kind": "stream",
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    preflights = []

    def validate(session, **values):
        assert session is request_session
        preflights.append(values)
        return {"chat_uuid": sys_uuid.uuid4()}

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        validate,
    )

    def reject_queue(*args, **kwargs):
        raise AssertionError("empty exact read selector must not be queued")

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        reject_queue,
    )
    row = {"uuid": str(stream_uuid), "unread_count": 0}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_stream_messages",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("streams", stream_uuid, "read", {})
    assert result["uuid"] == str(stream_uuid)
    assert result["unread_count"] == 0
    assert len(preflights) == 1
    assert preflights[0]["operation_kind"] == "read_state.set"
    assert preflights[0]["target_stream_uuid"] == stream_uuid
    assert order == []
    assert service.repository.entries == []


def test_external_stream_read_queues_exact_canonical_message_set(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    lower_uuid = sys_uuid.UUID("10000000-0000-0000-0000-000000000010")
    higher_uuid = sys_uuid.UUID("20000000-0000-0000-0000-000000000020")
    service.repository.projection.streams[stream_uuid] = {
        "uuid": str(stream_uuid),
        "kind": "stream",
    }
    for message_uuid in (higher_uuid, lower_uuid):
        service.repository.projection.messages[message_uuid] = {
            "uuid": str(message_uuid),
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "created_at": "2026-07-18T00:00:00+00:00",
        }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: {"chat_uuid": sys_uuid.uuid4()},
    )
    queued = []

    def queue(session, **values):
        assert session is request_session
        queued.append(values)
        return sys_uuid.uuid4()

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        queue,
    )
    row = {"uuid": str(stream_uuid), "unread_count": 0}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_stream_messages",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("streams", stream_uuid, "read", {})

    assert result["uuid"] == str(stream_uuid)
    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert payload["message_uuids"] == [str(lower_uuid), str(higher_uuid)]
    assert "through_message_uuid" not in payload
    assert [entry.record.entity_uuid for entry in service.repository.entries] == [
        lower_uuid,
        higher_uuid,
    ]


def test_external_read_up_to_queues_native_compound_prefix_as_exact_selector(
    monkeypatch,
):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    lower_uuid = sys_uuid.UUID("10000000-0000-0000-0000-000000000010")
    boundary_uuid = sys_uuid.UUID("20000000-0000-0000-0000-000000000020")
    higher_uuid = sys_uuid.UUID("30000000-0000-0000-0000-000000000030")
    for message_uuid in (higher_uuid, lower_uuid, boundary_uuid):
        service.repository.projection.messages[message_uuid] = {
            "uuid": str(message_uuid),
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "created_at": "2026-07-18T00:00:00+00:00",
        }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: {"chat_uuid": sys_uuid.uuid4()},
    )
    queued = []

    def queue(session, **values):
        assert session is request_session
        queued.append(values)
        return sys_uuid.uuid4()

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        queue,
    )
    row = {"uuid": str(boundary_uuid), "read": True}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_topic_messages_to_message",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("messages", boundary_uuid, "read_up_to", {})

    assert result["uuid"] == str(boundary_uuid)
    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert payload["message_uuids"] == [str(lower_uuid), str(boundary_uuid)]
    assert "through_message_uuid" not in payload
    assert [entry.record.entity_uuid for entry in service.repository.entries] == [
        lower_uuid,
        boundary_uuid,
    ]


def test_external_topic_read_queues_exact_canonical_message_set(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    lower_uuid = sys_uuid.UUID("10000000-0000-0000-0000-000000000010")
    higher_uuid = sys_uuid.UUID("20000000-0000-0000-0000-000000000020")
    service.repository.projection.topics[topic_uuid] = {
        "uuid": str(topic_uuid),
        "stream_uuid": str(stream_uuid),
    }
    for message_uuid in (higher_uuid, lower_uuid):
        service.repository.projection.messages[message_uuid] = {
            "uuid": str(message_uuid),
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "created_at": "2026-07-18T00:00:00+00:00",
        }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: {"chat_uuid": sys_uuid.uuid4()},
    )
    queued = []

    def queue(session, **values):
        assert session is request_session
        queued.append(values)
        return sys_uuid.uuid4()

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        queue,
    )
    row = {"uuid": str(topic_uuid), "unread_count": 0}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_stream_topic_messages",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("stream_topics", topic_uuid, "read", {})

    assert result["uuid"] == str(topic_uuid)
    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert payload["message_uuids"] == [str(lower_uuid), str(higher_uuid)]
    assert "through_message_uuid" not in payload


def test_external_message_read_queues_single_exact_selector(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    service.repository.projection.messages[message_uuid] = {
        "uuid": str(message_uuid),
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "created_at": "2026-07-18T00:00:00+00:00",
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: {"chat_uuid": sys_uuid.uuid4()},
    )
    queued = []

    def queue(session, **values):
        assert session is request_session
        queued.append(values)
        return sys_uuid.uuid4()

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        queue,
    )
    row = {"uuid": str(message_uuid), "read": True}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_message",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("messages", message_uuid, "read", {})

    assert result["uuid"] == str(message_uuid)
    assert len(queued) == 1
    payload = queued[0]["payload"]
    assert payload["message_uuids"] == [str(message_uuid)]
    assert "through_message_uuid" not in payload


def test_empty_external_topic_read_does_not_queue_bridge_operation(monkeypatch):
    order = []
    service = FakeMailService(order)
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    service.repository.projection.topics[topic_uuid] = {
        "uuid": str(topic_uuid),
        "stream_uuid": str(stream_uuid),
    }
    store = sql_store.SQLProjectedMessengerStore(
        PROJECT_UUID,
        USER_UUID,
        service,
        bridge_config=types.SimpleNamespace(
            realm_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=sys_uuid.uuid4(),
            identity_generation=1,
            enrollment_secret="opaque enrollment secret",
        ),
    )
    request_session = object()
    monkeypatch.setattr(
        sql_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: request_session),
    )
    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "validate_workspace_operation",
        lambda session, **values: {"chat_uuid": sys_uuid.uuid4()},
    )

    def reject_queue(*args, **kwargs):
        raise AssertionError("empty topic read must not queue a bridge operation")

    monkeypatch.setattr(
        sql_store.external_bridge_data_plane,
        "queue_workspace_operation",
        reject_queue,
    )
    row = {"uuid": str(topic_uuid), "unread_count": 0}
    monkeypatch.setattr(
        sql_store.helpers,
        "read_workspace_user_stream_topic_messages",
        lambda *args, **kwargs: row,
    )

    result = store.perform_action("stream_topics", topic_uuid, "read", {})

    assert result["uuid"] == str(topic_uuid)
    assert order == []
    assert service.repository.entries == []


def test_draft_page_reuses_request_database_session(monkeypatch):
    result = types.SimpleNamespace(fetchall=lambda: [])
    request_session = types.SimpleNamespace(execute=lambda *args: result)
    context = types.SimpleNamespace(get_session=lambda: request_session)
    monkeypatch.setattr(sql_store.contexts, "Context", lambda: context)

    store = sql_store.SQLDraftStore(PROJECT_UUID, USER_UUID)

    assert store.filter_draft_page({}, None, "asc", 20) == []


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

        def events_after(self, user_uuid, cursor, limit=None):
            assert user_uuid == USER_UUID
            assert cursor == repository.EpochCursor(91, 9)
            assert limit is None
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
        with pytest.raises(messenger_exceptions.EventsCursorExpiredError) as error:
            store.events_after(
                {"epoch_version": dm_filters.GT(after)},
                epoch_generation=generation,
            )
        assert error.value.reason == reason
        assert error.value.minimum_epoch_version == 10

    assert (
        store.events_after(
            {"epoch_version": dm_filters.GT(9)},
            epoch_generation="91",
        )
        == []
    )
