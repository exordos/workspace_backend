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

from workspace.common import emoji


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0239-Normalize-Unicode-reaction-symbols-and-requeue-pending-Zulip-intents-7c33da.py"
        ]

    @property
    def migration_id(self):
        return "dffae941-a4e7-49ee-ac65-220b1a58e001"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Preserve the original intent timestamps while merging symbol/shortcode
        # duplicates. Normalizing facts must not create fresh add/remove intents.
        session.execute("LOCK TABLE bridge_zulip_v5_tasks IN ACCESS EXCLUSIVE MODE")
        session.execute(
            "LOCK TABLE messenger_message_reaction_facts, bridge_zulip_v5_reaction_intents IN SHARE ROW EXCLUSIVE MODE"
        )
        names = [
            row["emoji_name"]
            for row in session.execute("""
                SELECT emoji_name FROM messenger_message_reaction_facts
                UNION SELECT emoji_name FROM bridge_zulip_v5_reaction_intents
            """).fetchall()
        ]
        canonical = [emoji.normalize_name(name) for name in names]
        previous = session.execute(
            "SELECT current_setting('workspace.zulip_reaction_import',true) AS value"
        ).fetchone()["value"]
        session.execute(
            "SELECT set_config('workspace.zulip_reaction_import','on',true)"
        )
        session.execute(
            """
            WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical)),
            ranked AS (
                SELECT fact.project_id,fact.uuid,
                       row_number() OVER (PARTITION BY fact.project_id,fact.canonical_message_uuid,fact.user_uuid,mapping.canonical
                           ORDER BY (fact.emoji_name=mapping.canonical) DESC,fact.updated_at DESC,fact.uuid) AS position
                FROM messenger_message_reaction_facts AS fact JOIN mapping ON mapping.alias=fact.emoji_name
            ), changed AS (
                DELETE FROM messenger_message_reaction_facts AS fact USING ranked
                WHERE fact.project_id=ranked.project_id AND fact.uuid=ranked.uuid AND ranked.position>1 RETURNING fact.*
            )
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            SELECT messenger_uuid_v5(uuid,'unicode-reaction-merge-240'),project_id,'reaction_snapshot','message',
                   project_id::text || ':' || canonical_message_uuid::text,
                   jsonb_build_object('source_kind','zulip.reconciled','placement_uuid',placement_uuid,'emit_reaction_event',false)
            FROM changed
            """,
            (names, canonical),
        )
        session.execute(
            """
            WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical)),
            changed AS (
                UPDATE messenger_message_reaction_facts AS fact SET emoji_name=mapping.canonical FROM mapping
                WHERE fact.emoji_name=mapping.alias AND fact.emoji_name<>mapping.canonical RETURNING fact.*
            )
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            SELECT messenger_uuid_v5(uuid,'unicode-reaction-normalization-240'),project_id,'reaction_snapshot','message',
                   project_id::text || ':' || canonical_message_uuid::text,
                   jsonb_build_object('source_kind','zulip.reconciled','placement_uuid',placement_uuid,'emit_reaction_event',false)
            FROM changed
            """,
            (names, canonical),
        )
        session.execute(
            "SELECT set_config('workspace.zulip_reaction_import',%s,true)",
            (previous or "off",),
        )
        session.execute(
            """
            WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical)),
            ranked AS (
                SELECT intent.uuid,
                       row_number() OVER (PARTITION BY intent.project_id,intent.canonical_message_uuid,intent.user_uuid,mapping.canonical
                           ORDER BY intent.changed_at DESC,(intent.emoji_name=mapping.canonical) DESC,intent.uuid) AS position
                FROM bridge_zulip_v5_reaction_intents AS intent JOIN mapping ON mapping.alias=intent.emoji_name
            )
            DELETE FROM bridge_zulip_v5_reaction_intents AS intent USING ranked
            WHERE intent.uuid=ranked.uuid AND ranked.position>1
            """,
            (names, canonical),
        )
        session.execute(
            """
            WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical))
            UPDATE bridge_zulip_v5_reaction_intents AS intent
            SET emoji_name=mapping.canonical,error_description=NULL FROM mapping
            WHERE intent.emoji_name=mapping.alias
              AND (intent.emoji_name<>mapping.canonical
                   OR intent.error_description LIKE 'Unsupported Unicode emoji: %%'
                      AND mapping.canonical=ANY(%s::text[]))
            """,
            (names, canonical, list(emoji.CODE_BY_NAME)),
        )
        session.execute(
            """
            WITH mapping AS (SELECT * FROM unnest(%s::text[],%s::text[]) AS names(alias,canonical))
            INSERT INTO bridge_zulip_v5_reaction_changes(project_id,canonical_message_uuid)
            SELECT DISTINCT intent.project_id,intent.canonical_message_uuid
            FROM bridge_zulip_v5_reaction_intents AS intent JOIN mapping ON intent.emoji_name=mapping.canonical
            WHERE intent.emoji_name = ANY(%s::text[])
            ON CONFLICT (project_id,canonical_message_uuid) DO UPDATE
                SET revision=EXCLUDED.revision,next_retry_at=NULL
            """,
            (names, canonical, list(emoji.CODE_BY_NAME)),
        )

    def downgrade(self, session):
        # Keep stable Unicode identities and any reactions already sent.
        pass


migration_step = MigrationStep()
