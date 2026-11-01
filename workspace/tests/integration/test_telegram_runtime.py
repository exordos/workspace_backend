"""Durability and source ordering through the real PostgreSQL schema."""

import asyncio
import base64
import time
import datetime
import importlib.util
import pathlib
import types as python_types
import uuid
from unittest import mock

import pytest
from telethon import sessions
from telethon.tl import functions, types

from workspace.bridges import messenger
from workspace.bridges.telegram import credentials
from workspace.services import telegram
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_incoming as incoming_tests


@pytest.fixture
def telegram_runtime(telegram_account, db, _test_database_url):
    api, root = telegram_account
    identifier = uuid.UUID(root.rsplit("/", 1)[1])
    conftest.seed_workspace_user(
        db, uuid.UUID(api.user_uuid), f"telegram-owner-{identifier}"
    )
    db.execute(
        "SELECT messenger_v2_register_project_user(%s,%s)",
        (uuid.UUID(api.project_id), uuid.UUID(api.user_uuid)),
    )
    db.execute(
        'UPDATE m_external_accounts_v2 SET external_account_id=\'7\',status=\'syncing\',settings=\'{"history_depth":"all","peer_keys":["user:8"]}\' WHERE uuid=%s',
        (identifier,),
    )
    db.execute(
        "INSERT INTO telegram_account_state(external_account_uuid,auth_state,pts,qts,cursor_date) VALUES(%s,'authorized',1,0,now())",
        (identifier,),
    )
    envelope = credentials.encrypt(
        identifier,
        {"peer": base64.b64encode(bytes(types.InputPeerUser(8, 77))).decode()},
        "synthetic",
    )
    db.execute(
        "INSERT INTO telegram_peer_state(external_account_uuid,peer_key,title,input_peer_envelope,peer_kind,can_send,selected) VALUES(%s,'user:8','Telegram test',%s,'user',true,true)",
        (identifier, messenger.json_value(envelope)),
    )

    async def execute(callback):
        service = telegram.TelegramService(
            _test_database_url,
            12345,
            "synthetic",
            "synthetic",
            batch_size=2,
            poll_interval=0.01,
        )
        await service.db.open()
        try:
            account = (
                await service.db.sql(
                    "SELECT * FROM m_external_accounts_v2 WHERE uuid=%s", (identifier,)
                )
            )[0]
            peer = (
                await service.db.sql(
                    "SELECT * FROM telegram_peer_state WHERE external_account_uuid=%s",
                    (identifier,),
                )
            )[0]
            return await callback(service, account, peer)
        finally:
            await service.db.close()

    return api, db, lambda callback: asyncio.run(execute(callback))


def message(identifier=41, text="History", **values):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "id": identifier,
        "identity": f"account:7:user:8:message:{identifier}",
        "peer": "user:8",
        "topic_id": 0,
        "sender_id": 8,
        "text": text,
        "date": now.isoformat(),
        "edited_at": now.isoformat(),
        "observed_at": now.isoformat(),
        "out": False,
        "reply_to": None,
        "files": [],
        "reactions": {},
        "reaction_actors": [],
        **values,
    }


