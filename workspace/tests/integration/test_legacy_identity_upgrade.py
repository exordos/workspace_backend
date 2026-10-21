# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Preserve the published database migration and identity contracts."""

import json
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from migrations import zulip_upgrade_data
from workspace.tests.integration import conftest

LEGACY_HEAD = "0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"


V5_HEADS = sorted(
    path.name
    for path in conftest.MIGRATIONS_DIR.glob("0*.py")
    if "0190-" <= path.name < "0248-"
)


@pytest.mark.parametrize(
    "captured_table",
    ["workspace_zulip_v5_legacy_state", "workspace_zulip_v5_legacy_archive"],
)
def test_legacy_conversion_still_requires_coordinated_rollback(captured_table):
    engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
    with conftest.isolated_migration_database(engine.get_latest_migration()) as url:
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(f"CREATE TABLE {captured_table} (kind TEXT)")
        with pytest.raises(RuntimeError, match="coordinated pre-upgrade checkpoint"):
            engine.rollback_migration(
                next(conftest.MIGRATIONS_DIR.glob("0247-*.py")).name
            )


@pytest.mark.parametrize("starting_head", V5_HEADS)
def test_existing_v5_upgrade_does_not_rewrite_data_or_require_a_bridge(starting_head):
    with conftest.isolated_migration_database(starting_head) as url:
        project, owner = uuid.uuid4(), uuid.uuid4()
        with psycopg.connect(url, autocommit=True) as database:
            # A real pre-existing v5 installation never ran our capture in 0189.
            database.execute("DROP TABLE IF EXISTS workspace_zulip_v5_legacy_state")
            conftest.seed_workspace_user(database, owner, "v5-owner")
            stream = uuid.uuid4()
            database.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES(%s,%s)",
                (project, owner),
            )
            database.execute(
                "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name) VALUES(%s,%s,%s,'Existing v5 chat')",
                (stream, project, owner),
            )
            before = database.execute(
                "SELECT to_jsonb(stream) FROM messenger_streams stream WHERE uuid=%s",
                (stream,),
            ).fetchone()[0]
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        engine.apply_migration(engine.get_latest_migration())
        with psycopg.connect(url) as database:
            after = database.execute(
                "SELECT to_jsonb(stream) FROM messenger_streams stream WHERE uuid=%s",
                (stream,),
            ).fetchone()[0]
            assert {key: after[key] for key in before} == before
            assert database.execute(
                "SELECT to_regclass('public.workspace_zulip_v5_legacy_state')"
            ).fetchone() == (None,)


def test_migration_keeps_public_addresses_when_they_overlap_canonical_ids(http_server):
    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        project, owner, topic, canonical_a, canonical_b, public_a = (
            uuid.uuid4() for _ in range(6)
        )
        public_b = canonical_a
        with psycopg.connect(url, autocommit=True) as database:
            stream = conftest.seed_user_stream(
                database, project, owner, "Native conversation"
            )
            database.execute(
                "INSERT INTO m_workspace_stream_topics(uuid,project_id,stream_uuid,name) VALUES(%s,%s,%s,'Native topic')",
                (topic, project, stream),
            )
            for canonical, public, text in [
                (canonical_a, public_a, "First original message"),
                (canonical_b, public_b, "Second original message"),
            ]:
                database.execute(
                    "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) VALUES(%s,%s,%s,%s::jsonb)",
                    (
                        canonical,
                        project,
                        owner,
                        json.dumps({"kind": "markdown", "content": text}),
                    ),
                )
                database.execute(
                    "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
                    (public, project, canonical, stream, topic),
                )
                binding = uuid.uuid5(public, str(owner))
                database.execute(
                    'INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid,membership_generation,relation_role,visibility,permissions) VALUES(%s,%s,%s,%s,1,\'author\',\'visible\',\'{"read":true,"react":true,"star":true,"pin":true}\'::jsonb)',
                    (binding, project, public, owner),
                )
                database.execute(
                    "INSERT INTO messenger_user_message_states(uuid,project_id,placement_uuid,user_uuid,membership_generation) VALUES(%s,%s,%s,%s,1)",
                    (binding, project, public, owner),
                )
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        engine.apply_migration(engine.get_latest_migration())
        api = conftest.ApiClient(http_server, owner, project)
        for public, text in [
            (public_a, "First original message"),
            (public_b, "Second original message"),
        ]:
            response = api.get("/v1/messenger/messages/" + str(public))
            assert response.status_code == 200, response.text
            assert response.json()["payload"]["content"] == text


