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

import logging

from restalchemy.storage.sql import migrations


SCHEMA_SQL = """
ALTER TABLE messenger_user_message_states
    ADD COLUMN IF NOT EXISTS preview_stream_uuid uuid,
    ADD COLUMN IF NOT EXISTS preview_topic_uuid uuid,
    ADD COLUMN IF NOT EXISTS preview_created_at timestamp without time zone;

-- Keep point lookups indexed even when a bulk import starts with empty tables.
CREATE OR REPLACE FUNCTION messenger_set_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_seqscan=off AS $$
BEGIN
    IF TG_OP='UPDATE' AND (NEW.project_id,NEW.placement_uuid)
        IS NOT DISTINCT FROM (OLD.project_id,OLD.placement_uuid) THEN
        RETURN NEW;
    END IF;
    SELECT placement.stream_uuid,placement.topic_uuid,message.created_at
    INTO NEW.preview_stream_uuid,NEW.preview_topic_uuid,NEW.preview_created_at
    FROM messenger_message_placements AS placement
    CROSS JOIN LATERAL (
        SELECT message.created_at FROM messenger_messages AS message
        WHERE message.project_id=placement.project_id AND message.uuid=placement.message_uuid
        LIMIT 1 FOR SHARE
    ) AS message
    WHERE placement.project_id=NEW.project_id AND placement.uuid=NEW.placement_uuid
    FOR SHARE OF placement;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS messenger_set_personal_preview_scope ON messenger_user_message_states;
CREATE TRIGGER messenger_set_personal_preview_scope
BEFORE INSERT OR UPDATE OF project_id,placement_uuid ON messenger_user_message_states
FOR EACH ROW EXECUTE FUNCTION messenger_set_personal_preview_scope();

CREATE OR REPLACE FUNCTION messenger_move_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_seqscan=off AS $$
DECLARE canonical_created_at timestamp without time zone;
BEGIN
    IF TG_OP='UPDATE' AND (NEW.stream_uuid,NEW.topic_uuid,NEW.message_uuid)
        IS NOT DISTINCT FROM (OLD.stream_uuid,OLD.topic_uuid,OLD.message_uuid) THEN
        RETURN NULL;
    END IF;
    IF TG_OP='UPDATE' AND NEW.message_uuid=OLD.message_uuid THEN
        UPDATE messenger_user_message_states
        SET preview_stream_uuid=NEW.stream_uuid,preview_topic_uuid=NEW.topic_uuid
        WHERE project_id=NEW.project_id AND placement_uuid=NEW.uuid
          AND (preview_stream_uuid,preview_topic_uuid)
              IS DISTINCT FROM (NEW.stream_uuid,NEW.topic_uuid);
        RETURN NULL;
    END IF;
    SELECT created_at INTO canonical_created_at FROM messenger_messages
    WHERE project_id=NEW.project_id AND uuid=NEW.message_uuid FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE messenger_user_message_states
    SET preview_stream_uuid=NEW.stream_uuid,preview_topic_uuid=NEW.topic_uuid,
        preview_created_at=canonical_created_at
    WHERE project_id=NEW.project_id AND placement_uuid=NEW.uuid
      AND (preview_stream_uuid,preview_topic_uuid,preview_created_at)
          IS DISTINCT FROM (NEW.stream_uuid,NEW.topic_uuid,canonical_created_at);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_move_personal_preview_scope ON messenger_message_placements;
CREATE TRIGGER messenger_move_personal_preview_scope
AFTER INSERT OR UPDATE OF stream_uuid,topic_uuid,message_uuid ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_move_personal_preview_scope();

CREATE OR REPLACE FUNCTION messenger_date_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_seqscan=off AS $$
DECLARE placement record;
BEGIN
    IF TG_OP='UPDATE' AND NEW.created_at IS NOT DISTINCT FROM OLD.created_at THEN
        RETURN NULL;
    END IF;
    FOR placement IN SELECT uuid,stream_uuid,topic_uuid
        FROM messenger_message_placements
        WHERE project_id=NEW.project_id AND message_uuid=NEW.uuid
    LOOP
        IF TG_OP='UPDATE' THEN
            UPDATE messenger_user_message_states
            SET preview_created_at=NEW.created_at
            WHERE project_id=NEW.project_id AND placement_uuid=placement.uuid
              AND preview_created_at IS DISTINCT FROM NEW.created_at;
        ELSE
            UPDATE messenger_user_message_states
            SET preview_created_at=NEW.created_at,
                preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid
            WHERE project_id=NEW.project_id AND placement_uuid=placement.uuid
              AND (preview_stream_uuid,preview_topic_uuid,preview_created_at)
                  IS DISTINCT FROM (placement.stream_uuid,placement.topic_uuid,NEW.created_at);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_date_personal_preview_scope ON messenger_messages;
CREATE TRIGGER messenger_date_personal_preview_scope
AFTER INSERT OR UPDATE OF created_at ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_date_personal_preview_scope();
"""

