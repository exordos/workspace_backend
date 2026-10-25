# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import contextlib
import copy
import datetime
import uuid
from unittest import mock

import pytest
from oslo_config import cfg

from workspace.bridges.zulip import history
from workspace.bridges.zulip import incoming_operations
from workspace.bridges.zulip import outgoing_operations
from workspace.common import mutation_journal_opts
from workspace.messenger_api import publication
from workspace.services import zulip_history
from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests


incoming = incoming_tests.incoming


def message(identifier=101, content="Hello", **changes):
    return {
        **incoming_tests.event(content, identifier)["message"],
        "timestamp": 1700000000,
        "reactions": [],
        **changes,
    }


def request(db, account, registration=None, queue="history-test"):
    db.execute(
        history.REQUEST_SQL,
        (
            account["uuid"],
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            account["external_account_id"],
            account["settings"]["server_url"],
            incoming_operations.json_value(registration or {}),
            queue,
            datetime.datetime.now(datetime.timezone.utc),
            [item["stream_id"] for item in registration["subscriptions"]]
            if registration and "subscriptions" in registration
            else None,
        ),
    )


@contextlib.asynccontextmanager
async def worker(incoming, messages, **options):
    _, _, account, _, url = incoming
    service = zulip_history.ZulipHistoryService(
        url, batch_size=3, queue_limit=6, **options
    )
    await service.db.open()
    calls = []

    async def http(_account, method, path, query=None, **kwargs):
        calls.append((method, path, query))
        if path == "/streams/42":
            return {"stream": {"stream_id": 42, "name": "Channel", "description": ""}}
        if path.startswith("/users/"):
            return {
                "user": {
                    "user_id": 7,
                    "full_name": "Sender",
                    "email": "user@example.com",
                }
            }
        if path.startswith("/messages/"):
            identifier = int(path.rsplit("/", 1)[1])
            if identifier not in messages:
                raise outgoing_operations.ApiError(404, {"msg": "Invalid message(s)"})
            return {"message": copy.deepcopy(messages[identifier])}
        assert path == "/messages"
        ordered = sorted(messages)
        anchor = (
            ordered[-1] if query["anchor"] == "newest" and ordered else query["anchor"]
        )
        if anchor == "newest":
            return {"messages": [], "found_oldest": True, "found_newest": True}
        before = (
            [value for value in ordered if value < anchor][-query["num_before"] :]
            if query["num_before"]
            else []
        )
        after = [value for value in ordered if value > anchor][: query["num_after"]]
        selected = (
            before
            + (
                [anchor]
                if anchor in messages and query.get("include_anchor", True)
                else []
            )
            + after
        )
        return {
            "messages": [copy.deepcopy(messages[value]) for value in selected],
            "found_oldest": not ordered
            or (bool(selected) and selected[0] == ordered[0])
            or anchor <= ordered[0],
            "found_newest": not ordered
            or (bool(selected) and selected[-1] == ordered[-1])
            or anchor >= ordered[-1],
        }

    service.http = mock.Mock(request=mock.AsyncMock(side_effect=http))
    service.operations = history.Operations(service.db, service.http)
    try:
        yield service, calls
    finally:
        await service.db.close()


async def job(service, account):
    jobs = await service.db.sql(zulip_history.SELECT_JOBS_SQL, ([], [], 100))
    return next(
        (row for row in jobs if row["external_account_uuid"] == account["uuid"]), None
    )


