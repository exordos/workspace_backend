# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Provider-owned CRUD over the clean Workspace v3 Messenger schema."""

import datetime
import hashlib
import json
import re
import tempfile
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exceptions

from workspace.messenger_api import event_origin
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import v3_store


RESOURCE_TYPES = {
    "users": "user",
    "streams": "stream",
    "stream_bindings": "stream_binding",
    "topics": "topic",
    "topic_bindings": "topic_binding",
    "messages": "message",
    "message_flags": "message_flag",
    "message_reactions": "message_reaction",
}
TABLES = {
    "users": "users",
    "streams": "streams",
    "stream_bindings": "stream_bindings",
    "topics": "topics",
    "topic_bindings": "topic_bindings",
    "messages": "messages",
    "message_flags": "message_flags",
    "message_reactions": "message_reactions",
}
MAX_BATCH_SIZE = 500
MAX_PAGE_SIZE = 500
HISTORY_RESOURCES = frozenset({"messages", "message_flags", "message_reactions"})
ZERO_UUID = sys_uuid.UUID(int=0)
IMMUTABLE_FIELDS = {
    "stream_bindings": ("stream_uuid", "user_uuid"),
    "topic_bindings": ("stream_uuid", "topic_uuid", "user_uuid"),
    "messages": ("author_uuid",),
    "message_flags": ("stream_uuid", "message_uuid", "user_uuid"),
    "message_reactions": ("message_uuid", "user_uuid"),
}
PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _error(
    status: int,
    error: str,
    message: str,
    *,
    item_index: int | None = None,
) -> typing.NoReturn:
    raise messenger_exceptions.ProviderApiError(
        status=status,
        error=error,
        message=message,
        item_index=item_index,
    )


def parse_timestamp(value: typing.Any, field: str) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _error(422, "invalid_timestamp", f"{field} must be UTC ISO-8601")
    else:
        _error(422, "invalid_timestamp", f"{field} must be UTC ISO-8601")
    if result.tzinfo is None or result.utcoffset() != datetime.timedelta(0):
        _error(422, "invalid_timestamp", f"{field} must use UTC")
    return result.astimezone(datetime.timezone.utc)


def parse_uuid(value: typing.Any, field: str) -> sys_uuid.UUID:
    try:
        return sys_uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        _error(422, "invalid_uuid", f"{field} must be a UUID")


def parse_content_hash(value: typing.Any) -> bytes:
    if not isinstance(value, str) or len(value) != 64:
        _error(422, "invalid_content_hash", "content_hash must be 64 hex characters")
    try:
        return bytes.fromhex(value)
    except ValueError:
        _error(422, "invalid_content_hash", "content_hash must be 64 hex characters")


