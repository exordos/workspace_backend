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


ACTIVE_BINDING = "IF NOT NEW.active THEN"
VISIBLE_TOPIC = """IF NEW.deleted_at IS NOT NULL OR EXISTS (
        SELECT 1 FROM messenger_streams AS stream
        WHERE stream.project_id = NEW.project_id
          AND stream.uuid = NEW.stream_uuid
          AND stream.deleted_at IS NOT NULL
    ) THEN"""
VISIBLE_STREAM_BINDING = """IF NOT NEW.active OR EXISTS (
        SELECT 1 FROM messenger_streams AS stream
        WHERE stream.project_id = NEW.project_id
          AND stream.uuid = NEW.stream_uuid
          AND stream.deleted_at IS NOT NULL
    ) THEN"""
TOPIC_BINDING_LOOKUP = "SELECT is_done INTO done_value"
VISIBLE_TOPIC_BINDING_LOOKUP = """IF EXISTS (
        SELECT 1 FROM messenger_topics AS topic
        JOIN messenger_streams AS stream
          ON stream.project_id = topic.project_id
         AND stream.uuid = topic.stream_uuid
        WHERE topic.project_id = NEW.project_id
          AND topic.uuid = NEW.topic_uuid
          AND (topic.deleted_at IS NOT NULL OR stream.deleted_at IS NOT NULL)
    ) THEN
        RETURN NEW;
    END IF;
    SELECT is_done INTO done_value"""


def _update_binding_mirror(session, *, upgrade):
    # SET NULL actions can update an active binding while its stream is hidden.
    # Its legacy parent has already been removed, so do not recreate the binding.
    for name, original, guarded in (
        (
            "messenger_v2_mirror_binding_to_legacy",
            ACTIVE_BINDING,
            VISIBLE_STREAM_BINDING,
        ),
        (
            "messenger_v2_mirror_topic_binding_to_legacy",
            TOPIC_BINDING_LOOKUP,
            VISIBLE_TOPIC_BINDING_LOOKUP,
        ),
        (
            "messenger_v2_mirror_topic_to_legacy",
            "IF NEW.deleted_at IS NOT NULL THEN",
            VISIBLE_TOPIC,
        ),
    ):
        definition = session.execute(
            "SELECT pg_get_functiondef(%s::regprocedure) AS definition",
            (f"{name}()",),
        ).fetchone()["definition"]
        source, target = (original, guarded) if upgrade else (guarded, original)
        session.execute(definition.replace(source, target), ())


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0187-Process-only-Markdown-message-content-90458e.py"]

    @property
    def migration_id(self):
        return "fd5982d0-05e3-4174-ba8e-7482bd9fc9cf"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _update_binding_mirror(session, upgrade=True)

    def downgrade(self, session):
        _update_binding_mirror(session, upgrade=False)


migration_step = MigrationStep()
