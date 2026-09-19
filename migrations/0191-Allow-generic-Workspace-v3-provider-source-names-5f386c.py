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


UPGRADE = """
ALTER TABLE workspace_v3.users
    DROP CONSTRAINT users_source_check,
    ALTER COLUMN source TYPE VARCHAR(32),
    ADD CONSTRAINT users_source_check
        CHECK (source ~ '^[a-z][a-z0-9_-]{0,31}$');
ALTER TABLE workspace_v3.provider_consumers
    DROP CONSTRAINT provider_consumers_name_check,
    ADD CONSTRAINT provider_consumers_name_check
        CHECK (
            name ~ '^[a-z][a-z0-9_-]{0,31}$'
            AND name NOT IN ('native', 'iam')
        );
ALTER TABLE workspace_v3.streams
    DROP CONSTRAINT streams_source_name_check,
    ADD CONSTRAINT streams_source_name_check
        CHECK (source_name ~ '^[a-z][a-z0-9_-]{0,31}$');
ALTER TABLE workspace_v3.topics
    DROP CONSTRAINT topics_source_name_check,
    ADD CONSTRAINT topics_source_name_check
        CHECK (source_name ~ '^[a-z][a-z0-9_-]{0,31}$');
ALTER TABLE workspace_v3.messages
    DROP CONSTRAINT messages_source_name_check,
    ADD CONSTRAINT messages_source_name_check
        CHECK (source_name ~ '^[a-z][a-z0-9_-]{0,31}$');
ALTER TABLE workspace_v3.message_reactions
    DROP CONSTRAINT message_reactions_source_name_check,
    ADD CONSTRAINT message_reactions_source_name_check
        CHECK (source_name ~ '^[a-z][a-z0-9_-]{0,31}$');
"""

DOWNGRADE = """
ALTER TABLE workspace_v3.provider_consumers
    DROP CONSTRAINT provider_consumers_name_check,
    ADD CONSTRAINT provider_consumers_name_check
        CHECK (name <> '' AND name <> 'native');
ALTER TABLE workspace_v3.users
    DROP CONSTRAINT users_source_check,
    ADD CONSTRAINT users_source_check
        CHECK (source IN ('iam', 'zulip')),
    ALTER COLUMN source TYPE VARCHAR(16);
ALTER TABLE workspace_v3.streams
    DROP CONSTRAINT streams_source_name_check,
    ADD CONSTRAINT streams_source_name_check
        CHECK (source_name IN ('native', 'zulip'));
ALTER TABLE workspace_v3.topics
    DROP CONSTRAINT topics_source_name_check,
    ADD CONSTRAINT topics_source_name_check
        CHECK (source_name IN ('native', 'zulip'));
ALTER TABLE workspace_v3.messages
    DROP CONSTRAINT messages_source_name_check,
    ADD CONSTRAINT messages_source_name_check
        CHECK (source_name IN ('native', 'zulip'));
ALTER TABLE workspace_v3.message_reactions
    DROP CONSTRAINT message_reactions_source_name_check,
    ADD CONSTRAINT message_reactions_source_name_check
        CHECK (source_name IN ('native', 'zulip'));
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0190-Track-provider-source-modification-time-443f26.py"]

    @property
    def migration_id(self):
        return "5f386c79-ba31-490c-944c-d22a253050a4"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
