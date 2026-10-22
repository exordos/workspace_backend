# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime

import pytest
from restalchemy.common import contexts
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as message_states


message_sources = message_states.message_sources


def _reconcile():
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)


@pytest.mark.parametrize(
    "flag", ["stream_wildcard_mentioned", "topic_wildcard_mentioned"]
)
@pytest.mark.parametrize(
    "stream_mode,policy,expected",
    [
        ("all_messages", 0, True),
        ("all_messages", 1, False),
        ("all_messages", 2, True),
        ("all_messages", 3, True),
        ("mentions_only", 0, False),
        ("mentions_only", 1, False),
        ("mentions_only", 2, True),
        ("mentions_only", 3, True),
    ],
)
def test_wildcard_mentions_follow_zulip_mute_and_topic_overrides(
    message_sources, flag, stream_mode, policy, expected
):
    account, source = message_sources()
    source.read = source.mentioned = False
    setattr(source, flag, True)
    source.update()
    source.channel.notification_mode = stream_mode
    source.channel.update()
    message_states._prepare(account)
    _reconcile()
    with contexts.Context().session_manager():
        models.ZulipTopicNotification.import_snapshot(
            source.channel,
            [
                {
                    "topic_name": "gEnErAl",
                    "visibility_policy": policy,
                    "last_updated": 42,
                }
            ],
        )
    message_states._reconcile_topic_preferences()
    _reconcile()
    (message,) = message_states._viewer_rows(account, v2_models.WorkspaceUserMessage)
    assert message.mentioned is expected
    assert not message.read
    message_states._refresh_counters()
    (stream,) = message_states._viewer_rows(account, v2_models.WorkspaceStreamBinding)
    assert stream.active_unread_count == int(expected)
    assert stream.passive_unread_count == 1 - int(expected)


@pytest.mark.parametrize("stream_mode", ["all_messages", "mentions_only"])
@pytest.mark.parametrize("policy", [0, 1])
def test_personal_mentions_survive_muted_channels_and_topics(
    message_sources, stream_mode, policy
):
    account, source = message_sources()
    source.channel.notification_mode = stream_mode
    source.channel.update()
    message_states._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipTopicNotification.import_snapshot(
            source.channel,
            [
                {
                    "topic_name": "General",
                    "visibility_policy": policy,
                    "last_updated": 42,
                }
            ],
        )
    message_states._reconcile_topic_preferences()
    _reconcile()
    (message,) = message_states._viewer_rows(account, v2_models.WorkspaceUserMessage)
    assert message.mentioned


def test_topic_preference_changes_reclassify_without_overwriting_local_flags(
    message_sources,
):
    account, source = message_sources()
    source.read = source.mentioned = False
    source.stream_wildcard_mentioned = True
    source.update()
    message_states._prepare(account)
    _reconcile()
    (state,) = message_states._viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert state.mentioned
    read_at = datetime.datetime.now(datetime.timezone.utc)
    state.read_at = read_at
    state.starred = False
    state.pinned = True
    state.update()
    _reconcile()
    snapshot_at = state.zulip_flags_updated_at
    for policy, expected in [(1, False), (2, True), (1, False), (0, True)]:
        with contexts.Context().session_manager():
            models.ZulipTopicNotification.import_snapshot(
                source.channel,
                [
                    {
                        "topic_name": "General",
                        "visibility_policy": policy,
                        "last_updated": 42,
                    }
                ],
            )
        message_states._reconcile_topic_preferences()
        assert message_states._changes(models.ZulipMessageChange, account)
        _reconcile()
        (updated,) = message_states._viewer_rows(
            account, v2_models.WorkspaceUserMessageState
        )
        assert updated.mentioned is expected
        assert updated.read_at == read_at
        assert not updated.starred and updated.pinned
        assert updated.zulip_flags_updated_at == snapshot_at
        assert message_states._user_states(account) == []


def test_channel_preferences_keep_other_observers_mention_state(message_sources):
    account, source = message_sources()
    other, other_source = message_sources(project_id=account.project_id, sender_id=8)
    for observed in (source, other_source):
        observed.read = observed.mentioned = False
        observed.topic_wildcard_mentioned = True
        observed.update()
    message_states._prepare(account)
    _reconcile()
    for mode, expected in [("mentions_only", False), ("all_messages", True)]:
        source.channel.notification_mode = mode
        source.channel.notification_observed_at = datetime.datetime.now(
            datetime.timezone.utc
        )
        source.channel.update()
        message_states._prepare(account)
        _reconcile()
        states = message_states._viewer_rows(
            account, v2_models.WorkspaceUserMessageState
        )
        assert {row.user_uuid: row.mentioned for row in states} == {
            account.owner_user_uuid: expected,
            other.owner_user_uuid: True,
        }
        message_states._refresh_counters()
        streams = message_states._viewer_rows(account, v2_models.WorkspaceStreamBinding)
        assert {row.user_uuid: row.active_unread_count for row in streams} == {
            account.owner_user_uuid: int(expected),
            other.owner_user_uuid: 1,
        }


@pytest.mark.pre_retirement_schema
def test_wildcard_migration_backfills_existing_flags_and_is_reversible(
    message_sources, db
):
    account, source = message_sources()
    source.read = source.mentioned = False
    source.stream_wildcard_mentioned = True
    source.update()
    message_states._prepare(account)
    _reconcile()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = (
        "0228-Reconcile-Zulip-wildcard-mentions-with-notification-policies-afb4d0.py"
    )
    engine.rollback_migration(migration)
    try:
        (state,) = message_states._viewer_rows(
            account, v2_models.WorkspaceUserMessageState
        )
        read_at = datetime.datetime.now(datetime.timezone.utc)
        state.read_at = read_at
        state.starred = state.mentioned = False
        state.update()
        message_states._clear_changes(db, account)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert message_states._changes(models.ZulipMessageChange, account)
    _reconcile()
    (updated,) = message_states._viewer_rows(
        account, v2_models.WorkspaceUserMessageState
    )
    assert updated.mentioned
    assert updated.read_at == read_at and not updated.starred
    assert updated.zulip_flags_updated_at == state.zulip_flags_updated_at
