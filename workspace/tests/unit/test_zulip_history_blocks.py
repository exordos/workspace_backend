# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import copy

from workspace.bridges.zulip import history_blocks


def message():
    return {
        "id": 123,
        "type": "stream",
        "sender_id": 42,
        "stream_id": 3,
        "subject": "Subject",
        "content": "Body",
        "timestamp": 1700000000,
        "flags": ["read", "starred"],
        "reactions": [
            {
                "user_id": 42,
                "emoji_name": "smile",
                "emoji_code": "1f604",
                "reaction_type": "unicode_emoji",
            },
            {
                "user_id": 43,
                "emoji_name": "smile",
                "emoji_code": "1f604",
                "reaction_type": "unicode_emoji",
            },
        ],
    }


def test_semantic_hash_ignores_json_set_order_and_unused_fields():
    original = message()
    reordered = dict(reversed(list(copy.deepcopy(original).items())))
    reordered["flags"] = ["starred", "mentioned", "read"]
    reordered["reactions"].reverse()
    reordered["avatar_url"] = "https://example.com/changed.png"
    reordered["last_edit_timestamp"] = original["timestamp"]
    assert history_blocks.fingerprints([original]) == history_blocks.fingerprints(
        [reordered]
    )


def test_data_and_personal_hashes_are_independent():
    original = message()
    changed = copy.deepcopy(original)
    changed["flags"] = []
    assert (
        history_blocks.fingerprints([original])[0]
        == history_blocks.fingerprints([changed])[0]
    )
    assert (
        history_blocks.fingerprints([original])[1]
        != history_blocks.fingerprints([changed])[1]
    )
    changed = copy.deepcopy(original)
    changed["last_edit_timestamp"] = original["timestamp"] + 1
    assert (
        history_blocks.fingerprints([original])[0]
        != history_blocks.fingerprints([changed])[0]
    )
    assert (
        history_blocks.fingerprints([original])[1]
        == history_blocks.fingerprints([changed])[1]
    )


def test_missing_file_in_one_stream_cannot_use_another_streams_receipt():
    messages = [
        {"id": 1, "_files": [("/user_uploads/file.txt", "/user_uploads/file.txt")]}
    ]
    batch = {
        "messenger_messages": [{"provider_identity": "1", "uuid": "root"}],
        "messenger_message_placements": [
            {"message_uuid": "root", "stream_uuid": "one"}
        ],
        "m_workspace_files": [
            {"provider_identity": "/user_uploads/file.txt", "stream_uuid": "other"}
        ],
    }
    assert not history_blocks.cacheable(messages, batch)
    batch["m_workspace_files"][0]["stream_uuid"] = "one"
    assert history_blocks.cacheable(messages, batch)
