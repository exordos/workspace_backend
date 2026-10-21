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
LOCK TABLE bridge_zulip_v5_stream_states, bridge_zulip_v5_topic_states,
    bridge_zulip_v5_message_states, bridge_zulip_v5_message_placement_states
IN ACCESS EXCLUSIVE MODE;
ALTER TABLE messenger_user_message_states
    ADD COLUMN binding_uuid uuid,
    ADD COLUMN zulip_source_uuid uuid,
    ADD COLUMN zulip_flags_updated_at timestamp without time zone,
    ADD CONSTRAINT messenger_user_message_states_binding_fkey
        FOREIGN KEY (project_id, binding_uuid)
        REFERENCES messenger_user_message_bindings(project_id, uuid) ON DELETE CASCADE;
CREATE INDEX messenger_user_message_states_placement_idx
    ON messenger_user_message_states(project_id, placement_uuid, user_uuid);
CREATE INDEX messenger_user_message_states_binding_idx
    ON messenger_user_message_states(project_id, binding_uuid);
CREATE INDEX messenger_zulip_messages_author_idx
    ON messenger_messages(project_id, author_uuid) WHERE source_name = 'zulip';

CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_account_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT message.project_id,
           messenger_uuid_v5(message.project_id, message.message_uuid::text)
    FROM bridge_zulip_v5_messages AS message WHERE message.external_account_uuid = NEW.uuid
    ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    IF OLD.owner_user_uuid IS DISTINCT FROM NEW.owner_user_uuid THEN
        INSERT INTO messenger_message_changes (project_id, message_uuid)
        SELECT project_id, uuid FROM messenger_messages
        WHERE project_id = NEW.project_id AND author_uuid = OLD.owner_user_uuid AND source_name = 'zulip'
        ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER bridge_zulip_v5_track_account_message_changes ON m_external_accounts_v2;
CREATE TRIGGER bridge_zulip_v5_track_account_message_changes
AFTER UPDATE OF provider, owner_user_uuid ON m_external_accounts_v2
FOR EACH ROW WHEN (OLD.provider IS DISTINCT FROM NEW.provider
                   OR OLD.owner_user_uuid IS DISTINCT FROM NEW.owner_user_uuid)
EXECUTE FUNCTION bridge_zulip_v5_track_account_message_changes();

CREATE FUNCTION messenger_mark_zulip_user_message_changed()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record;
BEGIN
    FOR target IN
        SELECT OLD.project_id AS project_id, OLD.placement_uuid AS placement_uuid WHERE TG_OP <> 'INSERT'
        UNION SELECT NEW.project_id, NEW.placement_uuid WHERE TG_OP <> 'DELETE'
    LOOP
        INSERT INTO messenger_message_changes (project_id, message_uuid)
        SELECT placement.project_id, placement.message_uuid
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
        WHERE placement.project_id = target.project_id AND placement.uuid = target.placement_uuid
          AND placement.uuid = messenger_uuid_v5(message.uuid, 'zulip-placement') AND message.source_name = 'zulip'
        ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_user_message_bindings
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_message_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_mark_zulip_user_message_changed();
CREATE TRIGGER messenger_track_zulip_user_message_states
AFTER INSERT OR UPDATE OR DELETE ON messenger_user_message_states
FOR EACH ROW EXECUTE FUNCTION messenger_mark_zulip_user_message_changed();

CREATE FUNCTION messenger_track_zulip_membership_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record;
BEGIN
    IF TG_OP = 'UPDATE' AND (OLD.project_id, OLD.stream_uuid, OLD.user_uuid, OLD.active, OLD.membership_generation)
        IS NOT DISTINCT FROM (NEW.project_id, NEW.stream_uuid, NEW.user_uuid, NEW.active, NEW.membership_generation) THEN
        RETURN NULL;
    END IF;
    FOR target IN
        SELECT OLD.project_id AS project_id, OLD.stream_uuid AS stream_uuid, OLD.user_uuid AS user_uuid WHERE TG_OP <> 'INSERT'
        UNION SELECT NEW.project_id, NEW.stream_uuid, NEW.user_uuid WHERE TG_OP <> 'DELETE'
    LOOP
        INSERT INTO messenger_message_changes (project_id, message_uuid)
        SELECT DISTINCT placement.project_id, placement.message_uuid
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
        WHERE placement.project_id = target.project_id AND placement.stream_uuid = target.stream_uuid
          AND message.source_name = 'zulip' AND placement.uuid = messenger_uuid_v5(message.uuid, 'zulip-placement')
        ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    END LOOP;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_track_zulip_membership_message_changes
