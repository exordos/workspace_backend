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
import importlib.util
import io
import concurrent.futures
import base64
import datetime
import json
import threading
import uuid as sys_uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.x509.oid import NameOID
from restalchemy.common import contexts as ra_contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import sessions as ra_sessions
from oslo_config import cfg

from workspace.common import external_bridge_opts
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.api import controllers as messenger_controllers
from workspace.messenger_api.dm import helpers as messenger_dm_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
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


def _run_database_operation(callback):
    with ra_contexts.Context().session_manager() as session:
        return callback(session)


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


def test_zb_account_001_external_account_crud_is_owner_scoped_and_write_only(
    api,
    db,
    tmp_path,
):
    _enable_zulip_policy(db)
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
        create = api.post(
            EXTERNAL_ACCOUNTS,
            json={
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
            },
        )
        assert create.status_code == 201, create.text
        account = create.json()
        assert account["uuid"] == str(account_uuid)
        assert account["credential_present"] is True
        assert "api_key" not in account["settings"]
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
        )
        assert duplicate.status_code == 409, duplicate.text

        another_user = sys_uuid.uuid4()
        foreign_list = api.get(EXTERNAL_ACCOUNTS, user=another_user)
        assert foreign_list.status_code == 200, foreign_list.text
        assert foreign_list.json() == []
        foreign_get = api.get(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}",
            user=another_user,
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
        missing_etag = api.post(reconnect_path, json=reconnect_body)
        assert missing_etag.status_code == 428, missing_etag.text
        reconnect = api.post(
            reconnect_path,
            json=reconnect_body,
            headers={"If-Match": '"1"'},
        )
        assert reconnect.status_code == 200, reconnect.text
        assert reconnect.headers["ETag"] == '"2"'
        assert "api_key" not in reconnect.text

        disconnect = api.post(
            f"{EXTERNAL_ACCOUNTS}{account_uuid}/actions/disconnect/invoke"
        )
        assert disconnect.status_code == 200, disconnect.text
        assert disconnect.json()["status"] == "disconnected"

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

        deleted = api.delete(f"{EXTERNAL_ACCOUNTS}{account_uuid}")
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
        created = api.post(EXTERNAL_ACCOUNTS, json=payload)
        assert created.status_code == 201, created.text

        reached = api.post(
            EXTERNAL_ACCOUNTS,
            json={**payload, "uuid": str(sys_uuid.uuid4())},
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
        )
        assert disabled.status_code == 403, disabled.text
    finally:
        cfg.CONF.clear_override("realm_uuid", group=external_bridge_opts.DOMAIN)


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
                        "server_url": "https://zulip.example.invalid",
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


