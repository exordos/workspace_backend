# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Sticker catalog acceptance tests through real WSGI and PostgreSQL."""

import hashlib
import io
import json
import pathlib
import uuid as sys_uuid
import zipfile

from workspace.messenger_api import file_storage
from workspace.messenger_api import sticker_storage
from workspace.tests.integration import conftest


MANAGE_PERMISSION = "workspace.sticker_catalog.manage"
UNIFIED_ROOT = "/v1/messenger/stickers"
STANDALONE_ROOT = "/v1/stickers"
PUBLIC_FIELDS = {
    "id",
    "title",
    "alt_text",
    "emoji",
    "tags",
    "category",
    "media",
    "is_favorite",
}
INTERNAL_FIELDS = {
    "uuid",
    "search_text",
    "size_bytes",
    "sha256",
    "media_object_id",
    "active",
    "blocked",
    "created_at",
    "updated_at",
}


def _manifest_item(
    client_id: sys_uuid.UUID,
    data: bytes,
    *,
    format: str,
    title: str,
    category: str,
    tags: list[str],
    width: int | None = None,
    height: int | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "client_id": str(client_id),
        "file": f"media/{client_id}.{format}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": format,
        "title": title,
        "alt_text": f"Accessible {title}",
        "emoji": ["👍"],
        "tags": tags,
        "category": category,
    }
    if width is not None:
        item["width"] = width
    if height is not None:
        item["height"] = height
    return item


def _archive(
    items: list[dict[str, object]],
    files: dict[str, bytes],
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"schema_version": 1, "items": items}),
        )
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _assert_public_card(card: dict[str, object]) -> None:
    assert set(card) == PUBLIC_FIELDS
    assert not INTERNAL_FIELDS.intersection(card)
    media = card["media"]
    assert isinstance(media, dict)
    assert set(media) <= {"format", "width", "height", "url"}
    assert {"format", "url"} <= set(media)
    assert not INTERNAL_FIELDS.intersection(media)
    assert media["url"] == (
        f"/api/workspace/v1/messenger/stickers/{card['id']}/actions/download"
    )


def _delete_imported_stickers(db, digests: list[str]) -> None:
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_stickers WHERE sha256 = ANY(%s)",
            (digests,),
        )


