# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Restore sticker references only from authenticated Zulip uploads."""

import collections.abc
import hashlib
import typing
import urllib.parse

from workspace.bridges.zulip import message_conversion, outgoing_operations
from workspace.messenger_api.dm import base


SELECT_CATALOG_SQL = """
    SELECT uuid, size_bytes, sha256, NOT blocked AS available
    FROM m_workspace_stickers
    WHERE uuid=ANY(%s::uuid[])
    UNION ALL
    SELECT sticker_uuid, size_bytes, sha256, false
    FROM messenger_sticker_cleanup_tasks
    WHERE sticker_uuid=ANY(%s::uuid[])
"""
Download = tuple[bytes, str] | int


def marker(account: dict, target: str, label: str | None) -> str | None:
    if label is None:
        return None
    identifier = message_conversion.sticker_uuid_from_label(label)
    if identifier is None:
        return None
    server = message_conversion.parse_url(account["settings"]["server_url"])
    url = message_conversion.parse_url(target, account["settings"]["server_url"])
    if (
        server is None
        or url is None
        or url.scheme != server.scheme
        or url.netloc.lower() != server.netloc.lower()
        or url.hostname != account["hostname"]
        or not url.path.startswith("/user_uploads/")
    ):
        return None
    path = message_conversion.zulip_upload_path(url.path)
    if path is None or not base.FileProviderIdentity().validate(path):
        return None
    parts = urllib.parse.unquote(path).removeprefix("/user_uploads/").split("/")
    if (
        len(parts) < 3
        or not parts[0].isdigit()
        or any(part in ("", ".", "..") or "\\" in part for part in parts)
    ):
        return None
    return identifier


async def catalog(
    query: collections.abc.Callable[
        [str, tuple], collections.abc.Awaitable[list[dict]]
    ],
    account: dict,
    messages: list[dict],
) -> dict[str, dict]:
    identifiers = sorted(
        {
            identifier
            for message in messages
            if message_conversion.STICKER_LABEL_PREFIX in message["content"]
            for target, label in message_conversion.get_zulip_file_links(message, {})
            if (identifier := marker(account, target, label)) is not None
        }
    )
    if not identifiers:
        return {}
    rows = await query(SELECT_CATALOG_SQL, (identifiers, identifiers))
    return {str(row["uuid"]): row for row in rows}


async def download(http: typing.Any, account: dict, path: str) -> Download:
    try:
        return await http.download(account, path)
    except outgoing_operations.ApiError as error:
        if error.status not in (403, 404):
            raise
        return error.status


async def verify(
    http: typing.Any,
    account: dict,
    path: str,
    identifiers: collections.abc.Iterable[str | None],
    stickers: dict[str, dict],
) -> tuple[dict[str, bool], Download | None]:
    candidates = {
        identifier: stickers[identifier]
        for identifier in identifiers
        if identifier is not None and identifier in stickers
    }
    if not candidates:
        return {}, None
    result = await download(http, account, path)
    if not isinstance(result, tuple):
        return {}, result
    data, _ = result
    digest = hashlib.sha256(data).hexdigest()
    return {
        identifier: row["available"]
        for identifier, row in candidates.items()
        if row["size_bytes"] == len(data) and row["sha256"] == digest
    }, result
