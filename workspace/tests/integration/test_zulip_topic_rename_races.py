# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import time
from unittest import mock

import pytest

from workspace.bridges.zulip import incoming_operations
from workspace.messenger_api.dm import external_models
from workspace.services import zulip_incoming
from workspace.tests.integration import test_zulip_incoming as helpers
from workspace.tests.integration import test_zulip_topic_rename as renames

incoming = helpers.incoming


def test_old_name_personal_event_cannot_overwrite_a_concurrent_rename(incoming):
    api, db, account, http, url = incoming
    source = renames.seed_messages(incoming, size=3)

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        paused, release = asyncio.Event(), asyncio.Event()
        save = service.operations.save

        async def pause_topic(connection, table, values, **kwargs):
            if table == "messenger_topics" and values.get("name") == "General":
                paused.set()
                await release.wait()
            return await save(connection, table, values, **kwargs)

        personal = None
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "personal-race"},
                    [
                        {
                            "id": 1,
                            "type": "user_topic",
                            "stream_id": 42,
                            "topic_name": "General",
                            "visibility_policy": 3,
                        },
                        {"id": 2, **renames.rename(message_ids=[101, 102, 103])},
                    ],
                    stage,
                )
            pending = await service.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            with mock.patch.object(service.operations, "save", side_effect=pause_topic):
                personal = asyncio.create_task(service.process(pending[0]))
                await asyncio.wait_for(paused.wait(), 2)
                await service.process(pending[1])
                release.set()
                await personal
            await helpers.drain_incoming(service)
        finally:
            release.set()
            if personal:
                await personal
            await service.db.close()

    asyncio.run(run())
    assert api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "Renamed"
    assert db.execute(
        "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall() == [(source,)]


@pytest.mark.parametrize(
    "resume",
    [
        "restart",
        "disabled_primary",
        "late_human",
        "late_notice",
        "late_destination_history",
        "late_destination_fresh",
    ],
)
def test_large_notice_destination_resumes_with_late_message_semantics(incoming, resume):
    api, db, account, http, url = incoming
    renames.seed_notification_bot(incoming)
    source = renames.seed_messages(incoming, size=3)
    notices = []
    for identifier in range(50001, 50152):
        notice = helpers.event("Old destination move notice", identifier)
        notice["message"].update(subject="Renamed", sender_id=6, timestamp=1000)
        notices.append(notice)
    helpers.apply(incoming, *notices)
    db.execute(
        "UPDATE messenger_user_topic_bindings SET notification_mode='follow',updated_at=now()+interval '1 minute' "
        "WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s",
        (account["project_id"], source, account["owner_user_uuid"]),
    )
    db.execute(
        "UPDATE messenger_user_topic_bindings b SET notification_mode='mute' "
        "FROM messenger_topics t WHERE t.project_id=b.project_id AND t.uuid=b.topic_uuid "
        "AND b.project_id=%s AND t.provider_identity='42:Renamed' AND b.user_uuid=%s",
        (account["project_id"], account["owner_user_uuid"]),
    )
    linked, peer = helpers.peer_account(incoming)
    helpers.apply(peer, helpers.event())
    http.request.side_effect = AssertionError("A rename must not reload bodies")

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            for owner in (account, peer[2]):
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        owner,
                        {"queue_id": "large-notice-destination"},
                        [{"id": 1, **renames.rename(message_ids=[101, 102, 103])}],
                        stage,
                    )
            pending = await service.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            await service.process(pending[0])
            assert db.execute(
                "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
                (account["project_id"], source),
            ).fetchone() == (103,)
            processed, payload = db.execute(
                "SELECT processed_at,payload FROM zulip_incoming_events WHERE id=%s",
                (pending[0]["id"],),
            ).fetchone()
            assert processed is None
            assert payload["_topic_rename_progress"]["history_uuid"] == str(source)
        finally:
            await service.db.close()
        if resume == "disabled_primary":
            db.execute(
                "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
                (account["uuid"],),
            )
        if resume.startswith("late_"):
            late = helpers.event("Late old-name history", 60000)
            late["message"]["timestamp"] = 1000
            if resume == "late_notice":
                late["message"]["sender_id"] = 6
            if resume.startswith("late_destination"):
                late["message"]["subject"] = "Renamed"
            if resume == "late_destination_fresh":
                late["message"]["timestamp"] = 2000000001
            await asyncio.to_thread(helpers.apply, incoming, late)
        restarted = zulip_incoming.ZulipIncomingService(url)
        await restarted.db.open()
        restarted.http = http
        restarted.operations = incoming_operations.Operations(restarted.db, http)
        try:
            pending = await restarted.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            restarted.registry.restore(pending)
            await helpers.drain_incoming(restarted)
        finally:
            await restarted.db.close()

    try:
        asyncio.run(run())
        destination = source
        if resume in ("late_human", "late_destination_history"):
            destination = db.execute(
                "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Renamed'",
                (account["project_id"],),
            ).fetchone()[0]
            assert destination != source
        else:
            assert (
                api.get(f"/v1/messenger/stream_topics/{source}").json()["name"]
                == "Renamed"
            )
        assert db.execute(
            "SELECT DISTINCT p.topic_uuid FROM messenger_message_placements p "
            "JOIN messenger_messages m ON m.project_id=p.project_id AND m.uuid=p.message_uuid "
            "WHERE p.project_id=%s AND m.provider_identity<>'60000'",
            (account["project_id"],),
        ).fetchall() == [(destination,)]
        if resume in ("late_human", "late_notice"):
            late_topic = db.execute(
                "SELECT p.topic_uuid,t.name FROM messenger_message_placements p "
                "JOIN messenger_messages m ON m.project_id=p.project_id AND m.uuid=p.message_uuid "
                "JOIN messenger_topics t ON t.project_id=p.project_id AND t.uuid=p.topic_uuid "
                "WHERE p.project_id=%s AND m.provider_identity='60000'",
                (account["project_id"],),
            ).fetchone()
            assert late_topic[0] != destination
            assert late_topic[1] == "General"
        expected_count = 155 if resume.startswith("late_destination") else 154
        assert db.execute(
            "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
            (account["project_id"], destination),
        ).fetchone() == (expected_count,)
        assert db.execute(
            "SELECT count(*) FROM zulip_incoming_events WHERE processed_at IS NULL"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT notification_mode FROM messenger_user_topic_bindings "
            "WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s",
            (account["project_id"], destination, account["owner_user_uuid"]),
        ).fetchone() == (
            "mute"
            if resume in ("late_human", "late_destination_history")
            else "follow",
        )
    finally:
        db.execute(
            "UPDATE m_external_accounts_v2 SET disabled=false WHERE uuid=%s",
            (account["uuid"],),
        )
        linked.delete()


