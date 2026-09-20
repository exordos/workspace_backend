# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import threading
import time
import uuid as sys_uuid

import psycopg

from workspace.tests.integration import conftest
from workspace.workspace_v3 import projections


def _insert_user(cursor, user_uuid):
    cursor.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status, avatar
        ) VALUES (
            %s, NOW(), NOW(), %s, 'iam', 'active',
            'urn:gravatar:00000000000000000000000000000000'
        )
        """,
        (user_uuid, f"user-{user_uuid}"),
    )


def _seed_conversation(
    db,
    *,
    user_count=3,
    stream_mode="all_messages",
    clear_tasks=True,
):
    project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    users = [sys_uuid.uuid4() for _ in range(user_count)]
    with db.cursor() as cursor:
        for user_uuid in users:
            _insert_user(cursor, user_uuid)
        cursor.execute(
            """
            INSERT INTO workspace_v3.streams (
                uuid, project_id, name, owner_uuid
            ) VALUES (%s, %s, 'Projection test', %s)
            """,
            (stream_uuid, project_id, users[0]),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.topics (
                uuid, project_id, stream_uuid, name
            ) VALUES (%s, %s, %s, 'General')
            """,
            (topic_uuid, project_id, stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid,
                role, notification_mode
            )
            SELECT gen_random_uuid(), %s, %s, input.user_uuid, %s,
                   CASE WHEN input.user_uuid = %s THEN 'owner' ELSE 'member' END,
                   %s
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (
                project_id,
                stream_uuid,
                users[0],
                users[0],
                stream_mode,
                users,
            ),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.topic_bindings (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid
            )
            SELECT gen_random_uuid(), %s, %s, %s, input.user_uuid
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (project_id, stream_uuid, topic_uuid, users),
        )
        if clear_tasks:
            cursor.execute(
                "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
                (project_id,),
            )
    return project_id, stream_uuid, topic_uuid, users


def _insert_message(db, project_id, stream_uuid, topic_uuid, author_uuid):
    message_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.messages (
                uuid, project_id, stream_uuid, topic_uuid, author_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"hello"}'::jsonb
            )
            """,
            (message_uuid, project_id, stream_uuid, topic_uuid, author_uuid),
        )
    return message_uuid


def _process(db, worker_id="integration:v3", **kwargs):
    with db.transaction():
        tasks = projections.claim_projection_tasks(
            db,
            worker_id,
            batch_size=1000,
        )
    with db.transaction():
        return projections.process_claimed_projection_tasks(
            db,
            worker_id,
            tasks,
            **kwargs,
        )


def test_reaction_projection_is_bounded_complete_and_provider_visible(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=4,
    )
    message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            )
            SELECT %s, %s, %s, input.user_uuid,
                   input.user_uuid = %s
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (project_id, stream_uuid, message_uuid, users[0], users),
        )
    db.execute("TRUNCATE workspace_v3.projection_tasks")
    provider_uuid = sys_uuid.uuid4()
    before = datetime.datetime.now(datetime.timezone.utc)
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workspace_v3.streams
            SET source_name = 'zulip'
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.provider_consumers (
                uuid, project_id, name, iam_user_uuid
            ) VALUES (%s, %s, 'zulip', %s)
            """,
            (provider_uuid, project_id, sys_uuid.uuid4()),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                project_id, message_uuid, user_uuid, emoji_name
            ) VALUES
                (%s, %s, %s, 'eyes'),
                (%s, %s, %s, 'eyes'),
                (%s, %s, %s, 'heart')
            """,
            (
                project_id,
                message_uuid,
                users[0],
                project_id,
                message_uuid,
                users[1],
                project_id,
                message_uuid,
                users[2],
            ),
        )

    metrics = _process(db, reaction_user_limit=2)

    assert metrics["completed"] == 1
    assert metrics["operations"] == 3
    assert metrics["projections"] == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT reactions, reaction_users, updated_at
            FROM workspace_v3.messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, message_uuid),
        )
        reactions, reaction_users, updated_at = cursor.fetchone()
        assert reactions == {"eyes": 2, "heart": 1}
        assert set(reaction_users["eyes"]) == {str(users[0]), str(users[1])}
        assert reaction_users["heart"] == [str(users[2])]
        assert updated_at.replace(tzinfo=datetime.timezone.utc) >= before
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events AS event
            JOIN workspace_v3.event_audience_members AS member
              ON member.project_id = event.project_id
             AND member.audience_snapshot_uuid = event.audience_snapshot_uuid
            WHERE event.project_id = %s
              AND member.consumer_type = 'provider'
              AND member.consumer_uuid = %s
            """,
            (project_id, provider_uuid),
        )
        assert cursor.fetchone()[0] == 4

        cursor.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                project_id, message_uuid, user_uuid, emoji_name
            ) VALUES (%s, %s, %s, 'eyes')
            """,
            (project_id, message_uuid, users[3]),
        )

    metrics = _process(db, reaction_user_limit=2)
    assert metrics["completed"] == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT reactions, reaction_users
            FROM workspace_v3.messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, message_uuid),
        )
        reactions, reaction_users = cursor.fetchone()
        assert reactions == {"eyes": 3, "heart": 1}
        assert "eyes" not in reaction_users
        assert reaction_users == {"heart": [str(users[2])]}

        cursor.execute(
            "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
            (project_id,),
        )
        event_count = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                project_id, message_uuid, user_uuid, emoji_name
            ) VALUES (%s, %s, %s, 'temporary')
            RETURNING uuid
            """,
            (project_id, message_uuid, users[0]),
        )
        temporary_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            DELETE FROM workspace_v3.message_reactions
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, temporary_uuid),
        )

    metrics = _process(db, reaction_user_limit=2)
    assert metrics["completed"] == 2
    assert metrics["events"] == 2
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
            (project_id,),
        )
        assert cursor.fetchone()[0] == event_count + 2


