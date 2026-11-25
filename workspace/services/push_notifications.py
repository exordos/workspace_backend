# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""Prepare device payloads independently of counters and Notification transport."""

import datetime
import logging
import time
import typing
import uuid as sys_uuid

from gcl_looper.services import basic
from gcl_sdk.events.dm import models as event_models
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mobile_push_opts
from workspace.messenger_api import push_events, push_notifications
from workspace.messenger_api.dm import message_payloads, schema
from workspace.messenger_api.dm import push_events as event_payloads

LOG = logging.getLogger(__name__)

# One snapshot for access, the published counters and the preview. The unread
# facts already reference the user's message binding (messenger_unread_access_fk).
TOPIC_SNAPSHOT_SQL = """
    WITH topic_state AS MATERIALIZED (
        SELECT topic.project_id, topic.uuid AS topic_uuid, topic.stream_uuid,
               member.user_uuid, member.membership_generation,
               topic.name AS topic_name, stream.name AS stream_name,
               personal.notification_mode AS topic_mode,
               member.notification_mode AS stream_mode,
               personal.unread_count, personal.active_unread_count,
               personal.passive_unread_count, last_message.placement_uuid AS last_message_uuid,
               cursor.epoch_generation
        FROM messenger_topics topic
        JOIN messenger_streams stream
          ON (stream.project_id, stream.uuid) = (topic.project_id, topic.stream_uuid)
        JOIN messenger_stream_bindings member
          ON (member.project_id, member.stream_uuid) = (topic.project_id, topic.stream_uuid)
        JOIN messenger_user_topic_bindings personal
          ON (personal.project_id, personal.user_uuid, personal.topic_uuid) =
             (topic.project_id, member.user_uuid, topic.uuid)
        JOIN m_workspace_event_cursors cursor
          ON (cursor.project_id, cursor.user_uuid) = (topic.project_id, member.user_uuid)
        LEFT JOIN messenger_topic_last_messages last_message
          ON (last_message.project_id, last_message.topic_uuid) = (topic.project_id, topic.uuid)
        WHERE topic.project_id = %s AND member.user_uuid = %s AND topic.uuid = %s
          AND member.active AND member.membership_generation = %s
          AND topic.deleted_at IS NULL AND stream.deleted_at IS NULL
    )
    SELECT state.*, published.epoch_version, preview.*
    FROM topic_state state
    LEFT JOIN LATERAL (
        SELECT event.epoch_version, event.payload || COALESCE(override.payload, '{}') AS counts
        FROM m_workspace_broadcast_message_events_v1 event
        JOIN m_workspace_event_audience_members_v1 audience
          ON audience.audience_snapshot_uuid = event.audience_snapshot_uuid
          AND audience.user_uuid = state.user_uuid
        LEFT JOIN m_workspace_event_recipient_payloads_v1 override
          ON override.event_uuid = event.uuid AND override.user_uuid = state.user_uuid
        WHERE event.project_id = state.project_id AND event.entity_uuid = state.topic_uuid
          AND event.object_type = 'topic' AND event.action = 'updated'
          AND NOT EXISTS (
              SELECT 1 FROM messenger_counter_updates dirty
              WHERE (dirty.project_id, dirty.user_uuid, dirty.topic_uuid) =
                    (state.project_id, state.user_uuid, state.topic_uuid)
          )
          AND NOT EXISTS (
              SELECT 1 FROM messenger_publication_outbox pending
              WHERE pending.project_id = state.project_id AND pending.entity_uuid = state.topic_uuid
                AND state.user_uuid = ANY(pending.recipients)
          )
        ORDER BY event.epoch_version DESC LIMIT 1
    ) published ON (published.counts->>'unread_count')::bigint = state.unread_count
       AND (published.counts->>'active_unread_count')::bigint = state.active_unread_count
       AND (published.counts->>'passive_unread_count')::bigint = state.passive_unread_count
    LEFT JOIN LATERAL (
        SELECT CASE WHEN message.uuid IS NOT NULL THEN placement.uuid END AS message_uuid,
               message.payload, message.created_at,
               author.username, author.first_name, author.last_name
        FROM (
            SELECT COALESCE((
                SELECT unread.placement_uuid FROM messenger_user_message_unreads unread
                JOIN messenger_message_placements placement
                  ON (placement.project_id, placement.uuid) = (unread.project_id, unread.placement_uuid)
                JOIN messenger_messages message
                  ON (message.project_id, message.uuid) = (placement.project_id, placement.message_uuid)
                WHERE state.unread_count > 0 AND message.deleted_at IS NULL
                  AND (unread.project_id, unread.user_uuid, unread.topic_uuid) =
                      (state.project_id, state.user_uuid, state.topic_uuid)
                ORDER BY message.created_at, placement.uuid LIMIT 1
            ), state.last_message_uuid) AS message_uuid
            WHERE published.epoch_version IS NOT NULL
            OFFSET 0
        ) picked
        LEFT JOIN messenger_user_message_bindings access
          ON (access.project_id, access.user_uuid, access.placement_uuid) =
             (state.project_id, state.user_uuid, picked.message_uuid)
        LEFT JOIN messenger_message_placements placement
          ON (placement.project_id, placement.uuid) = (access.project_id, access.placement_uuid)
          AND (placement.stream_uuid, placement.topic_uuid) = (state.stream_uuid, state.topic_uuid)
        LEFT JOIN messenger_messages_with_content_v1 message
          ON (message.project_id, message.uuid) = (placement.project_id, placement.message_uuid)
          AND message.deleted_at IS NULL
        LEFT JOIN m_workspace_users author ON author.uuid = message.author_uuid
    ) preview ON true
"""


