# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import datetime
import inspect
import json
from unittest import mock
import uuid

import pytest
import requests
from restalchemy.common import exceptions
from restalchemy.dm import types_dynamic

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import models as messenger_models


@pytest.fixture(autouse=True)
def _task_storage(monkeypatch):
    monkeypatch.setattr(models.utils, "savepoint", contextlib.nullcontext)
    update = mock.Mock()
    monkeypatch.setattr(models.Task, "update", update)
    monkeypatch.setattr(models.contexts.Context, "get_session", mock.Mock())
    return update


def _task():
    return models.Task(
        project_id=uuid.uuid4(),
        external_account_uuid=uuid.uuid4(),
        content=models.InitializeConnectionContent(),
    )


def test_task_is_concrete_with_priority_and_retry_status():
    task = _task()
    assert not inspect.isabstract(models.Task)
    assert task.priority == models.TASK_PRIORITY_IMPORT_HISTORY == 10_000
    assert task.status == "new"
    assert task.error_description is None
    assert task.not_before_at is None
    assert task.next_retry_at is None
    assert task.execution_time is None
    assert task.queue_wait_time is None
    assert [status.value for status in models.TaskStatus] == [
        "new",
        "waiting",
        "retry",
        "error",
        "done",
        "cancel",
    ]


def test_account_bound_task_requires_project():
    with pytest.raises(exceptions.ValidationErrorException):
        models.Task(
            external_account_uuid=uuid.uuid4(),
            content=models.InitializeConnectionContent(),
        )


@pytest.mark.parametrize(
    "status,delay,expected",
    [
        (None, None, "new"),
        (None, -1, "new"),
        (None, 0, "new"),
        (None, 1, "waiting"),
        ("new", 1, "waiting"),
        ("waiting", 1, "waiting"),
        ("retry", 1, "retry"),
        ("error", 1, "error"),
        ("done", 1, "done"),
        ("cancel", 1, "cancel"),
    ],
)
def test_task_initial_status_respects_schedule(status, delay, expected):
    now = datetime.datetime.now(datetime.timezone.utc)
    values = {}
    if status is not None:
        values["status"] = status
    if delay is not None:
        values["not_before_at"] = now + datetime.timedelta(seconds=delay)
    with mock.patch.object(models, "datetime") as clock:
        clock.datetime.now.return_value = now
        task = models.Task(
            project_id=uuid.uuid4(),
            external_account_uuid=uuid.uuid4(),
            content=models.InitializeConnectionContent(),
            **values,
        )
    assert task.status == expected


def test_task_content_is_fixed_but_schedule_can_change():
    task = _task()
    with pytest.raises(exceptions.ReadOnlyProperty):
        task.content = models.ImportHistoryContent(
            first_message_id=1, last_message_id=2
        )
    scheduled_at = datetime.datetime.now(datetime.timezone.utc)
    task.priority = models.TASK_PRIORITY_INITIALIZE_CONNECTION
    task.not_before_at = scheduled_at
    assert task.priority == models.TASK_PRIORITY_INITIALIZE_CONNECTION
    assert task.not_before_at == scheduled_at


def test_history_content_parameters_cannot_be_changed():
    history = models.ImportHistoryContent(first_message_id=10, last_message_id=20)
    for name, value in {"first_message_id": 11, "last_message_id": 21}.items():
        with pytest.raises(exceptions.ReadOnlyProperty):
            setattr(history, name, value)


def test_legacy_history_content_remains_readable_through_kind_selector():
    content = models.TASK_CONTENT_TYPE.from_simple_type(
        {
            "kind": "import_history",
            "first_message_id": 10,
            "last_message_id": 5009,
        }
    )
    assert isinstance(content, models.ImportHistoryContent)
    assert content.last_message_id == 5009
    assert models.TASK_CONTENT_TYPE.to_simple_type(content)["kind"] == "import_history"


def test_unknown_content_kind_is_rejected():
    with pytest.raises(types_dynamic.UnknownType):
        models.TASK_CONTENT_TYPE.from_simple_type({"kind": "unknown"})


