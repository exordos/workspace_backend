# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import json
import types
import uuid as sys_uuid

from workspace.common import messenger_reaction_opts
from workspace.messenger_api import reaction_users


PROJECT_UUID = sys_uuid.UUID("10000000-0000-0000-0000-000000000001")
MESSAGE_UUID_1 = sys_uuid.UUID("20000000-0000-0000-0000-000000000001")
MESSAGE_UUID_2 = sys_uuid.UUID("20000000-0000-0000-0000-000000000002")
USER_UUIDS = tuple(
    sys_uuid.UUID(f"30000000-0000-0000-0000-{index:012d}") for index in range(1, 7)
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)


class FakeConf:
    def __init__(self, limit=4):
        self.options = types.SimpleNamespace(user_list_limit=limit)

    def __getitem__(self, group):
        assert group == messenger_reaction_opts.DOMAIN
        return self.options


def test_lock_messages_deduplicates_and_orders_uuid_locks():
    session = FakeSession([[]])

    reaction_users.lock_messages(
        PROJECT_UUID,
        (MESSAGE_UUID_2, MESSAGE_UUID_1, MESSAGE_UUID_2),
        session=session,
    )

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert 'ORDER BY "uuid"' in statement
    assert "FOR UPDATE" in statement
    assert parameters == (
        PROJECT_UUID,
        [MESSAGE_UUID_1, MESSAGE_UUID_2],
    )


def test_refresh_groups_persists_only_complete_bounded_lists():
    session = FakeSession(
        [
            [
                {
                    "uuid": MESSAGE_UUID_1,
                    "reaction_users": {
                        "eyes": ["stale"],
                        "keep": [str(USER_UUIDS[5])],
                    },
                }
            ],
            [
                {
                    "message_uuid": MESSAGE_UUID_1,
                    "emoji_name": "heart",
                    "user_uuids": USER_UUIDS[:2],
                },
                {
                    "message_uuid": MESSAGE_UUID_1,
                    "emoji_name": "eyes",
                    "user_uuids": USER_UUIDS[:5],
                },
                {
                    "message_uuid": MESSAGE_UUID_1,
                    "emoji_name": "gone",
                    "user_uuids": None,
                },
            ],
            [],
        ]
    )

    reaction_users.refresh_groups(
        PROJECT_UUID,
        (
            (MESSAGE_UUID_1, "heart"),
            (MESSAGE_UUID_1, "gone"),
            (MESSAGE_UUID_1, "eyes"),
        ),
        session=session,
        conf=FakeConf(),
    )

    assert len(session.calls) == 3
    lookup_statement, lookup_parameters = session.calls[1]
    assert "LIMIT %s" in lookup_statement
    assert lookup_parameters == (
        [MESSAGE_UUID_1, MESSAGE_UUID_1, MESSAGE_UUID_1],
        ["eyes", "gone", "heart"],
        PROJECT_UUID,
        5,
    )
    update_statement, update_parameters = session.calls[2]
    assert 'SET "reaction_users"' in update_statement
    assert json.loads(update_parameters[0]) == {
        "heart": [str(USER_UUIDS[0]), str(USER_UUIDS[1])],
        "keep": [str(USER_UUIDS[5])],
    }
    assert update_parameters[1:] == (PROJECT_UUID, MESSAGE_UUID_1)


def test_refresh_groups_zero_limit_only_removes_affected_keys():
    session = FakeSession(
        [
            [
                {
                    "uuid": MESSAGE_UUID_1,
                    "reaction_users": {
                        "heart": [str(USER_UUIDS[0])],
                        "keep": [str(USER_UUIDS[1])],
                    },
                }
            ],
            [],
        ]
    )

    reaction_users.refresh_groups(
        PROJECT_UUID,
        ((MESSAGE_UUID_1, "heart"),),
        session=session,
        conf=FakeConf(limit=0),
    )

    assert len(session.calls) == 2
    assert reaction_users.REACTION_USERS_SQL not in {
        statement for statement, _parameters in session.calls
    }
    assert json.loads(session.calls[1][1][0]) == {
        "keep": [str(USER_UUIDS[1])],
    }


def test_refresh_groups_skips_unchanged_message_snapshot():
    session = FakeSession(
        [
            [
                {
                    "uuid": MESSAGE_UUID_1,
                    "reaction_users": {
                        "heart": [str(USER_UUIDS[0])],
                    },
                }
            ],
            [
                {
                    "message_uuid": MESSAGE_UUID_1,
                    "emoji_name": "heart",
                    "user_uuids": USER_UUIDS[:1],
                }
            ],
        ]
    )

    reaction_users.refresh_groups(
        PROJECT_UUID,
        ((MESSAGE_UUID_1, "heart"),),
        session=session,
        conf=FakeConf(),
    )

    assert len(session.calls) == 2
