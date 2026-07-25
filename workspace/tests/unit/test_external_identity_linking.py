# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import uuid as sys_uuid

import pytest

from workspace.external_bridge_control import identity_linking


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Session:
    def __init__(self, rows=()):
        self.statements = []
        self.rows = iter(rows)

    def execute(self, statement, values):
        self.statements.append((statement, values))
        return Result(next(self.rows, ()))


def test_payload_identity_rewrites_share_one_scan_per_table():
    session = Session()
    replacements = [
        (sys_uuid.uuid4(), sys_uuid.uuid4()),
        (sys_uuid.uuid4(), sys_uuid.uuid4()),
    ]

    identity_linking._rewrite_payload_uuid_references(session, replacements)

    assert len(session.statements) == 2
    for statement, values in session.statements:
        assert statement.count("replace(") == 2
        assert "LIKE ANY(%s::text[])" in statement
        assert "LIMIT %s" in statement
        assert len(values) == 6
        assert values[0] == [
            f"%{legacy}%"
            for legacy, _canonical in sorted(
                replacements,
                key=lambda item: item[0].int,
            )
        ]
        assert values[1] == identity_linking._PAYLOAD_REWRITE_ROW_BATCH_SIZE


def test_payload_identity_rewrites_are_bounded():
    session = Session()
    replacements = [
        (sys_uuid.uuid4(), sys_uuid.uuid4())
        for _unused in range(identity_linking._PAYLOAD_REWRITE_BATCH_SIZE + 1)
    ]

    identity_linking._rewrite_payload_uuid_references(session, replacements)

    assert len(session.statements) == 4
    assert [
        statement.count("replace(") for statement, _values in session.statements
    ] == [
        identity_linking._PAYLOAD_REWRITE_BATCH_SIZE,
        identity_linking._PAYLOAD_REWRITE_BATCH_SIZE,
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


def test_payload_identity_rewrite_requests_retry_after_full_row_batch():
    session = Session(
        rows=([object()] * identity_linking._PAYLOAD_REWRITE_ROW_BATCH_SIZE,)
    )

    with pytest.raises(identity_linking.IdentityMergePending):
        identity_linking._rewrite_payload_uuid_references(
            session,
            [(sys_uuid.uuid4(), sys_uuid.uuid4())],
        )

    assert len(session.statements) == 1


def test_relational_identity_rewrite_requests_retry_after_full_row_batch():
    session = Session(
        rows=([object()] * identity_linking._REFERENCE_UPDATE_ROW_BATCH_SIZE,)
    )

    with pytest.raises(identity_linking.IdentityMergePending):
        identity_linking._update_uuid_reference_batch(
            session,
            table_name="m_workspace_events",
            column_name="user_uuid",
            legacy_user_uuid=sys_uuid.uuid4(),
            canonical_user_uuid=sys_uuid.uuid4(),
        )

    statement, values = session.statements[0]
    assert "LIMIT %s" in statement
    assert "ORDER BY ctid" not in statement
    assert values[1] == identity_linking._REFERENCE_UPDATE_ROW_BATCH_SIZE