@pytest.mark.parametrize("first,last", [(1, 5001), (10, 9)])
def test_history_content_rejects_unbounded_or_reversed_range(first, last):
    with pytest.raises(exceptions.ValidationErrorException):
        models.ImportHistoryContent(
            first_message_id=first,
            last_message_id=last,
        )


def test_client_checks_credentials():
    session = mock.Mock()
    session.request.return_value.json.return_value = {"result": "success", "user_id": 7}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(
            external_models.ExternalAccount(
                project_id=uuid.uuid4(),
                owner_user_uuid=uuid.uuid4(),
                provider="zulip",
                settings={
                    "server_url": "https://zulip.example.com/",
                    "email": "user@example.com",
                    "api_key": "test-key",
                },
            )
        )
        assert zulip.check_connection()["user_id"] == 7
        zulip.close()
    assert session.auth == ("user@example.com", "test-key")
    assert session.request.call_args_list[0].args == (
        "GET",
        "https://zulip.example.com/api/v1/users/me",
    )
    session.request.assert_called_once()
    session.close.assert_called_once()


def test_client_gets_channels_including_archived():
    session = mock.Mock()
    channels = [
        {
            "stream_id": 42,
            "is_archived": True,
            "is_muted": False,
            "desktop_notifications": None,
            "push_notifications": False,
        }
    ]
    session.request.return_value.json.side_effect = [
        {
            "result": "success",
            "queue_id": "subscriptions:1",
            "subscriptions": channels,
            "user_settings": {
                "enable_stream_desktop_notifications": True,
                "enable_stream_push_notifications": False,
            },
            "streams": [{"stream_id": 99}],
        },
        {"result": "success"},
    ]
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=uuid.uuid4(),
        provider="zulip",
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "user@example.com",
            "api_key": "test-key",
        },
    )
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        assert zulip.get_channels() == [
            {**channels[0], "notification_mode": "all_messages"}
        ]
        zulip.close()
    assert session.request.call_args_list[0].args == (
        "POST",
        "https://zulip.example.com/api/v1/register",
    )
    request = session.request.call_args_list[0].kwargs["data"]
    assert json.loads(request["fetch_event_types"]) == ["subscription"]
    assert json.loads(request["client_capabilities"]) == {
        "notification_settings_null": True,
        "user_settings_object": True,
        "archived_channels": True,
    }
    assert request["event_types"] == "[]"
    assert session.request.call_args_list[1].args == (
        "DELETE",
        "https://zulip.example.com/api/v1/events",
    )
    assert session.request.call_args_list[1].kwargs["data"] == {
        "queue_id": "subscriptions:1"
    }
    session.close.assert_called_once()


@pytest.mark.parametrize(
    "muted,desktop,push,default_desktop,default_push,expected",
    [
        (True, True, True, True, True, "mentions_only"),
        (True, None, None, False, False, "mentions_only"),
        (False, True, False, False, False, "all_messages"),
        (False, False, True, False, False, "all_messages"),
        (False, None, None, True, False, "all_messages"),
        (False, None, None, False, True, "all_messages"),
        (False, False, False, True, True, "all_messages"),
        (False, None, None, False, False, "all_messages"),
    ],
)
def test_subscription_mode_depends_on_muting_not_delivery_settings(
    muted, desktop, push, default_desktop, default_push, expected
):
    session = mock.Mock()
    session.request.return_value.json.side_effect = [
        {
            "result": "success",
            "queue_id": "subscriptions:1",
            "subscriptions": [
                {
                    "is_muted": muted,
                    "desktop_notifications": desktop,
                    "push_notifications": push,
                }
            ],
            "user_settings": {
                "enable_stream_desktop_notifications": default_desktop,
                "enable_stream_push_notifications": default_push,
            },
        },
        {"result": "success"},
    ]
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(
            mock.Mock(
                settings={
                    "server_url": "https://zulip.example.com",
                    "email": "owner@example.com",
                    "api_key": "test-key",
                }
            )
        )
        assert zulip.get_channels()[0]["notification_mode"] == expected
        zulip.close()