def test_data_reference_rewrite_preserves_literal_uuid_text():

    with conftest.isolated_migration_database(
        migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR)).get_latest_migration()
    ):
        project, old, new = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with contexts.Context().session_manager() as session:
            session.execute(
                "CREATE TEMP TABLE workspace_upgrade_ids(kind text,project_id uuid,old_uuid uuid,new_uuid uuid) ON COMMIT DROP"
            )
            session.execute(
                "INSERT INTO workspace_upgrade_ids VALUES('public',%s,%s,%s)",
                (project, old, new),
            )
            zulip_upgrade_data.rewrite_json(session)
            value = {
                "uuid": str(old),
                "content": f"[Linked message](urn:message:{old}) Literal example: {old}",
            }
            result = session.execute(
                "SELECT pg_temp.workspace_upgrade_text(%s,%s) AS value",
                (json.dumps(value), project),
            ).fetchone()["value"]
            converted = json.loads(result)
            assert converted["uuid"] == str(new)
            assert (
                converted["content"]
                == f"[Linked message](urn:message:{new}) Literal example: {old}"
            )


@pytest.mark.parametrize(
    "table,constraint",
    [
        (
            "m_workspace_topic_summary_jobs",
            "m_workspace_topic_summary_job_boundary_fkey",
        ),
        ("m_workspace_topic_summary_jobs", "m_workspace_topic_summary_job_topic_fkey"),
        (
            "m_workspace_topic_summary_journal",
            "m_workspace_topic_summary_journal_topic_fkey",
        ),
    ],
)
def test_upgrade_accepts_release_schemas_without_legacy_summary_boundary_constraints(
    table, constraint
):
    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        with psycopg.connect(url, autocommit=True) as database:
            database.execute("ALTER TABLE " + table + " DROP CONSTRAINT " + constraint)
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        engine.apply_migration(engine.get_latest_migration())


