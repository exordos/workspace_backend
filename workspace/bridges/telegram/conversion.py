# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Stable Telegram identities and a JSON-safe message representation."""

import base64
import datetime
import re
import urllib.parse
import typing

import markdown_it

from telethon import helpers, utils
from telethon.tl import types


def peer_key(peer: object) -> str:
    for kind, model, field in (
        ("user", types.PeerUser, "user_id"),
        ("chat", types.PeerChat, "chat_id"),
        ("channel", types.PeerChannel, "channel_id"),
    ):
        if isinstance(peer, model):
            return f"{kind}:{getattr(peer, field)}"
    raise ValueError("Unsupported Telegram peer")


def stream_identity(account_id: typing.Any, peer: str) -> str:
    return peer if peer.startswith("channel:") else f"account:{account_id}:{peer}"


def message_identity(account_id: typing.Any, peer: str, message_id: int) -> str:
    return f"{stream_identity(account_id, peer)}:message:{message_id}"


def topic_identity(stream: str, topic_id: int) -> str:
    return f"{stream}:topic:{topic_id}"


def timestamp(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()


def normalize_message(
    account_id: typing.Any,
    peer: str,
    message: typing.Any,
    *,
    mentions: dict | None = None,
) -> dict:
    reply = message.reply_to
    forum = reply is not None and bool(reply.forum_topic)
    reactions = message.reactions
    counts = (
        {
            item.reaction.emoticon: item.count
            for item in reactions.results
            if isinstance(item.reaction, types.ReactionEmoji)
        }
        if reactions
        else {}
    )
    actors = (
        [
            {
                "user_id": utils.get_peer_id(item.peer_id),
                "emoji": item.reaction.emoticon,
            }
            for item in (reactions.recent_reactions or [])
            if isinstance(item.reaction, types.ReactionEmoji)
        ]
        if reactions
        else []
    )
    if reactions:
        for item in reactions.results:
            if (
                isinstance(item.reaction, types.ReactionEmoji)
                and item.chosen_order is not None
            ):
                own = {"user_id": int(account_id), "emoji": item.reaction.emoticon}
                if own not in actors:
                    actors.append(own)
    return {
        "id": message.id,
        "identity": message_identity(account_id, peer, message.id),
        "peer": peer,
        "topic_id": (reply.reply_to_top_id or reply.reply_to_msg_id or 0)
        if forum
        else 0,
        "sender_id": utils.get_peer_id(message.from_id or message.peer_id),
        "text": to_workspace(
            message.message or "", message.entities or [], mentions=mentions
        ),
        "date": timestamp(message.date),
        "edited_at": timestamp(message.edit_date or message.date),
        "out": bool(message.out),
        "reply_to": reply.reply_to_msg_id if reply else None,
        "media": base64.b64encode(bytes(message.media)).decode("ascii")
        if message.media
        else None,
        "grouped_id": str(message.grouped_id) if message.grouped_id else None,
        "reactions": counts,
        "reaction_actors": actors,
    }


def _escape(text: str) -> str:
    # Entity-free Telegram text is literal, including Workspace mentions/shortcodes.
    return re.sub(r"([\\`*_{()\[\]#+.!|>~<@:&-])", r"\\\1", text)


def to_workspace(text: str, entities: list, *, mentions: dict | None = None) -> str:
    text = helpers.add_surrogate(text)
    entities = sorted(entities, key=lambda item: (item.offset, -item.length))

    def render(start: int, end: int, values: list) -> str:
        result, cursor = [], start
        while values:
            entity, *values = values
            left, right = entity.offset, entity.offset + entity.length
            if left < cursor or right > end:
                continue
            result.append(_escape(helpers.del_surrogate(text[cursor:left])))
            children = [item for item in values if item.offset < right]
            values = [item for item in values if item.offset >= right]
            literal = helpers.del_surrogate(text[left:right])
            body = render(left, right, children)
            if (
                isinstance(entity, types.MessageEntityMentionName)
                and mentions
                and entity.user_id in mentions
            ):
                body = f"[{body}](urn:user:{mentions[entity.user_id]})"
            elif isinstance(entity, (types.MessageEntityCode, types.MessageEntityPre)):
                fence = "`" * max(
                    3 if isinstance(entity, types.MessageEntityPre) else 1,
                    max((len(item) for item in re.findall(r"`+", literal)), default=0)
                    + 1,
                )
                if isinstance(entity, types.MessageEntityPre):
                    language = re.sub(r"[^\w+-]", "", entity.language)
                    body = f"{fence}{language}\n{literal}\n{fence}"
                else:
                    padding = (
                        " " if literal.startswith("`") or literal.endswith("`") else ""
                    )
                    body = f"{fence}{padding}{literal}{padding}{fence}"
            elif isinstance(entity, types.MessageEntityBlockquote):
                body = "\n".join("> " + line for line in body.split("\n"))
                if left and text[left - 1] != "\n":
                    body = "\n\n" + body
                if right < len(text) and text[right] != "\n":
                    body += "\n\n"
            elif isinstance(
                entity,
                (
                    types.MessageEntityTextUrl,
                    types.MessageEntityUrl,
                    types.MessageEntityEmail,
                ),
            ):
                target = (
                    entity.url
                    if isinstance(entity, types.MessageEntityTextUrl)
                    else literal
                )
                if isinstance(entity, types.MessageEntityEmail):
                    target = "mailto:" + target
                if urllib.parse.urlsplit(target).scheme in (
                    "https",
                    "http",
                    "mailto",
                    "tg",
                ):
                    target = (
                        target.replace("<", "%3C")
                        .replace(">", "%3E")
                        .replace("\n", "%0A")
                    )
                    body = f"[{body}](<urn:url:{target}>)"
            else:
                marker = next(
                    (
                        marker
                        for model, marker in (
                            (types.MessageEntityBold, "**"),
                            (types.MessageEntityItalic, "*"),
                            (types.MessageEntityStrike, "~~"),
                            (types.MessageEntitySpoiler, "||"),
                        )
                        if isinstance(entity, model)
                    ),
                    "",
                )
                if marker and body.strip():
                    leading = body[: len(body) - len(body.lstrip())]
                    trailing = body[len(body.rstrip()) :]
                    body = leading + marker + body.strip() + marker + trailing
            result.append(body)
            cursor = right
        result.append(_escape(helpers.del_surrogate(text[cursor:end])))
        return "".join(result)

    return render(0, len(text), entities)


def _spoiler_rule(state: typing.Any, silent: bool) -> bool:
    if not state.src.startswith("||", state.pos):
        return False
    end = state.src.find("||", state.pos + 2)
    if end < 0:
        return False
    if not silent:
        state.push("spoiler_open", "", 1)
        # Use the same CommonMark inline parser inside the spoiler.
        # Its delimiter postprocessor must only see the nested token list.
        children: list[typing.Any] = []
        state.md.inline.parse(
            state.src[state.pos + 2 : end], state.md, state.env, children
        )
        for child in children:
            child.level += state.level
        state.tokens.extend(children)
        state.push("spoiler_close", "", -1)
    state.pos = end + 2
    return True


def to_telegram(markdown: str, *, references: dict | None = None) -> tuple[str, list]:
    """Parse Workspace CommonMark; Telegram's legacy Markdown is a different dialect."""
    parser = markdown_it.MarkdownIt("commonmark").enable("strikethrough")
    parser.inline.add_terminator_char("|")
    parser.inline.ruler.before("emphasis", "workspace_spoiler", _spoiler_rule)
    parts: list[str] = []
    entities: list = []
    opened: list[tuple[str, int, dict]] = []
    position = 0

    def append(value: str) -> None:
        nonlocal position
        parts.append(value)
        position += len(helpers.add_surrogate(value))

    def separator(count: int = 1) -> None:
        current = "".join(parts)
        trailing = len(current) - len(current.rstrip("\n"))
        if current and trailing < count:
            append("\n" * (count - trailing))

    models = {
        "strong": types.MessageEntityBold,
        "em": types.MessageEntityItalic,
        "s": types.MessageEntityStrike,
        "spoiler": types.MessageEntitySpoiler,
        "link": types.MessageEntityTextUrl,
        "blockquote": types.MessageEntityBlockquote,
    }

    def reference(target: str) -> bool:
        match = re.fullmatch(
            r"urn:(file|image|video|message|quote):([0-9a-fA-F-]{36})(?:\?.*)?",
            target,
        )
        if references is None or match is None:
            return False
        kind, identifier = match.groups()
        if kind in ("file", "image", "video"):
            references.setdefault("files", []).append(identifier)
        else:
            references.setdefault("reply", identifier)
        return True

    def walk(tokens: list) -> None:
        omitted_link = False
        for token in tokens:
            if omitted_link:
                if token.type == "link_close":
                    omitted_link = False
                continue
            if token.type == "link_open" and reference(token.attrGet("href")):
                omitted_link = True
                continue
            kind = token.type
            name = kind.removesuffix("_open").removesuffix("_close")
            if kind == "inline":
                walk(token.children or [])
            elif kind.endswith("_open") and name in models:
                args = (
                    {"url": token.attrGet("href").removeprefix("urn:url:")}
                    if name == "link"
                    else {}
                )
                opened.append((name, position, args))
            elif kind.endswith("_close") and name in models:
                previous, start, args = opened.pop()
                assert previous == name
                if name == "link" and references is not None:
                    mention = re.fullmatch(r"urn:user:([0-9a-fA-F-]{36})", args["url"])
                    if mention and position > start:
                        references.setdefault("mentions", []).append(
                            {
                                "user_uuid": mention[1],
                                "offset": start,
                                "length": position - start,
                            }
                        )
                if position > start and (
                    name != "link"
                    or urllib.parse.urlsplit(args["url"]).scheme
                    in ("https", "http", "mailto", "tg")
                ):
                    entities.append(
                        models[name](offset=start, length=position - start, **args)
                    )
            elif kind in ("fence", "code_block", "code_inline"):
                literal = (
                    token.content.rstrip("\n")
                    if kind != "code_inline"
                    else token.content
                )
                args = (
                    {"language": token.info.split()[0] if token.info else ""}
                    if kind != "code_inline"
                    else {}
                )
                model = (
                    types.MessageEntityPre
                    if kind != "code_inline"
                    else types.MessageEntityCode
                )
                entities.append(
                    model(
                        offset=position,
                        length=len(helpers.add_surrogate(literal)),
                        **args,
                    )
                )
                append(literal)
                if kind != "code_inline":
                    separator()
            elif kind in ("text", "html_inline", "html_block"):
                append(token.content)
            elif kind in ("softbreak", "hardbreak"):
                append("\n")
            elif kind == "image":
                if not reference(token.attrGet("src")):
                    append(token.content)
            elif kind == "list_item_open":
                separator()
                append("• ")
            elif kind == "paragraph_close":
                separator(1 if token.hidden else 2)
            elif kind in ("heading_close", "list_item_close", "blockquote_close"):
                separator()
            elif kind == "hr":
                separator()
                append("───\n")

    walk(parser.parse(markdown))
    text = "".join(parts).rstrip("\n")
    length = len(helpers.add_surrogate(text))
    for entity in entities:
        entity.length = min(entity.length, length - entity.offset)
    entities = [entity for entity in entities if entity.length > 0]
    return text, sorted(entities, key=lambda item: (item.offset, -item.length))
