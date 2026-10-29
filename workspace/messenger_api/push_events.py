# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").
"""SDK topic events; preview preparation and device delivery run off the counter path."""

import datetime
import json
import logging
import typing
import uuid as sys_uuid

import bazooka
import requests
from gcl_sdk.events import clients as event_clients
from gcl_sdk.events.dm import models as event_models
from gcl_sdk.events.services import senders as event_senders
from oslo_config import cfg
from restalchemy.common import contexts
from restalchemy.dm import filters as dm_filters
from restalchemy.storage.sql import sessions

from workspace.common import mobile_push_opts
from workspace.messenger_api import push_notifications
from workspace.messenger_api.dm import schema
from workspace.messenger_api.dm import push_events as event_payloads
from workspace.messenger_api.dm import push_devices

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
    event: dict[str, typing.Any],
    user_uuid: sys_uuid.UUID,
) -> None:
    # Reuse the RA transaction, as Core does through AsyncEventClient.
    stored = event_models.Event(
        uuid=sys_uuid.UUID(event["uuid"]),
        event_type=event_models.UserEvent(
            uuid=push_notifications.EVENT_TYPES[push_notifications.TOPIC_UPDATE],
            user_id=user_uuid,
        ),
        event_data=event_payloads.WorkspacePushEventPayload(body=event),
    )
    event_clients.AsyncEventClient({}).send_event(stored)


def enqueue_topic(
    project_id: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    topic_uuid: sys_uuid.UUID,
    source_uuid: sys_uuid.UUID,
    previous_active: int,
) -> None:
    """Save only a signal; the independent handler reads current topic state."""
    if not mobile_push_opts.enabled():
        return
    event_clients.AsyncEventClient({}).send_event(
        event_models.Event(
            uuid=sys_uuid.uuid5(source_uuid, f"topic-push:{user_uuid}"),
            event_type=event_models.UserEvent(
                uuid=push_notifications.EVENT_TYPES[push_notifications.TOPIC_UPDATE],
                user_id=user_uuid,
            ),
            event_data=event_payloads.WorkspaceTopicSignal(
                body={
                    "project_id": str(project_id),
                    "user_uuid": str(user_uuid),
                    "stream_uuid": str(stream_uuid),
                    "topic_uuid": str(topic_uuid),
                    "source_uuid": str(source_uuid),
                    "previous_active_count": previous_active,
                }
            ),
        )
    )


