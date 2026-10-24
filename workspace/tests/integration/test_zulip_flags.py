# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import importlib.util
import uuid
from unittest import mock

import pytest
import psycopg
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import test_zulip_message_states as source_tests
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests
from workspace.tests.integration import conftest


message_sources = source_tests.message_sources
outgoing = outgoing_tests.outgoing
MESSAGES = "/v1/messenger/messages/"


def reconcile():
    for _index in range(10):
        with contexts.Context().session_manager():
            if not flags.FlagChange.reconcile_batch(100):
                return
    pytest.fail("Message flag reconciliation did not converge")


def reload(state):
    return v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={
            "uuid": filters.EQ(state.uuid),
            "project_id": filters.EQ(state.project_id),
        }
    )


def pending(account):
    with contexts.Context().session_manager() as session:
        return session.execute(
            "SELECT * FROM zulip_outgoing_intents WHERE external_account_uuid=%s AND entity_type='message_state' AND status<>'done' ORDER BY id",
            (account.uuid,),
        ).fetchall()


@pytest.fixture
def imported(message_sources, monkeypatch):
    account, source = message_sources()
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test",
    }
    account.status = "active"
    account.update()
    source_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    reconcile()
    state = v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    subscription = models.RealtimeSubscription(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        generation=uuid.uuid4(),
        queue_id="flags-queue",
        last_event_id=-1,
        account_revision=account.subscription_revision(),
        longpoll_timeout=90,
    )
    subscription.insert()
    models.RealtimeEvent(
        uuid=uuid.uuid5(subscription.generation, "backfill"),
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        generation=subscription.generation,
        queue_id=subscription.queue_id,
        source="internal",
        payload={"type": "full_backfill", "last_message_id": source.message_id},
        processed_at=datetime.datetime.now(datetime.timezone.utc),
    ).insert()
    zulip = mock.Mock()
    zulip.update_message_flags.return_value = {
        "result": "success",
        "messages": [source.message_id],
    }
    constructor = mock.Mock(return_value=zulip)
    monkeypatch.setattr(client, "ZulipClient", constructor)
    return account, source, state, subscription, zulip, constructor


def receive(imported, payloads, received_at=None):
    account, _source, _state, subscription, _zulip, _constructor = imported
    received_at = received_at or datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account, subscription.generation, payloads, received_at
        )
    while True:
        with contexts.Context().session_manager():
            if not models.RealtimeEvent.dispatch_one():
                break
    reconcile()
    return received_at


@pytest.mark.parametrize("flag", flags.FLAGS)
def test_incoming_flags_use_receipt_time_and_do_not_echo(imported, flag, db):
    account, source, state, _subscription, zulip, _constructor = imported
    received_at = receive(
        imported,
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": flag,
                "op": "remove",
                "messages": [source.message_id],
                "all": False,
            }
        ],
    )
    current = reload(state)
    assert (current.read_at is not None if flag == "read" else current.starred) is False
    assert getattr(current, f"{flag}_updated_at") == received_at
    assert (current.starred if flag == "read" else current.read_at is not None) is True
    assert pending(account) == []
    zulip.update_message_flags.assert_not_called()
    raw = models.RealtimeEvent.objects.get_one(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "source": filters.EQ("zulip"),
        }
    )
    assert raw.created_at == received_at
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s AND payload->>'recipient_uuid'=%s",
            (account.project_id, str(state.user_uuid)),
        )
        assert cursor.fetchone()[0] >= 1


def test_flags_in_one_poll_keep_event_order_and_replay_does_not_refresh_time(imported):
    account, source, state, subscription, _zulip, _constructor = imported
    payloads = [
        {
            "id": index,
            "type": "update_message_flags",
            "flag": "starred",
            "op": op,
            "messages": [source.message_id],
        }
        for index, op in enumerate(("remove", "add", "remove"))
    ]
    received_at = receive(imported, payloads)
    assert not reload(state).starred
    receive(imported, payloads, received_at + datetime.timedelta(hours=1))
    assert reload(state).starred_updated_at == received_at
    assert all(
        event.created_at == received_at
        for event in models.RealtimeEvent.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "source": filters.EQ("zulip"),
            }
        )
    )
    assert models.RealtimeSubscription.for_account(account).last_event_id == 2


def test_same_poll_flags_can_be_reconciled_between_each_event(imported):
    account, source, state, _subscription, _zulip, _constructor = imported
    received_at = datetime.datetime.now(datetime.timezone.utc)
    for event_id, operation in enumerate(("remove", "add", "remove")):
        receive(
            imported,
            [
                {
                    "id": event_id,
                    "type": "update_message_flags",
                    "flag": "starred",
                    "op": operation,
                    "messages": [source.message_id],
                }
            ],
            received_at,
        )
        current = reload(state)
        assert current.starred == (operation == "add")
        assert current.starred_updated_at == received_at
        assert pending(account) == []


