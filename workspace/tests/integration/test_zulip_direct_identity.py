# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Direct addressing across imported and IAM user identities."""

import asyncio
import importlib.util
import json
import uuid

import pytest
from psycopg import rows
from psycopg.types import json as pg_json
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.messenger_api.dm import external_models

from workspace.bridges.zulip import incoming_operations
from workspace.services import zulip_incoming
from workspace.services import zulip_outgoing
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests

outgoing = outgoing_tests.outgoing
direct_outgoing = outgoing_tests.direct_outgoing


@pytest.mark.parametrize("legacy_self", [False, True])
@pytest.mark.parametrize("old_snapshot", [False, True])
def test_reverse_direct_send_uses_the_other_participant(
    direct_outgoing, db, _test_database_url, legacy_self, old_snapshot
):
    outgoing, peer = direct_outgoing
    api, account, channel, *_ = outgoing
    endpoint = account.owner_user_uuid
    if legacy_self:
        endpoint = uuid.uuid5(account.provider_uuid, "user:7")
        conftest.seed_workspace_user(db, endpoint, f"zulip-{endpoint}")
        db.execute(
            "UPDATE m_workspace_users SET source='hidden' WHERE uuid=%s", (endpoint,)
        )
        db.execute(incoming_operations.REGISTER_USER_SQL, (api.project_id, endpoint))
    conftest.seed_user_stream_binding(
        db, api.project_id, channel.stream_uuid, peer.owner_user_uuid
    )
    db.execute(
        "UPDATE messenger_streams SET owner_uuid=%s,direct_user_uuid=%s WHERE uuid=%s",
        (peer.owner_user_uuid, endpoint, channel.stream_uuid),
    )
    outgoing_tests.create(outgoing)
    assert outgoing_tests.intents(db, account)[0][3]["recipient_ids"] == [9]
    if old_snapshot:
        snapshot = outgoing_tests.intents(db, account)[0][3]
        snapshot["direct_user_uuid"] = str(endpoint)
        snapshot["recipient_ids"] = [7]
        db.execute(
            "UPDATE zulip_outgoing_intents SET data=%s WHERE external_account_uuid=%s",
            (pg_json.Jsonb(snapshot), account.uuid),
        )

    async def run():
        async with outgoing_tests.sender(account, _test_database_url) as (
            service,
            server,
        ):
            await outgoing_tests.drain(service)
            sent = [
                json.loads(data["to"])
                for method, path, data in server.calls
                if method == "POST" and path == "/api/v1/messages"
            ]
            assert sent == [[9]]

    asyncio.run(run())


