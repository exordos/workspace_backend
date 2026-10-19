# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
from unittest import mock
import uuid

import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.common import exceptions
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.tests.integration import conftest


@pytest.fixture
def channel_import_account(_database, db):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"channel-owner-{owner_uuid}")
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        provider="zulip",
        settings={"server_url": "https://zulip.example.com"},
    )
    account.insert()
    yield account
    account.delete()


def _channel_data(stream_id=42, **values):
    return {
        "stream_id": stream_id,
        "name": f"Channel {stream_id}",
        "description": "A channel",
        "rendered_description": "<p>A channel</p>",
        "date_created": 1691057093,
        "creator_id": None,
        "invite_only": False,
        "is_web_public": False,
        "history_public_to_subscribers": True,
        "is_archived": False,
        "message_retention_days": None,
        "notification_mode": "all_messages",
        **values,
    }


def _run_channel_import(account):
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        priority=models.TASK_PRIORITY_IMPORT_CHANNELS,
        content=models.ImportChannelsContent(),
    )
    task.insert()
    with contexts.Context().session_manager():
        restored = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
        assert isinstance(restored.content, models.ImportChannelsContent)
        restored.execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


def _channels(account):
    return models.ZulipChannel.objects.get_all(
        filters={"external_account_uuid": filters.EQ(account.uuid)},
        order_by={"stream_id": "asc"},
    )


def test_channel_import_saves_and_updates_metadata_preserving_identifiers(
    channel_import_account, monkeypatch
):
    account = channel_import_account
    data = [
        _channel_data(),
        _channel_data(
            43,
            creator_id=17,
            invite_only=True,
            is_archived=True,
            history_public_to_subscribers=False,
            message_retention_days=-1,
        ),
    ]
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = [{**row, "first_message_id": 7} for row in data]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)

    assert _run_channel_import(account).status == "done"

    stored = _channels(account)
    assert len(stored) == 2
    expected_uuids = [
        uuid.UUID("35b4c198-e00a-55a2-8a6c-2e976a347033"),
        uuid.UUID("c98a0b7b-278c-5441-a5d8-de2ceb446b0f"),
    ]
    for channel, row, expected_uuid in zip(stored, data, expected_uuids):
        assert channel.external_account_uuid == account.uuid
        assert channel.channel_type == "channel"
        assert channel.direct_user_uuid is None
        assert channel.stream_uuid == expected_uuid
        assert channel.stream_uuid.version == 5
        assert {name: getattr(channel, name) for name in row} == row
    data[0] = _channel_data(name="Renamed", description="Updated")
    zulip.get_channels.return_value = data

    assert _run_channel_import(account).status == "done"

    updated = _channels(account)
    assert [channel.uuid for channel in updated] == [channel.uuid for channel in stored]
    assert [channel.stream_uuid for channel in updated] == expected_uuids
    assert updated[0].name == "Renamed"
    assert updated[0].description == "Updated"
    assert len(updated) == 2
    assert zulip.close.call_count == 2


def test_channel_import_preserves_direct_and_group_conversations(
    channel_import_account, db, monkeypatch
):
    account = channel_import_account
    peer_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, peer_owner, f"channel-peer-{peer_owner}")
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=peer_owner,
        provider="zulip",
        settings=account.settings,
    )
    peer.insert()
    direct = models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=uuid.uuid4(),
        channel_type="private",
        direct_user_uuid=peer.uuid,
        **{**_channel_data(43), "stream_id": None},
    )
    direct.insert()
    group = models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=uuid.uuid4(),
        channel_type="group",
        **{**_channel_data(44), "stream_id": None},
    )
    group.insert()
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = [_channel_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current: zulip)
    try:
        assert _run_channel_import(account).status == "done"
        assert len(_channels(account)) == 3
        zulip.get_channels.return_value = []
        assert _run_channel_import(account).status == "done"
        remaining = _channels(account)
        assert {row.uuid for row in remaining} == {direct.uuid, group.uuid}
        assert {row.uuid: row.direct_user_uuid for row in remaining} == {
            direct.uuid: peer.uuid,
            group.uuid: None,
        }
        peer.delete()
        assert [row.uuid for row in _channels(account)] == [group.uuid]
    finally:
        if (
            external_models.ExternalAccount.objects.get_one_or_none(
                filters={"uuid": filters.EQ(peer.uuid)}
            )
            is not None
        ):
            peer.delete()


