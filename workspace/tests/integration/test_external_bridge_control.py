# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import contextlib
import hashlib
import json
from pathlib import Path
import uuid as sys_uuid

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage import exceptions as storage_exc
from restalchemy.storage.sql import engines

from workspace.external_bridge_control import file_repository
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import sql_state
from workspace.messenger_api.dm import external_models
from workspace.messenger_api.dm import helpers as messenger_helpers
from workspace.messenger_api.dm import message_payloads
from workspace.messenger_api.dm import models as messenger_models
from workspace.messenger_api import file_storage
from workspace.messenger_mail import external_bridge_codec
from workspace.messenger_mail import external_bridge_data_plane
from workspace.messenger_mail import protocol as mail_protocol
from workspace.tests.integration import conftest


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _identity(instance_uuid, realm_uuid):
    return pki.BridgeIdentity(
        realm_uuid=realm_uuid,
        provider_kind="zulip",
        bridge_instance_uuid=instance_uuid,
        identity_generation=1,
        uri_san="test",
    )


def _consume_ingress(session_factory, runtime_factory, **kwargs):
    with session_factory() as session:
        return external_bridge_data_plane.consume_ingress(
            session,
            runtime_factory,
            **kwargs,
        )


def _request_call(callable_, *args, **kwargs):
    with contexts.Context().session_manager():
        return callable_(*args, **kwargs)


def test_external_chat_assignment_producer_matches_complete_shared_fixture(
    _database,
    db,
):
    expected = json.loads(
        (FIXTURES / "external_bridge_complete_assignment.json").read_text(
            encoding="utf-8"
        )
    )
    account_uuid = sys_uuid.UUID(expected["external_account_uuid"])
    chat_uuid = sys_uuid.UUID(expected["uuid"])
    project_uuid = sys_uuid.UUID(expected["project_id"])
    stream = expected["workspace_projection"]["stream"]
    stream_uuid = sys_uuid.UUID(stream["uuid"])
    participants = expected["workspace_projection"]["participants"]
    topics = expected["workspace_projection"]["topics"]
    owner_uuid = sys_uuid.UUID(participants[0]["identity_uuid"])
    conftest.seed_workspace_user(db, owner_uuid, "assignment-owner")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_streams (
                uuid, name, description, private, source_name, source,
                user_uuid, project_id
            ) VALUES (%s, %s, %s, %s, 'zulip', '{"kind":"zulip"}'::jsonb,
                      %s, %s)
            """,
            (
                stream_uuid,
                stream["name"],
                stream["description"],
                stream["private"],
                owner_uuid,
                project_uuid,
            ),
        )
        for topic in topics:
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_topics (
                    uuid, project_id, name, stream_uuid, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, NOW(), NOW())
                """,
                (
                    topic["topic_uuid"],
                    project_uuid,
                    topic["name"],
                    stream_uuid,
                ),
            )
        cursor.execute(
            """
            UPDATE m_workspace_streams SET default_topic_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (stream["default_topic_uuid"], project_uuid, stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb, TRUE, 'live', TRUE)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                history_depth, projection_stream_uuid, status, revision
            ) VALUES (
                %s, %s, %s, 'zulip', %s, %s::jsonb, %s, TRUE, %s,
                %s, %s, 'live', %s
            )
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                expected["provider_chat"]["provider_chat_key"],
                sql_state._json(
                    {
                        "kind": "zulip",
                        "chat_type": "channel",
                        "description": stream["description"],
                        "private": stream["private"],
                        "participants": participants,
                        "topics": topics,
                    }
                ),
                stream["name"],
                project_uuid,
                expected["history_depth"],
                stream_uuid,
                expected["generation"],
            ),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        chat = external_models.ExternalChat.objects.get_one(
            filters={"uuid": dm_filters.EQ(chat_uuid)},
            session=session,
        )
        actual = sql_state.external_chat_assignment_desired(chat, session=session)

    assert actual == expected


def test_sql_control_state_feed_snapshot_and_encryption_target(_database, db):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status)
            VALUES (%s, 'zulip', 1, 'active')
            """,
            (instance_uuid,),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    identity = _identity(instance_uuid, realm_uuid)
    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    cursor = _request_call(repository.initial_cursor, identity)
    resource_uuid = sys_uuid.uuid4()
    resource = {
        "resource_type": "custom_ca_bundle",
        "uuid": str(resource_uuid),
        "generation": 1,
        "name": "provider-ca",
        "pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    }
    with session_factory() as session:
        sql_state.append_upsert(
            session,
            instance_uuid,
            "zulip",
            resource,
        )
        sql_state.persist_encryption_target(
            session,
            identity,
            {
                "key_uuid": str(sys_uuid.uuid4()),
                "public_key": "X25519-public-key",
            },
        )

    batch = _request_call(repository.changes, identity, cursor)
    assert batch["control_schema_version"] == "v1"
    assert [item["resource_uuid"] for item in batch["changes"]] == [str(resource_uuid)]
    snapshot, created = _request_call(
        repository.create_snapshot, identity, sys_uuid.uuid4()
    )
    assert created is True
    page = _request_call(repository.snapshot_page, identity, snapshot["snapshot_token"])
    assert page["resources"] == [{**resource, "required_capabilities": {}}]
    target = None
    with session_factory() as session:
        target = sql_state.active_encryption_target("zulip", session)
    assert target["bridge_instance_uuid"] == str(instance_uuid)
    assert target["identity_generation"] == 1


def test_bridge_bootstrap_creates_parent_and_authorization_tracks_current_state(
    _database, db
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    session_factory = engines.engine_factory.get_engine().session_manager
    identity = _identity(instance_uuid, realm_uuid)
    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)

    with session_factory() as session:
        sql_state.ensure_bridge_instance(session, instance_uuid, "zulip", 1)
        sql_state.persist_encryption_target(
            session,
            identity,
            {
                "key_uuid": str(sys_uuid.uuid4()),
                "public_key": "X25519-public-key",
            },
        )
        session.execute(
            "UPDATE m_external_bridge_instances_v2 SET status = 'active' WHERE uuid = %s",
            (instance_uuid,),
        )

    assert _request_call(repository.authorize_identity, identity)["status"] == "active"
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_external_bridge_instances_v2 SET status = 'suspended' WHERE uuid = %s",
            (instance_uuid,),
        )
    with pytest.raises(sql_state.state.BridgeForbiddenError):
        _request_call(repository.authorize_identity, identity)

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET status = 'active', identity_generation = 2
            WHERE uuid = %s
            """,
            (instance_uuid,),
        )
    with pytest.raises(sql_state.state.BridgeForbiddenError):
        _request_call(repository.authorize_identity, identity)


def test_observed_account_report_reconciles_snapshot_and_stale_report_cannot_regress(
    _database, db
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    conftest.seed_user_stream(db, project_uuid, owner_uuid, "Observed account")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status)
            VALUES (%s, 'zulip', 1, 'active')
            """,
            (instance_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                desired_generation, status
            ) VALUES (%s, %s, 'zulip', %s::jsonb, 1, 'connecting')
            """,
            (
                account_uuid,
                owner_uuid,
                '{"kind":"zulip","server_url":"https://zulip.example.test",'
                f'"default_project_id":"{project_uuid}"}}',
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2
                (uuid, external_account_uuid, key_version, envelope)
            VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                sql_state._json(
                    {"associated_data": {"bridge_instance_uuid": str(instance_uuid)}}
                ),
            ),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    identity = _identity(instance_uuid, realm_uuid)
    heartbeat = _request_call(
        repository.heartbeat,
        identity,
        {
            "heartbeat_uuid": str(sys_uuid.uuid4()),
            "client_timestamp": "2026-07-17T12:00:00Z",
            "image_version": "test",
            "provider_kind": "zulip",
            "capabilities": {"messenger.chat_catalog": {"revision": 1, "limits": {}}},
            "blocked_batch": None,
        },
    )
    assert "messenger.chat_catalog" in heartbeat["negotiated_capabilities"]
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT capabilities FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == {
            "messenger.chat_catalog": {
                "available": False,
                "revision": 1,
                "limits": {},
                "unavailable_reason": {
                    "code": "account_unavailable",
                    "message": (
                        "The external account is not ready for synchronization."
                    ),
                },
            }
        }
    desired = {
        "resource_type": "external_account",
        "uuid": str(account_uuid),
        "generation": 1,
    }
    with session_factory() as session:
        sql_state.append_upsert(session, instance_uuid, "zulip", desired)

    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "report_uuid": str(sys_uuid.uuid4()),
        "resource_type": "external_account",
        "resource_uuid": str(account_uuid),
        "observed_generation": 1,
        "status": "live_ready",
        "progress": {
            "phase": "live",
            "completed": 1,
            "total": 1,
            "last_progress_at": observed_at,
        },
        "safe_error": None,
        "observed_at": observed_at,
    }
    assert _request_call(repository.observed_reports, identity, [report])[
        "results"
    ] == [
        {
            "report_uuid": report["report_uuid"],
            "status": "applied",
            "safe_error": None,
        }
    ]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, live_ready, applied_generation, last_progress_at
            FROM m_external_accounts_v2 WHERE uuid = %s
            """,
            (account_uuid,),
        )
        account_state = cursor.fetchone()
        assert account_state[:3] == ("live", True, 1)
        assert account_state[3] is not None
        cursor.execute(
            """
            SELECT payload FROM m_workspace_events
                WHERE project_id = %s AND user_uuid = %s
                  AND object_type = 'external_account' AND action = 'updated'
                ORDER BY created_at DESC, epoch_version DESC
                LIMIT 1
            """,
            (project_uuid, owner_uuid),
        )
        snapshot = cursor.fetchone()[0]["snapshot"]
    assert snapshot["status"] == "live"
    assert snapshot["live_ready"] is True
    assert snapshot["applied_generation"] == 1

    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET desired_generation = 2, status = 'connecting', live_ready = FALSE
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    with session_factory() as session:
        sql_state.append_upsert(
            session, instance_uuid, "zulip", {**desired, "generation": 2}
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FROM m_workspace_events
            WHERE project_id = %s AND user_uuid = %s
              AND object_type = 'external_account' AND action = 'updated'
            """,
            (project_uuid, owner_uuid),
        )
        event_count_before_stale_report = cursor.fetchone()[0]
    stale = {
        **report,
        "report_uuid": str(sys_uuid.uuid4()),
        "status": "degraded",
        "safe_error": {
            "code": "provider_unavailable",
            "message": "Provider unavailable",
            "retryable": True,
        },
    }
    result = _request_call(repository.observed_reports, identity, [stale])["results"][0]
    assert result["status"] == "stale"
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT status, live_ready, applied_generation FROM m_external_accounts_v2 WHERE uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone() == ("connecting", False, 1)
        cursor.execute(
            """
            SELECT COUNT(*) FROM m_workspace_events
            WHERE project_id = %s AND user_uuid = %s
              AND object_type = 'external_account' AND action = 'updated'
            """,
            (project_uuid, owner_uuid),
        )
        assert cursor.fetchone()[0] == event_count_before_stale_report + 1
        cursor.execute(
            """
            SELECT payload->'snapshot'->'capabilities'
                       ->'messenger.chat_catalog'->>'available'
            FROM m_workspace_events
            WHERE project_id = %s AND user_uuid = %s
              AND object_type = 'external_account' AND action = 'updated'
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (project_uuid, owner_uuid),
        )
        assert cursor.fetchone() == ("false",)