def test_mark_all_read_handles_empty_message_ids_and_late_import(
    imported, message_sources
):
    account, source, state, _subscription, _zulip, _constructor = imported
    receive(
        imported,
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": "read",
                "op": "remove",
                "messages": [source.message_id],
            }
        ],
    )
    at = receive(
        imported,
        [
            {
                "id": 1,
                "type": "update_message_flags",
                "flag": "read",
                "op": "add",
                "messages": [],
                "all": True,
            }
        ],
    )
    assert reload(state).read_updated_at == at
    assert reload(state).read_at is not None
    receive(
        imported,
        [
            {
                "id": 2,
                "type": "update_message_flags",
                "flag": "read",
                "op": "remove",
                "messages": [source.message_id],
            }
        ],
    )
    assert reload(state).read_at is None


def add_source(account, source, message_id):
    result = models.ZulipMessage(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        message_id=message_id,
        message_uuid=uuid.uuid5(
            uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host()), str(message_id)
        ),
        sender_id=source.sender_id,
        timestamp=source.timestamp,
        channel=source.channel,
        topic=source.topic,
        content=source.content,
    )
    result.insert()
    return result


def test_all_read_covers_messages_materialized_after_the_event(imported):
    account, source, state, _subscription, _zulip, _constructor = imported
    already_read_at = reload(state).read_updated_at
    late = add_source(account, source, 43)
    at = receive(
        imported,
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": "read",
                "op": "add",
                "all": True,
                "messages": [],
            }
        ],
    )
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    reconcile()
    states = v2_models.WorkspaceUserMessageState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert len(states) == 2
    assert all(row.read_at is not None for row in states)
    assert reload(state).read_updated_at == already_read_at
    assert next(row for row in states if row.uuid != state.uuid).read_updated_at == at
    assert late.read is False  # The newer all-read event wins over this older snapshot.


def test_a_slow_history_snapshot_cannot_undo_a_newer_flag_event(imported):
    account, source, state, _subscription, _zulip, _constructor = imported
    snapshot_started = datetime.datetime.now(datetime.timezone.utc)
    received_at = receive(
        imported,
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": "starred",
                "op": "remove",
                "messages": [source.message_id],
            }
        ],
    )
    with contexts.Context().session_manager():
        snapshot = flags.observe_snapshot(
            account,
            [{"id": source.message_id, "flags": ["read", "starred"]}],
            snapshot_started,
        )
    reconcile()
    assert snapshot[source.message_id]["starred"] is False
    assert not reload(state).starred
    assert reload(state).starred_updated_at == received_at


def test_reconnect_refreshes_flags_outside_the_history_window(imported):
    account, source, state, _subscription, zulip, _constructor = imported
    zulip.get_messages.return_value = [{"id": source.message_id, "flags": []}]
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.RefreshMessageFlagsContent(last_message_id=source.message_id),
    )
    task.insert()
    with contexts.Context().session_manager():
        task.execute()
    reconcile()
    zulip.get_messages.assert_called_once_with(source.message_id, source.message_id)
    assert task.status == "done"
    assert reload(state).read_at is None and not reload(state).starred


def test_reconnect_refreshes_native_deliveries_without_imported_observations(outgoing):
    api, account, _channel, remote, zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        message = v2_models.WorkspaceMessage.objects.get_one(
            filters={"uuid": filters.EQ(placement.message_uuid)}
        )
        delivery = outgoing_tests.seed_delivery(account, placement, message)
        delivery.zulip_message_id = remote["id"]
        delivery.update()
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.RefreshMessageFlagsContent(last_message_id=remote["id"]),
        )
        task.insert()
    zulip.get_messages.side_effect = None
    zulip.get_messages.return_value = [{"id": remote["id"], "flags": ["starred"]}]
    with contexts.Context().session_manager():
        task.execute()
    reconcile()
    zulip.get_messages.assert_called_once_with(remote["id"], remote["id"])
    assert task.status == "done"
    assert api.get(f"{MESSAGES}{public['uuid']}").json()["starred"] is True
    assert pending(account) == []


