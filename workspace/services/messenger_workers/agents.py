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

import datetime
import logging
import time
import typing

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from gcl_looper.services import basic

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.api import sql_canonical_store
from workspace.external_bridge_control import sql_state

LOG = logging.getLogger(__name__)
EVENT_PRUNE_INTERVAL_SECONDS = 3600
PROJECTION_REPAIR_LIMIT = 5


def database_session_context() -> typing.ContextManager[typing.Any]:
    """Own one transaction at a worker or operator-command boundary."""
    ctx = contexts.Context()
    return ctx.session_manager()


class MessengerWorkerAgent(basic.BasicService):
    def __init__(
        self,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._last_event_prune: float | None = None

    def _prune_expired_events(
        self,
        session: typing.Any,
        now: datetime.datetime,
    ) -> int:
        return sql_canonical_store.prune_expired_events(session, now)

    def _iteration(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        with database_session_context() as session:
            messenger_dm_helpers.mark_stale_workspace_users_offline(
                session=session,
            )
            sql_state.refresh_effective_capabilities(session, now=now)
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
            self._repair_external_projection_transitions(session)

    def _repair_external_projection_transitions(
        self,
        session: typing.Any,
    ) -> None:
        """Repair a bounded batch in the worker-owned transaction."""
        rows = session.execute(
            """
            SELECT uuid, external_chat_uuid
            FROM m_external_projection_transitions_v1
            WHERE phase NOT IN ('completed', 'failed')
              AND next_repair_at <= NOW()
            ORDER BY next_repair_at, created_at
            LIMIT %s
            """,
            (PROJECTION_REPAIR_LIMIT,),
        ).fetchall()
        for row in rows:
            chat = external_models.ExternalChat.objects.get_one(
                filters={"uuid": dm_filters.EQ(row["external_chat_uuid"])},
                session=session,
            )
            messenger_controllers.ExternalChatController._resume_transition(
                row["uuid"], chat, session
            )
