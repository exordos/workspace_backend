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


LEGACY_TRACKING_SQL = """
CREATE OR REPLACE FUNCTION messenger_mark_counter_scope(p_project uuid, p_user uuid, p_stream uuid, p_topic uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_message_counter_changes
        (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
    SELECT p_project, p_user, scope.kind, scope.uuid, p_stream, scope.topic_uuid
    FROM (VALUES ('user-stream', p_stream, NULL::uuid),
                 ('user-topic', p_topic, p_topic)) AS scope(kind, uuid, topic_uuid)
    WHERE scope.uuid IS NOT NULL
    ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
$$;

CREATE OR REPLACE FUNCTION messenger_mark_counter_message(
    p_project uuid, p_user uuid, p_placement uuid, p_stream uuid, p_topic uuid
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM messenger_stream_bindings AS membership
        JOIN messenger_streams AS stream
          ON stream.project_id = membership.project_id AND stream.uuid = membership.stream_uuid
        WHERE membership.project_id = p_project AND membership.user_uuid = p_user
          AND membership.stream_uuid = p_stream
          AND stream.source_name = 'zulip'
    ) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM messenger_topics WHERE project_id = p_project AND uuid = p_topic) THEN
        PERFORM messenger_mark_counter_scope(p_project, p_user, p_stream, NULL);
        RETURN;
    END IF;
    INSERT INTO messenger_message_counter_changes
        (project_id, user_uuid, placement_uuid, stream_uuid, topic_uuid)
    VALUES (p_project, p_user, p_placement, p_stream, p_topic)
    ON CONFLICT (project_id, user_uuid, stream_uuid, placement_uuid)
    DO UPDATE SET topic_uuid = EXCLUDED.topic_uuid,
                  revision = EXCLUDED.revision;
    PERFORM messenger_mark_counter_scope(p_project, p_user, p_stream, p_topic);
END;
$$;

            CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_local_flags() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT NEW.project_id, NEW.uuid
                WHERE EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                    JOIN messenger_streams AS stream
                      ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                    WHERE placement.project_id=NEW.project_id AND placement.uuid=NEW.placement_uuid
                      AND stream.source_name='zulip')
                ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
"""
OWNED_TRACKING_SQL = """
CREATE OR REPLACE FUNCTION messenger_mark_counter_scope(p_project uuid, p_user uuid, p_stream uuid, p_topic uuid)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO bridge_zulip_v5_message_counter_changes
        (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
    SELECT p_project, p_user, scope.kind, scope.uuid, p_stream, scope.topic_uuid
    FROM (VALUES ('user-stream', p_stream, NULL::uuid),
                 ('user-topic', p_topic, p_topic)) AS scope(kind, uuid, topic_uuid)
    WHERE scope.uuid IS NOT NULL
      AND current_setting('workspace.owned_mutations', true) IS DISTINCT FROM 'on'
    ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid)
    DO UPDATE SET revision = EXCLUDED.revision;
$$;

CREATE OR REPLACE FUNCTION messenger_mark_counter_message(
    p_project uuid, p_user uuid, p_placement uuid, p_stream uuid, p_topic uuid
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('workspace.owned_mutations', true) = 'on' THEN RETURN; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM messenger_stream_bindings AS membership
        JOIN messenger_streams AS stream
          ON stream.project_id = membership.project_id AND stream.uuid = membership.stream_uuid
        WHERE membership.project_id = p_project AND membership.user_uuid = p_user
          AND membership.stream_uuid = p_stream
          AND stream.source_name = 'zulip'
    ) THEN
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM messenger_topics WHERE project_id = p_project AND uuid = p_topic) THEN
        PERFORM messenger_mark_counter_scope(p_project, p_user, p_stream, NULL);
        RETURN;
    END IF;
    INSERT INTO messenger_message_counter_changes
        (project_id, user_uuid, placement_uuid, stream_uuid, topic_uuid)
    VALUES (p_project, p_user, p_placement, p_stream, p_topic)
    ON CONFLICT (project_id, user_uuid, stream_uuid, placement_uuid)
    DO UPDATE SET topic_uuid = EXCLUDED.topic_uuid,
                  revision = EXCLUDED.revision;
    PERFORM messenger_mark_counter_scope(p_project, p_user, p_stream, p_topic);
END;
$$;

            CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_local_flags() RETURNS trigger AS $$
            BEGIN
                IF current_setting('workspace.batch_flags', true) = 'on' THEN RETURN NULL; END IF;
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT NEW.project_id, NEW.uuid
                WHERE EXISTS (SELECT 1 FROM messenger_message_placements AS placement
                    JOIN messenger_streams AS stream
                      ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
                    WHERE placement.project_id=NEW.project_id AND placement.uuid=NEW.placement_uuid
                      AND stream.source_name='zulip')
                ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0253-Keep-history-checkpoints-during-reaction-fact-projection-68942c.py"
        ]

    @property
    def migration_id(self):
        return "6d29df2d-5b5a-4585-992f-b3c8286089b7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(OWNED_TRACKING_SQL)
        session.execute("""
            CREATE TABLE messenger_mutation_projection_state (
                singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                initialized BOOLEAN NOT NULL DEFAULT FALSE
            );
            INSERT INTO messenger_mutation_projection_state DEFAULT VALUES;
            CREATE TABLE messenger_mutation_commands (
                sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE,
                request_uuid UUID NOT NULL,
                project_id UUID NOT NULL,
                user_uuid UUID NOT NULL,
                scope_key TEXT NOT NULL,
                operation TEXT NOT NULL,
                resource_uuid UUID NOT NULL,
                payload JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'applied', 'rejected')),
                result JSONB,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMP WITH TIME ZONE,
                FOREIGN KEY (project_id, user_uuid)
                    REFERENCES messenger_project_users(project_id, user_uuid) ON DELETE CASCADE
            );
            CREATE INDEX messenger_mutation_delete_tombstone_idx
                ON messenger_mutation_commands (scope_key, sequence DESC)
                WHERE operation='delete' AND status='applied';
            CREATE INDEX messenger_mutation_request_idx
                ON messenger_mutation_commands(request_uuid,project_id,user_uuid);
            CREATE VIEW messenger_mutation_requests AS
                SELECT request_uuid AS uuid,project_id,user_uuid,
                    CASE WHEN bool_or(status='pending') THEN 'pending'
                         WHEN bool_or(status='rejected') THEN 'rejected'
                         ELSE 'applied' END AS status,
                    count(*)::integer AS command_count,
                    count(*) FILTER (WHERE status='applied')::integer AS applied_count,
                    count(*) FILTER (WHERE status='rejected')::integer AS rejected_count,
                    min(created_at) AS created_at,
                    CASE WHEN bool_or(status='pending') THEN NULL ELSE max(finished_at) END AS finished_at
                FROM messenger_mutation_commands GROUP BY request_uuid,project_id,user_uuid;
            CREATE INDEX messenger_mutation_pending_scope_idx
                ON messenger_mutation_commands (scope_key, sequence)
                WHERE status = 'pending';
            CREATE INDEX messenger_mutation_pending_sequence_idx
                ON messenger_mutation_commands (sequence)
                WHERE status = 'pending';
            ALTER TABLE messenger_project_users
                ADD COLUMN unread_count BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN active_unread_count BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN passive_unread_count BIGINT NOT NULL DEFAULT 0;
            CREATE TABLE messenger_publication_outbox (
                sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                uuid UUID NOT NULL UNIQUE,
                project_id UUID NOT NULL,
                entity_uuid UUID NOT NULL,
                kind TEXT NOT NULL,
                recipients UUID[] NOT NULL,
                payload JSONB NOT NULL,
                recipient_payloads JSONB,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
            );
            CREATE INDEX messenger_publication_project_idx
                ON messenger_publication_outbox(project_id, sequence);
            CREATE FUNCTION messenger_notify_mutation_command() RETURNS trigger AS $$
            BEGIN
                PERFORM pg_notify('workspace_mutation_commands', '');
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER messenger_mutation_wakeup
                AFTER INSERT ON messenger_mutation_commands
                FOR EACH STATEMENT EXECUTE FUNCTION messenger_notify_mutation_command();
        """)

    def downgrade(self, session):
        session.execute(LEGACY_TRACKING_SQL)
        session.execute("""
            DROP VIEW messenger_mutation_requests;
            DROP TABLE messenger_mutation_commands;
            DROP TABLE messenger_mutation_projection_state;
            DROP TABLE messenger_publication_outbox;
            ALTER TABLE messenger_project_users DROP COLUMN unread_count,
                DROP COLUMN active_unread_count, DROP COLUMN passive_unread_count;
            DROP FUNCTION messenger_notify_mutation_command();
        """)


migration_step = MigrationStep()
