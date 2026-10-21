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


def _register_project_user_sql(skip_existing):
    project_user_filter = ""
    folder_filter = ""
    binding_filter = ""
    project_user_conflict = "DO UPDATE SET updated_at = NOW()"
    if skip_existing:
        project_user_conflict = "DO NOTHING"
        project_user_filter = """
      AND NOT EXISTS (
          SELECT 1 FROM messenger_project_users
          WHERE project_id = target_project_id AND user_uuid = target_user_uuid
      )"""
        folder_filter = """
      AND NOT EXISTS (
          SELECT 1 FROM messenger_folders
          WHERE project_id = target_project_id AND uuid = template.uuid
      )"""
        binding_filter = """
      AND NOT EXISTS (
          SELECT 1 FROM messenger_user_folder_bindings
          WHERE project_id = target_project_id AND user_uuid = target_user_uuid
            AND folder_uuid = template.folder_uuid
      )"""
    return f"""
CREATE OR REPLACE FUNCTION messenger_v2_register_project_user(
    target_project_id uuid, target_user_uuid uuid
)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO messenger_project_users (project_id, user_uuid)
    SELECT target_project_id, target_user_uuid
    WHERE target_user_uuid IS NOT NULL
    {project_user_filter}
    ON CONFLICT (project_id, user_uuid) {project_user_conflict};

    INSERT INTO messenger_folders (
        uuid, project_id, title, background_color_value, system_type,
        created_at, updated_at
    )
    SELECT template.uuid, target_project_id, template.title, 11184810, 'all',
           template.created_at, template.created_at
    FROM (
        VALUES
            ('00000000-0000-0000-0000-000000000000'::uuid,
             'All chats'::varchar, '2000-01-01 00:00:00'::timestamp),
            ('00000000-0000-0000-0000-000000000001'::uuid,
             'Personal'::varchar, '2000-01-01 00:00:01'::timestamp),
            ('00000000-0000-0000-0000-000000000002'::uuid,
             'Channels'::varchar, '2000-01-01 00:00:02'::timestamp)
    ) AS template(uuid, title, created_at)
    WHERE target_user_uuid IS NOT NULL
    {folder_filter}
    ON CONFLICT (project_id, uuid) DO NOTHING;

    INSERT INTO messenger_user_folder_bindings (
        uuid, project_id, user_uuid, folder_uuid, rule,
        created_at, updated_at, snapshot_updated_at
    )
    SELECT messenger_uuid_v5(template.folder_uuid, target_user_uuid::text),
           target_project_id, target_user_uuid, template.folder_uuid,
           template.rule, template.created_at, template.created_at,
           template.created_at AT TIME ZONE current_setting('TIMEZONE')
    FROM (
        VALUES
            ('00000000-0000-0000-0000-000000000000'::uuid,
             'all_chats'::varchar, '2000-01-01 00:00:00'::timestamp),
            ('00000000-0000-0000-0000-000000000001'::uuid,
             'personal'::varchar, '2000-01-01 00:00:01'::timestamp),
            ('00000000-0000-0000-0000-000000000002'::uuid,
             'channels'::varchar, '2000-01-01 00:00:02'::timestamp)
    ) AS template(folder_uuid, rule, created_at)
    WHERE target_user_uuid IS NOT NULL
    {binding_filter}
    ON CONFLICT (project_id, user_uuid, folder_uuid) DO NOTHING;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0229-Maintain-imported-message-counters-from-per-message-deltas-5d0ad5.py"
        ]

    @property
    def migration_id(self):
        return "fc01aa44-2e9e-4f36-bcc5-56f0b0281cf1"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(_register_project_user_sql(skip_existing=True))
        session.execute("""
            UPDATE bridge_zulip_v5_tasks
            SET content = jsonb_set(content, '{batch_size}', '16'::jsonb),
                updated_at = NOW()
            WHERE content->>'kind' = 'reconciliation'
              AND status IN ('new', 'waiting', 'retry')
              AND COALESCE((content->>'batch_size')::integer, 1000) = 1000
        """)

    def downgrade(self, session):
        session.execute(_register_project_user_sql(skip_existing=False))


migration_step = MigrationStep()
