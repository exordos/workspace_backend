#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import uuid as sys_uuid

import psycopg
import pytest


EXPECTED_COLUMNS = {
    "drafts": (
        "uuid",
        "project_id",
        "user_uuid",
        "stream_uuid",
        "topic_uuid",
        "payload",
        "revision",
        "created_at",
        "updated_at",
    ),
    "event_audience_members": (
        "project_id",
        "audience_snapshot_uuid",
        "consumer_type",
        "consumer_uuid",
        "created_at",
    ),
    "event_audience_snapshots": (
        "uuid",
        "project_id",
        "membership_digest",
        "current_epoch_version",
        "pruned_through_epoch_version",
        "created_at",
        "updated_at",
    ),
    "event_cursors": (
        "project_id",
        "consumer_type",
        "consumer_uuid",
        "epoch_generation",
        "current_epoch_version",
        "pruned_through_epoch_version",
        "created_at",
        "updated_at",
    ),
    "event_recipient_payloads": (
        "project_id",
        "event_uuid",
        "consumer_type",
        "consumer_uuid",
        "payload",
        "created_at",
    ),
    "events": (
        "epoch_version",
        "uuid",
        "project_id",
        "entity_uuid",
        "audience_snapshot_uuid",
        "schema_version",
        "object_type",
        "action",
        "payload",
        "created_at",
        "updated_at",
        "origin_consumer_type",
        "origin_consumer_uuid",
    ),
    "folder_items": (
        "uuid",
        "project_id",
        "user_uuid",
        "folder_uuid",
        "stream_uuid",
        "order_index",
        "pinned_at",
        "chat_type",
        "automatic",
        "created_at",
        "updated_at",
    ),
    "files": (
        "uuid",
        "project_id",
        "user_uuid",
        "stream_uuid",
        "acl_mode",
        "name",
        "description",
        "content_type",
        "size_bytes",
        "hash",
        "storage_type",
        "storage_id",
        "storage_object_id",
        "created_at",
        "updated_at",
    ),
    "folders": (
        "uuid",
        "project_id",
        "user_uuid",
        "kind",
        "title",
        "background_color_value",
        "unread_count",
        "active_unread_count",
        "passive_unread_count",
        "created_at",
        "updated_at",
    ),
    "message_flags": (
        "uuid",
        "project_id",
        "stream_uuid",
        "message_uuid",
        "user_uuid",
        "read",
        "pinned",
        "starred",
        "mentioned",
        "created_at",
        "updated_at",
    ),
    "message_reactions": (
        "uuid",
        "project_id",
        "message_uuid",
        "user_uuid",
        "emoji_name",
        "source_name",
        "created_at",
        "updated_at",
    ),
    "messages": (
        "uuid",
        "project_id",
        "stream_uuid",
        "topic_uuid",
        "author_uuid",
        "payload",
        "source_name",
        "reactions",
        "reaction_users",
        "created_at",
        "updated_at",
    ),
    "projection_tasks": (
        "uuid",
        "project_id",
        "task_type",
        "scope_type",
        "scope_uuid",
        "user_uuid",
        "payload",
        "status",
        "lease_owner",
        "lease_expires_at",
        "attempts",
        "next_retry_at",
        "last_error",
        "created_at",
        "updated_at",
    ),
    "provider_consumers": (
        "uuid",
        "project_id",
        "name",
        "iam_user_uuid",
        "enabled",
        "created_at",
        "updated_at",
    ),
    "provider_entity_states": (
        "project_id",
        "provider_uuid",
        "entity_type",
        "entity_uuid",
        "content_hash",
        "created_at",
        "updated_at",
        "source_updated_at",
        "source_content_hash",
    ),
    "stream_bindings": (
        "uuid",
        "project_id",
        "stream_uuid",
        "user_uuid",
        "who_uuid",
        "role",
        "notification_mode",
        "notification_updated_at",
        "unread_count",
        "active_unread_count",
        "passive_unread_count",
        "last_message_uuid",
        "created_at",
        "updated_at",
    ),
    "streams": (
        "uuid",
        "project_id",
        "name",
        "description",
        "owner_uuid",
        "source_name",
        "invite_only",
        "announce",
        "direct_user_uuid",
        "private",
        "is_archived",
        "private_index",
        "color",
        "default_topic_uuid",
        "created_at",
        "updated_at",
        "history_public_to_subscribers",
    ),
    "topic_bindings": (
        "uuid",
        "project_id",
        "stream_uuid",
        "topic_uuid",
        "user_uuid",
        "notification_mode",
        "notification_updated_at",
        "unread_count",
        "active_unread_count",
        "passive_unread_count",
        "last_message_uuid",
        "summary_has_new_messages",
        "created_at",
        "updated_at",
    ),
    "topics": (
        "uuid",
        "project_id",
        "stream_uuid",
        "name",
        "color",
        "source_name",
        "summary",
        "summary_last_message_uuid",
        "summary_enabled",
        "summary_system_prompt",
        "summary_reasoning_effort",
        "is_done",
        "version",
        "created_at",
        "updated_at",
    ),
    "users": (
        "uuid",
        "created_at",
        "updated_at",
        "username",
        "source",
        "status",
        "first_name",
        "last_name",
        "email",
        "last_ping_at",
        "status_emoji",
        "status_text",
        "avatar",
        "disabled",
        "is_bot",
    ),
}


