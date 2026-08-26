# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Compact, exact per-user message read state.

Unread is the implicit value.  Read messages are stored in fixed-size bitmaps
addressed by an immutable message ingest sequence.  Existing projects move
through ``legacy -> dual -> compact`` so the large legacy flag table can be
converted in bounded, resumable batches without changing the public API.
"""

import collections
import collections.abc
import dataclasses
import typing
import uuid as sys_uuid


READ_CHUNK_BITS = 4096
PROJECT_SEQUENCE_RANGE_SIZE = 4_294_967_296
PROJECT_SEQUENCE_BACKFILL_MAX = 2_147_483_647


class BulkReadSnapshotCallback(typing.Protocol):
    def __call__(
        self,
        session: typing.Any,
        candidate_sql: str,
        candidate_values: collections.abc.Sequence[object],
        candidate_chunks: collections.abc.Sequence[collections.abc.Mapping[str, object]]
        | None = None,
    ) -> None: ...


COMPACTION_BATCH_SIZE = 10_000
MAINTENANCE_CANDIDATE_LIMIT = 64
VERIFY_CHUNK_BATCH_SIZE = 50
PROJECT_MODE_LEGACY = "legacy"
PROJECT_MODE_PREPARING = "preparing"
PROJECT_MODE_DUAL = "dual"
PROJECT_MODE_COMPACT = "compact"
PROJECT_MODE_ROLLBACK = "rollback"
READ_STATE_SCHEMA_LOCK_KEY = "workspace-read-state-schema-v1"
READ_STATE_STRUCTURE_LOCK_KEY = "workspace-read-state-structure-v1"
EXTERNAL_ACCOUNT_RESOURCE_LOCK_KEY = "workspace-external-account-resource-v1"


def _project_structure_lock_key(project_id: object) -> str:
    return f"{READ_STATE_STRUCTURE_LOCK_KEY}:{project_id}"


def lock_external_account_resources(
    session: typing.Any,
    account_uuids: collections.abc.Iterable[object],
    *,
    shared: bool = False,
) -> None:
    """Serialize account projection discovery without global traffic locks."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    lock_read_state_schema_shared(session)
    lock_function = (
        "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    )
    for account_uuid in sorted(set(account_uuids), key=str):
        execute(
            f"SELECT {lock_function}(hashtextextended(%s, 0))",
            (f"{EXTERNAL_ACCOUNT_RESOURCE_LOCK_KEY}:{account_uuid}",),
        )


@dataclasses.dataclass(frozen=True)
class MessageReadCoordinate:
    uuid: sys_uuid.UUID
    topic_uuid: sys_uuid.UUID
    ingest_sequence: int


@dataclasses.dataclass(frozen=True)
class BulkReadResult:
    message_rows: list[collections.abc.Mapping[str, typing.Any]]
    topic_uuids: list[sys_uuid.UUID]

    @property
    def changed(self) -> bool:
        return bool(self.topic_uuids)


class MaintenanceProjectError(RuntimeError):
    """Identify a failed project so the worker can rotate past it."""

    def __init__(self, project_id: object) -> None:
        super().__init__(f"Workspace read-state maintenance failed for {project_id}")
        self.project_id = project_id


def lock_read_state_schema_shared(session: typing.Any) -> None:
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    execute(
        """
        SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))
        """,
        (READ_STATE_SCHEMA_LOCK_KEY,),
    )


def project_mode(session: typing.Any, project_id: object) -> str:
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return PROJECT_MODE_LEGACY
    # PostgreSQL takes relation locks while parsing/planning a statement, so
    # this must be a separate statement rather than a CTE in the query below.
    lock_read_state_schema_shared(session)
    result = execute(
        """
        SELECT mode
        FROM m_workspace_read_state_projects_v1
        WHERE project_id = %s
        """,
        (project_id,),
    )
    fetchone = getattr(result, "fetchone", None)
    if not callable(fetchone):
        return PROJECT_MODE_LEGACY
    row = fetchone()
    # Missing rows can be created by an older writer during a rolling upgrade.
    # Keep those projects on the legacy authority until a current writer has
    # registered them explicitly.
    if row is None:
        return PROJECT_MODE_LEGACY
    if not isinstance(row, collections.abc.Mapping) or row.get("mode") not in {
        PROJECT_MODE_LEGACY,
        PROJECT_MODE_PREPARING,
        PROJECT_MODE_DUAL,
        PROJECT_MODE_COMPACT,
        PROJECT_MODE_ROLLBACK,
    }:
        return PROJECT_MODE_LEGACY
    return row["mode"]


def project_structure_revision(session: typing.Any, project_id: object) -> int:
    """Return the optimistic epoch for message and membership changes."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return 0
    lock_read_state_schema_shared(session)
    result = execute(
        """
        SELECT structure_revision
        FROM m_workspace_read_state_projects_v1
        WHERE project_id = %s
        """,
        (project_id,),
    )
    fetchone = getattr(result, "fetchone", None)
    if not callable(fetchone):
        return 0
    row = fetchone()
    return 0 if row is None else int(row["structure_revision"])


def user_read_revision(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
) -> int:
    """Return one user's compact read epoch without coupling other readers."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return 0
    lock_read_state_schema_shared(session)
    result = execute(
        """
        SELECT revision
        FROM m_workspace_user_read_revisions_v1
        WHERE project_id = %s AND user_uuid = %s
        """,
        (project_id, user_uuid),
    )
    fetchone = getattr(result, "fetchone", None)
    if not callable(fetchone):
        return 0
    row = fetchone()
    return 0 if row is None else int(row["revision"])


def bump_user_read_revision(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
) -> None:
    """Invalidate only this user's optimistic compact read snapshot."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    execute(
        """
        INSERT INTO m_workspace_user_read_revisions_v1 (
            project_id, user_uuid, revision, created_at, updated_at
        ) VALUES (%s, %s, 1, NOW(), NOW())
        ON CONFLICT (project_id, user_uuid) DO UPDATE
        SET revision = m_workspace_user_read_revisions_v1.revision + 1,
            updated_at = NOW()
        """,
        (project_id, user_uuid),
    )


def bump_project_structure_revisions(
    session: typing.Any,
    project_ids: collections.abc.Iterable[object],
) -> None:
    """Invalidate optimistic bulk snapshots after project locks are held."""
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    values = sorted(
        {sys_uuid.UUID(str(project_id)) for project_id in project_ids},
        key=str,
    )
    if not values:
        return
    execute(
        """
        UPDATE m_workspace_read_state_projects_v1
        SET structure_revision = structure_revision + 1,
            updated_at = NOW()
        WHERE project_id = ANY(%s::uuid[])
        """,
        (values,),
    )


def mode_uses_compact_state(mode: object) -> bool:
    return mode in {PROJECT_MODE_COMPACT, PROJECT_MODE_ROLLBACK}


def uses_compact_state(session: typing.Any, project_id: object) -> bool:
    return mode_uses_compact_state(project_mode(session, project_id))


def writes_compact_state(session: typing.Any, project_id: object) -> bool:
    return project_mode(session, project_id) in {
        PROJECT_MODE_PREPARING,
        PROJECT_MODE_DUAL,
        PROJECT_MODE_COMPACT,
        PROJECT_MODE_ROLLBACK,
    }


def ensure_new_project(session: typing.Any, project_id: object) -> None:
    session.execute(
        """
        INSERT INTO m_workspace_read_state_projects_v1 (
            project_id, mode, created_at, updated_at
        )
        VALUES (%s, 'legacy', NOW(), NOW())
        ON CONFLICT (project_id) DO NOTHING
        """,
        (project_id,),
    )


def _assign_legacy_ingest_sequences(
    session: typing.Any,
    project_id: object,
    *,
    batch_size: int,
    stream_uuid: object | None = None,
    message_uuid: object | None = None,
) -> int:
    # The project lock taken by every caller serializes this resumable
    # allocator with structural changes. The range row also serializes it with
    # live inserts performed by the database trigger.
    session.execute(
        """
        INSERT INTO m_workspace_project_ingest_ranges_v2 (
            project_id, range_number
        ) VALUES (
            %s, nextval('m_workspace_project_ingest_range_v2_seq')
        )
        ON CONFLICT (project_id) DO NOTHING
        """,
        (project_id,),
    )
    range_row = session.execute(
        """
        SELECT range_number, last_backfill_sequence
        FROM m_workspace_project_ingest_ranges_v2
        WHERE project_id = %s
        FOR UPDATE
        """,
        (project_id,),
    ).fetchone()
    range_number = int(range_row["range_number"])
    range_base = range_number * PROJECT_SEQUENCE_RANGE_SIZE
    conditions = [
        "project_id = %s",
        "(ingest_sequence IS NULL OR ingest_sequence <= %s OR ingest_sequence >= %s)",
    ]
    values: list[object] = [
        project_id,
        range_base,
        range_base + PROJECT_SEQUENCE_RANGE_SIZE,
    ]
    if stream_uuid is not None:
        conditions.append("stream_uuid = %s")
        values.append(stream_uuid)
    if message_uuid is not None:
        conditions.append("uuid = %s")
        values.append(message_uuid)
    rows = session.execute(
        f"""
        SELECT uuid
        FROM m_workspace_messages
        WHERE {" AND ".join(conditions)}
        ORDER BY created_at, uuid
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (*values, batch_size),
    ).fetchall()
    if not rows:
        return 0
    first = int(range_row["last_backfill_sequence"]) + 1
    last = first + len(rows) - 1
    if last > PROJECT_SEQUENCE_BACKFILL_MAX:
        raise RuntimeError("Workspace project backfill sequence is exhausted")
    session.execute(
        """
        UPDATE m_workspace_project_ingest_ranges_v2
        SET last_backfill_sequence = %s, updated_at = NOW()
        WHERE project_id = %s
        """,
        (last, project_id),
    )
    session.execute(
        """
        UPDATE m_workspace_messages AS message
        SET ingest_sequence = allocated.ingest_sequence
        FROM unnest(%s::uuid[], %s::bigint[])
            AS allocated(uuid, ingest_sequence)
        WHERE message.uuid = allocated.uuid
          AND message.project_id = %s
          AND (
                message.ingest_sequence IS NULL
                OR message.ingest_sequence <= %s
                OR message.ingest_sequence >= %s
          )
        """,
        (
            [row["uuid"] for row in rows],
            [range_base + sequence for sequence in range(first, last + 1)],
            project_id,
            range_base,
            range_base + PROJECT_SEQUENCE_RANGE_SIZE,
        ),
    )
    return len(rows)


def _allocate_project_backfill_sequence(
    session: typing.Any,
    project_id: object,
) -> int:
    session.execute(
        """
        INSERT INTO m_workspace_project_ingest_ranges_v2 (
            project_id, range_number
        ) VALUES (
            %s, nextval('m_workspace_project_ingest_range_v2_seq')
        )
        ON CONFLICT (project_id) DO NOTHING
        """,
        (project_id,),
    )
    range_row = session.execute(
        """
        SELECT range_number, last_backfill_sequence
        FROM m_workspace_project_ingest_ranges_v2
        WHERE project_id = %s
        FOR UPDATE
        """,
        (project_id,),
    ).fetchone()
    local_sequence = int(range_row["last_backfill_sequence"]) + 1
    if local_sequence > PROJECT_SEQUENCE_BACKFILL_MAX:
        raise RuntimeError("Workspace project backfill sequence is exhausted")
    session.execute(
        """
        UPDATE m_workspace_project_ingest_ranges_v2
        SET last_backfill_sequence = %s, updated_at = NOW()
        WHERE project_id = %s
        """,
        (local_sequence, project_id),
    )
    return int(range_row["range_number"]) * PROJECT_SEQUENCE_RANGE_SIZE + local_sequence


def lock_projects(
    session: typing.Any,
    project_ids: collections.abc.Iterable[object],
) -> None:
    # The shared schema gate lets downgrade stop every current writer before
    # its final authority/view swap, including a writer for a project row that
    # does not exist yet.  It is transaction scoped and reentrant.
    lock_read_state_schema_shared(session)
    for project_id in sorted(set(project_ids), key=str):
        session.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
            """,
            (project_id,),
        )


def lock_message_structure(
    session: typing.Any,
    project_ids: collections.abc.Iterable[object],
    *,
    shared: bool = False,
    cross_project: bool = False,
) -> None:
    """Fence structural mutations in canonical project lock order.

    The lock is project-local. Cross-project callers pass their complete set,
    which is sorted here, so unrelated provider traffic is never globalized.
    """
    del cross_project
    execute = getattr(session, "execute", None)
    if not callable(execute):
        return
    lock_read_state_schema_shared(session)
    project_lock_function = (
        "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    )
    for project_id in sorted(set(project_ids), key=str):
        execute(
            f"SELECT {project_lock_function}(hashtextextended(%s, 0))",
            (_project_structure_lock_key(project_id),),
        )


def reset_identity_sensitive_progress(
    session: typing.Any,
    project_ids: collections.abc.Iterable[object],
) -> None:
    """Restart user-keyed cursors after an identity UUID rewrite.

    Identity merges can move unprocessed rows behind lexicographic cursors.
    Callers already hold every affected project lock, so restarting these
    idempotent phases is atomic with the rewrite and remains bounded.
    """
    values = [sys_uuid.UUID(str(value)) for value in set(project_ids)]
    if not values:
        return
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = NULL,
            last_user_uuid = NULL,
            last_ingest_sequence = 0,
            processed_rows = 0,
            completed_at = NULL,
            updated_at = NOW()
        WHERE project_id = ANY(%s::uuid[])
          AND phase IN ('memberships', 'flags')
        """,
        (values,),
    )
    downgrade_relation = session.execute(
        """
        SELECT to_regclass('m_workspace_read_state_downgrade_v1') AS relation
        """
    ).fetchone()
    if downgrade_relation is None or downgrade_relation["relation"] is None:
        return
    session.execute(
        """
        UPDATE m_workspace_read_state_downgrade_v1
        SET last_created_at = NULL,
            last_ingest_sequence = NULL,
            last_message_uuid = NULL,
            last_user_uuid = NULL,
            processed_rows = 0,
            completed_at = NULL,
            updated_at = NOW()
        WHERE project_id = ANY(%s::uuid[])
        """,
        (values,),
    )


def record_message_created(
    session: typing.Any,
    project_id: object,
    stream_uuid: object,
    topic_uuid: object,
    ingest_sequence: int,
) -> None:
    session.execute(
        """
        INSERT INTO m_workspace_topic_message_stats_v1 (
            topic_uuid, project_id, stream_uuid, message_count,
            last_ingest_sequence, created_at, updated_at
        ) VALUES (%s, %s, %s, 1, %s, NOW(), NOW())
        ON CONFLICT (topic_uuid) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            stream_uuid = EXCLUDED.stream_uuid,
            message_count = m_workspace_topic_message_stats_v1.message_count + 1,
            last_ingest_sequence = GREATEST(
                m_workspace_topic_message_stats_v1.last_ingest_sequence,
                EXCLUDED.last_ingest_sequence
            ),
            updated_at = NOW()
        """,
        (topic_uuid, project_id, stream_uuid, ingest_sequence),
    )
    bump_project_structure_revisions(session, (project_id,))


def record_message_deleted(
    session: typing.Any,
    project_id: object,
    topic_uuid: object,
    message_uuid: object,
) -> None:
    session.execute(
        """
        UPDATE m_workspace_topic_message_stats_v1
        SET message_count = GREATEST(message_count - 1, 0),
            last_ingest_sequence = (
                SELECT MAX(message.ingest_sequence)
                FROM m_workspace_messages AS message
                WHERE message.project_id = %s
                  AND message.topic_uuid = %s
                  AND message.uuid <> %s
            ),
            updated_at = NOW()
        WHERE project_id = %s AND topic_uuid = %s
        """,
        (project_id, topic_uuid, message_uuid, project_id, topic_uuid),
    )


