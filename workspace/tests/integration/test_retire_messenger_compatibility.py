# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Preserve populated Messenger data while retiring duplicate storage."""

import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.messenger_api.api import v2_store
from workspace.services.messenger_workers import v2_projection

from workspace.tests.integration import conftest


BASE = "0230-Avoid-redundant-project-user-writes-and-bound-Zulip-reconciliation-batches-fc01aa.py"
CUTOVER = "0231-Retire-duplicate-Messenger-compatibility-tables-bc9678.py"


@pytest.fixture
def before_cutover(_database):
    with conftest.isolated_migration_database(BASE) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            yield connection


def _seed(connection):
    project, user, stream, topic, message, placement, alias, binding, file, draft = (
        uuid.uuid4() for _ in range(10)
    )
    conftest.seed_workspace_user(connection, user, "cutover-user")
    connection.execute(
        "SELECT messenger_v2_register_project_user(%s, %s)", (project, user)
    )
    connection.execute(
        "INSERT INTO messenger_streams (uuid, project_id, owner_uuid, name) VALUES (%s,%s,%s,'Preserved stream')",
        (stream, project, user),
    )
    connection.execute(
        "INSERT INTO messenger_topics (uuid,project_id,stream_uuid,name) VALUES (%s,%s,%s,'Preserved topic')",
        (topic, project, stream),
    )
    connection.execute(
        "UPDATE messenger_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
        (topic, project, stream),
    )
    connection.execute(
        "INSERT INTO messenger_stream_bindings (uuid, project_id, stream_uuid, user_uuid, who_uuid, role, notification_mode) "
        "VALUES (%s,%s,%s,%s,%s,'owner','all_messages')",
        (binding, project, stream, user, user),
    )
    connection.execute(
        "INSERT INTO messenger_user_topic_bindings (uuid,project_id,user_uuid,topic_uuid,notification_mode) "
        "VALUES (%s,%s,%s,%s,'follow') ON CONFLICT DO NOTHING",
        (uuid.uuid4(), project, user, topic),
    )
    connection.execute(
        "INSERT INTO messenger_messages (uuid,project_id,author_uuid,payload) "
        'VALUES (%s,%s,%s,\'{"kind":"markdown","content":"Preserved content"}\')',
        (message, project, user),
    )
    connection.execute(
        "INSERT INTO messenger_message_placements (uuid,project_id,message_uuid,stream_uuid,topic_uuid,legacy_public_uuid) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (placement, project, message, stream, topic, alias),
    )
    connection.execute(
        "INSERT INTO messenger_user_message_bindings (uuid,project_id,placement_uuid,user_uuid,membership_generation,relation_role,visibility,permissions) "
        'VALUES (%s,%s,%s,%s,1,\'author\',\'visible\',\'{"read":true,"react":true,"star":true,"pin":true}\')',
        (uuid.uuid4(), project, placement, user),
    )
    connection.execute(
        "INSERT INTO messenger_user_message_states (uuid,project_id,placement_uuid,user_uuid,membership_generation,read_at,pinned,starred) "
        "VALUES (%s,%s,%s,%s,1,NOW(),true,true)",
        (uuid.uuid4(), project, placement, user),
    )
    connection.execute(
        "INSERT INTO messenger_message_reaction_facts (uuid,project_id,canonical_message_uuid,placement_uuid,user_uuid,emoji_name) "
        "VALUES (%s,%s,%s,%s,%s,'thumbs_up')",
        (uuid.uuid4(), project, message, placement, user),
    )
    connection.execute(
        "INSERT INTO m_workspace_files (uuid,project_id,user_uuid,stream_uuid,name,description,content_type,size_bytes,hash,storage_type,storage_id,storage_object_id,acl_mode) "
        "VALUES (%s,%s,%s,%s,'preserved.txt','','text/plain',7,'hash','file','','preserved.txt','stream')",
        (file, project, user, stream),
    )
    connection.execute(
        "INSERT INTO m_workspace_drafts (uuid,project_id,user_uuid,stream_uuid,topic_uuid,payload) "
        'VALUES (%s,%s,%s,%s,%s,\'{"kind":"markdown","content":"Preserved draft"}\')',
        (draft, project, user, stream, topic),
    )
    connection.execute(
        "INSERT INTO m_workspace_topic_summary_jobs (topic_uuid,project_id,status,boundary_message_uuid,effective_prompt,prompt_fingerprint) "
        "VALUES (%s,%s,'succeeded',%s,'Preserved prompt','fingerprint')",
        (topic, project, alias),
    )
    connection.execute(
        "INSERT INTO m_workspace_topic_summary_journal (uuid,topic_uuid,project_id,summary,boundary_message_uuid,boundary_message_created_at,generated_at) "
        "VALUES (%s,%s,%s,'Preserved summary',%s,NOW(),NOW())",
        (uuid.uuid4(), topic, project, alias),
    )
    return dict(
        project=project,
        user=user,
        stream=stream,
        topic=topic,
        message=message,
        placement=placement,
        alias=alias,
        binding=binding,
        file=file,
        draft=draft,
    )


