# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Regressions for review findings in topic-owned message mutations."""

import datetime
import concurrent.futures
import http.server
import importlib.util
import json
import threading
import urllib.parse
import time
import uuid

import pytest
import requests
from restalchemy.common import contexts
from restalchemy.dm import filters
from restalchemy.storage.sql import sessions
from oslo_config import cfg

from workspace.bridges.zulip import client
from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.common import mutation_journal_opts
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import journal_store
from workspace.messenger_api.dm import external_models
from workspace.services import history_import
from workspace.services.messenger_workers import keyed_dispatcher
from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import conftest
from workspace.tests.integration import test_mutation_journal_provider as provider_tests
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_zulip_flags as flag_tests
from workspace.tests.integration import test_zulip_reactions as reaction_tests
from workspace.tests.integration import test_zulip_message_states as source_tests


REAL_ZULIP_CLIENT = client.ZulipClient


@pytest.fixture
def provider_sink():
    calls = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path))
            body = json.dumps({"result": "success", "messages": [42]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


conversation = journal_tests.conversation
message_sources = flag_tests.message_sources
imported = flag_tests.imported
journal = provider_tests.journal


@pytest.mark.parametrize("remove_user", [False, True])
@pytest.mark.parametrize("expire_receipt", [False, True])
def test_delete_tombstone_survives_removal_of_its_project_user(
    api, db, imported, journal, record_property, remove_user, expire_receipt
):
    account, source, state, *_rest = imported
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    target = str(state.placement_uuid)
    canonical, stream_uuid = db.execute(
        "SELECT message_uuid,stream_uuid FROM messenger_message_placements WHERE project_id=%s AND uuid=%s",
        (account.project_id, state.placement_uuid),
    ).fetchone()
    deleted = api.delete("/v1/messenger/messages/" + target)
    assert deleted.status_code == 204, deleted.text
    journal_tests.drain(journal, db)
    before = db.execute(
        "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND operation='delete' AND status='applied'",
        (account.project_id,),
    ).fetchone()[0]
    assert before == 1
    if expire_receipt:
        db.execute(
            "UPDATE messenger_mutation_commands SET finished_at='2000-01-01' WHERE project_id=%s",
            (account.project_id,),
        )
        journal_tests.drain(journal, db)
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND operation='delete'",
                (account.project_id,),
            ).fetchone()[0]
            == 0
        )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_messages WHERE project_id=%s AND uuid=%s",
            (account.project_id, canonical),
        ).fetchone()[0]
        == 0
    )
    # Existing restrictive FKs block deleting a current stream owner/member.
    # Remove its stream through the real API before deleting the now-unreferenced
    # project membership. Do not disable constraints or manually delete child rows.
    response = api.delete(f"/v1/messenger/streams/{stream_uuid}")
    assert response.status_code == 204, response.text
    journal_tests.drain(journal, db)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_streams WHERE project_id=%s",
            (account.project_id,),
        ).fetchone()[0]
        == 0
    )
    if remove_user:
        removed = db.execute(
            "DELETE FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s RETURNING user_uuid",
            (account.project_id, account.owner_user_uuid),
        ).fetchall()
        assert len(removed) == 1
    tombstones = db.execute(
        "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND operation='delete' AND status='applied'",
        (account.project_id,),
    ).fetchone()[0]
    source_tests._prepare(account)
    source.content.workspace_content = "Late provider observation after user removal"
    source.content.workspace_content_updated_at += datetime.timedelta(days=1)
    source.content.update()
    source.update()
    journal_tests.drain(journal, db)
    recreated = db.execute(
        "SELECT count(*) FROM messenger_messages WHERE project_id=%s AND uuid=%s",
        (account.project_id, canonical),
    ).fetchone()[0]
    record_property("tombstones_before_user_removal", before)
    record_property("tombstones_after_user_removal", tombstones)
    record_property("canonical_messages_after_late_observation", recreated)
    assert recreated == 0


