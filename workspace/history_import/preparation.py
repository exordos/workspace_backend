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

"""Prepare import records and storage objects before acquiring write locks."""

import typing

import dataclasses
import datetime
import pathlib
import re
import unicodedata
import urllib.parse
import uuid as sys_uuid

from restalchemy.dm import types_dynamic

from workspace.external_bridge_control import identity_linking
from workspace.external_bridge_control import provider_v2
from workspace.external_bridge_control import sql_state
from workspace.history_import import contract
from workspace.history_import import markdown_conversion
from workspace.history_import import zulip_markdown
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import models


FILE_URN_PATTERN = re.compile(
    r"urn:(?:file|image|video):"
    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?![0-9a-fA-F-])"
)


def preserved_file_ids(prior: dict | None) -> list[str]:
    if prior is None or prior["deleted_at"] is not None:
        return []
    return sorted(
        {
            match.lower()
            for match in FILE_URN_PATTERN.findall(prior["payload"].get("content", ""))
        }
    )


def attachment_paths(content: str) -> set[str]:
    paths = set()

    def collect(link: markdown_conversion.MarkdownLink) -> str:
        if link.destination.startswith("/user_uploads/"):
            decoded = urllib.parse.unquote(urllib.parse.urlsplit(link.destination).path)
            if ".." in pathlib.PurePosixPath(decoded).parts or "\\" in decoded:
                raise contract.ImportError("invalid_history_attachment_path")
            contract.text(link.destination, 4096)
            paths.add(link.destination)
        return link.raw

    markdown_conversion.transform_markdown(
        content,
        text_transform=lambda value: value,
        link_transform=collect,
        convert_semantic_quotes=True,
    )
    return paths


def file_requests(
    batch: contract.Batch,
    sources: list[dict],
    job_uuid: sys_uuid.UUID,
    created_at: datetime.datetime,
    *,
    messages: list[dict] | None = None,
) -> list[dict]:
    observers: dict[tuple[str, int], set[str]] = {}
    for source in sources:
        observers.setdefault(
            (source["chat_key"], int(source["provider_owner_user_id"])), set()
        ).add(str(source["account_uuid"]))
    files: dict[str, set[str]] = {}
    for message in batch.body["messages"] if messages is None else messages:
        accounts = set()
        for access in eligible_accesses(message, sources, created_at):
            accounts.update(observers[(contract.chat_key(message), access["user_id"])])
        for path in attachment_paths(message["content"]) if accounts else ():
            files.setdefault(path, set()).update(accounts)
    return [
        {
            "uuid": str(sys_uuid.uuid5(job_uuid, "file:" + path)),
            "source_path": path,
            "account_uuids": sorted(accounts),
        }
        for path, accounts in sorted(files.items())
    ]


def load_identities(session: typing.Any, batch: contract.Batch) -> dict[int, str]:
    rows = session.execute(
        """SELECT provider_user_id, workspace_user_uuid
           FROM m_external_provider_identity_links_v1
           WHERE provider = 'zulip' AND provider_realm_uuid = %s
             AND provider_user_id = ANY(%s::text[])""",
        (batch.provider_realm_uuid, [str(user["id"]) for user in batch.body["users"]]),
    ).fetchall()
    links = {
        int(row["provider_user_id"]): str(row["workspace_user_uuid"]) for row in rows
    }
    return {
        user["id"]: links.get(
            user["id"],
            str(
                identity_linking.canonical_provider_identity_uuid(
                    "zulip", batch.provider_realm_uuid, str(user["id"])
                )
            ),
        )
        for user in batch.body["users"]
    }


def directory_records(batch: contract.Batch, identities: dict[int, str]) -> list[dict]:
    # These are insert-only records. Persisted profiles, including their avatars,
    # are preserved by ON CONFLICT DO NOTHING in write_directory.
    return [
        {
            "uuid": identities[user["id"]],
            "id": str(user["id"]),
            "name": user["name"],
            "avatar": models.build_workspace_user_default_avatar(
                identities[user["id"]]
            ),
        }
        for user in batch.body["users"]
    ]


