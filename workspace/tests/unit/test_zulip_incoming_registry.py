# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import dataclasses

from workspace.bridges.zulip import incoming_registry


def account(identifier="first", project="project", provider="provider"):
    return {"uuid": identifier, "project_id": project, "provider_uuid": provider}


def message():
    return {
        "type": "message",
        "id": 1,
        "message": {
            "id": 101,
            "sender_id": 7,
            "type": "stream",
            "stream_id": 42,
            "subject": "General",
            "timestamp": 100,
            "content": "Shared",
            "flags": [],
        },
    }


def test_personal_fields_and_queue_event_ids_do_not_split_shared_changes():
    registry = incoming_registry.Registry()
    first, new = registry.admit(account(), message(), 1)
    assert not new
    copy = message()
    copy["id"] = 99
    copy["message"]["flags"] = ["read", "starred"]
    copy["local_message_id"] = "local-only-receipt"
    duplicate, reused = registry.admit(account("second"), copy, 2)
    assert reused and duplicate is first
    assert len(registry.entries) == 1


def test_provider_and_project_namespaces_remain_separate():
    registry = incoming_registry.Registry()
    entries = [
        registry.admit(owner, message(), index)[0]
        for index, owner in enumerate(
            (account(), account(project="other"), account(provider="other")), 1
        )
    ]
    assert len({entry.key for entry in entries}) == 3


def test_successful_entries_expire_but_pending_and_retrying_entries_do_not():
    now = [0]
    registry = incoming_registry.Registry(clock=lambda: now[0])
    done, _ = registry.admit(account(), message(), 1)
    pending_event = message()
    pending_event["message"]["id"] = 102
    pending, _ = registry.admit(account(), pending_event, 2)
    registry.complete(done.key)
    now[0] = 1799
    registry.clean()
    assert len(registry.entries) == 2
    now[0] = 1800
    registry.clean()
    assert list(registry.entries) == [pending.key]
    assert registry.admit(account("second"), pending_event, 3)[1]


def test_distinct_successive_edits_and_late_copies_remain_distinct():
    registry = incoming_registry.Registry()
    changes = [
        {
            "type": "update_message",
            "id": index,
            "message_id": 101,
            "edit_timestamp": 100 + index,
            "orig_content": old,
            "content": new,
            "flags": [],
        }
        for index, (old, new) in enumerate((("a", "b"), ("b", "c"), ("c", "b")), 1)
    ]
    entries = []
    for index, event in enumerate(changes, 1):
        entry, duplicate = registry.admit(account(), event, index)
        assert not duplicate
        registry.complete(entry.key)
        entries.append(entry)
    for index, event in enumerate(changes, 10):
        copy = {**event, "id": index, "flags": ["starred"]}
        entry, duplicate = registry.admit(account("second"), copy, index)
        assert duplicate and entry is entries[index - 10]


def test_repeating_an_identical_edit_on_one_account_is_a_new_occurrence():
    registry = incoming_registry.Registry()
    event = {
        "type": "update_message",
        "message_id": 101,
        "edit_timestamp": 100,
        "orig_content": "a",
        "content": "b",
    }
    first, _ = registry.admit(account(), event, 1)
    registry.complete(first.key)
    registry.admit(account(), {**event, "orig_content": "b", "content": "a"}, 2)
    repeated, duplicate = registry.admit(account(), event, 3)
    assert not duplicate and repeated.key != first.key


def test_unversioned_toggles_are_not_mistaken_for_an_earlier_occurrence():
    event = {
        "type": "reaction",
        "op": "add",
        "message_id": 101,
        "user_id": 7,
        "emoji_name": "eyes",
        "emoji_code": "1f440",
        "reaction_type": "unicode_emoji",
    }
    assert incoming_registry.change(account(), event) is None


def test_restore_only_unfinished_intentions_and_retains_seen_accounts():
    registry = incoming_registry.Registry()
    entry, _ = registry.admit(account(), message(), 1)
    restored = incoming_registry.Registry()
    metadata = dataclasses.asdict(entry)
    metadata.pop("completed_at")
    restored.restore(
        [
            {"external_account_uuid": "first", "payload": {"_shared_key": metadata}},
            {
                "external_account_uuid": "second",
                "payload": {"_shared_key": metadata, "_personal_only": True},
            },
        ]
    )
    assert len(restored.entries) == 1
    assert restored.entries[entry.key].completed_at is None
    assert restored.admit(account("third"), message(), 3)[1]


def test_topic_rename_events_keep_the_existing_path():
    event = {
        "type": "update_message",
        "message_id": 101,
        "edit_timestamp": 101,
        "subject": "Renamed",
        "orig_subject": "General",
        "content": "Changed",
    }
    assert incoming_registry.change(account(), event) is None