@pytest.mark.parametrize("operation", ["flag", "reaction"])
@pytest.mark.parametrize("change", ["revision", "disabled"])
@pytest.mark.parametrize("revoke_at", ["before_send", "after_accept"])
def test_account_revocation_between_prepare_and_send_blocks_provider_call(
    api,
    db,
    imported,
    journal,
    monkeypatch,
    operation,
    change,
    revoke_at,
    record_property,
    provider_sink,
):
    account, source, state, _subscription, zulip, _constructor = imported
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    if operation == "flag":
        with contexts.Context().session_manager():
            flags.record(
                account,
                [source.message_id],
                "read",
                False,
                datetime.datetime.now(datetime.timezone.utc),
            )
        journal_tests.drain(journal, db)
        response = api.post(
            f"/v1/messenger/messages/{state.placement_uuid}/actions/read/invoke"
        )
        handler = flags
        get_pending = flag_tests.pending
    else:
        response = api.post(
            "/v1/messenger/message_reactions/",
            json={"message_uuid": str(state.placement_uuid), "emoji_name": "thumbs_up"},
        )
        handler = reactions
        get_pending = reaction_tests.pending
    assert response.status_code in (200, 201), response.text
    journal_tests.drain(journal, db)
    (task,) = get_pending(account)
    owner = uuid.uuid4()
    db.execute(
        "UPDATE bridge_zulip_v5_tasks SET lease_owner=%s,lease_expires_at=NOW()+interval '1 minute' WHERE uuid=%s",
        (owner, task.uuid),
    )
    prepare = handler.prepare_outgoing
    prepared_account = []
    sink_url, calls = provider_sink
    request = requests.Session.request

    def route_to_sink(session, method, url, **kwargs):
        if url.startswith("https://zulip.example.com/api/v1/"):
            url = sink_url + urllib.parse.urlsplit(url).path
        result = request(session, method, url, **kwargs)
        if revoke_at == "after_accept":
            revoke()
        return result

    monkeypatch.setattr(client, "ZulipClient", REAL_ZULIP_CLIENT)
    monkeypatch.setattr(requests.Session, "request", route_to_sink)

    def prepare_then_revoke(current):
        result = prepare(current)
        assert result is not None
        prepared_account.append(result[0])
        return result

    revoked = False

    def revoke():
        nonlocal revoked
        # The preparation transaction has ended: revoke in another committed transaction.
        with pytest.raises(sessions.SessionNotFound):
            contexts.Context().get_session()
        current = external_models.ExternalAccount.objects.get_one(
            filters={"uuid": filters.EQ(account.uuid)}
        )
        if change == "disabled":
            current.disabled = True
        else:
            current.settings = {**current.settings, "email": "changed@example.test"}
        current.update()
        revoked = True

    def before_send(prepared):
        if revoke_at == "before_send":
            revoke()
        return send(prepared)

    send = handler.send_prepared
    monkeypatch.setattr(handler, "prepare_outgoing", prepare_then_revoke)
    monkeypatch.setattr(handler, "send_prepared", before_send)
    history_import.HistoryImportService(queue="realtime")._run_task(task.uuid, owner)
    current_task = models.Task.objects.get_one(filters={"uuid": filters.EQ(task.uuid)})
    record_property("account_revoked_before_send", revoked)
    record_property("provider_calls_after_revocation", len(calls))
    record_property("task_status_after_send", current_task.status)
    assert revoked and prepared_account
    current = external_models.ExternalAccount.objects.get_one(
        filters={"uuid": filters.EQ(account.uuid)}
    )
    if change == "disabled":
        assert not current.can_stream()
    else:
        assert current.realtime_revision() != prepared_account[0].realtime_revision()
    assert len(calls) == (0 if revoke_at == "before_send" else 1)
    assert current_task.status == "cancel"


def test_read_up_to_leaves_hidden_personal_states_unchanged(
    api, db, conversation, record_property
):
    stream, messages, peer = conversation
    hidden, boundary = messages
    changed = db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s RETURNING uuid",
        (api.project_id, hidden["uuid"], peer),
    ).fetchall()
    assert len(changed) == 1
    assert api.get(f"{native.MESSAGES}{hidden['uuid']}", user=peer).status_code == 404
    assert api.get(f"{native.MESSAGES}{boundary['uuid']}", user=peer).status_code == 200
    response = api.post(
        f"{native.MESSAGES}{boundary['uuid']}/actions/read_up_to/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    state = db.execute(
        "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
        (api.project_id, hidden["uuid"], peer),
    ).fetchone()[0]
    record_property("hidden_state_marked_read", state)
    assert state is False


