# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
from unittest import mock

import pytest

from workspace.bridges.zulip import history_blocks, incoming_operations
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


def test_accounts_with_different_reference_access_keep_one_canonical_payload(incoming):
    api, db, account, http, _ = incoming
    linked, peer = history_tests.incoming_tests.peer_account(incoming)
    target = history_tests.message(101, "Restricted target", stream_id=43)
    url = account["settings"]["server_url"] + "/#narrow/near/101"
    messages = {201: history_tests.message(201, f"[Target]({url})")}
    http.request.return_value = {
        "stream": {"stream_id": 43, "name": "Other", "description": ""}
    }
    history_tests.incoming_tests.apply(incoming, {"type": "message", "message": target})
    for context in (incoming, peer):
        history_tests.incoming_tests.apply(
            context,
            {
                "type": "subscription",
                "op": "add",
                "subscriptions": [
                    {"stream_id": 42, "name": "Channel", "description": ""}
                ],
            },
        )
    target_uuid = db.execute(
        "SELECT placement.uuid FROM messenger_messages message "
        "JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()[0]

    async def scan(context):
        history_tests.request(db, context[2], cutoff_id=201)
        async with history_tests.worker(context, messages) as (service, _):
            with mock.patch.object(
                service.operations, "merge_batch", wraps=service.operations.merge_batch
            ) as merge:
                await history_tests.drain(service, context[2])
                return merge.call_count

    def canonical():
        return db.execute(
            "SELECT message.content_uuid,message.content_revision,message.content_updated_at,"
            "content.workspace_content FROM messenger_messages message "
            "JOIN messenger_message_contents content ON content.uuid=message.content_uuid "
            "WHERE message.project_id=%s AND message.provider_identity='201'",
            (account["project_id"],),
        ).fetchone()

    try:
        assert asyncio.run(scan(incoming)) == 1
        before = canonical()
        assert f"urn:url:{url}" in before[3]["content"]
        assert (
            asyncio.run(scan(peer)) == 1
        )  # The second account needs its own bindings.
        assert canonical() == before
        for context in (incoming, peer, incoming, peer):
            assert asyncio.run(scan(context)) == 0
            assert canonical() == before
        assert before[2] == datetime.datetime.fromtimestamp(
            messages[201]["timestamp"], datetime.timezone.utc
        ).replace(tzinfo=None)
        # The source link reveals no local identity and grants no target access.
        # Reading the shared referring message still works.
        assert (
            api.get(
                f"/v1/messenger/messages/{target_uuid}", user=linked.owner_user_uuid
            ).status_code
            == 404
        )
        visible = api.get("/v1/messenger/messages/", user=linked.owner_user_uuid).json()
        assert {row["provider_identity"] for row in visible} == {"201"}
        assert (
            db.execute(
                "SELECT 1 FROM messenger_user_message_bindings "
                "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (account["project_id"], linked.owner_user_uuid, target_uuid),
            ).fetchone()
            is None
        )
    finally:
        linked.delete()


def test_reference_refinement_keeps_provider_edit_time_and_next_scan_skips(incoming):
    _, db, account, *_ = incoming
    messages = {
        101: history_tests.message(101, "#**Channel>Fresh**"),
        102: history_tests.message(102, subject="Fresh"),
    }

    async def scan():
        history_tests.request(db, account, cutoff_id=102)
        async with history_tests.worker(incoming, messages) as (service, _):
            with mock.patch.object(
                service.operations, "merge_batch", wraps=service.operations.merge_batch
            ) as merge:
                await history_tests.drain(service, account)
                return merge.call_count

    assert asyncio.run(scan()) == 1
    assert asyncio.run(scan()) == 1  # Resolve the topic created in the first block.
    edited, payload = db.execute(
        "SELECT message.content_updated_at,content.workspace_content "
        "FROM messenger_messages message JOIN messenger_message_contents content "
        "ON content.uuid=message.content_uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    assert "urn:topic:" in payload["content"]
    assert edited == datetime.datetime.fromtimestamp(
        messages[101]["timestamp"], datetime.timezone.utc
    ).replace(tzinfo=None)
    assert asyncio.run(scan()) == 0


def test_unchanged_block_checks_context_once_in_cursor_transaction(incoming):
    _, db, account, *_ = incoming
    messages = {101: history_tests.message(101, "Stable body")}

    async def scan(observe=False):
        history_tests.request(db, account, cutoff_id=101)
        async with history_tests.worker(incoming, messages) as (service, _):
            with mock.patch.object(
                history_blocks, "context", wraps=history_blocks.context
            ) as context:
                await history_tests.drain(service, account)
                if observe:
                    assert context.call_count == 1

    asyncio.run(scan())
    asyncio.run(scan(observe=True))


@pytest.mark.parametrize(
    "provider,explicit_time,preserve",
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_conversion_clock_guard_keeps_local_edits_and_missing_time(
    incoming, provider, explicit_time, preserve
):
    _, db, account, *_ = incoming
    source = history_tests.message(101, "Original")
    history_tests.incoming_tests.apply(incoming, {"type": "message", "message": source})
    root, edited = db.execute(
        "SELECT uuid,content_updated_at FROM messenger_messages "
        "WHERE project_id=%s AND provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    content = db.execute(
        "SELECT messenger_resolve_message_content(%s::jsonb,%s)",
        (
            incoming_operations.json_value({"kind": "markdown", "content": "Refined"}),
            source["content"],
        ),
    ).fetchone()[0]
    with db.transaction():
        if provider:
            db.execute("SET LOCAL workspace.zulip_incoming='on'")
        actual = db.execute(
            "UPDATE messenger_messages SET content_uuid=%s,content_updated_at=%s "
            "WHERE project_id=%s AND uuid=%s RETURNING content_updated_at",
            (content, edited if explicit_time else None, account["project_id"], root),
        ).fetchone()[0]
    assert (actual == edited) if preserve else (actual > edited)
