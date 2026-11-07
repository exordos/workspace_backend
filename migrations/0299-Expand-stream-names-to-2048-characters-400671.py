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


def _grants(session, name):
    return session.execute(
        """SELECT NULL::text AS column_name,
                  CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END AS grantee,
                  acl.privilege_type,acl.is_grantable,
                  acl.grantor=relation.relowner AS owner_grant
           FROM pg_class relation,
                aclexplode(COALESCE(relation.relacl,acldefault('r',relation.relowner))) acl
           WHERE relation.oid=%s::regclass
           UNION ALL
           SELECT quote_ident(attribute.attname),
                  CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END,
                  acl.privilege_type,acl.is_grantable,
                  acl.grantor=relation.relowner
           FROM pg_attribute attribute
                JOIN pg_class relation ON relation.oid=attribute.attrelid,
                aclexplode(attribute.attacl) acl
           WHERE attribute.attrelid=%s::regclass
             AND attribute.attnum>0 AND NOT attribute.attisdropped""",
        (name, name),
    ).fetchall()


def _alter_name(session, length, *, expand_view=True):
    if (
        length == 255
        and session.execute(
            "SELECT 1 FROM messenger_streams WHERE char_length(name)>255 LIMIT 1"
        ).fetchone()
    ):
        raise ValueError("Cannot narrow stream names longer than 255 characters")
    views = session.execute(
        """
        WITH RECURSIVE dependent_views AS (
            SELECT rewrite.ev_class AS view_oid, 0 AS depth
            FROM pg_depend AS dependency
            JOIN pg_rewrite AS rewrite ON rewrite.oid = dependency.objid
            JOIN pg_attribute AS column_
              ON column_.attrelid = dependency.refobjid
             AND column_.attnum = dependency.refobjsubid
            WHERE dependency.refobjid = 'messenger_streams'::regclass
              AND column_.attname = 'name'
            UNION ALL
            SELECT rewrite.ev_class, parent.depth + 1
            FROM dependent_views AS parent
            JOIN pg_depend AS dependency ON dependency.refobjid = parent.view_oid
            JOIN pg_rewrite AS rewrite ON rewrite.oid = dependency.objid
            WHERE rewrite.ev_class <> parent.view_oid
        )
        SELECT relation.oid::regclass::text AS name,
               pg_get_viewdef(relation.oid) AS definition,relation.reloptions,
               quote_ident(pg_get_userbyid(relation.relowner)) AS owner
        FROM dependent_views JOIN pg_class relation ON relation.oid=view_oid
        GROUP BY relation.oid
        ORDER BY MAX(depth), name
        """
    ).fetchall()
    grants = {view["name"]: _grants(session, view["name"]) for view in views}
    if any(not grant["owner_grant"] for values in grants.values() for grant in values):
        raise ValueError("Cannot recreate dependent views with delegated view grants")
    for view in reversed(views):
        session.execute(f"DROP VIEW {view['name']}")
    session.execute(
        f"ALTER TABLE messenger_streams ALTER COLUMN name TYPE VARCHAR({length})"
    )
    for view in views:
        definition = view["definition"]
        if expand_view and view["name"] == "messenger_api_user_streams_v1":
            previous_length = 255 if length == 2048 else 2048
            definition = definition.replace(
                f"::character varying({previous_length}) AS name",
                f"::character varying({length}) AS name",
            )
        options = (
            " WITH (" + ",".join(view["reloptions"]) + ")" if view["reloptions"] else ""
        )
        session.execute(f"CREATE VIEW {view['name']}{options} AS {definition}", None)
        session.execute(f"ALTER VIEW {view['name']} OWNER TO {view['owner']}")
        for grant in _grants(session, view["name"]):
            session.execute(f"REVOKE ALL ON {view['name']} FROM {grant['grantee']}")
        for grant in grants[view["name"]]:
            option = " WITH GRANT OPTION" if grant["is_grantable"] else ""
            column = f" ({grant['column_name']})" if grant["column_name"] else ""
            session.execute(
                f"GRANT {grant['privilege_type']}{column} ON {view['name']} "
                f"TO {grant['grantee']}{option}"
            )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0298-Restrict-Zulip-default-labels-to-private-conversations-141e3a.py"
        ]

    @property
    def migration_id(self):
        return "4006714d-1255-4ed0-a254-bdd9da61fd63"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        _alter_name(session, 2048)

    def downgrade(self, session):
        _alter_name(session, 255)


migration_step = MigrationStep()
