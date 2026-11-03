# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Shared canonical Messenger persistence and fenced event publication."""

import datetime
import json
import typing
import uuid

from psycopg.types import json as pg_json

from workspace.messenger_api import publication

TABLES = {
    "stream": "messenger_streams",
    "stream_binding": "messenger_stream_bindings",
    "topic": "messenger_topics",
    "topic_binding": "messenger_user_topic_bindings",
    "message": "messenger_messages",
    "reaction": "messenger_message_reaction_facts",
    "file": "m_workspace_files",
}

SELECT_ROW_SQL = "SELECT * FROM {table} WHERE project_id = %s AND uuid = %s"

INSERT_ROW_SQL = """
    INSERT INTO {table} ({columns}) VALUES ({parameters})
    ON CONFLICT ({conflict}) DO UPDATE SET {assignments}
    RETURNING *
"""

UPDATE_ROW_SQL = """
    UPDATE {table} SET {assignments}
    WHERE project_id = %s AND uuid = %s RETURNING *
"""

SELECT_MEMBERS_SQL = """
    SELECT * FROM messenger_stream_bindings
    WHERE project_id = %s AND stream_uuid = %s AND active
    ORDER BY user_uuid
"""

SELECT_SNAPSHOT_SQL = """
    SELECT * FROM {view} WHERE project_id = %s AND uuid = %s
"""

ENQUEUE_COUNTERS_SQL = "SELECT messenger_queue_counters(%s,%s,%s,%s)"

VIEWS = {
    "stream": "messenger_api_user_streams_v1",
    "topic": "messenger_api_user_topics_v1",
    "message": "messenger_api_user_messages_v1",
}


