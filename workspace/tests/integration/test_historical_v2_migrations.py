# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Published native cutover migrations against an isolated historical schema."""

import json
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import legacy_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/messenger/streams/"
STREAM_BINDINGS = f"{V1}/messenger/stream_bindings/"
STREAM_TOPICS = f"{V1}/messenger/stream_topics/"
MESSAGES = f"{V1}/messenger/messages/"
MESSAGE_REACTIONS = f"{V1}/messenger/message_reactions/"
FOLDERS = f"{V1}/messenger/folders/"
FOLDER_ITEMS = f"{V1}/messenger/folder_items/"
FILES = f"{V1}/messenger/files/"
DRAFTS = f"{V1}/messenger/drafts/"
EVENTS = f"{V1}/events/"
V2_MIGRATION = "0152-add-messenger-v2-canonical-model-b59d87.py"
PREPARE_V2_MIGRATION = "0155-prepare-immutable-messenger-v2-cutover-887065.py"
PREPARE_V2_MIGRATION_UUID = "8870659b-eeb7-4e1c-9f3a-d84ff25dea96"
CURRENT_MIGRATION_HEAD = "0156-repair-retained-provider-identities-2022d5.py"
ZULIP_PROJECTION_RESET_MIGRATION = "0157-reset-zulip-projections-9a596b.py"
ZULIP_PROJECTION_RESET_UUID = "9a596b13-a187-45d6-8da6-d3b5d39a5c85"
ZULIP_MESSAGE_RESET_MIGRATION = "0158-reset-Zulip-message-projections-c1e8bf.py"
ZULIP_MESSAGE_RESET_UUID = "c1e8bf60-ff3c-4027-9b8c-410bec2c959d"
PROJECTION_CLAIM_INDEX_MIGRATION = (
    "0159-index-Messenger-v2-projection-claim-order-16837b.py"
)
INTERACTIVE_READ_INDEX_MIGRATION = (
    "0160-repair-native-read-state-and-prioritize-reads-259cc2.py"
)
PROVIDER_READ_PAGE_UNBLOCK_MIGRATION = (
    "0161-Unblock-interleaved-provider-read-pages-d06433.py"
)
PROVIDER_OWNER_READ_REPAIR_MIGRATION = "0162-repair-provider-owner-read-state-785e06.py"
DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION = (
    "0163-repair-duplicate-provider-projections-867945.py"
)
PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION = (
    "0164-stabilize-provider-private-chat-labels-f8bd03.py"
)
PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION = (
    "0165-repair-provider-participant-message-state-73514c.py"
)
V2_COMPATIBILITY_READ_REPAIR_MIGRATION = (
    "0166-repair-v2-compatibility-read-state-95c09d.py"
)
COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION = (
    "0167-reconcile-compact-topic-read-stats-303da9.py"
)
COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION = (
    "0168-reconcile-compact-topic-message-stats-7b45ba.py"
)
PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION = "0169-prefer-provider-chat-labels-485d62.py"
PROVIDER_CHAT_OWNER_LABEL_MIGRATION = "0170-accept-provider-chat-owner-labels-90d43c.py"
CANCELLED_PROVIDER_READ_MIGRATION = (
    "0171-discard-cancelled-provider-read-snapshots-87ed2e.py"
)
EXPIRED_PROVIDER_READ_RETRY_MIGRATION = (
    "0172-retry-expired-provider-read-pages-05d036.py"
)
PROJECTION_ACCELERATION_MIGRATION = "0173-accelerate-Messenger-v2-projections-8cda92.py"
LEGACY_BACKFILL_COUNTER_MIGRATION = "0174-suppress-legacy-backfill-counters-a2cd99.py"


@pytest.fixture(autouse=True)
def _historical_database(_database):
    # Schema 0165 is the original baseline for the published cutover tests.
    with conftest.isolated_migration_database(
        "0165-repair-provider-participant-message-state-73514c.py"
    ) as database_url:
        yield database_url


@pytest.fixture
def db(_historical_database):
    with psycopg.connect(_historical_database, autocommit=True) as connection:
        yield connection


