# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import uuid

import psycopg
from restalchemy.common import contexts

from workspace.services.messenger_workers import agents
from workspace.services.messenger_workers import retention
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


def _maintenance(monkeypatch):
    worker = agents.MessengerWorkerAgent()
    monkeypatch.setattr(worker, "_summarize_one_topic", lambda: None)
    worker._iteration()


def test_worker_retires_completed_outbox_without_deleting_messages(
    api, db, monkeypatch
):
    stream = api.post(
        "/v1/messenger/streams/", json={"name": "Retained messages"}
    ).json()
    message = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "Keep this message"},
        },
    ).json()
    db.execute(
        "UPDATE messenger_projection_tasks SET status='completed',updated_at=now()-interval '2 hours'"
    )
    db.execute(
        "UPDATE messenger_domain_outbox_events SET created_at=now()-interval '2 hours'"
    )
    assert (
        db.execute("SELECT count(*) FROM messenger_domain_outbox_events").fetchone()[0]
        > 0
    )
    _maintenance(monkeypatch)
    assert db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events"
    ).fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM messenger_projection_tasks").fetchone() == (
        0,
    )
    assert (
        api.get("/v1/messenger/messages/" + message["uuid"]).json()["payload"]
        == message["payload"]
    )


def test_worker_retires_successful_inbox_but_keeps_retry_and_failed_rows(
    incoming, monkeypatch
):
    _, db, account, *_ = incoming
    for number, state in enumerate(["old", "recent", "pending", "failed"]):
        db.execute(
            "INSERT INTO zulip_incoming_events(project_id,external_account_uuid,provider_uuid,"
            "owner_user_uuid,account_identity,queue_id,event_id,payload,ordering_keys,processed_at,error,received_at) "
            "VALUES (%s,%s,%s,%s,'7',%s,%s,'{\"type\":\"message\"}',ARRAY[]::text[],%s,%s,%s)",
            (
                account["project_id"],
                account["uuid"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                "expired-queue",
                number,
                None
                if state == "pending"
                else datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=2 if state != "recent" else 0),
                "needs operator attention" if state == "failed" else None,
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=2),
            ),
        )
    _maintenance(monkeypatch)
    assert db.execute(
        "SELECT event_id FROM zulip_incoming_events ORDER BY event_id"
    ).fetchall() == [(1,), (2,), (3,)]


def _outbox(db, status="completed", age_hours=2, project=None):
    project = project or uuid.uuid4()
    identifier = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_domain_outbox_events(uuid,project_id,event_kind,scope_kind,scope_key,payload,created_at) "
        "VALUES (%s,%s,'content_mentions','message',%s,'{}',now()-interval '3 hours')",
        (identifier, project, str(identifier)),
    )
    db.execute(
        "UPDATE messenger_projection_tasks SET status=%s,updated_at=now()-%s*interval '1 hour' WHERE outbox_event_uuid=%s",
        (status, age_hours, identifier),
    )
    return identifier


def _prune(category, **options):
    with contexts.Context().session_manager() as session:
        return retention.prune(
            session, category, datetime.datetime.now(datetime.timezone.utc), **options
        )


def test_collection_is_bounded_and_preserves_pending_errors_and_recent_completions(db):
    old = [_outbox(db), _outbox(db)]
    keep = [
        _outbox(db, "pending"),
        _outbox(db, "failed"),
        _outbox(db, "dead_letter"),
        _outbox(db, age_hours=0),
    ]
    assert _prune("outbox", batch_size=1) == 1
    assert db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE uuid=ANY(%s)", (old,)
    ).fetchone() == (1,)
    assert _prune("outbox", batch_size=1) == 1
    assert _prune("outbox", batch_size=1) == 0
    assert {
        row[0] for row in db.execute("SELECT uuid FROM messenger_domain_outbox_events")
    } == set(keep)


def test_configurable_retention_and_locked_outbox_roots(db, _test_database_url):
    old = _outbox(db, age_hours=1.5)
    assert _prune("outbox", retention_seconds=7200) == 0
    with psycopg.connect(_test_database_url) as other:
        other.execute(
            "SELECT 1 FROM messenger_domain_outbox_events WHERE uuid=%s FOR UPDATE",
            (old,),
        )
        assert _prune("outbox") == 0
    assert _prune("outbox") == 1


def test_fanout_descendants_finish_before_root_collection(api, db):
    stream = api.post("/v1/messenger/streams/", json={"name": "Fanout"}).json()
    message = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "Keep"},
        },
    ).json()
    event = _outbox(db, project=uuid.UUID(api.project_id))
    root = uuid.uuid4()
    db.execute(
        "INSERT INTO messenger_fanout_roots(uuid,project_id,outbox_event_uuid,placement_uuid,status,created_at,updated_at) VALUES (%s,%s,%s,%s,'pending',now()-interval '2 hours',now()-interval '2 hours')",
        (root, api.project_id, event, message["uuid"]),
    )
    _prune("outbox")
    assert db.execute(
        "SELECT 1 FROM messenger_domain_outbox_events WHERE uuid=%s", (event,)
    ).fetchone() == (1,)
    db.execute(
        "UPDATE messenger_fanout_roots SET status='completed' WHERE uuid=%s", (root,)
    )
    _prune("outbox")
    assert (
        db.execute(
            "SELECT 1 FROM messenger_fanout_roots WHERE uuid=%s", (root,)
        ).fetchone()
        is None
    )


