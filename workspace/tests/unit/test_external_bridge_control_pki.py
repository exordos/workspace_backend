# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import hashlib
import hmac
import json
import uuid as sys_uuid

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import x25519

from workspace.external_bridge_control import pki


REALM_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
INSTANCE_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")
HOSTNAME = "workspace-bridge-control.example.test"
TOKEN = "test-enrollment-token-with-sufficient-entropy"


def _control_pki(tmp_path):
    store = pki.PersistentControlPki(tmp_path / "pki", REALM_UUID, HOSTNAME)
    store.initialize()
    store.register_enrollment(INSTANCE_UUID, "zulip", 1, TOKEN)
    return store


def _csr():
    private_key = ec.generate_private_key(ec.SECP256R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(private_key, hashes.SHA256())
    )
    return private_key, request.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _enrollment_request(csr_pem, request_uuid=None):
    public_key = x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()
    return {
        "request_uuid": str(request_uuid or sys_uuid.uuid4()),
        "enrollment_generation": 1,
        "realm_uuid": str(REALM_UUID),
        "provider_kind": "zulip",
        "bridge_instance_uuid": str(INSTANCE_UUID),
        "csr_pem": csr_pem,
        "encryption_public_key": {
            "key_uuid": str(sys_uuid.uuid4()),
            "algorithm": "X25519",
            "public_key": base64.urlsafe_b64encode(public_key)
            .rstrip(b"=")
            .decode("ascii"),
        },
    }


def _certificate_der(response):
    certificate = x509.load_pem_x509_certificate(
        response["certificate_pem"].encode("ascii")
    )
    return certificate.public_bytes(serialization.Encoding.DER)


def _assert_key_identifiers(certificate, issuer_certificate):
    subject = certificate.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier
    ).value
    authority = certificate.extensions.get_extension_for_class(
        x509.AuthorityKeyIdentifier
    ).value
    expected_subject = x509.SubjectKeyIdentifier.from_public_key(
        certificate.public_key()
    )
    expected_authority = x509.SubjectKeyIdentifier.from_public_key(
        issuer_certificate.public_key()
    )
    assert subject.digest == expected_subject.digest
    assert authority.key_identifier == expected_authority.digest


def _without_key_identifiers(certificate, signing_key):
    builder = (
        x509.CertificateBuilder()
        .subject_name(certificate.subject)
        .issuer_name(certificate.issuer)
        .public_key(certificate.public_key())
        .serial_number(certificate.serial_number)
        .not_valid_before(certificate.not_valid_before_utc)
        .not_valid_after(certificate.not_valid_after_utc)
    )
    for extension in certificate.extensions:
        if isinstance(
            extension.value,
            (x509.SubjectKeyIdentifier, x509.AuthorityKeyIdentifier),
        ):
            continue
        builder = builder.add_extension(extension.value, extension.critical)
    return builder.sign(signing_key, hashes.SHA384())


def test_control_pki_is_persistent_realm_bound_and_fail_closed(tmp_path):
    store = _control_pki(tmp_path)
    original_ca = store.ca_path.read_bytes()

    same = pki.PersistentControlPki(store.root, REALM_UUID, HOSTNAME)
    same.initialize()
    assert same.ca_path.read_bytes() == original_ca

    mismatched = pki.PersistentControlPki(
        store.root,
        sys_uuid.UUID("33333333-3333-4333-8333-333333333333"),
        HOSTNAME,
    )
    with pytest.raises(pki.PersistentStoreError):
        mismatched.initialize()

    (store.root / "partial").write_text("unsafe", encoding="utf-8")
    with pytest.raises(pki.PersistentStoreError):
        same.initialize()


def test_control_certificates_have_strict_chain_key_identifiers(tmp_path):
    store = _control_pki(tmp_path)
    ca_certificate = x509.load_pem_x509_certificate(store.ca_path.read_bytes())
    server_certificate = x509.load_pem_x509_certificate(
        store.server_certificate_path.read_bytes()
    )
    _assert_key_identifiers(ca_certificate, ca_certificate)
    _assert_key_identifiers(server_certificate, ca_certificate)
    verifier = (
        x509.verification.PolicyBuilder()
        .store(x509.verification.Store([ca_certificate]))
        .build_server_verifier(x509.DNSName(HOSTNAME))
    )
    assert verifier.verify(server_certificate, []) == [
        server_certificate,
        ca_certificate,
    ]

    _, csr_pem = _csr()
    response = store.enroll(TOKEN, _enrollment_request(csr_pem))
    client_certificate = x509.load_pem_x509_certificate(
        response["certificate_pem"].encode("ascii")
    )
    _assert_key_identifiers(client_certificate, ca_certificate)


