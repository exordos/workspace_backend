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

from types import SimpleNamespace
from unittest import mock

from workspace.services.integration_bridge import agents


class FakeZulipProcessedEntity:
    inserted = []
    objects = SimpleNamespace(get_one_or_none=mock.Mock(return_value=None))

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def insert(self):
        type(self).inserted.append(self)


class FakeZulipQueueState:
    def __init__(
        self,
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
    ):
        self.queue_id = queue_id
        self.last_event_id = last_event_id
        self.last_message_id = last_message_id
        self.is_synced = is_synced
        self.update_dm = mock.Mock(side_effect=self._update_dm)
        self.update = mock.Mock()

    def _update_dm(self, values):
        self.__dict__.update(values)


def _patch_zulip_processed_entities(return_value=None):
    FakeZulipProcessedEntity.inserted = []
    FakeZulipProcessedEntity.objects = SimpleNamespace(
        get_one_or_none=mock.Mock(return_value=return_value),
    )
    return mock.patch.object(
        agents.models,
        "ZulipProcessedEntity",
        FakeZulipProcessedEntity,
    )


def test_bridge_worker_syncs_due_user_providers():
    provider = SimpleNamespace(sync=mock.Mock())

    class FakeExternalAccountUserSync:
        objects = SimpleNamespace(get_all=mock.Mock(return_value=[provider]))

    with mock.patch.object(
        agents.models,
        "ExternalAccountUserSync",
        FakeExternalAccountUserSync,
    ):
        agents.WorkspaceIntegrationBridgeWorker()._sync_zulip_users()

    FakeExternalAccountUserSync.objects.get_all.assert_called_once()
    filters = FakeExternalAccountUserSync.objects.get_all.call_args.kwargs[
        "filters"
    ]
    assert filters["account_type"].value == "zulip"
    provider.sync.assert_called_once_with()


def test_bridge_worker_syncs_due_iam_user_providers():
    provider = SimpleNamespace(sync=mock.Mock())

    class FakeExternalAccountUserSync:
        objects = SimpleNamespace(get_all=mock.Mock(return_value=[provider]))

    with mock.patch.object(
        agents.models,
        "ExternalAccountUserSync",
        FakeExternalAccountUserSync,
    ):
        agents.WorkspaceIntegrationBridgeWorker()._sync_iam_users()

    FakeExternalAccountUserSync.objects.get_all.assert_called_once()
    filters = FakeExternalAccountUserSync.objects.get_all.call_args.kwargs[
        "filters"
    ]
    assert filters["account_type"].value == "iam"
    provider.sync.assert_called_once_with()