def _truncate_messenger_test_data():
    """Fence this module from the integration suite's session-scoped data."""
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            CREATE TEMP TABLE messenger_v2_test_system_users
                (LIKE m_workspace_users INCLUDING ALL) ON COMMIT DROP;
            INSERT INTO messenger_v2_test_system_users
            SELECT * FROM m_workspace_users
            WHERE uuid = '00000000-0000-0000-0000-000000000000'::uuid;

            CREATE TEMP TABLE messenger_v2_test_system_folders
                (LIKE m_folders INCLUDING ALL) ON COMMIT DROP;
            INSERT INTO messenger_v2_test_system_folders
            SELECT * FROM m_folders
            WHERE uuid IN (
                '00000000-0000-0000-0000-000000000000'::uuid,
                '00000000-0000-0000-0000-000000000001'::uuid,
                '00000000-0000-0000-0000-000000000002'::uuid
            );

            TRUNCATE TABLE
                m_workspace_users,
                m_workspace_event_audience_snapshots_v1,
                m_workspace_read_state_projects_v1,
                m_workspace_read_state_compaction_v1,
                m_workspace_user_read_chunks_v1,
                m_workspace_message_mentions_v1,
                m_workspace_user_topic_read_stats_v1,
                m_workspace_topic_message_stats_v1,
                m_external_accounts_v2
            RESTART IDENTITY CASCADE;

            INSERT INTO m_workspace_users
            SELECT * FROM messenger_v2_test_system_users;
            INSERT INTO m_folders
            SELECT * FROM messenger_v2_test_system_folders;
            """,
            (),
        )


@pytest.fixture(autouse=True)
def _production_v2_store():
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            legacy_store.LegacyMessengerStoreFactory()
        )


@pytest.fixture(autouse=True)
def _isolate_projection_queue(db):
    """Keep each v2 worker scenario independent from earlier scale fixtures."""
    with db.cursor() as cursor:
        cursor.execute(
            """
            DO $cleanup$
            BEGIN
                IF to_regclass('messenger_projection_scope_leases') IS NOT NULL THEN
                    DELETE FROM messenger_projection_scope_leases;
                END IF;
                IF to_regclass('messenger_domain_outbox_events') IS NOT NULL THEN
                    DELETE FROM messenger_domain_outbox_events;
                END IF;
            END
            $cleanup$;
            """
        )


def _drain() -> int:
    with contexts.Context().session_manager() as session:
        processed = v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
        failures = session.execute(
            """
            SELECT task_kind, status, last_error
            FROM messenger_projection_tasks
            WHERE status IN ('failed', 'dead_letter')
            ORDER BY created_at, uuid
            """,
            (),
        ).fetchall()
        assert failures == []
        return processed


def _register_project_user(db, project_uuid, user_uuid):
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT messenger_v2_register_project_user(%s, %s)",
            (project_uuid, user_uuid),
        )


def test_native_read_repair_migration_preserves_compact_reads(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(PROVIDER_OWNER_READ_REPAIR_MIGRATION)
    engine.rollback_migration(PROVIDER_READ_PAGE_UNBLOCK_MIGRATION)
    engine.rollback_migration(INTERACTIVE_READ_INDEX_MIGRATION)
    try:
        peer_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)
        stream = api.post(
            STREAMS,
            json={
                "name": "Compact native read migration",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        ).json()
        _drain()
        added = api.post(
            f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(peer_uuid)]},
        )
        assert added.status_code == 200, added.text
        _drain()
        topic_uuid = stream["default_topic_uuid"]
        messages = []
        for content in ("compact read", "canonical read"):
            response = api.post(
                MESSAGES,
                json={
                    "stream_uuid": stream["uuid"],
                    "topic_uuid": topic_uuid,
                    "payload": {"kind": "markdown", "content": content},
                },
            )
            assert response.status_code == 201, response.text
            messages.append(response.json()["uuid"])
            _drain()

        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode
                ) VALUES (%s, 'compact')
                ON CONFLICT (project_id) DO UPDATE SET mode='compact'
                """,
                (api.project_id,),
            )
            cursor.execute(
                """
                SELECT legacy.ingest_sequence
                FROM messenger_message_placements AS placement
                JOIN m_workspace_messages AS legacy
                  ON legacy.project_id=placement.project_id
                 AND legacy.uuid=COALESCE(
                        placement.legacy_public_uuid,
                        placement.uuid
                     )
                WHERE placement.project_id=%s AND placement.uuid=%s
                """,
                (api.project_id, messages[0]),
            )
            ingest_sequence = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO m_workspace_user_read_chunks_v1 (
                    user_uuid, chunk_number, read_bits
                ) VALUES (
                    %s, %s / 4096,
                    set_bit(
                        B'0'::bit(4096),
                        (%s %% 4096)::integer,
                        1
                    )
                )
                ON CONFLICT (user_uuid, chunk_number) DO UPDATE
                SET read_bits=set_bit(
                    m_workspace_user_read_chunks_v1.read_bits,
                    (%s %% 4096)::integer,
                    1
                ), updated_at=NOW()
                """,
                (peer_uuid, ingest_sequence, ingest_sequence, ingest_sequence),
            )
            cursor.execute(
                """
                UPDATE messenger_user_message_states
                SET read_at=NOW(), updated_at=NOW()
                WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s
                """,
                (api.project_id, peer_uuid, messages[1]),
            )
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count=9, active_unread_count=9,
                    passive_unread_count=0
                WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s
                """,
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count=9, active_unread_count=9,
                    passive_unread_count=0
                WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s
                """,
                (api.project_id, peer_uuid, topic_uuid),
            )
        db.commit()

        engine.apply_migration(INTERACTIVE_READ_INDEX_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT placement_uuid, read_at IS NOT NULL
                FROM messenger_user_message_states
                WHERE project_id=%s AND user_uuid=%s
                  AND placement_uuid=ANY(%s::uuid[])
                ORDER BY placement_uuid
                """,
                (api.project_id, peer_uuid, messages),
            )
            assert {str(row[0]): row[1] for row in cursor.fetchall()} == {
                message_uuid: True for message_uuid in messages
            }
            cursor.execute(
                """
                SELECT unread_count, active_unread_count, passive_unread_count
                FROM messenger_stream_bindings
                WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s
                """,
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count, passive_unread_count
                FROM messenger_user_topic_bindings
                WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s
                """,
                (api.project_id, peer_uuid, topic_uuid),
            )
            assert cursor.fetchone() == (0, 0, 0)
    finally:
        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)


def test_v2_compatibility_read_repair_restores_all_canonical_reads(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    peer_uuid = sys_uuid.uuid4()
    try:
        conftest.seed_workspace_user(db, peer_uuid, f"compat-read-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)
        stream = api.post(
            STREAMS,
            json={
                "name": "Compatibility read repair",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        ).json()
        _drain()
        response = api.post(
            f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(peer_uuid)]},
        )
        assert response.status_code == 200, response.text
        _drain()
        empty_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream["uuid"],
            api.user_uuid,
            "Compatibility empty topic",
        )
        message_uuids = []
        for content in ("compatibility read one", "compatibility read two"):
            response = api.post(
                MESSAGES,
                json={
                    "stream_uuid": stream["uuid"],
                    "topic_uuid": stream["default_topic_uuid"],
                    "payload": {"kind": "markdown", "content": content},
                },
            )
            assert response.status_code == 201, response.text
            message_uuids.append(response.json()["uuid"])
            _drain()

        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode
                ) VALUES (%s, 'compact')
                ON CONFLICT (project_id) DO UPDATE SET mode = 'compact'
                """,
                (api.project_id,),
            )
            cursor.execute(
                """
                UPDATE messenger_user_message_states
                SET read_at = NOW(), updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s
                  AND placement_uuid = ANY(%s::uuid[])
                """,
                (api.project_id, peer_uuid, message_uuids),
            )
            cursor.execute(
                """
                DELETE FROM m_workspace_user_read_chunks_v1
                WHERE user_uuid = %s
                """,
                (peer_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_user_topic_read_stats_v1 (
                    project_id, user_uuid, topic_uuid, read_count
                ) VALUES (%s, %s, %s, 0)
                ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
                SET read_count = 0, updated_at = NOW()
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_user_topic_read_stats_v1 (
                    project_id, user_uuid, topic_uuid, read_count
                ) VALUES (%s, %s, %s, 7)
                ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
                SET read_count = 7, updated_at = NOW()
                """,
                (api.project_id, peer_uuid, empty_topic_uuid),
            )
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 0, active_unread_count = 0,
                    passive_unread_count = 0, updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count = 0, active_unread_count = 0,
                    passive_unread_count = 0, updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
        db.commit()

        engine.apply_migration(V2_COMPATIBILITY_READ_REPAIR_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                JOIN m_workspace_messages AS legacy
                  ON legacy.project_id = placement.project_id
                 AND legacy.uuid = COALESCE(
                        placement.legacy_public_uuid,
                        placement.uuid
                     )
                LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
                  ON chunk.user_uuid = state.user_uuid
                 AND chunk.chunk_number = legacy.ingest_sequence / 4096
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND state.placement_uuid = ANY(%s::uuid[])
                  AND state.read_at IS NOT NULL
                  AND COALESCE(
                      get_bit(
                          chunk.read_bits,
                          (legacy.ingest_sequence %% 4096)::integer
                      ),
                      0
                  ) = 1
                """,
                (api.project_id, peer_uuid, message_uuids),
            )
            assert cursor.fetchone() == (2,)
            cursor.execute(
                """
                SELECT read_count
                FROM m_workspace_user_topic_read_stats_v1
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (2,)
            cursor.execute(
                """
                UPDATE m_workspace_user_topic_read_stats_v1
                SET read_count = 0, updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
        db.commit()

        engine.apply_migration(COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT read_count
                FROM m_workspace_user_topic_read_stats_v1
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (2,)
            cursor.execute(
                """
                SELECT read_count
                FROM m_workspace_user_topic_read_stats_v1
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, peer_uuid, empty_topic_uuid),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count,
                       passive_unread_count
                FROM m_workspace_user_streams
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count,
                       passive_unread_count
                FROM m_workspace_user_topics_view
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (api.project_id, peer_uuid, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
            cursor.execute(
                """
                UPDATE m_workspace_topic_message_stats_v1
                SET message_count = message_count + 1,
                    last_ingest_sequence = last_ingest_sequence + 1,
                    updated_at = NOW()
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (api.project_id, stream["default_topic_uuid"]),
            )
        db.commit()

        engine.apply_migration(COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT stats.message_count = actual.message_count,
                       stats.last_ingest_sequence = actual.last_ingest_sequence
                FROM m_workspace_topic_message_stats_v1 AS stats
                CROSS JOIN LATERAL (
                    SELECT count(*)::bigint AS message_count,
                           max(message.ingest_sequence) AS last_ingest_sequence
                    FROM m_workspace_messages AS message
                    WHERE message.project_id = stats.project_id
                      AND message.topic_uuid = stats.topic_uuid
                ) AS actual
                WHERE stats.project_id = %s AND stats.topic_uuid = %s
                """,
                (api.project_id, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count,
                       passive_unread_count
                FROM m_workspace_user_streams
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (api.project_id, peer_uuid, stream["uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
    finally:
        engine.rollback_migration(COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION)
        engine.rollback_migration(COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION)
        engine.rollback_migration(V2_COMPATIBILITY_READ_REPAIR_MIGRATION)
        _truncate_messenger_test_data()


def test_native_v2_migration_rewrites_and_rolls_back_legacy_message_identity(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    audience_uuid = None
    api_store.configure_store_factory(
        legacy_store.LegacyMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Legacy cutover"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Legacy topic",
            is_default=True,
        )
        legacy = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "legacy message"},
            },
        )
        assert legacy.status_code == 201, legacy.text
        legacy_uuid = legacy.json()["uuid"]
        orphan_user_uuid = sys_uuid.uuid4()
        audience_uuid = sys_uuid.uuid4()
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_workspace_event_audience_snapshots_v1 (
                    uuid, project_id, membership_digest
                ) VALUES (%s, %s, %s)
                """,
                (audience_uuid, api.project_id, f"orphan:{audience_uuid}"),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_event_audience_members_v1 (
                    audience_snapshot_uuid, user_uuid
                ) VALUES (%s, %s)
                """,
                (audience_uuid, orphan_user_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_broadcast_message_events_v1 (
                    uuid, project_id, entity_uuid,
                    audience_snapshot_uuid, object_type, action, payload
                ) VALUES (
                    %s, %s, %s, %s, 'message', 'created',
                    jsonb_build_object(
                        'uuid', %s::text,
                        'stream_uuid', %s::text,
                        'topic_uuid', %s::text,
                        'source_name', 'native'
                    )
                )
                """,
                (
                    sys_uuid.uuid4(),
                    api.project_id,
                    legacy_uuid,
                    audience_uuid,
                    legacy_uuid,
                    stream_uuid,
                    topic_uuid,
                ),
            )
        engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM messenger_project_users
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, orphan_user_uuid),
            )
            assert cursor.fetchone() == (0,)

        placement_uuid = str(
            sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), str(legacy_uuid).lower())
        )
        mapped = api.get(f"{MESSAGES}{legacy_uuid}")
        assert mapped.status_code == 200, mapped.text
        assert mapped.json()["uuid"] == placement_uuid
        native_v2 = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "native v2 message"},
            },
        )
        assert native_v2.status_code == 201, native_v2.text
        native_v2_uuid = native_v2.json()["uuid"]
        _drain()
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload->>'uuid'
                FROM m_workspace_visible_events
                WHERE project_id = %s AND object_type = 'message'
                  AND action = 'created'
                ORDER BY epoch_version
                """,
                (api.project_id,),
            )
            assert {row[0] for row in cursor.fetchall()} >= {
                placement_uuid,
                native_v2_uuid,
            }

        api_store.configure_store_factory(
            legacy_store.LegacyMessengerStoreFactory()
        )
        engine.rollback_migration(V2_MIGRATION)
        restored = api.get(f"{MESSAGES}{legacy_uuid}")
        assert restored.status_code == 200, restored.text
        assert restored.json()["uuid"] == legacy_uuid
        restored_v2 = api.get(f"{MESSAGES}{native_v2_uuid}")
        assert restored_v2.status_code == 200, restored_v2.text
        assert restored_v2.json()["uuid"] == native_v2_uuid
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload->>'uuid'
                FROM m_workspace_visible_events
                WHERE project_id = %s AND object_type = 'message'
                  AND action = 'created'
                ORDER BY epoch_version
                """,
                (api.project_id,),
            )
            assert {row[0] for row in cursor.fetchall()} >= {
                legacy_uuid,
                native_v2_uuid,
            }
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                (PREPARE_V2_MIGRATION_UUID,),
            )
            assert cursor.fetchone() == (False,)

        _truncate_messenger_test_data()
        engine.apply_migration(CURRENT_MIGRATION_HEAD)
        api_store.configure_store_factory(store_factory.build_store_factory())
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                (PREPARE_V2_MIGRATION_UUID,),
            )
            assert cursor.fetchone() == (True,)
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            migration_applied = cursor.fetchone()[0] is not None
        if not migration_applied:
            engine.apply_migration(V2_MIGRATION)
        if audience_uuid is not None:
            with db.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM m_workspace_broadcast_message_events_v1 "
                    "WHERE audience_snapshot_uuid = %s",
                    (audience_uuid,),
                )
                cursor.execute(
                    "DELETE FROM m_workspace_event_audience_snapshots_v1 "
                    "WHERE uuid = %s",
                    (audience_uuid,),
                )
        api_store.configure_store_factory(store_factory.build_store_factory())


