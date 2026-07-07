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
import logging
import queue
import uuid as sys_uuid

from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.services.integration_bridge import workers

LOG = logging.getLogger(__name__)
SYNC_QUEUE_TIMEOUT = 0.1
RETRY_COMMAND_DELAY = datetime.timedelta(seconds=30)
DEFAULT_SYNC_QUEUE_BATCH_LIMIT = 100
DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT = 100
NO_VALUE = object()
OUTBOUND_EVENT_ACTIONS = ("created", "updated", "deleted")
OUTBOUND_PENDING_STATUS = "pending"
OUTBOUND_PROCESSING_STATUS = "processing"
OUTBOUND_SENT_STATUS = "sent"
OUTBOUND_SKIPPED_STATUS = "skipped"
OUTBOUND_FAILED_STATUS = "failed"
OUTBOUND_EVENT_COLUMNS = (
    "epoch_version",
    "uuid",
    "project_id",
    "user_uuid",
    "payload",
    "created_at",
    "updated_at",
    "schema_version",
    "object_type",
    "action",
)
OUTBOUND_EVENTS_QUERY = """
SELECT
    e.epoch_version,
    e.uuid,
    e.project_id,
    e.user_uuid,
    e.payload,
    e.created_at,
    e.updated_at,
    e.schema_version,
    e.object_type,
    e.action
FROM m_workspace_events AS e
WHERE e.object_type = 'message'
  AND e.action IN ('created', 'updated', 'deleted')
  AND e.epoch_version > %s
  AND e.payload->>'source_name' = 'zulip'
  AND e.payload ? 'author_uuid'
  AND e.payload ? 'source'
  AND e.user_uuid::text = e.payload->>'author_uuid'
  AND (
      e.action <> 'created'
      OR e.payload #>> '{source,message_id}' IS NULL
  )
  AND NOT EXISTS (
      SELECT 1
      FROM m_zulip_outbound_event_states AS s
      WHERE s.epoch_version = e.epoch_version
  )
ORDER BY e.epoch_version ASC
LIMIT %s
"""


class RetryCommandLater(Exception):
    pass


class SyncStreamsNeeded(RetryCommandLater):
    def __init__(self, external_account, stream_id):
        self.external_account = external_account
        self.stream_id = stream_id
        super().__init__(
            "Postpone Zulip item because stream %s from %s was not resolved" %
            (stream_id, external_account.server_url),
        )


