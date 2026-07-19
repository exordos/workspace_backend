# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Read-only PostgreSQL and object-storage inventory for scale fixtures."""

import collections
import datetime
import json
import uuid as sys_uuid

from workspace.messenger_api import file_storage
from workspace.tests.scale import fixture


INVENTORY_SCHEMA_VERSION = "workspace.messenger.fixture-actual-inventory/v1"


def _records(units):
    return [record for unit in units for record in unit["records"]]


def _iso(value):
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _digest(rows, fields):
    return fixture._digest_rows(iter(rows), fields)[1]


def _rows(session, statement, parameters):
    return [dict(row) for row in session.execute(statement, parameters).fetchall()]


def _one_by(rows, field="uuid"):
    return {str(row[field]): row for row in rows}


def _message_index(payload):
    content = payload.get("content") if isinstance(payload, dict) else None
    prefix = "fixture message "
    if not isinstance(content, str) or not content.startswith(prefix):
        return None
    try:
        return int(content[len(prefix) :])
    except ValueError:
        return None


def _event_bucket(created_at):
    older = fixture.EVENT_REFERENCE_TIME - datetime.timedelta(
        days=7,
        seconds=1,
    )
    boundary = fixture.EVENT_REFERENCE_TIME - datetime.timedelta(
        days=7,
    )
    newer = fixture.EVENT_REFERENCE_TIME - datetime.timedelta(
        days=6,
        seconds=86399,
    )
    if created_at == older:
        return "older_than_7d"
    if created_at == boundary:
        return "exact_7d_boundary"
    if created_at == newer:
        return "newer_than_7d"
    return "outside_fixture_buckets"


