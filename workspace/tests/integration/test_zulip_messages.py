# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
from unittest import mock
import uuid

import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest


def _channel(account, stream_id=42):
    channel = models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=uuid.uuid4(),
        stream_id=stream_id,
        name=f"Channel {stream_id}",
        description="",
        rendered_description="",
        date_created=0,
        invite_only=False,
        is_web_public=False,
        history_public_to_subscribers=True,
        is_archived=False,
    )
    channel.insert()
    return channel


@pytest.fixture
def history_account(_database, db):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"history-owner-{owner_uuid}")
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        provider="zulip",
        external_account_id="zulip.example.com:7",
        settings={"server_url": "https://zulip.example.com"},
    )
    account.insert()
    _channel(account)
    yield account
    account.delete()


def _message_data(message_id=42, **values):
    return {
        "id": message_id,
        "type": "stream",
        "stream_id": 42,
        "subject": "General",
        "content": f"Message **{message_id}**",
        "sender_id": 7,
        "timestamp": 1691057093,
        "is_me_message": False,
        "flags": [],
        **values,
    }


def _import_history(account, first=40, last=44):
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.ImportHistoryContent(
            first_message_id=first, last_message_id=last
        ),
    )
    task.insert()
    with contexts.Context().session_manager():
        restored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
        assert isinstance(restored.content, models.ImportHistoryContent)
        restored.execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


def _direct_message_data(message_id=43, user_ids=(7, 8), **values):
    data = _message_data(
        message_id,
        type="private",
        subject="",
        display_recipient=[
            {"id": user_id, "full_name": f"User {user_id}"} for user_id in user_ids
        ],
        **values,
    )
    del data["stream_id"]
    return data


@pytest.fixture
def direct_peer(history_account, db):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"direct-peer-{owner_uuid}")
    account = external_models.ExternalAccount(
        project_id=history_account.project_id,
        owner_user_uuid=owner_uuid,
        provider="zulip",
        external_account_id="zulip.example.com:8",
        settings=history_account.settings,
    )
    account.insert()
    yield account
    account.delete()


def _messages(account):
    return models.ZulipMessage.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "project_id": filters.EQ(account.project_id),
        },
        order_by={"message_id": "asc"},
    )


@pytest.mark.parametrize(
    "user_ids,channel_type,name",
    [
        ((7, 8), "private", "User 8"),
        ((8, 7), "private", "User 8"),
        ((9, 7, 8), "group", "User 8, User 9"),
        ((7,), "private", "User 7"),
    ],
)
def test_history_import_synchronizes_direct_and_group_conversations(
    history_account, direct_peer, monkeypatch, user_ids, channel_type, name
):
    account = history_account
    data = _direct_message_data(user_ids=user_ids, flags=["read", "starred"])
    zulip = mock.Mock()
    zulip.get_messages.return_value = [data]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)

    assert _import_history(account).status == "done"
    (message,) = _messages(account)
    channel = message.channel
    assert channel.channel_type == channel_type
    assert channel.name == name
    assert channel.stream_id is None
    assert channel.date_created is None
    assert channel.invite_only is True
    assert channel.is_web_public is False
    expected_peer = account.uuid if user_ids == (7,) else direct_peer.uuid
    assert channel.direct_user_uuid == (
        expected_peer if channel_type == "private" else None
    )
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    members = ",".join(str(user_id) for user_id in sorted(user_ids))
    assert channel.stream_uuid == uuid.uuid5(namespace, f"dm:{members}")
    assert channel.stream_uuid.version == 5
    assert message.project_id == account.project_id
    assert message.content.zulip_content == data["content"]
    assert message.topic.name == ""
    assert message.topic.stream_id is None
    assert message.read is True
    assert message.starred is True
    identity = message.uuid, message.message_uuid, channel.uuid, message.topic.uuid

    data["display_recipient"].reverse()
    data["content"] = "Updated direct message"
    for user in data["display_recipient"]:
        user["full_name"] += " renamed"
    assert _import_history(account).status == "done"
    (updated,) = _messages(account)
    assert (
        updated.uuid,
        updated.message_uuid,
        updated.channel.uuid,
        updated.topic.uuid,
    ) == identity
    assert updated.channel.name == ", ".join(
        f"{peer_name} renamed" for peer_name in name.split(", ")
    )
    assert updated.content.zulip_content == "Updated direct message"
    assert (
        len(
            models.ZulipChannel.objects.get_all(
                filters={"external_account_uuid": filters.EQ(account.uuid)}
            )
        )
        == 2
    )
    assert zulip.close.call_count == 2


