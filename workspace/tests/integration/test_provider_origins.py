# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
import uuid

import psycopg
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import history
from workspace.bridges.zulip.dm import models as zulip_models
from workspace.bridges.zulip.dm import reactions
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as message_tests


message_sources = message_tests.message_sources
MIGRATION = "0263-Normalize-provider-identities-and-remove-source-payloads-b52ac4.py"
BASE = "0262-Reconvert-stored-Zulip-silent-user-mentions-80e205.py"


def test_imported_objects_keep_provider_identity_and_native_objects_stay_native(
    message_sources, db, api
):
    account, observed = message_sources()
    message_tests._prepare(account)
    with contexts.Context().session_manager():
        zulip_models.ZulipMessageState.reconcile_batch(limit=100)
    expected = {
        "messenger_streams": "42",
        "messenger_stream_bindings": "42:7",
        "messenger_topics": "42:General",
        "messenger_user_topic_bindings": "42:General:7",
        "messenger_messages": "42",
        "messenger_user_message_states": "42:7",
    }
    for table, identity in expected.items():
        rows = db.execute(
            f"SELECT source_name,provider_uuid,provider_identity FROM {table} WHERE project_id=%s",
            (account.project_id,),
        ).fetchall()
        assert rows == [("zulip", account.provider_uuid, identity)], table
    assert db.execute(
        "SELECT hostname FROM messenger_providers WHERE uuid=%s",
        (account.provider_uuid,),
    ).fetchone() == ("zulip.example.com",)
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    placement = message_tests._placements(account)[0]
    response = api.get(f"/v1/messenger/messages/{placement.uuid}")
    assert response.status_code == 200, response.text
    imported = response.json()
    assert "source" not in imported
    assert imported["provider_uuid"] == str(account.provider_uuid)
    assert imported["provider_identity"] == str(observed.message_id)
    db.execute(
        "UPDATE messenger_topics SET name='Local title' WHERE project_id=%s",
        (account.project_id,),
    )
    assert db.execute(
        "SELECT provider_identity FROM messenger_topics WHERE project_id=%s",
        (account.project_id,),
    ).fetchone() == ("42:General",)
    native = api.post("/v1/messenger/streams/", json={"name": "Native"})
    assert native.status_code == 201, native.text
    assert native.json()["source_name"] == "native"
    assert native.json().get("provider_uuid") is None
    assert "source" not in native.json()
    db.execute(
        "DELETE FROM messenger_streams WHERE project_id=%s AND source_name='native'",
        (account.project_id,),
    )


def test_same_external_identity_is_scoped_by_provider(message_sources, db):
    hostname = uuid.uuid4().hex + ".example.com"
    provider = external_models.Provider(hostname=hostname)
    provider.insert()
    first, _ = message_sources(host=hostname)
    second, _ = message_sources(project_id=first.project_id, host="two.example.com")
    assert first.external_account_id == second.external_account_id == "7"
    assert first.provider_uuid != second.provider_uuid
    assert first.provider_uuid == provider.uuid
    assert first.get_host() == hostname
    assert second.get_host() == "two.example.com"
    message_tests._prepare(first)
    with contexts.Context().session_manager():
        zulip_models.ZulipMessageState.reconcile_batch(limit=100)
    rows = db.execute(
        "SELECT provider_uuid,provider_identity FROM messenger_messages WHERE project_id=%s",
        (first.project_id,),
    ).fetchall()
    assert set(rows) == {(first.provider_uuid, "42"), (second.provider_uuid, "42")}
    with contexts.Context().session_manager():
        reactions.observe_snapshot(
            first,
            [
                {
                    "id": 42,
                    "reactions": [
                        {
                            "user_id": 7,
                            "emoji_name": "thumbs_up",
                            "emoji_code": "1f44d",
                            "reaction_type": "unicode_emoji",
                        }
                    ],
                }
            ],
            datetime.datetime.now(datetime.timezone.utc),
        )
        reactions.ReactionChange.reconcile_batch(100)
    assert db.execute(
        "SELECT provider_uuid,provider_identity FROM messenger_message_reaction_facts WHERE project_id=%s",
        (first.project_id,),
    ).fetchall() == [(provider.uuid, "42:7:thumbs_up")]


def test_shared_hostname_registers_one_provider_and_supports_two_accounts_for_one_owner(
    message_sources, db
):
    first, _ = message_sources()
    account = external_models.ExternalAccount(
        project_id=first.project_id,
        owner_user_uuid=first.owner_user_uuid,
        provider="zulip",
        external_account_id="7",
        settings={"server_url": "https://second.example.com"},
    )
    account.insert()
    try:
        assert account.provider_uuid != first.provider_uuid
        assert (
            external_models.Provider.register("zulip.example.com").uuid
            == first.provider_uuid
        )
        assert db.execute(
            "SELECT count(*) FROM messenger_providers WHERE hostname='zulip.example.com'"
        ).fetchone() == (1,)
    finally:
        account.delete()


