# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage import exceptions as storage_exceptions
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import v2_models
from workspace.services import history_import
from workspace.tests.integration import conftest


@pytest.fixture
def stream_state_account(_database, db):
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"stream-state-{owner_uuid}")
    account = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=owner_uuid,
        provider="zulip",
        settings={"server_url": "https://zulip.example.com"},
    )
    account.insert()
    db.execute(
        "INSERT INTO messenger_project_users (project_id, user_uuid) VALUES (%s, %s)",
        (account.project_id, owner_uuid),
    )
    yield account
    for task in models.Task.objects.get_all(
        filters=filters.OR(
            {"project_id": filters.EQ(account.project_id)},
            {"project_id": filters.Is(None)},
        )
    ):
        task.delete()
    for stored in external_models.ExternalAccount.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    ):
        stored.delete()
    db.execute(
        "DELETE FROM messenger_streams WHERE project_id = %s", (account.project_id,)
    )


def _channel(account, stream_uuid, **values):
    channel = models.ZulipChannel(
        external_account_uuid=account.uuid,
        stream_uuid=stream_uuid,
        **{
            "stream_id": 42,
            "name": "General",
            "description": "",
            "rendered_description": "",
            "invite_only": False,
            "is_web_public": False,
            "history_public_to_subscribers": True,
            "is_archived": False,
            **values,
        },
    )
    channel.insert()
    return channel


def _stream(db, account, stream_uuid, source, deleted=False):
    db.execute(
        """
        INSERT INTO messenger_streams
            (uuid, project_id, owner_uuid, name, source_name, source, deleted_at)
        VALUES (%s, %s, %s, 'General', %s,
            jsonb_build_object('kind', %s::text), CASE WHEN %s THEN NOW() END)
        """,
        (
            stream_uuid,
            account.project_id,
            account.owner_user_uuid,
            source,
            source,
            deleted,
        ),
    )


def _states(account):
    return [
        state.as_plain_dict()
        for state in models.ZulipStreamState.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
    ]


@pytest.mark.parametrize(
    "has_channel,source,deleted,action",
    [
        (True, None, False, "create"),
        (True, "zulip", False, None),
        (True, "native", False, None),
        (False, "zulip", False, "delete"),
        (False, "native", False, None),
        (True, "zulip", True, "create"),
        (False, "zulip", True, None),
        (True, "native", True, "create"),
        (False, "native", True, None),
    ],
)
def test_stream_states_show_only_required_actions(
    stream_state_account, db, has_channel, source, deleted, action
):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    if has_channel:
        _channel(account, stream_uuid)
    if source is not None:
        _stream(db, account, stream_uuid, source, deleted)

    expected = (
        [dict(project_id=account.project_id, stream_uuid=stream_uuid, action=action)]
        if action is not None
        else []
    )
    assert _states(account) == expected
    assert set(models.ZulipStreamState.get_id_property()) == {
        "project_id",
        "stream_uuid",
    }


def test_stream_states_deduplicate_channels_across_accounts(stream_state_account, db):
    account = stream_state_account
    second_owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(
        db, second_owner_uuid, f"stream-state-{second_owner_uuid}"
    )
    second = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=second_owner_uuid,
        provider="zulip",
        settings={"server_url": "https://zulip.example.com"},
    )
    second.insert()
    stream_uuid = uuid.uuid4()
    first_channel = _channel(account, stream_uuid)
    second_channel = _channel(second, stream_uuid)

    assert _states(account) == [
        dict(project_id=account.project_id, stream_uuid=stream_uuid, action="create")
    ]
    first_channel.delete()
    assert _states(account) == [
        dict(project_id=account.project_id, stream_uuid=stream_uuid, action="create")
    ]
    _stream(db, account, stream_uuid, "zulip")
    assert _states(account) == []
    second_channel.delete()
    assert _states(account) == [
        dict(project_id=account.project_id, stream_uuid=stream_uuid, action="delete")
    ]


