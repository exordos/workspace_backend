# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
from types import SimpleNamespace
from unittest import mock
import uuid as sys_uuid

from workspace.external_bridge_control import sql_state


def test_heartbeat_retention_is_bounded_and_uses_cutoff():
    now = datetime.datetime(2026, 7, 27, tzinfo=datetime.timezone.utc)
    retention = datetime.timedelta(hours=24)
    calls = []

    class Result:
        @staticmethod
        def fetchone():
            return {"count": 23}

    session = SimpleNamespace(
        execute=lambda statement, params: calls.append((statement, params)) or Result()
    )

    assert (
        sql_state.prune_expired_heartbeats(
            session,
            now,
            retention=retention,
            batch_size=100,
        )
        == 23
    )
    statement, params = calls[0]
    assert 'DELETE FROM "m_external_bridge_heartbeats_v1"' in statement
    assert "ORDER BY" in statement
    assert "LIMIT %s" in statement
    assert params == (now - retention, 100)


def test_projected_capability_events_do_not_fan_out_to_message_history(
    monkeypatch,
):
    stream = object()
    topic = object()
    stream_get_all = mock.Mock(return_value=[stream])
    topic_get_all = mock.Mock(return_value=[topic])
    message_get_all = mock.Mock(
        side_effect=AssertionError("historical messages must not be loaded")
    )
    stream_updated = mock.Mock()
    topic_updated = mock.Mock()
    message_updated = mock.Mock()
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserStream",
        SimpleNamespace(objects=SimpleNamespace(get_all=stream_get_all)),
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserTopic",
        SimpleNamespace(objects=SimpleNamespace(get_all=topic_get_all)),
    )
    monkeypatch.setattr(
        sql_state.models,
        "WorkspaceUserMessage",
        SimpleNamespace(objects=SimpleNamespace(get_all=message_get_all)),
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_stream_updated_event",
        stream_updated,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_topic_updated_event",
        topic_updated,
    )
    monkeypatch.setattr(
        sql_state.messenger_events,
        "create_message_updated_event",
        message_updated,
    )
    session = object()

    sql_state._emit_projected_capability_events(
        session,
        {
            "project_id": sys_uuid.uuid4(),
            "projection_stream_uuid": sys_uuid.uuid4(),
        },
    )

    stream_updated.assert_called_once_with(stream, session=session)
    topic_updated.assert_called_once_with(topic, session=session)
    message_get_all.assert_not_called()
    message_updated.assert_not_called()
