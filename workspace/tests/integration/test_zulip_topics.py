# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
from unittest import mock
import uuid

import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import test_zulip_messages as history_tests
from workspace.tests.integration import test_zulip_message_states as state_tests
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests


history_account = history_tests.history_account
outgoing = outgoing_tests.outgoing


@pytest.mark.parametrize("name", ["", "A" * 60, "✔literal"])
def test_done_roundtrip_preserves_full_base_name_and_message_identity(
    history_account, monkeypatch, api, db, name
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data(subject=name))
    source = history_tests._messages(account)[0]
    identity = db.execute(
        "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s",
        (account.project_id,),
    ).fetchone()
    checked = ("✔ " + name)[:60]
    for identifier, original, subject, done in [
        (1, name, checked, True),
        (2, checked, checked.removeprefix("✔ "), False),
    ]:
        tasks = _topic_event(
            account,
            {
                "id": identifier,
                "type": "update_message",
                "message_id": 42,
                "message_ids": [42],
                "stream_id": 42,
                "orig_subject": original,
                "subject": subject,
                "propagate_mode": "change_all",
                "edit_timestamp": int(
                    datetime.datetime.now(datetime.timezone.utc).timestamp()
                ),
            },
        )
        for task in tasks:
            with contexts.Context().session_manager():
                task.execute()
            assert task.status == "done", task.error_description
        result = _topic(api, account, source.channel.stream_uuid, name)
        assert result.status_code == 200, result.text
        assert result.json()["name"] == (name or "General Topic")
        assert result.json()["is_done"] is done
        assert (
            db.execute(
                "SELECT uuid,message_uuid FROM messenger_message_placements WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()
            == identity
        )


def test_reference_batches_preserve_old_links_and_new_uses_of_a_recreated_topic(
    history_account, monkeypatch, api, db
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data())
    stream = history_tests._messages(account)[0].channel.stream_uuid
    old, new = uuid.uuid5(stream, "General"), uuid.uuid5(stream, "Renamed")
    db.execute(
        "INSERT INTO messenger_messages(uuid,project_id,author_uuid,payload) "
        "SELECT gen_random_uuid(),%s,%s,jsonb_build_object('kind','markdown','content',%s::text) FROM generate_series(1,150)",
        (account.project_id, account.owner_user_uuid, f"[Old](urn:topic:{old})"),
    )
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": 42,
            "message_ids": [42],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "Renamed",
            "propagate_mode": "change_all",
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    with contexts.Context().session_manager():
        tasks[0].execute()
    resolved = _topic(api, account, stream, "General")
    assert resolved.status_code == 200 and resolved.json()["uuid"] == str(new)

    def post_link(topic, content):
        response = api.post(
            "/v1/messenger/messages/",
            project=account.project_id,
            user=account.owner_user_uuid,
            json={
                "stream_uuid": str(stream),
                "topic_uuid": str(topic),
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    assert (
        post_link(new, f"[Old](urn:topic:{old})")["payload"]["content"]
        == f"[Old](urn:topic:{new})"
    )
    recreated = api.post(
        "/v1/messenger/stream_topics/",
        project=account.project_id,
        user=account.owner_user_uuid,
        json={
            "stream_uuid": str(stream),
            "name": "General",
            "source": {"kind": "native"},
        },
    )
    assert recreated.status_code == 201, recreated.text
    fresh = post_link(old, f"[Fresh](urn:topic:{old})")
    queued = models.Task.objects.get_all(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "rewrite_topic_references"}),
            "status": filters.EQ("new"),
        }
    )
    assert len(queued) == 1
    with contexts.Context().session_manager():
        queued[0].execute()
    response = api.get(
        f"/v1/messenger/messages/{fresh['uuid']}",
        project=account.project_id,
        user=account.owner_user_uuid,
    )
    assert response.json()["payload"]["content"] == f"[Fresh](urn:topic:{old})"
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_messages WHERE project_id=%s AND payload->>'content'=%s",
            (account.project_id, f"[Old](urn:topic:{new})"),
        ).fetchone()[0]
        == 151
    )


def _import(account, monkeypatch, *messages):
    zulip = mock.Mock()
    zulip.get_messages.return_value = list(messages)
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    assert history_tests._import_history(account).status == "done"
    state_tests._prepare(account)
    with contexts.Context().session_manager():
        while models.ZulipMessageState.reconcile_batch(
            100, project_id=account.project_id
        ):
            pass
    return zulip


