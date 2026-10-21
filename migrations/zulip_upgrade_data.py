# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""One-time conversion to the existing v5 identities; no runtime aliases."""

import base64
import collections
import json
import re
import urllib.parse
import uuid

from psycopg import sql

STATE_TABLE = "workspace_zulip_v5_legacy_state"
ROOTS = {
    "user": "m_workspace_users",
    "stream": "messenger_streams",
    "topic": "messenger_topics",
    "message": "messenger_messages",
    "placement": "messenger_message_placements",
}
JSON_COLUMNS = {
    "messenger_messages": ("payload", "reactions", "reaction_users"),
    "m_workspace_drafts": ("payload",),
    "messenger_user_folder_bindings": ("folder_items_snapshot",),
    "m_workspace_events": ("payload",),
    "m_workspace_broadcast_message_events_v1": ("payload",),
    "m_workspace_event_recipient_payloads_v1": ("payload",),
    "messenger_domain_outbox_events": ("payload",),
    "messenger_projection_tasks": ("payload",),
}


def state(session):
    result = collections.defaultdict(list)
    for row in session.execute(
        "SELECT kind,data FROM workspace_zulip_v5_legacy_state ORDER BY kind,identity_key"
    ).fetchall():
        result[row["kind"]].append(row["data"])
    return result


def account_scopes(session, saved):
    originals = {item["uuid"]: item for item in saved["account"]}
    accounts = {}
    for row in session.execute(
        "SELECT uuid,project_id,owner_user_uuid,settings->>'server_url' AS server_url,"
        "external_account_id FROM m_external_accounts_v2 WHERE provider='zulip'"
    ).fetchall():
        old = originals.get(str(row["uuid"]))
        if old is None or not row["server_url"]:
            continue
        host = urllib.parse.urlsplit(row["server_url"]).hostname
        accounts[str(row["uuid"])] = {
            **row,
            "host": host,
            "namespace": uuid.uuid5(uuid.NAMESPACE_DNS, host),
            "realm_uuid": old["provider_realm_uuid"],
            "provider_user_id": old["provider_owner_user_id"],
        }
    return accounts


def choose_account(accounts, *, project=None, account_uuid=None, host=None, realm=None):
    account = accounts.get(str(account_uuid))
    if account is not None:
        return account
    candidates = [
        candidate
        for candidate in accounts.values()
        if (project is None or str(candidate["project_id"]) == str(project))
        and (host is None or candidate["host"] == host)
        and (realm is None or candidate["realm_uuid"] == str(realm))
    ]
    if candidates and len({candidate["host"] for candidate in candidates}) == 1:
        return candidates[0]
    raise RuntimeError("Cannot infer the legacy Zulip server identity")