@pytest.mark.parametrize(
    "channel_type,direct_user_uuid",
    [
        ("private", None),
        ("channel", uuid.uuid4()),
        ("group", uuid.uuid4()),
        ("unknown", None),
    ],
)
def test_channel_rejects_invalid_type_or_direct_identity(
    channel_type, direct_user_uuid
):
    expected_error = (
        exceptions.TypeError
        if channel_type == "unknown"
        else exceptions.ValidationErrorException
    )
    with pytest.raises(expected_error):
        models.ZulipChannel(
            external_account_uuid=uuid.uuid4(),
            stream_uuid=uuid.uuid4(),
            channel_type=channel_type,
            direct_user_uuid=direct_user_uuid,
            **{
                **_channel_data(),
                "stream_id": None if channel_type in ("private", "group") else 42,
            },
        )


@pytest.mark.parametrize(
    "server_url,expected_stream_uuid",
    [
        ("http://ZULIP.example.com:9981/path/", "35b4c198-e00a-55a2-8a6c-2e976a347033"),
        ("https://other.example.com", "3c72a4fd-2105-50d9-afda-b522db89967e"),
    ],
)
def test_channel_stream_uuid_depends_on_hostname_and_id_not_the_account(
    channel_import_account, monkeypatch, server_url, expected_stream_uuid
):
    account = channel_import_account
    other_account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=account.owner_user_uuid,
        provider="zulip",
        settings={"server_url": server_url},
    )
    other_account.insert()
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = [_channel_data()]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)
    try:
        assert _run_channel_import(account).status == "done"
        assert _run_channel_import(other_account).status == "done"
        first = _channels(account)[0]
        other = _channels(other_account)[0]
        assert first.uuid != other.uuid
        assert first.stream_uuid == uuid.UUID("35b4c198-e00a-55a2-8a6c-2e976a347033")
        assert other.stream_uuid == uuid.UUID(expected_stream_uuid)
        zulip.get_channels.return_value = [_channel_data(name="Renamed")]
        assert _run_channel_import(account).status == "done"
        assert _channels(account)[0].name == "Renamed"
        assert _channels(other_account)[0].name == "Channel 42"
    finally:
        other_account.delete()


def test_empty_channel_import_succeeds(channel_import_account, monkeypatch):
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = []
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    assert _run_channel_import(channel_import_account).status == "done"
    assert _channels(channel_import_account) == []
    zulip.close.assert_called_once_with()


@pytest.mark.parametrize("stream_ids", [[43, 44], []])
def test_channel_import_removes_missing_channels_only_from_its_account(
    channel_import_account, monkeypatch, stream_ids
):
    account = channel_import_account
    other_account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=account.owner_user_uuid,
        provider="zulip",
        settings=account.settings,
    )
    other_account.insert()
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = [_channel_data(42), _channel_data(43)]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)
    try:
        assert _run_channel_import(account).status == "done"
        assert _run_channel_import(other_account).status == "done"
        previous = _channels(account)
        other_previous = _channels(other_account)
        zulip.get_channels.return_value = [
            _channel_data(stream_id, name="Updated", is_archived=True)
            for stream_id in stream_ids
        ]

        assert _run_channel_import(account).status == "done"

        stored = _channels(account)
        assert [channel.stream_id for channel in stored] == stream_ids
        if stream_ids:
            assert stored[0].uuid == previous[1].uuid
            assert stored[0].name == "Updated"
            assert stored[0].is_archived is True
        other_stored = _channels(other_account)
        assert [channel.uuid for channel in other_stored] == [
            channel.uuid for channel in other_previous
        ]
        assert [channel.name for channel in other_stored] == [
            channel.name for channel in other_previous
        ]
    finally:
        other_account.delete()


