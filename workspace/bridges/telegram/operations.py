# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Project-scoped Telegram imports into the canonical Messenger model."""

import datetime
import hashlib
import typing
import uuid

from workspace.bridges import history
from workspace.bridges import messenger
from workspace.bridges.telegram import conversion
from workspace.common import emoji


def origin(account: dict, identity: str) -> dict:
    return {
        "source_name": "telegram",
        "provider_uuid": account["provider_uuid"],
        "provider_identity": identity,
        "status": "active",
    }


class Operations(messenger.Operations):
    source_name = "telegram"

    def __init__(self, db: typing.Any) -> None:
        self.db = db

    async def user(
        self, connection: typing.Any, account: dict, remote: int, name: str = ""
    ) -> uuid.UUID:
        if str(remote) == account["external_account_id"]:
            identifier = account["owner_user_uuid"]
        else:
            found = await messenger.rows(
                connection,
                "SELECT owner_user_uuid FROM m_external_accounts_v2 WHERE project_id=%s AND provider='telegram' "
                "AND provider_uuid=%s AND external_account_id=%s ORDER BY uuid LIMIT 1",
                (account["project_id"], account["provider_uuid"], str(remote)),
            )
            identifier = (
                found[0]["owner_user_uuid"]
                if found
                else uuid.uuid5(account["provider_uuid"], f"user:{remote}")
            )
            if not found:
                await connection.execute(
                    "INSERT INTO m_workspace_users(uuid,username,source,status,first_name,avatar,created_at,updated_at) "
                    "VALUES(%s,%s,'telegram','offline',%s,%s,now(),now()) ON CONFLICT(uuid) DO NOTHING",
                    (
                        identifier,
                        f"telegram-{identifier}",
                        name or f"Telegram {remote}",
                        "urn:gravatar:"
                        + hashlib.md5(str(identifier).encode()).hexdigest(),
                    ),
                )
                await connection.execute(
                    "INSERT INTO m_external_accounts_v2(uuid,project_id,owner_user_uuid,provider,provider_uuid,external_account_id,status,settings,created_at,updated_at) "
                    "VALUES(%s,%s,%s,'telegram',%s,%s,'active','{}',now(),now()) ON CONFLICT DO NOTHING",
                    (
                        uuid.uuid5(
                            account["project_id"],
                            f"{account['provider_uuid']}:{remote}",
                        ),
                        account["project_id"],
                        identifier,
                        account["provider_uuid"],
                        str(remote),
                    ),
                )
                linked = await messenger.rows(
                    connection,
                    "SELECT owner_user_uuid FROM m_external_accounts_v2 WHERE project_id=%s AND provider='telegram' AND provider_uuid=%s AND external_account_id=%s",
                    (account["project_id"], account["provider_uuid"], str(remote)),
                )
                identifier = linked[0]["owner_user_uuid"]
        await connection.execute(
            "SELECT messenger_v2_register_project_user(%s,%s)",
            (account["project_id"], identifier),
        )
        return identifier

    async def person(
        self, connection: typing.Any, account: dict, person: typing.Any
    ) -> uuid.UUID:
        identifier = await self.user(
            connection,
            account,
            person.id,
            " ".join(value for value in (person.first_name, person.last_name) if value),
        )
        values = (
            person.username or f"telegram-{identifier}",
            person.first_name or f"Telegram {person.id}",
            person.last_name or None,
            bool(person.deleted),
        )
        changed = await messenger.rows(
            connection,
            """
            UPDATE m_workspace_users SET username=%s,first_name=%s,last_name=%s,disabled=%s,updated_at=now()
            WHERE uuid=%s AND source='telegram'
              AND (username,first_name,last_name,disabled) IS DISTINCT FROM (%s,%s,%s,%s)
            RETURNING *
        """,
            (*values, identifier, *values),
        )
        if changed:
            await self.publish(
                connection,
                account,
                "user.updated",
                identifier,
                changed,
                recipients=[account["owner_user_uuid"]],
            )
        return identifier

    async def stream(self, connection: typing.Any, account: dict, peer: dict) -> dict:
        remote = conversion.stream_identity(
            account["external_account_id"], peer["peer_key"]
        )
        identifier = await self.identity(
            account, "stream", remote, connection=connection
        )
        old = await messenger.rows(
            connection,
            messenger.SELECT_ROW_SQL.format(table="messenger_streams"),
            (account["project_id"], identifier),
        )
        direct = peer["peer_kind"] == "user"
        direct_user = (
            await self.user(
                connection, account, int(peer["peer_key"].split(":")[1]), peer["title"]
            )
            if direct
            else None
        )
        changed = await self.save(
            connection,
            "messenger_streams",
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "name": peer["title"],
                "owner_uuid": old[0]["owner_uuid"]
                if old
                else account["owner_user_uuid"],
                "private": True,
                "announcement_only": peer["broadcast"],
                "direct_user_uuid": direct_user,
                **origin(account, remote),
            },
        )
        stream = changed or old[0]
        previous_binding = await messenger.rows(
            connection,
            "SELECT notification_mode FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
            (account["project_id"], identifier, account["owner_user_uuid"]),
        )
        binding = await self.save(
            connection,
            "messenger_stream_bindings",
            {
                "uuid": uuid.uuid5(identifier, str(account["owner_user_uuid"])),
                "project_id": account["project_id"],
                "stream_uuid": identifier,
                "user_uuid": account["owner_user_uuid"],
                "who_uuid": stream["owner_uuid"],
                "active": True,
                "can_send": peer["can_send"],
                "notification_mode": previous_binding[0]["notification_mode"]
                if previous_binding
                else "muted"
                if peer["notification_muted"]
                else "all_messages",
                **origin(account, f"{remote}:user:{account['external_account_id']}"),
            },
        )
        await connection.execute(
            "UPDATE telegram_peer_state SET stream_uuid=%s WHERE external_account_uuid=%s AND peer_key=%s",
            (identifier, account["uuid"], peer["peer_key"]),
        )
        if changed or binding:
            await self.snapshot(
                connection,
                account,
                "stream",
                identifier,
                "created" if not old else "updated",
                identifier,
            )
        return stream

    async def topic(
        self,
        connection: typing.Any,
        account: dict,
        stream: dict,
        topic_id: int,
        title: str = "",
    ) -> dict:
        remote = conversion.topic_identity(stream["provider_identity"], topic_id)
        identifier = await self.identity(
            account, "topic", remote, connection=connection
        )
        old = await messenger.rows(
            connection,
            messenger.SELECT_ROW_SQL.format(table="messenger_topics"),
            (account["project_id"], identifier),
        )
        changed = await self.save(
            connection,
            "messenger_topics",
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "stream_uuid": stream["uuid"],
                "name": title
                or (
                    (old[0]["name"] if old else f"Topic {topic_id}")
                    if topic_id
                    else "telegram"
                ),
                **origin(account, remote),
            },
        )
        topic = changed or old[0]
        binding = await self.save(
            connection,
            "messenger_user_topic_bindings",
            {
                "uuid": uuid.uuid5(identifier, str(account["owner_user_uuid"])),
                "project_id": account["project_id"],
                "topic_uuid": identifier,
                "user_uuid": account["owner_user_uuid"],
                **origin(account, f"{remote}:user:{account['external_account_id']}"),
            },
        )
        if not topic_id and not stream["default_topic_uuid"]:
            await connection.execute(
                "UPDATE messenger_streams SET default_topic_uuid=%s WHERE project_id=%s AND uuid=%s",
                (identifier, account["project_id"], stream["uuid"]),
            )
            stream["default_topic_uuid"] = identifier
        if changed or binding:
            await self.snapshot(
                connection,
                account,
                "topic",
                identifier,
                "created" if not old else "updated",
                stream["uuid"],
            )
        return topic

    async def message(
        self,
        connection: typing.Any,
        account: dict,
        peer: dict,
        data: dict,
        *,
        stream: dict | None = None,
    ) -> tuple[bool, uuid.UUID]:
        stream = stream or await self.stream(connection, account, peer)
        topic = await self.topic(
            connection, account, stream, data["topic_id"], data.get("topic_title", "")
        )
        identifier = await self.identity(
            account, "message", data["identity"], connection=connection
        )
        old = await messenger.rows(
            connection,
            messenger.SELECT_ROW_SQL.format(table="messenger_messages"),
            (account["project_id"], identifier),
        )
        if old and old[0]["deleted_at"] is not None:
            return False, identifier
        author = await self.user(
            connection, account, data["sender_id"], data.get("sender_name", "")
        )
        text = data["text"]
        if data.get("reply_to"):
            reply_identity = conversion.message_identity(
                account["external_account_id"], peer["peer_key"], data["reply_to"]
            )
            reply_uuid = await self.identity(
                account, "message", reply_identity, connection=connection
            )
            reply_placement = uuid.uuid5(reply_uuid, str(stream["uuid"]))
            text = f"[Reply](urn:message:{reply_placement})\n\n{text}"
        for file in data.get("files", []):
            file_changed = await self.save(
                connection,
                "m_workspace_files",
                {
                    **file,
                    "project_id": account["project_id"],
                    "user_uuid": author,
                    "stream_uuid": stream["uuid"],
                    **origin(account, f"{data['identity']}:file"),
                },
            )
            if file_changed:
                recipients = await messenger.rows(
                    connection,
                    "SELECT * FROM m_workspace_visible_files_v1 WHERE uuid=%s",
                    (file["uuid"],),
                )
                await self.publish(
                    connection,
                    account,
                    "file.created",
                    file["uuid"],
                    recipients,
                    stream["uuid"],
                    recipients=[item["viewer_user_uuid"] for item in recipients],
                )
            kind = file["content_type"].lower().split("/", 1)[0]
            kind = kind if kind in ("image", "video") else "file"
            text += f"\n\n![{file['name']}](urn:{kind}:{file['uuid']})"
        content = await messenger.rows(
            connection,
            "SELECT messenger_resolve_message_content(%s::jsonb,%s) AS uuid",
            (messenger.json_value({"kind": "markdown", "content": text}), data["text"]),
        )
        created = datetime.datetime.fromisoformat(data["date"]).replace(tzinfo=None)
        edited = datetime.datetime.fromisoformat(data["edited_at"]).replace(tzinfo=None)
        changed = None
        if not old or edited >= old[0]["content_updated_at"].replace(tzinfo=None):
            changed = await self.save(
                connection,
                "messenger_messages",
                {
                    "uuid": identifier,
                    "project_id": account["project_id"],
                    "author_uuid": author,
                    "content_uuid": content[0]["uuid"],
                    "created_at": created,
                    "content_updated_at": edited,
                    **origin(account, data["identity"]),
                },
            )
        current_placements = await messenger.rows(
            connection,
            "SELECT uuid FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s AND stream_uuid=%s ORDER BY uuid LIMIT 1",
            (account["project_id"], identifier, stream["uuid"]),
        )
        public_uuid = (
            current_placements[0]["uuid"]
            if current_placements
            else uuid.uuid5(identifier, str(stream["uuid"]))
        )
        placement = {
            "uuid": public_uuid,
            "project_id": account["project_id"],
            "message_uuid": identifier,
            "stream_uuid": stream["uuid"],
            "topic_uuid": topic["uuid"],
            "created_at": created,
        }
        placed = await self.save(connection, "messenger_message_placements", placement)
        fresh = await messenger.rows(
            connection,
            "INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid) "
            "SELECT messenger_uuid_v5(%s,member.user_uuid::text),member.project_id,%s,member.user_uuid "
            "FROM messenger_stream_bindings member WHERE member.project_id=%s AND member.stream_uuid=%s AND member.active "
            "ON CONFLICT(project_id,placement_uuid,user_uuid) DO NOTHING RETURNING user_uuid",
            (public_uuid, public_uuid, account["project_id"], stream["uuid"]),
        )
        if (
            not data["out"]
            and data["id"] > peer["read_max_id"]
            and any(item["user_uuid"] == account["owner_user_uuid"] for item in fresh)
        ):
            await connection.execute(
                "INSERT INTO messenger_user_message_unreads(project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    public_uuid,
                    stream["uuid"],
                    topic["uuid"],
                ),
            )
        if data["out"] or data["id"] <= peer["read_max_id"]:
            await connection.execute(
                "DELETE FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s",
                (account["project_id"], account["owner_user_uuid"], public_uuid),
            )
        if changed or placed or fresh:
            snapshots = await messenger.rows(
                connection,
                messenger.SELECT_SNAPSHOT_SQL.format(view=messenger.VIEWS["message"]),
                (account["project_id"], public_uuid),
            )
            fresh_users = {item["user_uuid"] for item in fresh}
            if fresh_users:
                await self.publish(
                    connection,
                    account,
                    "message.created",
                    public_uuid,
                    [item for item in snapshots if item["user_uuid"] in fresh_users],
                    stream["uuid"],
                )
            if old and changed:
                await self.publish(
                    connection,
                    account,
                    "message.updated",
                    public_uuid,
                    [
                        item
                        for item in snapshots
                        if item["user_uuid"] not in fresh_users
                    ],
                    stream["uuid"],
                )
            await self.counters(connection, account, placement)
        await self.reactions(
            connection, account, identifier, public_uuid, stream["uuid"], data
        )
        return not old, identifier

    async def history_batch(
        self,
        connection: typing.Any,
        account: dict,
        peer: dict,
        stream: dict,
        messages: list[dict],
    ) -> int:
        """Normalize MTProto rows, then use the same COPY/merge engine as Zulip."""
        if not messages:
            return 0
        identities = {item["identity"] for item in messages}
        identities.update(
            conversion.message_identity(
                account["external_account_id"], peer["peer_key"], item["reply_to"]
            )
            for item in messages
            if item.get("reply_to")
        )
        known = await messenger.rows(
            connection,
            """
            SELECT message.*,placement.uuid AS placement_uuid
            FROM messenger_messages message
            LEFT JOIN messenger_message_placements placement ON placement.project_id=message.project_id
                AND placement.message_uuid=message.uuid AND placement.stream_uuid=%s
            WHERE message.project_id=%s AND message.provider_uuid=%s AND message.source_name='telegram'
                AND message.provider_identity=ANY(%s::text[])
        """,
            (
                stream["uuid"],
                account["project_id"],
                account["provider_uuid"],
                sorted(identities),
            ),
        )
        old = {row["provider_identity"]: row for row in known}
        members = await messenger.rows(
            connection,
            messenger.SELECT_MEMBERS_SQL,
            (account["project_id"], stream["uuid"]),
        )
        people = {}
        for item in messages:
            people[item["sender_id"]] = item.get("sender_name", "")
            people.update({actor["user_id"]: "" for actor in item["reaction_actors"]})
        users = {
            remote: await self.user(connection, account, remote, name)
            for remote, name in sorted(people.items())
        }
        topics = {}
        for item in messages:
            if item["topic_id"] not in topics:
                topics[item["topic_id"]] = await self.topic(
                    connection,
                    account,
                    stream,
                    item["topic_id"],
                    item.get("topic_title", ""),
                )
        batch: dict[str, list[dict]] = {
            table: []
            for table in (
                "messenger_message_contents",
                "messenger_messages",
                "messenger_message_placements",
                "messenger_user_message_bindings",
                "messenger_user_message_unreads",
                "messenger_message_reaction_facts",
                "m_workspace_files",
            )
        }
        contents: dict[tuple, dict] = {}
        files: dict[uuid.UUID, dict] = {}
        facts: dict[tuple, dict] = {}
        created = 0
        for item in messages:
            previous = old.get(item["identity"])
            if previous and previous["deleted_at"] is not None:
                continue
            identifier = (
                previous["uuid"]
                if previous
                else messenger.canonical_uuid(account, "message", item["identity"])
            )
            placement = (
                previous["placement_uuid"]
                if previous and previous["placement_uuid"]
                else uuid.uuid5(identifier, str(stream["uuid"]))
            )
            topic = topics[item["topic_id"]]
            text = item["text"]
            if item.get("reply_to"):
                remote = conversion.message_identity(
                    account["external_account_id"], peer["peer_key"], item["reply_to"]
                )
                reply = (
                    old[remote]["uuid"]
                    if remote in old
                    else messenger.canonical_uuid(account, "message", remote)
                )
                text = f"[Reply](urn:message:{uuid.uuid5(reply, str(stream['uuid']))})\n\n{text}"
            for file in item["files"]:
                files[file["uuid"]] = {
                    **file,
                    "project_id": account["project_id"],
                    "user_uuid": users[item["sender_id"]],
                    "stream_uuid": stream["uuid"],
                    **origin(account, f"{item['identity']}:file"),
                }
                kind = file["content_type"].lower().split("/", 1)[0]
                kind = kind if kind in ("image", "video") else "file"
                text += f"\n\n![{file['name']}](urn:{kind}:{file['uuid']})"
            payload = {"kind": "markdown", "content": text}
            content = contents.setdefault(
                (str(payload), item["text"]),
                {
                    "uuid": uuid.uuid4(),
                    "workspace_content": payload,
                    "provider_content": item["text"],
                },
            )
            edited = datetime.datetime.fromisoformat(item["edited_at"]).replace(
                tzinfo=None
            )
            counts = {
                emoji.normalize_name(name): count
                for name, count in item["reactions"].items()
            }
            actors: dict[str, set[str]] = {}
            for actor in item["reaction_actors"]:
                name = emoji.normalize_name(actor["emoji"])
                user = users[actor["user_id"]]
                actors.setdefault(name, set()).add(str(user))
                facts[(identifier, user, name)] = {
                    "uuid": uuid.uuid5(identifier, f"{user}:{name}"),
                    "project_id": account["project_id"],
                    "canonical_message_uuid": identifier,
                    "placement_uuid": placement,
                    "user_uuid": user,
                    "emoji_name": name,
                    **origin(
                        account,
                        f"{item['identity']}:reaction:{actor['user_id']}:{name}",
                    ),
                }
            root = {
                "uuid": identifier,
                "project_id": account["project_id"],
                "author_uuid": users[item["sender_id"]],
                "content_uuid": content["uuid"],
                "created_at": datetime.datetime.fromisoformat(item["date"]).replace(
                    tzinfo=None
                ),
                "content_updated_at": edited,
                "reactions": counts,
                "reaction_users": {
                    name: sorted(values)
                    for name, values in actors.items()
                    if len(values) == counts.get(name) and len(values) <= 5
                },
                **origin(account, item["identity"]),
            }
            if previous and edited < previous["content_updated_at"].replace(
                tzinfo=None
            ):
                for key in ("author_uuid", "content_uuid", "content_updated_at"):
                    root[key] = previous[key]
            batch["messenger_messages"].append(root)
            batch["messenger_message_placements"].append(
                {
                    "uuid": placement,
                    "project_id": account["project_id"],
                    "message_uuid": identifier,
                    "stream_uuid": stream["uuid"],
                    "topic_uuid": topic["uuid"],
                    "created_at": root["created_at"],
                }
            )
            batch["messenger_user_message_bindings"].extend(
                {
                    "uuid": uuid.uuid5(placement, str(member["user_uuid"])),
                    "project_id": account["project_id"],
                    "placement_uuid": placement,
                    "user_uuid": member["user_uuid"],
                }
                for member in members
            )
            if not item["out"] and item["id"] > peer["read_max_id"]:
                batch["messenger_user_message_unreads"].append(
                    {
                        "project_id": account["project_id"],
                        "user_uuid": account["owner_user_uuid"],
                        "placement_uuid": placement,
                        "stream_uuid": stream["uuid"],
                        "topic_uuid": topic["uuid"],
                    }
                )
            created += int(previous is None)
        batch["messenger_message_contents"] = list(contents.values())
        batch["m_workspace_files"] = list(files.values())
        batch["messenger_message_reaction_facts"] = list(facts.values())
        for table, values in batch.items():
            await history.stage(connection, table, values)
        if files:
            await history.merge(
                connection,
                "m_workspace_files",
                list(next(iter(files.values()))),
                key=("uuid",),
            )
        await history.merge_messages(
            connection,
            batch,
            account["provider_uuid"],
            "telegram",
            message_updates=(
                "author_uuid",
                "content_uuid",
                "content_updated_at",
                "provider_identity",
                "reactions",
                "reaction_users",
            ),
            condition="dst.deleted_at IS NULL AND src.content_updated_at>=dst.content_updated_at",
        )
        await history.merge_flags(connection, ("unreads",), account["owner_user_uuid"])
        await history.execute(
            connection,
            "delete.reactions",
            """
            DELETE FROM messenger_message_reaction_facts dst USING history_messenger_messages message
            WHERE dst.project_id=message.project_id AND dst.canonical_message_uuid=message.uuid
              AND dst.source_name='telegram' AND dst.provider_uuid=%s AND NOT EXISTS (
                SELECT 1 FROM history_messenger_message_reaction_facts src
                WHERE src.project_id=dst.project_id AND src.canonical_message_uuid=dst.canonical_message_uuid
                  AND src.user_uuid=dst.user_uuid AND src.emoji_name=dst.emoji_name)
        """,
            (account["provider_uuid"],),
        )
        if facts:
            await history.merge(
                connection,
                "messenger_message_reaction_facts",
                list(next(iter(facts.values()))),
                key=("project_id", "canonical_message_uuid", "user_uuid", "emoji_name"),
            )
        await history.queue_counters(connection)
        return created

    async def reactions(
        self,
        connection: typing.Any,
        account: dict,
        identifier: uuid.UUID,
        placement: uuid.UUID,
        stream: uuid.UUID,
        data: dict,
    ) -> None:
        if "reactions" not in data:
            return
        counts = {
            emoji.normalize_name(name): count
            for name, count in data["reactions"].items()
        }
        actors: dict[str, list[str]] = {}
        observed = []
        for actor in data["reaction_actors"]:
            name = emoji.normalize_name(actor["emoji"])
            user = await self.user(connection, account, actor["user_id"])
            actors.setdefault(name, []).append(str(user))
            fact = uuid.uuid5(identifier, f"{user}:{name}")
            saved = await self.save(
                connection,
                "messenger_message_reaction_facts",
                {
                    "uuid": fact,
                    "project_id": account["project_id"],
                    "canonical_message_uuid": identifier,
                    "placement_uuid": placement,
                    "user_uuid": user,
                    "emoji_name": name,
                    **origin(
                        account,
                        f"{data['identity']}:reaction:{actor['user_id']}:{name}",
                    ),
                },
                conflict="project_id, canonical_message_uuid, user_uuid, emoji_name",
            )
            # The outgoing Workspace reaction can already own this logical key.
            observed.append(saved["uuid"] if saved else fact)
        await connection.execute(
            "DELETE FROM messenger_message_reaction_facts WHERE project_id=%s AND canonical_message_uuid=%s AND provider_uuid=%s AND source_name='telegram' AND status='active' AND NOT(uuid=ANY(%s::uuid[]))",
            (account["project_id"], identifier, account["provider_uuid"], observed),
        )
        changed = await self.save(
            connection,
            "messenger_messages",
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "reactions": counts,
                "reaction_users": {
                    name: sorted(set(users))
                    for name, users in actors.items()
                    if len(set(users)) == counts.get(name) and len(users) <= 5
                },
            },
        )
        if changed:
            await self.snapshot(
                connection, account, "message", placement, "updated", stream
            )

    async def delete(
        self, connection: typing.Any, account: dict, peer: dict, remote_ids: list[int]
    ) -> None:
        for remote in remote_ids:
            identity = conversion.message_identity(
                account["external_account_id"], peer["peer_key"], remote
            )
            identifier = await self.identity(
                account, "message", identity, connection=connection
            )
            placements = await messenger.rows(
                connection,
                "SELECT * FROM messenger_message_placements WHERE project_id=%s AND message_uuid=%s",
                (account["project_id"], identifier),
            )
            for placement in placements:
                await self.snapshot(
                    connection,
                    account,
                    "message",
                    placement["uuid"],
                    "deleted",
                    placement["stream_uuid"],
                )
            await connection.execute(
                "UPDATE messenger_messages SET deleted_at=coalesce(deleted_at,now()) WHERE project_id=%s AND uuid=%s",
                (account["project_id"], identifier),
            )
            for placement in placements:
                await self.counters(connection, account, placement)
