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

from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.dm import types_dynamic

from workspace.messenger_api.dm import message_payloads


MESSAGE_EVENT_TIMESTAMP_TYPE = types.UTCDateTimeZ()


class MessageCreatedEventPayload(types_dynamic.AbstractKindModel):
    KIND = "message.created"

    uuid = properties.property(
        types.UUID(),
        required=True,
    )
    stream_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    topic_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    author_uuid = properties.property(
        types.UUID(),
        required=True,
    )
    payload = properties.property(
        message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE,
        required=True,
    )
    created_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        required=True,
    )
    updated_at = properties.property(
        MESSAGE_EVENT_TIMESTAMP_TYPE,
        required=True,
    )


WORKSPACE_EVENT_PAYLOAD_TYPE = types_dynamic.KindModelSelectorType(
    types_dynamic.KindModelType(MessageCreatedEventPayload),
)
