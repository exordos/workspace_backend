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


FUNCTION_SQL = "SELECT pg_get_functiondef('messenger_track_content_version()'::regprocedure) AS definition"
PREVIOUS_GUARD = """NEW.content_updated_at IS NULL
                OR (NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at
                    AND current_setting('workspace.zulip_incoming',true) IS DISTINCT FROM 'on')"""
PROVIDER_GUARD = """NEW.content_updated_at IS NULL
                OR (NEW.content_updated_at IS NOT DISTINCT FROM OLD.content_updated_at
                    AND current_setting('workspace.zulip_supplied_content_time',true)
                        IS DISTINCT FROM (NEW.project_id::text || ':' || NEW.uuid::text))"""
PREVIOUS_RETURN = "    RETURN NEW;"
PROVIDER_RETURN = """    IF current_setting('workspace.zulip_supplied_content_time',true)
        = (NEW.project_id::text || ':' || NEW.uuid::text) THEN
        PERFORM set_config('workspace.zulip_supplied_content_time','',true);
    END IF;
    RETURN NEW;"""

SUPPLIED_TIME_SQL = """
CREATE FUNCTION messenger_mark_supplied_content_time()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.content_updated_at IS NOT NULL
       AND current_setting('workspace.zulip_incoming',true)='on' THEN
        PERFORM set_config('workspace.zulip_supplied_content_time',
            NEW.project_id::text || ':' || NEW.uuid::text,true);
    END IF;
    RETURN NEW;
END;
$$;
-- PostgreSQL orders BEFORE triggers by name. Mark this row before tracking it.
CREATE TRIGGER messenger_supplied_content_time
BEFORE UPDATE OF content_updated_at ON messenger_messages
FOR EACH ROW EXECUTE FUNCTION messenger_mark_supplied_content_time();
"""


def replace_guard(session, before, after, previous_return, next_return):
    definition = session.execute(FUNCTION_SQL).fetchone()["definition"]
    if definition.count(before) != 1 or definition.count(previous_return) != 1:
        raise RuntimeError("Unexpected message content version trigger definition")
    session.execute(
        definition.replace(before, after, 1).replace(previous_return, next_return, 1)
    )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        # The published pre-rebase 0301 had the same UUID as 0303. Its newly
        # added dependencies are not visited when that UUID is already applied.
        self._depends = [
            "0302-Synchronize-compatible-Zulip-polls-40ec4a.py",
            "0303-Preserve-provider-edit-times-during-Zulip-conversion-8ecfb7.py"
        ]

    @property
    def migration_id(self):
        return "65fd64ef-7f79-4c42-a6ae-784e39f28e24"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        replace_guard(
            session, PREVIOUS_GUARD, PROVIDER_GUARD, PREVIOUS_RETURN, PROVIDER_RETURN
        )
        session.execute(SUPPLIED_TIME_SQL)

    def downgrade(self, session):
        session.execute(
            "DROP TRIGGER messenger_supplied_content_time ON messenger_messages;"
            "DROP FUNCTION messenger_mark_supplied_content_time()"
        )
        replace_guard(
            session, PROVIDER_GUARD, PREVIOUS_GUARD, PROVIDER_RETURN, PREVIOUS_RETURN
        )


migration_step = MigrationStep()
