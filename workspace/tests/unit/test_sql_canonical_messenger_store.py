# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import inspect
import types
import uuid as sys_uuid

import pytest

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")


class FakeObjects:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def get_all(self, **kwargs):
        self.calls.append(("get_all", kwargs))
        return self.rows

    def get_one(self, **kwargs):
        self.calls.append(("get_one", kwargs))
        return self.rows[0]

    def get_one_or_none(self, **kwargs):
        self.calls.append(("get_one_or_none", kwargs))
        return None if not self.rows else self.rows[0]


def _fake_model(objects, properties):
    return type(
        "FakeModel",
        (),
        {
            "objects": objects,
            "properties": types.SimpleNamespace(
                properties={name: object() for name in properties},
            ),
            "get_id_property_name": classmethod(lambda cls: "uuid"),
        },
    )


def test_resource_reads_are_project_and_user_scoped(monkeypatch):
    row = {"uuid": sys_uuid.uuid4()}
    objects = FakeObjects([row])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setitem(
        sql_canonical_store.RESOURCE_MODELS,
        "streams",
        model,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {**value, "resource": resource},
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_resources(
        "streams",
        {"name": dm_filters.EQ("general")},
        {"uuid": "asc"},
    )

    assert result == [{**row, "resource": "streams"}]
    _operation, query = objects.calls[0]
    assert query["filters"]["project_id"].value == PROJECT_UUID
    assert query["filters"]["user_uuid"].value == USER_UUID
    assert query["filters"]["name"].value == "general"
    assert query["order_by"] == {"uuid": "asc"}


def test_provider_collection_serialization_does_not_lookup_each_canonical_row(
    monkeypatch,
):
    rows = [
        {
            "uuid": sys_uuid.uuid4(),
            "project_id": PROJECT_UUID,
            "source": {"kind": "zulip"},
            "provider_metadata": {
                "kind": "zulip",
                "account_uuid": str(sys_uuid.uuid4()),
                "external_id": f"stream-{index}",
                "capabilities": {},
            },
            "delivery_metadata": None,
        }
        for index in range(50)
    ]
    objects = FakeObjects(rows)
    model = _fake_model(
        objects,
        (
            "uuid",
            "project_id",
            "source",
            "provider_metadata",
            "delivery_metadata",
        ),
    )
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "streams", model)
    lookups = []
    monkeypatch.setattr(
        sql_canonical_store.resource_projection.EXTENSION_CANONICAL_MODELS[
            "streams"
        ].objects,
        "get_one_or_none",
        lambda **kwargs: lookups.append(kwargs),
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_resources("streams", {}, {"uuid": "asc"})

    assert len(result) == 50
    assert lookups == []
    assert all(item["provider"]["kind"] == "zulip" for item in result)


def test_message_page_uses_created_at_uuid_keyset(monkeypatch):
    marker_uuid = sys_uuid.uuid4()
    marker = types.SimpleNamespace(
        uuid=marker_uuid,
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
    )
    row = {"uuid": sys_uuid.uuid4()}
    objects = FakeObjects([marker])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceUserMessage", model)
    monkeypatch.setitem(
        sql_canonical_store.RESOURCE_MODELS,
        "messages",
        model,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: value,
    )
    objects.rows = [marker]
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    objects.get_all = lambda **kwargs: (
        objects.calls.append(("get_all", kwargs)) or [row]
    )
    result = store.filter_message_page({}, marker_uuid, "asc", 51)

    assert result == [row]
    _operation, marker_query = objects.calls[0]
    assert marker_query["filters"]["uuid"].value == marker_uuid
    _operation, page_query = objects.calls[1]
    assert page_query["order_by"] == {"created_at": "asc", "uuid": "asc"}
    assert page_query["limit"] == 51


def test_draft_page_reuses_current_context_session(monkeypatch):
    draft_uuid = sys_uuid.uuid4()
    draft = types.SimpleNamespace(uuid=draft_uuid)
    rows = types.SimpleNamespace(fetchall=lambda: [{"uuid": draft_uuid}])
    session = types.SimpleNamespace(execute=lambda statement, params: rows)
    context = types.SimpleNamespace(get_session=lambda: session)
    objects = FakeObjects([draft])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setattr(sql_canonical_store.contexts, "Context", lambda: context)
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceDraft", model)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    result = store.filter_draft_page({}, None, "asc", 21)

    assert result == [{"uuid": draft_uuid}]
    _operation, query = objects.calls[0]
    assert query["session"] is session
    assert "session_manager" not in inspect.getsource(
        sql_canonical_store.SQLCanonicalReadStore,
    )


def test_file_list_uses_scoped_acl_without_public_user_cross_product(monkeypatch):
    file_uuid = sys_uuid.uuid4()
    row = {
        "uuid": file_uuid,
        "viewer_user_uuid": USER_UUID,
    }
    objects = FakeObjects([row])
    model = _fake_model(
        objects,
        ("uuid", "project_id", "user_uuid", "viewer_user_uuid"),
    )
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "files", model)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: value.copy(),
    )
    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)

    assert store.filter_resources("files", {}, {"uuid": "asc"}) == [{"uuid": file_uuid}]
    _operation, query = objects.calls[0]
    filters = query["filters"]
    assert isinstance(filters, dm_filters.AND)
    assert "project_id" in repr(filters)
    assert str(PROJECT_UUID) in repr(filters)
    assert "viewer_user_uuid" in repr(filters)
    assert str(USER_UUID) in repr(filters)
    assert "acl_mode" in repr(filters)
    assert "public" in repr(filters)


