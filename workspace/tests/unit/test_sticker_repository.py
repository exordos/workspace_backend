import base64
import datetime
import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_repository


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _UpdateSession:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        return _Rows([] if self.row is None else [self.row])


class _DeleteSession:
    def __init__(self, *, candidate=True, deleted=True):
        self.candidate = candidate
        self.deleted = deleted
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((statement, params))
        if "SELECT sha256 FROM m_workspace_stickers" in statement:
            rows = [{"sha256": "a" * 64}] if self.candidate else []
        elif "INSERT INTO messenger_sticker_cleanup_tasks" in statement:
            rows = [{"sticker_uuid": params[0]}] if self.deleted else []
        else:
            rows = []
        return _Rows(rows)


def test_marker_is_unpadded_canonical_json_and_round_trips() -> None:
    marker = sticker_repository.encode_page_marker(
        sort=sticker_repository.SORT_RANK_UPDATED,
        filters_sha256="a" * 64,
        values=(
            1.25,
            datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
            sys_uuid.UUID(int=1),
        ),
    )
    assert "=" not in marker
    decoded = sticker_repository.decode_page_marker(marker)
    assert decoded["v"] == 1
    assert decoded["sort"] == sticker_repository.SORT_RANK_UPDATED
    assert decoded["values"][0] == 1.25


def test_marker_rejects_noncanonical_or_tampered_payload() -> None:
    marker = sticker_repository.encode_page_marker(
        sort=sticker_repository.SORT_CREATED,
        filters_sha256="b" * 64,
        values=(
            datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc),
            sys_uuid.UUID(int=2),
        ),
    )
    raw = base64.urlsafe_b64decode(marker + "==")
    payload = json.loads(raw)
    payload["v"] = 2
    changed = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    with pytest.raises(sticker_repository.StickerRepositoryValidationError):
        sticker_repository.decode_page_marker(changed)


@pytest.mark.parametrize("value", ["", "not base64", "e30", "abc="])
def test_marker_rejects_malformed_values(value: str) -> None:
    with pytest.raises(sticker_repository.StickerRepositoryValidationError):
        sticker_repository.decode_page_marker(value)


def test_fingerprint_uses_canonical_list_query() -> None:
    first = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
    second = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
    left = sticker_repository.filters_fingerprint(
        query=sticker_catalog.build_list_query(
            q="кот",
            uuids=[first, second, first],
        ),
        sort=sticker_repository.SORT_CREATED,
    )
    right = sticker_repository.filters_fingerprint(
        query=sticker_catalog.build_list_query(q="кот", uuids=[second, first]),
        sort=sticker_repository.SORT_CREATED,
    )
    assert left == right
    assert left == ("83d81dd61b3c7ff19478880d1ec379a6e9f8e376710b075942496e29392c4b38")


def test_query_selects_each_d01_sort_and_parameterizes_search() -> None:
    repository = sticker_repository.StickerRepository()
    user_uuid = sys_uuid.uuid4()
    expected = {
        (False, False): sticker_repository.SORT_CREATED,
        (False, True): sticker_repository.SORT_FAVORITE,
        (True, False): sticker_repository.SORT_RANK_UPDATED,
        (True, True): sticker_repository.SORT_RANK_FAVORITE,
    }
    for has_q, favorite in expected:
        query = repository._query(
            sticker_catalog.build_list_query(
                q="кот" if has_q else None,
                favorite=favorite,
                page_limit=10,
            )
        )
        statement, params = repository._list_statement(query, user_uuid, None)
        assert query.sort == expected[(has_q, favorite)]
        assert "кот" not in statement
        assert "AND AND" not in statement
        assert statement.count("%s") == len(params)