def test_bridge_worker_starts_zulip_bridge_workers_by_user_uuid():
    credentials = object()
    first_user = SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="first-user",
        account_settings=SimpleNamespace(credentials=credentials),
    )
    second_user = SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="second-user",
        account_settings=SimpleNamespace(credentials=credentials),
    )
    skipped_user = SimpleNamespace(
        user_uuid="skipped-user",
        account_settings=SimpleNamespace(credentials=None),
    )

    class FakeExternalAccount:
        objects = SimpleNamespace(
            get_all=mock.Mock(
                return_value=[first_user, second_user, skipped_user],
            ),
        )

    class FakeZulipBridgeWorker:
        instances = []

        def __init__(
            self,
            external_account,
            input_queue,
            output_queue,
        ):
            self.external_account = external_account
            self.input_queue = input_queue
            self.output_queue = output_queue
            self.start = mock.Mock()
            type(self).instances.append(self)

    worker = agents.WorkspaceIntegrationBridgeWorker()
    existing_worker = object()
    existing_input_queue = queue.Queue()
    first_worker_key = (
        "project",
        "https://zulip.example.com",
        "first-user",
    )
    second_worker_key = (
        "project",
        "https://zulip.example.com",
        "second-user",
    )
    worker._workers = {first_worker_key: existing_worker}
    worker._worker_input_queues = {first_worker_key: existing_input_queue}
    worker._worker_accounts = {first_worker_key: first_user}
    queue_state = SimpleNamespace(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
    )

    with mock.patch.object(
        agents.models,
        "ExternalAccount",
        FakeExternalAccount,
    ):
        with mock.patch.object(
            agents.workers,
            "ZulipBridgeWorker",
            FakeZulipBridgeWorker,
        ):
            with mock.patch.object(
                worker,
                "_get_or_create_zulip_queue_state",
                mock.Mock(return_value=queue_state),
            ):
                worker._start_bridges()

    FakeExternalAccount.objects.get_all.assert_called_once()
    filters = FakeExternalAccount.objects.get_all.call_args.kwargs["filters"]
    assert filters["account_type"].value == "zulip"
    assert worker._workers[first_worker_key] is existing_worker
    assert worker._workers[second_worker_key] is (
        FakeZulipBridgeWorker.instances[0]
    )
    assert (
        "project",
        "https://zulip.example.com",
        "skipped-user",
    ) not in worker._workers
    assert FakeZulipBridgeWorker.instances[0].external_account is second_user
    assert FakeZulipBridgeWorker.instances[0].output_queue is (
        worker._sync_queue
    )
    assert worker._message_sync_worker_key == first_worker_key
    sync_message = existing_input_queue.get_nowait()
    assert isinstance(sync_message, agents.workers.SyncMessages)
    assert sync_message.queue_id == "queue-1"
    assert sync_message.last_event_id == 42
    assert sync_message.last_message_id == 103
    assert sync_message.is_synced is True
    sync_message.on_finished()
    assert worker._message_sync_worker_key is None
    FakeZulipBridgeWorker.instances[0].start.assert_called_once_with()

    worker._workers = {first_worker_key: existing_worker}
    worker._worker_input_queues = {first_worker_key: existing_input_queue}
    worker._worker_accounts = {first_worker_key: first_user}
    with mock.patch.object(
        agents.models,
        "ExternalAccount",
        FakeExternalAccount,
    ):
        with mock.patch.object(
            agents.workers,
            "ZulipBridgeWorker",
            FakeZulipBridgeWorker,
        ):
            with mock.patch.object(
                worker,
                "_get_or_create_zulip_queue_state",
                mock.Mock(return_value=queue_state),
            ):
                worker._start_bridges()

    sync_message = existing_input_queue.get_nowait()
    assert sync_message.queue_id == "queue-1"
    assert sync_message.last_event_id == 42
    assert sync_message.last_message_id == 103
    assert sync_message.is_synced is True


def test_bridge_cache_gets_existing_stream_once():
    existing_stream = SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
        source=SimpleNamespace(stream_id=3),
    )
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [],
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[existing_stream]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            stream = cache.get_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )
            cached_stream = cache.get_or_create_stream(
                external_account=external_account,
                stream_info=stream_info,
            )

    assert stream is existing_stream
    assert cached_stream is existing_stream
    FakeWorkspaceStream.objects.get_all.assert_called_once()
    filters = FakeWorkspaceStream.objects.get_all.call_args.kwargs["filters"]
    assert filters["project_id"].value == "project"
    assert filters["source_name"].value == "zulip"


def test_bridge_cache_binds_existing_stream_users_once():
    existing_stream = SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
        source=SimpleNamespace(stream_id=3),
    )
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [24, 25],
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[existing_stream]),
        )

    creator_account = SimpleNamespace(
        project_id="project",
        user_uuid="creator-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=24),
        ),
    )
    member_account = SimpleNamespace(
        project_id="project",
        user_uuid="member-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=25),
        ),
    )

    class FakeExternalAccount:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[creator_account, member_account]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_stream_bindings",
                    mock.Mock(),
                ) as get_or_create_bindings:
                    stream = cache.get_or_create_stream(
                        external_account=external_account,
                        stream_info=stream_info,
                    )
                    cached_stream = cache.get_or_create_stream(
                        external_account=external_account,
                        stream_info=stream_info,
                    )

    assert stream is existing_stream
    assert cached_stream is existing_stream
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="stream-uuid",
        who_uuid="creator-user",
        role_user_uuids={
            "member": [
                "member-user",
            ],
        },
    )


