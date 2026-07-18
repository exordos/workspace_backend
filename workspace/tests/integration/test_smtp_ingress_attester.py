# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import importlib.util
import pathlib
import uuid as sys_uuid

import psycopg
import psycopg.rows

from workspace.messenger_migration import writer_gate
from workspace.tests.integration import conftest as integration_conftest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "exordos/images/workspace-smtp-ingress-attester.py"
SPEC = importlib.util.spec_from_file_location("smtp_ingress_attester", SCRIPT)
smtp_ingress_attester = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smtp_ingress_attester)


def test_real_mail_side_client_heartbeats_and_acks_exact_gate(_database):
    project_uuid = sys_uuid.uuid4()
    instance_id = f"smtp_ingress:integration:{sys_uuid.uuid4()}"
    client = smtp_ingress_attester.PostgreSQLGateClient(
        integration_conftest.TEST_DB_URL,
        instance_id,
    )
    gate_uuid = None

    try:
        assert client.heartbeat() == ()
        with psycopg.connect(
            integration_conftest.TEST_DB_URL,
            row_factory=psycopg.rows.dict_row,
        ) as connection:
            gate_uuid = writer_gate.close_gate(connection, project_uuid)

        assert client.heartbeat()[0]["gate_id"] == gate_uuid
        client.acknowledge(project_uuid, gate_uuid)

        with psycopg.connect(
            integration_conftest.TEST_DB_URL,
            row_factory=psycopg.rows.dict_row,
        ) as connection:
            ack = connection.execute(
                """
                SELECT "instance_id" FROM "m_messenger_writer_gate_acks_v1"
                WHERE "gate_uuid" = %s AND "writer_class" = 'smtp_ingress'
                """,
                (gate_uuid,),
            ).fetchone()
            assert ack == {"instance_id": instance_id}
            writer_gate.release_gate(connection, project_uuid, gate_uuid)

        assert client.exact_gate_is_released(gate_uuid) is True
    finally:
        with psycopg.connect(integration_conftest.TEST_DB_URL) as connection:
            connection.execute(
                'DELETE FROM "m_messenger_writer_gates_v1" WHERE "project_id" = %s',
                (project_uuid,),
            )
            connection.execute(
                """
                DELETE FROM "m_messenger_writer_instances_v1"
                WHERE "writer_class" = 'smtp_ingress' AND "instance_id" = %s
                """,
                (instance_id,),
            )
