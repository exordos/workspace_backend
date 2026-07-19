# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import contextlib
import datetime
import json
import uuid as sys_uuid

from restalchemy.common import contexts

from workspace.external_bridge_control import provider_data
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models
from workspace.tests.scale import application_boundary
from workspace.tests.scale import concrete_adapter
from workspace.tests.scale import concrete_inventory
from workspace.tests.scale import fixture
from workspace.messenger_api import file_storage


class _PostgreSQLAdapter:
    def prepare(self, session, run):
        session.execute(
            """
            CREATE TABLE IF NOT EXISTS test_fixture_application_units (
                run_uuid UUID NOT NULL,
                unit_uuid UUID NOT NULL,
                backend_pid INTEGER NOT NULL,
                PRIMARY KEY (run_uuid, unit_uuid)
            )
            """,
            (),
        )
        rows = session.execute(
            """
            SELECT unit_uuid FROM test_fixture_application_units
            WHERE run_uuid = %s ORDER BY unit_uuid
            """,
            (run["run_id"],),
        ).fetchall()
        return {"completed_unit_ids": [str(row["unit_uuid"]) for row in rows]}

    def apply_unit(self, session, run, unit):
        session.execute(
            """
            INSERT INTO test_fixture_application_units (
                run_uuid, unit_uuid, backend_pid
            ) VALUES (%s, %s, pg_backend_pid())
            ON CONFLICT (run_uuid, unit_uuid) DO NOTHING
            """,
            (run["run_id"], unit["unit_id"]),
        )
        return {"unit_id": unit["unit_id"]}

    def export_observed(self, session, run):
        rows = session.execute(
            """
            SELECT unit_uuid FROM test_fixture_application_units
            WHERE run_uuid = %s ORDER BY unit_uuid
            """,
            (run["run_id"],),
        ).fetchall()
        return [{"unit_id": str(row["unit_uuid"])} for row in rows]

    def cleanup_manifest(self, session, run):
        count = session.execute(
            """
            SELECT COUNT(*) AS count FROM test_fixture_application_units
            WHERE run_uuid = %s
            """,
            (run["run_id"],),
        ).fetchone()["count"]
        return {"table": "test_fixture_application_units", "rows": count}


class _MismatchedAcknowledgementAdapter(_PostgreSQLAdapter):
    def apply_unit(self, session, run, unit):
        super().apply_unit(session, run, unit)
        return {"unit_id": str(sys_uuid.uuid4())}


def test_application_runner_resumes_durable_units_in_local_postgresql(_database):
    adapter = _PostgreSQLAdapter()

    @contextlib.contextmanager
    def session_context():
        with contexts.Context().session_manager() as session:
            yield session

    runner = application_boundary.ApplicationRunner(adapter, session_context)
    run = {"run_id": str(sys_uuid.uuid4())}
    units = [{"unit_id": str(sys_uuid.uuid4())} for _ in range(3)]
    try:
        first_observed, first_cleanup = runner.run(run, units)
        second_observed, second_cleanup = runner.run(run, units)
    finally:
        with contexts.Context().session_manager() as session:
            session.execute("DROP TABLE IF EXISTS test_fixture_application_units", ())

    assert first_observed == second_observed
    assert first_cleanup["rows"] == 3
    assert second_cleanup["rows"] == 3


def test_application_runner_rolls_back_unit_with_mismatched_acknowledgement(
    _database,
):
    adapter = _MismatchedAcknowledgementAdapter()

    @contextlib.contextmanager
    def session_context():
        with contexts.Context().session_manager() as session:
            yield session

    runner = application_boundary.ApplicationRunner(adapter, session_context)
    run = {"run_id": str(sys_uuid.uuid4())}
    unit = {"unit_id": str(sys_uuid.uuid4())}
    try:
        try:
            runner.run(run, [unit])
        except ValueError as error:
            assert str(error) == "adapter acknowledged a different fixture unit"
        else:
            raise AssertionError("runner accepted a mismatched unit acknowledgement")

        with contexts.Context().session_manager() as session:
            count = session.execute(
                """
                SELECT COUNT(*) AS count FROM test_fixture_application_units
                WHERE run_uuid = %s
                """,
                (run["run_id"],),
            ).fetchone()["count"]
    finally:
        with contexts.Context().session_manager() as session:
            session.execute("DROP TABLE IF EXISTS test_fixture_application_units", ())

    assert count == 0


