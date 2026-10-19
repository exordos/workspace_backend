# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
import contextlib
import datetime
import importlib.util
from unittest import mock
import uuid

import pytest
import psycopg
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import v2_models
from workspace.services import history_import
from workspace.services import zulip_realtime
from workspace.tests.integration import conftest


@pytest.fixture
def outgoing(api, db, scheduled_reconciliation, monkeypatch):
    api.project_id = uuid.UUID(api.project_id)
    api.user_uuid = uuid.UUID(api.user_uuid)
    stream_uuid = uuid.UUID(
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Channel")
    )
    db.execute(
        "UPDATE messenger_streams SET source_name='zulip', source='{\"kind\":\"zulip\"}' WHERE project_id=%s AND uuid=%s",
        (api.project_id, stream_uuid),
    )
    topic = v2_models.WorkspaceStreamTopic(
        project_id=api.project_id, stream_uuid=stream_uuid, name="General"
    )
    topic.insert()
    v2_models.WorkspaceUserTopicBinding(
        project_id=api.project_id, user_uuid=api.user_uuid, topic_uuid=topic.uuid
    ).insert()
    db.execute(
        "UPDATE messenger_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
        (topic.uuid, api.project_id, stream_uuid),
    )
    account = external_models.ExternalAccount(
        project_id=api.project_id,
        owner_user_uuid=api.user_uuid,
        provider="zulip",
        external_account_id="zulip.example.com:7",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "sender@example.com",
            "api_key": "sender-key",
        },
    )
    account.insert()
    channel = models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=stream_uuid,
        stream_id=42,
        name="Channel",
        description="",
        rendered_description="",
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    )
    channel.insert()
    subscription = models.RealtimeSubscription(
        project_id=api.project_id,
        external_account_uuid=account.uuid,
        generation=uuid.uuid4(),
        queue_id="sender-queue",
        last_event_id=-1,
        account_revision=account.realtime_revision(),
        longpoll_timeout=90,
    )
    subscription.insert()
    remote = {
        "id": 12345,
        "sender_id": 7,
        "timestamp": int(datetime.datetime.now(datetime.timezone.utc).timestamp()) + 10,
        "content": "Raw **message** [user](urn:user:unchanged)",
        "type": "stream",
        "stream_id": 42,
        "subject": "General",
    }
    zulip = mock.Mock()
    zulip.send_message.return_value = remote["id"]
    zulip.get_messages.side_effect = lambda first, last: [dict(remote)]
    constructor = mock.Mock(return_value=zulip)
    monkeypatch.setattr(client, "ZulipClient", constructor)
    yield api, account, channel, remote, zulip, constructor
    for message in v2_models.WorkspaceMessage.objects.get_all(
        filters={"project_id": filters.EQ(api.project_id)}
    ):
        message.delete()
    account.delete()
    for stream in v2_models.WorkspaceStream.objects.get_all(
        filters={"project_id": filters.EQ(api.project_id)}
    ):
        stream.delete()
    for model in (
        models.ZulipTopicChange,
        models.ZulipMessageChange,
        models.ZulipMessageCounterChange,
        v2_models.WorkspaceMessageChange,
        v2_models.WorkspaceMessageCounterChange,
    ):
        for change in model.objects.get_all(
            filters={"project_id": filters.EQ(api.project_id)}
        ):
            change.delete()


def create(outgoing, canonical_uuid=None):
    api, _account, channel, remote, _zulip, _constructor = outgoing
    values = {
        "stream_uuid": str(channel.stream_uuid),
        "payload": {"kind": "markdown", "content": remote["content"]},
    }
    if canonical_uuid is not None:
        values["uuid"] = str(canonical_uuid)
    response = api.post(
        "/v1/messenger/messages/",
        json=values,
    )
    assert response.status_code == 201, response.text
    return response.json()


def dispatch():
    with contexts.Context().session_manager():
        while models.WorkspaceEvent.dispatch_one():
            pass


def tasks(account):
    return models.Task.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)},
        order_by={"created_at": "asc"},
    )


def ready(task):
    task.next_retry_at = None
    task.update()


