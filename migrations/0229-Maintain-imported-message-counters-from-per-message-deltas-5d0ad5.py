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


UPGRADE_SQL = """
CREATE TABLE messenger_message_counter_contributions (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    membership_generation integer NOT NULL,
    membership_started_at timestamptz NOT NULL,
    mentioned boolean NOT NULL,
    PRIMARY KEY (project_id, user_uuid, placement_uuid),
    FOREIGN KEY (project_id, user_uuid, stream_uuid)
        REFERENCES messenger_stream_bindings(project_id, user_uuid, stream_uuid) ON DELETE CASCADE,
    FOREIGN KEY (project_id, topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE
);
CREATE INDEX messenger_counter_contributions_scope_idx
    ON messenger_message_counter_contributions(project_id, user_uuid, stream_uuid, topic_uuid);

CREATE TABLE messenger_message_counter_totals (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    membership_generation integer NOT NULL,
    membership_started_at timestamptz NOT NULL,
    unread_count bigint NOT NULL CHECK (unread_count >= 0),
    mentioned_count bigint NOT NULL CHECK (mentioned_count BETWEEN 0 AND unread_count),
    PRIMARY KEY (project_id, user_uuid, stream_uuid, topic_uuid,
                 membership_generation, membership_started_at),
    FOREIGN KEY (project_id, user_uuid, stream_uuid)
        REFERENCES messenger_stream_bindings(project_id, user_uuid, stream_uuid) ON DELETE CASCADE,
    FOREIGN KEY (project_id, topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE
);

CREATE TABLE messenger_message_counter_changes (
    project_id uuid NOT NULL,
    user_uuid uuid NOT NULL,
    placement_uuid uuid NOT NULL,
    stream_uuid uuid NOT NULL,
    topic_uuid uuid NOT NULL,
    revision bigint NOT NULL DEFAULT nextval('messenger_message_change_revision_seq'),
    PRIMARY KEY (project_id, user_uuid, stream_uuid, placement_uuid),
    FOREIGN KEY (project_id, user_uuid, stream_uuid)
        REFERENCES messenger_stream_bindings(project_id, user_uuid, stream_uuid) ON DELETE CASCADE,
    FOREIGN KEY (project_id, topic_uuid) REFERENCES messenger_topics(project_id, uuid) ON DELETE CASCADE
);

CREATE VIEW messenger_current_message_counter_contributions AS
SELECT state.project_id, state.user_uuid, state.placement_uuid,
       placement.stream_uuid, placement.topic_uuid, membership.membership_generation,
       membership.membership_started_at, state.mentioned
FROM messenger_user_message_states AS state
JOIN messenger_message_placements AS placement
  ON placement.project_id = state.project_id AND placement.uuid = state.placement_uuid
JOIN messenger_messages AS message
  ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
 AND message.deleted_at IS NULL
JOIN messenger_topics AS topic
  ON topic.project_id = placement.project_id AND topic.uuid = placement.topic_uuid
 AND topic.deleted_at IS NULL
JOIN messenger_streams AS stream
  ON stream.project_id = placement.project_id AND stream.uuid = placement.stream_uuid
 AND stream.source_name = 'zulip'
JOIN messenger_stream_bindings AS membership
  ON membership.project_id = state.project_id AND membership.user_uuid = state.user_uuid
 AND membership.stream_uuid = placement.stream_uuid AND membership.active
 AND membership.membership_generation = state.membership_generation
JOIN messenger_user_message_bindings AS access
  ON access.project_id = state.project_id AND access.user_uuid = state.user_uuid
 AND access.placement_uuid = state.placement_uuid
 AND access.membership_generation = state.membership_generation AND access.visibility = 'visible'
WHERE state.read_at IS NULL;

CREATE VIEW messenger_topic_counter_totals AS
SELECT membership.project_id, membership.user_uuid, membership.stream_uuid, topic.uuid AS topic_uuid,
       COALESCE(totals.unread_count, 0) AS unread_count,
       CASE
           WHEN topic.deleted_at IS NOT NULL THEN 0
           WHEN personal.notification_mode = 'mute' THEN COALESCE(totals.mentioned_count, 0)
           WHEN personal.notification_mode IN ('follow', 'unmute') THEN COALESCE(totals.unread_count, 0)
           WHEN membership.notification_mode = 'all_messages' THEN COALESCE(totals.unread_count, 0)
           WHEN membership.notification_mode = 'mentions_only' THEN COALESCE(totals.mentioned_count, 0)
           ELSE 0
       END AS active_unread_count
FROM messenger_stream_bindings AS membership
JOIN messenger_streams AS stream
  ON stream.project_id = membership.project_id AND stream.uuid = membership.stream_uuid
 AND stream.source_name = 'zulip'
JOIN messenger_topics AS topic
  ON topic.project_id = membership.project_id AND topic.stream_uuid = membership.stream_uuid
LEFT JOIN messenger_message_counter_totals AS totals
  ON totals.project_id = membership.project_id AND totals.user_uuid = membership.user_uuid
 AND totals.stream_uuid = membership.stream_uuid AND totals.topic_uuid = topic.uuid
 AND totals.membership_generation = membership.membership_generation
 AND totals.membership_started_at = membership.membership_started_at
 AND topic.deleted_at IS NULL
LEFT JOIN messenger_user_topic_bindings AS personal
  ON personal.project_id = membership.project_id AND personal.user_uuid = membership.user_uuid
 AND personal.topic_uuid = topic.uuid
WHERE membership.active;

CREATE FUNCTION messenger_mark_counter_scope(p_project uuid, p_user uuid, p_stream uuid, p_topic uuid)
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

CREATE FUNCTION messenger_mark_counter_message(
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

CREATE FUNCTION messenger_track_viewer_counter_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target record;
    scope record;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF TG_TABLE_NAME = 'messenger_user_message_states' THEN
            IF (NEW.read_at, NEW.mentioned, NEW.membership_generation)
                IS NOT DISTINCT FROM (OLD.read_at, OLD.mentioned, OLD.membership_generation) THEN
                RETURN NULL;
            END IF;
        ELSIF (NEW.visibility, NEW.membership_generation)
                IS NOT DISTINCT FROM (OLD.visibility, OLD.membership_generation) THEN
            RETURN NULL;
        END IF;
    END IF;
    target := CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    IF TG_TABLE_NAME = 'messenger_user_message_bindings' AND TG_OP = 'INSERT'
       AND NOT EXISTS (SELECT 1 FROM messenger_user_message_states
                       WHERE project_id = target.project_id AND user_uuid = target.user_uuid
                         AND placement_uuid = target.placement_uuid) THEN
        RETURN NULL;
    END IF;
    SELECT stream_uuid, topic_uuid INTO scope
    FROM messenger_message_placements
    WHERE project_id = target.project_id AND uuid = target.placement_uuid;
    IF NOT FOUND THEN
        SELECT stream_uuid, topic_uuid INTO scope
        FROM messenger_message_counter_contributions
        WHERE project_id = target.project_id AND user_uuid = target.user_uuid
          AND placement_uuid = target.placement_uuid;
    END IF;
    IF FOUND THEN
        PERFORM messenger_mark_counter_message(target.project_id, target.user_uuid,
                    target.placement_uuid, scope.stream_uuid, scope.topic_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_viewer_counter_changes
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_message_states
FOR EACH ROW EXECUTE FUNCTION messenger_track_viewer_counter_changes();
CREATE TRIGGER messenger_track_access_counter_changes
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_message_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_viewer_counter_changes();

CREATE FUNCTION messenger_track_placement_counter_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    viewer record;
BEGIN
    IF TG_OP = 'UPDATE' AND (NEW.stream_uuid, NEW.topic_uuid, NEW.message_uuid)
          IS NOT DISTINCT FROM (OLD.stream_uuid, OLD.topic_uuid, OLD.message_uuid) THEN
        RETURN NULL;
    END IF;
    FOR viewer IN
        SELECT user_uuid FROM messenger_user_message_bindings
        WHERE project_id = OLD.project_id AND placement_uuid = OLD.uuid
        UNION
        SELECT user_uuid FROM messenger_user_message_states
        WHERE project_id = OLD.project_id AND placement_uuid = OLD.uuid
    LOOP
        PERFORM messenger_mark_counter_message(OLD.project_id, viewer.user_uuid,
                    OLD.uuid, OLD.stream_uuid, OLD.topic_uuid);
        IF TG_OP = 'UPDATE' THEN
            PERFORM messenger_mark_counter_message(NEW.project_id, viewer.user_uuid,
                        NEW.uuid, NEW.stream_uuid, NEW.topic_uuid);
        END IF;
    END LOOP;
    RETURN OLD;
END;
$$;
CREATE TRIGGER messenger_track_placement_counter_delete
BEFORE DELETE ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_track_placement_counter_changes();
CREATE TRIGGER messenger_track_placement_counter_move
AFTER UPDATE ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_track_placement_counter_changes();

CREATE FUNCTION messenger_track_root_counter_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    viewer record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        FOR viewer IN
            SELECT state.user_uuid, placement.stream_uuid
            FROM messenger_user_message_states AS state
            JOIN messenger_message_placements AS placement
              ON placement.project_id = state.project_id AND placement.uuid = state.placement_uuid
            WHERE placement.project_id = OLD.project_id AND placement.topic_uuid = OLD.uuid
            UNION
            SELECT user_uuid, stream_uuid FROM messenger_message_counter_contributions
            WHERE project_id = OLD.project_id AND topic_uuid = OLD.uuid
        LOOP
            PERFORM messenger_mark_counter_scope(OLD.project_id, viewer.user_uuid,
                                                 viewer.stream_uuid, OLD.uuid);
        END LOOP;
        RETURN OLD;
    END IF;
    IF TG_TABLE_NAME = 'messenger_messages' THEN
        IF (NEW.deleted_at, NEW.created_at) IS NOT DISTINCT FROM (OLD.deleted_at, OLD.created_at) THEN
            RETURN NULL;
        END IF;
    ELSIF NEW.deleted_at IS NOT DISTINCT FROM OLD.deleted_at THEN
        RETURN NULL;
    END IF;
    FOR viewer IN
        SELECT placement.project_id, state.user_uuid, placement.uuid,
               placement.stream_uuid, placement.topic_uuid
        FROM messenger_message_placements AS placement
        JOIN messenger_user_message_states AS state
          ON state.project_id = placement.project_id AND state.placement_uuid = placement.uuid
        WHERE placement.project_id = NEW.project_id
          AND ((TG_TABLE_NAME = 'messenger_messages' AND placement.message_uuid = NEW.uuid)
            OR (TG_TABLE_NAME = 'messenger_topics' AND placement.topic_uuid = NEW.uuid))
    LOOP
        PERFORM messenger_mark_counter_message(viewer.project_id, viewer.user_uuid,
                    viewer.uuid, viewer.stream_uuid, viewer.topic_uuid);
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_root_counter_changes
AFTER UPDATE ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_track_root_counter_changes();
CREATE TRIGGER messenger_track_topic_counter_changes
AFTER UPDATE ON messenger_topics
FOR EACH ROW EXECUTE FUNCTION messenger_track_root_counter_changes();

CREATE TRIGGER messenger_track_topic_counter_delete
BEFORE DELETE ON messenger_topics
FOR EACH ROW EXECUTE FUNCTION messenger_track_root_counter_changes();

CREATE FUNCTION messenger_track_counter_preferences()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    target_stream uuid;
    membership_changed boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_stream := (SELECT stream_uuid FROM messenger_topics
                          WHERE project_id = OLD.project_id AND uuid = OLD.topic_uuid);
        IF EXISTS (SELECT 1 FROM messenger_streams WHERE project_id = OLD.project_id
                    AND uuid = target_stream AND source_name = 'zulip') THEN
            PERFORM messenger_mark_counter_scope(OLD.project_id, OLD.user_uuid,
                                                 target_stream, OLD.topic_uuid);
        END IF;
        RETURN NULL;
    END IF;
    IF TG_TABLE_NAME = 'messenger_stream_bindings' THEN
        target_stream := NEW.stream_uuid;
        IF TG_OP = 'UPDATE' THEN
            membership_changed := (NEW.active, NEW.membership_generation, NEW.membership_started_at)
                IS DISTINCT FROM (OLD.active, OLD.membership_generation, OLD.membership_started_at);
            IF NOT membership_changed AND NEW.notification_mode IS NOT DISTINCT FROM OLD.notification_mode THEN
                RETURN NULL;
            END IF;
        END IF;
    ELSE
        IF TG_OP = 'UPDATE' AND NEW.notification_mode IS NOT DISTINCT FROM OLD.notification_mode THEN
            RETURN NULL;
        END IF;
        target_stream := (SELECT stream_uuid FROM messenger_topics
                          WHERE project_id = NEW.project_id AND uuid = NEW.topic_uuid);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM messenger_streams WHERE project_id = NEW.project_id
                    AND uuid = target_stream AND source_name = 'zulip') THEN
        RETURN NULL;
    END IF;
    -- The current membership epoch selects its own totals. State backfill
    -- queues individual messages without synchronously walking old history.
    IF TG_TABLE_NAME = 'messenger_stream_bindings' THEN
        PERFORM messenger_mark_counter_scope(NEW.project_id, NEW.user_uuid, target_stream, NULL);
        INSERT INTO bridge_zulip_v5_message_counter_changes
            (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
        SELECT NEW.project_id, NEW.user_uuid, 'user-topic', topic.uuid, target_stream, topic.uuid
        FROM messenger_topics AS topic
        WHERE topic.project_id = NEW.project_id AND topic.stream_uuid = target_stream
        ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid)
        DO UPDATE SET revision = EXCLUDED.revision;
    ELSE
        PERFORM messenger_mark_counter_scope(NEW.project_id, NEW.user_uuid, target_stream, NEW.topic_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_counter_membership
AFTER INSERT OR UPDATE ON messenger_stream_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_counter_preferences();
CREATE TRIGGER messenger_track_counter_preferences
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_topic_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_counter_preferences();

CREATE FUNCTION messenger_apply_message_counter_changes(
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
        SELECT actual.* FROM changes
        JOIN messenger_current_message_counter_contributions AS actual
          ON actual.project_id = changes.project_id AND actual.user_uuid = changes.user_uuid
         AND actual.placement_uuid = changes.placement_uuid
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

-- Seed both sides from one canonical snapshot before enabling delta publication.
INSERT INTO messenger_message_counter_contributions
SELECT * FROM messenger_current_message_counter_contributions;
INSERT INTO messenger_message_counter_totals
SELECT project_id, user_uuid, stream_uuid, topic_uuid, membership_generation, membership_started_at,
       count(*), count(*) FILTER (WHERE mentioned)
FROM messenger_message_counter_contributions
GROUP BY project_id, user_uuid, stream_uuid, topic_uuid, membership_generation, membership_started_at;

UPDATE messenger_user_topic_bindings AS binding
SET unread_count = totals.unread_count, active_unread_count = totals.active_unread_count,
    passive_unread_count = totals.unread_count - totals.active_unread_count
FROM messenger_topic_counter_totals AS totals
WHERE binding.project_id = totals.project_id AND binding.user_uuid = totals.user_uuid
  AND binding.topic_uuid = totals.topic_uuid;
UPDATE messenger_stream_bindings AS binding
SET unread_count = totals.unread_count, active_unread_count = totals.active_unread_count,
    passive_unread_count = totals.unread_count - totals.active_unread_count
FROM (
    SELECT project_id, user_uuid, stream_uuid, sum(unread_count) AS unread_count,
           sum(active_unread_count) AS active_unread_count
    FROM messenger_topic_counter_totals GROUP BY project_id, user_uuid, stream_uuid
) AS totals
WHERE binding.project_id = totals.project_id AND binding.user_uuid = totals.user_uuid
  AND binding.stream_uuid = totals.stream_uuid;
INSERT INTO bridge_zulip_v5_message_counter_changes
    (project_id, user_uuid, scope_kind, scope_uuid, stream_uuid, topic_uuid)
SELECT totals.project_id, totals.user_uuid, scope.kind, scope.uuid, totals.stream_uuid, scope.topic_uuid
FROM messenger_topic_counter_totals AS totals
CROSS JOIN LATERAL (VALUES ('user-stream', totals.stream_uuid, NULL::uuid),
                          ('user-topic', totals.topic_uuid, totals.topic_uuid)) AS scope(kind, uuid, topic_uuid)
GROUP BY totals.project_id, totals.user_uuid, scope.kind, scope.uuid, totals.stream_uuid, scope.topic_uuid
ON CONFLICT (project_id, user_uuid, scope_kind, scope_uuid) DO UPDATE SET revision = EXCLUDED.revision;

ANALYZE messenger_message_counter_contributions;
ANALYZE messenger_message_counter_totals;
ANALYZE messenger_message_counter_changes;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0228-Reconcile-Zulip-wildcard-mentions-with-notification-policies-afb4d0.py"
        ]

    @property
    def migration_id(self):
        return "5d0ad5eb-b490-4f85-9142-4c17fa09ddfc"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER messenger_track_counter_preferences ON messenger_user_topic_bindings;
            DROP TRIGGER messenger_track_counter_membership ON messenger_stream_bindings;
            DROP TRIGGER messenger_track_topic_counter_delete ON messenger_topics;
            DROP TRIGGER messenger_track_topic_counter_changes ON messenger_topics;
            DROP TRIGGER messenger_track_root_counter_changes ON messenger_messages;
            DROP TRIGGER messenger_track_placement_counter_move ON messenger_message_placements;
            DROP TRIGGER messenger_track_placement_counter_delete ON messenger_message_placements;
            DROP TRIGGER messenger_track_access_counter_changes ON messenger_user_message_bindings;
            DROP TRIGGER messenger_track_viewer_counter_changes ON messenger_user_message_states;
            DROP FUNCTION messenger_apply_message_counter_changes(uuid, uuid, uuid, integer);
            DROP FUNCTION messenger_track_counter_preferences();
            DROP FUNCTION messenger_track_root_counter_changes();
            DROP FUNCTION messenger_track_placement_counter_changes();
            DROP FUNCTION messenger_track_viewer_counter_changes();
            DROP FUNCTION messenger_mark_counter_message(uuid, uuid, uuid, uuid, uuid);
            DROP FUNCTION messenger_mark_counter_scope(uuid, uuid, uuid, uuid);
            DROP VIEW messenger_topic_counter_totals;
            DROP VIEW messenger_current_message_counter_contributions;
            DROP TABLE messenger_message_counter_changes;
            DROP TABLE messenger_message_counter_totals;
            DROP TABLE messenger_message_counter_contributions;
        """)


migration_step = MigrationStep()