def test_bridge_cache_creates_missing_stream_once():
    created_stream = SimpleNamespace(
        uuid="stream-uuid",
        user_uuid="creator-user",
    )
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "General stream",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": True,
        "announce": True,
        "is_archived": True,
        "subscriber_ids": [10, 24, 25],
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[]),
        )

    creator_account = SimpleNamespace(
        project_id="project",
        user_uuid="creator-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=24),
        ),
    )
    bridge_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=10),
        ),
    )
    member_account = SimpleNamespace(
        project_id="project",
        user_uuid="member-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=25),
        ),
    )

    class FakeExternalAccount:
        objects = SimpleNamespace(
            get_all=mock.Mock(
                return_value=[
                    bridge_account,
                    creator_account,
                    member_account,
                ],
            ),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(return_value=created_stream),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )
                        cached_stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )

    assert stream is created_stream
    assert cached_stream is created_stream
    FakeWorkspaceStream.objects.get_all.assert_called_once()
    FakeExternalAccount.objects.get_all.assert_called_once()
    filters = FakeExternalAccount.objects.get_all.call_args.kwargs["filters"]
    assert filters["project_id"].value == "project"
    assert filters["account_type"].value == "zulip"
    assert filters["server_url"].value == "https://zulip.example.com"
    get_or_create_stream.assert_called_once()
    call_kwargs = get_or_create_stream.call_args.kwargs
    assert call_kwargs["project_id"] == "project"
    assert call_kwargs["user_uuid"] == "creator-user"
    assert call_kwargs["name"] == "general"
    assert call_kwargs["description"] == "General stream"
    assert call_kwargs["source_name"] == "zulip"
    assert call_kwargs["source"].kind == "zulip"
    assert call_kwargs["source"].stream_id == 3
    assert call_kwargs["invite_only"] is True
    assert call_kwargs["announce"] is True
    assert call_kwargs["is_archived"] is True
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="stream-uuid",
        who_uuid="creator-user",
        role_user_uuids={
            "member": [
                "bridge-user",
                "member-user",
            ],
        },
    )


