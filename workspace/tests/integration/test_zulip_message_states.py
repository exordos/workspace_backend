# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
import hashlib
from unittest import mock
import uuid

import psycopg
import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import migrations

from workspace.bridges.zulip.dm import models
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.messenger_api.dm import event_payloads
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import v2_models
from workspace.services.messenger_workers import v2_projection
from workspace.services import history_import
from workspace.tests.integration import conftest
from workspace.tests.integration import legacy_store


@pytest.fixture
def message_sources(_database, db):
    accounts = []

    def create(project_id=None, host=None, message_id=42, sender_id=7, stream_id=42):
        owner_uuid = uuid.uuid4()
        conftest.seed_workspace_user(db, owner_uuid, f"message-owner-{owner_uuid}")
        account = external_models.ExternalAccount(
            project_id=project_id or uuid.uuid4(),
            owner_user_uuid=owner_uuid,
            provider="zulip",
            external_account_id=f"{host or 'zulip.example.com'}:{sender_id}",
            settings={"server_url": f"https://{host or 'zulip.example.com'}"},
        )
        account.insert()
        accounts.append(account)
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
        channel = models.ZulipChannel(
            external_account_uuid=account.uuid,
            stream_uuid=uuid.uuid5(namespace, str(stream_id)),
            stream_id=stream_id,
            name="Channel",
            description="",
            rendered_description="",
            invite_only=False,
            is_web_public=False,
            history_public_to_subscribers=True,
            is_archived=False,
        )
        channel.insert()
        topic_uuid = uuid.uuid5(channel.stream_uuid, "General")
        topic = models.ZulipTopic.objects.get_one_or_none(
            filters={"uuid": filters.EQ(topic_uuid)}
        )
        if topic is None:
            topic = models.ZulipTopic(
                uuid=topic_uuid, stream_id=stream_id, name="General", max_id=message_id
            )
            topic.insert()
        edited_at = datetime.datetime(2023, 8, 4, tzinfo=datetime.timezone.utc)
        content = models.MessageContent(
            zulip_content="Original **message**",
            workspace_content="Workspace **message**",
            zulip_content_hash=hashlib.sha256(b"Original **message**").hexdigest(),
            workspace_content_hash=hashlib.sha256(b"Workspace **message**").hexdigest(),
            zulip_content_updated_at=edited_at,
            workspace_content_updated_at=edited_at,
        )
        content.insert()
        source = models.ZulipMessage(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            message_uuid=uuid.uuid5(namespace, str(message_id)),
            message_id=message_id,
            sender_id=7,
            timestamp=1691057093,
            channel=channel,
            topic=topic,
            content=content,
            read=True,
            starred=True,
            mentioned=True,
        )
        source.insert()
        return account, source

    yield create
    for account in accounts:
        account.delete()
        for model in (v2_models.WorkspaceMessage, v2_models.WorkspaceStream):
            for row in model.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            ):
                row.delete()
        _clear_changes(db, account)
        db.execute(
            "DELETE FROM bridge_zulip_v5_topic_changes WHERE project_id = %s",
            (account.project_id,),
        )
        db.execute(
            "DELETE FROM bridge_zulip_v5_message_counter_changes WHERE project_id = %s",
            (account.project_id,),
        )
    for task in models.Task.objects.get_all(filters={"project_id": filters.Is(None)}):
        task.delete()


def _prepare(account):
    with contexts.Context().session_manager():
        for model in (
            models.ZulipProjectUserState,
            models.ZulipStreamState,
            models.ZulipStreamBindingState,
            models.ZulipTopicState,
        ):
            for state in model.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            ):
                state.do()
        changes = _topic_changes(account)
        if changes:
            models.ZulipTopicChange.acknowledge_many(changes)


def _states(account):
    return models.ZulipMessageState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)},
        order_by={"message_uuid": "asc"},
    )


def _messages(account):
    return v2_models.WorkspaceMessage.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _placement_states(account):
    return models.ZulipMessagePlacementState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _placements(account):
    return v2_models.WorkspaceMessagePlacement.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _user_states(account):
    return models.ZulipUserMessageState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _changes(model, account):
    return model.objects.get_all(filters={"project_id": filters.EQ(account.project_id)})


def _topic_changes(account):
    return _changes(models.ZulipTopicChange, account)


def _topic_states(account):
    return models.ZulipTopicState.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def _clear_changes(db, account):
    for table in ("bridge_zulip_v5_message_changes", "messenger_message_changes"):
        db.execute(f"DELETE FROM {table} WHERE project_id = %s", (account.project_id,))


def test_message_state_creates_canonical_content_without_placements(
    message_sources, db, api
):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    assert state.action == "create"
    assert state.source_uuid == source.uuid
    assert state.message_uuid == uuid.uuid5(
        account.project_id, str(source.message_uuid)
    )
    assert state.get_id() == (account.project_id, state.message_uuid)
    assert set(models.ZulipMessageState.get_id_property()) == {
        "project_id",
        "message_uuid",
    }
    outbox_before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id = %s",
        (account.project_id,),
    ).fetchone()
    with contexts.Context().session_manager():
        state.do()
        state.do()
    assert _states(account) == []
    (message,) = _messages(account)
    assert message.uuid == state.message_uuid
    assert message.author_uuid == account.owner_user_uuid
    assert message.payload.as_plain_dict() == {
        "kind": "markdown",
        "content": source.content.workspace_content,
    }
    assert message.source_name == "zulip"
    assert message.source == {"kind": "zulip"}
    assert message.created_at == datetime.datetime.fromtimestamp(
        source.timestamp, tz=datetime.timezone.utc
    )
    assert message.updated_at == source.content.workspace_content_updated_at
    assert message.ingest_sequence > 0
    deleted_event = event_payloads.MessageDeletedEventPayload(
        uuid=message.uuid,
        stream_uuid=source.channel.stream_uuid,
        topic_uuid=source.topic.uuid,
        author_uuid=message.author_uuid,
        source_name="zulip",
        source=messenger_models.ZulipSource(),
    )
    assert deleted_event.source.kind == "zulip"
    for table in (
        "messenger_message_placements",
        "m_workspace_messages",
        "messenger_user_message_bindings",
        "messenger_user_message_states",
    ):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE project_id = %s", (account.project_id,)
        ).fetchone() == (0,)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id = %s",
            (account.project_id,),
        ).fetchone()
        == outbox_before
    )
    response = api.get(
        "/v1/messenger/messages/",
        user=account.owner_user_uuid,
        project=account.project_id,
        params={"stream_uuid": str(source.channel.stream_uuid)},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_message_states_deduplicate_accounts_and_delete_the_root(message_sources, db):
    first, source = message_sources()
    second, copy = message_sources(project_id=first.project_id, sender_id=8)
    _prepare(first)
    (creation,) = _states(first)
    with contexts.Context().session_manager():
        creation.do()
    assert len(_messages(first)) == 1
    assert _messages(first)[0].author_uuid == first.owner_user_uuid
    source.delete()
    assert _states(first) == []
    copy.delete()
    (deletion,) = _states(first)
    assert deletion.action == "delete"
    assert deletion.message_uuid == creation.message_uuid
    assert deletion.source_uuid is None
    with contexts.Context().session_manager():
        deletion.do()
    assert _states(first) == _messages(first) == []
    for table in ("messenger_message_placements", "m_workspace_messages"):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE project_id = %s", (first.project_id,)
        ).fetchone() == (0,)


def test_message_states_update_content_without_changing_personal_flags(message_sources):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    source.content.workspace_content = "Updated content"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    source.read = False
    source.starred = False
    source.mentioned = False
    source.update()
    (update,) = _states(account)
    assert update.action == "update"
    with contexts.Context().session_manager():
        update.do()
    (message,) = _messages(account)
    assert message.payload.content == "Updated content"
    assert message.created_at == datetime.datetime.fromtimestamp(
        source.timestamp, tz=datetime.timezone.utc
    )
    assert message.updated_at == source.content.workspace_content_updated_at
    stored = models.ZulipMessage.objects.get_one(
        filters={"uuid": filters.EQ(source.uuid)}
    )
    assert (stored.read, stored.starred, stored.mentioned) == (False, False, False)


