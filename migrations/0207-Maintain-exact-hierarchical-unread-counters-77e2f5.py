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


# Contributions are keyed by the canonical flag identity. Replaying a baseline
# page or an unchanged flag is a no-op; the fact and all deltas commit together.
UPGRADE = r"""
CREATE SEQUENCE workspace_v3.unread_counter_version;
CREATE TABLE workspace_v3.unread_counter_baseline (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    last_flag_uuid uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
    ready boolean NOT NULL DEFAULT false,
    snapshots_complete boolean NOT NULL DEFAULT false,
    snapshot_binding_uuid uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000'
);
INSERT INTO workspace_v3.unread_counter_baseline(singleton) VALUES (true);
ALTER TABLE workspace_v3.topic_bindings
    ADD COLUMN last_message_dirty boolean NOT NULL DEFAULT true,
    ADD COLUMN exact_unread_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN exact_mentioned_unread_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN exact_active_unread_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN counter_version bigint NOT NULL DEFAULT 0,
    ADD CHECK (exact_unread_count >= 0 AND exact_mentioned_unread_count >= 0
               AND exact_mentioned_unread_count <= exact_unread_count);
ALTER TABLE workspace_v3.stream_bindings
    ADD COLUMN last_message_dirty boolean NOT NULL DEFAULT true,
    ADD COLUMN unread_topic_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN active_unread_topic_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN passive_unread_topic_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN counter_version bigint NOT NULL DEFAULT 0,
    ADD CHECK (unread_topic_count >= 0 AND active_unread_topic_count >= 0
               AND active_unread_topic_count <= unread_topic_count);
ALTER TABLE workspace_v3.folders
    ADD COLUMN unread_stream_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN active_unread_stream_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN passive_unread_stream_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN counter_version bigint NOT NULL DEFAULT 0,
    ADD CHECK (unread_stream_count >= 0 AND active_unread_stream_count >= 0
               AND active_unread_stream_count <= unread_stream_count);
ALTER TABLE workspace_v3.folder_items
    ADD COLUMN counter_unread bigint NOT NULL DEFAULT 0,
    ADD COLUMN counter_active bigint NOT NULL DEFAULT 0;
CREATE TABLE workspace_v3.topic_unread_state (
    uuid uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    project_id uuid NOT NULL, topic_uuid uuid NOT NULL, user_uuid uuid NOT NULL,
    exact_unread_count bigint NOT NULL DEFAULT 0,
    exact_mentioned_unread_count bigint NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id,topic_uuid,user_uuid),
    FOREIGN KEY(project_id,topic_uuid) REFERENCES workspace_v3.topics(project_id,uuid) ON DELETE CASCADE,
    FOREIGN KEY(user_uuid) REFERENCES workspace_v3.users(uuid) ON DELETE CASCADE,
    CHECK(exact_unread_count>=0 AND exact_mentioned_unread_count>=0
          AND exact_mentioned_unread_count<=exact_unread_count)
);
CREATE TABLE workspace_v3.unread_contributions (
    project_id uuid NOT NULL, message_uuid uuid NOT NULL,
    user_uuid uuid NOT NULL, topic_uuid uuid NOT NULL,
    mentioned boolean NOT NULL,
    PRIMARY KEY (project_id, message_uuid, user_uuid),
    FOREIGN KEY (project_id, message_uuid, user_uuid)
        REFERENCES workspace_v3.message_flags(project_id, message_uuid, user_uuid)
        ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE FUNCTION workspace_v3.capture_unread_flags() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    DELETE FROM workspace_v3.unread_contributions AS contribution
    USING new_rows AS flag
    WHERE contribution.project_id = flag.project_id
      AND contribution.message_uuid = flag.message_uuid
      AND contribution.user_uuid = flag.user_uuid AND flag.read;
    INSERT INTO workspace_v3.unread_contributions
        (project_id, message_uuid, user_uuid, topic_uuid, mentioned)
    SELECT flag.project_id, flag.message_uuid, flag.user_uuid,
           message.topic_uuid, flag.mentioned
    FROM new_rows AS flag JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id AND message.uuid = flag.message_uuid
    WHERE NOT flag.read
    ORDER BY flag.project_id, flag.message_uuid, flag.user_uuid
    ON CONFLICT (project_id, message_uuid, user_uuid) DO UPDATE
    SET topic_uuid = EXCLUDED.topic_uuid, mentioned = EXCLUDED.mentioned
    WHERE (unread_contributions.topic_uuid, unread_contributions.mentioned)
       IS DISTINCT FROM (EXCLUDED.topic_uuid, EXCLUDED.mentioned);
    RETURN NULL;
END $f$;
CREATE TRIGGER aa_capture_unread_flags_insert AFTER INSERT
    ON workspace_v3.message_flags REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.capture_unread_flags();
CREATE TRIGGER aa_capture_unread_flags_update AFTER UPDATE
    ON workspace_v3.message_flags REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.capture_unread_flags();

CREATE FUNCTION workspace_v3.move_unread_contributions() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    -- Lock flags before reading/replacing their contribution. A direct
    -- contribution update races a flag statement whose message snapshot still
    -- contains the old topic. Even an in-stream topic move must serialize here.
    DELETE FROM workspace_v3.message_flags AS flag
    USING new_rows AS message,old_rows AS previous
    WHERE previous.uuid=message.uuid AND flag.project_id=message.project_id
      AND flag.message_uuid=message.uuid
      AND (previous.topic_uuid,previous.stream_uuid)
          IS DISTINCT FROM (message.topic_uuid,message.stream_uuid)
      AND NOT EXISTS (SELECT 1 FROM workspace_v3.stream_bindings AS binding
                      WHERE binding.project_id=message.project_id
                        AND binding.stream_uuid=message.stream_uuid
                        AND binding.user_uuid=flag.user_uuid);
    UPDATE workspace_v3.message_flags AS flag
    SET stream_uuid = message.stream_uuid
    FROM new_rows AS message JOIN old_rows AS previous USING (uuid)
    WHERE flag.project_id = message.project_id AND flag.message_uuid = message.uuid
      AND (previous.topic_uuid,previous.stream_uuid)
          IS DISTINCT FROM (message.topic_uuid,message.stream_uuid)
      AND EXISTS (SELECT 1 FROM workspace_v3.stream_bindings AS binding
                  WHERE binding.project_id=message.project_id
                    AND binding.stream_uuid=message.stream_uuid
                    AND binding.user_uuid=flag.user_uuid);
    RETURN NULL;
END $f$;
CREATE TRIGGER aa_move_unread_contributions AFTER UPDATE ON workspace_v3.messages
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.move_unread_contributions();

CREATE FUNCTION workspace_v3.serialize_new_flag_with_message_move() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    PERFORM message.uuid FROM workspace_v3.messages AS message
    WHERE message.project_id=NEW.project_id AND message.uuid=NEW.message_uuid FOR SHARE;
    RETURN NEW;
END $f$;
CREATE TRIGGER serialize_new_flag_with_message_move BEFORE INSERT ON workspace_v3.message_flags
    FOR EACH ROW EXECUTE FUNCTION workspace_v3.serialize_new_flag_with_message_move();

CREATE FUNCTION workspace_v3.prepare_topic_counter() RETURNS trigger
LANGUAGE plpgsql AS $f$
DECLARE stream_mode text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO workspace_v3.topic_unread_state(project_id,topic_uuid,user_uuid)
        VALUES (NEW.project_id,NEW.topic_uuid,NEW.user_uuid) ON CONFLICT DO NOTHING;
        SELECT exact_unread_count,exact_mentioned_unread_count
        INTO NEW.exact_unread_count,NEW.exact_mentioned_unread_count
        FROM workspace_v3.topic_unread_state
        WHERE project_id=NEW.project_id AND topic_uuid=NEW.topic_uuid
          AND user_uuid=NEW.user_uuid FOR NO KEY UPDATE;
    ELSIF (OLD.project_id,OLD.topic_uuid,OLD.user_uuid)
          IS DISTINCT FROM (NEW.project_id,NEW.topic_uuid,NEW.user_uuid) THEN
        NEW.last_message_dirty := true;
        -- An UPDATE already owns the binding lock. Never wait for state here:
        -- its writer may own state and be waiting for this binding. The
        -- identity-change snapshot task repairs the committed new scope.
        SELECT COALESCE(sum(exact_unread_count),0),
               COALESCE(sum(exact_mentioned_unread_count),0)
        INTO NEW.exact_unread_count,NEW.exact_mentioned_unread_count
        FROM workspace_v3.topic_unread_state
        WHERE project_id=NEW.project_id AND topic_uuid=NEW.topic_uuid AND user_uuid=NEW.user_uuid;
    END IF;
    SELECT notification_mode INTO stream_mode FROM workspace_v3.stream_bindings
    WHERE project_id = NEW.project_id AND stream_uuid = NEW.stream_uuid
      AND user_uuid = NEW.user_uuid;
    NEW.exact_active_unread_count := CASE
        WHEN NEW.notification_mode = 'mute' THEN 0
        WHEN NEW.notification_mode = 'follow' THEN NEW.exact_unread_count
        WHEN NEW.notification_mode = 'unmute' THEN NEW.exact_mentioned_unread_count
        WHEN stream_mode = 'all_messages' THEN NEW.exact_unread_count
        WHEN stream_mode = 'mentions_only' THEN NEW.exact_mentioned_unread_count
        ELSE 0 END;
    IF TG_OP = 'INSERT' OR
       (OLD.exact_unread_count, OLD.exact_active_unread_count, OLD.stream_uuid)
       IS DISTINCT FROM
       (NEW.exact_unread_count, NEW.exact_active_unread_count, NEW.stream_uuid) THEN
        NEW.counter_version := nextval('workspace_v3.unread_counter_version');
    END IF;
    RETURN NEW;
END $f$;
CREATE TRIGGER prepare_topic_counter BEFORE INSERT OR UPDATE
    ON workspace_v3.topic_bindings FOR EACH ROW
    EXECUTE FUNCTION workspace_v3.prepare_topic_counter();

CREATE FUNCTION workspace_v3.prepare_parent_counter() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_TABLE_NAME = 'stream_bindings' THEN
        NEW.passive_unread_topic_count :=
            NEW.unread_topic_count - NEW.active_unread_topic_count;
    ELSE
        NEW.passive_unread_stream_count :=
            NEW.unread_stream_count - NEW.active_unread_stream_count;
    END IF;
    IF TG_OP = 'INSERT' OR
       (to_jsonb(OLD)->'unread_topic_count', to_jsonb(OLD)->'active_unread_topic_count',
        to_jsonb(OLD)->'unread_stream_count', to_jsonb(OLD)->'active_unread_stream_count')
       IS DISTINCT FROM
       (to_jsonb(NEW)->'unread_topic_count', to_jsonb(NEW)->'active_unread_topic_count',
        to_jsonb(NEW)->'unread_stream_count', to_jsonb(NEW)->'active_unread_stream_count') THEN
        NEW.counter_version := nextval('workspace_v3.unread_counter_version');
    END IF;
    RETURN NEW;
END $f$;
CREATE TRIGGER prepare_parent_counter BEFORE INSERT OR UPDATE
    ON workspace_v3.stream_bindings FOR EACH ROW
    EXECUTE FUNCTION workspace_v3.prepare_parent_counter();
CREATE TRIGGER prepare_parent_counter BEFORE INSERT OR UPDATE
    ON workspace_v3.folders FOR EACH ROW
    EXECUTE FUNCTION workspace_v3.prepare_parent_counter();

CREATE FUNCTION workspace_v3.prepare_folder_item_counter() RETURNS trigger
LANGUAGE plpgsql AS $f$
DECLARE binding workspace_v3.stream_bindings;
BEGIN
    IF TG_OP = 'INSERT' OR
       (OLD.project_id, OLD.stream_uuid, OLD.user_uuid)
       IS DISTINCT FROM (NEW.project_id, NEW.stream_uuid, NEW.user_uuid) THEN
        -- Serialize a new membership with concurrent stream counter changes.
        SELECT * INTO binding FROM workspace_v3.stream_bindings
        WHERE project_id = NEW.project_id AND stream_uuid = NEW.stream_uuid
          AND user_uuid = NEW.user_uuid FOR NO KEY UPDATE;
        NEW.counter_unread := (binding.unread_topic_count > 0)::int;
        NEW.counter_active := (binding.active_unread_topic_count > 0)::int;
    END IF;
    RETURN NEW;
END $f$;
CREATE TRIGGER prepare_folder_item_counter BEFORE INSERT OR UPDATE
    ON workspace_v3.folder_items FOR EACH ROW
    EXECUTE FUNCTION workspace_v3.prepare_folder_item_counter();

CREATE FUNCTION workspace_v3.propagate_stream_counter() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    UPDATE workspace_v3.folder_items AS item
    SET counter_unread = (binding.unread_topic_count > 0)::int,
        counter_active = (binding.active_unread_topic_count > 0)::int
    FROM new_rows AS binding
    WHERE item.project_id = binding.project_id
      AND item.stream_uuid = binding.stream_uuid AND item.user_uuid = binding.user_uuid
      AND (item.counter_unread,item.counter_active) IS DISTINCT FROM
          ((binding.unread_topic_count > 0)::int,
           (binding.active_unread_topic_count > 0)::int);
    RETURN NULL;
END $f$;
CREATE TRIGGER zz_propagate_stream_counter AFTER UPDATE
    ON workspace_v3.stream_bindings REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.propagate_stream_counter();
"""


