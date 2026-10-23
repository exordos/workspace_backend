# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import datetime
import json
import threading
from unittest import mock

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip import history_fingerprints
from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import history
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_messages as messages_tests


history_account = messages_tests.history_account
direct_peer = messages_tests.direct_peer


@pytest.fixture(autouse=True)
def transaction(history_account):
    with contexts.Context().session_manager():
        yield


def message(message_id=42, **values):
    return messages_tests._message_data(message_id, **{"reactions": [], **values})


def scan(account, messages, monkeypatch, last_id=1000):
    zulip = mock.Mock()
    zulip.get_history_batch.return_value = {
        "messages": messages,
        "next_id": 0,
        "history_limited": False,
    }
    monkeypatch.setattr(client, "ZulipClient", lambda _account: zulip)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ScanHistoryContent(last_message_id=last_id),
    )
    task.insert()
    task.execute()
    assert task.status == "done", task.error_description
    return task


def pending(account):
    return models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "import_history"}),
            "status": filters.EQ("new"),
        }
    )


def apply(account):
    tasks = pending(account)
    for task in tasks:
        task.execute()
        assert task.status == "done", task.error_description
    return tasks


def test_interleaved_observers_finish_their_original_snapshot_children(
    history_account, direct_peer, monkeypatch
):
    """One observer's replay must not cancel another observer's pending batch."""
    messages_tests._channel(direct_peer)
    monkeypatch.setattr(models, "HISTORY_BATCH_SIZE", 1)
    reaction = {
        "user_id": 7,
        "emoji_name": "test_tube",
        "emoji_code": "1f9ea",
        "reaction_type": "unicode_emoji",
    }
    data = [message(message_id, reactions=[reaction]) for message_id in (41, 42)]
    for account in (history_account, direct_peer):
        scan(account, data, monkeypatch)
    first, second = pending(history_account), pending(direct_peer)
    assert len(first) == len(second) == 2
    for tasks in zip(first, second):
        for task in tasks:
            task.execute()
            assert task.status == "done", task.error_description
    for account in (history_account, direct_peer):
        assert len(messages_tests._messages(account)) == 2
        assert history.batch(account, 1)["ready"]


def test_parallel_snapshot_writers_do_not_cross_lock_observer_batches(
    history_account, direct_peer, monkeypatch
):
    """Reproduce the lock cycle from the full multi-account recovery run."""
    messages_tests._channel(direct_peer)
    for account in (history_account, direct_peer):
        scan(account, [message()], monkeypatch)
    contexts.Context().get_session().commit()
    barrier = threading.Barrier(2)

    def invalidate_other(account, other):
        try:
            with psycopg.connect(conftest.TEST_DB_URL) as connection:
                connection.execute("SET LOCAL lock_timeout='3s'")
                connection.execute(
                    "SELECT uuid FROM m_external_accounts_v2 WHERE uuid=%s FOR UPDATE",
                    (account.uuid,),
                )
                connection.execute(
                    "SELECT first_id FROM bridge_zulip_v5_history_batches "
                    "WHERE external_account_uuid=%s AND first_id=1 FOR UPDATE",
                    (account.uuid,),
                )
                connection.execute(
                    "SELECT set_config('workspace.zulip_history_batch',%s,true)",
                    (f"{account.uuid}:1",),
                )
                barrier.wait(timeout=5)
                connection.execute(
                    "SELECT bridge_zulip_v5_invalidate_history(%s,42,ARRAY['reactions'])",
                    (other.uuid,),
                )
            return None
        except psycopg.Error as error:
            return error.sqlstate

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(invalidate_other, history_account, direct_peer),
            executor.submit(invalidate_other, direct_peer, history_account),
        ]
        assert [future.result(timeout=10) for future in futures] == [None, None]


