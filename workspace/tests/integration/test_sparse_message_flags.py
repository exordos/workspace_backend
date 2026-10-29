# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import concurrent.futures
import time
import uuid

import psycopg

from workspace.tests.integration import conftest, test_messenger_v2


def test_sparse_schema_has_no_combined_message_state(db):
    assert db.execute(
        "SELECT to_regclass('messenger_user_message_states')"
    ).fetchone() == (None,)
    for name in ("unreads", "stars", "pins", "mentions"):
        assert (
            db.execute(
                "SELECT to_regclass(%s)", (f"messenger_user_message_{name}",)
            ).fetchone()[0]
            is not None
        )
    assert (
        db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='messenger_user_message_bindings' "
            "AND column_name IN ('read_at','starred','pinned','mentioned',"
            "'relation_role','permissions','visibility','membership_generation')"
        ).fetchall()
        == []
    )


def test_read_removes_only_unread_and_keeps_message_visibility(api, db):
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"user-{peer}")
    test_messenger_v2._register_project_user(db, api.project_id, peer)
    stream = api.post(
        test_messenger_v2.STREAMS,
        json={"name": "Sparse flags", "description": ""},
    ).json()
    api.post(
        f"{test_messenger_v2.STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    test_messenger_v2._drain()
    response = api.post(
        test_messenger_v2.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": f"[Peer](urn:user:{peer})"},
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    test_messenger_v2._drain()
    key = (api.project_id, peer, message["uuid"])
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        key,
    ).fetchone() == (1,)
    starred = api.post(
        f"{test_messenger_v2.MESSAGES}{message['uuid']}/actions/star/invoke", user=peer
    )
    assert starred.status_code == 200, starred.text
    read = api.post(
        f"{test_messenger_v2.MESSAGES}{message['uuid']}/actions/read/invoke", user=peer
    )
    assert read.status_code == 200, read.text
    assert read.json()["read"] and read.json()["starred"] and read.json()["mentioned"]
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        key,
    ).fetchone() == (0,)
    assert (
        api.get(f"{test_messenger_v2.MESSAGES}{message['uuid']}", user=peer).status_code
        == 200
    )


def _shared_scope(api, db):
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"user-{peer}")
    test_messenger_v2._register_project_user(db, api.project_id, peer)
    stream = api.post(
        test_messenger_v2.STREAMS,
        json={"name": "Shared preview", "description": ""},
    ).json()
    added = api.post(
        f"{test_messenger_v2.STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    assert added.status_code == 200, added.text
    test_messenger_v2._drain()
    return stream, peer


def _scope_message(api, stream, topic_uuid, content):
    response = api.post(
        test_messenger_v2.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": content},
        },
    )
    assert response.status_code == 201, response.text
    test_messenger_v2._drain()
    return response.json()


def _soft_delete_canonical(db, project_id, placement_uuid):
    db.execute(
        "UPDATE messenger_messages SET deleted_at=NOW() WHERE project_id=%s AND uuid=("
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s)",
        (project_id, project_id, placement_uuid),
    )


def test_shared_preview_is_independent_of_personal_message_visibility(api, db):
    stream, peer = _shared_scope(api, db)
    topic = stream["default_topic_uuid"]
    first = _scope_message(api, stream, topic, "First")
    last = _scope_message(api, stream, topic, "Last")
    db.execute(
        "DELETE FROM messenger_user_message_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (api.project_id, peer, last["uuid"]),
    )
    test_messenger_v2._drain()
    for user in (api.user_uuid, peer):
        assert (
            api.get(f"{test_messenger_v2.STREAMS}{stream['uuid']}", user=user).json()[
                "last_message_uuid"
            ]
            == last["uuid"]
        )
        assert (
            api.get(f"{test_messenger_v2.STREAM_TOPICS}{topic}", user=user).json()[
                "last_message_uuid"
            ]
            == last["uuid"]
        )
    assert (
        api.get(f"{test_messenger_v2.MESSAGES}{last['uuid']}", user=peer).status_code
        == 404
    )
    assert (
        api.get(f"{test_messenger_v2.MESSAGES}{first['uuid']}", user=peer).status_code
        == 200
    )


