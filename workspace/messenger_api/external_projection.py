# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Materialize backend-owned external chat streams in Messenger storage."""

import collections.abc
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api.dm import helpers
from workspace.messenger_api.dm import models


_DIRECT_PROVIDER_TOPIC_NAMESPACE = sys_uuid.UUID("4d1de6f0-5f93-58ad-9670-6a13754cb7aa")


def provider_topic_name(provider_kind: str) -> str:
    """Return the user-facing topic name for a provider projection."""
    return provider_kind.replace("_", " ").title()


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
            created_at, updated_at
        )
        SELECT
            %s, user_uuid, project_id, is_done, notification_mode,
            created_at, updated_at
        FROM m_workspace_user_topic_flags
        WHERE project_id = %s AND uuid = %s
        ON CONFLICT (uuid, user_uuid) DO UPDATE
        SET is_done = (
                m_workspace_user_topic_flags.is_done
                OR EXCLUDED.is_done
            ),
            notification_mode = CASE
                WHEN m_workspace_user_topic_flags.notification_mode = 'default'
                THEN EXCLUDED.notification_mode
                ELSE m_workspace_user_topic_flags.notification_mode
            END,
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
    provider_kind: str,
    source: collections.abc.Mapping[str, typing.Any],
    projection_stream_uuid: sys_uuid.UUID,
) -> tuple[sys_uuid.UUID, dict[str, typing.Any], bool]:
    """Merge a verified provider DM into an existing native direct chat."""
    normalized_source = dict(source)
    normalized_source["participants"] = [
        dict(participant) for participant in source.get("participants", [])
    ]
    normalized_source["topics"] = [dict(topic) for topic in source.get("topics", [])]
    if (
        source.get("chat_type") != "personal"
        or len(normalized_source["participants"]) != 2
        or len(normalized_source["topics"]) != 1
    ):
        return projection_stream_uuid, normalized_source, False

    participant_uuids = [
        sys_uuid.UUID(str(participant["identity_uuid"]))
        for participant in normalized_source["participants"]
    ]
    verified_count = session.execute(
        """
        SELECT COUNT(*) AS count
        FROM m_workspace_users
        WHERE uuid = ANY(%s) AND source = 'iam'
        """,
        (participant_uuids,),
    ).fetchone()["count"]
    if verified_count != 2:
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
        return projection_stream_uuid, normalized_source, False

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
        helpers.get_or_create_workspace_user_stream(
            project_id,
            owner_user_uuid,
            session=session,
            uuid=projection_stream_uuid,
            name=display_name,
            description=source["description"],
            private=chat_type != "channel",
            invite_only=chat_type != "channel",
            source_name=source_name,
            source=workspace_source,
            canonical_default_topic_uuid=(
                None
                if default_topic is None
                else sys_uuid.UUID(str(default_topic["topic_uuid"]))
            ),
            default_topic_name=(
                "General Topic" if default_topic is None else default_topic["name"]
            ),
            create_default_topic=default_topic is not None,
            provider_uuid=bridge_instance_uuid,
            external_account_uuid=external_account_uuid,
            provider_external_id=provider_chat_id,
            provider_metadata={
                "kind": provider_kind,
                "account_uuid": str(external_account_uuid),
                "external_id": provider_chat_id,
                "capabilities": dict(capabilities),
            },
            emit_events=emit_events,
        )
    elif not reconcile_participants:
        if stream.user_uuid != owner_user_uuid:
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
        if len(
            participants
        ) != 2 or stream.private_index != helpers.build_private_stream_index(
            *participants
        ):
            raise ValueError(
                "Native direct stream participants do not match assignment"
            )
        return
    if stream is not None and stream.user_uuid != owner_user_uuid:
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
    if not stale_bindings:
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
