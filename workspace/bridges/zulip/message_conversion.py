# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

"""Convert Markdown without fetching identities, attachments, or quoted messages.

Context contains server_url and original_url, plus optional mention_uuids
(id:<Zulip ID> or name:<name>), stream_uuids (name), topic_uuids (channel>topic),
message_uuids (Zulip ID), stream_ids (name -> Zulip ID), files (URL -> Workspace
file URN), linkifier_urls, user_group_ids, and custom_emoji_names.
The optional messages mapping contains
original Zulip messages keyed by their string IDs, for full/partial quotes.
Reverse conversion produces canonical Markdown, not the original spelling.
Unsupported content remains a notice with a link to the original message.
"""

import base64
import collections.abc
import re
import urllib.parse
import uuid

from workspace.messenger_api.dm import message_payloads
from workspace.common import emoji


_INLINE = (
    r"(?P<escape>\\[^\n])"
    r"|(?P<user>@_?\*\*[^*\n]+\*\*)"
    r"|(?P<group>@\*[^*\n]+\*)"
    r"|(?P<channel>\#\*\*[^*\n]+\*\*)"
    r"|(?P<link>!?\[)"
    r"|(?P<url><?https?://[^\s<>`]+>?)"
    r"|(?P<math>\$\$.+?\$\$)"
    r"|(?P<time><time:[^>\n]+>)"
    r"|(?P<emoji>:[\w+-]+:)"
)
_QUOTE_HEADER = re.compile(
    r"@_?\*\*(?P<name>[^|\n]+)\|\d+\*\* "
    r"\[[^\]\n]+\]\((?P<url>[^\n]+)\):\r?\n"
)
_FENCE = re.compile(r" {0,3}(?P<marker>`{3,}|~{3,})(?P<info>[^\n]*)\n")
_REFERENCE_URN = re.compile(
    r"urn:(user|stream|topic|message|quote|file|image|video|forward):"
    r"([\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12})(?:\?(.*))?",
    re.IGNORECASE,
)


def _escape_label(value: str) -> str:
    return re.sub(r"([\\`*_{()\[\]#+.!|>~-])", r"\\\1", value)


def _unescape(value: str) -> str:
    return re.sub(r"\\([\\`*_{()\[\]#+.!|>~-])", r"\1", value)


def _closing(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 1
    position = start + 1
    while position < len(text):
        character = text[position]
        if character == "\\":
            position += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return None


def _link(text: str, start: int) -> tuple[str, str, str, bool, int] | None:
    image = text[start] == "!"
    start += int(image)
    label_end = _closing(text, start, "[", "]")
    if label_end is None or text[label_end + 1 : label_end + 2] != "(":
        return None
    end = _closing(text, label_end + 1, "(", ")")
    if end is None:
        return None
    destination = text[label_end + 2 : end]
    parts = re.fullmatch(r"""(<[^>]*>|\S+?)(\s+(?:".*"|'.*'|\(.*\)))?""", destination)
    if parts is None:
        return None
    return (
        text[start + 1 : label_end],
        parts[1].removeprefix("<").removesuffix(">"),
        parts[2] or "",
        image,
        end + 1,
    )


def _markdown_link(
    label: str, target: str, title: str = "", image: bool = False
) -> str:
    return f"{'!' if image else ''}[{label}]({target}{title})"


def parse_url(target: str, base_url: str = "") -> urllib.parse.SplitResult | None:
    """Parse a message link, leaving malformed destinations as ordinary text."""
    try:
        return urllib.parse.urlsplit(urllib.parse.urljoin(base_url, target))
    except ValueError:
        return None


def _workspace_reference(target: str) -> tuple[str, str, str] | None:
    match = _REFERENCE_URN.fullmatch(target)
    if match is None:
        return None
    return match[1].lower(), match[2].lower(), match[3] or ""


def parse_snapshot(query: str) -> str | None:
    """Decode a forward snapshot, preserving malformed elements as text."""
    try:
        encoded = urllib.parse.parse_qs(query)["snapshot"][0]
        return base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        ).decode()
    except (KeyError, ValueError):
        return None


