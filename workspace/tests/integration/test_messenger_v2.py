# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Native Messenger v2 cutover tests through the unchanged HTTP contract."""

import datetime
import itertools
import json
import types
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.external_bridge_control import provider_event_apply
from workspace.external_bridge_control import provider_v2
from workspace.external_bridge_control import provider_data
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
STREAM_BINDINGS = f"{V1}/stream_bindings/"
STREAM_TOPICS = f"{V1}/stream_topics/"
MESSAGES = f"{V1}/messages/"
MESSAGE_REACTIONS = f"{V1}/message_reactions/"
FOLDERS = f"{V1}/folders/"
FOLDER_ITEMS = f"{V1}/folder_items/"
FILES = f"{V1}/files/"
DRAFTS = f"{V1}/drafts/"
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
LEGACY_BACKFILL_COUNTER_MIGRATION = (
    "0174-suppress-legacy-backfill-counters-a2cd99.py"
)


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
                m_external_operations_v2,
                m_external_provider_identity_links_v1,
                m_external_accounts_v2,
                m_external_chats_v2,
                m_external_bridge_instances_v2
            RESTART IDENTITY CASCADE;

            INSERT INTO m_workspace_users
            SELECT * FROM messenger_v2_test_system_users;
            INSERT INTO m_folders
            SELECT * FROM messenger_v2_test_system_folders;
            """,
            (),
        )


@pytest.fixture(scope="module", autouse=True)
def _isolate_v2_module(_database):
    """Keep migration rollback/reapply tests independent and bounded."""
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(LEGACY_BACKFILL_COUNTER_MIGRATION)
    engine.rollback_migration(PROJECTION_ACCELERATION_MIGRATION)
    engine.rollback_migration(EXPIRED_PROVIDER_READ_RETRY_MIGRATION)
    engine.rollback_migration(CANCELLED_PROVIDER_READ_MIGRATION)
    engine.rollback_migration(PROVIDER_CHAT_OWNER_LABEL_MIGRATION)
    engine.rollback_migration(PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION)
    engine.rollback_migration(COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION)
    engine.rollback_migration(COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION)
    engine.rollback_migration(V2_COMPATIBILITY_READ_REPAIR_MIGRATION)
    _truncate_messenger_test_data()
    try:
        yield
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(V2_COMPATIBILITY_READ_REPAIR_MIGRATION)
        engine.apply_migration(COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION)
        engine.apply_migration(COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION)
        engine.apply_migration(PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION)
        engine.apply_migration(PROVIDER_CHAT_OWNER_LABEL_MIGRATION)
        engine.apply_migration(CANCELLED_PROVIDER_READ_MIGRATION)
        engine.apply_migration(EXPIRED_PROVIDER_READ_RETRY_MIGRATION)
        engine.apply_migration(PROJECTION_ACCELERATION_MIGRATION)
        engine.apply_migration(LEGACY_BACKFILL_COUNTER_MIGRATION)


@pytest.fixture(autouse=True)
def _production_v2_store():
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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


def _seed_v2_provider_route(db, project_uuid, owner_uuid, stream_uuid):
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    capabilities = {
        name: {"available": True, "revision": revision, "limits": {}}
        for name, revision in (
            ("messenger.message.send", 1),
            ("messenger.message.edit", 1),
            ("messenger.message.delete", 1),
            ("messenger.message.read", 2),
            ("messenger.message.read.paging", 1),
            ("messenger.reaction.write", 1),
            ("messenger.membership.write", 1),
            ("messenger.notification.write", 1),
            ("messenger.stream.delete", 1),
            ("messenger.stream.rename", 1),
            ("messenger.topic.create", 1),
            ("messenger.topic.rename", 1),
            ("messenger.topic.delete", 1),
        )
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_provider_policies_v1 (
                uuid, provider, enabled, limits
            ) VALUES (%s, 'zulip', TRUE, %s::jsonb)
            ON CONFLICT (provider) DO UPDATE SET
                enabled = TRUE, emergency_suspended = FALSE,
                limits = EXCLUDED.limits
            """,
            (
                sys_uuid.uuid4(),
                json.dumps(
                    {
                        "max_accounts": 100,
                        "max_selected_chats_per_account": 1000,
                        "max_file_bytes": 104857600,
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status,
                capabilities, last_heartbeat_at
            ) VALUES (%s, 'zulip', 1, 'active', %s::jsonb, NOW())
            """,
            (bridge_uuid, json.dumps(capabilities)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, %s::jsonb
            )
            """,
            (
                account_uuid,
                owner_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://provider.example.invalid",
                        "default_project_id": str(project_uuid),
                    }
                ),
                json.dumps(capabilities),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities,
                catalog_capabilities
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                'Provider v2 outbound', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "chat_type": "channel",
                        "participants": [],
                        "topics": [],
                    }
                ),
                project_uuid,
                stream_uuid,
                json.dumps(capabilities),
                json.dumps(capabilities),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip', source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:42'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(
                    {
                        "kind": "zulip",
                        "stream_id": 42,
                        "server_url": "https://provider.example.invalid",
                        "source_scope": str(account_uuid),
                    }
                ),
                account_uuid,
                project_uuid,
                stream_uuid,
            ),
        )
    db.commit()
    return account_uuid


def test_v2_store_preserves_existing_provider_outbound_actions(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Provider v2 outbound",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (project_id, mode)
            VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = 'compact'
            """,
            (api.project_id,),
        )
    db.commit()

    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]

    updated_stream = api.put(
        f"{STREAMS}{stream['uuid']}",
        json={"name": "Provider v2 outbound renamed"},
    )
    assert updated_stream.status_code == 200, updated_stream.text
    stream = updated_stream.json()
    topic_response = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Outbound topic",
            "source": {"kind": "native"},
        },
    )
    assert topic_response.status_code == 201, topic_response.text
    topic = topic_response.json()
    renamed_topic = api.put(
        f"{STREAM_TOPICS}{topic['uuid']}",
        json={"name": "Outbound topic renamed"},
    )
    assert renamed_topic.status_code == 200, renamed_topic.text

    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {"kind": "markdown", "content": "outbound"},
        },
    )
    assert message_response.status_code == 201, message_response.text
    message = message_response.json()
    updated_message = api.put(
        f"{MESSAGES}{message['uuid']}",
        json={"payload": {"kind": "markdown", "content": "edited"}},
    )
    assert updated_message.status_code == 200, updated_message.text
    reaction_response = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": message["uuid"],
            "emoji_name": "thumbs_up",
        },
    )
    assert reaction_response.status_code == 201, reaction_response.text
    reaction = reaction_response.json()
    updated_reaction = api.put(
        f"{MESSAGE_REACTIONS}{reaction['uuid']}",
        json={"emoji_name": "heart"},
    )
    assert updated_reaction.status_code == 200, updated_reaction.text

    stream_notifications = api.post(
        f"{STREAMS}{stream['uuid']}/actions/notifications/invoke",
        json={"notification_mode": "all_messages"},
    )
    assert stream_notifications.status_code == 200, stream_notifications.text
    topic_notifications = api.post(
        f"{STREAM_TOPICS}{topic['uuid']}/actions/notifications/invoke",
        json={"notification_mode": "follow"},
    )
    assert topic_notifications.status_code == 200, topic_notifications.text

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_user_message_states
            SET read_at = NULL, updated_at = NOW()
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, message["uuid"], api.user_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_user_read_chunks_v1 AS chunk
            SET read_bits = set_bit(
                    chunk.read_bits,
                    (legacy.ingest_sequence %% 4096)::integer,
                    0
                ),
                updated_at = NOW()
            FROM m_workspace_messages AS legacy
            WHERE legacy.project_id = %s AND legacy.uuid = %s
              AND chunk.user_uuid = %s
              AND chunk.chunk_number = legacy.ingest_sequence / 4096
            """,
            (api.project_id, message["uuid"], api.user_uuid),
        )
    db.commit()
    read_response = api.post(
        f"{MESSAGES}{message['uuid']}/actions/read/invoke",
    )
    assert read_response.status_code == 200, read_response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(
                get_bit(
                    chunk.read_bits,
                    (legacy.ingest_sequence %% 4096)::integer
                ),
                0
            )
            FROM m_workspace_messages AS legacy
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = %s
             AND chunk.chunk_number = legacy.ingest_sequence / 4096
            WHERE legacy.project_id = %s AND legacy.uuid = %s
            """,
            (api.user_uuid, api.project_id, message["uuid"]),
        )
        assert cursor.fetchone() == (1,)

    assert api.delete(f"{MESSAGE_REACTIONS}{reaction['uuid']}").status_code == 204
    assert api.delete(f"{MESSAGES}{message['uuid']}").status_code == 204
    assert api.delete(f"{STREAM_TOPICS}{topic['uuid']}").status_code == 204
    assert api.delete(f"{STREAM_BINDINGS}{binding_uuid}").status_code == 204
    assert api.delete(f"{STREAMS}{stream['uuid']}").status_code == 204

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT action
            FROM m_external_operations_v2
            WHERE external_account_uuid = %s
            ORDER BY created_at, uuid
            """,
            (account_uuid,),
        )
        actions = [row[0] for row in cursor.fetchall()]
    assert sorted(actions) == sorted(
        [
            "membership.add",
            "stream.update",
            "topic.create",
            "topic.update",
            "message.create",
            "message.update",
            "reaction.create",
            "reaction.update",
            "stream.notification.update",
            "topic.notification.update",
            "read_state.set",
            "reaction.delete",
            "message.delete",
            "topic.delete",
            "membership.remove",
            "stream.delete",
        ]
    )


def test_v2_message_replay_rejects_foreign_author_before_provider_routing(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Provider v2 replay ownership",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    author_account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )

    other_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, other_user_uuid, f"user-{other_user_uuid}")
    _register_project_user(db, api.project_id, other_user_uuid)
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(other_user_uuid)]},
    )
    assert added.status_code == 200, added.text
    other_account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        other_user_uuid,
        stream["uuid"],
    )

    canonical_message_uuid = sys_uuid.uuid4()
    payload = {"kind": "markdown", "content": "owned by the first author"}
    message_request = {
        "uuid": str(canonical_message_uuid),
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
        "payload": payload,
    }
    created = api.post(MESSAGES, json=message_request)
    assert created.status_code == 201, created.text

    foreign_replay = api.post(
        MESSAGES,
        user=other_user_uuid,
        json=message_request,
    )
    assert foreign_replay.status_code == 400, foreign_replay.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT external_account_uuid, COUNT(*)
            FROM m_external_operations_v2
            WHERE action = 'message.create' AND target_uuid = %s
            GROUP BY external_account_uuid
            """,
            (created.json()["uuid"],),
        )
        operations = dict(cursor.fetchall())
    assert operations == {author_account_uuid: 1}
    assert other_account_uuid not in operations


def test_v2_provider_result_reconciles_echo_by_placement_uuid(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "V2 provider echo",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )
    provider_realm_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET provider_realm_uuid = %s, provider_owner_user_id = '1'
            WHERE uuid = %s
            """,
            (provider_realm_uuid, account_uuid),
        )
    db.commit()

    native_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "v2 echo"},
        },
    )
    assert native_response.status_code == 201, native_response.text
    native_placement_uuid = sys_uuid.UUID(native_response.json()["uuid"])
    provider_message_id = "14101"
    echo_public_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT provider.bridge_instance_uuid,
                   provider.uuid, operation.uuid
            FROM m_external_operations_v2 AS operation
            JOIN m_external_provider_operations_v1 AS provider
              ON provider.external_operation_uuid = operation.uuid
            WHERE operation.external_account_uuid = %s
              AND operation.action = 'message.create'
              AND operation.target_uuid = %s
            """,
            (account_uuid, native_placement_uuid),
        )
        bridge_uuid, provider_operation_uuid, _operation_uuid = cursor.fetchone()
        cursor.execute(
            """
            UPDATE messenger_message_placements
            SET legacy_public_uuid = NULL
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, native_placement_uuid),
        )
        cursor.execute(
            """
            SELECT legacy_public_uuid
            FROM messenger_message_placements
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, native_placement_uuid),
        )
        assert cursor.fetchone() == (None,)
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source, provider_uuid,
                external_account_uuid, provider_external_id,
                provider_metadata, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s::jsonb, 'zulip', %s::jsonb, %s,
                %s, %s, %s::jsonb, NOW(), NOW()
            )
            """,
            (
                echo_public_uuid,
                api.project_id,
                stream["uuid"],
                stream["default_topic_uuid"],
                api.user_uuid,
                json.dumps({"kind": "markdown", "content": "v2 echo"}),
                json.dumps({"kind": "zulip", "message_id": provider_message_id}),
                bridge_uuid,
                account_uuid,
                provider_message_id,
                json.dumps(
                    {
                        "kind": "zulip",
                        "account_uuid": str(account_uuid),
                        "external_id": provider_message_id,
                        "provider_realm_uuid": str(provider_realm_uuid),
                        "capabilities": {},
                    }
                ),
            ),
        )
    db.commit()

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    with contexts.Context().session_manager() as session:
        leased = provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )["operations"]
    assert [item["provider_operation_uuid"] for item in leased] == [
        str(provider_operation_uuid)
    ]
    with contexts.Context().session_manager() as session:
        result = provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": str(provider_operation_uuid),
                "lease_uuid": leased[0]["lease_uuid"],
                "status": "succeeded",
                "provider_entity_id": provider_message_id,
            },
        )
    assert result["status"] == "applied"

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT placement.legacy_public_uuid,
                   message.provider_realm_uuid, message.provider_message_id
            FROM messenger_message_placements AS placement
            JOIN messenger_messages AS message
              ON message.project_id = placement.project_id
             AND message.uuid = placement.message_uuid
            WHERE placement.project_id = %s AND placement.uuid = %s
            """,
            (api.project_id, native_placement_uuid),
        )
        assert cursor.fetchone() == (
            native_placement_uuid,
            provider_realm_uuid,
            provider_message_id,
        )
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM messenger_messages
            WHERE provider_realm_uuid = %s AND provider_message_id = %s
            """,
            (provider_realm_uuid, provider_message_id),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            """
            SELECT COUNT(*) FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, echo_public_uuid),
        )
        assert cursor.fetchone() == (0,)


def test_provider_read_page_accepts_successful_send_before_realtime_echo(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Provider send result delivery evidence",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )
    provider_realm_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET provider_realm_uuid = %s, provider_owner_user_id = '1'
            WHERE uuid = %s
            """,
            (provider_realm_uuid, account_uuid),
        )
        cursor.execute(
            """
            SELECT (envelope#>>'{associated_data,bridge_instance_uuid}')::uuid
            FROM m_external_credentials_v2
            WHERE external_account_uuid = %s
            """,
            (account_uuid,),
        )
        bridge_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO m_external_provider_events_v1 (
                bridge_instance_uuid, provider_event_uuid,
                external_account_uuid, project_id, event_kind,
                payload_sha256, status, target_uuid
            ) VALUES (
                %s, gen_random_uuid(), %s, %s, 'message.upsert',
                repeat('0', 64), 'applied', gen_random_uuid()
            )
            """,
            (bridge_uuid, account_uuid, api.project_id),
        )
    db.commit()

    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {
                "kind": "markdown",
                "content": "read before realtime provider echo",
            },
        },
    )
    assert message_response.status_code == 201, message_response.text
    message_uuid = sys_uuid.UUID(message_response.json()["uuid"])
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT provider.uuid
            FROM m_external_operations_v2 AS operation
            JOIN m_external_provider_operations_v1 AS provider
              ON provider.external_operation_uuid = operation.uuid
            WHERE operation.external_account_uuid = %s
              AND operation.action = 'message.create'
              AND operation.target_uuid = %s
            """,
            (account_uuid, message_uuid),
        )
        provider_operation_uuid = cursor.fetchone()[0]

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    with contexts.Context().session_manager() as session:
        leased = provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )["operations"]
    assert [item["provider_operation_uuid"] for item in leased] == [
        str(provider_operation_uuid)
    ]
    provider_message_id = "15101"
    with contexts.Context().session_manager() as session:
        result = provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": str(provider_operation_uuid),
                "lease_uuid": leased[0]["lease_uuid"],
                "status": "succeeded",
                "provider_entity_id": provider_message_id,
            },
        )
    assert result["status"] == "applied"

    with contexts.Context().session_manager() as session:
        bindings = provider_data._delivered_provider_read_page_bindings(
            session,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            message_uuids=[message_uuid],
        )
        echo_count = session.execute(
            """
            SELECT count(*)
            FROM m_external_provider_events_v1
            WHERE project_id = %s AND external_account_uuid = %s
              AND event_kind = 'message.upsert'
              AND target_uuid = %s
            """,
            (api.project_id, account_uuid, message_uuid),
        ).fetchone()["count"]

    assert echo_count == 0
    assert bindings == [(message_uuid, provider_message_id)]


def test_provider_read_page_accepts_same_realm_projection_recipient(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Shared provider projection delivery evidence",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    _drain()

    source_account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )
    target_account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        peer_uuid,
        stream["uuid"],
    )
    provider_realm_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    provider_message_id = "15102"
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET provider_realm_uuid = %s,
                provider_owner_user_id = CASE
                    WHEN uuid = %s THEN '1'
                    ELSE '2'
                END
            WHERE uuid = ANY(%s::uuid[])
            """,
            (
                provider_realm_uuid,
                source_account_uuid,
                [source_account_uuid, target_account_uuid],
            ),
        )
        cursor.execute(
            """
            SELECT account.uuid,
                   (credential.envelope#>>
                       '{associated_data,bridge_instance_uuid}')::uuid
            FROM m_external_accounts_v2 AS account
            JOIN m_external_credentials_v2 AS credential
              ON credential.external_account_uuid = account.uuid
            WHERE account.uuid = ANY(%s::uuid[])
            """,
            ([source_account_uuid, target_account_uuid],),
        )
        bridge_uuids = dict(cursor.fetchall())
        cursor.execute(
            """
            INSERT INTO m_external_provider_events_v1 (
                bridge_instance_uuid, provider_event_uuid,
                external_account_uuid, project_id, event_kind,
                payload_sha256, status, target_uuid
            ) VALUES (
                %s, gen_random_uuid(), %s, %s, 'message.upsert',
                repeat('0', 64), 'applied', gen_random_uuid()
            )
            """,
            (
                bridge_uuids[target_account_uuid],
                target_account_uuid,
                api.project_id,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source, provider_uuid,
                external_account_uuid, provider_external_id,
                provider_metadata, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s::jsonb, 'zulip', %s::jsonb, %s,
                %s, %s, %s::jsonb, NOW(), NOW()
            )
            """,
            (
                message_uuid,
                api.project_id,
                stream["uuid"],
                stream["default_topic_uuid"],
                api.user_uuid,
                json.dumps({"kind": "markdown", "content": "shared message"}),
                json.dumps({"kind": "zulip", "message_id": provider_message_id}),
                bridge_uuids[source_account_uuid],
                source_account_uuid,
                provider_message_id,
                json.dumps(
                    {
                        "kind": "zulip",
                        "account_uuid": str(source_account_uuid),
                        "external_id": provider_message_id,
                        "provider_realm_uuid": str(provider_realm_uuid),
                        "capabilities": {},
                    }
                ),
            ),
        )
    db.commit()

    with contexts.Context().session_manager() as session:
        bindings = provider_data._delivered_provider_read_page_bindings(
            session,
            external_account_uuid=target_account_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            message_uuids=[message_uuid],
        )
        target_echo_count = session.execute(
            """
            SELECT count(*)
            FROM m_external_provider_events_v1
            WHERE project_id = %s AND external_account_uuid = %s
              AND event_kind = 'message.upsert'
              AND target_uuid = %s
            """,
            (api.project_id, target_account_uuid, message_uuid),
        ).fetchone()["count"]

    assert target_echo_count == 0
    assert bindings == [(message_uuid, provider_message_id)]


def test_provider_read_state_resolves_v2_native_placement_uuid(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "V2 provider read state",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    _drain()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (project_id, mode)
            VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = 'compact'
            """,
            (api.project_id,),
        )
    db.commit()
    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "v2 provider unread"},
        },
    )
    assert message_response.status_code == 201, message_response.text
    message_uuid = sys_uuid.UUID(message_response.json()["uuid"])
    _drain()

    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM messenger_user_message_states
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, message_uuid, peer_uuid),
        )
        cursor.execute(
            """
            DELETE FROM messenger_user_message_bindings
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, message_uuid, peer_uuid),
        )
        cursor.execute(
            """
            UPDATE messenger_message_placements
            SET legacy_public_uuid = NULL
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
    db.commit()

    with contexts.Context().session_manager() as session:
        provider_event_apply._sync_provider_read_state(
            session,
            sys_uuid.UUID(api.project_id),
            peer_uuid,
            sys_uuid.UUID(stream["uuid"]),
            sys_uuid.UUID(stream["default_topic_uuid"]),
            [message_uuid],
            True,
        )

    with contexts.Context().session_manager() as session:
        provider_event_apply._sync_provider_read_state(
            session,
            sys_uuid.UUID(api.project_id),
            sys_uuid.UUID(api.user_uuid),
            sys_uuid.UUID(stream["uuid"]),
            sys_uuid.UUID(stream["default_topic_uuid"]),
            [message_uuid],
            False,
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT placement.legacy_public_uuid,
                   state.read_at IS NOT NULL,
                   COALESCE(
                       get_bit(
                           chunk.read_bits,
                           (legacy.ingest_sequence %% 4096)::integer
                       ),
                       0
                   )
            FROM messenger_message_placements AS placement
            JOIN messenger_user_message_states AS state
              ON state.project_id = placement.project_id
             AND state.placement_uuid = placement.uuid
             AND state.user_uuid = %s
            JOIN m_workspace_messages AS legacy
              ON legacy.project_id = placement.project_id
             AND legacy.uuid = placement.uuid
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = state.user_uuid
             AND chunk.chunk_number = legacy.ingest_sequence / 4096
            WHERE placement.project_id = %s AND placement.uuid = %s
            """,
            (api.user_uuid, api.project_id, message_uuid),
        )
        assert cursor.fetchone() == (None, False, 0)
        cursor.execute(
            """
            SELECT binding.membership_generation,
                   state.membership_generation,
                   state.read_at IS NOT NULL
            FROM messenger_user_message_bindings AS binding
            JOIN messenger_user_message_states AS state
              ON state.project_id = binding.project_id
             AND state.placement_uuid = binding.placement_uuid
             AND state.user_uuid = binding.user_uuid
            WHERE binding.project_id = %s
              AND binding.placement_uuid = %s
              AND binding.user_uuid = %s
            """,
            (api.project_id, message_uuid, peer_uuid),
        )
        assert cursor.fetchone() == (1, 1, True)


