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

import hashlib
import json
import re
import urllib.parse
import uuid as sys_uuid

from restalchemy.storage.sql import migrations


TABLES = (
    "messenger_streams",
    "messenger_stream_bindings",
    "messenger_topics",
    "messenger_user_topic_bindings",
    "messenger_messages",
    "messenger_user_message_states",
    "messenger_message_reaction_facts",
    "m_workspace_files",
)
ROOTS = ("messenger_streams", "messenger_topics", "messenger_messages")


def _views(session, upgrade=True):
    affected = session.execute(
        """
        WITH RECURSIVE affected AS (
            SELECT rewrite.ev_class AS oid FROM pg_depend dependency
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            JOIN pg_attribute column_ ON column_.attrelid=dependency.refobjid AND column_.attnum=dependency.refobjsubid
            WHERE dependency.refobjid=ANY(%s::regclass[]) AND column_.attname=ANY(%s::text[])
            UNION
            SELECT rewrite.ev_class FROM affected parent
            JOIN pg_depend dependency ON dependency.refobjid=parent.oid
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            WHERE rewrite.ev_class<>parent.oid
        ) SELECT oid::regclass::text AS name FROM affected
    """,
        (
            list(ROOTS) if upgrade else [*TABLES, "m_external_accounts_v2"],
            ["source"] if upgrade else ["provider_uuid", "provider_identity"],
        ),
    ).fetchall()
    dropped = {row["name"] for row in affected}
    views = session.execute("""
        WITH RECURSIVE dependencies AS (
            SELECT c.oid, 0 AS depth FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='v'
            UNION ALL
            SELECT r.ev_class, parent.depth+1 FROM dependencies parent
            JOIN pg_depend d ON d.refobjid=parent.oid
            JOIN pg_rewrite r ON r.oid=d.objid
            WHERE r.ev_class<>parent.oid
        )
        SELECT oid::regclass::text AS name, pg_get_viewdef(oid) AS definition
        FROM dependencies GROUP BY oid ORDER BY max(depth), name
    """).fetchall()
    return [{**view, "drop": view["name"] in dropped} for view in views]


def _restore_functions(session, functions, upgrade):
    existing = {
        row["signature"]
        for row in session.execute("""SELECT p.oid::regprocedure::text AS signature
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f'""").fetchall()
    }
    for function in functions:
        definition = _definition(function["definition"], upgrade)
        if (
            definition != function["definition"]
            or function["signature"] not in existing
        ):
            session.execute(definition)


def _disable_triggers(session):
    triggers = session.execute("""SELECT c.relname AS table_name,t.tgname AS name,t.tgenabled AS enabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgenabled<>'D'
          AND t.tgname<>'messenger_assign_provider_origin'""").fetchall()
    for trigger in triggers:
        session.execute(
            f"ALTER TABLE {trigger['table_name']} DISABLE TRIGGER {trigger['name']}"
        )
    return triggers


def _restore_triggers(session, triggers):
    for trigger in triggers:
        mode = {"O": "ENABLE", "A": "ENABLE ALWAYS", "R": "ENABLE REPLICA"}[
            trigger["enabled"]
        ]
        session.execute(
            f"ALTER TABLE {trigger['table_name']} {mode} TRIGGER {trigger['name']}"
        )