def test_stream_states_do_not_match_other_projects(stream_state_account, db):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    _channel(account, stream_uuid)
    other_project = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_project_users (project_id, user_uuid) VALUES (%s, %s)",
        (other_project, account.owner_user_uuid),
    )
    db.execute(
        """
        INSERT INTO messenger_streams
            (uuid, project_id, owner_uuid, name, source_name, source)
        VALUES (%s, %s, %s, 'Other project', 'zulip', '{"kind":"zulip"}')
        """,
        (stream_uuid, other_project, account.owner_user_uuid),
    )
    try:
        assert _states(account) == [
            dict(
                project_id=account.project_id, stream_uuid=stream_uuid, action="create"
            )
        ]
        state = models.ZulipStreamState.objects.get_one(
            filters={"project_id": filters.EQ(other_project)}
        )
        assert state.as_plain_dict() == dict(
            project_id=other_project, stream_uuid=stream_uuid, action="delete"
        )
        assert state.get_id() == (other_project, stream_uuid)
    finally:
        db.execute("DELETE FROM messenger_streams WHERE uuid = %s", (stream_uuid,))


def test_stream_states_ignore_other_providers(stream_state_account):
    account = stream_state_account
    _channel(account, uuid.uuid4())
    account.provider = "other"
    account.update()

    assert _states(account) == []


@pytest.mark.parametrize("channel_type", ["channel", "group", "private"])
def test_stream_state_creates_each_stream_type(stream_state_account, db, channel_type):
    account = stream_state_account
    peer_owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, peer_owner_uuid, f"stream-peer-{peer_owner_uuid}")
    peer = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=peer_owner_uuid,
        provider="zulip",
        settings=account.settings,
    )
    peer.insert()
    channel = _channel(
        account,
        uuid.uuid4(),
        channel_type=channel_type,
        stream_id=42 if channel_type == "channel" else None,
        direct_user_uuid=peer.uuid if channel_type == "private" else None,
        name="Imported conversation",
        description="\N{CYRILLIC SMALL LETTER YA}" * 10000,
        date_created=1691057093,
        invite_only=True,
        is_archived=True,
    )
    state = models.ZulipStreamState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )

    state.do()
    with pytest.raises(storage_exceptions.ConflictRecords):
        state.do()

    stream = v2_models.WorkspaceStream.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert stream.uuid == channel.stream_uuid
    assert stream.owner_uuid == account.owner_user_uuid
    assert stream.name == channel.name
    assert stream.description == channel.description
    assert stream.source_name == "zulip"
    assert stream.source == {"kind": "zulip"}
    assert stream.invite_only is True
    assert stream.is_archived is True
    assert stream.created_at == datetime.datetime.fromtimestamp(
        channel.date_created, datetime.timezone.utc
    )
    assert stream.private is (channel_type == "private")
    assert stream.direct_user_uuid == (
        peer_owner_uuid if channel_type == "private" else None
    )
    assert stream.private_index == (
        f"zulip:{stream.uuid}" if channel_type == "private" else None
    )
    assert _states(account) == []


def test_stream_state_resolves_channel_creator(stream_state_account, db):
    account = stream_state_account
    creator_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, creator_uuid, f"stream-creator-{creator_uuid}")
    creator = external_models.ExternalAccount(
        project_id=account.project_id,
        owner_user_uuid=creator_uuid,
        provider="zulip",
        external_account_id=f"{account.get_host()}:17",
        settings=account.settings,
    )
    creator.insert()
    _channel(account, uuid.uuid4(), creator_id=17)
    state = models.ZulipStreamState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )

    state.create_workspace_stream()

    stream = v2_models.WorkspaceStream.objects.get_one(
        filters={"uuid": filters.EQ(state.stream_uuid)}
    )
    assert stream.owner_uuid == creator_uuid
    assert db.execute(
        "SELECT user_uuid FROM messenger_project_users WHERE project_id = %s "
        "AND user_uuid = %s",
        (account.project_id, creator_uuid),
    ).fetchone() == (creator_uuid,)


