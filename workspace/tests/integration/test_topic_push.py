# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import base64
import json
import uuid
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from oslo_config import cfg
from gcl_sdk.events.dm import models as sdk_models

from workspace.common import mutation_journal_opts
from workspace.messenger_api import push_events, push_notifications
from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import (
    test_mobile_push,
    test_messenger_v2,
    test_mutation_journal,
)

configured_push = test_mobile_push.configured_push


def prepared_message(api, workspace_api, db):
    return test_mobile_push.prepared_message(api, workspace_api, db, prepare_push=False)


def drain(db):
    if not mutation_journal_opts.enabled():
        test_messenger_v2._drain()
        return
    runtime = mutation_runtime.MutationRuntime()
    try:
        test_mutation_journal.drain(runtime, db)
    finally:
        runtime.close()


def queued(db, user, *, internal=False):
    return [
        row[0]
        for row in db.execute(
            "SELECT event_data->'body' FROM gcl_sdk_events WHERE "
            "COALESCE(event_data->'body'->>'user_uuid',event_data->'body'->'exchange'->>'user_uuid')=%s "
            "AND event_data->>'kind'=%s ORDER BY created_at,uuid",
            (str(user), "WorkspaceTopicSignal" if internal else "WorkspacePush"),
        ).fetchall()
    ]


def expand():
    with mock.patch.object(
        push_notifications.hpke, "seal", wraps=push_notifications.hpke.seal
    ) as seal:
        test_mobile_push.prepare_deliveries()
    return [json.loads(call.args[1]) for call in seal.call_args_list]


@pytest.mark.parametrize("journal", [False, True])
def test_first_unread_anchor_then_last_message_after_full_read(
    api, workspace_api, db, journal
):
    cfg.CONF.set_override("enabled", journal, group=mutation_journal_opts.DOMAIN)
    try:
        stream, user, _, first = prepared_message(api, workspace_api, db)
        second = api.post(
            "/v1/messenger/messages/",
            json={
                "stream_uuid": stream["uuid"],
                "topic_uuid": first["topic_uuid"],
                "payload": {"kind": "markdown", "content": "second private preview"},
            },
        )
        assert second.status_code == 201, second.text
        drain(db)
        parents = queued(db, user, internal=True)
        assert len(parents) == 2
        assert all("unread_count" not in p for p in parents)
        assert "private push preview" not in json.dumps(parents)
        contents = expand()
        assert len(queued(db, user)) == 2
        assert all(
            body["event_params"]["message_uuid"] == first["uuid"]
            for body in queued(db, user)
        )
        assert all(content["text"] == "private push preview" for content in contents)
        assert [content["unread_count"] for content in contents] == [2, 2]

        response = api.post(
            f"/v1/messenger/messages/{first['uuid']}/actions/read/invoke", user=user
        )
        assert response.status_code == 200, response.text
        drain(db)
        (content,) = expand()
        assert content["unread_count"] == 1
        assert content["text"] == "second private preview"
        assert (
            queued(db, user)[-1]["event_params"]["message_uuid"]
            == second.json()["uuid"]
        )

        response = api.post(
            f"/v1/messenger/stream_topics/{first['topic_uuid']}/actions/read/invoke",
            user=user,
        )
        assert response.status_code == 200, response.text
        drain(db)
        (content,) = expand()
        assert content["unread_count"] == 0
        assert (
            queued(db, user)[-1]["event_params"]["message_uuid"]
            == second.json()["uuid"]
        )
        assert (
            api.post(
                f"/v1/messenger/stream_topics/{first['topic_uuid']}/actions/read/invoke",
                user=user,
            ).status_code
            == 200
        )
        drain(db)
        assert expand() == []
        assert len(queued(db, user)) == 4
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def test_counter_only_enqueues_minimal_sdk_data(api, workspace_api, db):
    with (
        mock.patch.object(
            push_events,
            "_devices",
            side_effect=AssertionError("device query on counter path"),
        ),
        mock.patch.object(
            push_notifications.hpke,
            "seal",
            side_effect=AssertionError("encryption on counter path"),
        ),
    ):
        _, user, _, _ = prepared_message(api, workspace_api, db)
    (parent,) = queued(db, user, internal=True)
    assert queued(db, user) == []
    assert "private push preview" not in json.dumps(parent)
    assert set(parent) == {
        "project_id",
        "user_uuid",
        "stream_uuid",
        "topic_uuid",
        "source_uuid",
        "previous_active_count",
    }
    (content,) = expand()
    (child,) = queued(db, user)
    assert content["epoch_version"] > 0
    assert uuid.UUID(content["epoch_generation"])
    assert "private push preview" not in json.dumps(child)
    wire = push_events.PushEventHttpClient("https://notification.invalid", "unused")
    sender = push_events.WorkspacePushSender()
    event = next(e for e in sender._fetch_new_events())
    assert "_membership_generation" not in wire._build_event_body(event)


