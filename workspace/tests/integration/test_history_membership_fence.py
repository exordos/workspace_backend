# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""An old prepared page cannot regrant a revoked membership generation."""

import asyncio

import pytest

from workspace.bridges.zulip import outgoing_operations
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("personal_only", [False, True])
def test_prepared_page_cannot_restore_old_generation_access(
    incoming, reset, personal_only
):
    api, db, account, *_ = incoming
    history_tests.incoming_tests.apply(
        incoming, history_tests.incoming_tests.event(identifier=995)
    )
    history_tests.request(db, account)

    async def run():
        messages = {995: history_tests.message(995, flags=["starred"])}
        async with history_tests.worker(incoming, messages) as (service, _):
            current = await history_tests.job(service, account)
            batch = await (
                service.operations.prepare_flags(current, list(messages.values()))
                if personal_only
                else service.operations.prepare_batch(current, list(messages.values()))
            )
            db.execute(
                "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1,active=%s WHERE project_id=%s AND user_uuid=%s",
                (reset, api.project_id, account["owner_user_uuid"]),
            )
            with pytest.raises(outgoing_operations.Pending, match="Membership changed"):
                async with service.db.transaction() as connection:
                    if personal_only:
                        await service.operations.merge_personal_flags(connection, batch)
                    else:
                        await service.operations.merge_batch(connection, current, batch)
            for table in (
                "messenger_user_message_bindings",
                "messenger_user_message_unreads",
                "messenger_user_message_stars",
            ):
                assert db.execute(
                    f"SELECT count(*) FROM {table} WHERE project_id=%s AND user_uuid=%s",
                    (api.project_id, account["owner_user_uuid"]),
                ).fetchone() == (0,)

    asyncio.run(run())