def message_coordinate(
    session: typing.Any,
    project_id: object,
    message_uuid: object,
) -> MessageReadCoordinate | None:
    row = session.execute(
        """
        SELECT uuid, topic_uuid, ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, message_uuid),
    ).fetchone()
    if row is None:
        return None
    return MessageReadCoordinate(
        uuid=row["uuid"],
        topic_uuid=row["topic_uuid"],
        ingest_sequence=row["ingest_sequence"],
    )


def _coordinate_parts(coordinate: MessageReadCoordinate) -> tuple[int, int]:
    return (
        coordinate.ingest_sequence // READ_CHUNK_BITS,
        coordinate.ingest_sequence % READ_CHUNK_BITS,
    )


def _bits_literal(offsets: collections.abc.Iterable[int]) -> str:
    bits = ["0"] * READ_CHUNK_BITS
    for offset in offsets:
        bits[offset] = "1"
    return "".join(bits)


def _apply_masks(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    masks: collections.abc.Mapping[int, tuple[set[int], set[int]]],
) -> None:
    for chunk_number, (covered_offsets, read_offsets) in sorted(masks.items()):
        covered_bits = _bits_literal(covered_offsets)
        read_bits = _bits_literal(read_offsets)
        session.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number,
                read_bits, created_at, updated_at
            ) VALUES (%s, %s, %s::bit(4096), NOW(), NOW())
            ON CONFLICT (user_uuid, chunk_number)
            DO UPDATE SET
                read_bits = (
                    m_workspace_user_read_chunks_v1.read_bits
                    & ~%s::bit(4096)
                ) | %s::bit(4096),
                updated_at = NOW()
            """,
            (
                user_uuid,
                chunk_number,
                read_bits,
                covered_bits,
                read_bits,
            ),
        )
        session.execute(
            """
            DELETE FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
              AND chunk_number = %s
              AND bit_count(read_bits) = 0
            """,
            (user_uuid, chunk_number),
        )


def _refresh_topic_read_stats(
    session: typing.Any,
    project_id: object,
    scopes: collections.abc.Iterable[tuple[object, object]],
) -> None:
    scopes = sorted(set(scopes), key=lambda value: (str(value[0]), str(value[1])))
    if not scopes:
        return
    session.execute(
        f"""
        WITH scopes AS (
            SELECT DISTINCT user_uuid, topic_uuid
            FROM unnest(%s::uuid[], %s::uuid[])
                AS scope(user_uuid, topic_uuid)
        ), canonical AS (
            SELECT
                scope.user_uuid,
                scope.topic_uuid,
                COUNT(message.uuid) FILTER (
                    WHERE COALESCE(
                        get_bit(
                            chunk.read_bits,
                            (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                        ),
                        0
                    ) = 1
                ) AS read_count
            FROM scopes AS scope
            LEFT JOIN m_workspace_messages AS message
              ON message.project_id = %s
             AND message.topic_uuid = scope.topic_uuid
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = scope.user_uuid
             AND chunk.chunk_number =
                    message.ingest_sequence / {READ_CHUNK_BITS}
            GROUP BY scope.user_uuid, scope.topic_uuid
        )
        INSERT INTO m_workspace_user_topic_read_stats_v1 (
            project_id, user_uuid, topic_uuid, read_count,
            created_at, updated_at
        )
        SELECT %s, user_uuid, topic_uuid, read_count, NOW(), NOW()
        FROM canonical
        ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
        SET read_count = EXCLUDED.read_count, updated_at = NOW()
        """,
        (
            [scope[0] for scope in scopes],
            [scope[1] for scope in scopes],
            project_id,
            project_id,
        ),
    )


def _read_deltas_for_rows(
    session: typing.Any,
    rows: collections.abc.Sequence[collections.abc.Mapping[str, typing.Any]],
) -> list[collections.abc.Mapping[str, typing.Any]]:
    if not rows:
        return []
    return session.execute(
        f"""
        WITH coordinates AS (
            SELECT DISTINCT ON (user_uuid, ingest_sequence)
                user_uuid, topic_uuid, ingest_sequence, read
            FROM unnest(
                %s::uuid[], %s::uuid[], %s::bigint[], %s::boolean[]
            ) AS coordinate(
                user_uuid, topic_uuid, ingest_sequence, read
            )
            ORDER BY user_uuid, ingest_sequence
        )
        SELECT
            coordinate.user_uuid,
            coordinate.topic_uuid,
            SUM(
                CASE
                    WHEN COALESCE(
                        get_bit(
                            chunk.read_bits,
                            (coordinate.ingest_sequence %% {READ_CHUNK_BITS})::integer
                        ),
                        0
                    ) = CASE WHEN coordinate.read THEN 1 ELSE 0 END
                        THEN 0
                    WHEN coordinate.read THEN 1
                    ELSE -1
                END
            )::bigint AS read_delta
        FROM coordinates AS coordinate
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = coordinate.user_uuid
         AND chunk.chunk_number =
                coordinate.ingest_sequence / {READ_CHUNK_BITS}
        GROUP BY coordinate.user_uuid, coordinate.topic_uuid
        HAVING SUM(
            CASE
                WHEN COALESCE(
                    get_bit(
                        chunk.read_bits,
                        (coordinate.ingest_sequence %% {READ_CHUNK_BITS})::integer
                    ),
                    0
                ) = CASE WHEN coordinate.read THEN 1 ELSE 0 END
                    THEN 0
                WHEN coordinate.read THEN 1
                ELSE -1
            END
        ) <> 0
        """,
        (
            [row["user_uuid"] for row in rows],
            [row["topic_uuid"] for row in rows],
            [row["ingest_sequence"] for row in rows],
            [row["read"] for row in rows],
        ),
    ).fetchall()


def _adjust_topic_read_stats(
    session: typing.Any,
    project_id: object,
    deltas: collections.abc.Sequence[collections.abc.Mapping[str, typing.Any]],
) -> None:
    if not deltas:
        return
    values = (
        [row["user_uuid"] for row in deltas],
        [row["topic_uuid"] for row in deltas],
        [row["read_delta"] for row in deltas],
    )
    session.execute(
        """
        WITH deltas AS (
            SELECT user_uuid, topic_uuid, SUM(read_delta)::bigint AS read_delta
            FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])
                AS delta(user_uuid, topic_uuid, read_delta)
            GROUP BY user_uuid, topic_uuid
        )
        UPDATE m_workspace_user_topic_read_stats_v1 AS stats
        SET read_count = GREATEST(stats.read_count + delta.read_delta, 0),
            updated_at = NOW()
        FROM deltas AS delta
        WHERE stats.project_id = %s
          AND stats.user_uuid = delta.user_uuid
          AND stats.topic_uuid = delta.topic_uuid
        """,
        (*values, project_id),
    )
    session.execute(
        """
        WITH deltas AS (
            SELECT user_uuid, topic_uuid, SUM(read_delta)::bigint AS read_delta
            FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])
                AS delta(user_uuid, topic_uuid, read_delta)
            GROUP BY user_uuid, topic_uuid
        )
        INSERT INTO m_workspace_user_topic_read_stats_v1 (
            project_id, user_uuid, topic_uuid, read_count,
            created_at, updated_at
        )
        SELECT %s, delta.user_uuid, delta.topic_uuid, delta.read_delta,
               NOW(), NOW()
        FROM deltas AS delta
        WHERE delta.read_delta > 0
          AND NOT EXISTS (
                SELECT 1
                FROM m_workspace_user_topic_read_stats_v1 AS stats
                WHERE stats.project_id = %s
                  AND stats.user_uuid = delta.user_uuid
                  AND stats.topic_uuid = delta.topic_uuid
            )
        ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
        SET read_count = GREATEST(
                m_workspace_user_topic_read_stats_v1.read_count
                    + EXCLUDED.read_count,
                0
            ),
            updated_at = NOW()
        """,
        (*values, project_id, project_id),
    )


def _apply_coordinate_rows(
    session: typing.Any,
    project_id: object,
    rows: collections.abc.Sequence[collections.abc.Mapping[str, typing.Any]],
) -> None:
    """Apply one compaction page with three set-based statements."""
    if not rows:
        return
    read_deltas = _read_deltas_for_rows(session, rows)
    user_uuids = [row["user_uuid"] for row in rows]
    ingest_sequences = [row["ingest_sequence"] for row in rows]
    read_values = [row["read"] for row in rows]
    coordinate_values = (
        user_uuids,
        ingest_sequences,
        read_values,
    )
    session.execute(
        f"""
        WITH coordinates AS (
            SELECT user_uuid, ingest_sequence, read
            FROM unnest(
                %s::uuid[], %s::bigint[], %s::boolean[]
            ) AS coordinate(
                user_uuid, ingest_sequence, read
            )
        ), masks AS (
            SELECT
                user_uuid,
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        (ingest_sequence %% {READ_CHUNK_BITS})::integer,
                        1
                    )
                ) AS covered_bits
            FROM coordinates
            GROUP BY user_uuid, ingest_sequence / {READ_CHUNK_BITS}
        )
        UPDATE m_workspace_user_read_chunks_v1 AS chunk
        SET read_bits = chunk.read_bits & ~masks.covered_bits,
            updated_at = NOW()
        FROM masks
        WHERE chunk.user_uuid = masks.user_uuid
          AND chunk.chunk_number = masks.chunk_number
        """,
        coordinate_values,
    )
    session.execute(
        f"""
        WITH coordinates AS (
            SELECT user_uuid, ingest_sequence
            FROM unnest(
                %s::uuid[], %s::bigint[], %s::boolean[]
            ) AS coordinate(
                user_uuid, ingest_sequence, read
            )
            WHERE read
        ), masks AS (
            SELECT
                user_uuid,
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        (ingest_sequence %% {READ_CHUNK_BITS})::integer,
                        1
                    )
                ) AS read_bits
            FROM coordinates
            GROUP BY user_uuid, ingest_sequence / {READ_CHUNK_BITS}
        )
        INSERT INTO m_workspace_user_read_chunks_v1 (
            user_uuid, chunk_number,
            read_bits, created_at, updated_at
        )
        SELECT user_uuid, chunk_number, read_bits, NOW(), NOW()
        FROM masks
        ON CONFLICT (user_uuid, chunk_number)
        DO UPDATE SET
            read_bits = (
                m_workspace_user_read_chunks_v1.read_bits
                | EXCLUDED.read_bits
            ),
            updated_at = NOW()
        """,
        coordinate_values,
    )
    session.execute(
        f"""
        WITH touched AS (
            SELECT DISTINCT
                user_uuid,
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number
            FROM unnest(
                %s::uuid[], %s::bigint[]
            ) AS coordinate(user_uuid, ingest_sequence)
        )
        DELETE FROM m_workspace_user_read_chunks_v1 AS chunk
        USING touched
        WHERE chunk.user_uuid = touched.user_uuid
          AND chunk.chunk_number = touched.chunk_number
          AND bit_count(chunk.read_bits) = 0
        """,
        (user_uuids, ingest_sequences),
    )
    _adjust_topic_read_stats(session, project_id, read_deltas)


def _record_detached_memberships(
    session: typing.Any,
    project_id: object,
    rows: collections.abc.Sequence[collections.abc.Mapping[str, typing.Any]],
) -> None:
    if not rows:
        return
    session.execute(
        """
        WITH scopes AS (
            SELECT user_uuid, stream_uuid,
                   MAX(ingest_sequence) AS last_detached_sequence
            FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])
                AS scope(user_uuid, stream_uuid, ingest_sequence)
            GROUP BY user_uuid, stream_uuid
        )
        INSERT INTO m_workspace_read_memberships_v1 (
            project_id, user_uuid, stream_uuid, last_detached_sequence,
            created_at, updated_at
        )
        SELECT
            %s,
            scope.user_uuid,
            scope.stream_uuid,
            scope.last_detached_sequence,
            NOW(),
            NOW()
        FROM scopes AS scope
        WHERE NOT EXISTS (
            SELECT 1
            FROM m_workspace_stream_bindings AS binding
            WHERE binding.project_id = %s
              AND binding.stream_uuid = scope.stream_uuid
              AND binding.user_uuid = scope.user_uuid
        )
        ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
        SET last_detached_sequence = GREATEST(
                m_workspace_read_memberships_v1.last_detached_sequence,
                EXCLUDED.last_detached_sequence
            ),
            updated_at = NOW()
        """,
        (
            [row["user_uuid"] for row in rows],
            [row["stream_uuid"] for row in rows],
            [row["ingest_sequence"] for row in rows],
            project_id,
            project_id,
        ),
    )


