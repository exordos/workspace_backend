# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import importlib.util

import psycopg

from workspace.tests.integration import conftest


def test_shared_snapshot_invalidation_migration_restores_previous_guard(_database):
    with conftest.isolated_migration_database(
        "0251-Add-Zulip-history-snapshot-fingerprints-fc9065.py"
    ) as url:
        path = next(conftest.MIGRATIONS_DIR.glob("0252-Scope-shared-history-*.py"))
        spec = importlib.util.spec_from_file_location("shared_snapshot_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        query = "SELECT pg_get_functiondef('bridge_zulip_v5_invalidate_history(uuid,bigint,text[])'::regprocedure)"
        with psycopg.connect(url, autocommit=True) as connection:
            before = connection.execute(query).fetchone()[0]
            migration.migration_step.upgrade(connection)
            assert (
                "COALESCE(current_setting('workspace.zulip_history_batch',true),'')<>''"
                in connection.execute(query).fetchone()[0]
            )
            migration.migration_step.downgrade(connection)
            assert connection.execute(query).fetchone()[0] == before
            migration.migration_step.upgrade(connection)


def test_hash_migration_round_trip_handles_recreated_flags_and_archived_tasks(
    _database,
):
    with conftest.isolated_migration_database(
        "0250-Index-unread-Messenger-mentions-by-viewer-40a61a.py"
    ) as url:
        path = next(conftest.MIGRATIONS_DIR.glob("0251-*.py"))
        spec = importlib.util.spec_from_file_location(
            "history_fingerprint_migration", path
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with psycopg.connect(url, autocommit=True) as connection:
            migration.migration_step.upgrade(connection)
            connection.execute(
                "DROP TRIGGER bridge_zulip_v5_track_history_source ON bridge_zulip_v5_message_flags"
            )
            connection.execute("""INSERT INTO bridge_zulip_v5_tasks(uuid,content,status)
                VALUES (gen_random_uuid(),'{"kind":"scan_history","first_message_id":1,"last_message_id":3000}', 'done'),
                       (gen_random_uuid(),'{"kind":"import_history","first_message_id":1,"last_message_id":100,"snapshot":[],"batch_first_id":1,"batch_revision":1}', 'new')""")
            migration.migration_step.downgrade(connection)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_message_fingerprints')"
            ).fetchone() == (None,)
            rows = connection.execute(
                "SELECT content FROM bridge_zulip_v5_tasks WHERE content->>'kind' IN ('full_backfill','import_history')"
            ).fetchall()
            assert any(
                row[0]
                == {"kind": "full_backfill", "last_message_id": 3000, "since": None}
                for row in rows
            )
            assert all("snapshot" not in row[0] for row in rows)
            migration.migration_step.upgrade(connection)
            assert connection.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgname='bridge_zulip_v5_track_history_source' AND NOT tgisinternal"
            ).fetchone() == (6,)
