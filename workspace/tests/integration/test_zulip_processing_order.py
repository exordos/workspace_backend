# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
from unittest import mock
import uuid

import pytest
import psycopg
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import v2_models
from workspace.services import history_import
from workspace.tests.integration import conftest
from workspace.tests.integration import (
    test_zulip_content_conversion as conversion_tests,
)
from workspace.tests.integration import test_zulip_message_states as state_tests
from workspace.tests.integration import test_zulip_messages as message_tests


history_account = message_tests.history_account


def _data():
    timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 1000
    return [
        message_tests._message_data(value, timestamp=timestamp)
        for value in (41, 43, 42)
    ]


def test_history_converts_content_and_downloads_files_newest_first(
    history_account, api, db, monkeypatch, tmp_path
):
    account = history_account
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    message_tests._create_workspace_stream(account, db)
    data = [
        {**row, "content": f"[file](/user_uploads/1/hash/{row['id']}.txt)"}
        for row in _data()
    ]
    zulip = mock.Mock()
    zulip.get_messages.return_value = data
    zulip.download_file.return_value = b"file", "text/plain"
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    original = content_conversion.ConversionContext.to_workspace
    seen = []

    def convert(converter, data, stream_uuid, zulip):
        seen.append(data["id"])
        return original(converter, data, stream_uuid, zulip)

    monkeypatch.setattr(content_conversion.ConversionContext, "to_workspace", convert)
    assert message_tests._import_history(account).status == "done"
    assert seen == [43, 42, 41]
    assert zulip.download_file.call_args_list == [
        mock.call(f"/user_uploads/1/hash/{value}.txt") for value in seen
    ]


def test_stored_conversion_continues_newest_first_with_equal_timestamps(
    history_account, db, monkeypatch
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = _data()
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    db.execute(
        "UPDATE bridge_zulip_v5_message_contents SET conversion_version=0 WHERE uuid IN (SELECT content FROM bridge_zulip_v5_messages WHERE project_id=%s)",
        (account.project_id,),
    )
    original = content_conversion.ConversionContext.to_workspace
    seen = []

    def convert(converter, data, stream_uuid, zulip):
        seen.append(data["id"])
        return original(converter, data, stream_uuid, zulip)

    monkeypatch.setattr(content_conversion.ConversionContext, "to_workspace", convert)
    # Old persisted UUID cursors must not skip unconverted content after 0242.
    task = models.Task(
        content=models.ConvertMessageContent(
            batch_size=1, after_uuid=uuid.UUID(int=2**128 - 1)
        )
    )
    task.insert()
    created = [task]
    try:
        for _ in range(4):
            with contexts.Context().session_manager():
                task.execute()
                assert task.status == "done"
            children = models.Task.objects.get_all(
                filters={
                    "content": filters.JSONFields({"kind": "convert_message_content"}),
                    "status": filters.EQ("new"),
                }
            )
            if not children:
                break
            (task,) = children
            created.append(task)
        assert seen == [43, 42, 41]
    finally:
        for task in created:
            task.delete()


@pytest.mark.parametrize(
    "queue", [models.ZulipMessageChange, flags.FlagChange, reactions.ReactionChange]
)
def test_reconciliation_prefers_the_newest_message_over_the_oldest_revision(
    history_account, db, monkeypatch, queue
):
    account = history_account
    data = sorted(_data(), key=lambda row: row["id"])
    zulip = mock.Mock()
    zulip.get_messages.return_value = data
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    state_tests._prepare(account)
    if queue is not models.ZulipMessageChange:
        conversion_tests._publish(account, db)
        with contexts.Context().session_manager():
            at = datetime.datetime.now(datetime.timezone.utc)
            if queue is flags.FlagChange:
                flags.observe_snapshot(
                    account, [{**row, "flags": ["read"]} for row in data], at
                )
            else:
                reactions.observe_snapshot(
                    account,
                    [
                        {
                            **row,
                            "reactions": [
                                {
                                    "user_id": 7,
                                    "emoji_name": "thumbs_up",
                                    "emoji_code": "1f44d",
                                    "reaction_type": "unicode_emoji",
                                }
                            ],
                        }
                        for row in data
                    ],
                    at,
                )
    pending = queue.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)},
        order_by={
            "message_created_at": "desc",
            "message_sequence": "desc",
            "revision": "asc",
        },
    )
    assert [change.message_sequence for change in pending] == [43, 42, 41]
    # Make the revision order actively disagree with message age.
    for change in pending:
        db.execute(
            f"UPDATE {queue.__tablename__} SET revision=%s WHERE project_id=%s AND message_sequence=%s",
            (
                change.message_sequence,
                account.project_id,
                change.message_sequence,
            ),
        )
    with contexts.Context().session_manager():
        if queue is models.ZulipMessageChange:
            assert models.ZulipMessageState.reconcile_batch(1) == 1
        elif queue is flags.FlagChange:
            assert queue.reconcile_batch(1) == 1
            for change in pending:
                state = v2_models.WorkspaceUserMessageState.objects.get_one(
                    filters={"uuid": filters.EQ(change.state_uuid)}
                )
                assert (state.read_at is not None) == (change.message_sequence == 43)
            return
        else:
            with mock.patch.object(
                reactions, "reconcile", wraps=reactions.reconcile
            ) as reconcile:
                assert queue.reconcile_batch(1) == 1
            assert reconcile.call_args.args == (
                account.project_id,
                pending[0].canonical_message_uuid,
            )
            return
    remaining = queue.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert sorted(change.message_sequence for change in remaining) == [41, 42]


