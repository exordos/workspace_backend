# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Counter projection must not hold up message and flag producers."""

import asyncio
import concurrent.futures
import json
import socket
import threading
import time
import uuid
from unittest import mock

import psycopg
import pytest
import websockets
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mutation_journal_opts
from workspace.messenger_api import (
    events,
    message_flags,
    publication,
    websocket_service,
)
from workspace.messenger_api.api import v2_store
from workspace.services.messenger_workers import counters, v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal_tests

conversation = journal_tests.conversation


@pytest.fixture
def counter_provider_account(api, db):
    provider = uuid.uuid5(uuid.NAMESPACE_DNS, "counter.example.com")
    identifier = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_providers VALUES(%s,'counter.example.com') ON CONFLICT DO NOTHING",
        (provider,),
    )
    db.execute(
        "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,provider_uuid,external_account_id,status,settings) "
        "VALUES(%s,%s,%s,'zulip',%s,'7','active',%s::jsonb)",
        (
            identifier,
            api.project_id,
            api.user_uuid,
            provider,
            json.dumps(
                {
                    "server_url": "https://counter.example.com",
                    "email": "sender@example.com",
                    "api_key": "synthetic",
                }
            ),
        ),
    )
    try:
        yield provider
    finally:
        db.execute("DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (identifier,))


def _count_one():
    with contexts.Context().session_manager() as session:
        return counters.process_pending(session)


def test_delete_after_read_change_does_not_deadlock_counter_acknowledgement(
    api, db, conversation, caplog
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    topic = stream["default_topic_uuid"]
    # Deletion refreshes personal summary state, sharing the counter's cache
    # row. A producer first changes an unread fact and its dirty revision.
    db.execute(
        "UPDATE messenger_topics SET summary='Existing summary' WHERE project_id=%s AND uuid=%s",
        (api.project_id, topic),
    )
    db.execute(
        "SELECT messenger_queue_counters(%s,%s,%s,%s)",
        (api.project_id, peer, stream["uuid"], topic),
    )
    revision = db.execute(
        "SELECT revision FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, peer, topic),
    ).fetchone()[0]
    acknowledging, release, changed = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    producer_pid = []

    class AcknowledgementGate:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values=()):
            if statement.startswith("DELETE FROM messenger_counter_updates"):
                acknowledging.set()
                assert release.wait(5), "Counter acknowledgement was not released"
            return self.session.execute(statement, values)

        def __getattr__(self, name):
            return getattr(self.session, name)

    def count():
        with contexts.Context().session_manager() as session:
            return counters.process_pending(AcknowledgementGate(session))

    def produce():
        with contexts.Context().session_manager() as session:
            producer_pid.append(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            session.execute(
                "DELETE FROM messenger_user_message_unreads "
                "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (api.project_id, peer, messages[0]["uuid"]),
            )
            changed.set()
            store = v2_store.MessengerV2Store(api.project_id, api.user_uuid)
            store.delete_message(uuid.UUID(messages[-1]["uuid"]))

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        counter = pool.submit(count)
        try:
            assert acknowledging.wait(3)
            producer = pool.submit(produce)
            assert changed.wait(3)
            deadline = time.monotonic() + 3
            while not producer.done():
                if db.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (producer_pid[0],),
                ).fetchone() == ("Lock",):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.002)
        finally:
            release.set()
        producer.result(timeout=5)
        assert counter.result(timeout=5) == 1
    assert "Cannot recount" not in caplog.text
    # CAS must preserve the producer's committed revision despite the older
    # COUNT/cache snapshot, so a later turn can converge to zero.
    assert db.execute(
        "SELECT revision>%s FROM messenger_counter_updates "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (revision, api.project_id, peer, topic),
    ).fetchone() == (True,)
    native._drain()
    assert _project_count(db, api, peer) == (0, 0, 0)
    assert api.get(f"{native.MESSAGES}{messages[-1]['uuid']}").status_code == 404