def test_public_file_view_has_no_users_cross_join():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0109-add-scalable-Messenger-visibility-views-0ae35f.py"
    ).read_text()

    assert "CROSS JOIN" not in migration
    assert 'NULL::UUID AS "viewer_user_uuid"' in migration


def test_canonical_message_write_uses_db_helper_in_request_scope(monkeypatch):
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    payload = {"kind": "markdown", "content": "hello"}
    calls = []
    row = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "create_workspace_user_message",
        lambda **kwargs: calls.append(kwargs) or row,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {
            "uuid": value.uuid,
            "resource": resource,
        },
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(store, "_queue_provider_operation", lambda **kwargs: None)

    result = store.create_message(
        {
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": payload,
        }
    )

    assert result == {"uuid": message_uuid, "resource": "messages"}
    assert calls == [
        {
            "project_id": PROJECT_UUID,
            "user_uuid": USER_UUID,
            "enforce_visibility": True,
            "compact_events": True,
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": payload,
        }
    ]


def test_canonical_store_has_no_mail_replay_or_nested_session_boundary():
    source = inspect.getsource(sql_canonical_store)

    assert "mail_service" not in source
    assert "messenger_mail" not in source
    assert "sql_store" not in source
    assert "session_manager" not in source


def test_provider_operation_uses_same_request_transaction(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    session = object()
    stream_objects = FakeObjects(
        [types.SimpleNamespace(external_account_uuid=account_uuid)]
    )
    account = types.SimpleNamespace(uuid=account_uuid)
    bridge = types.SimpleNamespace(uuid=bridge_uuid)
    monkeypatch.setattr(
        sql_canonical_store.models.WorkspaceStream,
        "objects",
        stream_objects,
    )
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: session),
    )
    calls = []
    target_calls = []
    monkeypatch.setattr(
        sql_canonical_store.provider_data,
        "resolve_provider_target",
        lambda current_session, **kwargs: (
            target_calls.append((current_session, kwargs))
            or (account, object(), bridge)
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.provider_data,
        "enqueue_provider_operation",
        lambda current_session, **kwargs: (
            calls.append((current_session, kwargs))
            or (types.SimpleNamespace(uuid=kwargs["operation_uuid"]), sys_uuid.uuid4())
        ),
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    store._queue_provider_operation(
        operation_kind="message.create",
        target_type="message",
        target_uuid=target_uuid,
        stream_uuid=stream_uuid,
        payload={"uuid": target_uuid},
    )

    assert calls[0][0] is session
    assert calls[0][1]["bridge_instance_uuid"] == bridge_uuid
    assert calls[0][1]["external_account_uuid"] == account_uuid
    assert calls[0][1]["project_id"] == PROJECT_UUID
    assert calls[0][1]["owner_user_uuid"] == USER_UUID
    assert target_calls == [
        (
            session,
            {
                "project_id": PROJECT_UUID,
                "owner_user_uuid": USER_UUID,
                "external_account_uuid": account_uuid,
                "stream_uuid": stream_uuid,
                "capability_name": "messenger.message.send",
            },
        )
    ]


def test_provider_capability_rejection_precedes_canonical_message_mutation(
    monkeypatch,
):
    stream_uuid = sys_uuid.uuid4()
    mutation_calls = []
    queue_calls = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda *args: (_ for _ in ()).throw(
            sql_canonical_store.ra_exceptions.ValidationErrorException()
        ),
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "create_workspace_user_message",
        lambda **kwargs: mutation_calls.append(kwargs),
    )
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queue_calls.append(kwargs),
    )

    with pytest.raises(sql_canonical_store.ra_exceptions.ValidationErrorException):
        store.create_message(
            {
                "uuid": sys_uuid.uuid4(),
                "stream_uuid": stream_uuid,
                "topic_uuid": sys_uuid.uuid4(),
                "payload": {"kind": "markdown", "content": "hello"},
            }
        )

    assert mutation_calls == []
    assert queue_calls == []


