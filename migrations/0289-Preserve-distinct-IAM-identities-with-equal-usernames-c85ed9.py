# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""IAM UUIDs identify users; names may repeat across historical identities."""

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0288-Add-Telegram-provider-accounts-and-durable-synchronization-b50483.py"
        ]

    @property
    def migration_id(self):
        return "c85ed9fb-77da-40c7-9309-b41cefefcf05"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE m_workspace_users DROP CONSTRAINT IF EXISTS m_workspace_users_username_key"
        )

    def downgrade(self, session):
        # PostgreSQL refuses this safely if legitimate duplicate names exist.
        session.execute(
            "ALTER TABLE m_workspace_users ADD CONSTRAINT m_workspace_users_username_key UNIQUE(username)"
        )


migration_step = MigrationStep()
