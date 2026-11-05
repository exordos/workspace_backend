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


# Working placements have composite foreign keys to live stream, topic and
# message roots after physical deletion in migration 0294. Resolve the message
# by key so stale cardinality estimates cannot reorder this lookup into scans.
REPAIR_SQL = """
CREATE OR REPLACE FUNCTION public.messenger_repair_last_message(p_project uuid, p_stream uuid, p_topic uuid, p_placement uuid)
 RETURNS void
 LANGUAGE plpgsql
AS $function$
DECLARE previous record;
BEGIN
    -- Serialize the two shared pointers in the same order as UPSERT. Lock
    -- even for a non-last deletion: it may become last during this wait.
    PERFORM 1 FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream FOR UPDATE;
    PERFORM 1 FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic FOR UPDATE;
    IF EXISTS(SELECT 1 FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement) THEN
        SELECT placement.uuid,message.created_at INTO previous FROM messenger_message_placements placement
        CROSS JOIN LATERAL (
            SELECT created_at FROM messenger_messages
            WHERE project_id=placement.project_id AND uuid=placement.message_uuid LIMIT 1
        ) message
        WHERE placement.project_id=p_project AND placement.stream_uuid=p_stream AND placement.uuid<>p_placement
        ORDER BY message.created_at DESC,placement.uuid DESC LIMIT 1;
        IF FOUND THEN
            UPDATE messenger_stream_last_messages SET placement_uuid=previous.uuid,created_at=previous.created_at
            WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement;
        ELSE DELETE FROM messenger_stream_last_messages WHERE project_id=p_project AND stream_uuid=p_stream AND placement_uuid=p_placement;
        END IF;
    END IF;    IF EXISTS(SELECT 1 FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement) THEN
        SELECT placement.uuid,message.created_at INTO previous FROM messenger_message_placements placement
        CROSS JOIN LATERAL (
            SELECT created_at FROM messenger_messages
            WHERE project_id=placement.project_id AND uuid=placement.message_uuid LIMIT 1
        ) message
        WHERE placement.project_id=p_project AND placement.topic_uuid=p_topic AND placement.uuid<>p_placement
        ORDER BY message.created_at DESC,placement.uuid DESC LIMIT 1;
        IF FOUND THEN
            UPDATE messenger_topic_last_messages SET placement_uuid=previous.uuid,created_at=previous.created_at
            WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement;
        ELSE DELETE FROM messenger_topic_last_messages WHERE project_id=p_project AND topic_uuid=p_topic AND placement_uuid=p_placement;
        END IF;
    END IF;

END $function$
"""
PREVIOUS_REPAIR_SQL = REPAIR_SQL.replace(
    """CROSS JOIN LATERAL (
            SELECT created_at FROM messenger_messages
            WHERE project_id=placement.project_id AND uuid=placement.message_uuid LIMIT 1
        ) message""",
    """JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
        JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
        JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid""",
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0296-Name-unnamed-Zulip-topics-after-their-provider-a228fe.py"
        ]

    @property
    def migration_id(self):
        return "5fe49e51-0960-48db-8f2e-9d77fa3f7a9e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(REPAIR_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_REPAIR_SQL)


migration_step = MigrationStep()
