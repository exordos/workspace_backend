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


def _user_streams_view_sql(*, recheck_last_message_visibility):
    last_message_source = (
        '"m_workspace_user_messages_view"'
        if recheck_last_message_visibility
        else '"m_workspace_messages"'
    )
    last_message_user_filter = (
        "\n      AND m.user_uuid = b.user_uuid"
        if recheck_last_message_visibility
        else ""
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_streams" AS
SELECT
    s.uuid,
    CASE
        WHEN s.private THEN
            COALESCE(
                NULLIF(
                    TRIM(
                        COALESCE(peer_user.first_name, '') || ' ' ||
                        COALESCE(peer_user.last_name, '')
                    ),
                    ''
                ),
                peer_user.username,
                s.name
            )
        ELSE s.name
    END AS name,
    s.description,
    s.project_id,
    s.source_name,
    s.source,
    s.user_uuid AS owner,
    b.user_uuid AS user_uuid,
    b.role AS role,
    COALESCE(un.unread_count, 0) AS unread_count,
    s.invite_only,
    s.announce,
    s.private,
    s.created_at,
    s.updated_at,
    CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        ELSE NULL
    END AS direct_user_uuid,
    s.private_index,
    s.is_archived,
    b.notification_mode,
    s.color,
    last_message.uuid AS last_message_uuid,
    s.default_topic_uuid,
    COALESCE(un.active_unread_count, 0) AS active_unread_count,
    COALESCE(un.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_streams" AS s
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = s.uuid
    AND b.project_id = s.project_id
LEFT JOIN "m_unread_user_messages" AS un
    ON un.uuid = s.uuid
    AND un.user_uuid = b.user_uuid
    AND un.project_id = s.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM {last_message_source} AS m
    WHERE m.project_id = s.project_id
      AND m.stream_uuid = s.uuid{last_message_user_filter}
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_users" AS peer_user
    ON peer_user.uuid = CASE
        WHEN s.private AND s.direct_user_uuid IS NOT NULL
             AND s.user_uuid = b.user_uuid THEN s.direct_user_uuid
        WHEN s.private AND s.direct_user_uuid IS NOT NULL THEN s.user_uuid
        WHEN s.private AND s.user_uuid <> b.user_uuid THEN s.user_uuid
        ELSE NULL
    END
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = s.project_id
    AND access.user_uuid = b.user_uuid
    AND access.stream_uuid = s.uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


def _user_topics_view_sql(*, recheck_last_message_visibility):
    last_message_source = (
        '"m_workspace_user_messages_view"'
        if recheck_last_message_visibility
        else '"m_workspace_messages"'
    )
    last_message_user_filter = (
        "\n      AND m.user_uuid = b.user_uuid"
        if recheck_last_message_visibility
        else ""
    )
    return f"""
CREATE OR REPLACE VIEW "m_workspace_user_topics_view" AS
SELECT
    t.uuid,
    t.name,
    t.stream_uuid,
    t.project_id,
    t.created_at,
    t.updated_at,
    (t.uuid = s.default_topic_uuid) AS is_default,
    b.user_uuid,
    COALESCE(uc.unread_count, 0) AS unread_count,
    COALESCE(f.is_done, FALSE) AS is_done,
    COALESCE(f.notification_mode, 'default') AS notification_mode,
    t.color,
    last_message.uuid AS last_message_uuid,
    t.source_name,
    t.source,
    t.summary,
    t.summary_last_message_uuid,
    CASE
        WHEN t.summary IS NULL THEN NULL
        ELSE t.summary_last_message_uuid IS DISTINCT FROM last_message.uuid
    END AS summary_has_new_messages,
    t.summary_system_prompt,
    t.summary_reasoning_effort,
    t.summary_enabled,
    COALESCE(uc.active_unread_count, 0) AS active_unread_count,
    COALESCE(uc.passive_unread_count, 0) AS passive_unread_count
FROM "m_workspace_stream_topics" AS t
JOIN "m_workspace_streams" AS s
    ON s.uuid = t.stream_uuid
    AND s.project_id = t.project_id
JOIN "m_workspace_stream_bindings" AS b
    ON b.stream_uuid = t.stream_uuid
    AND b.project_id = t.project_id
LEFT JOIN (
    SELECT
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id,
        COUNT(*) AS unread_count,
        COUNT(*) FILTER (WHERE unread.active)::integer
            AS active_unread_count,
        COUNT(*) FILTER (WHERE NOT unread.active)::integer
            AS passive_unread_count
    FROM "m_workspace_user_unread_messages_view" AS unread
    WHERE unread.topic_uuid IS NOT NULL
    GROUP BY
        unread.stream_uuid,
        unread.topic_uuid,
        unread.user_uuid,
        unread.project_id
) AS uc
    ON uc.topic_uuid = t.uuid
    AND uc.stream_uuid = t.stream_uuid
    AND uc.user_uuid = b.user_uuid
    AND uc.project_id = t.project_id
LEFT JOIN LATERAL (
    SELECT m.uuid
    FROM {last_message_source} AS m
    WHERE m.project_id = t.project_id
      AND m.topic_uuid = t.uuid{last_message_user_filter}
    ORDER BY m.created_at DESC, m.uuid DESC
    LIMIT 1
) AS last_message ON TRUE
LEFT JOIN "m_workspace_user_topic_flags" AS f
    ON f.uuid = t.uuid
    AND f.user_uuid = b.user_uuid
    AND f.project_id = t.project_id
LEFT JOIN "m_confirmed_external_stream_access" AS access
    ON access.project_id = t.project_id
    AND access.user_uuid = b.user_uuid
    AND access.stream_uuid = t.stream_uuid
WHERE s.source_name = 'native'
   OR access.user_uuid IS NOT NULL;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0130-split-active-and-passive-unread-counters-36e14b.py"]

    @property
    def migration_id(self):
        return "33fcdfb0-2623-44af-a2e2-06cee208f564"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # User stream/topic rows already prove canonical stream visibility.
        # Rechecking the same access view for every last-message probe
        # multiplies the external-account joins as accounts and synchronized
        # chats grow.
        session.execute(_user_streams_view_sql(recheck_last_message_visibility=False))
        session.execute(_user_topics_view_sql(recheck_last_message_visibility=False))

    def downgrade(self, session):
        session.execute(_user_streams_view_sql(recheck_last_message_visibility=True))
        session.execute(_user_topics_view_sql(recheck_last_message_visibility=True))


migration_step = MigrationStep()