class WorkspaceIntegrationBridgeCache:
    def __init__(self):
        self._streams = {}
        self._topics = {}
        self._messages = {}
        self._processed_entities = {}
        self._user_uuids = {}
        self._stream_bindings = set()

    def _processed_entity_cache_key(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        return (
            external_account.project_id,
            external_account.server_url,
            entity_type,
            str(entity_id),
        )

    def _get_processed_workspace_uuid(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        cache_key = self._processed_entity_cache_key(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        if cache_key in self._processed_entities:
            workspace_uuid = self._processed_entities[cache_key]
            LOG.info(
                "Duplicate Zulip %s %s from %s mapped to workspace %s",
                entity_type,
                entity_id,
                external_account.server_url,
                workspace_uuid,
            )
            return workspace_uuid

        processed = models.ZulipProcessedEntity.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "server_url": dm_filters.EQ(external_account.server_url),
                "entity_type": dm_filters.EQ(entity_type),
                "entity_id": dm_filters.EQ(str(entity_id)),
            },
        )
        if processed is None:
            return None

        self._processed_entities[cache_key] = processed.workspace_uuid
        LOG.info(
            "Duplicate Zulip %s %s from %s mapped to workspace %s",
            entity_type,
            entity_id,
            external_account.server_url,
            processed.workspace_uuid,
        )
        return processed.workspace_uuid

    def _save_processed_entity(
        self,
        external_account,
        entity_type,
        entity_id,
        workspace_uuid,
    ):
        processed = models.ZulipProcessedEntity(
            uuid=sys_uuid.uuid4(),
            project_id=external_account.project_id,
            server_url=external_account.server_url,
            entity_type=entity_type,
            entity_id=str(entity_id),
            workspace_uuid=workspace_uuid,
        )
        processed.insert()
        cache_key = self._processed_entity_cache_key(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        self._processed_entities[cache_key] = workspace_uuid
        LOG.info(
            "Processed Zulip %s %s from %s mapped to workspace %s",
            entity_type,
            entity_id,
            external_account.server_url,
            workspace_uuid,
        )

    def get_or_create_stream(self, external_account, stream_info):
        stream_id = stream_info["stream_id"]
        entity_type = self._get_stream_entity_type(stream_info)
        processed_uuid = self._get_processed_workspace_uuid(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=stream_id,
        )
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            stream_info["type"],
            stream_id,
        )
        if processed_uuid is not None and cache_key not in self._streams:
            self._streams[cache_key] = models.WorkspaceStream.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(processed_uuid),
                    "project_id": dm_filters.EQ(external_account.project_id),
                },
            )
        elif cache_key not in self._streams:
            stream = self._load_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )
            self._streams[cache_key] = stream
            self._save_processed_entity(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=stream_id,
                workspace_uuid=stream.uuid,
            )
        stream = self._streams[cache_key]
        self._bind_stream_users(
            external_account=external_account,
            stream=stream,
            subscriber_ids=stream_info["subscriber_ids"],
        )
        return stream

    @staticmethod
    def _is_private_stream_info(stream_info):
        return stream_info["type"] == "private"

    def _get_private_subscriber_ids(self, stream_info):
        return set(stream_info["subscriber_ids"])

    def _is_private_direct_stream_info(self, stream_info):
        return (
            self._is_private_stream_info(stream_info) and
            len(self._get_private_subscriber_ids(stream_info)) == 2
        )

    def _is_private_group_stream_info(self, stream_info):
        return (
            self._is_private_stream_info(stream_info) and
            len(self._get_private_subscriber_ids(stream_info)) > 2
        )

    def _get_stream_entity_type(self, stream_info):
        if self._is_private_stream_info(stream_info):
            return "private_stream"
        return "stream"

    def _is_matching_stream(self, external_account, stream, stream_info):
        server_url = getattr(
            stream.source,
            "server_url",
            external_account.server_url,
        )
        matches = (
            stream.source.stream_id == stream_info["stream_id"] and
            server_url == external_account.server_url and
            getattr(stream, "private", False) ==
            self._is_private_stream_info(stream_info)
        )
        if not matches:
            return False
        if self._is_private_direct_stream_info(stream_info):
            return getattr(stream, "direct_user_uuid", None) is not None
        if self._is_private_group_stream_info(stream_info):
            return getattr(stream, "direct_user_uuid", None) is None
        return matches

    def _load_or_create_stream(self, external_account, stream_info):
        for stream in models.WorkspaceStream.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "source_name": dm_filters.EQ(models.SourceName.ZULIP.value),
            },
        ):
            if self._is_matching_stream(
                external_account=external_account,
                stream=stream,
                stream_info=stream_info,
            ):
                return stream
        if self._should_request_stream_sync(stream_info):
            raise SyncStreamsNeeded(
                external_account=external_account,
                stream_id=stream_info["stream_id"],
            )
        return self._create_stream(
            external_account=external_account,
            stream_info=stream_info,
        )

    def _should_request_stream_sync(self, stream_info):
        return (
            stream_info["type"] == "stream" and
            stream_info.get("event_type") == "message"
        )

    def _get_zulip_user_uuid(self, external_account, user_id):
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            user_id,
        )
        if (
            cache_key not in self._user_uuids or
            self._user_uuids[cache_key] is None
        ):
            user_uuid = self._load_zulip_user_uuid(
                external_account=external_account,
                user_id=user_id,
            )
            if user_uuid is not None:
                self._user_uuids[cache_key] = user_uuid
            return user_uuid
        return self._user_uuids[cache_key]

    def _get_required_zulip_user_uuid(self, external_account, user_id):
        user_uuid = self._get_zulip_user_uuid(
            external_account=external_account,
            user_id=user_id,
        )
        if user_uuid is None:
            raise RetryCommandLater(
                "Postpone Zulip item because user %s was not resolved" %
                user_id,
            )
        return user_uuid

    def _load_zulip_user_uuid(self, external_account, user_id):
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            user_id,
        )
        accounts = models.ExternalAccount.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "account_type": dm_filters.EQ(
                    models.ExternalAccountType.ZULIP.value,
                ),
                "server_url": dm_filters.EQ(external_account.server_url),
            },
            order_by={"created_at": "asc", "uuid": "asc"},
        )
        for account in accounts:
            user_info = account.account_settings.user_info
            if user_info is not None:
                account_cache_key = (
                    account.project_id,
                    account.server_url,
                    user_info.user_id,
                )
                self._user_uuids[account_cache_key] = account.user_uuid
        return self._user_uuids.get(cache_key)

    def _get_zulip_user_uuids(self, external_account, user_ids):
        user_uuids = []
        for user_id in user_ids:
            user_uuid = self._get_zulip_user_uuid(
                external_account=external_account,
                user_id=user_id,
            )
            if user_uuid is not None and user_uuid not in user_uuids:
                user_uuids.append(user_uuid)
        return user_uuids

    def _get_private_direct_user_uuid(self, external_account, stream_info,
                                      user_uuid):
        participant_uuids = self._get_zulip_user_uuids(
            external_account=external_account,
            user_ids=stream_info["subscriber_ids"],
        )
        direct_user_uuids = [
            participant_uuid for participant_uuid in participant_uuids
            if participant_uuid != user_uuid
        ]
        if not direct_user_uuids:
            raise RetryCommandLater(
                (
                    "Postpone Zulip private stream %s because no direct user "
                    "was resolved from subscriber ids %s"
                ) % (
                    stream_info["stream_id"],
                    stream_info["subscriber_ids"],
                ),
            )
        return direct_user_uuids[0]

    def _bind_stream_users(
        self,
        external_account,
        stream,
        subscriber_ids,
    ):
        user_uuids = self._get_zulip_user_uuids(
            external_account=external_account,
            user_ids=subscriber_ids,
        )
        new_user_uuids = []
        for user_uuid in user_uuids:
            if user_uuid == stream.user_uuid:
                continue
            cache_key = (
                external_account.project_id,
                stream.uuid,
                user_uuid,
            )
            if cache_key in self._stream_bindings:
                continue
            self._stream_bindings.add(cache_key)
            new_user_uuids.append(user_uuid)
        if not new_user_uuids:
            return
        messenger_dm_helpers.get_or_create_workspace_stream_bindings(
            project_id=external_account.project_id,
            stream_uuid=stream.uuid,
            who_uuid=stream.user_uuid,
            role_user_uuids={
                models.WorkspaceStreamRole.MEMBER.value: new_user_uuids,
            },
        )

    def _create_stream(self, external_account, stream_info):
        stream_id = stream_info["stream_id"]
        if "default_topic_name" in stream_info:
            default_topic_name = stream_info["default_topic_name"]
        else:
            default_topic_name = "General Topic"
        user_uuid = self._get_required_zulip_user_uuid(
            external_account=external_account,
            user_id=stream_info["creator_id"],
        )
        values = {
            "project_id": external_account.project_id,
            "user_uuid": user_uuid,
            "name": stream_info["display_recipient"],
            "description": stream_info["description"],
            "source_name": models.SourceName.ZULIP.value,
            "source": models.ZulipSource(
                stream_id=stream_id,
                server_url=external_account.server_url,
            ),
            "invite_only": stream_info["invite_only"],
            "announce": stream_info["announce"],
            "is_archived": stream_info["is_archived"],
            "default_topic_name": default_topic_name,
        }
        if self._is_private_stream_info(stream_info):
            if self._is_private_group_stream_info(stream_info):
                return messenger_dm_helpers.create_workspace_private_group_stream(
                    **values
                )
            if self._is_private_direct_stream_info(stream_info):
                direct_user_uuid = self._get_private_direct_user_uuid(
                    external_account=external_account,
                    stream_info=stream_info,
                    user_uuid=user_uuid,
                )
                values["direct_user_uuid"] = direct_user_uuid
            else:
                raise RetryCommandLater(
                    (
                        "Postpone Zulip private stream %s because no direct "
                        "user was resolved from subscriber ids %s"
                    ) % (
                        stream_info["stream_id"],
                        stream_info["subscriber_ids"],
                    ),
                )
        return messenger_dm_helpers.get_or_create_workspace_user_stream(
            **values
        )

    def _build_zulip_topic_source(
        self,
        external_account,
        stream_info,
        topic_name,
    ):
        return models.ZulipSource(
            stream_id=stream_info["stream_id"],
            server_url=external_account.server_url,
            topic_name=topic_name,
        )

    def _build_zulip_message_source(
        self,
        external_account,
        stream_info,
        topic_name,
        message_info,
    ):
        return models.ZulipSource(
            stream_id=stream_info["stream_id"],
            server_url=external_account.server_url,
            topic_name=topic_name,
            message_id=message_info["message_id"],
        )

    def get_or_create_topic(
        self,
        external_account,
        stream,
        stream_info,
        topic_name,
    ):
        entity_id = "%s/%s" % (stream_info["stream_id"], topic_name)
        processed_uuid = self._get_processed_workspace_uuid(
            external_account=external_account,
            entity_type="topic",
            entity_id=entity_id,
        )
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            stream.uuid,
            topic_name,
        )
        if processed_uuid is not None and cache_key not in self._topics:
            self._topics[cache_key] = (
                models.WorkspaceStreamTopic.objects.get_one(
                    filters={
                        "uuid": dm_filters.EQ(processed_uuid),
                        "project_id": dm_filters.EQ(
                            external_account.project_id,
                        ),
                        "stream_uuid": dm_filters.EQ(stream.uuid),
                    },
                )
            )
        elif cache_key not in self._topics:
            source = self._build_zulip_topic_source(
                external_account=external_account,
                stream_info=stream_info,
                topic_name=topic_name,
            )
            topic = (
                messenger_dm_helpers
                .get_or_create_workspace_stream_topic_with_flags(
                    project_id=external_account.project_id,
                    stream_uuid=stream.uuid,
                    name=topic_name,
                    source_name=models.SourceName.ZULIP.value,
                    source=source,
                )
            )
            self._topics[cache_key] = topic
            self._save_processed_entity(
                external_account=external_account,
                entity_type="topic",
                entity_id=entity_id,
                workspace_uuid=topic.uuid,
            )
        return self._topics[cache_key]

    def get_or_create_message(
        self,
        external_account,
        stream,
        topic,
        stream_info,
        topic_name,
        message_info,
    ):
        entity_id = message_info["message_id"]
        user_uuid = self._get_required_zulip_user_uuid(
            external_account=external_account,
            user_id=message_info["sender_id"],
        )
        processed_uuid = self._get_processed_workspace_uuid(
            external_account=external_account,
            entity_type="message",
            entity_id=entity_id,
        )
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            message_info["message_id"],
        )
        if processed_uuid is not None and cache_key not in self._messages:
            self._messages[cache_key] = (
                messenger_dm_helpers.get_workspace_user_message(
                    project_id=external_account.project_id,
                    user_uuid=user_uuid,
                    message_uuid=processed_uuid,
                )
            )
        elif cache_key not in self._messages:
            source = self._build_zulip_message_source(
                external_account=external_account,
                stream_info=stream_info,
                topic_name=topic_name,
                message_info=message_info,
            )
            message = (
                messenger_dm_helpers.get_or_create_workspace_user_message(
                    project_id=external_account.project_id,
                    user_uuid=user_uuid,
                    stream_uuid=stream.uuid,
                    topic_uuid=topic.uuid,
                    payload=message_payloads.MarkdownPayload(
                        content=message_info["content"],
                    ),
                    source_name=models.SourceName.ZULIP.value,
                    source=source,
                    created_at=message_info["created_at"],
                    updated_at=message_info["updated_at"],
                )
            )
            self._messages[cache_key] = message
            self._save_processed_entity(
                external_account=external_account,
                entity_type="message",
                entity_id=entity_id,
                workspace_uuid=message.uuid,
            )
        message = self._messages[cache_key]
        self._sync_message_read_flag(
            external_account=external_account,
            message=message,
            message_info=message_info,
        )
        return message

    def _sync_message_read_flag(
        self,
        external_account,
        message,
        message_info,
    ):
        if not message_info["read"]:
            return
        messenger_dm_helpers.read_workspace_user_message(
            project_id=external_account.project_id,
            user_uuid=external_account.user_uuid,
            message_uuid=message.uuid,
        )


