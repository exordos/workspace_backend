# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import importlib.util
import pathlib

import pytest


MIGRATION_PATH = (
    pathlib.Path(__file__).parents[3]
    / "migrations"
    / "0185-Add-sticker-storage-cleanup-queue-c67142.py"
)


def _load_migration():
    specification = importlib.util.spec_from_file_location(
        "sticker_cleanup_migration",
        MIGRATION_PATH,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.migration_step


def _normalize_sql(statement):
    return " ".join(statement.split()).replace("' '", "")


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Session:
    def __init__(self, *, unfinished=False):
        self.unfinished = unfinished
        self.calls = []

    def execute(self, statement):
        self.calls.append(statement)
        if "SELECT EXISTS" in statement:
            return _Result({"unfinished": self.unfinished})
        return _Result()


def test_upgrade_revokes_public_and_grants_runtime_role():
    session = _Session()

    _load_migration().upgrade(session)

    assert len(session.calls) == 1
    statement = session.calls[0]
    normalized = _normalize_sql(statement)
    assert (
        "REVOKE ALL ON TABLE messenger_sticker_cleanup_tasks FROM PUBLIC" in statement
    )
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'workspace')" in statement
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "messenger_sticker_cleanup_tasks TO workspace" in normalized
    )


def test_downgrade_refuses_to_drop_unfinished_cleanup_tasks():
    session = _Session(unfinished=True)

    with pytest.raises(RuntimeError, match="must complete"):
        _load_migration().downgrade(session)

    assert "ACCESS EXCLUSIVE" in session.calls[0]
    assert "status IN ('pending', 'running', 'failed')" in session.calls[1]
    assert all("DROP TABLE" not in statement for statement in session.calls)


def test_downgrade_revokes_runtime_role_and_drops_drained_queue():
    session = _Session()

    _load_migration().downgrade(session)

    assert len(session.calls) == 3
    assert "ACCESS EXCLUSIVE" in session.calls[0]
    assert "SELECT EXISTS" in session.calls[1]
    assert (
        "REVOKE ALL ON TABLE messenger_sticker_cleanup_tasks FROM workspace"
        in _normalize_sql(session.calls[2])
    )
    assert "DROP TABLE messenger_sticker_cleanup_tasks" in session.calls[2]
