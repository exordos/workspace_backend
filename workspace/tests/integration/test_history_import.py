#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""History ingress and short final writes against the complete PostgreSQL schema."""

import contextlib
import datetime
import importlib
import json
import threading
import time
import types
import urllib.parse
import uuid as sys_uuid

import pytest
from botocore import exceptions as botocore_exceptions
from psycopg import errors as pg_errors
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import migrations as ra_migrations

from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_event_apply
from workspace.external_bridge_control import provider_service
from workspace.external_bridge_control import service as bridge_service
from workspace.external_bridge_control import sql_state
from workspace.history_import import agents
from workspace.history_import import contract
from workspace.history_import import http_service
from workspace.history_import import repository
from workspace.history_import import writer
from workspace.history_import import zulip_markdown
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import read_state
from workspace.tests.integration import conftest


HISTORY_CASCADE_MIGRATION = (
    "0180-Cascade-bridge-history-scopes-with-online-cleanup-indexes-f3bf9f.py"
)


def _completed_history_with_file(s):
    job_uuid = accept(
        s, envelope(s, count=1, content="[sample](/user_uploads/1/a/sample.txt)")
    )
    assert finish(s, job_uuid)["status"] == "waiting_files"
    request = _history_status(s, job_uuid)["files"][0]
    _upload_history_file(s, job_uuid, request["uuid"])
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        session.execute(
            """INSERT INTO m_external_history_notifications_v1
               (job_uuid, uuid, user_uuid, stream_uuid, topic_uuid)
               VALUES (%s,%s,%s,%s,%s)""",
            (job_uuid, sys_uuid.uuid4(), s.owner, s.stream, s.topic),
        )
        session.execute(
            """INSERT INTO m_external_history_tombstones_v1
               (provider_realm_uuid, provider_message_id) VALUES (%s,'deleted')""",
            (s.realm,),
        )
    return job_uuid


def _history_canonical_snapshot(s):
    with s.session_factory() as session:
        return {
            "messages": session.execute(
                "SELECT * FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchall(),
            "files": session.execute(
                """SELECT * FROM m_workspace_files WHERE uuid IN (
                       SELECT file_uuid FROM m_workspace_file_accesses
                       WHERE project_id=%s)""",
                (s.project,),
            ).fetchall(),
            "accesses": session.execute(
                "SELECT * FROM m_workspace_file_accesses WHERE project_id=%s",
                (s.project,),
            ).fetchall(),
            "tombstones": session.execute(
                "SELECT * FROM m_external_history_tombstones_v1 WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchall(),
            "objects": dict(s.objects),
            "metadata": dict(s.metadata),
        }


def _delete_history_bridge_root(s):
    with s.session_factory() as session:
        bridge = external_models.ExternalBridgeInstance.objects.get_one(
            filters={"uuid": dm_filters.EQ(s.bridge)}, session=session
        )
        bridge.delete(session=session)


def _history_operational_counts(s, job_uuid):
    with s.session_factory() as session:
        return [
            session.execute(
                f"SELECT count(*) AS n FROM {table} WHERE {key}=%s",
                (value,),
            ).fetchone()["n"]
            for table, key, value in (
                ("m_external_history_scopes_v1", "bridge_uuid", s.bridge),
                ("m_external_history_imports_v1", "uuid", job_uuid),
                ("m_external_history_files_v1", "job_uuid", job_uuid),
                ("m_external_history_message_hashes_v1", "job_uuid", job_uuid),
                ("m_external_history_notifications_v1", "job_uuid", job_uuid),
            )
        ]


def test_bridge_root_cascades_only_operational_history(history_setup):
    s = history_setup
    job_uuid = _completed_history_with_file(s)
    before = _history_canonical_snapshot(s)
    assert all(before.values())
    assert _history_operational_counts(s, job_uuid) == [1, 1, 1, 1, 1]
    _delete_history_bridge_root(s)
    assert _history_operational_counts(s, job_uuid) == [0, 0, 0, 0, 0]
    assert _history_canonical_snapshot(s) == before


@pytest.mark.parametrize("orphan_timing", ["before_upgrade", "during_index_build"])
def test_history_cascade_upgrade_rejects_old_orphans_without_deleting_data(
    history_setup, monkeypatch, orphan_timing
):
    s = history_setup
    job_uuid = _completed_history_with_file(s)
    before = _history_canonical_snapshot(s)
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(HISTORY_CASCADE_MIGRATION)
    try:
        with monkeypatch.context() as patch:
            if orphan_timing == "before_upgrade":
                _delete_history_bridge_root(s)
            else:
                module = importlib.import_module(HISTORY_CASCADE_MIGRATION[:-3])
                ensure_indexes = module._ensure_indexes

                def delete_during_index_build(session):
                    ensure_indexes(session)
                    assert (
                        s.db.execute(
                            "DELETE FROM m_external_bridge_instances_v2 WHERE uuid=%s",
                            (s.bridge,),
                        ).rowcount
                        == 1
                    )

                patch.setattr(module, "_ensure_indexes", delete_during_index_build)
            with pytest.raises(
                RuntimeError, match="History scopes reference missing bridges"
            ):
                engine.apply_migration(HISTORY_CASCADE_MIGRATION)
        assert _history_operational_counts(s, job_uuid) == [1, 1, 1, 1, 1]
        assert _history_canonical_snapshot(s) == before
        with s.session_factory() as session:
            assert (
                session.execute(
                    "SELECT 1 FROM m_external_bridge_instances_v2 WHERE uuid=%s",
                    (s.bridge,),
                ).fetchone()
                is None
            )
            assert (
                session.execute(
                    "SELECT applied FROM ra_migrations WHERE uuid=%s",
                    ("f3bf9f2b-84db-4153-964d-437ab09f9e0d",),
                ).fetchone()["applied"]
                is False
            )
            assert (
                session.execute(
                    "SELECT 1 FROM pg_constraint WHERE conname='m_external_history_scopes_bridge_fkey'"
                ).fetchone()
                is None
            )
    finally:
        # Explicit recovery of this tiny synthetic orphan only. Real deployment
        # cleanup must page large descendants before deleting their empty roots.
        with s.session_factory() as session:
            session.execute(
                "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s", (job_uuid,)
            )
            session.execute(
                "DELETE FROM m_external_history_scopes_v1 WHERE bridge_uuid=%s",
                (s.bridge,),
            )
        engine.apply_migration(HISTORY_CASCADE_MIGRATION)
    assert _history_canonical_snapshot(s) == before
    assert _history_operational_counts(s, job_uuid) == [0, 0, 0, 0, 0]


def test_history_cascade_upgrade_retries_final_lock_contention(history_setup):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(HISTORY_CASCADE_MIGRATION)
    index_query = """SELECT indexrelid, indisvalid FROM pg_index WHERE indexrelid IN (
        'm_external_history_imports_scope_cleanup_idx'::regclass,
        'm_external_history_hashes_job_cleanup_idx'::regclass,
        'm_external_history_hashes_message_cleanup_idx'::regclass) ORDER BY indexrelid"""
    indexes = s.db.execute(index_query).fetchall()
    try:
        with s.db.transaction():
            s.db.execute(
                "LOCK TABLE m_external_history_scopes_v1 IN ROW EXCLUSIVE MODE"
            )
            with pytest.raises(pg_errors.LockNotAvailable):
                engine.apply_migration(HISTORY_CASCADE_MIGRATION)
            assert s.db.execute(
                "SELECT applied FROM ra_migrations WHERE uuid=%s",
                ("f3bf9f2b-84db-4153-964d-437ab09f9e0d",),
            ).fetchone() == (False,)
    finally:
        engine.apply_migration(HISTORY_CASCADE_MIGRATION)
    assert s.db.execute(index_query).fetchall() == indexes
    assert s.db.execute(
        """SELECT convalidated, confdeltype FROM pg_constraint
           WHERE conname='m_external_history_scopes_bridge_fkey'"""
    ).fetchone() == (True, "c")
    assert finish(s, job_uuid)["status"] == "complete"


def test_history_online_index_waits_for_snapshot_without_blocking_foreground(
    history_setup,
):
    s = history_setup
    accept(s, envelope(s, count=1))
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(HISTORY_CASCADE_MIGRATION)
    s.db.execute("DROP INDEX CONCURRENTLY m_external_history_imports_scope_cleanup_idx")
    failures = []
    completed = threading.Event()

    def migrate():
        try:
            engine.apply_migration(HISTORY_CASCADE_MIGRATION)
        except Exception as error:
            failures.append(error)
        finally:
            completed.set()

    migration = threading.Thread(target=migrate)
    try:
        with s.db.transaction():
            s.db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            assert s.db.execute(
                "SELECT count(*) FROM m_external_history_imports_v1 WHERE bridge_uuid=%s",
                (s.bridge,),
            ).fetchone() == (1,)
            started = time.monotonic()
            migration.start()
            phase = None
            deadline = started + 5
            while time.monotonic() < deadline and not completed.is_set():
                with s.session_factory() as session:
                    progress = session.execute(
                        """SELECT phase FROM pg_stat_progress_create_index
                           WHERE relid='m_external_history_imports_v1'::regclass
                             AND command='CREATE INDEX CONCURRENTLY'"""
                    ).fetchone()
                phase = progress["phase"] if progress else None
                if phase == "waiting for old snapshots":
                    break
                time.sleep(0.02)
            assert phase == "waiting for old snapshots", failures
            # This real POST inserts into the table whose online index is
            # waiting. It and a foreground read finish before the old snapshot
            # is released, despite the build exceeding the final DDL timeout.
            later_job = accept(s, envelope(s, count=1, generation=2))
            with s.session_factory() as session:
                assert (
                    session.execute(
                        "SELECT count(*) AS n FROM m_external_history_imports_v1 WHERE bridge_uuid=%s",
                        (s.bridge,),
                    ).fetchone()["n"]
                    == 2
                )
            time.sleep(max(0, 2.0 - (time.monotonic() - started)))
            assert not completed.is_set()
            assert s.db.execute(
                "SELECT count(*) FROM m_external_history_imports_v1 WHERE bridge_uuid=%s",
                (s.bridge,),
            ).fetchone() == (1,)
    finally:
        migration.join(timeout=10)
        assert not migration.is_alive(), (
            "Migration did not finish after releasing its old snapshot"
        )
        if failures:
            engine.apply_migration(HISTORY_CASCADE_MIGRATION)
    assert not failures
    assert completed.is_set()
    assert s.db.execute(
        """SELECT indisvalid, indisready FROM pg_index WHERE indexrelid=
           'm_external_history_imports_scope_cleanup_idx'::regclass"""
    ).fetchone() == (True, True)
    assert finish(s, later_job)["status"] == "complete"


def test_history_cascade_upgrade_repairs_interrupted_online_index(history_setup):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    engine = ra_migrations.MigrationEngine(migrations_path=str(conftest.MIGRATIONS_DIR))
    engine.rollback_migration(HISTORY_CASCADE_MIGRATION)
    try:
        # PostgreSQL leaves an invalid index after an interrupted/failed
        # concurrent build; exercise that real catalog state without editing it.
        s.db.execute(
            "DROP INDEX CONCURRENTLY m_external_history_imports_scope_cleanup_idx"
        )
        with pytest.raises(pg_errors.DivisionByZero):
            s.db.execute(
                """CREATE INDEX CONCURRENTLY m_external_history_imports_scope_cleanup_idx
                   ON m_external_history_imports_v1 ((1 / (from_id - 1)))"""
            )
        assert s.db.execute(
            """SELECT indisvalid FROM pg_index WHERE indexrelid=
               'm_external_history_imports_scope_cleanup_idx'::regclass"""
        ).fetchone() == (False,)
    finally:
        engine.apply_migration(HISTORY_CASCADE_MIGRATION)
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        for name, columns in (
            (
                "m_external_history_imports_scope_cleanup_idx",
                "bridge_uuid, project_uuid, provider_realm_uuid",
            ),
            ("m_external_history_hashes_job_cleanup_idx", "job_uuid"),
            ("m_external_history_hashes_message_cleanup_idx", "message_uuid"),
        ):
            index = session.execute(
                """SELECT indisvalid, indisready, indpred IS NULL AS full_index,
                          pg_get_indexdef(indexrelid) AS definition
                   FROM pg_index WHERE indexrelid=to_regclass(%s)""",
                (name,),
            ).fetchone()
            assert index["indisvalid"] and index["indisready"] and index["full_index"]
            assert f"({columns})" in index["definition"]


@pytest.fixture
def history_setup(_database, db, api, monkeypatch):
    project = sys_uuid.UUID(api.project_id)
    owner = sys_uuid.UUID(api.user_uuid)
    bridge, realm, account, chat = [sys_uuid.uuid4() for _ in range(4)]
    stream = sys_uuid.UUID(
        conftest.seed_user_stream(db, project, owner, "cassi history")
    )
    topic = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project, stream, owner, "Import", is_default=True
        )
    )
    identity = pki.BridgeIdentity(realm, "zulip", bridge, 1, "cassi-test")
    context = contexts.Context()
    with context.session_manager() as session:
        session.execute(
            """INSERT INTO m_external_provider_policies_v1 (uuid, provider, enabled, limits)
               VALUES (%s, 'zulip', true, '{"max_file_bytes":52428800}') ON CONFLICT (provider)
               DO UPDATE SET enabled = true, emergency_suspended = false, limits=EXCLUDED.limits""",
            (sys_uuid.uuid4(),),
        )
        sql_state.ensure_bridge_instance(session, bridge, "zulip", 1)
        sql_state.persist_encryption_target(
            session,
            identity,
            {"key_uuid": str(sys_uuid.uuid4()), "public_key": "X25519-public-key"},
        )
        session.execute(
            "UPDATE m_external_bridge_instances_v2 SET status = 'active' WHERE uuid = %s",
            (bridge,),
        )
        session.execute(
            """INSERT INTO m_external_accounts_v2
            (uuid, owner_user_uuid, provider, settings, provider_realm_uuid, provider_owner_user_id)
            VALUES (%s, %s, 'zulip', '{"server_url":"https://zulip.example.test"}'::jsonb, %s, '7')""",
            (account, owner, realm),
        )
        session.execute(
            """INSERT INTO m_external_chats_v2
            (uuid, external_account_uuid, owner_user_uuid, provider, provider_chat_id, source,
             display_name, selected, project_id, history_depth, projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', 'channel:42', %s::jsonb, 'Import', true, %s, 'all', %s, 'live')""",
            (
                chat,
                account,
                owner,
                json.dumps(
                    {
                        "kind": "zulip",
                        "topics": [
                            {
                                "topic_uuid": str(topic),
                                "provider_topic_id": "42:Import",
                                "name": "Import",
                            }
                        ],
                    }
                ),
                project,
                stream,
            ),
        )
        session.execute(
            """INSERT INTO m_external_provider_identity_links_v1
            (provider, provider_realm_uuid, provider_user_id, workspace_user_uuid, link_kind)
            VALUES ('zulip', %s, '7', %s, 'verified_account_owner')""",
            (realm, owner),
        )
        sql_state.append_upsert(
            session,
            bridge,
            "zulip",
            {
                "resource_type": "external_account",
                "uuid": str(account),
                "generation": 1,
                "synchronization_enabled": True,
            },
        )
        sql_state.append_upsert(
            session,
            bridge,
            "zulip",
            {
                "resource_type": "external_chat_assignment",
                "uuid": str(chat),
                "generation": 1,
                "selected": True,
                "history_depth": "all",
                "provider_chat": {"provider_chat_key": "channel:42"},
            },
        )
    observer = threading.local()
    observer.depth = 0

    @contextlib.contextmanager
    def session_factory():
        observer.depth = getattr(observer, "depth", 0) + 1
        try:
            with context.session_manager() as session:
                yield session
        finally:
            observer.depth -= 1

    objects = {}
    metadata = {}

    def save(file_uuid, data, storage_type=None, storage_object_id=None):
        assert observer.depth == 0, "S3 upload held a database session"
        objects[storage_object_id] = data
        return file_storage.WorkspaceFileStorageInfo("s3", "test", storage_object_id)

    def read(file_uuid, storage_type=None, storage_object_id=None):
        assert observer.depth == 0, "S3 read held a database session"
        return objects[storage_object_id]

    def save_metadata(value, storage_type=None):
        assert observer.depth == 0, "S3 metadata upload held a database session"
        metadata[str(value.uuid)] = value

    monkeypatch.setattr(file_storage, "save_workspace_file", save)
    monkeypatch.setattr(file_storage, "read_workspace_file", read)
    monkeypatch.setattr(file_storage, "save_workspace_file_metadata", save_metadata)
    ingress = http_service.HistoryImportService(
        types.SimpleNamespace(authenticate_certificate=lambda _: identity),
        sql_state.SQLControlState(realm, b"k" * 32),
        session_factory,
    )
    worker = agents.HistoryImportWorker(session_factory)
    yield types.SimpleNamespace(**locals())
    with context.session_manager() as session:
        read_state.clear_stream_for_all_users(session, project, stream)
        session.execute(
            "DELETE FROM messenger_messages WHERE project_id = %s", (project,)
        )
        session.execute(
            "DELETE FROM messenger_streams WHERE project_id = %s", (project,)
        )
        session.execute(
            "DELETE FROM m_workspace_users WHERE provider_uuid=%s", (bridge,)
        )
        session.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (project,),
        )
        session.execute(
            "DELETE FROM messenger_domain_outbox_events WHERE project_id = %s",
            (project,),
        )
        # The database lives for the whole module/session. Remove only this
        # fixture's roots; their foreign keys clean jobs, files, chats and queues.
        session.execute(
            """DELETE FROM m_external_accounts_v2
               WHERE provider_realm_uuid=%s OR uuid=%s OR uuid IN (
                   SELECT resource_uuid FROM m_external_bridge_desired_resources_v1
                   WHERE bridge_instance_uuid=%s AND resource_type='external_account')""",
            (realm, account, bridge),
        )
        session.execute(
            "DELETE FROM m_external_bridge_instances_v2 WHERE uuid=%s",
            (bridge,),
        )
        session.execute(
            "DELETE FROM m_external_provider_identity_links_v1 WHERE provider='zulip' AND provider_realm_uuid=%s",
            (realm,),
        )