def _delta_triggers(table, keys, values, target, target_keys, assignments):
    """Generate statement triggers that fold a batch before touching parents."""
    statements = []
    for operation in ("insert", "update", "delete"):
        sources = []
        references = []
        if operation != "insert":
            references.append("OLD TABLE AS old_rows")
            expressions = [
                f"-({value})::bigint AS d{index}" for index, value in enumerate(values)
            ]
            sources.append(f"SELECT {keys}, {', '.join(expressions)} FROM old_rows")
        if operation != "delete":
            references.append("NEW TABLE AS new_rows")
            expressions = [
                f"({value})::bigint AS d{index}" for index, value in enumerate(values)
            ]
            sources.append(f"SELECT {keys}, {', '.join(expressions)} FROM new_rows")
        sums = ", ".join(f"sum(d{i}) AS d{i}" for i in range(len(values)))
        nonzero = " OR ".join(f"sum(d{i}) <> 0" for i in range(len(values)))
        updates = ", ".join(
            f"{column} = target.{column} + delta.d{i}"
            for i, column in enumerate(assignments)
        )
        predicate = " AND ".join(
            f"target.{left} = delta.{right}" for left, right in target_keys
        )
        name = f"{table}_unread_delta_{operation}"
        initialize = ""
        if target == "topic_unread_state" and operation != "delete":
            initialize = """
    INSERT INTO workspace_v3.topic_unread_state(project_id,topic_uuid,user_uuid)
    SELECT DISTINCT project_id,topic_uuid,user_uuid FROM new_rows
    ORDER BY project_id,topic_uuid,user_uuid ON CONFLICT DO NOTHING;
"""
        statements.append(f"""
CREATE FUNCTION workspace_v3.{name}() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    {initialize}
    PERFORM target.uuid FROM workspace_v3.{target} AS target
    JOIN (SELECT {keys}, {sums}
          FROM ({" UNION ALL ".join(sources)}) AS changes
          GROUP BY {keys} HAVING {nonzero}) AS delta ON {predicate}
    ORDER BY target.project_id, target.user_uuid, target.uuid FOR NO KEY UPDATE OF target;
    UPDATE workspace_v3.{target} AS target SET {updates}
    FROM (SELECT {keys}, {sums}
          FROM ({" UNION ALL ".join(sources)}) AS changes
          GROUP BY {keys} HAVING {nonzero}) AS delta
    WHERE {predicate};
    RETURN NULL;
END $f$;
CREATE TRIGGER {name} AFTER {operation.upper()} ON workspace_v3.{table}
    REFERENCING {" ".join(references)} FOR EACH STATEMENT
    EXECUTE FUNCTION workspace_v3.{name}();
""")
    return "".join(statements)


