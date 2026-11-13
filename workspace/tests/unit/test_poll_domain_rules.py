# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import datetime
import uuid

import pytest
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import exceptions
from workspace.messenger_api.dm import polls


NOW = datetime.datetime(2026, 11, 12, 12, tzinfo=datetime.timezone.utc)


@pytest.fixture
def poll():
    return polls.POLL_TYPE.from_simple_type(
        {
            "kind": "poll",
            "multiple_answers": False,
            "is_anonymous": False,
            "allow_revote": False,
            "results_visibility": "after_vote",
            "allow_adding_options": True,
            "questions": [
                {
                    "uuid": str(uuid.uuid4()),
                    "text": "When?",
                    "options": [
                        {"uuid": str(uuid.uuid4()), "text": text}
                        for text in ("Today", "Tomorrow")
                    ],
                }
            ],
        }
    )


def identifiers(poll):
    question = poll.questions[0]
    return str(question["uuid"]), [
        str(option["uuid"]) for option in question["options"]
    ]


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("revote", [False, True])
def test_domain_selection_keeps_policies_and_idempotence(poll, multiple, revote):
    poll.multiple_answers = multiple
    poll.allow_revote = revote
    question, options = identifiers(poll)
    current = {options[0]}
    assert poll.select_options(question, options[0], True, current, NOW) == current
    if multiple or revote:
        expected = set(options) if multiple else {options[1]}
        assert poll.select_options(question, options[1], True, current, NOW) == expected
    else:
        with pytest.raises(exceptions.PollRevoteForbiddenError):
            poll.select_options(question, options[1], True, current, NOW)
    if revote:
        assert poll.select_options(question, options[0], False, current, NOW) == set()
    else:
        with pytest.raises(exceptions.PollRevoteForbiddenError):
            poll.select_options(question, options[0], False, current, NOW)
    assert current == {options[0]}


@pytest.mark.parametrize("closed_by", ["manual", "deadline"])
def test_domain_closed_poll_preserves_noop_but_rejects_a_real_change(poll, closed_by):
    if closed_by == "manual":
        poll.closed_at = NOW
    else:
        poll.closes_at = NOW
    question, options = identifiers(poll)
    assert poll.is_closed(NOW)
    assert poll.select_options(question, options[0], True, {options[0]}, NOW) == {
        options[0]
    }
    assert poll.select_options(question, options[0], False, set(), NOW) == set()
    with pytest.raises(exceptions.PollClosedError):
        poll.select_options(question, options[1], True, {options[0]}, NOW)
    # Membership is validated before treating an absent selection as a no-op.
    with pytest.raises(ra_exc.ValidationErrorException):
        poll.select_options(question, str(uuid.uuid4()), False, set(), NOW)


def test_domain_unknown_question_is_rejected(poll):
    with pytest.raises(ra_exc.ValidationErrorException):
        poll.question(str(uuid.uuid4()))


def test_domain_append_retry_survives_close_and_policy_change(poll):
    question, options = identifiers(poll)
    existing = {"uuid": options[0], "text": "Today"}
    poll.closed_at = NOW
    poll.allow_adding_options = False
    assert poll.append_option(question, existing, NOW) is False
    with pytest.raises(exceptions.PollConflictError):
        poll.append_option(question, {**existing, "text": "Different"}, NOW)
    with pytest.raises(exceptions.PollClosedError):
        poll.append_option(question, {"uuid": str(uuid.uuid4()), "text": "Later"}, NOW)


def test_domain_append_enforces_policy_and_current_limit(poll, monkeypatch):
    question, _ = identifiers(poll)
    option = {"uuid": str(uuid.uuid4()), "text": "Later"}
    poll.allow_adding_options = False
    with pytest.raises(exceptions.PollConflictError):
        poll.append_option(question, option, NOW)
    poll.allow_adding_options = True
    monkeypatch.setattr(polls, "MAX_POLL_OPTIONS_PER_QUESTION", 2)
    with pytest.raises(ra_exc.ValidationErrorException):
        poll.append_option(question, option, NOW)
    monkeypatch.setattr(polls, "MAX_POLL_OPTIONS_PER_QUESTION", 3)
    assert poll.append_option(question, option, NOW) is True
    assert polls.POLL_TYPE.to_simple_type(poll)["questions"][0]["options"][-1] == option


@pytest.mark.parametrize("offset", [-1, 0])
def test_domain_settings_deadline_is_checked_against_supplied_database_clock(
    poll, offset
):
    settings = {
        "closes_at": (NOW + datetime.timedelta(seconds=offset)).isoformat(),
        "allow_adding_options": False,
    }
    with pytest.raises(ra_exc.ValidationErrorException):
        poll.update_settings(settings, NOW)
    assert poll.closes_at is None and poll.allow_adding_options


def test_domain_settings_are_atomic_idempotent_and_not_allowed_after_close(poll):
    settings = {
        "closes_at": (NOW + datetime.timedelta(minutes=1)).isoformat(),
        "allow_adding_options": False,
    }
    assert poll.update_settings(settings, NOW) is True
    assert poll.update_settings(settings, NOW) is False
    assert poll.close(NOW) is True
    assert poll.close(NOW) is False
    with pytest.raises(exceptions.PollClosedError):
        poll.update_settings(settings, NOW)
    assert poll.closed_at == NOW


def test_domain_expiration_does_not_rewrite_manual_close_time(poll):
    poll.closes_at = NOW
    assert poll.close(NOW) is False
    assert poll.closed_at is None
