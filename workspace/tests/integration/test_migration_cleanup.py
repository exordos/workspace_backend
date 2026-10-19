# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import hashlib
import threading
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts as ra_contexts
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.messenger_api.api import context as messenger_context
from workspace.tests.integration.legacy_messenger import helpers as messenger_dm_helpers
from migrations import legacy_messenger_read_state as read_state
from workspace.tests.integration import conftest

pytestmark = pytest.mark.pre_retirement_schema


@pytest.fixture(scope="module", autouse=True)
def _historical_database(_database):
    with conftest.isolated_migration_database(
        PER_USER_DELIVERY_REPAIR_MIGRATION_FILE
    ) as database_url:
        yield database_url


@pytest.fixture
def db(_historical_database):
    with psycopg.connect(_historical_database, autocommit=True) as connection:
        yield connection


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
EXTERNAL_STREAM_SNAPSHOT_MIGRATION_FILE = (
    "0184-allow-external-stream-snapshot-visibility-51c501.py"
)
PER_USER_DELIVERY_REPAIR_MIGRATION_UUID = "e1f5ca44-b5b5-4bdc-bfdd-bf5996a34b4f"
PER_USER_DELIVERY_REPAIR_MIGRATION_FILE = (
    "0185-Repair-per-user-operation-delivery-projections-e1f5ca.py"
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
EXTERNAL_CONTENT_INDEX_MIGRATION_UUID = "0bb3cac3-2f35-44a1-9cca-b91886bfa0da"
EXTERNAL_CONTENT_INDEX_MIGRATION_FILE = (
    "0131-index-reusable-external-file-content-0bb3ca.py"
)
UNREAD_FOLDER_PROJECTION_MIGRATION_UUID = "93849688-bd14-40b1-8703-12e5ebe13e6b"
UNREAD_FOLDER_PROJECTION_MIGRATION_FILE = (
    "0132-optimize-unread-folder-projections-938496.py"
)
NOTIFICATION_TIMESTAMPS_MIGRATION_UUID = "52d0f82b-e692-4368-b004-f9263a1f3709"
TOPIC_SUMMARY_REASONING_MIGRATION_UUID = "b9d39435-f461-45b8-aabb-771061953c15"
COMPACT_READ_STATE_MIGRATION_UUID = "e84da8dc-97f6-4b10-bce7-f9652c0207a3"
COMPACT_READ_STATE_MIGRATION_FILE = "0134-add-compact-workspace-unread-state-e84da8.py"
COMPACT_READ_STATE_INDEX_MIGRATION_UUID = "b469650b-f613-4f57-869a-1dd7f6f373c3"
COMPACT_READ_STATE_INDEX_MIGRATION_FILE = (
    "0135-add-resumable-compact-unread-indexes-b46965.py"
)
LAZY_PROVIDER_READ_MIGRATION_UUID = "e5b13624-7b61-4623-9081-61a2e51afd92"
LAZY_PROVIDER_READ_MIGRATION_FILE = "0136-add-lazy-provider-read-snapshots-e5b136.py"
PROVIDER_READ_LEASE_FENCE_MIGRATION_UUID = "dfc77921-c0d9-4d1e-b919-b360bc1f2b94"
PROVIDER_READ_LEASE_FENCE_MIGRATION_FILE = (
    "0137-fence-lazy-provider-read-leases-dfc779.py"
)
PROVIDER_READ_ROLLING_FENCE_MIGRATION_UUID = "1b0b0164-4d20-4d6a-9991-26a13b1a4d60"
PROVIDER_READ_ROLLING_FENCE_MIGRATION_FILE = (
    "0138-harden-lazy-provider-read-rolling-fences-1b0b01.py"
)
PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID = "1bca8f2b-147f-4af8-b6e4-8078a3be253b"
PROJECT_DENSE_READ_SEQUENCE_MIGRATION_FILE = (
    "0139-densify-project-read-sequences-1bca8f.py"
)
PROVIDER_HISTORY_DOWNGRADE_MIGRATION_UUID = "68c9b8f1-d900-46db-b395-b514499698df"
PROVIDER_HISTORY_DOWNGRADE_MIGRATION_FILE = (
    "0140-add-resumable-provider-read-downgrade-68c9b8.py"
)
READ_STATE_FORWARD_CORRECTION_MIGRATION_UUID = "60f5cad2-fe10-4df3-bced-2a248497afd1"
READ_STATE_FORWARD_CORRECTION_MIGRATION_FILE = (
    "0141-forward-correct-published-read-state-migrations-60f5ca.py"
)
COMPACT_DENSE_PREPARATION_MIGRATION_UUID = "0c93a123-8205-43cf-93dc-29031e06f2a7"
COMPACT_DENSE_PREPARATION_MIGRATION_FILE = (
    "0142-prepare-compact-dense-sequence-upgrade-0c93a1.py"
)
COMPACT_DENSE_JOIN_MIGRATION_UUID = "1ce3ae70-7ad1-447b-a7ca-e14318e38f98"
COMPACT_DENSE_JOIN_MIGRATION_FILE = (
    "0143-join-published-dense-sequence-upgrade-1ce3ae.py"
)
PROVIDER_READ_PAGING_CAPABILITY_MIGRATION_UUID = "523aa199-3aad-4678-b915-5b5439bb9f85"
PROVIDER_READ_PAGING_CAPABILITY_MIGRATION_FILE = (
    "0144-use-additive-provider-read-paging-capability-523aa1.py"
)
READ_STATE_MAINTENANCE_INDEX_MIGRATION_UUID = "8e2468e1-ea6e-4611-9d6e-266917e6c64e"
ROLLING_READ_STATE_PROJECT_MIGRATION_UUID = "4c8dc326-40db-4045-addb-bb8ac4d472c5"
ROLLING_READ_STATE_PROJECT_MIGRATION_FILE = (
    "0146-register-rolling-read-state-projects-4c8dc3.py"
)
READ_STATE_INDEX_REPAIR_MIGRATION_UUID = "804f7723-4d44-4d32-914e-3f9dfe90eee1"
READ_STATE_INDEX_REPAIR_MIGRATION_FILE = (
    "0147-repair-read-state-maintenance-indexes-804f77.py"
)
TOPIC_SUMMARY_REASONING_JOIN_MIGRATION_UUID = "4588d689-bb04-4599-8ab4-ade40e386548"
TOPIC_SUMMARY_REASONING_JOIN_MIGRATION_FILE = (
    "0148-join-topic-summary-reasoning-head-4588d6.py"
)
UNREAD_BRANCH_MIGRATION_UUID = "c84ae9cb-d3c1-4385-88b8-0b2c156d2cb5"
UNREAD_BRANCH_MIGRATION_FILE = (
    "0149-split-messenger-unread-read-state-branches-c84ae9.py"
)
COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_UUID = "7433535e-646d-4557-8f7e-5688aae458db"
COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_FILE = (
    "0151-index-detached-compact-read-memberships-743353.py"
)
MESSENGER_V2_MIGRATION_UUID = "b59d875a-561f-4166-8198-331c23bc89fb"
MESSENGER_V2_MIGRATION_FILE = "0152-add-messenger-v2-canonical-model-b59d87.py"
EXTERNAL_BRIDGE_SNAPSHOT_PAGING_MIGRATION_UUID = "75ad6f73-4ed6-43b5-9cb2-f853a82957da"
EXTERNAL_BRIDGE_SNAPSHOT_PAGING_MIGRATION_FILE = (
    "0153-page-external-bridge-snapshots-75ad6f.py"
)
SHARED_PROJECTION_RECOVERY_MIGRATION_UUID = "603fd077-99da-421a-baf6-2b3abc6312ee"
SHARED_PROJECTION_RECOVERY_MIGRATION_FILE = (
    "0154-retry-shared-provider-projections-603fd0.py"
)
MESSENGER_V2_PREPARATION_MIGRATION_UUID = "8870659b-eeb7-4e1c-9f3a-d84ff25dea96"
MESSENGER_V2_PREPARATION_MIGRATION_FILE = (
    "0155-prepare-immutable-messenger-v2-cutover-887065.py"
)
RETAINED_PROVIDER_IDENTITY_MIGRATION_UUID = "2022d56e-484d-4047-8e65-f37c65da229d"
RETAINED_PROVIDER_IDENTITY_MIGRATION_FILE = (
    "0156-repair-retained-provider-identities-2022d5.py"
)
ZULIP_PROJECTION_RESET_MIGRATION_UUID = "9a596b13-a187-45d6-8da6-d3b5d39a5c85"
ZULIP_PROJECTION_RESET_MIGRATION_FILE = "0157-reset-zulip-projections-9a596b.py"
ZULIP_MESSAGE_RESET_MIGRATION_UUID = "c1e8bf60-ff3c-4027-9b8c-410bec2c959d"
ZULIP_MESSAGE_RESET_MIGRATION_FILE = "0158-reset-Zulip-message-projections-c1e8bf.py"
PROJECTION_CLAIM_INDEX_MIGRATION_UUID = "16837bac-76d7-4e28-8b70-2f7739c9eb24"
PROJECTION_CLAIM_INDEX_MIGRATION_FILE = (
    "0159-index-Messenger-v2-projection-claim-order-16837b.py"
)
INTERACTIVE_READ_INDEX_MIGRATION_UUID = "259cc21a-d775-4d90-98e5-6fde45181e3f"
INTERACTIVE_READ_INDEX_MIGRATION_FILE = (
    "0160-repair-native-read-state-and-prioritize-reads-259cc2.py"
)
PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_UUID = "d0643389-d3dd-410a-a57c-5c1fab7691df"
PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_FILE = (
    "0161-Unblock-interleaved-provider-read-pages-d06433.py"
)
PROVIDER_OWNER_READ_REPAIR_MIGRATION_UUID = "785e0630-5d35-4324-bd53-4a03ce63c08c"
PROVIDER_OWNER_READ_REPAIR_MIGRATION_FILE = (
    "0162-repair-provider-owner-read-state-785e06.py"
)
DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_UUID = (
    "86794513-1402-47ed-b2a5-3f3e9aa05577"
)
DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_FILE = (
    "0163-repair-duplicate-provider-projections-867945.py"
)
PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_UUID = "f8bd035a-0e32-46ee-aa57-7f9ff1599640"
PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_FILE = (
    "0164-stabilize-provider-private-chat-labels-f8bd03.py"
)
PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_UUID = (
    "73514c7e-4cd6-496a-be60-1896107e7087"
)
PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_FILE = (
    "0165-repair-provider-participant-message-state-73514c.py"
)
V2_COMPATIBILITY_READ_REPAIR_MIGRATION_UUID = "95c09d2a-38f1-46ed-9115-cbe860d63fd6"
V2_COMPATIBILITY_READ_REPAIR_MIGRATION_FILE = (
    "0166-repair-v2-compatibility-read-state-95c09d.py"
)
COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_UUID = "303da967-7711-4781-9d38-8aefef13c82d"
COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_FILE = (
    "0167-reconcile-compact-topic-read-stats-303da9.py"
)
COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_UUID = (
    "7b45baf3-8dc5-4fb2-b8bf-d73985af14c6"
)
COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_FILE = (
    "0168-reconcile-compact-topic-message-stats-7b45ba.py"
)
PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_UUID = "485d6246-c577-4564-83d7-da856015b8d2"
PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_FILE = (
    "0169-prefer-provider-chat-labels-485d62.py"
)
PROVIDER_CHAT_OWNER_LABEL_MIGRATION_UUID = "90d43c8c-5d0f-4c84-99f3-9cda3f1f504a"
PROVIDER_CHAT_OWNER_LABEL_MIGRATION_FILE = (
    "0170-accept-provider-chat-owner-labels-90d43c.py"
)
CANCELLED_PROVIDER_READ_MIGRATION_UUID = "87ed2ec4-bcb3-4c5a-8ca4-904c3a998014"
CANCELLED_PROVIDER_READ_MIGRATION_FILE = (
    "0171-discard-cancelled-provider-read-snapshots-87ed2e.py"
)
EXPIRED_PROVIDER_READ_RETRY_MIGRATION_UUID = "05d036bd-5dd8-49e1-8e37-f8b3b93939c6"
EXPIRED_PROVIDER_READ_RETRY_MIGRATION_FILE = (
    "0172-retry-expired-provider-read-pages-05d036.py"
)
PROJECTION_ACCELERATION_MIGRATION_UUID = "8cda92b7-3a84-47f5-939b-11222441b0ff"
PROJECTION_ACCELERATION_MIGRATION_FILE = (
    "0173-accelerate-Messenger-v2-projections-8cda92.py"
)
LEGACY_BACKFILL_COUNTER_MIGRATION_UUID = "a2cd99ae-7165-4885-9889-f7729d74e45c"
LEGACY_BACKFILL_COUNTER_MIGRATION_FILE = (
    "0174-suppress-legacy-backfill-counters-a2cd99.py"
)
COMPACT_LEGACY_GAP_REPAIR_MIGRATION_UUID = "8e694871-17e9-4510-941d-c576aee5c2b4"
COMPACT_LEGACY_GAP_REPAIR_MIGRATION_FILE = (
    "0150-fence-compact-unread-legacy-gaps-8e6948.py"
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


def _set_historical_schema_fixture_before_forward_only_join(db):
    """Expose the published 0141 state for migration tests only.

    Production rollback is intentionally forbidden. Test databases start from
    HEAD, so historical-schema cases must adjust only RestAlchemy bookkeeping
    after asserting that the compatibility join left no staging relation.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass(staging_name)
            FROM unnest(%s::text[]) AS staging_name
            """,
            (
                [
                    "m_workspace_dense_compact_projects_v1",
                    "m_workspace_dense_membership_boundaries_v1",
                    "m_workspace_dense_read_messages_v1",
                ],
            ),
        )
        assert cur.fetchall() == [(None,), (None,), (None,)]
        cur.execute(
            """
            UPDATE ra_migrations
            SET applied = FALSE
            WHERE uuid = ANY(%s::text[])
            """,
            (
                [
                    COMPACT_DENSE_PREPARATION_MIGRATION_UUID,
                    COMPACT_DENSE_JOIN_MIGRATION_UUID,
                ],
            ),
        )


def _restore_current_provider_read_lease_fence(engine):
    """Restore the latest function after a historical owner is reapplied."""
    migration = engine._load_migrations()[PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_FILE]
    with ra_contexts.Context().session_manager() as session:
        migration.upgrade(session)


def test_published_messenger_v2_migration_is_immutable_and_joined_at_head():
    published_bytes = (
        conftest.MIGRATIONS_DIR / MESSENGER_V2_MIGRATION_FILE
    ).read_bytes()
    assert hashlib.sha256(published_bytes).hexdigest() == (
        "017f98bd8cf93c67feadd5900bfd7c15d0a45f6c121ce8e53c8ba97c2840d9d2"
    )

    migrations = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )._load_migrations()
    assert migrations[MESSENGER_V2_PREPARATION_MIGRATION_FILE]._depends == [
        COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_FILE
    ]
    assert migrations[RETAINED_PROVIDER_IDENTITY_MIGRATION_FILE]._depends == [
        MESSENGER_V2_PREPARATION_MIGRATION_FILE,
        SHARED_PROJECTION_RECOVERY_MIGRATION_FILE,
    ]
    assert migrations[ZULIP_PROJECTION_RESET_MIGRATION_FILE]._depends == [
        RETAINED_PROVIDER_IDENTITY_MIGRATION_FILE,
    ]
    assert migrations[ZULIP_MESSAGE_RESET_MIGRATION_FILE]._depends == [
        ZULIP_PROJECTION_RESET_MIGRATION_FILE,
    ]
    assert migrations[PROJECTION_CLAIM_INDEX_MIGRATION_FILE]._depends == [
        ZULIP_MESSAGE_RESET_MIGRATION_FILE,
    ]
    assert migrations[INTERACTIVE_READ_INDEX_MIGRATION_FILE]._depends == [
        PROJECTION_CLAIM_INDEX_MIGRATION_FILE,
    ]
    assert migrations[PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_FILE]._depends == [
        INTERACTIVE_READ_INDEX_MIGRATION_FILE,
    ]
    assert migrations[PROVIDER_OWNER_READ_REPAIR_MIGRATION_FILE]._depends == [
        PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_FILE,
    ]
    assert migrations[DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_FILE]._depends == [
        PROVIDER_OWNER_READ_REPAIR_MIGRATION_FILE
    ]
    assert migrations[PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_FILE]._depends == [
        DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_FILE,
    ]
    assert migrations[PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_FILE]._depends == [
        PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_FILE
    ]
    assert migrations[V2_COMPATIBILITY_READ_REPAIR_MIGRATION_FILE]._depends == [
        PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_FILE
    ]
    assert migrations[COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_FILE]._depends == [
        V2_COMPATIBILITY_READ_REPAIR_MIGRATION_FILE
    ]
    assert migrations[COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_FILE]._depends == [
        COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_FILE
    ]
    assert migrations[PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_FILE]._depends == [
        COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_FILE
    ]
    assert migrations[PROVIDER_CHAT_OWNER_LABEL_MIGRATION_FILE]._depends == [
        PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_FILE
    ]
    assert migrations[CANCELLED_PROVIDER_READ_MIGRATION_FILE]._depends == [
        PROVIDER_CHAT_OWNER_LABEL_MIGRATION_FILE
    ]
    assert migrations[EXPIRED_PROVIDER_READ_RETRY_MIGRATION_FILE]._depends == [
        CANCELLED_PROVIDER_READ_MIGRATION_FILE
    ]
    assert migrations[PROJECTION_ACCELERATION_MIGRATION_FILE]._depends == [
        EXPIRED_PROVIDER_READ_RETRY_MIGRATION_FILE
    ]
    assert migrations[LEGACY_BACKFILL_COUNTER_MIGRATION_FILE]._depends == [
        PROJECTION_ACCELERATION_MIGRATION_FILE
    ]
    assert migrations[PER_USER_DELIVERY_REPAIR_MIGRATION_FILE]._depends == [
        EXTERNAL_STREAM_SNAPSHOT_MIGRATION_FILE
    ]


