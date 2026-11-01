# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import uuid

import pytest

from workspace.bridges.zulip import incoming_operations, outgoing_operations
from workspace.tests.integration import test_projection_performance as projection_tests
from workspace.tests.integration import test_zulip_bounded_lookups as bounded_tests
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming
large_history = bounded_tests.large_history


def parameters(account):
    return (
        account["project_id"],
        account["owner_user_uuid"],
        ["101", "101", "missing"],
        account["provider_uuid"],
    )


@pytest.mark.isolated_schema
@pytest.mark.parametrize("reference_kind", ["placement", "canonical", "missing"])
def test_outgoing_message_reference_does_not_scan_unrelated_history(
    large_history, reference_kind
):
    db, account, target = large_history
    canonical = db.execute(
        "SELECT message_uuid FROM messenger_message_placements WHERE uuid=%s",
        (target[3],),
    ).fetchone()[0]
    reference = {
        "placement": target[3],
        "canonical": canonical,
        "missing": uuid.uuid4(),
    }[reference_kind]
    args = (account["project_id"], account["owner_user_uuid"], reference) * 2
    assert db.execute(
        outgoing_operations.SELECT_MESSAGE_REFERENCE_SQL, args
    ).fetchall() == ([] if reference_kind == "missing" else [(canonical,)])
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
        + outgoing_operations.SELECT_MESSAGE_REFERENCE_SQL,
        args,
    ).fetchone()[0][0]
    scanned = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in projection_tests._plan_nodes(plan["Plan"])
        if node.get("Relation Name")
        in {
            "messenger_messages",
            "messenger_message_placements",
            "messenger_user_message_bindings",
        }
    )
    assert scanned < 100, plan


@pytest.mark.isolated_schema
@pytest.mark.parametrize(
    "identities", [["101"], ["101", "101", "missing"], ["missing"]]
)
def test_message_reference_lookup_does_not_scan_unrelated_history(
    large_history, identities
):
    db, account, target = large_history
    args = list(parameters(account))
    args[2] = identities
    assert db.execute(
        incoming_operations.SELECT_MESSAGE_REFERENCES_SQL, args
    ).fetchall() == ([(target[3], "101")] if "101" in identities else [])
    plan = db.execute(
        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "
        + incoming_operations.SELECT_MESSAGE_REFERENCES_SQL,
        args,
    ).fetchone()[0][0]
    scanned = sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
        * node["Actual Loops"]
        for node in projection_tests._plan_nodes(plan["Plan"])
        if node.get("Relation Name")
        in {
            "messenger_messages",
            "messenger_message_placements",
            "messenger_user_message_bindings",
            "messenger_user_message_unreads",
        }
    )
    assert scanned < 100, plan


@pytest.mark.parametrize(
    "case",
    [
        "visible",
        "hidden",
        "deleted_message",
        "deleted_topic",
        "deleted_stream",
        "inactive_membership",
        "membership_reset",
        "empty_flags",
        "other_project",
        "other_user",
        "other_provider",
    ],
)
def test_message_references_keep_the_complete_visibility_rules(incoming, case):
    _, db, account, _, _ = incoming
    incoming_tests.apply(incoming, incoming_tests.event(identifier=101))
    message, placement, topic, stream = db.execute(
        "SELECT message.uuid,placement.uuid,placement.topic_uuid,placement.stream_uuid "
        "FROM messenger_messages message JOIN messenger_message_placements placement "
        "ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid "
        "WHERE message.project_id=%s AND message.provider_identity='101'",
        (account["project_id"],),
    ).fetchone()
    args = list(parameters(account))
    if case == "hidden":
        db.execute(
            "DELETE FROM messenger_user_message_bindings "
            "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
            (account["project_id"], account["owner_user_uuid"], placement),
        )
    elif case.startswith("deleted_"):
        table, identifier = {
            "deleted_message": ("messenger_messages", message),
            "deleted_topic": ("messenger_topics", topic),
            "deleted_stream": ("messenger_streams", stream),
        }[case]
        db.execute(
            f"UPDATE {table} SET deleted_at=now() WHERE project_id=%s AND uuid=%s",
            (account["project_id"], identifier),
        )
    elif case == "inactive_membership":
        db.execute(
            "UPDATE messenger_stream_bindings SET active=false "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (account["project_id"], account["owner_user_uuid"], stream),
        )
    elif case == "membership_reset":
        db.execute(
            "UPDATE messenger_stream_bindings SET membership_generation=membership_generation+1 "
            "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
            (account["project_id"], account["owner_user_uuid"], stream),
        )
    elif case == "empty_flags":
        for table in ("unreads", "stars", "pins", "mentions"):
            db.execute(
                f"DELETE FROM messenger_user_message_{table} "
                "WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (account["project_id"], account["owner_user_uuid"], placement),
            )
    elif case in ("other_project", "other_user", "other_provider"):
        args[{"other_project": 0, "other_user": 1, "other_provider": 3}[case]] = (
            uuid.uuid4()
        )
    assert db.execute(
        incoming_operations.SELECT_MESSAGE_REFERENCES_SQL, args
    ).fetchall() == ([(placement, "101")] if case in ("visible", "empty_flags") else [])
    for identifier in (placement, message):
        reference_args = (args[0], args[1], identifier) * 2
        assert db.execute(
            outgoing_operations.SELECT_MESSAGE_REFERENCE_SQL, reference_args
        ).fetchall() == (
            [(message,)] if case in ("visible", "empty_flags", "other_provider") else []
        )