def jsonable(value: typing.Any) -> typing.Any:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return (
            value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def canonical_hash(value: typing.Any) -> bytes:
    return hashlib.sha256(
        json.dumps(
            jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).digest()


class ProviderEntityStore:
    """Mutate only entities owned by one authenticated provider consumer."""

    def __init__(
        self,
        session: typing.Any,
        project_uuid: sys_uuid.UUID,
        iam_user_uuid: sys_uuid.UUID,
        provider: typing.Mapping[str, typing.Any],
    ) -> None:
        self.session = session
        self.project_uuid = project_uuid
        self.iam_user_uuid = iam_user_uuid
        self.provider_uuid = sys_uuid.UUID(str(provider["uuid"]))
        self.provider_name = str(provider["name"])
        if PROVIDER_NAME.fullmatch(self.provider_name) is None:
            _error(
                403,
                "invalid_provider_name",
                "Provider name must be a lowercase source identifier",
            )
        self.events = v3_store.MessengerV3Store(project_uuid, iam_user_uuid)
        self._stream_recipients_cache: dict[
            sys_uuid.UUID, tuple[sys_uuid.UUID, ...]
        ] = {}
        self._stream_providers_cache: dict[
            sys_uuid.UUID, tuple[sys_uuid.UUID, ...]
        ] = {}

    def _stream_recipients(
        self, stream_uuid: sys_uuid.UUID
    ) -> tuple[sys_uuid.UUID, ...]:
        if stream_uuid not in self._stream_recipients_cache:
            self._stream_recipients_cache[stream_uuid] = self.events._stream_recipients(
                stream_uuid
            )
        return self._stream_recipients_cache[stream_uuid]

    def _stream_providers(
        self, stream_uuid: sys_uuid.UUID
    ) -> tuple[sys_uuid.UUID, ...]:
        if stream_uuid not in self._stream_providers_cache:
            self._stream_providers_cache[stream_uuid] = (
                self.events._provider_consumers_for_stream(stream_uuid)
            )
        return self._stream_providers_cache[stream_uuid]

    def _invalidate_stream_recipients(self, stream_uuid: sys_uuid.UUID) -> None:
        self._stream_recipients_cache.pop(stream_uuid, None)

    def lock_entities(
        self,
        entities: typing.Iterable[tuple[str, sys_uuid.UUID]],
    ) -> None:
        keys = sorted(
            {
                f"provider-entity:{RESOURCE_TYPES[resource]}:{entity_uuid}"
                for resource, entity_uuid in entities
            }
        )
        if not keys:
            return
        self.session.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(input.key, 0))
            FROM (
                SELECT unnest(%s::text[]) AS key ORDER BY key
            ) AS input
            """,
            (keys,),
        )

    def defer_backfill_counter_projections(self) -> None:
        """Record activity so background counters wait for a quiet project."""
        lock_key = f"provider-backfill-read-counters:{self.project_uuid}"
        acquired = self.session.execute(
            """
            SELECT pg_try_advisory_xact_lock(
                hashtextextended(%s, 0)
            ) AS acquired
            """,
            (lock_key,),
        ).fetchone()["acquired"]
        if not acquired:
            return
        self.session.execute(
            """
            UPDATE workspace_v3.provider_consumers
            SET updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, self.provider_uuid),
        )

    def _state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        row = self.session.execute(
            """
            SELECT provider_uuid, content_hash, source_content_hash,
                   source_updated_at,
                   created_at, updated_at
            FROM workspace_v3.provider_entity_states
            WHERE project_id = %s AND entity_type = %s AND entity_uuid = %s
            """,
            (self.project_uuid, RESOURCE_TYPES[resource], entity_uuid),
        ).fetchone()
        return None if row is None else dict(row)

    def _entity_exists(self, resource: str, entity_uuid: sys_uuid.UUID) -> bool:
        table = TABLES[resource]
        parameters: tuple[typing.Any, ...]
        if resource == "users":
            query = f"SELECT 1 FROM workspace_v3.{table} WHERE uuid = %s"
            parameters = (entity_uuid,)
        else:
            query = (
                f"SELECT 1 FROM workspace_v3.{table} "
                "WHERE project_id = %s AND uuid = %s"
            )
            parameters = (self.project_uuid, entity_uuid)
        return self.session.execute(query, parameters).fetchone() is not None

    def _entity_project(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> sys_uuid.UUID | None:
        if resource == "users":
            return None
        row = self.session.execute(
            f"SELECT project_id FROM workspace_v3.{TABLES[resource]} WHERE uuid = %s",
            (entity_uuid,),
        ).fetchone()
        if row is None:
            return None
        return sys_uuid.UUID(str(row["project_id"]))

    def _ensure_project_identity_available(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> None:
        project_uuid = self._entity_project(resource, entity_uuid)
        if project_uuid is None or project_uuid == self.project_uuid:
            return
        _error(
            409,
            "entity_project_conflict",
            "The entity UUID already belongs to another project",
        )

    def _owned_state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        state = self._state(resource, entity_uuid)
        if state is not None and sys_uuid.UUID(str(state["provider_uuid"])) != (
            self.provider_uuid
        ):
            _error(
                409,
                "entity_owned_by_another_provider",
                "The entity UUID is owned by another provider",
            )
        return state

    def _ensure_create_available(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        state: dict[str, typing.Any] | None,
        entity_exists: bool,
        data: dict[str, typing.Any],
    ) -> None:
        if not self._incoming_entity_in_provider_scope(resource, data):
            _error(
                409,
                "entity_not_provider_owned",
                "The entity UUID already belongs to native or unmanaged data",
            )
        if state is not None:
            return
        if entity_exists and self._entity_in_provider_scope(resource, entity_uuid):
            return
        if not entity_exists:
            return
        _error(
            409,
            "entity_not_provider_owned",
            "The entity UUID already belongs to native or unmanaged data",
        )

    def _incoming_entity_in_provider_scope(
        self,
        resource: str,
        data: dict[str, typing.Any],
    ) -> bool:
        if resource in {"users", "streams"}:
            return True
        if resource in {
            "stream_bindings",
            "topics",
            "topic_bindings",
            "messages",
        }:
            stream_uuid = parse_uuid(data["stream_uuid"], "stream_uuid")
        elif resource in {"message_flags", "message_reactions"}:
            message_uuid = parse_uuid(data["message_uuid"], "message_uuid")
            row = self.session.execute(
                """
                SELECT stream.source_name
                FROM workspace_v3.messages AS message
                JOIN workspace_v3.streams AS stream
                  ON stream.project_id = message.project_id
                 AND stream.uuid = message.stream_uuid
                WHERE message.project_id = %s AND message.uuid = %s
                """,
                (self.project_uuid, message_uuid),
            ).fetchone()
            return row is not None and row["source_name"] == self.provider_name
        else:
            return False
        row = self.session.execute(
            """
            SELECT source_name FROM workspace_v3.streams
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, stream_uuid),
        ).fetchone()
        return row is not None and row["source_name"] == self.provider_name

    def _entity_in_provider_scope(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> bool:
        """Allow a provider to converge native rows inside its own streams."""
        if resource == "users":
            row = self.session.execute(
                "SELECT 1 FROM workspace_v3.users WHERE uuid = %s AND source = %s",
                (entity_uuid, self.provider_name),
            ).fetchone()
        elif resource == "streams":
            row = self.session.execute(
                "SELECT 1 FROM workspace_v3.streams "
                "WHERE project_id = %s AND uuid = %s AND source_name = %s",
                (self.project_uuid, entity_uuid, self.provider_name),
            ).fetchone()
        elif resource in {
            "stream_bindings",
            "topics",
            "topic_bindings",
            "messages",
        }:
            row = self.session.execute(
                f"""
                SELECT 1
                FROM workspace_v3.{TABLES[resource]} AS entity
                JOIN workspace_v3.streams AS stream
                  ON stream.project_id = entity.project_id
                 AND stream.uuid = entity.stream_uuid
                WHERE entity.project_id = %s AND entity.uuid = %s
                  AND stream.source_name = %s
                """,
                (self.project_uuid, entity_uuid, self.provider_name),
            ).fetchone()
        elif resource in {"message_flags", "message_reactions"}:
            row = self.session.execute(
                f"""
                SELECT 1
                FROM workspace_v3.{TABLES[resource]} AS entity
                JOIN workspace_v3.messages AS message
                  ON message.project_id = entity.project_id
                 AND message.uuid = entity.message_uuid
                JOIN workspace_v3.streams AS stream
                  ON stream.project_id = message.project_id
                 AND stream.uuid = message.stream_uuid
                WHERE entity.project_id = %s AND entity.uuid = %s
                  AND stream.source_name = %s
                """,
                (self.project_uuid, entity_uuid, self.provider_name),
            ).fetchone()
        else:
            return False
        return row is not None

    def _set_state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        content_hash: bytes,
        source_content_hash: bytes,
        source_updated_at: datetime.datetime,
    ) -> dict[str, typing.Any]:
        row = self.session.execute(
            """
            INSERT INTO workspace_v3.provider_entity_states (
                project_id, provider_uuid, entity_type, entity_uuid,
                content_hash, source_content_hash, source_updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, provider_uuid, entity_type, entity_uuid)
            DO UPDATE SET content_hash = EXCLUDED.content_hash,
                          source_content_hash = EXCLUDED.source_content_hash,
                          source_updated_at = EXCLUDED.source_updated_at,
                          updated_at = clock_timestamp()
            RETURNING source_updated_at, created_at, updated_at
            """,
            (
                self.project_uuid,
                self.provider_uuid,
                RESOURCE_TYPES[resource],
                entity_uuid,
                content_hash,
                source_content_hash,
                source_updated_at,
            ),
        ).fetchone()
        return dict(row)

    def upsert(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        content_hash: bytes,
        data: dict[str, typing.Any],
        source_updated_at: datetime.datetime | None = None,
        *,
        rebind_identity: bool = False,
        emit_event: bool = True,
        expand_message_flags: bool = True,
    ) -> dict[str, typing.Any]:
        source_timestamp_provided = source_updated_at is not None
        source_updated_at = source_updated_at or datetime.datetime.now(
            datetime.timezone.utc
        )
        self._ensure_project_identity_available(resource, entity_uuid)
        state = self._owned_state(resource, entity_uuid)
        if state is not None:
            current_source_updated_at = state["source_updated_at"]
            same_content = bytes(state["source_content_hash"]) == content_hash
            if not source_timestamp_provided and same_content:
                return self._result(resource, entity_uuid, "unchanged", state)
            if (
                source_timestamp_provided
                and source_updated_at < current_source_updated_at
            ):
                return self._result(resource, entity_uuid, "unchanged", state)
            if (
                source_timestamp_provided
                and source_updated_at == current_source_updated_at
            ):
                if same_content:
                    return self._result(resource, entity_uuid, "unchanged", state)
                _error(
                    409,
                    "entity_version_conflict",
                    "The entity version has conflicting content",
                )
            if same_content:
                new_state = self._set_state(
                    resource,
                    entity_uuid,
                    bytes(state["content_hash"]),
                    content_hash,
                    source_updated_at,
                )
                return self._result(resource, entity_uuid, "unchanged", new_state)
        entity_exists = self._entity_exists(resource, entity_uuid)
        self._ensure_create_available(
            resource,
            entity_uuid,
            state,
            entity_exists,
            data,
        )
        previous_user_uuid: sys_uuid.UUID | None = None
        previous_message_flag_event: dict[str, typing.Any] | None = None
        previous_topic_event: dict[str, typing.Any] | None = None
        previous_message_event: dict[str, typing.Any] | None = None
        if emit_event and resource == "topics" and entity_exists:
            previous_topic_event = dict(
                self.session.execute(
                    """
                    SELECT stream_uuid
                    FROM workspace_v3.topics
                    WHERE project_id = %s AND uuid = %s
                    """,
                    (self.project_uuid, entity_uuid),
                ).fetchone()
            )
            previous_topic_event["recipients"] = self._stream_recipients(
                previous_topic_event["stream_uuid"]
            )
        if emit_event and resource == "messages" and entity_exists:
            previous_message_event = dict(
                self.session.execute(
                    """
                    SELECT stream_uuid, topic_uuid, author_uuid, source_name
                    FROM workspace_v3.messages
                    WHERE project_id = %s AND uuid = %s
                    """,
                    (self.project_uuid, entity_uuid),
                ).fetchone()
            )
            previous_message_event["recipients"] = self._stream_recipients(
                previous_message_event["stream_uuid"]
            )
        if state is not None:
            if rebind_identity:
                if resource == "message_flags":
                    previous_message_flag_event = self._capture_delete_event(
                        resource, entity_uuid
                    )
                previous_user_uuid = self._prepare_identity_rebind(
                    resource, entity_uuid, data
                )
            else:
                self._ensure_identity_unchanged(resource, entity_uuid, data)
        status = (
            "created"
            if state is None and (resource == "users" or not entity_exists)
            else "updated"
        )
        with event_origin.use("provider", self.provider_uuid):
            if resource == "messages":
                self._upsert_messages(
                    entity_uuid,
                    data,
                    expand_flags=expand_message_flags,
                )
            else:
                getattr(self, f"_upsert_{resource}")(entity_uuid, data)
            if resource == "message_reactions" and not emit_event:
                self._suppress_reaction_projection_events(entity_uuid)
            canonical_data = self.provider_data_for_entity(resource, entity_uuid)
            if canonical_data is None:
                raise RuntimeError("provider upsert did not materialize its entity")
            new_state = self._set_state(
                resource,
                entity_uuid,
                canonical_hash(canonical_data),
                content_hash,
                source_updated_at,
            )
            if emit_event:
                self._emit_upsert(
                    resource,
                    entity_uuid,
                    status,
                    data,
                    previous_user_uuid=previous_user_uuid,
                )
                if previous_message_flag_event is not None and (
                    previous_message_flag_event["message_uuid"],
                    previous_message_flag_event["recipients"],
                ) != (
                    parse_uuid(data["message_uuid"], "message_uuid"),
                    (parse_uuid(data["user_uuid"], "user_uuid"),),
                ):
                    self._emit_delete(
                        "message_flags",
                        entity_uuid,
                        previous_message_flag_event,
                    )
                if previous_topic_event is not None and previous_topic_event[
                    "stream_uuid"
                ] != parse_uuid(data["stream_uuid"], "stream_uuid"):
                    current_recipients = set(
                        self._stream_recipients(
                            parse_uuid(data["stream_uuid"], "stream_uuid")
                        )
                    )
                    previous_recipients = (
                        recipient
                        for recipient in previous_topic_event["recipients"]
                        if recipient not in current_recipients
                    )
                    payload = {
                        "uuid": str(entity_uuid),
                        "stream_uuid": previous_topic_event["stream_uuid"],
                    }
                    self.events._emit(
                        kind="topic.deleted",
                        object_type="topic",
                        action="deleted",
                        entity_uuid=entity_uuid,
                        payloads={
                            recipient: payload for recipient in previous_recipients
                        },
                    )
                if previous_message_event is not None and previous_message_event[
                    "stream_uuid"
                ] != parse_uuid(data["stream_uuid"], "stream_uuid"):
                    current_recipients = set(
                        self._stream_recipients(
                            parse_uuid(data["stream_uuid"], "stream_uuid")
                        )
                    )
                    previous_message_event["recipients"] = tuple(
                        recipient
                        for recipient in previous_message_event["recipients"]
                        if recipient not in current_recipients
                    )
                    payload = {
                        "uuid": str(entity_uuid),
                        "stream_uuid": previous_message_event["stream_uuid"],
                        "topic_uuid": previous_message_event["topic_uuid"],
                        "author_uuid": previous_message_event["author_uuid"],
                        "source_name": previous_message_event["source_name"],
                        "source": v3_store.source_projection(
                            previous_message_event["source_name"]
                        ),
                    }
                    self.events._emit(
                        kind="message.deleted",
                        object_type="message",
                        action="deleted",
                        entity_uuid=entity_uuid,
                        payloads={
                            recipient: payload
                            for recipient in previous_message_event["recipients"]
                        },
                    )
        return self._result(resource, entity_uuid, status, new_state)

    def _prepare_identity_rebind(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        data: dict[str, typing.Any],
    ) -> sys_uuid.UUID | None:
        """Make an explicit provider-owned identity migration conflict-safe."""
        if resource == "stream_bindings":
            return self._prepare_stream_binding_identity_rebind(entity_uuid, data)
        if resource != "message_flags":
            return None
        message_uuid = parse_uuid(data["message_uuid"], "message_uuid")
        user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
        conflict = self.session.execute(
            """
            SELECT flag.uuid
            FROM workspace_v3.message_flags AS flag
            WHERE flag.project_id = %s AND flag.message_uuid = %s
              AND flag.user_uuid = %s AND flag.uuid <> %s
            """,
            (self.project_uuid, message_uuid, user_uuid, entity_uuid),
        ).fetchone()
        if conflict is not None:
            conflict_owned = self.session.execute(
                """
                SELECT 1 FROM workspace_v3.provider_entity_states
                WHERE project_id = %s AND entity_type = 'message_flag'
                  AND entity_uuid = %s
                LIMIT 1
                """,
                (self.project_uuid, conflict["uuid"]),
            ).fetchone()
            if conflict_owned is not None:
                _error(
                    409,
                    "entity_identity_conflict",
                    "The target message flag is owned by a provider entity",
                )
            self.session.execute(
                """
                DELETE FROM workspace_v3.message_flags
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, conflict["uuid"]),
            )
        self.session.execute(
            """
            UPDATE workspace_v3.message_flags
            SET stream_uuid = %s, message_uuid = %s, user_uuid = %s,
                updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            """,
            (
                parse_uuid(data["stream_uuid"], "stream_uuid"),
                message_uuid,
                user_uuid,
                self.project_uuid,
                entity_uuid,
            ),
        )
        return None

    def _prepare_stream_binding_identity_rebind(
        self,
        entity_uuid: sys_uuid.UUID,
        data: dict[str, typing.Any],
    ) -> sys_uuid.UUID | None:
        """Move a provider binding and its dependent rows to another user."""
        current = self.session.execute(
            """
            SELECT * FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND uuid = %s
            """,
            (self.project_uuid, entity_uuid),
        ).fetchone()
        if current is None:
            return None
        target_user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
        if current["user_uuid"] == target_user_uuid:
            return None
        stream_uuid = parse_uuid(data["stream_uuid"], "stream_uuid")
        conflict = self.session.execute(
            """
            SELECT uuid FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
              AND uuid <> %s
            """,
            (self.project_uuid, stream_uuid, target_user_uuid, entity_uuid),
        ).fetchone()
        dependent_conflict = self.session.execute(
            """
            SELECT 1
            FROM workspace_v3.topic_bindings AS source
            JOIN workspace_v3.topic_bindings AS target
              ON target.project_id = source.project_id
             AND target.topic_uuid = source.topic_uuid
             AND target.user_uuid = %s
             AND target.uuid <> source.uuid
            WHERE source.project_id = %s AND source.stream_uuid = %s
              AND source.user_uuid = %s
            UNION ALL
            SELECT 1
            FROM workspace_v3.message_flags AS source
            JOIN workspace_v3.message_flags AS target
              ON target.project_id = source.project_id
             AND target.message_uuid = source.message_uuid
             AND target.user_uuid = %s
             AND target.uuid <> source.uuid
            WHERE source.project_id = %s AND source.stream_uuid = %s
              AND source.user_uuid = %s
            UNION ALL
            SELECT 1 FROM workspace_v3.drafts
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            LIMIT 1
            """,
            (
                target_user_uuid,
                self.project_uuid,
                stream_uuid,
                current["user_uuid"],
                target_user_uuid,
                self.project_uuid,
                stream_uuid,
                current["user_uuid"],
                self.project_uuid,
                stream_uuid,
                current["user_uuid"],
            ),
        ).fetchone()
        if conflict is not None or dependent_conflict is not None:
            _error(
                409,
                "entity_identity_conflict",
                "The target user already has data for this stream",
            )

        dependent_states = self.session.execute(
            """
            SELECT 'topic_bindings' AS resource, state.entity_uuid,
                   state.source_content_hash, state.source_updated_at
            FROM workspace_v3.provider_entity_states AS state
            JOIN workspace_v3.topic_bindings AS binding
              ON binding.project_id = state.project_id
             AND binding.uuid = state.entity_uuid
            WHERE state.project_id = %s AND state.provider_uuid = %s
              AND state.entity_type = 'topic_binding'
              AND binding.stream_uuid = %s AND binding.user_uuid = %s
            UNION ALL
            SELECT 'message_flags' AS resource, state.entity_uuid,
                   state.source_content_hash, state.source_updated_at
            FROM workspace_v3.provider_entity_states AS state
            JOIN workspace_v3.message_flags AS flag
              ON flag.project_id = state.project_id
             AND flag.uuid = state.entity_uuid
            WHERE state.project_id = %s AND state.provider_uuid = %s
              AND state.entity_type = 'message_flag'
              AND flag.stream_uuid = %s AND flag.user_uuid = %s
            """,
            (
                self.project_uuid,
                self.provider_uuid,
                stream_uuid,
                current["user_uuid"],
                self.project_uuid,
                self.provider_uuid,
                stream_uuid,
                current["user_uuid"],
            ),
        ).fetchall()

        self.session.execute(
            """
            SET CONSTRAINTS
                workspace_v3.topic_bindings_stream_binding_fkey,
                workspace_v3.message_flags_stream_binding_fkey,
                workspace_v3.drafts_stream_binding_fkey,
                workspace_v3.folder_items_stream_binding_fkey
            DEFERRED
            """
        )
        self.session.execute(
            """
            UPDATE workspace_v3.stream_bindings
            SET user_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (
                target_user_uuid,
                self.project_uuid,
                entity_uuid,
            ),
        )
        self.session.execute(
            """
            UPDATE workspace_v3.topic_bindings SET user_uuid = %s
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (
                target_user_uuid,
                self.project_uuid,
                stream_uuid,
                current["user_uuid"],
            ),
        )
        self.session.execute(
            """
            UPDATE workspace_v3.message_flags SET user_uuid = %s
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (
                target_user_uuid,
                self.project_uuid,
                stream_uuid,
                current["user_uuid"],
            ),
        )
        self.session.execute(
            """
            DELETE FROM workspace_v3.folder_items
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (self.project_uuid, stream_uuid, current["user_uuid"]),
        )
        for dependent_state in dependent_states:
            resource = dependent_state["resource"]
            dependent_uuid = sys_uuid.UUID(str(dependent_state["entity_uuid"]))
            dependent_data = self.provider_data_for_entity(resource, dependent_uuid)
            if dependent_data is not None:
                self._set_state(
                    resource,
                    dependent_uuid,
                    canonical_hash(dependent_data),
                    bytes(dependent_state["source_content_hash"]),
                    dependent_state["source_updated_at"],
                )
        return sys_uuid.UUID(str(current["user_uuid"]))

    def delete(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        *,
        emit_event: bool = True,
    ) -> dict[str, typing.Any]:
        state = self._owned_state(resource, entity_uuid)
        if state is None:
            data = self.provider_data_for_entity(resource, entity_uuid)
            if data is None or not self._entity_in_provider_scope(
                resource, entity_uuid
            ):
                return {
                    "type": resource,
                    "uuid": entity_uuid,
                    "status": "not_found",
                    "source_updated_at": None,
                    "updated_at": None,
                }
            state = self._set_state(
                resource,
                entity_uuid,
                canonical_hash(data),
                canonical_hash(data),
                datetime.datetime.now(datetime.timezone.utc),
            )
        with event_origin.use("provider", self.provider_uuid):
            event = self._capture_delete_event(resource, entity_uuid)
            getattr(self, f"_delete_{resource}")(entity_uuid)
            if resource == "message_reactions" and not emit_event:
                self._suppress_reaction_projection_events(entity_uuid)
            if resource == "stream_bindings":
                self._invalidate_stream_recipients(event["stream_uuid"])
            if emit_event:
                self._emit_delete(resource, entity_uuid, event)
        return {
            "type": resource,
            "uuid": entity_uuid,
            "status": "deleted",
            "source_updated_at": state["source_updated_at"],
            "updated_at": datetime.datetime.now(datetime.timezone.utc),
        }

    def get(self, resource: str, entity_uuid: sys_uuid.UUID) -> dict[str, typing.Any]:
        state = self._owned_state(resource, entity_uuid)
        if state is None:
            _error(404, "entity_not_found", "The provider entity does not exist")
        row = self._entity_row(resource, entity_uuid)
        if row is None:
            _error(404, "entity_not_found", "The provider entity does not exist")
        return self._entity_result(resource, row)

    def list(
        self,
        resource: str,
        updated_after: datetime.datetime,
        after_uuid: sys_uuid.UUID,
        limit: int,
    ) -> dict[str, typing.Any]:
        table = TABLES[resource]
        project_join = (
            "" if resource == "users" else "AND entity.project_id = state.project_id"
        )
        rows = self.session.execute(
            f"""
            SELECT entity.*, state.content_hash,
                   state.source_updated_at,
                   state.created_at AS provider_created_at,
                   state.updated_at AS provider_updated_at
            FROM workspace_v3.provider_entity_states AS state
            JOIN workspace_v3.{table} AS entity
              ON entity.uuid = state.entity_uuid {project_join}
            WHERE state.project_id = %s AND state.provider_uuid = %s
              AND state.entity_type = %s
              AND (state.updated_at, state.entity_uuid) > (%s, %s)
            ORDER BY state.updated_at, state.entity_uuid
            LIMIT %s
            """,
            (
                self.project_uuid,
                self.provider_uuid,
                RESOURCE_TYPES[resource],
                updated_after,
                after_uuid,
                limit + 1,
            ),
        ).fetchall()
        has_more = len(rows) > limit
        items = [self._entity_result(resource, dict(row)) for row in rows[:limit]]
        next_cursor = None
        if has_more:
            next_cursor = {
                "updated_after": items[-1]["updated_at"],
                "after_uuid": items[-1]["uuid"],
            }
        return {"items": items, "next_cursor": next_cursor}

    def bootstrap_page(
        self,
        resource: str,
        after_uuid: sys_uuid.UUID,
        limit: int,
    ) -> dict[str, typing.Any]:
        """Return a stable UUID-keyset page with hashes of the current rows."""
        table = TABLES[resource]
        scope = self._snapshot_scope(resource)
        rows = self.session.execute(
            f"""
            SELECT entity.*, state.content_hash AS provider_content_hash,
                   state.source_updated_at AS provider_source_updated_at
            FROM workspace_v3.{table} AS entity
            LEFT JOIN workspace_v3.provider_entity_states AS state
              ON state.project_id = %(project)s
             AND state.provider_uuid = %(provider)s
             AND state.entity_type = %(entity_type)s
             AND state.entity_uuid = entity.uuid
            WHERE {scope} AND entity.uuid > %(after_uuid)s
            ORDER BY entity.uuid
            LIMIT %(limit)s
            """,
            {
                "project": self.project_uuid,
                "provider": self.provider_uuid,
                "provider_name": self.provider_name,
                "entity_type": RESOURCE_TYPES[resource],
                "after_uuid": after_uuid,
                "limit": limit + 1,
            },
        ).fetchall()
        has_more = len(rows) > limit
        items = []
        for raw_row in rows[:limit]:
            row = dict(raw_row)
            data = self._provider_data(resource, row)
            content_hash = canonical_hash(data)
            stored_hash = row.get("provider_content_hash")
            source_updated_at = (
                row["provider_source_updated_at"]
                if stored_hash is not None
                and bytes(stored_hash) == content_hash
                and row["provider_source_updated_at"] is not None
                else row["updated_at"]
            )
            items.append(
                {
                    "type": resource,
                    "uuid": row["uuid"],
                    "content_hash": content_hash.hex(),
                    "source_updated_at": source_updated_at,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "data": data,
                }
            )
        next_cursor = None
        if has_more:
            next_cursor = {
                "snapshot_after_uuid": items[-1]["uuid"],
                # Keep the old alias until existing bridge clients have moved
                # to the directly reusable snapshot cursor.
                "after_uuid": items[-1]["uuid"],
            }
        return {"items": items, "next_cursor": next_cursor}

    def _ensure_identity_unchanged(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        data: dict[str, typing.Any],
    ) -> None:
        fields = IMMUTABLE_FIELDS.get(resource, ())
        if not fields:
            return
        row = self._entity_row(resource, entity_uuid)
        if row is None:
            _error(409, "entity_state_conflict", "Provider entity state is stale")
        for field in fields:
            if parse_uuid(data[field], field) != sys_uuid.UUID(str(row[field])):
                _error(
                    409,
                    "entity_identity_conflict",
                    f"{field} cannot change for an existing provider entity",
                )

    def _entity_row(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        table = TABLES[resource]
        project_join = (
            "" if resource == "users" else "AND entity.project_id = state.project_id"
        )
        row = self.session.execute(
            f"""
            SELECT entity.*, state.content_hash,
                   state.source_updated_at,
                   state.created_at AS provider_created_at,
                   state.updated_at AS provider_updated_at
            FROM workspace_v3.provider_entity_states AS state
            JOIN workspace_v3.{table} AS entity
              ON entity.uuid = state.entity_uuid {project_join}
            WHERE state.project_id = %s AND state.provider_uuid = %s
              AND state.entity_type = %s AND state.entity_uuid = %s
            """,
            (
                self.project_uuid,
                self.provider_uuid,
                RESOURCE_TYPES[resource],
                entity_uuid,
            ),
        ).fetchone()
        return None if row is None else dict(row)

    def provider_data_for_entity(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        """Return the canonical Provider payload without requiring prior ownership."""
        table = TABLES[resource]
        if resource == "users":
            query = f"SELECT * FROM workspace_v3.{table} WHERE uuid = %s"
            parameters: tuple[typing.Any, ...] = (entity_uuid,)
        else:
            query = (
                f"SELECT * FROM workspace_v3.{table} "
                "WHERE project_id = %s AND uuid = %s"
            )
            parameters = (self.project_uuid, entity_uuid)
        row = self.session.execute(query, parameters).fetchone()
        if row is None:
            return None
        return jsonable(self._provider_data(resource, dict(row)))

    def refresh_owned_state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        """Refresh the provider cursor after a Workspace-originated mutation."""
        data = self.provider_data_for_entity(resource, entity_uuid)
        state = self._owned_state(resource, entity_uuid)
        if data is None or state is None:
            return data
        content_hash = canonical_hash(data)
        if bytes(state["content_hash"]) != content_hash:
            self._set_state(
                resource,
                entity_uuid,
                content_hash,
                bytes(state["source_content_hash"]),
                state["source_updated_at"],
            )
        return data

    def _entity_result(
        self,
        resource: str,
        row: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        return {
            "type": resource,
            "uuid": row["uuid"],
            "content_hash": bytes(row["content_hash"]).hex(),
            "data": self._provider_data(resource, row),
            "source_updated_at": row["source_updated_at"],
            "created_at": row["provider_created_at"],
            "updated_at": row["provider_updated_at"],
        }

    def _provider_data(
        self,
        resource: str,
        row: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        if resource == "users":
            display_name = " ".join(
                item for item in (row["first_name"], row["last_name"]) if item
            )
            return {
                "username": row["username"],
                "display_name": display_name or row["username"],
                "email": row["email"],
                "avatar": row["avatar"],
                "status": row["status"],
                "last_ping_at": row["last_ping_at"],
                "status_emoji": row["status_emoji"],
                "status_text": row["status_text"],
                "disabled": row["disabled"],
                "is_bot": row["is_bot"],
                "created_at": row["created_at"],
            }
        fields = {
            "streams": (
                "name",
                "description",
                "owner_uuid",
                "invite_only",
                "announce",
                "direct_user_uuid",
                "private",
                "is_archived",
                "color",
                "default_topic_uuid",
                "history_public_to_subscribers",
                "created_at",
            ),
            "stream_bindings": (
                "stream_uuid",
                "user_uuid",
                "who_uuid",
                "role",
                "notification_mode",
                "created_at",
            ),
            "topics": (
                "stream_uuid",
                "name",
                "color",
                "is_done",
                "version",
                "created_at",
            ),
            "topic_bindings": (
                "stream_uuid",
                "topic_uuid",
                "user_uuid",
                "notification_mode",
                "created_at",
            ),
            "messages": (
                "stream_uuid",
                "topic_uuid",
                "author_uuid",
                "payload",
                "created_at",
            ),
            "message_flags": (
                "stream_uuid",
                "message_uuid",
                "user_uuid",
                "read",
                "pinned",
                "starred",
                "mentioned",
            ),
            "message_reactions": (
                "message_uuid",
                "user_uuid",
                "emoji_name",
                "created_at",
            ),
        }[resource]
        return {field: row[field] for field in fields}

    def _result(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        status: str,
        state: typing.Mapping[str, typing.Any],
    ) -> dict[str, typing.Any]:
        return {
            "type": resource,
            "uuid": entity_uuid,
            "status": status,
            "source_updated_at": state["source_updated_at"],
            "updated_at": state["updated_at"],
        }

    def bootstrap_snapshot(
        self, cursor: typing.Mapping[str, typing.Any]
    ) -> typing.IO[bytes]:
        snapshot_uuid = sys_uuid.uuid4()
        output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        meta = {
            "record": "meta",
            "schema_version": 1,
            "snapshot_uuid": snapshot_uuid,
            "project_id": self.project_uuid,
            "provider_uuid": self.provider_uuid,
            "epoch_generation": cursor["epoch_generation"],
            "snapshot_epoch_version": int(cursor["current_epoch_version"]),
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }
        output.write(self._snapshot_line(meta))
        counts: dict[str, int] = {}
        digest = hashlib.sha256()
        for resource in RESOURCE_TYPES:
            count = 0
            for row in self._snapshot_rows(resource):
                data = self._provider_data(resource, row)
                content_hash = canonical_hash(data)
                state_hash = row.get("provider_content_hash")
                source_updated_at = (
                    row["provider_source_updated_at"]
                    if state_hash is not None
                    and bytes(state_hash) == content_hash
                    and row["provider_source_updated_at"] is not None
                    else row["updated_at"]
                )
                item = {
                    "record": "entity",
                    "type": resource,
                    "uuid": row["uuid"],
                    "content_hash": content_hash.hex(),
                    "source_updated_at": source_updated_at,
                    "data": data,
                }
                line = self._snapshot_line(item)
                output.write(line)
                digest.update(line)
                count += 1
            counts[resource] = count
        output.write(
            self._snapshot_line(
                {
                    "record": "complete",
                    "snapshot_uuid": snapshot_uuid,
                    "counts": counts,
                    "sha256": digest.hexdigest(),
                }
            )
        )
        output.seek(0)
        return output

    @staticmethod
    def _snapshot_line(value: dict[str, typing.Any]) -> bytes:
        return (
            json.dumps(
                jsonable(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _snapshot_scope(resource: str) -> str:
        return {
            "users": """
                entity.uuid IN (
                    WITH provider_streams AS (
                        SELECT stream.uuid, stream.owner_uuid,
                               stream.direct_user_uuid
                        FROM workspace_v3.streams AS stream
                        WHERE stream.project_id = %(project)s
                          AND stream.source_name = %(provider_name)s
                    ),
                    provider_messages AS (
                        SELECT message.uuid, message.author_uuid
                        FROM workspace_v3.messages AS message
                        JOIN provider_streams AS stream
                          ON stream.uuid = message.stream_uuid
                        WHERE message.project_id = %(project)s
                    )
                    SELECT provider_state.entity_uuid
                    FROM workspace_v3.provider_entity_states AS provider_state
                    WHERE provider_state.project_id = %(project)s
                      AND provider_state.provider_uuid = %(provider)s
                      AND provider_state.entity_type = 'user'
                    UNION SELECT stream.owner_uuid
                    FROM provider_streams AS stream
                    UNION SELECT stream.direct_user_uuid
                    FROM provider_streams AS stream
                    WHERE stream.direct_user_uuid IS NOT NULL
                    UNION SELECT binding.user_uuid
                    FROM workspace_v3.stream_bindings AS binding
                    JOIN provider_streams AS stream
                      ON stream.uuid = binding.stream_uuid
                    WHERE binding.project_id = %(project)s
                    UNION SELECT binding.who_uuid
                    FROM workspace_v3.stream_bindings AS binding
                    JOIN provider_streams AS stream
                      ON stream.uuid = binding.stream_uuid
                    WHERE binding.project_id = %(project)s
                    UNION SELECT binding.user_uuid
                    FROM workspace_v3.topic_bindings AS binding
                    JOIN provider_streams AS stream
                      ON stream.uuid = binding.stream_uuid
                    WHERE binding.project_id = %(project)s
                    UNION SELECT message.author_uuid
                    FROM provider_messages AS message
                    UNION SELECT flag.user_uuid
                    FROM workspace_v3.message_flags AS flag
                    JOIN provider_messages AS message
                      ON message.uuid = flag.message_uuid
                    WHERE flag.project_id = %(project)s
                    UNION SELECT reaction.user_uuid
                    FROM workspace_v3.message_reactions AS reaction
                    JOIN provider_messages AS message
                      ON message.uuid = reaction.message_uuid
                    WHERE reaction.project_id = %(project)s
                )
            """,
            "streams": """
                entity.project_id = %(project)s
                AND entity.source_name = %(provider_name)s
            """,
            "stream_bindings": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.streams AS stream
                    WHERE stream.project_id = entity.project_id
                      AND stream.uuid = entity.stream_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
            "topics": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.streams AS stream
                    WHERE stream.project_id = entity.project_id
                      AND stream.uuid = entity.stream_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
            "topic_bindings": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.streams AS stream
                    WHERE stream.project_id = entity.project_id
                      AND stream.uuid = entity.stream_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
            "messages": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.streams AS stream
                    WHERE stream.project_id = entity.project_id
                      AND stream.uuid = entity.stream_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
            "message_flags": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.messages AS message
                    JOIN workspace_v3.streams AS stream
                      ON stream.project_id = message.project_id
                     AND stream.uuid = message.stream_uuid
                    WHERE message.project_id = entity.project_id
                      AND message.uuid = entity.message_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
            "message_reactions": """
                entity.project_id = %(project)s AND EXISTS (
                    SELECT 1 FROM workspace_v3.messages AS message
                    JOIN workspace_v3.streams AS stream
                      ON stream.project_id = message.project_id
                     AND stream.uuid = message.stream_uuid
                    WHERE message.project_id = entity.project_id
                      AND message.uuid = entity.message_uuid
                      AND stream.source_name = %(provider_name)s
                )
            """,
        }[resource]

    def _snapshot_rows(self, resource: str) -> typing.Iterable[dict[str, typing.Any]]:
        table = TABLES[resource]
        scope = self._snapshot_scope(resource)
        rows = self.session.execute(
            f"""
            SELECT entity.*,
                   state.content_hash AS provider_content_hash,
                   state.source_updated_at AS provider_source_updated_at
            FROM workspace_v3.{table} AS entity
            LEFT JOIN workspace_v3.provider_entity_states AS state
              ON state.project_id = %(project)s
             AND state.provider_uuid = %(provider)s
             AND state.entity_type = %(entity_type)s
             AND state.entity_uuid = entity.uuid
            WHERE {scope}
            ORDER BY entity.uuid
            """,
            {
                "project": self.project_uuid,
                "provider": self.provider_uuid,
                "provider_name": self.provider_name,
                "entity_type": RESOURCE_TYPES[resource],
            },
        )
        return (dict(row) for row in rows)

    def _created_at(self, data: dict[str, typing.Any]) -> datetime.datetime:
        value = data.get("created_at")
        if value is None:
            return datetime.datetime.now(datetime.timezone.utc)
        return parse_timestamp(value, "created_at")

    def _upsert_users(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        display_name = str(data["display_name"]).strip()
        if not display_name:
            _error(422, "invalid_display_name", "display_name cannot be empty")
        email = data.get("email") or None
        avatar = data.get("avatar")
        if not avatar:
            avatar = (
                "urn:gravatar:"
                + hashlib.md5(
                    str(email or entity_uuid).strip().lower().encode(),
                    usedforsecurity=False,
                ).hexdigest()
            )
        created_at = self._created_at(data)
        last_ping_at = (
            created_at
            if data.get("last_ping_at") is None
            else parse_timestamp(data["last_ping_at"], "last_ping_at")
        )
        self.session.execute(
            """
            INSERT INTO workspace_v3.users (
                uuid, created_at, updated_at, username, source, status,
                first_name, last_name, email, last_ping_at,
                status_emoji, status_text, avatar, disabled, is_bot
            ) VALUES (
                %s, %s, clock_timestamp(), %s, %s, %s,
                %s, NULL, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (uuid) DO UPDATE SET
                username = EXCLUDED.username,
                status = EXCLUDED.status,
                first_name = EXCLUDED.first_name,
                last_name = NULL,
                email = EXCLUDED.email,
                last_ping_at = EXCLUDED.last_ping_at,
                status_emoji = EXCLUDED.status_emoji,
                status_text = EXCLUDED.status_text,
                avatar = EXCLUDED.avatar,
                disabled = EXCLUDED.disabled,
                is_bot = EXCLUDED.is_bot,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                created_at,
                data["username"],
                self.provider_name,
                data.get("status", "offline"),
                display_name,
                email,
                last_ping_at,
                data.get("status_emoji"),
                data.get("status_text"),
                avatar,
                bool(data.get("disabled", False)),
                bool(data.get("is_bot", False)),
            ),
        )

    def _upsert_streams(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        owner_uuid = parse_uuid(data["owner_uuid"], "owner_uuid")
        direct_user_uuid = (
            None
            if data.get("direct_user_uuid") is None
            else parse_uuid(data["direct_user_uuid"], "direct_user_uuid")
        )
        private = bool(data.get("private", direct_user_uuid is not None))
        private_index = None
        if direct_user_uuid is not None:
            private_index = ":".join(sorted((str(owner_uuid), str(direct_user_uuid))))
        self.session.execute(
            """
            INSERT INTO workspace_v3.streams (
                uuid, project_id, name, description, owner_uuid, source_name,
                invite_only, announce, direct_user_uuid, private,
                is_archived, private_index, color, default_topic_uuid,
                history_public_to_subscribers, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, clock_timestamp()
            )
            ON CONFLICT (uuid) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                owner_uuid = EXCLUDED.owner_uuid,
                invite_only = EXCLUDED.invite_only,
                announce = EXCLUDED.announce,
                direct_user_uuid = EXCLUDED.direct_user_uuid,
                private = EXCLUDED.private,
                is_archived = EXCLUDED.is_archived,
                private_index = EXCLUDED.private_index,
                color = EXCLUDED.color,
                default_topic_uuid = EXCLUDED.default_topic_uuid,
                history_public_to_subscribers =
                    EXCLUDED.history_public_to_subscribers,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["name"],
                data.get("description"),
                owner_uuid,
                self.provider_name,
                bool(data.get("invite_only", False)),
                bool(data.get("announce", False)),
                direct_user_uuid,
                private,
                bool(data.get("is_archived", False)),
                private_index,
                int(data.get("color", 0)),
                data.get("default_topic_uuid"),
                bool(data.get("history_public_to_subscribers", True)),
                self._created_at(data),
            ),
        )

    def _upsert_stream_bindings(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
        updated = self.session.execute(
            """
            UPDATE workspace_v3.stream_bindings
            SET stream_uuid = %s,
                user_uuid = %s,
                who_uuid = %s,
                role = %s,
                notification_mode = %s,
                notification_updated_at = clock_timestamp(),
                updated_at = clock_timestamp()
            WHERE project_id = %s AND uuid = %s
            RETURNING uuid
            """,
            (
                data["stream_uuid"],
                user_uuid,
                data.get("who_uuid", user_uuid),
                data.get("role", "member"),
                data.get("notification_mode", "all_messages"),
                self.project_uuid,
                entity_uuid,
            ),
        ).fetchone()
        if updated is None:
            self.session.execute(
                """
                INSERT INTO workspace_v3.stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid,
                    role, notification_mode, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                """,
                (
                    entity_uuid,
                    self.project_uuid,
                    data["stream_uuid"],
                    user_uuid,
                    data.get("who_uuid", user_uuid),
                    data.get("role", "member"),
                    data.get("notification_mode", "all_messages"),
                    self._created_at(data),
                ),
            )

    def _upsert_topics(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        self.session.execute(
            """
            INSERT INTO workspace_v3.topics (
                uuid, project_id, stream_uuid, name, color, source_name,
                is_done, version, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (uuid) DO UPDATE SET
                stream_uuid = EXCLUDED.stream_uuid,
                name = EXCLUDED.name,
                color = EXCLUDED.color,
                is_done = EXCLUDED.is_done,
                version = EXCLUDED.version,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["stream_uuid"],
                data["name"],
                int(data.get("color", 0)),
                self.provider_name,
                bool(data.get("is_done", False)),
                int(data.get("version", 0)),
                self._created_at(data),
            ),
        )

    def _upsert_topic_bindings(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        self.session.execute(
            """
            INSERT INTO workspace_v3.topic_bindings (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                notification_mode, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (uuid) DO UPDATE SET
                stream_uuid = EXCLUDED.stream_uuid,
                topic_uuid = EXCLUDED.topic_uuid,
                user_uuid = EXCLUDED.user_uuid,
                notification_mode = EXCLUDED.notification_mode,
                notification_updated_at = clock_timestamp(),
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["stream_uuid"],
                data["topic_uuid"],
                data["user_uuid"],
                data.get("notification_mode", "default"),
                self._created_at(data),
            ),
        )

    def _upsert_messages(
        self,
        entity_uuid: sys_uuid.UUID,
        data: dict[str, typing.Any],
        *,
        expand_flags: bool,
    ) -> None:
        self.session.execute(
            """
            INSERT INTO workspace_v3.messages (
                uuid, project_id, stream_uuid, topic_uuid, author_uuid,
                payload, source_name, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, clock_timestamp())
            ON CONFLICT (uuid) DO UPDATE SET
                stream_uuid = EXCLUDED.stream_uuid,
                topic_uuid = EXCLUDED.topic_uuid,
                author_uuid = EXCLUDED.author_uuid,
                payload = EXCLUDED.payload,
                created_at = CASE
                    WHEN %s THEN EXCLUDED.created_at
                    ELSE messages.created_at
                END,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["stream_uuid"],
                data["topic_uuid"],
                data["author_uuid"],
                json.dumps(data["payload"]),
                self.provider_name,
                self._created_at(data),
                "created_at" in data,
            ),
        )
        if expand_flags:
            self.expand_message_flags((entity_uuid,))

    def expand_message_flags(
        self, message_uuids: typing.Iterable[sys_uuid.UUID]
    ) -> None:
        ordered = sorted(set(message_uuids), key=str)
        if not ordered:
            return
        self.session.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                project_id, stream_uuid, message_uuid, user_uuid,
                read, mentioned
            )
            SELECT message.project_id, message.stream_uuid, message.uuid,
                   binding.user_uuid,
                   binding.user_uuid = message.author_uuid,
                   position(
                       'urn:user:' || lower(binding.user_uuid::text)
                       IN lower(message.payload ->> 'content')
                   ) > 0
            FROM workspace_v3.messages AS message
            JOIN workspace_v3.stream_bindings AS binding
              ON binding.project_id = message.project_id
             AND binding.stream_uuid = message.stream_uuid
            JOIN workspace_v3.streams AS stream
              ON stream.project_id = message.project_id
             AND stream.uuid = message.stream_uuid
            WHERE message.project_id = %s
              AND message.uuid = ANY(%s::uuid[])
              AND (stream.history_public_to_subscribers
                   OR message.created_at >= binding.created_at)
            ON CONFLICT (project_id, message_uuid, user_uuid) DO NOTHING
            """,
            (self.project_uuid, ordered),
        )

    def _upsert_message_flags(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        self.session.execute(
            """
            INSERT INTO workspace_v3.message_flags (
                uuid, project_id, stream_uuid, message_uuid, user_uuid,
                read, pinned, starred, mentioned
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, message_uuid, user_uuid) DO UPDATE SET
                uuid = EXCLUDED.uuid,
                stream_uuid = EXCLUDED.stream_uuid,
                read = EXCLUDED.read,
                pinned = EXCLUDED.pinned,
                starred = EXCLUDED.starred,
                mentioned = EXCLUDED.mentioned,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["stream_uuid"],
                data["message_uuid"],
                data["user_uuid"],
                bool(data.get("read", False)),
                bool(data.get("pinned", False)),
                bool(data.get("starred", False)),
                bool(data.get("mentioned", False)),
            ),
        )

    def _upsert_message_reactions(
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
    ) -> None:
        self.session.execute(
            """
            INSERT INTO workspace_v3.message_reactions (
                uuid, project_id, message_uuid, user_uuid,
                emoji_name, source_name, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (uuid) DO UPDATE SET
                message_uuid = EXCLUDED.message_uuid,
                user_uuid = EXCLUDED.user_uuid,
                emoji_name = EXCLUDED.emoji_name,
                updated_at = clock_timestamp()
            """,
            (
                entity_uuid,
                self.project_uuid,
                data["message_uuid"],
                data["user_uuid"],
                data["emoji_name"],
                self.provider_name,
                self._created_at(data),
            ),
        )

    def _suppress_reaction_projection_events(
        self,
        reaction_uuid: sys_uuid.UUID,
    ) -> None:
        self.session.execute(
            """
            UPDATE workspace_v3.projection_tasks AS task
            SET payload = task.payload || '{"emit_events": false}'::jsonb,
                updated_at = clock_timestamp()
            WHERE task.project_id = %s
              AND task.task_type = 'reaction_snapshot'
              AND task.status = 'pending'
              AND EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements(
                      COALESCE(task.payload -> 'operations', '[]'::jsonb)
                  ) AS operation
                  WHERE operation ->> 'reaction_uuid' = %s
              )
            """,
            (self.project_uuid, str(reaction_uuid)),
        )

    def _delete_users(self, entity_uuid: sys_uuid.UUID) -> None:
        local_references = self.session.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM workspace_v3.messages
                    WHERE project_id = %(project)s AND author_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.stream_bindings
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.stream_bindings
                    WHERE project_id = %(project)s AND who_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.streams
                    WHERE project_id = %(project)s
                      AND (owner_uuid = %(user)s OR direct_user_uuid = %(user)s)
                UNION ALL
                SELECT 1 FROM workspace_v3.topic_bindings
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.message_flags
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.message_reactions
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.drafts
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.files
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
                UNION ALL
                SELECT 1 FROM workspace_v3.folders
                    WHERE project_id = %(project)s AND user_uuid = %(user)s
            ) AS referenced
            """,
            {"project": self.project_uuid, "user": entity_uuid},
        ).fetchone()
        if local_references["referenced"]:
            _error(
                409,
                "provider_user_is_referenced",
                "Disable a referenced provider user instead of deleting it",
            )
        shared = self.session.execute(
            """
            SELECT 1
            FROM workspace_v3.provider_entity_states
            WHERE entity_type = 'user' AND entity_uuid = %s
              AND (project_id, provider_uuid) <> (%s, %s)
            LIMIT 1
            """,
            (entity_uuid, self.project_uuid, self.provider_uuid),
        ).fetchone()
        if shared is not None:
            self.session.execute(
                """
                DELETE FROM workspace_v3.provider_entity_states
                WHERE project_id = %s AND provider_uuid = %s
                  AND entity_type = 'user' AND entity_uuid = %s
                """,
                (self.project_uuid, self.provider_uuid, entity_uuid),
            )
            return
        references = self.session.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM workspace_v3.messages WHERE author_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.stream_bindings WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.stream_bindings WHERE who_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.streams
                    WHERE owner_uuid = %s OR direct_user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.topic_bindings WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.message_flags WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.message_reactions WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.drafts WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.files WHERE user_uuid = %s
                UNION ALL
                SELECT 1 FROM workspace_v3.folders WHERE user_uuid = %s
            ) AS referenced
            """,
            (entity_uuid,) * 11,
        ).fetchone()
        if references["referenced"]:
            _error(
                409,
                "provider_user_is_referenced",
                "Disable a referenced provider user instead of deleting it",
            )
        self.session.execute(
            "DELETE FROM workspace_v3.users WHERE uuid = %s", (entity_uuid,)
        )

    def _delete_entity(self, resource: str, entity_uuid: sys_uuid.UUID) -> None:
        self.session.execute(
            f"DELETE FROM workspace_v3.{TABLES[resource]} "
            "WHERE project_id = %s AND uuid = %s",
            (self.project_uuid, entity_uuid),
        )

    def _delete_streams(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("streams", entity_uuid)

    def _delete_stream_bindings(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("stream_bindings", entity_uuid)

    def _delete_topics(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("topics", entity_uuid)

    def _delete_topic_bindings(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("topic_bindings", entity_uuid)

    def _delete_messages(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("messages", entity_uuid)

    def _delete_message_flags(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("message_flags", entity_uuid)

    def _delete_message_reactions(self, entity_uuid: sys_uuid.UUID) -> None:
        self._delete_entity("message_reactions", entity_uuid)

    def _user_recipients(self, user_uuid: sys_uuid.UUID) -> tuple[sys_uuid.UUID, ...]:
        rows = self.session.execute(
            """
            SELECT DISTINCT peer.user_uuid
            FROM workspace_v3.stream_bindings AS target
            JOIN workspace_v3.stream_bindings AS peer
              ON peer.project_id = target.project_id
             AND peer.stream_uuid = target.stream_uuid
            WHERE target.project_id = %s AND target.user_uuid = %s
            ORDER BY peer.user_uuid
            """,
            (self.project_uuid, user_uuid),
        ).fetchall()
        return tuple(sys_uuid.UUID(str(row["user_uuid"])) for row in rows)

    def _emit_upsert(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        status: str,
        data: dict[str, typing.Any],
        *,
        previous_user_uuid: sys_uuid.UUID | None = None,
    ) -> None:
        action = "created" if status == "created" else "updated"
        if resource == "users":
            recipients = self._user_recipients(entity_uuid)
            if recipients:
                self.events._emit_resource("users", entity_uuid, action, recipients)
            return
        if resource == "streams":
            recipients = self.events._stream_recipients(entity_uuid)
            if recipients:
                self.events._emit_resource("streams", entity_uuid, action, recipients)
            return
        if resource == "stream_bindings":
            stream_uuid = parse_uuid(data["stream_uuid"], "stream_uuid")
            self._invalidate_stream_recipients(stream_uuid)
            recipients = self._stream_recipients(stream_uuid)
            payloads = self.events._event_resource_payloads(
                "stream_bindings", entity_uuid, recipients
            )
            if status == "created":
                self.events._emit(
                    kind="stream_bindings.created",
                    object_type="stream_binding",
                    action="created",
                    entity_uuid=stream_uuid,
                    payloads={
                        recipient: {
                            "uuid": str(stream_uuid),
                            "items": [payloads[recipient]],
                        }
                        for recipient in payloads
                    },
                    provider_consumers=self._stream_providers(stream_uuid),
                )
                user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
                self.events._emit_resource(
                    "streams", stream_uuid, "created", (user_uuid,)
                )
            else:
                self.events._emit(
                    kind="stream_binding.updated",
                    object_type="stream_binding",
                    action="updated",
                    entity_uuid=entity_uuid,
                    payloads=payloads,
                    provider_consumers=self._stream_providers(stream_uuid),
                )
                user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
                self.events._emit_resource(
                    "streams", stream_uuid, "created", (user_uuid,)
                )
                if previous_user_uuid is not None:
                    self.events._emit(
                        kind="stream.deleted",
                        object_type="stream",
                        action="deleted",
                        entity_uuid=stream_uuid,
                        payloads={previous_user_uuid: {"uuid": str(stream_uuid)}},
                    )
            return
        if resource == "topics":
            stream_uuid = parse_uuid(data["stream_uuid"], "stream_uuid")
            recipients = self._stream_recipients(stream_uuid)
            if recipients:
                self.events._emit_resource(
                    "stream_topics", entity_uuid, action, recipients
                )
            return
        if resource == "topic_bindings":
            topic_uuid = parse_uuid(data["topic_uuid"], "topic_uuid")
            user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
            self.events._emit_resource(
                "stream_topics",
                topic_uuid,
                "created" if status == "created" else "updated",
                (user_uuid,),
            )
            return
        if resource == "messages":
            stream_uuid = parse_uuid(data["stream_uuid"], "stream_uuid")
            recipients = self._stream_recipients(stream_uuid)
            if recipients:
                self.events._emit(
                    kind=f"message.{action}",
                    object_type="message",
                    action=action,
                    entity_uuid=entity_uuid,
                    payloads=self.events._event_resource_payloads(
                        "messages", entity_uuid, recipients
                    ),
                    provider_consumers=self._stream_providers(stream_uuid),
                )
            return
        if resource == "message_flags":
            message_uuid = parse_uuid(data["message_uuid"], "message_uuid")
            user_uuid = parse_uuid(data["user_uuid"], "user_uuid")
            self.events._emit_resource(
                "messages", message_uuid, "updated", (user_uuid,)
            )
            return
        if resource == "message_reactions":
            # The database trigger has queued a reaction projection. That
            # projection rebuilds the aggregate and emits one complete event.
            return

    def _capture_delete_event(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        row = self._entity_row(resource, entity_uuid)
        if row is None:
            _error(404, "entity_not_found", "The provider entity does not exist")
        data = self._provider_data(resource, row)
        result: dict[str, typing.Any] = {"data": data, "payloads": {}}
        if resource == "users":
            result["recipients"] = self._user_recipients(entity_uuid)
        elif resource == "streams":
            result["recipients"] = self._stream_recipients(entity_uuid)
        elif resource == "stream_bindings":
            result["recipients"] = self._stream_recipients(row["stream_uuid"])
            result["stream_uuid"] = row["stream_uuid"]
            result["user_uuid"] = row["user_uuid"]
        elif resource == "topics":
            result["recipients"] = self._stream_recipients(row["stream_uuid"])
            result["stream_uuid"] = row["stream_uuid"]
            result["was_default"] = self.session.execute(
                """
                SELECT default_topic_uuid = %s AS was_default
                FROM workspace_v3.streams
                WHERE project_id = %s AND uuid = %s
                """,
                (entity_uuid, self.project_uuid, row["stream_uuid"]),
            ).fetchone()["was_default"]
        elif resource == "topic_bindings":
            result["recipients"] = (sys_uuid.UUID(str(row["user_uuid"])),)
            result["stream_uuid"] = row["stream_uuid"]
            result["topic_uuid"] = row["topic_uuid"]
        elif resource == "messages":
            result["recipients"] = self._stream_recipients(row["stream_uuid"])
            result["stream_uuid"] = row["stream_uuid"]
            result["topic_uuid"] = row["topic_uuid"]
            result["author_uuid"] = row["author_uuid"]
        elif resource == "message_flags":
            result["recipients"] = (sys_uuid.UUID(str(row["user_uuid"])),)
            result["stream_uuid"] = row["stream_uuid"]
            result["message_uuid"] = row["message_uuid"]
        else:
            message = self.session.execute(
                """
                SELECT stream_uuid FROM workspace_v3.messages
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, row["message_uuid"]),
            ).fetchone()
            result["recipients"] = self._stream_recipients(message["stream_uuid"])
            result["stream_uuid"] = message["stream_uuid"]
            result["message_uuid"] = row["message_uuid"]
        return result

    def _emit_delete(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        event: dict[str, typing.Any],
    ) -> None:
        recipients = event["recipients"]
        if not recipients and resource != "message_reactions":
            return
        payload: dict[str, typing.Any] = {"uuid": str(entity_uuid)}
        object_type = RESOURCE_TYPES[resource]
        kind = f"{object_type}.deleted"
        event_entity_uuid = entity_uuid
        if resource == "stream_bindings":
            payload.update(
                {
                    "stream_uuid": event["stream_uuid"],
                    "user_uuid": event["user_uuid"],
                }
            )
        elif resource == "topics":
            payload["stream_uuid"] = event["stream_uuid"]
        elif resource == "topic_bindings":
            kind = "topic.deleted"
            object_type = "topic"
            event_entity_uuid = event["topic_uuid"]
            payload = {
                "uuid": str(event["topic_uuid"]),
                "stream_uuid": event["stream_uuid"],
            }
        elif resource == "messages":
            payload.update(
                {
                    "stream_uuid": event["stream_uuid"],
                    "topic_uuid": event["topic_uuid"],
                    "author_uuid": event["author_uuid"],
                    "source_name": self.provider_name,
                    "source": {"kind": self.provider_name},
                }
            )
        elif resource == "message_flags":
            kind = "message.deleted"
            object_type = "message"
            event_entity_uuid = event["message_uuid"]
            payload = {
                "uuid": str(event["message_uuid"]),
                "stream_uuid": event["stream_uuid"],
            }
        elif resource == "message_reactions":
            # The projection emits the complete user-facing deletion after it
            # rebuilds the message reaction aggregate.
            recipients = ()
        provider_consumers: tuple[sys_uuid.UUID, ...] = ()
        if "stream_uuid" in event:
            provider_consumers = self._stream_providers(event["stream_uuid"])
        self.events._emit(
            kind=kind,
            object_type=object_type,
            action="deleted",
            entity_uuid=event_entity_uuid,
            payloads={recipient: payload for recipient in recipients},
            provider_consumers=provider_consumers,
            provider_payload=jsonable(event["data"]),
        )
        if resource == "stream_bindings":
            self.events._emit(
                kind="stream.deleted",
                object_type="stream",
                action="deleted",
                entity_uuid=event["stream_uuid"],
                payloads={event["user_uuid"]: {"uuid": str(event["stream_uuid"])}},
            )
        elif resource == "topics" and event["was_default"]:
            self.events._emit_resource(
                "streams",
                event["stream_uuid"],
                "updated",
                recipients,
            )


def translate_database_error(error: Exception) -> typing.NoReturn:
    code = getattr(error, "sqlstate", None)
    if code == "23505":
        _error(
            409,
            "unique_constraint_conflict",
            "Provider entity conflicts with existing data",
        )
    if code in {"23503", "23514", "23502", "22P02"}:
        _error(422, "invalid_entity", "Provider entity violates the canonical schema")
    if isinstance(
        error, (KeyError, TypeError, ValueError, ra_exceptions.ValidationErrorException)
    ):
        _error(422, "invalid_entity", "Provider entity payload is invalid")
    raise error
