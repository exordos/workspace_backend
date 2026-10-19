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
from workspace.messenger_api.api import store_factory


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
USER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
PROJECTION_OWNER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000003")
PROVIDER_REALM_UUID = sys_uuid.UUID("40000000-0000-0000-0000-000000000004")


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
    monkeypatch.setattr(
        sql_canonical_store.SQLCanonicalReadStore,
        "_read_state_mode",
        lambda self: "legacy",
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


def test_slow_list_projection_emits_bounded_telemetry(monkeypatch):
    objects = FakeObjects([])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "folders", model)
    clock = iter((10.0, 11.25))
    monkeypatch.setattr(sql_canonical_store.time, "monotonic", lambda: next(clock))
    warnings = []
    monkeypatch.setattr(
        sql_canonical_store.LOG,
        "warning",
        lambda message, **kwargs: warnings.append((message, kwargs)),
    )

    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)
    assert store.filter_resources("folders", {}) == []

    assert warnings == [
        (
            "Slow Messenger list projection",
            {
                "extra": {
                    "projection_resource": "folders",
                    "projection_duration_seconds": 1.25,
                }
            },
        )
    ]


def test_stream_projections_emit_mode_duration_and_row_count(monkeypatch):
    stream_uuid = sys_uuid.uuid4()
    row = {"uuid": stream_uuid}
    objects = FakeObjects([row])
    model = _fake_model(objects, ("uuid", "project_id", "user_uuid"))
    monkeypatch.setitem(sql_canonical_store.RESOURCE_MODELS, "streams", model)
    monkeypatch.setattr(
        sql_canonical_store.resource_projection,
        "as_dict",
        lambda value, resource, **kwargs: value,
    )
    monkeypatch.setattr(
        sql_canonical_store.SQLCanonicalReadStore,
        "_read_state_mode",
        lambda self: "legacy",
    )
    clock = iter((10.0, 10.25, 20.0, 20.5))
    monkeypatch.setattr(sql_canonical_store.time, "monotonic", lambda: next(clock))
    infos = []
    monkeypatch.setattr(
        sql_canonical_store.LOG,
        "info",
        lambda message, *args, **kwargs: infos.append((message, args, kwargs)),
    )

    store = sql_canonical_store.SQLCanonicalReadStore(PROJECT_UUID, USER_UUID)
    assert store.filter_resources("streams", {}) == [row]
    assert store.get_resource("streams", stream_uuid) == row

    assert infos == [
        (
            "Messenger stream collection projection: "
            "read_state_mode=%s rows=%d duration_seconds=%.3f",
            ("legacy", 1, 0.25),
            {
                "extra": {
                    "projection_kind": "stream_collection",
                    "projection_duration_seconds": 0.25,
                    "read_state_mode": "legacy",
                    "projection_row_count": 1,
                }
            },
        ),
        (
            "Messenger exact stream projection: "
            "read_state_mode=%s rows=%d duration_seconds=%.3f",
            ("legacy", 1, 0.5),
            {
                "extra": {
                    "projection_kind": "stream_exact",
                    "projection_duration_seconds": 0.5,
                    "read_state_mode": "legacy",
                    "projection_row_count": 1,
                }
            },
        ),
    ]


@pytest.mark.parametrize(
    "private,direct_user_uuid,participants,valid",
    [
        (False, None, [USER_UUID, PROJECTION_OWNER_UUID], True),
        (True, USER_UUID, [USER_UUID], True),
        (True, USER_UUID, [USER_UUID, PROJECTION_OWNER_UUID], False),
        (True, PROJECTION_OWNER_UUID, [USER_UUID, PROJECTION_OWNER_UUID], True),
        (True, PROJECTION_OWNER_UUID, [USER_UUID], False),
        (
            True,
            PROJECTION_OWNER_UUID,
            [USER_UUID, PROJECTION_OWNER_UUID, PROJECT_UUID],
            False,
        ),
        (True, None, [USER_UUID], False),
        (True, None, [USER_UUID, PROJECTION_OWNER_UUID], False),
    ],
)
def test_stream_participant_validation_uses_private_identity_without_index(
    monkeypatch, private, direct_user_uuid, participants, valid
):
    stream_uuid = sys_uuid.uuid4()
    row = {
        "user_uuid": USER_UUID,
        "direct_user_uuid": direct_user_uuid,
        "private": private,
        "private_index": None,
    }

    class Session:
        def execute(self, _statement, values):
            assert values == (PROJECT_UUID, stream_uuid)
            return types.SimpleNamespace(fetchone=lambda: row)

    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(get_session=lambda: Session()),
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(PROJECT_UUID, USER_UUID)

    if valid:
        store._validate_stream_participants(stream_uuid, participants)
    else:
        with pytest.raises(sql_canonical_store.ra_exceptions.ValidationErrorException):
            store._validate_stream_participants(stream_uuid, participants)


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
        / "migrations/0113-remove-legacy-Messenger-mail-storage-eec69a.py"
    ).read_text()

    assert "CROSS JOIN" not in migration
    assert 'JOIN "m_workspace_stream_bindings" AS bindings' in migration
    assert 'JOIN "m_workspace_file_accesses" AS accesses' not in migration
    assert 'NULL::UUID AS "viewer_user_uuid"' in migration