def json_value(value: object) -> pg_json.Jsonb:
    def encode(item: object) -> str:
        if isinstance(item, datetime.datetime):
            if item.tzinfo is None:
                item = item.replace(tzinfo=datetime.timezone.utc)
            return (
                item.astimezone(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        return str(item)

    return pg_json.Jsonb(json.loads(json.dumps(value, default=encode)))


async def rows(
    connection: typing.Any, query: str, parameters: tuple = ()
) -> list[dict]:
    return await (await connection.execute(query, parameters)).fetchall()


SELECT_IDENTITY_SQL = "SELECT uuid FROM {table} WHERE project_id=%s AND provider_uuid=%s AND provider_identity=%s AND source_name=%s"


def canonical_uuid(account: dict, kind: str, identity: object) -> uuid.UUID:
    return uuid.uuid5(
        account["project_id"], f"{account['provider_uuid']}:{kind}:{identity}"
    )


class Operations:
    db: typing.Any
    source_name = "zulip"
    emit_public_event = True

    async def identity(
        self,
        account: dict,
        kind: str,
        identity: object,
        *,
        connection: typing.Any = None,
    ) -> uuid.UUID:
        remote = str(identity)
        query = SELECT_IDENTITY_SQL.format(table=TABLES[kind])
        parameters = (
            account["project_id"],
            account["provider_uuid"],
            remote,
            self.source_name,
        )
        found = (
            await self.db.sql(query, parameters)
            if connection is None
            else await rows(connection, query, parameters)
        )
        return found[0]["uuid"] if found else canonical_uuid(account, kind, remote)

    async def save(
        self,
        connection: typing.Any,
        table: str,
        values: dict,
        *,
        conflict: str = "project_id, uuid",
    ) -> dict | None:
        if table == "m_workspace_files":
            conflict = "uuid"
        old = await rows(
            connection,
            SELECT_ROW_SQL.format(table=table),
            (
                values["project_id"],
                values["uuid"],
            ),
        )
        values = dict(values)
        if old:
            values.pop("created_at", None)
            if all(
                (
                    old[0][key].replace(tzinfo=None) == value.replace(tzinfo=None)
                    if isinstance(value, datetime.datetime)
                    and isinstance(old[0][key], datetime.datetime)
                    else old[0][key] == value
                )
                for key, value in values.items()
            ):
                return None
        if not old:
            values.setdefault(
                "created_at", datetime.datetime.now(datetime.timezone.utc)
            )
        values["updated_at"] = datetime.datetime.now(datetime.timezone.utc)
        if old:
            updates = {
                key: value
                for key, value in values.items()
                if key not in ("uuid", "project_id")
            }
            saved = await rows(
                connection,
                UPDATE_ROW_SQL.format(
                    table=table,
                    assignments=", ".join(f"{key} = %s" for key in updates),
                ),
                tuple(
                    json_value(value) if isinstance(value, (dict, list)) else value
                    for value in updates.values()
                )
                + (values["project_id"], values["uuid"]),
            )
            return saved[0]
        columns = list(values)
        assignments = ", ".join(
            f"{name} = EXCLUDED.{name}"
            for name in columns
            if name not in ("uuid", "project_id", "created_at")
        )
        saved = await rows(
            connection,
            INSERT_ROW_SQL.format(
                table=table,
                columns=", ".join(columns),
                parameters=", ".join(["%s"] * len(columns)),
                conflict=conflict,
                assignments=assignments,
            ),
            tuple(
                json_value(value) if isinstance(value, (dict, list)) else value
                for value in values.values()
            ),
        )
        return saved[0]

    async def publish(
        self,
        connection: typing.Any,
        account: dict,
        kind: str,
        entity_uuid: object,
        snapshots: list[dict],
        stream_uuid: object = None,
        *,
        recipients: list | None = None,
        control_effect: bool = False,
    ) -> None:
        if not self.emit_public_event or not snapshots:
            return
        recipient_snapshots = recipients is None
        if recipients is None:
            recipients = [item["user_uuid"] for item in snapshots]
        snapshots = [dict(item) for item in snapshots]
        for snapshot in snapshots:
            snapshot.pop("private_index", None)
            snapshot.pop("canonical_message_uuid", None)
        common = {
            key: value
            for key, value in snapshots[0].items()
            if (key != "user_uuid" or not recipient_snapshots)
            and all(item.get(key) == value for item in snapshots)
        }
        overrides = {
            str(user): {
                key: value
                for key, value in snapshot.items()
                if (key != "user_uuid" or not recipient_snapshots) and key not in common
            }
            for user, snapshot in zip(recipients, snapshots)
        }
        generations = {}
        if stream_uuid is not None:
            generations = {
                str(member["user_uuid"]): member["membership_generation"]
                for member in await rows(
                    connection,
                    SELECT_MEMBERS_SQL,
                    (
                        account["project_id"],
                        stream_uuid,
                    ),
                )
            }
        recipients = sorted(set(recipients), key=str)
        event_uuid = uuid.uuid4()
        await connection.execute(
            publication.ENQUEUE_SQL,
            (
                event_uuid,
                account["project_id"],
                entity_uuid,
                kind,
                recipients,
                json_value(common),
                json_value(overrides),
                False,
            ),
        )
        if stream_uuid is not None:
            if control_effect:
                for user in recipients:
                    generations.setdefault(str(user), 1)
                exists = await rows(
                    connection,
                    "SELECT 1 FROM messenger_streams WHERE project_id=%s AND uuid=%s",
                    (account["project_id"], stream_uuid),
                )
                if not exists:
                    stream_uuid = None
            await connection.execute(
                """INSERT INTO messenger_event_membership_guards
                    (event_uuid,project_id,user_uuid,stream_uuid,
                     membership_generation,control_effect)
                    SELECT %s,%s,input.user_uuid,%s,input.generation,%s
                    FROM unnest(%s::uuid[],%s::integer[]) AS input(user_uuid,generation)
                    ON CONFLICT (event_uuid,user_uuid) DO NOTHING""",
                (
                    event_uuid,
                    account["project_id"],
                    stream_uuid,
                    control_effect,
                    recipients,
                    [generations[str(user)] for user in recipients],
                ),
            )

    async def snapshot(
        self,
        connection: typing.Any,
        account: dict,
        kind: str,
        identifier: object,
        action: str,
        stream_uuid: object,
    ) -> None:
        snapshots = await rows(
            connection,
            SELECT_SNAPSHOT_SQL.format(view=VIEWS[kind]),
            (
                account["project_id"],
                identifier,
            ),
        )
        await self.publish(
            connection, account, f"{kind}.{action}", identifier, snapshots, stream_uuid
        )

    async def counters(
        self,
        connection: typing.Any,
        account: dict,
        placement: dict,
        *,
        user: object | None = None,
    ) -> None:
        members = (
            [{"user_uuid": user}]
            if user is not None
            else await rows(
                connection,
                SELECT_MEMBERS_SQL,
                (account["project_id"], placement["stream_uuid"]),
            )
        )
        users = sorted({member["user_uuid"] for member in members}, key=str)
        for user_uuid in users:
            await connection.execute(
                ENQUEUE_COUNTERS_SQL,
                (
                    account["project_id"],
                    user_uuid,
                    placement["stream_uuid"],
                    placement["topic_uuid"],
                ),
            )
