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

import unittest
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import controllers
from workspace.messenger_api import events
from workspace.messenger_api import websocket_protocol


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

    def test_event_row_to_messenger_event_derives_message_flags(self):
        author_uuid = sys_uuid.uuid4()
        recipient_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        event = events.event_row_to_messenger_event(
            {
                "epoch_version": 7,
                "user_uuid": recipient_uuid,
                "payload": {
                    "kind": "message.created",
                    "uuid": str(message_uuid),
                    "stream_uuid": str(sys_uuid.uuid4()),
                    "topic_uuid": str(sys_uuid.uuid4()),
                    "author_uuid": str(author_uuid),
                    "payload": {
                        "kind": "markdown",
                        "content": "hello",
                    },
                    "created_at": "2026-06-24 10:00:00.000000",
                    "updated_at": "2026-06-24 10:00:00.000000",
                },
            }
        )

        self.assertEqual(7, event["epoch_version"])
        self.assertEqual("message", event["type"])
        self.assertEqual(str(message_uuid), event["message"]["id"])
        self.assertFalse(event["message"]["is_own"])
        self.assertFalse(event["message"]["read"])
        self.assertEqual([], event["message"]["flags"])


if __name__ == "__main__":
    unittest.main()
