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

from types import SimpleNamespace
from unittest import mock

from workspace.services.integration_bridge import agents


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
        user_uuid="first-user",
        account_settings=SimpleNamespace(credentials=credentials),
    )
    second_user = SimpleNamespace(
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

        def __init__(self, external_account, sync_queue):
            self.external_account = external_account
            self.sync_queue = sync_queue
            self.start = mock.Mock()
            type(self).instances.append(self)

    worker = agents.WorkspaceIntegrationBridgeWorker()
    existing_worker = object()
    worker._workers = {"first-user": existing_worker}

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
            worker._start_bridges()

    FakeExternalAccount.objects.get_all.assert_called_once()
    filters = FakeExternalAccount.objects.get_all.call_args.kwargs["filters"]
    assert filters["account_type"].value == "zulip"
    assert worker._workers["first-user"] is existing_worker
    assert worker._workers["second-user"] is FakeZulipBridgeWorker.instances[0]
    assert "skipped-user" not in worker._workers
    assert FakeZulipBridgeWorker.instances[0].external_account is second_user
    assert FakeZulipBridgeWorker.instances[0].sync_queue is worker._sync_queue
    FakeZulipBridgeWorker.instances[0].start.assert_called_once_with()


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


def test_bridge_worker_requeues_retryable_commands():
    command = SimpleNamespace(
        execute=mock.Mock(side_effect=agents.RetryCommandLater()),
    )
    worker = agents.WorkspaceIntegrationBridgeWorker()
    worker._sync_queue.put(command)

    with mock.patch.object(agents, "SYNC_QUEUE_TIMEOUT", 0):
        worker._process_sync_queue()

    command.execute.assert_called_once_with(cache=worker._cache)
    assert worker._sync_queue.get_nowait() is command


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
