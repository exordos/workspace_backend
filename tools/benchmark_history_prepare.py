# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Measure real history preparation with synthetic, in-memory SQL responses.

Run from the repository root with the development environment::

    .tox/develop/bin/python -m tools.benchmark_history_prepare --repeats 5

The measured interval includes prepare_batch, conversion and UUID generation.
Dataset construction and result hashing are outside the interval. There is no
provider, network, storage or database I/O. Use the same arguments on both trees.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import statistics
import time
import uuid
from unittest import mock

from workspace.bridges.zulip import history, incoming_operations


def identifier(kind, number):
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cassi-prepare-benchmark:{kind}:{number}")


def dataset(messages=1000, people=512, streams=512, topics=8192, *, new_topics=False):
    account = {
        "uuid": identifier("account", 0),
        "project_id": identifier("project", 0),
        "provider_uuid": identifier("provider", 0),
        "owner_user_uuid": identifier("person", 7),
        "external_account_id": "7",
        "hostname": "example.invalid",
        "settings": {"server_url": "https://example.invalid"},
        "subscribed_streams": None,
    }
    catalog = [
        {
            "external_account_id": str(i + 1),
            "owner_user_uuid": identifier("person", i + 1),
            "first_name": f"alias-{i}",
        }
        for i in range(people)
    ]
    channels = [
        {
            "uuid": identifier("stream", i),
            "name": f"stream-{i}",
            "provider_identity": str(i + 1),
            "active": True,
        }
        for i in range(streams)
    ]
    destinations = [
        {
            "uuid": identifier("topic", i),
            "stream_uuid": channels[i % streams]["uuid"],
            "provider_identity": f"{i % streams + 1}:topic-{i // streams}",
            "stream_name": channels[i % streams]["name"],
            "name": f"topic-{i // streams}",
            "is_done": False,
        }
        for i in range(topics)
    ]
    page = [
        {
            "id": 10000 + i,
            "type": "stream",
            "sender_id": 7,
            "stream_id": i % streams + 1,
            "subject": f"new-{i % 16}" if new_topics else "topic-0",
            "content": f"Body {i}: @**alias-1|2** #**stream-0>topic-0**",
            "timestamp": 1700000000,
            "flags": [],
            "reactions": [],
        }
        for i in range(messages)
    ]
    return account, page, Database(catalog, channels, destinations)


class Database:
    def __init__(self, people, streams, topics):
        self.people, self.streams, self.topics = people, streams, topics

    async def sql(self, query, parameters=()):
        if query == history.PEOPLE_SQL:
            return self.people
        if query == history.STREAMS_SQL:
            return self.streams
        if "SELECT topic.*,stream.name AS stream_name" in query:
            return self.topics
        if query in (
            history.MESSAGES_SQL,
            history.FILES_SQL,
            incoming_operations.SELECT_MESSAGE_REFERENCES_SQL,
        ):
            return []
        if (
            "SELECT DISTINCT ON (delivery.message_uuid,contents.provider_content)"
            in query
        ):
            return []
        raise AssertionError(query)


async def prepare(account, page, database):
    # Keep every random content UUID reproducible for complete row comparison.
    ids = iter(range(1, len(page) + 1))
    with mock.patch.object(
        history.uuid, "uuid4", side_effect=lambda: uuid.UUID(int=next(ids))
    ):
        return await history.Operations(database, mock.Mock()).prepare_batch(
            account, page
        )


def checksum(batch):
    rows = {name: value for name, value in batch.items() if name != "_context"}
    return hashlib.sha256(
        json.dumps(
            rows, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in (
        ("messages", 1000),
        ("people", 512),
        ("streams", 512),
        ("topics", 8192),
        ("repeats", 5),
    ):
        parser.add_argument("--" + name, type=int, default=default)
    parser.add_argument("--new-topics", action="store_true")
    args = parser.parse_args()
    account, page, database = dataset(
        args.messages,
        args.people,
        args.streams,
        args.topics,
        new_topics=args.new_topics,
    )
    durations, checksums = [], []
    for _ in range(args.repeats):
        start = time.perf_counter()
        batch = asyncio.run(prepare(account, page, database))
        durations.append(time.perf_counter() - start)
        checksums.append(checksum(batch))
    assert len(set(checksums)) == 1
    print(
        json.dumps(
            {
                "parameters": vars(args),
                "python": platform.python_version(),
                "seconds": durations,
                "median_seconds": statistics.median(durations),
                "row_checksum": checksums[0],
                "prepared_messages": len(batch["messenger_messages"]),
                "io": "synthetic SQL responses; no external I/O",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
