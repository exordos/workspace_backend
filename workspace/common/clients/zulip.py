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

from typing import Any, Dict, Optional

from bazooka import common
from bazooka import client as bz_client

try:
    import zulip
except ImportError:
    zulip = None


class ZulipClient(common.RESTClientMixIn):
    """Client for interacting with Zulip API.

    Currently supports fetching information about the current user
    via the official Zulip Python SDK for API key authentication.
    """

    ME_PATH_AUTH = "api/v1/users/me"
    ME_PATH_COOKIE = "json/users/me"

    def __init__(self, endpoint: str, timeout: int = 5, client_cls=None):
        super().__init__()
        self._client = bz_client.Client(default_timeout=timeout)
        self._sdk_client_cls = client_cls
        self._endpoint = endpoint

    def _get_sdk_client_cls(self):
        if self._sdk_client_cls is not None:
            return self._sdk_client_cls
        if zulip is None:
            raise ImportError("The official zulip Python SDK is not installed")
        return zulip.Client

    def _get_sdk_client(self, login: str, token: str):
        client_cls = self._get_sdk_client_cls()
        return client_cls(
            email=login,
            api_key=token,
            site=self._endpoint,
        )

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
        client = self._get_sdk_client(login=login, token=token)
        return client.get_profile()

    def get_users_with_api_key(
        self,
        login: str,
        token: str,
    ):
        client = self._get_sdk_client(login=login, token=token)
        data = client.get_members()
        return data["members"]

    def get_messages_with_api_key(
        self,
        login: str,
        token: str,
        message_filters: Dict[str, Any],
    ):
        client = self._get_sdk_client(login=login, token=token)
        data = client.get_messages(message_filters)
        return data["messages"]

    def get_streams_with_api_key(
        self,
        login: str,
        token: str,
    ):
        client = self._get_sdk_client(login=login, token=token)
        stream_filters = {
            "include_all": True,
            "exclude_archived": False,
        }
        data = client.get_streams(**stream_filters)
        return data["streams"]

    def get_stream_subscribers_with_api_key(
        self,
        login: str,
        token: str,
        stream_id: int,
    ):
        client = self._get_sdk_client(login=login, token=token)
        data = client.call_endpoint(
            url=f"streams/{stream_id}/members",
            method="GET",
        )
        return data["subscribers"]

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