UPGRADE += _delta_triggers(
    "unread_contributions",
    "project_id, topic_uuid, user_uuid",
    ["1", "mentioned::int"],
    "topic_unread_state",
    [
        ("project_id", "project_id"),
        ("topic_uuid", "topic_uuid"),
        ("user_uuid", "user_uuid"),
    ],
    ["exact_unread_count", "exact_mentioned_unread_count"],
)
UPGRADE += _delta_triggers(
    "topic_bindings",
    "project_id, stream_uuid, user_uuid",
    [
        "(exact_unread_count > 0)::int",
        "(exact_active_unread_count > 0)::int",
    ],
    "stream_bindings",
    [
        ("project_id", "project_id"),
        ("stream_uuid", "stream_uuid"),
        ("user_uuid", "user_uuid"),
    ],
    [
        "unread_topic_count",
        "active_unread_topic_count",
    ],
)
UPGRADE += _delta_triggers(
    "folder_items",
    "project_id, user_uuid, folder_uuid",
    ["counter_unread", "counter_active"],
    "folders",
    [("project_id", "project_id"), ("user_uuid", "user_uuid"), ("uuid", "folder_uuid")],
    [
        "unread_stream_count",
        "active_unread_stream_count",
    ],
)

# Migration installs capture first. The worker backfills finite, restartable
# flag pages using the same upsert under row locks. No source facts are erased.
# Function-local settings keep UUID cursor pages on their ordered indexes even
# after heavy write churn makes a full relation sort look cheaper. Required
# sorts of the finite input page still work; settings are restored on return.
UPGRADE += r"""
CREATE FUNCTION workspace_v3.advance_unread_baseline(batch_size integer)
RETURNS integer LANGUAGE plpgsql SET enable_sort = off SET jit = off AS $f$
DECLARE state workspace_v3.unread_counter_baseline;
        flag_ids uuid[];
        binding_ids uuid[];
BEGIN
    SELECT * INTO state FROM workspace_v3.unread_counter_baseline
    WHERE NOT snapshots_complete FOR UPDATE SKIP LOCKED;
    IF NOT FOUND THEN RETURN 0; END IF;
    IF state.ready THEN
        SELECT array_agg(uuid ORDER BY uuid) INTO binding_ids FROM (
            SELECT uuid FROM workspace_v3.topic_bindings
            WHERE uuid > state.snapshot_binding_uuid ORDER BY uuid LIMIT batch_size
        ) AS page;
        IF binding_ids IS NULL THEN
            UPDATE workspace_v3.unread_counter_baseline SET snapshots_complete=true;
            RETURN 0;
        END IF;
        INSERT INTO workspace_v3.projection_tasks
            (project_id,task_type,scope_type,scope_uuid,user_uuid,payload)
        SELECT binding.project_id,'read_counters','user_topic',binding.topic_uuid,
               binding.user_uuid,'{"emit_message_events":false}'::jsonb
        FROM workspace_v3.topic_bindings AS binding
        WHERE binding.uuid=ANY(binding_ids)
        ON CONFLICT (project_id,task_type,scope_type,scope_uuid,user_uuid)
        WHERE status='pending' AND task_type='read_counters'
          AND payload IN ('{"emit_message_events":false}'::jsonb,
                          '{"emit_message_event":false}'::jsonb) DO NOTHING;
        UPDATE workspace_v3.unread_counter_baseline
        SET snapshot_binding_uuid=binding_ids[array_length(binding_ids,1)];
        RETURN array_length(binding_ids,1);
    END IF;
    SELECT array_agg(uuid ORDER BY uuid) INTO flag_ids FROM (
        SELECT uuid FROM workspace_v3.message_flags
        WHERE uuid > state.last_flag_uuid ORDER BY uuid
        LIMIT batch_size FOR UPDATE
    ) AS page;
    IF flag_ids IS NULL THEN
        UPDATE workspace_v3.unread_counter_baseline SET ready = true;
        RETURN 1;
    END IF;
    INSERT INTO workspace_v3.unread_contributions
        (project_id, message_uuid, user_uuid, topic_uuid, mentioned)
    SELECT flag.project_id, flag.message_uuid, flag.user_uuid,
           message.topic_uuid, flag.mentioned
    FROM workspace_v3.message_flags AS flag JOIN workspace_v3.messages AS message
      ON message.project_id = flag.project_id AND message.uuid = flag.message_uuid
    WHERE flag.uuid = ANY(flag_ids) AND NOT flag.read
    ORDER BY flag.project_id, flag.message_uuid, flag.user_uuid
    ON CONFLICT (project_id, message_uuid, user_uuid) DO UPDATE
    SET topic_uuid = EXCLUDED.topic_uuid, mentioned = EXCLUDED.mentioned
    WHERE (unread_contributions.topic_uuid, unread_contributions.mentioned)
       IS DISTINCT FROM (EXCLUDED.topic_uuid, EXCLUDED.mentioned);
    UPDATE workspace_v3.unread_counter_baseline
    SET last_flag_uuid = flag_ids[array_length(flag_ids, 1)];
    RETURN array_length(flag_ids, 1);
END $f$;
"""


