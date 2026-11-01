import asyncio
import datetime
import uuid

from workspace.services import telegram
from workspace.tests.integration import conftest


def test_history_batch_is_visible_idempotent_and_advances_checkpoint_atomically(
    telegram_account, db, _test_database_url
):
    api, root = telegram_account
    identifier = uuid.UUID(root.rsplit("/", 1)[1])
    conftest.seed_workspace_user(db, uuid.UUID(api.user_uuid), "telegram-owner")
    db.execute(
        "SELECT messenger_v2_register_project_user(%s,%s)",
        (uuid.UUID(api.project_id), uuid.UUID(api.user_uuid)),
    )
    db.execute(
        "UPDATE m_external_accounts_v2 SET external_account_id='7',status='syncing' WHERE uuid=%s",
        (identifier,),
    )
    db.execute(
        "INSERT INTO telegram_account_state(external_account_uuid,auth_state,pts,qts,cursor_date) VALUES(%s,'authorized',1,0,now())",
        (identifier,),
    )
    db.execute(
        "INSERT INTO telegram_peer_state(external_account_uuid,peer_key,title,input_peer_envelope,peer_kind,selected) VALUES(%s,'user:8','Telegram test','{}','user',true)",
        (identifier,),
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    data = {
        "id": 41,
        "identity": "account:7:user:8:message:41",
        "peer": "user:8",
        "topic_id": 0,
        "sender_id": 8,
        "sender_name": "Other user",
        "text": "Imported **history**",
        "date": now.isoformat(),
        "edited_at": now.isoformat(),
        "out": False,
        "reply_to": None,
        "files": [],
        "reactions": {},
        "reaction_actors": [],
    }

    async def run():
        service = telegram.TelegramService(
            _test_database_url, 12345, "synthetic", "synthetic"
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
            first = await service.merge_batch(account, peer, [data])
            repeated = await service.merge_batch(account, peer, [data])
            assert first == 1
            assert repeated == 0
        finally:
            await service.db.close()

    asyncio.run(run())
    response = api.get("/v1/messenger/messages/")
    assert response.status_code == 200, response.text
    messages = response.json()
    assert len(messages) == 1
    assert messages[0]["payload"]["content"] == data["text"]
    assert messages[0]["source_name"] == "telegram"
    assert (
        db.execute(
            "SELECT count(*) FROM telegram_incoming_events WHERE external_account_uuid=%s AND processed_at IS NOT NULL",
            (identifier,),
        ).fetchone()[0]
        == 1
    )

    topics = api.get("/v1/messenger/stream_topics/").json()
    assert topics[0]["name"] == "telegram"
