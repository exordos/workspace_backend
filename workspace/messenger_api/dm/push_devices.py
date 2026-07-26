# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import base64
import enum
import typing

from restalchemy.common import exceptions as ra_exc
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic
from restalchemy.storage.sql import orm

from workspace.messenger_api.dm import base


HPKE_KIND = "HPKE"
HPKE_ALGORITHM = "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM"
X25519_PUBLIC_KEY_BYTES = 32


class PushDeviceTransport(str, enum.Enum):
    FCM = "fcm"


class PushDevicePlatform(str, enum.Enum):
    ANDROID = "android"
    IOS = "ios"


def _decode_x25519_public_key(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        padding = b"=" * ((4 - len(encoded) % 4) % 4)
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise ra_exc.ValidationErrorException() from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=")
    if len(decoded) != X25519_PUBLIC_KEY_BYTES or canonical != encoded:
        raise ra_exc.ValidationErrorException()
    return decoded


class PushDeviceHPKEEncryption(types_dynamic.AbstractKindModel):
    KIND = HPKE_KIND

    algorithm = properties.property(
        types.Enum([HPKE_ALGORITHM]),
        required=True,
    )
    key_uuid = properties.property(types.UUID(), required=True)
    public_key = properties.property(
        types.String(min_length=43, max_length=43),
        required=True,
    )

    def validate(self) -> None:
        _decode_x25519_public_key(self.public_key)


class PushDeviceEncryptionType(types_dynamic.KindModelSelectorType):
    def from_simple_type(self, value: typing.Any) -> typing.Any:
        try:
            return super().from_simple_type(value)
        except types_dynamic.UnknownType as exc:
            raise ValueError from exc


PUSH_DEVICE_ENCRYPTION_TYPE = PushDeviceEncryptionType(
    types_dynamic.KindModelType(PushDeviceHPKEEncryption),
)


class PushDevice(
    base.UserScopedModelWithUUID,
    models.ModelWithProject,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "m_workspace_push_devices"

    transport = properties.property(
        types.Enum([value.value for value in PushDeviceTransport]),
        required=True,
    )
    platform = properties.property(
        types.Enum([value.value for value in PushDevicePlatform]),
        required=True,
    )
    registration_token = properties.property(
        types.String(min_length=1, max_length=4096),
        required=True,
    )
    encryption = properties.property(
        PUSH_DEVICE_ENCRYPTION_TYPE,
        required=True,
    )