def _definition(text, upgrade):
    if upgrade:
        text = re.sub(
            r"'source',\s*(\w+)\.source\b",
            r"'provider_uuid', \1.provider_uuid, 'provider_identity', \1.provider_identity",
            text,
        )
        text = re.sub(
            r"\b(\w+)\.source\b", r"\1.provider_uuid, \1.provider_identity", text
        )
        # Legacy account IDs included hostname; the provider FK now owns it.
        text = re.sub(
            r"regexp_replace\(\(account.external_account_id\)::text, ':[^']+'::text, ''::text\)",
            "(SELECT hostname FROM messenger_providers WHERE uuid=account.provider_uuid)",
            text,
        )
        text = text.replace(
            "((author.external_account_id)::text = (((SELECT hostname FROM messenger_providers WHERE uuid=account.provider_uuid) || ':'::text) || (message.sender_id)::text))",
            "(author.provider_uuid=account.provider_uuid AND author.external_account_id=message.sender_id::text)",
        )
        text = text.replace(
            "regexp_replace(external_account_id, ':[^:]+$', '')",
            "(SELECT hostname FROM messenger_providers WHERE uuid=provider_uuid)",
        )
        text = text.replace(
            "OLD.external_account_id=message.hostname || ':' || reaction.user_id::text",
            "OLD.provider_uuid=(SELECT uuid FROM messenger_providers WHERE hostname=message.hostname) AND OLD.external_account_id=reaction.user_id::text",
        )
    else:
        text = re.sub(
            r"\(\s*SELECT messenger_providers.hostname\s+FROM messenger_providers\s+WHERE \(messenger_providers.uuid = account.provider_uuid\)\)",
            "regexp_replace((account.external_account_id)::text, ':[^:]+$'::text, ''::text)",
            text,
        )
        text = re.sub(
            r"\(author.provider_uuid = account.provider_uuid\) AND \(?\(?author.external_account_id\)?::text = \(message.sender_id\)::text\)?",
            "(author.external_account_id=regexp_replace(account.external_account_id, ':[^:]+$', '') || ':' || message.sender_id::text)",
            text,
        )
        text = re.sub(
            r"'provider_uuid',\s*(\w+)\.provider_uuid,\s*'provider_identity',\s*\1.provider_identity",
            r"'source', \1.source",
            text,
        )
        text = re.sub(
            r"\b(\w+)\.provider_uuid,\s*\1.provider_identity", r"\1.source", text
        )
        text = text.replace(
            "(SELECT hostname FROM messenger_providers WHERE uuid=account.provider_uuid)",
            "regexp_replace((account.external_account_id)::text, ':[^:]+$'::text, ''::text)",
        )
        text = text.replace(
            "(author.provider_uuid=account.provider_uuid AND author.external_account_id=message.sender_id::text)",
            "(author.external_account_id=regexp_replace(account.external_account_id, ':[^:]+$', '') || ':' || message.sender_id::text)",
        )
        text = text.replace(
            "(SELECT hostname FROM messenger_providers WHERE uuid=provider_uuid)",
            "regexp_replace(external_account_id, ':[^:]+$', '')",
        )
        text = text.replace(
            "OLD.provider_uuid=(SELECT uuid FROM messenger_providers WHERE hostname=message.hostname) AND OLD.external_account_id=reaction.user_id::text",
            "OLD.external_account_id=message.hostname || ':' || reaction.user_id::text",
        )
    return text


