# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Provider CRUD contract over the clean Workspace v3 schema."""

import hashlib
import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api.api import sql_canonical_store, store_factory
from workspace.messenger_api.api import store as api_store


ROOT = "/v1/provider/entities"


@pytest.fixture(autouse=True)
def _v3_store():
    api_store.configure_store_factory(store_factory.build_v3_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(
            sql_canonical_store.SQLCanonicalMessengerStoreFactory()
        )


def _hash(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _register_provider(api, db):
    provider_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'zulip', %s)
        """,
        (provider_uuid, api.project_id, api.user_uuid),
    )
    return provider_uuid


def _put(api, resource, entity_uuid, data, content_hash=None):
    return api.put(
        f"{ROOT}/{resource}/{entity_uuid}",
        json={
            "content_hash": content_hash or _hash(data),
            "data": data,
        },
    )


def _user_data(entity_uuid, **overrides):
    values = {
        "username": f"zulip-{entity_uuid}",
        "display_name": "Zulip User",
        "email": f"{entity_uuid}@example.test",
        "status": "active",
        "disabled": False,
        "is_bot": False,
        "created_at": "2026-09-19T08:00:00Z",
    }
    values.update(overrides)
    return values


def test_provider_api_requires_an_enabled_provider_consumer(api):
    response = api.get(f"{ROOT}/users/{sys_uuid.uuid4()}")

    assert response.status_code == 403, response.text
    assert response.json()["error"] == "provider_consumer_required"


def test_provider_api_user_crud_is_idempotent_and_private_fields_do_not_leak(
    api, db
):
    _register_provider(api, db)
    user_uuid = sys_uuid.uuid4()
    data = _user_data(user_uuid, disabled=True, is_bot=True)
    content_hash = _hash(data)

    created = _put(api, "users", user_uuid, data, content_hash)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "created"
    assert created.json()["updated_at"].endswith("Z")

    unchanged = _put(
        api,
        "users",
        user_uuid,
        {**data, "display_name": "Ignored duplicate"},
        content_hash,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["status"] == "unchanged"

    provider_view = api.get(f"{ROOT}/users/{user_uuid}")
    assert provider_view.status_code == 200, provider_view.text
    assert provider_view.json()["data"]["display_name"] == "Zulip User"
    assert provider_view.json()["data"]["disabled"] is True
    assert provider_view.json()["data"]["is_bot"] is True

    invalid_time = _put(
        api,
        "users",
        sys_uuid.uuid4(),
        _user_data(sys_uuid.uuid4(), created_at="2026-09-19T11:00:00+03:00"),
    )
    assert invalid_time.status_code == 422, invalid_time.text
    assert invalid_time.json()["error"] == "invalid_timestamp"

    public_view = api.get(f"/v1/users/{user_uuid}")
    assert public_view.status_code == 200, public_view.text
    assert "disabled" not in public_view.json()
    assert "is_bot" not in public_view.json()

    other_project = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, 'zulip', %s)
        """,
        (sys_uuid.uuid4(), other_project, api.user_uuid),
    )
    attached = api.put(
        f"{ROOT}/users/{user_uuid}",
        project=other_project,
        json={"content_hash": content_hash, "data": data},
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["status"] == "created"

    deleted = api.delete(f"{ROOT}/users/{user_uuid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    missing = api.get(f"{ROOT}/users/{user_uuid}")
    assert missing.status_code == 404, missing.text
    shared = api.get(f"{ROOT}/users/{user_uuid}", project=other_project)
    assert shared.status_code == 200, shared.text
    removed_last = api.delete(f"{ROOT}/users/{user_uuid}", project=other_project)
    assert removed_last.status_code == 200, removed_last.text
    assert api.get(f"{ROOT}/users/{user_uuid}", project=other_project).status_code == 404


def test_provider_batch_is_atomic_and_reports_the_failed_item(api, db):
    _register_provider(api, db)
    user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    user = _user_data(user_uuid)
    stream = {
        "name": "Broken stream",
        "owner_uuid": str(sys_uuid.uuid4()),
        "created_at": "2026-09-19T08:00:01Z",
    }

    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": "users",
                    "uuid": str(user_uuid),
                    "content_hash": _hash(user),
                    "data": user,
                },
                {
                    "action": "upsert",
                    "type": "streams",
                    "uuid": str(stream_uuid),
                    "content_hash": _hash(stream),
                    "data": stream,
                },
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["item_index"] == 1
    assert response.json()["error"] == "invalid_entity"
    assert api.get(f"{ROOT}/users/{user_uuid}").status_code == 404


def test_provider_batch_imports_graph_emits_events_and_cleans_cascades(api, db):
    provider_uuid = _register_provider(api, db)
    owner_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    owner_binding_uuid = sys_uuid.uuid4()
    peer_binding_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    owner_topic_binding_uuid = sys_uuid.uuid4()
    peer_topic_binding_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    owner_flag_uuid = sys_uuid.uuid4()
    peer_flag_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    entities = [
        ("users", owner_uuid, _user_data(owner_uuid)),
        ("users", peer_uuid, _user_data(peer_uuid, is_bot=True)),
        (
            "streams",
            stream_uuid,
            {
                "name": "Imported stream",
                "description": "Provider data",
                "owner_uuid": str(owner_uuid),
                "default_topic_uuid": str(topic_uuid),
                "history_public_to_subscribers": True,
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            owner_binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(owner_uuid),
                "who_uuid": str(owner_uuid),
                "role": "owner",
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            peer_binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(peer_uuid),
                "who_uuid": str(owner_uuid),
                "role": "member",
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
            owner_topic_binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "user_uuid": str(owner_uuid),
                "created_at": created_at,
            },
        ),
        (
            "topic_bindings",
            peer_topic_binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "user_uuid": str(peer_uuid),
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
                "payload": {"kind": "markdown", "content": "from bot"},
                "created_at": created_at,
            },
        ),
        (
            "message_flags",
            owner_flag_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "message_uuid": str(message_uuid),
                "user_uuid": str(owner_uuid),
                "read": True,
            },
        ),
        (
            "message_flags",
            peer_flag_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "message_uuid": str(message_uuid),
                "user_uuid": str(peer_uuid),
                "read": True,
            },
        ),
        (
            "message_reactions",
            reaction_uuid,
            {
                "message_uuid": str(message_uuid),
                "user_uuid": str(owner_uuid),
                "emoji_name": "thumbs_up",
                "created_at": created_at,
            },
        ),
    ]
    operations = [
        {
            "action": "upsert",
            "type": resource,
            "uuid": str(entity_uuid),
            "content_hash": _hash(data),
            "data": data,
        }
        for resource, entity_uuid, data in entities
    ]

    imported = api.post(
        f"{ROOT}/actions/apply/invoke", json={"operations": operations}
    )
    assert imported.status_code == 200, imported.text
    assert {item["status"] for item in imported.json()["results"]} == {"created"}

    message = api.get(f"{ROOT}/messages/{message_uuid}")
    assert message.status_code == 200, message.text
    assert message.json()["data"]["payload"]["content"] == "from bot"
    first_page = api.get(
        f"{ROOT}/users", params={"limit": 1}
    ).json()
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"] is not None
    second_page = api.get(
        f"{ROOT}/users",
        params={
            "limit": 1,
            "updated_after": first_page["next_cursor"]["updated_after"],
            "after_uuid": first_page["next_cursor"]["after_uuid"],
        },
    ).json()
    assert len(second_page["items"]) == 1

    state_count = db.execute(
        """
        SELECT count(*) FROM workspace_v3.provider_entity_states
        WHERE project_id = %s AND provider_uuid = %s
        """,
        (api.project_id, provider_uuid),
    ).fetchone()[0]
    assert state_count == len(entities)
    origin_events = db.execute(
        """
        SELECT count(*) FROM workspace_v3.events
        WHERE project_id = %s
          AND origin_consumer_type = 'provider'
          AND origin_consumer_uuid = %s
        """,
        (api.project_id, provider_uuid),
    ).fetchone()[0]
    assert origin_events > 0
    echoed = db.execute(
        """
        SELECT count(*)
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s
          AND event.origin_consumer_uuid = %s
          AND audience.consumer_type = 'provider'
          AND audience.consumer_uuid = %s
        """,
        (api.project_id, provider_uuid, provider_uuid),
    ).fetchone()[0]
    assert echoed == 0

    message_data = next(
        data
        for resource, entity_uuid, data in entities
        if resource == "messages" and entity_uuid == message_uuid
    )
    identity_change = _put(
        api,
        "messages",
        message_uuid,
        {**message_data, "author_uuid": str(owner_uuid)},
    )
    assert identity_change.status_code == 409, identity_change.text
    assert identity_change.json()["error"] == "entity_identity_conflict"

    blocked = api.delete(f"{ROOT}/users/{peer_uuid}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"] == "provider_user_is_referenced"

    removed = api.delete(f"{ROOT}/streams/{stream_uuid}")
    assert removed.status_code == 200, removed.text
    remaining_states = db.execute(
        """
        SELECT entity_type FROM workspace_v3.provider_entity_states
        WHERE project_id = %s AND provider_uuid = %s
        ORDER BY entity_type
        """,
        (api.project_id, provider_uuid),
    ).fetchall()
    assert [row[0] for row in remaining_states] == ["user", "user"]