def test_search_parameter_order_matches_cte_placeholder_order() -> None:
    repository = sticker_repository.StickerRepository()
    user_uuid = sys_uuid.uuid4()
    first = sys_uuid.uuid4()
    query = repository._query(
        sticker_catalog.build_list_query(
            q="кот",
            category="sticker",
            format="png",
            uuids=[first],
            page_limit=10,
        )
    )
    statement, params = repository._list_statement(query, user_uuid, None)
    assert statement.count("%s") == len(params)
    assert "unnest(s.tags)" in statement
    assert "replace(lower(s.search_text)" not in statement
    assert "s.search_text LIKE" in statement
    assert params[:5] == ("кот",) * 5
    assert params[5:9] == ("sticker", "png", [first], "кот")
    assert params[9:13] == ("sticker", "png", [first], "кот")
    assert params[13:17] == ("sticker", "png", [first], "кот")
    assert params[17:21] == ("sticker", "png", [first], "кот")
    assert params[21] == user_uuid
    assert params[22:] == (11,)


def test_search_keeps_raw_yo_spelling_for_exact_tag_candidates() -> None:
    query = sticker_repository.StickerRepository()._query(
        sticker_catalog.build_list_query(q="берёза", page_limit=10)
    )
    assert query.q == "береза"
    assert query.tag_query == "берёза"


def test_uuid_only_batch_allows_hidden_but_search_and_favorite_do_not() -> None:
    repository = sticker_repository.StickerRepository()
    user_uuid = sys_uuid.uuid4()
    sticker_uuid = sys_uuid.uuid4()

    uuid_query = repository._query(
        sticker_catalog.build_list_query(uuids=[sticker_uuid], page_limit=10)
    )
    uuid_statement, _ = repository._list_statement(uuid_query, user_uuid, None)
    assert "s.blocked = FALSE" in uuid_statement
    assert "s.active = TRUE" not in uuid_statement

    for favorite, q in ((False, "кот"), (True, None)):
        query = repository._query(
            sticker_catalog.build_list_query(
                q=q,
                favorite=favorite,
                uuids=[sticker_uuid],
                page_limit=10,
            )
        )
        statement, _ = repository._list_statement(query, user_uuid, None)
        assert "s.active = TRUE" in statement
        assert "s.blocked = FALSE" in statement


def test_rank_marker_rejects_non_finite_values() -> None:
    payload = (
        '{"filters_sha256":"%s","sort":"%s","v":1,'
        '"values":[NaN,"2026-01-01T00:00:00+00:00",'
        '"00000000-0000-0000-0000-000000000001"]}'
        % ("a" * 64, sticker_repository.SORT_RANK_UPDATED)
    )
    marker = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    decoded = sticker_repository.decode_page_marker(marker)
    with pytest.raises(sticker_repository.StickerRepositoryValidationError):
        sticker_repository._parse_marker_values(
            decoded, sticker_repository.SORT_RANK_UPDATED
        )