def envelope(setup, *, count=3, generation=1, content="Historical message"):
    messages = []
    for index in range(1, count + 1):
        message = {
            "id": index,
            "sender_id": 7,
            "channel_id": 42,
            "channel_name": "Import",
            "topic": "Import",
            "sent_at": 1788000000 + index,
            "content": content,
            "reactions": [
                {
                    "user_id": 8,
                    "reaction_type": "unicode_emoji",
                    "emoji_code": "1f44d",
                    "emoji_name": "+1",
                }
            ],
            "access": [{"user_id": 7, "read": index != 2, "starred": index == 1}],
        }
        message["hash"] = contract.digest(message)
        messages.append(message)
    batch = {
        "from_id": 1,
        "to_id": 5000,
        "users": [{"id": 7, "name": "Owner"}, {"id": 8, "name": "Directory only"}],
        "messages": messages,
    }
    batch["hash"] = contract.digest(batch)
    return {
        "schema_version": 1,
        "project_uuid": str(setup.project),
        "provider_realm_uuid": str(setup.realm),
        "generation": generation,
        "sources": [
            {
                "account_uuid": str(setup.account),
                "account_generation": 1,
                "chat_uuid": str(setup.chat),
                "assignment_generation": 1,
            }
        ],
        "batch": batch,
    }


def accept(setup, body):
    response = setup.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(body), b"certificate"
    )
    assert response.status in {200, 202}, response.body
    return sys_uuid.UUID(json.loads(response.body)["uuid"])


def finish(setup, job_uuid):
    for _ in range(100):
        with setup.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at = now() WHERE uuid = %s",
                (job_uuid,),
            )
            job = repository.get_job(session, setup.identity, job_uuid)
            if job["status"] in {"complete", "waiting_files", "failed", "superseded"}:
                return job
            claimed = repository.claim(session, setup.worker.worker_uuid)
        assert claimed is not None
        try:
            setup.worker.process(setup.identity, claimed)
        except agents.WriteBudgetExceeded:
            setup.worker.retry(claimed, "WriteBudgetExceeded", shrink=True)
    raise AssertionError("Import did not complete")


@pytest.mark.parametrize(
    "chat_type,legacy_key,recipients",
    [
        ("personal", "direct:7,8", [7, 8]),
        ("group", "group_direct:7,8,9", [7, 8, 9]),
    ],
    ids=["direct", "group-direct"],
)
@pytest.mark.parametrize("desired_key", ["legacy", "canonical", "empty", "missing"])
@pytest.mark.parametrize(
    "topic_state", ["existing", "missing_default", "missing_single"]
)
def test_direct_history_accepts_actual_desired_chat_keys(
    history_setup, chat_type, legacy_key, recipients, desired_key, topic_state
):
    s = history_setup
    topic_uuid = s.topic if topic_state == "existing" else sys_uuid.uuid4()
    provider_topic_id = f"{legacy_key}:default"
    canonical_key = f"direct-conversation:v1:{len(recipients)}:" + ",".join(
        map(str, recipients)
    )
    source = {
        "kind": "zulip",
        "chat_type": chat_type,
        "topics": [
            {
                "topic_uuid": str(topic_uuid),
                "provider_topic_id": provider_topic_id,
                "name": "Zulip",
                "is_default": topic_state != "missing_single",
            }
        ],
    }
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_chats_v2 SET provider_chat_id = %s, source = %s::jsonb, revision = 2 WHERE uuid = %s",
            (legacy_key, json.dumps(source), s.chat),
        )
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(s.chat)}, session=session
        )
        desired = sql_state.external_chat_assignment_desired(chat, session=session)
        assert desired["provider_chat"]["provider_chat_key"] == legacy_key
        if desired_key == "canonical":
            desired["provider_chat"]["provider_chat_key"] = canonical_key
        elif desired_key == "empty":
            desired["provider_chat"]["provider_chat_key"] = ""
        elif desired_key == "missing":
            del desired["provider_chat"]["provider_chat_key"]
        sql_state.append_upsert(session, s.bridge, "zulip", desired)

    body = envelope(s)
    body["sources"][0]["assignment_generation"] = 2
    body["batch"]["users"] = [
        {"id": user_id, "name": f"User {user_id}"} for user_id in recipients
    ]
    for message in body["batch"]["messages"]:
        message.update(
            channel_id=None, channel_name=None, topic=None, recipient_ids=recipients
        )
        message["hash"] = contract.digest(
            {key: value for key, value in message.items() if key != "hash"}
        )
    body["batch"]["hash"] = contract.digest(
        {key: value for key, value in body["batch"].items() if key != "hash"}
    )
    job_uuid = accept(s, body)
    job = finish(s, job_uuid)
    assert job["status"] == "complete"
    assert job["applied_messages"] == 3
    with s.session_factory() as session:
        routes = repository.authorize_sources(
            session, s.identity, contract.Batch.parse(body)
        )
        assert routes[0]["chat_key"] == canonical_key
        messages = session.execute(
            """SELECT message.provider_message_id, message.payload,
                      placement.stream_uuid, placement.topic_uuid
               FROM messenger_messages AS message
               JOIN messenger_message_placements AS placement
                 ON placement.message_uuid = message.uuid
               WHERE message.provider_realm_uuid = %s
               ORDER BY message.provider_message_id""",
            (s.realm,),
        ).fetchall()
        assert [row["provider_message_id"] for row in messages] == ["1", "2", "3"]
        assert all(
            row["payload"]["content"] == "Historical message" for row in messages
        )
        assert all(row["stream_uuid"] == s.stream for row in messages)
        assert all(row["topic_uuid"] == topic_uuid for row in messages)
        persisted_topic = session.execute(
            "SELECT provider FROM messenger_topics WHERE project_id=%s AND uuid=%s",
            (s.project, topic_uuid),
        ).fetchone()
        if topic_state != "existing":
            assert persisted_topic["provider"]["external_id"] == provider_topic_id
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(s.chat)}, session=session
        )
        # Creating a missing canonical row must not append a second, malformed
        # catalog entry or change the bridge's existing provider topic identity.
        assert chat.source["topics"] == source["topics"]
        desired = sql_state.external_chat_assignment_desired(chat, session=session)
        assert [
            topic["provider_topic_id"]
            for topic in desired["workspace_projection"]["topics"]
            if topic["topic_uuid"] == str(topic_uuid)
        ] == [provider_topic_id]
    response = s.ingress.handle(
        "GET",
        f"{contract.PATH}/{job_uuid}/mappings/{s.account}/messages/0",
        {},
        b"",
        b"certificate",
    )
    assert response.status == 200, response.body
    mappings = json.loads(response.body)["mappings"]
    assert len([row for row in mappings if row["kind"] == "message"]) == 3
    assert all(row["metadata"]["chat_key"] == legacy_key for row in mappings)
    assert [row["provider_id"] for row in mappings if row["kind"] == "topic"] == [
        provider_topic_id
    ]
    assert all(
        row["metadata"]["topic_provider_id"] == provider_topic_id
        for row in mappings
        if row["kind"] == "message"
    )
    for mapping in mappings:
        if mapping["kind"] == "message":
            response = s.api.get(f"/v1/messages/{mapping['workspace_uuid']}")
            assert response.status_code == 200
    if topic_state != "existing":
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_chats_v2 SET source=jsonb_set(source,'{topics}','[]'::jsonb) WHERE uuid=%s",
                (s.chat,),
            )
        response = s.ingress.handle(
            "GET",
            f"{contract.PATH}/{job_uuid}/mappings/{s.account}/messages/0",
            {},
            b"",
            b"certificate",
        )
        assert response.status == 200, response.body
        # The receipt falls back to the persisted provider blob when the live
        # catalog no longer contains this topic, retaining the same identities.
        assert json.loads(response.body)["mappings"] == mappings


def test_import_is_resumable_and_preserves_live_state(history_setup):
    s = history_setup
    body = envelope(s)
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        messages = session.execute(
            "SELECT * FROM messenger_messages WHERE provider_realm_uuid = %s ORDER BY provider_message_id",
            (s.realm,),
        ).fetchall()
        assert len(messages) == 3
        states = session.execute(
            "SELECT read_at, starred FROM messenger_user_message_states WHERE project_id = %s ORDER BY placement_uuid",
            (s.project,),
        ).fetchall()
        assert len(states) == 3
        assert sum(row["read_at"] is None for row in states) == 1
        assert sum(row["starred"] for row in states) == 1
        counts = session.execute(
            "SELECT unread_count FROM messenger_stream_bindings WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
            (s.project, s.stream, s.owner),
        ).fetchone()
        assert counts["unread_count"] == 1
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_message_reaction_facts WHERE project_id = %s",
                (s.project,),
            ).fetchone()["n"]
            == 3
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_user_message_bindings WHERE project_id = %s AND user_uuid <> %s",
                (s.project, s.owner),
            ).fetchone()["n"]
            == 0
        )
        session.execute(
            "UPDATE messenger_user_message_states SET starred = false WHERE project_id = %s",
            (s.project,),
        )
        session.execute(
            'UPDATE messenger_messages SET payload = \'{"kind":"markdown","content":"Live edit"}\'::jsonb WHERE provider_realm_uuid = %s',
            (s.realm,),
        )
    assert accept(s, body) == job_uuid
    second = accept(s, envelope(s, generation=2, content="Stale edit"))
    assert finish(s, second)["status"] == "complete"
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 3
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s AND payload->>'content' = 'Live edit'",
                (s.realm,),
            ).fetchone()["n"]
            == 3
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_user_message_states WHERE project_id = %s AND starred",
                (s.project,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("resumed_discovery", [False, True])
def test_attachments_are_staged_before_message_transaction(
    history_setup, resumed_discovery
):
    s = history_setup
    job_uuid = accept(
        s,
        envelope(
            s,
            count=1,
            content="````quote\n```quote\n[sample](/user_uploads/1/a/sample.txt)\n```\n````",
        ),
    )
    if resumed_discovery:
        # The previous worker treated semantic quotes as literal code fences.
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET files_discovered=true WHERE uuid=%s",
                (job_uuid,),
            )
    assert finish(s, job_uuid)["status"] == "waiting_files"
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job_uuid}", {}, b"", b"certificate"
    )
    request = json.loads(response.body)["files"][0]
    response = s.ingress.handle(
        "PUT",
        f"{contract.PATH}/{job_uuid}/files/{request['uuid']}",
        {"X-File-Name": "sample.txt", "Content-Type": "text/plain"},
        b"fixture bytes",
        b"certificate",
    )
    assert response.status == 200, response.body
    assert finish(s, job_uuid)["status"] == "complete"
    assert len(s.metadata) == 1
    with s.session_factory() as session:
        payload = session.execute(
            "SELECT payload FROM messenger_messages WHERE provider_realm_uuid = %s",
            (s.realm,),
        ).fetchone()["payload"]
        assert "urn:file:" in payload["content"]
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_workspace_file_accesses WHERE project_id = %s",
                (s.project,),
            ).fetchone()["n"]
            == 1
        )


