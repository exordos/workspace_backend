# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import threading
import uuid as sys_uuid

import psycopg
import pytest
from restalchemy.common import contexts

from workspace.tests.integration import legacy_store
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.messenger_api.api import v2_store
from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import message_payloads
from workspace.tests.integration import conftest
from workspace.tests.integration import test_messenger_v2


STREAMS = "/v1/messenger/streams/"
MESSAGES = "/v1/messenger/messages/"


@pytest.fixture(autouse=True)
def _production_v2_store(_database):
    api_store.configure_store_factory(store_factory.build_store_factory())
    try:
        yield
    finally:
        api_store.configure_store_factory(legacy_store.LegacyMessengerStoreFactory())


def _use_store(kind):
    factory = (
        store_factory.build_store_factory()
        if kind == "v2"
        else legacy_store.LegacyMessengerStoreFactory()
    )
    api_store.configure_store_factory(factory)


def _create_stream(api, encryption=False):
    values = {"name": "Key announcements", "encryption": encryption}
    if encryption:
        values["current_encryption_key_uuid"] = str(sys_uuid.uuid4())
    response = api.post(STREAMS, json=values)
    assert response.status_code == 201, response.text
    return response.json()


def _add_member(api, db, stream_uuid):
    member_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, member_uuid, f"key-member-{member_uuid}")
    test_messenger_v2._register_project_user(db, api.project_id, member_uuid)
    response = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(member_uuid)]},
    )
    assert response.status_code == 200, response.text
    return member_uuid


def _announcement(stream_uuid, key_uuid, **values):
    return {
        "stream_uuid": stream_uuid,
        "payload": {"kind": "key_announced", "key_uuid": str(key_uuid)},
        **values,
    }


@pytest.mark.parametrize("store_kind", ["v2"])
@pytest.mark.parametrize("encryption", [False, True])
def test_owner_announcement_updates_stream_history_and_events(
    api, workspace_api, db, store_kind, encryption
):
    stream = _create_stream(api, encryption)
    member_uuid = _add_member(api, db, stream["uuid"])
    test_messenger_v2._drain()
    key_uuid = sys_uuid.uuid4()
    values = _announcement(stream["uuid"], key_uuid, uuid=str(sys_uuid.uuid4()))
    _use_store(store_kind)
    response = api.post(MESSAGES, json=values)

    assert response.status_code == 201, response.text
    assert response.json()["payload"] == values["payload"]
    assert response.json()["topic_uuid"] == stream["default_topic_uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT encryption, current_encryption_key_uuid FROM messenger_streams "
            "WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream["uuid"]),
        )
        assert cursor.fetchone() == (encryption, key_uuid)
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_messages WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone() == (1,)
    test_messenger_v2._drain()
    workspace_api.project_id = api.project_id
    for user_uuid in (api.user_uuid, member_uuid):
        read = api.get(f"{STREAMS}{stream['uuid']}", user=user_uuid)
        assert read.status_code == 200, read.text
        assert read.json()["encryption"] is encryption
        assert read.json()["current_encryption_key_uuid"] == str(key_uuid)
        history = api.get(
            MESSAGES, user=user_uuid, params={"stream_uuid": stream["uuid"]}
        )
        assert history.status_code == 200, history.text
        assert [row["payload"] for row in history.json()] == [values["payload"]]
        workspace_api.user_uuid = str(user_uuid)
        response = workspace_api.get("/v1/events/")
        assert response.status_code == 200, response.text
        payloads = [event["payload"] for event in response.json()]
        assert any(
            event["kind"] == "message.created"
            and event["stream_uuid"] == stream["uuid"]
            and event["payload"] == values["payload"]
            for event in payloads
        )
        updated = [
            event
            for event in payloads
            if event["kind"] == "stream.updated" and event["uuid"] == stream["uuid"]
        ]
        assert updated[-1]["current_encryption_key_uuid"] == str(key_uuid)
        assert updated[-1]["encryption"] is encryption


@pytest.mark.parametrize("store_kind", ["v2"])
@pytest.mark.parametrize("role", ["member", "moderator", "administrator"])
def test_non_owner_announcement_is_rejected_without_side_effects(
    api, db, store_kind, role
):
    stream = _create_stream(api, True)
    member_uuid = _add_member(api, db, stream["uuid"])
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE messenger_stream_bindings SET role = %s "
            "WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
            (role, api.project_id, stream["uuid"], member_uuid),
        )
    test_messenger_v2._drain()
    _use_store(store_kind)
    response = api.post(
        MESSAGES, user=member_uuid, json=_announcement(stream["uuid"], sys_uuid.uuid4())
    )

    assert response.status_code == 400, response.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert (
        read.json()["current_encryption_key_uuid"]
        == stream["current_encryption_key_uuid"]
    )
    with db.cursor() as cursor:
        for table in ("messenger_messages", "messenger_message_placements"):
            cursor.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (api.project_id,)
            )
            assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("store_kind", ["v2"])
