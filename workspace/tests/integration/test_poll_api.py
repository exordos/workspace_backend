# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Public poll contract, including access, per-user state and delivery."""

import datetime
import uuid

import pytest
from oslo_config import cfg

from workspace.common import messenger_poll_opts
from workspace.common import mutation_journal_opts
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions

from workspace.messenger_api import publication
from workspace.services.messenger_workers import v2_projection

from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


def question(multiple):
    return {
        "uuid": str(uuid.uuid4()),
        "text": "Pick a time",
        "multiple_answers": multiple,
        "options": [
            {"uuid": str(uuid.uuid4()), "text": label} for label in ("12:00", "15:00")
        ],
    }


@pytest.fixture
def room(api, db, poll_creation):
    stream = api.post(native.STREAMS, json={"name": "Polls"}).json()
    peer = uuid.uuid4()
    conftest.seed_workspace_user(db, peer, f"poll-{peer}")
    native._register_project_user(db, api.project_id, peer)
    response = api.post(
        f"{native.STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    assert response.status_code == 200, response.text
    native._drain()
    return stream, peer


def create(api, room, **settings):
    payload = {
        "kind": "poll",
        "questions": [question(False), question(True)],
        "is_anonymous": False,
        "results_visibility": "after_vote",
        "allow_revote": False,
        **settings,
    }
    response = api.post(
        native.MESSAGES, json={"stream_uuid": room[0]["uuid"], "payload": payload}
    )
    assert response.status_code == 201, response.text
    native._drain()
    return response.json()


def invoke(api, path, action, body, *, user=None, status=200):
    response = api.post(f"{path}/actions/{action}/invoke", json=body, user=user)
    assert response.status_code == status, response.text
    return response.json()


def selection(q, option=0, selected=True):
    return {
        "question_uuid": q["uuid"],
        "option_uuid": q["options"][option]["uuid"],
        "selected": selected,
    }


def test_poll_clicks_noop_and_personal_snapshots(api, db, room):
    message = create(api, room, is_anonymous=True)
    stream, peer = room
    path = native.MESSAGES + message["uuid"]
    first, second = message["payload"]["questions"]
    assert (
        message["payload"]["closes_at"] is None
        and message["payload"]["closed_at"] is None
    )
    assert message["payload"]["allow_adding_options"] is False
    assert message["poll_state"]["revision"] == 1
    assert message["poll_state"]["questions"][0]["option_counts"] == {
        o["uuid"]: 0 for o in first["options"]
    }
    # Changing an old poll must still publish, without advancing the content clock.
    db.execute(
        """UPDATE messenger_messages SET created_at=now()-interval '1 year'
                  WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s)""",
        (api.project_id, message["uuid"]),
    )

    def content_identity():
        return db.execute(
            """SELECT content_uuid, content_revision, created_at, updated_at
            FROM messenger_messages WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s)""",
            (api.project_id, message["uuid"]),
        ).fetchone()

    before = content_identity()
    one = invoke(api, path, "vote", selection(first), user=peer)["poll_state"]
    assert one["voter_count"] == 1 and one["revision"] > 1
    assert invoke(api, path, "vote", selection(first), user=peer)["poll_state"] == one
    invoke(api, path, "vote", selection(first, 1), user=peer, status=409)
    invoke(api, path, "vote", selection(first, selected=False), user=peer, status=409)
    invoke(api, path, "vote", selection(second), user=peer)
    peer_state = invoke(api, path, "vote", selection(second, 1), user=peer)[
        "poll_state"
    ]
    assert (
        peer_state["voter_count"] == 1
        and peer_state["questions"][1]["voter_count"] == 1
    )
    assert before == content_identity()
    assert len(peer_state["my_votes"]) == 2
    assert api.get(path).json()["poll_state"]["my_votes"] == []
    assert (
        api.get(
            path + "/poll_voters/",
            params={k: v for k, v in selection(first).items() if k != "selected"},
        ).status_code
        == 403
    )
    native._drain()
    for reader, expected in ((peer, peer_state["my_votes"]), (api.user_uuid, [])):
        events = api.get(native.EVENTS, user=reader, params={"page_limit": 100}).json()
        updates = [
            e["payload"]
            for e in events
            if e["payload"]["kind"] == "message.updated"
            and e["payload"]["uuid"] == message["uuid"]
        ]
        assert updates and updates[-1]["poll_state"]["my_votes"] == expected
        assert updates[-1]["poll_state"]["revision"] == peer_state["revision"]
        page = api.get(
            native.MESSAGES,
            user=reader,
            params={"topic_uuid": stream["default_topic_uuid"], "page_limit": 10},
        )
        assert page.status_code == 200, page.text
        assert page.json()[0]["poll_state"] == updates[-1]["poll_state"]


def test_poll_append_settings_close_and_access(api, db, room):
    message = create(api, room, allow_adding_options=True)
    path = native.MESSAGES + message["uuid"]
    first = message["payload"]["questions"][0]
    extra = {
        "question_uuid": first["uuid"],
        "option": {"uuid": str(uuid.uuid4()), "text": "17:00"},
    }
    peer = room[1]
    invoke(api, path, "add_poll_option", extra, user=peer)
    deadline = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    ).isoformat()
    settings = {"settings": {"closes_at": deadline, "allow_adding_options": False}}
    invoke(api, path, "update_settings", settings, user=peer, status=403)
    invoke(
        api,
        path,
        "update_settings",
        {"settings": {"allow_adding_options": False}},
        status=400,
    )
    invoke(
        api,
        path,
        "update_settings",
        {
            "settings": {
                "closes_at": "2000-01-01T00:00:00Z",
                "allow_adding_options": False,
            }
        },
        status=400,
    )
    assert api.get(path).json()["payload"]["allow_adding_options"]
    changed = invoke(api, path, "update_settings", settings)
    assert changed["payload"]["questions"][0]["options"][-1] == extra["option"]
    assert (
        invoke(api, path, "update_settings", settings)["poll_state"]
        == changed["poll_state"]
    )
    closed = invoke(api, path, "close_poll", {})["poll_state"]
    assert closed["is_closed"]
    assert invoke(api, path, "close_poll", {})["poll_state"] == closed
    assert (
        invoke(api, path, "add_poll_option", extra, user=peer)["poll_state"] == closed
    )
    invoke(
        api,
        path,
        "add_poll_option",
        {**extra, "option": {**extra["option"], "text": "Different"}},
        user=peer,
        status=409,
    )
    invoke(api, path, "vote", selection(first), user=peer, status=409)
    invoke(
        api,
        path,
        "update_settings",
        {"settings": {"closes_at": None, "allow_adding_options": True}},
        status=409,
    )
    db.execute(
        "UPDATE messenger_stream_bindings SET active=false WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (api.project_id, room[0]["uuid"], peer),
    )
    invoke(api, path, "add_poll_option", extra, user=peer, status=404)


