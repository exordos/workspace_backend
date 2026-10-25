# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Batch comparison for the independent history process; domain writes stay shared."""

import datetime
import typing

from workspace.bridges.zulip import incoming_operations
from workspace.bridges.zulip import message_conversion
from workspace.bridges.zulip import outgoing_operations


REQUEST_SQL = """
    INSERT INTO zulip_history_jobs (
        external_account_uuid,project_id,provider_uuid,owner_user_uuid,
        account_identity,server_url,registration,registration_queue,
        registration_started_at,subscribed_streams
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (external_account_uuid) DO UPDATE SET
        project_id=EXCLUDED.project_id,provider_uuid=EXCLUDED.provider_uuid,
        owner_user_uuid=EXCLUDED.owner_user_uuid,
        account_identity=EXCLUDED.account_identity,server_url=EXCLUDED.server_url,
        requested_at=clock_timestamp(),registration=EXCLUDED.registration,
        registration_queue=EXCLUDED.registration_queue,
        subscribed_streams=EXCLUDED.subscribed_streams,
        registration_started_at=EXCLUDED.registration_started_at,catalog_position=0,
        flag_after_id=0,
        next_attempt_at=now(),error=NULL,
        newest_id=CASE WHEN {same} THEN zulip_history_jobs.newest_id ELSE 0 END,
        target_id=CASE WHEN {same} THEN zulip_history_jobs.target_id END,
        before_id=CASE WHEN {same} THEN zulip_history_jobs.before_id END,
        backfill_done=CASE WHEN {same} THEN zulip_history_jobs.backfill_done ELSE false END,
        repair_before_id=CASE WHEN {same} THEN zulip_history_jobs.repair_before_id END,
        completed_at=CASE WHEN {same} THEN zulip_history_jobs.completed_at ELSE 'epoch' END
""".format(
    same="""
    (zulip_history_jobs.project_id,zulip_history_jobs.provider_uuid,
     zulip_history_jobs.owner_user_uuid,zulip_history_jobs.account_identity,
     zulip_history_jobs.server_url) =
    (EXCLUDED.project_id,EXCLUDED.provider_uuid,EXCLUDED.owner_user_uuid,
     EXCLUDED.account_identity,EXCLUDED.server_url)
"""
)

