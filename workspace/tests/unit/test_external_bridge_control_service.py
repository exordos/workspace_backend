# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import datetime
import json
import types
import uuid as sys_uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import x25519

from workspace.common import file_storage_opts
from workspace.external_bridge_control import files
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_service
from workspace.external_bridge_control import service
from workspace.external_bridge_control import state
from workspace.messenger_api import file_storage


REALM_UUID = sys_uuid.UUID("11111111-1111-4111-8111-111111111111")
INSTANCE_UUID = sys_uuid.UUID("22222222-2222-4222-8222-222222222222")
TOKEN = "test-enrollment-token-with-sufficient-entropy"
HOSTNAME = "workspace-bridge-control.example.test"


def _runtime(tmp_path, monkeypatch):
    conf = {
        file_storage_opts.DOMAIN: types.SimpleNamespace(
            default_type=file_storage_opts.STORAGE_TYPE_FILE,
            storage_path=str(tmp_path / "objects"),
        ),
        file_storage_opts.S3_DOMAIN: types.SimpleNamespace(),
    }
    monkeypatch.setattr(file_storage, "CONF", conf)
    control_pki = pki.PersistentControlPki(tmp_path / "pki", REALM_UUID, HOSTNAME)
    control_pki.initialize()
    control_pki.register_enrollment(INSTANCE_UUID, "zulip", 1, TOKEN)
    control_state = state.PersistentControlState(tmp_path / "state", REALM_UUID)
    control_state.initialize()
    file_manager = files.ExternalFileTransferManager(
        control_state,
        f"https://{HOSTNAME}:21443",
        control_state.signing_key(),
    )
    return (
        service.PrivateBridgeService(control_pki, control_state, file_manager),
        control_pki,
        control_state,
    )


def _enroll(
    private_service,
    *,
    instance_uuid=INSTANCE_UUID,
    token=TOKEN,
):
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, hashes.SHA256())
    )
    encryption_key = x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()
    payload = {
        "request_uuid": str(sys_uuid.uuid4()),
        "enrollment_generation": 1,
        "realm_uuid": str(REALM_UUID),
        "provider_kind": "zulip",
        "bridge_instance_uuid": str(instance_uuid),
        "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "encryption_public_key": {
            "key_uuid": str(sys_uuid.uuid4()),
            "algorithm": "X25519",
            "public_key": base64.urlsafe_b64encode(encryption_key)
            .rstrip(b"=")
            .decode("ascii"),
        },
    }
    response = private_service.handle(
        "POST",
        "/v1/enrollments",
        {"X-Workspace-Enrollment-Token": token},
        json.dumps(payload).encode(),
        None,
    )
    issuance = json.loads(response.body)
    certificate = x509.load_pem_x509_certificate(
        issuance["certificate_pem"].encode("ascii")
    )
    return response, certificate.public_bytes(serialization.Encoding.DER)


def test_absent_and_empty_query_reach_private_service_dispatch(tmp_path, monkeypatch):
    private_service, _, _ = _runtime(tmp_path, monkeypatch)

    for target in ("/v1/desired-state/changes", "/v1/desired-state/changes?"):
        response = private_service.handle("GET", target, {}, b"", None)
        problem = json.loads(response.body)

        assert response.status == 401
        assert problem["error"] == "bridge_identity_invalid"


def test_malformed_query_is_still_rejected(tmp_path, monkeypatch):
    private_service, _, _ = _runtime(tmp_path, monkeypatch)

    response = private_service.handle(
        "GET",
        "/v1/desired-state/changes?broken",
        {},
        b"",
        None,
    )
    problem = json.loads(response.body)

    assert response.status == 400
    assert problem["error"] == "invalid_request"