def set_coordinates_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    coordinates: collections.abc.Sequence[MessageReadCoordinate],
    read: bool,
    *,
    coordinates_are_structurally_locked: bool = False,
) -> bool:
    submitted_coordinates = {
        coordinate.ingest_sequence: coordinate for coordinate in coordinates
    }
    requested_uuids = sorted(
        {coordinate.uuid for coordinate in submitted_coordinates.values()},
        key=str,
    )
    if not requested_uuids:
        return False
    # Serialize the mode decision with compact->rollback and rollback->legacy.
    # A writer that selected a compact path before waiting on this lock must
    # re-check the persisted authority before it changes either representation.
    lock_projects(session, (project_id,))
    if coordinates_are_structurally_locked:
        unique_coordinates = submitted_coordinates
    else:
        current_rows = session.execute(
            """
            SELECT uuid, topic_uuid, ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (project_id, requested_uuids),
        ).fetchall()
        unique_coordinates = {
            row["ingest_sequence"]: MessageReadCoordinate(
                row["uuid"],
                row["topic_uuid"],
                row["ingest_sequence"],
            )
            for row in current_rows
        }
    if not unique_coordinates:
        return False
    mode = project_mode(session, project_id)
    if mode == PROJECT_MODE_LEGACY:
        return _set_legacy_coordinates_read(
            session,
            project_id,
            user_uuid,
            list(unique_coordinates.values()),
            read,
        )
    coordinate_rows = [
        {
            "user_uuid": user_uuid,
            "topic_uuid": coordinate.topic_uuid,
            "ingest_sequence": coordinate.ingest_sequence,
            "read": read,
        }
        for coordinate in unique_coordinates.values()
    ]
    read_deltas = _read_deltas_for_rows(session, coordinate_rows)
    if not read_deltas:
        if mode == PROJECT_MODE_ROLLBACK:
            _set_legacy_coordinates_read(
                session,
                project_id,
                user_uuid,
                list(unique_coordinates.values()),
                read,
            )
        return False
    masks: collections.defaultdict[int, tuple[set[int], set[int]]] = (
        collections.defaultdict(lambda: (set(), set()))
    )
    for coordinate in unique_coordinates.values():
        chunk_number, offset = _coordinate_parts(coordinate)
        covered_offsets, read_offsets = masks[chunk_number]
        covered_offsets.add(offset)
        if read:
            read_offsets.add(offset)
    _apply_masks(session, project_id, user_uuid, masks)
    _adjust_topic_read_stats(session, project_id, read_deltas)
    if mode == PROJECT_MODE_ROLLBACK:
        _set_legacy_coordinates_read(
            session,
            project_id,
            user_uuid,
            list(unique_coordinates.values()),
            read,
        )
    bump_user_read_revision(session, project_id, user_uuid)
    return True


def _set_legacy_coordinates_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    coordinates: collections.abc.Sequence[MessageReadCoordinate],
    read: bool,
) -> bool:
    if not coordinates:
        return False
    changed = session.execute(
        """
        WITH coordinates AS (
            SELECT DISTINCT coordinate_uuid
            FROM unnest(%s::uuid[]) AS coordinate_uuid
        ), changed AS (
            INSERT INTO m_workspace_user_message_flags AS flags (
                uuid, user_uuid, project_id, read, pinned, starred,
                created_at, updated_at
            )
            SELECT
                message.uuid, %s, %s, %s, FALSE, FALSE, NOW(), NOW()
            FROM coordinates AS coordinate
            JOIN m_workspace_messages AS message
              ON message.uuid = coordinate.coordinate_uuid
             AND message.project_id = %s
            ON CONFLICT (uuid, user_uuid) DO UPDATE
            SET read = EXCLUDED.read, updated_at = NOW()
            WHERE flags.project_id = EXCLUDED.project_id
              AND flags.read IS DISTINCT FROM EXCLUDED.read
            RETURNING uuid
        )
        SELECT EXISTS (SELECT 1 FROM changed) AS changed
        """,
        (
            [coordinate.uuid for coordinate in coordinates],
            user_uuid,
            project_id,
            read,
            project_id,
        ),
    ).fetchone()
    return bool(changed["changed"])


def set_message_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    message_uuid: object,
    read: bool,
) -> bool:
    coordinate = message_coordinate(session, project_id, message_uuid)
    if coordinate is None:
        return False
    return set_coordinates_read(
        session,
        project_id,
        user_uuid,
        [coordinate],
        read,
    )


def set_message_uuids_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    message_uuids: collections.abc.Collection[object],
    read: bool,
) -> None:
    if not message_uuids:
        return
    rows = session.execute(
        """
        SELECT uuid, topic_uuid, ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND uuid = ANY(%s::uuid[])
        ORDER BY uuid
        """,
        (project_id, list(message_uuids)),
    ).fetchall()
    coordinates = [
        MessageReadCoordinate(row["uuid"], row["topic_uuid"], row["ingest_sequence"])
        for row in rows
    ]
    set_coordinates_read(session, project_id, user_uuid, coordinates, read)


def message_read_user_uuids(
    session: typing.Any,
    project_id: object,
    message_uuid: object,
) -> list[sys_uuid.UUID]:
    mode = project_mode(session, project_id)
    if not mode_uses_compact_state(mode):
        return [
            row["user_uuid"]
            for row in session.execute(
                """
                SELECT user_uuid
                FROM m_workspace_user_message_flags
                WHERE project_id = %s AND uuid = %s AND read = TRUE
                ORDER BY user_uuid
                """,
                (project_id, message_uuid),
            ).fetchall()
        ]
    coordinate = message_coordinate(session, project_id, message_uuid)
    if coordinate is None:
        return []
    chunk_number, offset = _coordinate_parts(coordinate)
    return [
        row["user_uuid"]
        for row in session.execute(
            """
            SELECT user_uuid
            FROM m_workspace_user_read_chunks_v1
            WHERE chunk_number = %s
              AND get_bit(read_bits, %s) = 1
            ORDER BY user_uuid
            """,
            (chunk_number, offset),
        ).fetchall()
    ]


def _coordinate_belongs_to_project(
    session: typing.Any,
    project_id: object,
    ingest_sequence: int,
) -> bool:
    range_row = session.execute(
        """
        SELECT range_number
        FROM m_workspace_project_ingest_ranges_v2
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchone()
    if range_row is None:
        return False
    range_base = int(range_row["range_number"]) * PROJECT_SEQUENCE_RANGE_SIZE
    return range_base < ingest_sequence < range_base + PROJECT_SEQUENCE_RANGE_SIZE


def _rebind_provider_candidate_coordinate(
    session: typing.Any,
    old_ingest_sequence: int,
    new_ingest_sequence: int,
) -> None:
    if old_ingest_sequence == new_ingest_sequence:
        return
    old_chunk_number, old_offset = divmod(
        old_ingest_sequence,
        READ_CHUNK_BITS,
    )
    new_chunk_number, new_offset = divmod(
        new_ingest_sequence,
        READ_CHUNK_BITS,
    )
    provider_lanes = session.execute(
        """
        SELECT DISTINCT
            snapshot.bridge_instance_uuid,
            snapshot.external_account_uuid,
            snapshot.causal_lane
        FROM m_external_provider_read_candidate_chunks_v1 AS candidate
        JOIN m_external_provider_read_snapshots_v1 AS snapshot
          ON snapshot.external_operation_uuid = candidate.external_operation_uuid
        WHERE candidate.chunk_number = %s
          AND get_bit(candidate.candidate_bits, %s) = 1
        """,
        (old_chunk_number, old_offset),
    ).fetchall()
    for bridge_instance_uuid in sorted(
        {row["bridge_instance_uuid"] for row in provider_lanes},
        key=str,
    ):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"provider-read-materialize-v1:{bridge_instance_uuid}",),
        )
    for row in sorted(
        provider_lanes,
        key=lambda value: (
            str(value["bridge_instance_uuid"]),
            str(value["external_account_uuid"]),
            str(value["causal_lane"]),
        ),
    ):
        session.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (
                "provider-causal-lane-v1:"
                f"{row['bridge_instance_uuid']}:"
                f"{row['external_account_uuid']}:"
                f"{row['causal_lane']}",
            ),
        )
    session.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
            m_workspace_message_provider_read_move_v1 (
                external_operation_uuid UUID PRIMARY KEY
            ) ON COMMIT DROP
        """
    )
    session.execute("TRUNCATE m_workspace_message_provider_read_move_v1")
    session.execute(
        """
        INSERT INTO m_workspace_message_provider_read_move_v1 (
            external_operation_uuid
        )
        SELECT external_operation_uuid
        FROM m_external_provider_read_candidate_chunks_v1
        WHERE chunk_number = %s
          AND get_bit(candidate_bits, %s) = 1
        """,
        (old_chunk_number, old_offset),
    )
    session.execute(
        """
        DELETE FROM m_external_provider_read_candidate_chunks_v1 AS candidate
        USING m_workspace_message_provider_read_move_v1 AS moved
        WHERE candidate.external_operation_uuid = moved.external_operation_uuid
          AND candidate.chunk_number = %s
          AND bit_count(set_bit(candidate.candidate_bits, %s, 0)) = 0
        """,
        (old_chunk_number, old_offset),
    )
    session.execute(
        """
        UPDATE m_external_provider_read_candidate_chunks_v1 AS candidate
        SET candidate_bits = set_bit(candidate.candidate_bits, %s, 0)
        FROM m_workspace_message_provider_read_move_v1 AS moved
        WHERE candidate.external_operation_uuid = moved.external_operation_uuid
          AND candidate.chunk_number = %s
          AND get_bit(candidate.candidate_bits, %s) = 1
        """,
        (old_offset, old_chunk_number, old_offset),
    )
    session.execute(
        f"""
        INSERT INTO m_external_provider_read_candidate_chunks_v1 (
            external_operation_uuid, chunk_number, candidate_bits
        )
        SELECT
            external_operation_uuid,
            %s,
            set_bit(B'0'::bit({READ_CHUNK_BITS}), %s, 1)
        FROM m_workspace_message_provider_read_move_v1
        ON CONFLICT (external_operation_uuid, chunk_number) DO UPDATE
        SET candidate_bits = set_bit(
                m_external_provider_read_candidate_chunks_v1.candidate_bits,
                %s,
                1
            )
        """,
        (new_chunk_number, new_offset, new_offset),
    )


def relocate_message(
    session: typing.Any,
    message_uuid: object,
    source_project_id: object,
    destination_project_id: object,
    destination_stream_uuid: object,
    destination_topic_uuid: object,
) -> None:
    lock_read_state_schema_shared(session)
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"workspace-message-resource-v1:{message_uuid}",),
    )
    lock_message_structure(
        session,
        (source_project_id, destination_project_id),
        cross_project=source_project_id != destination_project_id,
    )
    lock_projects(session, (source_project_id, destination_project_id))
    ensure_new_project(session, destination_project_id)
    bump_project_structure_revisions(
        session,
        (source_project_id, destination_project_id),
    )
    coordinate = message_coordinate(session, source_project_id, message_uuid)
    if coordinate is None:
        return
    message = session.execute(
        """
        SELECT stream_uuid, payload->>'content' AS content
        FROM m_workspace_messages
        WHERE project_id = %s AND uuid = %s
        """,
        (source_project_id, message_uuid),
    ).fetchone()
    if (
        source_project_id == destination_project_id
        and message["stream_uuid"] == destination_stream_uuid
        and coordinate.topic_uuid == destination_topic_uuid
    ):
        return
    rebind_coordinate = source_project_id != destination_project_id or not (
        _coordinate_belongs_to_project(
            session,
            source_project_id,
            coordinate.ingest_sequence,
        )
    )
    destination_ingest_sequence = coordinate.ingest_sequence
    if rebind_coordinate:
        destination_ingest_sequence = _allocate_project_backfill_sequence(
            session,
            destination_project_id,
        )
    read_user_uuids = message_read_user_uuids(
        session,
        source_project_id,
        message_uuid,
    )
    source_writes_compact = writes_compact_state(session, source_project_id)
    destination_writes_compact = writes_compact_state(
        session,
        destination_project_id,
    )
    if source_writes_compact:
        clear_message_for_all_users(session, source_project_id, message_uuid)
        record_message_deleted(
            session,
            source_project_id,
            coordinate.topic_uuid,
            message_uuid,
        )
    session.execute(
        """
        DELETE FROM m_workspace_message_mentions_v1
        WHERE message_uuid = %s
        """,
        (message_uuid,),
    )
    if rebind_coordinate:
        _rebind_provider_candidate_coordinate(
            session,
            coordinate.ingest_sequence,
            destination_ingest_sequence,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = %s, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
              AND ingest_sequence = %s
            """,
            (
                destination_ingest_sequence,
                source_project_id,
                message_uuid,
                coordinate.ingest_sequence,
            ),
        )
    destination_mode = project_mode(session, destination_project_id)
    destination_coordinate = MessageReadCoordinate(
        typing.cast(sys_uuid.UUID, message_uuid),
        typing.cast(sys_uuid.UUID, destination_topic_uuid),
        destination_ingest_sequence,
    )
    if destination_writes_compact:
        record_message_created(
            session,
            destination_project_id,
            destination_stream_uuid,
            destination_topic_uuid,
            destination_ingest_sequence,
        )
        for user_uuid in read_user_uuids:
            set_coordinates_read(
                session,
                destination_project_id,
                user_uuid,
                [destination_coordinate],
                True,
                coordinates_are_structurally_locked=True,
            )
    recipient_rows = session.execute(
        """
        SELECT binding.user_uuid
        FROM m_workspace_stream_bindings AS binding
        WHERE binding.project_id = %s AND binding.stream_uuid = %s
        UNION
        SELECT membership.user_uuid
        FROM m_workspace_read_memberships_v1 AS membership
        WHERE membership.project_id = %s
          AND membership.stream_uuid = %s
          AND membership.last_detached_sequence >= %s
        """,
        (
            destination_project_id,
            destination_stream_uuid,
            destination_project_id,
            destination_stream_uuid,
            destination_ingest_sequence,
        ),
    ).fetchall()
    if destination_writes_compact:
        sync_message_mentions(
            session,
            destination_project_id,
            message_uuid,
            destination_stream_uuid,
            destination_topic_uuid,
            destination_ingest_sequence,
            [row["user_uuid"] for row in recipient_rows],
            message["content"],
        )
    if destination_mode != PROJECT_MODE_COMPACT:
        session.execute(
            """
            WITH recipients AS (
                SELECT binding.user_uuid
                FROM m_workspace_stream_bindings AS binding
                WHERE binding.project_id = %s
                  AND binding.stream_uuid = %s
                UNION
                SELECT membership.user_uuid
                FROM m_workspace_read_memberships_v1 AS membership
                WHERE membership.project_id = %s
                  AND membership.stream_uuid = %s
                  AND membership.last_detached_sequence >= %s
            )
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred,
                created_at, updated_at
            )
            SELECT
                %s,
                recipient.user_uuid,
                %s,
                recipient.user_uuid = ANY(%s::uuid[]),
                FALSE,
                FALSE,
                NOW(),
                NOW()
            FROM recipients AS recipient
            ON CONFLICT (uuid, user_uuid) DO UPDATE
            SET read = EXCLUDED.read,
                project_id = EXCLUDED.project_id,
                updated_at = NOW()
            """,
            (
                destination_project_id,
                destination_stream_uuid,
                source_project_id,
                message["stream_uuid"],
                coordinate.ingest_sequence,
                message_uuid,
                destination_project_id,
                read_user_uuids,
            ),
        )


def _materialize_stream_legacy_flags(
    session: typing.Any,
    stream_uuid: object,
    source_project_id: object,
    destination_project_id: object,
    source_mode: str,
) -> None:
    compact_read_sql = (
        "COALESCE(get_bit(chunk.read_bits, "
        f"(message.ingest_sequence %% {READ_CHUNK_BITS})::integer), 0) = 1"
        if mode_uses_compact_state(source_mode)
        else "COALESCE(flags.read, FALSE)"
    )
    session.execute(
        f"""
        WITH recipients AS (
            SELECT binding.user_uuid
            FROM m_workspace_stream_bindings AS binding
            WHERE binding.project_id = %s
              AND binding.stream_uuid = %s
            UNION
            SELECT membership.user_uuid
            FROM m_workspace_read_memberships_v1 AS membership
            WHERE membership.project_id = %s
              AND membership.stream_uuid = %s
        )
        INSERT INTO m_workspace_user_message_flags AS destination_flags (
            uuid, user_uuid, project_id, read, pinned, starred,
            created_at, updated_at
        )
        SELECT
            message.uuid,
            recipient.user_uuid,
            %s,
            {compact_read_sql},
            COALESCE(flags.pinned, FALSE),
            COALESCE(flags.starred, FALSE),
            COALESCE(flags.created_at, NOW()),
            COALESCE(flags.updated_at, NOW())
        FROM m_workspace_messages AS message
        CROSS JOIN recipients AS recipient
        LEFT JOIN m_workspace_stream_bindings AS binding
          ON binding.project_id = message.project_id
         AND binding.stream_uuid = message.stream_uuid
         AND binding.user_uuid = recipient.user_uuid
        LEFT JOIN m_workspace_read_memberships_v1 AS membership
          ON membership.project_id = message.project_id
         AND membership.stream_uuid = message.stream_uuid
         AND membership.user_uuid = recipient.user_uuid
        LEFT JOIN m_workspace_user_message_flags AS flags
          ON flags.uuid = message.uuid
         AND flags.user_uuid = recipient.user_uuid
         AND flags.project_id = message.project_id
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = recipient.user_uuid
         AND chunk.chunk_number =
                message.ingest_sequence / {READ_CHUNK_BITS}
        WHERE message.project_id = %s
          AND message.stream_uuid = %s
          AND (
                binding.user_uuid IS NOT NULL
                OR message.ingest_sequence <= membership.last_detached_sequence
          )
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET project_id = EXCLUDED.project_id,
            read = EXCLUDED.read,
            pinned = EXCLUDED.pinned,
            starred = EXCLUDED.starred,
            updated_at = EXCLUDED.updated_at
        """,
        (
            source_project_id,
            stream_uuid,
            source_project_id,
            stream_uuid,
            destination_project_id,
            source_project_id,
            stream_uuid,
        ),
    )