def test_public_provider_message_delete_during_count_preserves_revision_and_realtime(
    api, db, conversation, _test_database_url, counter_provider_account
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    provider = counter_provider_account
    db.execute(
        "UPDATE messenger_streams SET source_name='zulip',provider_uuid=%s,provider_identity='42' "
        "WHERE project_id=%s AND uuid=%s",
        (provider, api.project_id, stream["uuid"]),
    )
    db.execute(
        "UPDATE messenger_messages SET source_name='zulip',provider_uuid=%s,provider_identity=uuid::text "
        "WHERE project_id=%s",
        (provider, api.project_id),
    )
    topic = stream["default_topic_uuid"]
    db.execute(
        "SELECT messenger_queue_counters(%s,%s,%s,%s)",
        (api.project_id, peer, stream["uuid"], topic),
    )
    revision = db.execute(
        "SELECT revision FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, peer, topic),
    ).fetchone()[0]
    counting, release = threading.Event(), threading.Event()

    class CountSnapshotGate:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values=()):
            result = self.session.execute(statement, values)
            if statement == counters.COUNT_TOPIC_SQL:
                snapshot = result.fetchone()
                counting.set()
                assert release.wait(5), "COUNT snapshot was not released"
                return mock.Mock(fetchone=lambda: snapshot)
            return result

        def __getattr__(self, name):
            return getattr(self.session, name)

    def count():
        with contexts.Context().session_manager() as session:
            return counters.process_pending(CountSnapshotGate(session))

    async def run():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=_test_database_url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=5,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.02,
        )
        server._authenticator.authenticate = lambda token: (
            str(peer),
            api.project_id,
        )
        serving = asyncio.create_task(server.serve("127.0.0.1", port))
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    websocket = await websockets.connect(
                        f"ws://127.0.0.1:{port}/v1/events/ws",
                        subprotocols=["workspace.events.v1", "bearer.test"],
                    )
                    break
                except OSError:
                    assert not serving.done()
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.01)
            try:

                async def receive(action, placement):
                    while True:
                        frame = json.loads(await asyncio.wait_for(websocket.recv(), 5))
                        if (
                            frame.get("object_type") == "message"
                            and frame.get("action") == action
                            and frame["payload"]["uuid"] == placement
                        ):
                            return frame

                while (
                    json.loads(await asyncio.wait_for(websocket.recv(), 5)).get("type")
                    != "ready"
                ):
                    pass
                with concurrent.futures.ThreadPoolExecutor(1) as pool:
                    counter = pool.submit(count)
                    try:
                        assert await asyncio.to_thread(counting.wait, 3)
                        response = await asyncio.to_thread(
                            api.delete, f"{native.MESSAGES}{messages[-1]['uuid']}"
                        )
                        assert response.status_code == 204, response.text
                    finally:
                        release.set()
                    assert await asyncio.to_thread(counter.result, 5) == 1
                assert db.execute(
                    "SELECT revision>%s FROM messenger_counter_updates "
                    "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
                    (revision, api.project_id, peer, topic),
                ).fetchone() == (True,)
                await asyncio.to_thread(native._drain)
                assert _project_count(db, api, peer) == (1, 1, 0)
                assert (
                    api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()[
                        "unread_count"
                    ]
                    == 1
                )
                assert (
                    api.get(f"{native.STREAM_TOPICS}{topic}", user=peer).json()[
                        "unread_count"
                    ]
                    == 1
                )
                assert db.execute(
                    "SELECT unread_count FROM messenger_user_folder_bindings "
                    "WHERE project_id=%s AND user_uuid=%s AND rule='all_chats'",
                    (api.project_id, peer),
                ).fetchone() == (1,)
                await receive("deleted", messages[-1]["uuid"])
                response = await asyncio.to_thread(
                    api.post,
                    native.MESSAGES,
                    json={
                        "stream_uuid": stream["uuid"],
                        "topic_uuid": topic,
                        "payload": {
                            "kind": "markdown",
                            "content": "After concurrent deletion",
                        },
                    },
                )
                assert response.status_code == 201, response.text
                await asyncio.to_thread(native._drain)
                await receive("created", response.json()["uuid"])
                assert _project_count(db, api, peer) == (2, 2, 0)
            finally:
                await websocket.close()
        finally:
            release.set()
            server.stop()
            await serving

    asyncio.run(run())


