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

import http.server
import io
import datetime
import json
import threading
import uuid as sys_uuid

import pytest
from restalchemy.common import contexts as ra_contexts

from workspace.messenger_api import file_storage
from workspace.messenger_api import topic_summarization
from workspace.services.messenger_workers import agents as messenger_worker_agents
from workspace.tests.integration import conftest


V1 = "/v1"
STREAMS = f"{V1}/messenger/streams/"
STREAM_BINDINGS = f"{V1}/messenger/stream_bindings/"
FOLDERS = f"{V1}/messenger/folders/"
FILES = f"{V1}/messenger/files/"
FOLDER_ITEMS = f"{V1}/messenger/folder_items/"
STREAM_TOPICS = f"{V1}/messenger/stream_topics/"
MESSAGES = f"{V1}/messenger/messages/"
DRAFTS = f"{V1}/messenger/drafts/"
MESSAGE_REACTIONS = f"{V1}/messenger/message_reactions/"
EVENTS = f"{V1}/events/"
EPOCH = f"{V1}/epoch/"
USERS = f"{V1}/users/"
EXTERNAL_ACCOUNTS = f"{V1}/messenger/external_accounts/"
EXTERNAL_OPERATIONS = f"{V1}/messenger/external_operations/"
EXTERNAL_CHATS = f"{V1}/messenger/external_chats/"
EXTERNAL_PROVIDER_POLICIES = f"{V1}/messenger/external_provider_policies/"
EXTERNAL_PROVIDER_HEALTH = f"{V1}/messenger/external_provider_health/"
TOPIC_SUMMARY_ENDPOINTS = f"{V1}/messenger/topic_summary_endpoints/"
TOPIC_SUMMARY_SETTINGS = f"{V1}/messenger/topic_summary_settings/"
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


@pytest.mark.isolated_schema
@pytest.mark.parametrize(
    ("global_enabled", "project_enabled", "endpoint_enabled"),
    ((False, True, True), (True, False, True), (True, True, False)),
)
def test_disabled_summary_does_not_scan_topics_or_create_jobs(
    api, db, global_enabled, project_enabled, endpoint_enabled
):
    endpoint = api.post(
        TOPIC_SUMMARY_ENDPOINTS,
        permissions=TOPIC_SUMMARY_ENDPOINT_MANAGE,
        json={
            "uuid": str(sys_uuid.uuid4()),
            "name": "disabled-summary",
            "base_url": "https://llm.example.invalid/v1",
            "model": "summary-model",
            "api_key": "test-secret",
            "enabled": endpoint_enabled,
        },
    )
    assert endpoint.status_code == 201, endpoint.text
    settings = api.put(
        f"{TOPIC_SUMMARY_SETTINGS}{api.project_id}",
        permissions=TOPIC_SUMMARY_SETTINGS_MANAGE,
        json={"global_enabled": global_enabled, "project_enabled": project_enabled},
    )
    assert settings.status_code == 200, settings.text
    stream = conftest.seed_user_stream(db, api.project_id, api.user_uuid, "Disabled")
    topic = conftest.seed_stream_topic(
        db, api.project_id, stream, api.user_uuid, "Work"
    )
    message = api.post(
        MESSAGES,
        json={
            "stream_uuid": stream,
            "topic_uuid": topic,
            "payload": {"kind": "markdown", "content": "Pending summary."},
        },
    )
    assert message.status_code == 201, message.text
    statements = []

    class Probe:
        def __init__(self, session):
            self.session = session

        def execute(self, statement, values=()):
            statements.append(statement)
            return self.session.execute(statement, values)

    claimed = _run_database_operation(
        lambda session: topic_summarization.claim_summary_work(
            Probe(session),
            now=datetime.datetime.now(datetime.timezone.utc),
            key_material="integration-test-topic-summary-key",
            topic_claim_seconds=60,
            endpoint_claim_seconds=60,
        )
    )
    assert claimed is None
    assert len(statements) == 1
    assert "messenger_topics" not in statements[0]
    assert "messenger_topic_messages_v1" not in statements[0]
    assert db.execute(
        "SELECT count(*) FROM m_workspace_topic_summary_jobs"
    ).fetchone() == (0,)


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
                "UPDATE messenger_topics "
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
    poll_clock = [0.0]
    monkeypatch.setattr(messenger_worker_agents.time, "monotonic", lambda: poll_clock[0])
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
        poll_clock[0] += 5
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

        poll_clock[0] += 5
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

        poll_clock[0] += 5
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
                "failed",
                3,
                sys_uuid.UUID(old_message.json()["uuid"]),
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

        poll_clock[0] += 5
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
