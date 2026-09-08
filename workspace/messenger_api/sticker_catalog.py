#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import dataclasses
import hashlib
import json
import typing
import uuid as sys_uuid

from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import sticker_storage
from workspace.messenger_api.dm import stickers as sticker_models

if typing.TYPE_CHECKING:
    from workspace.messenger_api import sticker_repository


MEDIA_CONTENT_TYPES = {
    "gif": "image/gif",
    "webp": "image/webp",
    "png": "image/png",
}
STICKER_CATALOG_MANAGE_PERMISSION = "workspace.sticker_catalog.manage"


@dataclasses.dataclass(frozen=True)
class StickerHttpResponse:
    """An application result that the RestAlchemy controller can return verbatim."""

    body: bytes | None
    status: int
    headers: dict[str, str]


class NormalizedStickerFields(typing.TypedDict):
    title: str
    alt_text: str
    emoji: list[str]
    tags: list[str]
    search_text: str


def normalize_whitespace(value: str) -> str:
    """Collapse Unicode whitespace and trim the value."""

    return " ".join(value.split())


def normalize_title(value: str) -> str:
    return normalize_whitespace(value)


def normalize_alt_text(value: str) -> str:
    return normalize_whitespace(value)


def normalize_emoji(values: typing.Iterable[str]) -> list[str]:
    """Deduplicate emoji in input order without inspecting media content."""

    normalized = [normalize_whitespace(value) for value in values]
    result = _stable_dedupe(value for value in normalized if value)
    if len(result) > sticker_models.MAX_EMOJI_COUNT:
        raise ValueError("emoji cannot contain more than 5 values")
    return result


def normalize_tags(values: typing.Iterable[str]) -> list[str]:
    """Normalize searchable tags and retain their first-seen order."""

    normalized = [normalize_whitespace(value).lower() for value in values]
    result = _stable_dedupe(value for value in normalized if value)
    if len(result) > sticker_models.MAX_TAG_COUNT:
        raise ValueError("tags cannot contain more than 64 values")
    if any(len(value) > sticker_models.MAX_TAG_LENGTH for value in result):
        raise ValueError("one tag exceeds 64 characters")
    if (
        sum(len(value.encode("utf-8")) for value in result)
        > sticker_models.MAX_TAGS_BYTES
    ):
        raise ValueError("tags exceed 4096 UTF-8 bytes")
    return result


def normalize_search_text(value: str) -> str:
    """Make search text case-insensitive and equivalent for ё and е."""

    return normalize_whitespace(value).lower().replace("ё", "е")


def build_list_query(
    *,
    q: str | None = None,
    favorite: str | bool | None = None,
    uuids: typing.Iterable[str | sys_uuid.UUID] = (),
    category: str | None = None,
    format: str | None = None,
    page_limit: str | int | None = None,
    page_marker: str | None = None,
) -> sticker_models.StickerListQuery:
    """Parse and normalize the public sticker list parameters once."""

    if q is not None and type(q) is not str:
        raise TypeError("q must be a string")
    normalized_query = normalize_whitespace(q or "").lower()
    if len(normalized_query) > sticker_models.MAX_QUERY_LENGTH:
        raise ValueError("q cannot exceed 200 characters")

    if favorite is None:
        normalized_favorite = False
    elif type(favorite) is bool:
        normalized_favorite = favorite
    elif favorite == "true":
        normalized_favorite = True
    elif favorite == "false":
        normalized_favorite = False
    else:
        raise ValueError("favorite must be true or false")

    raw_uuids = list(uuids)
    if len(raw_uuids) > sticker_models.MAX_UUID_FILTER_COUNT:
        raise ValueError("uuid cannot contain more than 100 values")
    parsed_uuids = [
        value if isinstance(value, sys_uuid.UUID) else sys_uuid.UUID(value)
        for value in raw_uuids
    ]
    canonical_uuids = [
        sys_uuid.UUID(value) for value in sorted({str(value) for value in parsed_uuids})
    ]

    if page_limit is None:
        normalized_page_limit = sticker_models.DEFAULT_PAGE_LIMIT
    elif type(page_limit) is int:
        normalized_page_limit = page_limit
    elif type(page_limit) is str:
        normalized_page_limit = int(page_limit)
    else:
        raise TypeError("page_limit must be an integer")

    return sticker_models.StickerListQuery(
        q=normalized_query.replace("ё", "е"),
        tag_query=normalized_query,
        favorite=normalized_favorite,
        uuids=canonical_uuids,
        category=category,
        format=format,
        page_limit=normalized_page_limit,
        page_marker=page_marker,
    )


def normalize_sticker_fields(
    title: str,
    alt_text: str,
    emoji: typing.Iterable[str],
    tags: typing.Iterable[str],
) -> NormalizedStickerFields:
    normalized_title = normalize_title(title)
    normalized_alt_text = normalize_alt_text(alt_text)
    normalized_emoji = normalize_emoji(emoji)
    normalized_tags = normalize_tags(tags)
    return {
        "title": normalized_title,
        "alt_text": normalized_alt_text,
        "emoji": normalized_emoji,
        "tags": normalized_tags,
        "search_text": normalize_search_text(
            " ".join(
                part
                for part in (
                    normalized_title,
                    normalized_alt_text,
                    *normalized_tags,
                )
                if part
            )
        ),
    }