def make_mapping(saved, accounts):
    mapping = {}

    def add(kind, project, old, new):
        key = (kind, str(project) if project is not None else None, str(old))
        new = str(new)
        if key in mapping and mapping[key] != new:
            raise RuntimeError("Legacy Zulip identity has conflicting destinations")
        mapping[key] = new

    owners = {str(account["owner_user_uuid"]) for account in accounts.values()}
    for person in saved["user"]:
        if person["uuid"] in owners:
            continue
        account = choose_account(accounts, account_uuid=person["external_account_uuid"])
        add(
            "user",
            None,
            person["uuid"],
            uuid.uuid5(account["namespace"], "user:" + person["provider_external_id"]),
        )
    chats = {
        (chat["project_id"], chat["projection_stream_uuid"]): chat
        for chat in saved["chat"]
        if chat["projection_stream_uuid"] is not None
    }
    streams = {}
    for stream in saved["stream"]:
        key = (stream["project_id"], stream["uuid"])
        chat = chats.get(key)
        provider = stream["provider"] or {}
        account = choose_account(
            accounts,
            project=stream["project_id"],
            account_uuid=chat["external_account_uuid"]
            if chat
            else provider.get("account_uuid"),
            host=urllib.parse.urlsplit(stream["source"].get("server_url", "")).hostname,
        )
        chat_key = chat["provider_chat_id"] if chat else provider["external_id"]
        kind, identifier = chat_key.split(":", 1)
        if kind == "channel":
            stream_uuid = uuid.uuid5(account["namespace"], identifier)
            recipient = int(identifier)
        else:
            participants = sorted({int(member) for member in identifier.split(",")})
            if chat is not None:
                participants = sorted(
                    {
                        int(member["provider_user_id"])
                        for member in chat["source"]["participants"]
                    }
                )
            stream_uuid = uuid.uuid5(
                account["namespace"], "dm:" + ",".join(map(str, participants))
            )
            recipient = participants
        add("stream", stream["project_id"], stream["uuid"], stream_uuid)
        streams[key] = {
            "uuid": stream_uuid,
            "account": account,
            "recipient": recipient,
            "chat": chat,
        }
    for topic in saved["topic"]:
        stream = streams[(topic["project_id"], topic["stream_uuid"])]
        name = (
            topic["source"].get("topic_name", topic["name"])
            if isinstance(stream["recipient"], int)
            else ""
        )
        add(
            "topic",
            topic["project_id"],
            topic["uuid"],
            uuid.uuid5(stream["uuid"], name),
        )
    for message in saved["message"]:
        # Legacy native rows also carry provider IDs. Some are genuine sends;
        # others are received messages attributed to the importing account.
        # Both must share the provider's canonical identity before observation.
        server_url = (
            message["source"].get("server_url")
            or (message["provider"] or {}).get("provider_original_url")
            or (message["delivery"] or {}).get("original_url")
        )
        account = choose_account(
            accounts,
            project=message["project_id"],
            account_uuid=message["external_account_uuid"],
            host=urllib.parse.urlsplit(server_url).hostname if server_url else None,
            realm=None if server_url else message["provider_realm_uuid"],
        )
        canonical = uuid.uuid5(
            account["project_id"],
            str(uuid.uuid5(account["namespace"], str(message["provider_message_id"]))),
        )
        placement = uuid.uuid5(canonical, "zulip-placement")
        add("message", message["project_id"], message["uuid"], canonical)
        # v5 has one provider-owned placement per canonical message.
        if len(message["placements"]) != 1:
            raise RuntimeError("Legacy imported message has multiple placements")
        for old in message["placements"]:
            add("placement", message["project_id"], old["uuid"], placement)
            add(
                "public",
                message["project_id"],
                old["legacy_public_uuid"] or old["uuid"],
                placement,
            )
    return mapping, streams


def _cascade_definition(definition):
    definition = re.sub(
        r" ON UPDATE (?:NO ACTION|RESTRICT|CASCADE|SET NULL(?: \([^)]*\))?|SET DEFAULT)",
        "",
        definition,
    )
    definition = re.sub(
        r" (?:NOT )?DEFERRABLE(?: INITIALLY (?:DEFERRED|IMMEDIATE))?", "", definition
    )
    position = definition.find(" ON DELETE ")
    if position < 0:
        position = len(definition)
    return (
        definition[:position]
        + " ON UPDATE CASCADE"
        + definition[position:]
        + " DEFERRABLE INITIALLY DEFERRED"
    )


def _merge_bindings(session, project, streams, keeper):
    bindings = session.execute(
        "SELECT * FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=ANY(%s) ORDER BY user_uuid,active DESC,membership_generation DESC,uuid",
        (project, streams),
    ).fetchall()
    grouped = collections.defaultdict(list)
    for binding in bindings:
        grouped[binding["user_uuid"]].append(binding)
    for user, members in grouped.items():
        selected = next(
            (row for row in members if row["stream_uuid"] == keeper), members[0]
        )
        active = [row for row in members if row["active"]]
        generation = (active[0] if active else selected)["membership_generation"]
        # Only the current, active membership's flags cross into the shared
        # conversation. Historical generations retain their access boundary.
        for member in members:
            if member["active"]:
                for table in (
                    "messenger_user_message_states",
                    "messenger_user_message_bindings",
                ):
                    session.execute(
                        sql.SQL(
                            "UPDATE {} current SET membership_generation=%s FROM messenger_message_placements placement WHERE current.project_id=%s AND current.user_uuid=%s AND current.membership_generation=%s AND placement.project_id=current.project_id AND placement.uuid=current.placement_uuid AND placement.stream_uuid=%s"
                        ).format(sql.Identifier(table)),
                        (
                            generation,
                            project,
                            user,
                            member["membership_generation"],
                            member["stream_uuid"],
                        ),
                    )
        preference = max(members, key=lambda row: row["notification_updated_at"])
        session.execute(
            "UPDATE messenger_stream_bindings SET active=%s,membership_generation=%s,notification_mode=%s,notification_updated_at=%s WHERE uuid=%s",
            (
                bool(active),
                generation,
                preference["notification_mode"],
                preference["notification_updated_at"],
                selected["uuid"],
            ),
        )
        # Drafts reference membership by its natural key, with cascading delete.
        # Move them before removing redundant memberships.
        session.execute(
            "UPDATE m_workspace_drafts SET stream_uuid=%s WHERE project_id=%s AND user_uuid=%s AND stream_uuid=ANY(%s)",
            (keeper, project, user, streams),
        )
        for member in members:
            if member["uuid"] != selected["uuid"]:
                session.execute(
                    "DELETE FROM messenger_stream_bindings WHERE uuid=%s",
                    (member["uuid"],),
                )


