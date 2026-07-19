# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import dataclasses
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import ssl
import tempfile
import threading
import typing
import uuid as sys_uuid
from collections.abc import Callable
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import types as asymmetric_types
from cryptography.x509 import oid


HMAC_KEY_CONTEXT = b"workspace-bridge-enrollment-v1\0"
HMAC_MESSAGE_CONTEXT = b"workspace-external-bridge-control-ca-v1\0"
IDENTITY_URI_PREFIX = (
    "https://schemas.genesis-corporation.ru/workspace/external-bridge/v1"
)
MAX_CA_BYTES = 1024 * 1024
CA_VALIDITY = datetime.timedelta(days=5 * 365)
LEAF_VALIDITY = datetime.timedelta(days=30)
RENEWAL_WINDOW = datetime.timedelta(days=7)
LEAF_OVERLAP = datetime.timedelta(hours=24)
CA_OVERLAP = datetime.timedelta(days=30)
CERTIFICATE_PROFILE_REVISION = 2
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


class PersistentStoreError(RuntimeError):
    """The persistent trust store is unsafe, partial, or realm-mismatched."""


class EnrollmentError(RuntimeError):
    """Enrollment or renewal cannot be completed."""


class EnrollmentAuthenticationError(EnrollmentError):
    pass


class EnrollmentValidationError(EnrollmentError):
    pass


class EnrollmentNotFoundError(EnrollmentError):
    pass


class EnrollmentConflictError(EnrollmentError):
    pass


class IdentityError(RuntimeError):
    """The presented client certificate is not a current bridge identity."""


@dataclasses.dataclass(frozen=True)
class BridgeIdentity:
    realm_uuid: sys_uuid.UUID
    provider_kind: str
    bridge_instance_uuid: sys_uuid.UUID
    identity_generation: int
    uri_san: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "realm_uuid": str(self.realm_uuid),
            "provider_kind": self.provider_kind,
            "bridge_instance_uuid": str(self.bridge_instance_uuid),
            "identity_generation": self.identity_generation,
            "uri_san": self.uri_san,
        }


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _atomic_write(path: pathlib.Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _private_pem(key: asymmetric_types.PrivateKeyTypes) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _certificate_pem(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _subject_key_identifier(
    public_key: ec.EllipticCurvePublicKey,
) -> x509.SubjectKeyIdentifier:
    return x509.SubjectKeyIdentifier.from_public_key(public_key)


def _certificate_public_key(
    certificate: x509.Certificate,
) -> ec.EllipticCurvePublicKey:
    public_key = certificate.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise PersistentStoreError("Control PKI certificate key must use EC")
    return public_key


def _elliptic_curve_private_key(
    key: asymmetric_types.PrivateKeyTypes,
) -> ec.EllipticCurvePrivateKey:
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise PersistentStoreError("Control PKI private key must use EC")
    return key


def _authority_key_identifier(
    ca_certificate: x509.Certificate,
) -> x509.AuthorityKeyIdentifier:
    try:
        subject_key_identifier = ca_certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
    except x509.ExtensionNotFound:
        return x509.AuthorityKeyIdentifier.from_issuer_public_key(
            _certificate_public_key(ca_certificate)
        )
    return x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
        subject_key_identifier
    )


def _certificate_has_key_identifiers(
    certificate: x509.Certificate,
    issuer_certificate: x509.Certificate,
) -> bool:
    try:
        subject_key_identifier = certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
        authority_key_identifier = certificate.extensions.get_extension_for_class(
            x509.AuthorityKeyIdentifier
        ).value
    except x509.ExtensionNotFound:
        return False
    expected_subject = _subject_key_identifier(_certificate_public_key(certificate))
    expected_authority = _subject_key_identifier(
        _certificate_public_key(issuer_certificate)
    )
    return (
        subject_key_identifier.digest == expected_subject.digest
        and authority_key_identifier.key_identifier == expected_authority.digest
    )


def _sign_ca_certificate(
    ca_key: ec.EllipticCurvePrivateKey,
    subject: x509.Name,
    serial_number: int,
    not_valid_before: datetime.datetime,
    not_valid_after: datetime.datetime,
) -> x509.Certificate:
    subject_key_identifier = _subject_key_identifier(ca_key.public_key())
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(serial_number)
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(subject_key_identifier, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                subject_key_identifier
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA384())
    )