def test_counter_crash_before_acknowledgement_keeps_committed_cache_and_dirty_scope(
    api, db, conversation
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    response = api.delete(f"{native.MESSAGES}{messages[-1]['uuid']}")
    assert response.status_code == 204, response.text
    db.execute(
        "DELETE FROM messenger_counter_updates WHERE project_id=%s AND user_uuid<>%s",
        (api.project_id, peer),
    )

    class CrashBeforeAcknowledgement:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values=()):
            if statement.startswith("DELETE FROM messenger_counter_updates"):
                raise RuntimeError("Counter stopped before acknowledgement")
            return self.session.execute(statement, values)

        def __getattr__(self, name):
            return getattr(self.session, name)

    with pytest.raises(RuntimeError, match="Counter stopped"):
        with contexts.Context().session_manager() as session:
            counters.process_pending(CrashBeforeAcknowledgement(session))
    assert _project_count(db, api, peer) == (1, 1, 0)
    assert db.execute(
        "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s",
        (api.project_id, peer),
    ).fetchone() == (1,)
    native._drain()
    assert _project_count(db, api, peer) == (1, 1, 0)
    assert db.execute(
        "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s",
        (api.project_id, peer),
    ).fetchone() == (0,)


@pytest.mark.parametrize("new_revision", [False, True])
def test_failed_counter_scope_rolls_back_cache_and_only_postpones_its_revision(
    api, db, conversation, monkeypatch, new_revision
):
    stream, _messages, peer = conversation
    topic = stream["default_topic_uuid"]
    db.execute(
        "SELECT messenger_queue_counters(%s,%s,%s,%s)",
        (api.project_id, peer, stream["uuid"], topic),
    )

    def fail(session, update):
        session.execute(
            "UPDATE messenger_user_topic_bindings SET unread_count=99 "
            "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
            (api.project_id, peer, topic),
        )
        if new_revision:
            db.execute(
                "SELECT messenger_queue_counters(%s,%s,%s,%s)",
                (api.project_id, peer, stream["uuid"], topic),
            )
        raise RuntimeError("Interrupted recount")

    with monkeypatch.context() as patch:
        patch.setattr(counters, "_recount_scope", fail)
        assert _count_one() == 1
    assert db.execute(
        "SELECT unread_count FROM messenger_user_topic_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, peer, topic),
    ).fetchone() == (2,)
    assert db.execute(
        "SELECT next_retry_at IS NULL FROM messenger_counter_updates "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, peer, topic),
    ).fetchone() == (new_revision,)


def test_counters_keep_publishing_when_facts_change_during_every_count(
    api, db, conversation
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    response = api.post(
        f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read_up_to/invoke",
        user=peer,
    )
    assert response.status_code == 200, response.text
    native._drain()
    for _ in range(2):
        assert _create_and_deliver(api, peer, stream)
    assert _project_count(db, api, peer) == (0, 0, 0)
    db.execute(
        "DELETE FROM messenger_counter_updates WHERE project_id=%s AND user_uuid<>%s",
        (api.project_id, peer),
    )
    placement = db.execute(
        "SELECT placement_uuid FROM messenger_user_message_unreads "
        "WHERE project_id=%s AND user_uuid=%s LIMIT 1",
        (api.project_id, peer),
    ).fetchone()[0]

    class ChangingCountSession:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values=()):
            result = self.session.execute(statement, values)
            if statement == counters.COUNT_TOPIC_SQL:
                removed = db.execute(
                    "DELETE FROM messenger_user_message_unreads "
                    "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s RETURNING placement_uuid",
                    (api.project_id, peer, placement),
                ).fetchone()
                if removed is None:
                    db.execute(
                        "INSERT INTO messenger_user_message_unreads VALUES(%s,%s,%s,%s,%s)",
                        (
                            api.project_id,
                            peer,
                            placement,
                            stream["uuid"],
                            stream["default_topic_uuid"],
                        ),
                    )
            return result

        def __getattr__(self, name):
            return getattr(self.session, name)

    for expected in (2, 1, 2):
        with contexts.Context().session_manager() as session:
            assert counters.process_pending(ChangingCountSession(session)) == 1
        # A committed COUNT snapshot must make progress even if another
        # revision is already waiting; it must not require a quiet interval.
        assert _project_count(db, api, peer) == (expected, expected, 0)
        assert db.execute(
            "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, peer),
        ).fetchone() == (1,)
    native._drain()
    assert _project_count(db, api, peer) == (1, 1, 0)


