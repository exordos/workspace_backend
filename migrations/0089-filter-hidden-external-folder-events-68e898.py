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


WORKSPACE_VISIBLE_EVENTS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_visible_events" AS
SELECT e.*
FROM "m_workspace_events" AS e
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = e.project_id
    AND access.user_uuid    = e.user_uuid
    AND access.account_type = e.payload->>'source_name'
    AND access.source_scope = COALESCE(
        e.payload->'source'->>'source_scope',
        e.payload->'source'->>'server_url'
    )
LEFT JOIN "m_confirmed_external_account_access" AS old_access
    ON  old_access.project_id   = e.project_id
    AND old_access.user_uuid    = e.user_uuid
    AND old_access.account_type = e.payload->>'old_source_name'
    AND old_access.source_scope = COALESCE(
        e.payload->'old_source'->>'source_scope',
        e.payload->'old_source'->>'server_url'
    )
WHERE (
        COALESCE(e.payload->>'source_name', 'native') = 'native'
        OR access.user_uuid IS NOT NULL
    )
  AND (
        e.payload->>'old_source_name' IS NULL
        OR e.payload->>'old_source_name' = 'native'
        OR old_access.user_uuid IS NOT NULL
    )
  AND (
        e.object_type != 'folder'
        OR NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                COALESCE(e.payload->'folder_items', '[]'::jsonb)
            ) AS folder_item(value)
            WHERE NOT EXISTS (
                SELECT 1
                FROM "m_workspace_streams" AS s
                JOIN "m_workspace_stream_bindings" AS b
                    ON  b.stream_uuid = s.uuid
                    AND b.project_id  = s.project_id
                    AND b.user_uuid   = e.user_uuid
                LEFT JOIN "m_confirmed_external_account_access" AS stream_access
                    ON  stream_access.project_id   = s.project_id
                    AND stream_access.user_uuid    = b.user_uuid
                    AND stream_access.account_type = s.source_name
                    AND stream_access.source_scope = COALESCE(
                        s.source->>'source_scope',
                        s.source->>'server_url'
                    )
                WHERE s.project_id = e.project_id
                  AND s.uuid::text = folder_item.value->>'stream_uuid'
                  AND (
                        s.source_name = 'native'
                        OR stream_access.user_uuid IS NOT NULL
                    )
            )
        )
    )
  AND (
        e.object_type != 'stream_binding'
        OR EXISTS (
            SELECT 1
            FROM "m_workspace_streams" AS s
            JOIN "m_workspace_stream_bindings" AS b
                ON  b.stream_uuid = s.uuid
                AND b.project_id  = s.project_id
                AND b.user_uuid   = e.user_uuid
            LEFT JOIN "m_confirmed_external_account_access" AS stream_access
                ON  stream_access.project_id   = s.project_id
                AND stream_access.user_uuid    = b.user_uuid
                AND stream_access.account_type = s.source_name
                AND stream_access.source_scope = COALESCE(
                    s.source->>'source_scope',
                    s.source->>'server_url'
                )
            WHERE s.project_id = e.project_id
              AND s.uuid::text = e.payload->>'uuid'
              AND (
                    s.source_name = 'native'
                    OR stream_access.user_uuid IS NOT NULL
                )
        )
    );
"""


PREVIOUS_WORKSPACE_VISIBLE_EVENTS_VIEW_SQL = """
CREATE OR REPLACE VIEW "m_workspace_visible_events" AS
SELECT e.*
FROM "m_workspace_events" AS e
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = e.project_id
    AND access.user_uuid    = e.user_uuid
    AND access.account_type = e.payload->>'source_name'
    AND access.source_scope = COALESCE(
        e.payload->'source'->>'source_scope',
        e.payload->'source'->>'server_url'
    )
LEFT JOIN "m_confirmed_external_account_access" AS old_access
    ON  old_access.project_id   = e.project_id
    AND old_access.user_uuid    = e.user_uuid
    AND old_access.account_type = e.payload->>'old_source_name'
    AND old_access.source_scope = COALESCE(
        e.payload->'old_source'->>'source_scope',
        e.payload->'old_source'->>'server_url'
    )
WHERE (
        COALESCE(e.payload->>'source_name', 'native') = 'native'
        OR access.user_uuid IS NOT NULL
    )
  AND (
        e.payload->>'old_source_name' IS NULL
        OR e.payload->>'old_source_name' = 'native'
        OR old_access.user_uuid IS NOT NULL
    );
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0088-add-external-account-access-gate-6ea388.py"]

    @property
    def migration_id(self):
        return "68e898fa-cc9b-4cd2-8cb0-34c839fa183a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(WORKSPACE_VISIBLE_EVENTS_VIEW_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_WORKSPACE_VISIBLE_EVENTS_VIEW_SQL)


migration_step = MigrationStep()