def test_v2_provider_bulk_reads_use_exact_lazy_snapshots(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Provider v2 read snapshots",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        api.user_uuid,
        stream["uuid"],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (project_id, mode)
            VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = 'compact'
            """,
            (api.project_id,),
        )
    db.commit()
    topics = []
    for name in ("Read topic A", "Read topic B"):
        response = api.post(
            STREAM_TOPICS,
            json={
                "stream_uuid": stream["uuid"],
                "name": name,
                "source": {"kind": "native"},
            },
        )
        assert response.status_code == 201, response.text
        topics.append(response.json())
    messages = []
    for index, topic in enumerate((topics[0], topics[0], topics[1])):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": topic["uuid"],
                "payload": {"kind": "markdown", "content": f"message {index}"},
            },
        )
        assert response.status_code == 201, response.text
        messages.append(response.json())

    def mark_unread(*message_uuids):
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_user_message_states
                SET read_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND user_uuid = %s
                  AND placement_uuid = ANY(%s::uuid[])
                """,
                (api.project_id, api.user_uuid, list(message_uuids)),
            )
            cursor.execute(
                """
                UPDATE m_workspace_user_read_chunks_v1 AS chunk
                SET read_bits = set_bit(
                        chunk.read_bits,
                        (message.ingest_sequence %% 4096)::integer,
                        0
                    ),
                    updated_at = NOW()
                FROM m_workspace_messages AS message
                WHERE message.project_id = %s
                  AND message.uuid = ANY(%s::uuid[])
                  AND chunk.user_uuid = %s
                  AND chunk.chunk_number = message.ingest_sequence / 4096
                """,
                (api.project_id, list(message_uuids), api.user_uuid),
            )
        db.commit()

    mark_unread(messages[0]["uuid"], messages[1]["uuid"])
    read_up_to = api.post(
        f"{MESSAGES}{messages[1]['uuid']}/actions/read_up_to/invoke",
    )
    assert read_up_to.status_code == 200, read_up_to.text
    repeated = api.post(
        f"{MESSAGES}{messages[1]['uuid']}/actions/read_up_to/invoke",
    )
    assert repeated.status_code == 200, repeated.text

    mark_unread(messages[0]["uuid"])
    topic_read = api.post(
        f"{STREAM_TOPICS}{topics[0]['uuid']}/actions/read/invoke",
    )
    assert topic_read.status_code == 200, topic_read.text

    mark_unread(messages[2]["uuid"])
    stream_read = api.post(
        f"{STREAMS}{stream['uuid']}/actions/read/invoke",
    )
    assert stream_read.status_code == 200, stream_read.text

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT operation.uuid, operation.target_type,
                   COALESCE(SUM(candidate.candidate_count), 0)
            FROM m_external_operations_v2 AS operation
            LEFT JOIN m_external_provider_read_candidate_packs_v1 AS candidate
              ON candidate.external_operation_uuid = operation.uuid
            WHERE operation.external_account_uuid = %s
              AND operation.action = 'read_state.set'
            GROUP BY operation.uuid, operation.target_type, operation.created_at
            ORDER BY operation.created_at, operation.uuid
            """,
            (account_uuid,),
        )
        snapshots = cursor.fetchall()
        cursor.execute(
            """
            SELECT count(*)
            FROM m_workspace_messages AS message
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = %s
             AND chunk.chunk_number = message.ingest_sequence / 4096
            WHERE message.project_id = %s
              AND message.uuid = ANY(%s::uuid[])
              AND COALESCE(
                  get_bit(
                      chunk.read_bits,
                      (message.ingest_sequence %% 4096)::integer
                  ),
                  0
              ) = 1
            """,
            (
                api.user_uuid,
                api.project_id,
                [message["uuid"] for message in messages],
            ),
        )
        assert cursor.fetchone() == (3,)
    assert [(row[1], row[2]) for row in snapshots] == [
        ("message", 2),
        ("message", 2),
        ("topic", 2),
        ("stream", 3),
    ]


def test_native_v2_cutover_preserves_v1_http_and_event_contract(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)

    response = api.post(
        STREAMS,
        json={
            "name": "Native v2",
            "description": "Canonical native Messenger",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert response.status_code == 201, response.text
    stream = response.json()
    assert stream["source"] == {"kind": "native"}
    assert _drain() >= 3

    response = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["user_uuid"] == str(peer_uuid)
    assert _drain() >= 3

    response = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Architecture",
            "source": {"kind": "native"},
        },
    )
    assert response.status_code == 201, response.text
    topic = response.json()
    assert _drain() >= 1

    canonical_uuid = sys_uuid.uuid4()
    response = api.post(
        MESSAGES,
        json={
            "uuid": str(canonical_uuid),
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {
                "kind": "markdown",
                "content": f"Hello [peer](urn:user:{peer_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    expected_public_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(topic["uuid"]),
        str(canonical_uuid),
    )
    assert message["uuid"] == str(expected_public_uuid)
    assert message["read"] is True

    pending_peer = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert pending_peer.status_code == 404, pending_peer.text
    assert _drain() >= 2
    peer_message = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert peer_message.status_code == 200, peer_message.text
    assert peer_message.json()["mentioned"] is True
    assert peer_message.json()["read"] is False

    reaction_uuid = sys_uuid.uuid4()
    response = api.post(
        MESSAGE_REACTIONS,
        json={
            "uuid": str(reaction_uuid),
            "message_uuid": message["uuid"],
            "emoji_name": "thumbs_up",
        },
    )
    assert response.status_code == 201, response.text
    assert _drain() >= 1
    reloaded = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["reactions"] == {"thumbs_up": 1}

    response = api.post(
        f"{MESSAGES}{message['uuid']}/actions/read/invoke",
        user=peer_uuid,
    )
    assert response.status_code == 200, response.text
    assert response.json()["read"] is True
    assert _drain() >= 1

    response = api.put(
        f"{MESSAGES}{message['uuid']}",
        json={"payload": {"kind": "markdown", "content": "Edited"}},
    )
    assert response.status_code == 200, response.text
    assert _drain() >= 2
    assert (
        api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).json()["payload"][
            "content"
        ]
        == "Edited"
    )

    events = workspace_api.get(EVENTS, params={"page_limit": 100}, user=peer_uuid)
    assert events.status_code == 200, events.text
    kinds = [event["payload"]["kind"] for event in events.json()]
    assert "stream.created" in kinds
    assert "topic.created" in kinds
    assert "message.created" in kinds
    assert "message.updated" in kinds
    assert "message.read" in kinds

    response = api.delete(f"{MESSAGES}{message['uuid']}")
    assert response.status_code == 204, response.text
    assert _drain() >= 1
    assert api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).status_code == 404

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM messenger_projection_tasks
            WHERE project_id = %s AND status <> 'completed'
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*) FROM messenger_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, canonical_uuid),
        )
        assert cursor.fetchone()[0] == 0


def test_native_v2_deleted_message_supersedes_pending_projection_work(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Deleted pending projection",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    assert _drain() >= 1

    topic_response = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Delete before fanout",
            "source": {"kind": "native"},
        },
    )
    assert topic_response.status_code == 201, topic_response.text
    topic = topic_response.json()
    assert _drain() >= 1

    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "payload": {"kind": "markdown", "content": "create"},
        },
    )
    assert message_response.status_code == 201, message_response.text
    message_uuid = message_response.json()["uuid"]
    updated = api.put(
        f"{MESSAGES}{message_uuid}",
        json={"payload": {"kind": "markdown", "content": "updated"}},
    )
    assert updated.status_code == 200, updated.text
    deleted = api.delete(f"{MESSAGES}{message_uuid}")
    assert deleted.status_code == 204, deleted.text

    assert _drain() >= 3
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND status <> 'completed'
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == 0


def test_native_v2_direct_stream_is_idempotent_and_closed(api, db):
    peer_uuid = sys_uuid.uuid4()
    third_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    conftest.seed_workspace_user(db, third_uuid, f"user-{third_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    _register_project_user(db, api.project_id, third_uuid)

    body = {
        "name": "Direct",
        "description": "",
        "source_name": "native",
        "source": {"kind": "native"},
        "direct_user_uuid": str(peer_uuid),
    }
    first = api.post(STREAMS, json=body)
    assert first.status_code == 201, first.text
    second = api.post(STREAMS, json=body)
    assert second.status_code in (200, 201), second.text
    assert second.json()["uuid"] == first.json()["uuid"]

    reciprocal = api.post(
        STREAMS,
        user=peer_uuid,
        json={**body, "direct_user_uuid": str(api.user_uuid)},
    )
    assert reciprocal.status_code in (200, 201), reciprocal.text
    assert reciprocal.json()["uuid"] == first.json()["uuid"]
    assert reciprocal.json()["direct_user_uuid"] == str(api.user_uuid)

    rejected = api.post(
        f"{STREAMS}{first.json()['uuid']}/actions/add_users/invoke",
        json={"member": [str(third_uuid)]},
    )
    assert rejected.status_code == 400, rejected.text

    group_uuid = sys_uuid.uuid4()
    group_body = {
        "uuid": str(group_uuid),
        "name": "Private group",
        "description": "",
        "private": True,
        "source_name": "native",
        "source": {"kind": "native"},
    }
    first_group = api.post(STREAMS, json=group_body)
    retry_group = api.post(STREAMS, json=group_body)
    assert first_group.status_code == retry_group.status_code == 201
    assert first_group.json()["uuid"] == retry_group.json()["uuid"] == str(group_uuid)
    added_to_group = api.post(
        f"{STREAMS}{group_uuid}/actions/add_users/invoke",
        json={"member": [str(third_uuid)]},
    )
    assert added_to_group.status_code == 200, added_to_group.text


def test_native_v2_fanout_is_bounded_and_outbox_is_one_to_one(api, db):
    peers = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    for peer_uuid in peers:
        conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Bounded fanout",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid) for peer_uuid in peers]},
    )
    assert added.status_code == 200, added.text
    _drain()

    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "bounded"},
        },
    )
    assert message.status_code == 201, message.text
    with contexts.Context().session_manager() as session:
        processed = v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
            fanout_batch_size=2,
        )
    assert processed >= 3

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM messenger_api_user_messages_v1
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message.json()["uuid"]),
        )
        assert cursor.fetchone()[0] == 3
        cursor.execute(
            """
            SELECT count(*), min(batch_size), max(batch_size)
            FROM messenger_fanout_batch_tasks
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == (2, 1, 2)
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_domain_outbox_events
                 WHERE project_id = %s),
                (SELECT count(*) FROM messenger_projection_tasks
                 WHERE project_id = %s),
                (SELECT count(DISTINCT outbox_event_uuid)
                 FROM messenger_projection_tasks WHERE project_id = %s)
            """,
            (api.project_id, api.project_id, api.project_id),
        )
        outbox_count, task_count, distinct_task_count = cursor.fetchone()
    assert outbox_count == task_count == distinct_task_count


def test_native_v2_membership_generation_fences_fanout_and_rebuilds_history(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Membership generations",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    historical = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "before membership"},
        },
    ).json()
    _drain()

    added = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert added.status_code == 200, added.text
    binding_uuid = added.json()[0]["uuid"]
    assert api.get(f"{MESSAGES}{historical['uuid']}", user=peer_uuid).status_code == 404
    _drain()
    peer_historical = api.get(f"{MESSAGES}{historical['uuid']}", user=peer_uuid)
    assert peer_historical.status_code == 200, peer_historical.text
    assert peer_historical.json()["read"] is True

    starred = api.post(
        f"{MESSAGES}{historical['uuid']}/actions/star/invoke",
        user=peer_uuid,
    )
    assert starred.status_code == 200, starred.text
    assert starred.json()["starred"] is True

    delivered_while_member = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "old generation event"},
        },
    ).json()
    _drain()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
              AND object_type = 'message' AND action = 'created'
              AND payload->>'uuid' = %s
            """,
            (api.project_id, peer_uuid, delivered_while_member["uuid"]),
        )
        old_generation_event_uuid = cursor.fetchone()[0]

    sent_while_member = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "fenced fanout"},
        },
    ).json()
    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    _drain()
    assert api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).status_code == 404
    assert (
        api.get(f"{MESSAGES}{sent_while_member['uuid']}", user=peer_uuid).status_code
        == 404
    )

    readded = api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    assert readded.status_code == 200, readded.text
    assert readded.json()[0]["uuid"] == binding_uuid
    _drain()
    for message_uuid in (
        historical["uuid"],
        delivered_while_member["uuid"],
        sent_while_member["uuid"],
    ):
        reloaded = api.get(f"{MESSAGES}{message_uuid}", user=peer_uuid)
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["read"] is True
        assert reloaded.json()["starred"] is False
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT active, membership_generation
            FROM messenger_stream_bindings
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, binding_uuid),
        )
        assert cursor.fetchone() == (True, 3)
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM m_workspace_visible_events
                 WHERE uuid = %s AND user_uuid = %s),
                (SELECT membership_generation
                 FROM messenger_event_membership_guards
                 WHERE event_uuid = %s AND user_uuid = %s)
            """,
            (
                old_generation_event_uuid,
                peer_uuid,
                old_generation_event_uuid,
                peer_uuid,
            ),
        )
        assert cursor.fetchone() == (0, 1)
        cursor.execute(
            """
            DELETE FROM m_workspace_broadcast_message_events_v1
            WHERE uuid = %s
            """,
            (old_generation_event_uuid,),
        )
        cursor.execute(
            """
            SELECT count(*) FROM messenger_event_membership_guards
            WHERE event_uuid = %s
            """,
            (old_generation_event_uuid,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_user_message_bindings AS binding
            JOIN messenger_user_message_states AS state
              ON state.project_id = binding.project_id
             AND state.placement_uuid = binding.placement_uuid
             AND state.user_uuid = binding.user_uuid
            WHERE binding.project_id = %s AND binding.user_uuid = %s
              AND binding.membership_generation = 3
              AND state.membership_generation = 3
            """,
            (api.project_id, peer_uuid),
        )
        assert cursor.fetchone()[0] == 3


def test_native_v2_canonical_message_supports_multiple_placements(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Canonical placements",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    first_topic = api.post(
        STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "First"},
    ).json()
    second_topic = api.post(
        STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Second"},
    ).json()
    _drain()

    canonical_uuid = sys_uuid.uuid4()
    message_body = {
        "uuid": str(canonical_uuid),
        "stream_uuid": stream["uuid"],
        "payload": {"kind": "markdown", "content": "shared content"},
    }
    first = api.post(
        MESSAGES,
        json={**message_body, "topic_uuid": first_topic["uuid"]},
    )
    second = api.post(
        MESSAGES,
        json={**message_body, "topic_uuid": second_topic["uuid"]},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["uuid"] != second.json()["uuid"]
    assert first.json()["uuid"] == str(
        sys_uuid.uuid5(sys_uuid.UUID(first_topic["uuid"]), str(canonical_uuid))
    )
    assert second.json()["uuid"] == str(
        sys_uuid.uuid5(sys_uuid.UUID(second_topic["uuid"]), str(canonical_uuid))
    )
    _drain()

    reaction = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": first.json()["uuid"],
            "emoji_name": "rocket",
        },
    )
    assert reaction.status_code == 201, reaction.text
    _drain()
    for placement_uuid in (first.json()["uuid"], second.json()["uuid"]):
        visible = api.get(f"{MESSAGES}{placement_uuid}", user=peer_uuid)
        assert visible.status_code == 200, visible.text
        assert visible.json()["reactions"] == {"rocket": 1}

    updated = api.put(
        f"{MESSAGES}{first.json()['uuid']}",
        json={"payload": {"kind": "markdown", "content": "updated once"}},
    )
    assert updated.status_code == 200, updated.text
    _drain()
    assert (
        api.get(f"{MESSAGES}{second.json()['uuid']}", user=peer_uuid).json()["payload"][
            "content"
        ]
        == "updated once"
    )
    events = workspace_api.get(EVENTS, params={"page_limit": 500}, user=peer_uuid)
    updated_message_uuids = {
        event["payload"]["uuid"]
        for event in events.json()
        if event["payload"]["kind"] == "message.updated"
    }
    assert {first.json()["uuid"], second.json()["uuid"]} <= updated_message_uuids

    deleted = api.delete(f"{MESSAGES}{first.json()['uuid']}")
    assert deleted.status_code == 204, deleted.text
    _drain()
    assert api.get(f"{MESSAGES}{first.json()['uuid']}").status_code == 404
    assert api.get(f"{MESSAGES}{second.json()['uuid']}").status_code == 404
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_messages
                 WHERE project_id = %s AND uuid = %s),
                (SELECT count(*) FROM messenger_message_reaction_facts
                 WHERE project_id = %s AND canonical_message_uuid = %s)
            """,
            (api.project_id, canonical_uuid, api.project_id, canonical_uuid),
        )
        assert cursor.fetchone() == (0, 0)


def test_native_v2_actions_converge_counters_notifications_and_events(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Actions and counters",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    _drain()
    topic_uuid = stream["default_topic_uuid"]
    message_uuids = []
    for content in ("first", "second"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
        _drain()

    peer_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert (
        peer_topic["unread_count"],
        peer_topic["active_unread_count"],
        peer_topic["passive_unread_count"],
    ) == (2, 2, 0)
    assert (
        peer_stream["unread_count"],
        peer_stream["active_unread_count"],
        peer_stream["passive_unread_count"],
    ) == (2, 2, 0)

    muted = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "mute"},
    )
    assert muted.status_code == 200, muted.text
    _drain()
    peer_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
    peer_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
    assert (peer_topic["active_unread_count"], peer_topic["passive_unread_count"]) == (
        0,
        2,
    )
    assert (
        peer_stream["active_unread_count"],
        peer_stream["passive_unread_count"],
    ) == (0, 2)

    followed = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        user=peer_uuid,
        json={"notification_mode": "follow"},
    )
    assert followed.status_code == 200, followed.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()[
            "active_unread_count"
        ]
        == 2
    )

    read_up_to = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read_up_to/invoke",
        user=peer_uuid,
    )
    assert read_up_to.status_code == 200, read_up_to.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["unread_count"]
        == 1
    )
    read_topic = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=peer_uuid,
    )
    assert read_topic.status_code == 200, read_topic.text
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["unread_count"]
        == 0
    )
    assert (
        api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()["unread_count"]
        == 0
    )

    toggled = api.post(f"{STREAM_TOPICS}{topic_uuid}/actions/toggle_done/invoke")
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["is_done"] is True
    _drain()
    assert (
        api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()["is_done"]
        is True
    )
    archived = api.post(f"{STREAMS}{stream['uuid']}/actions/archive/invoke")
    assert archived.status_code == 200, archived.text
    assert archived.json()["is_archived"] is True
    _drain()
    unarchived = api.post(f"{STREAMS}{stream['uuid']}/actions/unarchive/invoke")
    assert unarchived.status_code == 200, unarchived.text
    _drain()

    events = workspace_api.get(EVENTS, params={"page_limit": 500}, user=peer_uuid)
    kinds = [event["payload"]["kind"] for event in events.json()]
    assert "message.read" in kinds
    assert "topic.updated" in kinds
    assert "stream.updated" in kinds
    assert "folder.updated" in kinds


def test_native_v2_idempotent_reads_repair_stale_counters(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Idempotent read repair",
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
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "read repair"},
        },
    )
    assert message.status_code == 201, message.text
    message_uuid = message.json()["uuid"]
    _drain()

    initial_read = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=peer_uuid,
    )
    assert initial_read.status_code == 200, initial_read.text
    _drain()

    def corrupt_ready_counters():
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count=7, active_unread_count=7,
                    passive_unread_count=0
                WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s
                """,
                (api.project_id, stream["uuid"], peer_uuid),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count=7, active_unread_count=7,
                    passive_unread_count=0
                WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s
                """,
                (api.project_id, topic_uuid, peer_uuid),
            )
        db.commit()

    actions = (
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        f"{MESSAGES}{message_uuid}/actions/read_up_to/invoke",
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        f"{STREAMS}{stream['uuid']}/actions/read/invoke",
    )
    expected_source_kinds = (
        "message.read",
        "messages.read",
        "topic.read",
        "stream.read",
    )
    for path, source_kind in zip(actions, expected_source_kinds, strict=True):
        corrupt_ready_counters()
        repaired = api.post(path, user=peer_uuid)
        assert repaired.status_code == 200, repaired.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                FROM messenger_domain_outbox_events
                WHERE project_id=%s
                  AND event_kind='read_counters'
                  AND payload->>'source_kind'=%s
                  AND payload->>'user_uuid'=%s
                """,
                (api.project_id, source_kind, str(peer_uuid)),
            )
            assert cursor.fetchone()[0] >= 2
        _drain()
        topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=peer_uuid).json()
        current_stream = api.get(f"{STREAMS}{stream['uuid']}", user=peer_uuid).json()
        assert topic["unread_count"] == 0
        assert current_stream["unread_count"] == 0


def test_projection_claim_bounds_interactive_read_priority(
    api,
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Interactive read claim priority",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    old_event_uuid = sys_uuid.uuid4()
    read_event_uuid = sys_uuid.uuid4()
    now = datetime.datetime.now(datetime.UTC)
    background_created_at = now - datetime.timedelta(minutes=10)
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                (
                    old_event_uuid,
                    api.project_id,
                    "topic_state_projection",
                    "topic",
                    f"{api.project_id}:{stream['default_topic_uuid']}",
                    json.dumps(
                        {
                            "source_kind": "topic.updated",
                            "topic_uuid": stream["default_topic_uuid"],
                        }
                    ),
                    background_created_at,
                    background_created_at,
                ),
                (
                    read_event_uuid,
                    api.project_id,
                    "read_counters",
                    "user-topic",
                    f"{api.project_id}:{api.user_uuid}:{stream['default_topic_uuid']}",
                    json.dumps(
                        {
                            "source_kind": "topic.read",
                            "user_uuid": str(api.user_uuid),
                            "stream_uuid": stream["uuid"],
                            "topic_uuid": stream["default_topic_uuid"],
                        }
                    ),
                    now,
                    now,
                ),
            ),
        )
    db.commit()
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 2
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                    WHEN outbox_event_uuid = %s THEN %s ELSE %s
                END,
                updated_at = NOW()
            WHERE outbox_event_uuid IN (%s, %s)
            """,
            (
                old_event_uuid,
                background_created_at,
                now,
                old_event_uuid,
                read_event_uuid,
            ),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE outbox_event_uuid NOT IN (%s, %s)
            """,
            (old_event_uuid, read_event_uuid),
        )
        claimed = v2_projection._claim_task(session, "integration:read-priority", 30)
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == read_event_uuid
        assert claimed["payload"]["source_kind"] == "topic.read"