def test_subscription_queue_is_deleted_when_snapshot_processing_fails():
    session = mock.Mock()
    session.request.return_value.json.side_effect = [
        {"result": "success", "queue_id": "subscriptions:1", "subscriptions": [{}]},
        {"result": "success"},
    ]
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(
            mock.Mock(
                settings={
                    "server_url": "https://zulip.example.com",
                    "email": "owner@example.com",
                    "api_key": "test-key",
                }
            )
        )
        with pytest.raises(KeyError, match="is_muted"):
            zulip.get_channels()
        zulip.close()
    assert session.request.call_args_list[-1].kwargs["data"] == {
        "queue_id": "subscriptions:1"
    }


@pytest.mark.parametrize(
    "topics",
    [
        [],
        [
            {
                "stream_id": 24,
                "topic_name": "",
                "visibility_policy": 3,
                "last_updated": 42,
            },
            {
                "stream_id": 24,
                "topic_name": "Planning",
                "visibility_policy": 1,
                "last_updated": 43,
            },
        ],
    ],
)
def test_client_gets_personal_topic_notifications_with_raw_empty_names(topics):
    session = mock.Mock()
    session.request.return_value.json.side_effect = [
        {"result": "success", "queue_id": "topics:1", "user_topics": topics},
        {"result": "success"},
    ]
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "agent@example.com",
            "api_key": "test-key",
        }
    )
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        assert zulip.get_topic_notifications() == topics
        zulip.close()
    request = session.request.call_args_list[0]
    assert request.args == ("POST", "https://zulip.example.com/api/v1/register")
    assert json.loads(request.kwargs["data"]["fetch_event_types"]) == ["user_topic"]
    assert json.loads(request.kwargs["data"]["client_capabilities"])["empty_topic_name"]
    assert session.request.call_args_list[1].args == (
        "DELETE",
        "https://zulip.example.com/api/v1/events",
    )
    session.close.assert_called_once()


def test_topic_notification_queue_is_deleted_when_snapshot_is_missing():
    session = mock.Mock()
    session.request.return_value.json.side_effect = [
        {"result": "success", "queue_id": "topics:1"},
        {"result": "success"},
    ]
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "agent@example.com",
            "api_key": "test-key",
        }
    )
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        with pytest.raises(KeyError, match="user_topics"):
            zulip.get_topic_notifications()
        zulip.close()
    assert session.request.call_args_list[1].kwargs["data"] == {"queue_id": "topics:1"}


def test_client_gets_active_inactive_and_system_users():
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "user@example.com",
            "api_key": "test-key",
        }
    )
    session = mock.Mock()
    active = {"user_id": 9, "full_name": "User", "email": "user@example.com"}
    inactive = {"user_id": 8, "full_name": "Inactive", "email": "old@example.com"}
    bot = {
        "user_id": 6,
        "full_name": "Notification Bot",
        "email": "notification-bot@zulip.com",
        "is_active": True,
        "is_system_bot": True,
    }
    session.request.return_value.json.side_effect = [
        {
            "result": "success",
            "queue_id": "test-queue",
            "realm_users": [active],
            "realm_non_active_users": [inactive],
            "cross_realm_bots": [bot],
        },
        {"result": "success"},
    ]
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        assert zulip.get_users() == [
            {**active, "is_active": True},
            {**inactive, "is_active": False},
            bot,
        ]
        zulip.close()
    assert session.request.call_args_list == [
        mock.call(
            "POST",
            "https://zulip.example.com/api/v1/register",
            data={"event_types": "[]", "fetch_event_types": '["realm_user"]'},
            timeout=(5, 30),
        ),
        mock.call(
            "DELETE",
            "https://zulip.example.com/api/v1/events",
            data={"queue_id": "test-queue"},
            timeout=(5, 30),
        ),
    ]
    session.close.assert_called_once_with()


def test_user_fetch_deletes_the_event_queue_when_processing_fails():
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com",
            "email": "user@example.com",
            "api_key": "test-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.side_effect = [
        {"result": "success", "queue_id": "test-queue"},
        {"result": "success"},
    ]
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            with pytest.raises(KeyError, match="realm_users"):
                zulip.get_users()
        finally:
            zulip.close()
    assert session.request.call_count == 2
    session.request.assert_called_with(
        "DELETE",
        "https://zulip.example.com/api/v1/events",
        data={"queue_id": "test-queue"},
        timeout=(5, 30),
    )


