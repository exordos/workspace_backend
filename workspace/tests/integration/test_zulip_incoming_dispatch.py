# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime
import uuid

import pytest
from psycopg.types import json as pg_json

from workspace.bridges.zulip import incoming_operations
from workspace.services import zulip_incoming
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


@pytest.fixture
def inbox(db):
    # Exercise the production selector and indexes without provider payloads or
    # foreign-key setup obscuring the scheduling regression.
    db.execute("""CREATE TEMP TABLE zulip_incoming_events (
        id bigint PRIMARY KEY, project_id uuid NOT NULL, provider_uuid uuid NOT NULL,
        ordering_keys text[] NOT NULL, payload jsonb NOT NULL,
        next_retry_at timestamptz NOT NULL, processed_at timestamptz
    )""")
    db.execute("""CREATE INDEX ON zulip_incoming_events
        (project_id,provider_uuid,id) WHERE processed_at IS NULL""")
    db.execute("CREATE INDEX ON zulip_incoming_events USING gin(ordering_keys)")
    return db


def ready_ids(events, now, in_flight, limit):
    """The existing pairwise ordering rule, independent of the SQL strategy."""
    pending = [event for event in events if not event["processed"]]
    ready = []
    for event in pending:
        if event["id"] in in_flight or event["retry"] > now:
            continue
        earlier = [
            other
            for other in pending
            if other["id"] < event["id"]
            and other["project"] == event["project"]
            and other["provider"] == event["provider"]
            and other["kind"] is not None
            and other["kind"] != "ensure_dependency"
        ]
        keys = set(event["keys"]) - {None}
        blocked = any(
            keys.intersection(set(other["keys"]) - {None})
            or other["keys"] == ["*"]
            or event["keys"] == ["*"]
            for other in earlier
        )
        if event["kind"] == "ensure_dependency" or not blocked:
            ready.append(event)
    ready.sort(
        key=lambda event: (
            event["kind"] != "ensure_dependency",
            event["retry"],
            event["id"],
        )
    )
    return [event["id"] for event in ready[:limit]]


@pytest.mark.parametrize(
    "in_flight,limit", [([], 128), ([1, 4, 7], 128), ([], 2), ([], 0)]
)
def test_dispatch_preserves_ordering_retry_dependencies_and_scope(
    inbox, in_flight, limit
):
    now = inbox.execute("SELECT now()").fetchone()[0]
    projects = [uuid.uuid4(), uuid.uuid4()]
    providers = [uuid.uuid4(), uuid.uuid4()]
    events = []

    def add(
        keys, *, kind="message", project=0, provider=0, future=False, processed=False
    ):
        value = {
            "id": len(events) + 1,
            "project": projects[project],
            "provider": providers[provider],
            "keys": keys,
            "kind": kind,
            "retry": now
            + datetime.timedelta(seconds=60 if future else -60 - len(events)),
            "processed": processed,
        }
        events.append(value)
        inbox.execute(
            "INSERT INTO zulip_incoming_events VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                value["id"],
                value["project"],
                value["provider"],
                keys,
                pg_json.Jsonb({"type": kind}),
                value["retry"],
                now if processed else None,
            ),
        )

    add(["message:1"], future=True)
    add(["message:1"])
    add(["message:1"], kind="ensure_dependency")
    add(["message:2", "message:2"])
    add(["message:2", "message:3"])
    add(["message:4"], processed=True)
    add(["message:4"])
    add(["*"], future=True)
    add([])
    add(["message:5"])
    add(["message:6"], kind="ensure_dependency")
    add(["message:1"], project=1)
    add(["message:1"], provider=1)
    add(["*", "message:8"], provider=1)
    add(["message:9"], provider=1)
    add(["message:9"], kind=None, provider=1)
    add([None], project=1)
    add([None], project=1)
    add(["message:10"], kind=None, project=1)
    add(["message:10"], project=1)
    add(["*"], kind="ensure_dependency", project=1)
    add([], project=1)
    selected = inbox.execute(
        zulip_incoming.SELECT_EVENT_SQL, (in_flight, limit)
    ).fetchall()
    assert [event[0] for event in selected] == ready_ids(events, now, in_flight, limit)


def test_dispatch_large_backlog_has_linear_lookup_work(inbox):
    project, provider = uuid.uuid4(), uuid.uuid4()
    size = 20000
    inbox.execute(
        """INSERT INTO zulip_incoming_events
        SELECT n,%s,%s,
          ARRAY[CASE WHEN n%%5=0 THEN 'account:one' ELSE 'message:'||n END],
          jsonb_build_object('type',CASE WHEN n%%5=0 THEN 'heartbeat' ELSE 'message' END,
                             'padding',repeat(md5(n::text),16)),
          now()-interval '1 minute',NULL
        FROM generate_series(1,%s) AS source(n)""",
        (project, provider, size),
    )
    inbox.execute("ANALYZE zulip_incoming_events")
    inbox.execute("SET statement_timeout='2s'")
    plan = inbox.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + zulip_incoming.SELECT_EVENT_SQL,
        ([], 128),
    ).fetchone()[0][0]["Plan"]

    def lookup_work(node):
        own = 0
        if node.get("Relation Name") == "zulip_incoming_events":
            own = (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node[
                "Actual Loops"
            ]
        return own + sum(lookup_work(child) for child in node.get("Plans", []))

    assert plan["Actual Rows"] == 128
    assert lookup_work(plan) < size * 3, plan
    assert (
        inbox.execute(
            "SELECT count(*) FROM zulip_incoming_events WHERE processed_at IS NULL"
        ).fetchone()[0]
        == size
    )


def test_fresh_message_reaches_api_while_unrelated_backlog_remains(incoming):
    api, db, account, http, url = incoming
    incoming_tests.apply(incoming, incoming_tests.event("Existing", 101))
    db.execute(
        """INSERT INTO zulip_incoming_events
        (project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,
         queue_id,event_id,payload,ordering_keys,next_retry_at)
        SELECT %s,%s,%s,%s,%s,'deferred-backlog',n,'{"type":"heartbeat"}'::jsonb,
               ARRAY['account:deferred'],now()+interval '1 day'
        FROM generate_series(1,2000) AS source(n)""",
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
            async with service.db.transaction() as connection:
                await service.append(
                    connection,
                    account,
                    {"queue_id": "fresh-after-backlog"},
                    [{"id": 1, **incoming_tests.event("Fresh after backlog", 102)}],
                )
            await service.dispatch()
            tasks = list(service.tasks.values())
            assert tasks
            await asyncio.gather(*tasks)
        finally:
            await service.db.close()

    asyncio.run(run())
    incoming_tests.publish()
    messages = api.get("/v1/messenger/messages/").json()
    assert "Fresh after backlog" in {
        message["payload"]["content"] for message in messages
    }
    assert (
        db.execute(
            "SELECT count(*) FROM zulip_incoming_events "
            "WHERE queue_id='deferred-backlog' AND processed_at IS NULL"
        ).fetchone()[0]
        == 2000
    )
