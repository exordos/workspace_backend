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


WORKSPACE_ACL = """
    SELECT files.uuid,files.project_id,files.name,files.description,
           files.user_uuid,files.stream_uuid,files.content_type,files.size_bytes,
           files.hash,files.created_at,files.updated_at,files.storage_type,
           files.storage_id,files.storage_object_id,files.acl_mode,
           bindings.user_uuid AS viewer_user_uuid,files.source_name,
           files.provider_uuid,files.provider_identity,files.status
    FROM m_workspace_files files
    JOIN messenger_stream_bindings bindings
      ON bindings.project_id=files.project_id
     AND bindings.stream_uuid=files.stream_uuid
    JOIN messenger_streams stream
      ON stream.project_id=files.project_id AND stream.uuid=files.stream_uuid
    WHERE files.acl_mode='stream' AND bindings.active AND stream.deleted_at IS NULL
    UNION
    SELECT files.uuid,files.project_id,files.name,files.description,
           files.user_uuid,files.stream_uuid,files.content_type,files.size_bytes,
           files.hash,files.created_at,files.updated_at,files.storage_type,
           files.storage_id,files.storage_object_id,files.acl_mode,
           files.user_uuid AS viewer_user_uuid,files.source_name,
           files.provider_uuid,files.provider_identity,files.status
    FROM m_workspace_files files WHERE files.acl_mode='owner'
    UNION
    SELECT files.uuid,files.project_id,files.name,files.description,
           files.user_uuid,files.stream_uuid,files.content_type,files.size_bytes,
           files.hash,files.created_at,files.updated_at,files.storage_type,
           files.storage_id,files.storage_object_id,files.acl_mode,
           NULL::uuid AS viewer_user_uuid,files.source_name,
           files.provider_uuid,files.provider_identity,files.status
    FROM m_workspace_files files WHERE files.acl_mode='public'
"""


def _replace_view(session, definition):
    options = session.execute(
        "SELECT reloptions FROM pg_class WHERE oid='m_workspace_visible_files_v1'::regclass"
    ).fetchone()["reloptions"]
    options_sql = " WITH (" + ",".join(options) + ")" if options else ""
    session.execute(
        "CREATE OR REPLACE VIEW m_workspace_visible_files_v1"
        + options_sql
        + " AS "
        + definition
    )


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0283-Repair-linked-Zulip-direct-participant-identities-6b50ae.py"
        ]

    @property
    def migration_id(self):
        return "59b5ff88-7a35-4b55-a131-1b2ddb23285a"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        # Provider links record synchronization; Workspace membership owns access.
        # Replacing the view in place preserves its identity and dependent grants.
        _replace_view(session, WORKSPACE_ACL)

    def downgrade(self, session):
        _replace_view(
            session,
            f"""
            SELECT visible.* FROM ({WORKSPACE_ACL}) visible
            JOIN m_workspace_files file ON file.uuid=visible.uuid
            WHERE file.source_name<>'zulip'
               OR (file.provider_identity IS NULL AND file.user_uuid=visible.viewer_user_uuid)
               OR file.acl_mode='public'
               OR EXISTS (
                   SELECT 1 FROM zulip_entity_links link
                   JOIN m_external_accounts_v2 account
                     ON account.uuid=link.external_account_uuid
                   WHERE link.project_id=file.project_id AND link.entity_type='file'
                     AND link.entity_uuid=file.uuid AND link.provider_uuid=file.provider_uuid
                     AND account.provider_uuid=link.provider_uuid
                     AND account.owner_user_uuid=visible.viewer_user_uuid
                     AND link.data->>'account_identity'=account.external_account_id
               )
        """,
        )


migration_step = MigrationStep()
