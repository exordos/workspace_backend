# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import copy
import json
import re
import uuid

import pytest

from aiohttp import web

from workspace.bridges.zulip import (
    incoming_operations,
    outgoing_operations,
    poll_conversion,
)
from workspace.services import zulip_incoming, zulip_outgoing
from workspace.tests.integration import test_zulip_incoming as inbound
from workspace.tests.integration import test_zulip_outgoing as outbound

incoming = inbound.incoming
outgoing = outbound.outgoing
MESSAGES = "/v1/messenger/messages/"


def payload(**changes):
    return {
        "kind": "poll",
        "questions": [
            {
                "uuid": str(uuid.uuid4()),
                "text": "Deployment?",
                "options": [
                    {"uuid": str(uuid.uuid4()), "text": label}
                    for label in ("Monday", "Tuesday")
                ],
            }
        ],
        "is_anonymous": False,
        "multiple_answers": True,
        "allow_revote": True,
        "allow_adding_options": True,
        "results_visibility": "always",
        **changes,
    }


class PollServer(outbound.Server):
    lose_submessage_once = False
    deny_submessages = False

    async def handle(self, http):
        if http.path == "/api/v1/messages" and http.method == "POST":
            form = await http.post()
            if (
                "widget_content" in form
                and json.loads(form["widget_content"])["widget_type"] == "poll"
            ):
                return web.json_response(
                    {"result": "error", "msg": "Widgets: unknown widget type: poll"},
                    status=400,
                )
        if http.path == "/api/v1/submessage":
            assert http.headers["Authorization"] == self.auth
            if self.deny_submessages:
                return web.json_response(
                    {"result": "error", "msg": "Permission denied"}, status=403
                )
            data = dict(await http.post())
            self.calls.append((http.method, http.path, data))
            remote = int(data["message_id"])
            self.messages[remote]["submessages"].append(
                {
                    "id": 1
                    + max(
                        (m["id"] for m in self.messages[remote]["submessages"]),
                        default=0,
                    ),
                    "sender_id": self.sender_id,
                    "msg_type": data["msg_type"],
                    "content": data["content"],
                }
            )
            if self.lose_submessage_once:
                self.lose_submessage_once = False
                http.transport.close()
                return web.Response()
            return web.json_response({"result": "success"})
        response = await super().handle(http)
        if http.path == "/api/v1/messages" and http.method == "POST":
            message = self.messages[self.next_id]
            if message["content"].startswith("/poll "):
                question, *options = (
                    message["content"].removeprefix("/poll ").splitlines()
                )
                sent = self.calls[-1][2]
                initialization = (
                    json.loads(sent["widget_content"])
                    if "widget_content" in sent
                    else {
                        "widget_type": "poll",
                        "extra_data": {
                            "question": question,
                            "options": [
                                re.sub(r"(\s*[-*]?\s*)", "", option.strip(), count=1)
                                for option in options
                            ],
                        },
                    }
                )
                message["submessages"] = [
                    {
                        "id": 1,
                        "sender_id": self.sender_id,
                        "msg_type": "widget",
                        "content": json.dumps(initialization),
                    }
                ]
        return response


async def import_observation(service, account, event):
    account_data = (
        await service.db.sql(
            zulip_outgoing.SELECT_ACCOUNT_SQL, (account.uuid, account.project_id)
        )
    )[0]
    operations = incoming_operations.Operations(service.db, service.http)
    prepared = await inbound.prepare_dependencies(
        operations, service.db, account_data, event
    )
    async with service.db.transaction() as connection:
        await connection.execute(zulip_incoming.INCOMING_TRANSACTION_SQL)
        await operations.apply(connection, account_data, prepared)


async def reconcile(service, account):
    pending = await service.db.sql(
        "SELECT id,payload FROM zulip_incoming_events WHERE external_account_uuid=%s AND payload->>'type'='poll_refresh' AND processed_at IS NULL ORDER BY id",
        (account.uuid,),
    )
    assert pending
    for row in pending:
        await import_observation(service, account, row["payload"])
        await service.db.sql(
            "UPDATE zulip_incoming_events SET processed_at=now() WHERE id=%s",
            (row["id"],),
        )


