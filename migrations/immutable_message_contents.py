# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Published SQL for immutable content pages and per-message revisions."""

import re


CONTENTS_SQL = """
CREATE FUNCTION messenger_workspace_content_hash(p_workspace jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT encode(digest(convert_to(p_workspace::text,'UTF8'),'sha256'),'hex')
$$;
CREATE FUNCTION messenger_provider_content_hash(p_provider text)
RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT encode(digest(convert_to(p_provider,'UTF8'),'sha256'),'hex')
$$;

CREATE TABLE messenger_message_contents (
    uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_content jsonb NOT NULL,
    provider_content text,
    workspace_content_hash text GENERATED ALWAYS AS
        (messenger_workspace_content_hash(workspace_content)) STORED,
    provider_content_hash text GENERATED ALWAYS AS
        (messenger_provider_content_hash(provider_content)) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (workspace_content_hash,provider_content_hash)
);
CREATE INDEX messenger_message_contents_provider_hash_idx
    ON messenger_message_contents(provider_content_hash)
    WHERE provider_content_hash IS NOT NULL;

CREATE FUNCTION messenger_reject_content_update()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Message contents are immutable' USING ERRCODE='23514';
END;
$$;
CREATE TRIGGER messenger_reject_content_update
BEFORE UPDATE ON messenger_message_contents
FOR EACH ROW EXECUTE FUNCTION messenger_reject_content_update();

CREATE FUNCTION messenger_resolve_message_content(p_workspace jsonb,p_provider text)
RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    resolved uuid;
    existing messenger_message_contents%ROWTYPE;
BEGIN
    LOOP
        INSERT INTO messenger_message_contents(workspace_content,provider_content)
        VALUES (p_workspace,p_provider)
        ON CONFLICT (workspace_content_hash,provider_content_hash) DO NOTHING
        RETURNING uuid INTO resolved;
        IF resolved IS NOT NULL THEN RETURN resolved; END IF;

        SELECT * INTO existing FROM messenger_message_contents
        WHERE workspace_content_hash=messenger_workspace_content_hash(p_workspace)
          AND provider_content_hash IS NOT DISTINCT FROM
              messenger_provider_content_hash(p_provider)
        FOR KEY SHARE;
        IF FOUND THEN
            IF existing.workspace_content IS DISTINCT FROM p_workspace
               OR existing.provider_content IS DISTINCT FROM p_provider THEN
                RAISE EXCEPTION 'Message content hash collision' USING ERRCODE='23514';
            END IF;
            RETURN existing.uuid;
        END IF;
        -- A future collector may remove an unreferenced conflicting row before
        -- our lookup. Retry insertion; a found row stays pinned until commit.
    END LOOP;
END;
$$;
"""