def test_observed_chat_catalog_is_owned_idempotent_and_drives_selection_all(
    _database, db
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    conftest.seed_user_stream(db, project_uuid, owner_uuid, "Catalog owner")
    settings = {
        "kind": "zulip",
        "server_url": "https://zulip.example.test",
        "selection_mode": "explicit",
        "history_depth": "30_days",
        "default_project_id": str(project_uuid),
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status)
            VALUES (%s, 'zulip', 1, 'active')
            """,
            (instance_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings, desired_generation
            ) VALUES (%s, %s, 'zulip', %s::jsonb, 1)
            """,
            (account_uuid, owner_uuid, sql_state._json(settings)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_policies_v1
                (uuid, provider, enabled, limits)
            VALUES (%s, 'zulip', TRUE,
                    '{"max_selected_chats_per_account":2}'::jsonb)
            """,
            (sys_uuid.uuid4(),),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    identity = _identity(instance_uuid, realm_uuid)
    desired = {
        "resource_type": "external_account",
        "uuid": str(account_uuid),
        "generation": 1,
    }
    with session_factory() as session:
        sql_state.append_upsert(session, instance_uuid, "zulip", desired)

    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def catalog_report(resource_uuid, generation, operation="upsert"):
        return {
            "report_uuid": str(sys_uuid.uuid4()),
            "resource_type": "external_chat_catalog",
            "resource_uuid": str(resource_uuid),
            "observed_generation": generation,
            "status": "ready" if operation == "upsert" else "deleted",
            "progress": {
                "phase": "discovery",
                "completed": 1,
                "total": 1,
                "last_progress_at": observed_at,
            },
            "safe_error": None,
            "observed_at": observed_at,
            "catalog": {
                "operation": operation,
                "external_account_uuid": str(account_uuid),
                "owner_user_uuid": str(owner_uuid),
                "provider_kind": "zulip",
                "project_id": str(project_uuid),
                "source": {
                    "kind": "zulip",
                    "chat_type": "channel",
                    "provider_chat_key": f"channel:{resource_uuid}",
                    "original_url": (
                        f"https://zulip.example.test/#narrow/channel/{resource_uuid}"
                    ),
                },
                "display_name": "Engineering",
                "description": "Engineering discussions",
                "participants": [
                    {
                        "provider_user_id": "7",
                        "display_name": "Catalog owner",
                        "email": "owner@example.test",
                        "avatar_urn": None,
                        "is_owner": True,
                    }
                ],
                "topics": [
                    {
                        "provider_topic_id": f"{resource_uuid}:deploys",
                        "name": "deploys",
                        "is_default": False,
                    }
                ],
                "capabilities": {"messenger.message.send": {"available": True}},
            },
        }

    upsert = catalog_report(chat_uuid, 1)
    assert _request_call(repository.observed_reports, identity, [upsert])["results"][0][
        "status"
    ] == ("applied")

    invalid_direct_uuid = sys_uuid.uuid4()
    invalid_direct = catalog_report(invalid_direct_uuid, 1)
    invalid_direct["catalog"]["source"].update(
        {
            "chat_type": "direct",
            "provider_chat_key": "direct:7",
        }
    )
    invalid_direct["catalog"]["topics"][0]["is_default"] = True
    valid_after_invalid_uuid = sys_uuid.uuid4()
    valid_after_invalid = catalog_report(valid_after_invalid_uuid, 1)
    partial = _request_call(
        repository.observed_reports,
        identity,
        [invalid_direct, valid_after_invalid],
    )["results"]
    assert [result["status"] for result in partial] == ["rejected", "applied"]

    invalid_group_uuid = sys_uuid.uuid4()
    invalid_group = catalog_report(invalid_group_uuid, 1)
    invalid_group["catalog"]["source"].update(
        {
            "chat_type": "group_direct",
            "provider_chat_key": "group_direct:7,8",
        }
    )
    invalid_group["catalog"]["participants"].append(
        {
            "provider_user_id": "8",
            "display_name": "Peer",
            "email": "peer@example.test",
            "avatar_urn": None,
            "is_owner": False,
        }
    )
    invalid_group["catalog"]["topics"][0]["is_default"] = True
    assert (
        _request_call(repository.observed_reports, identity, [invalid_group])[
            "results"
        ][0]["status"]
        == "rejected"
    )

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_chats_v2 WHERE uuid = ANY(%s)",
            ([invalid_direct_uuid, invalid_group_uuid],),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_bridge_desired_resources_v1 "
            "WHERE resource_type = 'external_chat_assignment' "
            "AND resource_uuid = ANY(%s)",
            ([invalid_direct_uuid, invalid_group_uuid],),
        )
        assert cursor.fetchone()[0] == 0
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT selected, project_id, status, display_name, source
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        first_chat = cursor.fetchone()
        assert first_chat[:4] == (False, None, "available", "Engineering")
        assert first_chat[4] == {
            "kind": "zulip",
            "chat_type": "channel",
            "original_url": (f"https://zulip.example.test/#narrow/channel/{chat_uuid}"),
            "description": "Engineering discussions",
            "participants": [
                {
                    "identity_uuid": str(owner_uuid),
                    "provider_user_id": "7",
                    "display_name": "Catalog owner",
                    "email": "owner@example.test",
                    "avatar_urn": None,
                    "role": "owner",
                }
            ],
            "topics": [
                {
                    "topic_uuid": str(
                        sql_state._projection_uuid(
                            chat_uuid, "topic", f"{chat_uuid}:deploys"
                        )
                    ),
                    "provider_topic_id": f"{chat_uuid}:deploys",
                    "name": "deploys",
                    "is_default": False,
                }
            ],
        }

    collision = catalog_report(chat_uuid, 1)
    collision["catalog"]["source"]["provider_chat_key"] = "channel:collision"
    assert (
        _request_call(repository.observed_reports, identity, [collision])["results"][0][
            "status"
        ]
        == "rejected"
    )

    stale = catalog_report(sys_uuid.uuid4(), 0)
    assert _request_call(repository.observed_reports, identity, [stale])["results"][0][
        "status"
    ] == ("stale")
    tombstone = catalog_report(chat_uuid, 1, operation="delete")
    tombstone["catalog"]["source"] = upsert["catalog"]["source"]
    assert (
        _request_call(repository.observed_reports, identity, [tombstone])["results"][0][
            "status"
        ]
        == "applied"
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_chats_v2 WHERE uuid = %s", (chat_uuid,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET settings = jsonb_set(settings, '{selection_mode}', '"all"'),
                desired_generation = 2
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    with session_factory() as session:
        sql_state.append_upsert(
            session, instance_uuid, "zulip", {**desired, "generation": 2}
        )
    selected_uuid = sys_uuid.uuid4()
    selected = catalog_report(selected_uuid, 2)
    assert _request_call(repository.observed_reports, identity, [selected])["results"][
        0
    ]["status"] == ("applied")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT selected, project_id, status
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (selected_uuid,),
        )
        assert cursor.fetchone() == (True, project_uuid, "syncing")
        cursor.execute(
            """
            SELECT operation, generation, resource
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment' AND resource_uuid = %s
            """,
            (selected_uuid,),
        )
        operation, generation, assignment = cursor.fetchone()
        assert (operation, generation) == ("upsert", 1)
        projection = assignment["workspace_projection"]
        assert projection["stream"] == {
            "uuid": str(
                sql_state._projection_uuid(
                    selected_uuid,
                    "stream",
                    "canonical",
                )
            ),
            "name": "Engineering",
            "description": "Engineering discussions",
            "chat_kind": "channel",
            "private": False,
            "default_topic_uuid": None,
        }
        assert projection["participants"] == [
            {
                "identity_uuid": str(owner_uuid),
                "provider_user_id": "7",
                "display_name": "Catalog owner",
                "email": "owner@example.test",
                "avatar_urn": None,
                "role": "owner",
            }
        ]
        assert projection["topics"][0]["provider_topic_id"] == (
            f"{selected_uuid}:deploys"
        )
        assert sys_uuid.UUID(projection["topics"][0]["topic_uuid"])

    second_selected_uuid = sys_uuid.uuid4()
    assert (
        _request_call(
            repository.observed_reports,
            identity,
            [catalog_report(second_selected_uuid, 2)],
        )["results"][0]["status"]
        == "applied"
    )
    over_limit_uuid = sys_uuid.uuid4()
    assert (
        _request_call(
            repository.observed_reports,
            identity,
            [catalog_report(over_limit_uuid, 2)],
        )["results"][0]["status"]
        == "applied"
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT selected, project_id, status FROM m_external_chats_v2 "
            "WHERE uuid = %s",
            (over_limit_uuid,),
        )
        assert cursor.fetchone() == (False, None, "available")
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_bridge_desired_resources_v1 "
            "WHERE resource_type = 'external_chat_assignment' "
            "AND resource_uuid = %s",
            (over_limit_uuid,),
        )
        assert cursor.fetchone()[0] == 0

    assignment_report = {
        "report_uuid": str(sys_uuid.uuid4()),
        "resource_type": "external_chat_assignment",
        "resource_uuid": str(selected_uuid),
        "observed_generation": 1,
        "status": "applying",
        "progress": {
            "phase": "provisioning",
            "completed": 0,
            "total": 1,
            "last_progress_at": observed_at,
        },
        "safe_error": None,
        "observed_at": observed_at,
    }
    first = _request_call(repository.observed_reports, identity, [assignment_report])[
        "results"
    ][0]
    assert first["status"] == "applied"
    live_ready = {
        **assignment_report,
        "report_uuid": str(sys_uuid.uuid4()),
        "status": "live_ready",
        "progress": {
            **assignment_report["progress"],
            "phase": "live",
            "completed": 1,
        },
    }
    second = _request_call(repository.observed_reports, identity, [live_ready])[
        "results"
    ][0]
    assert second["status"] == "applied"
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT status, revision FROM m_external_chats_v2 WHERE uuid = %s",
            (selected_uuid,),
        )
        assert cursor.fetchone() == ("live", 3)


def test_canonical_bridge_file_projection_is_idempotent_and_access_is_current(
    _database, db, tmp_path, monkeypatch
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "External chat")
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings)
            VALUES (%s, %s, 'zulip', %s::jsonb)
            """,
            (
                account_uuid,
                owner_uuid,
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid)
            VALUES (%s, %s, %s, 'zulip', 'engineering',
                    '{"kind":"zulip","chat_type":"channel"}'::jsonb,
                    'Engineering', TRUE, %s, %s)
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    file_uuid = sys_uuid.uuid4()
    operation_uuid = sys_uuid.uuid4()
    data = b"canonical provider file"
    storage_info = file_storage.save_workspace_file(
        file_uuid, data, storage_type="file"
    )
    created_at = datetime.datetime.now(datetime.timezone.utc)
    origin = {
        "kind": "external_provider",
        "provider_kind": "zulip",
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(chat_uuid),
        "operation_uuid": str(operation_uuid),
    }
    metadata = file_storage.WorkspaceFileMetadata(
        uuid=file_uuid,
        project_id=project_uuid,
        stream_uuid=stream_uuid,
        owner_uuid=owner_uuid,
        name="attachment.txt",
        description="",
        content_type="text/plain",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        created_at=created_at,
        origin=origin,
    )
    file_storage.save_workspace_file_metadata(metadata, storage_type="file")
    sidecar = {
        "schema_version": 2,
        "uuid": str(file_uuid),
        "project_id": str(project_uuid),
        "stream_uuid": str(stream_uuid),
        "owner_uuid": str(owner_uuid),
        "name": metadata.name,
        "description": metadata.description,
        "content_type": metadata.content_type,
        "size_bytes": metadata.size_bytes,
        "sha256": metadata.sha256,
        "created_at": created_at.isoformat(),
        "acl": {"mode": "stream_members", "stream_uuid": str(stream_uuid)},
        "origin": origin,
    }
    repository = file_repository.CanonicalFileRepository()

    _request_call(repository.commit_projection, sidecar, storage_info)
    _request_call(repository.commit_projection, sidecar, storage_info)

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_files WHERE uuid = %s",
            (file_uuid,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_file_accesses WHERE file_uuid = %s",
            (file_uuid,),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_events "
            "WHERE object_type = 'file' AND payload->>'uuid' = %s",
            (str(file_uuid),),
        )
        assert cursor.fetchone()[0] == 1

    resolved = _request_call(repository.resolve, file_uuid)
    assert resolved["origin"] == origin
    assert resolved["authorized_user_uuids"] == [str(owner_uuid)]

    with db.cursor() as cursor:
        cursor.execute(
            "DELETE FROM m_workspace_file_accesses WHERE file_uuid = %s",
            (file_uuid,),
        )
    assert _request_call(repository.resolve, file_uuid)["authorized_user_uuids"] == []


def test_external_message_is_queued_once_as_signed_mail_and_flushed(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "External queue")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE,
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (
                account_uuid,
                owner_uuid,
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'channel:42',
                    '{"kind":"zulip","chat_type":"channel","description":"",'
                    '"private":false,"participants":[],"topics":[]}'::jsonb,
                    'External',
                    TRUE, %s, %s, 'live',
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status)
            VALUES (%s, 'zulip', 1, 'active')
            """,
            (instance_uuid,),
        )
    secret = "opaque enrollment secret"
    message_uuid = sys_uuid.uuid4()
    message = {
        "uuid": message_uuid,
        "stream_uuid": stream_uuid,
        "topic_uuid": topic_uuid,
        "payload": {"kind": "markdown", "content": "hello from Workspace"},
    }
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        messenger_helpers.create_workspace_user_message(
            project_id=project_uuid,
            user_uuid=owner_uuid,
            session=session,
            return_visible=False,
            uuid=message_uuid,
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            payload=message_payloads.MarkdownPayload(content="hello from Workspace"),
        )
        first = external_bridge_data_plane.queue_message_create(
            session,
            project_uuid=project_uuid,
            owner_user_uuid=owner_uuid,
            message=message,
            realm_uuid=realm_uuid,
            bridge_instance_uuid=instance_uuid,
            identity_generation=1,
            enrollment_secret=secret,
        )
        second = external_bridge_data_plane.queue_message_create(
            session,
            project_uuid=project_uuid,
            owner_user_uuid=owner_uuid,
            message=message,
            realm_uuid=realm_uuid,
            bridge_instance_uuid=instance_uuid,
            identity_generation=1,
            enrollment_secret=secret,
        )
    assert first == second

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT raw_message, operation_sha256
            FROM m_external_bridge_mail_outbox_v1
            WHERE record_uuid = %s
            """,
            (first,),
        )
        raw_message, operation_sha256 = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_operations_v2 WHERE uuid = %s",
            (
                sys_uuid.uuid5(
                    external_bridge_data_plane.OPERATION_NAMESPACE,
                    f"message.create:{account_uuid}:{message_uuid}",
                ),
            ),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT delivery_status, delivery_metadata->>'status'
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        assert cursor.fetchone() == ("pending", "pending")
        cursor.execute(
            """
            SELECT object_type, action, COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
              AND ((object_type = 'external_operation' AND action = 'created')
                OR (object_type = 'message' AND action = 'updated'))
            GROUP BY object_type, action
            ORDER BY object_type, action
            """,
            (project_uuid,),
        )
        assert cursor.fetchall() == [
            ("external_operation", "created", 1),
            ("message", "updated", 1),
        ]
    key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "workspace-to-zulip"
    )
    record = external_bridge_codec.parse_message(
        bytes(raw_message),
        "workspace-to-zulip",
        [key],
        external_bridge_data_plane.WORKSPACE_SENDER,
        external_bridge_data_plane.BRIDGE_ADDRESS,
    )
    assert record["operation_sha256"] == operation_sha256
    assert record["operation"]["payload"]["payload"]["content"] == (
        "hello from Workspace"
    )
    result_record = {
        name: value for name, value in record.items() if name != "operation"
    }
    result_record.update(
        {
            "record_kind": "result",
            "record_uuid": str(sys_uuid.uuid4()),
            "in_reply_to_record_uuid": record["record_uuid"],
            "result": {
                "outcome": "committed",
                "provider_entity_id": "9001",
                "provider_revision": "2",
                "safe_error": None,
                "manual_retry_allowed": False,
            },
        }
    )
    with session_factory() as session:
        external_bridge_data_plane._apply_result(session, result_record)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT delivery_status, delivery_metadata->>'status'
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        assert cursor.fetchone() == ("delivered", "delivered")
        cursor.execute(
            """
            SELECT object_type, action, COUNT(*)
            FROM m_workspace_events
            WHERE project_id = %s
              AND ((object_type = 'external_operation' AND action = 'updated')
                OR (object_type = 'message' AND action = 'updated'))
            GROUP BY object_type, action
            ORDER BY object_type, action
            """,
            (project_uuid,),
        )
        assert cursor.fetchall() == [
            ("external_operation", "updated", 1),
            ("message", "updated", 2),
        ]

    appended = []

    class Client:
        def append(self, path, raw):
            appended.append((path, raw))

    class Runtime:
        @contextlib.contextmanager
        def external_bridge_outbox(self, target_account_uuid):
            assert target_account_uuid == account_uuid
            yield Client(), f"Accounts/{target_account_uuid}/Outbox"

    with session_factory() as session:
        assert external_bridge_data_plane.flush_outbox(session, Runtime()) == 1
    with session_factory() as session:
        assert external_bridge_data_plane.flush_outbox(session, Runtime()) == 0
    assert appended == [(f"Accounts/{account_uuid}/Outbox", bytes(raw_message))]