def test_outbox_send_uses_author_and_server_date_without_echo_duplicate(outgoing):
    api, account, _channel, remote, zulip, constructor = outgoing
    public = create(outgoing)
    dispatch()
    (send,) = tasks(account)
    assert send.content.kind == "send_message"
    service = history_import.HistoryImportService()
    service._iteration()
    constructor.assert_called_with(mock.ANY)
    assert constructor.call_args.args[0].uuid == account.uuid
    (delivery,) = models.MessageDelivery.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    zulip.send_message.assert_called_once_with(
        42, remote["content"], "General", "sender-queue", str(delivery.uuid)
    )
    assert tasks(account)[0].status == "retry"
    first_attempt = tasks(account)[0]
    assert first_attempt.execution_time > 0
    assert first_attempt.queue_wait_time >= 0
    ready(tasks(account)[0])
    service._iteration()
    assert tasks(account)[0].status == "done"
    assert tasks(account)[0].execution_time > first_attempt.execution_time
    canonical = v2_models.WorkspaceMessage.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert canonical.created_at == datetime.datetime.fromtimestamp(
        remote["timestamp"], datetime.timezone.utc
    )
    placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert placement.created_at == canonical.created_at
    assert str(placement.uuid) == public["uuid"]
    # The event comes back through the same persisted inbox as every Zulip event.
    subscription = models.RealtimeSubscription.for_account(account)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": 0,
                    "type": "message",
                    "local_message_id": str(delivery.uuid),
                    "message": dict(remote),
                }
            ],
        )
        assert models.RealtimeEvent.dispatch_one()
    service._iteration()
    assert (
        len(
            v2_models.WorkspaceMessage.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            )
        )
        == 1
    )
    assert (
        models.ZulipMessage.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    dispatch()
    assert (
        len([task for task in tasks(account) if task.content.kind == "send_message"])
        == 1
    )
    zulip.send_message.assert_called_once()
    state = v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "user_uuid": filters.EQ(account.owner_user_uuid),
        }
    )
    assert state.read_at is not None


def test_edit_retries_until_send_then_uses_the_same_account_and_id(outgoing):
    api, account, _channel, remote, zulip, constructor = outgoing
    public = create(outgoing)
    response = api.put(
        f"/v1/messenger/messages/{public['uuid']}",
        json={"payload": {"kind": "markdown", "content": "Edited as-is"}},
    )
    assert response.status_code == 200, response.text
    dispatch()
    send, edit = tasks(account)
    assert send.content.kind == "send_message" and edit.content.kind == "edit_message"
    assert (
        models.TASK_PRIORITY_IMPORT_CHANNELS
        > send.priority
        > edit.priority
        > models.TASK_PRIORITY_IMPORT_HISTORY
    )
    with contexts.Context().session_manager():
        edit.execute()
    assert tasks(account)[1].status == "retry"
    zulip.edit_message.assert_not_called()
    remote["content"] = "Edited as-is"
    service = history_import.HistoryImportService()
    service._iteration()
    ready(tasks(account)[0])
    service._iteration()
    ready(tasks(account)[1])
    service._iteration()
    zulip.edit_message.assert_called_once_with(remote["id"], "Edited as-is")
    assert tasks(account)[1].status == "done"
    assert all(call.args[0].uuid == account.uuid for call in constructor.call_args_list)


def test_missing_sender_does_not_use_another_observers_credentials(outgoing, db):
    api, account, channel, _remote, zulip, _constructor = outgoing
    observer = uuid.uuid4()
    conftest.seed_workspace_user(db, observer, f"observer-{observer}")
    account.disabled = True
    account.update()
    other = external_models.ExternalAccount(
        project_id=api.project_id,
        owner_user_uuid=observer,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        status="active",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "observer@example.com",
            "api_key": "observer-key",
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
    try:
        create(outgoing)
        dispatch()
        assert tasks(account) == []
        (event,) = models.WorkspaceEvent.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        assert event.processed_at is None and event.next_retry_at is not None
        zulip.send_message.assert_not_called()
    finally:
        other.delete()


def test_lost_send_response_recovers_from_echo_without_reposting(outgoing):
    _api, account, _channel, remote, zulip, _constructor = outgoing
    create(outgoing)
    dispatch()
    zulip.send_message.side_effect = requests.Timeout("response lost")
    service = history_import.HistoryImportService()
    service._iteration()
    ready(tasks(account)[0])
    service._iteration()
    zulip.send_message.assert_called_once()

    (delivery,) = models.MessageDelivery.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    subscription = models.RealtimeSubscription.for_account(account)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": 0,
                    "type": "message",
                    "local_message_id": str(delivery.uuid),
                    "message": dict(remote),
                }
            ],
        )
        models.RealtimeEvent.dispatch_one()
    service._iteration()
    assert tasks(account)[0].status == "done"
    zulip.send_message.assert_called_once()