def test_reaction_projection_excludes_users_without_message_visibility(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=3,
    )
    message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            )
            SELECT %s, %s, %s, input.user_uuid, false
            FROM unnest(%s::uuid[]) AS input(user_uuid)
            """,
            (project_id, stream_uuid, message_uuid, users[:2]),
        )
        cursor.execute(
            "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
            (project_id,),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                project_id, message_uuid, user_uuid, emoji_name
            ) VALUES (%s, %s, %s, 'eyes')
            """,
            (project_id, message_uuid, users[0]),
        )

    metrics = _process(db)

    assert metrics["completed"] == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT event.payload ->> 'kind', member.consumer_uuid
            FROM workspace_v3.events AS event
            JOIN workspace_v3.event_audience_members AS member
              ON member.project_id = event.project_id
             AND member.audience_snapshot_uuid = event.audience_snapshot_uuid
            WHERE event.project_id = %s
              AND event.payload ->> 'kind' IN (
                  'message.updated', 'message_reaction.created'
              )
              AND member.consumer_type = 'user'
            """,
            (project_id,),
        )
        audiences = {(kind, user_uuid) for kind, user_uuid in cursor.fetchall()}
    assert audiences == {
        (kind, user_uuid)
        for kind in ("message.updated", "message_reaction.created")
        for user_uuid in users[:2]
    }


def test_message_flags_project_independent_unread_counters(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=2,
        stream_mode="mentions_only",
    )
    message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    db.execute("TRUNCATE workspace_v3.projection_tasks")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            ) VALUES
                (%s, %s, %s, %s, true),
                (%s, %s, %s, %s, false)
            """,
            (
                project_id,
                stream_uuid,
                message_uuid,
                users[0],
                project_id,
                stream_uuid,
                message_uuid,
                users[1],
            ),
        )

    metrics = _process(db)
    assert metrics["completed"] == 4
    assert metrics["projections"] == 4
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, unread_count, active_unread_count,
                   passive_unread_count, last_message_uuid
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (project_id, stream_uuid),
        )
        stream_counts = {row[0]: row[1:] for row in cursor.fetchall()}
        assert stream_counts[users[0]] == (0, 0, 0, message_uuid)
        assert stream_counts[users[1]] == (1, 0, 1, message_uuid)

        cursor.execute(
            """
            UPDATE workspace_v3.message_flags
            SET mentioned = true, updated_at = clock_timestamp()
            WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
            """,
            (project_id, message_uuid, users[1]),
        )

    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT unread_count, active_unread_count, passive_unread_count
            FROM workspace_v3.topic_bindings
            WHERE project_id = %s AND topic_uuid = %s AND user_uuid = %s
            """,
            (project_id, topic_uuid, users[1]),
        )
        assert cursor.fetchone() == (1, 1, 0)
        cursor.execute(
            """
            UPDATE workspace_v3.message_flags
            SET read = true, starred = true, updated_at = clock_timestamp()
            WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
            """,
            (project_id, message_uuid, users[1]),
        )

    metrics = _process(db)
    assert metrics["completed"] == 2
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT stream.unread_count, stream.active_unread_count,
                   stream.passive_unread_count,
                   topic.unread_count, topic.active_unread_count,
                   topic.passive_unread_count
            FROM workspace_v3.stream_bindings AS stream
            JOIN workspace_v3.topic_bindings AS topic
              ON topic.project_id = stream.project_id
             AND topic.user_uuid = stream.user_uuid
            WHERE stream.project_id = %s
              AND stream.stream_uuid = %s
              AND topic.topic_uuid = %s
              AND stream.user_uuid = %s
            """,
            (project_id, stream_uuid, topic_uuid, users[1]),
        )
        assert cursor.fetchone() == (0, 0, 0, 0, 0, 0)
        cursor.execute(
            """
            SELECT user_uuid, read, starred
            FROM workspace_v3.message_flags
            WHERE project_id = %s AND message_uuid = %s
            """,
            (project_id, message_uuid),
        )
        states = {row[0]: row[1:] for row in cursor.fetchall()}
        assert states == {
            users[0]: (True, False),
            users[1]: (True, True),
        }
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events
            WHERE project_id = %s AND object_type = 'message'
              AND action = 'read'
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == 1


