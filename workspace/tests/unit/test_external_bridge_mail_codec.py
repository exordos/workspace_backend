# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import email
import email.policy
import json
import pathlib
import uuid as sys_uuid

import pytest

from workspace.messenger_mail import external_bridge_codec


ACCOUNT_UUID = "10000000-0000-0000-0000-000000000001"
PROJECT_UUID = "20000000-0000-0000-0000-000000000002"
RECORD_UUID = "30000000-0000-0000-0000-000000000003"
OPERATION_UUID = "40000000-0000-0000-0000-000000000004"
SENDER = "zulip-bridge-producer@bridge.workspace.invalid"
RECIPIENT = "zulip-bridge@messenger.workspace.invalid"
FIXTURES = pathlib.Path(__file__).parents[1] / "fixtures"


def _record():
    record = {
        "schema": "workspace.zulip_bridge.mail",
        "schema_version": 1,
        "record_kind": "operation",
        "record_uuid": RECORD_UUID,
        "operation_uuid": OPERATION_UUID,
        "attempt": 1,
        "operation_sha256": "0" * 64,
        "account_uuid": ACCOUNT_UUID,
        "project_uuid": PROJECT_UUID,
        "origin": "workspace",
        "causal_lane": f"chat:{ACCOUNT_UUID}:{sys_uuid.uuid4()}",
        "sequence": 1,
        "predecessor_operation_uuid": None,
        "created_at": "2026-07-17T12:00:00Z",
        "expires_at": "2026-07-18T12:00:00Z",
        "operation": {
            "kind": "message.create",
            "entity_uuid": "50000000-0000-0000-0000-000000000005",
            "actor_uuid": "60000000-0000-0000-0000-000000000006",
            "occurred_at": "2026-07-17T12:00:00Z",
            "provider": {
                "kind": "zulip",
                "chat_id": "42",
                "entity_id": None,
                "revision": None,
            },
            "payload": {
                "stream_uuid": "70000000-0000-0000-0000-000000000007",
                "topic_uuid": "80000000-0000-0000-0000-000000000008",
                "author_uuid": "60000000-0000-0000-0000-000000000006",
                "payload": {"kind": "markdown", "content": "hello"},
                "reply_to_message_uuid": None,
            },
            "extensions": {},
        },
    }
    record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
    return record


def test_signed_bridge_mail_round_trips_and_contains_no_attachment():
    record = _record()
    key = external_bridge_codec.derive_direction_key(
        "opaque enrollment token",
        "90000000-0000-0000-0000-000000000009",
        "a0000000-0000-0000-0000-00000000000a",
        3,
        "workspace-to-zulip",
    )
    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )
    message = email.message_from_bytes(raw, policy=email.policy.default)
    assert not message.is_multipart()
    assert message.get_content_type() == "application/json"
    assert message.get_filename() is None
    assert b"hello" not in raw
    assert (
        external_bridge_codec.parse_message(
            raw,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )
        == record
    )


def test_bridge_mail_rejects_unknown_delivery_class():
    record = _record()
    record["operation"]["extensions"] = {"delivery_class": "historical"}
    record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
    key = external_bridge_codec.derive_direction_key(
        "opaque enrollment token",
        "90000000-0000-0000-0000-000000000009",
        "a0000000-0000-0000-0000-00000000000a",
        3,
        "workspace-to-zulip",
    )

    with pytest.raises(
        external_bridge_codec.InvalidExternalBridgeRecord,
        match="delivery class",
    ):
        external_bridge_codec.build_message(
            record,
            "workspace-to-zulip",
            key,
            SENDER,
            RECIPIENT,
        )


@pytest.mark.parametrize(
    "selector",
    (
        {"through_message_uuid": "50000000-0000-0000-0000-000000000005"},
        {
            "message_uuids": [
                "50000000-0000-0000-0000-000000000005",
                "51000000-0000-0000-0000-000000000005",
            ]
        },
    ),
)
def test_read_state_selector_round_trips(selector):
    record = _record()
    record["operation"].update(
        {
            "kind": "read_state.set",
            "payload": {
                "stream_uuid": "70000000-0000-0000-0000-000000000007",
                "topic_uuid": "80000000-0000-0000-0000-000000000008",
                "reader_uuid": "60000000-0000-0000-0000-000000000006",
                "read": True,
                **selector,
            },
        }
    )
    record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
    key = b"k" * 32

    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )

    assert (
        external_bridge_codec.parse_message(
            raw,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )
        == record
    )


def test_read_state_exact_selector_matches_cross_repository_fixture():
    record = json.loads(
        (FIXTURES / "external_bridge_read_state_exact_record.json").read_text(
            encoding="utf-8"
        )
    )
    assert external_bridge_codec.operation_sha256(record) == (
        "791b446acdcc6781bde46963e2e59d9b85b46bbe8d6a6b56f242f8b06125828d"
    )
    assert record["origin"] == "workspace"
    assert record["operation"]["payload"]["message_uuids"] == [
        "10000000-0000-0000-0000-000000000010",
        "20000000-0000-0000-0000-000000000020",
    ]
    key = b"k" * 32

    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )

    assert (
        external_bridge_codec.parse_message(
            raw,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )
        == record
    )


