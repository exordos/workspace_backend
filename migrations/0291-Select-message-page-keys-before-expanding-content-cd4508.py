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

# Reuse the established online preview-key mechanism on access bindings.
# These four UUID/timestamp keys have fixed size; content stays immutable in
# its existing table. The authoritative message/visibility view is unchanged.

from restalchemy.storage.sql import migrations


SCHEMA_SQL = """
ALTER TABLE messenger_user_message_bindings
    ADD COLUMN IF NOT EXISTS preview_message_uuid uuid,
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
    SELECT placement.stream_uuid,placement.topic_uuid,message.created_at,placement.message_uuid
    INTO NEW.preview_stream_uuid,NEW.preview_topic_uuid,NEW.preview_created_at,NEW.preview_message_uuid
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
DROP TRIGGER IF EXISTS messenger_set_personal_preview_scope ON messenger_user_message_bindings;
CREATE TRIGGER messenger_set_personal_preview_scope
BEFORE INSERT OR UPDATE OF project_id,placement_uuid ON messenger_user_message_bindings
FOR EACH ROW EXECUTE FUNCTION messenger_set_personal_preview_scope();

CREATE OR REPLACE FUNCTION messenger_move_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_seqscan=off AS $$
DECLARE canonical_created_at timestamp without time zone;
BEGIN
    IF (NEW.stream_uuid,NEW.topic_uuid,NEW.message_uuid)
        IS NOT DISTINCT FROM (OLD.stream_uuid,OLD.topic_uuid,OLD.message_uuid) THEN
        RETURN NULL;
    END IF;
    IF NEW.message_uuid=OLD.message_uuid THEN
        UPDATE messenger_user_message_bindings
        SET preview_stream_uuid=NEW.stream_uuid,preview_topic_uuid=NEW.topic_uuid
        WHERE project_id=NEW.project_id AND placement_uuid=NEW.uuid
          AND (preview_stream_uuid,preview_topic_uuid)
              IS DISTINCT FROM (NEW.stream_uuid,NEW.topic_uuid);
        RETURN NULL;
    END IF;
    SELECT created_at INTO canonical_created_at FROM messenger_messages
    WHERE project_id=NEW.project_id AND uuid=NEW.message_uuid FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE messenger_user_message_bindings
    SET preview_stream_uuid=NEW.stream_uuid,preview_topic_uuid=NEW.topic_uuid,
        preview_created_at=canonical_created_at,preview_message_uuid=NEW.message_uuid
    WHERE project_id=NEW.project_id AND placement_uuid=NEW.uuid
      AND (preview_stream_uuid,preview_topic_uuid,preview_created_at,preview_message_uuid)
          IS DISTINCT FROM (NEW.stream_uuid,NEW.topic_uuid,canonical_created_at,NEW.message_uuid);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_00_move_personal_preview_scope ON messenger_message_placements;
CREATE TRIGGER messenger_00_move_personal_preview_scope
AFTER UPDATE OF stream_uuid,topic_uuid,message_uuid ON messenger_message_placements
FOR EACH ROW EXECUTE FUNCTION messenger_move_personal_preview_scope();

CREATE OR REPLACE FUNCTION messenger_date_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_seqscan=off AS $$
DECLARE placement record;
BEGIN
    IF NEW.created_at IS NOT DISTINCT FROM OLD.created_at THEN
        RETURN NULL;
    END IF;
    FOR placement IN SELECT uuid,stream_uuid,topic_uuid
        FROM messenger_message_placements
        WHERE project_id=NEW.project_id AND message_uuid=NEW.uuid
    LOOP
        UPDATE messenger_user_message_bindings
        SET preview_created_at=NEW.created_at
        WHERE project_id=NEW.project_id AND placement_uuid=placement.uuid
          AND preview_created_at IS DISTINCT FROM NEW.created_at;
    END LOOP;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_00_date_personal_preview_scope ON messenger_messages;
CREATE TRIGGER messenger_00_date_personal_preview_scope
AFTER UPDATE OF created_at ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_date_personal_preview_scope();
-- A data-modifying CTE can create all three levels in one statement. Row-level
-- source INSERT repair would repeat the same history lookups once per message.
-- Transition tables keep repair set-based and still support deferred FK order.
CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=placement.message_uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_placements AS placement
    JOIN messenger_messages AS message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (placement.message_uuid,placement.stream_uuid,placement.topic_uuid,message.created_at);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_insert_personal_preview_scope ON messenger_message_placements;
CREATE TRIGGER messenger_insert_personal_preview_scope
AFTER INSERT ON messenger_message_placements REFERENCING NEW TABLE AS new_placements
FOR EACH STATEMENT EXECUTE FUNCTION messenger_insert_personal_preview_scope();

CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_dates()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=message.uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_messages AS message
    JOIN messenger_message_placements AS placement ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (message.uuid,placement.stream_uuid,placement.topic_uuid,message.created_at);
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS messenger_insert_personal_preview_dates ON messenger_messages;
CREATE TRIGGER messenger_insert_personal_preview_dates
AFTER INSERT ON messenger_messages REFERENCING NEW TABLE AS new_messages
FOR EACH STATEMENT EXECUTE FUNCTION messenger_insert_personal_preview_dates();

"""