UPGRADE += r"""
CREATE FUNCTION workspace_v3.enqueue_exact_counter_snapshot() RETURNS trigger
LANGUAGE plpgsql AS $f$
DECLARE scope text; identity_field text;
BEGIN
    IF TG_TABLE_NAME = 'topic_bindings' THEN
        scope := 'user_topic'; identity_field := 'topic_uuid';
    ELSE
        scope := 'user_stream'; identity_field := 'stream_uuid';
    END IF;
    INSERT INTO workspace_v3.projection_tasks
        (project_id, task_type, scope_type, scope_uuid, user_uuid, payload)
    SELECT binding.project_id, 'read_counters', scope,
           (to_jsonb(binding)->>identity_field)::uuid, binding.user_uuid,
           '{"emit_message_events":false}'::jsonb
    FROM new_rows AS binding JOIN old_rows AS previous USING (project_id, uuid)
    WHERE binding.counter_version IS DISTINCT FROM previous.counter_version
       OR (binding.project_id,binding.user_uuid,to_jsonb(binding)->'topic_uuid')
          IS DISTINCT FROM (previous.project_id,previous.user_uuid,to_jsonb(previous)->'topic_uuid')
    ON CONFLICT (project_id, task_type, scope_type, scope_uuid, user_uuid)
    WHERE status = 'pending' AND task_type = 'read_counters'
      AND payload IN ('{"emit_message_events":false}'::jsonb,
                      '{"emit_message_event":false}'::jsonb) DO NOTHING;
    RETURN NULL;
END $f$;
CREATE TRIGGER zz_enqueue_exact_counter_snapshot AFTER UPDATE
    ON workspace_v3.topic_bindings
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.enqueue_exact_counter_snapshot();
CREATE TRIGGER zz_enqueue_exact_counter_snapshot AFTER UPDATE
    ON workspace_v3.stream_bindings
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.enqueue_exact_counter_snapshot();
UPDATE workspace_v3.unread_counter_baseline
SET ready = NOT EXISTS (SELECT 1 FROM workspace_v3.message_flags LIMIT 1),
    snapshots_complete = NOT EXISTS (SELECT 1 FROM workspace_v3.message_flags LIMIT 1);
"""