@pytest.mark.parametrize(
    "phase", ["fetch", "validation", "close", "topic_fetch", "topic_validation"]
)
def test_failed_channel_import_preserves_previous_channels(
    channel_import_account, monkeypatch, phase
):
    account = channel_import_account
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.return_value = [_channel_data(), _channel_data(43)]
    monkeypatch.setattr(client, "ZulipClient", lambda current_account: zulip)
    assert _run_channel_import(account).status == "done"
    previous = _channels(account)
    zulip.get_channels.return_value = [_channel_data(name="Must roll back")]
    if phase == "fetch":
        zulip.get_channels.side_effect = requests.Timeout("Timed out")
    elif phase == "validation":
        zulip.get_channels.return_value.append(_channel_data(44, name=""))
    elif phase == "topic_fetch":
        zulip.get_topic_notifications.side_effect = requests.Timeout("Timed out")
    elif phase == "topic_validation":
        zulip.get_topic_notifications.return_value = [
            {
                "stream_id": 42,
                "topic_name": "General",
                "visibility_policy": 4,
                "last_updated": 42,
            }
        ]
    else:
        zulip.close.side_effect = RuntimeError("Close failed")

    task = _run_channel_import(account)

    assert task.status == ("retry" if phase in ("fetch", "topic_fetch") else "error")
    stored = _channels(account)
    assert [channel.uuid for channel in stored] == [
        channel.uuid for channel in previous
    ]
    assert [channel.name for channel in stored] == [
        channel.name for channel in previous
    ]
    assert zulip.close.call_count == 2


@pytest.mark.parametrize("status", [None, 401, 503])
def test_channel_import_handles_provider_errors(
    channel_import_account, monkeypatch, status
):
    if status is None:
        error = requests.Timeout("Timed out")
    else:
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(
            {
                "code": "INVALID_API_KEY" if status == 401 else "BAD_REQUEST",
                "msg": "Failed",
            }
        ).encode()
        error = requests.HTTPError("Failed", response=response)
    zulip = mock.Mock()
    zulip.get_topic_notifications.return_value = []
    zulip.get_channels.side_effect = error
    monkeypatch.setattr(client, "ZulipClient", lambda account: zulip)

    task = _run_channel_import(channel_import_account)

    assert task.status == ("error" if status == 401 else "retry")
    assert (task.next_retry_at is None) == (status == 401)
    assert (
        models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(task.external_account_uuid),
                "status": filters.EQ("new"),
            }
        )
        == []
    )
    assert _channels(channel_import_account) == []
    account = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(channel_import_account.uuid)}
    )
    assert account.status == "error"
    if status == 401:
        assert account.error_description == "HTTP 401 INVALID_API_KEY: Failed"
    else:
        assert task.next_retry_at.isoformat() in account.error_description
    zulip.close.assert_called_once_with()
    if status != 401:
        zulip.get_channels.side_effect = None
        zulip.get_channels.return_value = [_channel_data()]
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done"
        assert task.next_retry_at is None
        account = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        )
        assert account.status == "backfill"
        assert account.error_description is None
        assert len(_channels(account)) == 1
        assert zulip.close.call_count == 2
        pending = models.Task.objects.get_all(
            filters={
                "external_account_uuid": filters.EQ(account.uuid),
                "status": filters.EQ("waiting"),
            }
        )
        assert len(pending) == 1
        assert pending[0].priority == models.TASK_PRIORITY_IMPORT_CHANNELS
        assert pending[0].not_before_at is not None


