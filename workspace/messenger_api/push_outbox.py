# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""A durable encrypted event snapshot, written with native message/read events."""

import datetime
import functools
import json
import logging
import typing
import uuid as sys_uuid

import bazooka
import requests
from bazooka import exceptions as http_exceptions
from gcl_sdk.clients.http import base as http_client
from oslo_config import cfg
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import sessions

from workspace.common import mobile_push_opts
from workspace.messenger_api import push_notifications
from workspace.messenger_api.dm import message_payloads, models, push_devices

LOG = logging.getLogger(__name__)
RETENTION_SECONDS = 3600


def _devices(
    session: sessions.PgSQLSession, project_id: sys_uuid.UUID, user_uuid: sys_uuid.UUID
) -> list[push_devices.PushDevice]:
    return push_devices.PushDevice.objects.get_all(
        filters={
            "project_id": dm_filters.EQ(project_id),
            "user_uuid": dm_filters.EQ(user_uuid),
            "platform": dm_filters.EQ("android"),
            "transport": dm_filters.EQ("fcm"),
        },
        session=session,
    )


def _insert(
    session: sessions.PgSQLSession,
    event: dict[str, typing.Any],
    user_uuid: sys_uuid.UUID,
    message_uuid: sys_uuid.UUID,
) -> None:
    session.execute(
        """
        INSERT INTO messenger_push_outbox (uuid, project_id, user_uuid, message_uuid, event)
        VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (uuid) DO NOTHING
    """,
        (
            event["uuid"],
            event["project_id"],
            user_uuid,
            message_uuid,
            json.dumps(event),
        ),
    )


def enqueue_message(
    session: sessions.PgSQLSession,
    source_event_uuid: sys_uuid.UUID,
    project_id: sys_uuid.UUID,
    message: typing.Any,
    recipients: typing.Iterable[sys_uuid.UUID],
) -> None:
    if not mobile_push_opts.enabled():
        return
    # Native messages keep UTC timestamps without a SQL timezone annotation.
    created_at = message.created_at.replace(tzinfo=datetime.timezone.utc)
    if created_at < datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=RETENTION_SECONDS
    ):
        return
    # Follow the existing active-unread rules, including mute/mention overrides.
    eligible = session.execute(
        """
        SELECT state.user_uuid FROM messenger_user_message_states AS state
        JOIN messenger_stream_bindings AS binding
          ON binding.project_id = state.project_id AND binding.user_uuid = state.user_uuid
         AND binding.stream_uuid = %s AND binding.active
         AND binding.membership_generation = state.membership_generation
        LEFT JOIN messenger_user_topic_bindings AS topic
          ON topic.project_id = state.project_id AND topic.user_uuid = state.user_uuid
         AND topic.topic_uuid = %s
        WHERE state.project_id = %s AND state.placement_uuid = %s
          AND state.user_uuid = ANY(%s::uuid[]) AND state.read_at IS NULL
          AND CASE WHEN topic.notification_mode = 'mute' THEN false
                   WHEN topic.notification_mode = 'follow' THEN true
                   WHEN topic.notification_mode = 'unmute' THEN state.mentioned
                   WHEN binding.notification_mode = 'all_messages' THEN true
                   WHEN binding.notification_mode = 'mentions_only' THEN state.mentioned
                   ELSE false END
    """,
        (
            message.stream_uuid,
            message.topic_uuid,
            project_id,
            message.uuid,
            list(recipients),
        ),
    ).fetchall()
    author = models.WorkspaceUser.objects.get_one(
        filters={"uuid": dm_filters.EQ(message.user_uuid)}, session=session
    )
    name = (
        " ".join(part for part in (author.first_name, author.last_name) if part)
        or author.username
    )
    # Keep the entire encrypted wire payload within FCM's 4 KiB limit.
    text = message_payloads.markdown_content(message.payload)
    if not text:
        payload_kind = (
            message.payload.get("kind")
            if isinstance(message.payload, dict)
            else message.payload.kind
        )
        if payload_kind != "e2ee":
            return  # key grants/requests are protocol messages, not user alerts
        text = "Зашифрованное сообщение"
    content = {
        "kind": "message",
        "sender_name": name.encode()[:128].decode(errors="ignore"),
        "text": text.encode()[:768].decode(errors="ignore"),
        "created_at": created_at.isoformat(),
    }
    for recipient in eligible:
        user_uuid = recipient["user_uuid"]
        devices = _devices(session, project_id, user_uuid)
        if not devices:
            continue
        event_uuid = sys_uuid.uuid5(
            sys_uuid.UUID(str(source_event_uuid)), f"push:{user_uuid}"
        )
        event = push_notifications.build_event(
            event_uuid=event_uuid,
            kind=push_notifications.MESSAGE_CREATE,
            organization_uuid=sys_uuid.UUID(cfg.CONF.mobile_push.organization_uuid),
            project_uuid=project_id,
            stream_uuid=message.stream_uuid,
            topic_uuid=message.topic_uuid,
            message_uuid=message.uuid,
            user_uuid=user_uuid,
            devices=devices,
            content=content,
        )
        if event["event_params"]["devices"]:
            _insert(session, event, user_uuid, message.uuid)


