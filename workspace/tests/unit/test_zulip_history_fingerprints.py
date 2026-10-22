# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import copy
import hashlib

import pytest

from workspace.bridges.zulip import history_fingerprints


def message(message_id=1, **changes):
    return {
        "id": message_id,
        "sender_id": 7,
        "timestamp": 100,
        "type": "stream",
        "stream_id": 42,
        "subject": "",
        "is_me_message": False,
        "content": "Exact Markdown **Привет**\n",
        "flags": ["read", "starred"],
        "reactions": [],
        **changes,
    }


@pytest.mark.parametrize(
    "value,expected", [(1, 1), (1000, 1), (1001, 1001), (2000, 1001)]
)
def test_fixed_source_id_ranges(value, expected):
    assert history_fingerprints.range_start(value) == expected


def test_source_text_hash_reuses_the_existing_exact_utf8_hash():
    value = message()
    assert (
        history_fingerprints.message_hashes(value)["text"]
        == hashlib.sha256(value["content"].encode("utf-8")).hexdigest()
    )
    assert (
        history_fingerprints.message_hashes(value)["text"]
        != (
            history_fingerprints.message_hashes(
                message(content=value["content"].rstrip())
            )["text"]
        )
    )


def test_flags_are_a_canonical_set_and_do_not_change_content():
    original = history_fingerprints.message_hashes(message())
    assert original == history_fingerprints.message_hashes(
        message(flags=["starred", "read", "read"])
    )
    changed = history_fingerprints.message_hashes(message(flags=["read"]))
    assert history_fingerprints.changed_components(changed, original) == ["flags"]


def test_identical_text_with_a_new_edit_version_is_a_metadata_change():
    original = history_fingerprints.message_hashes(message())
    changed = history_fingerprints.message_hashes(message(last_edit_timestamp=101))
    assert history_fingerprints.changed_components(changed, original) == ["metadata"]


def test_topic_move_does_not_need_text_conversion():
    original = history_fingerprints.message_hashes(message())
    changed = history_fingerprints.message_hashes(message(subject="Another topic"))
    assert history_fingerprints.changed_components(changed, original) == ["metadata"]


def test_direct_recipients_have_stable_identity_order():
    first = message(
        type="private",
        display_recipient=[
            {"id": 9, "full_name": "Nine"},
            {"id": 7, "full_name": "Seven"},
        ],
    )
    second = copy.deepcopy(first)
    second["display_recipient"].reverse()
    assert history_fingerprints.message_hashes(
        first
    ) == history_fingerprints.message_hashes(second)


def test_reaction_order_and_unicode_aliases_do_not_cause_processing():
    reaction = {
        "user_id": 7,
        "reaction_type": "unicode_emoji",
        "emoji_code": "1f44d",
        "emoji_name": "+1",
    }
    other = {**reaction, "user_id": 8}
    first = message(reactions=[reaction, other])
    second = message(
        reactions=[{**other, "emoji_name": "thumbs_up"}, reaction, reaction]
    )
    assert history_fingerprints.message_hashes(
        first
    ) == history_fingerprints.message_hashes(second)


def test_missing_reaction_snapshot_is_not_an_observed_empty_snapshot():
    value = message()
    del value["reactions"]
    assert history_fingerprints.message_hashes(value)["reactions"] is None
    assert history_fingerprints.message_hashes(message())["reactions"] is not None
    assert history_fingerprints.batch_hashes([value], 1, 1000)["reactions"] is None


def test_batch_digest_includes_ids_and_the_exact_covered_tail():
    first = history_fingerprints.batch_hashes([message(1)], 1, 1000)
    moved_id = history_fingerprints.batch_hashes([message(2)], 1, 1000)
    partial = history_fingerprints.batch_hashes([message(1)], 1, 999)
    assert all(first[key] != moved_id[key] != partial[key] for key in first)
    assert first != history_fingerprints.batch_hashes([], 1, 1000)


def test_batch_order_does_not_change_hashes_and_observation_time_is_excluded():
    first = message(1, observed_at="2026-01-01")
    second = message(2)
    original = history_fingerprints.batch_hashes([first, second], 1, 1000)
    first["observed_at"] = "2026-02-01"
    assert original == history_fingerprints.batch_hashes([second, first], 1, 1000)


def test_unknown_previous_observation_requires_all_components():
    assert history_fingerprints.changed_components(
        history_fingerprints.message_hashes(message()), None
    ) == list(history_fingerprints.COMPONENTS)
