#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0

import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api import sticker_catalog
from workspace.messenger_api.dm import stickers


CLIENT_UUID = sys_uuid.UUID("20000000-0000-0000-0000-000000000000")
STICKER_UUID = sys_uuid.UUID("30000000-0000-0000-0000-000000000000")
SHA256 = "a" * 64


def _manifest_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "client_id": str(CLIENT_UUID),
        "file": "media/20000000-0000-0000-0000-000000000000.gif",
        "sha256": SHA256,
        "format": "gif",
        "title": "Одобрение",
        "alt_text": "",
        "emoji": ["👍", "👍", "✅"],
        "tags": [" Да ", "согласие", "да"],
    }
    item.update(overrides)
    return item


def test_manifest_v1_defaults_category_and_accepts_optional_dimensions() -> None:
    item = stickers.StickerManifestItem.from_simple_type(_manifest_item())
    assert item.category == "sticker"
    assert item.width is None
    assert item.height is None

    with_dimensions = stickers.StickerManifestItem.from_simple_type(
        _manifest_item(width=240, height=None)
    )
    assert with_dimensions.width == 240
    assert with_dimensions.height is None


def test_manifest_v1_is_strict_about_root_and_item_fields() -> None:
    with pytest.raises(ValueError, match="Unknown fields"):
        stickers.StickerManifest.from_simple_type(
            {"schema_version": 1, "items": [], "pack": {}}
        )
    with pytest.raises(ValueError, match="Unknown fields"):
        stickers.StickerManifestItem(**_manifest_item(generator="test"))


def test_manifest_v1_rejects_bad_limits_but_does_not_validate_media_content() -> None:
    with pytest.raises(Exception):
        stickers.StickerManifestItem.from_simple_type(
            _manifest_item(title="x" * (stickers.MAX_TITLE_LENGTH + 1))
        )
    with pytest.raises(ValueError):
        stickers.StickerManifestItem.from_simple_type(
            _manifest_item(tags=["x"] * (stickers.MAX_TAG_COUNT + 1))
        )
    with pytest.raises(Exception):
        stickers.StickerManifestItem.from_simple_type(_manifest_item(width=0))

    item = stickers.StickerManifestItem.from_simple_type(
        _manifest_item(sha256="b" * 64)
    )
    assert item.sha256 == "b" * 64


def test_normalization_preserves_order_and_excludes_emoji_from_search() -> None:
    normalized = sticker_catalog.normalize_sticker_fields(
        "  Ёлка\tодобрения  ",
        "  Большое   спасибо ",
        ["👍", "✅", "👍"],
        [" Да ", "СОГЛАСИЕ", "да", "ё"],
    )
    assert normalized == {
        "title": "Ёлка одобрения",
        "alt_text": "Большое спасибо",
        "emoji": ["👍", "✅"],
        "tags": ["да", "согласие", "ё"],
        "search_text": "елка одобрения большое спасибо да согласие е",
    }
    assert "👍" not in normalized["search_text"]


def test_gate_g0_query_page_and_uuid_limits() -> None:
    assert sticker_catalog.validate_query("  ЁЖ  ") == "еж"
    assert sticker_catalog.validate_page_limit(None) == stickers.DEFAULT_PAGE_LIMIT
    assert sticker_catalog.validate_page_limit(100) == 100
    with pytest.raises(ValueError):
        sticker_catalog.validate_query("x" * (stickers.MAX_QUERY_LENGTH + 1))
    with pytest.raises(ValueError):
        sticker_catalog.validate_page_limit(0)
    with pytest.raises(ValueError):
        sticker_catalog.validate_uuid_filter([sys_uuid.uuid4()] * 101)


def test_public_card_has_no_storage_fields_and_uses_exact_download_action() -> None:
    sticker = stickers.Sticker(
        uuid=STICKER_UUID,
        title="Одобрение",
        alt_text="",
        emoji=["👍"],
        tags=["да"],
        search_text="одобрение да",
        category="gif",
        format="gif",
        width=None,
        height=180,
        size_bytes=12,
        sha256=SHA256,
        media_object_id="bucket/secret-key",
    )
    card = sticker_catalog.build_public_card(sticker, is_favorite=True)
    encoded = json.dumps(sticker_catalog.public_card_dict(card), ensure_ascii=False)
    assert "media_object_id" not in encoded
    assert "secret-key" not in encoded
    assert card.media.url == (
        "/api/workspace/v1/messenger/stickers/"
        "30000000-0000-0000-0000-000000000000/actions/download"
    )
    assert card.media.width is None
    assert card.media.height == 180
    assert card.is_favorite is True


def test_sticker_field_permissions_match_admin_contract() -> None:
    props = stickers.Sticker.properties.properties
    for field in stickers.STICKER_ADMIN_MUTABLE_FIELDS:
        assert not props[field].get_kwargs().get("read_only", False)
    for field in stickers.STICKER_INTERNAL_FIELDS | {
        "uuid",
        "created_at",
        "updated_at",
    }:
        assert props[field].get_kwargs().get("read_only", False)


def test_import_result_has_only_created_and_duplicate_statuses() -> None:
    result = stickers.StickerImportResult(
        created=1,
        duplicates=1,
        items=[
            stickers.StickerImportItemResult(
                client_id=CLIENT_UUID,
                sticker_uuid=STICKER_UUID,
                status="created",
            ),
            stickers.StickerImportItemResult(
                client_id=sys_uuid.uuid4(),
                sticker_uuid=STICKER_UUID,
                status="duplicate",
            ),
        ],
    )
    assert [item.status for item in result["items"]] == ["created", "duplicate"]
    with pytest.raises(Exception):
        stickers.StickerImportItemResult(
            client_id=CLIENT_UUID,
            sticker_uuid=STICKER_UUID,
            status="rejected",
        )