def test_published_cleanup_migrations_remain_applied(_database, db):
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
                    EXTERNAL_CONTENT_INDEX_MIGRATION_UUID,
                    UNREAD_FOLDER_PROJECTION_MIGRATION_UUID,
                    NOTIFICATION_TIMESTAMPS_MIGRATION_UUID,
                    TOPIC_SUMMARY_REASONING_MIGRATION_UUID,
                    COMPACT_READ_STATE_MIGRATION_UUID,
                    COMPACT_READ_STATE_INDEX_MIGRATION_UUID,
                    LAZY_PROVIDER_READ_MIGRATION_UUID,
                    PROVIDER_READ_LEASE_FENCE_MIGRATION_UUID,
                    PROVIDER_READ_ROLLING_FENCE_MIGRATION_UUID,
                    PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID,
                    PROVIDER_HISTORY_DOWNGRADE_MIGRATION_UUID,
                    READ_STATE_FORWARD_CORRECTION_MIGRATION_UUID,
                    COMPACT_DENSE_PREPARATION_MIGRATION_UUID,
                    COMPACT_DENSE_JOIN_MIGRATION_UUID,
                    PROVIDER_READ_PAGING_CAPABILITY_MIGRATION_UUID,
                    READ_STATE_MAINTENANCE_INDEX_MIGRATION_UUID,
                    ROLLING_READ_STATE_PROJECT_MIGRATION_UUID,
                    READ_STATE_INDEX_REPAIR_MIGRATION_UUID,
                    TOPIC_SUMMARY_REASONING_JOIN_MIGRATION_UUID,
                    UNREAD_BRANCH_MIGRATION_UUID,
                    COMPACT_LEGACY_GAP_REPAIR_MIGRATION_UUID,
                    COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_UUID,
                    MESSENGER_V2_MIGRATION_UUID,
                    EXTERNAL_BRIDGE_SNAPSHOT_PAGING_MIGRATION_UUID,
                    SHARED_PROJECTION_RECOVERY_MIGRATION_UUID,
                    MESSENGER_V2_PREPARATION_MIGRATION_UUID,
                    RETAINED_PROVIDER_IDENTITY_MIGRATION_UUID,
                    ZULIP_PROJECTION_RESET_MIGRATION_UUID,
                    ZULIP_MESSAGE_RESET_MIGRATION_UUID,
                    PROJECTION_CLAIM_INDEX_MIGRATION_UUID,
                    INTERACTIVE_READ_INDEX_MIGRATION_UUID,
                    PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_UUID,
                    PROVIDER_OWNER_READ_REPAIR_MIGRATION_UUID,
                    DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_UUID,
                    PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_UUID,
                    PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_UUID,
                    V2_COMPATIBILITY_READ_REPAIR_MIGRATION_UUID,
                    COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_UUID,
                    COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_UUID,
                    PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_UUID,
                    PROVIDER_CHAT_OWNER_LABEL_MIGRATION_UUID,
                    CANCELLED_PROVIDER_READ_MIGRATION_UUID,
                    EXPIRED_PROVIDER_READ_RETRY_MIGRATION_UUID,
                    PROJECTION_ACCELERATION_MIGRATION_UUID,
                    LEGACY_BACKFILL_COUNTER_MIGRATION_UUID,
                    PER_USER_DELIVERY_REPAIR_MIGRATION_UUID,
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
            (EXTERNAL_CONTENT_INDEX_MIGRATION_UUID, True),
            (UNREAD_FOLDER_PROJECTION_MIGRATION_UUID, True),
            (NOTIFICATION_TIMESTAMPS_MIGRATION_UUID, True),
            (TOPIC_SUMMARY_REASONING_MIGRATION_UUID, True),
            (COMPACT_READ_STATE_MIGRATION_UUID, True),
            (COMPACT_READ_STATE_INDEX_MIGRATION_UUID, True),
            (LAZY_PROVIDER_READ_MIGRATION_UUID, True),
            (PROVIDER_READ_LEASE_FENCE_MIGRATION_UUID, True),
            (PROVIDER_READ_ROLLING_FENCE_MIGRATION_UUID, True),
            (PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID, True),
            (PROVIDER_HISTORY_DOWNGRADE_MIGRATION_UUID, True),
            (READ_STATE_FORWARD_CORRECTION_MIGRATION_UUID, True),
            (COMPACT_DENSE_PREPARATION_MIGRATION_UUID, True),
            (COMPACT_DENSE_JOIN_MIGRATION_UUID, True),
            (PROVIDER_READ_PAGING_CAPABILITY_MIGRATION_UUID, True),
            (READ_STATE_MAINTENANCE_INDEX_MIGRATION_UUID, True),
            (ROLLING_READ_STATE_PROJECT_MIGRATION_UUID, True),
            (READ_STATE_INDEX_REPAIR_MIGRATION_UUID, True),
            (TOPIC_SUMMARY_REASONING_JOIN_MIGRATION_UUID, True),
            (UNREAD_BRANCH_MIGRATION_UUID, True),
            (COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_UUID, True),
            (COMPACT_LEGACY_GAP_REPAIR_MIGRATION_UUID, True),
            (MESSENGER_V2_MIGRATION_UUID, True),
            (EXTERNAL_BRIDGE_SNAPSHOT_PAGING_MIGRATION_UUID, True),
            (SHARED_PROJECTION_RECOVERY_MIGRATION_UUID, True),
            (MESSENGER_V2_PREPARATION_MIGRATION_UUID, True),
            (RETAINED_PROVIDER_IDENTITY_MIGRATION_UUID, True),
            (ZULIP_PROJECTION_RESET_MIGRATION_UUID, True),
            (ZULIP_MESSAGE_RESET_MIGRATION_UUID, True),
            (PROJECTION_CLAIM_INDEX_MIGRATION_UUID, True),
            (INTERACTIVE_READ_INDEX_MIGRATION_UUID, True),
            (PROVIDER_READ_PAGE_UNBLOCK_MIGRATION_UUID, True),
            (PROVIDER_OWNER_READ_REPAIR_MIGRATION_UUID, True),
            (DUPLICATE_PROVIDER_PROJECTION_REPAIR_MIGRATION_UUID, True),
            (PROVIDER_PRIVATE_CHAT_LABEL_MIGRATION_UUID, True),
            (PROVIDER_PARTICIPANT_STATE_REPAIR_MIGRATION_UUID, True),
            (V2_COMPATIBILITY_READ_REPAIR_MIGRATION_UUID, True),
            (COMPACT_TOPIC_READ_STAT_REPAIR_MIGRATION_UUID, True),
            (COMPACT_TOPIC_MESSAGE_STAT_REPAIR_MIGRATION_UUID, True),
            (PROVIDER_CHAT_LABEL_PREFERENCE_MIGRATION_UUID, True),
            (PROVIDER_CHAT_OWNER_LABEL_MIGRATION_UUID, True),
            (CANCELLED_PROVIDER_READ_MIGRATION_UUID, True),
            (EXPIRED_PROVIDER_READ_RETRY_MIGRATION_UUID, True),
            (PROJECTION_ACCELERATION_MIGRATION_UUID, True),
            (LEGACY_BACKFILL_COUNTER_MIGRATION_UUID, True),
            (PER_USER_DELIVERY_REPAIR_MIGRATION_UUID, True),
        }
        cur.execute(
            """
            SELECT index.indisvalid, index.indisready,
                   pg_get_indexdef(index.indexrelid)
            FROM pg_index AS index
            WHERE index.indexrelid =
                'messenger_projection_tasks_active_created_idx'::regclass
            """
        )
        index_valid, index_ready, index_definition = cur.fetchone()
        assert index_valid is True
        assert index_ready is True
        assert "(created_at, uuid)" in index_definition
        assert "status" in index_definition
        assert "completed" in index_definition
        assert "dead_letter" in index_definition
        cur.execute(
            """
            SELECT index.indisvalid, index.indisready,
                   pg_get_indexdef(index.indexrelid)
            FROM pg_index AS index
            WHERE index.indexrelid =
                'messenger_projection_tasks_interactive_read_idx'::regclass
            """
        )
        index_valid, index_ready, index_definition = cur.fetchone()
        assert index_valid is True
        assert index_ready is True
        assert "(created_at, uuid)" in index_definition
        assert "read_counters" in index_definition
        assert "topic.read" in index_definition
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indisvalid, indisready,
                   pg_get_indexdef(indexrelid)
            FROM pg_index
            WHERE indexrelid IN (
                'messenger_domain_outbox_events_kind_created_idx'::regclass,
                'messenger_projection_tasks_background_created_idx'::regclass,
                'messenger_projection_tasks_fair_claim_idx'::regclass,
                'messenger_projection_tasks_global_partition_idx'::regclass,
                'messenger_projection_tasks_user_partition_idx'::regclass,
                'messenger_message_reaction_facts_snapshot_idx'::regclass
            )
            ORDER BY indexrelid::regclass::text
            """
        )
        acceleration_indexes = cur.fetchall()
        assert len(acceleration_indexes) == 6
        assert all(
            valid and ready for _name, valid, ready, _definition in acceleration_indexes
        )
        definitions = {
            name: definition
            for name, _valid, _ready, definition in acceleration_indexes
        }
        assert (
            "(event_kind, created_at, uuid)"
            in definitions["messenger_domain_outbox_events_kind_created_idx"]
        )
        assert (
            "(task_kind, created_at, ordering_created_at, outbox_event_uuid)"
            in definitions["messenger_projection_tasks_fair_claim_idx"]
        )
        assert "status" in definitions["messenger_projection_tasks_fair_claim_idx"]
        assert (
            "(project_id, created_at, ordering_created_at, outbox_event_uuid)"
            in definitions["messenger_projection_tasks_global_partition_idx"]
        )
        assert (
            "(payload ->> 'user_uuid'::text) IS NOT NULL"
            in definitions["messenger_projection_tasks_global_partition_idx"]
        )
        assert (
            "project_id, ((payload ->> 'user_uuid'::text)), created_at"
            in definitions["messenger_projection_tasks_user_partition_idx"]
        )
        assert (
            "(payload ->> 'user_uuid'::text) IS NOT NULL"
            in definitions["messenger_projection_tasks_user_partition_idx"]
        )
        assert (
            "(created_at, ordering_created_at, outbox_event_uuid)"
            in definitions["messenger_projection_tasks_background_created_idx"]
        )
        assert (
            "delivery_snapshot_event"
            in definitions["messenger_projection_tasks_background_created_idx"]
        )
        assert (
            "(project_id, canonical_message_uuid, emoji_name, created_at, uuid)"
            in definitions["messenger_message_reaction_facts_snapshot_idx"]
        )
        assert (
            "INCLUDE (user_uuid)"
            in definitions["messenger_message_reaction_facts_snapshot_idx"]
        )
        cur.execute(
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name = 'messenger_projection_tasks'
              AND column_name = 'execution_stats'
            """
        )
        data_type, is_nullable, column_default = cur.fetchone()
        assert data_type == "jsonb"
        assert is_nullable == "NO"
        assert "'{}'::jsonb" in column_default
        cur.execute(
            """
            SELECT pg_get_triggerdef(oid)
            FROM pg_trigger
            WHERE tgname = 'messenger_projection_task_stats_sync_v1'
              AND NOT tgisinternal
            """
        )
        trigger_definition = cur.fetchone()[0]
        assert "BEFORE INSERT OR UPDATE OF payload" in trigger_definition
        cur.execute(
            """
            SELECT pg_get_functiondef(
                'messenger_v2_guard_rolling_broadcast_event()'::regprocedure
            )
            """
        )
        broadcast_guard = cur.fetchone()[0]
        assert "FROM messenger_project_users AS project_user" in broadcast_guard
        assert "ON CONFLICT (project_id, user_uuid) DO NOTHING" in broadcast_guard
        cur.execute(
            """
            SELECT pg_get_functiondef(
                'm_external_provider_read_lease_fence_v1()'::regprocedure
            )
            """
        )
        lease_fence = cur.fetchone()[0]
        assert "page_snapshot.queue_sequence" in lease_fence
        assert "OLD.external_operation_uuid" in lease_fence
        cur.execute(
            "SELECT to_regclass('m_workspace_files_external_content_hash_size_idx')"
        )
        assert cur.fetchone()[0] == "m_workspace_files_external_content_hash_size_idx"
        cur.execute("SELECT to_regclass('m_external_chats_v2_selected_projection_idx')")
        assert cur.fetchone()[0] == "m_external_chats_v2_selected_projection_idx"
        cur.execute("SELECT to_regclass('m_workspace_events_user_identity_idx')")
        assert cur.fetchone()[0] == "m_workspace_events_user_identity_idx"
        cur.execute(
            "SELECT to_regclass('m_workspace_message_reactions_message_uuid_idx')"
        )
        assert cur.fetchone()[0] == "m_workspace_message_reactions_message_uuid_idx"
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
        cur.execute("SELECT to_regclass('m_workspace_unread_flags_user_message_idx')")
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
        cur.execute(
            """
            SELECT sequencename, start_value, min_value, max_value
            FROM pg_sequences
            WHERE sequencename IN (
                'm_workspace_messages_ingest_sequence_v1_seq',
                'm_workspace_messages_legacy_ingest_sequence_v1_seq'
            )
            ORDER BY sequencename
            """
        )
        assert cur.fetchall() == [
            (
                "m_workspace_messages_ingest_sequence_v1_seq",
                281474976710656,
                1,
                9223372036854775807,
            ),
            (
                "m_workspace_messages_legacy_ingest_sequence_v1_seq",
                1,
                1,
                281474976710655,
            ),
        ]
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'm_workspace_read_state_compaction_phase_check'
            """
        )
        phase_constraint = cur.fetchone()[0]
        assert "legacy_gaps" in phase_constraint
        assert "verify_chunks" in phase_constraint
        assert "verify_read_stats" in phase_constraint
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_read_state_compaction_v1'
              AND column_name = 'legacy_gap_repair_kind'
            """
        )
        assert cur.fetchone() == ("legacy_gap_repair_kind",)
        cur.execute(
            """
            SELECT tgname
            FROM pg_trigger
            WHERE tgname IN (
                'm_workspace_fence_legacy_gap_cutover_v1',
                'm_workspace_hold_legacy_gap_progress_v1'
            )
              AND NOT tgisinternal
            """
        )
        assert {row[0] for row in cur.fetchall()} == {
            "m_workspace_fence_legacy_gap_cutover_v1",
            "m_workspace_hold_legacy_gap_progress_v1",
        }
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'm_workspace_read_state_projects_mode_check'
            """
        )
        assert "rollback" in cur.fetchone()[0]
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'm_external_provider_operations_v1'
              AND column_name IN ('public_result_status', 'terminal_result')
            """
        )
        assert {row[0] for row in cur.fetchall()} == {
            "public_result_status",
            "terminal_result",
        }
        cur.execute(
            """
            SELECT to_regclass(
                'm_external_provider_operations_external_operation_idx'
            )
            """
        )
        assert cur.fetchone()[0] == (
            "m_external_provider_operations_external_operation_idx"
        )
        cur.execute(
            """
            SELECT COUNT(*)
            FROM pg_constraint
            WHERE conname =
                'm_external_provider_operations_v1_external_operation_uuid_key'
            """
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indisvalid
            FROM pg_index
            WHERE indexrelid IN (
                to_regclass('m_workspace_messages_ingest_sequence_idx'),
                to_regclass(
                    'm_workspace_messages_project_ingest_sequence_idx'
                ),
                to_regclass(
                    'm_workspace_messages_topic_ingest_sequence_idx'
                ),
                to_regclass('m_workspace_messages_stream_read_page_idx'),
                to_regclass('m_workspace_messages_topic_read_page_idx'),
                to_regclass(
                    'm_workspace_messages_stream_ingest_sequence_idx'
                ),
                to_regclass('m_workspace_read_flags_project_message_idx'),
                to_regclass('m_workspace_flags_project_message_user_idx'),
                to_regclass(
                    'm_workspace_read_memberships_stream_user_idx'
                )
            )
            """
        )
        assert set(cur.fetchall()) == {
            ("m_workspace_messages_ingest_sequence_idx", True),
            ("m_workspace_messages_project_ingest_sequence_idx", True),
            ("m_workspace_messages_topic_ingest_sequence_idx", True),
            ("m_workspace_messages_stream_read_page_idx", True),
            ("m_workspace_messages_topic_read_page_idx", True),
            ("m_workspace_messages_stream_ingest_sequence_idx", True),
            ("m_workspace_read_flags_project_message_idx", True),
            ("m_workspace_flags_project_message_user_idx", True),
            ("m_workspace_read_memberships_stream_user_idx", True),
        }


def test_per_user_delivery_migration_repairs_shared_resource_snapshots(
    _database,
    db,
):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    stream_operation_uuid = sys_uuid.uuid4()
    stream_delivery_operation_uuid = sys_uuid.uuid4()
    topic_operation_uuid = sys_uuid.uuid4()
    message_operation_uuid = sys_uuid.uuid4()
    message_delivery_operation_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "Per-user delivery repair",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        owner_uuid,
        "delivery repair",
    )
    message_uuid = sys_uuid.uuid4()
    migration = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )._load_migrations()[PER_USER_DELIVERY_REPAIR_MIGRATION_FILE]

    with db.cursor() as cur:
        cur.execute(
            """
            DROP TRIGGER IF EXISTS
                workspace_reject_per_user_message_delivery_v1
            ON m_workspace_messages;
            DROP TRIGGER IF EXISTS
                workspace_reject_per_user_canonical_delivery_v1
            ON messenger_messages;
            DROP TRIGGER IF EXISTS
                workspace_reject_per_user_stream_delivery_v1
            ON m_workspace_streams;
            DROP TRIGGER IF EXISTS
                workspace_reject_per_user_topic_delivery_v1
            ON m_workspace_stream_topics;
            """
        )
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb, FALSE, 'live', TRUE)
            """,
            (account_uuid, owner_uuid),
        )
        cur.executemany(
            """
            INSERT INTO m_external_operations_v2 (
                uuid, external_account_uuid, owner_user_uuid,
                action, target_type, target_uuid, status,
                safe_error, can_retry, can_discard
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                (
                    stream_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "stream.update",
                    "stream",
                    stream_uuid,
                    "discarded",
                    None,
                    False,
                    False,
                ),
                (
                    stream_delivery_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "stream.update",
                    "stream",
                    stream_uuid,
                    "succeeded",
                    None,
                    False,
                    False,
                ),
                (
                    topic_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "topic.notification.update",
                    "topic",
                    topic_uuid,
                    "succeeded",
                    None,
                    False,
                    False,
                ),
                (
                    message_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "read_state.set",
                    "message",
                    message_uuid,
                    "succeeded",
                    None,
                    False,
                    False,
                ),
                (
                    message_delivery_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "message.create",
                    "message",
                    message_uuid,
                    "failed",
                    "provider temporarily unavailable",
                    True,
                    True,
                ),
            ),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"delivery repair"}',
                'native', '{"kind":"native"}'
            )
            """,
            (message_uuid, project_uuid, stream_uuid, topic_uuid, owner_uuid),
        )
        for table, target_uuid, operation_uuid, delivery_status in (
            (
                "m_workspace_streams",
                stream_uuid,
                stream_operation_uuid,
                "discarded",
            ),
            (
                "m_workspace_stream_topics",
                topic_uuid,
                topic_operation_uuid,
                "delivered",
            ),
            (
                "m_workspace_messages",
                message_uuid,
                message_operation_uuid,
                "delivered",
            ),
        ):
            cur.execute(
                f"""
                UPDATE {table}
                SET delivery_metadata = jsonb_build_object(
                        'external_operation_uuid', %s::text,
                        'status', %s::text
                    ),
                    delivery_status = %s,
                    delivery_error = 'stale per-user delivery',
                    delivery_updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    operation_uuid,
                    delivery_status,
                    ("failed" if delivery_status == "discarded" else delivery_status),
                    project_uuid,
                    target_uuid,
                ),
            )
        cur.execute(
            """
            UPDATE messenger_messages
            SET delivery = jsonb_build_object(
                'external_operation_uuid', %s::text,
                'status', 'delivered'
            )
            WHERE project_id = %s AND legacy_public_uuid = %s
            """,
            (message_operation_uuid, project_uuid, message_uuid),
        )
        # Simulate operations whose deletion events are already outside the
        # bounded event-retention window. The genuine stream operation must
        # be restored just as safely as the stale per-user message operation.
        cur.execute(
            "DELETE FROM m_external_operations_v2 WHERE uuid = ANY(%s::uuid[])",
            ([stream_operation_uuid, message_operation_uuid],),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version,
                pruned_through_epoch_version
            ) VALUES (%s, %s, 17, 3)
            RETURNING epoch_generation
            """,
            (project_uuid, owner_uuid),
        )
        cursor_generation_before = cur.fetchone()[0]

        migration.upgrade(cur)

        cur.execute(
            """
            SELECT delivery_metadata->>'external_operation_uuid',
                   delivery_metadata->>'status', delivery_status,
                   delivery_error
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, stream_uuid),
        )
        assert cur.fetchone() == (
            str(stream_delivery_operation_uuid),
            "delivered",
            "delivered",
            None,
        )
        cur.execute(
            """
            SELECT delivery_metadata, delivery_status,
                   delivery_error, delivery_updated_at
            FROM m_workspace_stream_topics
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, topic_uuid),
        )
        assert cur.fetchone() == (None, None, None, None)
        cur.execute(
            """
            SELECT delivery_metadata->>'external_operation_uuid',
                   delivery_metadata->>'status', delivery_status,
                   delivery_error
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        assert cur.fetchone() == (
            str(message_delivery_operation_uuid),
            "failed",
            "failed",
            "provider temporarily unavailable",
        )
        cur.execute(
            """
            SELECT delivery->>'external_operation_uuid',
                   delivery->>'status', delivery->>'safe_error'
            FROM messenger_messages
            WHERE project_id = %s AND legacy_public_uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        assert cur.fetchone() == (
            str(message_delivery_operation_uuid),
            "failed",
            "provider temporarily unavailable",
        )
        cur.execute(
            """
            SELECT epoch_generation, current_epoch_version,
                   pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_uuid, owner_uuid),
        )
        generation_after, current_epoch, pruned_epoch = cur.fetchone()
        assert generation_after != cursor_generation_before
        assert (current_epoch, pruned_epoch) == (17, 17)

        late_message_operation_uuid = sys_uuid.uuid4()
        late_stream_operation_uuid = sys_uuid.uuid4()
        late_topic_operation_uuid = sys_uuid.uuid4()
        cur.executemany(
            """
            INSERT INTO m_external_operations_v2 (
                uuid, external_account_uuid, owner_user_uuid,
                action, target_type, target_uuid, status
            ) VALUES (%s, %s, %s, %s, %s, %s, 'succeeded')
            """,
            (
                (
                    late_message_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "read_state.set",
                    "message",
                    message_uuid,
                ),
                (
                    late_stream_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "stream.notification.update",
                    "stream",
                    stream_uuid,
                ),
                (
                    late_topic_operation_uuid,
                    account_uuid,
                    owner_uuid,
                    "topic.notification.update",
                    "topic",
                    topic_uuid,
                ),
            ),
        )
        for table, target_uuid, operation_uuid in (
            ("m_workspace_messages", message_uuid, late_message_operation_uuid),
            ("m_workspace_streams", stream_uuid, late_stream_operation_uuid),
            (
                "m_workspace_stream_topics",
                topic_uuid,
                late_topic_operation_uuid,
            ),
        ):
            cur.execute(
                f"""
                UPDATE {table}
                SET delivery_metadata = jsonb_build_object(
                        'external_operation_uuid', %s::text,
                        'status', 'delivered'
                    ),
                    delivery_status = 'delivered',
                    delivery_error = NULL,
                    delivery_updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                RETURNING uuid
                """,
                (operation_uuid, project_uuid, target_uuid),
            )
            assert cur.fetchone() is None
        cur.execute(
            """
            UPDATE messenger_messages
            SET delivery = jsonb_build_object(
                'external_operation_uuid', %s::text,
                'status', 'delivered'
            )
            WHERE project_id = %s AND legacy_public_uuid = %s
            RETURNING uuid
            """,
            (late_message_operation_uuid, project_uuid, message_uuid),
        )
        assert cur.fetchone() is None
        cur.execute(
            """
            SELECT count(*)
            FROM (
                SELECT delivery_metadata->>'external_operation_uuid' AS uuid
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
                UNION ALL
                SELECT delivery->>'external_operation_uuid'
                FROM messenger_messages
                WHERE project_id = %s AND legacy_public_uuid = %s
                UNION ALL
                SELECT delivery_metadata->>'external_operation_uuid'
                FROM m_workspace_streams
                WHERE project_id = %s AND uuid = %s
                UNION ALL
                SELECT delivery_metadata->>'external_operation_uuid'
                FROM m_workspace_stream_topics
                WHERE project_id = %s AND uuid = %s
            ) AS projection
            WHERE projection.uuid = ANY(%s::text[])
            """,
            (
                project_uuid,
                message_uuid,
                project_uuid,
                message_uuid,
                project_uuid,
                stream_uuid,
                project_uuid,
                topic_uuid,
                [
                    str(late_message_operation_uuid),
                    str(late_stream_operation_uuid),
                    str(late_topic_operation_uuid),
                ],
            ),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_events WHERE project_id = %s",
            (project_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_event_cursors WHERE project_id = %s",
            (project_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_streams WHERE project_id = %s AND uuid = %s",
            (project_uuid, stream_uuid),
        )


def test_legacy_gap_fence_is_active_before_online_index_build(_database):
    project_uuid = sys_uuid.uuid4()
    with ra_contexts.Context().session_manager() as session:
        try:
            session.execute("DROP INDEX m_workspace_read_memberships_stream_user_idx")
            assert (
                session.execute(
                    "SELECT to_regclass(%s) AS relation",
                    ("m_workspace_read_memberships_stream_user_idx",),
                ).fetchone()["relation"]
                is None
            )
            session.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                ) VALUES (%s, 'dual', NOW(), NOW())
                """,
                (project_uuid,),
            )
            session.execute(
                """
                INSERT INTO m_workspace_read_state_compaction_v1 (
                    project_id, phase, target_ingest_sequence,
                    legacy_gap_repair_kind, completed_at,
                    created_at, updated_at
                ) VALUES (
                    %s, 'verify_mentions', 10, 'full_done', NULL, NOW(), NOW()
                )
                """,
                (project_uuid,),
            )
            session.execute(
                "SELECT set_config(%s, %s, TRUE)",
                (read_state.LEGACY_GAP_CUTOVER_CAPABILITY, str(project_uuid)),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'compact', updated_at = NOW()
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            fenced = session.execute(
                """
                SELECT state.mode, progress.phase,
                       progress.legacy_gap_repair_kind
                FROM m_workspace_read_state_projects_v1 AS state
                JOIN m_workspace_read_state_compaction_v1 AS progress
                  ON progress.project_id = state.project_id
                WHERE state.project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert fenced["mode"] == "dual"
            assert fenced["phase"] == "legacy_gaps"
            assert fenced["legacy_gap_repair_kind"] == "full_pending"
        finally:
            session.rollback()


def test_online_index_downgrade_defers_drop_to_gap_fence(_database):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    index_migration = engine._load_migrations()[
        COMPACT_READ_MEMBERSHIP_INDEX_MIGRATION_FILE
    ]
    with ra_contexts.Context().session_manager() as session:
        index_migration.downgrade(session)
        assert (
            session.execute(
                "SELECT to_regclass(%s) AS relation",
                ("m_workspace_read_memberships_stream_user_idx",),
            ).fetchone()["relation"]
            == "m_workspace_read_memberships_stream_user_idx"
        )


def test_compact_legacy_gap_migration_leaves_completed_projects_unchanged(_database):
    compact_project_uuid = sys_uuid.uuid4()
    dual_project_uuid = sys_uuid.uuid4()
    project_uuids = [compact_project_uuid, dual_project_uuid]
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[COMPACT_LEGACY_GAP_REPAIR_MIGRATION_FILE]
    with ra_contexts.Context().session_manager() as session:
        try:
            migration.downgrade(session)
            session.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                ) VALUES
                    (%s, 'compact', NOW(), NOW()),
                    (%s, 'dual', NOW(), NOW())
                """,
                (compact_project_uuid, dual_project_uuid),
            )
            session.execute(
                """
                INSERT INTO m_workspace_read_state_compaction_v1 (
                    project_id, phase, target_ingest_sequence,
                    completed_at, created_at, updated_at
                ) VALUES
                    (%s, 'verify_mentions', 10, NOW(), NOW(), NOW()),
                    (%s, 'stats', 20, NULL, NOW(), NOW())
                """,
                (compact_project_uuid, dual_project_uuid),
            )

            migration.upgrade(session)
            rows = session.execute(
                """
                SELECT progress.project_id, state.mode, progress.phase,
                       progress.legacy_gap_repair_kind,
                       progress.completed_at
                FROM m_workspace_read_state_compaction_v1 AS progress
                JOIN m_workspace_read_state_projects_v1 AS state
                  ON state.project_id = progress.project_id
                WHERE progress.project_id = ANY(%s::uuid[])
                ORDER BY progress.project_id
                """,
                (project_uuids,),
            ).fetchall()
            scheduled = {
                row["project_id"]: (
                    row["mode"],
                    row["phase"],
                    row["legacy_gap_repair_kind"],
                    row["completed_at"],
                )
                for row in rows
            }
            assert scheduled[compact_project_uuid][:3] == (
                "compact",
                "verify_mentions",
                None,
            )
            assert scheduled[compact_project_uuid][3] is not None
            assert scheduled[dual_project_uuid] == ("dual", "stats", None, None)

            migration.downgrade(session)
            rows = session.execute(
                """
                SELECT progress.project_id, state.mode, progress.phase,
                       progress.completed_at
                FROM m_workspace_read_state_compaction_v1 AS progress
                JOIN m_workspace_read_state_projects_v1 AS state
                  ON state.project_id = progress.project_id
                WHERE progress.project_id = ANY(%s::uuid[])
                ORDER BY progress.project_id
                """,
                (project_uuids,),
            ).fetchall()
            restored = {
                row["project_id"]: (
                    row["mode"],
                    row["phase"],
                    row["completed_at"],
                )
                for row in rows
            }
            assert restored[compact_project_uuid][:2] == (
                "compact",
                "verify_mentions",
            )
            assert restored[compact_project_uuid][2] is not None
            assert restored[dual_project_uuid] == ("dual", "stats", None)
        finally:
            session.rollback()


def test_compact_legacy_gap_migration_fences_old_dual_completion(_database):
    project_uuid = sys_uuid.uuid4()
    partial_user_uuid = sys_uuid.uuid4()
    stale_user_uuid = sys_uuid.uuid4()
    stale_message_uuid = sys_uuid.uuid4()
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[COMPACT_LEGACY_GAP_REPAIR_MIGRATION_FILE]
    with ra_contexts.Context().session_manager() as session:
        try:
            session.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                ) VALUES (%s, 'dual', NOW(), NOW())
                """,
                (project_uuid,),
            )
            session.execute(
                """
                INSERT INTO m_workspace_read_state_compaction_v1 (
                    project_id, phase, target_ingest_sequence,
                    completed_at, created_at, updated_at
                ) VALUES (%s, 'verify_mentions', 10, NULL, NOW(), NOW())
                """,
                (project_uuid,),
            )

            session.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'compact', updated_at = NOW()
                WHERE project_id = %s AND mode = 'dual'
                """,
                (project_uuid,),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_compaction_v1
                SET completed_at = NOW(), updated_at = NOW()
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            fenced = session.execute(
                """
                SELECT state.mode, progress.phase,
                       progress.legacy_gap_repair_kind,
                       progress.completed_at
                FROM m_workspace_read_state_projects_v1 AS state
                JOIN m_workspace_read_state_compaction_v1 AS progress
                  ON progress.project_id = state.project_id
                WHERE state.project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert fenced["mode"] == "dual"
            assert fenced["phase"] == "legacy_gaps"
            assert fenced["legacy_gap_repair_kind"] == "full_pending"
            assert fenced["completed_at"] is None

            session.execute(
                "SELECT set_config(%s, %s, TRUE)",
                (read_state.LEGACY_GAP_SCAN_CAPABILITY, str(project_uuid)),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_compaction_v1
                SET last_user_uuid = %s,
                    last_ingest_sequence = 5,
                    processed_rows = processed_rows + 1,
                    updated_at = NOW()
                WHERE project_id = %s
                """,
                (partial_user_uuid, project_uuid),
            )
            session.execute(
                "SELECT set_config(%s, '', TRUE)",
                (read_state.LEGACY_GAP_SCAN_CAPABILITY,),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_compaction_v1
                SET last_message_uuid = %s,
                    last_user_uuid = %s,
                    processed_rows = processed_rows + 1,
                    updated_at = NOW()
                WHERE project_id = %s
                """,
                (stale_message_uuid, stale_user_uuid, project_uuid),
            )
            protected_cursor = session.execute(
                """
                SELECT last_message_uuid, last_user_uuid,
                       last_ingest_sequence, processed_rows
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert protected_cursor["last_message_uuid"] is None
            assert protected_cursor["last_user_uuid"] == partial_user_uuid
            assert protected_cursor["last_ingest_sequence"] == 5
            assert protected_cursor["processed_rows"] == 1

            session.execute(
                "SELECT set_config(%s, %s, TRUE)",
                (read_state.LEGACY_GAP_SCAN_CAPABILITY, str(project_uuid)),
            )
            session.execute(
                """
                UPDATE m_workspace_read_state_compaction_v1
                SET phase = 'verify_mentions', updated_at = NOW()
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            session.execute(
                "SELECT set_config(%s, '', TRUE)",
                (read_state.LEGACY_GAP_SCAN_CAPABILITY,),
            )
            read_state._complete_compaction(session, project_uuid)
            refenced = session.execute(
                """
                SELECT state.mode, progress.phase,
                       progress.legacy_gap_repair_kind,
                       progress.completed_at
                FROM m_workspace_read_state_projects_v1 AS state
                JOIN m_workspace_read_state_compaction_v1 AS progress
                  ON progress.project_id = state.project_id
                WHERE state.project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert refenced["mode"] == "dual"
            assert refenced["phase"] == "legacy_gaps"
            assert refenced["legacy_gap_repair_kind"] == "full_pending"
            assert refenced["completed_at"] is None

            assert read_state.compact_legacy_batch(session, project_uuid, 1) == 0
            gap_completed = session.execute(
                """
                SELECT phase, legacy_gap_repair_kind
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert gap_completed["phase"] == "stats"
            assert gap_completed["legacy_gap_repair_kind"] == "full_done"
            session.execute(
                """
                UPDATE m_workspace_read_state_compaction_v1
                SET phase = 'verify_mentions', updated_at = NOW()
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            read_state._complete_compaction(session, project_uuid)
            repaired = session.execute(
                """
                SELECT state.mode, progress.phase,
                       progress.legacy_gap_repair_kind,
                       progress.completed_at
                FROM m_workspace_read_state_projects_v1 AS state
                JOIN m_workspace_read_state_compaction_v1 AS progress
                  ON progress.project_id = state.project_id
                WHERE state.project_id = %s
                """,
                (project_uuid,),
            ).fetchone()
            assert repaired["mode"] == "compact"
            assert repaired["phase"] == "verify_mentions"
            assert repaired["legacy_gap_repair_kind"] is None
            assert repaired["completed_at"] is not None

            migration.downgrade(session)
        finally:
            session.rollback()


def test_compact_legacy_gap_migration_blocks_downgrade_during_full_repair(
    _database,
    db,
):
    project_uuid = sys_uuid.uuid4()
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[COMPACT_LEGACY_GAP_REPAIR_MIGRATION_FILE]
    with ra_contexts.Context().session_manager() as session:
        try:
            session.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                ) VALUES (%s, 'dual', NOW(), NOW())
                """,
                (project_uuid,),
            )
            session.execute(
                """
                INSERT INTO m_workspace_read_state_compaction_v1 (
                    project_id, phase, target_ingest_sequence,
                    legacy_gap_repair_kind, completed_at,
                    created_at, updated_at
                ) VALUES (
                    %s, 'legacy_gaps', 10, 'full_pending', NULL, NOW(), NOW()
                )
                """,
                (project_uuid,),
            )

            with pytest.raises(
                psycopg.errors.RaiseException,
                match="legacy gap repair must finish before downgrade",
            ):
                migration.downgrade(session)
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass(%s)",
                    ("m_workspace_read_memberships_stream_user_idx",),
                )
                assert cursor.fetchone()[0] == (
                    "m_workspace_read_memberships_stream_user_idx"
                )
        finally:
            session.rollback()


@pytest.mark.parametrize(
    "migration_file",
    (
        COMPACT_DENSE_PREPARATION_MIGRATION_FILE,
        COMPACT_DENSE_JOIN_MIGRATION_FILE,
    ),
)
def test_compact_dense_compatibility_join_is_explicitly_forward_only(
    _database,
    migration_file,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[migration_file]

    with ra_contexts.Context().session_manager() as session:
        with pytest.raises(
            RuntimeError,
            match="compatibility migration is forward-only",
        ):
            migration.downgrade(session)


@pytest.mark.pre_retirement_schema
def test_forward_correction_upgrades_published_read_state_without_rewrite(
    _database,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    _set_historical_schema_fixture_before_forward_only_join(db)
    engine.rollback_migration(READ_STATE_FORWARD_CORRECTION_MIGRATION_FILE)

    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        user_uuid,
        "Online sequence migration",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        user_uuid,
        "general",
        is_default=True,
    )
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_project_ingest_ranges_v2'
              AND column_name IN (
                    'next_local_sequence',
                    'last_backfill_sequence',
                    'last_live_sequence'
              )
            """
        )
        assert {row[0] for row in cur.fetchall()} == {"next_local_sequence"}
        cur.execute(
            """
            SELECT pg_get_functiondef(
                'm_external_provider_read_lease_fence_v1()'::regprocedure
            )
            """
        )
        published_fence = cur.fetchone()[0]
        assert "workspace.provider_read_snapshot_lease_v2" in published_fence
        assert "m_external_bridge_instances_v2" not in published_fence
        cur.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"stable coordinate"}',
                'native', '{"kind":"native"}'
            )
            RETURNING ingest_sequence, xmin::text, ctid::text
            """,
            (message_uuid, project_uuid, stream_uuid, topic_uuid, user_uuid),
        )
        ingest_sequence, row_xmin, row_ctid = cur.fetchone()
        assert ingest_sequence % 4_294_967_296 == 1
        cur.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode
            ) VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = EXCLUDED.mode
            """,
            (project_uuid,),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits
            ) VALUES (
                %s,
                %s,
                set_bit(B'0'::bit(4096), %s, 1)
            )
            """,
            (
                user_uuid,
                ingest_sequence // read_state.READ_CHUNK_BITS,
                ingest_sequence % read_state.READ_CHUNK_BITS,
            ),
        )
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indexrelid
            FROM pg_index
            WHERE indexrelid IN (
                to_regclass('m_workspace_messages_ingest_sequence_idx'),
                to_regclass(
                    'm_workspace_messages_project_ingest_sequence_idx'
                )
            )
            ORDER BY indexrelid::regclass::text
            """
        )
        index_oids = cur.fetchall()

    # Published 0139 already assigned project-local coordinates. The new HEAD
    # changes only allocator metadata and preserves every compact bitmap bit.
    engine.apply_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)
    _restore_current_provider_read_lease_fence(engine)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT ingest_sequence, xmin::text, ctid::text
            FROM m_workspace_messages
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        assert cur.fetchone() == (ingest_sequence, row_xmin, row_ctid)
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indexrelid
            FROM pg_index
            WHERE indexrelid IN (
                to_regclass('m_workspace_messages_ingest_sequence_idx'),
                to_regclass(
                    'm_workspace_messages_project_ingest_sequence_idx'
                )
            )
            ORDER BY indexrelid::regclass::text
            """
        )
        assert cur.fetchall() == index_oids
        cur.execute(
            """
            SELECT last_backfill_sequence, last_live_sequence
            FROM m_workspace_project_ingest_ranges_v2
            WHERE project_id = %s
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == (1, 2_147_483_647)
        cur.execute(
            """
            SELECT pg_get_functiondef(
                'm_external_provider_read_lease_fence_v1()'::regprocedure
            )
            """
        )
        corrected_fence = cur.fetchone()[0]
        assert "workspace.provider_read_snapshot_lease_v2" in corrected_fence
        assert "m_external_bridge_instances_v2" in corrected_fence

    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM m_workspace_user_read_chunks_v1 WHERE user_uuid = %s",
            (user_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        cur.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (project_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_project_ingest_ranges_v2 WHERE project_id = %s",
            (project_uuid,),
        )


@pytest.mark.pre_retirement_schema
def test_forward_graph_upgrades_compact_0137_read_state(_database, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    _set_historical_schema_fixture_before_forward_only_join(db)
    for migration_file in (
        READ_STATE_FORWARD_CORRECTION_MIGRATION_FILE,
        PROVIDER_HISTORY_DOWNGRADE_MIGRATION_FILE,
        PROJECT_DENSE_READ_SEQUENCE_MIGRATION_FILE,
        PROVIDER_READ_ROLLING_FENCE_MIGRATION_FILE,
    ):
        engine.rollback_migration(migration_file)

    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    external_operation_uuid = sys_uuid.uuid4()
    provider_operation_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    first_message_uuid = sys_uuid.uuid4()
    second_message_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        user_uuid,
        "Compact 0137 forward upgrade",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        user_uuid,
        "general",
        is_default=True,
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source, created_at
            ) VALUES
                (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"before detach"}',
                    'native', '{"kind":"native"}', NOW() - INTERVAL '1 hour'
                ),
                (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"while detached"}',
                    'native', '{"kind":"native"}', NOW()
                )
            RETURNING uuid, ingest_sequence
            """,
            (
                first_message_uuid,
                project_uuid,
                stream_uuid,
                topic_uuid,
                user_uuid,
                second_message_uuid,
                project_uuid,
                stream_uuid,
                topic_uuid,
                user_uuid,
            ),
        )
        sequences = dict(cur.fetchall())
        first_sequence = sequences[first_message_uuid]
        second_sequence = sequences[second_message_uuid]
        cur.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred
            ) VALUES (%s, %s, %s, FALSE, TRUE, FALSE)
            """,
            (first_message_uuid, user_uuid, project_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (project_id, mode)
            VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = EXCLUDED.mode
            """,
            (project_uuid,),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_read_state_compaction_v1 (
                project_id, phase, completed_at
            ) VALUES (%s, 'verify_mentions', NOW())
            """,
            (project_uuid,),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits
            ) VALUES (
                %s,
                %s / 4096,
                set_bit(B'0'::bit(4096), (%s %% 4096)::integer, 1)
            )
            """,
            (user_uuid, first_sequence, first_sequence),
        )
        # Empty chunks are valid but carry no logical reads. They must not turn
        # coordinate translation into chunk_count x 4096 work or be restored.
        cur.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits
            )
            SELECT %s, chunk_number, B'0'::bit(4096)
            FROM generate_series(0, 255) AS chunk_number
            ON CONFLICT (user_uuid, chunk_number) DO NOTHING
            """,
            (user_uuid,),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_user_topic_read_stats_v1 (
                project_id, user_uuid, topic_uuid, read_count
            ) VALUES (%s, %s, %s, 1)
            """,
            (project_uuid, user_uuid, topic_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_message_mentions_v1 (
                message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
                ingest_sequence
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                first_message_uuid,
                user_uuid,
                project_uuid,
                stream_uuid,
                topic_uuid,
                first_sequence,
            ),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence
            ) VALUES (%s, %s, %s, 2, %s)
            """,
            (topic_uuid, project_uuid, stream_uuid, second_sequence),
        )
        cur.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, user_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_read_memberships_v1 (
                project_id, user_uuid, stream_uuid, last_detached_sequence
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                project_uuid,
                user_uuid,
                stream_uuid,
                first_sequence,
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb)
            """,
            (account_uuid, user_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (uuid, provider)
            VALUES (%s, 'zulip')
            """,
            (bridge_uuid,),
        )
        cur.execute(
            """
            INSERT INTO m_external_operations_v2 (
                uuid, external_account_uuid, owner_user_uuid,
                action, target_type, status
            ) VALUES (%s, %s, %s, 'read_state.set', 'stream', 'running')
            """,
            (external_operation_uuid, account_uuid, user_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_external_provider_read_snapshots_v1 (
                external_operation_uuid, bridge_instance_uuid,
                external_account_uuid, project_id, causal_lane, payload
            ) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb)
            """,
            (
                external_operation_uuid,
                bridge_uuid,
                account_uuid,
                project_uuid,
                stream_uuid,
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_external_provider_operations_v1 (
                uuid, external_operation_uuid, bridge_instance_uuid,
                external_account_uuid, project_id, operation_kind,
                causal_lane, payload
            ) VALUES (
                %s, %s, %s, %s, %s, 'read_state.set', %s,
                '{"message_uuids":[]}'::jsonb
            )
            """,
            (
                (
                    provider_operation_uuid,
                    external_operation_uuid,
                    bridge_uuid,
                    account_uuid,
                    project_uuid,
                    stream_uuid,
                )
                for provider_operation_uuid in provider_operation_uuids
            ),
        )

    with pytest.raises(RuntimeError, match="snapshots to be drained first"):
        engine.apply_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (external_operation_uuid,),
        )
        assert cur.fetchone() == (1,)
        cur.execute(
            "SELECT applied FROM ra_migrations WHERE uuid = %s",
            (PROJECT_DENSE_READ_SEQUENCE_MIGRATION_UUID,),
        )
        assert cur.fetchone() == (False,)
        cur.execute(
            "DELETE FROM m_external_provider_read_snapshots_v1 "
            "WHERE external_operation_uuid = %s",
            (external_operation_uuid,),
        )

        # Make the old ingest prefix non-representable in the published
        # (created_at, uuid) ordering. The failed migration must not change
        # coordinates, flags, project mode, or compact bits.
        cur.execute(
            "UPDATE m_workspace_messages SET created_at = NOW() WHERE uuid = %s",
            (first_message_uuid,),
        )
        cur.execute(
            "UPDATE m_workspace_messages SET created_at = NOW() - INTERVAL '1 hour' "
            "WHERE uuid = %s",
            (second_message_uuid,),
        )

    with pytest.raises(RuntimeError, match="cannot preserve a detached membership"):
        engine.apply_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            "SELECT mode FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (project_uuid,),
        )
        assert cur.fetchone() == ("compact",)
        cur.execute(
            "SELECT read FROM m_workspace_user_message_flags "
            "WHERE uuid = %s AND user_uuid = %s",
            (first_message_uuid, user_uuid),
        )
        assert cur.fetchone() == (False,)
        cur.execute(
            "SELECT COUNT(*) FROM m_workspace_user_read_chunks_v1 WHERE user_uuid = %s",
            (user_uuid,),
        )
        assert cur.fetchone() == (257,)
        cur.execute(
            "SELECT last_detached_sequence FROM m_workspace_read_memberships_v1 "
            "WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
            (project_uuid, stream_uuid, user_uuid),
        )
        assert cur.fetchone() == (first_sequence,)
        cur.execute(
            "UPDATE m_workspace_messages SET created_at = NOW() - INTERVAL '1 hour' "
            "WHERE uuid = %s",
            (first_message_uuid,),
        )
        cur.execute(
            "UPDATE m_workspace_messages SET created_at = NOW() WHERE uuid = %s",
            (second_message_uuid,),
        )

    engine.apply_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT uuid, ingest_sequence
            FROM m_workspace_messages
            WHERE uuid = ANY(%s::uuid[])
            ORDER BY created_at, uuid
            """,
            ([first_message_uuid, second_message_uuid],),
        )
        dense_messages = cur.fetchall()
        assert [row[0] for row in dense_messages] == [
            first_message_uuid,
            second_message_uuid,
        ]
        dense_first_sequence = dense_messages[0][1]
        dense_second_sequence = dense_messages[1][1]
        assert dense_first_sequence != first_sequence
        cur.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == ("compact",)
        cur.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT external_operation_uuid)
            FROM m_external_provider_operations_v1
            WHERE uuid = ANY(%s::uuid[])
            """,
            (list(provider_operation_uuids),),
        )
        assert cur.fetchone() == (2, 1)
        cur.execute(
            "SELECT status FROM m_external_operations_v2 WHERE uuid = %s",
            (external_operation_uuid,),
        )
        assert cur.fetchone() == ("running",)
        cur.execute(
            "DELETE FROM m_external_operations_v2 WHERE uuid = %s",
            (external_operation_uuid,),
        )
        cur.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        cur.execute(
            "DELETE FROM m_external_bridge_instances_v2 WHERE uuid = %s",
            (bridge_uuid,),
        )
        cur.execute(
            """
            SELECT read, pinned, starred
            FROM m_workspace_user_message_flags
            WHERE uuid = %s AND user_uuid = %s
            """,
            (first_message_uuid, user_uuid),
        )
        assert cur.fetchone() == (False, True, False)
        cur.execute(
            """
            SELECT get_bit(chunk.read_bits, (%s %% 4096)::integer) = 1
            FROM m_workspace_user_read_chunks_v1 AS chunk
            WHERE chunk.user_uuid = %s
              AND chunk.chunk_number = %s / 4096
            """,
            (dense_first_sequence, user_uuid, dense_first_sequence),
        )
        assert cur.fetchone() == (True,)
        cur.execute(
            "SELECT COUNT(*) FROM m_workspace_user_read_chunks_v1 WHERE user_uuid = %s",
            (user_uuid,),
        )
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT last_detached_sequence
            FROM m_workspace_read_memberships_v1
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, user_uuid),
        )
        assert cur.fetchone() == (dense_first_sequence,)
        cur.execute(
            "SELECT completed_at IS NOT NULL "
            "FROM m_workspace_read_state_compaction_v1 WHERE project_id = %s",
            (project_uuid,),
        )
        assert cur.fetchone() == (True,)
        cur.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_message_mentions_v1
            WHERE message_uuid = %s AND user_uuid = %s
            """,
            (first_message_uuid, user_uuid),
        )
        assert cur.fetchone() == (dense_first_sequence,)
        cur.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (project_uuid, user_uuid, topic_uuid),
        )
        assert cur.fetchone() == (1,)
        cur.execute(
            """
            SELECT message_count, last_ingest_sequence
            FROM m_workspace_topic_message_stats_v1
            WHERE topic_uuid = %s
            """,
            (topic_uuid,),
        )
        assert cur.fetchone() == (2, dense_second_sequence)

    # RestAlchemy exposes no caller context to distinguish a direct HEAD
    # rollback from a recursive rollback that will later renumber 0139. Refuse
    # both before any persisted coordinate can become ambiguous.
    with pytest.raises(
        RuntimeError,
        match="compatibility migration is forward-only",
    ):
        engine.rollback_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (first_message_uuid,),
        )
        assert cur.fetchone() == (dense_first_sequence,)
        cur.execute(
            """
            SELECT last_detached_sequence
            FROM m_workspace_read_memberships_v1
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, user_uuid),
        )
        assert cur.fetchone() == (dense_first_sequence,)
        # An empty-prefix sentinel is guarded identically; it can exceed the old
        # global sequence base at high dense range numbers.
        cur.execute(
            """
            UPDATE m_workspace_read_memberships_v1
            SET last_detached_sequence = 0
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, user_uuid),
        )
    with pytest.raises(
        RuntimeError,
        match="compatibility migration is forward-only",
    ):
        engine.rollback_migration(COMPACT_DENSE_JOIN_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_read_memberships_v1
            SET last_detached_sequence = %s
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (dense_first_sequence, project_uuid, stream_uuid, user_uuid),
        )

    # RestAlchemy cannot remap a logical boundary after the published 0139
    # dependency downgrade. Refuse before 0139 can widen detached history.
    with pytest.raises(
        RuntimeError,
        match="compatibility migration is forward-only",
    ):
        engine.rollback_migration(PROVIDER_READ_ROLLING_FENCE_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (first_message_uuid,),
        )
        assert cur.fetchone() == (dense_first_sequence,)
        cur.execute(
            """
            SELECT last_detached_sequence
            FROM m_workspace_read_memberships_v1
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, user_uuid),
        )
        assert cur.fetchone() == (dense_first_sequence,)

    # Reattachment applies the saved detach boundary to the compact bitmap.
    # Only the gap message is newly marked read and no dense flag rows appear.
    with ra_contexts.Context().session_manager() as session:
        read_state.mark_stream_history_read(
            session,
            project_uuid,
            user_uuid,
            stream_uuid,
        )
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                message.uuid,
                get_bit(
                    chunk.read_bits,
                    (message.ingest_sequence %% 4096)::integer
                ) = 1 AS read
            FROM m_workspace_messages AS message
            JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = %s
             AND chunk.chunk_number = message.ingest_sequence / 4096
            WHERE message.uuid = ANY(%s::uuid[])
            ORDER BY message.uuid
            """,
            (user_uuid, [first_message_uuid, second_message_uuid]),
        )
        assert cur.fetchall() == sorted(
            [(first_message_uuid, True), (second_message_uuid, True)]
        )
        cur.execute(
            "DELETE FROM m_workspace_user_read_chunks_v1 WHERE user_uuid = %s",
            (user_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        cur.execute(
            "DELETE FROM m_workspace_read_state_compaction_v1 WHERE project_id = %s",
            (project_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (project_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_project_ingest_ranges_v2 WHERE project_id = %s",
            (project_uuid,),
        )


def test_compact_read_state_downgrade_releases_each_bounded_project_lock(
    _database,
    _historical_database,
    db,
    monkeypatch,
):
    project_uuids = (
        "10000000-0000-4000-8000-0000000008f1",
        "10000000-0000-4000-8000-0000000008f2",
    )
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration_step = engine._load_migrations()[COMPACT_READ_STATE_MIGRATION_FILE]
    migration_module = __import__(migration_step.__class__.__module__)
    original_batch = migration_module._hydrate_legacy_flags_batch
    original_lock = migration_module._lock_read_state_project
    observed_project_locks = []
    lock_entries = []
    forced_extra_batches = set()
    existing_compact_project_uuids = []

    def observe_released_lock(session, project_id):
        if str(project_id) in project_uuids:
            with psycopg.connect(_historical_database, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT pg_try_advisory_lock(
                            hashtextextended(%s::text, 0)
                        )
                        """,
                        (project_id,),
                    )
                    assert cur.fetchone() == (True,)
                    cur.execute(
                        """
                        SELECT pg_advisory_unlock(
                            hashtextextended(%s::text, 0)
                        )
                        """,
                        (project_id,),
                    )
                    assert cur.fetchone() == (True,)
            lock_entries.append(str(project_id))
        return original_lock(session, project_id)

    def observe_project_locks(session, project_id, batch_size):
        if str(project_id) in project_uuids and not observed_project_locks:
            assert str(project_id) == project_uuids[0]
            with psycopg.connect(_historical_database, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT pg_try_advisory_lock(
                            hashtextextended(%s::text, 0)
                        )
                        """,
                        (project_uuids[0],),
                    )
                    assert cur.fetchone() == (False,)
                    cur.execute(
                        """
                        SELECT pg_try_advisory_lock(
                            hashtextextended(%s::text, 0)
                        )
                        """,
                        (project_uuids[1],),
                    )
                    assert cur.fetchone() == (True,)
                    cur.execute(
                        """
                        SELECT pg_advisory_unlock(
                            hashtextextended(%s::text, 0)
                        )
                        """,
                        (project_uuids[1],),
                    )
                    assert cur.fetchone() == (True,)
            observed_project_locks.append(str(project_id))
        if (
            str(project_id) in project_uuids
            and str(project_id) not in forced_extra_batches
        ):
            forced_extra_batches.add(str(project_id))
            session.execute(
                """
                UPDATE m_workspace_read_state_downgrade_v1
                SET processed_rows = processed_rows + 1,
                    updated_at = NOW()
                WHERE project_id = %s
                """,
                (project_id,),
            )
            return 1
        return original_batch(session, project_id, batch_size)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT project_id::text
            FROM m_workspace_read_state_projects_v1
            WHERE mode = 'compact'
            """
        )
        existing_compact_project_uuids = [row[0] for row in cur.fetchall()]
        if existing_compact_project_uuids:
            cur.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'dual'
                WHERE project_id = ANY(%s::uuid[])
                """,
                (existing_compact_project_uuids,),
            )
        cur.executemany(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode
            ) VALUES (%s, 'compact')
            """,
            ((project_uuid,) for project_uuid in project_uuids),
        )
    monkeypatch.setattr(
        migration_module,
        "_hydrate_legacy_flags_batch",
        observe_project_locks,
    )
    monkeypatch.setattr(
        migration_module,
        "_lock_read_state_project",
        observe_released_lock,
    )
    try:
        with ra_contexts.Context().session_manager() as session:
            migration_module._prepare_downgrade_progress(session)
            session.execute(
                """
                INSERT INTO m_workspace_read_state_downgrade_v1 (
                    project_id, completed_at
                ) VALUES (%s, NOW())
                """,
                (project_uuids[0],),
            )
            session.commit()
            migration_module._hydrate_legacy_flags(session)

        assert observed_project_locks == [project_uuids[0]]
        assert lock_entries.count(project_uuids[0]) >= 3
        assert lock_entries.count(project_uuids[1]) >= 3
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT project_id::text, mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = ANY(%s::uuid[])
                ORDER BY project_id
                """,
                (list(project_uuids),),
            )
            assert cur.fetchall() == [
                (project_uuid, "rollback") for project_uuid in project_uuids
            ]
            for project_uuid in project_uuids:
                cur.execute(
                    """
                    SELECT pg_try_advisory_lock(
                        hashtextextended(%s::text, 0)
                    )
                    """,
                    (project_uuid,),
                )
                assert cur.fetchone() == (True,)
                cur.execute(
                    """
                    SELECT pg_advisory_unlock(
                        hashtextextended(%s::text, 0)
                    )
                    """,
                    (project_uuid,),
                )
                assert cur.fetchone() == (True,)
    finally:
        with db.cursor() as cur:
            cur.execute(
                """
                DELETE FROM m_workspace_read_state_downgrade_v1
                WHERE project_id = ANY(%s::uuid[])
                """,
                (list(project_uuids),),
            )
            cur.execute(
                """
                DELETE FROM m_workspace_read_state_projects_v1
                WHERE project_id = ANY(%s::uuid[])
                """,
                (list(project_uuids),),
            )
            if existing_compact_project_uuids:
                cur.execute(
                    """
                    UPDATE m_workspace_read_state_projects_v1
                    SET mode = 'compact'
                    WHERE project_id = ANY(%s::uuid[])
                    """,
                    (existing_compact_project_uuids,),
                )


def test_compact_read_state_final_downgrade_gate_fences_new_project_writer(
    _database,
    db,
):
    project_uuid = sys_uuid.uuid4()
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration_step = engine._load_migrations()[COMPACT_READ_STATE_MIGRATION_FILE]
    migration_module = __import__(migration_step.__class__.__module__)
    gate_ready = threading.Event()
    release_gate = threading.Event()
    writer_started = threading.Event()
    maintenance_started = threading.Event()
    reader_started = threading.Event()
    request_started = threading.Event()

    def hold_final_gate():
        with ra_contexts.Context().session_manager() as session:
            migration_module._lock_read_state_projects(session)
            gate_ready.set()
            assert release_gate.wait(timeout=5)
            session.rollback()

    def insert_project():
        with ra_contexts.Context().session_manager() as session:
            writer_started.set()
            read_state.lock_projects(session, (project_uuid,))
            read_state.ensure_new_project(session, project_uuid)
            session.commit()

    def probe_maintenance():
        class StopAfterSchemaGate:
            def __init__(self, session):
                self._session = session

            def execute(self, statement, params=()):
                if "FROM m_workspace_read_state_projects_v1 AS state" in statement:
                    raise RuntimeError("candidate query reached")
                return self._session.execute(statement, params)

            def __getattr__(self, name):
                return getattr(self._session, name)

        with ra_contexts.Context().session_manager() as session:
            maintenance_started.set()
            with pytest.raises(RuntimeError, match="candidate query reached"):
                read_state.maintain_next_project(StopAfterSchemaGate(session))
            session.rollback()

    def read_project_mode():
        with ra_contexts.Context().session_manager() as session:
            reader_started.set()
            return read_state.project_mode(session, project_uuid)

    def probe_api_relation():
        context = messenger_context.WorkspaceMessengerAuthContext(req=object())
        request_started.set()
        with context.session_manager() as session:
            return session.execute(
                "SELECT COUNT(*) AS count FROM m_workspace_messages"
            ).fetchone()["count"]

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            gate_future = executor.submit(hold_final_gate)
            assert gate_ready.wait(timeout=5)
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_try_advisory_xact_lock_shared(
                        hashtextextended(%s, 0)
                    )
                    """,
                    (read_state.READ_STATE_SCHEMA_LOCK_KEY,),
                )
                assert cur.fetchone() == (False,)

            writer_future = executor.submit(insert_project)
            maintenance_future = executor.submit(probe_maintenance)
            reader_future = executor.submit(read_project_mode)
            request_future = executor.submit(probe_api_relation)
            assert writer_started.wait(timeout=5)
            assert maintenance_started.wait(timeout=5)
            assert reader_started.wait(timeout=5)
            assert request_started.wait(timeout=5)
            with pytest.raises(concurrent.futures.TimeoutError):
                writer_future.result(timeout=0.2)
            with pytest.raises(concurrent.futures.TimeoutError):
                maintenance_future.result(timeout=0.2)
            with pytest.raises(concurrent.futures.TimeoutError):
                reader_future.result(timeout=0.2)
            with pytest.raises(concurrent.futures.TimeoutError):
                request_future.result(timeout=0.2)
            release_gate.set()
            gate_future.result(timeout=5)
            writer_future.result(timeout=5)
            maintenance_future.result(timeout=5)
            assert reader_future.result(timeout=5) == read_state.PROJECT_MODE_LEGACY
            assert request_future.result(timeout=5) >= 0

        with db.cursor() as cur:
            cur.execute(
                """
                SELECT mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            assert cur.fetchone() == ("legacy",)
    finally:
        release_gate.set()
        with db.cursor() as cur:
            cur.execute(
                """
                DELETE FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                """,
                (project_uuid,),
            )


@pytest.mark.pre_retirement_schema
def test_compact_read_state_populated_downgrade_restores_legacy_flags(
    _database,
    db,
    monkeypatch,
):
    project_uuid = "10000000-0000-4000-8000-000000000901"
    user_uuids = (
        "10000000-0000-4000-8000-000000000902",
        "10000000-0000-4000-8000-000000000905",
        "10000000-0000-4000-8000-000000000906",
        "10000000-0000-4000-8000-000000000907",
        "10000000-0000-4000-8000-000000000908",
    )
    user_uuid = user_uuids[0]
    message_uuid = "10000000-0000-4000-8000-000000000903"
    next_message_uuid = "10000000-0000-4000-8000-000000000904"
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        user_uuid,
        "Compact migration rollback",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        user_uuid,
        "general",
        is_default=True,
    )
    for bound_user_uuid in user_uuids[1:]:
        conftest.seed_user_stream_binding(
            db,
            project_uuid,
            stream_uuid,
            bound_user_uuid,
        )
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    _set_historical_schema_fixture_before_forward_only_join(db)
    # Exercise the original compact-state downgrade on its own schema.
    engine.rollback_migration(READ_STATE_FORWARD_CORRECTION_MIGRATION_FILE)
    engine.rollback_migration(PROVIDER_HISTORY_DOWNGRADE_MIGRATION_FILE)
    engine.rollback_migration(PROJECT_DENSE_READ_SEQUENCE_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"read before rollback"}',
                'native', '{"kind":"native"}'
            )
            RETURNING ingest_sequence
            """,
            (message_uuid, project_uuid, stream_uuid, topic_uuid, user_uuid),
        )
        ingest_sequence = cur.fetchone()[0]
        assert ingest_sequence >= 281474976710656
        cur.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred
            ) VALUES (%s, %s, %s, FALSE, TRUE, FALSE)
            """,
            (message_uuid, user_uuid, project_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode
            ) VALUES (%s, 'compact')
            ON CONFLICT (project_id) DO UPDATE SET mode = 'compact'
            """,
            (project_uuid,),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits
            ) VALUES (
                %s,
                %s / 4096,
                set_bit(
                    B'0'::bit(4096),
                    (%s %% 4096)::integer,
                    1
                )
            )
            """,
            (
                (bound_user_uuid, ingest_sequence, ingest_sequence)
                for bound_user_uuid in user_uuids
            ),
        )

    migration_step = engine._load_migrations()[COMPACT_READ_STATE_MIGRATION_FILE]
    migration_module = __import__(migration_step.__class__.__module__)
    monkeypatch.setattr(migration_module, "DOWNGRADE_BATCH_SIZE", 2)
    with ra_contexts.Context().session_manager() as session:
        migration_module._prepare_downgrade_progress(session)
        migration_module._prepare_rollback_projects(session)
        rollback_view = session.execute(
            """
            SELECT read
            FROM m_workspace_user_messages_view
            WHERE uuid = %s AND user_uuid = %s
            """,
            (message_uuid, user_uuid),
        ).fetchone()
        assert rollback_view["read"] is True
        migration_module._lock_read_state_project(session, project_uuid)
        assert (
            migration_module._hydrate_legacy_flags_batch(
                session,
                project_uuid,
                1,
            )
            == 1
        )
        session.commit()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == ("rollback",)
        cur.execute(
            """
            SELECT processed_rows, completed_at
            FROM m_workspace_read_state_downgrade_v1
            WHERE project_id = %s
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == (1, None)
        # Simulate an application dual-write after the first committed
        # hydration batch. A retry must preserve that newer legacy value when
        # it resumes after the persisted cursor.
        cur.execute(
            """
            UPDATE m_workspace_user_read_chunks_v1
            SET read_bits = set_bit(
                    read_bits,
                    (%s %% 4096)::integer,
                    0
                ),
                updated_at = NOW()
            WHERE user_uuid = %s
              AND chunk_number = %s / 4096
            """,
            (ingest_sequence, user_uuid, ingest_sequence),
        )
        cur.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE, updated_at = NOW()
            WHERE uuid = %s AND user_uuid = %s
            """,
            (message_uuid, user_uuid),
        )

    engine.rollback_migration(COMPACT_READ_STATE_MIGRATION_FILE)
    # RestAlchemy flips the migration metadata only after downgrade() returns.
    # Calling the step again with the schema already committed away reproduces
    # a retry after a crash in that narrow window.
    with ra_contexts.Context().session_manager() as session:
        migration_step.downgrade(session)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid::text, read, pinned, starred
            FROM m_workspace_user_message_flags
            WHERE uuid = %s
              AND user_uuid = ANY(%s::uuid[])
            ORDER BY user_uuid
            """,
            (message_uuid, list(user_uuids)),
        )
        assert cur.fetchall() == [
            (
                bound_user_uuid,
                bound_user_uuid != user_uuid,
                bound_user_uuid == user_uuid,
                False,
            )
            for bound_user_uuid in user_uuids
        ]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_name = 'm_workspace_messages'
              AND column_name = 'ingest_sequence'
            """
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT to_regclass('m_workspace_messages_ingest_sequence_v1_seq'),
                   to_regclass(
                       'm_workspace_messages_legacy_ingest_sequence_v1_seq'
                   )
            """
        )
        assert cur.fetchone() == (None, None)
        cur.execute(
            """
            SELECT to_regclass('m_workspace_read_state_downgrade_v1')
            """
        )
        assert cur.fetchone() == (None,)
        cur.execute(
            """
            SELECT pg_try_advisory_lock(hashtextextended(%s::text, 0))
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == (True,)
        cur.execute(
            """
            SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == (True,)

    engine.apply_migration(COMPACT_READ_STATE_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (project_uuid,),
        )
        assert cur.fetchone() == ("legacy",)
        cur.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        assert cur.fetchone() == (None,)
        cur.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"written after reapply"}',
                'native', '{"kind":"native"}'
            )
            RETURNING ingest_sequence
            """,
            (next_message_uuid, project_uuid, stream_uuid, topic_uuid, user_uuid),
        )
        assert cur.fetchone()[0] >= 281474976710656
        cur.execute(
            """
            SELECT to_regclass('m_workspace_messages_ingest_sequence_idx'),
                   to_regclass(
                       'm_workspace_messages_project_ingest_sequence_idx'
                   ),
                   to_regclass(
                       'm_workspace_messages_topic_ingest_sequence_idx'
                   ),
                   to_regclass('m_workspace_messages_stream_read_page_idx'),
                   to_regclass('m_workspace_messages_topic_read_page_idx'),
                   to_regclass(
                       'm_workspace_messages_stream_ingest_sequence_idx'
                   ),
                   to_regclass('m_workspace_read_flags_project_message_idx'),
                   to_regclass('m_workspace_flags_project_message_user_idx')
            """
        )
        assert cur.fetchone() == (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        cur.execute(
            """
            SELECT uuid, applied
            FROM ra_migrations
            WHERE uuid = ANY(%s::text[])
            """,
            (
                [
                    COMPACT_READ_STATE_MIGRATION_UUID,
                    COMPACT_READ_STATE_INDEX_MIGRATION_UUID,
                ],
            ),
        )
        assert set(cur.fetchall()) == {
            (COMPACT_READ_STATE_MIGRATION_UUID, True),
            (COMPACT_READ_STATE_INDEX_MIGRATION_UUID, False),
        }

    engine.apply_migration(COMPACT_READ_STATE_INDEX_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT indexrelid::regclass::text, indisvalid
            FROM pg_index
            WHERE indexrelid IN (
                to_regclass('m_workspace_messages_ingest_sequence_idx'),
                to_regclass(
                    'm_workspace_messages_project_ingest_sequence_idx'
                ),
                to_regclass(
                    'm_workspace_messages_topic_ingest_sequence_idx'
                ),
                to_regclass('m_workspace_messages_stream_read_page_idx'),
                to_regclass('m_workspace_messages_topic_read_page_idx'),
                to_regclass(
                    'm_workspace_messages_stream_ingest_sequence_idx'
                ),
                to_regclass('m_workspace_read_flags_project_message_idx'),
                to_regclass('m_workspace_flags_project_message_user_idx')
            )
            """
        )
        assert set(cur.fetchall()) == {
            ("m_workspace_messages_ingest_sequence_idx", True),
            ("m_workspace_messages_project_ingest_sequence_idx", True),
            ("m_workspace_messages_topic_ingest_sequence_idx", True),
            ("m_workspace_messages_stream_read_page_idx", True),
            ("m_workspace_messages_topic_read_page_idx", True),
            ("m_workspace_messages_stream_ingest_sequence_idx", True),
            ("m_workspace_read_flags_project_message_idx", True),
            ("m_workspace_flags_project_message_user_idx", True),
        }
    # Rebuild every dependency removed by the historical rollback before the
    # current head. Applying only HEAD is unsafe because RestAlchemy does not
    # recursively reapply migrations whose bookkeeping row is now false.
    for migration_file in sorted(engine._load_migrations()):
        if (
            LAZY_PROVIDER_READ_MIGRATION_FILE
            <= migration_file
            <= PER_USER_DELIVERY_REPAIR_MIGRATION_FILE
        ):
            engine.apply_migration(migration_file)