def _star(api, peer, placement):
    with contexts.Context().session_manager():
        store = v2_store.MessengerV2Store(api.project_id, peer)
        return store.perform_action("messages", uuid.UUID(placement), "star", {})


def _create_and_deliver(api, peer, stream, *, publish_events=True):
    response = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {
                "kind": "markdown",
                "content": "Realtime during slow folder count",
            },
        },
    )
    assert response.status_code == 201, response.text
    placement = response.json()["uuid"]
    with contexts.Context().session_manager() as session:
        task = session.execute(
            "SELECT * FROM messenger_projection_tasks WHERE project_id=%s "
            "AND task_kind='fanout' AND payload->>'placement_uuid'=%s",
            (api.project_id, placement),
        ).fetchone()
        assert v2_projection._process_fanout(session, task, 1000)
        session.execute(
            "UPDATE messenger_projection_tasks SET status='completed' WHERE uuid=%s",
            (task["uuid"],),
        )
    if publish_events:
        with contexts.Context().session_manager() as session:
            publication.publish_pending(session)
    with contexts.Context().session_manager() as session:
        return session.execute(
            "SELECT EXISTS(SELECT 1 FROM m_workspace_visible_events "
            "WHERE project_id=%s AND user_uuid=%s AND object_type='message' "
            "AND action='created' AND payload->>'uuid'=%s) AS visible",
            (api.project_id, peer, placement),
        ).fetchone()["visible"]


def _counter_snapshot_backlog(api, db, stream, peer):
    db.execute(
        """WITH topics AS (
             INSERT INTO messenger_topics(uuid,project_id,stream_uuid,name)
             SELECT gen_random_uuid(),%s,%s,'Counter backlog '||number
             FROM generate_series(1,80) number RETURNING uuid
           ) INSERT INTO messenger_user_topic_bindings(uuid,project_id,user_uuid,topic_uuid)
             SELECT gen_random_uuid(),%s,%s,uuid FROM topics""",
        (api.project_id, stream["uuid"], api.project_id, peer),
    )
    with contexts.Context().session_manager() as session:
        assert counters.process_pending(session, 1000) == 80
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_publication_outbox WHERE project_id=%s",
            (api.project_id,),
        ).fetchone()[0]
        > 256
    )


def test_live_fanout_bypasses_the_committed_counter_snapshot_backlog(
    api, db, conversation
):
    stream, _messages, peer = conversation
    _counter_snapshot_backlog(api, db, stream, peer)
    assert _create_and_deliver(api, peer, stream) is True, (
        "Committed counter snapshots delayed a new message's live feed"
    )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_publication_outbox WHERE project_id=%s",
            (api.project_id,),
        ).fetchone()[0]
        > 0
    )


def test_counter_snapshot_publication_releases_the_epoch_lock_after_a_short_batch(
    api, db, conversation, monkeypatch
):
    stream, _messages, peer = conversation
    _counter_snapshot_backlog(api, db, stream, peer)
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    publishing, release = threading.Event(), threading.Event()
    original = events.create_broadcast_event
    published = 0

    def bounded_broadcast(*args, **kwargs):
        nonlocal published
        if kwargs.get("owned_publication"):
            published += 1
            publishing.set()
            # An oversized batch retains the project epoch lock here. A live
            # fanout must get its turn before this 33rd background snapshot.
            if published == 33:
                assert release.wait(5), "Oversized publication was not released"
        return original(*args, **kwargs)

    def publish_batch():
        with contexts.Context().session_manager() as session:
            return publication.publish(session, uuid.UUID(api.project_id))

    monkeypatch.setattr(events, "create_broadcast_event", bounded_broadcast)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        publisher = pool.submit(publish_batch)
        try:
            assert publishing.wait(3)
            live = pool.submit(
                _create_and_deliver, api, peer, stream, publish_events=False
            )
            assert live.result(timeout=0.75) is True
        finally:
            release.set()
        assert publisher.result(timeout=5) <= 32


