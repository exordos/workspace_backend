import base64
import datetime
import json
import uuid as sys_uuid

import pytest

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


def test_fingerprint_canonicalizes_uuid_order_and_duplicates() -> None:
    first = sys_uuid.UUID("00000000-0000-0000-0000-000000000002")
    second = sys_uuid.UUID("00000000-0000-0000-0000-000000000001")
    left = sticker_repository.filters_fingerprint(
        q="кот",
        favorite=False,
        category=None,
        format=None,
        uuids=[first, second, first],
        sort=sticker_repository.SORT_CREATED,
    )
    right = sticker_repository.filters_fingerprint(
        q="кот",
        favorite=False,
        category=None,
        format=None,
        uuids=[second, first],
        sort=sticker_repository.SORT_CREATED,
    )
    assert left == right


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
            q="кот" if has_q else None,
            favorite=favorite,
            category=None,
            format=None,
            uuids=None,
            page_limit=10,
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
        q="кот",
        favorite=False,
        category="sticker",
        format="png",
        uuids=[first],
        page_limit=10,
    )
    statement, params = repository._list_statement(query, user_uuid, None)
    assert statement.count("%s") == len(params)
    assert "unnest(s.tags)" not in statement
    assert "replace(lower(s.search_text)" not in statement
    assert "s.search_text LIKE" in statement
    assert params[:6] == ("кот",) * 6
    assert params[6] == user_uuid
    assert params[7:9] == ("sticker", "png")
    assert params[9] == [first]
    assert params[10:] == ("кот",) * 6 + (11,)


def test_uuid_only_batch_allows_hidden_but_search_and_favorite_do_not() -> None:
    repository = sticker_repository.StickerRepository()
    user_uuid = sys_uuid.uuid4()
    sticker_uuid = sys_uuid.uuid4()

    uuid_query = repository._query(
        q=None,
        favorite=False,
        category=None,
        format=None,
        uuids=[sticker_uuid],
        page_limit=10,
    )
    uuid_statement, _ = repository._list_statement(uuid_query, user_uuid, None)
    assert "s.blocked = FALSE" in uuid_statement
    assert "s.active = TRUE" not in uuid_statement

    for favorite, q in ((False, "кот"), (True, None)):
        query = repository._query(
            q=q,
            favorite=favorite,
            category=None,
            format=None,
            uuids=[sticker_uuid],
            page_limit=10,
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