def test_idempotent_enrollment_retry_repairs_database_target(tmp_path):
    store = _control_pki(tmp_path)
    _, csr_pem = _csr()
    request = _enrollment_request(csr_pem)
    persisted = []

    first = store.enroll(
        TOKEN,
        request,
        before_commit=lambda identity, key: persisted.append((identity, key)),
    )
    retried = store.enroll(
        TOKEN,
        request,
        before_commit=lambda identity, key: persisted.append((identity, key)),
    )

    assert retried == first
    assert len(persisted) == 2
    assert persisted[0] == persisted[1]


def test_existing_control_pki_repairs_certificate_profile_in_place(tmp_path):
    store = _control_pki(tmp_path)
    ca_key_path = store.root / store.CA_KEY_NAME
    ca_key_pem = ca_key_path.read_bytes()
    metadata = (store.root / store.METADATA_NAME).read_bytes()
    enrollments = (store.root / store.ENROLLMENTS_NAME).read_bytes()
    control_hmac_key = store.control_hmac_key()
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    ca_certificate = x509.load_pem_x509_certificate(store.ca_path.read_bytes())
    server_certificate = x509.load_pem_x509_certificate(
        store.server_certificate_path.read_bytes()
    )
    legacy_ca = _without_key_identifiers(ca_certificate, ca_key)
    legacy_server = _without_key_identifiers(server_certificate, ca_key)
    store.ca_path.write_bytes(legacy_ca.public_bytes(serialization.Encoding.PEM))
    store.server_certificate_path.write_bytes(
        legacy_server.public_bytes(serialization.Encoding.PEM)
    )
    (store.root / store.TRUST_BUNDLE_NAME).write_bytes(
        legacy_ca.public_bytes(serialization.Encoding.PEM)
    )
    legacy_verifier = (
        x509.verification.PolicyBuilder()
        .store(x509.verification.Store([legacy_ca]))
        .build_server_verifier(x509.DNSName(HOSTNAME))
    )
    with pytest.raises(x509.verification.VerificationError):
        legacy_verifier.verify(legacy_server, [])

    store.initialize()

    repaired_ca = x509.load_pem_x509_certificate(store.ca_path.read_bytes())
    repaired_server = x509.load_pem_x509_certificate(
        store.server_certificate_path.read_bytes()
    )
    assert ca_key_path.read_bytes() == ca_key_pem
    assert (store.root / store.METADATA_NAME).read_bytes() == metadata
    assert (store.root / store.ENROLLMENTS_NAME).read_bytes() == enrollments
    assert store.control_hmac_key() == control_hmac_key
    assert repaired_ca.serial_number == legacy_ca.serial_number
    assert repaired_ca.subject == legacy_ca.subject
    assert repaired_ca.not_valid_before_utc == legacy_ca.not_valid_before_utc
    assert repaired_ca.not_valid_after_utc == legacy_ca.not_valid_after_utc
    _assert_key_identifiers(repaired_ca, repaired_ca)
    _assert_key_identifiers(repaired_server, repaired_ca)
    repaired_verifier = (
        x509.verification.PolicyBuilder()
        .store(x509.verification.Store([repaired_ca]))
        .build_server_verifier(x509.DNSName(HOSTNAME))
    )
    assert repaired_verifier.verify(repaired_server, []) == [
        repaired_server,
        repaired_ca,
    ]
    assert (store.root / store.TRUST_BUNDLE_NAME).read_bytes() == (
        repaired_ca.public_bytes(serialization.Encoding.PEM)
    )
    repaired_files = {path.name: path.read_bytes() for path in store.root.iterdir()}
    store.initialize()
    assert {path.name: path.read_bytes() for path in store.root.iterdir()} == (
        repaired_files
    )


def test_legacy_client_certificate_profile_requests_one_renewal(tmp_path):
    store = _control_pki(tmp_path)
    _, csr_pem = _csr()
    response = store.enroll(TOKEN, _enrollment_request(csr_pem))
    identity = store.authenticate_certificate(_certificate_der(response))
    assert store.ca_migration(identity)["renewal_required"] is False

    state = json.loads((store.root / store.ENROLLMENTS_NAME).read_text())
    item = state["items"][f"{INSTANCE_UUID}:1"]
    item.pop("certificate_profile_revision")
    store._write_json(store.ENROLLMENTS_NAME, state)
    assert store.ca_migration(identity)["renewal_required"] is True

    _, renewed_csr = _csr()
    renewed = store.renew(
        identity,
        {"request_uuid": str(sys_uuid.uuid4()), "csr_pem": renewed_csr},
    )
    renewed_identity = store.authenticate_certificate(_certificate_der(renewed))
    assert store.ca_migration(renewed_identity)["renewal_required"] is False


