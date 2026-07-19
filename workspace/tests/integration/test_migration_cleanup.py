# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import conftest


LEGACY_MIGRATION_UUIDS = (
    "e8e1b2c3-3739-4238-97cf-fa7613109917",
    "4b6e8031-28dd-4cb5-9bf6-37d75bb2da45",
    "c990ade7-933d-4cb9-bef5-3dbccecd2dff",
    "c73e3516-9240-4c3e-803a-97deedad2721",
    "bd462528-6582-49df-aa39-ef9108196127",
)
CLEANUP_MIGRATION_UUID = "eec69a95-cabb-49c5-89a1-8078732f27c2"
CLEANUP_MIGRATION_FILE = "0113-remove-legacy-Messenger-mail-storage-eec69a.py"
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


def test_mail_cleanup_is_the_single_migration_head(_database, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))

    assert engine.get_latest_migration() == CLEANUP_MIGRATION_FILE
    with db.cursor() as cur:
        cur.execute(
            'SELECT applied FROM "ra_migrations" WHERE uuid = %s',
            (CLEANUP_MIGRATION_UUID,),
        )
        assert cur.fetchone() == (True,)


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

    engine = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )
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