def test_refreshes_flags_and_reactions_from_the_latest_known_range(
    history_account, monkeypatch
):
    account = history_account
    account.settings = {**account.settings, "api_key": "key"}
    account.status = "active"
    account.update()
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        message_tests._message_data(value) for value in (1, 101, 201)
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account, 1, 201).status == "done"
    zulip.get_messages.reset_mock()
    zulip.get_messages.side_effect = lambda first, last: [
        row for row in _data() if first <= row["id"] <= last
    ]
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.RefreshMessageFlagsContent(last_message_id=201),
    )
    task.insert()
    tasks = [task]
    try:
        for _ in range(4):
            with contexts.Context().session_manager():
                task.execute()
                assert task.status == "done"
            children = models.Task.objects.get_all(
                filters={
                    "external_account_uuid": filters.EQ(account.uuid),
                    "content": filters.JSONFields({"kind": "refresh_message_flags"}),
                    "status": filters.EQ("new"),
                }
            )
            if not children:
                break
            (task,) = children
            tasks.append(task)
        assert zulip.get_messages.call_args_list == [
            mock.call(102, 201),
            mock.call(2, 101),
            mock.call(1, 1),
        ]
    finally:
        for task in tasks:
            task.delete()


def test_worker_selects_queued_flag_tasks_newest_first(
    history_account, db, monkeypatch, scheduled_reconciliation
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = _data()
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    conversion_tests._publish(account, db)
    rows = db.execute(
        """SELECT state.uuid, observed.message_id
                         FROM messenger_user_message_states AS state
                         JOIN messenger_message_placements AS placement
                           ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
                         JOIN bridge_zulip_v5_messages AS observed
                           ON observed.project_id=placement.project_id
                          AND messenger_uuid_v5(observed.project_id,observed.message_uuid::text)=placement.message_uuid
                         WHERE observed.project_id=%s ORDER BY observed.message_id""",
        (account.project_id,),
    ).fetchall()
    tasks = []
    try:
        for state_uuid, message_id in rows:
            task = models.Task(
                project_id=account.project_id,
                external_account_uuid=account.uuid,
                priority=models.TASK_PRIORITY_REALTIME_FLAGS,
                content=models.UpdateMessageFlagsContent(
                    state_uuid=state_uuid,
                    flag="read",
                    value=True,
                    changed_at=datetime.datetime.now(datetime.timezone.utc),
                    account_revision=account.realtime_revision(),
                ),
            )
            task.insert()
            tasks.append(task)
        seen = []
        service = history_import.HistoryImportService()
        for _ in tasks:
            with contexts.Context().session_manager():
                (task,) = service._get_tasks()
                assert task.uuid in {item.uuid for item in tasks}
                seen.append(task.message_sequence)
                task.status = "cancel"
                task.update()
        assert seen == [43, 42, 41]
    finally:
        for task in tasks:
            task.delete()


def test_ordering_migration_backfills_existing_content_without_requeueing_it(
    history_account, db, monkeypatch
):
    account = history_account
    data = _data()
    zulip = mock.Mock()
    zulip.get_messages.return_value = data
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)
    assert message_tests._import_history(account).status == "done"
    query = "SELECT message_uuid,revision FROM bridge_zulip_v5_message_changes WHERE project_id=%s ORDER BY message_uuid"
    before = db.execute(query, (account.project_id,)).fetchall()
    target = next(conftest.MIGRATIONS_DIR.glob("0242-*.py")).name
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(target)
    engine.apply_migration(target)
    assert db.execute(query, (account.project_id,)).fetchall() == before
    rows = db.execute(
        """SELECT content.source_timestamp,content.source_message_id,
                                  changes.message_sequence
                          FROM bridge_zulip_v5_messages AS observed
                          JOIN bridge_zulip_v5_message_contents AS content ON content.uuid=observed.content
                          JOIN bridge_zulip_v5_message_changes AS changes
                            ON changes.project_id=observed.project_id
                           AND changes.message_uuid=messenger_uuid_v5(observed.project_id,observed.message_uuid::text)
                          WHERE observed.project_id=%s ORDER BY changes.message_sequence DESC""",
        (account.project_id,),
    ).fetchall()
    assert rows == [(data[0]["timestamp"], value, value) for value in (43, 42, 41)]
    engine.apply_migration(engine.get_latest_migration())


def test_ordering_migration_round_trips(_database):
    target = next(conftest.MIGRATIONS_DIR.glob("0242-*.py")).name
    with conftest.isolated_migration_database(target) as url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(url, autocommit=True) as db:
            assert db.execute(
                "SELECT to_regclass('bridge_zulip_v5_content_newest_idx') IS NOT NULL"
            ).fetchone() == (True,)
            engine.rollback_migration(target)
            assert db.execute(
                "SELECT to_regprocedure('bridge_zulip_v5_order_message_change()')"
            ).fetchone() == (None,)
            engine.apply_migration(target)
            assert db.execute(
                "SELECT to_regclass('bridge_zulip_v5_flag_changes_newest_idx') IS NOT NULL"
            ).fetchone() == (True,)