def _message_links(text: str) -> collections.abc.Iterator[tuple[str, bool]]:
    tokens = re.compile(r"\\[^\n]|!?\[|<?https?://[^\s<>`]+>?|/user_uploads/[^\s<>`]+")
    position = 0
    while match := tokens.search(text, position):
        position = match.end()
        token = match[0]
        if token.startswith("\\"):
            continue
        if token in ("[", "!["):
            link = _link(text, match.start())
            if link is not None:
                _, target, _, image, position = link
                yield target, image
        else:
            yield token.removeprefix("<").removesuffix(">"), False


def workspace_references(text: str) -> collections.abc.Iterator[tuple[str, str, str]]:
    """Yield parsed Workspace links; malformed references remain ordinary text."""
    for target, _image in _message_links(text):
        reference = _workspace_reference(target)
        if reference is not None:
            yield reference


def get_zulip_files(
    message: dict, context: dict, *, include_quotes: bool = False
) -> list[str]:
    """Return this message's file URLs in occurrence order, excluding quotes.

    Context uses the same files mapping as conversion to recognize external
    file URLs. Repeated references are retained, as in the converted payload.
    """
    files = context.get("files", {})
    return [
        target
        for target, image in _message_links(
            message["content"]
            if include_quotes
            else _without_quotes(message["content"])
        )
        if (url := parse_url(target)) is not None
        and (target in files or image or url.path.startswith("/user_uploads/"))
    ]


def workspace_reference_text(text: str) -> str:
    """Include references embedded in forward snapshots when resolving a message."""
    contents = [text]
    for content in contents:
        for kind, _identifier, query in workspace_references(content):
            if kind == "forward":
                snapshot = parse_snapshot(query)
                if snapshot is not None:
                    contents.append(snapshot)
            elif kind == "quote":
                contents.extend(urllib.parse.parse_qs(query).get("text", []))
    return "\n".join(contents)


def get_workspace_files(payload: dict, *, include_quotes: bool = False) -> list[str]:
    """Return this message's file URNs in occurrence order, excluding quotes."""
    text = message_payloads.markdown_content(payload)
    text = workspace_reference_text(text) if include_quotes else _without_quotes(text)
    return [
        target
        for target, _ in _message_links(text)
        if (reference := _workspace_reference(target)) is not None
        and reference[0] in ("file", "image", "video")
    ]


def _fenced(text: str, start: int) -> tuple[str, str, int] | None:
    opening = _FENCE.match(text, start)
    if opening is None:
        return None
    marker = opening["marker"]
    closing = re.search(
        rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\n|$)",
        text[opening.end() :],
        re.MULTILINE,
    )
    if closing is None:
        return None
    body = text[opening.end() : opening.end() + closing.start()].removesuffix("\n")
    return opening["info"].strip(), body, opening.end() + closing.end()


def _without_quotes(text: str) -> str:
    output = []
    position = 0
    while position < len(text):
        header = _QUOTE_HEADER.match(text, position)
        block = _fenced(text, header.end() if header else position)
        if block is not None:
            info, content, end = block
            if info == "spoiler" or info.startswith("spoiler "):
                output.append("\n" + _without_quotes(content) + "\n")
            elif info != "quote":
                output.append(text[position:end])
        else:
            end = text.find("\n", position) + 1 or len(text)
            if not re.match(r" {0,3}>", text[position:end]):
                output.append(text[position:end])
        position = end
    return "".join(output)


def _quote_fence(content: str) -> str:
    longest = max((len(item) for item in re.findall(r"`+", content)), default=2)
    marker = "`" * max(3, longest + 1)
    return f"{marker}quote\n{content}\n{marker}"


def _near_id(url: str) -> str | None:
    parsed = parse_url(url)
    if parsed is None:
        return None
    match = re.search(r"(?:^|/)(?:near|with)/(\d+)(?:/|$)", parsed.fragment)
    return match[1] if match else None


def encode_hash(value: str) -> str:
    return urllib.parse.quote(value, safe="!*'-_").replace(".", ".2E").replace("%", ".")