SELECT_MESSAGES_SQL = """
    SELECT message.provider_identity AS remote_id,
           jsonb_build_object(
               'message',to_jsonb(message) || jsonb_build_object(
                   'payload',contents.workspace_content,
                   'provider_content',contents.provider_content
               ),'placement',to_jsonb(placement),
               'stream',to_jsonb(stream),'topic',to_jsonb(topic),
               -- Counter publications must not invalidate unrelated messages.
               -- Keep membership identity, generation and access controls.
               'membership',to_jsonb(member)-ARRAY[
                   'updated_at','unread_count','active_unread_count',
                   'passive_unread_count','last_message_uuid'
               ],'access',to_jsonb(access),
               'state',to_jsonb(state),'reactions',COALESCE(reactions.items,'[]')
           ) AS snapshot
    FROM messenger_messages message
    JOIN messenger_message_contents contents ON contents.uuid=message.content_uuid
    LEFT JOIN messenger_message_placements placement
      ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    LEFT JOIN messenger_streams stream
      ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
    LEFT JOIN messenger_topics topic
      ON topic.project_id=placement.project_id AND topic.uuid=placement.topic_uuid
    LEFT JOIN messenger_stream_bindings member
      ON member.project_id=stream.project_id AND member.stream_uuid=stream.uuid
     AND member.user_uuid=%s
    LEFT JOIN messenger_user_message_bindings access
      ON access.project_id=placement.project_id AND access.placement_uuid=placement.uuid
     AND access.user_uuid=%s
    LEFT JOIN messenger_user_message_states state
      ON state.project_id=placement.project_id AND state.placement_uuid=placement.uuid
     AND state.user_uuid=%s
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'provider_identity',provider_identity,'emoji_name',emoji_name,
            'user_uuid',user_uuid,'updated_at',updated_at
        ) ORDER BY provider_identity) AS items
        FROM messenger_message_reaction_facts
        WHERE project_id=message.project_id AND canonical_message_uuid=message.uuid
          AND source_name='zulip' AND provider_uuid=message.provider_uuid
    ) reactions ON true
    WHERE message.project_id=%s AND message.provider_uuid=%s
      AND message.source_name='zulip' AND message.provider_identity=ANY(%s::text[])
"""
SELECT_FILES_SQL = """
    SELECT file.*, EXISTS (
        SELECT 1 FROM zulip_entity_links link
        WHERE link.entity_type='file' AND link.entity_uuid=file.uuid
          AND link.provider_uuid=file.provider_uuid AND link.external_account_uuid=%s
          AND link.data->>'account_identity'=%s
    ) AS linked
    FROM m_workspace_files file
    WHERE file.project_id=%s AND file.provider_uuid=%s
      AND file.provider_identity=ANY(%s::text[])
"""
SELECT_OUTBOUND_CONTENT_SQL = """
    SELECT DISTINCT ON (delivery.message_uuid,contents.provider_content)
           delivery.message_uuid,contents.provider_content AS zulip_content,
           contents.workspace_content
    FROM bridge_zulip_v5_content_snapshots snapshot
    JOIN bridge_zulip_v5_deliveries delivery ON delivery.uuid=snapshot.delivery_uuid
    JOIN messenger_message_contents contents ON contents.uuid=snapshot.content_uuid
    WHERE delivery.project_id=%s AND delivery.message_uuid=ANY(%s::uuid[])
    ORDER BY delivery.message_uuid,contents.provider_content,snapshot.created_at DESC
"""
SELECT_CATALOG_PEOPLE_SQL = """
    SELECT account.external_account_id,users.*,account.owner_user_uuid
    FROM m_external_accounts_v2 account
    JOIN m_workspace_users users ON users.uuid=account.owner_user_uuid
    WHERE account.project_id=%s AND account.provider_uuid=%s
      AND account.external_account_id=ANY(%s::text[])
"""
SELECT_CATALOG_STREAMS_SQL = """
    SELECT stream.*,to_jsonb(member) AS membership
    FROM messenger_streams stream
    LEFT JOIN messenger_stream_bindings member
      ON member.project_id=stream.project_id AND member.stream_uuid=stream.uuid
     AND member.user_uuid=%s
    WHERE stream.project_id=%s AND stream.provider_uuid=%s AND stream.source_name='zulip'
"""
SELECT_CATALOG_ATTACHMENTS_SQL = """
    SELECT attachment.* FROM zulip_incoming_attachments attachment
    WHERE project_id=%s AND provider_uuid=%s AND attachment_id=ANY(%s::bigint[])
"""
SELECT_REMOVED_ATTACHMENTS_SQL = """
    SELECT attachment.* FROM zulip_incoming_attachments attachment
    WHERE project_id=%s AND provider_uuid=%s AND owner_identity=%s
      AND NOT(attachment_id=ANY(%s::bigint[])) AND updated_at<=%s
      AND NOT EXISTS (
          SELECT 1 FROM m_workspace_files file
          WHERE file.project_id=attachment.project_id AND file.provider_uuid=attachment.provider_uuid
            AND file.provider_identity=attachment.path AND file.updated_at>%s
      )
      AND NOT(attachment_id=ANY(%s::bigint[]))
    ORDER BY attachment_id LIMIT %s
"""
SELECT_CATALOG_TOPICS_SQL = """
    SELECT topic.provider_identity,personal.notification_mode
    FROM messenger_topics topic
    JOIN messenger_user_topic_bindings personal
      ON personal.project_id=topic.project_id AND personal.topic_uuid=topic.uuid
     AND personal.user_uuid=%s
    WHERE topic.project_id=%s AND topic.provider_uuid=%s
      AND topic.provider_identity=ANY(%s::text[])
"""
SELECT_FLAG_PAGE_SQL = """
    SELECT message.provider_identity::bigint AS remote_id,
           state.read_at IS NOT NULL AS read,state.starred
    FROM messenger_user_message_states state
    JOIN messenger_message_placements placement
      ON placement.project_id=state.project_id AND placement.uuid=state.placement_uuid
    JOIN messenger_messages message
      ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid
    WHERE state.project_id=%s AND state.user_uuid=%s AND message.provider_uuid=%s
      AND message.source_name='zulip' AND message.provider_identity ~ '^[0-9]+$'
      AND message.provider_identity::bigint>%s AND message.deleted_at IS NULL
    ORDER BY message.provider_identity::bigint LIMIT %s
"""


