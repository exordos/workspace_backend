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
import queue
import re
import threading
import urllib.parse
import uuid as sys_uuid

from workspace.common.clients import zulip as zulip_client
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import helpers as messenger_dm_helpers


LOG = logging.getLogger(__name__)
ZULIP_PRIVATE_TOPIC_NAME = "zulip"
MIN_ZULIP_USER_ID = 8
SYNC_RESPONSE_PRIORITY_STREAM = 0
SYNC_RESPONSE_PRIORITY_STREAM_FINISHED = 1
SYNC_RESPONSE_PRIORITY_SYNC_STARTED = 5
SYNC_RESPONSE_PRIORITY_OUTBOUND = 6
SYNC_RESPONSE_PRIORITY_MESSAGE = 10
SYNC_RESPONSE_PRIORITY_STATE = 20
SYNC_RESPONSE_PRIORITY_SYNC_FINISHED = 30
SYNC_RESPONSE_PRIORITY_DEFAULT = SYNC_RESPONSE_PRIORITY_MESSAGE
MAX_SYNC_QUEUE_SIZE = 1000
SYNC_QUEUE_PUT_TIMEOUT = 1
_SYNC_RESPONSE_SEQUENCE = itertools.count()
NO_VALUE = object()
WORKSPACE_FILE_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<name>[^\]]*)\]\((?P<url>[^)\s]+)\)"
)
WORKSPACE_FILE_URN_TYPES = {
    "file",
    "image",
    "video",
}


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
    response = PrioritizedSyncResponse(
        priority=getattr(
            command,
            "priority",
            SYNC_RESPONSE_PRIORITY_DEFAULT,
        ),
        command=command,
    )
    while True:
        try:
            output_queue.put(response, timeout=SYNC_QUEUE_PUT_TIMEOUT)
            return
        except queue.Full:
            LOG.warning(
                "Zulip sync output queue is full, waiting to enqueue %s",
                command.__class__.__name__,
            )


def get_sync_response_command(response):
    if isinstance(response, PrioritizedSyncResponse):
        return response.command
    return response


class StopWorker:
    def execute(self, worker):
        worker.stop()


class BadZulipEventQueue(Exception):
    pass


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
        queue_id,
        last_event_id=-1,
        last_message_id=0,
        is_synced=False,
        on_finished=None,
    ):
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.last_message_id = last_message_id
        self.is_synced = is_synced
        self.on_finished = on_finished

    def execute(self, worker):
        try:
            worker.sync_messages(
                queue_id=self.queue_id,
                last_event_id=self.last_event_id,
                last_message_id=self.last_message_id,
                is_synced=self.is_synced,
            )
        finally:
            if self.on_finished is not None:
                self.on_finished()