def test_replayed_announcement_cannot_restore_an_old_key(api, db, store_kind):
    stream = _create_stream(api)
    first_values = _announcement(
        stream["uuid"], sys_uuid.uuid4(), uuid=str(sys_uuid.uuid4())
    )
    second_key_uuid = sys_uuid.uuid4()
    _use_store(store_kind)
    first = api.post(MESSAGES, json=first_values)
    second = api.post(MESSAGES, json=_announcement(stream["uuid"], second_key_uuid))
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    repeated = api.post(MESSAGES, json=first_values)

    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["uuid"] == first.json()["uuid"]
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["current_encryption_key_uuid"] == str(second_key_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_messages WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone() == (2,)


def test_an_old_announcement_cannot_be_republished_in_another_topic(api, db):
    stream = _create_stream(api)
    first_values = _announcement(
        stream["uuid"], sys_uuid.uuid4(), uuid=str(sys_uuid.uuid4())
    )
    first = api.post(MESSAGES, json=first_values)
    assert first.status_code == 201, first.text
    second_key_uuid = sys_uuid.uuid4()
    second = api.post(MESSAGES, json=_announcement(stream["uuid"], second_key_uuid))
    assert second.status_code == 201, second.text
    topic = api.post(
        "/v1/messenger/stream_topics/",
        json={"name": "Other topic", "stream_uuid": stream["uuid"]},
    )
    assert topic.status_code == 201, topic.text
    repeated = api.post(
        MESSAGES, json={**first_values, "topic_uuid": topic.json()["uuid"]}
    )

    assert repeated.status_code == 400, repeated.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["current_encryption_key_uuid"] == str(second_key_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_message_placements WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone() == (2,)


def test_an_announcement_can_set_the_key_once_in_each_stream(api):
    streams = [_create_stream(api), _create_stream(api)]
    key_uuid = sys_uuid.uuid4()
    canonical_uuid = str(sys_uuid.uuid4())
    for stream in streams:
        response = api.post(
            MESSAGES, json=_announcement(stream["uuid"], key_uuid, uuid=canonical_uuid)
        )
        assert response.status_code == 201, response.text
        read = api.get(f"{STREAMS}{stream['uuid']}")
        assert read.status_code == 200, read.text
        assert read.json()["current_encryption_key_uuid"] == str(key_uuid)


def test_another_owner_can_announce_a_key_in_the_same_stream(api, db):
    stream = _create_stream(api, True)
    owner_uuid = _add_member(api, db, stream["uuid"])
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE messenger_stream_bindings SET role = 'owner' "
            "WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
            (api.project_id, stream["uuid"], owner_uuid),
        )
    key_uuid = sys_uuid.uuid4()
    response = api.post(
        MESSAGES, user=owner_uuid, json=_announcement(stream["uuid"], key_uuid)
    )

    assert response.status_code == 201, response.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["current_encryption_key_uuid"] == str(key_uuid)


@pytest.mark.parametrize("store_kind", ["v2"])
def test_failure_after_updating_the_key_rolls_back_the_whole_announcement(
    api, db, monkeypatch, store_kind
):
    stream = _create_stream(api, True)
    test_messenger_v2._drain()
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_domain_outbox_events WHERE project_id = %s",
            (api.project_id,),
        )
        outbox_count = cursor.fetchone()[0]

    def fail(*args, **kwargs):
        raise RuntimeError("Injected failure after updating the stream key")

    if store_kind == "v2":
        monkeypatch.setattr(v2_store.MessengerV2Store, "_enqueue_stream_snapshot", fail)
    else:
        monkeypatch.setattr(helpers, "_create_workspace_stream_updated_events", fail)
    _use_store(store_kind)
    response = api.post(MESSAGES, json=_announcement(stream["uuid"], sys_uuid.uuid4()))

    assert response.status_code == 500, response.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert (
        read.json()["current_encryption_key_uuid"]
        == stream["current_encryption_key_uuid"]
    )
    with db.cursor() as cursor:
        for table in (
            "messenger_messages",
            "messenger_message_placements",
        ):
            cursor.execute(
                f"SELECT COUNT(*) FROM {table} WHERE project_id = %s", (api.project_id,)
            )
            assert cursor.fetchone() == (0,)
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_domain_outbox_events WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == outbox_count


