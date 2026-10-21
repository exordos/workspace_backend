# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import uuid

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import v2_models
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as message_tests


message_sources = message_tests.message_sources


def _prepare_unread(message_sources):
    account, source = message_sources()
    source.read = False
    source.update()
    message_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    return account, source


def _queue(account):
    return v2_models.WorkspaceMessageCounterChange.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _personal(account):
    (state,) = message_tests._viewer_rows(account, v2_models.WorkspaceUserMessageState)
    return state


def _assert_counts(account, unread, active):
    for model in (
        v2_models.WorkspaceStreamBinding,
        v2_models.WorkspaceUserTopicBinding,
    ):
        (binding,) = message_tests._viewer_rows(account, model)
        assert binding.unread_count == unread
        assert binding.active_unread_count == active
        assert binding.passive_unread_count == unread - active


def test_counter_delta_queue_coalesces_and_ignores_unrelated_flags(message_sources):
    account, _ = _prepare_unread(message_sources)
    (before,) = _queue(account)
    state = _personal(account)
    state.starred = not state.starred
    state.pinned = not state.pinned
    state.update()
    assert _queue(account)[0].revision == before.revision
    state.mentioned = not state.mentioned
    state.update()
    (after,) = _queue(account)
    assert after.revision > before.revision
    message_tests._refresh_counters()
    assert _queue(account) == []
    _assert_counts(account, 1, 1)


def test_counter_deltas_are_idempotent_and_coalesce_reverted_reads(message_sources, db):
    account, source = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    _assert_counts(account, 1, 1)
    state = _personal(account)
    state.read_at = datetime.datetime.now(datetime.timezone.utc)
    state.update()
    state.read_at = None
    state.update()
    message_tests._refresh_counters()
    _assert_counts(account, 1, 1)
    for _ in range(2):
        db.execute(
            "SELECT messenger_mark_counter_message(%s, %s, %s, %s, %s)",
            (
                account.project_id,
                account.owner_user_uuid,
                state.placement_uuid,
                source.channel.stream_uuid,
                source.topic.uuid,
            ),
        )
        message_tests._refresh_counters()
        _assert_counts(account, 1, 1)
    assert db.execute(
        "SELECT count(*) FROM messenger_message_counter_contributions WHERE project_id=%s",
        (account.project_id,),
    ).fetchone() == (1,)


def test_failed_counter_delta_rolls_back_totals_contributions_and_ack(
    message_sources, db
):
    account, source = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    state = _personal(account)
    state.read_at = datetime.datetime.now(datetime.timezone.utc)
    state.update()
    with pytest.raises(RuntimeError, match="Abort delta"):
        with contexts.Context().session_manager():
            assert not v2_models.WorkspaceMessageCounterChange.reconcile_batch(
                account.project_id,
                account.owner_user_uuid,
                source.channel.stream_uuid,
                100,
            )
            raise RuntimeError("Abort delta")
    assert len(_queue(account)) == 1
    assert db.execute(
        "SELECT unread_count FROM messenger_message_counter_totals WHERE project_id=%s",
        (account.project_id,),
    ).fetchone() == (1,)
    message_tests._refresh_counters()
    _assert_counts(account, 0, 0)
    assert _queue(account) == []


def test_counter_delta_batches_continue_until_all_topics_are_published(
    message_sources, monkeypatch
):
    monkeypatch.setattr(v2_projection, "COUNTER_DELTA_BATCH_SIZE", 1)
    account, source = _prepare_unread(message_sources)
    for offset in range(1, 4):
        topic = models.ZulipTopic(
            uuid=uuid.uuid5(source.channel.stream_uuid, str(offset)),
            stream_id=source.channel.stream_id,
            name=f"Topic {offset}",
            max_id=source.message_id + offset,
        )
        topic.insert()
        models.ZulipMessage(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            message_uuid=uuid.uuid4(),
            message_id=source.message_id + offset,
            sender_id=source.sender_id,
            timestamp=source.timestamp + offset,
            channel=source.channel,
            topic=topic,
            content=source.content,
        ).insert()
    message_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    message_tests._refresh_counters()
    assert _queue(account) == []
    (stream,) = message_tests._viewer_rows(account, v2_models.WorkspaceStreamBinding)
    assert stream.unread_count == stream.active_unread_count == 4
    topics = message_tests._viewer_rows(account, v2_models.WorkspaceUserTopicBinding)
    assert len(topics) == 4
    assert all(topic.unread_count == topic.active_unread_count == 1 for topic in topics)


def test_counter_delta_keeps_tombstones_until_physical_deletion_is_applied(
    message_sources, db
):
    account, source = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    (message,) = message_tests._messages(account)
    message.delete()
    assert len(_queue(account)) == 1
    assert db.execute(
        "SELECT count(*) FROM messenger_message_counter_contributions WHERE project_id=%s",
        (account.project_id,),
    ).fetchone() == (1,)
    message_tests._refresh_counters()
    _assert_counts(account, 0, 0)
    assert _queue(account) == []


