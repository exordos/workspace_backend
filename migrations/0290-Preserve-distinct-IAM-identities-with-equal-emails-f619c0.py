# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Email is mutable IAM profile metadata, not a Workspace identity key."""

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0289-Preserve-distinct-IAM-identities-with-equal-usernames-c85ed9.py"
        ]

    @property
    def migration_id(self):
        return "f619c096-a2b9-44c6-9d16-f6b2e22a20b1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("DROP INDEX IF EXISTS m_workspace_users_email_unique_idx")

    def downgrade(self, session):
        # Refuse duplicate IAM emails rather than rewriting historical identities.
        session.execute(
            """
            CREATE UNIQUE INDEX m_workspace_users_email_unique_idx
                ON m_workspace_users (email)
                WHERE email IS NOT NULL AND source = 'iam'
            """
        )


migration_step = MigrationStep()