def test_message_delete_reprojects_unread_and_last_message(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=2,
    )
    first_message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    last_message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            )
            SELECT %s, %s, message_uuid, %s, false
            FROM unnest(%s::uuid[]) AS input(message_uuid)
            """,
            (
                project_id,
                stream_uuid,
                users[1],
                [first_message_uuid, last_message_uuid],
            ),
        )
    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM workspace_v3.messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, last_message_uuid),
        )
        cursor.execute(
            """
            SELECT scope_type, count(*)
            FROM workspace_v3.projection_tasks
            WHERE project_id = %s AND task_type = 'read_counters'
              AND user_uuid = %s AND status = 'pending'
            GROUP BY scope_type
            """,
            (project_id, users[1]),
        )
        assert dict(cursor.fetchall()) == {"user_stream": 1, "user_topic": 1}

    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT stream.unread_count, stream.last_message_uuid,
                   topic.unread_count, topic.last_message_uuid
            FROM workspace_v3.stream_bindings AS stream
            JOIN workspace_v3.topic_bindings AS topic
              ON topic.project_id = stream.project_id
             AND topic.stream_uuid = stream.stream_uuid
             AND topic.user_uuid = stream.user_uuid
            WHERE stream.project_id = %s AND stream.stream_uuid = %s
              AND stream.user_uuid = %s AND topic.topic_uuid = %s
            """,
            (project_id, stream_uuid, users[1], topic_uuid),
        )
        assert cursor.fetchone() == (
            1,
            first_message_uuid,
            1,
            first_message_uuid,
        )


def test_projection_failure_is_retried_without_losing_task(_database, db):
    project_id, _stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=1,
    )
    task_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.projection_tasks (
                uuid, project_id, task_type, scope_type, scope_uuid,
                user_uuid
            ) VALUES (%s, %s, 'folder_counters', 'user_stream', %s, %s)
            """,
            (task_uuid, project_id, sys_uuid.uuid4(), users[0]),
        )

    metrics = _process(db, worker_id="integration:v3:failure")

    assert metrics["failed"] == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, lease_owner, lease_expires_at,
                   next_retry_at, last_error
            FROM workspace_v3.projection_tasks
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, task_uuid),
        )
        status, attempts, owner, expires_at, retry_at, last_error = cursor.fetchone()
        assert status == "failed"
        assert attempts == 1
        assert owner is None
        assert expires_at is None
        assert retry_at is not None
        assert last_error == "ValueError"


def test_projection_batch_isolates_a_bad_task(_database, db):
    project_id, stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=1,
    )
    valid_uuid = sys_uuid.uuid4()
    invalid_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.projection_tasks (
                uuid, project_id, task_type, scope_type, scope_uuid,
                user_uuid
            ) VALUES
                (%s, %s, 'read_counters', 'user_stream', %s, %s),
                (%s, %s, 'folder_counters', 'user_stream', %s, %s)
            """,
            (
                valid_uuid,
                project_id,
                stream_uuid,
                users[0],
                invalid_uuid,
                project_id,
                stream_uuid,
                users[0],
            ),
        )

    metrics = _process(db, worker_id="integration:v3:isolation")

    assert metrics["completed"] == 1
    assert metrics["failed"] == 1
    rows = db.execute(
        """
        SELECT uuid, status, attempts, last_error
        FROM workspace_v3.projection_tasks
        WHERE uuid = ANY(%s::uuid[])
        """,
        ([valid_uuid, invalid_uuid],),
    ).fetchall()
    states = {row[0]: row[1:] for row in rows}
    assert states[valid_uuid] == ("completed", 1, None)
    assert states[invalid_uuid] == ("failed", 1, "ValueError")


