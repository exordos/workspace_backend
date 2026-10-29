# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import pathlib

from restalchemy.storage.sql import migrations


def test_sdk_push_migration_follows_current_master_without_rebase_joins():
    path = pathlib.Path(__file__).resolve().parents[3] / "migrations"
    engine = migrations.MigrationEngine(migrations_path=str(path))

    head = engine.get_latest_migration()
    steps = engine.get_all_migrations()
    assert head.startswith("0287-Use-SDK-events-for-native-mobile-push-")
    assert steps[head]["depends"] == [
        "0286-Replace-per-message-history-intents-with-bulk-import-batches-16952d.py"
    ]
    assert not any(
        "Join-" in name and ("mobile-push" in name or "native-mobile" in name)
        for name in steps
    )
    assert not any("Add-encrypted-mobile-push-outbox" in name for name in steps)
