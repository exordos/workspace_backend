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
            "0235-Synchronize-Zulip-Unicode-reactions-bidirectionally-19928b.py"
        ]

    @property
    def migration_id(self):
        return "50c3535e-a1e6-457d-99ac-d83754997e90"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute("""
            CREATE FUNCTION bridge_zulip_v5_skip_reconciliation_message_change()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE
                batch text := current_setting('workspace.zulip_reconciliation_batch', true);
            BEGIN
                IF NULLIF(batch, '') IS NOT NULL
                   AND batch::jsonb ? (NEW.project_id::text || ':' || NEW.message_uuid::text) THEN
                    RETURN NULL;
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER bridge_zulip_v5_skip_reconciliation_message_change
            BEFORE INSERT OR UPDATE ON bridge_zulip_v5_message_changes
            FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_skip_reconciliation_message_change();
            CREATE TRIGGER messenger_skip_zulip_reconciliation_message_change
            BEFORE INSERT OR UPDATE ON messenger_message_changes
            FOR EACH ROW EXECUTE FUNCTION bridge_zulip_v5_skip_reconciliation_message_change();
        """)

    def downgrade(self, session):
        session.execute("""
            DROP TRIGGER messenger_skip_zulip_reconciliation_message_change
                ON messenger_message_changes;
            DROP TRIGGER bridge_zulip_v5_skip_reconciliation_message_change
                ON bridge_zulip_v5_message_changes;
            DROP FUNCTION bridge_zulip_v5_skip_reconciliation_message_change();
        """)


migration_step = MigrationStep()
