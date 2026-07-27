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
        SELECT uuid
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
    topic = session.execute(
        """
        SELECT uuid
        FROM m_workspace_stream_topics
        WHERE project_id = %s AND stream_uuid = %s
          AND LOWER(name) = LOWER(%s)
        ORDER BY created_at, uuid
        LIMIT 1
        FOR UPDATE
        """,
        (project_id, target_stream_uuid, topic_name),
    ).fetchone()
    target_topic_uuid = (
        sys_uuid.UUID(str(topic["uuid"]))
        if topic is not None
        else sys_uuid.uuid5(
            _DIRECT_PROVIDER_TOPIC_NAMESPACE,
            f"{target_stream_uuid}:{provider_kind}",
        )
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
    provider_topic = normalized_source["topics"][0]
    old_topic_uuid = sys_uuid.UUID(str(provider_topic["topic_uuid"]))
    provider_topic["topic_uuid"] = str(target_topic_uuid)
    provider_topic["name"] = topic_name

    changed = target_stream_uuid != projection_stream_uuid
    if changed:
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
                (target_topic_uuid, project_id, old_topic_uuid),
            )
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
    return target_stream_uuid, normalized_source, changed


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
        )
    participants = {
        sys_uuid.UUID(str(participant["identity_uuid"])): participant["role"]
        for participant in source["participants"]
    }
    if stream is not None and getattr(stream, "private_index", None) is not None:
        if (
            len(participants) != 2
            or stream.private_index
            != helpers.build_private_stream_index(*participants)
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
        if participant_uuid in users:
            continue
        if participant_uuid == owner_user_uuid:
            raise ValueError("Provider stream projection owner identity is missing")
        user = models.WorkspaceUser(
            uuid=participant_uuid,
            username=f"{provider_kind}-{participant_uuid}",
            source=models.WorkspaceUserSource.ZULIP.value,
            status=models.WorkspaceUserStatus.ACTIVE.value,
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
