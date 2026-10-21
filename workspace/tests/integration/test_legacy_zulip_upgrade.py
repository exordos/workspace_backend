# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Automatic upgrades must not discard the legacy bridge's encrypted credentials."""

import json
import uuid

import psycopg
import pytest
from restalchemy.storage.sql import migrations

from workspace.tests.integration import conftest

LEGACY_HEAD = "0188-Skip-tombstoned-streams-in-binding-mirrors-fd5982.py"
RETIRE_PROVIDER = "0189-remove-external-provider-processing-6e254a.py"


def test_provider_retirement_requires_completed_credential_handoff():
    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        account_uuid, owner_uuid = uuid.uuid4(), uuid.uuid4()
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(
                "INSERT INTO m_external_accounts_v2 "
                "(uuid,owner_user_uuid,provider,settings,credential_present) "
                "VALUES (%s,%s,'zulip',%s::jsonb,true)",
                (
                    account_uuid,
                    owner_uuid,
                    json.dumps(
                        {
                            "server_url": "https://chat.example.test",
                            "email": "owner@example.test",
                            "default_project_id": str(uuid.uuid4()),
                        }
                    ),
                ),
            )
            database.execute(
                "INSERT INTO m_external_credentials_v2 "
                "(uuid,external_account_uuid,key_version,envelope) "
                "VALUES (%s,%s,1,%s::jsonb)",
                (uuid.uuid4(), account_uuid, json.dumps({"ciphertext": "opaque"})),
            )
        engine = migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR))
        with pytest.raises(RuntimeError, match="credential handoff"):
            engine.apply_migration(RETIRE_PROVIDER)
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT count(*) FROM m_external_credentials_v2 "
                "WHERE external_account_uuid=%s",
                (account_uuid,),
            ).fetchone() == (1,)
