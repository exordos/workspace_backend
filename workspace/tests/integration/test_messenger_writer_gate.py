# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import importlib.util
import os
import secrets
import threading
import uuid as sys_uuid

import psycopg
import psycopg.conninfo
import psycopg.rows
import psycopg.sql
import pytest

from workspace.messenger_migration import writer_gate
from workspace.tests.integration import conftest as integration_conftest


SMTP_GATE_ROLE = "workspace_mail_gate"


def _load_migration(filename, module_name):
    path = integration_conftest.MIGRATIONS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration.migration_step


def _public_database_privileges(connection):
    rows = connection.execute(
        """
        SELECT acl.privilege_type
        FROM pg_database AS database
        CROSS JOIN LATERAL aclexplode(
            COALESCE(database.datacl, acldefault('d', database.datdba))
        ) AS acl
        WHERE database.datname = current_database()
          AND acl.grantee = 0
        """
    ).fetchall()
    return {row[0] for row in rows}


def _set_public_database_privileges(connection, database_name, privileges):
    for privilege in ("CONNECT", "CREATE", "TEMPORARY"):
        action = "GRANT" if privilege in privileges else "REVOKE"
        recipient = "TO" if action == "GRANT" else "FROM"
        connection.execute(
            psycopg.sql.SQL(
                "{} {} ON DATABASE {} {} PUBLIC"
            ).format(
                psycopg.sql.SQL(action),
                psycopg.sql.SQL(privilege),
                psycopg.sql.Identifier(database_name),
                psycopg.sql.SQL(recipient),
            )
        )


def _admin_database_url(db):
    configured_url = os.environ.get("WORKSPACE_TEST_ADMIN_DB_URL")
    if configured_url:
        return configured_url
    can_create_role = db.execute(
        """
        SELECT rolsuper OR rolcreaterole
        FROM pg_roles
        WHERE rolname = current_user
        """
    ).fetchone()[0]
    if can_create_role:
        return integration_conftest.TEST_DB_URL
    pytest.skip(
        "WORKSPACE_TEST_ADMIN_DB_URL is required for the isolated "
        "writer-gate role privilege test"
    )