@pytest.mark.parametrize(
    "selectors",
    (
        {},
        {
            "through_message_uuid": "50000000-0000-0000-0000-000000000005",
            "message_uuids": ["50000000-0000-0000-0000-000000000005"],
        },
        {"through_message_uuid": None},
        {"message_uuids": []},
    ),
)
def test_read_state_requires_one_valid_selector(selectors):
    record = _record()
    record["operation"].update(
        {
            "kind": "read_state.set",
            "payload": {
                "stream_uuid": "70000000-0000-0000-0000-000000000007",
                "topic_uuid": None,
                "reader_uuid": "60000000-0000-0000-0000-000000000006",
                "read": False,
                **selectors,
            },
        }
    )
    record["operation_sha256"] = external_bridge_codec.operation_sha256(record)

    with pytest.raises(external_bridge_codec.InvalidExternalBridgeRecord):
        external_bridge_codec.build_message(
            record,
            "workspace-to-zulip",
            b"k" * 32,
            SENDER,
            RECIPIENT,
        )


def test_signed_bridge_mail_matches_cross_repository_wire_fixture():
    record = json.loads(
        (FIXTURES / "external_bridge_mail_record.json").read_text(encoding="utf-8")
    )
    encoded_key = (
        (FIXTURES / "external_bridge_mail_direction_key.b64url")
        .read_text(encoding="ascii")
        .strip()
    )
    key = base64.urlsafe_b64decode(encoded_key + "=")
    raw_message = base64.b64decode(
        (FIXTURES / "external_bridge_mail.eml.b64").read_bytes()
    )

    assert (
        external_bridge_codec.build_message(
            record,
            "workspace-to-zulip",
            key,
            SENDER,
            RECIPIENT,
        )
        == raw_message
    )
    assert (
        external_bridge_codec.parse_message(
            raw_message,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )
        == record
    )


@pytest.mark.parametrize("mutation", ["signature", "body", "header"])
def test_signed_bridge_mail_rejects_integrity_and_header_mismatch(mutation):
    record = _record()
    key = b"k" * 32
    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )
    if mutation == "signature":
        raw = raw.replace(
            b"X-Workspace-Bridge-Signature: v1=",
            b"X-Workspace-Bridge-Signature: v1=A",
            1,
        )
    elif mutation == "body":
        raw = raw[:-6] + b"AAAA\r\n"
    else:
        raw = raw.replace(ACCOUNT_UUID.encode(), b"f" * 36, 1)
    with pytest.raises(external_bridge_codec.InvalidExternalBridgeRecord):
        external_bridge_codec.parse_message(
            raw,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )


def test_signed_bridge_mail_rejects_duplicate_json_members():
    record = _record()
    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        b"k" * 32,
        SENDER,
        RECIPIENT,
    )
    message = email.message_from_bytes(raw, policy=email.policy.default)
    body = message.get_payload(decode=True)
    body = body.replace(
        b'{"account_uuid":', b'{"account_uuid":"duplicate","account_uuid":', 1
    )
    message.set_payload(body)
    message.replace_header("Content-Transfer-Encoding", "base64")
    raw = message.as_bytes(policy=email.policy.SMTP)
    with pytest.raises(external_bridge_codec.InvalidExternalBridgeRecord):
        external_bridge_codec.parse_message(
            raw,
            "workspace-to-zulip",
            [b"k" * 32],
            SENDER,
            RECIPIENT,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "content-disposition",
        "filename",
        "content-type-parameter",
    ],
)
def test_signed_bridge_mail_rejects_forbidden_mime_metadata(mutation):
    record = _record()
    key = b"k" * 32
    raw = external_bridge_codec.build_message(
        record,
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )
    message = email.message_from_bytes(raw, policy=email.policy.SMTP)
    if mutation == "content-disposition":
        message["Content-Disposition"] = "inline"
    elif mutation == "filename":
        message["Content-Disposition"] = 'attachment; filename="bridge.json"'
    else:
        message.set_param("format", "flowed", header="Content-Type")

    with pytest.raises(external_bridge_codec.InvalidExternalBridgeRecord):
        external_bridge_codec.parse_message(
            message.as_bytes(policy=email.policy.SMTP),
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )


@pytest.mark.parametrize("date_value", [None, "not-a-date"])
def test_signed_bridge_mail_requires_one_valid_rfc5322_date(date_value):
    key = b"k" * 32
    raw = external_bridge_codec.build_message(
        _record(),
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )
    message = email.message_from_bytes(raw, policy=email.policy.SMTP)
    del message["Date"]
    if date_value is not None:
        message["Date"] = date_value

    with pytest.raises(
        external_bridge_codec.InvalidExternalBridgeRecord,
        match="Date header",
    ):
        external_bridge_codec.parse_message(
            message.as_bytes(policy=email.policy.SMTP),
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )


def test_signed_bridge_mail_rejects_duplicate_date_header():
    key = b"k" * 32
    raw = external_bridge_codec.build_message(
        _record(),
        "workspace-to-zulip",
        key,
        SENDER,
        RECIPIENT,
    )
    duplicated = raw.replace(
        b"Date: ",
        b"Date: Thu, 01 Jan 1970 00:00:00 +0000\r\nDate: ",
        1,
    )

    with pytest.raises(
        external_bridge_codec.InvalidExternalBridgeRecord,
        match="Date header",
    ):
        external_bridge_codec.parse_message(
            duplicated,
            "workspace-to-zulip",
            [key],
            SENDER,
            RECIPIENT,
        )