def test_bridge_cache_creates_missing_private_stream_with_zulip_topic():
    public_stream_with_same_id = SimpleNamespace(
        uuid="public-stream-uuid",
        user_uuid="public-owner",
        private=False,
        source=SimpleNamespace(stream_id=79),
    )
    created_stream = SimpleNamespace(
        uuid="private-stream-uuid",
        user_uuid="gmelikov-user",
    )
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "private",
        "stream_id": 79,
        "display_recipient": "admin, gmelikov",
        "description": "",
        "creator_id": 10,
        "timestamp": 1772202531,
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [8, 10],
        "default_topic_name": "zulip",
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[public_stream_with_same_id]),
        )

    admin_account = SimpleNamespace(
        project_id="project",
        user_uuid="admin-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=8),
        ),
    )
    gmelikov_account = SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=10),
        ),
    )

    class FakeExternalAccount:
        objects = SimpleNamespace(
            get_all=mock.Mock(
                return_value=[
                    admin_account,
                    gmelikov_account,
                ],
            ),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(return_value=created_stream),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        stream = cache.get_or_create_stream(
                            external_account=external_account,
                            stream_info=stream_info,
                        )

    assert stream is created_stream
    get_or_create_stream.assert_called_once()
    call_kwargs = get_or_create_stream.call_args.kwargs
    assert call_kwargs["project_id"] == "project"
    assert call_kwargs["user_uuid"] == "gmelikov-user"
    assert call_kwargs["name"] == "admin, gmelikov"
    assert call_kwargs["description"] == ""
    assert call_kwargs["source_name"] == "zulip"
    assert call_kwargs["source"].kind == "zulip"
    assert call_kwargs["source"].stream_id == 79
    assert call_kwargs["invite_only"] is True
    assert call_kwargs["announce"] is False
    assert call_kwargs["direct_user_uuid"] == "admin-user"
    assert call_kwargs["is_archived"] is False
    assert call_kwargs["default_topic_name"] == "zulip"
    get_or_create_bindings.assert_called_once_with(
        project_id="project",
        stream_uuid="private-stream-uuid",
        who_uuid="gmelikov-user",
        role_user_uuids={
            "member": [
                "admin-user",
            ],
        },
    )


def test_bridge_cache_retries_private_stream_without_direct_user():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "private",
        "stream_id": 79,
        "display_recipient": "gmelikov",
        "description": "",
        "creator_id": 10,
        "timestamp": 1772202531,
        "invite_only": True,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [10],
        "default_topic_name": "zulip",
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(get_all=mock.Mock(return_value=[]))

    gmelikov_account = SimpleNamespace(
        project_id="project",
        user_uuid="gmelikov-user",
        server_url="https://zulip.example.com",
        account_settings=SimpleNamespace(
            user_info=SimpleNamespace(user_id=10),
        ),
    )

    class FakeExternalAccount:
        objects = SimpleNamespace(
            get_all=mock.Mock(return_value=[gmelikov_account]),
        )

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            with mock.patch.object(
                agents.models,
                "ExternalAccount",
                FakeExternalAccount,
            ):
                with mock.patch.object(
                    agents.messenger_dm_helpers,
                    "get_or_create_workspace_user_stream",
                    mock.Mock(),
                ) as get_or_create_stream:
                    with mock.patch.object(
                        agents.messenger_dm_helpers,
                        "get_or_create_workspace_stream_bindings",
                        mock.Mock(),
                    ) as get_or_create_bindings:
                        try:
                            cache.get_or_create_stream(
                                external_account=external_account,
                                stream_info=stream_info,
                            )
                        except agents.RetryCommandLater:
                            pass
                        else:
                            assert False

    get_or_create_stream.assert_not_called()
    get_or_create_bindings.assert_not_called()


def test_bridge_cache_requests_stream_sync_for_missing_message_stream():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    stream_info = {
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "general",
        "description": "",
        "creator_id": 24,
        "timestamp": 1770998098,
        "invite_only": False,
        "announce": False,
        "is_archived": False,
        "subscriber_ids": [24],
        "event_type": "message",
    }

    class FakeWorkspaceStream:
        objects = SimpleNamespace(get_all=mock.Mock(return_value=[]))

    cache = agents.WorkspaceIntegrationBridgeCache()
    with _patch_zulip_processed_entities():
        with mock.patch.object(
            agents.models,
            "WorkspaceStream",
            FakeWorkspaceStream,
        ):
            try:
                cache.get_or_create_stream(
                    external_account=external_account,
                    stream_info=stream_info,
                )
            except agents.SyncStreamsNeeded as exc:
                assert exc.external_account is external_account
                assert exc.stream_id == 3
            else:
                assert False


def test_bridge_cache_syncs_zulip_read_flag_for_each_external_account():
    first_account = SimpleNamespace(
        project_id="project",
        user_uuid="first-user",
        server_url="https://zulip.example.com",
    )
    second_account = SimpleNamespace(
        project_id="project",
        user_uuid="second-user",
        server_url="https://zulip.example.com",
    )
    stream = SimpleNamespace(uuid="stream-uuid")
    topic = SimpleNamespace(uuid="topic-uuid")
    message = SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
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
    }

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_save_processed_entity",
        mock.Mock(),
    ) as save_processed, mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ) as get_or_create_message, mock.patch.object(
        agents.messenger_dm_helpers,
        "read_workspace_user_message",
        mock.Mock(),
    ) as read_message:
        result = cache.get_or_create_message(
            external_account=first_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )
        cached_result = cache.get_or_create_message(
            external_account=second_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )

    assert result is message
    assert cached_result is message
    get_or_create_message.assert_called_once()
    save_processed.assert_called_once()
    assert read_message.call_args_list == [
        mock.call(
            project_id="project",
            user_uuid="first-user",
            message_uuid="message-uuid",
        ),
        mock.call(
            project_id="project",
            user_uuid="second-user",
            message_uuid="message-uuid",
        ),
    ]


