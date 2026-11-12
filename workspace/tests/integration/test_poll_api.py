# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Public poll contract, including access, per-user state and delivery."""

import datetime
import json
import uuid

import pytest

from workspace.common import mutation_journal_opts
from restalchemy.common import contexts
from restalchemy.storage.sql import sessions

from workspace.messenger_api import events, publication
from workspace.messenger_api.dm import polls
from workspace.services.messenger_workers import v2_projection

from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


def question():
    return {
        "uuid": str(uuid.uuid4()),
        "text": "Pick a time",
        "options": [
            {"uuid": str(uuid.uuid4()), "text": label} for label in ("12:00", "15:00")
        ],
    }


@pytest.fixture
def room(api, db):
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
        "questions": [question(), question()],
        "multiple_answers": False,
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


def test_voter_pages_follow_per_question_visibility_until_closure(api, room):
    message = create(api, room, allow_revote=True)
    path = native.MESSAGES + message["uuid"]
    first, second = message["payload"]["questions"]
    for q in (first, second):
        invoke(api, path, "vote", selection(q), user=room[1])

    def voters(q):
        return api.get(
            path + "/poll_voters/",
            params={"question_uuid": q["uuid"], "option_uuid": q["options"][0]["uuid"]},
        )

    assert voters(first).status_code == 403
    invoke(api, path, "vote", selection(first))
    assert voters(first).status_code == 200
    assert voters(second).status_code == 403
    invoke(api, path, "vote", selection(first, selected=False))
    assert voters(first).status_code == 403
    invoke(api, path, "close_poll", {})
    assert voters(first).status_code == 200
    assert voters(second).status_code == 200


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
    assert "option_voter_previews" not in one["questions"][0]
    assert invoke(api, path, "vote", selection(first), user=peer)["poll_state"] == one
    invoke(api, path, "vote", selection(first, 1), user=peer, status=409)
    invoke(api, path, "vote", selection(first, selected=False), user=peer, status=409)
    invoke(api, path, "vote", selection(second), user=peer)
    invoke(api, path, "vote", selection(second, 1), user=peer, status=409)
    peer_state = api.get(path, user=peer).json()["poll_state"]
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
        assert all(
            "option_voter_previews" not in question
            for question in updates[-1]["poll_state"]["questions"]
        )
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
    assert api.get(path).json()["payload"]["closed_at"] is not None
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
    voters_path = path + "/poll_voters/"
    voter_filters = {
        "question_uuid": first["uuid"],
        "option_uuid": first["options"][0]["uuid"],
    }
    for limit in (None, 100, 0, 101):
        params = (
            voter_filters if limit is None else {**voter_filters, "page_limit": limit}
        )
        page = api.get(voters_path, params=params)
        if limit in (0, 101):
            assert page.status_code == 400
        else:
            assert page.status_code == 200, page.text
            assert page.headers["X-Pagination-Limit"] == str(limit or 50)
    query = {**voter_filters, "page_limit": 1}
    response = api.get(voters_path, params=query)
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


def test_poll_voter_preview_bounds_and_pagination(api, db, room):
    message = create(api, room, results_visibility="always")
    path = native.MESSAGES + message["uuid"]
    first = message["payload"]["questions"][0]
    option = first["options"][0]["uuid"]
    assert "option_voter_previews" not in message["poll_state"]["questions"][0]

    users = sorted(uuid.uuid4() for _ in range(21))
    for user in users:
        conftest.seed_workspace_user(db, user, f"poll-{user}")
        native._register_project_user(db, api.project_id, user)
    response = api.post(
        f"{native.STREAMS}{room[0]['uuid']}/actions/add_users/invoke",
        json={"member": [str(user) for user in users]},
    )
    assert response.status_code == 200, response.text
    native._drain()

    def add_votes(voters):
        db.execute(
            """
            INSERT INTO messenger_poll_votes
                (project_id, message_uuid, user_uuid, question_uuid, option_uuid)
            SELECT p.project_id, p.message_uuid, user_uuid, %s, %s
            FROM messenger_message_placements p
            CROSS JOIN unnest(%s::uuid[]) AS voters(user_uuid)
            WHERE p.project_id=%s AND p.uuid=%s
            """,
            (first["uuid"], option, voters, api.project_id, message["uuid"]),
        )

    inserted = 0
    for count in (19, 20, 21):
        # Insert in reverse UUID order to check that selection has an explicit order.
        add_votes(list(reversed(users[inserted:count])))
        inserted = count
        history = api.get(
            native.MESSAGES,
            params={"topic_uuid": room[0]["default_topic_uuid"], "page_limit": 10},
        )
        assert history.status_code == 200, history.text
        assert len(history.json()) == 1
        state = history.json()[0]["poll_state"]
        assert api.get(path).json()["poll_state"] == state
        assert state["questions"][0]["option_counts"][option] == count
        assert state["questions"][0]["option_voter_previews"] == {
            option: [str(user) for user in users[: min(count, 20)]]
        }
        assert "option_voter_previews" not in state["questions"][1]

    preview = state["questions"][0]["option_voter_previews"][option]
    voter_query = {
        "question_uuid": first["uuid"],
        "option_uuid": option,
        "page_limit": 20,
    }
    page = api.get(path + "/poll_voters/", params=voter_query)
    assert page.status_code == 200, page.text
    assert page.json() == [{"uuid": user} for user in preview]
    assert page.headers["X-Pagination-Marker"] == preview[-1]
    rest = api.get(
        path + "/poll_voters/", params={**voter_query, "page_marker": preview[-1]}
    )
    assert rest.status_code == 200, rest.text
    assert rest.json() == [{"uuid": str(users[-1])}]
    assert "X-Pagination-Marker" not in rest.headers


