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


CAPTURE = """
CREATE INDEX zulip_removed_message_identity
    ON zulip_entity_links(project_id,provider_uuid,provider_identity)
    WHERE entity_type='message';
CREATE FUNCTION messenger_capture_message_deletion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    placement record;
    actor uuid := nullif(current_setting('workspace.outgoing_actor',true),'')::uuid;
BEGIN
    -- Keep only the existing provider identity link, without the message body.
    -- Its missing root distinguishes a removed message from an unseen one.
    INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,
        provider_uuid,external_account_uuid,provider_identity)
    SELECT OLD.project_id,'message',OLD.uuid,OLD.provider_uuid,account.uuid,OLD.provider_identity
    FROM m_external_accounts_v2 account
    WHERE OLD.source_name='zulip' AND OLD.provider_identity IS NOT NULL
      AND account.project_id=OLD.project_id AND account.provider_uuid=OLD.provider_uuid
    ON CONFLICT(entity_type,entity_uuid,provider_uuid,external_account_uuid)
        DO UPDATE SET provider_identity=EXCLUDED.provider_identity;
    -- Old soft-delete tasks already own their delivery snapshots.
    IF OLD.deleted_at IS NOT NULL THEN RETURN OLD; END IF;
    FOR placement IN
        SELECT * FROM messenger_message_placements
        WHERE project_id=OLD.project_id AND message_uuid=OLD.uuid
        ORDER BY stream_uuid,topic_uuid,uuid
    LOOP
        -- Match every producer: last-message rows precede dirty counter rows.
        PERFORM messenger_repair_last_message(
            placement.project_id,placement.stream_uuid,placement.topic_uuid,placement.uuid);
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid)
        SELECT binding.project_id,binding.user_uuid,placement.stream_uuid,placement.topic_uuid
        FROM messenger_user_message_bindings binding
        WHERE binding.project_id=OLD.project_id AND binding.placement_uuid=placement.uuid
        ORDER BY binding.project_id,binding.user_uuid
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE
            SET revision=EXCLUDED.revision,next_retry_at=NULL,
                publish_updates=messenger_counter_updates.publish_updates OR EXCLUDED.publish_updates;
        IF actor IS NOT NULL THEN
            -- The existing worker can deliver bounded recipient snapshots after
            -- the canonical root and all visibility bindings have disappeared.
            WITH recipients AS (
                SELECT binding.user_uuid,member.membership_generation,
                       (row_number() OVER(ORDER BY binding.user_uuid)-1)/1000 AS batch_no
                FROM messenger_user_message_bindings binding
                JOIN messenger_stream_bindings member
                  ON member.project_id=binding.project_id AND member.user_uuid=binding.user_uuid
                 AND member.stream_uuid=placement.stream_uuid AND member.active
                WHERE binding.project_id=OLD.project_id AND binding.placement_uuid=placement.uuid
            ), batches AS (
                SELECT batch_no,jsonb_agg(user_uuid ORDER BY user_uuid) AS users,
                       jsonb_object_agg(user_uuid::text,membership_generation) AS generations
                FROM recipients GROUP BY batch_no
            )
            INSERT INTO messenger_domain_outbox_events(
                uuid,project_id,event_kind,scope_kind,scope_key,payload)
            SELECT gen_random_uuid(),OLD.project_id,'delivery_snapshot_event','message',
                OLD.project_id::text||':'||OLD.uuid::text,
                jsonb_build_object(
                    'source_kind','message.deleted',
                    'placement',jsonb_build_object('uuid',placement.uuid,
                        'stream_uuid',placement.stream_uuid,'topic_uuid',placement.topic_uuid),
                    'canonical_message_uuid',OLD.uuid,'message_created_at',OLD.created_at,
                    'author_uuid',OLD.author_uuid,'source_name',OLD.source_name,
                    'provider_uuid',OLD.provider_uuid,'provider_identity',OLD.provider_identity,
                    'recipients',users,'membership_generations',generations)
            FROM batches ORDER BY batch_no;
        END IF;
    END LOOP;
    PERFORM pg_notify('workspace_counters','');
    RETURN OLD;
END $$;
CREATE TRIGGER messenger_capture_message_deletion BEFORE DELETE ON messenger_messages
    FOR EACH ROW EXECUTE FUNCTION messenger_capture_message_deletion();
"""

BEFORE_RETURN = (
    "IF TG_WHEN = 'BEFORE' AND TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NULL;"
)
UPDATE_GUARD = "IF row_->'payload' IS NOT DISTINCT FROM old_->'payload'"


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0286-Replace-per-message-history-intents-with-bulk-import-batches-16952d.py"
        ]

    @property
    def migration_id(self):
        return "7749a8fd-417b-4be0-91dd-dbd8cd9d6707"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(CAPTURE)
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        if definition.count(UPDATE_GUARD) != 1 or "RETURN NULL;" not in definition:
            raise RuntimeError("Unexpected outgoing capture function definition")
        definition = definition.replace(
            UPDATE_GUARD, "IF TG_OP='UPDATE' AND " + UPDATE_GUARD[3:]
        )
        # The same provider capture now runs before root deletion, while its
        # placement and routing metadata are still available. AFTER calls keep
        # their existing return behavior; a skipped BEFORE call must allow DELETE.
        session.execute(definition.replace("RETURN NULL;", BEFORE_RETURN))
        session.execute("""
            CREATE TRIGGER zulip_capture_message_delete BEFORE DELETE ON messenger_messages
                FOR EACH ROW EXECUTE FUNCTION zulip_capture_outgoing('message');
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER zulip_capture_message_delete ON messenger_messages;
            DROP TRIGGER messenger_capture_message_deletion ON messenger_messages;
            DROP FUNCTION messenger_capture_message_deletion();
            DROP INDEX zulip_removed_message_identity;
        """)
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS definition"
        ).fetchone()["definition"]
        definition = definition.replace(
            "IF TG_OP='UPDATE' AND " + UPDATE_GUARD[3:], UPDATE_GUARD
        )
        session.execute(definition.replace(BEFORE_RETURN, "RETURN NULL;"))


migration_step = MigrationStep()