@pytest.mark.parametrize("blocked_by", ("predecessor", "scope_lease"))
def test_projection_claim_ignores_unclaimable_overdue_tasks(
    api, blocked_by, monkeypatch
):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("interactive_read"),
    )
    streams = []
    for name in ("Blocked overdue scope", "Claimable background scope"):
        streams.append(
            api.post(
                STREAMS,
                json={
                    "name": name,
                    "description": "",
                    "source_name": "native",
                    "source": {"kind": "native"},
                },
            ).json()
        )
        _drain()

    now = datetime.datetime.now(datetime.UTC)
    blocked_event_uuid = sys_uuid.uuid4()
    background_event_uuid = sys_uuid.uuid4()
    read_event_uuid = sys_uuid.uuid4()
    predecessor_event_uuid = sys_uuid.uuid4() if blocked_by == "predecessor" else None
    blocked_stream, background_stream = streams
    events = []
    if predecessor_event_uuid is not None:
        events.append(
            (
                predecessor_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": blocked_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=42),
            )
        )
    events.extend(
        (
            (
                blocked_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": blocked_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=41),
            ),
            (
                background_event_uuid,
                "topic_state_projection",
                "topic",
                f"{api.project_id}:{background_stream['default_topic_uuid']}",
                {
                    "source_kind": "topic.updated",
                    "topic_uuid": background_stream["default_topic_uuid"],
                },
                now - datetime.timedelta(seconds=10),
            ),
            (
                read_event_uuid,
                "read_counters",
                "user-topic",
                (
                    f"{api.project_id}:{api.user_uuid}:"
                    f"{background_stream['default_topic_uuid']}"
                ),
                {
                    "source_kind": "topic.read",
                    "user_uuid": str(api.user_uuid),
                    "stream_uuid": background_stream["uuid"],
                    "topic_uuid": background_stream["default_topic_uuid"],
                },
                now,
            ),
        )
    )
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            )
            SELECT input.uuid, %s, input.event_kind, input.scope_kind,
                   input.scope_key, input.payload::jsonb,
                   input.created_at, input.created_at
            FROM unnest(
                %s::uuid[], %s::text[], %s::text[], %s::text[],
                %s::text[], %s::timestamptz[]
            ) AS input(
                uuid, event_kind, scope_kind, scope_key, payload, created_at
            )
            """,
            (
                api.project_id,
                [event[0] for event in events],
                [event[1] for event in events],
                [event[2] for event in events],
                [event[3] for event in events],
                [json.dumps(event[4]) for event in events],
                [event[5] for event in events],
            ),
        )
        assert v2_projection.derive_projection_tasks(session) == len(events)
        session.execute(
            """
            UPDATE messenger_projection_tasks AS task
            SET created_at = event.created_at, updated_at = NOW()
            FROM messenger_domain_outbox_events AS event
            WHERE task.project_id = %s
              AND event.project_id = task.project_id
              AND event.uuid = task.outbox_event_uuid
              AND event.uuid = ANY(%s::uuid[])
            """,
            (api.project_id, [event[0] for event in events]),
        )
        if predecessor_event_uuid is not None:
            session.execute(
                """
                UPDATE messenger_projection_tasks
                SET status = 'failed', next_retry_at = NOW() + interval '1 hour',
                    updated_at = NOW()
                WHERE project_id = %s AND outbox_event_uuid = %s
                """,
                (api.project_id, predecessor_event_uuid),
            )
        else:
            session.execute(
                """
                INSERT INTO messenger_projection_scope_leases (
                    uuid, project_id, scope_kind, scope_key, owner,
                    fencing_token, lease_expires_at
                ) VALUES (%s, %s, 'topic', %s, 'competing-worker', 1,
                          NOW() + interval '1 hour')
                ON CONFLICT (project_id, scope_kind, scope_key) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    fencing_token = messenger_projection_scope_leases.fencing_token + 1,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    updated_at = NOW()
                """,
                (
                    sys_uuid.uuid4(),
                    api.project_id,
                    f"{api.project_id}:{blocked_stream['default_topic_uuid']}",
                ),
            )

        claimed = v2_projection._claim_task(
            session,
            "integration:claimable-overdue",
            30,
        )
        assert claimed is not None
        assert claimed["outbox_event_uuid"] == read_event_uuid


def test_v2_idempotent_reads_survive_an_unavailable_provider_route(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Idempotent provider read",
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
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "idempotent read"},
        },
    ).json()
    _drain()
    initial_read = api.post(
        f"{MESSAGES}{message['uuid']}/actions/read/invoke",
        user=peer_uuid,
    )
    assert initial_read.status_code == 200, initial_read.text
    _drain()

    account_uuid = _seed_v2_provider_route(
        db,
        api.project_id,
        peer_uuid,
        stream["uuid"],
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2 AS bridge
            SET status = 'suspended', updated_at = NOW()
            FROM m_external_credentials_v2 AS credential
            WHERE credential.external_account_uuid = %s
              AND bridge.uuid::text = credential.envelope #>>
                  '{associated_data,bridge_instance_uuid}'
            """,
            (account_uuid,),
        )
        assert cursor.rowcount == 1
    db.commit()

    actions = (
        f"{MESSAGES}{message['uuid']}/actions/read/invoke",
        f"{MESSAGES}{message['uuid']}/actions/read_up_to/invoke",
        f"{STREAM_TOPICS}{stream['default_topic_uuid']}/actions/read/invoke",
        f"{STREAMS}{stream['uuid']}/actions/read/invoke",
    )
    for path in actions:
        response = api.post(path, user=peer_uuid)
        assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM m_external_operations_v2
            WHERE external_account_uuid = %s AND action = 'read_state.set'
            """,
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0


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


def test_provider_owner_read_repair_restores_missing_compact_state(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(PROVIDER_OWNER_READ_REPAIR_MIGRATION)
    try:
        owner_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        bridge_uuid = sys_uuid.uuid4()
        provider_event_uuids = [sys_uuid.uuid4(), sys_uuid.uuid4()]
        conftest.seed_workspace_user(db, owner_uuid, f"provider-owner-{owner_uuid}")
        _register_project_user(db, api.project_id, owner_uuid)
        stream = api.post(
            STREAMS,
            json={
                "name": "Provider owner repair",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        ).json()
        _drain()
        added = api.post(
            f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(owner_uuid)]},
        )
        assert added.status_code == 200, added.text
        _drain()
        message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {"kind": "markdown", "content": "provider unread"},
            },
        ).json()
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
                DELETE FROM messenger_user_message_states
                WHERE project_id = %s AND user_uuid = %s
                  AND placement_uuid = %s
                """,
                (api.project_id, owner_uuid, message["uuid"]),
            )
            cursor.execute(
                """
                DELETE FROM messenger_user_message_bindings
                WHERE project_id = %s AND user_uuid = %s
                  AND placement_uuid = %s
                """,
                (api.project_id, owner_uuid, message["uuid"]),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET source_name = 'zulip', user_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (owner_uuid, api.project_id, message["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_messages
                SET author_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (owner_uuid, api.project_id, message["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 99, active_unread_count = 99,
                    passive_unread_count = 0
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (api.project_id, owner_uuid, stream["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count = 99, active_unread_count = 99,
                    passive_unread_count = 0
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, owner_uuid, stream["default_topic_uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
                VALUES (%s, 'zulip')
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings
                ) VALUES (%s, %s, 'zulip', '{}'::jsonb)
                """,
                (account_uuid, owner_uuid),
            )
            cursor.executemany(
                """
                INSERT INTO m_external_provider_events_v1 (
                    bridge_instance_uuid, provider_event_uuid,
                    external_account_uuid, project_id, event_kind,
                    payload_sha256, status, target_uuid
                ) VALUES (
                    %s, %s, %s, %s, 'message.upsert',
                    repeat('0', 64), 'applied', %s
                )
                """,
                [
                    (
                        bridge_uuid,
                        provider_event_uuid,
                        account_uuid,
                        api.project_id,
                        message["uuid"],
                    )
                    for provider_event_uuid in provider_event_uuids
                ],
            )
        db.commit()

        engine.apply_migration(PROVIDER_OWNER_READ_REPAIR_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT binding.membership_generation,
                       state.membership_generation,
                       state.read_at IS NOT NULL
                FROM messenger_user_message_bindings AS binding
                JOIN messenger_user_message_states AS state
                  ON state.project_id = binding.project_id
                 AND state.placement_uuid = binding.placement_uuid
                 AND state.user_uuid = binding.user_uuid
                WHERE binding.project_id = %s
                  AND binding.user_uuid = %s
                  AND binding.placement_uuid = %s
                """,
                (api.project_id, owner_uuid, message["uuid"]),
            )
            assert cursor.fetchone() == (1, 1, False)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count,
                       passive_unread_count
                FROM messenger_stream_bindings
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (api.project_id, owner_uuid, stream["uuid"]),
            )
            assert cursor.fetchone() == (1, 1, 0)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count,
                       passive_unread_count
                FROM messenger_user_topic_bindings
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, owner_uuid, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (1, 1, 0)
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM m_workspace_user_message_flags
                WHERE project_id = %s AND user_uuid = %s AND uuid = %s
                """,
                (api.project_id, owner_uuid, message["uuid"]),
            )
            assert cursor.fetchone() == (0,)
    finally:
        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)
        _truncate_messenger_test_data()


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


def test_provider_group_dm_migrates_to_channel_and_repairs_participants(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION)
    engine.rollback_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)
    engine.rollback_migration(PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION)
    try:
        peer_read_uuid = sys_uuid.uuid4()
        peer_unread_uuid = sys_uuid.uuid4()
        bridge_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        chat_uuid = sys_uuid.uuid4()
        realm_uuid = sys_uuid.uuid4()
        event_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_read_uuid, "group-peer-read")
        conftest.seed_workspace_user(db, peer_unread_uuid, "group-peer-unread")
        _register_project_user(db, api.project_id, peer_read_uuid)
        _register_project_user(db, api.project_id, peer_unread_uuid)
        stream_response = api.post(
            STREAMS,
            json={
                "name": "group-peer-read, group-peer-unread",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        assert stream_response.status_code == 201, stream_response.text
        stream = stream_response.json()
        _drain()
        added = api.post(
            f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(peer_read_uuid), str(peer_unread_uuid)]},
        )
        assert added.status_code == 200, added.text
        _drain()
        message_response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {
                    "kind": "markdown",
                    "content": (
                        "provider group message "
                        f"[Unread peer](urn:user:{peer_unread_uuid})"
                    ),
                },
            },
        )
        assert message_response.status_code == 201, message_response.text
        message = message_response.json()
        _drain()

        participants = [
            {
                "identity_uuid": str(api.user_uuid),
                "display_name": "Cassandra",
                "role": "owner",
            },
            {
                "identity_uuid": str(peer_read_uuid),
                "display_name": "Read Peer",
                "role": "member",
            },
            {
                "identity_uuid": str(peer_unread_uuid),
                "display_name": "Unread Peer",
                "role": "member",
            },
        ]
        provider_source = {
            "kind": "zulip",
            "chat_type": "group",
            "provider_realm_uuid": str(realm_uuid),
            "participants": participants,
            "topics": [],
        }
        workspace_source = {
            "kind": "zulip",
            "stream_id": 0,
            "server_url": "https://provider.example.invalid",
            "source_scope": str(account_uuid),
        }
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_streams
                SET source_name = 'zulip', source = %s::jsonb,
                    private = TRUE, invite_only = TRUE,
                    direct_user_uuid = NULL, private_index = NULL,
                    provider = %s::jsonb, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    json.dumps(workspace_source),
                    json.dumps(
                        {
                            "kind": "zulip",
                            "account_uuid": str(account_uuid),
                            "external_id": "group_direct:8,9,10",
                        }
                    ),
                    api.project_id,
                    stream["uuid"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (
                    uuid, provider, identity_generation, status,
                    capabilities, last_heartbeat_at
                ) VALUES (%s, 'zulip', 1, 'active', '{}'::jsonb, NOW())
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    credential_present, status, live_ready,
                    provider_realm_uuid, provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', %s::jsonb,
                    TRUE, 'live', TRUE, %s, '8'
                )
                """,
                (
                    account_uuid,
                    api.user_uuid,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "server_url": "https://provider.example.invalid",
                            "default_project_id": api.project_id,
                        }
                    ),
                    realm_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET provider_uuid = %s, external_account_uuid = %s,
                    provider_external_id = 'group_direct:8,9,10'
                WHERE project_id = %s AND uuid = %s
                """,
                (bridge_uuid, account_uuid, api.project_id, stream["uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'group_direct:8,9,10', %s::jsonb,
                    'Read Peer, Unread Peer', TRUE, %s, %s, 'live'
                )
                """,
                (
                    chat_uuid,
                    account_uuid,
                    api.user_uuid,
                    json.dumps(provider_source),
                    api.project_id,
                    stream["uuid"],
                ),
            )
            cursor.execute(
                """
                DELETE FROM messenger_folder_items
                WHERE project_id = %s AND stream_uuid = %s
                  AND folder_uuid =
                      '00000000-0000-0000-0000-000000000002'::uuid
                """,
                (api.project_id, stream["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_folder_items
                SET chat_type = 'private', updated_at = NOW()
                WHERE project_id = %s AND stream_uuid = %s
                """,
                (api.project_id, stream["uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO messenger_folder_items (
                    uuid, project_id, user_uuid, folder_uuid, stream_uuid,
                    chat_type, automatic, created_at, updated_at
                )
                SELECT ('11' || substr(%s::uuid::text, 3))::uuid,
                       %s, binding.user_uuid,
                       '00000000-0000-0000-0000-000000000001'::uuid,
                       %s, 'private', TRUE, NOW(), NOW()
                FROM messenger_stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = %s AND binding.active
                ON CONFLICT (
                    project_id, user_uuid, folder_uuid, stream_uuid
                ) DO UPDATE SET
                    chat_type = 'private', automatic = TRUE, updated_at = NOW()
                """,
                (
                    stream["uuid"],
                    api.project_id,
                    stream["uuid"],
                    api.project_id,
                    stream["uuid"],
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET source_name = 'zulip', source = %s::jsonb,
                    provider_uuid = %s, external_account_uuid = %s,
                    provider_external_id = '101', updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                RETURNING ingest_sequence
                """,
                (
                    json.dumps({"kind": "zulip", "message_id": 101}),
                    bridge_uuid,
                    account_uuid,
                    api.project_id,
                    message["uuid"],
                ),
            )
            ingest_sequence = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode
                ) VALUES (%s, 'compact')
                ON CONFLICT (project_id) DO UPDATE SET
                    mode = 'compact', updated_at = NOW()
                """,
                (api.project_id,),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_user_read_chunks_v1 (
                    user_uuid, chunk_number, read_bits
                ) VALUES (
                    %s, %s,
                    set_bit(B'0'::bit(4096), %s, 1)
                )
                ON CONFLICT (user_uuid, chunk_number) DO UPDATE SET
                    read_bits = set_bit(
                        m_workspace_user_read_chunks_v1.read_bits, %s, 1
                    ),
                    updated_at = NOW()
                """,
                (
                    peer_read_uuid,
                    ingest_sequence // 4096,
                    ingest_sequence % 4096,
                    ingest_sequence % 4096,
                ),
            )
            cursor.execute(
                """
                DELETE FROM messenger_user_message_states
                WHERE project_id = %s AND placement_uuid = %s
                  AND user_uuid = ANY(%s::uuid[])
                """,
                (
                    api.project_id,
                    message["uuid"],
                    [peer_read_uuid, peer_unread_uuid],
                ),
            )
            cursor.execute(
                """
                DELETE FROM messenger_user_message_bindings
                WHERE project_id = %s AND placement_uuid = %s
                  AND user_uuid = ANY(%s::uuid[])
                """,
                (
                    api.project_id,
                    message["uuid"],
                    [peer_read_uuid, peer_unread_uuid],
                ),
            )
            cursor.execute(
                """
                UPDATE messenger_user_message_states
                SET read_at = NULL, updated_at = NOW()
                WHERE project_id = %s AND placement_uuid = %s
                  AND user_uuid = %s
                """,
                (api.project_id, message["uuid"], api.user_uuid),
            )
            cursor.execute(
                """
                UPDATE m_workspace_user_read_chunks_v1
                SET read_bits = set_bit(read_bits, %s, 0), updated_at = NOW()
                WHERE user_uuid = %s AND chunk_number = %s
                """,
                (
                    ingest_sequence % 4096,
                    api.user_uuid,
                    ingest_sequence // 4096,
                ),
            )
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 99, active_unread_count = 99,
                    passive_unread_count = 0
                WHERE project_id = %s AND stream_uuid = %s
                  AND user_uuid = ANY(%s::uuid[])
                """,
                (
                    api.project_id,
                    stream["uuid"],
                    [peer_read_uuid, peer_unread_uuid],
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_provider_events_v1 (
                    bridge_instance_uuid, provider_event_uuid,
                    external_account_uuid, project_id, event_kind,
                    payload_sha256, status, target_uuid
                ) VALUES (
                    %s, %s, %s, %s, 'message.upsert',
                    repeat('0', 64), 'applied', %s
                )
                """,
                (
                    bridge_uuid,
                    event_uuid,
                    account_uuid,
                    api.project_id,
                    message["uuid"],
                ),
            )
        db.commit()

        engine.apply_migration(PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT private, invite_only, direct_user_uuid,
                       provider->>'default_display_name'
                FROM messenger_streams
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, stream["uuid"]),
            )
            assert cursor.fetchone() == (
                False,
                True,
                None,
                "group-peer-read, group-peer-unread",
            )
            cursor.execute(
                """
                SELECT source, user_uuid, name FROM (
                    SELECT 'legacy' AS source, user_uuid, name
                    FROM m_workspace_user_streams
                    WHERE project_id = %s AND uuid = %s
                      AND user_uuid = ANY(%s::uuid[])
                    UNION ALL
                    SELECT 'canonical' AS source, user_uuid, name
                    FROM messenger_api_user_streams_v1
                    WHERE project_id = %s AND uuid = %s
                      AND user_uuid = ANY(%s::uuid[])
                ) AS labels
                ORDER BY source, user_uuid
                """,
                (
                    api.project_id,
                    stream["uuid"],
                    [sys_uuid.UUID(api.user_uuid), peer_read_uuid, peer_unread_uuid],
                    api.project_id,
                    stream["uuid"],
                    [sys_uuid.UUID(api.user_uuid), peer_read_uuid, peer_unread_uuid],
                ),
            )
            expected_labels = {
                sys_uuid.UUID(api.user_uuid): "Read Peer, Unread Peer",
                peer_read_uuid: "Cassandra, Unread Peer",
                peer_unread_uuid: "Cassandra, Read Peer",
            }
            assert cursor.fetchall() == sorted(
                [
                    (source, user_uuid, label)
                    for source in ("canonical", "legacy")
                    for user_uuid, label in expected_labels.items()
                ],
                key=lambda row: (row[0], row[1]),
            )
            cursor.execute(
                """
                SELECT binding.user_uuid, item.folder_uuid, item.chat_type
                FROM messenger_stream_bindings AS binding
                JOIN messenger_folder_items AS item
                  ON item.project_id = binding.project_id
                 AND item.user_uuid = binding.user_uuid
                 AND item.stream_uuid = binding.stream_uuid
                WHERE binding.project_id = %s AND binding.stream_uuid = %s
                  AND item.folder_uuid IN (
                      '00000000-0000-0000-0000-000000000001'::uuid,
                      '00000000-0000-0000-0000-000000000002'::uuid
                  )
                ORDER BY binding.user_uuid, item.folder_uuid
                """,
                (api.project_id, stream["uuid"]),
            )
            assert cursor.fetchall() == [
                (
                    user_uuid,
                    sys_uuid.UUID("00000000-0000-0000-0000-000000000002"),
                    "stream",
                )
                for user_uuid in sorted(
                    [
                        sys_uuid.UUID(api.user_uuid),
                        peer_read_uuid,
                        peer_unread_uuid,
                    ]
                )
            ]

        renamed = api.put(
            f"{STREAMS}{stream['uuid']}",
            json={"name": "Local group channel name"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "Local group channel name"
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT array_agg(DISTINCT name ORDER BY name)
                FROM m_workspace_user_streams
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, stream["uuid"]),
            )
            assert cursor.fetchone() == (["Local group channel name"],)

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_message_placements
                SET legacy_public_uuid = NULL
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, message["uuid"]),
            )
        db.commit()

        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)
        engine.apply_migration(PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT array_agg(DISTINCT name ORDER BY name)
                FROM messenger_api_user_streams_v1
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, stream["uuid"]),
            )
            assert cursor.fetchone() == (["Local group channel name"],)
            cursor.execute(
                """
                SELECT binding.user_uuid, binding.relation_role,
                       state.read_at IS NOT NULL, state.mentioned
                FROM messenger_user_message_bindings AS binding
                JOIN messenger_user_message_states AS state
                  ON state.project_id = binding.project_id
                 AND state.placement_uuid = binding.placement_uuid
                 AND state.user_uuid = binding.user_uuid
                WHERE binding.project_id = %s
                  AND binding.placement_uuid = %s
                ORDER BY binding.user_uuid
                """,
                (api.project_id, message["uuid"]),
            )
            assert cursor.fetchall() == sorted(
                [
                    (sys_uuid.UUID(api.user_uuid), "author", False, False),
                    (peer_read_uuid, "member", True, False),
                    (peer_unread_uuid, "member", False, True),
                ],
                key=lambda row: row[0],
            )
            cursor.execute(
                """
                SELECT binding.user_uuid, binding.unread_count
                FROM messenger_stream_bindings AS binding
                WHERE binding.project_id = %s AND binding.stream_uuid = %s
                  AND binding.user_uuid = ANY(%s::uuid[])
                ORDER BY binding.user_uuid
                """,
                (
                    api.project_id,
                    stream["uuid"],
                    [peer_read_uuid, peer_unread_uuid],
                ),
            )
            assert cursor.fetchall() == sorted(
                [(peer_read_uuid, 0), (peer_unread_uuid, 1)],
                key=lambda row: row[0],
            )
            cursor.execute(
                """
                SELECT user_uuid,
                       get_bit(read_bits, %s) AS read_bit
                FROM m_workspace_user_read_chunks_v1
                WHERE chunk_number = %s
                  AND user_uuid = ANY(%s::uuid[])
                ORDER BY user_uuid
                """,
                (
                    ingest_sequence % 4096,
                    ingest_sequence // 4096,
                    [
                        sys_uuid.UUID(api.user_uuid),
                        peer_read_uuid,
                        peer_unread_uuid,
                    ],
                ),
            )
            assert cursor.fetchall() == sorted(
                [
                    (peer_read_uuid, 1),
                ],
                key=lambda row: row[0],
            )
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)
        engine.apply_migration(PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION)


def test_provider_personal_chat_owner_uses_peer_label_without_owner_participant(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(PROVIDER_CHAT_OWNER_LABEL_MIGRATION)
    try:
        peer_uuid = sys_uuid.uuid4()
        bridge_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        chat_uuid = sys_uuid.uuid4()
        realm_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_uuid, "local-peer")
        _register_project_user(db, api.project_id, peer_uuid)
        response = api.post(
            STREAMS,
            json={
                "name": "local-peer",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
                "direct_user_uuid": str(peer_uuid),
            },
        )
        assert response.status_code == 201, response.text
        stream = response.json()
        _drain()

        provider_source = {
            "kind": "zulip",
            "chat_type": "personal",
            "provider_realm_uuid": str(realm_uuid),
            # Zulip DM catalog rows can contain only the peer. The selected
            # account owner remains authoritative even when omitted here.
            "participants": [
                {
                    "identity_uuid": str(peer_uuid),
                    "display_name": "Provider Peer",
                    "role": "member",
                }
            ],
            "topics": [],
        }
        workspace_source = {
            "kind": "zulip",
            "stream_id": 0,
            "server_url": "https://provider.example.invalid",
            "source_scope": str(account_uuid),
        }
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_streams
                SET source_name = 'zulip', source = %s::jsonb,
                    provider = %s::jsonb, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    json.dumps(workspace_source),
                    json.dumps(
                        {
                            "kind": "zulip",
                            "account_uuid": str(account_uuid),
                            "external_id": "direct:8,9",
                        }
                    ),
                    api.project_id,
                    stream["uuid"],
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (
                    uuid, provider, identity_generation, status,
                    capabilities, last_heartbeat_at
                ) VALUES (%s, 'zulip', 1, 'active', '{}'::jsonb, NOW())
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    credential_present, status, live_ready,
                    provider_realm_uuid, provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', %s::jsonb,
                    TRUE, 'live', TRUE, %s, '8'
                )
                """,
                (
                    account_uuid,
                    api.user_uuid,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "server_url": "https://provider.example.invalid",
                            "default_project_id": api.project_id,
                        }
                    ),
                    realm_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET provider_uuid = %s, external_account_uuid = %s,
                    provider_external_id = 'direct:8,9'
                WHERE project_id = %s AND uuid = %s
                """,
                (bridge_uuid, account_uuid, api.project_id, stream["uuid"]),
            )
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'direct:8,9', %s::jsonb,
                    'Provider Peer', TRUE, %s, %s, 'live'
                )
                """,
                (
                    chat_uuid,
                    account_uuid,
                    api.user_uuid,
                    json.dumps(provider_source),
                    api.project_id,
                    stream["uuid"],
                ),
            )
        db.commit()

        engine.apply_migration(PROVIDER_CHAT_OWNER_LABEL_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT source, name, direct_user_uuid FROM (
                    SELECT 'legacy' AS source, name, direct_user_uuid
                    FROM m_workspace_user_streams
                    WHERE project_id = %s AND uuid = %s AND user_uuid = %s
                    UNION ALL
                    SELECT 'canonical' AS source, name, direct_user_uuid
                    FROM messenger_api_user_streams_v1
                    WHERE project_id = %s AND uuid = %s AND user_uuid = %s
                ) AS labels
                ORDER BY source
                """,
                (
                    api.project_id,
                    stream["uuid"],
                    api.user_uuid,
                    api.project_id,
                    stream["uuid"],
                    api.user_uuid,
                ),
            )
            assert cursor.fetchall() == [
                ("canonical", "Provider Peer", peer_uuid),
                ("legacy", "Provider Peer", peer_uuid),
            ]
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(PROVIDER_CHAT_OWNER_LABEL_MIGRATION)


def test_duplicate_provider_projection_repair_keeps_native_author(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION)
    try:
        peer_uuid = sys_uuid.uuid4()
        bridge_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        provider_uuid = sys_uuid.uuid4()
        provider_realm_uuid = sys_uuid.uuid4()
        projected_uuid = sys_uuid.uuid4()
        provider_message_id = "9137"
        conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)
        stream = api.post(
            STREAMS,
            json={
                "name": "Duplicate provider projection repair",
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
        native = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": stream["default_topic_uuid"],
                "payload": {
                    "kind": "markdown",
                    "content": "repair duplicate provider echo",
                },
            },
        )
        assert native.status_code == 201, native.text
        native_uuid = sys_uuid.UUID(native.json()["uuid"])
        _drain()

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT created_at
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, native_uuid),
            )
            native_created_at = cursor.fetchone()[0]
            cursor.execute(
                """
                UPDATE messenger_message_placements
                SET legacy_public_uuid = %s
                WHERE project_id = %s AND uuid = %s
                """,
                (native_uuid, api.project_id, native_uuid),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
                VALUES (%s, 'zulip')
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready, provider_realm_uuid,
                    provider_owner_user_id
                ) VALUES (%s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s, '1')
                """,
                (account_uuid, peer_uuid, provider_realm_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, provider_uuid,
                    external_account_uuid, provider_external_id,
                    provider_metadata, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"repair duplicate provider echo"}'::jsonb,
                    'zulip', %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s
                )
                """,
                (
                    projected_uuid,
                    api.project_id,
                    stream["uuid"],
                    stream["default_topic_uuid"],
                    peer_uuid,
                    json.dumps({"kind": "zulip", "message_id": provider_message_id}),
                    provider_uuid,
                    account_uuid,
                    provider_message_id,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "account_uuid": str(account_uuid),
                            "external_id": provider_message_id,
                            "provider_realm_uuid": str(provider_realm_uuid),
                            "capabilities": {},
                        }
                    ),
                    native_created_at + datetime.timedelta(seconds=1),
                    native_created_at + datetime.timedelta(seconds=1),
                ),
            )
            cursor.execute(
                """
                SELECT uuid
                FROM messenger_message_placements
                WHERE project_id = %s AND legacy_public_uuid = %s
                """,
                (api.project_id, projected_uuid),
            )
            projected_placement_uuid = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO messenger_user_message_states (
                    uuid, project_id, placement_uuid, user_uuid,
                    membership_generation, read_at, mentioned, starred, pinned,
                    created_at, updated_at
                )
                SELECT messenger_uuid_v5(%s, %s::text), %s, %s, %s,
                       binding.membership_generation, NOW(), TRUE, TRUE, FALSE,
                       NOW(), NOW()
                FROM messenger_stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.user_uuid = %s
                  AND binding.stream_uuid = %s
                ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
                    read_at = EXCLUDED.read_at,
                    mentioned = EXCLUDED.mentioned,
                    starred = EXCLUDED.starred,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    projected_placement_uuid,
                    api.user_uuid,
                    api.project_id,
                    projected_placement_uuid,
                    api.user_uuid,
                    api.project_id,
                    api.user_uuid,
                    stream["uuid"],
                ),
            )
            assert cursor.rowcount == 1
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 99, active_unread_count = 99,
                    passive_unread_count = 0
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (api.project_id, api.user_uuid, stream["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count = 99, active_unread_count = 99,
                    passive_unread_count = 0
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, api.user_uuid, stream["default_topic_uuid"]),
            )
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM messenger_message_placements
                        WHERE project_id = %s AND legacy_public_uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM messenger_message_placements
                        WHERE project_id = %s AND legacy_public_uuid = %s
                    )
                """,
                (
                    api.project_id,
                    native_uuid,
                    api.project_id,
                    native_uuid,
                    api.project_id,
                    projected_uuid,
                    api.project_id,
                    projected_uuid,
                ),
            )
            assert cursor.fetchone() == (True, True, True, True)
        db.commit()

        engine.apply_migration(DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT message.author_uuid, message.provider_realm_uuid,
                       message.provider_message_id,
                       EXISTS(
                           SELECT 1 FROM messenger_messages
                           WHERE project_id = %s
                             AND provider_realm_uuid = %s
                             AND provider_message_id = %s
                       ),
                       EXISTS(
                           SELECT 1 FROM m_workspace_messages
                           WHERE project_id = %s AND uuid = %s
                       )
                FROM messenger_messages AS message
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = message.project_id
                 AND placement.message_uuid = message.uuid
                WHERE placement.project_id = %s
                  AND placement.legacy_public_uuid = %s
                """,
                (
                    api.project_id,
                    provider_realm_uuid,
                    provider_message_id,
                    api.project_id,
                    projected_uuid,
                    api.project_id,
                    native_uuid,
                ),
            )
            assert cursor.fetchone() == (
                sys_uuid.UUID(api.user_uuid),
                provider_realm_uuid,
                provider_message_id,
                True,
                False,
            )
            cursor.execute(
                """
                SELECT read_at IS NOT NULL, mentioned, starred
                FROM messenger_user_message_states AS state
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = state.project_id
                 AND placement.uuid = state.placement_uuid
                WHERE state.project_id = %s AND state.user_uuid = %s
                  AND placement.legacy_public_uuid = %s
                """,
                (api.project_id, api.user_uuid, native_uuid),
            )
            assert cursor.fetchone() == (True, True, True)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count, passive_unread_count
                FROM messenger_stream_bindings
                WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
                """,
                (api.project_id, api.user_uuid, stream["uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
            cursor.execute(
                """
                SELECT unread_count, active_unread_count, passive_unread_count
                FROM messenger_user_topic_bindings
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, api.user_uuid, stream["default_topic_uuid"]),
            )
            assert cursor.fetchone() == (0, 0, 0)
    finally:
        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)
        _truncate_messenger_test_data()


def test_native_v2_keeps_folder_file_and_draft_contracts(api, db):
    peer_uuid = sys_uuid.uuid4()
    outsider_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    conftest.seed_workspace_user(db, outsider_uuid, f"user-{outsider_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Adjacent contracts",
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
    binding_uuid = added.json()[0]["uuid"]
    _drain()

    file_response = api.post(
        FILES,
        json={
            "stream_uuid": stream["uuid"],
            "name": "architecture.txt",
            "description": "Native v2",
            "content_type": "text/plain",
            "size_bytes": 12,
            "hash": "v2-hash",
        },
    )
    assert file_response.status_code == 201, file_response.text
    file_uuid = file_response.json()["uuid"]
    assert api.get(f"{FILES}{file_uuid}", user=peer_uuid).status_code == 200
    assert api.get(f"{FILES}{file_uuid}", user=outsider_uuid).status_code == 404

    system_item_uuid = f"00{stream['uuid'][2:]}"
    system_item = api.get(f"{FOLDER_ITEMS}{system_item_uuid}")
    assert system_item.status_code == 200, system_item.text
    system_pinned = api.post(f"{FOLDER_ITEMS}{system_item_uuid}/actions/pin/invoke")
    assert system_pinned.status_code == 200, system_pinned.text
    assert system_pinned.json()["pinned_at"] is not None
    _drain()
    all_chats = api.get(f"{FOLDERS}00000000-0000-0000-0000-000000000000")
    assert all_chats.status_code == 200, all_chats.text
    nested_system_item = next(
        value
        for value in all_chats.json()["folder_items"]
        if value["uuid"] == system_item_uuid
    )
    assert nested_system_item["pinned_at"] is not None
    system_unpinned = api.post(f"{FOLDER_ITEMS}{system_item_uuid}/actions/unpin/invoke")
    assert system_unpinned.status_code == 200, system_unpinned.text
    _drain()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM m_folder_items
            WHERE project_id = %s AND user_uuid = %s
              AND folder_uuid = %s AND stream_uuid = %s
            """,
            (
                api.project_id,
                api.user_uuid,
                "00000000-0000-0000-0000-000000000000",
                stream["uuid"],
            ),
        )
        assert cursor.fetchone()[0] == 0

    folder = api.post(FOLDERS, json={"title": "V2 review"})
    assert folder.status_code == 201, folder.text
    item = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder.json()["uuid"],
            "stream_uuid": stream["uuid"],
            "chat_type": "stream",
        },
    )
    assert item.status_code == 201, item.text
    pinned = api.post(f"{FOLDER_ITEMS}{item.json()['uuid']}/actions/pin/invoke")
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned_at"] is not None
    _drain()
    reloaded_folder = api.get(f"{FOLDERS}{folder.json()['uuid']}")
    assert reloaded_folder.status_code == 200, reloaded_folder.text
    nested_items = reloaded_folder.json()["folder_items"]
    assert len(nested_items) == 1
    assert nested_items[0]["uuid"] == item.json()["uuid"]
    assert nested_items[0]["folder_uuid"] == folder.json()["uuid"]
    assert nested_items[0]["stream_uuid"] == stream["uuid"]
    assert nested_items[0]["pinned_at"] is not None
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_user_folder_bindings
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT count(*) FROM messenger_folder_items
                 WHERE project_id = %s AND user_uuid = %s),
                (SELECT jsonb_array_length(folder_items_snapshot)
                 FROM messenger_user_folder_bindings
                 WHERE project_id = %s AND user_uuid = %s
                   AND folder_uuid = %s)
            """,
            (
                api.project_id,
                api.user_uuid,
                api.project_id,
                api.user_uuid,
                api.project_id,
                api.user_uuid,
                folder.json()["uuid"],
            ),
        )
        binding_count, item_count, snapshot_count = cursor.fetchone()
    assert binding_count >= 4
    assert item_count >= 3
    assert snapshot_count == 1

    draft_uuid = sys_uuid.uuid4()
    draft_body = {
        "uuid": str(draft_uuid),
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
        "payload": {"kind": "markdown", "content": "  native draft  "},
    }
    draft = api.post(DRAFTS, json=draft_body)
    assert draft.status_code == 201, draft.text
    assert draft.headers["ETag"] == '"1"'
    assert draft.json()["payload"]["content"] == "native draft"
    retry = api.post(DRAFTS, json=draft_body)
    assert retry.status_code == 200, retry.text
    updated = api.put(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"1"'},
        json={"payload": {"kind": "markdown", "content": "updated"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.headers["ETag"] == '"2"'

    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code == 204, removed.text
    _drain()
    assert api.get(f"{FILES}{file_uuid}", user=peer_uuid).status_code == 404
    deleted_draft = api.delete(f"{DRAFTS}{draft_uuid}", headers={"If-Match": '"2"'})
    assert deleted_draft.status_code == 204, deleted_draft.text


def test_native_v2_isolates_projects_and_rejects_non_members(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    other_project = sys_uuid.uuid4()
    other_user = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, other_user, f"user-{other_user}")
    _register_project_user(db, other_project, other_user)
    current_stream = api.post(
        STREAMS,
        json={
            "name": "Current project",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert current_stream.status_code == 201, current_stream.text
    rejected_member = api.post(
        f"{STREAMS}{current_stream.json()['uuid']}/actions/add_users/invoke",
        json={"member": [str(other_user)]},
    )
    assert rejected_member.status_code == 400, rejected_member.text
    rejected_direct = api.post(
        STREAMS,
        json={
            "name": "Cross-project direct",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
            "direct_user_uuid": str(other_user),
        },
    )
    assert rejected_direct.status_code == 400, rejected_direct.text
    other_stream = api.post(
        STREAMS,
        project=other_project,
        json={
            "name": "Other project",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert other_stream.status_code == 201, other_stream.text
    _drain()
    other_message = api.post(
        MESSAGES,
        project=other_project,
        json={
            "stream_uuid": other_stream.json()["uuid"],
            "topic_uuid": other_stream.json()["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "isolated"},
        },
    )
    assert other_message.status_code == 201, other_message.text
    _drain()

    assert api.get(f"{STREAMS}{other_stream.json()['uuid']}").status_code == 404
    assert api.get(f"{MESSAGES}{other_message.json()['uuid']}").status_code == 404
    assert (
        api.get(
            f"{MESSAGES}{other_message.json()['uuid']}", project=other_project
        ).status_code
        == 200
    )
    current_events = workspace_api.get(EVENTS, params={"page_limit": 500})
    assert current_events.status_code == 200, current_events.text
    assert all(event["project_id"] == api.project_id for event in current_events.json())


def test_native_v2_rolling_legacy_message_update_converges_mentions(api, db):
    peer_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    _register_project_user(db, api.project_id, peer_uuid)
    stream = api.post(
        STREAMS,
        json={
            "name": "Rolling legacy writer",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    api.post(
        f"{STREAMS}{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(peer_uuid)]},
    )
    _drain()
    canonical_uuid = sys_uuid.uuid4()
    message = api.post(
        MESSAGES,
        json={
            "uuid": str(canonical_uuid),
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {
                "kind": "markdown",
                "content": f"hello [peer](urn:user:{peer_uuid})",
            },
        },
    ).json()
    _drain()
    assert api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid).json()["mentioned"]

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT legacy.uuid
            FROM m_workspace_messages AS legacy
            JOIN messenger_message_placements AS placement
              ON placement.project_id = legacy.project_id
             AND legacy.uuid = COALESCE(
                    placement.legacy_public_uuid, placement.uuid
                 )
            WHERE placement.project_id = %s AND placement.uuid = %s
            """,
            (api.project_id, message["uuid"]),
        )
        legacy_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET payload = '{"kind":"markdown","content":"legacy edit"}'::jsonb,
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, legacy_uuid),
        )
    _drain()
    reloaded = api.get(f"{MESSAGES}{message['uuid']}", user=peer_uuid)
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["payload"]["content"] == "legacy edit"
    assert reloaded.json()["mentioned"] is False
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM messenger_messages
            WHERE project_id = %s AND uuid IN (%s, %s)
            """,
            (api.project_id, canonical_uuid, legacy_uuid),
        )
        assert cursor.fetchone()[0] == 1


def test_native_v2_scheduler_preserves_fifo_within_fanout_lane(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Newest first",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    first = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "older"},
        },
    ).json()
    second = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "newer"},
        },
    ).json()
    with contexts.Context().session_manager() as session:
        session.execute(
            """
            UPDATE messenger_domain_outbox_events
            SET payload = jsonb_set(
                payload,
                '{message_created_at}',
                to_jsonb(NOW() + interval '1 minute')
            )
            WHERE project_id = %s AND event_kind = 'fanout'
              AND payload->>'placement_uuid' = %s
            """,
            (api.project_id, second["uuid"]),
        )
        v2_projection.derive_projection_tasks(session)
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = NOW(), updated_at = NOW()
            WHERE project_id = %s
              AND payload->>'placement_uuid' IN (%s, %s)
            """,
            (api.project_id, first["uuid"], second["uuid"]),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE project_id <> %s
               OR COALESCE(payload->>'placement_uuid', '') NOT IN (%s, %s)
            """,
            (api.project_id, first["uuid"], second["uuid"]),
        )
        claimed = v2_projection._claim_task(session, "integration:fifo", 30)
        assert claimed["task_kind"] == "fanout"
        assert str(claimed["payload"]["placement_uuid"]) == first["uuid"]
        assert str(claimed["payload"]["placement_uuid"]) != second["uuid"]


def test_native_v2_scheduler_drains_aged_tasks_before_fresh_fanout(api, monkeypatch):
    monkeypatch.setattr(
        v2_projection,
        "_FAIR_SCHEDULER_CYCLE",
        itertools.repeat("fanout"),
    )
    stream = api.post(
        STREAMS,
        json={
            "name": "Aged before fresh",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    _drain()
    aged = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "aged"},
        },
    ).json()
    fresh = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "fresh"},
        },
    ).json()
    with contexts.Context().session_manager() as session:
        v2_projection.derive_projection_tasks(session)
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                    WHEN payload->>'placement_uuid' = %s
                    THEN NOW() - interval '10 seconds'
                    ELSE NOW()
                END,
                updated_at = NOW()
            WHERE project_id = %s
              AND payload->>'placement_uuid' IN (%s, %s)
            """,
            (aged["uuid"], api.project_id, aged["uuid"], fresh["uuid"]),
        )
        session.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'completed', updated_at = NOW()
            WHERE project_id <> %s
               OR COALESCE(payload->>'placement_uuid', '') NOT IN (%s, %s)
            """,
            (api.project_id, aged["uuid"], fresh["uuid"]),
        )
        claimed = v2_projection._claim_task(session, "integration:aged", 30)
        assert str(claimed["payload"]["placement_uuid"]) == aged["uuid"]


def test_native_v2_migration_rewrites_and_rolls_back_legacy_message_identity(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    audience_uuid = None
    api_store.configure_store_factory(
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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


def test_native_v2_migration_rejects_ambiguous_zulip_provenance(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    ambiguous_uuid = sys_uuid.uuid4()
    conflicting_uuid = sys_uuid.uuid4()
    legacy_outbound_uuid = sys_uuid.uuid5(
        sys_uuid.NAMESPACE_URL,
        "zulip-looking-but-not-the-legacy-provider-identity",
    )
    account_uuid = sys_uuid.uuid4()
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Ambiguous migration stream"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Ambiguous migration topic",
            is_default=True,
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET source_name = 'zulip', source = '{"kind":"zulip"}'::jsonb
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, stream_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready
                ) VALUES (%s, %s, 'zulip', '{}'::jsonb, 'live', TRUE)
                """,
                (account_uuid, api.user_uuid),
            )
            cursor.executemany(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, provider_external_id,
                    external_account_uuid
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"ambiguous"}'::jsonb, %s,
                    %s::jsonb, %s, %s
                )
                """,
                (
                    (
                        ambiguous_uuid,
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "zulip",
                        json.dumps({"kind": "native"}),
                        None,
                        None,
                    ),
                    (
                        conflicting_uuid,
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "zulip",
                        json.dumps({"kind": "zulip", "message_id": "42"}),
                        "43",
                        None,
                    ),
                    (
                        legacy_outbound_uuid,
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "zulip",
                        json.dumps({"kind": "zulip", "message_id": "44"}),
                        "44",
                        account_uuid,
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_operations_v2 (
                    uuid, external_account_uuid, owner_user_uuid,
                    action, target_type, target_uuid
                ) VALUES (%s, %s, %s, 'message.create', 'message', %s)
                """,
                (sys_uuid.uuid4(), account_uuid, api.user_uuid, conflicting_uuid),
            )

        with pytest.raises(
            Exception, match="ambiguous legacy Zulip message provenance"
        ):
            engine.apply_migration(V2_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS(SELECT 1 FROM m_workspace_messages WHERE uuid = %s),
                       EXISTS(SELECT 1 FROM m_workspace_messages WHERE uuid = %s),
                       to_regclass('messenger_messages')
                """,
                (ambiguous_uuid, conflicting_uuid),
            )
            assert cursor.fetchone() == (True, True, None)
            cursor.execute(
                "DELETE FROM m_workspace_messages WHERE uuid = ANY(%s::uuid[])",
                ([ambiguous_uuid, legacy_outbound_uuid],),
            )
        # Durable operation provenance must not override a directly
        # contradictory source/provider ID pair.
        with pytest.raises(Exception, match="conflicting inbound and local outbound"):
            engine.apply_migration(V2_MIGRATION)
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_messages WHERE uuid = %s",
                (conflicting_uuid,),
            )
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
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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


def test_native_v2_migration_resets_zulip_projection_and_preserves_native(
    api, db, monkeypatch
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    engine.rollback_migration(PREPARE_V2_MIGRATION)
    _truncate_messenger_test_data()
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Mixed migration stream"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Mixed migration topic",
            is_default=True,
        )
        retained_file_uuid = sys_uuid.uuid4()
        retained_image_uuid = sys_uuid.uuid4()
        retained_video_uuid = sys_uuid.uuid4()
        reset_file_uuid = sys_uuid.uuid4()
        ambiguous_file_uuid = sys_uuid.uuid4()
        native = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": (
                        f"native urn:file:{retained_file_uuid} "
                        f"urn:image:{retained_image_uuid} "
                        f"urn:video:{retained_video_uuid}"
                    ),
                },
            },
        )
        assert native.status_code == 201, native.text
        native_uuid = native.json()["uuid"]
        outbound = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "native outbound"},
            },
        )
        assert outbound.status_code == 201, outbound.text
        outbound_uuid = outbound.json()["uuid"]

        account_uuid = sys_uuid.uuid4()
        provider_realm_uuid = sys_uuid.uuid4()
        alias_owner_uuid = sys_uuid.uuid4()
        alias_account_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(
            db,
            alias_owner_uuid,
            f"user-{alias_owner_uuid}",
        )
        bridge_uuid = sys_uuid.uuid4()
        chat_uuid = sys_uuid.uuid4()
        provider_message_uuid = sys_uuid.uuid5(
            sys_uuid.UUID("9a1d0e75-50a5-413c-b3e8-d070232ef57f"),
            f"zulip:{account_uuid}:message:42",
        )
        provider_reaction_uuid = sys_uuid.uuid4()
        retained_provider_reaction_uuid = sys_uuid.uuid4()
        retained_native_reaction_uuid = sys_uuid.uuid4()
        account_resource = {
            "resource_type": "external_account",
            "uuid": str(account_uuid),
            "generation": 1,
        }
        chat_resource = {
            "resource_type": "external_chat_assignment",
            "uuid": str(chat_uuid),
            "generation": 1,
        }
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
                VALUES (%s, 'zulip')
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                    INSERT INTO m_external_accounts_v2 (
                        uuid, owner_user_uuid, provider, settings,
                        status, live_ready, provider_realm_uuid,
                        provider_owner_user_id
                    ) VALUES (
                        %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s, '1'
                )
                """,
                (account_uuid, api.user_uuid, provider_realm_uuid),
            )
            cursor.execute(
                """
                    INSERT INTO m_external_accounts_v2 (
                        uuid, owner_user_uuid, provider, settings,
                        status, live_ready, provider_realm_uuid,
                        provider_owner_user_id
                    ) VALUES (
                        %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s, '2'
                )
                """,
                (alias_account_uuid, alias_owner_uuid, provider_realm_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'channel:42',
                    '{"chat_type":"channel"}'::jsonb, 'Migration chat', TRUE,
                    %s, %s, 'live'
                )
                """,
                (
                    chat_uuid,
                    account_uuid,
                    api.user_uuid,
                    api.project_id,
                    stream_uuid,
                ),
            )
            for resource_type, resource_uuid, resource in (
                ("external_account", account_uuid, account_resource),
                (
                    "external_account",
                    alias_account_uuid,
                    {
                        **account_resource,
                        "uuid": str(alias_account_uuid),
                    },
                ),
                ("external_chat_assignment", chat_uuid, chat_resource),
            ):
                cursor.execute(
                    """
                    INSERT INTO m_external_bridge_desired_resources_v1 (
                        bridge_instance_uuid, provider_kind, resource_type,
                        resource_uuid, operation, generation, resource
                    ) VALUES (%s, 'zulip', %s, %s, 'upsert', 1, %s::jsonb)
                    """,
                    (bridge_uuid, resource_type, resource_uuid, json.dumps(resource)),
                )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, external_account_uuid,
                    provider_external_id, provider_metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"zulip"}'::jsonb,
                    'zulip', '{"kind":"zulip","message_id":null}'::jsonb,
                    %s, '42', '{"chat_key":"channel:42"}'::jsonb
                )
                """,
                (
                    provider_message_uuid,
                    api.project_id,
                    stream_uuid,
                    topic_uuid,
                    api.user_uuid,
                    account_uuid,
                ),
            )
            cursor.execute(
                """
                    INSERT INTO m_workspace_message_reactions (
                        uuid, project_id, message_uuid, user_uuid, emoji_name,
                        external_account_uuid, provider_external_id,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, 'thumbs_up', %s, 'reaction:42',
                        NOW(), NOW()
                    )
                """,
                (
                    provider_reaction_uuid,
                    api.project_id,
                    provider_message_uuid,
                    api.user_uuid,
                    account_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_message_reactions (
                    uuid, project_id, message_uuid, user_uuid, emoji_name,
                    external_account_uuid, provider_external_id,
                    created_at, updated_at
                ) VALUES
                    (%s, %s, %s, %s, 'eyes', %s, 'reaction:43', NOW(), NOW()),
                    (%s, %s, %s, %s, 'heart', NULL, NULL, NOW(), NOW())
                """,
                (
                    retained_provider_reaction_uuid,
                    api.project_id,
                    outbound_uuid,
                    api.user_uuid,
                    account_uuid,
                    retained_native_reaction_uuid,
                    api.project_id,
                    native_uuid,
                    api.user_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s, provider_external_id = '43',
                    provider_metadata = '{"chat_key":"channel:42"}'::jsonb,
                    source_name = 'zulip',
                    source = '{"kind":"zulip","message_id":null}'::jsonb
                WHERE uuid = %s
                """,
                (account_uuid, outbound_uuid),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '44',
                    provider_metadata = '{"chat_key":"channel:42"}'::jsonb
                WHERE uuid = %s
                """,
                (account_uuid, native_uuid),
            )
            # Rows created before the durable provider-operation queue retain
            # their paired native source fields after echo reconciliation.
            # The migration must preserve them even when no operation exists.
            cursor.execute(
                """
                INSERT INTO m_external_operations_v2 (
                    uuid, external_account_uuid, owner_user_uuid,
                    action, target_type, target_uuid, status, details
                ) VALUES (
                    %s, %s, %s, 'message.create', 'message', %s,
                    'succeeded',
                    '{"provider_result":{"status":"succeeded"}}'::jsonb
                )
                """,
                (sys_uuid.uuid4(), account_uuid, api.user_uuid, outbound_uuid),
            )
            for file_uuid, object_id in (
                (retained_file_uuid, "external-content/sha256/aa/retained"),
                (retained_image_uuid, "external-content/sha256/aa/image"),
                (retained_video_uuid, "external-content/sha256/aa/video"),
                (reset_file_uuid, "external-content/sha256/bb/reset"),
                (ambiguous_file_uuid, "native-content/ambiguous"),
            ):
                cursor.execute(
                    """
                    INSERT INTO m_workspace_files (
                        uuid, project_id, name, user_uuid, stream_uuid,
                        content_type, size_bytes, hash, storage_type,
                        storage_id, storage_object_id, external_account_uuid
                    ) VALUES (
                        %s, %s, 'attachment.bin', %s, %s,
                        'application/octet-stream', 1, '00', 'file', '', %s, %s
                    )
                    """,
                    (
                        file_uuid,
                        api.project_id,
                        api.user_uuid,
                        stream_uuid,
                        object_id,
                        account_uuid,
                    ),
                )
        db.commit()

        engine.apply_migration(CURRENT_MIGRATION_HEAD)
        api_store.configure_store_factory(store_factory.build_store_factory())

        with contexts.Context().session_manager() as session:
            assert provider_v2._message_uuid(
                session,
                {
                    "provider_realm_uuid": provider_realm_uuid,
                    "account_uuid": alias_account_uuid,
                },
                "43",
            ) == sys_uuid.UUID(outbound_uuid)

        deleted_objects = []
        deleted_metadata = []
        monkeypatch.setattr(
            v2_projection.file_repository,
            "delete_storage_object_if_unreferenced",
            lambda *args: deleted_objects.append(args[1:]),
        )
        monkeypatch.setattr(
            v2_projection.file_storage,
            "delete_workspace_file_metadata",
            lambda *args, **kwargs: deleted_metadata.append((args, kwargs)),
        )
        with contexts.Context().session_manager() as session:
            assert v2_projection.process_one_provider_file_cleanup_task(
                session, "integration:provider-file-reset"
            )
        assert deleted_objects[0][0] == reset_file_uuid
        assert deleted_metadata[0][0][0] == reset_file_uuid

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS(SELECT 1 FROM messenger_messages WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM messenger_messages WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM messenger_messages WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_files WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_files WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_files WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_files WHERE uuid = %s),
                    EXISTS(SELECT 1 FROM m_workspace_files WHERE uuid = %s),
                    (SELECT provider_realm_uuid FROM messenger_messages
                     WHERE uuid = %s),
                    (SELECT provider_message_id FROM messenger_messages
                     WHERE uuid = %s),
                    (SELECT status FROM messenger_provider_file_cleanup_tasks
                     WHERE file_uuid = %s)
                """,
                (
                    native_uuid,
                    outbound_uuid,
                    provider_message_uuid,
                    retained_file_uuid,
                    retained_image_uuid,
                    retained_video_uuid,
                    reset_file_uuid,
                    ambiguous_file_uuid,
                    outbound_uuid,
                    outbound_uuid,
                    reset_file_uuid,
                ),
            )
            assert cursor.fetchone() == (
                True,
                True,
                False,
                True,
                True,
                True,
                False,
                True,
                provider_realm_uuid,
                "43",
                "completed",
            )
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM messenger_message_reaction_facts
                        WHERE uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM messenger_message_reaction_facts
                        WHERE uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM messenger_message_reaction_facts
                        WHERE uuid = %s
                    )
                """,
                (
                    provider_reaction_uuid,
                    retained_provider_reaction_uuid,
                    retained_native_reaction_uuid,
                ),
            )
            assert cursor.fetchone() == (False, False, True)
            cursor.execute(
                """
                SELECT desired_generation, projection_reset_generation,
                       status, live_ready
                FROM m_external_accounts_v2 WHERE uuid = %s
                """,
                (account_uuid,),
            )
            assert cursor.fetchone() == (3, 2, "backfill", False)
            cursor.execute(
                """
                SELECT generation,
                       (resource->>'projection_reset_generation')::bigint
                FROM m_external_bridge_desired_resources_v1
                WHERE resource_uuid = %s AND resource_type = 'external_account'
                """,
                (account_uuid,),
            )
            assert cursor.fetchone() == (3, 2)
            cursor.execute(
                """
                SELECT revision, status FROM m_external_chats_v2 WHERE uuid = %s
                """,
                (chat_uuid,),
            )
            assert cursor.fetchone() == (3, "syncing")
            cursor.execute(
                """
                SELECT
                    (SELECT count(*)
                     FROM m_external_operations_v2
                        WHERE details->>'migration_provenance' =
                            'pre_operation_native_echo'
                    ),
                    to_regclass(
                        'messenger_v2_prepare_message_payload_trgm_idx'
                    ),
                    to_regclass(
                        'messenger_v2_prepare_message_create_target_idx'
                    )
                """
            )
            assert cursor.fetchone() == (0, None, None)
    finally:
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('messenger_messages')")
            migration_applied = cursor.fetchone()[0] is not None
        if not migration_applied:
            engine.apply_migration(CURRENT_MIGRATION_HEAD)
        api_store.configure_store_factory(store_factory.build_store_factory())
        _truncate_messenger_test_data()


def test_forward_provider_identity_repair_requires_terminal_operation(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ZULIP_PROJECTION_RESET_MIGRATION)
    engine.rollback_migration(CURRENT_MIGRATION_HEAD)
    engine.rollback_migration(PREPARE_V2_MIGRATION)
    _truncate_messenger_test_data()
    account_uuid = sys_uuid.uuid4()
    alias_account_uuid = sys_uuid.uuid4()
    alias_owner_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    inserted_legacy_uuid = sys_uuid.uuid4()
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Forward identity repair stream"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Forward identity repair topic",
            is_default=True,
        )
        conftest.seed_workspace_user(
            db,
            alias_owner_uuid,
            f"user-{alias_owner_uuid}",
        )
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready, provider_realm_uuid,
                    provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s, %s
                )
                """,
                (
                    (
                        account_uuid,
                        api.user_uuid,
                        provider_realm_uuid,
                        "forward-owner",
                    ),
                    (
                        alias_account_uuid,
                        alias_owner_uuid,
                        provider_realm_uuid,
                        "forward-alias",
                    ),
                ),
            )

        native = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "native retained"},
            },
        )
        outbound = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "outbound retained"},
            },
        )
        assert native.status_code == 201, native.text
        assert outbound.status_code == 201, outbound.text
        native_uuid = sys_uuid.UUID(native.json()["uuid"])
        outbound_uuid = sys_uuid.UUID(outbound.json()["uuid"])

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '901',
                    provider_metadata = '{"external_id":"900"}'::jsonb
                WHERE uuid = %s
                """,
                (account_uuid, native_uuid),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '902',
                    provider_metadata = '{}'::jsonb,
                    source_name = 'zulip',
                    source = '{"kind":"zulip","message_id":"902"}'::jsonb
                WHERE uuid = %s
                """,
                (account_uuid, outbound_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_external_operations_v2 (
                    uuid, external_account_uuid, owner_user_uuid,
                    action, target_type, target_uuid, status
                ) VALUES (
                    %s, %s, %s, 'message.create', 'message', %s, 'queued'
                )
                """,
                (
                    operation_uuid,
                    account_uuid,
                    api.user_uuid,
                    outbound_uuid,
                ),
            )
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM m_external_operations_v2),
                    (SELECT count(*) FROM m_external_provider_operations_v1),
                    (SELECT count(*) FROM messenger_domain_outbox_events)
                """
            )
            operation_counts = cursor.fetchone()
        db.commit()

        with pytest.raises(
            Exception,
            match="ambiguous retained provider message provenance",
        ):
            engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT provider_realm_uuid, provider_message_id
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                ORDER BY provider_external_id
                """,
                ([native_uuid, outbound_uuid],),
            )
            assert cursor.fetchall() == [(None, None), (None, None)]
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                ("2022d56e-484d-4047-8e65-f37c65da229d",),
            )
            assert cursor.fetchone() == (False,)
            cursor.execute(
                """
                UPDATE m_external_operations_v2
                SET status = 'succeeded',
                    details = '{"provider_result":{"status":"succeeded"}}'::jsonb
                WHERE uuid = %s
                """,
                (operation_uuid,),
            )
        db.commit()

        with pytest.raises(
            Exception,
            match="metadata external id conflicts with provider identity",
        ):
            engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET provider_metadata = '{"external_id":"901"}'::jsonb
                WHERE uuid = %s
                """,
                (native_uuid,),
            )
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM m_external_operations_v2),
                    (SELECT count(*) FROM m_external_provider_operations_v1),
                    (SELECT count(*) FROM messenger_domain_outbox_events)
                """
            )
            operation_counts = cursor.fetchone()
        db.commit()

        engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM m_external_operations_v2),
                    (SELECT count(*) FROM m_external_provider_operations_v1),
                    (SELECT count(*) FROM messenger_domain_outbox_events)
                """
            )
            assert cursor.fetchone() == operation_counts

        with contexts.Context().session_manager() as session:
            for provider_message_id, public_uuid in (
                ("901", native_uuid),
                ("902", outbound_uuid),
            ):
                assert (
                    provider_v2._message_uuid(
                        session,
                        {
                            "provider_realm_uuid": provider_realm_uuid,
                            "account_uuid": alias_account_uuid,
                        },
                        provider_message_id,
                    )
                    == public_uuid
                )

        update_path = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "update path"},
            },
        )
        assert update_path.status_code == 201, update_path.text
        update_path_uuid = sys_uuid.UUID(update_path.json()["uuid"])
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '903',
                    provider_metadata = '{}'::jsonb
                WHERE uuid = %s
                """,
                (account_uuid, update_path_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, external_account_uuid,
                    provider_external_id, provider_metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"insert path"}'::jsonb,
                    'native', '{"kind":"native"}'::jsonb, %s, '904',
                    '{}'::jsonb
                )
                """,
                (
                    inserted_legacy_uuid,
                    api.project_id,
                    stream_uuid,
                    topic_uuid,
                    api.user_uuid,
                    account_uuid,
                ),
            )
        db.commit()

        with contexts.Context().session_manager() as session:
            for provider_message_id, public_uuid in (
                ("903", update_path_uuid),
                ("904", inserted_legacy_uuid),
            ):
                assert (
                    provider_v2._message_uuid(
                        session,
                        {
                            "provider_realm_uuid": provider_realm_uuid,
                            "account_uuid": alias_account_uuid,
                        },
                        provider_message_id,
                    )
                    == public_uuid
                )

        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="invalid provider message identifier",
        ):
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET provider_external_id = %s
                    WHERE uuid = %s
                    """,
                    ("1" * 33, update_path_uuid),
                )
        db.rollback()

        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="metadata external id conflicts with provider identity",
        ):
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET provider_metadata = '{"external_id":"999"}'::jsonb
                    WHERE uuid = %s
                    """,
                    (update_path_uuid,),
                )
        db.rollback()

        with pytest.raises(
            psycopg.errors.CheckViolation,
            match="metadata realm conflicts with account realm",
        ):
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET provider_metadata = jsonb_build_object(
                        'provider_realm_uuid', %s::text
                    )
                    WHERE uuid = %s
                    """,
                    (sys_uuid.uuid4(), update_path_uuid),
                )
        db.rollback()

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM m_external_operations_v2),
                    (SELECT count(*) FROM m_external_provider_operations_v1)
                """
            )
            assert cursor.fetchone() == operation_counts[:2]
            cursor.execute(
                """
                SELECT count(*)
                FROM m_external_operations_v2
                WHERE details->>'migration_provenance' =
                    'pre_operation_native_echo'
                """
            )
            assert cursor.fetchone() == (0,)
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE m_external_operations_v2 SET status = 'succeeded', "
                'details = \'{"provider_result":{"status":"succeeded"}}\'::jsonb '
                "WHERE uuid = %s",
                (operation_uuid,),
            )
        db.commit()
        engine.apply_migration(CURRENT_MIGRATION_HEAD)
        _truncate_messenger_test_data()
        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)


