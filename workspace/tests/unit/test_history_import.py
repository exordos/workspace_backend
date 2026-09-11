#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Untrusted input, memory bounds and separate transaction boundary contracts."""

import errno
import threading
import uuid as sys_uuid
from unittest import mock

import pytest
from botocore import exceptions as botocore_exceptions
from psycopg import errors as pg_errors

from workspace.external_bridge_control import service
from workspace.history_import import agents
from workspace.history_import import contract
from workspace.history_import import preparation
from workspace.tests.unit import test_external_bridge_control_server


def batch():
    message = dict(
        id=1,
        sender_id=7,
        channel_id=42,
        channel_name="Channel",
        topic="Topic",
        sent_at=1788000000,
        content="Hello",
        reactions=[],
        access=[dict(user_id=7, read=True, starred=False)],
    )
    message["hash"] = contract.digest(message)
    body = dict(
        from_id=1, to_id=5000, users=[dict(id=7, name="User")], messages=[message]
    )
    body["hash"] = contract.digest(body)
    return dict(
        schema_version=1,
        project_uuid=str(sys_uuid.uuid4()),
        provider_realm_uuid=str(sys_uuid.uuid4()),
        generation=1,
        sources=[
            dict(
                account_uuid=str(sys_uuid.uuid4()),
                account_generation=1,
                chat_uuid=str(sys_uuid.uuid4()),
                assignment_generation=1,
            )
        ],
        batch=body,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b["batch"]["messages"][0]["access"][0].update(read="false"),
        lambda b: b["batch"]["messages"][0].update(content="changed without hash"),
        lambda b: b["batch"].update(from_id=2),
        lambda b: b["batch"]["users"].append(dict(id=7, name="Duplicate")),
        lambda b: b["sources"][0].update(account_generation=True),
    ],
)
def test_rejects_noncanonical_or_corrupt_batches(mutation):
    value = batch()
    mutation(value)
    with pytest.raises((contract.ImportError, ValueError)):
        contract.Batch.parse(value)


def test_source_generation_is_part_of_job_identity():
    value = batch()
    bridge_uuid = sys_uuid.uuid4()
    first = contract.Batch.parse(value).job_uuid(bridge_uuid)
    value["sources"][0]["assignment_generation"] += 1
    assert contract.Batch.parse(value).job_uuid(bridge_uuid) != first


@pytest.mark.parametrize("kind", ["file", "image", "video"])
def test_preserved_file_references_require_canonical_uuid(kind):
    file_uuid = str(sys_uuid.uuid4())
    content = " ".join(
        [
            f"urn:{kind}:{file_uuid}",
            f"urn:{kind}:{file_uuid.upper()}",
            "urn:file:" + "a" * 36,
            "urn:file:" + "-" * 36,
            f"urn:file:{file_uuid}aaa",
        ]
    )
    assert preparation.preserved_file_ids(
        {"deleted_at": None, "payload": {"content": content}}
    ) == [file_uuid]


@pytest.mark.parametrize("content", ["", "x" * 40001], ids=["empty", "oversized"])
def test_message_content_uses_canonical_domain_validation(content):
    value = batch()
    value["batch"]["messages"][0]["content"] = content
    with pytest.raises(contract.ImportError, match="invalid_history_"):
        contract.Batch.parse(value)


def test_part_budget_accounts_for_observers_and_reactions():
    messages = [
        dict(id=i, access=[{}] * 100, reactions=[{}] * 30, content="x")
        for i in range(100)
    ]
    parts = list(contract.message_parts(messages))
    assert sum(map(len, parts)) == 100
    assert all(
        sum(1 + 2 * len(m["access"]) + len(m["reactions"]) for m in part)
        <= contract.MAX_PART_ROWS
        for part in parts
    )


def test_history_http_bypasses_legacy_request_transaction():
    handler = test_external_bridge_control_server._handler(
        [("Content-Length", "2")], body=b"{}", path=contract.PATH
    )
    handler.server.history_admission = threading.BoundedSemaphore(1)
    handler.server.history_service = mock.Mock()
    handler.server.history_service.handle.return_value = service.Response.json(
        202, {"status": "pending"}
    )
    handler._dispatch()
    assert handler.transaction_events == []
    handler.server.private_service.handle.assert_not_called()
    handler.server.history_service.handle.assert_called_once()


