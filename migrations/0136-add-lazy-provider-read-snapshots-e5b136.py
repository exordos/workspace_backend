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
        self._depends = ["0135-add-resumable-compact-unread-indexes-b46965.py"]

    @property
    def migration_id(self):
        return "e5b13624-7b61-4623-9081-61a2e51afd92"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            CREATE TABLE "m_external_provider_read_snapshots_v1" (
                "external_operation_uuid" UUID PRIMARY KEY,
                "bridge_instance_uuid" UUID NOT NULL,
                "external_account_uuid" UUID NOT NULL,
                "project_id" UUID NOT NULL,
                "causal_lane" UUID NOT NULL,
                "queue_sequence" BIGINT UNIQUE NOT NULL DEFAULT nextval(
                    'm_external_provider_operations_v1_sequence_seq'
                ),
                "payload" JSONB NOT NULL,
                "exhausted" BOOLEAN NOT NULL DEFAULT FALSE,
                "created_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                "updated_at" TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT "m_external_provider_read_snapshots_operation_fkey"
                    FOREIGN KEY ("external_operation_uuid")
                    REFERENCES "m_external_operations_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_read_snapshots_bridge_fkey"
                    FOREIGN KEY ("bridge_instance_uuid")
                    REFERENCES "m_external_bridge_instances_v2" ("uuid"),
                CONSTRAINT "m_external_provider_read_snapshots_account_fkey"
                    FOREIGN KEY ("external_account_uuid")
                    REFERENCES "m_external_accounts_v2" ("uuid")
                    ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_read_snapshots_lane_check"
                    CHECK ("causal_lane" <> '00000000-0000-0000-0000-000000000000')
            );
            CREATE INDEX "m_external_provider_read_snapshots_bridge_idx"
                ON "m_external_provider_read_snapshots_v1" (
                    "bridge_instance_uuid", "updated_at", "queue_sequence",
                    "external_operation_uuid"
                )
                WHERE "exhausted" = FALSE;
            CREATE INDEX "m_external_provider_read_snapshots_account_queue_idx"
                ON "m_external_provider_read_snapshots_v1" (
                    "external_account_uuid", "causal_lane", "queue_sequence"
                );

            CREATE TABLE "m_external_provider_read_candidate_packs_v1" (
                "external_operation_uuid" UUID NOT NULL,
                "pack_number" BIGINT NOT NULL,
                "candidate_count" INTEGER NOT NULL,
                "cursor_position" INTEGER NOT NULL DEFAULT 0,
                "candidate_uuids" UUID[] NOT NULL,
                PRIMARY KEY ("external_operation_uuid", "pack_number"),
                CONSTRAINT "m_external_provider_read_candidate_packs_fkey"
                    FOREIGN KEY ("external_operation_uuid")
                    REFERENCES "m_external_provider_read_snapshots_v1" (
                        "external_operation_uuid"
                    ) ON DELETE CASCADE,
                CONSTRAINT "m_external_provider_read_candidate_packs_number_check"
                    CHECK ("pack_number" >= 0),
                CONSTRAINT "m_external_provider_read_candidate_packs_count_check"
                    CHECK ("candidate_count" BETWEEN 1 AND 4000),
                CONSTRAINT "m_external_provider_read_candidate_packs_cursor_check"
                    CHECK (
                        "cursor_position" BETWEEN 0 AND "candidate_count" - 1
                    ),
                CONSTRAINT "m_external_provider_read_candidate_packs_array_check"
                    CHECK (
                        cardinality("candidate_uuids") = "candidate_count"
                    )
            );

            ALTER TABLE "m_external_provider_operations_v1"
                ADD COLUMN "causal_lane" UUID;

            CREATE FUNCTION "m_external_provider_operation_lane_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."causal_lane" IS NULL THEN
                    IF NEW."payload" ? 'stream_uuid' THEN
                        NEW."causal_lane" :=
                            (NEW."payload"->>'stream_uuid')::uuid;
                    ELSIF NEW."operation_kind" LIKE 'stream.%'
                          AND NEW."payload" ? 'uuid' THEN
                        NEW."causal_lane" := (NEW."payload"->>'uuid')::uuid;
                    ELSIF NEW."operation_kind" LIKE 'reaction.%'
                          AND NEW."payload" ? 'message_uuid' THEN
                        SELECT message."stream_uuid"
                        INTO NEW."causal_lane"
                        FROM "m_workspace_messages" AS message
                        WHERE message."uuid" =
                            (NEW."payload"->>'message_uuid')::uuid
                          AND message."project_id" = NEW."project_id";
                    END IF;
                END IF;
                IF NEW."causal_lane" IS NOT NULL THEN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended(
                            'provider-causal-lane-v1:'
                            || NEW."bridge_instance_uuid"::text || ':'
                            || NEW."external_account_uuid"::text || ':'
                            || NEW."causal_lane"::text,
                            0
                        )
                    );
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER "m_external_provider_operation_lane_v1"
            BEFORE INSERT ON "m_external_provider_operations_v1"
            FOR EACH ROW
            EXECUTE FUNCTION "m_external_provider_operation_lane_v1"();

            CREATE FUNCTION "m_external_provider_read_payload_scrub_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."operation_kind" = 'read_state.set'
                   AND NEW."status" = 'succeeded'
                   AND NEW."payload" ? 'message_uuids' THEN
                    -- Old application processes retain the page payload when
                    -- they report success. Keep rolling upgrades bounded even
                    -- when those processes finish lazy pages.
                    NEW."payload" := jsonb_set(
                        NEW."payload", '{message_uuids}', '[]'::jsonb
                    );
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER "m_external_provider_read_payload_scrub_v1"
            BEFORE UPDATE OF "status" ON "m_external_provider_operations_v1"
            FOR EACH ROW
            EXECUTE FUNCTION "m_external_provider_read_payload_scrub_v1"();

            CREATE FUNCTION "m_external_provider_read_completion_fence_v1"()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status"
                   AND EXISTS (
                        SELECT 1
                        FROM "m_external_provider_read_snapshots_v1" AS snapshot
                        WHERE snapshot."external_operation_uuid" = NEW."uuid"
                          AND snapshot."exhausted" = FALSE
                   ) THEN
                    -- An old application process can finalize only the
                    -- currently visible page window. Keep the public
                    -- operation running while committing that exact result.
                    RETURN NULL;
                END IF;
                IF NEW."status" = 'succeeded'
                   AND OLD."status" IS DISTINCT FROM NEW."status" THEN
                    -- Old processes do not know the snapshot table. Retire an
                    -- exhausted header here so its lane barrier cannot leak.
                    DELETE FROM "m_external_provider_read_snapshots_v1"
                    WHERE "external_operation_uuid" = NEW."uuid"
                      AND "exhausted" = TRUE;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER "m_external_provider_read_completion_fence_v1"
            BEFORE UPDATE OF "status" ON "m_external_operations_v2"
            FOR EACH ROW
            EXECUTE FUNCTION "m_external_provider_read_completion_fence_v1"();
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
                        PERFORM set_config('lock_timeout', '250ms', TRUE);
                        PERFORM pg_advisory_xact_lock(
                            hashtextextended(
                                'workspace-read-state-schema-v1', 0
                            )
                        );
                        LOCK TABLE m_external_provider_read_snapshots_v1
                            IN ACCESS EXCLUSIVE MODE;
                        IF EXISTS (
                            SELECT 1
                            FROM m_external_provider_read_snapshots_v1
                        ) THEN
                            RAISE EXCEPTION
                                'Lazy provider read snapshot downgrade requires active snapshots to be completed or discarded first'
                                USING ERRCODE = '55000';
                        END IF;
                        EXECUTE
                            'DROP TRIGGER IF EXISTS '
                            '"m_external_provider_read_completion_fence_v1" '
                            'ON "m_external_operations_v2"';
                        EXECUTE
                            'DROP FUNCTION IF EXISTS '
                            '"m_external_provider_read_completion_fence_v1"()';
                        EXECUTE
                            'DROP TRIGGER IF EXISTS '
                            '"m_external_provider_read_payload_scrub_v1" '
                            'ON "m_external_provider_operations_v1"';
                        EXECUTE
                            'DROP FUNCTION IF EXISTS '
                            '"m_external_provider_read_payload_scrub_v1"()';
                        EXECUTE
                            'DROP TRIGGER IF EXISTS '
                            '"m_external_provider_operation_lane_v1" '
                            'ON "m_external_provider_operations_v1"';
                        EXECUTE
                            'DROP FUNCTION IF EXISTS '
                            '"m_external_provider_operation_lane_v1"()';
                        EXECUTE
                            'DROP TABLE '
                            '"m_external_provider_read_candidate_packs_v1"';
                        EXECUTE
                            'DROP TABLE '
                            '"m_external_provider_read_snapshots_v1"';
                        EXECUTE
                            'ALTER TABLE '
                            '"m_external_provider_operations_v1" '
                            'DROP COLUMN "causal_lane"';
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
            """
        )


migration_step = MigrationStep()
