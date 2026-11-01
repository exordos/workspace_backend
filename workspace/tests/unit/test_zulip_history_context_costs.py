# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import copy
import datetime
import random
import re
import uuid
from unittest import mock

import pytest

from workspace.bridges.zulip import content_cache, history_blocks, incoming_operations


def uid(number):
    return uuid.UUID(int=number)


def context_data(profile="stream", topics=256):
    account = {
        "uuid": uid(1),
        "project_id": uid(2),
        "provider_uuid": uid(3),
        "owner_user_uuid": uid(4),
        "external_account_id": "7",
        "hostname": "provider.example.invalid",
        "settings": {"server_url": "https://provider.example.invalid"},
        "subscribed_streams": None,
    }
    message = {
        "id": 101,
        "type": "stream",
        "stream_id": 42,
        "subject": "General",
        "sender_id": 7,
        "content": "General @**Synthetic|7** #**Channel>General@101**",
        "timestamp": 1700000000,
        "flags": [],
        "reactions": [],
    }
    if profile == "done":
        message.update(
            subject="✔ General",
            content=message["content"]
            + " channel/42/topic/General channel/421/topic/Other",
        )
    if profile == "dm":
        message.update(type="private", display_recipient=[{"id": 7}, {"id": 8}])
    identity = incoming_operations.stream_identity(account, message)
    streams = [
        {
            "uuid": uid(10 + n),
            "provider_identity": remote,
            "name": name,
            "deleted_at": None,
            "active": True,
            "membership_generation": 1,
        }
        for n, (remote, name) in enumerate(
            ((identity, "Channel"), ("421", "Other"), ("4", "Unused"))
        )
    ]
    topic_rows = [
        {
            "uuid": uid(100 + n),
            "stream_uuid": uid(10),
            "provider_identity": identity + ":" + name,
            "name": name,
            "is_done": n == 1,
            "deleted_at": None,
        }
        for n, name in enumerate(
            ["General", "eneral", "", "✔ General"]
            + [f"Unused topic {n}" for n in range(topics)]
        )
    ]
    prepared = {
        "catalog": [
            {
                "external_account_id": "7",
                "owner_user_uuid": uid(4),
                "first_name": "Synthetic",
            }
        ],
        "streams": streams,
        "topics": topic_rows,
        "references": [{"remote_id": "101", "uuid": uid(10000)}],
        "files": [],
        "edited": {},
    }
    bindings = [
        {
            "uuid": uid(10000),
            "provider_identity": "101",
            "deleted_at": None,
            "content_updated_at": datetime.datetime(2026, 1, 1),
            "placement_uuid": uid(10001),
            "stream_uuid": uid(10),
            "topic_uuid": uid(100),
            "binding_uuid": uid(10002),
        }
    ]
    return account, [message], prepared, bindings


def context_rows(prepared, bindings, observed):
    async def rows(connection, query, parameters=()):
        if query == incoming_operations.SELECT_MESSAGE_REFERENCES_SQL:
            return prepared["references"]
        if "FROM m_external_accounts_v2" in query:
            return prepared["catalog"]
        if "FROM messenger_streams stream" in query:
            return prepared["streams"]
        if "FROM messenger_messages message" in query:
            return copy.deepcopy(bindings)
        if "FROM messenger_topics" in query:
            values = prepared["topics"]
            if "AND uuid=ANY(" in query:
                values = [row for row in values if row["uuid"] in parameters[-1]]
            observed.append((query, len(values)))
            return values
        raise AssertionError(query)

    return rows


# Recorded from the released context implementation, including ordering and
# conservative substring dependencies. Pin converter version 1 below to isolate
# context selection from intentional converter-version invalidation.
EXPECTED = {
    "stream": "952bba6f08757dd457ddc1f60516a4ac4627e1841dddccaaa0067ea57f7994f7",
    "done": "6aecd626393c844382136b239fbdd2d1b8863190f4825c93b34505760394d83b",
    "dm": "9aaeb8fa5c87e0c923eb230669c68f0b0217faf769d031d838648da56fb4d8f6",
}


