"""Integration coverage for the durable sticker cleanup queue migration."""

import uuid as sys_uuid

import pytest
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.tests.integration import conftest


MIGRATION_FILE = "0185-Add-sticker-storage-cleanup-queue-c67142.py"
MIGRATION_ID = "c67142e8-3c76-45c5-9421-095d9f2f4729"


def test_cleanup_migration_grants_and_guarded_round_trip(_database, db):
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    sticker_uuid = sys_uuid.uuid4()
    engine.apply_migration(MIGRATION_FILE)
    try:
        grants = db.execute(
            """
            SELECT privilege_type
            FROM information_schema.role_table_grants
            WHERE table_name = 'messenger_sticker_cleanup_tasks'
              AND grantee = 'workspace'
            ORDER BY privilege_type
            """
        ).fetchall()
        assert grants == [("DELETE",), ("INSERT",), ("SELECT",), ("UPDATE",)]
        assert (
            db.execute(
                """
                SELECT privilege_type
                FROM information_schema.table_privileges
                WHERE table_name = 'messenger_sticker_cleanup_tasks'
                  AND grantee = 'PUBLIC'
                """
            ).fetchall()
            == []
        )

        db.execute(
            """
            INSERT INTO messenger_sticker_cleanup_tasks (
                sticker_uuid, storage_object_id
            ) VALUES (%s, %s)
            """,
            (sticker_uuid, f"stickers/{sticker_uuid}/media.png"),
        )
        with pytest.raises(RuntimeError, match="must complete"):
            engine.rollback_migration(MIGRATION_FILE)

        assert db.execute(
            "SELECT to_regclass('messenger_sticker_cleanup_tasks')"
        ).fetchone() == ("messenger_sticker_cleanup_tasks",)
        assert db.execute(
            "SELECT applied FROM ra_migrations WHERE uuid = %s",
            (MIGRATION_ID,),
        ).fetchone() == (True,)

        db.execute(
            """
            UPDATE messenger_sticker_cleanup_tasks
            SET status = 'completed'
            WHERE sticker_uuid = %s
            """,
            (sticker_uuid,),
        )
        engine.rollback_migration(MIGRATION_FILE)

        assert db.execute(
            "SELECT to_regclass('messenger_sticker_cleanup_tasks')"
        ).fetchone() == (None,)
        assert db.execute(
            "SELECT applied FROM ra_migrations WHERE uuid = %s",
            (MIGRATION_ID,),
        ).fetchone() == (False,)
    finally:
        table = db.execute(
            "SELECT to_regclass('messenger_sticker_cleanup_tasks')"
        ).fetchone()[0]
        if table is not None:
            db.execute(
                "DELETE FROM messenger_sticker_cleanup_tasks WHERE sticker_uuid = %s",
                (sticker_uuid,),
            )
        engine.apply_migration(MIGRATION_FILE)
