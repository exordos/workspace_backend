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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0241-Convert-Zulip-message-content-using-persisted-identities-and-files-c8ac7d.py"
        ]

    @property
    def migration_id(self):
        return "e9bd040f-9373-4db3-9bdc-e29afe74da4b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            ALTER TABLE bridge_zulip_v5_message_contents
                ADD COLUMN IF NOT EXISTS source_timestamp BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS source_message_id BIGINT NOT NULL DEFAULT 0;
            ALTER TABLE bridge_zulip_v5_message_contents
                DISABLE TRIGGER bridge_zulip_v5_track_message_content_changes;
            UPDATE bridge_zulip_v5_message_contents AS content
            SET source_timestamp=observed.timestamp, source_message_id=observed.message_id
            FROM (SELECT DISTINCT ON (content) content,timestamp,message_id
                  FROM bridge_zulip_v5_messages ORDER BY content,timestamp DESC,message_id DESC) AS observed
            WHERE content.uuid=observed.content;
            ALTER TABLE bridge_zulip_v5_message_contents
                ENABLE TRIGGER bridge_zulip_v5_track_message_content_changes;
            CREATE INDEX bridge_zulip_v5_content_newest_idx
                ON bridge_zulip_v5_message_contents(source_timestamp DESC,source_message_id DESC,uuid DESC)
                WHERE conversion_version < 1;
            CREATE FUNCTION bridge_zulip_v5_content_source_timestamp()
            RETURNS TRIGGER LANGUAGE plpgsql AS $$
            BEGIN
                UPDATE bridge_zulip_v5_message_contents
                SET source_timestamp=NEW.timestamp, source_message_id=NEW.message_id
                WHERE uuid=NEW.content AND (source_timestamp,source_message_id)<(NEW.timestamp,NEW.message_id);
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER bridge_zulip_v5_content_source_timestamp
                AFTER INSERT OR UPDATE OF timestamp,message_id,content ON bridge_zulip_v5_messages
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_content_source_timestamp();

            CREATE FUNCTION bridge_zulip_v5_message_created_at(p_project UUID,p_message UUID)
            RETURNS TIMESTAMP WITHOUT TIME ZONE LANGUAGE SQL AS $$
                SELECT COALESCE(
                    (SELECT created_at FROM messenger_messages
                     WHERE project_id=p_project AND uuid=p_message),
                    (SELECT to_timestamp(max(timestamp)) AT TIME ZONE 'UTC'
                     FROM bridge_zulip_v5_messages
                     WHERE project_id=p_project
                       AND messenger_uuid_v5(project_id,message_uuid::text)=p_message)
                )
            $$;

            CREATE FUNCTION bridge_zulip_v5_order_message_change()
            RETURNS TRIGGER LANGUAGE plpgsql AS $$
            DECLARE
                target_message_uuid UUID;
            BEGIN
                IF TG_TABLE_NAME='bridge_zulip_v5_tasks' THEN
                    IF NEW.content->>'kind'='update_message_flags' THEN
                        SELECT placement.message_uuid INTO target_message_uuid
                        FROM messenger_user_message_states AS state
                        JOIN messenger_message_placements AS placement
                          ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                        WHERE state.project_id=NEW.project_id AND state.uuid=(NEW.content->>'state_uuid')::uuid;
                    ELSIF NEW.content->>'kind'='update_reaction' THEN
                        SELECT canonical_message_uuid INTO target_message_uuid
                        FROM bridge_zulip_v5_reaction_intents
                        WHERE project_id=NEW.project_id AND uuid=(NEW.content->>'intent_uuid')::uuid;
                    ELSE
                        RETURN NEW;
                    END IF;
                ELSIF TG_TABLE_NAME='bridge_zulip_v5_flag_changes' THEN
                    SELECT placement.message_uuid INTO target_message_uuid
                    FROM messenger_user_message_states AS state
                    JOIN messenger_message_placements AS placement
                      ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                    WHERE state.project_id=NEW.project_id AND state.uuid=NEW.state_uuid;
                ELSIF TG_TABLE_NAME='bridge_zulip_v5_reaction_changes' THEN
                    target_message_uuid := NEW.canonical_message_uuid;
                ELSE
                    target_message_uuid := NEW.message_uuid;
                END IF;
                NEW.message_created_at := COALESCE(
                    bridge_zulip_v5_message_created_at(NEW.project_id,target_message_uuid),
                    NEW.message_created_at,
                    'epoch'::timestamp
                );
                NEW.message_sequence := COALESCE(
                    (SELECT max(observed.message_id) FROM bridge_zulip_v5_messages AS observed
                     WHERE observed.project_id=NEW.project_id
                       AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=target_message_uuid),
                    (SELECT max(delivery.zulip_message_id) FROM bridge_zulip_v5_deliveries AS delivery
                     WHERE delivery.project_id=NEW.project_id AND delivery.message_uuid=target_message_uuid),
                    NEW.message_sequence,
                    0
                );
                RETURN NEW;
            END;
            $$;
        """)
        for table in (
            "messenger_message_changes",
            "bridge_zulip_v5_message_changes",
            "bridge_zulip_v5_flag_changes",
            "bridge_zulip_v5_reaction_changes",
            "bridge_zulip_v5_tasks",
        ):
            index = (
                f"CREATE INDEX {table}_newest_idx ON {table}(message_created_at DESC,message_sequence DESC,revision);"
                if table != "bridge_zulip_v5_tasks"
                else ""
            )
            session.execute(f"""
                ALTER TABLE {table} ADD COLUMN IF NOT EXISTS message_created_at
                    TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT 'epoch',
                    ADD COLUMN IF NOT EXISTS message_sequence BIGINT NOT NULL DEFAULT 0;
                CREATE TRIGGER newest_message_first BEFORE INSERT OR UPDATE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_order_message_change();
                UPDATE {table} SET message_created_at=message_created_at
                    {"WHERE status IN ('new','waiting','retry','error') AND content->>'kind' IN ('update_message_flags','update_reaction')" if table == "bridge_zulip_v5_tasks" else ""};
                {index}
            """)
        session.execute("""
            CREATE INDEX bridge_zulip_v5_tasks_message_order_idx
                ON bridge_zulip_v5_tasks(priority DESC,message_created_at DESC,message_sequence DESC,created_at,uuid)
                WHERE status IN ('new','waiting','retry');
            CREATE INDEX bridge_zulip_v5_flag_tasks_message_order_idx
                ON bridge_zulip_v5_tasks(project_id,external_account_uuid,message_created_at DESC,message_sequence DESC,flag_state_uuid)
                WHERE content->>'kind'='update_message_flags' AND status IN ('new','retry');
        """)

    def downgrade(self, session):
        for table in (
            "messenger_message_changes",
            "bridge_zulip_v5_message_changes",
            "bridge_zulip_v5_flag_changes",
            "bridge_zulip_v5_reaction_changes",
            "bridge_zulip_v5_tasks",
        ):
            session.execute(f"""
                DROP TRIGGER newest_message_first ON {table};
                ALTER TABLE {table} DROP COLUMN message_created_at, DROP COLUMN message_sequence;
            """)
        session.execute("""
            DROP FUNCTION bridge_zulip_v5_order_message_change();
            DROP FUNCTION bridge_zulip_v5_message_created_at(UUID,UUID);
            DROP TRIGGER bridge_zulip_v5_content_source_timestamp ON bridge_zulip_v5_messages;
            DROP FUNCTION bridge_zulip_v5_content_source_timestamp();
            ALTER TABLE bridge_zulip_v5_message_contents DROP COLUMN source_timestamp, DROP COLUMN source_message_id;
        """)


migration_step = MigrationStep()