def test_forward_provider_identity_repair_detaches_only_proven_aliases(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ZULIP_PROJECTION_RESET_MIGRATION)
    engine.rollback_migration(CURRENT_MIGRATION_HEAD)
    engine.rollback_migration(PREPARE_V2_MIGRATION)
    _truncate_messenger_test_data()
    account_uuid = sys_uuid.uuid4()
    alias_account_uuid = sys_uuid.uuid4()
    alias_owner_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    second_uuid = None
    try:
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Duplicate identity repair stream"
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Duplicate identity repair topic",
            is_default=True,
        )
        conftest.seed_workspace_user(
            db,
            alias_owner_uuid,
            f"user-{alias_owner_uuid}",
        )
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready, provider_realm_uuid,
                    provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s,
                    %s
                )
                """,
                (
                    (
                        account_uuid,
                        api.user_uuid,
                        provider_realm_uuid,
                        "duplicate-owner",
                    ),
                    (
                        alias_account_uuid,
                        alias_owner_uuid,
                        provider_realm_uuid,
                        "duplicate-alias",
                    ),
                ),
            )

        public_uuids = []
        for content in ("duplicate one", "duplicate two"):
            response = api.post(
                MESSAGES,
                json={
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "payload": {"kind": "markdown", "content": content},
                },
            )
            assert response.status_code == 201, response.text
            public_uuids.append(sys_uuid.UUID(response.json()["uuid"]))
        second_uuid = public_uuids[1]
        with db.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '990',
                    provider_metadata = '{}'::jsonb
                WHERE uuid = %s
                """,
                (
                    (account_uuid, public_uuids[0]),
                    (alias_account_uuid, public_uuids[1]),
                ),
            )
        db.commit()

        with pytest.raises(
            Exception,
            match="multiple retained messages claim one realm message",
        ):
            engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT provider_realm_uuid, provider_message_id
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (public_uuids,),
            )
            assert cursor.fetchall() == [(None, None), (None, None)]
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET provider_metadata = jsonb_build_object(
                        'external_id', '990',
                        'provider_realm_uuid', %s::text,
                        'provider_original_url',
                            'https://provider.invalid/messages/990',
                        'lossy_conversion', false
                    )
                WHERE uuid = ANY(%s::uuid[])
                """,
                (provider_realm_uuid, public_uuids),
            )
        db.commit()
        engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE provider_realm_uuid = %s
                          AND provider_message_id = '990'
                    ),
                    count(*) FILTER (
                        WHERE external_account_uuid IS NULL
                          AND provider_external_id IS NULL
                    ),
                    count(*)
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (provider_realm_uuid, public_uuids),
            )
            assert cursor.fetchone() == (1, 1, 2)
        with contexts.Context().session_manager() as session:
            resolved_uuid = provider_v2._message_uuid(
                session,
                {
                    "provider_realm_uuid": provider_realm_uuid,
                    "account_uuid": alias_account_uuid,
                },
                "990",
            )
        assert resolved_uuid in public_uuids
        for public_uuid in public_uuids:
            response = api.get(f"{MESSAGES}{public_uuid}")
            assert response.status_code == 200, response.text
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                ("2022d56e-484d-4047-8e65-f37c65da229d",),
            )
            head_applied = cursor.fetchone() == (True,)
        if not head_applied and second_uuid is not None:
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET provider_external_id = '991'
                    WHERE uuid = %s AND provider_external_id = '990'
                    """,
                    (second_uuid,),
                )
            db.commit()
        if not head_applied:
            engine.apply_migration(CURRENT_MIGRATION_HEAD)
        _truncate_messenger_test_data()
        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)


