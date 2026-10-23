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

import re

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
VIEWS = {
    "messenger_api_user_streams_v1": "stream",
    "messenger_api_stream_bindings_v1": "target",
    "messenger_api_user_topics_v1": "topic",
    "messenger_api_user_messages_v1": "message",
    "messenger_api_message_reactions_v1": "fact",
    "m_workspace_visible_files_v1": "files",
}
STATUS_GUARD = """
    -- Provider synchronization status is operational metadata.
    IF TG_OP='UPDATE' AND to_jsonb(NEW)->'status' IS DISTINCT FROM to_jsonb(OLD)->'status'
       AND (to_jsonb(NEW)-'status') IS NOT DISTINCT FROM (to_jsonb(OLD)-'status') THEN
        RETURN NULL;
    END IF;
"""


def _views(session):
    return session.execute("""
        WITH RECURSIVE dependencies AS (
            SELECT c.oid, 0 AS depth FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='v'
            UNION ALL
            SELECT rewrite.ev_class,parent.depth+1 FROM dependencies parent
            JOIN pg_depend dependency ON dependency.refobjid=parent.oid
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            WHERE rewrite.ev_class<>parent.oid
        ) SELECT oid::regclass::text AS name, pg_get_viewdef(oid) AS definition
          FROM dependencies GROUP BY oid ORDER BY max(depth),name
    """).fetchall()


def _functions(session):
    return session.execute("""SELECT p.oid::regprocedure::text AS signature,
        pg_get_functiondef(p.oid) AS definition FROM pg_proc p
        JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.prokind='f'""").fetchall()


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0263-Normalize-provider-identities-and-remove-source-payloads-b52ac4.py"
        ]

    @property
    def migration_id(self):
        return "ca9efdab-4c29-4a43-8945-cb685a33fcdd"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        for table in TABLES:
            session.execute(
                f"ALTER TABLE {table} ADD COLUMN status varchar(16) NOT NULL DEFAULT 'active', "
                f"ADD CONSTRAINT {table}_sync_status_check CHECK (status IN ('new','syncing','active','error') AND (source_name<>'native' OR status='active'))"
            )
        for view in _views(session):
            if view["name"] in VIEWS:
                alias = VIEWS[view["name"]]
                definition = view["definition"].replace(
                    "   FROM ", f",\n    {alias}.status\n   FROM "
                )
                session.execute(
                    f"CREATE OR REPLACE VIEW {view['name']} AS {definition}"
                )
        # Operational transitions must not enqueue new domain work or change counters.
        functions = session.execute(
            """SELECT DISTINCT pg_get_functiondef(trigger.tgfoid) AS definition
            FROM pg_trigger trigger WHERE trigger.tgrelid=ANY(%s::regclass[])
              AND NOT trigger.tgisinternal AND (trigger.tgtype & 1)=1 AND (trigger.tgtype & 2)=0""",
            (list(TABLES),),
        ).fetchall()
        for function in functions:
            definition = re.sub(
                r"\bBEGIN\b",
                lambda match: match[0] + STATUS_GUARD,
                function["definition"],
                count=1,
                flags=re.IGNORECASE,
            )
            session.execute(definition)

    def downgrade(self, session):
        views = _views(session)
        functions = _functions(session)
        affected = session.execute(
            """WITH RECURSIVE affected AS (
            SELECT rewrite.ev_class AS oid FROM pg_depend dependency
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            JOIN pg_attribute column_ ON column_.attrelid=dependency.refobjid AND column_.attnum=dependency.refobjsubid
            WHERE dependency.refobjid=ANY(%s::regclass[]) AND column_.attname='status'
            UNION SELECT rewrite.ev_class FROM affected parent
            JOIN pg_depend dependency ON dependency.refobjid=parent.oid
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid WHERE rewrite.ev_class<>parent.oid
        ) SELECT oid::regclass::text AS name FROM affected""",
            (list(TABLES),),
        ).fetchall()
        affected = {row["name"] for row in affected}
        for view in reversed(views):
            if view["name"] in affected:
                session.execute(f"DROP VIEW IF EXISTS {view['name']} CASCADE")
        for table in TABLES:
            session.execute(f"ALTER TABLE {table} DROP COLUMN status")
        for view in views:
            if view["name"] in affected:
                definition = re.sub(
                    r",\s*\w+\.status\s*\n\s*FROM", "\n FROM", view["definition"]
                )
                session.execute(f"CREATE VIEW {view['name']} AS {definition}")
        existing = {row["signature"] for row in _functions(session)}
        for function in functions:
            definition = function["definition"].replace(STATUS_GUARD, "")
            if (
                definition != function["definition"]
                or function["signature"] not in existing
            ):
                session.execute(definition)


migration_step = MigrationStep()