def test_direct_conversation_identity_is_shared_between_accounts(
    history_account, direct_peer, monkeypatch
):
    zulip = mock.Mock()
    zulip.get_messages.return_value = [_direct_message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    assert _import_history(history_account).status == "done"
    assert _import_history(direct_peer).status == "done"
    (outgoing,) = _messages(history_account)
    (incoming,) = _messages(direct_peer)
    assert outgoing.channel.stream_uuid == incoming.channel.stream_uuid
    assert outgoing.channel.uuid != incoming.channel.uuid
    assert outgoing.channel.direct_user_uuid == direct_peer.uuid
    assert incoming.channel.direct_user_uuid == history_account.uuid


@pytest.mark.parametrize("peer_scope", ("missing", "project", "host"))
def test_direct_messages_wait_for_a_peer_in_the_same_project_and_host(
    history_account, db, monkeypatch, peer_scope
):
    account = history_account
    other = None
    if peer_scope != "missing":
        owner_uuid = uuid.uuid4()
        conftest.seed_workspace_user(db, owner_uuid, f"other-direct-{owner_uuid}")
        other = external_models.ExternalAccount(
            project_id=uuid.uuid4() if peer_scope == "project" else account.project_id,
            owner_user_uuid=owner_uuid,
            provider="zulip",
            external_account_id=(
                "other.example.com:8" if peer_scope == "host" else "zulip.example.com:8"
            ),
            settings=account.settings,
        )
        other.insert()
    zulip = mock.Mock()
    zulip.get_messages.return_value = [_message_data(), _direct_message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    try:
        before = datetime.datetime.now(datetime.timezone.utc)
        task = _import_history(account)
        after = datetime.datetime.now(datetime.timezone.utc)
        assert task.status == "retry"
        assert (
            before + models.TASK_RETRY_DELAY
            <= task.next_retry_at
            <= after + models.TASK_RETRY_DELAY
        )
        assert "zulip.example.com:8" in task.error_description
        assert _messages(account) == []
        channels = models.ZulipChannel.objects.get_all(
            filters={"external_account_uuid": filters.EQ(account.uuid)}
        )
        assert len(channels) == 1
        assert channels[0].channel_type == "channel"
    finally:
        if other is not None:
            other.delete()


def test_history_import_saves_and_updates_messages_without_duplicates(
    history_account, monkeypatch
):
    account = history_account
    channel = _channel(account, 43)
    flags = [
        "read",
        "starred",
        "collapsed",
        "hide_link_previews",
        "mentioned",
        "stream_wildcard_mentioned",
        "topic_wildcard_mentioned",
        "has_alert_word",
        "historical",
    ]
    data = _message_data(
        44,
        content="Hello **world** 🌍",
        flags=flags,
        is_me_message=True,
        last_edit_timestamp=1691057103,
    )
    zulip = mock.Mock()
    zulip.get_messages.return_value = [
        data,
        _message_data(),
    ]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)

    assert _import_history(account).status == "done"

    stored = _messages(account)
    assert [message.message_id for message in stored] == [42, 44]
    assert all(message.project_id == account.project_id for message in stored)
    assert stored[0].message_uuid == uuid.UUID("35b4c198-e00a-55a2-8a6c-2e976a347033")
    assert stored[0].channel.external_account_uuid == account.uuid
    assert stored[0].topic.uuid == stored[1].topic.uuid
    assert stored[0].topic.max_id == 44
    assert stored[
        0
    ].content.zulip_content_updated_at == datetime.datetime.fromtimestamp(
        stored[0].timestamp, tz=datetime.timezone.utc
    )
    imported = stored[1]
    assert imported.sender_id == 7
    assert imported.timestamp == data["timestamp"]
    assert imported.is_me_message is True
    assert all(getattr(imported, flag) is True for flag in flags)
    assert imported.content.zulip_content == data["content"]
    assert imported.content.workspace_content == data["content"]
    assert (
        imported.content.zulip_content_hash
        == hashlib.sha256(data["content"].encode()).hexdigest()
    )
    assert (
        imported.content.workspace_content_hash == imported.content.zulip_content_hash
    )
    assert imported.content.zulip_content_updated_at == datetime.datetime.fromtimestamp(
        data["last_edit_timestamp"], tz=datetime.timezone.utc
    )
    zulip.get_messages.return_value = [
        _message_data(content="Updated", flags=["read"]),
        _message_data(
            44, stream_id=43, subject="Moved", last_edit_timestamp=1691057203
        ),
    ]

    assert _import_history(account).status == "done"

    updated = _messages(account)
    assert [message.uuid for message in updated] == [message.uuid for message in stored]
    assert [message.message_uuid for message in updated] == [
        message.message_uuid for message in stored
    ]
    assert [message.content.uuid for message in updated] == [
        message.content.uuid for message in stored
    ]
    assert updated[0].content.zulip_content == "Updated"
    assert updated[0].read is True
    assert updated[1].starred is False
    assert updated[1].is_me_message is False
    assert updated[1].channel.uuid == channel.uuid
    assert updated[1].topic.name == "Moved"
    assert updated[1].topic.uuid != stored[1].topic.uuid
    assert zulip.get_messages.call_args_list == [mock.call(40, 44), mock.call(40, 44)]
    assert zulip.close.call_count == 2


@pytest.mark.parametrize(
    "server_url,expected_uuid",
    [
        ("http://ZULIP.example.com:8443/path/", "35b4c198-e00a-55a2-8a6c-2e976a347033"),
        ("https://other.example.com", "3c72a4fd-2105-50d9-afda-b522db89967e"),
    ],
)
def test_history_import_keeps_accounts_and_projects_separate(
    history_account, monkeypatch, server_url, expected_uuid
):
    account = history_account
    other = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=account.owner_user_uuid,
        provider="zulip",
        settings={"server_url": server_url},
    )
    other.insert()
    _channel(other)
    zulip = mock.Mock()
    zulip.get_messages.return_value = [_message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)
    try:
        assert _import_history(account).status == "done"
        assert _import_history(other).status == "done"
        first = _messages(account)[0]
        second = _messages(other)[0]
        assert second.project_id == other.project_id
        assert second.message_uuid == uuid.UUID(expected_uuid)
        assert first.uuid != second.uuid
        assert first.topic.uuid != second.topic.uuid
        assert first.content.uuid != second.content.uuid
        zulip.get_messages.return_value = [_message_data(content="Changed")]

        assert _import_history(account).status == "done"

        assert _messages(account)[0].content.zulip_content == "Changed"
        assert _messages(other)[0].content.zulip_content == "Message **42**"
    finally:
        other.delete()