def _rebind_stream_read_coordinates(
    session: typing.Any,
    stream_uuid: object,
    source_project_id: object,
    destination_project_id: object,
    source_mode: str,
    destination_writes_compact: bool,
) -> None:
    """Move a stream into its destination range without losing bitmap bits."""
    session.execute(
        """
        INSERT INTO m_workspace_project_ingest_ranges_v2 (
            project_id, range_number
        ) VALUES (
            %s, nextval('m_workspace_project_ingest_range_v2_seq')
        )
        ON CONFLICT (project_id) DO NOTHING
        """,
        (destination_project_id,),
    )
    destination_range = session.execute(
        """
        SELECT range_number, last_backfill_sequence
        FROM m_workspace_project_ingest_ranges_v2
        WHERE project_id = %s
        FOR UPDATE
        """,
        (destination_project_id,),
    ).fetchone()
    message_count = session.execute(
        """
        SELECT COUNT(*) AS message_count
        FROM m_workspace_messages
        WHERE project_id = %s AND stream_uuid = %s
        """,
        (source_project_id, stream_uuid),
    ).fetchone()["message_count"]
    if not message_count:
        return
    first_sequence = int(destination_range["last_backfill_sequence"]) + 1
    last_sequence = first_sequence + int(message_count) - 1
    if last_sequence > PROJECT_SEQUENCE_BACKFILL_MAX:
        raise RuntimeError("Workspace project backfill sequence is exhausted")
    range_base = int(destination_range["range_number"]) * PROJECT_SEQUENCE_RANGE_SIZE
    session.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS
            m_workspace_stream_read_coordinate_move_v1 (
                message_uuid UUID PRIMARY KEY,
                old_ingest_sequence BIGINT,
                new_ingest_sequence BIGINT UNIQUE NOT NULL
            ) ON COMMIT DROP
        """
    )
    session.execute("TRUNCATE m_workspace_stream_read_coordinate_move_v1")
    session.execute(
        """
        INSERT INTO m_workspace_stream_read_coordinate_move_v1 (
            message_uuid, old_ingest_sequence, new_ingest_sequence
        )
        SELECT
            message.uuid,
            message.ingest_sequence,
            %s + %s + ROW_NUMBER() OVER (
                ORDER BY message.created_at, message.uuid
            ) - 1
        FROM m_workspace_messages AS message
        WHERE message.project_id = %s AND message.stream_uuid = %s
        """,
        (
            range_base,
            first_sequence,
            source_project_id,
            stream_uuid,
        ),
    )
    session.execute(
        """
        UPDATE m_workspace_project_ingest_ranges_v2
        SET last_backfill_sequence = %s, updated_at = NOW()
        WHERE project_id = %s
        """,
        (last_sequence, destination_project_id),
    )

    if destination_writes_compact:
        session.execute(
            f"""
            CREATE TEMP TABLE IF NOT EXISTS
                m_workspace_stream_user_read_move_v1 (
                    user_uuid UUID NOT NULL,
                    chunk_number BIGINT NOT NULL,
                    read_bits BIT({READ_CHUNK_BITS}) NOT NULL,
                    PRIMARY KEY (user_uuid, chunk_number)
                ) ON COMMIT DROP
            """
        )
        session.execute("TRUNCATE m_workspace_stream_user_read_move_v1")
        if mode_uses_compact_state(source_mode):
            read_source_sql = f"""
                FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
                JOIN m_workspace_user_read_chunks_v1 AS source_chunk
                  ON source_chunk.chunk_number =
                        coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
                WHERE get_bit(
                    source_chunk.read_bits,
                    mod(
                        coordinate.old_ingest_sequence,
                        {READ_CHUNK_BITS}
                    )::integer
                ) = 1
            """
            user_sql = "source_chunk.user_uuid"
        else:
            read_source_sql = """
                FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
                JOIN m_workspace_user_message_flags AS source_flags
                  ON source_flags.uuid = coordinate.message_uuid
                 AND source_flags.read = TRUE
            """
            user_sql = "source_flags.user_uuid"
        session.execute(
            f"""
            INSERT INTO m_workspace_stream_user_read_move_v1 (
                user_uuid, chunk_number, read_bits
            )
            SELECT
                {user_sql},
                coordinate.new_ingest_sequence / {READ_CHUNK_BITS},
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        mod(
                            coordinate.new_ingest_sequence,
                            {READ_CHUNK_BITS}
                        )::integer,
                        1
                    )
                )
            {read_source_sql}
            GROUP BY
                {user_sql},
                coordinate.new_ingest_sequence / {READ_CHUNK_BITS}
            ON CONFLICT (user_uuid, chunk_number) DO UPDATE
            SET read_bits = (
                    m_workspace_stream_user_read_move_v1.read_bits
                    | EXCLUDED.read_bits
                )
            """
        )

    session.execute(
        f"""
        CREATE TEMP TABLE IF NOT EXISTS
            m_workspace_stream_provider_read_move_v1 (
                external_operation_uuid UUID NOT NULL,
                chunk_number BIGINT NOT NULL,
                candidate_bits BIT({READ_CHUNK_BITS}) NOT NULL,
                PRIMARY KEY (external_operation_uuid, chunk_number)
            ) ON COMMIT DROP
        """
    )
    session.execute("TRUNCATE m_workspace_stream_provider_read_move_v1")
    session.execute(
        f"""
        INSERT INTO m_workspace_stream_provider_read_move_v1 (
            external_operation_uuid, chunk_number, candidate_bits
        )
        SELECT
            candidate.external_operation_uuid,
            coordinate.new_ingest_sequence / {READ_CHUNK_BITS},
            bit_or(
                set_bit(
                    B'0'::bit({READ_CHUNK_BITS}),
                    mod(
                        coordinate.new_ingest_sequence,
                        {READ_CHUNK_BITS}
                    )::integer,
                    1
                )
            )
        FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
        JOIN m_external_provider_read_candidate_chunks_v1 AS candidate
          ON candidate.chunk_number =
                coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
         AND get_bit(
                candidate.candidate_bits,
                mod(
                    coordinate.old_ingest_sequence,
                    {READ_CHUNK_BITS}
                )::integer
             ) = 1
        JOIN m_external_provider_read_snapshots_v1 AS snapshot
          ON snapshot.external_operation_uuid = candidate.external_operation_uuid
         AND snapshot.causal_lane = %s
        GROUP BY
            candidate.external_operation_uuid,
            coordinate.new_ingest_sequence / {READ_CHUNK_BITS}
        ON CONFLICT (external_operation_uuid, chunk_number) DO UPDATE
        SET candidate_bits = (
                m_workspace_stream_provider_read_move_v1.candidate_bits
                | EXCLUDED.candidate_bits
            )
        """,
        (stream_uuid,),
    )
    session.execute(
        f"""
        CREATE TEMP TABLE IF NOT EXISTS
            m_workspace_stream_provider_read_mask_v1 (
                external_operation_uuid UUID NOT NULL,
                chunk_number BIGINT NOT NULL,
                moved_bits BIT({READ_CHUNK_BITS}) NOT NULL,
                PRIMARY KEY (external_operation_uuid, chunk_number)
            ) ON COMMIT DROP
        """
    )
    session.execute("TRUNCATE m_workspace_stream_provider_read_mask_v1")
    session.execute(
        f"""
        INSERT INTO m_workspace_stream_provider_read_mask_v1 (
            external_operation_uuid, chunk_number, moved_bits
        )
        SELECT
            candidate.external_operation_uuid,
            coordinate.old_ingest_sequence / {READ_CHUNK_BITS},
            bit_or(
                set_bit(
                    B'0'::bit({READ_CHUNK_BITS}),
                    mod(
                        coordinate.old_ingest_sequence,
                        {READ_CHUNK_BITS}
                    )::integer,
                    1
                )
            )
        FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
        JOIN m_external_provider_read_candidate_chunks_v1 AS candidate
          ON candidate.chunk_number =
                coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
         AND get_bit(
                candidate.candidate_bits,
                mod(
                    coordinate.old_ingest_sequence,
                    {READ_CHUNK_BITS}
                )::integer
             ) = 1
        JOIN m_external_provider_read_snapshots_v1 AS snapshot
          ON snapshot.external_operation_uuid = candidate.external_operation_uuid
         AND snapshot.causal_lane = %s
        WHERE coordinate.old_ingest_sequence IS NOT NULL
        GROUP BY
            candidate.external_operation_uuid,
            coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
        """,
        (stream_uuid,),
    )
    session.execute(
        """
        DELETE FROM m_external_provider_read_candidate_chunks_v1 AS candidate
        USING m_workspace_stream_provider_read_mask_v1 AS mask
        WHERE candidate.external_operation_uuid = mask.external_operation_uuid
          AND candidate.chunk_number = mask.chunk_number
          AND bit_count(candidate.candidate_bits & ~mask.moved_bits) = 0
        """
    )
    session.execute(
        """
        UPDATE m_external_provider_read_candidate_chunks_v1 AS candidate
        SET candidate_bits = candidate.candidate_bits & ~mask.moved_bits
        FROM m_workspace_stream_provider_read_mask_v1 AS mask
        WHERE candidate.external_operation_uuid =
                mask.external_operation_uuid
          AND candidate.chunk_number = mask.chunk_number
        """
    )

    session.execute(
        f"""
        WITH masks AS (
            SELECT
                coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
                    AS chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        mod(
                            coordinate.old_ingest_sequence,
                            {READ_CHUNK_BITS}
                        )::integer,
                        1
                    )
                ) AS moved_bits
            FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
            WHERE coordinate.old_ingest_sequence IS NOT NULL
            GROUP BY coordinate.old_ingest_sequence / {READ_CHUNK_BITS}
        )
        UPDATE m_workspace_user_read_chunks_v1 AS chunk
        SET read_bits = chunk.read_bits & ~masks.moved_bits,
            updated_at = NOW()
        FROM masks
        WHERE chunk.chunk_number = masks.chunk_number
        """
    )
    session.execute(
        f"""
        DELETE FROM m_workspace_user_read_chunks_v1 AS chunk
        WHERE chunk.read_bits = B'0'::bit({READ_CHUNK_BITS})
          AND EXISTS (
                SELECT 1
                FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
                WHERE coordinate.old_ingest_sequence / {READ_CHUNK_BITS} =
                        chunk.chunk_number
          )
        """
    )
    if destination_writes_compact:
        session.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits, created_at, updated_at
            )
            SELECT user_uuid, chunk_number, read_bits, NOW(), NOW()
            FROM m_workspace_stream_user_read_move_v1
            ON CONFLICT (user_uuid, chunk_number) DO UPDATE
            SET read_bits = (
                    m_workspace_user_read_chunks_v1.read_bits
                    | EXCLUDED.read_bits
                ),
                updated_at = NOW()
            """
        )
    session.execute(
        """
        INSERT INTO m_external_provider_read_candidate_chunks_v1 (
            external_operation_uuid, chunk_number, candidate_bits
        )
        SELECT external_operation_uuid, chunk_number, candidate_bits
        FROM m_workspace_stream_provider_read_move_v1
        ON CONFLICT (external_operation_uuid, chunk_number) DO UPDATE
        SET candidate_bits = (
                m_external_provider_read_candidate_chunks_v1.candidate_bits
                | EXCLUDED.candidate_bits
            )
        """
    )
    session.execute(
        """
        UPDATE m_workspace_read_memberships_v1 AS membership
        SET last_detached_sequence = COALESCE(
                (
                    SELECT MAX(coordinate.new_ingest_sequence)
                    FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
                    WHERE coordinate.old_ingest_sequence
                            <= membership.last_detached_sequence
                ),
                0
            ),
            updated_at = NOW()
        WHERE membership.project_id = %s
          AND membership.stream_uuid = %s
        """,
        (source_project_id, stream_uuid),
    )
    session.execute(
        """
        UPDATE m_workspace_messages AS message
        SET ingest_sequence = coordinate.new_ingest_sequence
        FROM m_workspace_stream_read_coordinate_move_v1 AS coordinate
        WHERE message.uuid = coordinate.message_uuid
          AND message.project_id = %s
        """,
        (source_project_id,),
    )


def relocate_stream_project(
    session: typing.Any,
    stream_uuid: object,
    source_project_id: object,
    destination_project_id: object,
) -> None:
    """Move compact projections before the canonical stream changes project."""
    if source_project_id == destination_project_id:
        return
    lock_message_structure(
        session,
        (source_project_id, destination_project_id),
        cross_project=True,
    )
    lock_projects(session, (source_project_id, destination_project_id))
    ensure_new_project(session, destination_project_id)
    bump_project_structure_revisions(
        session,
        (source_project_id, destination_project_id),
    )
    source_mode = project_mode(session, source_project_id)
    destination_mode = project_mode(session, destination_project_id)
    destination_writes_compact = writes_compact_state(
        session,
        destination_project_id,
    )

    if destination_writes_compact:
        if not mode_uses_compact_state(source_mode):
            # Pure legacy storage has no explicit detach boundary. Its dense
            # flag rows are the recipient snapshot, so preserve their latest
            # message sequence before the stream enters compact storage.
            session.execute(
                """
                INSERT INTO m_workspace_read_memberships_v1 (
                    project_id, user_uuid, stream_uuid,
                    last_detached_sequence, created_at, updated_at
                )
                SELECT
                    %s,
                    flags.user_uuid,
                    %s,
                    MAX(message.ingest_sequence),
                    NOW(),
                    NOW()
                FROM m_workspace_user_message_flags AS flags
                JOIN m_workspace_messages AS message
                  ON message.uuid = flags.uuid
                 AND message.project_id = flags.project_id
                LEFT JOIN m_workspace_stream_bindings AS binding
                  ON binding.project_id = flags.project_id
                 AND binding.stream_uuid = message.stream_uuid
                 AND binding.user_uuid = flags.user_uuid
                LEFT JOIN m_workspace_read_memberships_v1 AS membership
                  ON membership.project_id = flags.project_id
                 AND membership.stream_uuid = message.stream_uuid
                 AND membership.user_uuid = flags.user_uuid
                WHERE flags.project_id = %s
                  AND message.stream_uuid = %s
                  AND binding.user_uuid IS NULL
                  AND membership.user_uuid IS NULL
                GROUP BY flags.user_uuid
                ON CONFLICT (project_id, user_uuid, stream_uuid) DO NOTHING
                """,
                (
                    source_project_id,
                    stream_uuid,
                    source_project_id,
                    stream_uuid,
                ),
            )
            session.execute(
                """
                INSERT INTO m_workspace_user_topic_read_stats_v1 (
                    project_id, user_uuid, topic_uuid, read_count,
                    created_at, updated_at
                )
                SELECT
                    %s,
                    flags.user_uuid,
                    message.topic_uuid,
                    COUNT(*),
                    NOW(),
                    NOW()
                FROM m_workspace_user_message_flags AS flags
                JOIN m_workspace_messages AS message
                  ON message.uuid = flags.uuid
                 AND message.project_id = flags.project_id
                WHERE flags.project_id = %s
                  AND message.stream_uuid = %s
                  AND flags.read = TRUE
                GROUP BY flags.user_uuid, message.topic_uuid
                ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
                SET read_count = EXCLUDED.read_count, updated_at = NOW()
                """,
                (destination_project_id, source_project_id, stream_uuid),
            )
            session.execute(
                """
                DELETE FROM m_workspace_user_topic_read_stats_v1
                WHERE project_id = %s
                  AND topic_uuid IN (
                        SELECT uuid
                        FROM m_workspace_stream_topics
                        WHERE project_id = %s AND stream_uuid = %s
                  )
                """,
                (source_project_id, source_project_id, stream_uuid),
            )

    if destination_mode != PROJECT_MODE_COMPACT:
        _materialize_stream_legacy_flags(
            session,
            stream_uuid,
            source_project_id,
            destination_project_id,
            source_mode,
        )
    _rebind_stream_read_coordinates(
        session,
        stream_uuid,
        source_project_id,
        destination_project_id,
        source_mode,
        destination_writes_compact,
    )

    if destination_writes_compact:
        session.execute(
            """
            DELETE FROM m_workspace_message_mentions_v1 AS mention
            USING m_workspace_messages AS message
            WHERE mention.message_uuid = message.uuid
              AND message.project_id = %s
              AND message.stream_uuid = %s
            """,
            (source_project_id, stream_uuid),
        )
        session.execute(
            """
            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence, created_at, updated_at
            )
            SELECT
                message.topic_uuid,
                %s,
                message.stream_uuid,
                COUNT(*),
                MAX(message.ingest_sequence),
                NOW(),
                NOW()
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s AND message.stream_uuid = %s
            GROUP BY message.topic_uuid, message.stream_uuid
            ON CONFLICT (topic_uuid) DO UPDATE
            SET project_id = EXCLUDED.project_id,
                stream_uuid = EXCLUDED.stream_uuid,
                message_count = EXCLUDED.message_count,
                last_ingest_sequence = EXCLUDED.last_ingest_sequence,
                updated_at = NOW()
            """,
            (destination_project_id, source_project_id, stream_uuid),
        )
        session.execute(
            """
            INSERT INTO m_workspace_message_mentions_v1 (
                message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
                ingest_sequence, created_at
            )
            SELECT
                message.uuid,
                recipient.user_uuid,
                %s,
                message.stream_uuid,
                message.topic_uuid,
                message.ingest_sequence,
                NOW()
            FROM m_workspace_messages AS message
            CROSS JOIN LATERAL regexp_matches(
                LOWER(COALESCE(message.payload->>'content', '')),
                '][(]urn:user:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[)]',
                'g'
            ) AS matched(value)
            JOIN LATERAL (
                SELECT binding.user_uuid
                FROM m_workspace_stream_bindings AS binding
                WHERE binding.project_id = message.project_id
                  AND binding.stream_uuid = message.stream_uuid
                  AND binding.user_uuid = (matched.value)[1]::uuid
                UNION
                SELECT membership.user_uuid
                FROM m_workspace_read_memberships_v1 AS membership
                WHERE membership.project_id = message.project_id
                  AND membership.stream_uuid = message.stream_uuid
                  AND membership.user_uuid = (matched.value)[1]::uuid
                  AND message.ingest_sequence
                      <= membership.last_detached_sequence
            ) AS recipient ON TRUE
            WHERE message.project_id = %s AND message.stream_uuid = %s
            ON CONFLICT (message_uuid, user_uuid) DO UPDATE
            SET project_id = EXCLUDED.project_id,
                stream_uuid = EXCLUDED.stream_uuid,
                topic_uuid = EXCLUDED.topic_uuid,
                ingest_sequence = EXCLUDED.ingest_sequence
            """,
            (destination_project_id, source_project_id, stream_uuid),
        )

    # Every legacy row belongs to the moved message even if its user detached
    # before membership boundaries were materialized.  Move the remaining
    # rows verbatim so a later re-add cannot collide with stale source state.
    session.execute(
        """
        UPDATE m_workspace_user_message_flags AS flags
        SET project_id = %s
        FROM m_workspace_messages AS message
        WHERE flags.project_id = %s
          AND flags.uuid = message.uuid
          AND message.project_id = %s
          AND message.stream_uuid = %s
        """,
        (
            destination_project_id,
            source_project_id,
            source_project_id,
            stream_uuid,
        ),
    )

    session.execute(
        """
        UPDATE m_workspace_read_memberships_v1
        SET project_id = %s, updated_at = NOW()
        WHERE project_id = %s AND stream_uuid = %s
        """,
        (destination_project_id, source_project_id, stream_uuid),
    )
    if destination_writes_compact:
        session.execute(
            """
            UPDATE m_workspace_user_topic_read_stats_v1
            SET project_id = %s, updated_at = NOW()
            WHERE project_id = %s
              AND topic_uuid IN (
                    SELECT uuid
                    FROM m_workspace_stream_topics
                    WHERE project_id = %s AND stream_uuid = %s
              )
            """,
            (
                destination_project_id,
                source_project_id,
                source_project_id,
                stream_uuid,
            ),
        )
        session.execute(
            """
            UPDATE m_workspace_message_mentions_v1
            SET project_id = %s
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (destination_project_id, source_project_id, stream_uuid),
        )
        session.execute(
            """
            UPDATE m_workspace_topic_message_stats_v1
            SET project_id = %s, updated_at = NOW()
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (destination_project_id, source_project_id, stream_uuid),
        )
    else:
        clear_stream_for_all_users(session, source_project_id, stream_uuid)
        session.execute(
            """
            DELETE FROM m_workspace_user_topic_read_stats_v1 AS stats
            USING m_workspace_stream_topics AS topic
            WHERE stats.topic_uuid = topic.uuid
              AND topic.project_id = %s
              AND topic.stream_uuid = %s
            """,
            (source_project_id, stream_uuid),
        )
        session.execute(
            """
            DELETE FROM m_workspace_message_mentions_v1 AS mention
            USING m_workspace_messages AS message
            WHERE mention.message_uuid = message.uuid
              AND message.project_id = %s
              AND message.stream_uuid = %s
            """,
            (source_project_id, stream_uuid),
        )
        session.execute(
            """
            DELETE FROM m_workspace_topic_message_stats_v1 AS stats
            USING m_workspace_stream_topics AS topic
            WHERE stats.topic_uuid = topic.uuid
              AND topic.project_id = %s
              AND topic.stream_uuid = %s
            """,
            (source_project_id, stream_uuid),
        )