def test_projection_workers_skip_locked_tasks_without_duplicates(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=1,
    )
    message_uuids = [
        _insert_message(
            db,
            project_id,
            stream_uuid,
            topic_uuid,
            users[0],
        )
        for _index in range(20)
    ]
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            )
            SELECT %s, %s, input.message_uuid, %s, false
            FROM unnest(%s::uuid[]) AS input(message_uuid)
            """,
            (project_id, stream_uuid, users[0], message_uuids),
        )
        cursor.execute(
            "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
            (project_id,),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                project_id, message_uuid, user_uuid, emoji_name
            )
            SELECT %s, input.message_uuid, %s, 'eyes'
            FROM unnest(%s::uuid[]) AS input(message_uuid)
            """,
            (project_id, users[0], message_uuids),
        )

    def run_worker(worker_number):
        connection = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
        try:
            worker_id = f"integration:v3:concurrent:{worker_number}"
            with connection.transaction():
                tasks = projections.claim_projection_tasks(
                    connection,
                    worker_id,
                    batch_size=10,
                )
            with connection.transaction():
                return projections.process_claimed_projection_tasks(
                    connection,
                    worker_id,
                    tasks,
                )
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        metrics = list(executor.map(run_worker, range(2)))

    assert sum(item["completed"] for item in metrics) == 20
    assert sum(item["failed"] for item in metrics) == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE status = 'completed')
            FROM workspace_v3.projection_tasks
            WHERE project_id = %s
            """,
            (project_id,),
        )
        assert cursor.fetchone() == (20, 20)
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.messages
            WHERE project_id = %s AND reactions = '{"eyes": 1}'::jsonb
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == 20
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events
            WHERE project_id = %s AND object_type = 'message_reaction'
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == 20


def test_folder_projection_creates_stable_automatic_membership(_database, db):
    project_id, stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=3,
        clear_tasks=False,
    )
    provider_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.event_cursors (
                project_id, consumer_type, consumer_uuid
            ) VALUES (%s, 'provider', %s)
            """,
            (project_id, provider_uuid),
        )

    metrics = _process(db)

    assert metrics["failed"] == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, kind, title
            FROM workspace_v3.folders
            WHERE project_id = %s
            ORDER BY user_uuid, kind
            """,
            (project_id,),
        )
        folders = cursor.fetchall()
        assert len(folders) == 9
        assert {row[1:] for row in folders} == {
            ("all_chats", "All chats"),
            ("direct", "Personal"),
            ("streams", "Channels"),
        }
        cursor.execute(
            """
            SELECT user_uuid, folder_uuid, uuid, chat_type, automatic
            FROM workspace_v3.folder_items
            WHERE project_id = %s
            ORDER BY user_uuid, folder_uuid
            """,
            (project_id,),
        )
        items = cursor.fetchall()
        assert len(items) == 6
        expected_item_uuids = {
            projections._folder_item_uuid("00", stream_uuid),
            projections._folder_item_uuid("22", stream_uuid),
        }
        assert {row[2] for row in items} == expected_item_uuids
        assert {row[3] for row in items} == {"stream"}
        assert all(row[4] for row in items)
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events AS event
            JOIN workspace_v3.event_audience_members AS member
              ON member.project_id = event.project_id
             AND member.audience_snapshot_uuid = event.audience_snapshot_uuid
            WHERE event.project_id = %s
              AND event.object_type = 'folder'
              AND member.consumer_type = 'provider'
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == 0


def test_folder_projection_reclassifies_and_removes_archived_stream(_database, db):
    project_id, stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=2,
        clear_tasks=False,
    )
    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE workspace_v3.streams
            SET private = true, updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, stream_uuid),
        )

    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, folder_uuid, uuid, chat_type
            FROM workspace_v3.folder_items
            WHERE project_id = %s
            ORDER BY user_uuid, folder_uuid
            """,
            (project_id,),
        )
        items = cursor.fetchall()
        assert len(items) == 4
        assert {row[1] for row in items} == {
            projections.ALL_CHATS_FOLDER_UUID,
            projections.DIRECT_FOLDER_UUID,
        }
        assert {row[2] for row in items} == {
            projections._folder_item_uuid("00", stream_uuid),
            projections._folder_item_uuid("11", stream_uuid),
        }
        assert {row[3] for row in items} == {"private"}
        cursor.execute(
            """
            UPDATE workspace_v3.streams
            SET is_archived = true, updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, stream_uuid),
        )

    _process(db)
    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM workspace_v3.folder_items WHERE project_id = %s",
            (project_id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events
            WHERE project_id = %s
              AND object_type = 'folder_item'
              AND action = 'deleted'
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == len(users) * 3


def test_folder_counters_follow_binding_projection(_database, db):
    project_id, stream_uuid, topic_uuid, users = _seed_conversation(
        db,
        user_count=2,
        clear_tasks=False,
    )
    _process(db)
    message_uuid = _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        users[0],
    )
    custom_folder_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.folders (
                uuid, project_id, user_uuid, kind, title
            ) VALUES (%s, %s, %s, 'custom', 'Important')
            """,
            (custom_folder_uuid, project_id, users[1]),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                chat_type
            ) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'stream')
            """,
            (project_id, users[1], custom_folder_uuid, stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid, read
            ) VALUES (%s, %s, %s, %s, false)
            """,
            (project_id, stream_uuid, message_uuid, users[1]),
        )

    _process(db)
    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT kind, unread_count, active_unread_count,
                   passive_unread_count
            FROM workspace_v3.folders
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY kind
            """,
            (project_id, users[1]),
        )
        assert cursor.fetchall() == [
            ("all_chats", 1, 1, 0),
            ("custom", 1, 1, 0),
            ("direct", 0, 0, 0),
            ("streams", 1, 1, 0),
        ]


def test_projection_claim_commits_before_processing_locks(_database, db):
    project_id, _stream_uuid, _topic_uuid, _users = _seed_conversation(
        db,
        user_count=1,
        clear_tasks=False,
    )
    worker_id = "integration:v3:short-claim"
    with db.transaction():
        tasks = projections.claim_projection_tasks(
            db,
            worker_id,
            batch_size=1,
        )
    assert len(tasks) == 1

    observer = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    try:
        with observer.transaction():
            row = observer.execute(
                """
                SELECT uuid
                FROM workspace_v3.projection_tasks
                WHERE project_id = %s AND uuid = %s
                FOR UPDATE NOWAIT
                """,
                (project_id, tasks[0]["uuid"]),
            ).fetchone()
            assert row == (tasks[0]["uuid"],)
    finally:
        observer.close()

    with db.transaction():
        metrics = projections.process_claimed_projection_tasks(
            db,
            worker_id,
            tasks,
        )
    assert metrics["completed"] == 1


def test_projection_processing_fences_expired_lease_reclaims(
    _database,
    db,
    monkeypatch,
):
    project_id, stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=1,
    )
    task_uuid = sys_uuid.uuid4()
    db.execute("DELETE FROM workspace_v3.projection_tasks")
    db.execute(
        """
        INSERT INTO workspace_v3.projection_tasks (
            uuid, project_id, task_type, scope_type, scope_uuid, user_uuid
        ) VALUES (%s, %s, 'read_counters', 'user_stream', %s, %s)
        """,
        (task_uuid, project_id, stream_uuid, users[0]),
    )
    worker_id = "integration:v3:lease-owner"
    with db.transaction():
        tasks = projections.claim_projection_tasks(
            db,
            worker_id,
            batch_size=1,
            lease_seconds=1,
        )
    assert [task["uuid"] for task in tasks] == [task_uuid]

    processing_started = threading.Event()
    allow_processing = threading.Event()
    original_update = projections._update_stream_counters

    def delayed_update(*args, **kwargs):
        processing_started.set()
        assert allow_processing.wait(timeout=5)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(projections, "_update_stream_counters", delayed_update)

    def process_original_claim():
        connection = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
        try:
            with connection.transaction():
                return projections.process_claimed_projection_tasks(
                    connection,
                    worker_id,
                    tasks,
                )
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(process_original_claim)
        assert processing_started.wait(timeout=5)
        time.sleep(1.1)
        observer = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
        try:
            with observer.transaction():
                reclaimed = projections.claim_projection_tasks(
                    observer,
                    "integration:v3:lease-reclaimer",
                    batch_size=1,
                )
            assert reclaimed == []
        finally:
            observer.close()
            allow_processing.set()
        metrics = future.result(timeout=5)

    assert metrics["completed"] == 1
    assert db.execute(
        """
        SELECT status, attempts, lease_owner
        FROM workspace_v3.projection_tasks
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, task_uuid),
    ).fetchone() == ("completed", 1, None)


