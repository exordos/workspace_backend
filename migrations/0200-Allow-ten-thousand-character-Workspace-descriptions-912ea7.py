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


CAPTURE_AND_DROP_VIEWS = """
CREATE TEMPORARY TABLE workspace_description_dependent_views (
    schema_name TEXT NOT NULL,
    view_name TEXT NOT NULL,
    create_order INTEGER NOT NULL,
    definition TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    access_control ACLITEM[]
) ON COMMIT DROP;

INSERT INTO workspace_description_dependent_views (
    schema_name, view_name, create_order, definition, owner_name,
    access_control
)
SELECT namespace.nspname, relation.relname, requested.create_order,
       pg_get_viewdef(relation.oid, true),
       pg_get_userbyid(relation.relowner), relation.relacl
FROM (
    VALUES
        ('m_workspace_user_streams', 1),
        ('m_workspace_visible_files_v1', 1),
        ('messenger_api_user_streams_v1', 1),
        ('m_folder_all_items_view', 2),
        ('m_folder_channel_items_view', 2),
        ('m_folder_items_created_view', 2),
        ('m_folder_private_items_view', 2),
        ('m_folders_view', 2),
        ('m_folder_all_view', 3),
        ('m_folder_channels_view', 3),
        ('m_folder_created_view', 3),
        ('m_folder_personal_view', 3)
) AS requested(view_name, create_order)
JOIN pg_class AS relation ON relation.relname = requested.view_name
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public' AND relation.relkind = 'v';

DO $capture_guard$
BEGIN
    IF (
        SELECT count(*) FROM workspace_description_dependent_views
    ) <> 12 THEN
        RAISE EXCEPTION 'Workspace description migration view set is incomplete';
    END IF;
END;
$capture_guard$;

DROP VIEW m_workspace_user_streams CASCADE;
DROP VIEW m_workspace_visible_files_v1 CASCADE;
DROP VIEW messenger_api_user_streams_v1 CASCADE;
"""

RECREATE_VIEWS = """
DO $recreate_views$
DECLARE
    saved_view RECORD;
    saved_grant RECORD;
BEGIN
    FOR saved_view IN
        SELECT * FROM workspace_description_dependent_views
        ORDER BY create_order, view_name
    LOOP
        EXECUTE format(
            'CREATE VIEW %I.%I AS %s',
            saved_view.schema_name,
            saved_view.view_name,
            saved_view.definition
        );
        EXECUTE format(
            'ALTER VIEW %I.%I OWNER TO %I',
            saved_view.schema_name,
            saved_view.view_name,
            saved_view.owner_name
        );
        FOR saved_grant IN
            SELECT privilege_type, grantee
            FROM aclexplode(saved_view.access_control)
        LOOP
            EXECUTE format(
                'GRANT %s ON TABLE %I.%I TO %s',
                saved_grant.privilege_type,
                saved_view.schema_name,
                saved_view.view_name,
                CASE
                    WHEN saved_grant.grantee = 0 THEN 'PUBLIC'
                    ELSE quote_ident(pg_get_userbyid(saved_grant.grantee))
                END
            );
        END LOOP;
    END LOOP;
END;
$recreate_views$;
"""

UPGRADE = (
    CAPTURE_AND_DROP_VIEWS
    + """
ALTER TABLE catalog_services
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE workspace_streams
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE m_workspace_streams
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE m_workspace_files
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE messenger_streams
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE workspace_v3.streams
    ALTER COLUMN description TYPE VARCHAR(10000);
ALTER TABLE workspace_v3.files
    ALTER COLUMN description TYPE VARCHAR(10000);
"""
    + RECREATE_VIEWS
)

DOWNGRADE = (
    CAPTURE_AND_DROP_VIEWS
    + """
ALTER TABLE catalog_services
    ALTER COLUMN description TYPE VARCHAR(255);
ALTER TABLE workspace_streams
    ALTER COLUMN description TYPE VARCHAR(255);
ALTER TABLE m_workspace_streams
    ALTER COLUMN description TYPE VARCHAR(255);
ALTER TABLE m_workspace_files
    ALTER COLUMN description TYPE VARCHAR(255);
ALTER TABLE messenger_streams
    ALTER COLUMN description TYPE VARCHAR(1024);
ALTER TABLE workspace_v3.streams
    ALTER COLUMN description TYPE VARCHAR(255);
ALTER TABLE workspace_v3.files
    ALTER COLUMN description TYPE VARCHAR(255);
"""
    + RECREATE_VIEWS
)


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0199-Index-provider-user-project-discovery-117e2a.py"]

    @property
    def migration_id(self):
        return "912ea7f0-905e-49cd-822d-c86b75c1b7da"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE)

    def downgrade(self, session):
        session.execute(DOWNGRADE)


migration_step = MigrationStep()