def eligible_accesses(
    message: dict, sources: list[dict], created_at: datetime.datetime
) -> list[dict]:
    timestamp = datetime.datetime.fromtimestamp(
        message["sent_at"], datetime.timezone.utc
    )
    key = contract.chat_key(message)
    observers = []
    for access in message["access"]:
        for source in sources:
            if (
                source["chat_key"] != key
                or int(source["provider_owner_user_id"]) != access["user_id"]
            ):
                continue
            depth = source["history_depth"]
            days = {"7_days": 7, "30_days": 30, "90_days": 90}.get(depth)
            cutoff = created_at - datetime.timedelta(days=days) if days else None
            if depth != "new" and (cutoff is None or timestamp >= cutoff):
                observers.append(access)
                break
    return observers


def source_for(message: dict, route: dict, *, include_message: bool) -> dict:
    value = models.ZulipSource(
        stream_id=message["channel_id"] or 0,
        server_url=route["server_url"],
        source_scope=str(route["account_uuid"]),
        topic_name=message["topic"],
        message_id=message["id"] if include_message else None,
    )
    return types_dynamic.KindModelType(models.ZulipSource).to_simple_type(value)


def topic_for(message: dict, route: dict) -> tuple[str, str, str]:
    source = route["source"]
    if message["channel_id"] is None:
        matches = [topic for topic in source["topics"] if topic.get("is_default")]
        if not matches and len(source["topics"]) == 1:
            matches = source["topics"]
        name = "Zulip"
    else:
        name = message["topic"] or "general chat"
        if name.casefold() == "general chat":
            name = "general chat"
        provider_topic_id = f"{message['channel_id']}:{name}"
        matches = [
            topic
            for topic in source["topics"]
            if topic["provider_topic_id"] == provider_topic_id
        ]
    if not matches and message["channel_id"] is not None:
        return (
            str(
                sql_state._projection_uuid(
                    sys_uuid.UUID(str(route["topic_namespace_uuid"])),
                    "topic",
                    provider_topic_id,
                )
            ),
            name,
            provider_topic_id,
        )
    if len(matches) != 1:
        raise contract.ImportError("history_topic_mapping_pending", 409)
    return (
        str(sys_uuid.UUID(matches[0]["topic_uuid"])),
        name,
        matches[0]["provider_topic_id"],
    )


def existing_message_ids(
    session: typing.Any, batch: contract.Batch, messages: list[dict]
) -> set[int]:
    identifiers = [str(message["id"]) for message in messages]
    rows = session.execute(
        """SELECT provider_message_id FROM messenger_messages
           WHERE provider_realm_uuid = %s AND provider_message_id = ANY(%s::text[])
           UNION
           SELECT provider_message_id FROM m_external_history_tombstones_v1
           WHERE provider_realm_uuid = %s AND provider_message_id = ANY(%s::text[])""",
        (
            batch.provider_realm_uuid,
            identifiers,
            batch.provider_realm_uuid,
            identifiers,
        ),
    ).fetchall()
    return {int(row["provider_message_id"]) for row in rows}


def existing_messages(
    session: typing.Any, batch: contract.Batch, messages: list[dict]
) -> dict[int, dict]:
    rows = session.execute(
        """SELECT message.uuid, message.project_id, message.provider_message_id,
                  message.updated_at, message.deleted_at, message.author_uuid, message.payload,
                  placement.uuid AS placement_uuid, placement.stream_uuid,
                  placement.topic_uuid, placement.legacy_public_uuid
           FROM messenger_messages AS message
           LEFT JOIN LATERAL (
               SELECT * FROM messenger_message_placements AS placement
               WHERE placement.project_id = message.project_id AND placement.message_uuid = message.uuid
               ORDER BY placement.created_at, placement.uuid LIMIT 1
           ) AS placement ON true
           WHERE message.provider_realm_uuid = %s AND message.provider_message_id = ANY(%s::text[])""",
        (batch.provider_realm_uuid, [str(message["id"]) for message in messages]),
    ).fetchall()
    result = {int(row["provider_message_id"]): dict(row) for row in rows}
    tombstones = session.execute(
        """SELECT provider_message_id, deleted_at FROM m_external_history_tombstones_v1
           WHERE provider_realm_uuid = %s AND provider_message_id = ANY(%s::text[])""",
        (batch.provider_realm_uuid, [str(message["id"]) for message in messages]),
    ).fetchall()
    for row in tombstones:
        message_id = int(row["provider_message_id"])
        if message_id not in result:
            result[message_id] = dict.fromkeys(
                (
                    "uuid",
                    "project_id",
                    "placement_uuid",
                    "stream_uuid",
                    "topic_uuid",
                    "updated_at",
                    "legacy_public_uuid",
                    "author_uuid",
                    "payload",
                )
            )
            result[message_id]["deleted_at"] = row["deleted_at"]
    return result