def test_direct_message_routes_to_the_peer_without_a_channel_topic(outgoing, db):
    api, account, channel, remote, zulip, _constructor = outgoing
    owner = uuid.uuid4()
    conftest.seed_workspace_user(db, owner, f"peer-{owner}")
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=owner,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings={
            "server_url": "https://zulip.example.com",
            "email": "peer@example.com",
        },
    )
    peer.insert()
    try:
        channel.channel_type = "private"
        channel.stream_id = None
        channel.direct_user_uuid = peer.uuid
        channel.update()
        create(outgoing)
        dispatch()
        history_import.HistoryImportService()._iteration()
        (delivery,) = models.MessageDelivery.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        zulip.send_message.assert_called_once_with(
            [8], remote["content"], None, "sender-queue", str(delivery.uuid)
        )
    finally:
        peer.delete()


def test_old_edit_retry_cannot_overwrite_a_newer_edit(outgoing):
    api, account, _channel, remote, zulip, _constructor = outgoing
    public = create(outgoing)
    dispatch()
    service = history_import.HistoryImportService()
    service._iteration()
    ready(tasks(account)[0])
    service._iteration()
    for content in ("First edit", "Latest edit"):
        response = api.put(
            f"/v1/messenger/messages/{public['uuid']}",
            json={"payload": {"kind": "markdown", "content": content}},
        )
        assert response.status_code == 200, response.text
        dispatch()
    send, old, latest = tasks(account)
    remote["content"] = "Latest edit"
    remote["last_edit_timestamp"] = remote["timestamp"] + 1
    with contexts.Context().session_manager():
        latest.execute()
        old.execute()
    zulip.edit_message.assert_called_once_with(remote["id"], "Latest edit")
    assert all(task.status == "done" for task in tasks(account))


def test_metadata_retry_does_not_resend_and_credentials_can_rotate(outgoing):
    api, account, _channel, remote, zulip, constructor = outgoing
    public = create(outgoing)
    dispatch()
    service = history_import.HistoryImportService()
    service._iteration()
    zulip.get_messages.side_effect = requests.Timeout("metadata unavailable")
    ready(tasks(account)[0])
    service._iteration()
    assert tasks(account)[0].status == "retry"
    zulip.send_message.assert_called_once()
    zulip.get_messages.side_effect = lambda first, last: [dict(remote)]
    ready(tasks(account)[0])
    service._iteration()
    account.settings = {**account.settings, "api_key": "rotated-key"}
    account.update()
    response = api.put(
        f"/v1/messenger/messages/{public['uuid']}",
        json={"payload": {"kind": "markdown", "content": "After credential rotation"}},
    )
    assert response.status_code == 200
    remote["content"] = "After credential rotation"
    dispatch()
    service._iteration()
    assert tasks(account)[-1].status == "done"
    assert constructor.call_args.args[0].settings["api_key"] == "rotated-key"
    zulip.edit_message.assert_called_once_with(remote["id"], remote["content"])