def test_poll_voters_cursor_survives_withdrawal_and_replacement(api, room):
    message = create(api, room, allow_revote=True)
    path = native.MESSAGES + message["uuid"]
    first = message["payload"]["questions"][0]
    users = sorted([str(room[1]), str(api.user_uuid)])
    for user in users:
        invoke(api, path, "vote", selection(first), user=user)
    query = {
        "question_uuid": first["uuid"],
        "option_uuid": first["options"][0]["uuid"],
        "page_limit": 1,
    }
    response = api.get(path + "/poll_voters/", params=query)
    assert response.status_code == 200, response.text
    assert response.json() == [{"uuid": users[0]}]
    marker = response.headers["X-Pagination-Marker"]
    replacement = invoke(api, path, "vote", selection(first, 1), user=users[0])[
        "poll_state"
    ]
    assert replacement["my_votes"] == [
        {"question_uuid": first["uuid"], "option_uuids": [first["options"][1]["uuid"]]}
    ]
    assert api.get(
        path + "/poll_voters/", params={**query, "page_marker": marker}
    ).json() == [{"uuid": users[1]}]
    assert (
        api.get(
            path + "/poll_voters/", project=str(uuid.uuid4()), params=query
        ).status_code
        == 404
    )


@pytest.mark.parametrize("journal_enabled", [False, True], ids=["sync", "journal"])
def test_poll_write_boundary_and_immutable_kind(
    api, room, journal_enabled, monkeypatch
):
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: journal_enabled)
    message = create(api, room)
    payload = message["payload"].copy()
    payload.pop("closed_at")
    path = native.MESSAGES + message["uuid"]
    envelope = {"stream_uuid": room[0]["uuid"], "payload": payload}
    for other in (
        {**envelope, "payload": {**payload, "closed_at": None}},
        {**envelope, "poll_state": message["poll_state"]},
    ):
        assert api.post(native.MESSAGES, json=other).status_code == 400
    markdown = {"kind": "markdown", "content": "plain"}
    ordinary = api.post(native.MESSAGES, json={**envelope, "payload": markdown}).json()
    assert ordinary["poll_state"] is None
    for target, proposed, status in (
        (path, payload, 409),
        (path, markdown, 409),
        (native.MESSAGES + ordinary["uuid"], payload, 400),
        (path, {**payload, "closed_at": None}, 400),
    ):
        response = api.put(target, json={"payload": proposed})
        assert response.status_code == status, response.text
    stream = api.post(
        native.STREAMS,
        json={
            "name": "Encrypted",
            "encryption": True,
            "current_encryption_key_uuid": str(uuid.uuid4()),
        },
    ).json()
    assert (
        api.post(
            native.MESSAGES, json={**envelope, "stream_uuid": stream["uuid"]}
        ).status_code
        == 400
    )