def _merge_folder_items(session, project, streams):
    rows = session.execute(
        "SELECT * FROM messenger_folder_items WHERE project_id=%s AND stream_uuid=ANY(%s) ORDER BY user_uuid,folder_uuid,order_index NULLS LAST,uuid",
        (project, streams),
    ).fetchall()
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[(row["user_uuid"], row["folder_uuid"])].append(row)
    for members in grouped.values():
        selected = members[0]
        pinned = [row["pinned_at"] for row in members if row["pinned_at"] is not None]
        session.execute(
            "UPDATE messenger_folder_items SET pinned_at=%s,automatic=%s WHERE project_id=%s AND user_uuid=%s AND uuid=%s",
            (
                max(pinned) if pinned else None,
                all(row["automatic"] for row in members),
                project,
                selected["user_uuid"],
                selected["uuid"],
            ),
        )
        for member in members[1:]:
            session.execute(
                "DELETE FROM messenger_folder_items WHERE project_id=%s AND user_uuid=%s AND uuid=%s",
                (project, member["user_uuid"], member["uuid"]),
            )


def _merge_topic_preferences(session, project, topics):
    rows = session.execute(
        "SELECT * FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=ANY(%s) ORDER BY user_uuid,updated_at DESC,uuid",
        (project, topics),
    ).fetchall()
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[row["user_uuid"]].append(row)
    for members in grouped.values():
        selected = members[0]
        session.execute(
            "UPDATE messenger_user_topic_bindings SET summary_has_new_messages=%s WHERE uuid=%s",
            (any(row["summary_has_new_messages"] for row in members), selected["uuid"]),
        )
        for member in members[1:]:
            session.execute(
                "DELETE FROM messenger_user_topic_bindings WHERE uuid=%s",
                (member["uuid"],),
            )
    # Journals retain every generated summary; the single scheduling slot keeps
    # the most recent job when two old per-reader topics become one topic.
    jobs = session.execute(
        "SELECT topic_uuid FROM m_workspace_topic_summary_jobs WHERE project_id=%s AND topic_uuid=ANY(%s) ORDER BY updated_at DESC,topic_uuid",
        (project, topics),
    ).fetchall()
    for job in jobs[1:]:
        session.execute(
            "DELETE FROM m_workspace_topic_summary_jobs WHERE topic_uuid=%s",
            (job["topic_uuid"],),
        )


