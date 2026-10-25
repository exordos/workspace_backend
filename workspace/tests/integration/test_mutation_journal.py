# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import threading
import time
import uuid

import psycopg
import pytest
from oslo_config import cfg

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import mutation_runtime
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


if mutation_journal_opts.DOMAIN not in cfg.CONF:
    mutation_journal_opts.register_opts()


@pytest.fixture
def conversation(api, db):
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    with db.cursor() as cursor:
        cursor.execute(
            "TRUNCATE messenger_mutation_commands, messenger_publication_outbox"
        )
    stream = api.post(native.STREAMS, json={"name": "Journal regression"}).json()
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"user-{peer}")
    native._register_project_user(db, api.project_id, peer)
    response = api.post(
        f"{native.STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    assert response.status_code == 200, response.text
    native._drain()
    messages = []
    for content in ["first", "second"]:
        response = api.post(
            native.MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        messages.append(response.json())
        native._drain()
    cfg.CONF.set_override("enabled", True, group=mutation_journal_opts.DOMAIN)
    try:
        yield stream, messages, peer
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def drain(runtime, db, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        runtime.tick()
        pending = db.execute("""SELECT
            (SELECT count(*) FROM messenger_mutation_commands WHERE status='pending') +
            (SELECT count(*) FROM messenger_publication_outbox) +
            (SELECT count(*) FROM messenger_projection_tasks WHERE status IN ('pending','failed','running'))""").fetchone()[
            0
        ]
        if not pending and runtime._dispatcher.idle:
            return
        assert time.monotonic() < deadline, (
            f"Runtime failed to drain, pending={pending}"
        )
        time.sleep(0.002)


def test_http_read_admission_avoids_locked_state_and_converges_all_counters(
    api, db, conversation
):
    stream, messages, peer = conversation
    placement = messages[-1]["uuid"]
    with psycopg.connect(conftest.TEST_DB_URL) as blocker:
        blocker.execute(
            "SELECT uuid FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s FOR UPDATE",
            (api.project_id, placement, peer),
        )
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            request = pool.submit(
                api.post,
                f"{native.MESSAGES}{placement}/actions/read_up_to/invoke",
                user=peer,
            )
            response = request.result(timeout=2)
        assert response.status_code == 200, response.text
        assert (
            db.execute(
                "SELECT read_at FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
                (api.project_id, placement, peer),
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='pending'",
                (api.project_id,),
            ).fetchone()[0]
            == 1
        )
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
    finally:
        runtime.close()
    for resource, identifier in [
        (native.STREAMS, stream["uuid"]),
        (native.STREAM_TOPICS, stream["default_topic_uuid"]),
    ]:
        result = api.get(f"{resource}{identifier}", user=peer).json()
        assert (
            result["unread_count"],
            result["active_unread_count"],
            result["passive_unread_count"],
        ) == (0, 0, 0)
    assert (
        db.execute(
            "SELECT unread_count FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, peer),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT status FROM messenger_mutation_commands WHERE project_id=%s",
            (api.project_id,),
        ).fetchone()[0]
        == "applied"
    )


def test_command_topic_snapshot_and_ack_rollback_together(
    api, db, conversation, monkeypatch
):
    _stream, messages, peer = conversation
    placement = messages[-1]["uuid"]
    response = api.post(
        f"{native.MESSAGES}{placement}/actions/read_up_to/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    command = db.execute(
        "SELECT uuid,scope_key FROM messenger_mutation_commands WHERE project_id=%s",
        (api.project_id,),
    ).fetchone()
    runtime = mutation_runtime.MutationRuntime()
    original = v2_projection._emit_unread_snapshots

    def fail(*args, **kwargs):
        raise RuntimeError("injected before counter commit")

    monkeypatch.setattr(v2_projection, "_emit_unread_snapshots", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            runtime._execute(
                keyed_dispatcher.Work(
                    ("command", command[1]),
                    {command[0]},
                    frozenset(
                        runtime._topic_reservations(
                            api.project_id,
                            [{"topic_uuid": _stream["default_topic_uuid"]}],
                        )
                    ),
                )
            )
        assert (
            db.execute(
                "SELECT read_at FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
                (api.project_id, placement, peer),
            ).fetchone()[0]
            is None
        )
        assert (
            db.execute(
                "SELECT status FROM messenger_mutation_commands WHERE uuid=%s",
                (command[0],),
            ).fetchone()[0]
            == "pending"
        )
        monkeypatch.setattr(v2_projection, "_emit_unread_snapshots", original)
        drain(runtime, db)
    finally:
        runtime.close()


def test_create_is_immediately_visible_edit_flags_and_delete_use_journal(
    api, db, conversation
):
    stream, _messages, _peer = conversation
    response = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "immediately visible"},
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    assert (
        api.get(f"{native.MESSAGES}{message['uuid']}").json()["payload"]["content"]
        == "immediately visible"
    )
    response = api.put(
        f"{native.MESSAGES}{message['uuid']}",
        json={"payload": {"kind": "markdown", "content": "edited through journal"}},
    )
    assert response.status_code == 200, response.text
    assert (
        api.get(f"{native.MESSAGES}{message['uuid']}").json()["payload"]["content"]
        == "immediately visible"
    )
    response = api.post(f"{native.MESSAGES}{message['uuid']}/actions/star/invoke")
    assert response.status_code == 200, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        actual = api.get(f"{native.MESSAGES}{message['uuid']}").json()
        assert actual["payload"]["content"] == "edited through journal"
        assert actual["starred"] is True
        response = api.delete(f"{native.MESSAGES}{message['uuid']}")
        assert response.status_code == 204, response.text
        drain(runtime, db)
        assert api.get(f"{native.MESSAGES}{message['uuid']}").status_code == 404
    finally:
        runtime.close()


def test_reaction_can_be_removed_before_create_command_is_applied(
    api, db, conversation
):
    _stream, messages, _peer = conversation
    response = api.post(
        native.MESSAGE_REACTIONS,
        json={"message_uuid": messages[0]["uuid"], "emoji_name": "thumbs_up"},
    )
    assert response.status_code == 201, response.text
    reaction = response.json()
    response = api.delete(f"{native.MESSAGE_REACTIONS}{reaction['uuid']}")
    assert response.status_code == 204, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGE_REACTIONS}{reaction['uuid']}").status_code == 404
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='applied'",
                (api.project_id,),
            ).fetchone()[0]
            == 2
        )
    finally:
        runtime.close()


