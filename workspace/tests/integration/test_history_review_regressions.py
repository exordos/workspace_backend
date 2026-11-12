# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio

import pytest

from workspace.bridges.zulip import incoming_operations, outgoing_operations
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


def test_incoming_content_update_without_edit_time_advances_clock(incoming):
    _, db, account, *_ = incoming
    source = history_tests.message(101, "Original")
    history_tests.incoming_tests.apply(incoming, {"type": "message", "message": source})
    identifier, before = db.execute(
        "SELECT uuid,content_updated_at FROM messenger_messages "
        "WHERE project_id=%s AND provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    content = db.execute(
        "SELECT messenger_resolve_message_content(%s::jsonb,%s)",
        (
            incoming_operations.json_value({"kind": "markdown", "content": "Changed"}),
            source["content"],
        ),
    ).fetchone()[0]
    with db.transaction():
        db.execute("SET LOCAL workspace.zulip_incoming='on'")
        # A preceding explicit-time statement must not affect this update.
        db.execute(
            "UPDATE messenger_messages SET content_updated_at=%s "
            "WHERE project_id=%s AND uuid=%s",
            (before, account["project_id"], identifier),
        )
        actual = db.execute(
            "UPDATE messenger_messages SET content_uuid=%s "
            "WHERE project_id=%s AND uuid=%s RETURNING content_updated_at",
            (content, account["project_id"], identifier),
        ).fetchone()[0]
    assert actual > before


@pytest.mark.parametrize("lane", ["history", "realtime"])
@pytest.mark.parametrize("syntax", ["link", "channel", "quote", "nested_quote"])
def test_private_target_import_does_not_change_a_readers_link(incoming, lane, syntax):
    api, db, account, http, _ = incoming
    linked, peer = history_tests.incoming_tests.peer_account(incoming)
    url = account["settings"]["server_url"] + "/#narrow/near/101"
    content = {
        "link": f"[Probe]({url})",
        "channel": "#**Channel>General@101**",
        "quote": f"@_**Sender|7** [said]({url}):\n```quote\nGuessed text\n```",
        "nested_quote": (
            f"@_**Sender|7** [said]({url}):\n````quote\n"
            "```quote\nGuessed text\n```\n````"
        ),
    }[syntax]
    probe = history_tests.message(201, content)

    async def provider_request(_account, method, path, *args, **kwargs):
        if path == "/messages/101":
            raise outgoing_operations.ApiError(403, {"msg": "Not available"})
        return http.request.return_value

    http.request.side_effect = provider_request
    history_tests.incoming_tests.apply(
        peer,
        {
            "type": "subscription",
            "op": "add",
            "subscriptions": [
                {"stream_id": 42, "name": "Channel", "description": ""}
            ],
        },
    )

    async def import_history():
        history_tests.request(db, peer[2], cutoff_id=201)
        async with history_tests.worker(peer, {201: probe}) as (service, _):
            await history_tests.drain(service, peer[2])

    def scan():
        if lane == "history":
            asyncio.run(import_history())
        else:
            history_tests.incoming_tests.apply(peer, {"type": "message", "message": probe})

    def visible_message():
        response = api.get("/v1/messenger/messages/", user=linked.owner_user_uuid)
        assert response.status_code == 200
        return next(
            item
            for item in response.json()
            if item["provider_identity"] == "201"
        )

    try:
        scan()
        before = visible_message()
        http.request.return_value = {
            "stream": {"stream_id": 43, "name": "Private", "description": ""}
        }
        history_tests.incoming_tests.apply(
            incoming,
            {
                "type": "message",
                "message": history_tests.message(101, "Private target", stream_id=43),
            },
        )
        target = db.execute(
            "SELECT placement.uuid FROM messenger_messages message "
            "JOIN messenger_message_placements placement "
            "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
            "WHERE message.project_id=%s AND message.provider_identity='101'",
            (account["project_id"],),
        ).fetchone()[0]
        assert api.get(
            f"/v1/messenger/messages/{target}", user=linked.owner_user_uuid
        ).status_code == 404
        scan()
        assert visible_message() == before
    finally:
        linked.delete()