def test_rebound_identity_cannot_execute_an_old_send(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    create(outgoing)
    dispatch()
    account.properties["external_account_id"].set_value_force("zulip.example.com:99")
    account.update()
    history_import.HistoryImportService()._iteration()
    assert tasks(account)[0].status == "cancel"
    zulip.send_message.assert_not_called()


def test_edit_of_previously_imported_message_uses_its_existing_remote_id(outgoing, db):
    api, account, channel, remote, zulip, _constructor = outgoing
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    remote_uuid = uuid.uuid5(namespace, str(remote["id"]))
    canonical_uuid = uuid.uuid5(account.project_id, str(remote_uuid))
    public = create(outgoing, canonical_uuid)
    # Seed an already imported message and acknowledge its fixture creation.
    for event in models.WorkspaceEvent.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    ):
        event.processed_at = datetime.datetime.now(datetime.timezone.utc)
        event.update()
    db.execute(
        "UPDATE messenger_messages SET source_name='zulip', source='{\"kind\":\"zulip\"}' WHERE project_id=%s AND uuid=%s",
        (account.project_id, canonical_uuid),
    )
    topic = models.ZulipTopic(
        uuid=uuid.uuid4(), stream_id=42, name="General", max_id=remote["id"]
    )
    topic.insert()
    content = models.MessageContent(
        zulip_content=remote["content"],
        workspace_content=remote["content"],
        zulip_content_hash="seed",
        workspace_content_hash="seed",
        zulip_content_updated_at=datetime.datetime.now(datetime.timezone.utc),
        workspace_content_updated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    content.insert()
    observed = models.ZulipMessage(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        message_uuid=remote_uuid,
        message_id=remote["id"],
        sender_id=7,
        timestamp=remote["timestamp"],
        channel=channel,
        topic=topic,
        content=content,
        read=True,
    )
    observed.insert()
    response = api.put(
        f"/v1/messenger/messages/{public['uuid']}",
        json={"payload": {"kind": "markdown", "content": "Imported edit"}},
    )
    assert response.status_code == 200, response.text
    remote["content"] = "Imported edit"
    dispatch()
    (edit,) = tasks(account)
    assert edit.content.kind == "edit_message"
    history_import.HistoryImportService()._iteration()
    assert tasks(account)[0].status == "done"
    zulip.send_message.assert_not_called()
    zulip.edit_message.assert_called_once_with(remote["id"], "Imported edit")
    observed.delete()
    topic.delete()
    content.delete()


def test_subscriber_rolls_back_task_and_delivery_together(outgoing, monkeypatch):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    create(outgoing)
    insert = models.Task.insert

    def fail_after_insert(task, **kwargs):
        insert(task, **kwargs)
        raise RuntimeError("transaction failed")

    with monkeypatch.context() as patch:
        patch.setattr(models.Task, "insert", fail_after_insert)
        dispatch()
    assert tasks(account) == []
    assert (
        models.MessageDelivery.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    (event,) = models.WorkspaceEvent.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert event.attempts == 1 and event.processed_at is None
    event.next_retry_at = None
    event.update()
    dispatch()
    assert len(tasks(account)) == 1
    zulip.send_message.assert_not_called()


def test_prepared_send_is_not_claimed_again_or_reposted_after_restart(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    create(outgoing)
    dispatch()
    (task,) = tasks(account)
    with contexts.Context().session_manager():
        assert task.content.prepare(task)
    (restored,) = tasks(account)
    with contexts.Context().session_manager():
        assert not restored.content.prepare(restored)
    restored.lease_expires_at = datetime.datetime.now(
        datetime.timezone.utc
    ) - datetime.timedelta(seconds=1)
    restored.update()
    history_import.HistoryImportService()._iteration()
    assert tasks(account)[0].status == "retry"
    zulip.send_message.assert_not_called()


def test_deleting_a_pending_message_cascades_its_delivery_and_tasks(outgoing):
    _api, account, _channel, _remote, zulip, _constructor = outgoing
    create(outgoing)
    dispatch()
    (message,) = v2_models.WorkspaceMessage.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    message.delete()
    assert tasks(account) == []
    assert (
        models.MessageDelivery.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    zulip.send_message.assert_not_called()


def test_realtime_listens_to_the_existing_workspace_notification_channel(_database):
    async def scenario():
        service = zulip_realtime.ZulipRealtimeService(conftest.TEST_DB_URL)
        listener = asyncio.create_task(service._listen_notifications())
        try:
            async with await psycopg.AsyncConnection.connect(
                conftest.TEST_DB_URL, autocommit=True
            ) as connection:
                async with asyncio.timeout(3):
                    while not service._workspace_events_ready.is_set():
                        await connection.execute(
                            "SELECT pg_notify(%s,'1')",
                            (zulip_realtime.WORKSPACE_EVENTS_CHANNEL,),
                        )
                        await asyncio.sleep(0.01)
        finally:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener
            service._executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_outgoing_migration_starts_with_new_events_and_round_trips(_database):
    with conftest.isolated_migration_database(
        "0232-Add-durable-Zulip-realtime-subscriptions-and-backfill-tracking-9ac2f0.py"
    ) as url:
        path = (
            conftest.MIGRATIONS_DIR
            / "0233-Add-outgoing-Zulip-message-tasks-and-Workspace-outbox-subscription-23c3f1.py"
        )
        spec = importlib.util.spec_from_file_location("outgoing_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with psycopg.connect(url, autocommit=True) as connection:
            old, new, project = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            sql = "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload) VALUES(%s,%s,'fanout','topic','test','{\"source_kind\":\"message.created\"}'::jsonb)"
            connection.execute(sql, (old, project))
            migration.migration_step.upgrade(connection)
            connection.execute(sql, (new, project))
            assert connection.execute(
                "SELECT uuid FROM bridge_zulip_v5_workspace_events"
            ).fetchall() == [(new,)]
            migration.migration_step.downgrade(connection)
            assert connection.execute(
                "SELECT to_regclass('bridge_zulip_v5_workspace_events')"
            ).fetchone() == (None,)
            migration.migration_step.upgrade(connection)