def merge_resources(session, mapping):
    """Coalesce legacy per-account copies before changing their primary keys."""
    for kind in ("stream", "topic"):
        table = ROOTS[kind]
        grouped = collections.defaultdict(set)
        for (item_kind, project, old), new in mapping.items():
            if item_kind == kind:
                grouped[(project, new)].add(uuid.UUID(old))
        references = session.execute(
            """
            SELECT DISTINCT child.relname AS table_name,source.attname AS column_name
            FROM pg_constraint constraint_row
            JOIN pg_class child ON child.oid=constraint_row.conrelid
            CROSS JOIN LATERAL unnest(constraint_row.conkey,constraint_row.confkey) keys(source_num,target_num)
            JOIN pg_attribute source ON source.attrelid=constraint_row.conrelid AND source.attnum=keys.source_num
            JOIN pg_attribute target ON target.attrelid=constraint_row.confrelid AND target.attnum=keys.target_num
            WHERE constraint_row.contype='f' AND constraint_row.confrelid=%s::regclass AND target.attname='uuid'
        """,
            (table,),
        ).fetchall()
        for (project, new), old_ids in grouped.items():
            existing = session.execute(
                sql.SQL(
                    "SELECT uuid FROM {} WHERE project_id=%s AND uuid=ANY(%s)"
                ).format(sql.Identifier(table)),
                (project, list(old_ids | {uuid.UUID(new)})),
            ).fetchall()
            ids = sorted((row["uuid"] for row in existing), key=str)
            if len(ids) < 2:
                continue
            keeper = uuid.UUID(new) if uuid.UUID(new) in ids else ids[0]
            losers = [value for value in ids if value != keeper]
            if kind == "stream":
                # Old archived projections must not hide an active conversation.
                session.execute(
                    "UPDATE messenger_streams SET is_archived=("
                    "SELECT bool_and(is_archived) FROM messenger_streams "
                    "WHERE project_id=%s AND uuid=ANY(%s)) "
                    "WHERE project_id=%s AND uuid=%s",
                    (project, ids, project, keeper),
                )
                # Derived caches are rebuilt from per-message facts after import.
                for cache in (
                    "messenger_message_counter_totals",
                    "messenger_message_counter_contributions",
                    "messenger_message_counter_changes",
                    "bridge_zulip_v5_message_counter_changes",
                ):
                    session.execute(
                        sql.SQL(
                            "DELETE FROM {} WHERE project_id=%s AND stream_uuid=ANY(%s)"
                        ).format(sql.Identifier(cache)),
                        (project, ids),
                    )
                _merge_bindings(session, project, ids, keeper)
                _merge_folder_items(session, project, ids)
            else:
                session.execute(
                    "DELETE FROM messenger_message_counter_totals WHERE project_id=%s AND topic_uuid=ANY(%s)",
                    (project, ids),
                )
                _merge_topic_preferences(session, project, ids)
            for reference in references:
                session.execute(
                    sql.SQL(
                        "UPDATE {} SET {}=%s WHERE project_id=%s AND {}=ANY(%s)"
                    ).format(
                        sql.Identifier(reference["table_name"]),
                        sql.Identifier(reference["column_name"]),
                        sql.Identifier(reference["column_name"]),
                    ),
                    (keeper, project, losers),
                )
            session.execute(
                sql.SQL("DELETE FROM {} WHERE project_id=%s AND uuid=ANY(%s)").format(
                    sql.Identifier(table)
                ),
                (project, losers),
            )


