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
import threading
import types
import unittest.mock as mock
import uuid as sys_uuid

import pytest

from workspace.services.integration_bridge import workers


class FakeZulipClient:
    init_endpoint = None
    calls = None
    pages = None
    stream_calls = None
    streams = None
    subscriber_calls = None
    subscribers = None
    register_calls = None
    registered_queue = None
    event_calls = None
    event_pages = None
    send_calls = None
    send_private_calls = None
    upload_calls = None
    update_calls = None
    delete_calls = None
    add_reaction_calls = None
    remove_reaction_calls = None

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

    def register_message_event_queue_with_api_key(self, login, token):
        type(self).register_calls.append({
            "login": login,
            "token": token,
        })
        return type(self).registered_queue

    def get_events_with_api_key(self, login, token, queue_id, last_event_id):
        type(self).event_calls.append({
            "login": login,
            "token": token,
            "queue_id": queue_id,
            "last_event_id": last_event_id,
        })
        return type(self).event_pages.pop(0)

    def send_message_with_api_key(
        self,
        login,
        token,
        stream_name,
        topic_name,
        content,
    ):
        type(self).send_calls.append({
            "login": login,
            "token": token,
            "stream_name": stream_name,
            "topic_name": topic_name,
            "content": content,
        })
        return {"id": 12345}

    def send_private_message_with_api_key(
        self,
        login,
        token,
        recipient_ids,
        content,
    ):
        type(self).send_private_calls.append({
            "login": login,
            "token": token,
            "recipient_ids": recipient_ids,
            "content": content,
        })
        return {"id": 12345}

    def update_message_with_api_key(self, login, token, message_id, content):
        type(self).update_calls.append({
            "login": login,
            "token": token,
            "message_id": message_id,
            "content": content,
        })
        return {"result": "success"}

    def delete_message_with_api_key(self, login, token, message_id):
        type(self).delete_calls.append({
            "login": login,
            "token": token,
            "message_id": message_id,
        })
        return {"result": "success"}

    def add_reaction_with_api_key(
        self,
        login,
        token,
        message_id,
        emoji_name,
        emoji_code=None,
        reaction_type=None,
    ):
        type(self).add_reaction_calls.append({
            "login": login,
            "token": token,
            "message_id": message_id,
            "emoji_name": emoji_name,
            "emoji_code": emoji_code,
            "reaction_type": reaction_type,
        })
        return {"result": "success"}

    def remove_reaction_with_api_key(
        self,
        login,
        token,
        message_id,
        emoji_name,
        emoji_code=None,
        reaction_type=None,
    ):
        type(self).remove_reaction_calls.append({
            "login": login,
            "token": token,
            "message_id": message_id,
            "emoji_name": emoji_name,
            "emoji_code": emoji_code,
            "reaction_type": reaction_type,
        })
        return {"result": "success"}

    def upload_file_with_api_key(self, login, token, file_name, data):
        type(self).upload_calls.append({
            "login": login,
            "token": token,
            "file_name": file_name,
            "data": data,
        })
        return {"uri": f"/user_uploads/1/{file_name}"}


