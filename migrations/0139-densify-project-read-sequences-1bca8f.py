# Copyright 2026 Genesis Corporation.
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


READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"
PROJECT_SEQUENCE_RANGE_SIZE = 4_294_967_296

MESSAGE_INDEXES = (
    "m_workspace_messages_ingest_sequence_idx",
    "m_workspace_messages_project_ingest_sequence_idx",
    "m_workspace_messages_topic_ingest_sequence_idx",
    "m_workspace_messages_stream_read_page_idx",
    "m_workspace_messages_topic_read_page_idx",
    "m_workspace_messages_stream_ingest_sequence_idx",
)

CREATE_MESSAGE_INDEXES_SQL = """
CREATE UNIQUE INDEX "m_workspace_messages_ingest_sequence_idx"
    ON "m_workspace_messages" ("ingest_sequence");
CREATE INDEX "m_workspace_messages_project_ingest_sequence_idx"
    ON "m_workspace_messages" (
        "project_id", "ingest_sequence", "uuid"
    );
CREATE INDEX "m_workspace_messages_topic_ingest_sequence_idx"
    ON "m_workspace_messages" (
        "project_id", "topic_uuid", "ingest_sequence"
    );
CREATE INDEX "m_workspace_messages_stream_read_page_idx"
    ON "m_workspace_messages" (
        "project_id", "stream_uuid", "created_at", "uuid"
    ) INCLUDE ("topic_uuid", "ingest_sequence");
CREATE INDEX "m_workspace_messages_topic_read_page_idx"
    ON "m_workspace_messages" (
        "project_id", "stream_uuid", "topic_uuid", "created_at", "uuid"
    ) INCLUDE ("ingest_sequence");
CREATE INDEX "m_workspace_messages_stream_ingest_sequence_idx"
    ON "m_workspace_messages" (
        "project_id", "stream_uuid", "ingest_sequence"
    );
"""