def _insert_user(conn, user_uuid, username, source="iam", email=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspace_v3.users (
                uuid, created_at, updated_at, username, source, status,
                email, avatar
            )
            VALUES (
                %s, NOW(), NOW(), %s, %s, 'active', %s,
                'urn:gravatar:00000000000000000000000000000000'
            )
            """,
            (user_uuid, username, source, email),
        )


def _insert_stream(conn, project_id, stream_uuid, owner_uuid, **values):
    columns = ["project_id", "uuid", "name", "owner_uuid"]
    parameters = [project_id, stream_uuid, "Schema test", owner_uuid]
    for name, value in values.items():
        columns.append(name)
        parameters.append(value)
    placeholders = ", ".join(["%s"] * len(columns))
    identifiers = ", ".join(columns)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO workspace_v3.streams ({identifiers}) VALUES ({placeholders})",
            parameters,
        )


def _insert_topic(conn, project_id, stream_uuid, topic_uuid):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspace_v3.topics (
                project_id, uuid, stream_uuid, name
            )
            VALUES (%s, %s, %s, 'General')
            """,
            (project_id, topic_uuid, stream_uuid),
        )


def _insert_message(
    conn,
    project_id,
    stream_uuid,
    topic_uuid,
    message_uuid,
    author_uuid,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspace_v3.messages (
                project_id, uuid, stream_uuid, topic_uuid, author_uuid,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, '{"content": "hello"}'::jsonb)
            """,
            (
                project_id,
                message_uuid,
                stream_uuid,
                topic_uuid,
                author_uuid,
            ),
        )


def test_workspace_v3_contains_only_approved_tables_and_columns(_database, db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'workspace_v3'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        tables = tuple(row[0] for row in cur.fetchall())

        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'workspace_v3'
            ORDER BY table_name, ordinal_position
            """
        )
        actual_columns = {table: [] for table in tables}
        for table, column in cur.fetchall():
            actual_columns[table].append(column)

    assert tables == tuple(sorted(EXPECTED_COLUMNS))
    assert {
        table: tuple(columns) for table, columns in actual_columns.items()
    } == EXPECTED_COLUMNS
    assert "project_id" not in actual_columns["users"]
    assert all(
        "project_id" in actual_columns[table]
        for table in EXPECTED_COLUMNS
        if table != "users"
    )


