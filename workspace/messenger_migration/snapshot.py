# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import datetime
import enum
import hashlib
import json
import re
import typing
import uuid as sys_uuid


URN_RE = re.compile(r"urn:[A-Za-z0-9][^\s\]\[(){}<>\"']+")
JsonScalar: typing.TypeAlias = str | int | float | bool | None
JsonValue: typing.TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@typing.runtime_checkable
class _SupportsSimpleTypes(typing.Protocol):
    def dump_to_simple_types(self) -> object: ...


def normalize(value: object) -> JsonValue:
    """Return a stable JSON value without changing public payload values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, _SupportsSimpleTypes):
        value = value.dump_to_simple_types()
    if isinstance(value, dict):
        return {
            str(key): normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, set):
        return sorted((normalize(item) for item in value), key=canonical_json)
    if isinstance(value, (sys_uuid.UUID, enum.Enum)):
        return str(value.value if isinstance(value, enum.Enum) else value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            raise ValueError("Migration timestamps must be timezone-aware")
        return (
            value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"Unsupported migration value {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def extract_urns(value: object) -> tuple[str, ...]:
    urns: set[str] = set()

    def visit(item: JsonValue) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple, set)):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            urns.update(URN_RE.findall(item))

    visit(normalize(value))
    return tuple(sorted(urns))


@dataclasses.dataclass(frozen=True)
class SourceCheckpoint:
    uid_validity: int | None
    checkpoint_uid: int
    uid_next: int | None = None
    highest_modseq: int | None = None

    def as_dict(self) -> JsonValue:
        return normalize(self)


@dataclasses.dataclass(frozen=True)
class SnapshotItem:
    collection: str
    entity_key: str
    operation: str
    payload: dict[str, object] | None

    def __post_init__(self) -> None:
        if self.operation not in {"upsert", "delete"}:
            raise ValueError("Migration operation must be upsert or delete")
        if (self.operation == "upsert") != (self.payload is not None):
            raise ValueError("Upserts require payloads and deletes forbid them")

    @property
    def payload_sha256(self) -> str:
        return digest(self.payload)

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "collection": self.collection,
            "entity_key": self.entity_key,
            "operation": self.operation,
            "payload": normalize(self.payload),
            "payload_sha256": self.payload_sha256,
        }


@dataclasses.dataclass(frozen=True)
class CanonicalSnapshot:
    project_id: sys_uuid.UUID
    checkpoint: SourceCheckpoint
    items: tuple[SnapshotItem, ...]

    def __post_init__(self) -> None:
        keys = [(item.collection, item.entity_key) for item in self.items]
        if keys != sorted(keys):
            raise ValueError("Snapshot items must have deterministic order")
        if len(keys) != len(set(keys)):
            raise ValueError("Snapshot contains duplicate logical entities")

    @property
    def digest(self) -> str:
        return digest([item.as_dict() for item in self.items])

    @property
    def state_digest(self) -> str:
        return digest(
            [item.as_dict() for item in self.items if item.operation == "upsert"]
        )

    @property
    def tombstone_digest(self) -> str:
        return digest(
            [item.as_dict() for item in self.items if item.operation == "delete"]
        )

    @property
    def inventory(self) -> dict[str, dict[str, int | str]]:
        collections: dict[str, dict[str, object]] = {}
        for item in self.items:
            entry = collections.setdefault(
                item.collection, {"upsert": 0, "delete": 0, "items": []}
            )
            count = entry[item.operation]
            assert isinstance(count, int)
            entry[item.operation] = count + 1
            items = entry["items"]
            assert isinstance(items, list)
            items.append((item.entity_key, item.operation, item.payload_sha256))
        result: dict[str, dict[str, int | str]] = {}
        for name, entry in sorted(collections.items()):
            upsert = entry["upsert"]
            delete = entry["delete"]
            items = entry["items"]
            assert isinstance(upsert, int)
            assert isinstance(delete, int)
            assert isinstance(items, list)
            result[name] = {
                "upsert": upsert,
                "delete": delete,
                "digest": digest(items),
            }
        return result

    @property
    def urn_inventory(self) -> dict[str, int | str | tuple[str, ...]]:
        values: set[str] = set()
        for item in self.items:
            values.update(extract_urns(item.payload))
        sorted_values = tuple(sorted(values))
        return {
            "count": len(sorted_values),
            "digest": digest(sorted_values),
            "urns": sorted_values,
        }

    def report(self) -> dict[str, object]:
        return {
            "project_id": str(self.project_id),
            "checkpoint": self.checkpoint.as_dict(),
            "inventory": self.inventory,
            "digest": self.digest,
            "state_digest": self.state_digest,
            "tombstone_digest": self.tombstone_digest,
            "urn_inventory": self.urn_inventory,
        }
