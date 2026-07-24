# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0113-remove-legacy-Messenger-mail-storage-eec69a.py"]

    @property
    def migration_id(self):
        return "1dbd2c19-1e0c-4d6c-8928-ee64ca5e2382"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            DROP INDEX IF EXISTS "m_workspace_users_email_unique_idx";

            CREATE UNIQUE INDEX "m_workspace_users_email_unique_idx"
                ON "m_workspace_users" ("email")
                WHERE "email" IS NOT NULL AND "source" = 'iam';
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_users"
            SET "email" = NULL
            WHERE "source" != 'iam';

            DROP INDEX IF EXISTS "m_workspace_users_email_unique_idx";

            CREATE UNIQUE INDEX "m_workspace_users_email_unique_idx"
                ON "m_workspace_users" ("email")
                WHERE "email" IS NOT NULL;
            """
        )


migration_step = MigrationStep()
