# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import concurrent.futures
import contextlib
import datetime
import hashlib
import json
import threading
import time
import uuid as sys_uuid
from pathlib import Path

import pytest
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import engines

from workspace.external_bridge_control import (
    file_repository,
    identity_linking,
    pki,
    provider_data,
    provider_event_apply,
    sql_state,
)
from workspace.messenger_api import events as messenger_events
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import external_models, message_payloads
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


@pytest.mark.parametrize(
    "attachment_kind",
    ["provider", "native-stream", "native-public"],
)
def test_provider_message_move_migrates_canonical_row_and_dependents_between_projects(
    _database,
    db,
    monkeypatch,
    tmp_path,
    attachment_kind,
):
    monkeypatch.setenv(file_storage.ENV_STORAGE_PATH, str(tmp_path))
    identity = _identity(sys_uuid.uuid4(), sys_uuid.uuid4())
    account_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    source_project_uuid = sys_uuid.uuid4()
    destination_project_uuid = sys_uuid.uuid4()
    source_stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db, source_project_uuid, owner_uuid, "Provider source"
        )
    )
    source_topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db,
            source_project_uuid,
            source_stream_uuid,
            owner_uuid,
            "Source topic",
            is_default=True,
        )
    )
    destination_stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db, destination_project_uuid, owner_uuid, "Provider destination"
        )
    )
    destination_topic_uuid = sys_uuid.UUID(
        conftest.seed_stream_topic(
            db,
            destination_project_uuid,
            destination_stream_uuid,
            owner_uuid,
            "Destination topic",
            is_default=True,
        )
    )
    message_uuid = sys_uuid.uuid4()
    reaction_uuid = sys_uuid.uuid4()
    source_chat_uuid = sys_uuid.uuid4()
    destination_chat_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    file_operation_uuid = sys_uuid.uuid4()
    provider_attachment = attachment_kind == "provider"
    native_attachment = not provider_attachment
    public_attachment = attachment_kind == "native-public"
    file_data = b"cross-project provider attachment"
    file_sha256 = hashlib.sha256(file_data).hexdigest()
    storage_info = file_storage.save_workspace_file(
        file_uuid,
        file_data,
        storage_type="file",
        storage_object_id=(
            f"external-content/sha256/{file_sha256[:2]}/{file_sha256}"
            if provider_attachment
            else None
        ),
    )
    source_file_metadata = file_storage.WorkspaceFileMetadata(
        uuid=file_uuid,
        project_id=source_project_uuid,
        stream_uuid=None if public_attachment else source_stream_uuid,
        owner_uuid=owner_uuid,
        name="attachment.txt",
        description="",
        content_type="text/plain",
        size_bytes=len(file_data),
        sha256=file_sha256,
        created_at=datetime.datetime.now(datetime.timezone.utc),
        acl_mode="public" if public_attachment else "stream_members",
        origin=(
            None
            if native_attachment
            else {
                "kind": "external_provider",
                "provider_kind": "zulip",
                "external_account_uuid": str(account_uuid),
                "external_chat_uuid": str(source_chat_uuid),
                "operation_uuid": str(file_operation_uuid),
            }
        ),
    )
    file_storage.save_workspace_file_metadata(
        source_file_metadata,
        storage_type="file",
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_files (
                uuid, project_id, name, description, user_uuid, stream_uuid,
                acl_mode, external_account_uuid, content_type, size_bytes,
                hash, storage_type, storage_id, storage_object_id
            ) VALUES (
                %s, %s, 'attachment.txt', '', %s, %s, %s, %s,
                'text/plain', %s, %s, %s, %s, %s
            )
            """,
            (
                file_uuid,
                source_project_uuid,
                owner_uuid,
                None if public_attachment else source_stream_uuid,
                "public" if public_attachment else "stream",
                None if native_attachment else account_uuid,
                len(file_data),
                file_sha256,
                storage_info.storage_type,
                storage_info.storage_id,
                storage_info.storage_object_id,
            ),
        )
        if not public_attachment:
            cursor.execute(
                """
                INSERT INTO m_workspace_file_accesses (
                    uuid, project_id, file_uuid, user_uuid
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    sys_uuid.uuid4(),
                    source_project_uuid,
                    file_uuid,
                    owner_uuid,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload,
                source_name, source, provider_uuid, external_account_uuid,
                provider_external_id, provider_metadata
            ) VALUES (
                %s, %s, %s, %s, %s, %s::jsonb,
                'native', '{"kind":"native"}'::jsonb, %s, %s, '601',
                '{"provider_sequence":"10"}'::jsonb
            )
            """,
            (
                message_uuid,
                source_project_uuid,
                source_stream_uuid,
                source_topic_uuid,
                owner_uuid,
                json.dumps(
                    {
                        "kind": "markdown",
                        "content": f"attachment urn:file:{file_uuid}",
                    }
                ),
                identity.bridge_instance_uuid,
                account_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_message_flags (
                uuid, user_uuid, project_id, read
            ) VALUES (%s, %s, %s, false)
            """,
            (message_uuid, owner_uuid, source_project_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_message_reactions (
                uuid, project_id, message_uuid, user_uuid, emoji_name,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, 'heart', NOW(), NOW())
            """,
            (
                reaction_uuid,
                source_project_uuid,
                message_uuid,
                owner_uuid,
            ),
        )

    monkeypatch.setattr(
        provider_event_apply,
        "_ensure_projection_owner_stream",
        lambda *_args, **_kwargs: None,
    )
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(destination_chat_uuid),
        "project_id": str(destination_project_uuid),
        "provider_sequence": "11",
        "kind": "message.upsert",
        "payload": {
            "resource": {
                "uuid": str(message_uuid),
                "stream_uuid": str(destination_stream_uuid),
                "topic_uuid": str(destination_topic_uuid),
                "user_uuid": str(owner_uuid),
                "provider_external_id": "601",
                "provider_metadata": {"delivery_class": "backfill"},
            }
        },
    }
    assignment = {
        "owner_user_uuid": owner_uuid,
        "projection_stream_uuid": destination_stream_uuid,
        "provider_chat_id": "channel:43",
        "source": {"chat_type": "channel"},
        "account_settings": {"server_url": "https://zulip.example.test"},
    }
    resource = provider_event_apply._resource(event, identity, account_uuid)
    session_factory = engines.engine_factory.get_engine().session_manager
    source_payload = message_payloads.MarkdownPayload(
        content=f"attachment urn:file:{file_uuid}"
    )
    if native_attachment:
        with session_factory() as session:
            with pytest.raises(
                ValueError,
                match="Native message file is not attached to the source",
            ):
                provider_event_apply._reproject_message_payload_files(
                    session,
                    source_payload,
                    source_project_uuid,
                    source_stream_uuid,
                    destination_project_uuid,
                    destination_stream_uuid,
                    destination_chat_uuid,
                    account_uuid,
                    False,
                    native_source_payload=message_payloads.MarkdownPayload(
                        content="no attachment"
                    ),
                )
    with session_factory() as session:
        first_projection = provider_event_apply._reproject_message_payload_files(
            session,
            source_payload,
            source_project_uuid,
            source_stream_uuid,
            destination_project_uuid,
            destination_stream_uuid,
            destination_chat_uuid,
            account_uuid,
            False,
            native_source_payload=source_payload,
        )
    with session_factory() as session:
        repeated_projection = provider_event_apply._reproject_message_payload_files(
            session,
            source_payload,
            source_project_uuid,
            source_stream_uuid,
            destination_project_uuid,
            destination_stream_uuid,
            destination_chat_uuid,
            account_uuid,
            False,
            native_source_payload=source_payload,
        )
    assert repeated_projection == first_projection

    with session_factory() as session:
        provider_event_apply._message_event(
            session,
            event,
            destination_project_uuid,
            assignment,
            resource,
            identity,
        )
    with session_factory() as session:
        provider_event_apply._message_event(
            session,
            event,
            destination_project_uuid,
            assignment,
            resource,
            identity,
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT project_id, stream_uuid, topic_uuid, payload
            FROM m_workspace_messages WHERE uuid = %s
            """,
            (message_uuid,),
        )
        moved_message = cursor.fetchone()
        assert moved_message[:3] == (
            destination_project_uuid,
            destination_stream_uuid,
            destination_topic_uuid,
        )
        moved_content = moved_message[3]["content"]
        destination_file_uuid = sys_uuid.UUID(
            moved_content.removeprefix("attachment urn:file:")
        )
        if public_attachment:
            assert destination_file_uuid == file_uuid
        else:
            assert destination_file_uuid != file_uuid
        destination_storage_object_id = (
            storage_info.storage_object_id
            if public_attachment or provider_attachment
            else file_storage.get_workspace_file_object_id(destination_file_uuid)
        )
        cursor.execute(
            """
            SELECT project_id FROM m_workspace_user_message_flags
            WHERE uuid = %s AND user_uuid = %s
            """,
            (message_uuid, owner_uuid),
        )
        assert cursor.fetchone() == (destination_project_uuid,)
        cursor.execute(
            """
            SELECT project_id, stream_uuid, storage_type, storage_id,
                   storage_object_id
            FROM m_workspace_files WHERE uuid = %s
            """,
            (file_uuid,),
        )
        assert cursor.fetchone() == (
            source_project_uuid,
            None if public_attachment else source_stream_uuid,
            storage_info.storage_type,
            storage_info.storage_id,
            storage_info.storage_object_id,
        )
        cursor.execute(
            """
            SELECT project_id, stream_uuid
            FROM m_workspace_visible_files_v1
            WHERE uuid = %s AND (
                viewer_user_uuid = %s
                OR (%s AND viewer_user_uuid IS NULL)
            )
            """,
            (file_uuid, owner_uuid, public_attachment),
        )
        assert cursor.fetchone() == (
            source_project_uuid,
            None if public_attachment else source_stream_uuid,
        )
        cursor.execute(
            """
            SELECT project_id, stream_uuid, storage_type, storage_id,
                   storage_object_id
            FROM m_workspace_files WHERE uuid = %s
            """,
            (destination_file_uuid,),
        )
        assert cursor.fetchone() == (
            (
                source_project_uuid,
                None,
                storage_info.storage_type,
                storage_info.storage_id,
                destination_storage_object_id,
            )
            if public_attachment
            else (
                destination_project_uuid,
                destination_stream_uuid,
                storage_info.storage_type,
                storage_info.storage_id,
                destination_storage_object_id,
            )
        )
        cursor.execute(
            """
            SELECT count(*) FROM m_workspace_files
            WHERE project_id = %s AND hash = %s
            """,
            (destination_project_uuid, file_sha256),
        )
        assert cursor.fetchone() == ((0,) if public_attachment else (1,))
        cursor.execute(
            """
            SELECT project_id, user_uuid
            FROM m_workspace_file_accesses WHERE file_uuid = %s
            """,
            (destination_file_uuid,),
        )
        assert cursor.fetchall() == (
            [] if public_attachment else [(destination_project_uuid, owner_uuid)]
        )
        cursor.execute(
            """
            SELECT project_id, stream_uuid
            FROM m_workspace_visible_files_v1
            WHERE uuid = %s AND (
                viewer_user_uuid = %s
                OR (%s AND viewer_user_uuid IS NULL)
            )
            """,
            (destination_file_uuid, owner_uuid, public_attachment),
        )
        assert cursor.fetchone() == (
            (source_project_uuid, None)
            if public_attachment
            else (destination_project_uuid, destination_stream_uuid)
        )
        cursor.execute(
            """
            SELECT project_id FROM m_workspace_message_reactions
            WHERE uuid = %s
            """,
            (reaction_uuid,),
        )
        assert cursor.fetchone() == (destination_project_uuid,)

    destination_file_metadata = file_storage.read_workspace_file_metadata(
        destination_file_uuid,
        storage_type="file",
    )
    assert destination_file_metadata.project_id == (
        source_project_uuid if public_attachment else destination_project_uuid
    )
    assert destination_file_metadata.stream_uuid == (
        None if public_attachment else destination_stream_uuid
    )
    assert destination_file_metadata.origin == (
        None
        if native_attachment
        else {
            **source_file_metadata.origin,
            "external_chat_uuid": str(destination_chat_uuid),
        }
    )
    assert (
        file_storage.read_workspace_file(
            destination_file_uuid,
            storage_type="file",
            storage_object_id=destination_storage_object_id,
        )
        == file_data
    )


def test_account_global_identity_event_uses_account_authorization(_database, db):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    identity_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, "identity-owner")
    settings = {
        "kind": "zulip",
        "server_url": "https://zulip.example.test",
        "selection_mode": "explicit",
        "history_depth": "30_days",
        "default_project_id": str(project_uuid),
    }
    desired = {
        "resource_type": "external_account",
        "uuid": str(account_uuid),
        "generation": 1,
        "owner_user_uuid": str(owner_uuid),
        "settings": settings,
        "synchronization_enabled": True,
        "credential_envelope": None,
    }
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status, last_heartbeat_at
            ) VALUES (%s, 'zulip', 1, 'active', NOW())
            """,
            (instance_uuid,),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status
            ) VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live')
            """,
            (account_uuid, owner_uuid, sql_state._json(settings)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_desired_resources_v1 (
                bridge_instance_uuid, provider_kind, resource_type,
                resource_uuid, operation, generation, resource
            ) VALUES (%s, 'zulip', 'external_account', %s, 'upsert', 1, %s::jsonb)
            """,
            (instance_uuid, account_uuid, sql_state._json(desired)),
        )
    event = {
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "external_account_uuid": str(account_uuid),
        "external_chat_uuid": str(sys_uuid.uuid4()),
        "project_id": str(project_uuid),
        "provider_sequence": "1",
        "kind": "identity.upsert",
        "payload": {
            "resource": {
                "uuid": str(identity_uuid),
                "display_name": "Provider identity",
                "email": "identity@example.test",
                "avatar_urn": None,
                "active": True,
                "provider_external_id": "42",
                "provider_metadata": {"chat_key": "account"},
            }
        },
    }
    identity = _identity(instance_uuid, realm_uuid)
    session_factory = engines.engine_factory.get_engine().session_manager

    with session_factory() as session:
        response = provider_data.apply_provider_event_batch(
            session,
            identity,
            [event],
            provider_event_apply.apply_event,
        )

    assert response["results"][0]["status"] == "applied"
    assert response["results"][0]["target_uuid"] == str(identity_uuid)
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT source, external_account_uuid, provider_external_id
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (identity_uuid,),
        )
        assert cursor.fetchone() == ("zulip", account_uuid, "42")
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_chats_v2 WHERE external_account_uuid = %s",
            (account_uuid,),
        )
        assert cursor.fetchone()[0] == 0

    foreign_project_event = {
        **event,
        "provider_event_uuid": str(sys_uuid.uuid4()),
        "project_id": str(sys_uuid.uuid4()),
    }
    with session_factory() as session:
        with pytest.raises(provider_data.ProviderBatchError, match="not assigned"):
            provider_data.apply_provider_event_batch(
                session,
                identity,
                [foreign_project_event],
                provider_event_apply.apply_event,
            )


def test_verified_direct_catalog_merges_existing_provider_chat_into_native_dm(
    _database,
    db,
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    peer_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    native_stream_uuid = sys_uuid.uuid4()
    native_topic_uuid = sys_uuid.uuid4()
    native_named_zulip_topic_uuid = sys_uuid.uuid4()
    provider_stream_uuid = sql_state.external_chat_projection_stream_uuid(chat_uuid)
    provider_topic_uuid = sql_state._projection_uuid(
        chat_uuid,
        "topic",
        "direct:7,8:default",
    )
    native_message_uuid = sys_uuid.uuid4()
    native_named_zulip_message_uuid = sys_uuid.uuid4()
    message_uuid = sys_uuid.uuid4()
    provider_draft_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, "merge-owner")
    conftest.seed_workspace_user(db, peer_uuid, "merge-peer")
    settings = {
        "kind": "zulip",
        "server_url": "https://zulip.example.test",
        "selection_mode": "explicit",
        "history_depth": "30_days",
        "default_project_id": str(project_uuid),
    }
    private_index = ":".join(sorted((str(owner_uuid), str(peer_uuid))))
    existing_source = {
        "kind": "zulip",
        "provider_realm_uuid": str(realm_uuid),
        "provider_owner_user_id": "7",
        "chat_type": "personal",
        "description": "",
        "participants": [
            {
                "identity_uuid": str(owner_uuid),
                "provider_user_id": "7",
                "display_name": "Owner",
                "email": "owner@example.test",
                "avatar_urn": None,
                "role": "owner",
            },
            {
                "identity_uuid": str(peer_uuid),
                "provider_user_id": "8",
                "display_name": "Peer",
                "email": "peer@example.test",
                "avatar_urn": None,
                "role": "member",
            },
        ],
        "topics": [
            {
                "topic_uuid": str(provider_topic_uuid),
                "provider_topic_id": "direct:7,8:default",
                "name": "default",
                "is_default": True,
            }
        ],
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
                    '{"max_selected_chats_per_account":10}'::jsonb)
            """,
            (sys_uuid.uuid4(),),
        )
        cursor.execute(
            """
            INSERT INTO m_external_provider_identity_links_v1 (
                provider, provider_realm_uuid, provider_user_id,
                workspace_user_uuid, link_kind
            ) VALUES ('zulip', %s, '8', %s, 'provider_identity')
            """,
            (realm_uuid, peer_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_streams (
                uuid, name, description, source_name, source, user_uuid,
                project_id, private, invite_only, direct_user_uuid,
                private_index
            ) VALUES (
                %s, 'Peer', '', 'native', '{"kind":"native"}'::jsonb, %s,
                %s, TRUE, TRUE, %s, %s
            )
            """,
            (
                native_stream_uuid,
                owner_uuid,
                project_uuid,
                peer_uuid,
                private_index,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_topics (
                uuid, project_id, name, stream_uuid
            ) VALUES (%s, %s, 'General Topic', %s)
            """,
            (native_topic_uuid, project_uuid, native_stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_topics (
                uuid, project_id, name, stream_uuid
            ) VALUES (%s, %s, 'Zulip', %s)
            """,
            (native_named_zulip_topic_uuid, project_uuid, native_stream_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET default_topic_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (native_topic_uuid, project_uuid, native_stream_uuid),
        )
        for user_uuid in (owner_uuid, peer_uuid):
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid, role
                ) VALUES (%s, %s, %s, %s, %s, 'owner')
                """,
                (
                    sys_uuid.uuid4(),
                    project_uuid,
                    native_stream_uuid,
                    user_uuid,
                    owner_uuid,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload
            ) VALUES
            (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"native history"}'::jsonb
            ),
            (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"user topic history"}'::jsonb
            )
            """,
            (
                native_message_uuid,
                project_uuid,
                native_stream_uuid,
                native_topic_uuid,
                owner_uuid,
                native_named_zulip_message_uuid,
                project_uuid,
                native_stream_uuid,
                native_named_zulip_topic_uuid,
                owner_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_streams (
                uuid, name, description, source_name, source, user_uuid,
                project_id, private, invite_only, external_account_uuid,
                provider_external_id
            ) VALUES (
                %s, 'Peer', '', 'zulip',
                %s::jsonb, %s, %s, TRUE, TRUE, %s, 'direct:7,8'
            )
            """,
            (
                provider_stream_uuid,
                sql_state._json(
                    {
                        "kind": "zulip",
                        "stream_id": 0,
                        "server_url": settings["server_url"],
                        "source_scope": str(account_uuid),
                    }
                ),
                owner_uuid,
                project_uuid,
                account_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_topics (
                uuid, project_id, name, stream_uuid
            ) VALUES (%s, %s, 'default', %s)
            """,
            (provider_topic_uuid, project_uuid, provider_stream_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET default_topic_uuid = %s
            WHERE project_id = %s AND uuid = %s
            """,
            (provider_topic_uuid, project_uuid, provider_stream_uuid),
        )
        for user_uuid in (owner_uuid, peer_uuid):
            cursor.execute(
                """
                INSERT INTO m_workspace_stream_bindings (
                    uuid, project_id, stream_uuid, user_uuid, who_uuid, role
                ) VALUES (%s, %s, %s, %s, %s, 'member')
                """,
                (
                    sys_uuid.uuid4(),
                    project_uuid,
                    provider_stream_uuid,
                    user_uuid,
                    owner_uuid,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload,
                external_account_uuid, provider_external_id
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"provider history"}'::jsonb,
                %s, '123'
            )
            """,
            (
                message_uuid,
                project_uuid,
                provider_stream_uuid,
                provider_topic_uuid,
                peer_uuid,
                account_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_drafts (
                uuid, project_id, user_uuid, stream_uuid, topic_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"provider draft"}'::jsonb
            )
            """,
            (
                provider_draft_uuid,
                project_uuid,
                owner_uuid,
                provider_stream_uuid,
                provider_topic_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid, status
            ) VALUES (
                %s, %s, %s, 'zulip', 'direct:7,8', %s::jsonb, 'Peer',
                FALSE, NULL, %s, 'available'
            )
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                sql_state._json(existing_source),
                provider_stream_uuid,
            ),
        )

    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    identity = _identity(instance_uuid, realm_uuid)
    with engines.engine_factory.get_engine().session_manager() as session:
        sql_state.append_upsert(
            session,
            instance_uuid,
            "zulip",
            {
                "resource_type": "external_account",
                "uuid": str(account_uuid),
                "generation": 1,
            },
        )
    observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report = {
        "report_uuid": str(sys_uuid.uuid4()),
        "resource_type": "external_chat_catalog",
        "resource_uuid": str(chat_uuid),
        "observed_generation": 1,
        "status": "ready",
        "progress": {
            "phase": "discovery",
            "completed": 1,
            "total": 1,
            "last_progress_at": observed_at,
        },
        "safe_error": None,
        "observed_at": observed_at,
        "catalog": {
            "operation": "upsert",
            "external_account_uuid": str(account_uuid),
            "owner_user_uuid": str(owner_uuid),
            "provider_kind": "zulip",
            "project_id": str(project_uuid),
            "source": {
                "kind": "zulip",
                "chat_type": "direct",
                "provider_chat_key": "direct:7,8",
                "provider_realm_uuid": str(realm_uuid),
                "provider_owner_user_id": "7",
            },
            "display_name": "Peer",
            "description": "",
            "participants": [
                {
                    "provider_user_id": "7",
                    "display_name": "Owner",
                    "email": "owner@example.test",
                    "avatar_urn": None,
                    "is_owner": True,
                },
                {
                    "provider_user_id": "8",
                    "display_name": "Peer",
                    "email": "peer@example.test",
                    "avatar_urn": None,
                    "is_owner": False,
                },
            ],
            "topics": [
                {
                    "provider_topic_id": "direct:7,8:default",
                    "name": "default",
                    "is_default": True,
                }
            ],
            "capabilities": {"messenger.message.send": {"available": True}},
        },
    }
    result = _request_call(repository.observed_reports, identity, [report])
    assert result["results"][0]["status"] == "applied"
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT selected, project_id, projection_stream_uuid
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone() == (False, None, provider_stream_uuid)
        cursor.execute(
            """
            SELECT stream.default_topic_uuid, topic.name
            FROM m_workspace_streams AS stream
            JOIN m_workspace_stream_topics AS topic
              ON topic.uuid = stream.default_topic_uuid
            WHERE stream.project_id = %s AND stream.uuid = %s
            """,
            (project_uuid, native_stream_uuid),
        )
        assert cursor.fetchone() == (native_topic_uuid, "General Topic")
        cursor.execute(
            """
            SELECT stream_uuid, topic_uuid
            FROM m_workspace_drafts WHERE uuid = %s
            """,
            (provider_draft_uuid,),
        )
        assert cursor.fetchone() == (provider_stream_uuid, provider_topic_uuid)
        cursor.execute(
            """
            UPDATE m_external_accounts_v2
            SET settings = jsonb_set(settings, '{selection_mode}', '"all"')
            WHERE uuid = %s
            """,
            (account_uuid,),
        )

    report["report_uuid"] = str(sys_uuid.uuid4())
    result = _request_call(repository.observed_reports, identity, [report])
    assert result["results"][0]["status"] == "applied"

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT projection_stream_uuid, source
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        projection_stream_uuid, source = cursor.fetchone()
        assert projection_stream_uuid == native_stream_uuid
        assert source["topics"] == [
            {
                "topic_uuid": source["topics"][0]["topic_uuid"],
                "provider_topic_id": "direct:7,8:default",
                "name": "Zulip",
                "is_default": True,
            }
        ]
        zulip_topic_uuid = sys_uuid.UUID(source["topics"][0]["topic_uuid"])
        assert zulip_topic_uuid == native_topic_uuid
        cursor.execute(
            """
            SELECT uuid, stream_uuid, topic_uuid
            FROM m_workspace_messages WHERE uuid = ANY(%s)
            ORDER BY uuid
            """,
            ([message_uuid, native_message_uuid],),
        )
        assert cursor.fetchall() == sorted(
            [
                (message_uuid, native_stream_uuid, native_topic_uuid),
                (native_message_uuid, native_stream_uuid, native_topic_uuid),
            ]
        )
        cursor.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_messages WHERE uuid = %s
            """,
            (native_named_zulip_message_uuid,),
        )
        assert cursor.fetchone()[0] == native_named_zulip_topic_uuid
        cursor.execute(
            """
            SELECT stream_uuid, topic_uuid
            FROM m_workspace_drafts WHERE uuid = %s
            """,
            (provider_draft_uuid,),
        )
        assert cursor.fetchone() == (native_stream_uuid, native_topic_uuid)
        cursor.execute(
            """
            SELECT stream.default_topic_uuid, topic.name
            FROM m_workspace_streams AS stream
            JOIN m_workspace_stream_topics AS topic
              ON topic.uuid = stream.default_topic_uuid
            WHERE stream.project_id = %s AND stream.uuid = %s
            """,
            (project_uuid, native_stream_uuid),
        )
        assert cursor.fetchone() == (native_topic_uuid, "Zulip")
        cursor.execute(
            """
            SELECT is_archived
            FROM m_workspace_streams WHERE uuid = %s
            """,
            (provider_stream_uuid,),
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            """
            SELECT resource
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (chat_uuid,),
        )
        assignment = cursor.fetchone()[0]
        assert assignment["workspace_projection"]["stream"]["uuid"] == str(
            native_stream_uuid
        )
        assert assignment["workspace_projection"]["topics"] == [
            {
                "topic_uuid": str(zulip_topic_uuid),
                "provider_topic_id": "direct:7,8:default",
                "name": "Zulip",
                "is_default": True,
            }
        ]

    stale_topic_uuid = sys_uuid.uuid4()
    stale_message_uuid = sys_uuid.uuid4()
    split_draft_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE m_external_chats_v2
            SET source = jsonb_set(
                    source,
                    '{topics,0,topic_uuid}',
                    to_jsonb(%s::text)
                )
            WHERE uuid = %s
            """,
            (str(stale_topic_uuid), chat_uuid),
        )
        cursor.execute(
            """
            UPDATE m_workspace_stream_topics
            SET name = 'General Topic'
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, native_topic_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_topics (
                uuid, project_id, name, stream_uuid
            ) VALUES (%s, %s, 'Zulip', %s)
            """,
            (stale_topic_uuid, project_uuid, native_stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"split history"}'::jsonb
            )
            """,
            (
                stale_message_uuid,
                project_uuid,
                native_stream_uuid,
                stale_topic_uuid,
                peer_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_user_topic_flags (
                uuid, user_uuid, project_id, is_done
            ) VALUES (%s, %s, %s, TRUE)
            """,
            (stale_topic_uuid, peer_uuid, project_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_drafts (
                uuid, project_id, user_uuid, stream_uuid, topic_uuid, payload
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"split draft"}'::jsonb
            )
            """,
            (
                split_draft_uuid,
                project_uuid,
                owner_uuid,
                native_stream_uuid,
                native_topic_uuid,
            ),
        )

    with engines.engine_factory.get_engine().session_manager() as session:
        assert (
            sql_state.repair_external_chat_assignments(
                session,
                account_uuid,
                instance_uuid,
                "zulip",
            )
            == 1
        )
    with engines.engine_factory.get_engine().session_manager() as session:
        assert (
            sql_state.repair_external_chat_assignments(
                session,
                account_uuid,
                instance_uuid,
                "zulip",
            )
            == 0
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_topics
            WHERE stream_uuid = %s AND name = 'Zulip'
            """,
            (native_stream_uuid,),
        )
        assert cursor.fetchone()[0] == 2
        cursor.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_messages WHERE uuid = ANY(%s)
            """,
            ([message_uuid, native_message_uuid, stale_message_uuid],),
        )
        assert {row[0] for row in cursor.fetchall()} == {stale_topic_uuid}
        cursor.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_messages WHERE uuid = %s
            """,
            (native_named_zulip_message_uuid,),
        )
        assert cursor.fetchone()[0] == native_named_zulip_topic_uuid
        cursor.execute(
            """
            SELECT topic_uuid
            FROM m_workspace_drafts WHERE uuid = ANY(%s)
            """,
            ([provider_draft_uuid, split_draft_uuid],),
        )
        assert {row[0] for row in cursor.fetchall()} == {stale_topic_uuid}
        cursor.execute(
            """
            SELECT is_done
            FROM m_workspace_user_topic_flags
            WHERE uuid = %s AND user_uuid = %s
            """,
            (stale_topic_uuid, peer_uuid),
        )
        assert cursor.fetchone()[0] is True
        cursor.execute(
            """
            SELECT default_topic_uuid
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, native_stream_uuid),
        )
        assert cursor.fetchone()[0] == stale_topic_uuid
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_topics
            WHERE stream_uuid = %s
            """,
            (native_stream_uuid,),
        )
        assert cursor.fetchone()[0] == 2
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_topics
            WHERE stream_uuid = %s AND uuid = %s
            """,
            (native_stream_uuid, native_named_zulip_topic_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT source
            FROM m_external_chats_v2 WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone()[0]["topics"][0]["topic_uuid"] == str(stale_topic_uuid)
        cursor.execute(
            """
            SELECT resource
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (chat_uuid,),
        )
        assignment = cursor.fetchone()[0]
        assert assignment["workspace_projection"]["topics"] == [
            {
                "topic_uuid": str(stale_topic_uuid),
                "provider_topic_id": "direct:7,8:default",
                "name": "Zulip",
                "is_default": True,
            }
        ]
        cursor.execute(
            "DELETE FROM m_external_provider_policies_v1 WHERE provider = 'zulip'"
        )


def test_verified_provider_identity_replaces_account_scoped_duplicates(
    _database,
    db,
):
    provider_realm_uuid = sys_uuid.uuid4()
    owner_a_uuid = sys_uuid.uuid4()
    owner_b_uuid = sys_uuid.uuid4()
    conflicting_owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_a_uuid = sys_uuid.uuid4()
    account_b_uuid = sys_uuid.uuid4()
    conflicting_account_uuid = sys_uuid.uuid4()
    legacy_user_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    account_b_chat_uuid = sys_uuid.uuid4()
    file_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db,
            project_uuid,
            owner_a_uuid,
            "Identity merge stream",
        )
    )
    conftest.seed_workspace_user(db, owner_b_uuid, "verified-owner-b")
    conftest.seed_workspace_user(
        db,
        conflicting_owner_uuid,
        "conflicting-owner",
    )
    settings = json.dumps(
        {
            "default_project_id": str(project_uuid),
            "server_url": "https://zulip.example.test",
        }
    )
    with db.cursor() as cursor:
        for account_uuid, owner_uuid in (
            (account_a_uuid, owner_a_uuid),
            (account_b_uuid, owner_b_uuid),
            (conflicting_account_uuid, conflicting_owner_uuid),
        ):
            cursor.execute(
                """
                INSERT INTO m_external_accounts_v2 (
                    uuid, owner_user_uuid, provider, settings,
                    credential_present, status
                ) VALUES (%s, %s, 'zulip', %s::jsonb, TRUE, 'live')
                """,
                (account_uuid, owner_uuid, settings),
            )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET external_account_uuid = %s,
                provider_external_id = 'direct:10,20',
                source_name = 'zulip',
                source = %s::jsonb
            WHERE project_id = %s AND uuid = %s
            """,
            (
                account_a_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "stream_id": 0,
                        "server_url": "https://zulip.example.test",
                        "source_scope": str(account_a_uuid),
                    }
                ),
                project_uuid,
                stream_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_users (
                uuid, username, source, status, avatar,
                provider_uuid, external_account_uuid, provider_external_id,
                created_at, updated_at, last_ping_at
            )
            SELECT %s, %s, 'zulip', 'active', avatar,
                   %s, %s, '20', NOW(), NOW(), NOW()
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (
                legacy_user_uuid,
                f"zulip-{legacy_user_uuid}",
                sys_uuid.uuid4(),
                account_a_uuid,
                owner_a_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                created_at, updated_at
            ) VALUES
                (%s, %s, %s, %s, %s, 'member', NOW(), NOW()),
                (%s, %s, %s, %s, %s, 'member', NOW(), NOW())
            """,
            (
                sys_uuid.uuid4(),
                project_uuid,
                stream_uuid,
                legacy_user_uuid,
                owner_a_uuid,
                sys_uuid.uuid4(),
                project_uuid,
                stream_uuid,
                owner_b_uuid,
                owner_a_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id,
                projection_stream_uuid
            ) VALUES (
                %s, %s, %s, 'zulip', 'direct:10,20', %s::jsonb, 'Peer',
                TRUE, %s, %s
            )
            """,
            (
                chat_uuid,
                account_a_uuid,
                owner_a_uuid,
                json.dumps(
                    {
                        "participants": [
                            {
                                "provider_user_id": "20",
                                "identity_uuid": str(legacy_user_uuid),
                            }
                        ]
                    }
                ),
                project_uuid,
                stream_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_chats_v2 (
                uuid, external_account_uuid, owner_user_uuid, provider,
                provider_chat_id, source, display_name, selected, project_id
            ) VALUES (
                %s, %s, %s, 'zulip', 'channel:99',
                '{"participants":[]}'::jsonb, 'Owner B access',
                TRUE, %s
            )
            """,
            (
                account_b_chat_uuid,
                account_b_uuid,
                owner_b_uuid,
                project_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_files (
                uuid, project_id, name, user_uuid, stream_uuid,
                content_type, size_bytes, hash, storage_object_id
            ) VALUES (
                %s, %s, 'identity-merge.txt', %s, %s,
                'text/plain', 1, 'test-hash', 'identity-merge.txt'
            )
            """,
            (file_uuid, project_uuid, owner_a_uuid, stream_uuid),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_file_accesses (
                uuid, project_id, file_uuid, user_uuid
            ) VALUES
                (%s, %s, %s, %s),
                (%s, %s, %s, %s)
            """,
            (
                sys_uuid.uuid4(),
                project_uuid,
                file_uuid,
                legacy_user_uuid,
                sys_uuid.uuid4(),
                project_uuid,
                file_uuid,
                owner_b_uuid,
            ),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        assert (
            identity_linking.bind_verified_account_owner(
                session,
                provider="zulip",
                account_uuid=account_a_uuid,
                owner_user_uuid=owner_a_uuid,
                provider_realm_uuid=provider_realm_uuid,
                provider_user_id="10",
            )
            is None
        )
        canonical_external_uuid = identity_linking.canonical_provider_identity_uuid(
            "zulip",
            provider_realm_uuid,
            "20",
        )
        assert identity_linking.merge_account_scoped_provider_identities(
            session,
            provider="zulip",
            account_uuid=account_a_uuid,
            provider_realm_uuid=provider_realm_uuid,
        ) == [chat_uuid]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid FROM m_workspace_users
            WHERE uuid = ANY(%s)
            ORDER BY uuid
            """,
            ([legacy_user_uuid, canonical_external_uuid],),
        )
        assert [row[0] for row in cursor.fetchall()] == [canonical_external_uuid]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE stream_uuid = %s
              AND role = 'member'
              AND user_uuid = %s
            """,
            (stream_uuid, canonical_external_uuid),
        )
        assert cursor.fetchone()[0] == 1
    with session_factory() as session:
        assert (
            identity_linking.bind_verified_account_owner(
                session,
                provider="zulip",
                account_uuid=account_b_uuid,
                owner_user_uuid=owner_b_uuid,
                provider_realm_uuid=provider_realm_uuid,
                provider_user_id="20",
            )
            == canonical_external_uuid
        )
        assert identity_linking.merge_workspace_user_identity(
            session,
            canonical_external_uuid,
            owner_b_uuid,
        ) == [chat_uuid]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_stream_bindings
            WHERE stream_uuid = %s
              AND role = 'member'
              AND user_uuid = %s
            """,
            (stream_uuid, owner_b_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT source_scope
            FROM m_confirmed_external_stream_access
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, owner_b_uuid),
        )
        assert cursor.fetchone()[0] == str(account_a_uuid)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_user_streams
            WHERE project_id = %s AND uuid = %s AND user_uuid = %s
            """,
            (project_uuid, stream_uuid, owner_b_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT source#>>'{participants,0,identity_uuid}'
            FROM m_external_chats_v2
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        assert cursor.fetchone()[0] == str(owner_b_uuid)
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM m_workspace_file_accesses
            WHERE file_uuid = %s AND user_uuid = %s
            """,
            (file_uuid, owner_b_uuid),
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            """
            SELECT workspace_user_uuid, link_kind
            FROM m_external_provider_identity_links_v1
            WHERE provider = 'zulip'
              AND provider_realm_uuid = %s
              AND provider_user_id = '20'
            """,
            (provider_realm_uuid,),
        )
        assert cursor.fetchone() == (
            owner_b_uuid,
            "verified_account_owner",
        )
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_users WHERE uuid = %s",
            (canonical_external_uuid,),
        )
        assert cursor.fetchone()[0] == 0
    with session_factory() as session:
        with pytest.raises(
            ValueError,
            match="already linked to another account",
        ):
            identity_linking.bind_verified_account_owner(
                session,
                provider="zulip",
                account_uuid=conflicting_account_uuid,
                owner_user_uuid=conflicting_owner_uuid,
                provider_realm_uuid=provider_realm_uuid,
                provider_user_id="20",
            )


