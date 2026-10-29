# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
import base64
import json
import uuid as sys_uuid
from unittest import mock

import pytest
import psycopg
from cryptography.hazmat.primitives.asymmetric import x25519
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mobile_push_opts
from workspace.messenger_api import push_events
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services import push_notifications as push_handler
from workspace.tests.integration import conftest
from workspace.tests.integration import test_key_announcements, test_messenger_v2


def prepare_deliveries():
    push_handler.TopicPushHandler()._iteration()


def _run_sender(client):
    prepare_deliveries()
    sender = push_events.WorkspacePushSender()
    sender._notification_client = client
    sender._iteration()
    return sender


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
        session.execute(
            "DELETE FROM gcl_sdk_events WHERE event_data->>'kind' IN ('WorkspacePush','WorkspaceTopicSignal'); DELETE FROM messenger_counter_updates",
            (),
        )
    yield
    for name in (
        "organization_uuid",
        "notification_endpoint",
        "notification_auth_file",
    ):
        cfg.CONF.clear_override(name, group=mobile_push_opts.DOMAIN)


def prepared_message(api, workspace_api, db, *, prepare_push=True):
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
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "private push preview"},
        },
    )
    assert response.status_code == 201, response.text
    test_messenger_v2._drain()
    if prepare_push:
        prepare_deliveries()
    return stream, peer, device_uuid, response.json()


@pytest.mark.parametrize("change", ["registration", "cancellation"])
def test_slow_notification_does_not_hold_device_or_outbox_locks(
    api,
    workspace_api,
    db,
    change,
):
    _, peer, device_uuid, message = prepared_message(api, workspace_api, db)
    callback_completed = []

    def notification_result(*args):
        if not callback_completed:
            if change == "registration":
                with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as writer:
                    writer.execute("SET statement_timeout='300ms'")
                    writer.execute(
                        "UPDATE m_workspace_push_devices SET registration_token='rotated-during-send' WHERE uuid=%s",
                        (device_uuid,),
                    )
            else:
                response = api.post(
                    f"/v1/messenger/messages/{message['uuid']}/actions/read/invoke",
                    user=peer,
                )
                assert response.status_code == 200, response.text
                test_messenger_v2._drain()
            callback_completed.append(True)
        return {"status": "ACTIVE"}

    client = mock.Mock()
    client.get_event.side_effect = notification_result
    _run_sender(client)
    assert callback_completed == [True]
    if change == "registration":
        assert db.execute(
            "SELECT registration_token FROM m_workspace_push_devices WHERE uuid=%s",
            (device_uuid,),
        ).fetchone() == ("rotated-during-send",)
    else:
        assert db.execute(
            "SELECT count(*) FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspaceTopicSignal'"
        ).fetchone() == (2,)


def test_retry_uses_same_event_and_inactive_response_cannot_remove_new_token(
    api, workspace_api, db
):
    _, peer, device_uuid, _ = prepared_message(api, workspace_api, db)
    db.execute(
        "UPDATE m_workspace_push_devices SET registration_token='new-token' WHERE uuid=%s",
        (device_uuid,),
    )
    client = mock.Mock()
    client.get_event.return_value = {
        "status": "ERROR",
        "errors": [
            {"code": "registration_token_inactive", "device_uuid": str(device_uuid)}
        ],
    }
    _run_sender(client)
    assert (
        db.execute(
            "SELECT registration_token FROM m_workspace_push_devices WHERE uuid=%s",
            (device_uuid,),
        ).fetchone()[0]
        == "new-token"
    )
    assert db.execute(
        "SELECT status IN ('ACTIVE','ERROR') FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush' AND event_data->'body'->'exchange'->>'user_uuid'=%s::text",
        (peer,),
    ).fetchone()[0]