LOG = logging.getLogger(__name__)
BATCH_BLOCKS = 128
INDEXES = tuple(
    (
        f"messenger_user_message_bindings_{scope}_preview_idx",
        f"""CREATE INDEX CONCURRENTLY IF NOT EXISTS
            messenger_user_message_bindings_{scope}_preview_idx
            ON messenger_user_message_bindings
               (project_id,user_uuid,preview_{scope}_uuid,
                preview_created_at DESC,placement_uuid DESC)""",
    )
    for scope in ("stream", "topic")
)


def _backfill(session):
    # Walk physical windows so the backfill does not fetch each heap page in
    # random UUID order. CTIDs are locked and used only within one statement.
    # A new pass includes rows relocated or inserted during an earlier pass.
    while True:
        # A concurrent flag update can relocate an unprocessed row behind
        # the physical cursor. Verify the whole snapshot before finishing.
        mismatch = session.execute("""
            SELECT count(*) > 0 AS mismatch
                FROM messenger_user_message_bindings AS personal
                JOIN messenger_message_placements AS placement
                  ON placement.project_id=personal.project_id AND placement.uuid=personal.placement_uuid
                JOIN messenger_messages AS message
                  ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
                WHERE (personal.preview_stream_uuid,personal.preview_topic_uuid,personal.preview_created_at,personal.preview_message_uuid)
                    IS DISTINCT FROM (placement.stream_uuid,placement.topic_uuid,message.created_at,placement.message_uuid)
        """).fetchone()["mismatch"]
        session.commit()
        if not mismatch:
            return

        blocks = session.execute(
            "SELECT ceil(pg_relation_size('messenger_user_message_bindings')::numeric "
            "/ current_setting('block_size')::int)::bigint AS blocks"
        ).fetchone()["blocks"]
        session.commit()
        repaired = 0
        for block in range(0, blocks, BATCH_BLOCKS):
            result = session.execute(
                """WITH batch AS MATERIALIZED (
                    SELECT project_id,uuid,placement_uuid,ctid
                    FROM messenger_user_message_bindings
                    WHERE ctid >= %s::tid AND ctid < %s::tid FOR UPDATE
                ), source AS MATERIALIZED (
                    SELECT batch.ctid,placement.stream_uuid,
                           placement.topic_uuid,message.created_at,placement.message_uuid
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
                    UPDATE messenger_user_message_bindings AS personal
                    SET preview_stream_uuid=source.stream_uuid,preview_topic_uuid=source.topic_uuid,
                        preview_created_at=source.created_at,preview_message_uuid=source.message_uuid
                    FROM source WHERE personal.ctid=source.ctid
                      AND (personal.preview_stream_uuid,personal.preview_topic_uuid,personal.preview_created_at,personal.preview_message_uuid)
                          IS DISTINCT FROM (source.stream_uuid,source.topic_uuid,source.created_at,source.message_uuid)
                    RETURNING 1
                ) SELECT count(*) AS repaired FROM repaired""",
                (f"({block},0)", f"({block + BATCH_BLOCKS},0)"),
            ).fetchone()
            session.commit()
            repaired += result["repaired"]
            if result["repaired"] or block % (BATCH_BLOCKS * 100) == 0:
                LOG.info(
                    "Personal preview backfill: %s rows repaired, block %s/%s",
                    repaired,
                    block,
                    blocks,
                )


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
            if create:
                # The backfill updates every old binding. Clean its dead tuple
                # versions and collect key statistics before switching readers;
                # waiting for autovacuum can temporarily penalize other APIs.
                cursor.execute("VACUUM (ANALYZE) messenger_user_message_bindings")
    finally:
        connection.autocommit = False
        session.engine.close_connection(connection)


