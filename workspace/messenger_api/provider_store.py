# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Provider-owned CRUD over the clean Workspace v3 Messenger schema."""

import datetime
import hashlib
import json
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
ZERO_UUID = sys_uuid.UUID(int=0)
IMMUTABLE_FIELDS = {
    "stream_bindings": ("stream_uuid", "user_uuid"),
    "topic_bindings": ("stream_uuid", "topic_uuid", "user_uuid"),
    "messages": ("author_uuid",),
    "message_flags": ("stream_uuid", "message_uuid", "user_uuid"),
    "message_reactions": ("message_uuid", "user_uuid"),
}


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
        if self.provider_name != "zulip":
            _error(
                403,
                "unsupported_provider",
                "This Provider API version supports only the zulip source",
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
            self._stream_recipients_cache[stream_uuid] = (
                self.events._stream_recipients(stream_uuid)
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
                f"{self.project_uuid}:{RESOURCE_TYPES[resource]}:{entity_uuid}"
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

    def _state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any] | None:
        row = self.session.execute(
            """
            SELECT provider_uuid, content_hash, created_at, updated_at
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
    ) -> None:
        if state is not None or not self._entity_exists(resource, entity_uuid):
            return
        if resource == "users":
            row = self.session.execute(
                "SELECT source FROM workspace_v3.users WHERE uuid = %s",
                (entity_uuid,),
            ).fetchone()
            if row["source"] == self.provider_name:
                return
        _error(
            409,
            "entity_not_provider_owned",
            "The entity UUID already belongs to native or unmanaged data",
        )

    def _set_state(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        content_hash: bytes,
    ) -> dict[str, typing.Any]:
        row = self.session.execute(
            """
            INSERT INTO workspace_v3.provider_entity_states (
                project_id, provider_uuid, entity_type, entity_uuid, content_hash
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_id, provider_uuid, entity_type, entity_uuid)
            DO UPDATE SET content_hash = EXCLUDED.content_hash,
                          updated_at = clock_timestamp()
            RETURNING created_at, updated_at
            """,
            (
                self.project_uuid,
                self.provider_uuid,
                RESOURCE_TYPES[resource],
                entity_uuid,
                content_hash,
            ),
        ).fetchone()
        return dict(row)

    def upsert(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
        content_hash: bytes,
        data: dict[str, typing.Any],
    ) -> dict[str, typing.Any]:
        state = self._owned_state(resource, entity_uuid)
        if state is not None and bytes(state["content_hash"]) == content_hash:
            return self._result(resource, entity_uuid, "unchanged", state)
        self._ensure_create_available(resource, entity_uuid, state)
        if state is not None:
            self._ensure_identity_unchanged(resource, entity_uuid, data)
        status = "created" if state is None else "updated"
        with event_origin.use("provider", self.provider_uuid):
            getattr(self, f"_upsert_{resource}")(entity_uuid, data)
            new_state = self._set_state(resource, entity_uuid, content_hash)
            self._emit_upsert(resource, entity_uuid, status, data)
        return self._result(resource, entity_uuid, status, new_state)

    def delete(
        self,
        resource: str,
        entity_uuid: sys_uuid.UUID,
    ) -> dict[str, typing.Any]:
        state = self._owned_state(resource, entity_uuid)
        if state is None:
            return {
                "type": resource,
                "uuid": entity_uuid,
                "status": "not_found",
                "updated_at": None,
            }
        with event_origin.use("provider", self.provider_uuid):
            event = self._capture_delete_event(resource, entity_uuid)
            getattr(self, f"_delete_{resource}")(entity_uuid)
            if resource == "stream_bindings":
                self._invalidate_stream_recipients(event["stream_uuid"])
            self._emit_delete(resource, entity_uuid, event)
        return {
            "type": resource,
            "uuid": entity_uuid,
            "status": "deleted",
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
            "updated_at": state["updated_at"],
        }

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
        self.session.execute(
            """
            INSERT INTO workspace_v3.stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid,
                role, notification_mode, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
            ON CONFLICT (uuid) DO UPDATE SET
                role = EXCLUDED.role,
                notification_mode = EXCLUDED.notification_mode,
                notification_updated_at = clock_timestamp(),
                updated_at = clock_timestamp()
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
        self, entity_uuid: sys_uuid.UUID, data: dict[str, typing.Any]
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
                created_at = EXCLUDED.created_at,
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
            ),
        )
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
            WHERE message.project_id = %s AND message.uuid = %s
            ON CONFLICT (project_id, message_uuid, user_uuid) DO NOTHING
            """,
            (self.project_uuid, entity_uuid),
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

    def _delete_users(self, entity_uuid: sys_uuid.UUID) -> None:
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
            message_uuid = parse_uuid(data["message_uuid"], "message_uuid")
            row = self.session.execute(
                """
                SELECT stream_uuid FROM workspace_v3.messages
                WHERE project_id = %s AND uuid = %s
                """,
                (self.project_uuid, message_uuid),
            ).fetchone()
            recipients = self._stream_recipients(row["stream_uuid"])
            self.events._emit_resource(
                "message_reactions", entity_uuid, action, recipients
            )

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
        if not recipients:
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
