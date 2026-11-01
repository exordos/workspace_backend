# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Account-scoped receipts for stable, completely fetched history ranges."""

import collections
import hashlib
import json
import re
import typing
import urllib.parse
import uuid

from workspace.bridges.zulip import (
    content_cache,
    incoming_operations,
    message_conversion,
)

# Bump when the importer/converter changes the meaning of a successful receipt.
FORMAT_VERSION = 1


def digest(value: object) -> bytes:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).digest()


def fingerprints(messages: list[dict]) -> tuple[bytes, bytes]:
    data, state = [], []
    for message in sorted(messages, key=lambda item: item["id"]):
        fields = {
            key: message[key]
            for key in ("id", "type", "sender_id", "content", "timestamp")
        }
        fields["last_edit_timestamp"] = (
            message.get("last_edit_timestamp") or message["timestamp"]
        )
        if message["type"] == "stream":
            fields.update(stream_id=message["stream_id"], subject=message["subject"])
        else:
            fields["recipients"] = sorted(
                item["id"] for item in message["display_recipient"]
            )
        fields["reactions"] = sorted(
            (
                {
                    key: item[key]
                    for key in ("user_id", "emoji_name", "emoji_code", "reaction_type")
                }
                for item in message.get("reactions", [])
            ),
            key=lambda item: (
                item["user_id"],
                item["reaction_type"],
                item["emoji_code"],
                item["emoji_name"],
            ),
        )
        data.append(fields)
        state.append(
            [message["id"], sorted(set(message["flags"]) & {"read", "starred"})]
        )
    return digest(data), digest(state)


def matching_names(text: str, names: typing.Iterable[str]) -> set[str]:
    """Find every catalog name without rescanning the page for every name.

    A trie with failure links preserves substring semantics, including nested
    and overlapping names. Construction visits each name character once; the
    page is then scanned once, rather than once per topic in a growing catalog.
    """
    names = set(names)
    if len(names) < 32:
        return {name for name in names if name in text}
    edges: list[dict[str, int]] = [{}]
    failures = [0]
    outputs: list[set[str]] = [set()]
    for name in names:
        state = 0
        for character in name:
            if character not in edges[state]:
                edges[state][character] = len(edges)
                edges.append({})
                failures.append(0)
                outputs.append(set())
            state = edges[state][character]
        outputs[state].add(name)
    pending = collections.deque(edges[0].values())
    while pending:
        state = pending.popleft()
        for character, target in edges[state].items():
            pending.append(target)
            fallback = failures[state]
            while fallback and character not in edges[fallback]:
                fallback = failures[fallback]
            failures[target] = edges[fallback].get(character, 0)
            outputs[target].update(outputs[failures[target]])
    found = set(outputs[0])
    state = 0
    visited = set()
    for character in text:
        while state and character not in edges[state]:
            state = failures[state]
        state = edges[state].get(character, 0)
        if state not in visited:
            visited.add(state)
            found.update(outputs[state])
    return found


def linked_stream_identities(text: str, identities: typing.Iterable[str]) -> set[str]:
    # Zulip streams use numeric IDs or a deterministic DM UUID. Extract those
    # links once, while preserving legacy/nonstandard identity matching.
    standard = r"(?:\d+|dm:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})"
    found = set(re.findall(r"channel/(" + standard + r")(?=\D|$)", text))
    return {
        identity
        for identity in identities
        if identity in found
        or (
            not re.fullmatch(standard, identity)
            and "channel/" in text
            and re.search(r"channel/" + re.escape(identity) + r"(?:\D|$)", text)
        )
    }


