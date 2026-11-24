# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import base64
import datetime
import json
import pathlib
from unittest import mock
import uuid as sys_uuid

from cryptography.hazmat.primitives.asymmetric import x25519
import pytest
from restalchemy.storage.sql import migrations

from workspace.messenger_api import push_hpke as hpke
from workspace.messenger_api import push_notifications
from workspace.messenger_api.dm import push_devices


def test_hpke_seal_matches_public_cfrg_vector():
    # Public RFC 9180 test material; these are not deployment credentials.
    path = pathlib.Path(__file__).parents[1] / "fixtures/mobile_push_hpke_rfc9180.json"
    vector = json.loads(path.read_text())["vector"]
    message = vector["encryption"]
    ephemeral = x25519.X25519PrivateKey.from_private_bytes(
        bytes.fromhex(vector["skEm"])
    )
    with mock.patch.object(x25519.X25519PrivateKey, "generate", return_value=ephemeral):
        enc, ciphertext = hpke.seal(
            bytes.fromhex(vector["pkRm"]),
            bytes.fromhex(message["pt"]),
            bytes.fromhex(message["aad"]),
            bytes.fromhex(vector["info"]),
        )
    assert enc.hex() == vector["enc"]
    assert ciphertext.hex() == message["ct"]


def test_message_event_is_encrypted_separately_for_each_device():
    project_uuid, user_uuid = sys_uuid.uuid4(), sys_uuid.uuid4()
    devices = []
    for number in range(2):
        public_key = x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()
        devices.append(
            push_devices.PushDevice(
                project_id=project_uuid,
                user_uuid=user_uuid,
                transport="fcm",
                platform="android",
                registration_token=f"test-token-{number}",
                encryption=push_devices.PushDeviceHPKEEncryption(
                    algorithm=push_devices.HPKE_ALGORITHM,
                    key_uuid=sys_uuid.uuid4(),
                    public_key=base64.urlsafe_b64encode(public_key)
                    .rstrip(b"=")
                    .decode(),
                ),
            )
        )
    common = {
        "event_uuid": sys_uuid.uuid4(),
        "kind": push_notifications.MESSAGE_CREATE,
        "organization_uuid": sys_uuid.uuid4(),
        "project_uuid": project_uuid,
        "stream_uuid": sys_uuid.uuid4(),
        "topic_uuid": sys_uuid.uuid4(),
        "message_uuid": sys_uuid.uuid4(),
        "user_uuid": user_uuid,
        "content": {
            "kind": "message",
            "sender_name": "Alice",
            "text": "Sensitive message",
        },
    }
    events = [
        push_notifications.build_event(device=device, **common) for device in devices
    ]
    assert all(event is not None for event in events)
    encoded = json.dumps(events)
    assert "Sensitive message" not in encoded
    assert "Alice" not in encoded
    assert all(
        event["event_params"]["kind"] == "notification_message_create"
        for event in events
    )
    assert all(
        event["exchange"] == {"kind": "User", "user_uuid": str(user_uuid)}
        for event in events
    )
    payloads = [json.loads(event["event_params"]["payload"]) for event in events]
    assert payloads[0]["content"]["ciphertext"] != payloads[1]["content"]["ciphertext"]
    assert all(payload["content"]["kind"] == "encrypted" for payload in payloads)


def test_associated_data_binds_action_route_recipient_and_key():
    event = {
        "uuid": str(sys_uuid.uuid4()),
        "exchange": {"user_uuid": str(sys_uuid.uuid4())},
        "event_params": {
            "kind": push_notifications.MESSAGE_CREATE,
            **{
                field: str(sys_uuid.uuid4())
                for field in push_notifications.ROUTE_FIELDS
            },
        },
    }
    device_uuid, key_uuid = sys_uuid.uuid4(), sys_uuid.uuid4()
    original = push_notifications.associated_data(event, device_uuid, key_uuid)
    event["event_params"]["kind"] = push_notifications.MESSAGE_CANCELLED
    assert push_notifications.associated_data(event, device_uuid, key_uuid) != original
    assert json.loads(original)[-2:] == [str(device_uuid), str(key_uuid)]
    assert (
        push_notifications.EVENT_TYPES[push_notifications.MESSAGE_CREATE]
        != (push_notifications.EVENT_TYPES[push_notifications.MESSAGE_CANCELLED])
    )