def test_http_client_rejects_unprepared_internal_topic(api, workspace_api, db):
    prepared_message(api, workspace_api, db)
    sender = push_events.WorkspacePushSender()
    with pytest.raises(push_events.PushDeliveryError, match="not been encrypted"):
        sender._notification_client._build_event_body(
            push_events.fetch_events(
                "NEW", push_events.event_payloads.WorkspaceTopicSignal.KIND
            )[0]
        )


def test_each_device_fanout_runs_in_handler(api, workspace_api, db):
    stream, user, first_device, first = prepared_message(api, workspace_api, db)
    device, key = uuid.uuid4(), x25519.X25519PrivateKey.generate()
    response = workspace_api.put(
        f"/v1/push_devices/{device}",
        user=user,
        json={
            "transport": "fcm",
            "platform": "android",
            "registration_token": "second-device-token",
            "encryption": {
                "kind": "HPKE",
                "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
                "key_uuid": str(uuid.uuid4()),
                "public_key": base64.urlsafe_b64encode(
                    key.public_key().public_bytes_raw()
                )
                .rstrip(b"=")
                .decode(),
            },
        },
    )
    assert response.status_code == 201, response.text
    assert len(queued(db, user, internal=True)) == 1
    contents = expand()
    children = queued(db, user)
    assert len(contents) == len(children) == 2
    assert {c["event_params"]["devices"][0]["device_uuid"] for c in children} == {
        str(device),
        str(first_device),
    }
    source = uuid.UUID(queued(db, user, internal=True)[0]["source_uuid"])
    for child in children:
        target = child["event_params"]["devices"][0]
        assert child["uuid"] == str(
            uuid.uuid5(source, f"{user}:{target['device_uuid']}")
        )
    assert (
        len(
            {c["event_params"]["devices"][0]["content"]["ciphertext"] for c in children}
        )
        == 2
    )
    assert all(c["text"] == "private push preview" for c in contents)


@pytest.mark.parametrize(
    "mode,topic_mode,mention,expected",
    [
        ("muted", "default", False, 0),
        ("muted", "default", True, 0),
        ("all_messages", "mute", True, 0),
        ("mentions_only", "default", False, 0),
        ("mentions_only", "default", True, 1),
        ("all_messages", "default", False, 1),
    ],
)
def test_mute_and_mentions_only(
    api, workspace_api, db, mode, topic_mode, mention, expected
):
    stream, user, _, first = prepared_message(api, workspace_api, db)
    for resource, identity, value in [
        ("streams", stream["uuid"], mode),
        ("stream_topics", first["topic_uuid"], topic_mode),
    ]:
        response = api.post(
            f"/v1/messenger/{resource}/{identity}/actions/notifications/invoke",
            user=user,
            json={"notification_mode": value},
        )
        assert response.status_code == 200, response.text
    drain(db)
    db.execute("DELETE FROM gcl_sdk_events")
    text = f"Hello [recipient](urn:user:{user})" if mention else "Not a mention"
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": first["topic_uuid"],
            "payload": {"kind": "markdown", "content": text},
        },
    )
    assert response.status_code == 201, response.text
    drain(db)
    assert len(expand()) == expected
    assert len(queued(db, user)) == expected
    if expected and mode == "mentions_only":
        response = api.post(
            f"/v1/messenger/stream_topics/{first['topic_uuid']}/actions/read/invoke",
            user=user,
        )
        assert response.status_code == 200, response.text
        drain(db)
        (zero,) = expand()
        assert zero["active_unread_count"] == 0


@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize(
    "action", ["read", "read_up_to", "topic_read", "stream_read", "delete"]
)
def test_zero_does_not_depend_on_old_delivery_rows(
    api, workspace_api, db, journal, action
):
    cfg.CONF.set_override("enabled", journal, group=mutation_journal_opts.DOMAIN)
    try:
        stream, user, _, message = prepared_message(api, workspace_api, db)
        drain(db)
        db.execute("DELETE FROM gcl_sdk_events")
        if action == "delete":
            response = api.delete(f"/v1/messenger/messages/{message['uuid']}")
        else:
            path = {
                "read": f"messages/{message['uuid']}/actions/read/invoke",
                "read_up_to": f"messages/{message['uuid']}/actions/read_up_to/invoke",
                "topic_read": f"stream_topics/{message['topic_uuid']}/actions/read/invoke",
                "stream_read": f"streams/{stream['uuid']}/actions/read/invoke",
            }[action]
            response = api.post("/v1/messenger/" + path, user=user)
        assert response.status_code in (200, 204), response.text
        drain(db)
        (zero,) = expand()
        assert zero["unread_count"] == 0
        assert queued(db, user)[0]["event_params"]["message_uuid"] == (
            None if action == "delete" else message["uuid"]
        )
    finally:
        cfg.CONF.clear_override("enabled", group=mutation_journal_opts.DOMAIN)


