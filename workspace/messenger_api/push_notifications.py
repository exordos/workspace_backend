# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import base64
import json
import typing
import uuid as sys_uuid

from workspace.external_bridge_control import hpke
from workspace.messenger_api.dm import push_devices


MESSAGE_CREATE = "workspace_message_create"
MESSAGE_CANCELLED = "workspace_message_cancelled"
EVENT_TYPES = {
    kind: sys_uuid.uuid5(sys_uuid.NAMESPACE_URL, f"urn:exordos:notification:{kind}")
    for kind in (MESSAGE_CREATE, MESSAGE_CANCELLED)
}
HPKE_INFO = b"workspace-mobile-push"
ROUTE_FIELDS = (
    "organization_uuid",
    "project_uuid",
    "stream_uuid",
    "topic_uuid",
    "message_uuid",
)


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
    message_uuid: sys_uuid.UUID,
    user_uuid: sys_uuid.UUID,
    devices: typing.Iterable[push_devices.PushDevice],
    content: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    """Prepare an opaque, self-contained event before it leaves Workspace."""
    event = {
        "uuid": str(event_uuid),
        "project_id": str(project_uuid),
        "event_type": f"/v1/event_types/{EVENT_TYPES[kind]}",
        "exchange": {"kind": "User", "user_uuid": str(user_uuid)},
        "event_params": {
            "kind": kind,
            "organization_uuid": str(organization_uuid),
            "project_uuid": str(project_uuid),
            "stream_uuid": str(stream_uuid),
            "topic_uuid": str(topic_uuid),
            "message_uuid": str(message_uuid),
            "devices": [],
        },
    }
    plaintext = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    for device in devices:
        encryption = device.encryption
        public_key = base64.urlsafe_b64decode(encryption.public_key + "=")
        enc, ciphertext = hpke.seal(
            public_key,
            plaintext,
            associated_data(event, device.uuid, encryption.key_uuid),
            HPKE_INFO,
        )
        event["event_params"]["devices"].append(
            {
                "device_uuid": str(device.uuid),
                "registration_token": device.registration_token,
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
        )
    return event