def test_effective_capabilities_converge_to_projected_resources_and_liveness(
    _database,
    db,
):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Capabilities")
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)
    capabilities = {
        "messenger.message.send": {
            "available": True,
            "revision": 3,
            "limits": {},
        },
        "messenger.stream.rename": {
            "available": True,
            "revision": 2,
            "limits": {},
        },
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_provider_policies_v1
                (uuid, provider, enabled, limits)
            VALUES (%s, 'zulip', TRUE,
                    '{"max_file_bytes":500}'::jsonb)
            ON CONFLICT (provider) DO UPDATE SET
                enabled = TRUE,
                emergency_suspended = FALSE,
                limits = EXCLUDED.limits
            """,
            (sys_uuid.uuid4(),),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status,
                 capabilities, last_heartbeat_at)
            VALUES (%s, 'zulip', 1, 'active', %s::jsonb, %s)
            """,
            (instance_uuid, sql_state._json(capabilities), now),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready)
            VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE)
            """,
            (
                account_uuid,
                owner_uuid,
                sql_state._json(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.test",
                        "default_project_id": str(project_uuid),
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_credentials_v2
                (uuid, external_account_uuid, key_version, envelope)
            VALUES (%s, %s, 1, %s::jsonb)
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                sql_state._json(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(instance_uuid),
                        }
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'dm:7',
                    '{"kind":"zulip","chat_type":"personal_dm"}'::jsonb,
                    'Capabilities', TRUE, %s, %s, 'live', %s::jsonb)
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                project_uuid,
                stream_uuid,
                sql_state._json(capabilities),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET external_account_uuid = %s,
                provider_metadata = '{"kind":"zulip"}'::jsonb
            WHERE project_id = %s AND uuid = %s
            """,
            (account_uuid, project_uuid, stream_uuid),
        )

    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        sql_state.refresh_effective_capabilities(session, now=now)
        with pytest.raises(ValueError, match="external_operation_unavailable"):
            external_bridge_data_plane.queue_workspace_operation(
                session,
                project_uuid=project_uuid,
                owner_user_uuid=owner_uuid,
                operation_uuid=sys_uuid.uuid4(),
                operation_kind="stream.upsert",
                entity_uuid=stream_uuid,
                payload={
                    "name": "Not allowed for a DM",
                    "description": "",
                    "private": True,
                    "chat_kind": "personal_dm",
                    "participant_uuids": [str(owner_uuid)],
                    "default_topic_uuid": None,
                },
                target_type="stream",
                target_stream_uuid=stream_uuid,
                realm_uuid=sys_uuid.uuid4(),
                bridge_instance_uuid=instance_uuid,
                identity_generation=1,
                enrollment_secret="capability test secret",
                now=now,
            )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT capabilities->'messenger.message.send'->>'available',
                   capabilities->'messenger.stream.rename'->>'available',
                   capabilities->'messenger.stream.rename'
                       ->'unavailable_reason'->>'code'
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == ("true", "false", "chat_type_unsupported")
        cursor.execute(
            """
            SELECT provider_metadata->'capabilities'
                       ->'messenger.message.send'->>'available',
                   provider_metadata->'capabilities'
                       ->'messenger.stream.rename'->>'available'
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, stream_uuid),
        )
        assert cursor.fetchone() == ("true", "false")
        cursor.execute(
            """
            SELECT payload->'provider'->'capabilities'
                       ->'messenger.stream.rename'->>'available'
            FROM m_workspace_events
            WHERE project_id = %s AND object_type = 'stream'
              AND action = 'updated' AND payload->'provider' IS NOT NULL
            ORDER BY epoch_version DESC
            LIMIT 1
            """,
            (project_uuid,),
        )
        assert cursor.fetchone() == ("false",)
        cursor.execute(
            "UPDATE m_external_bridge_instances_v2 "
            "SET status = 'suspended' WHERE uuid = %s",
            (instance_uuid,),
        )

    with session_factory() as session:
        sql_state.refresh_effective_capabilities(session, now=now)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT capabilities->'messenger.message.send'->>'available',
                   capabilities->'messenger.message.send'
                       ->'unavailable_reason'->>'code'
            FROM m_external_accounts_v2 WHERE uuid = %s
            """,
            (account_uuid,),
        )
        assert cursor.fetchone() == ("false", "bridge_suspended")
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET status = 'active', last_heartbeat_at = %s
            WHERE uuid = %s
            """,
            (now, instance_uuid),
        )

    with session_factory() as session:
        sql_state.refresh_effective_capabilities(session, now=now)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT capabilities->'messenger.message.send'->>'available'
            FROM m_external_accounts_v2 WHERE uuid = %s
            """,
            (account_uuid,),
        )
        assert cursor.fetchone() == ("true",)
        cursor.execute(
            """
            SELECT capabilities->'messenger.message.send'->>'available',
                   catalog_capabilities->'messenger.message.send'->>'available',
                   capabilities->'messenger.message.send'->'unavailable_reason'
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == ("true", "true", None)
        cursor.execute(
            """
            UPDATE m_external_bridge_instances_v2
            SET last_heartbeat_at = %s
            WHERE uuid = %s
            """,
            (now - datetime.timedelta(seconds=61), instance_uuid),
        )
    with session_factory() as session:
        sql_state.refresh_effective_capabilities(session, now=now)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT capabilities->'messenger.message.send'->>'available',
                   capabilities->'messenger.message.send'
                       ->'unavailable_reason'->>'code'
            FROM m_external_accounts_v2 WHERE uuid = %s
            """,
            (account_uuid,),
        )
        assert cursor.fetchone() == ("false", "bridge_offline")


