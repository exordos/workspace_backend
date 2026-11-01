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


# New source rows can dwarf their tables' last analyzed size. Replan INSERT
# repair per batch and keep large batches out of nested loops whose outer
# history relation may still be estimated as one row. Function-local planner
# settings are restored by PostgreSQL when the function returns.
PREVIOUS_INSERT_REPAIR_SQL = """
CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=placement.message_uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_placements AS placement
    JOIN messenger_messages AS message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (placement.message_uuid,placement.stream_uuid,placement.topic_uuid,message.created_at);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_dates()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=message.uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_messages AS message
    JOIN messenger_message_placements AS placement ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (message.uuid,placement.stream_uuid,placement.topic_uuid,message.created_at);
    RETURN NULL;
END;
$$;
"""

INSERT_REPAIR_SQL = """
CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_scope()
RETURNS trigger LANGUAGE plpgsql SET enable_nestloop=on AS $$
BEGIN
    IF (SELECT count(*) FROM new_placements) > 1000 THEN
        PERFORM set_config('enable_nestloop','off',true);
    END IF;
    EXECUTE $repair$
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=placement.message_uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_placements AS placement
    JOIN messenger_messages AS message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (placement.message_uuid,placement.stream_uuid,placement.topic_uuid,message.created_at)
    $repair$;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_insert_personal_preview_dates()
RETURNS trigger LANGUAGE plpgsql SET enable_nestloop=on AS $$
BEGIN
    IF (SELECT count(*) FROM new_messages) > 1000 THEN
        PERFORM set_config('enable_nestloop','off',true);
    END IF;
    EXECUTE $repair$
    UPDATE messenger_user_message_bindings AS binding
    SET preview_message_uuid=message.uuid,
        preview_stream_uuid=placement.stream_uuid,preview_topic_uuid=placement.topic_uuid,
        preview_created_at=message.created_at
    FROM new_messages AS message
    JOIN messenger_message_placements AS placement ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    WHERE binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
      AND (binding.preview_message_uuid,binding.preview_stream_uuid,binding.preview_topic_uuid,binding.preview_created_at)
          IS DISTINCT FROM (message.uuid,placement.stream_uuid,placement.topic_uuid,message.created_at)
    $repair$;
    RETURN NULL;
END;
$$;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0291-Select-message-page-keys-before-expanding-content-cd4508.py"
        ]

    @property
    def migration_id(self):
        return "b9410e11-2f42-47ae-a47e-99542f3f1753"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(INSERT_REPAIR_SQL)

    def downgrade(self, session):
        session.execute(PREVIOUS_INSERT_REPAIR_SQL)


migration_step = MigrationStep()
