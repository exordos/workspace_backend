# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
import uuid as sys_uuid
from typing import Any, cast

from workspace.messenger_api import file_storage
from workspace.messenger_migration import snapshot as migration_snapshot
from workspace.messenger_migration import writer_gate


class FileObjectVerifier:
    """Read-only binary and sidecar verifier; never logs object contents."""

    def verify(
        self,
        snapshot: migration_snapshot.CanonicalSnapshot,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for item in snapshot.items:
            if item.collection != "files" or item.operation != "upsert":
                continue
            payload = cast(dict[str, object], item.payload)
            file_uuid = __import__("uuid").UUID(item.entity_key)
            try:
                binary = file_storage.read_workspace_file(
                    file_uuid,
                    storage_type=cast(str | None, payload.get("storage_type")),
                    storage_object_id=cast(
                        str | None, payload.get("storage_object_id")
                    ),
                )
                metadata = file_storage.read_workspace_file_metadata(
                    file_uuid,
                    storage_type=cast(str | None, payload.get("storage_type")),
                )
                binary_sha256 = hashlib.sha256(binary).hexdigest()
                expected = str(payload.get("hash", ""))
                expected = expected.removeprefix("sha256:")
                sidecar = metadata.to_json()
                ok = (
                    binary_sha256 == expected
                    and len(binary) == payload.get("size_bytes")
                    and str(metadata.uuid) == item.entity_key
                    and metadata.sha256 == expected
                )
                results.append(
                    {
                        "file_uuid": item.entity_key,
                        "storage_type": payload.get("storage_type"),
                        "binary_sha256": binary_sha256,
                        "sidecar_sha256": hashlib.sha256(sidecar).hexdigest(),
                        "ok": ok,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "file_uuid": item.entity_key,
                        "storage_type": payload.get("storage_type"),
                        "error_code": type(exc).__name__,
                        "ok": False,
                    }
                )
        return {
            "checked": len(results),
            "failed": sum(not item["ok"] for item in results),
            "objects": results,
            "ok": all(item["ok"] for item in results),
        }


class ImportCoordinator:
    """Orchestration without transaction ownership or automatic cutover."""

    def __init__(
        self,
        source: Any,
        target: Any,
        file_verifier: FileObjectVerifier | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.file_verifier = file_verifier

    def inventory(self) -> Any:
        return self.source.capture()

    def stage(self, run_uuid: sys_uuid.UUID) -> tuple[Any, str]:
        capture = self.source.capture()
        phase = self.target.stage(run_uuid, capture)
        return capture, phase

    def apply_batch(
        self,
        run_uuid: sys_uuid.UUID,
        batch_size: int = 500,
    ) -> dict[str, int]:
        return self.target.apply_batch(run_uuid, batch_size=batch_size)

    @staticmethod
    def _source_signature(
        capture: Any,
    ) -> tuple[int | None, int, int | None, int | None, str]:
        checkpoint = capture.snapshot.checkpoint
        return (
            checkpoint.uid_validity,
            checkpoint.checkpoint_uid,
            checkpoint.uid_next,
            checkpoint.highest_modseq,
            capture.snapshot.digest,
        )

    @staticmethod
    def _checkpoint_gate(details: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(details, str):
            parsed: dict[str, Any] = json.loads(details)
        else:
            parsed = details
        gate = (parsed or {}).get("writer_gate")
        if not gate or not gate.get("gate_id"):
            raise ValueError("Import checkpoint has no authoritative writer gate")
        return gate

    def freeze(
        self,
        run_uuid: sys_uuid.UUID,
        *,
        gate_uuid: sys_uuid.UUID,
        now: datetime.datetime | None = None,
    ) -> datetime.datetime:
        session = self.target.session
        row = session.execute(
            """
            SELECT phase, project_id FROM m_messenger_import_runs_v1
            WHERE run_uuid = %s FOR UPDATE
            """,
            (run_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown Messenger import run")
        pending = session.execute(
            """
            SELECT COUNT(*) AS count
            FROM m_messenger_import_items_v1
            WHERE run_uuid = %s AND status != 'applied'
            """,
            (run_uuid,),
        ).fetchone()["count"]
        quarantined = session.execute(
            """
            SELECT COUNT(*) AS count
            FROM m_messenger_import_quarantine_v1
            WHERE run_uuid = %s
            """,
            (run_uuid,),
        ).fetchone()["count"]
        if pending or quarantined:
            raise ValueError(
                "Freeze is blocked by pending items or quarantined records"
            )
        now = now or datetime.datetime.now(datetime.timezone.utc)
        proof = writer_gate.validate_closed_gate(
            session,
            row["project_id"],
            gate_uuid,
            now=now,
        )
        first = self.source.capture()
        if first.snapshot.project_id != row["project_id"]:
            raise ValueError("Source capture belongs to another project")
        second = self.source.capture()
        if first.quarantined or second.quarantined:
            raise ValueError("Freeze is blocked by quarantined source records")
        if self._source_signature(first) != self._source_signature(second):
            raise ValueError("Source advanced while acquiring the writer freeze")
        session.execute(
            """
            UPDATE m_messenger_import_runs_v1
            SET phase = 'frozen', freeze_confirmed_at = %s, updated_at = NOW()
            WHERE run_uuid = %s
            """,
            (now, run_uuid),
        )
        session.execute(
            """
            INSERT INTO m_messenger_import_checkpoints_v1 (
                run_uuid, phase, source_uid_validity, source_checkpoint_uid,
                snapshot_digest, details
            ) VALUES (%s, 'frozen', %s, %s, %s, %s::jsonb)
            """,
            (
                run_uuid,
                second.snapshot.checkpoint.uid_validity,
                second.snapshot.checkpoint.checkpoint_uid,
                second.snapshot.digest,
                json.dumps({"writer_gate": proof}, sort_keys=True),
            ),
        )
        return now

    def final_delta(self, run_uuid: sys_uuid.UUID) -> tuple[Any, str]:
        session = self.target.session
        run = session.execute(
            """
            SELECT phase, project_id, source_uid_validity, freeze_confirmed_at
            FROM m_messenger_import_runs_v1
            WHERE run_uuid = %s FOR UPDATE
            """,
            (run_uuid,),
        ).fetchone()
        if (
            run is None
            or run["phase"] != "frozen"
            or run["freeze_confirmed_at"] is None
        ):
            raise ValueError("Import run has no recorded freeze confirmation")
        frozen = session.execute(
            """
            SELECT source_uid_validity, source_checkpoint_uid, snapshot_digest,
                   details
            FROM m_messenger_import_checkpoints_v1
            WHERE run_uuid = %s AND phase = 'frozen'
            ORDER BY sequence DESC LIMIT 1
            """,
            (run_uuid,),
        ).fetchone()
        if frozen is None:
            raise ValueError("Import run has no machine-verified writer gate")
        gate = self._checkpoint_gate(frozen["details"])
        writer_gate.validate_closed_gate(
            session,
            run["project_id"],
            __import__("uuid").UUID(gate["gate_id"]),
        )
        capture = self.source.capture()
        current = capture.snapshot.checkpoint
        if (
            run["source_uid_validity"] is not None
            and current.uid_validity != run["source_uid_validity"]
        ):
            raise ValueError("Maildir UIDVALIDITY changed after the base snapshot")
        if (
            current.uid_validity != frozen["source_uid_validity"]
            or current.checkpoint_uid != frozen["source_checkpoint_uid"]
            or capture.snapshot.digest != frozen["snapshot_digest"]
        ):
            raise ValueError("Source advanced after the writer gate was acquired")
        phase = self.target.stage(run_uuid, capture, final_delta=True)
        session.execute(
            """
            UPDATE m_messenger_import_checkpoints_v1
            SET details = details || %s::jsonb
            WHERE sequence = (
                SELECT sequence FROM m_messenger_import_checkpoints_v1
                WHERE run_uuid = %s AND phase = 'final_delta'
                ORDER BY sequence DESC LIMIT 1
            )
            """,
            (json.dumps({"writer_gate": gate}, sort_keys=True), run_uuid),
        )
        return capture, phase

    def parity(self, run_uuid: sys_uuid.UUID) -> dict[str, Any]:
        frozen = self.target.session.execute(
            """
            SELECT checkpoint.source_uid_validity,
                   checkpoint.source_checkpoint_uid,
                   checkpoint.snapshot_digest, checkpoint.details,
                   import_run.project_id
            FROM m_messenger_import_checkpoints_v1 AS checkpoint
            JOIN m_messenger_import_runs_v1 AS import_run
              ON import_run.run_uuid = checkpoint.run_uuid
            WHERE checkpoint.run_uuid = %s
              AND checkpoint.phase = 'final_delta'
            ORDER BY checkpoint.sequence DESC LIMIT 1
            """,
            (run_uuid,),
        ).fetchone()
        if frozen is None:
            raise ValueError("Parity requires an applied final-delta checkpoint")
        gate = self._checkpoint_gate(frozen["details"])
        writer_gate.validate_closed_gate(
            self.target.session,
            frozen["project_id"],
            __import__("uuid").UUID(gate["gate_id"]),
        )
        source = self.source.capture()
        current = source.snapshot.checkpoint
        if (
            current.uid_validity != frozen["source_uid_validity"]
            or current.checkpoint_uid != frozen["source_checkpoint_uid"]
            or source.snapshot.digest != frozen["snapshot_digest"]
        ):
            raise ValueError("Source advanced during final apply or parity")
        parity_source = self.target.normalize_snapshot_for_parity(source.snapshot)
        destination = self.target.capture_destination(
            parity_source.project_id, parity_source
        )
        source_items = {
            (item.collection, item.entity_key): item.payload_sha256
            for item in parity_source.items
            if item.operation == "upsert"
        }
        destination_items = {
            (item.collection, item.entity_key): item.payload_sha256
            for item in destination.items
        }
        missing = sorted(set(source_items) - set(destination_items))
        extra = sorted(set(destination_items) - set(source_items))
        conflicting = sorted(
            key
            for key in set(source_items) & set(destination_items)
            if source_items[key] != destination_items[key]
        )
        file_count = sum(
            item.collection == "files" and item.operation == "upsert"
            for item in source.snapshot.items
        )
        if self.file_verifier is None:
            file_report = {
                "checked": 0,
                "failed": file_count,
                "ok": file_count == 0,
                "blocked": file_count > 0,
            }
        else:
            file_report = self.file_verifier.verify(source.snapshot)
        report = {
            "project_id": str(source.snapshot.project_id),
            "source_state_digest": parity_source.state_digest,
            "destination_state_digest": destination.state_digest,
            "source_inventory": parity_source.inventory,
            "destination_inventory": destination.inventory,
            "source_urn_inventory": parity_source.urn_inventory,
            "destination_urn_inventory": destination.urn_inventory,
            "tombstone_digest": source.snapshot.tombstone_digest,
            "missing": [list(key) for key in missing],
            "extra": [list(key) for key in extra],
            "conflicting": [list(key) for key in conflicting],
            "quarantined": len(source.quarantined),
            "file_objects": file_report,
            "event_watermarks": source.event_watermarks,
            "ok": not (
                missing
                or extra
                or conflicting
                or source.quarantined
                or not file_report["ok"]
            ),
        }
        session = self.target.session
        session.execute(
            """
            UPDATE m_messenger_import_runs_v1
            SET phase = %s, destination_inventory = %s::jsonb,
                destination_digest = %s, updated_at = NOW(), last_error = %s
            WHERE run_uuid = %s
            """,
            (
                "parity_verified" if report["ok"] else "failed",
                __import__("json").dumps(destination.inventory, sort_keys=True),
                destination.state_digest,
                None if report["ok"] else "Canonical parity verification failed",
                run_uuid,
            ),
        )
        return report
