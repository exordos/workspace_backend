"""Backoff diagnostics are safe owner status fields with valid UTC dates."""

import datetime
import types
import uuid
from unittest import mock

from workspace.messenger_api import telegram_accounts


def test_status_serializes_aware_retry_deadlines_without_two_timezone_suffixes():
    instant = datetime.datetime(
        2026, 1, 2, 12, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))
    )
    state = {
        "auth_state": "authorized",
        "generation": uuid.uuid4(),
        "error_code": None,
        "catalog_error_code": None,
        "history_retry_at": instant,
        "catalog_retry_at": None,
        "common_retry_at": None,
    }
    session = mock.Mock()
    session.execute.return_value.fetchall.return_value = [
        {"peer_key": "user:8", "history_retry_at": instant}
    ]
    result = telegram_accounts.snapshot(
        session, types.SimpleNamespace(uuid=uuid.uuid4()), state, "unused"
    )
    assert result["retry_at"]["history_retry_at"] == "2026-01-02T09:00:00Z"
    assert result["dialogs"][0]["history_retry_at"] == "2026-01-02T09:00:00Z"
    assert "session_envelope" not in result