def _derive_verifier(token: str) -> str:
    return hashlib.sha256(HMAC_KEY_CONTEXT + token.encode("utf-8")).hexdigest()


def _identity_uri(
    realm_uuid: sys_uuid.UUID,
    provider_kind: str,
    bridge_instance_uuid: sys_uuid.UUID,
    generation: int,
) -> str:
    return (
        f"{IDENTITY_URI_PREFIX}/realms/{realm_uuid}/providers/{provider_kind}"
        f"/instances/{bridge_instance_uuid}/generations/{generation}"
    )


def parse_identity_uri(uri: str) -> BridgeIdentity:
    prefix = re.escape(IDENTITY_URI_PREFIX)
    match = re.fullmatch(
        prefix
        + r"/realms/([0-9a-f-]{36})/providers/([a-z][a-z0-9_]*)"
        + r"/instances/([0-9a-f-]{36})/generations/([1-9][0-9]*)",
        uri,
    )
    if match is None:
        raise IdentityError("Bridge certificate URI SAN is invalid")
    realm_uuid = sys_uuid.UUID(match.group(1))
    provider_kind = match.group(2)
    bridge_instance_uuid = sys_uuid.UUID(match.group(3))
    generation = int(match.group(4))
    if uri != _identity_uri(
        realm_uuid,
        provider_kind,
        bridge_instance_uuid,
        generation,
    ):
        raise IdentityError("Bridge certificate URI SAN is not canonical")
    return BridgeIdentity(
        realm_uuid=realm_uuid,
        provider_kind=provider_kind,
        bridge_instance_uuid=bridge_instance_uuid,
        identity_generation=generation,
        uri_san=uri,
    )