async def drain(service, account):
    for _ in range(100):
        current = await job(service, account)
        if current is None:
            return
        await service.step(current)
        errors = await service.db.sql(
            "SELECT error FROM zulip_history_intents WHERE failed"
        )
        assert not errors, errors
        state = await service.db.sql(
            "SELECT error FROM zulip_history_jobs WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        assert not state[0]["error"], state
    pytest.fail("History did not finish its bounded scan")


def test_history_import_resumes_and_repeated_scan_is_silent(incoming):
    _, db, account, _, _ = incoming
    incoming_tests.drain()
    before = incoming_tests.client_events(incoming)
    messages = {
        identifier: message(identifier, f"History {identifier}")
        for identifier in range(101, 109)
    }
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, calls):
            await service.step(await job(service, account))  # Empty registration.
            await service.step(await job(service, account))  # Latest page.
            assert len(await service.db.sql("SELECT 1 FROM zulip_history_intents")) == 3
            checkpoint = (
                await service.db.sql("SELECT before_id FROM zulip_history_jobs")
            )[0]["before_id"]
            assert checkpoint == 106
        async with worker(incoming, messages) as (service, calls):
            await drain(service, account)
            assert not [path for _, path, _ in calls if path.startswith("/messages/")]
            assert (
                len(
                    await service.db.sql(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                        (account["project_id"],),
                    )
                )
                == 8
            )
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_outgoing_intents WHERE project_id=%s",
                (account["project_id"],),
            )
        incoming_tests.publish()
        request(db, account, queue="replacement")
        async with worker(incoming, messages) as (service, calls):
            await drain(service, account)
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s",
                (account["project_id"],),
            )
            assert not [path for _, path, _ in calls if path.startswith("/messages/")]

    asyncio.run(run())
    incoming_tests.drain()
    assert len(incoming[0].get("/v1/messenger/messages/").json()) == 8
    assert incoming_tests.client_events(incoming) == before


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("public_position", [None, 0, -1])
def test_history_counters_preserve_a_realtime_publication(
    incoming, journal, public_position
):
    api, db, account, _, _ = incoming
    cfg.CONF.set_override("enabled", journal, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime() if journal else None

    def project():
        if runtime is None:
            incoming_tests.drain()
        else:
            journal_tests.drain(runtime, db)

    try:
        project()
        before = incoming_tests.client_events(incoming)
        request(db, account, {"subscriptions": [{"stream_id": 42, "name": "Channel"}]})

        async def run():
            async with worker(incoming, {101: message(), 102: message(102)}) as (
                service,
                _,
            ):
                await drain(service, account)

        asyncio.run(run())
        counters = db.execute(
            "SELECT uuid FROM messenger_projection_tasks WHERE project_id=%s "
            "AND task_kind='read_counters' ORDER BY created_at,uuid",
            (account["project_id"],),
        ).fetchall()
        assert len(counters) >= 2
        if public_position is not None:
            # A normal realtime invalidation can share this authoritative scope.
            db.execute(
                "UPDATE messenger_projection_tasks SET payload="
                "jsonb_set(payload,'{emit_public_event}','true') WHERE uuid=%s",
                (counters[public_position][0],),
            )
        project()
        messages = api.get("/v1/messenger/messages/").json()
        assert len(messages) == 2
        topic = api.get(
            "/v1/messenger/stream_topics/" + messages[0]["topic_uuid"]
        ).json()
        assert topic["unread_count"] == 2
        stream = api.get("/v1/messenger/streams/" + messages[0]["stream_uuid"]).json()
        assert stream["unread_count"] == 2
        assert bool(incoming_tests.client_events(incoming)[len(before) :]) == (
            public_position is not None
        )
    finally:
        if runtime is not None:
            runtime.close()
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


@pytest.mark.parametrize("provider_content", [None, "Previous provider spelling"])
def test_history_repairs_provider_content_without_changing_workspace_payload(
    incoming, provider_content
):
    _, db, account, _, _ = incoming
    original = message()
    incoming_tests.apply(incoming, {"type": "message", "message": original})
    with db.transaction():
        db.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
        db.execute(
            "UPDATE messenger_messages message SET content_uuid="
            "messenger_resolve_message_content(contents.workspace_content,%s) "
            "FROM messenger_message_contents contents "
            "WHERE contents.uuid=message.content_uuid AND message.project_id=%s",
            (provider_content, account["project_id"]),
        )
    incoming_tests.publish()
    request(db, account)

    async def run():
        async with worker(incoming, {101: original}) as (service, _):
            await drain(service, account)
            current = (
                await service.db.sql(
                    "SELECT message.content_revision,contents.workspace_content,"
                    "contents.provider_content FROM messenger_messages message "
                    "JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid "
                    "WHERE message.project_id=%s AND message.provider_identity='101'",
                    (account["project_id"],),
                )
            )[0]
            assert current == {
                "content_revision": 3,
                "workspace_content": {"kind": "markdown", "content": "Hello"},
                "provider_content": "Hello",
            }
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_outgoing_intents WHERE project_id=%s",
                (account["project_id"],),
            )

    asyncio.run(run())


