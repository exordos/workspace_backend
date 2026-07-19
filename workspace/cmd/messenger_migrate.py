# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Operator-only Maildir to PostgreSQL import; never changes writer mode."""

import argparse
import contextlib
import datetime
import json
import pathlib
import sys
import typing
import uuid as sys_uuid

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import external_bridge_opts
from workspace.common import log as infra_log
from workspace.common import file_storage_opts
from workspace.messenger_mail import runtime as mail_runtime
from workspace.messenger_migration import mail_source
from workspace.messenger_migration import legacy_provider_outbox
from workspace.messenger_migration import postgres_target
from workspace.messenger_migration import service
from workspace.messenger_migration import writer_gate
from workspace.services.messenger_workers import agents as messenger_worker_agents


CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
mail_runtime.register_opts(CONF)
file_storage_opts.register_opts(CONF)
external_bridge_opts.register_opts(CONF)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m workspace.cmd.messenger_migrate")
    parser.add_argument("--config-file", action="append", default=[])
    parser.add_argument("--project-id", type=sys_uuid.UUID, required=True)
    parser.add_argument("--run-id", type=sys_uuid.UUID)
    parser.add_argument("--output", type=pathlib.Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory")
    subparsers.add_parser("stage")
    apply = subparsers.add_parser("apply")
    apply.add_argument("--batch-size", type=int, default=500)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--gate-id", type=sys_uuid.UUID, required=True)
    subparsers.add_parser("final-delta")
    subparsers.add_parser("parity")
    close = subparsers.add_parser("writer-gate-close")
    close.add_argument("--gate-id", type=sys_uuid.UUID)
    close.add_argument("--lease-seconds", type=int, default=600)
    release = subparsers.add_parser("writer-gate-release")
    release.add_argument("--gate-id", type=sys_uuid.UUID, required=True)
    subparsers.add_parser("writer-gate-status")
    convert = subparsers.add_parser("legacy-provider-outbox-convert")
    convert.add_argument("--gate-id", type=sys_uuid.UUID, required=True)
    return parser


def _write_report(
    report: dict[str, typing.Any],
    output: pathlib.Path | None,
) -> None:
    body = json.dumps(report, sort_keys=True, indent=2, default=str) + "\n"
    if output is None:
        sys.stdout.write(body)
        return
    output.write_text(body)


@contextlib.contextmanager
def _coordinator(
    project_id: sys_uuid.UUID,
) -> typing.Iterator[service.ImportCoordinator]:
    runtime = mail_runtime.RuntimeFactory(CONF)
    with runtime.project_repository(project_id) as repository:
        yield service.ImportCoordinator(
            mail_source.MailProjectionSource(repository),
            postgres_target.PostgreSQLImportTarget(),
            file_verifier=service.FileObjectVerifier(),
        )


def _database_context() -> typing.ContextManager[typing.Any]:
    # This is the outer worker/CLI boundary. Migration services only reuse it.
    return messenger_worker_agents.database_session_context()


def main(argv: typing.Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_args: list[str] = []
    for path in args.config_file:
        config_args.extend(("--config-file", path))
    config.parse(config_args)
    infra_log.configure()
    engines.engine_factory.configure_postgresql_factory(conf=CONF)
    report: dict[str, typing.Any]
    gate_commands = {
        "writer-gate-close",
        "writer-gate-release",
        "writer-gate-status",
    }
    database_only_commands = gate_commands | {"legacy-provider-outbox-convert"}
    if (
        args.command != "inventory"
        and args.command not in database_only_commands
        and args.run_id is None
    ):
        raise SystemExit("--run-id is required for this command")
    if args.command in database_only_commands:
        with _database_context() as session:
            if args.command == "writer-gate-close":
                gate_uuid = writer_gate.close_gate(
                    session,
                    args.project_id,
                    gate_uuid=args.gate_id,
                    lease=datetime.timedelta(seconds=args.lease_seconds),
                )
                report = {
                    "project_id": str(args.project_id),
                    "gate_id": str(gate_uuid),
                }
            elif args.command == "writer-gate-release":
                writer_gate.release_gate(
                    session,
                    args.project_id,
                    args.gate_id,
                )
                report = {
                    "project_id": str(args.project_id),
                    "gate_id": str(args.gate_id),
                }
            elif args.command == "legacy-provider-outbox-convert":
                gate_evidence = writer_gate.validate_closed_gate(
                    session,
                    args.project_id,
                    args.gate_id,
                    phase=writer_gate.LEGACY_MAIL_FREEZE,
                )
                bridge = CONF[external_bridge_opts.DOMAIN]
                report = legacy_provider_outbox.convert_required_operations(
                    session,
                    project_id=args.project_id,
                    realm_uuid=bridge.realm_uuid,
                    bridge_instance_uuid=bridge.bridge_instance_uuid,
                    identity_generation=bridge.identity_generation,
                    enrollment_secret=bridge.enrollment_secret,
                )
                report["writer_gate"] = gate_evidence
            else:
                gate = session.execute(
                    """
                    SELECT "gate_uuid", "state", "acquired_at",
                           "lease_expires_at", "released_at"
                    FROM "m_messenger_writer_gates_v1"
                    WHERE "project_id" = %s
                    """,
                    (args.project_id,),
                ).fetchone()
                acks = (
                    []
                    if gate is None
                    else session.execute(
                        """
                    SELECT "writer_class", "instance_id", "acknowledged_at",
                           "lease_expires_at"
                    FROM "m_messenger_writer_gate_acks_v1"
                    WHERE "gate_uuid" = %s
                    ORDER BY "writer_class", "instance_id"
                    """,
                        (gate["gate_uuid"],),
                    ).fetchall()
                )
                report = {
                    "project_id": str(args.project_id),
                    "gate": gate,
                    "acks": acks,
                }
        _write_report(report, args.output)
        return 0 if report.get("ok", True) else 2
    with _coordinator(args.project_id) as coordinator:
        if args.command == "inventory":
            capture = coordinator.inventory()
            report = {
                **capture.snapshot.report(),
                "quarantined": [
                    {
                        "source_kind": item.source_kind,
                        "source_position": item.source_position,
                        "error_code": item.error_code,
                        "error_summary": item.error_summary,
                        "record_sha256": item.record_sha256,
                    }
                    for item in capture.quarantined
                ],
                "event_watermarks": capture.event_watermarks,
            }
        elif args.command == "stage":
            capture = coordinator.inventory()
            with _database_context():
                phase = coordinator.target.stage(args.run_id, capture)
            report = {**capture.snapshot.report(), "phase": phase}
        elif args.command == "apply":
            totals: dict[str, typing.Any] = {
                "processed": 0,
                "changed": 0,
                "remaining": 1,
            }
            while totals["remaining"]:
                with _database_context():
                    batch = coordinator.apply_batch(args.run_id, args.batch_size)
                totals["processed"] += batch["processed"]
                totals["changed"] += batch["changed"]
                totals["remaining"] = batch["remaining"]
                if batch["processed"] == 0 and batch["remaining"]:
                    break
            totals["ok"] = totals["remaining"] == 0
            report = totals
        elif args.command == "freeze":
            with _database_context():
                frozen_at = coordinator.freeze(args.run_id, gate_uuid=args.gate_id)
            report = {"run_id": str(args.run_id), "frozen_at": frozen_at}
        elif args.command == "final-delta":
            with _database_context():
                capture, phase = coordinator.final_delta(args.run_id)
            report = {**capture.snapshot.report(), "phase": phase}
        elif args.command == "parity":
            with _database_context():
                report = coordinator.parity(args.run_id)
        _write_report(report, args.output)
        return 0 if report.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
