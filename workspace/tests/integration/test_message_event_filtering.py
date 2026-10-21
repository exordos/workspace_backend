# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
import uuid

import pytest
from restalchemy.common import contexts

from workspace.bridges.zulip.dm import flags
from workspace.bridges.zulip.dm import models
from workspace.bridges.zulip.dm import reactions
from workspace.messenger_api.dm import v2_models
from workspace.tests.integration import test_messenger_v2 as messenger_tests
from workspace.tests.integration import test_zulip_flags as flag_tests
from workspace.tests.integration import test_zulip_message_states as source_tests
from workspace.tests.integration import test_zulip_reactions as reaction_tests


message_sources = source_tests.message_sources


def message_events(api, account):
    response = api.get(
        "/v1/events/",
        user=account.owner_user_uuid,
        project=account.project_id,
        params={"page_limit": 500},
    )
    assert response.status_code == 200, response.text
    return [
        event["payload"]
        for event in response.json()
        if event["payload"]["kind"].startswith(("message.", "message_reaction."))
    ]


@pytest.mark.parametrize("age", (13, 15))
def test_imported_message_events_respect_creation_age_and_keep_counters(
    message_sources, api, age
):
    now = datetime.datetime.now(datetime.timezone.utc)
    account, source = message_sources(
        timestamp=int((now - datetime.timedelta(days=age)).timestamp())
    )
    source.read = source.mentioned = False
    source.update()
    source_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    source_tests._refresh_counters()
    expected = ["message.created"] if age == 13 else []
    assert [event["kind"] for event in message_events(api, account)] == expected
    for model in (
        v2_models.WorkspaceStreamBinding,
        v2_models.WorkspaceUserTopicBinding,
    ):
        assert source_tests._viewer_rows(account, model)[0].unread_count == 1

    source.content.workspace_content = "A real edit"
    source.content.workspace_content_hash = hashlib.sha256(b"A real edit").hexdigest()
    source.content.workspace_content_updated_at = now
    source.content.update()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    if age == 13:
        expected.append("message.updated")
    assert [event["kind"] for event in message_events(api, account)] == expected

    # A later observation/edit timestamp with identical content is silent.
    source.content.workspace_content_updated_at += datetime.timedelta(seconds=1)
    source.content.update()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    assert [event["kind"] for event in message_events(api, account)] == expected

    now = datetime.datetime.now(datetime.timezone.utc)
    with contexts.Context().session_manager():
        for flag, value in (("read", True), ("starred", False)):
            flags.record(account, [source.message_id], flag, value, now)
    flag_tests.reconcile()
    messenger_tests._drain()
    if age == 13:
        expected.append("message.updated")
    assert [event["kind"] for event in message_events(api, account)] == expected
    for model in (
        v2_models.WorkspaceStreamBinding,
        v2_models.WorkspaceUserTopicBinding,
    ):
        assert source_tests._viewer_rows(account, model)[0].unread_count == 0

    with contexts.Context().session_manager():
        for flag, value in (("read", True), ("starred", False)):
            flags.record(
                account,
                [source.message_id],
                flag,
                value,
                now + datetime.timedelta(seconds=2),
            )
    flag_tests.reconcile()
    messenger_tests._drain()
    assert [event["kind"] for event in message_events(api, account)] == expected

    for items, suffix in (
        ([reaction_tests.reaction()], ["message_reaction.created", "message.updated"]),
        ([], ["message_reaction.deleted", "message.updated"]),
    ):
        with contexts.Context().session_manager():
            reactions.observe_snapshot(
                account,
                [{"id": source.message_id, "reactions": items}],
                datetime.datetime.now(datetime.timezone.utc),
            )
        reaction_tests.reconcile()
        messenger_tests._drain()
        if age == 13:
            expected.extend(suffix)
        assert [event["kind"] for event in message_events(api, account)] == expected
        message = source_tests._messages(account)[0]
        assert message.reactions == ({"thumbs_up": 1} if items else {})
        with contexts.Context().session_manager():
            reactions.observe_snapshot(
                account,
                [{"id": source.message_id, "reactions": items}],
                datetime.datetime.now(datetime.timezone.utc),
            )
        reaction_tests.reconcile()
        messenger_tests._drain()
        assert [event["kind"] for event in message_events(api, account)] == expected

    source.delete()
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    expected.append("message.deleted")
    assert [event["kind"] for event in message_events(api, account)] == expected


def test_redundant_reaction_projection_is_silent(message_sources, api, db):
    account, source = message_sources()
    source_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    messenger_tests._drain()
    before = message_events(api, account)
    placement = source_tests._placements(account)[0]
    db.execute(
        """INSERT INTO messenger_domain_outbox_events
            (uuid, project_id, event_kind, scope_kind, scope_key, payload)
        VALUES (%s, %s, 'reaction_snapshot', 'message', %s, %s::jsonb)""",
        (
            uuid.uuid4(),
            account.project_id,
            f"{account.project_id}:{source_tests._messages(account)[0].uuid}",
            json.dumps(
                {
                    "source_kind": "message_reaction.updated_old",
                    "placement_uuid": str(placement.uuid),
                    "emit_reaction_event": False,
                }
            ),
        ),
    )
    messenger_tests._drain()
    assert message_events(api, account) == before


