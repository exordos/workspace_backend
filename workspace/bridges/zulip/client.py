# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import datetime
import json
import typing
import urllib.parse

import aiohttp
import requests

from workspace.messenger_api.dm import external_models


class ZulipApiError(Exception):
    def __init__(self, code: str, message: str = "Zulip API request failed") -> None:
        super().__init__(message)
        self.code = code


class ZulipClient:
    def __init__(self, account: external_models.ExternalAccount):
        settings = account.settings
        self._base_url = settings["server_url"].rstrip("/") + "/api/v1"
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.auth = (settings["email"], settings["api_key"])

    def close(self) -> None:
        self._session.close()

    def _request(self, method: str, path: str, **kwargs: typing.Any) -> dict:
        response = self._session.request(
            method,
            self._base_url + path,
            timeout=(5, 30),
            **kwargs,
        )
        response.raise_for_status()
        result = response.json()
        if result["result"] != "success":
            raise ZulipApiError(result["code"], result["msg"])
        return result

    def _get(self, path: str, params: dict[str, typing.Any] | None = None) -> dict:
        return self._request("GET", path, params=params)

    def check_connection(self) -> dict:
        return self._get("/users/me")

    def send_message(
        self,
        recipient: int | list[int],
        content: str,
        topic: str | None,
        queue_id: str,
        local_id: str,
    ) -> int:
        data = {
            "type": "direct" if topic is None else "channel",
            "to": json.dumps(recipient),
            "content": content,
            "read_by_sender": "true",
            "queue_id": queue_id,
            "local_id": local_id,
        }
        if topic is not None:
            data["topic"] = topic
        return self._request("POST", "/messages", data=data)["id"]

    def edit_message(self, message_id: int, content: str) -> None:
        self._request("PATCH", f"/messages/{message_id}", data={"content": content})

    def upload_file(self, name: str, data: bytes, content_type: str) -> str:
        return self._request(
            "POST", "/user_uploads", files={"filename": (name, data, content_type)}
        )["url"]

    def download_file(self, path: str) -> tuple[bytes, str]:
        temporary = self._get(path)["url"]
        url = urllib.parse.urljoin(
            self._base_url.removesuffix("/api/v1") + "/", temporary
        )
        # Temporary links may point at object storage. Never forward API credentials.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, timeout=(5, 30))
            response.raise_for_status()
            return response.content, response.headers["Content-Type"].split(";", 1)[0]

    def update_message_flags(
        self, message_ids: list[int], flag: str, value: bool
    ) -> dict:
        return self._request(
            "POST",
            "/messages/flags",
            data={
                "messages": json.dumps(message_ids),
                "flag": flag,
                "op": "add" if value else "remove",
            },
        )

    def update_reaction(
        self,
        message_id: int,
        emoji_name: str,
        emoji_code: str,
        reaction_type: str,
        present: bool,
    ) -> dict:
        return self._request(
            "POST" if present else "DELETE",
            f"/messages/{message_id}/reactions",
            data={
                "emoji_name": emoji_name,
                "emoji_code": emoji_code,
                "reaction_type": reaction_type,
            },
        )

    def get_users(self) -> list[dict]:
        # Unlike /users, registration also includes cross-realm system bots.
        data = self._request(
            "POST",
            "/register",
            data={"event_types": "[]", "fetch_event_types": '["realm_user"]'},
        )
        try:
            # Registration separates inactive users and omits their is_active flag.
            return (
                [{**user, "is_active": True} for user in data["realm_users"]]
                + [
                    {**user, "is_active": False}
                    for user in data["realm_non_active_users"]
                ]
                + data["cross_realm_bots"]
            )
        finally:
            self._request("DELETE", "/events", data={"queue_id": data["queue_id"]})

    def get_channels(self) -> list[dict]:
        data = self._request(
            "POST",
            "/register",
            data={
                "event_types": "[]",
                "fetch_event_types": '["subscription"]',
                "client_capabilities": json.dumps(
                    {
                        "notification_settings_null": True,
                        "user_settings_object": True,
                        "archived_channels": True,
                    }
                ),
            },
        )
        try:
            # Workspace modes classify unread messages, independently of popup
            # delivery settings. Muted Zulip channels still notify on mentions.
            return [
                {
                    **subscription,
                    "notification_mode": (
                        "mentions_only" if subscription["is_muted"] else "all_messages"
                    ),
                }
                for subscription in data["subscriptions"]
            ]
        finally:
            self._request("DELETE", "/events", data={"queue_id": data["queue_id"]})

    def get_last_message_id(self) -> int:
        messages = self._get(
            "/messages",
            params={
                "anchor": "newest",
                "num_before": 1,
                "num_after": 0,
                "apply_markdown": "false",
            },
        )["messages"]
        return messages[-1]["id"] if messages else 0

    def get_topic_notifications(self) -> list[dict]:
        data = self._request(
            "POST",
            "/register",
            data={
                "event_types": "[]",
                "fetch_event_types": '["user_topic"]',
                "client_capabilities": json.dumps(
                    {
                        "notification_settings_null": True,
                        "user_settings_object": True,
                        "empty_topic_name": True,
                    }
                ),
            },
        )
        try:
            return data["user_topics"]
        finally:
            self._request("DELETE", "/events", data={"queue_id": data["queue_id"]})

    def get_messages(self, first_message_id: int, last_message_id: int) -> list[dict]:
        messages = self._get(
            "/messages",
            params={
                "anchor": first_message_id,
                "num_before": 0,
                "num_after": last_message_id - first_message_id,
                "include_anchor": "true",
                "apply_markdown": "false",
                "allow_empty_topic_name": "true",
            },
        )["messages"]
        return [
            message
            for message in messages
            if first_message_id <= message["id"] <= last_message_id
        ]

    def get_first_message_id_since(self, since: datetime.datetime) -> int | None:
        messages = self._get(
            "/messages",
            params={
                "anchor": "date",
                "anchor_date": since.isoformat(),
                "num_before": 0,
                "num_after": 0,
                "apply_markdown": "false",
            },
        )["messages"]
        # Date anchors fall back to the newest message if none follow the date.
        if messages and messages[0]["timestamp"] >= since.timestamp():
            return messages[0]["id"]
        return None