def test_forward_provider_identity_repair_prefers_existing_imported_identity(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ZULIP_PROJECTION_RESET_MIGRATION)
    engine.rollback_migration(CURRENT_MIGRATION_HEAD)
    engine.rollback_migration(PREPARE_V2_MIGRATION)
    _truncate_messenger_test_data()
    account_uuid = sys_uuid.uuid4()
    alias_account_uuid = sys_uuid.uuid4()
    alias_owner_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    imported_public_uuid = sys_uuid.uuid4()
    retained_public_uuid = None
    try:
        stream_uuid = conftest.seed_user_stream(
            db,
            api.project_id,
            api.user_uuid,
            "Imported identity repair stream",
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Imported identity repair topic",
            is_default=True,
        )
        conftest.seed_workspace_user(
            db,
            alias_owner_uuid,
            f"user-{alias_owner_uuid}",
        )
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready, provider_realm_uuid,
                    provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s,
                    %s
                )
                """,
                (
                    (
                        account_uuid,
                        api.user_uuid,
                        provider_realm_uuid,
                        "imported-owner",
                    ),
                    (
                        alias_account_uuid,
                        alias_owner_uuid,
                        provider_realm_uuid,
                        "imported-alias",
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, external_account_uuid,
                    provider_external_id, provider_metadata, created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"imported"}'::jsonb,
                    'zulip',
                    '{"kind":"zulip","stream_id":42}'::jsonb,
                    %s, '995',
                    jsonb_build_object(
                        'external_id', '995',
                        'provider_realm_uuid', %s::text,
                        'provider_original_url',
                            'https://provider.invalid/messages/995'
                    ),
                    TIMESTAMP '2025-01-01 00:00:00',
                    TIMESTAMP '2025-01-01 00:00:00'
                )
                """,
                (
                    imported_public_uuid,
                    api.project_id,
                    stream_uuid,
                    topic_uuid,
                    api.user_uuid,
                    account_uuid,
                    provider_realm_uuid,
                ),
            )

        retained = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "retained alias"},
            },
        )
        assert retained.status_code == 201, retained.text
        retained_public_uuid = sys_uuid.UUID(retained.json()["uuid"])
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s,
                    provider_external_id = '995',
                    provider_metadata = jsonb_build_object(
                        'external_id', '995',
                        'provider_realm_uuid', %s::text,
                        'provider_original_url',
                            'https://provider.invalid/messages/995'
                    )
                WHERE uuid = %s
                """,
                (
                    alias_account_uuid,
                    provider_realm_uuid,
                    retained_public_uuid,
                ),
            )
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE provider_realm_uuid = %s
                          AND provider_message_id = '995'
                    ),
                    count(*) FILTER (
                        WHERE legacy_public_uuid = %s
                          AND provider_realm_uuid IS NULL
                    )
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (
                    provider_realm_uuid,
                    retained_public_uuid,
                    [imported_public_uuid, retained_public_uuid],
                ),
            )
            assert cursor.fetchone() == (1, 1)
        db.commit()

        engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE provider_realm_uuid = %s
                          AND provider_message_id = '995'
                          AND source_name = 'zulip'
                    ),
                    count(*) FILTER (
                        WHERE legacy_public_uuid = %s
                          AND external_account_uuid IS NULL
                          AND provider_external_id IS NULL
                    ),
                    count(*)
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (
                    provider_realm_uuid,
                    retained_public_uuid,
                    [imported_public_uuid, retained_public_uuid],
                ),
            )
            assert cursor.fetchone() == (1, 1, 2)
            cursor.execute(
                """
                SELECT external_account_uuid, provider_external_id
                FROM m_workspace_messages
                WHERE uuid = %s
                """,
                (retained_public_uuid,),
            )
            assert cursor.fetchone() == (None, None)

        with contexts.Context().session_manager() as session:
            assert (
                provider_v2._message_uuid(
                    session,
                    {
                        "provider_realm_uuid": provider_realm_uuid,
                        "account_uuid": alias_account_uuid,
                    },
                    "995",
                )
                == imported_public_uuid
            )
        for public_uuid in (imported_public_uuid, retained_public_uuid):
            response = api.get(f"{MESSAGES}{public_uuid}")
            assert response.status_code == 200, response.text
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                ("2022d56e-484d-4047-8e65-f37c65da229d",),
            )
            head_applied = cursor.fetchone() == (True,)
        if not head_applied and retained_public_uuid is not None:
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET external_account_uuid = NULL,
                        provider_external_id = NULL
                    WHERE uuid = %s
                    """,
                    (retained_public_uuid,),
                )
            db.commit()
        if not head_applied:
            engine.apply_migration(CURRENT_MIGRATION_HEAD)
        _truncate_messenger_test_data()
        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)