@pytest.mark.parametrize("api_operation", ["create", "update"])
def test_api_link_rebinds_existing_imported_direct_endpoint(
    direct_outgoing, db, _test_database_url, api_operation
):
    outgoing, peer = direct_outgoing
    api, account, channel, *_ = outgoing
    queued = outgoing_tests.create(outgoing)
    canonical = uuid.uuid4()
    settings = {
        "server_url": account.settings["server_url"],
        "email": "linked@example.com",
        "api_key": "linked-test-key",
    }
    root = "/v1/messenger/external_accounts/"
    permissions = (
        "workspace.external_account.create",
        "workspace.external_account.update",
    )
    created = api.post(
        root,
        user=canonical,
        permissions=permissions,
        json={
            "provider": "zulip",
            "settings": settings
            if api_operation == "create"
            else {**settings, "api_key": "old-test-key"},
        },
    )
    assert created.status_code == 201, created.text
    account_uuid = created.json()["uuid"]
    if api_operation == "update":
        updated = api.put(
            root + account_uuid,
            user=canonical,
            permissions=permissions,
            json={"settings": settings},
        )
        assert updated.status_code == 200, updated.text
    with db.cursor(row_factory=rows.dict_row) as cursor:
        cursor.execute(
            zulip_incoming.SELECT_ACCOUNTS_SQL + " AND account.uuid=%s",
            (uuid.UUID(account_uuid),),
        )
        linked = cursor.fetchone()

    async def run():
        database = zulip_outgoing.Database(_test_database_url)
        await database.open()
        try:
            operations = incoming_operations.Operations(database, None)
            async with database.transaction() as connection:
                await connection.execute(
                    "SELECT set_config('workspace.zulip_incoming','on',true)"
                )
                result = await operations.initialize(
                    connection,
                    linked,
                    {
                        "user_id": 9,
                        "full_name": "Linked peer",
                        "email": "linked@example.com",
                    },
                )
                assert result["owner_user_uuid"] == canonical
        finally:
            await database.close()

    try:
        asyncio.run(run())
        assert db.execute(
            "SELECT direct_user_uuid FROM messenger_streams WHERE uuid=%s",
            (channel.stream_uuid,),
        ).fetchone() == (canonical,)
        assert db.execute(
            "SELECT owner_user_uuid FROM m_external_accounts_v2 WHERE uuid=%s",
            (peer.uuid,),
        ).fetchone() == (canonical,)
        assert db.execute(
            "SELECT active FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, channel.stream_uuid, canonical),
        ).fetchone() == (True,)

        assert db.execute(
            "SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s AND kind='topic.created' AND %s=ANY(recipients)",
            (api.project_id, canonical),
        ).fetchone() == (1,)

        async def send_both_directions():
            async with outgoing_tests.sender(account, _test_database_url) as (
                service,
                server,
            ):
                await outgoing_tests.drain(service)
                sent = [
                    json.loads(data["to"])
                    for method, path, data in server.calls
                    if method == "POST" and path == "/api/v1/messages"
                ]
                assert sent == [[9]]
            reply = api.post(
                "/v1/messenger/messages/",
                user=canonical,
                json={
                    "stream_uuid": str(channel.stream_uuid),
                    "payload": {"kind": "markdown", "content": "Reply after linking"},
                },
            )
            assert reply.status_code == 201, reply.text
            linked_account = external_models.ExternalAccount.objects.get_one(
                filters={"uuid": filters.EQ(peer.uuid)}
            )
            db.execute(
                """INSERT INTO zulip_incoming_subscriptions
                (external_account_uuid,project_id,provider_uuid,owner_user_uuid,account_identity,
                 server_url,queue_id,last_event_id,longpoll_timeout)
                VALUES (%s,%s,%s,%s,'9',%s,'linked-queue',-1,90)""",
                (
                    linked_account.uuid,
                    api.project_id,
                    account.provider_uuid,
                    canonical,
                    settings["server_url"],
                ),
            )
            async with outgoing_tests.sender(linked_account, _test_database_url) as (
                service,
                server,
            ):
                server.next_id = 200
                await outgoing_tests.drain(service)
                sent = [
                    json.loads(data["to"])
                    for method, path, data in server.calls
                    if method == "POST" and path == "/api/v1/messages"
                ]
                assert sent == [[7]]
                assert list(server.messages.values())[0]["sender_id"] == 9
            assert (
                api.get(f"/v1/messenger/messages/{queued['uuid']}").status_code == 200
            )

        asyncio.run(send_both_directions())
    finally:
        db.execute(
            "DELETE FROM m_external_accounts_v2 WHERE owner_user_uuid=%s", (canonical,)
        )