def _external_account():
    return types.SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="bridge-user",
        account_settings=types.SimpleNamespace(
            credentials=types.SimpleNamespace(
                login="user@example.com",
                token="zulip-token",
            ),
            user_info=types.SimpleNamespace(user_id=10),
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
    input_queue = object()
    output_queue = object()
    worker = workers.ZulipBridgeWorker(
        external_account=external_account,
        input_queue=input_queue,
        output_queue=output_queue,
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
    assert worker._input_queue is input_queue
    assert worker._output_queue is output_queue
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


def test_zulip_bridge_worker_registers_message_event_queue():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.register_calls = []
    FakeZulipClient.registered_queue = {
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    queue_id, last_event_id = worker.register_message_event_queue()

    assert queue_id == "queue-1"
    assert last_event_id == 42
    assert FakeZulipClient.register_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.UpdateZulipQueueState)
    assert command.queue_id == "queue-1"
    assert command.last_event_id == 42
    assert command.is_synced is False


def test_zulip_bridge_worker_reports_message_event_queue_register_error():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.register_calls = []
    FakeZulipClient.registered_queue = {
        "result": "error",
        "msg": "Unknown event type delete_message",
    }
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    with pytest.raises(RuntimeError) as exc_info:
        worker.register_message_event_queue()

    assert "Zulip message event queue registration failed" in str(
        exc_info.value,
    )
    assert "Unknown event type delete_message" in str(exc_info.value)
    assert output_queue.empty()


def test_zulip_bridge_worker_fetches_events():
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 43,
                    "type": "heartbeat",
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=queue.PriorityQueue(),
        client_cls=FakeZulipClient,
    )

    events = worker.fetch_events(
        queue_id="queue-1",
        last_event_id=42,
    )

    assert events == [
        {
            "id": 43,
            "type": "heartbeat",
        },
    ]
    assert FakeZulipClient.event_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "queue_id": "queue-1",
            "last_event_id": 42,
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
        input_queue=object(),
        output_queue=object(),
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
        input_queue=object(),
        output_queue=object(),
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


def test_zulip_bridge_worker_waits_for_commands_by_default():
    input_queue = queue.Queue()
    FakeZulipClient.calls = []
    FakeZulipClient.stream_calls = []
    FakeZulipClient.subscriber_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=input_queue,
        output_queue=queue.PriorityQueue(),
        client_cls=FakeZulipClient,
    )

    worker.start()
    input_queue.put(workers.StopWorker())
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert FakeZulipClient.calls == []
    assert FakeZulipClient.stream_calls == []
    assert FakeZulipClient.subscriber_calls == []


def test_zulip_bridge_worker_commands_sync_streams_and_messages():
    events = []
    processed_messages = []
    processed_streams = []
    output_queue = queue.PriorityQueue()
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
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    def process_stream(stream, subscriber_ids=None):
        processed_streams.append({
            "stream": stream,
            "subscriber_ids": subscriber_ids,
        })
        events.append("stream")

    def process_message(message, event_id=None):
        if not worker._should_process_message(message):
            return False
        processed_messages.append(message)
        events.append("message")
        return True

    worker._process_stream = process_stream
    worker._process_message = process_message

    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    workers.SyncStreams(
        event_owner=event_owner,
    ).execute(worker)
    last_message_id = worker._catch_up_messages(0)

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
    assert events == [
        "stream",
        "message",
        "message",
    ]
    assert last_message_id == 103
    response = output_queue.get_nowait()
    sync_finished = workers.get_sync_response_command(response)
    assert isinstance(sync_finished, workers.SyncStreamsFinished)
    assert sync_finished.event_owner == event_owner
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


def test_zulip_bridge_worker_uses_initial_message_anchor_for_catch_up():
    FakeZulipClient.calls = []
    FakeZulipClient.stream_calls = []
    FakeZulipClient.subscriber_calls = []
    FakeZulipClient.streams = []
    FakeZulipClient.subscribers = {}
    FakeZulipClient.pages = [[]]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=queue.PriorityQueue(),
        client_cls=FakeZulipClient,
    )

    worker._catch_up_messages(103)

    assert FakeZulipClient.calls == [
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


def test_zulip_bridge_worker_can_skip_stream_sync():
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
    FakeZulipClient.pages = [[]]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=queue.PriorityQueue(),
        client_cls=FakeZulipClient,
    )

    worker._catch_up_messages(103)

    assert FakeZulipClient.stream_calls == []
    assert FakeZulipClient.subscriber_calls == []
    assert FakeZulipClient.calls == [
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


def test_zulip_bridge_worker_syncs_message_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 43,
                    "type": "message",
                    "flags": ["read"],
                    "message": {
                        "id": 104,
                        "sender_id": 8,
                        "content": "hello",
                    },
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
    )

    assert last_event_id == 43
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.AddMessage)
    assert command.message["id"] == 104
    assert command.message["flags"] == ["read"]
    assert command.event_id == 43


