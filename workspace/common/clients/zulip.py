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

import io
import json
import typing
import urllib.parse

from bazooka import common
from bazooka import client as bz_client
import requests

try:
    import zulip
except ImportError:
    zulip = None

MESSAGE_EVENT_TYPES = [
    "message",
    "reaction",
    "update_message",
    "delete_message",
    "update_message_flags",
    "stream",
    "subscription",
]
MESSAGE_EVENT_QUEUE_SUBSCRIPTION_VERSION = 5
MESSAGE_EVENT_REGISTER_TIMEOUT = 30
MESSAGE_EVENT_LONGPOLL_TIMEOUT = 10
MESSAGE_FETCH_TIMEOUT = 30
USER_FETCH_TIMEOUT = 30
STREAM_FETCH_TIMEOUT = 30
STREAM_MEMBERS_FETCH_TIMEOUT = 30
INVALID_MESSAGE_CODE = "BAD_REQUEST"
INVALID_MESSAGE_MSG = "Invalid message(s)"


class ZulipClient(common.RESTClientMixIn):
    """Client for interacting with Zulip API.

    Currently supports fetching information about the current user
    via the official Zulip Python SDK for API key authentication.
    """

    ME_PATH_AUTH = "api/v1/users/me"
    ME_PATH_COOKIE = "json/users/me"

    def __init__(self, endpoint: str, timeout: int = 5, client_cls=None):
        super().__init__()
        self._timeout = timeout
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

    def _build_api_url(self, path: str):
        return urllib.parse.urljoin(
            f"{self._endpoint.rstrip('/')}/",
            f"api/v1/{path.lstrip('/')}",
        )

    def _format_api_params(
        self,
        params: typing.Optional[typing.Dict[str, typing.Any]],
    ):
        if params is None:
            return None
        formatted = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (bool, list, dict)):
                value = json.dumps(value)
            formatted[key] = value
        return formatted

    def _get_api_json_with_api_key(
        self,
        login: str,
        token: str,
        path: str,
        params: typing.Optional[typing.Dict[str, typing.Any]] = None,
        timeout: typing.Optional[int] = None,
    ):
        if timeout is None:
            timeout = self._timeout
        response = requests.get(
            self._build_api_url(path),
            auth=(login, token),
            params=self._format_api_params(params),
            timeout=timeout,
        )
        return response.json()

    def _post_api_json_with_api_key(
        self,
        login: str,
        token: str,
        path: str,
        data: typing.Optional[typing.Dict[str, typing.Any]] = None,
        timeout: typing.Optional[int] = None,
    ):
        if timeout is None:
            timeout = self._timeout
        response = requests.post(
            self._build_api_url(path),
            auth=(login, token),
            data=self._format_api_params(data),
            timeout=timeout,
        )
        return response.json()

    def get_current_user(
        self,
        headers: typing.Dict[str, str],
    ) -> typing.Dict[str, typing.Any]:
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
    ) -> typing.Dict[str, typing.Any]:
        return self._get_api_json_with_api_key(
            login=login,
            token=token,
            path="users/me",
            timeout=USER_FETCH_TIMEOUT,
        )

    def get_users_with_api_key(
        self,
        login: str,
        token: str,
    ):
        data = self._get_api_json_with_api_key(
            login=login,
            token=token,
            path="users",
            timeout=USER_FETCH_TIMEOUT,
        )
        return data["members"]

    def get_messages_with_api_key(
        self,
        login: str,
        token: str,
        message_filters: typing.Dict[str, typing.Any],
    ):
        data = self._get_api_json_with_api_key(
            login=login,
            token=token,
            path="messages",
            params=message_filters,
            timeout=MESSAGE_FETCH_TIMEOUT,
        )
        return data["messages"]

    def get_message_with_api_key(
        self,
        login: str,
        token: str,
        message_id: int,
    ):
        data = self._get_api_json_with_api_key(
            login=login,
            token=token,
            path=f"messages/{message_id}",
            params={
                "apply_markdown": False,
                "allow_empty_topic_name": True,
            },
            timeout=MESSAGE_FETCH_TIMEOUT,
        )
        if (
            data.get("code") == INVALID_MESSAGE_CODE and
            data.get("msg") == INVALID_MESSAGE_MSG
        ):
            return None
        return data["message"]

    def send_message_with_api_key(
        self,
        login: str,
        token: str,
        stream_name: str,
        topic_name: str,
        content: str,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.send_message({
            "type": "stream",
            "to": stream_name,
            "topic": topic_name,
            "content": content,
        })

    def send_private_message_with_api_key(
        self,
        login: str,
        token: str,
        recipient_ids: list[int],
        content: str,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.send_message({
            "type": "direct",
            "to": recipient_ids,
            "content": content,
        })

    def update_message_with_api_key(
        self,
        login: str,
        token: str,
        message_id: int,
        content: str,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.update_message({
            "message_id": message_id,
            "content": content,
        })

    def delete_message_with_api_key(
        self,
        login: str,
        token: str,
        message_id: int,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.delete_message(message_id)

    def add_reaction_with_api_key(
        self,
        login: str,
        token: str,
        message_id: int,
        emoji_name: str,
        emoji_code: typing.Optional[str] = None,
        reaction_type: typing.Optional[str] = None,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.call_endpoint(
            url=f"messages/{message_id}/reactions",
            method="POST",
            request={
                "emoji_name": emoji_name,
                "emoji_code": emoji_code,
                "reaction_type": reaction_type,
            },
        )

    def remove_reaction_with_api_key(
        self,
        login: str,
        token: str,
        message_id: int,
        emoji_name: str,
        emoji_code: typing.Optional[str] = None,
        reaction_type: typing.Optional[str] = None,
    ):
        client = self._get_sdk_client(login=login, token=token)
        return client.call_endpoint(
            url=f"messages/{message_id}/reactions",
            method="DELETE",
            request={
                "emoji_name": emoji_name,
                "emoji_code": emoji_code,
                "reaction_type": reaction_type,
            },
        )

    def upload_file_with_api_key(
        self,
        login: str,
        token: str,
        file_name: str,
        data: bytes,
    ):
        client = self._get_sdk_client(login=login, token=token)
        file = io.BytesIO(data)
        file.name = file_name
        return client.upload_file(file)

    def download_file_with_api_key(
        self,
        login: str,
        token: str,
        url: str,
    ):
        file_url = urllib.parse.urljoin(
            f"{self._endpoint.rstrip('/')}/",
            url.lstrip("/"),
        )
        response = requests.get(
            file_url,
            auth=(login, token),
            timeout=self._timeout,
        )
        response.raise_for_status()
        return {
            "content": response.content,
            "content_type": response.headers.get("Content-Type"),
        }

    def _register_message_event_queue(self, login, token, event_types):
        return self._post_api_json_with_api_key(
            login=login,
            token=token,
            path="register",
            data={
                "event_types": event_types,
                "apply_markdown": False,
                "client_capabilities": {
                    "archived_channels": True,
                    "notification_settings_null": True,
                    "bulk_message_deletion": True,
                },
            },
            timeout=MESSAGE_EVENT_REGISTER_TIMEOUT,
        )

    def _is_registered_event_queue(self, data):
        return (
            data.get("result") == "success" and
            "queue_id" in data and
            "last_event_id" in data
        )

    def register_message_event_queue_with_api_key(
        self,
        login: str,
        token: str,
    ):
        data = self._register_message_event_queue(
            login=login,
            token=token,
            event_types=MESSAGE_EVENT_TYPES,
        )
        if self._is_registered_event_queue(data):
            return data
        return self._register_message_event_queue(
            login=login,
            token=token,
            event_types=None,
        )

    def get_events_with_api_key(
        self,
        login: str,
        token: str,
        queue_id: str,
        last_event_id: int,
    ):
        return self._get_api_json_with_api_key(
            login=login,
            token=token,
            path="events",
            params={
                "queue_id": queue_id,
                "last_event_id": last_event_id,
            },
            timeout=MESSAGE_EVENT_LONGPOLL_TIMEOUT,
        )

    def get_streams_with_api_key(
        self,
        login: str,
        token: str,
    ):
        data = self._get_api_json_with_api_key(
            login=login,
            token=token,
            path="streams",
            params={
                "include_all": True,
                "exclude_archived": False,
            },
            timeout=STREAM_FETCH_TIMEOUT,
        )
        return data["streams"]

    def get_stream_subscribers_with_api_key(
        self,
        login: str,
        token: str,
        stream_id: int,
    ):
        data = self._get_api_json_with_api_key(
            login=login,
            token=token,
            path=f"streams/{stream_id}/members",
            timeout=STREAM_MEMBERS_FETCH_TIMEOUT,
        )
        return data["subscribers"]

    def get_current_user_id(
        self,
        headers: typing.Dict[str, str],
    ) -> typing.Optional[int]:
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
