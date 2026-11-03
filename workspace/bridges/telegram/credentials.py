# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Account-bound encryption for Telegram sessions and short-lived login data."""

import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives.ciphers import aead


def encrypt(account_uuid: object, value: dict, key: str) -> dict:
    if not key:
        raise ValueError("Telegram credential encryption is not configured")
    nonce = secrets.token_bytes(12)
    encrypted = aead.AESGCM(hashlib.sha256(key.encode()).digest()).encrypt(
        nonce, json.dumps(value).encode(), str(account_uuid).encode("ascii")
    )
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted).decode("ascii"),
    }


def decrypt(account_uuid: object, value: dict, key: str) -> dict:
    if not key:
        raise ValueError("Telegram credential encryption is not configured")
    plaintext = aead.AESGCM(hashlib.sha256(key.encode()).digest()).decrypt(
        base64.b64decode(value["nonce"], validate=True),
        base64.b64decode(value["ciphertext"], validate=True),
        str(account_uuid).encode("ascii"),
    )
    return json.loads(plaintext)
