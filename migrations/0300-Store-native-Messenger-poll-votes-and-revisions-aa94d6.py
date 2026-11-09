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
ALTER TABLE messenger_messages
    ADD COLUMN poll_state_revision bigint NOT NULL DEFAULT 0 CHECK (poll_state_revision >= 0);

CREATE TABLE messenger_poll_votes (
    project_id uuid NOT NULL,
    message_uuid uuid NOT NULL,
    user_uuid uuid NOT NULL,
    question_uuid uuid NOT NULL,
    option_uuid uuid NOT NULL,
    PRIMARY KEY (project_id, message_uuid, user_uuid, question_uuid, option_uuid),
    FOREIGN KEY (project_id, message_uuid)
        REFERENCES messenger_messages(project_id, uuid) ON DELETE CASCADE,
    FOREIGN KEY (project_id, user_uuid)
        REFERENCES messenger_project_users(project_id, user_uuid) ON DELETE CASCADE
);

-- An account cleanup searches across all polls of a project. This index
-- supports both the pre-lock query and the subsequent FK cascade.
CREATE INDEX messenger_poll_votes_user_idx ON messenger_poll_votes(project_id,user_uuid);

-- Lock the roots before the account FK starts deleting votes. Requests take
-- a key-share lock on their account before locking the poll. This keeps a
-- cascade and a concurrent click in the same root-before-vote lock order.
CREATE FUNCTION messenger_lock_account_polls() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM m.uuid
    FROM messenger_messages m
    JOIN (SELECT DISTINCT message_uuid FROM messenger_poll_votes
          WHERE project_id=OLD.project_id AND user_uuid=OLD.user_uuid) votes
      ON votes.message_uuid=m.uuid
    WHERE m.project_id=OLD.project_id
    ORDER BY m.uuid FOR NO KEY UPDATE OF m;
    RETURN OLD;
END $$;
CREATE TRIGGER messenger_lock_account_polls BEFORE DELETE ON messenger_project_users
    FOR EACH ROW EXECUTE FUNCTION messenger_lock_account_polls();

-- Transition tables cover both explicit requests and FK cascades. A removed
-- message has no surviving root and produces only its existing delete event.
CREATE FUNCTION messenger_track_poll_votes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE messenger_messages m SET poll_state_revision=m.poll_state_revision+1
    FROM (SELECT DISTINCT project_id,message_uuid FROM changed_votes) votes
    WHERE m.project_id=votes.project_id AND m.uuid=votes.message_uuid;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_insert_poll_votes AFTER INSERT ON messenger_poll_votes
    REFERENCING NEW TABLE AS changed_votes
    FOR EACH STATEMENT EXECUTE FUNCTION messenger_track_poll_votes();
CREATE TRIGGER messenger_remove_poll_votes AFTER DELETE ON messenger_poll_votes
    REFERENCING OLD TABLE AS changed_votes
    FOR EACH STATEMENT EXECUTE FUNCTION messenger_track_poll_votes();
-- The single-choice replacement changes only option_uuid in its existing row.
CREATE TRIGGER messenger_replace_poll_votes AFTER UPDATE ON messenger_poll_votes
    REFERENCING NEW TABLE AS changed_votes
    FOR EACH STATEMENT EXECUTE FUNCTION messenger_track_poll_votes();

CREATE FUNCTION messenger_queue_poll_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
    SELECT gen_random_uuid(),NEW.project_id,'delivery_snapshot_event','message',
           NEW.project_id::text||':'||NEW.uuid::text,
           jsonb_build_object('source_kind','message.updated','poll_changed',true,
               'canonical_message_uuid',NEW.uuid,'placement_uuid',p.uuid,'stream_uuid',p.stream_uuid)
    FROM messenger_message_placements p
    WHERE p.project_id=NEW.project_id AND p.message_uuid=NEW.uuid;
    RETURN NULL;
END $$;
CREATE TRIGGER messenger_queue_poll_change AFTER UPDATE OF poll_state_revision ON messenger_messages
    FOR EACH ROW WHEN (NEW.poll_state_revision IS DISTINCT FROM OLD.poll_state_revision)
    EXECUTE FUNCTION messenger_queue_poll_change();
"""

DOWNGRADE = """
DROP TRIGGER messenger_queue_poll_change ON messenger_messages;
DROP FUNCTION messenger_queue_poll_change();
DROP TRIGGER messenger_lock_account_polls ON messenger_project_users;
DROP FUNCTION messenger_lock_account_polls();
DROP TABLE messenger_poll_votes;
DROP FUNCTION messenger_track_poll_votes();
ALTER TABLE messenger_messages DROP COLUMN poll_state_revision;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0299-Expand-stream-names-to-2048-characters-400671.py"]

    @property
    def migration_id(self):
        return "aa94d68c-f422-49d8-8637-00dc9100eb64"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute("LOCK TABLE messenger_messages IN ACCESS EXCLUSIVE MODE")
        if session.execute(
            "SELECT 1 FROM messenger_messages WHERE poll_state_revision>0 LIMIT 1"
        ).fetchone():
            raise RuntimeError("Cannot downgrade while published Messenger polls exist")
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
