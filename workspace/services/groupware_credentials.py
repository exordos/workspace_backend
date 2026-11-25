# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import secrets
import typing
import uuid

from cryptography.hazmat.primitives.ciphers import aead
from oslo_config import cfg

from workspace.common import groupware_opts


Service = typing.Literal["mail", "calendar"]
_NONCE_BYTES = 12


def configured_store() -> "CredentialStore | None":
    groupware_opts.register_opts()
    material = cfg.CONF[groupware_opts.DOMAIN].secret_encryption_key
    if material is None:
        return None
    key = bytes.fromhex(material)
    if len(key) != 32:
        raise ValueError("Groupware encryption requires a 256-bit key")
    return CredentialStore(key)


class CredentialStore:
    def __init__(self, encryption_key: bytes) -> None:
        self._cipher = aead.AESGCM(encryption_key)

    @staticmethod
    def _scope(account_uuid: uuid.UUID, service: Service) -> bytes:
        return (
            b"workspace-groupware:"
            + account_uuid.bytes
            + b":"
            + service.encode("ascii")
        )

    def save(
        self,
        session: typing.Any,
        account_uuid: uuid.UUID,
        service: Service,
        password: str,
    ) -> None:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce, password.encode("utf-8"), self._scope(account_uuid, service)
        )
        session.execute(
            """
            INSERT INTO groupware_account_credentials (
                external_account_uuid, service, encrypted_password
            ) VALUES (%s, %s, %s)
            ON CONFLICT (external_account_uuid, service) DO UPDATE
            SET encrypted_password = excluded.encrypted_password, next_sync_at = now()
            """,
            (account_uuid, service, nonce + ciphertext),
        )

    def load(
        self, session: typing.Any, account_uuid: uuid.UUID, service: Service
    ) -> str | None:
        row = session.execute(
            """
            SELECT encrypted_password FROM groupware_account_credentials
            WHERE external_account_uuid = %s AND service = %s
            """,
            (account_uuid, service),
        ).fetchone()
        if row is None:
            return None
        sealed = row["encrypted_password"]
        return self._cipher.decrypt(
            sealed[:_NONCE_BYTES],
            sealed[_NONCE_BYTES:],
            self._scope(account_uuid, service),
        ).decode("utf-8")

    def remove(self, session: typing.Any, account_uuid: uuid.UUID) -> None:
        session.execute(
            "DELETE FROM groupware_account_credentials WHERE external_account_uuid = %s",
            (account_uuid,),
        )