def test_old_history_cannot_overwrite_live_edit_in_the_same_second(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        old = message(text="Before")
        new = {
            **old,
            "text": "After",
            "observed_at": (
                datetime.datetime.fromisoformat(old["observed_at"])
                + datetime.timedelta(seconds=1)
            ).isoformat(),
        }
        await service.merge_batch(account, peer, [new])
        await service.merge_batch(account, peer, [old])

    run(scenario)
    assert api.get("/v1/messenger/messages/").json()[0]["payload"]["content"] == "After"


def test_checkpoint_and_messages_rollback_together(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        original = service.operations.history_batch

        async def failed_after_write(*args, **kwargs):
            await original(*args, **kwargs)
            raise ValueError("synthetic import failure")

        with mock.patch.object(
            service.operations, "history_batch", side_effect=failed_after_write
        ):
            with pytest.raises(ValueError):
                await service.merge_batch(
                    account, peer, [message()], history={"before": 41}
                )

    run(scenario)
    assert (
        db.execute(
            "SELECT count(*) FROM telegram_incoming_events WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
            (uuid.UUID(api.project_id),),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT history_before_id FROM telegram_peer_state WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
            (uuid.UUID(api.project_id),),
        ).fetchone()[0]
        == 0
    )
    assert api.get("/v1/messenger/messages/").json() == []


def test_service_only_history_page_advances_and_resumes_after_restart(telegram_runtime):
    api, db, run = telegram_runtime
    now = datetime.datetime.now(datetime.timezone.utc)
    calls = []

    async def client(request):
        assert isinstance(request, functions.messages.GetHistoryRequest)
        calls.append(request.offset_id)
        if request.offset_id == 0:
            messages = [
                types.MessageService(
                    id=i,
                    peer_id=types.PeerUser(8),
                    date=now,
                    action=types.MessageActionHistoryClear(),
                )
                for i in (10, 9)
            ]
        else:
            messages = [
                types.Message(
                    id=8,
                    peer_id=types.PeerUser(8),
                    from_id=types.PeerUser(8),
                    date=now,
                    message="Oldest",
                )
            ]
        return python_types.SimpleNamespace(messages=messages, users=[], chats=[])

    async def first(service, account, peer):
        original = service.merge_batch

        async def merge(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()

        with mock.patch.object(service, "merge_batch", side_effect=merge):
            with pytest.raises(asyncio.CancelledError):
                await service.history_loop(account, client)

    run(first)
    assert (
        db.execute(
            "SELECT history_before_id FROM telegram_peer_state WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
            (uuid.UUID(api.project_id),),
        ).fetchone()[0]
        == 9
    )

    async def resumed(service, account, peer):
        task = asyncio.create_task(service.history_loop(account, client))
        for _ in range(100):
            if (await service.selected(account))[0]["history_done"]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(resumed)
    assert calls == [0, 9]
    assert (
        api.get("/v1/messenger/messages/").json()[0]["payload"]["content"] == "Oldest"
    )
    assert db.execute(
        "SELECT history_done FROM telegram_peer_state WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
        (uuid.UUID(api.project_id),),
    ).fetchone()[0]


def test_authoritative_gap_reconciliation_keeps_newer_live_messages(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        old = message(41)
        await service.merge_batch(account, peer, [old])
        started = datetime.datetime.now(datetime.timezone.utc)
        await service.merge_batch(account, peer, [message(42, "New during fetch")])
        history = {
            "before": 0,
            "done": True,
            "started_at": started,
            "files": 0,
            "bytes": 0,
            "fetch_seconds": 0,
            "file_seconds": 0,
            "merge_started": time.monotonic(),
            "covered_ids": [],
            "lower": 0,
            "upper": 100,
        }
        await service.merge_batch(account, peer, [], history=history)

    run(scenario)
    values = api.get("/v1/messenger/messages/").json()
    assert [item["payload"]["content"] for item in values] == ["New during fetch"]


def test_abandoned_qr_attempt_expires_without_remote_calls(telegram_runtime):
    api, db, run = telegram_runtime
    db.execute(
        "UPDATE telegram_account_state SET auth_state='starting',attempt_expires_at=now()-interval '1 second' WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
        (uuid.UUID(api.project_id),),
    )

    async def scenario(service, account, peer):
        client = mock.Mock()
        client.session = sessions.StringSession()
        with pytest.raises(asyncio.CancelledError):
            await service.authenticate(account, client)
        client.assert_not_called()

    run(scenario)
    state = db.execute(
        "SELECT auth_state,session_envelope,password_envelope,login_envelope FROM telegram_account_state WHERE external_account_uuid IN (SELECT uuid FROM m_external_accounts_v2 WHERE project_id=%s)",
        (uuid.UUID(api.project_id),),
    ).fetchone()
    assert state == ("expired", None, None, None)


def test_outgoing_http_send_and_own_echo_share_one_message_after_restart(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "From Workspace"},
        },
    )
    assert response.status_code == 201, response.text
    local = response.json()
    assert local["source_name"] == "telegram"

    async def scenario(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='message' ORDER BY id",
                (account["uuid"],),
            )
        )[0]
        # An authoritative echo can arrive before the send response. Keep it durable.
        echo = message(99, "From Workspace", out=True, sender_id=7)
        await service.merge_batch(account, peer, [echo])
        assert (
            len(
                await service.db.sql(
                    "SELECT uuid FROM messenger_messages WHERE project_id=%s",
                    (account["project_id"],),
                )
            )
            == 2
        )
        received = []

        async def client(request):
            assert isinstance(request, functions.messages.SendMessageRequest)
            assert request.random_id == intent["random_id"]
            assert request.message == "From Workspace"
            received.append(request)
            return types.UpdateShortSentMessage(
                id=99,
                pts=2,
                pts_count=1,
                date=datetime.datetime.now(datetime.timezone.utc),
                out=True,
            )

        await service.send(account, intent, client)
        await service.merge_batch(account, peer, [echo])
        await service.send(
            account,
            (
                await service.db.sql(
                    "SELECT * FROM telegram_outgoing_intents WHERE id=%s",
                    (intent["id"],),
                )
            )[0],
            client,
        )
        assert len(received) == 1
        assert (
            await service.db.sql(
                "SELECT processed_at FROM telegram_incoming_events WHERE external_account_uuid=%s AND payload->>'identity'=%s",
                (account["uuid"], echo["identity"]),
            )
        )[0]["processed_at"]

    run(scenario)
    values = api.get("/v1/messenger/messages/").json()
    assert len(values) == 2
    saved = next(item for item in values if item["uuid"] == local["uuid"])
    assert saved["provider_identity"].endswith(":message:99")
    assert saved["status"] == "active"


def test_remote_source_creation_and_edit_timestamps_survive_sql_triggers(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        value = message(
            date="2024-01-01T01:02:03+00:00", edited_at="2024-01-02T01:02:03+00:00"
        )
        await service.merge_batch(account, peer, [value])
        value = {**value, "text": "Edited", "edited_at": "2024-01-03T01:02:03+00:00"}
        await service.merge_batch(account, peer, [value])
        saved = (
            await service.db.sql(
                "SELECT created_at,content_updated_at FROM messenger_messages WHERE project_id=%s",
                (account["project_id"],),
            )
        )[0]
        assert saved["created_at"] == datetime.datetime(2024, 1, 1, 1, 2, 3)
        assert saved["content_updated_at"] == datetime.datetime(2024, 1, 3, 1, 2, 3)

    run(scenario)


@pytest.mark.parametrize(
    ("mime", "kind"),
    [("text/plain", "file"), ("image/png", "image"), ("video/mp4", "video")],
)
def test_imported_attachments_use_real_http_acl_and_media_urns(
    telegram_runtime, monkeypatch, tmp_path, mime, kind
):
    api, db, run = telegram_runtime
    monkeypatch.setenv("WORKSPACE_FILE_STORAGE_PATH", str(tmp_path))
    content = b"Telegram attachment bytes"
    now = datetime.datetime.now(datetime.timezone.utc)
    document = types.Document(
        id=90,
        access_hash=1,
        file_reference=b"",
        date=now,
        mime_type=mime,
        size=len(content),
        dc_id=2,
        attributes=[types.DocumentAttributeFilename("example.bin")],
    )
    value = types.Message(
        id=88,
        peer_id=types.PeerUser(8),
        from_id=types.PeerUser(8),
        date=now,
        message="Attachment",
        media=types.MessageMediaDocument(document=document),
    )
    client = mock.Mock(download_media=mock.AsyncMock(return_value=content))

    async def scenario(service, account, peer):
        prepared, *_ = await service.prepare(account, peer, [value], client)
        info = {
            "anchor": 0,
            "before": 88,
            "done": True,
            "started_at": now,
            "files": 1,
            "bytes": len(content),
            "fetch_seconds": 0.01,
            "file_seconds": 0.01,
            "merge_started": time.monotonic(),
            "covered_ids": [88],
            "lower": 0,
            "upper": 9223372036854775807,
        }
        await service.merge_batch(account, peer, prepared, history=info)
        # Replay reads the stored object, avoiding another provider download.
        repeated, *_ = await service.prepare(account, peer, [value], client)
        await service.merge_batch(account, peer, repeated)

    run(scenario)
    file = db.execute(
        "SELECT uuid FROM m_workspace_files WHERE project_id=%s", (api.project_id,)
    ).fetchone()[0]
    path = f"/v1/messenger/files/{file}"
    response = api.get(path + "/actions/download")
    assert response.status_code == 200, response.text
    assert response.content == content
    assert api.get(path + "/actions/download", user=uuid.uuid4()).status_code == 404
    assert api.get(path + "/actions/download", project=uuid.uuid4()).status_code == 404
    assert (
        f"(urn:{kind}:{file})"
        in api.get("/v1/messenger/messages/").json()[0]["payload"]["content"]
    )
    assert client.download_media.await_count == 1


def test_marking_message_read_captures_only_the_owner_receipt(telegram_runtime):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    response = api.post(f"/v1/messenger/messages/{saved['uuid']}/actions/read/invoke")
    assert response.status_code == 200, response.text

    async def scenario(service, account, peer):
        intents = await service.db.sql(
            "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='read'",
            (account["uuid"],),
        )
        assert len(intents) == 1
        client = mock.AsyncMock()
        await service.send(account, intents[0], client)
        request = client.call_args.args[0]
        assert isinstance(request, functions.messages.ReadHistoryRequest)
        assert request.max_id == 41

    run(scenario)


def test_revoked_authorization_requires_an_explicit_new_login(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        client = mock.Mock(
            connect=mock.AsyncMock(),
            disconnect=mock.AsyncMock(),
            is_user_authorized=mock.AsyncMock(return_value=False),
            log_out=mock.AsyncMock(),
        )
        service.client_factory = mock.Mock(return_value=client)
        with mock.patch.object(
            service,
            "authenticate",
            new=mock.AsyncMock(
                side_effect=AssertionError("Must not silently restart login")
            ),
        ) as authenticate:
            await service.account(account)
            authenticate.assert_not_awaited()
        state = await service.state(account)
        assert state["auth_state"] == "expired"
        public = (
            await service.db.sql(
                "SELECT status,error_description FROM m_external_accounts_v2 WHERE uuid=%s",
                (account["uuid"],),
            )
        )[0]
        assert public == {"status": "error", "error_description": "AUTH_REQUIRED"}

    run(scenario)


def test_telegram_channel_cannot_be_shared_with_a_workspace_outsider(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        peer = {**peer, "peer_key": "channel:123", "peer_kind": "channel"}
        # The canonical stream is scoped to verified source memberships, even for a public channel.
        async with service.db.transaction() as connection:
            await service.operations.stream(connection, account, peer)

    run(scenario)
    stream = api.get("/v1/messenger/streams/").json()[0]
    outsider = uuid.uuid4()
    conftest.seed_workspace_user(db, outsider, f"user-{outsider}")
    db.execute(
        "SELECT messenger_v2_register_project_user(%s,%s)", (api.project_id, outsider)
    )
    assert (
        api.get(f"/v1/messenger/streams/{stream['uuid']}", user=outsider).status_code
        == 404
    )
    response = api.post(
        f"/v1/messenger/streams/{stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(outsider)]},
    )
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("operation", ["edit", "reaction", "delete"])
def test_message_actions_reach_the_provider_with_the_source_id(
    telegram_runtime, operation
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message(out=True, sender_id=7)])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    url = f"/v1/messenger/messages/{saved['uuid']}"
    if operation == "edit":
        response = api.put(
            url,
            json={"payload": {"kind": "markdown", "content": "Edited in Workspace"}},
        )
    elif operation == "reaction":
        response = api.post(
            "/v1/messenger/message_reactions/",
            json={"message_uuid": saved["uuid"], "emoji_name": "thumbs_up"},
        )
    else:
        response = api.delete(url)
    assert response.status_code in (200, 201, 204), response.text

    async def scenario(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s ORDER BY id DESC",
                (account["uuid"],),
            )
        )[0]
        call = mock.AsyncMock(return_value=python_types.SimpleNamespace(updates=[]))
        client = mock.Mock()
        client.delete_messages = mock.AsyncMock()

        class Client:
            async def __call__(self, request):
                return await call(request)

            async def delete_messages(self, *args, **kwargs):
                return await client.delete_messages(*args, **kwargs)

        await service.send(account, intent, Client())
        if operation == "delete":
            assert client.delete_messages.call_args.args[1] == [41]
        elif operation == "edit":
            request = call.call_args.args[0]
            assert isinstance(request, functions.messages.EditMessageRequest)
            assert request.id == 41
            assert request.message == "Edited in Workspace"
        else:
            request = call.call_args.args[0]
            assert isinstance(request, functions.messages.SendReactionRequest)
            assert request.msg_id == 41
            assert request.reaction[0].emoticon == "👍"

    run(scenario)


def test_incoming_reaction_echo_reuses_the_workspace_fact_and_can_remove_it(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    response = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": saved["uuid"], "emoji_name": "thumbs_up"},
    )
    assert response.status_code == 201, response.text
    original = db.execute(
        "SELECT uuid FROM messenger_message_reaction_facts WHERE project_id=%s",
        (api.project_id,),
    ).fetchone()[0]

    async def echo(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='reaction'",
                (account["uuid"],),
            )
        )[0]
        await service.send(
            account,
            intent,
            mock.AsyncMock(return_value=python_types.SimpleNamespace(updates=[])),
        )
        received = message(
            reactions={"👍": 1},
            reaction_actors=[{"user_id": 7, "emoji": "👍"}],
        )
        await service.merge_batch(account, peer, [received])
        await service.merge_batch(account, peer, [received])

    run(echo)
    facts = db.execute(
        "SELECT uuid,user_uuid,emoji_name FROM messenger_message_reaction_facts WHERE project_id=%s",
        (api.project_id,),
    ).fetchall()
    assert facts == [(original, uuid.UUID(api.user_uuid), "thumbs_up")]
    assert api.get("/v1/messenger/messages/").json()[0]["reactions"] == {"thumbs_up": 1}

    async def remove(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(remove)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_message_reaction_facts WHERE project_id=%s",
            (api.project_id,),
        ).fetchone()[0]
        == 0
    )
    assert api.get("/v1/messenger/messages/").json()[0]["reactions"] == {}