def test_after_vote_previews_are_per_question_and_per_recipient(api, db, room):
    message = create(api, room, allow_revote=True)
    path = native.MESSAGES + message["uuid"]
    peer = room[1]
    first, second = message["payload"]["questions"]
    first_preview = {first["options"][0]["uuid"]: [str(peer)]}
    second_preview = {second["options"][0]["uuid"]: [str(api.user_uuid)]}

    peer_state = invoke(api, path, "vote", selection(first), user=peer)["poll_state"]
    assert peer_state["questions"][0]["option_voter_previews"] == first_preview
    assert all(
        "option_voter_previews" not in question
        for question in api.get(path).json()["poll_state"]["questions"]
    )
    author_state = invoke(api, path, "vote", selection(second))["poll_state"]
    assert "option_voter_previews" not in author_state["questions"][0]
    assert author_state["questions"][1]["option_voter_previews"] == second_preview

    with publication.defer(), contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(session, "poll-preview-delivery")
    pending = db.execute(
        "SELECT uuid,payload,recipient_payloads FROM messenger_publication_outbox "
        "WHERE project_id=%s AND entity_uuid=%s AND kind='message.updated' "
        "ORDER BY sequence DESC LIMIT 1",
        (api.project_id, message["uuid"]),
    ).fetchone()
    assert pending is not None
    event_uuid, common, overrides = pending
    assert all(
        "option_voter_previews" not in question
        for question in common["poll_state"]["questions"]
    )
    assert common["user_uuid"] == {
        "poll_previews_v1": {
            first["uuid"]: first_preview,
            second["uuid"]: second_preview,
        }
    }
    expected = {str(peer): peer_state, str(api.user_uuid): author_state}
    current = {user: api.get(path, user=user).json()["poll_state"] for user in expected}
    for user, override in overrides.items():
        assert "poll_state" not in override
        assert override["user_uuid"] == {"poll_my_votes_v1": current[user]["my_votes"]}

    # Change live votes before replay. Both compact and legacy payloads describe
    # the event revision, so the former voter still sees their historical result.
    invoke(api, path, "vote", {**selection(first), "selected": False}, user=peer)
    with contexts.Context().session_manager() as session:
        legacy_common = {
            key: value for key, value in common.items() if key != "user_uuid"
        }
        legacy_overrides = {
            user: {**override, "poll_state": current[user]}
            for user, override in overrides.items()
        }
        for override in legacy_overrides.values():
            del override["user_uuid"]
        legacy_uuid = publication.enqueue(
            session,
            api.project_id,
            message["uuid"],
            current,
            "message.updated",
            legacy_common,
            legacy_overrides,
        )[0].uuid
        publication.publish_pending(session)

    for user, state in expected.items():
        read = current[user]
        for extra_filters in (
            {"epoch_version>": 0},
            {"epoch_version>": 0, "object_type": "message"},
        ):
            response = api.get(
                native.EVENTS, user=user, params={"page_limit": 100, **extra_filters}
            )
            assert response.status_code == 200, response.text
            updates = {event["uuid"]: event["payload"] for event in response.json()}
            for stored_uuid in (event_uuid, legacy_uuid):
                assert updates[str(stored_uuid)]["poll_state"] == read
                assert updates[str(stored_uuid)]["user_uuid"] == user
        legacy_read = db.execute(
            "SELECT payload FROM m_workspace_visible_events WHERE uuid=%s AND user_uuid=%s",
            (event_uuid, user),
        ).fetchone()[0]
        assert legacy_read["user_uuid"] == user
        assert legacy_read["poll_state"] == common["poll_state"]
        assert [
            question.get("option_voter_previews") for question in read["questions"]
        ] == [question.get("option_voter_previews") for question in state["questions"]]
    closed = invoke(api, path, "close_poll", {})["poll_state"]
    assert [
        question.get("option_voter_previews") for question in closed["questions"]
    ] == [
        None,
        second_preview,
    ]
    native._drain()
    response = api.get(native.EVENTS, user=peer, params={"epoch_version>": 0})
    assert response.status_code == 200, response.text
    peer_updates = [
        event["payload"]["poll_state"]
        for event in response.json()
        if event["payload"]["kind"] == "message.updated"
        and event["payload"]["uuid"] == message["uuid"]
    ]
    assert peer_updates[-1] == {**closed, "my_votes": []}


