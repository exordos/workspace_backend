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



SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_delivery_flags() RETURNS trigger AS $$
BEGIN
    {guard}
    INSERT INTO bridge_zulip_v5_flag_changes(project_id, state_uuid)
    SELECT DISTINCT project_id, state_uuid FROM bridge_zulip_v5_flag_targets
    WHERE project_id=NEW.project_id AND message_uuid=NEW.message_uuid
    ORDER BY 1,2
    ON CONFLICT (project_id, state_uuid) DO UPDATE SET revision=EXCLUDED.revision;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""
GUARD = """
    IF TG_OP='UPDATE' AND
       (NEW.project_id,NEW.message_uuid,NEW.stream_uuid,NEW.external_account_uuid,
        NEW.external_account_id,NEW.hostname,NEW.zulip_message_id)
       IS NOT DISTINCT FROM
       (OLD.project_id,OLD.message_uuid,OLD.stream_uuid,OLD.external_account_uuid,
        OLD.external_account_id,OLD.hostname,OLD.zulip_message_id) THEN
        RETURN NULL;
    END IF;
"""


class MigrationStep(migrations.AbstractMigrationStep):

    def __init__(self):
        self._depends = ["0269-Wake-owned-projections-when-provider-inputs-commit-a78f4b.py"]

    @property
    def migration_id(self):
        return "e9d6c8a2-0803-4adc-8452-bcf62142bf29"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(SQL.format(guard=GUARD))

    def downgrade(self, session):
        session.execute(SQL.format(guard=""))


migration_step = MigrationStep()
