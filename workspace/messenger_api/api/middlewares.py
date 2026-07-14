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

import json

import webob

from restalchemy.api import middlewares

from workspace.messenger_api.api import versions


SERVER_SETTINGS_PATH = (
    f"/{versions.API_VERSION_1_0}/messenger/server_settings"
)


def _normalize_path(path):
    return path.rstrip("/") or "/"


def _get_realm_url(req):
    proto = req.headers.get("X-Forwarded-Proto", req.scheme)
    return f"{proto}://{req.headers['Host']}"


def build_server_settings(req):
    realm_url = _get_realm_url(req)
    result = {
        "result": "success",
        "msg": "Welcome to Exordos Workspace",
        "authentication_methods": {
            "password": True,
            "dev": False,
            "email": True,
            "ldap": False,
            "remoteuser": False,
            "github": False,
            "azuread": False,
            "gitlab": False,
            "google": False,
            "apple": False,
            "saml": False,
            "openid connect": False
        },
        "push_notifications_enabled": True,
        "email_auth_enabled": True,
        "require_email_format_usernames": True,
        "realm_url": realm_url,
        "realm_name": "Exordos Workspace",
        "realm_icon": "",
        "realm_description": "<p>Exordos Workspace messenger.</p>",
        "realm_web_public_access_enabled": False,
        "meet_url": "https://meet.genesis-core.tech",
        "external_authentication_methods": [],
        "realm_uri": realm_url
    }
    if req.GET:
        result["ignored_parameters_unsupported"] = sorted(req.GET)
    return result


class ServerSettingsMiddleware(middlewares.Middleware):
    def process_request(self, req):
        if (
            req.method == "GET"
            and _normalize_path(req.path) == SERVER_SETTINGS_PATH
        ):
            body = json.dumps(build_server_settings(req)).encode("utf-8")
            return webob.Response(
                body=body,
                status=200,
                content_type="application/json",
                charset="utf-8",
            )
        return None
