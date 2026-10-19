# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import concurrent.futures
import importlib.util
import threading
import time
import uuid

import psycopg
import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2 as messenger_tests
from workspace.tests.integration import test_zulip_flags as flag_tests
from workspace.tests.integration import test_zulip_message_states as source_tests
from workspace.tests.integration import test_zulip_outgoing as outgoing_tests


message_sources = source_tests.message_sources
imported = flag_tests.imported
outgoing = outgoing_tests.outgoing
REACTIONS = "/v1/messenger/message_reactions/"
MESSAGES = "/v1/messenger/messages/"


def reaction(user_id=7, name="thumbs_up", code="1f44d", kind="unicode_emoji"):
    return {
        "user_id": user_id,
        "emoji_name": name,
        "emoji_code": code,
        "reaction_type": kind,
    }


def reconcile():
    for _index in range(10):
        with contexts.Context().session_manager():
            if not reactions.ReactionChange.reconcile_batch(100):
                return
    pytest.fail("Reaction reconciliation did not converge")


def facts(account):
    return v2_models.WorkspaceMessageReactionFact.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )


def pending(account):
    return models.Task.objects.get_all(
        filters={
            "external_account_uuid": filters.EQ(account.uuid),
            "content": filters.JSONFields({"kind": "update_reaction"}),
            "status": filters.In(["new", "retry"]),
        }
    )


def snapshot(imported, items, at=None):
    account, source, *_rest = imported
    at = at or datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        reactions.observe_snapshot(
            account, [{"id": source.message_id, "reactions": items}], at
        )
    reconcile()
    return at


def receive(imported, op="add", event_id=0, at=None, data=None):
    account, source, _state, subscription, *_rest = imported
    at = at or datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        models.RealtimeSubscription.append_events(
            account,
            subscription.generation,
            [
                {
                    "id": event_id,
                    "type": "reaction",
                    "op": op,
                    "message_id": source.message_id,
                    **(data or reaction()),
                }
            ],
            at,
        )
    with contexts.Context().session_manager():
        while models.RealtimeEvent.dispatch_one():
            pass
    reconcile()
    return at