def test_shared_history_replay_keeps_other_snapshot_but_fresh_changes_are_detected(
    history_account, direct_peer, monkeypatch
):
    messages_tests._channel(direct_peer)
    for account in (history_account, direct_peer):
        scan(account, [message()], monkeypatch)
        apply(account)
    scan(history_account, [message()], monkeypatch)
    previous = history.batch(direct_peer, 1)["hashes"]
    reaction = {
        "user_id": 7,
        "emoji_name": "test_tube",
        "emoji_code": "1f9ea",
        "reaction_type": "unicode_emoji",
    }
    changed = message(reactions=[reaction])
    scan(history_account, [changed], monkeypatch)
    apply(history_account)
    assert history.batch(direct_peer, 1)["ready"]
    assert history.batch(direct_peer, 1)["hashes"] == previous
    scan(direct_peer, [changed], monkeypatch)
    (task,) = pending(direct_peer)
    assert task.content.snapshot[0]["components"] == ["reactions"]
    apply(direct_peer)
    assert all(
        history.batch(account, 1)["ready"] for account in (history_account, direct_peer)
    )
    # A real event or another out-of-band source mutation still fences both
    # observers; only application of a known history snapshot is exempt.
    contexts.Context().get_session().execute(
        "UPDATE bridge_zulip_v5_reactions SET present=false WHERE reaction_message_uuid IN "
        "(SELECT uuid FROM bridge_zulip_v5_reaction_messages WHERE project_id=%s)",
        (history_account.project_id,),
    )
    assert all(
        not history.batch(account, 1)["ready"]
        for account in (history_account, direct_peer)
    )


def test_reaction_projection_keeps_pending_and_ready_history_checkpoints(
    history_account, monkeypatch
):
    """The real projector links native facts without changing provider input."""
    account = history_account
    monkeypatch.setattr(models, "HISTORY_BATCH_SIZE", 1)
    reaction = {
        "user_id": 7,
        "emoji_name": "test_tube",
        "emoji_code": "1f9ea",
        "reaction_type": "unicode_emoji",
    }
    data = [message(message_id, reactions=[reaction]) for message_id in (41, 42)]
    scan(account, data, monkeypatch)
    first, second = pending(account)
    original_revision = history.batch(account, 1)["revision"]
    first.execute()
    assert first.status == "done", first.error_description
    for model in (
        models.ZulipProjectUserState,
        models.ZulipStreamState,
        models.ZulipStreamBindingState,
    ):
        for state in model.objects.get_all():
            state.do()
    models.ZulipTopicState.reconcile_batch(100)
    models.ZulipMessageState.reconcile_batch(100)
    # The first observation precedes canonical creation. Make the real
    # projector's deferred bind due now instead of sleeping for its retry.
    session = contexts.Context().get_session()
    session.execute(
        "UPDATE bridge_zulip_v5_reaction_messages SET next_retry_at=NULL WHERE project_id=%s",
        (account.project_id,),
    )
    reactions.ReactionChange.reconcile_batch(100)
    assert session.execute(
        "SELECT count(*) AS n FROM bridge_zulip_v5_reactions WHERE fact_uuid IS NOT NULL"
    ).fetchone()["n"]
    assert history.batch(account, 1)["revision"] == original_revision
    second.execute()
    assert second.status == "done", second.error_description
    models.ZulipMessageState.reconcile_batch(100)
    session.execute(
        "UPDATE bridge_zulip_v5_reaction_messages SET next_retry_at=NULL WHERE project_id=%s",
        (account.project_id,),
    )
    reactions.ReactionChange.reconcile_batch(100)
    assert history.batch(account, 1)["ready"]
    scan(account, data, monkeypatch)
    assert pending(account) == []
    session.execute(
        "UPDATE bridge_zulip_v5_reactions SET fact_uuid=fact_uuid WHERE user_id=7"
    )
    assert history.batch(account, 1)["ready"]
    # A newer provider observation must still fence an older in-flight scan,
    # even when the reaction value itself did not change.
    session.execute(
        "UPDATE bridge_zulip_v5_reactions SET observed_at=observed_at+interval '1 second' WHERE user_id=7"
    )
    assert not history.batch(account, 1)["ready"]