VERSIONS_SQL = """
CREATE TABLE messenger_message_content_versions (
    project_id uuid NOT NULL,
    message_uuid uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    content_uuid uuid NOT NULL REFERENCES messenger_message_contents(uuid),
    previous_content_uuid uuid REFERENCES messenger_message_contents(uuid),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id,message_uuid,revision),
    FOREIGN KEY (project_id,message_uuid)
        REFERENCES messenger_messages(project_id,uuid)
        ON UPDATE CASCADE ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE
);
CREATE INDEX messenger_content_versions_content_idx
    ON messenger_message_content_versions(content_uuid);
CREATE INDEX messenger_content_versions_previous_idx
    ON messenger_message_content_versions(previous_content_uuid)
    WHERE previous_content_uuid IS NOT NULL;

INSERT INTO messenger_message_content_versions
    (project_id,message_uuid,revision,content_uuid,created_at)
SELECT project_id,uuid,content_revision,content_uuid,
       COALESCE(content_updated_at,updated_at) AT TIME ZONE 'UTC'
FROM messenger_messages;

-- Pending requests retain their exact wire payload. Only add the internal
-- revision guard when their saved Workspace payload still matches the root.
UPDATE zulip_outgoing_intents intent SET data=intent.data ||
    jsonb_build_object('content_revision',message.content_revision)
FROM messenger_messages message
JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid
WHERE intent.project_id=message.project_id AND intent.entity_uuid=message.uuid
  AND intent.entity_type='message' AND intent.status<>'done'
  AND intent.data->'payload'=contents.workspace_content;

CREATE OR REPLACE FUNCTION messenger_track_content_version()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    previous_workspace jsonb;
    next_workspace jsonb;
BEGIN
    IF TG_OP='INSERT' THEN
        NEW.content_revision := 1;
        NEW.content_updated_at := COALESCE(NEW.content_updated_at,NEW.updated_at);
    ELSE
        SELECT workspace_content INTO previous_workspace
            FROM messenger_message_contents WHERE uuid=OLD.content_uuid;
        SELECT workspace_content INTO next_workspace
            FROM messenger_message_contents WHERE uuid=NEW.content_uuid;
        NEW.content_revision := OLD.content_revision;
        IF NEW.content_uuid IS DISTINCT FROM OLD.content_uuid THEN
            NEW.content_revision := OLD.content_revision+1;
        END IF;
        IF next_workspace IS DISTINCT FROM previous_workspace THEN
            IF (NEW.content_updated_at IS NULL
                OR NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at)
               AND current_setting('workspace.projected_message_content',true)
                   IS DISTINCT FROM (NEW.project_id::text || ':' || NEW.uuid::text) THEN
                NEW.content_updated_at := statement_timestamp() AT TIME ZONE 'UTC';
            END IF;
        END IF;
        NEW.content_updated_at := COALESCE(NEW.content_updated_at,OLD.content_updated_at);
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION messenger_record_content_version()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        INSERT INTO messenger_message_content_versions
            (project_id,message_uuid,revision,content_uuid)
        VALUES (NEW.project_id,NEW.uuid,NEW.content_revision,NEW.content_uuid);
    ELSIF NEW.content_revision<>OLD.content_revision THEN
        INSERT INTO messenger_message_content_versions
            (project_id,message_uuid,revision,content_uuid,previous_content_uuid)
        VALUES (NEW.project_id,NEW.uuid,NEW.content_revision,NEW.content_uuid,OLD.content_uuid);
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER messenger_record_content_version
AFTER INSERT OR UPDATE OF content_uuid ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_record_content_version();
"""


SNAPSHOT_SQL = """
CREATE FUNCTION zulip_record_content_snapshot(
    p_snapshot uuid,p_delivery uuid,p_content uuid,p_revision bigint
) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE
    target bridge_zulip_v5_deliveries%ROWTYPE;
BEGIN
    SELECT * INTO target FROM bridge_zulip_v5_deliveries WHERE uuid=p_delivery;
    INSERT INTO bridge_zulip_v5_content_snapshots
        (uuid,delivery_uuid,content_uuid,provider_uuid)
    SELECT p_snapshot,p_delivery,p_content,message.provider_uuid
    FROM messenger_messages message
    WHERE message.project_id=target.project_id AND message.uuid=target.message_uuid
    ON CONFLICT (uuid) DO NOTHING;

    UPDATE messenger_messages message SET content_uuid=p_content
    WHERE message.project_id=target.project_id AND message.uuid=target.message_uuid
      AND message.content_revision=p_revision AND message.content_uuid<>p_content
      AND (SELECT workspace_content FROM messenger_message_contents
           WHERE uuid=message.content_uuid)=(
           SELECT workspace_content FROM messenger_message_contents WHERE uuid=p_content);
    RETURN p_content;
END;
$$;
"""


def dependent_views(session):
    return session.execute("""
        WITH RECURSIVE dependencies AS (
            SELECT DISTINCT rewrite.ev_class AS oid,1 AS depth
            FROM pg_depend dependency
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            WHERE dependency.refobjid='messenger_messages'::regclass
            UNION ALL
            SELECT rewrite.ev_class,parent.depth+1
            FROM dependencies parent
            JOIN pg_depend dependency ON dependency.refobjid=parent.oid
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            WHERE rewrite.ev_class<>parent.oid
        )
        SELECT oid::regclass::text AS name,pg_get_viewdef(oid) AS definition
        FROM dependencies GROUP BY oid ORDER BY max(depth),name
    """).fetchall()


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


