# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import base64
import json
import pathlib
import uuid as sys_uuid
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import x25519
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
    event = push_notifications.build_event(
        event_uuid=sys_uuid.uuid4(),
        kind=push_notifications.MESSAGE_CREATE,
        organization_uuid=sys_uuid.uuid4(),
        project_uuid=project_uuid,
        stream_uuid=sys_uuid.uuid4(),
        topic_uuid=sys_uuid.uuid4(),
        message_uuid=sys_uuid.uuid4(),
        user_uuid=user_uuid,
        devices=devices,
        content={
            "kind": "message",
            "sender_name": "Alice",
            "text": "Sensitive message",
        },
    )
    encoded = json.dumps(event)
    assert "Sensitive message" not in encoded
    assert "Alice" not in encoded
    assert event["event_params"]["kind"] == "notification_message_create"
    assert event["exchange"] == {"kind": "User", "user_uuid": str(user_uuid)}
    contents = [device["content"] for device in event["event_params"]["devices"]]
    assert len(contents) == 2
    assert contents[0]["ciphertext"] != contents[1]["ciphertext"]
    assert all(content["kind"] == "encrypted" for content in contents)


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


def test_wire_fixture_matches_python_producer():
    path = pathlib.Path(__file__).parents[1] / "fixtures"
    fixture = json.loads((path / "mobile_push_event.json").read_text())
    vector = json.loads((path / "mobile_push_hpke_rfc9180.json").read_text())["vector"]
    original = fixture["event"]
    params = original["event_params"]
    target = params["devices"][0]
    private_key = x25519.X25519PrivateKey.from_private_bytes(
        bytes.fromhex(fixture["recipient_private_key_hex"])
    )
    device = push_devices.PushDevice(
        uuid=sys_uuid.UUID(target["device_uuid"]),
        project_id=sys_uuid.UUID(original["project_id"]),
        user_uuid=sys_uuid.UUID(original["exchange"]["user_uuid"]),
        transport="fcm",
        platform="android",
        registration_token=target["registration_token"],
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
            devices=[device],
            content={
                "kind": "message",
                "sender_name": "Alice",
                "text": "Привет из Workspace",
            },
        )
    assert event == original


def test_mobile_push_schema_has_one_migration_head():
    # A branched push migration prevents the normal bootstrap from running.
    migrations.MigrationEngine(
        migrations_path=str(pathlib.Path(__file__).parents[3] / "migrations")
    ).get_latest_migration()
