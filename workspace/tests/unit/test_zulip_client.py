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

from workspace.common.clients import zulip


class FakeZulipSdkClient:
    init_values = None
    stream_filters = None
    subscriber_call = None
    subscriber_response = None
    sent_message = None
    updated_message = None
    deleted_message_id = None

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

    def call_endpoint(self, url, method):
        type(self).subscriber_call = {
            "url": url,
            "method": method,
        }
        return type(self).subscriber_response

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


def test_zulip_client_uses_official_sdk_for_profile():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    data = client.get_current_user_with_api_key(
        login="user@example.com",
        token="zulip-token",
    )

    assert data == {"result": "success", "user_id": 42}
    assert FakeZulipSdkClient.init_values == {
        "email": "user@example.com",
        "api_key": "zulip-token",
        "site": "https://zulip.example.com",
    }


def test_zulip_client_gets_members_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

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


def test_zulip_client_gets_messages_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )
    message_filters = {
        "anchor": "newest",
        "num_before": 10,
        "num_after": 0,
    }

    messages = client.get_messages_with_api_key(
        login="user@example.com",
        token="zulip-token",
        message_filters=message_filters,
    )

    assert messages == [
        {
            "id": 100,
            "content": "hello",
            "filters": message_filters,
        },
    ]


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


def test_zulip_client_gets_streams_with_official_sdk():
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

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
    assert FakeZulipSdkClient.stream_filters == {
        "include_all": True,
        "exclude_archived": False,
    }


def test_zulip_client_gets_stream_subscribers_with_official_sdk():
    FakeZulipSdkClient.subscriber_response = {
        "result": "success",
        "subscribers": [10, 24],
    }
    client = zulip.ZulipClient(
        endpoint="https://zulip.example.com",
        client_cls=FakeZulipSdkClient,
    )

    subscriber_ids = client.get_stream_subscribers_with_api_key(
        login="user@example.com",
        token="zulip-token",
        stream_id=3,
    )

    assert subscriber_ids == [10, 24]
    assert FakeZulipSdkClient.subscriber_call == {
        "url": "streams/3/members",
        "method": "GET",
    }