BACKFILL_SQL = """
ALTER TABLE messenger_messages ADD COLUMN content_uuid uuid,
    ADD COLUMN content_revision bigint NOT NULL DEFAULT 1 CHECK (content_revision>0);
INSERT INTO messenger_message_contents(workspace_content,provider_content)
SELECT DISTINCT payload,NULL FROM messenger_messages
ON CONFLICT (workspace_content_hash,provider_content_hash) DO NOTHING;
UPDATE messenger_messages message SET content_uuid=contents.uuid
FROM messenger_message_contents contents
WHERE contents.workspace_content=message.payload AND contents.provider_content IS NULL;
ALTER TABLE messenger_messages ALTER COLUMN content_uuid SET NOT NULL,
    ADD CONSTRAINT messenger_messages_content_fk FOREIGN KEY(content_uuid)
        REFERENCES messenger_message_contents(uuid);
CREATE INDEX messenger_messages_content_idx ON messenger_messages(content_uuid);

ALTER TABLE bridge_zulip_v5_content_snapshots
    ADD COLUMN content_uuid uuid,
    ADD COLUMN provider_uuid uuid REFERENCES messenger_providers(uuid) ON DELETE CASCADE,
    ADD COLUMN direction text NOT NULL DEFAULT 'outgoing'
        CHECK (direction IN ('incoming','outgoing')),
    ADD COLUMN context_hash text,
    ALTER COLUMN delivery_uuid DROP NOT NULL;
INSERT INTO messenger_message_contents(workspace_content,provider_content)
SELECT DISTINCT jsonb_build_object('kind','markdown','content',workspace_content),zulip_content
FROM bridge_zulip_v5_content_snapshots
ON CONFLICT (workspace_content_hash,provider_content_hash) DO NOTHING;
UPDATE bridge_zulip_v5_content_snapshots snapshot SET content_uuid=contents.uuid
FROM messenger_message_contents contents
WHERE contents.workspace_content=jsonb_build_object('kind','markdown','content',snapshot.workspace_content)
  AND contents.provider_content=snapshot.zulip_content;
UPDATE bridge_zulip_v5_content_snapshots snapshot SET provider_uuid=message.provider_uuid
FROM bridge_zulip_v5_deliveries delivery
JOIN messenger_messages message ON message.project_id=delivery.project_id
    AND message.uuid=delivery.message_uuid
WHERE snapshot.delivery_uuid=delivery.uuid;
ALTER TABLE bridge_zulip_v5_content_snapshots
    ALTER COLUMN content_uuid SET NOT NULL,
    ADD CONSTRAINT bridge_zulip_content_snapshot_page_fk FOREIGN KEY(content_uuid)
        REFERENCES messenger_message_contents(uuid) ON DELETE CASCADE;
CREATE INDEX bridge_zulip_content_snapshots_page_idx
    ON bridge_zulip_v5_content_snapshots(content_uuid);

WITH matching AS (
    SELECT DISTINCT ON (message.project_id,message.uuid)
           message.project_id,message.uuid,snapshot.content_uuid
    FROM messenger_messages message
    JOIN bridge_zulip_v5_deliveries delivery ON delivery.project_id=message.project_id
        AND delivery.message_uuid=message.uuid
    JOIN bridge_zulip_v5_content_snapshots snapshot ON snapshot.delivery_uuid=delivery.uuid
    JOIN messenger_message_contents contents ON contents.uuid=snapshot.content_uuid
        AND contents.workspace_content=message.payload
    WHERE message.source_name='zulip'
    ORDER BY message.project_id,message.uuid,snapshot.created_at DESC,snapshot.uuid DESC
)
UPDATE messenger_messages message SET content_uuid=matching.content_uuid
FROM matching WHERE message.project_id=matching.project_id AND message.uuid=matching.uuid;
ALTER TABLE bridge_zulip_v5_content_snapshots
    DROP COLUMN workspace_content,DROP COLUMN zulip_content;
"""


