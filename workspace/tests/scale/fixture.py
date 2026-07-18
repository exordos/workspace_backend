# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Pure deterministic fixture planning and correctness-ledger verification."""

import collections
import datetime
import hashlib
import json
import math
import pathlib
import uuid as sys_uuid

from workspace.external_bridge_control.sql_state import (
    _projection_uuid as _backend_projection_uuid,
)


SCHEMA_VERSION = "workspace.messenger.fixture-manifest/v1"
LEDGER_SCHEMA_VERSION = "workspace.messenger.correctness-ledger/v1"
RUN_LEDGER_SCHEMA_VERSION = "workspace.messenger.run-ledger/v1"
APPLICATION_PLAN_SCHEMA_VERSION = "workspace.messenger.fixture-application-plan/v1"
PROFILE_SCHEMA_VERSION = "workspace.messenger.scale-profile/v1"
FILE_CONTENT_RECIPE_SCHEMA_VERSION = "workspace.messenger.fixture-content/v1"
EVENT_APPLICATION_CONTRACT_VERSION = "workspace.messenger.fixture-event-application/v1"
FIXTURE_NAMESPACE = sys_uuid.UUID("a8a0586e-afb8-5f16-bcc6-3601583b0198")
STREAM_KINDS = ("channel", "group_dm", "direct_dm")
EVENT_KINDS = ("message.created", "topic.updated", "stream.updated")
EVENT_REFERENCE_TIME = datetime.datetime(
    2026,
    7,
    18,
    tzinfo=datetime.timezone.utc,
)
FILE_CONTENT_MAX_BYTES = 1024 * 1024


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_uuid(seed, kind, index):
    return str(sys_uuid.uuid5(FIXTURE_NAMESPACE, f"{seed}:{kind}:{index}"))