class MappingStore:
    """The shared converter sees only this in-memory lookup surface."""

    def __init__(self, mappings: dict) -> None:
        self.mappings = mappings

    def provider_mapping(
        self, account_uuid: str, kind: str, provider_id: str
    ) -> dict | None:
        return self.mappings.get((kind, provider_id))

    def provider_mapping_by_name(
        self, account_uuid: str, kind: str, name: str
    ) -> dict | None:
        matches = [
            value
            for (item_kind, _), value in self.mappings.items()
            if item_kind == kind
            and value["metadata"].get("name", "").casefold() == name.casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def workspace_mapping(
        self, account_uuid: str, kind: str, workspace_uuid: str
    ) -> dict | None:
        return next(
            (
                value
                for (item_kind, _), value in self.mappings.items()
                if item_kind == kind and value["workspace_uuid"] == workspace_uuid
            ),
            None,
        )


def mappings_for(
    batch: contract.Batch,
    sources: list[dict],
    identities: dict,
    existing: dict,
    created_at: datetime.datetime,
) -> MappingStore:
    mappings = {}
    for user in batch.body["users"]:
        mappings[("identity", str(user["id"]))] = {
            "workspace_uuid": identities[user["id"]],
            "provider_id": str(user["id"]),
            "metadata": {"display_name": user["name"]},
        }
    for route in sources:
        key = provider_v2._legacy_chat_key(route["chat_key"])
        mappings[("stream", key)] = {
            "workspace_uuid": str(route["projection_stream_uuid"]),
            "provider_id": key,
            "metadata": {
                "name": route["display_name"],
                "chat_type": "channel" if key.startswith("channel:") else "direct",
            },
        }
        for topic in route["source"]["topics"]:
            mappings[("topic", topic["provider_topic_id"])] = {
                "workspace_uuid": str(topic["topic_uuid"]),
                "provider_id": topic["provider_topic_id"],
                "metadata": {},
            }
    routes = {row["chat_key"]: row for row in sources}
    for message in batch.body["messages"]:
        if not eligible_accesses(message, sources, created_at):
            continue
        canonical_uuid = sys_uuid.uuid5(
            batch.provider_realm_uuid, f"message:{message['id']}"
        )
        topic_uuid, topic_name, provider_topic_id = topic_for(
            message, routes[contract.chat_key(message)]
        )
        if message["channel_id"] is not None:
            mappings[("topic", provider_topic_id)] = {
                "workspace_uuid": topic_uuid,
                "provider_id": provider_topic_id,
                "metadata": {"name": topic_name},
            }
        public_uuid = str(
            sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), str(canonical_uuid))
        )
        if message["id"] in existing:
            prior = existing[message["id"]]
            if prior["deleted_at"] is not None or prior["placement_uuid"] is None:
                continue
            public_uuid = str(prior["legacy_public_uuid"] or prior["placement_uuid"])
        mappings[("message", str(message["id"]))] = {
            "workspace_uuid": public_uuid,
            "provider_id": str(message["id"]),
            "metadata": {"author_uuid": identities[message["sender_id"]]},
        }
    # Include quoted messages outside the incoming range, using their existing IDs.
    for message_id, prior in existing.items():
        if (
            ("message", str(message_id)) not in mappings
            and prior["placement_uuid"]
            and prior["deleted_at"] is None
        ):
            mappings[("message", str(message_id))] = {
                "workspace_uuid": str(
                    prior["legacy_public_uuid"] or prior["placement_uuid"]
                ),
                "provider_id": str(message_id),
                "metadata": {"author_uuid": str(prior["author_uuid"])},
            }
    return MappingStore(mappings)


