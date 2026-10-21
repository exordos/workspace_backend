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


DESCRIPTION_TYPES = {
    "bridge_zulip_v5_channels": "TEXT",
    "catalog_services": "VARCHAR(255)",
    "m_workspace_files": "VARCHAR(255)",
    "m_workspace_streams": "VARCHAR(255)",
    "messenger_streams": "VARCHAR(1024)",
    "workspace_streams": "VARCHAR(255)",
}


def _alter_descriptions(session, *, upgrade):
    views = session.execute(
        """
        WITH RECURSIVE dependent_views AS (
            SELECT rewrite.ev_class AS view_oid, 0 AS depth
            FROM pg_depend AS dependency
            JOIN pg_rewrite AS rewrite ON rewrite.oid = dependency.objid
            JOIN pg_attribute AS column_
              ON column_.attrelid = dependency.refobjid
             AND column_.attnum = dependency.refobjsubid
            WHERE dependency.refobjid = ANY(%s::regclass[])
              AND column_.attname = 'description'
            UNION ALL
            SELECT rewrite.ev_class, parent.depth + 1
            FROM dependent_views AS parent
            JOIN pg_depend AS dependency ON dependency.refobjid = parent.view_oid
            JOIN pg_rewrite AS rewrite ON rewrite.oid = dependency.objid
            WHERE rewrite.ev_class <> parent.view_oid
        )
        SELECT view_oid::regclass::text AS name,
               pg_get_viewdef(view_oid) AS definition
        FROM dependent_views
        GROUP BY view_oid
        ORDER BY MAX(depth), name
        """,
        (list(DESCRIPTION_TYPES),),
    ).fetchall()
    for view in reversed(views):
        session.execute(f"DROP VIEW {view['name']}")
    for table, original_type in DESCRIPTION_TYPES.items():
        description_type = "VARCHAR(10000)" if upgrade else original_type
        session.execute(
            f"ALTER TABLE {table} ALTER COLUMN description TYPE {description_type}"
        )
    for view in views:
        session.execute(f"CREATE VIEW {view['name']} AS {view['definition']}", None)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0213-Allow-global-background-tasks-without-a-project-3ee2ca.py"
        ]

    @property
    def migration_id(self):
        return "fa3fa93f-c7e5-4863-9ae5-1531280d1592"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _alter_descriptions(session, upgrade=True)

    def downgrade(self, session):
        _alter_descriptions(session, upgrade=False)


migration_step = MigrationStep()