@pytest.mark.parametrize(
    "messages,expected", [([], 0), ([{"id": 1}], 1), ([{"id": 10005}], 10005)]
)
def test_client_gets_last_available_message_id(messages, expected):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "user@example.com",
            "api_key": "test-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {
        "result": "success",
        "messages": messages,
    }
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        assert zulip.get_last_message_id() == expected
        zulip.close()
    session.request.assert_called_once_with(
        "GET",
        "https://zulip.example.com/api/v1/messages",
        params={
            "anchor": "newest",
            "num_before": 1,
            "num_after": 0,
            "apply_markdown": "false",
        },
        timeout=(5, 30),
    )
    session.close.assert_called_once_with()


@pytest.mark.parametrize(
    "offset,expected", [(None, None), (-1, None), (0, 42), (1, 42)]
)
def test_client_finds_backfill_boundary_by_exact_date(offset, expected):
    since = datetime.datetime(2026, 9, 19, 7, 0, 46, tzinfo=datetime.timezone.utc)
    messages = (
        [] if offset is None else [{"id": 42, "timestamp": since.timestamp() + offset}]
    )
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "user@example.com",
            "api_key": "test-key",
        }
    )
    session = mock.Mock()
    session.request.return_value.json.return_value = {
        "result": "success",
        "messages": messages,
    }
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        try:
            assert zulip.get_first_message_id_since(since) == expected
        finally:
            zulip.close()
    session.request.assert_called_once_with(
        "GET",
        "https://zulip.example.com/api/v1/messages",
        params={
            "anchor": "date",
            "anchor_date": since.isoformat(),
            "num_before": 0,
            "num_after": 0,
            "apply_markdown": "false",
        },
        timeout=(5, 30),
    )
    session.close.assert_called_once_with()


@pytest.mark.parametrize(
    "last_message_id,expected_ranges",
    [
        (0, []),
        (1, [(1, 1)]),
        (100, [(1, 100)]),
        (101, [(101, 101), (1, 100)]),
        (205, [(201, 205), (101, 200), (1, 100)]),
    ],
)
def test_full_backfill_creates_user_channel_and_bounded_history_tasks(
    last_message_id, expected_ranges
):
    task = models.Task(
        project_id=uuid.uuid4(),
        external_account_uuid=uuid.uuid4(),
        content=models.TASK_CONTENT_TYPE.from_simple_type(
            {"kind": "full_backfill", "last_message_id": last_message_id}
        ),
    )
    with (
        mock.patch.object(external_models.ExternalAccount, "objects"),
        mock.patch.object(client, "ZulipClient") as constructor,
        mock.patch.object(models.Task, "insert", autospec=True) as insert,
    ):
        task.content.execute(task)
    children = [call.args[0] for call in insert.call_args_list]
    assert len(children) == len(expected_ranges) + 2
    assert all(child.project_id == task.project_id for child in children)
    assert all(
        child.external_account_uuid == task.external_account_uuid for child in children
    )
    assert all(
        child.status == "new" and child.not_before_at is None for child in children
    )
    assert isinstance(children[0].content, models.ImportUsersContent)
    assert children[0].priority == models.TASK_PRIORITY_IMPORT_USERS
    assert isinstance(children[1].content, models.ImportChannelsContent)
    assert children[1].priority == models.TASK_PRIORITY_IMPORT_CHANNELS == 450_000
    assert [
        (child.content.first_message_id, child.content.last_message_id)
        for child in children[2:]
    ] == expected_ranges
    assert all(
        isinstance(child.content, models.ImportHistoryContent) for child in children[2:]
    )
    assert [child.priority for child in children[2:]] == list(
        range(10_000, 10_000 - len(expected_ranges), -1)
    )
    constructor.assert_not_called()


