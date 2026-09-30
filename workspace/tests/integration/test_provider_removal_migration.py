# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Removing integration state preserves canonical content and ownership."""

import uuid as sys_uuid
import json

import psycopg
import urllib.parse
from psycopg import sql
from restalchemy.storage.sql import engines
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


def test_cleanup_preserves_imported_content_and_generic_accounts():
    database_name = "workspace_provider_removal_" + sys_uuid.uuid4().hex
    parsed = urllib.parse.urlsplit(conftest.TEST_DB_URL)
    test_url = parsed._replace(path="/" + database_name).geturl()
    with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        engines.engine_factory.configure_factory(db_url=test_url)
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.apply_migration(
            "0185-Repair-per-user-operation-delivery-projections-e1f5ca.py"
        )
        project_uuid, user_uuid, topic_uuid, message_uuid, file_uuid, account_uuid = (
            sys_uuid.uuid4() for _ in range(6)
        )
        with psycopg.connect(test_url, autocommit=True) as database:
            stream_uuid = conftest.seed_user_stream(
                database, project_uuid, user_uuid, "Imported chat"
            )
            imported_user_uuid = sys_uuid.uuid4()
            conftest.seed_workspace_user(database, imported_user_uuid, "Imported user")
            database.execute(
                "UPDATE m_workspace_users SET source='zulip',email='same@example.test' WHERE uuid=%s",
                (imported_user_uuid,),
            )
            database.execute(
                "UPDATE m_workspace_users SET email='same@example.test' WHERE uuid=%s",
                (user_uuid,),
            )
            database.execute(
                "INSERT INTO m_workspace_stream_topics (uuid,project_id,name,stream_uuid) VALUES (%s,%s,'Imported topic',%s)",
                (topic_uuid, project_uuid, stream_uuid),
            )
            database.execute(
                """INSERT INTO m_workspace_messages
                (uuid,project_id,stream_uuid,user_uuid,topic_uuid,payload,source_name,source)
                VALUES (%s,%s,%s,%s,%s,'{"kind":"markdown","content":"Keep this message"}',
                        'zulip','{"kind":"zulip","stream_id":20,"message_id":42}')""",
                (message_uuid, project_uuid, stream_uuid, user_uuid, topic_uuid),
            )
            database.execute(
                """INSERT INTO m_workspace_files
                (uuid,project_id,name,user_uuid,stream_uuid,content_type,size_bytes,hash,storage_object_id)
                VALUES (%s,%s,'attachment.txt',%s,%s,'text/plain',4,'digest','shared/attachment')""",
                (file_uuid, project_uuid, user_uuid, stream_uuid),
            )
            database.execute(
                """INSERT INTO m_external_accounts_v2 (uuid,owner_user_uuid,provider,settings)
                VALUES (%s,%s,'zulip','{"label":"Keep account","selection_mode":"explicit"}')""",
                (account_uuid, user_uuid),
            )
            account_event_uuid = sys_uuid.uuid4()
            database.execute(
                """INSERT INTO m_workspace_events
                (uuid,project_id,user_uuid,schema_version,object_type,action,payload)
                VALUES (%s,%s,%s,1,'external_account','created',%s::jsonb)""",
                (
                    account_event_uuid,
                    project_uuid,
                    user_uuid,
                    json.dumps(
                        {
                            "kind": "external_account.created",
                            "uuid": str(account_uuid),
                            "snapshot": {
                                "uuid": str(account_uuid),
                                "provider": "zulip",
                                "status": "ready",
                                "capabilities": {},
                                "settings": {
                                    "label": "Keep account",
                                    "selection_mode": "explicit",
                                },
                            },
                        }
                    ),
                ),
            )
            database.execute(
                "INSERT INTO m_workspace_event_cursors (project_id,user_uuid) VALUES (%s,%s)",
                (project_uuid, user_uuid),
            )
            cursor_generation = database.execute(
                "SELECT epoch_generation FROM m_workspace_event_cursors WHERE project_id=%s AND user_uuid=%s",
                (project_uuid, user_uuid),
            ).fetchone()[0]
            before = database.execute(
                "SELECT uuid,project_id,user_uuid,topic_uuid,payload,ingest_sequence FROM m_workspace_messages WHERE uuid=%s",
                (message_uuid,),
            ).fetchone()
        engine.apply_migration("0186-remove-external-provider-processing-6e254a.py")
        with psycopg.connect(test_url) as database:
            assert database.execute(
                "SELECT source,email FROM m_workspace_users WHERE uuid=%s",
                (imported_user_uuid,),
            ).fetchone() == ("native", "same@example.test")
            assert database.execute(
                "SELECT source,email FROM m_workspace_users WHERE uuid=%s",
                (user_uuid,),
            ).fetchone() == ("iam", "same@example.test")
            assert database.execute(
                "SELECT payload->'snapshot' FROM m_workspace_events WHERE uuid=%s",
                (account_event_uuid,),
            ).fetchone()[0] == {
                "uuid": str(account_uuid),
                "provider": "zulip",
                "settings": {"label": "Keep account"},
            }
            assert (
                database.execute(
                    "SELECT epoch_generation FROM m_workspace_event_cursors WHERE project_id=%s AND user_uuid=%s",
                    (project_uuid, user_uuid),
                ).fetchone()[0]
                != cursor_generation
            )
            assert (
                database.execute(
                    "SELECT uuid,project_id,user_uuid,topic_uuid,payload,ingest_sequence FROM m_workspace_messages WHERE uuid=%s",
                    (message_uuid,),
                ).fetchone()
                == before
            )
            assert database.execute(
                "SELECT source_name,source FROM m_workspace_messages WHERE uuid=%s",
                (message_uuid,),
            ).fetchone() == ("native", {"kind": "native"})
            assert database.execute(
                "SELECT hash,storage_object_id FROM m_workspace_files WHERE uuid=%s",
                (file_uuid,),
            ).fetchone() == ("digest", "shared/attachment")
            assert database.execute(
                "SELECT owner_user_uuid,provider,settings FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (user_uuid, "zulip", {"label": "Keep account"})
            assert (
                database.execute(
                    "SELECT count(*) FROM m_workspace_user_messages_view WHERE uuid=%s AND user_uuid=%s",
                    (message_uuid, user_uuid),
                ).fetchone()[0]
                == 1
            )
            assert (
                database.execute(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND column_name IN ('provider_metadata','external_account_uuid','delivery_metadata')",
                ).fetchone()[0]
                == 0
            )
    finally:
        engines.engine_factory.destroy_all_engines()
        engines.engine_factory.configure_factory(db_url=conftest.TEST_DB_URL)
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )
