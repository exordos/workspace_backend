# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Provider CRUD contract over the clean Workspace v3 schema."""

import datetime
import hashlib
import json
import uuid as sys_uuid

import pytest

from workspace.messenger_api.api import sql_canonical_store, store_factory
from workspace.messenger_api.api import store as api_store
from workspace.workspace_v3 import projections


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


def _register_provider(api, db, name="zulip"):
    provider_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.provider_consumers (
            uuid, project_id, name, iam_user_uuid
        ) VALUES (%s, %s, %s, %s)
        """,
        (provider_uuid, api.project_id, name, api.user_uuid),
    )
    return provider_uuid


def _put(
    api,
    resource,
    entity_uuid,
    data,
    content_hash=None,
    *,
    rebind_identity=False,
    source_updated_at=None,
):
    payload = {
        "content_hash": content_hash or _hash(data),
        "data": data,
        "rebind_identity": rebind_identity,
    }
    if source_updated_at is not None:
        payload["source_updated_at"] = source_updated_at
    return api.put(
        f"{ROOT}/{resource}/{entity_uuid}",
        json=payload,
    )


def _drain_projections(db):
    while True:
        with db.transaction():
            tasks = projections.claim_projection_tasks(
                db,
                "integration:v3:provider-api",
                batch_size=1000,
            )
        if not tasks:
            return
        with db.transaction():
            projections.process_claimed_projection_tasks(
                db,
                "integration:v3:provider-api",
                tasks,
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


def test_provider_api_accepts_generic_source_names(api, db):
    _register_provider(api, db, name="mattermost")
    user_uuid = sys_uuid.uuid4()
    created = _put(api, "users", user_uuid, _user_data(user_uuid))
    assert created.status_code == 200, created.text
    assert api.get(f"{ROOT}/users/{user_uuid}").status_code == 200

    stream_uuid = sys_uuid.uuid4()
    stream = _put(
        api,
        "streams",
        stream_uuid,
        {
            "name": "Generic provider stream",
            "owner_uuid": str(user_uuid),
            "created_at": "2026-09-19T08:00:00Z",
        },
    )
    assert stream.status_code == 200, stream.text
    public = api.get(f"/v1/streams/{stream_uuid}")
    assert public.status_code == 404
    assert (
        db.execute(
            """
        SELECT source_name FROM workspace_v3.streams
        WHERE project_id = %s AND uuid = %s
        """,
            (api.project_id, stream_uuid),
        ).fetchone()[0]
        == "mattermost"
    )
    assert api.delete(f"{ROOT}/streams/{stream_uuid}").status_code == 200
    assert api.delete(f"{ROOT}/users/{user_uuid}").status_code == 200
    db.execute(
        """
        DELETE FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND name = 'mattermost'
        """,
        (api.project_id,),
    )


def test_provider_api_rejects_cross_project_entity_uuid_collisions(api, db):
    _register_provider(api, db)
    owner_uuid = sys_uuid.uuid4()
    owner_data = _user_data(owner_uuid)
    assert _put(api, "users", owner_uuid, owner_data).status_code == 200
    stream_uuid = sys_uuid.uuid4()
    original_data = {
        "name": "Original project stream",
        "owner_uuid": str(owner_uuid),
        "created_at": "2026-09-19T08:00:00Z",
    }
    assert _put(api, "streams", stream_uuid, original_data).status_code == 200

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
        f"{ROOT}/users/{owner_uuid}",
        project=other_project,
        json={"content_hash": _hash(owner_data), "data": owner_data},
    )
    assert attached.status_code == 200, attached.text

    conflicting_data = {**original_data, "name": "Other project stream"}
    conflicting = api.put(
        f"{ROOT}/streams/{stream_uuid}",
        project=other_project,
        json={
            "content_hash": _hash(conflicting_data),
            "data": conflicting_data,
        },
    )

    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["error"] == "entity_project_conflict"
    row = db.execute(
        """
        SELECT project_id, name FROM workspace_v3.streams WHERE uuid = %s
        """,
        (stream_uuid,),
    ).fetchone()
    assert row == (sys_uuid.UUID(api.project_id), "Original project stream")


def test_provider_users_are_visible_only_in_projects_that_reference_them(api, db):
    _register_provider(api, db)
    provider_user_uuid = sys_uuid.uuid4()
    assert (
        _put(
            api, "users", provider_user_uuid, _user_data(provider_user_uuid)
        ).status_code
        == 200
    )
    stream_uuid = sys_uuid.uuid4()
    assert (
        _put(
            api,
            "streams",
            stream_uuid,
            {
                "name": "Project-scoped provider directory",
                "owner_uuid": str(provider_user_uuid),
                "created_at": "2026-09-19T08:00:00Z",
            },
        ).status_code
        == 200
    )

    other_project = sys_uuid.uuid4()
    own_project_users = api.get("/v1/users/")
    other_project_users = api.get("/v1/users/", project=other_project)

    assert own_project_users.status_code == 200, own_project_users.text
    assert other_project_users.status_code == 200, other_project_users.text
    assert str(provider_user_uuid) in {row["uuid"] for row in own_project_users.json()}
    assert str(provider_user_uuid) not in {
        row["uuid"] for row in other_project_users.json()
    }
    assert str(api.user_uuid) in {row["uuid"] for row in other_project_users.json()}


def test_provider_bootstrap_includes_owned_and_reachable_users(api, db):
    _register_provider(api, db)
    owner_uuid = sys_uuid.uuid4()
    orphan_uuid = sys_uuid.uuid4()
    who_uuid = sys_uuid.uuid4()
    reactor_uuid = sys_uuid.uuid4()
    for user_uuid in (who_uuid, reactor_uuid):
        db.execute(
            """
            INSERT INTO workspace_v3.users (
                uuid, created_at, updated_at, username, source, status, avatar
            ) VALUES (
                %s, NOW(), NOW(), %s, 'iam', 'active',
                'urn:gravatar:00000000000000000000000000000000'
            )
            """,
            (user_uuid, f"iam-{user_uuid}"),
        )
    for user_uuid in (owner_uuid, orphan_uuid):
        created = _put(api, "users", user_uuid, _user_data(user_uuid))
        assert created.status_code == 200, created.text

    stream_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    graph = (
        (
            "streams",
            stream_uuid,
            {
                "name": "Complete provider users",
                "owner_uuid": str(owner_uuid),
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(owner_uuid),
                "who_uuid": str(who_uuid),
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
            "messages",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(owner_uuid),
                "payload": {"kind": "markdown", "content": "Hello"},
                "created_at": created_at,
            },
        ),
    )
    for resource, entity_uuid, data in graph:
        created = _put(api, resource, entity_uuid, data)
        assert created.status_code == 200, created.text
    db.execute(
        """
        INSERT INTO workspace_v3.message_reactions (
            uuid, project_id, message_uuid, user_uuid,
            emoji_name, source_name, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, 'eyes', 'native', NOW(), NOW())
        """,
        (
            sys_uuid.uuid4(),
            api.project_id,
            message_uuid,
            reactor_uuid,
        ),
    )

    expected_users = {owner_uuid, orphan_uuid, who_uuid, reactor_uuid}
    snapshot = api.get("/v1/provider/bootstrap")
    assert snapshot.status_code == 200, snapshot.text
    snapshot_users = {
        sys_uuid.UUID(record["uuid"])
        for record in map(json.loads, snapshot.text.splitlines())
        if record.get("record") == "entity" and record.get("type") == "users"
    }
    assert snapshot_users == expected_users

    page = api.get(
        f"{ROOT}/users",
        params={"limit": 100, "snapshot_after_uuid": str(sys_uuid.UUID(int=0))},
    )
    assert page.status_code == 200, page.text
    assert {sys_uuid.UUID(item["uuid"]) for item in page.json()["items"]} == (
        expected_users
    )


def test_provider_api_orders_updates_by_source_timestamp(api, db):
    _register_provider(api, db)
    user_uuid = sys_uuid.uuid4()
    initial = _user_data(user_uuid, display_name="Initial")
    latest = _user_data(user_uuid, display_name="Latest")
    stale = _user_data(user_uuid, display_name="Stale")
    assert (
        _put(
            api,
            "users",
            user_uuid,
            initial,
            source_updated_at="2026-09-19T08:00:00Z",
        ).status_code
        == 200
    )
    updated = _put(
        api,
        "users",
        user_uuid,
        latest,
        source_updated_at="2026-09-19T09:00:00Z",
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "updated"

    older = _put(
        api,
        "users",
        user_uuid,
        stale,
        source_updated_at="2026-09-19T08:30:00Z",
    )
    assert older.status_code == 200, older.text
    assert older.json()["status"] == "unchanged"
    assert (
        api.get(f"{ROOT}/users/{user_uuid}").json()["data"]["display_name"] == "Latest"
    )

    equal_conflict = _put(
        api,
        "users",
        user_uuid,
        stale,
        source_updated_at="2026-09-19T09:00:00Z",
    )
    assert equal_conflict.status_code == 409, equal_conflict.text
    assert equal_conflict.json()["error"] == "entity_version_conflict"

    same_newer = _put(
        api,
        "users",
        user_uuid,
        latest,
        _hash(latest),
        source_updated_at="2026-09-19T10:00:00Z",
    )
    assert same_newer.status_code == 200, same_newer.text
    assert same_newer.json()["status"] == "unchanged"
    assert same_newer.json()["source_updated_at"] == "2026-09-19T10:00:00Z"


def test_provider_api_user_crud_is_idempotent_and_private_fields_do_not_leak(api, db):
    _register_provider(api, db)
    user_uuid = sys_uuid.uuid4()
    data = _user_data(user_uuid, disabled=True, is_bot=True)
    content_hash = _hash(data)

    created = _put(api, "users", user_uuid, data, content_hash)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "created"
    assert created.json()["updated_at"].endswith("Z")

    provider_view = api.get(f"{ROOT}/users/{user_uuid}")
    assert provider_view.status_code == 200, provider_view.text
    canonical = provider_view.json()
    assert canonical["content_hash"] == _hash(canonical["data"])

    unchanged = _put(
        api,
        "users",
        user_uuid,
        data,
        content_hash,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["status"] == "unchanged"

    provider_view = api.get(f"{ROOT}/users/{user_uuid}")
    assert provider_view.status_code == 200, provider_view.text
    assert provider_view.json()["data"]["display_name"] == "Zulip User"
    assert provider_view.json()["data"]["disabled"] is True
    assert provider_view.json()["data"]["is_bot"] is True

    stream_uuid = sys_uuid.uuid4()
    stream_data = {
        "name": "Provider user visibility",
        "owner_uuid": str(user_uuid),
        "created_at": "2026-09-19T08:00:00Z",
    }
    stream = _put(api, "streams", stream_uuid, stream_data)
    assert stream.status_code == 200, stream.text

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

    referenced_shared = api.delete(f"{ROOT}/users/{user_uuid}")
    assert referenced_shared.status_code == 409, referenced_shared.text
    assert referenced_shared.json()["error"] == "provider_user_is_referenced"

    removed_stream = api.delete(f"{ROOT}/streams/{stream_uuid}")
    assert removed_stream.status_code == 200, removed_stream.text
    deleted = api.delete(f"{ROOT}/users/{user_uuid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    missing = api.get(f"{ROOT}/users/{user_uuid}")
    assert missing.status_code == 404, missing.text
    shared = api.get(f"{ROOT}/users/{user_uuid}", project=other_project)
    assert shared.status_code == 200, shared.text
    removed_last = api.delete(f"{ROOT}/users/{user_uuid}", project=other_project)
    assert removed_last.status_code == 200, removed_last.text
    assert (
        api.get(f"{ROOT}/users/{user_uuid}", project=other_project).status_code == 404
    )


def test_provider_api_rejects_new_children_of_native_streams(api, db):
    _register_provider(api, db)
    provider_owner_uuid = sys_uuid.uuid4()
    assert (
        _put(
            api,
            "users",
            provider_owner_uuid,
            _user_data(provider_owner_uuid),
        ).status_code
        == 200
    )
    provider_stream_uuid = sys_uuid.uuid4()
    assert (
        _put(
            api,
            "streams",
            provider_stream_uuid,
            {
                "name": "Provider stream",
                "owner_uuid": str(provider_owner_uuid),
                "created_at": "2026-09-19T08:00:00Z",
            },
        ).status_code
        == 200
    )
    native_stream = api.post(
        "/v1/streams/",
        json={
            "name": "Native stream",
            "source_name": "native",
            "source": {"kind": "native"},
        },
    )
    assert native_stream.status_code == 201, native_stream.text

    topic_uuid = sys_uuid.uuid4()
    blocked = _put(
        api,
        "topics",
        topic_uuid,
        {
            "stream_uuid": native_stream.json()["uuid"],
            "name": "Must remain native",
            "created_at": "2026-09-19T08:00:00Z",
        },
    )

    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"] == "entity_not_provider_owned"
    assert api.get(f"{ROOT}/topics/{topic_uuid}").status_code == 404

    owned_topic_uuid = sys_uuid.uuid4()
    owned_topic = {
        "stream_uuid": str(provider_stream_uuid),
        "name": "Provider topic",
        "created_at": "2026-09-19T08:00:00Z",
    }
    assert _put(api, "topics", owned_topic_uuid, owned_topic).status_code == 200
    moved = _put(
        api,
        "topics",
        owned_topic_uuid,
        {**owned_topic, "stream_uuid": native_stream.json()["uuid"]},
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"] == "entity_not_provider_owned"
    assert api.get(f"{ROOT}/topics/{owned_topic_uuid}").json()["data"][
        "stream_uuid"
    ] == str(provider_stream_uuid)


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

    invalid_delivery = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={"delivery_class": "archive", "operations": []},
    )
    assert invalid_delivery.status_code == 400, invalid_delivery.text
    assert invalid_delivery.json()["error"] == "invalid_delivery_class"


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
                "direct_user_uuid": str(peer_uuid),
                "private": True,
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

    imported = api.post(f"{ROOT}/actions/apply/invoke", json={"operations": operations})
    assert imported.status_code == 200, imported.text
    assert {item["status"] for item in imported.json()["results"]} == {"created"}

    message = api.get(f"{ROOT}/messages/{message_uuid}")
    assert message.status_code == 200, message.text
    assert message.json()["data"]["payload"]["content"] == "from bot"
    first_page = api.get(f"{ROOT}/users", params={"limit": 1}).json()
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

    snapshot = api.get("/v1/provider/bootstrap")
    assert snapshot.status_code == 200, snapshot.text
    records = [json.loads(line) for line in snapshot.text.splitlines()]
    assert records[0]["record"] == "meta"
    assert records[0]["provider_uuid"] == str(provider_uuid)
    assert records[-1]["record"] == "complete"
    assert records[-1]["snapshot_uuid"] == records[0]["snapshot_uuid"]
    assert records[-1]["counts"] == {
        "message_flags": 2,
        "message_reactions": 1,
        "messages": 1,
        "stream_bindings": 2,
        "streams": 1,
        "topic_bindings": 2,
        "topics": 1,
        "users": 2,
    }
    entity_lines = [
        line + "\n"
        for line in snapshot.text.splitlines()
        if json.loads(line)["record"] == "entity"
    ]
    assert (
        records[-1]["sha256"]
        == hashlib.sha256("".join(entity_lines).encode()).hexdigest()
    )
    assert all(
        record["source_updated_at"].endswith("Z")
        for record in records
        if record["record"] == "entity"
    )

    native_message_uuid = sys_uuid.uuid4()
    native_flag_uuid = sys_uuid.uuid4()
    native_reaction_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.messages (
            uuid, project_id, stream_uuid, topic_uuid,
            author_uuid, payload, source_name
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'native')
        """,
        (
            native_message_uuid,
            api.project_id,
            stream_uuid,
            topic_uuid,
            owner_uuid,
            json.dumps({"kind": "markdown", "content": "from Workspace"}),
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.message_flags (
            uuid, project_id, stream_uuid, message_uuid, user_uuid, read
        ) VALUES (%s, %s, %s, %s, %s, false)
        """,
        (
            native_flag_uuid,
            api.project_id,
            stream_uuid,
            native_message_uuid,
            owner_uuid,
        ),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.message_reactions (
            uuid, project_id, message_uuid, user_uuid,
            emoji_name, source_name
        ) VALUES (%s, %s, %s, %s, 'eyes', 'native')
        """,
        (
            native_reaction_uuid,
            api.project_id,
            native_message_uuid,
            owner_uuid,
        ),
    )

    manifest_response = api.get("/v1/provider/bootstrap?mode=paged")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest = manifest_response.json()
    assert manifest["record"] == "manifest"
    assert manifest["schema_version"] == 2
    assert manifest["project_id"] == str(api.project_id)
    assert manifest["provider_uuid"] == str(provider_uuid)
    assert manifest["snapshot_epoch_version"] >= 0
    assert manifest["created_at"].endswith("Z")
    bootstrap_page = api.get(
        f"{ROOT}/users?limit=1&snapshot_after_uuid={sys_uuid.UUID(int=0)}"
    )
    assert bootstrap_page.status_code == 200, bootstrap_page.text
    bootstrap_body = bootstrap_page.json()
    assert len(bootstrap_body["items"]) == 1
    assert bootstrap_body["items"][0]["created_at"].endswith("Z")
    assert bootstrap_body["items"][0]["updated_at"].endswith("Z")
    assert bootstrap_body["next_cursor"] is not None
    assert (
        bootstrap_body["next_cursor"]["snapshot_after_uuid"]
        == (bootstrap_body["next_cursor"]["after_uuid"])
    )
    continued_bootstrap = api.get(
        f"{ROOT}/users",
        params={"limit": 1, **bootstrap_body["next_cursor"]},
    )
    assert continued_bootstrap.status_code == 200, continued_bootstrap.text
    assert len(continued_bootstrap.json()["items"]) == 1
    assert (
        continued_bootstrap.json()["items"][0]["uuid"]
        != (bootstrap_body["items"][0]["uuid"])
    )
    for resource, entity_uuid in (
        ("messages", native_message_uuid),
        ("message_flags", native_flag_uuid),
        ("message_reactions", native_reaction_uuid),
    ):
        page = api.get(
            f"{ROOT}/{resource}",
            params={
                "limit": 100,
                "snapshot_after_uuid": str(sys_uuid.UUID(int=0)),
            },
        )
        assert page.status_code == 200, page.text
        assert str(entity_uuid) in {item["uuid"] for item in page.json()["items"]}

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

    rebound_data = {**message_data, "author_uuid": str(owner_uuid)}
    identity_rebind = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": "messages",
                    "uuid": str(message_uuid),
                    "content_hash": _hash(rebound_data),
                    "data": rebound_data,
                    "rebind_identity": True,
                }
            ]
        },
    )
    assert identity_rebind.status_code == 200, identity_rebind.text
    assert identity_rebind.json()["results"][0]["status"] == "updated"
    rebound_message = api.get(f"{ROOT}/messages/{message_uuid}")
    assert rebound_message.json()["data"]["author_uuid"] == str(owner_uuid)

    owner_binding_data = next(
        data
        for resource, entity_uuid, data in entities
        if resource == "stream_bindings" and entity_uuid == owner_binding_uuid
    )
    db.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status, avatar
        ) VALUES (
            %s, NOW(), NOW(), %s, 'iam', 'active',
            'urn:gravatar:00000000000000000000000000000000'
        )
        ON CONFLICT (uuid) DO NOTHING
        """,
        (api.user_uuid, f"iam-{api.user_uuid}"),
    )
    rebound_binding_data = {
        **owner_binding_data,
        "user_uuid": str(api.user_uuid),
        "who_uuid": str(api.user_uuid),
    }
    dependent_source_states = {
        row[0]: (bytes(row[1]), row[2])
        for row in db.execute(
            """
            SELECT entity_uuid, source_content_hash, source_updated_at
            FROM workspace_v3.provider_entity_states
            WHERE project_id = %s AND provider_uuid = %s
              AND entity_uuid IN (%s, %s)
            """,
            (
                api.project_id,
                provider_uuid,
                owner_topic_binding_uuid,
                owner_flag_uuid,
            ),
        ).fetchall()
    }
    binding_rebind = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": "stream_bindings",
                    "uuid": str(owner_binding_uuid),
                    "content_hash": _hash(rebound_binding_data),
                    "data": rebound_binding_data,
                    "rebind_identity": True,
                }
            ]
        },
    )
    assert binding_rebind.status_code == 200, binding_rebind.text
    rebound_binding = api.get(f"{ROOT}/stream_bindings/{owner_binding_uuid}")
    assert rebound_binding.json()["data"]["user_uuid"] == str(api.user_uuid)
    dependent_users = db.execute(
        """
        SELECT
            (SELECT user_uuid FROM workspace_v3.topic_bindings
             WHERE project_id = %s AND uuid = %s),
            (SELECT user_uuid FROM workspace_v3.message_flags
             WHERE project_id = %s AND uuid = %s)
        """,
        (
            api.project_id,
            owner_topic_binding_uuid,
            api.project_id,
            owner_flag_uuid,
        ),
    ).fetchone()
    assert dependent_users == (sys_uuid.UUID(api.user_uuid),) * 2
    for resource, entity_uuid in (
        ("topic_bindings", owner_topic_binding_uuid),
        ("message_flags", owner_flag_uuid),
    ):
        dependent = api.get(f"{ROOT}/{resource}/{entity_uuid}")
        assert dependent.status_code == 200, dependent.text
        assert dependent.json()["data"]["user_uuid"] == str(api.user_uuid)
        assert dependent.json()["content_hash"] == _hash(dependent.json()["data"])
        source_state = db.execute(
            """
            SELECT source_content_hash, source_updated_at
            FROM workspace_v3.provider_entity_states
            WHERE project_id = %s AND provider_uuid = %s
              AND entity_uuid = %s
            """,
            (api.project_id, provider_uuid, entity_uuid),
        ).fetchone()
        assert (bytes(source_state[0]), source_state[1]) == dependent_source_states[
            entity_uuid
        ]

    for resource, entity_uuid in (
        ("topic_bindings", owner_topic_binding_uuid),
        ("message_flags", owner_flag_uuid),
    ):
        original_data = next(
            data
            for item_resource, item_uuid, data in entities
            if item_resource == resource and item_uuid == entity_uuid
        )
        source_hash, source_updated_at = dependent_source_states[entity_uuid]
        repeated = _put(
            api,
            resource,
            entity_uuid,
            original_data,
            content_hash=source_hash.hex(),
            source_updated_at=source_updated_at.astimezone(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["status"] == "unchanged"

    removed_member_event = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'stream.deleted'
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        """,
        (api.project_id, stream_uuid, owner_uuid),
    ).fetchone()
    assert removed_member_event == ("stream.deleted",)

    removed_binding = api.delete(f"{ROOT}/stream_bindings/{owner_binding_uuid}")
    assert removed_binding.status_code == 200, removed_binding.text
    provider_removed_member_event = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'stream.deleted'
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        ORDER BY event.created_at DESC
        LIMIT 1
        """,
        (api.project_id, stream_uuid, api.user_uuid),
    ).fetchone()
    assert provider_removed_member_event == ("stream.deleted",)

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


def test_provider_graph_keeps_the_unchanged_client_contract(
    api,
    workspace_api,
    db,
):
    _register_provider(api, db)
    owner_uuid = api.user_uuid
    bot_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    stream_binding_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    entities = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        (
            "users",
            bot_uuid,
            _user_data(bot_uuid, display_name="Imported bot", is_bot=True),
        ),
        (
            "streams",
            stream_uuid,
            {
                "name": "Imported channel",
                "owner_uuid": str(owner_uuid),
                "default_topic_uuid": str(topic_uuid),
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            stream_binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(owner_uuid),
                "who_uuid": str(owner_uuid),
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
                "author_uuid": str(bot_uuid),
                "payload": {"kind": "markdown", "content": "bot message"},
                "created_at": created_at,
            },
        ),
        (
            "message_flags",
            sys_uuid.uuid4(),
            {
                "stream_uuid": str(stream_uuid),
                "message_uuid": str(message_uuid),
                "user_uuid": str(owner_uuid),
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
    imported = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in entities
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    _drain_projections(db)
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id

    stream = api.get(f"/v1/streams/{stream_uuid}")
    assert stream.status_code == 200, stream.text
    assert stream.json()["description"] == ""
    assert stream.json()["source_name"] == "zulip"
    assert stream.json()["source"] == {"kind": "zulip", "stream_id": 0}
    assert "provider" not in stream.json()
    assert "delivery" not in stream.json()

    topic = api.get(f"/v1/stream_topics/{topic_uuid}")
    assert topic.status_code == 200, topic.text
    assert topic.json()["is_default"] is True

    message = api.get(f"/v1/messages/{message_uuid}")
    assert message.status_code == 200, message.text
    assert message.json()["source_name"] == "zulip"
    assert message.json()["source"] == {"kind": "zulip", "stream_id": 0}
    assert message.json()["author_uuid"] == str(bot_uuid)
    assert message.json()["read"] is True
    assert message.json()["reactions"] == {"thumbs_up": 1}
    assert message.json()["reaction_users"] == {"thumbs_up": [str(owner_uuid)]}
    assert message.json()["created_at"].endswith("Z")
    assert message.json()["updated_at"].endswith("Z")

    user = api.get(f"/v1/users/{bot_uuid}")
    assert user.status_code == 200, user.text
    assert user.json()["source"] == "zulip"
    assert "is_bot" not in user.json()

    events = workspace_api.get("/v1/events/?epoch_version%3E=0&page_limit=100")
    assert events.status_code == 200, events.text
    reaction_events = [
        event
        for event in events.json()
        if event["payload"]["kind"] == "message_reaction.created"
    ]
    assert len(reaction_events) == 1
    reaction_event = reaction_events[0]
    assert reaction_event["payload"]["source"] == {
        "kind": "zulip",
        "stream_id": 0,
    }

    deleted_reaction = api.delete(f"{ROOT}/message_reactions/{reaction_uuid}")
    assert deleted_reaction.status_code == 200, deleted_reaction.text
    _drain_projections(db)
    reaction_deletions = db.execute(
        """
        SELECT count(*)
        FROM workspace_v3.events
        WHERE project_id = %s AND entity_uuid = %s
          AND object_type = 'message_reaction' AND action = 'deleted'
        """,
        (api.project_id, reaction_uuid),
    ).fetchone()[0]
    assert reaction_deletions == 1

    updated_binding = {
        "stream_uuid": str(stream_uuid),
        "user_uuid": str(owner_uuid),
        "who_uuid": str(owner_uuid),
        "role": "moderator",
        "created_at": created_at,
    }
    updated = _put(
        api,
        "stream_bindings",
        stream_binding_uuid,
        updated_binding,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "updated"

    refreshed_events = workspace_api.get(
        "/v1/events/?epoch_version%3E=0&page_limit=200"
    )
    assert refreshed_events.status_code == 200, refreshed_events.text
    assert any(
        event["payload"]["kind"] == "stream.created"
        and event["payload"].get("uuid") == str(stream_uuid)
        for event in refreshed_events.json()
    )

    deleted_topic = api.delete(f"{ROOT}/topics/{topic_uuid}")
    assert deleted_topic.status_code == 200, deleted_topic.text
    stream_after_topic_delete = api.get(f"/v1/streams/{stream_uuid}")
    assert stream_after_topic_delete.status_code == 200
    assert stream_after_topic_delete.json()["default_topic_uuid"] is None
    stream_updated = db.execute(
        """
        SELECT event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.payload ->> 'kind' = 'stream.updated'
          AND event.payload ->> 'default_topic_uuid' IS NULL
          AND audience.consumer_type = 'user'
          AND audience.consumer_uuid = %s
        ORDER BY event.created_at DESC
        LIMIT 1
        """,
        (api.project_id, stream_uuid, owner_uuid),
    ).fetchone()
    assert stream_updated == ("stream.updated",)


def test_provider_message_flag_rebind_notifies_old_and_new_viewers(api, db):
    _register_provider(api, db)
    old_user_uuid = sys_uuid.uuid4()
    new_user_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    flag_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    entities = [
        ("users", old_user_uuid, _user_data(old_user_uuid)),
        ("users", new_user_uuid, _user_data(new_user_uuid)),
        (
            "streams",
            stream_uuid,
            {
                "name": "Flag rebind",
                "owner_uuid": str(old_user_uuid),
                "default_topic_uuid": str(topic_uuid),
                "created_at": created_at,
            },
        ),
        *(
            (
                "stream_bindings",
                sys_uuid.uuid4(),
                {
                    "stream_uuid": str(stream_uuid),
                    "user_uuid": str(user_uuid),
                    "who_uuid": str(old_user_uuid),
                    "role": "owner" if user_uuid == old_user_uuid else "member",
                    "created_at": created_at,
                },
            )
            for user_uuid in (old_user_uuid, new_user_uuid)
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
            "messages",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(old_user_uuid),
                "payload": {"kind": "markdown", "content": "rebind"},
                "created_at": created_at,
            },
        ),
    ]
    imported = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in entities
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    original = {
        "stream_uuid": str(stream_uuid),
        "message_uuid": str(message_uuid),
        "user_uuid": str(old_user_uuid),
        "read": True,
    }
    assert _put(api, "message_flags", flag_uuid, original).status_code == 200
    after = db.execute(
        "SELECT COALESCE(max(epoch_version), 0) FROM workspace_v3.events "
        "WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0]

    rebound = {**original, "user_uuid": str(new_user_uuid)}
    response = _put(
        api,
        "message_flags",
        flag_uuid,
        rebound,
        rebind_identity=True,
    )
    assert response.status_code == 200, response.text
    events = db.execute(
        """
        SELECT audience.consumer_uuid, event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.epoch_version > %s AND audience.consumer_type = 'user'
        ORDER BY audience.consumer_uuid, event.epoch_version
        """,
        (api.project_id, message_uuid, after),
    ).fetchall()
    assert set(events) == {
        (old_user_uuid, "message.deleted"),
        (new_user_uuid, "message.updated"),
    }


def test_provider_can_converge_native_messages_only_inside_its_streams(api, db):
    provider_uuid = _register_provider(api, db)
    owner_uuid = api.user_uuid
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    setup = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        (
            "streams",
            stream_uuid,
            {
                "name": "Provider channel",
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
    ]
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in setup
            ]
        },
    )
    assert response.status_code == 200, response.text

    message_uuid = sys_uuid.uuid4()
    deleted_message_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.messages (
            uuid, project_id, stream_uuid, topic_uuid, author_uuid,
            payload, source_name, created_at, updated_at
        ) VALUES
            (%s, %s, %s, %s, %s, %s::jsonb, 'native', NOW(), NOW()),
            (%s, %s, %s, %s, %s, %s::jsonb, 'native', NOW(), NOW())
        """,
        (
            message_uuid,
            api.project_id,
            stream_uuid,
            topic_uuid,
            owner_uuid,
            json.dumps({"kind": "markdown", "content": "before"}),
            deleted_message_uuid,
            api.project_id,
            stream_uuid,
            topic_uuid,
            owner_uuid,
            json.dumps({"kind": "markdown", "content": "delete me"}),
        ),
    )
    updated_data = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "author_uuid": str(owner_uuid),
        "payload": {"kind": "markdown", "content": "edited in provider"},
        "created_at": created_at,
    }
    updated = _put(api, "messages", message_uuid, updated_data)
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "updated"
    row = db.execute(
        """
        SELECT message.payload, message.source_name, state.provider_uuid
        FROM workspace_v3.messages AS message
        JOIN workspace_v3.provider_entity_states AS state
          ON state.project_id = message.project_id
         AND state.entity_type = 'message'
         AND state.entity_uuid = message.uuid
        WHERE message.project_id = %s AND message.uuid = %s
        """,
        (api.project_id, message_uuid),
    ).fetchone()
    assert row[0]["content"] == "edited in provider"
    assert row[1] == "native"
    assert row[2] == provider_uuid

    deleted = api.delete(f"{ROOT}/messages/{deleted_message_uuid}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert (
        db.execute(
            """
        SELECT 1 FROM workspace_v3.messages
        WHERE project_id = %s AND uuid = %s
        """,
            (api.project_id, deleted_message_uuid),
        ).fetchone()
        is None
    )

    native_stream_uuid = sys_uuid.uuid4()
    native_topic_uuid = sys_uuid.uuid4()
    foreign_message_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.streams (
            uuid, project_id, name, owner_uuid, source_name
        ) VALUES (%s, %s, 'Native channel', %s, 'native')
        """,
        (native_stream_uuid, api.project_id, owner_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.topics (
            uuid, project_id, stream_uuid, name, source_name
        ) VALUES (%s, %s, %s, 'General', 'native')
        """,
        (native_topic_uuid, api.project_id, native_stream_uuid),
    )
    db.execute(
        """
        INSERT INTO workspace_v3.messages (
            uuid, project_id, stream_uuid, topic_uuid, author_uuid,
            payload, source_name
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'native')
        """,
        (
            foreign_message_uuid,
            api.project_id,
            native_stream_uuid,
            native_topic_uuid,
            owner_uuid,
            json.dumps({"kind": "markdown", "content": "private"}),
        ),
    )
    blocked_data = {
        **updated_data,
        "stream_uuid": str(native_stream_uuid),
        "topic_uuid": str(native_topic_uuid),
    }
    blocked = _put(api, "messages", foreign_message_uuid, blocked_data)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"] == "entity_not_provider_owned"


