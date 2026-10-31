# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Compatibility with the frozen 3.0.10 context and bounded batch catalog work."""

import asyncio
import collections.abc
import copy
import json
import uuid

import pytest

from tools import benchmark_history_prepare as benchmark
from workspace.bridges.zulip import incoming_operations, message_conversion


def legacy_context(
    account: dict,
    data: dict,
    stream: dict,
    topic: dict,
    public_uuid: object,
    prepared: dict,
) -> dict:
    context = {
        "server_url": account["settings"]["server_url"],
        "original_url": account["settings"]["server_url"].rstrip("/")
        + f"/#narrow/near/{data['id']}",
        "zulip_namespace": str(account["provider_uuid"]),
        "own_user_id": account["external_account_id"],
        "normalize_emoji": True,
        "strict_identities": True,
        "current_message_uuid": str(public_uuid),
        "message_uuids": {str(data["id"]): str(public_uuid)},
        "topic_uuids": {},
        "messages": prepared.get("quoted_messages", {}),
        "mention_uuids": {},
        "stream_uuids": {},
        "stream_ids": {},
        "dm_stream_uuids": {},
        "files": {},
    }
    for person in prepared.get("catalog", []):
        user = str(person["owner_user_uuid"])
        context["mention_uuids"][f"id:{person['external_account_id']}"] = user
        if person["first_name"]:
            context["mention_uuids"][f"name:{person['first_name']}"] = user
    for item in (*prepared.get("streams", []), stream):
        context["stream_uuids"][item["name"]] = str(item["uuid"])
        context["stream_ids"][item["name"]] = item["provider_identity"]
        if item["provider_identity"].startswith("dm:"):
            context["dm_stream_uuids"][item["provider_identity"]] = str(item["uuid"])
    if data["type"] == "stream":
        context["stream_ids"][stream["name"]] = str(data["stream_id"])
        context["topic_uuids"][f"{stream['name']}>{data['subject']}"] = str(
            topic["uuid"]
        )
    for item in prepared.get("references", []):
        context["message_uuids"][item["remote_id"]] = str(item["uuid"])
    for item in prepared.get("topics", []):
        context["topic_uuids"][f"{item['stream_name']}>{item['name']}"] = str(
            item["uuid"]
        )
    return context


def plain(value):
    if isinstance(value, collections.abc.Mapping):
        return {key: plain(item) for key, item in value.items()}
    return value


def encoded(context):
    return json.dumps(
        plain(context), ensure_ascii=False, separators=(",", ":")
    ).encode()


@pytest.fixture
def example():
    account, page, database = benchmark.dataset(2, 8, 3, 6)
    stream = database.streams[0]
    topic = database.topics[0]
    quoted = {
        **page[0],
        "id": 12,
        "sender_id": 2,
        "sender_full_name": "alias-1",
        "display_recipient": "stream-0",
        "content": "Quoted body",
    }
    prepared = {
        "catalog": database.people,
        "streams": database.streams,
        "topics": database.topics,
        "references": [
            {"remote_id": "12", "uuid": benchmark.identifier("reference", 12)}
        ],
        "quoted_messages": {"12": quoted},
    }
    return (
        account,
        page[0],
        stream,
        topic,
        benchmark.identifier("placement", 0),
        prepared,
    )


@pytest.mark.parametrize(
    "profile", ["stream", "done", "dm", "mentions", "quote", "unsupported"]
)
def test_batch_context_matches_release_bytes_and_converted_payload(example, profile):
    account, data, stream, topic, public, prepared = copy.deepcopy(example)
    if profile == "done":
        data["subject"] = "✔ topic-0"
    elif profile == "dm":
        data["type"] = "private"
        stream = {
            "uuid": benchmark.identifier("direct", 0),
            "name": "direct",
            "provider_identity": "dm:"
            + str(uuid.uuid5(account["provider_uuid"], "dm:7,8")),
        }
        data["content"] = (
            "[Direct conversation](https://example.invalid/#narrow/dm/7,8)"
        )
    elif profile == "mentions":
        data["content"] = "@**alias-1|2** @_**alias-2** #**stream-0>topic-0**"
    elif profile == "quote":
        data["content"] = (
            "@_**alias-1|2** [said](https://example.invalid/#narrow/near/12):\n```quote\nQuoted body\n```"
        )
    elif profile == "unsupported":
        data["content"] = "```math\nx+y\n```"
    expected = legacy_context(account, data, stream, topic, public, prepared)
    shared = incoming_operations.conversion_context_base(account, prepared)
    actual = incoming_operations.conversion_context(
        account, data, stream, topic, public, prepared, base=shared
    )
    single = incoming_operations.conversion_context(
        account, data, stream, topic, public, prepared
    )
    assert encoded(actual) == encoded(expected)
    assert json.dumps(
        single, ensure_ascii=False, separators=(",", ":")
    ).encode() == encoded(expected)
    assert message_conversion.zulip_to_workspace_cached(
        data, actual
    ) == message_conversion.zulip_to_workspace_cached(data, expected)