def test_only_enrollment_allows_missing_tls_certificate(tmp_path, monkeypatch):
    private_service, _, control_state = _runtime(tmp_path, monkeypatch)
    enrollment, certificate_der = _enroll(private_service)
    assert enrollment.status == 201
    authorized = []
    original_authorize = control_state.authorize_identity

    def authorize(identity):
        authorized.append(identity)
        return original_authorize(identity)

    monkeypatch.setattr(control_state, "authorize_identity", authorize)

    heartbeat = {
        "heartbeat_uuid": str(sys_uuid.uuid4()),
        "client_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "image_version": "test",
        "provider_kind": "zulip",
        "capabilities": {},
        "blocked_batch": None,
    }
    missing = private_service.handle(
        "PUT",
        "/v1/bridge-instances/self/heartbeat",
        {"X-Workspace-Bridge-Identity": "forged"},
        json.dumps(heartbeat).encode(),
        None,
    )
    assert missing.status == 401

    authenticated = private_service.handle(
        "PUT",
        "/v1/bridge-instances/self/heartbeat",
        {},
        json.dumps(heartbeat).encode(),
        certificate_der,
    )
    assert authenticated.status == 200
    assert (
        json.loads(authenticated.body)["heartbeat_uuid"] == heartbeat["heartbeat_uuid"]
    )
    authenticated_again = private_service.handle(
        "PUT",
        "/v1/bridge-instances/self/heartbeat",
        {},
        json.dumps({**heartbeat, "heartbeat_uuid": str(sys_uuid.uuid4())}).encode(),
        certificate_der,
    )
    assert authenticated_again.status == 200
    assert [identity.bridge_instance_uuid for identity in authorized] == [
        INSTANCE_UUID,
        INSTANCE_UUID,
    ]


def test_provider_route_reuses_authenticated_identity_and_request_session(
    tmp_path, monkeypatch
):
    private_service, _, _ = _runtime(tmp_path, monkeypatch)
    _, certificate_der = _enroll(private_service)
    request_session = object()
    calls = []

    class ProviderDataService:
        def handle(self, session, identity, method, path, query, payload):
            calls.append((session, identity, method, path, query, payload))
            return {"operations": []}

    private_service.provider_data_service = ProviderDataService()
    response = private_service.handle(
        "POST",
        f"{provider_service.API_ROOT}/operations/actions/lease",
        {},
        json.dumps({"request_uuid": str(sys_uuid.uuid4())}).encode(),
        certificate_der,
        request_session=request_session,
    )

    assert response.status == 200
    assert json.loads(response.body) == {"operations": []}
    assert calls[0][0] is request_session
    assert calls[0][1].bridge_instance_uuid == INSTANCE_UUID
    assert calls[0][1].provider_kind == "zulip"
    assert calls[0][2:5] == (
        "POST",
        f"{provider_service.API_ROOT}/operations/actions/lease",
        {},
    )


@pytest.mark.parametrize("raw_body", [b"null", b"[]", b'"text"', b"42"])
def test_provider_non_object_payload_returns_typed_invalid_request(
    tmp_path, monkeypatch, raw_body
):
    private_service, _, _ = _runtime(tmp_path, monkeypatch)
    _, certificate_der = _enroll(private_service)
    private_service.provider_data_service = provider_service.ProviderDataService(
        apply_event=lambda *_args: None,
    )

    response = private_service.handle(
        "POST",
        f"{provider_service.API_ROOT}/events",
        {},
        raw_body,
        certificate_der,
        request_session=object(),
    )

    assert response.status == 400
    assert json.loads(response.body)["error"] == "invalid_request"


