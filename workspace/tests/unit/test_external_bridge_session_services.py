# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types
import uuid as sys_uuid

from workspace.external_bridge_control import sql_state


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


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
            _Result(),
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
    refreshed = []
    monkeypatch.setattr(
        repository,
        "_reconcile_observed_report",
        lambda current_session, current_identity, current_report: reconciled.append(
            (current_session, current_identity, current_report)
        ),
    )
    monkeypatch.setattr(
        sql_state,
        "refresh_effective_capabilities",
        lambda current_session, **kwargs: refreshed.append((current_session, kwargs)),
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
    assert refreshed == [(session, {"provider_kind": "zulip"})]