def _topic(api, account, stream, name):
    return api.get(
        f"/v1/messenger/stream_topics/{uuid.uuid5(stream, name)}",
        project=account.project_id,
        user=account.owner_user_uuid,
    )


@pytest.mark.parametrize("subject,done", [("✔ General", True), ("General", False)])
def test_imported_topic_separates_service_marker_from_identity_and_state(
    history_account,
    monkeypatch,
    api,
    subject,
    done,
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data(subject=subject))
    (source,) = history_tests._messages(account)
    response = _topic(api, account, source.channel.stream_uuid, "General")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "General"
    assert response.json()["is_done"] is done


def _topic_event(account, payload):
    account.settings = {
        **account.settings,
        "email": account.settings.get("email", "topic-owner@example.com"),
        "api_key": account.settings.get("api_key", "test-key"),
    }
    account.status = "active"
    account.update()
    subscription = models.RealtimeSubscription.for_account(account)
    if subscription is None:
        subscription = models.RealtimeSubscription(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            generation=uuid.uuid4(),
            queue_id="topic-events",
            last_event_id=-1,
            account_revision=account.subscription_revision(),
            longpoll_timeout=90,
        )
        subscription.insert()
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account, subscription.generation, [payload]
        )
        event = models.RealtimeEvent.objects.get_one(
            filters={
                "uuid": filters.EQ(
                    uuid.uuid5(subscription.generation, str(payload["id"]))
                )
            }
        )
        event.dispatch()
    return models.Task.objects.get_all(
        filters={"realtime_event_uuid": filters.EQ(event.uuid)}
    )


@pytest.mark.parametrize("mode", ["done", "rename", "change_one"])
def test_provider_topic_events_apply_to_messages_sent_by_workspace(outgoing, db, mode):
    api, account, channel, remote, _zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    delivery = models.MessageDelivery.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    delivery.zulip_message_id = remote["id"]
    delivery.delivered_at = datetime.datetime.now(datetime.timezone.utc)
    delivery.update()
    old = uuid.UUID(public["topic_uuid"])
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": remote["id"],
            "message_ids": [remote["id"]],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "✔ General" if mode == "done" else "Renamed",
            "propagate_mode": "change_one" if mode == "change_one" else "change_all",
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    for task in tasks:
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done", task.error_description
    expected = old if mode == "done" else uuid.uuid5(channel.stream_uuid, "Renamed")
    response = api.get(f"/v1/messenger/stream_topics/{expected}")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == ("General" if mode == "done" else "Renamed")
    assert response.json()["is_done"] is (mode == "done")
    placement = db.execute(
        "SELECT topic_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (account.project_id, uuid.UUID(public["uuid"])),
    ).fetchone()
    assert placement[0] == expected


def test_older_resolve_cannot_override_a_reopen_in_the_same_timestamp_second(
    history_account,
    monkeypatch,
    api,
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data())
    source = history_tests._messages(account)[0]
    common = {
        "type": "update_message",
        "message_id": 42,
        "message_ids": [42],
        "stream_id": 42,
        "propagate_mode": "change_all",
        "edit_timestamp": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
    }
    older = _topic_event(
        account, {**common, "id": 1, "orig_subject": "General", "subject": "✔ General"}
    )[0]
    newer = _topic_event(
        account, {**common, "id": 2, "orig_subject": "✔ General", "subject": "General"}
    )[0]
    for task in (newer, older):
        with contexts.Context().session_manager():
            task.execute()
    response = _topic(api, account, source.channel.stream_uuid, "General")
    assert response.status_code == 200, response.text
    assert response.json()["is_done"] is False


