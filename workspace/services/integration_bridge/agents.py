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

import logging

from gcl_looper.services import basic
from restalchemy.common import contexts

LOG = logging.getLogger(__name__)


class WorkspaceIntegrationBridgeWorker(basic.BasicService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _iteration(self):
        ctx = contexts.Context()
        with ctx.session_manager() as session:
            self._run_iteration(session=session)

    def _run_iteration(self, session):
        LOG.debug("Workspace integration bridge worker iteration")
