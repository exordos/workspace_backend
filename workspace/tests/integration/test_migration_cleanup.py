# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations as ra_migrations
import psycopg
import pytest

from workspace.tests.integration import conftest


LEGACY_MIGRATION_UUIDS = (
    "e8e1b2c3-3739-4238-97cf-fa7613109917",
    "4b6e8031-28dd-4cb5-9bf6-37d75bb2da45",
    "c990ade7-933d-4cb9-bef5-3dbccecd2dff",
    "c73e3516-9240-4c3e-803a-97deedad2721",
    "bd462528-6582-49df-aa39-ef9108196127",
)
CLEANUP_MIGRATION_UUID = "eec69a95-cabb-49c5-89a1-8078732f27c2"
CLEANUP_MIGRATION_FILE = "0113-remove-legacy-Messenger-mail-storage-eec69a.py"
EMAIL_INDEX_MIGRATION_UUID = "1dbd2c19-1e0c-4d6c-8928-ee64ca5e2382"
EMAIL_INDEX_MIGRATION_FILE = "0114-scope-Messenger-email-uniqueness-to-IAM-1dbd2c.py"
ZULIP_IDENTITY_MIGRATION_UUID = "39cb26af-4a18-4e87-befd-e5e540271137"
ZULIP_IDENTITY_MIGRATION_FILE = "0115-link-Zulip-provider-identities-39cb26.py"
IDENTITY_RECONCILIATION_INDEX_MIGRATION_UUID = "72f59f53-0bc7-4cda-ae81-79d22c3fee2f"
IDENTITY_RECONCILIATION_INDEX_MIGRATION_FILE = (
    "0116-index-Messenger-event-identity-reconciliation-72f59f.py"
)
EXTERNAL_ACCOUNT_SCOPE_MIGRATION_UUID = "4a927983-57be-43d1-979e-cef820b86b2d"
EXTERNAL_ACCOUNT_SCOPE_MIGRATION_FILE = (
    "0117-scope-external-projections-by-account-4a9279.py"
)
PUSH_DEVICE_MIGRATION_UUID = "5c4ae023-56c1-442c-b45a-8068c0c2fa68"
PUSH_DEVICE_MIGRATION_FILE = "0118-add-push-devices-5c4ae0.py"
DIRECTORY_VIEW_MIGRATION_UUID = "52ef9640-de45-456d-807e-4bb972bfcb33"
DIRECTORY_VIEW_MIGRATION_FILE = (
    "0119-add-canonical-Workspace-user-directory-view-52ef96.py"
)
RETENTION_MIGRATION_UUID = "ae5fdfd7-8767-45f7-8471-8448b5900782"
RETENTION_MIGRATION_FILE = "0120-index-bounded-retention-cleanup-ae5fdf.py"
MEMBER_PROJECTION_ACCESS_MIGRATION_UUID = "35e3d356-9fe8-4dd4-b6db-6c9da527d891"
MEMBER_PROJECTION_ACCESS_MIGRATION_FILE = (
    "0121-grant-external-projection-access-to-members-35e3d3.py"
)
REVOKED_STREAM_ACCESS_MIGRATION_UUID = "640b9d0e-f465-4359-abb4-47fdd60b5c40"
REVOKED_STREAM_ACCESS_MIGRATION_FILE = (
    "0122-revoke-external-projection-access-on-stream-removal-640b9d.py"
)
EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID = "aadb67c9-c716-4066-9867-b82079c1c283"
EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_FILE = (
    "0123-deduplicate-and-revoke-external-chat-memberships-aadb67.py"
)
EXTERNAL_ACCOUNT_ACCESS_DEDUP_MIGRATION_UUID = "78c745a8-08a2-4432-a511-9e0875cc35db"
EXTERNAL_ACCOUNT_ACCESS_DEDUP_MIGRATION_FILE = (
    "0124-deduplicate-external-account-access-78c745.py"
)
EXTERNAL_STREAM_ACCESS_MIGRATION_UUID = "e82c027f-2481-4447-85fb-8648b335a6cd"
EXTERNAL_STREAM_ACCESS_MIGRATION_FILE = (
    "0125-scope-external-visibility-to-canonical-streams-e82c02.py"
)
TOPIC_READ_BOUNDARY_MIGRATION_UUID = "20ae2266-265f-488d-a306-f299160a1b25"
TOPIC_READ_BOUNDARY_MIGRATION_FILE = "0126-index-topic-read-boundaries-20ae22.py"
REACTION_USER_SNAPSHOT_MIGRATION_UUID = "547d747d-c9f1-4583-80d9-b932c1a5df2a"
REACTION_USER_SNAPSHOT_MIGRATION_FILE = (
    "0127-persist-bounded-reaction-user-snapshots-547d74.py"
)
TOPIC_SUMMARY_MIGRATION_UUID = "f3cbd414-4eba-4db1-8f1d-fc3c7eeb7f96"
TOPIC_SUMMARY_MIGRATION_FILE = (
    "0129-add-topic-summary-worker-and-LLM-endpoint-registry-22b3a6.py"
)
UNREAD_COUNTERS_MIGRATION_UUID = "36e14b04-23c3-412c-bc87-34a7ccc79d0e"
UNREAD_COUNTERS_MIGRATION_FILE = (
    "0130-split-active-and-passive-unread-counters-36e14b.py"
)
LEGACY_TABLES = (
    "m_messenger_writer_gate_acks_v1",
    "m_messenger_writer_gate_expected_v1",
    "m_messenger_writer_instances_v1",
    "m_messenger_writer_gate_releases_v1",
    "m_messenger_writer_gates_v1",
    "m_messenger_import_quarantine_v1",
    "m_messenger_import_checkpoints_v1",
    "m_messenger_import_items_v1",
    "m_messenger_import_runs_v1",
)