def test_provider_backfill_avoids_one_live_event_per_history_row(api, db):
    provider_uuid = _register_provider(api, db)
    owner_uuid = api.user_uuid
    bot_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    setup = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        ("users", bot_uuid, _user_data(bot_uuid, is_bot=True)),
        (
            "streams",
            stream_uuid,
            {
                "name": "Backfill channel",
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
    ]
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "delivery_class": "backfill",
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in setup
            ],
        },
    )
    assert response.status_code == 200, response.text
    events_before = db.execute(
        "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0]
    provider_activity_before = db.execute(
        """
        SELECT updated_at
        FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    ).fetchone()[0]
    unrelated_scope_uuid = sys_uuid.uuid4()
    db.execute(
        """
        INSERT INTO workspace_v3.projection_tasks (
            project_id, task_type, scope_type, scope_uuid,
            user_uuid, payload
        ) VALUES (
            %s, 'read_counters', 'user_stream', %s,
            %s, '{"emit_message_events": false}'::jsonb
        )
        """,
        (api.project_id, unrelated_scope_uuid, owner_uuid),
    )

    history = []
    for number in range(10):
        message_uuid = sys_uuid.uuid4()
        message = {
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "author_uuid": str(bot_uuid),
            "payload": {"kind": "markdown", "content": f"history {number}"},
            "created_at": created_at,
        }
        history.append(("messages", message_uuid, message))
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "delivery_class": "backfill",
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in history
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert db.execute(
        "SELECT count(*) FROM workspace_v3.messages WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0] == len(history)
    assert db.execute(
        "SELECT count(*) FROM workspace_v3.message_flags WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0] == len(history)
    pending_counters = db.execute(
        """
        SELECT count(*)
        FROM workspace_v3.projection_tasks
        WHERE project_id = %s
          AND task_type = 'read_counters'
          AND scope_uuid IN (%s, %s, %s)
          AND status = 'pending'
          AND payload IN (
              '{"emit_message_events": false}'::jsonb,
              '{"emit_message_event": false}'::jsonb
          )
        """,
        (api.project_id, stream_uuid, topic_uuid, unrelated_scope_uuid),
    ).fetchone()[0]
    assert pending_counters > 0
    assert db.execute(
        """
        SELECT updated_at > %s
        FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND uuid = %s
        """,
        (provider_activity_before, api.project_id, provider_uuid),
    ).fetchone()[0]
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
            (api.project_id,),
        ).fetchone()[0]
        == events_before
    )
    assert (
        db.execute(
            """
        SELECT count(*) FROM workspace_v3.events
        WHERE project_id = %s AND origin_consumer_uuid = %s
          AND object_type = 'message'
        """,
            (api.project_id, provider_uuid),
        ).fetchone()[0]
        == 0
    )
    db.execute(
        """
        UPDATE workspace_v3.provider_consumers
        SET updated_at = clock_timestamp() - interval '10 minutes'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    )
    _drain_projections(db)
    events_before_reaction = db.execute(
        "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0]

    reaction_uuid = sys_uuid.uuid4()
    reaction_data = {
        "message_uuid": str(history[0][1]),
        "user_uuid": str(owner_uuid),
        "emoji_name": "eyes",
        "created_at": created_at,
    }
    reaction = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "delivery_class": "backfill",
            "operations": [
                {
                    "action": "upsert",
                    "type": "message_reactions",
                    "uuid": str(reaction_uuid),
                    "content_hash": _hash(reaction_data),
                    "data": reaction_data,
                }
            ],
        },
    )
    assert reaction.status_code == 200, reaction.text
    db.execute(
        """
        UPDATE workspace_v3.provider_consumers
        SET updated_at = clock_timestamp() - interval '10 minutes'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    )
    _drain_projections(db)
    projected = db.execute(
        """
        SELECT reactions, reaction_users
        FROM workspace_v3.messages
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, history[0][1]),
    ).fetchone()
    assert projected == ({"eyes": 1}, {"eyes": [str(owner_uuid)]})
    assert (
        db.execute(
            "SELECT count(*) FROM workspace_v3.events WHERE project_id = %s",
            (api.project_id,),
        ).fetchone()[0]
        == events_before_reaction
    )