@pytest.mark.parametrize("age", (13, 15))
@pytest.mark.parametrize("action", ("read", "read_up_to"))
def test_read_events_respect_message_age_and_idempotence(
    message_sources, api, age, action
):
    account, source = message_sources(
        timestamp=int(
            (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=age)
            ).timestamp()
        )
    )
    source.read = source.mentioned = False
    source.update()
    source_tests._prepare(account)
    with contexts.Context().session_manager():
        models.ZulipMessageState.reconcile_batch(100)
    source_tests._refresh_counters()
    placement = source_tests._placements(account)[0]
    for _index in range(2):
        response = api.post(
            f"/v1/messenger/messages/{placement.uuid}/actions/{action}/invoke",
            user=account.owner_user_uuid,
            project=account.project_id,
        )
        assert response.status_code == 200, response.text
        messenger_tests._drain()
        expected = ["message.created", "message.read"] if age == 13 else []
        assert [event["kind"] for event in message_events(api, account)] == expected
        for model in (
            v2_models.WorkspaceStreamBinding,
            v2_models.WorkspaceUserTopicBinding,
        ):
            assert source_tests._viewer_rows(account, model)[0].unread_count == 0


def test_native_noop_message_flags_and_reaction_do_not_enqueue_events(api, db):
    response = api.post("/v1/messenger/streams/", json={"name": "Silent retries"})
    assert response.status_code == 201, response.text
    stream = response.json()
    payload = {"kind": "markdown", "content": "Unchanged"}
    response = api.post(
        "/v1/messenger/messages/",
        json={"stream_uuid": stream["uuid"], "payload": payload},
    )
    assert response.status_code == 201, response.text
    message_url = f"/v1/messenger/messages/{response.json()['uuid']}"
    assert api.post(f"{message_url}/actions/star/invoke").status_code == 200
    response = api.post(
        "/v1/messenger/message_reactions/",
        json={"message_uuid": response.json()["uuid"], "emoji_name": "thumbs_up"},
    )
    assert response.status_code == 201, response.text
    reaction_url = f"/v1/messenger/message_reactions/{response.json()['uuid']}"
    messenger_tests._drain()
    before = api.get("/v1/events/", params={"page_limit": 500}).json()
    message_before = api.get(message_url).json()
    reaction_before = api.get(reaction_url).json()
    outbox_before = db.execute(
        "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
        (api.project_id,),
    ).fetchone()
    for _index in range(2):
        assert api.put(message_url, json={"payload": payload}).status_code == 200
        assert api.post(f"{message_url}/actions/star/invoke").status_code == 200
        assert api.put(reaction_url, json={"emoji_name": "+1"}).status_code == 200
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE project_id=%s",
            (api.project_id,),
        ).fetchone()
        == outbox_before
    )
    messenger_tests._drain()
    assert api.get("/v1/events/", params={"page_limit": 500}).json() == before
    assert api.get(message_url).json() == message_before
    assert api.get(reaction_url).json() == reaction_before
    assert api.post(f"{message_url}/actions/read/invoke").status_code == 200
    messenger_tests._drain()
    after = api.get("/v1/events/", params={"page_limit": 500}).json()
    assert [
        event for event in after if event["payload"]["kind"].startswith("message.")
    ] == [event for event in before if event["payload"]["kind"].startswith("message.")]


@pytest.mark.parametrize("age", (13, 15))
def test_native_message_publication_uses_creation_time(api, db, age):
    response = api.post("/v1/messenger/streams/", json={"name": "Delayed fanout"})
    assert response.status_code == 201, response.text
    stream = response.json()
    response = api.post(
        "/v1/messenger/messages/",
        json={
            "stream_uuid": stream["uuid"],
            "payload": {"kind": "markdown", "content": "Original"},
        },
    )
    assert response.status_code == 201, response.text
    message = response.json()
    db.execute(
        """UPDATE messenger_messages SET created_at=%s
        WHERE project_id=%s AND uuid=(
            SELECT message_uuid FROM messenger_message_placements
            WHERE project_id=%s AND uuid=%s)""",
        (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=age),
            api.project_id,
            api.project_id,
            message["uuid"],
        ),
    )
    messenger_tests._drain()
    message_url = f"/v1/messenger/messages/{message['uuid']}"
    assert (
        api.put(
            message_url, json={"payload": {"kind": "markdown", "content": "Edited"}}
        ).status_code
        == 200
    )
    assert api.post(f"{message_url}/actions/star/invoke").status_code == 200
    assert (
        api.post(
            "/v1/messenger/message_reactions/",
            json={"message_uuid": message["uuid"], "emoji_name": "thumbs_up"},
        ).status_code
        == 201
    )
    messenger_tests._drain()
    response = api.get("/v1/events/", params={"page_limit": 500})
    assert response.status_code == 200, response.text
    kinds = [
        event["payload"]["kind"]
        for event in response.json()
        if event["payload"]["kind"].startswith(("message.", "message_reaction."))
    ]
    if age == 13:
        assert "message.created" in kinds
        assert "message.updated" in kinds
        assert "message_reaction.created" in kinds
    else:
        assert kinds == []
    updated = api.get(message_url).json()
    assert updated["payload"]["content"] == "Edited"
    assert updated["starred"] and updated["reactions"] == {"thumbs_up": 1}
