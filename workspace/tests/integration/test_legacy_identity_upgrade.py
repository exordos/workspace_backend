# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Convert legacy data and verify real HTTP behavior after native v5 import."""

import json
import uuid
from unittest import mock

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from migrations import zulip_upgrade_data
from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest
from workspace.upgrade import legacy_data

LEGACY_HEAD = "0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"


def _task(account_uuid, project_uuid, content):
    task = models.Task(
        project_id=project_uuid, external_account_uuid=account_uuid, content=content
    )
    task.insert()
    with contexts.Context().session_manager():
        restored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
        restored.execute()
    result = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    assert result.status == "done", result.error_description


@pytest.mark.parametrize(
    "installation_runner,source_case,private_chat",
    [
        (runner, source, False)
        for source in ("imported", "misattributed_native", "outgoing_native")
        for runner in (False, True, "resuming")
        if runner != "resuming" or source != "imported"
    ]
    + [(True, "imported", True), (True, "imported", "private_channel")],
)
@pytest.mark.parametrize("topic_is_native", [False, True])
@pytest.mark.parametrize("has_default_topic", [False, True])
def test_data_conversion_reimports_without_duplicate_legacy_resources(
    http_server,
    monkeypatch,
    has_default_topic,
    installation_runner,
    topic_is_native,
    source_case,
    private_chat,
):
    legacy_native = source_case != "imported"
    outgoing = source_case == "outgoing_native"
    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        project, owner, author, account, topic, public_message, realm, chat = (
            uuid.uuid4() for _ in range(8)
        )
        with psycopg.connect(url, autocommit=True) as db:
            stream = uuid.UUID(
                conftest.seed_user_stream(db, project, owner, "Migration channel")
            )
            conftest.seed_workspace_user(db, author, "legacy-author")
            db.execute(
                "UPDATE m_workspace_users SET source='zulip',provider_external_id='8',external_account_uuid=NULL WHERE uuid=%s",
                (author,),
            )
            db.execute(
                "INSERT INTO m_external_accounts_v2(uuid,owner_user_uuid,provider,settings,provider_realm_uuid,provider_owner_user_id) VALUES (%s,%s,'zulip',%s::jsonb,%s,'7')",
                (
                    account,
                    owner,
                    json.dumps(
                        {
                            "server_url": "https://chat.example.test",
                            "email": "owner@example.test",
                            "api_key": "synthetic-test-key",
                            "default_project_id": str(project),
                        }
                    ),
                    realm,
                ),
            )
            db.execute(
                "UPDATE m_workspace_users SET external_account_uuid=%s WHERE uuid=%s",
                (account, author),
            )
            db.execute(
                "INSERT INTO m_workspace_events(uuid,project_id,user_uuid,schema_version,object_type,action,payload) VALUES (%s,%s,%s,1,'external_account','created',%s::jsonb)",
                (
                    uuid.uuid4(),
                    project,
                    owner,
                    json.dumps(
                        {"kind": "external_account.created", "uuid": str(account)}
                    ),
                ),
            )
            db.execute(
                "UPDATE m_workspace_streams SET source_name='zulip',source=%s::jsonb WHERE project_id=%s AND uuid=%s",
                (
                    json.dumps(
                        {
                            "kind": "zulip",
                            "stream_id": 20,
                            "server_url": "https://chat.example.test",
                        }
                    ),
                    project,
                    stream,
                ),
            )
            db.execute(
                "INSERT INTO m_external_chats_v2(uuid,external_account_uuid,owner_user_uuid,provider,provider_chat_id,source,display_name,selected,project_id,projection_stream_uuid) VALUES (%s,%s,%s,'zulip','channel:20',%s::jsonb,'Migration channel',true,%s,%s)",
                (
                    chat,
                    account,
                    owner,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "participants": [
                                {"provider_user_id": "7"},
                                {"provider_user_id": "8"},
                            ],
                        }
                    ),
                    project,
                    stream,
                ),
            )
            db.execute(
                "INSERT INTO m_workspace_stream_topics(uuid,project_id,name,stream_uuid,source_name,source) VALUES (%s,%s,'Migration topic',%s,'zulip',%s::jsonb)",
                (
                    topic,
                    project,
                    stream,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "stream_id": 20,
                            "server_url": "https://chat.example.test",
                            "topic_name": "Migration topic",
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO m_workspace_messages(uuid,project_id,stream_uuid,user_uuid,topic_uuid,payload,source_name,source,external_account_uuid,provider_external_id,provider_metadata,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s::jsonb,'zulip',%s::jsonb,%s,'42',%s::jsonb,to_timestamp(1691057093),to_timestamp(1691057093))",
                (
                    public_message,
                    project,
                    stream,
                    author,
                    topic,
                    json.dumps({"kind": "markdown", "content": "Legacy body"}),
                    json.dumps(
                        {
                            "kind": "zulip",
                            "server_url": "https://chat.example.test",
                            "stream_id": 20,
                            "topic_name": "Migration topic",
                            "message_id": "42",
                        }
                    ),
                    account,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "external_id": "42",
                            "provider_realm_uuid": str(realm),
                            "account_uuid": str(account),
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO m_workspace_user_message_flags(uuid,project_id,user_uuid,read) VALUES (%s,%s,%s,true)",
                (public_message, project, owner),
            )
            if legacy_native:
                # The real legacy dataset also marked received provider messages
                # as native and attributed them to the importing account owner.
                db.execute(
                    "UPDATE m_workspace_messages SET source_name='native',source='{\"kind\":\"native\"}'::jsonb,user_uuid=%s WHERE uuid=%s",
                    (owner, public_message),
                )
                actual = db.execute(
                    "SELECT m.source_name,m.author_uuid,m.provider_message_id FROM messenger_messages m JOIN messenger_message_placements p ON p.project_id=m.project_id AND p.message_uuid=m.uuid WHERE COALESCE(p.legacy_public_uuid,p.uuid)=%s",
                    (public_message,),
                ).fetchone()
                assert actual == ("native", owner, "42")
            if topic_is_native:
                db.execute(
                    "UPDATE m_workspace_stream_topics SET source_name='native',source='{\"kind\":\"native\"}'::jsonb WHERE uuid=%s",
                    (topic,),
                )
            if has_default_topic:
                db.execute(
                    "UPDATE m_workspace_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
                    (topic, project, stream),
                )
            before = db.execute(
                "SELECT p.message_uuid,p.uuid FROM messenger_message_placements p WHERE p.project_id=%s AND COALESCE(p.legacy_public_uuid,p.uuid)=%s",
                (project, public_message),
            ).fetchone()
            assert before is not None
            if private_chat is True:
                # Legacy provider DMs have participants but no native peer FK.
                # Reconciliation restores the stream model before deleting the
                # old membership, which v5 rejects until the peer is converted.
                db.execute(
                    "UPDATE m_workspace_streams SET private=true,direct_user_uuid=NULL "
                    "WHERE project_id=%s AND uuid=%s",
                    (project, stream),
                )
                db.execute(
                    "UPDATE m_external_chats_v2 SET provider_chat_id='direct:7,8' WHERE uuid=%s",
                    (chat,),
                )
                former_member = uuid.uuid4()
                conftest.seed_workspace_user(db, former_member, "former-dm-member")
                conftest.seed_user_stream_binding(db, project, stream, former_member)
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        if installation_runner == "resuming":
            original_mapping = zulip_upgrade_data.make_mapping

            def previous_mapping(saved, accounts):
                mapping, streams = original_mapping(saved, accounts)
                return {
                    key: value
                    for key, value in mapping.items()
                    if key[0] not in ("message", "placement", "public")
                }, streams

            # Reproduce the interrupted installer: its schema is current, but
            # native provider rows retain old UUIDs and guessed deliveries.
            with mock.patch.object(
                zulip_upgrade_data, "make_mapping", side_effect=previous_mapping
            ):
                engine.apply_migration(engine.get_latest_migration())
            with contexts.Context().session_manager() as session:
                legacy_data.seed_deliveries(session, legacy_data.load_state(session))
        else:
            engine.apply_migration(engine.get_latest_migration())
        zulip = mock.Mock()
        zulip.check_connection.return_value = {
            "user_id": 7,
            "full_name": "Owner",
            "email": "owner@example.test",
        }
        zulip.get_users.return_value = [
            {
                "user_id": 7,
                "full_name": "Owner",
                "email": "owner@example.test",
                "is_active": True,
            },
            {
                "user_id": 8,
                "full_name": "Author",
                "email": "author@example.test",
                "is_active": True,
            },
        ]
        zulip.get_topic_notifications.return_value = []
        zulip.get_last_message_id.return_value = 42
        zulip.get_channels.return_value = [
            {
                "stream_id": 20,
                "name": "Migration channel",
                "description": "",
                "rendered_description": "",
                "date_created": 1691057093,
                "creator_id": 7,
                "invite_only": False,
                "is_web_public": False,
                "history_public_to_subscribers": True,
                "is_archived": False,
                "message_retention_days": None,
                "notification_mode": "all_messages",
            }
        ]
        zulip.get_messages.return_value = [
            {
                "id": 42,
                "type": "stream",
                "stream_id": 20,
                "subject": "Migration topic",
                "content": "Legacy body" if outgoing else "Provider refreshed text",
                "sender_id": 7 if outgoing else 8,
                "timestamp": 1691057093,
                "last_edit_timestamp": 1691057094,
                "is_me_message": False,
                "flags": ["read"],
                "reactions": [],
            }
        ]
        if private_chat == "private_channel":
            zulip.get_channels.return_value[0]["invite_only"] = True
        if private_chat is True:
            zulip.get_channels.return_value = []
            zulip.get_messages.return_value[0].update(
                type="private",
                subject="",
                display_recipient=[
                    {"id": 7, "email": "owner@example.test", "full_name": "Owner"},
                    {"id": 8, "email": "author@example.test", "full_name": "Author"},
                ],
            )
        monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
        if installation_runner:
            assert legacy_data.migrate() == 1
            assert legacy_data.migrate() == 0
        else:
            _task(account, project, models.InitializeConnectionContent())
            _task(account, project, models.ImportUsersContent())
            _task(account, project, models.ImportChannelsContent())
            with contexts.Context().session_manager() as session:
                saved = legacy_data.load_state(session)
                legacy_data.seed_deliveries(session, saved)
                current = external_models.ExternalAccount.objects.get_one(
                    filters={"uuid": filters.EQ(account)}
                )
                legacy_data.release_misattributed_deliveries(
                    session, current, zulip.get_messages.return_value, saved
                )
            _task(
                account,
                project,
                models.ImportHistoryContent(first_message_id=42, last_message_id=42),
            )
            with contexts.Context().session_manager() as session:
                legacy_data.promote_observed(session)
                for _ in range(10):
                    for state in models.ZulipProjectUserState.objects.get_all(
                        limit=100
                    ):
                        state.do()
                    for state in models.ZulipStreamState.objects.get_all(limit=100):
                        state.do()
                    for state in models.ZulipStreamBindingState.objects.get_all(
                        limit=100
                    ):
                        state.do()
                    models.ZulipTopicState.reconcile_batch(100)
                    models.ZulipMessageState.reconcile_batch(100)
        api = conftest.ApiClient(http_server, owner, project)
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, "chat.example.test")
        expected_stream = uuid.uuid5(
            namespace, "dm:7,8" if private_chat is True else "20"
        )
        expected_topic = uuid.uuid5(
            expected_stream, "" if private_chat is True else "Migration topic"
        )
        expected_canonical = uuid.uuid5(project, str(uuid.uuid5(namespace, "42")))
        expected_public = uuid.uuid5(expected_canonical, "zulip-placement")
        response = api.get("/v1/messenger/messages/" + str(expected_public))
        assert response.status_code == 200, response.text
        assert response.json()["payload"]["content"] == (
            "Legacy body" if outgoing else "Provider refreshed text"
        )
        assert response.json()["stream_uuid"] == str(expected_stream)
        assert response.json()["topic_uuid"] == str(expected_topic)
        stream_response = api.get("/v1/messenger/streams/" + str(expected_stream))
        assert stream_response.status_code == 200
        if private_chat == "private_channel":
            assert stream_response.json()["invite_only"] is True
        with psycopg.connect(url) as db:
            if private_chat is True:
                assert db.execute(
                    "SELECT owner_uuid,private,direct_user_uuid,private_index "
                    "FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                    (project, expected_stream),
                ).fetchone() == (
                    owner,
                    True,
                    uuid.uuid5(namespace, "user:8"),
                    "zulip:" + str(expected_stream),
                )
            assert db.execute(
                "SELECT count(*) FROM messenger_streams WHERE project_id=%s AND name='Migration channel'",
                (project,),
            ).fetchone() == (1,)
            assert db.execute(
                "SELECT p.message_uuid,p.uuid FROM messenger_message_placements p WHERE p.project_id=%s AND p.uuid=%s",
                (project, expected_public),
            ).fetchone() == (expected_canonical, expected_public)
            assert db.execute(
                "SELECT count(*) FROM messenger_messages WHERE project_id=%s",
                (project,),
            ).fetchone() == (1,)
            assert db.execute(
                "SELECT author_uuid,source_name FROM messenger_messages WHERE project_id=%s AND uuid=%s",
                (project, expected_canonical),
            ).fetchone() == (
                owner if outgoing else uuid.uuid5(namespace, "user:8"),
                "native" if outgoing else "zulip",
            )
            assert db.execute(
                "SELECT count(*) FROM bridge_zulip_v5_deliveries WHERE project_id=%s",
                (project,),
            ).fetchone() == (1 if outgoing else 0,)
            if installation_runner:
                assert db.execute(
                    "SELECT has_completed_backfill,status FROM m_external_accounts_v2 WHERE uuid=%s",
                    (account,),
                ).fetchone() == (True, "active")
                archived = db.execute(
                    "SELECT data FROM workspace_zulip_v5_legacy_archive WHERE kind='message'"
                ).fetchone()[0]
                assert archived["uuid"] == str(before[0])
                assert archived["author_uuid"] == str(
                    owner if legacy_native else author
                )
                assert archived["payload"]["content"] == "Legacy body"
                assert archived["placements"][0]["uuid"] == str(before[1])
                assert archived["placements"][0]["legacy_public_uuid"] == str(
                    public_message
                )
                assert db.execute(
                    "SELECT data->'settings'->>'api_key' FROM workspace_zulip_v5_legacy_archive WHERE kind='account'"
                ).fetchone() == (None,)
            assert db.execute(
                "SELECT count(*) FROM messenger_topics WHERE project_id=%s AND stream_uuid=%s",
                (project, expected_stream),
            ).fetchone() == (1,)
        zulip.send_message.assert_not_called()
        zulip.edit_message.assert_not_called()


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
    from migrations import zulip_upgrade_data

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
    from migrations import zulip_upgrade_data

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

    from migrations import zulip_upgrade_data
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
    from migrations import zulip_upgrade_data

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
    from migrations import zulip_upgrade_data

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


@pytest.mark.parametrize("page_kind", ["empty", "unrelated", "matched"])
def test_delivery_sender_check_is_bounded_with_fresh_migration_statistics(page_kind):
    head = migrations.MigrationEngine(
        str(conftest.MIGRATIONS_DIR)
    ).get_latest_migration()
    with conftest.isolated_migration_database(head) as url:
        project, owner, account_uuid = (uuid.uuid4() for _ in range(3))
        with psycopg.connect(url, autocommit=True) as db:
            stream = conftest.seed_user_stream(db, project, owner, "Migration scale")
            db.execute(
                "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,external_account_id,settings) VALUES(%s,%s,%s,'zulip','chat.example.test:7','{\"server_url\":\"https://chat.example.test\"}'::jsonb)",
                (account_uuid, project, owner),
            )
            # Match the first real history page after a bulk schema conversion:
            # thousands of acknowledged legacy sends and no updated statistics.
            db.execute("""
                CREATE TABLE workspace_zulip_v5_legacy_state(
                    kind text,identity_key text,data jsonb,PRIMARY KEY(kind,identity_key));
                ALTER TABLE workspace_zulip_v5_legacy_state SET(autovacuum_enabled=false);
                ALTER TABLE bridge_zulip_v5_deliveries SET(autovacuum_enabled=false);
                CREATE TEMP TABLE legacy_delivery_noise AS
                    SELECT gen_random_uuid() AS uuid,100000+i AS remote_id
                    FROM generate_series(1,5226) i;
                ALTER TABLE messenger_messages DISABLE TRIGGER USER;
            """)
            db.execute(
                'INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload,content_updated_at) SELECT uuid,%s,%s,\'{"kind":"markdown","content":"Legacy"}\'::jsonb,NOW() FROM legacy_delivery_noise',
                (project, owner),
            )
            db.execute("ALTER TABLE messenger_messages ENABLE TRIGGER USER")
            db.execute(
                """INSERT INTO workspace_zulip_v5_legacy_state
                SELECT 'message',uuid::text,jsonb_build_object(
                    'uuid',uuid,'project_id',%s::text,'source_name','native',
                    'provider_message_id',remote_id::text,
                    'placements',jsonb_build_array(jsonb_build_object('stream_uuid',%s::text)))
                FROM legacy_delivery_noise""",
                (project, stream),
            )
            db.execute(
                """INSERT INTO bridge_zulip_v5_deliveries(
                    uuid,project_id,message_uuid,stream_uuid,external_account_uuid,
                    hostname,external_account_id,recipient,zulip_message_id)
                SELECT messenger_uuid_v5(uuid,'legacy-delivery:'||%s::text),%s,uuid,%s,%s,
                    'chat.example.test','chat.example.test:7','{"to":20}'::jsonb,remote_id
                FROM legacy_delivery_noise""",
                (stream, project, stream, account_uuid),
            )
        with contexts.Context().session_manager() as session:
            account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(account_uuid)}
            )
            saved = legacy_data.load_state(session)
            session.execute("SET LOCAL statement_timeout=500")
            with mock.patch.object(
                session, "execute", wraps=session.execute
            ) as execute:
                legacy_data.release_misattributed_deliveries(
                    session,
                    account,
                    []
                    if page_kind == "empty"
                    else [{"id": 1, "sender_id": 8}]
                    if page_kind == "unrelated"
                    else [
                        {"id": 100001, "sender_id": 8},
                        {"id": 100002, "sender_id": 7},
                        {"id": 1, "sender_id": 8},
                    ],
                    saved,
                )
                # The slow production plan joined all legacy rows to all
                # deliveries before discovering that the provider page was empty.
                if page_kind != "matched":
                    execute.assert_not_called()
            assert session.execute(
                "SELECT count(*) AS n FROM bridge_zulip_v5_deliveries WHERE project_id=%s",
                (project,),
            ).fetchone()["n"] == (5225 if page_kind == "matched" else 5226)
            retained = session.execute(
                "SELECT zulip_message_id FROM bridge_zulip_v5_deliveries WHERE project_id=%s AND zulip_message_id IN (100001,100002) ORDER BY zulip_message_id",
                (project,),
            ).fetchall()
            assert [row["zulip_message_id"] for row in retained] == (
                [100002] if page_kind == "matched" else [100001, 100002]
            )