def test_poll_deferred_delivery_and_replay_respect_revoked_membership(api, db, room):
    stream, peer = room
    message = create(api, room, is_anonymous=True)
    path = native.MESSAGES + message["uuid"]
    body = selection(message["payload"]["questions"][0])
    invoke(api, path, "vote", body)
    invoke(api, path, "vote", body, user=peer)

    with publication.defer(), contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(session, "poll-deferred-delivery")
    pending = db.execute(
        "SELECT payload,recipient_payloads FROM messenger_publication_outbox "
        "WHERE project_id=%s AND entity_uuid=%s AND kind='message.updated'",
        (api.project_id, message["uuid"]),
    ).fetchall()
    assert pending
    for common, overrides in pending:
        assert common["poll_state"]["my_votes"] == []
        assert overrides[str(peer)]["poll_state"]["my_votes"]

    binding = db.execute(
        "SELECT uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
        (api.project_id, stream["uuid"], peer),
    ).fetchone()[0]
    removed = api.delete(native.STREAM_BINDINGS + str(binding))
    assert removed.status_code == 204, removed.text
    with contexts.Context().session_manager() as session:
        publication.publish_pending(session)

    def has_update(user):
        response = api.get(native.EVENTS, user=user, params={"page_limit": 100})
        assert response.status_code == 200, response.text
        return any(
            row["payload"].get("uuid") == message["uuid"]
            and row["payload"]["kind"] == "message.updated"
            for row in response.json()
        )

    assert has_update(api.user_uuid)
    assert not has_update(peer)
    added = api.post(
        f"{native.STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer)]},
    )
    assert added.status_code == 200, added.text
    native._drain()
    assert not has_update(peer)


def test_poll_rollout_can_pause_creation_without_disabling_votes(api, room):
    message = create(api, room)
    cfg.CONF.clear_override("creation_enabled", group=messenger_poll_opts.DOMAIN)
    payload = message["payload"].copy()
    payload.pop("closed_at")
    res = api.post(
        native.MESSAGES, json={"stream_uuid": room[0]["uuid"], "payload": payload}
    )
    assert res.status_code == 409, res.text
    vote = invoke(
        api,
        native.MESSAGES + message["uuid"],
        "vote",
        selection(payload["questions"][0]),
    )
    assert vote["poll_state"]["voter_count"] == 1


def test_poll_deletion_does_not_hide_next_history_page(api, db, room, monkeypatch):
    stream, _ = room

    def send(text):
        response = api.post(
            native.MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "payload": {"kind": "markdown", "content": text},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["uuid"]

    first = send("First")
    poll = create(api, room)
    last = send("Last")
    execute = sessions.PgSQLSession.execute
    deleted = False

    def delete_after_select(session, statement, *args, **kwargs):
        nonlocal deleted
        result = execute(session, statement, *args, **kwargs)
        if not deleted and "messenger_api_message_page_candidates_v1" in statement:
            deleted = True
            # Delete the lookahead row from another connection after the page read.
            db.execute(
                """SELECT messenger_delete_message(project_id, message_uuid)
                   FROM messenger_message_placements WHERE project_id=%s AND uuid=%s""",
                (api.project_id, poll["uuid"]),
            )
        return result

    monkeypatch.setattr(sessions.PgSQLSession, "execute", delete_after_select)
    params = {
        "topic_uuid": stream["default_topic_uuid"],
        "page_limit": 1,
        "sort_dir": "asc",
    }
    page = api.get(native.MESSAGES, params=params)
    assert page.status_code == 200, page.text
    assert deleted and [m["uuid"] for m in page.json()] == [first]
    assert page.headers.get("X-Pagination-Marker") == first
    following = api.get(native.MESSAGES, params={**params, "page_marker": first})
    assert following.status_code == 200, following.text
    assert [m["uuid"] for m in following.json()] == [last]
    assert "X-Pagination-Marker" not in following.headers
