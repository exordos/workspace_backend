# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Notification settings through the real HTTP API and existing bridge workers."""

from unittest import mock

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_message_states as state_tests


message_sources = state_tests.message_sources


@pytest.fixture
def notification_pair(message_sources, api, monkeypatch):
    account, source = message_sources()
    account.status = "active"
    account.settings = {
        **account.settings,
        "email": "owner@example.com",
        "api_key": "test-key",
    }
    account.update()
    source.channel.notification_mode = "all_messages"
    source.channel.update()
    state_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
        models.ZulipTopicNotification.import_snapshot(source.channel, [])
    state_tests._reconcile_topic_preferences()
    api.project_id = account.project_id
    api.user_uuid = account.owner_user_uuid
    zulip = mock.Mock()
    monkeypatch.setattr(client, "ZulipClient", mock.Mock(return_value=zulip))
    return api, account, source, zulip


def _change(pair, mode):
    api, _account, source, _zulip = pair
    return api.post(
        f"/v1/messenger/stream_topics/{source.topic.uuid}/actions/notifications/invoke",
        json={"notification_mode": mode},
    )


def _mode(pair):
    api, _account, source, _zulip = pair
    response = api.get(f"/v1/messenger/stream_topics/{source.topic.uuid}")
    assert response.status_code == 200, response.text
    return response.json()["notification_mode"]


def _intents(pair):
    with contexts.Context().session_manager() as session:
        return session.execute(
            "SELECT * FROM zulip_outgoing_intents WHERE external_account_uuid=%s AND entity_type IN ('stream_binding','topic_binding') ORDER BY id",
            (pair[1].uuid,),
        ).fetchall()


def _event(pair, policy, last_updated=100):
    _api, account, _source, _zulip = pair
    content = models.ImportChannelsContent(
        event={
            "type": "user_topic",
            "stream_id": 42,
            "topic_name": "gEnErAl",
            "visibility_policy": policy,
            "last_updated": last_updated,
        },
    )
    with contexts.Context().session_manager():
        assert content.apply_event(account)
    state_tests._reconcile_topic_preferences()


@pytest.mark.parametrize(
    "policy,mode", [(0, "default"), (1, "mute"), (2, "unmute"), (3, "follow")]
)
@pytest.mark.parametrize("stream_mode", ["all_messages", "mentions_only", "muted"])
def test_zulip_notification_reaches_workspace_without_an_outgoing_echo(
    notification_pair, db, policy, mode, stream_mode
):
    pair = notification_pair
    db.execute(
        "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s",
        (stream_mode, pair[1].project_id, pair[1].owner_user_uuid),
    )
    _event(pair, policy)
    assert _mode(pair) == mode
    assert _intents(pair) == []
    pair[3].update_topic_notifications.assert_not_called()


def test_native_stream_notification_does_not_create_a_provider_task(
    notification_pair, db
):
    pair = notification_pair
    db.execute(
        "UPDATE messenger_streams SET source_name='native',provider_uuid=NULL,provider_identity=NULL WHERE project_id=%s AND uuid=%s",
        (pair[1].project_id, pair[2].channel.stream_uuid),
    )
    assert _change(pair, "follow").status_code == 200
    assert _mode(pair) == "follow"
    assert _intents(pair) == []


def test_same_user_other_project_is_not_a_notification_target(
    notification_pair, message_sources
):
    pair = notification_pair
    other, _source = message_sources(sender_id=8)
    other.properties["owner_user_uuid"].set_value_force(pair[1].owner_user_uuid)
    other.update()
    assert _change(pair, "follow").status_code == 200
    assert {task["external_account_uuid"] for task in _intents(pair)} == {pair[1].uuid}
    assert (
        models.Task.objects.get_all(
            filters={"external_account_uuid": filters.EQ(other.uuid)}
        )
        == []
    )


def _stream_mode(pair):
    response = pair[0].get(f"/v1/messenger/streams/{pair[2].channel.stream_uuid}")
    assert response.status_code == 200, response.text
    return response.json()["notification_mode"]


def _change_stream(pair, mode):
    return pair[0].post(
        f"/v1/messenger/streams/{pair[2].channel.stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": mode},
    )


def _stream_event(pair, muted):
    content = models.ImportChannelsContent(
        event={
            "type": "subscription",
            "op": "update",
            "stream_id": 42,
            "property": "is_muted",
            "value": muted,
        },
    )
    with contexts.Context().session_manager():
        assert content.apply_event(pair[1])
        for state in models.ZulipStreamBindingState.objects.get_all(
            filters={"project_id": filters.EQ(pair[1].project_id)}
        ):
            state.do()


@pytest.mark.parametrize(
    "muted,mode", [(True, "mentions_only"), (False, "all_messages")]
)
def test_zulip_stream_changes_reach_workspace_without_outgoing_tasks(
    notification_pair, muted, mode
):
    pair = notification_pair
    _stream_event(pair, muted)
    assert _stream_mode(pair) == mode
    assert _intents(pair) == []


def test_topic_policy_reset_uses_the_existing_incoming_path(notification_pair):
    _event(notification_pair, 1)
    assert _mode(notification_pair) == "mute"
    _event(notification_pair, 0, last_updated=101)
    assert _mode(notification_pair) == "default"


def test_native_topic_notification_scope_migration_is_reversible(notification_pair, db):
    pair = notification_pair
    db.execute(
        "UPDATE messenger_topics SET source_name='native',provider_uuid=NULL,provider_identity=NULL WHERE project_id=%s AND uuid=%s",
        (pair[1].project_id, pair[2].topic.uuid),
    )
    name = "0259-Preserve-bidirectional-Zulip-topic-notification-changes-0e2e4b.py"
    engine = conftest.DataMigration(name)
    engine.rollback_migration(name)
    try:
        _event(pair, 1)
        assert _mode(pair) == "default"
    finally:
        engine.apply_migration(name)
    _event(pair, 3, last_updated=101)
    assert _mode(pair) == "follow"