def test_provider_migration_backfills_origins_snapshots_and_preserves_queued_work(
    _database,
):
    with conftest.isolated_migration_database(BASE) as url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        owner, project, account_uuid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        hostname = "migration.example.com"
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, hostname)
        stream_uuid = uuid.uuid5(namespace, "42")
        message_uuid = uuid.uuid5(project, str(uuid.uuid5(namespace, "77")))
        task_uuid = uuid.uuid4()
        settings = {
            "server_url": "https://migration.example.com:8443/path/",
            "email": "test@example.com",
            "api_key": "test-key",
        }
        old_revision = hashlib.sha256(
            json.dumps(
                [str(project), str(owner), "zulip", hostname + ":7", settings],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with psycopg.connect(url, autocommit=True) as connection:
            conftest.seed_workspace_user(connection, owner, "provider-migration")
            legacy_account_uuid = uuid.uuid4()
            connection.execute(
                "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,settings) VALUES (%s,%s,%s,'example','{}')",
                (legacy_account_uuid, project, owner),
            )
            connection.execute(
                "INSERT INTO messenger_project_users(project_id,user_uuid) VALUES (%s,%s)",
                (project, owner),
            )
            connection.execute(
                "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,external_account_id,settings) VALUES (%s,%s,%s,'zulip',%s,%s::jsonb)",
                (account_uuid, project, owner, hostname + ":7", json.dumps(settings)),
            )
            channel = zulip_models.ZulipChannel(
                external_account_uuid=account_uuid,
                stream_uuid=stream_uuid,
                stream_id=42,
                name="Channel",
                description="",
                rendered_description="",
                invite_only=False,
                is_web_public=False,
                history_public_to_subscribers=True,
                is_archived=False,
            )
            channel.insert()
            connection.execute(
                "INSERT INTO messenger_streams(uuid,project_id,owner_uuid,name,source_name,source) VALUES (%s,%s,%s,'Channel','zulip','{\"kind\":\"zulip\"}')",
                (stream_uuid, project, owner),
            )
            connection.execute(
                'INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload,source_name,source) VALUES (%s,%s,%s,\'{"kind":"markdown","content":"Text"}\',\'zulip\',\'{"kind":"zulip"}\')',
                (message_uuid, project, owner),
            )
            content = zulip_models.MessageContent(
                zulip_content="Text",
                workspace_content="Text",
                zulip_content_hash="hash",
                workspace_content_hash="hash",
                zulip_content_updated_at=datetime.datetime.now(datetime.timezone.utc),
                workspace_content_updated_at=datetime.datetime.now(
                    datetime.timezone.utc
                ),
            )
            content.insert()
            topic = zulip_models.ZulipTopic(
                uuid=uuid.uuid5(stream_uuid, "General"),
                stream_id=42,
                name="General",
                max_id=77,
            )
            topic.insert()
            placement_uuid = uuid.uuid5(topic.uuid, str(message_uuid))
            connection.execute(
                "INSERT INTO messenger_topics(uuid,project_id,stream_uuid,name,source_name,source) VALUES (%s,%s,%s,'General','zulip','{\"kind\":\"zulip\"}')",
                (topic.uuid, project, stream_uuid),
            )
            connection.execute(
                "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES (%s,%s,%s,%s,%s)",
                (placement_uuid, project, message_uuid, stream_uuid, topic.uuid),
            )
            observed = zulip_models.ZulipMessage(
                project_id=project,
                external_account_uuid=account_uuid,
                message_uuid=uuid.uuid5(namespace, "77"),
                message_id=77,
                sender_id=7,
                timestamp=1,
                channel=channel,
                topic=topic,
                content=content,
                read=False,
                starred=False,
                mentioned=False,
            )
            observed.insert()
            zulip_models.Task(
                uuid=task_uuid,
                project_id=project,
                external_account_uuid=account_uuid,
                content=zulip_models.ImportHistoryContent(
                    first_message_id=77,
                    last_message_id=77,
                    account_revision=old_revision,
                ),
            ).insert()
            payload = {
                "source_kind": "stream.deleted",
                "stream_uuid": str(stream_uuid),
                "source_name": "zulip",
                "source": {"kind": "zulip"},
                "old_message_uuid": str(placement_uuid),
                "old_source_name": "zulip",
                "old_source": {"kind": "zulip"},
                "snapshot": {
                    "uuid": str(account_uuid),
                    "provider": "zulip",
                    "external_account_id": hostname + ":7",
                    "settings": settings,
                },
            }
            outbox_uuid = uuid.uuid4()
            connection.execute(
                "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload) VALUES (%s,%s,'delivery_snapshot_event','stream',%s,%s::jsonb)",
                (outbox_uuid, project, str(stream_uuid), json.dumps(payload)),
            )
            old_subscription = hashlib.sha256(
                json.dumps(
                    [
                        str(project),
                        str(owner),
                        "zulip",
                        hostname + ":7",
                        settings["server_url"].rstrip("/"),
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            old_source_revision = hashlib.sha256(
                json.dumps(
                    [old_subscription, 2, 1],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            connection.execute(
                "INSERT INTO bridge_zulip_v5_history_batches(external_account_uuid,project_id,hostname,first_id,covered_to,source_revision,hashes,record_count,observed_at,ready) VALUES (%s,%s,%s,1,77,%s,'{}',0,NOW(),TRUE)",
                (account_uuid, project, hostname, old_source_revision),
            )
            engine.apply_migration(MIGRATION)
            assert connection.execute(
                "SELECT provider_uuid FROM m_external_accounts_v2 WHERE uuid=%s",
                (legacy_account_uuid,),
            ).fetchone() == (None,)
            provider_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, hostname)
            assert connection.execute(
                "SELECT external_account_id,provider_uuid FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == ("7", provider_uuid)
            for table, identity in (
                ("messenger_streams", "42"),
                ("messenger_messages", "77"),
            ):
                assert connection.execute(
                    f"SELECT provider_uuid,provider_identity FROM {table} WHERE project_id=%s",
                    (project,),
                ).fetchone() == (provider_uuid, identity)
            migrated = connection.execute(
                "SELECT payload FROM messenger_domain_outbox_events WHERE uuid=%s",
                (outbox_uuid,),
            ).fetchone()[0]
            assert "source" not in migrated
            assert migrated["provider_uuid"] == str(provider_uuid)
            assert migrated["provider_identity"] == "42"
            assert "old_source" not in migrated
            assert migrated["old_provider_uuid"] == str(provider_uuid)
            assert migrated["old_provider_identity"] == "77"
            assert migrated["snapshot"]["provider_uuid"] == str(provider_uuid)
            assert migrated["snapshot"]["external_account_id"] == "7"
            restored = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(account_uuid)}
            )
            assert connection.execute(
                "SELECT source_revision FROM bridge_zulip_v5_history_batches WHERE external_account_uuid=%s",
                (account_uuid,),
            ).fetchone() == (history.revision(restored),)
            assert connection.execute(
                "SELECT content->>'account_revision' FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                (task_uuid,),
            ).fetchone() == (restored.realtime_revision(),)
            connection.execute(
                "UPDATE bridge_zulip_v5_message_flags SET value=TRUE WHERE external_account_uuid=%s AND message_id=77 AND flag='read'",
                (account_uuid,),
            )
            engine.rollback_migration(MIGRATION)
            assert connection.execute(
                "SELECT value FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s AND message_id=77 AND flag='read'",
                (account_uuid,),
            ).fetchone() == (True,)
            assert connection.execute(
                "SELECT external_account_id FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (hostname + ":7",)
            assert connection.execute(
                "SELECT source FROM messenger_messages WHERE uuid=%s", (message_uuid,)
            ).fetchone() == ({"kind": "zulip"},)
            assert connection.execute(
                "SELECT content->>'account_revision' FROM bridge_zulip_v5_tasks WHERE uuid=%s",
                (task_uuid,),
            ).fetchone() == (old_revision,)
            engine.apply_migration(MIGRATION)
            assert connection.execute(
                "SELECT provider_identity FROM messenger_messages WHERE uuid=%s",
                (message_uuid,),
            ).fetchone() == ("77",)


def test_native_creation_rejects_provider_provenance(api, message_sources):
    account, _ = message_sources()
    response = api.post(
        "/v1/messenger/streams/",
        json={
            "name": "Forged origin",
            "provider_uuid": str(account.provider_uuid),
            "provider_identity": "42",
        },
    )
    assert response.status_code == 400, response.text
    response = api.post(
        "/v1/messenger/streams/",
        json={"name": "Removed source object", "source": {"kind": "native"}},
    )
    assert response.status_code == 400, response.text


def test_deleting_a_provider_cascades_its_objects_without_deleting_users(
    message_sources, db
):
    account, _ = message_sources(host=uuid.uuid4().hex + ".example.com")
    message_tests._prepare(account)
    with contexts.Context().session_manager():
        zulip_models.ZulipMessageState.reconcile_batch(limit=100)
    provider = external_models.Provider.objects.get_one(
        filters={"uuid": filters.EQ(account.provider_uuid)}
    )
    provider.delete()
    for table in (
        "m_external_accounts_v2",
        "messenger_streams",
        "messenger_topics",
        "messenger_messages",
        "messenger_user_message_states",
    ):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE project_id=%s", (account.project_id,)
        ).fetchone() == (0,), table
    assert db.execute(
        "SELECT count(*) FROM m_workspace_users WHERE uuid=%s",
        (account.owner_user_uuid,),
    ).fetchone() == (1,)


def test_provider_link_controls_endpoint_host_and_keeps_transport_options(
    message_sources,
):
    owner, _ = message_sources()
    provider = external_models.Provider.register("transport.example.com")
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner.owner_user_uuid,
        provider="zulip",
        provider_uuid=provider.uuid,
        settings={"server_url": "http://stale.example.com:8443/base/"},
    )
    account.insert()
    try:
        assert (
            account.settings["server_url"] == "http://transport.example.com:8443/base/"
        )
        account.provider_uuid = owner.provider_uuid
        account.update()
        assert account.settings["server_url"] == "http://zulip.example.com:8443/base/"
    finally:
        account.delete()