ORIGIN_TRIGGER = r"""
CREATE FUNCTION messenger_assign_provider_origin() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE origin record;
BEGIN
    IF TG_TABLE_NAME='messenger_streams' AND NEW.source_name='zulip' THEN
        SELECT account.provider_uuid, COALESCE(channel.stream_id::text, 'dm:'||channel.stream_uuid::text) AS identity
        INTO origin FROM bridge_zulip_v5_channels channel
        JOIN m_external_accounts_v2 account ON account.uuid=channel.external_account_uuid
        WHERE account.project_id=NEW.project_id AND channel.stream_uuid=NEW.uuid
        ORDER BY account.uuid LIMIT 1;
    ELSIF TG_TABLE_NAME='messenger_topics' AND NEW.source_name='zulip' THEN
        SELECT stream.provider_uuid, stream.provider_identity||':'||observed.name AS identity
        INTO origin FROM messenger_streams stream JOIN bridge_zulip_v5_topics observed ON observed.uuid=NEW.uuid
        WHERE stream.project_id=NEW.project_id AND stream.uuid=NEW.stream_uuid AND stream.provider_uuid IS NOT NULL;
    ELSIF TG_TABLE_NAME='messenger_stream_bindings' THEN
        SELECT stream.provider_uuid, stream.provider_identity||':'||account.external_account_id AS identity
        INTO origin FROM messenger_streams stream JOIN m_external_accounts_v2 account
          ON account.project_id=stream.project_id AND account.provider_uuid=stream.provider_uuid
         AND account.provider='zulip' AND account.owner_user_uuid=NEW.user_uuid AND account.external_account_id IS NOT NULL
        WHERE stream.project_id=NEW.project_id AND stream.uuid=NEW.stream_uuid LIMIT 1;
    ELSIF TG_TABLE_NAME='messenger_user_topic_bindings' THEN
        SELECT stream.provider_uuid, COALESCE(topic.provider_identity,stream.provider_identity||':'||topic.name)||':'||account.external_account_id AS identity
        INTO origin FROM messenger_topics topic JOIN messenger_streams stream ON stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
        JOIN m_external_accounts_v2 account
          ON account.project_id=topic.project_id AND account.provider_uuid=stream.provider_uuid
         AND account.provider='zulip' AND account.owner_user_uuid=NEW.user_uuid AND account.external_account_id IS NOT NULL
        WHERE topic.project_id=NEW.project_id AND topic.uuid=NEW.topic_uuid LIMIT 1;
    ELSIF TG_TABLE_NAME='messenger_user_message_states' THEN
        IF NEW.zulip_source_uuid IS NULL THEN RETURN NEW; END IF;
        SELECT account.provider_uuid, message.message_id::text||':'||account.external_account_id AS identity
        INTO origin FROM bridge_zulip_v5_messages message
        JOIN m_external_accounts_v2 account ON account.uuid=message.external_account_uuid
        WHERE message.uuid=NEW.zulip_source_uuid AND message.project_id=NEW.project_id;
    ELSE
        RETURN NEW;
    END IF;
    IF origin.provider_uuid IS NOT NULL THEN
        NEW.source_name := 'zulip';
        NEW.provider_uuid := origin.provider_uuid;
        NEW.provider_identity := origin.identity;
    END IF;
    RETURN NEW;
END $$;
"""