@pytest.mark.parametrize("profile", ["stream", "done", "dm"])
@pytest.mark.parametrize("prepared_context", [False, True])
def test_context_keeps_released_fingerprint(profile, prepared_context):
    account, messages, prepared, bindings = context_data(profile)
    with (
        mock.patch.object(content_cache, "CONVERTER_VERSION", 1),
        mock.patch.object(
            incoming_operations,
            "rows",
            side_effect=context_rows(prepared, bindings, []),
        ),
    ):
        actual = asyncio.run(
            history_blocks.context(
                None, account, messages, 5, prepared if prepared_context else None
            )
        )
    assert actual.hex() == EXPECTED[profile]


def test_context_does_not_scan_page_for_each_catalog_name():
    account, messages, prepared, bindings = context_data(topics=1024)
    scans = []

    class CountedText(str):
        def __contains__(self, value):
            scans.append(value)
            return super().__contains__(value)

    with (
        mock.patch.object(
            incoming_operations,
            "rows",
            side_effect=context_rows(prepared, bindings, []),
        ),
        mock.patch.object(
            history_blocks.urllib.parse,
            "unquote",
            side_effect=lambda text: CountedText(text),
        ),
    ):
        asyncio.run(history_blocks.context(None, account, messages, 5, prepared))
    assert len(scans) <= 10, f"Repeated whole-page substring scans: {len(scans)}"


def test_prepared_context_reads_only_bound_topics():
    account, messages, prepared, bindings = context_data()
    reads = []
    with mock.patch.object(
        incoming_operations, "rows", side_effect=context_rows(prepared, bindings, reads)
    ):
        asyncio.run(history_blocks.context(None, account, messages, 5, prepared))
    assert [count for query, count in reads] == [1]


@pytest.mark.parametrize("size", [0, 3, 32, 100])
def test_matching_names_preserves_overlapping_unicode_and_empty_substrings(size):
    generator = random.Random(42)
    for _ in range(30):
        text = "".join(generator.choices("ab✔Жé", k=40))
        names = {"", "a", "aa", "aaa", "Жé", "✔"}
        names.update(
            "".join(generator.choices("ab✔Жé", k=generator.randint(1, 8)))
            for _ in range(size)
        )
        assert history_blocks.matching_names(text, names) == {
            name for name in names if name in text
        }


def test_channel_identity_extraction_preserves_boundaries_and_legacy_identities():
    direct = "dm:" + str(uid(9))
    identities = [
        "",
        "4",
        "42",
        "421",
        "٤٢",
        direct,
        "legacy:stream",
        "42x",
        "dm:legacy",
        "a+b",
    ]
    texts = [
        "channel/42channel/421",
        "channel/42٣ channel/٤٢/topic/x",
        "channel/" + direct,
        "channel/" + direct + "1",
        "channel/" + direct + "x",
        "channel/legacy:stream/topic/x channel/dm:legacy channel/a+b",
        "channel/42x",
        "unrelated",
    ]
    for text in texts:
        expected = {
            identity
            for identity in identities
            if re.search("channel/" + re.escape(identity) + r"(?:\D|$)", text)
        }
        assert history_blocks.linked_stream_identities(text, identities) == expected


def test_context_does_not_search_page_separately_for_every_direct_stream():
    account, messages, prepared, bindings = context_data()
    prepared["streams"].extend(
        {
            "uuid": uid(100000 + number),
            "provider_identity": "dm:" + str(uid(200000 + number)),
            "name": "Synthetic direct conversation",
            "deleted_at": None,
            "active": False,
            "membership_generation": 1,
        }
        for number in range(1024)
    )
    searches = []
    search = re.search

    def observed(pattern, text, *args, **kwargs):
        if pattern.startswith("channel/"):
            searches.append(pattern)
        return search(pattern, text, *args, **kwargs)

    with (
        mock.patch.object(
            incoming_operations,
            "rows",
            side_effect=context_rows(prepared, bindings, []),
        ),
        mock.patch.object(history_blocks.re, "search", side_effect=observed),
    ):
        asyncio.run(history_blocks.context(None, account, messages, 5, prepared))
    assert len(searches) <= 10
