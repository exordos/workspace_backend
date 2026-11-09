# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import polls


def option():
    return {"uuid": str(uuid.uuid4()), "text": "Option"}


def question():
    return {
        "uuid": str(uuid.uuid4()),
        "text": "Question",
        "options": [option(), option()],
    }


@pytest.fixture
def payload():
    return {
        "kind": "poll",
        "multiple_answers": False,
        "questions": [question()],
        "is_anonymous": True,
        "allow_revote": False,
        "results_visibility": "after_vote",
    }


def test_poll_limits_apply_to_new_writes_and_preserve_existing_reads(payload, monkeypatch):
    payload["questions"] += [question() for _ in range(19)]
    payload["questions"][0]["options"] += [option() for _ in range(18)]
    saved = polls.normalize_create(payload)
    assert saved["allow_adding_options"] is False
    assert saved["closes_at"] is None and saved["closed_at"] is None

    for constant in ("MAX_POLL_QUESTIONS", "MAX_POLL_OPTIONS_PER_QUESTION"):
        with monkeypatch.context() as patch:
            patch.setattr(polls, constant, 2)
            with pytest.raises(ra_exc.ValidationErrorException):
                polls.normalize_create(payload)
            restored = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
                saved
            )
            assert polls.POLL_TYPE.to_simple_type(restored) == saved


def test_poll_limits_reject_twenty_one_questions_or_options(payload):
    payload["questions"] = [question() for _ in range(21)]
    with pytest.raises(ra_exc.ValidationErrorException):
        polls.normalize_create(payload)
    payload["questions"] = [question()]
    payload["questions"][0]["options"] = [option() for _ in range(21)]
    with pytest.raises(ra_exc.ValidationErrorException):
        polls.normalize_create(payload)


@pytest.mark.parametrize(
    "change",
    [
        lambda p, q: p.update(questions=[]),
        lambda p, q: p.pop("multiple_answers"),
        lambda p, q: p.update(multiple_answers="false"),
        lambda p, q: p.update(multiple_answers=0),
        lambda p, q: p.update(multiple_answers=None),
        lambda p, q: q.update(multiple_answers=False),
        lambda p, q: p["questions"].append(q),
        lambda p, q: q.update(options=q["options"][:1]),
        lambda p, q: q["options"].append(q["options"][0]),
        lambda p, q: q.update(correct_option_uuids=[str(uuid.uuid4())]),
        lambda p, q: q.update(correct_option_uuids=[q["options"][0]["uuid"]] * 2),
        lambda p, q: q.update(correct_option_uuids={}),
        lambda p, q: q.update(extra=False),
        lambda p, q: q["options"][0].update(extra="hidden"),
        lambda p, q: q["options"][0].update(text=" "),
        lambda p, q: q.update(uuid=1),
        lambda p, q: p.update(is_anonymous="false"),
        lambda p, q: p.update(closes_at="2099-01-01T12:30:00"),
        lambda p, q: p.update(closes_at="2000-01-01T12:30:00Z"),
        lambda p, q: p.update(closed_at=None),
    ],
)
def test_poll_create_rejects_ambiguous_or_server_owned_values(payload, change):
    change(payload, payload["questions"][0])
    with pytest.raises(ra_exc.RestAlchemyException):
        polls.normalize_create(payload)


@pytest.mark.parametrize("direct", [False, True], ids=["wire", "model"])
@pytest.mark.parametrize(
    "change",
    [
        lambda questions: questions.clear(),
        lambda questions: questions[0]["options"].pop(),
        lambda questions: questions[0].pop("text"),
        lambda questions: questions[0]["options"][0].pop("text"),
    ],
    ids=["no-questions", "one-option", "missing-question-text", "missing-option-text"],
)
def test_poll_structure_is_checked_for_wire_and_model_values(payload, direct, change):
    if direct:
        payload["questions"] = [
            polls.POLL_QUESTION.from_simple_type(q) for q in payload["questions"]
        ]
    change(payload["questions"])
    with pytest.raises(ra_exc.RestAlchemyException):
        if direct:
            polls.PollPayload(**payload)
        else:
            polls.POLL_TYPE.from_simple_type(payload)


def test_poll_timestamps_are_normalized_and_preserve_precision(payload):
    payload["closes_at"] = "2099-04-01T01:02:03.123456+02:30"
    assert polls.normalize_create(payload)["closes_at"] == "2099-03-31T22:32:03.123456Z"


def test_poll_creation_retry_preserves_expired_deadline(payload):
    payload["closes_at"] = "2000-01-01T12:30:00Z"
    assert (
        polls.normalize_create(payload, retry=True)["closes_at"] == payload["closes_at"]
    )


@pytest.mark.parametrize(
    "action,values",
    [
        (
            "vote",
            {
                "question_uuid": str(uuid.uuid4()),
                "option_uuid": str(uuid.uuid4()),
                "selected": "false",
            },
        ),
        (
            "vote",
            {
                "question_uuid": str(uuid.uuid4()),
                "option_uuid": str(uuid.uuid4()),
                "selected": True,
                "user_uuid": str(uuid.uuid4()),
            },
        ),
        ("update_settings", {"settings": {"allow_adding_options": False}}),
        ("update_settings", {"settings": {"closes_at": None}}),
        (
            "update_settings",
            {
                "settings": {
                    "closes_at": None,
                    "allow_adding_options": False,
                    "closed_at": None,
                }
            },
        ),
        ("add_poll_option", {"question_uuid": "bad", "option": option()}),
        ("close_poll", {"force": True}),
    ],
)
def test_poll_action_bodies_reject_partial_or_extra_data(action, values):
    with pytest.raises(ra_exc.ValidationErrorException):
        polls.action_body(action, values)


@pytest.mark.parametrize("multiple_answers", [False, True])
def test_poll_global_multiple_answers_round_trip(payload, multiple_answers):
    payload["multiple_answers"] = multiple_answers
    payload["questions"].append(question())
    saved = polls.normalize_create(payload)
    assert saved["multiple_answers"] is multiple_answers
    assert all("multiple_answers" not in q for q in saved["questions"])
    restored = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(saved)
    assert polls.POLL_TYPE.to_simple_type(restored) == saved


def test_poll_rejects_legacy_question_selection_mode(payload):
    payload.pop("multiple_answers")
    for q in payload["questions"]:
        q["multiple_answers"] = False
    with pytest.raises(ra_exc.RestAlchemyException):
        polls.normalize_create(payload)


@pytest.mark.parametrize("multiple_answers", [False, True])
@pytest.mark.parametrize("correct_count", [0, 1, 2])
def test_poll_correct_answers_are_independent_of_global_selection_mode(
    payload, multiple_answers, correct_count
):
    payload["multiple_answers"] = multiple_answers
    payload["questions"].append(question())
    for q in payload["questions"]:
        q["correct_option_uuids"] = [o["uuid"] for o in q["options"][:correct_count]]
    saved = polls.normalize_create(payload)
    assert [q["correct_option_uuids"] for q in saved["questions"]] == [
        q["correct_option_uuids"] for q in payload["questions"]
    ]