def test_forward_provider_identity_repair_accepts_released_bridge_payload(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ZULIP_PROJECTION_RESET_MIGRATION)
    engine.rollback_migration(CURRENT_MIGRATION_HEAD)
    engine.rollback_migration(PREPARE_V2_MIGRATION)
    _truncate_messenger_test_data()
    account_uuid = sys_uuid.uuid4()
    alias_account_uuid = sys_uuid.uuid4()
    alias_owner_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    public_uuids = [sys_uuid.uuid4() for _ in range(4)]
    try:
        stream_uuid = conftest.seed_user_stream(
            db,
            api.project_id,
            api.user_uuid,
            "Released Bridge identity repair stream",
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Released Bridge identity repair topic",
            is_default=True,
        )
        conftest.seed_workspace_user(
            db,
            alias_owner_uuid,
            f"user-{alias_owner_uuid}",
        )
        with db.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    status, live_ready, provider_realm_uuid,
                    provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, 'live', TRUE, %s, %s
                )
                """,
                (
                    (
                        account_uuid,
                        api.user_uuid,
                        provider_realm_uuid,
                        "released-owner",
                    ),
                    (
                        alias_account_uuid,
                        alias_owner_uuid,
                        provider_realm_uuid,
                        "released-alias",
                    ),
                ),
            )
            cursor.executemany(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source, external_account_uuid,
                    provider_external_id, provider_metadata, created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    jsonb_build_object(
                        'kind', 'markdown', 'content', %s::text
                    ),
                    'zulip', '{"kind":"zulip","stream_id":42}'::jsonb,
                    %s, %s,
                    jsonb_build_object(
                        'external_id', %s::text,
                        'provider_original_url',
                            'https://provider.invalid/messages/' || %s::text
                    ) || CASE WHEN %s::uuid IS NULL THEN '{}'::jsonb ELSE
                        jsonb_build_object(
                            'provider_realm_uuid', %s::uuid::text
                        ) END,
                    TIMESTAMP '2025-01-01 00:00:00',
                    TIMESTAMP '2025-01-01 00:00:00'
                )
                """,
                (
                    (
                        public_uuids[0],
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "existing imported projection",
                        account_uuid,
                        "995",
                        "995",
                        "995",
                        provider_realm_uuid,
                        provider_realm_uuid,
                    ),
                    (
                        public_uuids[1],
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "account alias projection",
                        alias_account_uuid,
                        "995",
                        "995",
                        "995",
                        None,
                        None,
                    ),
                    (
                        public_uuids[2],
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "unique legacy projection",
                        alias_account_uuid,
                        "996",
                        "996",
                        "996",
                        None,
                        None,
                    ),
                    (
                        public_uuids[3],
                        api.project_id,
                        stream_uuid,
                        topic_uuid,
                        api.user_uuid,
                        "invalid released projection",
                        alias_account_uuid,
                        "997",
                        "997",
                        "997",
                        None,
                        None,
                    ),
                ),
            )
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (WHERE provider_realm_uuid = %s),
                    count(*) FILTER (WHERE provider_realm_uuid IS NULL)
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (provider_realm_uuid, public_uuids),
            )
            assert cursor.fetchone() == (1, 3)
        db.commit()

        invalid_payloads = (
            (
                {"kind": "zulip", "stream_id": 42, "message_id": "997"},
                {
                    "external_id": "997",
                    "provider_original_url": "https://provider.invalid/messages/997",
                },
            ),
            (
                {"kind": "zulip", "stream_id": 42},
                {
                    "external_id": "998",
                    "provider_original_url": "https://provider.invalid/messages/997",
                },
            ),
            (
                {"kind": "zulip", "stream_id": 42},
                {"external_id": "997"},
            ),
            (
                {"kind": "zulip", "stream_id": 42},
                {
                    "external_id": "997",
                    "provider_original_url": "https://provider.invalid/messages/997",
                    "provider_realm_uuid": str(sys_uuid.uuid4()),
                },
            ),
        )
        for source, provider_metadata in invalid_payloads:
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET source = %s::jsonb,
                        provider_metadata = %s::jsonb
                    WHERE uuid = %s
                    """,
                    (
                        json.dumps(source),
                        json.dumps(provider_metadata),
                        public_uuids[3],
                    ),
                )
                # The immutable 0152 update trigger can eagerly key a row that
                # gains source.message_id.  Recreate the pre-repair persisted
                # shape so this test exercises 0156's fail-closed guard rather
                # than the already-keyed fast path.
                cursor.execute(
                    """
                    UPDATE messenger_messages
                    SET provider_realm_uuid = NULL,
                        provider_message_id = NULL
                    WHERE legacy_public_uuid = %s
                    """,
                    (public_uuids[3],),
                )
            db.commit()

            with db.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source, provider, provider_external_id,
                           provider_realm_uuid,
                           EXISTS (
                               SELECT 1
                               FROM m_external_operations_v2 AS operation
                               WHERE operation.action = 'message.create'
                                 AND operation.target_type = 'message'
                                 AND operation.target_uuid =
                                     COALESCE(message.legacy_public_uuid,
                                              message.uuid)
                                 AND operation.owner_user_uuid =
                                     message.author_uuid
                                 AND operation.external_account_uuid =
                                     message.external_account_uuid
                                 AND operation.status = 'succeeded'
                           )
                    FROM messenger_messages AS message
                    WHERE legacy_public_uuid = %s
                    """,
                    (public_uuids[3],),
                )
                canonical = cursor.fetchone()
                assert canonical == (
                    source,
                    provider_metadata,
                    "997",
                    None,
                    False,
                )

            with pytest.raises(
                Exception,
                match="ambiguous retained provider message provenance",
            ):
                engine.apply_migration(CURRENT_MIGRATION_HEAD)

            with db.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT external_account_uuid, provider_external_id
                    FROM m_workspace_messages
                    WHERE uuid = %s
                    """,
                    (public_uuids[1],),
                )
                assert cursor.fetchone() == (alias_account_uuid, "995")
                cursor.execute(
                    "SELECT applied FROM ra_migrations WHERE uuid = %s",
                    ("2022d56e-484d-4047-8e65-f37c65da229d",),
                )
                assert cursor.fetchone() == (False,)

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET source = '{"kind":"zulip","stream_id":42}'::jsonb,
                    provider_metadata = jsonb_build_object(
                        'external_id', '997',
                        'provider_original_url',
                            'https://provider.invalid/messages/997'
                    )
                WHERE uuid = %s
                """,
                (public_uuids[3],),
            )
        db.commit()

        engine.apply_migration(CURRENT_MIGRATION_HEAD)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE provider_realm_uuid = %s
                          AND provider_message_id IN ('995', '996', '997')
                    ),
                    count(*) FILTER (
                        WHERE legacy_public_uuid = %s
                          AND external_account_uuid IS NULL
                          AND provider_external_id IS NULL
                    ),
                    count(*)
                FROM messenger_messages
                WHERE legacy_public_uuid = ANY(%s::uuid[])
                """,
                (provider_realm_uuid, public_uuids[1], public_uuids),
            )
            assert cursor.fetchone() == (3, 1, 4)
            cursor.execute(
                """
                SELECT external_account_uuid, provider_external_id
                FROM m_workspace_messages
                WHERE uuid = %s
                """,
                (public_uuids[1],),
            )
            assert cursor.fetchone() == (None, None)
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                ("2022d56e-484d-4047-8e65-f37c65da229d",),
            )
            head_applied = cursor.fetchone() == (True,)
        if not head_applied:
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE m_workspace_messages
                    SET external_account_uuid = NULL,
                        provider_external_id = NULL
                    WHERE uuid = ANY(%s::uuid[])
                    """,
                    (public_uuids,),
                )
            db.commit()
            engine.apply_migration(CURRENT_MIGRATION_HEAD)
        _truncate_messenger_test_data()
        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)


def test_zulip_projection_reset_preserves_internal_messages_and_clears_counters(
    api,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(ZULIP_PROJECTION_RESET_MIGRATION)
    _truncate_messenger_test_data()
    try:
        internal_stream = api.post(
            STREAMS,
            json={
                "name": "Internal migration control",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        external_stream = api.post(
            STREAMS,
            json={
                "name": "External migration reset",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        assert internal_stream.status_code == 201, internal_stream.text
        assert external_stream.status_code == 201, external_stream.text
        internal_stream = internal_stream.json()
        external_stream = external_stream.json()

        account_uuid = _seed_v2_provider_route(
            db,
            api.project_id,
            api.user_uuid,
            external_stream["uuid"],
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT chat.uuid, bridge.uuid
                FROM m_external_chats_v2 AS chat
                JOIN m_external_bridge_instances_v2 AS bridge
                  ON bridge.provider = chat.provider
                WHERE chat.external_account_uuid = %s
                ORDER BY bridge.created_at, bridge.uuid
                LIMIT 1
                """,
                (account_uuid,),
            )
            chat_uuid, bridge_uuid = cursor.fetchone()
            cursor.executemany(
                """
                INSERT INTO m_external_bridge_desired_resources_v1 (
                    bridge_instance_uuid, provider_kind, resource_type,
                    resource_uuid, operation, generation, resource
                ) VALUES (%s, 'zulip', %s, %s, 'upsert', 1, %s::jsonb)
                """,
                (
                    (
                        bridge_uuid,
                        "external_account",
                        account_uuid,
                        json.dumps(
                            {
                                "generation": 1,
                                "projection_reset_generation": 0,
                            }
                        ),
                    ),
                    (
                        bridge_uuid,
                        "external_chat_assignment",
                        chat_uuid,
                        json.dumps({"generation": 1}),
                    ),
                ),
            )
        db.commit()

        internal_topic = api.post(
            STREAM_TOPICS,
            json={
                "stream_uuid": internal_stream["uuid"],
                "name": "Internal topic",
                "source": {"kind": "native"},
            },
        )
        external_topic = api.post(
            STREAM_TOPICS,
            json={
                "stream_uuid": external_stream["uuid"],
                "name": "External topic",
                "source": {"kind": "native"},
            },
        )
        assert internal_topic.status_code == 201, internal_topic.text
        assert external_topic.status_code == 201, external_topic.text
        internal_topic = internal_topic.json()
        external_topic = external_topic.json()

        internal_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": internal_stream["uuid"],
                "topic_uuid": internal_topic["uuid"],
                "payload": {"kind": "markdown", "content": "keep internal"},
            },
        )
        external_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": external_stream["uuid"],
                "topic_uuid": external_topic["uuid"],
                "payload": {"kind": "markdown", "content": "reset external"},
            },
        )
        assert internal_message.status_code == 201, internal_message.text
        assert external_message.status_code == 201, external_message.text
        internal_message_uuid = sys_uuid.UUID(internal_message.json()["uuid"])
        external_message_uuid = sys_uuid.UUID(external_message.json()["uuid"])

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 123,
                    active_unread_count = 100,
                    passive_unread_count = 23,
                    last_message_uuid = %s
                WHERE project_id = %s AND stream_uuid = %s
                """,
                (
                    external_message_uuid,
                    api.project_id,
                    external_stream["uuid"],
                ),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count = 123,
                    active_unread_count = 100,
                    passive_unread_count = 23,
                    last_message_uuid = %s
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (
                    external_message_uuid,
                    api.project_id,
                    external_topic["uuid"],
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_topic_message_stats_v1
                SET message_count = 123,
                    last_ingest_sequence = 123
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (api.project_id, external_topic["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_folder_bindings
                SET unread_count = 100
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, api.user_uuid),
            )
        db.commit()

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE messenger_streams
                SET source = '{"kind":"native"}'::jsonb
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, external_stream["uuid"]),
            )
        db.commit()
        with pytest.raises(
            Exception,
            match="contradictory canonical stream metadata",
        ):
            engine.apply_migration(ZULIP_PROJECTION_RESET_MIGRATION)
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    (SELECT unread_count
                     FROM messenger_stream_bindings
                     WHERE project_id = %s AND stream_uuid = %s
                       AND user_uuid = %s),
                    (SELECT applied FROM ra_migrations WHERE uuid = %s)
                """,
                (
                    api.project_id,
                    external_message_uuid,
                    api.project_id,
                    external_stream["uuid"],
                    api.user_uuid,
                    ZULIP_PROJECTION_RESET_UUID,
                ),
            )
            assert cursor.fetchone() == (True, 123, False)
            cursor.execute(
                """
                UPDATE messenger_streams
                SET source = %s::jsonb
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    json.dumps(
                        {
                            "kind": "zulip",
                            "stream_id": 42,
                            "server_url": "https://provider.example.invalid",
                            "source_scope": str(account_uuid),
                        }
                    ),
                    api.project_id,
                    external_stream["uuid"],
                ),
            )
        db.commit()

        engine.apply_migration(ZULIP_PROJECTION_RESET_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS (
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS (
                        SELECT 1 FROM messenger_messages
                        WHERE project_id = %s
                          AND legacy_public_uuid = %s
                    )
                """,
                (
                    api.project_id,
                    internal_message_uuid,
                    api.project_id,
                    external_message_uuid,
                    api.project_id,
                    external_message_uuid,
                ),
            )
            assert cursor.fetchone() == (True, False, False)
            cursor.execute(
                """
                SELECT binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count,
                       binding.last_message_uuid
                FROM messenger_stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = %s
                  AND binding.user_uuid = %s
                """,
                (
                    api.project_id,
                    external_stream["uuid"],
                    api.user_uuid,
                ),
            )
            assert cursor.fetchone() == (0, 0, 0, None)
            cursor.execute(
                """
                SELECT binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count,
                       binding.last_message_uuid
                FROM messenger_user_topic_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.topic_uuid = %s
                  AND binding.user_uuid = %s
                """,
                (
                    api.project_id,
                    external_topic["uuid"],
                    api.user_uuid,
                ),
            )
            assert cursor.fetchone() == (0, 0, 0, None)
            cursor.execute(
                """
                SELECT stats.message_count, stats.last_ingest_sequence
                FROM m_workspace_topic_message_stats_v1 AS stats
                WHERE stats.project_id = %s AND stats.topic_uuid = %s
                """,
                (api.project_id, external_topic["uuid"]),
            )
            assert cursor.fetchone() == (0, None)
            cursor.execute(
                """
                SELECT count(*)
                FROM messenger_user_folder_bindings AS folder
                CROSS JOIN LATERAL jsonb_array_elements(
                    folder.folder_items_snapshot
                ) AS item
                WHERE folder.project_id = %s
                  AND folder.user_uuid = %s
                  AND item->>'stream_uuid' = %s
                  AND (
                      (item->>'unread_count')::integer <> 0
                      OR (item->>'active_unread_count')::integer <> 0
                      OR (item->>'passive_unread_count')::integer <> 0
                  )
                """,
                (
                    api.project_id,
                    api.user_uuid,
                    str(external_stream["uuid"]),
                ),
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                """
                SELECT account.projection_reset_generation,
                       account.status,
                       account.live_ready,
                       chat.status,
                       (desired.resource->>'projection_reset_generation')::bigint
                FROM m_external_accounts_v2 AS account
                JOIN m_external_chats_v2 AS chat
                  ON chat.external_account_uuid = account.uuid
                JOIN m_external_bridge_desired_resources_v1 AS desired
                  ON desired.resource_uuid = account.uuid
                 AND desired.resource_type = 'external_account'
                WHERE account.uuid = %s
                """,
                (account_uuid,),
            )
            assert cursor.fetchone() == (1, "backfill", False, "syncing", 1)
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                (ZULIP_PROJECTION_RESET_UUID,),
            )
            assert cursor.fetchone() == (True,)

        streams = api.get(STREAMS)
        assert streams.status_code == 200, streams.text
        external_row = next(
            row for row in streams.json() if row["uuid"] == external_stream["uuid"]
        )
        assert (
            external_row["unread_count"],
            external_row["active_unread_count"],
            external_row["passive_unread_count"],
            external_row.get("last_message_uuid"),
        ) == (0, 0, 0, None)
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)


def test_zulip_message_reset_rebuilds_mixed_native_chat_state(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(PROVIDER_OWNER_READ_REPAIR_MIGRATION)
    engine.rollback_migration(PROVIDER_READ_PAGE_UNBLOCK_MIGRATION)
    engine.rollback_migration(INTERACTIVE_READ_INDEX_MIGRATION)
    engine.rollback_migration(PROJECTION_CLAIM_INDEX_MIGRATION)
    engine.rollback_migration(ZULIP_MESSAGE_RESET_MIGRATION)
    _truncate_messenger_test_data()
    try:
        peer_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(db, peer_uuid, f"reset-peer-{peer_uuid}")
        _register_project_user(db, api.project_id, peer_uuid)

        route_stream = api.post(
            STREAMS,
            json={
                "name": "Provider reset route",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        mixed_stream = api.post(
            STREAMS,
            json={
                "name": "Native direct reset control",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        assert route_stream.status_code == 201, route_stream.text
        assert mixed_stream.status_code == 201, mixed_stream.text
        route_stream = route_stream.json()
        mixed_stream = mixed_stream.json()
        _drain()

        account_uuid = _seed_v2_provider_route(
            db,
            api.project_id,
            api.user_uuid,
            route_stream["uuid"],
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT chat.uuid, bridge.uuid
                FROM m_external_chats_v2 AS chat
                JOIN m_external_bridge_instances_v2 AS bridge
                  ON bridge.provider = chat.provider
                WHERE chat.external_account_uuid = %s
                ORDER BY bridge.created_at, bridge.uuid
                LIMIT 1
                """,
                (account_uuid,),
            )
            chat_uuid, bridge_uuid = cursor.fetchone()
            cursor.executemany(
                """
                INSERT INTO m_external_bridge_desired_resources_v1 (
                    bridge_instance_uuid, provider_kind, resource_type,
                    resource_uuid, operation, generation, resource
                ) VALUES (%s, 'zulip', %s, %s, 'upsert', 1, %s::jsonb)
                """,
                (
                    (
                        bridge_uuid,
                        "external_account",
                        account_uuid,
                        json.dumps(
                            {
                                "generation": 1,
                                "projection_reset_generation": 0,
                            }
                        ),
                    ),
                    (
                        bridge_uuid,
                        "external_chat_assignment",
                        chat_uuid,
                        json.dumps({"generation": 1}),
                    ),
                ),
            )
        db.commit()

        added = api.post(
            f"{STREAMS}{mixed_stream['uuid']}/actions/add_users/invoke",
            json={"member": [str(peer_uuid)]},
        )
        assert added.status_code == 200, added.text
        peer_binding_uuid = added.json()[0]["uuid"]
        _drain()

        active_topic = api.post(
            STREAM_TOPICS,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "name": "Retained active topic",
                "source": {"kind": "native"},
            },
        )
        passive_topic = api.post(
            STREAM_TOPICS,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "name": "Retained passive topic",
                "source": {"kind": "native"},
            },
        )
        assert active_topic.status_code == 201, active_topic.text
        assert passive_topic.status_code == 201, passive_topic.text
        active_topic = active_topic.json()
        passive_topic = passive_topic.json()
        _drain()

        stream_notifications = api.post(
            f"{STREAMS}{mixed_stream['uuid']}/actions/notifications/invoke",
            user=peer_uuid,
            json={"notification_mode": "all_messages"},
        )
        active_notifications = api.post(
            f"{STREAM_TOPICS}{active_topic['uuid']}/actions/notifications/invoke",
            user=peer_uuid,
            json={"notification_mode": "follow"},
        )
        passive_notifications = api.post(
            f"{STREAM_TOPICS}{passive_topic['uuid']}/actions/notifications/invoke",
            user=peer_uuid,
            json={"notification_mode": "mute"},
        )
        assert stream_notifications.status_code == 200, stream_notifications.text
        assert active_notifications.status_code == 200, active_notifications.text
        assert passive_notifications.status_code == 200, passive_notifications.text

        done = api.post(
            f"{STREAM_TOPICS}{active_topic['uuid']}/actions/toggle_done/invoke"
        )
        assert done.status_code == 200, done.text
        assert done.json()["is_done"] is True

        folder = api.post(FOLDERS, user=peer_uuid, json={"title": "Reset control"})
        assert folder.status_code == 201, folder.text
        folder_item = api.post(
            FOLDER_ITEMS,
            user=peer_uuid,
            json={
                "folder_uuid": folder.json()["uuid"],
                "stream_uuid": mixed_stream["uuid"],
                "chat_type": "private",
            },
        )
        assert folder_item.status_code == 201, folder_item.text
        pinned_item = api.post(
            f"{FOLDER_ITEMS}{folder_item.json()['uuid']}/actions/pin/invoke",
            user=peer_uuid,
        )
        assert pinned_item.status_code == 200, pinned_item.text
        assert pinned_item.json()["pinned_at"] is not None
        _drain()

        read_native = api.post(
            MESSAGES,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "topic_uuid": active_topic["uuid"],
                "payload": {"kind": "markdown", "content": "keep read"},
            },
        )
        active_native = api.post(
            MESSAGES,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "topic_uuid": active_topic["uuid"],
                "payload": {"kind": "markdown", "content": "keep active"},
            },
        )
        passive_native = api.post(
            MESSAGES,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "topic_uuid": passive_topic["uuid"],
                "payload": {"kind": "markdown", "content": "keep passive"},
            },
        )
        provider_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": mixed_stream["uuid"],
                "topic_uuid": active_topic["uuid"],
                "payload": {"kind": "markdown", "content": "remove provider"},
            },
        )
        assert read_native.status_code == 201, read_native.text
        assert active_native.status_code == 201, active_native.text
        assert passive_native.status_code == 201, passive_native.text
        assert provider_message.status_code == 201, provider_message.text
        _drain()

        provider_public_uuid = sys_uuid.UUID(provider_message.json()["uuid"])
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT placement.uuid, placement.message_uuid,
                       placement.legacy_public_uuid
                FROM messenger_message_placements AS placement
                WHERE placement.uuid = %s OR placement.legacy_public_uuid = %s
                """,
                (provider_public_uuid, provider_public_uuid),
            )
            provider_placement_uuid, provider_canonical_uuid, provider_legacy_uuid = (
                cursor.fetchone()
            )
            provider_legacy_uuid = provider_legacy_uuid or provider_placement_uuid
            cursor.execute(
                """
                SELECT message.ingest_sequence
                FROM m_workspace_messages AS message
                WHERE message.project_id = %s AND message.uuid = %s
                """,
                (api.project_id, read_native.json()["uuid"]),
            )
            read_ingest_sequence = cursor.fetchone()[0]
            cursor.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'compact', updated_at = NOW()
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_user_read_chunks_v1 (
                    user_uuid, chunk_number, read_bits
                ) VALUES (
                    %s, %s,
                    set_bit(
                        B'0'::bit(4096),
                        %s,
                        1
                    )
                )
                ON CONFLICT (user_uuid, chunk_number) DO UPDATE
                SET read_bits = set_bit(
                        m_workspace_user_read_chunks_v1.read_bits,
                        %s,
                        1
                    ),
                    updated_at = NOW()
                """,
                (
                    peer_uuid,
                    read_ingest_sequence // 4096,
                    read_ingest_sequence % 4096,
                    read_ingest_sequence % 4096,
                ),
            )
            cursor.execute(
                """
                UPDATE messenger_messages
                SET source_name = 'zulip',
                    source = '{"kind":"native"}'::jsonb,
                    external_account_uuid = %s,
                    provider_external_id = 'direct:reset:42'
                WHERE project_id = %s AND uuid = %s
                """,
                (account_uuid, api.project_id, provider_canonical_uuid),
            )
        db.commit()

        with pytest.raises(
            Exception,
            match="contradictory canonical message metadata",
        ):
            engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS(
                           SELECT 1 FROM messenger_messages
                           WHERE project_id = %s AND uuid = %s
                       ),
                       (SELECT applied FROM ra_migrations WHERE uuid = %s)
                """,
                (
                    api.project_id,
                    provider_canonical_uuid,
                    ZULIP_MESSAGE_RESET_UUID,
                ),
            )
            assert cursor.fetchone() == (True, False)
            provider_source = json.dumps(
                {
                    "kind": "zulip",
                    "message_id": "42",
                    "chat_type": "direct",
                }
            )
            cursor.execute(
                """
                UPDATE messenger_messages
                SET source = %s::jsonb
                WHERE project_id = %s AND uuid = %s
                """,
                (provider_source, api.project_id, provider_canonical_uuid),
            )
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET source_name = 'zulip', source = %s::jsonb,
                    external_account_uuid = %s,
                    provider_external_id = 'direct:reset:42'
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    provider_source,
                    account_uuid,
                    api.project_id,
                    provider_legacy_uuid,
                ),
            )
            cursor.execute(
                """
                SELECT binding.active, binding.membership_generation,
                       binding.role, binding.notification_mode,
                       topic_binding.notification_mode,
                       topic.is_done, item.folder_uuid, item.pinned_at
                FROM messenger_stream_bindings AS binding
                JOIN messenger_user_topic_bindings AS topic_binding
                  ON topic_binding.project_id = binding.project_id
                 AND topic_binding.user_uuid = binding.user_uuid
                 AND topic_binding.topic_uuid = %s
                JOIN messenger_topics AS topic
                  ON topic.project_id = topic_binding.project_id
                 AND topic.uuid = topic_binding.topic_uuid
                JOIN messenger_folder_items AS item
                  ON item.project_id = binding.project_id
                 AND item.user_uuid = binding.user_uuid
                 AND item.stream_uuid = binding.stream_uuid
                 AND item.folder_uuid = %s
                WHERE binding.project_id = %s
                  AND binding.uuid = %s
                """,
                (
                    active_topic["uuid"],
                    folder.json()["uuid"],
                    api.project_id,
                    peer_binding_uuid,
                ),
            )
            retained_parameters = cursor.fetchone()
            cursor.execute(
                """
                UPDATE messenger_stream_bindings
                SET unread_count = 999, active_unread_count = 998,
                    passive_unread_count = 1,
                    last_message_uuid = %s
                WHERE project_id = %s AND stream_uuid = %s
                  AND user_uuid = %s
                """,
                (
                    provider_placement_uuid,
                    api.project_id,
                    mixed_stream["uuid"],
                    peer_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE messenger_user_topic_bindings
                SET unread_count = 999, active_unread_count = 999,
                    passive_unread_count = 0,
                    last_message_uuid = %s
                WHERE project_id = %s AND topic_uuid = %s
                  AND user_uuid = %s
                """,
                (
                    provider_placement_uuid,
                    api.project_id,
                    active_topic["uuid"],
                    peer_uuid,
                ),
            )
            cursor.execute(
                """
                UPDATE m_workspace_topic_message_stats_v1
                SET message_count = 999, last_ingest_sequence = 999
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (api.project_id, active_topic["uuid"]),
            )
            cursor.execute(
                """
                UPDATE messenger_user_folder_bindings
                SET unread_count = 999
                WHERE project_id = %s AND user_uuid = %s
                  AND folder_uuid = %s
                """,
                (api.project_id, peer_uuid, folder.json()["uuid"]),
            )
        db.commit()

        engine.apply_migration(ZULIP_MESSAGE_RESET_MIGRATION)

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM messenger_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    (SELECT count(*) FROM messenger_messages
                     WHERE project_id = %s
                       AND source_name = 'zulip'
                       AND source->>'kind' = 'zulip'),
                    (SELECT count(*) FROM m_workspace_messages
                     WHERE project_id = %s
                       AND source_name = 'zulip'
                       AND source->>'kind' = 'zulip')
                """,
                (
                    api.project_id,
                    provider_canonical_uuid,
                    api.project_id,
                    provider_legacy_uuid,
                    api.project_id,
                    api.project_id,
                ),
            )
            assert cursor.fetchone() == (False, False, 0, 0)

            cursor.execute(
                """
                SELECT binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count,
                       COALESCE(
                           placement.legacy_public_uuid,
                           placement.uuid
                       )
                FROM messenger_stream_bindings AS binding
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = binding.project_id
                 AND placement.uuid = binding.last_message_uuid
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = %s
                  AND binding.user_uuid = %s
                """,
                (api.project_id, mixed_stream["uuid"], peer_uuid),
            )
            assert cursor.fetchone() == (
                2,
                1,
                1,
                sys_uuid.UUID(passive_native.json()["uuid"]),
            )
            cursor.execute(
                """
                SELECT binding.unread_count,
                       binding.active_unread_count,
                       binding.passive_unread_count,
                       COALESCE(
                           placement.legacy_public_uuid,
                           placement.uuid
                       )
                FROM messenger_user_topic_bindings AS binding
                LEFT JOIN messenger_message_placements AS placement
                  ON placement.project_id = binding.project_id
                 AND placement.uuid = binding.last_message_uuid
                WHERE binding.project_id = %s
                  AND binding.topic_uuid = %s
                  AND binding.user_uuid = %s
                """,
                (api.project_id, active_topic["uuid"], peer_uuid),
            )
            assert cursor.fetchone() == (
                1,
                1,
                0,
                sys_uuid.UUID(active_native.json()["uuid"]),
            )
            cursor.execute(
                """
                SELECT binding.active, binding.membership_generation,
                       binding.role, binding.notification_mode,
                       topic_binding.notification_mode,
                       topic.is_done, item.folder_uuid, item.pinned_at
                FROM messenger_stream_bindings AS binding
                JOIN messenger_user_topic_bindings AS topic_binding
                  ON topic_binding.project_id = binding.project_id
                 AND topic_binding.user_uuid = binding.user_uuid
                 AND topic_binding.topic_uuid = %s
                JOIN messenger_topics AS topic
                  ON topic.project_id = topic_binding.project_id
                 AND topic.uuid = topic_binding.topic_uuid
                JOIN messenger_folder_items AS item
                  ON item.project_id = binding.project_id
                 AND item.user_uuid = binding.user_uuid
                 AND item.stream_uuid = binding.stream_uuid
                 AND item.folder_uuid = %s
                WHERE binding.project_id = %s
                  AND binding.uuid = %s
                """,
                (
                    active_topic["uuid"],
                    folder.json()["uuid"],
                    api.project_id,
                    peer_binding_uuid,
                ),
            )
            assert cursor.fetchone() == retained_parameters
            cursor.execute(
                """
                SELECT folder.unread_count,
                       item->>'stream_uuid',
                       (item->>'unread_count')::integer,
                       (item->>'active_unread_count')::integer,
                       (item->>'passive_unread_count')::integer
                FROM messenger_user_folder_bindings AS folder
                CROSS JOIN LATERAL jsonb_array_elements(
                    folder.folder_items_snapshot
                ) AS item
                WHERE folder.project_id = %s AND folder.user_uuid = %s
                  AND folder.folder_uuid = %s
                  AND item->>'stream_uuid' = %s
                """,
                (
                    api.project_id,
                    peer_uuid,
                    folder.json()["uuid"],
                    mixed_stream["uuid"],
                ),
            )
            assert cursor.fetchone() == (
                1,
                mixed_stream["uuid"],
                2,
                1,
                1,
            )
            cursor.execute(
                """
                SELECT stats.message_count,
                       reads.read_count,
                       account.projection_reset_generation,
                       account.status,
                       account.live_ready,
                       chat.status
                FROM m_workspace_topic_message_stats_v1 AS stats
                JOIN m_workspace_user_topic_read_stats_v1 AS reads
                  ON reads.project_id = stats.project_id
                 AND reads.topic_uuid = stats.topic_uuid
                 AND reads.user_uuid = %s
                CROSS JOIN m_external_accounts_v2 AS account
                JOIN m_external_chats_v2 AS chat
                  ON chat.external_account_uuid = account.uuid
                WHERE stats.project_id = %s AND stats.topic_uuid = %s
                  AND account.uuid = %s
                """,
                (
                    peer_uuid,
                    api.project_id,
                    active_topic["uuid"],
                    account_uuid,
                ),
            )
            assert cursor.fetchone() == (2, 1, 1, "backfill", False, "syncing")
            cursor.execute(
                """
                SELECT legacy.uuid, state.read_at IS NOT NULL
                FROM m_workspace_messages AS legacy
                JOIN messenger_message_placements AS placement
                  ON placement.project_id = legacy.project_id
                 AND COALESCE(
                        placement.legacy_public_uuid,
                        placement.uuid
                     ) = legacy.uuid
                JOIN messenger_user_message_states AS state
                  ON state.project_id = placement.project_id
                 AND state.placement_uuid = placement.uuid
                 AND state.user_uuid = %s
                WHERE legacy.project_id = %s
                  AND legacy.uuid IN (%s, %s, %s)
                ORDER BY legacy.ingest_sequence
                """,
                (
                    peer_uuid,
                    api.project_id,
                    read_native.json()["uuid"],
                    active_native.json()["uuid"],
                    passive_native.json()["uuid"],
                ),
            )
            assert cursor.fetchall() == [
                (sys_uuid.UUID(read_native.json()["uuid"]), True),
                (sys_uuid.UUID(active_native.json()["uuid"]), False),
                (sys_uuid.UUID(passive_native.json()["uuid"]), False),
            ]
            cursor.execute(
                "SELECT applied FROM ra_migrations WHERE uuid = %s",
                (ZULIP_MESSAGE_RESET_UUID,),
            )
            assert cursor.fetchone() == (True,)

        peer_stream = api.get(f"{STREAMS}{mixed_stream['uuid']}", user=peer_uuid)
        peer_active_topic = api.get(
            f"{STREAM_TOPICS}{active_topic['uuid']}", user=peer_uuid
        )
        peer_passive_topic = api.get(
            f"{STREAM_TOPICS}{passive_topic['uuid']}", user=peer_uuid
        )
        assert peer_stream.status_code == 200, peer_stream.text
        assert peer_active_topic.status_code == 200, peer_active_topic.text
        assert peer_passive_topic.status_code == 200, peer_passive_topic.text
        assert (
            peer_stream.json()["unread_count"],
            peer_stream.json()["active_unread_count"],
            peer_stream.json()["passive_unread_count"],
            peer_stream.json()["notification_mode"],
        ) == (2, 1, 1, "all_messages")
        assert (
            peer_active_topic.json()["unread_count"],
            peer_active_topic.json()["active_unread_count"],
            peer_active_topic.json()["passive_unread_count"],
            peer_active_topic.json()["notification_mode"],
            peer_active_topic.json()["is_done"],
        ) == (1, 1, 0, "follow", True)
        assert (
            peer_passive_topic.json()["unread_count"],
            peer_passive_topic.json()["active_unread_count"],
            peer_passive_topic.json()["passive_unread_count"],
            peer_passive_topic.json()["notification_mode"],
        ) == (1, 0, 1, "mute")
    finally:
        _truncate_messenger_test_data()
        engine.apply_migration(PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION)


