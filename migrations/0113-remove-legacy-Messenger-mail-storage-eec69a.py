# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0112-deduplicate-Messenger-message-recipient-events-6f42ab.py"
        ]

    @property
    def migration_id(self):
        return "eec69a95-cabb-49c5-89a1-8078732f27c2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DO $migration$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_roles
                    WHERE rolname = 'workspace_mail_gate'
                ) THEN
                    EXECUTE format(
                        'REVOKE CONNECT ON DATABASE %I '
                        'FROM "workspace_mail_gate"',
                        current_database()
                    );
                END IF;
            END
            $migration$;

            CREATE OR REPLACE VIEW "m_workspace_visible_files_v1" AS
            SELECT files.*, bindings."user_uuid" AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            JOIN "m_workspace_stream_bindings" AS bindings
              ON bindings."project_id" = files."project_id"
             AND bindings."stream_uuid" = files."stream_uuid"
            WHERE files."acl_mode" = 'stream'
            UNION
            SELECT files.*, files."user_uuid" AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            WHERE files."acl_mode" = 'owner'
            UNION
            SELECT files.*, NULL::UUID AS "viewer_user_uuid"
            FROM "m_workspace_files" AS files
            WHERE files."acl_mode" = 'public';

            DROP TABLE IF EXISTS "m_messenger_writer_gate_acks_v1";
            DROP TABLE IF EXISTS "m_messenger_writer_gate_expected_v1";
            DROP TABLE IF EXISTS "m_messenger_writer_instances_v1";
            DROP TABLE IF EXISTS "m_messenger_writer_gate_releases_v1";
            DROP TABLE IF EXISTS "m_messenger_writer_gates_v1";

            DROP TABLE IF EXISTS "m_messenger_import_quarantine_v1";
            DROP TABLE IF EXISTS "m_messenger_import_checkpoints_v1";
            DROP TABLE IF EXISTS "m_messenger_import_items_v1";
            DROP TABLE IF EXISTS "m_messenger_import_runs_v1";

            DELETE FROM "ra_migrations"
            WHERE "uuid" IN (
                'e8e1b2c3-3739-4238-97cf-fa7613109917',
                '4b6e8031-28dd-4cb5-9bf6-37d75bb2da45',
                'c990ade7-933d-4cb9-bef5-3dbccecd2dff',
                'c73e3516-9240-4c3e-803a-97deedad2721',
                'bd462528-6582-49df-aa39-ef9108196127'
            );
            """
        )

    def downgrade(self, session):
        # Removed mail storage and migration-control data cannot be restored.
        pass


migration_step = MigrationStep()