def _origins(session):
    session.execute("""
        UPDATE messenger_streams stream SET provider_uuid=origin.provider_uuid,
            provider_identity=origin.identity
        FROM (SELECT DISTINCT ON (account.project_id,channel.stream_uuid)
              account.project_id,channel.stream_uuid,account.provider_uuid,
              COALESCE(channel.stream_id::text,'dm:'||channel.stream_uuid::text) AS identity
              FROM bridge_zulip_v5_channels channel JOIN m_external_accounts_v2 account
                ON account.uuid=channel.external_account_uuid ORDER BY account.project_id,channel.stream_uuid,account.uuid) origin
        WHERE stream.project_id=origin.project_id AND stream.uuid=origin.stream_uuid AND stream.source_name='zulip';
        UPDATE messenger_topics topic SET provider_uuid=stream.provider_uuid,
            provider_identity=stream.provider_identity||':'||observed.name
        FROM messenger_streams stream, bridge_zulip_v5_topics observed WHERE observed.uuid=topic.uuid AND stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
            AND topic.source_name='zulip' AND stream.provider_uuid IS NOT NULL;
        UPDATE messenger_messages message SET provider_uuid=origin.provider_uuid, provider_identity=origin.message_id::text
        FROM (SELECT DISTINCT ON (observed.project_id,observed.message_uuid)
              observed.project_id,observed.message_uuid,observed.message_id,account.provider_uuid
              FROM bridge_zulip_v5_messages observed JOIN m_external_accounts_v2 account
                ON account.uuid=observed.external_account_uuid ORDER BY observed.project_id,observed.message_uuid,account.uuid) origin
        WHERE message.project_id=origin.project_id AND message.uuid=messenger_uuid_v5(origin.project_id,origin.message_uuid::text)
            AND message.source_name='zulip';
        UPDATE messenger_stream_bindings binding SET source_name='zulip', provider_uuid=stream.provider_uuid,
            provider_identity=stream.provider_identity||':'||account.external_account_id
        FROM messenger_streams stream JOIN m_external_accounts_v2 account ON account.project_id=stream.project_id
            AND account.provider='zulip' AND account.provider_uuid=stream.provider_uuid AND account.external_account_id IS NOT NULL
        WHERE stream.project_id=binding.project_id AND stream.uuid=binding.stream_uuid AND account.owner_user_uuid=binding.user_uuid;
        UPDATE messenger_user_topic_bindings binding SET source_name='zulip', provider_uuid=stream.provider_uuid,
            provider_identity=COALESCE(topic.provider_identity,stream.provider_identity||':'||topic.name)||':'||account.external_account_id
        FROM messenger_topics topic JOIN messenger_streams stream ON stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
        JOIN m_external_accounts_v2 account ON account.project_id=topic.project_id
            AND account.provider='zulip' AND account.provider_uuid=stream.provider_uuid AND account.external_account_id IS NOT NULL
        WHERE topic.project_id=binding.project_id AND topic.uuid=binding.topic_uuid AND account.owner_user_uuid=binding.user_uuid;
        UPDATE messenger_user_message_states state SET source_name='zulip', provider_uuid=account.provider_uuid,
            provider_identity=message.message_id::text||':'||account.external_account_id
        FROM bridge_zulip_v5_messages message JOIN m_external_accounts_v2 account ON account.uuid=message.external_account_uuid
        WHERE message.uuid=state.zulip_source_uuid AND message.project_id=state.project_id;
        UPDATE messenger_message_reaction_facts fact SET source_name='zulip', provider_uuid=provider.uuid,
            provider_identity=message.message_id::text||':'||reaction.user_id::text||':'||fact.emoji_name
        FROM bridge_zulip_v5_reactions reaction JOIN bridge_zulip_v5_reaction_messages message ON message.uuid=reaction.reaction_message_uuid
            JOIN messenger_providers provider ON provider.hostname=message.hostname
        WHERE reaction.fact_uuid=fact.uuid AND fact.project_id=message.project_id;
        UPDATE m_workspace_files file SET source_name='zulip',provider_uuid=provider.uuid,
            provider_identity=messenger_uuid_v5(provider.uuid,link.zulip_url)::text
        FROM bridge_zulip_v5_file_links link JOIN messenger_providers provider ON provider.hostname=link.hostname
        WHERE link.file_uuid=file.uuid AND link.project_id=file.project_id
          AND file.uuid=messenger_uuid_v5(link.project_id,'zulip-file:'||link.hostname||':'||link.stream_uuid::text||':'||link.zulip_url);
    """)