def test_zulip_bridge_worker_syncs_message_update_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 44,
                    "type": "update_message",
                    "user_id": 8,
                    "message_id": 104,
                    "message_ids": [104],
                    "content": "edited",
                    "edit_timestamp": 1770998100,
                    "rendering_only": False,
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=43,
        last_message_id=103,
    )

    assert last_event_id == 44
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.UpdateMessage)
    assert command.event_id == 44
    assert command.last_message_id == 104
    assert command.event["content"] == "edited"


def test_zulip_bridge_worker_syncs_message_delete_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 45,
                    "type": "delete_message",
                    "message_type": "stream",
                    "stream_id": 3,
                    "topic": "deploys",
                    "message_ids": [104, 105],
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=44,
        last_message_id=103,
    )

    assert last_event_id == 45
    assert last_message_id == 105
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.DeleteMessage)
    assert command.event_id == 45
    assert command.last_message_id == 105
    assert command.message_ids == [104, 105]


def test_zulip_bridge_worker_syncs_single_message_delete_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 45,
                    "type": "delete_message",
                    "message_type": "stream",
                    "stream_id": 3,
                    "topic": "deploys",
                    "message_id": 104,
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=44,
        last_message_id=103,
    )

    assert last_event_id == 45
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.DeleteMessage)
    assert command.event_id == 45
    assert command.last_message_id == 104
    assert command.message_ids == [104]


def test_zulip_bridge_worker_syncs_reaction_add_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 46,
                    "type": "reaction",
                    "op": "add",
                    "message_id": 104,
                    "user_id": 24,
                    "emoji_name": "thumbs_up",
                    "emoji_code": "1f44d",
                    "reaction_type": "unicode_emoji",
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=45,
        last_message_id=103,
    )

    assert last_event_id == 46
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.AddMessageReaction)
    assert command.event_id == 46
    assert command.last_message_id == 104
    assert command.event["emoji_name"] == "thumbs_up"


def test_zulip_bridge_worker_syncs_reaction_remove_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 47,
                    "type": "reaction",
                    "op": "remove",
                    "message_id": 104,
                    "user_id": 24,
                    "emoji_name": "thumbs_up",
                    "emoji_code": "1f44d",
                    "reaction_type": "unicode_emoji",
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=46,
        last_message_id=103,
    )

    assert last_event_id == 47
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.RemoveMessageReaction)
    assert command.event_id == 47
    assert command.last_message_id == 104
    assert command.event["emoji_name"] == "thumbs_up"


def test_zulip_bridge_worker_skips_rendering_only_message_update_event():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "events": [
                {
                    "id": 44,
                    "type": "update_message",
                    "user_id": None,
                    "message_id": 104,
                    "message_ids": [104],
                    "rendering_only": True,
                },
            ],
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    last_event_id, last_message_id = worker._sync_message_events(
        queue_id="queue-1",
        last_event_id=43,
        last_message_id=103,
    )

    assert last_event_id == 44
    assert last_message_id == 104
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.UpdateZulipQueueState)
    assert command.last_event_id == 44
    assert command.last_message_id == 104


def test_zulip_bridge_worker_reports_dead_message_queue():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.calls = []
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "result": "error",
            "code": "BAD_EVENT_QUEUE_ID",
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.SyncMessages(
        queue_id="dead-queue",
        last_event_id=42,
        last_message_id=103,
        is_synced=False,
    ).execute(worker)

    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipQueueFailed)
    assert command.external_account is worker._external_account
    assert FakeZulipClient.calls == []


def test_zulip_bridge_worker_reports_message_queue_without_events_as_dead():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.calls = []
    FakeZulipClient.event_calls = []
    FakeZulipClient.event_pages = [
        {
            "result": "success",
            "msg": "",
        },
    ]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.SyncMessages(
        queue_id="dead-queue",
        last_event_id=42,
        last_message_id=103,
        is_synced=False,
    ).execute(worker)

    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipQueueFailed)
    assert command.external_account is worker._external_account
    assert FakeZulipClient.calls == []