AFTER INSERT OR UPDATE OR DELETE ON messenger_stream_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_track_zulip_membership_message_changes();

CREATE VIEW bridge_zulip_v5_user_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, target.user_uuid, placement.uuid AS placement_uuid,
       COALESCE(binding.uuid, messenger_uuid_v5(placement.uuid, target.user_uuid::text)) AS binding_uuid,
       COALESCE(state.uuid, messenger_uuid_v5(placement.uuid, target.user_uuid::text)) AS state_uuid,
       target.source_uuid, target.flags_updated_at, target.membership_generation,
       CASE WHEN canonical.author_uuid = target.user_uuid THEN 'author' ELSE 'member' END AS relation_role,
       target.read, target.starred, target.mentioned,
       CASE WHEN target.source_uuid IS NULL THEN 'delete'
            WHEN binding.uuid IS NULL OR state.uuid IS NULL THEN 'create' ELSE 'update' END AS action
FROM changes
JOIN messenger_messages AS canonical
  ON canonical.project_id = changes.project_id AND canonical.uuid = changes.message_uuid AND canonical.source_name = 'zulip'
JOIN messenger_message_placements AS placement
  ON placement.project_id = changes.project_id AND placement.message_uuid = changes.message_uuid
 AND placement.uuid = messenger_uuid_v5(changes.message_uuid, 'zulip-placement')
CROSS JOIN LATERAL (
    WITH desired AS (
        SELECT DISTINCT ON (account.owner_user_uuid)
               account.owner_user_uuid AS user_uuid, observed.uuid AS source_uuid,
               observed.updated_at AT TIME ZONE 'UTC' AS flags_updated_at, membership.membership_generation,
               observed.read, observed.starred, observed.mentioned
        FROM bridge_zulip_v5_messages AS observed
        JOIN m_external_accounts_v2 AS account ON account.uuid = observed.external_account_uuid AND account.project_id = observed.project_id
        JOIN bridge_zulip_v5_channels AS channel ON channel.uuid = observed.channel AND channel.stream_uuid = placement.stream_uuid
        JOIN messenger_stream_bindings AS membership
          ON membership.project_id = observed.project_id AND membership.stream_uuid = placement.stream_uuid
         AND membership.user_uuid = account.owner_user_uuid AND membership.active
        WHERE observed.project_id = changes.project_id
          AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text) = changes.message_uuid
          AND account.provider = 'zulip' AND canonical.deleted_at IS NULL
        ORDER BY account.owner_user_uuid, observed.updated_at DESC, observed.uuid
    ), actual AS (
        SELECT user_uuid FROM messenger_user_message_bindings
        WHERE project_id = changes.project_id AND placement_uuid = placement.uuid
        UNION
        SELECT user_uuid FROM messenger_user_message_states
        WHERE project_id = changes.project_id AND placement_uuid = placement.uuid
    )
    SELECT COALESCE(desired.user_uuid, actual.user_uuid) AS user_uuid,
           desired.source_uuid, desired.flags_updated_at, desired.membership_generation,
           desired.read, desired.starred, desired.mentioned
    FROM desired FULL JOIN actual ON actual.user_uuid = desired.user_uuid
) AS target
LEFT JOIN messenger_user_message_bindings AS binding
  ON binding.project_id = changes.project_id AND binding.placement_uuid = placement.uuid AND binding.user_uuid = target.user_uuid