def test_message_creation_uses_the_current_author_account_owner(message_sources, db):
    account, source = message_sources()
    owner_uuid = uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"rebound-author-{owner_uuid}")
    account.properties["owner_user_uuid"].set_value_force(owner_uuid)
    account.update()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    assert _messages(account)[0].author_uuid == owner_uuid


@pytest.mark.parametrize("scope", ("host", "project"))
def test_message_identities_are_scoped_by_host_and_project(message_sources, scope):
    first, source = message_sources()
    second, other = message_sources(
        project_id=first.project_id if scope == "host" else None,
        host="other.zulip.example.com" if scope == "host" else None,
        stream_id=43,
    )
    _prepare(first)
    _prepare(second)
    states = {state.message_uuid: state for state in _states(first) + _states(second)}
    assert len(states) == 2
    with contexts.Context().session_manager():
        for state in states.values():
            state.do()
    assert _states(first) == _states(second) == []
    assert {
        message.author_uuid for message in _messages(first) + _messages(second)
    } == {first.owner_user_uuid, second.owner_user_uuid}
    source.delete()
    (deletion,) = _states(first)
    assert deletion.message_uuid == uuid.uuid5(
        first.project_id, str(source.message_uuid)
    )
    assert deletion.action == "delete"
    with contexts.Context().session_manager():
        deletion.do()
    assert len(_messages(second)) == 1


