#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import unittest.mock as mock

from workspace.common.clients import zulip


class FakeZulipSdkClient:
    init_values = None
    stream_filters = None
    subscriber_call = None
    subscriber_response = None
    sent_message = None
    updated_message = None
    deleted_message_id = None
    reaction_calls = []
    message_call = None
    uploaded_file = None
    registered_queue = None
    register_calls = []
    register_responses = None
    event_call = None

    def __init__(self, email, api_key, site):
        self.init_values = {
            "email": email,
            "api_key": api_key,
            "site": site,
        }
        type(self).init_values = self.init_values

    def get_profile(self):
        return {"result": "success", "user_id": 42}

    def get_members(self):
        return {
            "result": "success",
            "members": [
                {
                    "user_id": 42,
                    "email": "user@example.com",
                    "delivery_email": "user@example.com",
                    "full_name": "User Example",
                },
            ],
        }

    def get_messages(self, message_filters):
        return {
            "result": "success",
            "messages": [
                {
                    "id": 100,
                    "content": "hello",
                    "filters": message_filters,
                },
            ],
        }

    def get_streams(self, **stream_filters):
        type(self).stream_filters = stream_filters
        return {
            "result": "success",
            "streams": [
                {
                    "stream_id": 3,
                    "name": "general",
                    "description": "General stream",
                    "creator_id": 24,
                    "date_created": 1776940760,
                    "invite_only": False,
                    "is_archived": False,
                    "is_announcement_only": False,
                },
            ],
        }

    def call_endpoint(
        self,
        url,
        method,
        request=None,
        timeout=None,
        **kwargs,
    ):
        call = {
            "url": url,
            "method": method,
            "request": request,
            "timeout": timeout,
        }
        if url == "messages":
            type(self).message_call = call
            return {
                "result": "success",
                "messages": [
                    {
                        "id": 100,
                        "content": "hello",
                        "filters": request,
                    },
                ],
            }
        if url == "messages/404":
            type(self).message_call = call
            return {
                "result": "error",
                "code": "BAD_REQUEST",
                "msg": "Invalid message(s)",
            }
        if url.startswith("messages/") and "reactions" not in url:
            type(self).message_call = call
            return {
                "result": "success",
                "message": {
                    "id": 100,
                    "content": "hello",
                },
            }
        if url == "events":
            type(self).event_call = call
            return {
                "result": "success",
                "events": [],
            }
        type(self).subscriber_call = call
        if "reactions" in url:
            type(self).reaction_calls.append(call)
            return {"result": "success"}
        return type(self).subscriber_response

    def register(self, event_types, **kwargs):
        call = {
            "event_types": event_types,
            "kwargs": kwargs,
        }
        type(self).registered_queue = call
        type(self).register_calls.append(call)
        if type(self).register_responses is not None:
            return type(self).register_responses.pop(0)
        return {
            "result": "success",
            "queue_id": "queue-1",
            "last_event_id": 42,
        }

    def send_message(self, request):
        type(self).sent_message = request
        return {
            "result": "success",
            "id": 12345,
        }

    def update_message(self, request):
        type(self).updated_message = request
        return {"result": "success"}

    def delete_message(self, message_id):
        type(self).deleted_message_id = message_id
        return {"result": "success"}

    def upload_file(self, file):
        type(self).uploaded_file = {
            "name": file.name,
            "data": file.read(),
        }
        return {
            "result": "success",
            "uri": "/user_uploads/1/report.pdf",
        }


def test_zulip_client_gets_profile_with_api_key():
    response = mock.Mock()
    response.json.return_value = {"result": "success", "user_id": 42}
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        data = client.get_current_user_with_api_key(
            login="user@example.com",
            token="zulip-token",
        )

    assert data == {"result": "success", "user_id": 42}
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/users/me",
        auth=("user@example.com", "zulip-token"),
        params=None,
        timeout=zulip.USER_FETCH_TIMEOUT,
    )


def test_zulip_client_gets_members_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "members": [
            {
                "user_id": 42,
                "email": "user@example.com",
                "delivery_email": "user@example.com",
                "full_name": "User Example",
            },
        ],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        members = client.get_users_with_api_key(
            login="user@example.com",
            token="zulip-token",
        )

    assert members == [
        {
            "user_id": 42,
            "email": "user@example.com",
            "delivery_email": "user@example.com",
            "full_name": "User Example",
        },
    ]
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/users",
        auth=("user@example.com", "zulip-token"),
        params=None,
        timeout=zulip.USER_FETCH_TIMEOUT,
    )


def test_zulip_client_gets_messages_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "messages": [
            {
                "id": 100,
                "content": "hello",
            },
        ],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )
    message_filters = {
        "anchor": "newest",
        "num_before": 10,
        "num_after": 0,
    }

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        messages = client.get_messages_with_api_key(
            login="user@example.com",
            token="zulip-token",
            message_filters=message_filters,
        )

    assert messages == [
        {
            "id": 100,
            "content": "hello",
        },
    ]
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/messages",
        auth=("user@example.com", "zulip-token"),
        params=message_filters,
        timeout=zulip.MESSAGE_FETCH_TIMEOUT,
    )


