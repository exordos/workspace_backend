# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Materialize backend-owned external chat streams in Messenger storage."""

import collections.abc
import json
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import read_state


_DIRECT_PROVIDER_TOPIC_NAMESPACE = sys_uuid.UUID("4d1de6f0-5f93-58ad-9670-6a13754cb7aa")


def provider_topic_name(provider_kind: str) -> str:
    """Return the user-facing topic name for a provider projection."""
    return provider_kind.replace("_", " ").title()


def _native_direct_participant_uuids(
    source: collections.abc.Mapping[str, typing.Any],
) -> tuple[sys_uuid.UUID, sys_uuid.UUID] | None:
    participants = source.get("participants", [])
    chat_type = source.get("chat_type")
    if chat_type in {None, "personal"} and len(participants) == 2:
        return (
            sys_uuid.UUID(str(participants[0]["identity_uuid"])),
            sys_uuid.UUID(str(participants[1]["identity_uuid"])),
        )
    if (
        chat_type == "group"
        and len(participants) == 1
        and participants[0]["role"] == "owner"
    ):
        user_uuid = sys_uuid.UUID(str(participants[0]["identity_uuid"]))
        return user_uuid, user_uuid
    return None


def is_native_direct_projection(
    session: typing.Any,
    project_id: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
) -> bool:
    """Return whether a projection points at a canonical native direct chat."""
    row = session.execute(
        """
        SELECT private_index
        FROM m_workspace_streams
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, stream_uuid),
    ).fetchone()
    return row is not None and row["private_index"] is not None


def _merge_topic_flags(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    source_topic_uuid: sys_uuid.UUID,
    target_topic_uuid: sys_uuid.UUID,
) -> None:
    if source_topic_uuid == target_topic_uuid:
        return
    session.execute(
        """
        INSERT INTO m_workspace_user_topic_flags (
            uuid, user_uuid, project_id, is_done, notification_mode,
            notification_updated_at, created_at, updated_at
        )
        SELECT
            %s, user_uuid, project_id, is_done, notification_mode,
            notification_updated_at, created_at, updated_at
        FROM m_workspace_user_topic_flags
        WHERE project_id = %s AND uuid = %s
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET is_done = (
                m_workspace_user_topic_flags.is_done
                OR EXCLUDED.is_done
            ),
            notification_mode = CASE
                WHEN m_workspace_user_topic_flags.notification_updated_at
                    >= EXCLUDED.notification_updated_at
                THEN m_workspace_user_topic_flags.notification_mode
                ELSE EXCLUDED.notification_mode
            END,
            notification_updated_at = GREATEST(
                m_workspace_user_topic_flags.notification_updated_at,
                EXCLUDED.notification_updated_at
            ),
            updated_at = GREATEST(
                m_workspace_user_topic_flags.updated_at,
                EXCLUDED.updated_at
            )
        """,
        (target_topic_uuid, project_id, source_topic_uuid),
    )


def _invalidate_moved_topic_summaries(
    session: typing.Any,
    topic_uuids: list[sys_uuid.UUID],
) -> None:
    session.execute(
        """
        UPDATE m_workspace_llm_endpoints AS endpoint
        SET claim_token = NULL,
            claim_expires_at = NULL,
            updated_at = NOW()
        FROM m_workspace_topic_summary_jobs AS job
        WHERE job.topic_uuid = ANY(%s)
          AND endpoint.uuid = job.endpoint_uuid
          AND endpoint.claim_token = job.endpoint_claim_token
        """,
        (topic_uuids,),
    )
    session.execute(
        """
        DELETE FROM m_workspace_topic_summary_jobs
        WHERE topic_uuid = ANY(%s)
        """,
        (topic_uuids,),
    )
    session.execute(
        """
        UPDATE m_workspace_topic_summary_journal
        SET invalidated_at = NOW()
        WHERE topic_uuid = ANY(%s) AND invalidated_at IS NULL
        """,
        (topic_uuids,),
    )
    session.execute(
        """
        UPDATE m_workspace_stream_topics
        SET summary = NULL,
            summary_last_message_uuid = NULL,
            updated_at = NOW()
        WHERE uuid = ANY(%s)
        """,
        (topic_uuids,),
    )


def reconcile_personal_chat_projection(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    provider_kind: str,
    source: collections.abc.Mapping[str, typing.Any],
    projection_stream_uuid: sys_uuid.UUID,
    emit_events: bool = True,
) -> tuple[sys_uuid.UUID, dict[str, typing.Any], bool]:
    """Merge a verified provider DM into its canonical native direct chat."""
    read_state.lock_message_structure(session, (project_id,))
    read_state.lock_projects(session, (project_id,))
    read_state.bump_project_structure_revisions(session, (project_id,))
    normalized_source = dict(source)
    normalized_source["participants"] = [
        dict(participant) for participant in source.get("participants", [])
    ]
    normalized_source["topics"] = [dict(topic) for topic in source.get("topics", [])]
    participant_uuids = _native_direct_participant_uuids(normalized_source)
    if participant_uuids is None or len(normalized_source["topics"]) != 1:
        return projection_stream_uuid, normalized_source, False
    if normalized_source["chat_type"] == "group" and participant_uuids[
        0
    ] != sys_uuid.UUID(str(owner_user_uuid)):
        return projection_stream_uuid, normalized_source, False

    unique_participant_uuids = set(participant_uuids)
    verified_count = session.execute(
        """
        SELECT COUNT(*) AS count
        FROM m_workspace_users
        WHERE uuid = ANY(%s) AND source = 'iam'
        """,
        (list(unique_participant_uuids),),
    ).fetchone()["count"]
    if verified_count != len(unique_participant_uuids):
        return projection_stream_uuid, normalized_source, False

    private_index = helpers.build_private_stream_index(*participant_uuids)
    target = session.execute(
        """
        SELECT uuid, default_topic_uuid
        FROM m_workspace_streams
        WHERE project_id = %s AND private_index = %s
        FOR UPDATE
        """,
        (project_id, private_index),
    ).fetchone()
    if target is None:
        if normalized_source["chat_type"] != "group":
            return projection_stream_uuid, normalized_source, False
        target_stream_uuid = helpers.deterministic_direct_stream_uuid(
            project_id,
            participant_uuids[0],
            participant_uuids[1],
        )
        helpers.get_or_create_workspace_user_stream(
            project_id,
            participant_uuids[0],
            session=session,
            uuid=target_stream_uuid,
            name=normalized_source["participants"][0]["display_name"],
            description=normalized_source["description"],
            source_name=models.SourceName.NATIVE.value,
            source=models.NativeSource(),
            direct_user_uuid=participant_uuids[0],
            emit_events=emit_events,
        )
        target = session.execute(
            """
            SELECT uuid, default_topic_uuid
            FROM m_workspace_streams
            WHERE project_id = %s AND private_index = %s
            FOR UPDATE
            """,
            (project_id, private_index),
        ).fetchone()
        if target is None:
            raise ValueError("Native self-direct stream is missing")

    target_stream_uuid = sys_uuid.UUID(str(target["uuid"]))
    topic_name = provider_topic_name(provider_kind)
    provider_topic = normalized_source["topics"][0]
    old_topic_uuid = sys_uuid.UUID(str(provider_topic["topic_uuid"]))
    canonical_topic = session.execute(
        """
        SELECT uuid, name
        FROM m_workspace_stream_topics
        WHERE project_id = %s AND stream_uuid = %s AND uuid = %s
        FOR UPDATE
        """,
        (project_id, target_stream_uuid, old_topic_uuid),
    ).fetchone()
    default_topic_uuid = (
        None
        if target["default_topic_uuid"] is None
        else sys_uuid.UUID(str(target["default_topic_uuid"]))
    )
    if canonical_topic is not None:
        target_topic_uuid = sys_uuid.UUID(str(canonical_topic["uuid"]))
    elif default_topic_uuid is not None:
        canonical_topic = session.execute(
            """
            SELECT uuid, name
            FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s AND uuid = %s
            FOR UPDATE
            """,
            (project_id, target_stream_uuid, default_topic_uuid),
        ).fetchone()
        if canonical_topic is None:
            raise ValueError("Native direct stream default topic is missing")
        target_topic_uuid = default_topic_uuid
    else:
        canonical_topic = None
        target_topic_uuid = sys_uuid.uuid5(
            _DIRECT_PROVIDER_TOPIC_NAMESPACE,
            f"{target_stream_uuid}:{provider_kind}",
        )
    topic_projection_changed = (
        default_topic_uuid != target_topic_uuid
        or canonical_topic is None
        or canonical_topic["name"] != topic_name
    )
    session.execute(
        """
        INSERT INTO m_workspace_stream_topics (
            uuid, project_id, name, stream_uuid
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (uuid) DO UPDATE
        SET name = EXCLUDED.name,
            updated_at = NOW()
        WHERE m_workspace_stream_topics.project_id = EXCLUDED.project_id
          AND m_workspace_stream_topics.stream_uuid = EXCLUDED.stream_uuid
        """,
        (target_topic_uuid, project_id, topic_name, target_stream_uuid),
    )
    if default_topic_uuid != target_topic_uuid:
        session.execute(
            """
            UPDATE m_workspace_streams
            SET default_topic_uuid = %s, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (target_topic_uuid, project_id, target_stream_uuid),
        )
    provider_topic["topic_uuid"] = str(target_topic_uuid)
    provider_topic["name"] = topic_name

    obsolete_topics = session.execute(
        """
        SELECT uuid
        FROM m_workspace_stream_topics
        WHERE project_id = %s AND stream_uuid = %s AND uuid <> %s
          AND (uuid = %s OR uuid = %s)
        ORDER BY created_at, uuid
        FOR UPDATE
        """,
        (
            project_id,
            target_stream_uuid,
            target_topic_uuid,
            old_topic_uuid,
            default_topic_uuid,
        ),
    ).fetchall()
    obsolete_topic_uuids = [
        sys_uuid.UUID(str(topic["uuid"])) for topic in obsolete_topics
    ]
    topic_projection_changed = topic_projection_changed or bool(obsolete_topic_uuids)

    stream_changed = target_stream_uuid != projection_stream_uuid
    source_stream = None
    if stream_changed:
        source_stream = session.execute(
            """
            SELECT uuid
            FROM m_workspace_streams
            WHERE project_id = %s AND uuid = %s
            FOR UPDATE
            """,
            (project_id, projection_stream_uuid),
        ).fetchone()
        if source_stream is not None:
            source_topics = session.execute(
                """
                SELECT DISTINCT topic_uuid
                FROM m_workspace_messages
                WHERE project_id = %s AND stream_uuid = %s
                """,
                (project_id, projection_stream_uuid),
            ).fetchall()
            read_state.merge_topics(
                session,
                project_id,
                [row["topic_uuid"] for row in source_topics],
                target_stream_uuid,
                target_topic_uuid,
            )
            session.execute(
                """
                UPDATE m_workspace_messages
                SET stream_uuid = %s, topic_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND stream_uuid = %s
                """,
                (
                    target_stream_uuid,
                    target_topic_uuid,
                    project_id,
                    projection_stream_uuid,
                ),
            )
            session.execute(
                """
                UPDATE m_workspace_drafts
                SET stream_uuid = %s, topic_uuid = %s, updated_at = NOW()
                WHERE project_id = %s AND stream_uuid = %s
                """,
                (
                    target_stream_uuid,
                    target_topic_uuid,
                    project_id,
                    projection_stream_uuid,
                ),
            )
            _merge_topic_flags(
                session,
                project_id=project_id,
                source_topic_uuid=old_topic_uuid,
                target_topic_uuid=target_topic_uuid,
            )

    if obsolete_topic_uuids:
        read_state.merge_topics(
            session,
            project_id,
            obsolete_topic_uuids,
            target_stream_uuid,
            target_topic_uuid,
        )
        session.execute(
            """
            UPDATE m_workspace_messages
            SET topic_uuid = %s, updated_at = NOW()
            WHERE project_id = %s AND stream_uuid = %s
              AND topic_uuid = ANY(%s)
            """,
            (
                target_topic_uuid,
                project_id,
                target_stream_uuid,
                obsolete_topic_uuids,
            ),
        )
        session.execute(
            """
            UPDATE m_workspace_drafts
            SET topic_uuid = %s, updated_at = NOW()
            WHERE project_id = %s AND stream_uuid = %s
              AND topic_uuid = ANY(%s)
            """,
            (
                target_topic_uuid,
                project_id,
                target_stream_uuid,
                obsolete_topic_uuids,
            ),
        )
        for obsolete_topic_uuid in obsolete_topic_uuids:
            _merge_topic_flags(
                session,
                project_id=project_id,
                source_topic_uuid=obsolete_topic_uuid,
                target_topic_uuid=target_topic_uuid,
            )

    if source_stream is not None or obsolete_topic_uuids:
        affected_topic_uuids = list(
            dict.fromkeys(
                [
                    target_topic_uuid,
                    *obsolete_topic_uuids,
                    *([old_topic_uuid] if source_stream is not None else []),
                ]
            )
        )
        _invalidate_moved_topic_summaries(session, affected_topic_uuids)

    if obsolete_topic_uuids:
        # The canonical default topic now owns all data and user state.
        session.execute(
            """
            DELETE FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s AND uuid = ANY(%s)
            """,
            (project_id, target_stream_uuid, obsolete_topic_uuids),
        )

    if source_stream is not None:
        # Keep the carrier row for provider file sidecars, but remove it
        # from every chat list after its messages have moved.
        session.execute(
            """
            UPDATE m_workspace_streams
            SET is_archived = TRUE, updated_at = NOW()
            WHERE project_id = %s AND uuid = %s
            """,
            (project_id, projection_stream_uuid),
        )
    session.execute(
        """
        UPDATE m_workspace_streams
        SET is_archived = FALSE, updated_at = NOW()
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, target_stream_uuid),
    )
    return (
        target_stream_uuid,
        normalized_source,
        stream_changed or topic_projection_changed,
    )


def _workspace_source(
    provider_kind: str,
    provider_chat_id: str,
    chat_type: str,
    account_settings: collections.abc.Mapping[str, typing.Any],
    external_account_uuid: sys_uuid.UUID,
) -> tuple[str, typing.Any]:
    if provider_kind == models.SourceName.ZULIP.value:
        provider_stream_id = provider_chat_id.removeprefix("channel:")
        stream_id = (
            int(provider_stream_id)
            if chat_type == "channel" and provider_stream_id.isdecimal()
            else 0
        )
        return provider_kind, models.ZulipSource(
            stream_id=stream_id,
            server_url=account_settings["server_url"],
            source_scope=str(external_account_uuid),
        )
    return models.SourceName.NATIVE.value, models.NativeSource()


def ensure_external_chat_stream(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    owner_user_uuid: sys_uuid.UUID,
    projection_stream_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    external_account_uuid: sys_uuid.UUID,
    provider_kind: str,
    provider_chat_id: str,
    display_name: str,
    source: collections.abc.Mapping[str, typing.Any],
    capabilities: collections.abc.Mapping[str, typing.Any],
    account_settings: collections.abc.Mapping[str, typing.Any],
    emit_events: bool = True,
    reconcile_participants: bool = True,
    shared_projection: bool = False,
) -> None:
    """Create the canonical stream and materialize all participant bindings."""
    stream = models.WorkspaceStream.objects.get_one_or_none(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "uuid": dm_filters.EQ(projection_stream_uuid),
        },
        session=session,
    )
    if stream is None:
        chat_type = source["chat_type"]
        source_name, workspace_source = _workspace_source(
            provider_kind,
            provider_chat_id,
            chat_type,
            account_settings,
            external_account_uuid,
        )
        default_topic = next(
            (topic for topic in source["topics"] if topic["is_default"]),
            None,
        )
        self_direct_participants = _native_direct_participant_uuids(source)
        self_direct_user_uuid = (
            self_direct_participants[0]
            if source["chat_type"] == "group" and self_direct_participants is not None
            else None
        )
        is_provider_self_direct = self_direct_user_uuid is not None
        stream_fields = {
            "uuid": projection_stream_uuid,
            "name": display_name,
            "description": source["description"],
            # A multi-user provider DM behaves like a membership-scoped
            # channel in Workspace.  Keeping it non-private puts it in the
            # Channels system folder while the external access gate still
            # limits visibility to the confirmed provider participants.
            "private": chat_type == "personal" or is_provider_self_direct,
            "invite_only": chat_type != "channel",
            "source_name": source_name,
            "source": workspace_source,
            "canonical_default_topic_uuid": (
                None
                if default_topic is None
                else sys_uuid.UUID(str(default_topic["topic_uuid"]))
            ),
            "default_topic_name": (
                "General Topic" if default_topic is None else default_topic["name"]
            ),
            "create_default_topic": default_topic is not None,
            "provider_uuid": bridge_instance_uuid,
            "external_account_uuid": external_account_uuid,
            "provider_external_id": provider_chat_id,
            "provider_metadata": {
                "kind": provider_kind,
                "account_uuid": str(external_account_uuid),
                "external_id": provider_chat_id,
                "default_display_name": display_name,
                "capabilities": dict(capabilities),
            },
            "emit_events": emit_events,
        }
        if self_direct_user_uuid is not None:
            stream_fields.update(
                {
                    "direct_user_uuid": self_direct_user_uuid,
                    "source_name": models.SourceName.NATIVE.value,
                    "source": models.NativeSource(),
                }
            )
            for field_name in (
                "provider_uuid",
                "external_account_uuid",
                "provider_external_id",
                "provider_metadata",
            ):
                stream_fields.pop(field_name)
        helpers.get_or_create_workspace_user_stream(
            project_id,
            owner_user_uuid,
            session=session,
            **stream_fields,
        )
    elif not reconcile_participants:
        if getattr(stream, "private_index", None) is not None:
            participant_uuids = _native_direct_participant_uuids(source)
            if (
                participant_uuids is None
                or owner_user_uuid not in participant_uuids
                or stream.private_index
                != helpers.build_private_stream_index(*participant_uuids)
            ):
                raise ValueError(
                    "Native direct stream participants do not match assignment"
                )
        elif stream.user_uuid != owner_user_uuid and not shared_projection:
            raise ValueError(
                "Provider stream projection owner does not match assignment"
            )
        return
    participants = {
        sys_uuid.UUID(str(participant["identity_uuid"])): participant["role"]
        for participant in source["participants"]
    }
    provider_realm_id = str(
        source.get("provider_realm_uuid")
        or account_settings.get("server_url")
        or external_account_uuid
    )
    revoked_user_uuids = helpers.get_revoked_workspace_external_chat_members(
        project_id,
        provider_kind,
        provider_realm_id,
        provider_chat_id,
        session=session,
    )
    participants = {
        user_uuid: role
        for user_uuid, role in participants.items()
        if user_uuid == owner_user_uuid or user_uuid not in revoked_user_uuids
    }
    if stream is not None and getattr(stream, "private_index", None) is not None:
        direct_participant_uuids = _native_direct_participant_uuids(source)
        if (
            direct_participant_uuids is None
            or stream.private_index
            != helpers.build_private_stream_index(*direct_participant_uuids)
        ):
            raise ValueError(
                "Native direct stream participants do not match assignment"
            )
        return
    if (
        stream is not None
        and stream.user_uuid != owner_user_uuid
        and not shared_projection
    ):
        raise ValueError("Provider stream projection owner does not match assignment")
    users = {
        user.uuid: user
        for user in models.WorkspaceUser.objects.get_all(
            filters={"uuid": dm_filters.In(list(participants))},
            session=session,
        )
    }
    for participant in source["participants"]:
        participant_uuid = sys_uuid.UUID(str(participant["identity_uuid"]))
        if participant_uuid not in participants:
            continue
        if participant_uuid in users:
            continue
        if participant_uuid == owner_user_uuid:
            raise ValueError("Provider stream projection owner identity is missing")
        user = models.WorkspaceUser(
            uuid=participant_uuid,
            username=f"{provider_kind}-{participant_uuid}",
            source=models.WorkspaceUserSource.ZULIP.value,
            # The provider directory reports account enablement, not presence.
            status=models.WorkspaceUserStatus.OFFLINE.value,
            first_name=participant["display_name"],
            provider_uuid=bridge_instance_uuid,
            external_account_uuid=external_account_uuid,
            provider_external_id=participant["provider_user_id"],
            avatar=participant["avatar_urn"],
        )
        user.insert(session=session)
        users[user.uuid] = user
    role_user_uuids: dict[str, list[sys_uuid.UUID]] = {}
    for user_uuid, role in participants.items():
        role_user_uuids.setdefault(role, []).append(user_uuid)
    helpers.get_or_create_workspace_stream_bindings(
        project_id=project_id,
        stream_uuid=projection_stream_uuid,
        who_uuid=owner_user_uuid,
        role_user_uuids=role_user_uuids,
        session=session,
        emit_events=emit_events,
    )
    existing_bindings = models.WorkspaceStreamBinding.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "stream_uuid": dm_filters.EQ(projection_stream_uuid),
        },
        session=session,
    )
    stale_bindings = [
        binding
        for binding in existing_bindings
        if binding.user_uuid not in participants
    ]
    if not stale_bindings or shared_projection:
        return
    managed_user_uuids = {
        user.uuid
        for user in models.WorkspaceUser.objects.get_all(
            filters={
                "uuid": dm_filters.In(
                    [binding.user_uuid for binding in stale_bindings]
                ),
                "source": dm_filters.EQ(models.WorkspaceUserSource.ZULIP.value),
            },
            session=session,
        )
    }
    for binding in stale_bindings:
        if binding.user_uuid in managed_user_uuids:
            helpers.delete_workspace_stream_binding(
                project_id,
                binding.uuid,
                session=session,
            )


def handoff_shared_projection_route(
    session: typing.Any,
    *,
    project_id: object,
    stream_uuid: object,
    old_external_account_uuid: object,
    peer_chat_uuid: object,
) -> None:
    """Move one shared stream's provider route to a selected account alias."""
    route = session.execute(
        """
        SELECT chat.external_account_uuid, chat.owner_user_uuid,
               chat.capabilities,
               (credential.envelope #>>
                    '{associated_data,bridge_instance_uuid}')::uuid
                    AS bridge_instance_uuid
        FROM m_external_chats_v2 AS chat
        JOIN m_external_accounts_v2 AS account
          ON account.uuid = chat.external_account_uuid
        JOIN m_external_credentials_v2 AS credential
          ON credential.external_account_uuid = account.uuid
        WHERE chat.uuid = %s
          AND chat.selected
          AND chat.project_id = %s
          AND chat.projection_stream_uuid = %s
        FOR SHARE OF chat, account, credential
        """,
        (peer_chat_uuid, project_id, stream_uuid),
    ).fetchone()
    if route is None:
        raise ValueError("Shared projection handoff route is unavailable")
    account_uuid = route["external_account_uuid"]
    owner_user_uuid = route["owner_user_uuid"]
    bridge_uuid = route["bridge_instance_uuid"]
    capabilities = route["capabilities"]
    session.execute(
        """
        UPDATE m_workspace_streams
        SET user_uuid = %s, external_account_uuid = %s, provider_uuid = %s,
            source = CASE
                WHEN source_name = 'zulip'
                THEN jsonb_set(
                    source, '{source_scope}', to_jsonb(%s::text), true
                )
                ELSE source END,
            provider_metadata = jsonb_set(
                jsonb_set(
                    COALESCE(provider_metadata, '{}'::jsonb),
                    '{account_uuid}', to_jsonb(%s::text), true
                ),
                '{capabilities}', %s::jsonb, true
            ),
            updated_at = NOW()
        WHERE project_id = %s AND uuid = %s
          AND external_account_uuid = %s
        """,
        (
            owner_user_uuid,
            account_uuid,
            bridge_uuid,
            account_uuid,
            account_uuid,
            json.dumps(capabilities),
            project_id,
            stream_uuid,
            old_external_account_uuid,
        ),
    )
    for table in ("m_workspace_stream_topics", "m_workspace_messages"):
        session.execute(
            f"""
            UPDATE {table}
            SET external_account_uuid = %s, provider_uuid = %s,
                provider_metadata = jsonb_set(
                    COALESCE(provider_metadata, '{{}}'::jsonb),
                    '{{account_uuid}}', to_jsonb(%s::text), true
                ),
                updated_at = NOW()
            WHERE project_id = %s AND stream_uuid = %s
              AND external_account_uuid = %s
            """,
            (
                account_uuid,
                bridge_uuid,
                account_uuid,
                project_id,
                stream_uuid,
                old_external_account_uuid,
            ),
        )
    session.execute(
        """
        UPDATE m_workspace_files
        SET external_account_uuid = %s, provider_uuid = %s,
            updated_at = NOW()
        WHERE project_id = %s AND stream_uuid = %s
          AND external_account_uuid = %s
        """,
        (
            account_uuid,
            bridge_uuid,
            project_id,
            stream_uuid,
            old_external_account_uuid,
        ),
    )