def test_channel_type_migration_preserves_channels_and_enforces_direct_references(
    _database,
):
    migration = "0209-Add-Zulip-channel-types-and-direct-account-references-6bdbf2.py"
    with conftest.isolated_migration_database(
        "0208-Reserve-descending-priorities-for-history-import-tasks-bc5850.py"
    ) as database_url:
        with psycopg.connect(database_url, autocommit=True) as db:
            project_id = uuid.uuid4()
            accounts = []
            for _ in range(2):
                owner_uuid = uuid.uuid4()
                conftest.seed_workspace_user(
                    db, owner_uuid, f"channel-type-{owner_uuid}"
                )
                account = external_models.ExternalAccount(
                    project_id=project_id,
                    owner_user_uuid=owner_uuid,
                    provider="zulip",
                    settings={"server_url": "https://zulip.example.com"},
                )
                account.insert()
                accounts.append(account)
            source, peer = accounts
            legacy = models.ZulipChannel(
                external_account_uuid=source.uuid,
                stream_uuid=uuid.uuid4(),
                **_channel_data(),
            )
            conftest.seed_legacy_zulip_channel(db, legacy)
            engine = migrations.MigrationEngine(
                migrations_path=str(conftest.MIGRATIONS_DIR)
            )
            engine.apply_migration(migration)
            assert db.execute(
                "SELECT channel_type, direct_user_uuid, stream_uuid, stream_id "
                "FROM bridge_zulip_v5_channels WHERE uuid=%s",
                (legacy.uuid,),
            ).fetchone() == ("channel", None, legacy.stream_uuid, 42)
            direct = models.ZulipChannel(
                external_account_uuid=source.uuid,
                stream_uuid=uuid.uuid4(),
                channel_type="private",
                direct_user_uuid=peer.uuid,
                **{**_channel_data(), "stream_id": None, "date_created": None},
            )
            group = models.ZulipChannel(
                external_account_uuid=source.uuid,
                stream_uuid=uuid.uuid4(),
                channel_type="group",
                **{**_channel_data(), "stream_id": None, "date_created": None},
            )
            for channel in (direct, group):
                db.execute(
                    "INSERT INTO bridge_zulip_v5_channels "
                    "(uuid, external_account_uuid, stream_uuid, channel_type, "
                    "direct_user_uuid, name, description, rendered_description, "
                    "invite_only, is_web_public, history_public_to_subscribers, is_archived) "
                    "VALUES (%s, %s, %s, %s, %s, %s, '', '', false, false, true, false)",
                    (
                        channel.uuid,
                        channel.external_account_uuid,
                        channel.stream_uuid,
                        channel.channel_type,
                        channel.direct_user_uuid,
                        channel.name,
                    ),
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET direct_user_uuid=%s WHERE uuid=%s",
                    (uuid.uuid4(), direct.uuid),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET direct_user_uuid=NULL WHERE uuid=%s",
                    (direct.uuid,),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET channel_type='unknown' WHERE uuid=%s",
                    (legacy.uuid,),
                )
            with pytest.raises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET direct_user_uuid=%s WHERE uuid=%s",
                    (peer.uuid, group.uuid),
                )
            peer.delete()
            assert (
                db.execute(
                    "SELECT uuid FROM bridge_zulip_v5_channels WHERE uuid=%s",
                    (direct.uuid,),
                ).fetchone()
                is None
            )
            group.delete()
            engine.rollback_migration(migration)
            assert db.execute(
                "SELECT stream_id, name FROM bridge_zulip_v5_channels WHERE uuid=%s",
                (legacy.uuid,),
            ).fetchone() == (42, "Channel 42")
            engine.apply_migration(migration)