@pytest.mark.parametrize("rename", [False, True])
def test_workspace_topic_changes_enqueue_one_provider_intent_and_apply_after_success(
    history_account,
    monkeypatch,
    api,
    rename,
):
    account = history_account
    zulip = _import(account, monkeypatch, history_tests._message_data())
    source = history_tests._messages(account)[0]
    old = uuid.uuid5(source.channel.stream_uuid, "General")
    _topic_event(account, {"id": 0, "type": "heartbeat"})
    path = f"/v1/messenger/stream_topics/{old}"
    response = (
        api.put(
            path,
            project=account.project_id,
            user=account.owner_user_uuid,
            json={"name": "Renamed"},
        )
        if rename
        else api.post(
            path + "/actions/toggle_done/invoke",
            project=account.project_id,
            user=account.owner_user_uuid,
            json={},
        )
    )
    assert response.status_code == 200, response.text
    assert response.json()["uuid"] == str(old)
    tasks = models.Task.objects.get_all(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "update_topic"}),
        }
    )
    assert len(tasks) == 1
    zulip.move_topic.assert_not_called()
    # Pending provider state cannot undo the optimistic local title/done flag.
    with contexts.Context().session_manager():
        models.ZulipTopicState.reconcile_batch(100)
    pending = api.get(path, project=account.project_id, user=account.owner_user_uuid)
    assert pending.json()["name"] == ("Renamed" if rename else "General")
    assert pending.json()["is_done"] is (not rename)
    with contexts.Context().session_manager():
        tasks[0].execute()
    stored = models.Task.objects.get_one(filters={"uuid": filters.EQ(tasks[0].uuid)})
    assert stored.status == "done", stored.error_description
    zulip.move_topic.assert_called_once_with(42, "Renamed" if rename else "✔ General")
    final = _topic(
        api, account, source.channel.stream_uuid, "Renamed" if rename else "General"
    )
    assert final.status_code == 200, final.text
    assert final.json()["name"] == ("Renamed" if rename else "General")
    assert final.json()["is_done"] is (not rename)


def test_workspace_creates_one_canonical_topic_without_a_service_marker_in_its_name(
    history_account,
    monkeypatch,
    api,
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data())
    stream = history_tests._messages(account)[0].channel.stream_uuid
    for name in ("✔ Fresh", "Fresh"):
        response = api.post(
            "/v1/messenger/stream_topics/",
            project=account.project_id,
            user=account.owner_user_uuid,
            json={
                "stream_uuid": str(stream),
                "name": name,
                "source": {"kind": "native"},
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["uuid"] == str(uuid.uuid5(stream, "Fresh"))
        assert response.json()["name"] == "Fresh"
        assert response.json()["is_done"] is True


def test_provider_echo_can_replace_local_cache_time_with_its_event_watermark(
    history_account, monkeypatch, api, db
):
    account = history_account
    _import(account, monkeypatch, history_tests._message_data())
    source = history_tests._messages(account)[0]
    second = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    db.execute(
        "UPDATE bridge_zulip_v5_messages SET placement_updated_at=%s WHERE uuid=%s",
        (
            datetime.datetime.fromtimestamp(second + 0.25, datetime.timezone.utc),
            source.uuid,
        ),
    )
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": 42,
            "message_ids": [42],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "✔ General",
            "propagate_mode": "change_all",
            "edit_timestamp": second,
        },
    )
    with contexts.Context().session_manager():
        tasks[0].execute()
    assert (
        _topic(api, account, source.channel.stream_uuid, "General").json()["is_done"]
        is True
    )
    assert db.execute(
        "SELECT placement_event_uuid FROM bridge_zulip_v5_messages WHERE uuid=%s",
        (source.uuid,),
    ).fetchone()[0]


def test_conversion_uses_canonical_identity_and_closed_provider_name(
    history_account, monkeypatch
):
    account = history_account
    zulip = _import(
        account, monkeypatch, history_tests._message_data(subject="✔ General")
    )
    source = history_tests._messages(account)[0]
    canonical = uuid.uuid5(source.channel.stream_uuid, "General")
    with contexts.Context().session_manager():
        conversion = content_conversion.ConversionContext(account)
        incoming = conversion.to_workspace(
            history_tests._message_data(43, content="#**Channel 42>✔ General**"),
            source.channel.stream_uuid,
            zulip,
        )
        outgoing = conversion.to_zulip(
            {"kind": "markdown", "content": f"[Topic](urn:topic:{canonical})"},
            source.channel.stream_uuid,
            zulip,
        )
    assert f"urn:topic:{canonical}" in incoming
    assert "/topic/.E2.9C.94.20General" in outgoing


