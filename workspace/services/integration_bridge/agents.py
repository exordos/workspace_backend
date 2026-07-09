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
import email.message
import hashlib
import io
import logging
import mimetypes
import queue
import re
import types
import urllib.parse
import uuid as sys_uuid

import filetype
import requests
from PIL import Image as pil_image
from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.common.clients import zulip as zulip_client
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.services.integration_bridge import workers

LOG = logging.getLogger(__name__)
SYNC_QUEUE_TIMEOUT = 0.01
RETRY_COMMAND_DELAY = datetime.timedelta(seconds=30)
DEFAULT_SYNC_QUEUE_BATCH_LIMIT = 100
DEFAULT_HISTORY_SYNC_TASK_BATCH_LIMIT = 1
DEFAULT_OUTBOUND_EVENTS_BATCH_LIMIT = 100
ZULIP_HISTORY_SYNC_TASK_CHUNK_SIZE = 100
ZULIP_HISTORY_SYNC_RETRY_CHUNK_SIZE = 10
NO_VALUE = object()
OUTBOUND_EVENT_ACTIONS = ("created", "updated", "deleted")
OUTBOUND_PENDING_STATUS = "pending"
OUTBOUND_PROCESSING_STATUS = "processing"
OUTBOUND_SENT_STATUS = "sent"
OUTBOUND_SKIPPED_STATUS = "skipped"
OUTBOUND_FAILED_STATUS = "failed"
HISTORY_TASK_PENDING_STATUS = "pending"
HISTORY_TASK_DONE_STATUS = "done"
HISTORY_TASK_FAILED_STATUS = "failed"
DEFAULT_FILE_CONTENT_TYPE = "application/octet-stream"
ZULIP_FILE_IMPORT_FAILED_URN = "urn:zulip-file:download-failed"
ZULIP_UPLOAD_PATH = "/user_uploads/"
ZULIP_FILE_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<name>[^\]]*)\]\((?P<url>[^)\s]+)\)"
)
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
WHERE e.epoch_version > %s
  AND (
      (
          e.object_type = 'message'
          AND e.action IN ('created', 'updated', 'deleted')
          AND e.payload->>'source_name' = 'zulip'
          AND e.payload ? 'author_uuid'
          AND e.payload ? 'source'
          AND e.user_uuid::text = e.payload->>'author_uuid'
          AND (
              e.action <> 'created'
              OR e.payload #>> '{source,message_id}' IS NULL
          )
      )
      OR (
          e.object_type = 'message_reaction'
          AND e.action IN ('created', 'updated', 'deleted')
          AND e.payload->>'source_name' = 'zulip'
          AND e.payload ? 'user_uuid'
          AND e.payload ? 'source'
          AND e.user_uuid::text = e.payload->>'user_uuid'
      )
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
        self._message_raw_contents = {}
        self._processed_entities = {}
        self._seen_subscription_entities = set()
        self._user_uuids = {}
        self._stream_bindings = set()
        self._file_import_errors = []

    def _subscription_cache_owner(self, external_account):
        return workers.get_event_owner(external_account)

    def _subscription_seen_key(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        return (
            self._subscription_cache_owner(external_account),
            entity_type,
            str(entity_id),
        )

    def _has_seen_subscription_entity(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        return (
            self._subscription_seen_key(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=entity_id,
            ) in self._seen_subscription_entities
        )

    def _mark_seen_subscription_entity(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        self._seen_subscription_entities.add(
            self._subscription_seen_key(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=entity_id,
            ),
        )

    def reset_subscription_cache(self, external_account):
        owner = self._subscription_cache_owner(external_account)
        self._seen_subscription_entities = {
            cache_key for cache_key in self._seen_subscription_entities
            if cache_key[0] != owner
        }
        self._clear_project_server_cache(
            self._streams,
            external_account=external_account,
        )
        self._clear_project_server_cache(
            self._topics,
            external_account=external_account,
        )
        self._clear_project_server_cache(
            self._messages,
            external_account=external_account,
        )
        self._clear_project_server_cache(
            self._user_uuids,
            external_account=external_account,
        )
        self._stream_bindings = {
            cache_key for cache_key in self._stream_bindings
            if cache_key[0] != external_account.project_id
        }

    def _clear_project_server_cache(self, cache, external_account):
        for cache_key in list(cache):
            if (
                cache_key[0] == external_account.project_id and
                cache_key[1] == external_account.server_url
            ):
                cache.pop(cache_key, None)

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

    def _processed_entity_filters(
        self,
        external_account,
        entity_type,
        entity_id,
    ):
        return {
            "project_id": dm_filters.EQ(external_account.project_id),
            "server_url": dm_filters.EQ(external_account.server_url),
            "entity_type": dm_filters.EQ(entity_type),
            "entity_id": dm_filters.EQ(str(entity_id)),
        }

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
            filters=self._processed_entity_filters(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=entity_id,
            ),
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

    def _forget_processed_entity_cache(
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
        self._processed_entities.pop(cache_key, None)

    def _log_stale_processed_entity(
        self,
        external_account,
        entity_type,
        entity_id,
        workspace_uuid,
    ):
        LOG.warning(
            (
                "Forget stale Zulip %s %s from %s mapped to missing "
                "workspace %s"
            ),
            entity_type,
            entity_id,
            external_account.server_url,
            workspace_uuid,
        )
        self._forget_processed_entity_cache(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    def _save_processed_entity(
        self,
        external_account,
        entity_type,
        entity_id,
        workspace_uuid,
    ):
        existing = models.ZulipProcessedEntity.objects.get_one_or_none(
            filters=self._processed_entity_filters(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=entity_id,
            ),
        )
        if existing is not None:
            if existing.workspace_uuid != workspace_uuid:
                existing.update_dm(
                    values={
                        "workspace_uuid": workspace_uuid,
                    },
                )
                existing.update()
            cache_key = self._processed_entity_cache_key(
                external_account=external_account,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            self._processed_entities[cache_key] = workspace_uuid
            LOG.info(
                "Updated processed Zulip %s %s from %s to workspace %s",
                entity_type,
                entity_id,
                external_account.server_url,
                workspace_uuid,
            )
            return

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
            stream = models.WorkspaceStream.objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(processed_uuid),
                    "project_id": dm_filters.EQ(external_account.project_id),
                },
            )
            if stream is None:
                self._log_stale_processed_entity(
                    external_account=external_account,
                    entity_type=entity_type,
                    entity_id=stream_id,
                    workspace_uuid=processed_uuid,
                )
            else:
                self._streams[cache_key] = stream

        if cache_key not in self._streams:
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
        if self._has_seen_subscription_entity(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=stream_id,
        ):
            return stream
        self._sync_stream_info(
            external_account=external_account,
            stream=stream,
            stream_info=stream_info,
        )
        self._bind_stream_users(
            external_account=external_account,
            stream=stream,
            subscriber_ids=stream_info["subscriber_ids"],
        )
        self._mark_seen_subscription_entity(
            external_account=external_account,
            entity_type=entity_type,
            entity_id=stream_id,
        )
        return stream

    def pop_file_import_errors(self):
        errors = self._file_import_errors
        self._file_import_errors = []
        return errors

    def _sync_stream_info(self, external_account, stream, stream_info):
        values = {}
        if (
            hasattr(stream, "name") and
            stream.name != stream_info["display_recipient"]
        ):
            values["name"] = stream_info["display_recipient"]
        if (
            hasattr(stream, "description") and
            stream.description != stream_info["description"]
        ):
            values["description"] = stream_info["description"]
        if (
            hasattr(stream, "invite_only") and
            stream.invite_only != stream_info["invite_only"]
        ):
            values["invite_only"] = stream_info["invite_only"]
        if (
            hasattr(stream, "announce") and
            stream.announce != stream_info["announce"]
        ):
            values["announce"] = stream_info["announce"]
        if (
            hasattr(stream, "is_archived") and
            stream.is_archived != stream_info["is_archived"]
        ):
            values["is_archived"] = stream_info["is_archived"]
        if not values:
            return
        messenger_dm_helpers.update_workspace_user_stream(
            project_id=external_account.project_id,
            user_uuid=stream.user_uuid,
            stream_uuid=stream.uuid,
            values=values,
        )

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

    def _get_or_create_zulip_message_sender_uuid(
        self,
        external_account,
        message_info,
    ):
        user_id = message_info["sender_id"]
        try:
            return self._get_required_zulip_user_uuid(
                external_account=external_account,
                user_id=user_id,
            )
        except RetryCommandLater:
            pass

        if (
            "sender_email" not in message_info or
            "sender_full_name" not in message_info
        ):
            raise RetryCommandLater(
                "Postpone Zulip item because user %s was not resolved" %
                user_id,
            )

        workspace_user = models.WorkspaceUser.objects.get_one_or_none(
            filters={
                "email": dm_filters.EQ(message_info["sender_email"]),
            },
        )
        if workspace_user is None:
            workspace_user = models.WorkspaceUser(
                uuid=sys_uuid.uuid4(),
                username=message_info["sender_full_name"],
                source=models.WorkspaceUserSource.ZULIP.value,
                email=message_info["sender_email"],
            )
            workspace_user.insert()

        cache_key = (
            external_account.project_id,
            external_account.server_url,
            user_id,
        )
        self._user_uuids[cache_key] = workspace_user.uuid
        return workspace_user.uuid

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

    def _bind_stream_user(self, external_account, stream, user_uuid):
        if user_uuid == stream.user_uuid:
            return
        cache_key = (
            external_account.project_id,
            stream.uuid,
            user_uuid,
        )
        if cache_key in self._stream_bindings:
            return
        self._stream_bindings.add(cache_key)
        messenger_dm_helpers.get_or_create_workspace_stream_binding(
            project_id=external_account.project_id,
            stream_uuid=stream.uuid,
            user_uuid=user_uuid,
            who_uuid=stream.user_uuid,
            role=models.WorkspaceStreamRole.MEMBER.value,
        )

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
            topic = models.WorkspaceStreamTopic.objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(processed_uuid),
                    "project_id": dm_filters.EQ(
                        external_account.project_id,
                    ),
                    "stream_uuid": dm_filters.EQ(stream.uuid),
                },
            )
            if topic is None:
                self._log_stale_processed_entity(
                    external_account=external_account,
                    entity_type="topic",
                    entity_id=entity_id,
                    workspace_uuid=processed_uuid,
                )
            else:
                self._topics[cache_key] = topic

        if cache_key not in self._topics:
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
        topic = self._topics[cache_key]
        if self._has_seen_subscription_entity(
            external_account=external_account,
            entity_type="topic",
            entity_id=entity_id,
        ):
            return topic
        self._sync_topic_info(
            external_account=external_account,
            stream=stream,
            topic=topic,
            topic_name=topic_name,
        )
        self._mark_seen_subscription_entity(
            external_account=external_account,
            entity_type="topic",
            entity_id=entity_id,
        )
        return topic

    def _sync_topic_info(self, external_account, stream, topic, topic_name):
        if not hasattr(topic, "name") or topic.name == topic_name:
            return
        messenger_dm_helpers.update_workspace_user_stream_topic(
            project_id=external_account.project_id,
            user_uuid=stream.user_uuid,
            topic_uuid=topic.uuid,
            values={"name": topic_name},
        )

    def preprocess_zulip_message(self, external_account, stream, message_info):
        content = message_info["content"]
        processed_content = ZULIP_FILE_LINK_RE.sub(
            lambda match: self._preprocess_zulip_file_link(
                external_account=external_account,
                stream=stream,
                message_info=message_info,
                match=match,
            ),
            content,
        )
        if processed_content == content:
            return message_info
        processed_info = dict(message_info)
        processed_info["content"] = processed_content
        return processed_info

    def _preprocess_zulip_file_link(
        self,
        external_account,
        stream,
        message_info,
        match,
    ):
        url = match.group("url")
        if not self._is_zulip_file_url(
            external_account=external_account,
            url=url,
        ):
            return match.group(0)
        try:
            return self._download_zulip_file_link(
                external_account=external_account,
                stream=stream,
                message_info=message_info,
                match=match,
            )
        except Exception as exc:
            error = (
                "Failed to import Zulip file %s for message %s: %s" %
                (url, message_info["message_id"], exc)
            )
            LOG.exception("%s", error)
            self._file_import_errors.append(error)
            return self._build_zulip_file_import_failed_link(
                match=match,
                url=url,
            )

    def _build_zulip_file_import_failed_link(self, match, url):
        file_name = self._get_zulip_file_name(match=match, url=url)
        query = urllib.parse.urlencode([
            ("name", file_name),
            ("source_url", url),
            ("status", "download_failed"),
        ])
        prefix = "!" if match.group("bang") else ""
        return f"{prefix}[{file_name}]({ZULIP_FILE_IMPORT_FAILED_URN}?{query})"

    def _is_zulip_file_url(self, external_account, url):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme:
            server = urllib.parse.urlparse(external_account.server_url)
            return (
                parsed.netloc == server.netloc and
                parsed.path.startswith(ZULIP_UPLOAD_PATH)
            )
        return parsed.path.startswith(ZULIP_UPLOAD_PATH)

    def _download_zulip_file_link(
        self,
        external_account,
        stream,
        message_info,
        match,
    ):
        url = match.group("url")
        file_name = self._get_zulip_file_name(match=match, url=url)
        response = self._download_zulip_file(
            external_account=external_account,
            url=url,
        )
        data = response["content"]
        metadata = self._get_zulip_file_metadata(
            file_name=file_name,
            header_content_type=response["content_type"],
            data=data,
            url=url,
        )
        file = self._create_workspace_file_from_zulip(
            external_account=external_account,
            stream=stream,
            message_info=message_info,
            file_name=file_name,
            content_type=metadata["content_type"],
            data=data,
        )
        urn = self._build_workspace_file_urn(
            file=file,
            file_name=file_name,
            metadata=metadata,
        )
        prefix = "!" if urn.startswith("urn:image:") else ""
        return f"{prefix}[{file_name}]({urn})"

    def _get_zulip_file_name(self, match, url):
        name = urllib.parse.unquote(match.group("name").strip())
        if name:
            return name
        path = urllib.parse.urlparse(url).path
        return urllib.parse.unquote(path.rsplit("/", 1)[-1])

    def _download_zulip_file(self, external_account, url):
        credentials = external_account.account_settings.credentials
        client = zulip_client.ZulipClient(endpoint=external_account.server_url)
        return client.download_file_with_api_key(
            login=credentials.login,
            token=credentials.token,
            url=url,
        )

    def _get_zulip_file_metadata(
        self,
        file_name,
        header_content_type,
        data,
        url,
    ):
        image_metadata = self._get_image_file_metadata(data)
        content_type = self._get_zulip_file_content_type(
            file_name=file_name,
            header_content_type=header_content_type,
            data=data,
            image_metadata=image_metadata,
        )
        width = image_metadata.get("width")
        height = image_metadata.get("height")
        if width is None or height is None:
            dimensions = self._get_media_dimensions_from_url(
                content_type=content_type,
                url=url,
            )
            if dimensions is not None:
                width, height = dimensions
        return {
            "content_type": content_type,
            "width": width,
            "height": height,
            "size": len(data),
        }

    def _get_zulip_file_content_type(
        self,
        file_name,
        header_content_type,
        data,
        image_metadata,
    ):
        detected_type = self._detect_file_content_type(data)
        if detected_type is None:
            detected_type = image_metadata.get("content_type")
        parsed_type = self._parse_content_type(header_content_type)
        guessed_type = self._guess_file_content_type(file_name)
        if detected_type is not None:
            return self._select_content_type(
                content_type=detected_type,
                guessed_type=guessed_type,
            )
        return self._select_content_type(
            content_type=parsed_type,
            guessed_type=guessed_type,
        )

    def _select_content_type(self, content_type, guessed_type):
        if (
            guessed_type is not None and
            content_type in (None, DEFAULT_FILE_CONTENT_TYPE)
        ):
            return guessed_type
        return content_type or guessed_type or DEFAULT_FILE_CONTENT_TYPE

    def _detect_file_content_type(self, data):
        detected_type = filetype.guess(data)
        if detected_type is None:
            return None
        return detected_type.mime

    def _get_image_file_metadata(self, data):
        try:
            with pil_image.open(io.BytesIO(data)) as image:
                width, height = image.size
                return {
                    "content_type": pil_image.MIME.get(image.format),
                    "width": width,
                    "height": height,
                }
        except (pil_image.UnidentifiedImageError, OSError):
            return {}

    def _parse_content_type(self, content_type):
        if not content_type:
            return None
        message = email.message.Message()
        message["Content-Type"] = content_type
        return message.get_content_type()

    def _guess_file_content_type(self, file_name):
        guess_file_type = getattr(mimetypes, "guess_file_type", None)
        if guess_file_type is not None:
            return guess_file_type(file_name)[0]
        return mimetypes.guess_type(file_name)[0]

    def _create_workspace_file_from_zulip(
        self,
        external_account,
        stream,
        message_info,
        file_name,
        content_type,
        data,
    ):
        file_uuid = sys_uuid.uuid4()
        storage_info = file_storage.save_workspace_file(
            file_uuid=file_uuid,
            data=data,
        )
        user_uuid = self._get_or_create_zulip_message_sender_uuid(
            external_account=external_account,
            message_info=message_info,
        )
        try:
            return messenger_dm_helpers.create_workspace_file(
                project_id=external_account.project_id,
                user_uuid=user_uuid,
                uuid=file_uuid,
                stream_uuid=stream.uuid,
                name=file_name,
                description="",
                content_type=content_type,
                size_bytes=len(data),
                hash=hashlib.sha256(data).hexdigest(),
                storage_type=storage_info.storage_type,
                storage_id=storage_info.storage_id,
                storage_object_id=storage_info.storage_object_id,
            )
        except Exception:
            file_storage.delete_workspace_file(
                file_uuid=file_uuid,
                storage_type=storage_info.storage_type,
                storage_object_id=storage_info.storage_object_id,
            )
            raise

    def _build_workspace_file_urn(
        self,
        file,
        file_name,
        metadata,
    ):
        content_type = metadata["content_type"]
        file_type = self._get_workspace_file_urn_type(content_type)
        urn_metadata = [
            ("name", file_name),
            ("content_type", content_type),
        ]
        width = metadata["width"]
        height = metadata["height"]
        if width is not None and height is not None:
            urn_metadata.extend([
                ("w", width),
                ("h", height),
            ])
        urn_metadata.append(("size", metadata["size"]))
        query = urllib.parse.urlencode(urn_metadata)
        return f"urn:{file_type}:{file.uuid}?{query}"

    def _get_workspace_file_urn_type(self, content_type):
        maintype = self._get_content_maintype(content_type)
        if maintype in ("image", "video"):
            return maintype
        return "file"

    def _get_media_dimensions_from_url(self, content_type, url):
        maintype = self._get_content_maintype(content_type)
        if maintype not in ("image", "video"):
            return None
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        width = self._get_int_query_value(query, "w")
        height = self._get_int_query_value(query, "h")
        if width is None or height is None:
            return None
        return width, height

    def _get_content_maintype(self, content_type):
        message = email.message.Message()
        message["Content-Type"] = content_type
        return message.get_content_maintype()

    def _get_int_query_value(self, query, name):
        values = query.get(name)
        if not values:
            return None
        value = int(values[0])
        if value <= 0:
            return None
        return value

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
        user_uuid = self._get_or_create_zulip_message_sender_uuid(
            external_account=external_account,
            message_info=message_info,
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
        raw_message_info = message_info
        message_info = None
        if processed_uuid is not None and cache_key not in self._messages:
            message = models.WorkspaceUserMessage.objects.get_one_or_none(
                filters={
                    "uuid": dm_filters.EQ(processed_uuid),
                    "project_id": dm_filters.EQ(external_account.project_id),
                    "user_uuid": dm_filters.EQ(user_uuid),
                },
            )
            if message is None:
                self._log_stale_processed_entity(
                    external_account=external_account,
                    entity_type="message",
                    entity_id=entity_id,
                    workspace_uuid=processed_uuid,
                )
            else:
                self._messages[cache_key] = message

        if cache_key not in self._messages:
            message_info = self.preprocess_zulip_message(
                external_account=external_account,
                stream=stream,
                message_info=raw_message_info,
            )
            source = self._build_zulip_message_source(
                external_account=external_account,
                stream_info=stream_info,
                topic_name=topic_name,
                message_info=message_info,
            )
            self._bind_stream_user(
                external_account=external_account,
                stream=stream,
                user_uuid=user_uuid,
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
        if self._has_seen_subscription_entity(
            external_account=external_account,
            entity_type="message",
            entity_id=entity_id,
        ):
            return message
        if not self._is_message_raw_content_synced(
            external_account=external_account,
            message_info=raw_message_info,
        ):
            if message_info is None:
                message_info = self.preprocess_zulip_message(
                    external_account=external_account,
                    stream=stream,
                    message_info=raw_message_info,
                )
            message = self._sync_message_info(
                external_account=external_account,
                message=message,
                message_info=message_info,
            )
            self._messages[cache_key] = message
            self._mark_message_raw_content_synced(
                external_account=external_account,
                message_info=raw_message_info,
            )
        else:
            message_info = raw_message_info
        self._sync_message_read_flag(
            external_account=external_account,
            message=message,
            message_info=message_info,
        )
        self._mark_seen_subscription_entity(
            external_account=external_account,
            entity_type="message",
            entity_id=entity_id,
        )
        return message

    def _get_message_content(self, message):
        payload = getattr(message, "payload", None)
        if payload is None:
            return None
        if hasattr(payload, "content"):
            return payload.content
        return payload.get("content")

    def _sync_message_info(self, external_account, message, message_info):
        content = self._get_message_content(message)
        if content is None or content == message_info["content"]:
            return message
        before_epoch = self._get_last_workspace_event_epoch(
            external_account.project_id,
        )
        result = messenger_dm_helpers.update_workspace_user_message(
            project_id=external_account.project_id,
            user_uuid=message.user_uuid,
            message_uuid=message.uuid,
            values={
                "payload": message_payloads.MarkdownPayload(
                    content=message_info["content"],
                ),
            },
        )
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message",
            action="updated",
            payload_uuid=message.uuid,
            after_epoch=before_epoch,
        )
        return result

    def _get_message_raw_content_cache_key(self, external_account, message_id):
        return (
            external_account.project_id,
            external_account.server_url,
            message_id,
        )

    def _is_message_raw_content_synced(self, external_account, message_info):
        cache_key = self._get_message_raw_content_cache_key(
            external_account=external_account,
            message_id=message_info["message_id"],
        )
        return (
            self._message_raw_contents.get(cache_key) ==
            message_info["content"]
        )

    def _mark_message_raw_content_synced(self, external_account, message_info):
        cache_key = self._get_message_raw_content_cache_key(
            external_account=external_account,
            message_id=message_info["message_id"],
        )
        self._message_raw_contents[cache_key] = message_info["content"]

    def _get_zulip_workspace_message(self, external_account, message_id):
        message_uuid = self._get_processed_workspace_uuid(
            external_account=external_account,
            entity_type="message",
            entity_id=message_id,
        )
        if message_uuid is None:
            return None
        return models.WorkspaceMessage.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "uuid": dm_filters.EQ(message_uuid),
            },
        )

    def _get_last_workspace_event_epoch(self, project_id):
        events = models.WorkspaceEvent.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(project_id),
            },
            order_by={"epoch_version": "desc"},
            limit=1,
        )
        if not events:
            return 0
        return events[0].epoch_version

    def _mark_inbound_zulip_events_skipped(
        self,
        external_account,
        object_type,
        action,
        payload_uuid,
        after_epoch,
    ):
        events = models.WorkspaceEvent.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "object_type": dm_filters.EQ(object_type),
                "action": dm_filters.EQ(action),
                "epoch_version": dm_filters.GT(after_epoch),
            },
            order_by={"epoch_version": "asc"},
        )
        for event in events:
            if event.payload["uuid"] != str(payload_uuid):
                continue
            state = models.ZulipOutboundEventState.objects.get_one_or_none(
                filters={
                    "epoch_version": dm_filters.EQ(event.epoch_version),
                },
            )
            if state is not None:
                continue
            state = models.ZulipOutboundEventState(
                uuid=sys_uuid.uuid4(),
                project_id=external_account.project_id,
                epoch_version=event.epoch_version,
                external_account_uuid=external_account.uuid,
                status=OUTBOUND_SKIPPED_STATUS,
            )
            state.insert()

    def update_message(self, external_account, message_info):
        message = self._get_zulip_workspace_message(
            external_account=external_account,
            message_id=message_info["message_id"],
        )
        if message is None:
            return None
        message_info = self.preprocess_zulip_message(
            external_account=external_account,
            stream=types.SimpleNamespace(uuid=message.stream_uuid),
            message_info=message_info,
        )
        if self._get_message_content(message) == message_info["content"]:
            return message
        before_epoch = self._get_last_workspace_event_epoch(
            external_account.project_id,
        )
        result = messenger_dm_helpers.update_workspace_user_message(
            project_id=external_account.project_id,
            user_uuid=message.user_uuid,
            message_uuid=message.uuid,
            values={
                "payload": message_payloads.MarkdownPayload(
                    content=message_info["content"],
                ),
            },
        )
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            message_info["message_id"],
        )
        self._messages[cache_key] = result
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message",
            action="updated",
            payload_uuid=message.uuid,
            after_epoch=before_epoch,
        )
        return result

    def delete_messages(self, external_account, message_ids):
        results = []
        for message_id in message_ids:
            message = self._get_zulip_workspace_message(
                external_account=external_account,
                message_id=message_id,
            )
            if message is None:
                continue
            before_epoch = self._get_last_workspace_event_epoch(
                external_account.project_id,
            )
            messenger_dm_helpers.delete_workspace_user_message(
                project_id=external_account.project_id,
                user_uuid=message.user_uuid,
                message_uuid=message.uuid,
            )
            cache_key = (
                external_account.project_id,
                external_account.server_url,
                message_id,
            )
            self._messages.pop(cache_key, None)
            self._mark_inbound_zulip_events_skipped(
                external_account=external_account,
                object_type="message",
                action="deleted",
                payload_uuid=message.uuid,
                after_epoch=before_epoch,
            )
            results.append(message)
        return results

    def _get_message_reaction(self, external_account, reaction_info):
        message = self._get_zulip_workspace_message(
            external_account=external_account,
            message_id=reaction_info["message_id"],
        )
        if message is None:
            return None, None, None
        user_uuid = self._get_required_zulip_user_uuid(
            external_account=external_account,
            user_id=reaction_info["user_id"],
        )
        reaction = models.WorkspaceMessageReactions.objects.get_one_or_none(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "message_uuid": dm_filters.EQ(message.uuid),
                "user_uuid": dm_filters.EQ(user_uuid),
                "emoji_name": dm_filters.EQ(reaction_info["emoji_name"]),
            },
        )
        return message, user_uuid, reaction

    def add_message_reaction(self, external_account, reaction_info):
        message, user_uuid, reaction = self._get_message_reaction(
            external_account=external_account,
            reaction_info=reaction_info,
        )
        if message is None:
            return None
        if reaction is not None:
            return reaction
        before_epoch = self._get_last_workspace_event_epoch(
            external_account.project_id,
        )
        reaction = messenger_dm_helpers.create_workspace_message_reaction(
            project_id=external_account.project_id,
            user_uuid=user_uuid,
            message_uuid=message.uuid,
            emoji_name=reaction_info["emoji_name"],
        )
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message_reaction",
            action="created",
            payload_uuid=reaction.uuid,
            after_epoch=before_epoch,
        )
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message",
            action="updated",
            payload_uuid=message.uuid,
            after_epoch=before_epoch,
        )
        return reaction

    def remove_message_reaction(self, external_account, reaction_info):
        message, user_uuid, reaction = self._get_message_reaction(
            external_account=external_account,
            reaction_info=reaction_info,
        )
        if message is None or reaction is None:
            return None
        before_epoch = self._get_last_workspace_event_epoch(
            external_account.project_id,
        )
        messenger_dm_helpers.delete_workspace_message_reaction(
            project_id=external_account.project_id,
            user_uuid=user_uuid,
            reaction_uuid=reaction.uuid,
        )
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message_reaction",
            action="deleted",
            payload_uuid=reaction.uuid,
            after_epoch=before_epoch,
        )
        self._mark_inbound_zulip_events_skipped(
            external_account=external_account,
            object_type="message",
            action="updated",
            payload_uuid=message.uuid,
            after_epoch=before_epoch,
        )
        return reaction

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
        history_sync_task_batch_limit=DEFAULT_HISTORY_SYNC_TASK_BATCH_LIMIT,
        **kwargs
    ):
        super().__init__(**kwargs)
        self._sync_queue_batch_limit = sync_queue_batch_limit
        self._history_sync_task_batch_limit = history_sync_task_batch_limit
        self._workers = {}
        self._worker_input_queues = {}
        self._worker_accounts = {}
        self._sync_queue = queue.PriorityQueue(
            maxsize=workers.MAX_SYNC_QUEUE_SIZE,
        )
        self._postponed_commands = []
        self._last_sync_queue_stats = None
        self._message_sync_worker_keys = set()
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
                self._message_sync_worker_keys.discard(worker_key)
                self._queue_recreate_worker_keys.discard(worker_key)
        for worker_key in list(self._worker_accounts):
            if worker_key not in self._workers:
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
        for user in all_users:
            if user.account_settings.credentials is None:
                continue
            worker_key = self._get_zulip_worker_key(user)
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
            self._request_zulip_message_sync(worker_key)

    def _get_zulip_worker_key(self, external_account):
        return (
            external_account.project_id,
            external_account.server_url,
            external_account.user_uuid,
        )

    def _clear_zulip_message_sync(self, worker_key):
        self._message_sync_worker_keys.discard(worker_key)

    def _clear_zulip_queue_recreate(self, worker_key):
        self._queue_recreate_worker_keys.discard(worker_key)

    def _ensure_zulip_queue_subscription_version(
        self,
        external_account,
        queue_state,
    ):
        subscription_version = (
            zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
        )
        if queue_state.subscription_version == subscription_version:
            return queue_state
        LOG.info(
            (
                "Reset Zulip queue for %s because subscription version "
                "changed from %s to %s"
            ),
            external_account.server_url,
            queue_state.subscription_version,
            subscription_version,
        )
        queue_state.queue_id = None
        queue_state.last_event_id = -1
        queue_state.is_synced = False
        queue_state.subscription_version = subscription_version
        queue_state.update_dm(
            values={
                "queue_id": queue_state.queue_id,
                "last_event_id": queue_state.last_event_id,
                "last_message_id": queue_state.last_message_id,
                "is_synced": queue_state.is_synced,
                "subscription_version": queue_state.subscription_version,
            },
        )
        queue_state.update()
        return queue_state

    def _get_or_create_zulip_queue_state(self, external_account):
        queue_state = models.ZulipEventQueueState.objects.get_one_or_none(
            filters={
                "external_account_uuid": dm_filters.EQ(
                    external_account.uuid,
                ),
            },
        )
        if queue_state is not None:
            return self._ensure_zulip_queue_subscription_version(
                external_account=external_account,
                queue_state=queue_state,
            )

        queue_state = models.ZulipEventQueueState(
            uuid=sys_uuid.uuid4(),
            project_id=external_account.project_id,
            external_account_uuid=external_account.uuid,
            server_url=external_account.server_url,
            user_uuid=external_account.user_uuid,
            subscription_version=(
                zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
            ),
        )
        queue_state.insert()
        return queue_state

    def _request_zulip_message_sync(self, worker_key):
        if worker_key in self._message_sync_worker_keys:
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
        self._message_sync_worker_keys.add(worker_key)
        input_queue = self._worker_input_queues.get(worker_key)
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
        self._queue_recreate_worker_keys.add(worker_key)
        self._update_external_account_zulip_queue_state(
            external_account=external_account,
            queue_id=None,
            is_synced=False,
        )
        LOG.info(
            "Request Zulip queue recreate for %s",
            external_account.server_url,
        )
        input_queue = self._worker_input_queues.get(worker_key)
        if input_queue is None:
            self._clear_zulip_queue_recreate(worker_key)
            return
        input_queue.put(
            workers.CreateZulipEventQueue(),
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
        event_owner = getattr(
            command,
            "event_owner",
            workers.get_event_owner(command.external_account),
        )
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
        input_queue = self._worker_input_queues.get(worker_key)
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

    def _get_zulip_client(self, external_account):
        return zulip_client.ZulipClient(endpoint=external_account.server_url)

    def _get_zulip_credentials(self, external_account):
        credentials = external_account.account_settings.credentials
        if credentials is None:
            raise RetryCommandLater(
                "Zulip external account credentials are missing",
            )
        return credentials

    def _fetch_zulip_messages(self, external_account, message_filters):
        credentials = self._get_zulip_credentials(external_account)
        message_filters = dict(message_filters)
        message_filters["apply_markdown"] = False
        client = self._get_zulip_client(external_account)
        return client.get_messages_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_filters=message_filters,
        )

    def _fetch_zulip_message(self, external_account, message_id):
        credentials = self._get_zulip_credentials(external_account)
        client = self._get_zulip_client(external_account)
        return client.get_message_with_api_key(
            login=credentials.login,
            token=credentials.token,
            message_id=message_id,
        )

    def _get_zulip_latest_message_id(self, external_account):
        messages = self._fetch_zulip_messages(
            external_account=external_account,
            message_filters={
                "anchor": "newest",
                "num_before": 1,
                "num_after": 0,
            },
        )
        if not messages:
            return 0
        return max(message["id"] for message in messages)

    def _delete_unfinished_zulip_history_sync_tasks(self, external_account):
        for status in (
            HISTORY_TASK_PENDING_STATUS,
            HISTORY_TASK_FAILED_STATUS,
        ):
            tasks = models.ZulipHistorySyncTask.objects.get_all(
                filters={
                    "project_id": dm_filters.EQ(external_account.project_id),
                    "external_account_uuid": dm_filters.EQ(
                        external_account.uuid,
                    ),
                    "status": dm_filters.EQ(status),
                },
            )
            for task in tasks:
                task.delete()

    def _create_zulip_history_sync_task(
        self,
        external_account,
        from_message_id,
        to_message_id,
        status=HISTORY_TASK_PENDING_STATUS,
        last_error=None,
    ):
        task = models.ZulipHistorySyncTask(
            uuid=sys_uuid.uuid4(),
            project_id=external_account.project_id,
            external_account_uuid=external_account.uuid,
            server_url=external_account.server_url,
            user_uuid=external_account.user_uuid,
            from_message_id=from_message_id,
            to_message_id=to_message_id,
            status=status,
            last_error=last_error,
        )
        task.insert()
        return task

    def _rebuild_zulip_history_sync_tasks(self, external_account):
        self._delete_unfinished_zulip_history_sync_tasks(
            external_account=external_account,
        )
        latest_message_id = self._get_zulip_latest_message_id(
            external_account=external_account,
        )
        if latest_message_id <= 0:
            return 0

        created = 0
        from_message_id = 0
        while from_message_id <= latest_message_id:
            to_message_id = min(
                from_message_id + ZULIP_HISTORY_SYNC_TASK_CHUNK_SIZE - 1,
                latest_message_id,
            )
            self._create_zulip_history_sync_task(
                external_account=external_account,
                from_message_id=from_message_id,
                to_message_id=to_message_id,
            )
            created += 1
            from_message_id = to_message_id + 1
        LOG.info(
            "Created %s Zulip history sync tasks for %s up to message %s",
            created,
            external_account.server_url,
            latest_message_id,
        )
        return created

    def _get_next_zulip_history_sync_task_without_error(self):
        tasks = models.ZulipHistorySyncTask.objects.get_all(
            filters={
                "status": dm_filters.EQ(HISTORY_TASK_PENDING_STATUS),
                "last_error": dm_filters.EQ(None),
            },
            order_by={
                "to_message_id": "desc",
                "from_message_id": "desc",
                "created_at": "asc",
            },
            limit=1,
        )
        if not tasks:
            return None
        return tasks[0]

    def _get_next_zulip_history_sync_task(self):
        task = self._get_next_zulip_history_sync_task_without_error()
        if task is not None:
            return task
        tasks = models.ZulipHistorySyncTask.objects.get_all(
            filters={
                "status": dm_filters.EQ(HISTORY_TASK_PENDING_STATUS),
            },
            order_by={
                "to_message_id": "desc",
                "from_message_id": "desc",
                "created_at": "asc",
            },
            limit=1,
        )
        if not tasks:
            return None
        return tasks[0]

    def _get_zulip_history_task_external_account(self, task):
        return models.ExternalAccount.objects.get_one(
            filters={
                "uuid": dm_filters.EQ(task.external_account_uuid),
                "project_id": dm_filters.EQ(task.project_id),
            },
        )

    def _fetch_zulip_history_task_messages(self, external_account, task):
        if task.from_message_id == task.to_message_id:
            message = self._fetch_zulip_message(
                external_account=external_account,
                message_id=task.from_message_id,
            )
            if message is None:
                return []
            return [message]
        anchor = 0
        if task.from_message_id > 0:
            anchor = task.from_message_id - 1
        num_after = task.to_message_id - task.from_message_id + 1
        messages = self._fetch_zulip_messages(
            external_account=external_account,
            message_filters={
                "anchor": anchor,
                "num_before": 0,
                "num_after": num_after,
            },
        )
        return [
            message for message in messages
            if (
                task.from_message_id <= message["id"] <=
                task.to_message_id
            )
        ]

    def _update_zulip_history_sync_task(self, task, status, last_error=None):
        task.update_dm(
            values={
                "status": status,
                "last_error": last_error,
            },
        )
        task.update()

    def _has_pending_zulip_history_sync_tasks(self, external_account):
        tasks = models.ZulipHistorySyncTask.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "external_account_uuid": dm_filters.EQ(external_account.uuid),
                "status": dm_filters.EQ(HISTORY_TASK_PENDING_STATUS),
            },
            limit=1,
        )
        return bool(tasks)

    def _split_zulip_history_sync_task_for_retry(
        self,
        external_account,
        task,
        last_error,
    ):
        task_size = task.to_message_id - task.from_message_id + 1
        if task_size <= 1:
            self._update_zulip_history_sync_task(
                task,
                status=HISTORY_TASK_PENDING_STATUS,
                last_error=last_error,
            )
            return

        chunk_size = ZULIP_HISTORY_SYNC_RETRY_CHUNK_SIZE
        if task_size <= chunk_size:
            chunk_size = 1

        task.delete()
        from_message_id = task.from_message_id
        while from_message_id <= task.to_message_id:
            to_message_id = min(
                from_message_id + chunk_size - 1,
                task.to_message_id,
            )
            self._create_zulip_history_sync_task(
                external_account=external_account,
                from_message_id=from_message_id,
                to_message_id=to_message_id,
                status=HISTORY_TASK_PENDING_STATUS,
                last_error=None,
            )
            from_message_id = to_message_id + 1

    def _sync_zulip_history_task_message(self, external_account, message):
        command = workers.AddMessage(
            external_account=external_account,
            message=message,
        )
        self._cache.pop_file_import_errors()
        self._execute_sync_command(command)
        return self._cache.pop_file_import_errors()

    def _process_zulip_history_sync_task(self):
        task = self._get_next_zulip_history_sync_task()
        if task is None:
            return None

        external_account = self._get_zulip_history_task_external_account(task)
        try:
            messages = self._fetch_zulip_history_task_messages(
                external_account=external_account,
                task=task,
            )
            file_errors = []
            for message in sorted(
                messages,
                key=lambda item: item["id"],
                reverse=True,
            ):
                file_errors.extend(
                    self._sync_zulip_history_task_message(
                        external_account=external_account,
                        message=message,
                    ),
                )
        except SyncStreamsNeeded as exc:
            self._request_zulip_stream_sync(exc)
            self._update_zulip_history_sync_task(
                task,
                status=HISTORY_TASK_PENDING_STATUS,
                last_error=str(exc),
            )
            return False
        except RetryCommandLater as exc:
            self._update_zulip_history_sync_task(
                task,
                status=HISTORY_TASK_PENDING_STATUS,
                last_error=str(exc),
            )
            return False
        except requests.exceptions.RequestException as exc:
            LOG.warning(
                "Postpone Zulip history sync task %s after network error: %s",
                task.uuid,
                exc,
            )
            self._split_zulip_history_sync_task_for_retry(
                external_account=external_account,
                task=task,
                last_error=str(exc),
            )
            return False
        except Exception as exc:
            LOG.exception(
                "Failed to process Zulip history sync task %s",
                task.uuid,
            )
            self._update_zulip_history_sync_task(
                task,
                status=HISTORY_TASK_FAILED_STATUS,
                last_error=str(exc),
            )
            return True

        last_error = None
        if file_errors:
            last_error = "\n".join(file_errors)
        self._update_zulip_history_sync_task(
            task,
            status=HISTORY_TASK_DONE_STATUS,
            last_error=last_error,
        )
        if not self._has_pending_zulip_history_sync_tasks(external_account):
            self._update_external_account_zulip_queue_state(
                external_account=external_account,
                is_synced=True,
            )
        return True

    def _process_zulip_history_sync_tasks(self):
        self._process_zulip_history_sync_task()

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
            "source" not in payload
        ):
            return False
        if payload["source_name"] != models.SourceName.ZULIP.value:
            return False
        if event.object_type == "message":
            if "author_uuid" not in payload:
                return False
            if (
                event.action == "created" and
                self._source_value(payload["source"], "message_id") is not None
            ):
                return False
            return str(event.user_uuid) == str(payload["author_uuid"])
        if event.object_type == "message_reaction":
            if "user_uuid" not in payload:
                return False
            return str(event.user_uuid) == str(payload["user_uuid"])
        return False

    def _get_zulip_outbound_user_uuid(self, event):
        if event.object_type == "message_reaction":
            return event.payload["user_uuid"]
        return event.payload["author_uuid"]

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
                "user_uuid": dm_filters.EQ(
                    self._get_zulip_outbound_user_uuid(event),
                ),
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

    def _get_previous_message_event_payload(self, event):
        result = contexts.Context().get_session().execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND object_type = 'message'
              AND action IN ('created', 'updated')
              AND epoch_version < %s
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (
                event.project_id,
                event.epoch_version,
                event.payload["uuid"],
            ),
        )
        row = result.fetchone()
        if row is None:
            return None
        return row["payload"]

    def _message_payload_content(self, payload):
        return payload["payload"]["content"]

    def _message_content_changed(self, event):
        previous_payload = self._get_previous_message_event_payload(event)
        if previous_payload is None:
            return True
        return (
            self._message_payload_content(previous_payload) !=
            self._message_payload_content(event.payload)
        )

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
        if not self._message_content_changed(event):
            return None
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

    def _get_reaction_message_id(self, event, source_name="source"):
        message_id = self._source_value(event.payload[source_name], "message_id")
        if message_id is None:
            raise RetryCommandLater(
                "Postpone Zulip reaction because message_id is missing",
            )
        return message_id

    def _build_add_zulip_reaction_command(self, event):
        return workers.AddZulipReaction(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["message_uuid"],
            message_id=self._get_reaction_message_id(event),
            emoji_name=event.payload["emoji_name"],
        )

    def _build_remove_zulip_reaction_command(self, event):
        return workers.RemoveZulipReaction(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["message_uuid"],
            message_id=self._get_reaction_message_id(event),
            emoji_name=event.payload["emoji_name"],
        )

    def _old_reaction_source_is_zulip(self, event):
        return event.payload.get("old_source_name") == (
            models.SourceName.ZULIP.value
        )

    def _get_old_reaction_message_id(self, event):
        if not self._old_reaction_source_is_zulip(event):
            return None
        return self._get_reaction_message_id(event, source_name="old_source")

    def _build_update_zulip_reaction_command(self, event):
        return workers.UpdateZulipReaction(
            epoch_version=event.epoch_version,
            message_uuid=event.payload["message_uuid"],
            old_message_id=self._get_old_reaction_message_id(event),
            old_emoji_name=event.payload.get("old_emoji_name"),
            message_id=self._get_reaction_message_id(event),
            emoji_name=event.payload["emoji_name"],
        )

    def _build_zulip_reaction_outbound_command(self, event):
        if event.action == "created":
            return self._build_add_zulip_reaction_command(event)
        if event.action == "updated":
            return self._build_update_zulip_reaction_command(event)
        if event.action == "deleted":
            return self._build_remove_zulip_reaction_command(event)
        return None

    def _build_zulip_outbound_command(self, event):
        if event.object_type == "message_reaction":
            return self._build_zulip_reaction_outbound_command(event)
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
        if isinstance(command, workers.ZulipEventQueueCreated):
            self._handle_zulip_event_queue_created(command)
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
                workers.ZulipReactionAdded,
                workers.ZulipReactionRemoved,
                workers.ZulipReactionUpdated,
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
        if isinstance(
            command,
            (
                workers.AddMessage,
                workers.UpdateMessage,
                workers.DeleteMessage,
                workers.AddMessageReaction,
                workers.RemoveMessageReaction,
            ),
        ):
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

    def _handle_zulip_event_queue_created(self, command):
        worker_key = self._get_zulip_worker_key_by_event_owner(
            command.event_owner,
        )
        self._cache.reset_subscription_cache(command.external_account)
        created_tasks = self._rebuild_zulip_history_sync_tasks(
            external_account=command.external_account,
        )
        if created_tasks == 0:
            self._update_external_account_zulip_queue_state(
                external_account=command.external_account,
                is_synced=True,
            )
        if worker_key is None:
            return
        self._clear_zulip_queue_recreate(worker_key)
        self._request_zulip_message_sync(worker_key)

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
            last_message_id=command.last_message_id,
        )
        self._record_realtime_zulip_history_sync_task(command)

    def _get_realtime_command_message_ids(self, command):
        if hasattr(command, "message_ids"):
            return command.message_ids
        if hasattr(command, "message_id"):
            return [command.message_id]
        if hasattr(command, "last_message_id"):
            return [command.last_message_id]
        return []

    def _record_realtime_zulip_history_sync_task(self, command):
        if getattr(command, "event_id", None) is None:
            return
        message_ids = self._get_realtime_command_message_ids(command)
        if not message_ids:
            return
        file_errors = self._cache.pop_file_import_errors()
        last_error = None
        if file_errors:
            last_error = "\n".join(file_errors)
        self._create_zulip_history_sync_task(
            external_account=command.external_account,
            from_message_id=min(message_ids),
            to_message_id=max(message_ids),
            status=HISTORY_TASK_DONE_STATUS,
            last_error=last_error,
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
        subscription_version = (
            zulip_client.MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION
        )
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
        if queue_state.subscription_version != subscription_version:
            queue_state.subscription_version = subscription_version
            changed = True
        if not changed:
            return
        queue_state.update_dm(
            values={
                "queue_id": queue_state.queue_id,
                "last_event_id": queue_state.last_event_id,
                "last_message_id": queue_state.last_message_id,
                "is_synced": queue_state.is_synced,
                "subscription_version": queue_state.subscription_version,
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
            while True:
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

    def _join_stopped_workers(self):
        for worker in list(self._workers.values()):
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
        if self._workers or not self._sync_queue.empty():
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
        self._process_zulip_history_sync_tasks()
        self._process_sync_queue()