def test_zulip_bridge_worker_creates_queue_before_catch_up():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.register_calls = []
    FakeZulipClient.registered_queue = {
        "queue_id": "queue-1",
        "last_event_id": 42,
    }
    FakeZulipClient.calls = []
    FakeZulipClient.pages = [[]]
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.CreateZulipQueueAndFetchMessages(
        last_message_id=103,
    ).execute(worker)

    first_response = output_queue.get_nowait()
    second_response = output_queue.get_nowait()
    first_command = workers.get_sync_response_command(first_response)
    second_command = workers.get_sync_response_command(second_response)
    assert isinstance(first_command, workers.UpdateZulipQueueState)
    assert first_command.queue_id == "queue-1"
    assert first_command.last_event_id == 42
    assert first_command.is_synced is False
    assert isinstance(second_command, workers.FinishZulipMessageCatchUp)
    assert second_command.last_message_id == 103
    assert FakeZulipClient.calls == [
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


def test_zulip_bridge_worker_processes_message_with_output_queue():
    output_queue = queue.PriorityQueue()
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )
    message = {"id": 100, "sender_id": 8, "content": "hello"}

    worker._process_message(message)

    response = output_queue.get_nowait()
    add_message = workers.get_sync_response_command(response)
    assert add_message.external_account is worker._external_account
    assert add_message.message == message
    assert add_message.event_id is None


def test_zulip_bridge_worker_processes_stream_with_output_queue():
    output_queue = queue.PriorityQueue()
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
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

    response = output_queue.get_nowait()
    add_stream = workers.get_sync_response_command(response)
    assert add_stream.external_account is worker._external_account
    assert add_stream.stream == stream
    assert add_stream.subscriber_ids == [10, 24]


def test_send_zulip_message_command_sends_and_reports_message_id():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.send_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.SendZulipMessage(
        epoch_version=55,
        message_uuid="message-uuid",
        stream_name="general",
        topic_name="deploys",
        content="hello",
    ).execute(worker)

    assert FakeZulipClient.send_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "stream_name": "general",
            "topic_name": "deploys",
            "content": "hello",
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageSent)
    assert command.epoch_version == 55
    assert command.message_uuid == "message-uuid"
    assert command.zulip_message_id == 12345


def test_send_zulip_message_command_uploads_workspace_file_links():
    output_queue = queue.PriorityQueue()
    file_uuid = sys_uuid.UUID("41f23f93-b87c-47a4-9a12-1fcbd2719e78")
    file = types.SimpleNamespace(
        uuid=file_uuid,
        name="photo.png",
        storage_type="file",
        storage_object_id="41/file",
    )
    FakeZulipClient.send_calls = []
    FakeZulipClient.upload_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    with mock.patch.object(
        workers.messenger_dm_helpers,
        "get_workspace_user_file",
        return_value=file,
    ) as get_file, mock.patch.object(
        workers.file_storage,
        "read_workspace_file",
        return_value=b"image-data",
    ) as read_file:
        workers.SendZulipMessage(
            epoch_version=55,
            message_uuid="message-uuid",
            stream_name="general",
            topic_name="deploys",
            content=(
                f"see ![photo.png](urn:image:{file_uuid}?"
                "name=photo.png&content_type=image%2Fpng&size=10)"
            ),
        ).execute(worker)

    get_file.assert_called_once_with(
        project_id="project",
        user_uuid="bridge-user",
        file_uuid=file_uuid,
    )
    read_file.assert_called_once_with(
        file_uuid=file_uuid,
        storage_type="file",
        storage_object_id="41/file",
    )
    assert FakeZulipClient.upload_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "file_name": "photo.png",
            "data": b"image-data",
        },
    ]
    assert FakeZulipClient.send_calls[0]["content"] == (
        "see ![photo.png](/user_uploads/1/photo.png)"
    )
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageSent)


def test_send_zulip_private_message_command_sends_and_reports_message_id():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.send_private_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.SendZulipPrivateMessage(
        epoch_version=55,
        message_uuid="message-uuid",
        recipient_ids=[42],
        content="hello",
    ).execute(worker)

    assert FakeZulipClient.send_private_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "recipient_ids": [42],
            "content": "hello",
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageSent)
    assert command.epoch_version == 55
    assert command.message_uuid == "message-uuid"
    assert command.zulip_message_id == 12345


