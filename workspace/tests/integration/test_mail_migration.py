# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util

from workspace.tests.integration import conftest


def _load_migration(pattern):
    path = next(conftest.MIGRATIONS_DIR.glob(pattern))
    spec = importlib.util.spec_from_file_location("mail_api_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration.migration_step


def test_mail_schema_migration_round_trip(db):
    migration = _load_migration("0309-Add-canonical-Mail-API-*.py")
    successor = _load_migration("0310-Add-personal-Calendar-*.py")
    groupware = _load_migration("0311-Store-groupware-credentials-*.py")
    groupware.downgrade(db)
    successor.downgrade(db)
    migration.downgrade(db)
    try:
        assert db.execute(
            """
            SELECT to_regclass('m_mail_folders'),
                   to_regclass('m_mail_messages'),
                   to_regclass('mail_outgoing_intents'),
                   to_regclass('groupware_mailbox_progress'),
                   to_regclass('groupware_calendar_event_bindings')
            """
        ).fetchone() == (None, None, None, None, None)
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
        successor.upgrade(db)
        groupware.upgrade(db)

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
    assert "calendar_event" in constraint
    assert db.execute(
        "SELECT to_regclass('m_workspace_calendars'), to_regclass('m_workspace_calendar_events')"
    ).fetchone() == ("m_workspace_calendars", "m_workspace_calendar_events")
    assert db.execute(
        "SELECT to_regprocedure('mail_outgoing_wakeup()')"
    ).fetchone() == ("mail_outgoing_wakeup()",)
