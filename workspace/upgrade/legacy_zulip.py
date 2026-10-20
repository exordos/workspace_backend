# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Consume a bridge-local credential handoff before applying legacy migrations."""

import base64
import configparser
import hashlib
import json
import time

import psycopg
from cryptography.hazmat.primitives.ciphers import aead
from psycopg import rows

HANDOFF_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_zulip_v5_credential_handoff (
    account_uuid uuid PRIMARY KEY,
    owner_uuid uuid NOT NULL,
    source_hash text NOT NULL,
    sealed jsonb NOT NULL,
    verified_at timestamptz,
    settings_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def associated_data(account_uuid, owner_uuid, source_hash):
    return json.dumps(
        [str(account_uuid), str(owner_uuid), source_hash], separators=(",", ":")
    ).encode()


def unseal(record, key):
    cipher = aead.AESGCM(hashlib.sha256(key.encode()).digest())
    payload = cipher.decrypt(
        base64.b64decode(record["sealed"]["nonce"]),
        base64.b64decode(record["sealed"]["ciphertext"]),
        associated_data(
            record["account_uuid"], record["owner_uuid"], record["source_hash"]
        ),
    )
    result = json.loads(payload)
    if set(result) != {"server_url", "email", "api_key"} or not all(
        isinstance(value, str) and value for value in result.values()
    ):
        raise RuntimeError("Legacy credential handoff has an invalid payload")
    return result


def prepare(connection_url, key, *, timeout=600):
    with psycopg.connect(
        connection_url, autocommit=True, row_factory=rows.dict_row
    ) as database:
        legacy = database.execute(
            "SELECT to_regclass('public.m_external_credentials_v2') AS name"
        ).fetchone()["name"]
        if legacy is None:
            return 0
        count = database.execute(
            "SELECT count(*) AS count FROM m_external_credentials_v2 AS credential "
            "JOIN m_external_accounts_v2 AS account "
            "ON account.uuid=credential.external_account_uuid "
            "WHERE account.provider='zulip'"
        ).fetchone()["count"]
        if not count:
            return 0
        if not key:
            raise RuntimeError("Legacy credential handoff encryption key is missing")
        database.execute(HANDOFF_SCHEMA)
        deadline = time.monotonic() + timeout
        while True:
            pending = database.execute("""
                SELECT credential.external_account_uuid AS account_uuid,
                       account.owner_user_uuid AS owner_uuid, credential.envelope,
                       handoff.source_hash, handoff.sealed
                FROM m_external_credentials_v2 AS credential
                JOIN m_external_accounts_v2 AS account
                  ON account.uuid=credential.external_account_uuid
                LEFT JOIN workspace_zulip_v5_credential_handoff AS handoff
                  ON handoff.account_uuid=account.uuid
                 AND handoff.owner_uuid=account.owner_user_uuid
                WHERE account.provider='zulip'
                ORDER BY account.uuid
            """).fetchall()
            if all(
                record["sealed"] is not None
                and record["source_hash"] == fingerprint(record["envelope"])
                for record in pending
            ):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Legacy credential handoff timed out")
            time.sleep(1)
        # Validate every envelope before changing any account. A partial handoff
        # must leave all original encrypted credentials and settings intact.
        credentials = [(record, unseal(record, key)) for record in pending]
        with database.transaction():
            for record, settings in credentials:
                current = database.execute(
                    "SELECT credential.envelope, account.owner_user_uuid, "
                    "handoff.source_hash, handoff.owner_uuid, handoff.sealed "
                    "FROM m_external_credentials_v2 AS credential "
                    "JOIN m_external_accounts_v2 AS account "
                    "ON account.uuid=credential.external_account_uuid "
                    "JOIN workspace_zulip_v5_credential_handoff AS handoff "
                    "ON handoff.account_uuid=account.uuid "
                    "WHERE account.uuid=%s FOR UPDATE OF account, credential, handoff",
                    (record["account_uuid"],),
                ).fetchone()
                if (
                    current is None
                    or current["owner_user_uuid"] != record["owner_uuid"]
                    or current["owner_uuid"] != record["owner_uuid"]
                    or fingerprint(current["envelope"]) != record["source_hash"]
                    or current["source_hash"] != record["source_hash"]
                    or current["sealed"] != record["sealed"]
                ):
                    raise RuntimeError(
                        "Legacy credential handoff changed during upgrade"
                    )
                database.execute(
                    "UPDATE m_external_accounts_v2 SET settings=settings || %s::jsonb "
                    "WHERE uuid=%s",
                    (json.dumps(settings), record["account_uuid"]),
                )
                database.execute(
                    "UPDATE workspace_zulip_v5_credential_handoff "
                    "SET verified_at=now(),settings_hash=%s WHERE account_uuid=%s",
                    (fingerprint(settings), record["account_uuid"]),
                )
        return len(credentials)


def prepare_from_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.read(path)
    return prepare(
        config["db"]["connection_url"],
        config.get("legacy_upgrade", "encryption_key", fallback=""),
    )