def test_bridge_cache_does_not_sync_unread_zulip_message_as_unread():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="reader-user",
        server_url="https://zulip.example.com",
    )
    stream = SimpleNamespace(uuid="stream-uuid")
    topic = SimpleNamespace(uuid="topic-uuid")
    message = SimpleNamespace(uuid="message-uuid")
    stream_info = {
        "stream_id": 3,
    }
    message_info = {
        "message_id": 100,
        "sender_id": 24,
        "content": "hello",
        "read": False,
        "created_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
        "updated_at": datetime.datetime.fromtimestamp(
            1770998098,
            tz=datetime.timezone.utc,
        ),
    }

    cache = agents.WorkspaceIntegrationBridgeCache()
    with mock.patch.object(
        cache,
        "_get_required_zulip_user_uuid",
        mock.Mock(return_value="author-user"),
    ), mock.patch.object(
        cache,
        "_get_processed_workspace_uuid",
        mock.Mock(return_value=None),
    ), mock.patch.object(
        cache,
        "_save_processed_entity",
        mock.Mock(),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "get_or_create_workspace_user_message",
        mock.Mock(return_value=message),
    ), mock.patch.object(
        agents.messenger_dm_helpers,
        "read_workspace_user_message",
        mock.Mock(),
    ) as read_message:
        cache.get_or_create_message(
            external_account=external_account,
            stream=stream,
            topic=topic,
            stream_info=stream_info,
            topic_name="deploys",
            message_info=message_info,
        )

    read_message.assert_not_called()


