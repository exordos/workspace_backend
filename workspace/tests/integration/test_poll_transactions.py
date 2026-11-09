# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Exercise poll ownership, physical deletion, and PostgreSQL lock ordering."""

import concurrent.futures
import datetime
import threading
import time
import uuid

import psycopg
import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import exceptions
from workspace.messenger_api.api import poll_store
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native


def _wait_for_blocked_session(db, session):
    deadline = time.monotonic() + 5
    while not db.execute(
        "SELECT pg_backend_pid()=ANY(pg_blocking_pids(%s))",
        (session.info.backend_pid,),
    ).fetchone()[0]:
        assert time.monotonic() < deadline
        time.sleep(0.005)


@pytest.fixture
def poll(api, db, poll_creation):
    stream = api.post(native.STREAMS, json={"name": "Poll transactions"}).json()
    question = str(uuid.uuid4())
    options = [str(uuid.uuid4()), str(uuid.uuid4())]
    payload = {
        "kind": "poll",
        "is_anonymous": True,
        "results_visibility": "always",
        "allow_revote": True,
        "allow_adding_options": True,
        "questions": [
            {
                "uuid": question,
                "text": "Choose",
                "multiple_answers": True,
                "options": [{"uuid": value, "text": value} for value in options],
            }
        ],
    }
    res = api.post(
        native.MESSAGES, json={"stream_uuid": stream["uuid"], "payload": payload}
    )
    assert res.status_code == 201, res.text
    placement = res.json()["uuid"]
    root = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (api.project_id, placement),
    ).fetchone()[0]
    return placement, root, question, options


def test_concurrent_poll_selections_preserve_content_and_queue_each_change_once(
    api, db, poll
):
    placement, root, question, options = poll
    project = api.project_id
    before = db.execute(
        "SELECT content_uuid,content_revision,updated_at,created_at FROM messenger_messages WHERE uuid=%s",
        (root,),
    ).fetchone()
    ready = threading.Barrier(2)

    def select(option):
        with psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as session:
            ready.wait(timeout=5)
            poll_store.mutate(
                session,
                project,
                api.user_uuid,
                placement,
                root,
                "vote",
                {"question_uuid": question, "option_uuid": option, "selected": True},
            )

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        list(pool.map(select, options))
    assert db.execute(
        "SELECT poll_state_revision,content_uuid,content_revision,updated_at,created_at FROM messenger_messages WHERE uuid=%s",
        (root,),
    ).fetchone() == (3, *before)
    assert set(
        db.execute(
            "SELECT option_uuid::text FROM messenger_poll_votes WHERE project_id=%s AND message_uuid=%s",
            (project, root),
        ).fetchall()
    ) == {(option,) for option in options}
    assert db.execute(
        """SELECT count(*) FROM messenger_projection_tasks WHERE project_id=%s
           AND payload->>'canonical_message_uuid'=%s AND payload->>'poll_changed'='true'""",
        (project, str(root)),
    ).fetchone() == (2,)


