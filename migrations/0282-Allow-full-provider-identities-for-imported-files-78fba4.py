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


TABLES = ("m_workspace_files", "zulip_entity_links")


def _grants(session, name):
    return session.execute(
        """SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END AS grantee,
                  acl.privilege_type,acl.is_grantable
           FROM pg_class relation,
                aclexplode(COALESCE(relation.relacl,acldefault('r',relation.relowner))) acl
           WHERE relation.oid=%s::regclass""",
        (name,),
    ).fetchall()


def _alter_identity(session, *, upgrade):
    if (
        not upgrade
        and session.execute(
            "SELECT 1 FROM m_workspace_files WHERE length(provider_identity)>255 "
            "UNION ALL SELECT 1 FROM zulip_entity_links "
            "WHERE length(provider_identity)>255 LIMIT 1"
        ).fetchone()
    ):
        raise ValueError("Cannot narrow file provider identities containing long paths")
    views = session.execute(
        """
        WITH RECURSIVE dependent_views AS (
            SELECT rewrite.ev_class AS oid,0 AS depth
            FROM pg_depend dependency
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            JOIN pg_attribute column_ ON column_.attrelid=dependency.refobjid
              AND column_.attnum=dependency.refobjsubid
            WHERE dependency.refobjid=ANY(%s::regclass[])
              AND column_.attname='provider_identity'
            UNION ALL
            SELECT rewrite.ev_class,parent.depth+1
            FROM dependent_views parent
            JOIN pg_depend dependency ON dependency.refobjid=parent.oid
            JOIN pg_rewrite rewrite ON rewrite.oid=dependency.objid
            WHERE rewrite.ev_class<>parent.oid
        )
        SELECT relation.oid::regclass::text AS name,
               pg_get_viewdef(relation.oid) AS definition,relation.reloptions,
               quote_ident(pg_get_userbyid(relation.relowner)) AS owner
        FROM dependent_views dependent JOIN pg_class relation ON relation.oid=dependent.oid
        GROUP BY relation.oid ORDER BY max(dependent.depth),name
    """,
        (list(TABLES),),
    ).fetchall()
    grants = {view["name"]: _grants(session, view["name"]) for view in views}
    for view in reversed(views):
        session.execute(f"DROP VIEW {view['name']}")
    identity_type = "TEXT" if upgrade else "VARCHAR(255)"
    for table in TABLES:
        session.execute(
            f"ALTER TABLE {table} ALTER COLUMN provider_identity TYPE {identity_type}"
        )
    for view in views:
        options = (
            " WITH (" + ",".join(view["reloptions"]) + ")" if view["reloptions"] else ""
        )
        session.execute(
            f"CREATE VIEW {view['name']}{options} AS {view['definition']}", None
        )
        session.execute(f"ALTER VIEW {view['name']} OWNER TO {view['owner']}")
        for grant in _grants(session, view["name"]):
            session.execute(f"REVOKE ALL ON {view['name']} FROM {grant['grantee']}")
        for grant in grants[view["name"]]:
            option = " WITH GRANT OPTION" if grant["is_grantable"] else ""
            session.execute(
                f"GRANT {grant['privilege_type']} ON {view['name']} "
                f"TO {grant['grantee']}{option}"
            )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0281-Join-recipient-preview-and-Zulip-routing-migrations-5db781.py"
        ]

    @property
    def migration_id(self):
        return "78fba40c-bd00-47ac-80c1-56d406288564"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _alter_identity(session, upgrade=True)

    def downgrade(self, session):
        _alter_identity(session, upgrade=False)


migration_step = MigrationStep()
