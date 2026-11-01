# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio

import pytest

from workspace.tests.integration import test_zulip_history as history_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests


incoming = incoming_tests.incoming


@pytest.mark.parametrize("direction", ["history", "realtime"])
@pytest.mark.parametrize(
    "leaf,preserve_source",
    [("x" * 6000, True), ("😀" * 1200, True), ("x" * 40_001, False)],
    ids=["ascii", "unicode", "oversized-source"],
)
def test_large_nested_quotes_import_and_replay_without_blocking(
    incoming, direction, leaf, preserve_source
):
    api, db, account, http, *_ = incoming
    source = leaf
    for depth in range(8):
        fence = "`" * (depth + 3)
        source = (
            "@_**Sender|7** "
            f"[said](https://zulip.example.com/#narrow/near/{101 + depth}):\n"
            f"{fence}quote\n{source}\n{fence}"
        )
    messages = {
        identifier: history_tests.message(identifier, "Original quote")
        for identifier in range(101, 109)
    }
    messages[109] = history_tests.message(109, source)
    messages[110] = history_tests.message(110, source)
    messages[111] = history_tests.message(111, "After the large quotes")

    async def provider_request(_account, method, path, *args, **kwargs):
        assert method == "GET"
        if path.startswith("/messages/"):
            return {"message": messages[int(path.rsplit("/", 1)[1])]}
        assert path == "/streams/42"
        return {"stream": {"stream_id": 42, "name": "Channel", "description": ""}}

    http.request.side_effect = provider_request

    def run():
        if direction == "realtime":
            incoming_tests.apply(
                incoming,
                *({"type": "message", "message": value} for value in messages.values()),
            )
        else:
            history_tests.request(db, account)

            async def import_history():
                async with history_tests.worker(incoming, messages, batch_size=20) as (
                    service,
                    _,
                ):
                    await history_tests.drain(service, account)
                    assert not await service.db.sql(
                        "SELECT 1 FROM zulip_history_batches"
                    )

            asyncio.run(import_history())
        incoming_tests.drain()

    run()
    rows = api.get("/v1/messenger/messages/").json()
    by_remote = {row["provider_identity"]: row for row in rows}
    assert len(by_remote) == len(messages)
    assert by_remote["111"]["payload"]["content"] == "After the large quotes"
    for remote in ("109", "110"):
        content = by_remote[remote]["payload"]["content"]
        assert content.startswith(
            "Not supported in Workspace: formatted message is too large"
        )
        assert len(content) <= 40_000
        assert (source in content) == preserve_source
        assert f"/near/{remote})" in content
    assert db.execute(
        "SELECT count(*) FROM messenger_message_contents WHERE provider_content=%s",
        (source,),
    ).fetchone() == (2,)
    assert db.execute(
        "SELECT count(*) FROM bridge_zulip_v5_content_snapshots cache "
        "JOIN messenger_message_contents contents ON contents.uuid=cache.content_uuid "
        "WHERE cache.direction='incoming' AND cache.delivery_uuid IS NULL "
        "AND contents.provider_content=%s",
        (source,),
    ).fetchone() == (0,)
    roots = db.execute(
        "SELECT uuid,content_uuid,content_revision FROM messenger_messages ORDER BY uuid"
    ).fetchall()
    run()
    assert (
        roots
        == db.execute(
            "SELECT uuid,content_uuid,content_revision FROM messenger_messages ORDER BY uuid"
        ).fetchall()
    )
