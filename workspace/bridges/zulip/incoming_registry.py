# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Coalesce shared provider changes without dropping account-specific effects."""

import asyncio
import dataclasses
import hashlib
import json
import time


@dataclasses.dataclass
class Intention:
    key: str
    signature: str
    entity: str
    identifier: int
    version: int | None = None
    completed_at: float | None = None


def change(account: dict, event: dict) -> tuple[str, str, bool] | None:
    kind = event["type"]
    if kind == "message":
        message = event["message"]
        entity = ["message", message["id"]]
        shared = {
            key: message[key]
            for key in ("id", "sender_id", "type", "timestamp", "content")
        }
        if message["type"] == "stream":
            shared.update(stream_id=message["stream_id"], subject=message["subject"])
        else:
            shared["recipients"] = sorted(
                item["id"] for item in message["display_recipient"]
            )
        immutable = True
    elif kind == "update_message":
        # Topic moves/renames deliberately keep their existing path.
        if any(key in event for key in ("subject", "new_stream_id", "orig_subject")):
            return None
        if "content" not in event or "edit_timestamp" not in event:
            return None
        entity = ["message", event["message_id"]]
        shared = {
            key: event[key]
            for key in (
                "message_id",
                "edit_timestamp",
                "content",
                "orig_content",
                "rendering_only",
            )
            if key in event
        }
        immutable = False
    elif kind == "delete_message":
        identifiers = event.get("message_ids", [event.get("message_id")])
        entity = ["messages", sorted(identifiers)]
        shared = {"message_ids": sorted(identifiers)}
        immutable = True
    else:
        # Flags/subscriptions are personal. Unversioned toggles have no stable
        # change identity, so they retain the ordinary idempotent handler.
        return None
    scope = [str(account["project_id"]), str(account["provider_uuid"])]
    entity_key = json.dumps([*scope, entity], separators=(",", ":"))
    signature = hashlib.sha256(
        json.dumps(
            [*scope, kind, entity, shared], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return signature, entity_key, immutable


class Registry:
    def __init__(self, ttl: float = 1800, clock=time.monotonic):
        self.lock = asyncio.Lock()
        self.entries: dict[str, Intention] = {}
        self.by_signature: dict[str, list[str]] = {}
        self.latest: dict[str, int] = {}
        self.versions: dict[str, int] = {}
        self.seen: dict[tuple[str, str], int] = {}
        self.ttl, self.clock = ttl, clock

    def admit(
        self, account: dict, event: dict, identifier: int
    ) -> tuple[Intention | None, bool]:
        identity = change(account, event)
        if identity is None:
            return None, False
        signature, entity, immutable = identity
        source = (entity, str(account["uuid"]))
        previous = self.seen.get(source, 0)
        candidates = [
            self.entries[key]
            for key in self.by_signature.get(signature, [])
            if immutable or self.entries[key].identifier > previous
        ]
        # Without a provider version, a first observation cannot safely identify
        # an old occurrence after a different change intervened.
        version = event.get("edit_timestamp")
        if (
            not previous
            and not immutable
            and candidates
            and max(x.identifier for x in candidates) < self.latest[entity]
            and (version is None or version >= self.versions.get(entity, version))
        ):
            candidates = []
        if candidates:
            entry = min(candidates, key=lambda x: x.identifier)
            duplicate = True
        else:
            entry = Intention(
                f"{signature}:{identifier}", signature, entity, identifier, version
            )
            self.entries[entry.key] = entry
            self.by_signature.setdefault(signature, []).append(entry.key)
            self.latest[entity] = max(self.latest.get(entity, 0), identifier)
            if version is not None:
                self.versions[entity] = max(self.versions.get(entity, version), version)
            duplicate = False
        self.seen[source] = max(previous, entry.identifier)
        return entry, duplicate

    def complete(self, key: str) -> None:
        entry = self.entries.get(key)
        if entry is not None:
            entry.completed_at = self.clock()

    def remove(self, key: str) -> None:
        self.entries.pop(key, None)
        self.reindex()

    def reindex(self) -> None:
        self.by_signature, self.latest, self.versions = {}, {}, {}
        for entry in self.entries.values():
            self.by_signature.setdefault(entry.signature, []).append(entry.key)
            self.latest[entry.entity] = max(
                self.latest.get(entry.entity, 0), entry.identifier
            )
            if entry.version is not None:
                self.versions[entry.entity] = max(
                    self.versions.get(entry.entity, entry.version), entry.version
                )

    def clean(self) -> None:
        cutoff = self.clock() - self.ttl
        self.entries = {
            key: entry
            for key, entry in self.entries.items()
            if entry.completed_at is None or entry.completed_at > cutoff
        }
        self.reindex()
        entities = set(self.latest)
        self.seen = {
            key: value for key, value in self.seen.items() if key[0] in entities
        }

    def restore(self, events: list[dict]) -> None:
        for event in events:
            payload = event["payload"]
            if "_shared_key" not in payload:
                continue
            metadata = payload["_shared_key"]
            if not payload.get("_personal_only"):
                self.entries[metadata["key"]] = Intention(
                    metadata["key"],
                    metadata["signature"],
                    metadata["entity"],
                    metadata["identifier"],
                    metadata.get("version"),
                )
            source = (metadata["entity"], str(event["external_account_uuid"]))
            self.seen[source] = max(self.seen.get(source, 0), metadata["identifier"])
        self.reindex()