@pytest.mark.parametrize("refresh", [None, "token", "key"])
def test_inactive_response_preserves_registration_rotated_during_handoff(
    api, workspace_api, db, refresh
):
    _, peer, device_uuid, _ = prepared_message(api, workspace_api, db)
    encryption = db.execute(
        "SELECT encryption FROM m_workspace_push_devices WHERE uuid=%s", (device_uuid,)
    ).fetchone()[0]
    token = "new-token" if refresh == "token" else "old-token"
    if refresh == "key":
        key = x25519.X25519PrivateKey.generate()
        encryption = dict(
            encryption,
            key_uuid=str(sys_uuid.uuid4()),
            public_key=base64.urlsafe_b64encode(key.public_key().public_bytes_raw())
            .rstrip(b"=")
            .decode(),
        )

    def inactive_response(*args):
        if refresh:
            response = workspace_api.put(
                f"/v1/push_devices/{device_uuid}",
                user=peer,
                json={
                    "platform": "android",
                    "transport": "fcm",
                    "registration_token": token,
                    "encryption": encryption,
                },
            )
            assert response.status_code == 200, response.text
        return {
            "status": "ERROR",
            "errors": [
                {"code": "registration_token_inactive", "device_uuid": str(device_uuid)}
            ],
        }

    client = mock.Mock()
    client.get_event.side_effect = inactive_response
    _run_sender(client)
    current = db.execute(
        "SELECT registration_token, encryption FROM m_workspace_push_devices WHERE uuid=%s",
        (device_uuid,),
    ).fetchone()
    assert current == (token, encryption) if refresh else current is None
    assert db.execute(
        "SELECT status IN ('ACTIVE','ERROR') FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush' AND event_data->'body'->'exchange'->>'user_uuid'=%s::text",
        (peer,),
    ).fetchone()[0]


def test_transient_delivery_error_keeps_outbox_retrying(api, workspace_api, db):
    _, peer, _, _ = prepared_message(api, workspace_api, db)
    client = mock.Mock()
    client.get_event.return_value = {
        "status": "ERROR",
        "errors": [{"code": "fcm_http_503", "permanent": False}],
    }
    _run_sender(client)
    assert not db.execute(
        "SELECT status IN ('ACTIVE','ERROR') FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush' AND event_data->'body'->'exchange'->>'user_uuid'=%s::text",
        (peer,),
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
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Still reaches the chat"},
        },
    )
    assert created.status_code == 201, created.text
    test_messenger_v2._drain()
    message_uuid = created.json()["uuid"]
    result = api.get(f"/v1/messenger/messages/{message_uuid}", user=peer)
    assert result.status_code == 200, result.text
    client = mock.Mock()
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    _run_sender(client)
    assert client.send_event.call_count == 2
    assert all(
        call.args[0].event_data.body["event_params"]["devices"][0]["device_uuid"]
        == str(good_device)
        for call in client.send_event.call_args_list
    )
    assert db.execute(
        "SELECT count(*) FROM gcl_sdk_events WHERE status='ERROR'"
    ).fetchone() == (1,)


@pytest.mark.parametrize("retirement", ["delete", "token", "key"])
def test_retired_device_snapshot_is_not_submitted_to_notification(
    api, workspace_api, db, retirement
):
    _, peer, device_uuid, _ = prepared_message(api, workspace_api, db)
    if retirement == "delete":
        response = workspace_api.delete(f"/v1/push_devices/{device_uuid}", user=peer)
        assert response.status_code == 204, response.text
    elif retirement == "token":
        db.execute(
            "UPDATE m_workspace_push_devices SET registration_token='replacement-token' WHERE uuid=%s",
            (device_uuid,),
        )
    else:
        db.execute(
            "UPDATE m_workspace_push_devices SET encryption=jsonb_set(encryption, '{key_uuid}', %s::jsonb) WHERE uuid=%s",
            (json.dumps(str(sys_uuid.uuid4())), device_uuid),
        )
    client = mock.Mock()
    client.get_event.return_value = {"status": "ACTIVE"}
    _run_sender(client)
    client.get_event.assert_not_called()
    client.send_event.assert_not_called()
    row = db.execute(
        "SELECT status IN ('ACTIVE','ERROR'), event_data->'body'->'event_params'->'devices' FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush' AND event_data->'body'->'exchange'->>'user_uuid'=%s::text",
        (peer,),
    ).fetchone()
    assert row == (True, [])


