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
import logging
import random
import re
import time
import typing

import webob

from gcl_iam import middlewares as iam_middlewares
from restalchemy.api import middlewares

from workspace.messenger_api import exceptions as messenger_exceptions
from workspace.messenger_api.api import versions


SERVER_SETTINGS_PATH = f"/{versions.API_VERSION_1_0}/server_settings"
DATABASE_DEADLOCK_MAX_ATTEMPTS = 3
DATABASE_DEADLOCK_RETRY_BASE_SECONDS = 0.05
LOG = logging.getLogger(__name__)
_IDEMPOTENT_READ_ACTION_PATH = re.compile(
    rf"^/{versions.API_VERSION_1_0}/"
    r"(?:streams|stream_topics)/[^/]+/actions/read/invoke/?$"
    rf"|^/{versions.API_VERSION_1_0}/messages/[^/]+/"
    r"actions/(?:read|read_up_to)/invoke/?$"
)


def _normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _get_realm_url(req: typing.Any) -> str:
    proto = req.headers.get("X-Forwarded-Proto", "https")
    return f"{proto}://{req.headers['Host']}"


def build_server_settings(req: typing.Any) -> dict[str, typing.Any]:
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
            "openid connect": False,
        },
        "push_notifications_enabled": True,
        "email_auth_enabled": True,
        "require_email_format_usernames": True,
        "realm_url": realm_url,
        "realm_name": "Exordos Workspace",
        "realm_icon": f"urn:url:{realm_url}/logo-512x512.png",
        "realm_description": "<p>Exordos Workspace messenger.</p>",
        "realm_web_public_access_enabled": False,
        "meet_url": "https://meet.genesis-core.tech",
        "external_authentication_methods": [],
        "realm_uri": realm_url,
    }
    if req.GET:
        result["ignored_parameters_unsupported"] = sorted(req.GET)
    return result


class ServerSettingsMiddleware(middlewares.Middleware):
    def process_request(self, req: typing.Any) -> webob.Response | None:
        if req.method == "GET" and _normalize_path(req.path) == SERVER_SETTINGS_PATH:
            body = json.dumps(build_server_settings(req)).encode("utf-8")
            return webob.Response(
                body=body,
                status=200,
                content_type="application/json",
                charset="utf-8",
            )
        return None


def _is_database_deadlock(error: BaseException) -> bool:
    """Recognize both raw psycopg and RESTAlchemy deadlock exceptions."""
    current: BaseException | None = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == "40P01":
            return True
        if getattr(current, "code", None) == "40P01":
            return True
        current = current.__cause__ or current.__context__
    return False


class DatabaseDeadlockRetryMiddleware(middlewares.Middleware):
    """Replay only transactionally idempotent read-state requests."""

    @webob.dec.wsgify
    def __call__(self, req: typing.Any) -> typing.Any:
        if req.method != "POST" or not _IDEMPOTENT_READ_ACTION_PATH.fullmatch(req.path):
            return req.get_response(self.application)

        body = req.body
        for attempt in range(1, DATABASE_DEADLOCK_MAX_ATTEMPTS + 1):
            try:
                req.body = body
                return req.get_response(self.application)
            except Exception as error:
                if not _is_database_deadlock(error):
                    raise
                if attempt == DATABASE_DEADLOCK_MAX_ATTEMPTS:
                    LOG.exception(
                        "Idempotent read-state transaction exhausted PostgreSQL "
                        "deadlock retries",
                        extra={
                            "deadlock_retry_attempt": attempt,
                            "request_path": req.path,
                        },
                    )
                    raise (
                        messenger_exceptions.DatabaseDeadlockRetryExhaustedError()
                    ) from error
                delay = DATABASE_DEADLOCK_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                delay *= random.uniform(0.75, 1.25)
                LOG.warning(
                    "Retrying idempotent read-state transaction after "
                    "PostgreSQL deadlock",
                    extra={
                        "deadlock_retry_attempt": attempt,
                        "deadlock_retry_delay_seconds": delay,
                        "request_path": req.path,
                    },
                )
                time.sleep(delay)
        raise AssertionError("unreachable")


class ErrorsHandlerMiddleware(iam_middlewares.ErrorsHandlerMiddleware):
    def _construct_error_response(
        self,
        req: typing.Any,
        error: Exception,
    ) -> typing.Any:
        if isinstance(error, messenger_exceptions.EventsCursorExpiredError):
            return req.ResponseClass(
                status=410,
                json=error.as_dict(),
                headers={"Cache-Control": "no-store"},
            )
        if isinstance(
            error,
            messenger_exceptions.DatabaseDeadlockRetryExhaustedError,
        ):
            return req.ResponseClass(
                status=503,
                json=error.as_dict(),
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        if isinstance(error, messenger_exceptions.DraftConflictError):
            return req.ResponseClass(status=409, json={"message": error.msg})
        if isinstance(error, messenger_exceptions.DraftPreconditionRequiredError):
            return req.ResponseClass(status=428, json={"message": error.msg})
        if isinstance(error, messenger_exceptions.DraftPreconditionFailedError):
            return req.ResponseClass(
                status=412,
                json={"current": error.current},
                headers={"ETag": f'"{error.current["revision"]}"'},
            )
        return super()._construct_error_response(req, error)