def test_smtp_gate_role_migration_grants_exact_live_database_privileges(
    _database,
    db,
):
    admin_url = _admin_database_url(db)
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL role administrator is unavailable: {exc}")

    try:
        test_cluster_id = db.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
        admin_cluster_id = admin.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
    except psycopg.Error as exc:
        admin.close()
        pytest.skip(
            "The PostgreSQL cluster identity cannot be proven for the "
            f"writer-gate privilege test: {exc}"
        )
    if admin_cluster_id != test_cluster_id:
        admin.close()
        pytest.skip(
            "WORKSPACE_TEST_ADMIN_DB_URL must identify the same isolated "
            "PostgreSQL cluster as WORKSPACE_TEST_DB_URL"
        )

    role_preexists = admin.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s)",
        (SMTP_GATE_ROLE,),
    ).fetchone()[0]
    if role_preexists:
        admin.close()
        pytest.skip(
            "An isolated PostgreSQL cluster without the workspace_mail_gate "
            "role is required for the writer-gate privilege test"
        )

    migration_0114 = _load_migration(
        "0114-grant-SMTP-ingress-writer-gate-attester-access-c990ad.py",
        "writer_gate_acl_0114",
    )
    migration_0115 = _load_migration(
        "0115-grant-SMTP-writer-gate-database-connect-c73e35.py",
        "writer_gate_acl_0115",
    )
    database_name = db.execute("SELECT current_database()").fetchone()[0]
    public_privileges = _public_database_privileges(db)
    password = secrets.token_urlsafe(32)
    role_url = psycopg.conninfo.make_conninfo(
        integration_conftest.TEST_DB_URL,
        user=SMTP_GATE_ROLE,
        password=password,
        connect_timeout=3,
    )
    role_created = False

    try:
        migration_0115.upgrade(db)
        migration_0115.downgrade(db)
        _set_public_database_privileges(db, database_name, set())
        admin.execute(
            psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                psycopg.sql.Identifier(SMTP_GATE_ROLE),
                psycopg.sql.Literal(password),
            )
        )
        role_created = True

        migration_0114.upgrade(db)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(role_url)

        migration_0115.upgrade(db)
        with psycopg.connect(role_url) as role_connection:
            database_privileges = role_connection.execute(
                """
                SELECT
                    has_database_privilege(
                        current_user, current_database(), 'CONNECT'
                    ),
                    has_database_privilege(
                        current_user, current_database(), 'CREATE'
                    ),
                    has_database_privilege(
                        current_user, current_database(), 'TEMPORARY'
                    )
                """
            ).fetchone()
            assert database_privileges == (True, False, False)

            expected_table_privileges = {
                "m_messenger_writer_gates_v1": (True, False, False, False),
                "m_messenger_writer_instances_v1": (True, True, True, False),
                "m_messenger_writer_gate_expected_v1": (
                    True,
                    False,
                    False,
                    False,
                ),
                "m_messenger_writer_gate_acks_v1": (True, True, True, False),
            }
            for table, expected in expected_table_privileges.items():
                table_oid = db.execute(
                    "SELECT %s::regclass::oid",
                    (f"public.{table}",),
                ).fetchone()[0]
                privileges = role_connection.execute(
                    """
                    SELECT
                        has_table_privilege(current_user, %s::oid, 'SELECT'),
                        has_table_privilege(current_user, %s::oid, 'INSERT'),
                        has_table_privilege(current_user, %s::oid, 'UPDATE'),
                        has_table_privilege(current_user, %s::oid, 'DELETE')
                    """,
                    (table_oid, table_oid, table_oid, table_oid),
                ).fetchone()
                assert privileges == expected

        migration_0115.downgrade(db)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(role_url)
    finally:
        migration_0115.downgrade(db)
        migration_0114.downgrade(db)
        if role_created:
            admin.execute(
                psycopg.sql.SQL("DROP ROLE {}").format(
                    psycopg.sql.Identifier(SMTP_GATE_ROLE)
                )
            )
        _set_public_database_privileges(db, database_name, public_privileges)
        admin.close()


@pytest.mark.parametrize("writer_class", ["api", "worker"])
def test_first_writer_transaction_finishes_before_concurrent_gate_close(
    _database,
    db,
    writer_class,
):
    project_uuid = sys_uuid.uuid4()
    close_started = threading.Event()
    close_finished = threading.Event()
    gate_uuids = []
    failures = []

    def close_in_another_transaction():
        try:
            with psycopg.connect(
                integration_conftest.TEST_DB_URL,
                row_factory=psycopg.rows.dict_row,
            ) as close_connection:
                close_started.set()
                gate_uuids.append(
                    writer_gate.close_gate(close_connection, project_uuid)
                )
        except Exception as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            close_finished.set()

    try:
        with psycopg.connect(
            integration_conftest.TEST_DB_URL,
            row_factory=psycopg.rows.dict_row,
        ) as writer_connection:
            writer_gate.assert_writable(
                writer_connection,
                project_uuid,
                writer_class,
            )
            close_thread = threading.Thread(target=close_in_another_transaction)
            close_thread.start()
            assert close_started.wait(timeout=2)
            assert not close_finished.wait(timeout=0.2)
            writer_connection.execute("SELECT 1")

        assert close_finished.wait(timeout=5)
        close_thread.join(timeout=5)
        assert not failures
        assert len(gate_uuids) == 1

        with psycopg.connect(
            integration_conftest.TEST_DB_URL,
            row_factory=psycopg.rows.dict_row,
        ) as next_writer_connection:
            with pytest.raises(writer_gate.WriterGateClosed):
                writer_gate.assert_writable(
                    next_writer_connection,
                    project_uuid,
                    writer_class,
                )
    finally:
        db.execute(
            'DELETE FROM "m_messenger_writer_gates_v1" WHERE "project_id" = %s',
            (project_uuid,),
        )
