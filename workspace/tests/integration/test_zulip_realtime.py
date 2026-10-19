# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import asyncio
import datetime
import importlib.util
import logging
from unittest import mock
import uuid

import aiohttp.web
import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.services import zulip_realtime
from workspace.tests.integration import conftest


@pytest.fixture
def account(_database, db, scheduled_reconciliation):
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"realtime-{owner}")
    value = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:7",
        status="backfill",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "owner@example.com",
            "api_key": "test",
        },
    )
    value.insert()
    yield value
    value.delete()


def register(account, queue_id="queue-1", maximum=10005):
    with contexts.Context().session_manager():
        return models.RealtimeSubscription.register(
            account,
            {
                "queue_id": queue_id,
                "last_event_id": -1,
                "max_message_id": maximum,
                "event_queue_longpoll_timeout_seconds": 90,
            },
            datetime.datetime.now(datetime.timezone.utc),
        )


def stored(account):
    return external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(account.uuid)}
    )


def events(account):
    return models.RealtimeEvent.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)},
        order_by={"created_at": "asc"},
    )


def dispatch():
    with contexts.Context().session_manager():
        return models.RealtimeEvent.dispatch_one()


def test_registration_defers_backfill_to_the_local_subscriber(account):
    subscription = register(account)
    assert (
        models.Task.objects.get_all(filters={"external_account_uuid": account.uuid})
        == []
    )
    (event,) = events(account)
    assert event.source == "internal"
    assert event.payload == {"type": "full_backfill", "last_message_id": 10005}
    assert dispatch()
    (task,) = models.Task.objects.get_all(
        filters={"external_account_uuid": account.uuid}
    )
    assert task.backfill_uuid == event.uuid
    assert task.content.last_message_id == 10005
    assert subscription.last_event_id == -1
    assert not dispatch()


def test_raw_events_and_cursor_commit_together_and_replay_is_idempotent(
    account, monkeypatch
):
    subscription = register(account)
    incoming = [
        {"id": 0, "type": "heartbeat"},
        {"id": 2, "type": "future_event", "raw": {"unchanged": True}},
    ]
    insert = models.RealtimeEvent.insert

    def fail_second(event, **kwargs):
        if event.event_id == 2:
            raise RuntimeError("storage failed")
        return insert(event, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(models.RealtimeEvent, "insert", fail_second)
        with pytest.raises(RuntimeError, match="storage failed"):
            with contexts.Context().session_manager():
                models.RealtimeSubscription.append_events(
                    account, subscription.generation, incoming
                )
    assert models.RealtimeSubscription.for_account(account).last_event_id == -1
    assert len(events(account)) == 1
    for _index in range(2):
        with contexts.Context().session_manager():
            assert (
                models.RealtimeSubscription.append_events(
                    account, subscription.generation, incoming
                )
                == 2
            )
    assert [
        event.payload for event in events(account) if event.source == "zulip"
    ] == incoming
    assert models.RealtimeSubscription.for_account(account).last_event_id == 2
    while dispatch():
        pass
    assert all(event.processed_at is not None for event in events(account))
    assert (
        len(
            models.Task.objects.get_all(filters={"external_account_uuid": account.uuid})
        )
        == 1
    )


def test_subscriber_retries_atomically_without_duplicate_history_tasks(
    account, monkeypatch
):
    subscription = register(account)
    dispatch()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {"id": 0, "type": "update_message", "message_ids": [11, 12]},
            ],
        )
    insert = models.Task.insert
    calls = []

    def fail_second(task, **kwargs):
        calls.append(task.uuid)
        if len(calls) == 2:
            raise RuntimeError("task insert failed")
        return insert(task, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(models.Task, "insert", fail_second)
        assert dispatch()
    raw = next(event for event in events(account) if event.source == "zulip")
    assert raw.processed_at is None and raw.next_retry_at is not None
    assert models.Task.objects.get_all(filters={"realtime_event_uuid": raw.uuid}) == []
    raw.next_retry_at = None
    raw.update()
    assert dispatch()
    tasks = models.Task.objects.get_all(filters={"realtime_event_uuid": raw.uuid})
    assert len(tasks) == 2
    assert {task.content.first_message_id for task in tasks} == {11, 12}
    assert not dispatch()


@pytest.mark.parametrize("imported", [False, True])
def test_active_only_after_all_backfill_children_finish(
    account, monkeypatch, imported, caplog
):
    account.properties["has_completed_backfill"].set_value_force(imported)
    account.update()
    caplog.clear()
    register(account, maximum=10005)
    (event,) = events(account)
    assert event.payload["type"] == (
        "actualize_backfill" if imported else "full_backfill"
    )
    assert stored(account).status == ("fast_backfill" if imported else "backfill")
    assert dispatch()
    zulip = mock.Mock()
    zulip.get_first_message_id_since.return_value = 7001
    zulip.get_users.return_value = []
    zulip.get_channels.return_value = []
    zulip.get_topic_notifications.return_value = []
    zulip.get_messages.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    service = history_import.HistoryImportService()
    for _index in range(10):
        group = models.Task.objects.get_all(
            filters={"backfill_uuid": filters.EQ(event.uuid)}
        )
        unfinished = [task for task in group if task.status != "done"]
        if not unfinished:
            break
        assert stored(account).status != "active"
        service._iteration()
    else:
        pytest.fail("Backfill did not finish")
    assert stored(account).status == "active"
    assert stored(account).has_completed_backfill
    zulip.get_last_message_id.assert_not_called()
    if imported:
        since = datetime.datetime.fromisoformat(event.payload["since"])
        zulip.get_first_message_id_since.assert_called_once_with(since)
        assert zulip.get_messages.call_args_list == [mock.call(7001, 10005)]
    else:
        assert zulip.get_messages.call_args_list == [
            mock.call(10001, 10005),
            mock.call(5001, 10000),
            mock.call(1, 5000),
        ]
    # Catalog refreshes are not members of the completed group.
    periodic = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "status": filters.EQ("waiting"),
        }
    )
    assert len(periodic) == 2
    assert all(task.backfill_uuid is None for task in periodic)
    assert not [
        record.getMessage()
        for record in caplog.records
        if record.name == "restalchemy.storage.sql.filters"
        and record.levelno >= logging.WARNING
    ]