def test_workspace_outbound_mutations_use_provider_neutral_mail_contract(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Outbound")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    local_topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db,
            project_uuid,
            stream_uuid,
            owner_uuid,
            "first outbound",
            is_default=False,
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    capabilities = {
        name: {"available": True, "revision": 1, "limits": {}}
        for name in (
            "messenger.message.edit",
            "messenger.message.delete",
            "messenger.message.read",
            "messenger.stream.rename",
            "messenger.topic.rename",
        )
    }
    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO m_external_bridge_instances_v2 "
            "(uuid, provider, identity_generation, status) "
            "VALUES (%s, 'zulip', 1, 'active')",
            (instance_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip', '{}'::jsonb, TRUE, 'live', TRUE, %s::jsonb)
            """,
            (account_uuid, owner_uuid, sql_state._json(capabilities)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, history_depth, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'channel:42', %s::jsonb, 'Outbound',
                    TRUE, %s, %s, 'new', 'live', %s::jsonb)
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                sql_state._json(
                    {
                        "kind": "zulip",
                        "chat_type": "channel",
                        "description": "",
                        "participants": [
                            {
                                "identity_uuid": str(owner_uuid),
                                "provider_user_id": "7",
                                "display_name": "Owner",
                                "email": None,
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
                ),
                project_uuid,
                stream_uuid,
                sql_state._json(capabilities),
            ),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    identity = _identity(instance_uuid, realm_uuid)
    control_repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    initial_cursor = _request_call(control_repository.initial_cursor, identity)
    message_uuid = sys_uuid.uuid4()
    operations = (
        (
            "message.update",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(owner_uuid),
                "payload": {"kind": "markdown", "content": "edited"},
            },
            "message",
            None,
        ),
        (
            "message.delete",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "author_uuid": str(owner_uuid),
            },
            "message",
            None,
        ),
        (
            "read_state.set",
            message_uuid,
            {
                "stream_uuid": str(stream_uuid),
                "topic_uuid": str(topic_uuid),
                "reader_uuid": str(owner_uuid),
                "through_message_uuid": str(message_uuid),
                "read": True,
            },
            "message",
            None,
        ),
        (
            "stream.upsert",
            stream_uuid,
            {
                "name": "Renamed stream",
                "description": "",
                "private": False,
                "chat_kind": "channel",
                "participant_uuids": [str(owner_uuid)],
                "default_topic_uuid": str(topic_uuid),
            },
            "stream",
            stream_uuid,
        ),
        (
            "topic.upsert",
            topic_uuid,
            {"stream_uuid": str(stream_uuid), "name": "renamed general"},
            "topic",
            None,
        ),
        (
            "topic.upsert",
            local_topic_uuid,
            {"stream_uuid": str(stream_uuid), "name": "first outbound"},
            "topic",
            None,
        ),
    )
    secret = "outbound contract secret"
    record_uuids = []
    with session_factory() as session:
        for kind, entity_uuid, payload, target_type, target_stream_uuid in operations:
            if kind == "stream.upsert":
                session.execute(
                    "UPDATE m_workspace_streams "
                    "SET name = %s, description = %s, private = %s "
                    "WHERE project_id = %s AND uuid = %s",
                    (
                        payload["name"],
                        payload["description"],
                        payload["private"],
                        project_uuid,
                        entity_uuid,
                    ),
                )
            elif kind == "topic.upsert":
                session.execute(
                    "UPDATE m_workspace_stream_topics SET name = %s "
                    "WHERE project_id = %s AND uuid = %s",
                    (payload["name"], project_uuid, entity_uuid),
                )
            record_uuids.append(
                external_bridge_data_plane.queue_workspace_operation(
                    session,
                    project_uuid=project_uuid,
                    owner_user_uuid=owner_uuid,
                    operation_uuid=sys_uuid.uuid4(),
                    operation_kind=kind,
                    entity_uuid=entity_uuid,
                    payload=payload,
                    target_type=target_type,
                    target_stream_uuid=target_stream_uuid,
                    provider_entity_id=(None if kind == "topic.upsert" else "9001"),
                    provider_revision="7",
                    realm_uuid=realm_uuid,
                    bridge_instance_uuid=instance_uuid,
                    identity_generation=1,
                    enrollment_secret=secret,
                )
            )
    key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "workspace-to-zulip"
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT raw_message FROM m_external_bridge_mail_outbox_v1 "
            "WHERE record_uuid = ANY(%s) ORDER BY created_at, record_uuid",
            (record_uuids,),
        )
        records = [
            external_bridge_codec.parse_message(
                bytes(row[0]),
                "workspace-to-zulip",
                [key],
                external_bridge_data_plane.WORKSPACE_SENDER,
                external_bridge_data_plane.BRIDGE_ADDRESS,
            )
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT generation, resource
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (chat_uuid,),
        )
        desired_generation, assignment = cursor.fetchone()
    assert {record["operation"]["kind"] for record in records} == {
        item[0] for item in operations
    }
    assert sorted(record["sequence"] for record in records) == [1, 2, 3, 4, 5, 6]
    assert desired_generation == 4
    assert assignment["history_depth"] == "new"
    assert assignment["workspace_projection"]["stream"] == {
        "uuid": str(stream_uuid),
        "name": "Renamed stream",
        "description": "",
        "chat_kind": "channel",
        "private": False,
        "default_topic_uuid": str(topic_uuid),
    }
    renamed_topic = next(
        topic
        for topic in assignment["workspace_projection"]["topics"]
        if topic["topic_uuid"] == str(topic_uuid)
    )
    assert renamed_topic == {
        "topic_uuid": str(topic_uuid),
        "provider_topic_id": "42:renamed general",
        "name": "renamed general",
        "is_default": True,
    }
    mapped_topic = next(
        topic
        for topic in assignment["workspace_projection"]["topics"]
        if topic["topic_uuid"] == str(local_topic_uuid)
    )
    assert mapped_topic["provider_topic_id"] == "42:first outbound"
    topic_records = {
        record["operation"]["entity_uuid"]: record
        for record in records
        if record["operation"]["kind"] == "topic.upsert"
    }
    assert topic_records[str(topic_uuid)]["operation"]["provider"]["entity_id"] == (
        "42:renamed general"
    )
    assert topic_records[str(local_topic_uuid)]["operation"]["provider"][
        "entity_id"
    ] == ("42:first outbound")

    def inbound_record(kind, entity_uuid, payload, revision):
        return {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": kind,
                "entity_uuid": str(entity_uuid),
                "actor_uuid": str(owner_uuid),
                "occurred_at": f"2026-07-18T00:00:0{revision}Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "channel:42",
                    "entity_id": (
                        "channel:42"
                        if kind == "stream.upsert"
                        else f"42:{payload['name']}"
                    ),
                    "revision": str(revision),
                },
                "payload": payload,
                "extensions": {"delivery_class": "live"},
            },
        }

    inbound_stream_payload = {
        "name": "Provider renamed stream",
        "description": "Provider description",
        "private": True,
        "chat_kind": "channel",
        "participant_uuids": [str(owner_uuid)],
        "default_topic_uuid": str(topic_uuid),
    }
    inbound_topic_payload = {
        "stream_uuid": str(stream_uuid),
        "name": "provider renamed topic",
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            inbound_record("stream.upsert", stream_uuid, inbound_stream_payload, 8),
            conftest.TEST_MAIL_RUNTIME,
        )
        external_bridge_data_plane._apply_inbound_operation(
            session,
            inbound_record("topic.upsert", topic_uuid, inbound_topic_payload, 9),
            conftest.TEST_MAIL_RUNTIME,
        )

    batch = _request_call(control_repository.changes, identity, initial_cursor)
    assignment_changes = [
        change
        for change in batch["changes"]
        if change["resource_type"] == "external_chat_assignment"
    ]
    assert [change["generation"] for change in assignment_changes] == [2, 3, 4, 5, 6]
    final_assignment = assignment_changes[-1]["resource"]
    assert final_assignment["workspace_projection"]["stream"] == {
        "uuid": str(stream_uuid),
        "name": "Provider renamed stream",
        "description": "Provider description",
        "chat_kind": "channel",
        "private": True,
        "default_topic_uuid": str(topic_uuid),
    }
    provider_topic = next(
        topic
        for topic in final_assignment["workspace_projection"]["topics"]
        if topic["topic_uuid"] == str(topic_uuid)
    )
    assert provider_topic == {
        "topic_uuid": str(topic_uuid),
        "provider_topic_id": "42:provider renamed topic",
        "name": "provider renamed topic",
        "is_default": True,
    }
    snapshot, created = _request_call(
        control_repository.create_snapshot, identity, sys_uuid.uuid4()
    )
    assert created is True
    page = _request_call(
        control_repository.snapshot_page, identity, snapshot["snapshot_token"]
    )
    snapshot_assignment = next(
        resource
        for resource in page["resources"]
        if resource["resource_type"] == "external_chat_assignment"
        and resource["uuid"] == str(chat_uuid)
    )
    assert snapshot_assignment == {
        **final_assignment,
        "required_capabilities": assignment_changes[-1]["required_capabilities"],
    }


def test_inbound_stream_snapshot_reconciles_participant_state_with_canonical_helpers(
    _database, db
):
    owner_uuid = sys_uuid.uuid4()
    removed_uuid = sys_uuid.uuid4()
    added_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "External members")
    )
    conftest.seed_workspace_user(db, removed_uuid, f"user-{removed_uuid}")
    conftest.seed_workspace_user(db, added_uuid, f"user-{added_uuid}")
    conftest.seed_user_stream_binding(db, project_uuid, stream_uuid, removed_uuid)
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        message = messenger_helpers.create_workspace_user_message(
            project_id=project_uuid,
            user_uuid=owner_uuid,
            uuid=sys_uuid.uuid4(),
            stream_uuid=stream_uuid,
            topic_uuid=topic_uuid,
            payload=message_payloads.MarkdownPayload(content="existing message"),
            session=session,
            return_visible=False,
        )
        messenger_models.WorkspaceFile(
            uuid=file_uuid,
            project_id=project_uuid,
            user_uuid=owner_uuid,
            stream_uuid=stream_uuid,
            name="existing.txt",
            description="",
            content_type="text/plain",
            size_bytes=1,
            hash="0" * 64,
            storage_object_id=str(file_uuid),
        ).insert(session=session)
        for user_uuid in (owner_uuid, removed_uuid):
            messenger_models.WorkspaceFileAccess(
                project_id=project_uuid,
                file_uuid=file_uuid,
                user_uuid=user_uuid,
            ).insert(session=session)
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live', TRUE,
                    '{"messenger.stream.rename":{"available":true}}'::jsonb)
            """,
            (
                account_uuid,
                owner_uuid,
                '{"kind":"zulip","server_url":"https://zulip.example.test"}',
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', '42', '{}'::jsonb, 'Members',
                    TRUE, %s, %s, 'live')
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    record = {
        "origin": "zulip",
        "operation_uuid": str(sys_uuid.uuid4()),
        "account_uuid": str(account_uuid),
        "project_uuid": str(project_uuid),
        "operation": {
            "kind": "stream.upsert",
            "entity_uuid": str(stream_uuid),
            "actor_uuid": str(owner_uuid),
            "occurred_at": "2026-07-17T17:00:00Z",
            "provider": {
                "kind": "zulip",
                "chat_id": "42",
                "entity_id": "42",
                "revision": "2",
            },
            "payload": {
                "name": "External members",
                "description": "",
                "private": True,
                "chat_kind": "group_dm",
                "participant_uuids": [str(owner_uuid), str(added_uuid)],
                "default_topic_uuid": str(topic_uuid),
            },
            "extensions": {"delivery_class": "live"},
        },
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session, record, conftest.TEST_MAIL_RUNTIME
        )

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT user_uuid FROM m_workspace_stream_bindings "
            "WHERE project_id = %s AND stream_uuid = %s ORDER BY user_uuid",
            (project_uuid, stream_uuid),
        )
        assert {row[0] for row in cursor.fetchall()} == {owner_uuid, added_uuid}
        cursor.execute(
            "SELECT private FROM m_workspace_streams "
            "WHERE project_id = %s AND uuid = %s",
            (project_uuid, stream_uuid),
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            "SELECT user_uuid FROM m_workspace_user_message_flags "
            "WHERE project_id = %s AND uuid = %s ORDER BY user_uuid",
            (project_uuid, message.uuid),
        )
        assert {row[0] for row in cursor.fetchall()} == {
            owner_uuid,
            removed_uuid,
            added_uuid,
        }
        cursor.execute(
            "SELECT user_uuid FROM m_workspace_file_accesses "
            "WHERE project_id = %s AND file_uuid = %s ORDER BY user_uuid",
            (project_uuid, file_uuid),
        )
        assert {row[0] for row in cursor.fetchall()} == {owner_uuid, added_uuid}
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.streams[stream_uuid]["private"] is True
    projected_users = {
        sys_uuid.UUID(binding["user_uuid"])
        for binding in projection.bindings.values()
        if binding["stream_uuid"] == str(stream_uuid)
    }
    assert projected_users == {owner_uuid, added_uuid}


def test_inbound_identity_and_private_stream_lifecycle_are_canonical(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"owner-{owner_uuid}")
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip',
                    '{"server_url":"https://zulip.example.test"}'::jsonb,
                    TRUE, 'live', TRUE,
                    '{
                      "messenger.chat_catalog":{"available":true},
                      "messenger.stream.rename":{"available":true},
                      "stream.delete":{"available":true}
                    }'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', '42', '{}'::jsonb, 'Private chat',
                    TRUE, %s, %s, 'live')
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    def record(kind, entity_uuid, payload, actor_uuid=owner_uuid, revision="1"):
        return {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": kind,
                "entity_uuid": str(entity_uuid),
                "actor_uuid": str(actor_uuid),
                "occurred_at": "2026-07-17T17:30:00Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "42",
                    "entity_id": str(entity_uuid),
                    "revision": revision,
                },
                "payload": payload,
                "extensions": {"delivery_class": "live"},
            },
        }

    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record(
                "identity.upsert",
                peer_uuid,
                {
                    "display_name": "External Peer",
                    "email": "peer@example.test",
                    "active": True,
                    "avatar_urn": None,
                },
                actor_uuid=peer_uuid,
            ),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT source, external_account_uuid, provider_external_id "
            "FROM m_workspace_users WHERE uuid = %s",
            (peer_uuid,),
        )
        assert cursor.fetchone() == ("zulip", account_uuid, str(peer_uuid))

    stream_payload = {
        "name": "Private chat",
        "description": "",
        "private": True,
        "chat_kind": "personal_dm",
        "participant_uuids": [str(owner_uuid), str(peer_uuid)],
        "default_topic_uuid": str(topic_uuid),
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("stream.upsert", stream_uuid, stream_payload),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT private, source_name, external_account_uuid "
            "FROM m_workspace_streams WHERE uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        assert cursor.fetchone() == (True, "zulip", account_uuid)
        cursor.execute(
            "SELECT user_uuid FROM m_workspace_stream_bindings "
            "WHERE stream_uuid = %s AND project_id = %s ORDER BY user_uuid",
            (stream_uuid, project_uuid),
        )
        assert {row[0] for row in cursor.fetchall()} == {owner_uuid, peer_uuid}
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.streams[stream_uuid]["private"] is True
    assert topic_uuid in projection.topics

    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record(
                "stream.delete",
                stream_uuid,
                {"stream_uuid": str(stream_uuid)},
                revision="2",
            ),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_streams "
            "WHERE uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        assert cursor.fetchone()[0] == 0
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert stream_uuid not in projection.streams


def test_private_stream_first_create_rolls_back_and_validates_exactly_two(
    _database, db
):
    owner_uuid = sys_uuid.uuid4()
    second_uuid = sys_uuid.uuid4()
    third_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    for user_uuid in (owner_uuid, second_uuid, third_uuid):
        conftest.seed_workspace_user(db, user_uuid, f"user-{user_uuid}")
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    missing_user_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip',
                    '{}'::jsonb, TRUE, 'live', TRUE,
                    '{"messenger.stream.rename":{"available":true}}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', '42', '{}'::jsonb, 'Private chat',
                    TRUE, %s, %s, 'live')
            """,
            (
                sys_uuid.uuid4(),
                account_uuid,
                owner_uuid,
                project_uuid,
                stream_uuid,
            ),
        )

    def stream_record(participants):
        return {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": "stream.upsert",
                "entity_uuid": str(stream_uuid),
                "actor_uuid": str(owner_uuid),
                "occurred_at": "2026-07-17T17:45:00Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "42",
                    "entity_id": "42",
                    "revision": "1",
                },
                "payload": {
                    "name": "Private chat",
                    "description": "",
                    "private": True,
                    "chat_kind": "personal_dm",
                    "participant_uuids": [str(value) for value in participants],
                    "default_topic_uuid": str(topic_uuid),
                },
                "extensions": {"delivery_class": "live"},
            },
        }

    session_factory = engines.engine_factory.get_engine().session_manager
    with pytest.raises(storage_exc.ConflictRecords):
        with session_factory() as session:
            external_bridge_data_plane._apply_inbound_operation(
                session,
                stream_record((owner_uuid, missing_user_uuid)),
                conftest.TEST_MAIL_RUNTIME,
            )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_streams "
            "WHERE project_id = %s AND uuid = %s",
            (project_uuid, stream_uuid),
        )
        assert cursor.fetchone()[0] == 0
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert stream_uuid not in projection.streams

    with pytest.raises(ValueError, match="invalid_record"):
        with session_factory() as session:
            external_bridge_data_plane._apply_inbound_operation(
                session,
                stream_record((owner_uuid, second_uuid, third_uuid)),
                conftest.TEST_MAIL_RUNTIME,
            )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_streams "
            "WHERE project_id = %s AND uuid = %s",
            (project_uuid, stream_uuid),
        )
        assert cursor.fetchone()[0] == 0
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert stream_uuid not in projection.streams


def test_channel_stream_upsert_preserves_null_default_topic(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, "channel-owner")
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip',
                    '{"server_url":"https://zulip.example.test"}'::jsonb,
                    TRUE, 'live', TRUE,
                    '{"messenger.stream.rename":{"available":true}}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts
                (uuid, project_id, user_uuid, account_type, status,
                 account_settings, server_url, access_status)
            VALUES (%s, %s, %s, 'zulip', 'active',
                    '{"credentials":{"api_key":"test"}}'::jsonb,
                    'https://zulip.example.test', 'confirmed')
            """,
            (sys_uuid.uuid4(), project_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'channel:42',
                    '{"kind":"zulip","chat_type":"channel"}'::jsonb,
                    'Channel', TRUE, %s, %s, 'live',
                    '{"messenger.stream.rename":{"available":true}}'::jsonb)
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )
    record = {
        "origin": "zulip",
        "record_uuid": str(sys_uuid.uuid4()),
        "operation_uuid": str(sys_uuid.uuid4()),
        "account_uuid": str(account_uuid),
        "project_uuid": str(project_uuid),
        "operation": {
            "kind": "stream.upsert",
            "entity_uuid": str(stream_uuid),
            "actor_uuid": str(owner_uuid),
            "occurred_at": "2026-07-17T17:45:00Z",
            "provider": {
                "kind": "zulip",
                "chat_id": "channel:42",
                "entity_id": "42",
                "revision": "1",
            },
            "payload": {
                "name": "Channel",
                "description": "",
                "private": False,
                "chat_kind": "channel",
                "participant_uuids": [str(owner_uuid)],
                "default_topic_uuid": None,
            },
            "extensions": {"delivery_class": "live"},
        },
    }
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session, record, conftest.TEST_MAIL_RUNTIME
        )
    record["operation_uuid"] = str(sys_uuid.uuid4())
    record["record_uuid"] = str(sys_uuid.uuid4())
    record["operation"]["provider"]["revision"] = "2"
    record["operation"]["payload"]["name"] = "Channel renamed"
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session, record, conftest.TEST_MAIL_RUNTIME
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT name, default_topic_uuid, source->>'stream_id', "
            "provider_metadata->'capabilities'->'messenger.stream.rename'->>'available' "
            "FROM m_workspace_streams "
            "WHERE uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        assert cursor.fetchone() == ("Channel renamed", None, "42", "true")
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_stream_topics "
            "WHERE stream_uuid = %s AND project_id = %s",
            (stream_uuid, project_uuid),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """
            SELECT payload->'provider'->>'revision'
            FROM m_workspace_events
            WHERE project_id = %s AND object_type = 'stream'
              AND action = 'updated' AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC LIMIT 1
            """,
            (project_uuid, str(stream_uuid)),
        )
        assert cursor.fetchone() == ("2",)
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.streams[stream_uuid]["default_topic_uuid"] is None