class AsyncZulipClient:
    def __init__(self, account: external_models.ExternalAccount) -> None:
        settings = account.settings
        self._base_url = settings["server_url"].rstrip("/") + "/api/v1"
        self._session = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(settings["email"], settings["api_key"]),
            timeout=aiohttp.ClientTimeout(total=30, connect=5),
            trust_env=False,
        )

    async def close(self) -> None:
        await self._session.close()

    async def _request(self, method: str, path: str, **kwargs: typing.Any) -> dict:
        async with self._session.request(
            method, self._base_url + path, **kwargs
        ) as response:
            result = await response.json()
            if result["result"] != "success":
                raise ZulipApiError(result["code"], result["msg"])
            response.raise_for_status()
            return result

    async def register_queue(self) -> dict:
        # Omit event_types to subscribe to every event; fetch only the boundary
        # and realm timeout needed to start history import and long polling.
        return await self._request(
            "POST",
            "/register",
            data={
                "fetch_event_types": '["message", "realm"]',
                "apply_markdown": "false",
                "client_capabilities": json.dumps(
                    {
                        "empty_topic_name": True,
                        "notification_settings_null": True,
                        "user_settings_object": True,
                        "archived_channels": True,
                    }
                ),
            },
        )

    async def get_events(
        self, queue_id: str, last_event_id: int, timeout: int
    ) -> list[dict]:
        result = await self._request(
            "GET",
            "/events",
            params={
                "queue_id": queue_id,
                "last_event_id": last_event_id,
                "dont_block": "false",
            },
            timeout=aiohttp.ClientTimeout(total=timeout, connect=5),
        )
        return result["events"]
