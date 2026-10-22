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


REACTIONS_BRANCH = "ELSIF TG_TABLE_NAME='bridge_zulip_v5_reactions' THEN"
PROJECTED_FACT_GUARD = """
                    -- fact_uuid is a derived Workspace link. Neither linking it
                    -- nor rewriting the same row is a fresh provider observation.
                    -- Keep newer observed_at/event_id values as invalidations,
                    -- including real events whose reaction value did not change.
                    IF TG_OP='UPDATE' AND source-'fact_uuid'=previous-'fact_uuid' THEN
                        RETURN NULL;
                    END IF;"""
FUNCTION_SQL = "SELECT pg_get_functiondef('bridge_zulip_v5_track_history_source()'::regprocedure) AS definition"


def replace_guard(session, before, after):
    definition = session.execute(FUNCTION_SQL).fetchone()["definition"]
    if definition.count(before) != 1:
        raise RuntimeError("Unexpected Zulip history source trigger definition")
    session.execute(definition.replace(before, after, 1))


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0252-Scope-shared-history-snapshot-invalidation-04417c.py"]

    @property
    def migration_id(self):
        return "68942ccd-a1ad-43c8-998c-0a877070bbab"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        replace_guard(
            session, REACTIONS_BRANCH, REACTIONS_BRANCH + PROJECTED_FACT_GUARD
        )

    def downgrade(self, session):
        replace_guard(
            session, REACTIONS_BRANCH + PROJECTED_FACT_GUARD, REACTIONS_BRANCH
        )


migration_step = MigrationStep()
