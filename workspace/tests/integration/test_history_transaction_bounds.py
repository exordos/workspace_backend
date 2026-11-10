# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Slow history staging must not fence realtime work indefinitely."""

import asyncio
import contextlib
import urllib.parse
from unittest import mock

import psycopg
import pytest

from workspace.bridges.zulip import incoming_operations, outgoing_operations
from workspace.services import zulip_incoming, zulip_outgoing
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


@contextlib.contextmanager
def paused_staging(table):
    entered, release = asyncio.Event(), asyncio.Event()
    execute = psycopg.AsyncConnection.execute

    async def delayed(connection, query, parameters=None, **kwargs):
        text = query if isinstance(query, str) else query.as_string(connection)
        if text.startswith("CREATE TEMP TABLE") and f'"history_{table}"' in text:
            entered.set()
            await release.wait()
        return await execute(connection, query, parameters, **kwargs)

    with mock.patch.object(psycopg.AsyncConnection, "execute", delayed):
        yield entered, release


async def realtime_topic(incoming):
    _, _, account, http, url = incoming
    database = zulip_outgoing.Database(url, size=1)
    await database.open()
    try:
        operations = incoming_operations.Operations(database, http)
        prepared = await history_tests.incoming_tests.prepare_dependencies(
            operations,
            database,
            account,
            {
                "type": "user_topic",
                "stream_id": 42,
                "topic_name": "General",
                "visibility_policy": 3,
            },
        )
        async with database.transaction() as connection:
            await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
            await operations.apply(connection, account, prepared)
    finally:
        await database.close()