def test_terminal_non_delete_receipts_are_not_retained_forever(
    api, db, conversation, record_property
):
    _stream, messages, peer = conversation
    runtime = mutation_runtime.MutationRuntime()
    counts = []
    try:
        for _ in range(3):
            for _ in range(100):
                response = api.post(
                    f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke",
                    user=peer,
                )
                assert response.status_code == 200, response.text
            journal_tests.drain(runtime, db)
            counts.append(
                db.execute(
                    "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND status='applied' AND operation='read'",
                    (api.project_id,),
                ).fetchone()[0]
            )
        db.execute(
            "UPDATE messenger_mutation_commands SET created_at='2000-01-01',finished_at='2000-01-01' WHERE project_id=%s",
            (api.project_id,),
        )
    finally:
        runtime.close()
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        for _ in range(100):
            runtime.tick()
            time.sleep(0.002)
    finally:
        runtime.close()
    retained = db.execute(
        "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND operation='read' AND finished_at<'2001-01-01'",
        (api.project_id,),
    ).fetchone()[0]
    record_property("terminal_row_counts", str(counts))
    record_property("old_receipts_after_restart", retained)
    assert retained < counts[-1], (
        "No terminal receipt was collected, including receipts dated over 25 years ago"
    )


def test_hidden_provider_state_is_not_read_or_enqueued_for_flag_sync(
    api, db, imported, journal, record_property
):
    account, source, first_state, *_rest = imported
    second_source_uuid = uuid.uuid5(source.message_uuid, "review-visible-boundary")
    with contexts.Context().session_manager():
        models.ZulipMessage(
            project_id=account.project_id,
            external_account_uuid=account.uuid,
            message_uuid=second_source_uuid,
            message_id=source.message_id + 1,
            sender_id=source.sender_id,
            timestamp=source.timestamp + 1,
            channel=source.channel,
            topic=source.topic,
            content=source.content,
            read=False,
            starred=False,
            mentioned=False,
        ).insert()
        flags.record(
            account,
            [source.message_id, source.message_id + 1],
            "read",
            False,
            datetime.datetime.now(datetime.timezone.utc),
        )
    journal_tests.drain(journal, db)
    boundary = db.execute(
        "SELECT placement.uuid,placement.topic_uuid FROM messenger_message_placements AS placement "
        "WHERE project_id=%s AND message_uuid=messenger_uuid_v5(%s,%s)",
        (account.project_id, account.project_id, str(second_source_uuid)),
    ).fetchone()
    assert boundary is not None
    assert flag_tests.reload(first_state).read_at is None
    prior_intents = {
        task.uuid: task.content.changed_at
        for task in flag_tests.pending(account)
        if task.content.flag == "read" and task.content.state_uuid == first_state.uuid
    }
    db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' WHERE project_id=%s AND placement_uuid=%s AND user_uuid=%s",
        (account.project_id, first_state.placement_uuid, account.owner_user_uuid),
    )
    api.project_id, api.user_uuid = (
        str(account.project_id),
        str(account.owner_user_uuid),
    )
    assert (
        api.get(f"/v1/messenger/messages/{first_state.placement_uuid}").status_code
        == 404
    )
    response = api.post(
        f"/v1/messenger/messages/{boundary[0]}/actions/read_up_to/invoke"
    )
    assert response.status_code == 200, response.text
    command = db.execute(
        "SELECT uuid,scope_key,payload FROM messenger_mutation_commands WHERE request_uuid=%s",
        (response.headers["X-Workspace-Mutation-Id"],),
    ).fetchone()
    journal._execute(
        keyed_dispatcher.Work(
            ("command", command[1]),
            {command[0]},
            frozenset(
                journal._topic_reservations(account.project_id, command[2]["topics"])
            ),
        )
    )
    changed = flag_tests.reload(first_state).read_at is not None
    hidden_journal = db.execute(
        "SELECT count(*) FROM bridge_zulip_v5_flag_changes WHERE project_id=%s AND state_uuid=%s",
        (account.project_id, first_state.uuid),
    ).fetchone()[0]
    hidden_visibility_before_consume = db.execute(
        "SELECT visibility FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (account.project_id, account.owner_user_uuid, first_state.placement_uuid),
    ).fetchone()[0]
    with contexts.Context().session_manager():
        hidden_targets_before_consume = flags.flag_states(
            account.project_id, first_state.uuid, account.uuid, "read"
        )
    journal_tests.drain(journal, db)
    tasks = flag_tests.pending(account)
    hidden_outbound = [
        str(task.uuid)
        for task in tasks
        if task.content.state_uuid == first_state.uuid
        and task.content.flag == "read"
        and prior_intents.get(task.uuid) != task.content.changed_at
    ]
    record_property("hidden_state_marked_read", changed)
    record_property("hidden_visibility_after_command", hidden_visibility_before_consume)
    record_property(
        "provider_targets_while_still_hidden", len(hidden_targets_before_consume)
    )
    record_property("hidden_flag_journal_rows_after_command", hidden_journal)
    record_property("new_hidden_outbound_read_tasks", len(hidden_outbound))
    record_property(
        "hidden_binding_after_drain",
        db.execute(
            "SELECT visibility FROM messenger_user_message_bindings WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
            (account.project_id, account.owner_user_uuid, first_state.placement_uuid),
        ).fetchone()[0],
    )
    assert changed is False and hidden_journal == 0 and hidden_outbound == []


