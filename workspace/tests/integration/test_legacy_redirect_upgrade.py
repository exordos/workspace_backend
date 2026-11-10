# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import psycopg
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


@pytest.mark.isolated_schema
def test_legacy_redirect_repair_preserves_the_project_during_physical_delete_upgrade():
    with conftest.isolated_migration_database(
        "0293-Bound-topic-summary-boundary-lookups-cebf12.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            project, user = uuid.uuid4(), uuid.uuid4()
            stream = conftest.seed_user_stream(connection, project, user, "Upgrade")
            target = conftest.seed_stream_topic(
                connection, project, stream, user, "Removed"
            )
            survivor = conftest.seed_stream_topic(
                connection, project, stream, user, "Kept"
            )
            connection.execute("""
                ALTER TABLE messenger_topics ADD COLUMN redirect_topic_uuid uuid;
                ALTER TABLE messenger_topics ADD CONSTRAINT messenger_topics_redirect_fkey
                    FOREIGN KEY(project_id,redirect_topic_uuid)
                    REFERENCES messenger_topics(project_id,uuid) ON DELETE SET NULL;
            """)
            connection.execute(
                "UPDATE messenger_topics SET redirect_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
                (target, project, survivor),
            )
            connection.execute(
                "UPDATE messenger_topics SET deleted_at=now() WHERE project_id=%s AND uuid=%s",
                (project, target),
            )
            repair = (
                conftest.REPO_ROOT / "tools/repair_legacy_topic_redirect.sql"
            ).read_text()
            connection.execute(repair)
            connection.execute(repair)
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(
                "0294-Delete-Messenger-roots-with-durable-snapshots-and-prepared-unreads-77d94b.py"
            )
            assert connection.execute(
                "SELECT project_id,redirect_topic_uuid FROM messenger_topics WHERE uuid=%s",
                (survivor,),
            ).fetchone() == (project, None)
            assert (
                connection.execute(
                    "SELECT 1 FROM messenger_topics WHERE project_id=%s AND uuid=%s",
                    (project, target),
                ).fetchone()
                is None
            )
            assert connection.execute(
                "SELECT project_id FROM messenger_streams WHERE uuid=%s", (stream,)
            ).fetchone() == (project,)
            engine.apply_migration(engine.get_latest_migration())


@pytest.mark.isolated_schema
def test_legacy_redirect_repair_does_not_extend_the_standard_schema():
    with conftest.isolated_migration_database(
        "0293-Bound-topic-summary-boundary-lookups-cebf12.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as connection:
            query = "SELECT column_name FROM information_schema.columns WHERE table_name='messenger_topics' ORDER BY ordinal_position"
            before = connection.execute(query).fetchall()
            repair = (
                conftest.REPO_ROOT / "tools/repair_legacy_topic_redirect.sql"
            ).read_text()
            connection.execute(repair)
            assert connection.execute(query).fetchall() == before