def merge_topics(
    session: typing.Any,
    project_id: object,
    source_topic_uuids: collections.abc.Iterable[object],
    destination_stream_uuid: object,
    destination_topic_uuid: object,
) -> None:
    lock_message_structure(session, (project_id,))
    lock_projects(session, (project_id,))
    bump_project_structure_revisions(session, (project_id,))
    for source_topic_uuid in sorted(set(source_topic_uuids), key=str):
        if source_topic_uuid == destination_topic_uuid:
            continue
        session.execute(
            """
            INSERT INTO m_workspace_user_topic_read_stats_v1 (
                project_id, user_uuid, topic_uuid, read_count,
                created_at, updated_at
            )
            SELECT project_id, user_uuid, %s, read_count,
                   created_at, updated_at
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE
            SET read_count = (
                    m_workspace_user_topic_read_stats_v1.read_count
                    + EXCLUDED.read_count
                ),
                updated_at = NOW()
            """,
            (destination_topic_uuid, project_id, source_topic_uuid),
        )
        session.execute(
            """
            DELETE FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (project_id, source_topic_uuid),
        )
        session.execute(
            """
            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence, created_at, updated_at
            )
            SELECT %s, project_id, %s, message_count,
                   last_ingest_sequence, created_at, updated_at
            FROM m_workspace_topic_message_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            ON CONFLICT (topic_uuid) DO UPDATE
            SET project_id = EXCLUDED.project_id,
                stream_uuid = EXCLUDED.stream_uuid,
                message_count = (
                    m_workspace_topic_message_stats_v1.message_count
                    + EXCLUDED.message_count
                ),
                last_ingest_sequence = GREATEST(
                    m_workspace_topic_message_stats_v1.last_ingest_sequence,
                    EXCLUDED.last_ingest_sequence
                ),
                updated_at = NOW()
            """,
            (
                destination_topic_uuid,
                destination_stream_uuid,
                project_id,
                source_topic_uuid,
            ),
        )
        session.execute(
            """
            DELETE FROM m_workspace_topic_message_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (project_id, source_topic_uuid),
        )
        session.execute(
            """
            UPDATE m_workspace_message_mentions_v1
            SET stream_uuid = %s, topic_uuid = %s
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (
                destination_stream_uuid,
                destination_topic_uuid,
                project_id,
                source_topic_uuid,
            ),
        )


def _selected_coordinates(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
) -> list[collections.abc.Mapping[str, typing.Any]]:
    return session.execute(
        f"""
        SELECT
            message.uuid,
            message.stream_uuid,
            message.topic_uuid,
            message.created_at,
            message.ingest_sequence
        FROM m_workspace_messages AS message
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = %s
         AND chunk.chunk_number = message.ingest_sequence / {READ_CHUNK_BITS}
        WHERE message.project_id = %s
          AND {where_sql}
          AND COALESCE(
                get_bit(
                    chunk.read_bits,
                    (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                ),
                0
              ) = 0
        ORDER BY message.created_at, message.uuid
        """,
        (user_uuid, project_id, *values),
    ).fetchall()


def _mark_legacy_scope_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
    message_uuid_snapshot_callback: BulkReadSnapshotCallback | None = None,
) -> list[sys_uuid.UUID]:
    if message_uuid_snapshot_callback is not None:
        candidate_sql = f"""
            SELECT message.uuid, message.created_at, message.ingest_sequence
            FROM m_workspace_user_message_flags AS flags
            JOIN m_workspace_messages AS message
              ON message.project_id = flags.project_id
             AND message.uuid = flags.uuid
            WHERE flags.project_id = %s
              AND flags.user_uuid = %s
              AND flags.read = FALSE
              AND {where_sql}
            ORDER BY message.created_at, message.uuid
        """
        message_uuid_snapshot_callback(
            session,
            candidate_sql,
            (project_id, user_uuid, *values),
        )
    rows = session.execute(
        f"""
        WITH changed AS (
            UPDATE m_workspace_user_message_flags AS flags
            SET read = TRUE, updated_at = NOW()
            FROM m_workspace_messages AS message
            WHERE flags.project_id = %s
              AND flags.user_uuid = %s
              AND flags.uuid = message.uuid
              AND message.project_id = flags.project_id
              AND {where_sql}
              AND flags.read = FALSE
            RETURNING message.topic_uuid
        )
        SELECT DISTINCT topic_uuid
        FROM changed
        ORDER BY topic_uuid
        """,
        (project_id, user_uuid, *values),
    ).fetchall()
    return [row["topic_uuid"] for row in rows]


def _sync_rollback_scope_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
) -> None:
    session.execute(
        f"""
        INSERT INTO m_workspace_user_message_flags AS flags (
            uuid, user_uuid, project_id, read, pinned, starred,
            created_at, updated_at
        )
        SELECT
            message.uuid, %s, %s, TRUE, FALSE, FALSE, NOW(), NOW()
        FROM m_workspace_messages AS message
        WHERE message.project_id = %s
          AND {where_sql}
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET read = TRUE, updated_at = NOW()
        WHERE flags.project_id = EXCLUDED.project_id
          AND flags.read IS DISTINCT FROM TRUE
        """,
        (user_uuid, project_id, project_id, *values),
    )


def _bulk_mark_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
    message_uuid_snapshot_callback: BulkReadSnapshotCallback | None = None,
) -> list[sys_uuid.UUID]:
    # Compact bulk reads intentionally aggregate before the project lock so a
    # 500k-message mark-all does not serialize every user scan.  Structural
    # writers bump a project epoch while holding that project lock.  If the
    # optimistic scan raced a move/delete we rebuild once under the lock;
    # otherwise unrelated provider traffic never waits for the large scan.
    initial_mode = project_mode(session, project_id)
    initial_structure_revision = (
        project_structure_revision(session, project_id)
        if initial_mode == PROJECT_MODE_COMPACT
        else None
    )
    initial_user_read_revision = (
        user_read_revision(session, project_id, user_uuid)
        if initial_mode == PROJECT_MODE_COMPACT
        else None
    )

    def prepare() -> collections.abc.Mapping[str, typing.Any]:
        return session.execute(
            f"""
        WITH unread AS MATERIALIZED (
            SELECT
                message.topic_uuid,
                message.ingest_sequence / {READ_CHUNK_BITS} AS chunk_number,
                (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                    AS bit_offset
        FROM m_workspace_messages AS message
        LEFT JOIN m_workspace_user_read_chunks_v1 AS current_chunk
          ON current_chunk.user_uuid = %s
         AND current_chunk.chunk_number =
                message.ingest_sequence / {READ_CHUNK_BITS}
        WHERE message.project_id = %s
          AND {where_sql}
          AND COALESCE(
                get_bit(
                    current_chunk.read_bits,
                    (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                ),
                0
              ) = 0
        ), masks AS (
            SELECT
                unread.chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        unread.bit_offset,
                        1
                    )
                ) AS read_bits
            FROM unread
            GROUP BY unread.chunk_number
        ), topics AS (
            SELECT unread.topic_uuid, COUNT(*)::bigint AS read_delta
            FROM unread
            GROUP BY unread.topic_uuid
        )
        SELECT
            COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'chunk_number', masks.chunk_number,
                            'read_bits', masks.read_bits::text
                        )
                        ORDER BY masks.chunk_number
                    )
                    FROM masks
                ),
                '[]'::jsonb
            ) AS masks,
            COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'topic_uuid', topics.topic_uuid,
                            'read_delta', topics.read_delta
                        )
                        ORDER BY topics.topic_uuid
                    )
                    FROM topics
                ),
                '[]'::jsonb
            ) AS topics
            """,
            (user_uuid, project_id, *values),
        ).fetchone()

    if initial_mode == PROJECT_MODE_COMPACT:
        prepared = prepare()
        if prepared["topics"]:
            lock_projects(session, (project_id,))
    else:
        lock_projects(session, (project_id,))
        prepared = prepare()
    topic_rows = prepared["topics"] or []
    if not topic_rows:
        return []
    mask_rows = prepared["masks"] or []
    topic_uuids = [sys_uuid.UUID(str(row["topic_uuid"])) for row in topic_rows]
    mode = project_mode(session, project_id)
    if mode == PROJECT_MODE_LEGACY:
        return _mark_legacy_scope_read(
            session,
            project_id,
            user_uuid,
            where_sql,
            values,
            message_uuid_snapshot_callback,
        )
    structure_changed = initial_mode == PROJECT_MODE_COMPACT and (
        project_structure_revision(session, project_id) != initial_structure_revision
        or user_read_revision(session, project_id, user_uuid)
        != initial_user_read_revision
    )
    if initial_mode == PROJECT_MODE_COMPACT and (
        mode == PROJECT_MODE_ROLLBACK or structure_changed
    ):
        # An authority transition or structural mutation makes the optimistic
        # masks stale. Rebuild under the project lock so bitmap and rollback
        # shadow receive the exact same surviving scope.
        prepared = prepare()
        topic_rows = prepared["topics"] or []
        mask_rows = prepared["masks"] or []
        topic_uuids = [sys_uuid.UUID(str(row["topic_uuid"])) for row in topic_rows]
        if not topic_uuids:
            return []
    if message_uuid_snapshot_callback is not None:
        candidate_sql = f"""
            SELECT message.uuid, message.created_at, message.ingest_sequence
            FROM m_workspace_messages AS message
            JOIN unnest(%s::bigint[], %s::text[])
                AS snapshot(chunk_number, read_bits)
              ON snapshot.chunk_number =
                    message.ingest_sequence / {READ_CHUNK_BITS}
             AND get_bit(
                    snapshot.read_bits::bit({READ_CHUNK_BITS}),
                    (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                 ) = 1
            LEFT JOIN m_workspace_user_read_chunks_v1 AS current_chunk
              ON current_chunk.user_uuid = %s
             AND current_chunk.chunk_number =
                    message.ingest_sequence / {READ_CHUNK_BITS}
            WHERE message.project_id = %s
              AND {where_sql}
              AND COALESCE(
                    get_bit(
                        current_chunk.read_bits,
                        (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                    ),
                    0
                  ) = 0
            ORDER BY message.created_at, message.uuid
        """
        message_uuid_snapshot_callback(
            session,
            candidate_sql,
            (
                [row["chunk_number"] for row in mask_rows],
                [row["read_bits"] for row in mask_rows],
                user_uuid,
                project_id,
                *values,
            ),
            mask_rows if mode == PROJECT_MODE_COMPACT else None,
        )
    session.execute(
        f"""
        INSERT INTO m_workspace_user_read_chunks_v1 (
            user_uuid, chunk_number, read_bits, created_at, updated_at
        )
        SELECT
            %s,
            prepared.chunk_number,
            prepared.read_bits::bit({READ_CHUNK_BITS}),
            NOW(),
            NOW()
        FROM unnest(%s::bigint[], %s::text[])
            AS prepared(chunk_number, read_bits)
        ON CONFLICT (user_uuid, chunk_number)
        DO UPDATE SET
            read_bits = (
                m_workspace_user_read_chunks_v1.read_bits
                | EXCLUDED.read_bits
            ),
            updated_at = NOW()
        """,
        (
            user_uuid,
            [row["chunk_number"] for row in mask_rows],
            [row["read_bits"] for row in mask_rows],
        ),
    )
    _adjust_topic_read_stats(
        session,
        project_id,
        [
            {
                "user_uuid": user_uuid,
                "topic_uuid": row["topic_uuid"],
                "read_delta": row["read_delta"],
            }
            for row in topic_rows
        ],
    )
    if mode == PROJECT_MODE_ROLLBACK:
        _sync_rollback_scope_read(
            session,
            project_id,
            user_uuid,
            where_sql,
            values,
        )
    bump_user_read_revision(session, project_id, user_uuid)
    return topic_uuids


def _bounded_bulk_mark_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
    message_uuid_batch_callback: typing.Callable[
        [collections.abc.Sequence[sys_uuid.UUID]], None
    ],
    *,
    batch_size: int,
) -> list[sys_uuid.UUID]:
    """Mark one stable scope read without materializing all UUIDs in Python."""
    lock_projects(session, (project_id,))
    boundary = session.execute(
        f"""
        SELECT MAX(message.ingest_sequence) AS ingest_sequence
        FROM m_workspace_messages AS message
        WHERE message.project_id = %s AND {where_sql}
        """,
        (project_id, *values),
    ).fetchone()
    if boundary is None or boundary["ingest_sequence"] is None:
        return []
    boundary_sequence = int(boundary["ingest_sequence"])
    last_created_at = None
    last_uuid = None
    topic_uuids: set[sys_uuid.UUID] = set()
    while True:
        cursor_sql = ""
        cursor_values: tuple[object, ...] = ()
        if last_created_at is not None:
            cursor_sql = "AND (message.created_at, message.uuid) > (%s, %s)"
            cursor_values = (last_created_at, last_uuid)
        rows = session.execute(
            f"""
            SELECT message.uuid, message.topic_uuid, message.ingest_sequence,
                   message.created_at
            FROM m_workspace_messages AS message
            LEFT JOIN m_workspace_user_read_chunks_v1 AS current_chunk
              ON current_chunk.user_uuid = %s
             AND current_chunk.chunk_number =
                    message.ingest_sequence / {READ_CHUNK_BITS}
            WHERE message.project_id = %s
              AND {where_sql}
              AND message.ingest_sequence <= %s
              {cursor_sql}
              AND COALESCE(
                    get_bit(
                        current_chunk.read_bits,
                        (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                    ),
                    0
                  ) = 0
            ORDER BY message.created_at, message.uuid
            LIMIT %s
            """,
            (
                user_uuid,
                project_id,
                *values,
                boundary_sequence,
                *cursor_values,
                batch_size,
            ),
        ).fetchall()
        if not rows:
            break
        coordinates = [
            MessageReadCoordinate(
                sys_uuid.UUID(str(row["uuid"])),
                sys_uuid.UUID(str(row["topic_uuid"])),
                int(row["ingest_sequence"]),
            )
            for row in rows
        ]
        set_coordinates_read(
            session,
            project_id,
            user_uuid,
            coordinates,
            True,
            coordinates_are_structurally_locked=True,
        )
        message_uuid_batch_callback([coordinate.uuid for coordinate in coordinates])
        topic_uuids.update(coordinate.topic_uuid for coordinate in coordinates)
        last_created_at = rows[-1]["created_at"]
        last_uuid = rows[-1]["uuid"]
    return sorted(topic_uuids, key=str)


def read_stream(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
    collect_message_rows: bool = True,
    message_uuid_batch_callback: typing.Callable[
        [collections.abc.Sequence[sys_uuid.UUID]], None
    ]
    | None = None,
    message_uuid_snapshot_callback: BulkReadSnapshotCallback | None = None,
    batch_size: int = 500,
) -> BulkReadResult:
    if not collect_message_rows:
        topic_uuids = (
            _bulk_mark_read(
                session,
                project_id,
                user_uuid,
                "message.stream_uuid = %s",
                (stream_uuid,),
                message_uuid_snapshot_callback,
            )
            if message_uuid_batch_callback is None
            else _bounded_bulk_mark_read(
                session,
                project_id,
                user_uuid,
                "message.stream_uuid = %s",
                (stream_uuid,),
                message_uuid_batch_callback,
                batch_size=batch_size,
            )
        )
        return BulkReadResult([], topic_uuids)
    rows = _selected_coordinates(
        session,
        project_id,
        user_uuid,
        "message.stream_uuid = %s",
        (stream_uuid,),
    )
    coordinates = [
        MessageReadCoordinate(row["uuid"], row["topic_uuid"], row["ingest_sequence"])
        for row in rows
    ]
    set_coordinates_read(session, project_id, user_uuid, coordinates, True)
    return BulkReadResult(
        rows,
        list(dict.fromkeys(row["topic_uuid"] for row in rows)),
    )


def read_topic(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    collect_message_rows: bool = True,
    message_uuid_batch_callback: typing.Callable[
        [collections.abc.Sequence[sys_uuid.UUID]], None
    ]
    | None = None,
    message_uuid_snapshot_callback: BulkReadSnapshotCallback | None = None,
    batch_size: int = 500,
) -> BulkReadResult:
    if not collect_message_rows:
        topic_uuids = (
            _bulk_mark_read(
                session,
                project_id,
                user_uuid,
                "message.stream_uuid = %s AND message.topic_uuid = %s",
                (stream_uuid, topic_uuid),
                message_uuid_snapshot_callback,
            )
            if message_uuid_batch_callback is None
            else _bounded_bulk_mark_read(
                session,
                project_id,
                user_uuid,
                "message.stream_uuid = %s AND message.topic_uuid = %s",
                (stream_uuid, topic_uuid),
                message_uuid_batch_callback,
                batch_size=batch_size,
            )
        )
        return BulkReadResult([], topic_uuids)
    rows = _selected_coordinates(
        session,
        project_id,
        user_uuid,
        "message.stream_uuid = %s AND message.topic_uuid = %s",
        (stream_uuid, topic_uuid),
    )
    coordinates = [
        MessageReadCoordinate(row["uuid"], row["topic_uuid"], row["ingest_sequence"])
        for row in rows
    ]
    set_coordinates_read(session, project_id, user_uuid, coordinates, True)
    return BulkReadResult(
        rows,
        [typing.cast(sys_uuid.UUID, topic_uuid)] if rows else [],
    )


def read_topic_to_boundary(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    boundary_created_at: object,
    boundary_uuid: object,
    collect_message_rows: bool = True,
    message_uuid_batch_callback: typing.Callable[
        [collections.abc.Sequence[sys_uuid.UUID]], None
    ]
    | None = None,
    message_uuid_snapshot_callback: BulkReadSnapshotCallback | None = None,
    batch_size: int = 500,
) -> BulkReadResult:
    where_sql = """
        message.stream_uuid = %s
        AND message.topic_uuid = %s
        AND (message.created_at, message.uuid) <= (%s, %s)
    """
    values = (
        stream_uuid,
        topic_uuid,
        boundary_created_at,
        boundary_uuid,
    )
    if not collect_message_rows:
        topic_uuids = (
            _bulk_mark_read(
                session,
                project_id,
                user_uuid,
                where_sql,
                values,
                message_uuid_snapshot_callback,
            )
            if message_uuid_batch_callback is None
            else _bounded_bulk_mark_read(
                session,
                project_id,
                user_uuid,
                where_sql,
                values,
                message_uuid_batch_callback,
                batch_size=batch_size,
            )
        )
        return BulkReadResult([], topic_uuids)
    rows = _selected_coordinates(
        session,
        project_id,
        user_uuid,
        where_sql,
        values,
    )
    coordinates = [
        MessageReadCoordinate(row["uuid"], row["topic_uuid"], row["ingest_sequence"])
        for row in rows
    ]
    set_coordinates_read(session, project_id, user_uuid, coordinates, True)
    return BulkReadResult(
        rows,
        [typing.cast(sys_uuid.UUID, topic_uuid)] if rows else [],
    )


def mark_stream_history_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
) -> None:
    membership = session.execute(
        """
        SELECT last_detached_sequence
        FROM m_workspace_read_memberships_v1
        WHERE project_id = %s AND user_uuid = %s AND stream_uuid = %s
        FOR UPDATE
        """,
        (project_id, user_uuid, stream_uuid),
    ).fetchone()
    first_sequence = (
        0
        if membership is None or membership["last_detached_sequence"] is None
        else membership["last_detached_sequence"]
    )
    _bulk_mark_read(
        session,
        project_id,
        user_uuid,
        "message.stream_uuid = %s AND message.ingest_sequence > %s",
        (stream_uuid, first_sequence),
    )
    session.execute(
        """
        INSERT INTO m_workspace_read_memberships_v1 (
            project_id, user_uuid, stream_uuid, last_detached_sequence,
            created_at, updated_at
        ) VALUES (%s, %s, %s, NULL, NOW(), NOW())
        ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
        SET last_detached_sequence = NULL, updated_at = NOW()
        """,
        (project_id, user_uuid, stream_uuid),
    )


def record_stream_detached(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
) -> None:
    row = session.execute(
        """
        SELECT MAX(ingest_sequence) AS last_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND stream_uuid = %s
        """,
        (project_id, stream_uuid),
    ).fetchone()
    session.execute(
        """
        INSERT INTO m_workspace_read_memberships_v1 (
            project_id, user_uuid, stream_uuid, last_detached_sequence,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
        SET last_detached_sequence = EXCLUDED.last_detached_sequence,
            updated_at = NOW()
        """,
        (project_id, user_uuid, stream_uuid, row["last_sequence"] or 0),
    )


def sync_stream_mentions_for_user(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    stream_uuid: object,
) -> None:
    session.execute(
        """
        INSERT INTO m_workspace_message_mentions_v1 (
            message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
            ingest_sequence, created_at
        )
        SELECT
            message.uuid, %s, message.project_id, message.stream_uuid,
            message.topic_uuid, message.ingest_sequence, NOW()
        FROM m_workspace_messages AS message
        WHERE message.project_id = %s
          AND message.stream_uuid = %s
          AND POSITION(
                '](' || 'urn:user:' || LOWER(%s::uuid::text) || ')'
                IN LOWER(COALESCE(message.payload->>'content', ''))
              ) > 0
        ON CONFLICT (message_uuid, user_uuid) DO NOTHING
        """,
        (user_uuid, project_id, stream_uuid, user_uuid),
    )


def clear_message_for_all_users(
    session: typing.Any,
    project_id: object,
    message_uuid: object,
) -> None:
    lock_message_structure(session, (project_id,))
    lock_projects(session, (project_id,))
    bump_project_structure_revisions(session, (project_id,))
    coordinate = message_coordinate(session, project_id, message_uuid)
    if coordinate is None:
        return
    chunk_number, offset = _coordinate_parts(coordinate)
    changed_users = session.execute(
        """
        UPDATE m_workspace_user_read_chunks_v1
        SET read_bits = set_bit(read_bits, %s, 0), updated_at = NOW()
        WHERE chunk_number = %s
          AND get_bit(read_bits, %s) = 1
        RETURNING user_uuid
        """,
        (offset, chunk_number, offset),
    ).fetchall()
    session.execute(
        """
        DELETE FROM m_workspace_user_read_chunks_v1
        WHERE chunk_number = %s
          AND bit_count(read_bits) = 0
        """,
        (chunk_number,),
    )
    _adjust_topic_read_stats(
        session,
        project_id,
        [
            {
                "user_uuid": row["user_uuid"],
                "topic_uuid": coordinate.topic_uuid,
                "read_delta": -1,
            }
            for row in changed_users
        ],
    )


def _clear_message_scope_for_all_users(
    session: typing.Any,
    project_id: object,
    where_sql: str,
    values: collections.abc.Sequence[object],
) -> None:
    lock_message_structure(session, (project_id,))
    lock_projects(session, (project_id,))
    bump_project_structure_revisions(session, (project_id,))
    session.execute(
        f"""
        WITH doomed AS MATERIALIZED (
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND {where_sql}
        ), masks AS (
            SELECT
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        (ingest_sequence %% {READ_CHUNK_BITS})::integer,
                        1
                    )
                ) AS covered_bits
            FROM doomed
            GROUP BY ingest_sequence / {READ_CHUNK_BITS}
        )
        UPDATE m_workspace_user_read_chunks_v1 AS chunk
        SET read_bits = chunk.read_bits & ~masks.covered_bits,
            updated_at = NOW()
        FROM masks
        WHERE chunk.chunk_number = masks.chunk_number
          AND (chunk.read_bits & masks.covered_bits)
              <> B'0'::bit({READ_CHUNK_BITS})
        """,
        (project_id, *values),
    )
    session.execute(
        f"""
        WITH doomed AS (
            SELECT DISTINCT
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number
            FROM m_workspace_messages
            WHERE project_id = %s AND {where_sql}
        )
        DELETE FROM m_workspace_user_read_chunks_v1 AS chunk
        USING doomed
        WHERE chunk.chunk_number = doomed.chunk_number
          AND bit_count(chunk.read_bits) = 0
        """,
        (project_id, *values),
    )


def clear_topic_for_all_users(
    session: typing.Any,
    project_id: object,
    topic_uuid: object,
) -> None:
    _clear_message_scope_for_all_users(
        session,
        project_id,
        "topic_uuid = %s",
        (topic_uuid,),
    )


def clear_stream_for_all_users(
    session: typing.Any,
    project_id: object,
    stream_uuid: object,
) -> None:
    _clear_message_scope_for_all_users(
        session,
        project_id,
        "stream_uuid = %s",
        (stream_uuid,),
    )


def purge_external_account_messages(
    session: typing.Any,
    project_id: object,
    stream_uuid: object,
    external_account_uuid: object,
) -> None:
    """Remove compact state for an account-scoped subset before hard delete."""
    lock_message_structure(session, (project_id,))
    lock_projects(session, (project_id,))
    bump_project_structure_revisions(session, (project_id,))
    changed_users = session.execute(
        f"""
        WITH doomed AS MATERIALIZED (
            SELECT topic_uuid, ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s
              AND stream_uuid = %s
              AND external_account_uuid = %s
        ), masks AS (
            SELECT
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number,
                bit_or(
                    set_bit(
                        B'0'::bit({READ_CHUNK_BITS}),
                        (ingest_sequence %% {READ_CHUNK_BITS})::integer,
                        1
                    )
                ) AS covered_bits
            FROM doomed
            GROUP BY ingest_sequence / {READ_CHUNK_BITS}
        )
        UPDATE m_workspace_user_read_chunks_v1 AS chunk
        SET read_bits = chunk.read_bits & ~masks.covered_bits,
            updated_at = NOW()
        FROM masks
        WHERE chunk.chunk_number = masks.chunk_number
          AND (chunk.read_bits & masks.covered_bits)
              <> B'0'::bit({READ_CHUNK_BITS})
        RETURNING chunk.user_uuid
        """,
        (project_id, stream_uuid, external_account_uuid),
    ).fetchall()
    session.execute(
        f"""
        WITH doomed AS (
            SELECT DISTINCT
                ingest_sequence / {READ_CHUNK_BITS} AS chunk_number
            FROM m_workspace_messages
            WHERE project_id = %s
              AND stream_uuid = %s
              AND external_account_uuid = %s
        )
        DELETE FROM m_workspace_user_read_chunks_v1 AS chunk
        USING doomed
        WHERE chunk.chunk_number = doomed.chunk_number
          AND bit_count(chunk.read_bits) = 0
        """,
        (project_id, stream_uuid, external_account_uuid),
    )
    topic_rows = session.execute(
        """
        SELECT DISTINCT topic_uuid
        FROM m_workspace_messages
        WHERE project_id = %s
          AND stream_uuid = %s
          AND external_account_uuid = %s
        """,
        (project_id, stream_uuid, external_account_uuid),
    ).fetchall()
    _refresh_topic_read_stats(
        session,
        project_id,
        (
            (row["user_uuid"], topic["topic_uuid"])
            for row in changed_users
            for topic in topic_rows
        ),
    )
    session.execute(
        """
        WITH doomed AS (
            SELECT topic_uuid, COUNT(*) AS message_count
            FROM m_workspace_messages
            WHERE project_id = %s
              AND stream_uuid = %s
              AND external_account_uuid = %s
            GROUP BY topic_uuid
        )
        UPDATE m_workspace_topic_message_stats_v1 AS stats
        SET message_count = GREATEST(
                stats.message_count - doomed.message_count,
                0
            ),
            last_ingest_sequence = (
                SELECT MAX(message.ingest_sequence)
                FROM m_workspace_messages AS message
                WHERE message.project_id = stats.project_id
                  AND message.topic_uuid = stats.topic_uuid
                  AND (
                        message.stream_uuid IS DISTINCT FROM %s
                        OR message.external_account_uuid IS DISTINCT FROM %s
                  )
            ),
            updated_at = NOW()
        FROM doomed
        WHERE stats.project_id = %s
          AND stats.topic_uuid = doomed.topic_uuid
        """,
        (
            project_id,
            stream_uuid,
            external_account_uuid,
            stream_uuid,
            external_account_uuid,
            project_id,
        ),
    )


def sync_message_mentions(
    session: typing.Any,
    project_id: object,
    message_uuid: object,
    stream_uuid: object,
    topic_uuid: object,
    ingest_sequence: int,
    recipient_uuids: collections.abc.Collection[object],
    content: str | None,
) -> None:
    session.execute(
        """
        DELETE FROM m_workspace_message_mentions_v1
        WHERE message_uuid = %s
        """,
        (message_uuid,),
    )
    if not recipient_uuids:
        return
    session.execute(
        """
        INSERT INTO m_workspace_message_mentions_v1 (
            message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
            ingest_sequence, created_at
        )
        SELECT %s, recipient_uuid, %s, %s, %s, %s, NOW()
        FROM unnest(%s::uuid[]) AS recipient_uuid
        WHERE POSITION(
            '](' || 'urn:user:' || LOWER(recipient_uuid::text) || ')'
            IN LOWER(%s)
        ) > 0
        ON CONFLICT (message_uuid, user_uuid) DO NOTHING
        """,
        (
            message_uuid,
            project_id,
            stream_uuid,
            topic_uuid,
            ingest_sequence,
            list(recipient_uuids),
            content or "",
        ),
    )


def begin_compaction(session: typing.Any, project_id: object) -> None:
    session.execute(
        """
        INSERT INTO m_workspace_read_state_projects_v1 (
            project_id, mode, created_at, updated_at
        ) VALUES (%s, 'preparing', NOW(), NOW())
        ON CONFLICT (project_id) DO UPDATE
        SET mode = CASE
                WHEN m_workspace_read_state_projects_v1.mode = 'legacy'
                    THEN 'preparing'
                ELSE m_workspace_read_state_projects_v1.mode
            END,
            updated_at = NOW()
        """,
        (project_id,),
    )
    session.execute(
        """
        INSERT INTO m_workspace_read_state_compaction_v1 (
            project_id, phase, last_message_uuid, last_user_uuid,
            last_ingest_sequence, target_ingest_sequence, processed_rows,
            created_at, updated_at
        )
        SELECT
            %s, 'sequences', NULL, NULL, 0,
            NULL, 0, NOW(), NOW()
        ON CONFLICT (project_id) DO NOTHING
        """,
        (project_id,),
    )


def _backfill_ingest_sequences_batch(
    session: typing.Any,
    project_id: object,
    batch_size: int,
) -> int:
    processed = _assign_legacy_ingest_sequences(
        session,
        project_id,
        batch_size=batch_size,
    )
    if processed:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET processed_rows = processed_rows + %s, updated_at = NOW()
            WHERE project_id = %s
            """,
            (processed, project_id),
        )
        return processed
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1 AS progress
        SET phase = 'memberships', last_message_uuid = NULL,
            last_user_uuid = NULL, last_ingest_sequence = 0,
            target_ingest_sequence = COALESCE(
                (
                    SELECT MAX(message.ingest_sequence)
                    FROM m_workspace_messages AS message
                    WHERE message.project_id = %s
                ),
                0
            ),
            updated_at = NOW()
        WHERE progress.project_id = %s
        """,
        (project_id, project_id),
    )
    return 0


def _compact_memberships_batch(
    session: typing.Any,
    project_id: object,
    last_message_uuid: object | None,
    last_user_uuid: object | None,
    target_ingest_sequence: int,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_message_uuid is not None:
        boundary_sql = "AND (flags.uuid, flags.user_uuid) > (%s, %s)"
        boundary_values = (last_message_uuid, last_user_uuid)
    rows = session.execute(
        f"""
        SELECT
            flags.uuid,
            flags.user_uuid,
            message.stream_uuid,
            message.ingest_sequence
        FROM m_workspace_user_message_flags AS flags
        JOIN m_workspace_messages AS message
          ON message.uuid = flags.uuid
         AND message.project_id = flags.project_id
        WHERE flags.project_id = %s
          AND message.ingest_sequence <= %s
          {boundary_sql}
        ORDER BY flags.uuid, flags.user_uuid
        LIMIT %s
        """,
        (
            project_id,
            target_ingest_sequence,
            *boundary_values,
            batch_size,
        ),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_projects_v1
            SET mode = 'dual', updated_at = NOW()
            WHERE project_id = %s AND mode = 'preparing'
            """,
            (project_id,),
        )
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'flags', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    _record_detached_memberships(session, project_id, rows)
    last = rows[-1]
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = %s,
            last_user_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (
            last["uuid"],
            last["user_uuid"],
            len(rows),
            project_id,
        ),
    )
    return len(rows)