UPGRADE += r"""
CREATE FUNCTION workspace_v3.invalidate_stream_latest_message() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' OR (OLD.last_message_uuid IS DISTINCT FROM NEW.last_message_uuid)
       OR (OLD.stream_uuid IS DISTINCT FROM NEW.stream_uuid)
       OR (NEW.last_message_dirty AND NOT OLD.last_message_dirty) THEN
        UPDATE workspace_v3.stream_bindings SET last_message_dirty = true
        WHERE project_id = OLD.project_id AND stream_uuid = OLD.stream_uuid
          AND user_uuid = OLD.user_uuid AND NOT last_message_dirty;
    END IF;
    IF TG_OP <> 'DELETE' AND (TG_OP = 'INSERT' OR
       OLD.last_message_uuid IS DISTINCT FROM NEW.last_message_uuid OR
       OLD.stream_uuid IS DISTINCT FROM NEW.stream_uuid OR
       (NEW.last_message_dirty AND NOT OLD.last_message_dirty)) THEN
        UPDATE workspace_v3.stream_bindings SET last_message_dirty = true
        WHERE project_id = NEW.project_id AND stream_uuid = NEW.stream_uuid
          AND user_uuid = NEW.user_uuid AND NOT last_message_dirty;
    END IF;
    RETURN NULL;
END $f$;
CREATE TRIGGER invalidate_stream_latest_message AFTER INSERT OR UPDATE OR DELETE
    ON workspace_v3.topic_bindings FOR EACH ROW
    EXECUTE FUNCTION workspace_v3.invalidate_stream_latest_message();
"""