def test_poll_publication_stores_voter_previews_once_for_large_audience(api, db):
    recipients = [str(uuid.UUID(int=index)) for index in range(1, 1001)]
    question_uuid = str(uuid.uuid4())
    option_uuid = str(uuid.uuid4())
    placement_uuid = uuid.uuid4()
    previews = {option_uuid: recipients[:20]}
    state = {
        "revision": 10,
        "is_closed": False,
        "voter_count": 1000,
        "my_votes": [{"question_uuid": question_uuid, "option_uuids": [option_uuid]}],
        "questions": [
            {
                "question_uuid": question_uuid,
                "voter_count": 1000,
                "option_counts": {option_uuid: 1000},
                "option_voter_previews": previews,
            }
        ],
    }
    payload = {
        "uuid": str(placement_uuid),
        "payload": {
            "kind": "poll",
            "results_visibility": "after_vote",
            "is_anonymous": False,
        },
        "poll_state": state,
    }
    common, overrides = events._compress_recipient_payloads(
        dict.fromkeys(recipients, payload)
    )
    full_overrides = {user: {"poll_state": state} for user in recipients}
    with contexts.Context().session_manager() as session:
        event_uuid = publication.enqueue(
            session,
            api.project_id,
            placement_uuid,
            recipients,
            "message.updated",
            common,
            overrides,
        )[0].uuid
    stored = db.execute(
        "SELECT payload, recipient_payloads FROM messenger_publication_outbox WHERE uuid=%s",
        (event_uuid,),
    ).fetchone()
    assert stored == (common, overrides)
    assert len(json.dumps(overrides)) < len(json.dumps(full_overrides)) / 5

    with contexts.Context().session_manager() as session:
        publication.publish_pending(session)
    stored = db.execute(
        """
        SELECT event.payload, jsonb_object_agg(override.user_uuid, override.payload)
        FROM m_workspace_broadcast_message_events_v1 AS event
        JOIN m_workspace_event_recipient_payloads_v1 AS override
          ON override.event_uuid=event.uuid
        WHERE event.uuid=%s
        GROUP BY event.uuid, event.payload
        """,
        (event_uuid,),
    ).fetchone()
    published, overrides = stored
    assert published["user_uuid"] == {"poll_previews_v1": {question_uuid: previews}}
    assert "option_voter_previews" not in published["poll_state"]["questions"][0]
    assert overrides == {
        user: {"user_uuid": {"poll_my_votes_v1": state["my_votes"]}}
        for user in recipients
    }


@pytest.mark.parametrize("visibility", ["always", "after_vote"])
def test_personal_poll_event_keeps_state_in_direct_and_deferred_delivery(
    api, db, room, monkeypatch, visibility
):
    message = create(api, room, results_visibility=visibility)
    path = native.MESSAGES + message["uuid"]
    message = invoke(api, path, "vote", selection(message["payload"]["questions"][0]))
    project_uuid = uuid.UUID(str(api.project_id))
    user_uuid = uuid.UUID(str(api.user_uuid))
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: False)
    with contexts.Context().session_manager() as session:
        direct_epoch = events._create_workspace_event(
            project_uuid, user_uuid, "message.updated", message, session
        )
    with publication.defer(), contexts.Context().session_manager() as session:
        deferred_uuid = events._create_workspace_event(
            project_uuid, user_uuid, "message.updated", message, session
        ).uuid
    with contexts.Context().session_manager() as session:
        publication.publish_pending(session)

    old_reader_payload = db.execute(
        "SELECT payload FROM m_workspace_visible_events WHERE uuid=%s AND user_uuid=%s",
        (deferred_uuid, api.user_uuid),
    ).fetchone()[0]
    assert old_reader_payload["user_uuid"] == str(api.user_uuid)
    assert old_reader_payload["poll_state"]["my_votes"] == []
    question = old_reader_payload["poll_state"]["questions"][0]
    assert ("option_voter_previews" in question) == (visibility == "always")
    response = api.get(native.EVENTS, params={"epoch_version>": 0, "page_limit": 100})
    assert response.status_code == 200, response.text
    states = [
        event["payload"]["poll_state"]
        for event in response.json()
        if event["epoch_version"] == direct_epoch or event["uuid"] == str(deferred_uuid)
    ]
    assert states == [message["poll_state"], message["poll_state"]]