def test_deferred_message_publication_hides_revoked_membership(
    api, workspace_api, db, conversation, record_property
):
    stream, messages, peer = conversation
    target = messages[-1]["uuid"]
    response = api.put(
        f"{native.MESSAGES}{target}",
        json={"payload": {"kind": "markdown", "content": "Membership-fenced edit"}},
    )
    assert response.status_code == 200, response.text
    command = db.execute(
        "SELECT uuid,scope_key,payload FROM messenger_mutation_commands WHERE request_uuid=%s",
        (response.headers["X-Workspace-Mutation-Id"],),
    ).fetchone()
    runtime = mutation_runtime.MutationRuntime()
    try:
        runtime._execute(
            keyed_dispatcher.Work(
                ("command", command[1]),
                {command[0]},
                frozenset(
                    runtime._topic_reservations(
                        uuid.UUID(api.project_id), command[2]["topics"]
                    )
                ),
            )
        )
        pending = db.execute(
            "SELECT count(*) FROM messenger_publication_outbox WHERE project_id=%s AND kind='message.updated' AND %s=ANY(recipients)",
            (api.project_id, peer),
        ).fetchone()[0]
        assert pending > 0
        binding = db.execute(
            "SELECT uuid FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (api.project_id, stream["uuid"], peer),
        ).fetchone()[0]
        removed = api.delete(f"{native.STREAM_BINDINGS}{binding}")
        assert removed.status_code == 204, removed.text
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    owner_events = workspace_api.get(
        native.EVENTS,
        project=api.project_id,
        user=api.user_uuid,
        params={"page_limit": 100},
    )
    peer_events = workspace_api.get(
        native.EVENTS, project=api.project_id, user=peer, params={"page_limit": 100}
    )
    assert owner_events.status_code == peer_events.status_code == 200

    def edited(events):
        return [
            row
            for row in events.json()
            if row["payload"].get("uuid") == target
            and row["payload"].get("kind") == "message.updated"
        ]

    owner_count, peer_count = len(edited(owner_events)), len(edited(peer_events))
    record_property("deferred_events_before_revocation", pending)
    record_property("owner_visible_edit_events", owner_count)
    record_property("revoked_peer_visible_edit_events", peer_count)
    assert owner_count > 0 and peer_count == 0