def test_provider_message_move_preserves_time_and_reprojects_rekeyed_flags(api, db):
    provider_uuid = _register_provider(api, db)
    owner_uuid = api.user_uuid
    author_uuid = sys_uuid.uuid4()
    reader_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    stream_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    topic_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    created_at = "2026-09-19T08:00:00Z"
    setup = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        ("users", author_uuid, _user_data(author_uuid, display_name="Author")),
        (
            "users",
            reader_uuids[0],
            _user_data(reader_uuids[0], display_name="Old stream reader"),
        ),
        (
            "users",
            reader_uuids[1],
            _user_data(reader_uuids[1], display_name="New stream reader"),
        ),
    ]
    for number, (stream_uuid, topic_uuid) in enumerate(
        zip(stream_uuids, topic_uuids, strict=True)
    ):
        setup.extend(
            [
                (
                    "streams",
                    stream_uuid,
                    {
                        "name": f"Move target {number}",
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
                    "stream_bindings",
                    sys_uuid.uuid4(),
                    {
                        "stream_uuid": str(stream_uuid),
                        "user_uuid": str(reader_uuids[number]),
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
                    sys_uuid.uuid4(),
                    {
                        "stream_uuid": str(stream_uuid),
                        "topic_uuid": str(topic_uuid),
                        "user_uuid": str(owner_uuid),
                        "created_at": created_at,
                    },
                ),
            ]
        )
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in setup
            ]
        },
    )
    assert response.status_code == 200, response.text

    message_uuid = sys_uuid.uuid4()
    original = {
        "stream_uuid": str(stream_uuids[0]),
        "topic_uuid": str(topic_uuids[0]),
        "author_uuid": str(author_uuid),
        "payload": {"kind": "markdown", "content": "before move"},
        "created_at": created_at,
    }
    created = _put(api, "messages", message_uuid, original)
    assert created.status_code == 200, created.text
    db.execute(
        """
        UPDATE workspace_v3.provider_consumers
        SET updated_at = clock_timestamp() - interval '10 minutes'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    )
    _drain_projections(db)
    move_after = db.execute(
        "SELECT COALESCE(max(epoch_version), 0) FROM workspace_v3.events "
        "WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0]

    moved = {
        "stream_uuid": str(stream_uuids[1]),
        "topic_uuid": str(topic_uuids[1]),
        "author_uuid": str(author_uuid),
        "payload": {"kind": "markdown", "content": "after move"},
    }
    updated = _put(api, "messages", message_uuid, moved)
    assert updated.status_code == 200, updated.text
    move_events = db.execute(
        """
        SELECT audience.consumer_uuid, event.payload ->> 'kind'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.epoch_version > %s AND audience.consumer_type = 'user'
        ORDER BY audience.consumer_uuid, event.epoch_version
        """,
        (api.project_id, message_uuid, move_after),
    ).fetchall()
    kinds_by_recipient = {recipient: kind for recipient, kind in move_events}
    assert kinds_by_recipient[reader_uuids[0]] == "message.deleted"
    assert kinds_by_recipient[reader_uuids[1]] == "message.updated"
    canonical = api.get(f"{ROOT}/messages/{message_uuid}")
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["data"]["created_at"] == created_at
    moved_flag = db.execute(
        """
        SELECT stream_uuid, read FROM workspace_v3.message_flags
        WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
        """,
        (api.project_id, message_uuid, owner_uuid),
    ).fetchone()
    assert moved_flag == (stream_uuids[1], False)
    pending_scopes = db.execute(
        """
        SELECT scope_type, scope_uuid, user_uuid, payload
        FROM workspace_v3.projection_tasks
        WHERE project_id = %s AND status = 'pending'
        ORDER BY scope_type, scope_uuid
        """,
        (api.project_id,),
    ).fetchall()
    assert ("user_stream", stream_uuids[1], sys_uuid.UUID(str(owner_uuid))) in {
        (row[0], row[1], row[2]) for row in pending_scopes
    }, pending_scopes
    db.execute(
        """
        UPDATE workspace_v3.provider_consumers
        SET updated_at = clock_timestamp() - interval '10 minutes'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    )
    _drain_projections(db)
    failed_tasks = db.execute(
        """
        SELECT status, task_type, scope_type, scope_uuid, last_error
        FROM workspace_v3.projection_tasks
        WHERE project_id = %s AND status <> 'completed'
        """,
        (api.project_id,),
    ).fetchall()
    assert not failed_tasks, failed_tasks

    counters = db.execute(
        """
        SELECT stream_uuid, unread_count, last_message_uuid
        FROM workspace_v3.stream_bindings
        WHERE project_id = %s AND user_uuid = %s
          AND stream_uuid = ANY(%s::uuid[])
        ORDER BY stream_uuid
        """,
        (api.project_id, owner_uuid, list(stream_uuids)),
    ).fetchall()
    counter_by_stream = {row[0]: (row[1], row[2]) for row in counters}
    assert counter_by_stream[stream_uuids[0]] == (0, None)
    assert counter_by_stream[stream_uuids[1]] == (1, message_uuid)

    provider_flag_uuid = sys_uuid.uuid4()
    flag_data = {
        "stream_uuid": str(stream_uuids[1]),
        "message_uuid": str(message_uuid),
        "user_uuid": str(owner_uuid),
        "read": True,
    }
    flag = _put(api, "message_flags", provider_flag_uuid, flag_data)
    assert flag.status_code == 200, flag.text
    assert (
        db.execute(
            """
        SELECT uuid FROM workspace_v3.message_flags
        WHERE project_id = %s AND message_uuid = %s AND user_uuid = %s
        """,
            (api.project_id, message_uuid, owner_uuid),
        ).fetchone()[0]
        == provider_flag_uuid
    )
    assert (
        db.execute(
            """
        SELECT count(*) FROM workspace_v3.projection_tasks
        WHERE project_id = %s AND status = 'pending'
          AND task_type = 'read_counters'
          AND user_uuid = %s AND scope_uuid IN (%s, %s)
        """,
            (api.project_id, owner_uuid, stream_uuids[1], topic_uuids[1]),
        ).fetchone()[0]
        == 2
    )
    db.execute(
        """
        UPDATE workspace_v3.provider_consumers
        SET updated_at = clock_timestamp() - interval '10 minutes'
        WHERE project_id = %s AND uuid = %s
        """,
        (api.project_id, provider_uuid),
    )
    _drain_projections(db)
    assert (
        db.execute(
            """
        SELECT unread_count FROM workspace_v3.stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
        """,
            (api.project_id, stream_uuids[1], owner_uuid),
        ).fetchone()[0]
        == 0
    )


