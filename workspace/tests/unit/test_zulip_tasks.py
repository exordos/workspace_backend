# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import contextlib
import datetime
import inspect
from unittest import mock
import uuid

import pytest
import requests
from restalchemy.common import exceptions
from restalchemy.dm import types_dynamic

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models


@pytest.fixture(autouse=True)
def _task_storage(monkeypatch):
    monkeypatch.setattr(models.utils, "savepoint", contextlib.nullcontext)
    update = mock.Mock()
    monkeypatch.setattr(models.Task, "update", update)
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
    assert task.priority == 0
    assert task.status == "new"
    assert task.error_description is None
    assert task.not_before_at is None
    assert task.next_retry_at is None
    assert [status.value for status in models.TaskStatus] == [
        "new",
        "retry",
        "error",
        "done",
        "cancel",
    ]


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


def test_content_restores_through_kind_selector():
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
    session.get.return_value.json.return_value = {"result": "success", "user_id": 7}
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
        zulip.check_connection()
        zulip.close()
    assert session.auth == ("user@example.com", "test-key")
    assert session.get.call_args_list[0].args == (
        "https://zulip.example.com/api/v1/users/me",
    )
    session.get.assert_called_once()
    session.close.assert_called_once()


def test_task_executes_connection_content():
    task = _task()
    account = mock.Mock(settings={"api_key": "test-key"})
    zulip = mock.Mock()
    with (
        mock.patch.object(external_models.ExternalAccount, "objects") as accounts,
        mock.patch.object(client, "ZulipClient", return_value=zulip) as constructor,
    ):
        accounts.get_one.return_value = account
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
    account.update_status.assert_called_once_with("backfill")


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
    getattr(zulip, operation).side_effect = error
    with (
        mock.patch.object(external_models.ExternalAccount, "objects") as accounts,
        mock.patch.object(client, "ZulipClient", return_value=zulip),
    ):
        accounts.get_one.return_value = mock.Mock(settings={"api_key": "test-key"})
        with pytest.raises(type(error)) as caught:
            task.content.execute(task)
    assert caught.value is error
    zulip.close.assert_called_once()


def test_history_execution_remains_unimplemented():
    task = models.Task(
        project_id=uuid.uuid4(),
        external_account_uuid=uuid.uuid4(),
        content=models.ImportHistoryContent(first_message_id=1, last_message_id=5000),
    )
    with pytest.raises(NotImplementedError, match="History import execution"):
        task.content.execute(task)
    assert task.status == "new"