class WorkspaceIntegrationBridgeWorker(basic.BasicService):
    def __init__(
        self,
        sync_queue_batch_limit=DEFAULT_SYNC_QUEUE_BATCH_LIMIT,
        **kwargs
    ):
        super().__init__(**kwargs)
        self._sync_queue_batch_limit = sync_queue_batch_limit
        self._workers = {}
        self._history_workers = {}
        self._worker_input_queues = {}
        self._worker_history_input_queues = {}
        self._worker_accounts = {}
        self._sync_queue = queue.PriorityQueue(
            maxsize=workers.MAX_SYNC_QUEUE_SIZE,
        )
        self._postponed_commands = []
        self._last_sync_queue_stats = None
        self._message_sync_worker_key = None
        self._queue_recreate_worker_keys = set()
        self._stream_sync_event_owners = set()
        self._cache = WorkspaceIntegrationBridgeCache()
        self._last_outbound_event_epoch = 0
        self._stopping = False

    def stop(self):
        LOG.info("Stop workspace integration bridge worker")
        self._stopping = True

    def _get_unsynced_user_providers(self, account_type):
        now = datetime.datetime.now(datetime.timezone.utc)
        return models.ExternalAccountUserSync.objects.get_all(
            filters={
                "account_type": dm_filters.EQ(account_type),
                "next_sync_at": dm_filters.LE(now),
            },
        )

    def _sync_user_providers(self, account_type):
        user_providers = self._get_unsynced_user_providers(
            account_type=account_type,
        )
        for provider in user_providers:
            provider.sync()

    def _sync_iam_users(self):
        self._sync_user_providers(
            account_type=models.ExternalAccountType.IAM.value,
        )

    def _sync_zulip_users(self):
        self._sync_user_providers(
            account_type=models.ExternalAccountType.ZULIP.value,
        )

    def _iteration(self):
        ctx = contexts.Context()
        with ctx.session_manager():
            self._run_iteration()

    def _clear_finished_workers(self):
        for worker_key, worker in list(self._workers.items()):
            if hasattr(worker, "is_alive") and not worker.is_alive():
                del self._workers[worker_key]
                self._worker_input_queues.pop(worker_key, None)
        for worker_key, worker in list(self._history_workers.items()):
            if hasattr(worker, "is_alive") and not worker.is_alive():
                del self._history_workers[worker_key]
                self._worker_history_input_queues.pop(worker_key, None)
                if self._message_sync_worker_key == worker_key:
                    self._message_sync_worker_key = None
                self._queue_recreate_worker_keys.discard(worker_key)
        for worker_key in list(self._worker_accounts):
            if (
                worker_key not in self._workers and
                worker_key not in self._history_workers
            ):
                self._worker_accounts.pop(worker_key, None)

    def _start_bridges(self):
        self._clear_finished_workers()
        all_users = models.ExternalAccount.objects.get_all(
            filters={
                "account_type": dm_filters.EQ(
                    models.ExternalAccountType.ZULIP.value,
                ),
            },
        )
        first_worker_key = None
        for user in all_users:
            if user.account_settings.credentials is None:
                continue
            worker_key = self._get_zulip_worker_key(user)
            if first_worker_key is None:
                first_worker_key = worker_key
            self._worker_accounts[worker_key] = user
            if worker_key not in self._workers:
                LOG.info(
                    "Start Zulip bridge worker for %s",
                    user.server_url,
                )
                input_queue = queue.Queue(maxsize=workers.MAX_SYNC_QUEUE_SIZE)
                worker = workers.ZulipBridgeWorker(
                    external_account=user,
                    input_queue=input_queue,
                    output_queue=self._sync_queue,
                )
                worker.start()
                self._workers[worker_key] = worker
                self._worker_input_queues[worker_key] = input_queue
            if worker_key not in self._history_workers:
                LOG.info(
                    "Start Zulip history bridge worker for %s",
                    user.server_url,
                )
                history_input_queue = queue.Queue(
                    maxsize=workers.MAX_SYNC_QUEUE_SIZE,
                )
                history_worker = workers.ZulipBridgeWorker(
                    external_account=user,
                    input_queue=history_input_queue,
                    output_queue=self._sync_queue,
                )
                history_worker.start()
                self._history_workers[worker_key] = history_worker
                self._worker_history_input_queues[worker_key] = (
                    history_input_queue
                )

        if first_worker_key is not None:
            self._request_zulip_message_sync(first_worker_key)

    def _get_zulip_worker_key(self, external_account):
        return (
            external_account.project_id,
            external_account.server_url,
            external_account.user_uuid,
        )

    def _get_history_input_queue(self, worker_key):
        return (
            self._worker_history_input_queues.get(worker_key) or
            self._worker_input_queues.get(worker_key)
        )

    def _clear_zulip_message_sync(self, worker_key):
        if self._message_sync_worker_key == worker_key:
            self._message_sync_worker_key = None

    def _clear_zulip_queue_recreate(self, worker_key):
        self._queue_recreate_worker_keys.discard(worker_key)

    def _get_or_create_zulip_queue_state(self, external_account):
        queue_state = models.ZulipEventQueueState.objects.get_one_or_none(
            filters={
                "external_account_uuid": dm_filters.EQ(
                    external_account.uuid,
                ),
            },
        )
        if queue_state is not None:
            return queue_state

        queue_state = models.ZulipEventQueueState(
            uuid=sys_uuid.uuid4(),
            project_id=external_account.project_id,
            external_account_uuid=external_account.uuid,
            server_url=external_account.server_url,
            user_uuid=external_account.user_uuid,
        )
        queue_state.insert()
        return queue_state

    def _request_zulip_message_sync(self, worker_key):
        if self._message_sync_worker_key is not None:
            return
        external_account = self._worker_accounts[worker_key]
        queue_state = self._get_or_create_zulip_queue_state(
            external_account,
        )
        if queue_state.queue_id is None:
            self._request_zulip_queue_recreate(worker_key)
            return
        LOG.info(
            (
                "Request Zulip message sync for %s "
                "queue=%s event=%s message=%s"
            ),
            external_account.server_url,
            queue_state.queue_id,
            queue_state.last_event_id,
            queue_state.last_message_id,
        )
        self._message_sync_worker_key = worker_key
        input_queue = self._get_history_input_queue(worker_key)
        if input_queue is None:
            self._clear_zulip_message_sync(worker_key)
            return
        input_queue.put(
            workers.SyncMessages(
                queue_id=queue_state.queue_id,
                last_event_id=queue_state.last_event_id,
                last_message_id=queue_state.last_message_id,
                is_synced=queue_state.is_synced,
                on_finished=(
                    lambda worker_key=worker_key:
                    self._clear_zulip_message_sync(worker_key)
                ),
            ),
        )

    def _request_zulip_queue_recreate(self, worker_key):
        if worker_key in self._queue_recreate_worker_keys:
            return
        external_account = self._worker_accounts[worker_key]
        queue_state = self._get_or_create_zulip_queue_state(
            external_account,
        )
        self._queue_recreate_worker_keys.add(worker_key)
        self._update_external_account_zulip_queue_state(
            external_account=external_account,
            queue_id=None,
            is_synced=False,
        )
        LOG.info(
            "Request Zulip queue recreate for %s from message anchor %s",
            external_account.server_url,
            queue_state.last_message_id,
        )
        input_queue = self._get_history_input_queue(worker_key)
        if input_queue is None:
            self._clear_zulip_queue_recreate(worker_key)
            return
        input_queue.put(
            workers.CreateZulipQueueAndFetchMessages(
                last_message_id=queue_state.last_message_id,
            ),
        )

    def _clear_zulip_stream_sync(self, event_owner):
        self._stream_sync_event_owners.discard(event_owner)

    def _get_zulip_worker_key_by_event_owner(self, event_owner):
        if event_owner in self._workers:
            return event_owner
        for worker_key in self._workers:
            return worker_key
        return None

    def _request_zulip_stream_sync(self, command):
        event_owner = command.event_owner
        if event_owner in self._stream_sync_event_owners:
            return
        worker_key = self._get_zulip_worker_key_by_event_owner(event_owner)
        if worker_key is None:
            return
        external_account = self._worker_accounts[worker_key]
        self._stream_sync_event_owners.add(event_owner)
        LOG.info(
            "Request Zulip stream sync for %s",
            external_account.server_url,
        )
        input_queue = self._get_history_input_queue(worker_key)
        if input_queue is None:
            self._clear_zulip_stream_sync(event_owner)
            return
        input_queue.put(
            workers.SyncStreams(
                event_owner=event_owner,
            ),
        )

    def _get_sync_queue_stats(self):
        queued = self._sync_queue.qsize()
        postponed = len(self._postponed_commands)
        return queued, postponed, queued + postponed

    def _put_sync_command(self, command):
        workers.put_sync_response(self._sync_queue, command)

    def _get_outbound_event_state(self, epoch_version):
        return models.ZulipOutboundEventState.objects.get_one_or_none(
            filters={
                "epoch_version": dm_filters.EQ(epoch_version),
            },
        )

    def _create_outbound_event_state(self, event, external_account):
        state = models.ZulipOutboundEventState(
            uuid=sys_uuid.uuid4(),
            project_id=event.project_id,
            epoch_version=event.epoch_version,
            external_account_uuid=external_account.uuid,
        )
        state.insert()
        return state

    def _update_outbound_event_state(self, state, **values):
        state.update_dm(values=values)
        state.update()

    def _set_outbound_retry(self, state, error):
        self._update_outbound_event_state(
            state,
            status=OUTBOUND_PENDING_STATUS,
            attempts=state.attempts + 1,
            next_retry_at=(
                datetime.datetime.now(datetime.timezone.utc) +
                RETRY_COMMAND_DELAY
            ),
            last_error=error,
        )

    def _set_outbound_processing(self, state):
        self._update_outbound_event_state(
            state,
            status=OUTBOUND_PROCESSING_STATUS,
            attempts=state.attempts + 1,
            next_retry_at=(
                datetime.datetime.now(datetime.timezone.utc) +
                RETRY_COMMAND_DELAY
            ),
            last_error=None,
        )

    def _set_outbound_done(self, state, status=OUTBOUND_SENT_STATUS):
        self._update_outbound_event_state(
            state,
            status=status,
            next_retry_at=None,
            last_error=None,
        )

    def _set_outbound_failed(self, state, error):
        self._update_outbound_event_state(
            state,
            status=OUTBOUND_FAILED_STATUS,
            next_retry_at=None,
            last_error=error,
        )

    @staticmethod
    def _source_value(source, name):
        if hasattr(source, name):
            return getattr(source, name)
        return source.get(name)

    @staticmethod
    def _uuid_value(value):
        if isinstance(value, sys_uuid.UUID):
            return value
        return sys_uuid.UUID(str(value))

    def _is_zulip_outbound_event(self, event):
        payload = event.payload
        if (
            "source_name" not in payload or
            "author_uuid" not in payload or
            "source" not in payload
        ):
            return False
        if payload["source_name"] != models.SourceName.ZULIP.value:
            return False
        if (
            event.action == "created" and
            self._source_value(payload["source"], "message_id") is not None
        ):
            return False
        return str(event.user_uuid) == str(payload["author_uuid"])

    def _get_zulip_outbound_external_account(self, event):
        payload = event.payload
        source = payload["source"]
        return models.ExternalAccount.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(event.project_id),
                "account_type": dm_filters.EQ(
                    models.ExternalAccountType.ZULIP.value,
                ),
                "server_url": dm_filters.EQ(
                    self._source_value(source, "server_url"),
                ),
                "user_uuid": dm_filters.EQ(payload["author_uuid"]),
            },
        )

    def _get_ready_outbound_event_states(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        states = models.ZulipOutboundEventState.objects.get_all(
            filters={
                "status": dm_filters.EQ(OUTBOUND_PENDING_STATUS),
            },
            order_by={"epoch_version": "asc"},
            limit=DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT,
        )
        return [
            state for state in states
            if state.next_retry_at is None or state.next_retry_at <= now
        ]

    def _fail_expired_outbound_processing_states(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        states = models.ZulipOutboundEventState.objects.get_all(
            filters={
                "status": dm_filters.EQ(OUTBOUND_PROCESSING_STATUS),
            },
            order_by={"epoch_version": "asc"},
            limit=DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT,
        )
        for state in states:
            if state.next_retry_at is None or state.next_retry_at <= now:
                self._set_outbound_failed(
                    state,
                    "Zulip outbound command processing expired",
                )

    @staticmethod
    def _restore_outbound_event(row):
        return models.WorkspaceEvent.restore_from_storage(**{
            column: row[column]
            for column in OUTBOUND_EVENT_COLUMNS
        })

    def _get_new_outbound_events(self):
        result = contexts.Context().get_session().execute(
            OUTBOUND_EVENTS_QUERY,
            (
                self._last_outbound_event_epoch,
                DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT,
            ),
        )
        return [
            self._restore_outbound_event(row)
            for row in result.fetchall()
        ]

    def _discover_zulip_outbound_events(self):
        events = self._get_new_outbound_events()
        for event in events:
            if event.epoch_version > self._last_outbound_event_epoch:
                self._last_outbound_event_epoch = event.epoch_version
            if not self._is_zulip_outbound_event(event):
                continue
            if self._get_outbound_event_state(event.epoch_version) is not None:
                continue
            external_account = self._get_zulip_outbound_external_account(event)
            if external_account is None:
                LOG.warning(
                    "Skip Zulip outbound event %s because author account "
                    "was not resolved",
                    event.epoch_version,
                )
                continue
            self._create_outbound_event_state(
                event=event,
                external_account=external_account,
            )

    def _get_workspace_message(self, event):
        return models.WorkspaceMessage.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(event.payload["uuid"]),
                "project_id": dm_filters.EQ(event.project_id),
            },
        )

    def _get_workspace_stream(self, event, stream_uuid):
        return models.WorkspaceStream.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(stream_uuid),
                "project_id": dm_filters.EQ(event.project_id),
            },
        )

    def _get_workspace_topic(self, event, topic_uuid):
        return models.WorkspaceStreamTopic.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(topic_uuid),
                "project_id": dm_filters.EQ(event.project_id),
            },
        )

    def _get_outbound_message_content(self, event):
        return event.payload["payload"]["content"]

    def _get_zulip_private_recipient_id(self, event, message, stream):
        if stream.direct_user_uuid is None:
            return None
        server_url = self._source_value(message.source, "server_url")
        external_account = models.ExternalAccount.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(event.project_id),
                "account_type": dm_filters.EQ(
                    models.ExternalAccountType.ZULIP.value,
                ),
                "server_url": dm_filters.EQ(server_url),
                "user_uuid": dm_filters.EQ(stream.direct_user_uuid),
            },
        )
        if external_account is None:
            raise RetryCommandLater(
                "Postpone Zulip private message because recipient account "
                "was not resolved",
            )
        if external_account.account_settings.user_info is None:
            raise RetryCommandLater(
                "Postpone Zulip private message because recipient user_info "
                "is missing",
            )
        return external_account.account_settings.user_info.user_id

    def _build_send_zulip_message_command(self, event):
        message = self._get_workspace_message(event)
        if message is None:
            return None
        stream = self._get_workspace_stream(event, message.stream_uuid)
        if stream.private:
            recipient_id = self._get_zulip_private_recipient_id(
                event=event,
                message=message,
                stream=stream,
            )
            if recipient_id is None:
                return None
            return workers.SendZulipPrivateMessage(
                epoch_version=event.epoch_version,
                message_uuid=event.payload["uuid"],
                recipient_ids=[recipient_id],
                content=self._get_outbound_message_content(event),
            )
        topic = self._get_workspace_topic(event, message.topic_uuid)
        topic_name = self._source_value(message.source, "topic_name")
        if topic_name is None:
            topic_name = topic.name
        return workers.SendZulipMessage(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["uuid"],
            stream_name=stream.name,
            topic_name=topic_name,
            content=self._get_outbound_message_content(event),
        )

    def _build_update_zulip_message_command(self, event):
        message = self._get_workspace_message(event)
        if message is None:
            return None
        message_id = self._source_value(message.source, "message_id")
        if message_id is None:
            raise RetryCommandLater(
                "Postpone Zulip message update because message_id is missing",
            )
        return workers.UpdateZulipMessage(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["uuid"],
            message_id=message_id,
            content=self._get_outbound_message_content(event),
        )

    def _build_delete_zulip_message_command(self, event):
        source = event.payload["source"]
        message_id = self._source_value(source, "message_id")
        if message_id is None:
            raise RetryCommandLater(
                "Postpone Zulip message delete because message_id is missing",
            )
        return workers.DeleteZulipMessage(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["uuid"],
            message_id=message_id,
        )

    def _build_zulip_outbound_command(self, event):
        if event.action == "created":
            return self._build_send_zulip_message_command(event)
        if event.action == "updated":
            return self._build_update_zulip_message_command(event)
        if event.action == "deleted":
            return self._build_delete_zulip_message_command(event)
        return None

    def _get_zulip_outbound_event(self, state):
        return models.WorkspaceEvent.objects.get_one(
            filters={
                "epoch_version": dm_filters.EQ(state.epoch_version),
                "project_id": dm_filters.EQ(state.project_id),
            },
        )

    def _get_zulip_outbound_account(self, state):
        return models.ExternalAccount.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(state.external_account_uuid),
                "project_id": dm_filters.EQ(state.project_id),
            },
        )

    def _dispatch_zulip_outbound_state(self, state):
        event = self._get_zulip_outbound_event(state)
        if not self._is_zulip_outbound_event(event):
            self._set_outbound_done(state, status=OUTBOUND_SKIPPED_STATUS)
            return
        external_account = self._get_zulip_outbound_account(state)
        if external_account is None:
            self._set_outbound_retry(
                state,
                "Zulip external account was not resolved",
            )
            return
        if external_account.account_settings.credentials is None:
            self._set_outbound_retry(
                state,
                "Zulip external account credentials are missing",
            )
            return
        worker_key = self._get_zulip_worker_key(external_account)
        input_queue = self._worker_input_queues.get(worker_key)
        if input_queue is None:
            self._set_outbound_retry(
                state,
                "Zulip bridge worker is not running",
            )
            return
        command = self._build_zulip_outbound_command(event)
        if command is None:
            self._set_outbound_done(state, status=OUTBOUND_SKIPPED_STATUS)
            return
        try:
            self._set_outbound_processing(state)
            input_queue.put_nowait(command)
        except queue.Full:
            self._set_outbound_retry(
                state,
                "Zulip bridge worker input queue is full",
            )

    def _dispatch_zulip_outbound_events(self):
        self._discover_zulip_outbound_events()
        self._fail_expired_outbound_processing_states()
        for state in self._get_ready_outbound_event_states():
            try:
                self._dispatch_zulip_outbound_state(state)
            except RetryCommandLater as exc:
                self._set_outbound_retry(state, str(exc))

    def _has_pending_stream_sync(self):
        return bool(self._stream_sync_event_owners)

    def _can_process_sync_command(self, command):
        if not self._has_pending_stream_sync():
            return True
        return isinstance(
            command,
            (
                workers.AddStream,
                workers.SyncStreamsFinished,
            ),
        )

    def _get_outbound_state_for_command(self, command):
        return self._get_outbound_event_state(command.epoch_version)

    def _update_sent_zulip_message_source(self, command):
        message_uuid = self._uuid_value(command.message_uuid)
        message = models.WorkspaceMessage.objects.get_one_or_none(
            filters={
                "uuid": dm_filters.EQ(message_uuid),
                "project_id": dm_filters.EQ(command.external_account.project_id),
            },
        )
        if message is None:
            return
        source = message.source
        message.update_dm(
            values={
                "source": models.ZulipSource(
                    stream_id=self._source_value(source, "stream_id"),
                    server_url=(
                        self._source_value(source, "server_url") or
                        command.external_account.server_url
                    ),
                    topic_name=self._source_value(source, "topic_name"),
                    message_id=command.zulip_message_id,
                ),
            },
        )
        message.update()

    def _save_zulip_processed_message(self, command):
        message_uuid = self._uuid_value(command.message_uuid)
        existing = models.ZulipProcessedEntity.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(command.external_account.project_id),
                "server_url": dm_filters.EQ(
                    command.external_account.server_url,
                ),
                "entity_type": dm_filters.EQ("message"),
                "entity_id": dm_filters.EQ(str(command.zulip_message_id)),
            },
        )
        if existing is not None:
            return
        processed = models.ZulipProcessedEntity(
            uuid=sys_uuid.uuid4(),
            project_id=command.external_account.project_id,
            server_url=command.external_account.server_url,
            entity_type="message",
            entity_id=str(command.zulip_message_id),
            workspace_uuid=message_uuid,
        )
        processed.insert()

    def _handle_zulip_message_sent(self, command):
        self._update_sent_zulip_message_source(command)
        self._save_zulip_processed_message(command)
        state = self._get_outbound_state_for_command(command)
        if state is not None:
            self._set_outbound_done(state)

    def _handle_zulip_outbound_done(self, command):
        state = self._get_outbound_state_for_command(command)
        if state is not None:
            self._set_outbound_done(state)

    def _handle_zulip_message_failed(self, command):
        state = self._get_outbound_state_for_command(command)
        if state is not None:
            self._set_outbound_retry(state, command.error)

    def _handle_zulip_outbound_result_error(self, command, exc):
        LOG.exception(
            "Failed to handle Zulip outbound result for epoch %s",
            command.epoch_version,
        )
        state = self._get_outbound_state_for_command(command)
        if state is not None:
            self._set_outbound_failed(state, str(exc))

    def _execute_sync_command(self, command):
        if isinstance(command, workers.UpdateZulipQueueState):
            self._update_zulip_queue_state(command)
            return None
        if isinstance(command, workers.ZulipQueueFailed):
            self._handle_zulip_queue_failed(command)
            return None
        if isinstance(command, workers.FinishZulipMessageCatchUp):
            self._finish_zulip_message_catch_up(command)
            return None
        if isinstance(command, workers.ZulipMessageSent):
            try:
                self._handle_zulip_message_sent(command)
            except Exception as exc:
                self._handle_zulip_outbound_result_error(command, exc)
            return None
        if isinstance(
            command,
            (
                workers.ZulipMessageUpdated,
                workers.ZulipMessageDeleted,
            ),
        ):
            try:
                self._handle_zulip_outbound_done(command)
            except Exception as exc:
                self._handle_zulip_outbound_result_error(command, exc)
            return None
        if isinstance(command, workers.ZulipMessageFailed):
            self._handle_zulip_message_failed(command)
            return None
        result = command.execute(cache=self._cache)
        if isinstance(command, workers.AddMessage):
            self._update_zulip_queue_state_for_message(command)
        if isinstance(command, workers.SyncStreamsFinished):
            self._clear_zulip_stream_sync(command.event_owner)
            LOG.info(
                "Finished Zulip stream sync for event owner %s",
                command.event_owner,
            )
        return result

    def _handle_zulip_queue_failed(self, command):
        worker_key = self._get_zulip_worker_key_by_event_owner(
            command.event_owner,
        )
        if worker_key is None:
            return
        self._clear_zulip_message_sync(worker_key)
        self._request_zulip_queue_recreate(worker_key)

    def _finish_zulip_message_catch_up(self, command):
        self._update_external_account_zulip_queue_state(
            external_account=command.external_account,
            last_message_id=command.last_message_id,
            is_synced=True,
        )

    def _update_zulip_queue_state_for_message(self, command):
        self._update_external_account_zulip_queue_state(
            external_account=command.external_account,
            last_event_id=command.event_id,
            last_message_id=command.message["id"],
        )

    def _update_zulip_queue_state(self, command):
        values = {
            "external_account": command.external_account,
            "last_event_id": command.last_event_id,
            "last_message_id": command.last_message_id,
            "is_synced": command.is_synced,
        }
        if command.queue_id is not workers.NO_VALUE:
            values["queue_id"] = command.queue_id
        self._update_external_account_zulip_queue_state(**values)
        if (
            command.queue_id is not workers.NO_VALUE and
            command.queue_id is not None
        ):
            self._clear_zulip_queue_recreate(command.event_owner)

    def _update_external_account_zulip_queue_state(
        self,
        external_account,
        queue_id=NO_VALUE,
        last_event_id=None,
        last_message_id=None,
        is_synced=None,
    ):
        queue_state = self._get_or_create_zulip_queue_state(
            external_account,
        )
        changed = False
        queue_id_changed = (
            queue_id is not NO_VALUE and
            queue_id != queue_state.queue_id
        )
        if queue_id_changed:
            queue_state.queue_id = queue_id
            changed = True
        if last_event_id is not None and (
            queue_id_changed or last_event_id > queue_state.last_event_id
        ):
            queue_state.last_event_id = last_event_id
            changed = True
        if (
            last_message_id is not None and
            last_message_id > queue_state.last_message_id
        ):
            queue_state.last_message_id = last_message_id
            changed = True
        if is_synced is not None and is_synced != queue_state.is_synced:
            queue_state.is_synced = is_synced
            changed = True
        if not changed:
            return
        queue_state.update_dm(
            values={
                "queue_id": queue_state.queue_id,
                "last_event_id": queue_state.last_event_id,
                "last_message_id": queue_state.last_message_id,
                "is_synced": queue_state.is_synced,
            },
        )
        queue_state.update()
        LOG.info(
            "Updated Zulip queue state for %s: queue=%s event=%s message=%s",
            external_account.server_url,
            queue_state.queue_id,
            queue_state.last_event_id,
            queue_state.last_message_id,
        )

    def _log_sync_queue_length(self, force=False):
        stats = self._get_sync_queue_stats()
        if not force and stats == self._last_sync_queue_stats:
            return
        self._last_sync_queue_stats = stats
        LOG.info(
            "Zulip sync queue length: queued=%s postponed=%s total=%s",
            stats[0],
            stats[1],
            stats[2],
        )

    def _release_postponed_commands(self):
        if not self._postponed_commands:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        postponed_commands = []
        released_count = 0
        for retry_at, command in self._postponed_commands:
            if retry_at > now:
                postponed_commands.append((retry_at, command))
                continue
            self._put_sync_command(command)
            released_count += 1

        self._postponed_commands = postponed_commands
        if released_count:
            LOG.info(
                "Released %s postponed Zulip sync commands",
                released_count,
            )
            self._log_sync_queue_length(force=True)

    def _process_sync_queue(self):
        handled_count = 0
        processed_count = 0
        retry_commands = []
        deferred_commands = []
        self._release_postponed_commands()
        self._log_sync_queue_length()
        try:
            while handled_count < self._sync_queue_batch_limit:
                try:
                    response = self._sync_queue.get(
                        timeout=SYNC_QUEUE_TIMEOUT,
                    )
                except queue.Empty:
                    self._clear_finished_workers()
                    self._release_postponed_commands()
                    return

                command = workers.get_sync_response_command(response)
                handled_count += 1
                if not self._can_process_sync_command(command):
                    deferred_commands.append(command)
                    continue
                processed_count += 1
                try:
                    stream = self._execute_sync_command(command)
                except SyncStreamsNeeded as exc:
                    retry_at = (
                        datetime.datetime.now(datetime.timezone.utc) +
                        RETRY_COMMAND_DELAY
                    )
                    LOG.warning("%s", exc)
                    self._request_zulip_stream_sync(command)
                    retry_commands.append((retry_at, command))
                    continue
                except RetryCommandLater as exc:
                    retry_at = (
                        datetime.datetime.now(datetime.timezone.utc) +
                        RETRY_COMMAND_DELAY
                    )
                    LOG.warning("%s", exc)
                    retry_commands.append((retry_at, command))
                    continue
                LOG.debug(
                    "Processed workspace integration command: %s",
                    stream,
                )
            LOG.info(
                "Zulip sync queue batch limit reached: "
                "handled=%s processed=%s limit=%s",
                handled_count,
                processed_count,
                self._sync_queue_batch_limit,
            )
        finally:
            self._postponed_commands.extend(retry_commands)
            for command in deferred_commands:
                self._put_sync_command(command)
            if retry_commands:
                LOG.info(
                    "Postponed %s Zulip sync commands for retry",
                    len(retry_commands),
                )
                self._log_sync_queue_length(force=True)
            if deferred_commands:
                LOG.info(
                    "Deferred %s Zulip sync commands until stream sync "
                    "is finished",
                    len(deferred_commands),
                )
                self._log_sync_queue_length(force=True)
            if handled_count:
                LOG.info(
                    (
                        "Handled %s Zulip sync commands in current batch, "
                        "processed %s"
                    ),
                    handled_count,
                    processed_count,
                )
                self._log_sync_queue_length(force=True)

    def _request_worker_group_stop(self, workers_map, input_queues):
        for worker_key, worker in list(workers_map.items()):
            worker.stop()
            input_queue = input_queues.get(worker_key)
            if input_queue is None:
                continue
            try:
                input_queue.put_nowait(workers.StopWorker())
            except queue.Full:
                LOG.warning(
                    "Zulip worker input queue %s is full during shutdown",
                    worker_key,
                )

    def _request_workers_stop(self):
        self._request_worker_group_stop(
            self._workers,
            self._worker_input_queues,
        )
        self._request_worker_group_stop(
            self._history_workers,
            self._worker_history_input_queues,
        )

    def _join_stopped_workers(self):
        for worker in list(self._workers.values()):
            if hasattr(worker, "join"):
                worker.join(timeout=0.5)
        for worker in list(self._history_workers.values()):
            if hasattr(worker, "join"):
                worker.join(timeout=0.5)
        self._clear_finished_workers()

    def _drain_sync_queue_for_shutdown(self):
        while True:
            try:
                response = self._sync_queue.get_nowait()
            except queue.Empty:
                return
            command = workers.get_sync_response_command(response)
            try:
                self._execute_sync_command(command)
            except RetryCommandLater as exc:
                retry_at = (
                    datetime.datetime.now(datetime.timezone.utc) +
                    RETRY_COMMAND_DELAY
                )
                LOG.warning("%s", exc)
                self._postponed_commands.append((retry_at, command))

    def _shutdown_iteration(self):
        self._request_workers_stop()
        self._join_stopped_workers()
        self._drain_sync_queue_for_shutdown()
        if (
            self._workers or
            self._history_workers or
            not self._sync_queue.empty()
        ):
            return
        super().stop()

    def _run_iteration(self):
        LOG.debug("Workspace integration bridge worker iteration")
        if self._stopping:
            self._shutdown_iteration()
            return
        self._sync_iam_users()
        self._sync_zulip_users()
        self._start_bridges()
        self._dispatch_zulip_outbound_events()
        self._process_sync_queue()
