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


def _lease_fence(page_aware: bool) -> str:
    sequence_limit = (
        """
        COALESCE(
            (
                SELECT page_snapshot.queue_sequence
                FROM m_external_provider_read_snapshots_v1 AS page_snapshot
                WHERE page_snapshot.external_operation_uuid =
                        OLD.external_operation_uuid
            ),
            OLD.sequence
        )
        """
        if page_aware
        else "OLD.sequence"
    )
    return f"""
        CREATE OR REPLACE FUNCTION
            m_external_provider_read_lease_fence_v1()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.status <> 'queued' OR NEW.status <> 'leased' THEN
                RETURN NEW;
            END IF;
            IF EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_snapshots_v1 AS snapshot
                    WHERE snapshot.external_operation_uuid =
                            OLD.external_operation_uuid
               ) AND (
                    COALESCE(
                        current_setting(
                            'workspace.provider_read_snapshot_lease_v2',
                            TRUE
                        ),
                        ''
                    ) <> 'on'
                    OR NOT EXISTS (
                        SELECT 1
                        FROM m_external_bridge_instances_v2 AS bridge
                        WHERE bridge.uuid = OLD.bridge_instance_uuid
                          AND CASE
                                WHEN jsonb_typeof(
                                    bridge.capabilities
                                        ->'messenger.message.read.paging'
                                        ->'revision'
                                ) = 'number'
                                THEN (
                                    bridge.capabilities
                                        ->'messenger.message.read.paging'
                                        ->>'revision'
                                )::integer >= 1
                                ELSE FALSE
                              END
                    )
               ) THEN
                RETURN NULL;
            END IF;
            IF EXISTS (
                    SELECT 1
                    FROM m_external_provider_read_snapshots_v1 AS snapshot
                    WHERE snapshot.bridge_instance_uuid =
                            OLD.bridge_instance_uuid
                      AND snapshot.external_account_uuid =
                            OLD.external_account_uuid
                      AND snapshot.queue_sequence < {sequence_limit}
                      AND (
                            OLD.causal_lane IS NULL
                            OR snapshot.causal_lane = OLD.causal_lane
                      )
                      AND snapshot.external_operation_uuid <>
                            OLD.external_operation_uuid
               ) THEN
                RETURN NULL;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0160-repair-native-read-state-and-prioritize-reads-259cc2.py"]

    @property
    def migration_id(self):
        return "d0643389-d3dd-410a-a57c-5c1fab7691df"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(_lease_fence(True))

    def downgrade(self, session):
        session.execute(_lease_fence(False))


migration_step = MigrationStep()
