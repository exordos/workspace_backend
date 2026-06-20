#    Copyright 2025 Genesis Corporation.
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
from os import name

from restalchemy.common import contexts
from restalchemy.dm import filters as ra_filters
from gcl_looper.services import basic

from workspace.messanger_api.dm import models


LOG = logging.getLogger(__name__)


class MessangerWorkerAgent(basic.BasicService):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _process_new_binding(self, binding_to_sync):
        stream = binding_to_sync.stream
        binding = binding_to_sync.binding
        user_stream = models.WorkspaceUserStream(
            name=stream.name,
            description=stream.description,
            project_id=stream.project_id,
            user_uuid=binding.user_uuid,
            stream_uuid=stream.uuid,
            last_synced_at=stream.updated_at,
            source_name=stream.source_name,
            source=stream.source,
            invite_only=stream.invite_only,
            announce=stream.announce,
            private=stream.private,
        )
        user_stream.insert()
    
    def _process_stream_bindings(self):
        for binding_to_sync in models.StreamBindingToSync.objects.get_all(limit=100):
            LOG.info("Processing stream binding to sync: %s", binding_to_sync.uuid)
            if binding_to_sync.user_stream is None:
                self._process_new_binding(binding_to_sync)
            else:
                binding_to_sync.user_stream.sync()
                

    def _iteration(self):
        ctx = contexts.Context()
        with ctx.session_manager():
            self._process_stream_bindings()