@pytest.mark.parametrize("other_project,provider", [(True, "zulip"), (False, "other")])
def test_stream_creation_ignores_newer_channels_outside_project_provider(
    stream_state_account, other_project, provider
):
    account = stream_state_account
    channel = _channel(account, uuid.uuid4(), name="Expected channel")
    ignored_account = external_models.ExternalAccount(
        project_id=uuid.uuid4() if other_project else account.project_id,
        owner_user_uuid=account.owner_user_uuid,
        provider=provider,
        settings=account.settings,
    )
    ignored_account.insert()
    try:
        _channel(
            ignored_account,
            channel.stream_uuid,
            name="Ignored channel",
            updated_at=datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=1),
        )
        state = models.ZulipStreamState.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )

        state.create_workspace_stream()

        stream = v2_models.WorkspaceStream.objects.get_one(
            filters={"uuid": filters.EQ(channel.stream_uuid)}
        )
        assert stream.project_id == account.project_id
        assert stream.owner_uuid == account.owner_user_uuid
        assert stream.name == channel.name
    finally:
        ignored_account.delete()


def test_stream_state_restores_soft_deleted_stream(stream_state_account, db):
    account = stream_state_account
    channel = _channel(account, uuid.uuid4(), name="Restored channel")
    _stream(db, account, channel.stream_uuid, "zulip", deleted=True)
    state = models.ZulipStreamState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )

    state.create_workspace_stream()

    assert db.execute(
        "SELECT uuid, name, deleted_at FROM messenger_streams WHERE project_id = %s",
        (account.project_id,),
    ).fetchall() == [(channel.stream_uuid, channel.name, None)]
    assert _states(account) == []


def test_stream_state_creation_uses_current_transaction(stream_state_account):
    account = stream_state_account
    _channel(account, uuid.uuid4())
    state = models.ZulipStreamState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )

    with pytest.raises(RuntimeError, match="Rollback stream creation"):
        with contexts.Context().session_manager():
            state.create_workspace_stream()
            raise RuntimeError("Rollback stream creation")

    assert _states(account) == [state.as_plain_dict()]
    assert (
        v2_models.WorkspaceStream.objects.get_one_or_none(
            filters={"uuid": filters.EQ(state.stream_uuid)}
        )
        is None
    )


def test_stream_state_deletion_cascades_from_root(stream_state_account, db):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    _stream(db, account, stream_uuid, "zulip")
    conftest.seed_user_stream_binding(
        db, account.project_id, stream_uuid, account.owner_user_uuid
    )
    topic = v2_models.WorkspaceStreamTopic(
        project_id=account.project_id, stream_uuid=stream_uuid, name="History topic"
    )
    topic.insert()
    state = models.ZulipStreamState.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )

    state.do()
    with pytest.raises(storage_exceptions.RecordNotFound):
        state.do()

    assert (
        v2_models.WorkspaceStream.objects.get_one_or_none(
            filters={"uuid": filters.EQ(stream_uuid)}
        )
        is None
    )
    assert (
        messenger_models.WorkspaceStream.objects.get_one_or_none(
            filters={"uuid": filters.EQ(stream_uuid)}
        )
        is None
    )
    assert (
        v2_models.WorkspaceStreamTopic.objects.get_one_or_none(
            filters={"uuid": filters.EQ(topic.uuid)}
        )
        is None
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_stream_bindings WHERE stream_uuid = %s",
        (stream_uuid,),
    ).fetchone() == (0,)
    assert _states(account) == []


@pytest.mark.parametrize("source,other_project", [("native", False), ("zulip", True)])
def test_stream_state_deletion_preserves_unrelated_streams(
    stream_state_account, db, source, other_project
):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    _stream(db, account, stream_uuid, source)
    state = models.ZulipStreamState(
        project_id=uuid.uuid4() if other_project else account.project_id,
        stream_uuid=stream_uuid,
        action="delete",
    )

    with pytest.raises(storage_exceptions.RecordNotFound):
        state.delete_workspace_stream()

    assert (
        v2_models.WorkspaceStream.objects.get_one(
            filters={"uuid": filters.EQ(stream_uuid)}
        ).source_name
        == source
    )


