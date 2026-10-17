# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

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

    def _get(self, path: str, params: dict[str, typing.Any] | None = None) -> dict:
        response = self._session.get(
            self._base_url + path,
            params=params,
            timeout=(5, 30),
        )
        response.raise_for_status()
        result = response.json()
        if result["result"] != "success":
            raise ZulipApiError("Zulip API request failed")
        return result

    def check_connection(self) -> None:
        self._get("/users/me")

    def get_channels(self) -> list[dict]:
        return self._get("/streams", params={"exclude_archived": "false"})["streams"]

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
