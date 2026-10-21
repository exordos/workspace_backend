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
from restalchemy.storage.sql import migrations

from workspace.common import mobile_push_opts
from workspace.common import mutation_journal_opts
from workspace.messenger_api import push_outbox
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.tests.integration import conftest
from workspace.tests.integration import test_key_announcements, test_messenger_v2
from workspace.tests.integration import test_mutation_journal
from workspace.services.messenger_workers import mutation_runtime


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
        "/v1/messenger/messages/",
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
@pytest.mark.parametrize("journal_enabled", [False, True])
def test_native_message_snapshot_and_read_cancellation_are_durable(
    api, workspace_api, db, read_scope, delivery_expired, journal_enabled
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
    scope_uuid = {
        "messages": message["uuid"],
        "stream_topics": message["topic_uuid"],
        "streams": stream["uuid"],
    }[read_scope]
    cfg.CONF.set_override(
        "enabled", journal_enabled, group=mutation_journal_opts.DOMAIN
    )
    try:
        read = api.post(
            f"/v1/messenger/{read_scope}/{scope_uuid}/actions/read/invoke", user=peer
        )
        assert read.status_code == 200, read.text
        if journal_enabled:
            runtime = mutation_runtime.MutationRuntime()
            try:
                test_mutation_journal.drain(runtime, db)
            finally:
                runtime.close()
        else:
            test_messenger_v2._drain()
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)
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
    event = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE message_uuid=%s AND user_uuid=%s",
        (message_uuid, peer),
    ).fetchone()[0]
    assert [d["device_uuid"] for d in event["event_params"]["devices"]] == [
        str(good_device)
    ]


@pytest.mark.parametrize(
    "starting_migration",
    [
        "0189-Add-encrypted-mobile-push-outbox-140dd5.py",
        "0251-Join-mobile-push-with-unread-mention-indexes-64bc28.py",
        "0262-Reconvert-stored-Zulip-silent-user-mentions-80e205.py",
    ],
)
def test_applied_push_migration_upgrades_provider_schema_and_keeps_snapshot(
    starting_migration,
):
    with conftest.isolated_migration_database(starting_migration) as database_url:
        snapshot_uuid = sys_uuid.uuid4()
        with psycopg.connect(database_url, autocommit=True) as connection:
            has_push = connection.execute(
                "SELECT to_regclass('messenger_push_outbox') IS NOT NULL"
            ).fetchone()[0]
            if has_push:
                connection.execute(
                    "INSERT INTO messenger_push_outbox "
                    "(uuid, project_id, user_uuid, message_uuid, event) "
                    "VALUES (%s, %s, %s, %s, '{}'::jsonb)",
                    (
                        snapshot_uuid,
                        sys_uuid.uuid4(),
                        sys_uuid.uuid4(),
                        sys_uuid.uuid4(),
                    ),
                )
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.apply_migration(engine.get_latest_migration())
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_tasks') IS NOT NULL"
            ).fetchone()[0]
            assert connection.execute(
                "SELECT to_regclass('messenger_push_outbox') IS NOT NULL, "
                "to_regclass('messenger_mutation_commands') IS NOT NULL"
            ).fetchone() == (True, True)
            if has_push:
                assert (
                    connection.execute(
                        "SELECT uuid FROM messenger_push_outbox WHERE uuid=%s",
                        (snapshot_uuid,),
                    ).fetchone()[0]
                    == snapshot_uuid
                )