@pytest.mark.isolated_schema
@pytest.mark.parametrize("legacy_side", ["owner", "peer"])
@pytest.mark.parametrize("existing_binding", [False, True])
@pytest.mark.parametrize("legacy_active", [False, True])
def test_forward_migration_repairs_alias_and_preserves_prepared_receipts(
    direct_outgoing,
    db,
    _test_database_url,
    legacy_side,
    existing_binding,
    legacy_active,
):
    outgoing, peer = direct_outgoing
    api, account, channel, *_ = outgoing
    spec = importlib.util.spec_from_file_location(
        "direct_identity_repair",
        conftest.MIGRATIONS_DIR
        / "0283-Repair-linked-Zulip-direct-participant-identities-6b50ae.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with contexts.Context().session_manager() as session:
        module.migration_step.downgrade(session)
    legacy = uuid.uuid5(account.provider_uuid, "user:7")
    conftest.seed_workspace_user(db, legacy, f"zulip-{legacy}")
    db.execute("UPDATE m_workspace_users SET source='hidden' WHERE uuid=%s", (legacy,))
    db.execute(incoming_operations.REGISTER_USER_SQL, (api.project_id, legacy))
    conftest.seed_user_stream_binding(
        db, api.project_id, channel.stream_uuid, peer.owner_user_uuid
    )
    db.execute(
        "UPDATE messenger_streams SET owner_uuid=%s,direct_user_uuid=%s WHERE uuid=%s",
        (
            legacy if legacy_side == "owner" else peer.owner_user_uuid,
            peer.owner_user_uuid if legacy_side == "owner" else legacy,
            channel.stream_uuid,
        ),
    )
    outgoing_tests.create(outgoing)
    outgoing_tests.create(outgoing)
    conftest.seed_user_stream_binding(db, api.project_id, channel.stream_uuid, legacy)
    db.execute(
        "UPDATE messenger_stream_bindings SET active=%s,notification_mode='mentions' "
        "WHERE stream_uuid=%s AND user_uuid=%s",
        (legacy_active, channel.stream_uuid, legacy),
    )
    db.execute(
        "UPDATE messenger_stream_bindings SET active=false WHERE stream_uuid=%s AND user_uuid=%s",
        (channel.stream_uuid, account.owner_user_uuid),
    )
    topic_uuid = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE uuid=%s",
        (channel.stream_uuid,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO messenger_user_topic_bindings(uuid,project_id,topic_uuid,user_uuid,notification_mode) "
        "VALUES (%s,%s,%s,%s,'mute')",
        (uuid.uuid4(), api.project_id, topic_uuid, legacy),
    )
    if not existing_binding:
        db.execute(
            "DELETE FROM messenger_user_topic_bindings WHERE topic_uuid=%s AND user_uuid=%s",
            (topic_uuid, account.owner_user_uuid),
        )
        db.execute(
            "DELETE FROM messenger_stream_bindings WHERE stream_uuid=%s AND user_uuid=%s",
            (channel.stream_uuid, account.owner_user_uuid),
        )
    intents = db.execute(
        "SELECT id,entity_uuid FROM zulip_outgoing_intents WHERE external_account_uuid=%s ORDER BY id",
        (account.uuid,),
    ).fetchall()
    stale = db.execute(
        "SELECT data FROM zulip_outgoing_intents WHERE id=%s", (intents[0][0],)
    ).fetchone()[0]
    stale["recipient_ids"] = [7]
    if legacy_side == "owner":
        stale.pop("direct_user_uuid")
        stale.pop("channel_type")
    db.execute(
        "UPDATE zulip_outgoing_intents SET data=%s,error='waiting for old alias',next_attempt_at=now()+interval '1 day' WHERE id=%s",
        (pg_json.Jsonb(stale), intents[0][0]),
    )
    prepared = {
        "kind": "send",
        "data": {"to": [7], "local_id": str(intents[1][1])},
    }
    db.execute(
        "UPDATE zulip_outgoing_intents SET status='checking',request=%s,error='preserve receipt' WHERE id=%s",
        (pg_json.Jsonb(prepared), intents[1][0]),
    )
    before = db.execute(
        "SELECT data,request,error,next_attempt_at FROM zulip_outgoing_intents WHERE id=%s",
        (intents[1][0],),
    ).fetchone()
    with contexts.Context().session_manager() as session:
        module.migration_step.upgrade(session)
        module.migration_step.upgrade(session)
    assert db.execute(
        "SELECT direct_user_uuid FROM messenger_streams WHERE uuid=%s",
        (channel.stream_uuid,),
    ).fetchone() == (
        peer.owner_user_uuid if legacy_side == "owner" else account.owner_user_uuid,
    )
    assert db.execute(
        "SELECT owner_uuid FROM messenger_streams WHERE uuid=%s", (channel.stream_uuid,)
    ).fetchone() == (
        account.owner_user_uuid if legacy_side == "owner" else peer.owner_user_uuid,
    )
    assert db.execute(
        "SELECT active,notification_mode FROM messenger_stream_bindings WHERE stream_uuid=%s AND user_uuid=%s",
        (channel.stream_uuid, account.owner_user_uuid),
    ).fetchone() == (
        (False, "all_messages") if existing_binding else (legacy_active, "mentions")
    )
    assert db.execute(
        "SELECT active FROM messenger_stream_bindings WHERE stream_uuid=%s AND user_uuid=%s",
        (channel.stream_uuid, legacy),
    ).fetchone() == (False,)
    assert db.execute(
        "SELECT notification_mode FROM messenger_user_topic_bindings WHERE topic_uuid=%s AND user_uuid=%s",
        (topic_uuid, account.owner_user_uuid),
    ).fetchone() == (("default",) if existing_binding else ("mute",))
    assert db.execute(
        "SELECT data->'recipient_ids',data->>'direct_user_uuid',request,status FROM zulip_outgoing_intents WHERE id=%s",
        (intents[0][0],),
    ).fetchone() == (
        [9],
        str(
            peer.owner_user_uuid if legacy_side == "owner" else account.owner_user_uuid
        ),
        None,
        "new",
    )
    assert db.execute(
        "SELECT data->>'channel_type' FROM zulip_outgoing_intents WHERE id=%s",
        (intents[0][0],),
    ).fetchone() == ("private",)
    assert db.execute(
        "SELECT error IS NULL,next_attempt_at<=now() FROM zulip_outgoing_intents WHERE id=%s",
        (intents[0][0],),
    ).fetchone() == (True, True)
    assert (
        db.execute(
            "SELECT data,request,error,next_attempt_at FROM zulip_outgoing_intents WHERE id=%s",
            (intents[1][0],),
        ).fetchone()
        == before
    )
    if legacy_active and not existing_binding:

        async def recover():
            async with outgoing_tests.sender(account, _test_database_url) as (
                service,
                server,
            ):
                queued = await service.db.sql(
                    "SELECT * FROM zulip_outgoing_intents WHERE id=%s", (intents[0][0],)
                )
                await service.process(queued[0])
                sent = [
                    json.loads(data["to"])
                    for method, path, data in server.calls
                    if method == "POST" and path == "/api/v1/messages"
                ]
                assert sent == [[9]]
                with contexts.Context().session_manager() as session:
                    module.migration_step.upgrade(session)
                assert db.execute(
                    "SELECT status FROM zulip_outgoing_intents WHERE id=%s",
                    (intents[0][0],),
                ).fetchone() == ("done",)
                assert (
                    db.execute(
                        "SELECT data,request,error,next_attempt_at FROM zulip_outgoing_intents WHERE id=%s",
                        (intents[1][0],),
                    ).fetchone()
                    == before
                )

        asyncio.run(recover())
    assert db.execute(
        "SELECT count(*) FROM messenger_messages WHERE uuid=ANY(%s)",
        (
            [
                intents[0][1],
                intents[1][1],
            ],
        ),
    ).fetchone() == (2,)
    assert db.execute(
        "SELECT 1 FROM m_workspace_users WHERE uuid=%s", (legacy,)
    ).fetchone() == (1,)


@pytest.mark.isolated_schema
def test_rebind_repairs_only_matching_imported_direct_conversations(
    direct_outgoing, db
):
    outgoing, peer = direct_outgoing
    api, account, channel, *_ = outgoing
    legacy = uuid.uuid5(account.provider_uuid, "user:9")
    conftest.seed_workspace_user(db, legacy, f"zulip-{legacy}")
    db.execute("UPDATE m_workspace_users SET source='hidden' WHERE uuid=%s", (legacy,))
    db.execute(incoming_operations.REGISTER_USER_SQL, (api.project_id, legacy))
    db.execute(
        "UPDATE messenger_streams SET direct_user_uuid=%s WHERE uuid=%s",
        (legacy, channel.stream_uuid),
    )
    other_provider, other_project = uuid.uuid4(), uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_providers(uuid,hostname) VALUES (%s,'other-zulip.example.com')",
        (other_provider,),
    )
    db.execute(incoming_operations.REGISTER_USER_SQL, (other_project, legacy))
    untouched = []
    for source, private, direct, invite_only, announce, project, provider in (
        ("native", True, legacy, False, False, api.project_id, None),
        ("native", True, None, False, False, api.project_id, None),
        ("zulip", True, None, False, False, api.project_id, account.provider_uuid),
        ("zulip", False, None, False, False, api.project_id, account.provider_uuid),
        ("zulip", False, None, True, False, api.project_id, account.provider_uuid),
        ("zulip", False, None, False, True, api.project_id, account.provider_uuid),
        ("zulip", True, legacy, False, False, api.project_id, other_provider),
        ("zulip", True, legacy, False, False, other_project, account.provider_uuid),
    ):
        stream = uuid.uuid4()
        db.execute(
            """INSERT INTO messenger_streams
            (uuid,project_id,name,owner_uuid,source_name,provider_uuid,provider_identity,
             private,direct_user_uuid,invite_only,announce)
            VALUES (%s,%s,'Unrelated conversation',%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                stream,
                project,
                legacy,
                source,
                provider,
                str(stream) if provider else None,
                private,
                direct,
                invite_only,
                announce,
            ),
        )
        untouched.append((stream, legacy, direct))
    try:
        assert db.execute(
            "SELECT zulip_rebind_direct_user(%s,%s,%s,%s)",
            (api.project_id, account.provider_uuid, legacy, peer.owner_user_uuid),
        ).fetchall() == [(channel.stream_uuid,)]
        for stream, owner, direct in untouched:
            assert db.execute(
                "SELECT owner_uuid,direct_user_uuid FROM messenger_streams WHERE uuid=%s",
                (stream,),
            ).fetchone() == (owner, direct)
        assert db.execute(
            "SELECT direct_user_uuid FROM messenger_streams WHERE uuid=%s",
            (channel.stream_uuid,),
        ).fetchone() == (peer.owner_user_uuid,)
    finally:
        db.execute(
            "DELETE FROM messenger_streams WHERE uuid=ANY(%s)",
            ([stream for stream, _, _ in untouched],),
        )
        db.execute("DELETE FROM messenger_providers WHERE uuid=%s", (other_provider,))