def referenced_message_ids(messages: list[dict]) -> set[int]:
    result = {message["id"] for message in messages}
    for message in messages:
        result.update(
            int(value)
            for value in re.findall(r"(?:/near/|@)([0-9]+)", message["content"])
            if len(value) <= 16 and 0 < int(value) <= 2**53 - 1
        )
    return result


def emoji_name(reaction: dict) -> str:
    if reaction["reaction_type"] != "unicode_emoji":
        return reaction["emoji_name"]
    try:
        value = "".join(
            chr(int(code, 16)) for code in reaction["emoji_code"].split("-")
        )
        value = unicodedata.normalize(
            "NFC", value.replace("\ufe0f", "").replace("\ufe0e", "")
        )
        contract.text(value, 128)
        if not value:
            raise ValueError()
        return value
    except (ValueError, OverflowError) as error:
        raise contract.ImportError("invalid_history_emoji") from error


@dataclasses.dataclass(frozen=True)
class PreparedPart:
    messages: list[dict]
    topics: list[dict]
    states: list[dict]
    reactions: list[dict]
    files: list[dict]
    file_access: list[dict]
    expected_messages: dict
    identities: dict
    source_ids: list[int]
    encoded: dict
    file_access_progress: dict | None = None


def prepare_part(
    batch: contract.Batch,
    job: dict,
    messages: list[dict],
    sources: list[dict],
    identities: dict,
    existing: dict,
    files: dict,
    mapping_store: MappingStore,
    reaction_user_limit: int = 4,
    metadata_cache: set | None = None,
) -> PreparedPart:
    # Restored ACLs can fan out independently of incoming attachment counts.
    # Isolate their message and page the Cartesian product without building it.
    for index, message in enumerate(messages):
        if preserved_file_ids(existing.get(message["id"])):
            messages = messages[:index] if index else messages[:1]
            break
    file_access_progress = None
    routes: dict[str, dict] = {}
    for row in sources:
        key = row["chat_key"]
        if (
            key in routes
            and routes[key]["projection_stream_uuid"] != row["projection_stream_uuid"]
        ):
            raise contract.ImportError("history_route_conflict", 409)
        routes[key] = row
    result, topics, states, reactions, file_records, accesses = [], {}, [], [], {}, {}
    mention_uuids = {
        f"id:{user_id}": user_uuid for user_id, user_uuid in identities.items()
    }
    names: dict[str, list[str]] = {}
    for user in batch.body["users"]:
        names.setdefault(user["name"], []).append(identities[user["id"]])
    mention_uuids.update(
        {name: values[0] for name, values in names.items() if len(values) == 1}
    )
    for message in messages:
        observers = eligible_accesses(message, sources, job["created_at"])
        if not observers:
            continue
        key = contract.chat_key(message)
        route = next(
            (
                source
                for source in sources
                if source["chat_key"] == key
                and int(source["provider_owner_user_id"]) == message["sender_id"]
            ),
            routes[key],
        )
        stream_uuid = str(route["projection_stream_uuid"])
        topic_uuid, topic_name, provider_topic_id = topic_for(message, route)
        message_uuid = str(
            sys_uuid.uuid5(batch.provider_realm_uuid, f"message:{message['id']}")
        )
        placement_uuid = str(sys_uuid.uuid5(sys_uuid.UUID(topic_uuid), message_uuid))
        prior = existing.get(message["id"])
        if prior:
            if (
                prior["deleted_at"] is not None
                or str(prior["project_id"]) != str(batch.project_uuid)
                or str(prior["stream_uuid"]) != stream_uuid
                or str(prior["topic_uuid"]) != topic_uuid
            ):
                continue
            message_uuid, placement_uuid = (
                str(prior["uuid"]),
                str(prior["placement_uuid"]),
            )
        timestamp = datetime.datetime.fromtimestamp(
            message["sent_at"], datetime.timezone.utc
        )
        topics[topic_uuid] = {
            "uuid": topic_uuid,
            "stream_uuid": stream_uuid,
            "name": topic_name,
            "source": source_for(message, route, include_message=False),
            "provider": {
                "kind": "zulip",
                "provider_realm_uuid": str(batch.provider_realm_uuid),
                "chat_key": key,
                "external_id": provider_topic_id,
            },
        }
        owner_uuid = str(route["owner_user_uuid"])

        def resolve_file(path: str, label: str) -> str | None:
            transfer = files[path]
            if transfer["status"] == "unavailable":
                return None
            if transfer["status"] != "ready":
                raise contract.ImportError("history_attachment_pending", 409)
            # Metadata identity includes its authority; content bytes remain deduplicated.
            descriptor_key = contract.digest(
                {
                    "realm": str(batch.provider_realm_uuid),
                    "path": path,
                    "stream": stream_uuid,
                    "owner": owner_uuid,
                    "account": str(route["account_uuid"]),
                    "sha256": transfer["sha256"],
                    "name": transfer["name"],
                    "content_type": transfer["content_type"],
                }
            )
            file_uuid = sys_uuid.uuid5(job["uuid"], "history-file:v1:" + descriptor_key)
            storage_info = file_storage.WorkspaceFileStorageInfo(**transfer["storage"])
            if str(file_uuid) not in file_records:
                metadata = file_storage.WorkspaceFileMetadata(
                    uuid=file_uuid,
                    project_id=batch.project_uuid,
                    stream_uuid=sys_uuid.UUID(stream_uuid),
                    owner_uuid=sys_uuid.UUID(owner_uuid),
                    name=transfer["name"],
                    description="",
                    content_type=transfer["content_type"],
                    size_bytes=transfer["size_bytes"],
                    sha256=transfer["sha256"],
                    created_at=job["created_at"],
                    origin={
                        "kind": "external_provider",
                        "provider_kind": "zulip",
                        "external_account_uuid": str(route["account_uuid"]),
                        "external_chat_uuid": str(route["chat_uuid"]),
                        "operation_uuid": str(file_uuid),
                    },
                )
                if metadata_cache is None or file_uuid not in metadata_cache:
                    file_storage.save_workspace_file_metadata(
                        metadata, storage_type=storage_info.storage_type
                    )
                    if metadata_cache is not None:
                        metadata_cache.add(file_uuid)
                file_records[str(file_uuid)] = {
                    "uuid": str(file_uuid),
                    "stream_uuid": stream_uuid,
                    "owner_uuid": owner_uuid,
                    "account_uuid": str(route["account_uuid"]),
                    "name": metadata.name,
                    "content_type": metadata.content_type,
                    "size_bytes": metadata.size_bytes,
                    "sha256": metadata.sha256,
                    **dataclasses.asdict(storage_info),
                }
            for access in observers:
                user_uuid = identities[access["user_id"]]
                accesses[(str(file_uuid), user_uuid)] = {
                    "uuid": str(sys_uuid.uuid5(file_uuid, user_uuid)),
                    "file_uuid": str(file_uuid),
                    "user_uuid": user_uuid,
                }
            return f"urn:file:{file_uuid}"

        original_url = (
            route["server_url"].rstrip("/") + f"/#narrow/near/{message['id']}"
        )
        if prior is None:
            content = zulip_markdown._canonicalize_semantic_quotes(
                message["content"], mapping_store, str(route["account_uuid"])
            )
            content, _lossy = zulip_markdown.convert_markdown(
                content,
                mention_uuids,
                original_url,
                file_resolver=resolve_file,
                link_resolver=zulip_markdown.ZulipLinkResolver(
                    mapping_store, str(route["account_uuid"]), owner_uuid
                ),
            )
            contract.markdown_content(content)
        else:
            content = prior["payload"].get("content", "")
            file_ids = preserved_file_ids(prior)
            users = sorted({identities[access["user_id"]] for access in observers})
            fingerprint = contract.digest(
                {"content": content, "users": users, "placement": placement_uuid}
            )
            cursor = job.get("file_access_progress") or {}
            offset = (
                cursor["offset"]
                if cursor.get("source_id") == message["id"]
                and cursor.get("fingerprint") == fingerprint
                else 0
            )
            total = len(file_ids) * len(users)
            if offset < total:
                end = min(total, offset + job["part_size"] * 10)
                for index in range(offset, end):
                    file_id, user_uuid = (
                        file_ids[index // len(users)],
                        users[index % len(users)],
                    )
                    accesses[(file_id, user_uuid)] = {
                        "uuid": str(sys_uuid.uuid5(sys_uuid.UUID(file_id), user_uuid)),
                        "file_uuid": file_id,
                        "user_uuid": user_uuid,
                    }
                file_access_progress = {
                    "source_id": message["id"],
                    "fingerprint": fingerprint,
                    "offset": end,
                }
        normalized_reactions = {
            (identities[item["user_id"]], emoji_name(item))
            for item in message["reactions"]
        }
        counts: dict[str, int] = {}
        reaction_users: dict[str, list[str]] = {}
        for user_uuid, emoji in sorted(normalized_reactions):
            counts[emoji] = counts.get(emoji, 0) + 1
            reaction_users.setdefault(emoji, []).append(user_uuid)
            if prior is None:
                reactions.append(
                    {
                        "uuid": str(
                            sys_uuid.uuid5(
                                sys_uuid.UUID(message_uuid),
                                f"reaction:{user_uuid}:{emoji}",
                            )
                        ),
                        "message_uuid": message_uuid,
                        "placement_uuid": placement_uuid,
                        "user_uuid": user_uuid,
                        "emoji_name": emoji,
                    }
                )
        result.append(
            {
                "uuid": message_uuid,
                "placement_uuid": placement_uuid,
                "stream_uuid": stream_uuid,
                "topic_uuid": topic_uuid,
                "author_uuid": identities[message["sender_id"]],
                "payload": {"kind": "markdown", "content": content},
                "provider_id": str(message["id"]),
                "account_uuid": str(route["account_uuid"]),
                "created_at": timestamp.isoformat(),
                "source_hash": message["hash"],
                "source": source_for(message, route, include_message=True),
                "provider": {
                    "kind": "zulip",
                    "provider_realm_uuid": str(batch.provider_realm_uuid),
                    "external_id": str(message["id"]),
                    "chat_key": key,
                },
                "reactions": counts,
                "reaction_users": {
                    emoji: users
                    for emoji, users in reaction_users.items()
                    if len(users) <= reaction_user_limit
                },
            }
        )
        for access in observers:
            user_uuid = identities[access["user_id"]]
            states.append(
                {
                    "uuid": str(
                        sys_uuid.uuid5(sys_uuid.UUID(placement_uuid), user_uuid)
                    ),
                    "placement_uuid": placement_uuid,
                    "message_uuid": message_uuid,
                    "stream_uuid": stream_uuid,
                    "topic_uuid": topic_uuid,
                    "user_uuid": user_uuid,
                    "read": access["read"],
                    "starred": access["starred"],
                    "mentioned": f"](urn:user:{user_uuid})" in content,
                    "role": "author"
                    if user_uuid == identities[message["sender_id"]]
                    else "member",
                }
            )
    encoded = {
        key: contract.encode(value).decode()
        for key, value in {
            "messages": result,
            "topics": list(topics.values()),
            "states": states,
            "reactions": reactions,
            "files": list(file_records.values()),
            "file_access": list(accesses.values()),
        }.items()
    }
    used = {message["author_uuid"] for message in result}
    used.update(
        row["user_uuid"] for row in states + reactions + list(accesses.values())
    )
    for message in result:
        used.update(
            re.findall(r"urn:user:([0-9a-f-]{36})", message["payload"]["content"])
        )
    used_identities = {key: value for key, value in identities.items() if value in used}
    return PreparedPart(
        result,
        list(topics.values()),
        states,
        reactions,
        list(file_records.values()),
        list(accesses.values()),
        {key: existing.get(key) for key in [m["id"] for m in messages]},
        used_identities,
        [message["id"] for message in messages],
        encoded,
        file_access_progress,
    )
