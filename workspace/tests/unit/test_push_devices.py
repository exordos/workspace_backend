# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
from unittest import mock
import uuid as sys_uuid

import pytest
from restalchemy.common import exceptions as ra_exc
from restalchemy.storage.sql import orm as ra_orm

from workspace.messenger_api.api import controllers
from workspace.messenger_api.dm import models
from workspace.messenger_api.dm import push_devices


def _public_key(value: bytes = bytes(range(32))) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_hpke_encryption_uses_kind_selector():
    key_uuid = sys_uuid.uuid4()
    public_key = _public_key()
    encryption = push_devices.PUSH_DEVICE_ENCRYPTION_TYPE.from_simple_type(
        {
            "kind": "HPKE",
            "algorithm": push_devices.HPKE_ALGORITHM,
            "key_uuid": str(key_uuid),
            "public_key": public_key,
        },
    )

    assert encryption.kind == "HPKE"
    assert encryption.algorithm == push_devices.HPKE_ALGORITHM
    assert encryption.key_uuid == key_uuid
    assert encryption.public_key == public_key


@pytest.mark.parametrize(
    "public_key",
    [
        "",
        "not-base64url",
        _public_key() + "=",
        _public_key(bytes(range(31))),
        _public_key(bytes(range(32)))[:-1] + "+",
    ],
)
def test_hpke_registration_rejects_noncanonical_x25519_public_keys(public_key):
    with pytest.raises((ra_exc.RestAlchemyException, ValueError)):
        push_devices.PushDeviceHPKEEncryption(
            algorithm=push_devices.HPKE_ALGORITHM,
            key_uuid=sys_uuid.uuid4(),
            public_key=public_key,
        )


def test_hpke_registration_rejects_unknown_kind():
    with pytest.raises(ValueError):
        push_devices.PUSH_DEVICE_ENCRYPTION_TYPE.from_simple_type(
            {
                "kind": "RSA",
                "algorithm": push_devices.HPKE_ALGORITHM,
                "key_uuid": str(sys_uuid.uuid4()),
                "public_key": _public_key(),
            },
        )


def test_push_device_transport_and_platform_enums_match_supported_clients():
    assert [value.value for value in push_devices.PushDeviceTransport] == ["fcm"]
    assert [value.value for value in push_devices.PushDevicePlatform] == [
        "android",
        "ios",
    ]


def test_push_device_update_locks_owner_before_device_lookup():
    project_id = sys_uuid.uuid4()
    user_uuid = sys_uuid.uuid4()
    device_uuid = sys_uuid.uuid4()
    session = mock.sentinel.session
    request = mock.Mock()
    request.context.project_id = project_id
    request.context.user_uuid = user_uuid
    controller = controllers.PushDeviceController(request)
    device = mock.Mock(project_id=project_id, user_uuid=user_uuid)
    calls = []

    def lock_owner(collection, **kwargs):
        assert collection.model_cls is models.WorkspaceUser
        calls.append(("owner", kwargs))
        return mock.sentinel.owner

    def find_device(collection, **kwargs):
        assert collection.model_cls is push_devices.PushDevice
        calls.append(("device", kwargs))
        return device

    encryption = push_devices.PushDeviceHPKEEncryption(
        algorithm=push_devices.HPKE_ALGORITHM,
        key_uuid=sys_uuid.uuid4(),
        public_key=_public_key(),
    )
    with (
        mock.patch.object(
            controllers.contexts.Context,
            "get_session",
            return_value=session,
        ),
        mock.patch.object(
            ra_orm.ObjectCollection,
            "get_one",
            autospec=True,
            side_effect=lock_owner,
        ),
        mock.patch.object(
            ra_orm.ObjectCollection,
            "get_one_or_none",
            autospec=True,
            side_effect=find_device,
        ),
        mock.patch.object(controller, "_response", return_value={}),
    ):
        response = controller.update(
            device_uuid,
            transport=push_devices.PushDeviceTransport.FCM.value,
            platform=push_devices.PushDevicePlatform.IOS.value,
            registration_token="token",
            encryption=encryption,
        )

    assert [name for name, _ in calls] == ["owner", "device"]
    assert calls[0][1]["filters"]["uuid"].value == user_uuid
    assert calls[0][1]["session"] is session
    assert calls[0][1]["locked"] is True
    assert calls[1][1]["filters"]["uuid"].value == device_uuid
    assert calls[1][1]["session"] is session
    assert response == ({}, 200, {})
