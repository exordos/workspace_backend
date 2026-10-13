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


READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"

CANONICAL_USER_STREAM_VIEW = """
CREATE OR REPLACE VIEW messenger_api_user_streams_v1 AS
SELECT
    stream.uuid,
    binding.user_uuid,
    stream.name,
    stream.description,
    stream.project_id,
    stream.created_at,
    stream.updated_at,
    stream.owner_uuid AS owner,
    binding.role,
    binding.notification_mode,
    binding.unread_count,
    binding.active_unread_count,
    binding.passive_unread_count,
    stream.source_name,
    stream.source,
    stream.invite_only,
    stream.announce,
    stream.private,
    stream.is_archived,
    CASE
        WHEN stream.direct_user_uuid IS NULL THEN NULL
        WHEN binding.user_uuid = stream.owner_uuid THEN stream.direct_user_uuid
        ELSE stream.owner_uuid
    END AS direct_user_uuid,
    stream.private_index,
    stream.color,
    binding.last_message_uuid,
    stream.default_topic_uuid,
    stream.provider,
    stream.delivery,
    stream.deleted_at,
    stream.deleted_at IS NULL AS visible{encryption_fields}
FROM messenger_stream_bindings AS binding
JOIN messenger_streams AS stream
  ON stream.project_id = binding.project_id
 AND stream.uuid = binding.stream_uuid
WHERE binding.active;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0185-Repair-per-user-operation-delivery-projections-e1f5ca.py"
        ]

    @property
    def migration_id(self):
        return "17aa3c67-6be6-4da9-8602-905c1ecddf61"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(
            """
            ALTER TABLE messenger_streams
                ADD COLUMN encryption boolean NOT NULL DEFAULT false,
                ADD COLUMN current_encryption_key_uuid uuid;

            -- Keep existing dependent views attached to their original projection.
            ALTER VIEW m_workspace_user_streams
                RENAME TO m_workspace_user_streams_base_v1;
            CREATE VIEW m_workspace_user_streams AS
            SELECT legacy.*,
                   stream.encryption,
                   stream.current_encryption_key_uuid
            FROM m_workspace_user_streams_base_v1 AS legacy
            JOIN messenger_streams AS stream
              ON stream.project_id = legacy.project_id
             AND stream.uuid = legacy.uuid;
            """
        )
        session.execute(
            CANONICAL_USER_STREAM_VIEW.format(
                encryption_fields=(
                    ",\n    stream.encryption,\n    stream.current_encryption_key_uuid"
                ),
            )
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute(
            """
            DROP VIEW m_workspace_user_streams;
            ALTER VIEW m_workspace_user_streams_base_v1
                RENAME TO m_workspace_user_streams;
            DROP VIEW messenger_api_user_streams_v1;
            """
        )
        session.execute(CANONICAL_USER_STREAM_VIEW.format(encryption_fields=""))
        session.execute(
            """
            ALTER TABLE messenger_streams
                DROP COLUMN current_encryption_key_uuid,
                DROP COLUMN encryption;
            """
        )


migration_step = MigrationStep()
