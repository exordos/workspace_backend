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


# Each fingerprint describes an observer's provider snapshot. Replaying the
# same shared source row for another observer does not change that snapshot.
# Real events outside the transaction-scoped replay marker retain the original
# invalidation and timestamp fences for every affected observer.
INVALIDATE_HISTORY_SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_invalidate_history(
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
"""

REPLAY_GUARD = "IF current_setting('workspace.zulip_history_batch',true)\n                    = account_id::text || ':' || ((source_id-1)/1000*1000+1)::text THEN"
SNAPSHOT_GUARD = (
    "IF COALESCE(current_setting('workspace.zulip_history_batch',true),'')<>'' THEN"
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0251-Add-Zulip-history-snapshot-fingerprints-fc9065.py"]

    @property
    def migration_id(self):
        return "04417c8e-21b1-4b79-85fb-c78baafdb7c5"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(INVALIDATE_HISTORY_SQL.replace(REPLAY_GUARD, SNAPSHOT_GUARD))

    def downgrade(self, session):
        session.execute(INVALIDATE_HISTORY_SQL)


migration_step = MigrationStep()