def test_restoring_a_topic_does_not_restore_a_deleted_canonical_as_last(api, db):
    stream, _peer = _shared_scope(api, db)
    topic = stream["default_topic_uuid"]
    first = _scope_message(api, stream, topic, "Alive")
    last = _scope_message(api, stream, topic, "Deleted canonical")
    _soft_delete_canonical(db, api.project_id, last["uuid"])
    db.execute(
        "UPDATE messenger_topics SET deleted_at=NOW() WHERE project_id=%s AND uuid=%s",
        (api.project_id, topic),
    )
    db.execute(
        "UPDATE messenger_topics SET deleted_at=NULL WHERE project_id=%s AND uuid=%s",
        (api.project_id, topic),
    )
    assert (
        api.get(f"{test_messenger_v2.STREAMS}{stream['uuid']}").json()[
            "last_message_uuid"
        ]
        == first["uuid"]
    )
    assert (
        api.get(f"{test_messenger_v2.STREAM_TOPICS}{topic}").json()["last_message_uuid"]
        == first["uuid"]
    )


def test_placement_moved_into_a_deleted_topic_cannot_become_a_shared_preview(api, db):
    stream, _peer = _shared_scope(api, db)
    first_topic = stream["default_topic_uuid"]
    topic_response = api.post(
        test_messenger_v2.STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Deleted destination"},
    )
    assert topic_response.status_code == 201, topic_response.text
    second_topic = topic_response.json()["uuid"]
    first = _scope_message(api, stream, first_topic, "Visible")
    moved = _scope_message(api, stream, first_topic, "Moved to deleted scope")
    db.execute(
        "UPDATE messenger_topics SET deleted_at=NOW() WHERE project_id=%s AND uuid=%s",
        (api.project_id, second_topic),
    )
    db.execute(
        "UPDATE messenger_message_placements SET topic_uuid=%s WHERE project_id=%s AND uuid=%s",
        (second_topic, api.project_id, moved["uuid"]),
    )
    assert (
        api.get(f"{test_messenger_v2.STREAMS}{stream['uuid']}").json()[
            "last_message_uuid"
        ]
        == first["uuid"]
    )
    assert (
        db.execute(
            "SELECT placement_uuid FROM messenger_topic_last_messages WHERE project_id=%s AND topic_uuid=%s",
            (api.project_id, second_topic),
        ).fetchall()
        == []
    )


def test_counter_worker_refreshes_summary_when_first_and_last_message_changes(api, db):
    stream, peer = _shared_scope(api, db)
    topic = stream["default_topic_uuid"]
    db.execute(
        "UPDATE messenger_topics SET summary='Saved empty topic' WHERE project_id=%s AND uuid=%s",
        (api.project_id, topic),
    )
    message = _scope_message(api, stream, topic, "After summary")
    for user in (api.user_uuid, peer):
        assert (
            api.get(f"{test_messenger_v2.STREAM_TOPICS}{topic}", user=user).json()[
                "summary_has_new_messages"
            ]
            is True
        )
    _soft_delete_canonical(db, api.project_id, message["uuid"])
    test_messenger_v2._drain()
    for user in (api.user_uuid, peer):
        result = api.get(f"{test_messenger_v2.STREAM_TOPICS}{topic}", user=user).json()
        assert result.get("last_message_uuid") is None
        assert result["summary_has_new_messages"] is False


def _wait_for_database_lock(observer, pid, future):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = observer.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
        ).fetchone()
        if state == ("Lock",):
            return
        assert not future.done(), (
            "Scope mutation completed while another repair held its pointer"
        )
    raise AssertionError(f"Database session {pid} did not wait for the scope mutation")


