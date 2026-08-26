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
        self._depends = ["0136-add-lazy-provider-read-snapshots-e5b136.py"]

    @property
    def migration_id(self):
        return "dfc77921-c0d9-4d1e-b919-b360bc1f2b94"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE FUNCTION "m_external_provider_read_lease_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD."status" <> 'queued' OR NEW."status" <> 'leased' THEN
                    RETURN NEW;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" =
                                OLD."external_operation_uuid"
                   ) AND NOT EXISTS (
                        SELECT 1
                        FROM "m_external_bridge_instances_v2" AS bridge
                        WHERE bridge."uuid" = OLD."bridge_instance_uuid"
                          AND CASE
                                WHEN jsonb_typeof(
                                    bridge."capabilities"
                                        ->'messenger.message.read'->'revision'
                                ) = 'number'
                                THEN (
                                    bridge."capabilities"
                                        ->'messenger.message.read'->>'revision'
                                )::integer >= 2
                                ELSE FALSE
                              END
                   ) THEN
                    -- A revision-one bridge identifies every lazy page by
                    -- the shared public operation UUID. Keep pages queued
                    -- until a bridge that supports page identity is current.
                    RETURN NULL;
                END IF;
                IF EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."bridge_instance_uuid" =
                                OLD."bridge_instance_uuid"
                          AND snapshot."external_account_uuid" =
                                OLD."external_account_uuid"
                          AND snapshot."queue_sequence" < OLD."sequence"
                          AND (
                                OLD."causal_lane" IS NULL
                                OR snapshot."causal_lane" = OLD."causal_lane"
                          )
                          AND snapshot."external_operation_uuid" <>
                                OLD."external_operation_uuid"
                   ) THEN
                    -- Old application processes do not know the lazy lane
                    -- barrier. Suppress only the unsafe lease transition.
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER "m_external_provider_read_lease_fence_v1"
            BEFORE UPDATE OF "status" ON "m_external_provider_operations_v1"
            FOR EACH ROW
            EXECUTE FUNCTION "m_external_provider_read_lease_fence_v1"();
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            DO $$
            DECLARE
                attempt INTEGER := 0;
                previous_lock_timeout TEXT := current_setting('lock_timeout');
            BEGIN
                LOOP
                    attempt := attempt + 1;
                    BEGIN
                        -- Old rolling workers can hold the provider table and
                        -- request the schema gate only when publishing their
                        -- result. A short DDL timeout releases this attempt's
                        -- schema gate, lets that worker finish, and retries the
                        -- whole fenced check without a lock-order cycle.
                        PERFORM set_config('lock_timeout', '250ms', TRUE);
                        PERFORM pg_advisory_xact_lock(
                            hashtextextended(
                                'workspace-read-state-schema-v1', 0
                            )
                        );
                        LOCK TABLE m_external_provider_read_snapshots_v1
                            IN SHARE MODE;
                        IF EXISTS (
                            SELECT 1
                            FROM m_external_provider_read_snapshots_v1
                        ) THEN
                            RAISE EXCEPTION
                                'Provider read lease fence downgrade requires active snapshots to be completed or discarded first'
                                USING ERRCODE = '55000';
                        END IF;
                        EXECUTE
                            'DROP TRIGGER IF EXISTS '
                            '"m_external_provider_read_lease_fence_v1" ON '
                            '"m_external_provider_operations_v1"';
                        EXIT;
                    EXCEPTION
                        WHEN lock_not_available
                            OR query_canceled
                            OR deadlock_detected THEN
                            IF attempt >= 120 THEN
                                RAISE;
                            END IF;
                            PERFORM pg_sleep(0.05);
                    END;
                END LOOP;
                PERFORM set_config(
                    'lock_timeout', previous_lock_timeout, TRUE
                );
            END;
            $$;

            DROP FUNCTION IF EXISTS "m_external_provider_read_lease_fence_v1"();
            """
        )


migration_step = MigrationStep()
