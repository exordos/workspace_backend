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


REMOTE_SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_remote_flags() RETURNS trigger AS $$
            DECLARE
                target_message_uuids UUID[];
                account_owner UUID;
                account_hostname TEXT;
            BEGIN
                {guard}
                IF NEW.message_id=0 THEN
                    INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                    SELECT DISTINCT target.project_id, target.state_uuid FROM bridge_zulip_v5_flag_targets AS target
                    JOIN messenger_user_message_states AS state
                      ON state.project_id=target.project_id AND state.uuid=target.state_uuid
                    WHERE target.external_account_uuid=NEW.external_account_uuid AND state.read_at IS NULL
                    {order}
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                ELSE
                    SELECT owner_user_uuid, regexp_replace(external_account_id, ':[^:]+$', '')
                    INTO account_owner, account_hostname FROM m_external_accounts_v2
                    WHERE uuid=NEW.external_account_uuid;
                    SELECT array_agg(message_uuid) INTO target_message_uuids FROM (
                        SELECT messenger_uuid_v5(source.project_id, source.message_uuid::text) AS message_uuid
                        FROM bridge_zulip_v5_messages AS source
                        WHERE source.external_account_uuid=NEW.external_account_uuid AND source.message_id=NEW.message_id
                        UNION
                        SELECT delivery.message_uuid FROM bridge_zulip_v5_deliveries AS delivery
                        WHERE delivery.project_id=NEW.project_id AND delivery.zulip_message_id=NEW.message_id
                          AND delivery.hostname=account_hostname
                    ) AS messages;
                    INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                    SELECT state.project_id, state.uuid
                    FROM messenger_user_message_states AS state
                    JOIN messenger_message_placements AS placement
                      ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                    WHERE state.project_id=NEW.project_id AND state.user_uuid=account_owner
                      AND placement.message_uuid=ANY(target_message_uuids)
                    {order}
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                END IF;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
"""

DELIVERY_SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_delivery_flags() RETURNS trigger AS $$
            BEGIN
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT DISTINCT project_id, state_uuid FROM bridge_zulip_v5_flag_targets
                WHERE project_id=NEW.project_id AND message_uuid=NEW.message_uuid
                {order}
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
"""

ACCOUNT_SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_account_flags() RETURNS trigger AS $$
            BEGIN
                IF (OLD.provider, OLD.external_account_id, OLD.settings->>'server_url')
                    IS DISTINCT FROM (NEW.provider, NEW.external_account_id, NEW.settings->>'server_url') THEN
                    DELETE FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=NEW.uuid;
                    RETURN NULL;
                END IF;
                INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
                SELECT DISTINCT project_id, state_uuid FROM bridge_zulip_v5_flag_states
                WHERE external_account_uuid=NEW.uuid
                  AND local_updated_at IS NOT NULL
                  AND local_value IS DISTINCT FROM remote_value
                {order}
                    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0256-Avoid-full-message-repairs-for-owned-personal-flag-updates-1ca744.py"
        ]

    @property
    def migration_id(self):
        return "c445d0c9-08ba-49cc-b755-dc60d2c615ee"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            REMOTE_SQL.format(
                guard="IF current_setting('workspace.batch_flags',true)='on' THEN RETURN NULL; END IF;",
                order="ORDER BY 1,2",
            )
        )
        session.execute(DELIVERY_SQL.format(order="ORDER BY 1,2"))
        session.execute(ACCOUNT_SQL.format(order="ORDER BY 1,2"))

    def downgrade(self, session):
        session.execute(REMOTE_SQL.format(guard="", order=""))
        session.execute(DELIVERY_SQL.format(order=""))
        session.execute(ACCOUNT_SQL.format(order=""))


migration_step = MigrationStep()
