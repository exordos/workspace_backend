# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Persist conversions only for the exact provider, source and resolved context."""

import hashlib
import json
import typing
import uuid


CONVERTER_VERSION = 2

LOOKUP_SQL = """
    SELECT contents.uuid,contents.workspace_content,contents.provider_content
    FROM bridge_zulip_v5_content_snapshots cache
    JOIN messenger_message_contents contents ON contents.uuid=cache.content_uuid
    WHERE cache.uuid=%s AND cache.provider_uuid=%s AND cache.direction=%s
      AND cache.context_hash=%s AND contents.{source}=%s
    FOR KEY SHARE OF contents
"""
SAVE_SQL = """
    INSERT INTO bridge_zulip_v5_content_snapshots
        (uuid,provider_uuid,direction,context_hash,content_uuid)
    VALUES (%s,%s,%s,%s,%s) ON CONFLICT (uuid) DO NOTHING
"""
RESOLVE_SQL = """
    SELECT messenger_resolve_message_content(%s::jsonb,%s) AS uuid
"""


def _json(value: typing.Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=lambda item: sorted(item) if isinstance(item, set) else str(item),
    )


def key(
    provider: object, direction: str, source: typing.Any, context: dict
) -> tuple[uuid.UUID, object, str, str]:
    fingerprint = hashlib.sha256(
        _json([CONVERTER_VERSION, context]).encode("utf-8")
    ).hexdigest()
    identifier = uuid.uuid5(
        uuid.UUID(str(provider)), _json([direction, fingerprint, source])
    )
    return identifier, provider, direction, fingerprint


def incoming_context(message: dict, context: dict) -> dict:
    """Exclude only identity fields whose use the converter tracks explicitly."""
    return {
        **{
            name: value
            for name, value in context.items()
            if name not in ("original_url", "current_message_uuid", "message_uuids")
        },
        "message_uuids": {
            remote: value
            for remote, value in context["message_uuids"].items()
            if remote != str(message["id"])
        },
    }
