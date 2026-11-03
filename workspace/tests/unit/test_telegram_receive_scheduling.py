"""A large channel catalog must not hold direct-message updates behind its scan."""

import asyncio
import datetime
import types
import uuid
from unittest import mock

import pytest

from workspace.services import telegram


def account_service(monkeypatch):
    service = telegram.TelegramService("unused", 12345, "synthetic", "synthetic")
    service.db.connect = mock.AsyncMock(
        return_value=types.SimpleNamespace(close=mock.AsyncMock())
    )
    service.db.sql = mock.AsyncMock(return_value=[])
    monkeypatch.setattr(
        telegram.messenger, "rows", mock.AsyncMock(return_value=[{"acquired": True}])
    )
    service.state = mock.AsyncMock(
        return_value={
            "auth_state": "authorized",
            "disconnect_requested": False,
            "session_envelope": None,
        }
    )
    client = mock.AsyncMock()
    client.is_user_authorized.return_value = True
    client.add_event_handler = mock.Mock()
    service.client_factory = mock.Mock(return_value=client)
    service.initialize = mock.AsyncMock()
    service.selected = mock.AsyncMock(return_value=[])
    service.contacts = mock.AsyncMock()
    service.catalog = mock.AsyncMock()
    service.notification_defaults = mock.AsyncMock()
    return service