def test_changed_source_generation_stops_an_old_job(history_setup):
    s = history_setup
    accept(s, envelope(s))
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET generation = 2 WHERE resource_uuid = %s",
            (s.chat,),
        )
        job = repository.claim(session, s.worker.worker_uuid)
    with pytest.raises(contract.ImportError, match="history_sources_changed"):
        s.worker.process(s.identity, job)
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("predates_migration", [False, True])
def test_deleted_messages_are_not_resurrected(history_setup, predates_migration):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        session.execute(
            "UPDATE messenger_messages SET deleted_at = now() WHERE provider_realm_uuid = %s",
            (s.realm,),
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_external_history_tombstones_v1 WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 1
        )
        if predates_migration:
            # Model a soft-deleted row retained from before the update trigger.
            session.execute(
                "DELETE FROM m_external_history_tombstones_v1 WHERE provider_realm_uuid = %s",
                (s.realm,),
            )
        session.execute(
            "DELETE FROM messenger_messages WHERE provider_realm_uuid = %s", (s.realm,)
        )
    second = accept(s, envelope(s, count=1, generation=2))
    assert finish(s, second)["status"] == "complete"
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


def test_selected_history_depth_filters_each_observer(history_setup):
    s = history_setup
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET resource = jsonb_set(resource, '{history_depth}', '\"7_days\"'::jsonb) WHERE resource_uuid = %s",
            (s.chat,),
        )
    body = envelope(s, count=2)
    message = body["batch"]["messages"][1]
    message["sent_at"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["inserted_messages"] == 1


def test_rollback_keeps_checkpoint_and_counters_atomic(history_setup, monkeypatch):
    s = history_setup
    body = envelope(s, count=101)
    job_uuid = accept(s, body)
    with s.session_factory() as session:
        job = repository.claim(session, s.worker.worker_uuid)
    s.worker.process(s.identity, job)  # file discovery
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET available_at = now() WHERE uuid = %s",
            (job_uuid,),
        )
        job = repository.claim(session, s.worker.worker_uuid)
    s.worker.process(s.identity, job)  # directory
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET available_at = now() WHERE uuid = %s",
            (job_uuid,),
        )
        job = repository.claim(session, s.worker.worker_uuid)
    clock_values = iter([0, 1])
    monkeypatch.setattr(s.worker, "clock", lambda: next(clock_values))
    with pytest.raises(agents.WriteBudgetExceeded):
        s.worker.process(s.identity, job)
    with s.session_factory() as session:
        persisted = repository.get_job(session, s.identity, job_uuid)
        assert persisted["next_message_id"] == 0
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )
        repository.release(session, job)
    monkeypatch.setattr(s.worker, "clock", time.monotonic)
    assert finish(s, job_uuid)["inserted_messages"] == 101


def test_large_batch_records_bounded_transactions(history_setup):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=5000))
    result = finish(s, job_uuid)
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 5000
    assert result["max_write_seconds"] < 0.5
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_domain_outbox_events WHERE project_id = %s AND event_kind = 'read_counters'",
                (s.project,),
            ).fetchone()["n"]
            == 2
        )


def _two_history_observers(s):
    peer, account, chat = [sys_uuid.uuid4() for _ in range(3)]
    conftest.seed_user_stream_binding(s.db, s.project, s.stream, peer)
    with s.session_factory() as session:
        session.execute(
            """INSERT INTO m_external_accounts_v2
            (uuid, owner_user_uuid, provider, settings, provider_realm_uuid, provider_owner_user_id)
            VALUES (%s, %s, 'zulip', '{"server_url":"https://zulip.example.test"}'::jsonb, %s, '8')""",
            (account, peer, s.realm),
        )
        session.execute(
            """INSERT INTO m_external_chats_v2
            (uuid, external_account_uuid, owner_user_uuid, provider, provider_chat_id, source,
             display_name, selected, project_id, history_depth, projection_stream_uuid, status)
            SELECT %s, %s, %s, provider, provider_chat_id, source, display_name, selected,
                   project_id, history_depth, projection_stream_uuid, status FROM m_external_chats_v2 WHERE uuid = %s""",
            (chat, account, peer, s.chat),
        )
        session.execute(
            """INSERT INTO m_external_provider_identity_links_v1
            (provider, provider_realm_uuid, provider_user_id, workspace_user_uuid, link_kind)
            VALUES ('zulip', %s, '8', %s, 'verified_account_owner')""",
            (s.realm, peer),
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            {
                "resource_type": "external_account",
                "uuid": str(account),
                "generation": 1,
                "synchronization_enabled": True,
            },
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            {
                "resource_type": "external_chat_assignment",
                "uuid": str(chat),
                "generation": 1,
                "selected": True,
                "history_depth": "all",
                "provider_chat": {"provider_chat_key": "channel:42"},
            },
        )
    body = envelope(s, count=1)
    body["sources"].append(
        dict(
            account_uuid=str(account),
            account_generation=1,
            chat_uuid=str(chat),
            assignment_generation=1,
        )
    )
    body["sources"].sort(key=lambda source: source["chat_uuid"])
    message = body["batch"]["messages"][0]
    message["access"].append(dict(user_id=8, read=False, starred=False))
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    return peer, account, chat, body


def test_two_observers_keep_independent_flags_and_api_visibility(history_setup):
    s = history_setup
    peer, _account, _chat, body = _two_history_observers(s)
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        placement = session.execute(
            "SELECT uuid FROM messenger_message_placements WHERE project_id = %s AND stream_uuid = %s",
            (s.project, s.stream),
        ).fetchone()["uuid"]
    own_response = s.api.get(f"/v1/messages/{placement}")
    other_response = s.api.get(f"/v1/messages/{placement}", user=peer)
    assert own_response.status_code == 200, own_response.text
    assert other_response.status_code == 200, other_response.text
    own, other = own_response.json(), other_response.json()
    assert own["read"] and own["starred"]
    assert not other["read"] and not other["starred"]
    stranger = sys_uuid.uuid4()
    assert s.api.get(f"/v1/messages/{placement}", user=stranger).status_code == 404


def test_two_observers_resolve_quote_through_materialized_topic_after_catalog_omission(
    history_setup,
):
    s = history_setup
    peer, _account, chat, body = _two_history_observers(s)
    omitted_chat = min(s.chat, chat, key=str)
    sender_id = 7 if omitted_chat == s.chat else 8
    with s.session_factory() as session:
        session.execute(
            """UPDATE m_external_chats_v2 SET revision=2,
                   source=source || %s::jsonb WHERE uuid=%s""",
            (
                json.dumps(
                    {
                        "chat_type": "channel",
                        "topics": [
                            {
                                "topic_uuid": str(s.topic),
                                "provider_topic_id": "42:Import",
                                "name": "Import",
                                "is_default": False,
                            }
                        ],
                    }
                ),
                omitted_chat,
            ),
        )
        model = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(omitted_chat)}, session=session
        )
        desired = sql_state.external_chat_assignment_desired(model, session=session)
        assert desired["workspace_projection"]["topics"][0]["topic_uuid"] == str(
            s.topic
        )
        sql_state.append_upsert(session, s.bridge, "zulip", desired)
        session.execute(
            "UPDATE m_external_chats_v2 SET source=jsonb_set(source,'{topics}','[]'::jsonb) WHERE uuid=%s",
            (omitted_chat,),
        )
    for source in body["sources"]:
        if source["chat_uuid"] == str(omitted_chat):
            source["assignment_generation"] = 2
    first = body["batch"]["messages"][0]
    first["sender_id"] = sender_id
    first["hash"] = contract.digest({k: v for k, v in first.items() if k != "hash"})
    second = dict(
        first,
        id=2,
        content="[first](https://zulip.example.test/#narrow/channel/42-Import/topic/Import/near/1)",
    )
    second["hash"] = contract.digest({k: v for k, v in second.items() if k != "hash"})
    body["batch"]["messages"].append(second)
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    with s.session_factory() as session:
        routes = repository.authorize_sources(
            session, s.identity, contract.Batch.parse(body)
        )
        assert str(routes[0]["chat_uuid"]) == str(omitted_chat)
        assert routes[-1]["chat_uuid"] != omitted_chat
        assert all(
            row["source"]["topics"][0]["topic_uuid"] == str(s.topic) for row in routes
        )
    assert finish(s, accept(s, body))["inserted_messages"] == 2
    with s.session_factory() as session:
        rows = session.execute(
            """SELECT message.provider_message_id, message.payload,
                      placement.uuid, placement.topic_uuid
               FROM messenger_messages AS message
               JOIN messenger_message_placements AS placement
                 ON placement.message_uuid=message.uuid
               WHERE message.provider_realm_uuid=%s ORDER BY message.provider_message_id""",
            (s.realm,),
        ).fetchall()
    assert [row["provider_message_id"] for row in rows] == ["1", "2"]
    assert all(row["topic_uuid"] == s.topic for row in rows)
    assert f"urn:message:{rows[0]['uuid']}" in rows[1]["payload"]["content"]
    assert "https://zulip.example.test" not in rows[1]["payload"]["content"]
    for user in [s.owner, peer]:
        assert (
            s.api.get(f"/v1/messages/{rows[0]['uuid']}", user=user).status_code == 200
        )


def test_metadata_generation_accepts_same_hash_as_a_new_job(history_setup):
    s = history_setup
    body = envelope(s)
    old_uuid = accept(s, body)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET generation = 2 WHERE resource_uuid = %s",
            (s.chat,),
        )
    body["sources"][0]["assignment_generation"] = 2
    new_uuid = accept(s, body)
    assert new_uuid != old_uuid
    with s.session_factory() as session:
        assert (
            repository.get_job(session, s.identity, old_uuid)["status"] == "superseded"
        )
    assert finish(s, new_uuid)["status"] == "complete"


def test_waiting_for_files_rechecks_revoked_source(history_setup):
    s = history_setup
    job_uuid = accept(s, envelope(s, content="[file](/user_uploads/1/sample.txt)"))
    assert finish(s, job_uuid)["status"] == "waiting_files"
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET generation = 2 WHERE resource_uuid = %s",
            (s.chat,),
        )
        session.execute(
            "UPDATE m_external_history_imports_v1 SET available_at = now() WHERE uuid = %s",
            (job_uuid,),
        )
    assert s.worker.run_once()
    with s.session_factory() as session:
        assert (
            repository.get_job(session, s.identity, job_uuid)["status"] == "superseded"
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid = %s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


def test_completed_import_returns_canonical_routing_without_message_bodies(
    history_setup,
):
    s = history_setup
    job_uuid = accept(s, envelope(s))
    assert finish(s, job_uuid)["status"] == "complete"
    base = f"{contract.PATH}/{job_uuid}/mappings/{s.account}"
    response = s.ingress.handle("GET", base + "/users/0", {}, b"", b"certificate")
    assert response.status == 200, response.body
    users = json.loads(response.body)
    assert {m["provider_id"] for m in users["mappings"]} == {"7", "8"}
    assert users["next_cursor"] is None
    response = s.ingress.handle("GET", base + "/messages/0", {}, b"", b"certificate")
    assert response.status == 200, response.body
    page = json.loads(response.body)
    assert page["next_cursor"] == "250"
    assert len(page["mappings"]) == 4
    assert page["mappings"][0]["kind"] == "topic"
    assert page["mappings"][0]["workspace_uuid"] == str(s.topic)
    for mapping in page["mappings"]:
        assert mapping["metadata"]["chat_key"] == "channel:42"
        assert "payload" not in mapping and "content" not in mapping["metadata"]
        if mapping["kind"] == "message":
            response = s.api.get(f"/v1/messages/{mapping['workspace_uuid']}")
            assert response.status_code == 200
    response = s.ingress.handle(
        "GET",
        f"{contract.PATH}/{job_uuid}/mappings/{sys_uuid.uuid4()}/messages/0",
        {},
        b"",
        b"certificate",
    )
    assert response.status == 409


def test_provider_pause_prevents_further_import_parts(history_setup):
    s = history_setup
    job_uuid = accept(s, envelope(s))
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_provider_policies_v1 SET emergency_suspended = true WHERE provider = 'zulip'",
            (),
        )
        job = repository.claim(session, s.worker.worker_uuid)
    with pytest.raises(contract.ImportError, match="history_provider_paused"):
        s.worker.process(s.identity, job)
    with s.session_factory() as session:
        assert (
            repository.get_job(session, s.identity, job_uuid)["applied_messages"] == 0
        )
        session.execute(
            "UPDATE m_external_provider_policies_v1 SET emergency_suspended = false WHERE provider = 'zulip'",
            (),
        )


def test_large_directory_is_resolved_in_bounded_pages_and_cached(
    history_setup, monkeypatch
):
    from workspace.history_import import preparation

    s = history_setup
    body = envelope(s, count=1)
    body["batch"]["users"] += [
        {"id": i, "name": f"Directory {i}"} for i in range(9, 1010)
    ]
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    sizes = []
    original = preparation.load_identities

    def bounded(session, page):
        sizes.append(len(page.body["users"]))
        assert sizes[-1] <= 100
        return original(session, page)

    monkeypatch.setattr(preparation, "load_identities", bounded)
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "complete"
    assert sum(sizes) == len(body["batch"]["users"])
    body["generation"] += 1
    assert finish(s, accept(s, body))["status"] == "complete"
    assert sum(sizes) == len(body["batch"]["users"])


def test_two_large_directories_make_progress_without_cache_thrashing(history_setup):
    s = history_setup
    jobs = []
    for offset in (0, 5000):
        body = envelope(s, count=1)
        body["batch"]["users"] += [
            {"id": i, "name": f"User {i}"} for i in range(9, 210 + offset // 5000)
        ]
        body["batch"]["from_id"] += offset
        body["batch"]["to_id"] += offset
        body["batch"]["messages"][0]["id"] += offset
        m = body["batch"]["messages"][0]
        m["hash"] = contract.digest({k: v for k, v in m.items() if k != "hash"})
        body["batch"]["hash"] = contract.digest(
            {k: v for k, v in body["batch"].items() if k != "hash"}
        )
        jobs.append(accept(s, body))
    for _ in range(50):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=ANY(%s::uuid[])",
                (jobs,),
            )
            statuses = [
                repository.get_job(session, s.identity, job)["status"] for job in jobs
            ]
        if statuses == ["complete", "complete"]:
            break
        s.worker.run_once()
    assert statuses == ["complete", "complete"]


