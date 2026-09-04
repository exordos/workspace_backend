"""Integration coverage for the shared sticker catalog schema migration."""

import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import conftest


MIGRATION_FILE = "0175-add-workspace-sticker-catalog-tables-ba2289.py"
MIGRATION_ID = "ba2289b6-0a23-470e-a143-a5b986287601"


@pytest.fixture(scope="module")
def _sticker_catalog_migration(_database):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(MIGRATION_FILE)
    try:
        yield engine
    finally:
        engine.rollback_migration(MIGRATION_FILE)
        engine.apply_migration(MIGRATION_FILE)


def test_sticker_catalog_migration_round_trip_schema_constraints_indexes_and_grants(
    _sticker_catalog_migration,
    db,
):
    engine = _sticker_catalog_migration
    assert engine.get_latest_migration() == MIGRATION_FILE
    assert engine._load_migrations()[MIGRATION_FILE].depends == [
        "0174-suppress-legacy-backfill-counters-a2cd99.py"
    ]
    engine.apply_migration(MIGRATION_FILE)
    engine.rollback_migration(MIGRATION_FILE)
    engine.apply_migration(MIGRATION_FILE)

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT applied FROM ra_migrations WHERE uuid = %s",
            (MIGRATION_ID,),
        )
        assert cursor.fetchone() == (True,)

        cursor.execute(
            """
            SELECT table_name, column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name IN (
                'm_workspace_stickers', 'm_workspace_sticker_favorites'
            )
            ORDER BY table_name, ordinal_position
            """
        )
        columns = cursor.fetchall()
        assert columns == [
            ("m_workspace_sticker_favorites", "user_uuid", "NO", None),
            ("m_workspace_sticker_favorites", "sticker_uuid", "NO", None),
            (
                "m_workspace_sticker_favorites",
                "created_at",
                "NO",
                "now()",
            ),
            ("m_workspace_stickers", "uuid", "NO", "gen_random_uuid()"),
            ("m_workspace_stickers", "title", "NO", None),
            ("m_workspace_stickers", "alt_text", "NO", None),
            ("m_workspace_stickers", "emoji", "NO", None),
            ("m_workspace_stickers", "tags", "NO", None),
            ("m_workspace_stickers", "search_text", "NO", None),
            (
                "m_workspace_stickers",
                "category",
                "NO",
                "'sticker'::text",
            ),
            ("m_workspace_stickers", "format", "NO", None),
            ("m_workspace_stickers", "width", "YES", None),
            ("m_workspace_stickers", "height", "YES", None),
            ("m_workspace_stickers", "size_bytes", "NO", None),
            ("m_workspace_stickers", "sha256", "NO", None),
            ("m_workspace_stickers", "media_object_id", "NO", None),
            ("m_workspace_stickers", "active", "NO", "true"),
            ("m_workspace_stickers", "blocked", "NO", "false"),
            (
                "m_workspace_stickers",
                "created_at",
                "NO",
                "now()",
            ),
            (
                "m_workspace_stickers",
                "updated_at",
                "NO",
                "now()",
            ),
        ]

        cursor.execute(
            """
            SELECT conname, contype, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid IN (
                'm_workspace_stickers'::regclass,
                'm_workspace_sticker_favorites'::regclass
            )
            ORDER BY conname
            """
        )
        constraints = cursor.fetchall()
        assert constraints == [
            (
                "m_workspace_sticker_favorites_pkey",
                "p",
                "PRIMARY KEY (user_uuid, sticker_uuid)",
            ),
            (
                "m_workspace_sticker_favorites_sticker_fkey",
                "f",
                (
                    "FOREIGN KEY (sticker_uuid) REFERENCES "
                    "m_workspace_stickers(uuid) ON DELETE CASCADE"
                ),
            ),
            (
                "m_workspace_stickers_active_blocked_check",
                "c",
                "CHECK ((NOT (active AND blocked)))",
            ),
            (
                "m_workspace_stickers_category_check",
                "c",
                "CHECK ((category = ANY (ARRAY['gif'::text, 'sticker'::text])))",
            ),
            (
                "m_workspace_stickers_format_check",
                "c",
                (
                    "CHECK ((format = ANY (ARRAY['gif'::text, 'webp'::text, "
                    "'png'::text])))"
                ),
            ),
            (
                "m_workspace_stickers_height_check",
                "c",
                "CHECK (((height IS NULL) OR (height > 0)))",
            ),
            (
                "m_workspace_stickers_pkey",
                "p",
                "PRIMARY KEY (uuid)",
            ),
            (
                "m_workspace_stickers_sha256_key",
                "u",
                "UNIQUE (sha256)",
            ),
            (
                "m_workspace_stickers_size_bytes_check",
                "c",
                "CHECK ((size_bytes > 0))",
            ),
            (
                "m_workspace_stickers_width_check",
                "c",
                "CHECK (((width IS NULL) OR (width > 0)))",
            ),
        ]

        cursor.execute(
            """
            SELECT indexrelid::regclass::text, am.amname,
                   array_agg(opc.opcname ORDER BY classes.ordinality)
            FROM pg_index
            JOIN pg_class idx ON idx.oid = indexrelid
            JOIN pg_am am ON am.oid = idx.relam
            CROSS JOIN LATERAL unnest(indclass) WITH ORDINALITY AS classes(
                oid, ordinality
            )
            JOIN pg_opclass opc ON opc.oid = classes.oid
            WHERE indrelid IN (
                'm_workspace_stickers'::regclass,
                'm_workspace_sticker_favorites'::regclass
            )
            GROUP BY indexrelid, am.amname
            ORDER BY indexrelid::regclass::text
            """
        )
        indexes = cursor.fetchall()
        assert {
            (name, access_method, tuple(opclasses))
            for name, access_method, opclasses in indexes
        } >= {
            (
                "m_workspace_stickers_tags_gin_idx",
                "gin",
                ("array_ops",),
            ),
            (
                "m_workspace_stickers_search_text_trgm_idx",
                "gin",
                ("gin_trgm_ops",),
            ),
            (
                "m_workspace_sticker_favorites_user_created_idx",
                "btree",
                ("uuid_ops", "timestamptz_ops", "uuid_ops"),
            ),
        }
        index_names = {row[0] for row in indexes}
        assert not any("category" in name or "emoji" in name for name in index_names)

        cursor.execute(
            """
            SELECT grantee, privilege_type
            FROM information_schema.role_table_grants
            WHERE table_name IN (
                'm_workspace_stickers', 'm_workspace_sticker_favorites'
            )
              AND grantee = %s
            ORDER BY grantee, table_name, privilege_type
            """,
            ("workspace",),
        )
        grants = cursor.fetchall()
        assert grants.count(("workspace", "DELETE")) == 2
        assert grants.count(("workspace", "INSERT")) == 2
        assert grants.count(("workspace", "SELECT")) == 2
        assert grants.count(("workspace", "UPDATE")) == 2

        sticker_uuid = sys_uuid.uuid4()
        cursor.execute(
            """
            INSERT INTO m_workspace_stickers (
                uuid, title, alt_text, emoji, tags, search_text, format,
                size_bytes, sha256, media_object_id
            ) VALUES (%s, 'x', 'x', '{}', '{}', 'x', 'png', 1, %s, 'x')
            """,
            (str(sticker_uuid), "a" * 64),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_sticker_favorites (user_uuid, sticker_uuid)
            VALUES (%s, %s)
            """,
            (str(sys_uuid.uuid4()), str(sticker_uuid)),
        )
        cursor.execute(
            "SELECT category, width, height, active, blocked "
            "FROM m_workspace_stickers WHERE uuid = %s",
            (str(sticker_uuid),),
        )
        assert cursor.fetchone() == ("sticker", None, None, True, False)

        with pytest.raises(psycopg.errors.UniqueViolation):
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers (
                    title, alt_text, emoji, tags, search_text, format,
                    size_bytes, sha256, media_object_id
                ) VALUES ('x', 'x', '{}', '{}', 'x', 'png', 1, %s, 'x')
                """,
                ("a" * 64,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers (
                    title, alt_text, emoji, tags, search_text, category,
                    format, size_bytes, sha256, media_object_id
                ) VALUES ('x', 'x', '{}', '{}', 'x', 'invalid', 'png', 1, %s, 'x')
                """,
                ("b" * 64,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers (
                    title, alt_text, emoji, tags, search_text, format,
                    size_bytes, sha256, media_object_id
                ) VALUES ('x', 'x', '{}', '{}', 'x', 'invalid', 1, %s, 'x')
                """,
                ("c" * 64,),
            )
        for column in ("width", "height", "size_bytes"):
            with pytest.raises(psycopg.errors.CheckViolation):
                cursor.execute(
                    f"UPDATE m_workspace_stickers SET {column} = 0 WHERE uuid = %s",
                    (str(sticker_uuid),),
                )
        with pytest.raises(psycopg.errors.CheckViolation):
            cursor.execute(
                """
                INSERT INTO m_workspace_stickers (
                    title, alt_text, emoji, tags, search_text, format,
                    size_bytes, sha256, media_object_id, active, blocked
                ) VALUES ('x', 'x', '{}', '{}', 'x', 'png', 1, %s, 'x', TRUE, TRUE)
                """,
                ("g" * 64,),
            )
        cursor.execute(
            "DELETE FROM m_workspace_stickers WHERE uuid = %s", (str(sticker_uuid),)
        )
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_sticker_favorites "
            "WHERE sticker_uuid = %s",
            (str(sticker_uuid),),
        )
        assert cursor.fetchone() == (0,)
