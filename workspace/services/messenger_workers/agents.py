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
import importlib
import logging
import time
import typing
import uuid as sys_uuid

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from gcl_looper.services import basic

from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.api import sql_canonical_store
from workspace.common import messenger_storage_opts
from workspace.external_bridge_control import sql_state
from workspace.messenger_migration import writer_gate

LOG = logging.getLogger(__name__)
EVENT_PRUNE_INTERVAL_SECONDS = 3600
PROJECTION_REPAIR_LIMIT = 5


class _LazyModule:
    """Delay transitional Maildir imports until mail mode is actually used."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name

    def __getattr__(self, name: str) -> typing.Any:
        module = importlib.import_module(self._module_name)
        return getattr(module, name)


mail_repository = _LazyModule("workspace.messenger_mail.repository")
mail_runtime = _LazyModule("workspace.messenger_mail.runtime")
external_bridge_data_plane = _LazyModule(
    "workspace.messenger_mail.external_bridge_data_plane"
)


def database_session_context() -> typing.ContextManager[typing.Any]:
    """Own one transaction at a worker or operator-command boundary."""
    ctx = contexts.Context()
    return ctx.session_manager()


class MessengerWorkerAgent(basic.BasicService):
    def __init__(
        self,
        runtime_factory: typing.Any = None,
        bridge_config: typing.Any = None,
        storage_mode: str = messenger_storage_opts.MAIL_PROJECTION,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(**kwargs)
        self._runtime_factory = runtime_factory
        if (
            self._runtime_factory is None
            and storage_mode == messenger_storage_opts.MAIL_PROJECTION
        ):
            self._runtime_factory = mail_runtime.RuntimeFactory()
        self._bridge_config = bridge_config
        self._storage_mode = storage_mode
        self._last_event_prune: float | None = None

    def _prune_expired_events(
        self,
        session: typing.Any,
        now: datetime.datetime,
    ) -> int:
        if self._storage_mode == messenger_storage_opts.POSTGRESQL_CANONICAL:
            return sql_canonical_store.prune_expired_events(session, now)
        cutoff = now - mail_repository.EVENT_RETENTION
        expired_events = messenger_models.WorkspaceEvent.objects.get_all(
            filters={"created_at": dm_filters.LT(cutoff)},
            session=session,
        )
        targets = {(event.project_id, event.user_uuid) for event in expired_events}
        for project_id, user_uuid in targets:
            with self._runtime_factory.project_repository(project_id) as repository:
                repository.prune_events(user_uuid, now=now)
        for event in expired_events:
            event.delete(session=session)
        return len(expired_events)

    def _iteration(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        with database_session_context() as session:
            closed_projects = writer_gate.heartbeat_and_acknowledge(
                session,
                "worker",
                now=now,
            )
            if closed_projects:
                return
            for project_id in self._canonical_mutation_project_ids(session):
                # Held for the entire worker transaction. A concurrent close
                # either waits for this mutation boundary or wins first and
                # makes the worker fail before any background write.
                writer_gate.assert_writable(
                    session,
                    project_id,
                    "worker",
                    now=now,
                )
            messenger_dm_helpers.mark_stale_workspace_users_offline(
                session=session,
            )
            sql_state.refresh_effective_capabilities(session, now=now)
            if self._storage_mode == messenger_storage_opts.MAIL_PROJECTION:
                external_bridge_data_plane.flush_outbox(
                    session,
                    self._runtime_factory,
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
            if (
                self._storage_mode == messenger_storage_opts.MAIL_PROJECTION
                and self._bridge_config is not None
            ):
                external_bridge_data_plane.consume_ingress(
                    session,
                    self._runtime_factory,
                    realm_uuid=self._bridge_config.realm_uuid,
                    bridge_instance_uuid=self._bridge_config.bridge_instance_uuid,
                    identity_generation=self._bridge_config.identity_generation,
                    enrollment_secret=self._bridge_config.enrollment_secret,
                )
            self._repair_external_projection_transitions(session)

    @staticmethod
    def _canonical_mutation_project_ids(
        session: typing.Any,
    ) -> tuple[sys_uuid.UUID, ...]:
        """Lock every project whose canonical state this cycle may mutate."""
        rows = session.execute(
            """
            SELECT DISTINCT projects.project_id
            FROM (
                SELECT project_id FROM m_workspace_streams
                UNION ALL
                SELECT project_id FROM m_workspace_events
                UNION ALL
                SELECT project_id FROM m_workspace_broadcast_message_events_v1
                UNION ALL
                SELECT project_id FROM m_external_chats_v2
                WHERE project_id IS NOT NULL
                UNION ALL
                SELECT (settings ->> 'default_project_id')::uuid AS project_id
                FROM m_external_accounts_v2
                WHERE settings ? 'default_project_id'
                UNION ALL
                SELECT project_id FROM m_messenger_import_runs_v1
                WHERE phase IN (
                    'inventory', 'staged', 'applying', 'frozen', 'final_delta'
                )
                UNION ALL
                SELECT project_id FROM m_messenger_writer_gates_v1
            ) AS projects
            WHERE projects.project_id IS NOT NULL
            ORDER BY projects.project_id
            """,
            (),
        ).fetchall()
        return tuple(row["project_id"] for row in rows)

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