def test_current_migrations_have_a_single_head(_database, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))

    assert engine.get_latest_migration() == UNREAD_COUNTERS_MIGRATION_FILE
    with db.cursor() as cur:
        cur.execute(
            'SELECT uuid, applied FROM "ra_migrations" WHERE uuid = ANY(%s::text[])',
            (
                [
                    CLEANUP_MIGRATION_UUID,
                    EMAIL_INDEX_MIGRATION_UUID,
                    ZULIP_IDENTITY_MIGRATION_UUID,
                    IDENTITY_RECONCILIATION_INDEX_MIGRATION_UUID,
                    EXTERNAL_ACCOUNT_SCOPE_MIGRATION_UUID,
                    PUSH_DEVICE_MIGRATION_UUID,
                    DIRECTORY_VIEW_MIGRATION_UUID,
                    RETENTION_MIGRATION_UUID,
                    MEMBER_PROJECTION_ACCESS_MIGRATION_UUID,
                    REVOKED_STREAM_ACCESS_MIGRATION_UUID,
                    EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID,
                    EXTERNAL_ACCOUNT_ACCESS_DEDUP_MIGRATION_UUID,
                    EXTERNAL_STREAM_ACCESS_MIGRATION_UUID,
                    TOPIC_READ_BOUNDARY_MIGRATION_UUID,
                    REACTION_USER_SNAPSHOT_MIGRATION_UUID,
                    TOPIC_SUMMARY_MIGRATION_UUID,
                    UNREAD_COUNTERS_MIGRATION_UUID,
                ],
            ),
        )
        assert set(cur.fetchall()) == {
            (CLEANUP_MIGRATION_UUID, True),
            (EMAIL_INDEX_MIGRATION_UUID, True),
            (ZULIP_IDENTITY_MIGRATION_UUID, True),
            (IDENTITY_RECONCILIATION_INDEX_MIGRATION_UUID, True),
            (EXTERNAL_ACCOUNT_SCOPE_MIGRATION_UUID, True),
            (PUSH_DEVICE_MIGRATION_UUID, True),
            (DIRECTORY_VIEW_MIGRATION_UUID, True),
            (RETENTION_MIGRATION_UUID, True),
            (MEMBER_PROJECTION_ACCESS_MIGRATION_UUID, True),
            (REVOKED_STREAM_ACCESS_MIGRATION_UUID, True),
            (EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID, True),
            (EXTERNAL_ACCOUNT_ACCESS_DEDUP_MIGRATION_UUID, True),
            (EXTERNAL_STREAM_ACCESS_MIGRATION_UUID, True),
            (TOPIC_READ_BOUNDARY_MIGRATION_UUID, True),
            (REACTION_USER_SNAPSHOT_MIGRATION_UUID, True),
            (TOPIC_SUMMARY_MIGRATION_UUID, True),
            (UNREAD_COUNTERS_MIGRATION_UUID, True),
        }
        cur.execute("SELECT to_regclass('m_workspace_events_user_identity_idx')")
        assert cur.fetchone()[0] == "m_workspace_events_user_identity_idx"
        cur.execute(
            "SELECT to_regclass("
            "'m_workspace_message_reactions_message_uuid_idx'"
            ")"
        )
        assert (
            cur.fetchone()[0]
            == "m_workspace_message_reactions_message_uuid_idx"
        )
        cur.execute(
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_messages'
              AND column_name = 'reaction_users'
            """
        )
        data_type, is_nullable, column_default = cur.fetchone()
        assert data_type == "jsonb"
        assert is_nullable == "NO"
        assert "'{}'::jsonb" in column_default
        cur.execute(
            """
            SELECT reaction_users
            FROM m_workspace_user_messages_view
            LIMIT 0
            """
        )
        cur.execute("SELECT to_regclass('m_workspace_directory_users_v1')")
        assert cur.fetchone()[0] == "m_workspace_directory_users_v1"
        cur.execute(
            "SELECT to_regclass('m_external_bridge_heartbeats_v1_retention_idx')"
        )
        assert cur.fetchone()[0] == "m_external_bridge_heartbeats_v1_retention_idx"
        cur.execute("SELECT to_regclass('m_workspace_messages_topic_boundary_idx')")
        assert cur.fetchone()[0] == "m_workspace_messages_topic_boundary_idx"
        cur.execute(
            "SELECT to_regclass('m_workspace_unread_flags_user_message_idx')"
        )
        assert cur.fetchone()[0] == "m_workspace_unread_flags_user_message_idx"
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indisvalid
            FROM pg_index
            WHERE indexrelid IN (
                to_regclass('m_workspace_messages_topic_boundary_idx'),
                to_regclass('m_workspace_unread_flags_user_message_idx')
            )
            """
        )
        assert set(cur.fetchall()) == {
            ("m_workspace_messages_topic_boundary_idx", True),
            ("m_workspace_unread_flags_user_message_idx", True),
        }
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_stream_topics'
              AND column_name IN (
                  'summary',
                  'summary_last_message_uuid',
                  'summary_system_prompt',
                  'summary_enabled',
                  'summary_reasoning_effort'
              )
            """
        )
        assert {row[0] for row in cur.fetchall()} == {
            "summary",
            "summary_last_message_uuid",
            "summary_system_prompt",
            "summary_enabled",
            "summary_reasoning_effort",
        }
        cur.execute("SELECT to_regclass('m_workspace_topic_summary_journal')")
        assert cur.fetchone()[0] == "m_workspace_topic_summary_journal"
        cur.execute(
            "SELECT to_regclass('m_workspace_topic_summary_journal_restore_idx')"
        )
        assert cur.fetchone()[0] == "m_workspace_topic_summary_journal_restore_idx"
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_llm_endpoints'
              AND column_name = 'revision'
            """
        )
        assert cur.fetchone()[0] == 0


