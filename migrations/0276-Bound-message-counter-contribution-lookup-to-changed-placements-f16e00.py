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


PREVIOUS_DESIRED_SQL = """
        SELECT actual.* FROM changes
        JOIN messenger_current_message_counter_contributions AS actual
          ON actual.project_id = changes.project_id AND actual.user_uuid = changes.user_uuid
         AND actual.placement_uuid = changes.placement_uuid
""".strip("\n")

BOUNDED_DESIRED_SQL = """
        SELECT actual.* FROM changes
        CROSS JOIN LATERAL (
            SELECT actual.* FROM messenger_current_message_counter_contributions AS actual
            WHERE actual.project_id = changes.project_id
              AND actual.user_uuid = changes.user_uuid
              AND actual.placement_uuid = changes.placement_uuid
            -- Keep each change key parameterized when expanding the contribution view.
            OFFSET 0
        ) AS actual
""".strip("\n")

FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION messenger_apply_message_counter_changes(
    p_project uuid, p_user uuid, p_stream uuid, p_limit integer
) RETURNS TABLE(processed integer, pending boolean) LANGUAGE plpgsql AS $$
DECLARE
    processed_count integer;
BEGIN
    WITH changes AS MATERIALIZED (
        SELECT change.*
        FROM messenger_message_counter_changes AS change
        WHERE change.project_id = p_project AND change.user_uuid = p_user
          AND change.stream_uuid = p_stream
        LIMIT p_limit
    ), desired AS MATERIALIZED (
{desired}
    ), raw_deltas AS (
        SELECT previous.project_id, previous.user_uuid, previous.stream_uuid, previous.topic_uuid,
               previous.membership_generation, previous.membership_started_at,
               -1::bigint AS unread_count, -previous.mentioned::integer AS mentioned_count
        FROM messenger_message_counter_contributions AS previous
        JOIN changes ON changes.project_id = previous.project_id AND changes.user_uuid = previous.user_uuid
                    AND changes.placement_uuid = previous.placement_uuid
        UNION ALL
        SELECT project_id, user_uuid, stream_uuid, topic_uuid, membership_generation, membership_started_at,
               1::bigint, mentioned::integer
        FROM desired
    ), deltas AS MATERIALIZED (
        SELECT project_id, user_uuid, stream_uuid, topic_uuid, membership_generation, membership_started_at,
               sum(unread_count)::bigint AS unread_count, sum(mentioned_count)::bigint AS mentioned_count
        FROM raw_deltas GROUP BY project_id, user_uuid, stream_uuid, topic_uuid,
                                 membership_generation, membership_started_at
        HAVING sum(unread_count) <> 0 OR sum(mentioned_count) <> 0
    ), updated AS (
        UPDATE messenger_message_counter_totals AS total
        SET unread_count = total.unread_count + delta.unread_count,
            mentioned_count = total.mentioned_count + delta.mentioned_count
        FROM deltas AS delta
        WHERE (total.project_id, total.user_uuid, total.stream_uuid, total.topic_uuid,
               total.membership_generation, total.membership_started_at)
            = (delta.project_id, delta.user_uuid, delta.stream_uuid, delta.topic_uuid,
               delta.membership_generation, delta.membership_started_at)
        RETURNING total.project_id, total.user_uuid, total.stream_uuid, total.topic_uuid,
                  total.membership_generation, total.membership_started_at
    ), inserted AS (
        INSERT INTO messenger_message_counter_totals
        SELECT delta.* FROM deltas AS delta
        WHERE NOT EXISTS (
            SELECT 1 FROM updated WHERE (updated.project_id, updated.user_uuid, updated.stream_uuid,
                updated.topic_uuid, updated.membership_generation, updated.membership_started_at)
              = (delta.project_id, delta.user_uuid, delta.stream_uuid, delta.topic_uuid,
                 delta.membership_generation, delta.membership_started_at)
        ) RETURNING 1
    ), recorded AS (
        INSERT INTO messenger_message_counter_contributions
        SELECT * FROM desired
        ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
            stream_uuid = EXCLUDED.stream_uuid, topic_uuid = EXCLUDED.topic_uuid,
            membership_generation = EXCLUDED.membership_generation,
            membership_started_at = EXCLUDED.membership_started_at, mentioned = EXCLUDED.mentioned
        RETURNING 1
    ), removed AS (
        DELETE FROM messenger_message_counter_contributions AS previous USING changes
        WHERE previous.project_id = changes.project_id AND previous.user_uuid = changes.user_uuid
          AND previous.placement_uuid = changes.placement_uuid
          AND NOT EXISTS (SELECT 1 FROM desired WHERE desired.project_id = previous.project_id
              AND desired.user_uuid = previous.user_uuid AND desired.placement_uuid = previous.placement_uuid)
        RETURNING 1
    ), acknowledged AS (
        DELETE FROM messenger_message_counter_changes AS change USING changes
        WHERE change.project_id = changes.project_id AND change.user_uuid = changes.user_uuid
          AND change.placement_uuid = changes.placement_uuid AND change.stream_uuid = changes.stream_uuid
          AND change.revision = changes.revision
        RETURNING 1
    )
    SELECT count(*)::integer INTO processed_count FROM changes;
    RETURN QUERY SELECT processed_count, EXISTS (
        SELECT 1 FROM messenger_message_counter_changes AS change
        WHERE change.project_id = p_project AND change.user_uuid = p_user
          AND change.stream_uuid = p_stream
    );
END;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0275-Qualify-immutable-content-digest-functions-for-backup-restore-67692f.py"
        ]

    @property
    def migration_id(self):
        return "f16e0097-023b-4941-90e8-91f7a7092ef5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(FUNCTION_SQL.format(desired=BOUNDED_DESIRED_SQL))

    def downgrade(self, session):
        session.execute(FUNCTION_SQL.format(desired=PREVIOUS_DESIRED_SQL))


migration_step = MigrationStep()
