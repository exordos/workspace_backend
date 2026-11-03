"""Telegram source revocation removes only the affected owner's access."""

import asyncio
import datetime
import types as python_types
import uuid
from unittest import mock

import pytest
from telethon import errors
from telethon.tl import types

from workspace.tests.integration import conftest
from workspace.tests.integration import test_telegram_runtime as runtime

telegram_runtime = runtime.telegram_runtime


@pytest.mark.parametrize("remove_creator", [False, True])
@pytest.mark.parametrize("source", ["missing", "left", "private"])
def test_missing_channel_revokes_only_its_owner_before_other_members_receive(
    telegram_runtime,
    remove_creator,
    source,
):
    api, db, run = telegram_runtime
    removed_owner = uuid.uuid4()
    other_account = uuid.uuid4()
    conftest.seed_workspace_user(db, removed_owner, f"removed-{removed_owner}")
    db.execute(
        "SELECT messenger_v2_register_project_user(%s,%s)",
        (api.project_id, removed_owner),
    )

    async def scenario(service, account, peer):
        await service.db.sql(
            "UPDATE telegram_peer_state SET peer_key='channel:123',peer_kind='channel' WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        peer = (await service.selected(account))[0]
        await service.db.sql(
            "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,provider_uuid,external_account_id,status,settings,created_at,updated_at) VALUES(%s,%s,%s,'telegram',%s,'9','active','{}',now(),now())",
            (
                other_account,
                account["project_id"],
                removed_owner,
                account["provider_uuid"],
            ),
        )
        await service.db.sql(
            "INSERT INTO telegram_account_state(external_account_uuid,auth_state) VALUES(%s,'authorized')",
            (other_account,),
        )
        await service.db.sql(
            "INSERT INTO telegram_peer_state(external_account_uuid,peer_key,title,input_peer_envelope,peer_kind,can_send,selected) SELECT %s,peer_key,title,input_peer_envelope,peer_kind,can_send,true FROM telegram_peer_state WHERE external_account_uuid=%s",
            (other_account, account["uuid"]),
        )
        linked = (
            await service.db.sql(
                "SELECT * FROM m_external_accounts_v2 WHERE uuid=%s", (other_account,)
            )
        )[0]
        linked_peer = (await service.selected(linked))[0]
        await service.merge_batch(
            account,
            peer,
            [
                runtime.message(
                    peer="channel:123", identity="channel:123:message:41", sender_id=7
                )
            ],
        )
        await service.merge_batch(linked, linked_peer, [])
        stream = (await service.selected(account))[0]["stream_uuid"]

        class EmptyCatalog:
            async def iter_dialogs(self):
                if False:
                    yield None

        gone = account if remove_creator else linked
        remaining = linked if remove_creator else account
        removed_user = gone["owner_user_uuid"]
        if source == "missing":
            await service.catalog(gone, EmptyCatalog())
        elif source == "left":

            class LeftCatalog:
                async def iter_dialogs(self):
                    yield python_types.SimpleNamespace(
                        entity=types.Channel(
                            id=123,
                            title="Shared channel",
                            photo=types.ChatPhotoEmpty(),
                            date=datetime.datetime.now(datetime.timezone.utc),
                            left=True,
                        ),
                        input_entity=types.InputPeerChannel(123, 77),
                        title="Shared channel",
                        dialog=python_types.SimpleNamespace(
                            pts=1, read_inbox_max_id=0, notify_settings=None
                        ),
                    )

            await service.catalog(gone, LeftCatalog())
        else:
            with mock.patch.object(
                service,
                "receive_channel",
                side_effect=errors.ChannelPrivateError(request=None),
            ):
                task = asyncio.create_task(
                    service.channel_loop(gone, mock.AsyncMock(), asyncio.Event(), set())
                )
                try:

                    async def revoked():
                        while (
                            await service.db.sql(
                                "SELECT active FROM messenger_stream_bindings WHERE stream_uuid=%s AND user_uuid=%s",
                                (stream, removed_user),
                            )
                        )[0]["active"]:
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(revoked(), 2)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        binding = (
            await service.db.sql(
                "SELECT active FROM messenger_stream_bindings WHERE stream_uuid=%s AND user_uuid=%s",
                (stream, removed_user),
            )
        )[0]
        assert binding["active"] is False
        # A newly linked account has not fetched the pre-existing history yet.
        previous = (
            await service.db.sql(
                "SELECT count(*) AS total FROM messenger_user_message_bindings binding JOIN messenger_message_placements placement ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid WHERE binding.project_id=%s AND binding.user_uuid=%s AND placement.stream_uuid=%s",
                (account["project_id"], remaining["owner_user_uuid"], stream),
            )
        )[0]["total"]
        await service.merge_batch(
            remaining,
            (await service.selected(remaining))[0],
            [
                runtime.message(
                    42,
                    peer="channel:123",
                    identity="channel:123:message:42",
                    sender_id=7,
                )
            ],
        )
        return str(stream), removed_user, remaining["owner_user_uuid"], previous + 1

    try:
        stream, removed_owner, survivor, expected = run(scenario)
        api._user_seeder = None
        assert (
            api.get(f"/v1/messenger/streams/{stream}", user=removed_owner).status_code
            == 404
        )
        assert len(api.get("/v1/messenger/messages/", user=survivor).json()) == expected
        assert not api.get("/v1/messenger/messages/", user=removed_owner).json()
    finally:
        db.execute("DELETE FROM m_external_accounts_v2 WHERE uuid=%s", (other_account,))
