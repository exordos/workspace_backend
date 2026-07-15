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
from workspace.messenger_api.dm import message_payloads
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
        split = (
            controllers.WorkspaceBaseResourceControllerPaginated._split_filter_operator
        )
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

    def _workspace_event_row(self, *, payload, object_type, action):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        event_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        return {
            "schema_version": 1,
            "uuid": event_uuid,
            "epoch_version": 7,
            "project_id": project_id,
            "user_uuid": user_uuid,
            "object_type": object_type,
            "action": action,
            "created_at": created_at,
            "updated_at": created_at,
            "payload": payload,
        }

    def test_event_row_to_messenger_event_returns_flat_message_event(self):
        message_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "message.created",
            "uuid": str(message_uuid),
            "user_uuid": str(sys_uuid.uuid4()),
            "project_id": str(sys_uuid.uuid4()),
            "stream_uuid": str(sys_uuid.uuid4()),
            "topic_uuid": str(sys_uuid.uuid4()),
            "author_uuid": str(sys_uuid.uuid4()),
            "payload": {"kind": "markdown", "content": "hello"},
            "read": False,
            "pinned": False,
            "starred": False,
            "is_own": False,
            "reactions": {},
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="message",
            action="created",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual(1, event["schema_version"])
        self.assertEqual(str(row["uuid"]), event["uuid"])
        self.assertEqual(7, event["epoch_version"])
        self.assertEqual(str(row["project_id"]), event["project_id"])
        self.assertEqual(str(row["user_uuid"]), event["user_uuid"])
        self.assertEqual("message", event["object_type"])
        self.assertEqual("created", event["action"])
        self.assertEqual(payload, event["payload"])
        self.assertNotIn("type", event)
        self.assertNotIn("message", event)
        self.assertNotIn("kind", event)

    def test_event_row_to_messenger_event_preserves_reaction_event(self):
        reaction_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "message_reaction.created",
            "uuid": str(reaction_uuid),
            "project_id": str(sys_uuid.uuid4()),
            "message_uuid": str(message_uuid),
            "user_uuid": str(user_uuid),
            "emoji_name": "thumbs_up",
            "source_name": "zulip",
            "source": {
                "kind": "zulip",
                "stream_id": 17,
                "server_url": "https://zulip.example.test",
                "topic_name": "general",
                "message_id": 42,
            },
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="message_reaction",
            action="created",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual("message_reaction", event["object_type"])
        self.assertEqual("created", event["action"])
        self.assertEqual(payload, event["payload"])

    def test_event_row_to_messenger_event_preserves_delete_payload(self):
        topic_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "topic.deleted",
            "uuid": str(topic_uuid),
            "stream_uuid": str(stream_uuid),
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="topic",
            action="deleted",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual("topic", event["object_type"])
        self.assertEqual("deleted", event["action"])
        self.assertEqual(payload, event["payload"])
        self.assertNotIn("stream_uuid", event)
        self.assertNotIn("topic", event)

    def test_event_row_to_messenger_event_preserves_stream_binding_items(self):
        stream_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        binding_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "stream_bindings.created",
            "uuid": str(stream_uuid),
            "items": [
                {
                    "uuid": str(binding_uuid),
                    "project_id": str(sys_uuid.uuid4()),
                    "stream_uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "who_uuid": str(sys_uuid.uuid4()),
                    "role": "member",
                    "notification_mode": "mentions_only",
                    "created_at": "2026-06-24T10:00:00.000000Z",
                    "updated_at": "2026-06-24T10:00:00.000000Z",
                }
            ],
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="stream_binding",
            action="created",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual("stream_binding", event["object_type"])
        self.assertEqual("created", event["action"])
        self.assertEqual(str(stream_uuid), event["payload"]["uuid"])
        self.assertEqual(str(user_uuid), event["payload"]["items"][0]["user_uuid"])
        self.assertNotIn("stream_bindings", event["payload"])

    def test_event_row_to_messenger_event_preserves_legacy_messages_read(self):
        message_uuid_1 = sys_uuid.uuid4()
        message_uuid_2 = sys_uuid.uuid4()
        payload = {
            "kind": "messages.read",
            "project_id": str(sys_uuid.uuid4()),
            "message_uuids": [str(message_uuid_1), str(message_uuid_2)],
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="message",
            action="read",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual("message", event["object_type"])
        self.assertEqual("read", event["action"])
        self.assertEqual(payload, event["payload"])

    def test_event_row_to_messenger_event_preserves_user_payload(self):
        user_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "user.updated",
            "uuid": str(user_uuid),
            "username": "john",
            "source": "iam",
            "status": "active",
            "status_emoji": "coffee",
            "status_text": "Focusing",
            "first_name": "John",
            "last_name": "Smith",
            "email": "john@example.com",
            "avatar": models.build_workspace_user_gravatar_avatar(
                "john@example.com",
            ),
            "last_ping_at": "2026-06-24T10:00:00.000000Z",
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:05:00.000000Z",
        }
        row = self._workspace_event_row(
            payload=payload,
            object_type="user",
            action="updated",
        )

        event = events.event_row_to_messenger_event(row)

        self.assertEqual("user", event["object_type"])
        self.assertEqual("updated", event["action"])
        self.assertEqual(payload, event["payload"])

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
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.created_at),
        )
        self.assertEqual({}, payload.reactions)

    def test_messages_read_event_payload_accepts_message_ids(self):
        project_id = sys_uuid.uuid4()
        message_uuid_1 = sys_uuid.uuid4()
        message_uuid_2 = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "messages.read",
                "project_id": str(project_id),
                "message_uuids": [
                    str(message_uuid_1),
                    str(message_uuid_2),
                ],
            }
        )

        self.assertIsInstance(
            payload,
            event_payloads.MessagesReadEventPayload,
        )
        self.assertEqual(project_id, payload.project_id)
        self.assertEqual(
            [str(message_uuid_1), str(message_uuid_2)],
            payload.message_uuids,
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
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.created_at),
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
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.updated_at),
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
                "color": 1122867,
                "last_message_uuid": None,
                "source_name": "native",
                "source": {"kind": "native"},
                "invite_only": False,
                "announce": False,
                "private": False,
                "provider": None,
                "delivery": None,
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:34.166369",
            }
        )

        self.assertEqual("Engineering", payload.name)
        self.assertIsNone(payload.provider)
        self.assertIsNone(payload.delivery)
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.created_at),
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
                "uuid": str(stream_uuid),
                "items": [
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

        self.assertEqual(stream_uuid, payload.uuid)
        self.assertEqual(str(user_uuid), payload["items"][0]["user_uuid"])

    def test_user_updated_event_payload_accepts_postgres_json_timestamp(self):
        user_uuid = sys_uuid.uuid4()

        payload = event_payloads.WORKSPACE_EVENT_PAYLOAD_TYPE.from_simple_type(
            {
                "kind": "user.updated",
                "uuid": str(user_uuid),
                "username": "john",
                "source": "iam",
                "status": "active",
                "status_emoji": "coffee",
                "status_text": "Focusing",
                "first_name": "John",
                "last_name": "Smith",
                "email": "john@example.com",
                "avatar": models.build_workspace_user_gravatar_avatar(
                    "john@example.com",
                ),
                "last_ping_at": "2026-06-24T22:28:40.166369",
                "created_at": "2026-06-24T22:28:34.166369",
                "updated_at": "2026-06-24T22:28:40.166369",
            }
        )

        self.assertIsInstance(payload, event_payloads.UserUpdatedEventPayload)
        self.assertEqual(user_uuid, payload.uuid)
        self.assertEqual("active", payload.status)
        self.assertEqual(
            "2026-06-24T22:28:40.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(
                payload.last_ping_at
            ),
        )
        self.assertEqual(
            "2026-06-24T22:28:40.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.updated_at),
        )

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
                "color": 1193046,
                "last_message_uuid": None,
                "unread_count": 0,
                "is_default": False,
                "is_done": False,
                "notification_mode": "default",
                "provider": {
                    "uuid": str(sys_uuid.uuid4()),
                    "name": "Zulip Main",
                    "kind": "zulip",
                },
                "delivery": {
                    "status": "delivered",
                    "safe_error": None,
                    "updated_at": "2026-06-24T22:28:34.166369Z",
                },
            }
        )

        self.assertEqual("Planning", payload.name)
        self.assertEqual("default", payload.notification_mode)
        self.assertEqual("Zulip Main", payload.provider["name"])
        self.assertEqual("delivered", payload.delivery["status"])
        self.assertEqual(
            "2026-06-24T22:28:34.166369Z",
            event_payloads.MESSAGE_EVENT_TIMESTAMP_TYPE.dump_value(payload.created_at),
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

    def _capture_workspace_events(self, callback):
        created_events = []

        class FakeWorkspaceEvent:
            def __init__(self, **kwargs):
                created_events.append(kwargs)

            def insert(self, session=None):
                created_events[-1]["insert_session"] = session
                return len(created_events)

        with mock.patch.object(events.models, "WorkspaceEvent", FakeWorkspaceEvent):
            result = callback()
        return result, created_events

    def _assert_created_event_contract(self, event, object_type, action, kind):
        self.assertEqual(models.WORKSPACE_EVENT_SCHEMA_VERSION, event["schema_version"])
        self.assertEqual(object_type, event["object_type"])
        self.assertEqual(action, event["action"])
        self.assertEqual(kind, event["payload"]["kind"])

    def test_workspace_event_insert_omits_generated_epoch_version(self):
        user_uuid = sys_uuid.uuid4()
        project_id = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        payload = {
            "kind": "folder.created",
            "uuid": str(folder_uuid),
            "user_uuid": str(user_uuid),
            "project_id": str(project_id),
            "title": "Inbox",
            "background_color_value": None,
            "unread_count": 0,
            "system_type": "created",
            "folder_items": [],
        }
        event = models.WorkspaceEvent(
            schema_version=models.WORKSPACE_EVENT_SCHEMA_VERSION,
            uuid=sys_uuid.uuid4(),
            user_uuid=user_uuid,
            project_id=project_id,
            object_type="folder",
            action="created",
            payload=payload,
        )
        session = mock.MagicMock()
        session.execute.return_value.fetchone.return_value = {"epoch_version": 42}
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

    def test_create_message_updated_event_uses_flat_user_message_snapshot(self):
        project_id = sys_uuid.uuid4()
        author_uuid = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        updated_at = datetime.datetime(
            2026, 6, 24, 10, 5, 0, tzinfo=datetime.timezone.utc
        )
        user_message = models.WorkspaceUserMessage(
            uuid=message_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            author_uuid=author_uuid,
            payload=message_payloads.MarkdownPayload(content="edited"),
            read=True,
            pinned=False,
            starred=False,
            is_own=False,
            created_at=created_at,
            updated_at=updated_at,
        )
        session = object()

        with mock.patch.object(
            events.models.WorkspaceMessage,
            "objects",
            types.SimpleNamespace(
                get_one=mock.Mock(
                    return_value=types.SimpleNamespace(provider_uuid=None),
                ),
            ),
        ):
            result, created_events = self._capture_workspace_events(
                lambda: events.create_message_updated_event(
                    message=user_message,
                    session=session,
                )
            )

        created_event = created_events[0]
        self.assertEqual(1, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(user_uuid, created_event["user_uuid"])
        self._assert_created_event_contract(
            created_event,
            "message",
            "updated",
            "message.updated",
        )
        self.assertEqual("edited", created_event["payload"]["payload"]["content"])
        self.assertEqual(True, created_event["payload"]["read"])
        self.assertEqual({}, created_event["payload"]["reactions"])
        self.assertEqual(
            "2026-06-24T10:05:00.000000Z",
            created_event["payload"]["updated_at"],
        )

    def test_create_messages_read_event_uses_legacy_payload(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        message_uuid_1 = sys_uuid.uuid4()
        message_uuid_2 = sys_uuid.uuid4()
        session = object()

        result, created_events = self._capture_workspace_events(
            lambda: events.create_messages_read_event(
                project_id=project_id,
                user_uuid=user_uuid,
                message_uuids=[message_uuid_1, message_uuid_2],
                session=session,
            )
        )

        created_event = created_events[0]
        self.assertEqual(1, result)
        self.assertIs(session, created_event["insert_session"])
        self._assert_created_event_contract(
            created_event,
            "message",
            "read",
            "messages.read",
        )
        self.assertEqual(str(project_id), created_event["payload"]["project_id"])
        self.assertEqual(
            [str(message_uuid_1), str(message_uuid_2)],
            created_event["payload"]["message_uuids"],
        )

    def test_create_folder_stream_topic_and_read_events_use_flat_payload(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        owner_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        created_at = datetime.datetime(
            2026, 6, 24, 10, 0, 0, tzinfo=datetime.timezone.utc
        )
        folder = models.UserFolder(
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
        stream = models.WorkspaceUserStream(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            name="Engineering",
            description="Engineering workspace",
            project_id=project_id,
            owner=owner_uuid,
            role="member",
            notification_mode="all_messages",
            unread_count=0,
            color=1122867,
            source_name="native",
            source=models.NativeSource(),
            invite_only=False,
            announce=False,
            private=False,
            is_archived=False,
            created_at=created_at,
            updated_at=created_at,
        )
        topic = models.WorkspaceUserTopic(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            created_at=created_at,
            updated_at=created_at,
            name="Planning",
            stream_uuid=stream_uuid,
            color=1193046,
            unread_count=0,
            is_default=False,
            is_done=False,
            notification_mode="default",
        )

        native_resource = types.SimpleNamespace(provider_uuid=None)
        with (
            mock.patch.object(
                events.models.WorkspaceStream,
                "objects",
                types.SimpleNamespace(get_one=mock.Mock(return_value=native_resource)),
            ),
            mock.patch.object(
                events.models.WorkspaceStreamTopic,
                "objects",
                types.SimpleNamespace(get_one=mock.Mock(return_value=native_resource)),
            ),
        ):
            _result, created_events = self._capture_workspace_events(
                lambda: [
                    events.create_folder_event(folder=folder),
                    events.create_stream_event(stream=stream),
                    events.create_stream_read_event(stream=stream),
                    events.create_topic_event(topic=topic),
                    events.create_topic_read_event(topic=topic),
                ]
            )

        self._assert_created_event_contract(
            created_events[0], "folder", "created", "folder.created"
        )
        self.assertEqual("Inbox", created_events[0]["payload"]["title"])
        self._assert_created_event_contract(
            created_events[1], "stream", "created", "stream.created"
        )
        self.assertEqual("Engineering", created_events[1]["payload"]["name"])
        self.assertEqual({"kind": "native"}, created_events[1]["payload"]["source"])
        self.assertIsNone(created_events[1]["payload"]["provider"])
        self.assertIsNone(created_events[1]["payload"]["delivery"])
        self._assert_created_event_contract(
            created_events[2], "stream", "read", "stream.read"
        )
        self._assert_created_event_contract(
            created_events[3], "topic", "created", "topic.created"
        )
        self.assertEqual("Planning", created_events[3]["payload"]["name"])
        self.assertIsNone(created_events[3]["payload"]["provider"])
        self.assertIsNone(created_events[3]["payload"]["delivery"])
        self._assert_created_event_contract(
            created_events[4], "topic", "read", "topic.read"
        )

    def test_provider_stream_topic_and_reaction_events_match_rest_metadata(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        provider_uuid = sys_uuid.uuid4()
        account_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        updated_at = datetime.datetime(
            2026, 6, 24, 10, 5, 0, tzinfo=datetime.timezone.utc
        )
        provider = types.SimpleNamespace(
            uuid=provider_uuid,
            name="Zulip Main",
        )
        account = types.SimpleNamespace(account_type="zulip")
        provider_resource = types.SimpleNamespace(
            provider_uuid=provider_uuid,
            external_account_uuid=account_uuid,
            delivery_status="delivered",
            delivery_error=None,
            delivery_updated_at=updated_at,
        )
        stream = models.WorkspaceUserStream(
            uuid=stream_uuid,
            user_uuid=user_uuid,
            name="Engineering",
            description="Engineering workspace",
            project_id=project_id,
            owner=user_uuid,
            role="member",
            notification_mode="all_messages",
            source_name="zulip",
            source=models.ZulipSource(
                stream_id=17,
                server_url="https://zulip.example.test",
            ),
        )
        topic = models.WorkspaceUserTopic(
            uuid=topic_uuid,
            user_uuid=user_uuid,
            project_id=project_id,
            name="Planning",
            stream_uuid=stream_uuid,
        )
        reaction = types.SimpleNamespace(
            uuid=sys_uuid.uuid4(),
            project_id=project_id,
            message_uuid=message_uuid,
            user_uuid=user_uuid,
            emoji_name="thumbs_up",
            **vars(provider_resource),
        )
        message = types.SimpleNamespace(
            source_name="zulip",
            source=models.ZulipSource(
                stream_id=17,
                server_url="https://zulip.example.test",
                topic_name="Planning",
                message_id=42,
            ),
        )
        session = object()

        with (
            mock.patch.object(
                events.models.WorkspaceStream,
                "objects",
                types.SimpleNamespace(
                    get_one=mock.Mock(return_value=provider_resource),
                ),
            ),
            mock.patch.object(
                events.models.WorkspaceStreamTopic,
                "objects",
                types.SimpleNamespace(
                    get_one=mock.Mock(return_value=provider_resource),
                ),
            ),
            mock.patch.object(
                events.provider_payloads.provider_models.WorkspaceProvider,
                "objects",
                types.SimpleNamespace(get_one=mock.Mock(return_value=provider)),
            ),
            mock.patch.object(
                events.provider_payloads.messenger_models.ExternalAccount,
                "objects",
                types.SimpleNamespace(get_one=mock.Mock(return_value=account)),
            ),
        ):
            _result, created_events = self._capture_workspace_events(
                lambda: [
                    events.create_stream_event(stream, session=session),
                    events.create_topic_event(topic, session=session),
                    events.create_message_reaction_created_event(
                        reaction,
                        message,
                        session=session,
                    ),
                ]
            )

        expected_provider = {
            "uuid": str(provider_uuid),
            "name": "Zulip Main",
            "kind": "zulip",
        }
        expected_delivery = {
            "status": "delivered",
            "safe_error": None,
            "updated_at": "2026-06-24T10:05:00Z",
        }
        for created_event in created_events:
            self.assertEqual(expected_provider, created_event["payload"]["provider"])
            self.assertEqual(expected_delivery, created_event["payload"]["delivery"])

    def test_create_stream_bindings_created_event_uses_items_payload(self):
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
        bindings = [
            models.WorkspaceStreamBinding(
                uuid=binding_uuid,
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=user_uuid,
                who_uuid=owner_uuid,
                role="member",
                notification_mode="mentions_only",
                created_at=created_at,
                updated_at=created_at,
            ),
            models.WorkspaceStreamBinding(
                uuid=second_binding_uuid,
                project_id=project_id,
                stream_uuid=stream_uuid,
                user_uuid=second_user_uuid,
                who_uuid=owner_uuid,
                role="owner",
                notification_mode="all_messages",
                created_at=created_at,
                updated_at=created_at,
            ),
        ]
        session = object()

        result, created_events = self._capture_workspace_events(
            lambda: events.create_stream_bindings_created_event(
                bindings=bindings,
                user_uuid=recipient_uuid,
                session=session,
            )
        )

        created_event = created_events[0]
        self.assertEqual(1, result)
        self.assertIs(session, created_event["insert_session"])
        self.assertEqual(project_id, created_event["project_id"])
        self.assertEqual(recipient_uuid, created_event["user_uuid"])
        self._assert_created_event_contract(
            created_event,
            "stream_binding",
            "created",
            "stream_bindings.created",
        )
        self.assertEqual(str(stream_uuid), created_event["payload"]["uuid"])
        self.assertNotIn("stream_bindings", created_event["payload"])
        self.assertEqual(
            [str(user_uuid), str(second_user_uuid)],
            [binding["user_uuid"] for binding in created_event["payload"]["items"]],
        )
        self.assertEqual(
            "2026-06-24T10:00:00.000000Z",
            created_event["payload"]["items"][0]["created_at"],
        )

    def test_create_delete_events_use_expected_payload(self):
        project_id = sys_uuid.uuid4()
        user_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        folder_uuid = sys_uuid.uuid4()
        item_uuid = sys_uuid.uuid4()

        _result, created_events = self._capture_workspace_events(
            lambda: [
                events.create_stream_deleted_event(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=models.NativeSource(),
                ),
                events.create_topic_deleted_event(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    topic_uuid=topic_uuid,
                    stream_uuid=stream_uuid,
                    source_name="native",
                    source=models.NativeSource(),
                ),
                events.create_message_deleted_event(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    message_uuid=message_uuid,
                    stream_uuid=stream_uuid,
                    topic_uuid=topic_uuid,
                    author_uuid=user_uuid,
                    source_name="zulip",
                    source=models.ZulipSource(
                        stream_id=3,
                        server_url="https://zulip.example.com",
                        topic_name="deploys",
                        message_id=12345,
                    ),
                ),
                events.create_folder_deleted_event(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    folder_uuid=folder_uuid,
                ),
                events.create_folder_item_deleted_event(
                    project_id=project_id,
                    user_uuid=user_uuid,
                    item_uuid=item_uuid,
                ),
            ]
        )

        self._assert_created_event_contract(
            created_events[0], "stream", "deleted", "stream.deleted"
        )
        self.assertEqual(
            {
                "kind": "stream.deleted",
                "uuid": str(stream_uuid),
                "source_name": "native",
                "source": {"kind": "native"},
            },
            created_events[0]["payload"],
        )
        self._assert_created_event_contract(
            created_events[1], "topic", "deleted", "topic.deleted"
        )
        self.assertEqual(str(topic_uuid), created_events[1]["payload"]["uuid"])
        self.assertEqual(str(stream_uuid), created_events[1]["payload"]["stream_uuid"])
        self.assertEqual("native", created_events[1]["payload"]["source_name"])
        self.assertEqual({"kind": "native"}, created_events[1]["payload"]["source"])
        self._assert_created_event_contract(
            created_events[2], "message", "deleted", "message.deleted"
        )
        self.assertEqual(str(message_uuid), created_events[2]["payload"]["uuid"])
        self.assertEqual(str(topic_uuid), created_events[2]["payload"]["topic_uuid"])
        self.assertEqual(str(user_uuid), created_events[2]["payload"]["author_uuid"])
        self.assertEqual("zulip", created_events[2]["payload"]["source_name"])
        self.assertEqual(
            {
                "kind": "zulip",
                "stream_id": 3,
                "server_url": "https://zulip.example.com",
                "topic_name": "deploys",
                "message_id": 12345,
            },
            created_events[2]["payload"]["source"],
        )
        self._assert_created_event_contract(
            created_events[3], "folder", "deleted", "folder.deleted"
        )
        self.assertEqual(
            {"kind": "folder.deleted", "uuid": str(folder_uuid)},
            created_events[3]["payload"],
        )
        self._assert_created_event_contract(
            created_events[4], "folder_item", "deleted", "folder_item.deleted"
        )
        self.assertEqual(
            {"kind": "folder_item.deleted", "uuid": str(item_uuid)},
            created_events[4]["payload"],
        )

    def test_websocket_send_event_uses_flat_payload_without_envelope(self):
        websockets_stub = types.ModuleType("websockets")
        websockets_stub.serve = None
        sys.modules.setdefault("websockets", websockets_stub)
        websocket_service = importlib.import_module(
            "workspace.messenger_api.websocket_service"
        )

        sent_messages = []

        class SendingWebsocket(FakeWebsocket):
            async def send(self, message):
                sent_messages.append(message)

        server = websocket_service.MessengerEventsWebsocketServer(
            db_url="postgresql://example",
            iam_engine_driver=None,
            heartbeat_interval=30,
            client_timeout=30,
            catchup_limit=500,
            send_queue_limit=100,
        )
        connection = websocket_service.ClientConnection(
            websocket=SendingWebsocket([]),
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            last_epoch_version=7,
        )
        event = {
            "schema_version": 1,
            "uuid": str(sys_uuid.uuid4()),
            "epoch_version": 9,
            "project_id": str(connection.project_id),
            "user_uuid": str(connection.user_uuid),
            "object_type": "message",
            "action": "created",
            "created_at": "2026-06-24T10:00:00.000000Z",
            "updated_at": "2026-06-24T10:00:00.000000Z",
            "payload": {"kind": "message.created", "uuid": str(sys_uuid.uuid4())},
        }

        asyncio.run(server._send_event(connection, event))

        self.assertEqual(event, json.loads(sent_messages[0]))
        self.assertEqual(9, connection.last_epoch_version)
        self.assertNotIn("event", json.loads(sent_messages[0]))
        self.assertNotIn("type", json.loads(sent_messages[0]))

    def test_websocket_server_uses_configured_heartbeat_interval(self):
        websockets_stub = types.ModuleType("websockets")
        websockets_stub.serve = None
        sys.modules.setdefault("websockets", websockets_stub)
        websocket_service = importlib.import_module(
            "workspace.messenger_api.websocket_service"
        )
        serve_kwargs = {}

        server = websocket_service.MessengerEventsWebsocketServer(
            db_url="postgresql://example",
            iam_engine_driver=None,
            heartbeat_interval=25,
            client_timeout=30,
            catchup_limit=500,
            send_queue_limit=100,
        )

        class ServeContext:
            async def __aenter__(self):
                server.stop()

            async def __aexit__(self, exc_type, exc_value, traceback):
                pass

        def serve(*args, **kwargs):
            serve_kwargs.update(kwargs)
            return ServeContext()

        with mock.patch.object(
            websocket_service.websockets, "serve", side_effect=serve
        ):
            asyncio.run(server.serve("127.0.0.1", 21082))

        self.assertEqual(25, serve_kwargs["ping_interval"])

    def test_websocket_consumer_ignores_client_frames(self):
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
                    json.dumps({"type": "pong"}),
                    json.dumps({"type": "ack", "epoch_version": 9}),
                ]
            ),
            project_id=sys_uuid.uuid4(),
            user_uuid=sys_uuid.uuid4(),
            last_epoch_version=7,
        )

        asyncio.run(server._consume_client_frames(connection))

        self.assertEqual(7, connection.last_epoch_version)


if __name__ == "__main__":
    unittest.main()
