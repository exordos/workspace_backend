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


def _seed_handoff(database, account_uuid, owner_uuid, key, *, corrupt=False):
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers import aead

    from workspace.upgrade import legacy_zulip

    old_envelope = {"ciphertext": "opaque-" + str(account_uuid)}
    settings = {
        "server_url": "https://chat.example.test",
        "email": "owner@example.test",
        "api_key": "synthetic-test-api-key",
    }
    database.execute(
        "INSERT INTO m_external_accounts_v2 "
        "(uuid,owner_user_uuid,provider,settings,credential_present) "
        "VALUES (%s,%s,'zulip',%s::jsonb,true)",
        (account_uuid, owner_uuid, json.dumps({"label": "Retain label"})),
    )
    database.execute(
        "INSERT INTO m_external_credentials_v2 "
        "(uuid,external_account_uuid,key_version,envelope) VALUES (%s,%s,1,%s::jsonb)",
        (uuid.uuid4(), account_uuid, json.dumps(old_envelope)),
    )
    source_hash = legacy_zulip.fingerprint(old_envelope)
    nonce = os.urandom(12)
    ciphertext = aead.AESGCM(hashlib.sha256(key.encode()).digest()).encrypt(
        nonce,
        json.dumps(settings).encode(),
        legacy_zulip.associated_data(account_uuid, owner_uuid, source_hash),
    )
    if corrupt:
        ciphertext = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
    database.execute(
        "INSERT INTO workspace_zulip_v5_credential_handoff "
        "(account_uuid,owner_uuid,source_hash,sealed) VALUES (%s,%s,%s,%s::jsonb)",
        (
            account_uuid,
            owner_uuid,
            source_hash,
            json.dumps(
                {
                    "nonce": base64.b64encode(nonce).decode(),
                    "ciphertext": base64.b64encode(ciphertext).decode(),
                }
            ),
        ),
    )
    return settings


def test_verified_handoff_survives_retirement_and_is_idempotent():
    from workspace.upgrade import legacy_zulip

    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        account_uuid, owner_uuid = uuid.uuid4(), uuid.uuid4()
        key = "synthetic-migration-wrapping-key"
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(legacy_zulip.HANDOFF_SCHEMA)
            settings = _seed_handoff(database, account_uuid, owner_uuid, key)
        assert legacy_zulip.prepare(url, key, timeout=0) == 1
        assert legacy_zulip.prepare(url, key, timeout=0) == 1
        migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR)).apply_migration(
            RETIRE_PROVIDER
        )
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT settings FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone()[0] == {"label": "Retain label", **settings}
            assert database.execute(
                "SELECT data->'settings' FROM workspace_zulip_v5_legacy_state "
                "WHERE kind='account' AND identity_key=%s",
                (str(account_uuid),),
            ).fetchone()[0] == {
                "label": "Retain label",
                **{k: v for k, v in settings.items() if k != "api_key"},
            }
        assert legacy_zulip.prepare(url, "", timeout=0) == 0


def test_corrupted_handoff_does_not_partially_change_accounts():
    from cryptography import exceptions

    from workspace.upgrade import legacy_zulip

    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        key = "synthetic-migration-wrapping-key"
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(legacy_zulip.HANDOFF_SCHEMA)
            _seed_handoff(database, uuid.uuid4(), uuid.uuid4(), key)
            _seed_handoff(database, uuid.uuid4(), uuid.uuid4(), key, corrupt=True)
        with pytest.raises(exceptions.InvalidTag):
            legacy_zulip.prepare(url, key, timeout=0)
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT count(*) FROM m_external_accounts_v2 WHERE settings ? 'api_key'"
            ).fetchone() == (0,)
            assert database.execute(
                "SELECT count(*) FROM workspace_zulip_v5_credential_handoff "
                "WHERE verified_at IS NOT NULL"
            ).fetchone() == (0,)
            assert database.execute(
                "SELECT count(*) FROM m_external_credentials_v2"
            ).fetchone() == (2,)