def test_provider_topic_move_notifies_old_only_reader(api, db):
    _register_provider(api, db)
    owner_uuid = api.user_uuid
    reader_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    stream_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())
    topic_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    setup = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        (
            "users",
            reader_uuids[0],
            _user_data(reader_uuids[0], display_name="Old stream reader"),
        ),
        (
            "users",
            reader_uuids[1],
            _user_data(reader_uuids[1], display_name="New stream reader"),
        ),
    ]
    for number, stream_uuid in enumerate(stream_uuids):
        setup.extend(
            [
                (
                    "streams",
                    stream_uuid,
                    {
                        "name": f"Topic move target {number}",
                        "owner_uuid": str(owner_uuid),
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
                    "stream_bindings",
                    sys_uuid.uuid4(),
                    {
                        "stream_uuid": str(stream_uuid),
                        "user_uuid": str(reader_uuids[number]),
                        "role": "member",
                        "created_at": created_at,
                    },
                ),
            ]
        )
    topic = {
        "stream_uuid": str(stream_uuids[0]),
        "name": "Moved topic",
        "created_at": created_at,
    }
    setup.append(("topics", topic_uuid, topic))
    response = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in setup
            ]
        },
    )
    assert response.status_code == 200, response.text
    move_after = db.execute(
        "SELECT COALESCE(max(epoch_version), 0) FROM workspace_v3.events "
        "WHERE project_id = %s",
        (api.project_id,),
    ).fetchone()[0]

    moved = _put(
        api,
        "topics",
        topic_uuid,
        {**topic, "stream_uuid": str(stream_uuids[1])},
    )
    assert moved.status_code == 200, moved.text
    move_events = db.execute(
        """
        SELECT audience.consumer_uuid, event.payload ->> 'kind',
               event.payload ->> 'stream_uuid'
        FROM workspace_v3.events AS event
        JOIN workspace_v3.event_audience_members AS audience
          ON audience.project_id = event.project_id
         AND audience.audience_snapshot_uuid = event.audience_snapshot_uuid
        WHERE event.project_id = %s AND event.entity_uuid = %s
          AND event.epoch_version > %s AND audience.consumer_type = 'user'
        ORDER BY audience.consumer_uuid, event.epoch_version
        """,
        (api.project_id, topic_uuid, move_after),
    ).fetchall()
    event_by_reader = {
        recipient: (kind, stream_uuid)
        for recipient, kind, stream_uuid in move_events
        if recipient in reader_uuids
    }
    assert event_by_reader[reader_uuids[0]] == (
        "topic.deleted",
        str(stream_uuids[0]),
    )