def test_zulip_stream_source_is_readable_in_models_and_events(stream_state_account, db):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    v2_models.WorkspaceStream(
        uuid=stream_uuid,
        project_id=account.project_id,
        owner_uuid=account.owner_user_uuid,
        name="General",
        source_name="zulip",
        source={"kind": "zulip"},
    ).insert()
    conftest.seed_user_stream_binding(
        db, account.project_id, stream_uuid, account.owner_user_uuid
    )
    canonical = v2_models.WorkspaceStream.objects.get_one(
        filters={"uuid": filters.EQ(stream_uuid)}
    )
    legacy = messenger_models.WorkspaceStream.objects.get_one(
        filters={"uuid": filters.EQ(stream_uuid)}
    )
    visible = v2_models.WorkspaceUserStream.objects.get_one(
        filters={
            "uuid": filters.EQ(stream_uuid),
            "user_uuid": filters.EQ(account.owner_user_uuid),
        }
    )
    assert canonical.source_name == legacy.source_name == visible.source_name == "zulip"
    assert canonical.source == {"kind": "zulip"}
    assert isinstance(legacy.source, messenger_models.ZulipSource)
    assert isinstance(visible.source, messenger_models.ZulipSource)
    created = event_payloads.StreamCreatedEventPayload(**dict(visible))
    deleted = event_payloads.StreamDeletedEventPayload(
        uuid=stream_uuid, source_name=legacy.source_name, source=legacy.source
    )
    assert created.source.KIND == deleted.source.KIND == "zulip"


def _seed_reconciliation_channels(db, account, count):
    db.execute(
        """
        INSERT INTO bridge_zulip_v5_channels (
            uuid, external_account_uuid, stream_uuid, stream_id, name,
            description, rendered_description, invite_only, is_web_public,
            history_public_to_subscribers, is_archived
        )
        SELECT gen_random_uuid(), %s, gen_random_uuid(), stream_id,
            'Channel ' || stream_id, '', '', false, false, true, false
        FROM generate_series(1, %s) AS stream_id
        """,
        (account.uuid, count),
    )


def _run_reconciliation(task):
    with contexts.Context().session_manager():
        task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
        task.execute()
    return models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})


@pytest.mark.parametrize(
    "count,settings",
    [(0, {}), (1, {}), (1000, {}), (1001, {}), (3, {"batch_size": 2, "interval": 17})],
)
def test_reconciliation_batches_and_schedules_continuation(
    stream_state_account, db, count, settings
):
    account = stream_state_account
    _seed_reconciliation_channels(db, account, count)
    task = models.Task(
        priority=models.TASK_PRIORITY_RECONCILIATION,
        content=models.ReconciliationContent(**settings),
    )
    task.insert()
    before = datetime.datetime.now(datetime.timezone.utc)

    assert _run_reconciliation(task).status == "done"

    after = datetime.datetime.now(datetime.timezone.utc)
    batch_size = task.content.batch_size
    interval = datetime.timedelta(seconds=task.content.interval)
    assert len(_states(account)) == max(0, count - batch_size)
    assert db.execute(
        "SELECT count(*) FROM messenger_streams WHERE project_id = %s",
        (account.project_id,),
    ).fetchone() == (min(count, batch_size),)
    (followup,) = models.Task.objects.get_all(
        filters={
            "project_id": filters.Is(None),
            "uuid": filters.NE(task.uuid),
        }
    )
    assert isinstance(followup.content, models.ReconciliationContent)
    assert followup.content.batch_size == batch_size
    assert followup.content.interval == task.content.interval
    assert task.external_account_uuid is followup.external_account_uuid is None
    assert task.project_id is followup.project_id is None
    assert followup.priority == models.TASK_PRIORITY_RECONCILIATION
    assert followup.priority > max(
        models.TASK_PRIORITY_IMPORT_HISTORY,
        models.TASK_PRIORITY_IMPORT_CHANNELS,
        models.TASK_PRIORITY_IMPORT_USERS,
        models.TASK_PRIORITY_FULL_BACKFILL,
        models.TASK_PRIORITY_INITIALIZE_CONNECTION,
        models.TASK_PRIORITY_REALTIME_MESSAGE,
    )
    if count < batch_size:
        assert followup.status == "waiting"
        assert before + interval <= followup.not_before_at <= after + interval
    else:
        assert followup.status == "new"
        assert followup.not_before_at is None
        assert _run_reconciliation(followup).status == "done"
        assert _states(account) == []
        (delayed,) = models.Task.objects.get_all(
            filters={
                "project_id": filters.Is(None),
                "status": filters.EQ("waiting"),
            }
        )
        assert delayed.external_account_uuid is None
        assert delayed.not_before_at >= after + interval
        assert delayed.content.batch_size == batch_size
        assert delayed.content.interval == task.content.interval


