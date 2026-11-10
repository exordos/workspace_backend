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
            "0301-Store-native-Messenger-poll-votes-and-revisions-aa94d6.py"
        ]

    @property
    def migration_id(self):
        return "40ec4a15-596c-4c7f-8765-53264050ec3e"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""CREATE TABLE zulip_poll_observations (
            project_id uuid NOT NULL,
            message_uuid uuid NOT NULL,
            provider_uuid uuid NOT NULL REFERENCES messenger_providers(uuid),
            submessage_id bigint NOT NULL CHECK (submessage_id>=0),
            PRIMARY KEY(project_id,message_uuid,provider_uuid),
            FOREIGN KEY(project_id,message_uuid) REFERENCES messenger_messages(project_id,uuid) ON DELETE CASCADE
        )""")
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        old = "IF row_->'payload'->>'kind'<>'markdown' THEN"
        if definition.count(old) != 1:
            raise RuntimeError("Unexpected Zulip outgoing capture definition")
        session.execute(
            definition.replace(
                old, "IF row_->'payload'->>'kind' NOT IN ('markdown','poll') THEN"
            ).replace(
                "'message_uuid',message_,'stream_uuid',stream_",
                "'message_uuid',message_,'placement_uuid',placement_.uuid,'stream_uuid',stream_",
            )
        )

    def downgrade(self, session):
        if session.execute(
            "SELECT 1 FROM messenger_messages m JOIN messenger_message_contents c ON c.uuid=m.content_uuid WHERE m.source_name='zulip' AND c.workspace_content->>'kind'='poll' LIMIT 1"
        ).fetchone():
            raise RuntimeError("Cannot downgrade while synchronized Zulip polls exist")
        session.execute("DROP TABLE zulip_poll_observations")
        definition = session.execute(
            "SELECT pg_get_functiondef('zulip_capture_outgoing()'::regprocedure) AS sql"
        ).fetchone()["sql"]
        session.execute(
            definition.replace(
                "IF row_->'payload'->>'kind' NOT IN ('markdown','poll') THEN",
                "IF row_->'payload'->>'kind'<>'markdown' THEN",
            )
        )


migration_step = MigrationStep()