LOG = logging.getLogger(__name__)
BATCH_SIZE = 10000
INDEXES = tuple(
    (
        f"messenger_user_message_states_{scope}_preview_idx",
        f"""CREATE INDEX CONCURRENTLY IF NOT EXISTS
            messenger_user_message_states_{scope}_preview_idx
            ON messenger_user_message_states
               (project_id,user_uuid,preview_{scope}_uuid,membership_generation,
                preview_created_at DESC,placement_uuid DESC)""",
    )
    for scope in ("stream", "topic")
)


def _backfill(session):
    # A restart scans the stable key again and skips already repaired rows.
    # Commit each batch so history ingestion and other recipients can proceed.
    while True:
        cursor = None
        repaired = 0
        while True:
            after = (
                ""
                if cursor is None
                else "WHERE (project_id,uuid) > (%s::uuid,%s::uuid)"
            )
            result = session.execute(
                f"""WITH batch AS MATERIALIZED (
                    SELECT project_id,uuid,placement_uuid,ctid
                    FROM messenger_user_message_states
                    {after} ORDER BY project_id,uuid LIMIT {BATCH_SIZE} FOR UPDATE
                ), source AS MATERIALIZED (
                    SELECT batch.project_id,batch.uuid,batch.ctid,placement.stream_uuid,
                           placement.topic_uuid,message.created_at
                    FROM batch
                    CROSS JOIN LATERAL (
                        SELECT stream_uuid,topic_uuid,message_uuid
                        FROM messenger_message_placements
                        WHERE project_id=batch.project_id AND uuid=batch.placement_uuid LIMIT 1
                    ) AS placement
                    CROSS JOIN LATERAL (
                        SELECT created_at FROM messenger_messages
                        WHERE project_id=batch.project_id AND uuid=placement.message_uuid LIMIT 1
                    ) AS message
                ), repaired AS (
                    UPDATE messenger_user_message_states AS personal
                    SET preview_stream_uuid=source.stream_uuid,preview_topic_uuid=source.topic_uuid,
                        preview_created_at=source.created_at
                    FROM source WHERE personal.ctid=source.ctid
                      AND (personal.preview_stream_uuid,personal.preview_topic_uuid,personal.preview_created_at)
                          IS DISTINCT FROM (source.stream_uuid,source.topic_uuid,source.created_at)
                    RETURNING 1
                ) SELECT (SELECT count(*) FROM repaired) AS repaired,
                    (SELECT project_id FROM batch ORDER BY project_id DESC,uuid DESC LIMIT 1) AS project_id,
                    (SELECT uuid FROM batch ORDER BY project_id DESC,uuid DESC LIMIT 1) AS uuid""",
                () if cursor is None else cursor,
            ).fetchone()
            session.commit()
            repaired += result["repaired"]
            if result["uuid"] is None:
                break
            cursor = (result["project_id"], result["uuid"])
            LOG.info("Personal preview backfill: %s rows repaired", repaired)
        # Parent changes are atomic, but a concurrent move/date update can race
        # a backfill snapshot. Another pass repairs any such stale overwrite.
        if repaired == 0:
            return


def _indexes(session, *, create):
    # Match the established online-index migration path. Interrupted concurrent
    # builds are retried after removing only this migration's invalid indexes.
    session.commit()
    connection = session.engine.get_connection()
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for name, statement in INDEXES:
                if create:
                    cursor.execute(
                        "SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)",
                        (name,),
                    )
                    row = cursor.fetchone()
                    if row is not None and not row[0]:
                        cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
                    cursor.execute(statement)
                else:
                    cursor.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0279-Restore-migrated-Zulip-inbox-message-ordering-keys-7f3b75.py"
        ]

    @property
    def migration_id(self):
        return "6025ad64-2ebc-4796-9690-cd3be5497fdd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("SET LOCAL lock_timeout='3s'")
        session.execute(SCHEMA_SQL)
        session.commit()
        _backfill(session)
        _indexes(session, create=True)

    def downgrade(self, session):
        _indexes(session, create=False)
        session.execute("""
            DROP TRIGGER messenger_date_personal_preview_scope ON messenger_messages;
            DROP TRIGGER messenger_move_personal_preview_scope ON messenger_message_placements;
            DROP TRIGGER messenger_set_personal_preview_scope ON messenger_user_message_states;
            DROP FUNCTION messenger_date_personal_preview_scope();
            DROP FUNCTION messenger_move_personal_preview_scope();
            DROP FUNCTION messenger_set_personal_preview_scope();
            ALTER TABLE messenger_user_message_states
                DROP COLUMN preview_stream_uuid,DROP COLUMN preview_topic_uuid,
                DROP COLUMN preview_created_at;
        """)


migration_step = MigrationStep()
