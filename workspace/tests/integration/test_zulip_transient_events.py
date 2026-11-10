# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio

import pytest

from workspace.bridges.zulip import incoming_operations
from workspace.services import zulip_incoming
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


@pytest.mark.parametrize("rollback", [False, True])
def test_transients_advance_cursor_without_durable_work(incoming, rollback):
    _, db, account, _, url = incoming

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        subscription = {"queue_id": "transients", "last_event_id": -1}
        try:
            await service.db.sql(
                zulip_incoming.SAVE_SUBSCRIPTION_SQL,
                (
                    account["uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    account["external_account_id"],
                    account["settings"]["server_url"],
                    "transients",
                    -1,
                    90,
                ),
            )

            async def append():
                async with service.admission() as (connection, stage):
                    await service.append(
                        connection,
                        account,
                        subscription,
                        [
                            {"id": 1, "type": "message", "message": {"id": 101}},
                            {"id": 2, "type": "unknown_future_event"},
                            {"id": 3, "type": "typing_edit_message"},
                            {"id": 4, "type": "typing"},
                            {"id": 5, "type": "heartbeat"},
                        ],
                        stage,
                    )
                    if rollback:
                        raise RuntimeError("rollback")

            if rollback:
                with pytest.raises(RuntimeError, match="rollback"):
                    await append()
            else:
                await append()
                await append()  # Retried delivery remains idempotent.
        finally:
            await service.db.close()

    asyncio.run(run())
    assert db.execute(
        "SELECT last_event_id FROM zulip_incoming_subscriptions WHERE external_account_uuid=%s",
        (account["uuid"],),
    ).fetchone() == ((-1 if rollback else 5),)
    assert db.execute(
        "SELECT payload->>'type' FROM zulip_incoming_events WHERE external_account_uuid=%s ORDER BY event_id",
        (account["uuid"],),
    ).fetchall() == ([] if rollback else [("message",), ("unknown_future_event",)])


def test_ready_transient_backlog_cannot_hide_fresh_message(incoming):
    api, db, account, http, url = incoming
    incoming_tests.apply(incoming, incoming_tests.event("Existing", 101))
    db.execute(
        """INSERT INTO zulip_incoming_events
        (project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,
         queue_id,event_id,payload,ordering_keys,next_retry_at)
        SELECT %s,%s,%s,%s,%s,'old-transients',n,
          jsonb_build_object('type',CASE n%%3 WHEN 0 THEN 'heartbeat' WHEN 1 THEN 'typing' ELSE 'typing_edit_message' END),
          ARRAY['account:'||n],now()-interval '1 hour'
        FROM generate_series(1,3000) source(n)""",
        (
            account["project_id"],
            account["uuid"],
            account["provider_uuid"],
            account["owner_user_uuid"],
            account["external_account_id"],
        ),
    )

    async def run():
        service = zulip_incoming.ZulipIncomingService(url)
        await service.db.open()
        service.operations = incoming_operations.Operations(service.db, http)
        try:
            async with service.admission() as (connection, stage):
                await service.append(
                    connection,
                    account,
                    {"queue_id": "fresh"},
                    [
                        {
                            "id": 1,
                            **incoming_tests.event("Fresh beside old transients", 102),
                        }
                    ],
                    stage,
                )
            await service.dispatch()
            await asyncio.gather(*list(service.tasks.values()))
        finally:
            await service.db.close()

    asyncio.run(run())
    incoming_tests.publish()
    assert "Fresh beside old transients" in {
        m["payload"]["content"] for m in api.get("/v1/messenger/messages/").json()
    }
    # Cleanup is bounded, independent of the dispatch window; old audit rows stay.
    remaining = db.execute(
        "SELECT count(*) FROM zulip_incoming_events WHERE queue_id='old-transients' AND processed_at IS NULL"
    ).fetchone()[0]
    assert 0 < remaining < 3000
    assert db.execute(
        "SELECT count(*) FROM zulip_incoming_events WHERE queue_id='old-transients'"
    ).fetchone() == (3000,)
