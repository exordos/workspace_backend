# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import urllib.parse

import psycopg
import psycopg.conninfo
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest


def test_isolated_database_preserves_empty_uri_authority(monkeypatch):
    parameters = psycopg.conninfo.conninfo_to_dict(conftest.TEST_DB_URL)
    database_name = parameters.pop("dbname")
    empty_authority_url = (
        "postgresql:///"
        + urllib.parse.quote(database_name, safe="")
        + "?"
        + urllib.parse.urlencode(parameters, quote_via=urllib.parse.quote)
    )
    monkeypatch.setattr(conftest, "TEST_DB_URL", empty_authority_url)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))

    with conftest.isolated_migration_database(engine.get_latest_migration()) as url:
        actual = psycopg.conninfo.conninfo_to_dict(url)
        isolated_name = actual.pop("dbname")
        assert actual == parameters
        assert isolated_name.startswith("workspace_migration_")
        assert isolated_name != database_name
        with psycopg.connect(url) as connection:
            assert connection.execute("SELECT current_database()").fetchone() == (
                isolated_name,
            )
