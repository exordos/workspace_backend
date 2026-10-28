# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import psycopg
from restalchemy.storage.sql import migrations

from workspace.messenger_api.dm import models
from workspace.tests.integration import conftest


PREVIOUS = "0283-Repair-linked-Zulip-direct-participant-identities-6b50ae.py"
MIGRATION = "0284-Authorize-files-through-Workspace-membership-59b5ff.py"


def view_metadata(connection):
    return connection.execute("""
        SELECT relation.oid,relation.relname,relation.relowner,relation.reloptions,
               COALESCE(relation.relacl,acldefault('r',relation.relowner))::text,
               column_.attnum,column_.attname,column_.atttypid,column_.atttypmod
        FROM pg_class relation
        JOIN pg_attribute column_ ON column_.attrelid=relation.oid
        WHERE relation.oid IN ('m_workspace_visible_files_v1'::regclass,
                               'file_acl_reader'::regclass)
          AND column_.attnum>0 AND NOT column_.attisdropped
        ORDER BY relation.oid,column_.attnum
    """).fetchall()


def test_file_acl_migration_preserves_columns_grants_dependencies_and_stored_rows(
    _database,
):
    with conftest.isolated_migration_database(PREVIOUS) as url:
        with psycopg.connect(url, autocommit=True) as connection:
            user, project, provider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, user, "file-owner")
            connection.execute(
                "INSERT INTO messenger_providers(uuid,hostname) VALUES (%s,'zulip.example.com')",
                (provider,),
            )
            file = models.WorkspaceFile(
                project_id=project,
                user_uuid=user,
                name="imported.pdf",
                content_type="application/pdf",
                size_bytes=0,
                hash="test-digest",
                storage_object_id="stored-object",
                acl_mode="owner",
                source_name="zulip",
                provider_uuid=provider,
                provider_identity="/user_uploads/1/key/imported.pdf",
            )
            file.insert()
            connection.execute("""
                ALTER VIEW m_workspace_visible_files_v1 SET (security_barrier=true,security_invoker=true);
                GRANT SELECT ON m_workspace_visible_files_v1 TO PUBLIC;
                REVOKE INSERT ON m_workspace_visible_files_v1 FROM CURRENT_USER;
                CREATE VIEW file_acl_reader WITH (security_barrier=true,security_invoker=true)
                AS SELECT uuid,viewer_user_uuid,provider_identity FROM m_workspace_visible_files_v1;
                GRANT SELECT ON file_acl_reader TO PUBLIC;
                REVOKE INSERT ON file_acl_reader FROM CURRENT_USER;
            """)
            before = view_metadata(connection)
            data = connection.execute("SELECT * FROM m_workspace_files").fetchall()
            old_definition = connection.execute(
                "SELECT pg_get_viewdef('m_workspace_visible_files_v1'::regclass)"
            ).fetchone()
            reader_definition = connection.execute(
                "SELECT pg_get_viewdef('file_acl_reader'::regclass)"
            ).fetchone()
            assert not connection.execute("SELECT * FROM file_acl_reader").fetchall()
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            for _ in range(2):
                engine.apply_migration(MIGRATION)
                assert view_metadata(connection) == before
                assert (
                    connection.execute("SELECT * FROM m_workspace_files").fetchall()
                    == data
                )
                assert connection.execute(
                    "SELECT * FROM file_acl_reader"
                ).fetchall() == [(file.uuid, user, file.provider_identity)]
                assert (
                    connection.execute(
                        "SELECT pg_get_viewdef('file_acl_reader'::regclass)"
                    ).fetchone()
                    == reader_definition
                )
                engine.rollback_migration(MIGRATION)
                assert view_metadata(connection) == before
                assert (
                    connection.execute("SELECT * FROM m_workspace_files").fetchall()
                    == data
                )
                assert not connection.execute(
                    "SELECT * FROM file_acl_reader"
                ).fetchall()
                assert (
                    connection.execute(
                        "SELECT pg_get_viewdef('m_workspace_visible_files_v1'::regclass)"
                    ).fetchone()
                    == old_definition
                )
