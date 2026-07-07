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

from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models
from workspace.services.integration_bridge import workers

LOG = logging.getLogger(__name__)
SYNC_QUEUE_TIMEOUT = 0.1


class RetryCommandLater(Exception):
    pass


class WorkspaceIntegrationBridgeCache:
    def __init__(self):
        self._streams = {}
        self._user_uuids = {}
        self._stream_bindings = set()

    def get_or_create_stream(self, external_account, stream_info):
        stream_id = stream_info["stream_id"]
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            stream_info["type"],
            stream_id,
        )
        if cache_key not in self._streams:
            stream = self._load_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )
            self._streams[cache_key] = stream
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

    def _is_matching_stream(self, stream, stream_info):
        return (
            stream.source.stream_id == stream_info["stream_id"] and
            getattr(stream, "private", False) ==
            self._is_private_stream_info(stream_info)
        )

    def _load_or_create_stream(self, external_account, stream_info):
        for stream in models.WorkspaceStream.objects.get_all(
            filters={
                "project_id": dm_filters.EQ(external_account.project_id),
                "source_name": dm_filters.EQ(models.SourceName.ZULIP.value),
            },
        ):
            if self._is_matching_stream(
                stream=stream,
                stream_info=stream_info,
            ):
                return stream
        return self._create_stream(
            external_account=external_account,
            stream_info=stream_info,
        )

    def _get_zulip_user_uuid(self, external_account, user_id):
        cache_key = (
            external_account.project_id,
            external_account.server_url,
            user_id,
        )
        if cache_key not in self._user_uuids:
            self._user_uuids[cache_key] = self._load_zulip_user_uuid(
                external_account=external_account,
                user_id=user_id,
            )
        return self._user_uuids[cache_key]

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
        user_uuid = self._get_zulip_user_uuid(
            external_account=external_account,
            user_id=stream_info["creator_id"],
        )
        values = {
            "project_id": external_account.project_id,
            "user_uuid": user_uuid,
            "name": stream_info["display_recipient"],
            "description": stream_info["description"],
            "source_name": models.SourceName.ZULIP.value,
            "source": models.ZulipSource(stream_id=stream_id),
            "invite_only": stream_info["invite_only"],
            "announce": stream_info["announce"],
            "is_archived": stream_info["is_archived"],
            "default_topic_name": default_topic_name,
        }
        if self._is_private_stream_info(stream_info):
            direct_user_uuid = self._get_private_direct_user_uuid(
                external_account=external_account,
                stream_info=stream_info,
                user_uuid=user_uuid,
            )
            values["direct_user_uuid"] = direct_user_uuid
        return messenger_dm_helpers.get_or_create_workspace_user_stream(
            **values
        )


class WorkspaceIntegrationBridgeWorker(basic.BasicService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._workers = {}
        self._sync_queue = queue.Queue()
        self._cache = WorkspaceIntegrationBridgeCache()

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
        for user_uuid, worker in list(self._workers.items()):
            if hasattr(worker, "is_alive") and not worker.is_alive():
                del self._workers[user_uuid]

    def _has_active_workers(self):
        return any(
            worker.is_alive()
            for worker in self._workers.values()
            if hasattr(worker, "is_alive")
        )

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
            if user.user_uuid in self._workers:
                continue

            worker = workers.ZulipBridgeWorker(
                external_account=user,
                sync_queue=self._sync_queue,
            )
            worker.start()
            self._workers[user.user_uuid] = worker

    def _process_sync_queue(self):
        retry_commands = []
        try:
            while True:
                try:
                    command = self._sync_queue.get(
                        timeout=SYNC_QUEUE_TIMEOUT,
                    )
                except queue.Empty:
                    self._clear_finished_workers()
                    if (
                        not self._has_active_workers() and
                        self._sync_queue.empty()
                    ):
                        return
                    continue

                try:
                    stream = command.execute(cache=self._cache)
                except RetryCommandLater as exc:
                    LOG.warning("%s", exc)
                    retry_commands.append(command)
                    continue
                LOG.debug(
                    "Processed workspace integration command: %s",
                    stream,
                )
        finally:
            for command in retry_commands:
                self._sync_queue.put(command)

    def _run_iteration(self):
        LOG.debug("Workspace integration bridge worker iteration")
        self._sync_iam_users()
        self._sync_zulip_users()
        self._start_bridges()
        self._process_sync_queue()
