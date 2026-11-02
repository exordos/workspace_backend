# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Deletion is durable before delivery, and imports cannot resurrect a root."""

import asyncio
import json
import uuid

import pytest
from oslo_config import cfg

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import counters
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal
from workspace.tests.integration import test_zulip_incoming as provider
from workspace.tests.integration import test_zulip_history as history_tests

conversation = journal.conversation
incoming = provider.incoming


def test_delete_removes_root_before_worker_and_delivers_to_read_recipient(
    api, db, conversation
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    message = messages[-1]
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (message["uuid"],),
    ).fetchone()[0]
    response = api.post(
        f"{native.MESSAGES}{message['uuid']}/actions/read/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    native._drain()
    response = api.delete(f"{native.MESSAGES}{message['uuid']}")
    assert response.status_code == 204, response.text
    assert (
        db.execute(
            "SELECT 1 FROM messenger_messages WHERE uuid=%s", (canonical,)
        ).fetchone()
        is None
    )
    assert (
        db.execute(
            "SELECT 1 FROM messenger_user_message_unreads WHERE placement_uuid=%s",
            (message["uuid"],),
        ).fetchone()
        is None
    )
    payload = db.execute(
        "SELECT payload FROM messenger_projection_tasks WHERE project_id=%s AND payload->>'source_kind'='message.deleted' AND payload->>'canonical_message_uuid'=%s",
        (api.project_id, str(canonical)),
    ).fetchone()[0]
    assert set(payload["recipients"]) == {str(peer), str(api.user_uuid)}
    assert payload["membership_generations"][str(peer)] >= 1
    native._drain()
    provider.publish()
    assert db.execute(
        "SELECT count(DISTINCT user_uuid) FROM m_workspace_visible_events WHERE project_id=%s AND action='deleted' AND payload->>'uuid'=%s",
        (api.project_id, message["uuid"]),
    ).fetchone() == (2,)


def test_access_delete_cascades_unread_fact(api, db, conversation):
    stream, messages, peer = conversation
    db.execute(
        "DELETE FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (api.project_id, peer, messages[-1]["uuid"]),
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (api.project_id, peer, stream["default_topic_uuid"]),
    ).fetchone() == (1,)
    native._drain()
    assert (
        api.get(
            f"{native.STREAM_TOPICS}{stream['default_topic_uuid']}", user=peer
        ).json()["unread_count"]
        == 1
    )


def test_count_plan_never_expands_message_or_access_rows(api, db, conversation):
    stream, _, peer = conversation
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + counters.COUNT_TOPIC_SQL,
        (api.project_id, peer, stream["default_topic_uuid"]),
    ).fetchone()[0]
    encoded = json.dumps(plan)
    for table in (
        "messenger_messages",
        "messenger_message_placements",
        "messenger_user_message_bindings",
    ):
        assert '"Relation Name": "' + table + '"' not in encoded


def test_unknown_provider_delete_fences_late_message(incoming):
    api, db, account, *_ = incoming
    provider.apply(
        incoming,
        {"type": "delete_message", "message_ids": [991]},
        provider.event(identifier=991),
    )
    assert (
        db.execute(
            "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity='991'",
            (api.project_id,),
        ).fetchone()
        is None
    )
    marker = uuid.uuid5(
        account["project_id"], f"{account['provider_uuid']}:message:991"
    )
    assert db.execute(
        "SELECT 1 FROM messenger_message_delete_tombstones WHERE project_id=%s AND canonical_message_uuid=%s",
        (api.project_id, marker),
    ).fetchone() == (1,)


def test_known_provider_delete_fences_replayed_message(incoming):
    api, db, *_ = incoming
    provider.apply(incoming, provider.event(identifier=992))
    provider.apply(incoming, {"type": "delete_message", "message_ids": [992]})
    assert (
        db.execute(
            "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity='992'",
            (api.project_id,),
        ).fetchone()
        is None
    )
    provider.apply(incoming, provider.event(identifier=992))
    assert (
        db.execute(
            "SELECT 1 FROM messenger_messages WHERE project_id=%s AND provider_identity='992'",
            (api.project_id,),
        ).fetchone()
        is None
    )


def test_prepared_history_page_cannot_recreate_deleted_message(incoming):
    api, db, account, *_ = incoming
    provider.apply(incoming, provider.event(identifier=993))
    history_tests.request(db, account)

    async def run():
        messages = {993: history_tests.message(993), 994: history_tests.message(994)}
        async with history_tests.worker(incoming, messages) as (service, _):
            current = await history_tests.job(service, account)
            batch = await service.operations.prepare_batch(
                current, list(messages.values())
            )
            canonical = db.execute(
                "SELECT uuid FROM messenger_messages WHERE project_id=%s AND provider_identity='993'",
                (api.project_id,),
            ).fetchone()[0]
            db.execute(
                "SELECT messenger_delete_message(%s,%s)", (api.project_id, canonical)
            )
            async with service.db.transaction() as connection:
                await service.operations.merge_batch(connection, current, batch)
            assert db.execute(
                "SELECT provider_identity FROM messenger_messages WHERE project_id=%s ORDER BY provider_identity",
                (api.project_id,),
            ).fetchall() == [("994",)]

    asyncio.run(run())