@pytest.mark.pre_retirement_schema
def test_read_state_index_repair_recovers_partial_recursive_downgrade(
    _database,
    db,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(READ_STATE_INDEX_REPAIR_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            DROP INDEX m_workspace_read_state_active_maintenance_idx;
            DROP INDEX m_workspace_read_state_cleanup_maintenance_idx;
            """
        )

    engine.apply_migration(READ_STATE_INDEX_REPAIR_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT to_regclass('m_workspace_read_state_active_maintenance_idx'),
                   to_regclass('m_workspace_read_state_cleanup_maintenance_idx')
            """
        )
        assert cur.fetchone() == (
            "m_workspace_read_state_active_maintenance_idx",
            "m_workspace_read_state_cleanup_maintenance_idx",
        )


def test_compact_read_state_index_migration_recovers_invalid_index(
    _database,
    db,
):
    project_uuid = "10000000-0000-4000-8000-000000000911"
    user_uuid = "10000000-0000-4000-8000-000000000912"
    message_uuids = (
        "10000000-0000-4000-8000-000000000913",
        "10000000-0000-4000-8000-000000000914",
    )
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        user_uuid,
        "Compact index retry",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        user_uuid,
        "general",
        is_default=True,
    )
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"index retry"}',
                'native', '{"kind":"native"}'
            )
            """,
            (
                (
                    message_uuid,
                    project_uuid,
                    stream_uuid,
                    topic_uuid,
                    user_uuid,
                )
                for message_uuid in message_uuids
            ),
        )
        cur.execute('DROP INDEX "m_workspace_messages_ingest_sequence_idx"')
        cur.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = (
                SELECT ingest_sequence
                FROM m_workspace_messages
                WHERE uuid = %s
            )
            WHERE uuid = %s
            """,
            (message_uuids[0], message_uuids[1]),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                CREATE UNIQUE INDEX CONCURRENTLY
                    "m_workspace_messages_ingest_sequence_idx"
                    ON "m_workspace_messages" ("ingest_sequence")
                    WHERE "ingest_sequence" IS NOT NULL
                """
            )
        cur.execute(
            """
            SELECT indisvalid
            FROM pg_index
            WHERE indexrelid =
                'm_workspace_messages_ingest_sequence_idx'::regclass
            """
        )
        assert cur.fetchone() == (False,)
        cur.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = nextval(
                'm_workspace_messages_ingest_sequence_v1_seq'
            )
            WHERE uuid = %s
            """,
            (message_uuids[1],),
        )
        cur.execute(
            "DELETE FROM ra_migrations WHERE uuid = %s",
            (COMPACT_READ_STATE_INDEX_MIGRATION_UUID,),
        )

    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.apply_migration(COMPACT_READ_STATE_INDEX_MIGRATION_FILE)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT indisvalid
            FROM pg_index
            WHERE indexrelid =
                'm_workspace_messages_ingest_sequence_idx'::regclass
            """
        )
        assert cur.fetchone() == (True,)
        cur.execute(
            "SELECT applied FROM ra_migrations WHERE uuid = %s",
            (COMPACT_READ_STATE_INDEX_MIGRATION_UUID,),
        )
        assert cur.fetchone() == (True,)


def test_list_and_folder_projections_reuse_outer_visibility(_database, db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT relname, pg_get_viewdef(oid)
            FROM pg_class
            WHERE relname IN (
                'm_workspace_user_unread_messages_base_v1',
                'm_workspace_user_unread_messages_view',
                'm_workspace_user_streams',
                'm_workspace_user_topics_view',
                'm_folders_view'
            )
            ORDER BY relname
            """
        )
        definitions = dict(cur.fetchall())

    assert set(definitions) == {
        "m_workspace_user_unread_messages_base_v1",
        "m_workspace_user_unread_messages_view",
        "m_workspace_user_streams",
        "m_workspace_user_topics_view",
        "m_folders_view",
    }
    unread_base = definitions["m_workspace_user_unread_messages_base_v1"]
    protected_unread = definitions["m_workspace_user_unread_messages_view"]
    assert "m_confirmed_external_stream_access" not in unread_base
    assert "m_confirmed_external_stream_access" in protected_unread
    unread_sources = {
        "m_workspace_user_streams": "m_unread_user_messages",
        "m_workspace_user_topics_view": "m_workspace_user_topic_unread_counts_v1",
    }
    for name, unread_source in unread_sources.items():
        definition = definitions[name]
        assert "m_workspace_messages" in definition
        assert "m_workspace_user_messages_view" not in definition
        assert unread_source in definition
        assert definition.count("m_confirmed_external_stream_access") == 1
    folders = definitions["m_folders_view"]
    assert "system_folder_templates" in folders
    assert folders.count("FROM m_workspace_user_streams") == 1