def test_delete_uses_its_durable_snapshot_after_local_root_cleanup(telegram_runtime):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message(out=True, sender_id=7)])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    assert api.delete(f"/v1/messenger/messages/{saved['uuid']}").status_code == 204
    db.execute("DELETE FROM messenger_messages WHERE project_id=%s", (api.project_id,))

    async def scenario(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s ORDER BY id DESC",
                (account["uuid"],),
            )
        )[0]
        client = mock.Mock(delete_messages=mock.AsyncMock())
        await service.send(account, intent, client)
        client.delete_messages.assert_awaited_once()
        assert client.delete_messages.call_args.args[1] == [41]

    run(scenario)


def test_source_echo_does_not_replace_a_newer_queued_local_edit(telegram_runtime):
    api, db, run = telegram_runtime
    original = message(out=True, sender_id=7)

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [original])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    response = api.put(
        f"/v1/messenger/messages/{saved['uuid']}",
        json={"payload": {"kind": "markdown", "content": "Latest queued local edit"}},
    )
    assert response.status_code == 200, response.text

    async def scenario(service, account, peer):
        later = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)
        ).isoformat()
        echo = {
            **original,
            "text": "Earlier remote edit echo",
            "edited_at": later,
            "observed_at": later,
        }
        await service.merge_batch(account, peer, [echo])

    run(scenario)
    assert (
        api.get(f"/v1/messenger/messages/{saved['uuid']}").json()["payload"]["content"]
        == "Latest queued local edit"
    )


def test_deferred_edit_echo_is_refetched_after_the_provider_acknowledgement(
    telegram_runtime,
):
    api, db, run = telegram_runtime
    original = message(out=True, sender_id=7)

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [original])

    run(seed)
    saved = api.get("/v1/messenger/messages/").json()[0]
    assert (
        api.put(
            f"/v1/messenger/messages/{saved['uuid']}",
            json={
                "payload": {"kind": "markdown", "content": "Latest queued local edit"}
            },
        ).status_code
        == 200
    )

    async def scenario(service, account, peer):
        await service.merge_batch(
            account,
            peer,
            [
                {
                    **original,
                    "text": "Earlier remote echo",
                    "observed_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                }
            ],
        )
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s ORDER BY id DESC",
                (account["uuid"],),
            )
        )[0]
        await service.finish_send(account, intent, 41)
        now = datetime.datetime.now(datetime.timezone.utc)
        latest = types.Message(
            id=41,
            peer_id=types.PeerUser(8),
            from_id=types.PeerUser(7),
            date=datetime.datetime.fromisoformat(original["date"]),
            edit_date=now,
            message="Latest queued local edit",
            out=True,
        )
        client = mock.Mock(get_messages=mock.AsyncMock(return_value=[latest]))
        await service.replay_pending(account, peer, client)
        client.get_messages.assert_awaited_once()
        assert not await service.db.sql(
            "SELECT id FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NULL",
            (account["uuid"],),
        )

    run(scenario)
    assert (
        api.get(f"/v1/messenger/messages/{saved['uuid']}").json()["payload"]["content"]
        == "Latest queued local edit"
    )