@pytest.mark.parametrize("phase", ["process", "continuation"])
def test_reconciliation_retries_after_failure_and_preserves_continuation(
    stream_state_account, db, monkeypatch, phase
):
    account = stream_state_account
    _seed_reconciliation_channels(db, account, 2)
    task = models.Task(
        priority=models.TASK_PRIORITY_RECONCILIATION,
        content=models.ReconciliationContent(batch_size=2, interval=23),
    )
    task.insert()
    states = _states(account)
    original = models.ZulipStreamState.do
    processed = []

    def fail_after_second(state):
        original(state)
        processed.append(state.stream_uuid)
        if len(processed) == 2:
            raise RuntimeError("Reconciliation failed")

    def fail_continuation(self):
        raise RuntimeError("Reconciliation failed")

    before = datetime.datetime.now(datetime.timezone.utc)
    with monkeypatch.context() as patched:
        if phase == "process":
            patched.setattr(models.ZulipStreamState, "do", fail_after_second)
        else:
            patched.setattr(models.Task, "insert", fail_continuation)
        stored = _run_reconciliation(task)
    after = datetime.datetime.now(datetime.timezone.utc)

    assert stored.status == "retry"
    assert stored.error_description == "Reconciliation failed"
    delay = datetime.timedelta(seconds=23)
    assert before + delay <= stored.next_retry_at <= after + delay
    service = history_import.HistoryImportService()
    assert task.uuid not in [ready.uuid for ready in service._get_tasks()]
    assert _states(account) == states
    assert (
        models.Task.objects.get_all(
            filters={
                "project_id": filters.Is(None),
                "uuid": filters.NE(task.uuid),
            }
        )
        == []
    )
    stored.next_retry_at = before - datetime.timedelta(seconds=1)
    stored.update()

    service._iteration()

    completed = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    assert completed.status == "done"
    assert completed.error_description is None
    assert completed.next_retry_at is None
    assert _states(account) == []
    (followup,) = models.Task.objects.get_all(
        filters={
            "project_id": filters.Is(None),
            "status": filters.EQ("new"),
        }
    )
    assert followup.external_account_uuid is None
    assert followup.content.batch_size == 2
    assert followup.content.interval == 23


def test_reconciliation_preempts_other_tasks(stream_state_account):
    account = stream_state_account
    initialization = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        priority=models.TASK_PRIORITY_INITIALIZE_CONNECTION,
        content=models.InitializeConnectionContent(),
    )
    initialization.insert()
    reconciliation = models.Task(
        priority=models.TASK_PRIORITY_RECONCILIATION,
        content=models.ReconciliationContent(),
    )
    reconciliation.insert()
    service = history_import.HistoryImportService()

    service._iteration()

    assert (
        models.Task.objects.get_one(
            filters={"uuid": filters.EQ(reconciliation.uuid)}
        ).status
        == "done"
    )
    assert (
        models.Task.objects.get_one(
            filters={"uuid": filters.EQ(initialization.uuid)}
        ).status
        == "new"
    )
    assert [task.uuid for task in service._get_tasks()] == [initialization.uuid]


