# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import json
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers import aead


KEM_ID = 0x0020
KDF_ID = 0x0001
AEAD_ID = 0x0002
ALGORITHM = "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM"
SCHEMA = "workspace.external-credential.zulip/v1"
INFO = b"workspace-external-credential-zulip-v1"
_KEM_SUITE_ID = b"KEM" + KEM_ID.to_bytes(2, "big")
_HPKE_SUITE_ID = (
    b"HPKE"
    + KEM_ID.to_bytes(2, "big")
    + KDF_ID.to_bytes(2, "big")
    + AEAD_ID.to_bytes(2, "big")
)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_b64url(value: str) -> bytes:
    if "=" in value:
        raise ValueError("HPKE public key must use unpadded base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _extract(salt: bytes, ikm: bytes) -> bytes:
    key = salt or b"\0" * 32
    digest = hmac.HMAC(key, hashes.SHA256())
    digest.update(ikm)
    return digest.finalize()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = b""
    previous = b""
    counter = 1
    while len(output) < length:
        digest = hmac.HMAC(prk, hashes.SHA256())
        digest.update(previous + info + bytes([counter]))
        previous = digest.finalize()
        output += previous
        counter += 1
    return output[:length]


def _labeled_extract(
    suite_id: bytes,
    salt: bytes,
    label: bytes,
    ikm: bytes,
) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite_id + label + ikm)


def _labeled_expand(
    suite_id: bytes,
    prk: bytes,
    label: bytes,
    info: bytes,
    length: int,
) -> bytes:
    labeled_info = length.to_bytes(2, "big") + b"HPKE-v1" + suite_id + label + info
    return _expand(prk, labeled_info, length)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encrypt_zulip_credential(
    recipient: dict[str, Any],
    credential: dict[str, str],
    associated_data: dict[str, str | int],
) -> dict[str, object]:
    """Create an RFC 9180 base-mode envelope without any backend decrypt path."""
    if set(credential) != {"server_url", "email", "api_key"} or any(
        not isinstance(value, str) for value in credential.values()
    ):
        raise ValueError("Zulip credential plaintext schema is invalid")
    required_associated_data = {
        "realm_uuid",
        "provider_kind",
        "bridge_instance_uuid",
        "identity_generation",
        "credential_key_uuid",
        "account_uuid",
        "owner_user_uuid",
        "account_generation",
        "schema",
        "algorithm",
    }
    if set(associated_data) != required_associated_data:
        raise ValueError("Credential associated data schema is invalid")
    if associated_data["bridge_instance_uuid"] != recipient["bridge_instance_uuid"]:
        raise ValueError("Credential recipient does not match bridge instance")
    if associated_data["provider_kind"] != recipient["provider_kind"]:
        raise ValueError("Credential recipient does not match provider kind")
    if associated_data["identity_generation"] != recipient["identity_generation"]:
        raise ValueError("Credential recipient identity generation does not match")
    if associated_data["credential_key_uuid"] != recipient["key_uuid"]:
        raise ValueError("Credential recipient key UUID does not match")
    if associated_data["schema"] != SCHEMA or associated_data["algorithm"] != ALGORITHM:
        raise ValueError("Credential associated data version is invalid")
    recipient_bytes = _decode_b64url(recipient["public_key"])
    if recipient["algorithm"] != "X25519" or len(recipient_bytes) != 32:
        raise ValueError("Bridge recipient public key is invalid")
    recipient_key = x25519.X25519PublicKey.from_public_bytes(recipient_bytes)
    ephemeral_key = x25519.X25519PrivateKey.generate()
    encapsulated_key = ephemeral_key.public_key().public_bytes_raw()
    dh = ephemeral_key.exchange(recipient_key)
    eae_prk = _labeled_extract(_KEM_SUITE_ID, b"", b"eae_prk", dh)
    shared_secret = _labeled_expand(
        _KEM_SUITE_ID,
        eae_prk,
        b"shared_secret",
        encapsulated_key + recipient_bytes,
        32,
    )
    psk_id_hash = _labeled_extract(_HPKE_SUITE_ID, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(_HPKE_SUITE_ID, b"", b"info_hash", INFO)
    key_schedule_context = b"\0" + psk_id_hash + info_hash
    secret = _labeled_extract(_HPKE_SUITE_ID, shared_secret, b"secret", b"")
    key = _labeled_expand(_HPKE_SUITE_ID, secret, b"key", key_schedule_context, 32)
    nonce = _labeled_expand(
        _HPKE_SUITE_ID, secret, b"base_nonce", key_schedule_context, 12
    )
    aad = _canonical_json(associated_data)
    ciphertext = aead.AESGCM(key).encrypt(nonce, _canonical_json(credential), aad)
    return {
        "schema": SCHEMA,
        "algorithm": ALGORITHM,
        "associated_data": associated_data,
        "encapsulated_key": _b64url(encapsulated_key),
        "ciphertext": _b64url(ciphertext),
    }
