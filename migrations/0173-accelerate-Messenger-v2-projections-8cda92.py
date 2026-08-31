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


def _broadcast_guard_sql(*, skip_existing_project_users):
    project_user_filter = ""
    project_user_conflict = "DO UPDATE SET updated_at = NOW()"
    if skip_existing_project_users:
        project_user_filter = """
      AND NOT EXISTS (
          SELECT 1 FROM messenger_project_users AS project_user
          WHERE project_user.project_id = NEW.project_id
            AND project_user.user_uuid = audience.user_uuid
      )"""
        project_user_conflict = "DO NOTHING"
    return f"""
        CREATE OR REPLACE FUNCTION messenger_v2_guard_rolling_broadcast_event()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        DECLARE
            target_stream_uuid uuid;
        BEGIN
            target_stream_uuid := CASE
                WHEN NEW.payload->>'stream_uuid' IS NOT NULL
                THEN (NEW.payload->>'stream_uuid')::uuid
                WHEN NEW.object_type = 'stream'
                     AND NEW.payload->>'uuid' IS NOT NULL
                THEN (NEW.payload->>'uuid')::uuid
            END;
            IF target_stream_uuid IS NULL OR NOT EXISTS (
                SELECT 1 FROM messenger_streams
                WHERE project_id = NEW.project_id
                  AND uuid = target_stream_uuid
            ) THEN
                RETURN NEW;
            END IF;
            INSERT INTO messenger_project_users (project_id, user_uuid)
            SELECT NEW.project_id, audience.user_uuid
            FROM m_workspace_event_audience_members_v1 AS audience
            WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
            {project_user_filter}
            ON CONFLICT (project_id, user_uuid) {project_user_conflict};
            INSERT INTO messenger_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, active,
                membership_generation, membership_started_at, role,
                notification_mode, created_at, updated_at
            )
            SELECT messenger_uuid_v5(
                       target_stream_uuid,
                       'historical-membership:' || audience.user_uuid::text
                   ),
                   NEW.project_id, target_stream_uuid, audience.user_uuid,
                   stream.owner_uuid, false, 1, NEW.created_at,
                   'member', 'default',
                   NEW.created_at AT TIME ZONE current_setting('TIMEZONE'),
                   NEW.created_at AT TIME ZONE current_setting('TIMEZONE')
            FROM m_workspace_event_audience_members_v1 AS audience
            JOIN messenger_streams AS stream
              ON stream.project_id = NEW.project_id
             AND stream.uuid = target_stream_uuid
            WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
              AND NOT EXISTS (
                  SELECT 1 FROM messenger_stream_bindings AS binding
                  WHERE binding.project_id = NEW.project_id
                    AND binding.stream_uuid = target_stream_uuid
                    AND binding.user_uuid = audience.user_uuid
              );
            INSERT INTO messenger_event_membership_guards (
                event_uuid, project_id, user_uuid, stream_uuid,
                membership_generation, control_effect, created_at
            )
            SELECT NEW.uuid, NEW.project_id, audience.user_uuid,
                   target_stream_uuid, binding.membership_generation,
                   NEW.object_type = 'stream' AND NEW.action = 'deleted',
                   NEW.created_at
            FROM m_workspace_event_audience_members_v1 AS audience
            JOIN messenger_stream_bindings AS binding
              ON binding.project_id = NEW.project_id
             AND binding.stream_uuid = target_stream_uuid
             AND binding.user_uuid = audience.user_uuid
            WHERE audience.audience_snapshot_uuid = NEW.audience_snapshot_uuid
            ON CONFLICT (event_uuid, user_uuid) DO NOTHING;
            RETURN NEW;
        END;
        $function$
    """


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0172-retry-expired-provider-read-pages-05d036.py"]

    @property
    def migration_id(self):
        return "8cda92b7-3a84-47f5-939b-11222441b0ff"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Existing project users are already valid trigger recipients. Avoid
        # taking a write lock on each row while holding the project event lock:
        # an ingress transaction can hold that row and be waiting for the same
        # advisory lock, forming a deadlock that stalls domain outbox draining.
        session.execute(_broadcast_guard_sql(skip_existing_project_users=True))
        session.execute(
            """
            ALTER TABLE messenger_projection_tasks
                ADD COLUMN execution_stats JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
        session.execute(
            """
            CREATE FUNCTION messenger_projection_task_stats_sync_v1()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            BEGIN
                NEW.execution_stats := COALESCE(
                    NEW.payload->'_execution_stats', '{}'::jsonb
                );
                RETURN NEW;
            END
            $function$
            """
        )
        session.execute(
            """
            CREATE TRIGGER messenger_projection_task_stats_sync_v1
            BEFORE INSERT OR UPDATE OF payload
            ON messenger_projection_tasks
            FOR EACH ROW
            EXECUTE FUNCTION messenger_projection_task_stats_sync_v1()
            """
        )
        session.execute(
            """
            CREATE INDEX messenger_domain_outbox_events_kind_created_idx
                ON messenger_domain_outbox_events (
                    event_kind, created_at, uuid
                )
            """
        )
        session.execute(
            """
            CREATE INDEX messenger_projection_tasks_fair_claim_idx
                ON messenger_projection_tasks (
                    task_kind, created_at, ordering_created_at,
                    outbox_event_uuid
                )
                WHERE status NOT IN ('completed', 'dead_letter')
            """
        )
        session.execute(
            """
            CREATE INDEX messenger_projection_tasks_background_created_idx
                ON messenger_projection_tasks (
                    created_at, ordering_created_at, outbox_event_uuid
                )
                WHERE status NOT IN ('completed', 'dead_letter')
                  AND task_kind IN (
                    'content_mentions', 'folder_projection',
                    'delivery_snapshot_event', 'topic_state_projection',
                    'topic_membership_policy_rebuild'
                  )
            """
        )
        session.execute(
            """
            CREATE INDEX messenger_message_reaction_facts_snapshot_idx
                ON messenger_message_reaction_facts (
                    project_id, canonical_message_uuid, emoji_name,
                    created_at, uuid
                ) INCLUDE (user_uuid)
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DROP INDEX messenger_message_reaction_facts_snapshot_idx
            """
        )
        session.execute(
            """
            DROP INDEX messenger_projection_tasks_background_created_idx
            """
        )
        session.execute(
            """
            DROP INDEX messenger_projection_tasks_fair_claim_idx
            """
        )
        session.execute(
            """
            DROP INDEX messenger_domain_outbox_events_kind_created_idx
            """
        )
        session.execute(
            """
            DROP TRIGGER messenger_projection_task_stats_sync_v1
                ON messenger_projection_tasks
            """
        )
        session.execute(
            """
            DROP FUNCTION messenger_projection_task_stats_sync_v1()
            """
        )
        session.execute(
            """
            ALTER TABLE messenger_projection_tasks
                DROP COLUMN execution_stats
            """
        )
        session.execute(_broadcast_guard_sql(skip_existing_project_users=False))


migration_step = MigrationStep()
