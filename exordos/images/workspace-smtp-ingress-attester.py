#!/usr/bin/env python3

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Stop and drain Exim before attesting a closed Messenger writer gate."""

import argparse
import configparser
import datetime
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid as sys_uuid


LOG = logging.getLogger("workspace.smtp_ingress_attester")
DEFAULT_CONFIG = "/etc/workspace/smtp-writer-gate.conf"
COMPATIBILITY_MARKER = pathlib.Path(
    "/etc/workspace/smtp-writer-gate.compatibility"
)
ENFORCED_MARKER = pathlib.Path("/etc/workspace/smtp-writer-gate.enforced")
DEFAULT_HOLD_PATH = (
    "/var/lib/workspace/messenger/mail/.writer-gate/smtp-ingress-hold.json"
)


class AttestationError(RuntimeError):
    pass


class Configuration:
    def __init__(self, path):
        parser = configparser.ConfigParser()
        if not parser.read(path):
            raise AttestationError(f"Writer-gate configuration is absent: {path}")
        section = parser["smtp_writer_gate"]
        self.connection_url = section["connection_url"]
        self.poll_interval = section.getfloat("poll_interval_seconds", 5.0)
        self.drain_timeout = section.getfloat("drain_timeout_seconds", 120.0)
        self.hold_path = pathlib.Path(section.get("hold_path", DEFAULT_HOLD_PATH))
        self.instance_id = section.get(
            "instance_id", f"smtp_ingress:{socket.gethostname()}"
        )


class PostgreSQLGateClient:
    def __init__(self, connection_url, instance_id, runner=subprocess.run):
        self._environment = self._postgres_environment(connection_url)
        self._instance_id = instance_id
        self._runner = runner

    @staticmethod
    def _postgres_environment(connection_url):
        parsed = urllib.parse.urlsplit(connection_url)
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise AttestationError("Writer-gate connection must use PostgreSQL")
        if parsed.hostname is None or parsed.path in {"", "/"}:
            raise AttestationError("Writer-gate PostgreSQL endpoint is incomplete")
        environment = os.environ.copy()
        environment.update(
            {
                "PGCONNECT_TIMEOUT": "5",
                "PGHOST": parsed.hostname,
                "PGPORT": str(parsed.port or 5432),
                "PGDATABASE": urllib.parse.unquote(parsed.path.lstrip("/")),
                "PGUSER": urllib.parse.unquote(parsed.username or ""),
                "PGPASSWORD": urllib.parse.unquote(parsed.password or ""),
            }
        )
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for parameter, environment_name in {
            "connect_timeout": "PGCONNECT_TIMEOUT",
            "options": "PGOPTIONS",
            "sslmode": "PGSSLMODE",
        }.items():
            if parameter in query:
                environment[environment_name] = query[parameter][-1]
        return environment

    def _query(self, sql, **variables):
        command = [
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--field-separator=\t",
        ]
        for name, value in variables.items():
            command.extend(("--set", f"{name}={value}"))
        try:
            result = self._runner(
                command,
                input=sql,
                text=True,
                capture_output=True,
                check=False,
                env=self._environment,
                timeout=10,
            )
        except subprocess.TimeoutExpired as exc:
            raise AttestationError(
                "Writer-gate database transaction timed out"
            ) from exc
        if result.returncode != 0:
            summary = result.stderr.strip().splitlines()[-1:]
            raise AttestationError(
                "Writer-gate database transaction failed"
                + (f": {summary[0]}" if summary else "")
            )
        return tuple(
            tuple(field.strip() for field in line.split("\t"))
            for line in result.stdout.splitlines()
            if line.strip()
        )

    def heartbeat(self):
        rows = self._query(
            """
            BEGIN;
            INSERT INTO "m_messenger_writer_instances_v1" (
                "writer_class", "instance_id", "started_at", "heartbeat_at",
                "lease_expires_at"
            ) VALUES (
                'smtp_ingress', :'instance_id', NOW(), NOW(),
                NOW() + INTERVAL '45 seconds'
            )
            ON CONFLICT ("writer_class", "instance_id") DO UPDATE
            SET "heartbeat_at" = EXCLUDED."heartbeat_at",
                "lease_expires_at" = EXCLUDED."lease_expires_at";
            SELECT gate."project_id", gate."gate_uuid", gate."lease_expires_at"
            FROM "m_messenger_writer_gates_v1" AS gate
            JOIN "m_messenger_writer_gate_expected_v1" AS expected
              ON expected."gate_uuid" = gate."gate_uuid"
             AND expected."writer_class" = 'smtp_ingress'
             AND expected."instance_id" = :'instance_id'
            WHERE gate."state" = 'closed'
              AND gate."lease_expires_at" > NOW()
            ORDER BY gate."project_id";
            COMMIT;
            """,
            instance_id=self._instance_id,
        )
        return tuple(
            {
                "project_id": sys_uuid.UUID(project_id),
                "gate_id": sys_uuid.UUID(gate_id),
                "lease_expires_at": lease_expires_at,
            }
            for project_id, gate_id, lease_expires_at in rows
        )

    def acknowledge(self, project_id, gate_id):
        rows = self._query(
            """
            BEGIN;
            SELECT pg_advisory_xact_lock(hashtextextended(:'project_id', 0));
            WITH authoritative_gate AS (
                SELECT gate."gate_uuid", gate."lease_expires_at"
                FROM "m_messenger_writer_gates_v1" AS gate
                JOIN "m_messenger_writer_gate_expected_v1" AS expected
                  ON expected."gate_uuid" = gate."gate_uuid"
                 AND expected."writer_class" = 'smtp_ingress'
                 AND expected."instance_id" = :'instance_id'
                JOIN "m_messenger_writer_instances_v1" AS instance
                  ON instance."writer_class" = expected."writer_class"
                 AND instance."instance_id" = expected."instance_id"
                WHERE gate."project_id" = :'project_id'::uuid
                  AND gate."gate_uuid" = :'gate_id'::uuid
                  AND gate."state" = 'closed'
                  AND gate."lease_expires_at" > NOW()
                  AND instance."lease_expires_at" > NOW()
            ), acknowledged AS (
                INSERT INTO "m_messenger_writer_gate_acks_v1" (
                    "gate_uuid", "writer_class", "instance_id",
                    "acknowledged_at", "lease_expires_at"
                )
                SELECT "gate_uuid", 'smtp_ingress', :'instance_id', NOW(),
                       LEAST("lease_expires_at", NOW() + INTERVAL '2 minutes')
                FROM authoritative_gate
                ON CONFLICT ("gate_uuid", "writer_class", "instance_id")
                DO UPDATE SET
                    "acknowledged_at" = EXCLUDED."acknowledged_at",
                    "lease_expires_at" = EXCLUDED."lease_expires_at"
                RETURNING "gate_uuid"
            )
            SELECT "gate_uuid" FROM acknowledged;
            COMMIT;
            """,
            instance_id=self._instance_id,
            project_id=project_id,
            gate_id=gate_id,
        )
        if rows != ((str(gate_id),),):
            raise AttestationError(
                f"Writer gate {gate_id} is stale, replaced, or no longer expected"
            )

    def exact_gate_is_released(self, gate_id):
        rows = self._query(
            """
            SELECT "state", "released_at" IS NOT NULL
            FROM "m_messenger_writer_gates_v1"
            WHERE "gate_uuid" = :'gate_id'::uuid;
            """,
            gate_id=gate_id,
        )
        return rows == (("open", "t"),)

    def any_live_closed_gate(self):
        rows = self._query(
            """
            SELECT EXISTS (
                SELECT 1 FROM "m_messenger_writer_gates_v1"
                WHERE "state" = 'closed' AND "lease_expires_at" > NOW()
            );
            """
        )
        return rows == (("t",),)


class EximBoundary:
    def __init__(
        self,
        runner=subprocess.run,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ):
        self._runner = runner
        self._monotonic = monotonic
        self._sleep = sleeper

    def _run(self, command, *, timeout=None):
        return self._runner(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def stop_accepting(self):
        result = self._run(("systemctl", "stop", "exim4.service"))
        if result.returncode != 0:
            raise AttestationError("Exim listener could not be stopped")

    def queue_count(self):
        result = self._run(("exim4", "-bpc"))
        try:
            return int(result.stdout.strip()) if result.returncode == 0 else -1
        except ValueError:
            return -1

    def active_processes(self):
        result = self._run(("exiwhat",))
        if result.returncode != 0:
            raise AttestationError("Exim process state could not be inspected")
        return tuple(line for line in result.stdout.splitlines() if line.strip())

    def listener_is_inactive(self):
        result = self._run(("systemctl", "is-active", "--quiet", "exim4.service"))
        if result.returncode == 0:
            return False
        if result.returncode == 3:
            return True
        raise AttestationError("Exim listener state could not be inspected")

    def prove_quiesced(self):
        return (
            self.listener_is_inactive()
            and not self.active_processes()
            and self.queue_count() == 0
        )

    def stop_and_drain(self, timeout):
        deadline = self._monotonic() + timeout
        self.stop_accepting()
        while self._monotonic() < deadline:
            if self.prove_quiesced():
                return
            remaining = max(1.0, deadline - self._monotonic())
            self._run(("exim4", "-qff"), timeout=remaining)
            self._sleep(min(1.0, remaining))
        raise AttestationError("Exim did not drain before the writer-gate timeout")

    def start(self):
        result = self._run(("systemctl", "start", "exim4.service"))
        if result.returncode != 0:
            raise AttestationError("Exim listener could not be resumed")
        if self.listener_is_inactive():
            raise AttestationError("Exim listener did not remain active after resume")


class SMTPIngressAttester:
    def __init__(self, config, database, exim, sleeper=time.sleep):
        self._config = config
        self._database = database
        self._exim = exim
        self._sleep = sleeper

    def _read_hold(self):
        try:
            return json.loads(self._config.hold_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    @staticmethod
    def _fsync_directory(path):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _hold_gate_ids(hold):
        try:
            return {sys_uuid.UUID(gate_id) for gate_id in hold["gate_ids"]}
        except (KeyError, TypeError, ValueError) as exc:
            raise AttestationError("SMTP ingress hold evidence is invalid") from exc

    def _write_hold(self, gates):
        body = {
            "schema_version": 1,
            "instance_id": self._config.instance_id,
            "gate_ids": sorted(str(gate["gate_id"]) for gate in gates),
            "held_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self._config.hold_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=self._config.hold_path.parent,
            prefix=".smtp-ingress-hold-",
            text=True,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(body, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._config.hold_path)
            self._fsync_directory(self._config.hold_path.parent)
        finally:
            pathlib.Path(temporary_path).unlink(missing_ok=True)

    def _remove_hold(self):
        try:
            self._config.hold_path.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(self._config.hold_path.parent)

    def run_once(self):
        gates = self._database.heartbeat()
        hold = self._read_hold()
        if not gates:
            if hold is not None:
                LOG.error("SMTP ingress remains held pending explicit resume")
            elif self._exim.listener_is_inactive():
                self._exim.start()
            return
        held_gate_ids = self._hold_gate_ids(hold) if hold is not None else set()
        held_gate_ids.update(gate["gate_id"] for gate in gates)
        self._write_hold(tuple({"gate_id": gate_id} for gate_id in held_gate_ids))
        if not self._exim.prove_quiesced():
            self._exim.stop_and_drain(self._config.drain_timeout)
        if not self._exim.prove_quiesced():
            raise AttestationError("Exim quiescence proof changed before ack")
        refreshed_gates = self._database.heartbeat()
        if {gate["gate_id"] for gate in refreshed_gates} != {
            gate["gate_id"] for gate in gates
        }:
            raise AttestationError("Writer-gate generation changed during drain")
        for gate in refreshed_gates:
            self._database.acknowledge(gate["project_id"], gate["gate_id"])

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception:
                LOG.exception("SMTP ingress writer-gate iteration failed closed")
            self._sleep(self._config.poll_interval)

    def exim_prestart(self):
        if self._read_hold() is not None:
            raise AttestationError("SMTP ingress is held by writer-gate evidence")
        if self._database.any_live_closed_gate():
            raise AttestationError("SMTP ingress cannot start while a gate is closed")

    def resume(self, gate_id):
        hold = self._read_hold()
        held_gate_ids = self._hold_gate_ids(hold) if hold is not None else set()
        if gate_id not in held_gate_ids:
            raise AttestationError("The requested gate does not own the SMTP hold")
        for held_gate_id in held_gate_ids:
            if not self._database.exact_gate_is_released(held_gate_id):
                raise AttestationError(
                    "Every SMTP writer gate in the hold must be released"
                )
        if self._database.any_live_closed_gate():
            raise AttestationError("Another live writer gate still blocks SMTP")
        self._remove_hold()
        try:
            self._exim.start()
        except Exception:
            self._write_hold(
                tuple({"gate_id": held_gate_id} for held_gate_id in held_gate_ids)
            )
            self._exim.stop_accepting()
            raise


def build_parser():
    parser = argparse.ArgumentParser(prog="workspace-smtp-ingress-attester")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run")
    subparsers.add_parser("exim-prestart")
    resume = subparsers.add_parser("resume")
    resume.add_argument("--gate-id", type=sys_uuid.UUID, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "exim-prestart" and not pathlib.Path(args.config).is_file():
        if pathlib.Path(DEFAULT_HOLD_PATH).exists():
            LOG.error("SMTP ingress is held by writer-gate evidence")
            return 1
        if ENFORCED_MARKER.is_file():
            LOG.error("Enforced writer-gate configuration is absent")
            return 1
        if COMPATIBILITY_MARKER.is_file() and (
            COMPATIBILITY_MARKER.read_text(encoding="utf-8").strip()
            == "compatibility-prestart-v1"
        ):
            LOG.info("Compatibility phase permits Exim before gate provisioning")
            return 0
        LOG.error("Writer-gate configuration is absent outside compatibility")
        return 1
    config = Configuration(args.config)
    database = PostgreSQLGateClient(config.connection_url, config.instance_id)
    attester = SMTPIngressAttester(config, database, EximBoundary())
    try:
        if args.command == "run":
            attester.run()
        elif args.command == "exim-prestart":
            attester.exim_prestart()
        else:
            attester.resume(args.gate_id)
    except AttestationError as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