def test_pre_ready_backfill_and_live_accept_ingress_but_unassigned_does_not(
    _database,
    db,
):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    identity_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"owner-{owner_uuid}")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip',
                    '{"server_url":"https://zulip.example.test"}'::jsonb,
                    TRUE, 'backfill', FALSE,
                    '{
                      "messenger.chat_catalog":{"available":true},
                      "messenger.message.send":{"available":true}
                    }'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )

    def record(kind, entity_uuid, payload):
        return {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": kind,
                "entity_uuid": str(entity_uuid),
                "actor_uuid": str(owner_uuid),
                "occurred_at": "2026-07-18T12:00:00Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "channel:404",
                    "entity_id": str(entity_uuid),
                    "revision": "1",
                },
                "payload": payload,
                "extensions": {"delivery_class": "backfill"},
            },
        }

    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record(
                "identity.upsert",
                identity_uuid,
                {
                    "display_name": "Backfill User",
                    "email": "backfill@example.test",
                    "active": True,
                    "avatar_urn": None,
                },
            ),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT source FROM m_workspace_users WHERE uuid = %s",
            (identity_uuid,),
        )
        assert cursor.fetchone() == ("zulip",)
        cursor.execute(
            "UPDATE m_external_accounts_v2 SET status = 'live' WHERE uuid = %s",
            (account_uuid,),
        )

    pre_ready_uuid = sys_uuid.uuid4()
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record(
                "identity.upsert",
                pre_ready_uuid,
                {
                    "display_name": "Too Early",
                    "email": "too-early@example.test",
                    "active": True,
                    "avatar_urn": None,
                },
            ),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT source FROM m_workspace_users WHERE uuid = %s",
            (pre_ready_uuid,),
        )
        assert cursor.fetchone() == ("zulip",)

    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_external_accounts_v2 SET status = 'backfill' WHERE uuid = %s",
            (account_uuid,),
        )
    with pytest.raises(ValueError, match="result_binding_mismatch"):
        with session_factory() as session:
            external_bridge_data_plane._apply_inbound_operation(
                session,
                record(
                    "message.create",
                    sys_uuid.uuid4(),
                    {
                        "stream_uuid": str(sys_uuid.uuid4()),
                        "topic_uuid": str(sys_uuid.uuid4()),
                        "author_uuid": str(owner_uuid),
                        "payload": {"kind": "markdown", "content": "unassigned"},
                        "reply_to_message_uuid": None,
                    },
                ),
                conftest.TEST_MAIL_RUNTIME,
            )


