# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
import base64
import json
import uuid as sys_uuid
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mobile_push_opts
from workspace.messenger_api import push_outbox
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.tests.integration import test_key_announcements, test_messenger_v2


@pytest.fixture(autouse=True)
def configured_push(_database):
    for name, value in {
        "organization_uuid": str(sys_uuid.uuid4()),
        "notification_endpoint": "http://notification.invalid",
        "notification_auth_file": "/nonexistent/test-only.json",
    }.items():
        cfg.CONF.set_override(name, value, group=mobile_push_opts.DOMAIN)
    api_store.configure_store_factory(store_factory.build_store_factory())
    with contexts.Context().session_manager() as session:
        session.execute("DELETE FROM messenger_push_outbox", ())
    yield
    for name in (
        "organization_uuid",
        "notification_endpoint",
        "notification_auth_file",
    ):
        cfg.CONF.clear_override(name, group=mobile_push_opts.DOMAIN)


def prepared_message(api, workspace_api, db):
    stream = test_key_announcements._create_stream(api)
    peer = test_key_announcements._add_member(api, db, stream["uuid"])
    workspace_api.project_id = api.project_id
    device_uuid = sys_uuid.uuid4()
    key = x25519.X25519PrivateKey.generate()
    registered = workspace_api.put(
        f"/v1/push_devices/{device_uuid}",
        user=peer,
        json={
            "transport": "fcm",
            "platform": "android",
            "registration_token": "old-token",
            "encryption": {
                "kind": "HPKE",
                "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
                "key_uuid": str(sys_uuid.uuid4()),
                "public_key": base64.urlsafe_b64encode(
                    key.public_key().public_bytes_raw()
                )
                .rstrip(b"=")
                .decode(),
            },
        },
    )
    assert registered.status_code == 201, registered.text
    response = api.post(
        "/v1/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "private push preview"},
        },
    )
    assert response.status_code == 201, response.text
    test_messenger_v2._drain()
    return stream, peer, device_uuid, response.json()


@pytest.mark.parametrize("read_scope", ["messages", "stream_topics", "streams"])
@pytest.mark.parametrize("delivery_expired", [False, True])
def test_native_message_snapshot_and_read_cancellation_are_durable(
    api, workspace_api, db, read_scope, delivery_expired
):
    stream, peer, _, message = prepared_message(api, workspace_api, db)
    event = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE user_uuid=%s", (peer,)
    ).fetchone()[0]
    assert event["event_params"]["kind"] == "workspace_message_create"
    assert event["event_params"]["message_uuid"] == message["uuid"]
    assert event["event_params"]["stream_uuid"] == stream["uuid"]
    assert event["exchange"]["user_uuid"] == str(peer)
    assert "private push preview" not in json.dumps(event)
    first_ciphertext = event["event_params"]["devices"][0]["content"]["ciphertext"]
    test_messenger_v2._drain()
    assert (
        db.execute(
            "SELECT event->'event_params'->'devices'->0->'content'->>'ciphertext' FROM messenger_push_outbox WHERE user_uuid=%s",
            (peer,),
        ).fetchone()[0]
        == first_ciphertext
    )
    if delivery_expired:
        # A delivered alert can still be visible after its FCM delivery deadline.
        # Its retained snapshot must remain usable for a later read cancellation.
        db.execute(
            "UPDATE messenger_push_outbox SET created_at=NOW()-INTERVAL '70 minutes', "
            "expires_at=NOW()-INTERVAL '10 minutes' WHERE user_uuid=%s",
            (peer,),
        )
    read = api.post(f"/v1/messages/{message['uuid']}/actions/read/invoke", user=peer)
    assert read.status_code == 200, read.text
    test_messenger_v2._drain()
    rows = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE user_uuid=%s ORDER BY created_at",
        (peer,),
    ).fetchall()
    assert [row[0]["event_params"]["kind"] for row in rows] == [
        "workspace_message_create",
        "workspace_message_cancelled",
    ]
    assert rows[0][0]["uuid"] != rows[1][0]["uuid"]
    assert rows[1][0]["event_params"]["message_uuid"] == message["uuid"]


def test_retry_uses_same_event_and_inactive_response_cannot_remove_new_token(
    api, workspace_api, db
):
    _, peer, device_uuid, _ = prepared_message(api, workspace_api, db)
    db.execute(
        "UPDATE m_workspace_push_devices SET registration_token='new-token' WHERE uuid=%s",
        (device_uuid,),
    )
    client = mock.Mock()
    client.get.return_value = {
        "status": "ERROR",
        "errors": [
            {"code": "registration_token_inactive", "device_uuid": str(device_uuid)}
        ],
    }
    with (
        mock.patch.object(push_outbox, "notification_client", return_value=client),
        contexts.Context().session_manager() as session,
    ):
        assert push_outbox.process_one(session)
    assert (
        db.execute(
            "SELECT registration_token FROM m_workspace_push_devices WHERE uuid=%s",
            (device_uuid,),
        ).fetchone()[0]
        == "new-token"
    )
    assert db.execute(
        "SELECT terminal FROM messenger_push_outbox WHERE user_uuid=%s", (peer,)
    ).fetchone()[0]


def test_transient_delivery_error_keeps_outbox_retrying(api, workspace_api, db):
    _, peer, _, _ = prepared_message(api, workspace_api, db)
    client = mock.Mock()
    client.get.return_value = {
        "status": "ERROR",
        "errors": [{"code": "fcm_http_503", "permanent": False}],
    }
    with (
        mock.patch.object(push_outbox, "notification_client", return_value=client),
        contexts.Context().session_manager() as session,
    ):
        assert push_outbox.process_one(session)
    assert not db.execute(
        "SELECT terminal FROM messenger_push_outbox WHERE user_uuid=%s", (peer,)
    ).fetchone()[0]


def test_low_order_registration_key_is_rejected_and_legacy_bad_key_cannot_block_chat(
    api, workspace_api, db
):
    stream, peer, good_device, _ = prepared_message(api, workspace_api, db)
    bad_device = sys_uuid.uuid4()
    invalid_key = base64.urlsafe_b64encode(bytes(32)).rstrip(b"=").decode()
    payload = {
        "transport": "fcm",
        "platform": "android",
        "registration_token": "invalid-key-token",
        "encryption": {
            "kind": "HPKE",
            "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
            "key_uuid": str(sys_uuid.uuid4()),
            "public_key": invalid_key,
        },
    }
    rejected = workspace_api.put(
        f"/v1/push_devices/{bad_device}", user=peer, json=payload
    )
    assert rejected.status_code == 400, rejected.text
    # Previous servers accepted length-correct low-order keys. Exercise that real stored case.
    db.execute(
        """INSERT INTO m_workspace_push_devices (uuid, project_id, user_uuid, transport, platform, registration_token, encryption)
        VALUES (%s, %s, %s, 'fcm', 'android', 'invalid-key-token', %s::jsonb)""",
        (bad_device, api.project_id, peer, json.dumps(payload["encryption"])),
    )
    created = api.post(
        "/v1/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Still reaches the chat"},
        },
    )
    assert created.status_code == 201, created.text
    test_messenger_v2._drain()
    message_uuid = created.json()["uuid"]
    result = api.get(f"/v1/messages/{message_uuid}", user=peer)
    assert result.status_code == 200, result.text
    event = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE message_uuid=%s AND user_uuid=%s",
        (message_uuid, peer),
    ).fetchone()[0]
    assert [d["device_uuid"] for d in event["event_params"]["devices"]] == [
        str(good_device)
    ]
