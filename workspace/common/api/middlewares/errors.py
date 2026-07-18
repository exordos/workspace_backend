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

from http import client as http_client
from typing import Any

from bazooka import exceptions as bazooka_exc
from restalchemy.api.middlewares import errors as ra_errors


class ErrorsHandlerMiddleware(ra_errors.ErrorsHandlerMiddleware):
    """Extend RESTAlchemy ErrorsHandlerMiddleware with bazooka/Zulip errors.

    All core RESTAlchemy error handling is delegated to the base class.
    This subclass only adds mapping for bazooka HTTP exceptions.
    """

    unauthorized_exc = (bazooka_exc.UnauthorizedError,)
    forbidden_exc = (bazooka_exc.ForbiddenError,)
    external_http_exc = (bazooka_exc.BaseHTTPException,)

    def _construct_error_response(self, req: Any, exc: Exception) -> Any:
        # Auth backend (Zulip) / bazooka HTTP errors first
        if isinstance(exc, self.unauthorized_exc):
            return req.ResponseClass(
                status=http_client.UNAUTHORIZED,
                json=ra_errors.exception2dict(exc),
            )
        if isinstance(exc, self.forbidden_exc):
            return req.ResponseClass(
                status=http_client.FORBIDDEN,
                json=ra_errors.exception2dict(exc),
            )
        if isinstance(exc, self.external_http_exc):
            # Any other HTTP error from Zulip d 400
            return req.ResponseClass(
                status=http_client.BAD_REQUEST,
                json=ra_errors.exception2dict(exc),
            )

        # Delegate all other exceptions to the standard RESTAlchemy handler
        return super()._construct_error_response(req, exc)