def test_zulip_client_gets_single_message_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "message": {
            "id": 100,
            "content": "hello",
        },
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        message = client.get_message_with_api_key(
            login="user@example.com",
            token="zulip-token",
            message_id=100,
        )

    assert message == {
        "id": 100,
        "content": "hello",
    }
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/messages/100",
        auth=("user@example.com", "zulip-token"),
        params={
            "apply_markdown": "false",
            "allow_empty_topic_name": "true",
        },
        timeout=zulip.MESSAGE_FETCH_TIMEOUT,
    )


def test_zulip_client_returns_none_for_invalid_single_message():
    response = mock.Mock()
    response.json.return_value = {
        "result": "error",
        "code": "BAD_REQUEST",
        "msg": "Invalid message(s)",
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        message = client.get_message_with_api_key(
            login="user@example.com",
            token="zulip-token",
            message_id=404,
        )

    assert message is None
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/messages/404",
        auth=("user@example.com", "zulip-token"),
        params={
            "apply_markdown": "false",
            "allow_empty_topic_name": "true",
        },
        timeout=zulip.MESSAGE_FETCH_TIMEOUT,
    )


def test_zulip_client_registers_message_event_queue_with_update_and_delete():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "post",
        return_value=response,
    ) as request:
        result = client.register_message_event_queue_with_api_key(
            login="user@example.com",
            token="zulip-token",
        )

    assert result == {
        "result": "success",
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/register",
        auth=("user@example.com", "zulip-token"),
        data={
            "event_types": (
                '["message", "reaction", "update_message", '
                '"delete_message", "stream", "subscription"]'
            ),
            "apply_markdown": "false",
            "client_capabilities": (
                "{"
                '"archived_channels": true, '
                '"notification_settings_null": true, '
                '"bulk_message_deletion": true'
                "}"
            ),
        },
        timeout=zulip.MESSAGE_EVENT_REGISTER_TIMEOUT,
    )


def test_zulip_client_registers_all_events_when_filtered_queue_fails():
    filtered_response = mock.Mock()
    filtered_response.json.return_value = {
        "result": "error",
        "msg": "Unknown event type delete_message",
    }
    fallback_response = mock.Mock()
    fallback_response.json.return_value = {
        "result": "success",
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "post",
        side_effect=[filtered_response, fallback_response],
    ) as request:
        result = client.register_message_event_queue_with_api_key(
            login="user@example.com",
            token="zulip-token",
        )

    assert result == {
        "result": "success",
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    assert request.call_args_list == [
        mock.call(
            "https://zulip.example.com/api/v1/register",
            auth=("user@example.com", "zulip-token"),
            data={
                "event_types": (
                    '["message", "reaction", "update_message", '
                    '"delete_message", "stream", "subscription"]'
                ),
                "apply_markdown": "false",
                "client_capabilities": (
                    "{"
                    '"archived_channels": true, '
                    '"notification_settings_null": true, '
                    '"bulk_message_deletion": true'
                    "}"
                ),
            },
            timeout=zulip.MESSAGE_EVENT_REGISTER_TIMEOUT,
        ),
        mock.call(
            "https://zulip.example.com/api/v1/register",
            auth=("user@example.com", "zulip-token"),
            data={
                "apply_markdown": "false",
                "client_capabilities": (
                    "{"
                    '"archived_channels": true, '
                    '"notification_settings_null": true, '
                    '"bulk_message_deletion": true'
                    "}"
                ),
            },
            timeout=zulip.MESSAGE_EVENT_REGISTER_TIMEOUT,
        ),
    ]


def test_zulip_client_gets_events_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "events": [],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        result = client.get_events_with_api_key(
            login="user@example.com",
            token="zulip-token",
            queue_id="queue-1",
            last_event_id=42,
        )

    assert result == {
        "result": "success",
        "events": [],
    }
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/events",
        auth=("user@example.com", "zulip-token"),
        params={
            "queue_id": "queue-1",
            "last_event_id": 42,
        },
        timeout=zulip.MESSAGE_EVENT_LONGPOLL_TIMEOUT,
    )


def test_zulip_client_sends_message_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.send_message_with_api_key(
        login="user@example.com",
        token="zulip-token",
        stream_name="general",
        topic_name="deploys",
        content="hello",
    )

    assert result == {
        "result": "success",
        "id": 12345,
    }
    assert FakeZulipSdkClient.sent_message == {
        "type": "stream",
        "to": "general",
        "topic": "deploys",
        "content": "hello",
    }