def test_counter_deltas_follow_soft_deletion_and_restore(message_sources):
    account, _ = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    (message,) = message_tests._messages(account)
    for deleted, expected in [
        (datetime.datetime.now(datetime.timezone.utc), 0),
        (None, 1),
    ]:
        message.properties["deleted_at"].set_value_force(deleted)
        message.update()
        message_tests._refresh_counters()
        _assert_counts(account, expected, expected)


def test_counter_modes_use_topic_totals_without_requeueing_messages(message_sources):
    account, _ = _prepare_unread(message_sources)
    state = _personal(account)
    state.mentioned = False
    state.update()
    message_tests._refresh_counters()
    (stream,) = message_tests._viewer_rows(account, v2_models.WorkspaceStreamBinding)
    for mode, active in [("mentions_only", 0), ("all_messages", 1), ("muted", 0)]:
        stream.notification_mode = mode
        stream.update()
        assert _queue(account) == []
        message_tests._refresh_counters()
        _assert_counts(account, 1, active)


def test_counter_generation_change_does_not_include_previous_access(message_sources):
    account, _ = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    (stream,) = message_tests._viewer_rows(account, v2_models.WorkspaceStreamBinding)
    stream.properties["membership_generation"].set_value_force(
        stream.membership_generation + 1
    )
    stream.update()
    assert _queue(account) == []
    message_tests._refresh_counters()
    _assert_counts(account, 0, 0)
    state = _personal(account)
    binding = message_tests._viewer_rows(
        account, v2_models.WorkspaceUserMessageBinding
    )[0]
    for row in (state, binding):
        row.properties["membership_generation"].set_value_force(
            stream.membership_generation
        )
        row.update()
    message_tests._refresh_counters()
    _assert_counts(account, 1, 1)


@pytest.mark.pre_retirement_schema
def test_counter_delta_migration_seeds_existing_counts_and_rolls_back(
    message_sources, db
):
    account, source = _prepare_unread(message_sources)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    name = "0229-Maintain-imported-message-counters-from-per-message-deltas-5d0ad5.py"
    engine.rollback_migration(name)
    try:
        assert db.execute(
            "SELECT to_regclass('messenger_message_counter_changes')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    _assert_counts(account, 1, 1)
    message_tests._refresh_counters()
    _assert_counts(account, 1, 1)
    state = _personal(account)
    state.read_at = datetime.datetime.now(datetime.timezone.utc)
    state.update()
    message_tests._refresh_counters()
    _assert_counts(account, 0, 0)


def test_counter_changes_during_acknowledgement_are_processed_again(
    message_sources, db
):
    account, source = _prepare_unread(message_sources)
    db.execute("""
        CREATE FUNCTION test_requeue_counter_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE messenger_user_message_states SET read_at = NOW()
            WHERE project_id = NEW.project_id AND user_uuid = NEW.user_uuid
              AND placement_uuid = NEW.placement_uuid;
            RETURN NULL;
        END;
        $$;
        CREATE TRIGGER test_requeue_counter_change
        AFTER INSERT ON messenger_message_counter_contributions
        FOR EACH ROW EXECUTE FUNCTION test_requeue_counter_change();
    """)
    try:
        with contexts.Context().session_manager():
            assert v2_models.WorkspaceMessageCounterChange.reconcile_batch(
                account.project_id,
                account.owner_user_uuid,
                source.channel.stream_uuid,
                100,
            )
    finally:
        db.execute("""
            DROP TRIGGER test_requeue_counter_change ON messenger_message_counter_contributions;
            DROP FUNCTION test_requeue_counter_change();
        """)
    assert len(_queue(account)) == 1
    message_tests._refresh_counters()
    _assert_counts(account, 0, 0)
    assert _queue(account) == []


def test_topic_deletion_cascades_counter_storage_and_refreshes_stream(
    message_sources, db
):
    account, _ = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    (topic,) = v2_models.WorkspaceStreamTopic.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    topic.delete()
    for table in (
        "messenger_message_counter_contributions",
        "messenger_message_counter_totals",
        "messenger_message_counter_changes",
    ):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE project_id=%s", (account.project_id,)
        ).fetchone() == (0,)
    message_tests._refresh_counters()
    (stream,) = message_tests._viewer_rows(account, v2_models.WorkspaceStreamBinding)
    assert stream.unread_count == 0 and stream.last_message_uuid is None


def test_counter_deltas_follow_access_hiding_and_restoration(message_sources):
    account, _ = _prepare_unread(message_sources)
    message_tests._refresh_counters()
    (access,) = message_tests._viewer_rows(
        account, v2_models.WorkspaceUserMessageBinding
    )
    for visibility, expected in [("hidden", 0), ("visible", 1)]:
        access.visibility = visibility
        access.update()
        message_tests._refresh_counters()
        _assert_counts(account, expected, expected)