def test_native_v2_migration_canonicalizes_legacy_provider_identity_links(api, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    api_store.configure_store_factory(
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
    )
    engine.rollback_migration(V2_MIGRATION)
    try:
        provider_realm_uuid = sys_uuid.UUID("11111111-2222-3333-4444-555555555555")
        provider_user_id = "20"
        legacy_namespace = sys_uuid.UUID("fda6f96e-c86d-5c94-976d-4e813e3f3655")
        legacy_user_uuid = sys_uuid.uuid5(
            legacy_namespace,
            f"zulip:{provider_realm_uuid}:{provider_user_id}",
        )
        canonical_user_uuid = sys_uuid.UUID("78eb4f94-6149-5204-840f-7db321cadb1d")
        bridge_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        chat_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        reaction_uuid = sys_uuid.uuid4()
        conftest.seed_workspace_user(
            db,
            legacy_user_uuid,
            f"legacy-zulip-{provider_user_id}",
        )
        stream_uuid = conftest.seed_user_stream(
            db, api.project_id, api.user_uuid, "Provider identity upgrade"
        )
        conftest.seed_user_stream_binding(
            db, api.project_id, stream_uuid, legacy_user_uuid
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "Native identity history",
            is_default=True,
        )
        chat_source = {
            "kind": "zulip",
            "chat_type": "channel",
            "provider_realm_uuid": str(provider_realm_uuid),
            "participants": [{"workspace_user_uuid": str(legacy_user_uuid)}],
            "topics": [],
        }
        desired_resource = {
            "resource_type": "external_chat_assignment",
            "uuid": str(chat_uuid),
            "generation": 1,
            "external_account_uuid": str(account_uuid),
            "project_id": api.project_id,
            "selected": True,
            "workspace_user_uuid": str(legacy_user_uuid),
        }
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_users
                SET source = 'zulip', provider_uuid = %s,
                    external_account_uuid = %s,
                    provider_external_id = %s
                WHERE uuid = %s
                """,
                (
                    sys_uuid.uuid4(),
                    account_uuid,
                    provider_user_id,
                    legacy_user_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
                VALUES (%s, 'zulip')
                """,
                (bridge_uuid,),
            )
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    provider_realm_uuid, provider_owner_user_id,
                    status, live_ready
                ) VALUES (
                    %s, %s, 'zulip', '{}'::jsonb, %s, '10', 'live', TRUE
                )
                """,
                (account_uuid, api.user_uuid, provider_realm_uuid),
            )
            cursor.executemany(
                """
                INSERT INTO m_external_provider_identity_links_v1 (
                    provider, provider_realm_uuid, provider_user_id,
                    workspace_user_uuid, link_kind
                ) VALUES ('zulip', %s, %s, %s, %s)
                """,
                (
                    (
                        provider_realm_uuid,
                        provider_user_id,
                        legacy_user_uuid,
                        "provider_identity",
                    ),
                    (
                        provider_realm_uuid,
                        "10",
                        api.user_uuid,
                        "verified_account_owner",
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected,
                    project_id, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                    'Identity upgrade chat', TRUE, %s, %s, 'live'
                )
                """,
                (
                    chat_uuid,
                    account_uuid,
                    api.user_uuid,
                    json.dumps(chat_source),
                    api.project_id,
                    stream_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_bridge_desired_resources_v1 (
                    bridge_instance_uuid, provider_kind, resource_type,
                    resource_uuid, operation, generation, resource
                ) VALUES (
                    %s, 'zulip', 'external_chat_assignment',
                    %s, 'upsert', 1, %s::jsonb
                )
                """,
                (bridge_uuid, chat_uuid, json.dumps(desired_resource)),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"native history"}'::jsonb,
                    'native', '{"kind":"native"}'::jsonb
                )
                """,
                (
                    message_uuid,
                    api.project_id,
                    stream_uuid,
                    topic_uuid,
                    legacy_user_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_message_reactions (
                    uuid, project_id, message_uuid, user_uuid, emoji_name,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'thumbs_up', NOW(), NOW())
                """,
                (
                    reaction_uuid,
                    api.project_id,
                    message_uuid,
                    legacy_user_uuid,
                ),
            )
        db.commit()

        engine.apply_migration(V2_MIGRATION)
        api_store.configure_store_factory(store_factory.build_store_factory())

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT provider_user_id, workspace_user_uuid, link_kind
                FROM m_external_provider_identity_links_v1
                WHERE provider = 'zulip' AND provider_realm_uuid = %s
                ORDER BY provider_user_id::bigint
                """,
                (provider_realm_uuid,),
            )
            assert cursor.fetchall() == [
                (
                    "10",
                    sys_uuid.UUID(api.user_uuid),
                    "verified_account_owner",
                ),
                ("20", canonical_user_uuid, "provider_identity"),
            ]
            cursor.execute(
                """
                SELECT
                    EXISTS(SELECT 1 FROM m_workspace_users WHERE uuid = %s),
                    (SELECT source FROM m_workspace_users WHERE uuid = %s),
                    (SELECT user_uuid FROM m_workspace_messages WHERE uuid = %s),
                    (SELECT author_uuid FROM messenger_messages WHERE uuid = %s),
                    (SELECT user_uuid FROM m_workspace_message_reactions
                     WHERE uuid = %s),
                    (SELECT user_uuid FROM messenger_message_reaction_facts
                     WHERE uuid = %s)
                """,
                (
                    legacy_user_uuid,
                    canonical_user_uuid,
                    message_uuid,
                    message_uuid,
                    reaction_uuid,
                    reaction_uuid,
                ),
            )
            assert cursor.fetchone() == (
                False,
                "zulip",
                canonical_user_uuid,
                canonical_user_uuid,
                canonical_user_uuid,
                canonical_user_uuid,
            )
            cursor.execute(
                """
                SELECT
                    position(%s in chat.source::text) = 0,
                    position(%s in chat.source::text) > 0,
                    bool_and(position(%s in desired.resource::text) = 0),
                    bool_and(position(%s in desired.resource::text) > 0),
                    bool_and(position(%s in change.resource::text) = 0),
                    bool_and(position(%s in change.resource::text) > 0)
                FROM m_external_chats_v2 AS chat
                JOIN m_external_bridge_desired_resources_v1 AS desired
                  ON desired.resource_uuid = chat.uuid
                JOIN m_external_bridge_desired_changes_v1 AS change
                  ON change.resource_uuid = chat.uuid
                WHERE chat.uuid = %s
                GROUP BY chat.source
                """,
                (
                    str(legacy_user_uuid),
                    str(canonical_user_uuid),
                    str(legacy_user_uuid),
                    str(canonical_user_uuid),
                    str(legacy_user_uuid),
                    str(canonical_user_uuid),
                    chat_uuid,
                ),
            )
            assert cursor.fetchone() == (True, True, True, True, True, True)
    finally:
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
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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
        sql_canonical_store.SQLCanonicalMessengerStoreFactory()
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


def test_native_v2_projection_failure_is_retryable_and_dead_letters(api, db):
    event_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'folder_projection', 'user-folder', 'invalid',
                '{"source_kind":"stream.created"}'::jsonb
            )
            """,
            (event_uuid, api.project_id),
        )
    with contexts.Context().session_manager() as session:
        v2_projection.derive_projection_tasks(session)
        assert v2_projection.process_one_projection_task(
            session,
            "integration:failure",
            max_attempts=2,
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, last_error
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        status, attempts, last_error = cursor.fetchone()
        assert (status, attempts) == ("failed", 1)
        assert "folder_uuid" in last_error
        cursor.execute(
            """
            UPDATE messenger_projection_tasks SET next_retry_at = NOW()
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:failure",
            max_attempts=2,
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, lease_owner
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = %s
            """,
            (api.project_id, event_uuid),
        )
        assert cursor.fetchone() == ("dead_letter", 2, None)


def test_native_v2_reclaims_an_expired_running_projection_task(api, db):
    folder = api.post(FOLDERS, json={"title": "Lease recovery"})
    assert folder.status_code == 201, folder.text
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, attempts
            FROM messenger_projection_tasks
            WHERE project_id = %s AND task_kind = 'folder_projection'
              AND payload->>'folder_uuid' = %s
            ORDER BY created_at DESC, uuid DESC
            LIMIT 1
            """,
            (api.project_id, folder.json()["uuid"]),
        )
        task_uuid, attempts = cursor.fetchone()
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET status = 'running', lease_owner = 'stale-worker',
                lease_expires_at = NOW() - interval '1 second'
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, task_uuid),
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:reaper",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempts, lease_owner
            FROM messenger_projection_tasks
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, task_uuid),
        )
        assert cursor.fetchone() == ("completed", attempts + 1, None)


def test_native_v2_coalesces_legacy_folder_snapshot_bursts(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Legacy folder snapshot burst",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream_uuid = stream_response.json()["uuid"]
    folder_response = api.post(FOLDERS, json={"title": "Legacy snapshot target"})
    assert folder_response.status_code == 201, folder_response.text
    folder_uuid = folder_response.json()["uuid"]
    _drain()
    event_uuids = [sys_uuid.uuid4() for _ in range(3)]
    scope_key = f"{api.project_id}:{api.user_uuid}:{folder_uuid}"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot_version
            FROM messenger_user_folder_bindings
            WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
            """,
            (api.project_id, api.user_uuid, folder_uuid),
        )
        snapshot_version = cursor.fetchone()[0]
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload
            ) VALUES (
                %s, %s, 'folder_projection', 'user-folder', %s, %s::jsonb
            )
            """,
            [
                (
                    event_uuid,
                    api.project_id,
                    scope_key,
                    json.dumps(
                        {
                            "source_kind": source_kind,
                            "user_uuid": api.user_uuid,
                            "stream_uuid": stream_uuid,
                            "folder_uuid": folder_uuid,
                        }
                    ),
                )
                for event_uuid, source_kind in zip(
                    event_uuids,
                    (
                        "legacy_message_state.updated",
                        "legacy_message_state.deleted",
                        "legacy_message_state.updated",
                    ),
                    strict=True,
                )
            ],
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 3
    competing_claim = psycopg.connect(conftest.TEST_DB_URL)
    try:
        with competing_claim.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s AND outbox_event_uuid = ANY(%s::uuid[])
                ORDER BY created_at DESC, uuid DESC
                LIMIT 1
                FOR UPDATE
                """,
                (api.project_id, event_uuids),
            )
            assert cursor.fetchone() is not None
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '100ms'")
            assert v2_projection.process_one_projection_task(
                session,
                "integration:legacy-folder-coalesce",
            )
    finally:
        competing_claim.rollback()
        competing_claim.close()
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:legacy-folder-coalesce",
        )
        assert not v2_projection.process_one_projection_task(
            session,
            "integration:legacy-folder-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (3, 2, 1)
        cursor.execute(
            """
            SELECT snapshot_version
            FROM messenger_user_folder_bindings
            WHERE project_id = %s AND user_uuid = %s AND folder_uuid = %s
            """,
            (api.project_id, api.user_uuid, folder_uuid),
        )
        assert cursor.fetchone()[0] == snapshot_version + 2


def test_native_v2_coalesces_snapshot_only_read_counter_bursts(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Read counter snapshot burst",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    _drain()
    event_uuids = [sys_uuid.uuid4() for _ in range(4)]
    scope_key = f"{api.project_id}:{api.user_uuid}:{stream['uuid']}"
    common_payload = {
        "user_uuid": api.user_uuid,
        "stream_uuid": stream["uuid"],
        "topic_uuid": stream["default_topic_uuid"],
    }
    payloads = [
        {**common_payload, "source_kind": "message.created"},
        {**common_payload, "source_kind": "message.updated"},
        {**common_payload, "source_kind": "stream_binding.created"},
        {
            **common_payload,
            "source_kind": "message.read",
            "placement_uuid": str(sys_uuid.uuid4()),
            "emit_message_read": True,
        },
    ]
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key,
                payload, created_at, updated_at
            ) VALUES (
                %s, %s, 'read_counters', 'user-stream', %s, %s::jsonb,
                NOW() + %s * INTERVAL '1 second',
                NOW() + %s * INTERVAL '1 second'
            )
            """,
            [
                (
                    event_uuid,
                    api.project_id,
                    scope_key,
                    json.dumps(payload),
                    position,
                    position,
                )
                for position, (event_uuid, payload) in enumerate(
                    zip(event_uuids, payloads, strict=True)
                )
            ],
        )
    with contexts.Context().session_manager() as session:
        assert v2_projection.derive_projection_tasks(session) == 4
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE messenger_projection_tasks
            SET created_at = CASE
                WHEN (payload->>'emit_message_read')::boolean
                    THEN NOW()
                ELSE NOW() - INTERVAL '1 hour'
            END
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
    competing_claim = psycopg.connect(conftest.TEST_DB_URL)
    try:
        with competing_claim.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid
                FROM messenger_projection_tasks
                WHERE project_id = %s
                  AND outbox_event_uuid = ANY(%s::uuid[])
                  AND COALESCE(
                      (payload->>'emit_message_read')::boolean,
                      FALSE
                  ) = FALSE
                ORDER BY created_at DESC, uuid DESC
                LIMIT 1
                FOR UPDATE
                """,
                (api.project_id, event_uuids),
            )
            assert cursor.fetchone() is not None
        with contexts.Context().session_manager() as session:
            session.execute("SET LOCAL lock_timeout = '100ms'")
            assert v2_projection.process_one_projection_task(
                session,
                "integration:read-counter-coalesce",
            )
    finally:
        competing_claim.rollback()
        competing_claim.close()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0),
                   count(*) FILTER (
                       WHERE status = 'pending'
                         AND (payload->>'emit_message_read')::boolean
                   )
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        # The overdue snapshot batch gets one bounded turn before the fresh
        # interactive read. Its unlocked sibling coalesces while the row held
        # by a competing claim remains pending without blocking this worker.
        assert cursor.fetchone() == (2, 2, 1, 3, 1)
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE status = 'pending'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (3, 1, 2, 2)
    with contexts.Context().session_manager() as session:
        assert v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
        assert not v2_projection.process_one_projection_task(
            session,
            "integration:read-counter-coalesce",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'completed'),
                   count(*) FILTER (WHERE attempts = 1),
                   count(*) FILTER (WHERE attempts = 0)
            FROM messenger_projection_tasks
            WHERE project_id = %s AND outbox_event_uuid = ANY(%s)
            """,
            (api.project_id, event_uuids),
        )
        assert cursor.fetchone() == (4, 3, 1)