@pytest.mark.parametrize(
    "original_kind,new_kind",
    [
        ("markdown", "key_announced"),
        ("key_announced", "markdown"),
        ("key_announced", "key_announced"),
    ],
)
def test_put_cannot_create_or_change_a_key_announcement(api, original_kind, new_kind):
    stream = _create_stream(api)
    original = (
        {"kind": "markdown", "content": "Original"}
        if original_kind == "markdown"
        else {"kind": "key_announced", "key_uuid": str(sys_uuid.uuid4())}
    )
    created = api.post(
        MESSAGES, json={"stream_uuid": stream["uuid"], "payload": original}
    )
    assert created.status_code == 201, created.text
    replacement = (
        {"kind": "markdown", "content": "Replacement"}
        if new_kind == "markdown"
        else {"kind": "key_announced", "key_uuid": str(sys_uuid.uuid4())}
    )
    response = api.put(
        f"{MESSAGES}{created.json()['uuid']}", json={"payload": replacement}
    )

    assert response.status_code == 400, response.text
    read = api.get(f"{MESSAGES}{created.json()['uuid']}")
    assert read.status_code == 200, read.text
    assert read.json()["payload"] == original


def test_key_announcement_uses_the_committed_owner_role(api, db):
    stream = _create_stream(api, True)
    connection = psycopg.connect(conftest.TEST_DB_URL)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE messenger_stream_bindings SET role = 'member' "
                    "WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s",
                    (api.project_id, stream["uuid"], api.user_uuid),
                )
            future = executor.submit(
                api.post, MESSAGES, json=_announcement(stream["uuid"], sys_uuid.uuid4())
            )
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=0.1)
            connection.commit()
            response = future.result(timeout=10)
        finally:
            connection.rollback()
            connection.close()

    assert response.status_code == 400, response.text
    read = api.get(f"{STREAMS}{stream['uuid']}")
    assert read.status_code == 200, read.text
    assert (
        read.json()["current_encryption_key_uuid"]
        == stream["current_encryption_key_uuid"]
    )


def test_concurrent_announcements_commit_in_stream_lock_order(api, db):
    stream = _create_stream(api, True)
    keys = [sys_uuid.uuid4(), sys_uuid.uuid4()]
    first_stored = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def send(index):
        with contexts.Context().session_manager():
            session = contexts.Context().get_session()
            session.execute("SET LOCAL lock_timeout = '5s'")
            store = v2_store.MessengerV2Store(
                sys_uuid.UUID(api.project_id), sys_uuid.UUID(api.user_uuid)
            )
            if index == 1:
                second_started.set()
            row = store.create_message(
                {
                    "stream_uuid": stream["uuid"],
                    "payload": message_payloads.KeyAnnouncedPayload(
                        key_uuid=keys[index]
                    ),
                }
            )
            if index == 0:
                first_stored.set()
                assert release_first.wait(timeout=10)
            return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send, 0)
        try:
            assert first_stored.wait(timeout=10)
            second = executor.submit(send, 1)
            assert second_started.wait(timeout=10)
            with pytest.raises(concurrent.futures.TimeoutError):
                second.result(timeout=0.1)
            with db.cursor() as cursor:
                cursor.execute(
                    "SELECT current_encryption_key_uuid FROM messenger_streams "
                    "WHERE project_id = %s AND uuid = %s",
                    (api.project_id, stream["uuid"]),
                )
                assert (
                    str(cursor.fetchone()[0]) == stream["current_encryption_key_uuid"]
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM messenger_messages WHERE project_id = %s",
                    (api.project_id,),
                )
                assert cursor.fetchone() == (0,)
        finally:
            release_first.set()
        assert first.result(timeout=10)["payload"]["key_uuid"] == str(keys[0])
        assert second.result(timeout=10)["payload"]["key_uuid"] == str(keys[1])
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT current_encryption_key_uuid FROM messenger_streams "
            "WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream["uuid"]),
        )
        assert cursor.fetchone() == (keys[1],)
        cursor.execute(
            "SELECT COUNT(*) FROM messenger_messages WHERE project_id = %s",
            (api.project_id,),
        )
        assert cursor.fetchone() == (2,)
