# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Translate durable API intentions to Zulip requests using plain SQL."""

import asyncio
import datetime
import json
import typing
import urllib.parse
import uuid

from psycopg.types import json as pg_json

from workspace.bridges.zulip import content_cache, message_conversion, poll_conversion
from workspace.common import emoji
from workspace.messenger_api import file_storage
from workspace.messenger_api.dm import message_payloads

TABLES = {
    "stream": "messenger_streams",
    "stream_binding": "messenger_stream_bindings",
    "topic": "messenger_topics",
    "topic_binding": "messenger_user_topic_bindings",
    "message": "messenger_messages",
    "poll_vote": "messenger_messages",
    "reaction": "messenger_message_reaction_facts",
    "file": "m_workspace_files",
}

UPDATE_ENTITY_STATUS_SQL = """
    WITH desired AS (
        SELECT CASE WHEN %s = 'active' AND EXISTS (
            SELECT 1
            FROM zulip_outgoing_intents
            WHERE entity_type = ANY(%s)
              AND entity_uuid = %s
              AND id <> %s
              AND status IN ('new', 'checking')
        ) THEN 'new' ELSE %s END AS status
    )
    UPDATE {table} target
    SET status = desired.status
    FROM desired
    WHERE target.project_id = %s
      AND target.uuid = %s
      AND target.source_name <> 'native'
      AND target.status <> desired.status
"""

SELECT_ENTITY_IDENTITY_SQL = """
    SELECT provider_identity FROM {table}
    WHERE project_id = %s AND uuid = %s AND provider_uuid = %s
      AND source_name = 'zulip' AND provider_identity IS NOT NULL
"""

SELECT_FAILED_DEPENDENCY_SQL = """
    SELECT intent.id, intent.status, intent.error
    FROM zulip_outgoing_intents intent
    JOIN m_external_accounts_v2 account ON account.uuid = intent.external_account_uuid
    WHERE intent.project_id = %s AND intent.entity_type = %s
      AND intent.entity_uuid = %s AND account.provider_uuid = %s
      AND intent.operation = 'insert'
      AND intent.status <> 'done'
    ORDER BY intent.id DESC LIMIT 1
"""

UPDATE_ENTITY_IDENTITY_SQL = """
    UPDATE {table} SET provider_identity = %s
    WHERE project_id = %s AND uuid = %s AND provider_uuid = %s
      AND source_name = 'zulip' AND provider_identity IS DISTINCT FROM %s
"""

SAVE_RECEIPT_SQL = """
    UPDATE zulip_outgoing_intents SET data=data || jsonb_build_object('remote_identity',%s::text)
    WHERE project_id=%s AND entity_type=%s AND entity_uuid=%s
      AND external_account_uuid=%s AND id>%s AND status<>'done'
"""
SAVE_FILE_ACCESS_SQL = """
    INSERT INTO zulip_entity_links(project_id,entity_type,entity_uuid,provider_uuid,
        external_account_uuid,provider_identity,data)
    VALUES(%s,'file',%s,%s,%s,%s,%s)
    ON CONFLICT(entity_type,entity_uuid,provider_uuid,external_account_uuid)
    DO UPDATE SET provider_identity=EXCLUDED.provider_identity,
                  data=zulip_entity_links.data || EXCLUDED.data
"""

SELECT_REACTION_COPY_SQL = """
    SELECT data->>'emoji_name' AS emoji_name, data->>'emoji_code' AS emoji_code,
           data->>'reaction_type' AS reaction_type
    FROM zulip_entity_links
    WHERE project_id = %s AND entity_type = 'reaction' AND entity_uuid = %s
      AND provider_uuid = %s AND data ? 'emoji_code'
    LIMIT 1
"""
SELECT_REACTION_OBSERVATIONS_SQL = """
    SELECT reaction.emoji_name, reaction.emoji_code, reaction.reaction_type
    FROM bridge_zulip_v5_reactions reaction
    JOIN bridge_zulip_v5_reaction_messages message
      ON message.uuid = reaction.reaction_message_uuid
    WHERE message.project_id = %s AND message.canonical_message_uuid = %s
"""

SELECT_INACCESSIBLE_FILE_SQL = """
    SELECT 1
    FROM m_workspace_files file
    WHERE file.uuid = %s
      AND NOT EXISTS (
          SELECT 1
          FROM m_workspace_visible_files_v1 visible
          WHERE visible.uuid = file.uuid
            AND (
                visible.acl_mode = 'public'
                OR (visible.project_id = %s AND visible.viewer_user_uuid = %s)
            )
      )
"""

SELECT_PEER_ACCOUNT_SQL = """
    SELECT external_account_id
    FROM m_external_accounts_v2
    WHERE project_id = %s
      AND owner_user_uuid = %s
      AND provider = 'zulip'
      AND provider_uuid = %s
      AND external_account_id IS NOT NULL
"""

SELECT_DIRECT_RECIPIENTS_SQL = """
    SELECT jsonb_array_elements_text(
        zulip_direct_message_recipients(%s,%s,%s,%s)
    )::bigint AS id
"""

SELECT_STREAM_NAME_SQL = """
    SELECT name
    FROM messenger_streams
    WHERE project_id = %s AND uuid = %s
"""

UPSERT_DELIVERED_MESSAGE_SQL = """
    INSERT INTO bridge_zulip_v5_deliveries (
        uuid, project_id, message_uuid, stream_uuid, external_account_uuid,
        hostname, external_account_id, recipient, topic_name, zulip_message_id,
        delivered_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() AT TIME ZONE 'UTC')
    ON CONFLICT (project_id, message_uuid, stream_uuid)
    DO UPDATE SET zulip_message_id = EXCLUDED.zulip_message_id
    RETURNING uuid
"""

INSERT_CONTENT_SNAPSHOT_SQL = """
    SELECT zulip_record_content_snapshot(%s,%s,%s,%s) AS uuid
"""

SELECT_MESSAGE_RECIPIENTS_SQL = """
    WITH members AS (
        SELECT project_id,user_uuid
        FROM messenger_stream_bindings
        WHERE project_id = %s AND stream_uuid = %s AND active
    )
    SELECT DISTINCT account.external_account_id::bigint AS id
    FROM members
    LEFT JOIN m_external_accounts_v2 account
      ON account.project_id = members.project_id
     AND account.owner_user_uuid = members.user_uuid
     AND account.provider = 'zulip'
     AND account.provider_uuid = %s
    ORDER BY id
"""

SELECT_SUBSCRIPTION_SQL = """
    SELECT queue_id, last_event_id
    FROM zulip_incoming_subscriptions
    WHERE external_account_uuid = %s
"""

