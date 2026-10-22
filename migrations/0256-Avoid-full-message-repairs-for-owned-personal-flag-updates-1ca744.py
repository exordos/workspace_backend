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


SOURCE_TRACKING_SQL = """
CREATE OR REPLACE FUNCTION bridge_zulip_v5_track_message_changes()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    {guard}
    IF TG_OP <> 'INSERT' THEN
        PERFORM bridge_zulip_v5_mark_message_changed(OLD.project_id, OLD.message_uuid);
    END IF;
    IF TG_OP <> 'DELETE' AND (
        TG_OP <> 'UPDATE'
        OR (NEW.project_id, NEW.message_uuid) IS DISTINCT FROM
           (OLD.project_id, OLD.message_uuid)
    ) THEN
        PERFORM bridge_zulip_v5_mark_message_changed(NEW.project_id, NEW.message_uuid);
    END IF;
    RETURN NULL;
END;
$$;
"""

STATE_TRACKING_SQL = """
CREATE OR REPLACE FUNCTION messenger_mark_zulip_user_message_changed()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target record;
BEGIN
    {guard}
    FOR target IN
        SELECT OLD.project_id AS project_id, OLD.placement_uuid AS placement_uuid WHERE TG_OP <> 'INSERT'
        UNION SELECT NEW.project_id, NEW.placement_uuid WHERE TG_OP <> 'DELETE'
    LOOP
        INSERT INTO messenger_message_changes (project_id, message_uuid)
        SELECT placement.project_id, placement.message_uuid
        FROM messenger_message_placements AS placement
        JOIN messenger_messages AS message ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
        WHERE placement.project_id = target.project_id AND placement.uuid = target.placement_uuid
          AND placement.uuid = messenger_uuid_v5(message.uuid, 'zulip-placement') AND message.source_name = 'zulip'
        ON CONFLICT (project_id, message_uuid) DO UPDATE SET revision = EXCLUDED.revision;
    END LOOP;
    RETURN NULL;
END;
$$;
"""


def flag_guard(columns):
    ignored = ",".join("'" + name + "'" for name in columns)
    return f"""
    IF TG_OP='UPDATE'
       AND current_setting('workspace.owned_mutations',true)='on'
       AND (to_jsonb(NEW)-ARRAY[{ignored}]) IS NOT DISTINCT FROM
           (to_jsonb(OLD)-ARRAY[{ignored}]) THEN
        RETURN NULL;
    END IF;
    """


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0255-Bound-planning-for-scoped-Zulip-flag-probes-9aa152.py"]

    @property
    def migration_id(self):
        return "1ca7447d-0358-4fdd-83ef-65e8c2763d64"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Flag journals already own these updates. Do not re-run content,
        # placement and per-recipient reconciliation for a personal flag.
        # Any other changed column still invalidates the full message.
        session.execute(
            SOURCE_TRACKING_SQL.format(
                guard=flag_guard(
                    ["read", "starred", "updated_at"],
                )
            )
        )
        session.execute(
            STATE_TRACKING_SQL.format(
                guard=flag_guard(
                    [
                        "read_at",
                        "starred",
                        "pinned",
                        "read_updated_at",
                        "starred_updated_at",
                        "updated_at",
                    ],
                )
            )
        )

    def downgrade(self, session):
        session.execute(SOURCE_TRACKING_SQL.format(guard=""))
        session.execute(STATE_TRACKING_SQL.format(guard=""))


migration_step = MigrationStep()