def test_topic_mutation_continues_during_stream_snapshot_and_requests_rerun(
    api, db, conversation, monkeypatch
):
    stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    snapshot_ready, release = threading.Event(), threading.Event()
    parent_finished = threading.Event()
    original = runtime._stream
    held = False

    def hold(session, project_id, stream_uuid, users):
        nonlocal held
        result = original(session, project_id, stream_uuid, users)
        if str(stream_uuid) == stream["uuid"] and not held:
            held = True
            snapshot_ready.set()
            assert release.wait(10)
            parent_finished.set()
        return result

    try:
        drain(runtime, db)
        api._user_seeder = None
        monkeypatch.setattr(runtime, "_stream", hold)
        runtime._dispatcher.trigger(
            ("stream", uuid.UUID(api.project_id), uuid.UUID(stream["uuid"])), [peer]
        )
        runtime.tick()
        assert snapshot_ready.wait(3)
        response = api.post(
            f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read_up_to/invoke",
            user=peer,
        )
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 3
        while (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='applied'",
                (api.project_id,),
            ).fetchone()[0]
            == 0
        ):
            runtime.tick()
            assert time.monotonic() < deadline
        # The child commits while the parent's transaction is still open.
        assert not release.is_set()
        assert not parent_finished.is_set()
        release.set()
        drain(runtime, db)
        assert (
            api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()[
                "unread_count"
            ]
            == 0
        )
    finally:
        release.set()
        runtime.close()


def test_new_copy_after_admission_has_independent_canonical_reservations(
    api, db, conversation
):
    stream, messages, _peer = conversation
    message = messages[0]
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (api.project_id, message["uuid"]),
    ).fetchone()[0]
    response = api.put(
        f"{native.MESSAGES}{message['uuid']}",
        json={"payload": {"kind": "markdown", "content": "edited all placements"}},
    )
    assert response.status_code == 200, response.text
    topic = api.post(
        native.STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Late placement"},
    ).json()
    values = {
        "uuid": str(canonical),
        "stream_uuid": stream["uuid"],
        "topic_uuid": topic["uuid"],
        "payload": message["payload"],
    }
    rejected = api.post(native.MESSAGES, json=values)
    assert rejected.status_code == 400, rejected.text
    values["uuid"] = str(uuid.uuid4())
    copy = api.post(
        native.MESSAGES,
        json=values,
    )
    assert copy.status_code == 201, copy.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGES}{message['uuid']}").json()["payload"]["content"]
            == "edited all placements"
        )
        assert (
            api.get(f"{native.MESSAGES}{copy.json()['uuid']}").json()["payload"]
            == message["payload"]
        )
    finally:
        runtime.close()