def _upgrade():
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.apply_migration(CUTOVER)


def test_populated_cutover_preserves_messages_state_files_drafts_and_summary(
    before_cutover,
):
    connection = before_cutover
    ids = _seed(connection)
    before = connection.execute(
        "SELECT message.payload, state.read_at, state.pinned, state.starred, topic.notification_mode FROM messenger_api_user_messages_v1 AS message "
        "JOIN messenger_user_message_states AS state ON state.project_id=message.project_id AND state.placement_uuid=message.uuid AND state.user_uuid=message.user_uuid "
        "JOIN messenger_user_topic_bindings AS topic ON topic.project_id=message.project_id AND topic.topic_uuid=message.topic_uuid AND topic.user_uuid=message.user_uuid "
        "WHERE message.project_id=%s",
        (ids["project"],),
    ).fetchall()
    assert before
    _upgrade()
    assert (
        connection.execute(
            "SELECT message.payload, state.read_at, state.pinned, state.starred, topic.notification_mode FROM messenger_api_user_messages_v1 AS message "
            "JOIN messenger_user_message_states AS state ON state.project_id=message.project_id AND state.placement_uuid=message.uuid AND state.user_uuid=message.user_uuid "
            "JOIN messenger_user_topic_bindings AS topic ON topic.project_id=message.project_id AND topic.topic_uuid=message.topic_uuid AND topic.user_uuid=message.user_uuid "
            "WHERE message.project_id=%s",
            (ids["project"],),
        ).fetchall()
        == before
    )
    for relation in (
        "m_workspace_messages",
        "m_workspace_streams",
        "m_workspace_stream_bindings",
        "m_workspace_stream_topics",
        "m_folders",
        "m_folder_items",
        "m_workspace_user_message_flags",
        "m_workspace_user_read_chunks_v1",
        "m_workspace_user_topic_flags",
        "m_workspace_message_reactions",
        "m_workspace_message_mentions_v1",
        "m_workspace_project_ingest_ranges_v2",
        "m_workspace_read_memberships_v1",
        "m_workspace_read_state_compaction_v1",
        "m_workspace_read_state_projects_v1",
        "m_workspace_topic_message_stats_v1",
        "m_workspace_user_read_revisions_v1",
        "m_workspace_user_topic_read_stats_v1",
    ):
        assert connection.execute("SELECT to_regclass(%s)", (relation,)).fetchone() == (
            None,
        )
    assert connection.execute(
        "SELECT uuid FROM m_workspace_visible_files_v1 WHERE uuid=%s", (ids["file"],)
    ).fetchone() == (ids["file"],)
    assert connection.execute(
        "SELECT payload->>'content' FROM m_workspace_drafts WHERE uuid=%s",
        (ids["draft"],),
    ).fetchone() == ("Preserved draft",)
    assert connection.execute(
        "SELECT boundary_message_uuid FROM m_workspace_topic_summary_jobs WHERE topic_uuid=%s",
        (ids["topic"],),
    ).fetchone() == (ids["placement"],)
    assert connection.execute(
        "SELECT summary,boundary_message_uuid FROM m_workspace_topic_summary_journal WHERE topic_uuid=%s",
        (ids["topic"],),
    ).fetchone() == ("Preserved summary", ids["placement"])
    assert connection.execute(
        "SELECT legacy_public_uuid FROM messenger_message_placements WHERE uuid=%s",
        (ids["placement"],),
    ).fetchone() == (ids["alias"],)
    assert connection.execute(
        "SELECT emoji_name FROM messenger_message_reaction_facts WHERE project_id=%s",
        (ids["project"],),
    ).fetchone() == ("thumbs_up",)
    with contexts.Context().session_manager():
        store = v2_store.MessengerV2Store(ids["project"], ids["user"])
        public = store.get_resource("messages", ids["alias"])
        assert public["uuid"] == str(ids["placement"])
        assert public["payload"]["content"] == "Preserved content"
    assert (
        connection.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace='public'::regnamespace AND prokind='f' "
            "AND pg_get_functiondef(oid) ~ '\\m(m_workspace_messages|m_workspace_streams|m_workspace_stream_bindings|m_workspace_stream_topics|m_folders|m_folder_items)\\M'"
        ).fetchall()
        == []
    )
    # Deleting the canonical root owns cleanup after every FK is retargeted.
    connection.execute("DELETE FROM messenger_streams WHERE uuid=%s", (ids["stream"],))
    for table in (
        "m_workspace_files",
        "m_workspace_drafts",
        "m_workspace_topic_summary_jobs",
        "m_workspace_topic_summary_journal",
    ):
        assert connection.execute(
            f"SELECT count(*) FROM {table} WHERE project_id=%s", (ids["project"],)
        ).fetchone() == (0,)


