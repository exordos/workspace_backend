# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import conftest


MIGRATION_FILE = (
    "0193-Backfill-legacy-native-messenger-data-into-Workspace-v3-2ee199.py"
)


def test_native_backfill_keeps_user_visible_resources(_database, db):
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db,
            project_uuid,
            user_uuid,
            "Legacy native stream",
        )
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db,
            project_uuid,
            stream_uuid,
            user_uuid,
            "Legacy topic",
            is_default=True,
        )
    )
    message_uuid = sys_uuid.uuid4()
    folder_uuid = sys_uuid.uuid4()
    folder_binding_uuid = sys_uuid.uuid4()
    folder_item_uuid = sys_uuid.uuid4()
    draft_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    content = f"hello [member](urn:user:{user_uuid})"
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                jsonb_build_object('kind', 'markdown', 'content', %s::text)
            )
            """,
            (
                message_uuid,
                project_uuid,
                stream_uuid,
                topic_uuid,
                user_uuid,
                content,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred
            ) VALUES (%s, %s, %s, FALSE, TRUE, FALSE)
            """,
            (message_uuid, user_uuid, project_uuid),
        )
        cursor.execute(
            """
            INSERT INTO messenger_folders (
                uuid, project_id, title, background_color_value, system_type
            ) VALUES (%s, %s, 'Legacy custom', 123, NULL)
            """,
            (folder_uuid, project_uuid),
        )
        cursor.execute(
            """
            INSERT INTO messenger_user_folder_bindings (
                uuid, project_id, user_uuid, folder_uuid, rule
            ) VALUES (%s, %s, %s, %s, 'custom')
            """,
            (folder_binding_uuid, project_uuid, user_uuid, folder_uuid),
        )
        cursor.execute(
            """
            INSERT INTO messenger_folder_items (
                uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                order_index, chat_type, automatic
            ) VALUES (%s, %s, %s, %s, %s, 7, 'stream', FALSE)
            """,
            (
                folder_item_uuid,
                project_uuid,
                user_uuid,
                folder_uuid,
                stream_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_drafts (
                uuid, project_id, user_uuid, stream_uuid, topic_uuid,
                payload, revision
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"draft"}'::jsonb, 3
            )
            """,
            (draft_uuid, project_uuid, user_uuid, stream_uuid, topic_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_files (
                uuid, project_id, name, user_uuid, stream_uuid,
                content_type, size_bytes, hash, storage_type,
                storage_object_id, acl_mode
            ) VALUES (
                %s, %s, 'legacy.txt', %s, %s,
                'text/plain', 6, 'sha256:test', 'file', %s, 'stream'
            )
            """,
            (
                file_uuid,
                project_uuid,
                user_uuid,
                stream_uuid,
                f"{str(file_uuid)[:2]}/{file_uuid}",
            ),
        )

    engine = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )
    engine._load_migrations()[MIGRATION_FILE].upgrade(db)

    assert db.execute(
        """
        SELECT mentioned FROM workspace_v3.message_flags
        WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
        """,
        (project_uuid, message_uuid, user_uuid),
    ).fetchone()[0]
    assert db.execute(
        """
        SELECT kind, title, background_color_value
        FROM workspace_v3.folders
        WHERE project_id = %s AND user_uuid = %s AND uuid = %s
        """,
        (project_uuid, user_uuid, folder_uuid),
    ).fetchone() == ("custom", "Legacy custom", 123)
    assert db.execute(
        """
        SELECT order_index, automatic FROM workspace_v3.folder_items
        WHERE project_id = %s AND user_uuid = %s AND uuid = %s
        """,
        (project_uuid, user_uuid, folder_item_uuid),
    ).fetchone() == (7, False)
    assert db.execute(
        """
        SELECT revision FROM workspace_v3.drafts
        WHERE project_id = %s AND user_uuid = %s AND uuid = %s
        """,
        (project_uuid, user_uuid, draft_uuid),
    ).fetchone()[0] == 3
    assert db.execute(
        """
        SELECT name, storage_object_id FROM workspace_v3.files
        WHERE project_id = %s AND uuid = %s
        """,
        (project_uuid, file_uuid),
    ).fetchone() == ("legacy.txt", f"{str(file_uuid)[:2]}/{file_uuid}")


def test_native_backfill_materializes_compact_read_state_without_legacy_flag(
    _database, db
):
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, user_uuid, "Compact stream")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db,
            project_uuid,
            stream_uuid,
            user_uuid,
            "Compact topic",
            is_default=True,
        )
    )
    message_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"compact"}'::jsonb
            ) RETURNING ingest_sequence
            """,
            (message_uuid, project_uuid, stream_uuid, topic_uuid, user_uuid),
        )
        ingest_sequence = cursor.fetchone()[0]
        cursor.execute(
            """
            UPDATE m_workspace_read_state_projects_v1
            SET mode = 'compact' WHERE project_id = %s
            """,
            (project_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits
            ) VALUES (
                %s, %s, set_bit(B'0'::bit(4096), %s, 1)
            )
            """,
            (user_uuid, ingest_sequence // 4096, ingest_sequence % 4096),
        )

    engine = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )
    engine._load_migrations()[MIGRATION_FILE].upgrade(db)

    assert db.execute(
        """
        SELECT read, pinned, starred
        FROM workspace_v3.message_flags
        WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
        """,
        (project_uuid, message_uuid, user_uuid),
    ).fetchone() == (True, False, False)