def test_file_resolution_only_loads_paths_used_by_current_part(
    history_setup, monkeypatch
):
    from workspace.history_import import preparation

    s = history_setup
    body = envelope(s, count=3)
    for m in body["batch"]["messages"]:
        m["content"] = f"[sample](/user_uploads/1/{m['id']}/sample.txt)"
        m["hash"] = contract.digest({k: v for k, v in m.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "waiting_files"
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job_uuid}", {}, b"", b"certificate"
    )
    for request in json.loads(response.body)["files"]:
        response = s.ingress.handle(
            "PUT",
            f"{contract.PATH}/{job_uuid}/files/{request['uuid']}",
            {"X-File-Name": "sample.txt", "Content-Type": "text/plain"},
            b"fixture",
            b"certificate",
        )
        assert response.status == 200
    original = preparation.prepare_part
    counts = []

    def checked(*args, **kwargs):
        messages, files = args[2], args[6]
        expected = {
            path
            for m in messages
            for path in preparation.attachment_paths(m["content"])
        }
        assert set(files) == expected
        counts.append(len(files))
        return original(*args, **kwargs)

    monkeypatch.setattr(preparation, "prepare_part", checked)
    monkeypatch.setattr(agents, "WRITE_BUDGET_SECONDS", 1)
    # Keep each quantum at one message; normal adaptation is disabled in this test.
    original_write = s.worker.write

    def write(*args):
        result = original_write(*args)
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET part_size=1 WHERE uuid=%s",
                (job_uuid,),
            )
        return result

    monkeypatch.setattr(s.worker, "write", write)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET part_size=1 WHERE uuid=%s",
            (job_uuid,),
        )
    assert finish(s, job_uuid)["status"] == "complete"
    assert counts == [1, 1, 1]


@pytest.mark.parametrize(
    "old_content",
    ["[old](/user_uploads/1/old/file.txt)", "[invalid](/user_uploads/../file)"],
)
def test_depth_excluded_messages_need_no_topic_files_or_pending_quote_mapping(
    history_setup, monkeypatch, old_content
):
    from workspace.history_import import preparation

    s = history_setup
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET resource = jsonb_set(resource, '{history_depth}', '\"7_days\"'::jsonb) WHERE resource_uuid = %s",
            (s.chat,),
        )
    body = envelope(s, count=2)
    old, recent = body["batch"]["messages"]
    old.update(topic="Removed topic", content=old_content)
    recent["sent_at"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    for message in (old, recent):
        message["hash"] = contract.digest(
            {k: v for k, v in message.items() if k != "hash"}
        )
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    original = preparation.mappings_for

    def checked(*args):
        result = original(*args)
        assert ("message", str(old["id"])) not in result.mappings
        assert ("message", str(recent["id"])) in result.mappings
        return result

    monkeypatch.setattr(preparation, "mappings_for", checked)
    result = finish(s, accept(s, body))
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 1


def test_backed_off_batch_does_not_starve_other_ready_imports(
    history_setup, monkeypatch
):
    s = history_setup
    bad_body = envelope(s, count=1)
    bad_body["batch"]["messages"][0]["topic"] = "Missing topic"
    m = bad_body["batch"]["messages"][0]
    m["hash"] = contract.digest({k: v for k, v in m.items() if k != "hash"})
    bad_body["batch"]["hash"] = contract.digest(
        {k: v for k, v in bad_body["batch"].items() if k != "hash"}
    )
    bad = accept(s, bad_body)
    original = s.worker.process

    def fail_one(identity, job):
        if job["uuid"] == bad:
            s.worker.preferred_uuid = bad
            raise contract.ImportError("history_topic_mapping_pending", 409)
        return original(identity, job)

    monkeypatch.setattr(s.worker, "process", fail_one)
    for _ in range(20):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (bad,),
            )
        s.worker.run_once()
        with s.session_factory() as session:
            if repository.get_job(session, s.identity, bad)["safe_error"]:
                break
    body = envelope(s, count=1)
    body["batch"]["from_id"], body["batch"]["to_id"] = 5001, 10000
    m = body["batch"]["messages"][0]
    m["id"] = 5001
    m["hash"] = contract.digest({k: v for k, v in m.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    good = accept(s, body)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET available_at=now()+interval '1 hour' WHERE uuid=%s",
            (bad,),
        )
    for _ in range(20):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (good,),
            )
            result = repository.get_job(session, s.identity, good)
        if result["status"] == "complete":
            break
        s.worker.run_once()
    assert result["status"] == "complete"


def test_directory_preserves_linked_profile_avatar(history_setup):
    s = history_setup
    with s.session_factory() as session:
        before = session.execute(
            "SELECT avatar FROM m_workspace_users WHERE uuid=%s", (s.owner,)
        ).fetchone()["avatar"]
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT avatar FROM m_workspace_users WHERE uuid=%s", (s.owner,)
            ).fetchone()["avatar"]
            == before
        )


def test_history_materializes_topic_absent_from_current_catalog(history_setup):
    s = history_setup
    body = envelope(s, count=2)
    for message in body["batch"]["messages"]:
        message["topic"] = "Historical topic"
        message["hash"] = contract.digest(
            {k: v for k, v in message.items() if k != "hash"}
        )
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    result = finish(s, accept(s, body))
    assert result["inserted_messages"] == 2
    with s.session_factory() as session:
        topic = session.execute(
            "SELECT uuid FROM messenger_topics WHERE project_id=%s AND name='Historical topic'",
            (s.project,),
        ).fetchone()
        source = session.execute(
            "SELECT source FROM m_external_chats_v2 WHERE uuid=%s", (s.chat,)
        ).fetchone()["source"]
    assert any(
        item["topic_uuid"] == str(topic["uuid"])
        and item["provider_topic_id"] == "42:Historical topic"
        for item in source["topics"]
    )
    body["generation"] += 1
    assert finish(s, accept(s, body))["inserted_messages"] == 0

    # The provider's current catalog can omit materialized historical topics.
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_chats_v2 SET source=jsonb_set(source, '{topics}', '[]') WHERE uuid=%s",
            (s.chat,),
        )
    body["generation"] += 1
    repeated = accept(s, body)
    assert finish(s, repeated)["inserted_messages"] == 0
    response = s.ingress.handle(
        "GET",
        f"{contract.PATH}/{repeated}/mappings/{s.account}/messages/0",
        {},
        b"",
        b"certificate",
    )
    mappings = json.loads(response.body)["mappings"]
    assert len(mappings) == 3
    assert mappings[0]["workspace_uuid"] == str(topic["uuid"])
    assert mappings[0]["provider_id"] == "42:Historical topic"
    assert all(
        item["metadata"]["topic_provider_id"] == "42:Historical topic"
        for item in mappings[1:]
    )


def test_routing_receipts_require_current_membership_and_visible_bindings(
    history_setup,
):
    s = history_setup
    job = accept(s, envelope(s, count=1))
    assert finish(s, job)["status"] == "complete"
    path = f"{contract.PATH}/{job}/mappings/{s.account}/messages/0"
    response = s.ingress.handle("GET", path, {}, b"", b"certificate")
    assert len(json.loads(response.body)["mappings"]) == 2
    with s.session_factory() as session:
        session.execute(
            "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (s.project, s.owner, s.stream),
        )
    response = s.ingress.handle("GET", path, {}, b"", b"certificate")
    assert response.status == 200
    assert json.loads(response.body)["mappings"] == []


def test_links_to_new_historical_topics_resolve_before_conversion(history_setup):
    s = history_setup
    body = envelope(s, count=1, content="#**Import>Historical topic**")
    message = body["batch"]["messages"][0]
    message["topic"] = "Historical topic"
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    assert finish(s, accept(s, body))["inserted_messages"] == 1
    with s.session_factory() as session:
        row = session.execute(
            "SELECT placement.topic_uuid, message.payload FROM messenger_messages message JOIN messenger_message_placements placement ON placement.message_uuid=message.uuid WHERE message.provider_realm_uuid=%s",
            (s.realm,),
        ).fetchone()
    payload = json.dumps(row["payload"])
    assert "#**Import>Historical topic**" not in payload
    assert str(row["topic_uuid"]) in payload


def test_deleting_partial_provider_identity_does_not_create_invalid_tombstone(
    history_setup,
):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    with s.session_factory() as session:
        session.execute(
            "UPDATE messenger_messages SET provider_message_id=NULL WHERE provider_realm_uuid=%s",
            (s.realm,),
        )
        session.execute(
            "UPDATE messenger_messages SET deleted_at=now() WHERE provider_realm_uuid=%s",
            (s.realm,),
        )
        session.execute(
            "DELETE FROM messenger_messages WHERE provider_realm_uuid=%s",
            (s.realm,),
        )
        assert (
            session.execute(
                "SELECT count(*) n FROM m_external_history_tombstones_v1 WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


def test_historical_topic_assignment_survives_observer_namespace_change(
    history_setup, monkeypatch
):
    s = history_setup
    body = envelope(s, count=1)
    body["batch"]["messages"][0]["topic"] = "Historical topic"
    body["batch"]["messages"][0]["hash"] = contract.digest(
        {k: v for k, v in body["batch"]["messages"][0].items() if k != "hash"}
    )
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    assert finish(s, accept(s, body))["inserted_messages"] == 1
    with s.session_factory() as session:
        topic = session.execute(
            "SELECT uuid,name FROM messenger_topics WHERE project_id=%s AND name='Historical topic'",
            (s.project,),
        ).fetchone()
        session.execute(
            "UPDATE m_external_chats_v2 SET source=jsonb_set(source,'{topics}','[]') WHERE uuid=%s",
            (s.chat,),
        )
        session.execute(
            "UPDATE m_external_bridge_desired_resources_v1 SET resource=resource || %s::jsonb WHERE resource_uuid=%s",
            (
                json.dumps(
                    {
                        "workspace_projection": {
                            "topics": [
                                {
                                    "topic_uuid": str(topic["uuid"]),
                                    "provider_topic_id": "42:Historical topic",
                                    "name": topic["name"],
                                    "is_default": False,
                                }
                            ]
                        }
                    }
                ),
                s.chat,
            ),
        )
    original = repository.authorize_sources
    changed_namespace = sys_uuid.uuid4()

    def changed_observer_scope(*args):
        routes = original(*args)
        for route in routes:
            route["topic_namespace_uuid"] = changed_namespace
        return routes

    monkeypatch.setattr(repository, "authorize_sources", changed_observer_scope)
    extra = dict(body["batch"]["messages"][0], id=2)
    extra["hash"] = contract.digest({k: v for k, v in extra.items() if k != "hash"})
    body["batch"]["messages"].append(extra)
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )
    body["generation"] += 1
    assert finish(s, accept(s, body))["inserted_messages"] == 1
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) n FROM messenger_topics WHERE project_id=%s AND name='Historical topic'",
                (s.project,),
            ).fetchone()["n"]
            == 1
        )
        assert (
            session.execute(
                "SELECT count(*) n FROM messenger_message_placements WHERE project_id=%s AND topic_uuid=%s",
                (s.project, topic["uuid"]),
            ).fetchone()["n"]
            == 2
        )


@pytest.mark.parametrize(
    "case",
    [
        "too_large",
        "disabled",
        "missing_limit",
        "empty_content_type",
        "lowered_during_storage",
    ],
)
def test_history_file_upload_enforces_current_policy_and_domain(
    history_setup, monkeypatch, case
):
    s = history_setup
    job = accept(s, envelope(s, count=1, content="[file](/user_uploads/1/file.txt)"))
    assert finish(s, job)["status"] == "waiting_files"
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job}", {}, b"", b"certificate"
    )
    requested = json.loads(response.body)["files"][0]["uuid"]
    initial_files = len(s.objects)
    limits = {
        "max_file_bytes": 3
        if case == "too_large"
        else 0
        if case == "disabled"
        else 1024
    }
    if case == "missing_limit":
        limits = {}
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_provider_policies_v1 SET limits=%s::jsonb WHERE provider='zulip'",
            (json.dumps(limits),),
        )
    if case == "lowered_during_storage":
        original = file_storage.save_workspace_file

        def lower_limit(*args, **kwargs):
            stored = original(*args, **kwargs)
            with s.session_factory() as session:
                session.execute(
                    "UPDATE m_external_provider_policies_v1 SET limits='{}' WHERE provider='zulip'"
                )
            return stored

        monkeypatch.setattr(file_storage, "save_workspace_file", lower_limit)
    response = s.ingress.handle(
        "PUT",
        f"{contract.PATH}/{job}/files/{requested}",
        {
            "X-File-Name": "file.txt",
            "Content-Type": "" if case == "empty_content_type" else "text/plain",
        },
        b"fixture",
        b"certificate",
    )
    assert response.status == (400 if case == "empty_content_type" else 413)
    if case != "lowered_during_storage":
        assert len(s.objects) == initial_files
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT status FROM m_external_history_files_v1 WHERE job_uuid=%s",
                (job,),
            ).fetchone()["status"]
            == "missing"
        )


@pytest.mark.parametrize(
    "name, status",
    [("", 200), ("n" * 256, 422), ("n" * 255, 200), ("文" * 255, 200)],
    ids=["empty", "too-long", "maximum-ascii", "maximum-unicode"],
)
def test_uploaded_filename_obeys_domain_before_storage(history_setup, name, status):
    s = history_setup
    name_type = messenger_models.WorkspaceFile.properties.properties[
        "name"
    ].get_property_type()
    assert name_type.validate(name) == (status == 200)
    job = accept(s, envelope(s, count=1, content="[file](/user_uploads/1/file.txt)"))
    assert finish(s, job)["status"] == "waiting_files"
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job}", {}, b"", b"certificate"
    )
    requested = json.loads(response.body)["files"][0]["uuid"]
    initial_files = len(s.objects)
    response = s.ingress.handle(
        "PUT",
        f"{contract.PATH}/{job}/files/{requested}",
        {"X-File-Name": urllib.parse.quote(name), "Content-Type": "text/plain"},
        b"fixture",
        b"certificate",
    )
    assert response.status == status
    if status != 200:
        assert len(s.objects) == initial_files
        with s.session_factory() as session:
            assert (
                session.execute(
                    "SELECT status FROM m_external_history_files_v1 WHERE job_uuid=%s AND uuid=%s",
                    (job, requested),
                ).fetchone()["status"]
                == "missing"
            )
    else:
        assert finish(s, job)["status"] == "complete"
        with s.session_factory() as session:
            row = session.execute(
                "SELECT uuid, name FROM m_workspace_files WHERE project_id=%s",
                (s.project,),
            ).fetchone()
            model = messenger_models.WorkspaceFile.objects.get_one(
                filters={
                    "uuid": dm_filters.EQ(row["uuid"]),
                    "project_id": dm_filters.EQ(s.project),
                },
                session=session,
            )
            assert model.name == name
        response = s.api.get(f"/v1/files/{row['uuid']}")
        assert response.status_code == 200, response.text
        assert response.json()["name"] == name


