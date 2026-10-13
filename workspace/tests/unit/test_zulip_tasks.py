# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

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


def _task():
    return models.Task(
        content=models.CheckConnectionContent(external_account_uuid=uuid.uuid4()),
    )


def test_task_is_concrete_with_priority_and_retry_status():
    task = _task()
    assert not inspect.isabstract(models.Task)
    assert task.priority == 0
    assert task.status == "new"
    assert task.error_description is None
    assert task.next_retry_at is None
    assert [status.value for status in models.TaskStatus] == [
        "new",
        "retry",
        "error",
        "done",
    ]


def test_content_restores_through_kind_selector():
    content = models.TASK_CONTENT_TYPE.from_simple_type(
        {
            "kind": "import_history",
            "external_account_uuid": str(uuid.uuid4()),
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
            external_account_uuid=uuid.uuid4(),
            first_message_id=first,
            last_message_id=last,
        )


def test_client_checks_credentials():
    session = mock.Mock()
    session.get.return_value.json.return_value = {"result": "success", "user_id": 7}
    with mock.patch.object(client.requests, "Session", return_value=session):
        zulip = client.ZulipClient(
            external_models.ExternalAccount(
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
        == task.content.external_account_uuid
    )
    constructor.assert_called_once_with(account)
    assert zulip.method_calls == [mock.call.check_connection(), mock.call.close()]


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
            task.execute()
    assert caught.value is error
    zulip.close.assert_called_once()


def test_history_execution_remains_unimplemented():
    task = models.Task(
        content=models.ImportHistoryContent(
            external_account_uuid=uuid.uuid4(), first_message_id=1, last_message_id=5000
        )
    )
    with pytest.raises(NotImplementedError, match="History import execution"):
        task.execute()
    assert task.status == "new"