def test_native_v2_migration_fences_revoked_historical_events(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        legacy_store.LegacyMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    try:
        peer_uuid = sys_uuid.uuid4()
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Historical event fence"
        )
        conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, peer_uuid)
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Historical topic",
            is_default=True,
        )
        message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "historical"},
            },
        )
        assert message.status_code == 201, message.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT event.uuid
                FROM m_workspace_visible_events AS event
                WHERE event.project_id = %s AND event.user_uuid = %s
                  AND event.object_type = 'message' AND event.action = 'created'
                  AND event.payload->>'uuid' = %s
                """,
                (api.project_id, peer_uuid, message.json()["uuid"]),
            )
            historical_event_uuid = cursor.fetchone()[0]
            cursor.execute(
                """
                DELETE FROM m_workspace_stream_bindings
                WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
                """,
                (api.project_id, stream_uuid, peer_uuid),
            )
            cursor.execute(
                """
                SELECT count(*) FROM m_workspace_visible_events
                WHERE uuid = %s AND user_uuid = %s
                """,
                (historical_event_uuid, peer_uuid),
            )
            assert cursor.fetchone()[0] == 0

        engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())
        added = api.post(
            f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
            json={"member": [str(peer_uuid)]},
        )
        assert added.status_code == 200, added.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT guard.membership_generation, binding.membership_generation,
                       binding.active
                FROM messenger_event_membership_guards AS guard
                JOIN messenger_stream_bindings AS binding
                  ON binding.project_id = guard.project_id
                 AND binding.stream_uuid = guard.stream_uuid
                 AND binding.user_uuid = guard.user_uuid
                WHERE guard.event_uuid = %s AND guard.user_uuid = %s
                """,
                (historical_event_uuid, peer_uuid),
            )
            assert cursor.fetchone() == (1, 2, True)
            cursor.execute(
                """
                SELECT count(*) FROM m_workspace_visible_events
                WHERE uuid = %s AND user_uuid = %s
                """,
                (historical_event_uuid, peer_uuid),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            migration_applied = cursor.fetchone()[0] is not None
        if not migration_applied:
            engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())