def test_private_dispatcher_lock_directory_current_symlink_behavior(
    tmp_path, record_property
):
    directory = tmp_path / "private-run"
    directory.mkdir(mode=0o700)
    victim = tmp_path / "existing-file"
    victim.write_text("Synthetic fixture")
    (directory / "mutation-dispatcher.lock").symlink_to(victim)
    with pytest.raises(OSError):
        mutation_runtime.MutationRuntime(singleton_directory=directory)
    record_property("test_directory_mode", oct(directory.stat().st_mode & 0o777))
    assert victim.read_text() == "Synthetic fixture"


def test_pending_admission_is_bounded_and_recovers_after_drain(api, db, conversation):
    _stream, messages, peer = conversation
    cfg.CONF.set_override("max_pending_per_user", 2, group=mutation_journal_opts.DOMAIN)
    runtime = mutation_runtime.MutationRuntime()
    try:
        path = f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke"
        accepted = [api.post(path, user=peer) for _ in range(2)]
        assert all(response.status_code == 200 for response in accepted)
        rejected = api.post(path, user=peer)
        assert rejected.status_code == 429, rejected.text
        assert "X-Workspace-Mutation-Id" not in rejected.headers
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s AND status='pending'",
                (api.project_id, peer),
            ).fetchone()[0]
            == 2
        )
        # Another project member has its own capacity.
        assert api.post(path).status_code == 200
        journal_tests.drain(runtime, db)
        assert api.post(path, user=peer).status_code == 200
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )


def test_concurrent_admission_cannot_overfill_a_users_pending_slots(
    api, db, conversation
):
    _stream, messages, peer = conversation
    cfg.CONF.set_override("max_pending_per_user", 4, group=mutation_journal_opts.DOMAIN)
    start = threading.Barrier(8)

    def admit():
        start.wait(timeout=10)
        try:
            with contexts.Context().session_manager():
                store = journal_store.JournalStore(api.project_id, peer)
                store.perform_action(
                    "messages", uuid.UUID(messages[-1]["uuid"]), "read", {}
                )
            return True
        except messenger_exceptions.MutationQueueFullError:
            return False

    try:
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            accepted = sum(pool.map(lambda _: admit(), range(8)))
        assert 1 <= accepted <= 4
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s AND status='pending'",
                (api.project_id, peer),
            ).fetchone()[0]
            == accepted
        )
    finally:
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )


def test_receipt_gc_keeps_entire_unfinished_requests(api, db, conversation):
    _stream, messages, peer = conversation
    for _ in range(2):
        assert (
            api.post(
                f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read/invoke",
                user=peer,
            ).status_code
            == 200
        )
    rows = db.execute(
        "SELECT uuid,request_uuid FROM messenger_mutation_commands WHERE project_id=%s ORDER BY sequence",
        (api.project_id,),
    ).fetchall()
    db.execute(
        "UPDATE messenger_mutation_commands SET status='applied',finished_at='2000-01-01' WHERE uuid=%s",
        (rows[0][0],),
    )
    db.execute(
        "UPDATE messenger_mutation_commands SET request_uuid=%s,created_at='2000-01-01' WHERE uuid=%s",
        (rows[0][1], rows[1][0]),
    )
    runtime = mutation_runtime.MutationRuntime()
    try:
        runtime._execute(keyed_dispatcher.Work(("receipts",), set(), frozenset()))
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE request_uuid=%s",
                (rows[0][1],),
            ).fetchone()[0]
            == 2
        )
        receipt = api.get(f"/v1/messenger/message_mutations/{rows[0][1]}", user=peer)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["status"] == "pending"
        assert receipt.json()["command_count"] == 2
    finally:
        runtime.close()