def test_canonical_message_creation_does_not_require_workspace_parents(message_sources):
    account, source = message_sources()
    (state,) = _states(account)
    with contexts.Context().session_manager():
        for user_state in models.ZulipProjectUserState.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        ):
            user_state.do()
        state.do()
    assert (
        v2_models.WorkspaceStream.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    assert len(_messages(account)) == 1
    assert _states(account) == []
    source.delete()
    (deletion,) = _states(account)
    assert deletion.action == "delete"


@pytest.mark.parametrize("case", ("native_message", "other_provider"))
def test_message_states_preserve_native_data_and_filter_other_providers(
    message_sources, case
):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    if case == "native_message":
        v2_models.WorkspaceMessage(
            uuid=state.message_uuid,
            project_id=account.project_id,
            author_uuid=account.owner_user_uuid,
            payload=message_payloads.MarkdownPayload(content="Native message"),
            source={"kind": "native"},
            ingest_sequence=1,
        ).insert()
    else:
        account.provider = "other"
        account.update()
    assert _states(account) == []
    source.delete()
    assert _states(account) == []


def test_message_state_restores_soft_deleted_messages(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    original = _messages(account)[0]
    db.execute(
        "UPDATE messenger_messages SET deleted_at = NOW() WHERE project_id = %s",
        (account.project_id,),
    )
    (restoration,) = _states(account)
    assert restoration.action == "create"
    with contexts.Context().session_manager():
        restoration.do()
    assert _states(account) == []
    restored = _messages(account)[0]
    assert restored.deleted_at is None
    assert restored.ingest_sequence == original.ingest_sequence
    assert restored.payload.content == original.payload.content


def test_reconciliation_batches_messages_and_schedules_continuation(message_sources):
    account, source = message_sources()
    second, other = message_sources(
        project_id=account.project_id, message_id=43, sender_id=8
    )
    _prepare(account)
    task = models.Task(content=models.ReconciliationContent(batch_size=1, interval=17))
    task.insert()
    for remaining in (1, 1, 0, 0, 0, 0):
        with contexts.Context().session_manager():
            task.execute()
        assert task.status == "done"
        assert len(_states(account)) == remaining
        (task,) = models.Task.objects.get_all(
            filters={"project_id": filters.Is(None), "status": filters.EQ("new")}
        )
        assert task.not_before_at is None
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    (waiting,) = models.Task.objects.get_all(
        filters={"project_id": filters.Is(None), "status": filters.EQ("waiting")}
    )
    assert waiting.not_before_at is not None


def test_message_failure_rolls_back_all_reconciliation_stages(message_sources):
    account, source = message_sources()
    task = models.Task(content=models.ReconciliationContent())
    task.insert()
    with mock.patch.object(
        v2_models.WorkspaceMessage,
        "insert",
        side_effect=RuntimeError("Message failed"),
    ):
        with contexts.Context().session_manager():
            task.execute()
    assert task.status == "retry"
    assert _messages(account) == []
    assert (
        v2_models.WorkspaceStream.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    assert (
        len(
            models.ZulipProjectUserState.objects.get_all(
                filters={"project_id": filters.EQ(account.project_id)}
            )
        )
        == 1
    )


def test_message_state_view_migration_is_reversible(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0219-Add-Zulip-message-reconciliation-view-004ba4.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_message_states')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    (restored,) = _states(account)
    assert restored.as_plain_dict() == state.as_plain_dict()


def test_source_changes_include_content_flags_and_identity_updates(message_sources, db):
    account, source = message_sources()
    (original,) = _changes(models.ZulipMessageChange, account)
    assert original.message_uuid == uuid.uuid5(
        account.project_id, str(source.message_uuid)
    )
    assert original.get_id() == (account.project_id, original.message_uuid)
    assert _changes(v2_models.WorkspaceMessageChange, account) == []
    source.content.workspace_content = "Edited body"
    source.content.update()
    (edited,) = _changes(models.ZulipMessageChange, account)
    assert edited.revision > original.revision
    source.read = False
    source.update()
    (flags,) = _changes(models.ZulipMessageChange, account)
    assert flags.revision > edited.revision
    source.message_uuid = uuid.uuid4()
    source.update()
    assert {
        change.message_uuid for change in _changes(models.ZulipMessageChange, account)
    } == {
        original.message_uuid,
        uuid.uuid5(account.project_id, str(source.message_uuid)),
    }


def test_workspace_changes_track_updates_deletions_and_ignore_native_messages(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    (created,) = _changes(v2_models.WorkspaceMessageChange, account)
    (message,) = _messages(account)
    message.payload = message_payloads.MarkdownPayload(content="Updated Workspace body")
    message.update()
    (updated,) = _changes(v2_models.WorkspaceMessageChange, account)
    assert updated.revision > created.revision
    _clear_changes(db, account)
    message.delete()
    (deleted,) = _changes(v2_models.WorkspaceMessageChange, account)
    assert deleted.message_uuid == message.uuid
    assert _changes(models.ZulipMessageChange, account) == []
    assert _states(account)[0].action == "create"
    native = v2_models.WorkspaceMessage(
        project_id=account.project_id,
        author_uuid=account.owner_user_uuid,
        payload=message_payloads.MarkdownPayload(content="Native body"),
        source={"kind": "native"},
        ingest_sequence=1,
    )
    native.insert()
    native.payload = message_payloads.MarkdownPayload(content="Updated native body")
    native.update()
    native.delete()
    assert [
        change.message_uuid
        for change in _changes(v2_models.WorkspaceMessageChange, account)
    ] == [message.uuid]


@pytest.mark.parametrize("root", ("account", "channel", "topic", "content"))
def test_cascading_source_deletions_mark_the_message(message_sources, db, root):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    _clear_changes(db, account)
    {
        "account": account,
        "channel": source.channel,
        "topic": source.topic,
        "content": source.content,
    }[root].delete()
    (change,) = _changes(models.ZulipMessageChange, account)
    assert change.message_uuid == state.message_uuid
    assert _states(account)[0].action == "delete"


def test_provider_change_marks_observed_messages(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
    _clear_changes(db, account)
    account.provider = "other"
    account.update()
    assert (
        _changes(models.ZulipMessageChange, account)[0].message_uuid
        == state.message_uuid
    )
    assert _states(account)[0].action == "delete"


def test_batch_counts_a_shared_key_once_even_without_a_difference(message_sources):
    account, source = message_sources()
    _prepare(account)
    (state,) = _states(account)
    with contexts.Context().session_manager():
        state.do()
        _placement_states(account)[0].do()
        _user_states(account)[0].do()
        assert models.ZulipMessageState.reconcile_batch(1) == 1
    assert _changes(models.ZulipMessageChange, account) == []
    assert _changes(v2_models.WorkspaceMessageChange, account) == []
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(1) == 0


@pytest.mark.parametrize("side", ("zulip", "workspace"))
def test_concurrent_updates_survive_batch_acknowledgement(
    message_sources, db, monkeypatch, side
):
    account, source = message_sources()
    _prepare(account)
    model = models.ZulipMessageChange
    table = "bridge_zulip_v5_messages"
    if side == "workspace":
        with contexts.Context().session_manager():
            _states(account)[0].do()
            _placement_states(account)[0].do()
            _user_states(account)[0].do()
        model = v2_models.WorkspaceMessageChange
        table = "messenger_messages"
    snapshot = _changes(model, account)[0]
    acknowledge = model.acknowledge_many

    def update_before_acknowledgement(changes):
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as writer:
            writer.execute(
                f"UPDATE {table} SET updated_at = updated_at WHERE project_id = %s",
                (account.project_id,),
            )
        acknowledge(changes)

    monkeypatch.setattr(model, "acknowledge_many", update_before_acknowledgement)
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(10) == 1
    (pending,) = _changes(model, account)
    assert pending.revision > snapshot.revision
    assert pending.message_uuid == snapshot.message_uuid


@pytest.mark.parametrize("side", ("zulip", "workspace"))
def test_reinserted_marks_cannot_be_acknowledged_by_an_old_revision(
    message_sources, db, side
):
    account, source = message_sources()
    _prepare(account)
    model = models.ZulipMessageChange
    if side == "workspace":
        with contexts.Context().session_manager():
            _states(account)[0].do()
        model = v2_models.WorkspaceMessageChange
    (old,) = _changes(model, account)
    with contexts.Context().session_manager():
        old.acknowledge()
    table = "bridge_zulip_v5_messages" if side == "zulip" else "messenger_messages"
    db.execute(
        f"UPDATE {table} SET updated_at = updated_at WHERE project_id = %s",
        (account.project_id,),
    )
    (new,) = _changes(model, account)
    assert new.revision > old.revision
    with contexts.Context().session_manager():
        old.acknowledge()
    assert _changes(model, account)[0].revision == new.revision


def test_tracking_and_acknowledgement_roll_back_together(message_sources, db):
    account, source = message_sources()
    (pending,) = _changes(models.ZulipMessageChange, account)
    with pytest.raises(RuntimeError, match="Rollback changes"):
        with contexts.Context().session_manager():
            pending.acknowledge()
            source.read = False
            source.update()
            raise RuntimeError("Rollback changes")
    assert _changes(models.ZulipMessageChange, account)[0].revision == pending.revision
    assert db.execute(
        "SELECT read FROM bridge_zulip_v5_messages WHERE uuid = %s", (source.uuid,)
    ).fetchone() == (True,)


def test_change_migration_backfills_both_sides_and_is_reversible(message_sources, db):
    account, source = message_sources()
    other_account, other = message_sources(
        project_id=account.project_id, sender_id=8, message_id=43
    )
    _prepare(account)
    states = {state.source_uuid: state for state in _states(account)}
    with contexts.Context().session_manager():
        states[other.uuid].do()
    other.delete()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0220-Track-message-changes-for-bounded-Zulip-reconciliation-42f876.py"
    engine.rollback_migration(migration)
    try:
        for table in ("bridge_zulip_v5_message_changes", "messenger_message_changes"):
            assert db.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,)
        assert {state.action for state in _states(account)} == {"create", "delete"}
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert [
        change.message_uuid for change in _changes(models.ZulipMessageChange, account)
    ] == [states[source.uuid].message_uuid]
    assert [
        change.message_uuid
        for change in _changes(v2_models.WorkspaceMessageChange, account)
    ] == [states[other.uuid].message_uuid]
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(10) == 2
    assert [message.uuid for message in _messages(account)] == [
        states[source.uuid].message_uuid
    ]
    assert _states(account) == []


def _write_workspace_content(db, message, text, edited_at):
    db.execute(
        "UPDATE messenger_messages SET payload = jsonb_build_object("
        "'kind', 'markdown', 'content', %s::text), updated_at = %s, content_updated_at = %s "
        "WHERE project_id = %s AND uuid = %s",
        (
            text,
            edited_at.replace(tzinfo=None),
            edited_at.replace(tzinfo=None),
            message.project_id,
            message.uuid,
        ),
    )


@pytest.mark.parametrize(
    "workspace_year,source_year,winner",
    ((2024, 2025, "source"), (2025, 2024, "workspace"), (2025, 2025, "workspace")),
)
def test_content_reconciliation_uses_the_last_edit_and_preserves_creation_time(
    message_sources, db, workspace_year, source_year, winner
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (message,) = _messages(account)
    created_at = message.created_at
    workspace_time = datetime.datetime(
        workspace_year, 1, 1, tzinfo=datetime.timezone.utc
    )
    source_time = datetime.datetime(source_year, 1, 1, tzinfo=datetime.timezone.utc)
    _write_workspace_content(db, message, "Workspace edit 🌍", workspace_time)
    source.content.workspace_content = "Source edit 📝"
    source.content.workspace_content_updated_at = source_time
    source.content.update()
    (state,) = _states(account)
    assert state.action == "update"
    with contexts.Context().session_manager():
        state.do()
    (stored,) = _messages(account)
    content = models.MessageContent.objects.get_one(
        filters={"uuid": filters.EQ(source.content.uuid)}
    )
    expected = "Source edit 📝" if winner == "source" else "Workspace edit 🌍"
    expected_hash = hashlib.sha256(expected.encode()).hexdigest()
    assert stored.payload.content == content.workspace_content == expected
    assert stored.content_hash == content.workspace_content_hash == expected_hash
    assert (
        stored.content_updated_at
        == content.workspace_content_updated_at
        == max(workspace_time, source_time)
    )
    assert (
        stored.created_at
        == created_at
        == datetime.datetime.fromtimestamp(source.timestamp, tz=datetime.timezone.utc)
    )
    assert stored.updated_at == max(workspace_time, source_time)
    assert content.zulip_content == "Original **message**"
    assert (source.read, source.starred, source.mentioned) == (True, True, True)
    assert _states(account) == []
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(10) == 1
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(10) == 1
    assert _changes(models.ZulipMessageChange, account) == []
    assert _changes(v2_models.WorkspaceMessageChange, account) == []


def test_content_reconciliation_uses_content_dates_instead_of_observation_dates(
    message_sources, db
):
    account, old_copy = message_sources()
    _, latest_copy = message_sources(project_id=account.project_id, sender_id=8)
    latest_copy.content.workspace_content = "The most recent content"
    latest_copy.content.workspace_content_updated_at += datetime.timedelta(days=1)
    latest_copy.content.update()
    db.execute(
        "UPDATE bridge_zulip_v5_messages SET updated_at = '2099-01-01' WHERE uuid = %s",
        (old_copy.uuid,),
    )
    _prepare(account)
    (state,) = _states(account)
    assert state.source_uuid == latest_copy.uuid
    with contexts.Context().session_manager():
        state.do()
    (message,) = _messages(account)
    assert message.payload.content == latest_copy.content.workspace_content
    assert (
        message.content_updated_at == latest_copy.content.workspace_content_updated_at
    )
    assert message.updated_at == latest_copy.content.workspace_content_updated_at
    assert message.created_at == datetime.datetime.fromtimestamp(
        latest_copy.timestamp, tz=datetime.timezone.utc
    )
    for copy in (old_copy, latest_copy):
        content = models.MessageContent.objects.get_one(
            filters={"uuid": filters.EQ(copy.content.uuid)}
        )
        assert content.workspace_content == message.payload.content
        assert content.workspace_content_updated_at == message.content_updated_at
    assert _states(account) == []


def test_metadata_updates_do_not_win_over_a_content_edit(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (original,) = _messages(account)
    db.execute(
        "UPDATE messenger_messages SET updated_at = '2099-01-01', reactions = '{}'::jsonb "
        "WHERE project_id = %s AND uuid = %s",
        (account.project_id, original.uuid),
    )
    source.content.workspace_content = "A later actual edit"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (updated,) = _messages(account)
    assert updated.payload.content == "A later actual edit"
    assert updated.content_updated_at == source.content.workspace_content_updated_at
    assert updated.updated_at.year == 2099
    assert updated.created_at == original.created_at


def test_content_reconciliation_reloads_the_winner_after_reading_the_view(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    source.content.workspace_content = "Earlier source edit"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    (state,) = _states(account)
    (message,) = _messages(account)
    later = source.content.workspace_content_updated_at + datetime.timedelta(days=1)
    _write_workspace_content(db, message, "A concurrent newer edit", later)
    with contexts.Context().session_manager():
        state.do()
    (updated,) = _messages(account)
    content = models.MessageContent.objects.get_one(
        filters={"uuid": filters.EQ(source.content.uuid)}
    )
    assert (
        updated.payload.content
        == content.workspace_content
        == "A concurrent newer edit"
    )
    assert updated.content_updated_at == content.workspace_content_updated_at == later


def test_equal_hashes_align_edit_times_without_replacing_the_payload(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (message,) = _messages(account)
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (updated,) = _messages(account)
    assert updated.payload.as_plain_dict() == message.payload.as_plain_dict()
    assert updated.content_hash == message.content_hash
    assert updated.content_updated_at == source.content.workspace_content_updated_at
    assert updated.created_at == message.created_at
    assert _states(account) == []


def test_content_failure_rolls_back_the_payload_and_keeps_the_marks(
    message_sources, monkeypatch
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (original,) = _messages(account)
    source.content.workspace_content = "A changed payload"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    pending = _changes(models.ZulipMessageChange, account)[0]
    acknowledge = models.ZulipMessageChange.acknowledge_many

    def fail_after_acknowledgement(changes):
        acknowledge(changes)
        raise RuntimeError("Cannot finish content reconciliation")

    monkeypatch.setattr(
        models.ZulipMessageChange, "acknowledge_many", fail_after_acknowledgement
    )
    with pytest.raises(RuntimeError, match="Cannot finish content"):
        with contexts.Context().session_manager():
            models.ZulipMessageState.reconcile_batch(10)
    (message,) = _messages(account)
    assert message.payload.content == original.payload.content
    assert message.content_updated_at == original.content_updated_at
    assert _changes(models.ZulipMessageChange, account)[0].revision == pending.revision


def test_content_migration_backfills_marks_and_preserves_source_creation_time(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (original,) = _messages(account)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0221-Reconcile-Zulip-message-content-by-hash-and-edit-time-68dbbd.py"
    engine.rollback_migration(migration)
    try:
        _clear_changes(db, account)
        assert db.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'messenger_messages' "
            "AND column_name IN ('content_hash', 'content_updated_at')"
        ).fetchone() == (0,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    (message,) = _messages(account)
    assert message.created_at == original.created_at
    assert message.content_updated_at == original.updated_at
    assert (
        message.content_hash
        == hashlib.sha256(message.payload.content.encode()).hexdigest()
    )
    assert len(_changes(models.ZulipMessageChange, account)) == 1
    assert len(_changes(v2_models.WorkspaceMessageChange, account)) == 1
    assert _states(account) == []


def test_workspace_edits_record_utc_content_time_independently_of_database_timezone(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    (original,) = _messages(account)
    before = datetime.datetime.now(datetime.timezone.utc)
    db.execute("SET TIME ZONE 'Europe/Moscow'")
    db.execute(
        "UPDATE messenger_messages SET payload = "
        '\'{"kind":"markdown","content":"A real edit"}\'::jsonb, updated_at = NOW() '
        "WHERE project_id = %s AND uuid = %s",
        (account.project_id, original.uuid),
    )
    after = datetime.datetime.now(datetime.timezone.utc)
    (updated,) = _messages(account)
    assert before <= updated.content_updated_at <= after
    assert updated.created_at == original.created_at
    assert updated.content_hash == hashlib.sha256(b"A real edit").hexdigest()


def test_content_reconciliation_does_not_change_another_project(message_sources, db):
    first, source = message_sources()
    second, other = message_sources(stream_id=43)
    _prepare(first)
    _prepare(second)
    with contexts.Context().session_manager():
        _states(first)[0].do()
        _states(second)[0].do()
    (message,) = _messages(first)
    _write_workspace_content(
        db,
        message,
        "First project only",
        datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
    )
    with contexts.Context().session_manager():
        _states(first)[0].do()
    assert _messages(second)[0].payload.content == "Workspace **message**"
    other_content = models.MessageContent.objects.get_one(
        filters={"uuid": filters.EQ(other.content.uuid)}
    )
    assert other_content.workspace_content == "Workspace **message**"


def _move_source(source, stream_id=None, subject="Moved", edited_at=None):
    channel = source.channel
    if stream_id is not None:
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, "zulip.example.com")
        channel = models.ZulipChannel(
            external_account_uuid=source.external_account_uuid,
            stream_uuid=uuid.uuid5(namespace, str(stream_id)),
            stream_id=stream_id,
            name="Another channel",
            description="",
            rendered_description="",
            invite_only=False,
            is_web_public=False,
            history_public_to_subscribers=True,
            is_archived=False,
        )
        channel.insert()
    topic_uuid = uuid.uuid5(channel.stream_uuid, subject)
    topic = models.ZulipTopic.objects.get_one_or_none(
        filters={"uuid": filters.EQ(topic_uuid)}
    )
    if topic is None:
        topic = models.ZulipTopic(
            uuid=topic_uuid,
            stream_id=channel.stream_id,
            name=subject or "General Topic",
            max_id=source.message_id,
        )
        topic.insert()
    source.channel = channel
    source.topic = topic
    if edited_at is not None:
        source.placement_updated_at = edited_at
    source.update()
    return channel, topic


def test_message_batch_creates_a_stable_placement_and_checks_both_api_projections(
    message_sources, db, api
):
    account, source = message_sources()
    _prepare(account)
    (creation,) = _states(account)
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(1) == 1
    (placement,) = _placements(account)
    assert placement.uuid == uuid.uuid5(creation.message_uuid, "zulip-placement")
    assert placement.message_uuid == creation.message_uuid
    assert placement.stream_uuid == source.channel.stream_uuid
    assert placement.topic_uuid == source.topic.uuid
    assert placement.created_at == _messages(account)[0].created_at
    assert _placement_states(account) == []
    legacy = db.execute(
        "SELECT uuid, canonical_message_uuid, created_at FROM m_workspace_messages "
        "WHERE project_id = %s",
        (account.project_id,),
    ).fetchone()
    assert legacy[:2] == (placement.uuid, placement.message_uuid)
    assert legacy[2] == placement.created_at.replace(tzinfo=None)
    response = api.get(
        "/v1/messenger/messages/",
        user=account.owner_user_uuid,
        project=account.project_id,
        params={"stream_uuid": str(placement.stream_uuid)},
    )
    assert response.status_code == 200, response.text
    assert [row["uuid"] for row in response.json()] == [str(placement.uuid)]
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        response = api.get(
            "/v1/messenger/messages/",
            user=account.owner_user_uuid,
            project=account.project_id,
            params={"stream_uuid": str(placement.stream_uuid)},
        )
        assert response.status_code == 200, response.text
        assert [row["uuid"] for row in response.json()] == [str(placement.uuid)]
        assert response.json()[0]["read"] is True
        assert response.json()[0]["starred"] is True
        assert response.json()[0]["mentioned"] is True
    finally:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())
    assert _changes(models.ZulipMessageChange, account) == []
    assert len(_changes(v2_models.WorkspaceMessageChange, account)) == 1
    with contexts.Context().session_manager():
        assert models.ZulipMessageState.reconcile_batch(1) == 1
    assert _changes(v2_models.WorkspaceMessageChange, account) == []
    for table in ("messenger_user_message_bindings", "messenger_user_message_states"):
        assert db.execute(
            f"SELECT count(*) FROM {table} WHERE project_id = %s", (account.project_id,)
        ).fetchone() == (1,)


@pytest.mark.parametrize("stream_id,subject", ((None, "Another topic"), (43, "")))
def test_source_moves_keep_the_placement_identity(
    message_sources, db, stream_id, subject
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (original,) = _placements(account)
    message = _messages(account)[0]
    channel, topic = _move_source(source, stream_id, subject)
    _prepare(account)
    (state,) = _placement_states(account)
    assert state.action == "update"
    assert state.placement_uuid == original.uuid
    assert state.get_id() == (account.project_id, message.uuid)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (moved,) = _placements(account)
    assert moved.uuid == original.uuid
    assert moved.created_at == original.created_at
    assert (moved.stream_uuid, moved.topic_uuid) == (channel.stream_uuid, topic.uuid)
    assert _messages(account)[0].created_at == message.created_at
    assert db.execute(
        "SELECT stream_uuid, topic_uuid, created_at FROM m_workspace_messages WHERE uuid = %s",
        (moved.uuid,),
    ).fetchone() == (
        channel.stream_uuid,
        topic.uuid,
        message.created_at.replace(tzinfo=None),
    )
    assert _placement_states(account) == []


def test_old_observation_flags_cannot_reverse_a_move(message_sources):
    account, source = message_sources()
    _, old_copy = message_sources(project_id=account.project_id, sender_id=8)
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    _, topic = _move_source(source)
    _prepare(account)
    old_copy.read = False
    old_copy.update()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert _placements(account)[0].topic_uuid == topic.uuid


def test_workspace_placement_changes_are_marked_and_repaired(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
        models.ZulipMessageState.reconcile_batch(10)
    (placement,) = _placements(account)
    old_topic = source.topic.uuid
    _, topic = _move_source(source)
    _prepare(account)
    placement.properties["topic_uuid"].set_value_force(topic.uuid)
    placement.update()
    source.topic = models.ZulipTopic.objects.get_one(
        filters={"uuid": filters.EQ(old_topic)}
    )
    source.update()
    assert len(_changes(v2_models.WorkspaceMessageChange, account)) == 1
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert _placements(account)[0].topic_uuid == old_topic
    _placements(account)[0].delete()
    assert _placements(account) == []
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert _placements(account)[0].uuid == placement.uuid


@pytest.mark.parametrize("deleted_root", ("source", "canonical"))
def test_message_deletion_cascades_to_placement_and_legacy_projection(
    message_sources, db, deleted_root
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (placement,) = _placements(account)
    with contexts.Context().session_manager():
        if deleted_root == "source":
            source.delete()
            models.ZulipMessageState.reconcile_batch(10)
        else:
            _messages(account)[0].delete()
    assert _placements(account) == _messages(account) == []
    assert db.execute(
        "SELECT count(*) FROM m_workspace_messages WHERE uuid = %s", (placement.uuid,)
    ).fetchone() == (0,)


def test_placement_deletion_preserves_unmanaged_copies(message_sources):
    account, source = message_sources()
    _, other = message_sources(
        project_id=account.project_id, sender_id=8, message_id=99, stream_id=43
    )
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
        _states(account)[0].do()
        for state in _placement_states(account):
            state.do()
    message_uuid = uuid.uuid5(account.project_id, str(source.message_uuid))
    forwarded = v2_models.WorkspaceMessagePlacement(
        project_id=account.project_id,
        message_uuid=message_uuid,
        stream_uuid=other.channel.stream_uuid,
        topic_uuid=other.topic.uuid,
    )
    forwarded.insert()
    source.delete()
    (deletion,) = _placement_states(account)
    assert deletion.action == "delete"
    with contexts.Context().session_manager():
        deletion.do()
    assert forwarded.uuid in [row.uuid for row in _placements(account)]
    assert message_uuid in [row.uuid for row in _messages(account)]


def test_placement_failure_rolls_back_content_and_keeps_the_batch(
    message_sources, monkeypatch
):
    account, source = message_sources()
    _prepare(account)
    pending = _changes(models.ZulipMessageChange, account)[0]

    def fail_placement(self):
        raise RuntimeError("Placement failed")

    monkeypatch.setattr(models.ZulipMessagePlacementState, "do", fail_placement)
    with pytest.raises(RuntimeError, match="Placement failed"):
        with contexts.Context().session_manager():
            models.ZulipMessageState.reconcile_batch(10)
    assert _messages(account) == _placements(account) == []
    assert _changes(models.ZulipMessageChange, account)[0].revision == pending.revision


@pytest.mark.parametrize("existing_placement", (False, True))
def test_placement_migration_is_reversible_and_backfills_existing_messages(
    message_sources, db, existing_placement
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0222-Reconcile-Zulip-message-placements-1f12c4.py"
    (message,) = _messages(account)
    placement_uuid = uuid.uuid5(message.uuid, "zulip-placement")
    engine.rollback_migration(migration)
    try:
        if existing_placement:
            v2_models.WorkspaceMessagePlacement(
                uuid=placement_uuid,
                project_id=account.project_id,
                message_uuid=message.uuid,
                stream_uuid=source.channel.stream_uuid,
                topic_uuid=source.topic.uuid,
            ).insert()
        _clear_changes(db, account)
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_message_placement_states')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    if existing_placement:
        assert _placement_states(account) == []
        assert db.execute(
            "SELECT canonical_message_uuid FROM m_workspace_messages WHERE uuid = %s",
            (placement_uuid,),
        ).fetchone() == (message.uuid,)
        message.delete()
        assert db.execute(
            "SELECT count(*) FROM m_workspace_messages WHERE uuid = %s",
            (placement_uuid,),
        ).fetchone() == (0,)
    else:
        (state,) = _placement_states(account)
        assert state.action == "create"
        with contexts.Context().session_manager():
            models.ZulipMessageState.reconcile_batch(10)
        assert _placements(account)[0].uuid == state.placement_uuid
    assert (
        models.ZulipMessage.objects.get_one(
            filters={"uuid": filters.EQ(source.uuid)}
        ).placement_updated_at
        == source.content.zulip_content_updated_at
    )


def test_parent_cleanup_waits_until_the_placement_has_moved(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    old_channel = source.channel
    old_topic_uuid = source.topic.uuid
    placement_uuid = _placements(account)[0].uuid
    channel, topic = _move_source(source, stream_id=43)
    old_channel.delete()
    _prepare(account)
    assert _placements(account)[0].uuid == placement_uuid
    assert _placement_states(account)[0].action == "update"
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    _prepare(account)
    (placement,) = _placements(account)
    assert placement.uuid == placement_uuid
    assert (placement.stream_uuid, placement.topic_uuid) == (
        channel.stream_uuid,
        topic.uuid,
    )
    assert (
        v2_models.WorkspaceStreamTopic.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(account.project_id),
                "uuid": filters.EQ(old_topic_uuid),
            }
        )
        is None
    )
    assert (
        v2_models.WorkspaceStream.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(account.project_id),
                "uuid": filters.EQ(old_channel.stream_uuid),
            }
        )
        is None
    )


def _viewer_rows(account, model):
    return model.objects.get_all(filters={"project_id": filters.EQ(account.project_id)})


def test_imported_user_flags_preserve_local_changes_until_a_new_zulip_snapshot(
    message_sources,
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (state,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert state.read_at == source.updated_at
    assert state.zulip_source_uuid == source.uuid
    assert state.zulip_flags_updated_at == source.updated_at
    state.read_at = None
    state.starred = False
    state.pinned = True
    state.update()
    assert _user_states(account) == []
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (local,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert local.read_at is None and not local.starred and local.pinned
    source.mentioned = False
    source.update()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (updated,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert updated.read_at is not None and updated.starred
    assert not updated.mentioned and updated.pinned
    assert (
        updated.binding_uuid
        == _viewer_rows(account, v2_models.WorkspaceUserMessageBinding)[0].uuid
    )


def test_rebinding_switches_message_access_and_authorship(message_sources, db):
    account, source = message_sources()
    old_owner = account.owner_user_uuid
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (old_state,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    new_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, new_owner, f"rebound-owner-{new_owner}")
    account.properties["owner_user_uuid"].set_value_force(new_owner)
    account.update()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (binding,) = _viewer_rows(account, v2_models.WorkspaceUserMessageBinding)
    (state,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert binding.user_uuid == state.user_uuid == new_owner
    assert binding.relation_role == "author"
    assert state.uuid != old_state.uuid
    assert _messages(account)[0].author_uuid == new_owner
    assert (
        _viewer_rows(account, v2_models.WorkspaceUserMessage)[0].user_uuid == new_owner
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_user_message_states WHERE project_id=%s AND user_uuid=%s",
        (account.project_id, old_owner),
    ).fetchone() == (0,)


def test_imported_false_flags_are_preserved(message_sources):
    first, source = message_sources()
    source.read = source.starred = source.mentioned = False
    source.update()
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (state,) = _viewer_rows(first, v2_models.WorkspaceUserMessageState)
    assert state.zulip_source_uuid == source.uuid
    assert state.read_at is None and not state.starred and not state.mentioned
    assert len(_viewer_rows(first, v2_models.WorkspaceUserMessageBinding)) == 1


def test_only_observing_account_owners_receive_message_access(message_sources):
    first, source = message_sources()
    second, other = message_sources(
        project_id=first.project_id, sender_id=8, message_id=99
    )
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    expected = {
        (uuid.uuid5(first.project_id, str(source.message_uuid)), first.owner_user_uuid),
        (uuid.uuid5(first.project_id, str(other.message_uuid)), second.owner_user_uuid),
    }
    actual = {
        (row.canonical_message_uuid, row.user_uuid)
        for row in _viewer_rows(first, v2_models.WorkspaceUserMessage)
    }
    assert actual == expected


def test_native_history_rebuild_preserves_observed_zulip_access(message_sources):
    first, source = message_sources()
    second, other = message_sources(
        project_id=first.project_id, sender_id=8, message_id=99
    )
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    destination = v2_models.WorkspaceStreamTopic(
        project_id=first.project_id,
        stream_uuid=other.channel.stream_uuid,
        name="Forwarded",
    )
    destination.insert()
    forwarded = v2_models.WorkspaceMessagePlacement(
        project_id=first.project_id,
        message_uuid=uuid.uuid5(first.project_id, str(source.message_uuid)),
        stream_uuid=other.channel.stream_uuid,
        topic_uuid=destination.uuid,
    )
    forwarded.insert()
    membership = messenger_models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "project_id": filters.EQ(second.project_id),
            "user_uuid": filters.EQ(second.owner_user_uuid),
            "stream_uuid": filters.EQ(other.channel.stream_uuid),
        }
    )
    membership.role = "owner"
    membership.update()
    with contexts.Context().session_manager() as session:
        assert v2_projection.drain_projection_queue(session, str(uuid.uuid4())) > 0
        assert (
            session.execute(
                "SELECT count(*) FROM messenger_projection_tasks "
                "WHERE project_id = %s AND status IN ('failed', 'dead_letter')",
                (first.project_id,),
            ).fetchone()["count"]
            == 0
        )
    visible = {
        (row.uuid, row.user_uuid)
        for row in _viewer_rows(first, v2_models.WorkspaceUserMessage)
    }
    assert visible == {
        (
            uuid.uuid5(
                uuid.uuid5(first.project_id, str(source.message_uuid)),
                "zulip-placement",
            ),
            first.owner_user_uuid,
        ),
        (
            uuid.uuid5(
                uuid.uuid5(first.project_id, str(other.message_uuid)), "zulip-placement"
            ),
            second.owner_user_uuid,
        ),
        (forwarded.uuid, second.owner_user_uuid),
    }


def test_removing_an_observation_revokes_its_owner_and_cascades_the_state(
    message_sources,
):
    first, source = message_sources()
    second, copy = message_sources(project_id=first.project_id, sender_id=8)
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert len(_viewer_rows(first, v2_models.WorkspaceUserMessage)) == 2
    source.delete()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert [
        row.user_uuid
        for row in _viewer_rows(first, v2_models.WorkspaceUserMessageBinding)
    ] == [second.owner_user_uuid]
    assert [
        row.user_uuid
        for row in _viewer_rows(first, v2_models.WorkspaceUserMessageState)
    ] == [second.owner_user_uuid]
    assert len(_messages(first)) == 1


def test_membership_generation_changes_rebuild_imported_state(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
        models.ZulipMessageState.reconcile_batch(10)
    (state,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    state.pinned = True
    state.update()
    membership = v2_models.WorkspaceStreamBinding.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "user_uuid": filters.EQ(account.owner_user_uuid),
            "stream_uuid": filters.EQ(source.channel.stream_uuid),
        }
    )
    membership.properties["membership_generation"].set_value_force(
        membership.membership_generation + 1
    )
    membership.update()
    assert _viewer_rows(account, v2_models.WorkspaceUserMessage) == []
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (binding,) = _viewer_rows(account, v2_models.WorkspaceUserMessageBinding)
    (updated,) = _viewer_rows(account, v2_models.WorkspaceUserMessageState)
    assert (
        binding.membership_generation
        == updated.membership_generation
        == membership.membership_generation
    )
    assert updated.uuid == state.uuid
    assert updated.starred and updated.mentioned and updated.read_at is not None
    assert not updated.pinned
    assert len(_viewer_rows(account, v2_models.WorkspaceUserMessage)) == 1


def test_user_binding_deletion_owns_state_cleanup_and_reconciliation_repairs_it(
    message_sources,
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    (binding,) = _viewer_rows(account, v2_models.WorkspaceUserMessageBinding)
    binding.delete()
    assert _viewer_rows(account, v2_models.WorkspaceUserMessageState) == []
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert (
        _viewer_rows(account, v2_models.WorkspaceUserMessageBinding)[0].uuid
        == binding.uuid
    )
    assert len(_viewer_rows(account, v2_models.WorkspaceUserMessage)) == 1


def test_user_state_failure_rolls_back_all_message_stages(message_sources, monkeypatch):
    account, source = message_sources()
    _prepare(account)
    revision = _changes(models.ZulipMessageChange, account)[0].revision

    def fail_insert(self, *args, **kwargs):
        raise RuntimeError("Cannot save user flags")

    monkeypatch.setattr(v2_models.WorkspaceUserMessageState, "insert", fail_insert)
    with pytest.raises(RuntimeError, match="Cannot save user flags"):
        with contexts.Context().session_manager():
            models.ZulipMessageState.reconcile_batch(10)
    assert _messages(account) == _placements(account) == []
    assert _viewer_rows(account, v2_models.WorkspaceUserMessageBinding) == []
    assert _changes(models.ZulipMessageChange, account)[0].revision == revision


def test_user_message_migration_is_reversible_and_requeues_existing_placements(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        _states(account)[0].do()
        _placement_states(account)[0].do()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0223-Reconcile-Zulip-user-message-bindings-and-states-6430fc.py"
    engine.rollback_migration(migration)
    try:
        _clear_changes(db, account)
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_user_message_states')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert _user_states(account)[0].action == "create"
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
    assert (
        _viewer_rows(account, v2_models.WorkspaceUserMessage)[0].user_uuid
        == account.owner_user_uuid
    )


def _refresh_counters():
    task = models.Task(
        priority=models.TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS,
        content=models.RefreshMessageCountersContent(),
    )
    task.insert()
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done", task.error_description
    with contexts.Context().session_manager() as session:
        v2_projection.drain_projection_queue(session, str(uuid.uuid4()))
        assert (
            session.execute(
                "SELECT count(*) AS count FROM messenger_projection_tasks "
                "WHERE status IN ('failed', 'dead_letter')"
            ).fetchone()["count"]
            == 0
        )
    return task


def test_counter_refresh_combines_imports_and_updates_previews_and_folders(
    message_sources, db
):
    account, source = message_sources()
    source.read = source.mentioned = False
    source.update()
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
    for offset in range(1, 11):
        last = models.ZulipMessage(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            message_uuid=uuid.uuid5(namespace, str(source.message_id + offset)),
            message_id=source.message_id + offset,
            sender_id=source.sender_id,
            timestamp=source.timestamp + offset,
            channel=source.channel,
            topic=source.topic,
            content=source.content,
        )
        last.insert()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    changes = _changes(models.ZulipMessageCounterChange, account)
    assert len(changes) == 2
    (before,) = _viewer_rows(account, v2_models.WorkspaceStreamBinding)
    assert before.unread_count == 0 and before.last_message_uuid is None
    _refresh_counters()
    assert _changes(models.ZulipMessageCounterChange, account) == []
    last_uuid = uuid.uuid5(
        uuid.uuid5(account.project_id, str(last.message_uuid)), "zulip-placement"
    )
    (stream,) = _viewer_rows(account, v2_models.WorkspaceStreamBinding)
    (topic,) = _viewer_rows(account, v2_models.WorkspaceUserTopicBinding)
    assert stream.unread_count == stream.active_unread_count == topic.unread_count == 11
    assert stream.passive_unread_count == topic.passive_unread_count == 0
    assert stream.last_message_uuid == topic.last_message_uuid == last_uuid
    folder = v2_models.WorkspaceUserFolder.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "user_uuid": filters.EQ(account.owner_user_uuid),
            "uuid": filters.EQ(uuid.UUID(int=0)),
        }
    )
    assert folder.unread_count == 11
    pending_before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
        (account.project_id,),
    ).fetchone()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
        assert models.ZulipMessageCounterChange.publish_batch(100) == 0
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()
        == pending_before
    )


@pytest.mark.parametrize("inaccessible", [None, "hidden", "stale_generation"])
def test_counter_previews_only_include_the_users_accessible_messages(
    message_sources, inaccessible
):
    first, source = message_sources()
    second, other = message_sources(
        project_id=first.project_id, sender_id=8, message_id=99
    )
    source.read = other.read = False
    source.update()
    other.properties["timestamp"].set_value_force(source.timestamp + 100)
    other.update()
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    if inaccessible == "hidden":
        binding = v2_models.WorkspaceUserMessageBinding.objects.get_one(
            filters={
                "project_id": filters.EQ(first.project_id),
                "user_uuid": filters.EQ(first.owner_user_uuid),
            }
        )
        binding.visibility = "hidden"
        binding.update()
    elif inaccessible == "stale_generation":
        state = v2_models.WorkspaceUserMessageState.objects.get_one(
            filters={
                "project_id": filters.EQ(first.project_id),
                "user_uuid": filters.EQ(first.owner_user_uuid),
            }
        )
        state.properties["membership_generation"].set_value_force(
            state.membership_generation + 1
        )
        state.update()
    _refresh_counters()
    for account, observed in ((first, source), (second, other)):
        excluded = account == first and inaccessible is not None
        expected_uuid = (
            None
            if excluded
            else uuid.uuid5(
                uuid.uuid5(first.project_id, str(observed.message_uuid)),
                "zulip-placement",
            )
        )
        stream = v2_models.WorkspaceStreamBinding.objects.get_one(
            filters={
                "project_id": filters.EQ(first.project_id),
                "user_uuid": filters.EQ(account.owner_user_uuid),
            }
        )
        topic = v2_models.WorkspaceUserTopicBinding.objects.get_one(
            filters={
                "project_id": filters.EQ(first.project_id),
                "user_uuid": filters.EQ(account.owner_user_uuid),
            }
        )
        assert stream.unread_count == topic.unread_count == (0 if excluded else 1)
        assert stream.last_message_uuid == topic.last_message_uuid == expected_uuid


def test_counter_refresh_tracks_old_and_new_scopes_on_moves_and_deletion(
    message_sources,
):
    account, source = message_sources()
    source.read = False
    source.update()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    _refresh_counters()
    old_stream, old_topic = source.channel.stream_uuid, source.topic.uuid
    channel, topic = _move_source(source, stream_id=43)
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    changes = _changes(models.ZulipMessageCounterChange, account)
    assert {row.scope_uuid for row in changes} == {
        old_stream,
        old_topic,
        channel.stream_uuid,
        topic.uuid,
    }
    _refresh_counters()
    streams = {
        row.uuid: row for row in _viewer_rows(account, v2_models.WorkspaceUserStream)
    }
    assert (
        streams[old_stream].unread_count == 0
        and streams[old_stream].last_message_uuid is None
    )
    assert streams[channel.stream_uuid].unread_count == 1
    source.delete()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    _refresh_counters()
    assert all(
        row.unread_count == 0 and row.last_message_uuid is None
        for row in _viewer_rows(account, v2_models.WorkspaceStreamBinding)
    )


def test_counter_refresh_publication_preserves_concurrent_changes(
    message_sources, monkeypatch
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    pending = _changes(models.ZulipMessageCounterChange, account)
    acknowledge = models.ZulipMessageCounterChange.acknowledge_many

    def update_before_acknowledgement(changes):
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as concurrent:
            concurrent.execute(
                "UPDATE bridge_zulip_v5_message_counter_changes SET revision=nextval('messenger_message_change_revision_seq') WHERE project_id=%s",
                (account.project_id,),
            )
        acknowledge(changes)

    monkeypatch.setattr(
        models.ZulipMessageCounterChange,
        "acknowledge_many",
        update_before_acknowledgement,
    )
    with contexts.Context().session_manager():
        assert models.ZulipMessageCounterChange.publish_batch(100) == 2
    remaining = _changes(models.ZulipMessageCounterChange, account)
    assert len(remaining) == 2
    assert min(row.revision for row in remaining) > max(row.revision for row in pending)


def test_counter_refresh_failure_rolls_back_publication_and_retries(
    message_sources, monkeypatch, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    task = models.Task(
        priority=models.TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS,
        content=models.RefreshMessageCountersContent(),
    )
    task.insert()
    before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
        (account.project_id,),
    ).fetchone()

    def fail_schedule(self):
        raise RuntimeError("Cannot schedule the next refresh")

    monkeypatch.setattr(models.Task, "insert", fail_schedule)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "retry" and task.next_retry_at is not None
    assert len(_changes(models.ZulipMessageCounterChange, account)) == 2
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()
        == before
    )


def test_counter_task_is_periodic_high_priority_and_bounded(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    service = history_import.HistoryImportService()
    with contexts.Context().session_manager():
        service._ensure_reconciliation_task()
        service._ensure_reconciliation_task()
    (scheduled,) = models.Task.objects.get_all(
        filters={
            "content": filters.JSONFields({"kind": "refresh_message_counters"}),
            "status": filters.EQ("waiting"),
        }
    )
    assert (
        scheduled.priority == models.TASK_PRIORITY_REFRESH_MESSAGE_COUNTERS == 700_000
    )
    assert scheduled.content.interval == 30
    assert (
        25
        < (
            scheduled.not_before_at - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        <= 30
    )
    task = models.Task(
        priority=700_000, content=models.RefreshMessageCountersContent(batch_size=1)
    )
    task.insert()
    assert service._get_tasks()[0].uuid == task.uuid
    scheduled.delete()
    before = datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        task.execute()
    assert len(_changes(models.ZulipMessageCounterChange, account)) == 1
    (next_task,) = models.Task.objects.get_all(
        filters={
            "content": filters.JSONFields({"kind": "refresh_message_counters"}),
            "status": filters.EQ("waiting"),
        }
    )
    assert next_task.priority == 700_000 and next_task.content.batch_size == 1
    assert next_task.not_before_at >= before + datetime.timedelta(seconds=30)


def test_counter_migration_backfills_existing_imports_and_is_reversible(
    message_sources, db
):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0225-Refresh-imported-Zulip-message-counters-periodically-09ca0b.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_message_counter_changes')"
        ).fetchone() == (None,)
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert {
        row.scope_uuid for row in _changes(models.ZulipMessageCounterChange, account)
    } == {source.channel.stream_uuid, source.topic.uuid}


def test_topic_changes_coalesce_and_ignore_content_and_viewer_flags(message_sources):
    account, source = message_sources()
    (original,) = _topic_changes(account)
    assert original.get_id() == (account.project_id, source.topic.uuid)
    assert set(original.get_id_property()) == {"project_id", "topic_uuid"}
    source.read = False
    source.starred = False
    source.update()
    source.content.workspace_content = "Edited content"
    source.content.update()
    assert _topic_changes(account)[0].revision == original.revision
    _prepare(account)
    assert _topic_changes(account) == []
    message_sources(project_id=account.project_id, message_id=43, sender_id=8)
    (created,) = _topic_changes(account)
    assert created.topic_uuid == original.topic_uuid
    assert created.revision > original.revision
    source.delete()
    assert _topic_changes(account)[0].revision > created.revision


def test_topic_moves_mark_both_topics_and_wait_for_placement_cleanup(message_sources):
    account, source = message_sources()
    _prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(10)
        models.ZulipTopicState.reconcile_batch(10)
    old_topic = source.topic.uuid
    _, destination = _move_source(source)
    assert {change.topic_uuid for change in _topic_changes(account)} == {
        old_topic,
        destination.uuid,
    }
    assert [(state.topic_uuid, state.action) for state in _topic_states(account)] == [
        (destination.uuid, "create")
    ]
    with contexts.Context().session_manager():
        models.ZulipTopicState.reconcile_batch(10)
        models.ZulipMessageState.reconcile_batch(10)
    assert _placements(account)[0].topic_uuid == destination.uuid
    assert (old_topic, "delete") in {
        (state.topic_uuid, state.action) for state in _topic_states(account)
    }
    with contexts.Context().session_manager():
        models.ZulipTopicState.reconcile_batch(10)
    assert (
        v2_models.WorkspaceStreamTopic.objects.get_one_or_none(
            filters={
                "project_id": filters.EQ(account.project_id),
                "uuid": filters.EQ(old_topic),
            }
        )
        is None
    )


@pytest.mark.parametrize("root", ("account", "channel", "topic", "content"))
def test_source_cascades_queue_topic_deletion(message_sources, root):
    account, source = message_sources()
    _prepare(account)
    topic_uuid = source.topic.uuid
    {
        "account": account,
        "channel": source.channel,
        "topic": source.topic,
        "content": source.content,
    }[root].delete()
    assert _topic_changes(account)[0].topic_uuid == topic_uuid
    assert _topic_states(account)[0].action == "delete"


@pytest.mark.parametrize("changed", ("provider", "channel", "stream", "topic"))
def test_topic_parent_and_workspace_changes_are_tracked(message_sources, db, changed):
    account, source = message_sources()
    _prepare(account)
    if changed == "provider":
        account.provider = "other"
        account.update()
    elif changed == "channel":
        source.channel.properties["stream_uuid"].set_value_force(uuid.uuid4())
        source.channel.update()
    else:
        table = "messenger_streams" if changed == "stream" else "messenger_topics"
        target = (
            source.channel.stream_uuid if changed == "stream" else source.topic.uuid
        )
        db.execute(
            f"UPDATE {table} SET deleted_at = NOW() WHERE project_id = %s AND uuid = %s",
            (account.project_id, target),
        )
    assert _topic_changes(account)[0].topic_uuid == source.topic.uuid
    assert _topic_states(account)[0].action == (
        "create" if changed == "topic" else "delete"
    )


def test_topic_batch_is_bounded_and_acknowledges_no_difference(message_sources):
    first, source = message_sources()
    second, other = message_sources(host="other.example.com")
    _prepare(first)
    _prepare(second)
    source.delete()
    other.delete()
    with contexts.Context().session_manager():
        assert models.ZulipTopicState.reconcile_batch(1) == 1
    assert sum(len(_topic_states(account)) for account in (first, second)) == 1
    with contexts.Context().session_manager():
        while models.ZulipTopicState.reconcile_batch(1):
            pass
    assert _topic_states(first) == _topic_states(second) == []
    with mock.patch.object(models.ZulipTopicState, "objects") as manager:
        manager.get_all.side_effect = AssertionError("Empty queues must skip the view")
        with contexts.Context().session_manager():
            assert models.ZulipTopicState.reconcile_batch(1) == 0


def test_topic_checks_keep_projects_separate(message_sources):
    first, source = message_sources()
    second, other = message_sources()
    assert source.topic.uuid == other.topic.uuid
    _prepare(first)
    with contexts.Context().session_manager():
        models.ZulipTopicChange.acknowledge_many(_topic_changes(second))
    source.delete()
    assert _topic_states(first)[0].action == "delete"
    assert _topic_changes(second) == _topic_states(second) == []
    with contexts.Context().session_manager():
        models.ZulipTopicState.reconcile_batch(10)
    assert (
        models.ZulipMessage.objects.get_one(
            filters={
                "project_id": filters.EQ(second.project_id),
                "uuid": filters.EQ(other.uuid),
            }
        ).topic.uuid
        == other.topic.uuid
    )


def test_concurrent_topic_changes_survive_acknowledgement(message_sources, monkeypatch):
    account, source = message_sources()
    _prepare(account)
    message_sources(project_id=account.project_id, message_id=43, sender_id=8)
    snapshot = _topic_changes(account)[0]
    acknowledge = models.ZulipTopicChange.acknowledge_many

    def change_before_acknowledgement(changes):
        with psycopg.connect(conftest.TEST_DB_URL, autocommit=True) as writer:
            writer.execute(
                "UPDATE m_external_accounts_v2 SET provider = 'other' WHERE uuid = %s",
                (account.uuid,),
            )
        acknowledge(changes)

    monkeypatch.setattr(
        models.ZulipTopicChange, "acknowledge_many", change_before_acknowledgement
    )
    with contexts.Context().session_manager():
        assert models.ZulipTopicState.reconcile_batch(10) == 1
    (pending,) = _topic_changes(account)
    assert pending.revision > snapshot.revision


def test_topic_creation_and_acknowledgement_roll_back_together(
    message_sources, monkeypatch
):
    account, source = message_sources()
    _prepare(account)
    topic = v2_models.WorkspaceStreamTopic.objects.get_one(
        filters={
            "project_id": filters.EQ(account.project_id),
            "uuid": filters.EQ(source.topic.uuid),
        }
    )
    topic.delete()
    revision = _topic_changes(account)[0].revision
    acknowledge = models.ZulipTopicChange.acknowledge_many

    def fail_after_acknowledgement(changes):
        acknowledge(changes)
        raise RuntimeError("Cannot commit topic reconciliation")

    monkeypatch.setattr(
        models.ZulipTopicChange, "acknowledge_many", fail_after_acknowledgement
    )
    with pytest.raises(RuntimeError, match="Cannot commit topic reconciliation"):
        with contexts.Context().session_manager():
            models.ZulipTopicState.reconcile_batch(10)
    assert _topic_changes(account)[0].revision == revision
    assert _topic_states(account)[0].action == "create"


def test_topic_migration_round_trip_backfills_pending_topics(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    source.delete()
    expected = _topic_states(account)[0].as_plain_dict()
    engine = migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    migration = "0224-Track-topic-changes-for-bounded-Zulip-reconciliation-0dbef1.py"
    engine.rollback_migration(migration)
    try:
        assert db.execute(
            "SELECT to_regclass('bridge_zulip_v5_topic_changes')"
        ).fetchone() == (None,)
        assert _topic_states(account)[0].as_plain_dict() == expected
    finally:
        engine.apply_migration(engine.get_latest_migration())
    assert _topic_states(account)[0].as_plain_dict() == expected
    assert _topic_changes(account)[0].topic_uuid == source.topic.uuid


def test_empty_topic_queue_does_not_scan_source_messages(message_sources, db):
    account, source = message_sources()
    _prepare(account)
    plan = db.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM bridge_zulip_v5_topic_states LIMIT 1000"
    ).fetchone()[0][0]

    def check(node):
        if node.get("Relation Name") == "bridge_zulip_v5_messages":
            assert node["Actual Loops"] == 0
        for child in node.get("Plans", []):
            check(child)

    check(plan["Plan"])
