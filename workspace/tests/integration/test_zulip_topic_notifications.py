# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Personal notification changes through HTTP, durable tasks and reconciliation."""

import concurrent.futures
import datetime
import threading
from unittest import mock
import uuid

import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.tests.integration import test_zulip_channels as channel_tests
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


def _tasks(pair):
    return models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(pair[1].uuid),
            "content": filters.JSONFields({"kind": "update_notifications"}),
        },
        order_by={"created_at": "asc"},
    )


def _run(task):
    # Restore the payload from storage, as a newly started worker does.
    with contexts.Context().session_manager():
        models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)}).execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


def _event(pair, policy, observed_at=None, event_id=1, last_updated=100):
    _api, account, _source, _zulip = pair
    content = models.ImportChannelsContent(
        observed_at=observed_at or datetime.datetime.now(datetime.timezone.utc),
        event_id=event_id,
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


@pytest.mark.parametrize("stream_mode", ["all_messages", "mentions_only", "muted"])
@pytest.mark.parametrize(
    "mode,policy", [("default", 0), ("mute", 1), ("unmute", 2), ("follow", 3)]
)
def test_workspace_notification_survives_reconciliation_and_reaches_zulip(
    notification_pair, db, stream_mode, mode, policy
):
    pair = notification_pair
    _api, account, source, zulip = pair
    db.execute(
        "UPDATE messenger_stream_bindings SET notification_mode=%s WHERE project_id=%s AND user_uuid=%s",
        (stream_mode, account.project_id, account.owner_user_uuid),
    )
    response = _change(pair, mode)
    if mode == "unmute" and stream_mode != "muted":
        assert response.status_code == 400
        assert _tasks(pair) == []
        return
    assert response.status_code == 200, response.text
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == mode
    (task,) = _tasks(pair)
    assert task.queue == "realtime"
    assert _run(task).status == "done"
    zulip.update_topic_notifications.assert_called_once_with(42, "General", policy)
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == mode
    assert _run(task).status == "done"
    # Running an already acknowledged setting has no new remote write.
    assert zulip.update_topic_notifications.call_count == 1


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
    assert _tasks(pair) == []
    pair[3].update_topic_notifications.assert_not_called()


def test_pending_local_choice_is_not_reverted_by_a_later_catalog_snapshot(
    notification_pair,
):
    pair = notification_pair
    assert _change(pair, "follow").status_code == 200
    with contexts.Context().session_manager():
        models.ZulipTopicNotification.import_snapshot(pair[2].channel, [])
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == "follow"
    assert _run(_tasks(pair)[0]).status == "done"
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == "follow"


def test_new_local_choice_skips_old_tasks_after_worker_restart(notification_pair):
    pair = notification_pair
    assert _change(pair, "mute").status_code == 200
    assert _change(pair, "follow").status_code == 200
    first, second = _tasks(pair)
    assert _run(first).status == "done"
    pair[3].update_topic_notifications.assert_not_called()
    assert _run(second).status == "done"
    pair[3].update_topic_notifications.assert_called_once_with(42, "General", 3)
    assert _mode(pair) == "follow"


def test_new_zulip_change_supersedes_unsent_workspace_choice(notification_pair):
    pair = notification_pair
    assert _change(pair, "follow").status_code == 200
    _event(pair, 1)
    assert _mode(pair) == "mute"
    assert _run(_tasks(pair)[0]).status == "done"
    pair[3].update_topic_notifications.assert_not_called()


def test_old_event_and_old_snapshot_cannot_replace_a_new_local_choice(
    notification_pair,
):
    pair = notification_pair
    old = datetime.datetime.now(datetime.timezone.utc)
    assert _change(pair, "follow").status_code == 200
    _event(pair, 1, old)
    assert _mode(pair) == "follow"
    assert _run(_tasks(pair)[0]).status == "done"
    with contexts.Context().session_manager():
        models.ZulipTopicNotification.import_snapshot(pair[2].channel, [], old)
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == "follow"


def test_default_event_leaves_a_fence_against_stale_non_default_events(
    notification_pair,
):
    pair = notification_pair
    at = datetime.datetime.now(datetime.timezone.utc)
    _event(pair, 3, at, 10, 100)
    _event(pair, 0, at + datetime.timedelta(seconds=1), 11, 101)
    _event(pair, 1, at, 9, 99)
    assert _mode(pair) == "default"
    (row,) = models.ZulipTopicNotification.objects.get_all(
        filters={"channel_uuid": filters.EQ(pair[2].channel.uuid)}
    )
    assert row.visibility_policy == 0
    assert row.last_updated == 101


def test_equal_timestamp_events_use_event_sequence_and_are_idempotent(
    notification_pair,
):
    pair = notification_pair
    at = datetime.datetime.now(datetime.timezone.utc)
    _event(pair, 1, at, 10, 100)
    _event(pair, 3, at, 11, 100)
    _event(pair, 1, at, 10, 100)
    _event(pair, 3, at, 11, 100)
    assert _mode(pair) == "follow"


def test_timeout_retries_the_durable_intent_without_reverting_the_ui(notification_pair):
    pair = notification_pair
    assert _change(pair, "mute").status_code == 200
    pair[3].update_topic_notifications.side_effect = requests.Timeout(
        "Provider response lost"
    )
    task = _run(_tasks(pair)[0])
    assert task.status == "retry"
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == "mute"
    pair[3].update_topic_notifications.side_effect = None
    assert _run(task).status == "done"
    state_tests._reconcile_topic_preferences()
    assert _mode(pair) == "mute"


@pytest.mark.parametrize("revocation", ["disabled", "owner", "server"])
def test_revoked_or_rebound_account_cannot_send_an_old_intent(
    notification_pair, db, revocation
):
    pair = notification_pair
    assert _change(pair, "follow").status_code == 200
    account = pair[1]
    if revocation == "disabled":
        account.disabled = True
    elif revocation == "owner":
        other = uuid.uuid4()
        state_tests.conftest.seed_workspace_user(
            db, other, f"notification-owner-{other}"
        )
        account.properties["owner_user_uuid"].set_value_force(other)
    else:
        account.settings = {
            **account.settings,
            "server_url": "https://other.example.com",
        }
    account.update()
    assert _run(_tasks(pair)[0]).status == "cancel"
    pair[3].update_topic_notifications.assert_not_called()


def test_native_stream_notification_does_not_create_a_provider_task(
    notification_pair, db
):
    pair = notification_pair
    db.execute(
        "UPDATE messenger_streams SET source_name='native',source='{\"kind\":\"native\"}' WHERE project_id=%s AND uuid=%s",
        (pair[1].project_id, pair[2].channel.stream_uuid),
    )
    assert _change(pair, "follow").status_code == 200
    assert _mode(pair) == "follow"
    assert _tasks(pair) == []


def test_same_user_other_project_is_not_a_notification_target(
    notification_pair, message_sources
):
    pair = notification_pair
    other, _source = message_sources(sender_id=8)
    other.properties["owner_user_uuid"].set_value_force(pair[1].owner_user_uuid)
    other.update()
    assert _change(pair, "follow").status_code == 200
    assert {task.external_account_uuid for task in _tasks(pair)} == {pair[1].uuid}
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


def _stream_event(pair, muted, observed_at=None, event_id=1):
    content = models.ImportChannelsContent(
        observed_at=observed_at or datetime.datetime.now(datetime.timezone.utc),
        event_id=event_id,
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
    "mode,muted", [("all_messages", False), ("mentions_only", True), ("muted", True)]
)
def test_workspace_stream_notification_reaches_zulip_and_survives_reconciliation(
    notification_pair, mode, muted
):
    pair = notification_pair
    assert _change_stream(pair, mode).status_code == 200
    with contexts.Context().session_manager():
        for state in models.ZulipStreamBindingState.objects.get_all(
            filters={"project_id": filters.EQ(pair[1].project_id)}
        ):
            state.do()
    assert _stream_mode(pair) == mode
    (task,) = _tasks(pair)
    assert task.content.topic_uuid is None
    assert _run(task).status == "done"
    pair[3].update_stream_notifications.assert_called_once_with(42, muted)
    _stream_event(pair, muted)
    assert _stream_mode(pair) == mode
    assert _run(task).status == "done"
    assert pair[3].update_stream_notifications.call_count == 1


def test_new_zulip_stream_change_supersedes_unsent_workspace_choice(notification_pair):
    pair = notification_pair
    assert _change_stream(pair, "all_messages").status_code == 200
    _stream_event(pair, True)
    assert _stream_mode(pair) == "mentions_only"
    assert _run(_tasks(pair)[0]).status == "done"
    pair[3].update_stream_notifications.assert_not_called()


def test_old_zulip_stream_event_does_not_replace_a_newer_local_choice(
    notification_pair,
):
    pair = notification_pair
    at = datetime.datetime.now(datetime.timezone.utc)
    assert _change_stream(pair, "muted").status_code == 200
    _stream_event(pair, False, at)
    assert _stream_mode(pair) == "muted"
    assert _run(_tasks(pair)[0]).status == "done"
    _stream_event(pair, False, at)
    assert _stream_mode(pair) == "muted"


def test_zulip_stream_events_with_equal_timestamps_follow_event_sequence(
    notification_pair,
):
    pair = notification_pair
    at = datetime.datetime.now(datetime.timezone.utc)
    _stream_event(pair, True, at, 10)
    _stream_event(pair, False, at, 11)
    _stream_event(pair, True, at, 10)
    assert _stream_mode(pair) == "all_messages"
    assert _tasks(pair) == []


def test_stream_and_topic_pending_choices_do_not_cancel_each_other(notification_pair):
    pair = notification_pair
    assert _change_stream(pair, "muted").status_code == 200
    assert _change(pair, "unmute").status_code == 200
    stream_task, topic_task = _tasks(pair)
    assert _run(stream_task).status == "done"
    assert _run(topic_task).status == "done"
    _stream_event(pair, True)
    _event(pair, 2)
    assert _stream_mode(pair) == "muted"
    assert _mode(pair) == "unmute"
    pair[3].update_stream_notifications.assert_called_once_with(42, True)
    pair[3].update_topic_notifications.assert_called_once_with(42, "General", 2)


def test_topic_notification_enqueue_failure_rolls_back_the_http_change(
    notification_pair, monkeypatch
):
    pair = notification_pair
    with monkeypatch.context() as patched:
        patched.setattr(
            models.Task,
            "insert",
            mock.Mock(side_effect=RuntimeError("Cannot enqueue setting")),
        )
        response = _change(pair, "follow")
    assert response.status_code == 500
    assert _mode(pair) == "default"
    assert _tasks(pair) == []


def test_empty_topic_uses_the_raw_empty_name_at_the_provider(notification_pair):
    pair = notification_pair
    source = pair[2]
    empty = models.ZulipTopic(
        uuid=uuid.uuid5(source.channel.stream_uuid, ""),
        stream_id=42,
        name="General Topic",
        max_id=42,
    )
    empty.insert()
    source.topic = empty
    source.update()
    state_tests._prepare(pair[1])
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    assert _change(pair, "follow").status_code == 200
    assert _run(_tasks(pair)[0]).status == "done"
    pair[3].update_topic_notifications.assert_called_once_with(42, "", 3)


def test_outgoing_notification_targets_only_its_owner(
    notification_pair, message_sources
):
    pair = notification_pair
    other, _source = message_sources(project_id=pair[1].project_id, sender_id=8)
    other.settings = {
        **other.settings,
        "email": "other@example.com",
        "api_key": "other-test-key",
    }
    other.status = "active"
    other.update()
    state_tests._prepare(other)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    assert _change(pair, "follow").status_code == 200
    assert _run(_tasks(pair)[0]).status == "done"
    assert (
        models.Task.objects.get_all(
            filters={"external_account_uuid": filters.EQ(other.uuid)}
        )
        == []
    )
    assert (
        client.ZulipClient.call_args.args[0].owner_user_uuid == pair[1].owner_user_uuid
    )


def test_stale_reconciliation_object_cannot_replace_a_new_local_choice(
    notification_pair,
):
    pair = notification_pair
    with contexts.Context().session_manager():
        models.ZulipTopicNotification.record(
            pair[2].channel,
            "General",
            1,
            100,
            datetime.datetime.now(datetime.timezone.utc),
            1,
        )
    (stale,) = models.ZulipTopicNotificationState.objects.get_all(
        filters={"project_id": filters.EQ(pair[1].project_id)}
    )
    assert _change(pair, "follow").status_code == 200
    with contexts.Context().session_manager():
        stale.do()
    assert _mode(pair) == "follow"


def test_workspace_created_topic_in_a_zulip_stream_also_syncs_its_notifications(
    notification_pair, db
):
    pair = notification_pair
    db.execute(
        "UPDATE messenger_topics SET source_name='native',source='{\"kind\":\"native\"}' WHERE project_id=%s AND uuid=%s",
        (pair[1].project_id, pair[2].topic.uuid),
    )
    assert _change(pair, "follow").status_code == 200
    assert _run(_tasks(pair)[0]).status == "done"
    pair[3].update_topic_notifications.assert_called_once_with(42, "General", 3)
    _event(pair, 1)
    assert _mode(pair) == "mute"


@pytest.mark.parametrize("mode", ["all_messages", "mentions_only", "muted"])
def test_catalog_refresh_preserves_pending_and_acknowledged_stream_choices(
    notification_pair, mode
):
    pair = notification_pair
    pair[3].get_topic_notifications.return_value = []
    pair[3].get_channels.return_value = [
        channel_tests._channel_data(notification_mode="all_messages")
    ]
    assert _change_stream(pair, mode).status_code == 200
    content = models.ImportChannelsContent()
    with contexts.Context().session_manager():
        content.execute(
            models.Task(
                project_id=pair[1].project_id,
                external_account_uuid=pair[1].uuid,
                content=content,
            )
        )
        for state in models.ZulipStreamBindingState.objects.get_all(
            filters={"project_id": filters.EQ(pair[1].project_id)}
        ):
            state.do()
    assert _stream_mode(pair) == mode
    assert _run(_tasks(pair)[0]).status == "done"
    pair[3].get_channels.return_value = [
        channel_tests._channel_data(
            notification_mode="all_messages"
            if mode == "all_messages"
            else "mentions_only"
        )
    ]
    with contexts.Context().session_manager():
        content.execute(
            models.Task(
                project_id=pair[1].project_id,
                external_account_uuid=pair[1].uuid,
                content=content,
            )
        )
        for state in models.ZulipStreamBindingState.objects.get_all(
            filters={"project_id": filters.EQ(pair[1].project_id)}
        ):
            state.do()
    assert _stream_mode(pair) == mode


def test_http_setting_change_and_in_flight_worker_keep_account_first_lock_order(
    notification_pair,
):
    pair = notification_pair
    assert _change(pair, "mute").status_code == 200
    started, release = threading.Event(), threading.Event()

    def send(*_args):
        started.set()
        assert release.wait(5)

    pair[3].update_topic_notifications.side_effect = send
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(_run, _tasks(pair)[0])
        try:
            assert started.wait(5)
            changed = pool.submit(_change, pair, "follow")
        finally:
            release.set()
        assert worker.result(timeout=10).status == "done"
        assert changed.result(timeout=10).status_code == 200
    pair[3].update_topic_notifications.side_effect = None
    assert _run(_tasks(pair)[-1]).status == "done"
    assert _mode(pair) == "follow"