def test_old_generation_and_disabled_account_cannot_accept_events(account):
    first = register(account)
    dispatch()
    (old_task,) = models.Task.objects.get_all(
        filters={"external_account_uuid": account.uuid}
    )
    second = register(account, "queue-2")
    with pytest.raises(models.SubscriptionChangedError):
        with contexts.Context().session_manager():
            models.RealtimeSubscription.append_events(
                account, first.generation, [{"id": 0, "type": "heartbeat"}]
            )
    old_task = models.Task.objects.get_one(filters={"uuid": old_task.uuid})
    assert old_task.status == "cancel"
    account.disabled = True
    account.update()
    with pytest.raises(models.SubscriptionChangedError):
        with contexts.Context().session_manager():
            models.RealtimeSubscription.append_events(
                account, second.generation, [{"id": 0, "type": "heartbeat"}]
            )
    assert models.RealtimeSubscription.for_account(account).last_event_id == -1


def test_async_worker_acknowledges_saved_events_and_stops_disabled_poller(account):
    async def scenario():
        registration_count = 0
        response_sent_at = None
        acknowledged = asyncio.Event()
        hanging = asyncio.Event()
        service = zulip_realtime.ZulipRealtimeService(
            conftest.TEST_DB_URL, step_interval=0.05
        )

        async def register_queue(request):
            nonlocal registration_count
            registration_count += 1
            data = await request.post()
            assert "event_types" not in data
            return aiohttp.web.json_response(
                {
                    "result": "success",
                    "queue_id": "http-queue",
                    "last_event_id": -1,
                    "max_message_id": 0,
                    "event_queue_longpoll_timeout_seconds": 90,
                }
            )

        async def get_events(request):
            nonlocal response_sent_at
            cursor = int(request.query["last_event_id"])
            if cursor == -1:
                response_sent_at = datetime.datetime.now(datetime.timezone.utc)
                return aiohttp.web.json_response(
                    {
                        "result": "success",
                        "events": [
                            {"id": 4, "type": "heartbeat"},
                            {"id": 5, "type": "future_event"},
                        ],
                    }
                )
            assert cursor == 5
            subscription = await service._database(
                models.RealtimeSubscription.for_account, account
            )
            assert subscription.last_event_id == 5
            received = await service._database(
                lambda: models.RealtimeEvent.objects.get_all(
                    filters={
                        "external_account_uuid": filters.EQ(account.uuid),
                        "source": filters.EQ("zulip"),
                    }
                ),
            )
            assert len({event.created_at for event in received}) == 1
            assert received[0].created_at >= response_sent_at
            acknowledged.set()
            await hanging.wait()
            return aiohttp.web.json_response({"result": "success", "events": []})

        app = aiohttp.web.Application()
        app.router.add_post("/api/v1/register", register_queue)
        app.router.add_get("/api/v1/events", get_events)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        account.settings = {
            **account.settings,
            "server_url": f"http://127.0.0.1:{port}",
        }
        account.update()
        worker = asyncio.create_task(service.serve())
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=5)
            assert registration_count == 1
            account.disabled = True
            account.update()

            async def stopped():
                while service._pollers:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(stopped(), timeout=3)
        finally:
            service.stop()
            await asyncio.wait_for(worker, timeout=3)
            hanging.set()
            await runner.cleanup()
        assert len([event for event in events(account) if event.source == "zulip"]) == 2

    asyncio.run(scenario())