def test_concurrent_folder_workers_converge_for_one_user(_database, db):
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuids = [sys_uuid.uuid4() for _index in range(24)]
    with db.cursor() as cursor:
        _insert_user(cursor, user_uuid)
        cursor.execute(
            """
            INSERT INTO workspace_v3.streams (
                uuid, project_id, name, owner_uuid
            )
            SELECT input.uuid, %s, 'Concurrent folders', %s
            FROM unnest(%s::uuid[]) AS input(uuid)
            """,
            (project_id, user_uuid, stream_uuids),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, role
            )
            SELECT gen_random_uuid(), %s, input.uuid, %s, %s, 'owner'
            FROM unnest(%s::uuid[]) AS input(uuid)
            """,
            (project_id, user_uuid, user_uuid, stream_uuids),
        )
        cursor.execute(
            """
            DELETE FROM workspace_v3.projection_tasks
            WHERE project_id = %s AND task_type = 'read_counters'
            """,
            (project_id,),
        )

    def drain(worker_number):
        connection = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
        worker_id = f"integration:v3:folders:{worker_number}"
        completed = 0.0
        try:
            while True:
                with connection.transaction():
                    tasks = projections.claim_projection_tasks(
                        connection,
                        worker_id,
                        batch_size=5,
                    )
                if not tasks:
                    return completed
                with connection.transaction():
                    metrics = projections.process_claimed_projection_tasks(
                        connection,
                        worker_id,
                        tasks,
                    )
                assert metrics["failed"] == 0
                completed += metrics["completed"]
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        completed = sum(executor.map(drain, range(4)))

    assert completed >= len(stream_uuids)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM workspace_v3.folders
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM workspace_v3.folder_items
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM workspace_v3.projection_tasks
                 WHERE project_id = %s AND status <> 'completed')
            """,
            (project_id, user_uuid, project_id, user_uuid, project_id),
        )
        assert cursor.fetchone() == (3, len(stream_uuids) * 2, 0)