def test_unchanged_recovery_creates_no_processing_tasks_or_source_writes(
    history_account, monkeypatch, db
):
    account = history_account
    scan(account, [message()], monkeypatch)
    assert len(apply(account)) == 1
    assert history.batch(account, 1)["ready"]
    source = (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT updated_at,content FROM bridge_zulip_v5_messages WHERE external_account_uuid=%s",
            (account.uuid,),
        )
        .fetchone()
    )
    observed = (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT updated_at FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s ORDER BY flag",
            (account.uuid,),
        )
        .fetchall()
    )
    monkeypatch.setattr(
        content_conversion,
        "ConversionContext",
        mock.Mock(side_effect=AssertionError("unchanged body converted")),
    )
    scan(account, [message()], monkeypatch)
    assert pending(account) == []
    assert (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT updated_at,content FROM bridge_zulip_v5_messages WHERE external_account_uuid=%s",
            (account.uuid,),
        )
        .fetchone()
        == source
    )
    assert (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT updated_at FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s ORDER BY flag",
            (account.uuid,),
        )
        .fetchall()
        == observed
    )


@pytest.mark.parametrize(
    "values,components",
    [
        ({"flags": ["read"]}, ["flags"]),
        ({"subject": "Moved", "last_edit_timestamp": 1691057100}, ["metadata"]),
        ({"content": "Edited"}, ["text"]),
    ],
)
def test_delta_task_contains_only_changed_components(
    history_account, monkeypatch, values, components
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    if components != ["text"]:
        monkeypatch.setattr(
            content_conversion,
            "ConversionContext",
            mock.Mock(side_effect=AssertionError("unrelated body converted")),
        )
    scan(account, [message(**values)], monkeypatch)
    (task,) = pending(account)
    assert task.content.snapshot[0]["components"] == components
    apply(account)
    assert history.batch(account, 1)["ready"]
    scan(account, [message(**values)], monkeypatch)
    assert pending(account) == []


def test_fenced_snapshot_applies_a_topic_move_with_the_same_edit_timestamp(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    monkeypatch.setattr(
        content_conversion,
        "ConversionContext",
        mock.Mock(side_effect=AssertionError("topic move converted text")),
    )
    scan(account, [message(subject="Moved without a text edit")], monkeypatch)
    apply(account)
    (observed,) = models.ZulipMessage.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)}
    )
    assert observed.topic.name == "Moved without a text edit"
    assert history.batch(account, 1)["ready"]


def test_ready_checkpoint_waits_for_all_children(history_account, monkeypatch):
    account = history_account
    data = [message(index) for index in range(1, 102)]
    scan(account, data, monkeypatch)
    tasks = pending(account)
    assert len(tasks) == 2
    assert not history.batch(account, 1)["ready"]
    tasks[0].execute()
    assert tasks[0].status == "done", tasks[0].error_description
    assert not history.batch(account, 1)["ready"]
    tasks[1].execute()
    assert tasks[1].status == "done", tasks[1].error_description
    assert history.batch(account, 1)["ready"]


