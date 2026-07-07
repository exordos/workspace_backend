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
import threading

from workspace.common.clients import zulip as zulip_client


class AddMessage:
    def __init__(self, external_account, message):
        self.external_account = external_account
        self.message = message

    def _get_stream_info(self):
        if 'stream_id' not in self.message:
            print(self.message)
        return {
            "type": self.message["type"],
            "stream_id": self.message["stream_id"],
            "display_recipient": self.message["display_recipient"],
            "description": "",
            "creator_id": self.message["sender_id"],
            "timestamp": datetime.datetime.fromtimestamp(
                self.message["timestamp"],
                tz=datetime.timezone.utc,
            ),
            "invite_only": False,
            "announce": False,
            "is_archived": False,
            "subscriber_ids": [self.message["sender_id"]],
        }

    def execute(self, cache):
        return cache.get_or_create_stream(
            external_account=self.external_account,
            stream_info=self._get_stream_info(),
        )


class AddStream:
    def __init__(self, external_account, stream, subscriber_ids=None):
        self.external_account = external_account
        self.stream = stream
        self.subscriber_ids = subscriber_ids or []

    def _get_creator_id(self):
        creator_id = self.stream["creator_id"]
        if creator_id is None:
            return self.external_account.account_settings.user_info.user_id
        return creator_id

    def _get_stream_info(self):
        return {
            "type": "stream",
            "stream_id": self.stream["stream_id"],
            "display_recipient": self.stream["name"],
            "description": self.stream["description"],
            "creator_id": self._get_creator_id(),
            "created_at": datetime.datetime.fromtimestamp(
                self.stream["date_created"],
                tz=datetime.timezone.utc,
            ),
            "invite_only": self.stream["invite_only"],
            "announce": self.stream["is_announcement_only"],
            "is_archived": self.stream["is_archived"],
            "subscriber_ids": self.subscriber_ids,
        }

    def execute(self, cache):
        return cache.get_or_create_stream(
            external_account=self.external_account,
            stream_info=self._get_stream_info(),
        )


class ZulipBridgeWorker(threading.Thread):
    DEFAULT_MESSAGE_FILTERS = {
        "anchor": 0,
        "num_before": 0,
        "num_after": 100,
    }

    def __init__(
        self,
        external_account,
        sync_queue,
        client_cls=zulip_client.ZulipClient,
    ):
        super().__init__()
        self._external_account = external_account
        self._sync_queue = sync_queue
        self.client_cls = client_cls

    def fetch_messages(self, message_filters):
        message_filters = dict(message_filters)
        message_filters["apply_markdown"] = False
        credentials = self._external_account.account_settings.credentials
        client = self.client_cls(endpoint=self._external_account.server_url)
        return client.get_messages_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_filters=message_filters,
        )

    def fetch_streams(self):
        credentials = self._external_account.account_settings.credentials
        client = self.client_cls(endpoint=self._external_account.server_url)
        return client.get_streams_with_api_key(
            login=credentials.login,
            token=credentials.token,
        )

    def fetch_stream_subscribers(self, stream):
        credentials = self._external_account.account_settings.credentials
        client = self.client_cls(endpoint=self._external_account.server_url)
        return client.get_stream_subscribers_with_api_key(
            login=credentials.login,
            token=credentials.token,
            stream_id=stream["stream_id"],
        )

    def _process_stream(self, stream, subscriber_ids=None):
        self._sync_queue.put(
            AddStream(
                external_account=self._external_account,
                stream=stream,
                subscriber_ids=subscriber_ids,
            ),
        )

    def _process_message(self, message):
        self._sync_queue.put(
            AddMessage(
                external_account=self._external_account,
                message=message,
            ),
        )

    def run(self):
        message_filters = dict(self.DEFAULT_MESSAGE_FILTERS)
        last_message_id = message_filters["anchor"]

        for stream in self.fetch_streams():
            subscriber_ids = self.fetch_stream_subscribers(stream=stream)
            self._process_stream(
                stream,
                subscriber_ids=subscriber_ids,
            )

        while True:
            messages = self.fetch_messages(message_filters=message_filters)
            if not messages:
                break

            seen_message = False
            for message in messages:
                message_id = message["id"]
                if message_id <= last_message_id:
                    continue
                if message["sender_id"] >= 8:
                    self._process_message(message)
                last_message_id = message_id
                message_filters["anchor"] = last_message_id
                seen_message = True

            if not seen_message:
                break
