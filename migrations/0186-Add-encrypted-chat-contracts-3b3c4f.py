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


UPGRADE_SQL = r"""
ALTER TABLE messenger_streams
    ADD COLUMN IF NOT EXISTS encryption boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS current_encryption_key_uuid uuid,
    ADD COLUMN IF NOT EXISTS current_encryption_public_key varchar(40000);
DO $constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messenger_streams_encryption_key_pair_check'
          AND conrelid = 'messenger_streams'::regclass
    ) THEN
        ALTER TABLE messenger_streams
        ADD CONSTRAINT messenger_streams_encryption_key_pair_check CHECK (
        (current_encryption_key_uuid IS NULL) =
        (current_encryption_public_key IS NULL)
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messenger_streams_encryption_key_required_check'
          AND conrelid = 'messenger_streams'::regclass
    ) THEN
        ALTER TABLE messenger_streams
        ADD CONSTRAINT messenger_streams_encryption_key_required_check CHECK (
        NOT encryption OR current_encryption_key_uuid IS NOT NULL
        );
    END IF;
END;
$constraint$;

ALTER TABLE m_workspace_streams
    ADD COLUMN IF NOT EXISTS encryption boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS current_encryption_key_uuid uuid,
    ADD COLUMN IF NOT EXISTS current_encryption_public_key varchar(40000);
DO $constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm_workspace_streams_encryption_key_pair_check'
          AND conrelid = 'm_workspace_streams'::regclass
    ) THEN
        ALTER TABLE m_workspace_streams
        ADD CONSTRAINT m_workspace_streams_encryption_key_pair_check CHECK (
        (current_encryption_key_uuid IS NULL) =
        (current_encryption_public_key IS NULL)
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'm_workspace_streams_encryption_key_required_check'
          AND conrelid = 'm_workspace_streams'::regclass
    ) THEN
        ALTER TABLE m_workspace_streams
        ADD CONSTRAINT m_workspace_streams_encryption_key_required_check CHECK (
        NOT encryption OR current_encryption_key_uuid IS NOT NULL
        );
    END IF;
END;
$constraint$;

CREATE OR REPLACE FUNCTION messenger_v2_mirror_stream_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_streams WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        DELETE FROM m_workspace_streams WHERE uuid = NEW.uuid;
        RETURN NEW;
    END IF;
    INSERT INTO m_workspace_streams (
        uuid, project_id, name, description, source_name, source, user_uuid,
        created_at, updated_at, invite_only, announce, private,
        encryption, current_encryption_key_uuid,
        current_encryption_public_key,
        direct_user_uuid, private_index, is_archived, color,
        default_topic_uuid, provider_metadata, delivery_metadata
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description,
        NEW.source_name, NEW.source, NEW.owner_uuid, NEW.created_at,
        NEW.updated_at, NEW.invite_only, NEW.announce, NEW.private,
        NEW.encryption, NEW.current_encryption_key_uuid,
        NEW.current_encryption_public_key,
        NEW.direct_user_uuid, NEW.private_index, NEW.is_archived, NEW.color,
        NEW.default_topic_uuid, NEW.provider, NEW.delivery
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        user_uuid = EXCLUDED.user_uuid,
        updated_at = EXCLUDED.updated_at,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        private = EXCLUDED.private,
        encryption = EXCLUDED.encryption,
        current_encryption_key_uuid = EXCLUDED.current_encryption_key_uuid,
        current_encryption_public_key =
            EXCLUDED.current_encryption_public_key,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private_index = EXCLUDED.private_index,
        is_archived = EXCLUDED.is_archived,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider_metadata = EXCLUDED.provider_metadata,
        delivery_metadata = EXCLUDED.delivery_metadata;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_stream()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_streams
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            gen_random_uuid(), OLD.project_id, 'delivery_snapshot_event',
            'resource',
            OLD.project_id::text || ':stream:' || OLD.uuid::text,
            jsonb_build_object(
                'source_kind', 'stream.deleted',
                'stream_uuid', OLD.uuid,
                'source_name', OLD.source_name,
                'source', OLD.source,
                'all_recipients', true,
                'private', OLD.private,
                'emit_public_event', false
            )
        );
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        PERFORM messenger_v2_move_canonical_stream_project(
            NEW.uuid, OLD.project_id, NEW.project_id
        );
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    PERFORM messenger_v2_register_project_user(
        NEW.project_id, NEW.direct_user_uuid
    );
    INSERT INTO messenger_streams (
        uuid, project_id, name, description, owner_uuid, source_name, source,
        invite_only, announce, direct_user_uuid, private, encryption,
        current_encryption_key_uuid, current_encryption_public_key,
        is_archived, private_index, color, default_topic_uuid,
        provider, delivery, created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description, NEW.user_uuid,
        NEW.source_name, NEW.source, NEW.invite_only, NEW.announce,
        NEW.direct_user_uuid, NEW.private, NEW.encryption,
        NEW.current_encryption_key_uuid, NEW.current_encryption_public_key,
        NEW.is_archived, NEW.private_index, NEW.color,
        CASE WHEN EXISTS (
            SELECT 1 FROM messenger_topics
            WHERE project_id = NEW.project_id
              AND uuid = NEW.default_topic_uuid
        ) THEN NEW.default_topic_uuid ELSE NULL END,
        NEW.provider_metadata,
        NEW.delivery_metadata, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        owner_uuid = EXCLUDED.owner_uuid,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private = EXCLUDED.private,
        encryption = EXCLUDED.encryption,
        current_encryption_key_uuid = EXCLUDED.current_encryption_key_uuid,
        current_encryption_public_key =
            EXCLUDED.current_encryption_public_key,
        is_archived = EXCLUDED.is_archived,
        private_index = EXCLUDED.private_index,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), NEW.project_id, 'delivery_snapshot_event',
        'resource', NEW.project_id::text || ':stream:' || NEW.uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'stream.created'
                                ELSE 'stream.updated' END,
            'resource_kind', 'stream',
            'resource_uuid', NEW.uuid,
            'stream_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;

"""