def test_native_v2_fanout_supports_more_than_five_thousand_recipients(api, db):
    stream_response = api.post(
        STREAMS,
        json={
            "name": "Large native audience",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert stream_response.status_code == 201, stream_response.text
    stream = stream_response.json()
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(
            session,
            f"integration:{sys_uuid.uuid4()}",
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid,
                       value
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO m_workspace_users (
                uuid, username, source, status, avatar, last_ping_at,
                created_at, updated_at
            )
            SELECT user_uuid,
                   'fanout-' || value::text || '-' || %s::text,
                   'iam', 'active', 'urn:gravatar:' || md5(user_uuid::text),
                   NOW(), NOW(), NOW()
            FROM generated
            """,
            (api.project_id, api.project_id),
        )
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO messenger_project_users (project_id, user_uuid)
            SELECT %s, user_uuid FROM generated
            """,
            (api.project_id, api.project_id),
        )
        cursor.execute(
            """
            WITH generated AS (
                SELECT md5(%s::text || ':fanout-user:' || value::text)::uuid
                           AS user_uuid,
                       md5(%s::text || ':fanout-binding:' || value::text)::uuid
                           AS binding_uuid
                FROM generate_series(1, 5001) AS value
            )
            INSERT INTO messenger_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid,
                active, membership_generation, role, notification_mode,
                notification_updated_at, created_at, updated_at
            )
            SELECT binding_uuid, %s, %s, user_uuid, %s,
                   true, 1, 'member', 'all_messages', NOW(), NOW(), NOW()
            FROM generated
            """,
            (
                api.project_id,
                api.project_id,
                api.project_id,
                stream["uuid"],
                api.user_uuid,
            ),
        )
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "large fanout"},
        },
    )
    assert message.status_code == 201, message.text
    for _ in range(100):
        with contexts.Context().session_manager() as session:
            v2_projection.derive_projection_tasks(session)
            assert v2_projection.process_one_projection_task(
                session,
                f"integration:{sys_uuid.uuid4()}",
                fanout_batch_size=1000,
            )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM messenger_fanout_roots
                WHERE project_id = %s AND placement_uuid = %s
                """,
                (api.project_id, message.json()["uuid"]),
            )
            root = cursor.fetchone()
        if root is not None and root[0] == "completed":
            break
    assert root == ("completed",)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT count(*) FROM messenger_api_user_messages_v1
                 WHERE project_id = %s AND uuid = %s),
                (SELECT count(*) FROM messenger_fanout_batch_tasks
                 WHERE project_id = %s),
                (SELECT max(batch_size) FROM messenger_fanout_batch_tasks
                 WHERE project_id = %s),
                (SELECT bool_or(payload ? 'audience')
                 FROM messenger_domain_outbox_events
                 WHERE project_id = %s AND event_kind = 'fanout')
            """,
            (
                api.project_id,
                message.json()["uuid"],
                api.project_id,
                api.project_id,
                api.project_id,
            ),
        )
        message_count, batch_count, max_batch_size, embeds_audience = cursor.fetchone()
    assert message_count == 5002
    assert batch_count == 6
    assert max_batch_size == 1000
    assert embeds_audience is False


def test_rolling_provider_message_uses_realm_global_canonical_identity(api, db):
    stream = api.post(
        STREAMS,
        json={
            "name": "Provider rolling identity",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    topic = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "Provider topic",
            "source": {"kind": "native"},
        },
    ).json()
    realm_uuid = sys_uuid.uuid4()
    legacy_uuid = sys_uuid.uuid4()
    provider_message_id = "4242"
    canonical_uuid = sys_uuid.uuid5(realm_uuid, f"message:{provider_message_id}")
    placement_uuid = sys_uuid.uuid5(sys_uuid.UUID(topic["uuid"]), str(canonical_uuid))
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source, provider_uuid,
                external_account_uuid, provider_external_id,
                provider_metadata, delivery_metadata, reaction_users
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb, 'zulip', %s::jsonb,
                %s, %s, %s, %s::jsonb, '{}'::jsonb, '{}'::jsonb
            )
            """,
            (
                legacy_uuid,
                api.project_id,
                stream["uuid"],
                topic["uuid"],
                api.user_uuid,
                '{"kind":"markdown","content":"provider message"}',
                '{"kind":"zulip"}',
                sys_uuid.uuid4(),
                sys_uuid.uuid4(),
                provider_message_id,
                '{"provider_realm_uuid":"' + str(realm_uuid) + '"}',
            ),
        )
        cursor.execute(
            """
            SELECT message.uuid, message.provider_realm_uuid,
                   message.provider_message_id, placement.uuid,
                   placement.legacy_public_uuid
            FROM messenger_messages AS message
            JOIN messenger_message_placements AS placement
              ON placement.project_id = message.project_id
             AND placement.message_uuid = message.uuid
            WHERE message.project_id = %s AND message.uuid = %s
            """,
            (api.project_id, canonical_uuid),
        )
        row = cursor.fetchone()
        reaction_uuid = sys_uuid.uuid4()
        cursor.execute(
            """
            UPDATE messenger_user_message_states
            SET read_at = NOW(), starred = TRUE, pinned = TRUE,
                updated_at = NOW()
            WHERE project_id = %s AND placement_uuid = %s AND user_uuid = %s
            """,
            (
                api.project_id,
                placement_uuid,
                api.user_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO messenger_message_reaction_facts (
                uuid, project_id, canonical_message_uuid, placement_uuid,
                user_uuid, emoji_name
            ) VALUES (%s, %s, %s, %s, %s, 'thumbs_up')
            """,
            (
                reaction_uuid,
                api.project_id,
                canonical_uuid,
                placement_uuid,
                api.user_uuid,
            ),
        )
        cursor.execute(
            """
            SELECT flag.uuid, flag.read, flag.starred, flag.pinned,
                   reaction.message_uuid
            FROM m_workspace_user_message_flags AS flag
            JOIN m_workspace_message_reactions AS reaction
              ON reaction.uuid = %s
            WHERE flag.uuid = %s AND flag.user_uuid = %s
            """,
            (reaction_uuid, legacy_uuid, api.user_uuid),
        )
        legacy_state = cursor.fetchone()

    assert row == (
        canonical_uuid,
        realm_uuid,
        provider_message_id,
        placement_uuid,
        legacy_uuid,
    )
    assert legacy_state == (legacy_uuid, True, True, True, legacy_uuid)


def test_provider_backfill_move_source_counters_are_exact_and_idempotent(api, db):
    provider_event_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    recipients = [api.user_uuid, sys_uuid.uuid4()]

    for _attempt in range(2):
        with contexts.Context().session_manager() as session:
            provider_event_apply._schedule_backfill_move_source_counters(
                session,
                api.project_id,
                recipients,
                stream_uuid,
                topic_uuid,
                provider_event_uuid,
            )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT scope_kind, payload->>'user_uuid',
                   payload->>'stream_uuid', payload->>'topic_uuid'
            FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND payload->>'source_kind' = 'provider_backfill_message.moved'
            ORDER BY scope_kind, payload->>'user_uuid'
            """,
            (api.project_id,),
        )
        rows = cursor.fetchall()

    assert len(rows) == 4
    assert {row[0] for row in rows} == {"user-stream", "user-topic"}
    assert {row[1] for row in rows} == {str(value) for value in recipients}
    assert {row[2] for row in rows} == {str(stream_uuid)}
    assert {row[3] for row in rows} == {str(topic_uuid)}

    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND payload->>'source_kind' = 'provider_backfill_message.moved'
            """,
            (api.project_id,),
        )
    db.commit()

    for _attempt in range(2):
        with contexts.Context().session_manager() as session:
            provider_event_apply._schedule_backfill_move_source_counters(
                session,
                api.project_id,
                recipients,
                stream_uuid,
                topic_uuid,
                provider_event_uuid,
                include_stream=False,
            )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT scope_kind, payload->>'user_uuid'
            FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND payload->>'source_kind' = 'provider_backfill_message.moved'
            ORDER BY scope_kind, payload->>'user_uuid'
            """,
            (api.project_id,),
        )
        topic_only_rows = cursor.fetchall()

    assert len(topic_only_rows) == 2
    assert {row[0] for row in topic_only_rows} == {"user-topic"}
    assert {row[1] for row in topic_only_rows} == {str(value) for value in recipients}


def test_provider_v2_resolves_scope_and_identity_without_workspace_ids(api, db):
    stream = api.post(
        STREAMS,
        json={
            "name": "Provider v2",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    ).json()
    topic = api.post(
        STREAM_TOPICS,
        json={
            "stream_uuid": stream["uuid"],
            "name": "general",
            "source": {"kind": "native"},
        },
    ).json()
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    realm_uuid = sys_uuid.uuid4()
    provider_message_id = "101"
    source = {
        "kind": "zulip",
        "provider_realm_uuid": str(realm_uuid),
        "provider_owner_user_id": "1",
        "chat_type": "channel",
        "description": "",
        "private": False,
        "participants": [],
        "topics": [
            {
                "topic_uuid": topic["uuid"],
                "provider_topic_id": "42:general",
                "name": "general",
                "is_default": True,
            }
        ],
    }
    account_settings = {
        "kind": "zulip",
        "server_url": "https://provider.example.invalid",
        "selection_mode": "explicit",
        "history_depth": "30_days",
        "default_project_id": api.project_id,
    }
    account_resource = {
        "resource_type": "external_account",
        "uuid": str(account_uuid),
        "generation": 1,
        "owner_user_uuid": str(api.user_uuid),
        "settings": account_settings,
        "synchronization_enabled": True,
        "credential_envelope": {},
    }
    chat_resource = {
        "resource_type": "external_chat_assignment",
        "uuid": str(chat_uuid),
        "generation": 1,
        "external_account_uuid": str(account_uuid),
        "project_id": api.project_id,
        "selected": True,
        "history_depth": "30_days",
        "workspace_projection": {"stream": {"uuid": stream["uuid"]}},
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status,
                capabilities, last_heartbeat_at
            ) VALUES (%s, 'zulip', 1, 'active', '{}'::jsonb, NOW())
            """,
            (bridge_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready,
                provider_realm_uuid, provider_owner_user_id
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                FALSE, 'live', TRUE, %s, '1'
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(account_settings), realm_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                history_depth, projection_stream_uuid, status
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                'Provider v2', TRUE, %s, '30_days', %s, 'live'
            )
            """,
            (
                chat_uuid,
                account_uuid,
                api.user_uuid,
                json.dumps(source),
                api.project_id,
                stream["uuid"],
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_identity_links_v1 (
                provider, provider_realm_uuid, provider_user_id,
                workspace_user_uuid, link_kind
            ) VALUES ('zulip', %s, '1', %s, 'verified_account_owner')
            """,
            (realm_uuid, api.user_uuid),
        )
        for resource_type, resource_uuid, resource in (
            ("external_account", account_uuid, account_resource),
            ("external_chat_assignment", chat_uuid, chat_resource),
        ):
            cursor.execute(
                """
                INSERT INTO m_external_bridge_desired_resources_v1 (
                    bridge_instance_uuid, provider_kind, resource_type,
                    resource_uuid, operation, generation, resource
                ) VALUES (%s, 'zulip', %s, %s, 'upsert', 1, %s::jsonb)
                """,
                (bridge_uuid, resource_type, resource_uuid, json.dumps(resource)),
            )
    db.commit()
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    command = {
        "provider_event_key": "message:101:create",
        "delivery_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "provider_chat_key": "channel:42",
        "provider_sequence": "1",
        "delivery_class": "backfill",
        "kind": "message.upsert",
        "provider_object": {"kind": "message", "id": provider_message_id},
        "provider_references": {"topic": "42:general", "user": "1"},
        "payload": {
            "payload": {"kind": "markdown", "content": "Provider-native"},
            "read": False,
            "created_at": "2026-08-29T12:00:00Z",
            "provider_metadata": {
                "provider_original_url": "https://provider.invalid/#narrow/near/101",
            },
        },
    }
    with contexts.Context().session_manager() as session:
        response = provider_v2.apply_provider_command_batch(
            session,
            identity,
            [command],
        )

    expected_canonical_uuid = sys_uuid.uuid5(
        realm_uuid,
        f"message:{provider_message_id}",
    )
    expected_placement_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(topic["uuid"]),
        str(expected_canonical_uuid),
    )
    assert response == {
        "results": [
            {
                "provider_event_key": "message:101:create",
                "status": "applied",
                "target_uuid": str(expected_placement_uuid),
                "safe_error": None,
                "duplicate": False,
            }
        ]
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message.provider_realm_uuid, message.provider_message_id,
                   placement.uuid
            FROM messenger_messages AS message
            JOIN messenger_message_placements AS placement
              ON placement.project_id = message.project_id
             AND placement.message_uuid = message.uuid
            WHERE message.uuid = %s
            """,
            (expected_canonical_uuid,),
        )
        assert cursor.fetchone() == (
            realm_uuid,
            provider_message_id,
            expected_placement_uuid,
        )
        cursor.execute(
            """
            SELECT state.read_at,
                   COUNT(outbox.uuid) FILTER (
                       WHERE outbox.event_kind = 'read_counters'
                         AND outbox.payload->>'source_kind' =
                             'provider_message_state.updated'
                         AND outbox.payload->>'placement_uuid' =
                             state.placement_uuid::text
                   )
            FROM messenger_user_message_states AS state
            LEFT JOIN messenger_domain_outbox_events AS outbox
              ON outbox.project_id = state.project_id
            WHERE state.project_id = %s AND state.user_uuid = %s
              AND state.placement_uuid = %s
            GROUP BY state.read_at
            """,
            (api.project_id, api.user_uuid, expected_placement_uuid),
        )
        assert cursor.fetchone() == (None, 0)

    finalizer = {
        "provider_event_key": "history:channel:42:finalize:1",
        "delivery_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "provider_chat_key": "channel:42",
        "provider_sequence": None,
        "delivery_class": "backfill",
        "kind": "history.finalize",
        "provider_object": {"kind": "history", "id": "channel:42"},
        "provider_references": {},
        "payload": {"generation": 1},
    }
    with contexts.Context().session_manager() as session:
        finalized = provider_v2.apply_provider_command_batch(
            session,
            identity,
            [finalizer],
        )
    assert finalized["results"][0]["target_uuid"] == stream["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT scope_kind, COUNT(*)
            FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND payload->>'source_kind' = 'provider_history.finalized'
            GROUP BY scope_kind
            ORDER BY scope_kind
            """,
            (api.project_id,),
        )
        assert cursor.fetchall() == [("user-stream", 1), ("user-topic", 2)]
    public_message = api.get(f"{MESSAGES}{expected_placement_uuid}")
    assert public_message.status_code == 200, public_message.text
    assert public_message.json()["payload"]["content"] == "Provider-native"

    # Independent accounts in one verified realm can backfill the same chat.
    # Both deliveries must converge on the realm-global message while the
    # account that first materialized the projection remains its stable owner.
    alias_owner_uuid = sys_uuid.uuid4()
    alias_account_uuid = sys_uuid.uuid4()
    alias_chat_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, alias_owner_uuid, f"user-{alias_owner_uuid}")
    alias_account_resource = {
        **account_resource,
        "uuid": str(alias_account_uuid),
        "owner_user_uuid": str(alias_owner_uuid),
    }
    alias_chat_resource = {
        **chat_resource,
        "uuid": str(alias_chat_uuid),
        "external_account_uuid": str(alias_account_uuid),
    }
    alias_source = {**source, "provider_owner_user_id": "2"}
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready,
                provider_realm_uuid, provider_owner_user_id
                ) VALUES (
                    %s, %s, 'zulip', %s::jsonb,
                    TRUE, 'live', TRUE, %s, '2'
                )
            """,
            (
                alias_account_uuid,
                alias_owner_uuid,
                json.dumps(account_settings),
                realm_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                history_depth, projection_stream_uuid, status
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                'Provider v2 alias', TRUE, %s, '30_days', %s, 'live'
            )
            """,
            (
                alias_chat_uuid,
                alias_account_uuid,
                alias_owner_uuid,
                json.dumps(alias_source),
                api.project_id,
                stream["uuid"],
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_identity_links_v1 (
                provider, provider_realm_uuid, provider_user_id,
                workspace_user_uuid, link_kind
            ) VALUES ('zulip', %s, '2', %s, 'verified_account_owner')
            """,
            (realm_uuid, alias_owner_uuid),
        )
        for resource_type, resource_uuid, resource in (
            ("external_account", alias_account_uuid, alias_account_resource),
            ("external_chat_assignment", alias_chat_uuid, alias_chat_resource),
        ):
            cursor.execute(
                """
                INSERT INTO m_external_bridge_desired_resources_v1 (
                    bridge_instance_uuid, provider_kind, resource_type,
                    resource_uuid, operation, generation, resource
                ) VALUES (%s, 'zulip', %s, %s, 'upsert', 1, %s::jsonb)
                """,
                (bridge_uuid, resource_type, resource_uuid, json.dumps(resource)),
            )
        conftest.seed_user_stream_binding(
            db,
            api.project_id,
            stream["uuid"],
            alias_owner_uuid,
        )

    alias_command = {
        **command,
        "delivery_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(alias_account_uuid),
    }
    with contexts.Context().session_manager() as session:
        alias_response = provider_v2.apply_provider_command_batch(
            session,
            identity,
            [alias_command],
        )
    assert alias_response["results"][0] == {
        "provider_event_key": "message:101:create",
        "status": "applied",
        "target_uuid": str(expected_placement_uuid),
        "safe_error": None,
        "duplicate": False,
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM messenger_domain_outbox_events
            WHERE project_id = %s
              AND payload->>'source_kind' = 'provider_history.finalized'
            """,
            (api.project_id,),
        )
        cursor.execute(
            """
            UPDATE messenger_streams
            SET default_topic_uuid = NULL
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, stream["uuid"]),
        )
    db.commit()
    shared_finalizer = {
        **finalizer,
        "provider_event_key": "history:channel:42:finalize:shared-members",
        "delivery_uuid": str(sys_uuid.uuid4()),
    }
    with contexts.Context().session_manager() as session:
        shared_finalized = provider_v2.apply_provider_command_batch(
            session,
            identity,
            [shared_finalizer],
        )
    assert shared_finalized["results"][0]["target_uuid"] == stream["uuid"]
    expected_finalized_users = {str(api.user_uuid), str(alias_owner_uuid)}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload->>'user_uuid', payload->>'topic_uuid'
            FROM messenger_domain_outbox_events
            WHERE project_id = %s AND scope_kind = 'user-stream'
              AND payload->>'source_kind' = 'provider_history.finalized'
            """,
            (api.project_id,),
        )
        stream_targets = cursor.fetchall()
        assert {row[0] for row in stream_targets} == expected_finalized_users
        assert all(row[1] is None for row in stream_targets)
        cursor.execute(
            """
            SELECT DISTINCT payload->>'user_uuid'
            FROM messenger_domain_outbox_events
            WHERE project_id = %s AND scope_kind = 'user-topic'
              AND payload->>'source_kind' = 'provider_history.finalized'
            """,
            (api.project_id,),
        )
        assert {row[0] for row in cursor.fetchall()} == expected_finalized_users
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message.external_account_uuid,
                   message.provider_metadata->>'account_uuid',
                   EXISTS (
                       SELECT 1 FROM m_workspace_user_message_flags AS flags
                       WHERE flags.project_id = message.project_id
                         AND flags.uuid = message.uuid
                         AND flags.user_uuid = %s
                   )
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s AND message.uuid = %s
            """,
            (alias_owner_uuid, api.project_id, expected_placement_uuid),
        )
        assert cursor.fetchone() == (account_uuid, str(account_uuid), True)

        # A different chat in the same realm must not gain access merely by
        # pointing its assignment at the shared stream.
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET provider_chat_id = 'channel:43'
            WHERE uuid = %s
            """,
            (alias_chat_uuid,),
        )
    wrong_chat_command = {
        **alias_command,
        "provider_event_key": "message:101:wrong-chat",
        "delivery_uuid": str(sys_uuid.uuid4()),
        "provider_chat_key": "channel:43",
    }
    with contexts.Context().session_manager() as session:
        with pytest.raises(
            provider_data.ProviderBatchError,
            match="belongs to another account",
        ):
            provider_v2.apply_provider_command_batch(
                session,
                identity,
                [wrong_chat_command],
            )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET provider_chat_id = 'channel:42'
            WHERE uuid = %s
            """,
            (alias_chat_uuid,),
        )

    reaction_object = {"kind": "reaction", "id": "101:1:thumbs_up"}
    reaction_references = {"message": provider_message_id, "user": "1"}
    reaction_payload = {"emoji_name": "thumbs_up"}
    reaction_state_key = "reaction:101:1:thumbs_up:present"

    def apply_reaction(kind, event_key):
        reaction_command = {
            "provider_event_key": event_key,
            "delivery_uuid": str(sys_uuid.uuid4()),
            "external_account_uuid": str(account_uuid),
            "provider_chat_key": "channel:42",
            "provider_sequence": None,
            "kind": kind,
            "provider_object": reaction_object,
            "provider_references": reaction_references,
            "payload": reaction_payload,
        }
        with contexts.Context().session_manager() as session:
            return provider_v2.apply_provider_command_batch(
                session,
                identity,
                [reaction_command],
            )

    def reaction_count():
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM m_workspace_message_reactions
                WHERE project_id = %s AND message_uuid = %s
                  AND user_uuid = %s AND emoji_name = 'thumbs_up'
                """,
                (api.project_id, expected_placement_uuid, api.user_uuid),
            )
            return cursor.fetchone()[0]

    first_add = apply_reaction("reaction.upsert", reaction_state_key)
    assert first_add["results"][0]["status"] == "applied"
    assert reaction_count() == 1

    remove = apply_reaction(
        "reaction.delete",
        "reaction:101:1:thumbs_up:absent",
    )
    assert remove["results"][0]["status"] == "applied"
    assert reaction_count() == 0

    repeated_add = apply_reaction("reaction.upsert", reaction_state_key)
    assert repeated_add["results"][0]["status"] == "applied"
    assert repeated_add["results"][0]["provider_event_key"] == reaction_state_key
    assert reaction_count() == 1