UPGRADE += r"""
CREATE FUNCTION workspace_v3.publish_topic_unread_state() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
    PERFORM binding.uuid FROM workspace_v3.topic_bindings AS binding
    JOIN new_rows AS state USING(project_id,topic_uuid,user_uuid)
    ORDER BY binding.project_id,binding.user_uuid,binding.uuid
    FOR NO KEY UPDATE OF binding;
    UPDATE workspace_v3.topic_bindings AS binding
    SET exact_unread_count=state.exact_unread_count,
        exact_mentioned_unread_count=state.exact_mentioned_unread_count
    FROM new_rows AS state
    WHERE binding.project_id=state.project_id AND binding.topic_uuid=state.topic_uuid
      AND binding.user_uuid=state.user_uuid
      AND (binding.exact_unread_count,binding.exact_mentioned_unread_count)
         IS DISTINCT FROM (state.exact_unread_count,state.exact_mentioned_unread_count);
    RETURN NULL;
END $f$;
CREATE TRIGGER publish_topic_unread_state_insert AFTER INSERT ON workspace_v3.topic_unread_state
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION workspace_v3.publish_topic_unread_state();
CREATE TRIGGER publish_topic_unread_state_update AFTER UPDATE ON workspace_v3.topic_unread_state
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT
    EXECUTE FUNCTION workspace_v3.publish_topic_unread_state();
"""