@pytest.mark.parametrize(
    "content",
    [
        "@" + "9" * 9000,
        "[link](#narrow/dm/" + "9" * 9000 + ")",
        "[link](#narrow/channel/42/near/" + "9" * 9000 + ")",
    ],
    ids=["plain-text", "direct-link", "message-link"],
)
def test_oversized_numeric_links_do_not_stall_import(history_setup, content):
    s = history_setup
    body = envelope(s, count=1)
    message = body["batch"]["messages"][0]
    message["content"] = content
    message["hash"] = contract.digest(
        {key: value for key, value in message.items() if key != "hash"}
    )
    body["batch"]["hash"] = contract.digest(
        {key: value for key, value in body["batch"].items() if key != "hash"}
    )
    result = finish(s, accept(s, body))
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 1


@pytest.mark.parametrize("converted", [False, True])
def test_empty_markdown_is_rejected_before_message_insertion(
    history_setup, monkeypatch, converted
):
    s = history_setup
    body = envelope(s, count=1, content="Source" if converted else "")
    if converted:
        monkeypatch.setattr(
            zulip_markdown, "convert_markdown", lambda *a, **kw: ("", False)
        )
        job = accept(s, body)
        for _ in range(10):
            with s.session_factory() as session:
                session.execute(
                    "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                    (job,),
                )
            s.worker.run_once()
        with s.session_factory() as session:
            result = repository.get_job(session, s.identity, job)
            assert result["status"] == "failed"
            assert result["safe_error"] == "invalid_history_message_content"
    else:
        response = s.ingress.handle(
            "POST", contract.PATH, {}, contract.encode(body), b"certificate"
        )
        assert response.status == 422
        assert not s.objects
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


def test_malformed_preserved_file_urn_does_not_stall_or_change_payload(history_setup):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    content = "Literal urn:file:" + "a" * 36
    with s.session_factory() as session:
        session.execute(
            "UPDATE messenger_messages SET payload=%s::jsonb WHERE provider_realm_uuid=%s",
            (json.dumps({"kind": "markdown", "content": content}), s.realm),
        )
    result = finish(s, accept(s, envelope(s, count=1, generation=2)))
    assert result["status"] == "complete"
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT payload->>'content' AS content FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["content"]
            == content
        )


def test_preserved_file_message_does_not_skip_surrounding_batch_ids(history_setup):
    s = history_setup
    content = f"Existing [file](urn:file:{sys_uuid.uuid4()})"
    initial = envelope(s, count=1, content=content)
    message = initial["batch"]["messages"][0]
    message["id"] = 2
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    initial["batch"]["hash"] = contract.digest(
        {k: v for k, v in initial["batch"].items() if k != "hash"}
    )
    assert finish(s, accept(s, initial))["status"] == "complete"
    result = finish(s, accept(s, envelope(s, count=3, generation=2)))
    assert result["status"] == "complete"
    assert result["next_message_id"] == result["applied_messages"] == 3
    assert result["inserted_messages"] == 2
    assert result["skipped_messages"] == 1
    with s.session_factory() as session:
        messages = session.execute(
            "SELECT provider_message_id, payload FROM messenger_messages WHERE provider_realm_uuid=%s ORDER BY provider_message_id",
            (s.realm,),
        ).fetchall()
        assert [row["provider_message_id"] for row in messages] == ["1", "2", "3"]
        assert messages[1]["payload"]["content"] == content


@pytest.mark.parametrize("root", ["job", "scope"])
def test_history_root_deletion_cascades_hashes_and_preserves_messages(
    history_setup, root
):
    s = history_setup
    job = accept(s, envelope(s, count=1))
    assert finish(s, job)["status"] == "complete"
    with s.session_factory() as session:
        if root == "job":
            session.execute(
                "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s", (job,)
            )
        else:
            session.execute(
                "DELETE FROM m_external_history_scopes_v1 WHERE bridge_uuid=%s AND project_uuid=%s AND provider_realm_uuid=%s",
                (s.bridge, s.project, s.realm),
            )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_external_history_message_hashes_v1 WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 1
        )


def _add_history_observers(s, body, count):
    for provider_id in range(8, 7 + count):
        peer, account, chat = [sys_uuid.uuid4() for _ in range(3)]
        conftest.seed_user_stream_binding(s.db, s.project, s.stream, peer)
        with s.session_factory() as session:
            session.execute(
                """INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings, provider_realm_uuid, provider_owner_user_id)
                VALUES (%s, %s, 'zulip', '{"server_url":"https://zulip.example.test"}'::jsonb, %s, %s)""",
                (account, peer, s.realm, str(provider_id)),
            )
            session.execute(
                """INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider, provider_chat_id, source,
                 display_name, selected, project_id, history_depth, projection_stream_uuid, status)
                SELECT %s, %s, %s, provider, provider_chat_id, source, display_name, selected,
                       project_id, history_depth, projection_stream_uuid, status FROM m_external_chats_v2 WHERE uuid=%s""",
                (chat, account, peer, s.chat),
            )
            session.execute(
                """INSERT INTO m_external_provider_identity_links_v1
                (provider, provider_realm_uuid, provider_user_id, workspace_user_uuid, link_kind)
                VALUES ('zulip', %s, %s, %s, 'verified_account_owner')
                ON CONFLICT (provider, provider_realm_uuid, provider_user_id) DO UPDATE
                    SET workspace_user_uuid=EXCLUDED.workspace_user_uuid, link_kind=EXCLUDED.link_kind""",
                (s.realm, str(provider_id), peer),
            )
            for resource in [
                {
                    "resource_type": "external_account",
                    "uuid": str(account),
                    "generation": 1,
                    "synchronization_enabled": True,
                },
                {
                    "resource_type": "external_chat_assignment",
                    "uuid": str(chat),
                    "generation": 1,
                    "selected": True,
                    "history_depth": "all",
                    "provider_chat": {"provider_chat_key": "channel:42"},
                },
            ]:
                sql_state.append_upsert(session, s.bridge, "zulip", resource)
        body["sources"].append(
            dict(
                account_uuid=str(account),
                account_generation=1,
                chat_uuid=str(chat),
                assignment_generation=1,
            )
        )
        if provider_id != 8:
            body["batch"]["users"].append(
                dict(id=provider_id, name=f"Observer {provider_id}")
            )
        body["batch"]["messages"][0]["access"].append(
            dict(user_id=provider_id, read=False, starred=False)
        )
    body["sources"].sort(key=lambda source: source["chat_uuid"])
    message = body["batch"]["messages"][0]
    message["hash"] = contract.digest({k: v for k, v in message.items() if k != "hash"})
    body["batch"]["hash"] = contract.digest(
        {k: v for k, v in body["batch"].items() if k != "hash"}
    )


def test_many_sources_authorize_without_analyze_and_require_complete_generations(
    history_setup,
):
    s = history_setup
    body = envelope(s, count=1)
    _add_history_observers(s, body, 128)
    # Exercise fresh inserts under the real 500 ms statement limit, without
    # refreshing planner statistics to hide repeated desired-resource scans.
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        count = session.execute(
            "SELECT count(*) AS n FROM messenger_user_message_bindings WHERE project_id=%s",
            (s.project,),
        ).fetchone()["n"]
        assert count == 128

    body["generation"] += 1
    omitted = body["sources"].pop()
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(body), b"certificate"
    )
    assert response.status == 409
    assert json.loads(response.body)["error"] == "history_sources_changed"
    body["sources"].append(omitted)
    body["sources"][0]["assignment_generation"] += 1
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(body), b"certificate"
    )
    assert response.status == 409
    assert json.loads(response.body)["error"] == "history_sources_changed"


def test_preserved_file_acl_pages_resume_rollback_and_recheck_changed_payload(
    history_setup, monkeypatch
):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    files = [str(sys_uuid.uuid4()) for _ in range(301)]
    with s.session_factory() as session:
        session.execute(
            """INSERT INTO m_workspace_files
            (uuid, project_id, name, description, user_uuid, stream_uuid, content_type, size_bytes, hash, storage_type, storage_id, storage_object_id)
            SELECT file_uuid, %s, 'file.txt', '', %s, %s, 'text/plain', 1, %s, 's3', 'test', file_uuid::text
            FROM unnest(%s::uuid[]) AS files(file_uuid)""",
            (s.project, s.owner, s.stream, "a" * 64, files),
        )
        session.execute(
            "UPDATE messenger_messages SET payload=%s::jsonb, updated_at=now() WHERE provider_realm_uuid=%s",
            (
                json.dumps(
                    {
                        "kind": "markdown",
                        "content": " ".join(
                            f"[file](urn:file:{value})" for value in files[:-1]
                        ),
                    }
                ),
                s.realm,
            ),
        )
    body = envelope(s, count=1, generation=2)
    _add_history_observers(s, body, 128)
    job_uuid = accept(s, body)
    original_write = writer._write_files
    page_sizes = []
    failed = False

    def write_files(session, job, part):
        nonlocal failed
        if part.file_access_progress is not None:
            page_sizes.append(len(part.file_access))
            assert len(part.file_access) <= job["part_size"] * 10 <= 1000
            assert len(part.source_ids) == 1
            assert not part.files
            original_write(session, job, part)
            if not failed:
                failed = True
                raise agents.WriteBudgetExceeded()
        else:
            original_write(session, job, part)

    monkeypatch.setattr(writer, "_write_files", write_files)

    def step():
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
        s.worker.run_once()
        with s.session_factory() as session:
            return repository.get_job(session, s.identity, job_uuid)

    for _ in range(200):
        job = step()
        if failed:
            assert job["file_access_progress"] is None
            assert job["next_message_id"] == job["applied_messages"] == 0
            with s.session_factory() as session:
                assert (
                    session.execute(
                        "SELECT count(*) AS n FROM m_workspace_file_accesses WHERE project_id=%s",
                        (s.project,),
                    ).fetchone()["n"]
                    == 0
                )
            break
    else:
        pytest.fail("ACL page was not reached")
    # A fresh worker has no in-memory progress; successful pages persist only ACL work.
    s.worker = agents.HistoryImportWorker(s.session_factory)
    for _ in range(200):
        job = step()
        progress = job["file_access_progress"]
        assert job["next_message_id"] == job["applied_messages"] == 0
        if progress and progress["offset"] == 300 * 128:
            break
    else:
        pytest.fail("ACL pages did not finish")
    old_fingerprint = progress["fingerprint"]
    content = " ".join(f"[file](urn:file:{value})" for value in files)
    with s.session_factory() as session:
        session.execute(
            "UPDATE messenger_messages SET payload=%s::jsonb, updated_at=now() WHERE provider_realm_uuid=%s",
            (json.dumps({"kind": "markdown", "content": content}), s.realm),
        )
        session.execute(
            "UPDATE m_external_history_imports_v1 SET part_size=1 WHERE uuid=%s",
            (job_uuid,),
        )
    job = step()
    assert job["file_access_progress"]["fingerprint"] != old_fingerprint
    assert job["file_access_progress"]["offset"] == 10
    assert job["next_message_id"] == 0
    assert finish(s, job_uuid)["status"] == "complete"
    with s.session_factory() as session:
        result = repository.get_job(session, s.identity, job_uuid)
        assert result["file_access_progress"] is None
        assert result["applied_messages"] == result["skipped_messages"] == 1
        assert result["inserted_messages"] == 0
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_workspace_file_accesses WHERE project_id=%s",
                (s.project,),
            ).fetchone()["n"]
            == 301 * 128
        )
        assert (
            session.execute(
                "SELECT payload->>'content' AS content FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["content"]
            == content
        )
    assert max(page_sizes) == 1000
    assert 10 in page_sizes


def _rehash_history(body):
    for message in body["batch"]["messages"]:
        message["hash"] = contract.digest(
            {key: value for key, value in message.items() if key != "hash"}
        )
    body["batch"]["hash"] = contract.digest(
        {key: value for key, value in body["batch"].items() if key != "hash"}
    )


def _history_status(s, job_uuid):
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job_uuid}", {}, b"", b"certificate"
    )
    assert response.status == 200, response.body
    return json.loads(response.body)


def _upload_history_file(s, job_uuid, request_uuid):
    response = s.ingress.handle(
        "PUT",
        f"{contract.PATH}/{job_uuid}/files/{request_uuid}",
        {"X-File-Name": "sample.txt", "Content-Type": "text/plain"},
        b"fixture",
        b"certificate",
    )
    assert response.status == 200, response.body


def _pin_history_part(s, job_uuid, monkeypatch, size):
    original = s.worker.write

    def write(*args):
        result = original(*args)
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET part_size=%s WHERE uuid=%s",
                (size, job_uuid),
            )
        return result

    monkeypatch.setattr(s.worker, "write", write)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET part_size=%s WHERE uuid=%s",
            (size, job_uuid),
        )


@pytest.mark.parametrize("state", ["existing", "deleted", "purged"])
def test_existing_history_never_discovers_obsolete_source_files(history_setup, state):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    with s.session_factory() as session:
        if state != "existing":
            session.execute(
                "UPDATE messenger_messages SET deleted_at=now() WHERE provider_realm_uuid=%s",
                (s.realm,),
            )
        if state == "purged":
            session.execute(
                "DELETE FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            )
    replay = accept(
        s,
        envelope(
            s,
            count=1,
            generation=2,
            content="[obsolete](/user_uploads/old/missing.txt) [invalid](/user_uploads/../file)",
        ),
    )
    result = finish(s, replay)
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 0
    assert result["active_file_uuids"] == []
    assert s.worker.cached_file_accounts is None
    assert _history_status(s, replay)["files"] == []
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_external_history_files_v1 WHERE job_uuid=%s",
                (replay,),
            ).fetchone()["n"]
            == 0
        )
        row = session.execute(
            "SELECT payload FROM messenger_messages WHERE provider_realm_uuid=%s",
            (s.realm,),
        ).fetchone()
        assert (row is None) == (state == "purged")
        if row:
            assert row["payload"]["content"] == "Historical message"