def test_provider_identity_merge_invalidates_direct_event_history(
    _database,
    db,
    monkeypatch,
):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    provider_realm_uuid = sys_uuid.uuid4()
    legacy_a_uuid = sys_uuid.uuid4()
    legacy_b_uuid = sys_uuid.uuid4()
    canonical_a_uuid = identity_linking.canonical_provider_identity_uuid(
        "zulip",
        provider_realm_uuid,
        "20",
    )
    canonical_b_uuid = identity_linking.canonical_provider_identity_uuid(
        "zulip",
        provider_realm_uuid,
        "21",
    )
    event_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, "payload-rewrite-owner")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings
            ) VALUES (%s, %s, 'zulip', '{}'::jsonb)
            """,
            (account_uuid, owner_uuid),
        )
        for legacy_uuid, provider_user_id in (
            (legacy_a_uuid, "20"),
            (legacy_b_uuid, "21"),
        ):
            cursor.execute(
                """
                INSERT INTO m_workspace_users (
                    uuid, username, source, status, avatar,
                    provider_uuid, external_account_uuid, provider_external_id,
                    created_at, updated_at, last_ping_at
                )
                SELECT %s, %s, 'zulip', 'active', avatar,
                       %s, %s, %s, NOW(), NOW(), NOW()
                FROM m_workspace_users
                WHERE uuid = %s
                """,
                (
                    legacy_uuid,
                    f"zulip-{legacy_uuid}",
                    sys_uuid.uuid4(),
                    account_uuid,
                    provider_user_id,
                    owner_uuid,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_events (
                uuid, project_id, user_uuid, payload, object_type, action
            ) VALUES (%s, %s, %s, %s::jsonb, 'user', 'updated')
            """,
            (
                event_uuid,
                project_uuid,
                owner_uuid,
                json.dumps(
                    {
                        "participants": [
                            {"uuid": str(legacy_a_uuid)},
                            {"uuid": str(legacy_b_uuid)},
                        ],
                        "reference": f"provider:{legacy_a_uuid}",
                    }
                ),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version
            )
            SELECT project_id, user_uuid, epoch_version
            FROM m_workspace_events
            WHERE uuid = %s
            RETURNING epoch_generation, current_epoch_version
            """,
            (event_uuid,),
        )
        generation_before, event_epoch = cursor.fetchone()
    rewrite_calls = []
    original_rewrite = identity_linking._rewrite_payload_uuid_references

    def capture_rewrite(session, replacements):
        rewrite_calls.append(list(replacements))
        return original_rewrite(session, replacements)

    monkeypatch.setattr(
        identity_linking,
        "_rewrite_payload_uuid_references",
        capture_rewrite,
    )
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        assert (
            identity_linking.merge_account_scoped_provider_identities(
                session,
                provider="zulip",
                account_uuid=account_uuid,
                provider_realm_uuid=provider_realm_uuid,
            )
            == []
        )
    assert len(rewrite_calls) == 1
    assert set(rewrite_calls[0]) == {
        (legacy_a_uuid, canonical_a_uuid),
        (legacy_b_uuid, canonical_b_uuid),
    }
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT payload FROM m_workspace_events WHERE uuid = %s",
            (event_uuid,),
        )
        payload = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT uuid FROM m_workspace_users
            WHERE uuid = ANY(%s)
            ORDER BY uuid
            """,
            (
                [
                    legacy_a_uuid,
                    legacy_b_uuid,
                    canonical_a_uuid,
                    canonical_b_uuid,
                ],
            ),
        )
        user_rows = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            """
            SELECT epoch_generation, current_epoch_version,
                   pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_uuid, owner_uuid),
        )
        generation_after, current_epoch, pruned_through = cursor.fetchone()
    assert payload["participants"] == [
        {"uuid": str(legacy_a_uuid)},
        {"uuid": str(legacy_b_uuid)},
    ]
    assert payload["reference"] == f"provider:{legacy_a_uuid}"
    assert user_rows == sorted([canonical_a_uuid, canonical_b_uuid])
    assert generation_after != generation_before
    assert current_epoch == event_epoch
    assert pruned_through == event_epoch


def test_provider_identity_merge_refreshes_persisted_reaction_users(_database, db):
    project_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    legacy_user_uuid = sys_uuid.uuid4()
    canonical_user_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, f"owner-{owner_uuid}")
    conftest.seed_workspace_user(
        db,
        canonical_user_uuid,
        f"canonical-{canonical_user_uuid}",
    )
    stream_uuid = conftest.seed_user_stream(
        db,
        project_uuid,
        owner_uuid,
        "identity-reaction-users",
    )
    topic_uuid = conftest.seed_stream_topic(
        db,
        project_uuid,
        stream_uuid,
        owner_uuid,
        "general",
        is_default=True,
    )
    message_uuid = sys_uuid.uuid4()
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_users (
                uuid, username, source, status, avatar,
                created_at, updated_at, last_ping_at
            ) VALUES (
                %s, %s, 'zulip', 'active', %s, NOW(), NOW(), NOW()
            )
            """,
            (
                legacy_user_uuid,
                f"legacy-{legacy_user_uuid}",
                f"urn:gravatar:{legacy_user_uuid.hex}",
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_messages (
                uuid, project_id, stream_uuid, topic_uuid, user_uuid,
                payload, source_name, source, reaction_users
            ) VALUES (
                %s, %s, %s, %s, %s,
                '{"kind":"markdown","content":"identity reaction"}'::jsonb,
                'native', '{"kind":"native"}'::jsonb,
                jsonb_build_object(
                    'heart',
                    jsonb_build_array(CAST(%s AS text))
                )
            )
            """,
            (
                message_uuid,
                project_uuid,
                stream_uuid,
                topic_uuid,
                owner_uuid,
                legacy_user_uuid,
            ),
        )
        cursor.execute(
            """
                INSERT INTO m_workspace_message_reactions (
                    uuid, project_id, message_uuid, user_uuid, emoji_name,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'heart', NOW(), NOW())
            """,
            (
                sys_uuid.uuid4(),
                project_uuid,
                message_uuid,
                legacy_user_uuid,
            ),
        )

    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        identity_linking.merge_workspace_user_identity(
            session,
            legacy_user_uuid,
            canonical_user_uuid,
            rewrite_payloads=False,
            rewrite_chats=False,
        )

    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT reaction_users
            FROM m_workspace_messages
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        stored_snapshot = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT user_uuid
            FROM m_workspace_message_reactions
            WHERE project_id = %s AND message_uuid = %s
            """,
            (project_uuid, message_uuid),
        )
        stored_reaction_user_uuid = cursor.fetchone()[0]

    assert stored_snapshot == {"heart": [str(canonical_user_uuid)]}
    assert stored_reaction_user_uuid == canonical_user_uuid