def test_history_admission_precedes_body_allocation():
    handler = test_external_bridge_control_server._handler(
        [("Content-Length", "2")], body=b"{}", path=contract.PATH
    )
    handler.server.history_admission = threading.BoundedSemaphore(0)
    handler.server.history_service = mock.Mock()
    handler.rfile = mock.Mock()
    handler._dispatch()
    handler.rfile.read.assert_not_called()
    handler.server.history_service.handle.assert_not_called()
    handler.send_response.assert_called_once_with(503)
    handler.send_header.assert_any_call("Connection", "close")
    assert handler.close_connection is True


def test_file_discovery_ignores_fenced_code_and_rejects_traversal():
    assert preparation.attachment_paths("```\n[f](/user_uploads/1/f)\n```") == set()
    assert preparation.attachment_paths("[f](/user_uploads/1/f)") == {
        "/user_uploads/1/f"
    }
    with pytest.raises(contract.ImportError):
        preparation.attachment_paths("[f](/user_uploads/1/%2e%2e/private)")


def test_file_discovery_visits_nested_semantic_quotes():
    assert preparation.attachment_paths(
        "````quote\n```quote\n[f](/user_uploads/1/f)\n```\n````"
    ) == {"/user_uploads/1/f"}


def test_source_count_is_bounded_before_sql_authority_fences():
    body = batch()
    body["sources"] *= contract.MAX_SOURCES + 1
    with pytest.raises(contract.ImportError, match="history_source_limit_exceeded"):
        contract.Batch.parse(body)


def test_source_count_above_previous_limit_is_accepted():
    body = batch()
    source = body["sources"][0]
    body["sources"] = [
        {**source, "chat_uuid": str(sys_uuid.UUID(int=index + 1))}
        for index in range(147)
    ]
    assert len(contract.Batch.parse(body).sources) == 147


def test_bridge_golden_batch_and_markdown_are_compatible():
    import json
    import pathlib

    from workspace.history_import import zulip_markdown

    directory = pathlib.Path(__file__).parents[1] / "fixtures"
    fixture = json.loads((directory / "history_batch_v1.json").read_text())
    parsed = contract.Batch.parse(fixture["envelope"])
    assert parsed.body["hash"] == fixture["envelope"]["batch"]["hash"]
    for case in json.loads((directory / "history_markdown_v1.json").read_text()):
        converted, lossy = zulip_markdown.convert_markdown(
            case["content"],
            case["mention_uuids"],
            case["original_url"],
            file_resolver=lambda path, label: case["files"].get(path),
        )
        assert (converted, lossy) == (case["converted"], case["lossy"])


def test_directory_names_use_the_persisted_domain_limit_before_admission():
    value = batch()
    value["batch"]["users"][0]["name"] = "n" * 129
    value["batch"]["hash"] = contract.digest(
        {k: v for k, v in value["batch"].items() if k != "hash"}
    )
    with pytest.raises(contract.ImportError, match="invalid_history_user_name"):
        contract.Batch.parse(value)


@pytest.mark.parametrize("field,value", [("channel_id", 2**31), ("topic", "t" * 129)])
def test_channel_metadata_uses_source_domain_limits_before_admission(field, value):
    envelope = batch()
    message = envelope["batch"]["messages"][0]
    message[field] = value
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    envelope["batch"]["hash"] = contract.digest(
        {k: v for k, v in envelope["batch"].items() if k != "hash"}
    )
    with pytest.raises(contract.ImportError, match="invalid_history_channel_metadata"):
        contract.Batch.parse(envelope)


@pytest.mark.parametrize(
    "field", ["project_uuid", "provider_realm_uuid", "account_uuid", "chat_uuid"]
)
@pytest.mark.parametrize("value", [None, 17, [], {}, "invalid"])
def test_uuid_fields_reject_invalid_json_types(field, value):
    body = batch()
    target = body["sources"][0] if field in {"account_uuid", "chat_uuid"} else body
    target[field] = value
    with pytest.raises(contract.ImportError, match="invalid_history_uuid"):
        contract.Batch.parse(body)


@pytest.mark.parametrize("prefix", ["@", "/near/"])
def test_quote_targets_are_bounded_before_decimal_conversion(prefix):
    content = " ".join(
        prefix + value
        for value in ["8", "9007199254740991", "9007199254740992", "9" * 9000]
    )
    assert preparation.referenced_message_ids([{"id": 1, "content": content}]) == {
        1,
        8,
        9007199254740991,
    }