def test_tombstone_migration_backfills_and_guards_unsafe_downgrade():
    path = (
        conftest.MIGRATIONS_DIR
        / "0258-Retain-message-delete-tombstones-and-bound-mutation-receipts-92730c.py"
    )
    spec = importlib.util.spec_from_file_location("journal_retention_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with conftest.isolated_migration_database(
        "0257-Order-batched-flag-journal-writes-and-suppress-consumer-echoes-c445d0.py"
    ) as database_url:
        with contexts.Context().session_manager() as session:
            module.migration_step.upgrade(session)
            module.migration_step.downgrade(session)
        import psycopg

        with psycopg.connect(database_url, autocommit=True) as connection:
            project, user, canonical = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            conftest.seed_workspace_user(connection, user, f"user-{user}")
            native._register_project_user(connection, project, user)
            connection.execute(
                """INSERT INTO messenger_mutation_commands
                   (uuid,request_uuid,project_id,user_uuid,scope_key,operation,resource_uuid,payload,status,finished_at)
                   VALUES (%s,%s,%s,%s,%s,'delete',%s,%s::jsonb,'applied',NOW())""",
                (
                    uuid.uuid4(),
                    uuid.uuid4(),
                    project,
                    user,
                    f"{project}:message:{canonical}",
                    canonical,
                    json.dumps(
                        {
                            "topics": [
                                {
                                    "canonical_message_uuid": str(canonical),
                                    "ingest_sequence": 123,
                                }
                            ]
                        }
                    ),
                ),
            )
            with contexts.Context().session_manager() as session:
                module.migration_step.upgrade(session)
            connection.execute(
                "DELETE FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
                (project, user),
            )
            assert connection.execute(
                "SELECT ingest_sequence FROM messenger_message_delete_tombstones WHERE project_id=%s AND canonical_message_uuid=%s",
                (project, canonical),
            ).fetchone() == (123,)
            assert (
                connection.execute(
                    "SELECT count(*) FROM messenger_mutation_commands"
                ).fetchone()[0]
                == 0
            )
            with pytest.raises(RuntimeError, match="consistent pre-upgrade snapshot"):
                with contexts.Context().session_manager() as session:
                    module.migration_step.downgrade(session)


@pytest.mark.parametrize("resource", ["streams", "stream_topics"])
def test_scope_read_uses_a_visible_boundary(api, db, conversation, resource):
    stream, messages, peer = conversation
    db.execute(
        "UPDATE messenger_user_message_bindings SET visibility='hidden' WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
        (api.project_id, peer, messages[-1]["uuid"]),
    )
    resource_uuid = (
        stream["uuid"] if resource == "streams" else stream["default_topic_uuid"]
    )
    response = api.post(
        f"/v1/messenger/{resource}/{resource_uuid}/actions/read/invoke", user=peer
    )
    assert response.status_code == 200, response.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
    finally:
        runtime.close()
    receipt = api.get(
        f"/v1/messenger/message_mutations/{response.headers['X-Workspace-Mutation-Id']}",
        user=peer,
    )
    assert receipt.json()["status"] == "applied", receipt.text
    for message, visible in zip(messages, [True, False]):
        assert (
            db.execute(
                "SELECT read_at IS NOT NULL FROM messenger_user_message_states WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (api.project_id, peer, message["uuid"]),
            ).fetchone()[0]
            is visible
        )


def test_multi_topic_admission_rolls_back_when_queue_capacity_is_exhausted(
    api, db, conversation
):
    stream, _messages, peer = conversation
    topic = api.post(
        native.STREAM_TOPICS,
        json={"stream_uuid": stream["uuid"], "name": "Second topic"},
    )
    assert topic.status_code == 201, topic.text
    message = api.post(
        native.MESSAGES,
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic.json()["uuid"],
            "payload": {"kind": "markdown", "content": "Second topic message"},
        },
    )
    assert message.status_code == 201, message.text
    runtime = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(runtime, db)
        cfg.CONF.set_override(
            "max_pending_per_user", 1, group=mutation_journal_opts.DOMAIN
        )
        response = api.post(
            f"{native.STREAMS}{stream['uuid']}/actions/read/invoke", user=peer
        )
        assert response.status_code == 429, response.text
        assert "X-Workspace-Mutation-Id" not in response.headers
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_mutation_commands WHERE project_id=%s AND user_uuid=%s",
                (api.project_id, peer),
            ).fetchone()[0]
            == 0
        )
    finally:
        cfg.CONF.clear_override(
            "max_pending_per_user", group=mutation_journal_opts.DOMAIN
        )
        runtime.close()
