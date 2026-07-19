# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import uuid as sys_uuid
from typing import Any, cast

from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.api import resource_projection
from workspace.messenger_api.dm import models
from workspace.messenger_migration import snapshot as migration_snapshot


COLLECTION_MODELS: dict[str, Any] = {
    "streams": models.WorkspaceStream,
    "bindings": models.WorkspaceStreamBinding,
    "topics": models.WorkspaceStreamTopic,
    "messages": models.WorkspaceMessage,
    "message_states": models.WorkspaceUserMessageFlags,
    "reactions": models.WorkspaceMessageReactions,
    "folders": models.Folder,
    "folder_items": models.FolderItem,
    "files": models.WorkspaceFile,
    "events": models.WorkspaceEvent,
    "user_references": models.WorkspaceUser,
}

_NATIVE_SOURCE = {"kind": "native"}


def _json(value: object) -> str:
    return json.dumps(migration_snapshot.normalize(value), sort_keys=True)


def _parity_payload(
    collection: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Project transitional Maildir values onto the canonical SQL contract.

    Keep unknown values by default so parity still reports data that PostgreSQL
    cannot represent.  Only the documented transitional aliases below are
    removed, and only when their canonical equivalent proves that no value is
    being discarded.
    """
    values = cast(dict[str, object], migration_snapshot.normalize(payload))
    result = values.copy()

    if collection == "streams" and "kind" in result:
        expected_kind = "direct" if result.get("direct_user_uuid") else "stream"
        if result["kind"] == expected_kind:
            result.pop("kind")

    if collection == "messages" and "author_uuid" in result:
        author_uuid = result["author_uuid"]
        if result.get("user_uuid", author_uuid) == author_uuid:
            result["user_uuid"] = author_uuid
            result.pop("author_uuid")

    if collection in {
        "folders",
        "folder_items",
        "files",
        "reactions",
    }:
        if (
            result.get("source_name") == "native"
            and result.get("source") == _NATIVE_SOURCE
        ):
            result.pop("source_name")
            result.pop("source")

    if (
        collection == "events"
        and result.get("updated_at") == result.get("created_at")
    ):
        # Maildir events only have ``occurred_at``.  The source adapter copied
        # it into both timestamp fields, while SQL owns ``updated_at`` and may
        # assign it a few microseconds after the preserved ``created_at``.
        result.pop("updated_at")

    model = COLLECTION_MODELS[collection]
    for name, value in tuple(result.items()):
        prop = model.properties.properties.get(name)
        if prop is None:
            continue
        typed = prop.get_property_type().from_simple_type(value)
        result[name] = resource_projection.simple(typed)
    return result


class PostgreSQLImportTarget:
    """Destination adapter; callers own the RESTAlchemy transaction context."""

    @property
    def session(self) -> Any:
        return contexts.Context().get_session()

    def stage(
        self,
        run_uuid: sys_uuid.UUID,
        capture: Any,
        *,
        final_delta: bool = False,
    ) -> str:
        session = self.session
        snapshot = capture.snapshot
        run = session.execute(
            """
            SELECT project_id, phase, source_uid_validity
            FROM m_messenger_import_runs_v1
            WHERE run_uuid = %s FOR UPDATE
            """,
            (run_uuid,),
        ).fetchone()
        if run is not None:
            if run["project_id"] != snapshot.project_id:
                raise ValueError("Import run UUID belongs to another project")
            if (
                run["source_uid_validity"] is not None
                and run["source_uid_validity"] != snapshot.checkpoint.uid_validity
            ):
                raise ValueError("Maildir UIDVALIDITY changed during import")
            allowed = {"inventory", "staged", "applying", "failed"}
            if final_delta:
                allowed = {"frozen", "final_delta"}
            if run["phase"] not in allowed:
                raise ValueError(
                    f"Cannot stage import while run is in {run['phase']} phase"
                )
        existing = session.execute(
            """
            SELECT collection, entity_key, operation, payload_sha256, status
            FROM m_messenger_import_items_v1
            WHERE run_uuid = %s
            """,
            (run_uuid,),
        ).fetchall()
        existing_by_key = {
            (row["collection"], row["entity_key"]): row for row in existing
        }
        incoming_keys = set()
        session.execute(
            """
            INSERT INTO m_messenger_import_runs_v1 (
                run_uuid, project_id, phase, source_uid_validity,
                source_checkpoint_uid, source_inventory, source_digest,
                s3_urn_inventory, source_event_watermarks
            ) VALUES (
                %s, %s, 'inventory', %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb
            )
            ON CONFLICT (run_uuid) DO UPDATE SET
                source_uid_validity = EXCLUDED.source_uid_validity,
                source_checkpoint_uid = EXCLUDED.source_checkpoint_uid,
                source_inventory = EXCLUDED.source_inventory,
                source_digest = EXCLUDED.source_digest,
                s3_urn_inventory = EXCLUDED.s3_urn_inventory,
                source_event_watermarks = EXCLUDED.source_event_watermarks,
                updated_at = NOW()
            """,
            (
                run_uuid,
                snapshot.project_id,
                snapshot.checkpoint.uid_validity,
                snapshot.checkpoint.checkpoint_uid,
                _json(snapshot.inventory),
                snapshot.state_digest,
                _json(snapshot.urn_inventory),
                _json(capture.event_watermarks),
            ),
        )
        for item in snapshot.items:
            key = (item.collection, item.entity_key)
            incoming_keys.add(key)
            old = existing_by_key.get(key)
            unchanged_applied = (
                old is not None
                and old["operation"] == item.operation
                and old["payload_sha256"] == item.payload_sha256
                and old["status"] == "applied"
            )
            message_timestamp_reconciliation = (
                item.collection == "messages"
                and item.operation == "upsert"
                and item.payload is not None
                and "created_at" in item.payload
            )
            session.execute(
                """
                INSERT INTO m_messenger_import_items_v1 (
                    run_uuid, collection, entity_key, operation, payload,
                    payload_sha256, status
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (run_uuid, collection, entity_key) DO UPDATE SET
                    operation = EXCLUDED.operation,
                    payload = EXCLUDED.payload,
                    payload_sha256 = EXCLUDED.payload_sha256,
                    status = EXCLUDED.status,
                    last_error = NULL,
                    updated_at = NOW()
                """,
                (
                    run_uuid,
                    item.collection,
                    item.entity_key,
                    item.operation,
                    _json(item.payload) if item.payload is not None else None,
                    item.payload_sha256,
                    (
                        "applied"
                        if unchanged_applied
                        and not message_timestamp_reconciliation
                        else "staged"
                    ),
                ),
            )
        if final_delta:
            for key, old in existing_by_key.items():
                if key in incoming_keys or old["operation"] == "delete":
                    continue
                session.execute(
                    """
                    UPDATE m_messenger_import_items_v1
                    SET operation = 'delete', payload = NULL,
                        payload_sha256 = %s, status = 'staged',
                        last_error = NULL, updated_at = NOW()
                    WHERE run_uuid = %s AND collection = %s AND entity_key = %s
                    """,
                    (
                        migration_snapshot.digest(None),
                        run_uuid,
                        key[0],
                        key[1],
                    ),
                )
        session.execute(
            "DELETE FROM m_messenger_import_quarantine_v1 WHERE run_uuid = %s",
            (run_uuid,),
        )
        for record in capture.quarantined:
            session.execute(
                """
                INSERT INTO m_messenger_import_quarantine_v1 (
                    run_uuid, source_kind, source_position, error_code,
                    error_summary, record_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_uuid, source_kind, source_position) DO UPDATE SET
                    error_code = EXCLUDED.error_code,
                    error_summary = EXCLUDED.error_summary,
                    record_sha256 = EXCLUDED.record_sha256
                """,
                (
                    run_uuid,
                    record.source_kind,
                    record.source_position,
                    record.error_code,
                    record.error_summary,
                    record.record_sha256,
                ),
            )
        phase = (
            "failed"
            if capture.quarantined
            else ("final_delta" if final_delta else "staged")
        )
        session.execute(
            """
            UPDATE m_messenger_import_runs_v1
            SET phase = %s, updated_at = NOW(), last_error = %s
            WHERE run_uuid = %s
            """,
            (
                phase,
                "Source quarantine is not empty" if capture.quarantined else None,
                run_uuid,
            ),
        )
        session.execute(
            """
            INSERT INTO m_messenger_import_checkpoints_v1 (
                run_uuid, phase, source_uid_validity, source_checkpoint_uid,
                snapshot_digest, details
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                run_uuid,
                phase,
                snapshot.checkpoint.uid_validity,
                snapshot.checkpoint.checkpoint_uid,
                snapshot.digest,
                _json({"quarantined": len(capture.quarantined)}),
            ),
        )
        return phase

    @staticmethod
    def _filters(
        project_id: sys_uuid.UUID,
        collection: str,
        entity_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if collection == "user_references":
            return {"uuid": dm_filters.EQ(sys_uuid.UUID(entity_key))}
        filters = {
            "uuid": dm_filters.EQ(
                sys_uuid.UUID(str(payload.get("uuid", entity_key)))
            ),
            "project_id": dm_filters.EQ(project_id),
        }
        if collection == "message_states":
            user_uuid, message_uuid = entity_key.split(":", 1)
            filters["uuid"] = dm_filters.EQ(sys_uuid.UUID(message_uuid))
            filters["user_uuid"] = dm_filters.EQ(sys_uuid.UUID(user_uuid))
        return filters

    def _apply_item(
        self,
        project_id: sys_uuid.UUID,
        collection: str,
        entity_key: str,
        operation: str,
        payload: dict[str, Any] | None,
    ) -> bool:
        model = COLLECTION_MODELS[collection]
        session = self.session
        filters = self._filters(project_id, collection, entity_key, payload or {})
        row = model.objects.get_one_or_none(filters=filters, session=session)
        if collection == "user_references":
            if row is None:
                raise ValueError(f"Referenced Workspace user {entity_key} is absent")
            return False
        if operation == "delete":
            if row is not None:
                row.delete(session=session)
                return True
            return False
        assert payload is not None
        values = {
            name: model.properties.properties[name]
            .get_property_type()
            .from_simple_type(value)
            for name, value in payload.items()
            if name in model.properties.properties and not name.startswith("_")
        }
        values["project_id"] = project_id
        if row is None:
            row = model(**values)
            row.insert(session=session)
            return True
        immutable = set(filters)
        message_created_at_changed = (
            collection == "messages"
            and "created_at" in values
            and row.created_at != values["created_at"]
        )
        changed = {
            name: value
            for name, value in values.items()
            if name not in immutable
            and not model.properties.properties[name]
            .get_property_class()
            .is_id_property()
            and not model.properties.properties[name]
            .get_kwargs()
            .get("read_only", False)
            and getattr(row, name, None) != value
        }
        if changed:
            row.update_dm(values=changed)
            row.update(session=session)
        if message_created_at_changed:
            session.execute(
                """
                UPDATE "m_workspace_messages"
                SET "created_at" = %s
                WHERE "project_id" = %s AND "uuid" = %s
                """,
                (values["created_at"], project_id, row.uuid),
            )
        return bool(changed or message_created_at_changed)

    def apply_batch(
        self,
        run_uuid: sys_uuid.UUID,
        batch_size: int = 500,
    ) -> dict[str, int]:
        session = self.session
        run = session.execute(
            """
            SELECT project_id, phase
            FROM m_messenger_import_runs_v1
            WHERE run_uuid = %s FOR UPDATE
            """,
            (run_uuid,),
        ).fetchone()
        if run is None:
            raise ValueError("Unknown Messenger import run")
        if run["phase"] == "failed":
            raise ValueError("Import run is blocked by quarantined source records")
        rows = session.execute(
            """
            SELECT collection, entity_key, operation, payload
            FROM m_messenger_import_items_v1
            WHERE run_uuid = %s AND status = 'staged'
            ORDER BY
                CASE
                    WHEN operation = 'upsert' THEN CASE collection
                        WHEN 'user_references' THEN 10
                        WHEN 'streams' THEN 20
                        WHEN 'bindings' THEN 30
                        WHEN 'topics' THEN 40
                        WHEN 'folders' THEN 50
                        WHEN 'folder_items' THEN 60
                        WHEN 'messages' THEN 70
                        WHEN 'message_states' THEN 80
                        WHEN 'reactions' THEN 90
                        WHEN 'files' THEN 100
                        WHEN 'events' THEN 110
                    END
                    ELSE CASE collection
                        WHEN 'events' THEN 210
                        WHEN 'files' THEN 220
                        WHEN 'reactions' THEN 230
                        WHEN 'message_states' THEN 240
                        WHEN 'messages' THEN 250
                        WHEN 'folder_items' THEN 260
                        WHEN 'folders' THEN 270
                        WHEN 'topics' THEN 280
                        WHEN 'bindings' THEN 290
                        WHEN 'streams' THEN 300
                        WHEN 'user_references' THEN 310
                    END
                END,
                entity_key
            LIMIT %s FOR UPDATE SKIP LOCKED
            """,
            (run_uuid, batch_size),
        ).fetchall()
        changed = 0
        for index, item in enumerate(rows):
            savepoint = f"messenger_import_item_{index}"
            session.execute(f"SAVEPOINT {savepoint}")
            try:
                changed += bool(
                    self._apply_item(
                        run["project_id"],
                        item["collection"],
                        item["entity_key"],
                        item["operation"],
                        item["payload"],
                    )
                )
                session.execute(
                    """
                    UPDATE m_messenger_import_items_v1
                    SET status = 'applied', attempts = attempts + 1,
                        applied_at = NOW(), last_error = NULL, updated_at = NOW()
                    WHERE run_uuid = %s AND collection = %s AND entity_key = %s
                    """,
                    (run_uuid, item["collection"], item["entity_key"]),
                )
                session.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception as exc:
                session.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                session.execute(
                    """
                    UPDATE m_messenger_import_items_v1
                    SET status = 'error', attempts = attempts + 1,
                        last_error = %s, updated_at = NOW()
                    WHERE run_uuid = %s AND collection = %s AND entity_key = %s
                    """,
                    (
                        " ".join(str(exc).split())[:512],
                        run_uuid,
                        item["collection"],
                        item["entity_key"],
                    ),
                )
                session.execute(f"RELEASE SAVEPOINT {savepoint}")
        remaining = session.execute(
            """
            SELECT COUNT(*) AS count
            FROM m_messenger_import_items_v1
            WHERE run_uuid = %s AND status != 'applied'
            """,
            (run_uuid,),
        ).fetchone()["count"]
        session.execute(
            """
            UPDATE m_messenger_import_runs_v1
            SET phase = %s, updated_at = NOW()
            WHERE run_uuid = %s
            """,
            ("staged" if remaining == 0 else "applying", run_uuid),
        )
        return {"processed": len(rows), "changed": changed, "remaining": remaining}

    @staticmethod
    def normalize_snapshot_for_parity(
        source_snapshot: migration_snapshot.CanonicalSnapshot,
    ) -> migration_snapshot.CanonicalSnapshot:
        bound_users = {
            (str(item.payload["stream_uuid"]), str(item.payload["user_uuid"]))
            for item in source_snapshot.items
            if item.collection == "bindings"
            and item.operation == "upsert"
            and item.payload is not None
        }

        def normalize_item_payload(
            item: migration_snapshot.SnapshotItem,
        ) -> dict[str, object] | None:
            if item.payload is None:
                return None
            payload = item.payload
            if item.collection == "topics" and "user_uuid" in payload:
                topic_user = (str(payload["stream_uuid"]), str(payload["user_uuid"]))
                if topic_user in bound_users:
                    # Topics became project-scoped canonical resources.
                    # Visibility is represented by the separately verified
                    # binding row, so this viewer-scoped alias is redundant.
                    payload = payload.copy()
                    payload.pop("user_uuid")
            return _parity_payload(item.collection, payload)

        return migration_snapshot.CanonicalSnapshot(
            project_id=source_snapshot.project_id,
            checkpoint=source_snapshot.checkpoint,
            items=tuple(
                migration_snapshot.SnapshotItem(
                    item.collection,
                    item.entity_key,
                    item.operation,
                    normalize_item_payload(item),
                )
                for item in source_snapshot.items
            ),
        )

    def capture_destination(
        self,
        project_id: sys_uuid.UUID,
        source_snapshot: migration_snapshot.CanonicalSnapshot,
    ) -> migration_snapshot.CanonicalSnapshot:
        items = []
        session = self.session
        for collection, model in COLLECTION_MODELS.items():
            source_items = [
                item
                for item in source_snapshot.items
                if item.collection == collection and item.operation == "upsert"
            ]
            if not source_items:
                continue
            for source_item in source_items:
                payload = source_item.payload
                assert payload is not None
                filters = self._filters(
                    project_id, collection, source_item.entity_key, payload
                )
                row = model.objects.get_one_or_none(filters=filters, session=session)
                if row is None:
                    continue
                values = resource_projection.as_dict(row, None)
                values = {
                    name: value for name, value in values.items() if name in payload
                }
                items.append(
                    migration_snapshot.SnapshotItem(
                        collection,
                        source_item.entity_key,
                        "upsert",
                        cast(dict[str, object], migration_snapshot.normalize(values)),
                    )
                )
        return migration_snapshot.CanonicalSnapshot(
            project_id=project_id,
            checkpoint=source_snapshot.checkpoint,
            items=tuple(
                sorted(items, key=lambda item: (item.collection, item.entity_key))
            ),
        )