def fetch_events(status: str | tuple[str, ...], kind: str) -> list[event_models.Event]:
    return event_models.Event.objects.get_all(
        filters={
            "status": dm_filters.In((status,) if isinstance(status, str) else status),
            "event_data": dm_filters.JSONFields({"kind": kind}),
        },
        limit=100,
        order_by={"created_at": "ASC", "uuid": "ASC"},
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


class PushDeliveryError(RuntimeError):
    """A provider failure whose text cannot disclose credentials or payloads."""


class PushEventHttpClient(event_clients.HttpEventClient):
    """Use SDK transport with the prepared Workspace payload and managed token."""

    def __init__(self, endpoint: str, auth_file: str) -> None:
        self._auth_file = auth_file
        super().__init__(
            event_type_mapping={},
            endpoint=endpoint.rstrip("/"),
            version="v1",
            auth_token="",
            project_id=None,
            http_client=NotificationHttpClient(default_timeout=10),
        )

    def _get_headers(self) -> dict[str, str]:
        with open(self._auth_file) as stream:
            token = json.load(stream)["access_token"]
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def _build_event_body(self, event: event_models.Event) -> dict[str, typing.Any]:
        if isinstance(event.event_data, event_payloads.WorkspaceTopicSignal):
            raise PushDeliveryError("Topic signal has not been encrypted")
        if isinstance(event.event_data, event_payloads.WorkspacePushEventPayload):
            return {
                key: value
                for key, value in event.event_data.body.items()
                if key != "_membership_generation"
            }
        return super()._build_event_body(event)

    def send_event(self, event: event_models.Event) -> event_models.Event:
        try:
            return super().send_event(event)
        except Exception:
            raise PushDeliveryError("Notification request failed") from None

    def get_event(self, event_uuid: str) -> dict[str, typing.Any]:
        try:
            return super().get_event(event_uuid)
        except event_clients.EventNotFound:
            raise
        except Exception:
            raise PushDeliveryError("Notification result request failed") from None


def _with_devices(
    event: event_models.Event, devices: list[dict[str, typing.Any]]
) -> None:
    body = dict(event.event_data.body)
    body["event_params"] = dict(body["event_params"], devices=devices)
    event.event_data = event_payloads.WorkspacePushEventPayload(body=body)


def _suppression_reason(body: dict[str, typing.Any]) -> str | None:
    if datetime.datetime.fromisoformat(body["last_retry_at"]) <= datetime.datetime.now(
        datetime.timezone.utc
    ):
        return "expired"
    params = body["event_params"]
    if not params["devices"]:
        return "device_registration_changed"
    return None


class WorkspacePushSender(event_senders.EventSenderService):
    """Only push policy; persistence, scheduling and retries belong to the SDK."""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(
            notification_client=PushEventHttpClient(
                cfg.CONF.mobile_push.notification_endpoint,
                cfg.CONF.mobile_push.notification_auth_file,
            ),
            enabled=mobile_push_opts.enabled(),
            **kwargs,
        )

    def _fetch_new_events(self) -> list[event_models.Event]:
        return fetch_events("NEW", event_payloads.WorkspacePushEventPayload.KIND)

    def _fetch_in_progress_events(self) -> list[event_models.Event]:
        return fetch_events(
            "IN_PROGRESS", event_payloads.WorkspacePushEventPayload.KIND
        )

    def _prepare_event(self, event: event_models.Event) -> bool:
        if not isinstance(event.event_data, event_payloads.WorkspacePushEventPayload):
            return True
        body = event.event_data.body
        with contexts.Context().session_manager() as session:
            schema.lock_shared(session)
            reason = _suppression_reason(body)
            if (
                reason is None
                and body["event_params"]["kind"] == push_notifications.TOPIC_UPDATE
            ):
                membership = session.execute(
                    """SELECT 1 FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s
                    AND stream_uuid=%s AND active AND membership_generation=%s""",
                    (
                        body["project_id"],
                        body["exchange"]["user_uuid"],
                        body["event_params"]["stream_uuid"],
                        body["_membership_generation"],
                    ),
                ).fetchone()
                if membership is None:
                    reason = "membership_changed"
            current = (
                _devices(
                    session,
                    sys_uuid.UUID(body["project_id"]),
                    sys_uuid.UUID(body["exchange"]["user_uuid"]),
                )
                if reason is None
                else []
            )
        active = {
            (
                str(device.uuid),
                device.registration_token,
                str(device.encryption.key_uuid),
            )
            for device in current
        }
        remaining = [
            target
            for target in body["event_params"]["devices"]
            if (
                target["device_uuid"],
                target["registration_token"],
                target["content"]["key_uuid"],
            )
            in active
        ]
        _with_devices(event, remaining)
        if reason is not None or not remaining:
            event.status = event_models.Event.STATUS.ERROR.value
            return False
        return True

    def _get_event_status(self, event: event_models.Event) -> typing.Any:
        if not isinstance(event.event_data, event_payloads.WorkspacePushEventPayload):
            return super()._get_event_status(event)
        reason = _suppression_reason(event.event_data.body)
        if reason:
            return {
                "status": "ERROR",
                "errors": [{"code": reason, "permanent": True}],
            }
        return self._notification_client.get_event(str(event.uuid))

    def _apply_event_status(
        self, event: event_models.Event, result: typing.Any
    ) -> None:
        if not isinstance(event.event_data, event_payloads.WorkspacePushEventPayload):
            super()._apply_event_status(event, result)
            return
        errors = result.get("errors", [])
        inactive = {
            error["device_uuid"]
            for error in errors
            if error.get("code") == "registration_token_inactive"
        }
        if inactive:
            body = event.event_data.body
            with contexts.Context().session_manager() as session:
                schema.lock_shared(session)
                for target in body["event_params"]["devices"]:
                    if target["device_uuid"] in inactive:
                        session.execute(
                            """DELETE FROM m_workspace_push_devices
                            WHERE uuid=%s AND project_id=%s AND user_uuid=%s AND registration_token=%s
                              AND encryption->>'key_uuid'=%s""",
                            (
                                target["device_uuid"],
                                body["project_id"],
                                body["exchange"]["user_uuid"],
                                target["registration_token"],
                                target["content"]["key_uuid"],
                            ),
                        )
        terminal = result["status"] == "ACTIVE" or (
            result["status"] == "ERROR"
            and bool(errors)
            and all(
                error.get(
                    "permanent", error.get("code") == "registration_token_inactive"
                )
                for error in errors
            )
        )
        event.status = (
            result["status"]
            if terminal
            else event_models.Event.STATUS.IN_PROGRESS.value
        )
        if terminal:
            _with_devices(event, [])


def prune(session: sessions.PgSQLSession) -> None:
    """Discard old terminal delivery results and clear terminal ciphertext."""
    session.execute(
        """DELETE FROM gcl_sdk_events WHERE uuid IN (
        SELECT uuid FROM gcl_sdk_events
        WHERE event_data->>'kind' IN ('WorkspacePush','WorkspaceTopicSignal') AND status IN ('ACTIVE','ERROR')
          AND created_at<NOW()-INTERVAL '1 hour'
        LIMIT 500 FOR UPDATE SKIP LOCKED)""",
        (),
    )
    session.execute(
        """UPDATE gcl_sdk_events SET event_data=jsonb_set(event_data,'{body,event_params,devices}','[]'::jsonb)
        WHERE uuid IN (SELECT uuid FROM gcl_sdk_events WHERE event_data->>'kind'='WorkspacePush'
          AND status IN ('ACTIVE','ERROR') AND event_data->'body'->'event_params'->'devices'<>'[]'::jsonb
        LIMIT 500 FOR UPDATE SKIP LOCKED)""",
        (),
    )
