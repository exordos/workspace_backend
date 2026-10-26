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

import json
import re
import uuid

from restalchemy.storage.sql import migrations


FILE_REFERENCE = re.compile(
    r"(?P<prefix>\]\(<?)urn:file:"
    r"(?P<uuid>[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12})"
    r"(?=[?>)\s])",
    re.IGNORECASE,
)


def repair_pages(session, pages):
    file_ids = {
        uuid.UUID(match["uuid"])
        for page in pages
        for match in FILE_REFERENCE.finditer(page["workspace_content"]["content"])
    }
    if not file_ids:
        return
    media = {
        str(row["uuid"]): row["kind"]
        for row in session.execute(
            """
            SELECT uuid,split_part(lower(content_type),'/',1) AS kind
            FROM m_workspace_files
            WHERE uuid=ANY(%s::uuid[]) AND source_name='zulip'
              AND split_part(lower(content_type),'/',1) IN ('image','video')
        """,
            (list(file_ids),),
        ).fetchall()
    }
    session.execute("SELECT set_config('workspace.zulip_incoming','on',true)")
    for page in pages:
        original = page["workspace_content"]["content"]
        text = FILE_REFERENCE.sub(
            lambda match: (
                f"{match['prefix']}urn:{media[match['uuid'].lower()]}:{match['uuid']}"
                if match["uuid"].lower() in media
                else match[0]
            ),
            original,
        )
        if text == original:
            continue
        # Exact outgoing snapshots preserve the author's own formatting.
        messages = session.execute(
            """
            SELECT message.project_id,message.uuid
            FROM messenger_messages AS message
            WHERE message.content_uuid=%s AND message.source_name='zulip'
              AND NOT EXISTS (
                SELECT 1 FROM bridge_zulip_v5_deliveries AS delivery
                JOIN bridge_zulip_v5_content_snapshots AS snapshot
                  ON snapshot.delivery_uuid=delivery.uuid
                WHERE delivery.project_id=message.project_id
                  AND delivery.message_uuid=message.uuid
                  AND snapshot.content_uuid=message.content_uuid
              )
            ORDER BY message.project_id,message.uuid
        """,
            (page["uuid"],),
        ).fetchall()
        if not messages:
            continue
        resolved = session.execute(
            "SELECT messenger_resolve_message_content(%s::jsonb,%s) AS uuid",
            (
                json.dumps({**page["workspace_content"], "content": text}),
                page["provider_content"],
            ),
        ).fetchone()["uuid"]
        for message in messages:
            session.execute(
                "SELECT set_config('workspace.projected_message_content',%s,true)",
                (f"{message['project_id']}:{message['uuid']}",),
            )
            session.execute(
                """
                UPDATE messenger_messages SET content_uuid=%s
                WHERE project_id=%s AND uuid=%s AND content_uuid=%s
            """,
                (resolved, message["project_id"], message["uuid"], page["uuid"]),
            )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0276-Bound-message-counter-contribution-lookup-to-changed-placements-f16e00.py"
        ]

    @property
    def migration_id(self):
        return "45f9cace-c504-4ae8-b119-c0c59470ec0f"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        position = uuid.UUID(int=0)
        while True:
            pages = session.execute(
                """
                SELECT uuid,workspace_content,provider_content
                FROM messenger_message_contents
                WHERE uuid>%s AND provider_content IS NOT NULL
                  AND workspace_content->>'kind'='markdown'
                  AND strpos(workspace_content->>'content','urn:file:')>0
                ORDER BY uuid LIMIT 100
            """,
                (position,),
            ).fetchall()
            if not pages:
                return
            repair_pages(session, pages)
            position = pages[-1]["uuid"]

    def downgrade(self, session):
        raise RuntimeError("Media reference repair preserves immutable content history")


migration_step = MigrationStep()