def test_unread_views_split_legacy_and_compact_query_plans(_database, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[UNREAD_BRANCH_MIGRATION_FILE]
    migration_module = __import__(migration.__class__.__module__)
    with ra_contexts.Context().session_manager() as session:
        migration.upgrade(session)
    legacy_sql = migration_module.LEGACY_UNREAD_SELECT_SQL
    compact_sql = migration_module.COMPACT_UNREAD_SELECT_SQL
    assert "JOIN LATERAL" in legacy_sql
    assert "OFFSET 0" in legacy_sql
    assert "m_workspace_user_message_flags" in legacy_sql
    assert "read = FALSE" in legacy_sql
    for compact_relation in (
        "m_workspace_user_read_chunks_v1",
        "m_workspace_message_mentions_v1",
        "m_workspace_topic_message_stats_v1",
    ):
        assert compact_relation not in legacy_sql
    assert "m_workspace_user_message_flags" not in compact_sql
    assert "m_workspace_user_read_chunks_v1" in compact_sql
    assert "m_workspace_message_mentions_v1" in compact_sql

    view_names = (
        "m_workspace_user_unread_messages_base_v1",
        "m_workspace_user_topic_unread_counts_v1",
        "m_unread_user_messages",
        "m_workspace_user_streams",
        "m_workspace_user_topics_view",
        "m_folders_view",
    )
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT relname, pg_get_viewdef(oid)
            FROM pg_class
            WHERE relname = ANY(%s::text[])
            ORDER BY relname
            """,
            (list(view_names),),
        )
        definitions = dict(cur.fetchall())

    assert set(definitions) == set(view_names)
    unread_base = definitions["m_workspace_user_unread_messages_base_v1"]
    assert "UNION ALL" in unread_base
    assert unread_base.count("m_workspace_user_message_flags") == 1
    assert unread_base.count("m_workspace_user_read_chunks_v1") == 1
    assert unread_base.count("m_workspace_message_mentions_v1") == 1
    assert "message_flags.read = false" in unread_base
    assert "project.mode" in unread_base
    assert "'compact'::character varying" in unread_base
    assert "'rollback'::character varying" in unread_base

    topic_counts = definitions["m_workspace_user_topic_unread_counts_v1"]
    assert "m_workspace_user_message_flags" in topic_counts
    assert "m_workspace_topic_message_stats_v1" in topic_counts
    assert "m_workspace_user_topic_read_stats_v1" in topic_counts
    assert "m_workspace_message_mentions_v1" in topic_counts

    expected_dependencies = {
        "m_unread_user_messages": "m_workspace_user_topic_unread_counts_v1",
        "m_workspace_user_streams": "m_unread_user_messages",
        "m_workspace_user_topics_view": "m_workspace_user_topic_unread_counts_v1",
        "m_folders_view": "m_workspace_user_streams",
    }
    for view_name, dependency in expected_dependencies.items():
        assert dependency in definitions[view_name]


def test_unread_view_migration_downgrade_restores_mixed_mode_definitions(
    _database,
):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[UNREAD_BRANCH_MIGRATION_FILE]
    view_names = (
        "m_workspace_user_unread_messages_base_v1",
        "m_workspace_user_topic_unread_counts_v1",
    )

    def definitions(session):
        rows = session.execute(
            """
            SELECT relname, pg_get_viewdef(oid)
            FROM pg_class
            WHERE relname = ANY(%s::text[])
            ORDER BY relname
            """,
            (list(view_names),),
        ).fetchall()
        return {row["relname"]: row["pg_get_viewdef"] for row in rows}

    with ra_contexts.Context().session_manager() as session:
        migration.upgrade(session)
        split_definitions = definitions(session)
        migration.downgrade(session)
        previous_definitions = definitions(session)
        previous_base = previous_definitions["m_workspace_user_unread_messages_base_v1"]
        assert "UNION ALL" not in previous_base
        assert "CASE" in previous_base
        assert "LEFT JOIN m_workspace_user_message_flags" in previous_base
        assert "LEFT JOIN m_workspace_user_read_chunks_v1" in previous_base
        assert "LEFT JOIN m_workspace_message_mentions_v1" in previous_base

        migration.upgrade(session)
        assert definitions(session) == split_definitions


def test_stream_access_sql_excludes_message_and_unread_projections(_database, db):
    with db.cursor() as cur:
        cur.execute(
            "EXPLAIN (FORMAT JSON) "
            + messenger_dm_helpers._WORKSPACE_USER_STREAM_ACCESS_SQL,
            (sys_uuid.uuid4(), sys_uuid.uuid4(), sys_uuid.uuid4()),
        )
        plan = cur.fetchone()[0][0]["Plan"]

    def walk(node):
        yield node
        for child in node.get("Plans", []):
            yield from walk(child)

    relation_names = {
        node["Relation Name"] for node in walk(plan) if "Relation Name" in node
    }
    assert "m_workspace_messages" not in relation_names
    assert "m_workspace_user_streams" not in relation_names
    assert "m_workspace_user_message_flags" not in relation_names
    assert "m_workspace_user_read_chunks_v1" not in relation_names


def test_unread_branch_outputs_match_previous_views_in_all_read_modes(
    _database,
    db,
):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "Unread migration parity",
    )
    conftest.seed_user_stream_binding(db, project_uuid, stream_uuid, reader_uuid)
    topic_modes = ("mute", "follow", "unmute", "default")
    topic_uuids = {
        mode: conftest.seed_stream_topic(
            db,
            project_uuid,
            stream_uuid,
            owner_uuid,
            mode,
            is_default=mode == "default",
        )
        for mode in topic_modes
    }
    mention = f"[Reader](urn:user:{reader_uuid})"
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_stream_bindings
            SET notification_mode = 'mentions_only'
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, reader_uuid),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_user_topic_flags (
                uuid, user_uuid, project_id, notification_mode,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, NOW(), NOW())
            """,
            (
                (topic_uuids[mode], reader_uuid, project_uuid, mode)
                for mode in topic_modes
                if mode != "default"
            ),
        )
        for mode in topic_modes:
            content = mention if mode in {"unmute", "default"} else "plain"
            message_uuid = sys_uuid.uuid4()
            cur.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, created_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    jsonb_build_object(
                        'kind', 'markdown', 'content', %s::text
                    ),
                    NOW(), NOW()
                )
                RETURNING ingest_sequence
                """,
                (
                    message_uuid,
                    project_uuid,
                    stream_uuid,
                    topic_uuids[mode],
                    owner_uuid,
                    content,
                ),
            )
            ingest_sequence = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO m_workspace_user_message_flags (
                    uuid, user_uuid, project_id, read, pinned, starred
                ) VALUES (%s, %s, %s, FALSE, FALSE, FALSE)
                """,
                (message_uuid, reader_uuid, project_uuid),
            )
            cur.execute(
                """
                INSERT INTO m_workspace_topic_message_stats_v1 (
                    topic_uuid, project_id, stream_uuid, message_count,
                    last_ingest_sequence
                ) VALUES (%s, %s, %s, 1, %s)
                """,
                (topic_uuids[mode], project_uuid, stream_uuid, ingest_sequence),
            )
            if mode in {"unmute", "default"}:
                cur.execute(
                    """
                    INSERT INTO m_workspace_message_mentions_v1 (
                        message_uuid, user_uuid, project_id, stream_uuid,
                        topic_uuid, ingest_sequence
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        message_uuid,
                        reader_uuid,
                        project_uuid,
                        stream_uuid,
                        topic_uuids[mode],
                        ingest_sequence,
                    ),
                )

    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[UNREAD_BRANCH_MIGRATION_FILE]
    snapshot_queries = (
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_workspace_user_unread_messages_base_v1
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY message_uuid
        ) AS projection
        """,
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_workspace_user_topic_unread_counts_v1
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY topic_uuid
        ) AS projection
        """,
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_unread_user_messages
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
        ) AS projection
        """,
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_workspace_user_streams
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
        ) AS projection
        """,
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
        ) AS projection
        """,
        """
        SELECT to_jsonb(projection) AS value FROM (
            SELECT * FROM m_folders_view
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY uuid
        ) AS projection
        """,
    )

    def snapshot(session):
        return tuple(
            [row["value"] for row in session.execute(query, params).fetchall()]
            for query, params in (
                (query, (project_uuid, reader_uuid)) for query in snapshot_queries
            )
        )

    with ra_contexts.Context().session_manager() as session:
        migration.upgrade(session)
        for mode in ("legacy", "compact", "rollback"):
            session.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode
                ) VALUES (%s, %s)
                ON CONFLICT (project_id) DO UPDATE SET mode = EXCLUDED.mode
                """,
                (project_uuid, mode),
            )
            split_snapshot = snapshot(session)
            counts = session.execute(
                """
                SELECT topic.name, counts.unread_count,
                       counts.active_unread_count,
                       counts.passive_unread_count
                FROM m_workspace_user_topic_unread_counts_v1 AS counts
                JOIN m_workspace_stream_topics AS topic
                  ON topic.uuid = counts.topic_uuid
                WHERE counts.project_id = %s AND counts.user_uuid = %s
                ORDER BY topic.name
                """,
                (project_uuid, reader_uuid),
            ).fetchall()
            assert [
                (
                    row["name"],
                    row["unread_count"],
                    row["active_unread_count"],
                    row["passive_unread_count"],
                )
                for row in counts
            ] == [
                ("default", 1, 1, 0),
                ("follow", 1, 1, 0),
                ("mute", 1, 0, 1),
                ("unmute", 1, 1, 0),
            ]

            migration.downgrade(session)
            assert snapshot(session) == split_snapshot
            migration.upgrade(session)


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


def _assert_external_stream_snapshot_migration_resets_affected_event_cursors(
    _database,
    db,
):
    affected_user_uuid = "91000000-0000-4000-8000-000000000001"
    affected_project_uuid = "91000000-0000-4000-8000-000000000002"
    account_uuid = "91000000-0000-4000-8000-000000000003"
    chat_uuid = "91000000-0000-4000-8000-000000000004"
    unaffected_user_uuid = "91000000-0000-4000-8000-000000000005"
    unaffected_project_uuid = "91000000-0000-4000-8000-000000000006"
    stream_uuid = conftest.seed_user_stream(
        db,
        affected_project_uuid,
        affected_user_uuid,
        "External cursor reset",
    )
    conftest.seed_user_stream(
        db,
        unaffected_project_uuid,
        unaffected_user_uuid,
        "Native cursor unchanged",
    )
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = engine._load_migrations()[EXTERNAL_STREAM_SNAPSHOT_MIGRATION_FILE]

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
            (account_uuid, affected_user_uuid),
        )
        cur.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42',
                '{"provider_realm_uuid":"realm-42"}',
                'External cursor reset', TRUE, %s, %s
            )
            """,
            (
                chat_uuid,
                account_uuid,
                affected_user_uuid,
                affected_project_uuid,
                stream_uuid,
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version,
                pruned_through_epoch_version
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (project_id, user_uuid) DO UPDATE
            SET current_epoch_version = EXCLUDED.current_epoch_version,
                pruned_through_epoch_version =
                    EXCLUDED.pruned_through_epoch_version
            """,
            (
                (affected_project_uuid, affected_user_uuid, 17, 4),
                (unaffected_project_uuid, unaffected_user_uuid, 23, 5),
            ),
        )
        cur.execute(
            """
            SELECT project_id::text, epoch_generation
            FROM m_workspace_event_cursors
            WHERE project_id = ANY(%s::uuid[])
            """,
            ([affected_project_uuid, unaffected_project_uuid],),
        )
        generations_before = dict(cur.fetchall())
        cur.execute(
            """
            SELECT to_regclass(
                'public.m_workspace_visible_events_pre_messenger_v2'
            ) IS NOT NULL
            """
        )
        view_existed_before = cur.fetchone()[0]
        migration.upgrade(cur)
        cur.execute(
            """
            SELECT project_id::text, epoch_generation,
                   current_epoch_version, pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = ANY(%s::uuid[])
            """,
            ([affected_project_uuid, unaffected_project_uuid],),
        )
        cursors_after = {row[0]: row[1:] for row in cur.fetchall()}

    assert (
        cursors_after[affected_project_uuid][0]
        != generations_before[affected_project_uuid]
    )
    assert cursors_after[affected_project_uuid][1:] == (17, 17)
    assert cursors_after[unaffected_project_uuid] == (
        generations_before[unaffected_project_uuid],
        23,
        5,
    )
    with db.cursor() as cur:
        if view_existed_before:
            migration.downgrade(cur)
        else:
            cur.execute(
                'DROP VIEW IF EXISTS "m_workspace_visible_events_pre_messenger_v2"'
            )
        cur.execute(
            "DELETE FROM m_external_chats_v2 WHERE uuid = %s",
            (chat_uuid,),
        )
        cur.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        cur.execute(
            "DELETE FROM m_workspace_streams WHERE project_id = ANY(%s::uuid[])",
            ([affected_project_uuid, unaffected_project_uuid],),
        )
        cur.execute(
            "DELETE FROM messenger_streams WHERE project_id = ANY(%s::uuid[])",
            ([affected_project_uuid, unaffected_project_uuid],),
        )
        cur.execute(
            "DELETE FROM m_workspace_event_cursors WHERE project_id = ANY(%s::uuid[])",
            ([affected_project_uuid, unaffected_project_uuid],),
        )
        cur.execute(
            "DELETE FROM m_workspace_users WHERE uuid = ANY(%s::uuid[])",
            ([affected_user_uuid, unaffected_user_uuid],),
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