def test_startup_catalog_does_not_hold_realtime_receive_and_send(monkeypatch):
    async def run():
        service = account_service(monkeypatch)
        catalog_started = asyncio.Event()
        receiving = asyncio.Event()
        sending = asyncio.Event()

        async def blocked_catalog(*args):
            catalog_started.set()
            await asyncio.Future()

        async def receive(*args):
            receiving.set()
            await asyncio.Future()

        async def send(*args):
            sending.set()
            await asyncio.Future()

        async def idle(*args):
            await asyncio.Future()

        service.catalog = blocked_catalog
        service.receive_loop = receive
        service.outgoing_loop = send
        task = asyncio.create_task(
            service.account({"uuid": uuid.uuid4(), "project_id": uuid.uuid4()})
        )
        try:
            await asyncio.wait_for(catalog_started.wait(), 0.5)
            await asyncio.wait_for(receiving.wait(), 0.1)
            await asyncio.wait_for(sending.wait(), 0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_catalog_flood_wait_keeps_realtime_account_jobs_running(monkeypatch):
    async def run():
        service = account_service(monkeypatch)
        limited = asyncio.Event()
        receiving = asyncio.Event()

        async def catalog(*args):
            limited.set()
            raise telegram.errors.FloodWaitError(None, capture=30)

        async def wait_retry(account, worker):
            if limited.is_set():
                await asyncio.Future()

        async def receive(*args):
            await limited.wait()
            await asyncio.sleep(0.01)
            receiving.set()
            await asyncio.Future()

        async def idle(*args):
            await asyncio.Future()

        service.catalog = catalog
        service.notification_defaults = mock.AsyncMock()
        service.wait_retry = wait_retry
        service.receive_loop = receive
        service.outgoing_loop = idle
        task = asyncio.create_task(service.account({"uuid": uuid.uuid4()}))
        try:
            await asyncio.wait_for(receiving.wait(), 0.2)
            queries = [call.args[0] for call in service.db.sql.await_args_list]
            assert any("SET catalog_retry_at=" in query for query in queries)
            assert not any("SET next_attempt_at=" in query for query in queries)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_dirty_channel_is_prioritized_during_a_long_channel_scan():
    completed = []
    dirty = set()
    peers = [
        {"peer_kind": "channel", "peer_key": f"channel:{i}", "receive_retry_at": None}
        for i in range(20)
    ]
    service = telegram.TelegramService("unused", 12345, "synthetic", "synthetic")
    service.selected = mock.AsyncMock(return_value=peers)

    async def channel(account, peer, client):
        completed.append(peer["peer_key"])
        if len(completed) == 1:
            dirty.add("channel:19")
        if len(completed) == 6:
            raise asyncio.CancelledError()

    service.receive_channel = channel

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await service.channel_loop({}, None, asyncio.Event(), dirty)

    asyncio.run(run())
    assert completed[:3] == ["channel:0", "channel:19", "channel:1"]


@pytest.mark.parametrize("remote,expected_wake", [(8, False), (9, True)])
def test_common_difference_wakes_catalog_only_for_unknown_dialog(remote, expected_wake):
    async def run():
        service = telegram.TelegramService("unused", 12345, "synthetic", "synthetic")
        now = datetime.datetime.now(datetime.timezone.utc)
        service.state = mock.AsyncMock(
            return_value={"pts": 1, "qts": 0, "cursor_date": now}
        )
        service.db.sql = mock.AsyncMock(return_value=[])
        service.receipts = mock.AsyncMock()
        service.prepare = mock.AsyncMock(return_value=([], 0, 0, 0))
        service.merge_batch = mock.AsyncMock()
        service.apply_updates = mock.AsyncMock()
        response = types.SimpleNamespace(
            new_messages=[
                telegram.types.Message(
                    id=1,
                    peer_id=telegram.types.PeerUser(remote),
                    date=now,
                    message="new dialog",
                )
            ],
            other_updates=[],
            users=[],
            chats=[],
            state=types.SimpleNamespace(pts=2, qts=0, date=now),
        )
        client = mock.AsyncMock(return_value=response)
        wake = asyncio.Event()
        await service.receive_common(
            {"uuid": uuid.uuid4()}, [{"peer_key": "user:8"}], client, wake
        )
        assert wake.is_set() is expected_wake
        # Discovery does not prevent durable progress; initial history recovers the new dialog.
        assert service.db.sql.await_args.args[1][0] == 2

    asyncio.run(run())


def test_account_refreshes_catalog_when_a_new_dialog_message_arrives(monkeypatch):
    async def run():
        now = datetime.datetime.now(datetime.timezone.utc)
        response = types.SimpleNamespace(
            new_messages=[
                telegram.types.Message(
                    id=1,
                    peer_id=telegram.types.PeerUser(9),
                    date=now,
                    message="new dialog",
                )
            ],
            other_updates=[],
            users=[],
            chats=[],
            state=types.SimpleNamespace(pts=2, qts=0, date=now),
        )
        client = mock.AsyncMock(return_value=response)
        client.is_user_authorized.return_value = True
        client.add_event_handler = mock.Mock()
        service = telegram.TelegramService(
            "unused",
            12345,
            "synthetic",
            "synthetic",
            client_factory=mock.Mock(return_value=client),
        )
        lock = types.SimpleNamespace(close=mock.AsyncMock())
        service.db.connect = mock.AsyncMock(return_value=lock)
        service.db.sql = mock.AsyncMock(return_value=[])
        monkeypatch.setattr(
            telegram.messenger,
            "rows",
            mock.AsyncMock(return_value=[{"acquired": True}]),
        )
        service.state = mock.AsyncMock(
            return_value={
                "auth_state": "authorized",
                "disconnect_requested": False,
                "session_envelope": None,
                "pts": 1,
                "qts": 0,
                "cursor_date": now,
            }
        )
        service.initialize = mock.AsyncMock()
        service.contacts = mock.AsyncMock()
        service.selected = mock.AsyncMock(
            return_value=[{"peer_key": "user:8", "peer_kind": "user"}]
        )
        service.replay_pending = mock.AsyncMock()
        service.receipts = mock.AsyncMock()
        service.apply_updates = mock.AsyncMock()
        calls = 0

        async def catalog(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise asyncio.CancelledError()

        async def idle(*args):
            await asyncio.Future()

        service.catalog = catalog
        service.outgoing_loop = idle
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(service.account({"uuid": uuid.uuid4()}), 0.5)
        assert calls == 2
        client.disconnect.assert_awaited_once()

    asyncio.run(run())


def test_new_dialog_wakes_the_existing_catalog_job_without_waiting_a_minute():
    async def run():
        service = telegram.TelegramService("unused", 12345, "synthetic", "synthetic")
        service.selected = mock.AsyncMock()
        service.db.sql = mock.AsyncMock(return_value=[])
        service.contacts = mock.AsyncMock()
        service.notification_defaults = mock.AsyncMock()
        service.catalog = mock.AsyncMock(side_effect=asyncio.CancelledError())
        wake = asyncio.Event()
        wake.set()
        with pytest.raises(asyncio.CancelledError):
            account = {"uuid": uuid.uuid4()}
            await asyncio.wait_for(service.catalog_loop(account, None, wake), 0.5)
        service.catalog.assert_awaited_once_with(account, None)

    asyncio.run(run())


def test_slow_channel_rpc_does_not_hold_direct_realtime(monkeypatch):
    async def run():
        service = account_service(monkeypatch)
        service.poll_interval = 0.01
        service.selected = mock.AsyncMock(
            return_value=[
                {
                    "peer_kind": "channel",
                    "peer_key": "channel:8",
                    "receive_retry_at": None,
                }
            ]
        )
        blocked = asyncio.Event()
        common_after_block = asyncio.Event()

        async def common(*args):
            if blocked.is_set():
                common_after_block.set()

        async def channel(*args):
            blocked.set()
            await asyncio.Future()

        async def idle(*args):
            await asyncio.Future()

        service.receive_common = common
        service.receive_channel = channel
        service.replay_pending = mock.AsyncMock()
        service.outgoing_loop = idle
        service.catalog_loop = idle
        task = asyncio.create_task(service.account({"uuid": uuid.uuid4()}))
        try:
            await asyncio.wait_for(blocked.wait(), 0.2)
            await asyncio.wait_for(common_after_block.wait(), 0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["all_messages", "mentions_only", "muted"])
def test_muting_preserves_source_preview_sound_and_story_preferences(mode):
    async def run():
        service = telegram.TelegramService("unused", 12345, "synthetic", "synthetic")
        service.db.sql = mock.AsyncMock(return_value=[{"peer_key": "user:8"}])
        service.input_peer = mock.Mock(return_value=telegram.types.InputPeerUser(8, 77))
        service.finish_send = mock.AsyncMock()
        sound = telegram.types.NotificationSoundLocal("Synthetic", "synthetic.ogg")
        original = telegram.types.PeerNotifySettings(
            show_previews=False,
            silent=True,
            other_sound=sound,
            stories_muted=True,
            stories_hide_sender=True,
            stories_other_sound=sound,
        )

        async def rpc(request):
            if isinstance(request, telegram.functions.account.GetNotifySettingsRequest):
                return original
            return True

        client = mock.AsyncMock(side_effect=rpc)
        await service.send(
            {"uuid": uuid.uuid4()},
            {
                "entity_type": "notifications",
                "peer_key": "user:8",
                "data": {"notification_mode": mode},
            },
            client,
        )
        request = client.await_args.args[0]
        assert isinstance(
            request, telegram.functions.account.UpdateNotifySettingsRequest
        )
        assert request.settings.show_previews is False
        assert request.settings.silent is True
        assert request.settings.sound is sound
        assert request.settings.stories_muted is True
        assert request.settings.stories_hide_sender is True
        assert request.settings.stories_sound is sound
        assert (request.settings.mute_until != 0) is (mode != "all_messages")
        service.finish_send.assert_awaited_once()

    asyncio.run(run())
