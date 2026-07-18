# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import collections.abc
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import tempfile
import threading
import uuid as sys_uuid
from typing import Any

from workspace.external_bridge_control import pki


CONTROL_SCHEMA_VERSION = "v1"
CHANGE_RETENTION = datetime.timedelta(days=7)
SNAPSHOT_LIFETIME = datetime.timedelta(minutes=15)
DESIRED_RESOURCE_TYPES = (
    "custom_ca_bundle",
    "external_account",
    "external_chat_assignment",
    "external_provider_policy",
)
KNOWN_CAPABILITIES = {
    "messenger.chat_catalog",
    "messenger.file.transfer",
    "messenger.message.delete",
    "messenger.message.edit",
    "messenger.message.read",
    "messenger.message.send",
    "messenger.reaction.write",
    "messenger.stream.delete",
    "messenger.stream.rename",
    "messenger.topic.create",
    "messenger.topic.delete",
    "messenger.topic.rename",
}


class CursorExpiredError(RuntimeError):
    def __init__(self, reason: str, snapshot_generation: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.snapshot_generation = snapshot_generation


class SnapshotExpiredError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StateConflictError(RuntimeError):
    pass


class BridgeForbiddenError(RuntimeError):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _atomic_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


class PersistentControlState:
    """Crash-safe desired/observed control state with signed opaque cursors."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        realm_uuid: str | sys_uuid.UUID,
    ) -> None:
        self.root = pathlib.Path(root)
        self.realm_uuid = sys_uuid.UUID(str(realm_uuid))
        self.path = self.root / "control-state.json"
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            state = self.root.lstat()
            if (
                self.root.is_symlink()
                or not self.root.is_dir()
                or state.st_mode & 0o077
            ):
                raise ValueError(
                    "Control state directory must be a real mode-0700 directory"
                )
            if not self.path.exists():
                _atomic_json(
                    self.path,
                    {
                        "schema_version": 1,
                        "realm_uuid": str(self.realm_uuid),
                        "cursor_key": secrets.token_hex(32),
                        "snapshot_generation": 1,
                        "sequence": 0,
                        "resources": {},
                        "changes": [],
                        "pruned_through_sequence": {},
                        "snapshots": {},
                        "heartbeats": {},
                        "observed_reports": {},
                        "observed_resources": {},
                        "file_transfers": {},
                    },
                )
            current = self._read()
            if current["schema_version"] != 1 or current["realm_uuid"] != str(
                self.realm_uuid
            ):
                raise ValueError("Persistent control state belongs to another realm")
            if "pruned_through_sequence" not in current:
                current["pruned_through_sequence"] = {}
                self._write(current)

    def initial_cursor(
        self,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str] | None = None,
    ) -> str:
        state = self._read()
        return self._encode_cursor(
            state,
            identity,
            self._normalize_types(resource_types),
            sequence=0,
            snapshot_generation=state["snapshot_generation"],
        )

    def signing_key(self) -> bytes:
        return bytes.fromhex(self._read()["cursor_key"])

    def authorize_identity(self, identity: pki.BridgeIdentity) -> None:
        """The file-backed test repository has no independent identity model."""
        if identity.realm_uuid != self.realm_uuid:
            raise BridgeForbiddenError("Bridge identity belongs to another realm")

    def upsert_resource(
        self,
        identity: pki.BridgeIdentity,
        resource: dict[str, Any],
        required_capabilities: dict[str, Any] | None = None,
        now: datetime.datetime | None = None,
    ) -> str:
        now = now or _utcnow()
        resource_type = resource["resource_type"]
        resource_uuid = str(sys_uuid.UUID(resource["uuid"]))
        generation = int(resource["generation"])
        if resource_type not in DESIRED_RESOURCE_TYPES or generation < 1:
            raise ValueError("Desired resource identity is invalid")
        with self._lock:
            state = self._read()
            key = self._resource_key(identity, resource_type, resource_uuid)
            previous = state["resources"].get(key)
            if previous is not None and previous["generation"] >= generation:
                return previous["change_uuid"]
            state["sequence"] += 1
            change_uuid = str(sys_uuid.uuid4())
            entry = {
                "change_uuid": change_uuid,
                "sequence": state["sequence"],
                "resource_type": resource_type,
                "resource_uuid": resource_uuid,
                "operation": "upsert",
                "generation": generation,
                "required_capabilities": required_capabilities or {},
                "resource": resource,
                "created_at": _timestamp(now),
                "bridge_instance_uuid": str(identity.bridge_instance_uuid),
                "provider_kind": identity.provider_kind,
            }
            state["resources"][key] = entry
            state["changes"].append(entry)
            self._prune(state, now)
            self._write(state)
            return change_uuid

    def delete_resource(
        self,
        identity: pki.BridgeIdentity,
        resource_type: str,
        resource_uuid: str | sys_uuid.UUID,
        generation: int,
        now: datetime.datetime | None = None,
    ) -> str:
        now = now or _utcnow()
        resource_uuid = str(sys_uuid.UUID(str(resource_uuid)))
        if resource_type not in DESIRED_RESOURCE_TYPES or generation < 1:
            raise ValueError("Desired resource identity is invalid")
        with self._lock:
            state = self._read()
            key = self._resource_key(identity, resource_type, resource_uuid)
            previous = state["resources"].get(key)
            if previous is not None and previous["generation"] >= generation:
                return previous["change_uuid"]
            state["sequence"] += 1
            entry = {
                "change_uuid": str(sys_uuid.uuid4()),
                "sequence": state["sequence"],
                "resource_type": resource_type,
                "resource_uuid": resource_uuid,
                "operation": "delete",
                "generation": generation,
                "created_at": _timestamp(now),
                "bridge_instance_uuid": str(identity.bridge_instance_uuid),
                "provider_kind": identity.provider_kind,
            }
            state["resources"].pop(key, None)
            state["changes"].append(entry)
            self._prune(state, now)
            self._write(state)
            return entry["change_uuid"]

    def changes(
        self,
        identity: pki.BridgeIdentity,
        cursor: str,
        resource_types: collections.abc.Iterable[str] | None = None,
        limit: int = 200,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        types = self._normalize_types(resource_types)
        if not 1 <= limit <= 500:
            raise ValueError("Change page limit is invalid")
        with self._lock:
            state = self._read()
            self._prune(state, now)
            checkpoint = self._decode_cursor(state, cursor, identity, types)
            watermark = max(
                (
                    state["pruned_through_sequence"].get(
                        self._watermark_key(identity, resource_type),
                        0,
                    )
                    for resource_type in types
                ),
                default=0,
            )
            if checkpoint["sequence"] < watermark:
                raise CursorExpiredError("retention", state["snapshot_generation"])
            eligible = self._eligible_changes(state, identity, types)
            later = [
                item for item in eligible if item["sequence"] > checkpoint["sequence"]
            ]
            selected = later[:limit]
            next_sequence = (
                selected[-1]["sequence"] if selected else checkpoint["sequence"]
            )
            next_cursor = self._encode_cursor(
                state,
                identity,
                types,
                next_sequence,
                state["snapshot_generation"],
            )
            self._write(state)
            return {
                "control_schema_version": CONTROL_SCHEMA_VERSION,
                "snapshot_generation": state["snapshot_generation"],
                "current_cursor": cursor,
                "next_cursor": next_cursor,
                "changes": [self._wire_change(item) for item in selected],
                "retained_since": _timestamp(now - CHANGE_RETENTION),
            }

    def create_snapshot(
        self,
        identity: pki.BridgeIdentity,
        request_uuid: str | sys_uuid.UUID,
        resource_types: collections.abc.Iterable[str] | None = None,
        now: datetime.datetime | None = None,
    ) -> tuple[dict[str, object], bool]:
        now = now or _utcnow()
        request_uuid = str(sys_uuid.UUID(str(request_uuid)))
        types = self._normalize_types(resource_types)
        with self._lock:
            state = self._read()
            scope = self._scope(identity, types)
            for snapshot in state["snapshots"].values():
                if (
                    snapshot["request_uuid"] == request_uuid
                    and snapshot["scope"] == scope
                    and _parse_timestamp(snapshot["expires_at"]) > now
                ):
                    return self._wire_snapshot(snapshot), False
            token = secrets.token_urlsafe(32)
            anchor = self._encode_cursor(
                state,
                identity,
                types,
                state["sequence"],
                state["snapshot_generation"],
            )
            resources = [
                item["resource"]
                for item in self._eligible_resources(state, identity, types)
            ]
            resources.sort(key=lambda item: (item["resource_type"], item["uuid"]))
            snapshot = {
                "request_uuid": request_uuid,
                "snapshot_token": token,
                "anchor_cursor": anchor,
                "snapshot_generation": state["snapshot_generation"],
                "resource_types": list(types),
                "expires_at": _timestamp(now + SNAPSHOT_LIFETIME),
                "scope": scope,
                "resources": resources,
            }
            state["snapshots"][token] = snapshot
            self._remove_expired_snapshots(state, now)
            self._write(state)
            return self._wire_snapshot(snapshot), True

    def snapshot_page(
        self,
        identity: pki.BridgeIdentity,
        token: str,
        page_cursor: str | None = None,
        limit: int = 200,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        if not 1 <= limit <= 500:
            raise ValueError("Snapshot page limit is invalid")
        with self._lock:
            state = self._read()
            snapshot = state["snapshots"].get(token)
            if snapshot is None:
                raise SnapshotExpiredError("unknown")
            if _parse_timestamp(snapshot["expires_at"]) <= now:
                state["snapshots"].pop(token, None)
                self._write(state)
                raise SnapshotExpiredError("expired")
            expected = self._scope(identity, tuple(snapshot["resource_types"]))
            if snapshot["scope"] != expected:
                raise SnapshotExpiredError("scope_mismatch")
            offset = 0
            if page_cursor is not None:
                page = self._decode_page_cursor(state, page_cursor, token)
                offset = page["offset"]
            resources = snapshot["resources"][offset : offset + limit]
            next_offset = offset + len(resources)
            next_cursor = (
                None
                if next_offset >= len(snapshot["resources"])
                else self._encode_page_cursor(state, token, next_offset)
            )
            return {
                "snapshot_generation": snapshot["snapshot_generation"],
                "anchor_cursor": snapshot["anchor_cursor"],
                "resources": resources,
                "next_page_cursor": next_cursor,
                "complete": next_cursor is None,
            }

    def heartbeat(
        self,
        identity: pki.BridgeIdentity,
        request: dict[str, Any],
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utcnow()
        if request["provider_kind"] != identity.provider_kind:
            raise StateConflictError("Heartbeat provider does not match certificate")
        heartbeat_uuid = str(sys_uuid.UUID(request["heartbeat_uuid"]))
        canonical = hashlib.sha256(
            json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self._lock:
            state = self._read()
            existing = state["heartbeats"].get(heartbeat_uuid)
            if existing is not None and existing["canonical"] != canonical:
                raise StateConflictError("Heartbeat UUID was reused")
            capabilities = {
                name: descriptor
                for name, descriptor in request["capabilities"].items()
                if name in KNOWN_CAPABILITIES
            }
            response = {
                "heartbeat_uuid": heartbeat_uuid,
                "received_at": _timestamp(now),
                "instance_state": "incompatible"
                if request["blocked_batch"]
                else "healthy",
                "negotiated_capabilities": capabilities,
                "poll_interval_seconds": 2,
                "heartbeat_interval_seconds": 10,
                "degraded_after_seconds": 30,
                "offline_after_seconds": 60,
                "snapshot_generation": state["snapshot_generation"],
                "ca_migration": {
                    "active_ca_generations": [1],
                    "renewal_required": False,
                    "overlap_ends_at": None,
                },
                "incompatibility": (
                    request["blocked_batch"]["safe_error"]
                    if request["blocked_batch"]
                    else None
                ),
            }
            state["heartbeats"][heartbeat_uuid] = {
                "canonical": canonical,
                "response": response,
                "bridge_instance_uuid": str(identity.bridge_instance_uuid),
            }
            self._write(state)
            return response

    def observed_reports(
        self,
        identity: pki.BridgeIdentity,
        reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not 1 <= len(reports) <= 500:
            raise ValueError("Observed report batch size is invalid")
        with self._lock:
            state = self._read()
            results = []
            for report in reports:
                report_uuid = str(sys_uuid.UUID(report["report_uuid"]))
                canonical = hashlib.sha256(
                    json.dumps(report, separators=(",", ":"), sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest()
                existing = state["observed_reports"].get(report_uuid)
                if existing is not None:
                    status = "duplicate" if existing == canonical else "rejected"
                    error = (
                        None
                        if status == "duplicate"
                        else {
                            "code": "report_uuid_reused",
                            "message": "Report UUID was reused with different input",
                            "retryable": False,
                        }
                    )
                else:
                    key = self._resource_key(
                        identity,
                        report["resource_type"],
                        report["resource_uuid"],
                    )
                    previous = state["observed_resources"].get(key)
                    if (
                        previous is not None
                        and previous["observed_generation"]
                        > report["observed_generation"]
                    ):
                        status = "stale"
                    else:
                        status = "applied"
                        state["observed_resources"][key] = report
                    error = None
                    state["observed_reports"][report_uuid] = canonical
                results.append(
                    {"report_uuid": report_uuid, "status": status, "safe_error": error}
                )
            self._write(state)
            return {"results": results}

    def assignment(
        self,
        identity: pki.BridgeIdentity,
        external_account_uuid: str | sys_uuid.UUID,
        external_chat_uuid: str | sys_uuid.UUID,
    ) -> dict[str, object] | None:
        account_uuid = str(sys_uuid.UUID(str(external_account_uuid)))
        chat_uuid = str(sys_uuid.UUID(str(external_chat_uuid)))
        state = self._read()
        account_key = self._resource_key(identity, "external_account", account_uuid)
        chat_key = self._resource_key(identity, "external_chat_assignment", chat_uuid)
        account = state["resources"].get(account_key)
        chat = state["resources"].get(chat_key)
        if (
            account is None
            or chat is None
            or chat["resource"]["external_account_uuid"] != account_uuid
        ):
            return None
        return {
            "account": account["resource"],
            "chat": chat["resource"],
        }

    def file_transfer_get(self, key: str) -> object | None:
        return self._read()["file_transfers"].get(key)

    def file_transfer_put(self, key: str, value: object) -> None:
        with self._lock:
            state = self._read()
            state["file_transfers"][key] = value
            self._write(state)

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, value: object) -> None:
        _atomic_json(self.path, value)

    @staticmethod
    def _normalize_types(
        resource_types: collections.abc.Iterable[str] | None,
    ) -> tuple[str, ...]:
        if resource_types is None:
            return DESIRED_RESOURCE_TYPES
        values = tuple(sorted(resource_types))
        if not values or len(set(values)) != len(values):
            raise ValueError("Desired resource type filter is invalid")
        if any(value not in DESIRED_RESOURCE_TYPES for value in values):
            raise ValueError("Desired resource type filter is invalid")
        return values

    def _scope(
        self,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
    ) -> dict[str, object]:
        return {
            "realm_uuid": str(self.realm_uuid),
            "bridge_instance_uuid": str(identity.bridge_instance_uuid),
            "provider_kind": identity.provider_kind,
            "resource_types": list(resource_types),
            "control_schema_version": CONTROL_SCHEMA_VERSION,
        }

    def _encode_cursor(
        self,
        state: dict[str, Any],
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
        sequence: int,
        snapshot_generation: int,
    ) -> str:
        payload = {
            **self._scope(identity, resource_types),
            "sequence": sequence,
            "snapshot_generation": snapshot_generation,
        }
        return self._sign(state, payload)

    def _decode_cursor(
        self,
        state: dict[str, Any],
        cursor: str,
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
    ) -> dict[str, Any]:
        try:
            payload = self._verify(state, cursor)
        except ValueError as error:
            raise CursorExpiredError(
                "schema_mismatch", state["snapshot_generation"]
            ) from error
        if payload.get("control_schema_version") != CONTROL_SCHEMA_VERSION:
            raise CursorExpiredError("schema_mismatch", state["snapshot_generation"])
        expected = self._scope(identity, resource_types)
        if any(payload.get(key) != value for key, value in expected.items()):
            raise CursorExpiredError("scope_mismatch", state["snapshot_generation"])
        if payload.get("snapshot_generation") != state["snapshot_generation"]:
            raise CursorExpiredError(
                "generation_mismatch", state["snapshot_generation"]
            )
        return payload

    def _encode_page_cursor(
        self, state: dict[str, Any], token: str, offset: int
    ) -> str:
        return self._sign(
            state, {"kind": "snapshot_page", "token": token, "offset": offset}
        )

    def _decode_page_cursor(
        self, state: dict[str, Any], value: str, token: str
    ) -> dict[str, Any]:
        try:
            payload = self._verify(state, value)
        except ValueError as error:
            raise SnapshotExpiredError("schema_mismatch") from error
        if payload.get("kind") != "snapshot_page" or payload.get("token") != token:
            raise SnapshotExpiredError("scope_mismatch")
        return payload

    @staticmethod
    def _sign(state: dict[str, Any], payload: dict[str, Any]) -> str:
        content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        signature = hmac.new(
            bytes.fromhex(state["cursor_key"]), content, hashlib.sha256
        ).digest()
        return (
            base64.urlsafe_b64encode(content + signature).rstrip(b"=").decode("ascii")
        )

    @staticmethod
    def _verify(state: dict[str, Any], value: str) -> dict[str, Any]:
        if not isinstance(value, str) or not value or "=" in value:
            raise ValueError("Invalid cursor")
        try:
            decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            content, signature = decoded[:-32], decoded[-32:]
            expected = hmac.new(
                bytes.fromhex(state["cursor_key"]), content, hashlib.sha256
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid cursor signature")
            return json.loads(content)
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid cursor") from error

    def _eligible_changes(
        self,
        state: dict[str, Any],
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in state["changes"]
            if item["bridge_instance_uuid"] == str(identity.bridge_instance_uuid)
            and item["provider_kind"] == identity.provider_kind
            and item["resource_type"] in resource_types
        ]

    def _eligible_resources(
        self,
        state: dict[str, Any],
        identity: pki.BridgeIdentity,
        resource_types: collections.abc.Iterable[str],
    ) -> list[dict[str, Any]]:
        return [
            item
            for item in state["resources"].values()
            if item["bridge_instance_uuid"] == str(identity.bridge_instance_uuid)
            and item["provider_kind"] == identity.provider_kind
            and item["resource_type"] in resource_types
        ]

    @staticmethod
    def _wire_change(
        item: collections.abc.Mapping[str, object],
    ) -> dict[str, object]:
        return {
            key: value
            for key, value in item.items()
            if key not in {"created_at", "bridge_instance_uuid", "provider_kind"}
        }

    @staticmethod
    def _wire_snapshot(
        snapshot: collections.abc.Mapping[str, object],
    ) -> dict[str, object]:
        return {
            key: value
            for key, value in snapshot.items()
            if key not in {"scope", "resources"}
        }

    @staticmethod
    def _resource_key(
        identity: pki.BridgeIdentity,
        resource_type: str,
        resource_uuid: str,
    ) -> str:
        return (
            f"{identity.bridge_instance_uuid}:{identity.provider_kind}:"
            f"{resource_type}:{resource_uuid}"
        )

    @staticmethod
    def _watermark_key(identity: pki.BridgeIdentity, resource_type: str) -> str:
        return (
            f"{identity.bridge_instance_uuid}:{identity.provider_kind}:{resource_type}"
        )

    @staticmethod
    def _prune(state: dict[str, Any], now: datetime.datetime) -> None:
        cutoff = now - CHANGE_RETENTION
        retained = []
        watermarks = state.setdefault("pruned_through_sequence", {})
        for item in state["changes"]:
            if _parse_timestamp(item["created_at"]) >= cutoff:
                retained.append(item)
                continue
            key = (
                f"{item['bridge_instance_uuid']}:{item['provider_kind']}:"
                f"{item['resource_type']}"
            )
            watermarks[key] = max(watermarks.get(key, 0), item["sequence"])
        state["changes"] = retained
        PersistentControlState._remove_expired_snapshots(state, now)

    @staticmethod
    def _remove_expired_snapshots(
        state: dict[str, Any], now: datetime.datetime
    ) -> None:
        state["snapshots"] = {
            key: value
            for key, value in state["snapshots"].items()
            if _parse_timestamp(value["expires_at"]) > now
        }
