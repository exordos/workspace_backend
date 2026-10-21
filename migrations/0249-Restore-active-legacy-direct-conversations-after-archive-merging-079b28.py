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
        self._depends = [
            "0248-Snapshot-outgoing-Zulip-deletion-tasks-before-tombstone-cleanup-e378c7.py"
        ]

    @property
    def migration_id(self):
        return "079b287d-436b-4208-81c0-ec315f5fb595"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        if (
            session.execute(
                "SELECT to_regclass('workspace_zulip_v5_legacy_archive') AS archive"
            ).fetchone()["archive"]
            is None
        ):
            return
        restored = session.execute("""
            WITH working_sources AS (
                SELECT DISTINCT mapping.data->>'project_id' AS project_id,
                       mapping.data->>'new_uuid' AS stream_uuid,
                       original.data AS snapshot
                FROM workspace_zulip_v5_legacy_archive AS original
                JOIN workspace_zulip_v5_legacy_archive AS mapping
                  ON mapping.kind='mapping' AND mapping.data->>'kind'='stream'
                 AND mapping.data->>'old_uuid'=original.data->>'uuid'
                 AND mapping.data->>'project_id'=original.data->>'project_id'
                JOIN workspace_zulip_v5_legacy_archive AS chat
                  ON chat.kind='chat'
                 AND chat.data->>'project_id'=mapping.data->>'project_id'
                 AND chat.data->>'projection_stream_uuid'=original.data->>'uuid'
                 AND (chat.data->>'selected')::boolean
                WHERE original.kind='stream'
            ), confirmed AS (
                SELECT project_id,stream_uuid,
                       min((snapshot->>'updated_at')::timestamp) AS snapshot_updated_at
                FROM working_sources
                GROUP BY project_id,stream_uuid
                HAVING count(*)=1
                   AND bool_and(snapshot->>'source_name'='native'
                       AND (snapshot->>'private')::boolean
                       AND NOT (snapshot->>'is_archived')::boolean
                       AND snapshot->>'direct_user_uuid' IS NOT NULL)
            )
            UPDATE messenger_streams AS current
            SET is_archived=false,updated_at=NOW()
            FROM confirmed
            WHERE current.project_id::text=confirmed.project_id
              AND current.uuid::text=confirmed.stream_uuid
              AND current.source_name='zulip' AND current.private
              AND current.is_archived AND current.deleted_at IS NULL
              -- A later edit may be an intentional archive. Leave it alone.
              AND current.updated_at<=confirmed.snapshot_updated_at
              AND EXISTS (
                  SELECT 1 FROM workspace_zulip_v5_legacy_archive AS mapping
                  JOIN workspace_zulip_v5_legacy_archive AS carrier
                    ON carrier.kind='stream'
                   AND carrier.data->>'uuid'=mapping.data->>'old_uuid'
                   AND carrier.data->>'project_id'=mapping.data->>'project_id'
                  WHERE mapping.kind='mapping' AND mapping.data->>'kind'='stream'
                    AND mapping.data->>'project_id'=confirmed.project_id
                    AND mapping.data->>'new_uuid'=confirmed.stream_uuid
                    AND carrier.data->>'source_name'='zulip'
                    AND (carrier.data->>'is_archived')::boolean
              )
            RETURNING current.project_id,current.uuid
        """).fetchall()
        for stream in restored:
            project, stream_uuid = stream["project_id"], stream["uuid"]
            # Use the same recipient snapshots and folder refresh as unarchive.
            session.execute(
                """
                INSERT INTO messenger_domain_outbox_events
                    (uuid,project_id,event_kind,scope_kind,scope_key,payload)
                VALUES
                    (gen_random_uuid(),%s,'delivery_snapshot_event','resource',%s,
                     jsonb_build_object('source_kind','stream.updated',
                       'resource_kind','stream','resource_uuid',%s::uuid,
                       'stream_uuid',%s::uuid)),
                    (gen_random_uuid(),%s,'folder_projection','stream-folders',%s,
                     jsonb_build_object('source_kind','stream.updated',
                       'stream_uuid',%s::uuid,'private',true))
                """,
                (
                    project,
                    f"{project}:stream:{stream_uuid}",
                    stream_uuid,
                    stream_uuid,
                    project,
                    f"{project}:{stream_uuid}",
                    stream_uuid,
                ),
            )

    def downgrade(self, session):
        # Re-archiving recovered conversations would restore the data defect.
        pass


migration_step = MigrationStep()