def sha256(value):
    if not isinstance(value, bytes):
        value = canonical_json(value).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def file_content_from_recipe(recipe):
    """Materialize bounded synthetic bytes from the declared fixture recipe."""
    if recipe["schema_version"] != FILE_CONTENT_RECIPE_SCHEMA_VERSION:
        raise ValueError("unsupported fixture content recipe schema")
    if recipe["algorithm"] != "repeat-utf8-v1":
        raise ValueError("unsupported fixture content recipe algorithm")
    chunk = recipe["chunk"].encode("utf-8")
    size = recipe["size_bytes"]
    if (
        not chunk
        or not isinstance(size, int)
        or not 0 <= size <= FILE_CONTENT_MAX_BYTES
    ):
        raise ValueError("fixture content recipe is not bounded")
    return (chunk * ((size + len(chunk) - 1) // len(chunk)))[:size]


def _event_retention(message_index):
    bucket = (
        "older_than_7d"
        if message_index % 3 == 0
        else "exact_7d_boundary"
        if message_index % 3 == 1
        else "newer_than_7d"
    )
    created_at = (
        EVENT_REFERENCE_TIME - datetime.timedelta(days=7, seconds=1)
        if bucket == "older_than_7d"
        else EVENT_REFERENCE_TIME - datetime.timedelta(days=7)
        if bucket == "exact_7d_boundary"
        else EVENT_REFERENCE_TIME - datetime.timedelta(days=6, seconds=86399)
    )
    return bucket, created_at


def _broadcast_event_rows(seed, message):
    bucket, created_at = _event_retention(message["message_index"])
    for event_kind in EVENT_KINDS:
        yield {
            "event_uuid": stable_uuid(
                seed,
                f"broadcast-event:{event_kind}",
                message["message_index"],
            ),
            "event_kind": event_kind,
            "message_uuid": message["uuid"],
            "stream_uuid": message["stream_uuid"],
            "created_at": created_at.isoformat(),
            "retention_policy": {
                "kind": "fixed_reference_7d",
                "reference_time": EVENT_REFERENCE_TIME.isoformat(),
                "retention_days": 7,
                "bucket": bucket,
            },
        }


def load_profile(path):
    profile = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    validate_profile(profile)
    return profile


def validate_profile(profile):
    if profile["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported scale profile schema")

    dimensions = profile["dimensions"]
    stream_count = sum(dimensions["streams"].values())
    message_count = sum(profile["message_mix"].values())
    visible_delivery_count = 3 * sum(
        profile["message_mix"][kind] * profile["membership"][kind]
        for kind in STREAM_KINDS
    )

    if stream_count <= 0 or dimensions["topics"] % stream_count:
        raise ValueError("topics must divide evenly across streams")
    if message_count != dimensions["messages"]:
        raise ValueError("message mix does not match message count")
    if visible_delivery_count != dimensions["visible_event_deliveries"]:
        raise ValueError("message fanout does not match visible delivery count")
    if dimensions["zulip_accounts"] != dimensions["live_users"]:
        raise ValueError("every live user must have one Zulip account")
    if dimensions["provider_synced_streams"] <= stream_count // 2:
        raise ValueError("most streams must be provider-synced")
    if dimensions["provider_synced_streams"] > stream_count:
        raise ValueError("provider stream count exceeds all streams")

    workload = profile["provider_workload"]
    steady = workload["steady_messages_per_minute"]
    if (steady["minimum"], steady["maximum"]) != (100, 200):
        raise ValueError("steady provider rate must remain 100-200 messages/minute")
    if workload["burst_messages_per_minute"] != 400:
        raise ValueError("burst provider rate must remain 400 messages/minute")


def _ordered_ids(seed, kind, count):
    values = [stable_uuid(seed, kind, index) for index in range(count)]
    return sorted(values, key=lambda value: sha256(f"{seed}:{kind}:{value}".encode()))


def _zipf_counts(total, count, exponent):
    weights = [1 / math.pow(rank + 1, exponent) for rank in range(count)]
    denominator = sum(weights)
    exact = [total * weight / denominator for weight in weights]
    result = [int(value) for value in exact]
    remainder = total - sum(result)
    order = sorted(
        range(count),
        key=lambda index: (exact[index] - result[index], -index),
        reverse=True,
    )
    for index in order[:remainder]:
        result[index] += 1
    return result


def _stream_rows(profile, users, accounts):
    seed = profile["seed"]
    dimensions = profile["dimensions"]
    project_id = stable_uuid(seed, "project", 0)
    topics_per_stream = dimensions["topics"] // sum(dimensions["streams"].values())
    rows = []
    global_index = 0
    for kind in STREAM_KINDS:
        for kind_index in range(dimensions["streams"][kind]):
            member_count = profile["membership"][kind]
            start = (global_index * 17) % len(users)
            members = [
                users[(start + offset) % len(users)] for offset in range(member_count)
            ]
            stream_uuid = stable_uuid(seed, "stream", global_index)
            topics = [
                stable_uuid(seed, f"topic:{stream_uuid}", topic_index)
                for topic_index in range(topics_per_stream)
            ]
            rows.append(
                {
                    "uuid": stream_uuid,
                    "kind": kind,
                    "members": members,
                    "topics": topics,
                    "provider_synced": False,
                    "kind_index": kind_index,
                }
            )
            global_index += 1

    provider_total = dimensions["provider_synced_streams"]
    exact = {
        kind: provider_total * dimensions["streams"][kind] / len(rows)
        for kind in STREAM_KINDS
    }
    allocations = {kind: int(exact[kind]) for kind in STREAM_KINDS}
    remainder = provider_total - sum(allocations.values())
    for kind in sorted(
        STREAM_KINDS,
        key=lambda value: (exact[value] - allocations[value], value),
        reverse=True,
    )[:remainder]:
        allocations[kind] += 1
    for kind in STREAM_KINDS:
        provider_order = sorted(
            (row for row in rows if row["kind"] == kind),
            key=lambda row: sha256(f"{seed}:provider-stream:{row['uuid']}".encode()),
        )
        for row in provider_order[: allocations[kind]]:
            row["provider_synced"] = True

    provider_rows = sorted(
        (row for row in rows if row["provider_synced"]),
        key=lambda row: sha256(f"{seed}:provider-account:{row['uuid']}".encode()),
    )
    if len(provider_rows) > len(accounts):
        raise ValueError("provider streams require distinct external accounts")
    for row, account in zip(provider_rows, accounts):
        owner_uuid = account["owner_user_uuid"]
        row["external_account_uuid"] = account["uuid"]
        row["owner_user_uuid"] = owner_uuid
        if owner_uuid not in row["members"]:
            replacement_index = int(
                sha256(f"{seed}:provider-owner:{row['uuid']}".encode())[:8],
                16,
            ) % len(row["members"])
            row["members"][replacement_index] = owner_uuid
        if len(set(row["members"])) != len(row["members"]):
            raise ValueError("provider owner assignment produced duplicate membership")
        chat_type = {
            "channel": "channel",
            "group_dm": "group_direct",
            "direct_dm": "direct",
        }[row["kind"]]
        provider_chat_key = f"{chat_type}:{row['kind_index']:06d}"
        external_chat_uuid = stable_uuid(
            seed,
            f"external-chat:{account['uuid']}",
            row["kind_index"],
        )
        catalog_topics = []
        projection_topics = []
        for topic_index in range(len(row["topics"])):
            provider_topic_id = f"{provider_chat_key}:topic:{topic_index:04d}"
            topic_uuid = str(
                _backend_projection_uuid(
                    sys_uuid.UUID(external_chat_uuid),
                    "topic",
                    provider_topic_id,
                )
            )
            catalog_topics.append(
                {
                    "provider_topic_id": provider_topic_id,
                    "name": f"fixture-topic-{topic_index:04d}",
                    "is_default": topic_index == 0,
                }
            )
            projection_topics.append(
                {
                    "uuid": topic_uuid,
                    "provider_topic_id": provider_topic_id,
                    "name": f"fixture-topic-{topic_index:04d}",
                    "is_default": topic_index == 0,
                }
            )
        row.update(
            {
                "uuid": str(
                    _backend_projection_uuid(
                        sys_uuid.UUID(external_chat_uuid),
                        "stream",
                        "canonical",
                    )
                ),
                "topics": [item["uuid"] for item in projection_topics],
                "external_chat_uuid": external_chat_uuid,
                "provider_chat_key": provider_chat_key,
                "provider_chat_type": chat_type,
                "catalog_participants": [
                    {
                        "provider_user_id": f"zulip-user:{member_uuid}",
                        "display_name": f"fixture-user-{member_uuid[:8]}",
                        "is_owner": member_uuid == owner_uuid,
                    }
                    for member_uuid in row["members"]
                ],
                "catalog_topics": catalog_topics,
                "projection_topics": projection_topics,
            }
        )
    for row in rows:
        owner_uuid = row.get("owner_user_uuid") or row["members"][0]
        row.update(
            {
                "project_id": project_id,
                "owner_user_uuid": owner_uuid,
                "private": row["kind"] != "channel",
                "invite_only": row["kind"] != "channel",
                "default_topic_uuid": row["topics"][0],
                "bindings": [
                    {
                        "uuid": stable_uuid(
                            seed,
                            f"stream-binding:{row['uuid']}:{member_uuid}",
                            0,
                        ),
                        "user_uuid": member_uuid,
                        "role": (
                            "owner"
                            if row["kind"] == "direct_dm" or member_uuid == owner_uuid
                            else "member"
                        ),
                    }
                    for member_uuid in row["members"]
                ],
            }
        )
    return rows


def _message_rows(profile, streams):
    seed = profile["seed"]
    exponent = profile["distribution"]["exponent"]
    provider_total = profile["dimensions"]["provider_messages"]
    message_total = profile["dimensions"]["messages"]
    provider_streams = {
        kind: [row for row in streams if row["provider_synced"] and row["kind"] == kind]
        for kind in STREAM_KINDS
    }
    provider_cursors = collections.defaultdict(int)
    message_index = 0

    for kind in STREAM_KINDS:
        kind_streams = [row for row in streams if row["kind"] == kind]
        counts = _zipf_counts(profile["message_mix"][kind], len(kind_streams), exponent)
        for stream, stream_messages in zip(kind_streams, counts):
            for local_index in range(stream_messages):
                use_provider = (
                    (message_index + 1) * provider_total
                ) // message_total > (message_index * provider_total) // message_total
                selected_stream = stream
                if use_provider and not selected_stream["provider_synced"]:
                    candidates = provider_streams[kind]
                    cursor = provider_cursors[kind]
                    selected_stream = candidates[cursor % len(candidates)]
                    provider_cursors[kind] += 1
                message_uuid = stable_uuid(seed, "message", message_index)
                yield {
                    "uuid": message_uuid,
                    "project_id": selected_stream["project_id"],
                    "stream_uuid": selected_stream["uuid"],
                    "topic_uuid": selected_stream["topics"][
                        local_index % len(selected_stream["topics"])
                    ],
                    "sender_uuid": selected_stream["members"][
                        local_index % len(selected_stream["members"])
                    ],
                    "recipients": selected_stream["members"],
                    "provider": use_provider,
                    "external_account_uuid": (
                        selected_stream.get("external_account_uuid")
                        if use_provider
                        else None
                    ),
                    "external_chat_uuid": (
                        selected_stream.get("external_chat_uuid")
                        if use_provider
                        else None
                    ),
                    "provider_chat_key": (
                        selected_stream.get("provider_chat_key")
                        if use_provider
                        else None
                    ),
                    "payload_sha256": sha256(
                        {
                            "kind": "markdown",
                            "content": f"fixture message {message_index}",
                        }
                    ),
                    "message_index": message_index,
                }
                message_index += 1


def _workspace_message_resource(message):
    return {
        "uuid": message["uuid"],
        "stream_uuid": message["stream_uuid"],
        "topic_uuid": message["topic_uuid"],
        "user_uuid": message["sender_uuid"],
        "payload": {
            "kind": "markdown",
            "content": f"fixture message {message['message_index']}",
        },
    }


def _provider_contract(
    *,
    seed,
    message,
    provider_stream,
    provider_index,
    direction,
    cursor_ordinal,
):
    operation_uuid = stable_uuid(seed, "provider-operation", provider_index)
    event_uuid = stable_uuid(seed, "provider-event", provider_index)
    resource = _workspace_message_resource(message)
    if direction == "inbound":
        resource.update(
            {
                "provider_external_id": f"zulip-message:{provider_index:08d}",
                "provider_metadata": {
                    "kind": "zulip",
                    "provider_chat_key": provider_stream["provider_chat_key"],
                },
            }
        )
        return (
            operation_uuid,
            event_uuid,
            {
                "direction": "inbound",
                "service": "ProviderEventBatch.events[]",
                "event": {
                    "provider_event_uuid": event_uuid,
                    "external_account_uuid": provider_stream["external_account_uuid"],
                    "external_chat_uuid": provider_stream["external_chat_uuid"],
                    "project_id": provider_stream["project_id"],
                    "provider_sequence": str(cursor_ordinal),
                    "kind": "message.upsert",
                    "payload": {"resource": resource},
                },
            },
        )
    return (
        operation_uuid,
        event_uuid,
        {
            "direction": "outbound",
            "service": "enqueue_provider_operation",
            "external_chat_uuid": provider_stream["external_chat_uuid"],
            "arguments": {
                "operation_uuid": operation_uuid,
                "bridge_instance_uuid": stable_uuid(seed, "zulip-bridge-instance", 0),
                "external_account_uuid": provider_stream["external_account_uuid"],
                "project_id": provider_stream["project_id"],
                "owner_user_uuid": message["sender_uuid"],
                "operation_kind": "message.create",
                "target_type": "message",
                "target_uuid": message["uuid"],
                "payload": resource,
            },
            "runtime_generated_fields": [
                "provider_operation_uuid",
                "lease_uuid",
                "lease_expires_at",
            ],
        },
    )


def provider_persistence_ledger_rows(rows):
    """Return the stable provider-ledger fields that PostgreSQL must persist."""
    inbound = []
    outbound = []
    for row in rows:
        contract = row["provider_contract"]
        if row["direction"] == "inbound":
            event = contract["event"]
            inbound.append(
                {
                    "provider_event_uuid": row["provider_event_uuid"],
                    "external_account_uuid": row["account_uuid"],
                    "project_id": event["project_id"],
                    "provider_sequence": event["provider_sequence"],
                    "event_kind": event["kind"],
                    "target_uuid": row["workspace_message_uuid"],
                    "status": "applied",
                    "workspace_payload_sha256": row["payload_sha256"],
                    "envelope_payload_sha256_verified": True,
                }
            )
            continue
        arguments = contract["arguments"]
        outbound.append(
            {
                "external_operation_uuid": row["operation_uuid"],
                "external_account_uuid": row["account_uuid"],
                "owner_user_uuid": row["owner_user_uuid"],
                "project_id": arguments["project_id"],
                "operation_kind": arguments["operation_kind"],
                "public_action": arguments["operation_kind"],
                "target_type": arguments["target_type"],
                "target_uuid": arguments["target_uuid"],
                "queue_payload_sha256": sha256(arguments["payload"]),
                "workspace_payload_sha256": row["payload_sha256"],
                "queue_account_matches_public": True,
                "public_status": "queued",
                "queue_status": "queued",
                "public_attempt": 0,
                "queue_attempt": 0,
            }
        )
    return {
        "provider_event_ledger": sorted(
            inbound,
            key=lambda item: item["provider_event_uuid"],
        ),
        "provider_operation_ledger": sorted(
            outbound,
            key=lambda item: item["external_operation_uuid"],
        ),
    }


def _digest_rows(rows, fields):
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(canonical_json({field: row[field] for field in fields}).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def write_json_lines(path, rows):
    digest = hashlib.sha256()
    count = 0
    with pathlib.Path(path).open("w", encoding="utf-8") as stream:
        for row in rows:
            encoded = canonical_json(row)
            stream.write(encoded + "\n")
            digest.update(encoded.encode())
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def _application_records(profile, users, accounts, streams, files, provider_rows):
    seed = profile["seed"]
    provider_by_message = {row["workspace_message_uuid"]: row for row in provider_rows}
    for index, user_uuid in enumerate(users):
        yield {
            "record_kind": "user",
            "record_key": user_uuid,
            "values": {
                "uuid": user_uuid,
                "name": f"fixture-user-{index:06d}",
            },
        }
    for account in accounts:
        yield {
            "record_kind": "external_account",
            "record_key": account["uuid"],
            "values": account,
        }
    for index, stream in enumerate(row for row in streams if row["provider_synced"]):
        yield {
            "record_kind": "external_chat",
            "record_key": stream["external_chat_uuid"],
            "values": {
                "uuid": stream["external_chat_uuid"],
                "external_account_uuid": stream["external_account_uuid"],
                "owner_user_uuid": stream["owner_user_uuid"],
                "provider": "zulip",
                "provider_chat_key": stream["provider_chat_key"],
                "project_id": stream["project_id"],
                "selected": True,
                "catalog_report_spec": {
                    "operation": "upsert",
                    "external_account_uuid": stream["external_account_uuid"],
                    "owner_user_uuid": stream["owner_user_uuid"],
                    "provider_kind": "zulip",
                    "project_id": stream["project_id"],
                    "source": {
                        "kind": "zulip",
                        "chat_type": stream["provider_chat_type"],
                        "provider_chat_key": stream["provider_chat_key"],
                    },
                    "display_name": f"fixture-provider-chat-{index:06d}",
                    "description": "",
                    "capabilities": {},
                    "participants": stream["catalog_participants"],
                    "topics": stream["catalog_topics"],
                },
                "workspace_projection": {
                    "stream_uuid": stream["uuid"],
                    "topics": stream["projection_topics"],
                },
            },
        }
    for index, stream in enumerate(streams):
        yield {
            "record_kind": "stream",
            "record_key": stream["uuid"],
            "values": {
                "uuid": stream["uuid"],
                "project_id": stream["project_id"],
                "kind": stream["kind"],
                "name": f"fixture-stream-{index:06d}",
                "members": stream["members"],
                "bindings": stream["bindings"],
                "owner_user_uuid": stream["owner_user_uuid"],
                "private": stream["private"],
                "invite_only": stream["invite_only"],
                "default_topic_uuid": stream["default_topic_uuid"],
                "topics": [
                    {
                        "uuid": topic_uuid,
                        "name": f"fixture-topic-{topic_index:04d}",
                        "provider_topic_id": (
                            stream["projection_topics"][topic_index][
                                "provider_topic_id"
                            ]
                            if stream["provider_synced"]
                            else None
                        ),
                    }
                    for topic_index, topic_uuid in enumerate(stream["topics"])
                ],
                "provider_synced": stream["provider_synced"],
                "external_account_uuid": stream.get("external_account_uuid"),
                "external_chat_uuid": stream.get("external_chat_uuid"),
                "provider_chat_key": stream.get("provider_chat_key"),
            },
        }
    for message in _message_rows(profile, streams):
        provider = provider_by_message.get(message["uuid"])
        yield {
            "record_kind": "message",
            "record_key": message["uuid"],
            "values": {
                **message,
                "payload": {
                    "kind": "markdown",
                    "content": f"fixture message {message['message_index']}",
                },
                "provider_operation": provider,
            },
        }
        for event in _broadcast_event_rows(seed, message):
            yield {
                "record_kind": "event",
                "record_key": event["event_uuid"],
                "values": {
                    **event,
                    "application_contract": {
                        "schema_version": EVENT_APPLICATION_CONTRACT_VERSION,
                        "status": "ready",
                        "required_service": (
                            "create_deterministic_fixture_broadcast_event"
                        ),
                    },
                },
            }
    for index in range(profile["dimensions"]["reactions"]):
        reaction_uuid = stable_uuid(seed, "reaction", index)
        yield {
            "record_kind": "reaction",
            "record_key": reaction_uuid,
            "values": {
                "uuid": reaction_uuid,
                "message_uuid": stable_uuid(
                    seed,
                    "message",
                    index % profile["dimensions"]["messages"],
                ),
                "user_uuid": users[index % len(users)],
                "emoji_name": ("thumbs_up", "eyes", "rocket")[index % 3],
            },
        }
    for row in files:
        yield {
            "record_kind": "file",
            "record_key": row["uuid"],
            "values": row,
        }


def _write_application_plan(
    path,
    profile,
    users,
    accounts,
    streams,
    files,
    provider_rows,
    unit_size=100,
):
    def units():
        records = []
        unit_index = 0
        for record in _application_records(
            profile,
            users,
            accounts,
            streams,
            files,
            provider_rows,
        ):
            records.append(record)
            if len(records) < unit_size:
                continue
            yield _application_unit(profile["seed"], unit_index, records)
            records = []
            unit_index += 1
        if records:
            yield _application_unit(profile["seed"], unit_index, records)

    return write_json_lines(path, units())


def _application_unit(seed, index, records):
    records_digest = sha256(records)
    return {
        "schema_version": APPLICATION_PLAN_SCHEMA_VERSION,
        "unit_id": stable_uuid(
            seed,
            f"application-unit:{records_digest}",
            index,
        ),
        "unit_index": index,
        "records_sha256": records_digest,
        "records": records,
    }


def build_fixture(profile, output_directory, dry_run=True):
    """Write a deterministic logical manifest and expected correctness ledger."""
    validate_profile(profile)
    output_directory = pathlib.Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    seed = profile["seed"]
    dimensions = profile["dimensions"]
    users = _ordered_ids(seed, "user", dimensions["users"])
    live_users = users[: dimensions["live_users"]]
    accounts = [
        {
            "uuid": stable_uuid(seed, "zulip-account", index),
            "ordinal": index,
            "owner_user_uuid": user_uuid,
            "provider": "zulip",
            "credential_ref": f"zulip-account-{index:03d}",
            "settings": {
                "kind": "zulip",
                "server_url": "https://zulip.fixture.invalid",
                "selection_mode": "explicit",
                "history_depth": "30_days",
                "default_project_id": stable_uuid(seed, "project", 0),
            },
        }
        for index, user_uuid in enumerate(live_users)
    ]
    streams = _stream_rows(profile, users, accounts)
    stream_by_uuid = {row["uuid"]: row for row in streams}
    provider_stream_mappings = [
        {
            "stream_uuid": row["uuid"],
            "external_account_uuid": row["external_account_uuid"],
            "external_chat_uuid": row["external_chat_uuid"],
            "provider_chat_key": row["provider_chat_key"],
            "provider_chat_type": row["provider_chat_type"],
            "projection_topics": row["projection_topics"],
            "owner_user_uuid": row["owner_user_uuid"],
        }
        for row in streams
        if row["provider_synced"]
    ]
    if len({row["stream_uuid"] for row in provider_stream_mappings}) != len(
        provider_stream_mappings
    ):
        raise ValueError("a provider stream must have exactly one mapping")
    if len({row["external_account_uuid"] for row in provider_stream_mappings}) != len(
        provider_stream_mappings
    ):
        raise ValueError("fixture provider streams must use distinct accounts")
    for mapping in provider_stream_mappings:
        stream = stream_by_uuid[mapping["stream_uuid"]]
        if mapping["owner_user_uuid"] not in stream["members"]:
            raise ValueError("provider stream owner must be an authorized member")

    message_digest = hashlib.sha256()
    broadcast_event_digest = hashlib.sha256()
    visible_delivery_digest = hashlib.sha256()
    provider_mapping_digest = hashlib.sha256()
    ledger_digest = hashlib.sha256()
    canonical_event_age_buckets = {
        "older_than_7d": 0,
        "exact_7d_boundary": 0,
        "newer_than_7d": 0,
    }
    message_count = 0
    provider_count = 0
    provider_ordinals = collections.defaultdict(int)
    broadcast_event_count = 0
    visible_delivery_count = 0
    ledger_path = output_directory / "expected-ledger.jsonl"

    with ledger_path.open("w", encoding="utf-8") as ledger:
        for message in _message_rows(profile, streams):
            message_count += 1
            message_digest.update(canonical_json(message).encode())
            message_digest.update(b"\n")
            for broadcast_event_row in _broadcast_event_rows(seed, message):
                broadcast_event_digest.update(
                    canonical_json(broadcast_event_row).encode()
                )
                broadcast_event_digest.update(b"\n")
                canonical_event_age_buckets[
                    broadcast_event_row["retention_policy"]["bucket"]
                ] += 1
                broadcast_event_count += 1
                for recipient in message["recipients"]:
                    delivery_row = {
                        "event_uuid": broadcast_event_row["event_uuid"],
                        "event_kind": broadcast_event_row["event_kind"],
                        "message_uuid": message["uuid"],
                        "recipient_user_uuid": recipient,
                    }
                    visible_delivery_digest.update(
                        canonical_json(delivery_row).encode()
                    )
                    visible_delivery_digest.update(b"\n")
                    visible_delivery_count += 1

            if not message["provider"]:
                continue
            provider_count += 1
            provider_stream = stream_by_uuid[message["stream_uuid"]]
            account_uuid = provider_stream["external_account_uuid"]
            owner_user_uuid = provider_stream["owner_user_uuid"]
            direction = "inbound" if provider_count % 5 < 3 else "outbound"
            provider_ordinals[account_uuid] += 1
            operation_uuid, event_uuid, provider_contract = _provider_contract(
                seed=seed,
                message=message,
                provider_stream=provider_stream,
                provider_index=provider_count - 1,
                direction=direction,
                cursor_ordinal=provider_ordinals[account_uuid],
            )
            ledger_row = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "operation_uuid": operation_uuid,
                "provider_event_uuid": event_uuid,
                "account_uuid": account_uuid,
                "owner_user_uuid": owner_user_uuid,
                "external_chat_uuid": provider_stream["external_chat_uuid"],
                "provider_chat_key": provider_stream["provider_chat_key"],
                "stream_uuid": message["stream_uuid"],
                "topic_uuid": message["topic_uuid"],
                "workspace_message_uuid": message["uuid"],
                "direction": direction,
                "payload_sha256": message["payload_sha256"],
                "cursor_scope": f"zulip:{account_uuid}",
                "cursor_ordinal": provider_ordinals[account_uuid],
                "outbox_idempotency_key": stable_uuid(
                    seed, "outbox-idempotency", provider_count - 1
                ),
                "expected_attempts": 2 if provider_count % 10 == 0 else 1,
                "provider_contract": provider_contract,
            }
            encoded = canonical_json(ledger_row)
            ledger.write(encoded + "\n")
            ledger_digest.update(encoded.encode())
            ledger_digest.update(b"\n")
            provider_mapping_digest.update(
                canonical_json(
                    {
                        "account_uuid": account_uuid,
                        "external_chat_uuid": provider_stream["external_chat_uuid"],
                        "stream_uuid": message["stream_uuid"],
                        "message_uuid": message["uuid"],
                    }
                ).encode()
            )
            provider_mapping_digest.update(b"\n")

    files = []
    for index in range(dimensions["files"]):
        object_uuid = stable_uuid(seed, "file", index)
        object_name = f"{object_uuid[:2]}/{object_uuid}"
        content_recipe = {
            "schema_version": FILE_CONTENT_RECIPE_SCHEMA_VERSION,
            "algorithm": "repeat-utf8-v1",
            "chunk": f"workspace-fixture:{seed}:{index}\n",
            "size_bytes": 1024 + index,
        }
        binary = file_content_from_recipe(content_recipe)
        binary_hash = sha256(binary)
        stream = streams[index % len(streams)]
        owner_uuid = stream["members"][index % len(stream["members"])]
        sidecar = {
            "acl": {
                "mode": "stream_members",
                "stream_uuid": stream["uuid"],
            },
            "content_type": "application/octet-stream",
            "created_at": "2026-07-18T00:00:00+00:00",
            "description": "",
            "name": f"fixture-{index:06d}.bin",
            "owner_uuid": owner_uuid,
            "project_id": stable_uuid(seed, "project", 0),
            "schema_version": 1,
            "sha256": binary_hash,
            "size_bytes": len(binary),
            "stream_uuid": stream["uuid"],
            "uuid": object_uuid,
        }
        files.append(
            {
                "uuid": object_uuid,
                "content_recipe": content_recipe,
                "size_bytes": len(binary),
                "object_name": object_name,
                "binary_sha256": binary_hash,
                "sidecar_object_name": (
                    f"metadata/{object_uuid[:2]}/{object_uuid}.json"
                ),
                "sidecar_sha256": sha256(sidecar),
                "sidecar": sidecar,
            }
        )

    reaction_rows = (
        {
            "uuid": stable_uuid(seed, "reaction", index),
            "message_uuid": stable_uuid(seed, "message", index % message_count),
            "user_uuid": users[index % len(users)],
            "emoji_name": ("thumbs_up", "eyes", "rocket")[index % 3],
        }
        for index in range(dimensions["reactions"])
    )
    reaction_count, reaction_digest = _digest_rows(
        reaction_rows,
        ("uuid", "message_uuid", "user_uuid", "emoji_name"),
    )
    user_rows = ({"uuid": value} for value in users)
    _, user_digest = _digest_rows(user_rows, ("uuid",))
    stream_rows = (
        {
            "uuid": row["uuid"],
            "kind": row["kind"],
            "provider_synced": row["provider_synced"],
            "owner_user_uuid": row["owner_user_uuid"],
            "private": row["private"],
            "invite_only": row["invite_only"],
            "default_topic_uuid": row["default_topic_uuid"],
            "bindings": row["bindings"],
            "external_chat_uuid": row.get("external_chat_uuid"),
            "provider_chat_key": row.get("provider_chat_key"),
        }
        for row in streams
    )
    _, stream_digest = _digest_rows(
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
    )
    account_inventory_rows = (
        {
            "uuid": row["uuid"],
            "owner_user_uuid": row["owner_user_uuid"],
            "provider": row["provider"],
            "selection_mode": row["settings"]["selection_mode"],
            "history_depth": row["settings"]["history_depth"],
            "default_project_id": row["settings"]["default_project_id"],
        }
        for row in accounts
    )
    account_count, account_digest = _digest_rows(
        account_inventory_rows,
        (
            "uuid",
            "owner_user_uuid",
            "provider",
            "selection_mode",
            "history_depth",
            "default_project_id",
        ),
    )
    file_count, file_digest = _digest_rows(
        iter(files),
        (
            "uuid",
            "size_bytes",
            "object_name",
            "binary_sha256",
            "sidecar_object_name",
            "sidecar_sha256",
        ),
    )

    membership_rows = (
        {
            "stream_uuid": row["uuid"],
            "binding_uuid": binding["uuid"],
            "user_uuid": binding["user_uuid"],
            "role": binding["role"],
        }
        for row in streams
        for binding in row["bindings"]
    )
    membership_count, membership_digest = _digest_rows(
        membership_rows,
        ("stream_uuid", "binding_uuid", "user_uuid", "role"),
    )
    topic_rows = (
        {"stream_uuid": row["uuid"], "topic_uuid": topic}
        for row in streams
        for topic in row["topics"]
    )
    topic_count, topic_digest = _digest_rows(topic_rows, ("stream_uuid", "topic_uuid"))

    provider_rows = [row for _, row in read_json_lines(ledger_path)]
    application_plan_path = output_directory / "application-plan.jsonl"
    application_unit_count, application_plan_digest = _write_application_plan(
        application_plan_path,
        profile,
        users,
        accounts,
        streams,
        files,
        provider_rows,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile["profile_id"],
        "profile_sha256": sha256(profile),
        "seed": seed,
        "dry_run": dry_run,
        "expected_row_counts": {
            "users": len(users),
            "streams": len(streams),
            "topics": topic_count,
            "messages": message_count,
            "provider_messages": provider_count,
            "reactions": reaction_count,
            "files": file_count,
            "canonical_broadcast_events": broadcast_event_count,
            "visible_event_deliveries": visible_delivery_count,
            "zulip_accounts": account_count,
        },
        "relationship_counts": {
            "stream_memberships": membership_count,
            "provider_synced_streams": sum(row["provider_synced"] for row in streams),
        },
        "normalized_digests": {
            "users": user_digest,
            "streams": stream_digest,
            "stream_memberships": membership_digest,
            "topics": topic_digest,
            "messages": message_digest.hexdigest(),
            "reactions": reaction_digest,
            "files": file_digest,
            "canonical_broadcast_events": broadcast_event_digest.hexdigest(),
            "visible_event_deliveries": visible_delivery_digest.hexdigest(),
            "zulip_accounts": account_digest,
            "provider_mappings": provider_mapping_digest.hexdigest(),
        },
        "canonical_event_age_buckets": canonical_event_age_buckets,
        "s3_objects": files,
        "provider_stream_mappings": provider_stream_mappings,
        "provider_mapping_counts": {
            "external_accounts": len(accounts),
            "assigned_accounts": len(provider_stream_mappings),
            "streams": sum(row["provider_synced"] for row in streams),
            "topics": sum(
                len(row["topics"]) for row in streams if row["provider_synced"]
            ),
            "messages": provider_count,
        },
        "live_users": [],
        "correctness_ledger": {
            "path": ledger_path.name,
            "rows": provider_count,
            "sha256": ledger_digest.hexdigest(),
            "checks": [
                "loss",
                "duplication",
                "cross_account",
                "owner",
                "mapping",
                "direction",
                "payload_integrity",
                "cursor_monotonicity",
                "outbox_idempotency",
            ],
        },
        "application_plan": {
            "path": application_plan_path.name,
            "units": application_unit_count,
            "sha256": application_plan_digest,
            "unit_size": 100,
            "record_kinds": [
                "user",
                "external_account",
                "external_chat",
                "stream",
                "message",
                "event",
                "reaction",
                "file",
            ],
        },
    }
    provider_mapping_by_account = {
        row["external_account_uuid"]: row for row in provider_stream_mappings
    }
    for index, user_uuid in enumerate(live_users):
        account = accounts[index]
        mapping = provider_mapping_by_account.get(account["uuid"])
        native_stream = next(row for row in streams if user_uuid in row["members"])
        provider_stream = (
            stream_by_uuid[mapping["stream_uuid"]] if mapping is not None else None
        )
        manifest["live_users"].append(
            {
                "ordinal": index,
                "workspace_user_uuid": user_uuid,
                "zulip_account_uuid": account["uuid"],
                "credential_ref": account["credential_ref"],
                "provider_projection": mapping is not None,
                "stream_uuid": native_stream["uuid"],
                "topic_uuid": native_stream["topics"][0],
                "provider_stream_uuid": (
                    provider_stream["uuid"] if provider_stream is not None else None
                ),
                "provider_topic_uuid": (
                    provider_stream["topics"][0]
                    if provider_stream is not None
                    else None
                ),
            }
        )
    manifest_path = output_directory / "fixture-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def read_json_lines(path):
    with pathlib.Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                yield line_number, json.loads(line)


def verify_ledgers(expected_path, observed_path):
    """Compare content-free expected and observed delivery ledgers."""
    expected = {}
    for line_number, row in read_json_lines(expected_path):
        if row["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise ValueError(f"expected ledger line {line_number} has wrong schema")
        expected[row["operation_uuid"]] = row

    observations = collections.defaultdict(list)
    unexpected = []
    cross_account = []
    owner_errors = []
    mapping_errors = []
    direction_errors = []
    payload_errors = []
    cursor_errors = []
    cursor_positions = {}
    idempotency_results = collections.defaultdict(set)
    idempotency_errors = []
    for line_number, row in read_json_lines(observed_path):
        if row["schema_version"] != LEDGER_SCHEMA_VERSION:
            raise ValueError(f"observed ledger line {line_number} has wrong schema")
        operation_uuid = row["operation_uuid"]
        expected_row = expected.get(operation_uuid)
        if expected_row is None:
            unexpected.append(operation_uuid)
            continue
        observations[operation_uuid].append(row)
        if row["account_uuid"] != expected_row["account_uuid"]:
            cross_account.append(operation_uuid)
        if row["owner_user_uuid"] != expected_row["owner_user_uuid"]:
            owner_errors.append(operation_uuid)
        if any(
            row[field] != expected_row[field]
            for field in (
                "external_chat_uuid",
                "provider_chat_key",
                "stream_uuid",
                "topic_uuid",
                "workspace_message_uuid",
            )
        ):
            mapping_errors.append(operation_uuid)
        if row["direction"] != expected_row["direction"]:
            direction_errors.append(operation_uuid)
        if row["payload_sha256"] != expected_row["payload_sha256"]:
            payload_errors.append(operation_uuid)
        cursor_position = (row["cursor_scope"], row["cursor_ordinal"])
        if (
            row["cursor_scope"] != expected_row["cursor_scope"]
            or row["cursor_ordinal"] != expected_row["cursor_ordinal"]
        ):
            cursor_errors.append(operation_uuid)
        previous_operation = cursor_positions.setdefault(
            cursor_position, operation_uuid
        )
        if previous_operation != operation_uuid:
            cursor_errors.extend((previous_operation, operation_uuid))
        if row["outbox_idempotency_key"] != expected_row["outbox_idempotency_key"]:
            idempotency_errors.append(operation_uuid)
        provider_result_id = row.get("provider_result_id")
        if provider_result_id is not None:
            idempotency_results[row["outbox_idempotency_key"]].add(provider_result_id)

    missing = sorted(set(expected) - set(observations))
    duplicates = sorted(
        operation_uuid
        for operation_uuid, rows in observations.items()
        if len(
            {
                row.get("provider_result_id")
                for row in rows
                if row.get("provider_result_id") is not None
            }
        )
        > 1
    )
    idempotency_errors.extend(
        key for key, results in idempotency_results.items() if len(results) > 1
    )
    report = {
        "schema_version": "workspace.messenger.correctness-report/v1",
        "expected_operations": len(expected),
        "observed_operations": len(observations),
        "loss": missing,
        "duplication": duplicates,
        "unexpected": sorted(set(unexpected)),
        "cross_account": sorted(set(cross_account)),
        "owner": sorted(set(owner_errors)),
        "mapping": sorted(set(mapping_errors)),
        "direction": sorted(set(direction_errors)),
        "payload_integrity": sorted(set(payload_errors)),
        "cursor_monotonicity": sorted(set(cursor_errors)),
        "outbox_idempotency": sorted(set(idempotency_errors)),
    }
    report["passed"] = not any(
        report[key]
        for key in (
            "loss",
            "duplication",
            "unexpected",
            "cross_account",
            "owner",
            "mapping",
            "direction",
            "payload_integrity",
            "cursor_monotonicity",
            "outbox_idempotency",
        )
    )
    return report


RUN_IDENTITY_FIELDS = (
    "run_id",
    "source",
    "operation_kind",
    "account_uuid",
    "owner_user_uuid",
    "stream_uuid",
    "topic_uuid",
    "provider_event_uuid",
    "payload_sha256",
    "cursor_scope",
    "cursor_ordinal",
    "idempotency_key",
)
PROVIDER_DESTINATION_EVIDENCE = {
    "provider.message.inbound": "workspace_backend",
    "provider.message.outbound": "provider_connector",
}
PROVIDER_EXPORTERS = frozenset(PROVIDER_DESTINATION_EVIDENCE.values())


def verify_run_ledgers(expected_paths, observed_paths):
    """Verify composable native/provider run ledgers by exact operation IDs."""
    expected = {}
    duplicate_expectations = []
    expected_cursor_positions = {}
    cursor_errors = []
    unsupported_operation_kinds = []
    for path in expected_paths:
        for line_number, row in read_json_lines(path):
            if row["schema_version"] != RUN_LEDGER_SCHEMA_VERSION:
                raise ValueError(
                    f"expected run ledger line {line_number} has wrong schema"
                )
            key = (row["run_id"], row["source"], row["operation_uuid"])
            if key in expected:
                duplicate_expectations.append(":".join(key))
            expected[key] = row
            if (
                row["source"] == "k6.provider"
                and row["operation_kind"] not in PROVIDER_DESTINATION_EVIDENCE
            ):
                unsupported_operation_kinds.append(row["operation_uuid"])
            if row["cursor_scope"] is None:
                continue
            if row["cursor_ordinal"] <= 0:
                cursor_errors.append(row["operation_uuid"])
                continue
            cursor_key = (
                row["run_id"],
                row["source"],
                row["account_uuid"],
                row["cursor_scope"],
                row["cursor_ordinal"],
            )
            previous = expected_cursor_positions.setdefault(
                cursor_key,
                row["operation_uuid"],
            )
            if previous != row["operation_uuid"]:
                cursor_errors.extend((previous, row["operation_uuid"]))

    observations = collections.defaultdict(list)
    unexpected = []
    identity_errors = []
    outcome_errors = []
    result_missing = []
    evidence_errors = []
    ignored_source_evidence = []
    result_ids = collections.defaultdict(set)
    for path in observed_paths:
        for line_number, row in read_json_lines(path):
            if row["schema_version"] != RUN_LEDGER_SCHEMA_VERSION:
                raise ValueError(
                    f"observed run ledger line {line_number} has wrong schema"
                )
            key = (row["run_id"], row["source"], row["operation_uuid"])
            expected_row = expected.get(key)
            if expected_row is None:
                unexpected.append(":".join(key))
                continue
            evidence_source = row["evidence_source"]
            if expected_row["source"] == "k6.provider":
                destination = PROVIDER_DESTINATION_EVIDENCE.get(
                    expected_row["operation_kind"]
                )
                if destination is None:
                    continue
                if (
                    evidence_source in PROVIDER_EXPORTERS
                    and evidence_source != destination
                ):
                    ignored_source_evidence.append(row["operation_uuid"])
                    continue
                if evidence_source != destination:
                    evidence_errors.append(row["operation_uuid"])
                    continue
            if (
                expected_row["source"] == "k6.native"
                and evidence_source != "workspace_response"
            ):
                evidence_errors.append(row["operation_uuid"])
                continue
            observations[key].append(row)
            if any(row[field] != expected_row[field] for field in RUN_IDENTITY_FIELDS):
                identity_errors.append(row["operation_uuid"])
            if row["outcome"] != "succeeded":
                outcome_errors.append(row["operation_uuid"])
            if row["result_id"] is None:
                result_missing.append(row["operation_uuid"])
            if row["result_id"] is not None:
                result_ids[key].add(row["result_id"])

    missing = [":".join(key) for key in sorted(set(expected) - set(observations))]
    duplicates = [
        ":".join(key)
        for key, values in observations.items()
        if len({canonical_json(value) for value in values}) > 1
    ]
    result_conflicts = [
        ":".join(key) for key, values in result_ids.items() if len(values) > 1
    ]
    report = {
        "schema_version": "workspace.messenger.run-correctness-report/v1",
        "expected_operations": len(expected),
        "observed_operations": len(observations),
        "missing": sorted(missing),
        "unexpected": sorted(set(unexpected)),
        "duplicate_expectations": sorted(set(duplicate_expectations)),
        "duplicate_observations": sorted(set(duplicates)),
        "identity_mismatch": sorted(set(identity_errors)),
        "unsuccessful": sorted(set(outcome_errors)),
        "missing_result_id": sorted(set(result_missing)),
        "non_authoritative_evidence": sorted(set(evidence_errors)),
        "ignored_source_side_evidence": sorted(set(ignored_source_evidence)),
        "unsupported_operation_kind": sorted(set(unsupported_operation_kinds)),
        "cursor_monotonicity": sorted(set(cursor_errors)),
        "result_conflict": sorted(set(result_conflicts)),
    }
    report["passed"] = not any(
        report[key]
        for key in (
            "missing",
            "unexpected",
            "duplicate_expectations",
            "duplicate_observations",
            "identity_mismatch",
            "unsuccessful",
            "missing_result_id",
            "non_authoritative_evidence",
            "unsupported_operation_kind",
            "cursor_monotonicity",
            "result_conflict",
        )
    )
    return report