def test_zulip_message_sent_is_processed_before_inbound_add_message():
    output_queue = queue.PriorityQueue()
    external_account = _external_account()
    workers.put_sync_response(
        output_queue,
        workers.AddMessage(
            external_account=external_account,
            message={"id": 12345, "timestamp": 1776940760},
        ),
    )
    workers.put_sync_response(
        output_queue,
        workers.ZulipMessageSent(
            external_account=external_account,
            epoch_version=55,
            message_uuid="message-uuid",
            zulip_message_id=12345,
        ),
    )

    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageSent)


def test_update_zulip_message_command_updates_and_reports_success():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.update_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.UpdateZulipMessage(
        epoch_version=56,
        message_uuid="message-uuid",
        message_id=12345,
        content="edited",
    ).execute(worker)

    assert FakeZulipClient.update_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_id": 12345,
            "content": "edited",
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageUpdated)
    assert command.epoch_version == 56


def test_update_zulip_message_command_uploads_workspace_file_links():
    output_queue = queue.PriorityQueue()
    file_uuid = sys_uuid.UUID("9a0a7297-d98f-4d13-a946-85f172630a9d")
    file = types.SimpleNamespace(
        uuid=file_uuid,
        name="report.pdf",
        storage_type="file",
        storage_object_id="9a/file",
    )
    FakeZulipClient.update_calls = []
    FakeZulipClient.upload_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    with mock.patch.object(
        workers.messenger_dm_helpers,
        "get_workspace_user_file",
        return_value=file,
    ), mock.patch.object(
        workers.file_storage,
        "read_workspace_file",
        return_value=b"pdf-data",
    ):
        workers.UpdateZulipMessage(
            epoch_version=56,
            message_uuid="message-uuid",
            message_id=12345,
            content=f"[report.pdf](urn:file:{file_uuid}?name=report.pdf)",
        ).execute(worker)

    assert FakeZulipClient.upload_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "file_name": "report.pdf",
            "data": b"pdf-data",
        },
    ]
    assert FakeZulipClient.update_calls[0]["content"] == (
        "[report.pdf](/user_uploads/1/report.pdf)"
    )
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageUpdated)


def test_delete_zulip_message_command_deletes_and_reports_success():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.delete_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.DeleteZulipMessage(
        epoch_version=57,
        message_uuid="message-uuid",
        message_id=12345,
    ).execute(worker)

    assert FakeZulipClient.delete_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_id": 12345,
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipMessageDeleted)
    assert command.epoch_version == 57


def test_add_zulip_reaction_command_adds_and_reports_success():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.add_reaction_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.AddZulipReaction(
        epoch_version=58,
        message_uuid="message-uuid",
        message_id=12345,
        emoji_name="thumbs_up",
    ).execute(worker)

    assert FakeZulipClient.add_reaction_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_id": 12345,
            "emoji_name": "thumbs_up",
            "emoji_code": None,
            "reaction_type": None,
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipReactionAdded)
    assert command.epoch_version == 58


def test_remove_zulip_reaction_command_removes_and_reports_success():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.remove_reaction_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.RemoveZulipReaction(
        epoch_version=59,
        message_uuid="message-uuid",
        message_id=12345,
        emoji_name="thumbs_up",
    ).execute(worker)

    assert FakeZulipClient.remove_reaction_calls == [
        {
            "login": "user@example.com",
            "token": "zulip-token",
            "message_id": 12345,
            "emoji_name": "thumbs_up",
            "emoji_code": None,
            "reaction_type": None,
        },
    ]
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipReactionRemoved)
    assert command.epoch_version == 59


def test_update_zulip_reaction_command_removes_then_adds_and_reports_success():
    output_queue = queue.PriorityQueue()
    FakeZulipClient.add_reaction_calls = []
    FakeZulipClient.remove_reaction_calls = []
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )

    workers.UpdateZulipReaction(
        epoch_version=60,
        message_uuid="message-uuid",
        old_message_id=12345,
        old_emoji_name="heart",
        message_id=12345,
        emoji_name="thumbs_up",
    ).execute(worker)

    assert FakeZulipClient.remove_reaction_calls[0]["emoji_name"] == "heart"
    assert FakeZulipClient.add_reaction_calls[0]["emoji_name"] == "thumbs_up"
    response = output_queue.get_nowait()
    command = workers.get_sync_response_command(response)
    assert isinstance(command, workers.ZulipReactionUpdated)
    assert command.epoch_version == 60