def test_all_discovered_dialogs_are_enabled_without_a_saved_selection(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.db.sql(
            "UPDATE m_external_accounts_v2 SET settings='{}' WHERE uuid=%s",
            (account["uuid"],),
        )

        async def dialogs():
            for identifier in (8, 9):
                yield python_types.SimpleNamespace(
                    entity=types.User(id=identifier, deleted=identifier == 9),
                    input_entity=types.InputPeerUser(identifier, 77),
                    title=f"Discovered {identifier}",
                    dialog=python_types.SimpleNamespace(read_inbox_max_id=0),
                )

        client = mock.AsyncMock(return_value=python_types.SimpleNamespace(users=[]))
        client.iter_dialogs = dialogs
        await service.catalog(account, client)
        peers = await service.selected(account)
        assert {p["peer_key"] for p in peers} == {"user:8", "user:9"}
        assert next(p for p in peers if p["peer_key"] == "user:9")["can_send"] is False
        response = api.get("/v1/users/")
        assert response.status_code == 200, response.text
        users = [user for user in response.json() if user["source"] == "telegram"]
        assert len(users) == 2

    run(scenario)


def test_full_history_ignores_legacy_depth_and_imports_old_messages(telegram_runtime):
    api, db, run = telegram_runtime
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=365)

    async def scenario(service, account, peer):
        await service.db.sql(
            "UPDATE m_external_accounts_v2 SET settings=%s WHERE uuid=%s",
            (
                messenger.json_value(
                    {"history_depth": "30_days", "peer_keys": ["user:8"]}
                ),
                account["uuid"],
            ),
        )

        async def client(request):
            return python_types.SimpleNamespace(
                messages=[
                    types.Message(
                        id=17,
                        peer_id=types.PeerUser(8),
                        from_id=types.PeerUser(8),
                        date=old,
                        message="A year of history",
                    )
                ],
                users=[],
                chats=[],
            )

        original = service.merge_batch

        async def merge(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError()

        with mock.patch.object(service, "merge_batch", side_effect=merge):
            with pytest.raises(asyncio.CancelledError):
                await service.history_loop(account, client)

    run(scenario)
    rows = api.get("/v1/messenger/messages/").json()
    assert [row["payload"]["content"] for row in rows] == ["A year of history"]


def test_buffered_bulk_page_survives_failure_and_restart_without_refetch(
    telegram_runtime,
):
    api, db, run = telegram_runtime
    now = datetime.datetime.now(datetime.timezone.utc)
    client = mock.AsyncMock(
        return_value=python_types.SimpleNamespace(
            messages=[
                types.Message(
                    id=17,
                    peer_id=types.PeerUser(8),
                    from_id=types.PeerUser(8),
                    date=now,
                    message="Durable page",
                )
            ],
            users=[],
            chats=[],
        )
    )

    async def failed(service, account, peer):
        prepared, info = await service.history_page(account, peer, client)
        info["merge_started"] = time.monotonic()
        original = service.operations.history_batch

        async def after_write(*args, **kwargs):
            await original(*args, **kwargs)
            raise ValueError("synthetic failure after bulk write")

        with mock.patch.object(
            service.operations, "history_batch", side_effect=after_write
        ):
            with pytest.raises(ValueError):
                await service.merge_batch(account, peer, prepared, history=info)
        assert (await service.selected(account))[0]["history_before_id"] == 0

    run(failed)
    assert api.get("/v1/messenger/messages/").json() == []

    async def resumed(service, account, peer):
        prepared, info = await service.history_page(account, peer, client)
        info["merge_started"] = time.monotonic()
        with mock.patch.object(
            service.operations,
            "message",
            side_effect=AssertionError("History must use the shared bulk writer"),
        ):
            assert await service.merge_batch(account, peer, prepared, history=info) == 1
        assert not await service.db.sql(
            "SELECT 1 FROM telegram_history_batches WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        assert not await service.db.sql(
            "SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s AND kind LIKE 'message.%%'",
            (account["project_id"],),
        )

    run(resumed)
    client.assert_awaited_once()
    assert (
        api.get("/v1/messenger/messages/").json()[0]["payload"]["content"]
        == "Durable page"
    )


def test_contacts_create_stable_directory_users_without_overwriting_iam_profile(
    telegram_runtime,
):
    api, db, run = telegram_runtime
    client = mock.AsyncMock(
        return_value=types.contacts.Contacts(
            contacts=[],
            saved_count=1,
            users=[
                types.User(
                    id=8,
                    first_name="Remote",
                    last_name="Contact",
                    username="remote_contact",
                ),
                types.User(id=7, first_name="Different IAM name"),
            ],
        )
    )

    async def scenario(service, account, peer):
        await service.contacts(account, client)
        await service.contacts(account, client)
        people = await service.db.sql(
            "SELECT uuid,username,first_name,last_name,source FROM m_workspace_users WHERE uuid=ANY(%s::uuid[]) ORDER BY uuid",
            (
                [
                    account["owner_user_uuid"],
                    uuid.uuid5(account["provider_uuid"], "user:8"),
                ],
            ),
        )
        remote = next(row for row in people if row["source"] == "telegram")
        assert (
            remote["username"] == "remote_contact"
            and remote["first_name"] == "Remote"
            and remote["last_name"] == "Contact"
        )
        own = next(row for row in people if row["source"] == "iam")
        assert own["first_name"] != "Different IAM name"

    run(scenario)
    users = api.get("/v1/users/").json()
    assert len([user for user in users if user["username"] == "remote_contact"]) == 1


def test_periodic_catalog_discovers_later_dialogs_without_manual_selection(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        async def dialogs():
            for identifier in (8, 9):
                yield python_types.SimpleNamespace(
                    entity=types.User(
                        id=identifier, first_name=f"Contact {identifier}"
                    ),
                    input_entity=types.InputPeerUser(identifier, 77),
                    title=f"Contact {identifier}",
                    dialog=python_types.SimpleNamespace(read_inbox_max_id=0),
                )

        client = mock.AsyncMock(return_value=python_types.SimpleNamespace(users=[]))
        client.iter_dialogs = dialogs
        calls = 0

        async def pause(delay):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise asyncio.CancelledError()

        with mock.patch.object(telegram.asyncio, "sleep", side_effect=pause):
            with pytest.raises(asyncio.CancelledError):
                await service.catalog_loop(account, client)
        assert {row["peer_key"] for row in await service.selected(account)} == {
            "user:8",
            "user:9",
        }

    run(scenario)


def test_new_direct_update_discovers_the_dialog_before_the_periodic_catalog(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        response = types.updates.Difference(
            new_messages=[
                types.Message(
                    id=51,
                    peer_id=types.PeerUser(9),
                    date=now,
                    message="New direct chat",
                )
            ],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[],
            state=types.updates.State(pts=2, qts=0, date=now, seq=0, unread_count=0),
        )

        async def dialogs():
            for identifier in (8, 9):
                yield python_types.SimpleNamespace(
                    entity=types.User(
                        id=identifier, first_name=f"Contact {identifier}"
                    ),
                    input_entity=types.InputPeerUser(identifier, 77),
                    title=f"Contact {identifier}",
                    dialog=python_types.SimpleNamespace(read_inbox_max_id=0),
                )

        client = mock.AsyncMock(return_value=response)
        client.iter_dialogs = dialogs
        wake = asyncio.Event()
        refreshed = asyncio.Event()
        original_catalog = service.catalog

        async def refresh(*args):
            await original_catalog(*args)
            refreshed.set()

        service.catalog = refresh
        task = asyncio.create_task(service.catalog_loop(account, client, wake))
        try:
            await service.receive_common(account, [peer], client, wake)
            await asyncio.wait_for(refreshed.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        discovered = {row["peer_key"]: row for row in await service.selected(account)}
        assert set(discovered) == {"user:8", "user:9"}
        assert not discovered["user:9"]["history_done"]
        assert (await service.state(account))["pts"] == 2
        return str(uuid.uuid5(account["provider_uuid"], "user:9"))

    identifier = run(scenario)
    assert any(user["uuid"] == identifier for user in api.get("/v1/users/").json())


def test_history_does_not_restore_a_read_flag_while_its_provider_ack_is_pending(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    row = api.get("/v1/messenger/messages/").json()[0]
    assert (
        api.post(
            f"/v1/messenger/messages/{row['uuid']}/actions/read/invoke"
        ).status_code
        == 200
    )

    async def scenario(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        source = mock.AsyncMock(
            return_value=python_types.SimpleNamespace(
                messages=[
                    types.Message(
                        id=41,
                        peer_id=types.PeerUser(8),
                        from_id=types.PeerUser(8),
                        date=now,
                        message="History",
                    )
                ],
                users=[],
                chats=[],
            )
        )
        prepared, info = await service.history_page(account, peer, source)
        info["merge_started"] = time.monotonic()
        await service.merge_batch(account, peer, prepared, history=info)
        assert not await service.db.sql(
            "SELECT 1 FROM messenger_user_message_unreads WHERE project_id=%s",
            (account["project_id"],),
        )
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='read'",
                (account["uuid"],),
            )
        )[0]
        await service.send(account, intent, mock.AsyncMock())
        latest = (await service.selected(account))[0]
        assert latest["read_max_id"] >= 41
        info["anchor"] = latest["history_before_id"]
        info["merge_started"] = time.monotonic()
        await service.merge_batch(account, latest, prepared, history=info)
        assert not await service.db.sql(
            "SELECT 1 FROM messenger_user_message_unreads WHERE project_id=%s",
            (account["project_id"],),
        )

    run(scenario)


@pytest.mark.parametrize(
    "kind,remote,folder",
    [
        ("user", 8, "00000000-0000-0000-0000-000000000001"),
        ("chat", 123, "00000000-0000-0000-0000-000000000002"),
        ("channel", 123, "00000000-0000-0000-0000-000000000002"),
    ],
)
def test_telegram_system_folders_separate_direct_chats_from_channels_without_exposing_them(
    telegram_runtime, kind, remote, folder
):
    api, db, run = telegram_runtime
    peer_key = f"{kind}:{remote}"

    async def scenario(service, account, peer):
        await service.db.sql(
            "UPDATE telegram_peer_state SET peer_key=%s,peer_kind=%s WHERE external_account_uuid=%s AND peer_key=%s",
            (peer_key, kind, account["uuid"], peer["peer_key"]),
        )
        peer = {**peer, "peer_key": peer_key, "peer_kind": kind}
        identity = (
            f"channel:{remote}:message:41"
            if kind == "channel"
            else f"account:7:{peer_key}:message:41"
        )
        await service.merge_batch(
            account, peer, [message(identity=identity, peer=peer_key)]
        )

    run(scenario)
    incoming_tests.drain()
    stream = api.get("/v1/messenger/streams/").json()[0]
    assert stream["private"] is True
    memberships = db.execute(
        "SELECT folder_uuid,chat_type FROM messenger_folder_items WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (api.project_id, api.user_uuid, stream["uuid"]),
    ).fetchall()
    assert {str(row[0]) for row in memberships} == {
        "00000000-0000-0000-0000-000000000000",
        folder,
    }
    assert {row[1] for row in memberships} == {
        "private" if kind == "user" else "stream"
    }
    response = api.get(f"/v1/messenger/folders/{folder}")
    assert response.status_code == 200, response.text
    assert response.json()["folder_items"][0]["stream_uuid"] == stream["uuid"]
    assert (
        api.get(
            f"/v1/messenger/streams/{stream['uuid']}", user=uuid.uuid4()
        ).status_code
        == 404
    )


def test_history_yields_to_pending_realtime_then_resumes(telegram_runtime):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.db.sql(
            "INSERT INTO telegram_incoming_events(external_account_uuid,event_key,payload,received_at) VALUES(%s,'pressure',%s,now()-interval '1 second')",
            (account["uuid"], messenger.json_value({"peer": peer["peer_key"]})),
        )
        client = mock.AsyncMock(
            return_value=python_types.SimpleNamespace(messages=[], users=[], chats=[])
        )
        task = asyncio.create_task(service.history_loop(account, client))
        try:
            await asyncio.sleep(0.05)
            client.assert_not_awaited()
            await service.db.sql(
                "UPDATE telegram_incoming_events SET processed_at=now() WHERE external_account_uuid=%s",
                (account["uuid"],),
            )

            async def completed():
                while not (await service.selected(account))[0]["history_done"]:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(completed(), 2)
            client.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run(scenario)


@pytest.mark.isolated_schema
@pytest.mark.parametrize("revision", ["0298"])
def test_identity_upgrade_preserves_history_and_canonical_messages(
    telegram_runtime,
    revision,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        await service.db.sql(
            "UPDATE telegram_peer_state SET history_before_id=41,history_done=true,imported_messages=1 WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.db.sql(
            "INSERT INTO telegram_history_batches(external_account_uuid,peer_key,before_id,prepared,info) VALUES(%s,%s,41,'[]','{}')",
            (account["uuid"], peer["peer_key"]),
        )
        if revision == "0298":
            # Reproduce a pre-upgrade imported person without the provider
            # identity needed by outgoing mentions. Never remove the owner.
            await service.db.sql(
                "DELETE FROM m_external_accounts_v2 WHERE project_id=%s AND provider='telegram' AND external_account_id='8'",
                (account["project_id"],),
            )

    run(scenario)
    before = api.get("/v1/messenger/messages/").json()[0]["uuid"]
    path = next(
        (pathlib.Path(__file__).parents[3] / "migrations").glob(revision + "-*.py")
    )
    spec = importlib.util.spec_from_file_location("telegram_conversion_upgrade", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with incoming_tests.contexts.Context().session_manager() as session:
        module.migration_step.upgrade(session)

    async def verify(service, account, peer):
        assert peer["history_before_id"] == 41 and peer["history_done"]
        assert peer["imported_messages"] == 1
        assert account["external_account_id"] == "7"
        state = (
            await service.db.sql(
                "SELECT auth_state,pts FROM telegram_account_state WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        )[0]
        assert state == {"auth_state": "authorized", "pts": 1}
        if revision == "0298":
            identities = await service.db.sql(
                "SELECT identity.owner_user_uuid,state.auth_state FROM m_external_accounts_v2 identity "
                "LEFT JOIN telegram_account_state state ON state.external_account_uuid=identity.uuid "
                "WHERE identity.project_id=%s AND identity.provider='telegram' AND identity.external_account_id='8'",
                (account["project_id"],),
            )
            assert identities == [
                {
                    "owner_user_uuid": uuid.uuid5(account["provider_uuid"], "user:8"),
                    "auth_state": None,
                }
            ]
        assert await service.db.sql(
            "SELECT 1 FROM telegram_history_batches WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.merge_batch(account, peer, [message(text="*Fixed italic*")])

    run(verify)
    messages = api.get("/v1/messenger/messages/").json()
    assert len(messages) == 1 and messages[0]["uuid"] == before
    assert messages[0]["payload"]["content"] == "*Fixed italic*"


def test_periodic_catalog_rebinds_existing_direct_counterpart_after_verified_link(
    telegram_runtime,
):
    api, db, run = telegram_runtime
    linked_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, linked_owner, f"linked-{linked_owner}")

    async def scenario(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        stream = (await service.selected(account))[0]["stream_uuid"]
        legacy = (
            await service.db.sql(
                "SELECT direct_user_uuid FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                (account["project_id"], stream),
            )
        )[0]["direct_user_uuid"]
        assert legacy != linked_owner
        identifier = uuid.uuid4()
        linked = (
            await service.db.sql(
                "INSERT INTO m_external_accounts_v2(uuid,project_id,provider,provider_uuid,owner_user_uuid,status,settings) VALUES(%s,%s,'telegram',%s,%s,'syncing','{}') RETURNING *",
                (
                    identifier,
                    account["project_id"],
                    account["provider_uuid"],
                    linked_owner,
                ),
            )
        )[0]
        await service.db.sql(
            "INSERT INTO telegram_account_state(external_account_uuid,auth_state,pts) VALUES(%s,'authorized',1)",
            (identifier,),
        )
        login = mock.AsyncMock()
        login.get_me.return_value = types.User(id=8, first_name="Linked peer")
        await service.initialize(linked, login)
        mapping = await service.db.sql(
            "SELECT owner_user_uuid FROM m_external_accounts_v2 WHERE project_id=%s AND provider='telegram' AND external_account_id='8'",
            (account["project_id"],),
        )
        assert len(mapping) == 1 and mapping[0]["owner_user_uuid"] == linked_owner

        async def dialogs():
            yield python_types.SimpleNamespace(
                entity=types.User(id=8, first_name="Linked peer"),
                input_entity=types.InputPeerUser(8, 77),
                title="Linked peer",
                dialog=python_types.SimpleNamespace(read_inbox_max_id=0),
            )

        await service.catalog(account, mock.Mock(iter_dialogs=dialogs))
        refreshed = (
            await service.db.sql(
                "SELECT direct_user_uuid,private FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                (account["project_id"], stream),
            )
        )[0]
        assert refreshed["direct_user_uuid"] == linked_owner
        assert refreshed["private"]
        assert not await service.db.sql(
            "SELECT 1 FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s AND active",
            (account["project_id"], stream, linked_owner),
        )
        assert (
            len(
                await service.db.sql(
                    "SELECT 1 FROM messenger_message_placements WHERE project_id=%s AND stream_uuid=%s",
                    (account["project_id"], stream),
                )
            )
            == 1
        )

    run(scenario)


@pytest.mark.parametrize("reference", ["message", "quote"])
def test_outgoing_workspace_reply_reference_is_not_a_telegram_url(
    telegram_runtime, reference
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    original = api.get("/v1/messenger/messages/").json()[0]
    stream = api.get("/v1/messenger/streams/").json()[0]
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {
                "kind": "markdown",
                "content": f"[Original](urn:{reference}:{original['uuid']})\n\nAnswer",
            },
        },
    )
    assert response.status_code == 201, response.text

    async def scenario(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='message' ORDER BY id DESC LIMIT 1",
                (account["uuid"],),
            )
        )[0]
        client = mock.AsyncMock(
            return_value=types.UpdateShortSentMessage(
                id=99,
                pts=2,
                pts_count=1,
                date=datetime.datetime.now(datetime.timezone.utc),
                out=True,
            )
        )
        await service.send(account, intent, client)
        request = client.call_args.args[0]
        assert request.reply_to.reply_to_msg_id == 41
        assert request.message == "Answer"
        assert not request.entities

    run(scenario)


def test_outgoing_file_reference_inside_code_is_literal(telegram_runtime):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])

    run(seed)
    stream = api.get("/v1/messenger/streams/").json()[0]
    literal = f"[example](urn:file:{uuid.uuid4()})"
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": f"`{literal}`"},
        },
    )
    assert response.status_code == 201, response.text

    async def scenario(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='message' ORDER BY id DESC LIMIT 1",
                (account["uuid"],),
            )
        )[0]
        client = mock.AsyncMock(
            return_value=types.UpdateShortSentMessage(
                id=99,
                pts=2,
                pts_count=1,
                date=datetime.datetime.now(datetime.timezone.utc),
                out=True,
            )
        )
        await service.send(account, intent, client)
        request = client.call_args.args[0]
        assert request.message == literal
        assert isinstance(request.entities[0], types.MessageEntityCode)
        client.upload_file.assert_not_called()

    run(scenario)


@pytest.mark.parametrize("worker", ["history", "catalog"])
def test_worker_retry_deadline_survives_restart_without_blocking_the_account(
    telegram_runtime, worker
):
    api, db, run = telegram_runtime

    async def persist(service, account, peer):
        await service.defer_worker(account, worker, 30)
        state = await service.state(account)
        assert state[worker + "_retry_at"] > datetime.datetime.now(
            datetime.timezone.utc
        )
        # Account selection remains eligible while this one worker is limited.
        assert any(
            row["uuid"] == account["uuid"]
            for row in await service.db.sql(telegram.ACCOUNTS_SQL)
        )

    run(persist)

    async def resume(service, account, peer):
        waited = []

        async def sleep(seconds):
            waited.append(seconds)
            await service.db.sql(
                f"UPDATE telegram_account_state SET {worker}_retry_at=now()-interval '1 second' WHERE external_account_uuid=%s",
                (account["uuid"],),
            )

        # A new service reads the durable deadline before another provider RPC.
        with mock.patch.object(telegram.asyncio, "sleep", side_effect=sleep):
            await service.wait_retry(account, worker)
            await service.wait_retry(
                account, "catalog" if worker == "history" else "history"
            )
        assert len(waited) == 1 and 25 < waited[0] <= 30

    run(resume)


def test_read_only_telegram_chat_rejects_send_before_canonical_message_or_intent(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def import_channel(service, account, peer):
        await service.db.sql(
            "UPDATE telegram_peer_state SET can_send=false WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        peer = (await service.selected(account))[0]
        await service.merge_batch(account, peer, [message()])
        return (await service.selected(account))[0]["stream_uuid"], account["uuid"]

    stream_uuid, account_uuid = run(import_channel)
    stream = api.get(f"/v1/messenger/streams/{stream_uuid}").json()
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": str(stream_uuid),
            "payload": {"kind": "markdown", "content": "Must not be queued"},
        },
    )
    assert response.status_code == 403
    assert response.json()["type"] == "StreamReadOnlyError"
    assert stream["can_send"] is False
    assert (
        db.execute(
            "SELECT count(*) FROM telegram_outgoing_intents WHERE external_account_uuid=%s",
            (account_uuid,),
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "mode,muted", [("all_messages", False), ("mentions_only", True), ("muted", True)]
)
def test_stream_notification_change_reaches_telegram_and_preserves_local_mode_on_echo(
    telegram_runtime, mode, muted
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        return (await service.selected(account))[0]["stream_uuid"], account["uuid"]

    stream_uuid, account_uuid = run(seed)
    # Make every parameter a real change, including all_messages.
    db.execute(
        "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE stream_uuid=%s",
        ("muted" if mode == "all_messages" else "all_messages", stream_uuid),
    )
    response = api.post(
        f"/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": mode},
    )
    assert response.status_code == 200

    async def deliver(service, account, peer):
        intents = await service.db.sql(
            "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='notifications'",
            (account["uuid"],),
        )
        assert len(intents) == 1

        async def notification_request(request):
            if isinstance(request, functions.account.GetNotifySettingsRequest):
                return types.PeerNotifySettings()
            return True

        client = mock.AsyncMock(side_effect=notification_request)
        await service.send(account, intents[0], client)
        request = client.await_args.args[0]
        assert isinstance(request, functions.account.UpdateNotifySettingsRequest)
        assert isinstance(request.peer, types.InputNotifyPeer)
        assert request.peer.peer.user_id == 8
        assert (request.settings.mute_until != 0) is muted
        updated = (await service.selected(account))[0]
        await service.notify_remote(account, updated, muted)
        assert (
            await service.db.sql(
                "SELECT status FROM telegram_outgoing_intents WHERE id=%s",
                (intents[0]["id"],),
            )
        )[0]["status"] == "done"

    run(deliver)
    assert (
        api.get(f"/v1/messenger/streams/{stream_uuid}").json()["notification_mode"]
        == mode
    )


def test_a_shorter_history_flood_wait_cannot_shorten_an_existing_shared_deadline(
    telegram_runtime,
):
    _api, _db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.defer_worker(account, "history", 60)
        original = (await service.state(account))["history_retry_at"]
        await service.defer_worker(account, "history", 2)
        assert (await service.state(account))["history_retry_at"] >= original

    run(scenario)


def test_notification_source_changes_inherit_defaults_and_preserve_pending_local_intent(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        peer = (await service.selected(account))[0]
        await service.db.sql(
            "UPDATE telegram_account_state SET notification_defaults='{"
            + '"users":true'
            + "}' WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.notify_settings(
            account,
            peer,
            types.PeerNotifySettings(),
            datetime.datetime.now(datetime.timezone.utc),
        )
        return peer["stream_uuid"]

    stream_uuid = run(seed)
    assert (
        api.get(f"/v1/messenger/streams/{stream_uuid}").json()["notification_mode"]
        == "muted"
    )
    assert (
        api.post(
            f"/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke",
            json={"notification_mode": "mentions_only"},
        ).status_code
        == 200
    )

    async def pending(service, account, peer):
        await service.notify_remote(account, peer, False)
        return (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='notifications' ORDER BY id",
                (account["uuid"],),
            )
        )[0]

    intent = run(pending)
    assert (
        api.get(f"/v1/messenger/streams/{stream_uuid}").json()["notification_mode"]
        == "mentions_only"
    )

    async def delivered(service, account, peer):
        stale = datetime.datetime.now(datetime.timezone.utc)
        client = mock.AsyncMock(
            side_effect=lambda request: (
                types.PeerNotifySettings()
                if isinstance(request, functions.account.GetNotifySettingsRequest)
                else True
            )
        )
        await service.send(account, intent, client)
        await service.notify_remote(account, peer, False, observed_at=stale)
        assert (await service.selected(account))[0]["notification_muted"] is True

    run(delivered)
    assert (
        api.get(f"/v1/messenger/streams/{stream_uuid}").json()["notification_mode"]
        == "mentions_only"
    )

    async def changed_remotely(service, account, peer):
        await service.notify_remote(account, peer, False)

    run(changed_remotely)
    assert (
        api.get(f"/v1/messenger/streams/{stream_uuid}").json()["notification_mode"]
        == "all_messages"
    )


def test_ptsless_global_notification_update_only_changes_inheriting_chats(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        now = datetime.datetime.now(datetime.timezone.utc)
        queue = python_types.SimpleNamespace(
            get=mock.AsyncMock(
                side_effect=[
                    (
                        types.UpdateNotifySettings(
                            types.NotifyUsers(),
                            types.PeerNotifySettings(
                                mute_until=now + datetime.timedelta(days=1)
                            ),
                        ),
                        now,
                    ),
                    asyncio.CancelledError(),
                ]
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await service.notification_loop(account, queue)
        selected = (await service.selected(account))[0]
        assert selected["notification_inherited"] and selected["notification_muted"]
        # An explicit unmute in this chat must survive a later global mute update.
        await service.notify_remote(account, selected, False)
        later = datetime.datetime.now(datetime.timezone.utc)
        queue.get = mock.AsyncMock(
            side_effect=[
                (
                    types.UpdateNotifySettings(
                        types.NotifyUsers(),
                        types.PeerNotifySettings(
                            mute_until=later + datetime.timedelta(days=2)
                        ),
                    ),
                    later,
                ),
                asyncio.CancelledError(),
            ]
        )
        with pytest.raises(asyncio.CancelledError):
            await service.notification_loop(account, queue)
        selected = (await service.selected(account))[0]
        assert (
            not selected["notification_inherited"]
            and not selected["notification_muted"]
        )

    run(scenario)


def test_imported_person_has_the_same_provider_identity_mapping_as_zulip_users(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        async with service.db.transaction() as connection:
            user_uuid = await service.operations.person(
                connection,
                account,
                types.User(id=8, first_name="Remote", username="remote_user"),
            )
        mapping = await service.db.sql(
            "SELECT * FROM m_external_accounts_v2 WHERE project_id=%s AND provider='telegram' AND provider_uuid=%s AND external_account_id='8'",
            (account["project_id"], account["provider_uuid"]),
        )
        assert len(mapping) == 1 and mapping[0]["owner_user_uuid"] == user_uuid
        assert mapping[0]["settings"] == {}
        assert not await service.state(
            mapping[0]
        )  # Imported identities cannot start a session.

    run(scenario)


@pytest.mark.parametrize("legacy_alias", [False, True])
def test_mention_routing_uses_telegram_identity_not_the_same_users_zulip_id(
    telegram_runtime,
    legacy_alias,
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        incoming = types.Message(
            id=41,
            peer_id=types.PeerUser(8),
            from_id=types.PeerUser(8),
            date=now,
            message="😀 Remote",
            entities=[types.MessageEntityMentionName(3, 6, 8)],
        )
        prepared, *_ = await service.prepare(
            account, peer, [incoming], mock.AsyncMock()
        )
        await service.merge_batch(account, peer, prepared)
        person = uuid.uuid5(account["provider_uuid"], "user:8")
        assert prepared[0]["text"] == f"😀 [Remote](urn:user:{person})"
        other_provider = uuid.uuid4()
        await service.db.sql(
            "INSERT INTO messenger_providers(uuid,hostname) VALUES(%s,%s)",
            (other_provider, f"synthetic-{other_provider}.invalid"),
        )
        await service.db.sql(
            "INSERT INTO m_external_accounts_v2(uuid,project_id,provider,provider_uuid,external_account_id,owner_user_uuid,status,settings) VALUES(%s,%s,'zulip',%s,'9999',%s,'active','{}')",
            (uuid.uuid4(), account["project_id"], other_provider, person),
        )
        if legacy_alias:
            linked = uuid.uuid4()
            await service.db.sql(
                "INSERT INTO m_workspace_users(uuid,username,source,status,avatar,created_at,updated_at) VALUES(%s,%s,'iam','offline','urn:gravatar:00000000000000000000000000000000',now(),now())",
                (linked, f"linked-{linked}"),
            )
            await service.db.sql(
                "UPDATE m_external_accounts_v2 SET owner_user_uuid=%s WHERE project_id=%s AND owner_user_uuid=%s",
                (linked, account["project_id"], person),
            )
        return (await service.selected(account))[0]["stream_uuid"], person

    stream, person = run(seed)
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": str(stream),
            "payload": {
                "kind": "markdown",
                "content": f"😀 [Remote](urn:user:{person})",
            },
        },
    )
    assert response.status_code == 201

    async def deliver(service, account, peer):
        intent = (
            await service.db.sql(
                "SELECT * FROM telegram_outgoing_intents WHERE external_account_uuid=%s AND entity_type='message'",
                (account["uuid"],),
            )
        )[0]
        client = mock.AsyncMock(
            return_value=types.UpdateShortSentMessage(
                id=99,
                pts=2,
                pts_count=1,
                date=datetime.datetime.now(datetime.timezone.utc),
                out=True,
            )
        )
        client.get_input_entity.return_value = types.InputPeerUser(8, 77)
        await service.send(account, intent, client)
        client.get_input_entity.assert_awaited_once_with(types.PeerUser(8))
        request = client.await_args.args[0]
        assert request.message == "😀 Remote"
        mention = request.entities[0]
        assert isinstance(mention, types.InputMessageEntityMentionName)
        assert (mention.offset, mention.length, mention.user_id.user_id) == (3, 6, 8)

    run(deliver)


@pytest.mark.parametrize("state", ["disabled", "expired"])
def test_unavailable_telegram_connection_rejects_new_messages_without_losing_them(
    telegram_runtime, state
):
    api, db, run = telegram_runtime

    async def seed(service, account, peer):
        await service.merge_batch(account, peer, [message()])
        return account["uuid"], (await service.selected(account))[0]["stream_uuid"]

    account_uuid, stream_uuid = run(seed)
    assert api.get(f"/v1/messenger/streams/{stream_uuid}").json()["can_send"]
    queued_before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE event_kind='delivery_snapshot_event' AND payload->>'resource_uuid'=%s",
        (str(stream_uuid),),
    ).fetchone()[0]
    if state == "disabled":
        db.execute(
            "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
            (account_uuid,),
        )
    else:
        db.execute(
            "UPDATE telegram_account_state SET auth_state='expired' WHERE external_account_uuid=%s",
            (account_uuid,),
        )
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": str(stream_uuid),
            "payload": {"kind": "markdown", "content": "Unavailable must be explicit"},
        },
    )
    assert response.status_code == 403
    assert not api.get(f"/v1/messenger/streams/{stream_uuid}").json()["can_send"]
    assert (
        db.execute(
            "SELECT count(*) FROM telegram_outgoing_intents WHERE external_account_uuid=%s",
            (account_uuid,),
        ).fetchone()[0]
        == 0
    )

    queued_after = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE event_kind='delivery_snapshot_event' AND payload->>'resource_uuid'=%s",
        (str(stream_uuid),),
    ).fetchone()[0]
    assert (
        queued_after > queued_before
    )  # The open UI must learn about the changed capability.


def test_incoming_media_does_not_hold_following_text_and_survives_replay_restart(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        media = types.Message(
            id=51,
            peer_id=types.PeerUser(8),
            from_id=types.PeerUser(8),
            date=now,
            message="File",
            media=types.MessageMediaPhoto(photo=types.PhotoEmpty(123)),
        )
        text = types.Message(
            id=52,
            peer_id=types.PeerUser(8),
            from_id=types.PeerUser(8),
            date=now,
            message="Fast text",
        )
        response = types.updates.Difference(
            new_messages=[media, text],
            new_encrypted_messages=[],
            other_updates=[],
            chats=[],
            users=[],
            state=types.updates.State(pts=3, qts=0, date=now, seq=0, unread_count=0),
        )
        client = mock.AsyncMock(return_value=response)
        blocked = asyncio.Event()

        async def files(account, peer, message, data, client):
            data["files"] = []
            if message.media:
                blocked.set()
                await asyncio.Future()
            return 0, 0

        service.prepare_files = files
        await asyncio.wait_for(service.receive_common(account, [peer], client), 0.3)
        stored = await service.db.sql(
            "SELECT count(*) AS count FROM messenger_messages WHERE project_id=%s",
            (account["project_id"],),
        )
        assert stored[0]["count"] == 1
        assert (await service.state(account))["pts"] == 3
        pending = await service.db.sql(
            "SELECT id FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NULL",
            (account["uuid"],),
        )
        assert len(pending) == 1
        client.get_messages.return_value = [media]
        replay = asyncio.create_task(service.replay_pending(account, peer, client))
        try:
            await asyncio.wait_for(blocked.wait(), 0.3)
            response.new_messages = [
                types.Message(
                    id=53,
                    peer_id=types.PeerUser(8),
                    date=now,
                    message="Still fast",
                    from_id=types.PeerUser(8),
                )
            ]
            response.state.pts = 4
            await asyncio.wait_for(service.receive_common(account, [peer], client), 0.3)
        finally:
            replay.cancel()
            await asyncio.gather(replay, return_exceptions=True)

    run(scenario)

    async def recovered(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        media = types.Message(
            id=51,
            peer_id=types.PeerUser(8),
            from_id=types.PeerUser(8),
            date=now,
            message="File",
        )
        client = mock.AsyncMock()
        client.get_messages.return_value = [media]
        await service.replay_pending(account, peer, client)
        assert not await service.db.sql(
            "SELECT id FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NULL",
            (account["uuid"],),
        )

    run(recovered)
    rows = api.get("/v1/messenger/messages/").json()
    assert {row["payload"]["content"] for row in rows} == {
        "File",
        "Fast text",
        "Still fast",
    }


@pytest.mark.parametrize(
    "kind,role,expected",
    [
        ("chat", "member", False),
        ("chat", "administrator", True),
        ("channel", "member", False),
        ("channel", "administrator", True),
        ("channel", "individual_other_restriction", False),
        ("broadcast", "member", False),
        ("broadcast", "administrator", False),
        ("broadcast", "poster", True),
        ("broadcast", "creator", True),
    ],
)
def test_catalog_posting_capabilities_follow_default_and_individual_telegram_rights(
    telegram_runtime, kind, role, expected
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        now = datetime.datetime.now(datetime.timezone.utc)
        banned = types.ChatBannedRights(
            until_date=now + datetime.timedelta(days=1), send_messages=True
        )
        if kind == "chat":
            entity = types.Chat(
                id=9,
                title="Restricted group",
                photo=types.ChatPhotoEmpty(),
                participants_count=2,
                date=now,
                version=1,
                default_banned_rights=banned,
                admin_rights=types.ChatAdminRights()
                if role == "administrator"
                else None,
            )
            input_peer = types.InputPeerChat(9)
        else:
            entity = types.Channel(
                id=9,
                title="Channel",
                photo=types.ChatPhotoEmpty(),
                date=now,
                broadcast=kind == "broadcast",
                megagroup=kind != "broadcast",
                default_banned_rights=banned,
                admin_rights=types.ChatAdminRights(post_messages=role == "poster")
                if role in ("administrator", "poster")
                else None,
                creator=role == "creator",
                banned_rights=types.ChatBannedRights(
                    until_date=now + datetime.timedelta(days=1), send_polls=True
                )
                if role == "individual_other_restriction"
                else None,
            )
            input_peer = types.InputPeerChannel(9, 77)

        async def dialogs():
            yield python_types.SimpleNamespace(
                entity=entity,
                input_entity=input_peer,
                title=entity.title,
                dialog=python_types.SimpleNamespace(pts=1, read_inbox_max_id=0),
            )

        await service.catalog(account, mock.Mock(iter_dialogs=dialogs))
        selected = await service.selected(account)
        assert len(selected) == 1 and selected[0]["can_send"] is expected

    run(scenario)
