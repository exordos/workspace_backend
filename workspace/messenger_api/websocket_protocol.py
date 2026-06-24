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

import urllib.parse

from workspace.messenger_api import events as messenger_events


WORKSPACE_EVENTS_PROTOCOL = "workspace.events.v1"
BEARER_PROTOCOL_PREFIX = "bearer."


def select_subprotocol(client_subprotocols, server_subprotocols):
    if WORKSPACE_EVENTS_PROTOCOL in client_subprotocols:
        return WORKSPACE_EVENTS_PROTOCOL
    return None


def bearer_token_from_subprotocols(header_value):
    if not header_value:
        return None
    for item in header_value.split(","):
        protocol = item.strip()
        if protocol.startswith(BEARER_PROTOCOL_PREFIX):
            token = protocol[len(BEARER_PROTOCOL_PREFIX):].strip()
            return token or None
    return None


def parse_last_epoch_version(path):
    parsed = urllib.parse.urlsplit(path or "")
    params = urllib.parse.parse_qs(parsed.query)
    values = params.get("last_epoch_version") or []
    value = values[0] if values else None
    return messenger_events.normalize_epoch_version(value)