def test_flag_migration_round_trip(_database):
    with conftest.isolated_migration_database(
        "0233-Add-outgoing-Zulip-message-tasks-and-Workspace-outbox-subscription-23c3f1.py"
    ) as url:
        path = next(conftest.MIGRATIONS_DIR.glob("0234-*.py"))
        spec = importlib.util.spec_from_file_location("flag_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with psycopg.connect(url, autocommit=True) as connection:
            migration.migration_step.upgrade(connection)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_flag_states')"
            ).fetchone()[0]
            migration.migration_step.downgrade(connection)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_message_flags')"
            ).fetchone() == (None,)
            migration.migration_step.upgrade(connection)


@pytest.mark.pre_outgoing_schema
def test_flag_migration_preserves_existing_local_changes(imported, db):
    account, _source, state, _subscription, _zulip, _constructor = imported
    account_trigger = db.execute(
        "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE tgname='bridge_zulip_v5_track_account_flags'"
    ).fetchone()[0]
    path = next(conftest.MIGRATIONS_DIR.glob("0234-*.py"))
    spec = importlib.util.spec_from_file_location("flag_upgrade_data", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # The scoped probe returns this view's composite type. Remove its current
    # definition around the deliberately isolated historical migration, then
    # restore it against the recreated view.
    probe = db.execute(
        "SELECT pg_get_functiondef('bridge_zulip_v5_probe_flag_states(uuid,uuid,uuid,text)'::regprocedure)"
    ).fetchone()[0]
    db.execute("DROP FUNCTION bridge_zulip_v5_probe_flag_states(uuid,uuid,uuid,text)")
    migration.migration_step.downgrade(db)
    try:
        db.execute(
            "UPDATE messenger_user_message_states SET read_at=NULL, starred=FALSE, updated_at=NOW() WHERE uuid=%s",
            (state.uuid,),
        )
    finally:
        migration.migration_step.upgrade(db)
        db.execute(
            "DROP TRIGGER bridge_zulip_v5_track_account_flags ON m_external_accounts_v2"
        )
        db.execute(account_trigger)
        # Restore independent queue metadata from migration 0242 after the
        # historical migration recreates this table.
        db.execute("""
            ALTER TABLE bridge_zulip_v5_flag_changes
                ADD COLUMN message_created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch',
                ADD COLUMN message_sequence BIGINT NOT NULL DEFAULT 0;
            CREATE TRIGGER newest_message_first BEFORE INSERT OR UPDATE ON bridge_zulip_v5_flag_changes
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_order_message_change();
            UPDATE bridge_zulip_v5_flag_changes SET message_sequence=message_sequence;
            CREATE INDEX bridge_zulip_v5_flag_changes_newest_idx
                ON bridge_zulip_v5_flag_changes(message_created_at DESC,message_sequence DESC,revision);
        """)
        db.execute(probe)
    reconcile()
    current = reload(state)
    assert current.read_at is None and not current.starred
    assert (
        current.read_updated_at is not None and current.starred_updated_at is not None
    )


def test_historical_flag_probe_keeps_the_shared_history_trigger(db):
    # The preceding historical downgrade recreates the flags table. Its
    # private schema must not remove the current trigger from other scenarios.
    assert db.execute(
        "SELECT count(*) FROM pg_trigger "
        "WHERE tgrelid='bridge_zulip_v5_message_flags'::regclass "
        "AND tgname='bridge_zulip_v5_track_history_source' AND NOT tgisinternal"
    ).fetchone() == (1,)


def test_flags_for_a_missing_message_schedule_its_import(imported):
    account, _source, _state, _subscription, _zulip, _constructor = imported
    receive(
        imported,
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": "starred",
                "op": "add",
                "messages": [999],
            }
        ],
    )
    tasks = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "import_history"}),
        }
    )
    assert len(tasks) == 1
    assert tasks[0].content.first_message_id == tasks[0].content.last_message_id == 999


def test_delivered_native_message_flags_use_the_same_projection(outgoing):
    api, account, _channel, remote, zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    canonical = v2_models.WorkspaceMessage.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        delivery = outgoing_tests.seed_delivery(account, placement, canonical)
        delivery.zulip_message_id = remote["id"]
        delivery.delivered_at = datetime.datetime.now(datetime.timezone.utc)
        delivery.update()
    response = api.post(f"{MESSAGES}{public['uuid']}/actions/star/invoke")
    assert response.status_code == 200, response.text
    reconcile()
    assert len(pending(account)) == 1
    subscription = models.RealtimeSubscription.for_account(account)
    receive(
        (account, None, None, subscription, zulip, None),
        [
            {
                "id": 0,
                "type": "update_message_flags",
                "flag": "starred",
                "op": "remove",
                "messages": [remote["id"]],
            }
        ],
    )
    assert api.get(f"{MESSAGES}{public['uuid']}").json()["starred"] is False
    assert canonical.source_name == "native"
