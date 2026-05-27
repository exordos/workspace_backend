#    Copyright 2025 Genesis Corporation.
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

from restalchemy.common import status as ra_status
from restalchemy.openapi import constants as oa_c


FOLDER_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "uuid": {
            "type": "string",
            "format": "uuid",
        },
        "chat_id": {
            "type": "integer",
        },
        "chat_type": {
            "type": "string",
            "enum": ["stream", "group", "private"],
        },
        "order_index": {
            "type": "integer",
            "nullable": True,
        },
        "pinned_at": {
            "type": "string",
            "format": "date-time",
            "nullable": True,
        },
        "created_at": {
            "type": "string",
            "format": "date-time",
        },
        "updated_at": {
            "type": "string",
            "format": "date-time",
        },
    },
}

FOLDER_WITH_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "uuid": {
            "type": "string",
            "format": "uuid",
        },
        "title": {
            "type": "string",
        },
        "user_id": {
            "type": "integer",
        },
        "background_color_value": {
            "type": "integer",
            "nullable": True,
        },
        "unread_messages": {
            "type": "array",
            "items": {
                "type": "integer",
            },
        },
        "system_type": {
            "type": "string",
            "nullable": True,
            "enum": ["all", "created"],
        },
        "created_at": {
            "type": "string",
            "format": "date-time",
        },
        "updated_at": {
            "type": "string",
            "format": "date-time",
        },
        "items": {
            "type": "array",
            "items": FOLDER_ITEM_SCHEMA,
        },
    },
}

FOLDER_FILTER_PARAMETERS = []

FOLDER_FILTER_RESPONSES = {
    ra_status.HTTP_200_OK: {
        "description": "List of folders with nested items",
        "content": {
            "application/json": {
                "schema": {
                    "type": "array",
                    "items": FOLDER_WITH_ITEMS_SCHEMA,
                },
            },
        },
    },
    "default": oa_c.DEFAULT_RESPONSE,
}