def test_concurrent_deletion_of_last_and_previous_messages_keeps_a_live_preview(
    api, db
):
    stream, _peer = _shared_scope(api, db)
    topic = stream["default_topic_uuid"]
    first = _scope_message(api, stream, topic, "Remains visible")
    previous = _scope_message(api, stream, topic, "Previous last")
    last = _scope_message(api, stream, topic, "Current last")
    with (
        psycopg.connect(conftest.TEST_DB_URL) as blocker,
        psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as deleting_last,
        psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as deleting_previous,
        concurrent.futures.ThreadPoolExecutor(2) as pool,
    ):
        blocker.execute(
            "SELECT placement_uuid FROM messenger_stream_last_messages WHERE project_id=%s AND stream_uuid=%s FOR UPDATE",
            (api.project_id, stream["uuid"]),
        )
        last_future = pool.submit(
            _soft_delete_canonical, deleting_last, api.project_id, last["uuid"]
        )
        try:
            _wait_for_database_lock(db, deleting_last.info.backend_pid, last_future)
            previous_future = pool.submit(
                _soft_delete_canonical,
                deleting_previous,
                api.project_id,
                previous["uuid"],
            )
            _wait_for_database_lock(
                db, deleting_previous.info.backend_pid, previous_future
            )
        finally:
            blocker.rollback()
        last_future.result(5)
        previous_future.result(5)
    for scope, scope_uuid in (("stream", stream["uuid"]), ("topic", topic)):
        assert db.execute(
            f"SELECT placement_uuid FROM messenger_{scope}_last_messages WHERE project_id=%s AND {scope}_uuid=%s",
            (api.project_id, scope_uuid),
        ).fetchone() == (uuid.UUID(first["uuid"]),)


def test_moving_a_placement_into_a_deleted_stream_does_not_set_shared_previews(api, db):
    stream, _peer = _shared_scope(api, db)
    message = _scope_message(
        api, stream, stream["default_topic_uuid"], "Moved to deleted stream"
    )
    destination = api.post(
        test_messenger_v2.STREAMS,
        json={"name": "Deleted stream destination", "description": ""},
    ).json()
    db.execute(
        "UPDATE messenger_streams SET deleted_at=NOW() WHERE project_id=%s AND uuid=%s",
        (api.project_id, destination["uuid"]),
    )
    db.execute(
        "UPDATE messenger_message_placements SET stream_uuid=%s,topic_uuid=%s WHERE project_id=%s AND uuid=%s",
        (
            destination["uuid"],
            destination["default_topic_uuid"],
            api.project_id,
            message["uuid"],
        ),
    )
    for scope, scope_uuid in (
        ("stream", destination["uuid"]),
        ("topic", destination["default_topic_uuid"]),
    ):
        assert not db.execute(
            f"SELECT placement_uuid FROM messenger_{scope}_last_messages WHERE project_id=%s AND {scope}_uuid=%s",
            (api.project_id, scope_uuid),
        ).fetchall()


def test_stream_restore_rebuilds_only_live_shared_previews(api, db):
    stream, peer = _shared_scope(api, db)
    topic = stream["default_topic_uuid"]
    first = _scope_message(api, stream, topic, "Restored live message")
    last = _scope_message(api, stream, topic, "Deleted before stream restore")
    _soft_delete_canonical(db, api.project_id, last["uuid"])
    db.execute(
        "UPDATE messenger_streams SET deleted_at=NOW() WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream["uuid"]),
    )
    for scope, scope_uuid in (("stream", stream["uuid"]), ("topic", topic)):
        assert not db.execute(
            f"SELECT placement_uuid FROM messenger_{scope}_last_messages WHERE project_id=%s AND {scope}_uuid=%s",
            (api.project_id, scope_uuid),
        ).fetchall()
    db.execute(
        "UPDATE messenger_streams SET deleted_at=NULL WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream["uuid"]),
    )
    for user in (api.user_uuid, peer):
        assert (
            api.get(f"{test_messenger_v2.STREAMS}{stream['uuid']}", user=user).json()[
                "last_message_uuid"
            ]
            == first["uuid"]
        )
        assert (
            api.get(f"{test_messenger_v2.STREAM_TOPICS}{topic}", user=user).json()[
                "last_message_uuid"
            ]
            == first["uuid"]
        )