def test_new_workspace_message_stays_in_the_closed_provider_topic(outgoing, db):
    api, account, channel, _remote, zulip, _constructor = outgoing
    db.execute(
        "UPDATE messenger_topics SET is_done=true WHERE project_id=%s AND stream_uuid=%s",
        (account.project_id, channel.stream_uuid),
    )
    outgoing_tests.create(outgoing)
    outgoing_tests.dispatch()
    delivery = models.MessageDelivery.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert delivery.topic_name == "✔ General"


def test_topic_action_uses_the_actor_account_for_messages_sent_by_another_workspace_member(
    outgoing, db
):
    api, account, channel, remote, zulip, constructor = outgoing
    other_user = uuid.uuid4()
    outgoing_tests.conftest.seed_workspace_user(
        db, other_user, f"topic-sender-{other_user}"
    )
    outgoing_tests.conftest.seed_user_stream_binding(
        db, account.project_id, channel.stream_uuid, other_user
    )
    original_topic = db.execute(
        "SELECT default_topic_uuid FROM messenger_streams WHERE project_id=%s AND uuid=%s",
        (account.project_id, channel.stream_uuid),
    ).fetchone()[0]
    v2_models.WorkspaceUserTopicBinding(
        project_id=account.project_id, user_uuid=other_user, topic_uuid=original_topic
    ).insert()
    other = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=other_user,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "other@example.com",
            "api_key": "other-key",
        },
    )
    other.insert()
    models.ZulipChannel(
        external_account_uuid=other.uuid,
        stream_uuid=channel.stream_uuid,
        stream_id=42,
        name="Channel",
        description="",
        rendered_description="",
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    ).insert()
    response = api.post(
        "/v1/messenger/messages/",
        user=other_user,
        json={
            "stream_uuid": str(channel.stream_uuid),
            "payload": {"kind": "markdown", "content": "Another sender"},
        },
    )
    assert response.status_code == 201, response.text
    outgoing_tests.dispatch()
    delivery = models.MessageDelivery.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert delivery.external_account_uuid == other.uuid
    delivery.zulip_message_id = remote["id"]
    delivery.update()
    changed = api.put(
        f"/v1/messenger/stream_topics/{response.json()['topic_uuid']}",
        user=account.owner_user_uuid,
        json={"name": "Renamed"},
    )
    assert changed.status_code == 200, changed.text
    task = models.Task.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "update_topic"}),
        }
    )
    assert task.external_account_uuid == account.uuid
    assert task.content.origins == [{"name": "General", "message_id": remote["id"]}]
    constructor.reset_mock()
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done", task.error_description
    assert constructor.call_args.args[0].uuid == account.uuid
    zulip.move_topic.assert_called_once_with(remote["id"], "Renamed")
    other.delete()


def test_closed_topic_notification_targets_its_checked_provider_name(
    history_account, monkeypatch, api
):
    account = history_account
    zulip = _import(
        account, monkeypatch, history_tests._message_data(subject="✔ General")
    )
    source = history_tests._messages(account)[0]
    source.channel.topic_notifications_synced = True
    source.channel.update()
    _topic_event(account, {"id": 0, "type": "heartbeat"})
    canonical = uuid.uuid5(source.channel.stream_uuid, "General")
    response = api.post(
        f"/v1/messenger/stream_topics/{canonical}/actions/notifications/invoke",
        project=account.project_id,
        user=account.owner_user_uuid,
        json={"notification_mode": "follow"},
    )
    assert response.status_code == 200, response.text
    with contexts.Context().session_manager():
        models.ZulipTopicState.reconcile_batch(100)
    assert (
        _topic(api, account, source.channel.stream_uuid, "General").json()[
            "notification_mode"
        ]
        == "follow"
    )
    task = models.Task.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "update_notifications"}),
        }
    )
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done", task.error_description
    zulip.update_topic_notifications.assert_called_once_with(42, "✔ General", 3)


