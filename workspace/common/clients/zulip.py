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

import base64
from typing import Any, Dict, Optional

from bazooka import common
from bazooka import client as bz_client


class ZulipClient(common.RESTClientMixIn):
    """Client for interacting with Zulip API.

    Currently supports fetching information about the current user
    via the `json/users/me` endpoint.
    """

    ME_PATH_AUTH = "api/v1/users/me"
    ME_PATH_COOKIE = "json/users/me"

    def __init__(self, endpoint: str, timeout: int = 5):
        super().__init__()
        self._client = bz_client.Client(default_timeout=timeout)
        self._endpoint = endpoint

    def get_current_user(self, headers: Dict[str, str]) -> Dict[str, Any]:
        """Fetch raw information about the current user.

        This method directly returns the JSON decoded response from Zulip.

        :param headers: HTTP headers to be passed to Zulip, including
                        authentication and cookies.
        :return: Parsed JSON response as a dictionary.
        """
        url = self._build_resource_uri([self.ME_PATH_COOKIE])
        if "Authorization" in headers:
            url = self._build_resource_uri([self.ME_PATH_AUTH])
        response = self._client.get(url, headers=headers)
        return response.json()

    def get_current_user_with_api_key(
        self,
        login: str,
        token: str,
    ) -> Dict[str, Any]:
        raw_credentials = f"{login}:{token}".encode("utf-8")
        credentials = base64.b64encode(raw_credentials).decode("ascii")
        return self.get_current_user(
            headers={"Authorization": f"Basic {credentials}"},
        )

    def get_current_user_id(self, headers: Dict[str, str]) -> Optional[int]:
        """Extract current user's numeric ID from Zulip response.

        Expected response format (simplified)::

            {
                "result": "success",
                "user_id": 42,
                ...
            }

        :param headers: HTTP headers used for authentication.
        :return: user_id as int.
        """
        data = self.get_current_user(headers=headers)
        return data["user_id"]
