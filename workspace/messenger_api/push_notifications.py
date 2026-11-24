# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import base64
import hashlib
import json
import logging
import typing
import uuid as sys_uuid

from workspace.messenger_api import push_hpke as hpke
from workspace.messenger_api.dm import push_devices


LOG = logging.getLogger(__name__)

MESSAGE_CREATE = "notification_message_create"
MESSAGE_CANCELLED = "notification_message_cancel"
TOPIC_UPDATE = "notification_topic_update"
APNS_TOPIC_UPDATE = "notification_apns_topic_update"
EVENT_TYPES = {
    kind: sys_uuid.uuid5(sys_uuid.NAMESPACE_URL, f"urn:exordos:notification:{kind}")
    for kind in (MESSAGE_CREATE, MESSAGE_CANCELLED, TOPIC_UPDATE, APNS_TOPIC_UPDATE)
}
HPKE_INFO = b"workspace-mobile-push"
# Leave room for Base64 expansion, HPKE and the fixed APNs/Workspace envelope.
APNS_PLAINTEXT_LIMIT = 2_000
APNS_PREVIEW_LIMITS = {
    "stream_name": 128,
    "topic_name": 128,
    "sender_name": 64,
    "text": 192,
}
ROUTE_FIELDS = (
    "organization_uuid",
    "project_uuid",
    "stream_uuid",
    "topic_uuid",
    "message_uuid",
)


def _compact_json(data: dict[str, typing.Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def associated_data(
    event: dict[str, typing.Any],
    device_uuid: object,
    key_uuid: object,
) -> bytes:
    params = event["event_params"]
    values = [
        event["uuid"],
        params["kind"],
        *(params[field] for field in ROUTE_FIELDS),
        event["exchange"]["user_uuid"],
        str(device_uuid),
        str(key_uuid),
    ]
    return json.dumps(values, separators=(",", ":")).encode("ascii")


def build_event(
    *,
    event_uuid: sys_uuid.UUID,
    kind: str,
    organization_uuid: sys_uuid.UUID,
    project_uuid: sys_uuid.UUID,
    stream_uuid: sys_uuid.UUID,
    topic_uuid: sys_uuid.UUID,
    message_uuid: sys_uuid.UUID | None,
    user_uuid: sys_uuid.UUID,
    device: push_devices.PushDevice,
    content: dict[str, typing.Any],
    priority: str = "normal",
) -> dict[str, typing.Any] | None:
    """Prepare an opaque, self-contained event before it leaves Workspace."""
    apns_environment = push_devices.APNS_ENVIRONMENTS.get(device.transport)
    event_type = APNS_TOPIC_UPDATE if apns_environment else kind
    event: dict[str, typing.Any] = {
        "uuid": str(event_uuid),
        "project_id": str(project_uuid),
        "event_type": f"/v1/event_types/{EVENT_TYPES[event_type]}",
        "exchange": {"kind": "User", "user_uuid": str(user_uuid)},
        "event_params": {
            "kind": kind,
            "organization_uuid": str(organization_uuid),
            "project_uuid": str(project_uuid),
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "message_uuid": str(message_uuid) if message_uuid is not None else None,
            "device_uuid": str(device.uuid),
            "registration_token": device.registration_token,
            "priority": priority,
        },
    }
    if apns_environment:
        event["event_params"].update(
            apns_environment=apns_environment,
            apns_collapse_id=hashlib.sha256(
                f"{device.uuid}:{topic_uuid}".encode()
            ).hexdigest(),
        )
        event["_registration_updated_at"] = device.updated_at.isoformat()
        content = dict(
            content,
            **{
                field: content[field].encode()[:limit].decode(errors="ignore")
                for field, limit in APNS_PREVIEW_LIMITS.items()
            },
        )
    plaintext = _compact_json(content).encode("utf-8")
    if apns_environment and len(plaintext) > APNS_PLAINTEXT_LIMIT:
        # JSON escapes can expand a preview past the budget. Keep the unread
        # counters and message route so the alert can still open the topic.
        without_preview = dict(content, **dict.fromkeys(APNS_PREVIEW_LIMITS, ""))
        plaintext = _compact_json(without_preview).encode("utf-8")
    encryption = device.encryption
    public_key = base64.urlsafe_b64decode(encryption.public_key + "=")
    try:
        enc, ciphertext = hpke.seal(
            public_key,
            plaintext,
            associated_data(event, device.uuid, encryption.key_uuid),
            HPKE_INFO,
        )
    except ValueError:
        # A legacy bad key must not abort native chat fanout for every recipient.
        LOG.warning("Invalid push encryption key for device %s", device.uuid)
        return None
    payload = {
        "uuid": str(event_uuid),
        "kind": kind,
        "organization_uuid": str(organization_uuid),
        "project_uuid": str(project_uuid),
        "stream_uuid": str(stream_uuid),
        "topic_uuid": str(topic_uuid),
        "message_uuid": str(message_uuid) if message_uuid is not None else None,
        "user_uuid": str(user_uuid),
        "device_uuid": str(device.uuid),
        "content": {
            "kind": "encrypted",
            "algorithm": encryption.algorithm,
            "key_uuid": str(encryption.key_uuid),
            "enc": base64.urlsafe_b64encode(enc).rstrip(b"=").decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(ciphertext)
            .rstrip(b"=")
            .decode("ascii"),
        },
    }
    event["event_params"]["payload"] = _compact_json(payload)
    return event


def apns_payload(snapshot: str, priority: str) -> str:
    # The system can display the fallback without decrypting the preview.
    aps: dict[str, object] = {"content-available": 1}
    if priority == "high":
        aps.update(alert={"loc-key": "WORKSPACE_PUSH_NEW_MESSAGE"}, sound="default")
    return _compact_json({"aps": aps, "workspace": json.loads(snapshot)})
