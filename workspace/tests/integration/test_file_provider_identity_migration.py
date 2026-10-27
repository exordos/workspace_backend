# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import uuid

import psycopg
from psycopg import rows
import pytest
from restalchemy.storage.sql import migrations

from workspace.messenger_api.dm import models
from workspace.tests.integration import conftest


PREVIOUS = "0281-Join-recipient-preview-and-Zulip-routing-migrations-5db781.py"
MIGRATION = "0282-Allow-full-provider-identities-for-imported-files-78fba4.py"


def views(connection):
    return connection.execute("""
        SELECT relation.relname,pg_get_viewdef(relation.oid),relation.reloptions,
               relation.relowner,
               COALESCE(relation.relacl,acldefault('r',relation.relowner))::text
        FROM pg_class relation JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname='public' AND relation.relkind='v'
        ORDER BY relation.relname
    """).fetchall()


def test_file_identity_migration_preserves_rows_views_permissions_and_full_paths(
    _database,
):
    with conftest.isolated_migration_database(PREVIOUS) as url:
        with psycopg.connect(url, autocommit=True) as connection:
            user, provider, project = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, user, "file-owner")
            connection.execute(
                "INSERT INTO messenger_providers(uuid,hostname) VALUES (%s,%s)",
                (provider, "zulip.example.com"),
            )
            file = models.WorkspaceFile(
                project_id=project,
                user_uuid=user,
                name="file.pdf",
                content_type="application/pdf",
                size_bytes=0,
                hash="test-digest",
                storage_object_id="file-object",
                acl_mode="owner",
                source_name="zulip",
                provider_uuid=provider,
                provider_identity="/user_uploads/1/key/file.pdf",
            )
            file.insert()
            connection.execute("""
                CREATE VIEW file_identity_reader WITH (security_barrier=true,security_invoker=true)
                AS SELECT uuid,provider_identity FROM m_workspace_visible_files_v1;
                GRANT SELECT ON file_identity_reader TO PUBLIC;
                REVOKE INSERT ON file_identity_reader FROM CURRENT_USER;
            """)
            original_views = views(connection)
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(MIGRATION)
            assert views(connection) == original_views
            long_path = "/user_uploads/1/key/" + "x" * 253
            file.provider_identity = long_path
            file.update()
            assert connection.execute(
                "SELECT uuid,provider_identity FROM m_workspace_files WHERE uuid=%s",
                (file.uuid,),
            ).fetchone() == (file.uuid, long_path)
            assert connection.execute(
                "SELECT data_type,character_maximum_length FROM information_schema.columns "
                "WHERE table_name='m_workspace_files' AND column_name='provider_identity'"
            ).fetchone() == ("text", None)
            assert connection.execute(
                "SELECT data_type,character_maximum_length FROM information_schema.columns "
                "WHERE table_name='zulip_entity_links' AND column_name='provider_identity'"
            ).fetchone() == ("text", None)
            assert connection.execute(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name='messenger_messages' AND column_name='provider_identity'"
            ).fetchone() == (255,)

            spec = importlib.util.spec_from_file_location(
                "file_identity_migration", conftest.MIGRATIONS_DIR / MIGRATION
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with psycopg.connect(
                url, autocommit=True, row_factory=rows.dict_row
            ) as downgrade:
                with pytest.raises(
                    ValueError, match="Cannot narrow file provider identities"
                ):
                    module.migration_step.downgrade(downgrade)
            assert views(connection) == original_views
            file.provider_identity = "/user_uploads/1/key/file.pdf"
            file.update()
            engine.rollback_migration(MIGRATION)
            assert views(connection) == original_views
            assert connection.execute(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name='m_workspace_files' AND column_name='provider_identity'"
            ).fetchone() == (255,)
            engine.apply_migration(MIGRATION)
            assert views(connection) == original_views