def test_history_priorities_stop_decreasing_at_the_minimum(monkeypatch):
    assert models.TASK_PRIORITY_IMPORT_HISTORY_MIN == 10
    monkeypatch.setattr(models, "TASK_PRIORITY_IMPORT_HISTORY", 12)
    task = _task()
    with (
        mock.patch.object(external_models.ExternalAccount, "objects"),
        mock.patch.object(models.Task, "insert", autospec=True) as insert,
    ):
        models.FullBackfillContent(last_message_id=401).execute(task)
    history = [call.args[0] for call in insert.call_args_list[2:]]
    assert [child.priority for child in history] == [12, 11, 10, 10, 10]
    assert [child.content.first_message_id for child in history] == [
        401,
        301,
        201,
        101,
        1,
    ]
    assert all(
        newer.created_at < older.created_at
        for newer, older in zip(history, history[1:])
    )


def test_other_task_priorities_are_above_history():
    assert all(
        priority > models.TASK_PRIORITY_IMPORT_HISTORY
        for priority in (
            models.TASK_PRIORITY_IMPORT_CHANNELS,
            models.TASK_PRIORITY_IMPORT_USERS,
            models.TASK_PRIORITY_FULL_BACKFILL,
            models.TASK_PRIORITY_INITIALIZE_CONNECTION,
            models.TASK_PRIORITY_REALTIME_MESSAGE,
        )
    )


def test_task_executes_connection_content():
    task = _task()
    account = mock.Mock(
        settings={
            "server_url": "https://ZULIP.example.com/path",
            "api_key": "test-key",
        },
        properties={"external_account_id": mock.Mock()},
        external_account_id=None,
        disabled=False,
        get_host=mock.Mock(return_value="zulip.example.com"),
    )
    zulip = mock.Mock()
    zulip.check_connection.return_value = {
        "user_id": 7,
        "full_name": "Owner",
        "email": "owner@example.com",
    }
    with (
        mock.patch.object(external_models.ExternalAccount, "objects") as accounts,
        mock.patch.object(client, "ZulipClient", return_value=zulip) as constructor,
        mock.patch.object(models.Task, "insert", autospec=True) as insert,
        mock.patch.object(messenger_models.WorkspaceUser, "objects") as users,
        mock.patch.object(
            messenger_models.WorkspaceUser, "insert", autospec=True
        ) as insert_user,
    ):
        accounts.get_one.return_value = account
        accounts.get_one_or_none.return_value = None
        users.get_one_or_none.return_value = None
        task.execute()
    assert (
        accounts.get_one.call_args.kwargs["filters"]["uuid"].value
        == task.external_account_uuid
    )
    assert (
        accounts.get_one.call_args.kwargs["filters"]["project_id"].value
        == task.project_id
    )
    constructor.assert_called_once_with(account)
    assert zulip.method_calls == [mock.call.check_connection(), mock.call.close()]
    assert task.status == "done"
    account.properties["external_account_id"].set_value_force.assert_called_once_with(
        "zulip.example.com:7"
    )
    account.update.assert_called_once_with()
    account.update_status.assert_called_once_with("backfill")
    (profile,) = insert_user.call_args.args
    assert profile.source == "hidden"
    assert profile.uuid == uuid.uuid5(
        uuid.uuid5(uuid.NAMESPACE_DNS, "zulip.example.com"), "user:7"
    )
    insert.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_task_timings_accumulate_and_exclude_scheduled_retry_delays(fails):
    now = datetime.datetime.now(datetime.timezone.utc)
    task = _task()
    task.execution_time = 1.5
    task.queue_wait_time = 4.0
    task.properties["updated_at"].set_value_force(now - datetime.timedelta(seconds=100))
    task.not_before_at = now - datetime.timedelta(seconds=20)
    task.next_retry_at = now - datetime.timedelta(seconds=10)

    def retry(current_task, error):
        current_task.status = "retry"
        current_task.next_retry_at = now + datetime.timedelta(seconds=3)

    with (
        mock.patch.object(models.time, "monotonic", side_effect=[10.0, 12.5]),
        mock.patch.object(
            models.InitializeConnectionContent,
            "execute",
            side_effect=RuntimeError("Failed") if fails else None,
        ),
        mock.patch.object(models.InitializeConnectionContent, "on_done"),
        mock.patch.object(
            models.InitializeConnectionContent, "on_error", side_effect=retry
        ),
    ):
        task.execute()
    assert task.execution_time == 4.0
    assert task.queue_wait_time == pytest.approx(14.0, abs=0.5)
    assert task.status == ("retry" if fails else "done")


