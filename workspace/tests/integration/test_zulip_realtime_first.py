# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
import concurrent.futures
import datetime
import hashlib
import json
import threading
import time
from unittest import mock
import uuid

import psycopg.errors
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api import application_services
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_channels as channel_tests
from workspace.tests.integration import test_zulip_flags as flag_tests
from workspace.tests.integration import test_zulip_realtime as realtime_tests
from workspace.tests.integration import test_zulip_reactions as reaction_tests
from workspace.tests.integration import test_zulip_users as user_tests


account = realtime_tests.account
user_import_account = user_tests.user_import_account
channel_import_account = channel_tests.channel_import_account
message_sources = flag_tests.message_sources
imported = flag_tests.imported


def update(account, **values):
    with contexts.Context().session_manager() as session:
        return application_services.ExternalAccountApplicationService.update(
            session,
            application_services.ExternalAccountActor(
                account.owner_user_uuid, account.project_id
            ),
            realtime_tests.stored(account),
            values,
        )


def resume(account, monkeypatch):
    zulip = mock.Mock()
    zulip.get_events = mock.AsyncMock(side_effect=asyncio.CancelledError())
    zulip.register_queue = mock.AsyncMock()
    zulip.close = mock.AsyncMock()
    monkeypatch.setattr(client, "AsyncZulipClient", lambda current: zulip)

    async def run():
        service = realtime_tests.zulip_realtime.ZulipRealtimeService(
            conftest.TEST_DB_URL
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await service._poll_account(realtime_tests.stored(account))
        finally:
            service._executor.shutdown(wait=True)

    asyncio.run(run())
    zulip.register_queue.assert_not_awaited()
    return zulip


@pytest.mark.parametrize(
    "values",
    [
        {"settings": {"label": "A label"}},
        {"disabled": True},
        {"status": "error", "error_description": "A displayed error"},
    ],
)
def test_non_connection_updates_preserve_work_and_queue(
    account, db, monkeypatch, values
):
    subscription = realtime_tests.register(account)
    assert realtime_tests.dispatch()
    (task,) = models.Task.objects.get_all(
        filters={"external_account_uuid": account.uuid}
    )
    revision = account.realtime_revision()
    if "settings" in values:
        values = {"settings": {**account.settings, **values["settings"]}}
    current = update(account, **values)
    assert current.realtime_revision() == revision
    assert models.Task.objects.get_one(filters={"uuid": task.uuid}).status == "new"
    if current.disabled:
        current = update(current, disabled=False)
    poller = resume(current, monkeypatch)
    poller.get_events.assert_awaited_once_with(subscription.queue_id, -1, 90)
    saved = models.RealtimeSubscription.for_account(current)
    assert saved.generation == subscription.generation


@pytest.mark.parametrize("kind", ["flags", "reaction"])
def test_key_rotation_verifies_identity_and_resumes_without_backfill(
    imported, db, monkeypatch, api, kind
):
    account, _source, state, subscription, _zulip, _constructor = imported
    account.properties["has_completed_backfill"].set_value_force(True)
    account.status = "active"
    account.update()
    db.execute(
        "UPDATE bridge_zulip_v5_events SET processed_at=now() AT TIME ZONE 'UTC' WHERE external_account_uuid=%s",
        (account.uuid,),
    )
    if kind == "flags":
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        api.post(f"{flag_tests.MESSAGES}{state.placement_uuid}/actions/unstar/invoke")
        flag_tests.reconcile()
        (pending,) = flag_tests.pending(account)
    else:
        reaction_tests.add(api, imported)
        reaction_tests.reconcile()
        (pending,) = reaction_tests.pending(account)
    current = update(account, settings={**account.settings, "api_key": "rotated"})
    assert current.status == "new"
    assert models.Task.objects.get_one(filters={"uuid": pending.uuid}).status == "new"
    assert not realtime_tests.dispatch()
    owner = uuid.uuid4()
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s WHERE uuid=%s",
        (owner, pending.uuid),
    )
    service = realtime_tests.history_import.HistoryImportService(queue="realtime")
    service._run_task(pending.uuid, owner)
    paused = models.Task.objects.get_one(filters={"uuid": pending.uuid})
    assert paused.status == "new" and paused.lease_owner is None
    zulip = _zulip
    zulip.check_connection.return_value = user_tests._user_data(
        7, email=account.settings["email"]
    )
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    (initialization,) = models.Task.objects.get_all(
        filters={
            "external_account_uuid": account.uuid,
            "content": filters.JSONFields({"kind": "initialize_connection"}),
        }
    )
    with contexts.Context().session_manager():
        initialization.execute()
    assert initialization.status == "done"
    current = realtime_tests.stored(account)
    assert current.status == "active"
    assert (
        models.Task.objects.get_one(
            filters={"uuid": pending.uuid}
        ).content.account_revision
        == current.realtime_revision()
    )
    with contexts.Context().session_manager():
        saved = models.Task.objects.get_one(filters={"uuid": pending.uuid})
        saved.execute()
    assert saved.status == "done"
    if kind == "flags":
        zulip.update_message_flags.assert_called_once()
    else:
        zulip.update_reaction.assert_called_once()
    poller = resume(current, monkeypatch)
    poller.get_events.assert_awaited_once_with(subscription.queue_id, -1, 90)
    assert len(realtime_tests.events(account)) == 1
    assert (
        models.RealtimeSubscription.for_account(current).generation
        == subscription.generation
    )


