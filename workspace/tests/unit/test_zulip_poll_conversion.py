# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import asyncio
import copy
import json
import uuid
from unittest import mock

import pytest

from workspace.bridges.zulip import outgoing_operations, poll_conversion


def compatible_poll():
    return {
        "kind": "poll",
        "questions": [
            {
                "uuid": str(uuid.uuid4()),
                "text": "When?",
                "options": [
                    {"uuid": str(uuid.uuid4()), "text": text}
                    for text in ("Monday", "Tuesday")
                ],
            }
        ],
        "multiple_answers": True,
        "is_anonymous": False,
        "allow_revote": True,
        "results_visibility": "always",
        "allow_adding_options": True,
        "closes_at": None,
        "closed_at": None,
    }


def test_compatible_poll_uses_the_native_zulip_command():
    payload = compatible_poll()
    assert poll_conversion.compatible(payload)
    assert poll_conversion.command(payload) == "/poll When?\nMonday\nTuesday"


@pytest.mark.parametrize(
    "change",
    [
        {"is_anonymous": True},
        {"multiple_answers": False},
        {"allow_revote": False},
        {"results_visibility": "after_vote"},
        {"allow_adding_options": False},
        {"closes_at": "2099-01-01T00:00:00Z"},
        {"closed_at": "2026-01-01T00:00:00Z"},
    ],
)
def test_extended_policy_is_never_silently_downgraded(change):
    assert not poll_conversion.compatible({**compatible_poll(), **change})


def test_incompatible_question_shapes_are_not_exported_as_interactive_polls():
    payload = compatible_poll()
    payload["questions"].append(copy.deepcopy(payload["questions"][0]))
    assert not poll_conversion.compatible(payload)
    payload["questions"].pop()
    q = payload["questions"][0]
    q["correct_option_uuids"] = [q["options"][0]["uuid"]]
    assert not poll_conversion.compatible(payload)
    q.pop("correct_option_uuids")
    q["options"][1]["text"] = q["options"][0]["text"]
    assert not poll_conversion.compatible(payload)
    q["options"][1]["text"] = "One\nTwo"
    assert not poll_conversion.compatible(payload)


def test_provider_poll_identity_is_stable_and_preserves_exported_option_ids():
    native = compatible_poll()
    message = {
        "id": 123,
        "sender_id": 7,
        "content": "/poll When?\nMonday\nTuesday",
        "submessages": [
            {
                "sender_id": 7,
                "msg_type": "widget",
                "content": json.dumps(
                    {
                        "widget_type": "poll",
                        "extra_data": {
                            "question": "When?",
                            "options": ["Monday", "Tuesday"],
                        },
                    }
                ),
            }
        ],
    }
    namespace = uuid.uuid4()
    observed = poll_conversion.read(message)
    payload = poll_conversion.payload(observed, namespace, message["id"])
    assert poll_conversion.compatible(payload)
    assert payload == poll_conversion.payload(observed, namespace, message["id"])
    assert poll_conversion.payload(observed, namespace, message["id"], native) == native


@pytest.mark.parametrize("question", ["", "Two\nlines", "@**Everyone**", " Padded "])
def test_unrepresentable_provider_questions_remain_snapshots(question):
    observed = poll_conversion.Poll(
        question,
        {"canned,0": "A", "canned,1": "B"},
        {"canned,0": set(), "canned,1": set()},
    )
    assert poll_conversion.payload(observed, uuid.uuid4(), 1) is None


def test_lost_vote_response_does_not_reassert_a_vote_changed_later_in_zulip():
    submessages = []
    for identifier, data in enumerate(
        [
            {
                "widget_type": "poll",
                "extra_data": {"question": "When?", "options": ["A", "B"]},
            },
            {"type": "vote", "key": "canned,0", "vote": 1},
            {"type": "vote", "key": "canned,0", "vote": -1},
        ],
        1,
    ):
        submessages.append(
            {
                "id": identifier,
                "sender_id": 7,
                "msg_type": "widget",
                "content": json.dumps(data),
            }
        )
    http = mock.Mock(
        request=mock.AsyncMock(
            return_value={
                "message": {"id": 1, "sender_id": 7, "submessages": submessages}
            }
        )
    )
    operations = outgoing_operations.Operations(None, http)
    done, result = asyncio.run(
        operations.verify(
            {},
            {},
            {
                "kind": "poll_vote",
                "message_id": 1,
                "user_id": 7,
                "option_key": "canned,0",
                "selected": True,
                "after_revision": 1,
            },
        )
    )
    assert done
    assert result["poll_revision"] == 3


def test_message_url_matches_default_ports_and_the_public_placement():
    keys = [uuid.uuid4() for _ in range(4)]
    assert poll_conversion.message_url(
        "https://workspace.example.test:443/", *keys
    ) == poll_conversion.message_url("https://workspace.example.test", *keys).replace(
        "https://workspace.example.test/", "https://workspace.example.test:443/"
    )
    url = poll_conversion.message_url("https://192.0.2.1:18443", *keys)
    assert "/org/192.0.2.1-18443/project/" in url and url.endswith(
        "?msg=" + str(keys[-1])
    )