DOWNGRADE_SQL = r"""
CREATE OR REPLACE FUNCTION messenger_v2_mirror_stream_to_legacy()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        DELETE FROM m_workspace_streams WHERE uuid = OLD.uuid;
        RETURN OLD;
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        DELETE FROM m_workspace_streams WHERE uuid = NEW.uuid;
        RETURN NEW;
    END IF;
    INSERT INTO m_workspace_streams (
        uuid, project_id, name, description, source_name, source, user_uuid,
        created_at, updated_at, invite_only, announce, private,
        direct_user_uuid, private_index, is_archived, color,
        default_topic_uuid, provider_metadata, delivery_metadata
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description,
        NEW.source_name, NEW.source, NEW.owner_uuid, NEW.created_at,
        NEW.updated_at, NEW.invite_only, NEW.announce, NEW.private,
        NEW.direct_user_uuid, NEW.private_index, NEW.is_archived, NEW.color,
        NEW.default_topic_uuid, NEW.provider, NEW.delivery
    )
    ON CONFLICT (uuid) DO UPDATE SET
        project_id = EXCLUDED.project_id,
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        user_uuid = EXCLUDED.user_uuid,
        updated_at = EXCLUDED.updated_at,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        private = EXCLUDED.private,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private_index = EXCLUDED.private_index,
        is_archived = EXCLUDED.is_archived,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider_metadata = EXCLUDED.provider_metadata,
        delivery_metadata = EXCLUDED.delivery_metadata;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION messenger_v2_import_legacy_stream()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'DELETE' THEN
        UPDATE messenger_streams
        SET deleted_at = NOW()
        WHERE project_id = OLD.project_id AND uuid = OLD.uuid;
        INSERT INTO messenger_domain_outbox_events (
            uuid, project_id, event_kind, scope_kind, scope_key, payload
        ) VALUES (
            gen_random_uuid(), OLD.project_id, 'delivery_snapshot_event',
            'resource',
            OLD.project_id::text || ':stream:' || OLD.uuid::text,
            jsonb_build_object(
                'source_kind', 'stream.deleted',
                'stream_uuid', OLD.uuid,
                'source_name', OLD.source_name,
                'source', OLD.source,
                'all_recipients', true,
                'private', OLD.private,
                'emit_public_event', false
            )
        );
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.project_id IS DISTINCT FROM NEW.project_id THEN
        PERFORM messenger_v2_move_canonical_stream_project(
            NEW.uuid, OLD.project_id, NEW.project_id
        );
    END IF;
    PERFORM messenger_v2_register_project_user(NEW.project_id, NEW.user_uuid);
    PERFORM messenger_v2_register_project_user(
        NEW.project_id, NEW.direct_user_uuid
    );
    INSERT INTO messenger_streams (
        uuid, project_id, name, description, owner_uuid, source_name, source,
        invite_only, announce, direct_user_uuid, private, is_archived,
        private_index, color, default_topic_uuid, provider, delivery,
        created_at, updated_at
    ) VALUES (
        NEW.uuid, NEW.project_id, NEW.name, NEW.description, NEW.user_uuid,
        NEW.source_name, NEW.source, NEW.invite_only, NEW.announce,
        NEW.direct_user_uuid, NEW.private, NEW.is_archived, NEW.private_index,
        NEW.color,
        CASE WHEN EXISTS (
            SELECT 1 FROM messenger_topics
            WHERE project_id = NEW.project_id
              AND uuid = NEW.default_topic_uuid
        ) THEN NEW.default_topic_uuid ELSE NULL END,
        NEW.provider_metadata,
        NEW.delivery_metadata, NEW.created_at, NEW.updated_at
    )
    ON CONFLICT (project_id, uuid) DO UPDATE SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        owner_uuid = EXCLUDED.owner_uuid,
        source_name = EXCLUDED.source_name,
        source = EXCLUDED.source,
        invite_only = EXCLUDED.invite_only,
        announce = EXCLUDED.announce,
        direct_user_uuid = EXCLUDED.direct_user_uuid,
        private = EXCLUDED.private,
        is_archived = EXCLUDED.is_archived,
        private_index = EXCLUDED.private_index,
        color = EXCLUDED.color,
        default_topic_uuid = EXCLUDED.default_topic_uuid,
        provider = EXCLUDED.provider,
        delivery = EXCLUDED.delivery,
        deleted_at = NULL,
        updated_at = EXCLUDED.updated_at;
    INSERT INTO messenger_domain_outbox_events (
        uuid, project_id, event_kind, scope_kind, scope_key, payload
    ) VALUES (
        gen_random_uuid(), NEW.project_id, 'delivery_snapshot_event',
        'resource', NEW.project_id::text || ':stream:' || NEW.uuid::text,
        jsonb_build_object(
            'source_kind', CASE WHEN TG_OP = 'INSERT'
                                THEN 'stream.created'
                                ELSE 'stream.updated' END,
            'resource_kind', 'stream',
            'resource_uuid', NEW.uuid,
            'stream_uuid', NEW.uuid,
            'emit_public_event', false
        )
    );
    RETURN NEW;
END;
$$;

ALTER TABLE messenger_streams
    DROP CONSTRAINT messenger_streams_encryption_key_required_check,
    DROP CONSTRAINT messenger_streams_encryption_key_pair_check,
    DROP COLUMN current_encryption_public_key,
    DROP COLUMN current_encryption_key_uuid,
    DROP COLUMN encryption;

ALTER TABLE m_workspace_streams
    DROP CONSTRAINT m_workspace_streams_encryption_key_required_check,
    DROP CONSTRAINT m_workspace_streams_encryption_key_pair_check,
    DROP COLUMN current_encryption_public_key,
    DROP COLUMN current_encryption_key_uuid,
    DROP COLUMN encryption;

"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = [
            "0185-Repair-per-user-operation-delivery-projections-e1f5ca.py"
        ]

    @property
    def migration_id(self):
        return "3b3c4f96-bcba-4f1e-b9ce-83aaa4e6ddb2"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