def test_handler_waits_for_existing_broadcast_publication(api, workspace_api, db):
    _, user, _, _ = prepared_message(api, workspace_api, db)
    source = queued(db, user, internal=True)[0]["source_uuid"]
    db.execute(
        "CREATE TEMP TABLE topic_publication_backup AS SELECT * FROM m_workspace_broadcast_message_events_v1 WHERE uuid=%s",
        (source,),
    )
    db.execute(
        "DELETE FROM m_workspace_broadcast_message_events_v1 WHERE uuid=%s", (source,)
    )
    assert expand() == []
    assert db.execute("SELECT status FROM gcl_sdk_events").fetchone() == ("NEW",)
    db.execute(
        "INSERT INTO m_workspace_broadcast_message_events_v1 SELECT * FROM topic_publication_backup"
    )
    assert len(expand()) == len(queued(db, user)) == 1


def test_handler_rejects_previous_membership_before_fanout(api, workspace_api, db):
    stream, user, _, _ = prepared_message(api, workspace_api, db)
    db.execute(
        "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 WHERE stream_uuid=%s AND user_uuid=%s",
        (stream["uuid"], user),
    )
    assert expand() == []
    assert queued(db, user) == []


@pytest.mark.parametrize("membership_changed", [False, True])
def test_encrypted_retry_retains_membership_fence(
    api, workspace_api, db, membership_changed
):
    stream, user, _, _ = prepared_message(api, workspace_api, db)
    expand()
    client = mock.Mock()
    client.send_event.side_effect = push_events.PushDeliveryError("transient failure")
    test_mobile_push._run_sender(client)
    (captured,) = queued(db, user)
    if membership_changed:
        db.execute(
            "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 WHERE stream_uuid=%s AND user_uuid=%s",
            (stream["uuid"], user),
        )
    client.reset_mock(side_effect=True)
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    test_mobile_push._run_sender(client)
    if membership_changed:
        client.send_event.assert_not_called()
    else:
        client.send_event.assert_called_once()
        assert client.send_event.call_args.args[0].event_data.body == captured


def test_history_recount_is_silent(api, workspace_api, db):
    _, user, _, _ = prepared_message(api, workspace_api, db)
    db.execute("DELETE FROM gcl_sdk_events")
    try:
        db.execute("SET workspace.zulip_history='on'")
        db.execute(
            "DELETE FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s",
            (api.project_id, user),
        )
    finally:
        db.execute("RESET workspace.zulip_history")
    drain(db)
    assert queued(db, user, internal=True) == []
    assert expand() == []


def test_failed_fanout_transaction_keeps_the_topic_pending(api, workspace_api, db):
    _, user, _, _ = prepared_message(api, workspace_api, db)
    parent_uuid = str(push_events.fetch_events("NEW", "WorkspaceTopicSignal")[0].uuid)
    save = sdk_models.Event.save
    failed = []

    def fail_completion(event, *args, **kwargs):
        if str(event.uuid) == parent_uuid and event.status == "ACTIVE" and not failed:
            failed.append(True)
            raise RuntimeError("synthetic completion failure")
        return save(event, *args, **kwargs)

    client = mock.Mock()
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    sender = push_events.WorkspacePushSender()
    sender._notification_client = client
    with mock.patch.object(sdk_models.Event, "save", fail_completion):
        test_mobile_push.prepare_deliveries()
    assert failed
    assert queued(db, user) == []
    assert db.execute(
        "SELECT status FROM gcl_sdk_events WHERE uuid=%s", (parent_uuid,)
    ).fetchone() == ("NEW",)
    test_mobile_push.prepare_deliveries()
    sender._iteration()
    client.send_event.assert_called_once()


def test_delayed_signal_uses_current_counts_and_first_unread(api, workspace_api, db):
    stream, user, _, first = prepared_message(api, workspace_api, db)
    second = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": first["topic_uuid"],
            "payload": {"kind": "markdown", "content": "current unread preview"},
        },
    )
    assert second.status_code == 201, second.text
    drain(db)
    assert (
        api.post(
            f"/v1/messenger/messages/{first['uuid']}/actions/read/invoke", user=user
        ).status_code
        == 200
    )
    drain(db)
    contents = expand()
    assert contents
    assert all(c["unread_count"] == c["active_unread_count"] == 1 for c in contents)
    assert all(c["text"] == "current unread preview" for c in contents)
    assert all(
        c["event_params"]["message_uuid"] == second.json()["uuid"]
        for c in queued(db, user)
    )


