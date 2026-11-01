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


PREVIOUS_MESSAGE_VIEW = """
CREATE OR REPLACE VIEW messenger_api_user_messages_v1 AS
SELECT binding.uuid AS binding_uuid,placement.uuid,message.uuid AS canonical_message_uuid,
       binding.user_uuid,placement.project_id,message.created_at,message.updated_at,
       placement.stream_uuid,placement.topic_uuid,message.payload,message.author_uuid,
       unread.placement_uuid IS NULL AS read,
       pin.placement_uuid IS NOT NULL AS pinned,
       star.placement_uuid IS NOT NULL AS starred,
       message.author_uuid=binding.user_uuid AS is_own,
       mention.placement_uuid IS NOT NULL AS mentioned,
       message.reactions,message.reaction_users,message.source_name,message.provider_uuid,message.provider_identity,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) AS deleted_at,
       COALESCE(message.deleted_at,stream.deleted_at,topic.deleted_at) IS NULL AS visible,message.status,
       membership.membership_generation
FROM messenger_user_message_bindings binding
-- Keep the sparse relations in the first join group. Appending them after
-- the history joins can exceed PostgreSQL's default join_collapse_limit,
-- leaving selective mention predicates behind a project-wide history scan.
LEFT JOIN messenger_user_message_mentions mention
    ON (mention.project_id,mention.user_uuid,mention.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_unreads unread
    ON (unread.project_id,unread.user_uuid,unread.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_pins pin
    ON (pin.project_id,pin.user_uuid,pin.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
LEFT JOIN messenger_user_message_stars star
    ON (star.project_id,star.user_uuid,star.placement_uuid)=
       (binding.project_id,binding.user_uuid,binding.placement_uuid)
JOIN messenger_message_placements placement ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
JOIN messenger_messages_with_content_v1 message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
JOIN messenger_stream_bindings membership ON membership.project_id=binding.project_id AND membership.user_uuid=binding.user_uuid
    AND membership.stream_uuid=placement.stream_uuid AND membership.active
JOIN messenger_streams stream ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
JOIN messenger_topics topic ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid;
"""

# Keep sparse facts available to selective page predicates, while making
# history lookups depend on the selected binding/placement. A newly imported
# project can be estimated as one row until ANALYZE; unconstrained join order
# can otherwise rescan its entire history inside nested loops.
# Project and public identity come directly from the binding so request filters
# reach its indexes instead of stopping at the LATERAL limits.
MESSAGE_VIEW = (
    PREVIOUS_MESSAGE_VIEW.replace(
        "SELECT binding.uuid AS binding_uuid,placement.uuid,",
        "SELECT binding.uuid AS binding_uuid,binding.placement_uuid AS uuid,",
    )
    .replace(
        "binding.user_uuid,placement.project_id,",
        "binding.user_uuid,binding.project_id,",
    )
    .replace(
        "JOIN messenger_message_placements placement ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid",
        """JOIN LATERAL (
    SELECT * FROM messenger_message_placements
    WHERE project_id=binding.project_id AND uuid=binding.placement_uuid LIMIT 1
) placement ON true""",
    )
    .replace(
        "JOIN messenger_messages_with_content_v1 message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid",
        """JOIN LATERAL (
    SELECT * FROM messenger_messages_with_content_v1
    WHERE project_id=placement.project_id AND uuid=placement.message_uuid LIMIT 1
) message ON true""",
    )
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0289-Expose-sparse-message-flags-as-indexed-joins-92e690.py"]

    @property
    def migration_id(self):
        return "cbfed6b8-b954-446c-b1b1-ebc4d671a438"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(MESSAGE_VIEW)

    def downgrade(self, session):
        session.execute(PREVIOUS_MESSAGE_VIEW)


migration_step = MigrationStep()
