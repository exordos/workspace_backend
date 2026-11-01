# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Normalize one history page, then merge its relations with set-based SQL."""

import asyncio
import datetime
import hashlib
import logging
import typing
import urllib.parse
import uuid

from psycopg.types import json as pg_json

from workspace.bridges import history as common_history
from workspace.bridges.zulip import (
    incoming_operations,
    message_conversion,
    outgoing_operations,
)
from workspace.common import emoji
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import base

LOG = logging.getLogger(__name__)


execute = common_history.execute
stage = common_history.stage
merge = common_history.merge


REQUEST_SQL = """
    INSERT INTO zulip_history_accounts (
        external_account_uuid,project_id,provider_uuid,cutoff_at,cutoff_id,
        before_id,registration,subscribed_streams
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT(external_account_uuid) DO UPDATE SET
        registration=EXCLUDED.registration,subscribed_streams=EXCLUDED.subscribed_streams,catalog_position=0,
        cutoff_at=EXCLUDED.cutoff_at,cutoff_id=EXCLUDED.cutoff_id,
        before_id=EXCLUDED.before_id,finished=false,next_attempt_at=now(),error=NULL
"""
TRANSACTION_SQL = """
    SELECT set_config('workspace.zulip_incoming','on',true),
           set_config('workspace.zulip_history','on',true),
           set_config('workspace.batch_flags','on',true)
"""
PEOPLE_SQL = """
    SELECT account.external_account_id,account.owner_user_uuid,users.first_name
    FROM m_external_accounts_v2 account
    JOIN m_workspace_users users ON users.uuid=account.owner_user_uuid
    WHERE account.project_id=%s AND account.provider_uuid=%s
"""
STREAMS_SQL = """
    SELECT stream.*,member.active,member.membership_generation
    FROM messenger_streams stream
    LEFT JOIN messenger_stream_bindings member
      ON member.project_id=stream.project_id AND member.stream_uuid=stream.uuid
     AND member.user_uuid=%s
    WHERE stream.project_id=%s AND stream.provider_uuid=%s
"""
MESSAGES_SQL = """
    WITH scope(project_id,provider_uuid,identities) AS (
        VALUES (%s::uuid,%s::uuid,%s::text[])
    ), matches AS (
        SELECT message.project_id,message.uuid,message.provider_identity
        FROM scope JOIN messenger_messages message ON message.project_id=scope.project_id
            AND message.provider_uuid=scope.provider_uuid
            AND message.provider_identity=ANY(scope.identities)
        UNION
        -- A sent local message can have a durable delivery receipt before its
        -- realtime echo fills the canonical provider identity.
        SELECT message.project_id,message.uuid,delivery.zulip_message_id::text
        FROM scope JOIN messenger_providers provider ON provider.uuid=scope.provider_uuid
        JOIN bridge_zulip_v5_deliveries delivery ON delivery.project_id=scope.project_id
            AND delivery.hostname=provider.hostname
            AND delivery.zulip_message_id=ANY(scope.identities::bigint[])
        JOIN messenger_messages message ON message.project_id=delivery.project_id
            AND message.uuid=delivery.message_uuid AND message.provider_uuid=scope.provider_uuid
    )
    SELECT matches.provider_identity,matches.uuid,placement.uuid AS placement_uuid,
           placement.stream_uuid
    FROM matches
    LEFT JOIN messenger_message_placements placement
      ON placement.project_id=matches.project_id AND placement.message_uuid=matches.uuid
"""
FILES_SQL = """
    SELECT * FROM m_workspace_files WHERE project_id=%s AND provider_uuid=%s
      AND provider_identity=ANY(%s::text[])
"""


def catalog_events(registration: dict) -> list[dict]:
    return (
        [
            {"type": "realm_user", "op": "add", "person": item}
            for key in ("realm_users", "realm_non_active_users", "cross_realm_bots")
            for item in registration.get(key, [])
        ]
        + [
            {"type": "subscription", "op": "add", "subscriptions": [item]}
            for item in registration.get("subscriptions", [])
        ]
        + [
            {"type": "user_topic", **item}
            for item in registration.get("user_topics", [])
        ]
    )