def test_empty_history_import_succeeds_without_rows(history_account, monkeypatch):
    zulip = mock.Mock()
    zulip.get_messages.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    assert _import_history(history_account).status == "done"
    assert _messages(history_account) == []
    zulip.close.assert_called_once_with()


@pytest.mark.parametrize(
    "phase", ["timeout", "401", "503", "validation", "close", "channel"]
)
def test_history_import_errors_roll_back_partial_changes(
    history_account, monkeypatch, db, phase
):
    account = history_account
    zulip = mock.Mock()
    zulip.get_messages.return_value = [_message_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)
    assert _import_history(account).status == "done"
    previous = _messages(account)[0]
    counts = db.execute(
        "SELECT (SELECT count(*) FROM bridge_zulip_v5_message_contents), "
        "(SELECT count(*) FROM bridge_zulip_v5_topics)"
    ).fetchone()
    zulip.get_messages.return_value = [
        _message_data(content="Must roll back"),
        _message_data(44, subject="New topic"),
    ]
    if phase == "timeout":
        zulip.get_messages.side_effect = requests.Timeout("Timed out")
    elif phase in ("401", "503"):
        response = requests.Response()
        response.status_code = int(phase)
        response._content = json.dumps(
            {
                "code": "INVALID_API_KEY" if phase == "401" else "UNAVAILABLE",
                "msg": "Failed",
            }
        ).encode()
        zulip.get_messages.side_effect = requests.HTTPError("Failed", response=response)
    elif phase == "validation":
        zulip.get_messages.return_value[1]["sender_id"] = 0
    elif phase == "close":
        zulip.close.side_effect = RuntimeError("Close failed")
    else:
        zulip.get_messages.return_value[1]["stream_id"] = 999

    task = _import_history(account)

    assert task.status == ("retry" if phase in ("timeout", "503") else "error")
    stored = _messages(account)
    assert len(stored) == 1
    assert stored[0].uuid == previous.uuid
    assert stored[0].content.zulip_content == previous.content.zulip_content
    assert stored[0].topic.max_id == previous.topic.max_id
    assert (
        db.execute(
            "SELECT (SELECT count(*) FROM bridge_zulip_v5_message_contents), "
            "(SELECT count(*) FROM bridge_zulip_v5_topics)"
        ).fetchone()
        == counts
    )
    assert zulip.close.call_count == 2
    if task.status == "retry":
        assert task.next_retry_at is not None
        zulip.get_messages.side_effect = None
        zulip.get_messages.return_value = [_message_data(content="Recovered")]
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done"
        assert _messages(account)[0].content.zulip_content == "Recovered"
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        )
        assert account.status == "backfill"
        assert account.error_description is None