def test_inbound_delivery_class_freezes_notification_eligibility(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Notification gate")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip',
                    '{"server_url":"https://zulip.example.test"}'::jsonb,
                    TRUE, 'backfill', FALSE,
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'channel:42',
                    '{"kind":"zulip","chat_type":"channel"}'::jsonb,
                    'Notification gate', TRUE, %s, %s, 'live',
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    def message_record(index, delivery_class):
        message_uuid = sys_uuid.uuid4()
        return message_uuid, {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": "message.create",
                "entity_uuid": str(message_uuid),
                "actor_uuid": str(owner_uuid),
                "occurred_at": f"2026-07-18T12:00:0{index}Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "channel:42",
                    "entity_id": str(9000 + index),
                    "revision": "1",
                },
                "payload": {
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "author_uuid": str(owner_uuid),
                    "payload": {
                        "kind": "markdown",
                        "content": f"delivery {index}",
                    },
                    "reply_to_message_uuid": None,
                },
                "extensions": {"delivery_class": delivery_class},
            },
        }

    session_factory = engines.engine_factory.get_engine().session_manager
    expected = []
    for index, delivery_class, eligible in (
        (1, "backfill", False),
        (2, "live", False),
    ):
        message_uuid, record = message_record(index, delivery_class)
        with session_factory() as session:
            external_bridge_data_plane._apply_inbound_operation(
                session, record, conftest.TEST_MAIL_RUNTIME
            )
        expected.append((message_uuid, delivery_class, eligible))
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET status = 'live', live_ready = TRUE
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    message_uuid, record = message_record(3, "live")
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session, record, conftest.TEST_MAIL_RUNTIME
        )
    expected.append((message_uuid, "live", True))

    with db.cursor() as cursor:
        for message_uuid, delivery_class, eligible in expected:
            cursor.execute(
                """
                SELECT provider_metadata->>'delivery_class',
                       (provider_metadata->>'notification_eligible')::boolean
                FROM m_workspace_messages
                WHERE project_id = %s AND uuid = %s
                """,
                (project_uuid, message_uuid),
            )
            assert cursor.fetchone() == (delivery_class, eligible)
            cursor.execute(
                """
                SELECT payload->'provider'->>'delivery_class',
                       (payload->'provider'->>'notification_eligible')::boolean
                FROM m_workspace_events
                WHERE project_id = %s AND object_type = 'message'
                  AND action = 'created' AND payload->>'uuid' = %s
                ORDER BY epoch_version DESC LIMIT 1
                """,
                (project_uuid, str(message_uuid)),
            )
            assert cursor.fetchone() == (delivery_class, eligible)