def test_completed_tasks_release_bodies_after_fingerprints_are_durable(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    (task,) = pending(account)
    session = contexts.Context().get_session()
    before = session.execute(
        "SELECT pg_column_size(content) AS bytes FROM bridge_zulip_v5_tasks WHERE uuid=%s",
        (task.uuid,),
    ).fetchone()["bytes"]
    apply(account)
    after = session.execute(
        "SELECT content,pg_column_size(content) AS bytes FROM bridge_zulip_v5_tasks WHERE uuid=%s",
        (task.uuid,),
    ).fetchone()
    assert "snapshot" not in after["content"]
    assert after["bytes"] < before
    assert history.batch(account, 1)["ready"]
    scan(account, [message()], monkeypatch)
    assert pending(account) == []


def test_live_flag_change_invalidates_only_flag_fingerprint(
    history_account, monkeypatch, db
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    flags.record(
        account,
        [42],
        "read",
        True,
        datetime.datetime.now(datetime.timezone.utc),
        event_id=10,
    )
    assert not history.batch(account, 1)["ready"]
    row = (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT hashes FROM bridge_zulip_v5_message_fingerprints WHERE external_account_uuid=%s AND message_id=42",
            (account.uuid,),
        )
        .fetchone()
    )
    assert row["hashes"]["flags"] is None
    assert row["hashes"]["text"] is not None
    scan(account, [message(flags=["read"])], monkeypatch)
    (task,) = pending(account)
    assert task.content.snapshot[0]["components"] == ["flags"]


def test_equal_snapshot_rejects_old_flag_and_unknown_reaction_events(
    history_account, monkeypatch, db
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    before = datetime.datetime.now(datetime.timezone.utc)
    scan(account, [message()], monkeypatch)
    flags.record(account, [42], "read", True, before, event_id=10)
    reactions.record_event(
        account,
        {
            "message_id": 42,
            "user_id": 7,
            "reaction_type": "unicode_emoji",
            "emoji_code": "1f44d",
            "emoji_name": "thumbs_up",
            "op": "add",
        },
        before,
        11,
        "old-queue",
    )
    assert (
        not contexts.Context()
        .get_session()
        .execute(
            "SELECT value FROM bridge_zulip_v5_message_flags WHERE external_account_uuid=%s AND message_id=42 AND flag='read'",
            (account.uuid,),
        )
        .fetchone()["value"]
    )
    assert (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT count(*) AS n FROM bridge_zulip_v5_reactions AS reaction JOIN bridge_zulip_v5_reaction_messages AS remote ON remote.uuid=reaction.reaction_message_uuid WHERE remote.project_id=%s AND reaction.present",
            (account.project_id,),
        )
        .fetchone()["n"]
        == 0
    )
    assert history.batch(account, 1)["ready"]


@pytest.mark.parametrize(
    "checkpoint", ["none", "other_empty", "own_pending", "own_absent"]
)
def test_reaction_barrier_requires_this_observer_ready_visible_message(
    history_account, direct_peer, monkeypatch, checkpoint
):
    # Account 7's self-DM is invisible to account 8, even in the same ID window.
    data = messages_tests._direct_message_data(42, user_ids=(7,), reactions=[])
    scan(history_account, [data], monkeypatch)
    apply(history_account)
    observed_at = datetime.datetime.now(datetime.timezone.utc)
    assert history.batch(history_account, 1)["observed_at"] < observed_at.replace(
        tzinfo=None
    )
    if checkpoint == "other_empty":
        scan(direct_peer, [], monkeypatch)
        assert history.batch(direct_peer, 1)["ready"]
        assert history.batch(direct_peer, 1)["record_count"] == 0
    elif checkpoint == "own_pending":
        scan(history_account, [{**data, "content": "Pending edit"}], monkeypatch)
        assert not history.batch(history_account, 1)["ready"]
    elif checkpoint == "own_absent":
        scan(history_account, [], monkeypatch)
        apply(history_account)
        assert history.batch(history_account, 1)["ready"]
        assert history.batch(history_account, 1)["record_count"] == 0
    reactions.record_event(
        history_account,
        {
            "message_id": 42,
            "user_id": 7,
            "reaction_type": "unicode_emoji",
            "emoji_code": "1f44d",
            "emoji_name": "thumbs_up",
            "op": "add",
        },
        observed_at,
        11,
        "own-queue",
    )
    stored = (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT count(*) AS n FROM bridge_zulip_v5_reactions AS reaction "
            "JOIN bridge_zulip_v5_reaction_messages AS message "
            "ON message.uuid=reaction.reaction_message_uuid "
            "WHERE message.project_id=%s AND message.message_id=42 "
            "AND reaction.present",
            (history_account.project_id,),
        )
        .fetchone()["n"]
    )
    assert stored == 1


def test_live_change_between_scan_and_apply_restarts_the_range(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    scan(account, [message(content="Snapshot edit")], monkeypatch)
    flags.record(
        account,
        [42],
        "starred",
        True,
        datetime.datetime.now(datetime.timezone.utc),
        event_id=12,
    )
    apply(account)
    assert not history.batch(account, 1)["ready"]
    scans = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "scan_history"}),
            "status": filters.EQ("new"),
        }
    )
    assert len(scans) == 1


