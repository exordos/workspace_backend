# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import datetime
import types
import uuid as sys_uuid

from workspace.services.messenger_workers import agents
from workspace.common import messenger_storage_opts


def test_worker_prunes_canonical_event_mailboxes_before_sql_event_rows(monkeypatch):
    now = datetime.datetime(2026, 7, 16, tzinfo=datetime.timezone.utc)
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    calls = []

    class EventRow:
        def __init__(self):
            self.project_id = project_uuid
            self.user_uuid = user_uuid

        def delete(self, session=None):
            calls.append(("sql_event_deleted", session))

    class Repository:
        def prune_events(self, target_user_uuid, now=None):
            calls.append(("imap_events_pruned", target_user_uuid, now))

    class Runtime:
        @contextlib.contextmanager
        def project_repository(self, target_project_uuid):
            calls.append(("project_repository", target_project_uuid))
            yield Repository()

    monkeypatch.setattr(
        type(agents.messenger_models.WorkspaceEvent.objects),
        "get_all",
        lambda self, **kwargs: [EventRow()],
    )
    worker = agents.MessengerWorkerAgent(runtime_factory=Runtime())
    session = types.SimpleNamespace()

    assert worker._prune_expired_events(session, now) == 1
    assert calls == [
        ("project_repository", project_uuid),
        ("imap_events_pruned", user_uuid, now),
        ("sql_event_deleted", session),
    ]


def test_canonical_worker_prunes_only_postgresql_in_owned_session(monkeypatch):
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
    worker = agents.MessengerWorkerAgent(
        runtime_factory=None,
        storage_mode=messenger_storage_opts.POSTGRESQL_CANONICAL,
    )

    assert worker._prune_expired_events(session, now) == 9
    assert calls == [(session, now)]
