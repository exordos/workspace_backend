#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.

"""SQL repository for the global Workspace sticker catalog.

The repository deliberately accepts a caller-owned RestAlchemy session.  HTTP
parsing, permission checks, and media storage stay outside this module.
"""

import base64
import binascii
import collections.abc as collections_abc
import dataclasses
import datetime
import hashlib
import json
import math
import re
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api.dm import stickers as sticker_models


SORT_CREATED = "created_at_desc_uuid_desc"
SORT_FAVORITE = "favorite_created_at_desc_uuid_desc"
SORT_RANK_UPDATED = "rank_desc_updated_at_desc_uuid_desc"
SORT_RANK_FAVORITE = "rank_desc_favorite_created_at_desc_uuid_desc"
_SORT_COLUMNS = {
    SORT_CREATED: ("created_at", "uuid"),
    SORT_FAVORITE: ("favorite_created_at", "uuid"),
    SORT_RANK_UPDATED: ("rank", "updated_at", "uuid"),
    SORT_RANK_FAVORITE: ("rank", "favorite_created_at", "uuid"),
}
CURSOR_VERSION = 1
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_STICKER_COLUMNS = (
    "uuid",
    "title",
    "alt_text",
    "emoji",
    "tags",
    "search_text",
    "category",
    "format",
    "width",
    "height",
    "size_bytes",
    "sha256",
    "media_object_id",
    "created_at",
    "updated_at",
)


class StickerRepositoryValidationError(ra_exc.ValidationErrorException):
    """A client-controlled repository value is not valid."""

    message = "Invalid sticker catalog request"
    code = 400


class StickerNotFoundError(ra_exc.RestAlchemyException):
    """A sticker is missing from the catalog."""

    message = "Sticker is not available"
    code = 404


@dataclasses.dataclass(frozen=True)
class StickerRecord:
    sticker: sticker_models.Sticker
    is_favorite: bool = False
    rank: float | None = None
    favorite_created_at: datetime.datetime | None = None

    @property
    def uuid(self) -> sys_uuid.UUID:
        return self.sticker.uuid


@dataclasses.dataclass(frozen=True)
class StickerPage:
    items: list[StickerRecord]
    next_marker: str | None = None


@dataclasses.dataclass(frozen=True)
class _Query:
    q: str
    tag_query: str
    favorite: bool
    category: str | None
    format: str | None
    uuids: tuple[sys_uuid.UUID, ...]
    page_limit: int
    sort: str
    fingerprint: str


