# Copyright 2016 Eugene Frolov <eugene@frolov.net.ru>
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from restalchemy.storage.sql import migrations


USER_UUID_FOREIGN_KEYS = (
    ("m_external_accounts", "m_external_accounts_user_uuid_fkey"),
    ("m_folder_items", "m_folder_items_user_uuid_fkey"),
    ("m_folders", "m_folders_user_uuid_fkey"),
    ("m_workspace_events", "m_workspace_events_user_uuid_fkey"),
    ("m_workspace_file_accesses", "m_workspace_file_accesses_user_uuid_fkey"),
    ("m_workspace_files", "m_workspace_files_user_uuid_fkey"),
    (
        "m_workspace_message_reactions",
        "m_workspace_message_reactions_user_uuid_fkey",
    ),
    ("m_workspace_messages", "m_workspace_messages_user_uuid_fkey"),
    (
        "m_workspace_stream_bindings",
        "m_workspace_stream_bindings_user_uuid_fkey",
    ),
    ("m_workspace_streams", "m_workspace_streams_user_uuid_fkey"),
    (
        "m_workspace_user_message_flags",
        "m_workspace_user_message_flags_user_uuid_fkey",
    ),
    (
        "m_workspace_user_topic_flags",
        "m_workspace_user_topic_flags_user_uuid_fkey",
    ),
)


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0074-add-external-account-server-url-18e26a.py"]

    @property
    def migration_id(self):
        return "65d44b3e-5605-47dd-bd12-d172f6aa39bc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE IF EXISTS "m_external_accounts"
                DROP CONSTRAINT IF EXISTS "external_accounts_user_uuid_fkey";
            """
        )
        for table, constraint in USER_UUID_FOREIGN_KEYS:
            session.execute(
                f"""
                ALTER TABLE IF EXISTS "{table}"
                    DROP CONSTRAINT IF EXISTS "{constraint}";
                """
            )
            session.execute(
                f"""
                ALTER TABLE IF EXISTS "{table}"
                    ADD CONSTRAINT "{constraint}"
                    FOREIGN KEY ("user_uuid")
                    REFERENCES "m_workspace_users" ("uuid")
                    ON UPDATE CASCADE
                    ON DELETE CASCADE;
                """
            )

    def downgrade(self, session):
        for table, constraint in reversed(USER_UUID_FOREIGN_KEYS):
            session.execute(
                f"""
                ALTER TABLE IF EXISTS "{table}"
                    DROP CONSTRAINT IF EXISTS "{constraint}";
                """
            )
        session.execute(
            """
            ALTER TABLE IF EXISTS "m_external_accounts"
                ADD CONSTRAINT "m_external_accounts_user_uuid_fkey"
                FOREIGN KEY ("user_uuid")
                REFERENCES "m_workspace_users" ("uuid")
                ON DELETE CASCADE;
            """
        )


migration_step = MigrationStep()
