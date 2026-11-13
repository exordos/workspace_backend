# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import shutil

import psycopg
from psycopg import rows
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


def test_provider_edit_time_migration_round_trip_preserves_previous_function(_database):
    with conftest.isolated_migration_database(
        "0303-Preserve-provider-edit-times-during-Zulip-conversion-8ecfb7.py"
    ) as url:
        path = next(
            conftest.MIGRATIONS_DIR.glob("0304-Distinguish-explicit-Zulip-content-edit-times-*.py")
        )
        spec = importlib.util.spec_from_file_location(
            "provider_edit_time_migration", path
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with psycopg.connect(
            url, autocommit=True, row_factory=rows.dict_row
        ) as connection:
            before = connection.execute(migration.FUNCTION_SQL).fetchone()["definition"]
            migration.migration_step.upgrade(connection)
            assert (
                migration.PROVIDER_GUARD
                in connection.execute(migration.FUNCTION_SQL).fetchone()["definition"]
            )
            migration.migration_step.downgrade(connection)
            assert (
                connection.execute(migration.FUNCTION_SQL).fetchone()["definition"]
                == before
            )
            migration.migration_step.upgrade(connection)


def test_upgrade_from_the_published_pre_rebase_migration_applies_poll_dependencies(
    _database, tmp_path
):
    legacy_name = "0301-Preserve-provider-edit-times-during-Zulip-conversion-8ecfb7.py"
    current_name = "0303-Preserve-provider-edit-times-during-Zulip-conversion-8ecfb7.py"
    with conftest.isolated_migration_database(
        "0300-Preserve-Zulip-history-during-subscription-recovery-82877f.py"
    ) as url:
        # The original PR published the same UUID before the poll migrations.
        for path in conftest.MIGRATIONS_DIR.glob("*.py"):
            if not path.name.startswith(("0301-", "0302-", "0303-", "0304-")):
                shutil.copyfile(path, tmp_path / path.name)
        (tmp_path / legacy_name).write_text(
            (conftest.MIGRATIONS_DIR / current_name)
            .read_text()
            .replace(
                "0302-Synchronize-compatible-Zulip-polls-40ec4a.py",
                "0300-Preserve-Zulip-history-during-subscription-recovery-82877f.py",
            )
        )
        migrations.MigrationEngine(migrations_path=str(tmp_path)).apply_migration(
            legacy_name
        )
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.apply_migration(engine.get_latest_migration())
        with psycopg.connect(url) as connection:
            assert connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_attribute "
                "WHERE attrelid='messenger_messages'::regclass "
                "AND attname='poll_state_revision' AND NOT attisdropped)"
            ).fetchone()[0]
            assert connection.execute(
                "SELECT to_regclass('zulip_poll_observations') IS NOT NULL"
            ).fetchone()[0]
            assert connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_trigger "
                "WHERE tgrelid='messenger_messages'::regclass "
                "AND tgname='messenger_supplied_content_time')"
            ).fetchone()[0]