def test_workspace_mutation_refreshes_provider_state_and_cursor(api, db):
    _register_provider(api, db)
    owner_uuid = api.user_uuid
    stream_uuid = sys_uuid.uuid4()
    binding_uuid = sys_uuid.uuid4()
    created_at = "2026-09-19T08:00:00Z"
    entities = [
        ("users", owner_uuid, _user_data(owner_uuid, display_name="Owner")),
        (
            "streams",
            stream_uuid,
            {
                "name": "Provider notifications",
                "owner_uuid": str(owner_uuid),
                "created_at": created_at,
            },
        ),
        (
            "stream_bindings",
            binding_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "user_uuid": str(owner_uuid),
                "role": "owner",
                "notification_mode": "all_messages",
                "created_at": created_at,
            },
        ),
    ]
    imported = api.post(
        f"{ROOT}/actions/apply/invoke",
        json={
            "operations": [
                {
                    "action": "upsert",
                    "type": resource,
                    "uuid": str(entity_uuid),
                    "content_hash": _hash(data),
                    "data": data,
                }
                for resource, entity_uuid, data in entities
            ]
        },
    )
    assert imported.status_code == 200, imported.text
    before = api.get(f"{ROOT}/stream_bindings/{binding_uuid}").json()

    changed = api.put(
        f"/v1/stream_bindings/{binding_uuid}",
        json={"notification_mode": "muted"},
    )
    assert changed.status_code == 200, changed.text

    after = api.get(f"{ROOT}/stream_bindings/{binding_uuid}").json()
    assert after["data"]["notification_mode"] == "muted"
    assert after["content_hash"] == _hash(after["data"])
    assert after["content_hash"] != before["content_hash"]
    assert after["source_updated_at"] == before["source_updated_at"]
    assert after["updated_at"] > before["updated_at"]

    changes = api.get(
        f"{ROOT}/stream_bindings",
        params={
            "updated_after": before["updated_at"],
            "after_uuid": str(sys_uuid.UUID(int=0)),
        },
    )
    assert changes.status_code == 200, changes.text
    assert binding_uuid in {
        sys_uuid.UUID(item["uuid"]) for item in changes.json()["items"]
    }