def test_account_identity_change_never_reuses_old_fingerprints(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    account.settings = {"server_url": "https://other-zulip.example.com"}
    account.properties["external_account_id"].set_value_force("7")
    account.update()
    scan(account, [message()], monkeypatch)
    (task,) = pending(account)
    assert set(task.content.snapshot[0]["components"]) == {
        "text",
        "metadata",
        "flags",
        "reactions",
    }


def test_absence_withdraws_only_the_current_observer_and_is_cached(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    scan(account, [], monkeypatch)
    (task,) = pending(account)
    assert task.content.snapshot == [
        {"message": {"id": 42}, "components": ["visibility"]}
    ]
    apply(account)
    assert (
        models.ZulipMessage.objects.get_all(
            filters={"external_account_uuid": filters.EQ(account.uuid)}
        )
        == []
    )
    assert history.batch(account, 1)["ready"]
    scan(account, [], monkeypatch)
    assert pending(account) == []
    scan(account, [message()], monkeypatch)
    (task,) = pending(account)
    assert set(task.content.snapshot[0]["components"]) == {
        "text",
        "metadata",
        "flags",
        "reactions",
    }


def test_empty_source_cannot_skip_an_older_known_range(history_account, monkeypatch):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    scan(account, [], monkeypatch, last_id=2000)
    (continuation,) = models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "scan_history"}),
            "status": filters.EQ("new"),
        }
    )
    assert continuation.content.last_message_id == 1000


def _recover(account, messages, maximum, monkeypatch, root=None):
    zulip = mock.Mock()

    def page(first_id, last_id):
        older = [data["id"] for data in messages if data["id"] < first_id]
        return {
            "messages": [
                data for data in messages if first_id <= data["id"] <= last_id
            ],
            "next_id": max(older, default=0),
            "history_limited": False,
        }

    zulip.get_history_batch.side_effect = page
    monkeypatch.setattr(client, "ZulipClient", lambda _account: zulip)
    if root is None:
        root = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.FullBackfillContent(last_message_id=maximum),
        )
        root.insert()
    root.execute()
    assert root.status == "done", root.error_description
    for _ in range(10):
        cursors = models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "content": filters.JSONFields({"kind": "scan_history"}),
                "status": filters.EQ("new"),
            }
        )
        if not cursors:
            return zulip
        for cursor in cursors:
            cursor.execute()
            assert cursor.status == "done", cursor.error_description
            apply(account)
    pytest.fail("Bounded recovery did not finish")


@pytest.mark.parametrize("maximum", [0, 10])
def test_recovery_with_smaller_watermark_revisits_retained_tail(
    history_account, monkeypatch, maximum
):
    account = history_account
    scan(account, [message(1005)], monkeypatch, last_id=1005)
    apply(account)
    _recover(account, [message(10)] if maximum else [], maximum, monkeypatch)
    assert (
        models.ZulipMessage.objects.get_one_or_none(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "message_id": filters.EQ(1005),
            }
        )
        is None
    )


def test_recovery_does_not_chase_observations_created_after_it_was_queued(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message(10)], monkeypatch, last_id=10)
    apply(account)
    root = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.FullBackfillContent(last_message_id=10),
    )
    root.insert()
    scan(account, [message(1005)], monkeypatch, last_id=1005)
    apply(account)
    for cursor in models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "scan_history"}),
            "status": filters.EQ("new"),
        }
    ):
        cursor.status = "cancel"
        cursor.update()
    zulip = _recover(account, [message(10)], 10, monkeypatch, root=root)
    assert all(call.args[1] <= 10 for call in zulip.get_history_batch.call_args_list)
    assert (
        models.ZulipMessage.objects.get_one_or_none(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "message_id": filters.EQ(1005),
            }
        )
        is not None
    )


@pytest.mark.parametrize("previously_absent", [False, True])
def test_empty_range_withdraws_realtime_records_not_in_the_checkpoint(
    history_account, monkeypatch, previously_absent
):
    account = history_account
    scan(
        account,
        [message(10), message(42)] if previously_absent else [message(10)],
        monkeypatch,
        last_id=42,
    )
    apply(account)
    if previously_absent:
        scan(account, [message(10)], monkeypatch, last_id=42)
        apply(account)
    zulip = mock.Mock()
    zulip.get_messages.return_value = [message(42)]
    monkeypatch.setattr(client, "ZulipClient", lambda _account: zulip)
    realtime = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        queue="realtime",
        content=models.ImportHistoryContent(first_message_id=42, last_message_id=42),
    )
    realtime.insert()
    realtime.execute()
    assert realtime.status == "done", realtime.error_description
    scan(account, [], monkeypatch, last_id=42)
    apply(account)
    assert (
        models.ZulipMessage.objects.get_one_or_none(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "message_id": filters.EQ(42),
            }
        )
        is None
    )


