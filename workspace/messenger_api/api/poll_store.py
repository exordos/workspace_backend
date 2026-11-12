# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Apply an authenticated poll action inside the current request transaction."""

import datetime
import json
import typing

from restalchemy.common import exceptions as ra_exc
from restalchemy.storage.sql import sessions

from workspace.bridges.zulip import poll_conversion
from workspace.messenger_api import exceptions
from workspace.messenger_api.dm import base, polls


def _vote(
    session: sessions.PgSQLSession,
    key: tuple,
    poll: polls.PollPayload,
    values: dict,
    now: datetime.datetime,
) -> bool:
    where = "project_id=%s AND message_uuid=%s AND user_uuid=%s AND question_uuid=%s"
    rows = session.execute(
        f"SELECT option_uuid FROM messenger_poll_votes WHERE {where}", key
    ).fetchall()
    current = {str(row["option_uuid"]) for row in rows}
    option = values["option_uuid"]
    selected = poll.select_options(
        values["question_uuid"], option, values["selected"], current, now
    )
    if selected == current:
        return False
    if values["selected"] and current and selected == {option}:
        session.execute(
            f"UPDATE messenger_poll_votes SET option_uuid=%s WHERE {where}",
            (option, *key),
        )
    elif values["selected"]:
        session.execute(
            """INSERT INTO messenger_poll_votes
               (project_id,message_uuid,user_uuid,question_uuid,option_uuid)
               VALUES (%s,%s,%s,%s,%s)""",
            (*key, option),
        )
    else:
        session.execute(
            f"DELETE FROM messenger_poll_votes WHERE {where} AND option_uuid=%s",
            (*key, option),
        )
    return True


