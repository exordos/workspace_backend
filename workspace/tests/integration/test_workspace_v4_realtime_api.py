# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0.

"""Realtime-only contract layered on the Workspace v3 Provider store."""

import hashlib
import json
import uuid as sys_uuid


ROOT = "/v1/provider/v4/realtime"


def _hash(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _register(api, db):
    provider_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'zulip', %s)
        """,
        (provider_uuid, api.project_id, api.user_uuid),
    )


def _message_graph(api):
    owner_uuid = sys_uuid.UUID(str(api.user_uuid))
    peer_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    created_at = "2026-09-28T08:00:00Z"
    values = [
        (
            "users",
            owner_uuid,
            {
                "username": "owner",
                "display_name": "Owner",
                "email": "owner@example.test",
                "created_at": created_at,
            },
        ),
        (
            "users",
            peer_uuid,
            {
                "username": "peer",
                "display_name": "Peer",
                "email": "peer@example.test",
                "created_at": created_at,
            },
        ),
        (
            "streams",
            stream_uuid,
            {
                "name": "Realtime",
                "owner_uuid": str(owner_uuid),
                "default_topic_uuid": str(topic_uuid),
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            sys_uuid.uuid4(),
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(owner_uuid),
                "role": "owner",
                "created_at": created_at,
            },
        ),
        (
            "topics",
            topic_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "name": "General",
                "created_at": created_at,
            },
        ),
        (
            "topic_bindings",
            sys_uuid.uuid4(),
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "user_uuid": str(owner_uuid),
                "created_at": created_at,
            },
        ),
        (
            "messages",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(peer_uuid),
                "payload": {"kind": "markdown", "content": "live"},
                "created_at": created_at,
            },
        ),
    ]
    return values, message_uuid, created_at


def _operations(values, source_updated_at):
    return [
        {
            "action": "upsert",
            "type": resource,
            "uuid": str(entity_uuid),
            "content_hash": _hash(data),
            "source_updated_at": source_updated_at,
            "data": data,
        }
        for resource, entity_uuid, data in values
    ]


def test_v4_applies_live_message_graph_without_snapshot_routes(api, db):
    _register(api, db)
    values, message_uuid, created_at = _message_graph(api)
    response = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={"operations": _operations(values, created_at)},
    )

    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["results"]] == [
        "created",
        "created",
        "created",
        "created",
        "created",
        "created",
        "created",
    ]
    row = db.execute(
        """
        SELECT stream.source_name, topic.source_name, message.source_name,
               message.payload ->> 'content'
        FROM workspace_v3.messages AS message
        JOIN workspace_v3.streams AS stream
          ON stream.project_id = message.project_id
         AND stream.uuid = message.stream_uuid
        JOIN workspace_v3.topics AS topic
          ON topic.project_id = message.project_id
         AND topic.uuid = message.topic_uuid
        WHERE message.project_id = %s AND message.uuid = %s
        """,
        (api.project_id, message_uuid),
    ).fetchone()
    assert tuple(row) == ("zulip", "zulip", "zulip", "live")


def test_v4_rejects_history_controls_and_history_resources(api, db):
    _register(api, db)

    backfill = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={"delivery_class": "backfill", "operations": []},
    )
    reaction = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={
            "operations": [
                {
                    "action": "delete",
                    "type": "message_reactions",
                    "uuid": str(sys_uuid.uuid4()),
                }
            ]
        },
    )

    assert backfill.status_code == 422, backfill.text
    assert backfill.json()["error"] == "realtime_operations_only"
    assert reaction.status_code == 422, reaction.text
    assert reaction.json()["error"] == "realtime_entity_type_required"
    assert reaction.json()["item_index"] == 0


def test_v4_requires_message_and_only_deletes_messages(api, db):
    _register(api, db)
    graph_values, _, graph_updated_at = _message_graph(api)
    context_only = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={
            "operations": [
                {
                    "action": "delete",
                    "type": "streams",
                    "uuid": str(sys_uuid.uuid4()),
                }
            ]
        },
    )
    empty = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={"operations": []},
    )
    unrelated_context = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={
            "operations": _operations(graph_values, graph_updated_at)
            + [
                {
                    "action": "upsert",
                    "type": "users",
                    "uuid": str(sys_uuid.uuid4()),
                    "data": {
                        "username": "unrelated",
                        "display_name": "Unrelated",
                    },
                }
            ],
        },
    )
    rebind = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={
            "operations": [
                {
                    "action": "delete",
                    "type": "messages",
                    "uuid": str(sys_uuid.uuid4()),
                    "rebind_identity": True,
                }
            ]
        },
    )

    assert context_only.status_code == 422, context_only.text
    assert context_only.json()["error"] == "realtime_message_delete_required"
    assert empty.status_code == 422, empty.text
    assert empty.json()["error"] == "realtime_message_required"
    assert unrelated_context.status_code == 422, unrelated_context.text
    assert unrelated_context.json()["error"] == "realtime_context_mismatch"
    assert rebind.status_code == 422, rebind.text
    assert rebind.json()["error"] == "realtime_operation_fields"


def test_v4_requires_provider_scope_and_registration(api):
    denied = api.post(ROOT, permissions=(), json={"operations": []})
    unregistered = api.post(
        ROOT,
        permissions=("workspace.provider.sync",),
        json={"operations": []},
    )

    assert denied.status_code == 403, denied.text
    assert denied.json()["error"] == "provider_sync_forbidden"
    assert unregistered.status_code == 403, unregistered.text
    assert unregistered.json()["error"] == "provider_consumer_required"