def enqueue_read_state(
    session: sessions.PgSQLSession,
    project_id: typing.Any,
    user_uuid: typing.Any,
    topic_uuid: typing.Any,
) -> None:
    if not mobile_push_opts.enabled():
        return
    # Read-up-to can affect several retained alerts, even after public events
    # for those messages have aged out. Inspect only this user's topic routes.
    rows = session.execute(
        """
        SELECT route.message_uuid FROM messenger_push_outbox AS route
        JOIN messenger_user_message_states AS state
          ON state.project_id = route.project_id AND state.user_uuid = route.user_uuid
         AND state.placement_uuid = route.message_uuid
        WHERE route.project_id = %s AND route.user_uuid = %s
          AND route.event->'event_params'->>'kind' = %s
          AND route.event->'event_params'->>'topic_uuid' = %s
          AND state.read_at IS NOT NULL
        """,
        (project_id, user_uuid, push_notifications.MESSAGE_CREATE, str(topic_uuid)),
    ).fetchall()
    enqueue_read(
        session,
        project_id,
        user_uuid,
        "messages.read",
        {"message_uuids": [row["message_uuid"] for row in rows]},
    )


def enqueue_read(
    session: sessions.PgSQLSession | None,
    project_id: typing.Any,
    user_uuid: typing.Any,
    kind: str,
    payload: dict[str, typing.Any],
) -> None:
    if not mobile_push_opts.enabled():
        return
    # Retained snapshots can still have a displayed alert after the FCM delivery
    # deadline. Reading them must cancel that alert too. No history scan.
    scope = {
        "message.read": ("message_uuid", [payload.get("uuid")]),
        "message.deleted": ("message_uuid", [payload.get("uuid")]),
        "messages.read": ("message_uuid", payload.get("message_uuids", [])),
        "stream.read": ("stream_uuid", [payload.get("uuid")]),
        "topic.read": ("topic_uuid", [payload.get("uuid")]),
    }.get(kind)
    if scope is None or not scope[1]:
        return
    field, values = scope
    session = session or contexts.Context().get_session()
    rows = session.execute(
        f"""
        SELECT uuid, message_uuid, event FROM messenger_push_outbox
        WHERE project_id = %s AND user_uuid = %s
          AND event->'event_params'->>'kind' = %s
          AND event->'event_params'->>'{field}' = ANY(%s::text[])
    """,
        (
            project_id,
            user_uuid,
            push_notifications.MESSAGE_CREATE,
            [str(v) for v in values],
        ),
    ).fetchall()
    devices = _devices(session, project_id, user_uuid)
    for row in rows:
        params = row["event"]["event_params"]
        event = push_notifications.build_event(
            event_uuid=sys_uuid.uuid5(row["uuid"], "cancel"),
            kind=push_notifications.MESSAGE_CANCELLED,
            organization_uuid=sys_uuid.UUID(params["organization_uuid"]),
            project_uuid=project_id,
            stream_uuid=sys_uuid.UUID(params["stream_uuid"]),
            topic_uuid=sys_uuid.UUID(params["topic_uuid"]),
            message_uuid=row["message_uuid"],
            user_uuid=user_uuid,
            devices=devices,
            content={},
        )
        if event["event_params"]["devices"]:
            _insert(session, event, user_uuid, row["message_uuid"])
        session.execute(
            """UPDATE messenger_push_outbox SET terminal=true, error_code='cancelled',
            event=jsonb_set(event, '{event_params,devices}', '[]'::jsonb) WHERE uuid=%s""",
            (row["uuid"],),
        )


class NotificationHttpSession(bazooka.Client.SESSION):
    def _sanitize_body(self, body: typing.Any) -> str:
        return "[REDACTED]"

    @staticmethod
    def _response_body_for_log(response: requests.Response) -> str:
        return "[REDACTED]"


class NotificationHttpClient(bazooka.Client):
    SESSION = NotificationHttpSession

    def request(self, method: str, url: str, **kwargs: typing.Any) -> requests.Response:
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise ValueError("notification_redirect")
        return response


@functools.lru_cache(maxsize=4)
def notification_client(
    endpoint: str, auth_file: str
) -> http_client.CollectionBaseClient:
    client = NotificationHttpClient(default_timeout=10)
    with open(auth_file) as stream:
        auth = http_client.CoreIamAuthenticator(**json.load(stream), http_client=client)
    return http_client.CollectionBaseClient(
        base_url=endpoint.rstrip("/"),
        http_client=client,
        auth=auth,
    )


