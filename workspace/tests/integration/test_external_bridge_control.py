# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import datetime
import hashlib
import json
from pathlib import Path
import uuid as sys_uuid

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import engines

from workspace.external_bridge_control import file_repository
from workspace.external_bridge_control import pki
from workspace.external_bridge_control import sql_state
from workspace.messenger_api.dm import external_models
from workspace.messenger_api import file_storage
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
