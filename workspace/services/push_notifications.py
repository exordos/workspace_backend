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
                """SELECT topic.name AS topic_name,stream.name AS stream_name,
                       topic.notification_mode AS topic_mode,membership.notification_mode AS stream_mode,
                       membership.membership_generation,
                       current_epoch.epoch_version,routing_cursor.epoch_generation,
                       EXISTS(SELECT 1 FROM messenger_publication_outbox pending
                         WHERE pending.project_id=topic.project_id AND pending.entity_uuid=topic.uuid
                           AND topic.user_uuid=ANY(pending.recipients)) AS publication_pending,
                       topic.unread_count,topic.active_unread_count,topic.passive_unread_count,
                       EXISTS(SELECT 1 FROM messenger_counter_updates dirty
                         WHERE dirty.project_id=topic.project_id AND dirty.user_uuid=topic.user_uuid
                           AND dirty.topic_uuid=topic.uuid) AS counters_pending,
                       COALESCE(first_unread.placement_uuid,topic.last_message_uuid) AS message_uuid,
                       contents.workspace_content AS payload,message.created_at,
                       author.username,author.first_name,author.last_name
                FROM messenger_api_user_topics_v1 topic
                JOIN messenger_streams stream ON stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
                JOIN messenger_stream_bindings membership ON membership.project_id=topic.project_id
                  AND membership.user_uuid=topic.user_uuid AND membership.stream_uuid=stream.uuid AND membership.active
                JOIN m_workspace_event_cursors routing_cursor ON routing_cursor.project_id=topic.project_id
                  AND routing_cursor.user_uuid=topic.user_uuid
                LEFT JOIN LATERAL (
                    SELECT event.epoch_version,event.payload||COALESCE(override.payload,'{}'::jsonb) AS payload
                    FROM m_workspace_broadcast_message_events_v1 event
                    JOIN m_workspace_event_audience_members_v1 audience ON
                      audience.audience_snapshot_uuid=event.audience_snapshot_uuid AND audience.user_uuid=topic.user_uuid
                    LEFT JOIN m_workspace_event_recipient_payloads_v1 override ON
                      override.event_uuid=event.uuid AND override.user_uuid=topic.user_uuid
                    WHERE event.project_id=topic.project_id AND event.entity_uuid=topic.uuid
                      AND event.object_type='topic' AND event.action='updated'
                    ORDER BY event.epoch_version DESC LIMIT 1
                ) current_epoch ON (current_epoch.payload->>'unread_count')::bigint=topic.unread_count
                  AND (current_epoch.payload->>'active_unread_count')::bigint=topic.active_unread_count
                  AND (current_epoch.payload->>'passive_unread_count')::bigint=topic.passive_unread_count
                LEFT JOIN LATERAL (
                    SELECT unread.placement_uuid
                    FROM messenger_user_message_unreads unread
                    JOIN messenger_user_message_bindings access ON
                      (access.project_id,access.user_uuid,access.placement_uuid)=(unread.project_id,unread.user_uuid,unread.placement_uuid)
                    JOIN messenger_message_placements placement ON placement.project_id=unread.project_id AND placement.uuid=unread.placement_uuid
                    JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
                    WHERE unread.project_id=topic.project_id AND unread.user_uuid=topic.user_uuid AND unread.topic_uuid=topic.uuid AND topic.unread_count>0
                    ORDER BY message.created_at,placement.uuid LIMIT 1
                ) first_unread ON true
                LEFT JOIN messenger_message_placements placement ON placement.project_id=topic.project_id
                  AND placement.uuid=COALESCE(first_unread.placement_uuid,topic.last_message_uuid)
                LEFT JOIN messenger_user_message_bindings access ON access.project_id=topic.project_id
                  AND access.user_uuid=topic.user_uuid AND access.placement_uuid=placement.uuid
                LEFT JOIN messenger_messages message ON message.project_id=placement.project_id
                  AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL AND access.uuid IS NOT NULL
                LEFT JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid
                LEFT JOIN m_workspace_users author ON author.uuid=message.author_uuid
                WHERE topic.project_id=%s AND topic.user_uuid=%s AND topic.uuid=%s AND topic.visible AND stream.deleted_at IS NULL
                  AND membership.membership_generation=%s""",
                (
                    signal["project_id"],
                    signal["user_uuid"],
                    signal["topic_uuid"],
                    cursor["membership_generation"],
                ),
            ).fetchone()
            if route is not None and (
                route["counters_pending"]
                or route["publication_pending"]
                or route["epoch_version"] is None
            ):
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
                devices=[device],
                content=content,
                priority="high"
                if route["active_unread_count"] > signal["previous_active_count"]
                else "normal",
            )
            child["last_retry_at"] = expires_at.isoformat()
            child["_membership_generation"] = route["membership_generation"]
            children.append(child)
        # Prepared deliveries and signal completion share the RA transaction.
        with contexts.Context().session_manager():
            for child in children:
                push_events._insert(child, user)
            event.status = event_models.Event.STATUS.ACTIVE.value
            event.save()
