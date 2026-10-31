# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util

from workspace.tests.integration import conftest


def _load_mail_migration():
    path = next(conftest.MIGRATIONS_DIR.glob("0288-Add-canonical-Mail-API-*.py"))
    spec = importlib.util.spec_from_file_location("mail_api_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration.migration_step


def test_mail_schema_migration_round_trip(db):
    migration = _load_mail_migration()
    migration.downgrade(db)
    try:
        assert db.execute(
            """
            SELECT to_regclass('m_mail_folders'),
                   to_regclass('m_mail_messages'),
                   to_regclass('mail_outgoing_intents')
            """
        ).fetchone() == (None, None, None)
        constraint = db.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname = 'm_workspace_events_object_type_check'
            """
        ).fetchone()[0]
        assert "mail_folder" not in constraint
        assert "mail_message" not in constraint
        assert db.execute(
            "SELECT to_regprocedure('mail_outgoing_wakeup()')"
        ).fetchone() == (None,)
    finally:
        migration.upgrade(db)

    assert db.execute(
        """
        SELECT to_regclass('m_mail_folders'),
               to_regclass('m_mail_messages'),
               to_regclass('mail_outgoing_intents')
        """
    ).fetchone() == (
        "m_mail_folders",
        "m_mail_messages",
        "mail_outgoing_intents",
    )
    constraint = db.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conname = 'm_workspace_events_object_type_check'
        """
    ).fetchone()[0]
    assert "mail_folder" in constraint
    assert "mail_message" in constraint
    assert db.execute(
        "SELECT to_regprocedure('mail_outgoing_wakeup()')"
    ).fetchone() == ("mail_outgoing_wakeup()",)