def test_compatible_poll_sends_votes_revokes_and_options_through_the_durable_worker(
    outgoing, db, _test_database_url, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setattr(outbound, "Server", PollServer)
    created = api.post(
        MESSAGES, json={"stream_uuid": str(channel.stream_uuid), "payload": payload()}
    )
    assert created.status_code == 201, created.text
    message = created.json()
    path = MESSAGES + message["uuid"]
    question = message["payload"]["questions"][0]

    async def run():
        async with outbound.sender(account, _test_database_url) as (service, server):
            await outbound.drain(service)
            assert (
                server.messages[101]["content"] == "/poll Deployment?\nMonday\nTuesday"
            )
            vote = {
                "question_uuid": question["uuid"],
                "option_uuid": question["options"][0]["uuid"],
                "selected": True,
            }
            response = api.post(path + "/actions/vote/invoke", json=vote)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "new"
            server.lose_submessage_once = True
            await outbound.drain(service)
            # A new operations object has no in-memory receipt state.
            service.operations = outgoing_operations.Operations(
                service.db, service.http
            )
            observed = poll_conversion.read(server.messages[101])
            assert observed.votes["canned,0"] == {7}
            assert sum(path == "/api/v1/submessage" for _, path, _ in server.calls) == 1
            response = api.post(
                path + "/actions/vote/invoke", json={**vote, "selected": False}
            )
            assert response.status_code == 200, response.text
            await outbound.drain(service)
            assert poll_conversion.read(server.messages[101]).votes["canned,0"] == set()
            option = {"uuid": str(uuid.uuid4()), "text": "Wednesday"}
            response = api.post(
                path + "/actions/add_poll_option/invoke",
                json={"question_uuid": question["uuid"], "option": option},
            )
            assert response.status_code == 200, response.text
            server.lose_submessage_once = True
            await outbound.drain(service)
            assert list(
                poll_conversion.read(server.messages[101]).options.values()
            ) == ["Monday", "Tuesday", "Wednesday"]
            assert sum(path == "/api/v1/submessage" for _, path, _ in server.calls) == 3
            await reconcile(service, account)
            recovered = api.get(path).json()
            assert (
                recovered["payload"]["kind"] == "poll"
                and recovered["poll_state"]["my_votes"] == []
            )
            assert recovered["payload"]["questions"][0]["uuid"] == question["uuid"]
            assert recovered["payload"]["questions"][0]["options"][-1] == option
            server.deny_submessages = True
            failed = api.post(path + "/actions/vote/invoke", json=vote)
            assert failed.status_code == 200
            await service.dispatch()
            await asyncio.gather(*list(service.tasks.values()))
            assert api.get(path).json()["status"] == "error"
            assert poll_conversion.read(server.messages[101]).votes["canned,0"] == set()

    asyncio.run(run())


def widget(sender, **content):
    return {"sender_id": sender, "msg_type": "widget", "content": json.dumps(content)}


def test_incoming_submessages_update_votes_and_preserve_pending_workspace_votes(
    incoming,
):
    api, db, _account, http, *_ = incoming
    event = inbound.event("/poll Deployment?\nMonday\nTuesday", 831)
    event["message"]["submessages"] = [
        widget(
            7,
            widget_type="poll",
            extra_data={"question": "Deployment?", "options": ["Monday", "Tuesday"]},
        )
    ]
    inbound.apply(incoming, event)
    message = api.get(MESSAGES).json()[0]
    assert message["payload"]["kind"] == "poll"
    path = MESSAGES + message["uuid"]
    question = message["payload"]["questions"][0]
    response = api.post(
        path + "/actions/vote/invoke",
        json={
            "question_uuid": question["uuid"],
            "option_uuid": question["options"][0]["uuid"],
            "selected": True,
        },
    )
    assert response.status_code == 200, response.text
    remote = copy.deepcopy(event["message"])
    remote["submessages"].append(widget(8, type="vote", key="canned,1", vote=1))
    http.request.return_value = {"message": remote}
    inbound.apply(
        incoming,
        {
            "type": "realm_user",
            "op": "add",
            "person": {
                "user_id": 8,
                "full_name": "Voter",
                "email": "voter@example.test",
            },
        },
    )
    update = {
        "type": "submessage",
        "message_id": 831,
        "msg_type": "widget",
        "sender_id": 8,
        "submessage_id": 10,
        "content": remote["submessages"][-1]["content"],
    }
    inbound.apply(incoming, update, update)
    current = api.get(path).json()
    assert current["poll_state"]["voter_count"] == 2
    assert current["status"] == "new"
    assert current["poll_state"]["my_votes"] == [
        {
            "question_uuid": question["uuid"],
            "option_uuids": [question["options"][0]["uuid"]],
        }
    ]
    assert db.execute(
        "SELECT COUNT(*) FROM zulip_outgoing_intents WHERE entity_type='poll_vote'"
    ).fetchone() == (1,)
    db.execute(
        "UPDATE zulip_outgoing_intents SET status='done' WHERE entity_type='poll_vote'"
    )
    remote["submessages"].extend(
        [
            widget(7, type="vote", key="canned,0", vote=1),
            widget(8, type="vote", key="canned,1", vote=-1),
            widget(8, type="new_option", idx=1, option="Wednesday"),
        ]
    )
    http.request.return_value = {"message": remote}
    inbound.apply(incoming, {**update, "submessage_id": 11})
    current = api.get(path).json()
    assert current["poll_state"]["voter_count"] == 1
    assert [o["text"] for o in current["payload"]["questions"][0]["options"]] == [
        "Monday",
        "Tuesday",
        "Wednesday",
    ]
    assert current["payload"]["questions"][0]["uuid"] == question["uuid"]
    # A prepared provider read may finish after a newer observation. It must
    # not undo the confirmed vote or remove a subsequently added option.
    stale = copy.deepcopy(event["message"])
    stale["submessages"].append(widget(8, type="vote", key="canned,1", vote=1))
    for index, submessage in enumerate(stale["submessages"], 1):
        submessage["id"] = index
    for index, submessage in enumerate(remote["submessages"], 1):
        submessage["id"] = index
    http.request.return_value = {"message": remote}
    inbound.apply(incoming, {**update, "submessage_id": 20})
    http.request.return_value = {"message": stale}
    inbound.apply(incoming, {**update, "submessage_id": 19})
    current = api.get(path).json()
    assert current["poll_state"]["my_votes"]
    assert current["payload"]["questions"][0]["options"][-1]["text"] == "Wednesday"


def test_extended_poll_sends_warning_and_exact_workspace_link(
    outgoing, db, _test_database_url
):
    api, account, channel, *_ = outgoing
    db.execute(
        "UPDATE m_external_accounts_v2 SET settings=settings||%s::jsonb WHERE uuid=%s",
        (json.dumps({"workspace_url": "https://workspace.example.test"}), account.uuid),
    )
    extended = payload(is_anonymous=True, multiple_answers=False)
    extended["questions"].append(copy.deepcopy(extended["questions"][0]))
    extended["questions"][1]["uuid"] = str(uuid.uuid4())
    result = api.post(
        MESSAGES, json={"stream_uuid": str(channel.stream_uuid), "payload": extended}
    )
    assert result.status_code == 201, result.text
    message = result.json()

    async def run():
        async with outbound.sender(account, _test_database_url) as (service, server):
            await outbound.drain(service)
            content = server.messages[101]["content"]
            assert "not fully supported in Zulip" in content
            assert content.count("Deployment?") == 2
            assert (
                f"/stream/{message['stream_uuid']}/topic/{message['topic_uuid']}?msg={message['uuid']}"
                in content
            )
            assert "View and vote in Workspace" in content
            closed = api.post(
                MESSAGES + message["uuid"] + "/actions/close_poll/invoke", json={}
            )
            assert closed.status_code == 200, closed.text
            await outbound.drain(service)
            assert "closed; read-only" in server.messages[101]["content"]
            await import_observation(
                service, account, {"type": "message", "message": server.messages[101]}
            )
            recovered = api.get(MESSAGES + message["uuid"]).json()
            assert (
                recovered["payload"]["kind"] == "poll"
                and len(recovered["payload"]["questions"]) == 2
            )
            assert recovered["poll_state"]["is_closed"]

    asyncio.run(run())


def test_provider_metadata_only_update_advances_the_poll_revision(incoming):
    api, *_rest, http, _url = incoming
    event = inbound.event("/poll Deployment?\nMonday\nTuesday", 832)
    event["message"]["submessages"] = [
        {
            "id": 1,
            **widget(
                7,
                widget_type="poll",
                extra_data={
                    "question": "Deployment?",
                    "options": ["Monday", "Tuesday"],
                },
            ),
        }
    ]
    inbound.apply(incoming, event)
    message = api.get(MESSAGES).json()[0]
    remote = copy.deepcopy(event["message"])
    remote["submessages"].append(
        {"id": 2, **widget(7, type="question", question="New question?")}
    )
    http.request.return_value = {"message": remote}
    inbound.apply(
        incoming, {"type": "submessage", "message_id": 832, "msg_type": "widget"}
    )
    current = api.get(MESSAGES + message["uuid"]).json()
    assert current["payload"]["questions"][0]["text"] == "New question?"
    assert current["poll_state"]["revision"] > message["poll_state"]["revision"]
    assert current["poll_state"]["voter_count"] == 0


def test_unsupported_provider_shape_bypasses_the_original_outgoing_poll_snapshot(
    outgoing, _test_database_url, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setattr(outbound, "Server", PollServer)
    created = api.post(
        MESSAGES, json={"stream_uuid": str(channel.stream_uuid), "payload": payload()}
    )
    assert created.status_code == 201, created.text
    message = created.json()

    async def run():
        async with outbound.sender(account, _test_database_url) as (service, server):
            await outbound.drain(service)
            remote = server.messages[101]
            for index in range(19):
                remote["submessages"].append(
                    {
                        "id": index + 2,
                        **widget(
                            7, type="new_option", idx=index, option=f"Extra {index}"
                        ),
                    }
                )
            await import_observation(
                service, account, {"type": "message", "message": remote}
            )
            current = api.get(MESSAGES + message["uuid"]).json()
            assert current["payload"]["kind"] == "markdown"
            assert "Extra 18" in current["payload"]["content"]

    asyncio.run(run())


def test_outgoing_poll_preserves_literal_option_prefixes(
    outgoing, _test_database_url, monkeypatch
):
    api, account, channel, *_ = outgoing
    monkeypatch.setattr(outbound, "Server", PollServer)
    body = payload()
    labels = ["-1", "*another option"]
    for option, label in zip(body["questions"][0]["options"], labels):
        option["text"] = label
    created = api.post(
        MESSAGES, json={"stream_uuid": str(channel.stream_uuid), "payload": body}
    )
    assert created.status_code == 201

    async def run():
        async with outbound.sender(account, _test_database_url) as (service, server):
            await outbound.drain(service)
            assert (
                list(poll_conversion.read(server.messages[101]).options.values())
                == labels
            )

    asyncio.run(run())


@pytest.mark.parametrize("extended", [False, True])
def test_history_replay_preserves_exported_poll_payload(
    outgoing, db, _test_database_url, extended
):
    from workspace.bridges.zulip import history

    api, account, channel, *_ = outgoing
    db.execute(
        "UPDATE m_external_accounts_v2 SET settings=settings||%s::jsonb WHERE uuid=%s",
        (json.dumps({"workspace_url": "https://workspace.example.test"}), account.uuid),
    )
    created = api.post(
        MESSAGES,
        json={
            "stream_uuid": str(channel.stream_uuid),
            "payload": payload(is_anonymous=extended),
        },
    ).json()

    async def run():
        async with outbound.sender(account, _test_database_url) as (service, server):
            await outbound.drain(service)
            source = (
                await service.db.sql(
                    zulip_outgoing.SELECT_ACCOUNT_SQL,
                    (account.uuid, account.project_id),
                )
            )[0]
            source["subscribed_streams"] = None
            operations = history.Operations(service.db, service.http)
            batch = await operations.prepare_batch(
                source, [copy.deepcopy(server.messages[101])]
            )
            async with service.db.transaction() as connection:
                await operations.merge_batch(connection, source, batch)
            replay = api.get(MESSAGES + created["uuid"]).json()
            assert replay["payload"] == created["payload"]
            assert replay["poll_state"] == created["poll_state"]

    asyncio.run(run())
