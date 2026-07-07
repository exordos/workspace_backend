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

import datetime
import queue
from types import SimpleNamespace

from workspace.services.integration_bridge import workers


class FakeZulipClient:
    init_endpoint = None
    calls = None
    pages = None
    stream_calls = None
    streams = None
    subscriber_calls = None
    subscribers = None

    def __init__(self, endpoint):
        type(self).init_endpoint = endpoint

    def get_messages_with_api_key(self, login, token, message_filters):
        type(self).calls.append({
            "login": login,
            "token": token,
            "message_filters": dict(message_filters),
        })
        return type(self).pages.pop(0)

    def get_streams_with_api_key(self, login, token):
        type(self).stream_calls.append({
            "login": login,
            "token": token,
        })
        return type(self).streams

    def get_stream_subscribers_with_api_key(
        self,
        login,
        token,
        stream_id,
    ):
        type(self).subscriber_calls.append({
            "login": login,
            "token": token,
            "stream_id": stream_id,
        })
        return type(self).subscribers[stream_id]


def _external_account():
    return SimpleNamespace(
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            credentials=SimpleNamespace(
                login="user@example.com",
                token="zulip-token",
            ),
            user_info=SimpleNamespace(user_id=10),
        ),
    )


def test_zulip_bridge_worker_fetches_messages():
    message_filters = {
        "anchor": "newest",
        "num_before": 10,
        "num_after": 0,
    }
    FakeZulipClient.calls = []
    FakeZulipClient.pages = [[{"id": 100, "content": "hello"}]]
    external_account = _external_account()
    sync_queue = object()
    worker = workers.ZulipBridgeWorker(
        external_account=external_account,
        sync_queue=sync_queue,
        client_cls=FakeZulipClient,
    )

    messages = worker.fetch_messages(message_filters=message_filters)

    assert messages == [
        {
            "id": 100,
            "content": "hello",
        },
    ]
    assert worker._external_account is external_account
    assert worker._sync_queue is sync_queue
    assert not hasattr(worker, "message_filters")
    assert FakeZulipClient.init_endpoint == "https://zulip.example.com"
    assert FakeZulipClient.calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_filters": {
                "anchor": "newest",
                "num_before": 10,
                "num_after": 0,
                "apply_markdown": False,
            },
        },
    ]


def test_zulip_bridge_worker_fetches_streams():
    FakeZulipClient.stream_calls = []
    FakeZulipClient.streams = [
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
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        sync_queue=object(),
        client_cls=FakeZulipClient,
    )

    streams = worker.fetch_streams()

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
    assert FakeZulipClient.stream_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
        },
    ]


def test_zulip_bridge_worker_fetches_stream_subscribers():
    FakeZulipClient.subscriber_calls = []
    FakeZulipClient.subscribers = {
        3: [10, 24],
    }
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        sync_queue=object(),
        client_cls=FakeZulipClient,
    )

    subscriber_ids = worker.fetch_stream_subscribers(
        stream={
            "stream_id": 3,
            "name": "general",
        },
    )

    assert subscriber_ids == [10, 24]
    assert FakeZulipClient.subscriber_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "stream_id": 3,
        },
    ]


def test_zulip_bridge_worker_run_fetches_all_messages_and_processes_them():
    events = []
    processed_messages = []
    processed_streams = []
    FakeZulipClient.calls = []
    FakeZulipClient.stream_calls = []
    FakeZulipClient.subscriber_calls = []
    FakeZulipClient.streams = [
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
    FakeZulipClient.subscribers = {
        3: [10, 24],
    }
    FakeZulipClient.pages = [
        [
            {
                "id": 100,
                "sender_id": 7,
                "content": "ignored",
            },
            {
                "id": 101,
                "sender_id": 8,
                "content": "oldest",
            },
        ],
        [
            {
                "id": 101,
                "sender_id": 8,
                "content": "oldest-duplicate",
            },
            {
                "id": 102,
                "sender_id": 9,
                "content": "middle",
            },
            {
                "id": 103,
                "sender_id": 6,
                "content": "ignored-new",
            },
        ],
        [],
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        sync_queue=queue.Queue(),
        client_cls=FakeZulipClient,
    )

    def process_stream(stream, subscriber_ids=None):
        processed_streams.append({
            "stream": stream,
            "subscriber_ids": subscriber_ids,
        })
        events.append("stream")

    def process_message(message):
        processed_messages.append(message)
        events.append("message")

    worker._process_stream = process_stream
    worker._process_message = process_message

    worker.run()

    assert processed_streams == [
        {
            "stream": {
                "stream_id": 3,
                "name": "general",
                "description": "General stream",
                "creator_id": 24,
                "date_created": 1776940760,
                "invite_only": False,
                "is_archived": False,
                "is_announcement_only": False,
            },
            "subscriber_ids": [10, 24],
        },
    ]
    assert processed_messages == [
        {
            "id": 101,
            "sender_id": 8,
            "content": "oldest",
        },
        {
            "id": 102,
            "sender_id": 9,
            "content": "middle",
        },
    ]
    assert events == ["stream", "message", "message"]
    assert not hasattr(worker, "messages")
    assert FakeZulipClient.stream_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
        },
    ]
    assert FakeZulipClient.subscriber_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "stream_id": 3,
        },
    ]
    assert FakeZulipClient.calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_filters": {
                "anchor": 0,
                "num_before": 0,
                "num_after": 100,
                "apply_markdown": False,
            },
        },
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_filters": {
                "anchor": 101,
                "num_before": 0,
                "num_after": 100,
                "apply_markdown": False,
            },
        },
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_filters": {
                "anchor": 103,
                "num_before": 0,
                "num_after": 100,
                "apply_markdown": False,
            },
        },
    ]