def rekey(session, mapping):
    """Change keys and their FK graph atomically, restoring the exact schema."""
    session.execute(
        "CREATE TEMP TABLE workspace_upgrade_ids(kind text,project_id uuid,old_uuid uuid,new_uuid uuid) ON COMMIT DROP"
    )
    for (kind, project, old), new in mapping.items():
        session.execute(
            "INSERT INTO workspace_upgrade_ids VALUES (%s,%s,%s,%s)",
            (kind, project, old, new),
        )
    session.execute("CREATE INDEX ON workspace_upgrade_ids(old_uuid,project_id)")
    triggers = session.execute(
        "SELECT c.relname,t.tgname,t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal"
    ).fetchall()
    for table in {row["relname"] for row in triggers}:
        session.execute(
            sql.SQL("ALTER TABLE {} DISABLE TRIGGER USER").format(sql.Identifier(table))
        )
    constraints = session.execute(
        "SELECT c.conrelid::regclass::text AS table_name,c.conname,pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='f' AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) AND a.attgenerated<>'')"
    ).fetchall()
    for row in constraints:
        session.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                sql.Identifier(row["table_name"]), sql.Identifier(row["conname"])
            )
        )
        session.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} ").format(
                sql.Identifier(row["table_name"]), sql.Identifier(row["conname"])
            )
            + sql.SQL(_cascade_definition(row["definition"]))
        )
    session.execute("SET CONSTRAINTS ALL DEFERRED")
    merge_resources(session, mapping)
    for kind, table in ROOTS.items():
        # A collision requires an explicit merge policy, never an implicit delete.
        collision = session.execute(
            sql.SQL(
                "SELECT 1 FROM {table} current JOIN workspace_upgrade_ids ids ON ids.kind=%s AND current.uuid=ids.new_uuid AND ids.old_uuid<>ids.new_uuid {scope} JOIN {table} old ON old.uuid=ids.old_uuid {old_scope} LIMIT 1"
            ).format(
                table=sql.Identifier(table),
                scope=sql.SQL(
                    "AND current.project_id=ids.project_id" if kind != "user" else ""
                ),
                old_scope=sql.SQL(
                    "AND old.project_id=ids.project_id" if kind != "user" else ""
                ),
            ),
            (kind,),
        ).fetchone()
        if collision is not None:
            raise RuntimeError(
                "Legacy Zulip conversion would merge existing identities"
            )
        session.execute(
            sql.SQL(
                "UPDATE {table} current SET uuid=ids.new_uuid FROM workspace_upgrade_ids ids WHERE ids.kind=%s AND current.uuid=ids.old_uuid {scope}"
            ).format(
                table=sql.Identifier(table),
                scope=sql.SQL(
                    "AND current.project_id=ids.project_id" if kind != "user" else ""
                ),
            ),
            (kind,),
        )
    # Logical references which intentionally do not carry database foreign keys.
    for table, column, kind in (
        ("m_external_accounts_v2", "owner_user_uuid", "user"),
        ("messenger_message_changes", "message_uuid", "message"),
        ("bridge_zulip_v5_message_changes", "message_uuid", "message"),
        ("bridge_zulip_v5_topic_changes", "topic_uuid", "topic"),
        ("m_workspace_topic_summary_journal", "boundary_message_uuid", "placement"),
        ("messenger_fanout_roots", "next_user_uuid", "user"),
        ("messenger_fanout_batch_tasks", "start_user_uuid", "user"),
        ("messenger_fanout_batch_tasks", "end_user_uuid", "user"),
    ):
        session.execute(
            sql.SQL(
                "UPDATE {table} current SET {column}=ids.new_uuid FROM workspace_upgrade_ids ids WHERE ids.kind=%s AND current.{column}=ids.old_uuid {scope}"
            ).format(
                table=sql.Identifier(table),
                column=sql.Identifier(column),
                scope=sql.SQL(
                    "AND current.project_id=ids.project_id" if kind != "user" else ""
                ),
            ),
            (kind,),
        )
    rewrite_json(session)
    session.execute(
        "UPDATE messenger_streams current SET private_index='zulip:'||current.uuid::text FROM workspace_upgrade_ids ids WHERE ids.kind='stream' AND current.project_id=ids.project_id AND current.uuid=ids.new_uuid AND current.private"
    )
    session.execute("SET CONSTRAINTS ALL IMMEDIATE")
    for row in constraints:
        session.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                sql.Identifier(row["table_name"]), sql.Identifier(row["conname"])
            )
        )
        session.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} ").format(
                sql.Identifier(row["table_name"]), sql.Identifier(row["conname"])
            )
            + sql.SQL(row["definition"])
        )
    actions = {
        "O": "ENABLE",
        "A": "ENABLE ALWAYS",
        "R": "ENABLE REPLICA",
        "D": "DISABLE",
    }
    for row in triggers:
        session.execute(
            sql.SQL(
                "ALTER TABLE {} " + actions[row["tgenabled"]] + " TRIGGER {}"
            ).format(sql.Identifier(row["relname"]), sql.Identifier(row["tgname"]))
        )


