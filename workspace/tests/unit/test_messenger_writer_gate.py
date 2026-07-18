# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import ast
import datetime
import pathlib
import uuid as sys_uuid

import pytest

from workspace.messenger_migration import writer_gate
from workspace.services.messenger_workers import agents
from workspace.cmd import messenger_api
from workspace.cmd import external_bridge_api
from workspace.cmd import workspace_api


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
NOW = datetime.datetime(2026, 7, 18, 12, tzinfo=datetime.timezone.utc)


class Result:
    def __init__(self, row=None, rows=()):
        self.row = row
        self.rows = list(rows)

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class GateSession:
    def __init__(self, state="closed", expires=None):
        self.gate_uuid = sys_uuid.uuid4()
        self.state = state
        self.expires = expires or NOW + datetime.timedelta(minutes=5)
        self.acks = {}
        self.expected_rows = []

    @property
    def gate(self):
        return {
            "gate_uuid": self.gate_uuid,
            "state": self.state,
            "acquired_at": NOW,
            "lease_expires_at": self.expires,
        }

    def execute(self, statement, params):
        normalized = " ".join(statement.split())
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            return Result()
        if normalized.startswith('SELECT "gate_uuid", "state"'):
            if self.state is None:
                return Result()
            if len(params) == 2 and params[1] != self.gate_uuid:
                return Result()
            return Result(self.gate)
        if normalized.startswith('INSERT INTO "m_messenger_writer_gate_acks_v1"'):
            gate_uuid, writer_class, instance_id, acknowledged_at, expires = params
            self.acks[(writer_class, instance_id)] = {
                "gate_uuid": gate_uuid,
                "writer_class": writer_class,
                "acknowledged_at": acknowledged_at,
                "lease_expires_at": expires,
            }
            return Result()
        if normalized.startswith('SELECT expected."instance_id"'):
            return Result()
        if normalized.startswith('SELECT expected."writer_class"'):
            return Result(rows=self.expected_rows)
        if normalized.startswith('UPDATE "m_messenger_writer_gates_v1"'):
            _now, _project_id, gate_uuid = params
            if self.state != "closed" or gate_uuid != self.gate_uuid:
                return Result()
            self.state = "open"
            return Result({"gate_uuid": self.gate_uuid})
        raise AssertionError(normalized)


@pytest.mark.parametrize("writer_class", sorted(writer_gate.REQUIRED_WRITER_CLASSES))
def test_each_writer_class_is_blocked_by_authoritative_gate(writer_class):
    session = GateSession()

    with pytest.raises(writer_gate.WriterGateClosed):
        writer_gate.assert_writable(
            session,
            PROJECT_UUID,
            writer_class,
            instance_id=f"test:{writer_class}",
            now=NOW,
        )


def test_releasing_gate_resumes_writes():
    session = GateSession()
    writer_gate.release_gate(session, PROJECT_UUID, session.gate_uuid, now=NOW)

    writer_gate.assert_writable(
        session,
        PROJECT_UUID,
        "api",
        instance_id="test:api",
        now=NOW,
    )


def test_validation_rejects_missing_or_expired_service_acknowledgements():
    session = GateSession()
    session.expected_rows = [
        {
            "writer_class": writer_class,
            "instance_id": f"test:{writer_class}",
            "instance_lease_expires_at": NOW + datetime.timedelta(minutes=1),
            "ack_lease_expires_at": NOW + datetime.timedelta(minutes=1),
        }
        for writer_class in writer_gate.REQUIRED_WRITER_CLASSES
    ]
    assert writer_gate.validate_closed_gate(
        session, PROJECT_UUID, session.gate_uuid, now=NOW
    )["blocked_writer_classes"] == sorted(writer_gate.REQUIRED_WRITER_CLASSES)

    next(row for row in session.expected_rows if row["writer_class"] == "api")[
        "ack_lease_expires_at"
    ] = NOW
    with pytest.raises(ValueError, match="api"):
        writer_gate.validate_closed_gate(
            session, PROJECT_UUID, session.gate_uuid, now=NOW
        )


def test_validation_rejects_gate_loss_and_expired_gate():
    missing = GateSession(state=None)
    with pytest.raises(ValueError, match="absent"):
        writer_gate.validate_closed_gate(
            missing, PROJECT_UUID, missing.gate_uuid, now=NOW
        )

    expired = GateSession(expires=NOW)
    with pytest.raises(ValueError, match="expired"):
        writer_gate.validate_closed_gate(
            expired, PROJECT_UUID, expired.gate_uuid, now=NOW
        )


def test_worker_boundary_attests_only_its_actual_writer_class():
    source = pathlib.Path(agents.__file__).read_text()

    assert '"worker"' in source
    assert '"smtp_ingress"' not in source
    assert '"external_bridge"' not in source
    assert "writer_gate.heartbeat_and_acknowledge" in source
    assert "if closed_projects:" in source