def test_mixed_history_discovers_shared_file_only_for_missing_message(history_setup):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    body = envelope(
        s, count=2, generation=2, content="[shared](/user_uploads/1/shared.txt)"
    )
    body["batch"]["messages"][0]["content"] += " [invalid](/user_uploads/../obsolete)"
    _rehash_history(body)
    job_uuid = accept(s, body)
    assert finish(s, job_uuid)["status"] == "waiting_files"
    requests = _history_status(s, job_uuid)["files"]
    assert len(requests) == 1
    assert requests[0]["source_path"] == "/user_uploads/1/shared.txt"
    _upload_history_file(s, job_uuid, requests[0]["uuid"])
    assert finish(s, job_uuid)["inserted_messages"] == 1
    with s.session_factory() as session:
        messages = session.execute(
            "SELECT payload FROM messenger_messages WHERE provider_realm_uuid=%s ORDER BY provider_message_id",
            (s.realm,),
        ).fetchall()
        assert messages[0]["payload"]["content"] == "Historical message"
        assert "urn:file:" in messages[1]["payload"]["content"]


def _create_history_live_message(s):
    public_uuid = sys_uuid.uuid4()
    with s.session_factory() as session:
        messenger_helpers.create_workspace_user_message(
            uuid=public_uuid,
            project_id=s.project,
            user_uuid=s.owner,
            stream_uuid=s.stream,
            topic_uuid=s.topic,
            payload=message_payloads.MarkdownPayload(content="Live message"),
            session=session,
        )
        updated = session.execute(
            """UPDATE messenger_messages SET provider_realm_uuid=%s, provider_message_id='1'
               WHERE uuid=(SELECT message_uuid FROM messenger_message_placements
                   WHERE project_id=%s AND COALESCE(legacy_public_uuid, uuid)=%s)
               RETURNING uuid""",
            (s.realm, s.project, public_uuid),
        ).fetchall()
        assert len(updated) == 1
    return updated[0]["uuid"]


def test_live_arrival_ignores_old_pending_attachment_and_late_upload(history_setup):
    s = history_setup
    job_uuid = accept(
        s, envelope(s, count=1, content="[obsolete](/user_uploads/1/old.txt)")
    )
    assert finish(s, job_uuid)["status"] == "waiting_files"
    request_uuid = _history_status(s, job_uuid)["files"][0]["uuid"]
    _create_history_live_message(s)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
            (job_uuid,),
        )
    # The scheduled waiter rechecks canonical existence before requesting files.
    assert s.worker.run_once()
    completed = finish(s, job_uuid)
    assert completed["status"] == "complete"
    assert completed["inserted_messages"] == 0
    assert completed["active_file_uuids"] == []
    assert _history_status(s, job_uuid)["files"] == []
    _upload_history_file(s, job_uuid, request_uuid)
    with s.session_factory() as session:
        current = repository.get_job(session, s.identity, job_uuid)
        assert current["status"] == "complete"
        assert current["next_message_id"] == completed["next_message_id"] == 1
        assert (
            session.execute(
                "SELECT payload->>'content' AS content FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["content"]
            == "Live message"
        )


@pytest.mark.parametrize("purge", [False, True])
def test_canonical_deletion_after_preparation_does_not_request_source_files(
    history_setup,
    monkeypatch,
    purge,
):
    s = history_setup
    assert finish(s, accept(s, envelope(s, count=1)))["status"] == "complete"
    job_uuid = accept(
        s, envelope(s, count=1, generation=2, content="[old](/user_uploads/1/old.txt)")
    )
    original = writer.write_part
    deleted = False

    def write(*args):
        nonlocal deleted
        if not deleted:
            deleted = True
            with s.session_factory() as session:
                session.execute(
                    "UPDATE messenger_messages SET deleted_at=now() WHERE provider_realm_uuid=%s",
                    (s.realm,),
                )
                if purge:
                    session.execute(
                        "DELETE FROM messenger_messages WHERE provider_realm_uuid=%s",
                        (s.realm,),
                    )
        return original(*args)

    monkeypatch.setattr(writer, "write_part", write)
    for _ in range(30):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
            job = repository.get_job(session, s.identity, job_uuid)
        if job["status"] == "complete":
            break
        assert s.worker.run_once()
    assert deleted
    assert job["status"] == "complete"
    assert job["attempts"] >= 1
    assert job["inserted_messages"] == 0
    assert _history_status(s, job_uuid)["files"] == []
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_external_history_files_v1 WHERE job_uuid=%s",
                (job_uuid,),
            ).fetchone()["n"]
            == 0
        )


def test_shared_file_uses_later_observer_and_retries_old_unavailable_manifest(
    history_setup,
    monkeypatch,
):
    from workspace.history_import import preparation

    s = history_setup
    path = "/user_uploads/1/shared.txt"
    body = envelope(s, count=2, content=f"[shared]({path})")
    _add_history_observers(s, body, 2)
    body["batch"]["messages"][0]["access"] = [dict(user_id=7, read=True, starred=False)]
    body["batch"]["messages"][1]["access"] = [dict(user_id=8, read=False, starred=True)]
    _rehash_history(body)
    job_uuid = accept(s, body)
    file_uuid = sys_uuid.uuid5(job_uuid, "file:" + path)
    obsolete = sys_uuid.uuid4()
    with s.session_factory() as session:
        session.execute(
            """INSERT INTO m_external_history_files_v1(job_uuid,uuid,source_path,account_uuids,status)
               VALUES (%s,%s,%s,%s::jsonb,'unavailable'),(%s,%s,'/user_uploads/obsolete',%s::jsonb,'missing')""",
            (
                job_uuid,
                file_uuid,
                path,
                json.dumps([str(s.account)]),
                job_uuid,
                obsolete,
                json.dumps([str(s.account)]),
            ),
        )
    _pin_history_part(s, job_uuid, monkeypatch, 1)
    scans = []
    original = preparation.existing_message_ids

    def scan(session, batch, messages):
        scans.append(len(messages))
        return original(session, batch, messages)

    monkeypatch.setattr(preparation, "existing_message_ids", scan)
    assert finish(s, job_uuid)["status"] == "waiting_files"
    requests = _history_status(s, job_uuid)["files"]
    assert len(requests) == 1
    assert requests[0]["uuid"] == str(file_uuid)
    assert set(requests[0]["account_uuids"]) == {
        source["account_uuid"] for source in body["sources"]
    }
    _upload_history_file(s, job_uuid, file_uuid)
    with s.session_factory() as session:
        assert repository.get_job(session, s.identity, job_uuid)["status"] == "pending"
    assert finish(s, job_uuid)["status"] == "complete"
    assert scans == [2, 1]  # One shared-path scan and one locked staging recheck.
    assert _history_status(s, job_uuid)["files"] == []
    with s.session_factory() as session:
        messages = session.execute(
            "SELECT payload FROM messenger_messages WHERE provider_realm_uuid=%s",
            (s.realm,),
        ).fetchall()
        assert len(messages) == 2
        assert all("urn:file:" in row["payload"]["content"] for row in messages)
        assert (
            session.execute(
                "SELECT status FROM m_external_history_files_v1 WHERE job_uuid=%s AND uuid=%s",
                (job_uuid, obsolete),
            ).fetchone()["status"]
            == "missing"
        )


@pytest.mark.parametrize("part_size", [1, 100])
def test_dense_source_attachment_staging_is_bounded_and_resumable(
    history_setup,
    monkeypatch,
    part_size,
):
    s = history_setup
    total = 105 if part_size == 100 else 3
    body = envelope(
        s,
        count=1,
        content=" ".join(
            f"[file](/user_uploads/1/{index}.txt)" for index in range(total)
        ),
    )
    job_uuid = accept(s, body)
    _pin_history_part(s, job_uuid, monkeypatch, part_size)
    original = writer.write_missing_files
    staged = []

    def write(session, identity, job, data, absent_message_ids):
        staged.append(len(json.loads(data)))
        assert staged[-1] <= part_size
        return original(session, identity, job, data, absent_message_ids)

    monkeypatch.setattr(writer, "write_missing_files", write)
    transferred = set()
    for _ in range(total + 2):
        result = finish(s, job_uuid)
        if result["status"] == "complete":
            break
        assert result["status"] == "waiting_files"
        assert 0 < len(result["active_file_uuids"]) <= part_size
        # The active page is read from persisted job state by the API.
        for request in _history_status(s, job_uuid)["files"]:
            assert request["uuid"] not in transferred
            transferred.add(request["uuid"])
            _upload_history_file(s, job_uuid, request["uuid"])
    assert result["status"] == "complete"
    assert len(transferred) == total
    assert staged == ([1] * total if part_size == 1 else [100, 5])
    assert result["active_file_uuids"] == []


@pytest.mark.parametrize("state", ["existing", "deleted", "purged"])
def test_live_arrival_before_file_staging_rolls_back_request_publication(
    history_setup,
    monkeypatch,
    state,
):
    s = history_setup
    job_uuid = accept(
        s, envelope(s, count=1, content="[obsolete](/user_uploads/1/old.txt)")
    )
    original = s.worker.required_files
    arrived = False

    def discover(*args):
        nonlocal arrived
        requests = original(*args)
        if requests and not arrived:
            # Both absence reads have completed, but staging has not begun.
            assert s.observer.depth == 0
            arrived = True
            canonical_uuid = _create_history_live_message(s)
            with s.session_factory() as other:
                if state != "existing":
                    other.execute(
                        "UPDATE messenger_messages SET deleted_at=now() WHERE uuid=%s",
                        (canonical_uuid,),
                    )
                if state == "purged":
                    other.execute(
                        "DELETE FROM messenger_messages WHERE uuid=%s",
                        (canonical_uuid,),
                    )
        return requests

    monkeypatch.setattr(s.worker, "required_files", discover)
    for _ in range(30):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
            job = repository.get_job(session, s.identity, job_uuid)
            assert job["active_file_uuids"] == []
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM m_external_history_files_v1 WHERE job_uuid=%s",
                    (job_uuid,),
                ).fetchone()["n"]
                == 0
            )
        assert _history_status(s, job_uuid)["files"] == []
        if job["status"] == "complete":
            break
        assert s.worker.run_once()
    assert arrived
    assert job["status"] == "complete"
    assert job["attempts"] == 1
    assert job["inserted_messages"] == 0
    assert job["applied_messages"] == 1


def test_file_staging_holds_provider_identity_fence_until_publication(
    history_setup,
    monkeypatch,
):
    s = history_setup
    job_uuid = accept(
        s, envelope(s, count=1, content="[file](/user_uploads/1/file.txt)")
    )
    original_lock = writer._lock_provider_messages
    original_stage = writer.write_missing_files
    staging = False
    checked = False

    def lock(session, job, source_ids):
        nonlocal checked
        original_lock(session, job, source_ids)
        if staging:
            assert source_ids == [1]
            with s.db.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"provider-message-identity-v1:{s.realm}:1",),
                )
                assert cursor.fetchone()[0] is False
            checked = True

    def stage(*args):
        nonlocal staging
        staging = True
        try:
            return original_stage(*args)
        finally:
            staging = False

    monkeypatch.setattr(writer, "_lock_provider_messages", lock)
    monkeypatch.setattr(writer, "write_missing_files", stage)
    assert finish(s, job_uuid)["status"] == "waiting_files"
    assert checked
    assert len(_history_status(s, job_uuid)["files"]) == 1
    # Network transfer starts after publication, with no identity lock retained.
    with s.db.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"provider-message-identity-v1:{s.realm}:1",),
        )
        assert cursor.fetchone()[0] is True


@pytest.mark.parametrize("state", ["existing", "deleted", "purged"])
def test_reduced_depth_existing_messages_and_tombstones_advance_checkpoint(
    history_setup,
    state,
):
    s = history_setup
    body = envelope(s, count=2)
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    old, recent = body["batch"]["messages"]
    old["sent_at"] = now - 30 * 86400
    old["content"] = f"Preserved [file](urn:file:{sys_uuid.uuid4()})"
    recent["sent_at"] = now
    _rehash_history(body)
    assert finish(s, accept(s, body))["inserted_messages"] == 2
    with s.session_factory() as session:
        if state != "existing":
            session.execute(
                "UPDATE messenger_messages SET deleted_at=now() WHERE provider_realm_uuid=%s AND provider_message_id='1'",
                (s.realm,),
            )
        if state == "purged":
            session.execute(
                "DELETE FROM messenger_messages WHERE provider_realm_uuid=%s AND provider_message_id='1'",
                (s.realm,),
            )
        session.execute(
            "UPDATE m_external_chats_v2 SET history_depth='7_days' WHERE uuid=%s",
            (s.chat,),
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            {
                "resource_type": "external_chat_assignment",
                "uuid": str(s.chat),
                "generation": 2,
                "selected": True,
                "history_depth": "7_days",
                "provider_chat": {"provider_chat_key": "channel:42"},
            },
        )
    body["generation"] = 2
    body["sources"][0]["assignment_generation"] = 2
    old["topic"] = "Removed historical topic"
    old["content"] = "[invalid](/user_uploads/../obsolete)"
    _rehash_history(body)
    job_uuid = accept(s, body)
    result = finish(s, job_uuid)
    assert result["status"] == "complete"
    assert result["next_message_id"] == result["applied_messages"] == 2
    assert result["inserted_messages"] == result["attempts"] == 0
    assert _history_status(s, job_uuid)["files"] == []
    with s.session_factory() as session:
        assert session.execute(
            "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s AND deleted_at IS NULL",
            (s.realm,),
        ).fetchone()["n"] == (2 if state == "existing" else 1)
        assert (
            session.execute(
                "SELECT count(*) AS n FROM m_external_history_files_v1 WHERE job_uuid=%s",
                (job_uuid,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("status", ["pending", "complete", "failed", "superseded"])
@pytest.mark.parametrize("delete_scope", [False, True])
def test_polling_job_deleted_after_read_returns_not_found(
    history_setup, monkeypatch, status, delete_scope
):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET status=%s WHERE uuid=%s",
            (status, job_uuid),
        )
    original = repository.get_job
    deleted = False

    def get_job(session, identity, uuid, **kwargs):
        nonlocal deleted
        job = original(session, identity, uuid, **kwargs)
        if not deleted:
            deleted = True
            with s.db.cursor() as cursor:
                if delete_scope:
                    cursor.execute(
                        "DELETE FROM m_external_history_scopes_v1 WHERE bridge_uuid=%s AND project_uuid=%s AND provider_realm_uuid=%s",
                        (s.bridge, s.project, s.realm),
                    )
                else:
                    cursor.execute(
                        "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s",
                        (job_uuid,),
                    )
                assert cursor.rowcount == 1
        return job

    monkeypatch.setattr(repository, "get_job", get_job)
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job_uuid}", {}, b"", b"certificate"
    )
    assert deleted
    if status == "pending" and delete_scope:
        # Active polls still validate their scope before acquiring the job lock.
        assert response.status == 409
        assert json.loads(response.body) == {"error": "history_generation_superseded"}
    else:
        assert response.status == 404
        assert json.loads(response.body) == {"error": "history_import_not_found"}