UPSERT_SENDING_MESSAGE_SQL = """
    INSERT INTO bridge_zulip_v5_deliveries (
        uuid, project_id, message_uuid, stream_uuid, external_account_uuid,
        hostname, external_account_id, recipient, topic_name, sending
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
    ON CONFLICT (project_id, message_uuid, stream_uuid)
    DO UPDATE SET sending = true
    RETURNING uuid
"""

SELECT_DIRECT_PEERS_SQL = """
    SELECT DISTINCT peer.external_account_id::bigint AS id
    FROM messenger_stream_bindings binding
    JOIN m_external_accounts_v2 peer
      ON peer.project_id = binding.project_id
     AND peer.owner_user_uuid = binding.user_uuid
    WHERE binding.project_id = %s
      AND binding.stream_uuid = %s
      AND binding.active
      AND peer.provider_uuid = %s
      AND peer.external_account_id IS NOT NULL
      AND peer.external_account_id <> %s
    ORDER BY id
"""

SELECT_USER_REFERENCE_SQL = """
    SELECT account.external_account_id,
           COALESCE(nullif(users.first_name, ''), users.username) AS name
    FROM m_external_accounts_v2 account
    JOIN m_workspace_users users ON users.uuid = account.owner_user_uuid
    WHERE account.project_id = %s
      AND account.owner_user_uuid = %s
      AND account.provider_uuid = %s
"""

SELECT_MESSAGE_REFERENCE_SQL = """
    (
    SELECT canonical_message_uuid AS message_uuid
    FROM messenger_api_user_messages_v1
    WHERE project_id = %s
      AND user_uuid = %s
      AND visible
      AND uuid = %s
    LIMIT 1
    ) UNION ALL (
    SELECT visible.canonical_message_uuid AS message_uuid
    FROM (SELECT %s::uuid AS project_id,%s::uuid AS user_uuid,
                 %s::uuid AS message_uuid) requested
    CROSS JOIN LATERAL (
        SELECT uuid FROM messenger_message_placements
        WHERE project_id=requested.project_id AND message_uuid=requested.message_uuid
        OFFSET 0
    ) placement
    CROSS JOIN LATERAL (
        SELECT canonical_message_uuid FROM messenger_api_user_messages_v1
        WHERE project_id=requested.project_id AND user_uuid=requested.user_uuid
          AND uuid=placement.uuid AND visible
        OFFSET 0
    ) visible
    LIMIT 1
    )
    LIMIT 1
"""

SELECT_VISIBLE_FILE_SQL = """
    SELECT *
    FROM m_workspace_visible_files_v1
    WHERE uuid = %s
      AND (acl_mode = 'public' OR (project_id = %s AND viewer_user_uuid = %s))
    LIMIT 1
"""

SELECT_VISIBLE_STREAM_SQL = """
    SELECT 1
    FROM messenger_api_user_streams_v1
    WHERE project_id = %s AND user_uuid = %s AND uuid = %s AND visible
"""

SELECT_VISIBLE_TOPIC_SQL = """
    SELECT *
    FROM messenger_api_user_topics_v1
    WHERE project_id = %s AND user_uuid = %s AND uuid = %s AND visible
"""

SELECT_DELIVERY_MESSAGE_SQL = """
    SELECT zulip_message_id
    FROM bridge_zulip_v5_deliveries
    WHERE uuid = %s
"""

SELECT_SEND_DELIVERY_SQL = """
    SELECT uuid FROM bridge_zulip_v5_deliveries
    WHERE project_id = %s AND message_uuid = %s AND stream_uuid = %s
"""

SELECT_MESSAGE_RECEIPT_SQL = """
    SELECT payload
    FROM zulip_incoming_events
    WHERE external_account_uuid = %s AND payload->>'local_message_id' = ANY(%s::text[])
      AND payload ? 'local_message_id'
    ORDER BY id LIMIT 1
"""


INSERT_FILE_LINK_SQL = """
    INSERT INTO bridge_zulip_v5_file_links (
        uuid, project_id, hostname, stream_uuid, file_uuid, zulip_url
    )
    SELECT %s, %s, provider.hostname, %s, %s, %s
    FROM messenger_providers provider
    WHERE provider.uuid = %s
      AND EXISTS (
          SELECT 1 FROM m_workspace_files WHERE uuid = %s AND project_id = %s
      )
    ON CONFLICT DO NOTHING
"""

COMPLETE_DELIVERY_SQL = """
    UPDATE bridge_zulip_v5_deliveries
    SET zulip_message_id = %s,
        sending = false,
        delivered_at = coalesce(delivered_at, now() AT TIME ZONE 'UTC')
    WHERE uuid = %s
"""

COMPLETE_SEND_RECEIPT_SQL = """
    WITH locked AS MATERIALIZED (
        SELECT messenger_lock_message(%s,%s)
    ), receipt AS (
        -- Choose from the row being updated, including a receipt committed
        -- while this statement waited for the canonical message lock.
        UPDATE bridge_zulip_v5_deliveries delivery
        SET zulip_message_id=coalesce(delivery.zulip_message_id,%s::bigint),sending=false,
            delivered_at=coalesce(delivery.delivered_at,now() AT TIME ZONE 'UTC')
        FROM locked WHERE delivery.uuid=%s
        RETURNING delivery.project_id,delivery.message_uuid,delivery.zulip_message_id
    ), canonical AS (
        UPDATE messenger_messages message
        SET provider_identity=receipt.zulip_message_id::text
        FROM receipt
        WHERE message.project_id=receipt.project_id AND message.uuid=receipt.message_uuid
          AND message.source_name='zulip' AND message.provider_uuid=%s
          AND message.provider_identity IS DISTINCT FROM receipt.zulip_message_id::text
    )
    SELECT zulip_message_id::text AS identity FROM receipt
"""

COMPLETE_INTENT_SQL = """
    UPDATE zulip_outgoing_intents
    SET status = 'done', error = NULL, completed_at = now()
    WHERE id = %s
"""

SELECT_FILE_REFERENCES_SQL = """
    SELECT 'file' AS kind, uuid
    FROM m_workspace_files
    WHERE storage_type = %s AND storage_id = %s AND storage_object_id = %s
    UNION ALL
    SELECT 'upload' AS kind, entity_uuid AS uuid
    FROM zulip_outgoing_intents
    WHERE entity_type = 'file'
      AND operation = 'insert'
      AND status <> 'done'
      AND data->>'storage_type' = %s
      AND data->>'storage_id' = %s
      AND data->>'storage_object_id' = %s
"""


class Pending(Exception):
    pass


class RetrySend(Exception):
    pass


def lost_queue(error: "ApiError") -> bool:
    return error.data.get("code") == "BAD_EVENT_QUEUE_ID" or (
        error.status == 400
        and error.data.get("msg", "").startswith("An event newer than ")
        and error.data.get("msg", "").endswith(" has already been pruned!")
    )


class ApiError(Exception):
    def __init__(self, status: int, data: dict, method: str = "") -> None:
        self.status = status
        self.data = data
        self.method = method
        super().__init__(f"Zulip HTTP {status}: {data.get('msg', '')}")