def test_message_uuid_and_project_migrations_backfill_and_round_trip(_database):
    migration = "0203-Add-Workspace-message-UUID-to-Zulip-messages-4118dc.py"
    project_migration = "0204-Scope-Zulip-messages-to-projects-c07814.py"
    timestamp_migration = (
        "0205-Store-Zulip-message-content-timestamps-as-UTC-for-ORM-ee88ee.py"
    )
    with conftest.isolated_migration_database(
        "0202-Generate-required-Zulip-channel-stream-UUIDs-from-server-identity-e4f31c.py"
    ) as database_url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(database_url, autocommit=True) as db:
            owner_uuid = uuid.uuid4()
            conftest.seed_workspace_user(db, owner_uuid, f"message-owner-{owner_uuid}")
            now = datetime.datetime.now(datetime.timezone.utc)
            content = models.MessageContent(
                zulip_content="Hello",
                workspace_content="Hello",
                zulip_content_hash="zulip-hash",
                workspace_content_hash="workspace-hash",
                zulip_content_updated_at=now,
                workspace_content_updated_at=now,
            )
            content.insert()
            content_times = db.execute(
                "SELECT EXTRACT(EPOCH FROM zulip_content_updated_at), "
                "EXTRACT(EPOCH FROM workspace_content_updated_at) "
                "FROM bridge_zulip_v5_message_contents WHERE uuid=%s",
                (content.uuid,),
            ).fetchone()
            topic = models.ZulipTopic(stream_id=42, name="General", max_id=43)
            topic.insert()
            expected = {}
            expected_projects = {}
            for server_url, message_id, message_uuid in [
                (
                    "https://ZULIP.example.com:8443/path/",
                    42,
                    "35b4c198-e00a-55a2-8a6c-2e976a347033",
                ),
                (
                    "http://zulip.example.com",
                    42,
                    "35b4c198-e00a-55a2-8a6c-2e976a347033",
                ),
                (
                    "https://zulip.example.com",
                    43,
                    "c98a0b7b-278c-5441-a5d8-de2ceb446b0f",
                ),
                (
                    "https://other.example.com",
                    42,
                    "3c72a4fd-2105-50d9-afda-b522db89967e",
                ),
            ]:
                account = external_models.ExternalAccount(
                    project_id=uuid.uuid4(),
                    owner_user_uuid=owner_uuid,
                    provider="zulip",
                    settings={"server_url": server_url},
                )
                conftest.seed_legacy_external_account(db, account)
                channel = models.ZulipChannel(
                    external_account_uuid=account.uuid,
                    stream_uuid=uuid.uuid4(),
                    stream_id=42,
                    name="Channel",
                    description="",
                    rendered_description="",
                    date_created=0,
                    invite_only=False,
                    is_web_public=False,
                    history_public_to_subscribers=True,
                    is_archived=False,
                )
                conftest.seed_legacy_zulip_channel(db, channel)
                message_row_uuid = uuid.uuid4()
                expected[message_row_uuid] = uuid.UUID(message_uuid)
                expected_projects[message_row_uuid] = account.project_id
                db.execute(
                    "INSERT INTO bridge_zulip_v5_messages "
                    "(uuid, external_account_uuid, message_id, sender_id, timestamp, content, topic, channel) "
                    "VALUES (%s, %s, %s, 1, 0, %s, %s, %s)",
                    (
                        message_row_uuid,
                        account.uuid,
                        message_id,
                        content.uuid,
                        topic.uuid,
                        channel.uuid,
                    ),
                )
            before = db.execute(
                "SELECT to_jsonb(message) FROM bridge_zulip_v5_messages AS message ORDER BY uuid"
            ).fetchall()

            engine.apply_migration(migration)

            assert (
                dict(
                    db.execute(
                        "SELECT uuid, message_uuid FROM bridge_zulip_v5_messages"
                    ).fetchall()
                )
                == expected
            )
            assert (
                db.execute(
                    "SELECT to_jsonb(message) - 'message_uuid' "
                    "FROM bridge_zulip_v5_messages AS message ORDER BY uuid"
                ).fetchall()
                == before
            )
            engine.apply_migration(project_migration)
            engine.apply_migration(timestamp_migration)

            restored_content = models.MessageContent.objects.get_one(
                filters={"uuid": filters.EQ(content.uuid)}
            )
            assert restored_content.zulip_content_updated_at.timestamp() == float(
                content_times[0]
            )
            assert restored_content.workspace_content_updated_at.timestamp() == float(
                content_times[1]
            )

            stored = db.execute(
                "SELECT uuid, project_id FROM bridge_zulip_v5_messages ORDER BY uuid"
            ).fetchall()
            assert dict(stored) == expected_projects
            with pytest.raises(psycopg.errors.NotNullViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_messages SET message_uuid=NULL WHERE uuid=%s",
                    (stored[0][0],),
                )
            with pytest.raises(psycopg.errors.NotNullViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_messages SET project_id=NULL WHERE uuid=%s",
                    (stored[0][0],),
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_messages SET project_id=%s WHERE uuid=%s",
                    (uuid.uuid4(), stored[0][0]),
                )
            replacement_uuid = uuid.uuid4()
            db.execute(
                "UPDATE bridge_zulip_v5_messages SET message_uuid=%s WHERE uuid=%s",
                (replacement_uuid, stored[0][0]),
            )
            assert db.execute(
                "SELECT message_uuid FROM bridge_zulip_v5_messages WHERE uuid=%s",
                (stored[0][0],),
            ).fetchone() == (replacement_uuid,)
            before = db.execute(
                "SELECT to_jsonb(message) - 'message_uuid' - 'project_id' "
                "FROM bridge_zulip_v5_messages AS message ORDER BY uuid"
            ).fetchall()

            engine.rollback_migration(timestamp_migration)
            assert (
                db.execute(
                    "SELECT EXTRACT(EPOCH FROM zulip_content_updated_at), "
                    "EXTRACT(EPOCH FROM workspace_content_updated_at) "
                    "FROM bridge_zulip_v5_message_contents WHERE uuid=%s",
                    (content.uuid,),
                ).fetchone()
                == content_times
            )
            engine.rollback_migration(project_migration)
            engine.rollback_migration(migration)

            assert (
                db.execute(
                    "SELECT to_jsonb(message) FROM bridge_zulip_v5_messages AS message ORDER BY uuid"
                ).fetchall()
                == before
            )

            engine.apply_migration(timestamp_migration)

            assert (
                dict(
                    db.execute(
                        "SELECT uuid, message_uuid FROM bridge_zulip_v5_messages"
                    ).fetchall()
                )
                == expected
            )
            assert (
                dict(
                    db.execute(
                        "SELECT uuid, project_id FROM bridge_zulip_v5_messages"
                    ).fetchall()
                )
                == expected_projects
            )