def test_fixture_materialization_emits_only_exact_planned_event(_database):
    project_uuid = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    event_uuid = sys_uuid.uuid4()
    created_at = datetime.datetime(
        2026,
        7,
        18,
        12,
        30,
        tzinfo=datetime.timezone.utc,
    )

    with contexts.Context().session_manager() as session:
        models.WorkspaceUser.sync_iam_identity(
            user_uuid=user_uuid,
            username=f"fixture-{user_uuid}",
            first_name="Fixture",
            last_name="User",
            email="fixture@example.invalid",
        )
        with messenger_events.suppress_unplanned_fixture_events():
            stream = helpers.get_or_create_workspace_user_stream(
                project_uuid,
                user_uuid,
                session=session,
                uuid=sys_uuid.uuid4(),
                name="fixture stream",
                description="",
                source_name="native",
                source=models.NativeSource(),
            )
            stream_uuid = stream.uuid
            topic_uuid = stream.default_topic_uuid
            message = helpers.create_workspace_user_message(
                project_uuid,
                user_uuid,
                session=session,
                enforce_visibility=False,
                return_visible=False,
                uuid=message_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                payload=message_payloads.MarkdownPayload(content="fixture"),
            )
            helpers.create_workspace_message_reaction(
                project_uuid,
                user_uuid,
                session=session,
                enforce_visibility=False,
                uuid=sys_uuid.uuid4(),
                message_uuid=message_uuid,
                emoji_name="eyes",
            )
            helpers.create_workspace_file(
                project_uuid,
                user_uuid,
                sys_uuid.uuid4(),
                session=session,
                name="fixture.bin",
                description="",
                stream_uuid=stream_uuid,
                acl_mode="stream",
                content_type="application/octet-stream",
                size_bytes=1,
                hash="00",
                storage_type="s3",
                storage_id="fixture",
                storage_object_id="fixture.bin",
            )
        messenger_events.create_deterministic_fixture_broadcast_event(
            project_uuid,
            message.uuid,
            message.get_recipients(session=session),
            "message.created",
            {
                "uuid": str(message.uuid),
                "stream_uuid": str(message.stream_uuid),
                "topic_uuid": str(message.topic_uuid),
            },
            event_uuid,
            created_at,
            session,
        )
        rows = session.execute(
            """
            SELECT uuid, object_type, action, created_at, payload
            FROM m_workspace_broadcast_message_events_v1
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (project_uuid,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["uuid"] == event_uuid
    assert (rows[0]["object_type"], rows[0]["action"]) == (
        "message",
        "created",
    )
    assert rows[0]["created_at"] == created_at
    assert rows[0]["payload"]["kind"] == "message.created"


def test_fixture_user_mapping_never_mutates_or_deletes_existing_iam_identity(
    _database,
):
    logical_uuid = sys_uuid.uuid4()
    iam_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    run_uuid = sys_uuid.uuid4()
    adapter = concrete_adapter.ConcreteFixtureAdapter(
        {
            "workspace_identity_mappings": [
                {
                    "ordinal": 0,
                    "logical_user_uuid": str(logical_uuid),
                    "iam_user_uuid": str(iam_uuid),
                    "access_token": "private",
                }
            ],
            "external_account_credentials": [],
        }
    )
    record = {
        "record_kind": "user",
        "record_key": str(logical_uuid),
        "values": {"name": "synthetic-fixture-name"},
    }
    run = {
        "schema_version": "fixture/v1",
        "run_id": str(run_uuid),
        "test_project_id": str(project_uuid),
        "profile_id": "identity-isolation",
        "profile_sha256": "profile",
        "manifest_sha256": "manifest",
        "expected_ledger_sha256": "ledger",
        "application_plan_sha256": "plan",
    }

    def identity_row(session):
        return session.execute(
            """
            SELECT uuid, username, first_name, last_name, email
            FROM m_workspace_users WHERE uuid = %s
            """,
            (iam_uuid,),
        ).fetchone()

    try:
        with contexts.Context().session_manager() as session:
            models.WorkspaceUser.sync_iam_identity(
                user_uuid=iam_uuid,
                username="real-iam-username",
                first_name="Real",
                last_name="Person",
                email="real-person@example.invalid",
            )
        with contexts.Context().session_manager() as session:
            baseline = identity_row(session)
            actual_uuid, cleanup = adapter._apply_user(session, run, record)
            assert actual_uuid == iam_uuid
            assert cleanup == {}
            assert identity_row(session) == baseline
        try:
            with contexts.Context().session_manager() as session:
                adapter._apply_user(session, run, record)
                raise RuntimeError("injected application failure")
        except RuntimeError as error:
            assert str(error) == "injected application failure"
        with contexts.Context().session_manager() as session:
            assert identity_row(session) == baseline
            adapter.prepare(session, run)
            adapter._record_resource(session, run, record, iam_uuid)
            manifest = adapter.cleanup_manifest(session, run)
            assert manifest["delete_order"] == []
            assert identity_row(session) == baseline
        missing_adapter = concrete_adapter.ConcreteFixtureAdapter(
            {
                "workspace_identity_mappings": [
                    {
                        "ordinal": 0,
                        "logical_user_uuid": str(logical_uuid),
                        "iam_user_uuid": str(sys_uuid.uuid4()),
                        "access_token": "private",
                    }
                ],
                "external_account_credentials": [],
            }
        )
        with contexts.Context().session_manager() as session:
            try:
                missing_adapter._apply_user(session, run, record)
            except ValueError as error:
                assert str(error) == "mapped IAM user does not exist in Workspace"
            else:
                raise AssertionError("missing mapped IAM user was accepted")
    finally:
        with contexts.Context().session_manager() as session:
            for table in (
                "test_workspace_fixture_observations_v1",
                "test_workspace_fixture_resources_v1",
                "test_workspace_fixture_units_v1",
                "test_workspace_fixture_runs_v1",
            ):
                session.execute(f"DROP TABLE IF EXISTS {table} CASCADE", ())
            session.execute(
                "DELETE FROM m_workspace_users WHERE uuid = %s", (iam_uuid,)
            )


class _IsolatedInventoryStorage:
    storage_type = "s3"
    storage_id = "isolated-fixture"

    def __init__(self):
        self.objects = {}
        self.metadata = {}

    def read(self, file_uuid, storage_object_id=None):
        del file_uuid
        try:
            return self.objects[storage_object_id]
        except KeyError as error:
            raise FileNotFoundError(storage_object_id) from error

    def read_metadata(self, file_uuid):
        try:
            return self.metadata[str(file_uuid)]
        except KeyError as error:
            raise FileNotFoundError(str(file_uuid)) from error

    def list_object_ids(self):
        return sorted(
            [*self.objects]
            + [
                file_storage.get_workspace_file_metadata_object_id(file_uuid)
                for file_uuid in self.metadata
            ]
        )


def _blank_inventory_manifest():
    return {
        "expected_row_counts": {
            name: 0
            for name in (
                "users",
                "streams",
                "topics",
                "messages",
                "provider_messages",
                "reactions",
                "files",
                "canonical_broadcast_events",
                "visible_event_deliveries",
                "zulip_accounts",
            )
        },
        "relationship_counts": {
            "stream_memberships": 0,
            "provider_synced_streams": 0,
        },
        "normalized_digests": {
            name: ""
            for name in (
                "users",
                "streams",
                "stream_memberships",
                "topics",
                "messages",
                "reactions",
                "files",
                "canonical_broadcast_events",
                "visible_event_deliveries",
                "zulip_accounts",
                "provider_mappings",
            )
        },
        "canonical_event_age_buckets": {
            "older_than_7d": 0,
            "exact_7d_boundary": 0,
            "newer_than_7d": 0,
        },
        "s3_objects": [],
        "provider_stream_mappings": [],
        "provider_mapping_counts": {
            "external_accounts": 0,
            "assigned_accounts": 0,
            "streams": 0,
            "topics": 0,
            "messages": 0,
        },
    }


def test_actual_inventory_fails_on_tampered_missing_and_extra_database_and_storage(
    _database,
):
    logical_user_uuid = sys_uuid.uuid4()
    iam_user_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    run_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    event_uuid = sys_uuid.uuid4()
    storage = _IsolatedInventoryStorage()
    exporter = concrete_inventory.ActualInventoryExporter(
        [
            {
                "logical_user_uuid": str(logical_user_uuid),
                "iam_user_uuid": str(iam_user_uuid),
            }
        ],
        storage=storage,
    )
    run = {
        "run_id": str(run_uuid),
        "test_project_id": str(project_uuid),
        "profile_id": "inventory-integration",
        "manifest_sha256": "integration-manifest",
    }

    with contexts.Context().session_manager() as session:
        models.WorkspaceUser.sync_iam_identity(
            user_uuid=iam_user_uuid,
            username=f"inventory-{iam_user_uuid}",
            first_name="Inventory",
            last_name="Fixture",
            email="inventory@example.invalid",
        )
        with messenger_events.suppress_unplanned_fixture_events():
            stream = helpers.get_or_create_workspace_user_stream(
                project_uuid,
                iam_user_uuid,
                session=session,
                uuid=sys_uuid.uuid4(),
                name="inventory fixture",
                description="",
                source_name="native",
                source=models.NativeSource(),
            )
            message = helpers.create_workspace_user_message(
                project_uuid,
                iam_user_uuid,
                session=session,
                enforce_visibility=False,
                return_visible=False,
                uuid=message_uuid,
                stream_uuid=stream.uuid,
                topic_uuid=stream.default_topic_uuid,
                payload=message_payloads.MarkdownPayload(content="fixture message 0"),
            )
            helpers.create_workspace_message_reaction(
                project_uuid,
                iam_user_uuid,
                session=session,
                enforce_visibility=False,
                uuid=reaction_uuid,
                message_uuid=message_uuid,
                emoji_name="eyes",
            )
            helpers.create_workspace_file(
                project_uuid,
                iam_user_uuid,
                file_uuid,
                session=session,
                name="fixture.bin",
                description="",
                stream_uuid=stream.uuid,
                acl_mode="stream",
                content_type="application/octet-stream",
                size_bytes=7,
                hash=fixture.sha256(b"fixture"),
                storage_type="s3",
                storage_id=storage.storage_id,
                storage_object_id=file_storage.get_workspace_file_object_id(file_uuid),
            )
        created_at = fixture.EVENT_REFERENCE_TIME - datetime.timedelta(days=7)
        messenger_events.create_deterministic_fixture_broadcast_event(
            project_uuid,
            message.uuid,
            message.get_recipients(session=session),
            "message.created",
            {
                "uuid": str(message.uuid),
                "stream_uuid": str(message.stream_uuid),
                "topic_uuid": str(message.topic_uuid),
            },
            event_uuid,
            created_at,
            session,
        )
        binding = session.execute(
            "SELECT uuid, role FROM m_workspace_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s",
            (project_uuid, stream.uuid),
        ).fetchone()

    binary_id = file_storage.get_workspace_file_object_id(file_uuid)
    storage.objects[binary_id] = b"fixture"
    storage.metadata[str(file_uuid)] = file_storage.WorkspaceFileMetadata(
        uuid=file_uuid,
        project_id=project_uuid,
        stream_uuid=stream.uuid,
        owner_uuid=iam_user_uuid,
        name="fixture.bin",
        description="",
        content_type="application/octet-stream",
        size_bytes=7,
        sha256=fixture.sha256(b"fixture"),
        created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
    )
    units = [
        {
            "records": [
                {
                    "record_kind": "user",
                    "record_key": str(logical_user_uuid),
                    "values": {},
                },
                {
                    "record_kind": "stream",
                    "record_key": str(stream.uuid),
                    "values": {
                        "bindings": [
                            {
                                "uuid": str(binding["uuid"]),
                                "user_uuid": str(logical_user_uuid),
                                "role": binding["role"],
                            }
                        ],
                        "topics": [{"uuid": str(stream.default_topic_uuid)}],
                    },
                },
                {
                    "record_kind": "message",
                    "record_key": str(message_uuid),
                    "values": {"recipients": [str(logical_user_uuid)]},
                },
                {
                    "record_kind": "event",
                    "record_key": str(event_uuid),
                    "values": {},
                },
                {
                    "record_kind": "reaction",
                    "record_key": str(reaction_uuid),
                    "values": {},
                },
                {
                    "record_kind": "file",
                    "record_key": str(file_uuid),
                    "values": {},
                },
            ]
        }
    ]

    try:
        logical_user = str(logical_user_uuid)
        stream_uuid = str(stream.uuid)
        topic_uuid = str(stream.default_topic_uuid)
        binding_uuid = str(binding["uuid"])
        payload = {"kind": "markdown", "content": "fixture message 0"}
        normalized_sidecar = json.loads(storage.metadata[str(file_uuid)].to_json())
        normalized_sidecar["owner_uuid"] = logical_user
        stream_row = {
            "uuid": stream_uuid,
            "kind": "channel",
            "provider_synced": False,
            "owner_user_uuid": logical_user,
            "private": False,
            "invite_only": False,
            "default_topic_uuid": topic_uuid,
            "bindings": [
                {
                    "uuid": binding_uuid,
                    "user_uuid": logical_user,
                    "role": binding["role"],
                }
            ],
            "external_chat_uuid": None,
            "provider_chat_key": None,
        }
        message_row = {
            "uuid": str(message_uuid),
            "project_id": str(project_uuid),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "sender_uuid": logical_user,
            "recipients": [logical_user],
            "provider": False,
            "external_account_uuid": None,
            "external_chat_uuid": None,
            "provider_chat_key": None,
            "payload_sha256": fixture.sha256(payload),
            "message_index": 0,
        }
        event_row = {
            "event_uuid": str(event_uuid),
            "event_kind": "message.created",
            "message_uuid": str(message_uuid),
            "stream_uuid": stream_uuid,
            "created_at": created_at.isoformat(),
            "retention_policy": {
                "kind": "fixed_reference_7d",
                "reference_time": fixture.EVENT_REFERENCE_TIME.isoformat(),
                "retention_days": 7,
                "bucket": "exact_7d_boundary",
            },
        }
        delivery_row = {
            "event_uuid": str(event_uuid),
            "event_kind": "message.created",
            "message_uuid": str(message_uuid),
            "recipient_user_uuid": logical_user,
        }
        file_row = {
            "uuid": str(file_uuid),
            "size_bytes": 7,
            "object_name": binary_id,
            "binary_sha256": fixture.sha256(b"fixture"),
            "sidecar_object_name": (
                file_storage.get_workspace_file_metadata_object_id(file_uuid)
            ),
            "sidecar_sha256": fixture.sha256(normalized_sidecar),
        }
        manifest = _blank_inventory_manifest()
        manifest["expected_row_counts"].update(
            {
                "users": 1,
                "streams": 1,
                "topics": 1,
                "messages": 1,
                "reactions": 1,
                "files": 1,
                "canonical_broadcast_events": 1,
                "visible_event_deliveries": 1,
            }
        )
        manifest["relationship_counts"]["stream_memberships"] = 1
        manifest["canonical_event_age_buckets"]["exact_7d_boundary"] = 1
        manifest["normalized_digests"].update(
            {
                "users": fixture._digest_rows(
                    iter([{"uuid": logical_user}]),
                    ("uuid",),
                )[1],
                "streams": fixture._digest_rows(
                    iter([stream_row]),
                    tuple(stream_row),
                )[1],
                "stream_memberships": fixture._digest_rows(
                    iter(
                        [
                            {
                                "stream_uuid": stream_uuid,
                                "binding_uuid": binding_uuid,
                                "user_uuid": logical_user,
                                "role": binding["role"],
                            }
                        ]
                    ),
                    ("stream_uuid", "binding_uuid", "user_uuid", "role"),
                )[1],
                "topics": fixture._digest_rows(
                    iter([{"stream_uuid": stream_uuid, "topic_uuid": topic_uuid}]),
                    ("stream_uuid", "topic_uuid"),
                )[1],
                "messages": fixture._digest_rows(
                    iter([message_row]),
                    tuple(message_row),
                )[1],
                "reactions": fixture._digest_rows(
                    iter(
                        [
                            {
                                "uuid": str(reaction_uuid),
                                "message_uuid": str(message_uuid),
                                "user_uuid": logical_user,
                                "emoji_name": "eyes",
                            }
                        ]
                    ),
                    ("uuid", "message_uuid", "user_uuid", "emoji_name"),
                )[1],
                "files": fixture._digest_rows(
                    iter([file_row]),
                    tuple(file_row),
                )[1],
                "canonical_broadcast_events": fixture._digest_rows(
                    iter([event_row]),
                    tuple(event_row),
                )[1],
                "visible_event_deliveries": fixture._digest_rows(
                    iter([delivery_row]),
                    tuple(delivery_row),
                )[1],
                "zulip_accounts": fixture._digest_rows(iter(()), ())[1],
                "provider_mappings": fixture._digest_rows(iter(()), ())[1],
            }
        )
        manifest["s3_objects"] = [file_row]
        with contexts.Context().session_manager() as session:
            positive = exporter.export(session, run, manifest, units)
            assert positive["status"] == "PASS", positive
            assert positive["passed"] is True

        with contexts.Context().session_manager() as session:
            session.execute(
                "UPDATE m_workspace_messages SET payload = %s::jsonb WHERE uuid = %s",
                (json.dumps({"kind": "markdown", "content": "tampered"}), message_uuid),
            )
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "normalized_digests.messages" in report["mismatches"]
            session.execute(
                "UPDATE m_workspace_messages SET payload = %s::jsonb WHERE uuid = %s",
                (
                    json.dumps({"kind": "markdown", "content": "fixture message 0"}),
                    message_uuid,
                ),
            )

        with contexts.Context().session_manager() as session:
            session.execute(
                "DELETE FROM m_workspace_message_reactions WHERE uuid = %s",
                (reaction_uuid,),
            )
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "resources.reaction" in report["mismatches"]
            assert "expected_row_counts.reactions" in report["mismatches"]
            helpers.create_workspace_message_reaction(
                project_uuid,
                iam_user_uuid,
                session=session,
                enforce_visibility=False,
                compact_events=True,
                uuid=reaction_uuid,
                message_uuid=message_uuid,
                emoji_name="eyes",
            )

        extra_reaction_uuid = sys_uuid.uuid4()
        with contexts.Context().session_manager() as session:
            helpers.create_workspace_message_reaction(
                project_uuid,
                iam_user_uuid,
                session=session,
                enforce_visibility=False,
                compact_events=True,
                uuid=extra_reaction_uuid,
                message_uuid=message_uuid,
                emoji_name="rocket",
            )
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "resources.reaction" in report["mismatches"]
            session.execute(
                "DELETE FROM m_workspace_message_reactions WHERE uuid = %s",
                (extra_reaction_uuid,),
            )

        storage.objects[binary_id] = b"tampered"
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "s3_objects" in report["mismatches"]
            assert "normalized_digests.files" in report["mismatches"]
        storage.objects[binary_id] = b"fixture"

        metadata = storage.metadata.pop(str(file_uuid))
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "s3_objects.missing_or_invalid" in report["mismatches"]
        storage.metadata[str(file_uuid)] = metadata

        extra_file_uuid = sys_uuid.uuid4()
        storage.metadata[str(extra_file_uuid)] = file_storage.WorkspaceFileMetadata(
            uuid=extra_file_uuid,
            project_id=project_uuid,
            stream_uuid=stream.uuid,
            owner_uuid=iam_user_uuid,
            name="extra.bin",
            description="",
            content_type="application/octet-stream",
            size_bytes=5,
            sha256=fixture.sha256(b"extra"),
            created_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
        )
        storage.objects[file_storage.get_workspace_file_object_id(extra_file_uuid)] = (
            b"extra"
        )
        with contexts.Context().session_manager() as session:
            report = exporter.export(session, run, manifest, units)
            assert "s3_objects.extra_project_objects" in report["mismatches"]
    finally:
        with contexts.Context().session_manager() as session:
            session.execute(
                "DELETE FROM m_workspace_streams WHERE project_id = %s",
                (project_uuid,),
            )
            session.execute(
                "DELETE FROM m_workspace_users WHERE uuid = %s", (iam_user_uuid,)
            )


def test_actual_inventory_queries_nonempty_provider_persistence_ledgers(_database):
    logical_user_uuid = sys_uuid.uuid4()
    iam_user_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    bridge_uuid = sys_uuid.uuid4()
    external_operation_uuid = sys_uuid.uuid4()
    provider_event_uuid = sys_uuid.uuid4()
    target_uuid = sys_uuid.uuid4()
    exporter = concrete_inventory.ActualInventoryExporter(
        [
            {
                "logical_user_uuid": str(logical_user_uuid),
                "iam_user_uuid": str(iam_user_uuid),
            }
        ]
    )
    run = {"test_project_id": str(project_uuid)}
    units = [
        {
            "records": [
                {
                    "record_kind": "external_account",
                    "record_key": str(account_uuid),
                    "values": {},
                }
            ]
        }
    ]
    event = {
        "provider_event_uuid": str(provider_event_uuid),
        "provider_sequence": "17",
        "kind": "message.upsert",
        "payload": {"resource": {"uuid": str(target_uuid)}},
    }
    try:
        with contexts.Context().session_manager() as session:
            models.WorkspaceUser.sync_iam_identity(
                user_uuid=iam_user_uuid,
                username=f"provider-inventory-{iam_user_uuid}",
                first_name="Provider",
                last_name="Inventory",
                email="provider-inventory@example.invalid",
            )
            session.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings
                ) VALUES (%s, %s, 'zulip', %s::jsonb)
                """,
                (account_uuid, iam_user_uuid, json.dumps({"kind": "zulip"})),
            )
            session.execute(
                """
                INSERT INTO m_external_bridge_instances_v2 (
                    uuid, provider, status
                ) VALUES (%s, 'zulip', 'active')
                """,
                (bridge_uuid,),
            )
            provider_data.enqueue_provider_operation(
                session,
                operation_uuid=external_operation_uuid,
                bridge_instance_uuid=bridge_uuid,
                external_account_uuid=account_uuid,
                project_id=project_uuid,
                owner_user_uuid=iam_user_uuid,
                operation_kind="message.create",
                target_type="message",
                target_uuid=target_uuid,
                payload={"resource": {"uuid": str(target_uuid)}},
            )
            provider_data.apply_provider_event(
                session,
                bridge_instance_uuid=bridge_uuid,
                external_account_uuid=account_uuid,
                project_id=project_uuid,
                event=event,
                apply=lambda event, session: target_uuid,
            )

        with contexts.Context().session_manager() as session:
            _, actual = exporter._query(session, run, units)
        assert len(actual["provider_operations"]) == 1
        assert actual["provider_operations"][0]["external_operation_uuid"] == (
            external_operation_uuid
        )
        assert actual["provider_operations"][0]["queue_account_uuid"] == account_uuid
        assert actual["provider_operations"][0]["payload"] == {
            "resource": {"uuid": str(target_uuid)}
        }
        assert len(actual["provider_events"]) == 1
        assert actual["provider_events"][0]["provider_event_uuid"] == (
            provider_event_uuid
        )
        assert actual["provider_events"][0]["payload_sha256"] == fixture.sha256(event)
        assert actual["provider_events"][0]["target_uuid"] == target_uuid
    finally:
        with contexts.Context().session_manager() as session:
            session.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
                (account_uuid,),
            )
            session.execute(
                "DELETE FROM m_external_bridge_instances_v2 WHERE uuid = %s",
                (bridge_uuid,),
            )
            session.execute(
                "DELETE FROM m_workspace_users WHERE uuid = %s",
                (iam_user_uuid,),
            )