def test_success_calls_task_and_content_hooks_with_the_task(_task_storage):
    task = _task()
    task.error_description = "Previous failure"
    task.next_retry_at = datetime.datetime.now(datetime.timezone.utc)

    def on_done(current_task):
        assert current_task is task
        assert current_task.status == "done"
        assert current_task.error_description is None
        assert current_task.next_retry_at is None

    with (
        mock.patch.object(models.InitializeConnectionContent, "execute") as execute,
        mock.patch.object(
            models.InitializeConnectionContent, "on_done", side_effect=on_done
        ) as content_on_done,
        mock.patch.object(models.InitializeConnectionContent, "on_error") as on_error,
        mock.patch.object(task, "on_done", wraps=task.on_done) as task_on_done,
    ):
        task.execute()
    execute.assert_called_once_with(task)
    task_on_done.assert_called_once_with()
    content_on_done.assert_called_once_with(task)
    on_error.assert_not_called()
    assert task.status == "done"
    assert task.error_description is None
    assert task.next_retry_at is None
    _task_storage.assert_called_once_with()


def test_done_handler_failure_calls_on_error_and_saves_once(_task_storage):
    task = _task()
    task.lease_owner = uuid.uuid4()
    task.lease_expires_at = datetime.datetime.now(datetime.timezone.utc)
    error = RuntimeError("Completion failed")
    with (
        mock.patch.object(
            models.InitializeConnectionContent, "on_done", side_effect=error
        ) as on_done,
        mock.patch.object(models.InitializeConnectionContent, "on_error") as on_error,
        mock.patch.object(task, "on_error", wraps=task.on_error) as task_on_error,
    ):
        task.on_done()

    on_done.assert_called_once_with(task)
    task_on_error.assert_called_once_with(error)
    on_error.assert_called_once_with(task, error)
    assert task.status == "error"
    assert task.error_description == str(error)
    assert task.next_retry_at is None
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    _task_storage.assert_called_once_with()


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"error_description": "Custom error"},
        {
            "status": "retry",
            "next_retry_at": datetime.datetime(
                2026, 10, 18, tzinfo=datetime.timezone.utc
            ),
        },
        {
            "status": "retry",
            "error_description": "Custom retry",
            "next_retry_at": datetime.datetime(
                2026, 10, 19, tzinfo=datetime.timezone.utc
            ),
        },
    ],
)
def test_error_hook_overrides_prepared_terminal_error(overrides, _task_storage):
    now = datetime.datetime(2026, 10, 17, tzinfo=datetime.timezone.utc)
    task = _task()
    task.status = "retry"
    task.error_description = "Previous failure"
    task.next_retry_at = now
    task.lease_owner = uuid.uuid4()
    task.lease_expires_at = now
    error = RuntimeError("Execution failed")

    def on_error(current_task, current_error):
        assert current_task is task
        assert current_error is error
        assert current_task.status == "error"
        assert current_task.error_description == str(error)
        assert current_task.next_retry_at is None
        current_task.update_dm(overrides)

    with (
        mock.patch.object(
            models.InitializeConnectionContent, "execute", side_effect=error
        ) as execute,
        mock.patch.object(
            models.InitializeConnectionContent, "on_error", side_effect=on_error
        ) as content_on_error,
        mock.patch.object(models.InitializeConnectionContent, "on_done") as on_done,
        mock.patch.object(task, "on_error", wraps=task.on_error) as task_on_error,
    ):
        task.execute()
    execute.assert_called_once_with(task)
    task_on_error.assert_called_once_with(error)
    content_on_error.assert_called_once_with(task, error)
    on_done.assert_not_called()
    assert task.status == overrides.get("status", "error")
    assert task.error_description == overrides.get("error_description", str(error))
    assert task.next_retry_at == overrides.get("next_retry_at")
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    _task_storage.assert_called_once_with()


