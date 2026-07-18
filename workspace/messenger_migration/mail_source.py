# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import dataclasses
import datetime
import hashlib
import uuid as sys_uuid
from typing import Any, cast

from workspace.messenger_mail import protocol
from workspace.messenger_mail import repository as mail_repository
from workspace.messenger_migration import snapshot as migration_snapshot


@dataclasses.dataclass(frozen=True)
class QuarantinedRecord:
    source_kind: str
    source_position: str
    error_code: str
    error_summary: str
    record_sha256: str | None


@dataclasses.dataclass(frozen=True)
class SourceCapture:
    snapshot: migration_snapshot.CanonicalSnapshot
    quarantined: tuple[QuarantinedRecord, ...] = ()
    event_watermarks: dict[str, dict[str, Any]] = dataclasses.field(
        default_factory=dict
    )


class MailProjectionSource:
    """Read-only adapter over the transitional Maildir journal."""

    def __init__(
        self,
        repository: mail_repository.MessengerMailRepository,
        *,
        now: datetime.datetime | None = None,
    ) -> None:
        self.repository = repository
        self.now = now or datetime.datetime.now(datetime.timezone.utc)

    @staticmethod
    def _bounded_error(exc: Exception) -> str:
        text = " ".join(str(exc).split())
        return text[:512]

    def _state_projection(
        self,
    ) -> tuple[mail_repository.Projection, tuple[QuarantinedRecord, ...]]:
        repo = self.repository
        repo.imap_client.ensure_mailbox(repo.state_mailbox)
        metadata = repo.imap_client.select(repo.state_mailbox)
        if metadata.uid_validity is None:
            raise mail_repository.UidValidityChanged("State mailbox has no UIDVALIDITY")
        projection = mail_repository.Projection()
        projection.uid_validity = metadata.uid_validity
        projection.uid_next = metadata.uid_next
        projection.highest_modseq = metadata.highest_modseq
        quarantined = []
        uids = sorted(repo.imap_client.search("ALL"))
        for message in sorted(
            repo.imap_client.fetch(uids), key=lambda value: value.uid
        ):
            raw = message.raw_message
            projection.journal_uids.add(message.uid)
            try:
                record = mail_repository.decode_operation(raw)
                repo._check_project(record.project_uuid)
                position = protocol.AppendUid(metadata.uid_validity, message.uid)
                projection.apply(record, position)
            except Exception as exc:
                quarantined.append(
                    QuarantinedRecord(
                        source_kind="operation",
                        source_position=str(message.uid),
                        error_code=type(exc).__name__,
                        error_summary=self._bounded_error(exc),
                        record_sha256=hashlib.sha256(raw).hexdigest(),
                    )
                )
        repo.projection = projection
        return projection, tuple(quarantined)

    @staticmethod
    def _referenced_users(
        projection: mail_repository.Projection,
    ) -> tuple[sys_uuid.UUID, ...]:
        values: set[sys_uuid.UUID] = set()
        for binding in projection.bindings.values():
            values.add(sys_uuid.UUID(str(binding["user_uuid"])))
            values.add(sys_uuid.UUID(str(binding["who_uuid"])))
        for user_uuid, _message_uuid in projection.message_states:
            values.add(user_uuid)
        for collection in (projection.messages, projection.folders, projection.files):
            for payload in collection.values():
                payload_user_uuid = payload.get("user_uuid")
                if payload_user_uuid is not None:
                    values.add(sys_uuid.UUID(str(payload_user_uuid)))
        for payload in projection.folder_items.values():
            payload_user_uuid = payload.get("user_uuid")
            if payload_user_uuid is not None:
                values.add(sys_uuid.UUID(str(payload_user_uuid)))
        return tuple(sorted(values, key=str))

    def _event_items(
        self,
        user_uuids: tuple[sys_uuid.UUID, ...],
    ) -> tuple[
        list[migration_snapshot.SnapshotItem],
        list[QuarantinedRecord],
        dict[str, dict[str, Any]],
    ]:
        repo = self.repository
        items: list[migration_snapshot.SnapshotItem] = []
        quarantined: list[QuarantinedRecord] = []
        watermarks: dict[str, dict[str, Any]] = {}
        cutoff = self.now - mail_repository.EVENT_RETENTION
        for user_uuid in user_uuids:
            path = repo.event_mailbox(user_uuid)
            repo.imap_client.ensure_mailbox(path)
            metadata = repo.imap_client.select(path)
            if metadata.uid_validity is None:
                quarantined.append(
                    QuarantinedRecord(
                        source_kind="event",
                        source_position=f"{user_uuid}:mailbox",
                        error_code="UidValidityChanged",
                        error_summary="Event mailbox has no UIDVALIDITY",
                        record_sha256=None,
                    )
                )
                continue
            all_uids = tuple(sorted(repo.imap_client.search("ALL")))
            watermarks[str(user_uuid)] = {
                "source_epoch_generation": str(metadata.uid_validity),
                "source_current_epoch_version": max(0, (metadata.uid_next or 1) - 1),
                "source_minimum_epoch_version": min(all_uids) if all_uids else None,
                "destination_strategy": "new_generation_with_retained_suffix",
            }
            for message in sorted(
                repo.imap_client.fetch(list(all_uids)),
                key=lambda value: value.uid,
            ):
                raw = message.raw_message
                try:
                    record = mail_repository.decode_event(raw)
                    repo._check_project(record.project_uuid)
                    if record.user_uuid != user_uuid:
                        raise mail_repository.InvalidJournalRecord(
                            "Event belongs to another user"
                        )
                    if record.occurred_at < cutoff:
                        continue
                    payload = {
                        "uuid": record.event_uuid,
                        "project_id": record.project_uuid,
                        "user_uuid": record.user_uuid,
                        "schema_version": record.schema_version,
                        "object_type": record.object_type,
                        "action": record.action,
                        "payload": record.payload,
                        "created_at": record.occurred_at,
                        "updated_at": record.occurred_at,
                    }
                    items.append(
                        migration_snapshot.SnapshotItem(
                            "events", str(record.event_uuid), "upsert", payload
                        )
                    )
                except Exception as exc:
                    quarantined.append(
                        QuarantinedRecord(
                            source_kind="event",
                            source_position=f"{user_uuid}:{message.uid}",
                            error_code=type(exc).__name__,
                            error_summary=self._bounded_error(exc),
                            record_sha256=hashlib.sha256(raw).hexdigest(),
                        )
                    )
        return items, quarantined, watermarks

    def capture(self) -> SourceCapture:
        projection, quarantined = self._state_projection()
        project_id = self.repository.project_uuid
        items: list[migration_snapshot.SnapshotItem] = []
        collections = (
            ("streams", projection.streams),
            ("bindings", projection.bindings),
            ("topics", projection.topics),
            ("messages", projection.messages),
            ("reactions", projection.reactions),
            ("folders", projection.folders),
            ("folder_items", projection.folder_items),
            ("files", projection.files),
        )
        for name, values in collections:
            for entity_uuid, payload in values.items():
                items.append(
                    migration_snapshot.SnapshotItem(
                        name,
                        str(entity_uuid),
                        "upsert",
                        cast(dict[str, object], migration_snapshot.normalize(payload)),
                    )
                )
        for (user_uuid, message_uuid), payload in projection.message_states.items():
            items.append(
                migration_snapshot.SnapshotItem(
                    "message_states",
                    f"{user_uuid}:{message_uuid}",
                    "upsert",
                    cast(
                        dict[str, object],
                        migration_snapshot.normalize(
                            {"uuid": message_uuid, "user_uuid": user_uuid, **payload}
                        ),
                    ),
                )
            )
        for message_uuid in projection.message_tombstones:
            items = [
                item
                for item in items
                if not (
                    item.collection == "messages"
                    and item.entity_key == str(message_uuid)
                )
            ]
            items.append(
                migration_snapshot.SnapshotItem(
                    "messages", str(message_uuid), "delete", None
                )
            )
        user_uuids = self._referenced_users(projection)
        for user_uuid in user_uuids:
            items.append(
                migration_snapshot.SnapshotItem(
                    "user_references",
                    str(user_uuid),
                    "upsert",
                    {"uuid": str(user_uuid)},
                )
            )
        event_items, event_quarantine, event_watermarks = self._event_items(user_uuids)
        items.extend(event_items)
        checkpoint = migration_snapshot.SourceCheckpoint(
            uid_validity=projection.uid_validity,
            checkpoint_uid=max(projection.journal_uids, default=0),
            uid_next=projection.uid_next,
            highest_modseq=projection.highest_modseq,
        )
        return SourceCapture(
            snapshot=migration_snapshot.CanonicalSnapshot(
                project_id=project_id,
                checkpoint=checkpoint,
                items=tuple(
                    sorted(items, key=lambda item: (item.collection, item.entity_key))
                ),
            ),
            quarantined=tuple((*quarantined, *event_quarantine)),
            event_watermarks=event_watermarks,
        )