def _complete_compaction(session: typing.Any, project_id: object) -> None:
    session.execute(
        """
        UPDATE m_workspace_read_state_projects_v1
        SET mode = 'compact', updated_at = NOW()
        WHERE project_id = %s AND mode = 'dual'
        """,
        (project_id,),
    )
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET completed_at = NOW(), updated_at = NOW()
        WHERE project_id = %s
        """,
        (project_id,),
    )


def _compact_mentions_batch(
    session: typing.Any,
    project_id: object,
    last_ingest_sequence: int,
    batch_size: int,
) -> int:
    rows = session.execute(
        """
        SELECT ingest_sequence
        FROM m_workspace_messages
        WHERE project_id = %s AND ingest_sequence > %s
        ORDER BY ingest_sequence
        LIMIT %s
        """,
        (project_id, last_ingest_sequence, batch_size),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'verify', last_message_uuid = NULL,
                last_user_uuid = NULL, updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    boundary = rows[-1]["ingest_sequence"]
    session.execute(
        """
        INSERT INTO m_workspace_message_mentions_v1 (
            message_uuid, user_uuid, project_id, stream_uuid, topic_uuid,
            ingest_sequence, created_at
        )
        SELECT
            message.uuid,
            (matched.value)[1]::uuid,
            message.project_id,
            message.stream_uuid,
            message.topic_uuid,
            message.ingest_sequence,
            NOW()
        FROM m_workspace_messages AS message
        CROSS JOIN LATERAL regexp_matches(
            LOWER(COALESCE(message.payload->>'content', '')),
            '][(]urn:user:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[)]',
            'g'
        ) AS matched(value)
        JOIN LATERAL (
            SELECT binding.user_uuid
            FROM m_workspace_stream_bindings AS binding
            WHERE binding.project_id = message.project_id
              AND binding.stream_uuid = message.stream_uuid
              AND binding.user_uuid = (matched.value)[1]::uuid
            UNION
            SELECT membership.user_uuid
            FROM m_workspace_read_memberships_v1 AS membership
            WHERE membership.project_id = message.project_id
              AND membership.stream_uuid = message.stream_uuid
              AND membership.user_uuid = (matched.value)[1]::uuid
              AND message.ingest_sequence
                  <= membership.last_detached_sequence
        ) AS recipient ON TRUE
        WHERE message.project_id = %s
          AND message.ingest_sequence > %s
          AND message.ingest_sequence <= %s
        ON CONFLICT (message_uuid, user_uuid) DO NOTHING
        """,
        (project_id, last_ingest_sequence, boundary),
    )
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_ingest_sequence = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (boundary, len(rows), project_id),
    )
    return len(rows)


def _compact_topic_stats_batch(
    session: typing.Any,
    project_id: object,
    last_topic_uuid: object | None,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_topic_uuid is not None:
        boundary_sql = "AND topic.uuid > %s"
        boundary_values = (last_topic_uuid,)
    rows = session.execute(
        f"""
        WITH candidates AS MATERIALIZED (
            SELECT topic.uuid, topic.stream_uuid
            FROM m_workspace_stream_topics AS topic
            WHERE topic.project_id = %s
              {boundary_sql}
            ORDER BY topic.uuid
            LIMIT %s
        ), canonical AS (
            SELECT
                candidate.uuid AS topic_uuid,
                candidate.stream_uuid,
                COUNT(message.uuid) AS message_count,
                MAX(message.ingest_sequence) AS last_ingest_sequence
            FROM candidates AS candidate
            LEFT JOIN m_workspace_messages AS message
              ON message.project_id = %s
             AND message.topic_uuid = candidate.uuid
            GROUP BY candidate.uuid, candidate.stream_uuid
        ), upserted AS (
            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence, created_at, updated_at
            )
            SELECT
                topic_uuid, %s, stream_uuid, message_count,
                last_ingest_sequence, NOW(), NOW()
            FROM canonical
            ON CONFLICT (topic_uuid) DO UPDATE
            SET project_id = EXCLUDED.project_id,
                stream_uuid = EXCLUDED.stream_uuid,
                message_count = EXCLUDED.message_count,
                last_ingest_sequence = EXCLUDED.last_ingest_sequence,
                updated_at = NOW()
            RETURNING topic_uuid
        )
        SELECT topic_uuid
        FROM upserted
        ORDER BY topic_uuid
        """,
        (
            project_id,
            *boundary_values,
            batch_size,
            project_id,
            project_id,
        ),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'mentions', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (rows[-1]["topic_uuid"], len(rows), project_id),
    )
    return len(rows)


def _verify_flags_batch(
    session: typing.Any,
    project_id: object,
    last_message_uuid: object | None,
    last_user_uuid: object | None,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_message_uuid is not None:
        boundary_sql = "AND (flags.uuid, flags.user_uuid) > (%s, %s)"
        boundary_values = (last_message_uuid, last_user_uuid)
    rows = session.execute(
        f"""
        SELECT
            flags.uuid,
            flags.user_uuid,
            flags.read AS legacy_read,
            COALESCE(stats.read_count, 0) AS stored_read_count,
            COALESCE(
                get_bit(
                    chunk.read_bits,
                    (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                ),
                0
            ) = 1 AS compact_read
        FROM m_workspace_user_message_flags AS flags
        JOIN m_workspace_messages AS message
          ON message.uuid = flags.uuid
         AND message.project_id = flags.project_id
        LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
          ON chunk.user_uuid = flags.user_uuid
         AND chunk.chunk_number = message.ingest_sequence / {READ_CHUNK_BITS}
        LEFT JOIN m_workspace_user_topic_read_stats_v1 AS stats
          ON stats.project_id = flags.project_id
         AND stats.user_uuid = flags.user_uuid
         AND stats.topic_uuid = message.topic_uuid
        WHERE flags.project_id = %s
          AND flags.read = TRUE
          {boundary_sql}
        ORDER BY flags.uuid, flags.user_uuid
        LIMIT %s
        """,
        (project_id, *boundary_values, batch_size),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'verify_chunks', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    if any(row["legacy_read"] != row["compact_read"] for row in rows):
        raise RuntimeError("Compact Workspace read-state parity check failed")
    if any(row["stored_read_count"] <= 0 for row in rows):
        raise RuntimeError("Compact Workspace read-counter parity check failed")
    last = rows[-1]
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = %s,
            last_user_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (last["uuid"], last["user_uuid"], len(rows), project_id),
    )
    return len(rows)


def _verify_chunks_batch(
    session: typing.Any,
    project_id: object,
    last_user_uuid: object | None,
    last_chunk_number: int,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_user_uuid is not None:
        boundary_sql = "AND (chunk.user_uuid, chunk.chunk_number) > (%s, %s)"
        boundary_values = (last_user_uuid, last_chunk_number)
    rows = session.execute(
        f"""
        SELECT
            chunk.user_uuid,
            chunk.chunk_number,
            EXISTS (
                SELECT 1
                FROM m_workspace_messages AS message
                LEFT JOIN m_workspace_user_message_flags AS flags
                  ON flags.project_id = message.project_id
                 AND flags.uuid = message.uuid
                 AND flags.user_uuid = chunk.user_uuid
                WHERE message.project_id = %s
                  AND message.ingest_sequence >=
                        chunk.chunk_number * {READ_CHUNK_BITS}
                  AND message.ingest_sequence <
                        (chunk.chunk_number + 1) * {READ_CHUNK_BITS}
                  AND get_bit(
                        chunk.read_bits,
                        (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                      ) = 1
                  AND COALESCE(flags.read, FALSE) = FALSE
            ) AS compact_only_read
        FROM m_workspace_user_read_chunks_v1 AS chunk
        WHERE EXISTS (
                SELECT 1
                FROM m_workspace_messages AS message
                WHERE message.project_id = %s
                  AND message.ingest_sequence >=
                        chunk.chunk_number * {READ_CHUNK_BITS}
                  AND message.ingest_sequence <
                        (chunk.chunk_number + 1) * {READ_CHUNK_BITS}
                  AND get_bit(
                        chunk.read_bits,
                        (message.ingest_sequence %% {READ_CHUNK_BITS})::integer
                      ) = 1
            )
          {boundary_sql}
        ORDER BY chunk.user_uuid, chunk.chunk_number
        LIMIT %s
        """,
        (
            project_id,
            project_id,
            *boundary_values,
            min(batch_size, VERIFY_CHUNK_BATCH_SIZE),
        ),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'verify_read_stats', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    if any(row["compact_only_read"] for row in rows):
        raise RuntimeError("Compact Workspace read-state parity check failed")
    last = rows[-1]
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_user_uuid = %s,
            last_ingest_sequence = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (
            last["user_uuid"],
            last["chunk_number"],
            len(rows),
            project_id,
        ),
    )
    return len(rows)


def _verify_read_stats_batch(
    session: typing.Any,
    project_id: object,
    last_user_uuid: object | None,
    last_topic_uuid: object | None,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_user_uuid is not None:
        boundary_sql = "AND (stats.user_uuid, stats.topic_uuid) > (%s, %s)"
        boundary_values = (last_user_uuid, last_topic_uuid)
    rows = session.execute(
        f"""
        WITH candidates AS MATERIALIZED (
            SELECT stats.user_uuid, stats.topic_uuid, stats.read_count
            FROM m_workspace_user_topic_read_stats_v1 AS stats
            WHERE stats.project_id = %s
              {boundary_sql}
            ORDER BY stats.user_uuid, stats.topic_uuid
            LIMIT %s
        ), candidate_users AS MATERIALIZED (
            SELECT DISTINCT candidate.user_uuid
            FROM candidates AS candidate
        ), actual AS MATERIALIZED (
            SELECT
                flags.user_uuid,
                message.topic_uuid,
                COUNT(*)::bigint AS read_count
            FROM m_workspace_user_message_flags AS flags
            JOIN m_workspace_messages AS message
              ON message.uuid = flags.uuid
             AND message.project_id = flags.project_id
            JOIN candidate_users AS candidate_user
              ON candidate_user.user_uuid = flags.user_uuid
            WHERE flags.project_id = %s
              AND flags.read = TRUE
            GROUP BY flags.user_uuid, message.topic_uuid
        )
        SELECT
            candidate.user_uuid,
            candidate.topic_uuid,
            COALESCE(actual.read_count, 0) AS actual_read_count,
            candidate.read_count AS stored_read_count
        FROM candidates AS candidate
        LEFT JOIN actual
          ON actual.user_uuid = candidate.user_uuid
         AND actual.topic_uuid = candidate.topic_uuid
        ORDER BY candidate.user_uuid, candidate.topic_uuid
        """,
        (
            project_id,
            *boundary_values,
            batch_size,
            project_id,
        ),
    ).fetchall()
    if not rows:
        missing_counter = session.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM m_workspace_user_message_flags AS flags
                JOIN m_workspace_messages AS message
                  ON message.uuid = flags.uuid
                 AND message.project_id = flags.project_id
                LEFT JOIN m_workspace_user_topic_read_stats_v1 AS stats
                  ON stats.project_id = flags.project_id
                 AND stats.user_uuid = flags.user_uuid
                 AND stats.topic_uuid = message.topic_uuid
                WHERE flags.project_id = %s
                  AND flags.read = TRUE
                  AND stats.user_uuid IS NULL
            ) AS missing
            """,
            (project_id,),
        ).fetchone()["missing"]
        if missing_counter:
            raise RuntimeError("Compact Workspace read-counter parity check failed")
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'verify_stats', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    if any(row["actual_read_count"] != row["stored_read_count"] for row in rows):
        raise RuntimeError("Compact Workspace read-counter parity check failed")
    last = rows[-1]
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_user_uuid = %s,
            last_message_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (
            last["user_uuid"],
            last["topic_uuid"],
            len(rows),
            project_id,
        ),
    )
    return len(rows)


