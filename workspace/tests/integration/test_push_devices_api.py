# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import uuid as sys_uuid

from workspace.messenger_api.dm import push_devices


PUSH_DEVICES = "/v1/push_devices/"


def _public_key(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _payload(key_uuid, public_key, token="fcm-registration-token"):
    return {
        "transport": "fcm",
        "platform": "ios",
        "registration_token": token,
        "encryption": {
            "kind": "HPKE",
            "algorithm": push_devices.HPKE_ALGORITHM,
            "key_uuid": str(key_uuid),
            "public_key": public_key,
        },
    }


def test_push_device_put_rotate_and_delete_are_scoped(
    workspace_api,
    db,
):
    device_uuid = sys_uuid.uuid4()
    key_uuid = sys_uuid.uuid4()
    public_key_bytes = bytes(range(32))
    public_key = _public_key(public_key_bytes)

    created = workspace_api.put(
        f"{PUSH_DEVICES}{device_uuid}",
        json=_payload(key_uuid, public_key),
    )

    assert created.status_code == 201, created.text
    resource = created.json()
    assert resource == {
        "uuid": str(device_uuid),
        "project_id": workspace_api.project_id,
        "user_uuid": workspace_api.user_uuid,
        "transport": "fcm",
        "platform": "ios",
        "registration_token": "fcm-registration-token",
        "encryption": {
            "kind": "HPKE",
            "algorithm": push_devices.HPKE_ALGORITHM,
            "key_uuid": str(key_uuid),
            "public_key": public_key,
        },
        "created_at": resource["created_at"],
        "updated_at": resource["updated_at"],
    }

    replacement_key_uuid = sys_uuid.uuid4()
    replacement_key_bytes = bytes(reversed(range(32)))
    replacement_public_key = _public_key(replacement_key_bytes)
    rotated = workspace_api.put(
        f"{PUSH_DEVICES}{device_uuid}",
        json=_payload(
            replacement_key_uuid,
            replacement_public_key,
            token="rotated-fcm-registration-token",
        ),
    )

    assert rotated.status_code == 200, rotated.text
    rotated_resource = rotated.json()
    assert rotated_resource["uuid"] == str(device_uuid)
    assert rotated_resource["created_at"] == resource["created_at"]
    assert rotated_resource["encryption"]["key_uuid"] == str(
        replacement_key_uuid,
    )
    assert rotated_resource["registration_token"] == (
        "rotated-fcm-registration-token"
    )
    assert rotated_resource["encryption"]["public_key"] == replacement_public_key
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                registration_token,
                encryption->>'public_key'
            FROM m_workspace_push_devices
            WHERE uuid = %s
            """,
            (str(device_uuid),),
        )
        stored = cursor.fetchone()
    assert stored == (
        "rotated-fcm-registration-token",
        replacement_public_key,
    )

    other_user = sys_uuid.uuid4()
    denied = workspace_api.put(
        f"{PUSH_DEVICES}{device_uuid}",
        json=_payload(sys_uuid.uuid4(), _public_key(b"x" * 32)),
        user=other_user,
    )
    assert denied.status_code == 404, denied.text

    other_delete = workspace_api.delete(
        f"{PUSH_DEVICES}{device_uuid}",
        user=other_user,
    )
    assert other_delete.status_code == 204, other_delete.text

    deleted = workspace_api.delete(f"{PUSH_DEVICES}{device_uuid}")
    assert deleted.status_code == 204, deleted.text
    deleted_again = workspace_api.delete(f"{PUSH_DEVICES}{device_uuid}")
    assert deleted_again.status_code == 204, deleted_again.text
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM m_workspace_push_devices WHERE uuid = %s",
            (str(device_uuid),),
        )
        assert cursor.fetchone()[0] == 0


def test_push_device_put_rejects_unknown_kind_and_invalid_key(workspace_api):
    device_uuid = sys_uuid.uuid4()
    payload = _payload(sys_uuid.uuid4(), _public_key(bytes(range(32))))
    payload["encryption"]["kind"] = "RSA"

    unknown_kind = workspace_api.put(
        f"{PUSH_DEVICES}{device_uuid}",
        json=payload,
    )

    assert unknown_kind.status_code == 400, unknown_kind.text

    payload["encryption"]["kind"] = "HPKE"
    payload["encryption"]["public_key"] = "invalid"
    invalid_key = workspace_api.put(
        f"{PUSH_DEVICES}{device_uuid}",
        json=payload,
    )
    assert invalid_key.status_code == 400, invalid_key.text


def test_push_device_put_rejects_unsupported_platform(workspace_api):
    payload = _payload(
        sys_uuid.uuid4(),
        _public_key(bytes(range(32))),
    )
    payload["platform"] = "web"

    response = workspace_api.put(
        f"{PUSH_DEVICES}{sys_uuid.uuid4()}",
        json=payload,
    )

    assert response.status_code == 400, response.text