def test_reconciliation_processes_multiple_projects(stream_state_account):
    account = stream_state_account
    first = _channel(account, uuid.uuid4())
    other = external_models.ExternalAccount(
        project_id=uuid.uuid4(),
        owner_user_uuid=account.owner_user_uuid,
        provider="zulip",
        settings=account.settings,
    )
    other.insert()
    second = _channel(other, uuid.uuid4())
    try:
        task = models.Task(
            priority=models.TASK_PRIORITY_RECONCILIATION,
            content=models.ReconciliationContent(),
        )
        task.insert()

        assert _run_reconciliation(task).status == "done"

        for owner, channel in ((account, first), (other, second)):
            stream = v2_models.WorkspaceStream.objects.get_one(
                filters={"uuid": filters.EQ(channel.stream_uuid)}
            )
            assert stream.project_id == owner.project_id
        assert _states(account) == _states(other) == []
    finally:
        other.delete()
        stream = v2_models.WorkspaceStream.objects.get_one_or_none(
            filters={"uuid": filters.EQ(second.stream_uuid)}
        )
        if stream is not None:
            stream.delete()


@pytest.mark.parametrize(
    "status", [None, "new", "waiting", "retry", "done", "error", "cancel"]
)
def test_worker_ensures_one_active_reconciliation(stream_state_account, status):
    existing = None
    if status is not None:
        existing = models.Task(
            priority=models.TASK_PRIORITY_RECONCILIATION,
            status=status,
            content=models.ReconciliationContent(batch_size=17, interval=31),
        )
        existing.insert()
    service = history_import.HistoryImportService()

    with contexts.Context().session_manager():
        service._ensure_reconciliation_task()
        service._ensure_reconciliation_task()

    (active,) = models.Task.objects.get_all(
        filters={
            "content": filters.JSONFields({"kind": "reconciliation"}),
            "status": filters.In(["new", "waiting", "retry"]),
        }
    )
    assert active.project_id is active.external_account_uuid is None
    assert active.priority == models.TASK_PRIORITY_RECONCILIATION
    if status in ("new", "waiting", "retry"):
        assert active.uuid == existing.uuid
        assert active.content.batch_size == 17
        assert active.content.interval == 31
    else:
        assert active.status == "new"
        assert active.content.batch_size == 1000
        assert active.content.interval == 10


def test_worker_bootstraps_and_recreates_missing_reconciliation(stream_state_account):
    service = history_import.HistoryImportService()

    service._iteration()

    (waiting,) = models.Task.objects.get_all(filters={"status": filters.EQ("waiting")})
    assert waiting.project_id is waiting.external_account_uuid is None
    assert isinstance(waiting.content, models.ReconciliationContent)
    count = models.Task.objects.count()
    service._iteration()
    assert models.Task.objects.count() == count
    waiting.delete()

    service._iteration()

    (replacement,) = models.Task.objects.get_all(
        filters={"status": filters.EQ("waiting")}
    )
    assert replacement.uuid != waiting.uuid
    assert replacement.project_id is replacement.external_account_uuid is None


def test_stream_state_view_migration_is_reversible(stream_state_account, db):
    account = stream_state_account
    stream_uuid = uuid.uuid4()
    _stream(db, account, stream_uuid, "zulip")
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0211-Add-Zulip-stream-reconciliation-view-237f1b.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_stream_states')"
        ).fetchone() == (None,)
        assert db.execute(
            "SELECT source_name, source FROM messenger_streams WHERE uuid = %s",
            (stream_uuid,),
        ).fetchone() == ("native", {"kind": "native"})
        with pytest.raises(psycopg.errors.CheckViolation):
            _stream(db, account, uuid.uuid4(), "zulip")
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert (
        models.ZulipStreamState.objects.get_all(
            filters={"project_id": filters.EQ(uuid.uuid4())}
        )
        == []
    )