def _verify_topic_stats_batch(
    session: typing.Any,
    project_id: object,
    last_topic_uuid: object | None,
    batch_size: int,
) -> int:
    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_topic_uuid is not None:
        boundary_sql = "AND topic.uuid > %s"
        boundary_values = (last_topic_uuid,)
    rows = session.execute(
        f"""
        WITH candidates AS MATERIALIZED (
            SELECT topic.uuid, topic.stream_uuid
            FROM m_workspace_stream_topics AS topic
            WHERE topic.project_id = %s
              {boundary_sql}
            ORDER BY topic.uuid
            LIMIT %s
        ), expected AS (
            SELECT
                candidate.uuid AS topic_uuid,
                candidate.stream_uuid,
                COUNT(message.uuid) AS message_count,
                MAX(message.ingest_sequence) AS last_ingest_sequence
            FROM candidates AS candidate
            LEFT JOIN m_workspace_messages AS message
              ON message.project_id = %s
             AND message.topic_uuid = candidate.uuid
            GROUP BY candidate.uuid, candidate.stream_uuid
        )
        SELECT
            expected.topic_uuid,
            expected.stream_uuid,
            expected.message_count,
            expected.last_ingest_sequence,
            stats.project_id AS stored_project_id,
            stats.stream_uuid AS stored_stream_uuid,
            stats.message_count AS stored_message_count,
            stats.last_ingest_sequence AS stored_last_ingest_sequence
        FROM expected
        LEFT JOIN m_workspace_topic_message_stats_v1 AS stats
          ON stats.topic_uuid = expected.topic_uuid
        ORDER BY expected.topic_uuid
        """,
        (
            project_id,
            *boundary_values,
            batch_size,
            project_id,
        ),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'verify_mentions', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0
    if any(
        not (row["message_count"] == 0 and row["stored_project_id"] is None)
        and (
            str(row["stored_project_id"]) != str(project_id)
            or row["stored_stream_uuid"] != row["stream_uuid"]
            or row["stored_message_count"] != row["message_count"]
            or row["stored_last_ingest_sequence"] != row["last_ingest_sequence"]
        )
        for row in rows
    ):
        raise RuntimeError("Compact Workspace topic-stats parity check failed")
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (rows[-1]["topic_uuid"], len(rows), project_id),
    )
    return len(rows)


