# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import pytest
from oslo_config import cfg
from restalchemy.common import exceptions as ra_exc

from workspace.common import messenger_poll_opts
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import polls


def option():
    return {"uuid": str(uuid.uuid4()), "text": "Option"}


def question():
    return {
        "uuid": str(uuid.uuid4()),
        "text": "Question",
        "multiple_answers": False,
        "options": [option(), option()],
    }


@pytest.fixture
def payload():
    messenger_poll_opts.register_opts()
    return {
        "kind": "poll",
        "questions": [question()],
        "is_anonymous": True,
        "allow_revote": False,
        "results_visibility": "after_vote",
    }


def test_poll_limits_apply_to_new_writes_and_preserve_existing_reads(payload):
    group = messenger_poll_opts.DOMAIN
    payload["questions"] += [question() for _ in range(19)]
    payload["questions"][0]["options"] += [option() for _ in range(18)]
    saved = polls.normalize_create(payload)
    assert saved["allow_adding_options"] is False
    assert saved["closes_at"] is None and saved["closed_at"] is None
    try:
        for setting in ("poll_max_questions", "poll_max_options_per_question"):
            cfg.CONF.set_override(setting, 2, group=group)
            with pytest.raises(ra_exc.ValidationErrorException):
                polls.normalize_create(payload)
            restored = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(
                saved
            )
            assert polls.POLL_TYPE.to_simple_type(restored) == saved
            cfg.CONF.clear_override(setting, group=group)
    finally:
        cfg.CONF.clear_override("poll_max_questions", group=group)
        cfg.CONF.clear_override("poll_max_options_per_question", group=group)


@pytest.mark.parametrize(
    "change",
    [
        lambda p, q: p.update(questions=[]),
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


def test_poll_timestamps_are_normalized_and_preserve_precision(payload):
    payload["closes_at"] = "2099-04-01T01:02:03.123456+02:30"
    assert polls.normalize_create(payload)["closes_at"] == "2099-03-31T22:32:03.123456Z"


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
