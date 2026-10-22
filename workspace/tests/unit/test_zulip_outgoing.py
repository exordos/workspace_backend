# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
from unittest import mock

import pytest
import requests

from workspace.bridges.zulip import client


@pytest.mark.parametrize("error", [None, "absent", "forbidden", "server"])
def test_delete_is_idempotent_only_for_an_absent_message(error):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    response = requests.Response()
    response.status_code = 200 if error is None else 500 if error == "server" else 400
    response._content = json.dumps(
        {"result": "success"}
        if error is None
        else {
            "result": "error",
            "code": "BAD_REQUEST",
            "msg": "Invalid message(s)"
            if error == "absent"
            else "You don't have permission to delete this message",
        }
    ).encode()
    session = mock.Mock()
    session.request.return_value = response
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            if error in ("forbidden", "server"):
                with pytest.raises(requests.HTTPError):
                    zulip.delete_message(123)
            else:
                zulip.delete_message(123)
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "DELETE", "https://zulip.example.com/api/v1/messages/123", timeout=(5, 30)
    )
    session.close.assert_called_once()


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


def test_attachment_upload_uses_the_authenticated_api():
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {
        "result": "success",
        "url": "/user_uploads/1/file/report.txt",
    }
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            assert (
                zulip.upload_file("report.txt", b"report", "text/plain")
                == "/user_uploads/1/file/report.txt"
            )
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "POST",
        "https://zulip.example.com/api/v1/user_uploads",
        timeout=(5, 30),
        files={"filename": ("report.txt", b"report", "text/plain")},
    )


def test_attachment_download_does_not_forward_credentials_to_object_storage():
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    authenticated, anonymous = mock.MagicMock(), mock.MagicMock()
    authenticated.request.return_value.json.return_value = {
        "result": "success",
        "url": "https://storage.example.com/signed-file",
    }
    anonymous.__enter__.return_value = anonymous
    anonymous.get.return_value.content = b"report"
    anonymous.get.return_value.headers = {"Content-Type": "text/plain; charset=utf-8"}
    with mock.patch.object(
        client.requests, "Session", side_effect=[authenticated, anonymous]
    ):
        zulip = client.ZulipClient(account)
        try:
            assert zulip.download_file("/user_uploads/1/file/report.txt") == (
                b"report",
                "text/plain",
            )
        finally:
            zulip.close()
    authenticated.request.assert_called_once_with(
        "GET",
        "https://zulip.example.com/api/v1/user_uploads/1/file/report.txt",
        timeout=(5, 30),
        params=None,
    )
    anonymous.get.assert_called_once_with(
        "https://storage.example.com/signed-file", timeout=(5, 30)
    )
    assert "auth" not in anonymous.__dict__
    assert anonymous.trust_env is False


@pytest.mark.parametrize("policy", [0, 1, 2, 3])
@pytest.mark.parametrize("topic", ["General", ""])
def test_topic_notification_update_uses_the_personal_zulip_endpoint(policy, topic):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success"}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            zulip.update_topic_notifications(42, topic, policy)
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "POST",
        "https://zulip.example.com/api/v1/user_topics",
        timeout=(5, 30),
        data={"stream_id": 42, "topic": topic, "visibility_policy": policy},
    )
    session.close.assert_called_once()


@pytest.mark.parametrize("is_muted", [False, True])
def test_stream_notification_update_uses_the_personal_subscription(is_muted):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success"}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            zulip.update_stream_notifications(42, is_muted)
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "POST",
        "https://zulip.example.com/api/v1/users/me/subscriptions/properties",
        timeout=(5, 30),
        data={
            "subscription_data": json.dumps(
                [{"stream_id": 42, "property": "is_muted", "value": is_muted}]
            )
        },
    )