def test_user_presence_action_updates_current_user_presence(api, db):
    username = f"user-{api.user_uuid}"
    event_recipient_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, api.user_uuid, username)
    conftest.seed_workspace_user(
        db,
        event_recipient_uuid,
        f"user-{event_recipient_uuid}",
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
            FROM m_workspace_events
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
        files={"file": ("avatar.png", io.BytesIO(data), "image/png")},
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
            SELECT count(*), max(last_ping_at)
            FROM m_workspace_events AS events
            JOIN m_workspace_users AS users
                ON users.uuid = %s
            WHERE events.project_id = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (str(api.user_uuid), api.project_id, str(api.user_uuid)),
        )
        first_event_count, first_ping_at = cur.fetchone()

    resp = heartbeat_api.post(
        f"{USERS}{api.user_uuid}/actions/presence/invoke",
        json={"status": "idle"},
    )
    assert resp.status_code == 200, resp.text

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), max(last_ping_at)
            FROM m_workspace_events AS events
            JOIN m_workspace_users AS users
                ON users.uuid = %s
            WHERE events.project_id = %s
                AND events.payload->>'kind' = 'user.updated'
                AND events.payload->>'uuid' = %s
            """,
            (str(api.user_uuid), api.project_id, str(api.user_uuid)),
        )
        second_event_count, second_ping_at = cur.fetchone()

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
    conftest.seed_user_stream(db, api.project_id, api.user_uuid, "status-team")

    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE m_workspace_users
            SET status = 'active',
                last_ping_at = NOW() - INTERVAL '2 minutes'
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
            FROM m_workspace_events
            WHERE project_id = %s
                AND payload->>'kind' = 'user.updated'
                AND payload->>'uuid' = %s
            ORDER BY user_uuid
            """,
            (api.project_id, str(user_uuid)),
        )
        event_rows = cur.fetchall()
    event_recipient_uuids = {str(row[0]) for row in event_rows}
    assert str(user_uuid) in event_recipient_uuids
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
            FROM m_workspace_events
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

    resp = api.get(FILES)
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(FILES, user=stream_user)
    assert resp.status_code == 200, resp.text
    assert [item["uuid"] for item in resp.json()] == [file_uuid]

    resp = api.get(f"{FILES}{file_uuid}", user=stream_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["uuid"] == file_uuid

    resp = api.get(FILES, user=outsider_user)
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

    response = api.get(FILES)
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

    resp = api.post(
        FILES,
        data={"stream_uuid": stream_uuid},
        files={"file": ("upload.txt", io.BytesIO(data), "text/plain")},
    )
    assert resp.status_code in (200, 201), resp.text
    file = resp.json()

    path = file_storage.get_workspace_file_path(
        file_uuid=file["uuid"],
        storage_path=tmp_path,
    )
    assert path.read_bytes() == data
    assert file["name"] == "upload.txt"
    assert "storage_type" not in file
    assert "storage_id" not in file
    assert "storage_object_id" not in file
    resp = api.get(f"{FILES}{file['uuid']}/actions/download")
    assert resp.status_code == 200, resp.text
    assert resp.content == data
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert 'filename="upload.txt"' in resp.headers["Content-Disposition"]

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

    assert second_stream["uuid"] == first_stream["uuid"]
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


def test_stream_binding_create_notifies_added_user(api, db):
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
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT payload
                FROM m_workspace_events
                WHERE project_id = %s
                    AND user_uuid = %s
                ORDER BY epoch_version
                """,
                (api.project_id, target_uuid),
            )
            events = [row[0] for row in cur.fetchall()]

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
        str(api.user_uuid): "unmute",
        str(other_user): "default",
    }
    assert [payload["notification_mode"] for _, payload in event_rows] == [
        "follow",
        "unmute",
    ]


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


def test_message_create_writes_flags_and_visible_events(api, workspace_api, db):
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
    assert message["read"] is True
    assert message["is_own"] is True
    assert message["reactions"] == {}

    other_message_resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert other_message_resp.status_code == 200, other_message_resp.text
    other_message = other_message_resp.json()
    assert other_message["read"] is False
    assert other_message["is_own"] is False
    assert other_message["reactions"] == {}

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
    assert author_payload["uuid"] == message_uuid
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
    } == message
    assert {
        key: value for key, value in packed_other_payload.items() if key != "kind"
    } == other_message

    author_resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert author_resp.status_code == 200, author_resp.text
    author_events = author_resp.json()
    assert len(author_events) == 1
    event = author_events[0]
    assert event["project_id"] == str(api.project_id)
    assert event["user_uuid"] == str(api.user_uuid)
    assert event["payload"]["kind"] == "message.created"
    assert event["payload"]["uuid"] == message_uuid
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
    assert other_event["payload"]["uuid"] == message_uuid
    assert other_event["payload"]["kind"] == "message.created"
    assert other_event["payload"]["user_uuid"] == str(other_user)
    assert other_event["payload"]["project_id"] == str(api.project_id)
    assert other_event["payload"]["read"] is False
    assert other_event["payload"]["is_own"] is False
    assert other_event["payload"]["reactions"] == {}
    assert other_events[1]["payload"]["last_message_uuid"] == message_uuid
    assert other_events[2]["payload"]["last_message_uuid"] == message_uuid

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
    message_uuid = resp.json()["uuid"]

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
            FROM m_workspace_user_message_flags
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
    assert read_events[0]["uuid"] == message_uuid
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
    assert resp.json()["payload"]["content"] == "edited version"

    resp = api.get(f"{MESSAGES}{message_uuid}", user=other_user)
    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"]["content"] == "edited version"

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
    assert all(row[1]["uuid"] == message_uuid for row in delete_rows)
    assert all(row[1]["stream_uuid"] == stream_uuid for row in delete_rows)
    assert all(row[1]["topic_uuid"] == topic_uuid for row in delete_rows)


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
        payload["uuid"] == message_uuid for _, _, _, payload in message_event_rows
    )
    for index, expected_reactions in enumerate(expected_reaction_snapshots):
        group = message_event_rows[index * 2 : index * 2 + 2]
        assert {user_uuid for _, _, user_uuid, _ in group} == expected_event_users
        assert all(
            payload["reactions"] == expected_reactions for _, _, _, payload in group
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
        assert payload["message_uuid"] == message_uuid
        assert payload["user_uuid"] == expected_user_uuid
        assert payload["emoji_name"] == expected_emoji
        assert payload["source_name"] == "native"
        assert payload["source"]["kind"] == "native"
        if expected_action == "updated":
            assert payload["old_message_uuid"] == message_uuid
            assert payload["old_emoji_name"] == "thumbs_up"
            assert payload["old_source_name"] == "native"
            assert payload["old_source"]["kind"] == "native"
        else:
            assert "old_message_uuid" not in payload
            assert "old_emoji_name" not in payload


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
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT uuid, read
                FROM m_workspace_user_message_flags
                WHERE project_id = %s
                    AND user_uuid = %s
                    AND uuid IN (%s, %s, %s, %s)
                """,
                (api.project_id, str(other_user), *message_uuids),
            )
            return {str(uuid): read for uuid, read in cur.fetchall()}

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
            (api.project_id, message_uuid),
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
    assert events[0]["payload"]["uuid"] == str(message_uuid)


def test_zulip_message_flag_sync_keeps_author_read(api, db):
    other_user = sys_uuid.uuid4()
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
    conftest.seed_user_stream_binding(db, api.project_id, stream_uuid, other_user)
    with db.cursor() as cur:
        for user_uuid in (api.user_uuid, other_user):
            external_account_uuid = sys_uuid.uuid4()
            cur.execute(
                """
                INSERT INTO m_external_accounts_v2
                    (uuid, owner_user_uuid, provider, settings,
                     credential_present, status, live_ready)
                VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE)
                """,
                (
                    str(external_account_uuid),
                    str(user_uuid),
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
                    str(external_account_uuid),
                    str(user_uuid),
                    f"chat-{user_uuid}",
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
        )
        assert message.read is True
        return messenger_dm_helpers.get_workspace_user_message(
            project_id=sys_uuid.UUID(api.project_id),
            user_uuid=other_user,
            message_uuid=message_uuid,
        )

    other_message = _run_database_operation(create_and_sync_flags)
    assert other_message.read is False

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

    resp = workspace_api.get(EVENTS, params={"page_limit": 100})
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert [event["payload"]["uuid"] for event in events] == message_uuids
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