SNAPSHOT_REWRITE = r"""
CREATE FUNCTION messenger_rewrite_provider_snapshot(value jsonb, project uuid, upgrade boolean)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb; item record; origin record; target uuid; account_hostname text; account_provider uuid; identity text;
BEGIN
    IF jsonb_typeof(value)='array' THEN
        SELECT COALESCE(jsonb_agg(messenger_rewrite_provider_snapshot(element,project,upgrade)),'[]'::jsonb)
        INTO result FROM jsonb_array_elements(value) element;
        RETURN result;
    ELSIF jsonb_typeof(value)<>'object' THEN
        RETURN value;
    END IF;
    project := COALESCE(project,(value->>'project_id')::uuid);
    result := '{}';
    FOR item IN SELECT * FROM jsonb_each(value) LOOP
        result := result || jsonb_build_object(item.key,messenger_rewrite_provider_snapshot(item.value,project,upgrade));
    END LOOP;
    IF upgrade AND jsonb_typeof(value->'source')='object' AND value->'source'->>'kind' IN ('native','zulip') THEN
        target := COALESCE(value->>'canonical_message_uuid',value->>'message_uuid',value->>'resource_uuid',value->>'uuid')::uuid;
        SELECT candidate.provider_uuid,candidate.provider_identity INTO origin FROM (
            SELECT message.provider_uuid,message.provider_identity FROM messenger_messages message
              LEFT JOIN messenger_message_placements placement ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
              WHERE message.project_id=project AND (message.uuid=target OR placement.uuid=target)
            UNION ALL SELECT provider_uuid,provider_identity FROM messenger_topics WHERE project_id=project AND uuid=COALESCE(target,(value->>'topic_uuid')::uuid)
            UNION ALL SELECT provider_uuid,provider_identity FROM messenger_streams WHERE project_id=project AND uuid=COALESCE(target,(value->>'stream_uuid')::uuid)
            UNION ALL SELECT provider_uuid,provider_identity FROM m_workspace_files WHERE project_id=project AND uuid=target
        ) candidate LIMIT 1;
        result := (result-'source') || jsonb_build_object('provider_uuid',origin.provider_uuid,'provider_identity',origin.provider_identity);
    ELSIF NOT upgrade AND value ? 'source_name' AND value ? 'provider_uuid' THEN
        result := (result-'provider_uuid'-'provider_identity') || jsonb_build_object('source',jsonb_build_object('kind',value->>'source_name'));
    END IF;

    IF value ? 'external_account_id' AND jsonb_typeof(value->'provider')='string' THEN
        identity := value->>'external_account_id';
        IF upgrade THEN
            IF value->>'provider'='zulip' AND identity IS NOT NULL THEN
                account_hostname := regexp_replace(identity, ':[^:]+$', '');
                identity := regexp_replace(identity, '^.*:', '');
            ELSE
                account_hostname := trim(both '[]' from lower(substring(value#>>'{settings,server_url}' FROM '^[a-zA-Z][a-zA-Z0-9+.-]*://(\[[^]]+\]|[^/:]+)')));
            END IF;
            IF account_hostname IS NOT NULL THEN
                account_provider := messenger_uuid_v5('6ba7b810-9dad-11d1-80b4-00c04fd430c8',account_hostname);
                INSERT INTO messenger_providers(uuid,hostname) VALUES (account_provider,account_hostname) ON CONFLICT DO NOTHING;
                SELECT uuid INTO account_provider FROM messenger_providers WHERE messenger_providers.hostname=account_hostname;
            ELSE
                SELECT provider_uuid INTO account_provider FROM m_external_accounts_v2 WHERE uuid=(value->>'uuid')::uuid;
            END IF;
            result := result || jsonb_build_object('provider_uuid',account_provider,'external_account_id',identity);
        ELSE
            SELECT messenger_providers.hostname INTO account_hostname FROM messenger_providers WHERE uuid=(value->>'provider_uuid')::uuid;
            IF value->>'provider'='zulip' AND identity IS NOT NULL THEN
                identity := account_hostname||':'||identity;
            END IF;
            result := (result-'provider_uuid') || jsonb_build_object('external_account_id',identity);
        END IF;
    END IF;
    IF upgrade AND jsonb_typeof(value->'old_source')='object' THEN
        SELECT message.provider_uuid,message.provider_identity INTO origin FROM messenger_messages message
        LEFT JOIN messenger_message_placements placement ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
        WHERE message.project_id=project AND (message.uuid=(value->>'old_message_uuid')::uuid OR placement.uuid=(value->>'old_message_uuid')::uuid)
        LIMIT 1;
        result := (result-'old_source') || jsonb_build_object('old_provider_uuid',origin.provider_uuid,'old_provider_identity',origin.provider_identity);
    ELSIF NOT upgrade AND value ? 'old_source_name' AND value ? 'old_provider_uuid' THEN
        result := (result-'old_provider_uuid'-'old_provider_identity') || jsonb_build_object('old_source',jsonb_build_object('kind',value->>'old_source_name'));
    END IF;
    RETURN result;
END $$;
"""