def test_poll_account_cascade_bumps_once_and_message_delete_has_no_poll_update(
    api, db, poll
):
    _, root, question, options = poll
    user = uuid.uuid4()
    conftest.seed_workspace_user(db, user, str(user))
    native._register_project_user(db, api.project_id, user)
    db.execute(
        """INSERT INTO messenger_poll_votes(project_id,message_uuid,user_uuid,question_uuid,option_uuid)
           SELECT %s,%s,%s,%s,unnest(%s::uuid[])""",
        (api.project_id, root, user, question, options),
    )
    db.execute(
        "DELETE FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
        (api.project_id, user),
    )
    assert db.execute(
        "SELECT poll_state_revision FROM messenger_messages WHERE uuid=%s", (root,)
    ).fetchone() == (3,)
    assert not db.execute(
        "SELECT 1 FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchall()
    db.execute(
        "INSERT INTO messenger_poll_votes VALUES (%s,%s,%s,%s,%s)",
        (api.project_id, root, api.user_uuid, question, options[0]),
    )
    db.execute("SELECT messenger_delete_message(%s,%s)", (api.project_id, root))
    assert not db.execute(
        "SELECT 1 FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchall()
    assert db.execute(
        """SELECT count(*) FROM messenger_projection_tasks WHERE project_id=%s
           AND payload->>'canonical_message_uuid'=%s AND payload->>'poll_changed'='true'""",
        (api.project_id, str(root)),
    ).fetchone() == (3,)


def test_vote_uses_time_after_waiting_for_poll_lock(api, db, poll):
    placement, root, question, options = poll
    started, release = threading.Event(), threading.Event()

    def vote():
        with psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as session:
            # Hold open a real transaction whose timestamp precedes the deadline.
            session.execute("SELECT now()")
            started.set()
            assert release.wait(5)
            poll_store.mutate(
                session,
                api.project_id,
                api.user_uuid,
                placement,
                root,
                "vote",
                {
                    "question_uuid": question,
                    "option_uuid": options[0],
                    "selected": True,
                },
            )

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(vote)
        assert started.wait(5)
        with db.transaction():
            db.execute(
                "SELECT 1 FROM messenger_messages WHERE uuid=%s FOR UPDATE", (root,)
            )
            deadline = db.execute("SELECT clock_timestamp()").fetchone()[0]
            db.execute(
                """UPDATE messenger_messages m
                   SET content_uuid=messenger_resolve_message_content(
                       jsonb_set(c.workspace_content,'{closes_at}',to_jsonb(%s::text)),NULL)
                   FROM messenger_message_contents c WHERE c.uuid=m.content_uuid AND m.uuid=%s""",
                (deadline.astimezone(datetime.timezone.utc).isoformat(), root),
            )
            release.set()
        with pytest.raises(exceptions.PollClosedError):
            future.result(timeout=5)
    assert not db.execute(
        "SELECT 1 FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchall()


def test_delete_poll_while_account_cleanup_is_waiting_for_it(api, db, poll):
    _, root, question, options = poll
    project, user = api.project_id, uuid.uuid4()
    conftest.seed_workspace_user(db, user, str(user))
    native._register_project_user(db, project, user)
    db.execute(
        "INSERT INTO messenger_poll_votes VALUES (%s,%s,%s,%s,%s)",
        (project, root, user, question, options[0]),
    )

    with (
        psycopg.connect(conftest.TEST_DB_URL) as cleanup,
        concurrent.futures.ThreadPoolExecutor(1) as pool,
    ):
        with db.transaction():
            db.execute(
                "SELECT 1 FROM messenger_messages WHERE project_id=%s AND uuid=%s FOR UPDATE",
                (project, root),
            )
            deleting = pool.submit(
                cleanup.execute,
                "DELETE FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
                (project, user),
            )
            # Wait for the account trigger to reach the locked poll. If it
            # locked votes first, the simultaneous root cascade would deadlock.
            _wait_for_blocked_session(db, cleanup)
            db.execute("SELECT messenger_delete_message(%s,%s)", (project, root))
        deleting.result(timeout=5)
    assert not db.execute(
        "SELECT 1 FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchall()


def test_poll_appends_settings_and_close_serialize_with_concurrent_clicks(
    api, db, poll
):
    placement, root, question, options = poll

    def race(*actions):
        ready = threading.Barrier(len(actions))

        def apply(item):
            action, values = item
            with psycopg.connect(
                conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
            ) as session:
                ready.wait(timeout=5)
                try:
                    poll_store.mutate(
                        session,
                        api.project_id,
                        api.user_uuid,
                        placement,
                        root,
                        action,
                        values,
                    )
                except exceptions.PollConflictError:
                    return False
                return True

        with concurrent.futures.ThreadPoolExecutor(len(actions)) as pool:
            return list(pool.map(apply, actions))

    additions = [
        (
            "add_poll_option",
            {
                "question_uuid": question,
                "option": {"uuid": str(uuid.uuid4()), "text": str(i)},
            },
        )
        for i in range(3)
    ]
    assert race(*additions[:2]) == [True, True]
    disabled, appended = race(
        (
            "update_settings",
            {"settings": {"closes_at": None, "allow_adding_options": False}},
        ),
        additions[2],
    )
    assert disabled
    closed, voted = race(
        ("close_poll", {}),
        (
            "vote",
            {"question_uuid": question, "option_uuid": options[0], "selected": True},
        ),
    )
    assert closed
    payload, revision = db.execute(
        """SELECT c.workspace_content,m.poll_state_revision FROM messenger_messages m
           JOIN messenger_message_contents c ON c.uuid=m.content_uuid WHERE m.uuid=%s""",
        (root,),
    ).fetchone()
    assert not payload["allow_adding_options"] and payload["closed_at"]
    assert {o["uuid"] for o in payload["questions"][0]["options"]} == {
        *options,
        *(a[1]["option"]["uuid"] for a in additions[: 2 + int(appended)]),
    }
    assert revision == 5 + int(appended) + int(voted)
    assert db.execute(
        "SELECT count(*) FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchone()[0] == int(voted)


@pytest.mark.parametrize(
    ("parent", "action"),
    [
        ("messenger_topics", "vote"),
        ("messenger_streams", "close_poll"),
        ("messenger_messages", "vote"),
    ],
)
def test_poll_action_during_physical_delete(api, db, poll, parent, action):
    placement, root, question, options = poll
    project = api.project_id
    stream, topic = db.execute(
        "SELECT stream_uuid,topic_uuid FROM messenger_message_placements WHERE uuid=%s",
        (placement,),
    ).fetchone()
    identifier = {
        "messenger_topics": topic,
        "messenger_streams": stream,
        "messenger_messages": root,
    }[parent]
    values = (
        {"question_uuid": question, "option_uuid": options[0], "selected": True}
        if action == "vote"
        else {}
    )
    with (
        psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as voter,
        concurrent.futures.ThreadPoolExecutor(1) as pool,
    ):
        with db.transaction():
            db.execute("SET LOCAL statement_timeout='4s'")
            # Pause deletion after it has reached the placement but before it
            # removes the now orphaned canonical message.
            db.execute(
                f"SELECT 1 FROM {parent} WHERE project_id=%s AND uuid=%s FOR UPDATE",
                (project, identifier),
            )
            db.execute(
                "SELECT 1 FROM messenger_message_placements WHERE uuid=%s FOR UPDATE",
                (placement,),
            )
            vote = pool.submit(
                poll_store.mutate,
                voter,
                project,
                api.user_uuid,
                placement,
                root,
                action,
                values,
            )
            _wait_for_blocked_session(db, voter)
            db.execute(
                f"DELETE FROM {parent} WHERE project_id=%s AND uuid=%s",
                (project, identifier),
            )
        with pytest.raises(ra_exc.ResourceNotFoundError):
            vote.result(timeout=5)
    assert (
        db.execute("SELECT 1 FROM messenger_messages WHERE uuid=%s", (root,)).fetchone()
        is None
    )


def test_moved_poll_action_does_not_use_the_previous_scope(api, db, poll):
    placement, root, question, options = poll
    destination = api.post(native.STREAMS, json={"name": "New poll scope"}).json()
    vote = {"question_uuid": question, "option_uuid": options[0], "selected": True}
    with (
        psycopg.connect(
            conftest.TEST_DB_URL, row_factory=psycopg.rows.dict_row
        ) as voter,
        concurrent.futures.ThreadPoolExecutor(1) as pool,
    ):
        with db.transaction():
            db.execute("SET LOCAL statement_timeout='4s'")
            db.execute(
                "UPDATE messenger_message_placements SET stream_uuid=%s,topic_uuid=%s WHERE project_id=%s AND uuid=%s",
                (
                    destination["uuid"],
                    destination["default_topic_uuid"],
                    api.project_id,
                    placement,
                ),
            )
            voting = pool.submit(
                poll_store.mutate,
                voter,
                api.project_id,
                api.user_uuid,
                placement,
                root,
                "vote",
                vote,
            )
            _wait_for_blocked_session(db, voter)
            db.execute(
                "UPDATE messenger_messages SET updated_at=clock_timestamp() WHERE uuid=%s",
                (root,),
            )
        with pytest.raises(ra_exc.ResourceNotFoundError):
            voting.result(timeout=5)
    assert not db.execute(
        "SELECT 1 FROM messenger_poll_votes WHERE message_uuid=%s", (root,)
    ).fetchall()
    response = api.post(f"{native.MESSAGES}{placement}/actions/vote/invoke", json=vote)
    assert response.status_code == 200, response.text
    assert response.json()["stream_uuid"] == destination["uuid"]