def test_v1_checkpoint_cannot_hide_a_retained_untracked_observation(
    history_account, monkeypatch
):
    account = history_account
    with monkeypatch.context() as previous:
        previous.setattr(history_fingerprints, "VERSION", 1)
        scan(account, [message(42)], monkeypatch, last_id=42)
        apply(account)
        session = contexts.Context().get_session()
        # V1 could keep an absence marker after realtime recreated the record,
        # then confirm an empty checkpoint without withdrawing that record.
        session.execute(
            "UPDATE bridge_zulip_v5_message_fingerprints SET hashes=%s::jsonb "
            "WHERE external_account_uuid=%s",
            (json.dumps({"absent": True}), account.uuid),
        )
        session.execute(
            "UPDATE bridge_zulip_v5_history_batches SET hashes=%s::jsonb, "
            "record_count=0,ready=true WHERE external_account_uuid=%s",
            (json.dumps(history_fingerprints.batch_hashes([], 1, 42)), account.uuid),
        )
    scan(account, [], monkeypatch, last_id=42)
    apply(account)
    assert (
        models.ZulipMessage.objects.get_one_or_none(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "message_id": filters.EQ(42),
            }
        )
        is None
    )


def test_account_delete_cascades_fingerprints_without_updating_orphan_rows(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    # Exercise the real FK cascade, including source DELETE triggers.
    with models.utils.savepoint():
        contexts.Context().get_session().execute("SAVEPOINT account_delete_regression")
        models.external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        ).delete()
        assert history.batch(account, 1) is None
        contexts.Context().get_session().execute(
            "ROLLBACK TO SAVEPOINT account_delete_regression"
        )


def test_late_old_scan_cannot_replace_a_more_recent_snapshot(
    history_account, monkeypatch
):
    account = history_account
    before = datetime.datetime.now(datetime.timezone.utc)
    scan(account, [message()], monkeypatch)
    apply(account)
    assert (
        history.compare(account, 1, 1000, [message(content="Old response")], before)
        is None
    )
    assert history.batch(account, 1)["ready"]


def test_newer_same_value_event_fences_a_slow_conflicting_snapshot(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    apply(account)
    before = datetime.datetime.now(datetime.timezone.utc)
    flags.record(
        account,
        [42],
        "read",
        False,
        datetime.datetime.now(datetime.timezone.utc),
        event_id=15,
    )
    assert history.compare(account, 1, 1000, [message(flags=["read"])], before) is None
    scan(account, [message()], monkeypatch)
    assert pending(account) == []
    assert history.batch(account, 1)["ready"]


def test_rejected_older_content_is_never_confirmed_as_a_ready_fingerprint(
    history_account, monkeypatch
):
    account = history_account
    data = message(content="Latest", last_edit_timestamp=1691057300)
    scan(account, [data], monkeypatch)
    apply(account)
    scan(
        account, [message(content="Stale", last_edit_timestamp=1691057200)], monkeypatch
    )
    apply(account)
    (observed,) = models.ZulipMessage.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)}
    )
    assert observed.content.zulip_content == "Latest"
    assert not history.batch(account, 1)["ready"]