def test_large_destination_with_human_history_beyond_first_batch_is_a_real_merge(
    incoming,
):
    api, db, account, _http, _url = incoming
    renames.seed_notification_bot(incoming)
    source = renames.seed_messages(incoming, size=3)
    notices = []
    for identifier in range(50001, 50153):
        notice = helpers.event("Existing destination", identifier)
        notice["message"].update(subject="Renamed", sender_id=6, timestamp=1000)
        notices.append(notice)
    helpers.apply(incoming, *notices)
    target, message = db.execute(
        "SELECT p.topic_uuid,p.message_uuid FROM messenger_message_placements p "
        "JOIN messenger_topics t ON t.project_id=p.project_id AND t.uuid=p.topic_uuid "
        "WHERE p.project_id=%s AND t.provider_identity='42:Renamed' ORDER BY p.uuid DESC LIMIT 1",
        (account["project_id"],),
    ).fetchone()
    db.execute(
        "UPDATE messenger_messages SET author_uuid=%s WHERE project_id=%s AND uuid=%s",
        (account["owner_user_uuid"], account["project_id"], message),
    )
    helpers.apply(incoming, renames.rename(message_ids=[101, 102, 103]))
    assert db.execute(
        "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall() == [(target,)]
    assert api.get(f"/v1/messenger/stream_topics/{source}").status_code == 404


def test_provider_cannot_supply_internal_rename_progress(incoming):
    _api, db, account, _http, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "untrusted-progress"},
                    [
                        {
                            "id": 1,
                            **renames.rename(),
                            "_topic_rename_progress": {"invalid": True},
                        }
                    ],
                    stage,
                )
        finally:
            await service.db.close()

    asyncio.run(run())
    assert (
        "_topic_rename_progress"
        not in db.execute(
            "SELECT payload FROM zulip_incoming_events WHERE external_account_uuid=%s",
            (account["uuid"],),
        ).fetchone()[0]
    )