def test_slow_staging_does_not_block_realtime_topic_update(incoming):
    _, db, account, *_ = incoming
    history_tests.incoming_tests.apply(incoming, history_tests.incoming_tests.event())
    history_tests.incoming_tests.drain()
    history_tests.request(db, account)
    db.execute("UPDATE zulip_history_accounts SET registration=NULL")

    async def run():
        async with history_tests.worker(
            incoming, {102: history_tests.message(102)}
        ) as (service, _):
            with paused_staging("messenger_user_topic_bindings") as (entered, release):
                task = asyncio.create_task(
                    service.step(await history_tests.job(service, account))
                )
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    await realtime_topic(incoming)
                finally:
                    release.set()
                    await asyncio.wait_for(task, 5)
            assert not await service.db.sql(
                "SELECT error FROM zulip_history_accounts WHERE external_account_uuid=%s AND error IS NOT NULL",
                (account["uuid"],),
            )

    asyncio.run(run())
    assert db.execute(
        "SELECT binding.notification_mode FROM messenger_user_topic_bindings binding "
        "JOIN messenger_topics topic ON topic.project_id=binding.project_id AND topic.uuid=binding.topic_uuid "
        "WHERE binding.project_id=%s AND binding.user_uuid=%s AND topic.provider_identity='42:General'",
        (account["project_id"], account["owner_user_uuid"]),
    ).fetchone() == ("follow",)
    assert db.execute(
        "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (2,)


@pytest.mark.parametrize("personal_only", [False, True])
def test_membership_can_change_during_staging_without_regranting_access(
    incoming, personal_only
):
    _, db, account, *_ = incoming
    history_tests.incoming_tests.apply(incoming, history_tests.incoming_tests.event())
    history_tests.incoming_tests.drain()
    history_tests.request(db, account)

    async def run():
        async with history_tests.worker(incoming, {}) as (service, _):
            messages = [history_tests.message(flags=["starred"])]
            current = await history_tests.job(service, account)
            batch = await (
                service.operations.prepare_flags(account, messages)
                if personal_only
                else service.operations.prepare_batch(current, messages)
            )
            table = (
                "messenger_message_placements"
                if personal_only
                else "messenger_user_topic_bindings"
            )

            async def merge():
                async with service.db.transaction() as connection:
                    if personal_only:
                        await service.operations.merge_personal_flags(connection, batch)
                    else:
                        await service.operations.merge_batch(connection, account, batch)

            with paused_staging(table) as (entered, release):
                task = asyncio.create_task(merge())
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    with db.transaction():
                        db.execute(
                            "SELECT uuid FROM messenger_stream_bindings WHERE project_id=%s "
                            "AND user_uuid=%s FOR UPDATE NOWAIT",
                            (account["project_id"], account["owner_user_uuid"]),
                        )
                        db.execute(
                            "UPDATE messenger_stream_bindings SET active=false,"
                            "membership_generation=membership_generation+1 WHERE project_id=%s "
                            "AND user_uuid=%s",
                            (account["project_id"], account["owner_user_uuid"]),
                        )
                finally:
                    release.set()
                    result = await asyncio.wait_for(
                        asyncio.gather(task, return_exceptions=True), 5
                    )
            assert isinstance(result[0], outgoing_operations.Pending)
            assert "Membership changed" in str(result[0])

    asyncio.run(run())
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_stars WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (0,)


def test_statement_timeout_rolls_back_page_and_releases_topic_for_retry(incoming):
    _, db, account, *_ = incoming
    history_tests.incoming_tests.apply(incoming, history_tests.incoming_tests.event())
    history_tests.incoming_tests.drain()
    history_tests.request(db, account)
    db.execute("UPDATE zulip_history_accounts SET registration=NULL")

    async def run():
        async with history_tests.worker(
            incoming, {102: history_tests.message(102)}, statement_timeout=1
        ) as (service, calls):
            merge = service.operations.merge_batch
            reached_sleep = False

            async def slow_merge(connection, current, batch):
                nonlocal reached_sleep
                await merge(connection, current, batch)
                reached_sleep = True
                await connection.execute("SELECT pg_sleep(3)")

            with mock.patch.object(service.operations, "merge_batch", slow_merge):
                await service.step(await history_tests.job(service, account))
            assert reached_sleep
            state = db.execute(
                "SELECT before_id,error FROM zulip_history_accounts WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone()
            assert state[0] == 1000001
            assert "statement timeout" in state[1]
            assert db.execute(
                "SELECT count(*) FROM zulip_history_batches WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone() == (1,)
            assert db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            ).fetchone() == (1,)
            assert not db.execute(
                "SELECT 1 FROM zulip_history_block_receipts WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone()
            await realtime_topic(incoming)
            history_tests.incoming_tests.publish()
            db.execute("UPDATE zulip_history_accounts SET next_attempt_at=now()")
            await service.step(await history_tests.job(service, account))
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_history_batches WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
            assert db.execute(
                "SELECT before_id,finished,error FROM zulip_history_accounts WHERE external_account_uuid=%s",
                (account["uuid"],),
            ).fetchone() == (0, True, None)
            assert db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            ).fetchone() == (2,)
            assert len([call for call in calls if call[1] == "/messages"]) == 1

    asyncio.run(run())


def test_history_timeout_preserves_a_stricter_connection_limit_after_reconnect(
    incoming,
):
    _, _, _, _, url = incoming
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query))
    query["options"] = query.get("options", "") + " -c statement_timeout=100"
    limited = urllib.parse.urlunsplit(
        parts._replace(query=urllib.parse.urlencode(query))
    )

    async def run():
        database = zulip_outgoing.Database(limited, size=1, statement_timeout=1)
        await database.open()
        try:
            assert await database.sql(
                "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
            ) == [{"setting": "100"}]
            connection = await database.connections.get()
            await connection.close()
            database.connections.put_nowait(connection)
            assert await database.sql(
                "SELECT setting FROM pg_settings WHERE name='statement_timeout'"
            ) == [{"setting": "100"}]
            with pytest.raises(psycopg.errors.QueryCanceled):
                await database.sql("SELECT pg_sleep(1)")
            assert await database.sql("SELECT 1 AS healthy") == [{"healthy": 1}]
        finally:
            await database.close()

    asyncio.run(run())