def test_data_conversion_merges_shared_conversations_without_losing_user_data():

    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head) as url:
        project, owner, destination, topic_destination, folder = (
            uuid.uuid4() for _ in range(5)
        )
        streams = [uuid.uuid4(), uuid.uuid4()]
        topics = [uuid.uuid4(), uuid.uuid4()]
        publics = [uuid.uuid4(), uuid.uuid4()]
        with psycopg.connect(url, autocommit=True) as db:
            conftest.seed_workspace_user(db, owner, "shared-conversation-owner")
            db.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES(%s,%s)",
                (project, owner),
            )
            db.execute(
                "INSERT INTO messenger_folders(uuid,project_id,title) VALUES(%s,%s,'Migration folder')",
                (folder, project),
            )
            db.execute(
                "INSERT INTO messenger_user_folder_bindings(uuid,project_id,user_uuid,folder_uuid) VALUES(%s,%s,%s,%s)",
                (uuid.uuid4(), project, owner, folder),
            )
            for index, (stream, topic, public) in enumerate(
                zip(streams, topics, publics)
            ):
                db.execute(
                    "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name) VALUES(%s,%s,%s,'Shared DM')",
                    (stream, project, owner),
                )
                db.execute(
                    "INSERT INTO messenger_topics(uuid,project_id,stream_uuid,name) VALUES(%s,%s,%s,'Shared topic')",
                    (topic, project, stream),
                )
                db.execute(
                    "INSERT INTO messenger_stream_bindings(uuid,project_id,stream_uuid,user_uuid,who_uuid,membership_generation) VALUES(%s,%s,%s,%s,%s,%s)",
                    (uuid.uuid4(), project, stream, owner, owner, index + 1),
                )
                db.execute(
                    "INSERT INTO messenger_user_topic_bindings(uuid,project_id,user_uuid,topic_uuid) VALUES(%s,%s,%s,%s)",
                    (uuid.uuid4(), project, owner, topic),
                )
                db.execute(
                    "INSERT INTO messenger_folder_items(uuid,project_id,user_uuid,folder_uuid,stream_uuid,order_index,chat_type) VALUES(%s,%s,%s,%s,%s,%s,'private')",
                    (uuid.uuid4(), project, owner, folder, stream, index),
                )
                db.execute(
                    "INSERT INTO m_workspace_drafts(uuid,project_id,user_uuid,stream_uuid,topic_uuid,payload) VALUES(%s,%s,%s,%s,%s,%s::jsonb)",
                    (
                        uuid.uuid4(),
                        project,
                        owner,
                        stream,
                        topic,
                        json.dumps({"kind": "markdown", "content": f"Draft {index}"}),
                    ),
                )
                db.execute(
                    "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) VALUES(%s,%s,%s,%s::jsonb)",
                    (
                        public,
                        project,
                        owner,
                        json.dumps({"kind": "markdown", "content": f"Message {index}"}),
                    ),
                )
                db.execute(
                    "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
                    (public, project, public, stream, topic),
                )
                binding = uuid.uuid5(public, str(owner))
                db.execute(
                    "INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid,membership_generation,relation_role,visibility,permissions) VALUES(%s,%s,%s,%s,%s,'author','visible','{\"read\":true}'::jsonb)",
                    (binding, project, public, owner, index + 1),
                )
                db.execute(
                    "INSERT INTO messenger_user_message_states(uuid,project_id,placement_uuid,user_uuid,binding_uuid,membership_generation,read_at,starred,pinned) VALUES(%s,%s,%s,%s,%s,%s,now(),true,true)",
                    (binding, project, public, owner, binding, index + 1),
                )
        mapping = {
            ("stream", str(project), str(stream)): str(destination)
            for stream in streams
        }
        mapping.update(
            {
                ("topic", str(project), str(topic)): str(topic_destination)
                for topic in topics
            }
        )
        with contexts.Context().session_manager() as session:
            zulip_upgrade_data.rekey(session, mapping)
        with psycopg.connect(url) as db:
            assert db.execute(
                "SELECT uuid FROM messenger_streams WHERE project_id=%s", (project,)
            ).fetchall() == [(destination,)]
            assert db.execute(
                "SELECT uuid,stream_uuid FROM messenger_topics WHERE project_id=%s",
                (project,),
            ).fetchall() == [(topic_destination, destination)]
            assert db.execute(
                "SELECT count(*) FROM m_workspace_drafts WHERE project_id=%s AND stream_uuid=%s AND topic_uuid=%s",
                (project, destination, topic_destination),
            ).fetchone() == (2,)
            assert db.execute(
                "SELECT count(*) FROM messenger_folder_items WHERE project_id=%s AND stream_uuid=%s",
                (project, destination),
            ).fetchone() == (1,)
            assert db.execute(
                "SELECT count(*) FROM messenger_message_placements WHERE project_id=%s AND stream_uuid=%s AND topic_uuid=%s",
                (project, destination, topic_destination),
            ).fetchone() == (2,)
            assert db.execute(
                "SELECT count(*) FROM messenger_user_message_states state JOIN messenger_message_placements placement ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid JOIN messenger_stream_bindings membership ON membership.project_id=state.project_id AND membership.user_uuid=state.user_uuid AND membership.stream_uuid=placement.stream_uuid AND membership.membership_generation=state.membership_generation WHERE state.project_id=%s AND state.read_at IS NOT NULL AND state.starred AND state.pinned",
                (project,),
            ).fetchone() == (2,)