def test_service_updates_and_key_changes_do_not_run_history_triggers(account, db):
    functions = {
        name: db.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (name + "()",)
        ).fetchone()[0]
        for name in (
            "bridge_zulip_v5_track_account_flags",
            "bridge_zulip_v5_requeue_reactions",
        )
    }
    try:
        for name in functions:
            db.execute(
                f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Unexpected history scan'; END $$"
            )
        with contexts.Context().session_manager():
            current = realtime_tests.stored(account)
            current.update_status("error", error_description="Displayed state")
            current.update_status("active")
        update(account, settings={**account.settings, "label": "metadata"})
        update(account, settings={**account.settings, "api_key": "rotated"})
        with pytest.raises(
            psycopg.errors.RaiseException, match="Unexpected history scan"
        ):
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=TRUE WHERE uuid=%s",
                (account.uuid,),
            )
    finally:
        for definition in functions.values():
            db.execute(definition)


def test_user_events_apply_stored_fields_and_only_fetch_an_unknown_user(
    user_import_account, monkeypatch
):
    account = user_import_account
    zulip = mock.Mock()
    zulip.get_users.return_value = [user_tests._user_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    assert user_tests._run_user_import(account).status == "done"
    zulip.reset_mock()
    for person in (
        {"user_id": 8, "full_name": "Renamed"},
        {"user_id": 8, "new_email": "renamed@example.com"},
    ):
        with contexts.Context().session_manager():
            models.ImportUsersContent(event={"op": "update", "person": person}).execute(
                models.Task(
                    project_id=account.project_id,
                    external_account_uuid=account.uuid,
                    content=models.ImportUsersContent(),
                )
            )
    zulip.get_users.assert_not_called()
    zulip.get_user.assert_not_called()
    profile = user_tests.messenger_models.WorkspaceUser.objects.get_one(
        filters={
            "uuid": uuid.uuid5(
                uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host()), "user:8"
            )
        }
    )
    assert profile.first_name == "Renamed" and profile.email == "renamed@example.com"
    zulip.get_user.return_value = user_tests._user_data(9)
    with contexts.Context().session_manager():
        models.ImportUsersContent(
            event={"op": "update", "person": {"user_id": 9, "full_name": "Unknown"}}
        ).execute(
            models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                content=models.ImportUsersContent(),
            )
        )
    zulip.get_user.assert_called_once_with(9)
    zulip.get_users.assert_not_called()


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "stream",
            "op": "update",
            "stream_id": 42,
            "property": "name",
            "value": "Renamed",
        },
        {
            "type": "subscription",
            "op": "update",
            "stream_id": 42,
            "property": "is_muted",
            "value": True,
        },
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "Topic",
            "visibility_policy": 1,
            "last_updated": 1000,
        },
    ],
)
def test_channel_events_apply_without_catalog_reads(
    channel_import_account, monkeypatch, event
):
    account = channel_import_account
    zulip = mock.Mock()
    zulip.get_channels.return_value = [channel_tests._channel_data()]
    zulip.get_topic_notifications.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    assert channel_tests._run_channel_import(account).status == "done"
    zulip.reset_mock()
    with contexts.Context().session_manager():
        content = models.ImportChannelsContent(event=event)
        content.execute(
            models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                content=content,
            )
        )
    zulip.get_channels.assert_not_called()
    zulip.get_topic_notifications.assert_not_called()
    (channel,) = channel_tests._channels(account)
    if event["type"] == "stream":
        assert channel.name == "Renamed"
    elif event["type"] == "subscription":
        assert channel.notification_mode == "mentions_only"
    else:
        (notification,) = models.ZulipTopicNotification.objects.get_all(
            filters={"channel_uuid": channel.uuid}
        )
        assert (
            notification.topic_name == "topic" and notification.visibility_policy == 1
        )