@pytest.mark.parametrize("action", ["delete_account", "delete_stream", "rollback"])
def test_channel_account_and_stream_references(_database, action):
    migration = (
        "0200-Add-account-and-workspace-stream-references-to-Zulip-channels-e707ec.py"
    )
    with conftest.isolated_migration_database(
        "0199-Scope-external-accounts-and-Zulip-tasks-to-projects-4fb70b.py"
    ) as database_url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        engine.apply_migration(migration)
        with psycopg.connect(database_url, autocommit=True) as db:
            project_id, owner_uuid = uuid.uuid4(), uuid.uuid4()
            stream_uuid = uuid.UUID(
                conftest.seed_user_stream(db, project_id, owner_uuid, "Imported stream")
            )
            account = external_models.ExternalAccount(
                project_id=project_id,
                owner_user_uuid=owner_uuid,
                provider="zulip",
                settings={},
            )
            conftest.seed_legacy_external_account(db, account)
            channel = models.ZulipChannel(
                external_account_uuid=account.uuid,
                stream_uuid=stream_uuid,
                stream_id=42,
                name="Imported stream",
                description="",
                rendered_description="",
                date_created=0,
                invite_only=False,
                is_web_public=False,
                history_public_to_subscribers=True,
                is_archived=False,
            )
            conftest.seed_legacy_zulip_channel(db, channel)
            assert db.execute(
                "SELECT external_account_uuid, stream_uuid, stream_id "
                "FROM bridge_zulip_v5_channels WHERE uuid=%s",
                (channel.uuid,),
            ).fetchone() == (account.uuid, stream_uuid, 42)
            for field in ("external_account_uuid", "stream_uuid"):
                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    db.execute(
                        f"UPDATE bridge_zulip_v5_channels SET {field}=%s WHERE uuid=%s",
                        (uuid.uuid4(), channel.uuid),
                    )
                with pytest.raises(psycopg.errors.NotNullViolation):
                    db.execute(
                        f"UPDATE bridge_zulip_v5_channels SET {field}=NULL WHERE uuid=%s",
                        (channel.uuid,),
                    )

            if action == "rollback":
                engine.rollback_migration(migration)
                assert db.execute(
                    "SELECT uuid, stream_id, name FROM bridge_zulip_v5_channels"
                ).fetchall() == [(channel.uuid, 42, "Imported stream")]
                assert (
                    db.execute(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name='bridge_zulip_v5_channels' "
                        "AND column_name IN ('external_account_uuid', 'stream_uuid')"
                    ).fetchone()[0]
                    == 0
                )
            else:
                if action == "delete_account":
                    account.delete()
                else:
                    db.execute(
                        "DELETE FROM messenger_streams WHERE uuid=%s", (stream_uuid,)
                    )
                assert (
                    db.execute(
                        "SELECT count(*) FROM bridge_zulip_v5_channels"
                    ).fetchone()[0]
                    == 0
                )


def test_unmapped_channel_migration_round_trip(_database):
    migration = "0201-Allow-unmapped-Zulip-channels-and-enforce-account-stream-identity-42e7cb.py"
    with conftest.isolated_migration_database(
        "0200-Add-account-and-workspace-stream-references-to-Zulip-channels-e707ec.py"
    ) as database_url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(database_url, autocommit=True) as db:
            project_id, owner_uuid = uuid.uuid4(), uuid.uuid4()
            stream_uuid = uuid.UUID(
                conftest.seed_user_stream(db, project_id, owner_uuid, "Mapped channel")
            )
            account = external_models.ExternalAccount(
                project_id=project_id,
                owner_user_uuid=owner_uuid,
                provider="zulip",
                settings={},
            )
            conftest.seed_legacy_external_account(db, account)
            channels = [
                models.ZulipChannel(
                    external_account_uuid=account.uuid,
                    stream_uuid=stream_uuid,
                    **_channel_data(stream_id),
                )
                for stream_id in (42, 43)
            ]
            for channel in channels:
                conftest.seed_legacy_zulip_channel(db, channel)

            engine.apply_migration(migration)
            db.execute(
                "UPDATE bridge_zulip_v5_channels SET stream_uuid=NULL WHERE uuid=%s",
                (channels[0].uuid,),
            )
            assert db.execute(
                "SELECT stream_uuid FROM bridge_zulip_v5_channels WHERE uuid=%s",
                (channels[0].uuid,),
            ).fetchone() == (None,)
            with pytest.raises(psycopg.errors.UniqueViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET stream_id=42 WHERE uuid=%s",
                    (channels[1].uuid,),
                )
            db.execute(
                "UPDATE bridge_zulip_v5_channels SET stream_uuid=%s WHERE uuid=%s",
                (stream_uuid, channels[0].uuid),
            )
            engine.rollback_migration(migration)
            with pytest.raises(psycopg.errors.NotNullViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET stream_uuid=NULL WHERE uuid=%s",
                    (channels[0].uuid,),
                )
            assert db.execute(
                "SELECT uuid FROM bridge_zulip_v5_channels ORDER BY stream_id"
            ).fetchall() == [(channel.uuid,) for channel in channels]
            engine.apply_migration(migration)
            assert db.execute(
                "SELECT stream_uuid FROM bridge_zulip_v5_channels ORDER BY stream_id"
            ).fetchall() == [(stream_uuid,), (stream_uuid,)]


