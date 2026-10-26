# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import pathlib

from restalchemy.storage.sql import migrations


def test_migrations_have_one_automatic_head():
    path = pathlib.Path(__file__).resolve().parents[3] / "migrations"
    engine = migrations.MigrationEngine(migrations_path=str(path))

    assert engine.get_latest_migration()