def _verify_mentions_batch(
    session: typing.Any,
    project_id: object,
    last_ingest_sequence: int,
    batch_size: int,
) -> int:
    result = session.execute(
        """
        WITH candidates AS MATERIALIZED (
            SELECT
                message.uuid, message.project_id, message.stream_uuid,
                message.topic_uuid, message.ingest_sequence, message.payload
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s
              AND message.ingest_sequence > %s
            ORDER BY message.ingest_sequence
            LIMIT %s
        ), expected AS (
            SELECT
                message.uuid AS message_uuid,
                recipient.user_uuid,
                message.project_id,
                message.stream_uuid,
                message.topic_uuid,
                message.ingest_sequence
            FROM candidates AS message
            CROSS JOIN LATERAL regexp_matches(
                LOWER(COALESCE(message.payload->>'content', '')),
                '][(]urn:user:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[)]',
                'g'
            ) AS matched(value)
            JOIN LATERAL (
                SELECT binding.user_uuid
                FROM m_workspace_stream_bindings AS binding
                WHERE binding.project_id = message.project_id
                  AND binding.stream_uuid = message.stream_uuid
                  AND binding.user_uuid = (matched.value)[1]::uuid
                UNION
                SELECT membership.user_uuid
                FROM m_workspace_read_memberships_v1 AS membership
                WHERE membership.project_id = message.project_id
                  AND membership.stream_uuid = message.stream_uuid
                  AND membership.user_uuid = (matched.value)[1]::uuid
                  AND message.ingest_sequence
                      <= membership.last_detached_sequence
            ) AS recipient ON TRUE
        ), stored AS (
            SELECT
                mention.message_uuid, mention.user_uuid, mention.project_id,
                mention.stream_uuid, mention.topic_uuid,
                mention.ingest_sequence
            FROM m_workspace_message_mentions_v1 AS mention
            JOIN candidates AS message
              ON message.uuid = mention.message_uuid
        ), differences AS (
            (SELECT * FROM expected EXCEPT SELECT * FROM stored)
            UNION ALL
            (SELECT * FROM stored EXCEPT SELECT * FROM expected)
        )
        SELECT
            COUNT(*) AS message_count,
            MAX(ingest_sequence) AS last_ingest_sequence,
            EXISTS (SELECT 1 FROM differences) AS mismatch
        FROM candidates
        """,
        (project_id, last_ingest_sequence, batch_size),
    ).fetchone()
    if result["message_count"] == 0:
        _complete_compaction(session, project_id)
        return 0
    if result["mismatch"]:
        raise RuntimeError("Compact Workspace mention parity check failed")
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_ingest_sequence = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (
            result["last_ingest_sequence"],
            result["message_count"],
            project_id,
        ),
    )
    return result["message_count"]


def compact_legacy_batch(
    session: typing.Any,
    project_id: object,
    batch_size: int = COMPACTION_BATCH_SIZE,
) -> int:
    progress = session.execute(
        """
        SELECT phase, last_message_uuid, last_user_uuid,
               last_ingest_sequence, target_ingest_sequence, completed_at
        FROM m_workspace_read_state_compaction_v1
        WHERE project_id = %s
        FOR UPDATE
        """,
        (project_id,),
    ).fetchone()
    if progress is None:
        begin_compaction(session, project_id)
        return compact_legacy_batch(session, project_id, batch_size)
    else:
        if progress["completed_at"] is not None:
            return 0
        phase = progress["phase"]
        last_message_uuid = progress["last_message_uuid"]
        last_user_uuid = progress["last_user_uuid"]
        last_ingest_sequence = progress["last_ingest_sequence"]
        target_ingest_sequence = progress["target_ingest_sequence"]

    if phase == "memberships":
        return _compact_memberships_batch(
            session,
            project_id,
            last_message_uuid,
            last_user_uuid,
            target_ingest_sequence,
            batch_size,
        )
    if phase == "sequences":
        return _backfill_ingest_sequences_batch(
            session,
            project_id,
            batch_size,
        )

    if phase == "mentions":
        return _compact_mentions_batch(
            session,
            project_id,
            last_ingest_sequence,
            batch_size,
        )
    if phase == "stats":
        return _compact_topic_stats_batch(
            session,
            project_id,
            last_message_uuid,
            batch_size,
        )
    if phase == "verify":
        return _verify_flags_batch(
            session,
            project_id,
            last_message_uuid,
            last_user_uuid,
            batch_size,
        )
    if phase == "verify_stats":
        return _verify_topic_stats_batch(
            session,
            project_id,
            last_message_uuid,
            batch_size,
        )
    if phase == "verify_chunks":
        return _verify_chunks_batch(
            session,
            project_id,
            last_user_uuid,
            last_ingest_sequence,
            batch_size,
        )
    if phase == "verify_read_stats":
        return _verify_read_stats_batch(
            session,
            project_id,
            last_user_uuid,
            last_message_uuid,
            batch_size,
        )
    if phase == "verify_mentions":
        return _verify_mentions_batch(
            session,
            project_id,
            last_ingest_sequence,
            batch_size,
        )

    boundary_sql = ""
    boundary_values: tuple[object, ...] = ()
    if last_message_uuid is not None:
        boundary_sql = "AND (flags.uuid, flags.user_uuid) > (%s, %s)"
        boundary_values = (last_message_uuid, last_user_uuid)
    rows = session.execute(
        f"""
        SELECT
            flags.uuid,
            flags.user_uuid,
            flags.read,
            message.stream_uuid,
            message.topic_uuid,
            message.ingest_sequence
        FROM m_workspace_user_message_flags AS flags
        JOIN m_workspace_messages AS message
          ON message.uuid = flags.uuid
         AND message.project_id = flags.project_id
        WHERE flags.project_id = %s
          AND flags.read = TRUE
          {boundary_sql}
        ORDER BY flags.uuid, flags.user_uuid
        LIMIT %s
        """,
        (project_id, *boundary_values, batch_size),
    ).fetchall()
    if not rows:
        session.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET phase = 'stats', last_message_uuid = NULL,
                last_user_uuid = NULL, last_ingest_sequence = 0,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (project_id,),
        )
        return 0

    _apply_coordinate_rows(session, project_id, rows)
    last = rows[-1]
    session.execute(
        """
        UPDATE m_workspace_read_state_compaction_v1
        SET last_message_uuid = %s,
            last_user_uuid = %s,
            processed_rows = processed_rows + %s,
            updated_at = NOW()
        WHERE project_id = %s
        """,
        (last["uuid"], last["user_uuid"], len(rows), project_id),
    )
    return len(rows)


def maintain_next_project(
    session: typing.Any,
    batch_size: int = COMPACTION_BATCH_SIZE,
    *,
    cleanup_enabled: bool = False,
    excluded_project_ids: collections.abc.Iterable[object] = (),
) -> tuple[str, object, int] | None:
    # Fence candidate selection and the whole maintenance transaction against
    # the final downgrade schema swap, even before a project is known.
    lock_projects(session, ())
    excluded = sorted(
        {sys_uuid.UUID(str(project_id)) for project_id in excluded_project_ids},
        key=str,
    )
    candidate_cursor: tuple[object, object] | None = None
    while True:
        candidates = session.execute(
            """
            SELECT state.project_id, state.mode,
                   state.updated_at AS candidate_updated_at
            FROM m_workspace_read_state_projects_v1 AS state
            WHERE state.mode IN ('legacy', 'preparing', 'dual')
              AND NOT (state.project_id = ANY(%s::uuid[]))
              AND (
                    %s::timestamptz IS NULL
                    OR (
                        state.updated_at, state.project_id
                    ) > (%s::timestamptz, %s::uuid)
                  )
            ORDER BY state.updated_at, state.project_id
            LIMIT %s
            """,
            (
                excluded,
                None if candidate_cursor is None else candidate_cursor[0],
                None if candidate_cursor is None else candidate_cursor[0],
                None if candidate_cursor is None else candidate_cursor[1],
                MAINTENANCE_CANDIDATE_LIMIT,
            ),
        ).fetchall()
        if not candidates:
            break
        for candidate in candidates:
            locked = session.execute(
                """
                SELECT pg_try_advisory_xact_lock(
                    hashtextextended(%s::text, 0)
                ) AS locked
                """,
                (candidate["project_id"],),
            ).fetchone()["locked"]
            if not locked:
                continue
            project = session.execute(
                """
                SELECT project_id, mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                  AND mode IN ('legacy', 'preparing', 'dual')
                FOR UPDATE SKIP LOCKED
                """,
                (candidate["project_id"],),
            ).fetchone()
            if project is None:
                continue
            try:
                begin_compaction(session, project["project_id"])
                processed = compact_legacy_batch(
                    session,
                    project["project_id"],
                    batch_size,
                )
            except Exception as error:
                raise MaintenanceProjectError(project["project_id"]) from error
            action = (
                "prepare"
                if project["mode"] in {PROJECT_MODE_LEGACY, PROJECT_MODE_PREPARING}
                else "compact"
            )
            return action, project["project_id"], processed
        last_candidate = candidates[-1]
        candidate_cursor = (
            last_candidate["candidate_updated_at"],
            last_candidate["project_id"],
        )
        if len(candidates) < MAINTENANCE_CANDIDATE_LIMIT:
            break
    if not cleanup_enabled:
        return None
    candidate_cursor = None
    while True:
        candidates = session.execute(
            """
            SELECT state.project_id, state.updated_at AS candidate_updated_at
            FROM m_workspace_read_state_projects_v1 AS state
            WHERE state.mode = 'compact'
              AND NOT (state.project_id = ANY(%s::uuid[]))
              AND (
                    %s::timestamptz IS NULL
                    OR (state.updated_at, state.project_id) >
                       (%s::timestamptz, %s::uuid)
                  )
              AND EXISTS (
                    SELECT 1
                    FROM m_workspace_user_message_flags AS flags
                    WHERE flags.project_id = state.project_id
                      AND flags.pinned = FALSE
                      AND flags.starred = FALSE
                  )
            ORDER BY state.updated_at, state.project_id
            LIMIT %s
            """,
            (
                excluded,
                None if candidate_cursor is None else candidate_cursor[0],
                None if candidate_cursor is None else candidate_cursor[0],
                None if candidate_cursor is None else candidate_cursor[1],
                MAINTENANCE_CANDIDATE_LIMIT,
            ),
        ).fetchall()
        if not candidates:
            break
        for candidate in candidates:
            locked = session.execute(
                """
                SELECT pg_try_advisory_xact_lock(
                    hashtextextended(%s::text, 0)
                ) AS locked
                """,
                (candidate["project_id"],),
            ).fetchone()["locked"]
            if not locked:
                continue
            project = session.execute(
                """
                SELECT project_id
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s AND mode = 'compact'
                FOR UPDATE SKIP LOCKED
                """,
                (candidate["project_id"],),
            ).fetchone()
            if project is None:
                continue
            try:
                processed = cleanup_legacy_flags(
                    session,
                    project["project_id"],
                    batch_size,
                )
                session.execute(
                    """
                    UPDATE m_workspace_read_state_projects_v1
                    SET updated_at = NOW()
                    WHERE project_id = %s
                    """,
                    (project["project_id"],),
                )
            except Exception as error:
                raise MaintenanceProjectError(project["project_id"]) from error
            return "cleanup", project["project_id"], processed
        last_candidate = candidates[-1]
        candidate_cursor = (
            last_candidate["candidate_updated_at"],
            last_candidate["project_id"],
        )
        if len(candidates) < MAINTENANCE_CANDIDATE_LIMIT:
            break
    return None


def cleanup_legacy_flags(
    session: typing.Any,
    project_id: object,
    batch_size: int = COMPACTION_BATCH_SIZE,
) -> int:
    if project_mode(session, project_id) != PROJECT_MODE_COMPACT:
        raise RuntimeError(
            "Legacy read flags can only be cleaned after compact cutover"
        )
    rows = session.execute(
        """
        WITH candidates AS (
            SELECT flags.ctid
            FROM m_workspace_user_message_flags AS flags
            WHERE flags.project_id = %s
              AND flags.pinned = FALSE
              AND flags.starred = FALSE
            LIMIT %s
        )
        DELETE FROM m_workspace_user_message_flags AS flags
        USING candidates
        WHERE flags.ctid = candidates.ctid
          AND flags.pinned = FALSE
          AND flags.starred = FALSE
        RETURNING 1
        """,
        (project_id, batch_size),
    ).fetchall()
    return len(rows)
