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
import itertools
import logging
import threading

from workspace.common.clients import zulip as zulip_client


LOG = logging.getLogger(__name__)
ZULIP_PRIVATE_TOPIC_NAME = "zulip"
MIN_ZULIP_USER_ID = 8
SYNC_RESPONSE_PRIORITY_STREAM = 0
SYNC_RESPONSE_PRIORITY_STREAM_FINISHED = 1
SYNC_RESPONSE_PRIORITY_MESSAGE = 10
SYNC_RESPONSE_PRIORITY_DEFAULT = SYNC_RESPONSE_PRIORITY_MESSAGE
_SYNC_RESPONSE_SEQUENCE = itertools.count()


class PrioritizedSyncResponse:
    def __init__(self, priority, command):
        self.priority = priority
        self.sequence = next(_SYNC_RESPONSE_SEQUENCE)
        self.command = command

    def __lt__(self, other):
        return (
            self.priority,
            self.sequence,
        ) < (
            other.priority,
            other.sequence,
        )


def put_sync_response(output_queue, command):
    output_queue.put(
        PrioritizedSyncResponse(
            priority=getattr(
                command,
                "priority",
                SYNC_RESPONSE_PRIORITY_DEFAULT,
            ),
            command=command,
        ),
    )


def get_sync_response_command(response):
    if isinstance(response, PrioritizedSyncResponse):
        return response.command
    return response


class StopWorker:
    def execute(self, worker):
        worker.stop()


class SyncStreams:
    def __init__(self, event_owner):
        self.event_owner = event_owner

    def execute(self, worker):
        try:
            worker.sync_streams()
        finally:
            put_sync_response(
                worker.output_queue,
                SyncStreamsFinished(event_owner=self.event_owner),
            )


class SyncStreamsFinished:
    priority = SYNC_RESPONSE_PRIORITY_STREAM_FINISHED

    def __init__(self, event_owner):
        self.event_owner = event_owner

    def execute(self, cache):
        return None


class SyncMessages:
    def __init__(
        self,
        initial_message_anchor=0,
        on_message_anchor_update=None,
        on_finished=None,
    ):
        self.initial_message_anchor = initial_message_anchor
        self.on_message_anchor_update = on_message_anchor_update
        self.on_finished = on_finished

    def execute(self, worker):
        try:
            worker.sync_messages(
                initial_message_anchor=self.initial_message_anchor,
                on_message_anchor_update=self.on_message_anchor_update,
            )
        finally:
            if self.on_finished is not None:
                self.on_finished()


def get_event_owner(external_account):
    return (
        external_account.project_id,
        external_account.server_url,
        external_account.user_uuid,
    )


class AddMessage:
    priority = SYNC_RESPONSE_PRIORITY_MESSAGE

    def __init__(self, external_account, message):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.message = message

    def _get_timestamp(self):
        return datetime.datetime.fromtimestamp(
            self.message["timestamp"],
            tz=datetime.timezone.utc,
        )

    def _get_private_display_recipient(self):
        return ", ".join(
            recipient["full_name"]
            for recipient in self.message["display_recipient"]
        )

    def _get_private_subscriber_ids(self):
        return [
            recipient["id"]
            for recipient in self.message["display_recipient"]
        ]

    def _get_private_stream_info(self):
        return {
            "type": self.message["type"],
            "stream_id": self.message["recipient_id"],
            "display_recipient": self._get_private_display_recipient(),
            "description": "",
            "creator_id": (
                self.external_account.account_settings.user_info.user_id
            ),
            "timestamp": self._get_timestamp(),
            "invite_only": True,
            "announce": False,
            "is_archived": False,
            "subscriber_ids": self._get_private_subscriber_ids(),
            "default_topic_name": ZULIP_PRIVATE_TOPIC_NAME,
            "event_type": "message",
        }

    def _get_stream_info(self):
        if self.message["type"] == "private":
            return self._get_private_stream_info()

        return {
            "type": self.message["type"],
            "stream_id": self.message["stream_id"],
            "display_recipient": self.message["display_recipient"],
            "description": "",
            "creator_id": self.message["sender_id"],
            "timestamp": self._get_timestamp(),
            "invite_only": False,
            "announce": False,
            "is_archived": False,
            "subscriber_ids": [self.message["sender_id"]],
            "event_type": "message",
        }

    def _get_topic_name(self):
        if self.message["type"] == "private":
            return ZULIP_PRIVATE_TOPIC_NAME
        return self.message["subject"]

    def _get_message_info(self):
        timestamp = self._get_timestamp()
        return {
            "message_id": self.message["id"],
            "sender_id": self.message["sender_id"],
            "content": self.message["content"],
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def execute(self, cache):
        stream_info = self._get_stream_info()
        stream = cache.get_or_create_stream(
            external_account=self.external_account,
            stream_info=stream_info,
        )
        topic = cache.get_or_create_topic(
            external_account=self.external_account,
            stream=stream,
            stream_info=stream_info,
            topic_name=self._get_topic_name(),
        )
        return cache.get_or_create_message(
            external_account=self.external_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name=self._get_topic_name(),
            message_info=self._get_message_info(),
        )


class AddStream:
    priority = SYNC_RESPONSE_PRIORITY_STREAM

    def __init__(self, external_account, stream, subscriber_ids=None):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
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
            "event_type": "stream",
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
        input_queue,
        output_queue,
        client_cls=zulip_client.ZulipClient,
    ):
        super().__init__()
        self._external_account = external_account
        self._input_queue = input_queue
        self._output_queue = output_queue
        self.client_cls = client_cls
        self._stopped = False

    @property
    def output_queue(self):
        return self._output_queue

    def stop(self):
        self._stopped = True

    def sync_streams(self):
        for stream in self.fetch_streams():
            subscriber_ids = self.fetch_stream_subscribers(stream=stream)
            self._process_stream(
                stream,
                subscriber_ids=subscriber_ids,
            )

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
        put_sync_response(
            self._output_queue,
            AddStream(
                external_account=self._external_account,
                stream=stream,
                subscriber_ids=subscriber_ids,
            ),
        )

    def _should_process_message(self, message):
        if message["sender_id"] >= MIN_ZULIP_USER_ID:
            return True
        LOG.debug(
            "Skip Zulip system message %s from user %s",
            message["id"],
            message["sender_id"],
        )
        return False

    def _process_message(self, message):
        if not self._should_process_message(message):
            return
        put_sync_response(
            self._output_queue,
            AddMessage(
                external_account=self._external_account,
                message=message,
            ),
        )

    def sync_messages(
        self,
        initial_message_anchor=0,
        on_message_anchor_update=None,
    ):
        message_filters = dict(self.DEFAULT_MESSAGE_FILTERS)
        message_filters["anchor"] = initial_message_anchor
        last_message_id = message_filters["anchor"]

        while True:
            messages = self.fetch_messages(message_filters=message_filters)
            if not messages:
                break

            seen_message = False
            for message in messages:
                message_id = message["id"]
                if message_id <= last_message_id:
                    continue
                self._process_message(message)
                last_message_id = message_id
                if on_message_anchor_update is not None:
                    on_message_anchor_update(last_message_id)
                message_filters["anchor"] = last_message_id
                seen_message = True

            if not seen_message:
                break

    def run(self):
        while not self._stopped:
            command = self._input_queue.get()
            try:
                command.execute(self)
            except Exception:
                LOG.exception("Unexpected Zulip bridge worker command error")