def rewrite_json(session):
    session.execute(r"""
CREATE FUNCTION pg_temp.workspace_upgrade_uuid(value text,project uuid,wanted text DEFAULT NULL)
RETURNS uuid LANGUAGE sql AS $$
    SELECT new_uuid FROM workspace_upgrade_ids
    WHERE old_uuid=value::uuid AND (project_id=project OR project_id IS NULL)
      AND (wanted IS NULL OR kind=wanted)
    ORDER BY (project_id=project) DESC NULLS LAST,(kind='public') DESC LIMIT 1
$$;
CREATE FUNCTION pg_temp.workspace_upgrade_links(value text,project uuid)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE found text[]; replacement uuid; result text := value; wanted text;
BEGIN
    FOR found IN SELECT regexp_matches(value,
        '(urn(?::|%3a|%253a)(user|stream|topic|message|quote|forward)(?::|%3a|%253a)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))(?![0-9a-f-])','gi') LOOP
        wanted := CASE WHEN lower(found[2]) IN ('message','quote','forward') THEN 'public' ELSE lower(found[2]) END;
        replacement := pg_temp.workspace_upgrade_uuid(found[3],project,wanted);
        IF replacement IS NOT NULL THEN
            result := replace(result,found[1],replace(found[1],found[3],replacement::text));
        END IF;
    END LOOP;
    RETURN result;
END;
$$;
CREATE FUNCTION pg_temp.workspace_upgrade_json(value jsonb,project uuid,identity boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb; item record; element jsonb; text_value text; key_value text; replacement uuid;
BEGIN
    CASE jsonb_typeof(value)
    WHEN 'object' THEN
        result := '{}'::jsonb;
        FOR item IN SELECT * FROM jsonb_each(value) LOOP
            key_value := item.key;
            IF key_value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                replacement := pg_temp.workspace_upgrade_uuid(key_value,project);
                key_value := COALESCE(replacement::text,key_value);
            END IF;
            result := result || jsonb_build_object(key_value,
                pg_temp.workspace_upgrade_json(item.value,project,
                    item.key='uuid' OR item.key LIKE '%\_uuid' OR item.key LIKE '%\_uuids'
                    OR item.key IN ('reaction_users','member_uuids')));
        END LOOP;
        RETURN result;
    WHEN 'array' THEN
        result := '[]'::jsonb;
        FOR element IN SELECT * FROM jsonb_array_elements(value) LOOP
            result := result || jsonb_build_array(pg_temp.workspace_upgrade_json(element,project,identity));
        END LOOP;
        RETURN result;
    WHEN 'string' THEN
        text_value := value #>> '{}';
        IF identity AND text_value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            replacement := pg_temp.workspace_upgrade_uuid(text_value,project);
            RETURN to_jsonb(COALESCE(replacement::text,text_value));
        END IF;
        RETURN to_jsonb(pg_temp.workspace_upgrade_links(text_value,project));
    ELSE RETURN value;
    END CASE;
END;
$$;
CREATE FUNCTION pg_temp.workspace_upgrade_text(value text,project uuid)
RETURNS text LANGUAGE sql AS $$
    SELECT pg_temp.workspace_upgrade_json(value::jsonb,project)::text
$$;
CREATE FUNCTION pg_temp.workspace_upgrade_scope(value text,project uuid)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE found text[]; replacement uuid; result text := value;
BEGIN
    FOR found IN SELECT DISTINCT regexp_matches(value,
        '([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![0-9a-fA-F-])','g') LOOP
        replacement := pg_temp.workspace_upgrade_uuid(found[1],project);
        IF replacement IS NOT NULL THEN result := replace(result,found[1],replacement::text); END IF;
    END LOOP;
    RETURN result;
END;
$$;
    """)
    for table, columns in JSON_COLUMNS.items():
        scoped = table != "m_workspace_event_recipient_payloads_v1"
        for column in columns:
            project = sql.SQL(
                "project_id"
                if scoped
                else "(SELECT project_id FROM m_workspace_broadcast_message_events_v1 WHERE uuid=event_uuid)"
            )
            snapshots = session.execute(
                sql.SQL(
                    "SELECT ctid::text AS row_id,{column} AS value,{project} AS project_id FROM {table} WHERE {column}::text ILIKE '%urn:forward:%'"
                ).format(
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                    project=project,
                )
            ).fetchall()
            for row in snapshots:
                converted = rewrite_snapshots(session, row["value"], row["project_id"])
                if converted != row["value"]:
                    session.execute(
                        sql.SQL("UPDATE {} SET {}=%s::jsonb WHERE ctid=%s::tid").format(
                            sql.Identifier(table), sql.Identifier(column)
                        ),
                        (json.dumps(converted), row["row_id"]),
                    )
            session.execute(
                sql.SQL(
                    "UPDATE {table} SET {column}=pg_temp.workspace_upgrade_json({column},{project},{identity}) WHERE {column} IS NOT NULL AND {column}::text ~ %s"
                ).format(
                    table=sql.Identifier(table),
                    column=sql.Identifier(column),
                    project=project,
                    identity=sql.SQL("true" if column == "reaction_users" else "false"),
                ),
                ("[0-9a-fA-F]{8}-",),
            )

    # Identity conversion is not a user edit. Triggers are disabled by rekey(),
    # so refresh the content digest explicitly while retaining the edit time.
    session.execute("""UPDATE messenger_messages SET content_hash=
        encode(digest(convert_to(payload->>'content','UTF8'),'sha256'),'hex')
        WHERE payload->>'kind'='markdown' AND content_hash IS DISTINCT FROM
        encode(digest(convert_to(payload->>'content','UTF8'),'sha256'),'hex')""")

    for table in (
        "messenger_domain_outbox_events",
        "messenger_projection_tasks",
    ):
        session.execute(
            sql.SQL(
                "UPDATE {} SET scope_key=pg_temp.workspace_upgrade_scope(scope_key,project_id)"
            ).format(sql.Identifier(table))
        )
    # Bootstrap has stopped every writer. Old process leases must not survive
    # the identity cutover, particularly when two per-reader scopes become one.
    session.execute("""DELETE FROM messenger_projection_scope_leases
        WHERE project_id IN (SELECT project_id FROM workspace_upgrade_ids
                             WHERE project_id IS NOT NULL)""")
    session.execute("""UPDATE messenger_projection_tasks
        SET ordering_key=pg_temp.workspace_upgrade_scope(ordering_key,project_id),
            progress_uuid=COALESCE(pg_temp.workspace_upgrade_uuid(progress_uuid::text,project_id),progress_uuid)
        WHERE ordering_key ~ '[0-9a-fA-F]{8}-' OR progress_uuid IS NOT NULL""")
    session.execute("""UPDATE m_workspace_broadcast_message_events_v1
        SET entity_uuid=COALESCE(pg_temp.workspace_upgrade_uuid(entity_uuid::text,project_id),entity_uuid)
        WHERE entity_uuid IS NOT NULL""")


