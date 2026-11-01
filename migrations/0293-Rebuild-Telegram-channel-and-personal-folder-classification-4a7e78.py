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
            "0292-Use-provider-names-for-default-external-topics-319524.py"
        ]

    @property
    def migration_id(self):
        return "4a7e78a5-2769-42e2-866d-63179542d0e4"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        self.rebuild(session)

    def downgrade(self, session):
        self.rebuild(session)

    def rebuild(self, session):
        session.execute("""
            INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload)
            SELECT DISTINCT messenger_uuid_v5(member.project_id,'telegram:folder-classification:'||member.user_uuid::text||':'||folder.folder_uuid::text),
                member.project_id,'folder_projection','user-folder',
                member.project_id::text||':'||member.user_uuid::text||':'||folder.folder_uuid::text,
                jsonb_build_object('source_kind','telegram.folder_classification','user_uuid',member.user_uuid,'folder_uuid',folder.folder_uuid)
            FROM messenger_stream_bindings member
            JOIN messenger_streams stream ON stream.project_id=member.project_id AND stream.uuid=member.stream_uuid
            JOIN messenger_user_folder_bindings folder ON folder.project_id=member.project_id AND folder.user_uuid=member.user_uuid
            WHERE stream.source_name='telegram' AND member.active AND folder.rule IN ('all_chats','personal','channels')
            ON CONFLICT(project_id,uuid) DO NOTHING
        """)


migration_step = MigrationStep()