def test_unbinding_stream_cascades_all_folder_items(_database, db):
    project_id, stream_uuid, _topic_uuid, users = _seed_conversation(
        db,
        user_count=1,
        clear_tasks=False,
    )
    _process(db)
    _process(db)
    custom_folder_uuid = sys_uuid.uuid4()
    custom_item_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO workspace_v3.folders (
                uuid, project_id, user_uuid, kind, title
            ) VALUES (%s, %s, %s, 'custom', 'Custom')
            """,
            (custom_folder_uuid, project_id, users[0]),
        )
        cursor.execute(
            """
            INSERT INTO workspace_v3.folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                chat_type
            ) VALUES (%s, %s, %s, %s, %s, 'stream')
            """,
            (
                custom_item_uuid,
                project_id,
                users[0],
                custom_folder_uuid,
                stream_uuid,
            ),
        )
        cursor.execute(
            """
            DELETE FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_id, stream_uuid, users[0]),
        )
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.folder_items
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_id, users[0]),
        )
        assert cursor.fetchone()[0] == 0

    _process(db)
    _process(db)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM workspace_v3.events
            WHERE project_id = %s
              AND object_type = 'folder_item'
              AND action = 'deleted'
            """,
            (project_id,),
        )
        assert cursor.fetchone()[0] == 3