def request(
    kind: str, method: str, path: str, data: dict | None = None, **extra: typing.Any
) -> dict:
    return {"kind": kind, "method": method, "path": path, "data": data or {}, **extra}


class Operations:
    def __init__(self, db: typing.Any, http: typing.Any) -> None:
        self.db = db
        self.http = http

    async def status(self, intent: dict, status: str) -> None:
        if intent["entity_type"] == "message_state":
            return
        table = TABLES[intent["entity_type"]]
        await self.db.sql(
            UPDATE_ENTITY_STATUS_SQL.format(table=table),
            (
                status,
                ["message", "poll_vote"]
                if intent["entity_type"] in {"message", "poll_vote"}
                else [intent["entity_type"]],
                intent["entity_uuid"],
                intent["id"],
                status,
                intent["project_id"],
                intent["entity_uuid"],
            ),
        )

    async def identity(
        self, intent: dict, account: dict, kind: str, entity_uuid: object
    ) -> str:
        identities = await self.db.sql(
            SELECT_ENTITY_IDENTITY_SQL.format(table=TABLES[kind]),
            (intent["project_id"], entity_uuid, account["provider_uuid"]),
        )
        if not identities:
            parents = await self.db.sql(
                SELECT_FAILED_DEPENDENCY_SQL,
                (intent["project_id"], kind, entity_uuid, account["provider_uuid"]),
            )
            if parents and parents[0]["status"] == "error":
                raise ValueError(
                    f"Cannot resolve {kind} {entity_uuid}: outgoing intent "
                    f"{parents[0]['id']} failed: {parents[0]['error']}"
                )
            raise Pending(f"Waiting for {kind} {entity_uuid}")
        return identities[0]["provider_identity"]

    async def route(self, intent: dict, account: dict) -> dict:
        data = intent["data"]
        if data.get("channel_type"):
            return data
        streams = await self.db.sql(
            "SELECT private,direct_user_uuid,provider_identity FROM messenger_streams "
            "WHERE project_id=%s AND uuid=%s AND source_name='zulip' AND provider_uuid=%s",
            (intent["project_id"], data["stream_uuid"], account["provider_uuid"]),
        )
        if not streams:
            raise Pending("Waiting for the provider stream")
        stream = streams[0]
        if stream["private"] or (stream["provider_identity"] or "").startswith("dm:"):
            return {
                "channel_type": "private"
                if stream["direct_user_uuid"] is not None
                else "group",
                "direct_user_uuid": stream["direct_user_uuid"],
            }
        identity = await self.identity(intent, account, "stream", data["stream_uuid"])
        return {"stream_id": int(identity), "channel_type": "channel"}

    async def prepare(self, intent: dict, account: dict) -> dict:
        await self.status(intent, "syncing")
        kind, data, operation = (
            intent["entity_type"],
            intent["data"],
            intent["operation"],
        )
        if kind == "poll_vote":
            remote = data.get("remote_identity") or await self.identity(
                intent, account, "message", intent["entity_uuid"]
            )
            observed = await self.poll(account, remote)
            question = data["payload"]["questions"][0]
            label = next(
                o["text"]
                for o in question["options"]
                if o["uuid"] == data["option_uuid"]
            )
            key = next(
                (key for key, text in observed.options.items() if text == label), None
            )
            if key is None:
                raise ValueError("The poll option no longer exists in Zulip")
            return request(
                "poll_vote",
                "POST",
                "/submessage",
                {
                    "message_id": int(remote),
                    "msg_type": "widget",
                    "content": json.dumps(
                        {
                            "type": "vote",
                            "key": key,
                            "vote": 1 if data["selected"] else -1,
                        }
                    ),
                },
                message_id=int(remote),
                option_key=key,
                selected=data["selected"],
                user_id=int(account["external_account_id"]),
                after_revision=observed.revision,
            )
        if kind == "message":
            if operation == "insert":
                try:
                    remote = await self.identity(
                        intent, account, "message", intent["entity_uuid"]
                    )
                except Pending:
                    return await self.send_message(intent, account)
                deliveries = await self.db.sql(
                    SELECT_SEND_DELIVERY_SQL,
                    (intent["project_id"], intent["entity_uuid"], data["stream_uuid"]),
                )
                prepared = request("noop", "", "", identity=remote)
                if deliveries:
                    prepared["delivery_uuid"] = str(deliveries[0]["uuid"])
                return prepared
            remote = data.get("remote_identity") or await self.identity(
                intent, account, "message", intent["entity_uuid"]
            )
            if operation == "delete":
                return request("delete", "DELETE", f"/messages/{remote}")
            if await self.outdated_edit(intent, account):
                return request("noop", "", "", identity=remote)
            if data["payload"]["kind"] == "poll" and poll_conversion.compatible(
                data["payload"]
            ):
                return request(
                    "poll_options",
                    "",
                    "",
                    message_id=int(remote),
                    labels=[
                        o["text"] for o in data["payload"]["questions"][0]["options"]
                    ],
                    identity=str(remote),
                )
            content = await self.convert(intent, account)
            await self.content_snapshot(intent, account, content["uuid"], remote)
            return request(
                "edit",
                "PATCH",
                f"/messages/{remote}",
                {"content": content["provider_content"]},
                identity=remote,
            )
        if kind in ("reaction", "message_state"):
            remote = data.get("remote_message_identity") or await self.identity(
                intent, account, "message", data["message_uuid"]
            )
            if kind == "reaction":
                observations = data.get("emoji_observations")
                if observations is None:
                    observations = await self.db.sql(
                        SELECT_REACTION_OBSERVATIONS_SQL,
                        (intent["project_id"], data["message_uuid"]),
                    )
                copies = await self.db.sql(
                    SELECT_REACTION_COPY_SQL,
                    (
                        intent["project_id"],
                        intent["entity_uuid"],
                        account["provider_uuid"],
                    ),
                )
                observations = [*observations, *copies]
                custom = next(
                    (
                        row
                        for row in observations
                        if row["reaction_type"] != "unicode_emoji"
                        and row["emoji_name"] == data["emoji_name"]
                    ),
                    None,
                )
                if custom is not None:
                    return request(
                        "reaction",
                        "DELETE" if operation == "delete" else "POST",
                        f"/messages/{remote}/reactions",
                        custom,
                        identity=f"{remote}:{account['external_account_id']}:{custom['reaction_type']}:{custom['emoji_code']}",
                        message_id=int(remote),
                        present=operation != "delete",
                        user_id=int(account["external_account_id"]),
                    )
                identity = emoji.zulip_identity(data["emoji_name"], observations)
                if identity is None:
                    raise ValueError(f"Unsupported Unicode emoji: {data['emoji_name']}")
                code, name = identity
                return request(
                    "reaction",
                    "DELETE" if operation == "delete" else "POST",
                    f"/messages/{remote}/reactions",
                    {
                        "emoji_name": name,
                        "emoji_code": code,
                        "reaction_type": "unicode_emoji",
                    },
                    identity=f"{remote}:{account['external_account_id']}:unicode_emoji:{code}",
                    message_id=int(remote),
                    present=operation != "delete",
                    user_id=int(account["external_account_id"]),
                )
            if "flags" in data:
                changed = data["flags"]
            else:
                # Intentions queued before the sparse-flag migration retain
                # their captured before/after state until delivery completes.
                before = data["before"]
                flags = {
                    "read": data["read_at"] is not None,
                    "starred": data["starred"],
                }
                changed = {
                    key: value
                    for key, value in flags.items()
                    if value
                    != (
                        before.get("read_at") is not None
                        if key == "read"
                        else before.get("starred", False)
                    )
                }
            return request(
                "flags",
                "POST",
                "/messages/flags",
                identity=f"{remote}:{account['external_account_id']}",
                message_id=int(remote),
                flags=changed,
            )
        if kind == "stream":
            if data["private"]:
                return request("noop", "", "")
            remote = data.get("remote_identity")
            if remote is None:
                if operation == "insert":
                    return request(
                        "stream_create",
                        "POST",
                        "/users/me/subscriptions",
                        {
                            "subscriptions": [
                                {
                                    "name": data["name"],
                                    "description": data["description"],
                                }
                            ],
                            "invite_only": data["invite_only"],
                        },
                        name=data["name"],
                    )
                remote = await self.identity(
                    intent, account, kind, intent["entity_uuid"]
                )
            if operation == "delete" or data["is_archived"]:
                return request(
                    "archive", "DELETE", f"/streams/{remote}", stream_id=int(remote)
                )
            values = {
                key: data[key]
                for key in ("name", "description", "invite_only")
                if data[key] != data["before"].get(key)
            }
            if data["before"].get("is_archived"):
                values["is_archived"] = False
            return request(
                "stream_update",
                "PATCH",
                f"/streams/{remote}",
                values,
                stream_id=int(remote),
            )
        if kind == "file":
            if operation == "delete":
                if data.get("provider_deleted"):
                    return request("noop", "", "")
                remote = data.get("remote_identity") or await self.identity(
                    intent, account, "file", intent["entity_uuid"]
                )
                attachments = (await self.http.request(account, "GET", "/attachments"))[
                    "attachments"
                ]
                match = next(
                    (
                        row
                        for row in attachments
                        if row["path_id"] == remote.removeprefix("/user_uploads/")
                    ),
                    None,
                )
                return (
                    request("delete", "DELETE", f"/attachments/{match['id']}")
                    if match
                    else request("noop", "", "")
                )
            inaccessible = await self.db.sql(
                SELECT_INACCESSIBLE_FILE_SQL,
                (
                    intent["entity_uuid"],
                    intent["project_id"],
                    account["owner_user_uuid"],
                ),
            )
            if inaccessible:
                raise ValueError("Workspace attachment is not accessible")
            existing = (await self.http.request(account, "GET", "/attachments"))[
                "attachments"
            ]
            return request(
                "upload",
                "POST",
                "/user_uploads",
                file=data,
                upload_name=f"{intent['uuid']}-{data['name'][:120]}",
                existing_ids=[row["id"] for row in existing],
            )
        route = await self.route(intent, account)
        if route["channel_type"] != "channel":
            # Zulip DMs have no editable stream/topic/subscription objects.
            return request("noop", "", "")
        stream_id = route["stream_id"]
        if kind == "topic":
            name = self.topic_name(data, data["stream_uuid"])
            if operation == "insert":
                # Topics in Zulip are created by their first message.
                return request("noop", "", "", identity=f"{stream_id}:{name}")
            old_name = self.topic_name(data["before"] or data, data["stream_uuid"])
            if operation == "delete":
                return request(
                    "topic_delete",
                    "POST",
                    f"/streams/{stream_id}/delete_topic",
                    {"topic_name": old_name},
                    stream_id=stream_id,
                    topic=old_name,
                )
            messages = await self.http.request(
                account,
                "GET",
                "/messages",
                {
                    "narrow": [
                        {"operator": "stream", "operand": stream_id},
                        {"operator": "topic", "operand": old_name},
                    ],
                    "anchor": "newest",
                    "num_before": 1,
                    "num_after": 0,
                    "apply_markdown": False,
                },
            )
            if not messages["messages"]:
                return request("noop", "", "", identity=f"{stream_id}:{name}")
            return request(
                "topic_rename",
                "PATCH",
                f"/messages/{messages['messages'][0]['id']}",
                {"topic": name, "propagate_mode": "change_all"},
                stream_id=stream_id,
                old_topic=old_name,
                topic=name,
                identity=f"{stream_id}:{name}",
            )
        if kind == "stream_binding":
            peers = await self.db.sql(
                SELECT_PEER_ACCOUNT_SQL,
                (intent["project_id"], data["user_uuid"], account["provider_uuid"]),
            )
            if not peers:
                raise Pending("Waiting for the member's external account")
            principal = int(peers[0]["external_account_id"])
            membership = (
                operation in ("insert", "delete")
                or data["active"] != data["before"]["active"]
            )
            if membership:
                stream = await self.db.sql(
                    SELECT_STREAM_NAME_SQL,
                    (intent["project_id"], data["stream_uuid"]),
                )
                name = stream[0]["name"] if stream else data["stream_name"]
                present = operation != "delete" and data["active"]
                return request(
                    "membership",
                    "POST" if present else "DELETE",
                    "/users/me/subscriptions",
                    {
                        "subscriptions": [{"name": name}] if present else [name],
                        "principals": [principal],
                    },
                    identity=f"{stream_id}:{principal}",
                    stream_id=stream_id,
                    user_id=principal,
                    present=present,
                )
            return request(
                "stream_notifications",
                "POST",
                "/users/me/subscriptions/properties",
                {
                    "subscription_data": [
                        {
                            "stream_id": stream_id,
                            "property": "is_muted",
                            "value": data["notification_mode"] != "all_messages",
                        }
                    ]
                },
                identity=f"{stream_id}:{account['external_account_id']}",
                stream_id=stream_id,
            )
        if kind == "topic_binding":
            policy = {"default": 0, "mute": 1, "unmute": 2, "follow": 3}[
                data["notification_mode"]
            ]
            return request(
                "topic_notifications",
                "POST",
                "/user_topics",
                {
                    "stream_id": stream_id,
                    "topic": data["topic_name"],
                    "visibility_policy": policy,
                },
                identity=f"{stream_id}:{data['topic_name']}:{account['external_account_id']}",
            )
        raise ValueError(f"Unsupported outgoing entity: {kind}")

    @staticmethod
    def topic_name(topic: dict, stream_uuid: object) -> str:
        name = (
            ""
            if str(topic["uuid"]) == str(uuid.uuid5(uuid.UUID(str(stream_uuid)), ""))
            else topic["name"]
        )
        return (
            "✔ " + name
            if topic["is_done"] and not name.startswith("✔ ")
            else name.removeprefix("✔ ")
            if not topic["is_done"]
            else name
        )

    async def content_snapshot(
        self, intent: dict, account: dict, content_uuid: object, remote: str
    ) -> None:
        data = intent["data"]
        delivery_uuid = uuid.uuid5(
            account["uuid"], f"{intent['entity_uuid']}:{data['stream_uuid']}"
        )
        deliveries = await self.db.sql(
            UPSERT_DELIVERED_MESSAGE_SQL,
            (
                delivery_uuid,
                intent["project_id"],
                intent["entity_uuid"],
                data["stream_uuid"],
                account["uuid"],
                account["hostname"],
                account["external_account_id"],
                pg_json.Jsonb({"to": data.get("stream_id")}),
                data.get("topic_name"),
                int(remote),
            ),
        )
        await self.db.sql(
            INSERT_CONTENT_SNAPSHOT_SQL,
            (
                intent["uuid"],
                deliveries[0]["uuid"],
                content_uuid,
                data.get("content_revision"),
            ),
        )

    async def send_message(self, intent: dict, account: dict) -> dict:
        delivery_id = uuid.uuid5(
            account["uuid"], f"{intent['entity_uuid']}:{intent['data']['stream_uuid']}"
        )
        deliveries = await self.db.sql(
            SELECT_SEND_DELIVERY_SQL,
            (
                intent["project_id"],
                intent["entity_uuid"],
                intent["data"]["stream_uuid"],
            ),
        )
        if deliveries:
            delivery_id = deliveries[0]["uuid"]
            remote = await self.send_receipt_identity(intent, account, str(delivery_id))
            if remote is not None:
                return request(
                    "noop", "", "", identity=str(remote), delivery_uuid=str(delivery_id)
                )
        self.check_send_window(intent)
        route = await self.route(intent, account)
        data = intent["data"]
        if route["channel_type"] == "channel":
            recipient = route["stream_id"]
            topic = data["topic_name"]
        elif route["channel_type"] == "private":
            peers = await self.db.sql(
                SELECT_DIRECT_RECIPIENTS_SQL,
                (
                    intent["project_id"],
                    account["provider_uuid"],
                    data["stream_uuid"],
                    account["owner_user_uuid"],
                ),
            )
            if not peers:
                raise Pending("Waiting for the direct message recipient")
            recipient = [row["id"] for row in peers]
            topic = None
        else:
            peers = await self.db.sql(
                SELECT_MESSAGE_RECIPIENTS_SQL,
                (intent["project_id"], data["stream_uuid"], account["provider_uuid"]),
            )
            recipient = [row["id"] for row in peers]
            if (
                not recipient
                or None in recipient
                or not set(recipient) - {int(account["external_account_id"])}
            ):
                raise Pending("Waiting for the group message recipients")
            topic = None
        subscriptions = await self.db.sql(
            SELECT_SUBSCRIPTION_SQL,
            (account["uuid"],),
        )
        if not subscriptions:
            raise Pending("Waiting for the sender's event subscription")
        content = await self.convert(intent, account)
        deliveries = await self.db.sql(
            UPSERT_SENDING_MESSAGE_SQL,
            (
                delivery_id,
                intent["project_id"],
                intent["entity_uuid"],
                data["stream_uuid"],
                account["uuid"],
                account["hostname"],
                account["external_account_id"],
                pg_json.Jsonb({"to": recipient}),
                topic,
            ),
        )
        delivery_uuid = str(deliveries[0]["uuid"])
        await self.db.sql(
            INSERT_CONTENT_SNAPSHOT_SQL,
            (
                intent["uuid"],
                delivery_uuid,
                content["uuid"],
                data.get("content_revision"),
            ),
        )
        subscription = subscriptions[0]
        values = {
            "type": "direct" if topic is None else "channel",
            "to": recipient,
            "content": content["provider_content"],
            "read_by_sender": True,
            "queue_id": subscription["queue_id"],
            "local_id": str(intent["entity_uuid"]),
        }
        if topic is not None:
            values["topic"] = topic
        if data["payload"]["kind"] == "poll" and poll_conversion.compatible(
            data["payload"]
        ):
            question = data["payload"]["questions"][0]
            # Explicit widget data bypasses Zulip's /poll list syntax, which
            # strips leading '-' and '*' characters from option labels.
            values["widget_content"] = {
                "widget_type": "poll",
                "extra_data": {
                    "question": question["text"],
                    "options": [option["text"] for option in question["options"]],
                },
            }
        return request(
            "send",
            "POST",
            "/messages",
            values,
            delivery_uuid=delivery_uuid,
            last_event_id=subscription["last_event_id"],
        )

    async def stream_url(self, intent: dict, account: dict, stream_uuid: object) -> str:
        await self.identity(intent, account, "stream", stream_uuid)
        route = await self.route(
            {**intent, "data": {"stream_uuid": stream_uuid}}, account
        )
        base = account["settings"]["server_url"].rstrip("/")
        if route["channel_type"] == "channel":
            return f"{base}/#narrow/channel/{route['stream_id']}"
        peers = await self.db.sql(
            SELECT_DIRECT_PEERS_SQL,
            (
                intent["project_id"],
                stream_uuid,
                account["provider_uuid"],
                account["external_account_id"],
            ),
        )
        ids = (
            ",".join(str(peer["id"]) for peer in peers)
            or account["external_account_id"]
        )
        return f"{base}/#narrow/dm/{ids}" + (
            "-group" if route["channel_type"] == "group" else ""
        )

    async def convert(self, intent: dict, account: dict) -> dict:
        payload = intent["data"]["payload"]
        references = {
            (kind, identifier)
            for kind, identifier, query in message_conversion.workspace_references(
                message_conversion.workspace_reference_text(
                    message_payloads.markdown_content(payload)
                )
            )
            if kind != "forward" or message_conversion.parse_snapshot(query) is not None
        }
        context: dict[str, typing.Any] = {
            "server_url": account["settings"]["server_url"],
            "strict_identities": True,
            "normalize_emoji": True,
            "user_ids": {},
            "stream_urls": {},
            "topic_urls": {},
            "message_ids": {},
            "messages": {},
            "files": {},
        }
        if payload["kind"] == "poll" and not poll_conversion.compatible(payload):
            data = intent["data"]
            context["workspace_message_url"] = poll_conversion.message_url(
                account["settings"]["workspace_url"],
                intent["project_id"],
                data["stream_uuid"],
                data["topic_uuid"],
                data["placement_uuid"],
            )
        for kind, identifier in set(references):
            if kind == "user":
                users = await self.db.sql(
                    SELECT_USER_REFERENCE_SQL,
                    (intent["project_id"], identifier, account["provider_uuid"]),
                )
                if not users:
                    raise ValueError(f"No Zulip user mapping for {identifier}")
                context["user_ids"][identifier] = (
                    int(users[0]["external_account_id"]),
                    users[0]["name"],
                )
            elif kind in ("message", "quote", "forward"):
                placements = await self.db.sql(
                    SELECT_MESSAGE_REFERENCE_SQL,
                    (
                        intent["project_id"],
                        account["owner_user_uuid"],
                        identifier,
                        intent["project_id"],
                        account["owner_user_uuid"],
                        identifier,
                    ),
                )
                if not placements:
                    if kind == "forward":
                        continue
                    raise ValueError(
                        f"No accessible Zulip message mapping for {identifier}"
                    )
                try:
                    remote = await self.identity(
                        intent, account, "message", placements[0]["message_uuid"]
                    )
                except Pending:
                    if kind == "forward":
                        continue
                    raise
                context["message_ids"][identifier] = int(remote)
                if kind != "message":
                    context["messages"][int(remote)] = (
                        await self.http.request(
                            account,
                            "GET",
                            f"/messages/{remote}",
                            {"apply_markdown": False},
                        )
                    )["message"]
            elif kind in ("file", "image", "video"):
                files = await self.db.sql(
                    SELECT_VISIBLE_FILE_SQL,
                    (identifier, intent["project_id"], account["owner_user_uuid"]),
                )
                if not files:
                    raise ValueError(
                        f"Workspace attachment is not accessible: {identifier}"
                    )
                file = files[0]
                if file["source_name"] != "zulip" or str(file["provider_uuid"]) != str(
                    account["provider_uuid"]
                ):
                    raise ValueError(
                        f"Attachment belongs to a different source: {identifier}"
                    )
                remote = await self.identity(intent, account, "file", identifier)
                context["files"][remote] = f"urn:{kind}:{identifier}"
            elif kind in ("stream", "topic"):
                if kind == "stream":
                    streams = await self.db.sql(
                        SELECT_VISIBLE_STREAM_SQL,
                        (intent["project_id"], account["owner_user_uuid"], identifier),
                    )
                    if not streams:
                        raise ValueError(
                            f"Referenced stream is not accessible: {identifier}"
                        )
                    context["stream_urls"][identifier] = await self.stream_url(
                        intent, account, identifier
                    )
                else:
                    topics = await self.db.sql(
                        SELECT_VISIBLE_TOPIC_SQL,
                        (intent["project_id"], account["owner_user_uuid"], identifier),
                    )
                    if not topics:
                        raise ValueError(
                            f"Referenced topic is not accessible: {identifier}"
                        )
                    topic = topics[0]
                    url = await self.stream_url(intent, account, topic["stream_uuid"])
                    context["topic_urls"][identifier] = url + (
                        f"/topic/{message_conversion.encode_hash(self.topic_name(topic, topic['stream_uuid']))}"
                        if "/#narrow/channel/" in url
                        else ""
                    )
        cache_key = content_cache.key(
            account["provider_uuid"], "outgoing", payload, context
        )
        cached = await self.db.sql(
            content_cache.LOOKUP_SQL.format(source="workspace_content"),
            (*cache_key, pg_json.Jsonb(payload)),
        )
        if cached:
            return cached[0]
        text = message_conversion.workspace_to_zulip(payload, context)
        content = (
            await self.db.sql(content_cache.RESOLVE_SQL, (pg_json.Jsonb(payload), text))
        )[0]
        await self.db.sql(content_cache.SAVE_SQL, (*cache_key, content["uuid"]))
        return {"uuid": content["uuid"], "provider_content": text}

    async def poll(self, account: dict, remote: object) -> poll_conversion.Poll:
        message = (
            await self.http.request(
                account, "GET", f"/messages/{remote}", {"apply_markdown": False}
            )
        )["message"]
        observed = poll_conversion.read(message)
        if observed is None:
            raise Pending("Waiting for the Zulip poll widget")
        return observed

    async def send_receipt_identity(
        self,
        intent: dict,
        account: dict,
        delivery_uuid: str,
        local_id: str | None = None,
    ) -> int | None:
        deliveries = await self.db.sql(SELECT_DELIVERY_MESSAGE_SQL, (delivery_uuid,))
        if deliveries and deliveries[0]["zulip_message_id"] is not None:
            return deliveries[0]["zulip_message_id"]
        events = await self.db.sql(
            SELECT_MESSAGE_RECEIPT_SQL,
            (
                account["uuid"],
                [
                    local_id or str(intent["entity_uuid"]),
                    str(intent["entity_uuid"]),
                    delivery_uuid,
                ],
            ),
        )
        return events[0]["payload"]["message"]["id"] if events else None

    async def verify(
        self, intent: dict, account: dict, prepared: dict
    ) -> tuple[bool, dict]:
        kind = prepared["kind"]
        if kind == "noop":
            return True, {"identity": prepared.get("identity")}
        if kind in {"poll_vote", "poll_options"}:
            observed = await self.poll(account, prepared["message_id"])
            observation = {
                "poll_revision": observed.revision,
                "poll_message_id": prepared["message_id"],
            }
            if kind == "poll_vote":
                current = (
                    prepared["user_id"] in observed.votes[prepared["option_key"]]
                ) == prepared["selected"]
                applied = any(
                    revision > prepared["after_revision"]
                    and sender == prepared["user_id"]
                    and key == prepared["option_key"]
                    and vote == (1 if prepared["selected"] else -1)
                    for revision, sender, key, vote in observed.vote_events
                )
                return current or applied, observation
            return all(
                label in observed.options.values() for label in prepared["labels"]
            ), {"identity": prepared["identity"], **observation}
        if kind == "send":
            delivery_uuid = prepared["delivery_uuid"]
            local_id = prepared["data"]["local_id"]
            remote = await self.send_receipt_identity(
                intent, account, delivery_uuid, local_id
            )
            if remote is None:
                events = []
                if "data" in prepared:
                    try:
                        response = await self.http.request(
                            account,
                            "GET",
                            "/events",
                            {
                                "queue_id": prepared["data"]["queue_id"],
                                "last_event_id": prepared["last_event_id"],
                                "dont_block": True,
                            },
                        )
                    except ApiError as error:
                        if lost_queue(error):
                            raise RetrySend(
                                "Retrying send after its receipt queue expired or was pruned"
                            ) from error
                        raise
                    events = [
                        {"payload": event}
                        for event in response["events"]
                        if event.get("local_message_id") == local_id
                    ]
                if events:
                    remote = events[0]["payload"]["message"]["id"]
            if remote is None:
                raise RetrySend(
                    "Retrying send without a definitive receipt; a duplicate is possible"
                )
            try:
                await self.http.request(
                    account, "GET", f"/messages/{remote}", {"apply_markdown": False}
                )
            except ApiError as error:
                if not self.absent(error):
                    raise
            return True, {"identity": str(remote)}
        if kind == "upload":
            attachments = (await self.http.request(account, "GET", "/attachments"))[
                "attachments"
            ]
            matches = [
                row for row in attachments if row["name"] == prepared["upload_name"]
            ]
            if len(matches) != 1:
                raise Pending("GET has not confirmed the uploaded file yet")
            return True, {"identity": "/user_uploads/" + matches[0]["path_id"]}
        if kind == "stream_create":
            try:
                result = await self.http.request(
                    account, "GET", "/get_stream_id", {"stream": prepared["name"]}
                )
            except ApiError as error:
                if error.status == 404 or error.data.get("code") == "BAD_REQUEST":
                    return False, {}
                raise
            return True, {"identity": str(result["stream_id"])}
        if kind in ("delete", "archive"):
            path = prepared["path"]
            if path.startswith("/attachments/"):
                attachments = (await self.http.request(account, "GET", "/attachments"))[
                    "attachments"
                ]
                return not any(
                    str(row["id"]) == path.rsplit("/", 1)[1] for row in attachments
                ), {}
            try:
                result = await self.http.request(
                    account,
                    "GET",
                    path,
                    {"apply_markdown": False} if path.startswith("/messages/") else {},
                )
            except ApiError as error:
                if self.absent(error):
                    return True, {}
                raise
            return kind == "archive" and result["stream"]["is_archived"], {}
        if kind in ("edit", "reaction", "flags"):
            remote = prepared.get("message_id", prepared["path"].split("/")[2])
            result = await self.http.request(
                account, "GET", f"/messages/{remote}", {"apply_markdown": False}
            )
            message = result["message"]
            if kind == "edit":
                return message["content"] == prepared["data"]["content"], {
                    "identity": str(remote)
                }
            if kind == "flags":
                return all(
                    (flag in message["flags"]) == value
                    for flag, value in prepared["flags"].items()
                ), {}
            present = any(
                row["user_id"] == prepared["user_id"]
                and row["emoji_code"] == prepared["data"]["emoji_code"]
                and row["reaction_type"] == prepared["data"]["reaction_type"]
                for row in message["reactions"]
            )
            return present == prepared["present"], {}
        if kind == "stream_update":
            stream = (
                await self.http.request(
                    account, "GET", f"/streams/{prepared['stream_id']}"
                )
            )["stream"]
            return all(
                stream[key] == value for key, value in prepared["data"].items()
            ), {}
        if kind == "membership":
            users = (
                await self.http.request(
                    account, "GET", f"/streams/{prepared['stream_id']}/members"
                )
            )["subscribers"]
            return (prepared["user_id"] in users) == prepared["present"], {}
        if kind == "stream_notifications":
            subscriptions = (
                await self.http.request(account, "GET", "/users/me/subscriptions")
            )["subscriptions"]
            muted = prepared["data"]["subscription_data"][0]["value"]
            return any(
                row["stream_id"] == prepared["stream_id"] and row["is_muted"] == muted
                for row in subscriptions
            ), {}
        if kind == "topic_notifications":
            topics = (await self.http.request(account, "GET", "/user_topics"))[
                "user_topics"
            ]
            values = prepared["data"]
            current = next(
                (
                    row["visibility_policy"]
                    for row in topics
                    if row["stream_id"] == values["stream_id"]
                    and row["topic_name"].casefold() == values["topic"].casefold()
                ),
                0,
            )
            return current == values["visibility_policy"], {}
        if kind in ("topic_delete", "topic_rename"):
            topics = (
                await self.http.request(
                    account, "GET", f"/users/me/{prepared['stream_id']}/topics"
                )
            )["topics"]
            names = {row["name"] for row in topics}
            done = (
                prepared["topic"] not in names
                if kind == "topic_delete"
                else prepared["old_topic"] not in names and prepared["topic"] in names
            )
            return done, {"identity": prepared.get("identity")}
        raise ValueError(f"Unsupported verification: {kind}")

    @staticmethod
    def absent(error: ApiError) -> bool:
        return (
            error.status == 404
            or error.data.get("code") == "BAD_REQUEST"
            and error.data.get("msg") in ("Invalid message(s)", "Invalid message ID")
        )

    async def outdated_edit(self, intent: dict, account: dict) -> bool:
        current = await self.db.sql(
            "SELECT content_updated_at FROM messenger_messages WHERE project_id=%s AND uuid=%s AND provider_uuid=%s",
            (intent["project_id"], intent["entity_uuid"], account["provider_uuid"]),
        )
        return bool(current) and current[0]["content_updated_at"].replace(
            tzinfo=None
        ) > datetime.datetime.fromisoformat(
            intent["data"]["content_updated_at"]
        ).replace(tzinfo=None)

    async def execute(
        self, intent: dict, account: dict, prepared: dict, checking: bool
    ) -> dict:
        kind = prepared["kind"]
        if kind == "poll_options":
            # Fetch before each write so a retry after a lost response does not
            # add the option again. Reuse Zulip's per-sender index convention.
            sender = str(account["external_account_id"])
            for label in prepared["labels"]:
                observed = await self.poll(account, prepared["message_id"])
                if label in observed.options.values():
                    continue
                index = 1 + max(
                    (
                        int(key.split(",")[1])
                        for key in observed.options
                        if key.startswith(sender + ",")
                    ),
                    default=0,
                )
                if index > 1000:
                    raise ValueError("Zulip poll option index limit reached")
                await self.http.request(
                    account,
                    "POST",
                    "/submessage",
                    {
                        "message_id": prepared["message_id"],
                        "msg_type": "widget",
                        "content": json.dumps(
                            {"type": "new_option", "idx": index, "option": label}
                        ),
                    },
                )
            observed = await self.poll(account, prepared["message_id"])
            return {
                "identity": prepared["identity"],
                "poll_revision": observed.revision,
                "poll_message_id": prepared["message_id"],
            }
        if kind == "edit" and await self.outdated_edit(intent, account):
            return {"identity": prepared["identity"]}
        if checking or kind in ("noop", "stream_create"):
            done, result = await self.verify(intent, account, prepared)
            if done:
                return result
        timeout = 30.0
        if kind == "send":
            timeout = min(timeout, self.check_send_window(intent))
        if kind == "flags":
            for flag, value in prepared["flags"].items():
                result = await self.http.request(
                    account,
                    "POST",
                    "/messages/flags",
                    {
                        "messages": [prepared["message_id"]],
                        "flag": flag,
                        "op": "add" if value else "remove",
                    },
                )
                if result.get("ignored_because_not_subscribed_channels"):
                    raise ValueError("Cannot update flags in an unsubscribed channel")
            return {}
        file = None
        if kind == "upload":
            data = prepared["file"]
            content = await asyncio.to_thread(
                file_storage.read_workspace_file,
                uuid.UUID(data["uuid"]),
                storage_type=data["storage_type"],
                storage_object_id=data["storage_object_id"],
            )
            file = (prepared["upload_name"], content, data["content_type"])
        try:
            result = await self.http.request(
                account,
                prepared["method"],
                prepared["path"],
                prepared["data"],
                file=file,
                timeout=timeout,
            )
        except ApiError as error:
            if kind in ("delete", "topic_delete", "archive") and self.absent(error):
                return {}
            if kind == "reaction" and not prepared["present"] and self.absent(error):
                return {}
            if kind in ("reaction", "membership", "archive") and error.status in (
                400,
                409,
            ):
                done, verified = await self.verify(intent, account, prepared)
                if done:
                    return verified
            raise
        if kind == "send":
            return {"identity": str(result["id"])}
        if kind == "poll_vote":
            done, verified = await self.verify(intent, account, prepared)
            if not done:
                raise Pending("Waiting for the poll vote observation")
            return verified
        if kind == "upload":
            return {"identity": urllib.parse.urlparse(result["uri"]).path}
        if kind == "stream_create":
            done, result = await self.verify(intent, account, prepared)
            if not done:
                raise Pending("Waiting for the created channel")
            return result
        if kind == "topic_delete" and not result["complete"]:
            raise Pending("Topic deletion has more batches")
        return {"identity": prepared.get("identity")}

    @staticmethod
    def check_send_window(intent: dict) -> float:
        created = datetime.datetime.fromisoformat(intent["data"]["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        remaining = (
            created
            + datetime.timedelta(hours=1)
            - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        if remaining < 0:
            raise ValueError("The one-hour message sending window has expired")
        # A zero aiohttp timeout disables the deadline rather than expiring it.
        return max(remaining, 0.000001)

    async def finish(
        self, intent: dict, account: dict, prepared: dict, result: dict
    ) -> None:
        if "poll_revision" in result:
            # Fence older prepared provider reads under the same root lock as
            # the inbox. Enqueue reconciliation before marking the intent done;
            # a crash leaves either a retryable write or a durable refresh.
            async with self.db.transaction() as connection:
                await connection.execute(
                    "SELECT uuid FROM messenger_messages WHERE project_id=%s AND uuid=%s FOR NO KEY UPDATE",
                    (intent["project_id"], intent["entity_uuid"]),
                )
                await connection.execute(
                    poll_conversion.OBSERVE_SQL,
                    (
                        account["provider_uuid"],
                        result["poll_revision"],
                        intent["project_id"],
                        intent["entity_uuid"],
                    ),
                )
                await connection.execute(
                    """INSERT INTO zulip_incoming_events
                    (project_id,external_account_uuid,provider_uuid,owner_user_uuid,account_identity,queue_id,event_id,payload,ordering_keys)
                    VALUES(%s,%s,%s,%s,%s,%s,0,%s,%s)
                    ON CONFLICT(external_account_uuid,queue_id,event_id) DO NOTHING""",
                    (
                        intent["project_id"],
                        account["uuid"],
                        account["provider_uuid"],
                        account["owner_user_uuid"],
                        account["external_account_id"],
                        f"poll:{intent['uuid']}",
                        pg_json.Jsonb(
                            {
                                "type": "poll_refresh",
                                "message_id": result["poll_message_id"],
                                "outgoing_intent_uuid": str(intent["uuid"]),
                            }
                        ),
                        [f"message:{result['poll_message_id']}"],
                    ),
                )
        identity = result.get("identity")
        if "delivery_uuid" in prepared:
            receipt = await self.db.sql(
                COMPLETE_SEND_RECEIPT_SQL,
                (
                    intent["project_id"],
                    intent["entity_uuid"],
                    identity,
                    prepared["delivery_uuid"],
                    account["provider_uuid"],
                ),
            )
            identity = receipt[0]["identity"]
        if intent["entity_type"] == "file" and identity is not None:
            # Publish the access receipt before the identity makes the file
            # leave the pending-upload visibility branch.
            await self.db.sql(
                SAVE_FILE_ACCESS_SQL,
                (
                    intent["project_id"],
                    intent["entity_uuid"],
                    account["provider_uuid"],
                    account["uuid"],
                    identity,
                    pg_json.Jsonb({"account_identity": account["external_account_id"]}),
                ),
            )
        if identity is not None and intent["entity_type"] != "message_state":
            if "delivery_uuid" not in prepared:
                await self.db.sql(
                    UPDATE_ENTITY_IDENTITY_SQL.format(
                        table=TABLES[intent["entity_type"]]
                    ),
                    (
                        identity,
                        intent["project_id"],
                        intent["entity_uuid"],
                        account["provider_uuid"],
                        identity,
                    ),
                )
            await self.db.sql(
                SAVE_RECEIPT_SQL,
                (
                    identity,
                    intent["project_id"],
                    intent["entity_type"],
                    intent["entity_uuid"],
                    account["uuid"],
                    intent["id"],
                ),
            )
        if intent["entity_type"] == "file" and identity is not None:
            data = intent["data"]
            await self.db.sql(
                INSERT_FILE_LINK_SQL,
                (
                    uuid.uuid5(intent["entity_uuid"], str(account["provider_uuid"])),
                    intent["project_id"],
                    data["stream_uuid"],
                    intent["entity_uuid"],
                    identity,
                    account["provider_uuid"],
                    intent["entity_uuid"],
                    intent["project_id"],
                ),
            )
        await self.status(intent, "active")
        if intent["entity_type"] == "file" and intent["operation"] == "delete":
            data = intent["data"]
            references = await self.db.sql(
                SELECT_FILE_REFERENCES_SQL,
                (
                    data["storage_type"],
                    data["storage_id"],
                    data["storage_object_id"],
                    data["storage_type"],
                    data["storage_id"],
                    data["storage_object_id"],
                ),
            )
            if not references:
                await asyncio.to_thread(
                    file_storage.delete_workspace_file,
                    uuid.UUID(data["uuid"]),
                    storage_type=data["storage_type"],
                    storage_object_id=data["storage_object_id"],
                )
            if not any(
                row["kind"] == "file" and str(row["uuid"]) == data["uuid"]
                for row in references
            ):
                await asyncio.to_thread(
                    file_storage.delete_workspace_file_metadata,
                    uuid.UUID(data["uuid"]),
                    storage_type=data["storage_type"],
                )
            if references and not any(row["kind"] == "file" for row in references):
                raise Pending("Waiting for unfinished uploads before deleting storage")
        await self.db.sql(COMPLETE_INTENT_SQL, (intent["id"],))
