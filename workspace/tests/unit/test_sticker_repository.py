import base64
import datetime
import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_repository


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


def test_sha_locks_are_deduplicated_and_sorted() -> None:
    class Session:
        def __init__(self):
            self.locked = []

        def execute(self, statement, params):
            assert "pg_advisory_xact_lock" in statement
            self.locked.append(params[0])

    session = Session()
    sticker_repository.StickerRepository().lock_sha256_values(
        session,
        ["b" * 64, "a" * 64, "b" * 64],
    )

    assert session.locked == ["a" * 64, "b" * 64]


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