def test_migration_preserves_queue_cursor_and_outgoing_work_but_cancels_polling(
    imported, db
):
    account, _source, state, subscription, _zulip, _constructor = imported
    migration = next(conftest.MIGRATIONS_DIR.glob("0246-*.py")).name
    engine = conftest.DataMigration(migration)
    scope = [
        str(account.project_id),
        str(account.owner_user_uuid),
        account.provider,
        account.external_account_id,
    ]
    old_revision = hashlib.sha256(
        json.dumps([*scope, account.settings], sort_keys=True).encode()
    ).hexdigest()
    legacy_subscription_revision = hashlib.sha256(
        json.dumps(
            [*scope, account.settings["server_url"].rstrip("/")], sort_keys=True
        ).encode()
    ).hexdigest()
    legacy_realtime_revision = hashlib.sha256(
        json.dumps([*scope, account.connection_settings()], sort_keys=True).encode()
    ).hexdigest()
    tasks = []
    try:
        engine.rollback_migration(migration)
        db.execute(
            "UPDATE bridge_zulip_v5_subscriptions SET last_event_id=123,account_revision=%s WHERE uuid=%s",
            (old_revision, subscription.uuid),
        )
        outgoing = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.UpdateMessageFlagsContent(
                state_uuid=state.uuid,
                flag="read",
                value=True,
                changed_at=datetime.datetime.now(datetime.timezone.utc),
                account_revision=old_revision,
            ),
        )
        outgoing.insert()
        tasks.append(outgoing)
        for kind in (models.ImportUsersContent, models.ImportChannelsContent):
            for periodic in (False, True):
                task = models.Task(
                    project_id=account.project_id,
                    external_account_uuid=account.uuid,
                    content=kind(periodic=periodic),
                    not_before_at=datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(minutes=15),
                )
                task.insert()
                tasks.append(task)
        failed = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.ImportUsersContent(),
            status="error",
            error_description="deadlock detected\nError handler failed: deadlock detected",
        )
        failed.insert()
        tasks.append(failed)
        engine.apply_migration(migration)
        current = models.RealtimeSubscription.for_account(account)
        assert (current.queue_id, current.generation, current.last_event_id) == (
            subscription.queue_id,
            subscription.generation,
            123,
        )
        assert current.account_revision == legacy_subscription_revision
        assert (
            models.Task.objects.get_one(
                filters={"uuid": outgoing.uuid}
            ).content.account_revision
            == legacy_realtime_revision
        )
        for task in tasks[1:-1]:
            current = models.Task.objects.get_one(filters={"uuid": task.uuid})
            assert current.status == ("cancel" if task.content.periodic else "waiting")
        assert (
            models.Task.objects.get_one(filters={"uuid": failed.uuid}).status == "retry"
        )
        engine.rollback_migration(migration)
        assert (
            models.RealtimeSubscription.for_account(account).account_revision
            == old_revision
        )
        engine.apply_migration(migration)
        assert (
            models.RealtimeSubscription.for_account(account).account_revision
            == legacy_subscription_revision
        )
        assert (
            models.Task.objects.get_one(filters={"uuid": outgoing.uuid}).status == "new"
        )
    finally:
        engine.apply_migration(engine.get_latest_migration())
        for task in tasks:
            task.delete()