# Read/star/mention updates cannot change the latest visible message. Keep their
# projection path independent of history length, including sparse flag sets.
_metadata_triggers = []
for _table in ("message_flags", "messages"):
    for _operation in ("insert", "update", "delete"):
        if _table == "messages" and _operation == "insert":
            continue  # Visibility starts with insertion of a user's flag.
        _references = []
        _sources = []
        for _side in ("old", "new"):
            if (_side == "old" and _operation == "insert") or (
                _side == "new" and _operation == "delete"
            ):
                continue
            _references.append(f"{_side.upper()} TABLE AS {_side}_rows")
            _join = ""
            _where = ""
            if _operation == "update":
                _other = "new" if _side == "old" else "old"
                _join = f" JOIN {_other}_rows AS previous USING (uuid)"
                _fields = (
                    ("project_id", "message_uuid", "user_uuid", "stream_uuid")
                    if _table == "message_flags"
                    else ("project_id", "topic_uuid", "stream_uuid", "created_at")
                )
                _where = (
                    " WHERE ("
                    + ",".join(f"row.{f}" for f in _fields)
                    + ") IS DISTINCT FROM ("
                    + ",".join(f"previous.{f}" for f in _fields)
                    + ")"
                )
            if _table == "message_flags":
                _sources.append(
                    f"SELECT row.project_id,message.topic_uuid,row.user_uuid FROM {_side}_rows AS row{_join} "
                    f"JOIN workspace_v3.messages AS message ON message.project_id=row.project_id AND message.uuid=row.message_uuid{_where}"
                )
            else:
                _sources.append(
                    f"SELECT row.project_id,row.topic_uuid FROM {_side}_rows AS row{_join}{_where}"
                )
        _keys = (
            "project_id,topic_uuid,user_uuid"
            if _table == "message_flags"
            else "project_id,topic_uuid"
        )
        _predicate = " AND ".join(
            f"binding.{key}=target.{key}" for key in _keys.split(",")
        )
        _name = f"{_table}_invalidate_latest_{_operation}"
        _metadata_triggers.append((_table, _name))
        UPGRADE += f"""
CREATE FUNCTION workspace_v3.{_name}() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    PERFORM binding.uuid FROM workspace_v3.topic_bindings AS binding
    JOIN ({" UNION ".join(_sources)}) AS target ON {_predicate}
    WHERE NOT binding.last_message_dirty
    ORDER BY binding.project_id,binding.user_uuid,binding.uuid FOR NO KEY UPDATE OF binding;
    UPDATE workspace_v3.topic_bindings AS binding SET last_message_dirty=true
    FROM ({" UNION ".join(_sources)}) AS target
    WHERE {_predicate} AND NOT binding.last_message_dirty;
    RETURN NULL;
END $f$;
CREATE TRIGGER ab_invalidate_latest_{_operation} AFTER {_operation.upper()} ON workspace_v3.{_table}
    REFERENCING {" ".join(_references)} FOR EACH STATEMENT EXECUTE FUNCTION workspace_v3.{_name}();
"""