PAGE_CANDIDATE_VIEW = """CREATE OR REPLACE VIEW messenger_api_message_page_candidates_v1 AS
SELECT binding.uuid AS binding_uuid,binding.placement_uuid AS uuid,message.uuid AS canonical_message_uuid,
       binding.user_uuid,binding.project_id,binding.preview_created_at AS created_at,message.updated_at,
       binding.preview_stream_uuid AS stream_uuid,binding.preview_topic_uuid AS topic_uuid,(SELECT workspace_content FROM messenger_message_contents contents WHERE contents.uuid=message.content_uuid) AS payload,message.author_uuid,
       unread.placement_uuid IS NULL AS read,
       pin.placement_uuid IS NOT NULL AS pinned,
       star.placement_uuid IS NOT NULL AS starred,
       message.author_uuid=binding.user_uuid AS is_own,
       mention.placement_uuid IS NOT NULL AS mentioned,
       message.reactions,message.reaction_users,message.source_name,message.provider_uuid,message.provider_identity,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) AS deleted_at,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) IS NULL AS visible,message.status,
       membership.membership_generation
FROM messenger_user_message_bindings binding
-- Keep the sparse relations in the first join group. Appending them after
-- the history joins can exceed PostgreSQL's default join_collapse_limit,
-- leaving selective mention predicates behind a project-wide history scan.
LEFT JOIN messenger_user_message_mentions mention
    ON (mention.project_id,mention.user_uuid,mention.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_unreads unread
    ON (unread.project_id,unread.user_uuid,unread.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_pins pin
    ON (pin.project_id,pin.user_uuid,pin.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_stars star
    ON (star.project_id,star.user_uuid,star.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
JOIN messenger_messages message ON message.uuid=binding.preview_message_uuid AND message.project_id=ANY(ARRAY[binding.project_id])
JOIN messenger_stream_bindings membership ON membership.project_id=binding.project_id
    AND membership.user_uuid=binding.user_uuid AND membership.stream_uuid=binding.preview_stream_uuid AND membership.active
JOIN messenger_streams stream ON stream.project_id=binding.project_id AND stream.uuid=binding.preview_stream_uuid
JOIN messenger_topics topic ON topic.project_id=binding.project_id AND topic.uuid=binding.preview_topic_uuid;
"""

INDEXES += (
    (
        "messenger_bindings_page_idx",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS messenger_bindings_page_idx ON messenger_user_message_bindings(project_id,user_uuid,preview_created_at DESC,placement_uuid DESC)",
    ),
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0290-Keep-message-view-history-joins-keyed-with-stale-statistics-cbfed6.py"
        ]

    @property
    def migration_id(self):
        return "cd450862-468b-4a84-94cd-0df6463f83a7"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        initialized = session.execute("""
            SELECT count(*) = 5 AS initialized FROM pg_trigger
            WHERE (tgrelid,tgname) IN (
                ('messenger_user_message_bindings'::regclass,'messenger_set_personal_preview_scope'),
                ('messenger_message_placements'::regclass,'messenger_00_move_personal_preview_scope'),
                ('messenger_messages'::regclass,'messenger_00_date_personal_preview_scope'),
                ('messenger_message_placements'::regclass,'messenger_insert_personal_preview_scope'),
                ('messenger_messages'::regclass,'messenger_insert_personal_preview_dates')
            )
        """).fetchone()["initialized"]
        # Schema installation commits atomically. Resuming a backfill must not
        # reacquire exclusive table locks while ordinary readers are active.
        if not initialized:
            session.execute("SET LOCAL lock_timeout='3s'")
            session.execute(SCHEMA_SQL)
        session.commit()
        _backfill(session)
        _indexes(session, create=True)
        session.execute(PAGE_CANDIDATE_VIEW)

    def downgrade(self, session):
        session.execute("DROP VIEW IF EXISTS messenger_api_message_page_candidates_v1")
        _indexes(session, create=False)
        session.execute("""
            DROP TRIGGER messenger_insert_personal_preview_dates ON messenger_messages;
            DROP TRIGGER messenger_insert_personal_preview_scope ON messenger_message_placements;
            DROP FUNCTION messenger_insert_personal_preview_dates();
            DROP FUNCTION messenger_insert_personal_preview_scope();
            DROP TRIGGER messenger_00_date_personal_preview_scope ON messenger_messages;
            DROP TRIGGER messenger_00_move_personal_preview_scope ON messenger_message_placements;
            DROP TRIGGER messenger_set_personal_preview_scope ON messenger_user_message_bindings;
            DROP FUNCTION messenger_date_personal_preview_scope();
            DROP FUNCTION messenger_move_personal_preview_scope();
            DROP FUNCTION messenger_set_personal_preview_scope();
            ALTER TABLE messenger_user_message_bindings
                DROP COLUMN preview_stream_uuid,DROP COLUMN preview_topic_uuid,
                DROP COLUMN preview_created_at,DROP COLUMN preview_message_uuid;
        """)


migration_step = MigrationStep()