def test_put_sync_response_waits_when_output_queue_is_full():
    output_queue = queue.PriorityQueue(maxsize=1)
    output_queue.put(workers.PrioritizedSyncResponse(0, object()))
    command = workers.ZulipQueueFailed(external_account=_external_account())
    thread = threading.Thread(
        target=workers.put_sync_response,
        args=(output_queue, command),
    )

    thread.start()
    thread.join(timeout=0.05)
    assert thread.is_alive()
    output_queue.get_nowait()
    thread.join(timeout=1)

    assert not thread.is_alive()
    response = output_queue.get_nowait()
    assert workers.get_sync_response_command(response) is command


def test_stopped_zulip_bridge_worker_does_not_execute_normal_commands():
    executed = []
    input_queue = queue.Queue()

    class NormalCommand:
        def execute(self, worker):
            executed.append(worker)

    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=input_queue,
        output_queue=queue.PriorityQueue(),
        client_cls=FakeZulipClient,
    )

    worker.start()
    worker.stop()
    input_queue.put(NormalCommand())
    input_queue.put(workers.StopWorker())
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert executed == []


def test_zulip_bridge_worker_prioritizes_stream_responses():
    output_queue = queue.PriorityQueue()
    worker = workers.ZulipBridgeWorker(
        external_account=_external_account(),
        input_queue=queue.Queue(),
        output_queue=output_queue,
        client_cls=FakeZulipClient,
    )
    message = {
        "id": 100,
        "sender_id": 8,
        "content": "hello",
        "type": "stream",
    }
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

    worker._process_message(message)
    worker._process_stream(stream, subscriber_ids=[10, 24])

    first_response = output_queue.get_nowait()
    second_response = output_queue.get_nowait()
    assert isinstance(
        workers.get_sync_response_command(first_response),
        workers.AddStream,
    )
    assert isinstance(
        workers.get_sync_response_command(second_response),
        workers.AddMessage,
    )


def test_add_message_executes_with_cache():
    external_account = _external_account()
    stream = object()
    topic = object()
    message = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append({
                "method": "get_or_create_stream",
                "external_account": external_account,
                "stream_info": stream_info,
            })
            return stream

        def get_or_create_topic(
            self,
            external_account,
            stream,
            stream_info,
            topic_name,
        ):
            self.calls.append({
                "method": "get_or_create_topic",
                "external_account": external_account,
                "stream": stream,
                "stream_info": stream_info,
                "topic_name": topic_name,
            })
            return topic

        def get_or_create_message(
            self,
            external_account,
            stream,
            topic,
            stream_info,
            topic_name,
            message_info,
        ):
            self.calls.append({
                "method": "get_or_create_message",
                "external_account": external_account,
                "stream": stream,
                "topic": topic,
                "stream_info": stream_info,
                "topic_name": topic_name,
                "message_info": message_info,
            })
            return message

    cache = FakeCache()
    command = workers.AddMessage(
        external_account=external_account,
        message={
            "id": 100,
            "type": "stream",
            "stream_id": 3,
            "display_recipient": "general",
            "subject": "deploys",
            "sender_id": 24,
            "content": "hello",
            "flags": ["read"],
            "timestamp": 1770998098,
        },
    )

    result = command.execute(cache=cache)

    assert result is message
    stream_info = {
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
        "subscriber_ids": [24, 10],
        "event_type": "message",
    }
    assert cache.calls == [
        {
            "method": "get_or_create_stream",
            "external_account": external_account,
            "stream_info": stream_info,
        },
        {
            "method": "get_or_create_topic",
            "external_account": external_account,
            "stream": stream,
            "stream_info": stream_info,
            "topic_name": "deploys",
        },
        {
            "method": "get_or_create_message",
            "external_account": external_account,
            "stream": stream,
            "topic": topic,
            "stream_info": stream_info,
            "topic_name": "deploys",
            "message_info": {
                "message_id": 100,
                "sender_id": 24,
                "content": "hello",
                "read": True,
                "created_at": datetime.datetime.fromtimestamp(
                    1770998098,
                    tz=datetime.timezone.utc,
                ),
                "updated_at": datetime.datetime.fromtimestamp(
                    1770998098,
                    tz=datetime.timezone.utc,
                ),
            },
        },
    ]


