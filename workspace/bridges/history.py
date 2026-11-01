# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Shared bounded history admission, COPY staging and canonical bulk persistence."""

import logging
import time
import typing

from psycopg import sql
from workspace.bridges import messenger

LOG = logging.getLogger(__name__)


BUSY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM zulip_incoming_events WHERE project_id=%s
          AND processed_at IS NULL AND next_retry_at<=now()
          AND received_at<now()-%s*interval '1 second'
    ) OR EXISTS (
        SELECT 1 FROM telegram_incoming_events event
        JOIN m_external_accounts_v2 account ON account.uuid=event.external_account_uuid
        WHERE account.project_id=%s AND event.processed_at IS NULL
          AND event.received_at<now()-%s*interval '1 second'
    ) OR (SELECT count(*) FROM (
        SELECT 1 FROM messenger_publication_outbox WHERE project_id=%s LIMIT %s
    ) pending)>=%s AS busy
"""


async def busy(
    db: typing.Any, project: object, publication_limit: int, realtime_delay: float
) -> bool:
    return (
        await db.sql(
            BUSY_SQL,
            (
                project,
                realtime_delay,
                project,
                realtime_delay,
                project,
                publication_limit,
                publication_limit,
            ),
        )
    )[0]["busy"]


def page_before(identifiers: list[int], anchor: int, provider: str) -> int:
    if anchor and any(value >= anchor for value in identifiers):
        raise ValueError(f"{provider} history page did not advance below its anchor")
    return min(identifiers, default=0)


async def execute(
    connection: typing.Any, operation: str, query: str, parameters: tuple = ()
) -> None:
    started = time.monotonic()
    cursor = await connection.execute(query, parameters)
    LOG.info(
        "History SQL operation=%s duration_ms=%.1f rows=%s",
        operation,
        1000 * (time.monotonic() - started),
        cursor.rowcount,
    )


async def stage(connection: typing.Any, table: str, values: list[dict]) -> None:
    """A private relation with the same columns/defaults as the working table."""
    started = time.monotonic()
    await connection.execute(
        sql.SQL(
            "CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS INCLUDING GENERATED) ON COMMIT DROP"
        ).format(sql.Identifier("history_" + table), sql.Identifier(table))
    )
    created = time.monotonic()
    if not values:
        LOG.info(
            "History stage table=%s rows=0 create_ms=%.1f copy_ms=0",
            table,
            1000 * (created - started),
        )
        return
    columns = list(values[0])
    query = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier("history_" + table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
    )
    async with connection.cursor().copy(query) as copy:
        for value in values:
            await copy.write_row(
                tuple(
                    messenger.json_value(value[key])
                    if isinstance(value[key], (dict, list))
                    else value[key]
                    for key in columns
                )
            )

    LOG.info(
        "History stage table=%s rows=%s create_ms=%.1f copy_ms=%.1f",
        table,
        len(values),
        1000 * (created - started),
        1000 * (time.monotonic() - created),
    )


async def merge(
    connection: typing.Any,
    table: str,
    columns: list[str],
    update: typing.Sequence[str] = (),
    key: tuple[str, ...] = ("project_id", "uuid"),
    condition: str = "true",
) -> None:
    # Names are internal constants, never provider input.
    names = ",".join(columns)
    match = " AND ".join(f"dst.{name}=src.{name}" for name in key)
    await execute(
        connection,
        "insert." + table,
        f"""
        INSERT INTO {table} ({names}) SELECT {",".join("src." + c for c in columns)}
        FROM history_{table} src LEFT JOIN {table} dst ON {match}
        WHERE dst.{key[0]} IS NULL ON CONFLICT DO NOTHING
    """,
    )
    if update:
        await execute(
            connection,
            "update." + table,
            f"""
            UPDATE {table} dst SET {",".join(c + "=src." + c for c in update)},updated_at=now()
            FROM history_{table} src WHERE {match} AND ({condition})
              AND ({",".join("dst." + c for c in update)}) IS DISTINCT FROM
                  ({",".join("src." + c for c in update)})
        """,
        )


async def merge_messages(
    connection: typing.Any,
    batch: dict,
    provider_uuid: object,
    source_name: str,
    message_updates: tuple[str, ...] = (
        "author_uuid",
        "content_uuid",
        "content_updated_at",
        "provider_identity",
    ),
    condition: str = "true",
) -> None:
    await execute(
        connection,
        "insert.contents",
        """
        INSERT INTO messenger_message_contents(uuid,workspace_content,provider_content)
        SELECT DISTINCT ON (src.workspace_content_hash,src.provider_content_hash)
               src.uuid,src.workspace_content,src.provider_content
        FROM history_messenger_message_contents src
        LEFT JOIN messenger_message_contents dst
          ON dst.workspace_content_hash=src.workspace_content_hash
         AND dst.provider_content_hash IS NOT DISTINCT FROM src.provider_content_hash
        WHERE dst.uuid IS NULL ON CONFLICT DO NOTHING
    """,
    )
    await execute(
        connection,
        "remap.contents",
        """
        UPDATE history_messenger_messages message SET content_uuid=dst.uuid
        FROM history_messenger_message_contents src
        JOIN messenger_message_contents dst
          ON dst.workspace_content_hash=src.workspace_content_hash
         AND dst.provider_content_hash IS NOT DISTINCT FROM src.provider_content_hash
        WHERE message.content_uuid=src.uuid
    """,
    )
    if batch["messenger_messages"]:
        await merge(
            connection,
            "messenger_messages",
            list(batch["messenger_messages"][0]),
            message_updates,
            condition=condition,
        )
    # A canonical provider message has one destination; removing its old
    # placement lets the database cascade dependent rows.
    await execute(
        connection,
        "delete.placements",
        """
        DELETE FROM messenger_message_placements dst
        USING history_messenger_message_placements src,messenger_streams stream
        WHERE dst.project_id=src.project_id AND dst.message_uuid=src.message_uuid
          AND dst.stream_uuid<>src.stream_uuid AND stream.project_id=dst.project_id
          AND stream.uuid=dst.stream_uuid AND stream.provider_uuid=%s AND stream.source_name=%s
    """,
        (provider_uuid, source_name),
    )
    for table, updates in (
        ("messenger_message_placements", ["topic_uuid"]),
        ("messenger_user_message_bindings", []),
    ):
        if batch[table]:
            await merge(connection, table, list(batch[table][0]), updates)


async def merge_flags(
    connection: typing.Any,
    suffixes: tuple[str, ...] = ("unreads", "stars", "mentions"),
    user_uuid: object | None = None,
) -> None:
    for suffix in suffixes:
        table = "messenger_user_message_" + suffix
        await execute(
            connection,
            "delete." + table,
            f"""
            DELETE FROM {table} dst USING history_messenger_user_message_bindings access
            WHERE dst.project_id=access.project_id AND dst.user_uuid=access.user_uuid
              AND dst.placement_uuid=access.placement_uuid
              AND (%s::uuid IS NULL OR access.user_uuid=%s) AND NOT EXISTS (
                SELECT 1 FROM history_{table} src
                WHERE (src.project_id,src.user_uuid,src.placement_uuid)=
                      (dst.project_id,dst.user_uuid,dst.placement_uuid))
        """,
            (user_uuid, user_uuid),
        )
        await merge(
            connection,
            table,
            [
                "project_id",
                "user_uuid",
                "placement_uuid",
                "stream_uuid",
                "topic_uuid",
            ],
            key=("project_id", "user_uuid", "placement_uuid"),
        )


async def queue_counters(connection: typing.Any) -> None:
    # One dirty scope per affected topic; the existing independent counter
    # process derives topic/stream/folder totals, without history WS events.
    await execute(
        connection,
        "queue.counters",
        """
        INSERT INTO messenger_counter_updates(project_id,user_uuid,stream_uuid,topic_uuid,publish_updates)
        SELECT DISTINCT access.project_id,access.user_uuid,placement.stream_uuid,placement.topic_uuid,false
        FROM history_messenger_user_message_bindings access
        JOIN history_messenger_message_placements placement ON placement.project_id=access.project_id
            AND placement.uuid=access.placement_uuid
        ORDER BY 1,2,4
        ON CONFLICT(project_id,user_uuid,topic_uuid) DO UPDATE
            SET revision=EXCLUDED.revision,next_retry_at=NULL
    """,
    )
    await execute(
        connection, "notify.counters", "SELECT pg_notify('workspace_counters','')"
    )