def _page(db, text, old=True):
    return db.execute(
        "INSERT INTO messenger_message_contents(workspace_content,created_at) VALUES (jsonb_build_object('kind','markdown','content',%s::text),now()-%s*interval '1 hour') RETURNING uuid",
        (text, 2 if old else 0),
    ).fetchone()[0]


def test_unused_contents_collection_preserves_every_edit_revision(api, db):
    stream = api.post("/v1/messenger/streams/", json={"name": "Revisions"}).json()
    message = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "topic_uuid": stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "Initial"},
        },
    ).json()
    previous = _page(db, "Old referenced version")
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (message["uuid"],),
    ).fetchone()[0]
    db.execute(
        "UPDATE messenger_messages SET content_uuid=%s WHERE uuid=%s",
        (previous, canonical),
    )
    assert (
        api.put(
            "/v1/messenger/messages/" + message["uuid"],
            json={"payload": {"kind": "markdown", "content": "Current"}},
        ).status_code
        == 200
    )
    orphan = _page(db, "Unreferenced page")
    fresh = _page(db, "New unreferenced page", old=False)
    assert _prune("contents") == 1
    assert (
        db.execute(
            "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (orphan,)
        ).fetchone()
        is None
    )
    assert db.execute(
        "SELECT count(*) FROM messenger_message_contents WHERE uuid=ANY(%s)",
        ([previous, fresh],),
    ).fetchone() == (2,)
    assert (
        api.get("/v1/messenger/messages/" + message["uuid"]).json()["payload"][
            "content"
        ]
        == "Current"
    )


def test_outgoing_completion_history_is_retired_without_touching_unsent_work(incoming):
    _, db, account, *_ = incoming
    identifiers = []
    for state, old in [
        ("done", True),
        ("done", False),
        ("new", True),
        ("checking", True),
        ("error", True),
    ]:
        identifier = db.execute(
            "INSERT INTO zulip_outgoing_intents(project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data,status,created_at,completed_at) VALUES (%s,'message',%s,%s,%s,'insert','{}',%s,now()-interval '2 hours',CASE WHEN %s='done' THEN now()-%s*interval '1 hour' ELSE NULL END) RETURNING id",
            (
                account["project_id"],
                uuid.uuid4(),
                str(uuid.uuid4()),
                account["uuid"],
                state,
                state,
                2 if old else 0,
            ),
        ).fetchone()[0]
        identifiers.append(identifier)
    orphan = _page(db, "In-flight conversion")
    assert _prune("contents") == 0
    assert _prune("outgoing") == 1
    assert [
        row[0]
        for row in db.execute("SELECT id FROM zulip_outgoing_intents ORDER BY id")
    ] == identifiers[1:]
    assert db.execute(
        "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (orphan,)
    ).fetchone() == (1,)


def test_content_fk_pins_make_collection_skip_an_inflight_consumer(
    db, _test_database_url
):
    content = _page(db, "Pinned by a consumer")
    with psycopg.connect(_test_database_url) as consumer:
        consumer.execute(
            "SELECT 1 FROM messenger_message_contents WHERE uuid=%s FOR KEY SHARE",
            (content,),
        )
        _prune("contents")
        assert db.execute(
            "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (content,)
        ).fetchone() == (1,)
    _prune("contents")
    assert (
        db.execute(
            "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (content,)
        ).fetchone()
        is None
    )


def test_successful_current_queue_rows_are_kept_until_cursor_advances(incoming):
    _, db, account, *_ = incoming
    db.execute(
        "INSERT INTO zulip_incoming_subscriptions(external_account_uuid,project_id,provider_uuid,owner_user_uuid,account_identity,server_url,queue_id,last_event_id,longpoll_timeout) VALUES (%s,%s,%s,%s,'7','https://zulip.example.com','current',5,90)",
        (
            account["uuid"],
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
        ),
    )
    for event_id in [5, 6]:
        db.execute(
            "INSERT INTO zulip_incoming_events(project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,queue_id,event_id,payload,ordering_keys,processed_at) VALUES (%s,%s,%s,%s,'7','current',%s,'{}',ARRAY[]::text[],now()-interval '2 hours')",
            (
                account["project_id"],
                account["uuid"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                event_id,
            ),
        )
    assert _prune("incoming") == 1
    assert db.execute("SELECT event_id FROM zulip_incoming_events").fetchall() == [(6,)]
    assert db.execute(
        "SELECT last_event_id FROM zulip_incoming_subscriptions"
    ).fetchone() == (5,)
    db.execute("UPDATE zulip_incoming_subscriptions SET last_event_id=6")
    assert _prune("incoming") == 1


def test_conversion_cache_expiry_does_not_remove_used_content(incoming):
    _, db, account, *_ = incoming
    content = _page(db, "Cached conversion")
    cached = uuid.uuid4()
    db.execute(
        "INSERT INTO bridge_zulip_v5_content_snapshots(uuid,provider_uuid,direction,context_hash,content_uuid,created_at) VALUES (%s,%s,'incoming','context',%s,now()-interval '2 hours')",
        (cached, account["provider_uuid"], content),
    )
    _prune("contents")
    assert db.execute(
        "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (content,)
    ).fetchone() == (1,)
    assert _prune("conversion_cache") == 1
    _prune("contents")
    assert (
        db.execute(
            "SELECT 1 FROM messenger_message_contents WHERE uuid=%s", (content,)
        ).fetchone()
        is None
    )