def test_bridge_worker_requeues_retryable_commands():
    command = SimpleNamespace(
        execute=mock.Mock(side_effect=agents.RetryCommandLater()),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.empty()
    assert len(worker._postponed_commands) == 1
    assert worker._postponed_commands[0][1] is command

    worker._postponed_commands[0] = (
        datetime.datetime.now(datetime.timezone.utc) -
        datetime.timedelta(seconds=1),
        command,
    )
    worker._release_postponed_commands()

    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is command


def test_bridge_worker_requests_queue_recreate_without_queue_id():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    input_queue = queue.Queue()
    queue_state = FakeZulipQueueState(
        queue_id=None,
        last_event_id=-1,
        last_message_id=103,
        is_synced=False,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._request_zulip_message_sync(event_owner)

    command = input_queue.get_nowait()
    assert isinstance(
        command,
        agents.workers.CreateZulipQueueAndFetchMessages,
    )
    assert command.last_message_id == 103
    assert worker._message_sync_worker_key is None
    assert worker._queue_recreate_worker_keys == {event_owner}


def test_bridge_worker_recreates_queue_after_failed_queue_event():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    input_queue = queue.Queue()
    queue_state = FakeZulipQueueState(
        queue_id="dead-queue",
        last_event_id=42,
        last_message_id=103,
        is_synced=True,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account
    worker._message_sync_worker_key = event_owner
    worker._put_sync_command(
        agents.workers.ZulipQueueFailed(
            external_account=external_account,
        ),
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
            worker._process_sync_queue()

    command = input_queue.get_nowait()
    assert isinstance(
        command,
        agents.workers.CreateZulipQueueAndFetchMessages,
    )
    assert command.last_message_id == 103
    assert worker._message_sync_worker_key is None
    assert worker._queue_recreate_worker_keys == {event_owner}
    assert queue_state.queue_id is None
    assert queue_state.is_synced is False
    queue_state.update.assert_called_once_with()


def test_bridge_worker_marks_queue_synced_on_catch_up_barrier():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    queue_state = FakeZulipQueueState(is_synced=False)
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = agents.workers.FinishZulipMessageCatchUp(
        external_account=external_account,
        last_message_id=104,
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._execute_sync_command(command)

    assert queue_state.is_synced is True
    assert queue_state.last_message_id == 104
    queue_state.update.assert_called_once_with()


def test_bridge_worker_updates_message_anchor_without_clearing_queue_id():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    queue_state = FakeZulipQueueState(
        queue_id="queue-1",
        last_event_id=42,
        last_message_id=103,
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    command = agents.workers.UpdateZulipQueueState(
        external_account=external_account,
        last_message_id=104,
    )

    with mock.patch.object(
        worker,
        "_get_or_create_zulip_queue_state",
        mock.Mock(return_value=queue_state),
    ):
        worker._execute_sync_command(command)

    assert queue_state.queue_id == "queue-1"
    assert queue_state.last_message_id == 104
    queue_state.update.assert_called_once_with()


def test_bridge_worker_requests_stream_sync_and_retries_command():
    external_account = SimpleNamespace(
        project_id="project",
        user_uuid="bridge-user",
        server_url="https://zulip.example.com",
    )
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = SimpleNamespace(
        external_account=external_account,
        event_owner=event_owner,
        execute=mock.Mock(
            side_effect=agents.SyncStreamsNeeded(
                external_account=external_account,
                stream_id=3,
            ),
        ),
    )
    input_queue = queue.Queue()
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._workers[event_owner] = object()
    worker._worker_input_queues[event_owner] = input_queue
    worker._worker_accounts[event_owner] = external_account
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.empty()
    assert len(worker._postponed_commands) == 1
    assert worker._postponed_commands[0][1] is command
    sync_streams = input_queue.get_nowait()
    assert isinstance(sync_streams, agents.workers.SyncStreams)
    assert sync_streams.event_owner == event_owner
    assert worker._stream_sync_event_owners == {event_owner}


def test_bridge_worker_defers_non_stream_commands_during_stream_sync():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = SimpleNamespace(execute=mock.Mock(return_value="processed"))
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_not_called()
    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is command


def test_bridge_worker_processes_stream_commands_during_stream_sync():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    external_account = SimpleNamespace(
        project_id="project",
        server_url="https://zulip.example.com",
        user_uuid="bridge-user",
    )
    stream_command = agents.workers.AddStream(
        external_account=external_account,
        stream={
            "stream_id": 3,
            "name": "general",
            "description": "General stream",
            "creator_id": 24,
            "date_created": 1776940760,
            "invite_only": False,
            "is_archived": False,
            "is_announcement_only": False,
        },
    )
    stream_command.execute = mock.Mock(return_value="stream")
    message_command = SimpleNamespace(
        execute=mock.Mock(return_value="message"),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(message_command)
    worker._put_sync_command(stream_command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    stream_command.execute.assert_called_once_with(cache=worker._cache)
    message_command.execute.assert_not_called()
    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is (
        message_command
    )


def test_bridge_worker_releases_stream_sync_barrier_on_finished_response():
    event_owner = (
        "project",
        "https://zulip.example.com",
        "bridge-user",
    )
    command = SimpleNamespace(execute=mock.Mock(return_value="processed"))
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._stream_sync_event_owners.add(event_owner)
    worker._put_sync_command(command)
    worker._put_sync_command(
        agents.workers.SyncStreamsFinished(event_owner=event_owner),
    )

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    assert worker._stream_sync_event_owners == set()
    command.execute.assert_called_once_with(cache=worker._cache)


def test_bridge_worker_processes_sync_queue_in_batches():
    commands = [
        SimpleNamespace(execute=mock.Mock(return_value="processed"))
        for _ in range(3)
    ]
    worker = agents.WorkspaceIntegrationBridgeWorker(
        sync_queue_batch_limit=2,
    )
    for command in commands:
        worker._put_sync_command(command)

    worker._process_sync_queue()

    commands[0].execute.assert_called_once_with(cache=worker._cache)
    commands[1].execute.assert_called_once_with(cache=worker._cache)
    commands[2].execute.assert_not_called()
    response = worker._sync_queue.get_nowait()
    assert agents.workers.get_sync_response_command(response) is commands[2]


def test_bridge_worker_iteration_syncs_users():
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with mock.patch.object(worker, "_sync_iam_users") as sync_iam_users:
        with mock.patch.object(worker, "_sync_zulip_users") as sync_zulip_users:
            with mock.patch.object(worker, "_start_bridges") as start_bridges:
                with mock.patch.object(
                    worker,
                    "_process_sync_queue",
                ) as process_sync_queue:
                    worker._run_iteration()

    sync_iam_users.assert_called_once_with()
    sync_zulip_users.assert_called_once_with()
    start_bridges.assert_called_once_with()
    process_sync_queue.assert_called_once_with()