@pytest.mark.parametrize("status", ["complete", "failed", "superseded"])
def test_terminal_poll_holds_job_against_delete_until_receipt_is_built(
    history_setup, monkeypatch, status
):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET status=%s WHERE uuid=%s",
            (status, job_uuid),
        )
    original = repository.public_status
    blocked = False

    def public_status(session, job):
        nonlocal blocked
        try:
            with s.db.transaction():
                s.db.execute("SET LOCAL lock_timeout='50ms'")
                s.db.execute(
                    "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s",
                    (job_uuid,),
                )
        except Exception as error:
            assert getattr(error, "sqlstate", None) == "55P03"
            blocked = True
        return original(session, job)

    monkeypatch.setattr(repository, "public_status", public_status)
    response = s.ingress.handle(
        "GET", f"{contract.PATH}/{job_uuid}", {}, b"", b"certificate"
    )
    assert response.status == 200, response.body
    assert json.loads(response.body)["status"] == status
    assert blocked
    assert (
        s.db.execute(
            "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s", (job_uuid,)
        ).rowcount
        == 1
    )


@pytest.mark.parametrize("status", ["pending", "complete", "failed", "superseded"])
@pytest.mark.parametrize("boundary", ["fast-job", "fast-scope", "register-job"])
def test_post_retry_rechecks_job_after_duplicate_lookup(
    history_setup, monkeypatch, status, boundary
):
    s = history_setup
    body = envelope(s, count=1)
    job_uuid = accept(s, body)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET status=%s WHERE uuid=%s",
            (status, job_uuid),
        )
    original = repository.existing_job
    calls = 0
    deleted = False

    def existing_job(session, identity, batch):
        nonlocal calls, deleted
        calls += 1
        if boundary == "register-job" and calls == 1:
            # A concurrent POST can register the job while this request stores
            # its body. Exercise the duplicate lookup in register as well.
            return None
        job = original(session, identity, batch)
        assert job["uuid"] == job_uuid
        with s.db.cursor() as cursor:
            if boundary == "fast-scope":
                cursor.execute(
                    "DELETE FROM m_external_history_scopes_v1 WHERE bridge_uuid=%s AND project_uuid=%s AND provider_realm_uuid=%s",
                    (s.bridge, s.project, s.realm),
                )
            else:
                cursor.execute(
                    "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s",
                    (job_uuid,),
                )
            assert cursor.rowcount == 1
        deleted = True
        return job

    monkeypatch.setattr(repository, "existing_job", existing_job)
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(body), b"certificate"
    )
    assert deleted and calls == (2 if boundary == "register-job" else 1)
    assert response.status == 404
    assert json.loads(response.body) == {"error": "history_import_not_found"}


@pytest.mark.parametrize("boundary", ["fast", "register"])
@pytest.mark.parametrize("delete_scope", [False, True])
def test_post_receipt_holds_job_and_scope_until_response_is_built(
    history_setup, monkeypatch, boundary, delete_scope
):
    s = history_setup
    body = envelope(s, count=1)
    accept(s, body)
    if boundary == "register":
        original_existing = repository.existing_job
        calls = 0

        def existing_job(*args):
            nonlocal calls
            calls += 1
            return None if calls == 1 else original_existing(*args)

        monkeypatch.setattr(repository, "existing_job", existing_job)
    original_status = repository.public_status
    blocked = False
    delete_query = (
        "DELETE FROM m_external_history_scopes_v1 WHERE bridge_uuid=%s AND project_uuid=%s AND provider_realm_uuid=%s"
        if delete_scope
        else "DELETE FROM m_external_history_imports_v1 WHERE uuid=%s"
    )
    job_uuid = contract.Batch.parse(body).job_uuid(s.bridge, 1)
    delete_args = (s.bridge, s.project, s.realm) if delete_scope else (job_uuid,)

    def public_status(session, job):
        nonlocal blocked
        try:
            with s.db.transaction():
                s.db.execute("SET LOCAL lock_timeout='50ms'")
                s.db.execute(delete_query, delete_args)
        except Exception as error:
            assert getattr(error, "sqlstate", None) == "55P03"
            blocked = True
        return original_status(session, job)

    monkeypatch.setattr(repository, "public_status", public_status)
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(body), b"certificate"
    )
    assert response.status == (200 if boundary == "fast" else 202), response.body
    assert json.loads(response.body)["uuid"] == str(job_uuid)
    assert blocked
    assert s.db.execute(delete_query, delete_args).rowcount == 1


def _history_range(s, offset):
    body = envelope(s, count=1)
    body["batch"]["from_id"] += offset
    body["batch"]["to_id"] += offset
    body["batch"]["messages"][0]["id"] += offset
    _rehash_history(body)
    return body


def test_consecutive_processing_failures_free_all_admission_slots(
    history_setup,
    monkeypatch,
):
    s = history_setup
    jobs = [accept(s, _history_range(s, 5000 * index)) for index in range(8)]
    ninth = _history_range(s, 40000)
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(ninth), b"certificate"
    )
    assert response.status == 429
    failures = [
        KeyError("broken preparation"),
        writer.PreparationChanged(),
        contract.ImportError("history_topic_mapping_pending", 409),
        pg_errors.OperationalError("invalid connection option"),
    ]
    original = s.worker.process

    def fail(identity, job):
        raise failures[jobs.index(job["uuid"]) % len(failures)]

    monkeypatch.setattr(s.worker, "process", fail)
    for _ in range(8 * repository.MAX_CONSECUTIVE_ERRORS):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=ANY(%s::uuid[])",
                (jobs,),
            )
        assert s.worker.run_once()
    with s.session_factory() as session:
        results = [
            repository.get_job(session, s.identity, job_uuid) for job_uuid in jobs
        ]
    assert all(job["status"] == "failed" for job in results)
    assert all(
        job["consecutive_errors"] == repository.MAX_CONSECUTIVE_ERRORS
        for job in results
    )
    assert all(job["attempts"] == repository.MAX_CONSECUTIVE_ERRORS for job in results)
    assert all(_history_status(s, job_uuid)["safe_error"] for job_uuid in jobs)
    monkeypatch.setattr(s.worker, "process", original)
    assert finish(s, accept(s, ninth))["status"] == "complete"


def test_message_checkpoint_resets_streak_but_preserves_lifetime_attempts(
    history_setup,
    monkeypatch,
):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=2))
    _pin_history_part(s, job_uuid, monkeypatch, 1)
    for _ in range(30):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
            job = repository.get_job(session, s.identity, job_uuid)
        if job["next_message_id"] == 1:
            break
        assert s.worker.run_once()
    assert job["next_message_id"] == 1
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=%s, attempts=1000 WHERE uuid=%s",
            (repository.MAX_CONSECUTIVE_ERRORS - 1, job_uuid),
        )
    original = writer.write_part
    observed = False

    def write(session, identity, job, part):
        nonlocal observed
        assert job["consecutive_errors"] == repository.MAX_CONSECUTIVE_ERRORS - 1
        result = original(session, identity, job, part)
        persisted = repository.get_job(session, identity, job_uuid)
        assert persisted["next_message_id"] == 2
        assert persisted["consecutive_errors"] == 0
        assert persisted["attempts"] == 1000
        observed = True
        return result

    monkeypatch.setattr(writer, "write_part", write)
    result = finish(s, job_uuid)
    assert result["status"] == "complete"
    assert result["consecutive_errors"] == 0
    assert result["attempts"] == 1000
    assert observed


@pytest.mark.parametrize(
    "kind", ["contention", "write-budget", "lease", "provider-pause"]
)
def test_expected_yields_do_not_exhaust_processing_failure_budget(
    history_setup,
    monkeypatch,
    kind,
):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    if kind == "contention":
        error = RuntimeError("database contention")
        error.sqlstate = "55P03"
    elif kind == "write-budget":
        error = agents.WriteBudgetExceeded()
    else:
        error = contract.ImportError(
            "history_import_lease_expired"
            if kind == "lease"
            else "history_provider_paused",
            409,
        )
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=%s WHERE uuid=%s",
            (repository.MAX_CONSECUTIVE_ERRORS - 1, job_uuid),
        )
    original = s.worker.process

    def yield_work(identity, job):
        raise error

    monkeypatch.setattr(s.worker, "process", yield_work)
    for _ in range(repository.MAX_CONSECUTIVE_ERRORS + 5):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
        assert s.worker.run_once()
    with s.session_factory() as session:
        job = repository.get_job(session, s.identity, job_uuid)
        assert job["status"] == "pending"
        assert job["consecutive_errors"] == repository.MAX_CONSECUTIVE_ERRORS - 1
        assert job["attempts"] == repository.MAX_CONSECUTIVE_ERRORS + 5
    monkeypatch.setattr(s.worker, "process", original)
    assert finish(s, job_uuid)["status"] == "complete"


def test_repeated_missing_file_poll_preserves_processing_failure_streak(history_setup):
    s = history_setup
    job_uuid = accept(
        s, envelope(s, count=1, content="[file](/user_uploads/1/pending.txt)")
    )
    waiting = finish(s, job_uuid)
    assert waiting["status"] == "waiting_files"
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=%s, available_at=now() WHERE uuid=%s",
            (repository.MAX_CONSECUTIVE_ERRORS - 1, job_uuid),
        )
    assert s.worker.run_once()
    with s.session_factory() as session:
        current = repository.get_job(session, s.identity, job_uuid)
        assert current["status"] == "waiting_files"
        assert current["active_file_uuids"] == waiting["active_file_uuids"]
        assert current["next_message_id"] == waiting["next_message_id"] == 0
        assert current["consecutive_errors"] == repository.MAX_CONSECUTIVE_ERRORS - 1
    requests = _history_status(s, job_uuid)["files"]
    assert requests
    _upload_history_file(s, job_uuid, requests[0]["uuid"])
    with s.session_factory() as session:
        uploaded = repository.get_job(session, s.identity, job_uuid)
        assert uploaded["consecutive_errors"] == 0
        assert uploaded["next_message_id"] == 0
    assert finish(s, job_uuid)["status"] == "complete"


def _history_v1_ingress(s):
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_bridge_instances_v2 SET last_heartbeat_at=now() WHERE uuid=%s",
            (s.bridge,),
        )
        session.execute(
            "UPDATE m_external_chats_v2 SET revision=2, source=jsonb_set(jsonb_set(source, '{chat_type}', '\"channel\"'::jsonb), '{topics,0,is_default}', 'true'::jsonb) WHERE uuid=%s",
            (s.chat,),
        )
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(s.chat)},
            session=session,
        )
        desired = sql_state.external_chat_assignment_desired(chat, session=session)
        sql_state.append_upsert(session, s.bridge, "zulip", desired)
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(s.account),
        "external_chat_uuid": str(s.chat),
        "project_id": str(s.project),
        "provider_sequence": "1",
        "kind": "message.upsert",
        "payload": {
            "resource": {
                "uuid": str(sys_uuid.uuid4()),
                "stream_uuid": str(s.stream),
                "topic_uuid": str(s.topic),
                "user_uuid": str(s.owner),
                "provider_external_id": "1",
                "read": True,
                "provider_metadata": {
                    "provider_original_url": "https://zulip.example.test/#narrow/id/1",
                },
                "payload": {"kind": "markdown", "content": "Live V1 message"},
            }
        },
    }
    ingress = bridge_service.PrivateBridgeService(
        s.ingress.control_pki,
        s.ingress.control_state,
        None,
        provider_data_service=provider_service.ProviderDataService(
            apply_event=provider_event_apply.apply_event
        ),
    )
    return ingress, event, desired


def _v1_legacy_account(s, event):
    account, chat_uuid, owner = sys_uuid.uuid4(), sys_uuid.uuid4(), sys_uuid.uuid4()
    stream = sys_uuid.UUID(
        conftest.seed_user_stream(s.db, s.project, owner, "cassi legacy v1")
    )
    topic = sys_uuid.UUID(
        conftest.seed_stream_topic(
            s.db, s.project, stream, owner, "Import", is_default=True
        )
    )
    with s.session_factory() as session:
        session.execute(
            """INSERT INTO m_external_accounts_v2 (uuid, owner_user_uuid, provider, settings)
               SELECT %s, %s, provider, settings FROM m_external_accounts_v2 WHERE uuid=%s""",
            (account, owner, s.account),
        )
        session.execute(
            """INSERT INTO m_external_chats_v2
               (uuid, external_account_uuid, owner_user_uuid, provider, provider_chat_id, source,
                display_name, selected, project_id, history_depth, projection_stream_uuid, status, revision)
               SELECT %s, %s, %s, provider, provider_chat_id, jsonb_set(source, '{topics,0,topic_uuid}', to_jsonb(%s::text)),
                      display_name, selected, project_id, history_depth, %s, status, revision
               FROM m_external_chats_v2 WHERE uuid=%s""",
            (chat_uuid, account, owner, str(topic), stream, s.chat),
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            {
                "resource_type": "external_account",
                "uuid": str(account),
                "generation": 1,
                "synchronization_enabled": True,
            },
        )
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(chat_uuid)},
            session=session,
        )
        sql_state.append_upsert(
            session,
            s.bridge,
            "zulip",
            sql_state.external_chat_assignment_desired(chat, session=session),
        )
    event["external_account_uuid"] = str(account)
    event["external_chat_uuid"] = str(chat_uuid)
    event["payload"]["resource"]["user_uuid"] = str(owner)
    event["payload"]["resource"]["stream_uuid"] = str(stream)
    event["payload"]["resource"]["topic_uuid"] = str(topic)
    event["payload"]["resource"]["provider_metadata"]["provider_realm_uuid"] = str(
        s.realm
    ).upper()
    return account


