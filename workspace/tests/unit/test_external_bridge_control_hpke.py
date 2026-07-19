# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import json
import uuid as sys_uuid

from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers import aead

from workspace.external_bridge_control import hpke


def _decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _decrypt(private_key, envelope):
    encapsulated = _decode(envelope["encapsulated_key"])
    recipient = private_key.public_key().public_bytes_raw()
    dh = private_key.exchange(x25519.X25519PublicKey.from_public_bytes(encapsulated))
    eae_prk = hpke._labeled_extract(hpke._KEM_SUITE_ID, b"", b"eae_prk", dh)
    shared_secret = hpke._labeled_expand(
        hpke._KEM_SUITE_ID,
        eae_prk,
        b"shared_secret",
        encapsulated + recipient,
        32,
    )
    psk_id_hash = hpke._labeled_extract(hpke._HPKE_SUITE_ID, b"", b"psk_id_hash", b"")
    info_hash = hpke._labeled_extract(hpke._HPKE_SUITE_ID, b"", b"info_hash", hpke.INFO)
    context = b"\0" + psk_id_hash + info_hash
    secret = hpke._labeled_extract(hpke._HPKE_SUITE_ID, shared_secret, b"secret", b"")
    key = hpke._labeled_expand(hpke._HPKE_SUITE_ID, secret, b"key", context, 32)
    nonce = hpke._labeled_expand(
        hpke._HPKE_SUITE_ID, secret, b"base_nonce", context, 12
    )
    plaintext = aead.AESGCM(key).decrypt(
        nonce,
        _decode(envelope["ciphertext"]),
        hpke._canonical_json(envelope["associated_data"]),
    )
    return json.loads(plaintext)


def test_backend_hpke_is_encryption_only_and_bridge_private_key_decrypts():
    private_key = x25519.X25519PrivateKey.generate()
    key_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    recipient = {
        "bridge_instance_uuid": str(instance_uuid),
        "provider_kind": "zulip",
        "identity_generation": 3,
        "key_uuid": str(key_uuid),
        "algorithm": "X25519",
        "public_key": base64.urlsafe_b64encode(
            private_key.public_key().public_bytes_raw()
        )
        .rstrip(b"=")
        .decode("ascii"),
    }
    credential = {
        "server_url": "https://zulip.example.test",
        "email": "cassi@example.test",
        "api_key": "secret-api-key",
    }
    associated_data = {
        "realm_uuid": str(sys_uuid.uuid4()),
        "provider_kind": "zulip",
        "bridge_instance_uuid": str(instance_uuid),
        "identity_generation": 3,
        "credential_key_uuid": str(key_uuid),
        "account_uuid": str(account_uuid),
        "owner_user_uuid": str(sys_uuid.uuid4()),
        "account_generation": 7,
        "schema": hpke.SCHEMA,
        "algorithm": hpke.ALGORITHM,
    }

    envelope = hpke.encrypt_zulip_credential(
        recipient,
        credential,
        associated_data,
    )

    assert envelope["schema"] == "workspace.external-credential.zulip/v1"
    assert envelope["algorithm"] == hpke.ALGORITHM
    assert set(envelope) == {
        "schema",
        "algorithm",
        "associated_data",
        "encapsulated_key",
        "ciphertext",
    }
    assert "secret-api-key" not in json.dumps(envelope)
    assert _decrypt(private_key, envelope) == credential
    assert not hasattr(hpke, "decrypt_zulip_credential")


def test_hpke_associated_data_is_mandatory_and_recipient_bound():
    private_key = x25519.X25519PrivateKey.generate()
    recipient = {
        "bridge_instance_uuid": str(sys_uuid.uuid4()),
        "provider_kind": "zulip",
        "identity_generation": 1,
        "key_uuid": str(sys_uuid.uuid4()),
        "algorithm": "X25519",
        "public_key": base64.urlsafe_b64encode(
            private_key.public_key().public_bytes_raw()
        )
        .rstrip(b"=")
        .decode("ascii"),
    }
    associated_data = {
        "realm_uuid": str(sys_uuid.uuid4()),
        "provider_kind": "zulip",
        "bridge_instance_uuid": str(sys_uuid.uuid4()),
        "identity_generation": 1,
        "credential_key_uuid": recipient["key_uuid"],
        "account_uuid": str(sys_uuid.uuid4()),
        "owner_user_uuid": str(sys_uuid.uuid4()),
        "account_generation": 1,
        "schema": hpke.SCHEMA,
        "algorithm": hpke.ALGORITHM,
    }
    try:
        hpke.encrypt_zulip_credential(
            recipient,
            {
                "server_url": "https://zulip.example.test",
                "email": "cassi@example.test",
                "api_key": "secret",
            },
            associated_data,
        )
    except ValueError as error:
        assert "bridge instance" in str(error)
    else:
        raise AssertionError("Mismatched HPKE recipient was accepted")