class PersistentControlPki:
    """Realm-bound PKI and enrollment state on the dedicated secrets disk."""

    METADATA_NAME = "metadata.json"
    ENROLLMENTS_NAME = "enrollments.json"
    CA_KEY_NAME = "ca.key"
    CA_CERT_NAME = "ca.crt"
    SERVER_KEY_NAME = "server.key"
    SERVER_CERT_NAME = "server.crt"
    TRUST_BUNDLE_NAME = "trust-bundle.crt"
    PREVIOUS_CA_NAME = "previous-ca.crt"

    def __init__(
        self,
        root: str | os.PathLike[str],
        realm_uuid: str | sys_uuid.UUID,
        hostname: str,
    ) -> None:
        self.root = pathlib.Path(root)
        self.realm_uuid = sys_uuid.UUID(str(realm_uuid))
        self.hostname = hostname
        self._lock = threading.RLock()
        if _HOSTNAME_RE.fullmatch(hostname) is None:
            raise PersistentStoreError("Control hostname must be an RFC 1123 DNS name")

    @property
    def ca_path(self) -> pathlib.Path:
        return self.root / self.CA_CERT_NAME

    @property
    def server_key_path(self) -> pathlib.Path:
        return self.root / self.SERVER_KEY_NAME

    @property
    def server_certificate_path(self) -> pathlib.Path:
        return self.root / self.SERVER_CERT_NAME

    def initialize(self) -> None:
        """Create an empty store once, or strictly validate an existing store."""
        with self._lock:
            if not self.root.exists():
                self.root.mkdir(mode=0o700, parents=True)
                self._create_store()
                return
            self._validate_root()
            members = {path.name for path in self.root.iterdir()}
            expected = {
                self.METADATA_NAME,
                self.ENROLLMENTS_NAME,
                self.CA_KEY_NAME,
                self.CA_CERT_NAME,
                self.SERVER_KEY_NAME,
                self.SERVER_CERT_NAME,
                self.TRUST_BUNDLE_NAME,
            }
            if self.PREVIOUS_CA_NAME in members:
                expected.add(self.PREVIOUS_CA_NAME)
            if not members:
                self._create_store()
                return
            if members != expected:
                raise PersistentStoreError(
                    "Control PKI store is partial or has unknown files"
                )
            self._validate_store()

    def _validate_root(self) -> None:
        state = self.root.lstat()
        if self.root.is_symlink() or not self.root.is_dir():
            raise PersistentStoreError("Control PKI store must be a real directory")
        if state.st_mode & 0o077:
            raise PersistentStoreError("Control PKI store must have mode 0700")

    def _create_store(self) -> None:
        self._validate_root()
        now = _utcnow()
        ca_key = ec.generate_private_key(ec.SECP384R1())
        ca_name = x509.Name(
            [
                x509.NameAttribute(
                    oid.NameOID.COMMON_NAME, f"Workspace control {self.realm_uuid}"
                )
            ]
        )
        ca_certificate = _sign_ca_certificate(
            ca_key,
            ca_name,
            x509.random_serial_number(),
            now - datetime.timedelta(minutes=5),
            now + CA_VALIDITY,
        )
        server_key = ec.generate_private_key(ec.SECP384R1())
        server_certificate = self._sign_server_certificate(
            ca_key,
            ca_certificate,
            server_key.public_key(),
            now,
        )
        _atomic_write(self.root / self.CA_KEY_NAME, _private_pem(ca_key), 0o600)
        _atomic_write(self.ca_path, _certificate_pem(ca_certificate), 0o644)
        _atomic_write(self.server_key_path, _private_pem(server_key), 0o600)
        _atomic_write(
            self.server_certificate_path,
            _certificate_pem(server_certificate),
            0o644,
        )
        _atomic_write(
            self.root / self.TRUST_BUNDLE_NAME,
            _certificate_pem(ca_certificate),
            0o644,
        )
        metadata = {
            "schema_version": 2,
            "realm_uuid": str(self.realm_uuid),
            "hostname": self.hostname,
            "active_ca_generation": 1,
            "trusted_ca_generations": [1],
            "ca_overlap_ends_at": None,
            "control_hmac_key": os.urandom(32).hex(),
            "created_at": _timestamp(now),
        }
        self._write_json(self.METADATA_NAME, metadata)
        self._write_json(self.ENROLLMENTS_NAME, {"schema_version": 1, "items": {}})
        self._validate_store()

    def _sign_server_certificate(
        self,
        ca_key: ec.EllipticCurvePrivateKey,
        ca_certificate: x509.Certificate,
        public_key: ec.EllipticCurvePublicKey,
        now: datetime.datetime,
    ) -> x509.Certificate:
        not_after = min(now + LEAF_VALIDITY, ca_certificate.not_valid_after_utc)
        return (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(oid.NameOID.COMMON_NAME, self.hostname)])
            )
            .issuer_name(ca_certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(self.hostname)]), False
            )
            .add_extension(
                x509.ExtendedKeyUsage([oid.ExtendedKeyUsageOID.SERVER_AUTH]), False
            )
            .add_extension(_subject_key_identifier(public_key), False)
            .add_extension(_authority_key_identifier(ca_certificate), False)
            .sign(ca_key, hashes.SHA384())
        )

    def _validate_store(self) -> None:
        self._validate_root()
        metadata = self._read_json(self.METADATA_NAME)
        if metadata != {
            "schema_version": 2,
            "realm_uuid": str(self.realm_uuid),
            "hostname": self.hostname,
            "active_ca_generation": metadata.get("active_ca_generation"),
            "trusted_ca_generations": metadata.get("trusted_ca_generations"),
            "ca_overlap_ends_at": metadata.get("ca_overlap_ends_at"),
            "control_hmac_key": metadata.get("control_hmac_key"),
            "created_at": metadata.get("created_at"),
        }:
            raise PersistentStoreError("Control PKI realm metadata does not match")
        active_generation = metadata["active_ca_generation"]
        trusted_generations = metadata["trusted_ca_generations"]
        if (
            not isinstance(active_generation, int)
            or active_generation < 1
            or trusted_generations
            not in (
                [active_generation],
                [active_generation - 1, active_generation],
            )
        ):
            raise PersistentStoreError("Control CA generations are invalid")
        try:
            hmac_key = bytes.fromhex(metadata["control_hmac_key"])
        except (TypeError, ValueError) as error:
            raise PersistentStoreError("Control HMAC key is invalid") from error
        if len(hmac_key) != 32:
            raise PersistentStoreError("Control HMAC key is invalid")
        if len(trusted_generations) == 2:
            if (
                metadata["ca_overlap_ends_at"] is None
                or not (self.root / self.PREVIOUS_CA_NAME).is_file()
            ):
                raise PersistentStoreError("Control CA overlap state is partial")
        elif (
            metadata["ca_overlap_ends_at"] is not None
            or (self.root / self.PREVIOUS_CA_NAME).exists()
        ):
            raise PersistentStoreError("Unexpected previous control CA")
        self._validate_mode(self.CA_KEY_NAME, 0o600)
        self._validate_mode(self.CA_CERT_NAME, 0o644)
        self._validate_mode(self.SERVER_KEY_NAME, 0o600)
        self._validate_mode(self.SERVER_CERT_NAME, 0o644)
        self._validate_mode(self.TRUST_BUNDLE_NAME, 0o644)
        if len(trusted_generations) == 2:
            self._validate_mode(self.PREVIOUS_CA_NAME, 0o644)
        self._validate_mode(self.METADATA_NAME, 0o600)
        self._validate_mode(self.ENROLLMENTS_NAME, 0o600)
        ca_key = _elliptic_curve_private_key(
            serialization.load_pem_private_key(
                (self.root / self.CA_KEY_NAME).read_bytes(),
                password=None,
            )
        )
        ca_certificate = x509.load_pem_x509_certificate(self.ca_path.read_bytes())
        server_key = _elliptic_curve_private_key(
            serialization.load_pem_private_key(
                self.server_key_path.read_bytes(), password=None
            )
        )
        server_certificate = x509.load_pem_x509_certificate(
            self.server_certificate_path.read_bytes()
        )
        if (
            ca_key.public_key().public_numbers()
            != _certificate_public_key(ca_certificate).public_numbers()
        ):
            raise PersistentStoreError("Control CA key does not match certificate")
        if (
            server_key.public_key().public_numbers()
            != _certificate_public_key(server_certificate).public_numbers()
        ):
            raise PersistentStoreError("Control server key does not match certificate")
        names = server_certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        if (
            names != [self.hostname]
            or server_certificate.issuer != ca_certificate.subject
        ):
            raise PersistentStoreError(
                "Control server certificate identity does not match"
            )
        trust_bundle = (self.root / self.TRUST_BUNDLE_NAME).read_text(encoding="ascii")
        ssl.create_default_context(cadata=trust_bundle)
        expected_bundle = b""
        if len(trusted_generations) == 2:
            expected_bundle += (self.root / self.PREVIOUS_CA_NAME).read_bytes()
        expected_bundle += self.ca_path.read_bytes()
        if (self.root / self.TRUST_BUNDLE_NAME).read_bytes() != expected_bundle:
            raise PersistentStoreError("Control CA trust bundle is inconsistent")
        enrollments = self._read_json(self.ENROLLMENTS_NAME)
        if enrollments.get("schema_version") != 1 or not isinstance(
            enrollments.get("items"), dict
        ):
            raise PersistentStoreError("Control enrollment state is invalid")
        self._maintain_certificates(metadata, ca_certificate, server_certificate)

    def _maintain_certificates(
        self,
        metadata: dict[str, Any],
        ca_certificate: x509.Certificate,
        server_certificate: x509.Certificate,
    ) -> None:
        now = _utcnow()
        changed = False
        if not _certificate_has_key_identifiers(ca_certificate, ca_certificate):
            ca_key = serialization.load_pem_private_key(
                (self.root / self.CA_KEY_NAME).read_bytes(), password=None
            )
            ca_certificate = _sign_ca_certificate(
                typing.cast(ec.EllipticCurvePrivateKey, ca_key),
                ca_certificate.subject,
                ca_certificate.serial_number,
                ca_certificate.not_valid_before_utc,
                ca_certificate.not_valid_after_utc,
            )
            self._renew_server_leaf(ca_certificate, now)
            _atomic_write(self.ca_path, _certificate_pem(ca_certificate), 0o644)
            trust_bundle = b""
            if len(metadata["trusted_ca_generations"]) == 2:
                trust_bundle += (self.root / self.PREVIOUS_CA_NAME).read_bytes()
            trust_bundle += _certificate_pem(ca_certificate)
            _atomic_write(
                self.root / self.TRUST_BUNDLE_NAME,
                trust_bundle,
                0o644,
            )
            server_certificate = x509.load_pem_x509_certificate(
                self.server_certificate_path.read_bytes()
            )
        if (
            metadata["ca_overlap_ends_at"] is not None
            and _parse_time(metadata["ca_overlap_ends_at"]) <= now
        ):
            (self.root / self.PREVIOUS_CA_NAME).unlink()
            metadata["trusted_ca_generations"] = [metadata["active_ca_generation"]]
            metadata["ca_overlap_ends_at"] = None
            _atomic_write(
                self.root / self.TRUST_BUNDLE_NAME,
                self.ca_path.read_bytes(),
                0o644,
            )
            changed = True
        if (
            not _certificate_has_key_identifiers(server_certificate, ca_certificate)
            or server_certificate.not_valid_after_utc - now <= RENEWAL_WINDOW
        ):
            self._renew_server_leaf(ca_certificate, now)
        if changed:
            self._write_json(self.METADATA_NAME, metadata)

    def _renew_server_leaf(
        self,
        ca_certificate: x509.Certificate | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        now = now or _utcnow()
        ca_key = _elliptic_curve_private_key(
            serialization.load_pem_private_key(
                (self.root / self.CA_KEY_NAME).read_bytes(), password=None
            )
        )
        ca_certificate = ca_certificate or x509.load_pem_x509_certificate(
            self.ca_path.read_bytes()
        )
        server_key = ec.generate_private_key(ec.SECP384R1())
        server_certificate = self._sign_server_certificate(
            ca_key, ca_certificate, server_key.public_key(), now
        )
        _atomic_write(self.server_key_path, _private_pem(server_key), 0o600)
        _atomic_write(
            self.server_certificate_path,
            _certificate_pem(server_certificate),
            0o644,
        )

    def rotate_ca(self, now: datetime.datetime | None = None) -> dict[str, object]:
        """Begin explicit versioned CA rotation with a 30-day dual-trust window."""
        now = now or _utcnow()
        with self._lock:
            self._validate_store()
            metadata = self._read_json(self.METADATA_NAME)
            if metadata["ca_overlap_ends_at"] is not None:
                raise PersistentStoreError("Control CA rotation is already active")
            old_ca = self.ca_path.read_bytes()
            _atomic_write(self.root / self.PREVIOUS_CA_NAME, old_ca, 0o644)
            ca_key = ec.generate_private_key(ec.SECP384R1())
            ca_name = x509.Name(
                [
                    x509.NameAttribute(
                        oid.NameOID.COMMON_NAME,
                        f"Workspace control {self.realm_uuid} "
                        f"g{metadata['active_ca_generation'] + 1}",
                    )
                ]
            )
            certificate = _sign_ca_certificate(
                ca_key,
                ca_name,
                x509.random_serial_number(),
                now - datetime.timedelta(minutes=5),
                now + CA_VALIDITY,
            )
            _atomic_write(self.root / self.CA_KEY_NAME, _private_pem(ca_key), 0o600)
            _atomic_write(self.ca_path, _certificate_pem(certificate), 0o644)
            _atomic_write(
                self.root / self.TRUST_BUNDLE_NAME,
                old_ca + _certificate_pem(certificate),
                0o644,
            )
            old_generation = metadata["active_ca_generation"]
            metadata["active_ca_generation"] = old_generation + 1
            metadata["trusted_ca_generations"] = [old_generation, old_generation + 1]
            metadata["ca_overlap_ends_at"] = _timestamp(now + CA_OVERLAP)
            self._write_json(self.METADATA_NAME, metadata)
            self._renew_server_leaf(certificate, now)
            self._validate_store()
            return self.ca_migration()

    def ca_migration(self, identity: BridgeIdentity | None = None) -> dict[str, object]:
        metadata = self._read_json(self.METADATA_NAME)
        renewal_required = False
        if identity is not None:
            item = self._enrollment(
                identity.bridge_instance_uuid, identity.identity_generation
            )
            renewal_required = (
                item.get("issuer_ca_generation") != metadata["active_ca_generation"]
                or item.get("certificate_profile_revision")
                != CERTIFICATE_PROFILE_REVISION
            )
        return {
            "active_ca_generations": metadata["trusted_ca_generations"],
            "renewal_required": renewal_required,
            "overlap_ends_at": metadata["ca_overlap_ends_at"],
        }

    def control_hmac_key(self) -> bytes:
        return bytes.fromhex(self._read_json(self.METADATA_NAME)["control_hmac_key"])

    def active_encryption_public_key(
        self,
        bridge_instance_uuid: str | sys_uuid.UUID,
        provider_kind: str,
        identity_generation: int,
    ) -> dict[str, object]:
        """Return the public recipient key for one exact active identity generation."""
        item = self._enrollment(bridge_instance_uuid, identity_generation)
        if item["state"] != "active" or item["provider_kind"] != provider_kind:
            raise IdentityError("Bridge encryption recipient is not active")
        public_key = item["encryption_public_key"]
        if public_key is None:
            raise IdentityError("Bridge encryption recipient is unavailable")
        return {
            "bridge_instance_uuid": str(sys_uuid.UUID(str(bridge_instance_uuid))),
            "provider_kind": provider_kind,
            "identity_generation": identity_generation,
            **public_key,
        }

    def _validate_mode(self, name: str, mode: int) -> None:
        path = self.root / name
        state = path.lstat()
        if path.is_symlink() or not path.is_file() or state.st_mode & 0o777 != mode:
            raise PersistentStoreError(f"Unsafe control PKI file: {name}")

    def _read_json(self, name: str) -> dict[str, Any]:
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise PersistentStoreError(
                f"Invalid persistent control file: {name}"
            ) from error

    def _write_json(self, name: str, value: dict[str, Any]) -> None:
        content = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        _atomic_write(self.root / name, content, 0o600)

    def register_enrollment(
        self,
        bridge_instance_uuid: str | sys_uuid.UUID,
        provider_kind: str,
        generation: int,
        enrollment_token: str,
    ) -> None:
        bridge_instance_uuid = sys_uuid.UUID(str(bridge_instance_uuid))
        if _PROVIDER_RE.fullmatch(provider_kind) is None or generation < 1:
            raise EnrollmentError("Enrollment identity is invalid")
        verifier = _derive_verifier(enrollment_token)
        key = f"{bridge_instance_uuid}:{generation}"
        with self._lock:
            state = self._read_json(self.ENROLLMENTS_NAME)
            existing = state["items"].get(key)
            wanted = {
                "bridge_instance_uuid": str(bridge_instance_uuid),
                "provider_kind": provider_kind,
                "generation": generation,
                "verifier": verifier,
                "state": "unopened",
                "encryption_public_key": None,
                "issuances": {},
                "certificate_not_after": None,
                "certificate_profile_revision": None,
            }
            if existing is not None:
                if (
                    existing["bridge_instance_uuid"] != str(bridge_instance_uuid)
                    or existing["provider_kind"] != provider_kind
                    or existing["generation"] != generation
                    or not hmac.compare_digest(existing["verifier"], verifier)
                ):
                    raise EnrollmentError("Enrollment generation already exists")
                return
            state["items"][key] = wanted
            self._write_json(self.ENROLLMENTS_NAME, state)

    def ca_bootstrap(
        self,
        nonce: str,
        hostname: str,
        bridge_instance_uuid: str | sys_uuid.UUID,
        generation: int,
    ) -> tuple[bytes, str]:
        if len(nonce) != 64 or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            raise EnrollmentValidationError("Bootstrap nonce is invalid")
        bridge_instance_uuid = sys_uuid.UUID(str(bridge_instance_uuid))
        if hostname != self.hostname or generation < 1:
            raise EnrollmentConflictError("Bootstrap identity does not match")
        item = self._enrollment(bridge_instance_uuid, generation)
        if item["state"] != "unopened":
            raise EnrollmentNotFoundError("Enrollment generation is not open")
        content = self.ca_path.read_bytes()
        if not content or len(content) > MAX_CA_BYTES:
            raise PersistentStoreError("Control CA is unavailable")
        key = bytes.fromhex(item["verifier"])
        message = (
            HMAC_MESSAGE_CONTEXT
            + nonce.encode("ascii")
            + b"\0"
            + hostname.encode("utf-8")
            + b"\0"
            + str(bridge_instance_uuid).encode("ascii")
            + b"\0"
            + str(generation).encode("ascii")
            + b"\0"
            + content
        )
        return content, hmac.new(key, message, hashlib.sha256).hexdigest()

    def enroll(
        self,
        token: str,
        request: dict[str, Any],
        before_commit: Callable[[BridgeIdentity, dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        identity = BridgeIdentity(
            realm_uuid=sys_uuid.UUID(request["realm_uuid"]),
            provider_kind=request["provider_kind"],
            bridge_instance_uuid=sys_uuid.UUID(request["bridge_instance_uuid"]),
            identity_generation=int(request["enrollment_generation"]),
            uri_san="",
        )
        identity = dataclasses.replace(
            identity,
            uri_san=_identity_uri(
                identity.realm_uuid,
                identity.provider_kind,
                identity.bridge_instance_uuid,
                identity.identity_generation,
            ),
        )
        if identity.realm_uuid != self.realm_uuid:
            raise EnrollmentError("Enrollment realm does not match")
        encryption_key = request["encryption_public_key"]
        self._validate_encryption_key(encryption_key)
        csr = self._load_csr(request["csr_pem"])
        request_uuid = str(sys_uuid.UUID(request["request_uuid"]))
        with self._lock:
            state = self._read_json(self.ENROLLMENTS_NAME)
            key = f"{identity.bridge_instance_uuid}:{identity.identity_generation}"
            item = state["items"].get(key)
            if item is None or item["provider_kind"] != identity.provider_kind:
                raise EnrollmentConflictError("Enrollment generation was not found")
            if not hmac.compare_digest(item["verifier"], _derive_verifier(token)):
                raise EnrollmentAuthenticationError("Enrollment token is invalid")
            existing = item["issuances"].get(request_uuid)
            csr_hash = hashlib.sha256(request["csr_pem"].encode("utf-8")).hexdigest()
            if existing is not None:
                if existing["csr_sha256"] != csr_hash:
                    raise EnrollmentError("Enrollment request UUID was reused")
                if item.get("encryption_public_key") != encryption_key:
                    raise EnrollmentError("Enrollment request UUID was reused")
                if before_commit is not None:
                    # The certificate issuance is persisted before the outer HTTP
                    # request transaction commits.  Replaying an idempotent request
                    # must therefore repair a DB target whose previous commit failed.
                    before_commit(identity, item["encryption_public_key"])
                return existing["response"]
            if item["state"] != "unopened":
                raise EnrollmentConflictError(
                    "Enrollment generation was already consumed"
                )
            response = self._issue_client_certificate(
                request_uuid,
                identity,
                csr.public_key(),
                overlap=False,
            )
            item["state"] = "active"
            item["encryption_public_key"] = encryption_key
            item["certificate_not_after"] = response["not_after"]
            item["issuer_ca_generation"] = response["issuer_ca_generation"]
            item["certificate_profile_revision"] = CERTIFICATE_PROFILE_REVISION
            item["current_fingerprint"] = response.pop("_fingerprint")
            item["previous_fingerprints"] = []
            item["issuances"][request_uuid] = {
                "csr_sha256": csr_hash,
                "response": response,
            }
            if before_commit is not None:
                before_commit(identity, encryption_key)
            self._write_json(self.ENROLLMENTS_NAME, state)
            return response

    def renew(
        self,
        identity: BridgeIdentity,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        csr = self._load_csr(request["csr_pem"])
        request_uuid = str(sys_uuid.UUID(request["request_uuid"]))
        with self._lock:
            state = self._read_json(self.ENROLLMENTS_NAME)
            key = f"{identity.bridge_instance_uuid}:{identity.identity_generation}"
            item = state["items"].get(key)
            if item is None or item["state"] != "active":
                raise EnrollmentError("Bridge identity is not active")
            csr_hash = hashlib.sha256(request["csr_pem"].encode("utf-8")).hexdigest()
            existing = item["issuances"].get(request_uuid)
            if existing is not None:
                if existing["csr_sha256"] != csr_hash:
                    raise EnrollmentError("Renewal request UUID was reused")
                return existing["response"]
            response = self._issue_client_certificate(
                request_uuid,
                identity,
                csr.public_key(),
                overlap=True,
            )
            current_fingerprint = item.get("current_fingerprint")
            if current_fingerprint is not None:
                item["previous_fingerprints"] = [
                    {
                        "sha256": current_fingerprint,
                        "valid_until": response["overlap_ends_at"],
                    }
                ]
            item["current_fingerprint"] = response.pop("_fingerprint")
            item["certificate_not_after"] = response["not_after"]
            item["issuer_ca_generation"] = response["issuer_ca_generation"]
            item["certificate_profile_revision"] = CERTIFICATE_PROFILE_REVISION
            item["issuances"][request_uuid] = {
                "csr_sha256": csr_hash,
                "response": response,
            }
            self._write_json(self.ENROLLMENTS_NAME, state)
            return response

    def authenticate_certificate(self, certificate_der: bytes) -> BridgeIdentity:
        if not certificate_der:
            raise IdentityError("A bridge client certificate is required")
        certificate = x509.load_der_x509_certificate(certificate_der)
        try:
            uris = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.UniformResourceIdentifier)
        except x509.ExtensionNotFound as error:
            raise IdentityError("Bridge certificate has no URI SAN") from error
        if len(uris) != 1:
            raise IdentityError("Bridge certificate must contain exactly one URI SAN")
        identity = parse_identity_uri(uris[0])
        if identity.realm_uuid != self.realm_uuid:
            raise IdentityError("Bridge certificate belongs to another realm")
        item = self._enrollment(
            identity.bridge_instance_uuid,
            identity.identity_generation,
        )
        if item["state"] != "active" or item["provider_kind"] != identity.provider_kind:
            raise IdentityError("Bridge identity is not active")
        fingerprint = certificate.fingerprint(hashes.SHA256()).hex()
        accepted = hmac.compare_digest(item.get("current_fingerprint", ""), fingerprint)
        if not accepted:
            now = _utcnow()
            accepted = any(
                hmac.compare_digest(previous["sha256"], fingerprint)
                and _parse_time(previous["valid_until"]) > now
                for previous in item.get("previous_fingerprints", [])
            )
        if not accepted:
            raise IdentityError("Bridge certificate is not a current issued leaf")
        return identity

    def _issue_client_certificate(
        self,
        request_uuid: str,
        identity: BridgeIdentity,
        public_key: Any,
        overlap: bool,
    ) -> dict[str, Any]:
        now = _utcnow()
        ca_key = serialization.load_pem_private_key(
            (self.root / self.CA_KEY_NAME).read_bytes(), password=None
        )
        ca_key = typing.cast(ec.EllipticCurvePrivateKey, ca_key)
        ca_certificate = x509.load_pem_x509_certificate(self.ca_path.read_bytes())
        not_after = min(now + LEAF_VALIDITY, ca_certificate.not_valid_after_utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([]))
            .issuer_name(ca_certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(identity.uri_san)]
                ),
                False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([oid.ExtendedKeyUsageOID.CLIENT_AUTH]), False
            )
            .add_extension(_subject_key_identifier(public_key), False)
            .add_extension(_authority_key_identifier(ca_certificate), False)
            .sign(ca_key, hashes.SHA384())
        )
        ca_pem = self.ca_path.read_text(encoding="ascii")
        metadata = self._read_json(self.METADATA_NAME)
        return {
            "request_uuid": request_uuid,
            "certificate_uuid": str(sys_uuid.uuid4()),
            "issuer_ca_generation": metadata["active_ca_generation"],
            "identity": identity.as_dict(),
            "certificate_pem": _certificate_pem(certificate).decode("ascii"),
            "ca_chain_pem": [ca_pem],
            "trust_bundle_pem": [ca_pem],
            "not_before": _timestamp(certificate.not_valid_before_utc),
            "not_after": _timestamp(certificate.not_valid_after_utc),
            "renew_after": _timestamp(certificate.not_valid_after_utc - RENEWAL_WINDOW),
            "overlap_ends_at": _timestamp(now + LEAF_OVERLAP) if overlap else None,
            "_fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
        }

    def _enrollment(
        self,
        bridge_instance_uuid: str | sys_uuid.UUID,
        generation: int,
    ) -> dict[str, Any]:
        state = self._read_json(self.ENROLLMENTS_NAME)
        item = state["items"].get(f"{bridge_instance_uuid}:{generation}")
        if item is None:
            raise EnrollmentNotFoundError("Enrollment generation was not found")
        return item

    @staticmethod
    def _validate_encryption_key(value: dict[str, str]) -> None:
        if value["algorithm"] != "X25519":
            raise EnrollmentValidationError(
                "Bridge encryption key algorithm is unsupported"
            )
        sys_uuid.UUID(value["key_uuid"])
        encoded = value["public_key"]
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=")
        except ValueError as error:
            raise EnrollmentValidationError(
                "Bridge encryption public key is invalid"
            ) from error
        if len(encoded) != 43 or len(decoded) != 32 or "=" in encoded:
            raise EnrollmentValidationError("Bridge encryption public key is invalid")

    @staticmethod
    def _load_csr(value: str) -> x509.CertificateSigningRequest:
        try:
            csr = x509.load_pem_x509_csr(value.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise EnrollmentValidationError("Bridge CSR is invalid") from error
        if not csr.is_signature_valid:
            raise EnrollmentValidationError("Bridge CSR proof of possession failed")
        return csr

    def build_server_ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.server_certificate_path, self.server_key_path)
        context.load_verify_locations(cafile=self.root / self.TRUST_BUNDLE_NAME)
        context.verify_mode = ssl.CERT_OPTIONAL
        return context