def test_real_http_catalog_full_flow_through_unified_mount(
    workspace_api,
    api,
    db,
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    token = f"acceptance{sys_uuid.uuid4().hex}"
    search_token = f"approval{sys_uuid.uuid4().hex}"
    first_client_id = sys_uuid.uuid4()
    second_client_id = sys_uuid.uuid4()
    first_data = f"first-{token}".encode()
    second_data = f"second-{token}".encode()
    first_item = _manifest_item(
        first_client_id,
        first_data,
        format="gif",
        title=f"Approval {token}",
        category="gif",
        tags=[token, search_token],
        width=240,
        height=180,
    )
    second_item = _manifest_item(
        second_client_id,
        second_data,
        format="png",
        title=f"Sticker {token}",
        category="sticker",
        tags=[token, "static"],
    )
    archive = _archive(
        [first_item, second_item],
        {
            str(first_item["file"]): first_data,
            str(second_item["file"]): second_data,
        },
    )
    digests = [hashlib.sha256(data).hexdigest() for data in (first_data, second_data)]

    try:
        imported = workspace_api.post(
            f"{UNIFIED_ROOT}/actions/import_archive/invoke",
            permissions=(MANAGE_PERMISSION,),
            files={
                "archive": (
                    "stickers.zip",
                    io.BytesIO(archive),
                    "application/zip",
                )
            },
        )
        assert imported.status_code == 200, imported.text
        import_payload = imported.json()
        assert import_payload["created"] == 2
        assert import_payload["duplicates"] == 0
        assert {item["status"] for item in import_payload["items"]} == {"created"}
        assert all(
            set(item) == {"client_id", "file", "status", "sticker_uuid"}
            for item in import_payload["items"]
        )
        by_client_id = {
            item["client_id"]: item["sticker_uuid"] for item in import_payload["items"]
        }
        first_uuid = by_client_id[str(first_client_id)]
        second_uuid = by_client_id[str(second_client_id)]
        uuid_params = [("uuid", first_uuid), ("uuid", second_uuid)]

        first_page = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=[*uuid_params, ("page_limit", "1")],
        )
        assert first_page.status_code == 200, first_page.text
        assert first_page.headers["Cache-Control"] == "private, no-cache"
        assert first_page.headers["X-Pagination-Limit"] == "1"
        assert first_page.headers["ETag"].startswith('"')
        marker = first_page.headers["X-Pagination-Marker"]
        first_page_cards = first_page.json()
        assert len(first_page_cards) == 1
        _assert_public_card(first_page_cards[0])

        not_modified = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=[*uuid_params, ("page_limit", "1")],
            headers={"If-None-Match": first_page.headers["ETag"]},
        )
        assert not_modified.status_code == 304, not_modified.text
        assert not_modified.content == b""
        assert not_modified.headers["ETag"] == first_page.headers["ETag"]

        second_page = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=[
                *uuid_params,
                ("page_limit", "1"),
                ("page_marker", marker),
            ],
        )
        assert second_page.status_code == 200, second_page.text
        assert "X-Pagination-Marker" not in second_page.headers
        second_page_cards = second_page.json()
        assert len(second_page_cards) == 1
        _assert_public_card(second_page_cards[0])
        assert {
            first_page_cards[0]["id"],
            second_page_cards[0]["id"],
        } == {first_uuid, second_uuid}

        searched = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params={"q": search_token},
        )
        assert searched.status_code == 200, searched.text
        assert [card["id"] for card in searched.json()] == [first_uuid]
        _assert_public_card(searched.json()[0])

        category_filtered = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=[*uuid_params, ("category", "sticker")],
        )
        assert category_filtered.status_code == 200, category_filtered.text
        assert [card["id"] for card in category_filtered.json()] == [second_uuid]
        format_filtered = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=[*uuid_params, ("format", "png")],
        )
        assert format_filtered.status_code == 200, format_filtered.text
        assert [card["id"] for card in format_filtered.json()] == [second_uuid]

        detail = workspace_api.get(f"{UNIFIED_ROOT}/{first_uuid}")
        assert detail.status_code == 200, detail.text
        _assert_public_card(detail.json())
        assert detail.json()["media"] == {
            "format": "gif",
            "height": 180,
            "url": (
                f"/api/workspace/v1/messenger/stickers/{first_uuid}/actions/download"
            ),
            "width": 240,
        }

        downloaded = workspace_api.get(f"{UNIFIED_ROOT}/{first_uuid}/actions/download")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == first_data
        assert downloaded.headers["Content-Type"] == "image/gif"
        assert downloaded.headers["ETag"] == f'"{digests[0]}"'
        assert downloaded.headers["Cache-Control"] == (
            "private, max-age=31536000, immutable"
        )

        forbidden_update = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert forbidden_update.status_code == 403, forbidden_update.text
        assert str(tmp_path) not in forbidden_update.text

        updated = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            permissions=(MANAGE_PERMISSION,),
            json={"title": f"Updated {token}"},
        )
        assert updated.status_code == 200, updated.text
        _assert_public_card(updated.json())
        assert updated.json()["title"] == f"Updated {token}"
        assert updated.json()["is_favorite"] is False

        other_user = sys_uuid.uuid4()
        other_project = sys_uuid.uuid4()
        starred = workspace_api.post(f"{UNIFIED_ROOT}/{first_uuid}/actions/star/invoke")
        assert starred.status_code == 200, starred.text
        personalized_update = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            permissions=(MANAGE_PERMISSION,),
            json={"alt_text": f"Personalized {token}"},
        )
        assert personalized_update.status_code == 200, personalized_update.text
        _assert_public_card(personalized_update.json())
        assert personalized_update.json()["is_favorite"] is True
        same_user_other_project = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            project=other_project,
            params={"favorite": "true"},
        )
        assert same_user_other_project.status_code == 200
        assert [card["id"] for card in same_user_other_project.json()] == [first_uuid]
        assert same_user_other_project.json()[0]["is_favorite"] is True
        foreign_favorites = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            user=other_user,
            project=other_project,
            params={"favorite": "true"},
        )
        assert foreign_favorites.status_code == 200
        assert foreign_favorites.json() == []

        unstarred = workspace_api.post(
            f"{UNIFIED_ROOT}/{first_uuid}/actions/unstar/invoke",
            project=other_project,
        )
        assert unstarred.status_code == 200, unstarred.text
        repeated_unstar = workspace_api.post(
            f"{UNIFIED_ROOT}/{first_uuid}/actions/unstar/invoke",
            project=other_project,
        )
        assert repeated_unstar.status_code == 200, repeated_unstar.text

        assert (
            workspace_api.post(
                f"{UNIFIED_ROOT}/{first_uuid}/actions/star/invoke"
            ).status_code
            == 200
        )
        hidden = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            permissions=(MANAGE_PERMISSION,),
            json={"active": False},
        )
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["is_favorite"] is True
        assert workspace_api.get(f"{UNIFIED_ROOT}/{first_uuid}").status_code == 404
        hidden_search = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params={"q": token},
        )
        assert hidden_search.status_code == 200
        assert [card["id"] for card in hidden_search.json()] == [second_uuid]
        hidden_favorites = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params={"favorite": "true"},
        )
        assert hidden_favorites.status_code == 200
        assert hidden_favorites.json() == []
        hidden_batch = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=uuid_params,
        )
        assert hidden_batch.status_code == 200, hidden_batch.text
        assert {card["id"] for card in hidden_batch.json()} == {
            first_uuid,
            second_uuid,
        }
        assert (
            workspace_api.get(f"{UNIFIED_ROOT}/{first_uuid}/actions/download").content
            == first_data
        )
        assert (
            workspace_api.post(
                f"{UNIFIED_ROOT}/{first_uuid}/actions/star/invoke"
            ).status_code
            == 404
        )

        unhidden = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            permissions=(MANAGE_PERMISSION,),
            json={"active": True},
        )
        assert unhidden.status_code == 200, unhidden.text
        assert unhidden.json()["is_favorite"] is True
        restored_favorite = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params={"favorite": "true"},
        )
        assert [card["id"] for card in restored_favorite.json()] == [first_uuid]

        blocked = workspace_api.put(
            f"{UNIFIED_ROOT}/{first_uuid}",
            permissions=(MANAGE_PERMISSION,),
            json={"active": False, "blocked": True},
        )
        assert blocked.status_code == 200, blocked.text
        assert blocked.json()["is_favorite"] is True
        blocked_batch = workspace_api.get(
            f"{UNIFIED_ROOT}/",
            params=uuid_params,
        )
        assert [card["id"] for card in blocked_batch.json()] == [second_uuid]

        class StorageReadSentinel:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def read(self, media_object_id: str) -> bytes:
                self.calls.append(media_object_id)
                raise AssertionError("blocked media must not be read")

        storage_sentinel = StorageReadSentinel()
        monkeypatch.setattr(
            sticker_storage,
            "get_sticker_storage",
            lambda: storage_sentinel,
        )
        blocked_download = workspace_api.get(
            f"{UNIFIED_ROOT}/{first_uuid}/actions/download"
        )
        assert blocked_download.status_code == 404, blocked_download.text
        assert storage_sentinel.calls == []
        for secret in ("media_object_id", "sha256", "size_bytes", str(tmp_path)):
            assert secret not in blocked_download.text
        assert (
            workspace_api.post(
                f"{UNIFIED_ROOT}/{first_uuid}/actions/unstar/invoke"
            ).status_code
            == 200
        )

        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_stickers WHERE uuid = %s",
                (first_uuid,),
            )
        absent_download = workspace_api.get(
            f"{UNIFIED_ROOT}/{first_uuid}/actions/download"
        )
        assert absent_download.status_code == blocked_download.status_code == 404
        assert absent_download.json() == blocked_download.json()
        assert storage_sentinel.calls == []

        standalone = api.get(
            f"{STANDALONE_ROOT}/",
            params={"uuid": second_uuid},
        )
        assert standalone.status_code == 200, standalone.text
        assert [card["id"] for card in standalone.json()] == [second_uuid]
        _assert_public_card(standalone.json()[0])
    finally:
        _delete_imported_stickers(db, digests)


def test_real_http_import_checks_permission_before_multipart_parsing(
    workspace_api,
    db,
) -> None:
    before = db.execute("SELECT COUNT(*) FROM m_workspace_stickers").fetchone()[0]
    response = workspace_api.post(
        f"{UNIFIED_ROOT}/actions/import_archive/invoke",
        data=b"this is not a multipart body",
        headers={"Content-Type": "multipart/form-data; boundary=broken"},
    )
    after = db.execute("SELECT COUNT(*) FROM m_workspace_stickers").fetchone()[0]

    assert response.status_code == 403, response.text
    assert after == before


def test_sticker_markdown_urn_round_trips_unchanged_through_message_http_api(
    api,
    db,
) -> None:
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Sticker Markdown round-trip",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    sticker_uuid = sys_uuid.uuid4()
    content = f"![sticker](urn:sticker:{sticker_uuid})"

    created = api.post(
        "/v1/messages/",
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": content},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["payload"] == {"kind": "markdown", "content": content}

    loaded = api.get(f"/v1/messages/{created.json()['uuid']}")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["payload"] == {"kind": "markdown", "content": content}