@pytest.mark.parametrize("legacy_account", [False, True])
def test_v1_uncommitted_canonical_event_fences_history_file_publication(
    history_setup,
    monkeypatch,
    legacy_account,
):
    s = history_setup
    ingress, event, desired = _history_v1_ingress(s)
    if legacy_account:
        _v1_legacy_account(s, event)
    body = envelope(s, count=1, content="[obsolete](/user_uploads/1/obsolete.txt)")
    body["sources"][0]["assignment_generation"] = desired["generation"]
    job_uuid = accept(s, body)
    # Complete directory preparation before allowing an uncommitted live write.
    for _ in range(30):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
            job = repository.get_job(session, s.identity, job_uuid)
        if job["files_discovered"] and job["directory_cursor"] == len(
            body["batch"]["users"]
        ):
            break
        assert s.worker.run_once()
    assert job["next_message_id"] == 0
    applied = threading.Event()
    commit = threading.Event()
    errors = []

    def live():
        try:
            with s.session_factory() as session:
                response = ingress.handle(
                    "POST",
                    f"{provider_service.API_ROOT}/events",
                    {},
                    contract.encode({"events": [event]}),
                    b"certificate",
                    request_session=session,
                )
                assert response.status == 200, response.body
                assert json.loads(response.body)["results"][0]["status"] == "applied"
                assert (
                    session.execute(
                        "SELECT payload->>'content' AS content FROM messenger_messages WHERE provider_realm_uuid=%s AND provider_message_id='1'",
                        (s.realm,),
                    ).fetchone()["content"]
                    == "Live V1 message"
                )
                applied.set()
                assert commit.wait(timeout=10)
        except Exception as error:
            errors.append(error)
            applied.set()

    thread = threading.Thread(target=live)
    thread.start()
    try:
        assert applied.wait(timeout=10)
        assert not errors, errors
        with s.session_factory() as session:
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s",
                    (s.realm,),
                ).fetchone()["n"]
                == 0
            )
        staged = False
        original = writer.write_missing_files

        def stage(*args):
            nonlocal staged
            staged = True
            return original(*args)

        monkeypatch.setattr(writer, "write_missing_files", stage)
        assert s.worker.run_once()
        assert staged
        with s.session_factory() as session:
            blocked = repository.get_job(session, s.identity, job_uuid)
            assert blocked["safe_error"] == "history_database_contention"
            assert blocked["consecutive_errors"] == 0
            assert blocked["active_file_uuids"] == []
            assert (
                session.execute(
                    "SELECT count(*) AS n FROM m_external_history_files_v1 WHERE job_uuid=%s",
                    (job_uuid,),
                ).fetchone()["n"]
                == 0
            )
        assert _history_status(s, job_uuid)["files"] == []
    finally:
        commit.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors, errors
    result = finish(s, job_uuid)
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 0
    assert _history_status(s, job_uuid)["files"] == []
    with s.session_factory() as session:
        assert (
            session.execute(
                "SELECT payload->>'content' AS content FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["content"]
            == "Live V1 message"
        )


@pytest.mark.parametrize("metadata_realm", [False, True])
def test_v1_legacy_account_without_realm_identity(history_setup, metadata_realm):
    s = history_setup
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_accounts_v2 SET provider_realm_uuid=NULL, provider_owner_user_id=NULL WHERE uuid=%s",
            (s.account,),
        )
    ingress, event, _ = _history_v1_ingress(s)
    if metadata_realm:
        event["payload"]["resource"]["provider_metadata"]["provider_realm_uuid"] = str(
            s.realm
        )
    with s.session_factory() as session:
        response = ingress.handle(
            "POST",
            f"{provider_service.API_ROOT}/events",
            {},
            contract.encode({"events": [event]}),
            b"certificate",
            request_session=session,
        )
        assert response.status == 200, response.body
        message = session.execute(
            "SELECT provider_realm_uuid, provider_message_id FROM messenger_messages WHERE external_account_uuid=%s",
            (s.account,),
        ).fetchone()
        assert message is not None
        assert message["provider_realm_uuid"] == (s.realm if metadata_realm else None)
        assert message["provider_message_id"] == ("1" if metadata_realm else None)
        with s.db.transaction():
            available = s.db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS available",
                (f"provider-message-identity-v1:{s.realm}:1",),
            ).fetchone()[0]
            assert available is not metadata_realm


def test_v1_rechecks_account_realm_after_identity_fence(history_setup, monkeypatch):
    s = history_setup
    ingress, event, _ = _history_v1_ingress(s)
    account = _v1_legacy_account(s, event)
    original = provider_data._provider_message_account_realms
    reads = 0

    def change_realm(*args):
        nonlocal reads
        result = original(*args)
        reads += 1
        if reads == 1:
            assert result[account] is None
            assert (
                s.db.execute(
                    "UPDATE m_external_accounts_v2 SET provider_realm_uuid=%s, provider_owner_user_id='9' WHERE uuid=%s",
                    (s.realm, account),
                ).rowcount
                == 1
            )
        return result

    monkeypatch.setattr(provider_data, "_provider_message_account_realms", change_realm)
    with s.session_factory() as session:
        with pytest.raises(provider_data.ProviderUnavailableError) as rejected:
            ingress.handle(
                "POST",
                f"{provider_service.API_ROOT}/events",
                {},
                contract.encode({"events": [event]}),
                b"certificate",
                request_session=session,
            )
        assert rejected.value.status == 409
        assert reads == 2
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


def test_v1_rechecks_newly_authorized_account_before_mutation(
    history_setup, monkeypatch
):
    s = history_setup
    ingress, event, _ = _history_v1_ingress(s)
    with s.session_factory() as session:
        desired_resource = session.execute(
            "SELECT resource FROM m_external_bridge_desired_resources_v1 WHERE bridge_instance_uuid=%s AND resource_type='external_account' AND resource_uuid=%s",
            (s.bridge, s.account),
        ).fetchone()["resource"]
        assert (
            session.execute(
                "UPDATE m_external_bridge_desired_resources_v1 SET operation='delete', resource=NULL WHERE bridge_instance_uuid=%s AND resource_type='external_account' AND resource_uuid=%s",
                (s.bridge, s.account),
            ).rowcount
            == 1
        )
    original = provider_data._provider_message_account_realms
    reads = 0

    def authorize_account(*args):
        nonlocal reads
        result = original(*args)
        reads += 1
        if reads == 1:
            assert result == {}
            assert (
                s.db.execute(
                    "UPDATE m_external_bridge_desired_resources_v1 SET operation='upsert', resource=%s::jsonb WHERE bridge_instance_uuid=%s AND resource_type='external_account' AND resource_uuid=%s",
                    (json.dumps(desired_resource), s.bridge, s.account),
                ).rowcount
                == 1
            )
        return result

    monkeypatch.setattr(
        provider_data, "_provider_message_account_realms", authorize_account
    )
    with s.session_factory() as session:
        with pytest.raises(provider_data.ProviderUnavailableError) as rejected:
            ingress.handle(
                "POST",
                f"{provider_service.API_ROOT}/events",
                {},
                contract.encode({"events": [event]}),
                b"certificate",
                request_session=session,
            )
        assert rejected.value.status == 409
        assert reads == 2
        assert (
            session.execute(
                "SELECT count(*) AS n FROM messenger_messages WHERE provider_realm_uuid=%s",
                (s.realm,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("error_kind", ["connection", "service-unavailable"])
def test_storage_outage_keeps_all_queued_batch_jobs_retryable(
    history_setup, monkeypatch, error_kind
):
    s = history_setup
    bodies = [_history_range(s, index * 5000) for index in range(3)]
    jobs = [accept(s, body) for body in bodies]
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=19 WHERE uuid=ANY(%s::uuid[])",
            (jobs,),
        )
    original = file_storage.read_workspace_file

    def unavailable(*args, **kwargs):
        assert s.observer.depth == 0
        if error_kind == "connection":
            raise botocore_exceptions.EndpointConnectionError(
                endpoint_url="https://storage.example.test"
            )
        raise botocore_exceptions.ClientError(
            {
                "Error": {"Code": "ServiceUnavailable"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            },
            "GetObject",
        )

    monkeypatch.setattr(file_storage, "read_workspace_file", unavailable)
    rounds = repository.MAX_CONSECUTIVE_ERRORS + 5
    for _ in range(rounds):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=ANY(%s::uuid[])",
                (jobs,),
            )
        for _ in jobs:
            assert s.worker.run_once()
    for job_uuid, body in zip(jobs, bodies):
        with s.session_factory() as session:
            job = repository.get_job(session, s.identity, job_uuid)
        assert job["status"] == "pending"
        assert job["consecutive_errors"] == 19
        assert job["attempts"] == rounds
        assert job["safe_error"] == "history_storage_unavailable"
        assert accept(s, body) == job_uuid
    monkeypatch.setattr(file_storage, "read_workspace_file", original)
    for job_uuid in jobs:
        result = finish(s, job_uuid)
        assert result["status"] == "complete"
        assert result["inserted_messages"] == 1
        assert result["consecutive_errors"] == 0


def test_missing_local_batch_objects_fail_and_free_all_admission_slots(
    history_setup, monkeypatch, tmp_path
):
    s = history_setup
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    monkeypatch.setattr(file_storage, "get_default_storage_type", lambda: "file")

    def save(file_uuid, data, storage_type=None, storage_object_id=None):
        assert s.observer.depth == 0
        return file_storage.get_workspace_file_storage(storage_type).save(
            file_uuid, data, storage_object_id
        )

    def read(file_uuid, storage_type=None, storage_object_id=None):
        assert s.observer.depth == 0
        assert storage_type == "file"
        return file_storage.get_workspace_file_storage(storage_type).read(
            file_uuid, storage_object_id
        )

    monkeypatch.setattr(file_storage, "save_workspace_file", save)
    monkeypatch.setattr(file_storage, "read_workspace_file", read)
    jobs = [accept(s, _history_range(s, 5000 * index)) for index in range(8)]
    for job_uuid in jobs:
        with s.session_factory() as session:
            job = repository.get_job(session, s.identity, job_uuid)
        path = file_storage.get_workspace_file_path(
            file_uuid=job_uuid,
            storage_object_id=job["storage"]["storage_object_id"],
        )
        assert path.is_file()
        path.unlink()
    ninth = _history_range(s, 40000)
    response = s.ingress.handle(
        "POST", contract.PATH, {}, contract.encode(ninth), b"certificate"
    )
    assert response.status == 429
    for _ in range(repository.MAX_CONSECUTIVE_ERRORS):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=ANY(%s::uuid[])",
                (jobs,),
            )
        for _ in jobs:
            assert s.worker.run_once()
    with s.session_factory() as session:
        for job_uuid in jobs:
            job = repository.get_job(session, s.identity, job_uuid)
            assert job["status"] == "failed"
            assert job["consecutive_errors"] == repository.MAX_CONSECUTIVE_ERRORS
            assert job["safe_error"] == "history_processing_unavailable"
    assert finish(s, accept(s, ninth))["status"] == "complete"


@pytest.mark.parametrize("failure", ["terminated", "closed"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_lost_database_connections_preserve_budget_and_resume(
    history_setup, monkeypatch, failure, wrapped
):
    s = history_setup
    job_uuid = accept(s, envelope(s, count=1))
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=19 WHERE uuid=%s",
            (job_uuid,),
        )
    original = s.worker.process
    failures = []

    def disconnect(identity, job):
        try:
            with s.session_factory() as session:
                pid = session.execute(
                    "SELECT pg_backend_pid() AS pid", ()
                ).fetchone()["pid"]
                assert pid != s.db.info.backend_pid
                if failure == "terminated":
                    assert s.db.execute(
                        "SELECT pg_terminate_backend(%s)", (pid,)
                    ).fetchone()[0]
                else:
                    session._conn.close()
                session.execute("SELECT 1", ())
        except Exception as error:
            assert isinstance(error, pg_errors.OperationalError)
            failures.append(error)
            if wrapped:
                raise RuntimeError("database operation interrupted") from error
            raise

    monkeypatch.setattr(s.worker, "process", disconnect)
    for _ in range(repository.MAX_CONSECUTIVE_ERRORS + 5):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
        assert s.worker.run_once()
    assert len(failures) == repository.MAX_CONSECUTIVE_ERRORS + 5
    if failure == "closed":
        assert all(error.sqlstate is None for error in failures)
    with s.session_factory() as session:
        job = repository.get_job(session, s.identity, job_uuid)
    assert job["status"] == "pending"
    assert job["safe_error"] == "history_database_unavailable"
    assert job["consecutive_errors"] == 19
    assert job["attempts"] == len(failures)
    monkeypatch.setattr(s.worker, "process", original)
    result = finish(s, job_uuid)
    assert result["status"] == "complete" and result["inserted_messages"] == 1
    assert result["consecutive_errors"] == 0


def test_metadata_upload_outage_resumes_without_failing_the_job(
    history_setup, monkeypatch
):
    s = history_setup
    job_uuid = accept(
        s, envelope(s, count=1, content="[file](/user_uploads/1/metadata.txt)")
    )
    assert finish(s, job_uuid)["status"] == "waiting_files"
    request_uuid = _history_status(s, job_uuid)["files"][0]["uuid"]
    _upload_history_file(s, job_uuid, request_uuid)
    with s.session_factory() as session:
        session.execute(
            "UPDATE m_external_history_imports_v1 SET consecutive_errors=19 WHERE uuid=%s",
            (job_uuid,),
        )
    original = file_storage.save_workspace_file_metadata

    def unavailable(*args, **kwargs):
        assert s.observer.depth == 0
        raise botocore_exceptions.ReadTimeoutError(
            endpoint_url="https://storage.example.test"
        )

    monkeypatch.setattr(file_storage, "save_workspace_file_metadata", unavailable)
    for _ in range(repository.MAX_CONSECUTIVE_ERRORS + 5):
        with s.session_factory() as session:
            session.execute(
                "UPDATE m_external_history_imports_v1 SET available_at=now() WHERE uuid=%s",
                (job_uuid,),
            )
        assert s.worker.run_once()
    with s.session_factory() as session:
        job = repository.get_job(session, s.identity, job_uuid)
    assert job["status"] == "pending"
    assert job["consecutive_errors"] == 19
    assert job["next_message_id"] == 0
    monkeypatch.setattr(file_storage, "save_workspace_file_metadata", original)
    result = finish(s, job_uuid)
    assert result["status"] == "complete"
    assert result["inserted_messages"] == 1
    assert result["consecutive_errors"] == 0