def test_ca_bootstrap_uses_exact_generation_bound_hmac(tmp_path):
    store = _control_pki(tmp_path)
    nonce = "ab" * 32
    content, signature = store.ca_bootstrap(nonce, HOSTNAME, INSTANCE_UUID, 1)
    verifier = hashlib.sha256(pki.HMAC_KEY_CONTEXT + TOKEN.encode()).digest()
    expected = hmac.new(
        verifier,
        pki.HMAC_MESSAGE_CONTEXT
        + nonce.encode()
        + b"\0"
        + HOSTNAME.encode()
        + b"\0"
        + str(INSTANCE_UUID).encode()
        + b"\0"
        + b"1\0"
        + content,
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected


def test_enrollment_is_one_time_idempotent_and_der_identity_bound(tmp_path):
    store = _control_pki(tmp_path)
    _, csr_pem = _csr()
    request = _enrollment_request(csr_pem)
    response = store.enroll(TOKEN, request)

    assert store.enroll(TOKEN, request) == response
    identity = store.authenticate_certificate(_certificate_der(response))
    assert identity.realm_uuid == REALM_UUID
    assert identity.provider_kind == "zulip"
    assert identity.bridge_instance_uuid == INSTANCE_UUID
    assert identity.identity_generation == 1
    with pytest.raises(pki.EnrollmentError):
        store.ca_bootstrap("cd" * 32, HOSTNAME, INSTANCE_UUID, 1)

    _, another_csr = _csr()
    changed = dict(request, csr_pem=another_csr)
    with pytest.raises(pki.EnrollmentError):
        store.enroll(TOKEN, changed)


def test_enrollment_persistence_failure_does_not_consume_generation(tmp_path):
    store = _control_pki(tmp_path)
    _, csr_pem = _csr()
    request = _enrollment_request(csr_pem)

    def reject_persistence(identity, encryption_public_key):
        del identity, encryption_public_key
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.enroll(TOKEN, request, before_commit=reject_persistence)

    response = store.enroll(TOKEN, request)
    assert (
        store.authenticate_certificate(_certificate_der(response)).identity_generation
        == 1
    )


def test_client_renewal_allows_only_current_and_24_hour_overlap(tmp_path, monkeypatch):
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(pki, "_utcnow", lambda: now)
    store = _control_pki(tmp_path)
    _, csr_pem = _csr()
    response = store.enroll(TOKEN, _enrollment_request(csr_pem))
    old_der = _certificate_der(response)

    _, renewed_csr = _csr()
    renewed = store.renew(
        store.authenticate_certificate(old_der),
        {"request_uuid": str(sys_uuid.uuid4()), "csr_pem": renewed_csr},
    )
    new_der = _certificate_der(renewed)
    assert store.authenticate_certificate(old_der).provider_kind == "zulip"
    assert store.authenticate_certificate(new_der).provider_kind == "zulip"

    monkeypatch.setattr(
        pki,
        "_utcnow",
        lambda: now + pki.LEAF_OVERLAP + datetime.timedelta(seconds=1),
    )
    with pytest.raises(pki.IdentityError):
        store.authenticate_certificate(old_der)
    assert store.authenticate_certificate(new_der).provider_kind == "zulip"


def test_server_leaf_renews_and_explicit_ca_rotation_dual_trusts(tmp_path, monkeypatch):
    store = _control_pki(tmp_path)
    old_server = x509.load_pem_x509_certificate(
        store.server_certificate_path.read_bytes()
    )
    monkeypatch.setattr(pki, "RENEWAL_WINDOW", datetime.timedelta(days=31))
    store.initialize()
    renewed_server = x509.load_pem_x509_certificate(
        store.server_certificate_path.read_bytes()
    )
    assert renewed_server.serial_number != old_server.serial_number

    old_ca = store.ca_path.read_bytes()
    migration = store.rotate_ca()
    assert migration["active_ca_generations"] == [1, 2]
    assert migration["overlap_ends_at"] is not None
    assert (store.root / store.PREVIOUS_CA_NAME).read_bytes() == old_ca
    bundle = (store.root / store.TRUST_BUNDLE_NAME).read_bytes()
    assert bundle.startswith(old_ca)
    assert bundle.endswith(store.ca_path.read_bytes())