def test_live_publication_preserves_earlier_background_updates_for_the_entity(
    api, db, conversation
):
    stream, _messages, peer = conversation
    with contexts.Context().session_manager() as session:
        with publication.defer():
            publication.enqueue(
                session,
                api.project_id,
                stream["uuid"],
                [peer],
                "stream.updated",
                {"uuid": stream["uuid"], "name": "earlier counter snapshot"},
            )
        publication.enqueue(
            session,
            api.project_id,
            stream["uuid"],
            [peer],
            "stream.updated",
            {"uuid": stream["uuid"], "name": "later live snapshot"},
        )
    with contexts.Context().session_manager() as session:
        assert publication.publish(session, uuid.UUID(api.project_id)) == 2
    assert db.execute(
        "SELECT payload->>'name' FROM m_workspace_visible_events "
        "WHERE project_id=%s AND user_uuid=%s AND payload->>'name' "
        "IN ('earlier counter snapshot','later live snapshot') ORDER BY epoch_version",
        (api.project_id, peer),
    ).fetchall() == [("earlier counter snapshot",), ("later live snapshot",)]


@pytest.mark.parametrize("journal_enabled", [False, True])
@pytest.mark.parametrize("producer", ["flag", "message"])
def test_slow_counter_folder_snapshot_does_not_block_producers(
    api, db, conversation, monkeypatch, journal_enabled, producer
):
    stream, messages, peer = conversation
    cfg.CONF.set_override(
        "enabled", journal_enabled, group=mutation_journal_opts.DOMAIN
    )
    native._drain()
    db.execute(
        "SELECT messenger_queue_counters(%s,%s,%s,%s)",
        (api.project_id, peer, stream["uuid"], stream["default_topic_uuid"]),
    )
    aggregating, release = threading.Event(), threading.Event()
    original = v2_projection._process_folder_projection
    processed_folders = 0

    def slow_folder(session, task):
        nonlocal processed_folders
        processed_folders += 1
        # The first folder has already emitted its update. Later folder work
        # must not retain its project-wide publication gate.
        if processed_folders == 2:
            aggregating.set()
            assert release.wait(5), "Counter folder snapshot was not released"
        return original(session, task)

    monkeypatch.setattr(v2_projection, "_process_folder_projection", slow_folder)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        counter = pool.submit(_count_one)
        try:
            assert aggregating.wait(3)
            if producer == "flag":
                writing = pool.submit(_star, api, peer, messages[-1]["uuid"])
            else:
                writing = pool.submit(_create_and_deliver, api, peer, stream)
            result = writing.result(timeout=0.5)
            if producer == "flag":
                assert result["starred"] is True
            else:
                assert result is True, (
                    "The new message did not reach its live event feed"
                )
        finally:
            release.set()
        assert counter.result(timeout=5) == 1


@pytest.mark.parametrize(
    "change", ["active=false", "membership_generation=membership_generation+1"]
)
def test_flag_membership_fence_blocks_revocation_until_flag_commit(
    api, db, conversation, change
):
    stream, messages, peer = conversation
    flag_written, release = threading.Event(), threading.Event()

    def flag():
        with contexts.Context().session_manager() as session:
            store = v2_store.MessengerV2Store(api.project_id, peer)
            message = store._message(uuid.UUID(messages[-1]["uuid"]))
            assert message_flags.set_flag(session, message, "starred", True)
            flag_written.set()
            assert release.wait(5)

    with (
        psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as revoking,
        concurrent.futures.ThreadPoolExecutor(2) as pool,
    ):
        writer = pool.submit(flag)
        try:
            assert flag_written.wait(3)
            revoke = pool.submit(
                revoking.execute,
                f"UPDATE messenger_stream_bindings SET {change} "
                "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
                (api.project_id, peer, stream["uuid"]),
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if db.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                    (revoking.info.backend_pid,),
                ).fetchone() == ("Lock",):
                    break
                assert not revoke.done(), (
                    "Revocation passed the pending membership fence"
                )
                time.sleep(0.002)
            else:
                raise AssertionError(
                    "Revocation did not wait for the pending flag transaction"
                )
        finally:
            release.set()
        writer.result(timeout=5)
        revoke.result(timeout=5)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_user_message_stars WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, peer),
        ).fetchone()[0]
        == 0
    )