class _ZulipToWorkspace:
    def __init__(self, message: dict, context: dict) -> None:
        self.message = message
        self.context = context
        self.unsupported = False
        linkifiers = context.get("linkifier_urls", {})
        pattern = _INLINE
        if linkifiers:
            words = "|".join(
                re.escape(word) for word in sorted(linkifiers, key=len, reverse=True)
            )
            pattern += rf"|(?P<linkifier>(?<!\w)(?:{words})(?!\w))"
        self.tokens = re.compile(pattern)

    def notice(self, description: str, *, block: bool = False) -> str:
        self.unsupported = True
        text = f"Not supported in Workspace: {description}"
        return text if block else f"({text})"

    def block_notice(self, description: str, source: str) -> str:
        parts = [self.notice(description, block=True)]
        for url in get_zulip_files({"content": source}, self.context):
            target = self.context["files"][url]
            name = urllib.parse.unquote(
                urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            )
            parts.append(
                _markdown_link(
                    _escape_label(name), target, image=target.startswith("urn:image:")
                )
            )
        return "\n\n".join(parts)

    def message_uuid(self, message_id: str) -> str:
        mapped = self.context.get("message_uuids", {}).get(message_id)
        if mapped is not None:
            return mapped
        if self.context.get("strict_identities"):
            raise ValueError(f"No Workspace message mapping for {message_id}")
        host = urllib.parse.urlsplit(self.context["server_url"]).hostname
        if host is None:
            raise ValueError("server_url must contain a hostname")
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, host)
        return str(uuid.uuid5(namespace, message_id))

    def snapshot(self, content: str) -> str:
        encoded = (
            base64.urlsafe_b64encode(self.inline(content).encode()).decode().rstrip("=")
        )
        identifier = self.context.get("current_message_uuid") or self.message_uuid(
            str(self.message["id"])
        )
        target = f"urn:forward:{identifier}?snapshot={encoded}"
        return _markdown_link("unknown", target)

    def target(self, target: str) -> str | None:
        files = self.context.get("files", {})
        if target in files:
            return files[target]
        url = parse_url(target, self.context["server_url"].rstrip("/") + "/")
        if url is None:
            return None
        absolute = url.geturl()
        server = urllib.parse.urlsplit(self.context["server_url"])
        if (url.scheme, url.netloc) == (server.scheme, server.netloc):
            message_id = _near_id(absolute)
            if message_id is not None:
                mapped = self.context.get("message_uuids", {}).get(message_id)
                if mapped is not None:
                    return f"urn:message:{mapped}"
                return f"urn:url:{absolute}"
            direct = re.search(
                r"(?:^|/)dm/(\d+(?:,\d+)*)(?:-[^/]*)?(?:/|$)", url.fragment
            )
            if direct and self.context.get("strict_identities"):
                users = sorted(
                    {int(value) for value in direct[1].split(",")}
                    | {int(self.context["own_user_id"])}
                )
                identifier = str(
                    uuid.uuid5(
                        uuid.UUID(self.context["zulip_namespace"]),
                        "dm:" + ",".join(map(str, users)),
                    )
                )
                if identifier in self.context["dm_stream_uuids"]:
                    return f"urn:stream:{identifier}"
            match = re.search(
                r"(?:^|/)channel/(\d+)(?:-([^/]+))?(?:/topic/([^/]+))?", url.fragment
            )
            if match:
                channel = next(
                    (
                        name
                        for name, stream_id in self.context.get(
                            "stream_ids", {}
                        ).items()
                        if str(stream_id) == match[1]
                    ),
                    urllib.parse.unquote(
                        re.sub(r"\.([\da-fA-F]{2})", r"%\1", match[2] or "")
                    ),
                )
                if match[3] is not None:
                    topic = urllib.parse.unquote(
                        re.sub(r"\.([\da-fA-F]{2})", r"%\1", match[3])
                    )
                    mapped = self.context.get("topic_uuids", {}).get(
                        f"{channel}>{topic}"
                    )
                    if mapped is not None:
                        return f"urn:topic:{mapped}"
                elif channel in self.context.get("stream_uuids", {}):
                    return f"urn:stream:{self.context['stream_uuids'][channel]}"
        return f"urn:url:{absolute}"

    def inline(self, text: str) -> str:
        output = []
        position = 0
        while match := self.tokens.search(text, position):
            output.append(text[position : match.start()])
            position = match.end()
            token = match[0]
            kind = match.lastgroup
            if kind == "link":
                link = _link(text, match.start())
                if link is None:
                    output.append(token)
                    continue
                label, destination, title, image, position = link
                target = self.target(destination)
                if target is None:
                    output.append(text[match.start() : position])
                    continue
                output.append(
                    _markdown_link(
                        label, target, title, image or target.startswith("urn:image:")
                    )
                )
            elif kind == "url":
                target = token.removeprefix("<").removesuffix(">")
                target = target.rstrip(".,!?;:")
                while target.endswith(")") and target.count(")") > target.count("("):
                    target = target[:-1]
                position = match.start() + len(target) + int(token.startswith("<"))
                if token.startswith("<") and text[position : position + 1] == ">":
                    position += 1
                urn = self.target(target)
                if urn is None:
                    output.append(text[match.start() : position])
                    continue
                label = (
                    urllib.parse.unquote(
                        urllib.parse.urlsplit(target).path.rsplit("/", 1)[-1]
                    )
                    if urn.startswith("urn:image:")
                    else target
                )
                output.append(
                    _markdown_link(label, urn, image=urn.startswith("urn:image:"))
                )
            elif kind == "user":
                silent = token.startswith("@_")
                value = token[4 if silent else 3 : -2]
                name, separator, user_id = value.rpartition("|")
                if not separator:
                    name = value
                if name in ("all", "everyone", "topic"):
                    scope = "topic" if name == "topic" else "channel"
                    output.append(self.notice(f"{scope}-wide mention"))
                    continue
                key = f"id:{user_id}" if separator else f"name:{name}"
                mapped = self.context.get("mention_uuids", {}).get(key)
                if mapped is None:
                    output.append(self.notice(f"unresolved user {name}"))
                elif silent:
                    output.append(self.notice(f"silent mention of {name}"))
                else:
                    output.append(
                        _markdown_link(_escape_label(name), f"urn:user:{mapped}")
                    )
            elif kind == "group":
                name = token[2:-1]
                description = (
                    "mention of user group"
                    if name in self.context.get("user_group_ids", {})
                    else "unresolved user group"
                )
                output.append(self.notice(f"{description} {name}"))
            elif kind == "channel":
                value = token[3:-2]
                topic, separator, message_id = value.rpartition("@")
                if not separator:
                    topic = value
                label = "#" + topic.replace(">", " > ")
                if separator:
                    mapped = self.context.get("message_uuids", {}).get(message_id)
                    target = f"urn:message:{mapped}" if mapped else None
                elif ">" in topic:
                    mapped = self.context.get("topic_uuids", {}).get(topic)
                    target = f"urn:topic:{mapped}" if mapped else None
                else:
                    mapped = self.context.get("stream_uuids", {}).get(topic)
                    target = f"urn:stream:{mapped}" if mapped else None
                output.append(
                    _markdown_link(label, target)
                    if target
                    else self.notice(f"unresolved channel reference {value}")
                )
            elif kind == "math":
                output.append(self.notice("inline LaTeX"))
            elif kind == "time":
                output.append(self.notice(f"global time {token[6:-1]}"))
            elif kind == "emoji" and token[1:-1] in self.context.get(
                "custom_emoji_names", []
            ):
                output.append(self.notice(f"custom emoji {token[1:-1]}"))
            elif kind == "emoji" and self.context.get("normalize_emoji"):
                code = emoji.ZULIP_CODE_BY_NAME.get(token[1:-1])
                output.append(
                    "".join(chr(int(point, 16)) for point in code.split("-"))
                    if code
                    else token
                )
            elif kind == "linkifier":
                output.append(
                    _markdown_link(
                        token, f"urn:url:{self.context['linkifier_urls'][token]}"
                    )
                )
            else:
                output.append(token)
        output.append(text[position:])
        return "".join(output)

    def body(self, text: str) -> str:
        command = re.match(r"^/(me|poll|todo)(?:\s|$)", text)
        if command:
            return self.block_notice(
                {
                    "me": "/me action",
                    "poll": "interactive poll",
                    "todo": "collaborative to-do list",
                }[command[1]],
                text,
            )
        if (
            "\n" not in text
            and "`" not in text
            and re.search(r"(?<!\\)\$\$.+?\$\$", text)
        ):
            return self.block_notice("inline LaTeX", text)
        output = []
        position = 0
        while position < len(text):
            header = _QUOTE_HEADER.match(text, position)
            block_start = header.end() if header else position
            block = _fenced(text, block_start)
            if block is not None:
                info, content, end = block
                if info == "quote":
                    message_id = _near_id(header["url"]) if header else None
                    if (
                        header is not None
                        and message_id is not None
                        and urllib.parse.urlsplit(header["url"]).netloc
                        == urllib.parse.urlsplit(self.context["server_url"]).netloc
                        and urllib.parse.urlsplit(header["url"]).scheme
                        == urllib.parse.urlsplit(self.context["server_url"]).scheme
                        and (
                            not self.context.get("strict_identities")
                            or message_id in self.context["message_uuids"]
                        )
                    ):
                        target = f"urn:quote:{self.message_uuid(message_id)}"
                        original = self.context.get("messages", {}).get(message_id)
                        if original is None or content != original["content"]:
                            selected = (
                                self.inline(content)
                                if self.context.get("strict_identities")
                                else content
                            )
                            target += "?text=" + urllib.parse.quote(selected, safe="")
                        output.append(
                            _markdown_link(_escape_label(header["name"]), target)
                        )
                    else:
                        output.append(self.snapshot(content))
                        if header is not None and self.context.get("strict_identities"):
                            source = self.target(header["url"])
                            output.append(
                                "\n"
                                + (
                                    _markdown_link(
                                        _escape_label(header["name"]), source
                                    )
                                    if source is not None
                                    else header[0].rstrip("\r\n")
                                )
                            )
                elif info == "math":
                    output.append(self.block_notice("LaTeX block", text[position:end]))
                else:
                    output.append(self.inline(text[position:end]))
                    position = end
                    continue
                if text[end - 1] == "\n":
                    output.append("\n")
                position = end
                continue
            line_end = text.find("\n", position) + 1 or len(text)
            if re.match(r" {0,3}>", text[position:line_end]):
                lines = []
                while re.match(r" {0,3}>", text[position:line_end]):
                    lines.append(re.sub(r"^ {0,3}> ?", "", text[position:line_end]))
                    position = line_end
                    line_end = text.find("\n", position) + 1 or len(text)
                content = "".join(lines)
                output.append(self.snapshot(content.removesuffix("\n")))
                if content.endswith("\n"):
                    output.append("\n")
            else:
                output.append(self.inline(text[position:line_end]))
                position = line_end
        return "".join(output)