def _canonical_json(value: typing.Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def filters_fingerprint(
    *,
    query: sticker_models.StickerListQuery,
    sort: str,
) -> str:
    """Return the D-01 canonical query fingerprint."""

    payload = {
        "category": query.category,
        "favorite": query.favorite,
        "format": query.format,
        "q": query.q,
        "sort": sort,
        "uuid": [str(value) for value in query.uuids],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def encode_page_marker(
    *,
    sort: str,
    filters_sha256: str,
    values: typing.Sequence[typing.Any],
) -> str:
    """Encode an unpadded base64url canonical JSON page marker."""

    payload = {
        "filters_sha256": filters_sha256,
        "sort": sort,
        "v": CURSOR_VERSION,
        "values": [_marker_value(value) for value in values],
    }
    raw = _canonical_json(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_page_marker(value: str) -> dict[str, typing.Any]:
    """Decode and structurally validate a page marker.

    Query-specific validation is performed by ``_validate_marker`` after the
    request filters and sort tuple are known.
    """

    if not isinstance(value, str) or not value or not _CURSOR_RE.fullmatch(value):
        raise StickerRepositoryValidationError()
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise StickerRepositoryValidationError() from None
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "sort",
        "filters_sha256",
        "values",
    }:
        raise StickerRepositoryValidationError()
    if payload["v"] != CURSOR_VERSION or not isinstance(payload["sort"], str):
        raise StickerRepositoryValidationError()
    if (
        not isinstance(payload["filters_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["filters_sha256"])
        or not isinstance(payload["values"], list)
    ):
        raise StickerRepositoryValidationError()
    if raw != _canonical_json(payload).encode("utf-8"):
        raise StickerRepositoryValidationError()
    return typing.cast(dict[str, typing.Any], payload)


def _marker_value(value: typing.Any) -> typing.Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, sys_uuid.UUID):
        return str(value)
    if isinstance(value, (str, int, float)) or value is None:
        return value
    raise StickerRepositoryValidationError()


def _parse_marker_values(
    payload: dict[str, typing.Any], expected_sort: str
) -> tuple[typing.Any, ...]:
    if payload["sort"] != expected_sort:
        raise StickerRepositoryValidationError()
    values = payload["values"]
    expected_count = (
        3
        if expected_sort
        in {
            SORT_RANK_UPDATED,
            SORT_RANK_FAVORITE,
        }
        else 2
    )
    if len(values) != expected_count:
        raise StickerRepositoryValidationError()
    try:
        marker_uuid = sys_uuid.UUID(values[-1])
    except (AttributeError, ValueError, TypeError):
        raise StickerRepositoryValidationError() from None
    if not isinstance(values[-2], str):
        raise StickerRepositoryValidationError()
    try:
        marker_time = datetime.datetime.fromisoformat(values[-2])
    except (TypeError, ValueError):
        raise StickerRepositoryValidationError() from None
    if marker_time.tzinfo is None:
        marker_time = marker_time.replace(tzinfo=datetime.timezone.utc)
    if expected_count == 2:
        return marker_time, marker_uuid
    rank = values[0]
    if isinstance(rank, bool) or not isinstance(rank, (int, float)):
        raise StickerRepositoryValidationError()
    try:
        rank = float(rank)
    except OverflowError:
        raise StickerRepositoryValidationError() from None
    if not math.isfinite(rank) or not 0 <= rank <= 4:
        raise StickerRepositoryValidationError()
    return rank, marker_time, marker_uuid


class StickerRepository:
    """Read/write access to stickers using a caller-owned SQL session."""

    def _query(
        self,
        query: sticker_models.StickerListQuery,
    ) -> _Query:
        has_query = bool(query.q)
        if has_query and query.favorite:
            sort = SORT_RANK_FAVORITE
        elif has_query:
            sort = SORT_RANK_UPDATED
        elif query.favorite:
            sort = SORT_FAVORITE
        else:
            sort = SORT_CREATED
        fingerprint = filters_fingerprint(
            query=query,
            sort=sort,
        )
        return _Query(
            q=query.q,
            tag_query=query.tag_query,
            favorite=query.favorite,
            category=query.category,
            format=query.format,
            uuids=tuple(query.uuids),
            page_limit=query.page_limit,
            sort=sort,
            fingerprint=fingerprint,
        )

    def list_stickers(
        self,
        session: typing.Any,
        user_uuid: sys_uuid.UUID,
        list_query: sticker_models.StickerListQuery,
    ) -> StickerPage:
        """List stickers and compute favorite state in the same query."""

        query = self._query(list_query)
        marker_values: tuple[typing.Any, ...] | None = None
        if list_query.page_marker is not None:
            payload = decode_page_marker(list_query.page_marker)
            if payload["filters_sha256"] != query.fingerprint:
                raise StickerRepositoryValidationError()
            marker_values = _parse_marker_values(payload, query.sort)
        rows, params = self._list_statement(query, user_uuid, marker_values)
        if query.q:
            connection = getattr(session, "_conn", session)
            is_autocommit = bool(getattr(connection, "autocommit", False))
            previous_row = session.execute(
                "SELECT show_limit() AS threshold"
            ).fetchone()
            previous_limit = (
                previous_row["threshold"]
                if isinstance(previous_row, collections_abc.Mapping)
                else previous_row[0]
            )
            if is_autocommit:
                session.execute("SELECT set_limit(%s::real)", (0.1,))
            else:
                session.execute(
                    "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                    ("0.1",),
                )
            try:
                result_rows = session.execute(rows, params).fetchall()
            finally:
                try:
                    if is_autocommit:
                        session.execute(
                            "SELECT set_limit(%s::real)",
                            (previous_limit,),
                        )
                    else:
                        session.execute(
                            "SELECT set_config('pg_trgm.similarity_threshold', %s, true)",
                            (str(previous_limit),),
                        )
                except Exception:
                    # The caller owns rollback/connection lifecycle; never
                    # replace a query error with threshold restoration noise.
                    pass
        else:
            result_rows = session.execute(rows, params).fetchall()
        has_next = len(result_rows) > query.page_limit
        if has_next:
            result_rows = result_rows[: query.page_limit]
        records = [self._record_from_row(row) for row in result_rows]
        next_marker = None
        if has_next and records:
            record = records[-1]
            values: tuple[typing.Any, ...]
            if query.sort == SORT_CREATED:
                values = (record.sticker.created_at, record.uuid)
            elif query.sort == SORT_FAVORITE:
                values = (record.favorite_created_at, record.uuid)
            elif query.sort == SORT_RANK_UPDATED:
                values = (record.rank, record.sticker.updated_at, record.uuid)
            else:
                values = (record.rank, record.favorite_created_at, record.uuid)
            next_marker = encode_page_marker(
                sort=query.sort,
                filters_sha256=query.fingerprint,
                values=values,
            )
        return StickerPage(items=records, next_marker=next_marker)

    def _list_statement(
        self,
        query: _Query,
        user_uuid: sys_uuid.UUID,
        marker_values: tuple[typing.Any, ...] | None,
    ) -> tuple[str, tuple[typing.Any, ...]]:
        query_rank = "0.0"
        rank_params: list[typing.Any] = []
        candidate_from = "m_workspace_stickers AS s"
        candidate_params: list[typing.Any] = []
        if query.q:
            # Tags retain their original ё spelling, while search_text stores
            # the canonical е spelling.  Ranking keeps tag normalization; the
            # candidate branches use only raw indexed search predicates.
            like_query = (
                query.q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            )
            tag_match = "replace(lower(tag.value), 'ё', 'е') = %s"
            query_rank = """
                CASE
                  WHEN EXISTS (
                    SELECT 1 FROM unnest(s.tags) AS tag(value)
                    WHERE {tag_match}
                  ) THEN 4.0
                  WHEN replace(lower(s.title), 'ё', 'е') = %s THEN 3.0
                  WHEN replace(lower(s.title), 'ё', 'е') LIKE %s || '%%' ESCAPE '!' THEN 2.0
                  WHEN s.search_text LIKE '%%' || %s || '%%' ESCAPE '!'
                    THEN 1.0
                  ELSE similarity(s.search_text, %s)
                END
            """.format(tag_match=tag_match)
            rank_params = [query.q, query.q, like_query, like_query, query.q]

        where = []
        if query.category:
            where.append("s.category = %s")
        if query.format:
            where.append("s.format = %s")
        if query.uuids:
            where.append("s.uuid = ANY(%s::uuid[])")
        if query.q:
            candidate_where = "".join(
                part.replace("s.", "candidate.") + " AND " for part in where
            )
            candidate_from = """(
                SELECT candidate.*
                  FROM m_workspace_stickers AS candidate
                 WHERE {where}candidate.tags @> ARRAY[%s]::text[]
                UNION
                SELECT candidate.*
                  FROM m_workspace_stickers AS candidate
                 WHERE {where}candidate.tags @> ARRAY[%s]::text[]
                UNION
                SELECT candidate.*
                  FROM m_workspace_stickers AS candidate
                 WHERE {where}candidate.search_text LIKE '%%' || %s || '%%' ESCAPE '!'
                UNION
                SELECT candidate.*
                  FROM m_workspace_stickers AS candidate
                 WHERE {where}candidate.search_text %% %s
            ) AS s""".format(where=candidate_where)
            for value in (
                query.q,
                query.tag_query,
                like_query,
                query.q,
            ):
                if query.category:
                    candidate_params.append(query.category)
                if query.format:
                    candidate_params.append(query.format)
                if query.uuids:
                    candidate_params.append(list(query.uuids))
                candidate_params.append(value)
            where.clear()
        if query.favorite:
            where.append("favorite.sticker_uuid IS NOT NULL")
        sort_columns = _SORT_COLUMNS[query.sort]
        order = ", ".join(f"{column} DESC" for column in sort_columns)
        outer_where = ""
        marker_params: list[typing.Any] = []
        if marker_values is not None:
            marker_columns = ", ".join(sort_columns)
            placeholders = ", ".join(["%s"] * len(sort_columns))
            outer_where = f"WHERE ({marker_columns}) < ({placeholders})"
            marker_params = list(marker_values)
        columns = ", ".join("s.%s" % column for column in _STICKER_COLUMNS)
        statement = """
            WITH ranked AS (
              SELECT {columns},
                     favorite.created_at AS favorite_created_at,
                     favorite.sticker_uuid IS NOT NULL AS is_favorite,
                     {rank} AS rank
                FROM {candidate_from}
                LEFT JOIN m_workspace_sticker_favorites AS favorite
                  ON favorite.sticker_uuid = s.uuid
                 AND favorite.user_uuid = %s
               {where}
            )
            SELECT * FROM ranked
            {outer_where}
            ORDER BY {order}
            LIMIT %s
        """.format(
            columns=columns,
            rank=query_rank,
            candidate_from=candidate_from,
            where="WHERE " + " AND ".join(where) if where else "",
            outer_where=outer_where,
            order=order,
        )
        params: list[typing.Any] = []
        params.extend(rank_params)
        params.extend(candidate_params)
        params.append(user_uuid)
        if not query.q:
            if query.category:
                params.append(query.category)
            if query.format:
                params.append(query.format)
            if query.uuids:
                params.append(list(query.uuids))
        params.extend(marker_params)
        params.append(query.page_limit + 1)
        return statement, tuple(params)

    def get(
        self,
        session: typing.Any,
        user_uuid: sys_uuid.UUID,
        sticker_uuid: sys_uuid.UUID,
    ) -> StickerRecord | None:
        """Get one sticker, including its favorite state."""

        rows = session.execute(
            """
            SELECT s.*, favorite.created_at AS favorite_created_at,
                   favorite.sticker_uuid IS NOT NULL AS is_favorite
              FROM m_workspace_stickers AS s
              LEFT JOIN m_workspace_sticker_favorites AS favorite
                ON favorite.sticker_uuid = s.uuid AND favorite.user_uuid = %s
             WHERE s.uuid = %s
            """,
            (user_uuid, sticker_uuid),
        ).fetchall()
        return self._record_from_row(rows[0]) if rows else None

    def find_duplicates(
        self,
        session: typing.Any,
        sha256_values: typing.Iterable[str],
    ) -> dict[str, sys_uuid.UUID]:
        values = list(sha256_values)
        if not values:
            return {}
        rows = session.execute(
            "SELECT sha256, uuid FROM m_workspace_stickers WHERE sha256 = ANY(%s::text[])",
            (values,),
        ).fetchall()
        return {str(row["sha256"]): sys_uuid.UUID(str(row["uuid"])) for row in rows}

    def lock_sha256_values(
        self,
        session: typing.Any,
        sha256_values: typing.Iterable[str],
    ) -> None:
        """Serialize catalog mutations by SHA in one deterministic order."""

        for value in sorted(set(sha256_values)):
            session.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (value,),
            )

    def insert_batch(
        self,
        session: typing.Any,
        stickers: typing.Iterable[sticker_models.Sticker],
    ) -> dict[str, sys_uuid.UUID]:
        """Insert rows in the caller transaction and return SHA to UUID map."""

        values = list(stickers)
        for sticker in values:
            try:
                sticker.validate()
            except (TypeError, ValueError):
                raise StickerRepositoryValidationError() from None
            session.execute(
                """
                INSERT INTO m_workspace_stickers
                  (uuid, title, alt_text, emoji, tags, search_text, category,
                   format, width, height, size_bytes, sha256, media_object_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                """,
                (
                    sticker.uuid,
                    sticker.title,
                    sticker.alt_text,
                    list(sticker.emoji),
                    list(sticker.tags),
                    sticker.search_text,
                    sticker.category,
                    sticker.format,
                    sticker.width,
                    sticker.height,
                    sticker.size_bytes,
                    sticker.sha256,
                    sticker.media_object_id,
                ),
            )
        return self.find_duplicates(session, [sticker.sha256 for sticker in values])

    def update(
        self,
        session: typing.Any,
        user_uuid: sys_uuid.UUID,
        sticker_uuid: sys_uuid.UUID,
        fields: dict[str, typing.Any],
    ) -> StickerRecord | None:
        """Update only admin mutable fields and rebuild search text as needed."""

        unknown = set(fields).difference(sticker_models.STICKER_ADMIN_MUTABLE_FIELDS)
        if unknown:
            raise StickerRepositoryValidationError()
        if not fields:
            return self.get(session, user_uuid, sticker_uuid)
        current_rows = session.execute(
            "SELECT * FROM m_workspace_stickers WHERE uuid = %s FOR UPDATE",
            (sticker_uuid,),
        ).fetchall()
        if not current_rows:
            return None
        current = current_rows[0]
        update_fields = dict(fields)
        text_fields = {"title", "alt_text", "emoji", "tags"}
        if text_fields.intersection(update_fields):
            normalized = sticker_catalog.normalize_sticker_fields(
                update_fields.get("title", current["title"]),
                update_fields.get("alt_text", current["alt_text"]),
                update_fields.get("emoji", current["emoji"]),
                update_fields.get("tags", current["tags"]),
            )
            update_fields.update(normalized)
        candidate_values = {column: current[column] for column in _STICKER_COLUMNS}
        candidate_values["uuid"] = sys_uuid.UUID(str(candidate_values["uuid"]))
        candidate_values.update(update_fields)
        try:
            candidate = sticker_models.Sticker(**candidate_values)
            candidate.validate()
        except (TypeError, ValueError):
            raise StickerRepositoryValidationError() from None
        assignments = []
        params: list[typing.Any] = []
        allowed_columns = (
            "title",
            "alt_text",
            "emoji",
            "tags",
            "search_text",
            "category",
        )
        for column in allowed_columns:
            if column in update_fields:
                assignments.append("%s = %%s" % column)
                params.append(update_fields[column])
        params.extend((sticker_uuid, user_uuid))
        updated = session.execute(
            """
            UPDATE m_workspace_stickers AS sticker
               SET %s, updated_at = CURRENT_TIMESTAMP
             WHERE sticker.uuid = %%s
            RETURNING sticker.*, EXISTS (
                SELECT 1 FROM m_workspace_sticker_favorites
                WHERE sticker_uuid = sticker.uuid AND user_uuid = %%s
            ) AS is_favorite
            """
            % ", ".join(assignments),
            tuple(params),
        ).fetchone()
        return self._record_from_row(updated)

    def delete(
        self,
        session: typing.Any,
        sticker_uuid: sys_uuid.UUID,
    ) -> bool:
        """Delete one row and atomically enqueue its storage cleanup."""

        candidates = session.execute(
            "SELECT sha256 FROM m_workspace_stickers WHERE uuid = %s",
            (sticker_uuid,),
        ).fetchall()
        if not candidates:
            return False
        self.lock_sha256_values(session, [str(candidates[0]["sha256"])])
        rows = session.execute(
            """
            WITH deleted AS (
                DELETE FROM m_workspace_stickers
                WHERE uuid = %s
                RETURNING uuid, media_object_id, size_bytes, sha256
            )
            INSERT INTO messenger_sticker_cleanup_tasks (
                sticker_uuid, storage_object_id, size_bytes, sha256
            )
            SELECT uuid, media_object_id, size_bytes, sha256 FROM deleted
            RETURNING sticker_uuid
            """,
            (sticker_uuid,),
        ).fetchall()
        return bool(rows)

    def star(
        self,
        session: typing.Any,
        user_uuid: sys_uuid.UUID,
        sticker_uuid: sys_uuid.UUID,
    ) -> bool:
        """Idempotently star an existing sticker."""

        existing = session.execute(
            """
            WITH sticker AS (
                SELECT uuid FROM m_workspace_stickers WHERE uuid = %s FOR KEY SHARE
            ), starred AS (
                INSERT INTO m_workspace_sticker_favorites (user_uuid, sticker_uuid)
                SELECT %s, uuid FROM sticker
                ON CONFLICT (user_uuid, sticker_uuid) DO NOTHING
            )
            SELECT uuid FROM sticker
            """,
            (sticker_uuid, user_uuid),
        ).fetchone()
        if existing is None:
            raise StickerNotFoundError()
        return True

    def unstar(
        self,
        session: typing.Any,
        user_uuid: sys_uuid.UUID,
        sticker_uuid: sys_uuid.UUID,
    ) -> bool:
        session.execute(
            "DELETE FROM m_workspace_sticker_favorites WHERE user_uuid = %s AND sticker_uuid = %s",
            (user_uuid, sticker_uuid),
        )
        return True

    @staticmethod
    def _record_from_row(row: typing.Any) -> StickerRecord:
        values = {column: row[column] for column in _STICKER_COLUMNS}
        values["uuid"] = sys_uuid.UUID(str(values["uuid"]))
        model = sticker_models.Sticker(**values)
        try:
            favorite_created_at = row["favorite_created_at"]
        except (KeyError, IndexError, TypeError):
            favorite_created_at = None
        if favorite_created_at is not None and favorite_created_at.tzinfo is None:
            favorite_created_at = favorite_created_at.replace(
                tzinfo=datetime.timezone.utc
            )
        return StickerRecord(
            sticker=model,
            is_favorite=bool(_optional_row_value(row, "is_favorite", False)),
            rank=(
                float(_optional_row_value(row, "rank", None))
                if _optional_row_value(row, "rank", None) is not None
                else None
            ),
            favorite_created_at=favorite_created_at,
        )


def _optional_row_value(row: typing.Any, key: str, default: typing.Any) -> typing.Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default