def test_another_providers_notice_is_retained_as_real_history(incoming):
    api, db, account, _http, _url = incoming
    renames.seed_notification_bot(incoming)
    helpers.apply(incoming, helpers.event())
    notice = helpers.event("Another provider's history", 50001)
    notice["message"].update(sender_id=6, timestamp=1000)
    helpers.apply(incoming, notice)
    message, owner, source = db.execute(
        "SELECT m.uuid,m.author_uuid,p.topic_uuid FROM messenger_messages m "
        "JOIN messenger_message_placements p ON p.project_id=m.project_id AND p.message_uuid=m.uuid "
        "WHERE m.project_id=%s AND m.provider_identity='50001'",
        (account["project_id"],),
    ).fetchone()
    peer = external_models.ExternalAccount(
        project_id=account["project_id"],
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="6",
        status="active",
        settings={
            "server_url": "https://other-zulip.example.com",
            "email": "notification-bot@zulip.com",
        },
    )
    peer.insert()
    try:
        assert peer.provider_uuid != account["provider_uuid"]
        db.execute(
            "UPDATE messenger_messages SET provider_uuid=%s WHERE project_id=%s AND uuid=%s",
            (peer.provider_uuid, account["project_id"], message),
        )
        helpers.apply(incoming, renames.rename(message_ids=[101]))
        assert (
            api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "General"
        )
        assert db.execute(
            "SELECT topic_uuid FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s",
            (account["project_id"], message),
        ).fetchone() == (source,)
    finally:
        peer.delete()


def test_unaffected_move_notices_split_in_bounded_batches_and_keep_realtime(
    incoming, record_property
):
    api, db, account, http, url = incoming
    renames.seed_notification_bot(incoming)
    helpers.apply(incoming, helpers.event())
    source = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:General'",
        (account["project_id"],),
    ).fetchone()[0]
    notices = []
    for identifier in range(50001, 50151):
        notice = helpers.event("Old topic move notice", identifier)
        notice["message"].update(sender_id=6, timestamp=1000)
        notices.append(notice)
    helpers.apply(
        incoming,
        *notices,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 43, "name": "Other"}],
        },
    )
    other = helpers.event("Other topic seed", 49999)
    other["message"].update(stream_id=43, subject="Other")
    helpers.apply(incoming, other)
    http.request.side_effect = AssertionError(
        "Moving notices must not reload message bodies"
    )
    fresh = helpers.event("Unrelated realtime during notice split", 60001)
    fresh["message"].update(stream_id=43, subject="Other")

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "notice-split"},
                    [
                        {"id": 1, **renames.rename(message_ids=[101])},
                        {"id": 2, **fresh},
                    ],
                    stage,
                )
            pending = await service.db.sql(
                "SELECT * FROM zulip_incoming_events WHERE processed_at IS NULL ORDER BY id"
            )
            await service.process(pending[0])
            assert db.execute(
                "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
                (account["project_id"], source),
            ).fetchone() == (51,)
            assert db.execute(
                "SELECT processed_at FROM zulip_incoming_events WHERE id=%s",
                (pending[0]["id"],),
            ).fetchone() == (None,)
            start = time.monotonic()
            await service.process(pending[1])
            await asyncio.to_thread(helpers.publish)
            assert any(
                message["payload"]["content"] == fresh["message"]["content"]
                for message in api.get("/v1/messenger/messages/").json()
            )
            elapsed = time.monotonic() - start
            record_property(
                "unrelated_message_during_notice_split_seconds", round(elapsed, 4)
            )
            assert elapsed < 1
            await helpers.drain_incoming(service)
        finally:
            await service.db.close()

    asyncio.run(run())
    assert api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "Renamed"
    assert db.execute(
        "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
        (account["project_id"], source),
    ).fetchone() == (1,)
    assert db.execute(
        "SELECT count(*) FROM messenger_topics WHERE project_id=%s AND provider_identity='42:General'",
        (account["project_id"],),
    ).fetchone() == (1,)


