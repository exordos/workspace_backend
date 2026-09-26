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


UPGRADE = """
ALTER TABLE workspace_v3.users
    ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN is_bot BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE workspace_v3.provider_entity_states (
    project_id UUID NOT NULL,
    provider_uuid UUID NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_uuid UUID NOT NULL,
    content_hash BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id, provider_uuid, entity_type, entity_uuid),
    UNIQUE (project_id, entity_type, entity_uuid),
    CONSTRAINT provider_entity_states_provider_fkey
        FOREIGN KEY (project_id, provider_uuid)
        REFERENCES workspace_v3.provider_consumers (project_id, uuid)
        ON DELETE CASCADE,
    CONSTRAINT provider_entity_states_type_check
        CHECK (entity_type IN (
            'user', 'stream', 'stream_binding', 'topic', 'topic_binding',
            'message', 'message_flag', 'message_reaction'
        )),
    CONSTRAINT provider_entity_states_hash_check
        CHECK (octet_length(content_hash) = 32)
);

CREATE INDEX provider_entity_states_list_idx
    ON workspace_v3.provider_entity_states (
        project_id, provider_uuid, entity_type, updated_at, entity_uuid
    );

CREATE FUNCTION workspace_v3.cleanup_provider_entity_state()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_ARGV[1] = 'global' THEN
        DELETE FROM workspace_v3.provider_entity_states
        WHERE entity_type = TG_ARGV[0] AND entity_uuid = OLD.uuid;
    ELSE
        DELETE FROM workspace_v3.provider_entity_states
        WHERE project_id = OLD.project_id
          AND entity_type = TG_ARGV[0]
          AND entity_uuid = OLD.uuid;
    END IF;
    RETURN OLD;
END
$function$;

CREATE TRIGGER users_provider_state_cleanup
    AFTER DELETE ON workspace_v3.users
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'user', 'global'
    );
CREATE TRIGGER streams_provider_state_cleanup
    AFTER DELETE ON workspace_v3.streams
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'stream', 'project'
    );
CREATE TRIGGER stream_bindings_provider_state_cleanup
    AFTER DELETE ON workspace_v3.stream_bindings
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'stream_binding', 'project'
    );
CREATE TRIGGER topics_provider_state_cleanup
    AFTER DELETE ON workspace_v3.topics
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'topic', 'project'
    );
CREATE TRIGGER topic_bindings_provider_state_cleanup
    AFTER DELETE ON workspace_v3.topic_bindings
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'topic_binding', 'project'
    );
CREATE TRIGGER messages_provider_state_cleanup
    AFTER DELETE ON workspace_v3.messages
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message', 'project'
    );
CREATE TRIGGER message_flags_provider_state_cleanup
    AFTER DELETE ON workspace_v3.message_flags
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message_flag', 'project'
    );
CREATE TRIGGER message_reactions_provider_state_cleanup
    AFTER DELETE ON workspace_v3.message_reactions
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.cleanup_provider_entity_state(
        'message_reaction', 'project'
    );
"""


DOWNGRADE = """
DROP TRIGGER message_reactions_provider_state_cleanup
    ON workspace_v3.message_reactions;
DROP TRIGGER message_flags_provider_state_cleanup
    ON workspace_v3.message_flags;
DROP TRIGGER messages_provider_state_cleanup ON workspace_v3.messages;
DROP TRIGGER topic_bindings_provider_state_cleanup
    ON workspace_v3.topic_bindings;
DROP TRIGGER topics_provider_state_cleanup ON workspace_v3.topics;
DROP TRIGGER stream_bindings_provider_state_cleanup
    ON workspace_v3.stream_bindings;
DROP TRIGGER streams_provider_state_cleanup ON workspace_v3.streams;
DROP TRIGGER users_provider_state_cleanup ON workspace_v3.users;
DROP FUNCTION workspace_v3.cleanup_provider_entity_state();
DROP TABLE workspace_v3.provider_entity_states;
ALTER TABLE workspace_v3.users
    DROP COLUMN is_bot,
    DROP COLUMN disabled;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0188-Add-Workspace-v3-provider-consumers-and-event-origins-4d57fc.py"
        ]

    @property
    def migration_id(self):
        return "d72d97bb-edaf-4b40-8ef3-38201f628bb3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