@pytest.mark.parametrize("priority", ["high", "normal"])
def test_apns_snapshot_fits_provider_limit_with_json_escaped_previews(priority):
    user, project = sys_uuid.uuid4(), sys_uuid.uuid4()
    key = x25519.X25519PrivateKey.generate()
    device = push_devices.PushDevice(
        uuid=sys_uuid.uuid4(),
        project_id=project,
        user_uuid=user,
        transport="apns",
        platform="ios",
        registration_token="ab" * 32,
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        encryption=push_devices.PushDeviceHPKEEncryption(
            algorithm=push_devices.HPKE_ALGORITHM,
            key_uuid=sys_uuid.uuid4(),
            public_key=base64.urlsafe_b64encode(key.public_key().public_bytes_raw())[
                :-1
            ].decode(),
        ),
    )
    event = push_notifications.build_event(
        event_uuid=sys_uuid.uuid4(),
        kind=push_notifications.TOPIC_UPDATE,
        organization_uuid=sys_uuid.uuid4(),
        project_uuid=project,
        stream_uuid=sys_uuid.uuid4(),
        topic_uuid=sys_uuid.uuid4(),
        message_uuid=sys_uuid.uuid4(),
        user_uuid=user,
        device=device,
        content={
            "kind": "topic",
            "stream_name": "\x01" * 256,
            "topic_name": "\x01" * 256,
            "sender_name": "\x01" * 128,
            "text": "\x01" * 768,
            "unread_count": 1,
            "active_unread_count": 1,
            "passive_unread_count": 0,
            "epoch_version": 2**63 - 1,
            "epoch_generation": str(sys_uuid.uuid4()),
            "created_at": "2026-01-01T12:34:56.123456+00:00",
        },
        priority=priority,
    )
    params = event["event_params"]
    body = push_notifications.apns_payload(params["payload"], params["priority"])
    payload = json.loads(body)
    aps = {"content-available": 1}
    if priority == "high":
        aps.update(alert={"loc-key": "WORKSPACE_PUSH_NEW_MESSAGE"}, sound="default")
    assert payload["aps"] == aps
    assert payload["workspace"]["content"]["kind"] == "encrypted"
    assert len(body.encode()) <= 4096


def test_wire_fixture_matches_python_producer():
    path = pathlib.Path(__file__).parents[1] / "fixtures"
    fixture = json.loads((path / "mobile_push_event.json").read_text())
    vector = json.loads((path / "mobile_push_hpke_rfc9180.json").read_text())["vector"]
    original = fixture["event"]
    params = original["event_params"]
    target = fixture["payload"]
    private_key = x25519.X25519PrivateKey.from_private_bytes(
        bytes.fromhex(fixture["recipient_private_key_hex"])
    )
    device = push_devices.PushDevice(
        uuid=sys_uuid.UUID(target["device_uuid"]),
        project_id=sys_uuid.UUID(original["project_id"]),
        user_uuid=sys_uuid.UUID(original["exchange"]["user_uuid"]),
        transport="fcm",
        platform="android",
        registration_token="test-token",
        encryption=push_devices.PushDeviceHPKEEncryption(
            algorithm=push_devices.HPKE_ALGORITHM,
            key_uuid=sys_uuid.UUID(target["content"]["key_uuid"]),
            public_key=base64.urlsafe_b64encode(
                private_key.public_key().public_bytes_raw()
            )
            .rstrip(b"=")
            .decode(),
        ),
    )
    with mock.patch.object(
        x25519.X25519PrivateKey,
        "generate",
        return_value=x25519.X25519PrivateKey.from_private_bytes(
            bytes.fromhex(vector["skEm"])
        ),
    ):
        event = push_notifications.build_event(
            event_uuid=sys_uuid.UUID(original["uuid"]),
            kind=params["kind"],
            **{
                field: sys_uuid.UUID(params[field])
                for field in push_notifications.ROUTE_FIELDS
            },
            user_uuid=sys_uuid.UUID(original["exchange"]["user_uuid"]),
            device=device,
            content={
                "kind": "message",
                "sender_name": "Alice",
                "text": "Привет из Workspace",
            },
        )
    assert event is not None
    assert json.loads(event["event_params"]["payload"]) == fixture["payload"]
    assert event["event_params"]["device_uuid"] == target["device_uuid"]
    assert event["event_params"]["registration_token"] == "test-token"


def test_mobile_push_schema_has_one_migration_head():
    # A branched push migration prevents the normal bootstrap from running.
    migrations.MigrationEngine(
        migrations_path=str(pathlib.Path(__file__).parents[3] / "migrations")
    ).get_latest_migration()
