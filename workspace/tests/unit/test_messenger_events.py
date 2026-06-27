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

import asyncio
import datetime
import importlib
import json
import sys
import types
import unittest
import uuid as sys_uuid
from unittest import mock

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import controllers
from workspace.messenger_api import events
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models
from workspace.messenger_api import websocket_protocol


class FakeWebsocket:
    def __init__(self, frames):
        self._frames = iter(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._frames)
        except StopIteration:
            raise StopAsyncIteration


class MessengerEventsTestCase(unittest.TestCase):
    def test_normalize_epoch_version(self):
        self.assertEqual(0, events.normalize_epoch_version(None))
        self.assertEqual(42, events.normalize_epoch_version("42"))
        with self.assertRaises(ra_exc.ParseError):
            events.normalize_epoch_version("-1")

    def test_limit_is_capped(self):
        self.assertEqual(1, events.normalize_events_limit("1"))
        self.assertEqual(events.MAX_EVENTS_LIMIT, events.normalize_events_limit("9999"))
        with self.assertRaises(ra_exc.ParseError):
            events.normalize_events_limit("0")

    def test_bearer_token_from_subprotocols(self):
        self.assertEqual(
            "abc.def",
            websocket_protocol.bearer_token_from_subprotocols(
                "workspace.events.v1, bearer.abc.def"
            ),
        )
        self.assertIsNone(
            websocket_protocol.bearer_token_from_subprotocols("workspace.events.v1")
        )

    def test_parse_last_epoch_version(self):
        self.assertEqual(
            11,
            websocket_protocol.parse_last_epoch_version(
                "/v1/events/ws?last_epoch_version=11"
            ),
        )

    def test_conditional_filter_suffixes(self):
        split = controllers.WorkspaceBaseResourceControllerPaginated._split_filter_operator
        self.assertEqual(
            ("created_at", dm_filters.GE),
            split("created_at=>"),
        )
        self.assertEqual(
            ("epoch_version", dm_filters.LE),
            split("epoch_version=<"),
        )
        self.assertEqual(
            ("epoch_version", dm_filters.GT),
            split("epoch_version>"),
        )
        self.assertEqual(
            ("epoch_version", dm_filters.LT),
            split("epoch_version<"),
        )
        self.assertEqual(
            ("epoch_version", None),
            split("epoch_version"),
        )

    def test_event_row_to_messenger_event_uses_rest_message_snapshot(self):
        author_uuid = sys_uuid.uuid4()
        recipient_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message = {
            "uuid": str(message_uuid),
            "user_uuid": str(recipient_uuid),
            "project_id": str(project_id),
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
            "stream_uuid": str(stream_uuid),
            "author_uuid": str(author_uuid),
            "topic_uuid": str(topic_uuid),
            "payload": {
                "kind": "markdown",
                "content": "hello",
            },
            "read": False,
            "pinned": False,
            "starred": False,
            "is_own": False,
        }
        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 7,
                "user_uuid": recipient_uuid,
                "payload": {
                    "kind": "message.created",
                    "uuid": str(message_uuid),
                    "user_uuid": str(recipient_uuid),
                    "project_id": str(project_id),
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "author_uuid": str(author_uuid),
                    "payload": {
                        "kind": "markdown",
                        "content": "hello",
                    },
                    "read": False,
                    "pinned": False,
                    "starred": False,
                    "is_own": False,
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:00:00.000000",
                },
            }
        )

        self.assertEqual(7, event["epoch_version"])
        self.assertEqual("message", event["type"])
        self.assertEqual(message, event["message"])

    def test_event_row_to_messenger_event_uses_rest_folder_snapshot(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        folder = {
            "uuid": str(folder_uuid),
            "user_uuid": str(user_uuid),
            "project_id": str(project_id),
            "title": "Inbox",
            "background_color_value": None,
            "unread_count": 0,
            "system_type": "created",
            "folder_items": [],
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
        }

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 8,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "folder.created",
                    "uuid": str(folder_uuid),
                    "user_uuid": str(user_uuid),
                    "project_id": str(project_id),
                    "title": "Inbox",
                    "background_color_value": None,
                    "unread_count": 0,
                    "system_type": "created",
                    "folder_items": [],
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:00:00.000000",
                },
            }
        )

        self.assertEqual(8, event["epoch_version"])
        self.assertEqual("folder", event["type"])
        self.assertEqual("folder.created", event["kind"])
        self.assertEqual(folder, event["folder"])

    def test_event_row_to_messenger_event_uses_updated_folder_snapshot(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 9,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "folder.updated",
                    "uuid": str(folder_uuid),
                    "user_uuid": str(user_uuid),
                    "project_id": str(project_id),
                    "title": "Archive",
                    "background_color_value": None,
                    "unread_count": 0,
                    "system_type": "created",
                    "folder_items": [],
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:05:00.000000",
                },
            }
        )

        self.assertEqual(9, event["epoch_version"])
        self.assertEqual("folder", event["type"])
        self.assertEqual("folder.updated", event["kind"])
        self.assertEqual("Archive", event["folder"]["title"])
        self.assertEqual(str(folder_uuid), event["folder"]["uuid"])

    def test_event_row_to_messenger_event_uses_rest_stream_snapshot(self):
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        stream = {
            "uuid": str(stream_uuid),
            "user_uuid": str(user_uuid),
            "name": "Engineering",
            "description": "Engineering workspace",
            "project_id": str(project_id),
            "owner": str(owner_uuid),
            "role": "member",
            "notification_mode": "all_messages",
            "unread_count": 0,
            "source_name": "native",
            "source": {"kind": "native"},
            "invite_only": False,
            "announce": False,
            "private": False,
            "is_archived": False,
            "direct_user_uuid": None,
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
        }

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 10,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "stream.created",
                    "uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "name": "Engineering",
                    "description": "Engineering workspace",
                    "project_id": str(project_id),
                    "owner": str(owner_uuid),
                    "role": "member",
                    "notification_mode": "all_messages",
                    "unread_count": 0,
                    "source_name": "native",
                    "source": {"kind": "native"},
                    "invite_only": False,
                    "announce": False,
                    "private": False,
                    "is_archived": False,
                    "private_index": "internal-value",
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:00:00.000000",
                },
            }
        )

        self.assertEqual(10, event["epoch_version"])
        self.assertEqual("stream", event["type"])
        self.assertEqual("stream.created", event["kind"])
        self.assertEqual(stream, event["stream"])

    def test_event_row_to_messenger_event_uses_updated_stream_snapshot(self):
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 11,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "stream.updated",
                    "uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "name": "Core Team",
                    "description": "Core workspace",
                    "project_id": str(project_id),
                    "owner": str(owner_uuid),
                    "role": "member",
                    "notification_mode": "muted",
                    "unread_count": 0,
                    "source_name": "native",
                    "source": {"kind": "native"},
                    "invite_only": True,
                    "announce": False,
                    "private": False,
                    "is_archived": True,
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:05:00.000000",
                },
            }
        )

        self.assertEqual(11, event["epoch_version"])
        self.assertEqual("stream", event["type"])
        self.assertEqual("stream.updated", event["kind"])
        self.assertEqual("Core Team", event["stream"]["name"])
        self.assertEqual(True, event["stream"]["invite_only"])
        self.assertEqual(True, event["stream"]["is_archived"])
        self.assertEqual("muted", event["stream"]["notification_mode"])

    def test_event_row_to_messenger_event_uses_deleted_stream_id(self):
        stream_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 12,
                "payload": {
                    "kind": "stream.deleted",
                    "uuid": str(stream_uuid),
                },
            }
        )

        self.assertEqual(12, event["epoch_version"])
        self.assertEqual("stream", event["type"])
        self.assertEqual("stream.deleted", event["kind"])
        self.assertEqual(
            {"uuid": str(stream_uuid)},
            event["stream"],
        )

    def test_event_row_to_messenger_event_uses_rest_topic_snapshot(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic = {
            "uuid": str(topic_uuid),
            "user_uuid": str(user_uuid),
            "project_id": str(project_id),
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
            "name": "Planning",
            "stream_uuid": str(stream_uuid),
            "unread_count": 2,
            "is_default": False,
            "is_done": True,
        }

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 13,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "topic.created",
                    "uuid": str(topic_uuid),
                    "user_uuid": str(user_uuid),
                    "project_id": str(project_id),
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:00:00.000000",
                    "name": "Planning",
                    "stream_uuid": str(stream_uuid),
                    "unread_count": 2,
                    "is_default": False,
                    "is_done": True,
                },
            }
        )

        self.assertEqual(13, event["epoch_version"])
        self.assertEqual("topic", event["type"])
        self.assertEqual("topic.created", event["kind"])
        self.assertEqual(topic, event["topic"])

    def test_event_row_to_messenger_event_uses_updated_topic_snapshot(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 14,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "topic.updated",
                    "uuid": str(topic_uuid),
                    "user_uuid": str(user_uuid),
                    "project_id": str(project_id),
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:05:00.000000",
                    "name": "Retros",
                    "stream_uuid": str(stream_uuid),
                    "unread_count": 0,
                    "is_default": False,
                    "is_done": False,
                },
            }
        )

        self.assertEqual(14, event["epoch_version"])
        self.assertEqual("topic", event["type"])
        self.assertEqual("topic.updated", event["kind"])
        self.assertEqual("Retros", event["topic"]["name"])
        self.assertEqual(str(topic_uuid), event["topic"]["uuid"])
        self.assertEqual(str(stream_uuid), event["topic"]["stream_uuid"])

    def test_event_row_to_messenger_event_uses_deleted_topic_id(self):
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 15,
                "payload": {
                    "kind": "topic.deleted",
                    "uuid": str(topic_uuid),
                    "stream_uuid": str(stream_uuid),
                },
            }
        )

        self.assertEqual(15, event["epoch_version"])
        self.assertEqual("topic", event["type"])
        self.assertEqual("topic.deleted", event["kind"])
        self.assertEqual(
            {
                "uuid": str(topic_uuid),
                "stream_uuid": str(stream_uuid),
            },
            event["topic"],
        )

    def test_event_row_to_messenger_event_uses_stream_bindings_snapshot(self):
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        second_user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding_uuid = sys_uuid.uuid4()
        second_binding_uuid = sys_uuid.uuid4()
        stream_bindings = [
            {
                "uuid": str(binding_uuid),
                "project_id": str(project_id),
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(user_uuid),
                "who_uuid": str(owner_uuid),
                "role": "member",
                "notification_mode": "mentions_only",
                "created_at": "2026-06-24T10:00:00.000000Z",
                "updated_at": "2026-06-24T10:00:00.000000Z",
            },
            {
                "uuid": str(second_binding_uuid),
                "project_id": str(project_id),
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(second_user_uuid),
                "who_uuid": str(owner_uuid),
                "role": "owner",
                "notification_mode": "all_messages",
                "created_at": "2026-06-24T10:00:00.000000Z",
                "updated_at": "2026-06-24T10:00:00.000000Z",
            },
        ]
        first_payload_binding = {
            "uuid": str(binding_uuid),
            "project_id": str(project_id),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(user_uuid),
            "who_uuid": str(owner_uuid),
            "role": "member",
            "notification_mode": "mentions_only",
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
        }
        second_payload_binding = {
            "uuid": str(second_binding_uuid),
            "project_id": str(project_id),
            "stream_uuid": str(stream_uuid),
            "user_uuid": str(second_user_uuid),
            "who_uuid": str(owner_uuid),
            "role": "owner",
            "notification_mode": "all_messages",
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
        }

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 11,
                "user_uuid": owner_uuid,
                "payload": {
                    "kind": "stream_bindings.created",
                    "project_id": str(project_id),
                    "stream_uuid": str(stream_uuid),
                    "stream_bindings": [
                        first_payload_binding,
                        second_payload_binding,
                    ],
                },
            }
        )

        self.assertEqual(11, event["epoch_version"])
        self.assertEqual("stream_binding", event["type"])
        self.assertEqual("stream_bindings.created", event["kind"])
        self.assertEqual(str(stream_uuid), event["stream_uuid"])
        self.assertEqual(stream_bindings, event["stream_bindings"])

    def test_event_row_to_messenger_event_uses_deleted_folder_id(self):
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 10,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "folder.deleted",
                    "uuid": str(folder_uuid),
                },
            }
        )

        self.assertEqual(10, event["epoch_version"])
        self.assertEqual("folder", event["type"])
        self.assertEqual("folder.deleted", event["kind"])
        self.assertEqual({"uuid": str(folder_uuid)}, event["folder"])

    def test_event_row_to_messenger_event_uses_deleted_folder_item_id(self):
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()

        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 11,
                "user_uuid": user_uuid,
                "payload": {
                    "kind": "folder_item.deleted",
                    "uuid": str(item_uuid),
                },
            }
        )

        self.assertEqual(11, event["epoch_version"])
        self.assertEqual("folder_item", event["type"])
        self.assertEqual("folder_item.deleted", event["kind"])
        self.assertEqual({"uuid": str(item_uuid)}, event["folder_item"])

    def test_message_event_payload_accepts_postgres_json_timestamp(self):
        author_uuid = sys_uuid.uuid4()
        recipient_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "message.created",
                "uuid": str(message_uuid),
                "user_uuid": str(recipient_uuid),
                "project_id": str(project_id),
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(author_uuid),
                "payload": {
                    "kind": "markdown",
                    "content": "hello",
                },
                "read": False,
                "pinned": False,
                "starred": False,
                "is_own": False,
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
            }
        )

        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.created_at
            ),
        )

    def test_folder_event_payload_accepts_postgres_json_timestamp(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "folder.created",
                "uuid": str(folder_uuid),
                "user_uuid": str(user_uuid),
                "project_id": str(project_id),
                "title": "Inbox",
                "background_color_value": None,
                "unread_count": 0,
                "system_type": "created",
                "folder_items": [],
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
            }
        )

        self.assertEqual("Inbox", payload.title)
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.created_at
            ),
        )

    def test_folder_updated_event_payload_accepts_postgres_json_timestamp(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "folder.updated",
                "uuid": str(folder_uuid),
                "user_uuid": str(user_uuid),
                "project_id": str(project_id),
                "title": "Archive",
                "background_color_value": None,
                "unread_count": 0,
                "system_type": "created",
                "folder_items": [],
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
            }
        )

        self.assertEqual("Archive", payload.title)
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.updated_at
            ),
        )

    def test_stream_created_event_payload_accepts_postgres_json_timestamp(self):
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "stream.created",
                "uuid": str(stream_uuid),
                "user_uuid": str(user_uuid),
                "name": "Engineering",
                "description": "Engineering workspace",
                "project_id": str(project_id),
                "owner": str(owner_uuid),
                "role": "owner",
                "notification_mode": "all_messages",
                "unread_count": 0,
                "source_name": "native",
                "source": {"kind": "native"},
                "invite_only": False,
                "announce": False,
                "private": False,
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
            }
        )

        self.assertEqual("Engineering", payload.name)
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.created_at
            ),
        )

    def test_stream_bindings_created_event_payload_accepts_binding_list(self):
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "stream_bindings.created",
                "project_id": str(project_id),
                "stream_uuid": str(stream_uuid),
                "stream_bindings": [
                    {
                        "uuid": str(binding_uuid),
                        "project_id": str(project_id),
                        "stream_uuid": str(stream_uuid),
                        "user_uuid": str(user_uuid),
                        "who_uuid": str(owner_uuid),
                        "role": "member",
                        "notification_mode": "mentions_only",
                        "created_at": "2026-06-24T22:28:34.166369Z",
                        "updated_at": "2026-06-24T22:28:34.166369Z",
                    }
                ],
            }
        )

        self.assertEqual(stream_uuid, payload.stream_uuid)
        self.assertEqual(str(user_uuid), payload.stream_bindings[0]["user_uuid"])

    def test_topic_created_event_payload_accepts_postgres_json_timestamp(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "topic.created",
                "uuid": str(topic_uuid),
                "user_uuid": str(user_uuid),
                "project_id": str(project_id),
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
                "name": "Planning",
                "stream_uuid": str(stream_uuid),
                "unread_count": 0,
                "is_default": False,
                "is_done": False,
            }
        )

        self.assertEqual("Planning", payload.name)
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.created_at
            ),
        )

    def test_topic_deleted_event_payload_accepts_topic_id(self):
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "topic.deleted",
                "uuid": str(topic_uuid),
                "stream_uuid": str(stream_uuid),
            }
        )

        self.assertEqual(topic_uuid, payload.uuid)
        self.assertEqual(stream_uuid, payload.stream_uuid)

    def test_folder_deleted_event_payload_accepts_folder_id(self):
        folder_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "folder.deleted",
                "uuid": str(folder_uuid),
            }
        )

        self.assertEqual(folder_uuid, payload.uuid)

    def test_folder_item_deleted_event_payload_accepts_item_id(self):
        item_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "folder_item.deleted",
                "uuid": str(item_uuid),
            }
        )

        self.assertEqual(item_uuid, payload.uuid)

    def test_workspace_event_insert_omits_generated_epoch_version(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        payload = event_payloads.FolderCreatedEventPayload(
            uuid=folder_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            title="Inbox",
            background_color_value=None,
            unread_count=0,
            system_type="created",
            folder_items=[],
        )
        event = models.WorkspaceEvent(
            uuid=sys_uuid.uuid4(),
            user_uuid=user_uuid,
            project_id=project_id,
            payload=payload,
        )
        session = mock.MagicMock()
        session.execute.return_value.fetchone.return_value = {
            "epoch_version": 42
        }
        engine = mock.MagicMock()
        engine.escape.side_effect = lambda value: f'"{value}"'
        engine.session_manager.return_value.__enter__.return_value = session

        with mock.patch.object(
            models.WorkspaceEvent, "_get_engine", return_value=engine
        ):
            result = event.insert()

        statement = session.execute.call_args.args[0]
        inserted_columns = statement.split("VALUES", 1)[0]
        self.assertNotIn("epoch_version", inserted_columns)
        self.assertIn('RETURNING "epoch_version"', statement)
        self.assertEqual(42, result)
        self.assertEqual(42, event.epoch_version)

    def test_create_folder_event_uses_user_folder_snapshot(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        user_folder = models.UserFolder(
            uuid=folder_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            title="Inbox",
            background_color_value=None,
            unread_count=0,
            system_type="created",
            folder_items=[],
            created_at=created_at,
            updated_at=created_at,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 42

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_folder_event(
                folder=user_folder,
                session=session,
            )

        self.assertEqual(42, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.FolderCreatedEventPayload,
        )
        self.assertEqual("Inbox", created_event["payload"].title)
        self.assertEqual(created_at, created_event["payload"].created_at)

    def test_create_folder_updated_event_uses_user_folder_snapshot(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        updated_at = datetime.datetime(
            2026, 6, 24, 10, 5, 0, tzinfo=datetime.timezone.utc
        )
        user_folder = models.UserFolder(
            uuid=folder_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            title="Archive",
            background_color_value=None,
            unread_count=0,
            system_type="created",
            folder_items=[],
            created_at=created_at,
            updated_at=updated_at,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 43

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_folder_updated_event(
                folder=user_folder,
                session=session,
            )

        self.assertEqual(43, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.FolderUpdatedEventPayload,
        )
        self.assertEqual("Archive", created_event["payload"].title)
        self.assertEqual(updated_at, created_event["payload"].updated_at)

    def test_create_stream_event_uses_user_stream_snapshot(self):
        project_id = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        user_stream = models.WorkspaceUserStream(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            name="Engineering",
            description="Engineering workspace",
            project_id=project_id,
            owner=owner_uuid,
            role="member",
            notification_mode="all_messages",
            unread_count=0,
            source_name="native",
            source=models.NativeSource(),
            invite_only=False,
            announce=False,
            private=False,
            is_archived=False,
            created_at=created_at,
            updated_at=created_at,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 44

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_stream_event(
                stream=user_stream,
                session=session,
            )

        self.assertEqual(44, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.StreamCreatedEventPayload,
        )
        self.assertEqual("Engineering", created_event["payload"].name)
        self.assertEqual(False, created_event["payload"].is_archived)
        self.assertEqual(
            "all_messages",
            created_event["payload"].notification_mode,
        )
        self.assertEqual(created_at, created_event["payload"].created_at)

    def test_create_stream_updated_event_uses_user_stream_snapshot(self):
        project_id = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        user_stream = models.WorkspaceUserStream(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            name="Engineering",
            description="Engineering workspace",
            project_id=project_id,
            owner=owner_uuid,
            role="member",
            notification_mode="muted",
            unread_count=0,
            source_name="native",
            source=models.NativeSource(),
            invite_only=False,
            announce=False,
            private=False,
            is_archived=True,
            created_at=created_at,
            updated_at=created_at,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 46

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_stream_updated_event(
                stream=user_stream,
                session=session,
            )

        self.assertEqual(46, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.StreamUpdatedEventPayload,
        )
        self.assertEqual("Engineering", created_event["payload"].name)
        self.assertEqual(True, created_event["payload"].is_archived)
        self.assertEqual("muted", created_event["payload"].notification_mode)

    def test_create_topic_event_uses_user_topic_snapshot(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        user_topic = models.WorkspaceUserTopic(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            created_at=created_at,
            updated_at=created_at,
            name="Planning",
            stream_uuid=stream_uuid,
            unread_count=0,
            is_default=False,
            is_done=False,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 48

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_topic_event(
                topic=user_topic,
                session=session,
            )

        self.assertEqual(48, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.TopicCreatedEventPayload,
        )
        self.assertEqual("Planning", created_event["payload"].name)
        self.assertEqual(False, created_event["payload"].is_done)

    def test_create_topic_updated_event_uses_user_topic_snapshot(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        updated_at = datetime.datetime(
            2026, 6, 24, 10, 5, 0, tzinfo=datetime.timezone.utc
        )
        user_topic = models.WorkspaceUserTopic(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            created_at=created_at,
            updated_at=updated_at,
            name="Retros",
            stream_uuid=stream_uuid,
            unread_count=1,
            is_default=False,
            is_done=True,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 49

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_topic_updated_event(
                topic=user_topic,
                session=session,
            )

        self.assertEqual(49, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.TopicUpdatedEventPayload,
        )
        self.assertEqual("Retros", created_event["payload"].name)
        self.assertEqual(True, created_event["payload"].is_done)

    def test_create_stream_bindings_created_event_uses_binding_snapshots(self):
        project_id = sys_uuid.uuid4()
        recipient_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        second_user_uuid = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        binding_uuid = sys_uuid.uuid4()
        second_binding_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        binding = models.WorkspaceStreamBinding(
            uuid=binding_uuid,
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=user_uuid,
            who_uuid=owner_uuid,
            role="member",
            notification_mode="mentions_only",
            created_at=created_at,
            updated_at=created_at,
        )
        second_binding = models.WorkspaceStreamBinding(
            uuid=second_binding_uuid,
            project_id=project_id,
            stream_uuid=stream_uuid,
            user_uuid=second_user_uuid,
            who_uuid=owner_uuid,
            role="owner",
            notification_mode="all_messages",
            created_at=created_at,
            updated_at=created_at,
        )
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 45

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_stream_bindings_created_event(
                bindings=[binding, second_binding],
                user_uuid=recipient_uuid,
                session=session,
            )

        self.assertEqual(45, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(recipient_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.StreamBindingsCreatedEventPayload,
        )
        self.assertEqual(stream_uuid, created_event["payload"].stream_uuid)
        self.assertEqual(
            [str(user_uuid), str(second_user_uuid)],
            [
                binding["user_uuid"]
                for binding in created_event["payload"].stream_bindings
            ],
        )
        self.assertEqual(
            "2026-06-24T10:00:00.000000Z",
            created_event["payload"].stream_bindings[0]["created_at"],
        )
        self.assertEqual(
            "mentions_only",
            created_event["payload"].stream_bindings[0]["notification_mode"],
        )

    def test_create_folder_deleted_event_uses_folder_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 44

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_folder_deleted_event(
                project_id=project_id,
                user_uuid=user_uuid,
                folder_uuid=folder_uuid,
                session=session,
            )

        self.assertEqual(44, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.FolderDeletedEventPayload,
        )
        self.assertEqual(folder_uuid, created_event["payload"].uuid)

    def test_create_stream_deleted_event_uses_stream_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 47

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_stream_deleted_event(
                project_id=project_id,
                user_uuid=user_uuid,
                stream_uuid=stream_uuid,
                session=session,
            )

        self.assertEqual(47, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.StreamDeletedEventPayload,
        )
        self.assertEqual(stream_uuid, created_event["payload"].uuid)

    def test_create_topic_deleted_event_uses_topic_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 50

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_topic_deleted_event(
                project_id=project_id,
                user_uuid=user_uuid,
                topic_uuid=topic_uuid,
                stream_uuid=stream_uuid,
                session=session,
            )

        self.assertEqual(50, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.TopicDeletedEventPayload,
        )
        self.assertEqual(topic_uuid, created_event["payload"].uuid)
        self.assertEqual(stream_uuid, created_event["payload"].stream_uuid)

    def test_create_folder_item_deleted_event_uses_item_id(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()
        session = object()
        created_event = {}

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_event.update(kwargs)

            def insert(self, session=None):
                created_event["insert_session"] = session
                return 45

        with mock.patch.object(
            events.models, "WorkspaceEvent", FakeWorkspaceEvent
        ):
            result = events.create_folder_item_deleted_event(
                project_id=project_id,
                user_uuid=user_uuid,
                item_uuid=item_uuid,
                session=session,
            )

        self.assertEqual(45, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self.assertIsInstance(
            created_event["payload"],
            event_payloads.FolderItemDeletedEventPayload,
        )
        self.assertEqual(item_uuid, created_event["payload"].uuid)

    def test_websocket_consumer_accepts_pong_frames(self):
        websockets_stub = types.ModuleType("websockets")
        websockets_stub.serve = None
        sys.modules.setdefault("websockets", websockets_stub)
        websocket_service = importlib.import_module(
            "workspace.messenger_api.websocket_service"
        )

        server = websocket_service.MessengerEventsWebsocketServer(
            db_url="postgresql://example",
            iam_engine_driver=None,
            heartbeat_interval=30,
            client_timeout=30,
            catchup_limit=500,
            send_queue_limit=100,
        )
        connection = websocket_service.ClientConnection(
            websocket=FakeWebsocket(
                [
                    json.dumps({"type": "pong", "ts": "2026-06-24T00:00:00+00:00"}),
                    json.dumps({"type": "ack", "epoch_version": 9}),
                ]
            ),
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            last_epoch_version=7,
        )

        asyncio.run(server._consume_client_frames(connection))

        self.assertEqual(9, connection.last_epoch_version)


if __name__ == "__main__":
    unittest.main()
