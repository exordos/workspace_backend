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

from bazooka import common
from bazooka import client as bz_client
from bazooka import exceptions as bz_exc
from collections.abc import Callable
from requests import exceptions as request_exc
from restalchemy.common import exceptions as ra_exc
from typing import Any


class IamProfileRequestError(ra_exc.RestAlchemyException):
    code = 502
    message = "IAM profile request failed."

    def __init__(self, code: int = 502) -> None:
        self.code = code
        super().__init__()


class IamClient(common.RESTClientMixIn):
    USERS_PATH = "users"

    def __init__(
        self,
        endpoint: str,
        timeout: int = 5,
        client_cls: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self._client = (client_cls or bz_client.Client)(default_timeout=timeout)
        self._endpoint = endpoint

    def _get_users_url(self) -> str:
        iam_root, clients_separator, _client_path = self._endpoint.partition(
            "/clients/",
        )
        if clients_separator:
            return self._build_collection_uri([self.USERS_PATH], init_uri=iam_root)
        return self._build_collection_uri([self.USERS_PATH])

    def get_users(self, token: str) -> Any:
        response = self._client.get(
            self._get_users_url(),
            headers={"Authorization": f"Bearer {token}"},
        )
        return response.json()

    def _profile_request(
        self, method: str, user_uuid: object, token: str, **kwargs: Any
    ) -> Any:
        url = f"{self._get_users_url()}{user_uuid}"
        try:
            response = getattr(self._client, method)(
                url,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
            return response.json()
        except bz_exc.BaseHTTPException as exc:
            code = exc.code if 400 <= exc.code < 500 else 502
            raise IamProfileRequestError(code) from None
        except (request_exc.RequestException, ValueError):
            raise IamProfileRequestError() from None

    def get_user(self, user_uuid: object, token: str) -> Any:
        return self._profile_request("get", user_uuid, token)

    def update_user(self, user_uuid: object, token: str, values: dict[str, Any]) -> Any:
        return self._profile_request("put", user_uuid, token, json=values)