def test_message_and_poll_request_validation(api, room):
    message = create(api, room)
    path = native.MESSAGES + message["uuid"]
    payload = message["payload"].copy()
    del payload["closed_at"]
    envelope = {"stream_uuid": room[0]["uuid"], "payload": payload}
    with_closed_at = {**envelope, "payload": {**payload, "closed_at": None}}
    assert api.post(native.MESSAGES, json=with_closed_at).status_code == 400
    assert (
        api.post(native.MESSAGES, json={**envelope, "poll_state": None}).status_code
        == 403
    )
    assert api.put(path, json={"poll_state": message["poll_state"]}).status_code == 403
    assert api.post(native.MESSAGES, json=["invalid"]).status_code == 400
    invoke(api, path, "star", {"resource": None}, status=400)
    assert invoke(api, path, "star", {"poll_state": None})["starred"]
    choice = selection(payload["questions"][0])
    invoke(api, path, "vote", {**choice, "poll_state": None}, status=400)


@pytest.mark.parametrize("journal_enabled", [False, True], ids=["sync", "journal"])
def test_poll_kind_is_immutable(api, room, journal_enabled, monkeypatch):
    monkeypatch.setattr(mutation_journal_opts, "enabled", lambda: journal_enabled)
    message = create(api, room)
    payload = message["payload"].copy()
    payload.pop("closed_at")
    path = native.MESSAGES + message["uuid"]
    envelope = {"stream_uuid": room[0]["uuid"], "payload": payload}
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
        assert overrides[str(peer)]["user_uuid"]["poll_my_votes_v1"]
        assert "poll_state" not in overrides[str(peer)]

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


def test_poll_creation_retry_compares_original_payload_and_returns_current_state(
    api, room, monkeypatch
):
    stream, peer = room
    body = {
        "uuid": str(uuid.uuid4()),
        "stream_uuid": stream["uuid"],
        "payload": {
            "kind": "poll",
            "questions": [question(), question()],
            "multiple_answers": False,
            "is_anonymous": True,
            "allow_revote": False,
            "results_visibility": "always",
        },
    }
    created = api.post(native.MESSAGES, json=body)
    assert created.status_code == 201, created.text
    path = native.MESSAGES + created.json()["uuid"]
    closed = invoke(api, path, "close_poll", {})
    monkeypatch.setattr(polls, "MAX_POLL_QUESTIONS", 1)
    retry = api.post(native.MESSAGES, json=body)
    assert retry.status_code == 201, retry.text
    assert retry.json() == closed
    fresh = {**body, "uuid": str(uuid.uuid4())}
    assert api.post(native.MESSAGES, json=fresh).status_code == 400
    assert api.post(native.MESSAGES, json=body, user=peer).status_code == 400
    changed = {**body, "payload": {**body["payload"], "is_anonymous": False}}
    assert api.post(native.MESSAGES, json=changed).status_code == 400


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


@pytest.mark.parametrize("multiple_answers", [False, True])
@pytest.mark.parametrize("allow_revote", [False, True])
def test_poll_global_selection_mode_keeps_questions_independent(
    api, room, multiple_answers, allow_revote
):
    message = create(
        api, room, multiple_answers=multiple_answers, allow_revote=allow_revote
    )
    path = native.MESSAGES + message["uuid"]
    questions = message["payload"]["questions"]
    for q in questions:
        invoke(api, path, "vote", selection(q))
    for q in questions:
        invoke(
            api,
            path,
            "vote",
            selection(q, 1),
            status=200 if multiple_answers or allow_revote else 409,
        )
    state = api.get(path).json()["poll_state"]
    assert state["voter_count"] == 1
    assert state["my_votes"] == [
        {
            "question_uuid": q["uuid"],
            "option_uuids": [
                o["uuid"]
                for o in (
                    q["options"]
                    if multiple_answers
                    else [q["options"][1 if allow_revote else 0]]
                )
            ],
        }
        for q in questions
    ]
    assert all(q["voter_count"] == 1 for q in state["questions"])