def sticker_download_url(sticker_uuid: sys_uuid.UUID) -> str:
    return "/api/workspace/v1/messenger/stickers/%s/actions/download" % sticker_uuid


def build_public_card(
    sticker: sticker_models.Sticker,
    is_favorite: bool = False,
) -> sticker_models.StickerCard:
    media = sticker_models.StickerMedia(
        format=sticker.format,
        width=sticker.width,
        height=sticker.height,
        url=sticker_download_url(sticker.uuid),
    )
    return sticker_models.StickerCard(
        id=sticker.uuid,
        title=sticker.title,
        alt_text=sticker.alt_text,
        emoji=list(sticker.emoji),
        tags=list(sticker.tags),
        category=sticker.category,
        media=media,
        is_favorite=is_favorite,
    )


def public_card_dict(card: sticker_models.StickerCard) -> dict[str, object]:
    """Return exactly the public card fields, excluding storage identity."""

    media = card.media
    media_dict: dict[str, object] = {
        "format": media.format,
        "url": media.url,
    }
    if media.width is not None:
        media_dict["width"] = media.width
    if media.height is not None:
        media_dict["height"] = media.height
    return {
        "id": str(card.id),
        "title": card.title,
        "alt_text": card.alt_text,
        "emoji": list(card.emoji),
        "tags": list(card.tags),
        "category": card.category,
        "media": media_dict,
        "is_favorite": card.is_favorite,
    }


def _public_record(record: typing.Any) -> dict[str, object]:
    return public_card_dict(
        build_public_card(record.sticker, is_favorite=record.is_favorite)
    )


def _stable_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _etag(value: bytes) -> str:
    return '"%s"' % hashlib.sha256(value).hexdigest()


def list_public_stickers(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    *,
    query: sticker_models.StickerListQuery,
    if_none_match: str | None = None,
) -> StickerHttpResponse:
    """Build one private, stable JSON catalog page for the current user."""

    page = repository.list_stickers(session, user_uuid, query)
    body = _stable_json_bytes([_public_record(record) for record in page.items])
    etag = _etag(body)
    headers = {
        "Cache-Control": "private, no-cache",
        "Content-Type": "application/json; charset=UTF-8",
        "ETag": etag,
        "X-Pagination-Limit": str(query.page_limit),
    }
    if page.next_marker is not None:
        headers["X-Pagination-Marker"] = page.next_marker
    if if_none_match == etag:
        return StickerHttpResponse(body=None, status=304, headers=headers)
    return StickerHttpResponse(body=body, status=200, headers=headers)


def get_public_sticker(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    sticker_uuid: sys_uuid.UUID,
) -> dict[str, object]:
    record = repository.get_active(session, user_uuid, sticker_uuid)
    if record is None:
        raise ra_exc.ResourceNotFoundError(
            resource="Sticker",
            path=str(sticker_uuid),
        )
    return _public_record(record)


def resolve_public_stickers(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    sticker_uuids: typing.Iterable[sys_uuid.UUID],
) -> list[dict[str, object]]:
    records = repository.resolve_batch(session, user_uuid, sticker_uuids)
    return [_public_record(record) for record in records]


def download_sticker(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    storage: typing.Any,
    sticker_uuid: sys_uuid.UUID,
) -> StickerHttpResponse:
    """Read media only after the catalog row is known to be downloadable."""

    records = repository.resolve_batch(session, user_uuid, [sticker_uuid])
    if not records or records[0].sticker.blocked:
        raise ra_exc.ResourceNotFoundError(
            resource="Sticker",
            path=str(sticker_uuid),
        )
    sticker = records[0].sticker
    try:
        body = storage.read(sticker.media_object_id)
    except sticker_storage.StickerStorageNotFoundError:
        raise ra_exc.ResourceNotFoundError(
            resource="Sticker",
            path=str(sticker_uuid),
        ) from None
    return StickerHttpResponse(
        body=body,
        status=200,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Type": MEDIA_CONTENT_TYPES[sticker.format],
            "ETag": '"%s"' % sticker.sha256,
        },
    )


def star_sticker(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    sticker_uuid: sys_uuid.UUID,
) -> None:
    """Idempotently add one visible sticker to the current user's favorites."""

    repository.star(session, user_uuid, sticker_uuid)


def unstar_sticker(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    sticker_uuid: sys_uuid.UUID,
) -> None:
    """Idempotently remove one sticker from the current user's favorites."""

    repository.unstar(session, user_uuid, sticker_uuid)


def update_sticker(
    session: typing.Any,
    user_uuid: sys_uuid.UUID,
    repository: typing.Any,
    sticker_uuid: sys_uuid.UUID,
    values: dict[str, typing.Any],
) -> "sticker_repository.StickerRecord":
    """Update only catalog metadata; media identity remains immutable."""

    if not isinstance(values, dict) or not values:
        raise ra_exc.ValidationErrorException()
    if set(values).difference(sticker_models.STICKER_ADMIN_MUTABLE_FIELDS):
        raise ra_exc.ValidationErrorException()
    updated = repository.update_admin_fields(session, sticker_uuid, values)
    if updated is None:
        raise ra_exc.ResourceNotFoundError(
            resource="Sticker",
            path=str(sticker_uuid),
        )
    record = repository.get_any(session, user_uuid, sticker_uuid)
    if record is None:
        raise ra_exc.ResourceNotFoundError(
            resource="Sticker",
            path=str(sticker_uuid),
        )
    return record


def _stable_dedupe(values: typing.Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
