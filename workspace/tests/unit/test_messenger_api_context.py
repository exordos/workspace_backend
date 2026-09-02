# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import threading
from unittest import mock

import pytest
from restalchemy.common import contexts as ra_contexts
from restalchemy.storage.sql import sessions as ra_sessions

from workspace.messenger_api.api import context as messenger_context


def test_schema_lock_failure_releases_stored_session(monkeypatch):
    storage = ra_sessions.SessionThreadStorage()
    first_session = mock.Mock(name="first_session")
    second_session = mock.Mock(name="second_session")
    engine = mock.Mock()
    engine.get_session.side_effect = [first_session, second_session]
    engine.get_session_storage.return_value = storage
    lock_error = RuntimeError("schema lock failed")

    monkeypatch.setattr(
        ra_sessions.SessionThreadStorage,
        "_storage",
        threading.local(),
    )
    monkeypatch.setattr(
        ra_contexts.engines.engine_factory,
        "get_engine",
        lambda name: engine,
    )
    monkeypatch.setattr(
        messenger_context.read_state,
        "lock_read_state_schema_shared",
        mock.Mock(side_effect=[lock_error, None]),
    )
    context = messenger_context.WorkspaceMessengerAuthContext(req=object())

    with pytest.raises(RuntimeError) as exc_info:
        context.start_new_session()

    assert exc_info.value is lock_error
    first_session.close.assert_called_once_with()
    with pytest.raises(ra_sessions.SessionNotFound):
        storage.get_session()

    session = context.start_new_session()

    assert session is second_session
    assert storage.get_session() is second_session
    context.session_close()
    second_session.close.assert_called_once_with()
    with pytest.raises(ra_sessions.SessionNotFound):
        storage.get_session()
