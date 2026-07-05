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

from gcl_looper.services import basic
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import models

LOG = logging.getLogger(__name__)


class WorkspaceIntegrationBridgeWorker(basic.BasicService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _get_unsynced_user_providers(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        return models.ExternalAccountUserSync.objects.get_all(
            filters=dm_filters.AND(
                {"next_sync_at": dm_filters.IsNot(None)},
                {"next_sync_at": dm_filters.LE(now)},
            ),
            order_by={"next_sync_at": "asc", "uuid": "asc"},
        )

    def _sync_users(self):
        user_providers = self._get_unsynced_user_providers()
        for provider in user_providers:
            provider.sync()

    def _iteration(self):
        ctx = contexts.Context()
        with ctx.session_manager():
            self._run_iteration()

    def _run_iteration(self):
        LOG.debug("Workspace integration bridge worker iteration")
        self._sync_iam_users()
        self._sync_users()
