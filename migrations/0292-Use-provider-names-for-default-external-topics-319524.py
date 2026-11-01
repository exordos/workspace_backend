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
            "0291-Import-all-Telegram-dialogs-and-full-history-automatically-253267.py"
        ]

    @property
    def migration_id(self):
        return "319524e3-9a5e-4e66-9f92-901a26ad2e2c"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        self.rename(session, "General", None)

    def downgrade(self, session):
        self.rename(session, None, "General")

    def rename(self, session, previous, target):
        session.execute(
            """
            UPDATE messenger_topics topic SET name=coalesce(%s,topic.source_name),updated_at=now()
            FROM messenger_streams stream
            WHERE stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
              AND topic.name=coalesce(%s,topic.source_name)
              AND ((topic.source_name='telegram' AND topic.provider_identity=stream.provider_identity||':topic:0')
                OR (topic.source_name='zulip' AND topic.provider_identity=stream.provider_identity||':'))
        """,
            (target, previous),
        )


migration_step = MigrationStep()