async def context(
    connection: typing.Any,
    account: dict,
    messages: list[dict],
    reaction_user_limit: int,
    prepared: dict | None = None,
) -> bytes:
    """Fingerprint identities/access used by conversion, without converting text.

    Only referenced catalog entries participate: importing unrelated older topics
    must not invalidate receipts of already committed newer blocks.
    """
    scope = (account["project_id"], account["provider_uuid"])
    refs = incoming_operations.references(messages)
    people = set(refs["people"])
    identities = {
        incoming_operations.stream_identity(account, item) for item in messages
    }
    for item in messages:
        people.update(str(r["user_id"]) for r in item.get("reactions", []))
        if item["type"] != "stream":
            people.update(str(r["id"]) for r in item["display_recipient"])
    text = "\n".join(item["content"] for item in messages)
    decoded = urllib.parse.unquote(re.sub(r"\.([\da-fA-F]{2})", r"%\1", text))
    for remote in re.findall(r"(?:^|/)dm/(\d+(?:,\d+)*)", decoded):
        recipients = sorted(
            set(map(int, remote.split(","))) | {int(account["external_account_id"])}
        )
        identities.add(
            "dm:"
            + str(
                uuid.uuid5(
                    account["provider_uuid"], "dm:" + ",".join(map(str, recipients))
                )
            )
        )
    catalog = (
        prepared["catalog"]
        if prepared is not None
        else await incoming_operations.rows(
            connection,
            """
        SELECT account.external_account_id,account.owner_user_uuid,users.first_name
        FROM m_external_accounts_v2 account JOIN m_workspace_users users ON users.uuid=account.owner_user_uuid
        WHERE account.project_id=%s AND account.provider_uuid=%s
          AND (account.external_account_id=ANY(%s::text[]) OR users.first_name=ANY(%s::text[]))
        ORDER BY account.external_account_id,account.owner_user_uuid
    """,
            (*scope, sorted(people), sorted(refs["names"])),
        )
    )
    catalog = sorted(
        [
            {
                key: row[key]
                for key in ("external_account_id", "owner_user_uuid", "first_name")
            }
            for row in catalog
            if row["external_account_id"] in people
            or row["first_name"] in refs["names"]
        ],
        key=lambda row: (row["external_account_id"], str(row["owner_user_uuid"])),
    )
    streams = (
        prepared["streams"]
        if prepared is not None
        else await incoming_operations.rows(
            connection,
            """
        SELECT stream.uuid,stream.provider_identity,stream.name,stream.deleted_at,
               member.active,member.membership_generation
        FROM messenger_streams stream LEFT JOIN messenger_stream_bindings member
          ON member.project_id=stream.project_id AND member.stream_uuid=stream.uuid AND member.user_uuid=%s
        WHERE stream.project_id=%s AND stream.provider_uuid=%s AND stream.provider_identity IS NOT NULL
        ORDER BY stream.uuid
    """,
            (account["owner_user_uuid"], *scope),
        )
    )
    stream_names = matching_names(decoded, (row["name"] for row in streams))
    channel_ids = linked_stream_identities(
        decoded,
        (
            row["provider_identity"]
            for row in streams
            if row["provider_identity"] is not None
        ),
    )
    streams = sorted(
        [
            {
                key: row[key]
                for key in (
                    "uuid",
                    "provider_identity",
                    "name",
                    "deleted_at",
                    "active",
                    "membership_generation",
                )
            }
            for row in streams
            if row["provider_identity"] is not None
            and (
                row["provider_identity"] in identities
                or row["name"] in stream_names
                or row["provider_identity"] in channel_ids
            )
        ],
        key=lambda row: str(row["uuid"]),
    )
    bindings = await incoming_operations.rows(
        connection,
        """
        SELECT message.provider_identity,message.uuid,message.deleted_at,message.content_updated_at,placement.uuid AS placement_uuid,
               placement.stream_uuid,placement.topic_uuid,access.uuid AS binding_uuid
        FROM messenger_messages message
        LEFT JOIN LATERAL (
            SELECT * FROM messenger_message_placements
            WHERE project_id=message.project_id AND message_uuid=message.uuid OFFSET 0
        ) placement ON true
        LEFT JOIN LATERAL (
            SELECT uuid FROM messenger_user_message_bindings
            WHERE project_id=placement.project_id AND placement_uuid=placement.uuid AND user_uuid=%s
            LIMIT 1
        ) access ON true
        WHERE message.project_id=%s AND message.provider_uuid=%s AND message.provider_identity=ANY(%s::text[])
        ORDER BY message.provider_identity,placement.uuid
    """,
        (account["owner_user_uuid"], *scope, [str(item["id"]) for item in messages]),
    )
    if prepared is not None:
        # The content-change trigger can temporarily replace an imported edit
        # time during reference refinement. Do not certify that intermediate row.
        for binding in bindings:
            binding["content_updated_at"] = prepared["edited"].get(
                str(binding["uuid"]), binding["content_updated_at"]
            )
    binding_topics = {binding["topic_uuid"] for binding in bindings}
    topic_parameters: tuple = (*scope, [row["uuid"] for row in streams])
    if prepared is not None:
        topic_parameters += (list(binding_topics),)
    current_topics = await incoming_operations.rows(
        connection,
        f"""
        SELECT uuid,stream_uuid,provider_identity,name,is_done,deleted_at
        FROM messenger_topics WHERE project_id=%s AND provider_uuid=%s
          AND stream_uuid=ANY(%s::uuid[])
          {"AND uuid=ANY(%s::uuid[])" if prepared is not None else ""}
        ORDER BY uuid
    """,
        topic_parameters,
    )
    topics = prepared["topics"] if prepared is not None else current_topics
    stream_ids = {row["uuid"] for row in streams}
    topics = [row for row in topics if row["stream_uuid"] in stream_ids]
    topic_names = matching_names(decoded, (row["name"] for row in topics))
    remote_topics = set(refs["remote_topics"])
    topics = sorted(
        [
            {
                key: row[key]
                for key in (
                    "uuid",
                    "stream_uuid",
                    "provider_identity",
                    "name",
                    "is_done",
                )
            }
            | {"deleted_at": row.get("deleted_at")}
            for row in topics
            if row["provider_identity"] in remote_topics or row["name"] in topic_names
        ],
        key=lambda row: str(row["uuid"]),
    )
    references = (
        prepared["references"]
        if prepared is not None
        else await incoming_operations.rows(
            connection,
            incoming_operations.SELECT_MESSAGE_REFERENCES_SQL,
            (
                account["project_id"],
                account["owner_user_uuid"],
                sorted(refs["messages"]),
                account["provider_uuid"],
            ),
        )
    )
    references = {
        str(row["remote_id"]): str(row["uuid"])
        for row in references
        if str(row["remote_id"]) in refs["messages"]
    }
    active = {row["provider_identity"] for row in streams if row["active"]}
    paths = upload_paths(
        account,
        [
            item
            for item in messages
            if incoming_operations.stream_identity(account, item) in active
        ],
    )
    files = (
        prepared["files"]
        if prepared is not None
        else await incoming_operations.rows(
            connection,
            """
        SELECT uuid,stream_uuid,provider_identity,content_type,hash,storage_object_id
        FROM m_workspace_files WHERE project_id=%s AND provider_uuid=%s
          AND stream_uuid=ANY(%s::uuid[]) AND provider_identity=ANY(%s::text[]) ORDER BY uuid
        """,
            (*scope, [row["uuid"] for row in streams if row["active"]], sorted(paths)),
        )
        if paths
        else []
    )
    files = sorted(
        [
            {
                key: row[key]
                for key in (
                    "uuid",
                    "stream_uuid",
                    "provider_identity",
                    "content_type",
                    "hash",
                    "storage_object_id",
                )
            }
            for row in files
            if row["provider_identity"] in paths
        ],
        key=lambda row: str(row["uuid"]),
    )
    return digest(
        [
            FORMAT_VERSION,
            content_cache.CONVERTER_VERSION,
            str(account["uuid"]),
            str(account["owner_user_uuid"]),
            str(account["project_id"]),
            str(account["provider_uuid"]),
            account["external_account_id"],
            account["hostname"],
            account["settings"]["server_url"],
            sorted(account["subscribed_streams"])
            if account["subscribed_streams"] is not None
            else None,
            reaction_user_limit,
            catalog,
            streams,
            topics,
            sorted(
                [row for row in current_topics if row["uuid"] in binding_topics],
                key=lambda row: str(row["uuid"]),
            ),
            bindings,
            references,
            files,
        ]
    )


def cacheable(messages: list[dict], batch: dict) -> bool:
    files = {
        (row["stream_uuid"], row["provider_identity"])
        for row in batch["m_workspace_files"]
    }
    roots = {
        row["provider_identity"]: row["uuid"] for row in batch["messenger_messages"]
    }
    streams = {
        row["message_uuid"]: row["stream_uuid"]
        for row in batch["messenger_message_placements"]
    }
    return all(
        (streams[roots[str(item["id"])]], path) in files
        for item in messages
        for _, path in item.get("_files", [])
    )


def upload_paths(account: dict, messages: list[dict]) -> set[str]:
    paths = set()
    for item in messages:
        if "/user_uploads/" not in item["content"] and "![" not in item["content"]:
            continue
        for target in message_conversion.get_zulip_files(item, {}):
            endpoint = message_conversion.parse_url(
                target, account["settings"]["server_url"]
            )
            if endpoint is not None and endpoint.hostname == account["hostname"]:
                path = message_conversion.zulip_upload_path(endpoint.path)
                if path is not None:
                    paths.add(path)
    return paths
