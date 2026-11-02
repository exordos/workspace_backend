# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Presence rows are the facts; message bindings only grant visibility."""

import typing

from restalchemy.common import exceptions


TABLES = {
    "starred": "messenger_user_message_stars",
    "pinned": "messenger_user_message_pins",
    "mentioned": "messenger_user_message_mentions",
}


def set_flag(session: typing.Any, message: typing.Any, field: str, value: bool) -> bool:
    table = TABLES[field]
    membership = session.execute(
        "SELECT membership_generation FROM messenger_stream_bindings "
        "WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s AND active "
        "FOR KEY SHARE",
        (message.project_id, message.user_uuid, message.stream_uuid),
    ).fetchone()
    if (
        membership is None
        or membership["membership_generation"] != message.membership_generation
    ):
        raise exceptions.ValidationErrorException()
    if value:
        changed = session.execute(
            f"INSERT INTO {table} VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING placement_uuid",
            (
                message.project_id,
                message.user_uuid,
                message.uuid,
                message.stream_uuid,
                message.topic_uuid,
            ),
        ).fetchone()
    else:
        changed = session.execute(
            f"DELETE FROM {table} WHERE project_id=%s AND user_uuid=%s AND placement_uuid=%s RETURNING placement_uuid",
            (message.project_id, message.user_uuid, message.uuid),
        ).fetchone()
    return changed is not None


def mark_read(
    session: typing.Any,
    project_id: object,
    user_uuid: object,
    *,
    stream_uuid: object | None = None,
    topic_uuid: object | None = None,
    placement_uuid: object | None = None,
    boundary: tuple[object, object] | None = None,
) -> list[typing.Any]:
    predicates = ["unread.project_id=%s", "unread.user_uuid=%s"]
    values: list[object] = [project_id, user_uuid]
    for name, value in (
        ("stream_uuid", stream_uuid),
        ("topic_uuid", topic_uuid),
        ("placement_uuid", placement_uuid),
    ):
        if value is not None:
            predicates.append(f"unread.{name}=%s")
            values.append(value)
    if boundary is not None:
        predicates.append(
            "(binding.preview_created_at,binding.placement_uuid)<=(%s,%s)"
        )
        values.extend(boundary)
        using = "USING messenger_user_message_bindings binding"
        predicates.append(
            "(binding.project_id,binding.user_uuid,binding.placement_uuid)="
            "(unread.project_id,unread.user_uuid,unread.placement_uuid)"
        )
    else:
        using = ""
    return session.execute(
        f"""DELETE FROM messenger_user_message_unreads unread {using}
            WHERE {" AND ".join(predicates)} RETURNING unread.placement_uuid""",
        values,
    ).fetchall()