def parameters(account: dict, identifiers: list) -> tuple:
    return (
        account["owner_user_uuid"],
        account["owner_user_uuid"],
        account["owner_user_uuid"],
        account["project_id"],
        account["provider_uuid"],
        [str(value) for value in identifiers],
    )


def timestamp(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    return (
        parsed.replace(tzinfo=datetime.timezone.utc)
        if parsed.tzinfo is None
        else parsed.astimezone(datetime.timezone.utc)
    )


def recently_changed(snapshot: dict | None, since: datetime.datetime) -> bool:
    if snapshot is None:
        return False
    objects = [
        snapshot[name]
        for name in (
            "message",
            "placement",
            "stream",
            "topic",
            "access",
            "state",
        )
        if snapshot[name] is not None
    ]
    objects.extend(snapshot["reactions"])
    return any(timestamp(item["updated_at"]) > since for item in objects)


def catalog_events(registration: dict) -> list[dict]:
    events = [
        {"type": "realm_user", "op": "add", "person": item}
        for item in registration.get("realm_users", [])
    ]
    events.extend(
        {"type": "subscription", "op": "add", "subscriptions": [item]}
        for item in registration.get("subscriptions", [])
    )
    events.extend(
        {"type": "attachment", "op": "add", "attachment": item}
        for item in registration.get("attachments", [])
    )
    events.extend(
        {"type": "user_topic", **item} for item in registration.get("user_topics", [])
    )
    return events


def entity_key(event: dict) -> str:
    kind = event["type"]
    if kind == "message":
        return f"message:{event['message']['id']}"
    if kind == "history_flags":
        return f"flags:{event['message_id']}"
    if kind == "history_absent":
        return f"absent:{event['message_id']}"
    if kind == "realm_user":
        return f"user:{event['person']['user_id']}"
    if kind == "subscription":
        return f"subscription:{event['subscriptions'][0]['stream_id']}"
    if kind == "user_topic":
        return f"topic:{event['stream_id']}:{event['topic_name']}"
    return f"attachment:{event['attachment']['id']}"


class Operations(incoming_operations.Operations):
    emit_public_event = False

    async def snapshots(
        self, account: dict, identifiers: list, connection: typing.Any = None
    ) -> dict[str, dict]:
        args = parameters(account, identifiers)
        found = (
            await self.db.sql(SELECT_MESSAGES_SQL, args)
            if connection is None
            else await incoming_operations.rows(connection, SELECT_MESSAGES_SQL, args)
        )
        return {row["remote_id"]: row["snapshot"] for row in found}

    async def differences(
        self, account: dict, messages: list, subscribed_streams: list | None = None
    ) -> list[tuple[dict, dict | None]]:
        """Read each relation in batches, then compare without per-message SQL/HTTP."""
        memberships = await self.db.sql(
            SELECT_CATALOG_STREAMS_SQL,
            (
                account["owner_user_uuid"],
                account["project_id"],
                account["provider_uuid"],
            ),
        )
        members = {
            item["provider_identity"]: item["membership"] for item in memberships
        }
        messages = [
            item
            for item in messages
            if (
                member["active"]
                if (
                    member := members.get(
                        incoming_operations.stream_identity(account, item)
                    )
                )
                is not None
                else item["type"] != "stream"
                or subscribed_streams is None
                or item["stream_id"] in subscribed_streams
            )
        ]
        states = await self.snapshots(account, [item["id"] for item in messages])
        known = [item for item in messages if str(item["id"]) in states]
        if not known:
            return [(item, None) for item in messages]
        scope = (
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
        )
        refs_ = incoming_operations.references(known)
        catalog = await self.db.sql(
            incoming_operations.SELECT_CATALOG_SQL,
            (*scope[:2], refs_["people"], refs_["names"]),
        )
        streams = [
            item
            for item in memberships
            if item["membership"]
            and item["membership"]["active"]
            and item["deleted_at"] is None
        ]
        topics = (
            await self.db.sql(
                incoming_operations.SELECT_TOPIC_REFERENCES_SQL,
                (*scope, refs_["remote_topics"], refs_["named_topics"]),
            )
            if refs_["remote_topics"] or refs_["named_topics"]
            else []
        )
        references = refs_["messages"]
        refs = (
            await self.db.sql(
                incoming_operations.SELECT_MESSAGE_REFERENCES_SQL,
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    list(references),
                    account["provider_uuid"],
                ),
            )
            if references
            else []
        )
        paths = set()
        targets = {}
        for item in known:
            for target in message_conversion.get_zulip_files(item, {}):
                endpoint = message_conversion.parse_url(
                    target, account["settings"]["server_url"]
                )
                if (
                    endpoint
                    and endpoint.hostname == account["hostname"]
                    and endpoint.path.startswith("/user_uploads/")
                ):
                    paths.add(endpoint.path)
                    targets[target] = endpoint.path
        files = (
            await self.db.sql(
                SELECT_FILES_SQL,
                (
                    account["uuid"],
                    account["external_account_id"],
                    account["project_id"],
                    account["provider_uuid"],
                    list(paths),
                ),
            )
            if paths
            else []
        )
        outgoing = await self.db.sql(
            SELECT_OUTBOUND_CONTENT_SQL,
            (
                account["project_id"],
                [states[str(item["id"])]["message"]["uuid"] for item in known],
            ),
        )
        preserved = {
            (str(row["message_uuid"]), row["zulip_content"]): row["workspace_content"]
            for row in outgoing
        }
        # Quotes use the same source-message context as ordinary incoming preparation.
        quoted = {}
        if any("quote" in item["content"] for item in known):
            page = {str(item["id"]): item for item in messages}
            for identifier in references:
                if identifier in page:
                    quoted[identifier] = page[identifier]
                else:
                    try:
                        quoted[identifier] = (
                            await self.http.request(
                                account,
                                "GET",
                                f"/messages/{identifier}",
                                {"apply_markdown": False},
                            )
                        )["message"]
                    except outgoing_operations.ApiError as error:
                        if (
                            error.status != 403
                            and not outgoing_operations.Operations.absent(error)
                        ):
                            raise
        prepared = {
            "catalog": catalog,
            "streams": streams,
            "topics": topics,
            "references": refs,
            "quoted_messages": quoted,
        }
        users = {
            str(item["external_account_id"]): str(item["owner_user_uuid"])
            for item in catalog
        }
        changes = []
        for item in messages:
            old = states.get(str(item["id"]))
            if old is None or not self.matches(
                account, item, old, prepared, users, files, targets, preserved
            ):
                changes.append((item, old))
        return changes

    @staticmethod
    def matches(
        account: dict,
        data: dict,
        old: dict,
        prepared: dict,
        users: dict,
        files: list,
        targets: dict,
        preserved: dict,
    ) -> bool:
        message, stream, topic = old["message"], old["stream"], old["topic"]
        if message["deleted_at"] is not None:
            return True
        if stream is None or topic is None or old["placement"] is None:
            return False
        remote = incoming_operations.stream_identity(account, data)
        subject = data["subject"] if data["type"] == "stream" else ""
        if (
            stream["provider_identity"] != remote
            or topic["provider_identity"] != f"{remote}:{subject}"
        ):
            return False
        if (
            message["author_uuid"] != users.get(str(data["sender_id"]))
            or message["status"] != "active"
        ):
            return False
        context = incoming_operations.conversion_context(
            account, data, stream, topic, old["placement"]["uuid"], prepared
        )
        file_map = {
            row["provider_identity"]: row
            for row in files
            if str(row["stream_uuid"]) == stream["uuid"] or row["acl_mode"] == "public"
        }
        for target in message_conversion.get_zulip_files(data, {}):
            if target in targets:
                file = file_map.get(targets[target])
                if file is None or not file["linked"]:
                    return False
                context["files"][target] = f"urn:file:{file['uuid']}"
        payload = message_conversion.zulip_to_workspace(data, context)
        saved_content = preserved.get((message["uuid"], data["content"]))
        if saved_content is not None:
            payload = saved_content
        edited = datetime.datetime.fromtimestamp(
            data.get("last_edit_timestamp") or data["timestamp"], datetime.timezone.utc
        )
        if edited >= timestamp(message["content_updated_at"]) and (
            message["payload"] != payload
            or message["provider_content"] != data["content"]
        ):
            return False
        membership, access, state = old["membership"], old["access"], old["state"]
        if not membership or not membership["active"] or not access or not state:
            return False
        if (
            access["membership_generation"] != membership["membership_generation"]
            or state["membership_generation"] != membership["membership_generation"]
            or access["visibility"] != "visible"
            or state["provider_identity"]
            != f"{data['id']}:{account['external_account_id']}"
        ):
            return False
        if (state["read_at"] is not None) != ("read" in data["flags"]) or state[
            "starred"
        ] != ("starred" in data["flags"]):
            return False
        identities = {
            f"{data['id']}:{item['user_id']}:{item['reaction_type']}:{item['emoji_code']}"
            for item in data["reactions"]
        }
        return identities == {item["provider_identity"] for item in old["reactions"]}

    async def catalog_differences(self, account: dict, events: list) -> list[dict]:
        scope = (account["project_id"], account["provider_uuid"])
        user_ids = [
            str(item["person"]["user_id"])
            for item in events
            if item["type"] == "realm_user"
        ]
        attachment_ids = [
            item["attachment"]["id"] for item in events if item["type"] == "attachment"
        ]
        topic_ids = [
            f"{item['stream_id']}:{item['topic_name']}"
            for item in events
            if item["type"] == "user_topic"
        ]
        people = {
            row["external_account_id"]: row
            for row in await self.db.sql(SELECT_CATALOG_PEOPLE_SQL, (*scope, user_ids))
        }
        streams = {
            row["provider_identity"]: row
            for row in await self.db.sql(
                SELECT_CATALOG_STREAMS_SQL, (account["owner_user_uuid"], *scope)
            )
        }
        attachments = {
            row["attachment_id"]: row
            for row in await self.db.sql(
                SELECT_CATALOG_ATTACHMENTS_SQL, (*scope, attachment_ids)
            )
        }
        topics = (
            {
                row["provider_identity"]: row["notification_mode"]
                for row in await self.db.sql(
                    SELECT_CATALOG_TOPICS_SQL,
                    (account["owner_user_uuid"], *scope, topic_ids),
                )
            }
            if topic_ids
            else {}
        )
        changes = []
        for event in events:
            kind = event["type"]
            equal = False
            if kind == "realm_user":
                data = event["person"]
                old = people.get(str(data["user_id"]))
                equal = old is not None and (
                    old["source"] not in ("zulip", "hidden")
                    or (
                        old["first_name"] == data["full_name"]
                        and old["email"] == data.get("email")
                        and old["disabled"] == (not data.get("is_active", True))
                    )
                )
            elif kind == "subscription":
                data = event["subscriptions"][0]
                old = streams.get(str(data["stream_id"]))
                equal = old is not None and old["membership"] is not None
                if old and old["membership"] and event["op"] == "remove":
                    equal = not old["membership"]["active"]
                elif old and old["membership"]:
                    equal = (
                        old["membership"]["active"]
                        and all(
                            old[key] == data[key]
                            for key in (
                                "name",
                                "description",
                                "invite_only",
                                "is_archived",
                            )
                            if key in data
                        )
                        and old["membership"]["notification_mode"]
                        == (
                            "mentions_only"
                            if data.get("is_muted", False)
                            else "all_messages"
                        )
                    )
            elif kind == "attachment":
                data = event["attachment"]
                old = attachments.get(data["id"])
                equal = (
                    old is None
                    if event["op"] == "remove"
                    else (
                        old is not None
                        and old["name"] == data["name"]
                        and old["path"] == "/user_uploads/" + data["path_id"]
                        and old["owner_identity"] == account["external_account_id"]
                    )
                )
            elif kind == "user_topic":
                equal = (
                    topics.get(f"{event['stream_id']}:{event['topic_name']}")
                    == {0: "default", 1: "mute", 2: "unmute", 3: "follow"}[
                        event["visibility_policy"]
                    ]
                )
            if not equal:
                changes.append(event)
        return changes

    async def prepare(self, account: dict, event: dict) -> dict:
        if event["type"] in ("history_flags", "history_absent"):
            return {"event": event}
        return await super().prepare(account, event)

    async def catalog_objects(
        self, connection: typing.Any, account: dict, event: dict
    ) -> list[dict]:
        kind = event["type"]
        if kind == "attachment":
            path = "/user_uploads/" + event["attachment"]["path_id"]
            return await incoming_operations.rows(
                connection,
                incoming_operations.SELECT_FILE_SQL,
                (account["project_id"], account["provider_uuid"], path),
            )
        if kind == "realm_user":
            users = await incoming_operations.rows(
                connection,
                SELECT_CATALOG_PEOPLE_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    [str(event["person"]["user_id"])],
                ),
            )
            return users
        remote = (
            event["subscriptions"][0]["stream_id"]
            if kind == "subscription"
            else event["stream_id"]
        )
        identifier = await self.identity(
            account, "stream", remote, connection=connection
        )
        objects = await incoming_operations.rows(
            connection,
            incoming_operations.SELECT_ROW_SQL.format(table="messenger_streams"),
            (account["project_id"], identifier),
        )
        objects.extend(
            await incoming_operations.rows(
                connection,
                incoming_operations.SELECT_BINDING_SQL,
                (account["project_id"], identifier, account["owner_user_uuid"]),
            )
        )
        if kind == "user_topic":
            topic = await self.identity(
                account,
                "topic",
                f"{remote}:{event['topic_name']}",
                connection=connection,
            )
            objects.extend(
                await incoming_operations.rows(
                    connection,
                    incoming_operations.SELECT_TOPIC_BINDING_SQL,
                    (account["project_id"], account["owner_user_uuid"], topic),
                )
            )
        return objects

    async def apply(
        self, connection: typing.Any, account: dict, prepared: dict
    ) -> None:
        event = prepared["event"]
        if event["type"] == "history_flags":
            await self.flags(
                connection,
                account,
                [event["message_id"]],
                event["flags"],
                snapshot=True,
            )
        elif event["type"] == "history_absent":
            await self.remove_access(connection, account, event["message_id"])
        else:
            await super().apply(connection, account, prepared)

    async def remove_access(
        self, connection: typing.Any, account: dict, remote: int
    ) -> None:
        states = await self.snapshots(account, [remote], connection)
        old = states.get(str(remote))
        if not old or not old["access"] or not old["placement"]:
            return
        access = old["access"]
        if access["visibility"] == "hidden":
            return
        await self.save(
            connection,
            "messenger_user_message_bindings",
            {
                "project_id": account["project_id"],
                "uuid": access["uuid"],
                "visibility": "hidden",
            },
        )
        await self.publish(
            connection,
            account,
            "message.deleted",
            old["placement"]["uuid"],
            [old["message"]],
            old["placement"]["stream_uuid"],
            recipients=[account["owner_user_uuid"]],
        )
        await self.counters(
            connection, account, old["placement"], user=account["owner_user_uuid"]
        )
