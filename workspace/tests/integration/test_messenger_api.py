#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""End-to-end messenger API tests against a real server + test database."""

import hashlib
import http.server
import importlib.util
import io
import itertools
import concurrent.futures
import base64
import datetime
import json
import threading
import types
import uuid as sys_uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.x509.oid import NameOID
from restalchemy.common import contexts as ra_contexts
from restalchemy.common import exceptions as ra_exceptions
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import engines as ra_engines
from restalchemy.storage.sql import migrations as ra_migrations
from restalchemy.storage.sql import sessions as ra_sessions
from oslo_config import cfg

from workspace.common import external_bridge_opts
from workspace.common import messenger_reaction_opts
from workspace.external_bridge_control import identity_linking
from workspace.external_bridge_control import provider_data
from workspace.external_bridge_control import provider_event_apply
from workspace.external_bridge_control import sql_state
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api import external_projection
from workspace.messenger_api import file_storage
from workspace.messenger_api import topic_summarization
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.api import sql_canonical_store
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api.dm import read_state
from workspace.services.messenger_workers import agents as messenger_worker_agents
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/streams/"
STREAM_BINDINGS = f"{V1}/stream_bindings/"
FOLDERS = f"{V1}/folders/"
FILES = f"{V1}/files/"
FOLDER_ITEMS = f"{V1}/folder_items/"
STREAM_TOPICS = f"{V1}/stream_topics/"
MESSAGES = f"{V1}/messages/"
DRAFTS = f"{V1}/drafts/"
MESSAGE_REACTIONS = f"{V1}/message_reactions/"
EVENTS = f"{V1}/events/"
EPOCH = f"{V1}/epoch/"
USERS = f"{V1}/users/"
EXTERNAL_ACCOUNTS = f"{V1}/external_accounts/"
EXTERNAL_OPERATIONS = f"{V1}/external_operations/"
EXTERNAL_CHATS = f"{V1}/external_chats/"
EXTERNAL_PROVIDER_POLICIES = f"{V1}/external_provider_policies/"
EXTERNAL_PROVIDER_HEALTH = f"{V1}/external_provider_health/"
TOPIC_SUMMARY_ENDPOINTS = f"{V1}/topic_summary_endpoints/"
TOPIC_SUMMARY_SETTINGS = f"{V1}/topic_summary_settings/"
EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID = "aadb67c9-c716-4066-9867-b82079c1c283"
EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_FILE = (
    "0123-deduplicate-and-revoke-external-chat-memberships-aadb67.py"
)
EXTERNAL_ACCOUNT_READ = ("workspace.external_account.read",)
EXTERNAL_ACCOUNT_CREATE = ("workspace.external_account.create",)
EXTERNAL_ACCOUNT_UPDATE = ("workspace.external_account.update",)
EXTERNAL_ACCOUNT_RECONNECT = (
    "workspace.external_account.read",
    "workspace.external_account.reconnect",
)
EXTERNAL_ACCOUNT_DISCONNECT = (
    "workspace.external_account.read",
    "workspace.external_account.disconnect",
)
EXTERNAL_ACCOUNT_DELETE = ("workspace.external_account.delete",)
TOPIC_SUMMARY_ENDPOINT_MANAGE = (topic_summarization.ENDPOINT_MANAGE_PERMISSION,)
TOPIC_SUMMARY_SETTINGS_MANAGE = (topic_summarization.SETTINGS_MANAGE_PERMISSION,)


def _run_database_operation(callback):
    with ra_contexts.Context().session_manager() as session:
        return callback(session)


def _workspace_message_read_states(db, project_id, user_uuid, message_uuids):
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, read
            FROM m_workspace_user_messages_view
            WHERE project_id = %s AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (project_id, user_uuid, message_uuids),
        )
        return {str(message_uuid): read for message_uuid, read in cursor.fetchall()}


def _set_workspace_read_mode(db, project_id, mode):
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            ) VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (project_id) DO UPDATE
            SET mode = EXCLUDED.mode, updated_at = NOW()
            """,
            (project_id, mode),
        )
    db.commit()


def _advance_read_compaction_to_phase(db, project_id, phase):
    for _iteration in range(50):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT phase
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (project_id,),
            )
            progress = cursor.fetchone()
            if progress is not None and progress[0] == phase:
                return
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                project_id,
                batch_size=10,
            )
        )
    pytest.fail(f"Compaction did not reach {phase}")


def _enable_zulip_policy(db, *, max_accounts=100):
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_provider_policies_v1 (
                uuid, provider, enabled, limits
            ) VALUES (%s, 'zulip', TRUE, %s::jsonb)
            ON CONFLICT (provider) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                emergency_suspended = FALSE,
                limits = EXCLUDED.limits
            """,
            (
                str(sys_uuid.uuid4()),
                json.dumps(
                    {
                        "max_accounts": max_accounts,
                        "max_selected_chats_per_account": 1000,
                        "max_file_bytes": 104857600,
                    }
                ),
            ),
        )
    db.commit()


def _seed_zulip_bridge_target(db):
    bridge_instance_uuid = sys_uuid.uuid4()
    key_uuid = sys_uuid.uuid4()
    private_key = x25519.X25519PrivateKey.generate()
    public_key = (
        base64.urlsafe_b64encode(private_key.public_key().public_bytes_raw())
        .rstrip(b"=")
        .decode("ascii")
    )
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_external_bridge_instances_v2 "
            "SET status = 'revoked' WHERE provider = 'zulip'"
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status
            ) VALUES (%s, 'zulip', 1, 'active')
            """,
            (str(bridge_instance_uuid),),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_control_instances_v1 (
                bridge_instance_uuid, provider_kind, identity_generation,
                encryption_key_uuid, encryption_public_key
            ) VALUES (%s, 'zulip', 1, %s, %s)
            """,
            (str(bridge_instance_uuid), str(key_uuid), public_key),
        )
    db.commit()
    return bridge_instance_uuid, key_uuid, private_key


def _ca_certificate_pem():
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Zulip CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


# --------------------------------------------------------------------------- #
# Smoke
# --------------------------------------------------------------------------- #


def test_root_endpoint_is_served(api):
    resp = api.get(f"{V1}/")
    assert resp.status_code == 200, resp.text


def test_topic_summary_endpoint_registry_and_settings_are_permissioned_and_safe(
    api,
    db,
):
    endpoint_uuid = sys_uuid.uuid4()
    endpoint_path = f"{TOPIC_SUMMARY_ENDPOINTS}{endpoint_uuid}"
    payload = {
        "uuid": str(endpoint_uuid),
        "name": "primary-compatible",
        "base_url": "https://llm.example.invalid/v1/",
        "model": "summary-model",
        "api_key": "endpoint-secret-value",
        "enabled": True,
        "priority": 20,
        "supports_vision": True,
        "supports_reasoning": True,
        "temperature": 0.4,
        "max_output_tokens": 700,
        "top_p": 0.8,
        "presence_penalty": 0.1,
        "frequency_penalty": -0.2,
    }

    denied = api.post(TOPIC_SUMMARY_ENDPOINTS, json=payload)
    assert denied.status_code == 403, denied.text

    created = api.post(
        TOPIC_SUMMARY_ENDPOINTS,
        json=payload,
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
    )
    assert created.status_code == 201, created.text
    endpoint = created.json()
    assert {key: endpoint[key] for key in payload if key != "api_key"} == {
        **{key: value for key, value in payload.items() if key != "api_key"},
        "base_url": "https://llm.example.invalid/v1",
    }
    assert "revision" not in endpoint
    assert endpoint["credential_present"] is True
    assert endpoint["failure_count"] == 0
    assert "api_key" not in created.text
    assert "claim_token" not in created.text

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT envelope FROM m_workspace_llm_endpoint_secrets "
            "WHERE endpoint_uuid = %s",
            (str(endpoint_uuid),),
        )
        envelope = cursor.fetchone()[0]
    assert "endpoint-secret-value" not in json.dumps(envelope)
    assert (
        topic_summarization.decrypt_api_key(
            endpoint_uuid,
            envelope,
            "integration-test-topic-summary-key",
        )
        == "endpoint-secret-value"
    )

    denied_list = api.get(TOPIC_SUMMARY_ENDPOINTS)
    assert denied_list.status_code == 403, denied_list.text
    listed = api.get(
        TOPIC_SUMMARY_ENDPOINTS,
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
    )
    assert listed.status_code == 200, listed.text
    assert any(item["uuid"] == str(endpoint_uuid) for item in listed.json())
    assert "api_key" not in listed.text
    assert "claim_token" not in listed.text

    invalid_generation_setting = api.put(
        endpoint_path,
        json={"top_p": 1.1},
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
    )
    assert invalid_generation_setting.status_code == 400
    updated = api.put(
        endpoint_path,
        json={
            "enabled": False,
            "priority": 10,
            "supports_vision": False,
            "api_key": "replacement-secret-value",
        },
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
    )
    assert updated.status_code == 200, updated.text
    assert "ETag" not in updated.headers
    assert "revision" not in updated.json()
    assert updated.json()["enabled"] is False
    assert "replacement-secret-value" not in updated.text

    settings_path = f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}"
    initial_settings = api.get(settings_path)
    assert initial_settings.status_code == 200, initial_settings.text
    assert initial_settings.json() == {
        "project_id": api.project_id,
        "global_enabled": False,
        "project_enabled": False,
    }
    denied_settings = api.put(
        settings_path,
        json={"global_enabled": True, "project_enabled": True},
    )
    assert denied_settings.status_code == 403, denied_settings.text
    wrong_project = api.get(f"{TOPIC_SUMMARY_SETTINGS}{sys_uuid.uuid4()}")
    assert wrong_project.status_code == 403, wrong_project.text
    enabled_settings = api.put(
        settings_path,
        json={"global_enabled": True, "project_enabled": True},
        permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
    )
    assert enabled_settings.status_code == 200, enabled_settings.text
    assert enabled_settings.json()["global_enabled"] is True
    assert enabled_settings.json()["project_enabled"] is True

    disabled_settings = api.put(
        settings_path,
        json={"global_enabled": False, "project_enabled": False},
        permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
    )
    assert disabled_settings.status_code == 200, disabled_settings.text
    deleted = api.delete(
        endpoint_path,
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
    )
    assert deleted.status_code == 204, deleted.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_llm_endpoint_secrets "
            "WHERE endpoint_uuid = %s",
            (str(endpoint_uuid),),
        )
        assert cursor.fetchone()[0] == 0


def test_topic_summary_expired_attempt_budget_does_not_block_queue(api, db):
    endpoint_uuid = sys_uuid.uuid4()
    topic_claim_token = sys_uuid.uuid4()
    endpoint_claim_token = sys_uuid.uuid4()
    try:
        endpoint_response = api.post(
            TOPIC_SUMMARY_ENDPOINTS,
            permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
            json={
                "uuid": str(endpoint_uuid),
                "name": "attempt-budget",
                "base_url": "https://llm.example.invalid/v1",
                "model": "summary-model",
                "api_key": "endpoint-secret-value",
            },
        )
        assert endpoint_response.status_code == 201, endpoint_response.text
        settings_response = api.put(
            f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}",
            permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
            json={"global_enabled": True, "project_enabled": True},
        )
        assert settings_response.status_code == 200, settings_response.text

        stream_uuid = conftest.seed_user_stream(
            db,
            api.project_id,
            api.user_uuid,
            "attempt-budget",
        )
        exhausted_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "exhausted",
        )
        ready_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "ready",
        )
        exhausted_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": exhausted_topic_uuid,
                "payload": {"kind": "markdown", "content": "Old work."},
            },
        )
        assert exhausted_message.status_code == 201, exhausted_message.text
        ready_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": ready_topic_uuid,
                "payload": {"kind": "markdown", "content": "Ready work."},
            },
        )
        assert ready_message.status_code == 201, ready_message.text

        prompt_fingerprint = topic_summarization._prompt_fingerprint(
            topic_summarization.DEFAULT_SYSTEM_PROMPT,
            None,
        )
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE m_workspace_llm_endpoints "
                "SET claim_token = %s, "
                "claim_expires_at = NOW() - INTERVAL '1 second' "
                "WHERE uuid = %s",
                (str(endpoint_claim_token), str(endpoint_uuid)),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_topic_summary_jobs (
                    topic_uuid, project_id, status, attempt,
                    boundary_message_uuid, effective_prompt,
                    prompt_fingerprint, claim_token, claim_expires_at,
                    endpoint_uuid, endpoint_claim_token,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, 'running', 3, %s, %s, %s, %s,
                    NOW() - INTERVAL '1 second', %s, %s, NOW(), NOW()
                )
                """,
                (
                    exhausted_topic_uuid,
                    api.project_id,
                    exhausted_message.json()["uuid"],
                    topic_summarization.DEFAULT_SYSTEM_PROMPT,
                    prompt_fingerprint,
                    str(topic_claim_token),
                    str(endpoint_uuid),
                    str(endpoint_claim_token),
                ),
            )
            cursor.execute(
                "UPDATE m_workspace_stream_topics "
                "SET updated_at = NOW() - INTERVAL '1 hour' "
                "WHERE uuid = %s",
                (exhausted_topic_uuid,),
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        exhausted_claim = _run_database_operation(
            lambda session: topic_summarization.claim_summary_work(
                session,
                now=now,
                key_material="integration-test-topic-summary-key",
                topic_claim_seconds=60,
                endpoint_claim_seconds=60,
            )
        )
        assert exhausted_claim is None
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempt, claim_token, claim_expires_at, "
                "endpoint_claim_token, last_error_code "
                "FROM m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
                (exhausted_topic_uuid,),
            )
            assert cursor.fetchone() == (
                "failed",
                3,
                None,
                None,
                None,
                "claim_expired",
            )
            cursor.execute(
                "SELECT claim_token, claim_expires_at "
                "FROM m_workspace_llm_endpoints WHERE uuid = %s",
                (str(endpoint_uuid),),
            )
            assert cursor.fetchone() == (None, None)

        ready_claim = _run_database_operation(
            lambda session: topic_summarization.claim_summary_work(
                session,
                now=now,
                key_material="integration-test-topic-summary-key",
                topic_claim_seconds=60,
                endpoint_claim_seconds=60,
            )
        )
        assert ready_claim is not None
        assert ready_claim.topic_uuid == sys_uuid.UUID(ready_topic_uuid)
        assert ready_claim.attempt == 1
        _run_database_operation(
            lambda session: topic_summarization.complete_summary_work(
                session,
                ready_claim,
                "Ready summary.",
                now=now,
            )
        )
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE m_workspace_topic_summary_global_settings "
                "SET enabled = FALSE WHERE singleton = TRUE"
            )
            cursor.execute(
                "DELETE FROM m_workspace_topic_summary_project_settings "
                "WHERE project_id = %s",
                (api.project_id,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_llm_endpoints WHERE uuid = %s",
                (str(endpoint_uuid),),
            )


def test_topic_summary_worker_waits_for_busy_vision_and_completes_outside_api(
    api,
    db,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    received_requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            request = {
                "path": self.path,
                "authorization": self.headers["Authorization"],
                "body": json.loads(self.rfile.read(length)),
            }
            received_requests.append(request)
            if request["body"]["model"] == "retryable-failure-model":
                status = 503
                response_payload = {"error": "temporarily unavailable"}
            else:
                status = 200
                response_payload = {
                    "choices": [{"message": {"content": "Decision: ship on Friday."}}]
                }
            body = json.dumps(response_payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            del args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    endpoint_uuids = (
        sys_uuid.uuid4(),
        sys_uuid.uuid4(),
        sys_uuid.uuid4(),
    )
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1"
        for endpoint_uuid, name, model, priority, supports_vision in (
            (endpoint_uuids[0], "vision", "vision-model", 10, True),
            (endpoint_uuids[1], "text", "text-model", 20, False),
            (
                endpoint_uuids[2],
                "retryable-failure",
                "retryable-failure-model",
                5,
                False,
            ),
        ):
            response = api.post(
                TOPIC_SUMMARY_ENDPOINTS,
                permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
                json={
                    "uuid": str(endpoint_uuid),
                    "name": name,
                    "base_url": base_url,
                    "model": model,
                    "api_key": f"{name}-secret",
                    "priority": priority,
                    "supports_vision": supports_vision,
                    "supports_reasoning": True,
                    "temperature": 0.25,
                    "max_output_tokens": 321,
                    "top_p": 0.75,
                    "presence_penalty": 0.1,
                    "frequency_penalty": -0.1,
                },
            )
            assert response.status_code == 201, response.text

        stream_uuid = conftest.seed_user_stream(
            db,
            api.project_id,
            api.user_uuid,
            "worker-summary",
        )
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "delivery",
        )
        image_response = api.post(
            FILES,
            data={"stream_uuid": stream_uuid},
            files={
                "file": (
                    "architecture.png",
                    io.BytesIO(b"\x89PNG\r\n\x1a\nsummary-image"),
                    "image/png",
                )
            },
        )
        assert image_response.status_code in (200, 201), image_response.text
        image_uuid = image_response.json()["uuid"]
        message_response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": (f"Decision: ship on Friday. urn:image:{image_uuid}"),
                },
            },
        )
        assert message_response.status_code == 201, message_response.text
        message_uuid = message_response.json()["uuid"]
        prompt_response = api.post(
            f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
            json={
                "summary_system_prompt": "Summarize decisions only.",
                "summary_reasoning_effort": "off",
            },
        )
        assert prompt_response.status_code == 200, prompt_response.text
        settings_response = api.put(
            f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}",
            permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
            json={"global_enabled": True, "project_enabled": False},
        )
        assert settings_response.status_code == 200, settings_response.text
        worker = messenger_worker_agents.MessengerWorkerAgent(
            summary_secret_key="integration-test-topic-summary-key",
            summary_request_timeout_seconds=3,
        )
        assert worker._summarize_one_topic() is False
        assert received_requests == []
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM m_workspace_topic_summary_jobs "
                "WHERE topic_uuid = %s",
                (topic_uuid,),
            )
            assert cursor.fetchone()[0] == 0

        settings_response = api.put(
            f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}",
            permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
            json={"global_enabled": True, "project_enabled": True},
        )
        assert settings_response.status_code == 200, settings_response.text

        claimed_before_disable = _run_database_operation(
            lambda session: topic_summarization.claim_summary_work(
                session,
                now=datetime.datetime.now(datetime.timezone.utc),
                key_material="integration-test-topic-summary-key",
                topic_claim_seconds=60,
                endpoint_claim_seconds=60,
            )
        )
        assert claimed_before_disable is not None
        disabled_topic = api.post(
            f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
            json={"summary_enabled": False},
        )
        assert disabled_topic.status_code == 200, disabled_topic.text
        assert disabled_topic.json()["summary_enabled"] is False
        _run_database_operation(
            lambda session: topic_summarization.complete_summary_work(
                session,
                claimed_before_disable,
                "This result must be discarded.",
                now=datetime.datetime.now(datetime.timezone.utc),
            )
        )
        assert worker._summarize_one_topic() is False
        assert received_requests == []
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM m_workspace_topic_summary_jobs "
                "WHERE topic_uuid = %s",
                (topic_uuid,),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                "SELECT claim_token FROM m_workspace_llm_endpoints WHERE uuid = %s",
                (str(claimed_before_disable.endpoint.uuid),),
            )
            assert cursor.fetchone()[0] is None
        assert api.get(f"{STREAM_TOPICS}{topic_uuid}").json()["summary"] is None
        enabled_topic = api.post(
            f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
            json={"summary_enabled": True},
        )
        assert enabled_topic.status_code == 200, enabled_topic.text
        assert enabled_topic.json()["summary_enabled"] is True

        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE m_workspace_llm_endpoints "
                "SET claim_token = %s, claim_expires_at = NOW() + INTERVAL '1 hour' "
                "WHERE uuid = %s",
                (str(sys_uuid.uuid4()), str(endpoint_uuids[0])),
            )

        assert worker._summarize_one_topic() is False
        assert received_requests == []
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, last_error_code FROM "
                "m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
                (topic_uuid,),
            )
            assert cursor.fetchone() == (
                "waiting_endpoint",
                "vision_endpoint_busy",
            )
            cursor.execute(
                "SELECT claim_token FROM m_workspace_llm_endpoints WHERE uuid = %s",
                (str(endpoint_uuids[1]),),
            )
            assert cursor.fetchone()[0] is None
            cursor.execute(
                "UPDATE m_workspace_llm_endpoints "
                "SET claim_token = NULL, claim_expires_at = NULL "
                "WHERE uuid = %s",
                (str(endpoint_uuids[0]),),
            )
            cursor.execute(
                "UPDATE m_workspace_topic_summary_jobs "
                "SET next_attempt_at = NOW() - INTERVAL '1 second' "
                "WHERE topic_uuid = %s",
                (topic_uuid,),
            )

        assert worker._summarize_one_topic() is True
        assert len(received_requests) == 1
        provider_request = received_requests[0]
        assert provider_request["path"] == "/v1/chat/completions"
        assert provider_request["authorization"] == "Bearer vision-secret"
        assert provider_request["body"]["model"] == "vision-model"
        assert provider_request["body"]["reasoning_effort"] == "none"
        assert provider_request["body"]["max_tokens"] == 321
        assert isinstance(provider_request["body"]["messages"][0]["content"], str)
        user_content = provider_request["body"]["messages"][-1]["content"]
        assert any(part["type"] == "image_url" for part in user_content)

        topic_response = api.get(f"{STREAM_TOPICS}{topic_uuid}")
        assert topic_response.status_code == 200, topic_response.text
        topic = topic_response.json()
        assert topic["summary"] == "Decision: ship on Friday."
        assert topic["summary_last_message_uuid"] == message_uuid
        assert topic["summary_has_new_messages"] is False
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempt, last_error_code "
                "FROM m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
                (topic_uuid,),
            )
            assert cursor.fetchone() == ("succeeded", 1, None)
            cursor.execute(
                "SELECT claim_token, claim_expires_at FROM "
                "m_workspace_llm_endpoints WHERE uuid = %s",
                (str(endpoint_uuids[0]),),
            )
            assert cursor.fetchone() == (None, None)

        disabled_vision = api.put(
            f"{TOPIC_SUMMARY_ENDPOINTS}{endpoint_uuids[0]}",
            permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
            json={"enabled": False},
        )
        assert disabled_vision.status_code == 200, disabled_vision.text
        second_image_response = api.post(
            FILES,
            data={"stream_uuid": stream_uuid},
            files={
                "file": (
                    "updated-architecture.png",
                    io.BytesIO(b"\x89PNG\r\n\x1a\nupdated-summary-image"),
                    "image/png",
                )
            },
        )
        assert second_image_response.status_code in (200, 201)
        second_message_response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": (
                        "Follow-up with a new image "
                        f"urn:image:{second_image_response.json()['uuid']}"
                    ),
                },
            },
        )
        assert second_message_response.status_code == 201

        assert worker._summarize_one_topic() is True
        assert len(received_requests) == 3
        failed_request = received_requests[1]
        assert failed_request["authorization"] == ("Bearer retryable-failure-secret")
        assert failed_request["body"]["model"] == "retryable-failure-model"
        text_only_request = received_requests[2]
        assert text_only_request["authorization"] == "Bearer text-secret"
        assert text_only_request["body"]["model"] == "text-model"
        assert isinstance(
            text_only_request["body"]["messages"][-1]["content"],
            str,
        )
        assert "image_url" not in json.dumps(text_only_request["body"])
        refreshed_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}").json()
        assert (
            refreshed_topic["summary_last_message_uuid"]
            == (second_message_response.json()["uuid"])
        )
        assert refreshed_topic["summary_has_new_messages"] is False
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT claim_token, failure_count, last_error_code "
                "FROM m_workspace_llm_endpoints WHERE uuid = %s",
                (str(endpoint_uuids[2]),),
            )
            assert cursor.fetchone() == (None, 1, "http_503")

        retry_topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            "retry-budget-reset",
        )
        old_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": retry_topic_uuid,
                "payload": {"kind": "markdown", "content": "Old work."},
            },
        )
        assert old_message.status_code == 201, old_message.text
        prompt_fingerprint = topic_summarization._prompt_fingerprint(
            topic_summarization.DEFAULT_SYSTEM_PROMPT,
            None,
        )
        with db.cursor() as cursor:
            cursor.execute("UPDATE m_workspace_llm_endpoints SET enabled = FALSE")
            cursor.execute(
                """
                INSERT INTO m_workspace_topic_summary_jobs (
                    topic_uuid, project_id, status, attempt,
                    boundary_message_uuid, effective_prompt,
                    prompt_fingerprint, last_error_code,
                    created_at, updated_at
                ) VALUES (%s, %s, 'failed', 3, %s, %s, %s, 'http_503', NOW(), NOW())
                """,
                (
                    retry_topic_uuid,
                    api.project_id,
                    old_message.json()["uuid"],
                    topic_summarization.DEFAULT_SYSTEM_PROMPT,
                    prompt_fingerprint,
                ),
            )
        new_message = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": retry_topic_uuid,
                "payload": {"kind": "markdown", "content": "New work."},
            },
        )
        assert new_message.status_code == 201, new_message.text

        assert worker._summarize_one_topic() is False
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempt, boundary_message_uuid "
                "FROM m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
                (retry_topic_uuid,),
            )
            assert cursor.fetchone() == (
                "waiting_endpoint",
                0,
                sys_uuid.UUID(new_message.json()["uuid"]),
            )
            cursor.execute(
                "UPDATE m_workspace_llm_endpoints SET enabled = TRUE WHERE uuid = %s",
                (str(endpoint_uuids[1]),),
            )
            cursor.execute(
                "UPDATE m_workspace_topic_summary_jobs "
                "SET next_attempt_at = NOW() - INTERVAL '1 second' "
                "WHERE topic_uuid = %s",
                (retry_topic_uuid,),
            )

        assert worker._summarize_one_topic() is True
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT status, attempt, boundary_message_uuid "
                "FROM m_workspace_topic_summary_jobs WHERE topic_uuid = %s",
                (retry_topic_uuid,),
            )
            assert cursor.fetchone() == (
                "succeeded",
                1,
                sys_uuid.UUID(new_message.json()["uuid"]),
            )
    finally:
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE m_workspace_topic_summary_global_settings "
                "SET enabled = FALSE WHERE singleton = TRUE"
            )
            cursor.execute(
                "DELETE FROM m_workspace_topic_summary_project_settings "
                "WHERE project_id = %s",
                (api.project_id,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_llm_endpoints WHERE uuid = ANY(%s)",
                (list(endpoint_uuids),),
            )


def test_zb_account_001_external_account_crud_is_owner_scoped_and_write_only(
    api,
    db,
    tmp_path,
):
    _enable_zulip_policy(db, max_accounts=1)
    del tmp_path
    realm_uuid = sys_uuid.uuid4()
    _seed_zulip_bridge_target(db)
    cfg.CONF.set_override(
        "realm_uuid",
        str(realm_uuid),
        group=external_bridge_opts.DOMAIN,
    )
    try:
        account_uuid = sys_uuid.uuid4()
        account_payload = {
            "uuid": str(account_uuid),
            "settings": {
                "kind": "zulip",
                "server_url": "https://zulip.example.invalid",
                "email": "owner@example.invalid",
                "api_key": "provider-secret",
                "selection_mode": "explicit",
                "history_depth": "30_days",
                "default_project_id": api.project_id,
            },
        }
        denied = api.post(EXTERNAL_ACCOUNTS, json=account_payload)
        assert denied.status_code == 403, denied.text

        create = api.post(
            EXTERNAL_ACCOUNTS,
            json=account_payload,
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert create.status_code == 201, create.text
        account = create.json()
        assert account["uuid"] == str(account_uuid)
        assert account["credential_present"] is True
        assert "api_key" not in account["settings"]
        assert "projection_reset_generation" not in account
        assert create.headers["ETag"] == '"1"'

        duplicate = api.post(
            EXTERNAL_ACCOUNTS,
            json={
                "uuid": str(sys_uuid.uuid4()),
                "settings": {
                    **account["settings"],
                    "api_key": "another-secret",
                },
            },
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert duplicate.status_code == 403, duplicate.text

        another_user = sys_uuid.uuid4()
        foreign_list = api.get(
            EXTERNAL_ACCOUNTS,
            user=another_user,
            permissions=EXTERNAL_ACCOUNT_READ,
        )
        assert foreign_list.status_code == 200, foreign_list.text
        assert foreign_list.json() == []
        foreign_get = api.get(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}",
            user=another_user,
            permissions=EXTERNAL_ACCOUNT_READ,
        )
        assert foreign_get.status_code == 404, foreign_get.text

        reconnect_path = f"{EXTERNAL_ACCOUNTS}{account_uuid}/actions/reconnect/invoke"
        reconnect_body = {
            "settings": {
                "kind": "zulip",
                "server_url": "https://zulip.example.invalid",
                "email": "owner@example.invalid",
                "api_key": "replacement-secret",
            }
        }
        missing_etag = api.post(
            reconnect_path,
            json=reconnect_body,
            permissions=EXTERNAL_ACCOUNT_RECONNECT,
        )
        assert missing_etag.status_code == 428, missing_etag.text
        reconnect = api.post(
            reconnect_path,
            json=reconnect_body,
            headers={"If-Match": '"1"'},
            permissions=EXTERNAL_ACCOUNT_RECONNECT,
        )
        assert reconnect.status_code == 200, reconnect.text
        assert reconnect.headers["ETag"] == '"2"'
        assert "api_key" not in reconnect.text
        assert "projection_reset_generation" not in reconnect.json()

        disconnect = api.post(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}/actions/disconnect/invoke",
            permissions=EXTERNAL_ACCOUNT_DISCONNECT,
        )
        assert disconnect.status_code == 200, disconnect.text
        assert disconnect.json()["status"] == "disconnected"
        assert "projection_reset_generation" not in disconnect.json()

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT envelope::text
                FROM m_external_credentials_v2
                WHERE external_account_uuid = %s
                """,
                (str(account_uuid),),
            )
            envelope = cursor.fetchone()[0]
            assert "provider-secret" not in envelope
            assert "replacement-secret" not in envelope
            encrypted = json.loads(envelope)
            assert set(encrypted) == {
                "schema",
                "algorithm",
                "associated_data",
                "encapsulated_key",
                "ciphertext",
            }
            assert encrypted["associated_data"] == {
                "realm_uuid": str(realm_uuid),
                "provider_kind": "zulip",
                "bridge_instance_uuid": encrypted["associated_data"][
                    "bridge_instance_uuid"
                ],
                "identity_generation": 1,
                "credential_key_uuid": encrypted["associated_data"][
                    "credential_key_uuid"
                ],
                "account_uuid": str(account_uuid),
                "owner_user_uuid": api.user_uuid,
                "account_generation": 2,
                "schema": "workspace.external-credential.zulip/v1",
                "algorithm": ("HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM"),
            }
            cursor.execute(
                """
                SELECT object_type, action, payload
                FROM m_workspace_events
                WHERE project_id = %s AND user_uuid = %s
                ORDER BY epoch_version
                """,
                (api.project_id, api.user_uuid),
            )
            events = cursor.fetchall()
        assert [row[0:2] for row in events] == [
            ("external_account", "created"),
            ("external_account", "updated"),
            ("external_account", "updated"),
        ]
        assert all("api_key" not in json.dumps(row[2]) for row in events)
        assert all(
            "projection_reset_generation" not in json.dumps(row[2])
            for row in events
        )

        deleted = api.delete(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}",
            permissions=EXTERNAL_ACCOUNT_DELETE,
        )
        assert deleted.status_code == 204, deleted.text
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM m_external_credentials_v2 "
                "WHERE external_account_uuid = %s",
                (str(account_uuid),),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT operation, generation
                FROM m_external_bridge_desired_changes_v1
                WHERE resource_type = 'external_account'
                  AND resource_uuid = %s
                ORDER BY sequence
                """,
                (str(account_uuid),),
            )
            assert cursor.fetchall() == [
                ("upsert", 1),
                ("upsert", 2),
                ("upsert", 3),
                ("delete", 4),
            ]
    finally:
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


def test_external_account_policy_keeps_one_account_per_provider(api, db):
    _enable_zulip_policy(db, max_accounts=2)
    _seed_zulip_bridge_target(db)
    cfg.CONF.set_override(
        "realm_uuid",
        str(sys_uuid.uuid4()),
        group=external_bridge_opts.DOMAIN,
    )
    account_uuids = [sys_uuid.uuid4() for _index in range(3)]

    def payload(account_uuid, index):
        return {
            "uuid": str(account_uuid),
            "settings": {
                "kind": "zulip",
                "server_url": f"https://zulip-{index}.example.invalid",
                "email": f"owner-{index}@example.invalid",
                "api_key": f"provider-secret-{index}",
                "selection_mode": "explicit",
                "history_depth": "30_days",
                "default_project_id": api.project_id,
            },
        }

    try:
        created = api.post(
            EXTERNAL_ACCOUNTS,
            json=payload(account_uuids[0], 0),
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert created.status_code == 201, created.text

        duplicate_provider = api.post(
            EXTERNAL_ACCOUNTS,
            json=payload(account_uuids[1], 1),
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert duplicate_provider.status_code == 409, duplicate_provider.text

        over_limit = api.post(
            EXTERNAL_ACCOUNTS,
            json=payload(account_uuids[2], 2),
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert over_limit.status_code == 409, over_limit.text

        account_list = api.get(
            EXTERNAL_ACCOUNTS,
            permissions=EXTERNAL_ACCOUNT_READ,
        )
        assert account_list.status_code == 200, account_list.text
        assert str(account_uuids[0]) in {row["uuid"] for row in account_list.json()}
        assert str(account_uuids[1]) not in {row["uuid"] for row in account_list.json()}

        deleted = api.delete(
            f"{EXTERNAL_ACCOUNTS}{account_uuids[0]}",
            permissions=EXTERNAL_ACCOUNT_DELETE,
        )
        assert deleted.status_code == 204, deleted.text
    finally:
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


def test_external_account_limit_serializes_concurrent_creates(api, db):
    _enable_zulip_policy(db, max_accounts=1)
    _seed_zulip_bridge_target(db)
    cfg.CONF.set_override(
        "realm_uuid",
        str(sys_uuid.uuid4()),
        group=external_bridge_opts.DOMAIN,
    )
    account_uuids = (sys_uuid.uuid4(), sys_uuid.uuid4())

    def create(index):
        return api.post(
            EXTERNAL_ACCOUNTS,
            json={
                "uuid": str(account_uuids[index]),
                "settings": {
                    "kind": "zulip",
                    "server_url": f"https://zulip-{index}.example.invalid",
                    "email": f"owner-{index}@example.invalid",
                    "api_key": f"provider-secret-{index}",
                    "selection_mode": "explicit",
                    "history_depth": "30_days",
                    "default_project_id": api.project_id,
                },
            },
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                future.result(timeout=10)
                for future in (
                    executor.submit(create, 0),
                    executor.submit(create, 1),
                )
            ]
        assert sorted(response.status_code for response in responses) == [201, 403]
        created_uuid = responses[
            next(
                index
                for index, response in enumerate(responses)
                if response.status_code == 201
            )
        ].json()["uuid"]
        deleted = api.delete(
            f"{EXTERNAL_ACCOUNTS}{created_uuid}",
            permissions=EXTERNAL_ACCOUNT_DELETE,
        )
        assert deleted.status_code == 204, deleted.text
    finally:
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


@pytest.mark.parametrize("account_history_depth", ["30_days", "all"])
def test_external_account_history_depth_requeues_selected_chat_assignments(
    api,
    db,
    account_history_depth,
):
    _enable_zulip_policy(db)
    realm_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    owner_user_uuid = sys_uuid.uuid4()
    _seed_zulip_bridge_target(db)
    cfg.CONF.set_override(
        "realm_uuid",
        str(realm_uuid),
        group=external_bridge_opts.DOMAIN,
    )
    try:
        created = api.post(
            EXTERNAL_ACCOUNTS,
            json={
                "uuid": str(account_uuid),
                "settings": {
                    "kind": "zulip",
                    "server_url": "https://zulip.example.invalid",
                    "email": "owner@example.invalid",
                    "api_key": "provider-secret",
                    "selection_mode": "explicit",
                    "history_depth": account_history_depth,
                    "default_project_id": api.project_id,
                },
            },
            user=owner_user_uuid,
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert created.status_code == 201, created.text
        chat_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        topic_uuid = sys_uuid.uuid4()
        identity_uuid = sys_uuid.uuid4()
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected, project_id,
                    history_depth, projection_stream_uuid, status
                ) VALUES (
                    %s, %s, %s, 'zulip', 'channel:42', %s::jsonb, 'Engineering',
                    TRUE, %s, '30_days', %s, 'live'
                )
                """,
                (
                    str(chat_uuid),
                    str(account_uuid),
                    str(owner_user_uuid),
                    json.dumps(
                        {
                            "chat_type": "channel",
                            "description": "",
                            "private": False,
                            "participants": [
                                {
                                    "identity_uuid": str(identity_uuid),
                                    "provider_user_id": "1",
                                    "display_name": "Owner",
                                    "is_owner": True,
                                }
                            ],
                            "topics": [
                                {
                                    "topic_uuid": str(topic_uuid),
                                    "provider_topic_id": "42:general",
                                    "name": "general",
                                    "is_default": True,
                                }
                            ],
                        }
                    ),
                    api.project_id,
                    str(stream_uuid),
                ),
            )
        db.commit()

        updated = api.put(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}",
            headers={"If-Match": '"1"'},
            json={
                "settings": {
                    "kind": "zulip",
                    "selection_mode": "explicit",
                    "history_depth": "all",
                    "default_project_id": api.project_id,
                }
            },
            user=owner_user_uuid,
            permissions=EXTERNAL_ACCOUNT_UPDATE,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["settings"]["history_depth"] == "all"
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT history_depth, status, revision
                FROM m_external_chats_v2
                WHERE uuid = %s
                """,
                (str(chat_uuid),),
            )
            assert cursor.fetchone() == ("all", "syncing", 2)
            cursor.execute(
                """
                SELECT generation, resource
                FROM m_external_bridge_desired_changes_v1
                WHERE resource_type = 'external_chat_assignment'
                  AND resource_uuid = %s
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (str(chat_uuid),),
            )
            generation, resource = cursor.fetchone()
        assert generation == 2
        assert resource["history_depth"] == "all"
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
                (str(account_uuid),),
            )
        db.commit()
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


@pytest.mark.parametrize(
    ("account_history_depth", "expected_history_depth"),
    [("all", "all"), (None, "30_days")],
)
def test_external_chat_can_be_selected_again_after_deselect(
    api,
    db,
    account_history_depth,
    expected_history_depth,
):
    _enable_zulip_policy(db)
    bridge_instance_uuid, key_uuid, _ = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    source = {
        "kind": "zulip",
        "chat_type": "channel",
        "description": "",
        "private": False,
        "participants": [
            {
                "identity_uuid": api.user_uuid,
                "provider_user_id": "1",
                "display_name": "Owner",
                "email": "owner@example.invalid",
                "avatar_urn": None,
                "role": "owner",
            }
        ],
        "topics": [
            {
                "topic_uuid": str(topic_uuid),
                "provider_topic_id": "42:general",
                "name": "general",
                "is_default": True,
            }
        ],
    }
    account_settings = {
        "kind": "zulip",
        # Each parameter case represents an independent provider realm.  Keep
        # its pre-discovery origin unique so the realm-global chat invariant
        # does not intentionally treat the two persistent fixtures as one
        # provider chat assigned to different projects.
        "server_url": f"https://{account_uuid}.zulip.example.invalid",
        "selection_mode": "explicit",
        "default_project_id": api.project_id,
    }
    if account_history_depth is not None:
        account_settings["history_depth"] = account_history_depth
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings, credential_present,
                status
            ) VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live')
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(account_settings),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_instance_uuid),
                            "credential_key_uuid": str(key_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                history_depth, projection_stream_uuid, status
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb, 'Engineering',
                FALSE, NULL, '30_days', NULL, 'deselected'
            )
            """,
            (chat_uuid, account_uuid, api.user_uuid, json.dumps(source)),
        )

    selected = api.post(
        f"{EXTERNAL_CHATS}{chat_uuid}/actions/select/invoke",
        json={"project_id": api.project_id},
    )

    assert selected.status_code == 200, selected.text
    expected_stream_uuid = sys_uuid.uuid5(
        sys_uuid.UUID("71bdfd0a-35b6-54ac-83d1-54869e3c7e67"),
        f"{chat_uuid}:stream:canonical",
    )
    assert selected.json()["projection_stream_uuid"] == str(expected_stream_uuid)
    assert selected.json()["history_depth"] == expected_history_depth
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT selected, project_id, history_depth, projection_stream_uuid,
                   status
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == (
            True,
            sys_uuid.UUID(api.project_id),
            expected_history_depth,
            expected_stream_uuid,
            "syncing",
        )
        cursor.execute(
            """
            SELECT generation, resource
            FROM m_external_bridge_desired_changes_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (chat_uuid,),
        )
        generation, resource = cursor.fetchone()
        assert generation == 2
        assert resource["history_depth"] == expected_history_depth
        cursor.execute(
            """
            SELECT COUNT(*) FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, expected_stream_uuid),
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.parametrize("scenario", ["materialize", "reuse", "repair"])
def test_provider_self_dm_selection_materializes_or_reuses_canonical_self_chat(
    api,
    db,
    scenario,
):
    _enable_zulip_policy(db)
    bridge_instance_uuid, key_uuid, _ = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    expected_stream_uuid = messenger_dm_helpers.deterministic_direct_stream_uuid(
        api.project_id,
        api.user_uuid,
        api.user_uuid,
    )
    generic_projection_stream_uuid = sql_state.external_chat_projection_stream_uuid(
        chat_uuid
    )
    legacy_message_uuid = sys_uuid.uuid4()
    legacy_file_uuid = sys_uuid.uuid4()
    native_message_uuid = sys_uuid.uuid4()
    extra_native_topic_uuid = sys_uuid.uuid4()
    unrelated_project_uuid = sys_uuid.uuid4()
    unrelated_user_uuid = sys_uuid.uuid4()
    expected_private_index = f"{api.user_uuid}:{api.user_uuid}"
    has_legacy_projection = scenario in {"reuse", "repair"}
    if has_legacy_projection:
        created = api.post(
            STREAMS,
            json={
                "name": "Personal notes",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
                "direct_user_uuid": api.user_uuid,
            },
        )
        assert created.status_code in (200, 201), created.text
        assert created.json()["uuid"] == str(expected_stream_uuid)

    source = {
        "kind": "zulip",
        "chat_type": "group",
        "description": "",
        "participants": [
            {
                "identity_uuid": api.user_uuid,
                "provider_user_id": "7",
                "display_name": "Saved messages",
                "email": "owner@example.invalid",
                "avatar_urn": None,
                "role": "owner",
            }
        ],
        "topics": [
            {
                "topic_uuid": str(topic_uuid),
                "provider_topic_id": "group_direct:7:default",
                "name": "default",
                "is_default": True,
            }
        ],
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings, credential_present,
                status
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb, TRUE, 'live'
            )
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": (
                            f"https://{account_uuid}.zulip.example.invalid"
                        ),
                        "selection_mode": "explicit",
                        "default_project_id": api.project_id,
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_instance_uuid),
                            "credential_key_uuid": str(key_uuid),
                        }
                    }
                ),
            ),
        )
        if has_legacy_projection:
            cursor.execute(
                """
                SELECT default_topic_uuid
                FROM m_workspace_streams
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, expected_stream_uuid),
            )
            native_topic_uuid = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_topics (
                    uuid, project_id, name, stream_uuid
                ) VALUES (%s, %s, 'native-only', %s)
                """,
                (
                    extra_native_topic_uuid,
                    api.project_id,
                    expected_stream_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, source_name, source
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"native before provider"}'::jsonb,
                    'native', '{"kind":"native"}'::jsonb
                )
                """,
                (
                    native_message_uuid,
                    api.project_id,
                    expected_stream_uuid,
                    native_topic_uuid,
                    api.user_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_streams (
                    uuid, name, description, source_name, source, user_uuid,
                    project_id, private, invite_only, external_account_uuid,
                    provider_external_id
                ) VALUES (
                    %s, 'Saved messages', '', 'zulip', %s::jsonb, %s, %s,
                    TRUE, TRUE, %s, 'group_direct:7'
                )
                """,
                (
                    generic_projection_stream_uuid,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "stream_id": 0,
                            "server_url": "https://zulip.example.invalid",
                            "source_scope": str(account_uuid),
                        }
                    ),
                    api.user_uuid,
                    api.project_id,
                    account_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_topics (
                    uuid, project_id, name, stream_uuid
                ) VALUES (%s, %s, 'default', %s)
                """,
                (topic_uuid, api.project_id, generic_projection_stream_uuid),
            )
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET default_topic_uuid = %s
                WHERE project_id = %s AND uuid = %s
                """,
                (topic_uuid, api.project_id, generic_projection_stream_uuid),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid, role
                ) VALUES (%s, %s, %s, %s, %s, 'owner')
                """,
                (
                    sys_uuid.uuid4(),
                    api.project_id,
                    generic_projection_stream_uuid,
                    api.user_uuid,
                    api.user_uuid,
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_workspace_messages (
                    uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                    payload, external_account_uuid, provider_external_id
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '{"kind":"markdown","content":"legacy self DM"}'::jsonb,
                    %s, '123'
                )
                """,
                (
                    legacy_message_uuid,
                    api.project_id,
                    generic_projection_stream_uuid,
                    topic_uuid,
                    api.user_uuid,
                    account_uuid,
                ),
            )
            if scenario == "reuse":
                cursor.execute(
                    """
                    INSERT INTO m_workspace_files (
                        uuid, project_id, name, user_uuid, stream_uuid,
                        acl_mode, external_account_uuid, content_type,
                        size_bytes, hash, storage_type, storage_object_id
                    ) VALUES (
                        %s, %s, 'legacy-self-dm.txt', %s, %s, 'stream', %s,
                        'text/plain', 1, 'legacy-self-dm', 'file',
                        'legacy-self-dm.txt'
                    )
                    """,
                    (
                        legacy_file_uuid,
                        api.project_id,
                        api.user_uuid,
                        generic_projection_stream_uuid,
                        account_uuid,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO m_workspace_file_accesses (
                        uuid, project_id, file_uuid, user_uuid
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        sys_uuid.uuid4(),
                        api.project_id,
                        legacy_file_uuid,
                        api.user_uuid,
                    ),
                )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                history_depth, projection_stream_uuid, status
            ) VALUES (
                %s, %s, %s, 'zulip', 'group_direct:7', %s::jsonb,
                'Saved messages', %s, %s, '30_days', %s, %s
            )
            """,
            (
                chat_uuid,
                account_uuid,
                api.user_uuid,
                json.dumps(source),
                scenario == "repair",
                api.project_id if scenario == "repair" else None,
                (generic_projection_stream_uuid if scenario == "repair" else None),
                "live" if scenario == "repair" else "available",
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version
            ) VALUES (%s, %s, 0)
            ON CONFLICT (project_id, user_uuid) DO UPDATE
            SET current_epoch_version = EXCLUDED.current_epoch_version
            RETURNING epoch_generation
            """,
            (api.project_id, api.user_uuid),
        )
        epoch_generation_before = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version
            ) VALUES (%s, %s, 0)
            ON CONFLICT (project_id, user_uuid) DO UPDATE
            SET current_epoch_version = EXCLUDED.current_epoch_version
            RETURNING epoch_generation
            """,
            (unrelated_project_uuid, unrelated_user_uuid),
        )
        unrelated_epoch_generation = cursor.fetchone()[0]

    if scenario == "repair":
        db.commit()
        with ra_engines.engine_factory.get_engine().session_manager() as session:
            assert (
                sql_state.repair_external_chat_assignments(
                    session,
                    account_uuid,
                    bridge_instance_uuid,
                    "zulip",
                )
                == 1
            )
        selected = api.get(f"{EXTERNAL_CHATS}{chat_uuid}")
    else:
        selected = api.post(
            f"{EXTERNAL_CHATS}{chat_uuid}/actions/select/invoke",
            json={"project_id": api.project_id},
        )

    assert selected.status_code == 200, selected.text
    assert selected.json()["projection_stream_uuid"] == str(expected_stream_uuid)
    stream = api.get(f"{STREAMS}{expected_stream_uuid}")
    assert stream.status_code == 200, stream.text
    assert stream.json()["private"] is True
    assert stream.json()["direct_user_uuid"] == api.user_uuid
    assert "private_index" not in stream.json()

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, source_name, direct_user_uuid, private_index
            FROM m_workspace_streams
            WHERE project_id = %s AND private_index = %s
            """,
            (api.project_id, expected_private_index),
        )
        assert cursor.fetchall() == [
            (
                expected_stream_uuid,
                "native",
                sys_uuid.UUID(api.user_uuid),
                expected_private_index,
            )
        ]
        cursor.execute(
            """
            SELECT default_topic_uuid FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, expected_stream_uuid),
        )
        canonical_topic_uuid = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT is_archived FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, generic_projection_stream_uuid),
        )
        assert cursor.fetchall() == ([(True,)] if has_legacy_projection else [])
        cursor.execute(
            """
            SELECT user_uuid, role
            FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, expected_stream_uuid),
        )
        assert cursor.fetchall() == [(sys_uuid.UUID(api.user_uuid), "owner")]
        if has_legacy_projection:
            cursor.execute(
                """
                SELECT message.stream_uuid, message.topic_uuid,
                       stream.default_topic_uuid
                FROM m_workspace_messages AS message
                JOIN m_workspace_streams AS stream
                  ON stream.project_id = message.project_id
                 AND stream.uuid = message.stream_uuid
                WHERE message.project_id = %s AND message.uuid = %s
                """,
                (api.project_id, legacy_message_uuid),
            )
            moved_stream_uuid, moved_topic_uuid, moved_default_topic_uuid = (
                cursor.fetchone()
            )
            assert moved_stream_uuid == expected_stream_uuid
            assert moved_topic_uuid == moved_default_topic_uuid
            assert moved_default_topic_uuid == canonical_topic_uuid
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'stream.created'
              AND payload->>'uuid' = %s
            """,
            (api.project_id, str(expected_stream_uuid)),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT resource
            FROM m_external_bridge_desired_changes_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (chat_uuid,),
        )
        assignment = cursor.fetchone()[0]
        assert assignment["provider_chat"]["chat_type"] == "group_direct"
        assert assignment["workspace_projection"]["stream"]["uuid"] == str(
            expected_stream_uuid
        )
        assignment_topics = assignment["workspace_projection"]["topics"]
        assert len(assignment_topics) == 1
        assert assignment_topics[0]["topic_uuid"] == str(canonical_topic_uuid)
        assert assignment_topics[0]["provider_topic_id"] == "group_direct:7:default"
        assert assignment_topics[0]["is_default"] is True
        cursor.execute(
            """
            SELECT epoch_generation
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, api.user_uuid),
        )
        assert cursor.fetchone()[0] != epoch_generation_before
        cursor.execute(
            """
            SELECT epoch_generation
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (unrelated_project_uuid, unrelated_user_uuid),
        )
        assert cursor.fetchone()[0] == unrelated_epoch_generation

        capability = {
            "messenger.message.send": {
                "available": True,
                "revision": 1,
                "limits": {},
            },
            "messenger.message.edit": {
                "available": True,
                "revision": 1,
                "limits": {},
            },
            "messenger.message.delete": {
                "available": True,
                "revision": 1,
                "limits": {},
            },
            "messenger.message.read": {
                "available": True,
                "revision": 2,
                "limits": {},
            },
            "messenger.reaction.write": {
                "available": True,
                "revision": 1,
                "limits": {},
            },
        }
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET live_ready = TRUE, capabilities = %s::jsonb
            WHERE uuid = %s
            """,
            (json.dumps(capability), account_uuid),
        )
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET status = 'live', capabilities = %s::jsonb,
                catalog_capabilities = %s::jsonb
            WHERE uuid = %s
            """,
            (
                json.dumps(capability),
                json.dumps(capability),
                chat_uuid,
            ),
        )
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (
                json.dumps(capability),
                bridge_instance_uuid,
            ),
        )
    db.commit()

    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": str(expected_stream_uuid),
            "topic_uuid": str(canonical_topic_uuid),
            "payload": {"kind": "markdown", "content": "provider self DM"},
        },
    )
    assert message.status_code == 201, message.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT operation_kind, external_account_uuid
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid IN (
                SELECT uuid FROM m_external_operations_v2
                WHERE target_uuid = %s
            )
            """,
            (message.json()["uuid"],),
        )
        assert cursor.fetchone() == ("message.create", account_uuid)
        cursor.execute(
            """
            UPDATE m_external_operations_v2
            SET status = 'failed', can_retry = TRUE
            WHERE target_uuid = %s AND action = 'message.create'
            """,
            (message.json()["uuid"],),
        )
    db.commit()

    updated_provider_message = api.put(
        f"{MESSAGES}{message.json()['uuid']}",
        json={
            "payload": {
                "kind": "markdown",
                "content": "provider self DM edited before echo",
            }
        },
    )
    assert updated_provider_message.status_code == 200, updated_provider_message.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT action
            FROM m_external_operations_v2
            WHERE target_uuid = %s
            ORDER BY created_at, uuid
            """,
            (message.json()["uuid"],),
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "message.create",
            "message.update",
        ]

    deleted_after_failed_create = api.post(
        MESSAGES,
        json={
            "stream_uuid": str(expected_stream_uuid),
            "topic_uuid": str(canonical_topic_uuid),
            "payload": {"kind": "markdown", "content": "failed create route"},
        },
    )
    assert deleted_after_failed_create.status_code == 201, (
        deleted_after_failed_create.text
    )
    failed_message_uuid = deleted_after_failed_create.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_operations_v2
            SET status = 'failed', can_retry = TRUE
            WHERE target_uuid = %s AND action = 'message.create'
            """,
            (failed_message_uuid,),
        )
    db.commit()
    failed_update = api.put(
        f"{MESSAGES}{failed_message_uuid}",
        json={"payload": {"kind": "markdown", "content": "failed create edited"}},
    )
    assert failed_update.status_code == 200, failed_update.text
    failed_delete = api.delete(f"{MESSAGES}{failed_message_uuid}")
    assert failed_delete.status_code in (200, 204), failed_delete.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT action
            FROM m_external_operations_v2
            WHERE target_uuid = %s
            ORDER BY created_at, uuid
            """,
            (failed_message_uuid,),
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "message.create",
            "message.update",
            "message.delete",
        ]
    with ra_engines.engine_factory.get_engine().session_manager() as session:
        assert provider_event_apply._message_event(
            session,
            {
                "external_account_uuid": str(account_uuid),
                "kind": "message.upsert",
            },
            sys_uuid.UUID(api.project_id),
            {
                "owner_user_uuid": sys_uuid.UUID(api.user_uuid),
                "projection_stream_uuid": expected_stream_uuid,
            },
            {
                "uuid": failed_message_uuid,
                "stream_uuid": str(expected_stream_uuid),
            },
            types.SimpleNamespace(
                bridge_instance_uuid=bridge_instance_uuid,
                provider_kind="zulip",
            ),
        ) == sys_uuid.UUID(failed_message_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, failed_message_uuid),
        )
        assert cursor.fetchone() == (0,)

    if has_legacy_projection:
        read_native = api.post(
            f"{MESSAGES}{native_message_uuid}/actions/read/invoke",
            json={},
        )
        assert read_native.status_code == 200, read_native.text
        native_reaction = api.post(
            MESSAGE_REACTIONS,
            json={
                "message_uuid": str(native_message_uuid),
                "emoji_name": "thumbs_up",
            },
        )
        assert native_reaction.status_code == 201, native_reaction.text
        native_reaction_uuid = native_reaction.json()["uuid"]
        updated_native_reaction = api.put(
            f"{MESSAGE_REACTIONS}{native_reaction_uuid}",
            json={"emoji_name": "heart"},
        )
        assert updated_native_reaction.status_code == 200, updated_native_reaction.text
        deleted_native_reaction = api.delete(
            f"{MESSAGE_REACTIONS}{native_reaction_uuid}"
        )
        assert deleted_native_reaction.status_code in (200, 204), (
            deleted_native_reaction.text
        )
        updated_native = api.put(
            f"{MESSAGES}{native_message_uuid}",
            json={
                "payload": {
                    "kind": "markdown",
                    "content": "still Workspace-only",
                }
            },
        )
        assert updated_native.status_code == 200, updated_native.text
        deleted_native = api.delete(f"{MESSAGES}{native_message_uuid}")
        assert deleted_native.status_code in (200, 204), deleted_native.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM m_external_operations_v2
                WHERE target_uuid = ANY(%s::uuid[])
                  AND action IN (
                      'read_state.set', 'reaction.create', 'reaction.update',
                      'reaction.delete', 'message.update', 'message.delete'
                  )
                """,
                ([str(native_message_uuid), str(native_reaction_uuid)],),
            )
            assert cursor.fetchone() == (0,)

    if scenario == "reuse":
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT epoch_generation
                FROM m_workspace_event_cursors
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, api.user_uuid),
            )
            before_deselect_generation = cursor.fetchone()[0]
        deselected = api.post(
            f"{EXTERNAL_CHATS}{chat_uuid}/actions/deselect/invoke",
            json={},
        )
        assert deselected.status_code == 200, deselected.text
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM m_workspace_streams
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM m_workspace_files WHERE uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM m_workspace_file_accesses
                        WHERE file_uuid = %s
                    )
                """,
                (
                    api.project_id,
                    generic_projection_stream_uuid,
                    legacy_file_uuid,
                    legacy_file_uuid,
                ),
            )
            assert cursor.fetchone() == (False, False, False)
            cursor.execute(
                """
                SELECT
                    EXISTS(
                        SELECT 1 FROM m_workspace_messages
                        WHERE project_id = %s AND uuid = %s
                    ),
                    EXISTS(
                        SELECT 1 FROM m_external_operations_v2
                        WHERE target_uuid = %s
                          AND action IN ('message.create', 'message.update')
                    )
                """,
                (
                    api.project_id,
                    message.json()["uuid"],
                    message.json()["uuid"],
                ),
            )
            assert cursor.fetchone() == (False, False)
            cursor.execute(
                """
                SELECT epoch_generation
                FROM m_workspace_event_cursors
                WHERE project_id = %s AND user_uuid = %s
                """,
                (api.project_id, api.user_uuid),
            )
            assert cursor.fetchone()[0] != before_deselect_generation


def test_native_self_dm_operations_fan_out_to_every_selected_provider_account(api, db):
    pytest.skip("multiple accounts per provider remain intentionally unsupported")
    _enable_zulip_policy(db, max_accounts=2)
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    created_stream = api.post(
        STREAMS,
        json={
            "name": "Personal notes",
            "description": "",
            "source_name": "native",
            "source": {"kind": "native"},
            "direct_user_uuid": api.user_uuid,
        },
    )
    assert created_stream.status_code in (200, 201), created_stream.text
    stream_uuid = created_stream.json()["uuid"]
    topic_uuid = created_stream.json()["default_topic_uuid"]
    account_uuids = tuple(sorted((sys_uuid.uuid4(), sys_uuid.uuid4()), key=str))
    capability = {
        name: {"available": True, "revision": revision, "limits": {}}
        for name, revision in (
            ("messenger.message.send", 1),
            ("messenger.message.edit", 1),
            ("messenger.message.delete", 1),
            ("messenger.message.read", 2),
            ("messenger.reaction.write", 1),
        )
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        for index, account_uuid in enumerate(account_uuids):
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    credential_present, status, live_ready, capabilities
                ) VALUES (
                    %s, %s, 'zulip', %s::jsonb,
                    TRUE, 'live', TRUE, %s::jsonb
                )
                """,
                (
                    account_uuid,
                    api.user_uuid,
                    json.dumps(
                        {
                            "kind": "zulip",
                            "server_url": f"https://zulip-{index}.example.invalid",
                            "default_project_id": api.project_id,
                        }
                    ),
                    json.dumps(capability),
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_credentials_v2 (
                    uuid, external_account_uuid, key_version, envelope
                ) VALUES (%s, %s, 1, %s::jsonb)
                """,
                (
                    sys_uuid.uuid4(),
                    account_uuid,
                    json.dumps(
                        {
                            "associated_data": {
                                "bridge_instance_uuid": str(bridge_uuid),
                            }
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO m_external_chats_v2 (
                    uuid, external_account_uuid, owner_user_uuid, provider,
                    provider_chat_id, source, display_name, selected, project_id,
                    projection_stream_uuid, status, capabilities,
                    catalog_capabilities
                ) VALUES (
                    %s, %s, %s, 'zulip', %s,
                    %s::jsonb, 'Saved messages', TRUE, %s, %s, 'live',
                    %s::jsonb, %s::jsonb
                )
                """,
                (
                    sys_uuid.uuid4(),
                    account_uuid,
                    api.user_uuid,
                    f"group_direct:{index}",
                    json.dumps(
                        {
                            "kind": "zulip",
                            "chat_type": "group_direct",
                            "participants": [],
                            "topics": [],
                        }
                    ),
                    api.project_id,
                    stream_uuid,
                    json.dumps(capability),
                    json.dumps(capability),
                ),
            )
    db.commit()

    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "fan out"},
        },
    )
    assert message.status_code == 201, message.text
    message_uuid = message.json()["uuid"]
    edited = api.put(
        f"{MESSAGES}{message_uuid}",
        json={"payload": {"kind": "markdown", "content": "fan out edited"}},
    )
    assert edited.status_code == 200, edited.text
    reaction = api.post(
        MESSAGE_REACTIONS,
        json={"message_uuid": message_uuid, "emoji_name": "thumbs_up"},
    )
    assert reaction.status_code == 201, reaction.text
    reaction_uuid = reaction.json()["uuid"]
    reaction_edit = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"emoji_name": "heart"},
    )
    assert reaction_edit.status_code == 200, reaction_edit.text
    reaction_delete = api.delete(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert reaction_delete.status_code in (200, 204), reaction_delete.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, api.user_uuid, message_uuid),
        )
    db.commit()
    read_message = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        json={},
    )
    assert read_message.status_code == 200, read_message.text
    deleted = api.delete(f"{MESSAGES}{message_uuid}")
    assert deleted.status_code in (200, 204), deleted.text

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT action, ARRAY_AGG(external_account_uuid ORDER BY external_account_uuid)
            FROM m_external_operations_v2
            WHERE target_uuid = %s
            GROUP BY action
            """,
            (message_uuid,),
        )
        message_routes = dict(cursor.fetchall())
        assert message_routes == {
            action: list(account_uuids)
            for action in (
                "message.create",
                "message.update",
                "read_state.set",
                "message.delete",
            )
        }
        cursor.execute(
            """
            SELECT action, ARRAY_AGG(external_account_uuid ORDER BY external_account_uuid)
            FROM m_external_operations_v2
            WHERE target_uuid = %s
            GROUP BY action
            """,
            (reaction_uuid,),
        )
        assert dict(cursor.fetchall()) == {
            action: list(account_uuids)
            for action in (
                "reaction.create",
                "reaction.update",
                "reaction.delete",
            )
        }


def test_direct_event_history_invalidation_covers_every_bound_participant(api, db):
    peer_uuid = sys_uuid.uuid4()
    unrelated_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, peer_uuid, f"user-{peer_uuid}")
    conftest.seed_workspace_user(db, unrelated_uuid, f"user-{unrelated_uuid}")
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Direct invalidation scope",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        peer_uuid,
    )
    generations_before = {}
    with db.cursor() as cursor:
        for user_uuid in (api.user_uuid, peer_uuid, unrelated_uuid):
            cursor.execute(
                """
                INSERT INTO m_workspace_event_cursors (
                    project_id, user_uuid, current_epoch_version
                ) VALUES (%s, %s, 0)
                ON CONFLICT (project_id, user_uuid) DO UPDATE
                SET current_epoch_version = EXCLUDED.current_epoch_version
                RETURNING epoch_generation
                """,
                (api.project_id, user_uuid),
            )
            generations_before[sys_uuid.UUID(str(user_uuid))] = cursor.fetchone()[0]
    db.commit()

    _run_database_operation(
        lambda session: identity_linking.invalidate_direct_event_history(
            session,
            project_id=api.project_id,
            stream_uuid=stream_uuid,
        )
    )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, epoch_generation
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = ANY(%s::uuid[])
            """,
            (
                api.project_id,
                [str(api.user_uuid), str(peer_uuid), str(unrelated_uuid)],
            ),
        )
        generations_after = dict(cursor.fetchall())
    assert (
        generations_after[sys_uuid.UUID(str(api.user_uuid))]
        != (generations_before[sys_uuid.UUID(str(api.user_uuid))])
    )
    assert generations_after[peer_uuid] != generations_before[peer_uuid]
    assert generations_after[unrelated_uuid] == generations_before[unrelated_uuid]


def test_external_provider_policy_blocks_account_and_operation_boundaries(
    api,
    db,
    tmp_path,
):
    realm_uuid = sys_uuid.uuid4()
    del tmp_path
    _seed_zulip_bridge_target(db)
    cfg.CONF.set_override(
        "realm_uuid", str(realm_uuid), group=external_bridge_opts.DOMAIN
    )
    account_uuid = sys_uuid.uuid4()
    payload = {
        "uuid": str(account_uuid),
        "settings": {
            "kind": "zulip",
            "server_url": "https://zulip.example.invalid",
            "email": "owner@example.invalid",
            "api_key": "provider-secret",
            "selection_mode": "explicit",
            "history_depth": "30_days",
            "default_project_id": api.project_id,
        },
    }
    try:
        _enable_zulip_policy(db, max_accounts=1)
        created = api.post(
            EXTERNAL_ACCOUNTS,
            json=payload,
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert created.status_code == 201, created.text

        reached = api.post(
            EXTERNAL_ACCOUNTS,
            json={**payload, "uuid": str(sys_uuid.uuid4())},
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert reached.status_code == 403, reached.text

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_external_provider_policies_v1
                SET emergency_suspended = TRUE
                WHERE provider = 'zulip'
                """
            )
        db.commit()
        reconnect = api.post(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}/actions/reconnect/invoke",
            headers={"If-Match": '"1"'},
            json={
                "settings": {
                    "kind": "zulip",
                    "server_url": "https://zulip.example.invalid",
                    "email": "owner@example.invalid",
                    "api_key": "replacement-secret",
                }
            },
            permissions=EXTERNAL_ACCOUNT_RECONNECT,
        )
        assert reconnect.status_code == 403, reconnect.text
        preflight = api.post(
            f"{EXTERNAL_OPERATIONS}actions/preflight/invoke",
            json={
                "external_account_uuid": str(account_uuid),
                "action": "message.create",
                "target": {},
            },
        )
        assert preflight.status_code == 403, preflight.text

        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_external_provider_policies_v1
                SET enabled = FALSE, emergency_suspended = FALSE
                WHERE provider = 'zulip'
                """
            )
        db.commit()
        disabled = api.post(
            EXTERNAL_ACCOUNTS,
            json={**payload, "uuid": str(sys_uuid.uuid4())},
            permissions=EXTERNAL_ACCOUNT_CREATE,
        )
        assert disabled.status_code == 403, disabled.text
    finally:
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


def test_provider_operation_resolution_allows_backfill_and_uses_current_policy(
    api,
    db,
):
    _enable_zulip_policy(db)
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Current policy gate",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    capability = {
        "messenger.message.send": {
            "available": True,
            "revision": 1,
            "limits": {},
        }
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, %s::jsonb
            )
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.invalid",
                        "default_project_id": api.project_id,
                    }
                ),
                json.dumps(capability),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities,
                catalog_capabilities
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:current-policy',
                '{"kind":"zulip","chat_type":"channel"}'::jsonb,
                'Current policy gate', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                chat_uuid,
                account_uuid,
                api.user_uuid,
                api.project_id,
                stream_uuid,
                json.dumps(capability),
                json.dumps(capability),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:current-policy'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(
                    {
                        "kind": "zulip",
                        "stream_id": 42,
                        "server_url": "https://zulip.example.invalid",
                        "source_scope": str(account_uuid),
                    }
                ),
                account_uuid,
                api.project_id,
                stream_uuid,
            ),
        )
    db.commit()

    def resolve():
        return _run_database_operation(
            lambda session: provider_data.resolve_provider_target(
                session,
                project_id=api.project_id,
                owner_user_uuid=api.user_uuid,
                external_account_uuid=account_uuid,
                stream_uuid=stream_uuid,
                capability_name="messenger.message.send",
            )
        )

    assert resolve()[0].uuid == account_uuid

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET status = 'backfill', live_ready = FALSE
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET status = 'syncing'
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
    db.commit()
    assert resolve()[0].uuid == account_uuid
    preflight = api.post(
        f"{EXTERNAL_OPERATIONS}actions/preflight/invoke",
        json={
            "external_account_uuid": str(account_uuid),
            "action": "messenger.message.send",
            "target": {"type": "stream", "uuid": str(stream_uuid)},
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["allowed"] is True
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "payload": {"kind": "markdown", "content": "during backfill"},
        },
    )
    assert message.status_code == 201, message.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT queue.operation_kind, operation.target_uuid
            FROM m_external_provider_operations_v1 AS queue
            JOIN m_external_operations_v2 AS operation
              ON operation.uuid = queue.external_operation_uuid
            WHERE queue.external_account_uuid = %s
            """,
            (account_uuid,),
        )
        assert cursor.fetchone() == (
            "message.create",
            sys_uuid.UUID(message.json()["uuid"]),
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET status = 'connecting'
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    db.commit()
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()
    preflight = api.post(
        f"{EXTERNAL_OPERATIONS}actions/preflight/invoke",
        json={
            "external_account_uuid": str(account_uuid),
            "action": "messenger.message.send",
            "target": {"type": "stream", "uuid": str(stream_uuid)},
        },
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["allowed"] is False

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET status = 'backfill'
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    db.commit()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = '{}'::jsonb
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
    db.commit()
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET catalog_capabilities = '{}'::jsonb
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
    db.commit()
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET catalog_capabilities = %s::jsonb
            WHERE uuid = %s
            """,
            (json.dumps(capability), chat_uuid),
        )
    db.commit()
    assert resolve()[1].uuid == chat_uuid

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET status = 'degraded',
                last_heartbeat_at = NOW() - INTERVAL '61 seconds'
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
    db.commit()
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET status = 'active', last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
    db.commit()
    assert resolve()[2].uuid == bridge_uuid

    replacement_bridge_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET created_at = NOW() - INTERVAL '1 hour'
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status, capabilities,
                last_heartbeat_at
            ) VALUES (%s, 'zulip', 2, 'active', %s::jsonb, NOW())
            """,
            (replacement_bridge_uuid, json.dumps(capability)),
        )
    db.commit()
    assert resolve()[2].uuid == bridge_uuid
    assert (
        _run_database_operation(
            lambda session: provider_data.resolve_provider_queue_target(
                session,
                project_id=api.project_id,
                owner_user_uuid=api.user_uuid,
                external_account_uuid=account_uuid,
                stream_uuid=stream_uuid,
            )
        )[2].uuid
        == bridge_uuid
    )

    suspended = api.post(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip/actions/suspend/invoke",
        permissions=("workspace.external_provider_policy.suspend",),
    )
    assert suspended.status_code == 200, suspended.text
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()

    resumed = api.post(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip/actions/resume/invoke",
        permissions=("workspace.external_provider_policy.resume",),
    )
    assert resumed.status_code == 200, resumed.text
    assert resolve()[1].uuid == chat_uuid

    current = api.get(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip",
        permissions=("workspace.external_provider_policy.read",),
    )
    assert current.status_code == 200, current.text
    disabled = api.put(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip",
        permissions=("workspace.external_provider_policy.update",),
        headers={"If-Match": current.headers["ETag"]},
        json={
            "settings": {
                "kind": "zulip",
                "enabled": False,
                "limits": {
                    "max_accounts": 100,
                    "max_selected_chats_per_account": 1000,
                    "max_file_bytes": 104857600,
                },
                "custom_ca_bundle": None,
            }
        },
    )
    assert disabled.status_code == 200, disabled.text
    with pytest.raises(provider_data.ProviderUnavailableError):
        resolve()

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT account.capabilities, chat.capabilities
            FROM m_external_accounts_v2 AS account
            JOIN m_external_chats_v2 AS chat
              ON chat.external_account_uuid = account.uuid
            WHERE account.uuid = %s AND chat.uuid = %s
            """,
            (account_uuid, chat_uuid),
        )
        account_capabilities, chat_capabilities = cursor.fetchone()
    assert account_capabilities == capability
    assert chat_capabilities == capability


def test_external_provider_admin_policy_ca_and_health_are_permission_scoped(api, db):
    _seed_zulip_bridge_target(db)
    denied = api.get(f"{EXTERNAL_PROVIDER_POLICIES}zulip")
    assert denied.status_code == 403, denied.text

    read_permission = ("workspace.external_provider_policy.read",)
    current = api.get(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip",
        permissions=read_permission,
    )
    assert current.status_code == 200, current.text
    ca_pem = _ca_certificate_pem()
    settings = {
        "settings": {
            "kind": "zulip",
            "enabled": True,
            "limits": {
                "max_accounts": 50,
                "max_selected_chats_per_account": 500,
                "max_file_bytes": 104857600,
            },
            "custom_ca_bundle": {"certificates_pem": [ca_pem]},
        }
    }
    updated = api.put(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip",
        permissions=("workspace.external_provider_policy.update",),
        headers={"If-Match": current.headers["ETag"]},
        json=settings,
    )
    assert updated.status_code == 200, updated.text
    policy = updated.json()
    assert policy["enabled"] is True
    assert policy["custom_ca_bundle"]["certificate_count"] == 1
    assert "certificates_pem" not in updated.text
    assert "PRIVATE KEY" not in updated.text

    invalid = api.put(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip",
        permissions=("workspace.external_provider_policy.update",),
        headers={"If-Match": updated.headers["ETag"]},
        json={
            "settings": {
                **settings["settings"],
                "custom_ca_bundle": {
                    "certificates_pem": [ca_pem + "-----BEGIN PRIVATE KEY-----"]
                },
            }
        },
    )
    assert invalid.status_code == 400, invalid.text

    suspended = api.post(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip/actions/suspend/invoke",
        permissions=("workspace.external_provider_policy.suspend",),
    )
    assert suspended.status_code == 200, suspended.text
    assert suspended.json()["emergency_suspended"] is True
    resumed = api.post(
        f"{EXTERNAL_PROVIDER_POLICIES}zulip/actions/resume/invoke",
        permissions=("workspace.external_provider_policy.resume",),
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["emergency_suspended"] is False

    health_denied = api.get(f"{EXTERNAL_PROVIDER_HEALTH}zulip")
    assert health_denied.status_code == 403, health_denied.text
    health = api.get(
        f"{EXTERNAL_PROVIDER_HEALTH}zulip",
        permissions=("workspace.external_provider_health.read",),
    )
    assert health.status_code == 200, health.text
    assert health.json()["provider"] == "zulip"
    assert health.json()["status"] == "healthy"
    assert isinstance(health.json()["chat_counts"], dict)
    assert set(health.json()["metrics"]) == {
        "queue_depth",
        "selected_chats",
        "synchronized_messages",
        "synchronized_users",
    }

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT resource_type, operation, resource
            FROM m_external_bridge_desired_changes_v1
            WHERE resource_type IN (
                'external_provider_policy', 'custom_ca_bundle'
            )
            ORDER BY sequence
            """
        )
        desired = cursor.fetchall()
    assert any(row[0:2] == ("custom_ca_bundle", "upsert") for row in desired)
    assert desired[-1][0:2] == ("external_provider_policy", "upsert")
    assert desired[-1][2]["emergency_suspended"] is False


@pytest.mark.parametrize(
    "crash_phase",
    [
        "canonical_new",
        "canonical_old",
        "sql_applied",
        "files_purged",
    ],
)
def test_external_projection_move_is_request_atomic_after_each_phase(
    api, db, monkeypatch, crash_phase
):
    _enable_zulip_policy(db)
    bridge_instance_uuid, key_uuid, _ = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    old_project = sys_uuid.UUID(api.project_id)
    new_project = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, old_project, api.user_uuid, "Crash replay")
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings, credential_present, status
            ) VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live')
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": (
                            f"https://{account_uuid}.zulip.example.invalid"
                        ),
                        "selection_mode": "explicit",
                        "history_depth": "30_days",
                        "default_project_id": str(old_project),
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_instance_uuid),
                            "credential_key_uuid": str(key_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, revision
            ) VALUES (
                %s, %s, %s, 'zulip', 'crash-chat',
                '{"kind":"zulip","chat_type":"channel"}'::jsonb,
                'Crash replay', TRUE, %s, %s, 'live', 2
            )
            """,
            (
                chat_uuid,
                account_uuid,
                api.user_uuid,
                old_project,
                stream_uuid,
            ),
        )

    original = messenger_controllers.ExternalChatController._transition_phase
    crashed = False

    def crash_after_phase(session, transition_uuid, phase, safe_error=None):
        nonlocal crashed
        original(session, transition_uuid, phase, safe_error)
        if phase == crash_phase and not crashed:
            crashed = True
            raise RuntimeError(f"crash after {phase}")

    monkeypatch.setattr(
        messenger_controllers.ExternalChatController,
        "_transition_phase",
        staticmethod(crash_after_phase),
    )
    path = f"{EXTERNAL_CHATS}{chat_uuid}/actions/move/invoke"
    failed = api.post(
        path,
        project=new_project,
        headers={"If-Match": '"2"'},
        json={"project_id": str(new_project)},
    )
    assert failed.status_code == 500
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT phase FROM m_external_projection_transitions_v1
            WHERE external_chat_uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() is None
        cursor.execute(
            """
            SELECT selected, project_id, projection_stream_uuid, status, revision
            FROM m_external_chats_v2
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == (
            True,
            old_project,
            stream_uuid,
            "live",
            2,
        )
        cursor.execute(
            "SELECT COUNT(*), ARRAY_AGG(project_id) FROM m_workspace_streams WHERE uuid = %s",
            (stream_uuid,),
        )
        assert cursor.fetchone() == (1, [old_project])

    monkeypatch.setattr(
        messenger_controllers.ExternalChatController,
        "_transition_phase",
        staticmethod(original),
    )
    resumed = api.post(
        path,
        project=new_project,
        headers={"If-Match": '"2"'},
        json={"project_id": str(new_project)},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["project_id"] == str(new_project)
    assert resumed.json()["transition_pending"] is False
    repeated = api.post(
        path,
        project=new_project,
        headers={"If-Match": '"3"'},
        json={"project_id": str(new_project)},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["revision"] == 3
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT phase, COUNT(*) OVER ()
            FROM m_external_projection_transitions_v1
            WHERE external_chat_uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == ("completed", 1)
        cursor.execute(
            "SELECT COUNT(*), ARRAY_AGG(project_id) FROM m_workspace_streams WHERE uuid = %s",
            (stream_uuid,),
        )
        assert cursor.fetchone() == (1, [new_project])


def test_database_operation_boundary_owns_one_isolated_session_per_worker(
    _database,
    monkeypatch,
):
    del _database
    created_sessions = []
    created_sessions_lock = threading.Lock()
    workers_ready = threading.Barrier(2)
    original_start_new_session = ra_contexts.Context.start_new_session

    def record_started_session(context):
        session = original_start_new_session(context)
        with created_sessions_lock:
            created_sessions.append((threading.get_ident(), session))
        return session

    monkeypatch.setattr(
        ra_contexts.Context,
        "start_new_session",
        record_started_session,
    )

    def run_worker_operation():
        def operation(session):
            assert ra_contexts.Context().get_session() is session
            workers_ready.wait(timeout=5)
            assert ra_contexts.Context().get_session() is session
            return session

        session = _run_database_operation(operation)
        with pytest.raises(ra_sessions.SessionNotFound):
            ra_contexts.Context().get_session()
        return session

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(lambda _: run_worker_operation(), range(2)))

    assert sessions[0] is not sessions[1]
    assert len(created_sessions) == 2
    assert len({thread_id for thread_id, _ in created_sessions}) == 2
    assert {id(session) for session in sessions} == {
        id(session) for _, session in created_sessions
    }


def test_user_get_by_uuid_uses_global_user_table(api, db):
    user_uuid = sys_uuid.uuid4()
    username = f"user-{user_uuid}"
    conftest.seed_workspace_user(db, user_uuid, username)

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["uuid"] == str(user_uuid)
    assert user["username"] == username
    assert user["avatar"] == (
        messenger_models.build_workspace_user_default_avatar(user_uuid)
    )

    resp = api.get(USERS, params={"username": username})
    assert resp.status_code == 200, resp.text
    assert [user["uuid"] for user in resp.json()] == [str(user_uuid)]


def test_own_message_read_backfill_migration(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "own-message-backfill"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuid = sys_uuid.uuid4()
    _run_database_operation(
        lambda session: messenger_dm_helpers.create_workspace_user_message(
            uuid=message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(stream_uuid),
            topic_uuid=sys_uuid.UUID(topic_uuid),
            payload=message_payloads.MarkdownPayload(content="backfill me"),
            session=session,
        )
    )
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE
            WHERE uuid = %s
                AND user_uuid = %s
            """,
            (message_uuid, api.user_uuid),
        )

    migration_path = conftest.MIGRATIONS_DIR / "0094-mark-own-messages-read-8413a3.py"
    spec = importlib.util.spec_from_file_location(
        "mark_own_messages_read_migration",
        migration_path,
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.migration_step.upgrade(db)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, read
            FROM m_workspace_user_message_flags
            WHERE uuid = %s
            ORDER BY user_uuid
            """,
            (message_uuid,),
        )
        flags = {str(row[0]): row[1] for row in cur.fetchall()}

    assert flags == {
        str(api.user_uuid): True,
        str(other_user): False,
    }


def test_user_presence_action_updates_current_user_presence(api, workspace_api, db):
    username = f"user-{api.user_uuid}"
    event_recipient_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, api.user_uuid, username)
    conftest.seed_workspace_user(
        db,
        event_recipient_uuid,
        f"user-{event_recipient_uuid}",
    )
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "presence-team",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        event_recipient_uuid,
    )

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={
            "status": "idle",
            "emoji": "coffee",
            "text": "Focusing",
        },
    )
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["uuid"] == str(api.user_uuid)
    assert user["avatar"] == (
        messenger_models.build_workspace_user_default_avatar(api.user_uuid)
    )
    assert user["status"] == "idle"
    assert user["status_emoji"] == "coffee"
    assert user["status_text"] == "Focusing"
    assert user["last_ping_at"] is not None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status, status_emoji, status_text, last_ping_at
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(api.user_uuid),),
        )
        row = cur.fetchone()
    assert row[0] == "idle"
    assert row[1] == "coffee"
    assert row[2] == "Focusing"
    assert row[3] is not None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, str(api.user_uuid)),
        )
        event_rows = cur.fetchall()
    event_recipient_uuids = {str(row[0]) for row in event_rows}
    assert str(api.user_uuid) in event_recipient_uuids
    assert str(event_recipient_uuid) in event_recipient_uuids
    for _, payload in event_rows:
        assert payload["username"] == username
        assert payload["avatar"] == (
            messenger_models.build_workspace_user_default_avatar(api.user_uuid)
        )
        assert payload["status"] == "idle"
        assert payload["status_emoji"] == "coffee"
        assert payload["status_text"] == "Focusing"
        assert payload["last_ping_at"] is not None
        assert "user_uuid" not in payload
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM m_workspace_events
                 WHERE project_id = %s
                   AND payload->>'kind' = 'user.updated'),
                (SELECT count(*) FROM m_workspace_broadcast_message_events_v1
                 WHERE project_id = %s
                   AND payload->>'kind' = 'user.updated')
            """,
            (api.project_id, api.project_id),
        )
        direct_count, broadcast_count = cur.fetchone()
    assert direct_count == 0
    assert broadcast_count == 1

    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    events_resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert events_resp.status_code == 200, events_resp.text
    public_events = [
        event
        for event in events_resp.json()
        if event["payload"]["kind"] == "user.updated"
    ]
    assert len(public_events) == 1
    assert public_events[0]["payload"] == {
        "kind": "user.updated",
        "first_name": None,
        "last_name": None,
        "email": None,
        **user,
    }

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "active"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert resp.json()["status_emoji"] == "coffee"
    assert resp.json()["status_text"] == "Focusing"

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle", "emoji": None, "text": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status_emoji") is None
    assert resp.json().get("status_text") is None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status_emoji, status_text
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(api.user_uuid),),
        )
        row = cur.fetchone()
    assert row == (None, None)

    other_user_uuid = sys_uuid.uuid4()
    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        user=other_user_uuid,
        json={"status": "active"},
    )
    assert resp.status_code == 404, resp.text


def test_user_directory_keeps_only_canonical_provider_identities(api, db):
    iam_user_uuid = sys_uuid.uuid4()
    canonical_user_uuid = sys_uuid.uuid4()
    legacy_user_uuid = sys_uuid.uuid4()
    linked_owner_legacy_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, iam_user_uuid, "iam-directory-user")
    with db.cursor() as cursor:
        for user_uuid, username, provider_user_id in (
            (canonical_user_uuid, "canonical-provider-user", "25"),
            (legacy_user_uuid, "legacy-provider-user", "25"),
            (linked_owner_legacy_uuid, "linked-owner-legacy-user", "9"),
        ):
            cursor.execute(
                """
                INSERT INTO m_workspace_users (
                    uuid, username, source, status, avatar,
                    external_account_uuid, provider_external_id,
                    created_at, updated_at
                ) VALUES (
                    %s, %s, 'zulip', 'offline', %s, %s, %s, NOW(), NOW()
                )
                """,
                (
                    str(user_uuid),
                    username,
                    messenger_models.build_workspace_user_default_avatar(user_uuid),
                    str(account_uuid),
                    provider_user_id,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_external_provider_identity_links_v1 (
                provider, provider_realm_uuid, provider_user_id,
                workspace_user_uuid, link_kind
            ) VALUES
                ('zulip', %s, '25', %s, 'provider_identity'),
                ('zulip', %s, '9', %s, 'verified_account_owner')
            """,
            (
                str(provider_realm_uuid),
                str(canonical_user_uuid),
                str(provider_realm_uuid),
                str(iam_user_uuid),
            ),
        )
    db.commit()

    response = api.get(USERS)
    assert response.status_code == 200, response.text
    directory_uuids = {row["uuid"] for row in response.json()}
    assert str(iam_user_uuid) in directory_uuids
    assert str(canonical_user_uuid) in directory_uuids
    assert str(legacy_user_uuid) not in directory_uuids
    assert str(linked_owner_legacy_uuid) not in directory_uuids

    legacy_lookup = api.get(f"{USERS}{legacy_user_uuid}")
    assert legacy_lookup.status_code == 200, legacy_lookup.text
    assert legacy_lookup.json()["uuid"] == str(legacy_user_uuid)


def test_avatar_upload_is_public_to_authenticated_users_and_reset_removes_it(
    api, db, tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    conftest.seed_workspace_user(db, api.user_uuid, f"user-{api.user_uuid}")
    other_user_uuid = sys_uuid.uuid4()
    other_project_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        other_user_uuid,
        f"user-{other_user_uuid}",
    )
    data = b"\x89PNG\r\n\x1a\nworkspace-avatar"

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/avatar_upload/invoke",
        files={
            "file": (
                "Снимок экрана 2026-06-08 в 08.50.54.png",
                io.BytesIO(data),
                "image/png",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    user = resp.json()
    assert user["avatar"].startswith("urn:image:")
    file_uuid = user["avatar"].removeprefix("urn:image:")

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT project_id, user_uuid, stream_uuid
            FROM m_workspace_files
            WHERE uuid = %s
            """,
            (file_uuid,),
        )
        row = cur.fetchone()
    assert str(row[0]) == str(api.project_id)
    assert str(row[1]) == str(api.user_uuid)
    assert row[2] is None

    metadata = file_storage.read_workspace_file_metadata(file_uuid)
    assert metadata.acl_mode == "public"
    assert metadata.stream_uuid is None
    assert metadata.owner_uuid == sys_uuid.UUID(api.user_uuid)
    metadata_path = tmp_path / file_storage.get_workspace_file_metadata_object_id(
        file_uuid
    )
    assert metadata_path.exists()

    resp = api.get(
        f"{FILES}{file_uuid}",
        user=other_user_uuid,
        project=other_project_uuid,
    )
    assert resp.status_code == 200, resp.text
    resp = api.get(
        f"{FILES}{file_uuid}/actions/download",
        user=other_user_uuid,
        project=other_project_uuid,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == data
    content_disposition = resp.headers["Content-Disposition"]
    assert 'filename="download.png"' in content_disposition
    assert (
        "filename*=UTF-8''%D0%A1%D0%BD%D0%B8%D0%BC%D0%BE%D0%BA" in content_disposition
    )

    resp = api.post(
        f"{USERS}{api.user_uuid}/actions/avatar_reset/invoke",
        json={},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["avatar"] == (
        messenger_models.build_workspace_user_default_avatar(api.user_uuid)
    )
    assert not file_storage.get_workspace_file_path(
        file_uuid,
        storage_path=tmp_path,
    ).exists()
    assert not metadata_path.exists()
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM m_workspace_files WHERE uuid = %s",
            (file_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE payload->>'kind' = 'file.deleted'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (file_uuid,),
        )
        event_rows = cur.fetchall()
        # Public download ACL is global for authenticated users, while realtime
        # file invalidations stay scoped to identities participating in the
        # file's project.
        assert str(api.user_uuid) in {str(row[0]) for row in event_rows}
        assert str(other_user_uuid) not in {str(row[0]) for row in event_rows}
    assert all(
        row[1]
        == {
            "kind": "file.deleted",
            "uuid": file_uuid,
            "stream_uuid": None,
        }
        for row in event_rows
    )


def test_avatar_actions_reject_another_user_uuid(api, db, tmp_path, monkeypatch):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    target_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, target_uuid, f"user-{target_uuid}")
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM m_workspace_files")
        file_count_before = cur.fetchone()[0]

    resp = api.post(
        f"{USERS}{target_uuid}/actions/avatar_upload/invoke",
        files={
            "file": (
                "avatar.png",
                io.BytesIO(b"\x89PNG\r\n\x1a\nworkspace-avatar"),
                "image/png",
            )
        },
    )
    assert resp.status_code == 400, resp.text
    resp = api.post(
        f"{USERS}{target_uuid}/actions/avatar_reset/invoke",
        json={},
    )
    assert resp.status_code == 400, resp.text

    with db.cursor() as cur:
        cur.execute(
            "SELECT avatar FROM m_workspace_users WHERE uuid = %s",
            (target_uuid,),
        )
        assert cur.fetchone()[0] == (
            messenger_models.build_workspace_user_default_avatar(target_uuid)
        )
        cur.execute("SELECT COUNT(*) FROM m_workspace_files")
        assert cur.fetchone()[0] == file_count_before


def test_user_presence_action_skips_event_for_heartbeat(api, db):
    username = f"user-{api.user_uuid}"
    conftest.seed_workspace_user(db, api.user_uuid, username)
    conftest.seed_user_stream(db, api.project_id, api.user_uuid, "heartbeat-team")
    heartbeat_api = conftest.ApiClient(
        base_url=api.base_url,
        user_uuid=api.user_uuid,
        project_id=api.project_id,
    )

    resp = heartbeat_api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM m_workspace_visible_events AS events
            WHERE events.project_id = %s
                AND events.user_uuid = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (api.project_id, str(api.user_uuid), str(api.user_uuid)),
        )
        first_event_count = cur.fetchone()[0]
        cur.execute(
            "SELECT last_ping_at FROM m_workspace_users WHERE uuid = %s",
            (str(api.user_uuid),),
        )
        first_ping_at = cur.fetchone()[0]

    resp = heartbeat_api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM m_workspace_visible_events AS events
            WHERE events.project_id = %s
                AND events.user_uuid = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (api.project_id, str(api.user_uuid), str(api.user_uuid)),
        )
        second_event_count = cur.fetchone()[0]
        cur.execute(
            "SELECT last_ping_at FROM m_workspace_users WHERE uuid = %s",
            (str(api.user_uuid),),
        )
        second_ping_at = cur.fetchone()[0]

    assert second_event_count == first_event_count
    assert second_ping_at >= first_ping_at


def test_user_status_is_offline_when_last_ping_is_stale(api, db):
    user_uuid = sys_uuid.uuid4()
    event_recipient_uuid = sys_uuid.uuid4()
    username = f"user-{user_uuid}"
    conftest.seed_workspace_user(db, user_uuid, username)
    conftest.seed_workspace_user(
        db,
        event_recipient_uuid,
        f"user-{event_recipient_uuid}",
    )
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "status-team",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        event_recipient_uuid,
    )

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_users
            SET status = 'active',
                last_ping_at = NOW() - INTERVAL '4 minutes'
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )

    _run_database_operation(
        lambda session: messenger_dm_helpers.mark_stale_workspace_users_offline(
            session=session
        )
    )

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "offline"
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )
        assert cur.fetchone()[0] == "offline"

        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, str(user_uuid)),
        )
        event_rows = cur.fetchall()
    event_recipient_uuids = {str(row[0]) for row in event_rows}
    assert str(user_uuid) not in event_recipient_uuids
    assert str(api.user_uuid) in event_recipient_uuids
    assert str(event_recipient_uuid) in event_recipient_uuids
    for _, payload in event_rows:
        assert payload["username"] == username
        assert payload["status"] == "offline"

    _run_database_operation(
        lambda session: messenger_dm_helpers.mark_stale_workspace_users_offline(
            session=session
        )
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            """,
            (api.project_id, str(user_uuid)),
        )
        assert cur.fetchone()[0] == len(event_rows)

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_users
            SET status = 'do_not_disturb',
                last_ping_at = NOW()
            WHERE uuid = %s
            """,
            (str(user_uuid),),
        )

    resp = api.get(f"{USERS}{user_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "do_not_disturb"


def test_event_retention_prunes_one_cross_table_batch_with_watermarks(api, db):
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    direct_event_uuid = sys_uuid.uuid4()
    broadcast_event_uuid = sys_uuid.uuid4()

    def seed_and_prune(session):
        direct_epoch = session.execute(
            """
            INSERT INTO m_workspace_events (
                uuid, project_id, user_uuid, schema_version,
                object_type, action, payload, created_at, updated_at
            ) VALUES (
                %s, %s, %s, 1, 'user', 'updated', %s::jsonb, %s, %s
            )
            RETURNING epoch_version
            """,
            (
                direct_event_uuid,
                api.project_id,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "user.updated",
                        "uuid": str(api.user_uuid),
                    }
                ),
                now - datetime.timedelta(days=4, minutes=1),
                now - datetime.timedelta(days=4, minutes=1),
            ),
        ).fetchone()["epoch_version"]
        broadcast_epoch = messenger_events.create_broadcast_event(
            api.project_id,
            api.user_uuid,
            [api.user_uuid],
            messenger_events.USER_UPDATED_EVENT,
            {"uuid": str(api.user_uuid)},
            session=session,
            event_uuid=broadcast_event_uuid,
            created_at=now - datetime.timedelta(days=4),
        )[0]
        pruned = sql_canonical_store.prune_expired_events(
            session,
            now,
            retention=datetime.timedelta(days=3),
            batch_size=1,
        )
        return direct_epoch, broadcast_epoch, pruned

    direct_epoch, broadcast_epoch, first_pruned = _run_database_operation(
        seed_and_prune
    )
    assert first_pruned == 1
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                EXISTS(
                    SELECT 1 FROM m_workspace_events WHERE uuid = %s
                ),
                EXISTS(
                    SELECT 1
                    FROM m_workspace_broadcast_message_events_v1
                    WHERE uuid = %s
                ),
                (
                    SELECT pruned_through_epoch_version
                    FROM m_workspace_event_cursors
                    WHERE project_id = %s AND user_uuid = %s
                )
            """,
            (
                direct_event_uuid,
                broadcast_event_uuid,
                api.project_id,
                api.user_uuid,
            ),
        )
        direct_exists, broadcast_exists, pruned_through = cur.fetchone()
    assert direct_exists is False
    assert broadcast_exists is True
    assert pruned_through >= direct_epoch

    second_pruned = _run_database_operation(
        lambda session: sql_canonical_store.prune_expired_events(
            session,
            now,
            retention=datetime.timedelta(days=3),
            batch_size=1,
        )
    )
    assert second_pruned == 1
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                EXISTS(
                    SELECT 1
                    FROM m_workspace_broadcast_message_events_v1
                    WHERE uuid = %s
                ),
                (
                    SELECT pruned_through_epoch_version
                    FROM m_workspace_event_cursors
                    WHERE project_id = %s AND user_uuid = %s
                )
            """,
            (broadcast_event_uuid, api.project_id, api.user_uuid),
        )
        broadcast_exists, pruned_through = cur.fetchone()
    assert broadcast_exists is False
    assert pruned_through >= broadcast_epoch


def test_event_retention_folds_shared_membership_orphan_watermarks(api, db):
    second_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    conftest.seed_workspace_user(
        db,
        second_user_uuid,
        f"user-{second_user_uuid}",
    )
    now = datetime.datetime.now(datetime.timezone.utc)

    def seed_and_prune(session):
        first_epoch = messenger_events.create_broadcast_event(
            api.project_id,
            sys_uuid.uuid4(),
            [api.user_uuid],
            messenger_events.USER_UPDATED_EVENT,
            {"uuid": str(api.user_uuid)},
            session=session,
            created_at=now - datetime.timedelta(days=4, minutes=1),
        )[0]
        second_epoch = messenger_events.create_broadcast_event(
            api.project_id,
            sys_uuid.uuid4(),
            [api.user_uuid, second_user_uuid],
            messenger_events.USER_UPDATED_EVENT,
            {"uuid": str(api.user_uuid)},
            session=session,
            created_at=now - datetime.timedelta(days=4),
        )[0]
        pruned = sql_canonical_store.prune_expired_events(
            session,
            now,
            retention=datetime.timedelta(days=3),
            batch_size=2,
        )
        return first_epoch, second_epoch, pruned

    first_epoch, second_epoch, pruned = _run_database_operation(seed_and_prune)

    assert pruned == 2
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, current_epoch_version, pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = ANY(%s)
            ORDER BY user_uuid
            """,
            (api.project_id, [api.user_uuid, str(second_user_uuid)]),
        )
        cursors = {
            str(user_uuid): (current_epoch, pruned_through)
            for user_uuid, current_epoch, pruned_through in cur.fetchall()
        }
    assert cursors[str(api.user_uuid)] == (
        max(first_epoch, second_epoch),
        max(first_epoch, second_epoch),
    )
    assert cursors[str(second_user_uuid)] == (second_epoch, second_epoch)


def test_workspace_event_payload_identity_backfill_migration(_database, db):
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    clean_user_uuid = sys_uuid.uuid4()
    damaged_user_uuid = sys_uuid.uuid4()
    for workspace_user_uuid in (
        user_uuid,
        clean_user_uuid,
        damaged_user_uuid,
    ):
        conftest.seed_workspace_user(
            db,
            workspace_user_uuid,
            f"user-{workspace_user_uuid}",
        )

    def run_migration(filename, module_name):
        migration_path = conftest.MIGRATIONS_DIR / filename
        spec = importlib.util.spec_from_file_location(module_name, migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migration_step.upgrade(db)

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'message',
                'created',
                jsonb_build_object(
                    'kind', 'message.created',
                    'uuid', %s::text,
                    'stream_uuid', %s::text,
                    'topic_uuid', %s::text,
                    'author_uuid', %s::text,
                    'payload', jsonb_build_object(
                        'kind', 'markdown',
                        'content', 'legacy event'
                    ),
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(user_uuid),
                str(message_uuid),
                str(stream_uuid),
                str(topic_uuid),
                str(user_uuid),
            ),
        )
        message_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'user',
                'updated',
                jsonb_build_object(
                    'kind', 'user.updated',
                    'uuid', %s::text,
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000',
                    'username', 'clean-user',
                    'source', 'iam',
                    'status', 'active',
                    'avatar', 'urn:gravatar:' || md5(%s::text),
                    'last_ping_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(clean_user_uuid),
                str(clean_user_uuid),
                str(clean_user_uuid),
            ),
        )
        clean_user_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO m_workspace_events
                (uuid, project_id, user_uuid, schema_version, object_type, action,
                 payload, created_at, updated_at)
            VALUES (
                %s,
                %s,
                %s,
                1,
                'user',
                'updated',
                jsonb_build_object(
                    'kind', 'user.updated',
                    'project_id', %s::text,
                    'uuid', %s::text,
                    'created_at', '2026-07-02 12:00:00.000000',
                    'updated_at', '2026-07-02 12:00:00.000000',
                    'username', 'damaged-user',
                    'source', 'iam',
                    'status', 'active',
                    'avatar', 'urn:gravatar:' || md5(%s::text),
                    'last_ping_at', '2026-07-02 12:00:00.000000'
                ),
                NOW(),
                NOW()
            )
            RETURNING epoch_version
            """,
            (
                str(sys_uuid.uuid4()),
                str(project_id),
                str(damaged_user_uuid),
                str(project_id),
                str(damaged_user_uuid),
                str(damaged_user_uuid),
            ),
        )
        damaged_user_epoch_version = cur.fetchone()[0]

        cur.execute(
            """
            SELECT payload->>'project_id', payload->>'user_uuid'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (message_epoch_version,),
        )
        assert cur.fetchone() == (None, None)

    run_migration(
        "0061-backfill-workspace-event-payload-identity-fields-f25144.py",
        "migration_0061",
    )

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(message_epoch_version)},
    )
    assert event.payload["project_id"] == str(project_id)
    assert event.payload["user_uuid"] == str(user_uuid)

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(clean_user_epoch_version)},
    )
    assert event.payload["username"] == "clean-user"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'project_id', payload->>'user_uuid'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (message_epoch_version,),
        )
        assert cur.fetchone() == (str(project_id), str(user_uuid))

        cur.execute(
            """
            SELECT payload->>'project_id'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (clean_user_epoch_version,),
        )
        assert cur.fetchone()[0] is None

    run_migration(
        "0062-clean-invalid-workspace-event-payload-project-ids-82eab5.py",
        "migration_0062",
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload->>'project_id'
            FROM m_workspace_events
            WHERE epoch_version = %s
            """,
            (damaged_user_epoch_version,),
        )
        assert cur.fetchone()[0] is None

    event = messenger_models.WorkspaceEvent.objects.get_one(
        filters={"epoch_version": dm_filters.EQ(damaged_user_epoch_version)},
    )
    assert event.payload["username"] == "damaged-user"
    assert project_id in messenger_dm_helpers._get_workspace_event_project_ids()


# --------------------------------------------------------------------------- #
# Files: metadata and local storage
# --------------------------------------------------------------------------- #


def test_file_json_crud_scopes_access_and_deletes_access_rows(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "files-team"
    )
    stream_user = sys_uuid.uuid4()
    outsider_user = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, stream_user)

    resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "example.txt",
            "description": "Example",
            "content_type": "text/plain",
            "size_bytes": 12,
            "hash": "abc",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()
    file_uuid = file["uuid"]
    assert file["name"] == "example.txt"
    assert file["stream_uuid"] == stream_uuid
    assert file["user_uuid"] == str(api.user_uuid)
    assert "project_id" not in file

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
            """,
            (file_uuid,),
        )
        access_user_uuids = {str(row[0]) for row in cur.fetchall()}
    assert access_user_uuids == {str(api.user_uuid), str(stream_user)}

    resp = api.get(FILES, params={"stream_uuid": stream_uuid})
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(FILES, user=stream_user, params={"stream_uuid": stream_uuid})
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(f"{FILES}{file_uuid}", user=stream_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == file_uuid

    resp = api.get(FILES, user=outsider_user, params={"stream_uuid": stream_uuid})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    resp = api.get(f"{FILES}{file_uuid}", user=outsider_user)
    assert resp.status_code == 404, resp.text
    resp = api.get(f"{FILES}{file_uuid}/actions/download", user=outsider_user)
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_file_accesses
                (uuid, project_id, file_uuid, user_uuid, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (project_id, file_uuid, user_uuid) DO NOTHING
            """,
            (
                str(sys_uuid.uuid4()),
                api.project_id,
                file_uuid,
                str(outsider_user),
            ),
        )

    resp = api.get(f"{FILES}{file_uuid}", user=outsider_user)
    assert resp.status_code == 404, resp.text

    resp = api.put(
        f"{FILES}{file_uuid}",
        user=outsider_user,
        json={"name": "not-owner.txt"},
    )
    assert resp.status_code == 404, resp.text

    resp = api.put(
        f"{FILES}{file_uuid}",
        json={"name": "renamed.txt", "description": "Updated"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "renamed.txt"
    assert resp.json()["description"] == "Updated"

    resp = api.delete(f"{FILES}{file_uuid}")
    assert resp.status_code in (200, 204), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_files WHERE uuid = %s),
                (SELECT COUNT(*)
                 FROM m_workspace_file_accesses
                 WHERE file_uuid = %s)
            """,
            (file_uuid, file_uuid),
        )
        file_count, access_count = cur.fetchone()

    assert file_count == 0
    assert access_count == 0


def test_non_public_files_are_scoped_to_the_request_project(api, db):
    current_stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "current-project-files",
    )
    other_project_uuid = sys_uuid.uuid4()
    other_stream_uuid = conftest.seed_user_stream(
        db,
        other_project_uuid,
        api.user_uuid,
        "other-project-files",
    )

    current_response = api.post(
        FILES,
        json={
            "stream_uuid": current_stream_uuid,
            "name": "current.txt",
            "description": "Current project",
            "content_type": "text/plain",
            "size_bytes": 7,
            "hash": "current",
        },
    )
    assert current_response.status_code in (200, 201), current_response.text
    current_file_uuid = current_response.json()["uuid"]

    other_response = api.post(
        FILES,
        project=other_project_uuid,
        json={
            "stream_uuid": other_stream_uuid,
            "name": "other.txt",
            "description": "Other project",
            "content_type": "text/plain",
            "size_bytes": 5,
            "hash": "other",
        },
    )
    assert other_response.status_code in (200, 201), other_response.text
    other_file_uuid = other_response.json()["uuid"]

    response = api.get(FILES, params={"stream_uuid": current_stream_uuid})
    assert response.status_code == 200, response.text
    assert [item["uuid"] for item in response.json()] == [current_file_uuid]

    response = api.get(f"{FILES}{other_file_uuid}")
    assert response.status_code == 404, response.text


def test_file_multipart_upload_writes_local_file(api, db, tmp_path, monkeypatch):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "file-upload-team"
    )
    stream_user = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, stream_user)
    data = b"uploaded file data"
    file_name = "Рабочий документ.txt"

    resp = api.post(
        FILES,
        data={"stream_uuid": stream_uuid},
        files={"file": (file_name, io.BytesIO(data), "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()

    path = file_storage.get_workspace_file_path(
        file_uuid=file["uuid"],
        storage_path=tmp_path,
    )
    assert path.read_bytes() == data
    assert file["name"] == file_name
    assert "storage_type" not in file
    assert "storage_id" not in file
    assert "storage_object_id" not in file
    resp = api.get(f"{FILES}{file['uuid']}/actions/download")
    assert resp.status_code == 200, resp.text
    assert resp.content == data
    assert resp.headers["Content-Type"].startswith("text/plain")
    content_disposition = resp.headers["Content-Disposition"]
    assert 'filename="download.txt"' in content_disposition
    assert (
        "filename*=UTF-8''%D0%A0%D0%B0%D0%B1%D0%BE%D1%87%D0%B8%D0%B9"
        in content_disposition
    )

    resp = api.get(f"{FILES}{file['uuid']}/actions/download", user=stream_user)
    assert resp.status_code == 200, resp.text
    assert resp.content == data

    assert file["size_bytes"] == len(data)
    assert file["hash"] == hashlib.sha256(data).hexdigest()

    resp = api.delete(f"{FILES}{file['uuid']}")
    assert resp.status_code in (200, 204), resp.text
    assert not path.exists()


def test_public_file_multipart_upload_is_visible_to_authenticated_user(
    api, db, tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    other_user_uuid = sys_uuid.uuid4()
    other_project_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        other_user_uuid,
        f"user-{other_user_uuid}",
    )
    data = b"public file data"

    resp = api.post(
        FILES,
        data={"acl": '{"mode":"public"}'},
        files={"file": ("public.txt", io.BytesIO(data), "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()
    assert file.get("stream_uuid") is None

    metadata = file_storage.read_workspace_file_metadata(file["uuid"])
    assert metadata.acl_mode == "public"
    assert metadata.stream_uuid is None

    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM m_workspace_file_accesses WHERE file_uuid = %s",
            (file["uuid"],),
        )
        assert cur.fetchone()[0] == 0

    resp = api.get(
        f"{FILES}{file['uuid']}",
        user=other_user_uuid,
        project=other_project_uuid,
    )
    assert resp.status_code == 200, resp.text
    resp = api.get(
        f"{FILES}{file['uuid']}/actions/download",
        user=other_user_uuid,
        project=other_project_uuid,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == data


# --------------------------------------------------------------------------- #
# Folders: full write path through the real ORM
# --------------------------------------------------------------------------- #


def test_folder_crud_roundtrip(api):
    # create
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    folder_uuid = folder["uuid"]
    assert folder["title"] == "Inbox"
    # hidden fields must not leak
    assert "user_uuid" not in folder
    assert "project_id" not in folder

    # get
    resp = api.get(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == folder_uuid

    # update
    resp = api.put(f"{FOLDERS}{folder_uuid}", json={"title": "Archive"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Archive"

    # delete
    resp = api.delete(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code in (200, 204), resp.text

    # gone
    resp = api.get(f"{FOLDERS}{folder_uuid}")
    assert resp.status_code == 404, resp.text


def test_system_folders_exist_for_user_without_streams(api, db):
    conftest.seed_workspace_user(
        db,
        api.user_uuid,
        f"user-{api.user_uuid}",
    )
    external_account_uuid = sys_uuid.uuid4()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready)
            VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE)
            """,
            (
                str(external_account_uuid),
                api.user_uuid,
                '{"kind":"zulip","server_url":"https://zulip.example"}',
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id)
            VALUES (%s, %s, %s, 'zulip', 'without-streams', '{}'::jsonb,
                    'Without streams', TRUE, %s)
            """,
            (
                str(sys_uuid.uuid4()),
                str(external_account_uuid),
                api.user_uuid,
                api.project_id,
            ),
        )

    resp = api.get(FOLDERS)
    assert resp.status_code == 200, resp.text
    folders_by_uuid = {folder["uuid"]: folder for folder in resp.json()}
    expected_folders = {
        str(messenger_dm_helpers.ALL_CHATS_FOLDER_UUID): "All chats",
        str(messenger_dm_helpers.PERSONAL_FOLDER_UUID): "Personal",
        str(messenger_dm_helpers.CHANNELS_FOLDER_UUID): "Channels",
    }
    assert {
        uuid: folders_by_uuid[uuid]["title"] for uuid in expected_folders
    } == expected_folders
    assert all(folders_by_uuid[uuid]["folder_items"] == [] for uuid in expected_folders)
    assert all(
        folders_by_uuid[uuid]["background_color_value"] == 11184810
        for uuid in expected_folders
    )

    for folder_uuid, title in expected_folders.items():
        resp = api.get(f"{FOLDERS}{folder_uuid}")
        assert resp.status_code == 200, resp.text
        folder = resp.json()
        assert folder["title"] == title
        assert folder["background_color_value"] == 11184810
        assert folder["folder_items"] == []


def test_folder_create_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    epoch_version, user_uuid, payload = rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.created"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Inbox"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert payload["folder_items"] == []

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.created"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["title"] == "Inbox"


def test_folder_update_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    resp = api.put(f"{FOLDERS}{folder['uuid']}", json={"title": "Archive"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "Archive"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.updated"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Archive"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert payload["folder_items"] == []

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["title"] == "Archive"


def test_folder_delete_writes_realtime_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()

    resp = api.delete(f"{FOLDERS}{folder['uuid']}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{FOLDERS}{folder['uuid']}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload == {
        "kind": "folder.deleted",
        "uuid": folder["uuid"],
    }

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.deleted"
    assert event["payload"] == payload


def test_folder_item_create_writes_folder_updated_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )

    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()
    assert item["folder_uuid"] == folder["uuid"]
    assert item["stream_uuid"] == stream_uuid

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    epoch_version, user_uuid, payload = rows[1]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "folder.updated"
    assert payload["uuid"] == folder["uuid"]
    assert payload["title"] == "Inbox"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["unread_count"] == 0
    assert len(payload["folder_items"]) == 1
    assert payload["folder_items"][0]["uuid"] == item["uuid"]
    assert payload["folder_items"][0]["folder_uuid"] == folder["uuid"]
    assert payload["folder_items"][0]["stream_uuid"] == stream_uuid

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["uuid"] == folder["uuid"]
    assert event["payload"]["folder_items"][0]["stream_uuid"] == stream_uuid


def test_folder_item_delete_writes_deleted_event(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )
    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()

    resp = api.delete(f"{FOLDER_ITEMS}{item['uuid']}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{FOLDER_ITEMS}{item['uuid']}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 4
    epoch_version, user_uuid, payload = rows[2]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload == {
        "kind": "folder_item.deleted",
        "uuid": item["uuid"],
    }

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "folder_item"
    assert event["payload"]["kind"] == "folder_item.deleted"
    assert event["payload"] == payload

    _, folder_user_uuid, folder_payload = rows[3]
    assert str(folder_user_uuid) == str(api.user_uuid)
    assert folder_payload["kind"] == "folder.updated"
    assert folder_payload["uuid"] == folder["uuid"]
    assert folder_payload["folder_items"] == []


def test_folder_item_pin_unpin_actions_write_folder_updated_events(api, db):
    resp = api.post(FOLDERS, json={"title": "Inbox"})
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "standups"
    )
    resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text
    item = resp.json()
    assert item.get("pinned_at") is None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/pin/invoke")
    assert resp.status_code == 200, resp.text
    pinned_item = resp.json()
    assert pinned_item["uuid"] == item["uuid"]
    assert pinned_item["pinned_at"] is not None

    resp = api.post(f"{FOLDER_ITEMS}{item['uuid']}/actions/unpin/invoke")
    assert resp.status_code == 200, resp.text
    unpinned_item = resp.json()
    assert unpinned_item["uuid"] == item["uuid"]
    assert unpinned_item.get("pinned_at") is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 4
    _, user_uuid, pin_payload = rows[2]
    assert str(user_uuid) == str(api.user_uuid)
    assert pin_payload["kind"] == "folder.updated"
    assert pin_payload["uuid"] == folder["uuid"]
    assert pin_payload["folder_items"][0]["uuid"] == item["uuid"]
    assert pin_payload["folder_items"][0]["pinned_at"] is not None

    epoch_version, user_uuid, unpin_payload = rows[3]
    assert str(user_uuid) == str(api.user_uuid)
    assert unpin_payload["kind"] == "folder.updated"
    assert unpin_payload["uuid"] == folder["uuid"]
    assert unpin_payload["folder_items"][0]["uuid"] == item["uuid"]
    assert unpin_payload["folder_items"][0].get("pinned_at") is None

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": unpin_payload,
        }
    )
    assert event["object_type"] == "folder"
    assert event["payload"]["kind"] == "folder.updated"
    assert event["payload"]["folder_items"][0].get("pinned_at") is None


def test_system_folder_item_pin_unpin_actions_materialize_user_item(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "system-pins"
    )
    item_uuid = f"00{stream_uuid[2:]}"

    resp = api.post(f"{FOLDER_ITEMS}{item_uuid}/actions/pin/invoke")
    assert resp.status_code == 200, resp.text
    pinned_item = resp.json()
    assert pinned_item["uuid"] == item_uuid
    assert pinned_item["stream_uuid"] == stream_uuid
    assert pinned_item["folder_uuid"] == str(messenger_dm_helpers.ALL_CHATS_FOLDER_UUID)
    assert pinned_item["pinned_at"] is not None

    resp = api.get(f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}")
    assert resp.status_code == 200, resp.text
    folder_item = [
        item for item in resp.json()["folder_items"] if item["uuid"] == item_uuid
    ][0]
    assert folder_item["pinned_at"] is not None

    resp = api.post(f"{FOLDER_ITEMS}{item_uuid}/actions/unpin/invoke")
    assert resp.status_code == 200, resp.text
    unpinned_item = resp.json()
    assert unpinned_item["uuid"] == item_uuid
    assert unpinned_item.get("pinned_at") is None

    resp = api.get(f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}")
    assert resp.status_code == 200, resp.text
    folder_item = [
        item for item in resp.json()["folder_items"] if item["uuid"] == item_uuid
    ][0]
    assert folder_item.get("pinned_at") is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_folder_items
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid = %s
                AND folder_uuid = %s
                AND stream_uuid = %s
            """,
            (
                item_uuid,
                api.project_id,
                api.user_uuid,
                str(messenger_dm_helpers.ALL_CHATS_FOLDER_UUID),
                stream_uuid,
            ),
        )
        item_count = cur.fetchone()[0]

    assert item_count == 1


def test_folders_are_scoped_to_the_authenticated_user(api):
    other_user = sys_uuid.uuid4()
    system_folder_titles = {"All chats", "Personal", "Channels"}

    api.post(FOLDERS, json={"title": "mine"})
    api.post(FOLDERS, json={"title": "theirs"}, user=other_user)

    titles = [
        f["title"]
        for f in api.get(FOLDERS).json()
        if f["title"] not in system_folder_titles
    ]
    assert titles == ["mine"]

    other_titles = [
        f["title"]
        for f in api.get(FOLDERS, user=other_user).json()
        if f["title"] not in system_folder_titles
    ]
    assert other_titles == ["theirs"]


# --------------------------------------------------------------------------- #
# Streams: composite primary key controller (read paths)
# --------------------------------------------------------------------------- #


def test_streams_list_is_scoped_to_user(api, db):
    other_user = sys_uuid.uuid4()
    for i in range(3):
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, f"mine-{i}")
    for i in range(2):
        conftest.seed_user_stream(db, api.project_id, other_user, f"other-{i}")

    resp = api.get(STREAMS)
    assert resp.status_code == 200, resp.text
    names = sorted(s["name"] for s in resp.json())
    assert names == ["mine-0", "mine-1", "mine-2"]


def test_stream_get_by_uuid_is_scoped(api, db):
    other_user = sys_uuid.uuid4()
    mine = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "mine")
    theirs = conftest.seed_user_stream(db, api.project_id, other_user, "theirs")

    # own row is visible
    resp = api.get(f"{STREAMS}{mine}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == mine
    assert resp.json()["name"] == "mine"

    # another user's row, addressed by its real uuid, is not found
    resp = api.get(f"{STREAMS}{theirs}")
    assert resp.status_code == 404, resp.text


def test_stream_create_writes_realtime_event(api, db):
    resp = api.post(
        STREAMS,
        json={
            "name": "Engineering",
            "description": "Engineering workspace",
            "source_name": "native",
            "source": {"kind": "native"},
            "invite_only": False,
            "announce": False,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    stream = resp.json()

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 4
    epoch_version, user_uuid, payload = rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["kind"] == "stream.created"
    assert payload["uuid"] == stream["uuid"]
    assert payload["name"] == "Engineering"
    assert payload["description"] == "Engineering workspace"
    assert payload["user_uuid"] == str(api.user_uuid)
    assert payload["project_id"] == str(api.project_id)
    assert payload["owner"] == str(api.user_uuid)
    assert payload["role"] == "owner"
    assert payload["notification_mode"] == "all_messages"
    assert payload["unread_count"] == 0
    assert stream.get("last_message_uuid") is None
    assert payload.get("last_message_uuid") is None
    assert stream["default_topic_uuid"] is not None
    assert payload["default_topic_uuid"] == stream["default_topic_uuid"]
    assert 0 <= stream["color"] <= 0xFFFFFF
    assert payload["color"] == stream["color"]
    assert payload["source_name"] == "native"
    assert payload["source"] == {"kind": "native"}

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": api.user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "stream"
    assert event["payload"]["kind"] == "stream.created"
    assert event["payload"]["uuid"] == stream["uuid"]
    assert event["payload"]["name"] == "Engineering"
    assert event["payload"]["role"] == "owner"
    assert event["payload"]["notification_mode"] == "all_messages"
    assert event["payload"]["color"] == stream["color"]
    assert event["payload"].get("last_message_uuid") is None
    assert event["payload"]["default_topic_uuid"] == stream["default_topic_uuid"]

    topic_epoch_version, topic_user_uuid, topic_payload = rows[3]
    assert str(topic_user_uuid) == str(api.user_uuid)
    assert topic_payload["kind"] == "topic.created"
    assert topic_payload["name"] == "General Topic"
    assert topic_payload["stream_uuid"] == stream["uuid"]
    assert topic_payload["uuid"] == stream["default_topic_uuid"]
    assert topic_payload["user_uuid"] == str(api.user_uuid)
    assert topic_payload["project_id"] == str(api.project_id)
    assert topic_payload["is_default"] is True
    assert topic_payload["is_done"] is False
    assert topic_payload["unread_count"] == 0
    assert topic_payload["notification_mode"] == "default"
    assert topic_payload.get("last_message_uuid") is None
    assert 0 <= topic_payload["color"] <= 0xFFFFFF

    topic_event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": topic_epoch_version,
            "user_uuid": api.user_uuid,
            "payload": topic_payload,
        }
    )
    assert topic_event["object_type"] == "topic"
    assert topic_event["payload"]["kind"] == "topic.created"
    assert topic_event["payload"]["uuid"] == topic_payload["uuid"]
    assert topic_event["payload"]["name"] == "General Topic"
    assert topic_event["payload"]["is_default"] is True

    folder_events = [row[2] for row in rows[1:3]]
    assert [payload["kind"] for payload in folder_events] == [
        "folder.updated",
        "folder.updated",
    ]
    assert [payload["uuid"] for payload in folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert [payload["title"] for payload in folder_events] == [
        "All chats",
        "Channels",
    ]
    assert all(payload["user_uuid"] == str(api.user_uuid) for payload in folder_events)


def test_stream_notifications_are_user_scoped_and_write_event(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "notifications-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)

    resp = api.get(f"{STREAMS}{stream_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    resp = api.get(f"{STREAMS}{stream_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_epoch = cur.fetchone()[0]

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "mentions_only"},
    )
    assert resp.status_code == 200, resp.text
    stream = resp.json()
    assert stream["notification_mode"] == "mentions_only"

    resp = api.get(f"{STREAMS}{stream_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "all_messages"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, notification_mode
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, stream_uuid),
        )
        bindings = cur.fetchall()
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
                AND payload->>'kind' = 'stream.updated'
                AND payload->>'uuid' = %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_epoch, stream_uuid),
        )
        event_rows = cur.fetchall()

    assert dict((str(user_uuid), mode) for user_uuid, mode in bindings) == {
        str(api.user_uuid): "mentions_only",
        str(other_user): "all_messages",
    }
    assert len(event_rows) == 1
    epoch_version, user_uuid, payload = event_rows[0]
    assert str(user_uuid) == str(api.user_uuid)
    assert payload["notification_mode"] == "mentions_only"

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": epoch_version,
            "user_uuid": user_uuid,
            "payload": payload,
        }
    )
    assert event["object_type"] == "stream"
    assert event["payload"]["kind"] == "stream.updated"
    assert event["payload"]["notification_mode"] == "mentions_only"


def test_stream_delete_cascades_data_and_writes_realtime_events(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "delete-me"
    )
    conftest.seed_user_stream(db, api.project_id, api.user_uuid, "keep-owner")
    conftest.seed_user_stream(db, api.project_id, other_user, "keep-other")
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    conftest.seed_stream_topic_flags(db, topic_uuid, api.user_uuid, api.project_id)

    folder_resp = api.post(FOLDERS, json={"title": "Pinned"})
    assert folder_resp.status_code in (200, 201), folder_resp.text
    folder_uuid = folder_resp.json()["uuid"]
    item_resp = api.post(
        FOLDER_ITEMS,
        json={
            "folder_uuid": folder_uuid,
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert item_resp.status_code in (200, 201), item_resp.text

    message_resp = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "delete cascade check",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message = message_resp.json()
    message_uuid = message["uuid"]
    assert message["reactions"] == {}
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_workspace_message_reactions
                (uuid, project_id, created_at, updated_at, message_uuid,
                 user_uuid, emoji_name)
            VALUES (%s, %s, NOW(), NOW(), %s, %s, 'thumbs_up')
            """,
            (str(sys_uuid.uuid4()), api.project_id, message_uuid, api.user_uuid),
        )
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{STREAMS}{stream_uuid}")
    assert resp.status_code in (200, 204), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_streams
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_stream_topics
                 WHERE stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_topic_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_message_reactions
                 WHERE message_uuid = %s),
                (SELECT COUNT(*) FROM m_folder_items
                 WHERE stream_uuid = %s)
            """,
            (
                stream_uuid,
                stream_uuid,
                topic_uuid,
                stream_uuid,
                message_uuid,
                message_uuid,
                stream_uuid,
            ),
        )
        counts = cur.fetchone()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        event_rows = cur.fetchall()

    assert counts == (0, 0, 0, 0, 0, 0, 0)
    events_by_user = {}
    for user_uuid, payload in event_rows:
        events_by_user.setdefault(str(user_uuid), []).append(payload)

    assert set(events_by_user) == {str(api.user_uuid), str(other_user)}
    assert [event["kind"] for event in events_by_user[str(api.user_uuid)]] == [
        "stream.deleted",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert [event["kind"] for event in events_by_user[str(other_user)]] == [
        "stream.deleted",
        "folder.updated",
        "folder.updated",
    ]
    assert events_by_user[str(api.user_uuid)][0]["uuid"] == stream_uuid
    assert events_by_user[str(other_user)][0]["uuid"] == stream_uuid

    owner_folder_events = events_by_user[str(api.user_uuid)][1:]
    other_folder_events = events_by_user[str(other_user)][1:]
    assert [event["uuid"] for event in owner_folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
        folder_uuid,
    ]
    assert [event["uuid"] for event in other_folder_events] == [
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000002",
    ]
    for event in owner_folder_events + other_folder_events:
        assert all(item["stream_uuid"] != stream_uuid for item in event["folder_items"])


def test_direct_stream_create_is_idempotent_and_creates_owner_bindings(api, db):
    direct_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        direct_user_uuid,
        f"user-{direct_user_uuid}",
    )
    expected_index = ":".join(sorted([str(api.user_uuid), str(direct_user_uuid)]))
    payload = {
        "name": "Direct",
        "description": "Private workspace",
        "source_name": "native",
        "source": {"kind": "native"},
        "direct_user_uuid": str(direct_user_uuid),
    }

    first_resp = api.post(STREAMS, json=payload)
    assert first_resp.status_code in (200, 201), first_resp.text
    first_stream = first_resp.json()

    second_resp = api.post(STREAMS, json=payload)
    assert second_resp.status_code in (200, 201), second_resp.text
    second_stream = second_resp.json()

    matching_private_resp = api.post(
        STREAMS,
        json={**payload, "private": True},
    )
    assert matching_private_resp.status_code in (200, 201), matching_private_resp.text
    assert matching_private_resp.json()["uuid"] == first_stream["uuid"]

    conflicting_private_resp = api.post(
        STREAMS,
        json={**payload, "private": False},
    )
    assert conflicting_private_resp.status_code == 400, conflicting_private_resp.text
    assert conflicting_private_resp.json()["code"] == 400001011

    different_source_resp = api.post(
        STREAMS,
        json={
            **payload,
            "source_name": "zulip",
            "source": {
                "kind": "zulip",
                "stream_id": 17,
                "server_url": "https://zulip.example.invalid",
                "topic_name": None,
                "message_id": None,
            },
        },
    )
    assert different_source_resp.status_code == 400, different_source_resp.text
    assert different_source_resp.json()["code"] == 400001011

    unchanged_resp = api.get(f"{STREAMS}{first_stream['uuid']}")
    assert unchanged_resp.status_code == 200, unchanged_resp.text
    unchanged_stream = unchanged_resp.json()

    assert second_stream["uuid"] == first_stream["uuid"]
    assert unchanged_stream["source_name"] == "native"
    assert unchanged_stream["source"] == {"kind": "native"}
    assert unchanged_stream["private"] is True
    assert first_stream["private"] is True
    assert first_stream["direct_user_uuid"] == str(direct_user_uuid)
    assert "private_index" not in first_stream

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT private_index
            FROM m_workspace_streams
            WHERE project_id = %s
                AND uuid = %s
            """,
            (api.project_id, first_stream["uuid"]),
        )
        stored_private_index = cur.fetchone()[0]
        cur.execute(
            """
            SELECT uuid, user_uuid, role
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, first_stream["uuid"]),
        )
        bindings = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'stream.created'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, first_stream["uuid"]),
        )
        events = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'folder.updated'
            ORDER BY user_uuid, payload->>'uuid'
            """,
            (api.project_id,),
        )
        folder_events = cur.fetchall()

    assert stored_private_index == expected_index
    assert [(str(user_uuid), role) for _uuid, user_uuid, role in bindings] == [
        (user_uuid, "owner")
        for user_uuid in sorted([str(api.user_uuid), str(direct_user_uuid)])
    ]
    assert [str(user_uuid) for user_uuid, _payload in events] == sorted(
        [str(api.user_uuid), str(direct_user_uuid)]
    )
    assert [
        (str(user_uuid), payload["uuid"], payload["title"])
        for user_uuid, payload in folder_events
    ] == [
        (user_uuid, folder_uuid, title)
        for user_uuid in sorted([str(api.user_uuid), str(direct_user_uuid)])
        for folder_uuid, title in (
            ("00000000-0000-0000-0000-000000000000", "All chats"),
            ("00000000-0000-0000-0000-000000000001", "Personal"),
        )
    ]

    third_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        third_user_uuid,
        f"user-{third_user_uuid}",
    )
    resp = api.post(
        f"{STREAMS}{first_stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(third_user_uuid)]},
    )
    assert resp.status_code == 400, resp.text

    first_binding_uuid = bindings[0][0]
    resp = api.put(
        f"{STREAM_BINDINGS}{first_binding_uuid}",
        json={"role": "member"},
    )
    assert resp.status_code == 400, resp.text

    resp = api.delete(f"{STREAM_BINDINGS}{first_binding_uuid}")
    assert resp.status_code == 400, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, role
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, first_stream["uuid"]),
        )
        unchanged_bindings = cur.fetchall()
    assert [(str(user_uuid), role) for user_uuid, role in unchanged_bindings] == [
        (user_uuid, "owner")
        for user_uuid in sorted([str(api.user_uuid), str(direct_user_uuid)])
    ]


def test_self_direct_stream_is_stable_private_singleton_with_message_history(api, db):
    expected_stream_uuid = messenger_dm_helpers.deterministic_direct_stream_uuid(
        api.project_id,
        api.user_uuid,
        api.user_uuid,
    )
    expected_index = f"{api.user_uuid}:{api.user_uuid}"
    payload = {
        "name": "Personal notes",
        "description": "",
        "source_name": "native",
        "source": {"kind": "native"},
        "direct_user_uuid": api.user_uuid,
    }

    mismatched_source_resp = api.post(
        STREAMS,
        json={
            **payload,
            "source": {
                "kind": "zulip",
                "stream_id": 1,
                "server_url": "https://zulip.example.invalid",
                "topic_name": None,
                "message_id": None,
            },
        },
    )
    assert mismatched_source_resp.status_code == 400, mismatched_source_resp.text
    assert mismatched_source_resp.json()["code"] == 400001010

    first_resp = api.post(STREAMS, json=payload)
    assert first_resp.status_code in (200, 201), first_resp.text
    first_stream = first_resp.json()
    second_resp = api.post(STREAMS, json=payload)
    assert second_resp.status_code in (200, 201), second_resp.text
    second_stream = second_resp.json()

    assert first_stream["uuid"] == str(expected_stream_uuid)
    assert second_stream["uuid"] == first_stream["uuid"]
    assert first_stream["private"] is True
    assert first_stream["direct_user_uuid"] == api.user_uuid
    assert first_stream["owner"] == api.user_uuid
    assert first_stream["user_uuid"] == api.user_uuid
    assert first_stream["role"] == "owner"
    assert "private_index" not in first_stream

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT private_index, direct_user_uuid, default_topic_uuid
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, first_stream["uuid"]),
        )
        stored_index, direct_user_uuid, default_topic_uuid = cur.fetchone()
        cur.execute(
            """
            SELECT uuid, user_uuid, role
            FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, first_stream["uuid"]),
        )
        bindings = cur.fetchall()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, first_stream["uuid"]),
        )
        topic_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND payload->>'kind' = 'stream.created'
              AND payload->>'uuid' = %s
            """,
            (api.project_id, api.user_uuid, first_stream["uuid"]),
        )
        created_event_count = cur.fetchone()[0]

    assert stored_index == expected_index
    assert str(direct_user_uuid) == api.user_uuid
    assert str(default_topic_uuid) == first_stream["default_topic_uuid"]
    assert [(str(user_uuid), role) for _uuid, user_uuid, role in bindings] == [
        (api.user_uuid, "owner")
    ]
    assert topic_count == 1
    assert created_event_count == 1

    message_resp = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": first_stream["uuid"],
            "topic_uuid": first_stream["default_topic_uuid"],
            "payload": {"kind": "markdown", "content": "remember this"},
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]
    history_resp = api.get(
        MESSAGES,
        params={"stream_uuid": first_stream["uuid"]},
    )
    assert history_resp.status_code == 200, history_resp.text
    assert [message["uuid"] for message in history_resp.json()] == [message_uuid]

    third_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, third_user_uuid, f"user-{third_user_uuid}")
    for identity_update in (
        {"source_name": "native"},
        {"source": {"kind": "native"}},
        {"direct_user_uuid": str(third_user_uuid)},
        {"private": False},
        {"private_index": f"{api.user_uuid}:{third_user_uuid}"},
    ):
        identity_update_resp = api.put(
            f"{STREAMS}{first_stream['uuid']}",
            json=identity_update,
        )
        assert identity_update_resp.status_code == 400, identity_update_resp.text

    add_resp = api.post(
        f"{STREAMS}{first_stream['uuid']}/actions/add_users/invoke",
        json={"member": [str(third_user_uuid)]},
    )
    assert add_resp.status_code == 400, add_resp.text
    binding_uuid = str(bindings[0][0])
    role_resp = api.put(
        f"{STREAM_BINDINGS}{binding_uuid}",
        json={"role": "member"},
    )
    assert role_resp.status_code == 400, role_resp.text
    binding_delete_resp = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert binding_delete_resp.status_code == 400, binding_delete_resp.text
    stream_delete_resp = api.delete(f"{STREAMS}{first_stream['uuid']}")
    assert stream_delete_resp.status_code == 400, stream_delete_resp.text

    reloaded_message = api.get(f"{MESSAGES}{message_uuid}")
    assert reloaded_message.status_code == 200, reloaded_message.text
    assert reloaded_message.json()["payload"]["content"] == "remember this"


def test_concurrent_self_direct_ensure_creates_one_canonical_stream(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    conftest.seed_workspace_user(db, user_uuid, f"user-{user_uuid}")
    stream_uuid = messenger_dm_helpers.deterministic_direct_stream_uuid(
        project_id,
        user_uuid,
        user_uuid,
    )
    workers_ready = threading.Barrier(2)

    def ensure_self_stream():
        workers_ready.wait(timeout=5)
        return _run_database_operation(
            lambda session: messenger_dm_helpers.get_or_create_workspace_user_stream(
                project_id=project_id,
                user_uuid=user_uuid,
                uuid=stream_uuid,
                name="Personal notes",
                description="",
                source_name="native",
                source=messenger_models.NativeSource(),
                direct_user_uuid=user_uuid,
                session=session,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: ensure_self_stream(), range(2)))

    assert [str(stream.uuid) for stream in results] == [
        str(stream_uuid),
        str(stream_uuid),
    ]
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_streams
                 WHERE project_id = %s AND uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_stream_bindings
                 WHERE project_id = %s AND stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_stream_topics
                 WHERE project_id = %s AND stream_uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_events
                 WHERE project_id = %s
                   AND payload->>'kind' = 'stream.created'
                   AND payload->>'uuid' = %s)
            """,
            (
                project_id,
                stream_uuid,
                project_id,
                stream_uuid,
                project_id,
                stream_uuid,
                project_id,
                str(stream_uuid),
            ),
        )
        assert cur.fetchone() == (1, 1, 1, 1)


def test_stream_binding_create_notifies_added_user(api, workspace_api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Engineering",
    )
    target_user_uuid = sys_uuid.uuid4()
    second_target_user_uuid = sys_uuid.uuid4()
    for target_uuid in (target_user_uuid, second_target_user_uuid):
        conftest.seed_workspace_user(
            db,
            target_uuid,
            f"user-{target_uuid}",
        )
    file_resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "roadmap.txt",
            "description": "Roadmap",
            "content_type": "text/plain",
            "size_bytes": 7,
            "hash": "hash",
        },
    )
    assert file_resp.status_code in (200, 201), file_resp.text
    file_uuid = file_resp.json()["uuid"]

    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={
            "member": [
                str(target_user_uuid),
                str(second_target_user_uuid),
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
            """,
            (file_uuid,),
        )
        access_user_uuids = {str(row[0]) for row in cur.fetchall()}
    assert access_user_uuids == {
        str(api.user_uuid),
        str(target_user_uuid),
        str(second_target_user_uuid),
    }

    for target_uuid in (target_user_uuid, second_target_user_uuid):
        resp = api.get(f"{FILES}{file_uuid}", user=target_uuid)
        assert resp.status_code == 200, resp.text
        assert resp.json()["uuid"] == file_uuid

    for target_uuid in (target_user_uuid, second_target_user_uuid):
        workspace_api.project_id = api.project_id
        workspace_api.user_uuid = str(target_uuid)
        response = workspace_api.get(
            EVENTS,
            params={"page_limit": 100},
        )
        assert response.status_code == 200, response.text
        events = [event["payload"] for event in response.json()]

        assert [event["kind"] for event in events] == [
            "stream.created",
            "folder.updated",
            "folder.updated",
        ]
        assert events[0]["uuid"] == stream_uuid
        assert events[0]["user_uuid"] == str(target_uuid)
        assert events[0]["role"] == "member"
        assert events[0]["notification_mode"] == "all_messages"
        assert [(event["uuid"], event["title"]) for event in events[1:]] == [
            ("00000000-0000-0000-0000-000000000000", "All chats"),
            ("00000000-0000-0000-0000-000000000002", "Channels"),
        ]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND user_uuid = %s
            ORDER BY epoch_version
            """,
            (api.project_id, api.user_uuid),
        )
        owner_events = [row[0] for row in cur.fetchall()]

    assert [event["kind"] for event in owner_events] == [
        "file.created",
        "stream_bindings.created",
    ]
    binding_event = owner_events[1]
    assert binding_event["uuid"] == stream_uuid
    assert [binding["user_uuid"] for binding in binding_event["items"]] == [
        str(target_user_uuid),
        str(second_target_user_uuid),
    ]
    assert {binding["who_uuid"] for binding in binding_event["items"]} == {
        str(api.user_uuid)
    }
    assert {binding["role"] for binding in binding_event["items"]} == {"member"}
    assert {binding["notification_mode"] for binding in binding_event["items"]} == {
        "all_messages"
    }


def test_stream_binding_create_starts_with_read_history_boundary(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Membership history boundary",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    target_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        target_user_uuid,
        f"user-{target_user_uuid}",
    )
    historical = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "before membership"},
        },
    )
    assert historical.status_code == 201, historical.text

    added = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(target_user_uuid)]},
    )
    assert added.status_code in (200, 201), added.text

    visible_history = api.get(
        f"{MESSAGES}{historical.json()['uuid']}",
        user=target_user_uuid,
    )
    assert visible_history.status_code == 200, visible_history.text
    assert visible_history.json()["read"] is True
    visible_stream = api.get(f"{STREAMS}{stream_uuid}", user=target_user_uuid)
    assert visible_stream.status_code == 200, visible_stream.text
    assert visible_stream.json()["unread_count"] == 0
    visible_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=target_user_uuid)
    assert visible_topic.status_code == 200, visible_topic.text
    assert visible_topic.json()["unread_count"] == 0

    new_message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "after membership"},
        },
    )
    assert new_message.status_code == 201, new_message.text
    visible_new_message = api.get(
        f"{MESSAGES}{new_message.json()['uuid']}",
        user=target_user_uuid,
    )
    assert visible_new_message.status_code == 200, visible_new_message.text
    assert visible_new_message.json()["read"] is False
    visible_stream = api.get(f"{STREAMS}{stream_uuid}", user=target_user_uuid)
    assert visible_stream.json()["unread_count"] == 1
    visible_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=target_user_uuid)
    assert visible_topic.json()["unread_count"] == 1


def test_provider_stream_membership_add_remove_duplicate_and_readd_queue(api, db):
    _enable_zulip_policy(db)
    bridge_instance_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    target_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        target_user_uuid,
        f"user-{target_user_uuid}",
    )
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Provider membership queue",
    )
    capability = {
        "messenger.membership.write": {
            "available": True,
            "revision": 1,
            "limits": {},
        }
    }
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, %s::jsonb
            )
            """,
            (
                str(account_uuid),
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.invalid",
                    }
                ),
                json.dumps(capability),
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities,
                catalog_capabilities
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                'Provider membership queue', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                str(chat_uuid),
                str(account_uuid),
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "chat_type": "channel",
                        "participants": [],
                        "topics": [],
                    }
                ),
                api.project_id,
                stream_uuid,
                json.dumps(capability),
                json.dumps(capability),
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_instance_uuid),
                        }
                    }
                ),
            ),
        )
        cur.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_instance_uuid),
        )
        cur.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:42'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(
                    {
                        "kind": "zulip",
                        "stream_id": 42,
                        "server_url": "https://zulip.example.invalid",
                        "source_scope": str(account_uuid),
                    }
                ),
                str(account_uuid),
                api.project_id,
                stream_uuid,
            ),
        )

    added = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(target_user_uuid)]},
    )
    assert added.status_code in (200, 201), added.text
    duplicate = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(target_user_uuid)]},
    )
    assert duplicate.status_code in (200, 201), duplicate.text
    binding_uuid = added.json()[0]["uuid"]

    removed = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert removed.status_code in (200, 204), removed.text
    readded = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(target_user_uuid)]},
    )
    assert readded.status_code in (200, 201), readded.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT operation_kind, payload, bridge_instance_uuid
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
            ORDER BY sequence
            """,
            (str(account_uuid),),
        )
        operations = cur.fetchall()
    assert [operation[0] for operation in operations] == [
        "membership.add",
        "membership.remove",
        "membership.add",
    ]
    assert {
        (operation[1]["stream_uuid"], operation[1]["user_uuid"])
        for operation in operations
    } == {(stream_uuid, str(target_user_uuid))}
    assert {operation[2] for operation in operations} == {bridge_instance_uuid}

    blocked_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        blocked_user_uuid,
        f"user-{blocked_user_uuid}",
    )
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_external_provider_policies_v1
            SET emergency_suspended = TRUE
            WHERE provider = 'zulip'
            """
        )
    db.commit()

    blocked = api.post(
        f"{STREAMS}{stream_uuid}/actions/add_users/invoke",
        json={"member": [str(blocked_user_uuid)]},
    )
    assert blocked.status_code == 403, blocked.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, blocked_user_uuid),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
            """,
            (account_uuid,),
        )
        assert cur.fetchone()[0] == 3

    suspended_remove = api.delete(f"{STREAM_BINDINGS}{readded.json()[0]['uuid']}")
    assert suspended_remove.status_code in (200, 204), suspended_remove.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT operation_kind
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
            ORDER BY sequence
            """,
            (account_uuid,),
        )
        assert [row[0] for row in cur.fetchall()] == [
            "membership.add",
            "membership.remove",
            "membership.add",
            "membership.remove",
        ]

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_instance_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    blocked_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=10,
            lease_seconds=30,
        )
    )
    assert blocked_lease["operations"] == []

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_external_provider_policies_v1
            SET emergency_suspended = FALSE
            WHERE provider = 'zulip'
            """
        )
    db.commit()
    resumed_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=10,
            lease_seconds=30,
        )
    )
    assert "membership.remove" in {
        operation["operation_kind"] for operation in resumed_lease["operations"]
    }


def test_stream_binding_delete_notifies_removed_user(api, db):
    target_user_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Remove user team",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        target_user_uuid,
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT uuid
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, str(target_user_uuid)),
        )
        binding_uuid = cur.fetchone()[0]

    file_resp = api.post(
        FILES,
        json={
            "stream_uuid": stream_uuid,
            "name": "handoff.txt",
            "description": "Handoff",
            "content_type": "text/plain",
            "size_bytes": 8,
            "hash": "hash",
        },
    )
    assert file_resp.status_code in (200, 201), file_resp.text
    file_uuid = file_resp.json()["uuid"]

    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 200, resp.text

    resp = api.post(
        FOLDERS,
        user=target_user_uuid,
        json={"title": "Watched"},
    )
    assert resp.status_code in (200, 201), resp.text
    folder = resp.json()
    resp = api.post(
        FOLDER_ITEMS,
        user=target_user_uuid,
        json={
            "folder_uuid": folder["uuid"],
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert resp.status_code in (200, 201), resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{STREAMS}{stream_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text
    resp = api.get(f"{FILES}{file_uuid}", user=target_user_uuid)
    assert resp.status_code == 404, resp.text
    resp = api.get(f"{FILES}{file_uuid}")
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE uuid = %s
            """,
            (binding_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s
                AND user_uuid = %s
            """,
            (file_uuid, str(target_user_uuid)),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        event_rows = cur.fetchall()

    assert [str(row[0]) for row in event_rows] == [
        str(target_user_uuid),
        str(api.user_uuid),
        str(target_user_uuid),
        str(target_user_uuid),
        str(target_user_uuid),
    ]
    events = [row[1] for row in event_rows]
    assert [event["kind"] for event in events] == [
        "stream.deleted",
        "stream_binding.deleted",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert events[0]["uuid"] == stream_uuid
    assert events[1] == {
        "kind": "stream_binding.deleted",
        "uuid": str(binding_uuid),
        "stream_uuid": stream_uuid,
        "user_uuid": str(target_user_uuid),
    }
    assert [(event["uuid"], event["title"]) for event in events[2:]] == [
        ("00000000-0000-0000-0000-000000000000", "All chats"),
        ("00000000-0000-0000-0000-000000000002", "Channels"),
        (folder["uuid"], "Watched"),
    ]
    for event in events[2:]:
        assert all(item["stream_uuid"] != stream_uuid for item in event["folder_items"])


@pytest.mark.parametrize(
    "compact_events",
    [False, True],
    ids=["direct", "broadcast"],
)
def test_stream_binding_delete_revokes_queued_provider_message_events(
    api,
    workspace_api,
    db,
    compact_events,
):
    removed_user_uuid = sys_uuid.uuid4()
    external_account_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Revoked provider backlog",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        removed_user_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE
            )
            """,
            (
                str(external_account_uuid),
                str(removed_user_uuid),
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', '{}'::jsonb,
                'Revoked provider backlog', TRUE, %s, %s
            )
            """,
            (
                str(sys_uuid.uuid4()),
                str(external_account_uuid),
                str(removed_user_uuid),
                api.project_id,
                stream_uuid,
            ),
        )
        cur.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:42'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(
                    {
                        "kind": "zulip",
                        "stream_id": 42,
                        "server_url": "https://zulip.example.test",
                        "source_scope": str(external_account_uuid),
                    }
                ),
                str(external_account_uuid),
                api.project_id,
                stream_uuid,
            ),
        )
        cur.execute(
            """
            SELECT uuid
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
              AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, str(removed_user_uuid)),
        )
        binding_uuid = cur.fetchone()[0]

    message_uuid = sys_uuid.uuid4()
    _run_database_operation(
        lambda session: messenger_dm_helpers.create_workspace_user_message(
            uuid=message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(stream_uuid),
            topic_uuid=sys_uuid.UUID(topic_uuid),
            payload=message_payloads.MarkdownPayload(content="revoked backlog"),
            source_name=messenger_models.SourceName.ZULIP.value,
            source=messenger_models.ZulipSource(
                stream_id=42,
                server_url="https://zulip.example.test",
                source_scope=str(external_account_uuid),
                topic_name="general",
                message_id=101,
            ),
            session=session,
            compact_events=compact_events,
        )
    )

    workspace_api.user_uuid = str(removed_user_uuid)
    workspace_api.project_id = api.project_id
    before_delete = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert before_delete.status_code == 200, before_delete.text
    assert "message.created" in [
        event["payload"]["kind"] for event in before_delete.json()
    ]

    deleted = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert deleted.status_code in (200, 204), deleted.text

    after_delete = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert after_delete.status_code == 200, after_delete.text
    visible_kinds = [event["payload"]["kind"] for event in after_delete.json()]
    assert "message.created" not in visible_kinds
    assert "stream.deleted" in visible_kinds

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT source_scope
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone() is None
        cur.execute(
            """
            SELECT payload->>'kind'
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
            ORDER BY epoch_version
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        visible_view_kinds = [row[0] for row in cur.fetchall()]

    assert "message.created" not in visible_view_kinds
    assert "stream.deleted" in visible_view_kinds


def test_external_member_removal_revokes_duplicate_chat_projections(
    api,
    workspace_api,
    db,
):
    removed_user_uuid = sys_uuid.uuid4()
    second_owner_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(
        db,
        removed_user_uuid,
        f"removed-{removed_user_uuid}",
    )
    conftest.seed_workspace_user(
        db,
        second_owner_uuid,
        f"owner-{second_owner_uuid}",
    )
    first_stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "First provider projection",
    )
    second_stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        second_owner_uuid,
        "Second provider projection",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        first_stream_uuid,
        removed_user_uuid,
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        second_stream_uuid,
        removed_user_uuid,
    )
    first_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        first_stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    second_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        second_stream_uuid,
        second_owner_uuid,
        "general",
        is_default=True,
    )
    first_account_uuid = sys_uuid.UUID("10000000-0000-4000-8000-000000000001")
    second_account_uuid = sys_uuid.UUID("20000000-0000-4000-8000-000000000002")
    provider_realm_uuid = "30000000-0000-4000-8000-000000000003"
    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (
                %s, %s, 'zulip',
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
                TRUE, 'live', TRUE
            )
            """,
            (
                (str(first_account_uuid), str(removed_user_uuid)),
                (str(second_account_uuid), str(second_owner_uuid)),
            ),
        )
        cur.executemany(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                %s, TRUE, %s, %s
            )
            """,
            (
                (
                    str(sys_uuid.uuid4()),
                    str(first_account_uuid),
                    str(removed_user_uuid),
                    json.dumps({"provider_realm_uuid": provider_realm_uuid}),
                    "First provider projection",
                    api.project_id,
                    first_stream_uuid,
                ),
                (
                    str(sys_uuid.uuid4()),
                    str(second_account_uuid),
                    str(second_owner_uuid),
                    json.dumps({"provider_realm_uuid": provider_realm_uuid}),
                    "Second provider projection",
                    api.project_id,
                    second_stream_uuid,
                ),
            ),
        )
        for stream_uuid, account_uuid in (
            (first_stream_uuid, first_account_uuid),
            (second_stream_uuid, second_account_uuid),
        ):
            cur.execute(
                """
                UPDATE m_workspace_streams
                SET source_name = 'zulip',
                    source = %s::jsonb,
                    external_account_uuid = %s,
                    provider_external_id = 'channel:42'
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    json.dumps(
                        {
                            "kind": "zulip",
                            "stream_id": 42,
                            "server_url": "https://zulip.example.test",
                            "source_scope": str(account_uuid),
                        }
                    ),
                    str(account_uuid),
                    api.project_id,
                    stream_uuid,
                ),
            )
        cur.execute(
            """
            SELECT uuid
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
              AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (api.project_id, first_stream_uuid, str(removed_user_uuid)),
        )
        first_binding_uuid = cur.fetchone()[0]

    first_message_uuid = sys_uuid.uuid4()
    second_message_uuid = sys_uuid.uuid4()
    first_public_message_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(first_topic_uuid), str(first_message_uuid).lower()
    )
    second_public_message_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(second_topic_uuid), str(second_message_uuid).lower()
    )

    def create_messages(session):
        for message_uuid, stream_uuid, topic_uuid, author_uuid in (
            (
                first_message_uuid,
                first_stream_uuid,
                first_topic_uuid,
                api.user_uuid,
            ),
            (
                second_message_uuid,
                second_stream_uuid,
                second_topic_uuid,
                second_owner_uuid,
            ),
        ):
            messenger_dm_helpers.create_workspace_user_message(
                uuid=message_uuid,
                project_id=sys_uuid.UUID(api.project_id),
                user_uuid=sys_uuid.UUID(str(author_uuid)),
                stream_uuid=sys_uuid.UUID(stream_uuid),
                topic_uuid=sys_uuid.UUID(topic_uuid),
                payload=message_payloads.MarkdownPayload(
                    content=f"provider event for {stream_uuid}"
                ),
                # Reproduce the legacy provider payload that was mislabeled
                # as native. Stream-level access must still protect it.
                source_name=messenger_models.SourceName.NATIVE.value,
                source=messenger_models.NativeSource(),
                session=session,
            )

    _run_database_operation(create_messages)

    workspace_api.user_uuid = str(removed_user_uuid)
    workspace_api.project_id = api.project_id
    streams_before = api.get(STREAMS, user=removed_user_uuid)
    assert streams_before.status_code == 200, streams_before.text
    visible_projection_uuids = {
        stream["uuid"]
        for stream in streams_before.json()
        if stream["uuid"] in {first_stream_uuid, second_stream_uuid}
    }
    assert visible_projection_uuids == {first_stream_uuid}

    events_before = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert events_before.status_code == 200, events_before.text
    visible_message_uuids = {
        event["payload"].get("uuid")
        for event in events_before.json()
        if event["payload"]["kind"] == "message.created"
    }
    assert str(first_public_message_uuid) in visible_message_uuids
    assert str(second_public_message_uuid) not in visible_message_uuids

    deleted = api.delete(f"{STREAM_BINDINGS}{first_binding_uuid}")
    assert deleted.status_code in (200, 204), deleted.text

    streams_after = api.get(STREAMS, user=removed_user_uuid)
    assert streams_after.status_code == 200, streams_after.text
    assert not {
        stream["uuid"]
        for stream in streams_after.json()
        if stream["uuid"] in {first_stream_uuid, second_stream_uuid}
    }
    events_after = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert events_after.status_code == 200, events_after.text
    visible_message_uuids = {
        event["payload"].get("uuid")
        for event in events_after.json()
        if event["payload"]["kind"] == "message.created"
    }
    assert str(first_public_message_uuid) not in visible_message_uuids
    assert str(second_public_message_uuid) not in visible_message_uuids

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT provider, provider_realm_id, provider_chat_id
            FROM m_workspace_external_chat_membership_revocations
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone() == ("zulip", provider_realm_uuid, "channel:42")
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
              AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (api.project_id, second_stream_uuid, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s
              AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (api.project_id, second_stream_uuid, str(removed_user_uuid)),
        )
        cur.execute(
            """
            DELETE FROM m_workspace_external_chat_membership_revocations
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        cur.execute(
            'DELETE FROM "ra_migrations" WHERE uuid = %s',
            (EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID,),
        )

    migration_engine = ra_migrations.MigrationEngine(
        migrations_path=str(conftest.MIGRATIONS_DIR)
    )
    migration_engine.apply_migration(EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT provider, provider_realm_id, provider_chat_id
            FROM m_workspace_external_chat_membership_revocations
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone() == ("zulip", provider_realm_uuid, "channel:42")
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 0

    _run_database_operation(
        lambda session: external_projection.ensure_external_chat_stream(
            session,
            project_id=sys_uuid.UUID(api.project_id),
            owner_user_uuid=second_owner_uuid,
            projection_stream_uuid=sys_uuid.UUID(second_stream_uuid),
            bridge_instance_uuid=sys_uuid.uuid4(),
            external_account_uuid=second_account_uuid,
            provider_kind="zulip",
            provider_chat_id="channel:42",
            display_name="Second provider projection",
            source={
                "chat_type": "channel",
                "provider_realm_uuid": provider_realm_uuid,
                "participants": [
                    {
                        "identity_uuid": str(second_owner_uuid),
                        "role": "owner",
                    },
                    {
                        "identity_uuid": str(removed_user_uuid),
                        "role": "member",
                    },
                ],
            },
            capabilities={},
            account_settings={"server_url": "https://zulip.example.test"},
        )
    )
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
              AND stream_uuid = %s
              AND user_uuid = %s
            """,
            (api.project_id, second_stream_uuid, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 0

    restored = api.post(
        f"{STREAMS}{first_stream_uuid}/actions/add_users/invoke",
        json={"member": [str(removed_user_uuid)]},
    )
    assert restored.status_code in (200, 201), restored.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_external_chat_membership_revocations
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            'DELETE FROM "ra_migrations" WHERE uuid = %s',
            (EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_UUID,),
        )

    migration_engine.apply_migration(EXTERNAL_CHAT_MEMBERSHIP_MIGRATION_FILE)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_external_chat_membership_revocations
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_confirmed_external_account_access
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(removed_user_uuid)),
        )
        assert cur.fetchone()[0] == 1


def test_streams_cursor_pagination_with_composite_pk(api, db):
    seeded = {
        conftest.seed_user_stream(db, api.project_id, api.user_uuid, f"s-{i}")
        for i in range(5)
    }
    # noise that must never appear in this user's pages
    other_user = sys_uuid.uuid4()
    for i in range(3):
        conftest.seed_user_stream(db, api.project_id, other_user, f"noise-{i}")

    collected = []
    pages = 0
    marker = None
    while True:
        params = {"page_limit": 2}
        if marker:
            params["page_marker"] = marker
        resp = api.get(STREAMS, params=params)
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-Pagination-Limit"] == "2"

        page = resp.json()
        collected.extend(item["uuid"] for item in page)
        pages += 1

        marker = resp.headers.get("X-Pagination-Marker")
        if marker is None:
            break
        assert len(page) == 2
        assert marker == page[-1]["uuid"]
        assert pages < 10  # safety net against an infinite loop

    # every seeded row returned exactly once, nothing from the other user
    assert sorted(collected) == sorted(seeded)
    assert len(collected) == len(set(collected)) == 5
    assert pages == 3  # 2 + 2 + 1


def test_messages_cursor_pagination_uses_created_at_uuid_keyset(api, db):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE indexname = 'm_workspace_messages_project_created_uuid_idx'
            """
        )
        index_definition = cur.fetchone()[0]
    assert "(project_id, created_at, uuid)" in index_definition.replace('"', "")

    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "message-keyset-pagination",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuids = [
        sys_uuid.UUID(f"40000000-0000-4000-8000-{value:012d}") for value in range(1, 5)
    ]

    def seed_messages(session):
        for message_uuid in message_uuids:
            messenger_dm_helpers.create_workspace_user_message(
                uuid=message_uuid,
                project_id=sys_uuid.UUID(api.project_id),
                user_uuid=sys_uuid.UUID(api.user_uuid),
                stream_uuid=sys_uuid.UUID(stream_uuid),
                topic_uuid=sys_uuid.UUID(topic_uuid),
                payload=message_payloads.MarkdownPayload(content=str(message_uuid)),
                session=session,
            )

    _run_database_operation(seed_messages)
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_messages
            SET created_at = CASE
                WHEN uuid = %s THEN '2026-07-15T09:00:00Z'::timestamptz
                ELSE '2026-07-15T10:00:00Z'::timestamptz
            END
            WHERE uuid = ANY(%s)
            """,
            (message_uuids[-1], message_uuids),
        )

    other_stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "message-keyset-other-scope",
    )
    other_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        other_stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    other_message_uuid = sys_uuid.UUID("50000000-0000-4000-8000-000000000001")
    _run_database_operation(
        lambda session: messenger_dm_helpers.create_workspace_user_message(
            uuid=other_message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(other_stream_uuid),
            topic_uuid=sys_uuid.UUID(other_topic_uuid),
            payload=message_payloads.MarkdownPayload(content="other scope"),
            session=session,
        )
    )

    def collect(direction):
        collected = []
        marker = None
        page_headers = []
        while True:
            params = {
                "page_limit": 2,
                "sort_key": "created_at",
                "sort_dir": direction,
                "stream_uuid": stream_uuid,
            }
            if marker is not None:
                params["page_marker"] = marker
            response = api.get(MESSAGES, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            collected.extend(item["uuid"] for item in page)
            marker = response.headers.get("X-Pagination-Marker")
            page_headers.append(marker)
            if marker is None:
                break
        return collected, page_headers

    descending, descending_headers = collect("desc")
    ascending, ascending_headers = collect("asc")

    assert descending == [
        str(message_uuids[2]),
        str(message_uuids[1]),
        str(message_uuids[0]),
        str(message_uuids[3]),
    ]
    assert ascending == [
        str(message_uuids[3]),
        str(message_uuids[0]),
        str(message_uuids[1]),
        str(message_uuids[2]),
    ]
    assert descending_headers == [str(message_uuids[1]), None]
    assert ascending_headers == [str(message_uuids[0]), None]

    wrong_scope = api.get(
        MESSAGES,
        params={
            "page_limit": 2,
            "page_marker": str(other_message_uuid),
            "sort_key": "created_at",
            "sort_dir": "asc",
            "stream_uuid": stream_uuid,
        },
    )
    assert wrong_scope.status_code == 404, wrong_scope.text

    unsupported_sort = api.get(
        MESSAGES,
        params={
            "page_limit": 2,
            "sort_key": "updated_at",
            "sort_dir": "asc",
            "stream_uuid": stream_uuid,
        },
    )
    assert unsupported_sort.status_code == 400, unsupported_sort.text


def test_mentioned_unread_messages_use_scoped_keyset_page(api, db):
    target_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "mentioned-unread-keyset",
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, target_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    mention = f"[Target](urn:user:{target_user})"
    message_uuids = []
    for content in (f"first {mention}", "not a mention", f"second {mention}"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    common_params = {
        "page_limit": 1,
        "sort_key": "created_at",
        "sort_dir": "desc",
        "mentioned": "true",
        "read": "false",
    }
    first_page = api.get(MESSAGES, user=target_user, params=common_params)
    assert first_page.status_code == 200, first_page.text
    assert [item["uuid"] for item in first_page.json()] == [message_uuids[2]]
    assert first_page.json()[0]["mentioned"] is True
    assert first_page.json()[0]["read"] is False
    marker = first_page.headers["X-Pagination-Marker"]

    second_page = api.get(
        MESSAGES,
        user=target_user,
        params={**common_params, "page_marker": marker},
    )
    assert second_page.status_code == 200, second_page.text
    assert [item["uuid"] for item in second_page.json()] == [message_uuids[0]]
    assert "X-Pagination-Marker" not in second_page.headers

    response = api.post(
        f"{MESSAGES}{message_uuids[2]}/actions/read/invoke",
        user=target_user,
    )
    assert response.status_code == 200, response.text
    unread_mentions = api.get(
        MESSAGES,
        user=target_user,
        params={**common_params, "page_limit": 10},
    )
    assert unread_mentions.status_code == 200, unread_mentions.text
    assert [item["uuid"] for item in unread_mentions.json()] == [message_uuids[0]]

    all_mentions = api.get(
        MESSAGES,
        user=target_user,
        params={
            "page_limit": 10,
            "sort_key": "created_at",
            "sort_dir": "desc",
            "mentioned": "true",
        },
    )
    assert all_mentions.status_code == 200, all_mentions.text
    assert [item["uuid"] for item in all_mentions.json()] == [
        message_uuids[2],
        message_uuids[0],
    ]
    assert [item["read"] for item in all_mentions.json()] == [True, False]


def test_draft_crud_idempotency_etags_owner_scope_and_no_events(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-crud",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        other_user,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "drafts",
    )
    draft_uuid = sys_uuid.uuid4()
    create_body = {
        "uuid": str(draft_uuid),
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "payload": {"kind": "markdown", "content": "  first draft  "},
    }

    response = api.post(DRAFTS, json=create_body)
    assert response.status_code == 201, response.text
    assert response.headers["ETag"] == '"1"'
    created = response.json()
    assert created["project_id"] == api.project_id
    assert created["user_uuid"] == api.user_uuid
    assert created["payload"]["content"] == "first draft"
    assert created["revision"] == 1

    response = api.post(DRAFTS, json=create_body)
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"1"'
    assert response.json() == created

    conflict_body = dict(create_body)
    conflict_body["payload"] = {"kind": "markdown", "content": "different"}
    response = api.post(DRAFTS, json=conflict_body)
    assert response.status_code == 409, response.text

    response = api.get(f"{DRAFTS}{draft_uuid}")
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"1"'
    response = api.get(f"{DRAFTS}{draft_uuid}", user=other_user)
    assert response.status_code == 404, response.text

    response = api.put(
        f"{DRAFTS}{draft_uuid}",
        json={"payload": {"kind": "markdown", "content": "updated"}},
    )
    assert response.status_code == 428, response.text

    for invalid_etag in ('W/"1"', '"0"', '"01"', "1"):
        response = api.put(
            f"{DRAFTS}{draft_uuid}",
            headers={"If-Match": invalid_etag},
            json={"payload": {"kind": "markdown", "content": "updated"}},
        )
        assert response.status_code == 412, response.text
        assert response.headers["ETag"] == '"1"'
        assert response.json()["current"]["uuid"] == str(draft_uuid)
        assert response.json()["current"]["project_id"] == api.project_id
        assert response.json()["current"]["user_uuid"] == api.user_uuid

    response = api.put(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"1"'},
        json={"payload": {"kind": "markdown", "content": "  updated  "}},
    )
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"2"'
    assert response.json()["payload"]["content"] == "updated"
    assert response.json()["revision"] == 2

    response = api.delete(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"1"'},
    )
    assert response.status_code == 412, response.text
    assert response.headers["ETag"] == '"2"'
    assert response.json()["current"]["revision"] == 2

    response = api.delete(f"{DRAFTS}{draft_uuid}")
    assert response.status_code == 428, response.text
    response = api.delete(
        f"{DRAFTS}{draft_uuid}",
        headers={"If-Match": '"2"'},
    )
    assert response.status_code == 204, response.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'uuid' = %s
            """,
            (api.project_id, str(draft_uuid)),
        )
        event_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_messages
            WHERE project_id = %s
                AND stream_uuid = %s
            """,
            (api.project_id, stream_uuid),
        )
        message_count = cur.fetchone()[0]

    assert event_count == 0
    assert message_count == 0


def test_draft_pagination_uses_updated_at_uuid_owner_filter_scope(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-pagination",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "draft-pagination",
    )
    draft_uuids = [
        sys_uuid.UUID(f"60000000-0000-4000-8000-{value:012d}") for value in range(1, 5)
    ]
    for draft_uuid in draft_uuids:
        response = api.post(
            DRAFTS,
            json={
                "uuid": str(draft_uuid),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": str(draft_uuid),
                },
            },
        )
        assert response.status_code == 201, response.text
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_drafts
            SET updated_at = CASE
                WHEN uuid = %s THEN '2026-07-16T09:00:00Z'::timestamptz
                ELSE '2026-07-16T10:00:00Z'::timestamptz
            END
            WHERE uuid = ANY(%s)
            """,
            (draft_uuids[-1], draft_uuids),
        )

    def collect(direction):
        result = []
        marker = None
        pages = 0
        while True:
            params = {
                "page_limit": 2,
                "sort_key": "updated_at",
                "sort_dir": direction,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
            }
            if marker is not None:
                params["page_marker"] = marker
            response = api.get(DRAFTS, params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            result.extend(item["uuid"] for item in page)
            marker = response.headers.get("X-Pagination-Marker")
            pages += 1
            assert pages < 10, (direction, marker, result)
            if marker is None:
                break
        return result

    assert collect("asc") == [
        str(draft_uuids[3]),
        str(draft_uuids[0]),
        str(draft_uuids[1]),
        str(draft_uuids[2]),
    ]
    assert collect("desc") == [
        str(draft_uuids[2]),
        str(draft_uuids[1]),
        str(draft_uuids[0]),
        str(draft_uuids[3]),
    ]

    other_user = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        other_user,
    )
    other_draft_uuid = sys_uuid.uuid4()
    response = api.post(
        DRAFTS,
        user=other_user,
        json={
            "uuid": str(other_draft_uuid),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "other"},
        },
    )
    assert response.status_code == 201, response.text
    response = api.get(
        DRAFTS,
        params={
            "page_limit": 2,
            "page_marker": str(other_draft_uuid),
            "sort_key": "updated_at",
            "sort_dir": "asc",
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
        },
    )
    assert response.status_code == 404, response.text


def test_draft_cascades_hard_delete_for_binding_topic_and_stream(api, db):
    other_user = sys_uuid.uuid4()

    binding_stream = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-binding-cascade",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        binding_stream,
        other_user,
    )
    binding_topic = conftest.seed_stream_topic(
        db,
        api.project_id,
        binding_stream,
        api.user_uuid,
        "binding",
    )
    binding_draft = sys_uuid.uuid4()
    response = api.post(
        DRAFTS,
        user=other_user,
        json={
            "uuid": str(binding_draft),
            "stream_uuid": binding_stream,
            "topic_uuid": binding_topic,
            "payload": {"kind": "markdown", "content": "binding"},
        },
    )
    assert response.status_code == 201, response.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT uuid
            FROM m_workspace_stream_bindings
            WHERE project_id = %s
                AND stream_uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, binding_stream, str(other_user)),
        )
        binding_uuid = cur.fetchone()[0]
    response = api.delete(f"{STREAM_BINDINGS}{binding_uuid}")
    assert response.status_code in (200, 204), response.text

    topic_stream = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-topic-cascade",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        topic_stream,
        api.user_uuid,
        "topic",
    )
    topic_draft = sys_uuid.uuid4()
    response = api.post(
        DRAFTS,
        json={
            "uuid": str(topic_draft),
            "stream_uuid": topic_stream,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "topic"},
        },
    )
    assert response.status_code == 201, response.text
    response = api.delete(f"{STREAM_TOPICS}{topic_uuid}")
    assert response.status_code in (200, 204), response.text

    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-stream-cascade",
    )
    stream_topic = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "stream",
    )
    stream_draft = sys_uuid.uuid4()
    response = api.post(
        DRAFTS,
        json={
            "uuid": str(stream_draft),
            "stream_uuid": stream_uuid,
            "topic_uuid": stream_topic,
            "payload": {"kind": "markdown", "content": "stream"},
        },
    )
    assert response.status_code == 201, response.text
    response = api.delete(f"{STREAMS}{stream_uuid}")
    assert response.status_code in (200, 204), response.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_drafts
            WHERE uuid = ANY(%s)
            """,
            ([binding_draft, topic_draft, stream_draft],),
        )
        remaining = cur.fetchone()[0]

    assert remaining == 0


def test_draft_create_serializes_before_stream_cascade(api, db, monkeypatch):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-create-delete-race",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "draft-create-delete-race",
    )
    draft_uuid = sys_uuid.uuid4()
    scope_locked = threading.Event()
    release_create = threading.Event()
    original_lock_scope = messenger_dm_helpers._lock_workspace_draft_scope

    def pause_create_scope(*args, **kwargs):
        result = original_lock_scope(*args, **kwargs)
        scope_locked.set()
        assert release_create.wait(timeout=5)
        return result

    monkeypatch.setattr(
        messenger_dm_helpers,
        "_lock_workspace_draft_scope",
        pause_create_scope,
    )

    def create_side():
        return _run_database_operation(
            lambda session: messenger_dm_helpers.create_workspace_draft(
                project_id,
                user_uuid,
                draft_uuid,
                sys_uuid.UUID(stream_uuid),
                sys_uuid.UUID(topic_uuid),
                {"kind": "markdown", "content": "created"},
                session=session,
            )
        )

    def delete_side():
        return _run_database_operation(
            lambda session: messenger_dm_helpers.delete_workspace_user_stream(
                project_id,
                user_uuid,
                sys_uuid.UUID(stream_uuid),
                session=session,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        create_future = executor.submit(create_side)
        assert scope_locked.wait(timeout=5)
        delete_future = executor.submit(delete_side)
        _, pending = concurrent.futures.wait(
            [delete_future],
            timeout=0.1,
        )
        assert pending == {delete_future}
        release_create.set()
        _, created = create_future.result(timeout=5)
        assert created is True
        assert delete_future.result(timeout=5) is None

    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM m_workspace_drafts WHERE uuid = %s",
            (draft_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'uuid' = %s
            """,
            (project_id, str(draft_uuid)),
        )
        event_count = cur.fetchone()[0]
    assert event_count == 0


def test_draft_update_waits_for_stream_cascade_and_cannot_recreate_deleted_draft(
    api,
    db,
    monkeypatch,
):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-update-delete-race",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "draft-update-delete-race",
    )
    draft_uuid = sys_uuid.uuid4()
    response = api.post(
        DRAFTS,
        json={
            "uuid": str(draft_uuid),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "initial"},
        },
    )
    assert response.status_code == 201, response.text

    stream_locked = threading.Event()
    release_delete = threading.Event()
    original_lock_stream = messenger_dm_helpers._lock_workspace_stream

    def pause_stream_delete(*args, **kwargs):
        result = original_lock_stream(*args, **kwargs)
        stream_locked.set()
        assert release_delete.wait(timeout=5)
        return result

    monkeypatch.setattr(
        messenger_dm_helpers,
        "_lock_workspace_stream",
        pause_stream_delete,
    )

    def delete_side():
        return _run_database_operation(
            lambda session: messenger_dm_helpers.delete_workspace_user_stream(
                project_id,
                user_uuid,
                sys_uuid.UUID(stream_uuid),
                session=session,
            )
        )

    def update_side():
        try:
            return _run_database_operation(
                lambda session: messenger_dm_helpers.update_workspace_draft(
                    project_id,
                    user_uuid,
                    draft_uuid,
                    {"kind": "markdown", "content": "must not survive"},
                    1,
                    session=session,
                )
            )
        except Exception as exc:
            return exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(delete_side)
        assert stream_locked.wait(timeout=5)
        update_future = executor.submit(update_side)
        _, pending = concurrent.futures.wait(
            [update_future],
            timeout=0.1,
        )
        assert pending == {update_future}
        release_delete.set()
        assert delete_future.result(timeout=5) is None
        update_result = update_future.result(timeout=5)
        assert isinstance(update_result, Exception)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'uuid' = %s
            """,
            (project_id, str(draft_uuid)),
        )
        event_count = cur.fetchone()[0]
    assert event_count == 0


def test_draft_revision_compare_and_swap_serializes_concurrent_mutations(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-concurrency",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "draft-concurrency",
    )

    def create(draft_uuid):
        response = api.post(
            DRAFTS,
            json={
                "uuid": str(draft_uuid),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "initial"},
            },
        )
        assert response.status_code == 201, response.text

    update_race_uuid = sys_uuid.uuid4()
    create(update_race_uuid)
    barrier = threading.Barrier(2)

    def concurrent_update(content):
        barrier.wait()
        return _run_database_operation(
            lambda session: messenger_dm_helpers.update_workspace_draft(
                project_id,
                user_uuid,
                update_race_uuid,
                {"kind": "markdown", "content": content},
                1,
                session=session,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(concurrent_update, ("first winner", "second winner"))
        )
    assert sorted(updated for _, updated in results) == [False, True]
    response = api.get(f"{DRAFTS}{update_race_uuid}")
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2

    mixed_race_uuid = sys_uuid.uuid4()
    create(mixed_race_uuid)
    barrier = threading.Barrier(2)

    def update_side():
        barrier.wait()
        try:
            _, updated = _run_database_operation(
                lambda session: messenger_dm_helpers.update_workspace_draft(
                    project_id,
                    user_uuid,
                    mixed_race_uuid,
                    {"kind": "markdown", "content": "updated"},
                    1,
                    session=session,
                )
            )
            return "updated" if updated else "stale"
        except Exception:
            return "missing"

    def delete_side():
        barrier.wait()
        try:
            _, deleted = _run_database_operation(
                lambda session: messenger_dm_helpers.delete_workspace_draft(
                    project_id,
                    user_uuid,
                    mixed_race_uuid,
                    1,
                    session=session,
                )
            )
            return "deleted" if deleted else "stale"
        except Exception:
            return "missing"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        update_future = executor.submit(update_side)
        delete_future = executor.submit(delete_side)
        outcomes = {update_future.result(), delete_future.result()}
    assert outcomes in ({"updated", "stale"}, {"deleted", "missing"})


def test_different_drafts_in_same_scope_do_not_share_an_exclusive_hot_lock(
    api,
    db,
    monkeypatch,
):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "draft-shared-scope-lock",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "draft-shared-scope-lock",
    )
    first_uuid = sys_uuid.uuid4()
    second_uuid = sys_uuid.uuid4()
    for draft_uuid in (first_uuid, second_uuid):
        response = api.post(
            DRAFTS,
            json={
                "uuid": str(draft_uuid),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "initial"},
            },
        )
        assert response.status_code == 201, response.text

    first_scope_locked = threading.Event()
    release_first = threading.Event()
    pause_guard = threading.Lock()
    first_scope_seen = False
    original_lock_scope = messenger_dm_helpers._lock_workspace_draft_scope

    def pause_first_scope(*args, **kwargs):
        nonlocal first_scope_seen
        result = original_lock_scope(*args, **kwargs)
        with pause_guard:
            should_pause = not first_scope_seen
            first_scope_seen = True
        if should_pause:
            first_scope_locked.set()
            assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(
        messenger_dm_helpers,
        "_lock_workspace_draft_scope",
        pause_first_scope,
    )

    def update(draft_uuid, content):
        return _run_database_operation(
            lambda session: messenger_dm_helpers.update_workspace_draft(
                project_id,
                user_uuid,
                draft_uuid,
                {"kind": "markdown", "content": content},
                1,
                session=session,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(update, first_uuid, "first")
        assert first_scope_locked.wait(timeout=5)
        second_future = executor.submit(update, second_uuid, "second")
        second_draft, second_updated = second_future.result(timeout=2)
        assert second_updated is True
        assert second_draft.revision == 2
        release_first.set()
        first_draft, first_updated = first_future.result(timeout=5)
        assert first_updated is True
        assert first_draft.revision == 2


# --------------------------------------------------------------------------- #
# Stream topics: CRUD
# --------------------------------------------------------------------------- #


def test_stream_topic_create_is_visible_to_stream_users(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-create-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)

    resp = api.post(
        STREAM_TOPICS,
        json={
            "name": "planning",
            "stream_uuid": stream_uuid,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    topic = resp.json()
    assert topic["name"] == "planning"
    assert topic["stream_uuid"] == stream_uuid
    assert 0 <= topic["color"] <= 0xFFFFFF
    assert topic.get("last_message_uuid") is None
    assert topic["is_default"] is False
    assert topic["is_done"] is False
    assert topic["notification_mode"] == "default"
    assert topic["summary"] is None
    assert topic["summary_last_message_uuid"] is None
    assert topic["summary_has_new_messages"] is None
    assert topic["summary_enabled"] is True
    assert topic["summary_system_prompt"] is None

    resp = api.get(f"{STREAM_TOPICS}{topic['uuid']}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "planning"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_topic_flags
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid IN (%s, %s)
            """,
            (topic["uuid"], api.project_id, api.user_uuid, other_user),
        )
        flags_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT epoch_version, user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.created'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic["uuid"]),
        )
        event_rows = cur.fetchall()

    assert flags_count == 2
    assert {str(row[1]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, _, payload in event_rows:
        assert payload["kind"] == "topic.created"
        assert payload["uuid"] == topic["uuid"]
        assert payload["name"] == "planning"
        assert payload["stream_uuid"] == stream_uuid
        assert payload["color"] == topic["color"]
        assert payload.get("last_message_uuid") is None
        assert payload["unread_count"] == 0
        assert payload["is_default"] is False
        assert payload["is_done"] is False
        assert payload["notification_mode"] == "default"

    event = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": event_rows[0][0],
            "user_uuid": event_rows[0][1],
            "payload": event_rows[0][2],
        }
    )
    assert event["object_type"] == "topic"
    assert event["payload"]["kind"] == "topic.created"
    assert event["payload"]["uuid"] == topic["uuid"]
    assert event["payload"]["name"] == "planning"
    assert event["payload"]["color"] == topic["color"]
    assert event["payload"].get("last_message_uuid") is None
    assert event["payload"]["notification_mode"] == "default"
    assert event["payload"]["summary"] is None
    assert event["payload"]["summary_last_message_uuid"] is None
    assert event["payload"]["summary_has_new_messages"] is None
    assert event["payload"]["summary_enabled"] is True
    assert event["payload"]["summary_system_prompt"] is None


def test_stream_topic_summary_has_no_public_write_action(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-summary-internal"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )

    response = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary/invoke",
        json={"summary": None, "summary_last_message_uuid": None},
    )

    assert response.status_code == 404, response.text


def test_stream_topic_summary_tracks_new_messages_and_emits_snapshots(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-summary-team"
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        other_user,
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )

    first_message = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "first decision"},
        },
    )
    assert first_message.status_code == 201, first_message.text
    first_message_uuid = first_message.json()["uuid"]
    first_public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), first_message_uuid.lower())
    )

    topic = _run_database_operation(
        lambda session: messenger_dm_helpers.set_workspace_user_stream_topic_summary(
            project_id,
            user_uuid,
            sys_uuid.UUID(topic_uuid),
            "The first decision was recorded.",
            sys_uuid.UUID(first_message_uuid),
            session=session,
        )
    )
    assert topic.summary == "The first decision was recorded."
    assert str(topic.summary_last_message_uuid) == first_message_uuid
    assert topic.summary_has_new_messages is False

    other_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert other_topic.status_code == 200, other_topic.text
    assert other_topic.json()["summary"] == "The first decision was recorded."
    assert other_topic.json()["summary_has_new_messages"] is False

    second_message = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "new follow-up"},
        },
    )
    assert second_message.status_code == 201, second_message.text
    refreshed = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["summary_has_new_messages"] is True

    topic = _run_database_operation(
        lambda session: messenger_dm_helpers.set_workspace_user_stream_topic_summary(
            project_id,
            user_uuid,
            sys_uuid.UUID(topic_uuid),
            None,
            None,
            session=session,
        )
    )
    assert topic.summary is None
    assert topic.summary_last_message_uuid is None
    assert topic.summary_has_new_messages is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'topic.updated'
              AND payload->>'uuid' = %s
              AND payload->>'summary' = %s
            ORDER BY user_uuid
            """,
            (
                api.project_id,
                topic_uuid,
                "The first decision was recorded.",
            ),
        )
        event_rows = cur.fetchall()
    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert all(
        payload["summary_last_message_uuid"] == first_public_message_uuid
        and payload["summary_has_new_messages"] is False
        for _, payload in event_rows
    )


def test_hard_delete_restores_topic_summary_journal_and_resets_work(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "topic-summary-delete-journal",
    )

    def create_topic(name, contents):
        topic_uuid = conftest.seed_stream_topic(
            db,
            api.project_id,
            stream_uuid,
            api.user_uuid,
            name,
        )
        message_uuids = []
        for content in contents:
            response = api.post(
                MESSAGES,
                json={
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "payload": {"kind": "markdown", "content": content},
                },
            )
            assert response.status_code == 201, response.text
            message_uuids.append(response.json()["uuid"])
        return topic_uuid, message_uuids

    def store_summary(topic_uuid, boundary_uuid, summary):
        return _run_database_operation(
            lambda session: (
                messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                    project_id,
                    user_uuid,
                    sys_uuid.UUID(topic_uuid),
                    summary,
                    sys_uuid.UUID(boundary_uuid),
                    session=session,
                )
            )
        )

    def seed_finished_job(topic_uuid, boundary_uuid):
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO m_workspace_topic_summary_jobs (
                    topic_uuid, project_id, status, attempt,
                    boundary_message_uuid, effective_prompt,
                    prompt_fingerprint, completed_at, created_at, updated_at
                ) VALUES (%s, %s, 'succeeded', 1, %s, %s, %s, NOW(), NOW(), NOW())
                """,
                (
                    topic_uuid,
                    api.project_id,
                    boundary_uuid,
                    topic_summarization.DEFAULT_SYSTEM_PROMPT,
                    topic_summarization._prompt_fingerprint(
                        topic_summarization.DEFAULT_SYSTEM_PROMPT,
                        None,
                    ),
                ),
            )

    boundary_topic, boundary_messages = create_topic(
        "delete-current-boundary",
        ("first", "second"),
    )
    store_summary(boundary_topic, boundary_messages[0], "First summary.")
    store_summary(boundary_topic, boundary_messages[1], "Second summary.")
    seed_finished_job(boundary_topic, boundary_messages[1])
    deleted = api.delete(f"{MESSAGES}{boundary_messages[1]}")
    assert deleted.status_code == 204, deleted.text
    restored = api.get(f"{STREAM_TOPICS}{boundary_topic}").json()
    assert restored["summary"] == "First summary."
    assert restored["summary_last_message_uuid"] == boundary_messages[0]
    assert restored["summary_has_new_messages"] is False

    earlier_topic, earlier_messages = create_topic(
        "delete-earlier-covered",
        ("one", "two", "three"),
    )
    store_summary(earlier_topic, earlier_messages[0], "Summary at one.")
    store_summary(earlier_topic, earlier_messages[2], "Summary at three.")
    seed_finished_job(earlier_topic, earlier_messages[2])
    deleted = api.delete(f"{MESSAGES}{earlier_messages[1]}")
    assert deleted.status_code == 204, deleted.text
    restored = api.get(f"{STREAM_TOPICS}{earlier_topic}").json()
    assert restored["summary"] == "Summary at one."
    assert restored["summary_last_message_uuid"] == earlier_messages[0]
    assert restored["summary_has_new_messages"] is True

    only_topic, only_messages = create_topic("delete-only-message", ("only",))
    store_summary(only_topic, only_messages[0], "Only summary.")
    seed_finished_job(only_topic, only_messages[0])
    deleted = api.delete(f"{MESSAGES}{only_messages[0]}")
    assert deleted.status_code == 204, deleted.text
    cleared = api.get(f"{STREAM_TOPICS}{only_topic}").json()
    assert cleared["summary"] is None
    assert cleared["summary_last_message_uuid"] is None
    assert cleared["summary_has_new_messages"] is None

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_topic_summary_jobs "
            "WHERE topic_uuid = ANY(%s)",
            ([boundary_topic, earlier_topic, only_topic],),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT topic_uuid, COUNT(*)
            FROM m_workspace_topic_summary_journal
            WHERE invalidated_at IS NOT NULL
              AND topic_uuid = ANY(%s)
            GROUP BY topic_uuid
            """,
            ([boundary_topic, earlier_topic, only_topic],),
        )
        invalidated_counts = {
            str(topic_uuid): count for topic_uuid, count in cursor.fetchall()
        }
        assert invalidated_counts == {
            boundary_topic: 1,
            earlier_topic: 1,
            only_topic: 1,
        }
        cursor.execute(
            """
            SELECT payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'topic.updated'
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (api.project_id, earlier_topic),
        )
        deletion_event = cursor.fetchone()[0]
    assert deletion_event["summary"] == "Summary at one."
    assert deletion_event["summary_last_message_uuid"] == str(
        sys_uuid.uuid5(
            sys_uuid.UUID(earlier_topic),
            earlier_messages[0].lower(),
        )
    )
    assert deletion_event["summary_has_new_messages"] is True


def test_stream_topic_summary_rejects_invalid_and_older_boundaries(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-summary-boundaries"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )
    other_topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "other"
    )

    message_uuids = []
    for content in ("older", "newer"):
        response = api.post(
            MESSAGES,
            json={
                "uuid": str(sys_uuid.uuid4()),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    other_message = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": other_topic_uuid,
            "payload": {"kind": "markdown", "content": "wrong topic"},
        },
    )
    assert other_message.status_code == 201, other_message.text

    with pytest.raises(messenger_exceptions.InvalidTopicSummaryBoundaryError):
        _run_database_operation(
            lambda session: (
                messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                    project_id,
                    user_uuid,
                    sys_uuid.UUID(topic_uuid),
                    "Invalid boundary.",
                    sys_uuid.UUID(other_message.json()["uuid"]),
                    session=session,
                )
            )
        )

    _run_database_operation(
        lambda session: messenger_dm_helpers.set_workspace_user_stream_topic_summary(
            project_id,
            user_uuid,
            sys_uuid.UUID(topic_uuid),
            "Current summary.",
            sys_uuid.UUID(message_uuids[1]),
            session=session,
        )
    )

    with pytest.raises(messenger_exceptions.TopicSummaryConflictError):
        _run_database_operation(
            lambda session: (
                messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                    project_id,
                    user_uuid,
                    sys_uuid.UUID(topic_uuid),
                    "Stale summary.",
                    sys_uuid.UUID(message_uuids[0]),
                    session=session,
                )
            )
        )
    current = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert current.status_code == 200, current.text
    assert current.json()["summary"] == "Current summary."
    assert current.json()["summary_last_message_uuid"] == message_uuids[1]

    with pytest.raises(messenger_exceptions.InvalidTopicSummaryStateError):
        _run_database_operation(
            lambda session: (
                messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                    project_id,
                    user_uuid,
                    sys_uuid.UUID(topic_uuid),
                    None,
                    sys_uuid.UUID(message_uuids[1]),
                    session=session,
                )
            )
        )

    with pytest.raises(ra_exceptions.TypeError):
        _run_database_operation(
            lambda session: (
                messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                    project_id,
                    user_uuid,
                    sys_uuid.UUID(topic_uuid),
                    "x" * 4097,
                    sys_uuid.UUID(message_uuids[1]),
                    session=session,
                )
            )
        )


def test_concurrent_topic_summary_jobs_cannot_leave_an_older_boundary(api, db):
    project_id = sys_uuid.UUID(api.project_id)
    user_uuid = sys_uuid.UUID(api.user_uuid)
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-summary-race"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )
    message_uuids = []
    for content in ("older snapshot", "newer snapshot"):
        response = api.post(
            MESSAGES,
            json={
                "uuid": str(sys_uuid.uuid4()),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    barrier = threading.Barrier(2)

    def update(summary, boundary):
        barrier.wait()
        try:
            _run_database_operation(
                lambda session: (
                    messenger_dm_helpers.set_workspace_user_stream_topic_summary(
                        project_id,
                        user_uuid,
                        sys_uuid.UUID(topic_uuid),
                        summary,
                        boundary,
                        session=session,
                    )
                )
            )
            return "stored"
        except messenger_exceptions.TopicSummaryConflictError:
            return "conflict"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(update, "Older summary.", message_uuids[0])
        newer = executor.submit(update, "Newer summary.", message_uuids[1])
        outcomes = {older.result(timeout=5), newer.result(timeout=5)}

    assert outcomes in ({"stored"}, {"stored", "conflict"})
    current = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert current.status_code == 200, current.text
    assert current.json()["summary"] == "Newer summary."
    assert current.json()["summary_last_message_uuid"] == str(message_uuids[1])


def test_stream_topic_summary_prompt_requires_owner_or_administrator(api, db):
    member_uuid = sys_uuid.uuid4()
    moderator_uuid = sys_uuid.uuid4()
    administrator_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-summary-prompt"
    )
    for user_uuid, role in (
        (member_uuid, "member"),
        (moderator_uuid, "moderator"),
        (administrator_uuid, "administrator"),
        (owner_uuid, "owner"),
    ):
        conftest.seed_user_stream_binding(
            db,
            api.project_id,
            stream_uuid,
            user_uuid,
            role=role,
        )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )

    forbidden = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=member_uuid,
        json={"summary_system_prompt": "Member prompt."},
    )
    assert forbidden.status_code == 403, forbidden.text
    disable_forbidden = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=member_uuid,
        json={"summary_enabled": False},
    )
    assert disable_forbidden.status_code == 403, disable_forbidden.text

    oversized = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        json={"summary_system_prompt": "x" * 16385},
    )
    assert oversized.status_code == 400, oversized.text
    invalid_reasoning = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        json={
            "summary_system_prompt": "Focus on decisions.",
            "summary_reasoning_effort": "ultra",
        },
    )
    assert invalid_reasoning.status_code == 400, invalid_reasoning.text

    moderator_forbidden = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=moderator_uuid,
        json={"summary_system_prompt": "Focus on decisions."},
    )
    assert moderator_forbidden.status_code == 403, moderator_forbidden.text

    response = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=administrator_uuid,
        json={
            "summary_system_prompt": "Focus on decisions and owners.",
            "summary_reasoning_effort": "off",
            "summary_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary_system_prompt"] == "Focus on decisions and owners."
    assert response.json()["summary_reasoning_effort"] == "off"
    assert response.json()["summary_enabled"] is False

    response = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=owner_uuid,
        json={"summary_system_prompt": "Focus on risks.", "summary_enabled": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["summary_system_prompt"] == "Focus on risks."
    assert response.json()["summary_reasoning_effort"] == "off"
    assert response.json()["summary_enabled"] is True
    member_topic = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=member_uuid)
    assert member_topic.status_code == 200, member_topic.text
    assert member_topic.json()["summary_system_prompt"] == "Focus on risks."

    reset = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/set_summary_prompt/invoke",
        user=owner_uuid,
        json={
            "summary_system_prompt": None,
            "summary_reasoning_effort": None,
        },
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["summary_system_prompt"] is None
    assert reset.json().get("summary_reasoning_effort") is None


def test_stream_topic_rename(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )

    resp = api.put(
        f"{STREAM_TOPICS}{topic_uuid}",
        json={"name": "retros", "color": 0xABCDEF},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"
    assert resp.json()["color"] == 0xABCDEF

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "retros"
    assert resp.json()["color"] == 0xABCDEF

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["name"] == "retros"
        assert payload["stream_uuid"] == stream_uuid
        assert payload["color"] == 0xABCDEF


def test_stream_topic_notifications_follow_stream_mute_rules(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-notifications-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "follow"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "follow"

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "unmute"},
    )
    assert resp.status_code == 400, resp.text

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "muted"},
    )
    assert resp.status_code == 200, resp.text

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
        json={"notification_mode": "unmute"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "unmute"

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        json={"notification_mode": "all_messages"},
    )
    assert resp.status_code == 200, resp.text
    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notification_mode"] == "default"

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, notification_mode
            FROM m_workspace_user_topics_view
            WHERE project_id = %s
                AND uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        topic_rows = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND user_uuid = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
            ORDER BY epoch_version
            """,
            (api.project_id, api.user_uuid, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert dict((str(user_uuid), mode) for user_uuid, mode in topic_rows) == {
        str(api.user_uuid): "default",
        str(other_user): "default",
    }
    assert [payload["notification_mode"] for _, payload in event_rows] == [
        "follow",
        "follow",
        "unmute",
        "default",
    ]


def test_unread_counters_split_active_and_passive_notification_traffic(api, db):
    target_user = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, target_user, f"user-{target_user}")

    def create_stream(name, notification_mode):
        stream_uuid = conftest.seed_user_stream(db, api.project_id, api.user_uuid, name)
        conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, target_user)
        response = api.post(
            f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
            user=target_user,
            json={"notification_mode": notification_mode},
        )
        assert response.status_code == 200, response.text
        return stream_uuid

    def create_topic(stream_uuid, name):
        return conftest.seed_stream_topic(
            db, api.project_id, stream_uuid, api.user_uuid, name
        )

    def set_topic_mode(topic_uuid, notification_mode):
        response = api.post(
            f"{STREAM_TOPICS}{topic_uuid}/actions/notifications/invoke",
            user=target_user,
            json={"notification_mode": notification_mode},
        )
        assert response.status_code == 200, response.text

    def post_message(stream_uuid, topic_uuid, content):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text

    muted_stream = create_stream("Muted counter split", "muted")
    inherited_topic = create_topic(muted_stream, "Inherited mute")
    unmuted_topic = create_topic(muted_stream, "Mentions override")
    followed_topic = create_topic(muted_stream, "All messages override")
    muted_topic = create_topic(muted_stream, "Explicit mute")
    set_topic_mode(unmuted_topic, "unmute")
    set_topic_mode(followed_topic, "follow")
    set_topic_mode(muted_topic, "mute")

    mention = f"[Target](urn:user:{target_user})"
    post_message(muted_stream, inherited_topic, "inherited and passive")
    post_message(muted_stream, unmuted_topic, f"active mention {mention}")
    post_message(muted_stream, unmuted_topic, "passive non-mention")
    post_message(muted_stream, followed_topic, "active followed message")
    post_message(muted_stream, muted_topic, f"muted mention {mention}")

    response = api.post(FOLDERS, user=target_user, json={"title": "Counter split"})
    assert response.status_code in (200, 201), response.text
    custom_folder_uuid = response.json()["uuid"]
    response = api.post(
        FOLDER_ITEMS,
        user=target_user,
        json={
            "folder_uuid": custom_folder_uuid,
            "stream_uuid": muted_stream,
            "chat_type": "stream",
        },
    )
    assert response.status_code in (200, 201), response.text

    mentions_stream = create_stream("Mentions counter split", "mentions_only")
    mentions_topic = create_topic(mentions_stream, "Inherited mentions")
    post_message(mentions_stream, mentions_topic, f"active mention {mention}")
    post_message(mentions_stream, mentions_topic, "passive non-mention")

    all_messages_stream = create_stream("All messages counter split", "all_messages")
    all_messages_topic = create_topic(all_messages_stream, "Inherited all")
    post_message(all_messages_stream, all_messages_topic, "active message")

    expected_stream_counts = {
        muted_stream: (5, 2, 3),
        mentions_stream: (2, 1, 1),
        all_messages_stream: (1, 1, 0),
    }
    for stream_uuid, expected_counts in expected_stream_counts.items():
        response = api.get(f"{STREAMS}{stream_uuid}", user=target_user)
        assert response.status_code == 200, response.text
        stream = response.json()
        assert (
            stream["unread_count"],
            stream["active_unread_count"],
            stream["passive_unread_count"],
        ) == expected_counts

    expected_topic_counts = {
        inherited_topic: (1, 0, 1),
        unmuted_topic: (2, 1, 1),
        followed_topic: (1, 1, 0),
        muted_topic: (1, 0, 1),
        mentions_topic: (2, 1, 1),
        all_messages_topic: (1, 1, 0),
    }
    for topic_uuid, expected_counts in expected_topic_counts.items():
        response = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=target_user)
        assert response.status_code == 200, response.text
        topic = response.json()
        assert (
            topic["unread_count"],
            topic["active_unread_count"],
            topic["passive_unread_count"],
        ) == expected_counts

    response = api.get(f"{FOLDERS}{custom_folder_uuid}", user=target_user)
    assert response.status_code == 200, response.text
    custom_folder = response.json()
    assert custom_folder["unread_count"] == 2
    assert (
        custom_folder["folder_items"][0]["unread_count"],
        custom_folder["folder_items"][0]["active_unread_count"],
        custom_folder["folder_items"][0]["passive_unread_count"],
    ) == (5, 2, 3)

    response = api.get(
        f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}",
        user=target_user,
    )
    assert response.status_code == 200, response.text
    all_folder = response.json()
    assert all_folder["unread_count"] == 4
    items_by_stream = {item["stream_uuid"]: item for item in all_folder["folder_items"]}
    for stream_uuid, expected_counts in expected_stream_counts.items():
        item = items_by_stream[stream_uuid]
        assert (
            item["unread_count"],
            item["active_unread_count"],
            item["passive_unread_count"],
        ) == expected_counts

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, target_user),
        )
        before_topic_mode_epoch = cur.fetchone()[0]
    set_topic_mode(followed_topic, "mute")
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, target_user, before_topic_mode_epoch),
        )
        topic_mode_events = [row[0] for row in cur.fetchall()]
    assert [event["kind"] for event in topic_mode_events] == [
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    custom_folder_event = next(
        event
        for event in topic_mode_events
        if event["kind"] == "folder.updated" and event["uuid"] == custom_folder_uuid
    )
    assert custom_folder_event["unread_count"] == 1
    assert (
        custom_folder_event["folder_items"][0]["unread_count"],
        custom_folder_event["folder_items"][0]["active_unread_count"],
        custom_folder_event["folder_items"][0]["passive_unread_count"],
    ) == (5, 1, 4)
    set_topic_mode(followed_topic, "follow")

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, target_user),
        )
        before_stream_mode_epoch = cur.fetchone()[0]
    response = api.post(
        f"{STREAMS}{all_messages_stream}/actions/notifications/invoke",
        user=target_user,
        json={"notification_mode": "muted"},
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["unread_count"],
        response.json()["active_unread_count"],
        response.json()["passive_unread_count"],
    ) == (1, 0, 1)
    response = api.get(
        f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}",
        user=target_user,
    )
    assert response.json()["unread_count"] == 3
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, target_user, before_stream_mode_epoch),
        )
        stream_mode_events = [row[0] for row in cur.fetchall()]
    assert [event["kind"] for event in stream_mode_events] == [
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert (
        stream_mode_events[0]["active_unread_count"],
        stream_mode_events[0]["passive_unread_count"],
    ) == (0, 1)
    assert (
        stream_mode_events[1]["active_unread_count"],
        stream_mode_events[1]["passive_unread_count"],
    ) == (0, 1)
    assert [event["unread_count"] for event in stream_mode_events[2:]] == [3, 3]

    set_topic_mode(all_messages_topic, "follow")
    response = api.get(f"{STREAMS}{all_messages_stream}", user=target_user)
    assert (
        response.json()["unread_count"],
        response.json()["active_unread_count"],
        response.json()["passive_unread_count"],
    ) == (1, 1, 0)
    response = api.get(
        f"{FOLDERS}{messenger_dm_helpers.ALL_CHATS_FOLDER_UUID}",
        user=target_user,
    )
    assert response.json()["unread_count"] == 4

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND payload->>'kind' = 'stream.updated'
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (api.project_id, target_user, muted_stream),
        )
        latest_stream_event = cur.fetchone()[0]
    assert latest_stream_event["unread_count"] == 5
    assert latest_stream_event["active_unread_count"] == 2
    assert latest_stream_event["passive_unread_count"] == 3


def test_compact_topic_unread_event_serializes_non_default_as_false(api, db):
    target_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact topic boolean"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, target_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "Imported topic"
    )

    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "new message"},
        },
    )
    assert response.status_code == 201, response.text

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND payload->>'kind' = 'topic.updated'
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (api.project_id, target_user, topic_uuid),
        )
        payload = cursor.fetchone()[0]

    assert payload["unread_count"] == 1
    assert payload["is_default"] is False


def test_scoped_read_state_snapshots_match_public_views_in_every_mode(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Scoped snapshot parity"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_ROLLBACK)
    message_uuids = []
    for content in (
        "read message",
        f"mention [Reader](urn:user:{reader_uuid})",
        "passive message",
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    users = sorted(
        [sys_uuid.UUID(str(api.user_uuid)), reader_uuid],
        key=str,
    )
    modes = (
        read_state.PROJECT_MODE_LEGACY,
        read_state.PROJECT_MODE_PREPARING,
        read_state.PROJECT_MODE_DUAL,
        read_state.PROJECT_MODE_COMPACT,
        read_state.PROJECT_MODE_ROLLBACK,
    )
    for mode in modes:
        _set_workspace_read_mode(db, api.project_id, mode)
        with db.cursor() as cursor:
            cursor.execute(
                messenger_dm_helpers._READ_STATE_USER_STREAM_SNAPSHOTS_SQL,
                (
                    users,
                    api.project_id,
                    users,
                    api.project_id,
                    stream_uuid,
                    api.project_id,
                    stream_uuid,
                ),
            )
            scoped_streams = cursor.fetchall()
            cursor.execute(
                """
                SELECT * FROM m_workspace_user_streams
                WHERE project_id = %s AND user_uuid = ANY(%s::uuid[])
                  AND uuid = %s
                ORDER BY user_uuid
                """,
                (api.project_id, users, stream_uuid),
            )
            assert scoped_streams == cursor.fetchall(), mode

            cursor.execute(
                messenger_dm_helpers._READ_STATE_USER_TOPIC_SNAPSHOTS_SQL,
                (
                    users,
                    [topic_uuid],
                    api.project_id,
                    users,
                    api.project_id,
                    api.project_id,
                ),
            )
            scoped_topics = cursor.fetchall()
            cursor.execute(
                """
                SELECT * FROM m_workspace_user_topics_view
                WHERE project_id = %s AND user_uuid = ANY(%s::uuid[])
                  AND uuid = ANY(%s::uuid[])
                ORDER BY uuid, user_uuid
                """,
                (api.project_id, users, [topic_uuid]),
            )
            assert scoped_topics == cursor.fetchall(), mode

            cursor.execute(
                messenger_dm_helpers._READ_STATE_USER_MESSAGE_SNAPSHOTS_SQL,
                (
                    users,
                    message_uuids,
                    api.project_id,
                    users,
                    api.project_id,
                    api.project_id,
                ),
            )
            scoped_messages = cursor.fetchall()
            cursor.execute(
                """
                SELECT * FROM m_workspace_user_messages_view
                WHERE project_id = %s AND user_uuid = ANY(%s::uuid[])
                  AND uuid = ANY(%s::uuid[])
                ORDER BY created_at, uuid, user_uuid
                """,
                (api.project_id, users, message_uuids),
            )
            assert scoped_messages == cursor.fetchall(), mode

    def relation_loops(plan, relation):
        loops = []
        if plan.get("Relation Name") == relation:
            loops.append(plan.get("Actual Loops", 0))
        for child in plan.get("Plans", []):
            loops.extend(relation_loops(child, relation))
        return loops

    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    with db.cursor() as cursor:
        cursor.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            + messenger_dm_helpers._READ_STATE_USER_STREAM_SNAPSHOTS_SQL,
            (
                users,
                api.project_id,
                users,
                api.project_id,
                stream_uuid,
                api.project_id,
                stream_uuid,
            ),
        )
        compact_plan = cursor.fetchone()[0][0]["Plan"]
    assert not any(relation_loops(compact_plan, "m_workspace_user_message_flags"))
    assert not any(relation_loops(compact_plan, "m_external_accounts_v2"))

    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    with db.cursor() as cursor:
        cursor.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) "
            + messenger_dm_helpers._READ_STATE_USER_STREAM_SNAPSHOTS_SQL,
            (
                users,
                api.project_id,
                users,
                api.project_id,
                stream_uuid,
                api.project_id,
                stream_uuid,
            ),
        )
        legacy_plan = cursor.fetchone()[0][0]["Plan"]
    assert not any(relation_loops(legacy_plan, "m_workspace_topic_message_stats_v1"))


def test_message_mention_edit_refreshes_unread_snapshots(api, db):
    target_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Mention edit counters"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, target_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "Mention edits"
    )
    response = api.post(
        f"{STREAMS}{stream_uuid}/actions/notifications/invoke",
        user=target_user,
        json={"notification_mode": "mentions_only"},
    )
    assert response.status_code == 200, response.text
    response = api.post(
        FOLDERS,
        user=target_user,
        json={"title": "Mention edit folder"},
    )
    assert response.status_code in (200, 201), response.text
    custom_folder_uuid = response.json()["uuid"]
    response = api.post(
        FOLDER_ITEMS,
        user=target_user,
        json={
            "folder_uuid": custom_folder_uuid,
            "stream_uuid": stream_uuid,
            "chat_type": "stream",
        },
    )
    assert response.status_code in (200, 201), response.text
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "passive before edit"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, target_user),
        )
        before_edit_epoch = cur.fetchone()[0]

    mention = f"[Target](urn:user:{target_user})"
    response = api.put(
        f"{MESSAGES}{message_uuid}",
        json={
            "payload": {
                "kind": "markdown",
                "content": f"active after edit {mention}",
            }
        },
    )
    assert response.status_code == 200, response.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, target_user, before_edit_epoch),
        )
        edit_events = [row[0] for row in cur.fetchall()]
    assert [event["kind"] for event in edit_events] == [
        "message.updated",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert edit_events[0]["mentioned"] is True
    assert (
        edit_events[1]["active_unread_count"],
        edit_events[1]["passive_unread_count"],
    ) == (1, 0)
    assert (
        edit_events[2]["active_unread_count"],
        edit_events[2]["passive_unread_count"],
    ) == (1, 0)
    custom_folder_event = next(
        event
        for event in edit_events
        if event["kind"] == "folder.updated" and event["uuid"] == custom_folder_uuid
    )
    assert custom_folder_event["unread_count"] == 1
    assert (
        custom_folder_event["folder_items"][0]["active_unread_count"],
        custom_folder_event["folder_items"][0]["passive_unread_count"],
    ) == (1, 0)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, target_user),
        )
        before_remove_epoch = cur.fetchone()[0]
    response = api.put(
        f"{MESSAGES}{message_uuid}",
        json={
            "payload": {
                "kind": "markdown",
                "content": "passive after removing mention",
            }
        },
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, target_user, before_remove_epoch),
        )
        remove_events = [row[0] for row in cur.fetchall()]
    assert remove_events[0]["kind"] == "message.updated"
    assert remove_events[0]["mentioned"] is False
    assert (
        remove_events[1]["active_unread_count"],
        remove_events[1]["passive_unread_count"],
    ) == (0, 1)
    assert (
        remove_events[2]["active_unread_count"],
        remove_events[2]["passive_unread_count"],
    ) == (0, 1)
    custom_folder_event = next(
        event
        for event in remove_events
        if event["kind"] == "folder.updated" and event["uuid"] == custom_folder_uuid
    )
    assert custom_folder_event["unread_count"] == 0
    assert (
        custom_folder_event["folder_items"][0]["active_unread_count"],
        custom_folder_event["folder_items"][0]["passive_unread_count"],
    ) == (0, 1)


def test_stream_topic_delete_cascades_topic_messages(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-delete-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )
    message_uuid = str(sys_uuid.uuid4())

    resp = api.post(
        MESSAGES,
        json={
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "delete with topic",
            },
        },
    )
    assert resp.status_code == 201, resp.text

    resp = api.delete(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_stream_topics
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_topic_flags
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s)
            """,
            (topic_uuid, topic_uuid, message_uuid, message_uuid),
        )
        counts = cur.fetchone()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.deleted'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert counts == (0, 0, 0, 0)
    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["stream_uuid"] == stream_uuid


def test_stream_topic_set_default_updates_stream_and_topics(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-default-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    previous_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "planning"
    )

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/actions/set_default/invoke")
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == topic_uuid
    assert resp.json()["is_default"] is True

    resp = api.get(f"{STREAMS}{stream_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_topic_uuid"] == topic_uuid

    resp = api.get(f"{STREAM_TOPICS}{previous_topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_default"] is False

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'stream.updated'
              AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, stream_uuid),
        )
        stream_events = cur.fetchall()
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'topic.updated'
              AND payload->>'uuid' IN (%s, %s)
            ORDER BY payload->>'uuid', user_uuid
            """,
            (api.project_id, previous_topic_uuid, topic_uuid),
        )
        topic_events = cur.fetchall()

    assert {str(row[0]) for row in stream_events} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert all(
        payload["default_topic_uuid"] == topic_uuid for _, payload in stream_events
    )
    assert len(topic_events) == 4
    assert {
        (payload["uuid"], payload["is_default"]) for _, payload in topic_events
    } == {
        (previous_topic_uuid, False),
        (topic_uuid, True),
    }


def test_stream_default_topic_delete_sends_stream_update(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "topic-default-delete-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )

    resp = api.delete(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{STREAMS}{stream_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json().get("default_topic_uuid") is None

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
              AND payload->>'kind' = 'stream.updated'
              AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, stream_uuid),
        )
        stream_events = cur.fetchall()

    assert {str(row[0]) for row in stream_events} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert all(payload["default_topic_uuid"] is None for _, payload in stream_events)


def test_stream_topic_is_done_flag(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "team-chat"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "standups"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/actions/toggle_done/invoke")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is True

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is True

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'topic.updated'
                AND payload->>'uuid' = %s
                AND payload->>'is_done' = 'true'
            ORDER BY user_uuid
            """,
            (api.project_id, topic_uuid),
        )
        event_rows = cur.fetchall()

    assert {str(row[0]) for row in event_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    for _, payload in event_rows:
        assert payload["stream_uuid"] == stream_uuid
        assert payload["is_done"] is True

    resp = api.post(f"{STREAM_TOPICS}{topic_uuid}/actions/toggle_done/invoke")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False

    resp = api.get(f"{STREAM_TOPICS}{topic_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_done"] is False


# --------------------------------------------------------------------------- #
# Message events: durable epoch/outbox delivery
# --------------------------------------------------------------------------- #


def test_epoch_is_zero_without_visible_events(api, workspace_api):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    resp = workspace_api.get(EPOCH)
    assert resp.status_code == 200, resp.text
    cursor = resp.json()
    assert cursor["epoch_version"] == 0
    assert cursor["current_epoch_version"] == 0
    assert cursor["minimum_epoch_version"] == 1
    assert cursor["epoch_generation"]


def test_message_create_writes_compact_read_state_and_visible_events(
    api, workspace_api, db
):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    other_user = sys_uuid.uuid4()
    outsider = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)

    resp = api.post(
        MESSAGES,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "hello over epochs",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    message = resp.json()
    message_uuid = message["uuid"]
    public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid.lower())
    )
    assert message["read"] is True
    assert message["is_own"] is True
    assert message["reactions"] == {}

    other_message_resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert other_message_resp.status_code == 200, other_message_resp.text
    other_message = other_message_resp.json()
    assert other_message["read"] is False
    assert other_message["is_own"] is False
    assert other_message["reactions"] == {}

    assert _workspace_message_read_states(
        db,
        api.project_id,
        api.user_uuid,
        [message_uuid],
    ) == {message_uuid: True}
    assert _workspace_message_read_states(
        db,
        api.project_id,
        other_user,
        [message_uuid],
    ) == {message_uuid: False}
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_message_flags
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            """
            SELECT user_uuid, bit_count(read_bits)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid IN (%s, %s)
            """,
            (api.user_uuid, other_user),
        )
        compact_rows = {str(row[0]): row[1] for row in cur.fetchall()}
    assert compact_rows == {
        str(api.user_uuid): 1,
    }

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT last_message_uuid
            FROM m_workspace_user_streams
            WHERE project_id = %s
                AND uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, api.user_uuid),
        )
        assert str(cur.fetchone()[0]) == message_uuid
        cur.execute(
            """
            SELECT last_message_uuid
            FROM m_workspace_user_topics_view
            WHERE project_id = %s
                AND uuid = %s
                AND user_uuid = %s
            """,
            (api.project_id, topic_uuid, api.user_uuid),
        )
        assert str(cur.fetchone()[0]) == message_uuid

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (api.project_id,),
        )
        event_rows = cur.fetchall()
    events_by_user = {}
    for user_uuid, payload in event_rows:
        events_by_user.setdefault(str(user_uuid), []).append(payload)

    assert set(events_by_user) == {str(api.user_uuid), str(other_user)}
    assert [payload["kind"] for payload in events_by_user[str(api.user_uuid)]] == [
        "message.created",
    ]
    assert [payload["kind"] for payload in events_by_user[str(other_user)]] == [
        "message.created",
        "topic.updated",
        "stream.updated",
    ]
    author_payload = events_by_user[str(api.user_uuid)][0]
    other_payload = events_by_user[str(other_user)][0]
    assert author_payload["kind"] == "message.created"
    assert author_payload["uuid"] == public_message_uuid
    assert author_payload["stream_uuid"] == stream_uuid
    assert author_payload["topic_uuid"] == topic_uuid
    assert author_payload["author_uuid"] == str(api.user_uuid)
    assert author_payload["payload"] == {
        "kind": "markdown",
        "content": "hello over epochs",
    }
    assert author_payload["user_uuid"] == str(api.user_uuid)
    assert author_payload["project_id"] == str(api.project_id)
    assert author_payload["read"] is True
    assert author_payload["pinned"] is False
    assert author_payload["starred"] is False
    assert author_payload["is_own"] is True
    assert author_payload["reactions"] == {}
    assert other_payload["user_uuid"] == str(other_user)
    assert other_payload["project_id"] == str(api.project_id)
    assert other_payload["read"] is False
    assert other_payload["pinned"] is False
    assert other_payload["starred"] is False
    assert other_payload["is_own"] is False
    assert other_payload["reactions"] == {}
    packed_author_payload = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 1,
            "user_uuid": api.user_uuid,
            "payload": author_payload,
        }
    )["payload"]
    packed_other_payload = messenger_events.event_row_to_messenger_event(
        {
            "epoch_version": 2,
            "user_uuid": other_user,
            "payload": other_payload,
        }
    )["payload"]
    assert packed_author_payload["kind"] == "message.created"
    assert packed_other_payload["kind"] == "message.created"
    assert {
        key: value for key, value in packed_author_payload.items() if key != "kind"
    } == {**message, "uuid": public_message_uuid}
    assert {
        key: value for key, value in packed_other_payload.items() if key != "kind"
    } == {**other_message, "uuid": public_message_uuid}

    author_resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert author_resp.status_code == 200, author_resp.text
    author_events = author_resp.json()
    assert len(author_events) == 1
    event = author_events[0]
    assert event["project_id"] == str(api.project_id)
    assert event["user_uuid"] == str(api.user_uuid)
    assert event["payload"]["kind"] == "message.created"
    assert event["payload"]["uuid"] == public_message_uuid
    assert event["payload"]["stream_uuid"] == stream_uuid
    assert event["payload"]["topic_uuid"] == topic_uuid
    assert event["payload"]["author_uuid"] == str(api.user_uuid)
    assert event["payload"]["payload"]["content"] == "hello over epochs"
    assert event["payload"]["user_uuid"] == str(api.user_uuid)
    assert event["payload"]["project_id"] == str(api.project_id)
    assert event["payload"]["read"] is True
    assert event["payload"]["is_own"] is True
    assert event["payload"]["reactions"] == {}

    other_events = workspace_api.get(
        EVENTS,
        user=other_user,
        params={"page_limit": 100},
    ).json()
    assert [event["payload"]["kind"] for event in other_events] == [
        "message.created",
        "topic.updated",
        "stream.updated",
    ]
    other_event = other_events[0]
    assert other_event["payload"]["uuid"] == public_message_uuid
    assert other_event["payload"]["kind"] == "message.created"
    assert other_event["payload"]["user_uuid"] == str(other_user)
    assert other_event["payload"]["project_id"] == str(api.project_id)
    assert other_event["payload"]["read"] is False
    assert other_event["payload"]["is_own"] is False
    assert other_event["payload"]["reactions"] == {}
    assert other_events[1]["payload"]["last_message_uuid"] == public_message_uuid
    assert other_events[2]["payload"]["last_message_uuid"] == public_message_uuid

    outsider_events = workspace_api.get(
        EVENTS,
        user=outsider,
        params={"page_limit": 100},
    ).json()
    assert outsider_events == []

    epoch_generation = workspace_api.get(EPOCH).json()["epoch_generation"]
    next_page = workspace_api.get(
        EVENTS,
        params={
            "page_limit": 100,
            "page_marker": event["epoch_version"],
            "epoch_generation": epoch_generation,
        },
    ).json()
    assert next_page == []


def test_message_star_actions_are_user_scoped_idempotent_and_realtime(api, db):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    other_user = sys_uuid.uuid4()
    outsider_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "starred-messages"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "keep this message",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_actions_epoch = cursor.fetchone()[0]

    star_path = f"{MESSAGES}{message_uuid}/actions/star/invoke"
    response = api.post(star_path, user=other_user)
    assert response.status_code == 200, response.text
    assert response.json()["starred"] is True

    response = api.post(star_path, user=other_user)
    assert response.status_code == 200, response.text
    assert response.json()["starred"] is True

    author_message = api.get(f"{MESSAGES}{message_uuid}")
    assert author_message.status_code == 200, author_message.text
    assert author_message.json()["starred"] is False

    starred_page = api.get(
        MESSAGES,
        user=other_user,
        params={"starred": "true"},
    )
    assert starred_page.status_code == 200, starred_page.text
    assert [item["uuid"] for item in starred_page.json()] == [message_uuid]

    author_starred_page = api.get(MESSAGES, params={"starred": "true"})
    assert author_starred_page.status_code == 200, author_starred_page.text
    assert author_starred_page.json() == []

    outsider_response = api.post(star_path, user=outsider_user)
    assert outsider_response.status_code == 404, outsider_response.text

    unstar_path = f"{MESSAGES}{message_uuid}/actions/unstar/invoke"
    response = api.post(unstar_path, user=other_user)
    assert response.status_code == 200, response.text
    assert response.json()["starred"] is False

    response = api.post(unstar_path, user=other_user)
    assert response.status_code == 200, response.text
    assert response.json()["starred"] is False

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, starred
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = %s
            ORDER BY user_uuid
            """,
            (api.project_id, message_uuid),
        )
        flags = {str(user_uuid): starred for user_uuid, starred in cursor.fetchall()}
        cursor.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_actions_epoch),
        )
        event_rows = cursor.fetchall()

    # Compact projects materialize only exceptional pin/star rows.  Read state
    # and ordinary false flag values stay implicit.
    assert flags == {str(other_user): False}
    assert [str(user_uuid) for user_uuid, _payload in event_rows] == [
        str(other_user),
        str(other_user),
    ]
    assert [payload["kind"] for _user_uuid, payload in event_rows] == [
        "message.updated",
        "message.updated",
    ]
    assert [payload["starred"] for _user_uuid, payload in event_rows] == [
        True,
        False,
    ]


def test_message_star_update_is_serialized_and_concurrently_idempotent(api, db):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "concurrent-starred-messages"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "keep this message concurrently",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    public_message_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(topic_uuid), str(message_uuid).lower()
    )
    project_uuid = sys_uuid.UUID(api.project_id)
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    def latest_epoch():
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(epoch_version), 0)
                FROM m_workspace_visible_events
                WHERE project_id = %s
                """,
                (project_uuid,),
            )
            return cursor.fetchone()[0]

    def message_events_after(epoch_version):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM m_workspace_visible_events
                WHERE project_id = %s
                  AND user_uuid = %s
                  AND epoch_version > %s
                  AND payload->>'uuid' = %s
                ORDER BY epoch_version
                """,
                (
                    project_uuid,
                    other_user,
                    epoch_version,
                    str(public_message_uuid),
                ),
            )
            return [row[0] for row in cursor.fetchall()]

    class CoordinatedSession:
        def __init__(self, session, before_project_lock):
            self._session = session
            self._before_project_lock = before_project_lock
            self._coordinated = False

        def execute(self, statement, params=()):
            if (
                not self._coordinated
                and isinstance(statement, str)
                and "pg_advisory_xact_lock" in statement
            ):
                self._coordinated = True
                self._before_project_lock()
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def update_flags(values, before_project_lock=lambda: None):
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '5s'")
            return messenger_dm_helpers.sync_workspace_user_message_flags(
                project_id=project_uuid,
                user_uuid=other_user,
                message_uuid=message_uuid,
                values=values,
                session=CoordinatedSession(session, before_project_lock),
            )

    def mark_read(before_project_lock=lambda: None):
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '5s'")
            return messenger_dm_helpers.read_workspace_user_message(
                project_id=project_uuid,
                user_uuid=other_user,
                message_uuid=message_uuid,
                session=CoordinatedSession(session, before_project_lock),
            )

    before_read_race_epoch = latest_epoch()
    star_project_lock_started = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        with session_factory() as read_session:
            read_session.execute("SET LOCAL statement_timeout = '5s'")
            read_message = messenger_dm_helpers.read_workspace_user_message(
                project_id=project_uuid,
                user_uuid=other_user,
                message_uuid=message_uuid,
                session=read_session,
            )
            star_future = executor.submit(
                update_flags,
                {"starred": True},
                star_project_lock_started.set,
            )
            assert star_project_lock_started.wait(timeout=3)
        starred_message = star_future.result(timeout=5)

    assert read_message.read is True
    assert read_message.starred is False
    assert starred_message.read is True
    assert starred_message.starred is True
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read, starred
            FROM m_workspace_user_messages_view
            WHERE uuid = %s AND project_id = %s AND user_uuid = %s
            """,
            (message_uuid, project_uuid, other_user),
        )
        assert cursor.fetchone() == (True, True)
    read_race_events = message_events_after(before_read_race_epoch)
    assert [event["kind"] for event in read_race_events] == [
        "message.read",
        "message.updated",
    ]
    assert read_race_events[-1]["read"] is True
    assert read_race_events[-1]["starred"] is True

    _run_database_operation(
        lambda session: messenger_dm_helpers.sync_workspace_user_message_flags(
            project_id=project_uuid,
            user_uuid=other_user,
            message_uuid=message_uuid,
            values={"starred": False},
            session=session,
            emit_events=False,
        )
    )
    before_duplicate_epoch = latest_epoch()
    update_barrier = threading.Barrier(2)

    def wait_for_both_updates():
        update_barrier.wait(timeout=5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                update_flags,
                {"starred": True},
                wait_for_both_updates,
            ),
            executor.submit(
                update_flags,
                {"starred": True},
                wait_for_both_updates,
            ),
        ]
        starred_messages = [future.result(timeout=10) for future in futures]

    assert all(message.read is True for message in starred_messages)
    assert all(message.starred is True for message in starred_messages)
    duplicate_events = message_events_after(before_duplicate_epoch)
    assert len(duplicate_events) == 1
    assert duplicate_events[0]["kind"] == "message.updated"
    assert duplicate_events[0]["starred"] is True

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read, starred
            FROM m_workspace_user_messages_view
            WHERE uuid = %s AND project_id = %s AND user_uuid = %s
            """,
            (message_uuid, project_uuid, other_user),
        )
        assert cursor.fetchone() == (True, True)
    _run_database_operation(
        lambda session: (
            read_state.set_message_read(
                session,
                project_uuid,
                other_user,
                message_uuid,
                False,
            ),
            messenger_dm_helpers.sync_workspace_user_message_flags(
                project_id=project_uuid,
                user_uuid=other_user,
                message_uuid=message_uuid,
                values={"starred": False},
                session=session,
                emit_events=False,
            ),
        )
    )
    before_star_race_epoch = latest_epoch()
    read_project_lock_started = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        with session_factory() as star_session:
            star_session.execute("SET LOCAL statement_timeout = '5s'")
            starred_message = messenger_dm_helpers.sync_workspace_user_message_flags(
                project_id=project_uuid,
                user_uuid=other_user,
                message_uuid=message_uuid,
                values={"starred": True},
                session=star_session,
            )
            read_future = executor.submit(
                mark_read,
                read_project_lock_started.set,
            )
            assert read_project_lock_started.wait(timeout=3)
        read_message = read_future.result(timeout=5)

    assert starred_message.read is False
    assert starred_message.starred is True
    assert read_message.read is True
    assert read_message.starred is True
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read, starred
            FROM m_workspace_user_messages_view
            WHERE uuid = %s AND project_id = %s AND user_uuid = %s
            """,
            (message_uuid, project_uuid, other_user),
        )
        assert cursor.fetchone() == (True, True)
    star_race_events = message_events_after(before_star_race_epoch)
    assert [event["kind"] for event in star_race_events] == [
        "message.updated",
        "message.read",
    ]
    assert star_race_events[-1]["read"] is True
    assert star_race_events[-1]["starred"] is True


def test_message_update_read_delete_write_realtime_events(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "message-crud-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "first version",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    created_message = resp.json()
    message_uuid = created_message["uuid"]
    public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid.lower())
    )
    message_created_at = created_message["created_at"]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_read_epoch = cur.fetchone()[0]

    resp = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["read"] is True

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT read
            FROM m_workspace_user_messages_view
            WHERE uuid = %s
                AND project_id = %s
                AND user_uuid = %s
            """,
            (message_uuid, api.project_id, str(other_user)),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND user_uuid = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, str(other_user), before_read_epoch),
        )
        read_events = [row[0] for row in cur.fetchall()]

    assert [event["kind"] for event in read_events] == [
        "message.read",
        "topic.updated",
        "stream.updated",
        "folder.updated",
        "folder.updated",
    ]
    assert read_events[0]["uuid"] == public_message_uuid
    assert read_events[0]["read"] is True
    assert read_events[1]["unread_count"] == 0
    assert read_events[2]["unread_count"] == 0
    assert [event["unread_count"] for event in read_events[3:]] == [0, 0]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_update_epoch = cur.fetchone()[0]

    resp = api.put(
        f"{MESSAGES}{message_uuid}",
        json={
            "payload": {
                "kind": "markdown",
                "content": "edited version",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    updated_message = resp.json()
    assert updated_message["payload"]["content"] == "edited version"
    assert updated_message["created_at"] == message_created_at
    assert updated_message["updated_at"] != message_created_at

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    reloaded_message = resp.json()
    assert reloaded_message["payload"]["content"] == "edited version"
    assert reloaded_message["created_at"] == message_created_at
    assert reloaded_message["updated_at"] == updated_message["updated_at"]

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_update_epoch),
        )
        update_rows = cur.fetchall()

    assert {str(row[0]) for row in update_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert [row[1]["kind"] for row in update_rows] == [
        "message.updated",
        "message.updated",
    ]
    assert all(row[1]["payload"]["content"] == "edited version" for row in update_rows)
    assert all(row[1]["created_at"] == message_created_at for row in update_rows)
    assert all(
        row[1]["updated_at"] == updated_message["updated_at"] for row in update_rows
    )

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_delete_epoch = cur.fetchone()[0]

    resp = api.delete(f"{MESSAGES}{message_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 404, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_workspace_messages
                 WHERE uuid = %s),
                (SELECT COUNT(*) FROM m_workspace_user_message_flags
                 WHERE uuid = %s)
            """,
            (message_uuid, message_uuid),
        )
        assert cur.fetchone() == (0, 0)
        cur.execute(
            """
            SELECT s.last_message_uuid, t.last_message_uuid
            FROM m_workspace_user_streams AS s
            JOIN m_workspace_user_topics_view AS t
                ON t.stream_uuid = s.uuid
                AND t.project_id = s.project_id
                AND t.user_uuid = s.user_uuid
            WHERE s.project_id = %s
                AND s.uuid = %s
                AND t.uuid = %s
                AND s.user_uuid = %s
            """,
            (api.project_id, stream_uuid, topic_uuid, api.user_uuid),
        )
        assert cur.fetchone() == (None, None)
        cur.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_delete_epoch),
        )
        delete_rows = cur.fetchall()

    assert {str(row[0]) for row in delete_rows} == {
        str(api.user_uuid),
        str(other_user),
    }
    assert [row[1]["kind"] for row in delete_rows] == [
        "message.deleted",
        "message.deleted",
    ]
    assert all(row[1]["uuid"] == public_message_uuid for row in delete_rows)
    assert all(row[1]["stream_uuid"] == stream_uuid for row in delete_rows)
    assert all(row[1]["topic_uuid"] == topic_uuid for row in delete_rows)


def test_provider_message_update_preserves_created_at_in_storage_api_and_event(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "provider-created-at",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    created = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "before provider echo",
            },
        },
    )
    assert created.status_code == 201, created.text
    message = created.json()
    message_uuid = sys_uuid.UUID(message["uuid"])

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_update_epoch = cursor.fetchone()[0]

    incoming_created_at = "2099-01-01T00:00:00Z"
    event = {
        "kind": "message.upsert",
        "external_account_uuid": str(sys_uuid.uuid4()),
    }
    resource = {
        "uuid": str(message_uuid),
        "user_uuid": str(api.user_uuid),
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "payload": {
            "kind": "markdown",
            "content": "after provider echo",
        },
        "created_at": incoming_created_at,
    }
    identity = types.SimpleNamespace(
        provider_kind=messenger_models.SourceName.NATIVE.value,
        bridge_instance_uuid=sys_uuid.uuid4(),
    )
    assignment = {
        "owner_user_uuid": api.user_uuid,
        "projection_stream_uuid": sys_uuid.UUID(stream_uuid),
    }

    def apply_provider_update(session):
        return provider_event_apply._message_event(
            session,
            event,
            sys_uuid.UUID(api.project_id),
            assignment,
            resource,
            identity,
        )

    assert _run_database_operation(apply_provider_update) == message_uuid

    reloaded = api.get(f"{MESSAGES}{message_uuid}")
    assert reloaded.status_code == 200, reloaded.text
    updated_message = reloaded.json()
    assert updated_message["payload"]["content"] == "after provider echo"
    assert updated_message["created_at"] == message["created_at"]
    assert updated_message["created_at"] != incoming_created_at

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT created_at, updated_at
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
        stored_created_at, stored_updated_at = cursor.fetchone()
        cursor.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND epoch_version > %s
              AND object_type = 'message'
              AND action = 'updated'
            ORDER BY epoch_version
            """,
            (api.project_id, api.user_uuid, before_update_epoch),
        )
        event_payloads = [row[0] for row in cursor.fetchall()]

    assert stored_created_at.isoformat() + "Z" == message["created_at"]
    assert stored_updated_at.isoformat() + "Z" == updated_message["updated_at"]
    assert len(event_payloads) == 1
    assert event_payloads[0]["kind"] == "message.updated"
    assert event_payloads[0]["created_at"] == message["created_at"]
    assert event_payloads[0]["updated_at"] == updated_message["updated_at"]


def test_message_reaction_crud_is_user_scoped_and_writes_message_events(api, db):
    other_user = sys_uuid.uuid4()
    outsider_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "reaction-crud-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )

    message_resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "react to this",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]
    public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid.lower())
    )
    assert message_resp.json()["reaction_users"] == {}

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(MAX(epoch_version), 0)
            FROM m_workspace_visible_events
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        before_reactions_epoch = cur.fetchone()[0]

    reaction_uuid = str(sys_uuid.uuid4())
    resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "uuid": reaction_uuid,
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert resp.status_code == 201, resp.text
    reaction = resp.json()
    assert reaction["uuid"] == reaction_uuid
    assert reaction["project_id"] == str(api.project_id)
    assert reaction["user_uuid"] == str(api.user_uuid)
    assert reaction["message_uuid"] == message_uuid
    assert reaction["emoji_name"] == "thumbs_up"
    assert "status" not in reaction

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["emoji_name"] == "thumbs_up"

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_uuid"] == str(api.user_uuid)

    resp = api.get(f"{MESSAGE_REACTIONS}{reaction_uuid}", user=outsider_user)
    assert resp.status_code == 404, resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {"thumbs_up": 1}
    assert resp.json()["reaction_users"] == {
        "thumbs_up": [str(api.user_uuid)],
    }

    duplicate_resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert duplicate_resp.status_code == 409, duplicate_resp.text

    second_resp = api.post(
        MESSAGE_REACTIONS,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "eyes",
        },
    )
    assert second_resp.status_code == 201, second_resp.text
    second_reaction_uuid = second_resp.json()["uuid"]

    other_resp = api.post(
        MESSAGE_REACTIONS,
        user=other_user,
        json={
            "message_uuid": message_uuid,
            "emoji_name": "thumbs_up",
        },
    )
    assert other_resp.status_code == 201, other_resp.text
    other_reaction_uuid = other_resp.json()["uuid"]

    resp = api.get(f"{MESSAGES}{message_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "eyes": 1,
        "thumbs_up": 2,
    }
    assert resp.json()["reaction_users"] == {
        "eyes": [str(api.user_uuid)],
        "thumbs_up": sorted((str(api.user_uuid), str(other_user))),
    }

    resp = api.get(MESSAGE_REACTIONS, params={"message_uuid": message_uuid})
    assert resp.status_code == 200, resp.text
    expected_reactions = {
        ("eyes", str(api.user_uuid)),
        ("thumbs_up", str(api.user_uuid)),
        ("thumbs_up", str(other_user)),
    }
    assert {
        (item["emoji_name"], item["user_uuid"]) for item in resp.json()
    } == expected_reactions

    other_filter_resp = api.get(
        MESSAGE_REACTIONS,
        user=other_user,
        params={"message_uuid": message_uuid},
    )
    assert other_filter_resp.status_code == 200, other_filter_resp.text
    assert {
        (item["emoji_name"], item["user_uuid"]) for item in other_filter_resp.json()
    } == expected_reactions

    user_filter_resp = api.get(
        MESSAGE_REACTIONS,
        params={"message_uuid": message_uuid, "user_uuid": str(other_user)},
    )
    assert user_filter_resp.status_code == 200, user_filter_resp.text
    assert [item["emoji_name"] for item in user_filter_resp.json()] == [
        "thumbs_up",
    ]

    outsider_filter_resp = api.get(
        MESSAGE_REACTIONS,
        user=outsider_user,
        params={"message_uuid": message_uuid},
    )
    assert outsider_filter_resp.status_code == 200, outsider_filter_resp.text
    assert outsider_filter_resp.json() == []

    resp = api.put(
        f"{MESSAGE_REACTIONS}{reaction_uuid}",
        json={"emoji_name": "heart"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["emoji_name"] == "heart"

    resp = api.get(f"{MESSAGES}{message_uuid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "eyes": 1,
        "heart": 1,
        "thumbs_up": 1,
    }
    assert resp.json()["reaction_users"] == {
        "eyes": [str(api.user_uuid)],
        "heart": [str(api.user_uuid)],
        "thumbs_up": [str(other_user)],
    }

    resp = api.delete(f"{MESSAGE_REACTIONS}{other_reaction_uuid}")
    assert resp.status_code == 404, resp.text

    resp = api.delete(f"{MESSAGE_REACTIONS}{second_reaction_uuid}")
    assert resp.status_code in (200, 204), resp.text

    resp = api.get(f"{MESSAGE_REACTIONS}{second_reaction_uuid}")
    assert resp.status_code == 404, resp.text

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reactions"] == {
        "heart": 1,
        "thumbs_up": 1,
    }
    assert resp.json()["reaction_users"] == {
        "heart": [str(api.user_uuid)],
        "thumbs_up": [str(other_user)],
    }

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT emoji_name, user_uuid
            FROM m_workspace_message_reactions
            WHERE project_id = %s
                AND message_uuid = %s
            ORDER BY emoji_name, user_uuid
            """,
            (api.project_id, message_uuid),
        )
        stored_reactions = [
            (emoji_name, str(user_uuid)) for emoji_name, user_uuid in cur.fetchall()
        ]
        cur.execute(
            """
            SELECT object_type, action, user_uuid, payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND epoch_version > %s
            ORDER BY epoch_version
            """,
            (api.project_id, before_reactions_epoch),
        )
        reaction_event_rows = [
            (object_type, action, str(user_uuid), payload)
            for object_type, action, user_uuid, payload in cur.fetchall()
        ]

    assert stored_reactions == [
        ("heart", str(api.user_uuid)),
        ("thumbs_up", str(other_user)),
    ]
    expected_event_users = {str(api.user_uuid), str(other_user)}
    expected_reaction_snapshots = [
        {"thumbs_up": 1},
        {"eyes": 1, "thumbs_up": 1},
        {"eyes": 1, "thumbs_up": 2},
        {"eyes": 1, "heart": 1, "thumbs_up": 1},
        {"heart": 1, "thumbs_up": 1},
    ]
    expected_reaction_user_snapshots = [
        {"thumbs_up": [str(api.user_uuid)]},
        {
            "eyes": [str(api.user_uuid)],
            "thumbs_up": [str(api.user_uuid)],
        },
        {
            "eyes": [str(api.user_uuid)],
            "thumbs_up": sorted((str(api.user_uuid), str(other_user))),
        },
        {
            "eyes": [str(api.user_uuid)],
            "heart": [str(api.user_uuid)],
            "thumbs_up": [str(other_user)],
        },
        {
            "heart": [str(api.user_uuid)],
            "thumbs_up": [str(other_user)],
        },
    ]
    message_event_rows = [row for row in reaction_event_rows if row[0] == "message"]
    reaction_state_event_rows = [
        row for row in reaction_event_rows if row[0] == "message_reaction"
    ]
    assert len(message_event_rows) == len(expected_reaction_snapshots) * 2
    assert all(
        action == "updated" and payload["kind"] == "message.updated"
        for _, action, _, payload in message_event_rows
    )
    assert all(
        payload["kind"] == "message.updated" for _, _, _, payload in message_event_rows
    )
    assert all(
        payload["uuid"] == public_message_uuid
        for _, _, _, payload in message_event_rows
    )
    for index, expected_reactions in enumerate(expected_reaction_snapshots):
        group = message_event_rows[index * 2 : index * 2 + 2]
        assert {user_uuid for _, _, user_uuid, _ in group} == expected_event_users
        assert all(
            payload["reactions"] == expected_reactions for _, _, _, payload in group
        )
        assert all(
            payload["reaction_users"] == expected_reaction_user_snapshots[index]
            for _, _, _, payload in group
        )

    expected_reaction_events = [
        ("created", str(api.user_uuid), reaction_uuid, "thumbs_up"),
        ("created", str(api.user_uuid), second_reaction_uuid, "eyes"),
        ("created", str(other_user), other_reaction_uuid, "thumbs_up"),
        ("updated", str(api.user_uuid), reaction_uuid, "heart"),
        ("deleted", str(api.user_uuid), second_reaction_uuid, "eyes"),
    ]
    assert len(reaction_state_event_rows) == len(expected_reaction_events)
    for event_row, expected in zip(
        reaction_state_event_rows,
        expected_reaction_events,
    ):
        _, action, event_user_uuid, payload = event_row
        expected_action, expected_user_uuid, expected_uuid, expected_emoji = expected
        assert action == expected_action
        assert event_user_uuid == expected_user_uuid
        assert payload["kind"] == f"message_reaction.{expected_action}"
        assert payload["uuid"] == expected_uuid
        assert payload["message_uuid"] == public_message_uuid
        assert payload["user_uuid"] == expected_user_uuid
        assert payload["emoji_name"] == expected_emoji
        assert payload["source_name"] == "native"
        assert payload["source"]["kind"] == "native"
        if expected_action == "updated":
            assert payload["old_message_uuid"] == public_message_uuid
            assert payload["old_emoji_name"] == "thumbs_up"
            assert payload["old_source_name"] == "native"
            assert payload["old_source"]["kind"] == "native"
        else:
            assert "old_message_uuid" not in payload
            assert "old_emoji_name" not in payload


def test_message_reaction_users_are_complete_at_limit_and_omit_large_groups(
    api,
    db,
):
    users = [sys_uuid.UUID(str(api.user_uuid))] + [
        sys_uuid.uuid4() for _index in range(4)
    ]
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "bounded-reaction-users",
    )
    for user_uuid in users[1:]:
        conftest.seed_user_stream_binding(
            db,
            api.project_id,
            stream_uuid,
            user_uuid,
        )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "bounded reaction users",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]
    public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid.lower())
    )

    for user_uuid in users[:4]:
        response = api.post(
            MESSAGE_REACTIONS,
            user=user_uuid,
            json={
                "message_uuid": message_uuid,
                "emoji_name": "heart",
            },
        )
        assert response.status_code == 201, response.text
    for user_uuid in users:
        response = api.post(
            MESSAGE_REACTIONS,
            user=user_uuid,
            json={
                "message_uuid": message_uuid,
                "emoji_name": "eyes",
            },
        )
        assert response.status_code == 201, response.text

    expected_reaction_users = {
        "heart": sorted(str(user_uuid) for user_uuid in users[:4]),
    }
    response = api.get(f"{MESSAGES}{message_uuid}")
    assert response.status_code == 200, response.text
    assert response.json()["reactions"] == {"eyes": 5, "heart": 4}
    assert response.json()["reaction_users"] == expected_reaction_users

    page_response = api.get(MESSAGES, params={"stream_uuid": str(stream_uuid)})
    assert page_response.status_code == 200, page_response.text
    page_message = next(
        item for item in page_response.json() if item["uuid"] == message_uuid
    )
    assert page_message["reaction_users"] == expected_reaction_users

    read_response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=users[-1],
    )
    assert read_response.status_code == 200, read_response.text
    assert read_response.json()["reaction_users"] == expected_reaction_users

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT reaction_users
            FROM m_workspace_messages
            WHERE project_id = %s
              AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
        stored_reaction_users = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND object_type = 'message'
              AND action = 'updated'
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (api.project_id, api.user_uuid, public_message_uuid),
        )
        event_payload = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
              AND user_uuid = %s
              AND object_type = 'message'
              AND action = 'read'
              AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (api.project_id, users[-1], public_message_uuid),
        )
        read_event_payload = cursor.fetchone()[0]
    assert stored_reaction_users == expected_reaction_users
    assert event_payload["reactions"] == {"eyes": 5, "heart": 4}
    assert event_payload["reaction_users"] == expected_reaction_users
    assert read_event_payload["reaction_users"] == expected_reaction_users


def test_reaction_user_limit_changes_apply_only_on_the_next_group_write(api, db):
    users = [sys_uuid.UUID(str(api.user_uuid))] + [
        sys_uuid.uuid4() for _index in range(3)
    ]
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "lazy-reaction-user-limit",
    )
    for user_uuid in users[1:]:
        conftest.seed_user_stream_binding(
            db,
            api.project_id,
            stream_uuid,
            user_uuid,
        )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "lazy reaction limit",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = message_resp.json()["uuid"]

    for user_uuid in users[:3]:
        response = api.post(
            MESSAGE_REACTIONS,
            user=user_uuid,
            json={
                "message_uuid": message_uuid,
                "emoji_name": "heart",
            },
        )
        assert response.status_code == 201, response.text

    expected_before_change = {
        "heart": sorted(str(user_uuid) for user_uuid in users[:3]),
    }
    response = api.get(f"{MESSAGES}{message_uuid}")
    assert response.status_code == 200, response.text
    assert response.json()["reaction_users"] == expected_before_change

    cfg.CONF.set_override(
        "user_list_limit",
        2,
        group=messenger_reaction_opts.DOMAIN,
    )
    try:
        response = api.get(f"{MESSAGES}{message_uuid}")
        assert response.status_code == 200, response.text
        assert response.json()["reaction_users"] == expected_before_change

        response = api.post(
            MESSAGE_REACTIONS,
            user=users[-1],
            json={
                "message_uuid": message_uuid,
                "emoji_name": "heart",
            },
        )
        assert response.status_code == 201, response.text
        response = api.get(f"{MESSAGES}{message_uuid}")
        assert response.status_code == 200, response.text
        assert response.json()["reactions"] == {"heart": 4}
        assert response.json()["reaction_users"] == {}
    finally:
        cfg.CONF.clear_override(
            "user_list_limit",
            group=messenger_reaction_opts.DOMAIN,
        )


def test_concurrent_reaction_writes_keep_one_complete_user_snapshot(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "concurrent-reaction-users",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        other_user,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "concurrent reaction users",
            },
        },
    )
    assert message_resp.status_code == 201, message_resp.text
    message_uuid = sys_uuid.UUID(message_resp.json()["uuid"])
    barrier = threading.Barrier(2)

    def create_reaction(user_uuid):
        def operation(session):
            barrier.wait(timeout=5)
            return messenger_dm_helpers.create_workspace_message_reaction(
                project_id=sys_uuid.UUID(api.project_id),
                user_uuid=user_uuid,
                message_uuid=message_uuid,
                emoji_name="heart",
                session=session,
                compact_events=True,
            )

        return _run_database_operation(operation)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        reactions = list(
            executor.map(
                create_reaction,
                (sys_uuid.UUID(str(api.user_uuid)), other_user),
            )
        )

    assert len({reaction.uuid for reaction in reactions}) == 2
    response = api.get(f"{MESSAGES}{message_uuid}")
    assert response.status_code == 200, response.text
    assert response.json()["reactions"] == {"heart": 2}
    assert response.json()["reaction_users"] == {
        "heart": sorted((str(api.user_uuid), str(other_user))),
    }


def test_stream_topic_and_message_read_actions_mark_expected_messages(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "read-actions-team"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    other_topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "random"
    )

    message_uuids = []
    for topic, content in (
        (topic_uuid, "first"),
        (topic_uuid, "second"),
        (topic_uuid, "third"),
        (other_topic_uuid, "other topic"),
    ):
        resp = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic,
                "payload": {
                    "kind": "markdown",
                    "content": content,
                },
            },
        )
        assert resp.status_code == 201, resp.text
        message_uuids.append(resp.json()["uuid"])

    def other_user_flags():
        return _workspace_message_read_states(
            db,
            api.project_id,
            other_user,
            message_uuids,
        )

    assert other_user_flags() == {
        message_uuids[0]: False,
        message_uuids[1]: False,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == message_uuids[1]
    assert resp.json()["read"] is True
    assert other_user_flags() == {
        message_uuids[0]: False,
        message_uuids[1]: True,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read_up_to/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == message_uuids[1]
    assert resp.json()["read"] is True
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: False,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{STREAM_TOPICS}{topic_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == topic_uuid
    assert resp.json()["unread_count"] == 0
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: True,
        message_uuids[3]: False,
    }

    resp = api.post(
        f"{STREAMS}{stream_uuid}/actions/read/invoke",
        user=other_user,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == stream_uuid
    assert resp.json()["unread_count"] == 0
    assert other_user_flags() == {
        message_uuids[0]: True,
        message_uuids[1]: True,
        message_uuids[2]: True,
        message_uuids[3]: True,
    }


def test_read_mutations_return_the_exact_rows_changed_by_postgres(api, db):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Atomic read snapshots",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        reader_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    other_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "random",
    )
    message_uuids = []
    for topic, content in (
        (topic_uuid, "first"),
        (topic_uuid, "second"),
        (other_topic_uuid, "other topic"),
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    def set_all_unread():
        _run_database_operation(
            lambda session: read_state.set_message_uuids_read(
                session,
                api.project_id,
                reader_uuid,
                message_uuids,
                False,
            )
        )

    set_all_unread()
    _message, changed = _run_database_operation(
        lambda session: messenger_dm_helpers.read_workspace_user_message(
            project_id=api.project_id,
            user_uuid=reader_uuid,
            message_uuid=message_uuids[0],
            session=session,
            return_message_uuids=True,
        )
    )
    assert [str(value) for value in changed] == [message_uuids[0]]
    _message, changed_again = _run_database_operation(
        lambda session: messenger_dm_helpers.read_workspace_user_message(
            project_id=api.project_id,
            user_uuid=reader_uuid,
            message_uuid=message_uuids[0],
            session=session,
            return_message_uuids=True,
        )
    )
    assert changed_again == []

    set_all_unread()
    _topic, changed = _run_database_operation(
        lambda session: messenger_dm_helpers.read_workspace_user_stream_topic_messages(
            project_id=api.project_id,
            user_uuid=reader_uuid,
            topic_uuid=topic_uuid,
            session=session,
            return_message_uuids=True,
        )
    )
    assert [str(value) for value in changed] == message_uuids[:2]

    set_all_unread()
    _stream, changed = _run_database_operation(
        lambda session: messenger_dm_helpers.read_workspace_user_stream_messages(
            project_id=api.project_id,
            user_uuid=reader_uuid,
            stream_uuid=stream_uuid,
            session=session,
            return_message_uuids=True,
        )
    )
    assert [str(value) for value in changed] == message_uuids


def test_provider_read_up_to_and_capability_refresh_do_not_deadlock(api, db):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    reader_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, reader_uuid, f"user-{reader_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Concurrent provider read state",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        reader_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuids = []
    for index in range(3):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": f"concurrent read {index}",
                },
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    source = {
        "kind": "zulip",
        "chat_type": "channel",
        "participants": [],
        "topics": [],
    }
    stream_source = {
        "kind": "zulip",
        "stream_id": 42,
        "server_url": "https://zulip.example.invalid",
        "source_scope": str(account_uuid),
    }
    fixed_created_at = datetime.datetime(
        2026,
        7,
        18,
        12,
        0,
        tzinfo=datetime.timezone.utc,
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), str(bridge_uuid)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, %s::jsonb
            )
            """,
            (
                str(account_uuid),
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.invalid",
                        "default_project_id": api.project_id,
                    }
                ),
                json.dumps(read_capability),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                str(sys_uuid.uuid4()),
                str(account_uuid),
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities,
                catalog_capabilities
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:42', %s::jsonb,
                'Concurrent provider read state', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                str(chat_uuid),
                str(account_uuid),
                api.user_uuid,
                json.dumps(source),
                api.project_id,
                str(stream_uuid),
                json.dumps(read_capability),
                json.dumps(read_capability),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip', source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:42'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(stream_source),
                str(account_uuid),
                api.project_id,
                str(stream_uuid),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET source_name = 'zulip', source = %s::jsonb,
                external_account_uuid = %s,
                created_at = %s
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (
                json.dumps(stream_source),
                str(account_uuid),
                fixed_created_at.replace(tzinfo=None),
                api.project_id,
                str(stream_uuid),
            ),
        )

    ordered_message_uuids = sorted(message_uuids)
    boundary_uuid = ordered_message_uuids[1]
    expected_message_uuids = [str(value) for value in ordered_message_uuids[:2]]

    def run_read(barrier):
        def operation(_session):
            barrier.wait(timeout=5)
            store = sql_canonical_store.SQLCanonicalMessengerStore(
                api.project_id,
                reader_uuid,
            )
            return store.perform_action(
                "messages",
                boundary_uuid,
                "read_up_to",
                {},
            )

        return _run_database_operation(operation)

    def run_refresh(barrier):
        def operation(session):
            session.execute("SET LOCAL lock_timeout = '2s'")
            session.execute("SET LOCAL statement_timeout = '5s'")
            barrier.wait(timeout=5)
            session.execute(
                """
                SELECT uuid
                FROM m_external_accounts_v2
                WHERE uuid = %s
                FOR UPDATE
                """,
                (str(account_uuid),),
            ).fetchone()
            return sql_state.refresh_effective_capabilities(
                session,
                account_uuid=account_uuid,
                now=datetime.datetime.now(datetime.timezone.utc),
            )

        return _run_database_operation(operation)

    for _cycle in range(8):
        _run_database_operation(
            lambda session: read_state.set_message_uuids_read(
                session,
                api.project_id,
                reader_uuid,
                message_uuids,
                False,
            )
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_external_accounts_v2
                SET capabilities = %s::jsonb
                WHERE uuid = %s
                """,
                (json.dumps(read_capability), str(account_uuid)),
            )
            cursor.execute(
                """
                UPDATE m_external_chats_v2
                SET capabilities = %s::jsonb
                WHERE uuid = %s
                """,
                (json.dumps(read_capability), str(chat_uuid)),
            )

        barrier = threading.Barrier(2)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            read_future = executor.submit(run_read, barrier)
            refresh_future = executor.submit(run_refresh, barrier)
            read_future.result(timeout=10)
            assert refresh_future.result(timeout=10) == 1

        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT uuid, read
                FROM m_workspace_user_messages_view
                WHERE project_id = %s AND user_uuid = %s
                  AND stream_uuid = %s
                ORDER BY created_at, uuid
                """,
                (api.project_id, str(reader_uuid), str(stream_uuid)),
            )
            flags = cursor.fetchall()
            cursor.execute(
                """
                SELECT array_agg(message.uuid ORDER BY message.created_at, message.uuid)
                FROM m_external_provider_read_snapshots_v1 AS snapshot
                JOIN m_external_provider_read_candidate_chunks_v1 AS candidate
                  ON candidate.external_operation_uuid =
                     snapshot.external_operation_uuid
                CROSS JOIN LATERAL generate_series(0, 4095) AS bit_offset
                JOIN m_workspace_messages AS message
                  ON message.project_id = snapshot.project_id
                 AND message.ingest_sequence =
                        candidate.chunk_number * 4096 + bit_offset
                WHERE snapshot.external_account_uuid = %s
                  AND get_bit(candidate.candidate_bits, bit_offset) = 1
                GROUP BY snapshot.queue_sequence
                ORDER BY snapshot.queue_sequence DESC
                LIMIT 1
                """,
                (str(account_uuid),),
            )
            candidate_uuids = cursor.fetchone()[0]

        assert [read for _uuid, read in flags] == [True, True, False]
        assert [str(value) for value in candidate_uuids] == expected_message_uuids

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_account_uuid = %s
            """,
            (str(account_uuid),),
        )
        assert cursor.fetchone()[0] == 8

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_provider_policies_v1
            SET emergency_suspended = TRUE
            WHERE provider = 'zulip'
            """
        )
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = '{}'::jsonb
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET capabilities = '{}'::jsonb
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET capabilities = '{}'::jsonb,
                catalog_capabilities = '{}'::jsonb
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
    db.commit()

    result = _run_database_operation(
        lambda _session: sql_canonical_store.SQLCanonicalMessengerStore(
            api.project_id,
            reader_uuid,
        ).perform_action(
            "messages",
            boundary_uuid,
            "read_up_to",
            {},
        )
    )
    assert result["uuid"] == str(boundary_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            """
                SELECT COUNT(*)
                FROM m_external_provider_read_snapshots_v1
                WHERE external_account_uuid = %s
                """,
            (str(account_uuid),),
        )
        assert cursor.fetchone()[0] == 8


def test_provider_read_prelocks_lane_before_project_during_page_lease(api, db):
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    reader_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, reader_uuid, f"user-{reader_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Provider read lane order",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        reader_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "lane order"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    source = {
        "kind": "zulip",
        "chat_type": "channel",
        "participants": [],
        "topics": [],
    }
    stream_source = {
        "kind": "zulip",
        "stream_id": 43,
        "server_url": "https://zulip.example.invalid",
        "source_scope": str(account_uuid),
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, %s::jsonb
            )
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.invalid",
                        "default_project_id": api.project_id,
                    }
                ),
                json.dumps(read_capability),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status, capabilities,
                catalog_capabilities
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:43', %s::jsonb,
                'Provider read lane order', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                chat_uuid,
                account_uuid,
                api.user_uuid,
                json.dumps(source),
                api.project_id,
                stream_uuid,
                json.dumps(read_capability),
                json.dumps(read_capability),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip', source = %s::jsonb,
                external_account_uuid = %s,
                provider_external_id = 'channel:43'
            WHERE project_id = %s AND uuid = %s
            """,
            (
                json.dumps(stream_source),
                account_uuid,
                api.project_id,
                stream_uuid,
            ),
        )
    db.commit()

    def read_stream():
        return _run_database_operation(
            lambda _session: sql_canonical_store.SQLCanonicalMessengerStore(
                api.project_id,
                reader_uuid,
            ).perform_action("streams", stream_uuid, "read", {})
        )

    read_stream()
    _run_database_operation(
        lambda session: read_state.set_message_uuids_read(
            session,
            api.project_id,
            reader_uuid,
            [message_uuid],
            False,
        )
    )

    lease_waiting_for_project = threading.Event()
    release_lease = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )

    class CoordinatedLeaseSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            if (
                not self._paused
                and isinstance(statement, str)
                and "hashtextextended(%s::text, 0)" in statement
                and params
                and str(params[0]) == str(api.project_id)
            ):
                self._paused = True
                lease_waiting_for_project.set()
                assert release_lease.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def lease_page():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '3s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            return provider_data.lease_provider_operations(
                CoordinatedLeaseSession(session),
                identity,
                request_uuid=sys_uuid.uuid4(),
                limit=1,
                lease_seconds=30,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        lease_future = executor.submit(lease_page)
        assert lease_waiting_for_project.wait(timeout=5)
        read_future = executor.submit(read_stream)
        with pytest.raises(concurrent.futures.TimeoutError):
            read_future.result(timeout=0.2)
        release_lease.set()
        lease = lease_future.result(timeout=8)
        read_future.result(timeout=8)

    assert len(lease["operations"]) == 1
    assert lease["operations"][0]["payload"]["message_uuids"] == [str(message_uuid)]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_account_uuid = %s
            """,
            (account_uuid,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            """
            SELECT read
            FROM m_workspace_user_messages_view
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuid),
        )
        assert cursor.fetchone() == (True,)


def test_provider_account_read_and_delete_follow_account_project_lock_order(
    api,
    db,
    monkeypatch,
):
    account_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb, FALSE, 'disconnected')
            """,
            (account_uuid, api.user_uuid),
        )

    thread_context = threading.local()
    monkeypatch.setattr(
        sql_canonical_store.contexts,
        "Context",
        lambda: types.SimpleNamespace(
            get_session=lambda: thread_context.session,
        ),
    )
    account_row_locked = threading.Event()
    delete_lock_attempted = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    def provider_read():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            thread_context.session = session
            sql_canonical_store.SQLCanonicalMessengerStore._lock_provider_accounts(
                None,
                (account_uuid,),
            )
            account_row_locked.set()
            assert delete_lock_attempted.wait(timeout=3)
            read_state.lock_projects(session, (project_uuid,))
        return "read"

    def delete_account():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            delete_lock_attempted.set()
            read_state.lock_external_account_resources(session, (account_uuid,))
            read_state.lock_projects(session, (project_uuid,))
            session.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
                (account_uuid,),
            )
        return "deleted"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        read_future = executor.submit(provider_read)
        assert account_row_locked.wait(timeout=3)
        delete_future = executor.submit(delete_account)
        assert read_future.result(timeout=10) == "read"
        assert delete_future.result(timeout=10) == "deleted"

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_capability_refresh_claim_and_account_delete_follow_lock_order(api, db):
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.UUID(int=(1 << 128) - 1)
    claim_after_uuid = sys_uuid.UUID(int=(1 << 128) - 2)
    project_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb, TRUE, 'live')
            """,
            (account_uuid, api.user_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                        }
                    }
                ),
            ),
        )

    account_row_locked = threading.Event()
    delete_lock_attempted = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    def refresh_account():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            claimed_uuid = sql_state.claim_capability_refresh_account(
                session,
                after_uuid=claim_after_uuid,
            )
            assert claimed_uuid == account_uuid
            account_row_locked.set()
            assert delete_lock_attempted.wait(timeout=3)
            read_state.lock_projects(session, (project_uuid,))
        return "refreshed"

    def delete_account():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            delete_lock_attempted.set()
            read_state.lock_external_account_resources(session, (account_uuid,))
            read_state.lock_projects(session, (project_uuid,))
            session.execute(
                "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
                (account_uuid,),
            )
        return "deleted"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        refresh_future = executor.submit(refresh_account)
        assert account_row_locked.wait(timeout=3)
        delete_future = executor.submit(delete_account)
        assert refresh_future.result(timeout=10) == "refreshed"
        assert delete_future.result(timeout=10) == "deleted"


def test_external_account_delete_cleans_compact_state_across_projects(api, db):
    bridge_uuid, key_uuid, _private_key = _seed_zulip_bridge_target(db)
    account_uuid = sys_uuid.uuid4()
    reader_uuid = sys_uuid.uuid4()
    project_ids = [sys_uuid.UUID(api.project_id), sys_uuid.uuid4()]
    streams = []
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status
            ) VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live')
            """,
            (
                account_uuid,
                api.user_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.invalid",
                        "default_project_id": api.project_id,
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2 (
                uuid, external_account_uuid, key_version, envelope
            ) VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                json.dumps(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(bridge_uuid),
                            "credential_key_uuid": str(key_uuid),
                        }
                    }
                ),
            ),
        )

    for index, project_id in enumerate(project_ids):
        _set_workspace_read_mode(db, project_id, read_state.PROJECT_MODE_COMPACT)
        stream_uuid = sys_uuid.UUID(
            conftest.seed_user_stream(
                db,
                project_id,
                api.user_uuid,
                f"Account cleanup {index}",
            )
        )
        conftest.seed_user_stream_binding(
            db,
            project_id,
            stream_uuid,
            reader_uuid,
        )
        topic_uuid = sys_uuid.UUID(
            conftest.seed_stream_topic(
                db,
                project_id,
                stream_uuid,
                api.user_uuid,
                "general",
                is_default=True,
            )
        )
        message_uuid = sys_uuid.uuid4()
        _run_database_operation(
            lambda session, project_id=project_id, stream_uuid=stream_uuid, topic_uuid=topic_uuid, message_uuid=message_uuid: (
                messenger_dm_helpers.create_workspace_user_message(
                    uuid=message_uuid,
                    project_id=project_id,
                    user_uuid=sys_uuid.UUID(api.user_uuid),
                    stream_uuid=stream_uuid,
                    topic_uuid=topic_uuid,
                    payload=message_payloads.MarkdownPayload(
                        content=f"account cleanup {index}"
                    ),
                    session=session,
                    emit_events=False,
                )
            )
        )
        _run_database_operation(
            lambda session, project_id=project_id, message_uuid=message_uuid: (
                read_state.set_message_uuids_read(
                    session,
                    project_id,
                    reader_uuid,
                    (message_uuid,),
                    True,
                )
            )
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_streams
                SET source_name = 'zulip',
                    source = %s::jsonb,
                    external_account_uuid = %s,
                    provider_external_id = %s
                WHERE project_id = %s AND uuid = %s
                """,
                (
                    json.dumps(
                        {
                            "kind": "zulip",
                            "server_url": "https://zulip.example.invalid",
                            "source_scope": str(account_uuid),
                        }
                    ),
                    account_uuid,
                    f"channel:{index}",
                    project_id,
                    stream_uuid,
                ),
            )
        streams.append(stream_uuid)

    response = api.delete(
        f"{EXTERNAL_ACCOUNTS}{account_uuid}",
        permissions=EXTERNAL_ACCOUNT_DELETE,
    )
    assert response.status_code == 204, response.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_streams
            WHERE project_id = ANY(%s::uuid[]) AND uuid = ANY(%s::uuid[])
            """,
            (project_ids, streams),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_topic_message_stats_v1
            WHERE project_id = ANY(%s::uuid[])
            """,
            (project_ids,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
              AND read_bits <> B'0'::bit(4096)
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_read_up_to_plan_starts_from_small_unread_tail(api, db):
    reader_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, reader_uuid, f"user-{reader_uuid}")
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Large read history",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        reader_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    with db.cursor() as cursor:
        cursor.execute(
            "EXPLAIN (FORMAT JSON) "
            + messenger_dm_helpers._WORKSPACE_USER_STREAM_ACCESS_SQL,
            (api.project_id, str(stream_uuid), str(reader_uuid)),
        )
        access_cost_before_history = cursor.fetchone()[0][0]["Plan"]["Total Cost"]
    history_size = 100_000
    unread_size = 100
    uuid_seed = str(sys_uuid.uuid4())
    created_at = datetime.datetime(2026, 7, 29)
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            )
            SELECT md5(%s || ':' || series)::uuid, %s, %s, %s, %s,
                   '{"kind":"markdown","content":"history"}'::jsonb,
                   %s::timestamp + series * interval '1 microsecond',
                   %s::timestamp + series * interval '1 microsecond'
            FROM generate_series(1, %s) AS series
            """,
            (
                uuid_seed,
                api.project_id,
                str(stream_uuid),
                str(topic_uuid),
                api.user_uuid,
                created_at,
                created_at,
                history_size,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read
            )
            SELECT message.uuid, %s, message.project_id,
                   message.created_at < %s::timestamp
            FROM m_workspace_messages AS message
            WHERE message.project_id = %s
              AND message.stream_uuid = %s
            """,
            (
                str(reader_uuid),
                created_at
                + datetime.timedelta(
                    microseconds=history_size - unread_size + 1,
                ),
                api.project_id,
                str(stream_uuid),
            ),
        )
        cursor.execute(
            """
            SELECT uuid, created_at
            FROM m_workspace_messages
            WHERE project_id = %s AND stream_uuid = %s
            ORDER BY created_at DESC, uuid DESC
            LIMIT 1
            """,
            (api.project_id, str(stream_uuid)),
        )
        boundary_uuid, boundary_created_at = cursor.fetchone()
        cursor.execute("ANALYZE m_workspace_messages")
        cursor.execute("ANALYZE m_workspace_user_message_flags")
        cursor.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT *
            FROM m_workspace_user_streams
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, str(reader_uuid), str(stream_uuid)),
        )
        exact_stream_plan = cursor.fetchone()[0][0]["Plan"]
        cursor.execute(
            """
            EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
            SELECT *
            FROM m_workspace_user_streams
            WHERE project_id = %s AND user_uuid = %s
            """,
            (api.project_id, str(reader_uuid)),
        )
        stream_collection_plan = cursor.fetchone()[0][0]["Plan"]
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            + messenger_dm_helpers._WORKSPACE_USER_STREAM_ACCESS_SQL,
            (api.project_id, str(stream_uuid), str(reader_uuid)),
        )
        access_plan = cursor.fetchone()[0][0]["Plan"]
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            + messenger_dm_helpers._READ_TOPIC_MESSAGES_TO_BOUNDARY_SQL,
            (
                api.project_id,
                str(reader_uuid),
                str(stream_uuid),
                str(topic_uuid),
                boundary_created_at,
                str(boundary_uuid),
            ),
        )
        plan = cursor.fetchone()[0][0]["Plan"]

    def walk(node):
        yield node
        for child in node.get("Plans", []):
            yield from walk(child)

    nodes = list(walk(plan))
    assert all(node["Node Type"] != "Materialize" for node in nodes)
    assert any(
        node.get("Index Name") == "m_workspace_unread_flags_user_message_idx"
        for node in nodes
    )
    assert not any(
        node["Node Type"] == "Seq Scan"
        and node.get("Relation Name") == "m_workspace_messages"
        for node in nodes
    )
    assert plan["Actual Rows"] == unread_size

    exact_nodes = list(walk(exact_stream_plan))
    collection_nodes = list(walk(stream_collection_plan))
    for projection_plan, nodes in (
        (exact_stream_plan, exact_nodes),
        (stream_collection_plan, collection_nodes),
    ):
        assert any(
            node.get("Index Name") == "m_workspace_unread_flags_user_message_idx"
            for node in nodes
        )
        assert sum(node.get("Temp Read Blocks", 0) for node in nodes) == 0
        assert sum(node.get("Temp Written Blocks", 0) for node in nodes) == 0
        assert (
            sum(
                node.get("Actual Rows", 0) * node.get("Actual Loops", 0)
                for node in nodes
                if node.get("Relation Name") == "m_workspace_messages"
            )
            <= unread_size + 1
        )
        assert projection_plan["Actual Rows"] == 1

    access_nodes = list(walk(access_plan))
    assert access_cost_before_history > 0
    assert access_plan["Total Cost"] > 0
    assert access_plan["Actual Rows"] == 1
    assert not any(
        node.get("Relation Name") == "m_workspace_messages" for node in access_nodes
    )
    assert sum(node.get("Temp Read Blocks", 0) for node in access_nodes) == 0
    assert sum(node.get("Temp Written Blocks", 0) for node in access_nodes) == 0


def test_unbound_user_cannot_send_message(api, db):
    other_user = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, other_user, "private-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, other_user, "general", is_default=True
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "nope",
            },
        },
    )
    assert resp.status_code == 400, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_messages
            WHERE project_id = %s
              AND user_uuid = %s
              AND stream_uuid = %s
            """,
            (api.project_id, api.user_uuid, stream_uuid),
        )
        assert cur.fetchone()[0] == 0


def test_message_create_uses_stream_default_topic(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "default-topic-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuid = str(sys_uuid.uuid4())
    public_message_uuid = str(
        sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid.lower())
    )

    resp = api.post(
        MESSAGES,
        json={
            "uuid": message_uuid,
            "stream_uuid": stream_uuid,
            "payload": {
                "kind": "markdown",
                "content": "missing topic",
            },
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["topic_uuid"] == topic_uuid

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_messages
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        stored_topic_uuid = cur.fetchone()[0]
        cur.execute(
            """
            SELECT payload
            FROM m_workspace_visible_events
            WHERE project_id = %s
                AND payload->>'kind' = 'message.created'
                AND payload->>'uuid' = %s
            """,
            (api.project_id, public_message_uuid),
        )
        event_payload = cur.fetchone()[0]

    assert str(stored_topic_uuid) == topic_uuid
    assert event_payload["topic_uuid"] == topic_uuid


def test_message_create_without_topic_rejects_stream_without_default(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "no-default-topic-team"
    )

    resp = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "payload": {
                "kind": "markdown",
                "content": "missing default topic",
            },
        },
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == 400001007


def test_projection_helper_does_not_bypass_canonical_event_journal(
    api, workspace_api, db
):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "helper-events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    message_uuid = sys_uuid.uuid4()
    public_message_uuid = sys_uuid.uuid5(
        sys_uuid.UUID(topic_uuid), str(message_uuid).lower()
    )
    _run_database_operation(
        lambda session: messenger_dm_helpers.create_workspace_user_message(
            uuid=message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(stream_uuid),
            topic_uuid=sys_uuid.UUID(topic_uuid),
            payload=message_payloads.MarkdownPayload(content="created through model"),
            session=session,
        )
    )

    resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 1
    assert events[0]["object_type"] == "message"
    assert events[0]["action"] == "created"
    assert events[0]["payload"]["kind"] == "message.created"
    assert events[0]["payload"]["uuid"] == str(public_message_uuid)


def test_zulip_message_flag_sync_can_keep_author_unread(api, db):
    server_url = "https://zulip.example.test"
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "zulip-own-message"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    owner_external_account_uuid = sys_uuid.uuid4()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready)
            VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE)
            """,
            (
                str(owner_external_account_uuid),
                str(api.user_uuid),
                f'{{"kind":"zulip","server_url":"{server_url}"}}',
            ),
        )
        cur.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected,
                 project_id)
            VALUES (%s, %s, %s, 'zulip', %s, '{}'::jsonb,
                    'Zulip test', TRUE, %s)
            """,
            (
                str(sys_uuid.uuid4()),
                str(owner_external_account_uuid),
                str(api.user_uuid),
                f"chat-{api.user_uuid}",
                api.project_id,
            ),
        )
    message_uuid = sys_uuid.uuid4()

    def create_and_sync_flags(session):
        message = messenger_dm_helpers.create_workspace_user_message(
            uuid=message_uuid,
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            stream_uuid=sys_uuid.UUID(stream_uuid),
            topic_uuid=sys_uuid.UUID(topic_uuid),
            payload=message_payloads.MarkdownPayload(content="sent through Zulip"),
            source_name=messenger_models.SourceName.ZULIP.value,
            source=messenger_models.ZulipSource(
                stream_id=42,
                server_url=server_url,
                source_scope=str(owner_external_account_uuid),
                topic_name="general",
                message_id=123,
            ),
            session=session,
        )
        assert message.read is True
        message = messenger_dm_helpers.sync_workspace_user_message_flags(
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=sys_uuid.UUID(api.user_uuid),
            message_uuid=message_uuid,
            values={"read": False},
            session=session,
            allow_author_unread=True,
        )
        assert message.read is False
        return message

    owner_message = _run_database_operation(create_and_sync_flags)
    assert owner_message.read is False

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT user_uuid, read
            FROM m_workspace_user_messages_view
            WHERE uuid = %s AND project_id = %s
            ORDER BY user_uuid
            """,
            (message_uuid, api.project_id),
        )
        flags = {str(row[0]): row[1] for row in cur.fetchall()}

    assert flags == {
        str(api.user_uuid): False,
    }


def test_events_filter_by_epoch_range(api, workspace_api, db):
    workspace_api.user_uuid = api.user_uuid
    workspace_api.project_id = api.project_id
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "range-events-team"
    )
    topic_uuid = conftest.seed_stream_topic(
        db, api.project_id, stream_uuid, api.user_uuid, "general", is_default=True
    )
    message_uuids = []
    for content in ("first through API", "second through API"):
        create_resp = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": content,
                },
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        message_uuids.append(create_resp.json()["uuid"])
    public_message_uuids = [
        str(sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), value.lower()))
        for value in message_uuids
    ]

    resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert [event["payload"]["uuid"] for event in events] == public_message_uuids
    first_epoch = events[0]["epoch_version"]
    second_epoch = events[1]["epoch_version"]
    epoch_generation = workspace_api.get(EPOCH).json()["epoch_generation"]

    after_resp = workspace_api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=>", first_epoch),
            ("epoch_generation", epoch_generation),
        ],
    )
    assert after_resp.status_code == 200, after_resp.text
    assert [event["epoch_version"] for event in after_resp.json()] == [
        first_epoch,
        second_epoch,
    ]

    strict_after_resp = workspace_api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version>", first_epoch),
            ("epoch_generation", epoch_generation),
        ],
    )
    assert strict_after_resp.status_code == 200, strict_after_resp.text
    assert [event["epoch_version"] for event in strict_after_resp.json()] == [
        second_epoch
    ]

    before_resp = workspace_api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=<", first_epoch),
            ("epoch_generation", epoch_generation),
        ],
    )
    assert before_resp.status_code == 200, before_resp.text
    assert [event["epoch_version"] for event in before_resp.json()] == [first_epoch]

    strict_before_resp = workspace_api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version<", second_epoch),
            ("epoch_generation", epoch_generation),
        ],
    )
    assert strict_before_resp.status_code == 200, strict_before_resp.text
    assert [event["epoch_version"] for event in strict_before_resp.json()] == [
        first_epoch
    ]

    exact_resp = workspace_api.get(
        EVENTS,
        params=[
            ("page_limit", 100),
            ("epoch_version=>", second_epoch),
            ("epoch_version=<", second_epoch),
            ("epoch_generation", epoch_generation),
        ],
    )
    assert exact_resp.status_code == 200, exact_resp.text
    assert [event["epoch_version"] for event in exact_resp.json()] == [second_epoch]


def test_old_writer_stream_registers_project_for_gated_compaction(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Rolling upgrade read state"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuid = sys_uuid.uuid4()
    rolling_stream_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (api.project_id,),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_streams (
                uuid, name, description, source_name, source,
                user_uuid, project_id
            ) VALUES (
                %s, 'Rolling old writer', '', 'native',
                '{"kind":"native"}'::jsonb, %s, %s
            )
            """,
            (rolling_stream_uuid, api.user_uuid, api.project_id),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"legacy"}'::jsonb,
                NOW(), NOW()
            )
            """,
            (
                message_uuid,
                api.project_id,
                stream_uuid,
                topic_uuid,
                api.user_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, created_at, updated_at
            ) VALUES
                (%s, %s, %s, TRUE, NOW(), NOW()),
                (%s, %s, %s, FALSE, NOW(), NOW())
            """,
            (
                message_uuid,
                api.user_uuid,
                api.project_id,
                message_uuid,
                reader_uuid,
                api.project_id,
            ),
        )
    db.commit()

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == read_state.PROJECT_MODE_LEGACY

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id
            FROM m_workspace_read_state_projects_v1
            WHERE project_id <> %s
            """,
            (api.project_id,),
        )
        unrelated_project_ids = {row[0] for row in cursor.fetchall()}
    maintenance = _run_database_operation(
        lambda session: read_state.maintain_next_project(
            session,
            batch_size=10,
            excluded_project_ids=unrelated_project_ids,
        )
    )
    assert maintenance is not None
    assert maintenance[0] == "prepare"
    assert maintenance[1] == sys_uuid.UUID(str(api.project_id))
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == read_state.PROJECT_MODE_PREPARING

    _finish_read_compaction(db, api.project_id)
    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): False}
    assert _workspace_message_read_states(
        db,
        api.project_id,
        api.user_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}


def test_read_state_maintenance_candidate_plan_is_index_bounded(api, db):
    del api
    seed = str(sys_uuid.uuid4())
    with db.cursor() as cursor:
        cursor.execute("BEGIN")
        try:
            cursor.execute(
                """
                INSERT INTO m_workspace_read_state_projects_v1 (
                    project_id, mode, created_at, updated_at
                )
                SELECT md5(%s || sample::text)::uuid, 'legacy', NOW(),
                       NOW() - sample * interval '1 second'
                FROM generate_series(1, 20000) AS sample
                ON CONFLICT (project_id) DO NOTHING
                """,
                (seed,),
            )
            cursor.execute("ANALYZE m_workspace_read_state_projects_v1")
            cursor.execute(
                """
                EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF, FORMAT JSON)
                SELECT state.project_id, state.mode, state.updated_at
                FROM m_workspace_read_state_projects_v1 AS state
                WHERE state.mode IN ('legacy', 'preparing', 'dual')
                ORDER BY state.updated_at, state.project_id
                LIMIT %s
                """,
                (read_state.MAINTENANCE_CANDIDATE_LIMIT,),
            )
            plan = cursor.fetchone()[0][0]["Plan"]
        finally:
            cursor.execute("ROLLBACK")

    def walk(node):
        yield node
        for child in node.get("Plans", []):
            yield from walk(child)

    nodes = list(walk(plan))
    assert any(
        node.get("Index Name") == "m_workspace_read_state_active_maintenance_idx"
        for node in nodes
    )
    assert max(int(node.get("Actual Rows", 0)) for node in nodes) <= (
        read_state.MAINTENANCE_CANDIDATE_LIMIT
    )


def test_read_state_maintenance_skips_a_locked_project(api, db):
    del api
    locked_project_id = sys_uuid.UUID("00000000-0000-0000-0000-000000000011")
    available_project_id = sys_uuid.UUID("00000000-0000-0000-0000-000000000012")
    project_ids = [locked_project_id, available_project_id]
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_read_state_compaction_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            ) VALUES
                (%s, 'legacy', '2000-01-01 UTC', '2000-01-01 UTC'),
                (%s, 'legacy', '2000-01-02 UTC', '2000-01-02 UTC')
            """,
            (locked_project_id, available_project_id),
        )
    db.commit()

    try:
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_lock(hashtextextended(%s::text, 0))
                """,
                (locked_project_id,),
            )
        result = _run_database_operation(
            lambda session: read_state.maintain_next_project(session)
        )
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))
                """,
                (locked_project_id,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_compaction_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_projects_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
        db.commit()

    assert result == ("prepare", available_project_id, 0)


def test_read_state_maintenance_scans_past_full_locked_candidate_page(api, db):
    del api
    project_ids = [
        sys_uuid.UUID(f"00000000-0000-0000-0001-{index:012x}")
        for index in range(read_state.MAINTENANCE_CANDIDATE_LIMIT + 1)
    ]
    locked_project_ids = project_ids[:-1]
    available_project_id = project_ids[-1]
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_read_state_compaction_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.executemany(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            ) VALUES (%s, 'legacy', '1900-01-01 UTC', '1900-01-01 UTC')
            """,
            ((project_id,) for project_id in project_ids),
        )
    db.commit()

    try:
        with db.cursor() as cursor:
            for project_id in locked_project_ids:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s::text, 0))",
                    (project_id,),
                )
        result = _run_database_operation(
            lambda session: read_state.maintain_next_project(session)
        )
    finally:
        with db.cursor() as cursor:
            for project_id in locked_project_ids:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                    (project_id,),
                )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_compaction_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_projects_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
        db.commit()

    assert result == ("prepare", available_project_id, 0)


def test_read_state_cleanup_scans_past_full_locked_candidate_page(api, db):
    project_ids = [
        sys_uuid.UUID(f"00000000-0000-0000-0002-{index:012x}")
        for index in range(read_state.MAINTENANCE_CANDIDATE_LIMIT + 1)
    ]
    flag_uuids = [sys_uuid.uuid4() for _project_id in project_ids]
    locked_project_ids = project_ids[:-1]
    available_project_id = project_ids[-1]
    stream_uuids = []
    topic_uuids = []
    for index, project_id in enumerate(project_ids):
        stream_uuid = conftest.seed_user_stream(
            db,
            project_id,
            api.user_uuid,
            f"Cleanup candidate {index}",
        )
        stream_uuids.append(stream_uuid)
        topic_uuids.append(
            conftest.seed_stream_topic(
                db,
                project_id,
                stream_uuid,
                api.user_uuid,
                "general",
                is_default=True,
            )
        )
    with db.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"cleanup"}'::jsonb,
                NOW(), NOW()
            )
            """,
            (
                (
                    flag_uuid,
                    project_id,
                    stream_uuid,
                    topic_uuid,
                    api.user_uuid,
                )
                for flag_uuid, project_id, stream_uuid, topic_uuid in zip(
                    flag_uuids,
                    project_ids,
                    stream_uuids,
                    topic_uuids,
                )
            ),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.executemany(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            ) VALUES (%s, 'compact', '1900-01-01 UTC', '1900-01-01 UTC')
            """,
            ((project_id,) for project_id in project_ids),
        )
        cursor.executemany(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read, pinned, starred,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, FALSE, FALSE, FALSE, NOW(), NOW()
            )
            """,
            (
                (flag_uuid, api.user_uuid, project_id)
                for flag_uuid, project_id in zip(flag_uuids, project_ids)
            ),
        )
        cursor.execute(
            """
            SELECT project_id
            FROM m_workspace_read_state_projects_v1
            WHERE NOT (project_id = ANY(%s::uuid[]))
            """,
            (project_ids,),
        )
        unrelated_project_ids = [row[0] for row in cursor.fetchall()]
    db.commit()

    try:
        with db.cursor() as cursor:
            for project_id in locked_project_ids:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s::text, 0))",
                    (project_id,),
                )
        result = _run_database_operation(
            lambda session: read_state.maintain_next_project(
                session,
                cleanup_enabled=True,
                excluded_project_ids=unrelated_project_ids,
            )
        )
    finally:
        with db.cursor() as cursor:
            for project_id in locked_project_ids:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s::text, 0))",
                    (project_id,),
                )
            cursor.execute(
                "DELETE FROM m_workspace_user_message_flags "
                "WHERE uuid = ANY(%s::uuid[])",
                (flag_uuids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_streams WHERE uuid = ANY(%s::uuid[])",
                (stream_uuids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_projects_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_project_ingest_ranges_v2 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
        db.commit()

    assert result == ("cleanup", available_project_id, 1)


def test_read_state_maintenance_rotates_after_each_bounded_batch(api, db):
    del api
    first_project_id = sys_uuid.UUID("00000000-0000-0000-0000-000000000021")
    second_project_id = sys_uuid.UUID("00000000-0000-0000-0000-000000000022")
    project_ids = [first_project_id, second_project_id]
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_read_state_compaction_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 "
            "WHERE project_id = ANY(%s::uuid[])",
            (project_ids,),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_projects_v1 (
                project_id, mode, created_at, updated_at
            ) VALUES
                (%s, 'preparing', '2000-01-01 UTC', '2000-01-01 UTC'),
                (%s, 'preparing', '2000-01-01 UTC', '2000-01-01 UTC')
            """,
            (first_project_id, second_project_id),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_compaction_v1 (
                project_id, phase, created_at, updated_at
            ) VALUES
                (%s, 'sequences', '2000-01-01 UTC', '2000-01-01 UTC'),
                (%s, 'sequences', '2000-01-01 UTC', '2000-01-01 UTC')
            """,
            (first_project_id, second_project_id),
        )
    db.commit()

    try:
        first = _run_database_operation(
            lambda session: read_state.maintain_next_project(session)
        )
        second = _run_database_operation(
            lambda session: read_state.maintain_next_project(session)
        )
    finally:
        with db.cursor() as cursor:
            cursor.execute(
                "DELETE FROM m_workspace_read_state_compaction_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
            cursor.execute(
                "DELETE FROM m_workspace_read_state_projects_v1 "
                "WHERE project_id = ANY(%s::uuid[])",
                (project_ids,),
            )
        db.commit()

    assert first == ("prepare", first_project_id, 0)
    assert second == ("prepare", second_project_id, 0)


def test_compact_read_state_cutover_is_resumable_exact_and_sparse(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact cutover"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in (
        "first",
        f"mention [reader](urn:user:{reader_uuid})",
        "third",
        f"raw urn:user:{reader_uuid} is not a mention",
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    response = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/star/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    response = api.post(
        f"{MESSAGES}{message_uuids[2]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    expected_messages = api.get(
        MESSAGES,
        user=reader_uuid,
        params={"stream_uuid": stream_uuid},
    ).json()

    observed_phases = set()
    for _iteration in range(50):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT phase
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            progress = cursor.fetchone()
            if progress is not None:
                observed_phases.add(progress[0])
            cursor.execute(
                """
                SELECT mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            if cursor.fetchone()[0] == read_state.PROJECT_MODE_COMPACT:
                break
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
    else:
        pytest.fail("Compact read-state cutover did not complete")

    assert observed_phases == {
        "sequences",
        "memberships",
        "flags",
        "legacy_gaps",
        "stats",
        "mentions",
        "verify",
        "verify_chunks",
        "verify_read_stats",
        "verify_stats",
        "verify_mentions",
    }
    compact_messages = api.get(
        MESSAGES,
        user=reader_uuid,
        params={"stream_uuid": stream_uuid},
    )
    assert compact_messages.status_code == 200, compact_messages.text
    assert compact_messages.json() == expected_messages
    assert (
        next(
            item for item in compact_messages.json() if item["uuid"] == message_uuids[1]
        )["mentioned"]
        is True
    )
    assert (
        next(
            item for item in compact_messages.json() if item["uuid"] == message_uuids[3]
        )["mentioned"]
        is False
    )

    while _run_database_operation(
        lambda session: read_state.cleanup_legacy_flags(
            session,
            api.project_id,
            batch_size=2,
        )
    ):
        pass
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, user_uuid, pinned, starred
            FROM m_workspace_user_message_flags
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        sparse_flags = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        chunk_count = cursor.fetchone()[0]
    assert sparse_flags == [(sys_uuid.UUID(message_uuids[1]), reader_uuid, False, True)]
    assert chunk_count <= 2
    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
        == 0
    )


def test_compact_cutover_preserves_missing_legacy_flags_as_read(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact legacy gaps"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in (
        "explicit unread one",
        "explicit unread two",
        "implicit legacy read without compact bit",
        "implicit legacy read with compact bit",
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids[2:]),
        )
        cursor.execute(
            """
            SELECT unread_count
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone()[0] == 2
    db.commit()

    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    for _iteration in range(20):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT phase
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            if cursor.fetchone()[0] == "legacy_gaps":
                break
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=100,
            )
        )
    else:
        pytest.fail("Compaction did not reach the legacy gap phase")

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids[2:]),
        )
        gap_sequences = {str(row[0]): row[1] for row in cursor.fetchall()}
        preexisting_read_sequence = gap_sequences[message_uuids[3]]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_messages AS message
            JOIN m_workspace_stream_bindings AS binding
              ON binding.project_id = message.project_id
             AND binding.stream_uuid = message.stream_uuid
            WHERE message.project_id = %s
              AND message.uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids[:2]),
        )
        no_gap_page_size = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, stream_uuid),
        )
        final_page_size = cursor.fetchone()[0] * 2

    _run_database_operation(
        lambda session: read_state._apply_coordinate_rows(
            session,
            api.project_id,
            [
                {
                    "uuid": message_uuids[3],
                    "user_uuid": reader_uuid,
                    "read": True,
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "ingest_sequence": preexisting_read_sequence,
                }
            ],
        )
    )

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=no_gap_page_size,
            )
        )
        == no_gap_page_size
    )
    with db.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COALESCE(
                get_bit(
                    chunk.read_bits,
                    (message.ingest_sequence %% {read_state.READ_CHUNK_BITS})::integer
                ),
                0
            )
            FROM m_workspace_messages AS message
            LEFT JOIN m_workspace_user_read_chunks_v1 AS chunk
              ON chunk.user_uuid = %s
             AND chunk.chunk_number =
                    message.ingest_sequence / {read_state.READ_CHUNK_BITS}
            WHERE message.project_id = %s
              AND message.uuid = ANY(%s::uuid[])
            ORDER BY message.ingest_sequence
            """,
            (reader_uuid, api.project_id, message_uuids[2:]),
        )
        assert cursor.fetchall() == [(0,), (1,)]
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        read_count_before_gap_repair = cursor.fetchone()[0]

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=1,
            )
        )
        == 1
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT last_message_uuid, last_user_uuid,
                   last_ingest_sequence, processed_rows
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        partial_cursor = cursor.fetchone()
        cursor.execute(
            """
            UPDATE m_workspace_read_state_compaction_v1
            SET last_message_uuid = %s,
                last_user_uuid = %s,
                last_ingest_sequence = %s,
                processed_rows = processed_rows + 1,
                updated_at = NOW()
            WHERE project_id = %s
            """,
            (
                message_uuids[3],
                reader_uuid,
                gap_sequences[message_uuids[3]],
                api.project_id,
            ),
        )
        cursor.execute(
            """
            SELECT last_message_uuid, last_user_uuid,
                   last_ingest_sequence, processed_rows
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == partial_cursor
    db.commit()

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=no_gap_page_size,
            )
        )
        == final_page_size - 1
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (api.project_id, reader_uuid, message_uuids[2:]),
        )
        assert cursor.fetchall() == [(True,), (True,)]
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone()[0] == read_count_before_gap_repair + 1

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=no_gap_page_size,
            )
        )
        == 0
    )
    _finish_read_compaction(db, api.project_id)

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT unread_count
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone()[0] == 2
    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: False,
        message_uuids[1]: False,
        message_uuids[2]: True,
        message_uuids[3]: True,
    }


def test_legacy_gap_scan_bounds_and_advances_recipient_empty_messages(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Recipient-empty legacy gap page",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuids = []
    for index in range(5):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": f"recipient-empty {index}",
                },
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, stream_uuid),
        )
        cursor.execute(
            """
            DELETE FROM m_workspace_read_memberships_v1
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (api.project_id, stream_uuid),
        )
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            ORDER BY ingest_sequence
            """,
            (api.project_id, message_uuids),
        )
        sequences = [row[0] for row in cursor.fetchall()]
    db.commit()

    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_DUAL)
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_state_compaction_v1 (
                project_id, phase, target_ingest_sequence,
                legacy_gap_repair_kind, completed_at,
                created_at, updated_at
            ) VALUES (
                %s, 'legacy_gaps', %s, 'full_pending', NULL, NOW(), NOW()
            )
            ON CONFLICT (project_id) DO UPDATE
            SET phase = 'legacy_gaps',
                last_message_uuid = NULL,
                last_user_uuid = NULL,
                last_ingest_sequence = 0,
                target_ingest_sequence = EXCLUDED.target_ingest_sequence,
                processed_rows = 0,
                legacy_gap_repair_kind = 'full_pending',
                completed_at = NULL,
                updated_at = NOW()
            """,
            (api.project_id, sequences[-1]),
        )
    db.commit()

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
        == 2
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT phase, last_user_uuid, last_ingest_sequence, processed_rows
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == ("legacy_gaps", None, sequences[1], 2)

    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
        == 2
    )
    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
        == 1
    )
    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=2,
            )
        )
        == 0
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT phase, legacy_gap_repair_kind, processed_rows
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == ("stats", "full_done", 5)


def test_compact_read_state_parity_failure_blocks_cutover(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact parity gate"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "verify me"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )

    for _iteration in range(20):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT phase
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            if cursor.fetchone()[0] == "verify":
                break
    else:
        pytest.fail("Compaction did not reach parity verification")

    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (api.user_uuid,),
        )
    db.commit()
    with pytest.raises(RuntimeError, match="parity check failed"):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == read_state.PROJECT_MODE_DUAL

    def repair_compact_state(session):
        read_state.set_message_read(
            session,
            api.project_id,
            api.user_uuid,
            message_uuid,
            True,
        )
        read_state._refresh_topic_read_stats(
            session,
            api.project_id,
            [(api.user_uuid, topic_uuid)],
        )

    _run_database_operation(repair_compact_state)
    for _iteration in range(20):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                """,
                (api.project_id,),
            )
            if cursor.fetchone()[0] == read_state.PROJECT_MODE_COMPACT:
                break
    else:
        pytest.fail("Compaction did not resume after parity repair")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == read_state.PROJECT_MODE_COMPACT


def test_rollback_mode_reads_compact_state_and_dual_writes_legacy_flags(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Rollback dual writes"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for content in ("first", "second", "third"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids),
        )
        cursor.execute(
            """
            UPDATE m_workspace_read_state_projects_v1
            SET mode = 'rollback', updated_at = NOW()
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
    db.commit()

    assert _workspace_message_read_states(
        db, api.project_id, reader_uuid, message_uuids
    ) == {
        message_uuids[0]: True,
        message_uuids[1]: False,
        message_uuids[2]: False,
    }
    response = api.post(
        f"{STREAMS}{stream_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid::text, read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (api.project_id, reader_uuid, message_uuids),
        )
        assert dict(cursor.fetchall()) == {
            message_uuid: True for message_uuid in message_uuids
        }

    _run_database_operation(
        lambda session: messenger_dm_helpers.sync_workspace_user_message_flags(
            project_id=api.project_id,
            user_uuid=reader_uuid,
            message_uuid=message_uuids[-1],
            values={"read": False},
            session=session,
            allow_author_unread=True,
            emit_events=False,
        )
    )
    assert _workspace_message_read_states(
        db, api.project_id, reader_uuid, [message_uuids[-1]]
    ) == {message_uuids[-1]: False}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuids[-1]),
        )
        assert cursor.fetchone() == (False,)

    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "during rollback"},
        },
    )
    assert response.status_code == 201, response.text
    new_message_uuid = response.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, new_message_uuid),
        )
        assert dict(cursor.fetchall()) == {
            sys_uuid.UUID(str(api.user_uuid)): True,
            reader_uuid: False,
        }
    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        [new_message_uuid],
    ) == {new_message_uuid: False}


def test_compact_bulk_read_rechecks_rollback_mode_under_project_lock(
    api, db, monkeypatch
):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Rollback transition race"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for content in ("first", "second"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])

    original_lock_projects = read_state.lock_projects
    transitioned = False

    def transition_before_compact_write(session, project_ids):
        nonlocal transitioned
        original_lock_projects(session, project_ids)
        if not transitioned:
            session.execute(
                """
                UPDATE m_workspace_read_state_projects_v1
                SET mode = 'rollback', updated_at = NOW()
                WHERE project_id = %s AND mode = 'compact'
                """,
                (api.project_id,),
            )
            transitioned = True

    monkeypatch.setattr(read_state, "lock_projects", transition_before_compact_write)
    _run_database_operation(
        lambda session: read_state.read_stream(
            session,
            api.project_id,
            reader_uuid,
            stream_uuid,
            collect_message_rows=False,
        )
    )

    assert transitioned is True
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid::text, read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            ORDER BY uuid
            """,
            (api.project_id, reader_uuid, message_uuids),
        )
        assert dict(cursor.fetchall()) == {
            message_uuid: True for message_uuid in message_uuids
        }


@pytest.mark.parametrize("scope", ["stream", "topic", "boundary"])
def test_bulk_read_rechecks_compact_mode_after_dual_cleanup(api, db, scope):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, f"Dual cleanup {scope}"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "dual cleanup race"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_DUAL)

    mode_snapshot_read = threading.Event()
    cleanup_finished = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class PausedDualSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, values=None):
            result = self._session.execute(statement, values)
            if (
                not self._paused
                and "SELECT mode" in statement
                and "m_workspace_read_state_projects_v1" in statement
            ):
                self._paused = True
                mode_snapshot_read.set()
                assert cleanup_finished.wait(timeout=5)
            return result

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_bulk_read():
        with session_factory() as session:
            coordinated = PausedDualSession(session)
            if scope == "stream":
                return messenger_dm_helpers.read_workspace_user_stream_messages(
                    api.project_id,
                    reader_uuid,
                    stream_uuid,
                    session=coordinated,
                    collect_message_uuids=False,
                )
            if scope == "topic":
                return messenger_dm_helpers.read_workspace_user_stream_topic_messages(
                    api.project_id,
                    reader_uuid,
                    topic_uuid,
                    session=coordinated,
                    collect_message_uuids=False,
                )
            return messenger_dm_helpers.read_workspace_user_topic_messages_to_message(
                api.project_id,
                reader_uuid,
                message_uuid,
                session=coordinated,
                collect_message_uuids=False,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        read_future = executor.submit(run_bulk_read)
        assert mode_snapshot_read.wait(timeout=5)

        def compact_and_cleanup(session):
            read_state.lock_projects(session, (api.project_id,))
            session.execute(
                """
                INSERT INTO m_workspace_read_state_compaction_v1 (
                    project_id, phase, target_ingest_sequence,
                    legacy_gap_repair_kind, completed_at,
                    created_at, updated_at
                ) VALUES (
                    %s, 'verify_mentions', 0, 'full_done', NULL, NOW(), NOW()
                )
                ON CONFLICT (project_id) DO UPDATE
                SET phase = 'verify_mentions',
                    legacy_gap_repair_kind = 'full_done',
                    completed_at = NULL,
                    updated_at = NOW()
                """,
                (api.project_id,),
            )
            read_state._complete_compaction(session, api.project_id)
            session.execute(
                """
                DELETE FROM m_workspace_user_message_flags
                WHERE project_id = %s AND pinned = FALSE AND starred = FALSE
                """,
                (api.project_id,),
            )

        _run_database_operation(compact_and_cleanup)
        cleanup_finished.set()
        read_future.result(timeout=8)

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        [message_uuid],
    ) == {message_uuid: True}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuid),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 1


def test_compact_and_rollback_bulk_reads_have_no_lock_order_cycle(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Rollback lock ordering"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "lock ordering"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    compact_waiting_for_project = threading.Event()
    release_compact = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class PausedCompactSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            if (
                not self._paused
                and isinstance(statement, str)
                and "pg_advisory_xact_lock(hashtextextended(%s::text" in statement
            ):
                self._paused = True
                compact_waiting_for_project.set()
                assert release_compact.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_compact_bulk():
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '5s'")
            return read_state.read_stream(
                PausedCompactSession(session),
                api.project_id,
                reader_uuid,
                stream_uuid,
                collect_message_rows=False,
            )

    def run_rollback_bulk():
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '2s'")
            return read_state.read_stream(
                session,
                api.project_id,
                reader_uuid,
                stream_uuid,
                collect_message_rows=False,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        compact_future = executor.submit(run_compact_bulk)
        assert compact_waiting_for_project.wait(timeout=5)
        _run_database_operation(
            lambda session: (
                read_state.lock_projects(session, (api.project_id,)),
                session.execute(
                    """
                    UPDATE m_workspace_read_state_projects_v1
                    SET mode = 'rollback', updated_at = NOW()
                    WHERE project_id = %s AND mode = 'compact'
                    """,
                    (api.project_id,),
                ),
            )
        )
        rollback_future = executor.submit(run_rollback_bulk)
        rollback_result = rollback_future.result(timeout=3)
        release_compact.set()
        compact_result = compact_future.result(timeout=5)

    assert rollback_result.changed is True
    # The rollback transaction linearizes first. The earlier compact snapshot
    # is rebuilt under the project lock and correctly becomes a no-op.
    assert compact_result.changed is False
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuid),
        )
        assert cursor.fetchone() == (True,)


def _advance_read_compaction_to_phase(db, project_id, phase):
    for _iteration in range(50):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT phase
                FROM m_workspace_read_state_compaction_v1
                WHERE project_id = %s
                """,
                (project_id,),
            )
            progress = cursor.fetchone()
        if progress is not None and progress[0] == phase:
            return
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                project_id,
                batch_size=10,
            )
        )
    pytest.fail(f"Compaction did not reach {phase}")


def test_readd_mode_snapshot_is_serialized_with_preparing_cutover(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Serialized preparing re-add"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in ("old unread", "old read"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "detached gap"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuids.append(response.json()["uuid"])

    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "memberships")
    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=100,
            )
        )
        > 0
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)

    mode_snapshot_read = threading.Event()
    compactor_has_project_lock = threading.Event()
    compaction_finished = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class CoordinatedReaddSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def execute(self, statement, values=None):
            result = self.session.execute(statement, values)
            if (
                "SELECT mode" in statement
                and "m_workspace_read_state_projects_v1" in statement
            ):
                mode_snapshot_read.set()
                if compactor_has_project_lock.wait(timeout=0.25):
                    assert compaction_finished.wait(timeout=5)
            return result

    class CoordinatedCompactionSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def execute(self, statement, values=None):
            result = self.session.execute(statement, values)
            if (
                "pg_advisory_xact_lock(hashtextextended(%s::text, 0))" in statement
                and values == (api.project_id,)
            ):
                compactor_has_project_lock.set()
            return result

    def readd():
        with session_factory() as session:
            coordinated = CoordinatedReaddSession(session)
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                api.project_id,
                stream_uuid,
                reader_uuid,
                session=coordinated,
            )
        return "readded"

    def compact():
        assert mode_snapshot_read.wait(timeout=3)
        for _iteration in range(50):
            with session_factory() as session:
                coordinated = CoordinatedCompactionSession(session)
                read_state.lock_projects(coordinated, ())
                read_state.lock_projects(coordinated, (api.project_id,))
                read_state.compact_legacy_batch(
                    coordinated,
                    api.project_id,
                    batch_size=100,
                )
                mode = read_state.project_mode(coordinated, api.project_id)
            if mode == read_state.PROJECT_MODE_COMPACT:
                compaction_finished.set()
                return "compacted"
        pytest.fail("Concurrent compaction did not finish")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        readd_future = executor.submit(readd)
        compact_future = executor.submit(compact)
        assert readd_future.result(timeout=10) == "readded"
        assert compact_future.result(timeout=10) == "compacted"

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: False,
        message_uuids[1]: True,
        message_uuids[2]: True,
    }


def test_compact_only_read_bit_blocks_cutover_for_detached_legacy_user(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact reverse parity"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "must stay unread"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "verify_chunks")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
        ingest_sequence = cursor.fetchone()[0]
        chunk_number, bit_offset = divmod(ingest_sequence, read_state.READ_CHUNK_BITS)
        cursor.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits, created_at, updated_at
            ) VALUES (
                %s, %s, set_bit(B'0'::bit(4096), %s, 1), NOW(), NOW()
            )
            ON CONFLICT (user_uuid, chunk_number) DO UPDATE
            SET read_bits = set_bit(
                    m_workspace_user_read_chunks_v1.read_bits,
                    %s,
                    1
                ),
                updated_at = NOW()
            """,
            (reader_uuid, chunk_number, bit_offset, bit_offset),
        )
    db.commit()

    with pytest.raises(RuntimeError, match="read-state parity"):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
    _run_database_operation(
        lambda session: read_state.set_message_read(
            session,
            api.project_id,
            reader_uuid,
            message_uuid,
            False,
        )
    )
    _finish_read_compaction(db, api.project_id)


def test_compact_read_counter_corruption_blocks_cutover_and_can_resume(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact counter parity"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "count exactly once"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "verify_read_stats")
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_user_topic_read_stats_v1
            SET read_count = read_count + 1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
    db.commit()

    with pytest.raises(RuntimeError, match="read-counter parity"):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_user_topic_read_stats_v1
            SET read_count = read_count - 1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
    db.commit()
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
    db.commit()
    for _iteration in range(20):
        try:
            _run_database_operation(
                lambda session: read_state.compact_legacy_batch(
                    session,
                    api.project_id,
                    batch_size=10,
                )
            )
        except RuntimeError as exc:
            assert "read-counter parity" in str(exc)
            break
    else:
        pytest.fail("Missing compact read counter did not block cutover")
    _run_database_operation(
        lambda session: read_state._refresh_topic_read_stats(
            session,
            api.project_id,
            [(reader_uuid, topic_uuid)],
        )
    )
    _finish_read_compaction(db, api.project_id)


def test_live_read_after_verified_chunk_keeps_counter_verification_exact(api, db):
    reader_uuid = sys_uuid.UUID(int=1)
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Live compact counter verification"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "change while verifying"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "verify_chunks")
    assert (
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=1,
            )
        )
        == 1
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT progress.last_user_uuid, flags.read
            FROM m_workspace_read_state_compaction_v1 AS progress
            JOIN m_workspace_user_message_flags AS flags
              ON flags.project_id = progress.project_id
             AND flags.user_uuid = progress.last_user_uuid
             AND flags.uuid = %s
            WHERE progress.project_id = %s
              AND progress.phase = 'verify_chunks'
            """,
            (message_uuid, api.project_id),
        )
        verified_user_uuid, current_read = cursor.fetchone()
    assert verified_user_uuid == reader_uuid
    assert current_read is True

    _run_database_operation(
        lambda session: messenger_dm_helpers.sync_workspace_user_message_flags(
            project_id=api.project_id,
            user_uuid=verified_user_uuid,
            message_uuid=message_uuid,
            values={"read": False},
            session=session,
            allow_author_unread=True,
            emit_events=False,
        )
    )
    _finish_read_compaction(db, api.project_id)


def _finish_read_compaction(db, project_id):
    for _iteration in range(50):
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT mode
                FROM m_workspace_read_state_projects_v1
                WHERE project_id = %s
                """,
                (project_id,),
            )
            if cursor.fetchone()[0] == read_state.PROJECT_MODE_COMPACT:
                return
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                project_id,
                batch_size=10,
            )
        )
    pytest.fail("Compaction did not finish")


def test_compact_topic_stats_corruption_blocks_cutover_and_can_resume(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact stats parity"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "stats"},
        },
    )
    assert response.status_code == 201, response.text
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "verify_stats")

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_topic_message_stats_v1
            SET message_count = message_count + 1
            WHERE topic_uuid = %s
            """,
            (topic_uuid,),
        )
    db.commit()
    with pytest.raises(RuntimeError, match="topic-stats parity"):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_topic_message_stats_v1 AS stats
            SET message_count = canonical.message_count,
                last_ingest_sequence = canonical.last_ingest_sequence
            FROM (
                SELECT COUNT(*) AS message_count,
                       MAX(ingest_sequence) AS last_ingest_sequence
                FROM m_workspace_messages
                WHERE project_id = %s AND topic_uuid = %s
            ) AS canonical
            WHERE stats.topic_uuid = %s
            """,
            (api.project_id, topic_uuid, topic_uuid),
        )
    db.commit()
    _finish_read_compaction(db, api.project_id)


def test_compact_mention_corruption_blocks_cutover_and_can_resume(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact mention parity"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": f"hello [reader](urn:user:{reader_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _advance_read_compaction_to_phase(db, api.project_id, "verify_mentions")

    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_message_mentions_v1 WHERE message_uuid = %s",
            (message_uuid,),
        )
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE uuid = %s
            """,
            (message_uuid,),
        )
        ingest_sequence = cursor.fetchone()[0]
    db.commit()
    with pytest.raises(RuntimeError, match="mention parity"):
        _run_database_operation(
            lambda session: read_state.compact_legacy_batch(
                session,
                api.project_id,
                batch_size=10,
            )
        )
    _run_database_operation(
        lambda session: read_state.sync_message_mentions(
            session,
            api.project_id,
            message_uuid,
            stream_uuid,
            topic_uuid,
            ingest_sequence,
            [reader_uuid],
            f"hello [reader](urn:user:{reader_uuid})",
        )
    )
    _finish_read_compaction(db, api.project_id)


def test_detached_legacy_history_survives_cutover_and_reattach(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Detached compact history"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in ("old unread", "old read"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    _run_database_operation(
        lambda session: read_state.record_stream_detached(
            session,
            api.project_id,
            reader_uuid,
            stream_uuid,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuids[1]),
        )
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "detached gap"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuids.append(response.json()["uuid"])

    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    _finish_read_compaction(db, api.project_id)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid, ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids),
        )
        sequences = {str(row[0]): row[1] for row in cursor.fetchall()}
        cursor.execute(
            """
            SELECT read
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, message_uuids[1]),
        )
        assert cursor.fetchone() == (True,)
    assert (
        sequences[message_uuids[0]]
        < sequences[message_uuids[1]]
        < sequences[message_uuids[2]]
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    _run_database_operation(
        lambda session: (
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                api.project_id,
                stream_uuid,
                reader_uuid,
                session=session,
            )
        )
    )

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: False,
        message_uuids[1]: True,
        message_uuids[2]: True,
    }


def test_readd_during_preparing_preserves_legacy_unread_history(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Preparing re-add"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in ("old unread", "old read"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[1]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "detached gap"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuids.append(response.json()["uuid"])

    _run_database_operation(
        lambda session: read_state.begin_compaction(session, api.project_id)
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    _run_database_operation(
        lambda session: (
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                api.project_id,
                stream_uuid,
                reader_uuid,
                session=session,
            )
        )
    )
    _finish_read_compaction(db, api.project_id)

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: False,
        message_uuids[1]: True,
        message_uuids[2]: True,
    }


@pytest.mark.parametrize(
    ("source_mode", "destination_mode"),
    list(
        itertools.product(
            (
                read_state.PROJECT_MODE_LEGACY,
                read_state.PROJECT_MODE_PREPARING,
                read_state.PROJECT_MODE_DUAL,
                read_state.PROJECT_MODE_COMPACT,
                read_state.PROJECT_MODE_ROLLBACK,
            ),
            repeat=2,
        )
    ),
)
def test_stream_project_move_preserves_read_state_across_storage_modes(
    api, db, source_mode, destination_mode
):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Cross-project read state"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, source_mode)
    _set_workspace_read_mode(db, destination_project_id, destination_mode)
    message_uuids = []
    for content in (
        f"move mention ](urn:user:{reader_uuid})",
        "move two",
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    expected = _workspace_message_read_states(
        db, api.project_id, reader_uuid, message_uuids
    )

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            destination_project_id,
        )
    )
    assert (
        _workspace_message_read_states(
            db, destination_project_id, reader_uuid, message_uuids
        )
        == expected
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id, message_count, last_ingest_sequence
            FROM m_workspace_topic_message_stats_v1
            WHERE topic_uuid = %s
            """,
            (topic_uuid,),
        )
        stats_row = cursor.fetchone()
        destination_has_compact_state = destination_mode != (
            read_state.PROJECT_MODE_LEGACY
        )
        if destination_has_compact_state:
            assert stats_row[:2] == (destination_project_id, 2)
            assert stats_row[2] is not None
        else:
            assert stats_row is None
        cursor.execute(
            """
            SELECT message_uuid, project_id
            FROM m_workspace_message_mentions_v1
            WHERE user_uuid = %s AND message_uuid = ANY(%s::uuid[])
            """,
            (reader_uuid, message_uuids),
        )
        mention_row = cursor.fetchone()
        if destination_has_compact_state:
            assert mention_row == (
                sys_uuid.UUID(message_uuids[0]),
                destination_project_id,
            )
        else:
            assert mention_row is None
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        destination_chunks = cursor.fetchone()[0]
    if destination_has_compact_state:
        assert destination_chunks > 0
    else:
        assert destination_chunks == 0


def test_compact_stream_move_rebinds_pre_dense_coordinates_across_projects(api, db):
    reader_uuid = sys_uuid.uuid4()
    middle_project_id = sys_uuid.uuid4()
    final_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Pre-dense compact project move"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    for project_id in (api.project_id, middle_project_id, final_project_id):
        _set_workspace_read_mode(db, project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "pre-dense read bit"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    pre_dense_sequence = 123_456_789
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        source_sequence = cursor.fetchone()[0]
        source_chunk, source_offset = divmod(
            source_sequence,
            read_state.READ_CHUNK_BITS,
        )
        pre_dense_chunk, pre_dense_offset = divmod(
            pre_dense_sequence,
            read_state.READ_CHUNK_BITS,
        )
        cursor.execute(
            """
            UPDATE m_workspace_user_read_chunks_v1
            SET read_bits = set_bit(read_bits, %s, 0), updated_at = NOW()
            WHERE user_uuid = %s AND chunk_number = %s
            """,
            (source_offset, reader_uuid, source_chunk),
        )
        cursor.execute(
            """
            DELETE FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = %s
              AND read_bits = B'0'::bit(4096)
            """,
            (reader_uuid, source_chunk),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_read_chunks_v1 (
                user_uuid, chunk_number, read_bits, created_at, updated_at
            ) VALUES (
                %s, %s, set_bit(B'0'::bit(4096), %s, 1), NOW(), NOW()
            )
            ON CONFLICT (user_uuid, chunk_number) DO UPDATE
            SET read_bits = set_bit(
                    m_workspace_user_read_chunks_v1.read_bits,
                    %s,
                    1
                ),
                updated_at = NOW()
            """,
            (reader_uuid, pre_dense_chunk, pre_dense_offset, pre_dense_offset),
        )
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (pre_dense_sequence, api.project_id, message_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_topic_message_stats_v1
            SET last_ingest_sequence = %s, updated_at = NOW()
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (pre_dense_sequence, api.project_id, topic_uuid),
        )
    db.commit()

    def move(source_project_id, destination_project_id):
        _run_database_operation(
            lambda session: messenger_controllers._move_projection_rows(
                session,
                stream_uuid,
                source_project_id,
                destination_project_id,
            )
        )

    move(api.project_id, middle_project_id)
    assert _workspace_message_read_states(
        db,
        middle_project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        middle_sequence = cursor.fetchone()[0]
        middle_chunk = middle_sequence // read_state.READ_CHUNK_BITS
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = %s
            """,
            (reader_uuid, pre_dense_chunk),
        )
        assert cursor.fetchone() == (0,)

    move(middle_project_id, final_project_id)
    assert _workspace_message_read_states(
        db,
        final_project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message.ingest_sequence, range.range_number
            FROM m_workspace_messages AS message
            JOIN m_workspace_project_ingest_ranges_v2 AS range
              ON range.project_id = message.project_id
            WHERE message.project_id = %s AND message.uuid = %s
            """,
            (final_project_id, message_uuid),
        )
        final_sequence, final_range = cursor.fetchone()
        assert final_range * read_state.PROJECT_SEQUENCE_RANGE_SIZE < final_sequence
        assert final_sequence < (
            (final_range + 1) * read_state.PROJECT_SEQUENCE_RANGE_SIZE
        )
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = ANY(%s::bigint[])
            """,
            (reader_uuid, [pre_dense_chunk, middle_chunk]),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            SELECT bit_count(read_bits)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = %s
            """,
            (reader_uuid, final_sequence // read_state.READ_CHUNK_BITS),
        )
        assert cursor.fetchone() == (1,)


def test_compact_stream_move_into_preparing_survives_full_compaction(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Preparing compact project move"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "preparing destination"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        source_sequence = cursor.fetchone()[0]
    _run_database_operation(
        lambda session: read_state.begin_compaction(
            session,
            destination_project_id,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT mode
            FROM m_workspace_read_state_projects_v1
            WHERE project_id = %s
            """,
            (destination_project_id,),
        )
        assert cursor.fetchone() == (read_state.PROJECT_MODE_PREPARING,)

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            destination_project_id,
        )
    )
    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}
    _finish_read_compaction(db, destination_project_id)
    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        destination_sequence = cursor.fetchone()[0]
        assert destination_sequence != source_sequence
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = %s
            """,
            (reader_uuid, source_sequence // read_state.READ_CHUNK_BITS),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            SELECT bit_count(read_bits)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s AND chunk_number = %s
            """,
            (reader_uuid, destination_sequence // read_state.READ_CHUNK_BITS),
        )
        assert cursor.fetchone() == (1,)


def test_legacy_stream_move_into_dual_gap_cutover_preserves_missing_read(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Legacy missing read stream move",
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "implicit legacy read"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (api.project_id, message_uuid, reader_uuid),
        )
    db.commit()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT unread_count
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone() == (0,)

    _run_database_operation(
        lambda session: read_state.begin_compaction(
            session,
            destination_project_id,
        )
    )
    _advance_read_compaction_to_phase(
        db,
        destination_project_id,
        "legacy_gaps",
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT target_ingest_sequence
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (destination_project_id,),
        )
        cutover_target = cursor.fetchone()[0]

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            destination_project_id,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (destination_project_id, message_uuid),
        )
        assert cursor.fetchone()[0] > cutover_target

    _finish_read_compaction(db, destination_project_id)
    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        [message_uuid],
    ) == {message_uuid: True}


def test_readd_and_stream_project_move_do_not_deadlock(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Concurrent re-add and move",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        stream_uuid,
        reader_uuid,
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    _set_workspace_read_mode(
        db,
        destination_project_id,
        read_state.PROJECT_MODE_COMPACT,
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": "concurrent re-add and move",
            },
        },
    )
    assert response.status_code == 201, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_read_memberships_v1 (
                project_id, user_uuid, stream_uuid, last_detached_sequence,
                created_at, updated_at
            ) VALUES (%s, %s, %s, 0, NOW(), NOW())
            ON CONFLICT (project_id, user_uuid, stream_uuid) DO UPDATE
            SET last_detached_sequence = 0, updated_at = NOW()
            """,
            (api.project_id, reader_uuid, stream_uuid),
        )

    membership_locked = threading.Event()
    move_project_lock_attempted = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class ReaddSession:
        def __init__(self, session):
            self._session = session

        def execute(self, statement, params=()):
            result = self._session.execute(statement, params)
            if (
                "FROM m_workspace_read_memberships_v1" in statement
                and "FOR UPDATE" in statement
            ):
                membership_locked.set()
                assert move_project_lock_attempted.wait(timeout=3)
            return result

        def __getattr__(self, name):
            return getattr(self._session, name)

    class MoveSession:
        def __init__(self, session):
            self._session = session

        def execute(self, statement, params=()):
            if "pg_advisory_xact_lock(hashtextextended(%s::text, 0))" in statement:
                move_project_lock_attempted.set()
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def readd_member():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                api.project_id,
                stream_uuid,
                reader_uuid,
                session=ReaddSession(session),
            )
        return "readded"

    def move_stream():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '5s'")
            session.execute("SET LOCAL statement_timeout = '8s'")
            messenger_controllers._move_projection_rows(
                MoveSession(session),
                stream_uuid,
                api.project_id,
                destination_project_id,
            )
        return "moved"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        readd_future = executor.submit(readd_member)
        assert membership_locked.wait(timeout=3)
        move_future = executor.submit(move_stream)
        assert readd_future.result(timeout=10) == "readded"
        assert move_future.result(timeout=10) == "moved"

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id
            FROM m_workspace_read_memberships_v1
            WHERE user_uuid = %s AND stream_uuid = %s
            """,
            (reader_uuid, stream_uuid),
        )
        assert cursor.fetchone()[0] == destination_project_id


def test_compact_stream_move_through_legacy_discards_stale_shadow_state(api, db):
    reader_uuid = sys_uuid.uuid4()
    legacy_project_id = sys_uuid.uuid4()
    compact_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact legacy compact move"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    _set_workspace_read_mode(db, legacy_project_id, read_state.PROJECT_MODE_LEGACY)
    _set_workspace_read_mode(db, compact_project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": f"move mention ](urn:user:{reader_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            legacy_project_id,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (legacy_project_id, message_uuid, reader_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET payload = '{"kind":"markdown","content":"mention removed"}',
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (legacy_project_id, message_uuid),
        )
    db.commit()

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            legacy_project_id,
            compact_project_id,
        )
    )

    assert _workspace_message_read_states(
        db,
        compact_project_id,
        reader_uuid,
        [message_uuid],
    ) == {message_uuid: False}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_message_mentions_v1
            WHERE message_uuid = %s AND user_uuid = %s
            """,
            (message_uuid, reader_uuid),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_legacy_stream_move_preserves_detached_flags_without_membership(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Detached legacy move"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    _set_workspace_read_mode(db, destination_project_id, read_state.PROJECT_MODE_LEGACY)
    message_uuids = []
    for content in ("detached read", "detached unread"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            destination_project_id,
        )
    )
    conftest.seed_user_stream_binding(
        db, destination_project_id, stream_uuid, reader_uuid
    )
    _run_database_operation(
        lambda session: (
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                destination_project_id,
                stream_uuid,
                reader_uuid,
                session=session,
            )
        )
    )

    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: True,
        message_uuids[1]: False,
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_message_flags
            WHERE project_id = %s AND user_uuid = %s
              AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, reader_uuid, message_uuids),
        )
        assert cursor.fetchone()[0] == 0


def test_legacy_to_compact_stream_move_preserves_detached_boundary(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Detached legacy compact move"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    _set_workspace_read_mode(
        db,
        destination_project_id,
        read_state.PROJECT_MODE_COMPACT,
    )
    message_uuids = []
    for content in ("detached read", "detached unread"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
    db.commit()
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "post-detach gap"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuids.append(response.json()["uuid"])

    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            stream_uuid,
            api.project_id,
            destination_project_id,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT membership.last_detached_sequence, message.ingest_sequence
            FROM m_workspace_read_memberships_v1 AS membership
            JOIN m_workspace_messages AS message
              ON message.project_id = membership.project_id
             AND message.uuid = %s
            WHERE membership.project_id = %s
              AND membership.user_uuid = %s
              AND membership.stream_uuid = %s
            """,
            (
                message_uuids[1],
                destination_project_id,
                reader_uuid,
                stream_uuid,
            ),
        )
        detached_sequence, last_pre_detach_sequence = cursor.fetchone()
        assert detached_sequence == last_pre_detach_sequence

    conftest.seed_user_stream_binding(
        db,
        destination_project_id,
        stream_uuid,
        reader_uuid,
    )
    _run_database_operation(
        lambda session: (
            messenger_dm_helpers._create_workspace_stream_binding_message_flags(
                destination_project_id,
                stream_uuid,
                reader_uuid,
                session=session,
            )
        )
    )

    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: True,
        message_uuids[1]: False,
        message_uuids[2]: True,
    }


def test_compact_topic_merge_preserves_read_counts_and_mentions(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact topic merge"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    destination_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "destination",
        is_default=True,
    )
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "source",
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for topic_uuid, content in (
        (source_topic_uuid, f"read mention ](urn:user:{reader_uuid})"),
        (destination_topic_uuid, "unread destination"),
    ):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuids[0]}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    def merge(session):
        read_state.merge_topics(
            session,
            api.project_id,
            (source_topic_uuid,),
            stream_uuid,
            destination_topic_uuid,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET topic_uuid = %s, updated_at = NOW()
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (destination_topic_uuid, api.project_id, source_topic_uuid),
        )
        session.execute(
            """
            DELETE FROM m_workspace_stream_topics
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, source_topic_uuid),
        )

    _run_database_operation(merge)

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        message_uuids,
    ) == {
        message_uuids[0]: True,
        message_uuids[1]: False,
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_count
            FROM m_workspace_topic_message_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (api.project_id, destination_topic_uuid),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, destination_topic_uuid),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_message_mentions_v1
            WHERE message_uuid = %s AND user_uuid = %s
            """,
            (message_uuids[0], reader_uuid),
        )
        assert cursor.fetchone() == (sys_uuid.UUID(destination_topic_uuid),)


@pytest.mark.parametrize(
    "source_mode",
    [read_state.PROJECT_MODE_LEGACY, read_state.PROJECT_MODE_COMPACT],
)
def test_message_move_materializes_compact_destination_before_canonical_move(
    api, db, source_mode
):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    source_stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact message source"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, source_stream_uuid, reader_uuid
    )
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        source_stream_uuid,
        api.user_uuid,
        "source",
        is_default=True,
    )
    destination_stream_uuid = conftest.seed_user_stream(
        db, destination_project_id, api.user_uuid, "Compact message destination"
    )
    conftest.seed_user_stream_binding(
        db, destination_project_id, destination_stream_uuid, reader_uuid
    )
    destination_topic_uuid = conftest.seed_stream_topic(
        db,
        destination_project_id,
        destination_stream_uuid,
        api.user_uuid,
        "destination",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, source_mode)
    _set_workspace_read_mode(
        db, destination_project_id, read_state.PROJECT_MODE_COMPACT
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": source_stream_uuid,
            "topic_uuid": source_topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": f"move read bit ](urn:user:{reader_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    def move_message(session):
        read_state.relocate_message(
            session,
            message_uuid,
            api.project_id,
            destination_project_id,
            destination_stream_uuid,
            destination_topic_uuid,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET project_id = %s, stream_uuid = %s, topic_uuid = %s
            WHERE uuid = %s AND project_id = %s
            """,
            (
                destination_project_id,
                destination_stream_uuid,
                destination_topic_uuid,
                message_uuid,
                api.project_id,
            ),
        )

    _run_database_operation(move_message)

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (destination_project_id, reader_uuid, destination_topic_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (destination_project_id, message_uuid),
        )
        assert cursor.fetchone()[0] is not None
        cursor.execute(
            """
            SELECT project_id, stream_uuid, topic_uuid
            FROM m_workspace_message_mentions_v1
            WHERE message_uuid = %s AND user_uuid = %s
            """,
            (message_uuid, reader_uuid),
        )
        assert cursor.fetchone() == (
            destination_project_id,
            sys_uuid.UUID(destination_stream_uuid),
            sys_uuid.UUID(destination_topic_uuid),
        )
    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        [message_uuid],
    ) == {str(message_uuid): True}


def test_legacy_message_move_into_dual_gap_cutover_preserves_missing_read(api, db):
    reader_uuid = sys_uuid.uuid4()
    destination_project_id = sys_uuid.uuid4()
    source_stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Legacy missing read message source",
    )
    conftest.seed_user_stream_binding(
        db,
        api.project_id,
        source_stream_uuid,
        reader_uuid,
    )
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        source_stream_uuid,
        api.user_uuid,
        "source",
        is_default=True,
    )
    destination_stream_uuid = conftest.seed_user_stream(
        db,
        destination_project_id,
        api.user_uuid,
        "Dual cutover message destination",
    )
    conftest.seed_user_stream_binding(
        db,
        destination_project_id,
        destination_stream_uuid,
        reader_uuid,
    )
    destination_topic_uuid = conftest.seed_stream_topic(
        db,
        destination_project_id,
        destination_stream_uuid,
        api.user_uuid,
        "destination",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_LEGACY)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": source_stream_uuid,
            "topic_uuid": source_topic_uuid,
            "payload": {"kind": "markdown", "content": "implicit legacy read"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_user_message_flags
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (api.project_id, message_uuid, reader_uuid),
        )
    db.commit()
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT unread_count
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND user_uuid = %s AND uuid = %s
            """,
            (api.project_id, reader_uuid, source_topic_uuid),
        )
        assert cursor.fetchone() == (0,)

    _run_database_operation(
        lambda session: read_state.begin_compaction(
            session,
            destination_project_id,
        )
    )
    _advance_read_compaction_to_phase(
        db,
        destination_project_id,
        "legacy_gaps",
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT target_ingest_sequence
            FROM m_workspace_read_state_compaction_v1
            WHERE project_id = %s
            """,
            (destination_project_id,),
        )
        cutover_target = cursor.fetchone()[0]

    def move_message(session):
        read_state.relocate_message(
            session,
            message_uuid,
            api.project_id,
            destination_project_id,
            destination_stream_uuid,
            destination_topic_uuid,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET project_id = %s, stream_uuid = %s, topic_uuid = %s
            WHERE uuid = %s AND project_id = %s
            """,
            (
                destination_project_id,
                destination_stream_uuid,
                destination_topic_uuid,
                message_uuid,
                api.project_id,
            ),
        )

    _run_database_operation(move_message)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (destination_project_id, message_uuid),
        )
        assert cursor.fetchone()[0] > cutover_target

    _finish_read_compaction(db, destination_project_id)
    assert _workspace_message_read_states(
        db,
        destination_project_id,
        reader_uuid,
        [message_uuid],
    ) == {message_uuid: True}


def test_compact_message_cross_project_move_rebinds_every_coordinate(api, db):
    reader_uuid = sys_uuid.uuid4()
    project_ids = (
        sys_uuid.UUID(str(api.project_id)),
        sys_uuid.uuid4(),
        sys_uuid.uuid4(),
    )
    stream_uuids = []
    topic_uuids = []
    for index, project_id in enumerate(project_ids):
        stream_uuid = conftest.seed_user_stream(
            db,
            project_id,
            api.user_uuid,
            f"Message coordinate destination {index}",
        )
        conftest.seed_user_stream_binding(db, project_id, stream_uuid, reader_uuid)
        topic_uuid = conftest.seed_stream_topic(
            db,
            project_id,
            stream_uuid,
            api.user_uuid,
            f"destination-{index}",
            is_default=True,
        )
        _set_workspace_read_mode(db, project_id, read_state.PROJECT_MODE_COMPACT)
        stream_uuids.append(sys_uuid.UUID(stream_uuid))
        topic_uuids.append(sys_uuid.UUID(topic_uuid))

    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": str(stream_uuids[0]),
            "topic_uuid": str(topic_uuids[0]),
            "payload": {
                "kind": "markdown",
                "content": f"moving read mention ](urn:user:{reader_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT ingest_sequence FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        original_sequence = cursor.fetchone()[0]
        original_chunk, original_offset = divmod(
            original_sequence,
            read_state.READ_CHUNK_BITS,
        )
        rolling_sequence = 123_456_789
        rolling_chunk, rolling_offset = divmod(
            rolling_sequence,
            read_state.READ_CHUNK_BITS,
        )
        cursor.execute(
            """
            SELECT user_uuid
            FROM m_workspace_user_read_chunks_v1
            WHERE chunk_number = %s AND get_bit(read_bits, %s) = 1
            """,
            (original_chunk, original_offset),
        )
        read_user_uuids = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """
            UPDATE m_workspace_user_read_chunks_v1
            SET read_bits = set_bit(read_bits, %s, 0), updated_at = NOW()
            WHERE chunk_number = %s AND get_bit(read_bits, %s) = 1
            """,
            (original_offset, original_chunk, original_offset),
        )
        cursor.execute(
            """
            DELETE FROM m_workspace_user_read_chunks_v1
            WHERE chunk_number = %s AND bit_count(read_bits) = 0
            """,
            (original_chunk,),
        )
        for read_user_uuid in read_user_uuids:
            cursor.execute(
                """
                INSERT INTO m_workspace_user_read_chunks_v1 (
                    user_uuid, chunk_number, read_bits, created_at, updated_at
                ) VALUES (
                    %s, %s, set_bit(B'0'::bit(4096), %s, 1), NOW(), NOW()
                )
                ON CONFLICT (user_uuid, chunk_number) DO UPDATE
                SET read_bits = set_bit(
                        m_workspace_user_read_chunks_v1.read_bits,
                        %s,
                        1
                    ),
                    updated_at = NOW()
                """,
                (
                    read_user_uuid,
                    rolling_chunk,
                    rolling_offset,
                    rolling_offset,
                ),
            )
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = %s, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (rolling_sequence, project_ids[0], message_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_message_mentions_v1
            SET ingest_sequence = %s
            WHERE project_id = %s AND message_uuid = %s
            """,
            (rolling_sequence, project_ids[0], message_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_topic_message_stats_v1
            SET last_ingest_sequence = %s, updated_at = NOW()
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (rolling_sequence, project_ids[0], topic_uuids[0]),
        )
    db.commit()
    initial_sequence = rolling_sequence

    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    operation_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(read_capability)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_operations_v2 (
                uuid, external_account_uuid, owner_user_uuid,
                action, target_type, target_uuid, details
            ) VALUES (
                %s, %s, %s, 'read_state.set', 'stream', %s, '{}'::jsonb
            )
            """,
            (
                operation_uuid,
                account_uuid,
                api.user_uuid,
                stream_uuids[0],
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_read_snapshots_v1 (
                external_operation_uuid, bridge_instance_uuid,
                external_account_uuid, project_id, causal_lane, payload
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                operation_uuid,
                bridge_uuid,
                account_uuid,
                project_ids[0],
                stream_uuids[0],
                json.dumps(
                    {
                        "stream_uuid": str(stream_uuids[0]),
                        "topic_uuid": None,
                        "reader_uuid": str(api.user_uuid),
                        "read": True,
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_read_candidate_chunks_v1 (
                external_operation_uuid, chunk_number, candidate_bits
            ) VALUES (%s, %s, %s::bit(4096))
            """,
            (
                operation_uuid,
                initial_sequence // read_state.READ_CHUNK_BITS,
                read_state._bits_literal(
                    [initial_sequence % read_state.READ_CHUNK_BITS]
                ),
            ),
        )
    db.commit()

    def move_message(source_index, destination_index):
        def move(session):
            read_state.relocate_message(
                session,
                message_uuid,
                project_ids[source_index],
                project_ids[destination_index],
                stream_uuids[destination_index],
                topic_uuids[destination_index],
            )
            session.execute(
                """
                UPDATE m_workspace_messages
                SET project_id = %s, stream_uuid = %s, topic_uuid = %s
                WHERE uuid = %s AND project_id = %s
                """,
                (
                    project_ids[destination_index],
                    stream_uuids[destination_index],
                    topic_uuids[destination_index],
                    message_uuid,
                    project_ids[source_index],
                ),
            )

        _run_database_operation(move)

    previous_sequences = [original_sequence, initial_sequence]
    for source_index, destination_index in ((0, 1), (1, 2)):
        move_message(source_index, destination_index)
        assert _workspace_message_read_states(
            db,
            project_ids[destination_index],
            reader_uuid,
            [message_uuid],
        ) == {str(message_uuid): True}
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT message.ingest_sequence, range.range_number
                FROM m_workspace_messages AS message
                JOIN m_workspace_project_ingest_ranges_v2 AS range
                  ON range.project_id = message.project_id
                WHERE message.project_id = %s AND message.uuid = %s
                """,
                (project_ids[destination_index], message_uuid),
            )
            moved_sequence, range_number = cursor.fetchone()
            range_base = range_number * read_state.PROJECT_SEQUENCE_RANGE_SIZE
            assert (
                range_base
                < moved_sequence
                < (range_base + read_state.PROJECT_SEQUENCE_RANGE_SIZE)
            )
            cursor.execute(
                """
                SELECT project_id, stream_uuid, topic_uuid, ingest_sequence
                FROM m_workspace_message_mentions_v1
                WHERE message_uuid = %s AND user_uuid = %s
                """,
                (message_uuid, reader_uuid),
            )
            assert cursor.fetchone() == (
                project_ids[destination_index],
                stream_uuids[destination_index],
                topic_uuids[destination_index],
                moved_sequence,
            )
            cursor.execute(
                """
                SELECT message_count, last_ingest_sequence
                FROM m_workspace_topic_message_stats_v1
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (project_ids[destination_index], topic_uuids[destination_index]),
            )
            assert cursor.fetchone() == (1, moved_sequence)
            cursor.execute(
                """
                SELECT message_count, last_ingest_sequence
                FROM m_workspace_topic_message_stats_v1
                WHERE project_id = %s AND topic_uuid = %s
                """,
                (project_ids[source_index], topic_uuids[source_index]),
            )
            assert cursor.fetchone() == (0, None)
            cursor.execute(
                """
                SELECT project_id, topic_uuid, read_count
                FROM m_workspace_user_topic_read_stats_v1
                WHERE user_uuid = %s
                  AND (project_id, topic_uuid) IN (
                        (%s, %s),
                        (%s, %s)
                  )
                ORDER BY project_id = %s, project_id
                """,
                (
                    reader_uuid,
                    project_ids[source_index],
                    topic_uuids[source_index],
                    project_ids[destination_index],
                    topic_uuids[destination_index],
                    project_ids[source_index],
                ),
            )
            assert set(cursor.fetchall()) == {
                (
                    project_ids[source_index],
                    topic_uuids[source_index],
                    0,
                ),
                (
                    project_ids[destination_index],
                    topic_uuids[destination_index],
                    1,
                ),
            }
            cursor.execute(
                """
                SELECT chunk_number, bit_count(candidate_bits)
                FROM m_external_provider_read_candidate_chunks_v1
                WHERE external_operation_uuid = %s
                ORDER BY chunk_number
                """,
                (operation_uuid,),
            )
            assert cursor.fetchall() == [
                (moved_sequence // read_state.READ_CHUNK_BITS, 1)
            ]
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM m_workspace_user_read_chunks_v1
                WHERE user_uuid = %s
                  AND chunk_number = ANY(%s::bigint[])
                """,
                (
                    reader_uuid,
                    [
                        sequence // read_state.READ_CHUNK_BITS
                        for sequence in previous_sequences
                    ],
                ),
            )
            assert cursor.fetchone() == (0,)
        previous_sequences.append(moved_sequence)


def test_compact_message_move_through_legacy_discards_stale_shadow_state(api, db):
    reader_uuid = sys_uuid.uuid4()
    legacy_project_id = sys_uuid.uuid4()
    compact_project_id = sys_uuid.uuid4()
    source_stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Message move source"
    )
    conftest.seed_user_stream_binding(
        db, api.project_id, source_stream_uuid, reader_uuid
    )
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        source_stream_uuid,
        api.user_uuid,
        "source",
        is_default=True,
    )
    legacy_stream_uuid = conftest.seed_user_stream(
        db, legacy_project_id, api.user_uuid, "Message move legacy"
    )
    conftest.seed_user_stream_binding(
        db, legacy_project_id, legacy_stream_uuid, reader_uuid
    )
    legacy_topic_uuid = conftest.seed_stream_topic(
        db,
        legacy_project_id,
        legacy_stream_uuid,
        api.user_uuid,
        "legacy",
        is_default=True,
    )
    compact_stream_uuid = conftest.seed_user_stream(
        db, compact_project_id, api.user_uuid, "Message move compact"
    )
    conftest.seed_user_stream_binding(
        db, compact_project_id, compact_stream_uuid, reader_uuid
    )
    compact_topic_uuid = conftest.seed_stream_topic(
        db,
        compact_project_id,
        compact_stream_uuid,
        api.user_uuid,
        "compact",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    _set_workspace_read_mode(db, legacy_project_id, read_state.PROJECT_MODE_LEGACY)
    _set_workspace_read_mode(db, compact_project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": source_stream_uuid,
            "topic_uuid": source_topic_uuid,
            "payload": {
                "kind": "markdown",
                "content": f"move mention ](urn:user:{reader_uuid})",
            },
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = response.json()["uuid"]
    response = api.post(
        f"{MESSAGES}{message_uuid}/actions/read/invoke",
        user=reader_uuid,
    )
    assert response.status_code == 200, response.text

    def move_message(
        session,
        source_project_id,
        destination_project_id,
        destination_stream_uuid,
        destination_topic_uuid,
    ):
        read_state.relocate_message(
            session,
            message_uuid,
            source_project_id,
            destination_project_id,
            destination_stream_uuid,
            destination_topic_uuid,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET project_id = %s, stream_uuid = %s, topic_uuid = %s
            WHERE uuid = %s AND project_id = %s
            """,
            (
                destination_project_id,
                destination_stream_uuid,
                destination_topic_uuid,
                message_uuid,
                source_project_id,
            ),
        )

    _run_database_operation(
        lambda session: move_message(
            session,
            api.project_id,
            legacy_project_id,
            legacy_stream_uuid,
            legacy_topic_uuid,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_user_message_flags
            SET read = FALSE, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (legacy_project_id, message_uuid, reader_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET payload = '{"kind":"markdown","content":"mention removed"}',
                updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (legacy_project_id, message_uuid),
        )
    db.commit()

    _run_database_operation(
        lambda session: move_message(
            session,
            legacy_project_id,
            compact_project_id,
            compact_stream_uuid,
            compact_topic_uuid,
        )
    )

    assert _workspace_message_read_states(
        db,
        compact_project_id,
        reader_uuid,
        [message_uuid],
    ) == {message_uuid: False}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_message_mentions_v1
            WHERE message_uuid = %s AND user_uuid = %s
            """,
            (message_uuid, reader_uuid),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_compact_topic_and_stream_delete_clear_only_their_bitmap_bits(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact delete cleanup"
    )
    conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact delete survivor"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    default_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    deleted_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "delete first",
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for topic_uuid in (default_topic_uuid, deleted_topic_uuid):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "read then delete"},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))
    _run_database_operation(
        lambda session: read_state.set_message_uuids_read(
            session,
            api.project_id,
            reader_uuid,
            message_uuids,
            True,
        )
    )

    response = api.delete(f"{STREAM_TOPICS}{deleted_topic_uuid}")
    assert response.status_code in (200, 204), response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 1

    response = api.delete(f"{STREAMS}{stream_uuid}")
    assert response.status_code in (200, 204), response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_project_dense_read_coordinates_ignore_other_project_traffic(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Dense read coordinates"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    noise_project_uuid = sys_uuid.uuid4()
    noise_stream_uuid = conftest.seed_user_stream(
        db, noise_project_uuid, api.user_uuid, "Dense read coordinate noise"
    )
    noise_topic_uuid = conftest.seed_stream_topic(
        db,
        noise_project_uuid,
        noise_stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)

    first = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "before noise"},
        },
    )
    assert first.status_code == 201, first.text
    noise_seed = str(sys_uuid.uuid4())
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            )
            SELECT md5(%s || ':' || series)::uuid, %s, %s, %s, %s,
                   '{"kind":"markdown","content":"noise"}'::jsonb,
                   NOW() + series * interval '1 microsecond',
                   NOW() + series * interval '1 microsecond'
            FROM generate_series(1, 4096) AS series
            """,
            (
                noise_seed,
                noise_project_uuid,
                noise_stream_uuid,
                noise_topic_uuid,
                api.user_uuid,
            ),
        )
    db.commit()
    second = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "after noise"},
        },
    )
    assert second.status_code == 201, second.text
    message_uuids = [
        sys_uuid.UUID(first.json()["uuid"]),
        sys_uuid.UUID(second.json()["uuid"]),
    ]

    _run_database_operation(
        lambda session: read_state.set_message_uuids_read(
            session,
            api.project_id,
            reader_uuid,
            message_uuids,
            True,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT array_agg(ingest_sequence ORDER BY created_at, uuid)
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids),
        )
        first_sequence, second_sequence = cursor.fetchone()[0]
        assert second_sequence == first_sequence + 1
        cursor.execute(
            """
            SELECT COUNT(*), SUM(bit_count(read_bits))
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone() == (1, 2)

        cursor.execute(
            "DELETE FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
            (noise_stream_uuid, noise_project_uuid),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (noise_project_uuid,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_project_ingest_ranges_v2 WHERE project_id = %s",
            (noise_project_uuid,),
        )
    db.commit()


def test_project_sequence_backfill_uses_bounded_low_half(api, db):
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Bounded sequence backfill",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuids = []
    for index in range(3):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": f"legacy coordinate {index}",
                },
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT range_number, last_live_sequence
            FROM m_workspace_project_ingest_ranges_v2
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        range_number, last_live_sequence = cursor.fetchone()
        range_base = range_number * read_state.PROJECT_SEQUENCE_RANGE_SIZE
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET ingest_sequence = nextval(
                'm_workspace_messages_legacy_ingest_sequence_v1_seq'
            )
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids),
        )

    def backfill(batch_size):
        def run(session):
            read_state.lock_projects(session, (api.project_id,))
            return read_state._assign_legacy_ingest_sequences(
                session,
                api.project_id,
                batch_size=batch_size,
            )

        return _run_database_operation(run)

    assert backfill(2) == 2
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            ORDER BY created_at, uuid
            """,
            (api.project_id, message_uuids),
        )
        sequences = [row[0] for row in cursor.fetchall()]
        assert sequences[:2] == [range_base + 1, range_base + 2]
        assert not (
            range_base
            < sequences[2]
            < range_base + read_state.PROJECT_SEQUENCE_RANGE_SIZE
        )

    assert backfill(2) == 1
    assert backfill(2) == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT array_agg(ingest_sequence ORDER BY created_at, uuid)
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, message_uuids),
        )
        assert cursor.fetchone()[0] == [
            range_base + 1,
            range_base + 2,
            range_base + 3,
        ]
        cursor.execute(
            """
            SELECT last_backfill_sequence, last_live_sequence
            FROM m_workspace_project_ingest_ranges_v2
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone() == (3, last_live_sequence)


def test_compact_storage_stays_bounded_for_many_users_and_messages(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact scale"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    topic_uuids = [topic_uuid]
    for index in range(1, 64):
        topic_uuids.append(
            conftest.seed_stream_topic(
                db,
                api.project_id,
                stream_uuid,
                api.user_uuid,
                f"scale-{index}",
            )
        )
    reader_uuids = [sys_uuid.uuid4() for _index in range(499)]
    for reader_uuid in reader_uuids:
        conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_count = 10_000
    seed = str(sys_uuid.uuid4())
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, created_at, updated_at
            )
            SELECT md5(%s || ':' || series)::uuid, %s, %s,
                   (%s::uuid[])[
                       ((series - 1) %% array_length(%s::uuid[], 1)) + 1
                   ],
                   %s,
                   '{"kind":"markdown","content":"scale"}'::jsonb,
                   NOW() + series * interval '1 microsecond',
                   NOW() + series * interval '1 microsecond'
            FROM generate_series(1, %s) AS series
            """,
            (
                seed,
                api.project_id,
                stream_uuid,
                topic_uuids,
                topic_uuids,
                api.user_uuid,
                message_count,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_topic_message_stats_v1 (
                topic_uuid, project_id, stream_uuid, message_count,
                last_ingest_sequence, created_at, updated_at
            )
            SELECT topic_uuid, %s, %s, COUNT(*), MAX(ingest_sequence), NOW(), NOW()
            FROM m_workspace_messages
            WHERE project_id = %s AND stream_uuid = %s
            GROUP BY topic_uuid
            ON CONFLICT (topic_uuid) DO UPDATE
            SET message_count = EXCLUDED.message_count,
                last_ingest_sequence = EXCLUDED.last_ingest_sequence,
                updated_at = NOW()
            """,
            (
                api.project_id,
                stream_uuid,
                api.project_id,
                stream_uuid,
            ),
        )
    db.commit()

    reader_uuid = reader_uuids[0]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT SUM(unread_count)
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
        assert cursor.fetchone()[0] == message_count
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_message_flags
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == 0

    result = _run_database_operation(
        lambda session: read_state.read_stream(
            session,
            api.project_id,
            reader_uuid,
            stream_uuid,
            collect_message_rows=False,
        )
    )
    assert result.changed is True
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*), SUM(bit_count(read_bits))
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        chunk_count, read_count = cursor.fetchone()
        cursor.execute(
            """
            SELECT SUM(unread_count)
            FROM m_workspace_user_topics_view
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (api.project_id, stream_uuid, reader_uuid),
        )
        assert cursor.fetchone()[0] == 0
    assert chunk_count <= (message_count // read_state.READ_CHUNK_BITS) + 2
    assert read_count == message_count
    response = api.get(
        MESSAGES,
        user=reader_uuid,
        params={"stream_uuid": stream_uuid, "read": False, "page_limit": 50},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []

    provider_reader_uuid = reader_uuids[1]
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, provider_reader_uuid, json.dumps(read_capability)),
        )
    db.commit()

    public_operation_uuids = []

    def queue_provider_snapshot(
        session,
        candidate_sql,
        candidate_values,
        candidate_chunks,
    ):
        operation = provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=provider_reader_uuid,
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(stream_uuid)),
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(provider_reader_uuid),
                "read": True,
            },
            candidate_sql=candidate_sql,
            candidate_values=candidate_values,
            candidate_chunks=candidate_chunks,
            use_candidate_chunks=True,
        )
        assert operation is not None
        public_operation_uuids.append(operation.uuid)

    provider_result = _run_database_operation(
        lambda session: read_state.read_stream(
            session,
            api.project_id,
            provider_reader_uuid,
            stream_uuid,
            collect_message_rows=False,
            message_uuid_snapshot_callback=queue_provider_snapshot,
        )
    )
    assert provider_result.changed is True
    assert len(public_operation_uuids) == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*), SUM(bit_count(read_bits))
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (provider_reader_uuid,),
        )
        provider_chunk_count, provider_read_count = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
              AND operation_kind = 'read_state.set'
            """,
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT COUNT(*), SUM(bit_count(candidate_bits)),
                   SUM(pg_column_size(candidate_bits))
            FROM m_external_provider_read_candidate_chunks_v1
            WHERE external_operation_uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        (
            snapshot_chunk_count,
            snapshot_candidate_count,
            snapshot_storage_bytes,
        ) = cursor.fetchone()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_candidate_packs_v1
            WHERE external_operation_uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT uuid::text
            FROM m_workspace_messages
            WHERE project_id = %s AND stream_uuid = %s
            ORDER BY ingest_sequence
            """,
            (api.project_id, stream_uuid),
        )
        expected_provider_uuids = [row[0] for row in cursor.fetchall()]
    assert provider_chunk_count <= (message_count // read_state.READ_CHUNK_BITS) + 2
    assert provider_read_count == message_count
    assert snapshot_chunk_count <= (message_count // read_state.READ_CHUNK_BITS) + 2
    assert snapshot_candidate_count == message_count
    assert snapshot_storage_bytes < message_count

    post_snapshot = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "after snapshot"},
        },
    )
    assert post_snapshot.status_code == 201, post_snapshot.text
    post_snapshot_uuid = post_snapshot.json()["uuid"]

    # A later exact read for the same provider account must stay behind the
    # snapshot without consuming its bounded lazy-page window.
    later_public_uuid = sys_uuid.uuid4()
    _later_operation, later_provider_uuid = _run_database_operation(
        lambda session: provider_data.enqueue_provider_operation(
            session,
            operation_uuid=later_public_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=provider_reader_uuid,
            operation_kind="read_state.set",
            target_type="provider-read-test",
            target_uuid=None,
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(provider_reader_uuid),
                "read": True,
                "message_uuids": [post_snapshot_uuid],
            },
        )
    )

    # Coordinates are immutable while message UUIDs are resolved lazily. A
    # fully deleted first page is skipped, its progress bits are consumed, and
    # the same lease call continues to materialize useful pages.
    deleted_after_snapshot_uuids = expected_provider_uuids[:500]
    with db.cursor() as cursor:
        cursor.execute(
            """
            DELETE FROM m_workspace_messages
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (api.project_id, deleted_after_snapshot_uuids),
        )
    db.commit()
    expected_provider_uuids = expected_provider_uuids[500:]
    operation_count = (len(expected_provider_uuids) + 499) // 500

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )

    def lease_once(page_limit=3, request_uuid=None):
        return _run_database_operation(
            lambda session: provider_data.lease_provider_operations(
                session,
                identity,
                request_uuid=request_uuid or sys_uuid.uuid4(),
                limit=page_limit,
                lease_seconds=30,
            )
        )

    concurrent_request_uuid = sys_uuid.uuid4()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_leases = list(
            executor.map(
                lambda _index: lease_once(request_uuid=concurrent_request_uuid),
                range(2),
            )
        )
    assert [len(item["operations"]) for item in concurrent_leases] == [3, 3]
    assert [
        operation["provider_operation_uuid"]
        for operation in concurrent_leases[0]["operations"]
    ] == [
        operation["provider_operation_uuid"]
        for operation in concurrent_leases[1]["operations"]
    ]
    lease = concurrent_leases[0]
    assert all(
        leased["provider_operation_uuid"] != str(later_provider_uuid)
        for leased in lease["operations"]
    )
    assert all(
        leased["external_operation_uuid"] == leased["provider_operation_uuid"]
        and "_workspace_response_revision" not in leased["payload"]
        for leased in lease["operations"]
    )
    delivered_message_uuids = [
        message_uuid
        for leased in lease["operations"]
        for message_uuid in leased["payload"]["message_uuids"]
    ]
    assert all(
        1 <= len(leased["payload"]["message_uuids"]) <= 500
        for leased in lease["operations"]
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid = %s
              AND operation_kind = 'read_state.set'
              AND status IN ('queued', 'leased')
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone()[0] == 3

    def report_result(leased, status, safe_error=None, result_uuid=None):
        result_uuid = result_uuid or sys_uuid.uuid4()
        result = {
            "result_uuid": str(result_uuid),
            "provider_operation_uuid": leased["provider_operation_uuid"],
            "lease_uuid": leased["lease_uuid"],
            "status": status,
            "safe_error": safe_error,
        }
        return _run_database_operation(
            lambda session: provider_data.report_provider_result(
                session,
                identity,
                result,
            )
        )

    failed_lease = lease["operations"][0]
    failed_provider_uuid = failed_lease["provider_operation_uuid"]
    failed_result_uuid = sys_uuid.uuid4()
    reported = report_result(
        failed_lease,
        "failed",
        "temporary read failure",
        failed_result_uuid,
    )
    assert reported["status"] == "applied"
    for leased in lease["operations"][1:]:
        assert report_result(leased, "succeeded")["status"] == "applied"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, can_retry
            FROM m_external_operations_v2
            WHERE uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone() == ("failed", True)

    response = api.post(
        f"{EXTERNAL_OPERATIONS}{public_operation_uuids[0]}/actions/retry/invoke",
        user=provider_reader_uuid,
        json={},
    )
    assert response.status_code == 200, response.text
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (bridge_uuid,),
        )
        cursor.execute(
            """
            SELECT uuid::text, status, attempt
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid = %s
              AND status = 'queued'
            """,
            (public_operation_uuids[0],),
        )
        retried_provider_uuid, retry_status, retry_attempt = cursor.fetchone()
    db.commit()
    assert retried_provider_uuid != failed_provider_uuid
    assert (retry_status, retry_attempt) == ("queued", 1)

    retry_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )
    )
    assert len(retry_lease["operations"]) == 1
    retried = retry_lease["operations"][0]
    assert retried["provider_operation_uuid"] == retried_provider_uuid
    assert retried["external_operation_uuid"] == retried_provider_uuid
    assert retried["payload"] == failed_lease["payload"]
    assert retried["attempt"] == 2
    assert report_result(retried, "succeeded")["status"] == "applied"
    assert (
        report_result(
            failed_lease,
            "failed",
            "temporary read failure",
            failed_result_uuid,
        )["status"]
        == "duplicate"
    )

    while len(delivered_message_uuids) < len(expected_provider_uuids):
        page_lease = lease_once()
        assert 1 <= len(page_lease["operations"]) <= 3
        for leased in page_lease["operations"]:
            page_message_uuids = leased["payload"]["message_uuids"]
            assert 1 <= len(page_message_uuids) <= 500
            delivered_message_uuids.extend(page_message_uuids)
            assert report_result(leased, "succeeded")["status"] == "applied"
        if len(delivered_message_uuids) < len(expected_provider_uuids):
            with db.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status
                    FROM m_external_operations_v2
                    WHERE uuid = %s
                    """,
                    (public_operation_uuids[0],),
                )
                assert cursor.fetchone()[0] == "running"

    assert delivered_message_uuids == expected_provider_uuids
    assert post_snapshot_uuid not in delivered_message_uuids

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, attempt, safe_error
            FROM m_external_operations_v2
            WHERE uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone() == ("succeeded", 2, None)
        cursor.execute(
            """
            SELECT COUNT(*), MIN(attempt), MAX(attempt),
                   COUNT(*) FILTER (
                       WHERE status = 'succeeded'
                         AND payload->'message_uuids' = '[]'::jsonb
                   )
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone() == (
            operation_count + 1,
            1,
            2,
            operation_count,
        )
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (public_operation_uuids[0],),
        )
        assert cursor.fetchone()[0] == 0

    later_lease = lease_once(page_limit=1)
    assert len(later_lease["operations"]) == 1
    assert later_lease["operations"][0]["provider_operation_uuid"] == str(
        later_provider_uuid
    )
    assert report_result(later_lease["operations"][0], "succeeded")["status"] == (
        "applied"
    )

    revision_one_reader_uuid = reader_uuids[2]
    revision_one_operation = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=revision_one_reader_uuid,
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(stream_uuid)),
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(revision_one_reader_uuid),
                "read": True,
            },
            candidate_sql="""
                SELECT uuid, created_at, ingest_sequence
                FROM m_workspace_messages
                WHERE project_id = %s AND stream_uuid = %s
            """,
            candidate_values=(api.project_id, stream_uuid),
            use_candidate_chunks=False,
        )
    )
    assert revision_one_operation is not None
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*), MIN(jsonb_array_length(payload->'message_uuids')),
                   MAX(jsonb_array_length(payload->'message_uuids'))
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
              AND payload->>'reader_uuid' = %s
            """,
            (account_uuid, str(revision_one_reader_uuid)),
        )
        assert cursor.fetchone() == (20, 1, 500)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_account_uuid = %s
              AND payload->>'reader_uuid' = %s
            """,
            (account_uuid, str(revision_one_reader_uuid)),
        )
        assert cursor.fetchone()[0] == 0

    # Keep the session-scoped integration database small.  Migration tests run
    # a real downgrade later in the suite, and retaining this 5,000,000-row
    # logical recipient/message matrix would turn their deliberately tiny
    # resume batches into unrelated scale work.
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_streams WHERE project_id = %s AND uuid = %s",
            (api.project_id, stream_uuid),
        )
        cursor.execute(
            "DELETE FROM m_workspace_users WHERE uuid = ANY(%s::uuid[])",
            (reader_uuids,),
        )
    db.commit()


def test_compact_provider_snapshot_bounds_deleted_candidate_scans(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Deleted provider read candidates"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "deleted before lease"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(capability)),
        )
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
        ingest_sequence = cursor.fetchone()[0]
    db.commit()
    # Point a full chunk beyond any message coordinate.  Resolving it produces
    # no provider page, but one lease must not scan the whole chunk while it
    # holds the bridge materialization lock.
    chunk_number = ingest_sequence // read_state.READ_CHUNK_BITS + 1_000_000
    candidate_bits = read_state._bits_literal(range(read_state.READ_CHUNK_BITS))
    operation_uuid = sys_uuid.uuid4()
    operation = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=operation_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(stream_uuid)),
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql="SELECT * FROM compact_candidate_query_must_not_run",
            candidate_values=(),
            candidate_chunks=[
                {"chunk_number": chunk_number, "read_bits": candidate_bits}
            ],
            use_candidate_chunks=True,
        )
    )
    assert operation is not None
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )

    def lease_once():
        return _run_database_operation(
            lambda session: provider_data.lease_provider_operations(
                session,
                identity,
                request_uuid=sys_uuid.uuid4(),
                limit=1,
                lease_seconds=30,
            )
        )

    assert lease_once()["operations"] == []
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT status, attempt FROM m_external_operations_v2 WHERE uuid = %s",
            (operation_uuid,),
        )
        assert cursor.fetchone() == ("queued", 0)
        cursor.execute(
            """
            SELECT SUM(bit_count(candidate_bits))
            FROM m_external_provider_read_candidate_chunks_v1
            WHERE external_operation_uuid = %s
            """,
            (operation_uuid,),
        )
        assert cursor.fetchone()[0] == read_state.READ_CHUNK_BITS - 4 * 500

    assert lease_once()["operations"] == []
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT SUM(bit_count(candidate_bits))
            FROM m_external_provider_read_candidate_chunks_v1
            WHERE external_operation_uuid = %s
            """,
            (operation_uuid,),
        )
        assert cursor.fetchone()[0] == read_state.READ_CHUNK_BITS - 8 * 500

    assert lease_once()["operations"] == []
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT status, attempt FROM m_external_operations_v2 WHERE uuid = %s",
            (operation_uuid,),
        )
        assert cursor.fetchone() == ("succeeded", 0)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (operation_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_provider_read_materializer_bounds_snapshot_backlog_by_lease_limit(api, db):
    conftest.seed_workspace_user(db, api.user_uuid, f"user-{api.user_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(capability)),
        )
    db.commit()

    operation_uuids = []
    candidate_bits = read_state._bits_literal([0])
    for index in range(6):
        operation_uuid = sys_uuid.uuid4()
        stream_uuid = sys_uuid.uuid4()
        operation = _run_database_operation(
            lambda session, operation_uuid=operation_uuid, index=index, stream_uuid=stream_uuid: (
                provider_data.enqueue_provider_read_operation(
                    session,
                    operation_uuid=operation_uuid,
                    bridge_instance_uuid=bridge_uuid,
                    external_account_uuid=account_uuid,
                    project_id=sys_uuid.UUID(str(api.project_id)),
                    owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
                    target_type="stream",
                    target_uuid=stream_uuid,
                    payload={
                        "stream_uuid": str(stream_uuid),
                        "topic_uuid": None,
                        "reader_uuid": str(api.user_uuid),
                        "read": True,
                    },
                    candidate_sql="SELECT * FROM candidate_query_must_not_run",
                    candidate_values=(),
                    candidate_chunks=[
                        {
                            "chunk_number": 10_000_000 + index,
                            "read_bits": candidate_bits,
                        }
                    ],
                    use_candidate_chunks=True,
                )
            )
        )
        assert operation is not None
        operation_uuids.append(operation_uuid)

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    request_uuid = sys_uuid.uuid4()
    lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )
    assert lease["operations"] == []
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = ANY(%s::uuid[])
            """,
            (operation_uuids,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            """
            SELECT status, COUNT(*)
            FROM m_external_operations_v2
            WHERE uuid = ANY(%s::uuid[])
            GROUP BY status
            ORDER BY status
            """,
            (operation_uuids,),
        )
        assert cursor.fetchall() == [("queued", 2), ("succeeded", 4)]
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
    db.commit()


def test_provider_read_replay_freezes_identity_across_revision_transitions(api, db):
    conftest.seed_workspace_user(db, api.user_uuid, f"user-{api.user_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 1,
            "limits": {},
        }
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(capability)),
        )
    db.commit()

    public_uuid = sys_uuid.uuid4()
    operation = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=public_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=stream_uuid,
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql=(
                "SELECT %s::uuid AS uuid, NOW() AS created_at, "
                "1::bigint AS ingest_sequence"
            ),
            candidate_values=(message_uuid,),
            use_candidate_chunks=False,
        )
    )
    assert operation is not None
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    request_uuid = sys_uuid.uuid4()
    lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert len(lease) == 1
    assert lease[0]["provider_operation_uuid"] != str(public_uuid)
    assert lease[0]["external_operation_uuid"] == str(public_uuid)
    assert lease[0]["payload"]["message_uuids"] == [str(message_uuid)]
    capability[provider_data.PROVIDER_READ_PAGING_CAPABILITY] = {
        "revision": 1,
        "limits": {},
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
    db.commit()
    replay = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert replay == lease
    reported = _run_database_operation(
        lambda session: provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": lease[0]["provider_operation_uuid"],
                "lease_uuid": lease[0]["lease_uuid"],
                "status": "succeeded",
                "safe_error": None,
            },
        )
    )
    assert reported["status"] == "applied"

    revision_two_public_uuid = sys_uuid.uuid4()
    _revision_two_operation, revision_two_provider_uuid = _run_database_operation(
        lambda session: provider_data.enqueue_provider_operation_in_lane(
            session,
            operation_uuid=revision_two_public_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            operation_kind="read_state.set",
            target_type="stream",
            target_uuid=stream_uuid,
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
                "_workspace_response_revision": 2,
                "message_uuids": [str(sys_uuid.uuid4())],
            },
            causal_lane=stream_uuid,
        )
    )
    revision_two_request_uuid = sys_uuid.uuid4()
    revision_two_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=revision_two_request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert len(revision_two_lease) == 1
    assert revision_two_lease[0]["provider_operation_uuid"] == str(
        revision_two_provider_uuid
    )
    assert revision_two_lease[0]["external_operation_uuid"] == str(
        revision_two_provider_uuid
    )
    capability.pop(provider_data.PROVIDER_READ_PAGING_CAPABILITY)
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
    db.commit()
    revision_two_replay = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=revision_two_request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert revision_two_replay == revision_two_lease
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (public_uuid,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
    db.commit()


def test_revision_one_provider_read_rejects_500k_candidates_without_queue_growth(
    api,
    db,
):
    bridge_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    assert (
        provider_data.PROVIDER_READ_LEGACY_MAX_PAGES
        * provider_data.PROVIDER_READ_MAX_MESSAGES
        == 50_000
    )

    with pytest.raises(
        provider_data.ProviderUnavailableError,
        match=r"messenger\.message\.read\.paging revision 1",
    ):
        _run_database_operation(
            lambda session: provider_data.enqueue_provider_read_operation(
                session,
                operation_uuid=operation_uuid,
                bridge_instance_uuid=bridge_uuid,
                external_account_uuid=account_uuid,
                project_id=sys_uuid.UUID(str(api.project_id)),
                owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
                target_type="stream",
                target_uuid=stream_uuid,
                payload={
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": None,
                    "reader_uuid": str(api.user_uuid),
                    "read": True,
                },
                candidate_sql="""
                    SELECT
                        (
                            '00000000-0000-4000-8000-'
                            || lpad(to_hex(candidate), 12, '0')
                        )::uuid AS uuid,
                        TIMESTAMPTZ '2026-01-01 00:00:00+00'
                            + candidate * INTERVAL '1 microsecond' AS created_at,
                        candidate::bigint AS ingest_sequence
                    FROM generate_series(1, 500000) AS candidate
                """,
                candidate_values=(),
                use_candidate_chunks=False,
            )
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM m_external_operations_v2 WHERE uuid = %s),
                (
                    SELECT COUNT(*)
                    FROM m_external_provider_operations_v1
                    WHERE bridge_instance_uuid = %s
                ),
                (
                    SELECT COUNT(*)
                    FROM m_external_provider_read_snapshots_v1
                    WHERE bridge_instance_uuid = %s
                )
            """,
            (operation_uuid, bridge_uuid, bridge_uuid),
        )
        assert cursor.fetchone() == (0, 0, 0)


def test_provider_read_lane_survives_full_cross_project_stream_move(api, db):
    source_stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Provider snapshot source"
    )
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        source_stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    destination_project_uuid = sys_uuid.uuid4()
    _run_database_operation(
        lambda session: read_state.ensure_new_project(session, destination_project_uuid)
    )
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": source_stream_uuid,
            "topic_uuid": source_topic_uuid,
            "payload": {"kind": "markdown", "content": "move after snapshot"},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])

    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(read_capability)),
        )
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )
        ingest_sequence = cursor.fetchone()[0]
    db.commit()

    operation_uuid = sys_uuid.uuid4()
    candidate_bits = read_state._bits_literal(
        [ingest_sequence % read_state.READ_CHUNK_BITS]
    )
    operation = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=operation_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(source_stream_uuid)),
            payload={
                "stream_uuid": str(source_stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql="SELECT * FROM bitmap_candidate_query_must_not_run",
            candidate_values=(),
            candidate_chunks=[
                {
                    "chunk_number": ingest_sequence // read_state.READ_CHUNK_BITS,
                    "read_bits": candidate_bits,
                }
            ],
            use_candidate_chunks=True,
        )
    )
    assert operation is not None

    later_public_uuid = sys_uuid.uuid4()
    later_operation, later_provider_uuid = _run_database_operation(
        lambda session: provider_data.enqueue_provider_operation_in_lane(
            session,
            operation_uuid=later_public_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            operation_kind="read_state.set",
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(source_stream_uuid)),
            payload={
                "stream_uuid": str(source_stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
                "message_uuids": [str(sys_uuid.uuid4())],
            },
            causal_lane=sys_uuid.UUID(str(source_stream_uuid)),
        )
    )
    assert later_operation.uuid == later_public_uuid
    _run_database_operation(
        lambda session: messenger_controllers._move_projection_rows(
            session,
            sys_uuid.UUID(str(source_stream_uuid)),
            sys_uuid.UUID(str(api.project_id)),
            destination_project_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT ingest_sequence
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (destination_project_uuid, message_uuid),
        )
        moved_ingest_sequence = cursor.fetchone()[0]
        assert moved_ingest_sequence != ingest_sequence
        cursor.execute(
            """
            SELECT chunk_number, bit_count(candidate_bits)
            FROM m_external_provider_read_candidate_chunks_v1
            WHERE external_operation_uuid = %s
            ORDER BY chunk_number
            """,
            (operation_uuid,),
        )
        assert cursor.fetchall() == [
            (
                moved_ingest_sequence // read_state.READ_CHUNK_BITS,
                1,
            )
        ]
        cursor.execute(
            """
            SELECT project_id
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = %s
            """,
            (operation_uuid,),
        )
        assert cursor.fetchone() == (destination_project_uuid,)
        cursor.execute(
            """
            SELECT project_id, status, attempt
            FROM m_external_provider_operations_v1
            WHERE uuid = %s
            """,
            (later_provider_uuid,),
        )
        assert cursor.fetchone() == (destination_project_uuid, "queued", 0)
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    request_uuid = sys_uuid.uuid4()
    lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )
    assert len(lease["operations"]) == 1
    assert lease["operations"][0]["payload"]["message_uuids"] == [str(message_uuid)]
    leased_snapshot_page = lease["operations"][0]
    paging_capability = read_capability.pop(
        provider_data.PROVIDER_READ_PAGING_CAPABILITY
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
    db.commit()
    replay = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=request_uuid,
            limit=1,
            lease_seconds=30,
        )
    )
    assert replay == lease
    read_capability[provider_data.PROVIDER_READ_PAGING_CAPABILITY] = paging_capability
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
    db.commit()
    reported = _run_database_operation(
        lambda session: provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": leased_snapshot_page[
                    "provider_operation_uuid"
                ],
                "lease_uuid": leased_snapshot_page["lease_uuid"],
                "status": "succeeded",
                "safe_error": None,
            },
        )
    )
    assert reported["status"] == "applied"
    later_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert len(later_lease) == 1
    assert later_lease[0]["provider_operation_uuid"] == str(later_provider_uuid)
    assert later_lease[0]["project_id"] == str(destination_project_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
            (source_stream_uuid, destination_project_uuid),
        )
        cursor.execute(
            "DELETE FROM m_workspace_read_state_projects_v1 WHERE project_id = %s",
            (destination_project_uuid,),
        )
        cursor.execute(
            "DELETE FROM m_workspace_project_ingest_ranges_v2 WHERE project_id = %s",
            (destination_project_uuid,),
        )
    db.commit()


@pytest.mark.parametrize("page_status", ["queued", "leased"])
def test_projection_move_rejects_attempted_provider_read_page(
    api,
    db,
    page_status,
):
    conftest.seed_workspace_user(db, api.user_uuid, f"user-{api.user_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    old_project_uuid = sys_uuid.UUID(str(api.project_id))
    new_project_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE
            )
            """,
            (account_uuid, api.user_uuid),
        )
    db.commit()
    _operation, provider_uuid = _run_database_operation(
        lambda session: provider_data.enqueue_provider_operation_in_lane(
            session,
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=old_project_uuid,
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            operation_kind="read_state.set",
            target_type="stream",
            target_uuid=stream_uuid,
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
                "message_uuids": [str(sys_uuid.uuid4())],
            },
            causal_lane=stream_uuid,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_provider_operations_v1
            SET status = %s, attempt = 1,
                lease_uuid = CASE WHEN %s = 'leased' THEN gen_random_uuid() END,
                lease_expires_at = CASE
                    WHEN %s = 'leased' THEN NOW() + INTERVAL '1 minute'
                END
            WHERE uuid = %s
            """,
            (page_status, page_status, page_status, provider_uuid),
        )
    db.commit()

    with pytest.raises(
        messenger_exceptions.ExternalProjectionMoveConflictError
    ) as error:
        _run_database_operation(
            lambda session: messenger_controllers._move_projection_rows(
                session,
                stream_uuid,
                old_project_uuid,
                new_project_uuid,
                bridge_instance_uuid=bridge_uuid,
                external_account_uuid=account_uuid,
            )
        )
    assert error.value.as_dict()["code"] == 409
    assert error.value.as_dict()["retryable"] is True
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id, status, attempt
            FROM m_external_provider_operations_v1
            WHERE uuid = %s
            """,
            (provider_uuid,),
        )
        assert cursor.fetchone() == (old_project_uuid, page_status, 1)
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
    db.commit()


def test_projection_move_rebinds_failed_provider_read_snapshot_retry(
    api,
    db,
    monkeypatch,
):
    conftest.seed_workspace_user(db, api.user_uuid, f"user-{api.user_uuid}")
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    old_project_uuid = sys_uuid.UUID(str(api.project_id))
    new_project_uuid = sys_uuid.uuid4()
    capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(capability)),
        )
    db.commit()

    public_uuid = sys_uuid.uuid4()
    operation = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=public_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=old_project_uuid,
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=stream_uuid,
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql=(
                "SELECT %s::uuid AS uuid, NOW() AS created_at, "
                "1::bigint AS ingest_sequence"
            ),
            candidate_values=(message_uuid,),
            use_candidate_chunks=None,
        )
    )
    assert operation is not None
    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )
    lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert len(lease) == 1
    emitted_projects = []
    monkeypatch.setattr(
        provider_data,
        "_emit_operation_event",
        lambda session, operation_uuid, project_id, event_kind: emitted_projects.append(
            project_id
        ),
    )
    failed = _run_database_operation(
        lambda session: provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": lease[0]["provider_operation_uuid"],
                "lease_uuid": lease[0]["lease_uuid"],
                "status": "failed",
                "safe_error": "provider rejected the read page",
            },
        )
    )
    assert failed["status"] == "applied"
    assert emitted_projects == [old_project_uuid]

    _run_database_operation(
        lambda session: provider_data.rebind_provider_read_lane_project(
            session,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            causal_lane=stream_uuid,
            old_project_id=old_project_uuid,
            new_project_id=new_project_uuid,
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT snapshot.project_id, page.project_id, page.status
            FROM m_external_provider_read_snapshots_v1 AS snapshot
            JOIN m_external_provider_operations_v1 AS page
              ON page.external_operation_uuid = snapshot.external_operation_uuid
            WHERE snapshot.external_operation_uuid = %s
            """,
            (public_uuid,),
        )
        assert cursor.fetchone() == (
            new_project_uuid,
            new_project_uuid,
            "failed",
        )

    queued = _run_database_operation(
        lambda session: provider_data.retry_provider_operation(
            session,
            external_operation_uuid=public_uuid,
            next_attempt=2,
        )
    )
    assert queued["project_id"] == new_project_uuid
    emitted_projects.clear()
    retry_lease = _run_database_operation(
        lambda session: provider_data.lease_provider_operations(
            session,
            identity,
            request_uuid=sys_uuid.uuid4(),
            limit=1,
            lease_seconds=30,
        )
    )["operations"]
    assert len(retry_lease) == 1
    assert retry_lease[0]["project_id"] == str(new_project_uuid)
    succeeded = _run_database_operation(
        lambda session: provider_data.report_provider_result(
            session,
            identity,
            {
                "result_uuid": str(sys_uuid.uuid4()),
                "provider_operation_uuid": retry_lease[0]["provider_operation_uuid"],
                "lease_uuid": retry_lease[0]["lease_uuid"],
                "status": "succeeded",
                "safe_error": None,
            },
        )
    )
    assert succeeded["status"] == "applied"
    assert emitted_projects == [new_project_uuid, new_project_uuid]
    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
    db.commit()


def test_provider_read_page_precedes_a_later_snapshot_in_the_same_lane(api, db):
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Interleaved provider reads"
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    message_uuids = []
    for content in ("first read snapshot", "second read snapshot"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    capabilities = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(capabilities), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(capabilities)),
        )
    db.commit()

    snapshot_uuids = []
    for message_uuid in message_uuids:
        snapshot_uuid = sys_uuid.uuid4()
        snapshot_uuids.append(snapshot_uuid)
        queued = _run_database_operation(
            lambda session, current_snapshot=snapshot_uuid, current_message=message_uuid: (
                provider_data.enqueue_provider_read_operation(
                    session,
                    operation_uuid=current_snapshot,
                    bridge_instance_uuid=bridge_uuid,
                    external_account_uuid=account_uuid,
                    project_id=sys_uuid.UUID(str(api.project_id)),
                    owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
                    target_type="stream",
                    target_uuid=sys_uuid.UUID(str(stream_uuid)),
                    payload={
                        "stream_uuid": str(stream_uuid),
                        "topic_uuid": None,
                        "reader_uuid": str(api.user_uuid),
                        "read": True,
                    },
                    candidate_sql="""
                    SELECT uuid, created_at, ingest_sequence
                    FROM m_workspace_messages
                    WHERE project_id = %s AND uuid = %s
                """,
                    candidate_values=(api.project_id, current_message),
                )
            )
        )
        assert queued is not None

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )

    def lease_one():
        return _run_database_operation(
            lambda session: provider_data.lease_provider_operations(
                session,
                identity,
                request_uuid=sys_uuid.uuid4(),
                limit=1,
                lease_seconds=30,
            )
        )["operations"]

    def report(operation):
        return _run_database_operation(
            lambda session: provider_data.report_provider_result(
                session,
                identity,
                {
                    "result_uuid": str(sys_uuid.uuid4()),
                    "provider_operation_uuid": operation["provider_operation_uuid"],
                    "lease_uuid": operation["lease_uuid"],
                    "status": "succeeded",
                    "safe_error": None,
                },
            )
        )

    first_page = lease_one()
    assert len(first_page) == 1
    assert first_page[0]["payload"]["message_uuids"] == [str(message_uuids[0])]
    assert report(first_page[0])["status"] == "applied"

    second_page = lease_one()
    assert len(second_page) == 1
    assert second_page[0]["payload"]["message_uuids"] == [str(message_uuids[1])]
    assert report(second_page[0])["status"] == "applied"

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*)
            FROM m_external_provider_read_snapshots_v1
            WHERE external_operation_uuid = ANY(%s::uuid[])
            """,
            (snapshot_uuids,),
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            "DELETE FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
    db.commit()


def test_provider_read_snapshot_fifo_is_scoped_to_stream_lane(api, db, monkeypatch):
    first_stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Provider lane first"
    )
    first_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        first_stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    second_stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Provider lane second"
    )
    message_response = api.post(
        MESSAGES,
        json={
            "stream_uuid": first_stream_uuid,
            "topic_uuid": first_topic_uuid,
            "payload": {"kind": "markdown", "content": "legacy sequence"},
        },
    )
    assert message_response.status_code == 201, message_response.text
    message_uuid = sys_uuid.UUID(message_response.json()["uuid"])

    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    read_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
        provider_data.PROVIDER_READ_PAGING_CAPABILITY: {
            "available": True,
            "revision": 1,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, %s::jsonb
            )
            """,
            (account_uuid, api.user_uuid, json.dumps(read_capability)),
        )
    db.commit()

    snapshot_uuid = sys_uuid.uuid4()
    snapshot = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=snapshot_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(first_stream_uuid)),
            payload={
                "stream_uuid": str(first_stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql="""
                SELECT uuid, created_at, ingest_sequence
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
            """,
            candidate_values=(api.project_id, message_uuid),
        )
    )
    assert snapshot is not None

    def queue_exact(stream_uuid, message_value):
        return _run_database_operation(
            lambda session: provider_data.enqueue_provider_operation_in_lane(
                session,
                operation_uuid=sys_uuid.uuid4(),
                bridge_instance_uuid=bridge_uuid,
                external_account_uuid=account_uuid,
                project_id=sys_uuid.UUID(str(api.project_id)),
                owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
                operation_kind="read_state.set",
                target_type="provider-read-test",
                target_uuid=None,
                payload={
                    "reader_uuid": str(api.user_uuid),
                    "read": True,
                    "message_uuids": [str(message_value)],
                },
                causal_lane=stream_uuid,
            )
        )[1]

    same_lane_provider_uuid = queue_exact(first_stream_uuid, sys_uuid.uuid4())
    other_lane_provider_uuid = queue_exact(second_stream_uuid, sys_uuid.uuid4())
    _unknown_lane_operation, unknown_lane_provider_uuid = _run_database_operation(
        lambda session: provider_data.enqueue_provider_operation(
            session,
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            operation_kind="read_state.set",
            target_type="provider-read-test",
            target_uuid=None,
            payload={
                "reader_uuid": str(api.user_uuid),
                "read": True,
                "message_uuids": [str(sys_uuid.uuid4())],
            },
        )
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT candidate_count, candidate_uuids, cursor_position
            FROM m_external_provider_read_candidate_packs_v1
            WHERE external_operation_uuid = %s
            """,
            (snapshot_uuid,),
        )
        assert cursor.fetchone() == (1, [message_uuid], 0)
        cursor.execute(
            """
            SELECT causal_lane
            FROM m_external_provider_operations_v1
            WHERE uuid = ANY(%s::uuid[])
            ORDER BY causal_lane
            """,
            ([same_lane_provider_uuid, other_lane_provider_uuid],),
        )
        assert {row[0] for row in cursor.fetchall()} == {
            sys_uuid.UUID(str(first_stream_uuid)),
            sys_uuid.UUID(str(second_stream_uuid)),
        }

    identity = types.SimpleNamespace(
        bridge_instance_uuid=bridge_uuid,
        provider_kind="zulip",
        identity_generation=1,
    )

    def lease(limit):
        return _run_database_operation(
            lambda session: provider_data.lease_provider_operations(
                session,
                identity,
                request_uuid=sys_uuid.uuid4(),
                limit=limit,
                lease_seconds=30,
            )
        )["operations"]

    paging_lease = lease(3)
    assert len(paging_lease) == 2
    assert any(
        operation["provider_operation_uuid"] == str(other_lane_provider_uuid)
        for operation in paging_lease
    )
    snapshot_page = next(
        operation
        for operation in paging_lease
        if operation["payload"]["message_uuids"] == [str(message_uuid)]
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_operations_v1
            WHERE external_operation_uuid = %s
            """,
            (snapshot_uuid,),
        )
        assert cursor.fetchone() == (1,)
        cursor.execute(
            """
            SELECT candidate_count, cursor_position
            FROM m_external_provider_read_candidate_packs_v1
            WHERE external_operation_uuid = %s
            """,
            (snapshot_uuid,),
        )
        assert cursor.fetchone() is None

    def report(operation, result_uuid=None):
        return _run_database_operation(
            lambda session: provider_data.report_provider_result(
                session,
                identity,
                {
                    "result_uuid": str(result_uuid or sys_uuid.uuid4()),
                    "provider_operation_uuid": operation["provider_operation_uuid"],
                    "lease_uuid": operation["lease_uuid"],
                    "status": "succeeded",
                    "safe_error": None,
                },
            )
        )

    other_lane_operation = next(
        operation
        for operation in paging_lease
        if operation["provider_operation_uuid"] == str(other_lane_provider_uuid)
    )
    assert report(other_lane_operation)["status"] == "applied"

    duplicate_result_uuid = sys_uuid.uuid4()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        duplicate_results = list(
            executor.map(
                lambda _index: report(snapshot_page, duplicate_result_uuid),
                range(2),
            )
        )
    assert {result["status"] for result in duplicate_results} == {
        "applied",
        "duplicate",
    }

    second_lease = lease(1)
    assert len(second_lease) == 1
    assert second_lease[0]["provider_operation_uuid"] == str(same_lane_provider_uuid)
    third_lease = lease(1)
    assert len(third_lease) == 1
    assert third_lease[0]["provider_operation_uuid"] == str(unknown_lane_provider_uuid)
    assert report(third_lease[0])["status"] == "applied"

    # A rolling paging-capability removal after the backend reads revision 1 is
    # still fenced
    # by the database trigger. Other lanes continue, and the lazy page becomes
    # deliverable again only after the persisted bridge contract returns to 2.
    rolling_snapshot_uuid = sys_uuid.uuid4()
    rolling_snapshot = _run_database_operation(
        lambda session: provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=rolling_snapshot_uuid,
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=sys_uuid.UUID(str(api.user_uuid)),
            target_type="stream",
            target_uuid=sys_uuid.UUID(str(first_stream_uuid)),
            payload={
                "stream_uuid": str(first_stream_uuid),
                "topic_uuid": None,
                "reader_uuid": str(api.user_uuid),
                "read": True,
            },
            candidate_sql="""
                SELECT uuid, created_at, ingest_sequence
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
            """,
            candidate_values=(api.project_id, message_uuid),
        )
    )
    assert rolling_snapshot is not None
    rolling_page = lease(1)[0]
    assert rolling_page["payload"]["message_uuids"] == [str(message_uuid)]
    later_lane_provider_uuid = queue_exact(second_stream_uuid, sys_uuid.uuid4())
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_provider_operations_v1
            SET lease_expires_at = NOW() - INTERVAL '1 second'
            WHERE uuid = %s
            """,
            (rolling_page["provider_operation_uuid"],),
        )
    db.commit()

    original_bridge_capabilities = provider_data._bridge_capabilities
    removed_paging_capability = {}

    def downgrade_after_capability_read(session, identity, now):
        capabilities = original_bridge_capabilities(session, identity, now)
        removed_paging_capability.update(
            read_capability.pop(
                provider_data.PROVIDER_READ_PAGING_CAPABILITY,
                {},
            )
        )
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_external_bridge_instances_v2
                SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
                WHERE uuid = %s
                """,
                (json.dumps(read_capability), bridge_uuid),
            )
        db.commit()
        return capabilities

    monkeypatch.setattr(
        provider_data,
        "_bridge_capabilities",
        downgrade_after_capability_read,
    )

    downgraded_lease = lease(1)
    monkeypatch.setattr(
        provider_data,
        "_bridge_capabilities",
        original_bridge_capabilities,
    )
    assert len(downgraded_lease) == 1
    assert downgraded_lease[0]["provider_operation_uuid"] == str(
        later_lane_provider_uuid
    )
    assert report(downgraded_lease[0])["status"] == "applied"

    read_capability[provider_data.PROVIDER_READ_PAGING_CAPABILITY] = (
        removed_paging_capability
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET capabilities = %s::jsonb, last_heartbeat_at = NOW()
            WHERE uuid = %s
            """,
            (json.dumps(read_capability), bridge_uuid),
        )
    db.commit()
    resumed_page = lease(1)
    assert len(resumed_page) == 1
    assert (
        resumed_page[0]["provider_operation_uuid"]
        == rolling_page["provider_operation_uuid"]
    )
    assert report(resumed_page[0])["status"] == "applied"


def test_compact_bulk_and_single_read_race_keeps_exact_topic_counter(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact bulk read race"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for content in ("single winner", "bulk remainder"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    bulk_waiting_for_project = threading.Event()
    release_bulk = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class CoordinatedBulkSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            if (
                not self._paused
                and isinstance(statement, str)
                and "pg_advisory_xact_lock" in statement
                and "workspace-read:" not in statement
            ):
                self._paused = True
                bulk_waiting_for_project.set()
                assert release_bulk.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_bulk_read():
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '5s'")
            return read_state.read_stream(
                CoordinatedBulkSession(session),
                api.project_id,
                reader_uuid,
                stream_uuid,
                collect_message_rows=False,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        bulk_future = executor.submit(run_bulk_read)
        assert bulk_waiting_for_project.wait(timeout=5)
        _run_database_operation(
            lambda session: messenger_dm_helpers.read_workspace_user_message(
                project_id=api.project_id,
                user_uuid=reader_uuid,
                message_uuid=message_uuids[0],
                session=session,
            )
        )
        release_bulk.set()
        bulk_result = bulk_future.result(timeout=10)
    assert bulk_result.changed is True

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone()[0] == 2
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 2


def test_compact_bulk_read_ignores_another_users_read_revision(api, db):
    bulk_reader_uuid = sys_uuid.uuid4()
    other_reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact independent readers"
    )
    for reader_uuid in (bulk_reader_uuid, other_reader_uuid):
        conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for content in ("first", "second"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    bulk_waiting_for_project = threading.Event()
    release_bulk = threading.Event()
    prepare_count = 0
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class CoordinatedBulkSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            nonlocal prepare_count
            if (
                isinstance(statement, str)
                and "WITH unread AS MATERIALIZED" in statement
            ):
                prepare_count += 1
            if (
                not self._paused
                and isinstance(statement, str)
                and "pg_advisory_xact_lock" in statement
                and "workspace-read:" not in statement
            ):
                self._paused = True
                bulk_waiting_for_project.set()
                assert release_bulk.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_bulk_read():
        with session_factory() as session:
            return read_state.read_stream(
                CoordinatedBulkSession(session),
                api.project_id,
                bulk_reader_uuid,
                stream_uuid,
                collect_message_rows=False,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        bulk_future = executor.submit(run_bulk_read)
        assert bulk_waiting_for_project.wait(timeout=5)
        _run_database_operation(
            lambda session: messenger_dm_helpers.read_workspace_user_message(
                project_id=api.project_id,
                user_uuid=other_reader_uuid,
                message_uuid=message_uuids[0],
                session=session,
            )
        )
        release_bulk.set()
        assert bulk_future.result(timeout=10).changed is True

    assert prepare_count == 1
    assert _workspace_message_read_states(
        db,
        api.project_id,
        bulk_reader_uuid,
        message_uuids,
    ) == {str(message_uuid): True for message_uuid in message_uuids}


def test_compact_provider_bulk_read_rebuilds_after_concurrent_message(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db,
        api.project_id,
        api.user_uuid,
        "Bulk concurrent create",
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    first = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": topic_uuid,
            "payload": {"kind": "markdown", "content": "before snapshot"},
        },
    )
    assert first.status_code == 201, first.text
    first_uuid = sys_uuid.UUID(first.json()["uuid"])
    bridge_uuid, _key_uuid, _private_key = _seed_zulip_bridge_target(db)
    _enable_zulip_policy(db)
    account_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', '{}'::jsonb,
                FALSE, 'live', TRUE, '{}'::jsonb
            )
            """,
            (account_uuid, reader_uuid),
        )
    db.commit()

    bulk_waiting_for_project = threading.Event()
    release_bulk = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    def queue_provider_snapshot(
        session,
        candidate_sql,
        candidate_values,
        candidate_chunks,
    ):
        provider_data.enqueue_provider_read_operation(
            session,
            operation_uuid=sys_uuid.uuid4(),
            bridge_instance_uuid=bridge_uuid,
            external_account_uuid=account_uuid,
            project_id=sys_uuid.UUID(str(api.project_id)),
            owner_user_uuid=reader_uuid,
            target_type="topic",
            target_uuid=sys_uuid.UUID(str(topic_uuid)),
            payload={
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(reader_uuid),
                "read": True,
            },
            candidate_sql=candidate_sql,
            candidate_values=candidate_values,
            candidate_chunks=candidate_chunks,
            use_candidate_chunks=True,
        )

    class CoordinatedBulkSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            if (
                not self._paused
                and isinstance(statement, str)
                and "pg_advisory_xact_lock(hashtextextended(%s::text" in statement
            ):
                self._paused = True
                bulk_waiting_for_project.set()
                assert release_bulk.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_bulk_read():
        with session_factory() as session:
            return read_state.read_topic(
                CoordinatedBulkSession(session),
                api.project_id,
                reader_uuid,
                stream_uuid,
                topic_uuid,
                collect_message_rows=False,
                message_uuid_snapshot_callback=queue_provider_snapshot,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        bulk_future = executor.submit(run_bulk_read)
        assert bulk_waiting_for_project.wait(timeout=5)
        second = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {
                    "kind": "markdown",
                    "content": "after snapshot",
                },
            },
        )
        assert second.status_code == 201, second.text
        second_uuid = sys_uuid.UUID(second.json()["uuid"])
        release_bulk.set()
        assert bulk_future.result(timeout=5).changed is True

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        [first_uuid, second_uuid],
    ) == {
        str(first_uuid): True,
        str(second_uuid): True,
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_external_provider_operations_v1
            WHERE external_account_uuid = %s
              AND operation_kind = 'read_state.set'
            """,
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT array_agg(message.uuid ORDER BY chunk.chunk_number, bit_offset)
            FROM m_external_provider_read_snapshots_v1 AS snapshot
            JOIN m_external_provider_read_candidate_chunks_v1 AS chunk
              ON chunk.external_operation_uuid = snapshot.external_operation_uuid
            CROSS JOIN LATERAL generate_series(
                0, 4095
            ) AS bit_offset
            JOIN m_workspace_messages AS message
              ON message.project_id = snapshot.project_id
             AND message.ingest_sequence =
                    chunk.chunk_number * 4096 + bit_offset
            WHERE snapshot.external_account_uuid = %s
              AND get_bit(chunk.candidate_bits, bit_offset) = 1
            """,
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == [first_uuid, second_uuid]


@pytest.mark.parametrize(
    "structural_change",
    ["move", "delete", "purge", "stream_delete"],
)
def test_compact_bulk_read_retries_after_message_structure_changes(
    api,
    db,
    structural_change,
):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, f"Bulk structure {structural_change}"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    source_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "source",
        is_default=True,
    )
    destination_topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "destination",
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    response = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream_uuid,
            "topic_uuid": source_topic_uuid,
            "payload": {"kind": "markdown", "content": structural_change},
        },
    )
    assert response.status_code == 201, response.text
    message_uuid = sys_uuid.UUID(response.json()["uuid"])
    external_account_uuid = sys_uuid.uuid4()
    if structural_change == "purge":
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE m_workspace_messages
                SET external_account_uuid = %s
                WHERE project_id = %s AND uuid = %s
                """,
                (external_account_uuid, api.project_id, message_uuid),
            )
        db.commit()

    bulk_waiting_for_project = threading.Event()
    release_bulk = threading.Event()
    structure_started = threading.Event()
    session_factory = ra_engines.engine_factory.get_engine().session_manager

    class CoordinatedBulkSession:
        def __init__(self, session):
            self._session = session
            self._paused = False

        def execute(self, statement, params=()):
            if (
                not self._paused
                and isinstance(statement, str)
                and "pg_advisory_xact_lock(hashtextextended(%s::text" in statement
            ):
                self._paused = True
                bulk_waiting_for_project.set()
                assert release_bulk.wait(timeout=5)
            return self._session.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self._session, name)

    def run_bulk_read():
        with session_factory() as session:
            session.execute("SET LOCAL statement_timeout = '5s'")
            return read_state.read_topic(
                CoordinatedBulkSession(session),
                api.project_id,
                reader_uuid,
                stream_uuid,
                source_topic_uuid,
                collect_message_rows=False,
            )

    def change_structure(session):
        structure_started.set()
        if structural_change == "stream_delete":
            messenger_controllers._delete_projection_stream_rows(
                session,
                project_id=api.project_id,
                stream_uuid=stream_uuid,
            )
            return
        if structural_change == "move":
            read_state.relocate_message(
                session,
                message_uuid,
                api.project_id,
                api.project_id,
                stream_uuid,
                destination_topic_uuid,
            )
            session.execute(
                """
                UPDATE m_workspace_messages
                SET topic_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND uuid = %s
                """,
                (destination_topic_uuid, api.project_id, message_uuid),
            )
            return
        if structural_change == "purge":
            read_state.purge_external_account_messages(
                session,
                api.project_id,
                stream_uuid,
                external_account_uuid,
            )
        else:
            read_state.clear_message_for_all_users(
                session,
                api.project_id,
                message_uuid,
            )
            read_state.record_message_deleted(
                session,
                api.project_id,
                source_topic_uuid,
                message_uuid,
            )
        session.execute(
            """
            DELETE FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (api.project_id, message_uuid),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        bulk_future = executor.submit(run_bulk_read)
        assert bulk_waiting_for_project.wait(timeout=5)
        structure_future = executor.submit(
            lambda: _run_database_operation(change_structure)
        )
        assert structure_started.wait(timeout=5)
        # Structural work is not blocked by the optimistic 500k-class scan.
        # It commits first and bumps the revision; bulk must discard its stale
        # masks after it acquires the project lock.
        structure_future.result(timeout=5)
        release_bulk.set()
        bulk_result = bulk_future.result(timeout=5)

    assert bulk_result.changed is False
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        remaining_read_bits = cursor.fetchone()[0]
        if structural_change == "move":
            assert remaining_read_bits == 0
            assert _workspace_message_read_states(
                db,
                api.project_id,
                reader_uuid,
                [message_uuid],
            ) == {str(message_uuid): False}
            cursor.execute(
                """
                SELECT read_count
                FROM m_workspace_user_topic_read_stats_v1
                WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
                """,
                (api.project_id, reader_uuid, destination_topic_uuid),
            )
            assert cursor.fetchone() is None
        else:
            assert remaining_read_bits == 0
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
                """,
                (api.project_id, message_uuid),
            )
            assert cursor.fetchone() == (0,)


def test_compact_account_message_purge_preserves_other_read_bits(api, db):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Compact account purge"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, read_state.PROJECT_MODE_COMPACT)
    message_uuids = []
    for content in ("first account", "second account"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))
    first_account_uuid = sys_uuid.uuid4()
    second_account_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_workspace_messages
            SET external_account_uuid = CASE uuid
                    WHEN %s THEN %s
                    ELSE %s
                END
            WHERE project_id = %s AND uuid = ANY(%s::uuid[])
            """,
            (
                message_uuids[0],
                first_account_uuid,
                second_account_uuid,
                api.project_id,
                message_uuids,
            ),
        )
    db.commit()
    _run_database_operation(
        lambda session: read_state.set_message_uuids_read(
            session,
            api.project_id,
            reader_uuid,
            message_uuids,
            True,
        )
    )

    def purge_first_account(session):
        read_state.purge_external_account_messages(
            session,
            api.project_id,
            stream_uuid,
            first_account_uuid,
        )
        session.execute(
            """
            DELETE FROM m_workspace_messages
            WHERE project_id = %s AND stream_uuid = %s
              AND external_account_uuid = %s
            """,
            (api.project_id, stream_uuid, first_account_uuid),
        )

    _run_database_operation(purge_first_account)

    assert _workspace_message_read_states(
        db,
        api.project_id,
        reader_uuid,
        [message_uuids[1]],
    ) == {str(message_uuids[1]): True}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_count
            FROM m_workspace_topic_message_stats_v1
            WHERE project_id = %s AND topic_uuid = %s
            """,
            (api.project_id, topic_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT read_count
            FROM m_workspace_user_topic_read_stats_v1
            WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
            """,
            (api.project_id, reader_uuid, topic_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT COALESCE(SUM(bit_count(read_bits)), 0)
            FROM m_workspace_user_read_chunks_v1
            WHERE user_uuid = %s
            """,
            (reader_uuid,),
        )
        assert cursor.fetchone()[0] == 1


@pytest.mark.parametrize(
    "mode",
    [read_state.PROJECT_MODE_COMPACT, read_state.PROJECT_MODE_ROLLBACK],
)
def test_compact_provider_read_state_uses_bitmaps_and_keeps_own_message_read(
    api, db, mode
):
    reader_uuid = sys_uuid.uuid4()
    stream_uuid = conftest.seed_user_stream(
        db, api.project_id, api.user_uuid, "Provider compact read state"
    )
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, reader_uuid)
    topic_uuid = conftest.seed_stream_topic(
        db,
        api.project_id,
        stream_uuid,
        api.user_uuid,
        "general",
        is_default=True,
    )
    _set_workspace_read_mode(db, api.project_id, mode)
    message_uuids = []
    for content in ("provider one", "provider two"):
        response = api.post(
            MESSAGES,
            json={
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": content},
            },
        )
        assert response.status_code == 201, response.text
        message_uuids.append(sys_uuid.UUID(response.json()["uuid"]))

    _run_database_operation(
        lambda session: provider_event_apply._sync_provider_read_state(
            session,
            sys_uuid.UUID(api.project_id),
            reader_uuid,
            sys_uuid.UUID(stream_uuid),
            sys_uuid.UUID(topic_uuid),
            message_uuids,
            True,
        )
    )
    assert _workspace_message_read_states(
        db, api.project_id, reader_uuid, message_uuids
    ) == {str(message_uuid): True for message_uuid in message_uuids}
    _run_database_operation(
        lambda session: provider_event_apply._sync_provider_read_state(
            session,
            sys_uuid.UUID(api.project_id),
            reader_uuid,
            sys_uuid.UUID(stream_uuid),
            sys_uuid.UUID(topic_uuid),
            message_uuids,
            False,
        )
    )
    assert _workspace_message_read_states(
        db, api.project_id, reader_uuid, message_uuids
    ) == {str(message_uuid): False for message_uuid in message_uuids}
    _run_database_operation(
        lambda session: provider_event_apply._sync_provider_read_state(
            session,
            sys_uuid.UUID(api.project_id),
            sys_uuid.UUID(api.user_uuid),
            sys_uuid.UUID(stream_uuid),
            sys_uuid.UUID(topic_uuid),
            message_uuids,
            False,
        )
    )
    assert _workspace_message_read_states(
        db, api.project_id, api.user_uuid, message_uuids
    ) == {str(message_uuid): True for message_uuid in message_uuids}
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_message_flags
            WHERE project_id = %s
            """,
            (api.project_id,),
        )
        assert cursor.fetchone()[0] == (
            0 if mode == read_state.PROJECT_MODE_COMPACT else 4
        )
