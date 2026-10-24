# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import importlib.util
import uuid

from restalchemy.common import contexts

from workspace.bridges.zulip import incoming_operations
from workspace.messenger_api import publication
from workspace.tests.integration import conftest
from workspace.tests.integration import test_zulip_incoming as incoming_tests

incoming = incoming_tests.incoming


def test_cutover_publishes_pending_snapshots_and_discards_empty_flag_artifacts(
    incoming,
):
    api, db, account, _http, _url = incoming
    incoming_tests.apply(incoming, incoming_tests.event())
    message = api.get("/v1/messenger/messages/").json()[0]
    project, owner = account["project_id"], account["owner_user_uuid"]
    body = {
        "source_kind": "zulip.incoming",
        "entity_uuid": message["uuid"],
        "snapshot": {
            "uuid": message["uuid"],
            "payload": {"kind": "markdown", "content": "Retained"},
        },
        "recipient_payloads": {str(owner): {}},
        "recipients": [str(owner)],
        "stream_uuid": message["stream_uuid"],
        "membership_generations": {str(owner): 1},
        "control_effect": False,
    }
    insert = """INSERT INTO messenger_domain_outbox_events
        (uuid,project_id,event_kind,scope_kind,scope_key,payload)
        VALUES (%s,%s,%s,'resource',%s,%s)"""
    good, bad, counter = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.execute(
        insert,
        (
            good,
            project,
            "delivery_snapshot_event",
            str(good),
            incoming_operations.json_value({**body, "kind": "message.updated"}),
        ),
    )
    bad_body = {**body, "kind": "message.read"}
    with db.transaction():
        db.execute(
            """INSERT INTO zulip_incoming_events
            (project_id,external_account_uuid,provider_uuid,owner_user_uuid,
             account_identity,queue_id,event_id,payload,ordering_keys,processed_at)
            VALUES (%s,%s,%s,%s,%s,'cassi-empty-flags',1,%s,'{}',NOW())""",
            (
                project,
                account["uuid"],
                account["provider_uuid"],
                owner,
                account["external_account_id"],
                incoming_operations.json_value(
                    {
                        "type": "update_message_flags",
                        "messages": [],
                        "all": False,
                        "flag": "read",
                        "op": "remove",
                    }
                ),
            ),
        )
        db.execute(
            insert,
            (
                bad,
                project,
                "delivery_snapshot_event",
                str(bad),
                incoming_operations.json_value(bad_body),
            ),
        )
        db.execute(
            insert,
            (
                counter,
                project,
                "read_counters",
                str(counter),
                incoming_operations.json_value(
                    {"source_kind": "zulip.incoming", "user_uuid": str(owner)}
                ),
            ),
        )
    with contexts.Context().session_manager() as session:
        invalid_pending = publication.enqueue(
            session,
            project,
            uuid.UUID(message["uuid"]),
            [owner],
            "message.read",
            body["snapshot"],
            body["recipient_payloads"],
        )
    path = next(conftest.MIGRATIONS_DIR.glob("0269-*.py"))
    spec = importlib.util.spec_from_file_location("direct_zulip_cutover", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
        migration.migration_step.upgrade(session)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_domain_outbox_events WHERE uuid=ANY(%s)",
            ([good, bad, counter],),
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_publication_outbox WHERE uuid=%s",
            (invalid_pending[0].uuid,),
        ).fetchone()[0]
        == 0
    )
    event = db.execute(
        "SELECT payload FROM m_workspace_broadcast_message_events_v1 WHERE uuid=%s",
        (good,),
    ).fetchone()[0]
    assert event["payload"]["content"] == "Retained"
    assert db.execute(
        "SELECT membership_generation FROM messenger_event_membership_guards "
        "WHERE event_uuid=%s AND user_uuid=%s",
        (good, owner),
    ).fetchone() == (1,)
    assert (
        db.execute(
            "SELECT count(*) FROM messenger_messages WHERE project_id=%s", (project,)
        ).fetchone()[0]
        == 1
    )


def test_incoming_order_cutover_keeps_entity_fifo_and_explicit_global_barrier(incoming):
    _api, db, account, _http, _url = incoming
    for index, all_messages in enumerate((False, True), 1):
        db.execute(
            """INSERT INTO zulip_incoming_events
            (project_id,external_account_uuid,provider_uuid,owner_user_uuid,
             account_identity,queue_id,event_id,payload,ordering_keys)
            VALUES (%s,%s,%s,%s,%s,'cassi-old-order',%s,%s,%s)""",
            (
                account["project_id"],
                account["uuid"],
                account["provider_uuid"],
                account["owner_user_uuid"],
                account["external_account_id"],
                index,
                incoming_operations.json_value(
                    {
                        "type": "update_message_flags",
                        "messages": [101] if not all_messages else [],
                        "flag": "read",
                        "op": "add",
                        "all": all_messages,
                    }
                ),
                ["account:old", "stream:42", "user:7", "message:101"],
            ),
        )
    path = next(conftest.MIGRATIONS_DIR.glob("0270-*.py"))
    spec = importlib.util.spec_from_file_location("independent_zulip_cutover", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with contexts.Context().session_manager() as session:
        migration.migration_step.upgrade(session)
        migration.migration_step.upgrade(session)
    assert db.execute(
        "SELECT ordering_keys FROM zulip_incoming_events "
        "WHERE queue_id='cassi-old-order' ORDER BY event_id"
    ).fetchall() == [(["message:101"],), (["*"],)]
