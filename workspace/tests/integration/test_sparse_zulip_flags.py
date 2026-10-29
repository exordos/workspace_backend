# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import concurrent.futures
import uuid

import psycopg
import pytest

from workspace.tests.integration import test_zulip_incoming

incoming = test_zulip_incoming.incoming


@pytest.mark.parametrize(
    "flag,present",
    [("read", True), ("read", False), ("starred", True), ("starred", False)],
)
def test_remote_sparse_flags_are_idempotent_and_do_not_recreate_bindings(
    incoming, flag, present
):
    api, db, account, _http, _url = incoming
    initial = test_zulip_incoming.event()
    initial["message"]["flags"] = [] if present else [flag]
    test_zulip_incoming.apply(incoming, initial)
    message = api.get("/v1/messenger/messages/").json()[0]
    placement = uuid.UUID(message["uuid"])
    binding = db.execute(
        "SELECT uuid FROM messenger_user_message_bindings WHERE project_id=%s "
        "AND user_uuid=%s AND placement_uuid=%s",
        (account["project_id"], account["owner_user_uuid"], placement),
    ).fetchone()[0]
    update = {
        "type": "update_message_flags",
        "messages": [101],
        "flag": flag,
        "op": "add" if present else "remove",
    }
    test_zulip_incoming.apply(incoming, update)
    first_events = test_zulip_incoming.client_events(incoming)
    test_zulip_incoming.apply(incoming, update)
    assert test_zulip_incoming.client_events(incoming) == first_events
    message = api.get("/v1/messenger/messages/").json()[0]
    assert message[flag] is present
    assert (
        db.execute(
            "SELECT uuid FROM messenger_user_message_bindings WHERE project_id=%s "
            "AND user_uuid=%s AND placement_uuid=%s",
            (account["project_id"], account["owner_user_uuid"], placement),
        ).fetchone()[0]
        == binding
    )
    table = "unreads" if flag == "read" else "stars"
    assert bool(
        db.execute(
            f"SELECT 1 FROM messenger_user_message_{table} "
            "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
            (account["project_id"], account["owner_user_uuid"], placement),
        ).fetchall()
    ) is (not present if flag == "read" else present)
    assert (
        db.execute(
            "SELECT count(*) FROM zulip_outgoing_intents WHERE project_id=%s "
            "AND entity_type='message_state'",
            (account["project_id"],),
        ).fetchone()[0]
        == 0
    )


def test_remote_flags_do_not_wait_for_the_counter_cache_row(incoming):
    api, _db, account, _http, url = incoming
    test_zulip_incoming.apply(incoming, test_zulip_incoming.event())
    stream = uuid.UUID(api.get("/v1/messenger/messages/").json()[0]["stream_uuid"])
    with psycopg.connect(url) as counter:
        counter.execute(
            "UPDATE messenger_stream_bindings SET unread_count=unread_count "
            "WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (account["project_id"], stream, account["owner_user_uuid"]),
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            remote = pool.submit(
                test_zulip_incoming.apply,
                incoming,
                {
                    "type": "update_message_flags",
                    "messages": [101],
                    "flag": "read",
                    "op": "add",
                },
            )
            try:
                remote.result(timeout=2)
            finally:
                counter.rollback()
    assert api.get("/v1/messenger/messages/").json()[0]["read"] is True