def test_provider_topic_move_fails_before_canonical_mutation(monkeypatch):
    source_stream_uuid = sys_uuid.uuid4()
    destination_stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    provider_target = (object(), object())
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "projection_values",
        lambda values: values,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_stream_topic",
        lambda *args: types.SimpleNamespace(
            uuid=topic_uuid,
            stream_uuid=source_stream_uuid,
        ),
    )
    targets = []
    monkeypatch.setattr(
        store,
        "_provider_target",
        lambda stream_uuid, operation_kind=None: (
            targets.append((stream_uuid, operation_kind)) or provider_target
        ),
    )
    mutations = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "update_workspace_user_stream_topic",
        lambda *args, **kwargs: mutations.append((args, kwargs)),
    )

    with pytest.raises(sql_canonical_store.ra_exceptions.ValidationErrorException):
        store.update_resource(
            "stream_topics",
            topic_uuid,
            {"stream_uuid": destination_stream_uuid},
        )

    assert targets == [
        (source_stream_uuid, "topic.update"),
        (destination_stream_uuid, None),
    ]
    assert mutations == []


def test_provider_read_operation_preserves_exact_workspace_order(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    first_message_uuid = sys_uuid.uuid4()
    last_message_uuid = sys_uuid.uuid4()
    calls = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: calls.append(kwargs) or object(),
    )

    store._queue_provider_read(
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        message_uuids=[first_message_uuid, last_message_uuid],
        target_type="message",
        target_uuid=last_message_uuid,
    )
    store._queue_provider_read(
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        message_uuids=[],
        target_type="message",
        target_uuid=last_message_uuid,
    )

    assert calls == [
        {
            "operation_kind": "read_state.set",
            "target_type": "message",
            "target_uuid": last_message_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(USER_UUID),
                "message_uuids": [
                    str(first_message_uuid),
                    str(last_message_uuid),
                ],
                "read": True,
            },
        }
    ]


def test_stream_read_queues_only_exact_unread_projection(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    messages = [types.SimpleNamespace(uuid=value) for value in message_uuids]
    row = types.SimpleNamespace(uuid=stream_uuid)
    queued = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "_get_unread_workspace_user_messages",
        lambda **kwargs: messages,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_stream_messages",
        lambda *args: row,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    assert store.perform_action("streams", stream_uuid, "read", {}) == {
        "uuid": stream_uuid
    }
    assert queued == [
        {
            "stream_uuid": stream_uuid,
            "topic_uuid": None,
            "message_uuids": message_uuids,
            "target_type": "stream",
            "target_uuid": stream_uuid,
            "provider_target": None,
        }
    ]


def test_topic_read_queues_exact_unread_projection(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    messages = [types.SimpleNamespace(uuid=value) for value in message_uuids]
    topic = types.SimpleNamespace(uuid=topic_uuid, stream_uuid=stream_uuid)
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_stream_topic",
        lambda *args: topic,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "_get_unread_workspace_user_messages",
        lambda **kwargs: messages,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_stream_topic_messages",
        lambda *args: topic,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("stream_topics", topic_uuid, "read", {})

    assert queued == [
        {
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "message_uuids": message_uuids,
            "target_type": "topic",
            "target_uuid": topic_uuid,
            "provider_target": None,
        }
    ]


def test_duplicate_message_read_does_not_queue_provider_operation(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    message = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        read=True,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_message",
        lambda *args: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_message",
        lambda *args: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_queue_provider_operation",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("messages", message_uuid, "read", {})

    assert queued == []


def test_message_read_up_to_queues_exact_composite_boundary_order(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    created_at = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    message = types.SimpleNamespace(
        uuid=message_uuid,
        stream_uuid=stream_uuid,
        topic_uuid=topic_uuid,
        created_at=created_at,
    )
    first_message_uuid = sys_uuid.uuid4()
    messages = [
        types.SimpleNamespace(uuid=first_message_uuid),
        types.SimpleNamespace(uuid=message_uuid),
    ]
    unread_queries = []
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "get_workspace_user_message",
        lambda *args: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "_get_unread_workspace_user_messages",
        lambda **kwargs: unread_queries.append(kwargs) or messages,
    )
    monkeypatch.setattr(
        sql_canonical_store.helpers,
        "read_workspace_user_topic_messages_to_message",
        lambda *args: message,
    )
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: {"uuid": value.uuid},
    )
    queued = []
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)
    monkeypatch.setattr(store, "_provider_target", lambda *args: None)
    monkeypatch.setattr(
        store,
        "_queue_provider_read",
        lambda **kwargs: queued.append(kwargs),
    )

    store.perform_action("messages", message_uuid, "read_up_to", {})

    assert unread_queries[0]["created_at"] == created_at
    assert unread_queries[0]["boundary_uuid"] == message_uuid
    assert queued[0]["message_uuids"] == [first_message_uuid, message_uuid]


def test_canonical_factory_separates_event_store_without_mail_runtime():
    factory = sql_canonical_store.SQLCanonicalMessengerStoreFactory()

    with factory(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, api_store.WriterGateStoreProxy)
        assert isinstance(
            store._store,
            sql_canonical_store.SQLCanonicalMessengerStore,
        )
    with factory.event_store(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, sql_canonical_store.PostgresEventStore)


class CursorSession:
    def __init__(self, cursor):
        self.cursor = cursor
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        row = None if statement.lstrip().startswith("INSERT") else self.cursor
        return types.SimpleNamespace(fetchone=lambda: row)


def _event_store(monkeypatch, cursor):
    session = CursorSession(cursor)
    context = types.SimpleNamespace(get_session=lambda: session)
    monkeypatch.setattr(sql_canonical_store.contexts, "Context", lambda: context)
    return (
        sql_canonical_store.PostgresEventStore(PROJECT_UUID, USER_UUID),
        session,
    )


def test_postgres_event_cursor_preserves_public_shape(monkeypatch):
    generation = sys_uuid.uuid4()
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )

    assert store.event_cursor() == {
        "epoch_generation": str(generation),
        "current_epoch_version": 41,
        "minimum_epoch_version": 13,
    }
    assert len(session.statements) == 2
    assert (
        'ON CONFLICT ("project_id", "user_uuid") DO NOTHING'
        in (session.statements[0][0])
    )


