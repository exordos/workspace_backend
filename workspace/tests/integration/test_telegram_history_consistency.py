"""History snapshots cannot displace live data or starve behind deferred work."""

import datetime
import time
import types as python_types
import uuid

import pytest
from telethon.tl import types

from workspace.bridges import history
from workspace.bridges import messenger
from workspace.tests.integration import test_telegram_runtime as runtime

telegram_runtime = runtime.telegram_runtime


def test_history_rpc_snapshot_cannot_overwrite_a_live_edit_received_during_fetch(
    telegram_runtime,
):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        timestamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

        async def client(request):
            await service.merge_batch(
                account,
                peer,
                [
                    runtime.message(
                        text="Live",
                        date=timestamp.isoformat(),
                        edited_at=timestamp.isoformat(),
                    )
                ],
            )
            return python_types.SimpleNamespace(
                messages=[
                    types.Message(
                        id=41,
                        peer_id=types.PeerUser(8),
                        from_id=types.PeerUser(8),
                        date=timestamp,
                        edit_date=timestamp,
                        message="Snapshot",
                    )
                ],
                users=[],
                chats=[],
            )

        prepared, info = await service.history_page(account, peer, client)
        info["merge_started"] = time.monotonic()
        await service.merge_batch(account, peer, prepared, history=info)

    run(scenario)
    assert api.get("/v1/messenger/messages/").json()[0]["payload"]["content"] == "Live"


@pytest.mark.parametrize(
    "reason",
    [
        "new",
        "checking",
        "SEND_UNCERTAIN",
        "media",
        "unselected",
        "disabled",
        "unauthorized",
        "disconnect",
        "unknown_peer",
    ],
)
def test_only_runnable_telegram_replay_delays_shared_history(telegram_runtime, reason):
    api, db, run = telegram_runtime

    async def scenario(service, account, peer):
        await service.db.sql(
            "INSERT INTO telegram_incoming_events(external_account_uuid,event_key,payload,received_at) VALUES(%s,'deferred',%s,now()-interval '1 second')",
            (
                account["uuid"],
                messenger.json_value({"id": 41, "peer": peer["peer_key"]}),
            ),
        )
        if reason in ("new", "checking", "SEND_UNCERTAIN"):
            await service.db.sql(
                "INSERT INTO telegram_outgoing_intents(project_id,external_account_uuid,peer_key,entity_type,entity_uuid,operation,data,status,error_code) VALUES(%s,%s,%s,'message',%s,'create','{}',%s,%s)",
                (
                    account["project_id"],
                    account["uuid"],
                    peer["peer_key"],
                    uuid.uuid4(),
                    "error" if reason == "SEND_UNCERTAIN" else reason,
                    "SEND_UNCERTAIN" if reason == "SEND_UNCERTAIN" else None,
                ),
            )
        elif reason == "media":
            await service.db.sql(
                "UPDATE telegram_peer_state SET media_retry_at=now()+interval '1 hour' WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        elif reason == "unselected":
            await service.db.sql(
                "UPDATE telegram_peer_state SET selected=false WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        elif reason == "disabled":
            await service.db.sql(
                "UPDATE m_external_accounts_v2 SET disabled=true WHERE uuid=%s",
                (account["uuid"],),
            )
        elif reason == "unauthorized":
            await service.db.sql(
                "UPDATE telegram_account_state SET auth_state='expired' WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        elif reason == "disconnect":
            await service.db.sql(
                "UPDATE telegram_account_state SET disconnect_requested=true WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        else:
            await service.db.sql(
                "UPDATE telegram_incoming_events SET payload=jsonb_set(payload,'{peer}','\"user:999\"') WHERE external_account_uuid=%s",
                (account["uuid"],),
            )
        assert not await history.busy(service.db, account["project_id"], 10000, 0.1)

        # Deferred events are retained. Once the prerequisite clears, realtime
        # must take priority again without reinserting the event.
        await service.db.sql(
            "UPDATE telegram_outgoing_intents SET status='sent',error_code=NULL WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.db.sql(
            "UPDATE telegram_peer_state SET selected=true,media_retry_at=NULL WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.db.sql(
            "UPDATE m_external_accounts_v2 SET disabled=false WHERE uuid=%s",
            (account["uuid"],),
        )
        await service.db.sql(
            "UPDATE telegram_account_state SET auth_state='authorized',disconnect_requested=false WHERE external_account_uuid=%s",
            (account["uuid"],),
        )
        await service.db.sql(
            "UPDATE telegram_incoming_events SET payload=jsonb_set(payload,'{peer}',to_jsonb(%s::text)) WHERE external_account_uuid=%s",
            (peer["peer_key"], account["uuid"]),
        )
        assert await history.busy(service.db, account["project_id"], 10000, 0.1)

    run(scenario)
