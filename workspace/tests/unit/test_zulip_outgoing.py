# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
from unittest import mock

import pytest

from workspace.bridges.zulip import client


@pytest.mark.parametrize("recipient,topic", [(42, "General"), (42, ""), ([7, 8], None)])
def test_send_preserves_content_and_marks_it_read_by_the_sender(recipient, topic):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success", "id": 123}
    content = "Unchanged **markdown** [user](urn:user:123)"
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            assert (
                zulip.send_message(recipient, content, topic, "queue", "local-id")
                == 123
            )
        finally:
            zulip.close()
    expected = {
        "type": "direct" if topic is None else "channel",
        "to": json.dumps(recipient),
        "content": content,
        "read_by_sender": "true",
        "queue_id": "queue",
        "local_id": "local-id",
    }
    if topic is not None:
        expected["topic"] = topic
    assert session.auth == ("sender@example.com", "sender-key")
    session.request.assert_called_once_with(
        "POST",
        "https://zulip.example.com/api/v1/messages",
        timeout=(5, 30),
        data=expected,
    )
    session.close.assert_called_once()


def test_edit_preserves_content_and_uses_the_remote_id():
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success"}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            zulip.edit_message(123, "Raw **edit**")
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "PATCH",
        "https://zulip.example.com/api/v1/messages/123",
        timeout=(5, 30),
        data={"content": "Raw **edit**"},
    )