def test_cutover_refuses_to_discard_unprojected_messages(before_cutover):
    connection = before_cutover
    ids = _seed(connection)
    connection.execute("ALTER TABLE m_workspace_messages DISABLE TRIGGER USER")
    connection.execute(
        "INSERT INTO m_workspace_messages (uuid,project_id,stream_uuid,user_uuid,topic_uuid,payload,ingest_sequence) "
        'VALUES (%s,%s,%s,%s,%s,\'{"kind":"markdown","content":"Unprojected content"}\',1000000)',
        (uuid.uuid4(), ids["project"], ids["stream"], ids["user"], ids["topic"]),
    )
    with pytest.raises(Exception, match="unprojected messages remain"):
        _upgrade()
    assert connection.execute(
        "SELECT to_regclass('m_workspace_messages')"
    ).fetchone() == ("m_workspace_messages",)
    assert connection.execute(
        "SELECT count(*) FROM messenger_messages WHERE project_id=%s", (ids["project"],)
    ).fetchone() == (1,)


def test_cutover_preserves_bitmap_reads_after_legacy_flags_were_cleaned(before_cutover):
    connection = before_cutover
    ids = _seed(connection)
    connection.execute(
        "ALTER TABLE messenger_user_message_states DISABLE TRIGGER messenger_v2_mirror_state_to_legacy"
    )
    connection.execute(
        "DELETE FROM messenger_user_message_states WHERE project_id=%s",
        (ids["project"],),
    )
    connection.execute(
        "ALTER TABLE m_workspace_user_message_flags DISABLE TRIGGER USER"
    )
    connection.execute(
        "DELETE FROM m_workspace_user_message_flags WHERE project_id=%s",
        (ids["project"],),
    )
    connection.execute(
        "UPDATE m_workspace_read_state_projects_v1 SET mode='rollback' WHERE project_id=%s",
        (ids["project"],),
    )
    (sequence,) = connection.execute(
        "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid=%s",
        (ids["alias"],),
    ).fetchone()
    connection.execute(
        "INSERT INTO m_workspace_user_read_chunks_v1 (user_uuid,chunk_number,read_bits) "
        "VALUES (%s,%s,set_bit(repeat('0',4096)::bit(4096),%s,1)) "
        "ON CONFLICT (user_uuid,chunk_number) DO UPDATE SET read_bits=EXCLUDED.read_bits",
        (ids["user"], sequence // 4096, sequence % 4096),
    )
    _upgrade()
    # This cutover predates the publication journal. Draining its projection
    # work must not require later publication tables or process that queue.
    assert connection.execute(
        "SELECT to_regclass('messenger_publication_outbox')"
    ).fetchone() == (None,)
    with contexts.Context().session_manager() as session:
        assert (
            v2_projection.drain_projection_queue(
                session, "integration:compatibility-cutover"
            )
            > 0
        )
    assert connection.execute(
        "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s",
        (ids["project"],),
    ).fetchone() == (True,)