def test_provider_identity_merge_resumes_bounded_event_batches(
    _database,
    db,
    monkeypatch,
):
    owner_uuid = sys_uuid.uuid4()
    legacy_user_uuid = sys_uuid.uuid4()
    canonical_user_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    conftest.seed_workspace_user(db, owner_uuid, "bounded-merge-owner")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_workspace_users (
                uuid, username, source, status, avatar,
                provider_uuid, external_account_uuid, provider_external_id,
                created_at, updated_at, last_ping_at
            )
            SELECT %s, %s, 'zulip', 'active', avatar,
                   %s, %s, '20', NOW(), NOW(), NOW()
            FROM m_workspace_users
            WHERE uuid = %s
            """,
            (
                legacy_user_uuid,
                f"zulip-{legacy_user_uuid}",
                sys_uuid.uuid4(),
                sys_uuid.uuid4(),
                owner_uuid,
            ),
        )
        for offset in range(3):
            cursor.execute(
                """
                INSERT INTO m_workspace_events (
                    uuid, project_id, user_uuid, payload, object_type, action
                ) VALUES (
                    %s, %s, %s, %s::jsonb, 'user', 'updated'
                )
                """,
                (
                    sys_uuid.uuid4(),
                    project_uuid,
                    legacy_user_uuid,
                    json.dumps(
                        {
                            "offset": offset,
                            "identity_uuid": str(legacy_user_uuid),
                        }
                    ),
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid, current_epoch_version
            )
            SELECT project_id, user_uuid, MAX(epoch_version)
            FROM m_workspace_events
            WHERE project_id = %s AND user_uuid = %s
            GROUP BY project_id, user_uuid
            RETURNING epoch_generation, current_epoch_version
            """,
            (project_uuid, legacy_user_uuid),
        )
        _legacy_generation, event_epoch = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO m_workspace_event_cursors (
                project_id, user_uuid
            ) VALUES (%s, %s)
            RETURNING epoch_generation
            """,
            (project_uuid, canonical_user_uuid),
        )
        generation_before = cursor.fetchone()[0]
    monkeypatch.setattr(identity_linking, "_REFERENCE_UPDATE_ROW_BATCH_SIZE", 2)
    session_factory = engines.engine_factory.get_engine().session_manager
    pending_attempts = 0
    for _attempt in range(6):
        completed = False
        with session_factory() as session:
            try:
                identity_linking.merge_workspace_user_identity(
                    session,
                    legacy_user_uuid,
                    canonical_user_uuid,
                )
            except identity_linking.IdentityMergePending:
                pending_attempts += 1
            else:
                completed = True
        if completed:
            break
    else:
        pytest.fail("Identity reconciliation did not finish")

    assert pending_attempts == 1
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT user_uuid, payload
            FROM m_workspace_events
            WHERE project_id = %s
            ORDER BY epoch_version
            """,
            (project_uuid,),
        )
        rows = cursor.fetchall()
        cursor.execute(
            "SELECT uuid FROM m_workspace_users WHERE uuid = ANY(%s)",
            ([legacy_user_uuid, canonical_user_uuid],),
        )
        user_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT epoch_generation, current_epoch_version,
                   pruned_through_epoch_version
            FROM m_workspace_event_cursors
            WHERE project_id = %s AND user_uuid = %s
            """,
            (project_uuid, canonical_user_uuid),
        )
        generation_after, current_epoch, pruned_through = cursor.fetchone()
    assert [row[0] for row in rows] == [canonical_user_uuid] * 3
    assert [row[1]["identity_uuid"] for row in rows] == [str(legacy_user_uuid)] * 3
    assert user_rows == [(canonical_user_uuid,)]
    assert generation_after != generation_before
    assert current_epoch == event_epoch
    assert pruned_through == event_epoch


def test_unreferenced_provider_identity_cleanup_removes_only_stale_rows(
    _database,
    db,
):
    owner_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    orphan_uuid = sys_uuid.uuid4()
    referenced_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db,
            project_uuid,
            owner_uuid,
            "Stale identity cleanup stream",
        )
    )
    with db.cursor() as cursor:
        for user_uuid in (orphan_uuid, referenced_uuid):
            cursor.execute(
                """
                INSERT INTO m_workspace_users (
                    uuid, username, source, status, avatar,
                    provider_uuid, external_account_uuid, provider_external_id,
                    created_at, updated_at, last_ping_at
                )
                SELECT %s, %s, 'zulip', 'active', avatar,
                       %s, %s, %s, NOW(), NOW(), NOW()
                FROM m_workspace_users
                WHERE uuid = %s
                """,
                (
                    user_uuid,
                    f"zulip-{user_uuid}",
                    sys_uuid.uuid4(),
                    sys_uuid.uuid4(),
                    str(user_uuid),
                    owner_uuid,
                ),
            )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_bindings (
                uuid, project_id, stream_uuid, user_uuid, who_uuid, role,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'member', NOW(), NOW())
            """,
            (
                sys_uuid.uuid4(),
                project_uuid,
                stream_uuid,
                referenced_uuid,
                owner_uuid,
            ),
        )
    session_factory = engines.engine_factory.get_engine().session_manager
    with session_factory() as session:
        assert identity_linking.delete_unreferenced_provider_identities(session) == [
            orphan_uuid
        ]
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT uuid
            FROM m_workspace_users
            WHERE uuid = ANY(%s)
            ORDER BY uuid
            """,
            ([orphan_uuid, referenced_uuid],),
        )
        assert [row[0] for row in cursor.fetchall()] == [referenced_uuid]


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


def test_worker_capability_refresh_does_not_wait_on_heartbeat_bridge_lock(
    _database,
    db,
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    # The integration database is session-scoped. Use the smallest possible
    # UUID so the global ordered worker claim deterministically selects this
    # test account even when earlier tests left credentialed accounts behind.
    account_uuid = sys_uuid.UUID(int=0)
    now = datetime.datetime.now(datetime.timezone.utc)
    conftest.seed_workspace_user(db, owner_uuid, f"worker-heartbeat-{owner_uuid}")
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status,
                capabilities, last_heartbeat_at
            ) VALUES (%s, 'zulip', 1, 'active', '{}'::jsonb, %s)
            """,
            (instance_uuid, now - datetime.timedelta(seconds=45)),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_control_instances_v1 (
                bridge_instance_uuid, provider_kind, identity_generation,
                encryption_key_uuid, encryption_public_key
            ) VALUES (%s, 'zulip', 1, %s, 'test-public-key')
            """,
            (instance_uuid, sys_uuid.uuid4()),
        )
        cursor.execute(
            """
            INSERT INTO m_external_accounts_v2 (
                uuid, owner_user_uuid, provider, settings,
                credential_present, status, live_ready, capabilities
            ) VALUES (
                %s, %s, 'zulip', %s::jsonb,
                TRUE, 'live', TRUE, '{}'::jsonb
            )
            """,
            (
                account_uuid,
                owner_uuid,
                sql_state._json(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.test",
                        "default_project_id": str(sys_uuid.uuid4()),
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
                sql_state._json(
                    {
                        "associated_data": {
                            "bridge_instance_uuid": str(instance_uuid),
                        }
                    }
                ),
            ),
        )

    bridge_locked = threading.Event()
    release_heartbeat = threading.Event()
    session_factory = engines.engine_factory.get_engine().session_manager

    class CoordinatedSession:
        def __init__(self, session):
            self._session = session

        def execute(self, statement, params=()):
            result = self._session.execute(statement, params)
            if (
                'UPDATE "m_external_bridge_instances_v2"' in statement
                and '"last_heartbeat_at" = %s' in statement
            ):
                bridge_locked.set()
                if not release_heartbeat.wait(timeout=5):
                    raise TimeoutError("heartbeat bridge lock was not released")
            return result

        def __getattr__(self, name):
            return getattr(self._session, name)

    class CoordinatedRepository(sql_state.SQLControlState):
        @staticmethod
        @contextlib.contextmanager
        def _current_session():
            with session_factory() as session:
                yield CoordinatedSession(session)

    repository = CoordinatedRepository(realm_uuid, b"k" * 32)
    identity = _identity(instance_uuid, realm_uuid)
    heartbeat_request = {
        "heartbeat_uuid": str(sys_uuid.uuid4()),
        "client_timestamp": "2026-07-29T00:00:00Z",
        "image_version": "test",
        "provider_kind": "zulip",
        "capabilities": {},
        "blocked_batch": None,
    }

    def heartbeat():
        return repository.heartbeat(identity, heartbeat_request, now=now)

    def worker_refresh():
        with session_factory() as session:
            session.execute("SET LOCAL lock_timeout = '750ms'")
            session.execute("SET LOCAL statement_timeout = '3s'")
            claimed_uuid = sql_state.claim_capability_refresh_account(session)
            assert claimed_uuid == account_uuid
            return sql_state.refresh_effective_capabilities(
                session,
                account_uuid=claimed_uuid,
                now=now,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        heartbeat_future = executor.submit(heartbeat)
        assert bridge_locked.wait(timeout=3)
        refresh_future = executor.submit(worker_refresh)
        try:
            assert refresh_future.result(timeout=3) == 1
        finally:
            release_heartbeat.set()
        heartbeat_result = heartbeat_future.result(timeout=3)

    assert heartbeat_result["heartbeat_uuid"] == heartbeat_request["heartbeat_uuid"]


def test_large_capability_projection_refresh_releases_lock_between_topic_batches(
    _database,
    db,
):
    realm_uuid = sys_uuid.uuid4()
    instance_uuid = sys_uuid.uuid4()
    owner_uuid = sys_uuid.uuid4()
    account_uuid = sys_uuid.uuid4()
    chat_uuid = sys_uuid.uuid4()
    project_uuid = sys_uuid.uuid4()
    stream_uuid = sys_uuid.UUID(
        conftest.seed_user_stream(
            db,
            project_uuid,
            owner_uuid,
            "Large capability projection",
        )
    )
    recipient_uuids = [owner_uuid]
    for _index in range(299):
        recipient_uuid = sys_uuid.uuid4()
        recipient_uuids.append(recipient_uuid)
        conftest.seed_user_stream_binding(
            db,
            project_uuid,
            stream_uuid,
            recipient_uuid,
        )
    available_capability = {
        "messenger.message.read": {
            "available": True,
            "revision": 1,
            "limits": {},
        }
    }
    now = datetime.datetime.now(datetime.timezone.utc)
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO m_external_bridge_instances_v2 (
                uuid, provider, identity_generation, status,
                capabilities, last_heartbeat_at
            ) VALUES (%s, 'zulip', 1, 'active', %s::jsonb, %s)
            """,
            (
                instance_uuid,
                json.dumps(available_capability),
                now - datetime.timedelta(seconds=61),
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_external_bridge_control_instances_v1 (
                bridge_instance_uuid, provider_kind, identity_generation,
                encryption_key_uuid, encryption_public_key
            ) VALUES (%s, 'zulip', 1, %s, 'test-public-key')
            """,
            (instance_uuid, sys_uuid.uuid4()),
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
                owner_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "server_url": "https://zulip.example.test",
                        "default_project_id": str(project_uuid),
                    }
                ),
                json.dumps(available_capability),
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
                            "bridge_instance_uuid": str(instance_uuid),
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
                %s, %s, %s, 'zulip', 'channel:large', %s::jsonb,
                'Large capability projection', TRUE, %s, %s, 'live',
                %s::jsonb, %s::jsonb
            )
            """,
            (
                chat_uuid,
                account_uuid,
                owner_uuid,
                json.dumps(
                    {
                        "kind": "zulip",
                        "chat_type": "channel",
                        "participants": [],
                        "topics": [],
                    }
                ),
                project_uuid,
                stream_uuid,
                json.dumps(available_capability),
                json.dumps(available_capability),
            ),
        )
        cursor.execute(
            """
            UPDATE m_workspace_streams
            SET source_name = 'zulip',
                source = '{"kind": "zulip", "stream_id": 42}'::jsonb,
                external_account_uuid = %s,
                provider_metadata = jsonb_build_object(
                    'capabilities', %s::jsonb
                )
            WHERE project_id = %s AND uuid = %s
            """,
            (
                account_uuid,
                json.dumps(available_capability),
                project_uuid,
                stream_uuid,
            ),
        )
        cursor.execute(
            """
            INSERT INTO m_workspace_stream_topics (
                uuid, project_id, name, stream_uuid, external_account_uuid,
                provider_metadata, created_at, updated_at
            )
            SELECT
                md5(%s || ':' || sequence.value::text)::uuid,
                %s,
                'topic-' || sequence.value::text,
                %s,
                %s,
                jsonb_build_object('capabilities', %s::jsonb),
                NOW(),
                NOW()
            FROM generate_series(1, 64) AS sequence(value)
            """,
            (
                str(chat_uuid),
                project_uuid,
                stream_uuid,
                account_uuid,
                json.dumps(available_capability),
            ),
        )

    session_factory = engines.engine_factory.get_engine().session_manager
    repository = sql_state.SQLControlState(realm_uuid, b"k" * 32)
    identity = _identity(instance_uuid, realm_uuid)
    heartbeat_request = {
        "heartbeat_uuid": str(sys_uuid.uuid4()),
        "client_timestamp": "2026-08-17T00:00:00Z",
        "image_version": "test",
        "provider_kind": "zulip",
        "capabilities": available_capability,
        "blocked_batch": None,
    }
    with session_factory() as session:
        session.execute("SET LOCAL statement_timeout = '10s'")
        sql_state.refresh_effective_capabilities(
            session,
            account_uuid=account_uuid,
            now=now,
        )

    project_lock_acquired = threading.Event()
    release_project_lock = threading.Event()

    def hold_project_event_lock():
        with session_factory() as session:
            session.execute(
                """
                SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))
                """,
                (project_uuid,),
            )
            project_lock_acquired.set()
            if not release_project_lock.wait(timeout=5):
                raise TimeoutError("project event lock was not released")

    lock_heartbeat_request = {
        **heartbeat_request,
        "heartbeat_uuid": str(sys_uuid.uuid4()),
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        lock_future = executor.submit(hold_project_event_lock)
        assert project_lock_acquired.wait(timeout=3)
        try:
            deferred_started_at = time.monotonic()
            with pytest.raises(messenger_events.ProjectEventLockUnavailableError):
                with session_factory() as session:
                    session.execute("SET LOCAL statement_timeout = '2s'")
                    sql_state.refresh_projected_capabilities_batch(
                        session,
                        account_uuid=account_uuid,
                        batch_size=16,
                    )
            deferred_duration = time.monotonic() - deferred_started_at
            heartbeat_started_at = time.monotonic()
            heartbeat_future = executor.submit(
                _request_call,
                repository.heartbeat,
                identity,
                lock_heartbeat_request,
                now=now,
            )
            lock_heartbeat_result = heartbeat_future.result(timeout=2)
            lock_heartbeat_duration = time.monotonic() - heartbeat_started_at
        finally:
            release_project_lock.set()
        lock_future.result(timeout=3)

    assert deferred_duration < 1
    assert lock_heartbeat_duration < 2
    assert (
        lock_heartbeat_result["heartbeat_uuid"]
        == lock_heartbeat_request["heartbeat_uuid"]
    )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT provider_metadata->'capabilities'
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            """,
            (project_uuid, stream_uuid),
        )
        assert cursor.fetchone()[0] == available_capability

    first_topic_batch_committed = threading.Event()
    release_projection_batches = threading.Event()
    batch_stats = []

    def refresh_capabilities_and_projections():
        while True:
            with session_factory() as session:
                claimed = session.execute(
                    """
                    SELECT uuid
                    FROM m_external_accounts_v2
                    WHERE uuid = %s
                      AND EXISTS (
                          SELECT 1
                          FROM m_external_chats_v2 AS chat
                          JOIN m_workspace_streams AS stream
                            ON stream.project_id = chat.project_id
                           AND stream.uuid = chat.projection_stream_uuid
                          WHERE chat.external_account_uuid = %s
                            AND (
                                stream.provider_metadata->'capabilities'
                                    IS DISTINCT FROM chat.capabilities
                                OR EXISTS (
                                    SELECT 1
                                    FROM m_workspace_stream_topics AS topic
                                    WHERE topic.project_id = chat.project_id
                                      AND topic.stream_uuid =
                                          chat.projection_stream_uuid
                                      AND topic.provider_metadata->'capabilities'
                                          IS DISTINCT FROM chat.capabilities
                                )
                            )
                      )
                    FOR UPDATE
                    """,
                    (account_uuid, account_uuid),
                ).fetchone()
                if claimed is None:
                    return
                stats = sql_state.refresh_projected_capabilities_batch(
                    session,
                    account_uuid=account_uuid,
                    batch_size=16,
                )
            batch_stats.append(stats)
            if len(batch_stats) == 2:
                first_topic_batch_committed.set()
                if not release_projection_batches.wait(timeout=5):
                    raise TimeoutError("projection batches were not released")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        refresh_future = executor.submit(refresh_capabilities_and_projections)
        if not first_topic_batch_committed.wait(timeout=10):
            refresh_future.result(timeout=1)
            pytest.fail("capability projection did not commit two batches")
        try:
            heartbeat_started_at = time.monotonic()
            heartbeat_future = executor.submit(
                _request_call,
                repository.heartbeat,
                identity,
                heartbeat_request,
                now=now,
            )
            heartbeat_result = heartbeat_future.result(timeout=2)
            heartbeat_duration = time.monotonic() - heartbeat_started_at
            project_event_started_at = time.monotonic()
            with session_factory() as session:
                session.execute("SET LOCAL lock_timeout = '1s'")
                messenger_events.create_broadcast_event(
                    project_uuid,
                    sys_uuid.uuid4(),
                    [owner_uuid],
                    messenger_events.USER_UPDATED_EVENT,
                    {"uuid": str(owner_uuid)},
                    session=session,
                )
            project_event_duration = time.monotonic() - project_event_started_at
        finally:
            release_projection_batches.set()
        refresh_future.result(timeout=30)

    assert heartbeat_result["heartbeat_uuid"] == heartbeat_request["heartbeat_uuid"]
    assert heartbeat_duration < 2
    assert project_event_duration < 2
    assert batch_stats[0][0] == 1
    assert batch_stats[1][0] == 16
    assert all(entities <= 16 for entities, _recipients, _events in batch_stats)
    assert sum(entities for entities, _recipients, _events in batch_stats) == 65
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT capabilities
            FROM m_external_chats_v2
            WHERE uuid = %s
            """,
            (chat_uuid,),
        )
        offline_capabilities = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT
                (SELECT provider_metadata->'capabilities'
                 FROM m_workspace_streams
                 WHERE project_id = %s AND uuid = %s),
                COUNT(*) FILTER (
                    WHERE provider_metadata->'capabilities'
                          IS NOT DISTINCT FROM %s::jsonb
                )
            FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s
            """,
            (
                project_uuid,
                stream_uuid,
                json.dumps(offline_capabilities),
                project_uuid,
                stream_uuid,
            ),
        )
        stream_capabilities, refreshed_topics = cursor.fetchone()
        assert stream_capabilities == offline_capabilities
        assert refreshed_topics == 64
        cursor.execute(
            """
            SELECT object_type, MIN(epoch_version), MAX(epoch_version), COUNT(*)
            FROM m_workspace_broadcast_message_events_v1
            WHERE project_id = %s AND object_type IN ('stream', 'topic')
            GROUP BY object_type
            ORDER BY object_type
            """,
            (project_uuid,),
        )
        events_by_type = {row[0]: row[1:] for row in cursor.fetchall()}
    assert events_by_type["stream"][2] == 1
    assert events_by_type["topic"][2] == 64
    assert events_by_type["stream"][0] < events_by_type["topic"][0]


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
    with session_factory() as session:
        claimed_uuid = session.execute(
            """
            SELECT uuid
            FROM m_external_accounts_v2
            WHERE uuid = %s
            FOR UPDATE
            """,
            (account_uuid,),
        ).fetchone()["uuid"]
        sql_state.refresh_effective_capabilities(
            session,
            account_uuid=claimed_uuid,
        )
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
        # Report ingestion stays account-bounded; capability propagation is
        # deferred to the worker instead of scanning all provider accounts.
        assert cursor.fetchone()[0] == event_count_before_stale_report
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
                    "provider_realm_uuid": str(realm_uuid),
                    "provider_owner_user_id": "7",
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
    invalid_owner_uuid = sys_uuid.uuid4()
    invalid_owner = catalog_report(invalid_owner_uuid, 1)
    invalid_owner["catalog"]["source"]["provider_owner_user_id"] = "8"
    assert (
        _request_call(repository.observed_reports, identity, [invalid_owner])[
            "results"
        ][0]["status"]
        == "rejected"
    )

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_chats_v2 WHERE uuid = ANY(%s)",
            ([invalid_direct_uuid, invalid_group_uuid, invalid_owner_uuid],),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT COUNT(*) FROM m_external_bridge_desired_resources_v1 "
            "WHERE resource_type = 'external_chat_assignment' "
            "AND resource_uuid = ANY(%s)",
            ([invalid_direct_uuid, invalid_group_uuid, invalid_owner_uuid],),
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
            "provider_realm_uuid": str(realm_uuid),
            "provider_owner_user_id": "7",
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
        assert cursor.fetchone() == ("live", 2)

    with session_factory() as session:
        assert (
            sql_state.repair_external_chat_assignments(
                session,
                account_uuid,
                instance_uuid,
                "zulip",
            )
            == 1
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT operation, generation
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (selected_uuid,),
        )
        assert cursor.fetchone() == ("upsert", 2)
        cursor.execute(
            """
            DELETE FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (selected_uuid,),
        )
    with session_factory() as session:
        assert (
            sql_state.repair_external_chat_assignments(
                session,
                account_uuid,
                instance_uuid,
                "zulip",
            )
            == 1
        )
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT operation, generation
            FROM m_external_bridge_desired_resources_v1
            WHERE resource_type = 'external_chat_assignment'
              AND resource_uuid = %s
            """,
            (selected_uuid,),
        )
        assert cursor.fetchone() == ("upsert", 2)

    current_live = {
        **live_ready,
        "report_uuid": str(sys_uuid.uuid4()),
        "observed_generation": 2,
    }
    assert (
        _request_call(
            repository.observed_reports,
            identity,
            [current_live],
        )["results"][0]["status"]
        == "applied"
    )
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT status, revision FROM m_external_chats_v2 WHERE uuid = %s",
            (selected_uuid,),
        )
        assert cursor.fetchone() == ("live", 2)


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
    sha256 = hashlib.sha256(data).hexdigest()
    storage_info = file_storage.save_workspace_file(
        file_uuid,
        data,
        storage_type="file",
        storage_object_id=f"external-content/sha256/{sha256[:2]}/{sha256}",
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
        sha256=sha256,
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
    assert (
        _request_call(repository.find_reusable_content, sha256, len(data))
        == storage_info
    )

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