LEFT JOIN messenger_user_message_states AS state
  ON state.project_id = changes.project_id AND state.placement_uuid = placement.uuid AND state.user_uuid = target.user_uuid
WHERE target.source_uuid IS NULL
   OR binding.uuid IS NULL OR state.uuid IS NULL
   OR binding.membership_generation <> target.membership_generation
   OR binding.relation_role IS DISTINCT FROM CASE WHEN canonical.author_uuid = target.user_uuid THEN 'author' ELSE 'member' END
   OR binding.visibility <> 'visible'
   OR binding.permissions <> '{"read":true,"react":true,"star":true,"pin":true}'::jsonb
   OR state.binding_uuid IS DISTINCT FROM binding.uuid
   OR state.membership_generation <> target.membership_generation
   OR state.zulip_source_uuid IS DISTINCT FROM target.source_uuid
   OR state.zulip_flags_updated_at IS NULL OR state.zulip_flags_updated_at < target.flags_updated_at;
CREATE OR REPLACE VIEW bridge_zulip_v5_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid
    FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id
          AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, source.uuid AS source_uuid,
       CASE WHEN source.uuid IS NULL THEN 'delete'
            WHEN workspace.uuid IS NULL OR workspace.deleted_at IS NOT NULL
                THEN 'create'
            ELSE 'update' END AS action
FROM changes
LEFT JOIN LATERAL (
    SELECT message.uuid, author.owner_user_uuid AS author_uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    JOIN bridge_zulip_v5_message_contents AS content ON content.uuid = message.content
    LEFT JOIN m_external_accounts_v2 AS author
      ON author.project_id = message.project_id AND author.provider = 'zulip'
     AND author.external_account_id = regexp_replace(account.external_account_id, ':[^:]+$', '')
         || ':' || message.sender_id::text
    WHERE message.project_id = changes.project_id
      AND messenger_uuid_v5(message.project_id, message.message_uuid::text)
          = changes.message_uuid
      AND account.provider = 'zulip'
    ORDER BY content.workspace_content_updated_at DESC, content.uuid, message.uuid
    LIMIT 1
) AS source ON true
LEFT JOIN messenger_messages AS workspace
  ON workspace.project_id = changes.project_id
 AND workspace.uuid = changes.message_uuid
WHERE (source.uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (workspace.source_name = 'zulip'
       AND (source.uuid IS NULL OR workspace.deleted_at IS NOT NULL
            OR (source.author_uuid IS NOT NULL AND workspace.author_uuid IS DISTINCT FROM source.author_uuid)
            OR (workspace.payload->>'kind' = 'markdown' AND EXISTS (
                SELECT 1
                FROM bridge_zulip_v5_messages AS observed
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = observed.external_account_uuid
                 AND account.project_id = observed.project_id
                JOIN bridge_zulip_v5_message_contents AS content
                  ON content.uuid = observed.content
                WHERE observed.project_id = changes.project_id
                  AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text)
                      = changes.message_uuid
                  AND account.provider = 'zulip'
                  AND (content.workspace_content_hash IS DISTINCT FROM workspace.content_hash
                       OR content.workspace_content_updated_at
                           IS DISTINCT FROM workspace.content_updated_at)
            ))));


INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
SELECT DISTINCT message.project_id, messenger_uuid_v5(message.project_id, message.message_uuid::text)
FROM bridge_zulip_v5_messages AS message
JOIN m_external_accounts_v2 AS account ON account.uuid = message.external_account_uuid AND account.project_id = message.project_id
WHERE account.provider = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
INSERT INTO messenger_message_changes (project_id, message_uuid)
SELECT project_id, uuid FROM messenger_messages WHERE source_name = 'zulip'
ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
"""


DOWNGRADE_SQL = """
DROP VIEW bridge_zulip_v5_user_message_states;
DROP TRIGGER messenger_track_zulip_membership_message_changes ON messenger_stream_bindings;
DROP FUNCTION messenger_track_zulip_membership_message_changes();
DROP TRIGGER messenger_track_zulip_user_message_bindings ON messenger_user_message_bindings;
DROP TRIGGER messenger_track_zulip_user_message_states ON messenger_user_message_states;
DROP FUNCTION messenger_mark_zulip_user_message_changed();
ALTER TABLE messenger_user_message_states DROP COLUMN binding_uuid, DROP COLUMN zulip_source_uuid, DROP COLUMN zulip_flags_updated_at;
DROP INDEX messenger_user_message_states_placement_idx;
DROP INDEX messenger_zulip_messages_author_idx;
DROP TRIGGER bridge_zulip_v5_track_account_message_changes ON m_external_accounts_v2;
CREATE TRIGGER bridge_zulip_v5_track_account_message_changes
AFTER UPDATE OF provider ON m_external_accounts_v2
FOR EACH ROW WHEN (OLD.provider IS DISTINCT FROM NEW.provider)
EXECUTE FUNCTION bridge_zulip_v5_track_account_message_changes();
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_account_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO bridge_zulip_v5_message_changes (project_id, message_uuid)
    SELECT DISTINCT message.project_id, messenger_uuid_v5(message.project_id, message.message_uuid::text)
    FROM bridge_zulip_v5_messages AS message WHERE message.external_account_uuid = NEW.uuid
    ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE VIEW bridge_zulip_v5_message_states AS
WITH changes AS (
    SELECT project_id, message_uuid FROM bridge_zulip_v5_message_changes
    UNION ALL
    SELECT workspace.project_id, workspace.message_uuid
    FROM messenger_message_changes AS workspace
    WHERE NOT EXISTS (
        SELECT 1 FROM bridge_zulip_v5_message_changes AS zulip
        WHERE zulip.project_id = workspace.project_id
          AND zulip.message_uuid = workspace.message_uuid
    )
)
SELECT changes.project_id, changes.message_uuid, source.uuid AS source_uuid,
       CASE WHEN source.uuid IS NULL THEN 'delete'
            WHEN workspace.uuid IS NULL OR workspace.deleted_at IS NOT NULL
                THEN 'create'
            ELSE 'update' END AS action
FROM changes
LEFT JOIN LATERAL (
    SELECT message.uuid
    FROM bridge_zulip_v5_messages AS message
    JOIN m_external_accounts_v2 AS account
      ON account.uuid = message.external_account_uuid
     AND account.project_id = message.project_id
    JOIN bridge_zulip_v5_message_contents AS content ON content.uuid = message.content
    WHERE message.project_id = changes.project_id
      AND messenger_uuid_v5(message.project_id, message.message_uuid::text)
          = changes.message_uuid
      AND account.provider = 'zulip'
    ORDER BY content.workspace_content_updated_at DESC, content.uuid, message.uuid
    LIMIT 1
) AS source ON true
LEFT JOIN messenger_messages AS workspace
  ON workspace.project_id = changes.project_id
 AND workspace.uuid = changes.message_uuid
WHERE (source.uuid IS NOT NULL AND workspace.uuid IS NULL)
   OR (workspace.source_name = 'zulip'
       AND (source.uuid IS NULL OR workspace.deleted_at IS NOT NULL
            OR (workspace.payload->>'kind' = 'markdown' AND EXISTS (
                SELECT 1
                FROM bridge_zulip_v5_messages AS observed
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = observed.external_account_uuid
                 AND account.project_id = observed.project_id
                JOIN bridge_zulip_v5_message_contents AS content
                  ON content.uuid = observed.content
                WHERE observed.project_id = changes.project_id
                  AND messenger_uuid_v5(observed.project_id, observed.message_uuid::text)
                      = changes.message_uuid
                  AND account.provider = 'zulip'
                  AND (content.workspace_content_hash IS DISTINCT FROM workspace.content_hash
                       OR content.workspace_content_updated_at
                           IS DISTINCT FROM workspace.content_updated_at)
            ))));

"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0222-Reconcile-Zulip-message-placements-1f12c4.py"]

    @property
    def migration_id(self):
        return "6430fcd3-3700-49d4-a49a-c1a60ca8dd2d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
