#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
import typing
import uuid as sys_uuid

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic


MARKDOWN_CONTENT_MAX_LENGTH = 40_000
OPAQUE_CONTENT_MAX_LENGTH = 40_000
PUBLIC_KEY_MAX_LENGTH = 40_000
DEVICE_ID_MAX_LENGTH = 255
DEVICE_NAME_MAX_LENGTH = 255


class EncryptionKeyType(types.Dict):
    _key_uuid_type = types.UUID()
    _public_key_type = types.String(
        min_length=1,
        max_length=PUBLIC_KEY_MAX_LENGTH,
    )

    def validate(self, value: typing.Any) -> bool:
        return (
            super().validate(value)
            and set(value) == {"key_uuid", "public_key"}
            and self._key_uuid_type.validate(value["key_uuid"])
            and self._public_key_type.validate(value["public_key"])
        )

    def from_simple_type(self, value: typing.Any) -> dict[str, typing.Any]:
        value = super().from_simple_type(value)
        if set(value) != {"key_uuid", "public_key"}:
            raise ValueError("Invalid encryption key fields")
        result = {
            "key_uuid": self._key_uuid_type.from_simple_type(value["key_uuid"]),
            "public_key": value["public_key"],
        }
        if not self.validate(result):
            raise ValueError("Invalid encryption key")
        return result

    def to_simple_type(
        self, value: typing.Mapping[str, typing.Any]
    ) -> dict[str, typing.Any]:
        return {
            "key_uuid": self._key_uuid_type.to_simple_type(value["key_uuid"]),
            "public_key": value["public_key"],
        }

    def to_openapi_spec(
        self, prop_kwargs: dict[str, typing.Any]
    ) -> dict[str, typing.Any]:
        openapi = prop_kwargs.get(types.OPENAPI_KEYWORD)
        nested_kwargs = {} if openapi is None else {types.OPENAPI_KEYWORD: openapi}
        spec = {
            "type": "object",
            "required": ["key_uuid", "public_key"],
            "additionalProperties": False,
            "properties": {
                "key_uuid": self._key_uuid_type.to_openapi_spec(nested_kwargs),
                "public_key": self._public_key_type.to_openapi_spec(nested_kwargs),
            },
            "example": {
                "key_uuid": str(sys_uuid.UUID(int=0)),
                "public_key": "base64-public-key",
            },
        }
        spec.update(types.build_prop_kwargs(kwargs=prop_kwargs))
        return spec


ENCRYPTION_KEY_TYPE = EncryptionKeyType()


def markdown_content(payload: typing.Any) -> str:
    if isinstance(payload, dict):
        if payload.get("kind") != MarkdownPayload.KIND:
            return ""
        content = payload.get("content")
    else:
        if not isinstance(payload, MarkdownPayload):
            return ""
        content = payload.content
    return content if isinstance(content, str) else ""


class MarkdownPayload(types_dynamic.AbstractKindModel):
    KIND = "markdown"

    content = properties.property(
        types.String(min_length=1, max_length=MARKDOWN_CONTENT_MAX_LENGTH),
        required=True,
    )

    def is_user_mentioned(self, user_uuid: sys_uuid.UUID) -> bool:
        return (
            re.search(
                r"\]\(urn:user:" + re.escape(str(user_uuid)) + r"\)",
                self.content,
                flags=re.IGNORECASE,
            )
            is not None
        )


class EncryptedPayload(types_dynamic.AbstractKindModel):
    KIND = "e2ee"

    key_uuid = properties.property(types.UUID(), required=True)
    content = properties.property(
        types.String(min_length=1, max_length=OPAQUE_CONTENT_MAX_LENGTH),
        required=True,
    )


class ChatKeyPayloadBase(types_dynamic.AbstractKindModel):
    key_uuid = properties.property(types.UUID(), required=True)


class KeyRequestPayload(ChatKeyPayloadBase):
    KIND = "key_request"

    device_id = properties.property(
        types.String(min_length=1, max_length=DEVICE_ID_MAX_LENGTH),
        required=True,
    )
    device_name = properties.property(
        types.String(min_length=1, max_length=DEVICE_NAME_MAX_LENGTH),
        required=True,
    )
    public_key = properties.property(
        types.String(min_length=1, max_length=PUBLIC_KEY_MAX_LENGTH),
        required=True,
    )


class KeyGrantPayload(ChatKeyPayloadBase):
    KIND = "key_grant"

    request_message_uuid = properties.property(types.UUID(), required=True)
    sender_device_id = properties.property(
        types.String(min_length=1, max_length=DEVICE_ID_MAX_LENGTH),
        required=True,
    )
    recipient_device_id = properties.property(
        types.String(min_length=1, max_length=DEVICE_ID_MAX_LENGTH),
        required=True,
    )
    content = properties.property(
        types.String(min_length=1, max_length=OPAQUE_CONTENT_MAX_LENGTH),
        required=True,
    )


class KeyRejectPayload(ChatKeyPayloadBase):
    KIND = "key_reject"

    request_message_uuid = properties.property(types.UUID(), required=True)
    sender_device_id = properties.property(
        types.String(min_length=1, max_length=DEVICE_ID_MAX_LENGTH),
        required=True,
    )
    recipient_device_id = properties.property(
        types.String(min_length=1, max_length=DEVICE_ID_MAX_LENGTH),
        required=True,
    )


class KeyAnnouncedPayload(ChatKeyPayloadBase):
    KIND = "key_announced"

    public_key = properties.property(
        types.String(min_length=1, max_length=PUBLIC_KEY_MAX_LENGTH),
        required=True,
    )


WORKSPACE_MESSAGE_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MarkdownPayload),
    types_dynamic.KindModelType(EncryptedPayload),
    types_dynamic.KindModelType(KeyRequestPayload),
    types_dynamic.KindModelType(KeyGrantPayload),
    types_dynamic.KindModelType(KeyRejectPayload),
    types_dynamic.KindModelType(KeyAnnouncedPayload),
)