@pytest.mark.parametrize("action", ["read", "read_up_to", "delete"])
@pytest.mark.parametrize("journal_enabled", [False, True])
def test_old_delivered_notification_retains_only_route_and_can_be_cancelled(
    api, workspace_api, db, action, journal_enabled
):
    _, peer, _, message = prepared_message(api, workspace_api, db)
    client = mock.Mock()
    client.get.return_value = {"status": "ACTIVE"}
    with (
        mock.patch.object(push_outbox, "notification_client", return_value=client),
        contexts.Context().session_manager() as session,
    ):
        assert push_outbox.process_one(session)
    db.execute(
        "UPDATE messenger_push_outbox SET created_at=NOW()-INTERVAL '15 days', "
        "expires_at=NOW()-INTERVAL '15 days' WHERE user_uuid=%s",
        (peer,),
    )
    db.execute(
        "UPDATE messenger_messages SET created_at=NOW()-INTERVAL '20 days' "
        "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements "
        "WHERE project_id=%s AND uuid=%s)",
        (api.project_id, api.project_id, message["uuid"]),
    )
    # The routine cleanup must not erase an unread message's cancellation route.
    with contexts.Context().session_manager() as session:
        assert not push_outbox.process_one(session)
    retained = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE user_uuid=%s", (peer,)
    ).fetchone()
    assert retained is not None
    assert retained[0]["event_params"]["devices"] == []
    cfg.CONF.set_override(
        "enabled", journal_enabled, group=mutation_journal_opts.DOMAIN
    )
    try:
        if action in ("read", "read_up_to"):
            response = api.post(
                f"/v1/messenger/messages/{message['uuid']}/actions/{action}/invoke",
                user=peer,
            )
            assert response.status_code == 200, response.text
        else:
            response = api.delete(f"/v1/messenger/messages/{message['uuid']}")
            assert response.status_code == 204, response.text
        if journal_enabled:
            runtime = mutation_runtime.MutationRuntime()
            try:
                test_mutation_journal.drain(runtime, db)
            finally:
                runtime.close()
        else:
            test_messenger_v2._drain()
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)
    cancelled = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE user_uuid=%s "
        "AND event->'event_params'->>'kind'='workspace_message_cancelled'",
        (peer,),
    ).fetchall()
    assert len(cancelled) == 1
    assert cancelled[0][0]["event_params"]["message_uuid"] == message["uuid"]
    assert cancelled[0][0]["event_params"]["devices"]


def test_read_up_to_cancels_read_routes_and_preserves_the_newer_unread_alert(
    api, workspace_api, db
):
    stream, peer, _, first = prepared_message(api, workspace_api, db)
    messages = [first]
    for content in ("read boundary", "still unread"):
        response = api.post(
            "/v1/messenger/messages/",
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": first["topic_uuid"],
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        messages.append(response.json())
    test_messenger_v2._drain()
    for message, days in zip(messages[:2], (20, 19)):
        db.execute(
            "UPDATE messenger_messages SET created_at=NOW()-(%s * INTERVAL '1 day') "
            "WHERE project_id=%s AND uuid=(SELECT message_uuid FROM messenger_message_placements "
            "WHERE project_id=%s AND uuid=%s)",
            (days, api.project_id, api.project_id, message["uuid"]),
        )
    response = api.post(
        f"/v1/messenger/messages/{messages[1]['uuid']}/actions/read_up_to/invoke",
        user=peer,
    )
    assert response.status_code == 200, response.text
    test_messenger_v2._drain()
    cancelled = db.execute(
        "SELECT message_uuid FROM messenger_push_outbox WHERE user_uuid=%s "
        "AND event->'event_params'->>'kind'='workspace_message_cancelled'",
        (peer,),
    ).fetchall()
    assert {str(row[0]) for row in cancelled} == {
        message["uuid"] for message in messages[:2]
    }


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
    client.get.return_value = {"status": "ACTIVE"}
    with (
        mock.patch.object(push_outbox, "notification_client", return_value=client),
        contexts.Context().session_manager() as session,
    ):
        assert push_outbox.process_one(session)
    client.get.assert_not_called()
    client.create.assert_not_called()
    row = db.execute(
        "SELECT terminal, event->'event_params'->'devices' FROM messenger_push_outbox WHERE user_uuid=%s",
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
    message = created.json()["uuid"]
    snapshot = db.execute(
        "SELECT event FROM messenger_push_outbox WHERE message_uuid=%s AND user_uuid=%s",
        (message, peer),
    ).fetchone()[0]
    expected = [
        target
        for target in snapshot["event_params"]["devices"]
        if target["device_uuid"] == str(surviving)
    ]
    removed = workspace_api.delete(f"/v1/push_devices/{retired}", user=peer)
    assert removed.status_code == 204, removed.text
    client = mock.Mock()
    cause = mock.Mock()
    cause.response.status_code = 404
    client.get.side_effect = push_outbox.http_exceptions.NotFoundError(cause)
    client.create.return_value = {"status": "ACTIVE"}
    with (
        mock.patch.object(push_outbox, "notification_client", return_value=client),
        contexts.Context().session_manager() as session,
    ):
        assert push_outbox.process_one(
            session
        )  # The earlier message has no active targets.
        assert push_outbox.process_one(session)
    client.create.assert_called_once()
    submitted = client.create.call_args.kwargs["data"]
    assert submitted["uuid"] == snapshot["uuid"]
    assert submitted["event_params"]["devices"] == expected
    assert len(expected) == 1


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
