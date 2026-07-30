# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Persist complete bounded reaction-user lists on the reaction write path."""

import collections.abc
import json
import typing
import uuid as sys_uuid

from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import messenger_reaction_opts


LOCK_MESSAGES_SQL = """
    SELECT "uuid"
    FROM "m_workspace_messages"
    WHERE "project_id" = %s
      AND "uuid" = ANY(%s::uuid[])
    ORDER BY "uuid"
    FOR UPDATE
"""

MESSAGE_SNAPSHOTS_SQL = """
    SELECT "uuid", "reaction_users"
    FROM "m_workspace_messages"
    WHERE "project_id" = %s
      AND "uuid" = ANY(%s::uuid[])
"""

REACTION_USERS_SQL = """
    WITH requested AS (
        SELECT *
        FROM unnest(
            %s::uuid[],
            %s::text[]
        ) AS candidate("message_uuid", "emoji_name")
    )
    SELECT
        requested."message_uuid",
        requested."emoji_name",
        matched."user_uuids"
    FROM requested
    LEFT JOIN LATERAL (
        SELECT array_agg(
            bounded."user_uuid"
            ORDER BY bounded."user_uuid"
        ) AS "user_uuids"
        FROM (
            SELECT reaction."user_uuid"
            FROM "m_workspace_message_reactions" AS reaction
            WHERE reaction."project_id" = %s
              AND reaction."message_uuid" = requested."message_uuid"
              AND reaction."emoji_name" = requested."emoji_name"
            ORDER BY reaction."user_uuid"
            LIMIT %s
        ) AS bounded
    ) AS matched ON TRUE
"""

UPDATE_SNAPSHOT_SQL = """
    UPDATE "m_workspace_messages"
    SET "reaction_users" = %s::jsonb
    WHERE "project_id" = %s
      AND "uuid" = %s
"""


def _session(session: typing.Any) -> typing.Any:
    return session or contexts.Context().get_session()


def _limit(conf: cfg.ConfigOpts) -> int:
    try:
        return conf[messenger_reaction_opts.DOMAIN].user_list_limit
    except (cfg.NoSuchGroupError, cfg.NoSuchOptError):
        # Isolated unit tests and maintenance helpers may import the write
        # path without initializing a service CLI.
        return messenger_reaction_opts.DEFAULT_USER_LIST_LIMIT


def _message_uuids(
    message_uuids: collections.abc.Iterable[object],
) -> list[sys_uuid.UUID]:
    return sorted({sys_uuid.UUID(str(value)) for value in message_uuids})


def _groups(
    groups: collections.abc.Iterable[tuple[object, object]],
) -> list[tuple[sys_uuid.UUID, str]]:
    return sorted(
        {
            (sys_uuid.UUID(str(message_uuid)), str(emoji_name))
            for message_uuid, emoji_name in groups
        }
    )


def lock_messages(
    project_id: object,
    message_uuids: collections.abc.Iterable[object],
    session: typing.Any = None,
) -> None:
    """Serialize reaction snapshots for the supplied messages."""
    ordered = _message_uuids(message_uuids)
    if not ordered:
        return
    _session(session).execute(
        LOCK_MESSAGES_SQL,
        (project_id, ordered),
    ).fetchall()


def refresh_groups(
    project_id: object,
    groups: collections.abc.Iterable[tuple[object, object]],
    session: typing.Any = None,
    conf: cfg.ConfigOpts = cfg.CONF,
) -> None:
    """Refresh only affected emoji keys after their reaction mutation."""
    ordered = _groups(groups)
    if not ordered:
        return

    current_session = _session(session)
    message_uuids = _message_uuids(message_uuid for message_uuid, _ in ordered)
    snapshots = {
        sys_uuid.UUID(str(row["uuid"])): dict(row["reaction_users"] or {})
        for row in current_session.execute(
            MESSAGE_SNAPSHOTS_SQL,
            (project_id, message_uuids),
        ).fetchall()
    }

    limit = _limit(conf)
    users_by_group: dict[tuple[sys_uuid.UUID, str], list[object]] = {}
    if limit > 0:
        rows = current_session.execute(
            REACTION_USERS_SQL,
            (
                [message_uuid for message_uuid, _emoji_name in ordered],
                [emoji_name for _message_uuid, emoji_name in ordered],
                project_id,
                limit + 1,
            ),
        ).fetchall()
        users_by_group = {
            (
                sys_uuid.UUID(str(row["message_uuid"])),
                str(row["emoji_name"]),
            ): list(row["user_uuids"] or ())
            for row in rows
        }

    changed_messages: set[sys_uuid.UUID] = set()
    for message_uuid, emoji_name in ordered:
        snapshot = snapshots[message_uuid]
        user_uuids = users_by_group.get((message_uuid, emoji_name), [])
        if 0 < len(user_uuids) <= limit:
            value = [str(user_uuid) for user_uuid in user_uuids]
            if snapshot.get(emoji_name) != value:
                snapshot[emoji_name] = value
                changed_messages.add(message_uuid)
        elif emoji_name in snapshot:
            snapshot.pop(emoji_name)
            changed_messages.add(message_uuid)

    for message_uuid in sorted(changed_messages):
        current_session.execute(
            UPDATE_SNAPSHOT_SQL,
            (
                json.dumps(snapshots[message_uuid], sort_keys=True),
                project_id,
                message_uuid,
            ),
        )
