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
import datetime
import time

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from gcl_looper.services import basic

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_mail import repository as mail_repository
from workspace.messenger_mail import runtime as mail_runtime

LOG = logging.getLogger(__name__)
EVENT_PRUNE_INTERVAL_SECONDS = 3600


class MessengerWorkerAgent(basic.BasicService):
    def __init__(self, runtime_factory=None, **kwargs):
        super().__init__(**kwargs)
        self._runtime_factory = runtime_factory or mail_runtime.RuntimeFactory()
        self._last_event_prune = None

    def _prune_expired_events(self, session, now):
        cutoff = now - mail_repository.EVENT_RETENTION
        expired_events = messenger_models.WorkspaceEvent.objects.get_all(
            filters={"created_at": dm_filters.LT(cutoff)},
            session=session,
        )
        targets = {
            (event.project_id, event.user_uuid) for event in expired_events
        }
        for project_id, user_uuid in targets:
            with self._runtime_factory.project_repository(project_id) as repository:
                repository.prune_events(user_uuid, now=now)
        for event in expired_events:
            event.delete(session=session)
        return len(expired_events)

    def _iteration(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ctx = contexts.Context()
        with ctx.session_manager() as session:
            messenger_dm_helpers.mark_stale_workspace_users_offline(
                session=session,
            )
            monotonic_now = time.monotonic()
            if (
                self._last_event_prune is None
                or monotonic_now - self._last_event_prune
                >= EVENT_PRUNE_INTERVAL_SECONDS
            ):
                pruned = self._prune_expired_events(session, now)
                self._last_event_prune = monotonic_now
                if pruned:
                    LOG.info("Pruned %d expired Workspace event rows", pruned)