@pytest.mark.parametrize("kind", ["flags", "reaction"])
def test_local_changes_wait_for_verification_without_a_history_scan(
    imported, db, monkeypatch, api, kind
):
    account, _source, state, _subscription, zulip, _constructor = imported
    update(account, settings={**account.settings, "api_key": "rotated"})
    if kind == "flags":
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        api.post(f"{flag_tests.MESSAGES}{state.placement_uuid}/actions/unstar/invoke")
        change_model = flag_tests.flags.FlagChange
        pending = flag_tests.pending
    else:
        reaction_tests.add(api, imported)
        change_model = reaction_tests.reactions.ReactionChange
        pending = reaction_tests.pending
    with contexts.Context().session_manager():
        change_model.reconcile_batch(100)
    assert change_model.objects.get_all(filters={"project_id": account.project_id})
    assert pending(account) == []
    zulip.check_connection.return_value = user_tests._user_data(
        7, email=account.settings["email"]
    )
    (initialization,) = models.Task.objects.get_all(
        filters={
            "external_account_uuid": account.uuid,
            "content": filters.JSONFields({"kind": "initialize_connection"}),
        }
    )
    with contexts.Context().session_manager():
        initialization.execute()
    assert initialization.status == "done"
    if kind == "reaction":
        db.execute(
            "UPDATE bridge_zulip_v5_reaction_changes SET next_retry_at=NULL WHERE project_id=%s",
            (account.project_id,),
        )
    with contexts.Context().session_manager():
        change_model.reconcile_batch(100)
        (task,) = pending(account)
        task.execute()
    assert task.status == "done"
    if kind == "flags":
        zulip.update_message_flags.assert_called_once()
    else:
        zulip.update_reaction.assert_called_once()
    zulip.get_messages.assert_not_called()
    zulip.get_users.assert_not_called()
    zulip.get_channels.assert_not_called()


@pytest.mark.parametrize("kind", ["flags", "reaction"])
def test_background_projection_does_not_deadlock_realtime_changes(
    imported, api, db, monkeypatch, kind
):
    account, _source, state, *_rest = imported
    if kind == "flags":
        api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
        response = api.post(
            f"{flag_tests.MESSAGES}{state.placement_uuid}/actions/unstar/invoke"
        )
        assert response.status_code == 200, response.text
        changes = flag_tests.flags.FlagChange
        pending = flag_tests.pending
    else:
        reaction_tests.add(api, imported)
        changes = reaction_tests.reactions.ReactionChange
        pending = reaction_tests.pending

    projected, release, consuming = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    consumer_pid = []

    def project_messages(cls, limit):
        # Projection triggers also write these queue rows. Retain that lock
        # until the realtime consumer holds the account and reaches its ack.
        contexts.Context().get_session().execute(
            f"SELECT * FROM {changes.__tablename__} WHERE project_id=%s FOR UPDATE",
            (account.project_id,),
        ).fetchall()
        projected.set()
        assert release.wait(5)
        return 0

    monkeypatch.setattr(
        models.ZulipMessageState, "reconcile_batch", classmethod(project_messages)
    )
    task = models.Task(content=models.ReconciliationContent())
    task.insert()

    def project():
        with contexts.Context().session_manager():
            task.execute()

    def consume():
        with contexts.Context().session_manager() as session:
            consumer_pid.append(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            consuming.set()
            return changes.reconcile_batch(16)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        projection = executor.submit(project)
        consumer = None
        try:
            assert projected.wait(3)
            consumer = executor.submit(consume)
            assert consuming.wait(3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                row = db.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (consumer_pid[0],),
                ).fetchone()
                if row and row[0] == "Lock":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("Realtime consumer did not reach the queue lock")
        finally:
            release.set()
            projection.result(timeout=5)
            if consumer is not None:
                assert consumer.result(timeout=5) == 1

    assert models.Task.objects.get_one(filters={"uuid": task.uuid}).status == "done"
    assert len(pending(account)) == 1
    with contexts.Context().session_manager():
        changes.reconcile_batch(16)
    assert not changes.objects.get_all(filters={"project_id": account.project_id})
    assert len(pending(account)) == 1
