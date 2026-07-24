# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import identity_linking


class Session:
    def __init__(self):
        self.statements = []

    def execute(self, statement, values):
        self.statements.append((statement, values))


def test_payload_identity_rewrites_share_one_scan_per_table():
    session = Session()
    replacements = [
        (sys_uuid.uuid4(), sys_uuid.uuid4()),
        (sys_uuid.uuid4(), sys_uuid.uuid4()),
    ]

    identity_linking._rewrite_payload_uuid_references(session, replacements)

    assert len(session.statements) == 3
    for statement, values in session.statements:
        assert statement.count("replace(") == 2
        assert "LIKE ANY(%s::text[])" in statement
        assert len(values) == 5
        assert values[-1] == [
            f"%{legacy}%"
            for legacy, _canonical in sorted(
                replacements,
                key=lambda item: item[0].int,
            )
        ]


def test_payload_identity_rewrites_are_bounded():
    session = Session()
    replacements = [
        (sys_uuid.uuid4(), sys_uuid.uuid4())
        for _unused in range(identity_linking._PAYLOAD_REWRITE_BATCH_SIZE + 1)
    ]

    identity_linking._rewrite_payload_uuid_references(session, replacements)

    assert len(session.statements) == 6
    assert [
        statement.count("replace(") for statement, _values in session.statements
    ] == [
        identity_linking._PAYLOAD_REWRITE_BATCH_SIZE,
        identity_linking._PAYLOAD_REWRITE_BATCH_SIZE,
        identity_linking._PAYLOAD_REWRITE_BATCH_SIZE,
        1,
        1,
        1,
    ]


def test_payload_identity_rewrite_rejects_conflicting_targets():
    session = Session()
    legacy = sys_uuid.uuid4()

    with pytest.raises(
        ValueError,
        match="conflicting canonical users",
    ):
        identity_linking._rewrite_payload_uuid_references(
            session,
            [
                (legacy, sys_uuid.uuid4()),
                (legacy, sys_uuid.uuid4()),
            ],
        )
