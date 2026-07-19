# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import types

from workspace.services.messenger_workers import agents


def test_worker_prunes_postgresql_events_in_owned_session(monkeypatch):
    now = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)
    session = types.SimpleNamespace()
    calls = []
    monkeypatch.setattr(
        agents.sql_canonical_store,
        "prune_expired_events",
        lambda target_session, target_now: (
            calls.append((target_session, target_now)) or 9
        ),
    )

    worker = agents.MessengerWorkerAgent()

    assert worker._prune_expired_events(session, now) == 9
    assert calls == [(session, now)]