def test_postgres_event_cursor_combines_direct_and_audience_watermarks(monkeypatch):
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": sys_uuid.uuid4(),
            "current_epoch_version": 81,
            "pruned_through_epoch_version": 40,
        },
    )

    assert store.current_epoch() == 81
    query = session.statements[1][0]
    assert 'LEFT JOIN "m_workspace_event_audience_members_v1"' in query
    assert 'MAX(audience."current_epoch_version")' in query
    assert 'MAX(audience."pruned_through_epoch_version")' in query
    assert "GROUP BY" in query


def test_postgres_event_cursor_rejects_mail_generation(monkeypatch):
    generation = sys_uuid.uuid4()
    store, _session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )

    with pytest.raises(
        sql_canonical_store.messenger_exceptions.EventsCursorExpiredError
    ) as error:
        store.events_after(
            {"epoch_version": dm_filters.GT(20)},
            epoch_generation="old-mail-generation",
        )

    assert error.value.as_dict()["reason"] == "epoch_generation_changed"
    assert error.value.as_dict()["epoch_generation"] == str(generation)


def test_postgres_events_are_user_scoped_and_keep_epoch_order(monkeypatch):
    generation = sys_uuid.uuid4()
    store, _session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )
    events = [types.SimpleNamespace(epoch_version=21)]
    objects = FakeObjects(events)
    model = _fake_model(objects, ("epoch_version", "project_id", "user_uuid"))
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceVisibleEvent", model)
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "pack_workspace_event",
        lambda event: {"epoch_version": event.epoch_version},
    )

    result = store.events_after(
        {"epoch_version": dm_filters.GT(20)},
        epoch_generation=str(generation),
        limit=25,
    )

    assert result == [{"epoch_version": 21}]
    _operation, query = objects.calls[0]
    assert query["filters"]["project_id"].value == PROJECT_UUID
    assert query["filters"]["user_uuid"].value == USER_UUID
    assert query["filters"]["epoch_version"].value == 20
    assert query["order_by"] == {"epoch_version": "asc"}
    assert query["limit"] == 25


def test_event_retention_advances_watermark_before_delete():
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    statements = []

    def execute(statement, params):
        statements.append((statement, params))
        return types.SimpleNamespace(fetchone=lambda: {"count": 17})

    result = sql_canonical_store.prune_expired_events(
        types.SimpleNamespace(execute=execute),
        now,
    )

    assert result == 17
    assert 'INSERT INTO "m_workspace_event_cursors"' in statements[0][0]
    assert 'UPDATE "m_workspace_event_audience_snapshots_v1"' in statements[1][0]
    assert 'DELETE FROM "m_workspace_events"' in statements[2][0]
    assert "WHERE NOT EXISTS" in statements[3][0]
    assert 'INSERT INTO "m_workspace_event_cursors"' in statements[3][0]
    assert 'DELETE FROM "m_workspace_event_audience_snapshots_v1"' in statements[4][0]
    assert "WHERE NOT EXISTS" in statements[4][0]
    assert statements[0][1] == (now - sql_canonical_store.EVENT_RETENTION,)


def test_event_retention_has_created_at_leading_index():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0111-index-Messenger-event-retention-cutoff-117285.py"
    ).read_text()

    assert '"created_at", "project_id", "user_uuid", "epoch_version"' in migration
    assert "m_workspace_events_retention_cutoff_idx" in migration
