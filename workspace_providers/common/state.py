import contextlib
import datetime
import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from workspace_providers.common import reconciliation


COMMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_runtime (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    provider_uuid UUID NOT NULL UNIQUE,
    backend_url TEXT NOT NULL,
    provider_kind VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS account_states (
    account_uuid UUID PRIMARY KEY,
    provider_kind VARCHAR(32) NOT NULL,
    config_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'new',
    last_error TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS entity_maps (
    account_uuid UUID NOT NULL,
    entity_kind VARCHAR(64) NOT NULL,
    external_key TEXT NOT NULL,
    workspace_urn TEXT NOT NULL,
    content_hash VARCHAR(64),
    provider_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_uuid, entity_kind, external_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS entity_maps_workspace_urn_idx
    ON entity_maps (account_uuid, entity_kind, workspace_urn);
CREATE TABLE IF NOT EXISTS command_dedupe (
    command_uuid UUID PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    result JSONB,
    last_error TEXT,
    next_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE command_dedupe
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS reconciliation_partitions (
    account_uuid UUID NOT NULL,
    entity_kind VARCHAR(64) NOT NULL,
    partition_key TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    estimated_cost DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    mismatch_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    clean_streak INTEGER NOT NULL DEFAULT 0,
    last_verified_at TIMESTAMPTZ,
    next_due_at TIMESTAMPTZ,
    cursor TEXT,
    PRIMARY KEY (account_uuid, entity_kind, partition_key)
);
CREATE INDEX IF NOT EXISTS reconciliation_partitions_due_idx
    ON reconciliation_partitions (next_due_at);
"""

MAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_folder_states (
    account_uuid UUID NOT NULL,
    path TEXT NOT NULL,
    delimiter VARCHAR(8) NOT NULL DEFAULT '/',
    special_use VARCHAR(64),
    uid_validity BIGINT,
    uid_next BIGINT,
    highest_modseq BIGINT,
    workspace_urn TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_uuid, path)
);
CREATE TABLE IF NOT EXISTS mail_message_states (
    account_uuid UUID NOT NULL,
    folder_path TEXT NOT NULL,
    uid BIGINT NOT NULL,
    message_id TEXT,
    workspace_urn TEXT NOT NULL,
    flags_hash VARCHAR(64),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_uuid, folder_path, uid)
);
"""

CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_collection_states (
    account_uuid UUID NOT NULL,
    href TEXT NOT NULL,
    ctag TEXT,
    sync_token TEXT,
    workspace_urn TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_uuid, href)
);
CREATE TABLE IF NOT EXISTS calendar_object_states (
    account_uuid UUID NOT NULL,
    calendar_href TEXT NOT NULL,
    href TEXT NOT NULL,
    uid TEXT NOT NULL,
    recurrence_id TEXT NOT NULL DEFAULT '',
    etag TEXT,
    workspace_urn TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_uuid, href, uid, recurrence_id)
);
CREATE INDEX IF NOT EXISTS calendar_object_states_collection_idx
    ON calendar_object_states (account_uuid, calendar_href);
"""

ZULIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS zulip_queue_states (
    account_uuid UUID PRIMARY KEY,
    queue_id TEXT,
    last_event_id BIGINT NOT NULL DEFAULT -1,
    last_message_id BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

KIND_SCHEMAS = {
    "mail": MAIL_SCHEMA,
    "calendar": CALENDAR_SCHEMA,
    "zulip": ZULIP_SCHEMA,
}


class PostgresStateRepository:
    """Provider-local state store.

    Each daemon receives a connection URL for its own database.  The repository
    never accepts or derives a Workspace database connection.
    """

    def __init__(
        self,
        connection_url: str,
        provider_kind: str,
        connect: Callable[..., Any] = psycopg.connect,
    ):
        if provider_kind not in KIND_SCHEMAS:
            raise ValueError(f"unsupported provider kind: {provider_kind}")
        self.connection_url = connection_url
        self.provider_kind = provider_kind
        self._connect = connect

    @contextlib.contextmanager
    def connection(self) -> Iterator[Any]:
        with self._connect(self.connection_url) as connection:
            yield connection

    def bootstrap(self) -> None:
        with self.connection() as connection:
            connection.execute(COMMON_SCHEMA)
            connection.execute(KIND_SCHEMAS[self.provider_kind])

    @staticmethod
    def _fetchone(connection, statement: str, parameters: tuple[Any, ...]):
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchone()

    @staticmethod
    def _fetchall(connection, statement: str, parameters: tuple[Any, ...]):
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    def save_runtime(self, provider_uuid: uuid.UUID, backend_url: str) -> None:
        with self.connection() as connection:
            existing = self._fetchone(
                connection,
                "SELECT * FROM provider_runtime WHERE singleton = TRUE",
                (),
            )
            if existing is not None and (
                existing["provider_uuid"] != provider_uuid
                or existing["backend_url"] != backend_url
                or existing["provider_kind"] != self.provider_kind
            ):
                raise RuntimeError(
                    "provider database is already bound to another runtime identity"
                )
            connection.execute(
                """
                INSERT INTO provider_runtime
                    (singleton, provider_uuid, backend_url, provider_kind)
                VALUES (TRUE, %s, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    updated_at = NOW()
                """,
                (provider_uuid, backend_url, self.provider_kind),
            )

    @staticmethod
    def config_hash(settings: dict[str, Any]) -> str:
        value = json.dumps(settings, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()

    def save_account(
        self,
        account_uuid: uuid.UUID,
        settings: dict[str, Any],
        status: str,
        error: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO account_states
                    (account_uuid, provider_kind, config_hash, status, last_error)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid) DO UPDATE SET
                    config_hash = EXCLUDED.config_hash,
                    status = EXCLUDED.status,
                    last_error = EXCLUDED.last_error,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    account_uuid,
                    self.provider_kind,
                    self.config_hash(settings),
                    status,
                    error,
                ),
            )

    def save_entity_map(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
        external_key: str,
        workspace_urn: str,
        content_hash: str | None = None,
        provider_payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO entity_maps
                    (account_uuid, entity_kind, external_key, workspace_urn,
                     content_hash, provider_payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, entity_kind, external_key) DO UPDATE SET
                    workspace_urn = EXCLUDED.workspace_urn,
                    content_hash = EXCLUDED.content_hash,
                    provider_payload = EXCLUDED.provider_payload,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    account_uuid,
                    entity_kind,
                    external_key,
                    workspace_urn,
                    content_hash,
                    Jsonb(provider_payload or {}),
                ),
            )

    def get_entity_map(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
        external_key: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                """
                SELECT * FROM entity_maps
                WHERE account_uuid = %s AND entity_kind = %s AND external_key = %s
                """,
                (account_uuid, entity_kind, external_key),
            )

    def get_entity_map_by_urn(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
        workspace_urn: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                """
                SELECT * FROM entity_maps
                WHERE account_uuid = %s AND entity_kind = %s AND workspace_urn = %s
                """,
                (account_uuid, entity_kind, workspace_urn),
            )

    def list_entity_maps(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return self._fetchall(
                connection,
                """
                SELECT * FROM entity_maps
                WHERE account_uuid = %s AND entity_kind = %s
                """,
                (account_uuid, entity_kind),
            )

    def delete_entity_map(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
        external_key: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM entity_maps
                WHERE account_uuid = %s AND entity_kind = %s AND external_key = %s
                """,
                (account_uuid, entity_kind, external_key),
            )

    def replace_entity_map(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
        external_key: str,
        workspace_urn: str,
        provider_payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM entity_maps
                WHERE account_uuid = %s AND entity_kind = %s AND workspace_urn = %s
                  AND external_key <> %s
                """,
                (account_uuid, entity_kind, workspace_urn, external_key),
            )
            connection.execute(
                """
                INSERT INTO entity_maps
                    (account_uuid, entity_kind, external_key, workspace_urn,
                     provider_payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, entity_kind, external_key) DO UPDATE SET
                    workspace_urn = EXCLUDED.workspace_urn,
                    provider_payload = EXCLUDED.provider_payload,
                    last_seen_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    account_uuid,
                    entity_kind,
                    external_key,
                    workspace_urn,
                    Jsonb(provider_payload or {}),
                ),
            )

    def get_command(self, command_uuid: uuid.UUID) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                "SELECT * FROM command_dedupe WHERE command_uuid = %s",
                (command_uuid,),
            )

    def save_command(
        self,
        command_uuid: uuid.UUID,
        status: str,
        attempts: int,
        external_id: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        next_retry_at: datetime.datetime | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO command_dedupe
                    (command_uuid, status, attempts, external_id, result,
                     last_error, next_retry_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (command_uuid) DO UPDATE SET
                    status = EXCLUDED.status,
                    attempts = EXCLUDED.attempts,
                    external_id = EXCLUDED.external_id,
                    result = EXCLUDED.result,
                    last_error = EXCLUDED.last_error,
                    next_retry_at = EXCLUDED.next_retry_at,
                    updated_at = NOW()
                """,
                (
                    command_uuid,
                    status,
                    attempts,
                    external_id,
                    Jsonb(result) if result is not None else None,
                    error,
                    next_retry_at,
                ),
            )

    def load_partitions(
        self,
        account_uuid: uuid.UUID,
        entity_kind: str,
    ) -> list[reconciliation.ReconciliationPartition]:
        with self.connection() as connection:
            rows = self._fetchall(
                connection,
                """
                SELECT * FROM reconciliation_partitions
                WHERE account_uuid = %s AND entity_kind = %s
                """,
                (account_uuid, entity_kind),
            )
        return [
            reconciliation.ReconciliationPartition(
                account_uuid=str(row["account_uuid"]),
                entity_kind=row["entity_kind"],
                partition_key=row["partition_key"],
                depth=row["depth"],
                interval_seconds=row["interval_seconds"],
                estimated_cost=row["estimated_cost"],
                mismatch_score=row["mismatch_score"],
                clean_streak=row["clean_streak"],
                last_verified_at=row["last_verified_at"],
                next_due_at=row["next_due_at"],
                cursor=row["cursor"],
            )
            for row in rows
        ]

    def save_partition(
        self,
        partition: reconciliation.ReconciliationPartition,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO reconciliation_partitions
                    (account_uuid, entity_kind, partition_key, depth,
                     interval_seconds, estimated_cost, mismatch_score,
                     clean_streak, last_verified_at, next_due_at, cursor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, entity_kind, partition_key) DO UPDATE SET
                    depth = EXCLUDED.depth,
                    interval_seconds = EXCLUDED.interval_seconds,
                    estimated_cost = EXCLUDED.estimated_cost,
                    mismatch_score = EXCLUDED.mismatch_score,
                    clean_streak = EXCLUDED.clean_streak,
                    last_verified_at = EXCLUDED.last_verified_at,
                    next_due_at = EXCLUDED.next_due_at,
                    cursor = EXCLUDED.cursor
                """,
                (
                    partition.account_uuid,
                    partition.entity_kind,
                    partition.partition_key,
                    partition.depth,
                    partition.interval_seconds,
                    partition.estimated_cost,
                    partition.mismatch_score,
                    partition.clean_streak,
                    partition.last_verified_at,
                    partition.next_due_at,
                    partition.cursor,
                ),
            )

    def save_mail_folder_state(
        self,
        account_uuid: uuid.UUID,
        path: str,
        **values: Any,
    ) -> None:
        columns = (
            "delimiter",
            "special_use",
            "uid_validity",
            "uid_next",
            "highest_modseq",
            "workspace_urn",
        )
        row = {column: values.get(column) for column in columns}
        row["delimiter"] = row["delimiter"] or "/"
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO mail_folder_states
                    (account_uuid, path, delimiter, special_use, uid_validity,
                     uid_next, highest_modseq, workspace_urn)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, path) DO UPDATE SET
                    delimiter = EXCLUDED.delimiter,
                    special_use = EXCLUDED.special_use,
                    uid_validity = EXCLUDED.uid_validity,
                    uid_next = EXCLUDED.uid_next,
                    highest_modseq = EXCLUDED.highest_modseq,
                    workspace_urn = EXCLUDED.workspace_urn,
                    updated_at = NOW()
                """,
                (account_uuid, path, *(row[column] for column in columns)),
            )

    def get_mail_folder_state(
        self,
        account_uuid: uuid.UUID,
        path: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                "SELECT * FROM mail_folder_states WHERE account_uuid = %s AND path = %s",
                (account_uuid, path),
            )

    def save_mail_message_state(
        self,
        account_uuid: uuid.UUID,
        folder_path: str,
        uid: int,
        message_id: str | None,
        workspace_urn: str,
        flags_hash: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO mail_message_states
                    (account_uuid, folder_path, uid, message_id, workspace_urn,
                     flags_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, folder_path, uid) DO UPDATE SET
                    message_id = EXCLUDED.message_id,
                    workspace_urn = EXCLUDED.workspace_urn,
                    flags_hash = EXCLUDED.flags_hash,
                    last_seen_at = NOW()
                """,
                (
                    account_uuid,
                    folder_path,
                    uid,
                    message_id,
                    workspace_urn,
                    flags_hash,
                ),
            )

    def list_mail_message_states(
        self,
        account_uuid: uuid.UUID,
        folder_path: str,
        start_uid: int = 1,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return self._fetchall(
                connection,
                """
                SELECT * FROM mail_message_states
                WHERE account_uuid = %s AND folder_path = %s AND uid >= %s
                ORDER BY uid
                """,
                (account_uuid, folder_path, start_uid),
            )

    def delete_mail_message_state(
        self,
        account_uuid: uuid.UUID,
        folder_path: str,
        uid: int,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM mail_message_states
                WHERE account_uuid = %s AND folder_path = %s AND uid = %s
                """,
                (account_uuid, folder_path, uid),
            )

    def save_calendar_collection_state(
        self,
        account_uuid: uuid.UUID,
        href: str,
        ctag: str | None,
        sync_token: str | None,
        workspace_urn: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO calendar_collection_states
                    (account_uuid, href, ctag, sync_token, workspace_urn)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, href) DO UPDATE SET
                    ctag = EXCLUDED.ctag,
                    sync_token = EXCLUDED.sync_token,
                    workspace_urn = EXCLUDED.workspace_urn,
                    updated_at = NOW()
                """,
                (account_uuid, href, ctag, sync_token, workspace_urn),
            )

    def get_calendar_collection_state(
        self,
        account_uuid: uuid.UUID,
        href: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                """
                SELECT * FROM calendar_collection_states
                WHERE account_uuid = %s AND href = %s
                """,
                (account_uuid, href),
            )

    def list_calendar_collection_states(
        self,
        account_uuid: uuid.UUID,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return self._fetchall(
                connection,
                """
                SELECT * FROM calendar_collection_states
                WHERE account_uuid = %s
                """,
                (account_uuid,),
            )

    def delete_calendar_collection_state(
        self,
        account_uuid: uuid.UUID,
        href: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM calendar_object_states
                WHERE account_uuid = %s AND calendar_href = %s
                """,
                (account_uuid, href),
            )
            connection.execute(
                """
                DELETE FROM calendar_collection_states
                WHERE account_uuid = %s AND href = %s
                """,
                (account_uuid, href),
            )

    def save_calendar_object_state(
        self,
        account_uuid: uuid.UUID,
        calendar_href: str,
        href: str,
        uid: str,
        recurrence_id: str | None,
        etag: str | None,
        workspace_urn: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO calendar_object_states
                    (account_uuid, calendar_href, href, uid, recurrence_id, etag,
                     workspace_urn)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_uuid, href, uid, recurrence_id) DO UPDATE SET
                    calendar_href = EXCLUDED.calendar_href,
                    etag = EXCLUDED.etag,
                    workspace_urn = EXCLUDED.workspace_urn,
                    last_seen_at = NOW()
                """,
                (
                    account_uuid,
                    calendar_href,
                    href,
                    uid,
                    recurrence_id or "",
                    etag,
                    workspace_urn,
                ),
            )

    def get_zulip_queue_state(
        self,
        account_uuid: uuid.UUID,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            return self._fetchone(
                connection,
                "SELECT * FROM zulip_queue_states WHERE account_uuid = %s",
                (account_uuid,),
            )

    def save_zulip_queue_state(
        self,
        account_uuid: uuid.UUID,
        queue_id: str | None,
        last_event_id: int,
        last_message_id: int,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO zulip_queue_states
                    (account_uuid, queue_id, last_event_id, last_message_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (account_uuid) DO UPDATE SET
                    queue_id = EXCLUDED.queue_id,
                    last_event_id = EXCLUDED.last_event_id,
                    last_message_id = EXCLUDED.last_message_id,
                    updated_at = NOW()
                """,
                (account_uuid, queue_id, last_event_id, last_message_id),
            )

    def list_calendar_object_states(
        self,
        account_uuid: uuid.UUID,
        calendar_href: str,
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return self._fetchall(
                connection,
                """
                SELECT * FROM calendar_object_states
                WHERE account_uuid = %s AND calendar_href = %s
                """,
                (account_uuid, calendar_href),
            )

    def delete_calendar_object_state(
        self,
        account_uuid: uuid.UUID,
        href: str,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                DELETE FROM calendar_object_states
                WHERE account_uuid = %s AND href = %s
                """,
                (account_uuid, href),
            )

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)