def test_migration_marks_only_preexisting_zulip_backfill_accounts(_database):
    with conftest.isolated_migration_database(
        "0231-Retire-duplicate-Messenger-compatibility-tables-bc9678.py"
    ) as url:
        with psycopg.connect(url, autocommit=True) as connection:
            identifiers = {}
            for status in ("new", "backfill", "fast_backfill", "active", "error"):
                owner = uuid.uuid4()
                conftest.seed_workspace_user(connection, owner, f"migration-{owner}")
                account_uuid = uuid.uuid4()
                connection.execute(
                    "INSERT INTO m_external_accounts_v2 (uuid, project_id, owner_user_uuid, provider, settings, status) VALUES (%s,%s,%s,%s,%s,%s)",
                    (account_uuid, uuid.uuid4(), owner, "zulip", "{}", status),
                )
                identifiers[account_uuid] = status
            path = (
                conftest.MIGRATIONS_DIR
                / "0232-Add-durable-Zulip-realtime-subscriptions-and-backfill-tracking-9ac2f0.py"
            )
            spec = importlib.util.spec_from_file_location("realtime_migration", path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.migration_step.upgrade(connection)
            rows = connection.execute(
                "SELECT uuid, has_completed_backfill FROM m_external_accounts_v2"
            ).fetchall()
            assert {row[0]: row[1] for row in rows} == {
                key: status == "backfill" for key, status in identifiers.items()
            }
            migration.migration_step.downgrade(connection)
            migration.migration_step.upgrade(connection)


def test_supervisor_preserves_healthy_tasks_and_replaces_changed_credentials(account):
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService(conftest.TEST_DB_URL)
        lifecycle = []

        async def poll(current):
            key = current.settings["api_key"]
            lifecycle.append(("start", key))
            try:
                await asyncio.Event().wait()
            finally:
                lifecycle.append(("stop", key))

        service._poll_account = poll
        try:
            await service._reconcile_accounts()
            await asyncio.sleep(0)
            await service._reconcile_accounts()
            assert lifecycle == [("start", "test")]
            account.settings = {**account.settings, "api_key": "replacement"}
            account.update()
            await service._reconcile_accounts()
            await asyncio.sleep(0)
            assert lifecycle == [
                ("start", "test"),
                ("stop", "test"),
                ("start", "replacement"),
            ]
            account.disabled = True
            account.update()
            await service._reconcile_accounts()
            assert not service._pollers
            assert lifecycle[-1] == ("stop", "replacement")
        finally:
            for _, task in service._pollers.values():
                task.cancel()
            await asyncio.gather(
                *(entry[1] for entry in service._pollers.values()),
                return_exceptions=True,
            )
            service._executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_expired_queue_gets_one_new_backfill_and_saved_queue_resumes(
    account, monkeypatch
):
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService(conftest.TEST_DB_URL)
        zulip = mock.Mock()
        zulip.register_queue = mock.AsyncMock(
            side_effect=[
                {"queue_id": "expired", "last_event_id": -1, "max_message_id": 10},
                {"queue_id": "replacement", "last_event_id": -1, "max_message_id": 20},
            ]
        )
        zulip.get_events = mock.AsyncMock(
            side_effect=[
                client.ZulipApiError("BAD_EVENT_QUEUE_ID"),
                [{"id": 3, "type": "heartbeat"}],
                asyncio.CancelledError(),
            ]
        )
        zulip.close = mock.AsyncMock()
        monkeypatch.setattr(client, "AsyncZulipClient", lambda current: zulip)
        try:
            with pytest.raises(asyncio.CancelledError):
                await service._poll_account(account)
            assert zulip.register_queue.await_count == 2
            subscription = await service._database(
                models.RealtimeSubscription.for_account, account
            )
            assert (
                subscription.queue_id == "replacement"
                and subscription.last_event_id == 3
            )
            zulip.register_queue.reset_mock()
            zulip.get_events = mock.AsyncMock(side_effect=asyncio.CancelledError())
            with pytest.raises(asyncio.CancelledError):
                await service._poll_account(account)
            zulip.register_queue.assert_not_awaited()
            zulip.get_events.assert_awaited_once_with("replacement", 3, 90)
        finally:
            service._executor.shutdown(wait=True)
        while dispatch():
            pass
        tasks = models.Task.objects.get_all(
            filters={"external_account_uuid": account.uuid}
        )
        assert len(tasks) == 1 and tasks[0].content.last_message_id == 20

    asyncio.run(scenario())


def test_initial_catalogs_run_before_live_message_import(account, monkeypatch):
    subscription = register(account)
    dispatch()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {"id": 0, "type": "message", "message": {"id": 10006}},
            ],
        )
    dispatch()
    zulip = mock.Mock()
    zulip.get_users.return_value = []
    zulip.get_channels.return_value = []
    zulip.get_topic_notifications.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    service = history_import.HistoryImportService()
    for kind in ("full_backfill", "import_users", "import_channels"):
        with contexts.Context().session_manager():
            (task,) = service._get_tasks()
            assert task.content.kind == kind
            service._execute_task(task)
    with contexts.Context().session_manager():
        (next_task,) = service._get_tasks()
        assert next_task.content.first_message_id == 10006


