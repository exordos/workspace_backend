# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
import hashlib
import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.dm import stickers as sticker_models


USER_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
STICKER_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000001")
SHA256 = hashlib.sha256(b"media").hexdigest()


def _record(
    *,
    active: bool = True,
    blocked: bool = False,
    is_favorite: bool = True,
    media_object_id: str = "stickers/moved/media.gif",
) -> sticker_repository.StickerRecord:
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return sticker_repository.StickerRecord(
        sticker=sticker_models.Sticker(
            uuid=STICKER_UUID,
            title="Одобрение",
            alt_text="Мужчина кивает",
            emoji=["👍"],
            tags=["да"],
            search_text="одобрение мужчина кивает да",
            category="gif",
            format="gif",
            width=None,
            height=180,
            size_bytes=5,
            sha256=SHA256,
            media_object_id=media_object_id,
            active=active,
            blocked=blocked,
            created_at=now,
            updated_at=now,
        ),
        is_favorite=is_favorite,
    )


class _Repository:
    def __init__(self, *, active=None, resolved=None, page=None):
        self.active = active
        self.resolved = list(resolved or [])
        self.page = page or sticker_repository.StickerPage([])
        self.calls = []

    def list_stickers(self, session, user_uuid, **kwargs):
        self.calls.append(("list", session, user_uuid, kwargs))
        return self.page

    def get_active(self, session, user_uuid, sticker_uuid):
        self.calls.append(("get", session, user_uuid, sticker_uuid))
        return self.active

    def resolve_batch(self, session, user_uuid, sticker_uuids):
        self.calls.append(("resolve", session, user_uuid, list(sticker_uuids)))
        return self.resolved


class _Storage:
    def __init__(self, body=b"media", error=None):
        self.body = body
        self.error = error
        self.calls = []

    def read(self, storage_object_id):
        self.calls.append(storage_object_id)
        if self.error is not None:
            raise self.error
        return self.body


def test_list_response_is_stable_private_json_with_exact_pagination_headers():
    marker = "opaque-marker"
    repository = _Repository(
        page=sticker_repository.StickerPage([_record()], next_marker=marker)
    )
    session = object()

    response = sticker_catalog.list_public_stickers(
        session,
        USER_UUID,
        repository,
        q="да",
        favorite=True,
        page_limit=25,
    )

    expected = json.dumps(
        [
            {
                "id": str(STICKER_UUID),
                "title": "Одобрение",
                "alt_text": "Мужчина кивает",
                "emoji": ["👍"],
                "tags": ["да"],
                "category": "gif",
                "media": {
                    "format": "gif",
                    "height": 180,
                    "url": (
                        "/api/workspace/v1/messenger/stickers/"
                        f"{STICKER_UUID}/actions/download"
                    ),
                },
                "is_favorite": True,
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert response.body == expected
    assert response.status == 200
    assert response.headers == {
        "Cache-Control": "private, no-cache",
        "Content-Type": "application/json; charset=UTF-8",
        "ETag": '"%s"' % hashlib.sha256(expected).hexdigest(),
        "X-Pagination-Limit": "25",
        "X-Pagination-Marker": marker,
    }
    assert repository.calls == [
        (
            "list",
            session,
            USER_UUID,
            {
                "q": "да",
                "favorite": True,
                "uuids": None,
                "category": None,
                "format": None,
                "page_limit": 25,
                "page_marker": None,
            },
        )
    ]
    assert b"media_object_id" not in response.body
    assert b"stickers/moved" not in response.body


def test_list_matching_if_none_match_returns_304_without_body():
    repository = _Repository(page=sticker_repository.StickerPage([_record()]))
    first = sticker_catalog.list_public_stickers(
        object(), USER_UUID, repository, page_limit=None
    )

    cached = sticker_catalog.list_public_stickers(
        object(),
        USER_UUID,
        repository,
        page_limit=None,
        if_none_match=first.headers["ETag"],
    )

    assert cached.status == 304
    assert cached.body is None
    assert cached.headers["ETag"] == first.headers["ETag"]
    assert cached.headers["Cache-Control"] == "private, no-cache"
    assert cached.headers["X-Pagination-Limit"] == "50"
    assert "X-Pagination-Marker" not in cached.headers


def test_get_uses_active_lookup_and_returns_safe_not_found():
    repository = _Repository(active=_record())
    card = sticker_catalog.get_public_sticker(
        object(), USER_UUID, repository, STICKER_UUID
    )
    assert card["id"] == str(STICKER_UUID)
    assert card["is_favorite"] is True

    with pytest.raises(sticker_repository.StickerNotFoundError):
        sticker_catalog.get_public_sticker(
            object(), USER_UUID, _Repository(), STICKER_UUID
        )


def test_batch_resolve_preserves_hidden_and_excludes_repository_blocked_rows():
    hidden = _record(active=False)
    repository = _Repository(resolved=[hidden])

    cards = sticker_catalog.resolve_public_stickers(
        object(), USER_UUID, repository, [STICKER_UUID]
    )

    assert [card["id"] for card in cards] == [str(STICKER_UUID)]
    assert repository.calls[0][0] == "resolve"


@pytest.mark.parametrize("available", [False, True])
def test_download_checks_database_before_storage_and_uses_stored_object_id(available):
    record = _record(active=False)
    repository = _Repository(resolved=[record] if available else [])
    storage = _Storage()

    if not available:
        with pytest.raises(sticker_repository.StickerNotFoundError):
            sticker_catalog.download_sticker(
                object(), USER_UUID, repository, storage, STICKER_UUID
            )
        assert storage.calls == []
        return

    response = sticker_catalog.download_sticker(
        object(), USER_UUID, repository, storage, STICKER_UUID
    )
    assert storage.calls == ["stickers/moved/media.gif"]
    assert response.body == b"media"
    assert response.status == 200
    assert response.headers == {
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Type": "image/gif",
        "ETag": f'"{SHA256}"',
    }


def test_download_maps_missing_storage_object_to_safe_not_found():
    storage = _Storage(error=sticker_storage.StickerStorageNotFoundError())

    with pytest.raises(sticker_repository.StickerNotFoundError) as error:
        sticker_catalog.download_sticker(
            object(),
            USER_UUID,
            _Repository(resolved=[_record()]),
            storage,
            STICKER_UUID,
        )

    assert "stickers/moved" not in str(error.value)