def upgrade(session):
    views = dependent_views(session)
    columns = [
        row["attname"]
        for row in session.execute("""
            SELECT attname FROM pg_attribute
            WHERE attrelid='messenger_messages'::regclass AND attnum>0 AND NOT attisdropped
            ORDER BY attnum
        """).fetchall()
    ]
    capture = session.execute(
        "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS definition"
    ).fetchone()["definition"]
    summary = session.execute(
        "SELECT pg_get_functiondef('messenger_refresh_summary_after_content_change()'::regprocedure) AS definition"
    ).fetchone()["definition"]
    session.execute(CONTENTS_SQL)
    session.execute(BACKFILL_SQL)
    session.execute(VERSIONS_SQL)
    session.execute(SNAPSHOT_SQL)

    # Views are read projections. Preserve their old column sets, including
    # whole-row JSON expressions, while moving physical content out of roots.
    for view in reversed(views):
        session.execute(f"DROP VIEW {view['name']}")
    session.execute("""
        DROP TRIGGER messenger_refresh_summary_after_content_change ON messenger_messages;
        ALTER TABLE messenger_messages DROP COLUMN payload,DROP COLUMN content_hash;
    """)
    selection = []
    for column in columns:
        name = quote_identifier(column)
        if column == "payload":
            selection.append(f"contents.workspace_content AS {name}")
        elif column == "content_hash":
            selection.append(f"contents.workspace_content_hash AS {name}")
        else:
            selection.append(f"message.{name}")
    session.execute(
        "CREATE VIEW messenger_messages_with_content_v1 AS SELECT "
        + ",".join(selection)
        + " FROM messenger_messages message JOIN messenger_message_contents contents "
        "ON contents.uuid=message.content_uuid"
    )
    for view in views:
        definition = re.sub(
            r"\bmessenger_messages\b",
            "messenger_messages_with_content_v1",
            view["definition"],
        )
        session.execute(f"CREATE VIEW {view['name']} AS {definition}")

    logical_rows = """
    IF kind_='message' AND TG_TABLE_NAME='messenger_messages' THEN
        row_ := row_ || jsonb_build_object('payload',(
            SELECT workspace_content FROM messenger_message_contents
            WHERE uuid=(row_->>'content_uuid')::uuid));
        old_ := old_ || jsonb_build_object('payload',(
            SELECT workspace_content FROM messenger_message_contents
            WHERE uuid=(old_->>'content_uuid')::uuid));
    END IF;
"""
    capture = capture.replace(
        "    IF kind_='message' THEN", logical_rows + "    IF kind_='message' THEN", 1
    ).replace(
        "SELECT to_jsonb(m) INTO data_ FROM messenger_messages m WHERE",
        "SELECT to_jsonb(m) || jsonb_build_object('payload',c.workspace_content) "
        "INTO data_ FROM messenger_messages m "
        "JOIN messenger_message_contents c ON c.uuid=m.content_uuid WHERE",
    )
    session.execute(capture)
    summary_guard = """
    IF OLD.deleted_at IS NOT DISTINCT FROM NEW.deleted_at AND (
        SELECT workspace_content->>'kind' FROM messenger_message_contents
        WHERE uuid=OLD.content_uuid) IS NOT DISTINCT FROM (
        SELECT workspace_content->>'kind' FROM messenger_message_contents
        WHERE uuid=NEW.content_uuid) THEN
        RETURN NULL;
    END IF;
"""
    session.execute(summary.replace("\nBEGIN\n", "\nBEGIN\n" + summary_guard, 1))
    session.execute("""
        CREATE TRIGGER messenger_refresh_summary_after_content_change
        AFTER UPDATE OF content_uuid,deleted_at ON messenger_messages
        FOR EACH ROW WHEN (OLD.content_uuid IS DISTINCT FROM NEW.content_uuid
            OR OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
        EXECUTE FUNCTION messenger_refresh_summary_after_content_change();
    """)