def test_full_provider_variant_move_does_not_move_its_open_canonical_sibling(
    history_account, monkeypatch, api, db
):
    account = history_account
    _import(
        account,
        monkeypatch,
        history_tests._message_data(42),
        history_tests._message_data(43, subject="✔ General"),
    )
    source = history_tests._messages(account)[0]
    old = uuid.uuid5(source.channel.stream_uuid, "General")
    linked = api.post(
        "/v1/messenger/messages/",
        project=account.project_id,
        user=account.owner_user_uuid,
        json={
            "stream_uuid": str(source.channel.stream_uuid),
            "topic_uuid": str(old),
            "payload": {
                "kind": "markdown",
                "content": f"[Discussion](urn:topic:{old})",
            },
        },
    )
    assert linked.status_code == 201
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": 43,
            "message_ids": [43],
            "stream_id": 42,
            "orig_subject": "✔ General",
            "subject": "Renamed",
            "propagate_mode": "change_all",
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    for task in tasks:
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done", task.error_description
    original = _topic(api, account, source.channel.stream_uuid, "General")
    assert original.status_code == 200 and original.json()["is_done"] is False
    link = api.get(
        f"/v1/messenger/messages/{linked.json()['uuid']}",
        project=account.project_id,
        user=account.owner_user_uuid,
    )
    assert link.json()["topic_uuid"] == str(old)
    assert link.json()["payload"]["content"] == f"[Discussion](urn:topic:{old})"


def test_provider_permission_failure_restores_local_state_and_does_not_rekey(
    history_account, monkeypatch, api, db
):
    account = history_account
    zulip = _import(account, monkeypatch, history_tests._message_data())
    source = history_tests._messages(account)[0]
    old = uuid.uuid5(source.channel.stream_uuid, "General")
    _topic_event(account, {"id": 0, "type": "heartbeat"})
    response = api.put(
        f"/v1/messenger/stream_topics/{old}",
        project=account.project_id,
        user=account.owner_user_uuid,
        json={"name": "Denied"},
    )
    assert response.status_code == 200
    error = requests.Response()
    error.status_code = 403
    error._content = (
        b'{"result":"error","code":"FORBIDDEN","msg":"Insufficient permission"}'
    )
    zulip.move_topic.side_effect = requests.HTTPError(response=error)
    task = models.Task.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "update_topic"}),
        }
    )
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "error"
    restored = _topic(api, account, source.channel.stream_uuid, "General")
    assert restored.status_code == 200
    assert restored.json()["name"] == "General"
    assert not db.execute(
        "SELECT 1 FROM messenger_topics WHERE project_id=%s AND uuid=%s",
        (account.project_id, uuid.uuid5(source.channel.stream_uuid, "Denied")),
    ).fetchone()


