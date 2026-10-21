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


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0215-Name-empty-Zulip-topics-General-Topic-146c8a.py"]

    @property
    def migration_id(self):
        return "1407303c-047c-4008-99e6-550e6e3cd7e2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            LOCK TABLE bridge_zulip_v5_topics, bridge_zulip_v5_messages
                IN SHARE ROW EXCLUSIVE MODE;
            CREATE TEMP TABLE zulip_topic_identity_map ON COMMIT DROP AS
            SELECT topic.uuid AS old_uuid,
                   messenger_uuid_v5(
                       channel.stream_uuid,
                       CASE
                           WHEN topic.uuid IN (
                               messenger_uuid_v5(channel.uuid, ''),
                               messenger_uuid_v5(channel.stream_uuid, '')
                           ) THEN ''
                           ELSE topic.name
                       END
                   ) AS topic_uuid
            FROM bridge_zulip_v5_topics AS topic
            JOIN LATERAL (
                SELECT channel.uuid, channel.stream_uuid
                FROM bridge_zulip_v5_messages AS message
                JOIN bridge_zulip_v5_channels AS channel
                  ON channel.uuid = message.channel
                 AND channel.external_account_uuid = message.external_account_uuid
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = channel.external_account_uuid
                 AND account.project_id = message.project_id
                WHERE message.topic = topic.uuid AND account.provider = 'zulip'
                LIMIT 1
            ) AS channel ON true;

            INSERT INTO bridge_zulip_v5_topics
                (uuid, stream_id, name, max_id, created_at, updated_at)
            SELECT identity.topic_uuid, MAX(topic.stream_id),
                   COALESCE(NULLIF(MAX(topic.name), ''), 'General Topic'),
                   MAX(topic.max_id), MIN(topic.created_at), MAX(topic.updated_at)
            FROM zulip_topic_identity_map AS identity
            JOIN bridge_zulip_v5_topics AS topic ON topic.uuid = identity.old_uuid
            GROUP BY identity.topic_uuid
            ON CONFLICT (uuid) DO UPDATE SET
                max_id = GREATEST(bridge_zulip_v5_topics.max_id, EXCLUDED.max_id),
                created_at = LEAST(bridge_zulip_v5_topics.created_at, EXCLUDED.created_at),
                updated_at = GREATEST(bridge_zulip_v5_topics.updated_at, EXCLUDED.updated_at);
            UPDATE bridge_zulip_v5_messages AS message
            SET topic = identity.topic_uuid
            FROM zulip_topic_identity_map AS identity
            WHERE message.topic = identity.old_uuid
              AND identity.old_uuid <> identity.topic_uuid;
            DELETE FROM bridge_zulip_v5_topics AS topic
            USING zulip_topic_identity_map AS identity
            WHERE topic.uuid = identity.old_uuid
              AND identity.old_uuid <> identity.topic_uuid;

            ALTER TABLE messenger_topics
                DROP CONSTRAINT messenger_topics_source_name_check;
            ALTER TABLE messenger_topics
                ADD CONSTRAINT messenger_topics_source_name_check
                CHECK (source_name IN ('native', 'zulip'));

            CREATE VIEW bridge_zulip_v5_topic_states AS
            WITH zulip_topics AS (
                SELECT DISTINCT account.project_id, channel.stream_uuid,
                       message.topic AS topic_uuid
                FROM bridge_zulip_v5_messages AS message
                JOIN bridge_zulip_v5_channels AS channel
                  ON channel.uuid = message.channel
                 AND channel.external_account_uuid = message.external_account_uuid
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid = channel.external_account_uuid
                 AND account.project_id = message.project_id
                JOIN messenger_streams AS stream
                  ON stream.project_id = account.project_id
                 AND stream.uuid = channel.stream_uuid
                 AND stream.source_name = 'zulip'
                 AND stream.deleted_at IS NULL
                WHERE account.provider = 'zulip'
            ), workspace_topics AS (
                SELECT project_id, stream_uuid, uuid AS topic_uuid, source_name
                FROM messenger_topics
                WHERE deleted_at IS NULL
            )
            SELECT
                COALESCE(zulip.project_id, workspace.project_id) AS project_id,
                COALESCE(zulip.stream_uuid, workspace.stream_uuid) AS stream_uuid,
                COALESCE(zulip.topic_uuid, workspace.topic_uuid) AS topic_uuid,
                CASE
                    WHEN workspace.topic_uuid IS NULL THEN 'create'
                    ELSE 'delete'
                END AS action
            FROM zulip_topics AS zulip
            FULL JOIN workspace_topics AS workspace
              ON workspace.project_id = zulip.project_id
             AND workspace.topic_uuid = zulip.topic_uuid
            WHERE workspace.topic_uuid IS NULL
               OR (
                   zulip.topic_uuid IS NULL
                   AND workspace.source_name = 'zulip'
               );
            """
        )

    def downgrade(self, session):
        # Canonical topic UUIDs are retained when rolling back the view.
        session.execute(
            """
            DROP VIEW bridge_zulip_v5_topic_states;
            UPDATE messenger_topics
                SET source_name = 'native', source = '{"kind":"native"}'::jsonb
                WHERE source_name = 'zulip';
            ALTER TABLE messenger_topics
                DROP CONSTRAINT messenger_topics_source_name_check;
            ALTER TABLE messenger_topics
                ADD CONSTRAINT messenger_topics_source_name_check
                CHECK (source_name = 'native');
            """
        )


migration_step = MigrationStep()