def test_api_processes_and_bridge_service_heartbeat_their_own_instances():
    api_source = pathlib.Path(messenger_api.__file__).read_text()
    workspace_api_source = pathlib.Path(workspace_api.__file__).read_text()
    bridge_source = pathlib.Path(external_bridge_api.__file__).read_text()

    assert '"api"' in api_source
    assert "writer_gate.start_heartbeat" in api_source
    assert '"api"' in workspace_api_source
    assert "writer_gate.start_heartbeat" in workspace_api_source
    assert '"external_bridge"' in bridge_source
    assert "writer_gate.start_heartbeat" in bridge_source


def test_absent_smtp_boundary_cannot_validate():
    session = GateSession()
    session.expected_rows = [
        {
            "writer_class": writer_class,
            "instance_id": f"test:{writer_class}",
            "instance_lease_expires_at": NOW + datetime.timedelta(minutes=1),
            "ack_lease_expires_at": NOW + datetime.timedelta(minutes=1),
        }
        for writer_class in writer_gate.REQUIRED_WRITER_CLASSES
        if writer_class != "smtp_ingress"
    ]

    with pytest.raises(ValueError, match="smtp_ingress"):
        writer_gate.validate_closed_gate(
            session, PROJECT_UUID, session.gate_uuid, now=NOW
        )


def test_canonical_runtime_phase_does_not_require_retired_smtp_ingress():
    session = GateSession()
    required = writer_gate.REQUIRED_WRITER_CLASSES_BY_PHASE[
        writer_gate.POSTGRESQL_CANONICAL_RUNTIME
    ]
    session.expected_rows = [
        {
            "writer_class": writer_class,
            "instance_id": f"test:{writer_class}",
            "instance_lease_expires_at": NOW + datetime.timedelta(minutes=1),
            "ack_lease_expires_at": NOW + datetime.timedelta(minutes=1),
        }
        for writer_class in required
    ]
    session.expected_rows.append(
        {
            "writer_class": "smtp_ingress",
            "instance_id": "retired:smtp",
            "instance_lease_expires_at": NOW - datetime.timedelta(minutes=1),
            "ack_lease_expires_at": NOW - datetime.timedelta(minutes=1),
        }
    )

    evidence = writer_gate.validate_closed_gate(
        session,
        PROJECT_UUID,
        session.gate_uuid,
        now=NOW,
        phase=writer_gate.POSTGRESQL_CANONICAL_RUNTIME,
    )

    assert evidence["phase"] == writer_gate.POSTGRESQL_CANONICAL_RUNTIME
    assert evidence["blocked_writer_classes"] == sorted(required)
    assert "smtp_ingress" not in evidence["blocked_writer_classes"]


def test_unknown_writer_gate_phase_fails_closed():
    session = GateSession()

    with pytest.raises(ValueError, match="Unknown Messenger writer-gate phase"):
        writer_gate.validate_closed_gate(
            session,
            PROJECT_UUID,
            session.gate_uuid,
            now=NOW,
            phase="future_phase",
        )


def test_silent_extra_api_instance_blocks_validation():
    session = GateSession()
    session.expected_rows = [
        {
            "writer_class": writer_class,
            "instance_id": f"test:{writer_class}",
            "instance_lease_expires_at": NOW + datetime.timedelta(minutes=1),
            "ack_lease_expires_at": NOW + datetime.timedelta(minutes=1),
        }
        for writer_class in writer_gate.REQUIRED_WRITER_CLASSES
    ]
    session.expected_rows.append(
        {
            "writer_class": "api",
            "instance_id": "test:api:silent",
            "instance_lease_expires_at": NOW,
            "ack_lease_expires_at": None,
        }
    )

    with pytest.raises(ValueError, match="api:test:api:silent"):
        writer_gate.validate_closed_gate(
            session, PROJECT_UUID, session.gate_uuid, now=NOW
        )


def test_forged_proxy_ack_is_rejected_without_preclose_live_instance():
    session = GateSession()

    with pytest.raises(ValueError, match="not live before"):
        writer_gate.acknowledge(
            session,
            PROJECT_UUID,
            "smtp_ingress",
            instance_id="worker-pretending-to-be-smtp",
            now=NOW,
        )


def test_first_gate_and_first_writer_share_advisory_transaction_lock():
    source = pathlib.Path(writer_gate.__file__).read_text()

    assert "pg_advisory_xact_lock" in source
    assert source.index("def close_gate") < source.index(
        "_lock_project(session, project_id)", source.index("def close_gate")
    )
    assert source.index("def assert_writable") < source.index(
        "_lock_project(session, project_id)", source.index("def assert_writable")
    )


