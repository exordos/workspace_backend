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


USER_STREAM_VIEW = "messenger_api_user_streams_v1"
STATUS_COLUMN = "    stream.status\n"
KEY_SHARING_COLUMN = ",\n    stream.allow_key_sharing_by_holders"


def _view(session):
    return session.execute(
        """SELECT pg_get_viewdef(oid) AS definition, reloptions,
                  quote_ident(pg_get_userbyid(relowner)) AS owner
           FROM pg_class WHERE oid=%s::regclass""",
        (USER_STREAM_VIEW,),
    ).fetchone()


def _view_options(view):
    return " WITH (" + ",".join(view["reloptions"]) + ")" if view["reloptions"] else ""


def _grants(session):
    return session.execute(
        """SELECT NULL::text AS column_name,
                  CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END AS grantee,
                  acl.privilege_type, acl.is_grantable,
                  acl.grantor=relation.relowner AS owner_grant
           FROM pg_class relation,
                aclexplode(COALESCE(relation.relacl,acldefault('r',relation.relowner))) acl
           WHERE relation.oid=%s::regclass
           UNION ALL
           SELECT quote_ident(attribute.attname),
                  CASE WHEN acl.grantee=0 THEN 'PUBLIC'
                       ELSE quote_ident(pg_get_userbyid(acl.grantee)) END,
                  acl.privilege_type, acl.is_grantable,
                  acl.grantor=relation.relowner
           FROM pg_attribute attribute
                JOIN pg_class relation ON relation.oid=attribute.attrelid,
                aclexplode(attribute.attacl) acl
           WHERE attribute.attrelid=%s::regclass
             AND attribute.attnum>0 AND NOT attribute.attisdropped
             AND attribute.attname<>'allow_key_sharing_by_holders'""",
        (USER_STREAM_VIEW, USER_STREAM_VIEW),
    ).fetchall()


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0300-Preserve-Zulip-history-during-subscription-recovery-82877f.py"
        ]

    @property
    def migration_id(self):
        return "58fcbcb0-e96a-4168-8d81-b46b7c3a44f3"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(
            "ALTER TABLE messenger_streams "
            "ADD COLUMN allow_key_sharing_by_holders boolean NOT NULL DEFAULT false"
        )
        view = _view(session)
        definition = view["definition"]
        if STATUS_COLUMN not in definition:
            raise ValueError("Expected the user stream view to end with stream.status")
        definition = definition.replace(
            STATUS_COLUMN, STATUS_COLUMN.rstrip() + KEY_SHARING_COLUMN + "\n", 1
        )
        session.execute(
            f"CREATE OR REPLACE VIEW {USER_STREAM_VIEW}{_view_options(view)} AS {definition}"
        )

    def downgrade(self, session):
        view = _view(session)
        grants = _grants(session)
        if any(not grant["owner_grant"] for grant in grants):
            raise ValueError(
                "Cannot recreate the stream view with delegated view grants"
            )
        definition = view["definition"].replace(KEY_SHARING_COLUMN, "", 1)
        session.execute(f"DROP VIEW {USER_STREAM_VIEW}")
        session.execute(
            f"CREATE VIEW {USER_STREAM_VIEW}{_view_options(view)} AS {definition}"
        )
        session.execute(f"ALTER VIEW {USER_STREAM_VIEW} OWNER TO {view['owner']}")
        for grant in _grants(session):
            session.execute(f"REVOKE ALL ON {USER_STREAM_VIEW} FROM {grant['grantee']}")
        for grant in grants:
            option = " WITH GRANT OPTION" if grant["is_grantable"] else ""
            column = f" ({grant['column_name']})" if grant["column_name"] else ""
            session.execute(
                f"GRANT {grant['privilege_type']}{column} ON {USER_STREAM_VIEW} "
                f"TO {grant['grantee']}{option}"
            )
        session.execute(
            "ALTER TABLE messenger_streams DROP COLUMN allow_key_sharing_by_holders"
        )


migration_step = MigrationStep()