def test_workspace_entity_description_columns_allow_ten_thousand_characters(
    _database,
    db,
):
    expected_columns = {
        ("public", "catalog_services"),
        ("public", "workspace_streams"),
        ("public", "m_workspace_streams"),
        ("public", "m_workspace_files"),
        ("public", "messenger_streams"),
        ("workspace_v3", "streams"),
        ("workspace_v3", "files"),
    }
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, table_name, character_maximum_length
            FROM information_schema.columns
            WHERE column_name = 'description'
              AND (table_schema, table_name) IN (
                    ('public', 'catalog_services'),
                    ('public', 'workspace_streams'),
                    ('public', 'm_workspace_streams'),
                    ('public', 'm_workspace_files'),
                    ('public', 'messenger_streams'),
                    ('workspace_v3', 'streams'),
                    ('workspace_v3', 'files')
              )
            """
        )
        description_columns = {
            (schema_name, table_name): maximum_length
            for schema_name, table_name, maximum_length in cur.fetchall()
        }

    assert set(description_columns) == expected_columns
    assert set(description_columns.values()) == {10_000}


def test_workspace_v3_enforces_project_graph_and_physical_cascade(_database, db):
    user_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    project_id = sys_uuid.uuid4()
    other_project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    _insert_user(db, user_uuid, f"user-{user_uuid}")
    _insert_user(db, peer_uuid, f"user-{peer_uuid}")
    _insert_stream(db, project_id, stream_uuid, user_uuid)
    _insert_topic(db, project_id, stream_uuid, topic_uuid)
    _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        message_uuid,
        user_uuid,
    )

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE workspace_v3.streams
            SET default_topic_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (topic_uuid, project_id, stream_uuid),
        )
        cur.execute(
            """
            UPDATE workspace_v3.topics
            SET summary_last_message_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (message_uuid, project_id, topic_uuid),
        )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_topic(db, other_project_id, stream_uuid, sys_uuid.uuid4())

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_stream(
            db,
            sys_uuid.uuid4(),
            sys_uuid.uuid4(),
            user_uuid,
            direct_user_uuid=peer_uuid,
        )

    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM workspace_v3.streams WHERE project_id = %s AND uuid = %s",
            (project_id, stream_uuid),
        )
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM workspace_v3.topics
                 WHERE project_id = %s),
                (SELECT count(*) FROM workspace_v3.messages
                 WHERE project_id = %s)
            """,
            (project_id, project_id),
        )
        assert cur.fetchone() == (0, 0)


def test_workspace_v3_uses_global_stable_ids_and_restricts_authors(_database, db):
    author_uuid = sys_uuid.uuid4()
    project_id = sys_uuid.uuid4()
    other_project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    other_stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    other_topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    _insert_user(
        db,
        author_uuid,
        f"user-{author_uuid}",
        email="shared@example.test",
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_user(
            db,
            sys_uuid.uuid4(),
            f"user-{sys_uuid.uuid4()}",
            email="shared@example.test",
        )

    _insert_user(
        db,
        sys_uuid.uuid4(),
        f"zulip-{sys_uuid.uuid4()}",
        source="zulip",
        email="shared@example.test",
    )
    _insert_stream(db, project_id, stream_uuid, author_uuid)
    _insert_topic(db, project_id, stream_uuid, topic_uuid)
    _insert_message(
        db,
        project_id,
        stream_uuid,
        topic_uuid,
        message_uuid,
        author_uuid,
    )
    _insert_stream(db, other_project_id, other_stream_uuid, author_uuid)
    _insert_topic(db, other_project_id, other_stream_uuid, other_topic_uuid)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_message(
            db,
            other_project_id,
            other_stream_uuid,
            other_topic_uuid,
            message_uuid,
            author_uuid,
        )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM workspace_v3.users WHERE uuid = %s",
                (author_uuid,),
            )


def test_workspace_v3_private_streams_have_at_most_two_members(_database, db):
    project_id = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    users = [sys_uuid.uuid4() for _index in range(3)]
    for user_uuid in users:
        _insert_user(db, user_uuid, f"user-{user_uuid}")
    _insert_stream(
        db,
        project_id,
        stream_uuid,
        users[0],
        private=True,
    )

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workspace_v3.stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid
            ) VALUES
                (gen_random_uuid(), %s, %s, %s, %s),
                (gen_random_uuid(), %s, %s, %s, %s)
            """,
            (
                project_id,
                stream_uuid,
                users[0],
                users[0],
                project_id,
                stream_uuid,
                users[1],
                users[0],
            ),
        )

    with pytest.raises(psycopg.errors.CheckViolation):
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workspace_v3.stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s)
                """,
                (project_id, stream_uuid, users[2], users[0]),
            )

    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM workspace_v3.projection_tasks WHERE project_id = %s",
            (project_id,),
        )


def test_workspace_v3_indexes_user_reactions_for_user_listing(_database, db):
    index = db.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'workspace_v3'
          AND indexname = 'message_reactions_user_idx'
        """
    ).fetchone()

    assert index is not None
    assert index[0].endswith(
        "ON workspace_v3.message_reactions USING btree (project_id, user_uuid)"
    )


def test_workspace_v3_indexes_provider_user_project_discovery(_database, db):
    index = db.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = 'workspace_v3'
          AND indexname = 'provider_entity_states_entity_projects_idx'
        """
    ).fetchone()

    assert index is not None
    assert index[0].endswith(
        "ON workspace_v3.provider_entity_states USING btree "
        "(entity_type, entity_uuid, project_id)"
    )