@pytest.mark.parametrize("source", ["native", "zulip"])
@pytest.mark.parametrize(
    "membership,default_active",
    [("all_messages", 2), ("mentions_only", 1), ("muted", 0)],
)
@pytest.mark.parametrize("personal", ["default", "follow", "mute", "unmute"])
def test_prepared_count_preserves_all_notification_policies(
    api, db, conversation, source, membership, default_active, personal
):
    stream, messages, peer = conversation
    provider_uuid = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_providers(uuid,hostname) VALUES(%s,%s)",
        (provider_uuid, str(provider_uuid) + ".invalid"),
    )
    db.execute(
        "UPDATE messenger_streams SET source_name=%s,provider_uuid=%s,provider_identity=%s WHERE project_id=%s AND uuid=%s",
        (
            source,
            provider_uuid if source == "zulip" else None,
            "42" if source == "zulip" else None,
            api.project_id,
            stream["uuid"],
        ),
    )
    db.execute(
        "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (membership, api.project_id, peer, stream["uuid"]),
    )
    values = (api.project_id, peer, stream["default_topic_uuid"])
    db.execute(
        "UPDATE messenger_user_topic_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (personal, *values),
    )
    db.execute(
        "INSERT INTO messenger_user_message_mentions VALUES(%s,%s,%s,%s,%s)",
        (api.project_id, peer, messages[-1]["uuid"], stream["uuid"], values[-1]),
    )
    overrides = {
        "native": {"follow": 2, "mute": 0, "unmute": 1},
        "zulip": {"follow": 2, "mute": 1, "unmute": 2},
    }
    assert db.execute(counters.COUNT_TOPIC_SQL, values).fetchone() == (
        2,
        overrides[source].get(personal, default_active),
    )


@pytest.mark.parametrize("resource", ["topic", "stream"])
def test_deleting_scope_preserves_canonical_message_placed_elsewhere(
    api, db, conversation, resource
):
    stream, messages, _ = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    other = api.post(
        native.STREAMS, json={"name": "Shared canonical destination"}
    ).json()
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (messages[-1]["uuid"],),
    ).fetchone()[0]
    placement = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_message_placements(uuid,project_id,message_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s)",
        (
            placement,
            api.project_id,
            canonical,
            other["uuid"],
            other["default_topic_uuid"],
        ),
    )
    db.execute(
        "INSERT INTO messenger_user_message_bindings(uuid,project_id,user_uuid,placement_uuid) VALUES(gen_random_uuid(),%s,%s,%s)",
        (api.project_id, api.user_uuid, placement),
    )
    shared = {"uuid": str(placement)}
    native._drain()
    target = (
        f"{native.STREAM_TOPICS}{stream['default_topic_uuid']}"
        if resource == "topic"
        else f"{native.STREAMS}{stream['uuid']}"
    )
    response = api.delete(target)
    assert response.status_code == 204, response.text
    assert db.execute(
        "SELECT 1 FROM messenger_messages WHERE uuid=%s", (canonical,)
    ).fetchone() == (1,)
    native._drain()
    assert api.get(f"{native.MESSAGES}{shared['uuid']}").status_code == 200
    assert api.get(f"{native.MESSAGES}{messages[-1]['uuid']}").status_code == 404


def test_deleted_uuid_cannot_be_recreated_by_native_api(api, db, conversation):
    stream, messages, _ = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (messages[-1]["uuid"],),
    ).fetchone()[0]
    assert api.delete(f"{native.MESSAGES}{messages[-1]['uuid']}").status_code == 204
    response = api.post(
        native.MESSAGES,
        json={
            "uuid": str(canonical),
            "stream_uuid": stream["uuid"],
            "payload": messages[-1]["payload"],
        },
    )
    assert response.status_code == 400, response.text


def test_late_send_echo_keeps_receipt_after_physical_delete(incoming):
    api, db, account, *_ = incoming
    provider.apply(incoming, provider.event(identifier=996))
    stream = api.get(native.STREAMS).json()[0]
    created = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Pending send"},
        },
    )
    assert created.status_code == 201, created.text
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (created.json()["uuid"],),
    ).fetchone()[0]
    delivery = uuid.uuid4()
    db.execute(
        "INSERT INTO bridge_zulip_v5_deliveries(uuid,project_id,message_uuid,stream_uuid,external_account_uuid,hostname,external_account_id,recipient,topic_name,sending) VALUES(%s,%s,%s,%s,%s,%s,'7','{}','General',true)",
        (
            delivery,
            api.project_id,
            canonical,
            stream["uuid"],
            account["uuid"],
            account["hostname"],
        ),
    )
    assert api.delete(f"{native.MESSAGES}{created.json()['uuid']}").status_code == 204
    echo = provider.event("Pending send", identifier=997)
    echo["local_message_id"] = str(canonical)
    provider.apply(incoming, echo)
    assert db.execute(
        "SELECT zulip_message_id,sending FROM bridge_zulip_v5_deliveries WHERE uuid=%s",
        (delivery,),
    ).fetchone() == (997, False)
    assert (
        db.execute(
            "SELECT 1 FROM messenger_messages WHERE uuid=%s", (canonical,)
        ).fetchone()
        is None
    )
