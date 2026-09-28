# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Materialize backend-owned external chat streams in Messenger storage."""

import collections.abc
import datetime
import json
import typing
import uuid as sys_uuid

from restalchemy.dm import filters as dm_filters

from workspace.messenger_api import file_storage
from workspace.messenger_api import provider_store
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


def _stable_binding_uuid(
    parent_uuid: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
) -> sys_uuid.UUID:
    return sys_uuid.uuid5(parent_uuid, f"binding\0{user_uuid}")


def _utc_datetime(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _sync_provider_projection_to_v3(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    projection_stream_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    provider_kind: str,
    source: collections.abc.Mapping[str, typing.Any],
    participant_uuids: collections.abc.Iterable[sys_uuid.UUID],
    emit_events: bool,
    emit_membership_events: bool | None = None,
    topic_binding_materialization_limit: int | None = None,
) -> None:
    """Materialize a catalog-created provider chat in the v3 canonical store."""
    if emit_membership_events is None:
        emit_membership_events = emit_events
    provider = session.execute(
        """
        SELECT uuid, name, iam_user_uuid
        FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND uuid = %s AND name = %s AND enabled
        """,
        (project_id, bridge_instance_uuid, provider_kind),
    ).fetchone()
    if provider is None:
        return
    stream = session.execute(
        """
        SELECT * FROM m_workspace_streams
        WHERE project_id = %s AND uuid = %s AND source_name = %s
        """,
        (project_id, projection_stream_uuid, provider_kind),
    ).fetchone()
    if stream is None:
        return

    participant_users = set(participant_uuids)
    # Imported direct chats can retain a historical physical owner that no
    # longer belongs to the current pair.  The catalog participants are the
    # authoritative membership for a private projection; adding that stale
    # owner would attempt to materialize a third binding and trip the schema's
    # direct-chat invariant.
    users = sorted(
        participant_users
        if stream["private"]
        else participant_users | {stream["user_uuid"]},
        key=str,
    )
    session.execute(
        """
        INSERT INTO workspace_v3.users (
            uuid, created_at, updated_at, username, source, status,
            first_name, last_name, email, last_ping_at,
            status_emoji, status_text, avatar
        )
        SELECT legacy.uuid, legacy.created_at, legacy.updated_at,
               COALESCE(NULLIF(legacy.username, ''), legacy.uuid::text),
               legacy.source, legacy.status, legacy.first_name,
               legacy.last_name, legacy.email, legacy.last_ping_at,
               legacy.status_emoji, legacy.status_text,
               COALESCE(
                   NULLIF(legacy.avatar, ''),
                   'urn:gravatar:' || md5(
                       lower(COALESCE(legacy.email, legacy.username))
                   )
               )
        FROM m_workspace_users AS legacy
        WHERE legacy.uuid = ANY(%s::uuid[])
        ON CONFLICT (uuid) DO NOTHING
        """,
        (users,),
    )
    store = provider_store.ProviderEntityStore(
        session,
        project_id,
        sys_uuid.UUID(str(provider["iam_user_uuid"])),
        provider,
    )
    source_topics = [dict(topic) for topic in source.get("topics", [])]
    source_default_topic = next(
        (topic for topic in source_topics if topic.get("is_default")),
        None,
    )
    default_topic_uuid = (
        sys_uuid.UUID(str(source_default_topic["topic_uuid"]))
        if source_default_topic is not None
        else (
            None
            if stream["default_topic_uuid"] is None
            else sys_uuid.UUID(str(stream["default_topic_uuid"]))
        )
    )
    source_topic_uuids = {
        sys_uuid.UUID(str(topic["topic_uuid"])) for topic in source_topics
    }
    if default_topic_uuid is not None and default_topic_uuid not in source_topic_uuids:
        legacy_default_topic = session.execute(
            """
            SELECT uuid, name
            FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s AND uuid = %s
            """,
            (project_id, projection_stream_uuid, default_topic_uuid),
        ).fetchone()
        if legacy_default_topic is not None:
            source_topics.append(
                {
                    "topic_uuid": str(default_topic_uuid),
                    "name": legacy_default_topic["name"],
                    "is_default": True,
                }
            )

    stream_data = {
        "name": stream["name"],
        "description": stream["description"],
        "owner_uuid": stream["user_uuid"],
        "invite_only": stream["invite_only"],
        "announce": stream["announce"],
        "direct_user_uuid": stream["direct_user_uuid"],
        "private": stream["private"],
        "is_archived": stream["is_archived"],
        "color": stream["color"] or 0,
        "default_topic_uuid": default_topic_uuid,
        "history_public_to_subscribers": True,
        "created_at": _utc_datetime(stream["created_at"]),
    }
    store.upsert(
        "streams",
        projection_stream_uuid,
        provider_store.canonical_hash(stream_data),
        stream_data,
        source_updated_at=_utc_datetime(stream["updated_at"]),
        emit_event=emit_events,
    )

    topics = []
    for source_topic in source_topics:
        topic_uuid = sys_uuid.UUID(str(source_topic["topic_uuid"]))
        legacy_topic = session.execute(
            """
            SELECT * FROM m_workspace_stream_topics
            WHERE project_id = %s AND stream_uuid = %s AND uuid = %s
            """,
            (project_id, projection_stream_uuid, topic_uuid),
        ).fetchone()
        topic_data = {
            "stream_uuid": projection_stream_uuid,
            "name": source_topic["name"],
            "color": 0 if legacy_topic is None else legacy_topic["color"] or 0,
            "is_done": False,
            "version": 0,
            "created_at": (
                _utc_datetime(stream["created_at"])
                if legacy_topic is None
                else _utc_datetime(legacy_topic["created_at"])
            ),
        }
        topic_updated_at = (
            stream["updated_at"] if legacy_topic is None else legacy_topic["updated_at"]
        )
        store.upsert(
            "topics",
            topic_uuid,
            provider_store.canonical_hash(topic_data),
            topic_data,
            source_updated_at=_utc_datetime(topic_updated_at),
            emit_event=emit_events,
        )
        topics.append((topic_uuid, topic_data["created_at"]))

    if stream["private"]:
        stale_private_bindings = session.execute(
            """
            SELECT uuid
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s
              AND NOT (user_uuid = ANY(%s::uuid[]))
            ORDER BY uuid
            FOR UPDATE
            """,
            (project_id, projection_stream_uuid, users),
        ).fetchall()
        if stale_private_bindings:
            session.execute(
                """
                DELETE FROM workspace_v3.stream_bindings
                WHERE project_id = %s AND uuid = ANY(%s::uuid[])
                """,
                (
                    project_id,
                    [binding["uuid"] for binding in stale_private_bindings],
                ),
            )

    bindings = session.execute(
        """
        SELECT * FROM m_workspace_stream_bindings
        WHERE project_id = %s AND stream_uuid = %s
          AND user_uuid = ANY(%s::uuid[])
        ORDER BY user_uuid
        """,
        (project_id, projection_stream_uuid, users),
    ).fetchall()
    materialize_topic_bindings = (
        topic_binding_materialization_limit is None
        or len(bindings) * len(topics) <= topic_binding_materialization_limit
    )
    for binding in bindings:
        user_uuid = sys_uuid.UUID(str(binding["user_uuid"]))
        existing_stream_binding = session.execute(
            """
            SELECT uuid
            FROM workspace_v3.stream_bindings
            WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
            FOR UPDATE
            """,
            (project_id, projection_stream_uuid, user_uuid),
        ).fetchone()
        stream_binding_uuid = (
            _stable_binding_uuid(projection_stream_uuid, user_uuid)
            if existing_stream_binding is None
            else sys_uuid.UUID(str(existing_stream_binding["uuid"]))
        )
        binding_data = {
            "stream_uuid": projection_stream_uuid,
            "user_uuid": user_uuid,
            "who_uuid": binding["who_uuid"],
            "role": binding["role"],
            "notification_mode": binding["notification_mode"],
            "created_at": _utc_datetime(binding["created_at"]),
        }
        store.upsert(
            "stream_bindings",
            stream_binding_uuid,
            provider_store.canonical_hash(binding_data),
            binding_data,
            source_updated_at=_utc_datetime(binding["updated_at"]),
            emit_event=emit_membership_events,
        )
        if not materialize_topic_bindings:
            continue
        for topic_uuid, topic_created_at in topics:
            existing_topic_binding = session.execute(
                """
                SELECT uuid
                FROM workspace_v3.topic_bindings
                WHERE project_id = %s AND topic_uuid = %s AND user_uuid = %s
                FOR UPDATE
                """,
                (project_id, topic_uuid, user_uuid),
            ).fetchone()
            topic_binding_uuid = (
                _stable_binding_uuid(topic_uuid, user_uuid)
                if existing_topic_binding is None
                else sys_uuid.UUID(str(existing_topic_binding["uuid"]))
            )
            topic_flag = session.execute(
                """
                SELECT notification_mode, updated_at
                FROM m_workspace_user_topic_flags
                WHERE project_id = %s AND uuid = %s AND user_uuid = %s
                """,
                (project_id, topic_uuid, user_uuid),
            ).fetchone()
            topic_binding_data = {
                "stream_uuid": projection_stream_uuid,
                "topic_uuid": topic_uuid,
                "user_uuid": user_uuid,
                "notification_mode": (
                    "default" if topic_flag is None else topic_flag["notification_mode"]
                ),
                "created_at": topic_created_at,
            }
            store.upsert(
                "topic_bindings",
                topic_binding_uuid,
                provider_store.canonical_hash(topic_binding_data),
                topic_binding_data,
                source_updated_at=_utc_datetime(
                    binding["updated_at"]
                    if topic_flag is None
                    else topic_flag["updated_at"]
                ),
                emit_event=emit_membership_events,
            )


def archive_external_chat_stream(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    projection_stream_uuid: sys_uuid.UUID,
    bridge_instance_uuid: sys_uuid.UUID,
    provider_kind: str,
    emit_events: bool,
) -> bool:
    """Archive an obsolete provider projection in both Messenger stores."""
    stream = session.execute(
        """
        UPDATE m_workspace_streams
        SET is_archived = TRUE, updated_at = NOW()
        WHERE project_id = %s AND uuid = %s AND source_name = %s
          AND NOT is_archived
        RETURNING *
        """,
        (project_id, projection_stream_uuid, provider_kind),
    ).fetchone()
    if stream is None:
        return False
    provider = session.execute(
        """
        SELECT uuid, name, iam_user_uuid
        FROM workspace_v3.provider_consumers
        WHERE project_id = %s AND uuid = %s AND name = %s AND enabled
        """,
        (project_id, bridge_instance_uuid, provider_kind),
    ).fetchone()
    if provider is None:
        return True
    provider_stream = session.execute(
        """
        SELECT default_topic_uuid
        FROM workspace_v3.streams
        WHERE project_id = %s AND uuid = %s
        """,
        (project_id, projection_stream_uuid),
    ).fetchone()
    stream_data = {
        "name": stream["name"],
        "description": stream["description"],
        "owner_uuid": stream["user_uuid"],
        "invite_only": stream["invite_only"],
        "announce": stream["announce"],
        "direct_user_uuid": stream["direct_user_uuid"],
        "private": stream["private"],
        "is_archived": True,
        "color": stream["color"] or 0,
        # Legacy stream rows can outlive the provider topic that originally
        # supplied their default_topic_uuid.  Reusing that stale UUID while
        # archiving makes the deferred v3 stream/topic FK fail at COMMIT, after
        # the HTTP response has already been written.  Preserve the v3 value
        # when the provider projection exists; otherwise leave it unset.
        "default_topic_uuid": (
            provider_stream["default_topic_uuid"]
            if provider_stream is not None
            else None
        ),
        "history_public_to_subscribers": True,
        "created_at": _utc_datetime(stream["created_at"]),
    }
    store = provider_store.ProviderEntityStore(
        session,
        project_id,
        sys_uuid.UUID(str(provider["iam_user_uuid"])),
        provider,
    )
    store.upsert(
        "streams",
        projection_stream_uuid,
        provider_store.canonical_hash(stream_data),
        stream_data,
        source_updated_at=_utc_datetime(stream["updated_at"]),
        emit_event=emit_events,
    )
    return True


def rebind_external_chat_files(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    source_stream_uuid: sys_uuid.UUID,
    target_stream_uuid: sys_uuid.UUID,
) -> int:
    """Move provider file ACLs when a duplicate projection is retired."""
    rows = session.execute(
        """
        SELECT uuid, user_uuid, name, description, content_type, size_bytes,
               hash, storage_type, created_at
        FROM workspace_v3.files
        WHERE project_id = %s AND stream_uuid = %s
        ORDER BY uuid
        FOR UPDATE
        """,
        (project_id, source_stream_uuid),
    ).fetchall()
    for row in rows:
        try:
            metadata = file_storage.read_workspace_file_metadata(
                row["uuid"], storage_type=row["storage_type"]
            )
        except FileNotFoundError:
            continue
        if metadata.stream_uuid not in {source_stream_uuid, target_stream_uuid}:
            raise ValueError("External file sidecar belongs to another stream")
        if metadata.stream_uuid == target_stream_uuid:
            continue
        file_storage.save_workspace_file_metadata(
            file_storage.WorkspaceFileMetadata(
                uuid=metadata.uuid,
                project_id=metadata.project_id,
                stream_uuid=target_stream_uuid,
                owner_uuid=metadata.owner_uuid,
                name=metadata.name,
                description=metadata.description,
                content_type=metadata.content_type,
                size_bytes=metadata.size_bytes,
                sha256=metadata.sha256,
                created_at=metadata.created_at,
                acl_mode=metadata.acl_mode,
                origin=metadata.origin,
            ),
            storage_type=row["storage_type"],
        )
    session.execute(
        """
        UPDATE workspace_v3.files
        SET stream_uuid = %s, updated_at = NOW()
        WHERE project_id = %s AND stream_uuid = %s
        """,
        (target_stream_uuid, project_id, source_stream_uuid),
    )
    session.execute(
        """
        UPDATE m_workspace_files
        SET stream_uuid = %s, updated_at = NOW()
        WHERE project_id = %s AND stream_uuid = %s
        """,
        (target_stream_uuid, project_id, source_stream_uuid),
    )
    return len(rows)


def rebind_external_chat_messages(
    session: typing.Any,
    *,
    project_id: sys_uuid.UUID,
    source_stream_uuid: sys_uuid.UUID,
    target_stream_uuid: sys_uuid.UUID,
) -> int:
    """Move messages from a retired provider projection to its canonical stream."""
    if source_stream_uuid == target_stream_uuid:
        return 0

    missing_v3_topics = session.execute(
        """
        SELECT source.uuid, source.name
        FROM workspace_v3.topics AS source
        JOIN workspace_v3.messages AS message
          ON message.project_id = source.project_id
         AND message.stream_uuid = source.stream_uuid
         AND message.topic_uuid = source.uuid
        JOIN workspace_v3.streams AS target_stream
          ON target_stream.project_id = source.project_id
         AND target_stream.uuid = %s
        WHERE source.project_id = %s AND source.stream_uuid = %s
          AND target_stream.default_topic_uuid IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM workspace_v3.topics AS target
              WHERE target.project_id = source.project_id
                AND target.stream_uuid = target_stream.uuid
                AND lower(btrim(target.name)) = lower(btrim(source.name))
          )
        LIMIT 1
        """,
        (target_stream_uuid, project_id, source_stream_uuid),
    ).fetchone()
    if missing_v3_topics is not None:
        raise ValueError(
            "Canonical provider stream has no matching topic for "
            f"{missing_v3_topics['uuid']} ({missing_v3_topics['name']})"
        )

    moved_v3 = session.execute(
        """
        WITH topic_map AS MATERIALIZED (
            SELECT source.uuid AS source_topic_uuid,
                   COALESCE(
                       (
                           SELECT target.uuid
                           FROM workspace_v3.topics AS target
                           WHERE target.project_id = source.project_id
                             AND target.stream_uuid = %s
                             AND lower(btrim(target.name)) =
                                 lower(btrim(source.name))
                           ORDER BY target.uuid
                           LIMIT 1
                       ),
                       target_stream.default_topic_uuid
                   ) AS target_topic_uuid
            FROM workspace_v3.topics AS source
            JOIN workspace_v3.streams AS target_stream
              ON target_stream.project_id = source.project_id
             AND target_stream.uuid = %s
            WHERE source.project_id = %s AND source.stream_uuid = %s
        ), moved AS (
            UPDATE workspace_v3.messages AS message
            SET stream_uuid = %s,
                topic_uuid = topic_map.target_topic_uuid,
                updated_at = clock_timestamp()
            FROM topic_map
            WHERE message.project_id = %s
              AND message.stream_uuid = %s
              AND message.topic_uuid = topic_map.source_topic_uuid
            RETURNING message.uuid
        )
        SELECT COUNT(*) AS count FROM moved
        """,
        (
            target_stream_uuid,
            target_stream_uuid,
            project_id,
            source_stream_uuid,
            target_stream_uuid,
            project_id,
            source_stream_uuid,
        ),
    ).fetchone()["count"]

    return moved_v3


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
    emit_legacy_events: bool | None = None,
    emit_membership_events: bool | None = None,
    topic_binding_materialization_limit: int | None = None,
    reconcile_participants: bool = True,
    shared_projection: bool = False,
) -> None:
    """Create the canonical stream and materialize all participant bindings."""
    if emit_legacy_events is None:
        emit_legacy_events = emit_events
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
            "emit_events": emit_legacy_events,
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
        _sync_provider_projection_to_v3(
            session,
            project_id=sys_uuid.UUID(str(project_id)),
            projection_stream_uuid=projection_stream_uuid,
            bridge_instance_uuid=bridge_instance_uuid,
            provider_kind=provider_kind,
            source=source,
            participant_uuids=(
                sys_uuid.UUID(str(participant["identity_uuid"]))
                for participant in source["participants"]
            ),
            emit_events=emit_events,
            emit_membership_events=emit_membership_events,
            topic_binding_materialization_limit=(
                topic_binding_materialization_limit
            ),
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
        emit_events=emit_legacy_events,
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
    if stale_bindings and not shared_projection:
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
    _sync_provider_projection_to_v3(
        session,
        project_id=sys_uuid.UUID(str(project_id)),
        projection_stream_uuid=projection_stream_uuid,
        bridge_instance_uuid=bridge_instance_uuid,
        provider_kind=provider_kind,
        source=source,
        participant_uuids=participants,
        emit_events=emit_events,
        emit_membership_events=emit_membership_events,
        topic_binding_materialization_limit=topic_binding_materialization_limit,
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
