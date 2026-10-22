# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Versioned source fingerprints; never hash a derived Workspace rendering."""

import hashlib
import json
import typing

from workspace.common import emoji


VERSION = 2
BATCH_SIZE = 1000
COMPONENTS = ("text", "flags", "metadata", "reactions")
FLAGS = (
    "read",
    "starred",
    "collapsed",
    "hide_link_previews",
    "mentioned",
    "stream_wildcard_mentioned",
    "topic_wildcard_mentioned",
    "has_alert_word",
    "historical",
)


def digest(value: typing.Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def range_start(message_id: int) -> int:
    return (message_id - 1) // BATCH_SIZE * BATCH_SIZE + 1


def normalized_flags(flags: typing.Iterable[str]) -> list[str]:
    return sorted(set(flags).intersection(FLAGS))


def flags_hash(flags: typing.Iterable[str]) -> str:
    return digest(normalized_flags(flags))


def metadata(message: dict[str, typing.Any]) -> dict[str, typing.Any]:
    result = {
        "type": message["type"],
        "sender_id": message["sender_id"],
        "timestamp": message["timestamp"],
        "last_edit_timestamp": message.get("last_edit_timestamp", message["timestamp"]),
        "is_me_message": message["is_me_message"],
        "subject": message["subject"],
    }
    if message["type"] == "stream":
        result["stream_id"] = message["stream_id"]
    else:
        result["recipients"] = sorted(
            (recipient["id"], recipient["full_name"])
            for recipient in message["display_recipient"]
        )
    return result


def reactions_hash(reactions: list[dict[str, typing.Any]]) -> str:
    values = set()
    for reaction in reactions:
        kind = reaction["reaction_type"]
        code = reaction["emoji_code"]
        name = reaction["emoji_name"]
        if kind == "unicode_emoji":
            code = emoji.normalize_code(code)
            name = emoji.name_for_code(code, name)
        values.add((reaction["user_id"], kind, code, name))
    return digest(sorted(values))


def message_hashes(message: dict[str, typing.Any]) -> dict[str, str | None]:
    """A missing component is unknown, distinct from an observed empty value."""
    return {
        "text": hashlib.sha256(message["content"].encode("utf-8")).hexdigest(),
        "metadata": digest(metadata(message)),
        "flags": flags_hash(message["flags"]),
        "reactions": (
            reactions_hash(message["reactions"]) if "reactions" in message else None
        ),
    }


def batch_hashes(
    messages: list[dict[str, typing.Any]], first_id: int, covered_to: int
) -> dict[str, str | None]:
    entries = sorted((message["id"], message_hashes(message)) for message in messages)
    return batch_hashes_from_entries(entries, first_id, covered_to)


def batch_hashes_from_entries(
    entries: list[tuple[int, dict[str, str | None]]], first_id: int, covered_to: int
) -> dict[str, str | None]:
    result = {}
    for component in COMPONENTS:
        values = [(message_id, hashes[component]) for message_id, hashes in entries]
        result[component] = (
            digest([VERSION, first_id, covered_to, values])
            if all(value is not None for _message_id, value in values)
            else None
        )
    return result


def changed_components(
    current: dict[str, str | None], previous: dict[str, str | None] | None
) -> list[str]:
    if previous is None:
        return list(COMPONENTS)
    return [
        component
        for component in COMPONENTS
        if current[component] is not None and current[component] != previous[component]
    ]