def test_native_v2_migration_requires_explicit_large_cutover_authorization(
    api, db, monkeypatch
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        legacy_store.LegacyMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Bounded migration stream"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Bounded migration topic",
            is_default=True,
        )
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"native"}'::jsonb,
                    'native', '{"kind":"native"}'::jsonb
                )
                """,
                [
                    (
                        sys_uuid.uuid4(),
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                    )
                    for _index in range(2)
                ],
            )

        monkeypatch.setenv("WORKSPACE_MESSENGER_V2_CUTOVER_ROW_LIMIT", "1000001")
        with pytest.raises(
            Exception, match="raising the unattended cutover limit requires"
        ):
            engine.apply_migration(V2_MIGRATION)

        monkeypatch.setenv("WORKSPACE_MESSENGER_V2_CUTOVER_ROW_LIMIT", "1")
        with pytest.raises(Exception, match="explicitly authorized rehearsed cutover"):
            engine.apply_migration(V2_MIGRATION)

        monkeypatch.setenv("WORKSPACE_MESSENGER_V2_LARGE_CUTOVER_AUTHORIZED", "on")
        engine.apply_migration(V2_MIGRATION)
    finally:
        monkeypatch.delenv("WORKSPACE_MESSENGER_V2_CUTOVER_ROW_LIMIT", raising=False)
        monkeypatch.delenv(
            "WORKSPACE_MESSENGER_V2_LARGE_CUTOVER_AUTHORIZED", raising=False
        )
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            migration_applied = cursor.fetchone()[0] is not None
        if not migration_applied:
            engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())


def test_native_v2_migration_collapses_realm_chat_aliases_and_keeps_native_rows(
    api, db
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        legacy_store.LegacyMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    try:
        peer_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
        canonical_stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Canonical provider projection"
        )
        alias_stream_uuid = conftest.seed_user_stream(
            db, api.project_id, peer_uuid, "Alias provider projection"
        )
        conftest.seed_user_stream_binding(
            db, api.project_id, canonical_stream_uuid, peer_uuid
        )
        conftest.seed_user_stream_binding(
            db, api.project_id, alias_stream_uuid, api.user_uuid
        )
        canonical_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            canonical_stream_uuid,
            api.user_uuid,
            "Provider topic",
            is_default=True,
        )
        alias_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            alias_stream_uuid,
            peer_uuid,
            "Provider topic",
            is_default=True,
        )
        native_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            alias_stream_uuid,
            peer_uuid,
            "Workspace-only topic",
        )
        canonical_catalog_topic_uuid = sys_uuid.uuid4()
        alias_catalog_topic_uuid = sys_uuid.uuid4()
        realm_uuid = sys_uuid.uuid4()
        bridge_uuid = sys_uuid.uuid4()
        canonical_account_uuid = sys_uuid.uuid4()
        alias_account_uuid = sys_uuid.uuid4()
        canonical_chat_uuid = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
        alias_chat_uuid = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
        provider_message_uuid = sys_uuid.uuid4()
        native_message_uuid = sys_uuid.uuid4()
        source_base = {
            "kind": "zulip",
            "chat_type": "channel",
            "description": "",
            "participants": [],
            "provider_realm_uuid": str(realm_uuid),
        }
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
                VALUES (%s, 'zulip')
                """,
                (bridge_uuid,),
            )
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    provider_realm_uuid, provider_owner_user_id,
                    status, live_ready
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, %s, %s, 'live', TRUE
                )
                """,
                (
                    (
                        canonical_account_uuid,
                        api.user_uuid,
                        realm_uuid,
                        "provider-owner-1",
                    ),
                    (alias_account_uuid, peer_uuid, realm_uuid, "provider-owner-2"),
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET source_name = 'zulip',
                    source = jsonb_build_object(
                        'kind', 'zulip', 'stream_id', 42,
                        'server_url', 'https://provider.example.invalid'
                    ),
                    external_account_uuid = CASE uuid
                        WHEN %s THEN %s::uuid ELSE %s::uuid END,
                    provider_external_id = 'channel:42'
                WHERE project_id = %s AND uuid IN (%s, %s)
                """,
                (
                    canonical_stream_uuid,
                    canonical_account_uuid,
                    alias_account_uuid,
                    api.project_id,
                    canonical_stream_uuid,
                    alias_stream_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_stream_topics
                SET source_name = 'zulip',
                    source = jsonb_build_object('kind', 'zulip'),
                    external_account_uuid = CASE uuid
                        WHEN %s THEN %s::uuid ELSE %s::uuid END,
                    provider_external_id = 'provider-topic'
                WHERE uuid IN (%s, %s)
                """,
                (
                    canonical_topic_uuid,
                    canonical_account_uuid,
                    alias_account_uuid,
                    canonical_topic_uuid,
                    alias_topic_uuid,
                ),
            )
            cursor.executemany(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                    'Shared provider channel', TRUE, %s, %s, 'live'
                )
                """,
                (
                    (
                        canonical_chat_uuid,
                        canonical_account_uuid,
                        api.user_uuid,
                        json.dumps(
                            {
                                **source_base,
                                "topics": [
                                    {
                                        "provider_topic_id": "provider-topic",
                                        "topic_uuid": canonical_topic_uuid,
                                        "name": "Provider topic",
                                        "is_default": True,
                                    },
                                    {
                                        "provider_topic_id": "provider-unmaterialized",
                                        "topic_uuid": canonical_catalog_topic_uuid,
                                        "name": "Catalog only",
                                        "is_default": False,
                                    },
                                ],
                            },
                            default=str,
                        ),
                        api.project_id,
                        canonical_stream_uuid,
                    ),
                    (
                        alias_chat_uuid,
                        alias_account_uuid,
                        peer_uuid,
                        json.dumps(
                            {
                                **source_base,
                                "topics": [
                                    {
                                        "provider_topic_id": "provider-topic",
                                        "topic_uuid": alias_topic_uuid,
                                        "name": "Provider topic",
                                        "is_default": True,
                                    },
                                    {
                                        "provider_topic_id": "provider-unmaterialized",
                                        "topic_uuid": alias_catalog_topic_uuid,
                                        "name": "Catalog only",
                                        "is_default": False,
                                    },
                                ],
                            },
                            default=str,
                        ),
                        api.project_id,
                        alias_stream_uuid,
                    ),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    jsonb_build_object('kind', 'markdown', 'content', %s::text),
                    'native', '{"kind":"native"}'::jsonb
                )
                """,
                (
                    (
                        provider_message_uuid,
                        api.project_id,
                        alias_stream_uuid,
                        alias_topic_uuid,
                        peer_uuid,
                        "native message in provider topic",
                    ),
                    (
                        native_message_uuid,
                        api.project_id,
                        alias_stream_uuid,
                        native_topic_uuid,
                        peer_uuid,
                        "native message in workspace-only topic",
                    ),
                ),
            )
        db.commit()

        engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid, projection_stream_uuid,
                       source#>>'{topics,0,topic_uuid}',
                       source#>>'{topics,1,topic_uuid}'
                FROM m_external_chats_v2
                WHERE uuid IN (%s, %s)
                ORDER BY uuid
                """,
                (canonical_chat_uuid, alias_chat_uuid),
            )
            assert cursor.fetchall() == [
                (
                    canonical_chat_uuid,
                    sys_uuid.UUID(canonical_stream_uuid),
                    canonical_topic_uuid,
                    str(canonical_catalog_topic_uuid),
                ),
                (
                    alias_chat_uuid,
                    sys_uuid.UUID(canonical_stream_uuid),
                    canonical_topic_uuid,
                    str(canonical_catalog_topic_uuid),
                ),
            ]
            cursor.execute(
                """
                SELECT
                    EXISTS(SELECT 1 FROM m_workspace_streams WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_stream_topics WHERE uuid = %s),
                    (SELECT stream_uuid FROM m_workspace_stream_topics
                     WHERE uuid = %s),
                    (SELECT count(*) FROM m_workspace_stream_bindings
                     WHERE project_id = %s AND stream_uuid = %s)
                """,
                (
                    alias_stream_uuid,
                    alias_topic_uuid,
                    native_topic_uuid,
                    api.project_id,
                    canonical_stream_uuid,
                ),
            )
            assert cursor.fetchone() == (
                False,
                False,
                sys_uuid.UUID(canonical_stream_uuid),
                2,
            )
            cursor.execute(
                """
                SELECT placement.message_uuid, placement.stream_uuid,
                       placement.topic_uuid
                FROM messenger_message_placements AS placement
                WHERE placement.message_uuid IN (%s, %s)
                ORDER BY placement.message_uuid
                """,
                (provider_message_uuid, native_message_uuid),
            )
            placements = {row[0]: row[1:] for row in cursor.fetchall()}
            assert placements[provider_message_uuid] == (
                sys_uuid.UUID(canonical_stream_uuid),
                sys_uuid.UUID(canonical_topic_uuid),
            )
            assert placements[native_message_uuid] == (
                sys_uuid.UUID(canonical_stream_uuid),
                sys_uuid.UUID(native_topic_uuid),
            )
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            migration_applied = cursor.fetchone()[0] is not None
        if not migration_applied:
            engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())


