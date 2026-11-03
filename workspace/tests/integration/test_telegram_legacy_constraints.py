"""An upgraded database can retain a different PostgreSQL cast representation."""

import importlib.util
import pathlib
import types

import pytest
import psycopg


MIGRATION = (
    pathlib.Path(__file__).parents[3]
    / "migrations/0288-Add-Telegram-provider-accounts-and-durable-synchronization-b50483.py"
)


def test_populated_legacy_varchar_casts_keep_native_and_zulip_when_telegram_is_added(
    db, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "telegram_constraint_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db.execute(
        "CREATE TEMP TABLE cassi_telegram_legacy_constraint(source_name varchar(17), CONSTRAINT cassi_telegram_legacy_source_check CHECK(source_name::text=ANY(ARRAY[('native'::varchar)::text,('zulip'::varchar)::text])))"
    )
    db.execute(
        "INSERT INTO cassi_telegram_legacy_constraint VALUES('native'),('zulip')"
    )
    definition = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='cassi_telegram_legacy_constraint'::regclass"
    ).fetchone()[0]
    assert "('zulip'::character varying)::text" in definition

    class Session:
        def execute(self, query, parameters=None):
            if "FROM pg_constraint" in query:
                return types.SimpleNamespace(
                    fetchall=lambda: [
                        {
                            "table_name": "cassi_telegram_legacy_constraint",
                            "conname": "cassi_telegram_legacy_source_check",
                            "definition": definition,
                        }
                    ]
                )
            return db.execute(query, parameters)

    monkeypatch.setattr(module, "SCHEMA", "SELECT 1")
    monkeypatch.setattr(module, "CAPTURE", "SELECT 1")
    module.migration_step.upgrade(Session())
    db.execute("INSERT INTO cassi_telegram_legacy_constraint VALUES('telegram')")
    assert db.execute(
        "SELECT source_name FROM cassi_telegram_legacy_constraint ORDER BY source_name"
    ).fetchall() == [("native",), ("telegram",), ("zulip",)]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("INSERT INTO cassi_telegram_legacy_constraint VALUES('unsupported')")