def test_error_handler_failure_is_added_to_the_saved_error(_task_storage):
    task = _task()
    error = RuntimeError("Execution failed")
    handler_error = ValueError("Error handler failed")

    def on_error(current_task, current_error):
        current_task.status = "retry"
        current_task.next_retry_at = datetime.datetime.now(datetime.timezone.utc)
        current_task.error_description = "Partial handler result"
        raise handler_error

    with (
        mock.patch.object(
            models.InitializeConnectionContent, "execute", side_effect=error
        ),
        mock.patch.object(
            models.InitializeConnectionContent, "on_error", side_effect=on_error
        ),
    ):
        task.execute()

    assert task.status == "error"
    assert task.error_description == f"{error}\nError handler failed: {handler_error}"
    assert task.next_retry_at is None
    _task_storage.assert_called_once_with()


@pytest.mark.parametrize(
    "operation,error",
    [
        ("check_connection", requests.HTTPError("Connection failed")),
        ("check_connection", requests.Timeout("Connection timed out")),
        ("close", RuntimeError("Client close failed")),
    ],
)
def test_content_closes_client_and_propagates_execution_errors(operation, error):
    task = _task()
    zulip = mock.Mock()
    zulip.check_connection.return_value = {"user_id": 7}
    getattr(zulip, operation).side_effect = error
    with (
        mock.patch.object(external_models.ExternalAccount, "objects") as accounts,
        mock.patch.object(client, "ZulipClient", return_value=zulip),
    ):
        accounts.get_one.return_value = mock.Mock(
            settings={"server_url": "https://zulip.example.com", "api_key": "test-key"},
            properties={"external_account_id": mock.Mock()},
            get_host=mock.Mock(return_value="zulip.example.com"),
        )
        with pytest.raises(type(error)) as caught:
            task.content.execute(task)
    assert caught.value is error
    zulip.close.assert_called_once()


@pytest.mark.parametrize("first,last", [(10, 20), (10, 10), (1, 5000)])
def test_client_gets_only_messages_in_the_inclusive_range(first, last):
    account = mock.Mock(
        settings={
            "server_url": "https://zulip.example.com/",
            "email": "user@example.com",
            "api_key": "test-key",
        }
    )
    session = mock.Mock()
    messages = [{"id": message_id} for message_id in sorted({first, last})]
    session.request.return_value.json.return_value = {
        "result": "success",
        "messages": [{"id": first - 1}, *messages, {"id": last + 1}],
    }
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(account)
        assert zulip.get_messages(first, last) == messages
        zulip.close()
    session.request.assert_called_once_with(
        "GET",
        "https://zulip.example.com/api/v1/messages",
        params={
            "anchor": first,
            "num_before": 0,
            "num_after": last - first,
            "include_anchor": "true",
            "apply_markdown": "false",
            "allow_empty_topic_name": "true",
        },
        timeout=(5, 30),
    )
    session.close.assert_called_once_with()


def test_history_content_passes_bounds_and_closes_the_client():
    task = models.Task(
        project_id=uuid.uuid4(),
        external_account_uuid=uuid.uuid4(),
        content=models.ImportHistoryContent(first_message_id=1, last_message_id=100),
    )
    account = mock.Mock(
        settings={"server_url": "https://zulip.example.com"},
        get_host=mock.Mock(return_value="zulip.example.com"),
    )
    zulip = mock.Mock()
    zulip.check_connection.return_value = {"user_id": 7}
    zulip.get_messages.return_value = []
    with (
        mock.patch.object(external_models.ExternalAccount, "objects") as accounts,
        mock.patch.object(client, "ZulipClient", return_value=zulip),
    ):
        accounts.get_one.return_value = account
        task.content.execute(task)
    accounts.get_one.assert_called_once()
    selected = accounts.get_one.call_args.kwargs["filters"]
    assert selected["uuid"].value == task.external_account_uuid
    assert selected["project_id"].value == task.project_id
    zulip.get_messages.assert_called_once_with(1, 100)
    zulip.close.assert_called_once_with()