def test_duplicate_delete_commands_do_not_roll_back_first_delete(api, db, conversation):
    _stream, messages, _peer = conversation
    for _ in range(2):
        response = api.delete(f"{native.MESSAGES}{messages[0]['uuid']}")
        assert response.status_code == 204, response.text
    rows = db.execute(
        "SELECT uuid,scope_key FROM messenger_mutation_commands WHERE project_id=%s ORDER BY sequence",
        (api.project_id,),
    ).fetchall()
    runtime = mutation_runtime.MutationRuntime()
    reservations = frozenset(
        [("topic", uuid.UUID(api.project_id), uuid.UUID(messages[0]["topic_uuid"]))]
    )
    try:
        runtime._execute(
            keyed_dispatcher.Work(
                ("command", rows[0][1]), {row[0] for row in rows}, reservations
            )
        )
        assert api.get(f"{native.MESSAGES}{messages[0]['uuid']}").status_code == 404
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='applied'",
                (api.project_id,),
            ).fetchone()[0]
            == 2
        )
    finally:
        runtime.close()


def test_duplicate_pending_reaction_returns_the_persisted_identity(
    api, db, conversation
):
    _stream, messages, _peer = conversation
    values = {"message_uuid": messages[0]["uuid"], "emoji_name": "thumbs_up"}
    first = api.post(native.MESSAGE_REACTIONS, json=values)
    second = api.post(native.MESSAGE_REACTIONS, json=values)
    assert first.status_code == second.status_code == 201
    assert first.json()["uuid"] == second.json()["uuid"]
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGE_REACTIONS}{second.json()['uuid']}").status_code
            == 200
        )
        removed = api.delete(f"{native.MESSAGE_REACTIONS}{first.json()['uuid']}")
        assert removed.status_code == 204
        added = api.post(native.MESSAGE_REACTIONS, json=values)
        assert added.status_code == 201
        drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGE_REACTIONS}{added.json()['uuid']}").status_code
            == 200
        )
    finally:
        runtime.close()


def test_canonical_edit_does_not_change_a_copy_the_author_no_longer_sees(
    api, db, conversation
):
    _stream, messages, peer = conversation
    second = api.post(native.STREAMS, json={"name": "Other placement stream"}).json()
    response = api.post(
        f"{native.STREAMS}{second['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    assert response.status_code == 200
    copy = api.post(
        native.MESSAGES,
        json={
            "uuid": str(uuid.uuid4()),
            "stream_uuid": second["uuid"],
            "topic_uuid": second["default_topic_uuid"],
            "payload": messages[0]["payload"],
        },
    )
    assert copy.status_code == 201, copy.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, second["uuid"], api.user_uuid),
        )
        assert api.get(f"{native.MESSAGES}{copy.json()['uuid']}").status_code == 404
        response = api.put(
            f"{native.MESSAGES}{messages[0]['uuid']}",
            json={
                "payload": {"kind": "markdown", "content": "Edited canonical content"}
            },
        )
        assert response.status_code == 200
        drain(runtime, db)
        assert (
            api.get(f"{native.MESSAGES}{messages[0]['uuid']}").json()["payload"][
                "content"
            ]
            == "Edited canonical content"
        )
        assert (
            api.get(f"{native.MESSAGES}{copy.json()['uuid']}", user=peer).json()[
                "payload"
            ]["content"]
            == messages[0]["payload"]["content"]
        )
    finally:
        runtime.close()


def test_stream_preview_tracks_newest_topic_placement_and_delete(api, db, conversation):
    stream, messages, _peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    try:
        drain(runtime, db)
        response = api.post(
            native.MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "payload": {"kind": "markdown", "content": "Newest stream preview"},
            },
        )
        assert response.status_code == 201
        target = response.json()["uuid"]
        drain(runtime, db)
        assert (
            str(
                db.execute(
                    "SELECT last_message_uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
                    (api.project_id, stream["uuid"], api.user_uuid),
                ).fetchone()[0]
            )
            == target
        )
        response = api.delete(f"{native.MESSAGES}{target}")
        assert response.status_code == 204
        drain(runtime, db)
        assert (
            str(
                db.execute(
                    "SELECT last_message_uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
                    (api.project_id, stream["uuid"], api.user_uuid),
                ).fetchone()[0]
            )
            == messages[-1]["uuid"]
        )
    finally:
        runtime.close()