def process_one(session: sessions.PgSQLSession) -> bool:
    if not mobile_push_opts.enabled():
        return False
    session.execute(
        """DELETE FROM messenger_push_outbox WHERE expires_at < NOW() - INTERVAL '1 hour'
        AND (error_code='cancelled' OR event->'event_params'->>'kind'=%s)""",
        (push_notifications.MESSAGE_CANCELLED,),
    )
    # Keep only cancellation routes for terminal creates; never retain old tokens/previews.
    session.execute(
        """UPDATE messenger_push_outbox SET event=jsonb_set(event, '{event_params,devices}', '[]'::jsonb)
        WHERE terminal AND event->'event_params'->'devices' <> '[]'::jsonb""",
        (),
    )
    row = session.execute(
        """
        SELECT * FROM messenger_push_outbox WHERE NOT terminal AND next_attempt_at <= NOW()
        ORDER BY created_at, uuid LIMIT 1 FOR UPDATE SKIP LOCKED
    """,
        (),
    ).fetchone()
    if row is None:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    if row["expires_at"] <= now:
        session.execute(
            """UPDATE messenger_push_outbox SET terminal=true, error_code='expired',
            event=jsonb_set(event, '{event_params,devices}', '[]'::jsonb) WHERE uuid=%s""",
            (row["uuid"],),
        )
        return True
    event = row["event"]
    targets = event["event_params"]["devices"]
    # Retire queued snapshots before handing them to Notification. Share locks keep
    # DELETE/token/key replacement serialized with this handoff transaction.
    current = session.execute(
        """SELECT uuid, registration_token, encryption FROM m_workspace_push_devices
        WHERE project_id=%s AND user_uuid=%s AND uuid=ANY(%s::uuid[])
        ORDER BY uuid FOR SHARE""",
        (
            row["project_id"],
            row["user_uuid"],
            [target["device_uuid"] for target in targets],
        ),
    ).fetchall()
    active = {
        (
            str(device["uuid"]),
            device["registration_token"],
            device["encryption"]["key_uuid"],
        )
        for device in current
    }
    remaining = [
        target
        for target in targets
        if (
            target["device_uuid"],
            target["registration_token"],
            target["content"]["key_uuid"],
        )
        in active
    ]
    if remaining != targets:
        event["event_params"]["devices"] = remaining
        session.execute(
            """UPDATE messenger_push_outbox SET event=%s::jsonb, terminal=%s,
            error_code=CASE WHEN %s THEN 'device_registration_changed' ELSE error_code END
            WHERE uuid=%s""",
            (json.dumps(event), not remaining, not remaining, row["uuid"]),
        )
    if not remaining:
        return True
    client = notification_client(
        cfg.CONF.mobile_push.notification_endpoint,
        cfg.CONF.mobile_push.notification_auth_file,
    )
    # Use the same UUID after timeout/restart. The saved encrypted snapshot never changes.
    event["last_retry_at"] = row["expires_at"].isoformat()
    try:
        try:
            result = client.get("/v1/events/", str(row["uuid"]))
        except http_exceptions.NotFoundError:
            try:
                result = client.create("/v1/events/", data=event)
            except http_exceptions.ConflictError:
                result = client.get("/v1/events/", str(row["uuid"]))
    except Exception:  # noqa: BLE001 - untrusted provider exceptions can echo credentials
        # Provider errors can contain echoed tokens; never log exception bodies.
        LOG.warning("Notification request failed for event %s", row["uuid"])
        result = {"status": "IN_PROGRESS"}
    for error in result.get("errors", []):
        if error.get("code") != "registration_token_inactive":
            continue
        for target in event["event_params"]["devices"]:
            if target["device_uuid"] == error.get("device_uuid"):
                # A response for an old snapshot cannot delete a refreshed token.
                session.execute(
                    """DELETE FROM m_workspace_push_devices
                    WHERE uuid=%s AND project_id=%s AND user_uuid=%s AND registration_token=%s""",
                    (
                        target["device_uuid"],
                        row["project_id"],
                        row["user_uuid"],
                        target["registration_token"],
                    ),
                )
    errors = result.get("errors", [])
    terminal = result["status"] == "ACTIVE" or (
        result["status"] == "ERROR"
        and bool(errors)
        and all(
            error.get("permanent", error.get("code") == "registration_token_inactive")
            for error in errors
        )
    )
    code = "delivery_failed" if result["status"] == "ERROR" else None
    session.execute(
        """UPDATE messenger_push_outbox SET terminal=%s, error_code=%s,
        next_attempt_at=NOW()+INTERVAL '3 seconds',
        event=CASE WHEN %s THEN jsonb_set(event, '{event_params,devices}', '[]'::jsonb) ELSE event END
        WHERE uuid=%s""",
        (terminal, code, terminal, row["uuid"]),
    )
    return True
