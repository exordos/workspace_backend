#!/usr/bin/env python3
# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Rewrap credentials locally; the legacy HPKE private key never leaves its VM.

The native Core Config driver prepends UPGRADE_CONFIG and installs this file with
mode 0600. It is executed with the installed legacy bridge interpreter.
"""

import base64
import hashlib
import json
import os
import pathlib
import sys

import psycopg
from cryptography.hazmat.primitives.ciphers import aead
from psycopg import rows
from workspace_zulip_bridge import config, credentials


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main(settings):
    runtime = config.load(pathlib.Path("/etc/workspace-zulip-bridge/bridge.conf"))
    decryptor = credentials.CredentialDecryptor(
        runtime.control.credential_private_key_file,
        runtime.identity.realm_uuid,
        runtime.identity.bridge_instance_uuid,
        runtime.identity.identity_generation,
        credentials.credential_key_uuid(
            runtime.control.credential_private_key_file.parent
            / "enrollment-request.json"
        ),
    )
    cipher = aead.AESGCM(hashlib.sha256(settings["encryption_key"].encode()).digest())
    with psycopg.connect(
        settings["connection_url"], autocommit=True, row_factory=rows.dict_row
    ) as database:
        if (
            database.execute(
                "SELECT to_regclass('public.m_external_credentials_v2') AS name"
            ).fetchone()["name"]
            is None
        ):
            return
        accounts = database.execute(
            """
            SELECT account.uuid, account.owner_user_uuid, credential.envelope
            FROM m_external_accounts_v2 AS account
            JOIN m_external_credentials_v2 AS credential
              ON credential.external_account_uuid=account.uuid
            WHERE account.provider='zulip'
              AND credential.envelope #>> '{associated_data,bridge_instance_uuid}'=%s
            ORDER BY account.uuid
        """,
            (str(runtime.identity.bridge_instance_uuid),),
        ).fetchall()
        for account in accounts:
            envelope = account["envelope"]
            value = decryptor.decrypt(
                str(account["uuid"]),
                str(account["owner_user_uuid"]),
                envelope["associated_data"]["account_generation"],
                envelope,
            )
            source_hash = fingerprint(envelope)
            associated = json.dumps(
                [str(account["uuid"]), str(account["owner_user_uuid"]), source_hash],
                separators=(",", ":"),
            ).encode()
            nonce = os.urandom(12)
            content = json.dumps(
                {
                    "server_url": value.site,
                    "email": value.email,
                    "api_key": value.api_key,
                }
            ).encode()
            sealed = {
                "nonce": base64.b64encode(nonce).decode(),
                "ciphertext": base64.b64encode(
                    cipher.encrypt(nonce, content, associated)
                ).decode(),
            }
            database.execute(
                """
                INSERT INTO workspace_zulip_v5_credential_handoff
                    (account_uuid,owner_uuid,source_hash,sealed)
                VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT (account_uuid) DO UPDATE
                    SET owner_uuid=EXCLUDED.owner_uuid,source_hash=EXCLUDED.source_hash,
                        sealed=EXCLUDED.sealed,verified_at=NULL,settings_hash=NULL
            """,
                (
                    account["uuid"],
                    account["owner_user_uuid"],
                    source_hash,
                    json.dumps(sealed),
                ),
            )
    print(f"Legacy credential handoff exported {len(accounts)} accounts")


if __name__ == "__main__":
    try:
        main(globals()["UPGRADE_CONFIG"])
    except Exception as error:  # noqa: BLE001 - keep credentials out of deployment logs.
        print(
            f"Legacy credential handoff failed ({type(error).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
