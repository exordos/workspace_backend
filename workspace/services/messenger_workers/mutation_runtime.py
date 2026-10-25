# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Experimental sole-dispatcher runtime for the real Messenger schema.

The dispatcher reads durable input only. Executors commit application writes,
topic snapshots, event outbox and input acknowledgements together. Parent
invalidations cross the thread boundary only after that commit succeeds.
"""

import json
import fcntl
import logging
import os
import pathlib
import time
import typing
import uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import sessions

from workspace.messenger_api import events
from workspace.messenger_api import publication
from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import synchronization
from workspace.messenger_api.dm import v2_models
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import owned_projection
from workspace.services.messenger_workers import v2_projection


LOG = logging.getLogger(__name__)


class ScopeChanged(RuntimeError):
    def __init__(self, reservations: typing.Iterable[tuple]) -> None:
        self.reservations = reservations
        super().__init__("Placement scope changed before executor snapshot")


def _uuid(value: object) -> uuid.UUID:
    return uuid.UUID(str(value))


class MutationStore(v2_store.MessengerV2Store):
    def _enqueue(
        self,
        event_kind: str,
        scope_kind: str,
        scope_key: str,
        payload: dict[str, typing.Any],
    ) -> typing.Any:
        if event_kind != "content_mentions":
            return super()._enqueue(event_kind, scope_kind, scope_key, payload)
        session = contexts.Context().get_session()
        message = typing.cast(
            typing.Any,
            v2_projection._message_namespace(
                session, self.project_uuid, payload["placement_uuid"]
            ),
        )
        content = message_payloads.markdown_content(message.payload)
        # Only changed mention flags affect counters. Ordinary text edits do
        # not need to recount every member's entire history.
        changed = session.execute(
            """UPDATE messenger_user_message_states SET
                 mentioned=position(('](urn:user:'||lower(user_uuid::text)||')')
                     IN lower(%s))>0,updated_at=NOW()
               WHERE project_id=%s AND placement_uuid=%s
                 AND mentioned IS DISTINCT FROM
                     (position(('](urn:user:'||lower(user_uuid::text)||')') IN lower(%s))>0)
               RETURNING user_uuid""",
            (content, self.project_uuid, message.uuid, content),
        ).fetchall()
        if changed:
            typing.cast(owned_projection.Changes, owned_projection.CURRENT.get()).topic(
                self.project_uuid,
                message.stream_uuid,
                message.topic_uuid,
                [row["user_uuid"] for row in changed],
            )
        rows = v2_projection._v2_rows(
            v2_models.WorkspaceUserMessage,
            self.project_uuid,
            {"uuid": dm_filters.EQ(message.uuid)},
            order_by={"user_uuid": "asc"},
        )
        v2_projection._emit_message_updated_rows(session, self.project_uuid, rows)
        return None

    def delete_message(self, message_uuid: uuid.UUID) -> dict[str, typing.Any] | None:
        message = self._message(message_uuid)
        scopes = (
            contexts.Context()
            .get_session()
            .execute(
                """SELECT placement.stream_uuid,placement.topic_uuid,array_agg(state.user_uuid) AS users
               FROM messenger_message_placements AS placement
               JOIN messenger_user_message_states AS state
                 ON state.project_id=placement.project_id AND state.placement_uuid=placement.uuid
               WHERE placement.project_id=%s AND placement.message_uuid=%s
               GROUP BY placement.stream_uuid,placement.topic_uuid""",
                (self.project_uuid, message.canonical_message_uuid),
            )
            .fetchall()
        )
        result = super().delete_message(message_uuid)
        for scope in scopes:
            typing.cast(owned_projection.Changes, owned_projection.CURRENT.get()).topic(
                self.project_uuid,
                scope["stream_uuid"],
                scope["topic_uuid"],
                scope["users"],
            )
        return result

    def _enqueue_counter_projections(self, **payload: typing.Any) -> None:
        changes = typing.cast(owned_projection.Changes, owned_projection.CURRENT.get())
        changes.topic(
            self.project_uuid,
            payload["stream_uuid"],
            payload["topic_uuid"],
            [payload["user_uuid"]],
        )
        if payload.get("emit_message_read"):
            message = self._message(payload["placement_uuid"])
            if events.is_recent_message(message.created_at):
                emitted = events.create_message_read_event(message)
                v2_projection._guard_emitted_events(
                    contexts.Context().get_session(),
                    emitted,
                    project_id=self.project_uuid,
                    stream_uuid=message.stream_uuid,
                )


class MutationRuntime:
    def __init__(
        self,
        threads: int = 4,
        batch_size: int = 32,
        singleton_directory: str | pathlib.Path | None = None,
        receipt_retention_seconds: int = 86400,
    ) -> None:
        self._ownership: typing.TextIO | None = None
        if singleton_directory is not None:
            directory = pathlib.Path(singleton_directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                directory / "mutation-dispatcher.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            self._ownership = os.fdopen(descriptor, "a+")
            try:
                fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._ownership.close()
                raise RuntimeError(
                    "The Messenger mutation dispatcher is already running"
                ) from None
        self._dispatcher = keyed_dispatcher.KeyedDispatcher(self._execute, threads)
        self._batch_size = batch_size
        self._receipt_retention_seconds = receipt_retention_seconds
        self._started = False
        self._last_scan = 0.0
        self._recovery_due = False
        self._initial_topics: set[tuple] | None = None
        self._initializing = False

    def _topic_reservations(
        self,
        project_id: uuid.UUID,
        topics: typing.Iterable[typing.Mapping[str, typing.Any]],
    ) -> list[tuple]:
        return [
            ("topic", _uuid(project_id), _uuid(topic["topic_uuid"])) for topic in topics
        ]

    def _recover(self, session: sessions.PgSQLSession) -> None:
        initialized = session.execute(
            "SELECT initialized FROM messenger_mutation_projection_state WHERE singleton",
            (),
        ).fetchone()["initialized"]
        if not initialized:
            self._initial_topics = set()
            for row in session.execute(
                """SELECT topic.project_id, topic.uuid, topic.stream_uuid,
                          array_agg(binding.user_uuid) AS users
                   FROM messenger_topics AS topic
                   JOIN messenger_user_topic_bindings AS binding
                     ON binding.project_id=topic.project_id AND binding.topic_uuid=topic.uuid
                   GROUP BY topic.project_id, topic.uuid, topic.stream_uuid""",
                (),
            ).fetchall():
                self._initial_topics.add(("topic", row["project_id"], row["uuid"]))
                self._dispatcher.trigger(
                    ("topic", row["project_id"], row["uuid"]),
                    [(row["stream_uuid"], user) for user in row["users"]],
                )
        # Include empty streams and project owners, not just nonzero scopes.
        for row in session.execute(
            "SELECT project_id, stream_uuid, user_uuid FROM messenger_stream_bindings",
            (),
        ).fetchall():
            self._dispatcher.trigger(
                ("stream", row["project_id"], row["stream_uuid"]), [row["user_uuid"]]
            )
        for row in session.execute(
            "SELECT project_id, user_uuid, folder_uuid FROM messenger_user_folder_bindings",
            (),
        ).fetchall():
            self._dispatcher.trigger(
                ("folder", row["project_id"], row["user_uuid"], row["folder_uuid"])
            )

        for row in session.execute(
            "SELECT project_id,user_uuid FROM messenger_project_users", ()
        ).fetchall():
            self._dispatcher.trigger(("project", row["project_id"], row["user_uuid"]))

    def _scan(self, session: sessions.PgSQLSession) -> None:
        if (
            session.execute(
                """SELECT 1 FROM messenger_mutation_commands
               WHERE status IN ('applied','rejected')
                 AND finished_at < NOW() - %s * interval '1 second'
                 AND NOT EXISTS (
                   SELECT 1 FROM messenger_mutation_commands AS sibling
                   WHERE sibling.request_uuid=messenger_mutation_commands.request_uuid
                     AND (sibling.status='pending'
                       OR sibling.finished_at >= NOW() - %s * interval '1 second'))
               LIMIT 1""",
                (self._receipt_retention_seconds, self._receipt_retention_seconds),
            ).fetchone()
            is not None
        ):
            self._dispatcher.trigger(("receipts",))
        commands = session.execute(
            """WITH scopes AS (
                   SELECT scope_key, min(sequence) AS first_sequence
                   FROM messenger_mutation_commands WHERE status='pending'
                   GROUP BY scope_key ORDER BY first_sequence LIMIT 128
               )
               SELECT command.* FROM scopes CROSS JOIN LATERAL (
                   SELECT * FROM messenger_mutation_commands
                   WHERE status='pending' AND scope_key=scopes.scope_key
                   ORDER BY sequence LIMIT %s
               ) AS command ORDER BY command.sequence""",
            (self._batch_size,),
        ).fetchall()
        for row in commands:
            self._dispatcher.trigger(
                ("command", row["scope_key"]),
                [row["uuid"]],
                self._topic_reservations(row["project_id"], row["payload"]["topics"]),
            )
        tasks = session.execute(
            """SELECT * FROM messenger_projection_tasks
               WHERE status IN ('pending', 'failed', 'running')
                 AND (next_retry_at IS NULL OR next_retry_at<=NOW())
                 AND (lease_expires_at IS NULL OR lease_expires_at<=NOW())
               ORDER BY created_at, uuid LIMIT 128""",
            (),
        ).fetchall()
        for task in tasks:
            payload = task["payload"]
            topics = []
            if payload.get("topic_uuid"):
                topics = [{"topic_uuid": payload["topic_uuid"]}]
            placement = payload.get("placement_uuid") or payload.get(
                "placement", {}
            ).get("uuid")
            if placement:
                topics = session.execute(
                    """SELECT sibling.topic_uuid FROM messenger_message_placements AS placement
                       JOIN messenger_message_placements AS sibling
                         ON sibling.project_id=placement.project_id AND sibling.message_uuid=placement.message_uuid
                       WHERE placement.project_id=%s AND placement.uuid=%s""",
                    (task["project_id"], placement),
                ).fetchall()
            reservations = self._topic_reservations(task["project_id"], topics)
            if (
                task["task_kind"] == "read_counters"
                and task["scope_kind"] == "user-stream"
            ):
                reservations.append(
                    ("stream", task["project_id"], _uuid(payload["stream_uuid"]))
                )
            if task["task_kind"] == "folder_projection" and payload.get("user_uuid"):
                reservations.append(
                    (
                        "folder",
                        task["project_id"],
                        _uuid(payload["user_uuid"]),
                        _uuid(payload["folder_uuid"]),
                    )
                )
            self._dispatcher.trigger(
                ("projection", task["scope_key"]), [task["uuid"]], reservations
            )
        for row in session.execute(
            "SELECT DISTINCT project_id FROM messenger_publication_outbox LIMIT 128",
            (),
        ).fetchall():
            self._dispatcher.trigger(("publish", row["project_id"]))

    def tick(self) -> bool:
        completed = self._dispatcher.poll()
        for work, changes, error in completed:
            if error is not None:
                if isinstance(error, ScopeChanged):
                    self._dispatcher.trigger(work.key, work.values, error.reservations)
                    continue
                if getattr(error, "sqlstate", None) in {"40001", "40P01"}:
                    # PostgreSQL confirmed rollback. The original input is
                    # still durable; retrying it cannot lose a committed
                    # counter invalidation. Do not flood live work with a
                    # complete catalog rebuild for this normal retry path.
                    LOG.info("Retrying rolled-back Messenger job key=%s", work.key)
                    continue
                self._recovery_due = True
                LOG.error(
                    "Owned Messenger job failed key=%s",
                    work.key,
                    exc_info=(type(error), error, error.__traceback__),
                )
                continue
            if self._initial_topics is not None and work.key[0] == "topic":
                self._initial_topics.discard(work.key)
            if work.key[0] == "initialize":
                self._initial_topics = None
                self._initializing = False
            changes = typing.cast(owned_projection.Changes, changes)
            for (project_id, stream_uuid), users in changes.streams.items():
                self._dispatcher.trigger(
                    ("stream", _uuid(project_id), _uuid(stream_uuid)), users
                )
            for key in changes.folders:
                self._dispatcher.trigger(("folder", *key))
            for key in changes.projects:
                self._dispatcher.trigger(("project", *key))
        now = time.monotonic()
        if not self._started or self._dispatcher.idle or now - self._last_scan >= 0.02:
            with contexts.Context().session_manager() as session:
                if not self._started or self._recovery_due:
                    self._recover(session)
                    self._started = True
                    self._recovery_due = False
                self._scan(session)
            self._last_scan = now
        if self._initial_topics == set() and not self._initializing:
            self._dispatcher.trigger(("initialize",))
            self._initializing = True
        if not completed and not self._dispatcher.idle:
            time.sleep(0.002)
        return not self._dispatcher.idle

    def _command(
        self, session: sessions.PgSQLSession, command: typing.Mapping[str, typing.Any]
    ) -> typing.Any:
        store = MutationStore(command["project_id"], command["user_uuid"])
        operation = command["operation"]
        resource_uuid = command["resource_uuid"]
        values = command["payload"]["values"]
        for target in command["payload"]["topics"]:
            if "canonical_message_uuid" not in target:
                continue
            canonical = session.execute(
                "SELECT ingest_sequence FROM messenger_messages WHERE project_id=%s AND uuid=%s",
                (store.project_uuid, _uuid(target["canonical_message_uuid"])),
            ).fetchone()
            if (
                canonical is not None
                and canonical["ingest_sequence"] != target["ingest_sequence"]
            ):
                raise exceptions.ValidationErrorException()
        if "_membership_generation" in values:
            state = session.execute(
                """SELECT membership_generation FROM messenger_user_message_states
                   WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s""",
                (store.project_uuid, resource_uuid, store.user_uuid),
            ).fetchone()
            if (
                state is None
                or state["membership_generation"] != values["_membership_generation"]
            ):
                raise exceptions.ValidationErrorException()
        if operation == "edit":
            return store.update_message(resource_uuid, values)
        if operation == "delete":
            return store.delete_message(resource_uuid)
        if operation == "reaction_create":
            return store.create_resource("message_reactions", values)
        if operation == "reaction_delete":
            return store.delete_resource("message_reactions", resource_uuid)
        if operation in {"read", "read_up_to"}:
            return self._read(session, store, command)
        return store.perform_action("messages", resource_uuid, operation, values)

    def _read(
        self,
        session: sessions.PgSQLSession,
        store: MutationStore,
        command: typing.Mapping[str, typing.Any],
    ) -> dict[str, typing.Any]:
        message = store._message(command["resource_uuid"])
        if (
            command["operation"] == "read_up_to"
            and str(message.topic_uuid) != command["payload"]["values"]["_topic_uuid"]
        ):
            raise exceptions.ValidationErrorException()
        actor = session.execute(
            "SELECT current_setting('workspace.outgoing_actor',true) AS actor"
        ).fetchone()["actor"]
        session.execute(
            "SELECT set_config('workspace.outgoing_actor',%s,true)",
            (str(store.user_uuid),),
        )
        session.execute("SELECT set_config('workspace.batch_flags', 'on', true)", ())
        changed = session.execute(
            """UPDATE messenger_user_message_states AS state
               SET read_at=NOW(), updated_at=NOW()
               FROM messenger_message_placements AS placement,
                    messenger_messages AS canonical,
                    messenger_stream_bindings AS membership,
                    messenger_user_message_bindings AS access
               WHERE state.project_id=%s AND state.user_uuid=%s AND state.read_at IS NULL
                 AND placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                 AND placement.topic_uuid=%s
                 AND canonical.project_id=placement.project_id AND canonical.uuid=placement.message_uuid AND canonical.deleted_at IS NULL
                 AND membership.project_id=state.project_id AND membership.user_uuid=state.user_uuid
                 AND membership.stream_uuid=placement.stream_uuid AND membership.active
                 AND membership.membership_generation=state.membership_generation
                 AND access.project_id=state.project_id AND access.user_uuid=state.user_uuid
                 AND access.placement_uuid=state.placement_uuid AND access.visibility='visible'
                 AND access.membership_generation=state.membership_generation
                 AND (%s='read_up_to' OR placement.uuid=%s)
                 AND (canonical.created_at, placement.uuid)<=(%s::timestamptz,%s::uuid)
               RETURNING state.uuid, state.placement_uuid, state.mentioned,
                         state.membership_generation""",
            (
                store.project_uuid,
                store.user_uuid,
                message.topic_uuid,
                command["operation"],
                message.uuid,
                message.created_at,
                message.uuid,
            ),
        ).fetchall()
        session.execute("SELECT set_config('workspace.batch_flags', 'off', true)", ())
        session.execute(
            "SELECT set_config('workspace.outgoing_actor',%s,true)", (actor or "",)
        )
        changes = typing.cast(owned_projection.Changes, owned_projection.CURRENT.get())
        if not changed:
            # Preserve the public idempotent repair behavior.
            changes.topic(
                store.project_uuid,
                message.stream_uuid,
                message.topic_uuid,
                [store.user_uuid],
            )
            return v2_store._public(store._message(message.uuid), "messages")
        session.execute(
            """WITH delta AS (
                 SELECT count(*) AS unread,
                        count(*) FILTER (WHERE CASE
                          WHEN personal.notification_mode='mute' THEN stream.source_name='zulip' AND changed.mentioned
                          WHEN personal.notification_mode='follow' THEN true
                          WHEN personal.notification_mode='unmute' THEN stream.source_name='zulip' OR changed.mentioned
                          WHEN membership.notification_mode='all_messages' THEN true
                          WHEN membership.notification_mode='mentions_only' THEN changed.mentioned
                          ELSE false END) AS active
                 FROM unnest(%s::uuid[],%s::boolean[],%s::integer[])
                   AS changed(placement_uuid,mentioned,membership_generation)
                 JOIN messenger_user_message_bindings AS access
                   ON access.project_id=%s AND access.user_uuid=%s
                  AND access.placement_uuid=changed.placement_uuid AND access.visibility='visible'
                  AND access.membership_generation=changed.membership_generation
                 JOIN messenger_stream_bindings AS membership
                   ON membership.project_id=access.project_id AND membership.user_uuid=access.user_uuid
                  AND membership.stream_uuid=%s AND membership.active
                  AND membership.membership_generation=changed.membership_generation
                 JOIN messenger_streams AS stream ON stream.project_id=membership.project_id AND stream.uuid=membership.stream_uuid
                 LEFT JOIN messenger_user_topic_bindings AS personal
                   ON personal.project_id=membership.project_id AND personal.user_uuid=membership.user_uuid
                  AND personal.topic_uuid=%s
               ) UPDATE messenger_user_topic_bindings AS binding
               SET unread_count=binding.unread_count-delta.unread,
                   active_unread_count=binding.active_unread_count-delta.active,
                   passive_unread_count=binding.passive_unread_count-delta.unread+delta.active,
                   updated_at=NOW()
               FROM delta WHERE binding.project_id=%s AND binding.user_uuid=%s AND binding.topic_uuid=%s""",
            (
                [row["placement_uuid"] for row in changed],
                [row["mentioned"] for row in changed],
                [row["membership_generation"] for row in changed],
                store.project_uuid,
                store.user_uuid,
                message.stream_uuid,
                message.topic_uuid,
                store.project_uuid,
                store.user_uuid,
                message.topic_uuid,
            ),
        )
        v2_projection._emit_unread_snapshots(
            session,
            store.project_uuid,
            message.stream_uuid,
            message.topic_uuid,
            [store.user_uuid],
            "user-topic",
        )
        changes.stream(store.project_uuid, message.stream_uuid, [store.user_uuid])
        updated = store._message(message.uuid)
        emitted = events.create_message_read_event(updated, session=session)
        v2_projection._guard_emitted_events(
            session,
            emitted,
            project_id=store.project_uuid,
            stream_uuid=message.stream_uuid,
        )
        return v2_store._public(updated, "messages")

    def _commands(
        self, session: sessions.PgSQLSession, work: keyed_dispatcher.Work
    ) -> None:
        for command in session.execute(
            """SELECT * FROM messenger_mutation_commands WHERE uuid=ANY(%s::uuid[])
               AND status='pending' ORDER BY sequence""",
            (list(work.values),),
        ).fetchall():
            session.execute("SAVEPOINT mutation_command", ())
            try:
                result = self._command(session, command)
                status = "applied"
            except exceptions.RestAlchemyException as error:
                if not 400 <= error.code < 500:
                    raise
                session.execute("ROLLBACK TO SAVEPOINT mutation_command", ())
                if error.code == 404 and command["operation"] in {
                    "delete",
                    "reaction_delete",
                }:
                    result, status = None, "applied"
                else:
                    result, status = {"error": type(error).__name__}, "rejected"
            session.execute("RELEASE SAVEPOINT mutation_command", ())
            if status == "applied" and command["operation"] == "delete":
                for topic in command["payload"]["topics"]:
                    session.execute(
                        """INSERT INTO messenger_message_delete_tombstones
                               (project_id,canonical_message_uuid,ingest_sequence)
                           VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (
                            command["project_id"],
                            _uuid(topic["canonical_message_uuid"]),
                            topic["ingest_sequence"],
                        ),
                    )
            session.execute(
                """UPDATE messenger_mutation_commands SET status=%s, result=%s::jsonb,
                   finished_at=clock_timestamp(), payload=jsonb_build_object('topics',payload->'topics') WHERE uuid=%s""",
                (
                    status,
                    json.dumps(
                        v2_projection._plain(result) if status == "rejected" else None
                    ),
                    command["uuid"],
                ),
            )

    def _projections(
        self, session: sessions.PgSQLSession, work: keyed_dispatcher.Work
    ) -> None:
        for task in session.execute(
            """SELECT task.*, event.created_at AS outbox_created_at
               FROM messenger_projection_tasks AS task
               JOIN messenger_domain_outbox_events AS event
                 ON event.project_id=task.project_id AND event.uuid=task.outbox_event_uuid
               WHERE task.uuid=ANY(%s::uuid[]) AND task.status<>'completed'
               ORDER BY task.created_at, task.uuid""",
            (list(work.values),),
        ).fetchall():
            if task["task_kind"] == "read_counters" and task["payload"].get(
                "emit_public_event", True
            ):
                payload = task["payload"]
                changes = typing.cast(
                    owned_projection.Changes, owned_projection.CURRENT.get()
                )
                if task["scope_kind"] == "user-topic":
                    changes.topic(
                        task["project_id"],
                        _uuid(payload["stream_uuid"]),
                        _uuid(payload["topic_uuid"]),
                        v2_projection._counter_users(payload),
                    )
                else:
                    changes.stream(
                        task["project_id"],
                        _uuid(payload["stream_uuid"]),
                        v2_projection._counter_users(payload),
                    )
                completed = True
            else:
                completed = v2_projection._process_task(session, task, 1000)
            session.execute(
                """UPDATE messenger_projection_tasks SET status=%s, lease_owner=NULL,
                   lease_expires_at=NULL, next_retry_at=NULL, last_error=NULL, updated_at=NOW()
                   WHERE project_id=%s AND uuid=%s""",
                (
                    "completed" if completed else "pending",
                    task["project_id"],
                    task["uuid"],
                ),
            )
            if completed:
                v2_projection._purge_completed_tombstone(session, task)
            if task["task_kind"] == "folder_projection" and task["payload"].get(
                "user_uuid"
            ):
                typing.cast(
                    owned_projection.Changes, owned_projection.CURRENT.get()
                ).projects.add(
                    (task["project_id"], _uuid(task["payload"]["user_uuid"]))
                )

    def _stream(
        self,
        session: sessions.PgSQLSession,
        project_id: uuid.UUID,
        stream_uuid: uuid.UUID,
        users: typing.Iterable[uuid.UUID],
    ) -> None:
        users = [_uuid(user) for user in users]
        session.execute(
            """WITH totals AS (
                 SELECT membership.uuid,
                        COALESCE(sum(topic_binding.unread_count),0) AS unread,
                        COALESCE(sum(topic_binding.active_unread_count),0) AS active,
                        (array_agg(topic_binding.last_message_uuid
                            ORDER BY preview.created_at DESC NULLS LAST,
                                     topic_binding.last_message_uuid DESC NULLS LAST)
                            FILTER (WHERE preview.uuid IS NOT NULL))[1] AS last_message_uuid
                 FROM messenger_stream_bindings AS membership
                 LEFT JOIN messenger_topics AS topic ON topic.project_id=membership.project_id
                   AND topic.stream_uuid=membership.stream_uuid AND topic.deleted_at IS NULL
                 LEFT JOIN messenger_user_topic_bindings AS topic_binding
                   ON topic_binding.project_id=topic.project_id AND topic_binding.topic_uuid=topic.uuid
                  AND topic_binding.user_uuid=membership.user_uuid
                 LEFT JOIN messenger_message_placements AS last_placement
                   ON last_placement.project_id=topic_binding.project_id
                  AND last_placement.uuid=topic_binding.last_message_uuid
                 LEFT JOIN messenger_messages AS preview
                   ON preview.project_id=last_placement.project_id
                  AND preview.uuid=last_placement.message_uuid AND preview.deleted_at IS NULL
                 WHERE membership.project_id=%s AND membership.stream_uuid=%s
                   AND membership.user_uuid=ANY(%s::uuid[]) AND membership.active
                 GROUP BY membership.uuid
               ) UPDATE messenger_stream_bindings AS binding
                 SET unread_count=totals.unread, active_unread_count=totals.active,
                     passive_unread_count=totals.unread-totals.active,
                     last_message_uuid=totals.last_message_uuid, updated_at=NOW()
               FROM totals WHERE binding.project_id=%s AND binding.uuid=totals.uuid""",
            (project_id, stream_uuid, users, project_id),
        )
        v2_projection._emit_unread_snapshots(
            session, project_id, stream_uuid, None, users, "user-stream"
        )
        for row in session.execute(
            """SELECT user_uuid, folder_uuid FROM messenger_user_folder_bindings
               WHERE project_id=%s AND user_uuid=ANY(%s::uuid[])""",
            (project_id, users),
        ).fetchall():
            typing.cast(
                owned_projection.Changes, owned_projection.CURRENT.get()
            ).folders[(project_id, row["user_uuid"], row["folder_uuid"])] = True

    def _publish(self, session: sessions.PgSQLSession, project_id: uuid.UUID) -> None:
        publication.publish(session, project_id)

    def _execute(self, work: keyed_dispatcher.Work) -> owned_projection.Changes:
        try:
            return self._execute_transaction(work)
        except Exception as error:
            if getattr(error, "workspace_sync_scopes", ()):
                with contexts.Context().session_manager() as session:
                    synchronization.record_error(error, session=session)
            raise

    def _execute_transaction(
        self, work: keyed_dispatcher.Work
    ) -> owned_projection.Changes:
        with contexts.Context().session_manager() as session:
            session.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())
            with owned_projection.capture() as changes:
                session.execute(
                    "SELECT set_config('workspace.owned_mutations', 'on', true)", ()
                )
                kind = work.key[0]
                self._check_reservations(session, work)
                if kind == "receipts":
                    session.execute(
                        """DELETE FROM messenger_mutation_commands WHERE uuid IN (
                             SELECT uuid FROM messenger_mutation_commands
                             WHERE status IN ('applied','rejected')
                               AND finished_at < NOW() - %s * interval '1 second'
                               AND NOT EXISTS (
                                 SELECT 1 FROM messenger_mutation_commands AS sibling
                                 WHERE sibling.request_uuid=messenger_mutation_commands.request_uuid
                                   AND (sibling.status='pending'
                                     OR sibling.finished_at >= NOW() - %s * interval '1 second'))
                             ORDER BY finished_at,uuid LIMIT 1000)""",
                        (
                            self._receipt_retention_seconds,
                            self._receipt_retention_seconds,
                        ),
                    )
                elif kind == "initialize":
                    session.execute(
                        "UPDATE messenger_mutation_projection_state SET initialized=TRUE WHERE singleton",
                        (),
                    )
                elif kind == "command":
                    self._commands(session, work)
                elif kind == "projection":
                    self._projections(session, work)
                elif kind == "topic":
                    project_id, topic_uuid = work.key[1:]
                    for stream_uuid, user in work.values:
                        changes.topic(project_id, stream_uuid, topic_uuid, [user])
                elif kind == "stream":
                    self._stream(session, work.key[1], work.key[2], work.values)
                elif kind == "folder":
                    project_id, user_uuid, folder_uuid = work.key[1:]
                    v2_projection._process_folder_projection(
                        session,
                        {
                            "project_id": project_id,
                            "payload": {
                                "source_kind": "counters.updated",
                                "user_uuid": user_uuid,
                                "folder_uuid": folder_uuid,
                            },
                        },
                    )
                    changes.projects.add((project_id, user_uuid))
                elif kind == "project":
                    project_id, user_uuid = work.key[1:]
                    session.execute(
                        """WITH total AS (
                             SELECT COALESCE(sum(binding.unread_count),0) AS unread,
                                    COALESCE(sum(binding.active_unread_count),0) AS active
                             FROM messenger_stream_bindings AS binding
                             JOIN messenger_streams AS stream ON stream.project_id=binding.project_id
                               AND stream.uuid=binding.stream_uuid AND stream.deleted_at IS NULL
                               AND NOT stream.is_archived
                             WHERE binding.project_id=%s AND binding.user_uuid=%s AND binding.active
                           ) UPDATE messenger_project_users SET unread_count=total.unread,
                             active_unread_count=total.active, passive_unread_count=total.unread-total.active
                           FROM total WHERE project_id=%s AND user_uuid=%s""",
                        (project_id, user_uuid, project_id, user_uuid),
                    )
                elif kind == "publish":
                    self._publish(session, work.key[1])
                for (
                    project_id,
                    stream_uuid,
                    topic_uuid,
                ), users in changes.topics.items():
                    v2_projection._refresh_recipient_counters(
                        session,
                        project_id,
                        stream_uuid,
                        topic_uuid,
                        users,
                        "user-topic",
                    )
                    v2_projection._emit_unread_snapshots(
                        session,
                        project_id,
                        stream_uuid,
                        topic_uuid,
                        users,
                        "user-topic",
                    )
                    changes.stream(project_id, stream_uuid, users)
        return changes

    def _check_reservations(
        self, session: sessions.PgSQLSession, work: keyed_dispatcher.Work
    ) -> None:
        kind = work.key[0]
        pairs = []
        if kind == "command" and ":message:" in work.key[1]:
            project_id, _kind, message_uuid = work.key[1].split(":")
            pairs.append((_uuid(project_id), _uuid(message_uuid)))
        if kind in {"command", "projection"}:
            if kind == "command":
                rows = session.execute(
                    "SELECT project_id,resource_uuid,payload,operation FROM messenger_mutation_commands WHERE uuid=ANY(%s::uuid[]) AND status='pending'",
                    (list(work.values),),
                ).fetchall()
                placements = [
                    (row["project_id"], row["resource_uuid"])
                    for row in rows
                    if row["operation"]
                    in {"read", "read_up_to", "star", "unstar", "pin", "unpin"}
                ]
                placements.extend(
                    (row["project_id"], _uuid(row["payload"]["values"]["message_uuid"]))
                    for row in rows
                    if row["operation"] in {"reaction_create", "reaction_update"}
                    and "message_uuid" in row["payload"]["values"]
                )
            else:
                rows = session.execute(
                    "SELECT project_id,payload FROM messenger_projection_tasks WHERE uuid=ANY(%s::uuid[])",
                    (list(work.values),),
                ).fetchall()
                placements = [
                    (
                        row["project_id"],
                        _uuid(
                            row["payload"].get("placement_uuid")
                            or row["payload"].get("placement", {}).get("uuid")
                        ),
                    )
                    for row in rows
                    if row["payload"].get("placement_uuid")
                    or row["payload"].get("placement", {}).get("uuid")
                ]
            for project_id, placement_uuid in placements:
                row = session.execute(
                    "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
                    (project_id, placement_uuid),
                ).fetchone()
                if row is not None:
                    pairs.append((project_id, row["message_uuid"]))
        required = set()
        for project_id, canonical_uuid in pairs:
            topics = session.execute(
                """SELECT topic_uuid FROM messenger_message_placements
                   WHERE project_id=%s AND message_uuid=%s""",
                (project_id, canonical_uuid),
            ).fetchall()
            required.update(self._topic_reservations(project_id, topics))
        if not required.issubset(work.reservations):
            raise ScopeChanged(required)

    def close(self) -> None:
        self._dispatcher.close()
        if self._ownership is not None:
            self._ownership.close()
            self._ownership = None
