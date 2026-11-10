# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import contextlib
from unittest import mock

import psycopg
import pytest

from workspace.bridges.zulip import outgoing_operations
from workspace.services import zulip_incoming
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


async def renew(incoming, maximum=1000, streams=(42,)):
    _, _, account, _, url = incoming
    service = zulip_incoming.ZulipIncomingService(url)
    await service.db.open()
    if not await service.db.sql(
        "SELECT 1 FROM zulip_incoming_subscriptions WHERE external_account_uuid=%s",
        (account["uuid"],),
    ):
        await service.db.sql(
            zulip_incoming.SAVE_SUBSCRIPTION_SQL,
            (
                account["uuid"],
                account["project_id"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                account["external_account_id"],
                account["settings"]["server_url"],
                "expired-subscription",
                -1,
                90,
            ),
        )
    service.http = mock.Mock(
        request=mock.AsyncMock(
            return_value={
                "queue_id": f"renewed-{maximum}",
                "last_event_id": -1,
                "max_message_id": maximum,
                "subscriptions": [
                    {
                        "stream_id": stream,
                        "name": "Channel" if stream == 42 else "Other",
                        "description": "",
                    }
                    for stream in streams
                ],
            }
        )
    )
    service.poll_http = mock.Mock(
        request=mock.AsyncMock(
            side_effect=[
                outgoing_operations.ApiError(400, {"code": "BAD_EVENT_QUEUE_ID"}),
                asyncio.CancelledError(),
            ]
        )
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.receive(account)
        service.operations = history_tests.incoming_operations.Operations(
            service.db, service.http
        )
        await service.dispatch()
        await asyncio.gather(*list(service.tasks.values()))
    finally:
        await service.db.close()


@pytest.mark.parametrize("finished", [False, True])
def test_queue_renewal_retains_history_and_saved_batch(incoming, finished):
    _, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute(
        "UPDATE zulip_history_accounts SET before_id=100,finished=%s,registration=NULL",
        (finished,),
    )
    db.execute(
        "INSERT INTO zulip_history_batches(external_account_uuid,before_id) VALUES (%s,100)",
        (account["uuid"],),
    )
    before = db.execute(
        "SELECT before_id,cutoff_at,cutoff_id,finished FROM zulip_history_accounts"
    ).fetchone()
    batch = db.execute("SELECT id,before_id FROM zulip_history_batches").fetchone()
    asyncio.run(renew(incoming))
    assert (
        db.execute(
            "SELECT before_id,cutoff_at,cutoff_id,finished FROM zulip_history_accounts"
        ).fetchone()
        == before
    )
    assert (
        db.execute("SELECT id,before_id FROM zulip_history_batches").fetchone() == batch
    )


def test_renewal_recovers_gap_and_keeps_old_history_resumable(incoming):
    api, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute("UPDATE zulip_history_accounts SET before_id=100,registration=NULL")
    asyncio.run(renew(incoming, 1000))

    async def run():
        async with history_tests.worker(
            incoming, {99: history_tests.message(99), 750: history_tests.message(750)}
        ) as (service, _):
            for _ in range(20):
                jobs = await service.db.sql(
                    history_tests.zulip_history.SELECT_JOBS_SQL, ([], 1)
                )
                if not jobs:
                    break
                await service.step(jobs[0])
                history_tests.incoming_tests.publish()
            else:
                raise AssertionError("History did not finish")

    asyncio.run(run())
    history_tests.incoming_tests.publish()
    assert {
        m["payload"]["content"] for m in api.get("/v1/messenger/messages/").json()
    } == {"Hello"}
    assert db.execute(
        "SELECT provider_identity FROM messenger_messages WHERE project_id=%s ORDER BY provider_identity",
        (account["project_id"],),
    ).fetchall() == [("750",), ("99",)]
    assert db.execute(
        "SELECT cutoff_id,before_id,finished FROM zulip_history_accounts"
    ).fetchone() == (500, 0, True)


async def drain_all(incoming, messages, limit=100):
    _, _, account, *_ = incoming
    async with history_tests.worker(incoming, messages) as (service, _):
        for _ in range(limit):
            jobs = await service.db.sql(
                history_tests.zulip_history.SELECT_JOBS_SQL, ([], 1)
            )
            if not jobs:
                return
            await service.step(jobs[0])
            history_tests.incoming_tests.publish()
        raise AssertionError("History did not finish")


def test_completed_coverage_survives_repeated_renewal_and_recovers_old_state(incoming):
    api, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    asyncio.run(drain_all(incoming, {101: history_tests.message(101)}))
    coverage = db.execute(
        "SELECT cutoff_at,cutoff_id,before_id,finished FROM zulip_history_accounts"
    ).fetchone()
    asyncio.run(renew(incoming, 750, streams=(42, 43)))
    asyncio.run(renew(incoming, 1000, streams=(42, 43)))
    assert (
        db.execute(
            "SELECT cutoff_at,cutoff_id,before_id,finished FROM zulip_history_accounts"
        ).fetchone()
        == coverage
    )
    # New service instance: recovery is durable rather than an in-memory task.
    asyncio.run(
        drain_all(
            incoming,
            {
                55: history_tests.message(55, stream_id=43),
                101: history_tests.message(101, flags=["read", "starred"]),
                750: history_tests.message(750),
                1001: history_tests.message(1001),  # Beyond this registration boundary.
            },
        )
    )
    history_tests.incoming_tests.publish()
    assert (
        db.execute(
            "SELECT cutoff_at,cutoff_id,before_id,finished FROM zulip_history_accounts"
        ).fetchone()
        == coverage
    )
    assert db.execute(
        "SELECT provider_identity FROM messenger_messages WHERE project_id=%s ORDER BY provider_identity",
        (account["project_id"],),
    ).fetchall() == [("101",), ("55",), ("750",)]
    assert not db.execute("SELECT 1 FROM zulip_history_recovery_accounts").fetchall()
    messages = api.get("/v1/messenger/messages/").json()
    assert len(messages) == 3
    updated = next(item for item in messages if item["read"])
    assert updated["starred"]


def test_recovery_and_main_history_both_advance_after_recent_gap(incoming):
    _, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute("UPDATE zulip_history_accounts SET before_id=100,registration=NULL")
    db.execute(
        "INSERT INTO zulip_history_batches(external_account_uuid,before_id) VALUES (%s,100)",
        (account["uuid"],),
    )
    asyncio.run(renew(incoming))

    async def run():
        messages = {i: history_tests.message(i) for i in range(1, 401)}
        messages[750] = history_tests.message(750)
        async with history_tests.worker(incoming, messages) as (service, _):
            for _ in range(10):
                jobs = await service.db.sql(
                    history_tests.zulip_history.SELECT_JOBS_SQL, ([], 1)
                )
                await service.step(jobs[0])
                history_tests.incoming_tests.publish()

    asyncio.run(run())
    assert (
        db.execute("SELECT before_id FROM zulip_history_accounts").fetchone()[0] < 100
    )
    assert (
        db.execute("SELECT before_id FROM zulip_history_recovery_accounts").fetchone()[
            0
        ]
        < 1001
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_messages WHERE provider_identity='750' AND project_id=%s",
        (account["project_id"],),
    ).fetchone() == (1,)


def test_explicit_full_resync_can_still_reset_original_history(incoming):
    _, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute(
        "UPDATE zulip_history_accounts SET before_id=100,finished=true,registration=NULL"
    )
    history_tests.request(db, account, cutoff_id=900)
    assert db.execute(
        "SELECT cutoff_id,before_id,finished FROM zulip_history_accounts"
    ).fetchone() == (900, 901, False)


def test_old_selected_recovery_cannot_recreate_a_batch_after_renewal(incoming):
    _, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute("UPDATE zulip_history_accounts SET before_id=100,registration=NULL")
    asyncio.run(renew(incoming, 750))

    async def run():
        async with history_tests.worker(incoming, {}) as (service, _):
            await service.step(await history_tests.job(service, account))  # Catalog.
            stale = await history_tests.job(service, account)
            if not stale["_recovery"]:
                await service.step(stale)  # The scheduler gives main work its turn.
                stale = await history_tests.job(service, account)
            assert stale["_recovery"] and stale["registration"] is None
            await renew(incoming, 1000)
            await service.step(stale)
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_history_recovery_batches"
            )
            state = await service.db.sql(
                "SELECT cutoff_id,before_id FROM zulip_history_recovery_accounts"
            )
            assert state == [{"cutoff_id": 1000, "before_id": 1001}]

    asyncio.run(run())


def test_different_subscription_identity_does_not_inherit_old_frontier(incoming):
    _, db, account, *_ = incoming
    history_tests.request(db, account, cutoff_id=500)
    db.execute("UPDATE zulip_history_accounts SET before_id=100,registration=NULL")
    db.execute(
        "INSERT INTO zulip_history_batches(external_account_uuid,before_id) VALUES (%s,100)",
        (account["uuid"],),
    )
    db.execute(
        zulip_incoming.SAVE_SUBSCRIPTION_SQL,
        (
            account["uuid"],
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            "different-identity",
            account["settings"]["server_url"],
            "expired-subscription",
            -1,
            90,
        ),
    )
    asyncio.run(renew(incoming))
    assert db.execute(
        "SELECT before_id,cutoff_id,finished FROM zulip_history_accounts"
    ).fetchone() == (1001, 1000, False)
    assert not db.execute("SELECT 1 FROM zulip_history_batches").fetchall()


def test_failed_registration_commit_does_not_poll_an_uncommitted_queue(incoming):
    _, db, account, _, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = mock.Mock(
            request=mock.AsyncMock(
                side_effect=[
                    {
                        "queue_id": "rolled-back",
                        "last_event_id": -1,
                        "max_message_id": 500,
                        "subscriptions": [],
                    },
                    {
                        "queue_id": "committed",
                        "last_event_id": -1,
                        "max_message_id": 1000,
                        "subscriptions": [],
                    },
                ]
            )
        )
        service.poll_http = mock.Mock(
            request=mock.AsyncMock(side_effect=asyncio.CancelledError())
        )
        admission = service.admission
        failed = False

        @contextlib.asynccontextmanager
        async def fail_once():
            nonlocal failed
            async with admission() as value:
                yield value
                if not failed:
                    failed = True
                    raise psycopg.OperationalError("Lost registration transaction")

        service.admission = fail_once
        try:
            with pytest.raises(asyncio.CancelledError):
                await service.receive(account)
            assert service.http.request.await_count == 2
            assert (
                service.poll_http.request.call_args.args[3]["queue_id"] == "committed"
            )
            assert await service.db.sql(
                "SELECT queue_id FROM zulip_incoming_subscriptions WHERE external_account_uuid=%s",
                (account["uuid"],),
            ) == [{"queue_id": "committed"}]
        finally:
            await service.db.close()

    asyncio.run(run())


@pytest.mark.parametrize("deferred", [False, True])
def test_unrelated_initial_job_cannot_starve_ready_recovery(incoming, deferred):
    _, db, account, *_ = incoming
    peer, other_context = history_tests.incoming_tests.peer_account(incoming, 8)
    other = other_context[2]
    try:
        history_tests.request(db, account, cutoff_id=500)
        db.execute(
            "UPDATE zulip_history_accounts SET finished=true,before_id=0,registration=NULL"
        )
        asyncio.run(renew(incoming, 750))
        db.execute(
            "UPDATE zulip_history_recovery_accounts SET registration=NULL,last_run_at=now()-interval '2 minutes'"
        )
        history_tests.request(db, other, cutoff_id=1000)
        db.execute(
            "UPDATE zulip_history_accounts SET registration=NULL,last_run_at=now()-interval '1 minute' WHERE external_account_uuid=%s",
            (other["uuid"],),
        )
        if deferred:
            db.execute(
                "UPDATE zulip_history_accounts SET error='provider unavailable',next_attempt_at=now()+interval '5 seconds' WHERE external_account_uuid=%s",
                (other["uuid"],),
            )

        async def run():
            async with history_tests.worker(incoming, {}) as (service, _):
                selected = await service.db.sql(
                    history_tests.zulip_history.SELECT_JOBS_SQL, ([], 1)
                )
                assert [(item["uuid"], item["_recovery"]) for item in selected] == [
                    (account["uuid"], True)
                ]

        asyncio.run(run())
    finally:
        peer.delete()


def test_api_unlink_can_finish_during_history_merge_without_restoring_access(incoming):
    api, db, account, _, _ = incoming
    history_tests.incoming_tests.apply(incoming, history_tests.incoming_tests.event())
    history_tests.request(db, account, cutoff_id=500)
    db.execute("UPDATE zulip_history_accounts SET registration=NULL")

    async def run():
        async with history_tests.worker(
            incoming, {102: history_tests.message(102)}
        ) as (service, _):
            entered, release = asyncio.Event(), asyncio.Event()
            merge = service.operations.merge_batch

            async def pause(*args):
                entered.set()
                await release.wait()
                return await merge(*args)

            task = unlink = None
            with mock.patch.object(service.operations, "merge_batch", pause):
                try:
                    task = asyncio.create_task(
                        service.step(await history_tests.job(service, account))
                    )
                    await asyncio.wait_for(entered.wait(), 3)
                    unlink = asyncio.create_task(
                        asyncio.to_thread(
                            api.delete,
                            f"/v1/messenger/external_accounts/{account['uuid']}",
                            permissions=("workspace.external_account.delete",),
                        )
                    )
                    response = await asyncio.wait_for(asyncio.shield(unlink), 1)
                    assert response.status_code in (200, 204), response.text
                finally:
                    release.set()
                    if task:
                        await asyncio.wait_for(task, 5)
                    if unlink:
                        await asyncio.wait_for(unlink, 5)

    asyncio.run(run())
    assert not api.get("/v1/messenger/messages/").json()
    assert not db.execute(
        "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity='102'",
        (account["project_id"],),
    ).fetchall()