@pytest.mark.parametrize("direct", [False, True])
def test_catalog_override_order_is_preserved(example, direct):
    account, data, stream, topic, public, prepared = copy.deepcopy(example)
    if direct:
        data["type"] = "private"
        stream["provider_identity"] = "dm:synthetic"
    prepared["streams"].append(
        {
            **stream,
            "uuid": benchmark.identifier("duplicate-stream", 1),
            "provider_identity": "dm:other",
        }
    )
    prepared["references"].append(
        {
            "remote_id": str(data["id"]),
            "uuid": benchmark.identifier("override-message", 1),
        }
    )
    prepared["topics"].append(
        {**topic, "uuid": benchmark.identifier("override-topic", 1)}
    )
    expected = legacy_context(account, data, stream, topic, public, prepared)
    shared = incoming_operations.conversion_context_base(account, prepared)
    actual = incoming_operations.conversion_context(
        account, data, stream, topic, public, prepared, base=shared
    )
    assert encoded(actual) == encoded(expected)


def test_batch_messages_do_not_leak_dynamic_context(example):
    account, data, stream, topic, public, prepared = example
    shared = incoming_operations.conversion_context_base(account, prepared)
    first = incoming_operations.conversion_context(
        account, data, stream, topic, public, prepared, base=shared
    )
    first["files"]["synthetic"] = "unavailable"
    first["deleted_files"] = {"synthetic"}
    second = incoming_operations.conversion_context(
        account,
        {**data, "id": 999},
        stream,
        topic,
        benchmark.identifier("placement", 1),
        prepared,
        base=shared,
    )
    assert second["files"] == {} and "deleted_files" not in second
    assert str(data["id"]) not in second["message_uuids"]
    assert first["current_message_uuid"] != second["current_message_uuid"]
    assert encoded(second) == encoded(
        legacy_context(
            account,
            {**data, "id": 999},
            stream,
            topic,
            benchmark.identifier("placement", 1),
            prepared,
        )
    )


class CountedUuid(uuid.UUID):
    counts = {}

    def __str__(self):
        self.counts[self.int] = self.counts.get(self.int, 0) + 1
        return super().__str__()


def test_prepare_formats_catalog_uuids_once_per_batch():
    account, page, database = benchmark.dataset(40, 16, 8, 64)
    for rows, key in (
        (database.people, "owner_user_uuid"),
        (database.streams, "uuid"),
        (database.topics, "uuid"),
    ):
        for row in rows:
            row[key] = CountedUuid(str(row[key]))
    for item in page:
        item["reactions"] = [
            {
                "user_id": 8,
                "emoji_name": "smile",
                "emoji_code": "1f604",
                "reaction_type": "unicode_emoji",
            }
        ]
    CountedUuid.counts = {}
    asyncio.run(benchmark.prepare(account, page, database))
    assert CountedUuid.counts
    assert max(CountedUuid.counts.values()) <= 1, CountedUuid.counts


class CountedTopics(list):
    iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_prepare_indexes_fallback_topics_once_per_batch():
    account, page, database = benchmark.dataset(40, 8, 2, 64, new_topics=True)
    database.topics = CountedTopics(database.topics)
    asyncio.run(benchmark.prepare(account, page, database))
    assert database.topics.iterations <= 3


@pytest.mark.parametrize("new_topics", [False, True])
def test_complete_prepared_rows_match_release_context(new_topics):
    from unittest import mock

    account, page, database = benchmark.dataset(40, 16, 8, 64, new_topics=new_topics)
    with mock.patch.object(
        incoming_operations,
        "conversion_context",
        side_effect=lambda *args, **kwargs: legacy_context(*args),
    ):
        expected = asyncio.run(benchmark.prepare(account, page, database))
    actual = asyncio.run(benchmark.prepare(account, page, database))
    assert benchmark.checksum(actual) == benchmark.checksum(expected)


def test_prepare_formats_repeated_attachment_uuid_once():
    from unittest import mock
    from workspace.bridges.zulip import history

    account, page, database = benchmark.dataset(8, 8, 1, 1)
    path = "/user_uploads/synthetic.txt"
    for item in page:
        item["_files"] = [(path, path)]
    identifier = CountedUuid(str(benchmark.identifier("file", 0)))
    files = {
        (database.streams[0]["uuid"], path): {
            "uuid": identifier,
            "content_type": "text/plain",
        }
    }
    CountedUuid.counts = {}
    with mock.patch.object(history.Operations, "prepare_files", return_value=files):
        asyncio.run(benchmark.prepare(account, page, database))
    assert CountedUuid.counts[identifier.int] == 1
