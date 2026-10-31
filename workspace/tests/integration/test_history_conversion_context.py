# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import copy
import itertools
import uuid
from unittest import mock

from tools import benchmark_history_prepare as benchmark
from workspace.bridges.zulip import history, incoming_operations
from workspace.tests.integration import test_zulip_history as history_tests
from workspace.tests.unit import test_zulip_conversion_context as context_tests

incoming = history_tests.incoming


def test_batch_context_preserves_prepared_rows_and_persisted_messages(incoming):
    _, db, account, *_ = incoming
    messages = {
        101: history_tests.message(101, "@**alias|7** hello"),
        102: history_tests.message(102, "#**Channel>General@101**"),
        103: history_tests.message(103, "> snapshot", subject="✔ General"),
        104: history_tests.message(
            104,
            "Direct",
            type="private",
            display_recipient=[
                {"id": 7, "full_name": "alias-7"},
                {"id": 8, "full_name": "alias-8"},
            ],
        ),
    }
    history_tests.request(db, account)

    def snapshot():
        return {
            table: db.execute(
                f"SELECT to_jsonb(item) FROM {table} item WHERE project_id=%s ORDER BY to_jsonb(item)::text",
                (account["project_id"],),
            ).fetchall()
            for table in (
                "messenger_messages",
                "messenger_message_placements",
                "messenger_topics",
                "messenger_user_message_bindings",
                "messenger_user_message_unreads",
                "messenger_user_message_stars",
                "messenger_user_message_mentions",
                "messenger_message_reaction_facts",
            )
        }

    async def run():
        async with history_tests.worker(incoming, messages) as (service, _):
            await service.step(await history_tests.job(service, account))
            current = await history_tests.job(service, account)
            page = list(messages.values())
            await service.operations.prepare_batch(
                current, copy.deepcopy(page)
            )  # Resolve dependencies.

            async def prepare():
                ids = itertools.count(100000)
                with mock.patch.object(
                    history.uuid, "uuid4", side_effect=lambda: uuid.UUID(int=next(ids))
                ):
                    return await service.operations.prepare_batch(
                        current, copy.deepcopy(page)
                    )

            with mock.patch.object(
                incoming_operations,
                "conversion_context",
                side_effect=lambda *args, **kwargs: context_tests.legacy_context(*args),
            ):
                expected = await prepare()
            actual = await prepare()
            assert benchmark.checksum(actual) == benchmark.checksum(expected)
            async with service.db.transaction() as connection:
                await service.operations.merge_batch(connection, current, expected)
            before = snapshot()
            assert len(before["messenger_messages"]) == 4
            async with service.db.transaction() as connection:
                await service.operations.merge_batch(connection, current, actual)
            assert snapshot() == before

    asyncio.run(run())