def _snapshots(session, upgrade):
    session.execute(SNAPSHOT_REWRITE)
    columns = (
        ("m_workspace_events", "payload"),
        ("m_workspace_broadcast_message_events_v1", "payload"),
        ("m_workspace_event_recipient_payloads_v1", "payload"),
        ("messenger_domain_outbox_events", "payload"),
        ("messenger_projection_tasks", "payload"),
        ("messenger_mutation_commands", "payload"),
        ("messenger_mutation_commands", "result"),
        ("messenger_publication_outbox", "payload"),
        ("messenger_publication_outbox", "recipient_payloads"),
    )
    for table, column in columns:
        fields = {
            row["column_name"]
            for row in session.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s",
                (table,),
            ).fetchall()
        }
        if column not in fields:
            continue
        project = "project_id" if "project_id" in fields else "NULL::uuid"
        session.execute(
            f"UPDATE {table} SET {column}=messenger_rewrite_provider_snapshot({column},{project},%s) WHERE {column} @? '$.**.source.kind' OR {column} @? '$.**.old_source.kind' OR {column} @? '$.**.external_account_id'"
            if upgrade
            else f"UPDATE {table} SET {column}=messenger_rewrite_provider_snapshot({column},{project},%s) WHERE {column} @? '$.**.provider_uuid'",
            (upgrade,),
        )
    session.execute(
        "DROP FUNCTION messenger_rewrite_provider_snapshot(jsonb,uuid,boolean)"
    )


