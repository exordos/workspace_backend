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
        agents.WorkspaceIntegrationBridgeWorker()._sync_users()

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


def test_bridge_worker_iteration_syncs_users():
    worker = agents.WorkspaceIntegrationBridgeWorker()

    with (
        mock.patch.object(worker, "_sync_iam_users") as sync_iam_users,
        mock.patch.object(worker, "_sync_users") as sync_users,
    ):
        worker._run_iteration()

    sync_iam_users.assert_called_once_with()
    sync_users.assert_called_once_with()
