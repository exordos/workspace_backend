# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import json
import typing

import requests

from workspace.messenger_api.dm import external_models


class ZulipApiError(Exception):
    pass


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
            raise ZulipApiError("Zulip API request failed")
        return result

    def _get(self, path: str, params: dict[str, typing.Any] | None = None) -> dict:
        return self._request("GET", path, params=params)

    def check_connection(self) -> dict:
        return self._get("/users/me")

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