def test_legacy_project_scope_survives_pruned_creation_events():
    from workspace.upgrade import legacy_zulip

    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        account_uuid, owner_uuid, project_uuid = (uuid.uuid4() for _ in range(3))
        key = "synthetic-migration-wrapping-key"
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(legacy_zulip.HANDOFF_SCHEMA)
            _seed_handoff(database, account_uuid, owner_uuid, key)
            database.execute(
                "UPDATE m_external_accounts_v2 SET settings=settings || %s::jsonb "
                "WHERE uuid=%s",
                (json.dumps({"default_project_id": str(project_uuid)}), account_uuid),
            )
        legacy_zulip.prepare(url, key, timeout=0)
        migrations.MigrationEngine(str(conftest.MIGRATIONS_DIR)).apply_migration(
            "0199-Scope-external-accounts-and-Zulip-tasks-to-projects-4fb70b.py"
        )
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT project_id FROM m_external_accounts_v2 WHERE uuid=%s",
                (account_uuid,),
            ).fetchone() == (project_uuid,)


def test_replaced_sealed_handoff_is_not_marked_verified(monkeypatch):
    import base64
    import hashlib
    import os

    from cryptography.hazmat.primitives.ciphers import aead

    from workspace.upgrade import legacy_zulip

    with conftest.isolated_migration_database(LEGACY_HEAD) as url:
        account_uuid, owner_uuid = uuid.uuid4(), uuid.uuid4()
        key = "synthetic-migration-wrapping-key"
        with psycopg.connect(url, autocommit=True) as database:
            database.execute(legacy_zulip.HANDOFF_SCHEMA)
            _seed_handoff(database, account_uuid, owner_uuid, key)
        original = legacy_zulip.unseal

        def replace_handoff(record, wrapping_key):
            result = original(record, wrapping_key)
            nonce = os.urandom(12)
            ciphertext = aead.AESGCM(
                hashlib.sha256(b"synthetic-rotated-wrapping-key").digest()
            ).encrypt(
                nonce,
                json.dumps(result).encode(),
                legacy_zulip.associated_data(
                    account_uuid, owner_uuid, record["source_hash"]
                ),
            )
            with psycopg.connect(url, autocommit=True) as database:
                database.execute(
                    "UPDATE workspace_zulip_v5_credential_handoff SET sealed=%s::jsonb "
                    "WHERE account_uuid=%s",
                    (
                        json.dumps(
                            {
                                "nonce": base64.b64encode(nonce).decode(),
                                "ciphertext": base64.b64encode(ciphertext).decode(),
                            }
                        ),
                        account_uuid,
                    ),
                )
            return result

        monkeypatch.setattr(legacy_zulip, "unseal", replace_handoff)
        with pytest.raises(RuntimeError, match="changed during upgrade"):
            legacy_zulip.prepare(url, key, timeout=0)
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT count(*) FROM m_external_accounts_v2 WHERE settings ? 'api_key'"
            ).fetchone() == (0,)
            assert database.execute(
                "SELECT verified_at FROM workspace_zulip_v5_credential_handoff"
            ).fetchone() == (None,)


def test_already_v5_needs_neither_a_legacy_bridge_nor_a_handoff_key():
    import importlib.util

    from workspace.upgrade import legacy_zulip

    spec = importlib.util.spec_from_file_location(
        "workspace_upgrade_core",
        conftest.REPO_ROOT / "exordos/artifacts/upgrade/workspace-upgrade-core.py",
    )
    coordinator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(coordinator)
    with conftest.isolated_migration_database(
        "0246-Prefer-Zulip-realtime-queues-over-account-and-periodic-rescans-0060ed.py"
    ) as url:
        assert legacy_zulip.prepare(url, "", timeout=0) == 0
        assert coordinator.prepare({"connection_url": url}, {}) == 0
        with psycopg.connect(url) as database:
            assert database.execute(
                "SELECT to_regclass('public.workspace_zulip_v5_credential_handoff')"
            ).fetchone() == (None,)