def _account_revisions(session, upgrade):
    accounts = session.execute("""SELECT account.*,provider.hostname FROM m_external_accounts_v2 account
        JOIN messenger_providers provider ON provider.uuid=account.provider_uuid
        WHERE account.provider='zulip'""").fetchall()
    for account in accounts:
        identity = account["external_account_id"]
        original_identity = (
            (account["hostname"] + ":" + identity)
            if identity is not None and account["provider"] == "zulip"
            else identity
        )
        common = [
            str(account["project_id"]),
            str(account["owner_user_uuid"]),
            account["provider"],
        ]
        settings = account["settings"]
        connection = {
            name: settings.get(name) for name in ("server_url", "email", "api_key")
        }
        for table, tail in (
            ("bridge_zulip_v5_tasks", connection),
            ("bridge_zulip_v5_subscriptions", settings["server_url"].rstrip("/")),
        ):
            old = hashlib.sha256(
                json.dumps(common + [original_identity, tail], sort_keys=True).encode()
            ).hexdigest()
            new = hashlib.sha256(
                json.dumps(
                    common + [str(account["provider_uuid"]), identity, tail],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            before, after = (old, new) if upgrade else (new, old)
            if table == "bridge_zulip_v5_tasks":
                session.execute(
                    "UPDATE bridge_zulip_v5_tasks SET content=jsonb_set(content,'{account_revision}',to_jsonb(%s::text)) WHERE external_account_uuid=%s AND content->>'account_revision'=%s",
                    (after, account["uuid"], before),
                )
            else:
                session.execute(
                    "UPDATE bridge_zulip_v5_subscriptions SET account_revision=%s WHERE external_account_uuid=%s AND account_revision=%s",
                    (after, account["uuid"], before),
                )
                # Pin the fingerprint/content versions used by migration 0262.
                before_history = hashlib.sha256(
                    json.dumps(
                        [before, 2, 1],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                after_history = hashlib.sha256(
                    json.dumps(
                        [after, 2, 1],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                session.execute(
                    "UPDATE bridge_zulip_v5_history_batches SET source_revision=%s WHERE external_account_uuid=%s AND source_revision=%s",
                    (after_history, account["uuid"], before_history),
                )
    if upgrade:
        session.execute(
            "UPDATE bridge_zulip_v5_tasks SET content=jsonb_set(content,'{external_account_id}',to_jsonb(regexp_replace(content->>'external_account_id','^.*:',''))) WHERE content ? 'external_account_id'"
        )
    else:
        session.execute(
            "UPDATE bridge_zulip_v5_tasks SET content=jsonb_set(content,'{external_account_id}',to_jsonb(content->>'hostname'||':'||(content->>'external_account_id'))) WHERE content ? 'external_account_id' AND content ? 'hostname'"
        )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0262-Reconvert-stored-Zulip-silent-user-mentions-80e205.py"]

    @property
    def migration_id(self):
        return "b52ac422-7a37-433c-9e87-1536f902208d"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        views = _views(session)
        functions = session.execute("""SELECT p.oid::regprocedure::text AS signature, pg_get_functiondef(p.oid) AS definition
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' AND p.prokind='f'""").fetchall()
        for view in reversed(views):
            if view["drop"]:
                session.execute(f"DROP VIEW IF EXISTS {view['name']} CASCADE")
        # Preserve enabled trigger state while backfilling provenance without creating public events.
        triggers = _disable_triggers(session)
        session.execute("""
            CREATE TABLE messenger_providers (uuid uuid PRIMARY KEY,hostname varchar(255) NOT NULL UNIQUE);
            ALTER TABLE m_external_accounts_v2 ADD COLUMN provider_uuid uuid REFERENCES messenger_providers(uuid) ON DELETE CASCADE;

            ALTER TABLE m_external_accounts_v2 DROP CONSTRAINT m_external_accounts_v2_project_owner_provider_key,
                DROP CONSTRAINT m_external_accounts_v2_project_provider_external_id_key,
                ADD CONSTRAINT external_accounts_owner_provider_key UNIQUE(project_id,owner_user_uuid,provider,provider_uuid),
                ADD CONSTRAINT external_accounts_provider_identity_key UNIQUE(project_id,provider,provider_uuid,external_account_id);
            UPDATE m_external_accounts_v2 SET external_account_id=regexp_replace(external_account_id,'^.*:','') WHERE provider='zulip';
            UPDATE bridge_zulip_v5_deliveries SET external_account_id=regexp_replace(external_account_id,'^.*:','');
        """)
        urls = session.execute(
            "SELECT DISTINCT settings->>'server_url' AS server_url FROM m_external_accounts_v2 WHERE settings ? 'server_url'"
        ).fetchall()
        for row in urls:
            hostname = urllib.parse.urlsplit(row["server_url"]).hostname
            if hostname is None:
                continue
            provider_uuid = sys_uuid.uuid5(sys_uuid.NAMESPACE_DNS, hostname)
            session.execute(
                "INSERT INTO messenger_providers(uuid,hostname) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (provider_uuid, hostname),
            )
            session.execute(
                "UPDATE m_external_accounts_v2 SET provider_uuid=%s WHERE settings->>'server_url'=%s",
                (provider_uuid, row["server_url"]),
            )
        for table in TABLES:
            if table not in ROOTS:
                session.execute(
                    f"ALTER TABLE {table} ADD COLUMN source_name varchar(64) NOT NULL DEFAULT 'native'"
                )
            session.execute(
                f"ALTER TABLE {table} ADD COLUMN provider_uuid uuid REFERENCES messenger_providers(uuid) ON DELETE CASCADE, ADD COLUMN provider_identity varchar(255)"
            )
            session.execute(
                f"CREATE INDEX {table}_provider_idx ON {table}(provider_uuid,provider_identity) WHERE provider_uuid IS NOT NULL"
            )
        for table in TABLES:
            session.execute(
                f"ALTER TABLE {table} ADD CONSTRAINT {table}_provider_pair_check CHECK ((provider_uuid IS NULL) = (provider_identity IS NULL) AND (provider_uuid IS NULL OR source_name<>'native'))"
            )
            if table not in ROOTS:
                session.execute(
                    f"ALTER TABLE {table} ADD CONSTRAINT {table}_source_name_check CHECK (source_name IN ('native','zulip'))"
                )
        _origins(session)
        _account_revisions(session, True)
        _snapshots(session, True)
        for table in ROOTS:
            session.execute(f"ALTER TABLE {table} DROP COLUMN source")
        for view in views:
            definition = _definition(view["definition"], True)
            additions = {
                "messenger_api_stream_bindings_v1": "target",
                "messenger_api_message_reactions_v1": "fact",
                "m_workspace_visible_files_v1": "files",
            }
            alias = additions.get(view["name"])
            if alias:
                definition = definition.replace(
                    "   FROM ",
                    f",\n    {alias}.source_name, {alias}.provider_uuid, {alias}.provider_identity\n   FROM ",
                )
            if view["drop"] or definition != view["definition"]:
                session.execute(
                    f"CREATE OR REPLACE VIEW {view['name']} AS {definition}"
                )
        _restore_functions(session, functions, True)
        session.execute(ORIGIN_TRIGGER)
        for table in (
            "messenger_streams",
            "messenger_topics",
            "messenger_stream_bindings",
            "messenger_user_topic_bindings",
            "messenger_user_message_states",
        ):
            session.execute(
                f"CREATE TRIGGER messenger_assign_provider_origin BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION messenger_assign_provider_origin()"
            )
        _restore_triggers(session, triggers)

    def downgrade(self, session):
        # Recreate the previous payload representation before removing provider columns.
        views = _views(session, False)
        functions = session.execute("""SELECT p.oid::regprocedure::text AS signature, pg_get_functiondef(p.oid) AS definition FROM pg_proc p
            JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind='f'
            AND p.proname<>'messenger_assign_provider_origin'""").fetchall()
        triggers = _disable_triggers(session)
        for view in reversed(views):
            if view["drop"]:
                session.execute(f"DROP VIEW IF EXISTS {view['name']} CASCADE")
        for table in (
            "messenger_streams",
            "messenger_topics",
            "messenger_stream_bindings",
            "messenger_user_topic_bindings",
            "messenger_user_message_states",
        ):
            session.execute(f"DROP TRIGGER messenger_assign_provider_origin ON {table}")
        session.execute("DROP FUNCTION messenger_assign_provider_origin()")
        _snapshots(session, False)
        _account_revisions(session, False)
        for table in ROOTS:
            session.execute(
                f'ALTER TABLE {table} ADD COLUMN source jsonb NOT NULL DEFAULT \'{{"kind":"native"}}\'::jsonb'
            )
            session.execute(
                f"UPDATE {table} SET source=jsonb_build_object('kind',source_name)"
            )
        session.execute("""
            UPDATE m_external_accounts_v2 account SET external_account_id=provider.hostname||':'||account.external_account_id
            FROM messenger_providers provider WHERE provider.uuid=account.provider_uuid AND account.provider='zulip' AND account.external_account_id IS NOT NULL;
            UPDATE bridge_zulip_v5_deliveries SET external_account_id=hostname||':'||external_account_id;
            ALTER TABLE m_external_accounts_v2 DROP CONSTRAINT external_accounts_owner_provider_key,
                DROP CONSTRAINT external_accounts_provider_identity_key,
                ADD CONSTRAINT m_external_accounts_v2_project_owner_provider_key UNIQUE(project_id,owner_user_uuid,provider),
                ADD CONSTRAINT m_external_accounts_v2_project_provider_external_id_key UNIQUE(project_id,provider,external_account_id);
        """)
        for table in TABLES:
            session.execute(
                f"ALTER TABLE {table} DROP COLUMN provider_identity,DROP COLUMN provider_uuid"
            )
            if table not in ROOTS:
                session.execute(f"ALTER TABLE {table} DROP COLUMN source_name")
        session.execute(
            "ALTER TABLE m_external_accounts_v2 DROP COLUMN provider_uuid; DROP TABLE messenger_providers"
        )
        for view in views:
            definition = view["definition"]
            if view["name"] in (
                "messenger_api_stream_bindings_v1",
                "messenger_api_message_reactions_v1",
                "m_workspace_visible_files_v1",
            ):
                definition = re.sub(
                    r",\s*\w+\.source_name,\s*\w+\.provider_uuid,\s*\w+\.provider_identity\s*\n\s*FROM",
                    "\n FROM",
                    definition,
                )
            definition = _definition(definition, False)
            if view["drop"] or definition != view["definition"]:
                session.execute(
                    f"CREATE OR REPLACE VIEW {view['name']} AS {definition}"
                )
        _restore_functions(session, functions, False)
        _restore_triggers(session, triggers)


migration_step = MigrationStep()
