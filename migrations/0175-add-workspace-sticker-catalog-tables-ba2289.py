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


UPGRADE_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE m_workspace_stickers (
    uuid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    alt_text TEXT NOT NULL,
    emoji TEXT[] NOT NULL,
    tags TEXT[] NOT NULL,
    search_text TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'sticker',
    format TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    size_bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    media_object_id TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    blocked BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT m_workspace_stickers_category_check
        CHECK (category IN ('gif', 'sticker')),
    CONSTRAINT m_workspace_stickers_format_check
        CHECK (format IN ('gif', 'webp', 'png')),
    CONSTRAINT m_workspace_stickers_width_check
        CHECK (width IS NULL OR width > 0),
    CONSTRAINT m_workspace_stickers_height_check
        CHECK (height IS NULL OR height > 0),
    CONSTRAINT m_workspace_stickers_size_bytes_check
        CHECK (size_bytes > 0),
    CONSTRAINT m_workspace_stickers_active_blocked_check
        CHECK (NOT (active AND blocked))
);

CREATE TABLE m_workspace_sticker_favorites (
    user_uuid UUID NOT NULL,
    sticker_uuid UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_uuid, sticker_uuid),
    CONSTRAINT m_workspace_sticker_favorites_sticker_fkey
        FOREIGN KEY (sticker_uuid)
        REFERENCES m_workspace_stickers (uuid)
        ON DELETE CASCADE
);

CREATE INDEX m_workspace_stickers_tags_gin_idx
    ON m_workspace_stickers USING GIN (tags);

CREATE INDEX m_workspace_stickers_search_text_trgm_idx
    ON m_workspace_stickers USING GIN (search_text gin_trgm_ops);

CREATE INDEX m_workspace_sticker_favorites_user_created_idx
    ON m_workspace_sticker_favorites (user_uuid, created_at DESC, sticker_uuid);

REVOKE ALL ON TABLE
    m_workspace_stickers,
    m_workspace_sticker_favorites
FROM PUBLIC;

DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workspace') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
            'm_workspace_stickers, m_workspace_sticker_favorites '
            'TO workspace';
    END IF;
END
$migration$;
"""


DOWNGRADE_SQL = """
DO $migration$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workspace') THEN
        EXECUTE 'REVOKE ALL ON TABLE '
            'm_workspace_stickers, m_workspace_sticker_favorites '
            'FROM workspace';
    END IF;
END
$migration$;

DROP TABLE m_workspace_sticker_favorites;
DROP TABLE m_workspace_stickers;
"""


class MigrationStep(migrations.AbstractMigrationStep):
    def __init__(self):
        self._depends = ["0174-suppress-legacy-backfill-counters-a2cd99.py"]

    @property
    def migration_id(self):
        return "ba2289b6-0a23-470e-a143-a5b986287601"

    @property
    def is_manual(self):
        return False

    def upgrade(self, session):
        session.execute(UPGRADE_SQL)

    def downgrade(self, session):
        session.execute(DOWNGRADE_SQL)


migration_step = MigrationStep()
