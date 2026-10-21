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

import collections.abc

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic


MARKDOWN_CONTENT_MAX_LENGTH = 40_000
E2EE_CONTENT_MAX_LENGTH = 4 * ((4 * MARKDOWN_CONTENT_MAX_LENGTH + 2) // 3)
PUBLIC_KEY_MAX_LENGTH = 2048


def markdown_content(payload) -> str:
    if isinstance(payload, collections.abc.Mapping):
        return payload["content"] if payload["kind"] == MarkdownPayload.KIND else ""
    return payload.content if payload.kind == MarkdownPayload.KIND else ""


class MarkdownPayload(types_dynamic.AbstractKindModel):
    KIND = "markdown"

    content = properties.property(
        types.String(min_length=1, max_length=MARKDOWN_CONTENT_MAX_LENGTH),
        required=True,
    )


class E2EEPayload(types_dynamic.AbstractKindModel):
    KIND = "e2ee"

    key_uuid = properties.property(types.UUID(), required=True)
    content = properties.property(
        types.String(min_length=1, max_length=E2EE_CONTENT_MAX_LENGTH),
        required=True,
    )


class KeyRequestPayload(types_dynamic.AbstractKindModel):
    KIND = "key_request"

    key_uuid = properties.property(types.UUID(), required=True)
    device_id = properties.property(types.String(max_length=255), required=True)
    device_name = properties.property(types.String(max_length=255), required=True)
    public_key = properties.property(
        types.String(min_length=1, max_length=PUBLIC_KEY_MAX_LENGTH),
        required=True,
    )


class KeyGrantPayload(types_dynamic.AbstractKindModel):
    KIND = "key_grant"

    key_uuid = properties.property(types.UUID(), required=True)
    request_message_uuid = properties.property(types.UUID(), required=True)
    sender_device_id = properties.property(types.String(max_length=255), required=True)
    recipient_device_id = properties.property(
        types.String(max_length=255), required=True
    )
    content = properties.property(
        types.String(min_length=1, max_length=40_000),
        required=True,
    )


class KeyRejectPayload(types_dynamic.AbstractKindModel):
    KIND = "key_reject"

    key_uuid = properties.property(types.UUID(), required=True)
    request_message_uuid = properties.property(types.UUID(), required=True)
    sender_device_id = properties.property(types.String(max_length=255), required=True)
    recipient_device_id = properties.property(
        types.String(max_length=255), required=True
    )


class KeyAnnouncedPayload(types_dynamic.AbstractKindModel):
    KIND = "key_announced"

    key_uuid = properties.property(types.UUID(), required=True)


WORKSPACE_MESSAGE_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MarkdownPayload),
    types_dynamic.KindModelType(E2EEPayload),
    types_dynamic.KindModelType(KeyRequestPayload),
    types_dynamic.KindModelType(KeyGrantPayload),
    types_dynamic.KindModelType(KeyRejectPayload),
    types_dynamic.KindModelType(KeyAnnouncedPayload),
)
