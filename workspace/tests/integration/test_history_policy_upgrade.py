# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
from unittest import mock

import pytest

from workspace.bridges.zulip import incoming_operations, message_conversion
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


def test_reference_free_block_keeps_its_v2_receipt(incoming):
    _, db, account, *_ = incoming
    messages = {201: history_tests.message(201, "No source references")}

    async def scan():
        history_tests.request(db, account, cutoff_id=201)
        async with history_tests.worker(incoming, messages) as (service, _):
            with mock.patch.object(
                service.operations, "merge_batch", wraps=service.operations.merge_batch
            ) as merge:
                await history_tests.drain(service, account)
                return merge.call_count

    with mock.patch.object(
        message_conversion, "PROVIDER_MESSAGE_REFERENCE_POLICY", "source-snapshots-v2"
    ):
        assert asyncio.run(scan()) == 1
    assert asyncio.run(scan()) == 0


def test_source_quote_reconverts_an_already_certified_v2_snapshot(incoming):
    api, db, account, *_ = incoming
    url = account["settings"]["server_url"] + "/#narrow/with/101"
    messages = {
        201: history_tests.message(
            201, f"@_**Alice|7** [said]({url}):\n```quote\nSupplied text\n```"
        )
    }
    convert = message_conversion.zulip_to_workspace_cached

    def previous_layout(*args, **kwargs):
        result, reusable = convert(*args, **kwargs)
        return {
            **result,
            "content": result["content"].replace(
                f"\n\n[Alice](urn:url:{url})", f"\n[Alice](urn:url:{url})", 1
            ),
        }, reusable

    async def scan():
        history_tests.request(db, account, cutoff_id=201)
        async with history_tests.worker(incoming, messages) as (service, _):
            with mock.patch.object(
                service.operations, "merge_batch", wraps=service.operations.merge_batch
            ) as merge:
                await history_tests.drain(service, account)
                return merge.call_count

    def content():
        return next(
            row["payload"]["content"]
            for row in api.get("/v1/messenger/messages/").json()
            if row["provider_identity"] == "201"
        )

    with (
        mock.patch.object(
            message_conversion,
            "PROVIDER_MESSAGE_REFERENCE_POLICY",
            "source-snapshots-v2",
        ),
        mock.patch.object(
            message_conversion, "zulip_to_workspace_cached", side_effect=previous_layout
        ),
    ):
        assert asyncio.run(scan()) == 1
    assert "urn:url:" in content().partition("\n\n")[0]
    assert asyncio.run(scan()) == 1
    assert "urn:url:" not in content().partition("\n\n")[0]
    assert f"\n\n[Alice](urn:url:{url})" in content()
    assert asyncio.run(scan()) == 0


@pytest.mark.parametrize("lane", ["history", "realtime"])
def test_with_link_reconverts_an_already_certified_legacy_payload(incoming, lane):
    api, db, account, http, _ = incoming
    target = history_tests.message(101, "Private target", stream_id=43)
    http.request.return_value = {
        "stream": {"stream_id": 43, "name": "Private", "description": ""}
    }
    history_tests.incoming_tests.apply(incoming, {"type": "message", "message": target})
    history_tests.incoming_tests.apply(
        incoming,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [{"stream_id": 42, "name": "Channel", "description": ""}],
        },
    )
    identifier = db.execute(
        "SELECT placement.uuid FROM messenger_messages message "
        "JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()[0]
    url = account["settings"]["server_url"] + "/#narrow/with/101"
    messages = {101: target, 201: history_tests.message(201, f"[Target]({url})")}
    references = incoming_operations.references
    conversion_context = incoming_operations.conversion_context

    def legacy_references(messages):
        # The previous extractor did not recognize with/ID links.
        return {**references(messages), "messages": []}

    def legacy_conversion(*args, **kwargs):
        context = conversion_context(*args, **kwargs)
        return {
            **context,
            "preserve_provider_message_links": False,
            "provider_message_reference_policy": None,
            # The previous bulk converter included every member of the page.
            "message_uuids": {**context["message_uuids"], "101": str(identifier)},
        }

    async def scan():
        history_tests.request(db, account, cutoff_id=201)
        async with history_tests.worker(incoming, messages) as (service, _):
            with mock.patch.object(
                service.operations, "merge_batch", wraps=service.operations.merge_batch
            ) as merge:
                await history_tests.drain(service, account)
                return merge.call_count

    def content():
        return next(
            row["payload"]["content"]
            for row in api.get("/v1/messenger/messages/").json()
            if row["provider_identity"] == "201"
        )

    with (
        mock.patch.object(
            incoming_operations, "references", side_effect=legacy_references
        ),
        mock.patch.object(
            incoming_operations, "conversion_context", side_effect=legacy_conversion
        ),
    ):
        assert asyncio.run(scan()) == 1
    assert f"urn:message:{identifier}" in content()
    if lane == "history":
        assert asyncio.run(scan()) == 1
    else:
        history_tests.incoming_tests.apply(
            incoming, {"type": "message", "message": messages[201]}
        )
    assert f"urn:url:{url}" in content()
    if lane == "history":
        assert asyncio.run(scan()) == 0
    else:
        before = content()
        history_tests.incoming_tests.apply(
            incoming, {"type": "message", "message": messages[201]}
        )
        assert content() == before