def test_zulip_client_sends_private_message_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.send_private_message_with_api_key(
        login="user@example.com",
        token="zulip-token",
        recipient_ids=[42],
        content="hello",
    )

    assert result == {
        "result": "success",
        "id": 12345,
    }
    assert FakeZulipSdkClient.sent_message == {
        "type": "direct",
        "to": [42],
        "content": "hello",
    }


def test_zulip_client_adds_reaction_with_official_sdk():
    FakeZulipSdkClient.reaction_calls = []
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.add_reaction_with_api_key(
        login="user@example.com",
        token="zulip-token",
        message_id=12345,
        emoji_name="thumbs_up",
    )

    assert result == {"result": "success"}
    assert FakeZulipSdkClient.reaction_calls == [
        {
            "url": "messages/12345/reactions",
            "method": "POST",
            "request": {
                "emoji_name": "thumbs_up",
                "emoji_code": None,
                "reaction_type": None,
            },
            "timeout": None,
        },
    ]


def test_zulip_client_removes_reaction_with_official_sdk():
    FakeZulipSdkClient.reaction_calls = []
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.remove_reaction_with_api_key(
        login="user@example.com",
        token="zulip-token",
        message_id=12345,
        emoji_name="thumbs_up",
        emoji_code="1f44d",
        reaction_type="unicode_emoji",
    )

    assert result == {"result": "success"}
    assert FakeZulipSdkClient.reaction_calls == [
        {
            "url": "messages/12345/reactions",
            "method": "DELETE",
            "request": {
                "emoji_name": "thumbs_up",
                "emoji_code": "1f44d",
                "reaction_type": "unicode_emoji",
            },
            "timeout": None,
        },
    ]


def test_zulip_client_updates_message_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.update_message_with_api_key(
        login="user@example.com",
        token="zulip-token",
        message_id=12345,
        content="edited",
    )

    assert result == {"result": "success"}
    assert FakeZulipSdkClient.updated_message == {
        "message_id": 12345,
        "content": "edited",
    }


def test_zulip_client_deletes_message_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.delete_message_with_api_key(
        login="user@example.com",
        token="zulip-token",
        message_id=12345,
    )

    assert result == {"result": "success"}
    assert FakeZulipSdkClient.deleted_message_id == 12345


def test_zulip_client_uploads_file_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    result = client.upload_file_with_api_key(
        login="user@example.com",
        token="zulip-token",
        file_name="report.pdf",
        data=b"pdf-data",
    )

    assert result == {
        "result": "success",
        "uri": "/user_uploads/1/report.pdf",
    }
    assert FakeZulipSdkClient.uploaded_file == {
        "name": "report.pdf",
        "data": b"pdf-data",
    }


def test_zulip_client_downloads_file_with_api_key():
    response = mock.Mock()
    response.content = b"file-data"
    response.headers = {"Content-Type": "image/png"}
    client = zulip.ZulipClient(endpoint="https://zulip.example.com")

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        result = client.download_file_with_api_key(
            login="user@example.com",
            token="zulip-token",
            url="/user_uploads/1/photo.png",
        )

    assert result == {
        "content": b"file-data",
        "content_type": "image/png",
    }
    request.assert_called_once_with(
        "https://zulip.example.com/user_uploads/1/photo.png",
        auth=("user@example.com", "zulip-token"),
        timeout=5,
    )
    response.raise_for_status.assert_called_once_with()


def test_zulip_client_gets_streams_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "streams": [
            {
                "stream_id": 3,
                "name": "general",
                "description": "General stream",
                "creator_id": 24,
                "date_created": 1776940760,
                "invite_only": False,
                "is_archived": False,
                "is_announcement_only": False,
            },
        ],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        streams = client.get_streams_with_api_key(
            login="user@example.com",
            token="zulip-token",
        )

    assert streams == [
        {
            "stream_id": 3,
            "name": "general",
            "description": "General stream",
            "creator_id": 24,
            "date_created": 1776940760,
            "invite_only": False,
            "is_archived": False,
            "is_announcement_only": False,
        },
    ]
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/streams",
        auth=("user@example.com", "zulip-token"),
        params={
            "include_all": "true",
            "exclude_archived": "false",
        },
        timeout=zulip.STREAM_FETCH_TIMEOUT,
    )


def test_zulip_client_gets_stream_subscribers_with_api_key():
    response = mock.Mock()
    response.json.return_value = {
        "result": "success",
        "subscribers": [10, 24],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    with mock.patch.object(
        zulip.requests,
        "get",
        return_value=response,
    ) as request:
        subscriber_ids = client.get_stream_subscribers_with_api_key(
            login="user@example.com",
            token="zulip-token",
            stream_id=3,
        )

    assert subscriber_ids == [10, 24]
    request.assert_called_once_with(
        "https://zulip.example.com/api/v1/streams/3/members",
        auth=("user@example.com", "zulip-token"),
        params=None,
        timeout=zulip.STREAM_MEMBERS_FETCH_TIMEOUT,
    )