class CreateZulipQueueAndFetchMessages:
    def __init__(
        self,
        last_message_id=0,
        on_finished=None,
    ):
        self.last_message_id = last_message_id
        self.on_finished = on_finished

    def execute(self, worker):
        try:
            worker.create_queue_and_fetch_messages(
                last_message_id=self.last_message_id,
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

    def __init__(self, external_account, message, event_id=None):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.message = message
        self.event_id = event_id

    @property
    def last_message_id(self):
        return self.message["id"]

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

    def _get_stream_subscriber_ids(self):
        subscriber_ids = [self.message["sender_id"]]
        user_id = self.external_account.account_settings.user_info.user_id
        if user_id not in subscriber_ids:
            subscriber_ids.append(user_id)
        return subscriber_ids

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
            "subscriber_ids": self._get_stream_subscriber_ids(),
            "event_type": "message",
        }

    def _should_skip_stream_info(self, stream_info):
        return (
            stream_info["type"] == "private" and
            len(set(stream_info["subscriber_ids"])) < 2
        )

    def _get_topic_name(self):
        if self.message["type"] == "private":
            return ZULIP_PRIVATE_TOPIC_NAME
        return self.message["subject"]

    def _get_flags(self):
        if "flags" not in self.message:
            return []
        return self.message["flags"]

    def _get_message_info(self):
        timestamp = self._get_timestamp()
        return {
            "message_id": self.message["id"],
            "sender_id": self.message["sender_id"],
            "content": self.message["content"],
            "read": "read" in self._get_flags(),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def execute(self, cache):
        stream_info = self._get_stream_info()
        if self._should_skip_stream_info(stream_info):
            LOG.warning(
                "Skip unsupported Zulip private message %s from %s "
                "with subscriber ids %s",
                self.message["id"],
                self.external_account.server_url,
                stream_info["subscriber_ids"],
            )
            return None
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


class UpdateMessage:
    priority = SYNC_RESPONSE_PRIORITY_MESSAGE

    def __init__(self, external_account, event):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.event = event
        self.event_id = event["id"]
        self.message_id = event["message_id"]

    @property
    def last_message_id(self):
        return max(self.event.get("message_ids", [self.message_id]))

    def _get_sender_id(self):
        return self.event.get("sender_id") or self.event.get("user_id")

    def _get_updated_at(self):
        edit_timestamp = self.event.get("edit_timestamp")
        if edit_timestamp is None:
            return None
        return datetime.datetime.fromtimestamp(
            edit_timestamp,
            tz=datetime.timezone.utc,
        )

    def execute(self, cache):
        return cache.update_message(
            external_account=self.external_account,
            message_info={
                "message_id": self.message_id,
                "sender_id": self._get_sender_id(),
                "content": self.event["content"],
                "updated_at": self._get_updated_at(),
            },
        )


class DeleteMessage:
    priority = SYNC_RESPONSE_PRIORITY_MESSAGE

    def __init__(self, external_account, event):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.event = event
        self.event_id = event["id"]
        if "message_ids" in event:
            self.message_ids = event["message_ids"]
        else:
            self.message_ids = [event["message_id"]]

    @property
    def last_message_id(self):
        return max(self.message_ids)

    def execute(self, cache):
        return cache.delete_messages(
            external_account=self.external_account,
            message_ids=self.message_ids,
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


class UpdateZulipQueueState:
    priority = SYNC_RESPONSE_PRIORITY_STATE

    def __init__(
        self,
        external_account,
        queue_id=NO_VALUE,
        last_event_id=None,
        last_message_id=None,
        is_synced=None,
        priority=None,
    ):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.last_message_id = last_message_id
        self.is_synced = is_synced
        if priority is not None:
            self.priority = priority

    def execute(self, cache):
        return None


class FinishZulipMessageCatchUp:
    priority = SYNC_RESPONSE_PRIORITY_SYNC_FINISHED

    def __init__(self, external_account, last_message_id):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.last_message_id = last_message_id

    def execute(self, cache):
        return None


class ZulipQueueFailed:
    priority = SYNC_RESPONSE_PRIORITY_SYNC_STARTED

    def __init__(self, external_account):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)

    def execute(self, cache):
        return None


class ZulipOutboundResponse:
    priority = SYNC_RESPONSE_PRIORITY_OUTBOUND

    def __init__(self, external_account, epoch_version, message_uuid):
        self.external_account = external_account
        self.event_owner = get_event_owner(external_account)
        self.epoch_version = epoch_version
        self.message_uuid = message_uuid

    def execute(self, cache):
        return None


class ZulipMessageSent(ZulipOutboundResponse):
    def __init__(
        self,
        external_account,
        epoch_version,
        message_uuid,
        zulip_message_id,
    ):
        super().__init__(
            external_account=external_account,
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.zulip_message_id = zulip_message_id


class ZulipMessageUpdated(ZulipOutboundResponse):
    pass


class ZulipMessageDeleted(ZulipOutboundResponse):
    pass


class ZulipMessageFailed(ZulipOutboundResponse):
    def __init__(
        self,
        external_account,
        epoch_version,
        message_uuid,
        error,
    ):
        super().__init__(
            external_account=external_account,
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.error = error


class ZulipOutboundCommand:
    def __init__(self, epoch_version, message_uuid):
        self.epoch_version = epoch_version
        self.message_uuid = message_uuid

    def _put_failed(self, worker, exc):
        put_sync_response(
            worker.output_queue,
            ZulipMessageFailed(
                external_account=worker.external_account,
                epoch_version=self.epoch_version,
                message_uuid=self.message_uuid,
                error=str(exc),
            ),
        )


class SendZulipMessage(ZulipOutboundCommand):
    def __init__(
        self,
        epoch_version,
        message_uuid,
        stream_name,
        topic_name,
        content,
    ):
        super().__init__(
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.stream_name = stream_name
        self.topic_name = topic_name
        self.content = content

    def execute(self, worker):
        try:
            zulip_message_id = worker.send_message(
                stream_name=self.stream_name,
                topic_name=self.topic_name,
                content=self.content,
            )
        except Exception as exc:
            self._put_failed(worker, exc)
            return
        put_sync_response(
            worker.output_queue,
            ZulipMessageSent(
                external_account=worker.external_account,
                epoch_version=self.epoch_version,
                message_uuid=self.message_uuid,
                zulip_message_id=zulip_message_id,
            ),
        )


class SendZulipPrivateMessage(ZulipOutboundCommand):
    def __init__(
        self,
        epoch_version,
        message_uuid,
        recipient_ids,
        content,
    ):
        super().__init__(
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.recipient_ids = recipient_ids
        self.content = content

    def execute(self, worker):
        try:
            zulip_message_id = worker.send_private_message(
                recipient_ids=self.recipient_ids,
                content=self.content,
            )
        except Exception as exc:
            self._put_failed(worker, exc)
            return
        put_sync_response(
            worker.output_queue,
            ZulipMessageSent(
                external_account=worker.external_account,
                epoch_version=self.epoch_version,
                message_uuid=self.message_uuid,
                zulip_message_id=zulip_message_id,
            ),
        )


class UpdateZulipMessage(ZulipOutboundCommand):
    def __init__(self, epoch_version, message_uuid, message_id, content):
        super().__init__(
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.message_id = message_id
        self.content = content

    def execute(self, worker):
        try:
            worker.update_message(
                message_id=self.message_id,
                content=self.content,
            )
        except Exception as exc:
            self._put_failed(worker, exc)
            return
        put_sync_response(
            worker.output_queue,
            ZulipMessageUpdated(
                external_account=worker.external_account,
                epoch_version=self.epoch_version,
                message_uuid=self.message_uuid,
            ),
        )


class DeleteZulipMessage(ZulipOutboundCommand):
    def __init__(self, epoch_version, message_uuid, message_id):
        super().__init__(
            epoch_version=epoch_version,
            message_uuid=message_uuid,
        )
        self.message_id = message_id

    def execute(self, worker):
        try:
            worker.delete_message(message_id=self.message_id)
        except Exception as exc:
            self._put_failed(worker, exc)
            return
        put_sync_response(
            worker.output_queue,
            ZulipMessageDeleted(
                external_account=worker.external_account,
                epoch_version=self.epoch_version,
                message_uuid=self.message_uuid,
            ),
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

    @property
    def external_account(self):
        return self._external_account

    def stop(self):
        self._stopped = True

    def _get_client(self):
        return self.client_cls(endpoint=self._external_account.server_url)

    def _get_credentials(self):
        return self._external_account.account_settings.credentials

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
        credentials = self._get_credentials()
        client = self._get_client()
        return client.get_messages_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_filters=message_filters,
        )

    def register_message_event_queue(self):
        credentials = self._get_credentials()
        client = self._get_client()
        data = client.register_message_event_queue_with_api_key(
            login=credentials.login,
            token=credentials.token,
        )
        if "queue_id" not in data or "last_event_id" not in data:
            raise RuntimeError(
                "Zulip message event queue registration failed: %s" % data,
            )
        queue_id = data["queue_id"]
        last_event_id = data["last_event_id"]
        self._put_zulip_queue_state(
            queue_id=queue_id,
            last_event_id=last_event_id,
            is_synced=False,
            priority=SYNC_RESPONSE_PRIORITY_SYNC_STARTED,
        )
        LOG.info(
            "Registered Zulip message event queue %s for %s",
            queue_id,
            self._external_account.server_url,
        )
        return queue_id, last_event_id

    def fetch_events(self, queue_id, last_event_id):
        credentials = self._get_credentials()
        client = self._get_client()
        try:
            data = client.get_events_with_api_key(
                login=credentials.login,
                token=credentials.token,
                queue_id=queue_id,
                last_event_id=last_event_id,
            )
        except Exception as exc:
            if "BAD_EVENT_QUEUE_ID" in str(exc):
                raise BadZulipEventQueue() from exc
            raise
        if data.get("result") == "error" and (
            data.get("code") == "BAD_EVENT_QUEUE_ID"
        ):
            raise BadZulipEventQueue()
        if "events" not in data:
            LOG.warning(
                "Zulip message event queue %s for %s returned no events: %s",
                queue_id,
                self._external_account.server_url,
                data,
            )
            raise BadZulipEventQueue()
        return data["events"]

    def fetch_streams(self):
        credentials = self._get_credentials()
        client = self._get_client()
        return client.get_streams_with_api_key(
            login=credentials.login,
            token=credentials.token,
        )

    def fetch_stream_subscribers(self, stream):
        credentials = self._get_credentials()
        client = self._get_client()
        return client.get_stream_subscribers_with_api_key(
            login=credentials.login,
            token=credentials.token,
            stream_id=stream["stream_id"],
        )

    def _convert_workspace_file_links(self, content):
        uploaded_files = {}
        return WORKSPACE_FILE_LINK_RE.sub(
            lambda match: self._convert_workspace_file_link(
                match=match,
                uploaded_files=uploaded_files,
            ),
            content,
        )

    def _convert_workspace_file_link(self, match, uploaded_files):
        parsed_urn = self._parse_workspace_file_urn(match.group("url"))
        if parsed_urn is None:
            return match.group(0)
        file_type, file_uuid, params = parsed_urn
        if file_uuid not in uploaded_files:
            file = messenger_dm_helpers.get_workspace_user_file(
                project_id=self._external_account.project_id,
                user_uuid=self._external_account.user_uuid,
                file_uuid=file_uuid,
            )
            uploaded_files[file_uuid] = self._upload_workspace_file(
                file=file,
                file_name=self._get_workspace_file_name(
                    file=file,
                    match=match,
                    params=params,
                ),
            )
        file_name = self._get_workspace_file_name(
            file=uploaded_files[file_uuid]["file"],
            match=match,
            params=params,
        )
        prefix = "!" if file_type == "image" else ""
        return f"{prefix}[{file_name}]({uploaded_files[file_uuid]['uri']})"

    def _parse_workspace_file_urn(self, url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "urn":
            return None
        urn_parts = parsed.path.split(":", 1)
        if len(urn_parts) != 2 or urn_parts[0] not in WORKSPACE_FILE_URN_TYPES:
            return None
        return (
            urn_parts[0],
            sys_uuid.UUID(urn_parts[1]),
            urllib.parse.parse_qs(parsed.query),
        )

    def _get_workspace_file_name(self, file, match, params):
        names = params.get("name")
        if names:
            return names[0]
        link_name = match.group("name").strip()
        if link_name:
            return link_name
        return file.name

    def _upload_workspace_file(self, file, file_name):
        data = file_storage.read_workspace_file(
            file_uuid=file.uuid,
            storage_type=file.storage_type,
            storage_object_id=file.storage_object_id,
        )
        credentials = self._get_credentials()
        client = self._get_client()
        response = client.upload_file_with_api_key(
            login=credentials.login,
            token=credentials.token,
            file_name=file_name,
            data=data,
        )
        return {
            "file": file,
            "uri": response["uri"],
        }

    def send_message(self, stream_name, topic_name, content):
        credentials = self._get_credentials()
        client = self._get_client()
        data = client.send_message_with_api_key(
            login=credentials.login,
            token=credentials.token,
            stream_name=stream_name,
            topic_name=topic_name,
            content=self._convert_workspace_file_links(content),
        )
        return data["id"]

    def send_private_message(self, recipient_ids, content):
        credentials = self._get_credentials()
        client = self._get_client()
        data = client.send_private_message_with_api_key(
            login=credentials.login,
            token=credentials.token,
            recipient_ids=recipient_ids,
            content=self._convert_workspace_file_links(content),
        )
        return data["id"]

    def update_message(self, message_id, content):
        credentials = self._get_credentials()
        client = self._get_client()
        return client.update_message_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_id=message_id,
            content=self._convert_workspace_file_links(content),
        )

    def delete_message(self, message_id):
        credentials = self._get_credentials()
        client = self._get_client()
        return client.delete_message_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_id=message_id,
        )

    def _put_zulip_queue_state(
        self,
        queue_id=NO_VALUE,
        last_event_id=None,
        last_message_id=None,
        is_synced=None,
        priority=None,
    ):
        if (
            queue_id is NO_VALUE and
            last_event_id is None and
            last_message_id is None and
            is_synced is None
        ):
            return
        put_sync_response(
            self._output_queue,
            UpdateZulipQueueState(
                external_account=self._external_account,
                queue_id=queue_id,
                last_event_id=last_event_id,
                last_message_id=last_message_id,
                is_synced=is_synced,
                priority=priority,
            ),
        )

    def _finish_zulip_message_catch_up(self, last_message_id):
        put_sync_response(
            self._output_queue,
            FinishZulipMessageCatchUp(
                external_account=self._external_account,
                last_message_id=last_message_id,
            ),
        )

    def _fail_zulip_queue(self):
        put_sync_response(
            self._output_queue,
            ZulipQueueFailed(
                external_account=self._external_account,
            ),
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

    def _process_message(self, message, event_id=None):
        if not self._should_process_message(message):
            return False
        put_sync_response(
            self._output_queue,
            AddMessage(
                external_account=self._external_account,
                message=message,
                event_id=event_id,
            ),
        )
        return True

    def _process_message_update(self, event):
        if event.get("rendering_only") or "content" not in event:
            return False
        put_sync_response(
            self._output_queue,
            UpdateMessage(
                external_account=self._external_account,
                event=event,
            ),
        )
        return True

    def _process_message_delete(self, event):
        if "message_ids" not in event and "message_id" not in event:
            return False
        put_sync_response(
            self._output_queue,
            DeleteMessage(
                external_account=self._external_account,
                event=event,
            ),
        )
        return True

    def _catch_up_messages(self, last_message_id):
        message_filters = dict(self.DEFAULT_MESSAGE_FILTERS)
        message_filters["anchor"] = last_message_id

        while not self._stopped:
            self._process_pending_commands()
            if self._stopped:
                break
            messages = self.fetch_messages(message_filters=message_filters)
            if not messages:
                break

            seen_message = False
            seen_processable_message = False
            page_last_message_id = last_message_id
            for message in messages:
                message_id = message["id"]
                if message_id <= last_message_id:
                    continue
                if self._process_message(message):
                    seen_processable_message = True
                last_message_id = message_id
                page_last_message_id = message_id
                message_filters["anchor"] = last_message_id
                seen_message = True

            if seen_message and not seen_processable_message:
                self._put_zulip_queue_state(
                    last_message_id=page_last_message_id,
                )
            if not seen_message:
                break
        if not self._stopped:
            self._finish_zulip_message_catch_up(last_message_id)
        return last_message_id

    def _get_event_message_ids(self, event):
        if event["type"] == "message":
            return [event["message"]["id"]]
        if "message_ids" in event:
            return event["message_ids"]
        if "message_id" in event:
            return [event["message_id"]]
        return []

    def _process_event(self, event):
        event_type = event["type"]
        if event_type == "message":
            message = self._get_message_from_event(event)
            return self._process_message(message, event_id=event["id"])
        if event_type == "update_message":
            return self._process_message_update(event)
        if event_type == "delete_message":
            return self._process_message_delete(event)
        return False

    def _sync_message_events(
        self,
        queue_id,
        last_event_id,
        last_message_id,
    ):
        events = self.fetch_events(
            queue_id=queue_id,
            last_event_id=last_event_id,
        )
        if not events:
            return last_event_id, last_message_id

        seen_processable_message = False
        event_last_event_id = last_event_id
        event_last_message_id = last_message_id
        for event in events:
            event_last_event_id = event["id"]
            message_ids = self._get_event_message_ids(event)
            if self._process_event(event):
                seen_processable_message = True
            if message_ids:
                event_last_message_id = max(
                    event_last_message_id,
                    max(message_ids),
                )

        if not seen_processable_message:
            self._put_zulip_queue_state(
                last_event_id=event_last_event_id,
                last_message_id=event_last_message_id,
            )
        return event_last_event_id, event_last_message_id

    def _get_message_from_event(self, event):
        message = dict(event["message"])
        if "flags" in event:
            message["flags"] = event["flags"]
        return message

    def create_queue_and_fetch_messages(self, last_message_id=0):
        self.register_message_event_queue()
        self._catch_up_messages(last_message_id)

    def sync_messages(
        self,
        queue_id,
        last_event_id=-1,
        last_message_id=0,
        is_synced=False,
    ):
        if not is_synced:
            try:
                last_event_id, last_message_id = self._sync_message_events(
                    queue_id=queue_id,
                    last_event_id=last_event_id,
                    last_message_id=last_message_id,
                )
            except BadZulipEventQueue:
                LOG.warning(
                    "Zulip message event queue %s for %s is dead",
                    queue_id,
                    self._external_account.server_url,
                )
                self._fail_zulip_queue()
                return
            last_message_id = self._catch_up_messages(last_message_id)

        while not self._stopped:
            self._process_pending_commands()
            if self._stopped:
                break
            try:
                last_event_id, last_message_id = self._sync_message_events(
                    queue_id=queue_id,
                    last_event_id=last_event_id,
                    last_message_id=last_message_id,
                )
            except BadZulipEventQueue:
                LOG.warning(
                    "Zulip message event queue %s for %s is dead",
                    queue_id,
                    self._external_account.server_url,
                )
                self._fail_zulip_queue()
                return

    def _execute_command(self, command):
        if self._stopped and not isinstance(command, StopWorker):
            return
        try:
            command.execute(self)
        except Exception:
            LOG.exception("Unexpected Zulip bridge worker command error")

    def _process_pending_commands(self):
        while not self._stopped:
            try:
                command = self._input_queue.get_nowait()
            except queue.Empty:
                return
            self._execute_command(command)

    def run(self):
        while not self._stopped:
            command = self._input_queue.get()
            self._execute_command(command)