def test_data_conversion_rewrites_nested_forward_links_without_changing_edit_time():
    import base64
    import hashlib

    from workspace.bridges.zulip import message_conversion

    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head) as url:
        project, owner, old, new, message = (uuid.uuid4() for _ in range(5))
        inner = f"[Original](urn:message:{old}) Plain UUID: {old}"
        encoded = base64.urlsafe_b64encode(inner.encode()).decode().rstrip("=")
        nested = f"[Forward](urn:forward:{old}?snapshot={encoded})"
        outer = base64.urlsafe_b64encode(nested.encode()).decode().rstrip("=")
        text = f"[Outer](urn:forward:{old}?snapshot={outer})"
        with psycopg.connect(url, autocommit=True) as db:
            conftest.seed_workspace_user(db, owner, "forward-owner")
            db.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES(%s,%s)",
                (project, owner),
            )
            db.execute(
                "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload,content_updated_at) VALUES(%s,%s,%s,%s::jsonb,to_timestamp(1691057093))",
                (
                    message,
                    project,
                    owner,
                    json.dumps({"kind": "markdown", "content": text}),
                ),
            )
            before = db.execute(
                "SELECT content_updated_at FROM messenger_messages WHERE uuid=%s",
                (message,),
            ).fetchone()[0]
        with contexts.Context().session_manager() as session:
            zulip_upgrade_data.rekey(
                session, {("public", str(project), str(old)): str(new)}
            )
        with psycopg.connect(url) as db:
            payload, edited, digest = db.execute(
                "SELECT payload,content_updated_at,content_hash FROM messenger_messages WHERE uuid=%s",
                (message,),
            ).fetchone()
        assert edited == before
        assert digest == hashlib.sha256(payload["content"].encode()).hexdigest()
        references = list(message_conversion.workspace_references(payload["content"]))
        assert references[0][1] == str(new)
        middle = message_conversion.parse_snapshot(references[0][2])
        nested_reference = next(message_conversion.workspace_references(middle))
        assert nested_reference[1] == str(new)
        assert (
            message_conversion.parse_snapshot(nested_reference[2])
            == f"[Original](urn:message:{new}) Plain UUID: {old}"
        )


def test_data_conversion_keeps_pending_projection_ordering_and_progress_on_new_ids():

    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head):
        project, old, new, event, task = (uuid.uuid4() for _ in range(5))
        with contexts.Context().session_manager() as session:
            session.execute(
                "CREATE TEMP TABLE workspace_upgrade_ids(kind text,project_id uuid,old_uuid uuid,new_uuid uuid) ON COMMIT DROP"
            )
            session.execute(
                "INSERT INTO workspace_upgrade_ids VALUES('public',%s,%s,%s)",
                (project, old, new),
            )
            session.execute(
                "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload) VALUES(%s,%s,'read_counters','message',%s,%s::jsonb)",
                (event, project, str(old), json.dumps({"placement_uuid": str(old)})),
            )
            task = session.execute(
                "UPDATE messenger_projection_tasks SET ordering_key=%s,progress_uuid=%s WHERE project_id=%s AND outbox_event_uuid=%s RETURNING uuid",
                (str(old), old, project, event),
            ).fetchone()["uuid"]
            zulip_upgrade_data.rewrite_json(session)
            row = session.execute(
                "SELECT ordering_key,progress_uuid,payload FROM messenger_projection_tasks WHERE uuid=%s",
                (task,),
            ).fetchone()
            assert row["ordering_key"] == str(new)
            assert row["progress_uuid"] == new
            assert row["payload"]["placement_uuid"] == str(new)


def test_data_conversion_releases_old_worker_leases_before_scopes_merge():

    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head):
        project, destination = uuid.uuid4(), uuid.uuid4()
        with contexts.Context().session_manager() as session:
            session.execute(
                "CREATE TEMP TABLE workspace_upgrade_ids(kind text,project_id uuid,old_uuid uuid,new_uuid uuid) ON COMMIT DROP"
            )
            for _ in range(2):
                old = uuid.uuid4()
                session.execute(
                    "INSERT INTO workspace_upgrade_ids VALUES('stream',%s,%s,%s)",
                    (project, old, destination),
                )
                session.execute(
                    "INSERT INTO messenger_projection_scope_leases(uuid,project_id,scope_kind,scope_key,owner,lease_expires_at) VALUES(%s,%s,'user-stream',%s,'stopped-worker',now()+interval '1 hour')",
                    (uuid.uuid4(), project, str(old)),
                )
            zulip_upgrade_data.rewrite_json(session)
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM messenger_projection_scope_leases WHERE project_id=%s",
                    (project,),
                ).fetchone()["n"]
                == 0
            )
