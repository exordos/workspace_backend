# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import datetime
import types
import uuid as sys_uuid

from workspace.external_bridge_control import identity_linking
from workspace.external_bridge_control import sql_state


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def test_observed_report_batch_reuses_request_session_atomically(monkeypatch):
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
    )
    reports = [
        {"report_uuid": str(sys_uuid.uuid4())},
        {"report_uuid": str(sys_uuid.uuid4())},
    ]

    session = object()
    repository = sql_state.SQLControlState(sys_uuid.uuid4(), b"k" * 32)
    reconciled_sessions = []

    def reconcile(current_session, _identity, current_reports):
        reconciled_sessions.append(current_session)
        return {
            "results": [
                {
                    "report_uuid": report["report_uuid"],
                    "status": "applied",
                    "safe_error": None,
                }
                for report in current_reports
            ]
        }

    monkeypatch.setattr(repository, "reconcile_observed_reports", reconcile)

    result = repository.observed_reports(identity, reports, session=session)

    assert [item["report_uuid"] for item in result["results"]] == [
        report["report_uuid"] for report in reports
    ]
    assert reconciled_sessions == [session]


def test_observed_report_batch_without_explicit_session_uses_current_session(
    monkeypatch,
):
    identity = types.SimpleNamespace(
        bridge_instance_uuid=sys_uuid.uuid4(),
        provider_kind="zulip",
    )
    reports = [
        {"report_uuid": str(sys_uuid.uuid4())},
        {"report_uuid": str(sys_uuid.uuid4())},
    ]
    session = object()
    repository = sql_state.SQLControlState(sys_uuid.uuid4(), b"k" * 32)
    reconciled = []

    def reconcile(current_session, _identity, current_reports):
        reconciled.append((current_session, current_reports))
        return {
            "results": [
                {
                    "report_uuid": report["report_uuid"],
                    "status": "applied",
                    "safe_error": None,
                }
                for report in current_reports
            ]
        }

    monkeypatch.setattr(repository, "reconcile_observed_reports", reconcile)
    monkeypatch.setattr(
        repository, "_current_session", lambda: contextlib.nullcontext(session)
    )

    result = repository.observed_reports(identity, reports)

    assert [item["report_uuid"] for item in result["results"]] == [
        report["report_uuid"] for report in reports
    ]
    assert reconciled == [(session, reports)]


def test_observed_report_reconciliation_reuses_the_caller_session(monkeypatch):
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
    )
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "report_uuid": str(sys_uuid.uuid4()),
        "resource_type": "external_chat_catalog",
        "resource_uuid": str(chat_uuid),
        "observed_generation": 1,
        "status": "ready",
        "progress": {
            "phase": "catalog",
            "completed": 1,
            "total": 1,
            "last_progress_at": observed_at,
        },
        "safe_error": None,
        "observed_at": observed_at,
        "catalog": {"external_account_uuid": str(account_uuid)},
    }
    responses = iter(
        (
            _Result(None),
            _Result({"operation": "upsert", "generation": 1}),
            _Result({"generation": None}),
            _Result({"canonical_sha256": "inserted"}),
        )
    )

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))
            return next(responses)

    session = Session()

    repository = sql_state.SQLControlState(sys_uuid.uuid4(), b"k" * 32)
    reconciled = []
    monkeypatch.setattr(
        repository,
        "_reconcile_observed_report",
        lambda current_session, current_identity, current_report: reconciled.append(
            (current_session, current_identity, current_report)
        ),
    )

    result = repository.reconcile_observed_reports(session, identity, [report])

    assert result == {
        "results": [
            {
                "report_uuid": report["report_uuid"],
                "status": "applied",
                "safe_error": None,
            }
        ]
    }
    assert reconciled == [(session, identity, report)]
    assert not any(
        "m_external_accounts_v2" in statement for statement, _params in session.calls
    )


def test_pending_identity_reconciliation_keeps_report_retryable(monkeypatch):
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
    )
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "report_uuid": str(sys_uuid.uuid4()),
        "resource_type": "external_chat_catalog",
        "resource_uuid": str(chat_uuid),
        "observed_generation": 1,
        "status": "ready",
        "progress": {
            "phase": "catalog",
            "completed": 1,
            "total": 1,
            "last_progress_at": observed_at,
        },
        "safe_error": None,
        "observed_at": observed_at,
        "catalog": {"external_account_uuid": str(account_uuid)},
    }

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((statement, params))
            if 'SELECT "canonical_sha256"' in statement:
                return _Result()
            if 'SELECT "operation", "generation"' in statement:
                return _Result({"operation": "upsert", "generation": 1})
            if "SELECT MAX" in statement:
                return _Result({"generation": None})
            if "INSERT INTO" in statement:
                return _Result({"canonical_sha256": "inserted"})
            return _Result()

    session = Session()
    repository = sql_state.SQLControlState(sys_uuid.uuid4(), b"k" * 32)

    def reconciliation_pending(*_args):
        raise identity_linking.IdentityMergePending

    monkeypatch.setattr(
        repository,
        "_reconcile_observed_report",
        reconciliation_pending,
    )
    result = repository.reconcile_observed_reports(session, identity, [report])

    assert result == {
        "results": [
            {
                "report_uuid": report["report_uuid"],
                "status": "rejected",
                "safe_error": {
                    "code": "identity_reconciliation_in_progress",
                    "message": (
                        "Legacy provider identity reconciliation is still in progress"
                    ),
                    "retryable": True,
                },
            }
        ]
    }
    assert any(
        "DELETE FROM m_external_bridge_observed_reports_v1" in statement
        for statement, _params in session.calls
    )


def test_concurrent_observed_report_retry_is_idempotent(monkeypatch):
    bridge_uuid = sys_uuid.uuid4()
    report_uuid = sys_uuid.uuid4()
    resource_uuid = sys_uuid.uuid4()
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
    )
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "report_uuid": str(report_uuid),
        "resource_type": "external_account",
        "resource_uuid": str(resource_uuid),
        "observed_generation": 1,
        "status": "ready",
        "progress": None,
        "safe_error": None,
        "observed_at": observed_at,
    }

    class Session:
        def __init__(self):
            self.calls = []
            self.canonical_selects = 0

        def execute(self, statement, params):
            self.calls.append((statement, params))
            if 'SELECT "canonical_sha256"' in statement:
                self.canonical_selects += 1
                if self.canonical_selects == 1:
                    return _Result()
                canonical = (
                    __import__("hashlib")
                    .sha256(sql_state._json(report).encode())
                    .hexdigest()
                )
                return _Result({"canonical_sha256": canonical})
            if 'SELECT "operation", "generation"' in statement:
                return _Result({"operation": "upsert", "generation": 1})
            if "SELECT MAX" in statement:
                return _Result({"generation": None})
            if "INSERT INTO" in statement:
                return _Result()
            raise AssertionError(statement)

    session = Session()
    repository = sql_state.SQLControlState(sys_uuid.uuid4(), b"k" * 32)
    monkeypatch.setattr(
        repository,
        "_reconcile_observed_report",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("duplicate report must not be reconciled")
        ),
    )

    result = repository.reconcile_observed_reports(session, identity, [report])

    assert result == {
        "results": [
            {
                "report_uuid": str(report_uuid),
                "status": "duplicate",
                "safe_error": None,
            }
        ]
    }
    assert any("ON CONFLICT" in statement for statement, _params in session.calls)