DOWNGRADE = ""
for _table, _name in _metadata_triggers:
    _operation = _name.rsplit("_", 1)[1]
    DOWNGRADE += (
        f"DROP TRIGGER ab_invalidate_latest_{_operation} ON workspace_v3.{_table};\n"
    )
    DOWNGRADE += f"DROP FUNCTION workspace_v3.{_name}();\n"
for _table, _triggers in {
    "topic_unread_state": [
        "publish_topic_unread_state_insert",
        "publish_topic_unread_state_update",
    ],
    "message_flags": [
        "serialize_new_flag_with_message_move",
        "aa_capture_unread_flags_insert",
        "aa_capture_unread_flags_update",
    ],
    "messages": ["aa_move_unread_contributions"],
    "topic_bindings": [
        "invalidate_stream_latest_message",
        "prepare_topic_counter",
        "zz_enqueue_exact_counter_snapshot",
    ],
    "stream_bindings": [
        "prepare_parent_counter",
        "zz_propagate_stream_counter",
        "zz_enqueue_exact_counter_snapshot",
    ],
    "folders": ["prepare_parent_counter"],
    "folder_items": ["prepare_folder_item_counter"],
}.items():
    for _trigger in _triggers:
        DOWNGRADE += f"DROP TRIGGER {_trigger} ON workspace_v3.{_table};\n"
for _table in ("unread_contributions", "topic_bindings", "folder_items"):
    for _operation in ("insert", "update", "delete"):
        _name = f"{_table}_unread_delta_{_operation}"
        DOWNGRADE += f"DROP TRIGGER {_name} ON workspace_v3.{_table};\n"
        DOWNGRADE += f"DROP FUNCTION workspace_v3.{_name}();\n"
for _function in (
    "publish_topic_unread_state()",
    "invalidate_stream_latest_message()",
    "capture_unread_flags()",
    "move_unread_contributions()",
    "serialize_new_flag_with_message_move()",
    "prepare_topic_counter()",
    "prepare_parent_counter()",
    "prepare_folder_item_counter()",
    "propagate_stream_counter()",
    "enqueue_exact_counter_snapshot()",
    "advance_unread_baseline(integer)",
):
    DOWNGRADE += f"DROP FUNCTION workspace_v3.{_function};\n"
DOWNGRADE += "DROP TABLE workspace_v3.unread_contributions, workspace_v3.topic_unread_state, workspace_v3.unread_counter_baseline;"
for _table, _columns in {
    "topic_bindings": [
        "last_message_dirty",
        "exact_unread_count",
        "exact_mentioned_unread_count",
        "exact_active_unread_count",
        "counter_version",
    ],
    "stream_bindings": [
        "last_message_dirty",
        "unread_topic_count",
        "active_unread_topic_count",
        "passive_unread_topic_count",
        "counter_version",
    ],
    "folders": [
        "unread_stream_count",
        "active_unread_stream_count",
        "passive_unread_stream_count",
        "counter_version",
    ],
    "folder_items": ["counter_unread", "counter_active"],
}.items():
    DOWNGRADE += (
        f"ALTER TABLE workspace_v3.{_table} "
        + ", ".join(f"DROP COLUMN {column}" for column in _columns)
        + ";"
    )
DOWNGRADE += "DROP SEQUENCE workspace_v3.unread_counter_version;"


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0206-Backfill-external-bridge-files-into-Workspace-v3-7d5a7e.py"
        ]

    @property
    def migration_id(self):
        return "77e2f5e9-5bb1-4c51-b42d-63382a54e55a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
