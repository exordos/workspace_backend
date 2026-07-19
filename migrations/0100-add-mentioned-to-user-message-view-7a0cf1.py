# Copyright 2026 Genesis Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

from restalchemy.storage.sql import migrations


USER_MESSAGES_VIEW_WITH_MENTIONS_SQL = """
CREATE OR REPLACE VIEW "m_workspace_user_messages_view" AS
SELECT
    m.uuid                          AS uuid,
    m.stream_uuid,
    m.user_uuid                     AS author_uuid,
    m.topic_uuid,
    m.payload,
    m.created_at,
    m.updated_at,
    b.user_uuid                     AS user_uuid,
    m.project_id,
    COALESCE(f.read,    FALSE)      AS read,
    COALESCE(f.pinned,  FALSE)      AS pinned,
    COALESCE(f.starred, FALSE)      AS starred,
    (m.user_uuid = b.user_uuid)     AS is_own,
    COALESCE(
        (
            SELECT jsonb_object_agg(
                reaction_counts.emoji_name,
                reaction_counts.reaction_count
            )
            FROM (
                SELECT
                    r.emoji_name,
                    COUNT(*) AS reaction_count
                FROM "m_workspace_message_reactions" AS r
                WHERE r.project_id = m.project_id
                    AND r.message_uuid = m.uuid
                GROUP BY r.emoji_name
            ) AS reaction_counts
        ),
        '{}'::jsonb
    )                               AS reactions,
    m.source_name,
    m.source,
    POSITION(
        '](' || 'urn:user:' || LOWER(b.user_uuid::text) || ')'
        IN LOWER(COALESCE(m.payload->>'content', ''))
    ) > 0                           AS mentioned
FROM "m_workspace_messages" AS m
JOIN "m_workspace_stream_bindings" AS b
    ON  b.stream_uuid  = m.stream_uuid
    AND b.project_id   = m.project_id
LEFT JOIN "m_workspace_user_message_flags" AS f
    ON  f.uuid       = m.uuid
    AND f.user_uuid  = b.user_uuid
    AND f.project_id = m.project_id
LEFT JOIN "m_confirmed_external_account_access" AS access
    ON  access.project_id   = m.project_id
    AND access.user_uuid    = b.user_uuid
    AND access.account_type = m.source_name
    AND access.source_scope = COALESCE(
        m.source->>'source_scope',
        m.source->>'server_url'
    )
WHERE m.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0099-add-message-pagination-keyset-index-9e1858.py"]

    @property
    def migration_id(self):
        return "7a0cf169-12fb-4c8d-b68b-2c43983a2228"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(USER_MESSAGES_VIEW_WITH_MENTIONS_SQL)

    def downgrade(self, session):
        # The column is an additive, rebuildable projection. Keeping it avoids dropping dependent
        # stream/topic views during a rollback; older models safely ignore the extra view column.
        del session


migration_step = MigrationStep()
