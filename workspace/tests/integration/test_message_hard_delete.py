# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Deletion delivery survives removal of the canonical root and visibility."""

import asyncio
import json
import socket
import time
from unittest import mock

import pytest
import websockets
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mutation_journal_opts
from workspace.messenger_api import publication, websocket_service
from workspace.messenger_api.api import v2_store
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal

conversation = journal.conversation


def _delete(api, db, public):
    root = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (api.project_id, public["uuid"]),
    ).fetchone()[0]
    response = api.delete(f"{native.MESSAGES}{public['uuid']}")
    assert response.status_code == 204, response.text
    assert not db.execute(
        "SELECT 1 FROM messenger_messages WHERE project_id=%s AND uuid=%s",
        (api.project_id, root),
    ).fetchall()
    assert not db.execute(
        "SELECT 1 FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s",
        (api.project_id, root),
    ).fetchall()
    return root


@pytest.mark.parametrize("index", [0, 1])
def test_delete_removes_root_before_projection_and_preserves_delivery(
    api, db, conversation, index
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    _delete(api, db, messages[index])
    snapshots = db.execute(
        "SELECT payload FROM messenger_domain_outbox_events WHERE project_id=%s "
        "AND payload->>'source_kind'='message.deleted'",
        (api.project_id,),
    ).fetchall()
    assert len(snapshots) == 1
    payload = snapshots[0][0]
    assert set(payload["recipients"]) == {str(api.user_uuid), str(peer)}
    assert set(payload["membership_generations"]) == set(payload["recipients"])
    native._drain()
    assert api.get(f"{native.MESSAGES}{messages[index]['uuid']}").status_code == 404
    result = api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()
    assert result["last_message_uuid"] == messages[1 - index]["uuid"]
    assert result["unread_count"] == 1
    events = api.get(native.EVENTS, user=peer).json()
    assert any(
        event["payload"]["kind"] == "message.deleted"
        and event["payload"]["uuid"] == messages[index]["uuid"]
        for event in events
    )


def test_delete_snapshot_and_root_removal_roll_back_together(api, db, conversation):
    _stream, messages, _peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    with pytest.raises(RuntimeError, match="rollback deletion"):
        with contexts.Context().session_manager() as session:
            store = v2_store.MessengerV2Store(api.project_id, api.user_uuid)
            store.delete_message(messages[0]["uuid"])
            assert not session.execute(
                "SELECT 1 FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
                (api.project_id, messages[0]["uuid"]),
            ).fetchall()
            raise RuntimeError("rollback deletion")
    assert api.get(f"{native.MESSAGES}{messages[0]['uuid']}").status_code == 200
    assert not db.execute(
        "SELECT 1 FROM messenger_domain_outbox_events WHERE project_id=%s "
        "AND payload->>'source_kind'='message.deleted'",
        (api.project_id,),
    ).fetchall()


def test_socket_delivers_delete_after_root_is_gone(
    api, db, conversation, _test_database_url
):
    _stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)

    async def run():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = websocket_service.MessengerEventsWebsocketServer(
            db_url=_test_database_url,
            iam_engine_driver=mock.Mock(),
            heartbeat_interval=25,
            client_timeout=5,
            catchup_limit=500,
            send_queue_limit=32,
            poll_interval=0.02,
        )
        server._authenticator.authenticate = lambda token: (str(peer), api.project_id)
        serving = asyncio.create_task(server.serve("127.0.0.1", port))
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    websocket = await websockets.connect(
                        f"ws://127.0.0.1:{port}/v1/events/ws",
                        subprotocols=["workspace.events.v1", "bearer.test"],
                    )
                    break
                except OSError:
                    assert not serving.done()
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.01)
            try:
                while (
                    json.loads(await asyncio.wait_for(websocket.recv(), 5)).get("type")
                    != "ready"
                ):
                    pass
                await asyncio.to_thread(_delete, api, db, messages[0])
                await asyncio.to_thread(native._drain)
                while True:
                    frame = json.loads(await asyncio.wait_for(websocket.recv(), 5))
                    if (
                        frame.get("object_type") == "message"
                        and frame.get("action") == "deleted"
                        and frame["payload"]["uuid"] == messages[0]["uuid"]
                    ):
                        break
            finally:
                await websocket.close()
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("deferred", [False, True])
def test_frozen_delete_audience_keeps_the_original_membership_generation(
    api, db, conversation, deferred
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    _delete(api, db, messages[0])
    original_generation = db.execute(
        "SELECT membership_generation FROM messenger_stream_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (api.project_id, peer, stream["uuid"]),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_stream_bindings SET active=false "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (api.project_id, peer, stream["uuid"]),
    )
    db.execute(
        "UPDATE messenger_stream_bindings SET active=true,membership_generation=membership_generation+1 "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (api.project_id, peer, stream["uuid"]),
    )
    if deferred:
        with publication.defer():
            native._drain()
    else:
        native._drain()
    guards = db.execute(
        "SELECT guard.membership_generation,member.membership_generation "
        "FROM m_workspace_broadcast_message_events_v1 event "
        "JOIN messenger_event_membership_guards guard ON guard.event_uuid=event.uuid "
        "JOIN messenger_stream_bindings member ON member.project_id=event.project_id "
        "AND member.user_uuid=guard.user_uuid AND member.stream_uuid=%s "
        "WHERE event.project_id=%s AND guard.user_uuid=%s "
        "AND event.payload->>'kind'='message.deleted' AND event.payload->>'uuid'=%s",
        (stream["uuid"], api.project_id, peer, messages[0]["uuid"]),
    ).fetchall()
    assert guards == [(original_generation, original_generation + 1)], guards
    events = api.get(native.EVENTS, user=peer).json()
    assert not any(
        event["payload"]["kind"] == "message.deleted"
        and event["payload"]["uuid"] == messages[0]["uuid"]
        for event in events
    )
    events = api.get(native.EVENTS).json()
    assert any(
        event["payload"]["kind"] == "message.deleted"
        and event["payload"]["uuid"] == messages[0]["uuid"]
        for event in events
    )


def test_delete_freezes_more_than_one_recipient_batch(api, db, conversation):
    stream, messages, _peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    db.execute(
        "CREATE TEMP TABLE hard_delete_users AS SELECT gen_random_uuid() AS uuid FROM generate_series(1,1001)"
    )
    db.execute("""INSERT INTO m_workspace_users(uuid,username,source,status,avatar,last_ping_at,created_at,updated_at)
        SELECT uuid,'delete-'||uuid::text,'iam','active','urn:gravatar:'||md5(uuid::text),now(),now(),now()
        FROM hard_delete_users""")
    db.execute(
        "INSERT INTO messenger_project_users(project_id,user_uuid) SELECT %s,uuid FROM hard_delete_users",
        (api.project_id,),
    )
    db.execute(
        """INSERT INTO messenger_stream_bindings
        (uuid,project_id,stream_uuid,user_uuid,who_uuid,active,membership_generation,role,notification_mode,notification_updated_at,created_at,updated_at)
        SELECT gen_random_uuid(),%s,%s,uuid,%s,true,1,'member','all_messages',now(),now(),now() FROM hard_delete_users""",
        (api.project_id, stream["uuid"], api.user_uuid),
    )
    db.execute(
        """INSERT INTO messenger_user_topic_bindings(uuid,project_id,user_uuid,topic_uuid)
        SELECT gen_random_uuid(),%s,uuid,%s FROM hard_delete_users""",
        (api.project_id, stream["default_topic_uuid"]),
    )
    db.execute(
        """INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid)
        SELECT gen_random_uuid(),%s,uuid,%s FROM hard_delete_users""",
        (api.project_id, messages[0]["uuid"]),
    )
    _delete(api, db, messages[0])
    payloads = [
        row[0]
        for row in db.execute(
            "SELECT payload FROM messenger_domain_outbox_events WHERE project_id=%s AND payload->>'source_kind'='message.deleted'",
            (api.project_id,),
        ).fetchall()
    ]
    assert len(payloads) == 2
    recipients = [user for payload in payloads for user in payload["recipients"]]
    assert len(recipients) == len(set(recipients)) == 1003
    assert max(len(payload["recipients"]) for payload in payloads) <= 1000
    native._drain()
    assert not db.execute(
        "SELECT 1 FROM messenger_projection_tasks WHERE project_id=%s AND status<>'completed'",
        (api.project_id,),
    ).fetchall()
    assert db.execute(
        "SELECT count(DISTINCT user_uuid) FROM m_workspace_visible_events "
        "WHERE project_id=%s AND payload->>'kind'='message.deleted' AND payload->>'uuid'=%s",
        (api.project_id, messages[0]["uuid"]),
    ).fetchone() == (1003,)


def test_delete_migration_round_trip_preserves_existing_messages(api, db, conversation):
    _stream, messages, _peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    before = db.execute(
        "SELECT uuid,content_uuid FROM messenger_messages WHERE project_id=%s ORDER BY uuid",
        (api.project_id,),
    ).fetchall()
    migration_name = (
        "0287-Capture-message-deletion-before-removing-canonical-rows-7749a8.py"
    )
    migration = conftest.DataMigration(migration_name)
    try:
        migration.rollback_migration(migration_name)
    finally:
        migration.apply_migration(migration_name)
    assert (
        db.execute(
            "SELECT uuid,content_uuid FROM messenger_messages WHERE project_id=%s ORDER BY uuid",
            (api.project_id,),
        ).fetchall()
        == before
    )
    _delete(api, db, messages[0])
    native._drain()