def _drop_message_indexes(session):
    for index_name in MESSAGE_INDEXES:
        session.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def _require_legacy_read_authority(session):
    active = session.execute(
        """
        SELECT state.project_id
        FROM m_workspace_read_state_projects_v1 AS state
        WHERE state.mode <> 'legacy'
        UNION ALL
        SELECT progress.project_id
        FROM m_workspace_read_state_compaction_v1 AS progress
        LIMIT 1
        """
    ).fetchone()
    compact_rows = session.execute(
        """
        SELECT 1
        FROM m_workspace_user_read_chunks_v1
        UNION ALL
        SELECT 1
        FROM m_workspace_message_mentions_v1
        UNION ALL
        SELECT 1
        FROM m_workspace_user_topic_read_stats_v1
        UNION ALL
        SELECT 1
        FROM m_workspace_topic_message_stats_v1
        LIMIT 1
        """
    ).fetchone()
    snapshot = session.execute(
        """
        SELECT 1
        FROM m_external_provider_read_snapshots_v1
        LIMIT 1
        """
    ).fetchone()
    if active is not None or compact_rows is not None or snapshot is not None:
        raise RuntimeError(
            "Project-dense read sequence migration requires legacy read "
            "authority and drained provider read snapshots"
        )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0138-harden-lazy-provider-read-rolling-fences-1b0b01.py"]

    @property
    def migration_id(self):
        return "1bca8f2b-147f-4af8-b6e4-8078a3be253b"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute("LOCK TABLE m_workspace_messages IN ACCESS EXCLUSIVE MODE")
        _require_legacy_read_authority(session)
        session.execute(
            """
            ALTER TABLE m_workspace_messages
                ALTER COLUMN ingest_sequence DROP DEFAULT;
            DROP TRIGGER IF EXISTS m_workspace_assign_ingest_sequence_v1
                ON m_workspace_messages;
            DROP FUNCTION IF EXISTS m_workspace_assign_ingest_sequence_v1();

            CREATE SEQUENCE m_workspace_project_ingest_range_v2_seq
                AS BIGINT START WITH 1 MAXVALUE 2147483646;
            CREATE TABLE m_workspace_project_ingest_ranges_v2 (
                project_id UUID PRIMARY KEY,
                range_number BIGINT UNIQUE NOT NULL,
                next_local_sequence BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT m_workspace_project_ingest_ranges_number_check
                    CHECK (range_number BETWEEN 1 AND 2147483646),
                CONSTRAINT m_workspace_project_ingest_ranges_local_check
                    CHECK (next_local_sequence BETWEEN 0 AND 4294967295)
            );

            WITH project_counts AS (
                SELECT project_id, COUNT(*)::bigint AS message_count
                FROM m_workspace_messages
                GROUP BY project_id
            ), numbered AS (
                SELECT project_id, message_count,
                       row_number() OVER (ORDER BY project_id)::bigint
                           AS range_number
                FROM project_counts
            )
            INSERT INTO m_workspace_project_ingest_ranges_v2 (
                project_id, range_number, next_local_sequence
            )
            SELECT project_id, range_number, message_count
            FROM numbered;

            SELECT setval(
                'm_workspace_project_ingest_range_v2_seq',
                COALESCE(
                    (SELECT MAX(range_number)
                     FROM m_workspace_project_ingest_ranges_v2),
                    1
                ),
                EXISTS (SELECT 1 FROM m_workspace_project_ingest_ranges_v2)
            );
            """
        )
        _drop_message_indexes(session)
        session.execute(
            f"""
            WITH ranked AS MATERIALIZED (
                SELECT message.uuid,
                       range.range_number * {PROJECT_SEQUENCE_RANGE_SIZE}
                         + row_number() OVER (
                               PARTITION BY message.project_id
                               ORDER BY message.created_at, message.uuid
                           )::bigint AS ingest_sequence
                FROM m_workspace_messages AS message
                JOIN m_workspace_project_ingest_ranges_v2 AS range
                  ON range.project_id = message.project_id
            )
            UPDATE m_workspace_messages AS message
            SET ingest_sequence = ranked.ingest_sequence
            FROM ranked
            WHERE message.uuid = ranked.uuid;

            ALTER TABLE m_workspace_messages
                ALTER COLUMN ingest_sequence SET NOT NULL;

            CREATE FUNCTION m_workspace_assign_ingest_sequence_v1()
            RETURNS TRIGGER AS $$
            DECLARE
                allocated_sequence BIGINT;
            BEGIN
                IF NEW.ingest_sequence IS NOT NULL THEN
                    RETURN NEW;
                END IF;
                LOOP
                    UPDATE m_workspace_project_ingest_ranges_v2
                    SET next_local_sequence = next_local_sequence + 1,
                        updated_at = NOW()
                    WHERE project_id = NEW.project_id
                      AND next_local_sequence < 4294967295
                    RETURNING
                        range_number * {PROJECT_SEQUENCE_RANGE_SIZE}
                            + next_local_sequence
                    INTO allocated_sequence;
                    IF FOUND THEN
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    END IF;
                    IF EXISTS (
                        SELECT 1
                        FROM m_workspace_project_ingest_ranges_v2
                        WHERE project_id = NEW.project_id
                    ) THEN
                        RAISE EXCEPTION
                            'Workspace project message sequence is exhausted'
                            USING ERRCODE = '54000';
                    END IF;
                    BEGIN
                        INSERT INTO m_workspace_project_ingest_ranges_v2 (
                            project_id, range_number, next_local_sequence
                        ) VALUES (
                            NEW.project_id,
                            nextval(
                                'm_workspace_project_ingest_range_v2_seq'
                            ),
                            1
                        )
                        RETURNING
                            range_number * {PROJECT_SEQUENCE_RANGE_SIZE} + 1
                        INTO allocated_sequence;
                        NEW.ingest_sequence := allocated_sequence;
                        RETURN NEW;
                    EXCEPTION WHEN unique_violation THEN
                        -- A concurrent first insert created the project range.
                    END;
                END LOOP;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER m_workspace_assign_ingest_sequence_v1
            BEFORE INSERT ON m_workspace_messages
            FOR EACH ROW
            EXECUTE FUNCTION m_workspace_assign_ingest_sequence_v1();

            {CREATE_MESSAGE_INDEXES_SQL}
            """
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (READ_STATE_SCHEMA_LOCK_KEY,),
        )
        session.execute("LOCK TABLE m_workspace_messages IN ACCESS EXCLUSIVE MODE")
        _require_legacy_read_authority(session)
        _drop_message_indexes(session)
        session.execute(
            """
            DROP TRIGGER IF EXISTS m_workspace_assign_ingest_sequence_v1
                ON m_workspace_messages;
            DROP FUNCTION IF EXISTS m_workspace_assign_ingest_sequence_v1();
            ALTER TABLE m_workspace_messages
                ALTER COLUMN ingest_sequence DROP NOT NULL;

            WITH sequence_base AS (
                SELECT nextval(
                    'm_workspace_messages_ingest_sequence_v1_seq'
                ) AS first_sequence
            ), ranked AS MATERIALIZED (
                SELECT message.uuid,
                       sequence_base.first_sequence
                         + row_number() OVER (
                               ORDER BY message.created_at, message.uuid
                           )::bigint - 1 AS ingest_sequence
                FROM m_workspace_messages AS message
                CROSS JOIN sequence_base
            ), updated AS (
                UPDATE m_workspace_messages AS message
                SET ingest_sequence = ranked.ingest_sequence
                FROM ranked
                WHERE message.uuid = ranked.uuid
                RETURNING message.ingest_sequence
            )
            SELECT setval(
                'm_workspace_messages_ingest_sequence_v1_seq',
                COALESCE(MAX(ingest_sequence), 281474976710656),
                TRUE
            )
            FROM updated;

            ALTER TABLE m_workspace_messages
                ALTER COLUMN ingest_sequence SET DEFAULT nextval(
                    'm_workspace_messages_ingest_sequence_v1_seq'
                );

            CREATE FUNCTION m_workspace_assign_ingest_sequence_v1()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.ingest_sequence IS NULL THEN
                    NEW.ingest_sequence := nextval(
                        'm_workspace_messages_ingest_sequence_v1_seq'
                    );
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER m_workspace_assign_ingest_sequence_v1
            BEFORE INSERT ON m_workspace_messages
            FOR EACH ROW
            EXECUTE FUNCTION m_workspace_assign_ingest_sequence_v1();

            DROP TABLE m_workspace_project_ingest_ranges_v2;
            DROP SEQUENCE m_workspace_project_ingest_range_v2_seq;
            """
        )
        session.execute(CREATE_MESSAGE_INDEXES_SQL)


migration_step = MigrationStep()