def rewrite_snapshots(session, value, project):
    if isinstance(value, dict):
        return {
            key: rewrite_snapshots(session, item, project)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [rewrite_snapshots(session, item, project) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        target = urllib.parse.urlsplit(match[0])
        query = urllib.parse.parse_qsl(target.query, keep_blank_values=True)
        converted = []
        for key, item in query:
            if key == "snapshot":
                try:
                    decoded = base64.b64decode(
                        item + "=" * (-len(item) % 4), altchars=b"-_", validate=True
                    ).decode()
                except (ValueError, UnicodeDecodeError):
                    return match[0]
                decoded = rewrite_snapshots(session, decoded, project)
                decoded = session.execute(
                    "SELECT pg_temp.workspace_upgrade_links(%s,%s) AS value",
                    (decoded, project),
                ).fetchone()["value"]
                item = base64.urlsafe_b64encode(decoded.encode()).decode().rstrip("=")
            converted.append((key, item))
        return urllib.parse.urlunsplit(
            target._replace(query=urllib.parse.urlencode(converted))
        )

    return re.sub(
        r"urn:forward:[0-9a-f-]{36}\?[^\s)<>]+", replace, value, flags=re.IGNORECASE
    )


def prepare(session):
    if (
        session.execute(
            "SELECT to_regclass('public.workspace_zulip_v5_legacy_state') AS name"
        ).fetchone()["name"]
        is None
    ):
        return
    saved = state(session)
    if not saved["account"]:
        session.execute("DROP TABLE workspace_zulip_v5_legacy_state")
        session.execute("DROP TABLE IF EXISTS workspace_zulip_v5_credential_handoff")
        return
    accounts = account_scopes(session, saved)
    for account in accounts.values():
        if (
            account["external_account_id"] is None
            and account["provider_user_id"] is not None
        ):
            session.execute(
                "UPDATE m_external_accounts_v2 SET external_account_id=%s WHERE uuid=%s",
                (account["host"] + ":" + account["provider_user_id"], account["uuid"]),
            )
    mapping, _streams = make_mapping(saved, accounts)
    applied = {
        (item["kind"], item["project_id"], item["old_uuid"]): item["new_uuid"]
        for item in saved["mapping"]
    }
    mapping = {key: value for key, value in mapping.items() if key not in applied}
    if mapping:
        rekey(session, mapping)
    session.execute(
        "INSERT INTO workspace_zulip_v5_legacy_state VALUES ('phase','rekeyed','{}'::jsonb) ON CONFLICT DO NOTHING"
    )
    for (kind, project, old), new in mapping.items():
        session.execute(
            "INSERT INTO workspace_zulip_v5_legacy_state VALUES ('mapping',%s,%s::jsonb)",
            (
                kind + ":" + str(project) + ":" + old,
                json.dumps(
                    {
                        "kind": kind,
                        "project_id": project,
                        "old_uuid": old,
                        "new_uuid": new,
                    }
                ),
            ),
        )
    session.execute("DROP TABLE IF EXISTS workspace_zulip_v5_credential_handoff")
