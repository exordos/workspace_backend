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
        self._depends = ["0133-add-notification-setting-timestamps-52d0f8.py"]

    @property
    def migration_id(self):
        return "b9d39435-f461-45b8-aabb-771061953c15"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            """
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_reasoning_check",
                ADD CONSTRAINT "m_workspace_topic_summary_reasoning_check"
                    CHECK (
                        "summary_reasoning_effort" IS NULL
                        OR "summary_reasoning_effort" IN (
                            'off', 'minimal', 'low', 'medium', 'high'
                        )
                    );
            ALTER TABLE "m_workspace_topic_summary_jobs"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_job_reasoning_check",
                ADD CONSTRAINT "m_workspace_topic_summary_job_reasoning_check"
                    CHECK (
                        "reasoning_effort" IS NULL
                        OR "reasoning_effort" IN (
                            'off', 'minimal', 'low', 'medium', 'high'
                        )
                    );
            """
        )

    def downgrade(self, session):
        session.execute(
            """
            UPDATE "m_workspace_topic_summary_jobs"
            SET "reasoning_effort" = NULL
            WHERE "reasoning_effort" = 'off';
            UPDATE "m_workspace_stream_topics"
            SET "summary_reasoning_effort" = NULL
            WHERE "summary_reasoning_effort" = 'off';

            ALTER TABLE "m_workspace_topic_summary_jobs"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_job_reasoning_check",
                ADD CONSTRAINT "m_workspace_topic_summary_job_reasoning_check"
                    CHECK (
                        "reasoning_effort" IS NULL
                        OR "reasoning_effort" IN (
                            'minimal', 'low', 'medium', 'high'
                        )
                    );
            ALTER TABLE "m_workspace_stream_topics"
                DROP CONSTRAINT IF EXISTS
                    "m_workspace_topic_summary_reasoning_check",
                ADD CONSTRAINT "m_workspace_topic_summary_reasoning_check"
                    CHECK (
                        "summary_reasoning_effort" IS NULL
                        OR "summary_reasoning_effort" IN (
                            'minimal', 'low', 'medium', 'high'
                        )
                    );
            """
        )


migration_step = MigrationStep()