def test_provider_ingress_is_bound_to_authenticated_bridge_assignment(
    tmp_path, monkeypatch
):
    private_service, control_pki, _ = _runtime(tmp_path, monkeypatch)
    other_instance_uuid = sys_uuid.uuid4()
    other_token = "other-test-enrollment-token-with-sufficient-entropy"
    control_pki.register_enrollment(
        other_instance_uuid,
        "zulip",
        1,
        other_token,
    )
    _, other_certificate_der = _enroll(
        private_service,
        instance_uuid=other_instance_uuid,
        token=other_token,
    )
    private_service.provider_data_service = provider_service.ProviderDataService(
        apply_event=lambda *_args: None,
    )

    class Result:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Session:
        def __init__(self):
            self.rows = iter(
                [
                    {
                        "status": "active",
                        "capabilities": {},
                        "last_heartbeat_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ),
                    },
                    None,
                ]
            )
            self.statements = []

        def execute(self, statement, params):
            self.statements.append((statement, params))
            if (
                "m_messenger_writer_gates_v1" in statement
                or "pg_advisory_xact_lock" in statement
            ):
                return Result(None)
            return Result(next(self.rows))

    session = Session()
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(sys_uuid.uuid4()),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
        "kind": "message.upsert",
        "payload": {"resource": {}},
    }

    with pytest.raises(provider_data.ProviderBatchError, match="not assigned"):
        private_service.handle(
            "POST",
            f"{provider_service.API_ROOT}/events",
            {},
            json.dumps({"events": [event]}).encode(),
            other_certificate_der,
            request_session=session,
        )

    provider_statements = [
        item
        for item in session.statements
        if "m_messenger_writer_gates_v1" not in item[0]
        and "pg_advisory_xact_lock" not in item[0]
    ]
    assert provider_statements[0][1][0] == other_instance_uuid
    assert provider_statements[1][1][2] == other_instance_uuid


def test_enrollment_invalid_token_is_401_and_does_not_consume_generation(
    tmp_path, monkeypatch
):
    private_service, _, _ = _runtime(tmp_path, monkeypatch)
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([]))
        .sign(key, hashes.SHA256())
    )
    encryption_key = x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()
    payload = {
        "request_uuid": str(sys_uuid.uuid4()),
        "enrollment_generation": 1,
        "realm_uuid": str(REALM_UUID),
        "provider_kind": "zulip",
        "bridge_instance_uuid": str(INSTANCE_UUID),
        "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "encryption_public_key": {
            "key_uuid": str(sys_uuid.uuid4()),
            "algorithm": "X25519",
            "public_key": base64.urlsafe_b64encode(encryption_key)
            .rstrip(b"=")
            .decode("ascii"),
        },
    }
    rejected = private_service.handle(
        "POST",
        "/v1/enrollments",
        {"X-Workspace-Enrollment-Token": "wrong"},
        json.dumps(payload).encode(),
        None,
    )
    assert rejected.status == 401

    accepted = private_service.handle(
        "POST",
        "/v1/enrollments",
        {"X-Workspace-Enrollment-Token": TOKEN},
        json.dumps(payload).encode(),
        None,
    )
    assert accepted.status == 201


def test_pruned_cursor_returns_typed_410_over_private_service(tmp_path, monkeypatch):
    private_service, _, control_state = _runtime(tmp_path, monkeypatch)
    _, certificate_der = _enroll(private_service)
    identity = pki.BridgeIdentity(
        realm_uuid=REALM_UUID,
        provider_kind="zulip",
        bridge_instance_uuid=INSTANCE_UUID,
        identity_generation=1,
        uri_san="test",
    )
    cursor = control_state.initial_cursor(identity, ("external_account",))
    old = datetime.datetime.now(datetime.timezone.utc) - state.CHANGE_RETENTION
    control_state.upsert_resource(
        identity,
        {
            "resource_type": "external_account",
            "uuid": str(sys_uuid.uuid4()),
            "generation": 1,
            "owner_user_uuid": str(sys_uuid.uuid4()),
            "settings": {
                "kind": "zulip",
                "server_url": "https://zulip.example.test",
                "selection_mode": "explicit",
                "history_depth": "30_days",
                "default_project_id": str(sys_uuid.uuid4()),
            },
            "synchronization_enabled": True,
            "credential_envelope": None,
        },
        now=old - datetime.timedelta(seconds=1),
    )
    response = private_service.handle(
        "GET",
        f"/v1/desired-state/changes?cursor={cursor}&resource_types=external_account",
        {},
        b"",
        certificate_der,
    )
    problem = json.loads(response.body)
    assert response.status == 410
    assert problem["type"] == "ControlCursorExpiredError"
    assert problem["reason"] == "retention"
