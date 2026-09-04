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

from workspace.messenger_api import sticker_repository
from workspace.messenger_api import sticker_storage
from workspace.messenger_api.dm import stickers as sticker_models


MEDIA_CONTENT_TYPES = {
    "gif": "image/gif",
    "webp": "image/webp",
    "png": "image/png",
}


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


def build_search_text(title: str, alt_text: str, tags: typing.Iterable[str]) -> str:
    parts = [
        normalize_title(title),
        normalize_alt_text(alt_text),
        *normalize_tags(tags),
    ]
    return normalize_search_text(" ".join(part for part in parts if part))


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
    q: str | None = None,
    favorite: bool = False,
    uuids: typing.Iterable[sys_uuid.UUID] | None = None,
    category: str | None = None,
    format: str | None = None,
    page_limit: int | None = None,
    page_marker: str | None = None,
    if_none_match: str | None = None,
) -> StickerHttpResponse:
    """Build one private, stable JSON catalog page for the current user."""

    try:
        actual_page_limit = validate_page_limit(page_limit)
    except (TypeError, ValueError):
        raise sticker_repository.StickerRepositoryValidationError() from None
    page = repository.list_stickers(
        session,
        user_uuid,
        q=q,
        favorite=favorite,
        uuids=uuids,
        category=category,
        format=format,
        page_limit=actual_page_limit,
        page_marker=page_marker,
    )
    body = _stable_json_bytes([_public_record(record) for record in page.items])
    etag = _etag(body)
    headers = {
        "Cache-Control": "private, no-cache",
        "Content-Type": "application/json; charset=UTF-8",
        "ETag": etag,
        "X-Pagination-Limit": str(actual_page_limit),
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
        raise sticker_repository.StickerNotFoundError()
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
    if not records:
        raise sticker_repository.StickerNotFoundError()
    sticker = records[0].sticker
    try:
        body = storage.read(sticker.media_object_id)
    except sticker_storage.StickerStorageNotFoundError:
        raise sticker_repository.StickerNotFoundError() from None
    return StickerHttpResponse(
        body=body,
        status=200,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Type": MEDIA_CONTENT_TYPES[sticker.format],
            "ETag": '"%s"' % sticker.sha256,
        },
    )


def validate_query(value: str) -> str:
    normalized = normalize_whitespace(value)
    if len(normalized) > sticker_models.MAX_QUERY_LENGTH:
        raise ValueError("q cannot exceed 200 characters")
    return normalize_search_text(normalized)


def validate_page_limit(value: int | None) -> int:
    if value is None:
        return sticker_models.DEFAULT_PAGE_LIMIT
    if type(value) is not int or not 1 <= value <= sticker_models.MAX_PAGE_LIMIT:
        raise ValueError("page_limit must be between 1 and 100")
    return value


def validate_uuid_filter(values: typing.Iterable[sys_uuid.UUID]) -> list[sys_uuid.UUID]:
    raw_values = list(values)
    if len(raw_values) > sticker_models.MAX_UUID_FILTER_COUNT:
        raise ValueError("uuid cannot contain more than 100 values")
    result = sorted({str(value) for value in raw_values})
    return [sys_uuid.UUID(value) for value in result]


def _stable_dedupe(values: typing.Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