def test_retiring_one_device_keeps_the_other_encrypted_delivery(api, workspace_api, db):
    stream, peer, retired, _ = prepared_message(api, workspace_api, db)
    encryption = db.execute(
        "SELECT encryption FROM m_workspace_push_devices WHERE uuid=%s", (retired,)
    ).fetchone()[0]
    surviving = sys_uuid.uuid4()
    registered = workspace_api.put(
        f"/v1/push_devices/{surviving}",
        user=peer,
        json={
            "transport": "fcm",
            "platform": "android",
            "registration_token": "surviving-token",
            "encryption": encryption,
        },
    )
    assert registered.status_code == 201, registered.text
    created = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Two-device delivery"},
        },
    )
    assert created.status_code == 201, created.text
    test_messenger_v2._drain()
    prepare_deliveries()
    snapshot = db.execute(
        "SELECT event_data->'body' FROM gcl_sdk_events WHERE event_data->'body'->'event_params'->'devices'->0->>'device_uuid'=%s",
        (str(surviving),),
    ).fetchone()[0]
    removed = workspace_api.delete(f"/v1/push_devices/{retired}", user=peer)
    assert removed.status_code == 204, removed.text
    client = mock.Mock()
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    _run_sender(client)
    client.send_event.assert_called_once()
    submitted = client.send_event.call_args.args[0].event_data.body
    assert submitted["uuid"] == snapshot["uuid"]
    assert [
        target["device_uuid"] for target in submitted["event_params"]["devices"]
    ] == [str(surviving)]
    assert submitted["event_params"]["devices"][0]["content"]["kind"] == "encrypted"


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_push_registration_and_message_http_logs_omit_bodies(
    api, workspace_api, db, caplog, level
):
    with caplog.at_level(level):
        prepared_message(api, workspace_api, db)
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "restalchemy.api.middlewares.logging"
    )
    assert "RESP:" in logged
    assert "old-token" not in logged
    assert "private push preview" not in logged


def test_sdk_sender_retries_a_crashed_handoff_with_the_same_uuid_and_ciphertext(
    api,
    workspace_api,
    db,
):
    prepared_message(api, workspace_api, db)
    client = mock.Mock()
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    client.send_event.side_effect = SystemExit("synthetic crash after handoff")
    sender = push_events.WorkspacePushSender()
    sender._notification_client = client
    with pytest.raises(SystemExit):
        sender._iteration()
    first = json.loads(json.dumps(client.send_event.call_args.args[0].event_data.body))
    assert db.execute(
        "SELECT status FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush'"
    ).fetchone() == ("NEW",)
    client.send_event.side_effect = None
    sender._iteration()
    assert client.send_event.call_args.args[0].event_data.body == first
    assert db.execute(
        "SELECT status FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush'"
    ).fetchone() == ("IN_PROGRESS",)


def test_sdk_enqueue_rolls_back_with_its_projection_transaction(api, workspace_api, db):
    _, peer, _, message = prepared_message(api, workspace_api, db)
    original = db.execute("SELECT event_data->'body' FROM gcl_sdk_events").fetchone()[0]
    event = json.loads(json.dumps(original))
    event["uuid"] = str(sys_uuid.uuid4())
    with pytest.raises(RuntimeError, match="synthetic projection rollback"):
        with contexts.Context().session_manager() as session:
            push_events._insert(event, peer)
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM gcl_sdk_events WHERE uuid=%s",
                    (event["uuid"],),
                ).fetchone()["n"]
                == 1
            )
            assert db.execute(
                "SELECT count(*) FROM gcl_sdk_events WHERE uuid=%s", (event["uuid"],)
            ).fetchone() == (0,)
            raise RuntimeError("synthetic projection rollback")
    assert db.execute(
        "SELECT count(*) FROM gcl_sdk_events WHERE uuid=%s", (event["uuid"],)
    ).fetchone() == (0,)