def test_database_deadlock_rolls_back_child_without_masking_retry_or_confirming_cache(
    history_account, monkeypatch
):
    account = history_account
    scan(account, [message()], monkeypatch)
    (task,) = pending(account)

    def deadlock(_converter, _messages):
        contexts.Context().get_session().execute(
            "DO $$ BEGIN RAISE EXCEPTION 'hash-child deadlock' USING ERRCODE='40P01'; END $$"
        )

    with monkeypatch.context() as patch:
        patch.setattr(content_conversion.ConversionContext, "prefetch", deadlock)
        task.execute()
    assert task.status == "retry", task.error_description
    assert not history.batch(account, 1)["ready"]
    assert (
        models.ZulipMessage.objects.get_all(
            filters={"external_account_uuid": filters.EQ(account.uuid)}
        )
        == []
    )
    assert (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT count(*) AS n FROM bridge_zulip_v5_message_fingerprints WHERE external_account_uuid=%s",
            (account.uuid,),
        )
        .fetchone()["n"]
        == 0
    )
    task.next_retry_at = None
    task.update()
    task.execute()
    assert task.status == "done", task.error_description
    assert history.batch(account, 1)["ready"]


def test_unsubscribed_history_is_cached_and_catalog_change_invalidates_it(
    history_account, monkeypatch
):
    account = history_account
    data = message(stream_id=999)
    scan(account, [data], monkeypatch)
    apply(account)
    assert history.batch(account, 1)["ready"]

    scan(account, [data], monkeypatch)
    assert pending(account) == []
    messages_tests._channel(account, 999)
    assert not history.batch(account, 1)["ready"]
    scan(account, [data], monkeypatch)
    (task,) = pending(account)
    assert set(task.content.snapshot[0]["components"]) == {
        "text",
        "metadata",
        "flags",
        "reactions",
    }
    apply(account)
    (observed,) = models.ZulipMessage.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)}
    )
    assert observed.channel.stream_id == 999
    assert history.batch(account, 1)["ready"]


def test_new_observer_creates_its_own_source_and_personal_flags(
    history_account, direct_peer, monkeypatch
):
    first, second = history_account, direct_peer
    messages_tests._channel(second)
    scan(first, [message(flags=["read"])], monkeypatch)
    apply(first)
    scan(second, [message(flags=["starred"])], monkeypatch)
    (task,) = pending(second)
    assert set(task.content.snapshot[0]["components"]) == {
        "text",
        "metadata",
        "flags",
        "reactions",
    }
    apply(second)
    rows = models.ZulipMessage.objects.get_all(
        filters={"project_id": filters.EQ(first.project_id)}
    )
    assert len(rows) == 2
    assert {row.external_account_uuid: (row.read, row.starred) for row in rows} == {
        first.uuid: (True, False),
        second.uuid: (False, True),
    }


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("batch_size", [1, 100])
def test_stale_snapshot_retry_preserves_history_cutoff(
    history_account, monkeypatch, stale, batch_size
):
    """A fenced batch must not import messages excluded by its original scan."""
    account = history_account
    cutoff = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(models, "HISTORY_BATCH_SIZE", batch_size)
    zulip = mock.Mock()
    zulip.get_history_batch.return_value = {
        "messages": [
            message(42, timestamp=1691057200),
            message(43, timestamp=1735689600),
        ],
        "next_id": 0,
        "history_limited": False,
    }
    monkeypatch.setattr(client, "ZulipClient", lambda _account: zulip)
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ScanHistoryContent(last_message_id=1000, since=cutoff),
    )
    task.insert()
    task.execute()
    assert task.status == "done", task.error_description
    children = pending(account)
    assert len(children) == (2 if batch_size == 1 else 1)
    assert all(child.content.since == cutoff for child in children)
    session = contexts.Context().get_session()
    if stale:
        session.execute(
            "UPDATE bridge_zulip_v5_history_batches SET revision=revision+1 "
            "WHERE external_account_uuid=%s",
            (account.uuid,),
        )
    for index, child in enumerate(children):
        child.execute()
        assert child.status == "done", child.error_description
        retries = models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "status": filters.EQ("new"),
                "content": filters.JSONFields({"kind": "scan_history"}),
            }
        )
        assert len(retries) == int(stale and index == len(children) - 1)
    if stale:
        (retry,) = retries
        assert retry.content.since == cutoff
        retry.execute()
        assert retry.status == "done", retry.error_description
        apply(account)
    ids = session.execute(
        "SELECT message_id FROM bridge_zulip_v5_messages "
        "WHERE external_account_uuid=%s ORDER BY message_id",
        (account.uuid,),
    ).fetchall()
    assert [row["message_id"] for row in ids] == [43]