def add(api, imported, name="thumbs_up"):
    account, _source, state, *_rest = imported
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    response = api.post(
        REACTIONS, json={"message_uuid": str(state.placement_uuid), "emoji_name": name}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_snapshot_projects_reactions_and_updates_public_counts_without_echo(
    imported, api
):
    account, _source, state, _subscription, zulip, _constructor = imported
    snapshot(imported, [reaction(name="+1")])
    (fact,) = facts(account)
    assert fact.user_uuid == account.owner_user_uuid and fact.emoji_name == "thumbs_up"
    snapshot(imported, [reaction()])
    assert [row.uuid for row in facts(account)] == [fact.uuid]
    messenger_tests._drain()
    api.project_id, api.user_uuid = account.project_id, account.owner_user_uuid
    response = api.get(f"{MESSAGES}{state.placement_uuid}")
    assert response.json()["reactions"] == {"thumbs_up": 1}
    assert response.json()["reaction_users"] == {
        "thumbs_up": [str(account.owner_user_uuid)]
    }
    assert pending(account) == []
    zulip.update_reaction.assert_not_called()


def test_realtime_events_in_one_poll_keep_order_and_do_not_echo(imported):
    account, *_rest = imported
    at = datetime.datetime.now(datetime.timezone.utc)
    for event_id, op in enumerate(("add", "remove", "add", "remove")):
        receive(imported, op, event_id, at)
        assert bool(facts(account)) == (op == "add")
    assert pending(account) == []


def test_snapshot_removal_and_its_barrier_reject_an_old_unknown_add(imported):
    account, *_rest = imported
    old = datetime.datetime.now(datetime.timezone.utc)
    snapshot(imported, [])
    receive(imported, at=old)
    assert facts(account) == []


def test_slow_snapshot_cannot_resurrect_a_realtime_removal(imported):
    account, *_rest = imported
    receive(imported)
    started = datetime.datetime.now(datetime.timezone.utc)
    receive(imported, "remove", 1)
    snapshot(imported, [reaction()], started)
    assert facts(account) == [] and pending(account) == []


def test_snapshot_removes_disappeared_reactions(imported):
    account, *_rest = imported
    snapshot(imported, [reaction()])
    snapshot(imported, [])
    assert facts(account) == [] and pending(account) == []


def test_local_add_and_remove_are_durable_and_use_the_actor_account(imported, api):
    account, source, _state, _subscription, zulip, constructor = imported
    public = add(api, imported, name="+1")
    assert public["emoji_name"] == "thumbs_up"
    assert reactions.ReactionIntent.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    ).present
    reconcile()
    (task,) = pending(account)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    zulip.update_reaction.assert_called_once_with(
        source.message_id, "+1", "1f44d", "unicode_emoji", True
    )
    assert constructor.call_args.args[0].uuid == account.uuid
    assert api.delete(f"{REACTIONS}{public['uuid']}").status_code == 204
    (intent,) = reactions.ReactionIntent.objects.get_all(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    assert not intent.present
    reconcile()
    (task,) = pending(account)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    assert zulip.update_reaction.call_args.args[-1] is False
    reconcile()
    assert facts(account) == [] and pending(account) == []


def test_a_late_add_cannot_override_a_local_removal(imported, api):
    account, *_rest = imported
    public = add(api, imported)
    at = datetime.datetime.now(datetime.timezone.utc)
    assert api.delete(f"{REACTIONS}{public['uuid']}").status_code == 204
    receive(imported, at=at)
    assert facts(account) == []
    assert not pending(account)[0].content.present


def test_stale_outgoing_add_is_skipped_after_local_remove(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    public = add(api, imported)
    reconcile()
    (task,) = pending(account)
    api.delete(f"{REACTIONS}{public['uuid']}")
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    zulip.update_reaction.assert_not_called()


def test_newer_event_supersedes_an_outgoing_http_retry(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    add(api, imported)
    reconcile()
    (task,) = pending(account)
    zulip.update_reaction.side_effect = requests.Timeout("Temporary timeout")
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "retry"
    receive(imported, "remove")
    assert facts(account) == []
    zulip.update_reaction.reset_mock()
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    zulip.update_reaction.assert_not_called()


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("transport", ["api", "http"])
def test_repeated_http_requests_confirm_the_desired_state(
    imported, api, present, transport
):
    account, _source, _state, _subscription, zulip, _constructor = imported
    public = add(api, imported)
    if not present:
        api.delete(f"{REACTIONS}{public['uuid']}")
    reconcile()
    (task,) = pending(account)
    code = "REACTION_ALREADY_EXISTS" if present else "REACTION_DOES_NOT_EXIST"
    if transport == "http":
        response = requests.Response()
        response.status_code = 400
        response._content = ('{"code":"' + code + '"}').encode()
        error = requests.HTTPError(response=response)
    else:
        error = client.ZulipApiError(code)
    zulip.update_reaction.side_effect = error
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    reconcile()
    assert bool(facts(account)) == present and pending(account) == []


def test_changing_emoji_schedules_remove_and_add(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    public = add(api, imported)
    reconcile()
    with contexts.Context().session_manager():
        pending(account)[0].execute()
    response = api.put(f"{REACTIONS}{public['uuid']}", json={"emoji_name": "rocket"})
    assert response.status_code == 200, response.text
    reconcile()
    tasks = pending(account)
    assert len(tasks) == 2
    zulip.update_reaction.reset_mock()
    for task in tasks:
        with contexts.Context().session_manager():
            task.execute()
    assert {
        (call.args[1], call.args[-1]) for call in zulip.update_reaction.call_args_list
    } == {("+1", False), ("rocket", True)}


def test_same_reaction_seen_by_two_accounts_creates_one_fact(imported, message_sources):
    author, source, *_rest = imported
    viewer, _copy = message_sources(project_id=author.project_id, sender_id=8)
    source_tests._prepare(viewer)
    snapshot(imported, [reaction()])
    with contexts.Context().session_manager():
        reactions.record_event(
            viewer,
            {"message_id": source.message_id, "op": "add", **reaction()},
            datetime.datetime.now(datetime.timezone.utc),
            0,
            "viewer-queue",
        )
    reconcile()
    (fact,) = facts(author)
    assert fact.user_uuid == author.owner_user_uuid


def test_reactions_use_the_readers_credentials_on_another_authors_message(
    imported, message_sources, api
):
    author, _source, _state, _subscription, zulip, constructor = imported
    reader, _copy = message_sources(project_id=author.project_id, sender_id=8)
    reader.settings = {
        **reader.settings,
        "email": "reader@example.com",
        "api_key": "reader-key",
    }
    reader.status = "active"
    reader.update()
    source_tests._prepare(reader)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    state = v2_models.WorkspaceUserMessageState.objects.get_one(
        filters={
            "project_id": filters.EQ(reader.project_id),
            "user_uuid": filters.EQ(reader.owner_user_uuid),
        }
    )
    add(api, (reader, None, state, None, None, None))
    reconcile()
    (task,) = pending(reader)
    with contexts.Context().session_manager():
        task.execute()
    assert constructor.call_args.args[0].uuid == reader.uuid
    assert facts(reader)[0].user_uuid == reader.owner_user_uuid
    zulip.update_reaction.assert_called_once()


def test_custom_emoji_are_preserved_in_the_bridge_without_unicode_name_collisions(
    imported, db
):
    account, *_rest = imported
    snapshot(
        imported,
        [reaction(), reaction(name="thumbs_up", code="123", kind="realm_emoji")],
    )
    assert len(facts(account)) == 1
    assert (
        db.execute("SELECT count(*) FROM bridge_zulip_v5_reactions").fetchone()[0] >= 2
    )


def test_reconnect_refreshes_old_message_reactions(imported):
    account, source, _state, _subscription, zulip, _constructor = imported
    zulip.get_messages.return_value = [
        {"id": source.message_id, "flags": [], "reactions": [reaction()]}
    ]
    task = models.Task(
        project_id=account.project_id,
        external_account_uuid=account.uuid,
        content=models.RefreshMessageFlagsContent(last_message_id=source.message_id),
    )
    task.insert()
    with contexts.Context().session_manager():
        task.execute()
    reconcile()
    assert task.status == "done"
    assert len(facts(account)) == 1 and pending(account) == []


def test_native_delivery_imports_reactions_before_skipping_the_source_message(outgoing):
    api, account, _channel, remote, zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        canonical = v2_models.WorkspaceMessage.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        delivery = models.MessageDelivery.route(
            account.project_id, placement, canonical
        )
        delivery.zulip_message_id = remote["id"]
        delivery.update()
        task = models.Task(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            content=models.ImportHistoryContent(
                first_message_id=remote["id"], last_message_id=remote["id"]
            ),
        )
        task.insert()
    zulip.get_messages.side_effect = None
    zulip.get_messages.return_value = [
        {**remote, "flags": [], "reactions": [reaction()]}
    ]
    with contexts.Context().session_manager():
        task.execute()
    reconcile()
    assert task.status == "done"
    assert facts(account)[0].canonical_message_uuid == canonical.uuid
    assert (
        models.ZulipMessage.objects.get_all(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        == []
    )
    messenger_tests._drain()
    assert api.get(f"{MESSAGES}{public['uuid']}").json()["reactions"] == {
        "thumbs_up": 1
    }


def test_unknown_message_reactions_wait_for_import(message_sources, db):
    account, source = message_sources()
    with contexts.Context().session_manager():
        reactions.record_event(
            account,
            {"message_id": source.message_id, "op": "add", **reaction()},
            datetime.datetime.now(datetime.timezone.utc),
            0,
            "queue",
        )
    reconcile()
    assert facts(account) == []
    source_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    db.execute(
        "UPDATE bridge_zulip_v5_reaction_messages SET next_retry_at=NULL WHERE project_id=%s",
        (account.project_id,),
    )
    reconcile()
    assert len(facts(account)) == 1


def test_credential_rotation_cancels_old_tasks_and_requeues_the_intent(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    add(api, imported)
    reconcile()
    (old_task,) = pending(account)
    account.settings = {**account.settings, "api_key": "new-key"}
    account.update()
    reconcile()
    tasks = pending(account)
    assert len(tasks) == 2
    with contexts.Context().session_manager():
        old_task.execute()
    assert old_task.status == "cancel"
    zulip.update_reaction.assert_not_called()


def test_reaction_migration_round_trip(_database):
    with conftest.isolated_migration_database(
        "0234-Synchronize-Zulip-read-and-starred-flags-bidirectionally-94b41f.py"
    ) as url:
        path = next(conftest.MIGRATIONS_DIR.glob("0235-*.py"))
        spec = importlib.util.spec_from_file_location("reaction_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with psycopg.connect(url, autocommit=True) as db:
            worker_ready = threading.Event()
            release_worker = threading.Event()
            migration_ready = threading.Event()
            migration_pid = []

            def history_transaction():
                with psycopg.connect(
                    url, options="-c statement_timeout=5000"
                ) as worker:
                    worker.execute(
                        "LOCK TABLE bridge_zulip_v5_tasks IN ROW EXCLUSIVE MODE"
                    )
                    worker_ready.set()
                    assert release_worker.wait(5)
                    worker.execute(
                        "UPDATE messenger_messages SET updated_at=updated_at WHERE false"
                    )

            def upgrade():
                with psycopg.connect(
                    url, options="-c statement_timeout=10000"
                ) as connection:
                    migration_pid.append(
                        connection.execute("SELECT pg_backend_pid()").fetchone()[0]
                    )
                    migration_ready.set()
                    migration.migration_step.upgrade(connection)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                worker = executor.submit(history_transaction)
                assert worker_ready.wait(5)
                installing = executor.submit(upgrade)
                try:
                    assert migration_ready.wait(5)
                    for _index in range(100):
                        waiting = db.execute(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND relation='bridge_zulip_v5_tasks'::regclass AND mode='AccessExclusiveLock' AND NOT granted)",
                            (migration_pid[0],),
                        ).fetchone()[0]
                        if waiting:
                            break
                        time.sleep(0.01)
                    assert waiting
                finally:
                    release_worker.set()
                worker.result(timeout=5)
                installing.result(timeout=10)
            with db.transaction():
                migration.migration_step.downgrade(db)
            assert db.execute(
                "SELECT to_regclass('bridge_zulip_v5_reactions')"
            ).fetchone() == (None,)
            with db.transaction():
                migration.migration_step.upgrade(db)


def test_a_snapshot_of_one_remote_copy_does_not_erase_another_copy(imported):
    account, source, *_rest = imported
    canonical = v2_models.WorkspaceMessage.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
        filters={"project_id": filters.EQ(account.project_id)}
    )
    with contexts.Context().session_manager():
        delivery = models.MessageDelivery.route(
            account.project_id, placement, canonical
        )
        delivery.zulip_message_id = source.message_id + 1
        delivery.update()
    snapshot(imported, [reaction()])
    with contexts.Context().session_manager():
        reactions.observe_snapshot(
            account,
            [{"id": source.message_id + 1, "reactions": []}],
            datetime.datetime.now(datetime.timezone.utc),
        )
    reconcile()
    assert len(facts(account)) == 1
    snapshot(imported, [])
    assert facts(account) == []


def test_actor_identity_linking_moves_the_imported_reaction(imported, db):
    account, *_rest = imported
    snapshot(imported, [reaction()])
    new_owner = uuid.uuid4()
    conftest.seed_workspace_user(db, new_owner, f"reaction-owner-{new_owner}")
    account.properties["owner_user_uuid"].set_value_force(new_owner)
    account.update()
    source_tests._prepare(account)
    reconcile()
    (fact,) = facts(account)
    assert fact.user_uuid == new_owner


def test_rebinding_to_a_different_server_never_exports_old_message_ids(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    add(api, imported)
    reconcile()
    (old_task,) = pending(account)
    account.settings = {**account.settings, "server_url": "https://other.example.com"}
    account.update()
    reconcile()
    with contexts.Context().session_manager():
        old_task.execute()
    assert old_task.status == "cancel"
    assert len(pending(account)) == 0
    zulip.update_reaction.assert_not_called()


def test_empty_history_snapshots_do_not_flood_the_reconciliation_queue(imported, db):
    account, source, *_rest = imported
    with contexts.Context().session_manager():
        reactions.observe_snapshot(
            account,
            [{"id": source.message_id, "reactions": []}],
            datetime.datetime.now(datetime.timezone.utc),
        )
    assert (
        db.execute(
            "SELECT count(*) FROM bridge_zulip_v5_reaction_changes WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()[0]
        == 0
    )


def test_migration_normalizes_existing_aliases_and_preserves_local_intentions(
    imported, db
):
    account, _source, state, *_rest = imported
    path = next(conftest.MIGRATIONS_DIR.glob("0235-*.py"))
    spec = importlib.util.spec_from_file_location("reaction_upgrade_data", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migration_step.downgrade(db)
    try:
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={
                "uuid": filters.EQ(state.placement_uuid),
                "project_id": filters.EQ(account.project_id),
            }
        )
        for name in ("+1", "thumbs_up"):
            v2_models.WorkspaceMessageReactionFact(
                project_id=account.project_id,
                canonical_message_uuid=placement.message_uuid,
                placement_uuid=placement.uuid,
                user_uuid=account.owner_user_uuid,
                emoji_name=name,
            ).insert()
    finally:
        with db.transaction():
            migration.migration_step.upgrade(db)
    (fact,) = facts(account)
    assert fact.emoji_name == "thumbs_up"
    reconcile()
    assert len(pending(account)) == 1 and pending(account)[0].content.present


def test_workspace_and_zulip_shortcode_names_can_differ(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    public = add(api, imported, name="party")
    assert public["emoji_name"] == "party"
    reconcile()
    with contexts.Context().session_manager():
        pending(account)[0].execute()
    assert zulip.update_reaction.call_args.args[1:] == (
        "tada",
        "1f389",
        "unicode_emoji",
        True,
    )
    receive(imported, data=reaction(name="tada", code="1f389"))
    (fact,) = facts(account)
    assert str(fact.uuid) == public["uuid"] and fact.emoji_name == "party"


def test_native_reaction_waits_for_the_message_delivery(outgoing, db):
    api, account, _channel, remote, zulip, _constructor = outgoing
    public = outgoing_tests.create(outgoing)
    response = api.post(
        REACTIONS, json={"message_uuid": public["uuid"], "emoji_name": "rocket"}
    )
    assert response.status_code == 201, response.text
    reconcile()
    assert pending(account) == []
    with contexts.Context().session_manager():
        placement = v2_models.WorkspaceMessagePlacement.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        canonical = v2_models.WorkspaceMessage.objects.get_one(
            filters={"project_id": filters.EQ(account.project_id)}
        )
        delivery = models.MessageDelivery.route(
            account.project_id, placement, canonical
        )
        delivery.zulip_message_id = remote["id"]
        delivery.update()
    db.execute(
        "UPDATE bridge_zulip_v5_reaction_changes SET next_retry_at=NULL WHERE project_id=%s",
        (account.project_id,),
    )
    reconcile()
    (task,) = pending(account)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "done"
    zulip.update_reaction.assert_called_once_with(
        remote["id"], "rocket", "1f680", "unicode_emoji", True
    )


def test_non_json_server_failure_remains_retryable(imported, api):
    account, _source, _state, _subscription, zulip, _constructor = imported
    add(api, imported)
    reconcile()
    (task,) = pending(account)
    response = requests.Response()
    response.status_code = 503
    response._content = b"Upstream temporarily unavailable"
    zulip.update_reaction.side_effect = requests.HTTPError(response=response)
    with contexts.Context().session_manager():
        task.execute()
    assert task.status == "retry"


def test_unknown_actor_waits_for_user_import(imported, message_sources, db):
    account, *_rest = imported
    receive(imported, data=reaction(user_id=9))
    assert facts(account) == []
    imports = models.Task.objects.get_all(
        filters={
            "project_id": filters.EQ(account.project_id),
            "content": filters.JSONFields({"kind": "import_users"}),
        }
    )
    assert len(imports) == 1
    actor, _copy = message_sources(project_id=account.project_id, sender_id=9)
    source_tests._prepare(actor)
    db.execute(
        "UPDATE bridge_zulip_v5_reaction_changes SET next_retry_at=NULL WHERE project_id=%s",
        (account.project_id,),
    )
    reconcile()
    assert facts(account)[0].user_uuid == actor.owner_user_uuid
