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
import uuid

from restalchemy.storage.sql import migrations


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0268-Keep-Zulip-delivery-ordered-per-entity-during-read-backlog-4ce31b.py"
        ]

    @property
    def migration_id(self):
        return "a2ab0940-613c-4f4c-b722-7604e0736bb2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE TEMP TABLE invalid_zulip_flag_snapshots ON COMMIT DROP AS
            SELECT event.* FROM messenger_domain_outbox_events event
            JOIN zulip_incoming_events incoming
              ON incoming.project_id=event.project_id
             AND incoming.processed_at=event.created_at
            WHERE incoming.payload->>'type'='update_message_flags'
              AND incoming.payload->'messages'='[]'::jsonb
              AND NOT COALESCE((incoming.payload->>'all')::boolean,false)
              AND event.payload->>'source_kind'='zulip.incoming'
              AND (event.payload->>'user_uuid'=incoming.owner_user_uuid::text
                   OR event.payload->'recipients' ? incoming.owner_user_uuid::text);
            DELETE FROM messenger_publication_outbox publication
            USING invalid_zulip_flag_snapshots invalid
            WHERE publication.project_id=invalid.project_id
              AND publication.entity_uuid=(invalid.payload->>'entity_uuid')::uuid
              AND publication.kind=invalid.payload->>'kind'
              AND publication.payload=invalid.payload->'snapshot'
              AND publication.recipient_payloads=invalid.payload->'recipient_payloads';
            DELETE FROM messenger_domain_outbox_events event
            USING invalid_zulip_flag_snapshots invalid
            WHERE event.project_id=invalid.project_id AND event.uuid=invalid.uuid;
            DROP TABLE invalid_zulip_flag_snapshots;
        """)
        events = session.execute("""
            SELECT event.* FROM messenger_domain_outbox_events event
            LEFT JOIN messenger_projection_tasks task
              ON task.project_id=event.project_id AND task.outbox_event_uuid=event.uuid
            WHERE event.event_kind='delivery_snapshot_event'
              AND event.payload->>'source_kind'='zulip.incoming'
              AND (task.uuid IS NULL OR task.status NOT IN ('completed','dead_letter'))
            ORDER BY event.created_at,event.uuid
        """).fetchall()
        for event in events:
            data = event["payload"]
            project = event["project_id"]
            recipients = sorted(set(data["recipients"]))
            digest = hashlib.sha256("\n".join(recipients).encode("ascii")).hexdigest()
            audience = uuid.uuid5(project, digest)
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text,0))", (project,)
            )
            session.execute(
                """INSERT INTO m_workspace_event_audience_snapshots_v1
                (uuid,project_id,membership_digest) VALUES (%s,%s,%s)
                ON CONFLICT (project_id,membership_digest) DO NOTHING""",
                (audience, project, digest),
            )
            session.execute(
                """INSERT INTO m_workspace_event_audience_members_v1
                (audience_snapshot_uuid,user_uuid) SELECT %s,unnest(%s::uuid[])
                ON CONFLICT (audience_snapshot_uuid,user_uuid) DO NOTHING""",
                (audience, recipients),
            )
            object_type, action = data["kind"].rsplit(".", 1)
            object_type = {
                "stream_bindings": "stream_binding",
                "messages": "message",
            }.get(object_type, object_type)
            epoch = session.execute(
                """INSERT INTO m_workspace_broadcast_message_events_v1
                (uuid,project_id,entity_uuid,audience_snapshot_uuid,schema_version,
                 object_type,action,payload,created_at,updated_at)
                VALUES (%s,%s,%s,%s,1,%s,%s,%s::jsonb,%s,%s) RETURNING epoch_version""",
                (
                    event["uuid"],
                    project,
                    data["entity_uuid"],
                    audience,
                    object_type,
                    action,
                    json.dumps({"kind": data["kind"], **data["snapshot"]}),
                    event["created_at"],
                    event["created_at"],
                ),
            ).fetchone()["epoch_version"]
            session.execute(
                """INSERT INTO m_workspace_event_recipient_payloads_v1
                (event_uuid,user_uuid,payload) SELECT %s,item.key::uuid,item.value
                FROM jsonb_each(%s::jsonb) AS item""",
                (event["uuid"], json.dumps(data["recipient_payloads"])),
            )
            session.execute(
                """UPDATE m_workspace_event_audience_snapshots_v1
                SET current_epoch_version=GREATEST(current_epoch_version,%s) WHERE uuid=%s""",
                (epoch, audience),
            )
            stream = data["stream_uuid"]
            if stream is not None:
                generations = data["membership_generations"]
                if (
                    data["control_effect"]
                    and not session.execute(
                        "SELECT 1 FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                        (project, stream),
                    ).fetchone()
                ):
                    stream = None
                for user in recipients:
                    generation = (
                        generations.get(user, 1)
                        if data["control_effect"]
                        else generations[user]
                    )
                    session.execute(
                        """INSERT INTO messenger_event_membership_guards
                        (event_uuid,project_id,user_uuid,stream_uuid,membership_generation,control_effect)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_uuid,user_uuid) DO NOTHING""",
                        (
                            event["uuid"],
                            project,
                            user,
                            stream,
                            generation,
                            data["control_effect"],
                        ),
                    )
            session.execute(
                "DELETE FROM messenger_domain_outbox_events WHERE project_id=%s AND uuid=%s",
                (project, event["uuid"]),
            )

        session.execute("""DELETE FROM messenger_domain_outbox_events
            WHERE event_kind='delivery_snapshot_event'
              AND payload->>'source_kind'='zulip.incoming'""")

    def downgrade(self, session):
        raise RuntimeError("Direct Zulip client publication cutover is irreversible")


migration_step = MigrationStep()