def test_partial_monthly_backfill_recovery_does_not_activate_early(
    account, monkeypatch
):
    account.properties["has_completed_backfill"].set_value_force(True)
    account.update()
    register(account, maximum=5001)
    dispatch()
    zulip = mock.Mock()
    zulip.get_first_message_id_since.return_value = 1
    zulip.get_users.return_value = []
    zulip.get_channels.return_value = []
    zulip.get_topic_notifications.return_value = []
    zulip.get_messages.side_effect = [requests.Timeout("temporary"), [], []]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    service = history_import.HistoryImportService()
    for _index in range(4):
        service._iteration()
    assert stored(account).status == "error"
    service._iteration()
    assert stored(account).status == "fast_backfill"
    (retry,) = models.Task.objects.get_all(
        filters={"external_account_uuid": account.uuid, "status": "retry"}
    )
    retry.next_retry_at = None
    retry.update()
    service._iteration()
    assert stored(account).status == "active"


def test_changing_zulip_identity_requires_full_import_again(account, monkeypatch):
    account.properties["has_completed_backfill"].set_value_force(True)
    account.update()
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.InitializeConnectionContent(),
    )
    task.insert()
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 8,
        "full_name": "Another account",
        "email": "another@example.com",
    }
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    with contexts.Context().session_manager():
        task.execute()
    current = stored(account)
    assert current.external_account_id == "zulip.example.com:8"
    assert not current.has_completed_backfill
    assert (
        models.Task.objects.get_all(
            filters={"external_account_uuid": account.uuid, "status": "new"}
        )
        == []
    )
    register(current)
    (event,) = events(current)
    assert event.payload["type"] == "full_backfill"


def test_recovered_catalog_does_not_skip_an_unconsumed_backfill_event(
    account, monkeypatch
):
    account.properties["has_completed_backfill"].set_value_force(True)
    account.update()
    register(account)
    account = stored(account)
    with contexts.Context().session_manager():
        account.update_status("error", error_description="temporary")
    zulip = mock.Mock()
    zulip.get_users.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportUsersContent(periodic=False),
    )
    task.insert()
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    assert stored(account).status == "fast_backfill"
    (event,) = events(account)
    assert event.processed_at is None