def _storage_read(reader):
    try:
        return reader()
    except FileNotFoundError:
        raise
    except Exception as error:
        response = getattr(error, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            raise FileNotFoundError from error
        raise


class ActualInventoryExporter:
    """Export only observed values; the manifest is used solely for comparison."""

    def __init__(self, identity_mappings, storage=None):
        self._logical_to_iam = {
            str(row["logical_user_uuid"]): str(row["iam_user_uuid"])
            for row in identity_mappings
        }
        self._iam_to_logical = {
            actual: logical for logical, actual in self._logical_to_iam.items()
        }
        self._storage = storage

    def _logical_user(self, value):
        value = str(value)
        return self._iam_to_logical.get(value, f"unmapped:{value}")

    def _query(self, session, run, units):
        records = _records(units)
        project_uuid = sys_uuid.UUID(run["test_project_id"])
        expected_by_kind = collections.defaultdict(list)
        for record in records:
            expected_by_kind[record["record_kind"]].append(record)
        account_ids = [
            sys_uuid.UUID(row["record_key"])
            for row in expected_by_kind["external_account"]
        ]
        actual_user_ids = [
            sys_uuid.UUID(value) for value in self._logical_to_iam.values()
        ]

        return expected_by_kind, {
            "users": _rows(
                session,
                "SELECT uuid FROM m_workspace_users "
                "WHERE uuid = ANY(%s::uuid[]) ORDER BY uuid",
                (actual_user_ids,),
            ),
            "streams": _rows(
                session,
                """
                SELECT uuid, user_uuid, private, invite_only, direct_user_uuid,
                       default_topic_uuid, external_account_uuid,
                       provider_external_id, provider_metadata
                FROM m_workspace_streams
                WHERE project_id = %s ORDER BY uuid
                """,
                (project_uuid,),
            ),
            "bindings": _rows(
                session,
                """
                SELECT uuid, stream_uuid, user_uuid, role
                FROM m_workspace_stream_bindings
                WHERE project_id = %s ORDER BY stream_uuid, uuid
                """,
                (project_uuid,),
            ),
            "topics": _rows(
                session,
                """
                SELECT uuid, stream_uuid, name, external_account_uuid,
                       provider_external_id
                FROM m_workspace_stream_topics
                WHERE project_id = %s ORDER BY stream_uuid, uuid
                """,
                (project_uuid,),
            ),
            "messages": _rows(
                session,
                """
                SELECT uuid, stream_uuid, topic_uuid, user_uuid, payload,
                       external_account_uuid, provider_external_id
                FROM m_workspace_messages
                WHERE project_id = %s ORDER BY uuid
                """,
                (project_uuid,),
            ),
            "reactions": _rows(
                session,
                """
                SELECT uuid, message_uuid, user_uuid, emoji_name
                FROM m_workspace_message_reactions
                WHERE project_id = %s ORDER BY uuid
                """,
                (project_uuid,),
            ),
            "files": _rows(
                session,
                """
                SELECT uuid, user_uuid, stream_uuid, size_bytes, hash,
                       storage_type, storage_id, storage_object_id
                FROM m_workspace_files
                WHERE project_id = %s ORDER BY uuid
                """,
                (project_uuid,),
            ),
            "events": _rows(
                session,
                """
                SELECT uuid, entity_uuid, audience_snapshot_uuid, payload,
                       created_at
                FROM m_workspace_broadcast_message_events_v1
                WHERE project_id = %s ORDER BY epoch_version
                """,
                (project_uuid,),
            ),
            "deliveries": _rows(
                session,
                """
                SELECT event.uuid AS event_uuid, member.user_uuid
                FROM m_workspace_broadcast_message_events_v1 AS event
                JOIN m_workspace_event_audience_members_v1 AS member
                  ON member.audience_snapshot_uuid = event.audience_snapshot_uuid
                WHERE event.project_id = %s
                ORDER BY event.epoch_version, member.user_uuid
                """,
                (project_uuid,),
            ),
            "accounts": (
                []
                if not actual_user_ids
                else _rows(
                    session,
                    """
                    SELECT uuid, owner_user_uuid, provider, settings
                    FROM m_external_accounts_v2
                    WHERE owner_user_uuid = ANY(%s::uuid[]) AND provider = 'zulip'
                    ORDER BY uuid
                    """,
                    (actual_user_ids,),
                )
            ),
            "chats": (
                []
                if not account_ids
                else _rows(
                    session,
                    """
                    SELECT uuid, external_account_uuid, owner_user_uuid,
                           provider_chat_id, source, selected, project_id,
                           projection_stream_uuid
                    FROM m_external_chats_v2
                    WHERE external_account_uuid = ANY(%s::uuid[])
                    ORDER BY uuid
                    """,
                    (account_ids,),
                )
            ),
            "provider_operations": (
                []
                if not account_ids
                else _rows(
                    session,
                    """
                    SELECT operation.uuid AS external_operation_uuid,
                           operation.external_account_uuid,
                           operation.owner_user_uuid,
                           operation.action,
                           operation.target_type,
                           operation.target_uuid,
                           operation.status AS public_status,
                           operation.attempt AS public_attempt,
                           queue.project_id,
                           queue.external_account_uuid AS queue_account_uuid,
                           queue.operation_kind,
                           queue.payload,
                           queue.status AS queue_status,
                           queue.attempt AS queue_attempt
                    FROM m_external_operations_v2 AS operation
                    JOIN m_external_provider_operations_v1 AS queue
                      ON queue.external_operation_uuid = operation.uuid
                    WHERE operation.external_account_uuid = ANY(%s::uuid[])
                      AND queue.project_id = %s
                    ORDER BY operation.uuid
                    """,
                    (account_ids, project_uuid),
                )
            ),
            "provider_events": (
                []
                if not account_ids
                else _rows(
                    session,
                    """
                    SELECT provider_event_uuid, external_account_uuid,
                           project_id, provider_sequence, event_kind,
                           payload_sha256, status, target_uuid
                    FROM m_external_provider_events_v1
                    WHERE external_account_uuid = ANY(%s::uuid[])
                      AND project_id = %s
                    ORDER BY provider_event_uuid
                    """,
                    (account_ids, project_uuid),
                )
            ),
        }

    def export(self, session, run, manifest, units):
        expected, actual = self._query(session, run, units)
        user_ids = {str(row["uuid"]) for row in actual["users"]}
        stream_by_id = _one_by(actual["streams"])
        bindings_by_stream = collections.defaultdict(list)
        for row in actual["bindings"]:
            bindings_by_stream[str(row["stream_uuid"])].append(row)
        topic_by_id = _one_by(actual["topics"])
        message_by_id = _one_by(actual["messages"])
        reaction_by_id = _one_by(actual["reactions"])
        file_by_id = _one_by(actual["files"])
        event_by_id = _one_by(actual["events"])
        account_by_id = _one_by(actual["accounts"])
        chat_by_id = _one_by(actual["chats"])
        outbound_by_message = {
            str(row["target_uuid"]): row
            for row in actual["provider_operations"]
            if row["operation_kind"] == "message.create"
        }

        user_rows = [
            {"uuid": record["record_key"]}
            for record in expected["user"]
            if self._logical_to_iam[record["record_key"]] in user_ids
        ]
        membership_rows = []
        stream_rows = []
        for record in expected["stream"]:
            logical = record["values"]
            stream = stream_by_id.get(record["record_key"])
            if stream is None:
                continue
            bindings = _one_by(bindings_by_stream[record["record_key"]])
            normalized_bindings = []
            for declared in logical["bindings"]:
                binding = bindings.get(declared["uuid"])
                if binding is None:
                    continue
                normalized = {
                    "uuid": str(binding["uuid"]),
                    "user_uuid": self._logical_user(binding["user_uuid"]),
                    "role": binding["role"],
                }
                normalized_bindings.append(normalized)
                membership_rows.append(
                    {
                        "stream_uuid": str(binding["stream_uuid"]),
                        "binding_uuid": str(binding["uuid"]),
                        "user_uuid": normalized["user_uuid"],
                        "role": binding["role"],
                    }
                )
            kind = (
                "direct_dm"
                if stream["direct_user_uuid"] is not None
                else "group_dm"
                if stream["private"]
                else "channel"
            )
            metadata = stream["provider_metadata"] or {}
            stream_rows.append(
                {
                    "uuid": str(stream["uuid"]),
                    "kind": kind,
                    "provider_synced": stream["external_account_uuid"] is not None,
                    "owner_user_uuid": self._logical_user(stream["user_uuid"]),
                    "private": stream["private"],
                    "invite_only": stream["invite_only"],
                    "default_topic_uuid": str(stream["default_topic_uuid"]),
                    "bindings": normalized_bindings,
                    "external_chat_uuid": metadata.get("external_chat_uuid"),
                    "provider_chat_key": stream["provider_external_id"],
                }
            )

        topic_rows = []
        for stream_record in expected["stream"]:
            for declared in stream_record["values"]["topics"]:
                topic = topic_by_id.get(declared["uuid"])
                if topic is not None:
                    topic_rows.append(
                        {
                            "stream_uuid": str(topic["stream_uuid"]),
                            "topic_uuid": str(topic["uuid"]),
                        }
                    )

        message_rows = []
        provider_mapping_rows = []
        provider_message_count = 0
        chat_by_stream = {
            str(row["projection_stream_uuid"]): row
            for row in actual["chats"]
            if row["projection_stream_uuid"] is not None
        }
        for record in expected["message"]:
            message = message_by_id.get(record["record_key"])
            if message is None:
                continue
            stream = stream_by_id.get(str(message["stream_uuid"]))
            recipients = [
                self._logical_user(binding["user_uuid"])
                for binding in bindings_by_stream[str(message["stream_uuid"])]
            ]
            declared_recipients = record["values"]["recipients"]
            recipient_set = set(recipients)
            recipients = [
                value for value in declared_recipients if value in recipient_set
            ] + sorted(recipient_set - set(declared_recipients))
            outbound = outbound_by_message.get(str(message["uuid"]))
            account_uuid = message["external_account_uuid"]
            if account_uuid is None and outbound is not None:
                account_uuid = outbound["external_account_uuid"]
            is_provider = account_uuid is not None
            chat = (
                chat_by_stream.get(str(message["stream_uuid"])) if is_provider else None
            )
            payload = message["payload"]
            normalized = {
                "uuid": str(message["uuid"]),
                "project_id": record["values"].get(
                    "project_id",
                    run["test_project_id"],
                ),
                "stream_uuid": str(message["stream_uuid"]),
                "topic_uuid": str(message["topic_uuid"]),
                "sender_uuid": self._logical_user(message["user_uuid"]),
                "recipients": recipients,
                "provider": is_provider,
                "external_account_uuid": (
                    None if account_uuid is None else str(account_uuid)
                ),
                "external_chat_uuid": (None if chat is None else str(chat["uuid"])),
                "provider_chat_key": (
                    None if stream is None else stream["provider_external_id"]
                )
                if is_provider
                else None,
                "payload_sha256": fixture.sha256(payload),
                "message_index": _message_index(payload),
            }
            message_rows.append(normalized)
            if is_provider:
                provider_message_count += 1
                provider_mapping_rows.append(
                    {
                        "account_uuid": normalized["external_account_uuid"],
                        "external_chat_uuid": normalized["external_chat_uuid"],
                        "stream_uuid": normalized["stream_uuid"],
                        "message_uuid": normalized["uuid"],
                    }
                )

        reaction_rows = []
        for record in expected["reaction"]:
            reaction = reaction_by_id.get(record["record_key"])
            if reaction is not None:
                reaction_rows.append(
                    {
                        "uuid": str(reaction["uuid"]),
                        "message_uuid": str(reaction["message_uuid"]),
                        "user_uuid": self._logical_user(reaction["user_uuid"]),
                        "emoji_name": reaction["emoji_name"],
                    }
                )

        account_rows = []
        for record in expected["external_account"]:
            account = account_by_id.get(record["record_key"])
            if account is None:
                continue
            settings = account["settings"]
            default_project_id = str(settings["default_project_id"])
            actual_project_id = str(run["test_project_id"])
            logical_project_id = record["values"]["settings"]["default_project_id"]
            account_rows.append(
                {
                    "uuid": str(account["uuid"]),
                    "owner_user_uuid": self._logical_user(account["owner_user_uuid"]),
                    "provider": account["provider"],
                    "selection_mode": settings["selection_mode"],
                    "history_depth": settings["history_depth"],
                    "default_project_id": (
                        logical_project_id
                        if default_project_id == actual_project_id
                        else f"unexpected:{default_project_id}"
                    ),
                }
            )

        event_rows = []
        event_delivery_rows = []
        delivery_by_event = collections.defaultdict(list)
        for row in actual["deliveries"]:
            delivery_by_event[str(row["event_uuid"])].append(row["user_uuid"])
        age_buckets = {
            "older_than_7d": 0,
            "exact_7d_boundary": 0,
            "newer_than_7d": 0,
            "outside_fixture_buckets": 0,
        }
        for record in expected["event"]:
            event = event_by_id.get(record["record_key"])
            if event is None:
                continue
            payload = event["payload"]
            event_kind = payload.get("kind")
            bucket = _event_bucket(event["created_at"])
            age_buckets[bucket] += 1
            event_rows.append(
                {
                    "event_uuid": str(event["uuid"]),
                    "event_kind": event_kind,
                    "message_uuid": str(event["entity_uuid"]),
                    "stream_uuid": str(payload.get("stream_uuid")),
                    "created_at": _iso(event["created_at"]),
                    "retention_policy": {
                        "kind": "fixed_reference_7d",
                        "reference_time": fixture.EVENT_REFERENCE_TIME.isoformat(),
                        "retention_days": 7,
                        "bucket": bucket,
                    },
                }
            )
            message_record = next(
                row
                for row in expected["message"]
                if row["record_key"] == str(event["entity_uuid"])
            )
            actual_recipients = {
                self._logical_user(value)
                for value in delivery_by_event[str(event["uuid"])]
            }
            declared = message_record["values"]["recipients"]
            ordered_recipients = [
                value for value in declared if value in actual_recipients
            ] + sorted(actual_recipients - set(declared))
            event_delivery_rows.extend(
                {
                    "event_uuid": str(event["uuid"]),
                    "event_kind": event_kind,
                    "message_uuid": str(event["entity_uuid"]),
                    "recipient_user_uuid": recipient,
                }
                for recipient in ordered_recipients
            )

        storage = self._storage or file_storage.get_workspace_file_storage("s3")
        file_rows = []
        s3_objects = []
        fixture_storage_ids = set()
        storage_faults = []
        for record in expected["file"]:
            database_file = file_by_id.get(record["record_key"])
            if database_file is None:
                continue
            file_uuid = str(database_file["uuid"])
            object_name = database_file["storage_object_id"]
            sidecar_name = file_storage.get_workspace_file_metadata_object_id(file_uuid)
            try:
                binary = _storage_read(lambda: storage.read(file_uuid, object_name))
                sidecar = json.loads(
                    _storage_read(lambda: storage.read_metadata(file_uuid)).to_json()
                )
            except (FileNotFoundError, ValueError) as error:
                storage_faults.append(f"{file_uuid}:{type(error).__name__}")
                continue
            binary_sha256 = fixture.sha256(binary)
            normalized_sidecar = json.loads(json.dumps(sidecar))
            declared_sidecar = record["values"].get("sidecar", {})
            if str(sidecar.get("project_id")) == run["test_project_id"]:
                normalized_sidecar["project_id"] = declared_sidecar.get(
                    "project_id",
                    run["test_project_id"],
                )
            normalized_sidecar["owner_uuid"] = self._logical_user(sidecar["owner_uuid"])
            sidecar_sha256 = fixture.sha256(normalized_sidecar)
            fixture_storage_ids.update((object_name, sidecar_name))
            normalized = {
                "uuid": file_uuid,
                "size_bytes": len(binary),
                "object_name": object_name,
                "binary_sha256": binary_sha256,
                "sidecar_object_name": sidecar_name,
                "sidecar_sha256": sidecar_sha256,
            }
            file_rows.append(normalized)
            s3_objects.append(
                {
                    **normalized,
                    "sidecar": sidecar,
                    "actual_sidecar_sha256": fixture.sha256(sidecar),
                    "normalized_sidecar": normalized_sidecar,
                }
            )

        extra_project_objects = []
        for object_id in storage.list_object_ids():
            if not object_id.startswith("metadata/") or not object_id.endswith(".json"):
                continue
            file_uuid = object_id.rsplit("/", 1)[-1][:-5]
            try:
                metadata = _storage_read(lambda: storage.read_metadata(file_uuid))
            except (FileNotFoundError, ValueError):
                continue
            if str(metadata.project_id) != run["test_project_id"]:
                continue
            binary_id = file_storage.get_workspace_file_object_id(file_uuid)
            for scoped_id in (object_id, binary_id):
                if scoped_id not in fixture_storage_ids:
                    extra_project_objects.append(scoped_id)

        counts = {
            "users": len(user_rows),
            "streams": len(actual["streams"]),
            "topics": len(actual["topics"]),
            "messages": len(actual["messages"]),
            "provider_messages": provider_message_count,
            "reactions": len(actual["reactions"]),
            "files": len(actual["files"]),
            "canonical_broadcast_events": len(actual["events"]),
            "visible_event_deliveries": len(actual["deliveries"]),
            "zulip_accounts": len(actual["accounts"]),
        }
        relationship_counts = {
            "stream_memberships": len(actual["bindings"]),
            "provider_synced_streams": sum(
                row["external_account_uuid"] is not None for row in actual["streams"]
            ),
        }
        digests = {
            "users": _digest(user_rows, ("uuid",)),
            "streams": _digest(
                stream_rows,
                (
                    "uuid",
                    "kind",
                    "provider_synced",
                    "owner_user_uuid",
                    "private",
                    "invite_only",
                    "default_topic_uuid",
                    "bindings",
                    "external_chat_uuid",
                    "provider_chat_key",
                ),
            ),
            "stream_memberships": _digest(
                membership_rows,
                ("stream_uuid", "binding_uuid", "user_uuid", "role"),
            ),
            "topics": _digest(topic_rows, ("stream_uuid", "topic_uuid")),
            "messages": _digest(
                message_rows, tuple(message_rows[0]) if message_rows else ()
            ),
            "reactions": _digest(
                reaction_rows,
                ("uuid", "message_uuid", "user_uuid", "emoji_name"),
            ),
            "files": _digest(
                file_rows,
                (
                    "uuid",
                    "size_bytes",
                    "object_name",
                    "binary_sha256",
                    "sidecar_object_name",
                    "sidecar_sha256",
                ),
            ),
            "canonical_broadcast_events": _digest(
                event_rows,
                tuple(event_rows[0]) if event_rows else (),
            ),
            "visible_event_deliveries": _digest(
                event_delivery_rows,
                tuple(event_delivery_rows[0]) if event_delivery_rows else (),
            ),
            "zulip_accounts": _digest(
                account_rows,
                (
                    "uuid",
                    "owner_user_uuid",
                    "provider",
                    "selection_mode",
                    "history_depth",
                    "default_project_id",
                ),
            ),
            "provider_mappings": _digest(
                provider_mapping_rows,
                tuple(provider_mapping_rows[0]) if provider_mapping_rows else (),
            ),
        }
        provider_counts = {
            "external_accounts": len(actual["accounts"]),
            "assigned_accounts": sum(row["selected"] for row in actual["chats"]),
            "streams": relationship_counts["provider_synced_streams"],
            "topics": sum(
                row["external_account_uuid"] is not None for row in actual["topics"]
            ),
            "messages": provider_message_count,
        }
        provider_stream_mappings = []
        for record in expected["external_chat"]:
            chat = chat_by_id.get(record["record_key"])
            if chat is None or not chat["selected"]:
                continue
            stream = stream_by_id.get(str(chat["projection_stream_uuid"]))
            if stream is None:
                continue
            declared_topics = record["values"]["workspace_projection"]["topics"]
            projection_topics = []
            for declared in declared_topics:
                topic = topic_by_id.get(declared["uuid"])
                if topic is None:
                    continue
                projection_topics.append(
                    {
                        "uuid": str(topic["uuid"]),
                        "provider_topic_id": topic["provider_external_id"],
                        "name": topic["name"],
                        "is_default": topic["uuid"] == stream["default_topic_uuid"],
                    }
                )
            provider_stream_mappings.append(
                {
                    "stream_uuid": str(stream["uuid"]),
                    "external_account_uuid": str(chat["external_account_uuid"]),
                    "external_chat_uuid": str(chat["uuid"]),
                    "provider_chat_key": chat["provider_chat_id"],
                    "provider_chat_type": chat["source"].get("chat_type"),
                    "projection_topics": projection_topics,
                    "owner_user_uuid": self._logical_user(chat["owner_user_uuid"]),
                }
            )
        expected_provider_rows = [
            record["values"].get("provider_operation")
            for record in expected["message"]
            if record["values"].get("provider_operation") is not None
        ]
        expected_provider_ledgers = fixture.provider_persistence_ledger_rows(
            expected_provider_rows
        )
        expected_provider_by_event = {
            row["provider_event_uuid"]: row for row in expected_provider_rows
        }
        expected_message_record_by_event = {
            record["values"].get("provider_operation")["provider_event_uuid"]: record
            for record in expected["message"]
            if record["values"].get("provider_operation") is not None
        }
        provider_event_ledger = []
        for row in actual["provider_events"]:
            event_uuid = str(row["provider_event_uuid"])
            expected_row = expected_provider_by_event.get(event_uuid)
            expected_message_record = expected_message_record_by_event.get(event_uuid)
            payload_verified = False
            if expected_row is not None and expected_message_record is not None:
                runtime_event = json.loads(
                    json.dumps(expected_row["provider_contract"]["event"])
                )
                runtime_event["project_id"] = run["test_project_id"]
                runtime_event["payload"]["resource"]["user_uuid"] = (
                    self._logical_to_iam[
                        expected_message_record["values"]["sender_uuid"]
                    ]
                )
                payload_verified = (
                    fixture.sha256(runtime_event) == row["payload_sha256"]
                )
            target_message = message_by_id.get(str(row["target_uuid"]))
            provider_event_ledger.append(
                {
                    "provider_event_uuid": event_uuid,
                    "external_account_uuid": str(row["external_account_uuid"]),
                    "project_id": (
                        expected_row["provider_contract"]["event"]["project_id"]
                        if expected_row is not None
                        and str(row["project_id"]) == run["test_project_id"]
                        else f"unexpected:{row['project_id']}"
                    ),
                    "provider_sequence": row["provider_sequence"],
                    "event_kind": row["event_kind"],
                    "target_uuid": str(row["target_uuid"]),
                    "status": row["status"],
                    "workspace_payload_sha256": (
                        None
                        if target_message is None
                        else fixture.sha256(target_message["payload"])
                    ),
                    "envelope_payload_sha256_verified": payload_verified,
                }
            )
        provider_operation_ledger = []
        expected_provider_by_operation = {
            row["operation_uuid"]: row for row in expected_provider_rows
        }
        for row in actual["provider_operations"]:
            operation_uuid = str(row["external_operation_uuid"])
            expected_row = expected_provider_by_operation.get(operation_uuid)
            target_message = message_by_id.get(str(row["target_uuid"]))
            provider_operation_ledger.append(
                {
                    "external_operation_uuid": operation_uuid,
                    "external_account_uuid": str(row["external_account_uuid"]),
                    "owner_user_uuid": self._logical_user(row["owner_user_uuid"]),
                    "project_id": (
                        expected_row["provider_contract"]["arguments"]["project_id"]
                        if expected_row is not None
                        and str(row["project_id"]) == run["test_project_id"]
                        else f"unexpected:{row['project_id']}"
                    ),
                    "operation_kind": row["operation_kind"],
                    "public_action": row["action"],
                    "target_type": row["target_type"],
                    "target_uuid": str(row["target_uuid"]),
                    "queue_payload_sha256": fixture.sha256(row["payload"]),
                    "workspace_payload_sha256": (
                        None
                        if target_message is None
                        else fixture.sha256(target_message["payload"])
                    ),
                    "queue_account_matches_public": (
                        row["queue_account_uuid"] == row["external_account_uuid"]
                    ),
                    "public_status": row["public_status"],
                    "queue_status": row["queue_status"],
                    "public_attempt": row["public_attempt"],
                    "queue_attempt": row["queue_attempt"],
                }
            )
        provider_event_ledger.sort(key=lambda item: item["provider_event_uuid"])
        provider_operation_ledger.sort(key=lambda item: item["external_operation_uuid"])
        observed = {
            "expected_row_counts": counts,
            "relationship_counts": relationship_counts,
            "normalized_digests": digests,
            "canonical_event_age_buckets": {
                key: age_buckets[key]
                for key in (
                    "older_than_7d",
                    "exact_7d_boundary",
                    "newer_than_7d",
                )
            },
            "s3_objects": s3_objects,
            "provider_stream_mappings": provider_stream_mappings,
            "provider_mapping_counts": provider_counts,
            "provider_event_ledger": provider_event_ledger,
            "provider_operation_ledger": provider_operation_ledger,
        }
        mismatches = []
        for section in (
            "expected_row_counts",
            "relationship_counts",
            "normalized_digests",
            "canonical_event_age_buckets",
            "provider_mapping_counts",
        ):
            for name, expected_value in manifest[section].items():
                if observed[section].get(name) != expected_value:
                    mismatches.append(f"{section}.{name}")
        expected_s3 = {
            (
                row["uuid"],
                row["object_name"],
                row["binary_sha256"],
                row["sidecar_object_name"],
                row["sidecar_sha256"],
            )
            for row in manifest["s3_objects"]
        }
        actual_s3 = {
            (
                row["uuid"],
                row["object_name"],
                row["binary_sha256"],
                row["sidecar_object_name"],
                row["sidecar_sha256"],
            )
            for row in s3_objects
        }
        if actual_s3 != expected_s3:
            mismatches.append("s3_objects")
        if provider_stream_mappings != manifest["provider_stream_mappings"]:
            mismatches.append("provider_stream_mappings")
        for name, expected_rows in expected_provider_ledgers.items():
            if observed[name] != expected_rows:
                mismatches.append(name)
        if extra_project_objects:
            mismatches.append("s3_objects.extra_project_objects")
        if storage_faults:
            mismatches.append("s3_objects.missing_or_invalid")
        if age_buckets["outside_fixture_buckets"]:
            mismatches.append("canonical_event_age_buckets.outside_fixture_buckets")
        expected_resource_ids = {
            kind: {row["record_key"] for row in values}
            for kind, values in expected.items()
        }
        actual_resource_ids = {
            "stream": {str(row["uuid"]) for row in actual["streams"]},
            "message": {str(row["uuid"]) for row in actual["messages"]},
            "reaction": {str(row["uuid"]) for row in actual["reactions"]},
            "file": {str(row["uuid"]) for row in actual["files"]},
            "event": {str(row["uuid"]) for row in actual["events"]},
            "external_account": {str(row["uuid"]) for row in actual["accounts"]},
            "external_chat": {str(row["uuid"]) for row in actual["chats"]},
        }
        for kind, actual_ids in actual_resource_ids.items():
            if actual_ids != expected_resource_ids.get(kind, set()):
                mismatches.append(f"resources.{kind}")
        return {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "run_id": run["run_id"],
            "test_project_id": run["test_project_id"],
            "profile_id": run["profile_id"],
            "manifest_sha256": run["manifest_sha256"],
            "status": "PASS" if not mismatches else "FAIL",
            "passed": not mismatches,
            **observed,
            "extra_project_objects": sorted(set(extra_project_objects)),
            "storage_faults": sorted(storage_faults),
            "mismatches": sorted(set(mismatches)),
        }
