# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import copy
import json
import uuid

import pytest

from workspace.bridges.zulip import message_conversion, outgoing_operations

WORKSPACE_URL = "https://workspace.example.test/message"


def poll():
    return {
        "kind": "poll",
        "questions": [
            {
                "uuid": str(uuid.uuid4()),
                "text": question,
                "options": [
                    {"uuid": str(uuid.uuid4()), "text": text}
                    for text in ("First", "Second")
                ],
            }
            for question in ("When?", "Where?")
        ],
        "multiple_answers": False,
        "is_anonymous": True,
        "allow_revote": True,
        "results_visibility": "always",
    }


def test_native_poll_exports_every_question_without_personal_votes():
    payload = poll()
    state = {
        "is_closed": True,
        "my_votes": [{"private": "secret-voter"}],
        "questions": [
            {
                "question_uuid": q["uuid"],
                "option_counts": {o["uuid"]: n for n, o in enumerate(q["options"])},
            }
            for q in payload["questions"]
        ],
    }
    before = copy.deepcopy((payload, state))
    text = message_conversion.workspace_to_zulip(
        payload, {"poll_state": state, "workspace_message_url": WORKSPACE_URL}
    )
    assert "Poll snapshot (closed; read-only)" in text
    assert "When?" in text and "Where?" in text
    assert text.count("First — 0 votes") == 2
    assert text.count("Second — 1 votes") == 2
    assert "secret-voter" not in text and "my_votes" not in text
    assert (payload, state) == before
    assert message_conversion.zulip_to_workspace(
        {"id": 1, "content": text}, {"server_url": "https://zulip.example.test"}
    ) == {
        "kind": "markdown",
        "content": text.replace(
            "(" + WORKSPACE_URL + ")", "(urn:url:" + WORKSPACE_URL + ")"
        ),
    }


def test_snapshot_text_cannot_turn_into_mentions_or_fences():
    payload = poll()
    payload["questions"][0]["text"] = "```\n@**Everyone|1** [link](urn:user:x)"
    text = message_conversion.workspace_to_zulip(
        payload, {"workspace_message_url": WORKSPACE_URL}
    )
    assert "````text\n" in text
    assert "Results unavailable" in text
    assert text.endswith("````")


def widget(sender, data):
    return {"sender_id": sender, "msg_type": "widget", "content": json.dumps(data)}


def test_zulip_snapshot_replays_votes_options_and_author_question_changes():
    message = {
        "id": 71,
        "sender_id": 9,
        "content": "/poll Original\nA\nB",
        "submessages": [
            widget(
                9,
                {
                    "widget_type": "poll",
                    "extra_data": {"question": "Original", "options": ["A", "B", "A"]},
                },
            ),
            widget(12, {"type": "vote", "key": "canned,0", "vote": 1}),
            widget(12, {"type": "vote", "key": "canned,0", "vote": 1}),
            widget(13, {"type": "vote", "key": "canned,0", "vote": 1}),
            widget(13, {"type": "vote", "key": "canned,0", "vote": -1}),
            widget(12, {"type": "new_option", "idx": 0, "option": "C"}),
            widget(13, {"type": "vote", "key": "12,0", "vote": 1}),
            widget(12, {"type": "question", "question": "Forged"}),
            widget(9, {"type": "question", "question": "Updated"}),
            {"msg_type": "widget", "content": "{broken"},
        ],
    }
    context = {
        "server_url": "https://zulip.example.test",
        "original_url": "https://zulip.example.test/#narrow/near/71",
    }
    payload, reusable = message_conversion.zulip_to_workspace_cached(message, context)
    assert not reusable
    assert "Updated" in payload["content"] and "Forged" not in payload["content"]
    assert payload["content"].count("- A — 1 votes") == 1
    assert "- B — 0 votes" in payload["content"]
    assert "- C — 1 votes" in payload["content"]
    assert "Original message" in payload["content"]
    message["submessages"].append(
        widget(12, {"type": "vote", "key": "canned,0", "vote": -1})
    )
    assert (
        "- A — 0 votes"
        in message_conversion.zulip_to_workspace(message, context)["content"]
    )


@pytest.mark.parametrize(
    "submessages", [[], [{"msg_type": "widget", "content": "null"}]]
)
def test_poll_without_widget_data_keeps_question_and_options(submessages):
    payload = message_conversion.zulip_to_workspace(
        {"id": 71, "content": "/poll Question\nOne\nTwo", "submessages": submessages},
        {
            "server_url": "https://zulip.example.test",
            "original_url": "https://zulip.example.test/#narrow/near/71",
        },
    )
    assert "Question" in payload["content"]
    assert "- One" in payload["content"] and "- Two" in payload["content"]
    assert "Results unavailable" in payload["content"]


def test_oversized_poll_snapshot_is_rejected_before_sending():
    payload = poll()
    payload["questions"] = [
        {
            "uuid": str(uuid.uuid4()),
            "text": "q" * 1000,
            "options": [
                {"uuid": str(uuid.uuid4()), "text": "o" * 200} for _ in range(20)
            ],
        }
        for _ in range(20)
    ]
    with pytest.raises(ValueError, match="snapshot.*limit"):
        message_conversion.workspace_to_zulip(payload, {})


def test_outgoing_operation_keeps_the_original_poll_for_its_provider_echo():
    class Database:
        saved = None

        async def sql(self, query, parameters=()):
            if "messenger_resolve_message_content" in query:
                self.saved = parameters[0].obj
                return [{"uuid": uuid.uuid4()}]
            return []

    db = Database()
    operations = outgoing_operations.Operations(db, None)
    payload = poll()
    result = asyncio.run(
        operations.convert(
            {
                "project_id": uuid.uuid4(),
                "data": {
                    "payload": payload,
                    "stream_uuid": uuid.uuid4(),
                    "topic_uuid": uuid.uuid4(),
                    "placement_uuid": uuid.uuid4(),
                },
            },
            {
                "provider_uuid": uuid.uuid4(),
                "settings": {
                    "server_url": "https://zulip.example.test",
                    "workspace_url": "https://workspace.example.test",
                },
            },
        )
    )
    assert db.saved == payload
    assert "When?" in result["provider_content"]
    assert "Where?" in result["provider_content"]
    assert "not fully supported in Zulip" in result["provider_content"]
    assert (
        "https://workspace.example.test/org/workspace.example.test/project/"
        in result["provider_content"]
    )
