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


MEMBER_RECIPIENTS = """(SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                FROM messenger_stream_bindings member JOIN m_external_accounts_v2 peer
                  ON peer.project_id=member.project_id AND peer.owner_user_uuid=member.user_uuid
                WHERE member.project_id=(row_->>'project_id')::uuid AND member.stream_uuid=stream_
                  AND member.active AND peer.provider_uuid=target_.provider_uuid
                  AND peer.external_account_id IS NOT NULL)"""

GROUP_RECIPIENTS = """(SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                FROM messenger_stream_bindings member LEFT JOIN m_external_accounts_v2 peer
                  ON peer.project_id=member.project_id AND peer.owner_user_uuid=member.user_uuid
                 AND peer.provider='zulip' AND peer.provider_uuid=target_.provider_uuid
                WHERE member.project_id=(row_->>'project_id')::uuid AND member.stream_uuid=stream_
                  AND member.active)"""

DIRECT_RECIPIENTS = (
    """CASE WHEN target_.channel_type='private' THEN
                (SELECT jsonb_agg(DISTINCT peer.external_account_id::bigint)
                 FROM m_external_accounts_v2 peer
                 WHERE peer.project_id=(row_->>'project_id')::uuid
                   AND peer.owner_user_uuid=target_.direct_user_uuid
                   AND peer.provider='zulip'
                   AND peer.provider_uuid=target_.provider_uuid
                   AND peer.external_account_id IS NOT NULL)
                ELSE """
    + GROUP_RECIPIENTS
    + " END"
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0279-Restore-migrated-Zulip-inbox-message-ordering-keys-7f3b75.py"
        ]

    @property
    def migration_id(self):
        return "d1cf4e0c-8831-4730-8d4f-1faa69bc1925"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        capture = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        session.execute(
            capture.replace(
                "'recipient_ids'," + MEMBER_RECIPIENTS,
                "'recipient_ids'," + DIRECT_RECIPIENTS,
            )
        )

    def downgrade(self, session):
        capture = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        session.execute(
            capture.replace(
                "'recipient_ids'," + DIRECT_RECIPIENTS,
                "'recipient_ids'," + MEMBER_RECIPIENTS,
            )
        )


migration_step = MigrationStep()