def test_old_move_notice_alias_is_not_a_real_destination_merge(incoming):
    api, db, account, _http, _url = incoming
    renames.seed_notification_bot(incoming)
    source = renames.seed_messages(incoming, size=3)
    notice = helpers.event("This topic was moved", 50001)
    notice["message"].update(subject="Renamed", sender_id=6, timestamp=1000)
    helpers.apply(incoming, notice)
    helpers.apply(incoming, renames.rename(message_ids=[101, 102, 103]))
    assert api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "Renamed"
    assert db.execute(
        "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall() == [(source,)]


def test_real_destination_created_in_same_provider_second_keeps_its_uuid(incoming):
    _api, db, account, _http, _url = incoming
    source = helpers.event()
    source["message"]["timestamp"] = 2000
    destination = helpers.event("Existing destination history", 102)
    destination["message"].update(subject="Destination", timestamp=2000)
    helpers.apply(incoming, source, destination)
    target = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:Destination'",
        (account["project_id"],),
    ).fetchone()[0]
    helpers.apply(
        incoming, renames.rename("Destination", message_ids=[101], edit_timestamp=2000)
    )
    assert db.execute(
        "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall() == [(target,)]


def test_repeated_rename_copy_cannot_choose_a_delayed_old_name_alias(incoming):
    api, db, account, _http, _url = incoming
    initial = helpers.event()
    initial["message"]["timestamp"] = 2000
    helpers.apply(incoming, initial)
    source = db.execute(
        "SELECT uuid FROM messenger_topics WHERE project_id=%s AND provider_identity='42:General'",
        (account["project_id"],),
    ).fetchone()[0]
    change = renames.rename(message_ids=[101], edit_timestamp=2000)
    helpers.apply(
        incoming,
        change,
        {
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "General",
            "visibility_policy": 0,
        },
        change,
    )
    assert api.get(f"/v1/messenger/stream_topics/{source}").json()["name"] == "Renamed"
    assert db.execute(
        "SELECT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchone() == (source,)


def test_rename_retries_while_a_fresh_notice_uses_its_provisional_topic(
    incoming, caplog
):
    _api, db, account, http, url = incoming
    renames.seed_notification_bot(incoming)
    source = renames.seed_messages(incoming, size=3)
    notice = helpers.event("This topic was moved", 50001)
    notice["message"].update(subject="Renamed", sender_id=6, timestamp=2000000001)

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.http = http
        service.operations = incoming_operations.Operations(service.db, http)
        started, release = asyncio.Event(), asyncio.Event()
        save = service.operations.save

        async def blocked_save(connection, table, values, **kwargs):
            if table == "messenger_message_placements":
                started.set()
                await release.wait()
            return await save(connection, table, values, **kwargs)

        async def apply_notice():
            prepared = await helpers.prepare_dependencies(
                service.operations, service.db, account, notice
            )
            async with service.db.transaction() as connection:
                await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
                await service.operations.apply(connection, account, prepared)

        task = None
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "notice-race"},
                    [
                        {
                            "id": 1,
                            **renames.rename(message_ids=[101, 102, 103]),
                        }
                    ],
                    stage,
                )
            await asyncio.to_thread(
                helpers.apply,
                incoming,
                {
                    "type": "user_topic",
                    "stream_id": 42,
                    "topic_name": "Renamed",
                    "visibility_policy": 3,
                },
            )
            with mock.patch.object(
                service.operations, "save", side_effect=blocked_save
            ):
                task = asyncio.create_task(apply_notice())
                await asyncio.wait_for(started.wait(), 2)
                ready = await service.db.sql(zulip_incoming.SELECT_EVENT_SQL, ([], 128))
                await service.process(ready[0])
                release.set()
                await task
                await helpers.drain_incoming(service)
            assert not db.execute(
                "SELECT 1 FROM zulip_incoming_events WHERE processed_at IS NULL OR error IS NOT NULL"
            ).fetchall()
        finally:
            release.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await service.db.close()

    asyncio.run(run())
    assert "Cannot apply Zulip event" not in caplog.text
    assert db.execute(
        "SELECT DISTINCT topic_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account["project_id"],),
    ).fetchall() == [(source,)]