def test_zulip_bridge_worker_processes_message_with_sync_queue():
    sync_queue = queue.Queue()
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        sync_queue=sync_queue,
        client_cls=FakeZulipClient,
    )
    message = {"id": 100, "content": "hello"}

    worker._process_message(message)

    add_message = sync_queue.get_nowait()
    assert add_message.external_account is worker._external_account
    assert add_message.message == message


def test_zulip_bridge_worker_processes_stream_with_sync_queue():
    sync_queue = queue.Queue()
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        sync_queue=sync_queue,
        client_cls=FakeZulipClient,
    )
    stream = {
        "stream_id": 3,
        "name": "general",
        "description": "General stream",
        "creator_id": 24,
        "date_created": 1776940760,
        "invite_only": True,
        "is_archived": True,
        "is_announcement_only": True,
    }

    worker._process_stream(stream, subscriber_ids=[10, 24])

    add_stream = sync_queue.get_nowait()
    assert add_stream.external_account is worker._external_account
    assert add_stream.stream == stream
    assert add_stream.subscriber_ids == [10, 24]


def test_add_message_executes_with_cache():
    external_account = _external_account()
    stream = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append({
                "external_account": external_account,
                "stream_info": stream_info,
            })
            return stream

    cache = FakeCache()
    command = workers.AddMessage(
        external_account=external_account,
        message={
            "type": "stream",
            "stream_id": 3,
            "display_recipient": "general",
            "sender_id": 24,
            "timestamp": 1770998098,
        },
    )

    result = command.execute(cache=cache)

    assert result is stream
    assert cache.calls == [
        {
            "external_account": external_account,
            "stream_info": {
                "type": "stream",
                "stream_id": 3,
                "display_recipient": "general",
                "description": "",
                "creator_id": 24,
                "timestamp": datetime.datetime.fromtimestamp(
                    1770998098,
                    tz=datetime.timezone.utc,
                ),
                "invite_only": False,
                "announce": False,
                "is_archived": False,
                "subscriber_ids": [24],
            },
        },
    ]


def test_add_stream_executes_with_cache():
    external_account = _external_account()
    stream = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append({
                "external_account": external_account,
                "stream_info": stream_info,
            })
            return stream

    cache = FakeCache()
    command = workers.AddStream(
        external_account=external_account,
        subscriber_ids=[10, 24],
        stream={
            "stream_id": 3,
            "name": "general",
            "description": "General stream",
            "creator_id": 24,
            "date_created": 1776940760,
            "invite_only": True,
            "is_archived": True,
            "is_announcement_only": True,
        },
    )

    result = command.execute(cache=cache)

    assert result is stream
    assert cache.calls == [
        {
            "external_account": external_account,
            "stream_info": {
                "type": "stream",
                "stream_id": 3,
                "display_recipient": "general",
                "description": "General stream",
                "creator_id": 24,
                "created_at": datetime.datetime.fromtimestamp(
                    1776940760,
                    tz=datetime.timezone.utc,
                ),
                "invite_only": True,
                "announce": True,
                "is_archived": True,
                "subscriber_ids": [10, 24],
            },
        },
    ]


def test_add_stream_uses_current_user_id_without_creator_id():
    external_account = _external_account()
    stream = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append({
                "external_account": external_account,
                "stream_info": stream_info,
            })
            return stream

    cache = FakeCache()
    command = workers.AddStream(
        external_account=external_account,
        stream={
            "stream_id": 3,
            "name": "general",
            "description": "General stream",
            "creator_id": None,
            "date_created": 1776940760,
            "invite_only": True,
            "is_archived": True,
            "is_announcement_only": True,
        },
    )

    result = command.execute(cache=cache)

    assert result is stream
    assert cache.calls[0]["stream_info"]["creator_id"] == 10
