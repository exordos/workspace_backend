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


FLAG_STATES_SQL = """
CREATE OR REPLACE VIEW bridge_zulip_v5_flag_states AS
SELECT target.*, local.flag, local.value AS local_value,
       local.updated_at AS local_updated_at,
       remote.value AS remote_value, remote.updated_at AS remote_updated_at
FROM bridge_zulip_v5_flag_targets AS target
JOIN messenger_user_message_states AS state
  ON state.project_id=target.project_id AND state.uuid=target.state_uuid
CROSS JOIN LATERAL (VALUES
    ('read', state.read_at IS NOT NULL, state.read_updated_at),
    ('starred', state.starred, state.starred_updated_at)
) AS local(flag, value, updated_at)
LEFT JOIN LATERAL (
    SELECT observed.value, observed.updated_at
    FROM bridge_zulip_v5_message_flags AS observed
    WHERE observed.external_account_uuid=target.external_account_uuid
      AND observed.flag=local.flag AND observed.message_id IN (0, target.message_id)
      %(barrier)s
    ORDER BY observed.updated_at DESC, observed.event_id DESC LIMIT 1
) AS remote ON true
WHERE local.value IS DISTINCT FROM remote.value
   OR local.updated_at IS NULL OR remote.updated_at > local.updated_at;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0250-Index-unread-Messenger-mentions-by-viewer-40a61a.py"]

    @property
    def migration_id(self):
        return "fc906584-1ab1-488a-8532-104e70ca5c21"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TABLE bridge_zulip_v5_history_batches (
                external_account_uuid UUID NOT NULL,
                project_id UUID NOT NULL,
                hostname TEXT NOT NULL,
                first_id BIGINT NOT NULL CHECK (first_id > 0 AND (first_id-1) % 1000=0),
                covered_to BIGINT NOT NULL,
                source_revision TEXT NOT NULL,
                hashes JSONB NOT NULL DEFAULT '{}',
                record_count INTEGER NOT NULL DEFAULT 0,
                revision BIGINT NOT NULL DEFAULT 1,
                ready BOOLEAN NOT NULL DEFAULT false,
                pending UUID[] NOT NULL DEFAULT '{}',
                observed_at TIMESTAMP WITHOUT TIME ZONE,
                invalidated_at TIMESTAMP WITHOUT TIME ZONE,
                PRIMARY KEY (external_account_uuid, first_id),
                FOREIGN KEY (external_account_uuid, project_id)
                    REFERENCES m_external_accounts_v2(uuid, project_id) ON DELETE CASCADE,
                CHECK (covered_to BETWEEN first_id AND first_id+999)
            );
            CREATE INDEX bridge_zulip_v5_history_batches_source_idx
                ON bridge_zulip_v5_history_batches(project_id, hostname, first_id);
            CREATE TABLE bridge_zulip_v5_message_fingerprints (
                external_account_uuid UUID NOT NULL,
                message_id BIGINT NOT NULL CHECK (message_id > 0),
                first_id BIGINT GENERATED ALWAYS AS ((message_id-1)/1000*1000+1) STORED,
                hashes JSONB NOT NULL,
                PRIMARY KEY (external_account_uuid, message_id),
                FOREIGN KEY (external_account_uuid, first_id)
                    REFERENCES bridge_zulip_v5_history_batches(external_account_uuid, first_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX bridge_zulip_v5_message_fingerprints_batch_idx
                ON bridge_zulip_v5_message_fingerprints(external_account_uuid,first_id);
            CREATE FUNCTION bridge_zulip_v5_invalidate_history(
                account_id UUID, source_id BIGINT, components TEXT[]
            ) RETURNS void LANGUAGE plpgsql AS $$
            DECLARE component TEXT;
            BEGIN
                IF current_setting('workspace.zulip_history_batch',true)
                    = account_id::text || ':' || ((source_id-1)/1000*1000+1)::text THEN
                    RETURN;
                END IF;
                UPDATE bridge_zulip_v5_history_batches
                SET ready=false, revision=revision+1,
                    invalidated_at=clock_timestamp() AT TIME ZONE 'UTC'
                WHERE external_account_uuid=account_id
                  AND (source_id=0 OR first_id=(source_id-1)/1000*1000+1);
                -- Cascading account deletion can remove the parent batch first.
                IF NOT FOUND THEN RETURN; END IF;
                FOREACH component IN ARRAY components LOOP
                    UPDATE bridge_zulip_v5_message_fingerprints
                    SET hashes=hashes || jsonb_build_object(component,NULL)
                    WHERE external_account_uuid=account_id
                      AND (source_id=0 OR message_id=source_id);
                END LOOP;
            END;
            $$;
            CREATE FUNCTION bridge_zulip_v5_track_history_source() RETURNS trigger
            LANGUAGE plpgsql AS $$
            DECLARE item RECORD; source JSONB; previous JSONB; components TEXT[];
            BEGIN
                source := CASE WHEN TG_OP='DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
                previous := CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) ELSE '{}'::jsonb END;
                IF TG_TABLE_NAME='bridge_zulip_v5_message_contents' THEN
                    IF TG_OP='UPDATE' AND source->'zulip_content_hash'=previous->'zulip_content_hash'
                        AND source->'zulip_content_updated_at'=previous->'zulip_content_updated_at' THEN
                        RETURN NULL;
                    END IF;
                    FOR item IN SELECT external_account_uuid, message_id FROM bridge_zulip_v5_messages
                        WHERE content=(source->>'uuid')::uuid LOOP
                        components := ARRAY['metadata'];
                        IF source->'zulip_content_hash' IS DISTINCT FROM previous->'zulip_content_hash' THEN
                            components := components || ARRAY['text'];
                        END IF;
                        PERFORM bridge_zulip_v5_invalidate_history(item.external_account_uuid,
                            item.message_id,components);
                    END LOOP;
                ELSIF TG_TABLE_NAME='bridge_zulip_v5_reactions' THEN
                    components := ARRAY['reactions'];
                    IF TG_OP='UPDATE' AND source->'present'=previous->'present'
                        AND source->'emoji_name'=previous->'emoji_name' THEN components := '{}'; END IF;
                    FOR item IN SELECT batch.external_account_uuid, remote.message_id
                        FROM bridge_zulip_v5_reaction_messages AS remote
                        JOIN bridge_zulip_v5_history_batches AS batch
                          ON batch.project_id=remote.project_id AND batch.hostname=remote.hostname
                         AND batch.first_id=(remote.message_id-1)/1000*1000+1
                        WHERE remote.uuid=(source->>'reaction_message_uuid')::uuid LOOP
                        PERFORM bridge_zulip_v5_invalidate_history(item.external_account_uuid,
                            item.message_id,components);
                    END LOOP;
                ELSIF TG_TABLE_NAME='bridge_zulip_v5_channels' THEN
                    IF source->>'channel_type'<>'channel' THEN RETURN NULL; END IF;
                    IF TG_OP='UPDATE' AND source-ARRAY['updated_at','created_at']
                        =previous-ARRAY['updated_at','created_at'] THEN RETURN NULL; END IF;
                    PERFORM bridge_zulip_v5_invalidate_history((source->>'external_account_uuid')::uuid,
                        0,ARRAY['text','flags','metadata','reactions']);
                ELSIF TG_TABLE_NAME='bridge_zulip_v5_deliveries' THEN
                    IF TG_OP='UPDATE' AND source->'observed_zulip_content'=previous->'observed_zulip_content'
                        AND source->'zulip_message_id'=previous->'zulip_message_id'
                        AND source->'content_observed_at'=previous->'content_observed_at'
                        AND source->'topic_name'=previous->'topic_name' THEN RETURN NULL; END IF;
                    components := ARRAY['metadata'];
                    IF source->'observed_zulip_content' IS DISTINCT FROM previous->'observed_zulip_content' THEN
                        components := components || ARRAY['text'];
                    END IF;
                    FOR item IN SELECT external_account_uuid FROM bridge_zulip_v5_history_batches
                        WHERE project_id=(source->>'project_id')::uuid AND hostname=source->>'hostname'
                          AND first_id=((source->>'zulip_message_id')::bigint-1)/1000*1000+1 LOOP
                        PERFORM bridge_zulip_v5_invalidate_history(item.external_account_uuid,
                            (source->>'zulip_message_id')::bigint,components);
                    END LOOP;
                ELSE
                    components := CASE WHEN TG_TABLE_NAME='bridge_zulip_v5_message_flags'
                        THEN ARRAY['flags'] ELSE ARRAY['text','flags','metadata','reactions'] END;
                    IF TG_OP='UPDATE' AND TG_TABLE_NAME='bridge_zulip_v5_message_flags'
                        AND source->'value'=previous->'value' THEN components := '{}'; END IF;
                    IF TG_OP='UPDATE' AND TG_TABLE_NAME='bridge_zulip_v5_messages'
                        AND source - ARRAY['updated_at','created_at'] = previous - ARRAY['updated_at','created_at']
                        THEN RETURN NULL; END IF;
                    IF TG_OP='UPDATE' AND TG_TABLE_NAME='bridge_zulip_v5_messages' THEN
                        components := '{}';
                        IF source - ARRAY['updated_at','created_at','read','starred','collapsed',
                            'hide_link_previews','mentioned','stream_wildcard_mentioned',
                            'topic_wildcard_mentioned','has_alert_word','historical']
                            IS DISTINCT FROM previous - ARRAY['updated_at','created_at','read','starred','collapsed',
                            'hide_link_previews','mentioned','stream_wildcard_mentioned',
                            'topic_wildcard_mentioned','has_alert_word','historical'] THEN
                            components := ARRAY['metadata'];
                            IF source->'content' IS DISTINCT FROM previous->'content' THEN
                                components := components || ARRAY['text'];
                            END IF;
                        END IF;
                        IF source - ARRAY['updated_at','created_at','message_uuid','message_id','sender_id',
                            'timestamp','placement_updated_at','content','topic','channel','is_me_message',
                            'uuid','project_id','external_account_uuid']
                            IS DISTINCT FROM previous - ARRAY['updated_at','created_at','message_uuid','message_id','sender_id',
                            'timestamp','placement_updated_at','content','topic','channel','is_me_message',
                            'uuid','project_id','external_account_uuid'] THEN
                            components := components || ARRAY['flags'];
                        END IF;
                    END IF;
                    PERFORM bridge_zulip_v5_invalidate_history((source->>'external_account_uuid')::uuid,
                        (source->>'message_id')::bigint,components);
                END IF;
                RETURN NULL;
            END;
            $$;
        """)
        for table in (
            "channels",
            "messages",
            "message_contents",
            "message_flags",
            "reactions",
            "deliveries",
        ):
            session.execute(f"""CREATE TRIGGER bridge_zulip_v5_track_history_source
                AFTER INSERT OR UPDATE OR DELETE ON bridge_zulip_v5_{table}
                FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_track_history_source()""")
        session.execute(
            FLAG_STATES_SQL
            % {
                "barrier": """
            AND (observed.message_id<>0 OR NOT EXISTS (
                SELECT 1 FROM bridge_zulip_v5_history_batches AS batch
                WHERE batch.external_account_uuid=target.external_account_uuid
                  AND batch.first_id=(target.message_id-1)/1000*1000+1
                  AND batch.covered_to>=target.message_id
                  AND batch.observed_at>=observed.updated_at))
        """
            }
        )

    def downgrade(self, session):
        session.execute(FLAG_STATES_SQL % {"barrier": ""})
        # The previous worker can resume remaining ranges using its original
        # backfill protocol, and can deserialize archived completed tasks.
        session.execute("""
            UPDATE bridge_zulip_v5_tasks SET content=jsonb_build_object(
                'kind','full_backfill','last_message_id',(content->>'last_message_id')::bigint,
                'since',content->'since') WHERE content->>'kind'='scan_history';
            UPDATE bridge_zulip_v5_tasks SET content=content-ARRAY[
                'snapshot','snapshot_at','account_revision','batch_first_id','batch_revision']
                WHERE content->>'kind'='import_history';
        """)
        for table in (
            "channels",
            "messages",
            "message_contents",
            "message_flags",
            "reactions",
            "deliveries",
        ):
            session.execute(
                f"DROP TRIGGER IF EXISTS bridge_zulip_v5_track_history_source ON bridge_zulip_v5_{table}"
            )
        session.execute("""
            DROP FUNCTION bridge_zulip_v5_track_history_source();
            DROP FUNCTION bridge_zulip_v5_invalidate_history(UUID,BIGINT,TEXT[]);
            DROP TABLE bridge_zulip_v5_message_fingerprints;
            DROP TABLE bridge_zulip_v5_history_batches;
        """)


migration_step = MigrationStep()