@pytest.mark.parametrize("journal", [False, True])
def test_history_repairs_content_reactions_flags_and_scoped_absence(incoming, journal):
    _, db, account, _, _ = incoming
    first = message(
        reactions=[
            {
                "user_id": 7,
                "emoji_name": "smile",
                "emoji_code": "1f604",
                "reaction_type": "unicode_emoji",
            }
        ]
    )
    second = message(102, "Will disappear")
    incoming_tests.apply(
        incoming,
        {"type": "message", "message": first},
        {"type": "message", "message": second},
    )
    incoming_tests.drain()
    cfg.CONF.set_override("enabled", journal, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime() if journal else None
    try:
        if runtime is not None:
            journal_tests.drain(runtime, db)
        before = incoming_tests.client_events(incoming)
        changed = message(
            content="Updated",
            flags=["read", "starred"],
            last_edit_timestamp=first["timestamp"] + 5,
        )
        request(db, account)

        async def run():
            async with worker(incoming, {101: changed}) as (service, _):
                await drain(service, account)
                root = (
                    await service.db.sql(
                        "SELECT payload,reactions FROM messenger_messages_with_content_v1 WHERE project_id=%s AND provider_identity=%s",
                        (account["project_id"], "101"),
                    )
                )[0]
                assert root["payload"]["content"] == "Updated"
                assert root["reactions"] == {}
                assert not await service.db.sql(
                    "SELECT 1 FROM messenger_message_reaction_facts WHERE project_id=%s",
                    (account["project_id"],),
                )
                assert not await service.db.sql(
                    "SELECT 1 FROM messenger_messages WHERE project_id=%s AND deleted_at IS NOT NULL",
                    (account["project_id"],),
                )

        asyncio.run(run())
        if runtime is None:
            incoming_tests.drain()
        else:
            journal_tests.drain(runtime, db)
        assert incoming_tests.client_events(incoming) == before
        visible = incoming[0].get("/v1/messenger/messages/").json()
        assert len(visible) == 1
        assert visible[0]["starred"] is True
        assert db.execute(
            "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s AND provider_identity=%s",
            (account["project_id"], "101:7"),
        ).fetchone()[0]
    finally:
        if runtime is not None:
            runtime.close()
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def test_repeated_registration_keeps_cursors_and_does_not_duplicate_work(incoming):
    _, db, account, _, _ = incoming
    request(db, account)
    db.execute(
        "UPDATE zulip_history_jobs SET newest_id=1000,before_id=800,target_id=1200,repair_before_id=400 WHERE external_account_uuid=%s",
        (account["uuid"],),
    )
    request(db, account, queue="next-queue")
    row = db.execute(
        "SELECT newest_id,before_id,target_id,repair_before_id FROM zulip_history_jobs WHERE external_account_uuid=%s",
        (account["uuid"],),
    ).fetchone()
    assert tuple(row) == (1000, 800, 1200, 400)
    assert (
        db.execute(
            "SELECT count(*) FROM zulip_history_jobs WHERE external_account_uuid=%s",
            (account["uuid"],),
        ).fetchone()[0]
        == 1
    )


def test_stale_snapshot_does_not_overwrite_a_newer_realtime_change(incoming):
    _, db, account, _, _ = incoming
    original = message()
    incoming_tests.apply(incoming, {"type": "message", "message": original})
    request(db, account)
    remote = message(
        content="Current", last_edit_timestamp=original["timestamp"] + 3, flags=["read"]
    )

    async def run():
        async with worker(incoming, {101: remote}) as (service, calls):
            started = (await service.db.sql("SELECT clock_timestamp() AS value"))[0][
                "value"
            ]
            expected = (await service.operations.snapshots(account, [101]))["101"]
            async with service.db.transaction() as connection:
                await service.enqueue(
                    connection,
                    account,
                    {
                        "type": "message",
                        "message": message(
                            content="Stale",
                            last_edit_timestamp=original["timestamp"] + 1,
                        ),
                        "history": True,
                    },
                    expected,
                    started,
                )
            # Simulate a committed realtime edit while the old page was in flight.
            prepared = await incoming_tests.prepare_dependencies(
                service.operations,
                service.db,
                account,
                {"type": "message", "message": remote},
            )
            async with service.db.transaction() as connection:
                await connection.execute(
                    "SELECT set_config('workspace.zulip_incoming','on',true)"
                )
                await service.operations.apply(connection, account, prepared)
            await drain(service, account)
            assert [path for _, path, _ in calls if path == "/messages/101"]
            current = (
                await service.db.sql(
                    "SELECT payload FROM messenger_messages_with_content_v1 WHERE project_id=%s AND provider_identity=%s",
                    (account["project_id"], "101"),
                )
            )[0]
            assert current["payload"]["content"] == "Current"

    asyncio.run(run())


def test_snapshot_flags_do_not_mark_truncated_old_unreads_as_read(incoming):
    _, db, account, _, _ = incoming
    current = message()
    incoming_tests.apply(incoming, {"type": "message", "message": current})
    request(
        db,
        account,
        {
            "unread_msgs": {"old_unreads_missing": True, "streams": []},
            "starred_messages": [101],
        },
    )

    async def run():
        async with worker(incoming, {101: message(flags=["starred"])}) as (service, _):
            await drain(service, account)
            row = (
                await service.db.sql(
                    "SELECT read_at,starred FROM messenger_user_message_states WHERE project_id=%s",
                    (account["project_id"],),
                )
            )[0]
            assert row["read_at"] is None and row["starred"]

    asyncio.run(run())


def test_publication_prioritizes_realtime_and_preserves_each_entity_order(incoming):
    _, db, account, _, _ = incoming
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.execute("SELECT set_config('workspace.zulip_history','on',false)")
    for identifier in (first, third):
        db.execute(
            publication.ENQUEUE_SQL,
            (
                uuid.uuid4(),
                account["project_id"],
                identifier,
                "message.updated",
                [],
                "{}",
                "{}",
            ),
        )
    db.execute("SELECT set_config('workspace.zulip_history','off',false)")
    for identifier in (second, third):
        db.execute(
            publication.ENQUEUE_SQL,
            (
                uuid.uuid4(),
                account["project_id"],
                identifier,
                "message.updated",
                [],
                "{}",
                "{}",
            ),
        )
    with db.cursor() as cursor:
        cursor.execute(publication.SELECT_PENDING_SQL, (account["project_id"],))
        # Realtime promotes the preceding historical snapshot of its own entity.
        assert [row[3] for row in cursor.fetchall()] == [third, second, third, first]


def test_queue_replacement_recovers_a_gap_even_when_realtime_has_a_newer_id(incoming):
    _, db, account, _, _ = incoming
    messages = {value: message(value, f"Old {value}") for value in range(101, 106)}
    request(db, account)

    async def run():
        async with worker(incoming, messages) as (service, _):
            await drain(service, account)
        incoming_tests.publish()
        messages.update(
            {value: message(value, f"New {value}") for value in range(106, 115)}
        )
        # Seeing the newest ID in realtime does not confirm the intervening IDs.
        await asyncio.to_thread(
            incoming_tests.apply,
            incoming,
            {"type": "message", "message": messages[114]},
        )
        request(db, account, queue="first-reset")
        async with worker(incoming, messages) as (service, _):
            await service.step(await job(service, account))
            await service.step(await job(service, account))
            current = (
                await service.db.sql(
                    "SELECT newest_id,target_id FROM zulip_history_jobs WHERE external_account_uuid=%s",
                    (account["uuid"],),
                )
            )[0]
            assert current == {"newest_id": 105, "target_id": 114}
            messages[115] = message(115, "Another reset")
            request(db, account, queue="second-reset")
            await drain(service, account)
            ids = await service.db.sql(
                "SELECT provider_identity FROM messenger_messages WHERE project_id=%s ORDER BY provider_identity",
                (account["project_id"],),
            )
            assert {int(row["provider_identity"]) for row in ids} == set(
                range(101, 116)
            )

    asyncio.run(run())


def test_history_observes_access_per_account_and_does_not_repeat_shared_writes(
    incoming,
):
    api, db, account, _, _ = incoming
    linked, peer = incoming_tests.peer_account(incoming)
    subscription = {
        "type": "subscription",
        "op": "add",
        "subscriptions": [{"stream_id": 42, "name": "Channel"}],
    }
    incoming_tests.apply(incoming, subscription)
    incoming_tests.apply(peer, subscription)
    request(db, account)
    first = message(flags=["starred"])
    try:

        async def run():
            async with worker(incoming, {101: first}) as (service, _):
                await drain(service, account)
            incoming_tests.publish()
            assert not api.get(
                "/v1/messenger/messages/", user=linked.owner_user_uuid
            ).json()
            before = db.execute(
                "SELECT updated_at FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            ).fetchone()[0]
            request(db, peer[2])
            async with worker(peer, {101: message(flags=["read"])}) as (service, _):
                await drain(service, peer[2])
            assert (
                db.execute(
                    "SELECT updated_at FROM messenger_messages WHERE project_id=%s",
                    (account["project_id"],),
                ).fetchone()[0]
                == before
            )
            incoming_tests.publish()
            assert api.get("/v1/messenger/messages/").json()[0]["starred"]
            other = api.get(
                "/v1/messenger/messages/", user=linked.owner_user_uuid
            ).json()[0]
            assert other["read"] and not other["starred"]
            request(db, account, queue="lost-access")
            async with worker(incoming, {}) as (service, _):
                await drain(service, account)
            incoming_tests.publish()
            assert not api.get("/v1/messenger/messages/").json()
            assert (
                len(
                    api.get(
                        "/v1/messenger/messages/", user=linked.owner_user_uuid
                    ).json()
                )
                == 1
            )

        asyncio.run(run())
    finally:
        db.execute("DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (linked.uuid,))


def test_history_pauses_for_publication_and_realtime_backlogs(incoming):
    _, db, account, _, _ = incoming
    request(db, account)
    db.execute(
        "INSERT INTO messenger_publication_outbox(uuid,project_id,entity_uuid,kind,recipients,payload) VALUES (%s,%s,%s,'message.updated','{}','{}')",
        (uuid.uuid4(), account["project_id"], uuid.uuid4()),
    )

    async def run():
        async with worker(incoming, {101: message()}, publication_limit=1) as (
            service,
            calls,
        ):
            await service.step(await job(service, account))
            assert calls == []
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")
            await service.db.sql(
                "DELETE FROM messenger_publication_outbox WHERE project_id=%s",
                (account["project_id"],),
            )
            await service.db.sql("UPDATE zulip_history_jobs SET next_attempt_at=now()")
            await service.db.sql(
                "INSERT INTO zulip_incoming_events(project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,queue_id,event_id,payload,received_at) VALUES (%s,%s,%s,%s,%s,'busy',1,'{}',now()-interval '1 minute')",
                (
                    account["project_id"],
                    account["uuid"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    account["external_account_id"],
                ),
            )
            await service.step(await job(service, account))
            assert calls == []
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")

    asyncio.run(run())


def test_pending_outbox_work_defers_history_without_http_requests(incoming):
    _, db, account, _, _ = incoming
    original = message()
    incoming_tests.apply(incoming, {"type": "message", "message": original})
    request(db, account)
    root = db.execute(
        "SELECT uuid FROM messenger_messages WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data) VALUES (%s,'message',%s,%s,%s,'update','{}')",
        (account["project_id"], root, f"message:{root}", account["uuid"]),
    )

    async def run():
        async with worker(
            incoming,
            {
                101: message(
                    content="Provider edit",
                    last_edit_timestamp=original["timestamp"] + 1,
                )
            },
        ) as (service, calls):
            started = (await service.db.sql("SELECT clock_timestamp() AS value"))[0][
                "value"
            ]
            expected = (await service.operations.snapshots(account, [101]))["101"]
            async with service.db.transaction() as connection:
                await service.enqueue(
                    connection,
                    account,
                    {"type": "message", "message": original, "history": True},
                    expected,
                    started,
                )
            intent = (
                await service.db.sql(
                    zulip_history.SELECT_INTENT_SQL, (account["uuid"],)
                )
            )[0]
            await service.process(account, intent)
            assert calls == []
            assert await service.db.sql("SELECT 1 FROM zulip_history_intents")
            await service.db.sql(
                "UPDATE zulip_outgoing_intents SET status='done' WHERE project_id=%s",
                (account["project_id"],),
            )
            await service.db.sql(
                "UPDATE zulip_history_intents SET next_attempt_at=now()"
            )
            await drain(service, account)

    asyncio.run(run())


def test_batch_comparison_uses_a_bounded_number_of_queries(incoming):
    _, db, account, _, _ = incoming
    messages = [
        message(identifier, f"Plain {identifier}") for identifier in range(101, 111)
    ]
    incoming_tests.apply(
        incoming, *({"type": "message", "message": item} for item in messages)
    )

    async def run():
        async with worker(incoming, {item["id"]: item for item in messages}) as (
            service,
            calls,
        ):
            service.db.sql = mock.AsyncMock(wraps=service.db.sql)
            assert await service.operations.differences(account, messages) == []
            assert service.db.sql.await_count <= 6
            assert calls == []

    asyncio.run(run())


def test_history_flags_do_not_refetch_siblings_after_counter_updates(incoming):
    _, db, account, _, _ = incoming
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 5
    messages = {
        i: message(i, flags=["read"], sender_id=8, timestamp=now)
        for i in range(101, 104)
    }
    incoming_tests.apply(
        incoming,
        {
            "type": "realm_user",
            "op": "add",
            "person": {
                "user_id": 8,
                "full_name": "Other sender",
                "email": "other@example.com",
            },
        },
    )
    incoming_tests.apply(
        incoming, *({"type": "message", "message": item} for item in messages.values())
    )
    for item in messages.values():
        item["flags"] = []
    request(db, account)
    db.execute("UPDATE zulip_history_jobs SET registration=NULL")

    async def run():
        async with worker(incoming, messages) as (service, calls):
            for _ in range(100):
                current = await job(service, account)
                if current is None:
                    break
                await service.step(current)
                incoming_tests.drain()
                # Counter projection may touch the shared membership between
                # history steps even when its resulting counters are unchanged.
                db.execute(
                    "UPDATE messenger_stream_bindings SET updated_at=clock_timestamp() "
                    "WHERE project_id=%s AND user_uuid=%s",
                    (account["project_id"], account["owner_user_uuid"]),
                )
            assert await job(service, account) is None
            assert not [path for _, path, _ in calls if path.startswith("/messages/")]
            assert (
                await service.db.sql(
                    "SELECT count(*) AS count FROM messenger_user_message_states "
                    "WHERE user_uuid=%s AND read_at IS NULL",
                    (account["owner_user_uuid"],),
                )
            )[0]["count"] == 3

    asyncio.run(run())


def test_failed_page_commit_keeps_both_work_and_cursor_unadvanced(
    incoming, monkeypatch
):
    _, db, account, _, _ = incoming
    request(db, account)
    db.execute("UPDATE zulip_history_jobs SET registration=NULL")

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            original = service.db.transaction

            @contextlib.asynccontextmanager
            async def fail_commit():
                async with original() as connection:
                    yield connection
                    raise RuntimeError("simulated lost transaction")

            with monkeypatch.context() as patch:
                patch.setattr(service.db, "transaction", fail_commit)
                await service.step(await job(service, account))
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")
            state = (
                await service.db.sql(
                    "SELECT before_id,newest_id FROM zulip_history_jobs"
                )
            )[0]
            assert state == {"before_id": None, "newest_id": 0}
            await service.db.sql("UPDATE zulip_history_jobs SET next_attempt_at=now()")
            await drain(service, account)
            assert await service.db.sql(
                "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            )

    asyncio.run(run())


@pytest.mark.isolated_schema
@pytest.mark.parametrize("age_days", [0, 4])
def test_history_process_is_silent_for_an_actual_websocket_client(
    incoming, tmp_path, age_days
):
    import json
    import signal
    import socket
    import sys

    from aiohttp import web
    import websockets
    from workspace.messenger_api import websocket_service
    from workspace.messenger_api.dm import external_models

    _, db, original, _, url = incoming

    async def run():
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        payload = message(
            content="From the history process", timestamp=now - age_days * 86400
        )
        transports = []

        async def provider(request):
            transports.append(request.transport)
            assert not db.execute(
                "SELECT 1 FROM pg_stat_activity WHERE application_name='workspace-zulip-history' AND state='idle in transaction'"
            ).fetchone()
            if request.path.endswith("/messages"):
                return web.json_response(
                    {"messages": [payload], "found_oldest": True, "found_newest": True}
                )
            return web.json_response(
                {"stream": {"stream_id": 42, "name": "Channel", "description": ""}}
            )

        app = web.Application()
        app.router.add_get("/api/v1/messages", provider)
        app.router.add_get("/api/v1/streams/42", provider)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        provider_row = external_models.Provider.register("127.0.0.1")
        account = {
            **original,
            "provider_uuid": provider_row.uuid,
            "hostname": "127.0.0.1",
            "settings": {
                **original["settings"],
                "server_url": f"http://127.0.0.1:{port}",
            },
        }
        db.execute(
            "UPDATE m_external_accounts_v2 SET provider_uuid=%s,settings=%s WHERE uuid=%s",
            (
                provider_row.uuid,
                incoming_operations.json_value(account["settings"]),
                account["uuid"],
            ),
        )
        request(db, account)
        config = tmp_path / "history.conf"
        config.write_text(
            f"[db]\nconnection_url = {url}\n[zulip_history]\npoll_interval = 0.02\n"
        )
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        websocket_port = probe.getsockname()[1]
        probe.close()
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=20,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.02,
        )
        server._authenticator.authenticate = lambda token: (
            account["owner_user_uuid"],
            account["project_id"],
        )
        websocket_task = asyncio.create_task(server.serve("127.0.0.1", websocket_port))
        child = None
        try:
            async with asyncio.timeout(20):
                while server._stop_event is None:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.02)
                async with websockets.connect(
                    f"ws://127.0.0.1:{websocket_port}/v1/events/ws",
                    subprotocols=["workspace.events.v1", "bearer.test"],
                ) as websocket:
                    while json.loads(await websocket.recv()).get("type") != "ready":
                        pass
                    child = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "workspace.cmd.zulip_history",
                        "--config-file",
                        str(config),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    while not db.execute(
                        "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                        (account["project_id"],),
                    ).fetchone():
                        assert child.returncode is None
                        await asyncio.sleep(0.02)
                    await asyncio.to_thread(incoming_tests.drain)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(websocket.recv(), 0.2)
                    payload["content"] = "Edited through recovery"
                    payload["last_edit_timestamp"] = now + 5
                    request(db, account, queue="recovered")
                    while (
                        db.execute(
                            "SELECT payload->>'content' FROM messenger_messages_with_content_v1 WHERE project_id=%s",
                            (account["project_id"],),
                        ).fetchone()[0]
                        != payload["content"]
                    ):
                        await asyncio.sleep(0.02)
                    await asyncio.to_thread(incoming_tests.drain)
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(websocket.recv(), 0.2)
                    # A new realtime message must still reach this same connection.
                    realtime = (incoming[0], db, account, incoming[3], url)
                    await asyncio.to_thread(
                        incoming_tests.apply,
                        realtime,
                        incoming_tests.event("Realtime stays visible", 102),
                    )
                    await asyncio.to_thread(incoming_tests.drain)
                    while True:
                        frame = json.loads(await websocket.recv())
                        if frame.get("object_type") == "message":
                            assert frame["action"] == "created"
                            assert (
                                frame["payload"]["payload"]["content"]
                                == "Realtime stays visible"
                            )
                            break
                    assert len(set(transports)) < len(transports)
        finally:
            if child is not None and child.returncode is None:
                child.send_signal(signal.SIGINT)
                _, stderr = await asyncio.wait_for(child.communicate(), 10)
                assert child.returncode == 0, stderr.decode()
            server.stop()
            await websocket_task
            await runner.cleanup()

    asyncio.run(run())


def test_global_history_queue_limit_prevents_more_page_downloads(incoming):
    _, db, account, _, _ = incoming
    linked, peer = incoming_tests.peer_account(incoming)
    request(db, account)
    request(db, peer[2])
    try:

        async def run():
            async with worker(incoming, {101: message()}) as (service, calls):
                async with service.db.transaction() as connection:
                    for identifier in range(201, 207):
                        await service.enqueue(
                            connection,
                            peer[2],
                            {
                                "type": "message",
                                "message": message(identifier),
                                "history": True,
                            },
                            None,
                            datetime.datetime.now(datetime.timezone.utc),
                        )
                await service.step(await job(service, account))
                assert calls == []
                assert (
                    len(await service.db.sql("SELECT 1 FROM zulip_history_intents"))
                    == 6
                )

        asyncio.run(run())
    finally:
        db.execute("DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (linked.uuid,))


@pytest.mark.parametrize("known", [False, True])
def test_history_does_not_subscribe_an_account_to_old_channels(incoming, known):
    _, db, account, _, _ = incoming
    if known:
        incoming_tests.apply(incoming, {"type": "message", "message": message()})
        incoming_tests.apply(
            incoming,
            {
                "type": "subscription",
                "op": "remove",
                "subscriptions": [{"stream_id": 42}],
            },
        )
    request(db, account, {"subscriptions": []})

    async def run():
        async with worker(incoming, {101: message()}) as (service, _):
            await drain(service, account)
            assert not await service.db.sql(
                "SELECT 1 FROM messenger_stream_bindings WHERE project_id=%s AND active",
                (account["project_id"],),
            )
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")

    asyncio.run(run())


def test_registration_recovers_a_missed_attachment_removal(
    incoming, tmp_path, monkeypatch
):
    _, db, account, http, _ = incoming
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    http.download = mock.AsyncMock(return_value=(b"file content", "text/plain"))
    incoming_tests.apply(
        incoming,
        {
            "type": "message",
            "message": message(content="[file](/user_uploads/1/a/file.txt)"),
        },
        {
            "type": "attachment",
            "op": "add",
            "attachment": {"id": 123, "path_id": "1/a/file.txt", "name": "file.txt"},
        },
    )
    assert db.execute(
        "SELECT 1 FROM m_workspace_files WHERE project_id=%s", (account["project_id"],)
    ).fetchone()
    request(db, account, {"attachments": []})

    async def run():
        async with worker(incoming, {}) as (service, _):
            # Exercise only catalog recovery; message history has its own scope checks.
            await service.db.sql(
                "UPDATE zulip_history_jobs SET newest_id=101,before_id=0,backfill_done=true,completed_at=requested_at,repair_after=now()+interval '1 day'"
            )
            await drain(service, account)
            assert not await service.db.sql(
                "SELECT 1 FROM m_workspace_files WHERE project_id=%s",
                (account["project_id"],),
            )
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_incoming_attachments WHERE project_id=%s",
                (account["project_id"],),
            )
            # Cleanup intentions contain provider_deleted and do not resend a deletion.
            assert not await service.db.sql(
                "SELECT 1 FROM zulip_outgoing_intents WHERE project_id=%s AND NOT(data @> '{\"provider_deleted\":true}')",
                (account["project_id"],),
            )

    asyncio.run(run())


def test_failed_catalog_removal_does_not_keep_registration_running(incoming):
    _, db, account, _, _ = incoming
    incoming_tests.apply(incoming, {"type": "message", "message": message()})
    request(db, account, {"subscriptions": []})

    async def run():
        async with worker(incoming, {}) as (service, _):
            await service.db.sql(
                "UPDATE zulip_history_jobs SET newest_id=101,before_id=0,"
                "backfill_done=true,completed_at=requested_at,"
                "repair_after=now()+interval '1 day'"
            )
            await service.step(await job(service, account))
            intent = (await service.db.sql("SELECT * FROM zulip_history_intents"))[0]
            assert intent["entity_key"] == "subscription:42"
            with mock.patch.object(
                service, "prepare", side_effect=ValueError("permanent catalog failure")
            ):
                await service.step(await job(service, account))
            for _ in range(5):
                current = await job(service, account)
                if current is None:
                    break
                await service.step(current)
            assert await job(service, account) is None
            failed = await service.db.sql("SELECT * FROM zulip_history_intents")
            assert len(failed) == 1
            assert failed[0]["failed"]
            assert failed[0]["error"] == "permanent catalog failure"
            assert not (
                await service.db.sql("SELECT registration FROM zulip_history_jobs")
            )[0]["registration"]

    asyncio.run(run())


def test_newer_catalog_event_is_not_repeatedly_reversed_by_registration(incoming):
    _, db, account, _, _ = incoming
    incoming_tests.apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 42, "name": "Channel"}],
        },
    )
    request(db, account, {"subscriptions": []})
    db.execute(
        "INSERT INTO zulip_incoming_events(project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,queue_id,event_id,payload,ordering_keys,processed_at) VALUES (%s,%s,%s,%s,%s,'after-registration',1,%s,%s,now())",
        (
            account["project_id"],
            account["uuid"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            account["external_account_id"],
            incoming_operations.json_value(
                {
                    "type": "subscription",
                    "op": "add",
                    "subscriptions": [{"stream_id": 42, "name": "Channel"}],
                }
            ),
            ["stream:42"],
        ),
    )

    async def run():
        async with worker(incoming, {}) as (service, _):
            await drain(service, account)
            assert await service.db.sql(
                "SELECT 1 FROM messenger_stream_bindings WHERE project_id=%s AND active",
                (account["project_id"],),
            )
            assert not await service.db.sql("SELECT 1 FROM zulip_history_intents")

    asyncio.run(run())
