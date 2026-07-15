import re
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any


MARKDOWN_LINK_RE = re.compile(r"(?P<bang>!?)\[(?P<name>[^\]]*)\]\((?P<url>[^)\s]+)\)")
USER_MENTION_RE = re.compile(
    r"(?P<prefix>@_?)\*\*(?P<name>[^*|]+?)(?:\|(?P<id>\d+))?\*\*"
)


def urn(kind: str, value: str | uuid.UUID) -> str:
    return f"urn:{kind}:{value}"


def external_url_urn(url: str) -> str:
    return urn("url", url)


def normalize_links(
    content: str,
    server_url: str,
    internal_file_resolver: Callable[[str, str], str] | None = None,
) -> str:
    server = urllib.parse.urlsplit(server_url)

    def replace(match: re.Match) -> str:
        url = match.group("url")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme and parsed.netloc != server.netloc:
            url = external_url_urn(url)
        elif parsed.path.startswith("/user_uploads/") and internal_file_resolver:
            absolute_url = urllib.parse.urljoin(server_url.rstrip("/") + "/", url)
            url = internal_file_resolver(absolute_url, match.group("name"))
        return f"{match.group('bang')}[{match.group('name')}]({url})"

    return MARKDOWN_LINK_RE.sub(replace, content)


def normalize_message(
    message: dict[str, Any],
    server_url: str,
    internal_file_resolver: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    stream_id = message.get("stream_id")
    is_direct = message.get("type") in ("private", "direct") or stream_id is None
    topic = "private" if is_direct else message.get("subject") or ""
    recipients = message.get("display_recipient") or []
    recipient_ids = sorted(
        {
            int(recipient["id"])
            for recipient in recipients
            if isinstance(recipient, dict) and recipient.get("id") is not None
        },
    )
    recipient_id = stream_id or message.get("recipient_id")
    return {
        "external_message_id": str(message["id"]),
        "author_external_id": str(message["sender_id"]),
        "author_name": message.get("sender_full_name", ""),
        "author_email": message.get("sender_email"),
        "stream_external_id": str(recipient_id),
        "topic_external_id": f"{recipient_id}:{topic}",
        "is_direct": is_direct,
        "recipients": recipients,
        "recipient_ids": recipient_ids,
        "payload": {
            "kind": "markdown",
            "content": normalize_links(
                message.get("content", ""),
                server_url,
                internal_file_resolver,
            ),
        },
        "created_at": message.get("timestamp"),
        "flags": list(message.get("flags", [])),
    }


def rewrite_file_urns(
    content: str,
    file_resolver: Callable[[str, str], str],
) -> str:
    def replace(match: re.Match) -> str:
        url = match.group("url")
        if not url.startswith("urn:file:"):
            return match.group(0)
        resolved = file_resolver(url, match.group("name"))
        return f"{match.group('bang')}[{match.group('name')}]({resolved})"

    return MARKDOWN_LINK_RE.sub(replace, content)
