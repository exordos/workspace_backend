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


CONTENT_FUNCTION = "workspace_message_markdown_content_v1"
MENTION_VIEWS = (
    "m_workspace_user_messages_view",
    "m_workspace_user_unread_messages_base_v1",
    "m_workspace_user_topic_unread_counts_v1",
)
MENTION_FUNCTIONS = (
    "messenger_v2_import_legacy_message",
    "messenger_v2_import_legacy_message_inserts",
)


def _update_mentions(session, *, upgrade):
    for name in MENTION_VIEWS:
        definition = session.execute(
            "SELECT pg_get_viewdef(%s::regclass) AS definition", (name,)
        ).fetchone()["definition"]
        original = "(message.payload ->> 'content'::text)"
        guarded = f"{CONTENT_FUNCTION}(message.payload)"
        source, target = (original, guarded) if upgrade else (guarded, original)
        session.execute(
            f"CREATE OR REPLACE VIEW {name} AS " + definition.replace(source, target),
            None,
        )
    for name in MENTION_FUNCTIONS:
        definition = session.execute(
            "SELECT pg_get_functiondef(%s::regprocedure) AS definition", (f"{name}()",)
        ).fetchone()["definition"]
        for alias in ("NEW", "message"):
            original = f"{alias}.payload->>'content'"
            guarded = f"{CONTENT_FUNCTION}({alias}.payload)"
            source, target = (original, guarded) if upgrade else (guarded, original)
            definition = definition.replace(source, target)
        session.execute(definition, None)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0186-Add-stream-encryption-metadata-17aa3c.py"]

    @property
    def migration_id(self):
        return "90458eff-2c38-4928-bc52-db892e266636"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("workspace-read-state-schema-v1",),
        )
        session.execute(
            f"""
            CREATE FUNCTION {CONTENT_FUNCTION}(payload jsonb)
            RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
                SELECT CASE WHEN payload->>'kind' = 'markdown'
                    THEN payload->>'content' ELSE '' END
            $$
            """,
            (),
        )
        _update_mentions(session, upgrade=True)
        session.execute(
            """
            DELETE FROM m_workspace_message_mentions_v1 AS mention
            USING m_workspace_messages AS message
            WHERE message.uuid = mention.message_uuid
              AND message.payload->>'kind' <> 'markdown'
            """,
            (),
        )
        session.execute(
            """
            INSERT INTO messenger_domain_outbox_events (
                uuid, project_id, event_kind, scope_kind, scope_key, payload,
                created_at, updated_at
            )
            SELECT messenger_uuid_v5(placement.uuid, 'markdown-only-mentions-v1'),
                   placement.project_id, 'content_mentions', 'topic',
                   placement.project_id::text || ':' || placement.topic_uuid::text,
                   jsonb_build_object(
                       'source_kind', 'message.updated',
                       'placement_uuid', placement.uuid,
                       'emit_message_updated', false
                   ), NOW(), NOW()
            FROM messenger_message_placements AS placement
            JOIN messenger_messages AS message
              ON message.project_id = placement.project_id
             AND message.uuid = placement.message_uuid
            WHERE message.payload->>'kind' <> 'markdown'
              AND EXISTS (
                  SELECT 1 FROM messenger_user_message_states AS state
                  WHERE state.project_id = placement.project_id
                    AND state.placement_uuid = placement.uuid AND state.mentioned
              )
            ON CONFLICT (project_id, uuid) DO NOTHING
            """,
            (),
        )

        session.execute(
            """
            UPDATE messenger_user_message_states AS state
            SET mentioned = false, updated_at = NOW()
            FROM messenger_message_placements AS placement
            JOIN messenger_messages AS message
              ON message.project_id = placement.project_id
             AND message.uuid = placement.message_uuid
            WHERE state.project_id = placement.project_id
              AND state.placement_uuid = placement.uuid
              AND state.mentioned AND message.payload->>'kind' <> 'markdown'
            """,
            (),
        )

    def downgrade(self, session):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("workspace-read-state-schema-v1",),
        )
        _update_mentions(session, upgrade=False)
        session.execute(f"DROP FUNCTION {CONTENT_FUNCTION}(jsonb)", ())


migration_step = MigrationStep()
