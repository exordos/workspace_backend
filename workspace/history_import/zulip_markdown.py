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

"""Pure Zulip Markdown conversion, vendored from workspace_zulip_bridge.

Keep conversion parity fixtures synchronized with the bridge. Resolvers supplied
by the importer contain prepared dictionaries only: no SQL, credentials or I/O.
"""

import re
import typing
import urllib.parse

from workspace.history_import import markdown_conversion

MENTION_RE = re.compile(
    r"@_?\*\*(?:"
    r"(?P<name_with_id>[^*|]+)\|(?P<user_id>[0-9]+)"
    r"|\|(?P<user_id_only>[0-9]+)"
    r"|(?P<name_only>[^*]+)"
    r")\*\*"
)


REPLY_FRAGMENT_RE = re.compile(r"^narrow/(?:.*/)?near/(?P<id>[0-9]+)$")


ZULIP_NATIVE_LINK_RE = re.compile(r"#\*\*(?P<reference>[^*]+)\*\*")


ANGLE_URL_RE = re.compile(r"<(?P<url>https?://[^>\s]+)>")


BARE_URL_RE = re.compile(r"(?<!urn:url:)(?P<url>https?://[^\s<]+|www\.[^\s<]+)")


SCHEMELESS_WEB_TARGET_RE = re.compile(
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::[0-9]+)?(?:[/?#].*)?"
)


ZULIP_EMPTY_TOPIC_FALLBACK_NAME = "general chat"


WORKSPACE_MARKDOWN_MAX_LENGTH = 40_000


WORKSPACE_MARKDOWN_TRUNCATION_MARKER = "\n\n[Message truncated]"


FileResolver = typing.Callable[[str, str], str | None]


UNAVAILABLE_FILE_MARKER = "File unavailable"


class ZulipLinkResolver:
    def __init__(
        self,
        store: typing.Any,
        account_uuid: str,
        owner_uuid: str,
    ):
        self.store = store
        self.account_uuid = account_uuid
        self.owner_uuid = owner_uuid
        self._channels_by_name: dict[str, dict[str, object] | None] = {}
        self._owner_provider_id_loaded = False
        self._owner_provider_id: int | None = None

    @staticmethod
    def _urn(entity_kind: str, mapping: dict[str, object] | None) -> str | None:
        if mapping is None:
            return None
        urn_kind = {
            "identity": "user",
            "message": "message",
            "stream": "stream",
            "topic": "topic",
        }.get(entity_kind)
        if urn_kind is None:
            return None
        return f"urn:{urn_kind}:{mapping['workspace_uuid']}"

    def provider_urn(self, entity_kind: str, provider_id: str) -> str | None:
        return self._urn(
            entity_kind,
            self.store.provider_mapping(self.account_uuid, entity_kind, provider_id),
        )

    def channel_mapping(self, channel_name: str) -> dict[str, object] | None:
        cache_key = channel_name.casefold()
        if cache_key in self._channels_by_name:
            return self._channels_by_name[cache_key]
        mapping = self.store.provider_mapping_by_name(
            self.account_uuid, "stream", channel_name
        )
        if mapping is None:
            self._channels_by_name[cache_key] = None
            return None
        metadata = mapping.get("metadata")
        if (
            not isinstance(metadata, dict)
            or metadata.get("chat_type") != "channel"
            or not str(mapping.get("provider_id", "")).startswith("channel:")
        ):
            self._channels_by_name[cache_key] = None
            return None
        self._channels_by_name[cache_key] = mapping
        return mapping

    def channel_urn(self, channel_name: str) -> str | None:
        return self._urn("stream", self.channel_mapping(channel_name))

    def topic_urn(self, channel_name: str, topic_name: str) -> str | None:
        channel = self.channel_mapping(channel_name)
        if channel is None:
            return None
        provider_id = str(channel["provider_id"]).removeprefix("channel:")
        return self.provider_urn("topic", f"{provider_id}:{topic_name}")

    def direct_stream_urn(self, provider_user_ids: list[int]) -> str | None:
        ids = {int(value) for value in provider_user_ids}
        if not self._owner_provider_id_loaded:
            owner = self.store.workspace_mapping(
                self.account_uuid, "identity", self.owner_uuid
            )
            try:
                self._owner_provider_id = (
                    None if owner is None else int(str(owner["provider_id"]))
                )
            except (KeyError, TypeError, ValueError):
                self._owner_provider_id = None
            self._owner_provider_id_loaded = True
        if self._owner_provider_id is None:
            return None
        ids.add(self._owner_provider_id)
        if not ids:
            return None
        chat_type = "direct" if len(ids) == 2 else "group_direct"
        provider_id = f"{chat_type}:{','.join(str(value) for value in sorted(ids))}"
        return self.provider_urn("stream", provider_id)


def provider_message_mapping(
    store: typing.Any,
    account_uuid: str,
    provider_message_id: str,
) -> dict[str, object] | None:
    pending_lookup = getattr(store, "provider_message_mapping", None)
    if callable(pending_lookup):
        return pending_lookup(account_uuid, provider_message_id)
    return store.provider_mapping(account_uuid, "message", provider_message_id)


def provider_message_reference_mapping(
    store: typing.Any,
    account_uuid: str,
    provider_message_id: str,
) -> dict[str, object] | None:
    """Resolve a quote target without requiring its local projection to be ready."""
    reference_lookup = getattr(store, "provider_message_reference_mapping", None)
    if callable(reference_lookup):
        return reference_lookup(account_uuid, provider_message_id)
    return provider_message_mapping(store, account_uuid, provider_message_id)


def convert_markdown(
    content: str,
    mention_uuids: dict[str, str],
    original_url: str,
    file_resolver: FileResolver | None = None,
    link_resolver: ZulipLinkResolver | None = None,
) -> tuple[str, bool]:
    """Convert raw Zulip Markdown without leaking provider-only file URLs."""
    converted, lossy = _convert_zulip_links(
        content,
        original_url,
        link_resolver,
        mention_uuids=mention_uuids,
        file_resolver=file_resolver,
    )
    if lossy and original_url and original_url not in converted:
        converted = f"{converted}\n\n[Open original](urn:url:{original_url})"
    if len(converted) > WORKSPACE_MARKDOWN_MAX_LENGTH:
        marker = WORKSPACE_MARKDOWN_TRUNCATION_MARKER
        if _provider_site(original_url):
            linked_marker = (
                f"\n\n[Message truncated; open original](urn:url:{original_url})"
            )
            if len(linked_marker) < WORKSPACE_MARKDOWN_MAX_LENGTH:
                marker = linked_marker
        converted = converted[: WORKSPACE_MARKDOWN_MAX_LENGTH - len(marker)] + marker
        lossy = True
    return converted, lossy


def _provider_site(original_url: str) -> str:
    parsed = urllib.parse.urlsplit(original_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _safe_urlsplit(value: str) -> urllib.parse.SplitResult | None:
    try:
        return urllib.parse.urlsplit(value)
    except ValueError:
        return None


def _decode_hash_component(value: str) -> str:
    return urllib.parse.unquote(value.replace(".", "%"))


def _channel_id_from_slug(slug: str) -> str | None:
    candidate = slug.split("-", 1)[0]
    return candidate if candidate.isdigit() else None


def _dm_user_ids_from_slug(slug: str) -> list[int] | None:
    candidate = slug.split("-", 1)[0]
    if not candidate:
        return None
    parts = candidate.split(",")
    if not all(
        part.isascii()
        and part.isdecimal()
        and len(part) <= 16
        and 0 < int(part) <= 2**53 - 1
        for part in parts
    ):
        return None
    return [int(part) for part in parts]


def _same_provider_url(target: str, provider_site: str) -> bool:
    if target.startswith("#") or (
        target.startswith("/") and not target.startswith("//")
    ):
        return True
    parsed = _safe_urlsplit(target)
    if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if not provider_site:
        return False
    provider = urllib.parse.urlsplit(provider_site)
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
    ) == (
        provider.scheme.lower(),
        provider.netloc.lower(),
    )


def _zulip_url_urn(
    target: str,
    provider_site: str,
    resolver: ZulipLinkResolver | None,
) -> str | None:
    if resolver is None or not _same_provider_url(target, provider_site):
        return None
    parsed = _safe_urlsplit(target)
    if parsed is None:
        return None
    fragment = parsed.fragment
    if fragment.startswith("user/"):
        provider_user_id = fragment.removeprefix("user/").split("/", 1)[0]
        if provider_user_id.isdigit():
            return resolver.provider_urn("identity", provider_user_id)
        return None
    if not fragment.startswith("narrow/"):
        return None

    parts = fragment.split("/")
    terms: list[tuple[str, str]] = []
    for index in range(1, len(parts), 2):
        operator = _decode_hash_component(parts[index]).lower().removeprefix("-")
        operator = {
            "stream": "channel",
            "pm": "dm",
            "pm-with": "dm",
        }.get(operator, operator)
        operand = parts[index + 1] if index + 1 < len(parts) else ""
        terms.append((operator, operand))

    near = next(
        (
            operand
            for operator, operand in terms
            if operator == "near" and operand.isdigit()
        ),
        None,
    )
    if near is not None:
        return resolver.provider_urn("message", near)

    channel_operand = next(
        (operand for operator, operand in terms if operator == "channel"),
        None,
    )
    if channel_operand is not None:
        channel_id = _channel_id_from_slug(channel_operand)
        channel_mapping = (
            resolver.store.provider_mapping(
                resolver.account_uuid, "stream", f"channel:{channel_id}"
            )
            if channel_id is not None
            else None
        )
        if channel_mapping is None and channel_id is None:
            legacy_name = _decode_hash_component(channel_operand)
            channel_mapping = resolver.channel_mapping(legacy_name)
            if channel_mapping is None and "-" in legacy_name:
                channel_mapping = resolver.channel_mapping(
                    legacy_name.replace("-", " ")
                )
        if channel_mapping is None:
            return None
        topic_operand = next(
            (operand for operator, operand in terms if operator == "topic"),
            None,
        )
        if topic_operand is None:
            return resolver._urn("stream", channel_mapping)
        provider_channel_id = str(channel_mapping["provider_id"]).removeprefix(
            "channel:"
        )
        return resolver.provider_urn(
            "topic",
            f"{provider_channel_id}:{_decode_hash_component(topic_operand)}",
        )

    dm_operand = next(
        (operand for operator, operand in terms if operator == "dm"),
        None,
    )
    if dm_operand is not None:
        provider_user_ids = _dm_user_ids_from_slug(dm_operand)
        if provider_user_ids is not None:
            return resolver.direct_stream_urn(provider_user_ids)
    return None


def _absolute_provider_url(target: str, provider_site: str) -> str | None:
    if target.startswith(("#", "/")):
        if not provider_site:
            return None
        return urllib.parse.urljoin(provider_site.rstrip("/") + "/", target)
    parsed = _safe_urlsplit(target)
    if parsed is not None and parsed.scheme in {"http", "https"} and parsed.netloc:
        return target
    if SCHEMELESS_WEB_TARGET_RE.fullmatch(target):
        return f"https://{target}"
    return None


def _workspace_link_target(
    target: str,
    provider_site: str,
    resolver: ZulipLinkResolver | None,
) -> str:
    if target.startswith("urn:"):
        return target
    entity_urn = _zulip_url_urn(target, provider_site, resolver)
    if entity_urn is not None:
        return entity_urn
    absolute_url = _absolute_provider_url(target, provider_site)
    if absolute_url is not None:
        return f"urn:url:{absolute_url}"
    return target


def _native_zulip_link(
    match: re.Match[str],
    resolver: ZulipLinkResolver | None,
) -> tuple[str, bool]:
    reference = match.group("reference")
    if resolver is None:
        return match.group(0), True
    if ">" not in reference:
        urn = resolver.channel_urn(reference)
        if urn is None:
            return match.group(0), True
        return f"[#{reference}]({urn})", False

    channel_name, topic_reference = reference.split(">", 1)
    message_match = re.fullmatch(
        r"(?P<topic>.*)@(?P<message_id>[0-9]+)", topic_reference
    )
    if message_match is not None:
        urn = resolver.provider_urn("message", message_match.group("message_id"))
        if urn is None:
            return match.group(0), True
        topic_name = message_match.group("topic")
        return f"[#{channel_name} > {topic_name} @ 💬]({urn})", False

    urn = resolver.topic_urn(channel_name, topic_reference)
    if urn is None:
        return match.group(0), True
    return f"[#{channel_name} > {topic_reference}]({urn})", False


def _trim_bare_url(value: str) -> tuple[str, str]:
    url = value
    suffix = ""
    while url and url[-1] in ".,;:!?":
        suffix = url[-1] + suffix
        url = url[:-1]
    while url.endswith(")") and url.count("(") < url.count(")"):
        suffix = ")" + suffix
        url = url[:-1]
    return url, suffix


def _source_link_destination(link: markdown_conversion.MarkdownLink) -> str:
    if link.reference:
        return link.destination
    if not link.raw.startswith(link.destination_prefix):
        return link.destination
    if link.destination_suffix and not link.raw.endswith(link.destination_suffix):
        return link.destination
    destination_end = len(link.raw) - len(link.destination_suffix)
    if destination_end < len(link.destination_prefix):
        return link.destination
    return link.raw[len(link.destination_prefix) : destination_end]


def _reply_provider_id(link: markdown_conversion.MarkdownLink) -> str | None:
    if _safe_urlsplit(_source_link_destination(link)) is None:
        return None
    target = _safe_urlsplit(link.destination)
    if target is None:
        return None
    if target.scheme and target.scheme.casefold() not in {"http", "https"}:
        return None
    match = REPLY_FRAGMENT_RE.fullmatch(target.fragment)
    return match.group("id") if match is not None else None


def _semantic_reply_provider_id(content: str) -> str | None:
    for link in markdown_conversion.semantic_quote_links(content):
        provider_id = _reply_provider_id(link)
        if provider_id is not None:
            return provider_id
    return None


def _canonicalize_semantic_quotes(
    content: str,
    store: typing.Any,
    account_uuid: str,
) -> str:
    def canonical_quote(
        link: markdown_conversion.MarkdownLink,
        _quoted_content: str,
    ) -> str | None:
        provider_id = _reply_provider_id(link)
        if provider_id is None:
            return None
        message = provider_message_reference_mapping(store, account_uuid, provider_id)
        if message is None:
            return None
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        author_uuid = metadata.get("author_uuid")
        author = (
            store.workspace_mapping(account_uuid, "identity", str(author_uuid))
            if author_uuid is not None
            else None
        )
        author_metadata = author.get("metadata") if author is not None else None
        author_metadata = author_metadata if isinstance(author_metadata, dict) else {}
        display_name = str(author_metadata.get("display_name", "Quoted message"))
        label = display_name.replace("\\", "\\\\").replace("]", "\\]")
        return f"[{label}](urn:quote:{message['workspace_uuid']})"

    return markdown_conversion.transform_semantic_quotes(content, canonical_quote)


def _convert_zulip_links(
    content: str,
    original_url: str,
    resolver: ZulipLinkResolver | None,
    *,
    mention_uuids: dict[str, str] | None = None,
    file_resolver: FileResolver | None = None,
) -> tuple[str, bool]:
    provider_site = _provider_site(original_url)
    lossy = False
    mention_uuids = mention_uuids or {}

    def transform_text(segment: str) -> str:
        nonlocal lossy
        converted: list[str] = []
        cursor = 0
        patterns = (
            ("mention", MENTION_RE),
            ("native", ZULIP_NATIVE_LINK_RE),
            ("angle", ANGLE_URL_RE),
            ("bare", BARE_URL_RE),
        )
        while cursor < len(segment):
            candidates: list[tuple[int, int, str, re.Match[str] | None]] = []
            for priority, (kind, pattern) in enumerate(patterns):
                match = pattern.search(segment, cursor)
                if match is not None:
                    candidates.append((match.start(), priority, kind, match))
            upload_index = segment.find("/user_uploads/", cursor)
            if upload_index >= 0:
                candidates.append((upload_index, len(patterns), "upload", None))
            if not candidates:
                converted.append(segment[cursor:])
                break

            start, _priority, kind, match = min(candidates)
            converted.append(segment[cursor:start])
            if kind == "upload":
                lossy = True
                converted.append(original_url + "#file-")
                cursor = start + len("/user_uploads/")
                continue
            if match is None:
                raise AssertionError("text token match is required")
            cursor = match.end()

            if kind == "mention":
                provider_user_id = match.group("user_id") or match.group("user_id_only")
                name = (
                    match.group("name_with_id")
                    or match.group("name_only")
                    or provider_user_id
                    or "User"
                )
                user_uuid = (
                    mention_uuids.get(f"id:{provider_user_id}")
                    if provider_user_id is not None
                    else None
                ) or mention_uuids.get(name)
                if user_uuid is None:
                    lossy = True
                    converted.append(f"@{name}")
                else:
                    converted.append(f"[{name}](urn:user:{user_uuid})")
                continue
            if kind == "native":
                replacement, replacement_lossy = _native_zulip_link(match, resolver)
                lossy = lossy or replacement_lossy
                converted.append(replacement)
                continue
            if kind == "angle":
                url = match.group("url")
                target = _workspace_link_target(url, provider_site, resolver)
                converted.append(
                    match.group(0) if target == url else f"[{url}]({target})"
                )
                continue
            if kind == "bare":
                raw_url, suffix = _trim_bare_url(match.group("url"))
                target = (
                    raw_url
                    if raw_url.startswith(("http://", "https://"))
                    else f"https://{raw_url}"
                )
                workspace_target = _workspace_link_target(
                    target, provider_site, resolver
                )
                if workspace_target == target:
                    converted.append(raw_url + suffix)
                else:
                    converted.append(f"[{raw_url}]({workspace_target}){suffix}")
                continue
            raise AssertionError(f"unknown text token kind: {kind}")
        return "".join(converted)

    def transform_link(link: markdown_conversion.MarkdownLink) -> str:
        nonlocal lossy
        if link.destination.startswith("/user_uploads/"):
            if file_resolver is None:
                lossy = True
                return link.with_destination(original_url)
            destination = file_resolver(link.destination, link.label)
            if destination is None:
                lossy = True
                label = link.label.strip() or "attachment"
                return f"**{UNAVAILABLE_FILE_MARKER}:** {label}"
            return link.with_destination(destination)
        if _safe_urlsplit(_source_link_destination(link)) is None:
            return link.raw
        target = _workspace_link_target(
            link.destination,
            provider_site,
            resolver,
        )
        return link.raw if target == link.destination else link.with_destination(target)

    return (
        markdown_conversion.transform_markdown(
            content,
            text_transform=transform_text,
            link_transform=transform_link,
            convert_semantic_quotes=True,
        ),
        lossy,
    )
