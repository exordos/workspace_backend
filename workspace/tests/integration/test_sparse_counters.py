# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Counter projection must not hold up message and flag producers."""

import concurrent.futures
import threading
import time
import uuid

import psycopg
import pytest
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mutation_journal_opts
from workspace.messenger_api import message_flags, publication
from workspace.messenger_api.api import v2_store
from workspace.services.messenger_workers import counters, v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal_tests

conversation = journal_tests.conversation


def _count_one():
    with contexts.Context().session_manager() as session:
        return counters.process_pending(session)


def _star(api, peer, placement):
    with contexts.Context().session_manager():
        store = v2_store.MessengerV2Store(api.project_id, peer)
        return store.perform_action("messages", uuid.UUID(placement), "star", {})


def _create_and_deliver(api, peer, stream):
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
    with contexts.Context().session_manager() as session:
        publication.publish_pending(session)
    with contexts.Context().session_manager() as session:
        return session.execute(
            "SELECT EXISTS(SELECT 1 FROM m_workspace_visible_events "
            "WHERE project_id=%s AND user_uuid=%s AND object_type='message' "
            "AND action='created' AND payload->>'uuid'=%s) AS visible",
            (api.project_id, peer, placement),
        ).fetchone()["visible"]


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