def test_update_locks_row_before_rebuilding_search_text() -> None:
    sticker_uuid = sys_uuid.uuid4()
    timestamp = datetime.datetime.now(datetime.timezone.utc)
    row = {
        "uuid": sticker_uuid,
        "title": "Old title",
        "alt_text": "Old alt text",
        "emoji": [],
        "tags": ["old"],
        "search_text": "old title old alt text old",
        "category": "sticker",
        "format": "png",
        "width": None,
        "height": None,
        "size_bytes": 1,
        "sha256": "a" * 64,
        "media_object_id": "stickers/internal/media.png",
        "active": True,
        "blocked": False,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    session = _UpdateSession(row)

    sticker_repository.StickerRepository().update(
        session,
        sticker_uuid,
        {"title": "New title"},
    )

    first_statement, first_params = session.calls[0]
    assert "SELECT * FROM m_workspace_stickers WHERE uuid = %s FOR UPDATE" in (
        first_statement
    )
    assert first_params == (sticker_uuid,)
    assert session.calls[1][0].startswith("UPDATE m_workspace_stickers")

    no_op_session = _UpdateSession(row)
    no_op = sticker_repository.StickerRepository().update(
        no_op_session,
        sticker_uuid,
        {},
    )
    assert no_op is not None
    assert len(no_op_session.calls) == 1

    missing_session = _UpdateSession(None)
    assert (
        sticker_repository.StickerRepository().update(
            missing_session,
            sticker_uuid,
            {"title": "New title"},
        )
        is None
    )
    assert len(missing_session.calls) == 1


def test_delete_locks_sha_then_atomically_enqueues_root_cleanup() -> None:
    sticker_uuid = sys_uuid.uuid4()
    session = _DeleteSession()

    result = sticker_repository.StickerRepository().delete(session, sticker_uuid)

    assert result is True
    assert "SELECT sha256 FROM m_workspace_stickers" in session.calls[0][0]
    assert "pg_advisory_xact_lock" in session.calls[1][0]
    assert session.calls[1][1] == ("a" * 64,)
    assert "DELETE FROM m_workspace_stickers" in session.calls[2][0]
    assert "INSERT INTO messenger_sticker_cleanup_tasks" in session.calls[2][0]
    assert session.calls[2][1] == (sticker_uuid,)


def test_delete_returns_false_for_missing_row() -> None:
    session = _DeleteSession(candidate=False)

    assert (
        sticker_repository.StickerRepository().delete(session, sys_uuid.uuid4())
        is False
    )
    assert len(session.calls) == 1


def test_sha_locks_are_deduplicated_and_sorted() -> None:
    session = _DeleteSession()

    sticker_repository.StickerRepository().lock_sha256_values(
        session,
        ["b" * 64, "a" * 64, "b" * 64],
    )

    assert [params for _statement, params in session.calls] == [
        ("a" * 64,),
        ("b" * 64,),
    ]
    assert all("pg_advisory_xact_lock" in statement for statement, _ in session.calls)


@pytest.mark.parametrize(
    "text, escaped",
    [("%", "!%"), ("_", "!_"), ("\\", "\\"), ("!", "!!"), ("a!%_\\b", "a!!!%!_\\b")],
)
def test_search_escapes_only_like_parameters(text, escaped) -> None:
    repository = sticker_repository.StickerRepository()
    query = repository._query(sticker_catalog.build_list_query(q=text))
    statement, params = repository._list_statement(query, sys_uuid.UUID(int=1), None)

    assert statement.count("ESCAPE '!'") == 3
    assert params[:5] == (text, text, escaped, escaped, text)
    assert params[5:9] == (text, text, escaped, text)


@pytest.mark.parametrize("favorite", [False, True])
@pytest.mark.parametrize("rank", [10**400, -(10**400), 1e300, -1e300, -0.1, 4.1])
def test_list_rejects_out_of_range_rank_before_sql(favorite, rank) -> None:
    repository = sticker_repository.StickerRepository()
    query = sticker_catalog.build_list_query(q="cat", favorite=favorite)
    prepared = repository._query(query)
    marker = sticker_repository.encode_page_marker(
        sort=prepared.sort,
        filters_sha256=prepared.fingerprint,
        values=(rank, "2026-01-01T00:00:00+00:00", sys_uuid.UUID(int=1)),
    )
    query = sticker_catalog.build_list_query(
        q="cat", favorite=favorite, page_marker=marker
    )
    with pytest.raises(sticker_repository.StickerRepositoryValidationError):
        repository.list_stickers(None, sys_uuid.UUID(int=2), query)


@pytest.mark.parametrize("rank", [0, 4])
@pytest.mark.parametrize(
    "sort",
    [sticker_repository.SORT_RANK_UPDATED, sticker_repository.SORT_RANK_FAVORITE],
)
def test_rank_marker_accepts_inclusive_bounds(rank, sort) -> None:
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    sticker_uuid = sys_uuid.UUID(int=1)
    marker = sticker_repository.encode_page_marker(
        sort=sort,
        filters_sha256="a" * 64,
        values=(rank, timestamp, sticker_uuid),
    )
    assert sticker_repository._parse_marker_values(
        sticker_repository.decode_page_marker(marker), sort
    ) == (float(rank), timestamp, sticker_uuid)