def mutate(
    session: sessions.PgSQLSession,
    project_uuid: object,
    user_uuid: object,
    placement_uuid: object,
    canonical_uuid: object,
    action: str,
    values: dict[str, typing.Any],
) -> None:
    not_found = ra_exc.ResourceNotFoundError(
        resource="Message", path=str(placement_uuid)
    )
    # Account deletion and message deletion can each cascade through our votes.
    # Lock parents before vote rows, even when the request ends up a no-op.
    account = session.execute(
        """SELECT 1 FROM messenger_project_users
           WHERE project_id=%s AND user_uuid=%s FOR KEY SHARE""",
        (project_uuid, user_uuid),
    ).fetchone()
    if account is None:
        raise not_found
    # A scope deletion takes stream/topic, then placement, then the orphaned
    # message. Pin the parent scope before taking any lock on that message.
    scope = session.execute(
        """WITH room AS MATERIALIZED (
               SELECT p.stream_uuid,p.topic_uuid FROM messenger_message_placements p
               JOIN messenger_streams s ON (s.project_id,s.uuid)=(p.project_id,p.stream_uuid)
               WHERE p.project_id=%s AND p.uuid=%s AND p.message_uuid=%s
               FOR KEY SHARE OF s
           )
           SELECT room.stream_uuid,t.uuid AS topic_uuid FROM room
           JOIN messenger_topics t ON (t.stream_uuid,t.uuid)=(room.stream_uuid,room.topic_uuid)
           WHERE t.project_id=%s FOR KEY SHARE OF t""",
        (project_uuid, placement_uuid, canonical_uuid, project_uuid),
    ).fetchone()
    if scope is None:
        raise not_found
    # Direct deletion takes the message before its placement. KEY SHARE
    # fences it but permits other clicks, and a move can finish its own
    # non-key message updates before we serialize on the message below.
    pinned = session.execute(
        """WITH surviving AS MATERIALIZED (
               SELECT project_id,uuid FROM messenger_messages
               WHERE project_id=%s AND uuid=%s FOR KEY SHARE
           )
           SELECT p.uuid FROM surviving m JOIN messenger_message_placements p
             ON (p.project_id,p.message_uuid)=(m.project_id,m.uuid)
           WHERE p.uuid=%s AND p.stream_uuid=%s AND p.topic_uuid=%s FOR SHARE OF p""",
        (
            project_uuid,
            canonical_uuid,
            placement_uuid,
            scope["stream_uuid"],
            scope["topic_uuid"],
        ),
    ).fetchone()
    if pinned is None:
        raise not_found
    session.execute(
        """SELECT uuid FROM messenger_messages WHERE project_id=%s AND uuid=%s
           FOR NO KEY UPDATE""",
        (project_uuid, canonical_uuid),
    )
    # Start a fresh statement after the root lock. A waiter must see the
    # committed payload and check the placement's current audience and role.
    row = session.execute(
        """SELECT c.workspace_content AS payload, m.author_uuid,
                  m.source_name AS message_source, m.provider_identity, s.provider_uuid,
                  s.source_name, s.encryption, member.role
           FROM messenger_messages m
           JOIN messenger_message_contents c ON c.uuid=m.content_uuid
           JOIN messenger_message_placements p ON (p.project_id,p.message_uuid)=(m.project_id,m.uuid)
           JOIN messenger_streams s ON (s.project_id,s.uuid)=(p.project_id,p.stream_uuid)
           JOIN messenger_stream_bindings member ON (member.project_id,member.stream_uuid)=(p.project_id,p.stream_uuid)
           JOIN messenger_user_message_bindings access ON (access.project_id,access.placement_uuid,access.user_uuid)
                                                       =(p.project_id,p.uuid,member.user_uuid)
           WHERE m.project_id=%s AND m.uuid=%s AND p.uuid=%s AND member.user_uuid=%s AND member.active
           FOR SHARE OF member,access""",
        (project_uuid, canonical_uuid, placement_uuid, user_uuid),
    ).fetchone()
    if row is None:
        raise not_found
    payload = row["payload"]
    if (
        payload["kind"] != "poll"
        or row["source_name"] not in {"native", "zulip"}
        or row["message_source"] != row["source_name"]
        or row["encryption"]
    ):
        raise ra_exc.ValidationErrorException()
    synchronized = row["source_name"] == "zulip" and poll_conversion.compatible(payload)
    account = None
    if synchronized:
        # Zulip cannot close a widget or restrict who adds options. Keep its
        # policy stable rather than accepting a change that the other client
        # would silently ignore.
        if action in {"close_poll", "update_settings"}:
            raise exceptions.PollConflictError()
        account = session.execute(
            """SELECT uuid,external_account_id FROM m_external_accounts_v2
               WHERE project_id=%s AND owner_user_uuid=%s AND provider_uuid=%s
                 AND provider='zulip' AND NOT disabled AND status<>'new'
                 AND COALESCE(settings->>'api_key','')<>''""",
            (project_uuid, user_uuid, row["provider_uuid"]),
        ).fetchone()
        if account is None:
            raise exceptions.PollForbiddenError()
    if (
        action in {"close_poll", "update_settings"}
        and row["author_uuid"] != user_uuid
        and row["role"]
        not in {
            base.WorkspaceStreamRole.OWNER.value,
            base.WorkspaceStreamRole.ADMINISTRATOR.value,
            base.WorkspaceStreamRole.MODERATOR.value,
        }
    ):
        raise exceptions.PollForbiddenError()

    now = session.execute("SELECT clock_timestamp() AS value", ()).fetchone()["value"]
    poll = polls.POLL_TYPE.from_simple_type(payload)
    if action == "vote":
        changed = _vote(
            session,
            (project_uuid, canonical_uuid, user_uuid, values["question_uuid"]),
            poll,
            values,
            now,
        )
        if synchronized and changed:
            session.execute(
                """INSERT INTO zulip_outgoing_intents
                   (project_id,entity_type,entity_uuid,ordering_key,external_account_uuid,operation,data)
                   VALUES (%s,'poll_vote',%s,%s,%s,'vote',%s::jsonb)""",
                (
                    project_uuid,
                    canonical_uuid,
                    f"message:{canonical_uuid}",
                    account["uuid"],
                    json.dumps(
                        {
                            "message_uuid": str(canonical_uuid),
                            "actor_uuid": str(user_uuid),
                            "account_identity": account["external_account_id"],
                            "remote_identity": row["provider_identity"],
                            "payload": payload,
                            **values,
                        }
                    ),
                ),
            )
            session.execute(
                "UPDATE messenger_messages SET status='new' WHERE project_id=%s AND uuid=%s",
                (project_uuid, canonical_uuid),
            )
        return
    if action == "close_poll":
        if not poll.close(now):
            return
    elif action == "add_poll_option":
        if not poll.append_option(values["question_uuid"], values["option"], now):
            return
        if synchronized:
            if not poll_conversion.compatible(polls.POLL_TYPE.to_simple_type(poll)):
                raise exceptions.PollConflictError()
    elif action == "update_settings":
        if not poll.update_settings(values["settings"], now):
            return
        # Zulip initializes widgets only when creating a message. Editing a
        # delivered preview cannot turn that existing message into a widget.
        if row["source_name"] == "zulip" and poll_conversion.compatible(
            polls.POLL_TYPE.to_simple_type(poll)
        ):
            raise exceptions.PollConflictError()
    else:
        raise ra_exc.ValidationErrorException()

    payload = polls.POLL_TYPE.to_simple_type(poll)
    session.execute(
        """UPDATE messenger_messages SET content_uuid=messenger_resolve_message_content(%s::jsonb,NULL),
                  poll_state_revision=poll_state_revision+1, updated_at=clock_timestamp() AT TIME ZONE 'UTC'
           WHERE project_id=%s AND uuid=%s""",
        (json.dumps(payload), project_uuid, canonical_uuid),
    )