def test_add_message_treats_missing_flags_as_unread():
    command = workers.AddMessage(
        external_account=_external_account(),
        message={
            "id": 100,
            "sender_id": 24,
            "content": "hello",
            "timestamp": 1770998098,
        },
    )

    message_info = command._get_message_info()

    assert message_info["read"] is False


def test_add_message_skips_single_subscriber_private_message():
    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append("get_or_create_stream")

        def get_or_create_topic(
            self,
            external_account,
            stream,
            stream_info,
            topic_name,
        ):
            self.calls.append("get_or_create_topic")

        def get_or_create_message(
            self,
            external_account,
            stream,
            topic,
            stream_info,
            topic_name,
            message_info,
        ):
            self.calls.append("get_or_create_message")

    cache = FakeCache()
    command = workers.AddMessage(
        external_account=_external_account(),
        message={
            "id": 82,
            "type": "private",
            "recipient_id": 82,
            "display_recipient": [
                {"id": 8, "full_name": "Admin"},
            ],
            "sender_id": 8,
            "content": "self dm",
            "flags": [],
            "timestamp": 1770998098,
        },
    )

    result = command.execute(cache=cache)

    assert result is None
    assert cache.calls == []


def test_add_private_message_executes_with_cache():
    external_account = _external_account()
    stream = object()
    topic = object()
    message = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def get_or_create_stream(self, external_account, stream_info):
            self.calls.append({
                "method": "get_or_create_stream",
                "external_account": external_account,
                "stream_info": stream_info,
            })
            return stream

        def get_or_create_topic(
            self,
            external_account,
            stream,
            stream_info,
            topic_name,
        ):
            self.calls.append({
                "method": "get_or_create_topic",
                "external_account": external_account,
                "stream": stream,
                "stream_info": stream_info,
                "topic_name": topic_name,
            })
            return topic

        def get_or_create_message(
            self,
            external_account,
            stream,
            topic,
            stream_info,
            topic_name,
            message_info,
        ):
            self.calls.append({
                "method": "get_or_create_message",
                "external_account": external_account,
                "stream": stream,
                "topic": topic,
                "stream_info": stream_info,
                "topic_name": topic_name,
                "message_info": message_info,
            })
            return message

    cache = FakeCache()
    command = workers.AddMessage(
        external_account=external_account,
        message={
            "id": 101,
            "type": "private",
            "recipient_id": 79,
            "display_recipient": [
                {
                    "id": 8,
                    "full_name": "admin",
                },
                {
                    "id": 10,
                    "full_name": "gmelikov",
                },
            ],
            "sender_id": 8,
            "content": "hello private",
            "flags": [],
            "timestamp": 1772202531,
        },
    )

    result = command.execute(cache=cache)

    assert result is message
    stream_info = {
        "type": "private",
        "stream_id": 79,
        "display_recipient": "admin, gmelikov",
        "description": "",
        "creator_id": 10,
        "timestamp": datetime.datetime.fromtimestamp(
            1772202531,
            tz=datetime.timezone.utc,
        ),
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [8, 10],
        "default_topic_name": "zulip",
        "event_type": "message",
    }
    assert cache.calls == [
        {
            "method": "get_or_create_stream",
            "external_account": external_account,
            "stream_info": stream_info,
        },
        {
            "method": "get_or_create_topic",
            "external_account": external_account,
            "stream": stream,
            "stream_info": stream_info,
            "topic_name": "zulip",
        },
        {
            "method": "get_or_create_message",
            "external_account": external_account,
            "stream": stream,
            "topic": topic,
            "stream_info": stream_info,
            "topic_name": "zulip",
            "message_info": {
                "message_id": 101,
                "sender_id": 8,
                "content": "hello private",
                "read": False,
                "created_at": datetime.datetime.fromtimestamp(
                    1772202531,
                    tz=datetime.timezone.utc,
                ),
                "updated_at": datetime.datetime.fromtimestamp(
                    1772202531,
                    tz=datetime.timezone.utc,
                ),
            },
        },
    ]