def test_revoked_stream_messages_require_current_membership():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / (
            "migrations/"
            "0122-revoke-external-projection-access-on-stream-removal-640b9d.py"
        )
    ).read_text()

    assert "e.\"object_type\" <> 'message'" in migration
    assert 'FROM "m_workspace_stream_bindings" AS binding' in migration
    assert 'binding."stream_uuid" =' in migration
    assert "(e.\"payload\"->>'stream_uuid')::uuid" in migration
    assert 'binding."user_uuid" = e."user_uuid"' in migration


def test_canonical_store_has_no_mail_replay_or_nested_session_boundary():
    source = inspect.getsource(sql_canonical_store)

    assert "mail_service" not in source
    assert "messenger_mail" not in source
    assert "sql_store" not in source
    assert "session_manager" not in source


def test_canonical_factory_separates_event_store_without_mail_runtime():
    factory = store_factory.build_store_factory()

    with factory(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, sql_canonical_store.SQLCanonicalMessengerStore)
    with factory.event_store(PROJECT_UUID, USER_UUID) as store:
        assert isinstance(store, sql_canonical_store.PostgresEventStore)


class CursorSession:
    def __init__(self, cursor, events=()):
        self.cursor = cursor
        self.events = list(events)
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))
        if statement.lstrip().startswith("INSERT"):
            row = None
            rows = []
        elif statement.lstrip().startswith("WITH direct_events"):
            row = None
            rows = self.events
        else:
            row = self.cursor
            rows = []
        return types.SimpleNamespace(
            fetchone=lambda: row,
            fetchall=lambda: rows,
        )


def _event_store(monkeypatch, cursor, events=()):
    session = CursorSession(cursor, events)
    context = types.SimpleNamespace(get_session=lambda: session)
    monkeypatch.setattr(sql_canonical_store.contexts, "Context", lambda: context)
    return (
        sql_canonical_store.PostgresEventStore(PROJECT_UUID, USER_UUID),
        session,
    )


def test_canonical_store_events_preserve_cursor_scope_order_and_limit(monkeypatch):
    generation = sys_uuid.uuid4()
    events = [types.SimpleNamespace(epoch_version=21)]
    _postgres_store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
        events,
    )
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "event_row_to_messenger_event",
        lambda event: {"epoch_version": event.epoch_version},
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    result = store.events_after(
        {"epoch_version": dm_filters.GT(20)},
        order_by={"epoch_version": "asc"},
        epoch_generation=str(generation),
        limit=3,
    )

    assert result == [{"epoch_version": 21}]
    statement, parameters = session.statements[2]
    assert statement.count("LIMIT %s") == 3
    assert 'FROM "m_workspace_events"' in statement
    assert 'FROM "m_workspace_broadcast_message_events_v1"' in statement
    assert statement.count('FROM "messenger_stream_bindings" AS binding') == 2
    assert "m_workspace_messages" not in statement
    assert "m_confirmed_external" not in statement
    assert statement.count('FROM "messenger_event_membership_guards" AS guard') == 4
    assert statement.count('LEFT JOIN "messenger_stream_bindings" AS binding') == 2
    assert "UNION ALL" in statement
    assert parameters == (
        PROJECT_UUID,
        USER_UUID,
        20,
        3,
        USER_UUID,
        PROJECT_UUID,
        20,
        3,
        3,
    )


