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
            "0293-Rebuild-Telegram-channel-and-personal-folder-classification-4a7e78.py"
        ]

    @property
    def migration_id(self):
        return "015446b4-a4e6-4656-957f-18e9747cd477"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Buffered pages contain normalized Markdown and cannot retain the old dialect.
        session.execute("DELETE FROM telegram_history_batches")
        session.execute("""
            UPDATE telegram_peer_state SET history_before_id=0,history_done=false,
                history_started_at=NULL,first_message_at=NULL,history_finished_at=NULL,
                imported_messages=0,imported_files=0,imported_bytes=0,
                fetch_seconds=0,merge_seconds=0,file_seconds=0,updated_at=now()
            WHERE history_before_id>0 OR imported_messages>0 OR history_done
        """)

    def downgrade(self, session):
        # Replayed progress cannot safely restore an older checkpoint. Messages and files remain.
        pass


migration_step = MigrationStep()