class Operations(incoming_operations.Operations):
    emit_public_event = False

    async def current(self, connection: typing.Any, account: dict) -> bool:
        # Reconnection replaces the job. An old in-flight page must not move
        # the replacement job's cursor or restore a disconnected membership.
        return bool(
            await incoming_operations.rows(
                connection,
                """
            SELECT 1 FROM m_external_accounts_v2 account
            JOIN zulip_history_accounts job ON job.external_account_uuid=account.uuid
            WHERE account.uuid=%s AND account.project_id=%s AND account.provider_uuid=%s
              AND account.owner_user_uuid=%s AND account.external_account_id=%s
              AND NOT account.disabled AND account.settings=%s AND job.cutoff_at=%s
            """,
                (
                    account["uuid"],
                    account["project_id"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    account["external_account_id"],
                    pg_json.Jsonb(account["settings"]),
                    account["cutoff_at"],
                ),
            )
        )

    async def ensure(self, account: dict, spec: dict) -> None:
        try:
            prepared = await self.prepare_dependency(account, spec)
        except incoming_operations.MissingDependency as missing:
            await self.ensure(account, missing.spec)
            return await self.ensure(account, spec)
        async with self.db.transaction() as connection:
            if not await self.current(connection, account):
                raise asyncio.CancelledError
            await connection.execute(TRANSACTION_SQL)
            await self.ensure_dependency(connection, account, prepared)

    async def prepare_batch(self, account: dict, messages: list[dict]) -> dict:
        subscriptions = account["subscribed_streams"]
        if subscriptions is not None:
            messages = [
                item
                for item in messages
                if item["type"] != "stream" or item["stream_id"] in subscriptions
            ]
        scope = (account["project_id"], account["provider_uuid"])
        uuid_strings: dict = {}
        owner = incoming_operations._uuid_text(uuid_strings, account["owner_user_uuid"])
        provider = incoming_operations._uuid_text(
            uuid_strings, account["provider_uuid"]
        )
        people = await self.db.sql(PEOPLE_SQL, scope)
        users = {row["external_account_id"]: row["owner_user_uuid"] for row in people}
        needed = {str(item["sender_id"]) for item in messages}
        for item in messages:
            needed.update(str(r["user_id"]) for r in item.get("reactions", []))
            if item["type"] != "stream":
                needed.update(str(r["id"]) for r in item["display_recipient"])
        refs = incoming_operations.references(messages)
        needed.update(refs["people"])
        for remote in sorted(needed - users.keys()):
            await self.ensure(account, {"kind": "user", "identity": remote})
        if needed - users.keys():
            people = await self.db.sql(PEOPLE_SQL, scope)
            users = {
                row["external_account_id"]: row["owner_user_uuid"] for row in people
            }
        streams = await self.db.sql(STREAMS_SQL, (account["owner_user_uuid"], *scope))
        stream_map = {row["provider_identity"]: row for row in streams}
        for item in messages:
            remote = incoming_operations.stream_identity(account, item)
            if remote in stream_map and (
                item["type"] == "stream" or stream_map[remote]["active"] is not None
            ):
                continue
            spec = {
                "kind": "stream_binding",
                "identity": remote,
                "stream_identity": remote,
                "user": account["external_account_id"],
            }
            if item["type"] != "stream":
                spec["recipients"] = item["display_recipient"]
            await self.ensure(account, spec)
            streams = await self.db.sql(
                STREAMS_SQL, (account["owner_user_uuid"], *scope)
            )
            stream_map = {row["provider_identity"]: row for row in streams}
        # An unsubscribed account must not regain access from old history.
        messages = [
            item
            for item in messages
            if stream_map[incoming_operations.stream_identity(account, item)]["active"]
        ]
        topics = await self.db.sql(
            """
            SELECT topic.*,stream.name AS stream_name FROM messenger_topics topic
            JOIN messenger_streams stream ON stream.uuid=topic.stream_uuid AND stream.project_id=topic.project_id
            WHERE topic.project_id=%s AND topic.provider_uuid=%s AND topic.stream_uuid=ANY(%s::uuid[])
        """,
            (*scope, [row["uuid"] for row in streams]),
        )
        known = await self.db.sql(
            MESSAGES_SQL, (*scope, [str(m["id"]) for m in messages])
        )
        identities = {row["provider_identity"]: row["uuid"] for row in known}
        placements = {
            (row["uuid"], row["stream_uuid"]): row["placement_uuid"] for row in known
        }
        public_ids = {}
        for item in messages:
            remote = str(item["id"])
            identifier = identities.setdefault(
                remote,
                uuid.uuid5(
                    account["project_id"],
                    f"{provider}:message:{remote}",
                ),
            )
            stream_uuid = stream_map[
                incoming_operations.stream_identity(account, item)
            ]["uuid"]
            public_ids[remote] = placements.get(
                (identifier, stream_uuid)
            ) or uuid.uuid5(
                identifier, incoming_operations._uuid_text(uuid_strings, stream_uuid)
            )
        prepared = {"catalog": people, "streams": streams, "topics": topics}
        prepared["references"] = await self.db.sql(
            incoming_operations.SELECT_MESSAGE_REFERENCES_SQL,
            (
                account["project_id"],
                account["owner_user_uuid"],
                refs["messages"],
                account["provider_uuid"],
            ),
        )
        # Every page member will be visible after this merge, including quote
        # targets which have not reached the working tables yet.
        prepared["references"].extend(
            {"remote_id": remote, "uuid": placement}
            for remote, placement in public_ids.items()
        )
        prepared["quoted_messages"] = {str(item["id"]): item for item in messages}
        files = await self.prepare_files(account, messages, stream_map, users)
        context_base = incoming_operations.conversion_context_base(
            account, prepared, uuid_strings=uuid_strings
        )
        batch: dict = {
            table: []
            for table in (
                "messenger_topics",
                "messenger_user_topic_bindings",
                "messenger_message_contents",
                "messenger_messages",
                "messenger_message_placements",
                "messenger_user_message_bindings",
                "messenger_user_message_unreads",
                "messenger_user_message_stars",
                "messenger_user_message_mentions",
                "messenger_message_reaction_facts",
                "m_workspace_files",
                "zulip_entity_links",
            )
        }
        topic_map = {row["provider_identity"]: row for row in topics}
        named_topics: dict[tuple, dict] = {}
        for row in topics:
            named_topics.setdefault(
                (row["stream_uuid"], row["name"], row["is_done"]), row
            )
        new_topics: dict[str, dict] = {}
        personal_topics: dict[object, dict] = {}
        content_pages: dict[tuple, dict] = {}
        snapshots = await self.db.sql(
            """
            SELECT DISTINCT ON (delivery.message_uuid,contents.provider_content)
                delivery.message_uuid,contents.provider_content,contents.workspace_content
            FROM bridge_zulip_v5_deliveries delivery
            JOIN bridge_zulip_v5_content_snapshots snapshot ON snapshot.delivery_uuid=delivery.uuid
            JOIN messenger_message_contents contents ON contents.uuid=snapshot.content_uuid
            WHERE delivery.project_id=%s AND delivery.message_uuid=ANY(%s::uuid[])
            ORDER BY delivery.message_uuid,contents.provider_content,snapshot.created_at DESC
        """,
            (account["project_id"], list(identities.values())),
        )
        original = {
            (r["message_uuid"], r["provider_content"]): r["workspace_content"]
            for r in snapshots
        }
        for item in messages:
            remote = incoming_operations.stream_identity(account, item)
            stream = stream_map[remote]
            subject = item["subject"] if item["type"] == "stream" else ""
            topic_identity = f"{remote}:{subject}"
            topic = topic_map.get(topic_identity)
            if topic is None:
                topic = named_topics.get(
                    (
                        stream["uuid"],
                        subject.removeprefix("✔ ") or "zulip",
                        subject.startswith("✔ "),
                    )
                )
            if topic is None:
                topic = new_topics.setdefault(
                    topic_identity,
                    {
                        "uuid": uuid.uuid5(stream["uuid"], subject.lower()),
                        "project_id": account["project_id"],
                        "stream_uuid": stream["uuid"],
                        "name": subject.removeprefix("✔ ") or "zulip",
                        "is_done": subject.startswith("✔ "),
                        **incoming_operations.origin(account, topic_identity),
                    },
                )
            personal_topics[topic["uuid"]] = {
                "uuid": uuid.uuid5(topic["uuid"], owner),
                "project_id": account["project_id"],
                "user_uuid": account["owner_user_uuid"],
                "topic_uuid": topic["uuid"],
                **incoming_operations.origin(
                    account, f"{topic_identity}:{account['external_account_id']}"
                ),
            }
            identifier = identities[str(item["id"])]
            placement = public_ids[str(item["id"])]
            context = incoming_operations.conversion_context(
                account, item, stream, topic, placement, prepared, base=context_base
            )
            for target, path in item.get("_files", []):
                record = files[(stream["uuid"], path)]
                if "missing" in record:
                    reason = {
                        404: "deleted_files",
                        "path_too_long": "oversized_file_paths",
                    }.get(record["missing"], "inaccessible_files")
                    context.setdefault(reason, set()).add(target)
                else:
                    context["files"][target] = message_conversion.file_urn(
                        incoming_operations._uuid_text(uuid_strings, record["uuid"]),
                        record["content_type"],
                    )
            payload = original.get((identifier, item["content"]))
            if payload is None:
                payload = message_conversion.zulip_to_workspace_cached(item, context)[0]
            content_key = (str(payload), item["content"])
            page = content_pages.setdefault(
                content_key,
                {
                    "uuid": uuid.uuid4(),
                    "workspace_content": payload,
                    "provider_content": item["content"],
                },
            )
            created = datetime.datetime.fromtimestamp(
                item["timestamp"], datetime.timezone.utc
            )
            edited = datetime.datetime.fromtimestamp(
                item.get("last_edit_timestamp") or item["timestamp"],
                datetime.timezone.utc,
            )
            reactions: dict[str, int] = {}
            reaction_users: dict[str, list] = {}
            for reaction in item.get("reactions", []):
                name = (
                    emoji.name_for_code(reaction["emoji_code"], reaction["emoji_name"])
                    if reaction["reaction_type"] == "unicode_emoji"
                    else reaction["emoji_name"]
                )
                identity = f"{item['id']}:{reaction['user_id']}:{reaction['reaction_type']}:{reaction['emoji_code']}"
                actor = users[str(reaction["user_id"])]
                fact = {
                    "uuid": uuid.uuid5(identifier, identity),
                    "project_id": account["project_id"],
                    "canonical_message_uuid": identifier,
                    "placement_uuid": placement,
                    "user_uuid": actor,
                    "emoji_name": name,
                    **incoming_operations.origin(account, identity),
                }
                batch["messenger_message_reaction_facts"].append(fact)
                reactions[name] = reactions.get(name, 0) + 1
                reaction_users.setdefault(name, []).append(
                    incoming_operations._uuid_text(uuid_strings, actor)
                )
                batch["zulip_entity_links"].append(
                    {
                        "project_id": account["project_id"],
                        "entity_type": "reaction",
                        "entity_uuid": fact["uuid"],
                        "provider_uuid": account["provider_uuid"],
                        "external_account_uuid": account["uuid"],
                        "provider_identity": identity,
                        "data": {
                            k: reaction[k]
                            for k in ("emoji_name", "emoji_code", "reaction_type")
                        },
                    }
                )
            batch["messenger_messages"].append(
                {
                    "uuid": identifier,
                    "project_id": account["project_id"],
                    "author_uuid": users[str(item["sender_id"])],
                    "content_uuid": page["uuid"],
                    "created_at": created.replace(tzinfo=None),
                    "content_updated_at": edited.replace(tzinfo=None),
                    "reactions": reactions,
                    "reaction_users": {
                        k: v
                        for k, v in reaction_users.items()
                        if len(v) <= self.reaction_user_limit
                    },
                    **incoming_operations.origin(account, str(item["id"])),
                }
            )
            batch["messenger_message_placements"].append(
                {
                    "uuid": placement,
                    "project_id": account["project_id"],
                    "message_uuid": identifier,
                    "stream_uuid": stream["uuid"],
                    "topic_uuid": topic["uuid"],
                    "created_at": created.replace(tzinfo=None),
                }
            )
            batch["messenger_user_message_bindings"].append(
                {
                    "uuid": uuid.uuid5(placement, owner),
                    "project_id": account["project_id"],
                    "placement_uuid": placement,
                    "user_uuid": account["owner_user_uuid"],
                }
            )
            flag = {
                "project_id": account["project_id"],
                "user_uuid": account["owner_user_uuid"],
                "placement_uuid": placement,
                "stream_uuid": stream["uuid"],
                "topic_uuid": topic["uuid"],
            }
            if "read" not in item["flags"]:
                batch["messenger_user_message_unreads"].append(flag)
            if "starred" in item["flags"]:
                batch["messenger_user_message_stars"].append(flag)
            if f"](urn:user:{owner})" in payload["content"].lower():
                batch["messenger_user_message_mentions"].append(flag)
        batch["messenger_topics"] = list(new_topics.values())
        batch["messenger_user_topic_bindings"] = list(personal_topics.values())
        batch["messenger_message_contents"] = list(content_pages.values())
        for (stream_uuid, path), record in files.items():
            if "missing" in record:
                continue
            batch["m_workspace_files"].append(record)
            batch["zulip_entity_links"].append(
                {
                    "project_id": account["project_id"],
                    "entity_type": "file",
                    "entity_uuid": record["uuid"],
                    "provider_uuid": account["provider_uuid"],
                    "external_account_uuid": account["uuid"],
                    "provider_identity": path,
                    "data": {"account_identity": account["external_account_id"]},
                }
            )
        # Keep the exact resolver snapshot used above. Reading a newer catalog
        # after merge could otherwise certify a payload prepared with old mappings.
        batch["_context"] = {
            "catalog": people,
            "streams": streams,
            # New topics are used for their own placements, but cross-message
            # references were converted with the pre-merge resolver snapshot.
            "topics": topics,
            "references": prepared["references"],
            "files": [record for record in files.values() if "missing" not in record],
            "edited": {
                str(row["uuid"]): row["content_updated_at"]
                for row in batch["messenger_messages"]
            },
        }
        return batch

    async def prepare_files(
        self, account: dict, messages: list, streams: dict, users: dict
    ) -> dict:
        targets = {}
        for item in messages:
            stream = streams[incoming_operations.stream_identity(account, item)]["uuid"]
            item["_files"] = []
            for target in message_conversion.get_zulip_files(item, {}):
                endpoint = message_conversion.parse_url(
                    target, account["settings"]["server_url"]
                )
                if endpoint is None or endpoint.hostname != account["hostname"]:
                    continue
                path = message_conversion.zulip_upload_path(endpoint.path)
                if path is None:
                    continue
                item["_files"].append((target, path))
                targets[(stream, path)] = item
        existing = await self.db.sql(
            FILES_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                list({path for _, path in targets}),
            ),
        )
        by_path = {
            (row["stream_uuid"], row["provider_identity"]): row for row in existing
        }
        files: dict[tuple, dict] = {}
        blobs: dict[tuple, dict] = {}
        downloaded: dict[str, dict] = {}
        for (stream, path), item in targets.items():
            if not base.FileProviderIdentity().validate(path):
                files[(stream, path)] = {"missing": "path_too_long"}
                continue
            known = by_path.get((stream, path))
            if known is None:
                if path not in downloaded:
                    try:
                        content, content_type = await self.http.download(account, path)
                    except outgoing_operations.ApiError as error:
                        if error.status not in (403, 404):
                            raise
                        downloaded[path] = {"missing": error.status}
                    else:
                        digest = hashlib.sha256(content).hexdigest()
                        key = (digest, len(content))
                        if key not in blobs:
                            found = await self.db.sql(
                                """
                                SELECT storage_type,storage_id,storage_object_id
                                FROM m_workspace_files WHERE project_id=%s AND hash=%s
                                  AND size_bytes=%s AND storage_type='s3' LIMIT 1
                            """,
                                (account["project_id"], *key),
                            )
                            if found:
                                blobs[key] = found[0]
                            else:
                                object_id = (
                                    f"history/sha256/{account['project_id']}/{digest}"
                                )
                                stored = await asyncio.to_thread(
                                    file_storage.save_workspace_file,
                                    uuid.uuid4(),
                                    content,
                                    storage_object_id=object_id,
                                )
                                blobs[key] = {
                                    "storage_type": stored.storage_type,
                                    "storage_id": stored.storage_id,
                                    "storage_object_id": stored.storage_object_id,
                                }
                        downloaded[path] = {
                            "hash": digest,
                            "size_bytes": len(content),
                            "content_type": content_type,
                            **blobs[key],
                        }
                if "missing" in downloaded[path]:
                    files[(stream, path)] = downloaded[path]
                    continue
                known = {
                    "uuid": uuid.uuid5(
                        stream, f"{account['provider_uuid']}:file:{path}"
                    ),
                    "project_id": account["project_id"],
                    "stream_uuid": stream,
                    "user_uuid": users[str(item["sender_id"])],
                    "name": urllib.parse.unquote(path.rsplit("/", 1)[1]),
                    "created_at": datetime.datetime.fromtimestamp(
                        item["timestamp"], datetime.timezone.utc
                    ),
                    **downloaded[path],
                    **incoming_operations.origin(account, path),
                }
                await asyncio.to_thread(
                    file_storage.save_workspace_file_metadata,
                    file_storage.WorkspaceFileMetadata(
                        uuid=known["uuid"],
                        project_id=known["project_id"],
                        stream_uuid=stream,
                        owner_uuid=known["user_uuid"],
                        name=known["name"],
                        description="",
                        content_type=known["content_type"],
                        size_bytes=known["size_bytes"],
                        sha256=known["hash"],
                        created_at=known["created_at"],
                    ),
                    known["storage_type"],
                )
            columns = (
                "uuid",
                "project_id",
                "stream_uuid",
                "user_uuid",
                "name",
                "created_at",
                "hash",
                "size_bytes",
                "content_type",
                "storage_type",
                "storage_id",
                "storage_object_id",
                "source_name",
                "provider_uuid",
                "provider_identity",
                "status",
            )
            files[(stream, path)] = {key: known[key] for key in columns}
        return files

    async def merge_batch(
        self, connection: typing.Any, account: dict, batch: dict
    ) -> None:
        await execute(connection, "setup", TRANSACTION_SQL)
        for table, values in batch.items():
            if table != "_context":
                await stage(connection, table, values)
        for table in (
            "messenger_topics",
            "messenger_user_topic_bindings",
            "m_workspace_files",
        ):
            if batch[table]:
                await merge(
                    connection,
                    table,
                    list(batch[table][0]),
                    key=("uuid",)
                    if table == "m_workspace_files"
                    else ("project_id", "uuid"),
                )
        await common_history.merge_messages(
            connection, batch, account["provider_uuid"], "zulip"
        )
        await self.merge_flags(connection)
        await execute(
            connection,
            "delete.reactions",
            """
            DELETE FROM messenger_message_reaction_facts dst USING history_messenger_messages message
            WHERE dst.project_id=message.project_id AND dst.canonical_message_uuid=message.uuid
              AND dst.source_name='zulip' AND dst.provider_uuid=%s AND NOT EXISTS (
                SELECT 1 FROM history_messenger_message_reaction_facts src
                WHERE src.project_id=dst.project_id AND src.canonical_message_uuid=dst.canonical_message_uuid
                  AND src.user_uuid=dst.user_uuid AND src.emoji_name=dst.emoji_name)
        """,
            (account["provider_uuid"],),
        )
        if batch["messenger_message_reaction_facts"]:
            await merge(
                connection,
                "messenger_message_reaction_facts",
                list(batch["messenger_message_reaction_facts"][0]),
                key=("project_id", "canonical_message_uuid", "user_uuid", "emoji_name"),
            )
        await execute(
            connection,
            "update.reaction_snapshot",
            """
            WITH counts AS (
                SELECT message.project_id,message.uuid,fact.emoji_name,count(*) AS total,
                       array_agg(fact.user_uuid ORDER BY fact.user_uuid) AS users
                FROM history_messenger_messages message
                JOIN messenger_message_reaction_facts fact ON fact.project_id=message.project_id
                    AND fact.canonical_message_uuid=message.uuid
                GROUP BY message.project_id,message.uuid,fact.emoji_name
            ), snapshots AS (
                SELECT message.project_id,message.uuid,
                       COALESCE(jsonb_object_agg(counts.emoji_name,counts.total)
                           FILTER(WHERE counts.emoji_name IS NOT NULL),'{}') AS reactions,
                       COALESCE(jsonb_object_agg(counts.emoji_name,counts.users)
                           FILTER(WHERE counts.total<=%s),'{}') AS reaction_users
                FROM history_messenger_messages message
                LEFT JOIN counts ON counts.project_id=message.project_id AND counts.uuid=message.uuid
                GROUP BY message.project_id,message.uuid
            )
            UPDATE messenger_messages dst SET reactions=src.reactions,
                reaction_users=src.reaction_users,updated_at=now()
            FROM snapshots src WHERE dst.project_id=src.project_id AND dst.uuid=src.uuid
              AND (dst.reactions,dst.reaction_users) IS DISTINCT FROM (src.reactions,src.reaction_users)
        """,
            (self.reaction_user_limit,),
        )
        # Existing native/provider facts can already own this user/emoji key.
        await execute(
            connection,
            "remap.reactions",
            """
            UPDATE history_zulip_entity_links link SET entity_uuid=dst.uuid
            FROM history_messenger_message_reaction_facts src
            JOIN messenger_message_reaction_facts dst ON dst.project_id=src.project_id
                AND dst.canonical_message_uuid=src.canonical_message_uuid
                AND dst.user_uuid=src.user_uuid AND dst.emoji_name=src.emoji_name
            WHERE link.entity_type='reaction' AND link.entity_uuid=src.uuid
        """,
        )
        if batch["zulip_entity_links"]:
            await merge(
                connection,
                "zulip_entity_links",
                list(batch["zulip_entity_links"][0]),
                key=(
                    "entity_type",
                    "entity_uuid",
                    "provider_uuid",
                    "external_account_uuid",
                ),
            )
        await self.queue_counters(connection)

    async def merge_flags(
        self,
        connection: typing.Any,
        suffixes: tuple[str, ...] = ("unreads", "stars", "mentions"),
    ) -> None:
        await common_history.merge_flags(connection, suffixes)

    async def queue_counters(self, connection: typing.Any) -> None:
        await common_history.queue_counters(connection)

    async def prepare_flags(self, account: dict, messages: list[dict]) -> dict | None:
        records = await self.db.sql(
            """
            SELECT message.provider_identity,access.*,placement.message_uuid,placement.stream_uuid,placement.topic_uuid
            FROM messenger_messages message
            JOIN LATERAL (
                SELECT * FROM messenger_message_placements
                WHERE project_id=message.project_id AND message_uuid=message.uuid OFFSET 0
            ) placement ON true
            JOIN LATERAL (
                SELECT * FROM messenger_user_message_bindings
                WHERE project_id=placement.project_id AND placement_uuid=placement.uuid AND user_uuid=%s LIMIT 1
            ) access ON true
            JOIN LATERAL (
                SELECT 1 FROM messenger_stream_bindings
                WHERE project_id=placement.project_id AND stream_uuid=placement.stream_uuid
                  AND user_uuid=access.user_uuid AND active LIMIT 1
            ) member ON true
            WHERE message.project_id=%s AND message.provider_uuid=%s
                AND message.provider_identity=ANY(%s::text[])
        """,
            (
                account["owner_user_uuid"],
                account["project_id"],
                account["provider_uuid"],
                [str(item["id"]) for item in messages],
            ),
        )
        if len(records) != len(messages):
            return None
        by_id = {row["provider_identity"]: row for row in records}
        batch: dict[str, list] = {
            table: []
            for table in (
                "messenger_message_placements",
                "messenger_user_message_bindings",
                "messenger_user_message_unreads",
                "messenger_user_message_stars",
            )
        }
        for item in messages:
            record = by_id[str(item["id"])]
            batch["messenger_message_placements"].append(
                {
                    key: record[key]
                    for key in (
                        "project_id",
                        "message_uuid",
                        "stream_uuid",
                        "topic_uuid",
                    )
                }
                | {"uuid": record["placement_uuid"]}
            )
            batch["messenger_user_message_bindings"].append(
                {
                    key: record[key]
                    for key in ("uuid", "project_id", "placement_uuid", "user_uuid")
                }
            )
            flag = {
                key: record[key]
                for key in (
                    "project_id",
                    "placement_uuid",
                    "user_uuid",
                    "stream_uuid",
                    "topic_uuid",
                )
            }
            if "read" not in item["flags"]:
                batch["messenger_user_message_unreads"].append(flag)
            if "starred" in item["flags"]:
                batch["messenger_user_message_stars"].append(flag)
        return batch

    async def merge_personal_flags(self, connection: typing.Any, batch: dict) -> None:
        await execute(connection, "setup", TRANSACTION_SQL)
        for table, values in batch.items():
            await stage(connection, table, values)
        await self.merge_flags(connection, ("unreads", "stars"))
        await self.queue_counters(connection)