def test_update_message_executes_with_cache():
    external_account = _external_account()
    message = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def update_message(self, external_account, message_info):
            self.calls.append({
                "external_account": external_account,
                "message_info": message_info,
            })
            return message

    cache = FakeCache()
    command = workers.UpdateMessage(
        external_account=external_account,
        event={
            "id": 45,
            "type": "update_message",
            "user_id": 8,
            "message_id": 101,
            "message_ids": [101],
            "content": "edited",
            "edit_timestamp": 1772202600,
            "rendering_only": False,
        },
    )

    result = command.execute(cache=cache)

    assert result is message
    assert command.event_id == 45
    assert command.last_message_id == 101
    assert cache.calls == [
        {
            "external_account": external_account,
            "message_info": {
                "message_id": 101,
                "sender_id": 8,
                "content": "edited",
                "updated_at": datetime.datetime.fromtimestamp(
                    1772202600,
                    tz=datetime.timezone.utc,
                ),
            },
        },
    ]


def test_delete_message_executes_with_cache():
    external_account = _external_account()
    messages = [object()]

    class FakeCache:
        def __init__(self):
            self.calls = []

        def delete_messages(self, external_account, message_ids):
            self.calls.append({
                "external_account": external_account,
                "message_ids": message_ids,
            })
            return messages

    cache = FakeCache()
    command = workers.DeleteMessage(
        external_account=external_account,
        event={
            "id": 46,
            "type": "delete_message",
            "message_type": "stream",
            "message_ids": [101, 102],
        },
    )

    result = command.execute(cache=cache)

    assert result is messages
    assert command.event_id == 46
    assert command.last_message_id == 102
    assert cache.calls == [
        {
            "external_account": external_account,
            "message_ids": [101, 102],
        },
    ]


def test_add_message_reaction_executes_with_cache():
    external_account = _external_account()
    reaction = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def add_message_reaction(self, external_account, reaction_info):
            self.calls.append({
                "external_account": external_account,
                "reaction_info": reaction_info,
            })
            return reaction

    cache = FakeCache()
    command = workers.AddMessageReaction(
        external_account=external_account,
        event={
            "id": 48,
            "type": "reaction",
            "op": "add",
            "message_id": 101,
            "user_id": 24,
            "emoji_name": "thumbs_up",
        },
    )

    result = command.execute(cache=cache)

    assert result is reaction
    assert command.event_id == 48
    assert command.last_message_id == 101
    assert cache.calls == [
        {
            "external_account": external_account,
            "reaction_info": {
                "message_id": 101,
                "user_id": 24,
                "emoji_name": "thumbs_up",
            },
        },
    ]


def test_remove_message_reaction_executes_with_cache():
    external_account = _external_account()
    reaction = object()

    class FakeCache:
        def __init__(self):
            self.calls = []

        def remove_message_reaction(self, external_account, reaction_info):
            self.calls.append({
                "external_account": external_account,
                "reaction_info": reaction_info,
            })
            return reaction

    cache = FakeCache()
    command = workers.RemoveMessageReaction(
        external_account=external_account,
        event={
            "id": 49,
            "type": "reaction",
            "op": "remove",
            "message_id": 101,
            "user_id": 24,
            "emoji_name": "thumbs_up",
        },
    )

    result = command.execute(cache=cache)

    assert result is reaction
    assert command.event_id == 49
    assert command.last_message_id == 101
    assert cache.calls == [
        {
            "external_account": external_account,
            "reaction_info": {
                "message_id": 101,
                "user_id": 24,
                "emoji_name": "thumbs_up",
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
                "event_type": "stream",
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