def test_read_before_signal_handler_never_recreates_an_unread_card(
    api, workspace_api, db
):
    _, user, _, first = prepared_message(api, workspace_api, db)
    assert (
        api.post(
            f"/v1/messenger/messages/{first['uuid']}/actions/read/invoke", user=user
        ).status_code
        == 200
    )
    drain(db)
    contents = expand()
    assert contents
    assert all(c["unread_count"] == c["active_unread_count"] == 0 for c in contents)


def test_sdk_sender_only_consumes_prepared_device_events(api, workspace_api, db):
    _, user, _, _ = prepared_message(api, workspace_api, db)
    client = mock.Mock()
    client.get_event.return_value = {"status": "IN_PROGRESS"}
    sender = push_events.WorkspacePushSender()
    sender._notification_client = client
    sender._iteration()
    client.send_event.assert_not_called()
    assert db.execute("SELECT status FROM gcl_sdk_events").fetchone() == ("NEW",)
    test_mobile_push.prepare_deliveries()
    sender._iteration()
    client.send_event.assert_called_once()
    assert queued(db, user)


def test_handler_waits_for_pending_counter_before_reading_preview(
    api, workspace_api, db
):
    _, user, _, first = prepared_message(api, workspace_api, db)
    # Remove the unread fact without draining the dirty counter scope yet.
    assert (
        api.post(
            f"/v1/messenger/messages/{first['uuid']}/actions/read/invoke", user=user
        ).status_code
        == 200
    )
    assert expand() == []
    assert db.execute("SELECT status FROM gcl_sdk_events").fetchone() == ("NEW",)
    drain(db)
    contents = expand()
    assert contents and all(c["unread_count"] == 0 for c in contents)


def test_current_state_uses_current_topic_epoch_not_the_signal_epoch(
    api, workspace_api, db
):
    stream, user, _, first = prepared_message(api, workspace_api, db)
    assert (
        api.post(
            f"/v1/messenger/messages/{first['uuid']}/actions/read/invoke", user=user
        ).status_code
        == 200
    )
    drain(db)
    second = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": first["topic_uuid"],
            "payload": {"kind": "markdown", "content": "M2 current preview"},
        },
    )
    assert second.status_code == 201, second.text
    drain(db)
    latest = db.execute(
        "SELECT max(event.epoch_version) FROM m_workspace_broadcast_message_events_v1 event JOIN m_workspace_event_audience_members_v1 audience ON audience.audience_snapshot_uuid=event.audience_snapshot_uuid WHERE event.project_id=%s AND audience.user_uuid=%s AND event.entity_uuid=%s AND event.object_type='topic' AND event.action='updated'",
        (api.project_id, user, first["topic_uuid"]),
    ).fetchone()[0]
    contents = expand()
    assert len(contents) == 3
    assert all(c["epoch_version"] == latest for c in contents)
    assert all(
        c["unread_count"] == 1 and c["text"] == "M2 current preview" for c in contents
    )
    assert all(
        c["event_params"]["message_uuid"] == second.json()["uuid"]
        for c in queued(db, user)
    )


def test_handler_resumes_migrated_in_progress_signal(api, workspace_api, db):
    _, user, _, _ = prepared_message(api, workspace_api, db)
    db.execute(
        "UPDATE gcl_sdk_events SET status='IN_PROGRESS' WHERE event_data->>'kind'='WorkspaceTopicSignal'"
    )
    assert len(expand()) == 1
    assert queued(db, user)
    assert db.execute(
        "SELECT status FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspaceTopicSignal'"
    ).fetchone() == ("ACTIVE",)


def test_handler_waits_for_current_topic_publication(api, workspace_api, db):
    _, user, _, first = prepared_message(api, workspace_api, db)
    pending = uuid.uuid4()
    db.execute(
        """INSERT INTO messenger_publication_outbox(uuid,project_id,entity_uuid,kind,recipients,payload,priority)
        SELECT %s,project_id,entity_uuid,'topic.updated',ARRAY[%s::uuid],payload,0
        FROM m_workspace_broadcast_message_events_v1
        WHERE project_id=%s AND entity_uuid=%s AND object_type='topic'
        ORDER BY epoch_version DESC LIMIT 1""",
        (pending, user, api.project_id, first["topic_uuid"]),
    )
    assert expand() == []
    assert queued(db, user) == []
    db.execute("DELETE FROM messenger_publication_outbox WHERE uuid=%s", (pending,))
    assert len(expand()) == 1