@pytest.mark.parametrize(
    "error, expected",
    [
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (OSError(errno.EAGAIN, "temporary I/O"), True),
        (OSError(errno.EINTR, "interrupted I/O"), True),
        (OSError(errno.ENETUNREACH, "network unavailable"), True),
        (FileNotFoundError(), False),
        (PermissionError(), False),
        (IsADirectoryError(), False),
        (NotADirectoryError(), False),
        (OSError(errno.EINVAL, "invalid path"), False),
        (OSError(errno.ENAMETOOLONG, "invalid path"), False),
        (OSError(errno.EIO, "persistent I/O failure"), False),
        (OSError(), False),
        (
            botocore_exceptions.EndpointConnectionError(
                endpoint_url="https://storage.example.test"
            ),
            True,
        ),
        (
            botocore_exceptions.IncompleteReadError(actual_bytes=1, expected_bytes=2),
            True,
        ),
        (botocore_exceptions.ParamValidationError(report="Invalid parameter"), False),
        (KeyError("missing field"), False),
        *[
            (
                botocore_exceptions.ClientError(
                    {
                        "Error": {"Code": code},
                        "ResponseMetadata": {"HTTPStatusCode": status},
                    },
                    "GetObject",
                ),
                expected,
            )
            for status, code, expected in [
                (503, "ServiceUnavailable", True),
                (429, "Throttling", True),
                (408, "RequestTimeout", True),
                (400, "RequestTimeout", True),
                (404, "NoSuchKey", False),
                (403, "AccessDenied", False),
                (400, "InvalidArgument", False),
            ]
        ],
        (botocore_exceptions.ClientError({}, "GetObject"), False),
    ],
)
def test_transient_storage_errors_do_not_include_deterministic_failures(
    error, expected
):
    assert agents.transient_storage_error(error) is expected


def test_transient_storage_error_unwraps_transport_cause_without_looping():
    error = RuntimeError("storage request failed")
    error.__cause__ = botocore_exceptions.EndpointConnectionError(
        endpoint_url="https://storage.example.test"
    )
    assert agents.transient_storage_error(error)
    error.__cause__ = error
    assert not agents.transient_storage_error(error)


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), PermissionError(), IsADirectoryError()]
)
@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_wrapped_permanent_storage_failure_uses_processing_budget(error, link):
    wrapped = RuntimeError("storage request failed")
    setattr(wrapped, link, error)
    assert not agents.transient_storage_error(wrapped)


def test_permanent_local_failure_does_not_inherit_an_earlier_network_error():
    error = FileNotFoundError()
    error.__context__ = ConnectionResetError()
    assert not agents.transient_storage_error(error)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("08000", True),
        ("08003", True),
        ("08006", True),
        ("08P01", True),
        ("57P01", True),
        ("57P02", True),
        ("57P03", True),
        ("57P04", False),
        ("28P01", False),
        ("22001", False),
        ("42601", False),
    ],
)
def test_database_infrastructure_sqlstates_are_narrow(code, expected):
    assert (
        agents.database_connection_failure(pg_errors.lookup(code)("failure"))
        is expected
    )


@pytest.mark.parametrize(
    "message,expected",
    [
        ("the connection is closed", True),
        ("consuming input failed: server closed the connection unexpectedly", True),
        ("SSL connection has been closed unexpectedly", True),
        ("consuming input failed: Connection reset by peer", True),
        ("SSL SYSCALL error: EOF detected", True),
        ("invalid connection option", False),
        ("password authentication failed", False),
        ("invalid sslmode value", False),
        ("root certificate file does not exist", False),
        ("could not translate host name", False),
        ("unclassified operational failure", False),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_no_sqlstate_database_failures_require_driver_transport_evidence(
    message, expected, wrapped
):
    error = pg_errors.OperationalError(message)
    if wrapped:
        wrapper = RuntimeError("database operation failed")
        wrapper.__cause__ = error
        error = wrapper
    assert agents.database_connection_failure(error) is expected


def test_database_transport_classifier_requires_driver_type_and_handles_cycles():
    assert agents.database_connection_failure(pg_errors.ConnectionTimeout())
    error = RuntimeError("the connection is closed")
    assert not agents.database_connection_failure(error)
    error.__context__ = error
    assert not agents.database_connection_failure(error)
