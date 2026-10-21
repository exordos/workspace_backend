# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

from cryptography.hazmat.primitives import hashes

from cryptography.hazmat.primitives import hmac

from cryptography.hazmat.primitives.asymmetric import x25519

from cryptography.hazmat.primitives.ciphers import aead

KEM_ID = 0x0020

KDF_ID = 0x0001

AEAD_ID = 0x0002

_KEM_SUITE_ID = b"KEM" + KEM_ID.to_bytes(2, "big")

_HPKE_SUITE_ID = (
    b"HPKE"
    + KEM_ID.to_bytes(2, "big")
    + KDF_ID.to_bytes(2, "big")
    + AEAD_ID.to_bytes(2, "big")
)

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

def seal(
    recipient_public_key: bytes,
    plaintext: bytes,
    associated_data: bytes,
    info: bytes,
) -> tuple[bytes, bytes]:
    """Seal one RFC 9180 base-mode message with the existing AES-256-GCM suite."""
    recipient_key = x25519.X25519PublicKey.from_public_bytes(recipient_public_key)
    ephemeral_key = x25519.X25519PrivateKey.generate()
    encapsulated_key = ephemeral_key.public_key().public_bytes_raw()
    dh = ephemeral_key.exchange(recipient_key)
    eae_prk = _labeled_extract(_KEM_SUITE_ID, b"", b"eae_prk", dh)
    shared_secret = _labeled_expand(
        _KEM_SUITE_ID,
        eae_prk,
        b"shared_secret",
        encapsulated_key + recipient_public_key,
        32,
    )
    psk_id_hash = _labeled_extract(_HPKE_SUITE_ID, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(_HPKE_SUITE_ID, b"", b"info_hash", info)
    key_schedule_context = b"\0" + psk_id_hash + info_hash
    secret = _labeled_extract(_HPKE_SUITE_ID, shared_secret, b"secret", b"")
    key = _labeled_expand(_HPKE_SUITE_ID, secret, b"key", key_schedule_context, 32)
    nonce = _labeled_expand(
        _HPKE_SUITE_ID, secret, b"base_nonce", key_schedule_context, 12
    )
    ciphertext = aead.AESGCM(key).encrypt(nonce, plaintext, associated_data)
    return encapsulated_key, ciphertext
