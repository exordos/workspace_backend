# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import datetime

import pytest
from psycopg import rows

from workspace.bridges.zulip import history
from workspace.services import zulip_history, zulip_outgoing
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


def test_history_flag_page_keeps_read_messages_without_any_flag_rows(incoming):
    _api, db, account, _http, _url = incoming
    message = incoming_tests.event()
    message["message"]["flags"] = ["read"]
    incoming_tests.apply(incoming, message)
    with db.cursor(row_factory=rows.dict_row) as cursor:
        result = cursor.execute(
            history.SELECT_FLAG_PAGE_SQL,
            (
                account["project_id"],
                account["owner_user_uuid"],
                account["provider_uuid"],
                0,
                100,
            ),
        ).fetchall()
    assert result == [{"remote_id": 101, "read": True, "starred": False}]


def test_history_absence_deletes_access_and_preserves_personal_flags(incoming):
    api, db, account, http, url = incoming
    message = incoming_tests.event()
    message["message"]["flags"] = ["starred"]
    incoming_tests.apply(incoming, message)
    placement = api.get("/v1/messenger/messages/").json()[0]["uuid"]

    async def remove_access():
        database = zulip_outgoing.Database(url)
        await database.open()
        try:
            operations = history.Operations(database, http)
            async with database.transaction() as connection:
                await connection.execute(
                    "SELECT set_config('workspace.zulip_incoming','on',true)"
                )
                await operations.remove_access(connection, account, 101)
        finally:
            await database.close()

    asyncio.run(remove_access())
    key = (account["project_id"], account["owner_user_uuid"], placement)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        key,
    ).fetchone() == (0,)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_stars "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        key,
    ).fetchone() == (1,)
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_unreads "
        "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        key,
    ).fetchone() == (1,)
    assert api.get(f"/v1/messenger/messages/{placement}").status_code == 404


@pytest.mark.parametrize("completed_after_fetch", [False, True])
def test_history_refreshes_after_a_completed_outgoing_flag_change(
    incoming, completed_after_fetch
):
    _api, db, account, http, url = incoming
    message = incoming_tests.event()
    incoming_tests.apply(incoming, message)
    since = datetime.datetime.now(datetime.timezone.utc)
    binding_uuid = db.execute(
        "SELECT uuid FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s",
        (account["project_id"], account["owner_user_uuid"]),
    ).fetchone()[0]
    db.execute(
        """INSERT INTO zulip_outgoing_intents(
             project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,
             operation,data,status,created_at,completed_at
           ) VALUES(%s,'message_state',%s,%s,%s,'update','{}','done',%s,%s)""",
        (
            account["project_id"],
            binding_uuid,
            f"message_state:{binding_uuid}",
            account["uuid"],
            since - datetime.timedelta(days=1),
            since + datetime.timedelta(seconds=1 if completed_after_fetch else -1),
        ),
    )

    async def activity():
        service = zulip_history.ZulipHistoryService(url)
        await service.db.open()
        service.operations = history.Operations(service.db, http)
        try:
            snapshots = await service.operations.snapshots(account, [101])
            async with service.db.transaction() as connection:
                return await service.activity(
                    connection, account, message, snapshots["101"], since
                )
        finally:
            await service.db.close()

    result = asyncio.run(activity())
    assert result == {"pending": False, "changed": completed_after_fetch}
