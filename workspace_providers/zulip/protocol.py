import json
import pathlib
import urllib.parse
from typing import Any

import requests


class ZulipApiError(RuntimeError):
    pass


class ZulipClient:
    def __init__(self, settings: dict[str, Any], timeout: float = 10.0):
        credentials = settings["credentials"]
        self.base_url = settings["server_url"].rstrip("/") + "/api/v1"
        self.auth = (credentials["login"], credentials["token"])
        self.timeout = timeout
        self.session = requests.Session()

    def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        response = self.session.request(
            method,
            self.base_url + "/" + path.lstrip("/"),
            auth=self.auth,
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("result") == "error":
            raise ZulipApiError(payload.get("msg", "Zulip API error"))
        return payload

    def current_user(self) -> dict[str, Any]:
        return self.request("GET", "/users/me")

    def users(self) -> list[dict[str, Any]]:
        return self.request("GET", "/users").get("members", [])

    def streams(self) -> list[dict[str, Any]]:
        return self.request("GET", "/users/me/subscriptions").get("subscriptions", [])

    def messages(
        self,
        anchor: str | int = "newest",
        limit: int = 100,
        before: int | None = None,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        if before is None:
            before = limit if anchor == "newest" else 0
        if after is None:
            after = 0 if anchor == "newest" else limit
        payload = self.request(
            "GET",
            "/messages",
            params={
                "anchor": anchor,
                "num_before": before,
                "num_after": after,
                "narrow": "[]",
                "apply_markdown": "false",
            },
        )
        return payload.get("messages", [])

    def message(self, message_id: int) -> dict[str, Any]:
        return self.request("GET", f"/messages/{message_id}")["message"]

    def download_file(self, url: str) -> tuple[bytes, str, str]:
        response = self.session.get(
            url,
            auth=self.auth,
            timeout=self.timeout,
        )
        response.raise_for_status()
        name = pathlib.PurePosixPath(
            urllib.parse.urlsplit(url).path,
        ).name
        return (
            response.content,
            response.headers.get("Content-Type", "application/octet-stream"),
            name or "attachment",
        )

    def upload_file(self, name: str, content_type: str, data: bytes) -> str:
        result = self.request(
            "POST",
            "/user_uploads",
            files={"file": (name, data, content_type)},
        )
        return result["uri"]

    def register_queue(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/register",
            data={
                "event_types": json.dumps(
                    [
                        "message",
                        "update_message",
                        "delete_message",
                        "reaction",
                        "update_message_flags",
                        "subscription",
                        "stream",
                        "realm_user",
                    ]
                ),
                "all_public_streams": "true",
            },
        )

    def events(self, queue_id: str, last_event_id: int) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            "/events",
            params={
                "queue_id": queue_id,
                "last_event_id": last_event_id,
                "dont_block": "true",
            },
        ).get("events", [])

    def send_message(self, payload: dict[str, Any]) -> int:
        result = self.request("POST", "/messages", data=payload)
        return int(result["id"])

    def update_message(self, message_id: int, content: str) -> None:
        self.request("PATCH", f"/messages/{message_id}", data={"content": content})

    def delete_message(self, message_id: int) -> None:
        self.request("DELETE", f"/messages/{message_id}")

    def update_stream(self, stream_id: int, name: str, description: str) -> None:
        self.request(
            "PATCH",
            f"/streams/{stream_id}",
            data={"new_name": name, "description": description},
        )

    def delete_stream(self, stream_id: int) -> None:
        self.request("DELETE", f"/streams/{stream_id}")

    def update_topic(self, stream_id: int, old_name: str, new_name: str) -> None:
        self.request(
            "PATCH",
            f"/streams/{stream_id}/topics",
            data={"topic": old_name, "new_topic_name": new_name},
        )

    def delete_topic(self, stream_id: int, topic_name: str) -> None:
        self.request(
            "POST",
            f"/streams/{stream_id}/delete_topic",
            data={"topic_name": topic_name},
        )

    def add_reaction(self, message_id: int, emoji_name: str) -> None:
        self.request(
            "POST",
            f"/messages/{message_id}/reactions",
            data={"emoji_name": emoji_name},
        )

    def remove_reaction(self, message_id: int, emoji_name: str) -> None:
        self.request(
            "DELETE",
            f"/messages/{message_id}/reactions",
            data={"emoji_name": emoji_name},
        )