def test_deterministic_channel_uuid_migration_backfills_and_restores_constraints(
    _database,
):
    migration = "0202-Generate-required-Zulip-channel-stream-UUIDs-from-server-identity-e4f31c.py"
    with conftest.isolated_migration_database(
        "0201-Allow-unmapped-Zulip-channels-and-enforce-account-stream-identity-42e7cb.py"
    ) as database_url:
        engine = migrations.MigrationEngine(
            migrations_path=str(conftest.MIGRATIONS_DIR)
        )
        with psycopg.connect(database_url, autocommit=True) as db:
            project_id, owner_uuid = uuid.uuid4(), uuid.uuid4()
            old_stream_uuid = uuid.UUID(
                conftest.seed_user_stream(db, project_id, owner_uuid, "Old mapping")
            )
            account = external_models.ExternalAccount(
                project_id=project_id,
                owner_user_uuid=owner_uuid,
                provider="zulip",
                settings={"server_url": "https://ZULIP.example.com:8443/path/"},
            )
            conftest.seed_legacy_external_account(db, account)
            channels = [
                models.ZulipChannel(
                    external_account_uuid=account.uuid,
                    stream_uuid=old_stream_uuid,
                    **_channel_data(stream_id),
                )
                for stream_id in (42, 43)
            ]
            for channel in channels:
                conftest.seed_legacy_zulip_channel(db, channel)
            db.execute(
                "UPDATE bridge_zulip_v5_channels SET stream_uuid=NULL WHERE uuid=%s",
                (channels[0].uuid,),
            )
            before = db.execute(
                "SELECT to_jsonb(channel) - 'stream_uuid' "
                "FROM bridge_zulip_v5_channels AS channel ORDER BY stream_id"
            ).fetchall()
            expected = [
                uuid.UUID("35b4c198-e00a-55a2-8a6c-2e976a347033"),
                uuid.UUID("c98a0b7b-278c-5441-a5d8-de2ceb446b0f"),
            ]

            engine.apply_migration(migration)

            assert db.execute(
                "SELECT stream_uuid FROM bridge_zulip_v5_channels ORDER BY stream_id"
            ).fetchall() == [(stream_uuid,) for stream_uuid in expected]
            assert (
                db.execute(
                    "SELECT to_jsonb(channel) - 'stream_uuid' "
                    "FROM bridge_zulip_v5_channels AS channel ORDER BY stream_id"
                ).fetchall()
                == before
            )
            with pytest.raises(psycopg.errors.NotNullViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET stream_uuid=NULL WHERE uuid=%s",
                    (channels[0].uuid,),
                )
            assert db.execute(
                "SELECT count(*) FROM messenger_streams WHERE uuid=ANY(%s)",
                (expected,),
            ).fetchone() == (0,)

            for stream_uuid in expected:
                db.execute(
                    "INSERT INTO messenger_streams (uuid, project_id, owner_uuid, name) "
                    "VALUES (%s, %s, %s, 'Mapped stream')",
                    (stream_uuid, project_id, owner_uuid),
                )
            engine.rollback_migration(migration)
            db.execute(
                "UPDATE bridge_zulip_v5_channels SET stream_uuid=NULL WHERE uuid=%s",
                (channels[0].uuid,),
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                db.execute(
                    "UPDATE bridge_zulip_v5_channels SET stream_uuid=%s WHERE uuid=%s",
                    (uuid.uuid4(), channels[0].uuid),
                )
            engine.apply_migration(migration)
            assert db.execute(
                "SELECT stream_uuid FROM bridge_zulip_v5_channels ORDER BY stream_id"
            ).fetchall() == [(stream_uuid,) for stream_uuid in expected]
