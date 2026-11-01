# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import uuid
from unittest import mock

from workspace.bridges.zulip import history_blocks, incoming_operations
from workspace.tests.integration import test_zulip_history as history_tests

incoming = history_tests.incoming


def test_prepared_receipt_context_reads_bound_topics_and_preserves_receipt(
    incoming,
):
    _, db, account, *_ = incoming
    messages = {
        101: history_tests.message(101, "@**Synthetic|7** in General"),
        102: history_tests.message(
            102, "channel/42/topic/General", subject="✔ General"
        ),
        103: history_tests.message(
            103,
            "Direct General",
            type="private",
            display_recipient=[
                {"id": 7, "full_name": "Synthetic 7"},
                {"id": 8, "full_name": "Synthetic 8"},
            ],
        ),
    }
    history_tests.request(db, account)

    async def run():
        async with history_tests.worker(incoming, messages) as (service, _):
            await service.step(await history_tests.job(service, account))
            current = await history_tests.job(service, account)
            await service.operations.prepare_batch(current, list(messages.values()))
            batch = await service.operations.prepare_batch(
                current, list(messages.values())
            )
            stream = batch["messenger_message_placements"][0]["stream_uuid"]
            for number in range(40):
                db.execute(
                    "INSERT INTO messenger_topics(uuid,project_id,stream_uuid,name,source_name,provider_uuid,provider_identity) "
                    "VALUES (%s,%s,%s,%s,'zulip',%s,%s)",
                    (
                        uuid.uuid4(),
                        account["project_id"],
                        stream,
                        f"Unrelated {number}",
                        account["provider_uuid"],
                        f"synthetic:{number}",
                    ),
                )
            async with service.db.transaction() as connection:
                await service.operations.merge_batch(connection, current, batch)
                rows = incoming_operations.rows
                topic_counts = []

                async def observed(connection, query, parameters=()):
                    values = await rows(connection, query, parameters)
                    if "FROM messenger_topics" in query:
                        topic_counts.append(len(values))
                    return values

                with mock.patch.object(
                    incoming_operations, "rows", side_effect=observed
                ):
                    prepared = await history_blocks.context(
                        connection,
                        current,
                        list(messages.values()),
                        5,
                        batch["_context"],
                    )
                assert topic_counts == [
                    len(
                        {
                            row["topic_uuid"]
                            for row in batch["messenger_message_placements"]
                        }
                    )
                ]

                async def original_topic_scope(connection, query, parameters=()):
                    if "FROM messenger_topics" in query:
                        query = query.replace("AND uuid=ANY(%s::uuid[])", "")
                        parameters = parameters[:-1]
                    return await rows(connection, query, parameters)

                # Prepared catalogs intentionally describe the conversion
                # snapshot. Compare the previous SQL scope with the new one,
                # rather than assuming it equals a fresh post-merge snapshot.
                with mock.patch.object(
                    incoming_operations, "rows", side_effect=original_topic_scope
                ):
                    assert prepared == await history_blocks.context(
                        connection,
                        current,
                        list(messages.values()),
                        5,
                        batch["_context"],
                    )

    asyncio.run(run())