def test_canonical_store_event_cursor_delegates_to_postgres_store(monkeypatch):
    generation = sys_uuid.uuid4()
    _postgres_store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )
    store = sql_canonical_store.SQLCanonicalMessengerStore(
        PROJECT_UUID,
        USER_UUID,
    )

    assert store.event_cursor() == {
        "epoch_generation": str(generation),
        "current_epoch_version": 41,
        "minimum_epoch_version": 13,
    }
    assert [params for _statement, params in session.statements] == [
        (PROJECT_UUID, USER_UUID),
        (PROJECT_UUID, USER_UUID),
    ]


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
    events = [types.SimpleNamespace(epoch_version=21)]
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
        events,
    )
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "event_row_to_messenger_event",
        lambda event: {"epoch_version": event.epoch_version},
    )

    result = store.events_after(
        {"epoch_version": dm_filters.GT(20)},
        epoch_generation=str(generation),
        limit=25,
    )

    assert result == [{"epoch_version": 21}]
    statement, parameters = session.statements[2]
    assert 'recipient."user_uuid" = %s' in statement
    assert 'event."project_id" = %s' in statement
    assert 'event."epoch_version" > %s' in statement
    assert parameters[-1] == 25


def test_postgres_events_keep_model_path_for_additional_filters(monkeypatch):
    generation = sys_uuid.uuid4()
    store, session = _event_store(
        monkeypatch,
        {
            "epoch_generation": generation,
            "current_epoch_version": 41,
            "pruned_through_epoch_version": 12,
        },
    )
    events = [types.SimpleNamespace(epoch_version=21)]
    objects = FakeObjects(events)
    model = _fake_model(
        objects,
        ("epoch_version", "project_id", "user_uuid", "object_type"),
    )
    monkeypatch.setattr(sql_canonical_store.models, "WorkspaceVisibleEvent", model)
    monkeypatch.setattr(
        sql_canonical_store.messenger_events,
        "pack_workspace_event",
        lambda event: {"epoch_version": event.epoch_version},
    )

    result = store.events_after(
        {
            "epoch_version": dm_filters.GT(20),
            "object_type": dm_filters.EQ("message"),
        },
        order_by={"epoch_version": "desc"},
        epoch_generation=str(generation),
        limit=25,
    )

    assert result == [{"epoch_version": 21}]
    assert len(session.statements) == 2
    _operation, query = objects.calls[0]
    assert query["filters"]["project_id"].value == PROJECT_UUID
    assert query["filters"]["user_uuid"].value == USER_UUID
    assert query["filters"]["epoch_version"].value == 20
    assert query["filters"]["object_type"].value == "message"
    assert query["order_by"] == {"epoch_version": "desc"}
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
    assert 'UPDATE "m_workspace_event_audience_snapshots_v1"' in statements[0][0]
    assert 'DELETE FROM "m_workspace_events"' in statements[0][0]
    assert 'DELETE FROM "m_workspace_broadcast_message_events_v1"' in (statements[0][0])
    assert "LIMIT %s" in statements[0][0]
    assert "WHERE NOT EXISTS" in statements[1][0]
    assert 'INSERT INTO "m_workspace_event_cursors"' in statements[1][0]
    assert 'MAX(audience."current_epoch_version")' in statements[1][0]
    assert 'MAX(audience."pruned_through_epoch_version")' in statements[1][0]
    assert 'GROUP BY audience."project_id", member."user_uuid"' in statements[1][0]
    assert 'DELETE FROM "m_workspace_event_audience_snapshots_v1"' in statements[2][0]
    assert "WHERE NOT EXISTS" in statements[2][0]
    assert statements[0][1] == (
        now - sql_canonical_store.EVENT_RETENTION,
        now - sql_canonical_store.EVENT_RETENTION,
        sql_canonical_store.EVENT_PRUNE_BATCH_SIZE,
    )


def test_event_retention_has_created_at_leading_index():
    migration = (
        __import__("pathlib").Path(__file__).parents[3]
        / "migrations/0111-index-Messenger-event-retention-cutoff-117285.py"
    ).read_text()

    assert '"created_at", "project_id", "user_uuid", "epoch_version"' in migration
    assert "m_workspace_events_retention_cutoff_idx" in migration