@pytest.mark.parametrize("message_count", [3, 118])
def test_bulk_resolve_uses_one_topic_task_and_does_not_reimport_message_content(
    history_account,
    monkeypatch,
    api,
    db,
    message_count,
):
    account = history_account
    ids = list(range(40, 40 + message_count))
    zulip = _import(
        account,
        monkeypatch,
        *[history_tests._message_data(identifier) for identifier in ids],
    )
    source = history_tests._messages(account)[0]
    before = db.execute(
        "SELECT uuid,payload,content_hash,content_updated_at,reaction_users FROM messenger_messages "
        "WHERE project_id=%s ORDER BY uuid",
        (account.project_id,),
    ).fetchall()
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": ids[0],
            "message_ids": ids,
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "✔ General",
            "propagate_mode": "change_all",
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    assert [task.content.kind for task in tasks] == ["apply_topic_event"]
    assert len(before) == message_count
    zulip.get_messages.reset_mock()
    with contexts.Context().session_manager():
        tasks[0].execute()
    zulip.get_messages.assert_not_called()
    response = _topic(api, account, source.channel.stream_uuid, "General")
    assert response.status_code == 200, response.text
    assert response.json()["is_done"] is True
    assert (
        db.execute(
            "SELECT uuid,payload,content_hash,content_updated_at,reaction_users FROM messenger_messages "
            "WHERE project_id=%s ORDER BY uuid",
            (account.project_id,),
        ).fetchall()
        == before
    )
    for table in ("bridge_zulip_v5_message_changes", "messenger_message_changes"):
        assert (
            db.execute(
                f"SELECT count(*) FROM {table} WHERE project_id=%s",
                (account.project_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "mode,moved,cross_channel",
    [
        ("change_one", [41], False),
        ("change_later", [41, 42], False),
        ("change_all", [40, 41, 42], False),
        ("change_all", [40, 41, 42], True),
    ],
)
def test_topic_movement_keeps_messages_and_updates_links_only_for_full_moves(
    history_account,
    monkeypatch,
    api,
    db,
    mode,
    moved,
    cross_channel,
):
    account = history_account
    if cross_channel:
        destination = history_tests._channel(account, 43)
    _import(
        account,
        monkeypatch,
        *[history_tests._message_data(identifier) for identifier in [40, 41, 42]],
    )
    source = history_tests._messages(account)[0]
    source_uuid = uuid.uuid5(source.channel.stream_uuid, "General")
    link = api.post(
        "/v1/messenger/messages/",
        project=account.project_id,
        user=account.owner_user_uuid,
        json={
            "stream_uuid": str(source.channel.stream_uuid),
            "topic_uuid": str(source_uuid),
            "payload": {
                "kind": "markdown",
                "content": f"[Discussion](urn:topic:{source_uuid})",
            },
        },
    )
    assert link.status_code == 201, link.text
    original = db.execute(
        "SELECT message_id, messenger_uuid_v5(project_id,message_uuid::text) FROM bridge_zulip_v5_messages WHERE external_account_uuid=%s",
        (account.uuid,),
    ).fetchall()
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": moved[0],
            "message_ids": moved,
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "Renamed",
            "propagate_mode": mode,
            **({"new_stream_id": 43} if cross_channel else {}),
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    for task in tasks:
        with contexts.Context().session_manager():
            task.execute()
    destination_stream = (
        destination.stream_uuid if cross_channel else source.channel.stream_uuid
    )
    destination_uuid = uuid.uuid5(destination_stream, "Renamed")
    placements = dict(
        db.execute(
            "SELECT canonical.uuid,placement.topic_uuid FROM messenger_messages AS canonical "
            "JOIN messenger_message_placements AS placement ON placement.project_id=canonical.project_id AND placement.message_uuid=canonical.uuid "
            "WHERE canonical.project_id=%s",
            (account.project_id,),
        ).fetchall()
    )
    for identifier, canonical in original:
        assert placements[canonical] == (
            destination_uuid if identifier in moved else source_uuid
        )
    updated = api.get(
        f"/v1/messenger/messages/{link.json()['uuid']}",
        project=account.project_id,
        user=account.owner_user_uuid,
    )
    assert updated.status_code == 200, updated.text
    expected = destination_uuid if mode == "change_all" else source_uuid
    assert updated.json()["payload"]["content"] == f"[Discussion](urn:topic:{expected})"


@pytest.mark.parametrize("existing_destination", [False, True])
def test_full_move_republishes_historical_placements_after_topic_eviction(
    history_account, monkeypatch, api, db, existing_destination
):
    account = history_account
    messages = [history_tests._message_data(40)]
    if existing_destination:
        messages.append(history_tests._message_data(42, subject="Renamed"))
    _import(account, monkeypatch, *messages)
    source = next(
        item for item in history_tests._messages(account) if item.message_id == 40
    )
    canonical = uuid.uuid5(account.project_id, str(source.message_uuid))
    public = db.execute(
        "SELECT COALESCE(legacy_public_uuid,uuid) FROM messenger_message_placements "
        "WHERE project_id=%s AND message_uuid=%s",
        (account.project_id, canonical),
    ).fetchone()[0]
    tasks = _topic_event(
        account,
        {
            "id": 1,
            "type": "update_message",
            "message_id": 40,
            "message_ids": [40],
            "stream_id": 42,
            "orig_subject": "General",
            "subject": "Renamed",
            "propagate_mode": "change_all",
            "edit_timestamp": int(
                datetime.datetime.now(datetime.timezone.utc).timestamp()
            ),
        },
    )
    for task in tasks:
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done", task.error_description
    response = api.get(
        "/v1/events/",
        project=account.project_id,
        user=account.owner_user_uuid,
        params={"page_limit": 500},
    )
    assert response.status_code == 200, response.text
    payloads = [event["payload"] for event in response.json()]
    created = [item for item in payloads if item["kind"] == "message.created"]
    assert [(item["uuid"], item["topic_uuid"]) for item in created] == [
        (str(public), str(uuid.uuid5(source.channel.stream_uuid, "Renamed")))
    ]
    removed_topic = next(
        index for index, item in enumerate(payloads) if item["kind"] == "topic.deleted"
    )
    assert removed_topic < payloads.index(created[0])