def test_worker_holds_writer_gate_lock_before_background_mutations():
    source = pathlib.Path(agents.__file__).read_text()
    module = ast.parse(source)
    worker = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "MessengerWorkerAgent"
    )
    iteration = next(
        node
        for node in worker.body
        if isinstance(node, ast.FunctionDef) and node.name == "_iteration"
    )

    session_contexts = [
        item
        for node in ast.walk(iteration)
        if isinstance(node, ast.With)
        for item in node.items
        if isinstance(item.context_expr, ast.Call)
        and ast.unparse(item.context_expr.func) == "database_session_context"
    ]
    assert len(session_contexts) == 1

    calls = sorted(
        (node for node in ast.walk(iteration) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    call_positions = {ast.unparse(call.func): index for index, call in enumerate(calls)}
    assert not any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "session_manager"
        for call in calls
    )

    lock_index = call_positions["writer_gate.assert_writable"]
    for mutation in (
        "messenger_dm_helpers.mark_stale_workspace_users_offline",
        "sql_state.refresh_effective_capabilities",
        "external_bridge_data_plane.flush_outbox",
        "self._prune_expired_events",
        "self._repair_external_projection_transitions",
    ):
        assert lock_index < call_positions[mutation]


def test_worker_discovers_every_project_scoped_canonical_mutation_source():
    first_project = sys_uuid.uuid4()
    second_project = sys_uuid.uuid4()
    statements = []

    class Session:
        def execute(self, statement, params):
            statements.append((statement, params))
            return Result(
                rows=(
                    {"project_id": first_project},
                    {"project_id": second_project},
                )
            )

    assert agents.MessengerWorkerAgent._canonical_mutation_project_ids(Session()) == (
        first_project,
        second_project,
    )
    statement, params = statements[0]
    assert params == ()
    for table in (
        "m_workspace_streams",
        "m_workspace_events",
        "m_workspace_broadcast_message_events_v1",
        "m_external_chats_v2",
        "m_external_accounts_v2",
        "m_messenger_import_runs_v1",
        "m_messenger_writer_gates_v1",
    ):
        assert table in statement
    assert "ORDER BY projects.project_id" in statement


def test_gate_migration_has_authoritative_rows_and_live_ack_index():
    migration = pathlib.Path(
        "migrations/0113-add-Messenger-writer-gates-4b6e80.py"
    ).read_text()

    assert 'CREATE TABLE "m_messenger_writer_gates_v1"' in migration
    assert 'CREATE TABLE "m_messenger_writer_instances_v1"' in migration
    assert 'CREATE TABLE "m_messenger_writer_gate_expected_v1"' in migration
    assert 'CREATE TABLE "m_messenger_writer_gate_acks_v1"' in migration
    assert '"gate_uuid", "writer_class", "lease_expires_at"' in migration


def test_smtp_attester_role_has_only_writer_gate_dml_privileges():
    migration = pathlib.Path(
        "migrations/0114-grant-SMTP-ingress-writer-gate-attester-access-c990ad.py"
    ).read_text()

    assert "workspace_mail_gate" in migration
    assert "GRANT SELECT ON" in migration
    assert "GRANT INSERT, UPDATE ON" in migration
    dml_grant = migration.split("GRANT INSERT, UPDATE ON", 1)[1].split(
        'TO "workspace_mail_gate"',
        1,
    )[0]
    assert "m_messenger_writer_instances_v1" in dml_grant
    assert "m_messenger_writer_gate_acks_v1" in dml_grant
    assert "m_messenger_writer_gates_v1" not in dml_grant
    assert "m_messenger_writer_gate_expected_v1" not in dml_grant
    assert "DELETE" not in migration
    for table in (
        "m_messenger_writer_gates_v1",
        "m_messenger_writer_instances_v1",
        "m_messenger_writer_gate_expected_v1",
        "m_messenger_writer_gate_acks_v1",
    ):
        assert table in migration


def test_smtp_attester_role_reasserts_table_access_and_current_database_connect():
    migration = pathlib.Path(
        "migrations/0115-grant-SMTP-writer-gate-database-connect-c73e35.py"
    ).read_text()

    assert "IF EXISTS" in migration.split("def downgrade", 1)[0]
    assert "GRANT SELECT ON" in migration
    assert "GRANT INSERT, UPDATE ON" in migration
    assert 'GRANT CONNECT ON DATABASE %I TO "workspace_mail_gate"' in migration
    assert 'REVOKE CONNECT ON DATABASE %I FROM "workspace_mail_gate"' in migration
    assert "current_database()" in migration
    dml_grant = migration.split("GRANT INSERT, UPDATE ON", 1)[1].split(
        'TO "workspace_mail_gate"',
        1,
    )[0]
    assert "m_messenger_writer_instances_v1" in dml_grant
    assert "m_messenger_writer_gate_acks_v1" in dml_grant
    assert "m_messenger_writer_gates_v1" not in dml_grant
    assert "m_messenger_writer_gate_expected_v1" not in dml_grant
    assert "DELETE" not in migration
    assert "GRANT CREATE" not in migration
    assert "GRANT TEMPORARY" not in migration