def test_ingress_rejects_missing_capability_and_unauthorized_actor_without_effects(
    _database, db
):
    owner_uuid = sys_uuid.uuid4()
    stranger_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Authorization")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    conftest.seed_workspace_user(db, stranger_uuid, f"user-{stranger_uuid}")
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
                VALUES (%s, %s, 'zulip',
                        '{"server_url":"https://zulip.example.test"}'::jsonb,
                        TRUE, 'live', TRUE,
                    '{}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', '42', '{}'::jsonb, 'Authorization',
                    TRUE, %s, %s, 'live')
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )

    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    secret = "authorization enrollment secret"
    ingress_key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "zulip-to-workspace"
    )

    def signed(uid, actor_uuid, predecessor=None, content="denied"):
        operation_uuid = sys_uuid.uuid4()
        message_uuid = sys_uuid.uuid4()
        record = {
            "schema": external_bridge_codec.SCHEMA,
            "schema_version": external_bridge_codec.SCHEMA_VERSION,
            "record_kind": "operation",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(operation_uuid),
            "attempt": 1,
            "operation_sha256": "0" * 64,
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "origin": "zulip",
            "causal_lane": f"chat:{account_uuid}:{stream_uuid}",
            "sequence": uid,
            "predecessor_operation_uuid": (
                None if predecessor is None else str(predecessor)
            ),
            "created_at": f"2026-07-17T16:00:0{uid}Z",
            "expires_at": None,
            "operation": {
                "kind": "message.create",
                "entity_uuid": str(message_uuid),
                "actor_uuid": str(actor_uuid),
                "occurred_at": f"2026-07-17T16:00:0{uid}Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "42",
                    "entity_id": str(9000 + uid),
                    "revision": "1",
                },
                "payload": {
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "author_uuid": str(actor_uuid),
                    "payload": {"kind": "markdown", "content": content},
                    "reply_to_message_uuid": None,
                },
                "extensions": {"delivery_class": "live"},
            },
        }
        record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
        return (
            operation_uuid,
            message_uuid,
            external_bridge_codec.build_message(
                record,
                "zulip-to-workspace",
                ingress_key,
                external_bridge_data_plane.BRIDGE_ADDRESS,
                external_bridge_data_plane.INGRESS_ADDRESS,
            ),
        )

    first_operation_uuid, first_message_uuid, first_raw = signed(1, owner_uuid)
    second_operation_uuid, second_message_uuid, second_raw = signed(
        2, stranger_uuid, first_operation_uuid
    )
    third_operation_uuid, third_message_uuid, third_raw = signed(
        3,
        owner_uuid,
        second_operation_uuid,
        f"missing [file](urn:file:{sys_uuid.uuid4()})",
    )
    messages = {
        1: mail_protocol.FetchedMessage(1, frozenset(), first_raw),
        2: mail_protocol.FetchedMessage(2, frozenset(), second_raw),
        3: mail_protocol.FetchedMessage(3, frozenset(), third_raw),
    }

    class Client:
        def select(self, path, readonly=True):
            assert path == "INBOX" and readonly
            return mail_protocol.MailboxMetadata(91, len(messages), None)

        def search(self, criteria):
            first = int(criteria.split()[1].split(":", 1)[0])
            return [uid for uid in sorted(messages) if uid >= first]

        def fetch(self, uids):
            return [messages[uid] for uid in uids]

    class Runtime:
        @contextlib.contextmanager
        def external_bridge_ingress(self):
            yield Client()

        @contextlib.contextmanager
        def messenger_service(self, target_project_uuid):
            with conftest.TEST_MAIL_RUNTIME.messenger_service(
                target_project_uuid
            ) as service:
                yield service

    session_factory = engines.engine_factory.get_engine().session_manager
    args = {
        "realm_uuid": realm_uuid,
        "bridge_instance_uuid": instance_uuid,
        "identity_generation": 1,
        "enrollment_secret": secret,
        "limit": 1,
    }
    assert _consume_ingress(session_factory, Runtime(), **args) == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET capabilities = '{"messenger.message.send":{"available":true}}'::jsonb
            WHERE uuid = %s
            """,
            (account_uuid,),
        )
    assert _consume_ingress(session_factory, Runtime(), **args) == 1
    assert _consume_ingress(session_factory, Runtime(), **args) == 1

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uid, reason FROM m_external_bridge_mail_quarantine_v1 "
            "WHERE bridge_instance_uuid = %s ORDER BY uid",
            (instance_uuid,),
        )
        assert cursor.fetchall() == [
            (1, "capability_missing"),
            (2, "permission_denied"),
            (3, "permission_denied"),
        ]
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_messages WHERE uuid IN (%s, %s, %s)",
            (first_message_uuid, second_message_uuid, third_message_uuid),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT operation_uuid, raw_message "
            "FROM m_external_bridge_mail_outbox_v1 "
            "WHERE operation_uuid IN (%s, %s, %s)",
            (first_operation_uuid, second_operation_uuid, third_operation_uuid),
        )
        result_rows = cursor.fetchall()
    workspace_key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "workspace-to-zulip"
    )
    rejected = {
        operation_uuid: external_bridge_codec.parse_message(
            bytes(raw_message),
            "workspace-to-zulip",
            [workspace_key],
            external_bridge_data_plane.WORKSPACE_SENDER,
            external_bridge_data_plane.BRIDGE_ADDRESS,
        )["result"]
        for operation_uuid, raw_message in result_rows
    }
    assert rejected[first_operation_uuid]["safe_error"]["code"] == (
        "capability_missing"
    )
    assert rejected[second_operation_uuid]["safe_error"]["code"] == (
        "permission_denied"
    )
    assert rejected[third_operation_uuid]["safe_error"]["code"] == ("permission_denied")
    assert {value["outcome"] for value in rejected.values()} == {"rejected"}
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert first_message_uuid not in projection.messages
    assert second_message_uuid not in projection.messages
    assert third_message_uuid not in projection.messages


def test_inbound_message_update_and_delete_are_durable_in_canonical_mail(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Mail authority")
    )
    conftest.seed_stream_topic(
        db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "provider", is_default=False
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
                VALUES (%s, %s, 'zulip',
                        '{"server_url":"https://zulip.example.test"}'::jsonb,
                        TRUE, 'live', TRUE,
                        '{
                          "messenger.message.send":{"available":true},
                      "messenger.message.edit":{"available":true},
                      "messenger.message.delete":{"available":true},
                      "messenger.topic.rename":{"available":true},
                      "messenger.message.read":{"available":true}
                    }'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status)
            VALUES (%s, %s, %s, 'zulip', '42', '{}'::jsonb, 'Mail authority',
                    TRUE, %s, %s, 'live')
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        messenger_models.WorkspaceFile(
            uuid=file_uuid,
            project_id=project_uuid,
            user_uuid=owner_uuid,
            stream_uuid=stream_uuid,
            external_account_uuid=account_uuid,
            name="provider.txt",
            description="",
            content_type="text/plain",
            size_bytes=1,
            hash="0" * 64,
            storage_object_id=str(file_uuid),
        ).insert(session=session)
        messenger_models.WorkspaceFileAccess(
            project_id=project_uuid,
            file_uuid=file_uuid,
            user_uuid=owner_uuid,
        ).insert(session=session)

    def record(kind, payload, revision, entity_uuid=message_uuid):
        return {
            "origin": "zulip",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(sys_uuid.uuid4()),
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "operation": {
                "kind": kind,
                "entity_uuid": str(entity_uuid),
                "actor_uuid": str(owner_uuid),
                "occurred_at": f"2026-07-17T17:00:0{revision}Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": "42",
                    "entity_id": "9100",
                    "revision": str(revision),
                },
                "payload": payload,
                "extensions": {"delivery_class": "live"},
            },
        }

    topic_payload = {"stream_uuid": str(stream_uuid), "name": "renamed"}
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("topic.upsert", topic_payload, 1, topic_uuid),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.topics[topic_uuid]["name"] == "renamed"
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT name, provider_metadata->>'revision' "
            "FROM m_workspace_stream_topics WHERE uuid = %s",
            (topic_uuid,),
        )
        assert cursor.fetchone() == ("renamed", "1")
        cursor.execute(
            """
            SELECT payload->'provider'->>'revision'
            FROM m_workspace_events
            WHERE project_id = %s AND object_type = 'topic'
              AND action = 'updated' AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC LIMIT 1
            """,
            (project_uuid, str(topic_uuid)),
        )
        assert cursor.fetchone() == ("1",)

    create_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "author_uuid": str(owner_uuid),
        "payload": {
            "kind": "markdown",
            "content": f"original [file](urn:file:{file_uuid})",
        },
        "reply_to_message_uuid": None,
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("message.create", create_payload, 1),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.messages[message_uuid]["payload"]["content"] == (
        f"original [file](urn:file:{file_uuid})"
    )
    other_user_uuid = sys_uuid.uuid4()
    conftest.seed_user_stream_binding(
        db,
        project_uuid,
        stream_uuid,
        other_user_uuid,
    )
    second_message_uuid = sys_uuid.uuid4()
    third_message_uuid = sys_uuid.uuid4()
    with session_factory() as session:
        for extra_message_uuid in (second_message_uuid, third_message_uuid):
            messenger_helpers.create_workspace_user_message(
                uuid=extra_message_uuid,
                project_id=project_uuid,
                user_uuid=other_user_uuid,
                stream_uuid=stream_uuid,
                topic_uuid=topic_uuid,
                payload=message_payloads.MarkdownPayload(
                    content=f"exact read state {extra_message_uuid}"
                ),
                session=session,
                return_visible=False,
            )
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE m_workspace_messages SET created_at = CASE "
            "WHEN uuid = %s THEN '2026-07-17T17:00:01Z'::timestamptz "
            "WHEN uuid = %s THEN '2026-07-17T17:00:02Z'::timestamptz "
            "ELSE '2026-07-17T17:00:03Z'::timestamptz END "
            "WHERE project_id = %s AND uuid = ANY(%s)",
            (
                message_uuid,
                second_message_uuid,
                project_uuid,
                [message_uuid, second_message_uuid, third_message_uuid],
            ),
        )
        cursor.execute(
            "UPDATE m_workspace_user_message_flags SET read = FALSE "
            "WHERE project_id = %s AND uuid = ANY(%s) AND user_uuid = %s",
            (
                project_uuid,
                [message_uuid, second_message_uuid, third_message_uuid],
                owner_uuid,
            ),
        )
    db.commit()
    read_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "reader_uuid": str(owner_uuid),
        "message_uuids": [str(message_uuid), str(third_message_uuid)],
        "read": True,
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("read_state.set", read_payload, 2, sys_uuid.uuid4()),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.message_states[(owner_uuid, message_uuid)]["read"] is True
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid, read FROM m_workspace_user_message_flags "
            "WHERE project_id = %s AND uuid = ANY(%s) AND user_uuid = %s",
            (
                project_uuid,
                [message_uuid, second_message_uuid, third_message_uuid],
                owner_uuid,
            ),
        )
        assert dict(cursor.fetchall()) == {
            message_uuid: True,
            second_message_uuid: False,
            third_message_uuid: True,
        }

    exact_unread_payload = {
        **read_payload,
        "message_uuids": [str(third_message_uuid)],
        "read": False,
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("read_state.set", exact_unread_payload, 3, sys_uuid.uuid4()),
            conftest.TEST_MAIL_RUNTIME,
        )
    boundary_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "reader_uuid": str(owner_uuid),
        "through_message_uuid": str(second_message_uuid),
        "read": True,
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("read_state.set", boundary_payload, 4, sys_uuid.uuid4()),
            conftest.TEST_MAIL_RUNTIME,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uuid, read FROM m_workspace_user_message_flags "
            "WHERE project_id = %s AND uuid = ANY(%s) AND user_uuid = %s",
            (
                project_uuid,
                [message_uuid, second_message_uuid, third_message_uuid],
                owner_uuid,
            ),
        )
        assert dict(cursor.fetchall()) == {
            message_uuid: True,
            second_message_uuid: True,
            third_message_uuid: False,
        }

    update_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "author_uuid": str(owner_uuid),
        "payload": {"kind": "markdown", "content": "updated"},
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("message.update", update_payload, 2),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert projection.messages[message_uuid]["payload"]["content"] == "updated"
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT payload->>'content', provider_metadata->>'revision' "
            "FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        assert cursor.fetchone() == ("updated", "2")
        cursor.execute(
            """
            SELECT payload->'provider'->>'revision'
            FROM m_workspace_events
            WHERE project_id = %s AND object_type = 'message'
              AND action = 'updated' AND payload->>'uuid' = %s
            ORDER BY epoch_version DESC LIMIT 1
            """,
            (project_uuid, str(message_uuid)),
        )
        assert cursor.fetchone() == ("2",)

    delete_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "author_uuid": str(owner_uuid),
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("message.delete", delete_payload, 3),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert message_uuid not in projection.messages
    assert projection.message_tombstones[message_uuid]["source_name"] == "zulip"
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_messages WHERE uuid = %s",
            (message_uuid,),
        )
        assert cursor.fetchone()[0] == 0
    delete_topic_payload = {
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
    }
    with session_factory() as session:
        external_bridge_data_plane._apply_inbound_operation(
            session,
            record("topic.delete", delete_topic_payload, 4, topic_uuid),
            conftest.TEST_MAIL_RUNTIME,
        )
    projection = conftest.TEST_MAIL_RUNTIME._repository(project_uuid).projection
    assert topic_uuid not in projection.topics
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_stream_topics WHERE uuid = %s",
            (topic_uuid,),
        )
        assert cursor.fetchone()[0] == 0


def test_ingress_quarantines_poison_advances_cursor_and_survives_restart(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(db, project_uuid, owner_uuid, "Ingress")
    )
    topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db, project_uuid, stream_uuid, owner_uuid, "general", is_default=True
        )
    )
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip', '{}'::jsonb, TRUE, 'live', TRUE,
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2
                (uuid, external_account_uuid, owner_user_uuid, provider,
                 provider_chat_id, source, display_name, selected, project_id,
                 projection_stream_uuid, status, capabilities)
            VALUES (%s, %s, %s, 'zulip', 'channel:42',
                    '{"kind":"zulip","chat_type":"channel","description":"",'
                    '"private":false,"participants":[],"topics":[]}'::jsonb,
                    'Ingress',
                    TRUE, %s, %s, 'live',
                    '{"messenger.message.send":{"available":true}}'::jsonb)
            """,
            (chat_uuid, account_uuid, owner_uuid, project_uuid, stream_uuid),
        )
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2
                (uuid, provider, identity_generation, status)
            VALUES (%s, 'zulip', 1, 'active')
            """,
            (instance_uuid,),
        )
    secret = "ingress enrollment secret"
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        outbox_uuid = external_bridge_data_plane.queue_message_create(
            session,
            project_uuid=project_uuid,
            owner_user_uuid=owner_uuid,
            message={
                "uuid": sys_uuid.uuid4(),
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "payload": {"kind": "markdown", "content": "deliver"},
            },
            realm_uuid=realm_uuid,
            bridge_instance_uuid=instance_uuid,
            identity_generation=1,
            enrollment_secret=secret,
        )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT raw_message FROM m_external_bridge_mail_outbox_v1 "
            "WHERE record_uuid = %s",
            (outbox_uuid,),
        )
        operation_raw = bytes(cursor.fetchone()[0])
    workspace_key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "workspace-to-zulip"
    )
    operation = external_bridge_codec.parse_message(
        operation_raw,
        "workspace-to-zulip",
        [workspace_key],
        external_bridge_data_plane.WORKSPACE_SENDER,
        external_bridge_data_plane.BRIDGE_ADDRESS,
    )
    result = {name: value for name, value in operation.items() if name != "operation"}
    result.update(
        {
            "record_kind": "result",
            "record_uuid": str(sys_uuid.uuid4()),
            "in_reply_to_record_uuid": operation["record_uuid"],
            "created_at": "2026-07-17T16:00:00Z",
            "result": {
                "outcome": "committed",
                "committed_at": "2026-07-17T16:00:00Z",
                "provider_entity_id": "9001",
                "provider_revision": "1",
                "safe_error": None,
                "manual_retry_allowed": False,
            },
        }
    )
    ingress_key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "zulip-to-workspace"
    )
    valid_raw = external_bridge_codec.build_message(
        result,
        "zulip-to-workspace",
        ingress_key,
        external_bridge_data_plane.BRIDGE_ADDRESS,
        external_bridge_data_plane.INGRESS_ADDRESS,
    )
    invalid_raw = valid_raw.replace(
        b"X-Workspace-Bridge-Signature: v1=",
        b"X-Workspace-Bridge-Signature: v1=A",
        1,
    )
    inbound = {
        **operation,
        "record_uuid": str(sys_uuid.uuid4()),
        "operation_uuid": str(sys_uuid.uuid4()),
        "operation_sha256": "0" * 64,
        "origin": "zulip",
        "causal_lane": f"provider-chat:{account_uuid}:{chat_uuid}",
        "sequence": 1,
        "predecessor_operation_uuid": None,
        "operation": {
            **operation["operation"],
            "entity_uuid": str(sys_uuid.uuid4()),
            "extensions": {"delivery_class": "live"},
            "provider": {
                "kind": "zulip",
                "chat_id": "channel:42",
                "entity_id": "9010",
                "revision": "1",
            },
            "payload": {
                **operation["operation"]["payload"],
                "author_uuid": str(owner_uuid),
                "payload": {
                    "kind": "markdown",
                    "content": "inbound from Zulip",
                },
            },
        },
    }
    inbound["operation_sha256"] = external_bridge_codec.operation_sha256(inbound)
    inbound_raw = external_bridge_codec.build_message(
        inbound,
        "zulip-to-workspace",
        ingress_key,
        external_bridge_data_plane.BRIDGE_ADDRESS,
        external_bridge_data_plane.INGRESS_ADDRESS,
    )
    messages = {
        1: mail_protocol.FetchedMessage(1, frozenset(), invalid_raw),
        2: mail_protocol.FetchedMessage(2, frozenset(), valid_raw),
        3: mail_protocol.FetchedMessage(3, frozenset(), inbound_raw),
    }
    delivered = []

    class Client:
        def select(self, path, readonly=True):
            assert path == "INBOX" and readonly
            return mail_protocol.MailboxMetadata(77, 3, None)

        def search(self, criteria):
            first = int(criteria.split()[1].split(":", 1)[0])
            return [uid for uid in sorted(messages) if uid >= first]

        def fetch(self, uids):
            return [messages[uid] for uid in uids]

    class Runtime:
        @contextlib.contextmanager
        def external_bridge_ingress(self):
            yield Client()

        @contextlib.contextmanager
        def messenger_service(self, target_project_uuid):
            assert target_project_uuid == project_uuid

            class Repository:
                def refresh(self):
                    return None

            class Service:
                repository = Repository()

                def deliver_message(self, record):
                    delivered.append(record)

            yield Service()

    args = {
        "realm_uuid": realm_uuid,
        "bridge_instance_uuid": instance_uuid,
        "identity_generation": 1,
        "enrollment_secret": secret,
    }
    assert _consume_ingress(session_factory, Runtime(), **args) == 3
    assert _consume_ingress(session_factory, Runtime(), **args) == 0
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT uid, reason, length(raw_sha256) "
            "FROM m_external_bridge_mail_quarantine_v1 "
            "WHERE bridge_instance_uuid = %s",
            (instance_uuid,),
        )
        assert cursor.fetchall() == [(1, "invalid_record", 64)]
        cursor.execute(
            "SELECT uid_validity, last_uid "
            "FROM m_external_bridge_mail_cursors_v1 "
            "WHERE bridge_instance_uuid = %s",
            (instance_uuid,),
        )
        assert cursor.fetchall() == [(77, 3)]
        cursor.execute(
            "SELECT status, details->>'provider_entity_id' "
            "FROM m_external_operations_v2 WHERE uuid = %s",
            (operation["operation_uuid"],),
        )
        assert cursor.fetchone() == ("succeeded", "9001")
        cursor.execute(
            "SELECT provider_external_id, provider_metadata->>'kind', "
            "payload->>'content' FROM m_workspace_messages WHERE uuid = %s",
            (inbound["operation"]["entity_uuid"],),
        )
        assert cursor.fetchone() == ("9010", "zulip", "inbound from Zulip")
        cursor.execute(
            "SELECT raw_message FROM m_external_bridge_mail_outbox_v1 "
            "WHERE operation_uuid = %s",
            (inbound["operation_uuid"],),
        )
        committed_raw = bytes(cursor.fetchone()[0])
    committed = external_bridge_codec.parse_message(
        committed_raw,
        "workspace-to-zulip",
        [workspace_key],
        external_bridge_data_plane.WORKSPACE_SENDER,
        external_bridge_data_plane.BRIDGE_ADDRESS,
    )
    assert committed["in_reply_to_record_uuid"] == inbound["record_uuid"]
    assert committed["operation_sha256"] == inbound["operation_sha256"]
    assert committed["causal_lane"] == inbound["causal_lane"]
    assert committed["sequence"] == inbound["sequence"]
    assert committed["result"]["outcome"] == "committed"
    assert len(delivered) == 1
    assert str(delivered[0].entity_uuid) == inbound["operation"]["entity_uuid"]


def test_ingress_causal_lanes_are_gapless_restart_safe_and_independent(_database, db):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.uuid4()
    topic_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2
                (uuid, owner_user_uuid, provider, settings,
                 credential_present, status, live_ready, capabilities)
            VALUES (%s, %s, 'zulip', '{}'::jsonb, TRUE, 'live', TRUE,
                    '{}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )

    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    secret = "causal lane enrollment secret"
    key = external_bridge_codec.derive_direction_key(
        secret, realm_uuid, instance_uuid, 1, "zulip-to-workspace"
    )
    lane_a = f"provider-chat:{account_uuid}:a"
    lane_b = f"provider-chat:{account_uuid}:b"
    operation_a1 = sys_uuid.uuid4()
    operation_a2 = sys_uuid.uuid4()
    operation_b1 = sys_uuid.uuid4()

    def signed_record(uid, lane, sequence, operation_uuid, predecessor, attempt=1):
        record = {
            "schema": external_bridge_codec.SCHEMA,
            "schema_version": external_bridge_codec.SCHEMA_VERSION,
            "record_kind": "operation",
            "record_uuid": str(sys_uuid.uuid4()),
            "operation_uuid": str(operation_uuid),
            "attempt": attempt,
            "operation_sha256": "0" * 64,
            "account_uuid": str(account_uuid),
            "project_uuid": str(project_uuid),
            "origin": "zulip",
            "causal_lane": lane,
            "sequence": sequence,
            "predecessor_operation_uuid": (
                None if predecessor is None else str(predecessor)
            ),
            "created_at": f"2026-07-17T18:00:0{uid}Z",
            "expires_at": None,
            "operation": {
                "kind": "message.create",
                "entity_uuid": str(sys_uuid.uuid4()),
                "actor_uuid": str(owner_uuid),
                "occurred_at": f"2026-07-17T18:00:0{uid}Z",
                "provider": {
                    "kind": "zulip",
                    "chat_id": lane.rsplit(":", 1)[-1],
                    "entity_id": str(1000 + uid),
                    "revision": "1",
                },
                "payload": {
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "author_uuid": str(owner_uuid),
                    "payload": {"kind": "markdown", "content": f"message {uid}"},
                    "reply_to_message_uuid": None,
                },
                "extensions": {"delivery_class": "live"},
            },
        }
        record["operation_sha256"] = external_bridge_codec.operation_sha256(record)
        raw = external_bridge_codec.build_message(
            record,
            "zulip-to-workspace",
            key,
            external_bridge_data_plane.BRIDGE_ADDRESS,
            external_bridge_data_plane.INGRESS_ADDRESS,
        )
        return record, mail_protocol.FetchedMessage(uid, frozenset(), raw)

    record_a2, fetched_a2 = signed_record(1, lane_a, 2, operation_a2, operation_a1)
    _record_b1, fetched_b1 = signed_record(2, lane_b, 1, operation_b1, None)
    _record_a1, fetched_a1 = signed_record(3, lane_a, 1, operation_a1, None)
    retry_a2 = dict(record_a2)
    retry_a2["record_uuid"] = str(sys_uuid.uuid4())
    retry_a2["attempt"] = 2
    retry_raw = external_bridge_codec.build_message(
        retry_a2,
        "zulip-to-workspace",
        key,
        external_bridge_data_plane.BRIDGE_ADDRESS,
        external_bridge_data_plane.INGRESS_ADDRESS,
    )
    fetched_retry = mail_protocol.FetchedMessage(4, frozenset(), retry_raw)
    messages = {1: fetched_a2, 2: fetched_b1}
    applied = []

    class Client:
        def select(self, path, readonly=True):
            assert path == "INBOX" and readonly
            return mail_protocol.MailboxMetadata(123, len(messages), None)

        def search(self, criteria):
            first = int(criteria.split()[1].split(":", 1)[0])
            return [uid for uid in sorted(messages) if uid >= first]

        def fetch(self, uids):
            return [messages[uid] for uid in uids]

    class Runtime:
        @contextlib.contextmanager
        def external_bridge_ingress(self):
            yield Client()

    def handler(_session, record):
        applied.append(sys_uuid.UUID(record["operation_uuid"]))

    session_factory = engines.engine_factory.get_engine().session_manager
    args = {
        "realm_uuid": realm_uuid,
        "bridge_instance_uuid": instance_uuid,
        "identity_generation": 1,
        "enrollment_secret": secret,
        "operation_handler": handler,
        "limit": 10,
    }
    assert _consume_ingress(session_factory, Runtime(), **args) == 2
    assert applied == [operation_b1]
    with db.cursor() as cursor:
        cursor.execute("SELECT sequence FROM m_external_bridge_mail_pending_v1")
        assert cursor.fetchall() == [(2,)]

    messages[3] = fetched_a1
    assert _consume_ingress(session_factory, Runtime(), **args) == 1
    assert applied == [operation_b1, operation_a1, operation_a2]

    messages[4] = fetched_retry
    assert _consume_ingress(session_factory, Runtime(), **args) == 1
    assert applied == [operation_b1, operation_a1, operation_a2]
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT causal_lane, last_sequence, last_operation_uuid "
            "FROM m_external_bridge_mail_lanes_v1 "
            "WHERE external_account_uuid = %s AND origin = 'zulip' "
            "ORDER BY causal_lane",
            (account_uuid,),
        )
        assert cursor.fetchall() == [
            (lane_a, 2, operation_a2),
            (lane_b, 1, operation_b1),
        ]
        cursor.execute("SELECT COUNT(*) FROM m_external_bridge_mail_pending_v1")
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_bridge_mail_records_v1 "
            "WHERE direction = 'zulip-to-workspace' "
            "AND external_account_uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 3
        cursor.execute(
            "SELECT last_uid FROM m_external_bridge_mail_cursors_v1 "
            "WHERE bridge_instance_uuid = %s AND mailbox = 'INBOX'",
            (instance_uuid,),
        )
        assert cursor.fetchone()[0] == 4