def test_external_projection_access_is_scoped_to_account(
    _database,
    db,
):
    owner_a_uuid = "10000000-0000-4000-8000-000000000101"
    owner_b_uuid = "10000000-0000-4000-8000-000000000102"
    project_uuid = "10000000-0000-4000-8000-000000000103"
    account_a_uuid = "10000000-0000-4000-8000-000000000104"
    account_b_uuid = "10000000-0000-4000-8000-000000000105"
    chat_a_uuid = "10000000-0000-4000-8000-000000000106"
    chat_b_uuid = "10000000-0000-4000-8000-000000000107"
    unbound_user_uuid = "10000000-0000-4000-8000-000000000108"
    conftest.seed_workspace_user(db, owner_a_uuid, "projection-owner-a")
    conftest.seed_workspace_user(db, owner_b_uuid, "projection-owner-b")
    conftest.seed_workspace_user(db, unbound_user_uuid, "projection-unbound")
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_a_uuid,
        "Account A provider projection",
    )
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status
            ) VALUES (
                %s, %s, 'zulip',
                '{"server_url":"https://zulip.example.test"}'::jsonb,
                TRUE, 'live'
            )
            """,
            (
                (account_a_uuid, owner_a_uuid),
                (account_b_uuid, owner_b_uuid),
            ),
        )
        cur.execute(
            """
            UPDATE m_workspace_streams
            SET external_account_uuid = %s,
                provider_external_id = 'channel:7',
                source_name = 'zulip',
                source = %s::jsonb
            WHERE project_id = %s AND uuid = %s
            """,
            (
                account_a_uuid,
                (
                    '{"kind":"zulip","stream_id":7,'
                    '"server_url":"https://zulip.example.test"}'
                ),
                project_uuid,
                stream_uuid,
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', %s,
                '{"participants":[]}'::jsonb, %s, TRUE, %s, %s
            )
            """,
            (
                (
                    chat_a_uuid,
                    account_a_uuid,
                    owner_a_uuid,
                    "channel:7",
                    "Account A provider projection",
                    project_uuid,
                    stream_uuid,
                ),
                (
                    chat_b_uuid,
                    account_b_uuid,
                    owner_b_uuid,
                    "channel:8",
                    "Account B provider projection",
                    project_uuid,
                    None,
                ),
            ),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                created_at, updated_at
            ) VALUES (gen_random_uuid(), %s, %s, %s, %s, 'member', NOW(), NOW())
            """,
            (project_uuid, stream_uuid, owner_b_uuid, owner_a_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version,
                pruned_through_epoch_version
            ) VALUES (%s, %s, 17, 4)
            ON CONFLICT (project_id, user_uuid) DO UPDATE
            SET current_epoch_version = 17,
                pruned_through_epoch_version = 4
            RETURNING epoch_generation
            """,
            (project_uuid, owner_b_uuid),
        )
        generation_before = cur.fetchone()[0]
        cur.execute(
            'DELETE FROM "ra_migrations" WHERE uuid = %s',
            (EXTERNAL_ACCOUNT_SCOPE_MIGRATION_UUID,),
        )
        cur.execute(
            'DELETE FROM "ra_migrations" WHERE uuid = %s',
            (MEMBER_PROJECTION_ACCESS_MIGRATION_UUID,),
        )

    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.apply_migration(EXTERNAL_ACCOUNT_SCOPE_MIGRATION_FILE)
    engine.apply_migration(MEMBER_PROJECTION_ACCESS_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, owner_b_uuid),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_streams
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, owner_b_uuid),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_streams
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, unbound_user_uuid),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT source->>'source_scope'
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, stream_uuid),
        )
        assert cur.fetchone()[0] == account_a_uuid
        cur.execute(
            """
            SELECT source_scope
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY source_scope
            """,
            (project_uuid, owner_b_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == [
            account_a_uuid,
            account_b_uuid,
        ]
        cur.execute(
            """
            SELECT epoch_generation, current_epoch_version,
                   pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_uuid, owner_b_uuid),
        )
        generation_after, current_epoch, pruned_through = cur.fetchone()
        assert generation_after != generation_before
        assert current_epoch == 17
        assert pruned_through == 17


def test_external_account_access_deduplicates_multiple_selected_chats(
    _database,
    db,
):
    owner_uuid = "20000000-0000-4000-8000-000000000101"
    project_uuid = "20000000-0000-4000-8000-000000000102"
    account_uuid = "20000000-0000-4000-8000-000000000103"
    provider_realm_uuid = "20000000-0000-4000-8000-000000000104"
    first_stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "First selected provider chat",
    )
    second_stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "Second selected provider chat",
    )

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (
                %s, %s, 'zulip',
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
                TRUE, 'live', TRUE
            )
            """,
            (account_uuid, owner_uuid),
        )
        cur.executemany(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', %s,
                jsonb_build_object(
                    'provider_realm_uuid', CAST(%s AS text)
                ),
                %s, TRUE, %s, %s
            )
            """,
            (
                (
                    "20000000-0000-4000-8000-000000000105",
                    account_uuid,
                    owner_uuid,
                    "channel:1",
                    provider_realm_uuid,
                    "First selected provider chat",
                    project_uuid,
                    first_stream_uuid,
                ),
                (
                    "20000000-0000-4000-8000-000000000106",
                    account_uuid,
                    owner_uuid,
                    "channel:2",
                    provider_realm_uuid,
                    "Second selected provider chat",
                    project_uuid,
                    second_stream_uuid,
                ),
            ),
        )
        cur.executemany(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = jsonb_build_object(
                    'kind', 'zulip',
                    'server_url', 'https://zulip.example.test',
                    'source_scope', CAST(%s AS text)
                ),
                external_account_uuid = %s,
                provider_external_id = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (
                (
                    account_uuid,
                    account_uuid,
                    "channel:1",
                    project_uuid,
                    first_stream_uuid,
                ),
                (
                    account_uuid,
                    account_uuid,
                    "channel:2",
                    project_uuid,
                    second_stream_uuid,
                ),
            ),
        )
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_uuid, owner_uuid),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT uuid::text, COUNT(*)
            FROM m_workspace_user_streams
            WHERE project_id = %s
              AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            GROUP BY uuid
            ORDER BY uuid
            """,
            (
                project_uuid,
                owner_uuid,
                [first_stream_uuid, second_stream_uuid],
            ),
        )
        assert cur.fetchall() == sorted(
            [
                (first_stream_uuid, 1),
                (second_stream_uuid, 1),
            ]
        )


def test_external_visibility_uses_the_canonical_stream_per_logical_chat(
    _database,
    db,
):
    owner_a_uuid = "30000000-0000-4000-8000-000000000101"
    owner_b_uuid = "30000000-0000-4000-8000-000000000102"
    project_uuid = "30000000-0000-4000-8000-000000000103"
    account_a_uuid = "30000000-0000-4000-8000-000000000104"
    account_b_uuid = "30000000-0000-4000-8000-000000000105"
    provider_realm_uuid = "30000000-0000-4000-8000-000000000106"
    stream_a_x_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_a_uuid,
        "Account A chat X",
    )
    stream_b_x_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_b_uuid,
        "Account B chat X",
    )
    stream_b_y_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_b_uuid,
        "Account B chat Y",
    )
    conftest.seed_user_stream_binding(
        db,
        project_uuid,
        stream_b_x_uuid,
        owner_a_uuid,
    )
    conftest.seed_user_stream_binding(
        db,
        project_uuid,
        stream_b_y_uuid,
        owner_a_uuid,
    )
    topic_a_x_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_a_x_uuid,
        owner_a_uuid,
        "Topic A/X",
    )
    topic_b_x_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_b_x_uuid,
        owner_a_uuid,
        "Topic B/X",
    )
    topic_b_y_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_b_y_uuid,
        owner_a_uuid,
        "Topic B/Y",
    )
    message_uuids = (
        "30000000-0000-4000-8000-000000000201",
        "30000000-0000-4000-8000-000000000202",
        "30000000-0000-4000-8000-000000000203",
    )
    event_uuids = (
        "30000000-0000-4000-8000-000000000301",
        "30000000-0000-4000-8000-000000000302",
        "30000000-0000-4000-8000-000000000303",
    )
    reaction_event_uuids = (
        "30000000-0000-4000-8000-000000000401",
        "30000000-0000-4000-8000-000000000402",
        "30000000-0000-4000-8000-000000000403",
    )

    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (
                %s, %s, 'zulip',
                jsonb_build_object(
                    'kind', 'zulip',
                    'server_url', 'https://zulip.example.test'
                ),
                TRUE, 'live', TRUE
            )
            """,
            (
                (account_a_uuid, owner_a_uuid),
                (account_b_uuid, owner_b_uuid),
            ),
        )
        cur.executemany(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = jsonb_build_object(
                    'kind', 'zulip',
                    'server_url', 'https://zulip.example.test',
                    'source_scope', CAST(%s AS text)
                ),
                external_account_uuid = %s,
                provider_external_id = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (
                (
                    account_a_uuid,
                    account_a_uuid,
                    "channel:x",
                    project_uuid,
                    stream_a_x_uuid,
                ),
                (
                    account_b_uuid,
                    account_b_uuid,
                    "channel:x",
                    project_uuid,
                    stream_b_x_uuid,
                ),
                (
                    account_b_uuid,
                    account_b_uuid,
                    "channel:y",
                    project_uuid,
                    stream_b_y_uuid,
                ),
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', %s,
                jsonb_build_object(
                    'provider_realm_uuid', CAST(%s AS text)
                ),
                %s, TRUE, %s, %s
            )
            """,
            (
                (
                    "30000000-0000-4000-8000-000000000111",
                    account_a_uuid,
                    owner_a_uuid,
                    "channel:x",
                    provider_realm_uuid,
                    "Account A chat X",
                    project_uuid,
                    stream_a_x_uuid,
                ),
                (
                    "30000000-0000-4000-8000-000000000112",
                    account_b_uuid,
                    owner_b_uuid,
                    "channel:x",
                    provider_realm_uuid,
                    "Account B chat X",
                    project_uuid,
                    stream_b_x_uuid,
                ),
                (
                    "30000000-0000-4000-8000-000000000113",
                    account_b_uuid,
                    owner_b_uuid,
                    "channel:y",
                    provider_realm_uuid,
                    "Account B chat Y",
                    project_uuid,
                    stream_b_y_uuid,
                ),
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"historical"}',
                'native', '{"kind":"native"}'
            )
            """,
            (
                (
                    message_uuids[0],
                    project_uuid,
                    stream_a_x_uuid,
                    topic_a_x_uuid,
                    owner_a_uuid,
                ),
                (
                    message_uuids[1],
                    project_uuid,
                    stream_b_x_uuid,
                    topic_b_x_uuid,
                    owner_b_uuid,
                ),
                (
                    message_uuids[2],
                    project_uuid,
                    stream_b_y_uuid,
                    topic_b_y_uuid,
                    owner_b_uuid,
                ),
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred
            ) VALUES (%s, %s, %s, FALSE, FALSE, FALSE)
            """,
            (
                (message_uuid, owner_a_uuid, project_uuid)
                for message_uuid in message_uuids
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_events (
                uuid, project_id, user_uuid, schema_version,
                object_type, action, payload
            ) VALUES (
                %s, %s, %s, 1, 'message', 'created',
                jsonb_build_object(
                    'kind', 'message.created',
                    'uuid', CAST(%s AS text),
                    'stream_uuid', CAST(%s AS text),
                    'source_name', 'native'
                )
            )
            """,
            (
                (
                    event_uuids[0],
                    project_uuid,
                    owner_a_uuid,
                    message_uuids[0],
                    stream_a_x_uuid,
                ),
                (
                    event_uuids[1],
                    project_uuid,
                    owner_a_uuid,
                    message_uuids[1],
                    stream_b_x_uuid,
                ),
                (
                    event_uuids[2],
                    project_uuid,
                    owner_a_uuid,
                    message_uuids[2],
                    stream_b_y_uuid,
                ),
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_events (
                uuid, project_id, user_uuid, schema_version,
                object_type, action, payload
            ) VALUES (
                %s, %s, %s, 1, 'message_reaction', 'created',
                jsonb_build_object(
                    'kind', 'message_reaction.created',
                    'uuid', CAST(%s AS text),
                    'message_uuid', CAST(%s AS text),
                    'source_name', 'native'
                )
            )
            """,
            (
                (
                    reaction_event_uuids[0],
                    project_uuid,
                    owner_a_uuid,
                    reaction_event_uuids[0],
                    message_uuids[0],
                ),
                (
                    reaction_event_uuids[1],
                    project_uuid,
                    owner_a_uuid,
                    reaction_event_uuids[1],
                    message_uuids[1],
                ),
                (
                    reaction_event_uuids[2],
                    project_uuid,
                    owner_a_uuid,
                    reaction_event_uuids[2],
                    message_uuids[2],
                ),
            ),
        )

        cur.execute(
            """
            SELECT source_scope
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY source_scope
            """,
            (project_uuid, owner_a_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == [
            account_a_uuid,
            account_b_uuid,
        ]
        cur.execute(
            """
            SELECT stream_uuid::text
            FROM m_confirmed_external_stream_access
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY stream_uuid
            """,
            (project_uuid, owner_a_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == sorted(
            [stream_a_x_uuid, stream_b_y_uuid]
        )
        cur.execute(
            """
            SELECT uuid::text
            FROM m_workspace_user_streams
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
            """,
            (project_uuid, owner_a_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == sorted(
            [stream_a_x_uuid, stream_b_y_uuid]
        )
        cur.execute(
            """
            SELECT uuid::text
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
            """,
            (project_uuid, owner_a_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == sorted(
            [topic_a_x_uuid, topic_b_y_uuid]
        )
        cur.execute(
            """
            SELECT uuid::text
            FROM m_workspace_user_messages_view
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
            """,
            (project_uuid, owner_a_uuid),
        )
        assert [row[0] for row in cur.fetchall()] == sorted(
            [message_uuids[0], message_uuids[2]]
        )
        cur.execute(
            """
            SELECT uuid::text, unread_count
            FROM m_unread_user_messages
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
            """,
            (project_uuid, owner_a_uuid),
        )
        assert cur.fetchall() == sorted([(stream_a_x_uuid, 1), (stream_b_y_uuid, 1)])
        cur.execute(
            """
            SELECT uuid::text
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (
                project_uuid,
                owner_a_uuid,
                [*event_uuids, *reaction_event_uuids],
            ),
        )
        assert [row[0] for row in cur.fetchall()] == sorted(
            [
                event_uuids[0],
                event_uuids[2],
                reaction_event_uuids[0],
                reaction_event_uuids[2],
            ]
        )


def test_email_uniqueness_is_scoped_to_iam_users(_database, db):
    shared_email = "shared-provider-identity@example.invalid"
    rows = (
        ("10000000-0000-4000-8000-000000000001", "zulip-one", "zulip"),
        ("10000000-0000-4000-8000-000000000002", "zulip-two", "zulip"),
        ("10000000-0000-4000-8000-000000000003", "iam-one", "iam"),
    )
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO m_workspace_users (
                uuid, username, source, status, email, avatar,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, 'active', %s,
                'urn:gravatar:00000000000000000000000000000000',
                NOW(), NOW()
            )
            """,
            ((uuid, username, source, shared_email) for uuid, username, source in rows),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO m_workspace_users (
                    uuid, username, source, status, email, avatar,
                    created_at, updated_at
                ) VALUES (
                    %s, 'iam-two', 'iam', 'active', %s,
                    'urn:gravatar:00000000000000000000000000000000',
                    NOW(), NOW()
                )
                """,
                ("10000000-0000-4000-8000-000000000004", shared_email),
            )
        cur.execute(
            "DELETE FROM m_workspace_users WHERE uuid = ANY(%s::uuid[])",
            ([row[0] for row in rows],),
        )


def test_mail_cleanup_removes_upgraded_database_artifacts(_database, db):
    with db.cursor() as cur:
        cur.execute(
            'DELETE FROM "ra_migrations" WHERE uuid = %s',
            (CLEANUP_MIGRATION_UUID,),
        )
        cur.execute(
            """
            CREATE TABLE "m_messenger_import_runs_v1" (
                "run_uuid" UUID PRIMARY KEY
            );
            CREATE TABLE "m_messenger_import_items_v1" (
                "run_uuid" UUID REFERENCES "m_messenger_import_runs_v1"
            );
            CREATE TABLE "m_messenger_import_checkpoints_v1" (
                "run_uuid" UUID REFERENCES "m_messenger_import_runs_v1"
            );
            CREATE TABLE "m_messenger_import_quarantine_v1" (
                "run_uuid" UUID REFERENCES "m_messenger_import_runs_v1"
            );

            CREATE TABLE "m_messenger_writer_gates_v1" (
                "gate_uuid" UUID PRIMARY KEY
            );
            CREATE TABLE "m_messenger_writer_instances_v1" (
                "instance_id" TEXT PRIMARY KEY
            );
            CREATE TABLE "m_messenger_writer_gate_expected_v1" (
                "gate_uuid" UUID PRIMARY KEY REFERENCES
                    "m_messenger_writer_gates_v1" ("gate_uuid")
            );
            CREATE TABLE "m_messenger_writer_gate_acks_v1" (
                "gate_uuid" UUID PRIMARY KEY REFERENCES
                    "m_messenger_writer_gate_expected_v1" ("gate_uuid")
            );
            CREATE TABLE "m_messenger_writer_gate_releases_v1" (
                "gate_uuid" UUID PRIMARY KEY
            );

            CREATE OR REPLACE VIEW "m_workspace_visible_files_v1" AS
            SELECT files.*, accesses."user_uuid" AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            JOIN "m_workspace_file_accesses" AS accesses
              ON accesses."project_id" = files."project_id"
             AND accesses."file_uuid" = files."uuid"
            UNION
            SELECT files.*, NULL::UUID AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            WHERE files."acl_mode" = 'public';
            """
        )
        cur.executemany(
            """
            INSERT INTO "ra_migrations" (uuid, applied)
            VALUES (%s, TRUE)
            ON CONFLICT (uuid) DO UPDATE SET applied = TRUE
            """,
            ((migration_uuid,) for migration_uuid in LEGACY_MIGRATION_UUIDS),
        )

    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.apply_migration(CLEANUP_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('public.' || table_name)
            FROM unnest(%s::text[]) AS table_name
            """,
            (list(LEGACY_TABLES),),
        )
        assert cur.fetchall() == [(None,)] * len(LEGACY_TABLES)
        cur.execute(
            'SELECT uuid FROM "ra_migrations" WHERE uuid = ANY(%s::text[])',
            (list(LEGACY_MIGRATION_UUIDS),),
        )
        assert cur.fetchall() == []
        cur.execute(
            'SELECT applied FROM "ra_migrations" WHERE uuid = %s',
            (CLEANUP_MIGRATION_UUID,),
        )
        assert cur.fetchone() == (True,)
        cur.execute(
            "SELECT pg_get_viewdef('m_workspace_visible_files_v1'::regclass, TRUE)"
        )
        view_definition = cur.fetchone()[0]
        assert "m_workspace_stream_bindings" in view_definition
        assert "m_workspace_file_accesses" not in view_definition