class TopicPushHandler(basic.BasicService):
    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._last_prune = 0.0

    def _iteration(self) -> None:
        if not mobile_push_opts.enabled():
            return
        if time.monotonic() - self._last_prune >= 60:
            with contexts.Context().session_manager() as session:
                push_events.prune(session)
            self._last_prune = time.monotonic()
        for event in push_events.fetch_events(
            ("NEW", "IN_PROGRESS"), event_payloads.WorkspaceTopicSignal.KIND
        ):
            try:
                self._handle_signal(event)
            except Exception:
                LOG.exception("Cannot prepare topic push signal %s", event.uuid)

    def _handle_signal(self, event: event_models.Event) -> None:
        signal = event.event_data.body
        expires_at = event.created_at + datetime.timedelta(
            seconds=push_events.RETENTION_SECONDS
        )
        if expires_at <= datetime.datetime.now(datetime.timezone.utc):
            event.status = event_models.Event.STATUS.ERROR.value
            event.save()
            return
        with contexts.Context().session_manager() as session:
            schema.lock_shared(session)
            session.execute(
                """INSERT INTO m_workspace_event_cursors(project_id,user_uuid)
                VALUES (%s,%s) ON CONFLICT(project_id,user_uuid) DO NOTHING""",
                (signal["project_id"], signal["user_uuid"]),
            )
            cursor = session.execute(
                """SELECT guard.membership_generation
                FROM m_workspace_broadcast_message_events_v1 event
                JOIN m_workspace_event_cursors cursor ON cursor.project_id=event.project_id AND cursor.user_uuid=%s
                JOIN messenger_event_membership_guards guard ON guard.event_uuid=event.uuid
                  AND guard.project_id=event.project_id AND guard.user_uuid=cursor.user_uuid
                WHERE event.project_id=%s AND event.uuid=%s""",
                (
                    signal["user_uuid"],
                    signal["project_id"],
                    signal["source_uuid"],
                ),
            ).fetchone()
            if cursor is None:
                return  # The ordinary publication worker has not committed its epoch yet.
            route = session.execute(
                TOPIC_SNAPSHOT_SQL,
                (
                    signal["project_id"],
                    signal["user_uuid"],
                    signal["topic_uuid"],
                    cursor["membership_generation"],
                ),
            ).fetchone()
            if route is not None and route["epoch_version"] is None:
                return  # Wait until current counters and their WebSocket publication agree.
            if route is None or (
                route["unread_count"] > 0 and route["message_uuid"] is None
            ):
                event.status = event_models.Event.STATUS.ACTIVE.value
                event.save(session=session)
                return
            mode = route["topic_mode"]
            muted = mode == "mute" or (
                mode not in ("follow", "unmute") and route["stream_mode"] == "muted"
            )
            mentions_only = mode == "unmute" or (
                mode != "follow" and route["stream_mode"] == "mentions_only"
            )
            if muted or (
                mentions_only
                and route["active_unread_count"] == signal["previous_active_count"]
            ):
                event.status = event_models.Event.STATUS.ACTIVE.value
                event.save(session=session)
                return
            devices = push_events._devices(
                session,
                sys_uuid.UUID(signal["project_id"]),
                sys_uuid.UUID(signal["user_uuid"]),
            )
        # No counter transaction, schema/device lock or database connection is
        # held while preparing/encrypting previews or talking to Notification.
        text = (
            message_payloads.markdown_content(route["payload"])
            if route["payload"]
            else ""
        )
        if route["payload"] and route["payload"]["kind"] == "e2ee":
            text = "Зашифрованное сообщение"
        content = {
            "kind": "topic",
            "stream_name": route["stream_name"].encode()[:256].decode(errors="ignore"),
            "topic_name": route["topic_name"].encode()[:256].decode(errors="ignore"),
            "sender_name": (
                " ".join(
                    part for part in (route["first_name"], route["last_name"]) if part
                )
                or route["username"]
                or ""
            )
            .encode()[:128]
            .decode(errors="ignore"),
            "text": str(text or "").encode()[:768].decode(errors="ignore"),
            "created_at": route["created_at"].isoformat()
            if route["created_at"]
            else None,
            "unread_count": route["unread_count"],
            "active_unread_count": route["active_unread_count"],
            "passive_unread_count": route["unread_count"]
            - route["active_unread_count"],
            "epoch_version": route["epoch_version"],
            "epoch_generation": str(route["epoch_generation"]),
        }
        children = []
        user = sys_uuid.UUID(signal["user_uuid"])
        for device in devices:
            child = push_notifications.build_event(
                event_uuid=sys_uuid.uuid5(
                    sys_uuid.UUID(signal["source_uuid"]), f"{user}:{device.uuid}"
                ),
                kind=push_notifications.TOPIC_UPDATE,
                organization_uuid=sys_uuid.UUID(cfg.CONF.mobile_push.organization_uuid),
                project_uuid=sys_uuid.UUID(signal["project_id"]),
                stream_uuid=sys_uuid.UUID(signal["stream_uuid"]),
                topic_uuid=sys_uuid.UUID(signal["topic_uuid"]),
                message_uuid=route["message_uuid"],
                user_uuid=user,
                device=device,
                content=content,
                priority="high"
                if route["active_unread_count"] > signal["previous_active_count"]
                else "normal",
            )
            if child is None:
                continue
            child["last_retry_at"] = expires_at.isoformat()
            child["_membership_generation"] = route["membership_generation"]
            children.append(child)
        # Prepared deliveries and signal completion share the RA transaction.
        with contexts.Context().session_manager():
            for child in children:
                push_events._insert(child, user)
            event.status = event_models.Event.STATUS.ACTIVE.value
            event.save()