@pytest.mark.parametrize("materialized", [True, False])
def test_native_v2_migration_rejects_realm_chat_selected_in_multiple_projects(
    api, db, materialized
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        legacy_store.LegacyMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    account_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    chat_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    project_uuids = (api.project_id, str(sys_uuid.uuid4()))
    peer_uuid = sys_uuid.uuid4()
    owner_uuids = (api.user_uuid, peer_uuid)
    stream_uuids = (
        conftest.seed_user_stream(
            db, project_uuids[0], owner_uuids[0], "Provider projection one"
        ),
        conftest.seed_user_stream(
            db, project_uuids[1], owner_uuids[1], "Provider projection two"
        ),
    )
    provider_realm_uuid = sys_uuid.uuid4()
    try:
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    provider_realm_uuid, provider_owner_user_id,
                    status, live_ready
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, %s, %s, 'live', TRUE
                )
                """,
                (
                    (
                        account_uuids[0],
                        owner_uuids[0],
                        provider_realm_uuid,
                        "provider-owner-1",
                    ),
                    (
                        account_uuids[1],
                        owner_uuids[1],
                        provider_realm_uuid,
                        "provider-owner-2",
                    ),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'channel:42',
                    '{"chat_type":"channel"}'::jsonb,
                    'Cross-project provider chat', TRUE, %s, %s, 'live'
                )
                """,
                (
                    (
                        chat_uuids[0],
                        account_uuids[0],
                        owner_uuids[0],
                        project_uuids[0],
                        stream_uuids[0] if materialized else None,
                    ),
                    (
                        chat_uuids[1],
                        account_uuids[1],
                        owner_uuids[1],
                        project_uuids[1],
                        stream_uuids[1] if materialized else None,
                    ),
                ),
            )

        with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
            engine.apply_migration(V2_MIGRATION)
        assert "provider realm chat is selected in multiple projects" in str(
            exc_info.value
        )
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            assert cursor.fetchone()[0] is None
            cursor.execute(
                """
                SELECT count(DISTINCT project_id),
                       count(DISTINCT projection_stream_uuid),
                       bool_and(selected)
                FROM m_external_chats_v2
                WHERE uuid = ANY(%s)
                """,
                (list(chat_uuids),),
            )
            assert cursor.fetchone() == (2, 2 if materialized else 0, True)
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_external_chats_v2 WHERE uuid = ANY(%s)",
                (list(chat_uuids),),
            )
            cursor.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid = ANY(%s)",
                (list(account_uuids),),
            )
            cursor.execute(
                """
                DELETE FROM m_workspace_streams
                WHERE project_id = ANY(%s) AND uuid = ANY(%s)
                """,
                (list(project_uuids), list(stream_uuids)),
            )
        engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())