def _project_count(db, api, user):
    return db.execute(
        "SELECT unread_count,active_unread_count,passive_unread_count "
        "FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
        (api.project_id, user),
    ).fetchone()


def test_stream_read_preserves_unreads_in_a_deleted_topic(api, db, conversation):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    db.execute(
        "UPDATE messenger_topics SET deleted_at=NOW() WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream["default_topic_uuid"]),
    )
    response = api.post(
        f"{native.STREAMS}{stream['uuid']}/actions/read/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    db.execute(
        "UPDATE messenger_topics SET deleted_at=NULL WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream["default_topic_uuid"]),
    )
    for message in messages:
        assert (
            api.get(f"{native.MESSAGES}{message['uuid']}", user=peer).json()["read"]
            is False
        )


def test_physical_topic_deletion_zeros_stream_folder_and_project_counts(
    api, db, conversation
):
    stream, _messages, peer = conversation
    assert _project_count(db, api, peer) == (2, 2, 0)
    db.execute(
        "DELETE FROM messenger_topics WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream["default_topic_uuid"]),
    )
    native._drain()
    assert _project_count(db, api, peer) == (0, 0, 0)
    assert (
        api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()["unread_count"]
        == 0
    )
    assert all(
        folder[0] == 0
        for folder in db.execute(
            "SELECT unread_count FROM messenger_user_folder_bindings WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, peer),
        ).fetchall()
    )


@pytest.mark.parametrize("journal_enabled", [False, True])
def test_archiving_a_stream_zeros_project_counts_without_marking_messages_read(
    api, db, conversation, journal_enabled
):
    stream, messages, peer = conversation
    cfg.CONF.set_override(
        "enabled", journal_enabled, group=mutation_journal_opts.DOMAIN
    )
    assert _project_count(db, api, peer) == (2, 2, 0)
    response = api.post(f"{native.STREAMS}{stream['uuid']}/actions/archive/invoke")
    assert response.status_code == 200, response.text
    native._drain()
    assert _project_count(db, api, peer) == (0, 0, 0)
    for message in messages:
        assert (
            api.get(f"{native.MESSAGES}{message['uuid']}", user=peer).json()["read"]
            is False
        )


@pytest.mark.parametrize(
    "stream_mode,topic_mode,active",
    [
        ("all_messages", "default", 2),
        ("mentions_only", "default", 1),
        ("muted", "default", 0),
        ("muted", "follow", 2),
        ("all_messages", "mute", 0),
    ],
)
def test_counter_modes_preserve_unread_facts_and_zero_parent_active_counts(
    api, db, conversation, stream_mode, topic_mode, active
):
    stream, messages, peer = conversation
    # Exactly one of the two unread placements mentions this recipient.
    db.execute(
        "INSERT INTO messenger_user_message_mentions VALUES(%s,%s,%s,%s,%s)",
        (
            api.project_id,
            peer,
            messages[-1]["uuid"],
            stream["uuid"],
            stream["default_topic_uuid"],
        ),
    )
    db.execute(
        "UPDATE messenger_stream_bindings SET notification_mode=%s "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (stream_mode, api.project_id, peer, stream["uuid"]),
    )
    db.execute(
        "UPDATE messenger_user_topic_bindings SET notification_mode=%s "
        "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (topic_mode, api.project_id, peer, stream["default_topic_uuid"]),
    )
    native._drain()
    assert _project_count(db, api, peer) == (2, active, 2 - active)
    topic = api.get(
        f"{native.STREAM_TOPICS}{stream['default_topic_uuid']}", user=peer
    ).json()
    assert (
        topic["unread_count"],
        topic["active_unread_count"],
        topic["passive_unread_count"],
    ) == (2, active, 2 - active)