def zulip_to_workspace(message: dict, context: dict) -> dict[str, str]:
    """Convert a Zulip API Markdown message to a Workspace Markdown payload."""
    converter = _ZulipToWorkspace(message, context)
    content = converter.body(message["content"])
    if converter.unsupported:
        content = (
            content.rstrip()
            + "\n\n"
            + _markdown_link("Original message", f"urn:url:{context['original_url']}")
        )
    payload = message_payloads.MarkdownPayload(content=content)
    return {"kind": payload.kind, "content": payload.content}


def _reverse_lookup(context: dict, mapping: str, value: str) -> str:
    for key, mapped in context.get(mapping, {}).items():
        if mapped == value:
            return key
    raise ValueError(f"No Zulip mapping for {mapping}: {value}")


def workspace_to_zulip(payload: dict, context: dict) -> str:
    """Convert a Workspace Markdown payload to canonical Zulip Markdown.

    Identity and file URNs require their context mappings. Full quotes also
    require the referenced original messages; notices are not reversible.
    """
    model = message_payloads.WORKSPACE_MESSAGE_PAYLOAD_TYPE.from_simple_type(payload)
    if model.kind != "markdown":
        raise ValueError("Only Markdown messages can be converted to Zulip")

    def convert(label: str, target: str, title: str, image: bool) -> str | None:
        if not target.startswith("urn:"):
            return None
        kind, _, value = target[4:].partition(":")
        if kind == "url":
            if parse_url(value) is None:
                return None
            return _markdown_link(label, value, title, image)
        reference = _workspace_reference(target)
        if reference is None:
            return None
        kind, identifier, query = reference
        if kind == "user":
            if context.get("strict_identities"):
                if identifier not in context["user_ids"]:
                    raise ValueError(
                        f"No unambiguous Zulip user mapping for {identifier}"
                    )
                user_id, name = context["user_ids"][identifier]
                return f"@**{name}|{user_id}**"
            key = _reverse_lookup(context, "mention_uuids", identifier)
            name = _unescape(label)
            return f"@**{name}|{key[3:]}**" if key.startswith("id:") else f"@**{name}**"
        if kind in ("stream", "topic"):
            urls = context.get("stream_urls" if kind == "stream" else "topic_urls", {})
            if identifier in urls:
                return _markdown_link(label, urls[identifier], title)
            mapping = "stream_uuids" if kind == "stream" else "topic_uuids"
            name = _reverse_lookup(context, mapping, identifier.rsplit(":", 1)[-1])
            if _unescape(label) != "#" + name.replace(">", " > "):
                channel, separator, topic = name.partition(">")
                stream_id = context["stream_ids"][channel]
                slug = encode_hash(f"{stream_id}-{channel.replace(' ', '-')}")
                target = f"{context['server_url'].rstrip('/')}/#narrow/channel/{slug}"
                if separator:
                    target += "/topic/" + encode_hash(topic)
                return _markdown_link(label, target, title)
            return f"#**{name}**"
        if kind in ("file", "image", "video"):
            files = context.get("files", {})
            destination = next(
                (
                    url
                    for url, urn in files.items()
                    if urn.partition("?")[0] == target.partition("?")[0]
                ),
                None,
            )
            if destination is None:
                raise ValueError(f"No Zulip file mapping for {target}")
            return _markdown_link(label, destination, title, image)
        if kind == "forward":
            snapshot = parse_snapshot(query)
            if snapshot is None:
                return None
            content = workspace_to_zulip(
                {"kind": "markdown", "content": snapshot}, context
            )
            return _quote_fence(content)
        if kind in ("message", "quote"):
            message_id = context.get("message_ids", {}).get(identifier)
            if message_id is None:
                message_id = _reverse_lookup(context, "message_uuids", identifier)
            base_url = context["server_url"].rstrip("/")
            if kind == "message":
                name = _unescape(label).removeprefix("#").replace(" > ", ">")
                if label.startswith("#") and name in context.get("topic_uuids", {}):
                    return f"#**{name}@{message_id}**"
                return _markdown_link(
                    label, f"{base_url}/#narrow/near/{message_id}", title
                )
            source = context["messages"][message_id]
            selected = urllib.parse.parse_qs(query).get("text")
            content = selected[0] if selected else source["content"]
            if selected and context.get("strict_identities"):
                content = workspace_to_zulip(
                    {"kind": "markdown", "content": content}, context
                )
            url = f"{base_url}/#narrow/near/{message_id}"
            if source["type"] == "stream":
                channel = encode_hash(
                    f"{source['stream_id']}-{source['display_recipient'].replace(' ', '-')}"
                )
                topic = encode_hash(source["subject"])
                url = f"{base_url}/#narrow/channel/{channel}/topic/{topic}/near/{message_id}"
            author = source["sender_full_name"]
            return f"@_**{author}|{source['sender_id']}** [said]({url}):\n{_quote_fence(content)}"

    def workspace_text(text):
        if not context.get("normalize_emoji"):
            return text

        def replace_emoji(match):
            code = emoji.code_for_name(match[1])
            if code is None:
                return match[0]
            name = emoji.ZULIP_NAME_BY_CODE.get(code)
            return (
                f":{name}:"
                if name
                else "".join(chr(int(point, 16)) for point in code.split("-"))
            )

        return re.sub(r":([\w+-]+):", replace_emoji, text)

    text = model.content
    output = []
    position = 0
    tokens = re.compile(r"\\[^\n]|!?\[")
    while match := tokens.search(text, position):
        output.append(workspace_text(text[position : match.start()]))
        position = match.end()
        link = _link(text, match.start()) if not match[0].startswith("\\") else None
        if link is None:
            output.append(match[0])
        else:
            label, target, title, image, position = link
            converted = convert(label, target, title, image)
            output.append(
                text[match.start() : position] if converted is None else converted
            )
    output.append(workspace_text(text[position:]))
    return "".join(output)
