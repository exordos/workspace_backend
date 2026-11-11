# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Read a bounded set of poll placements and personalize one database snapshot."""

import typing
import uuid

from restalchemy.common import contexts
from restalchemy.common import exceptions as ra_exc

from workspace.messenger_api import exceptions
from workspace.messenger_api.dm import polls


# Separate totals from personal selections. A delivery batch reads the question
# counts once for each poll regardless of the number of recipients.
_SNAPSHOT_SELECT = f"""
SELECT readers.uuid, readers.users, c.workspace_content AS payload, root.poll_state_revision,
       status.is_closed, counts.items AS counts, choices.items AS choices,
       CASE WHEN (c.workspace_content->>'is_anonymous')::boolean IS FALSE THEN (
           SELECT jsonb_agg(jsonb_build_array(option.question_uuid, option.option_uuid, sample.users))
           FROM (
               SELECT (entry->>0)::uuid AS question_uuid, (entry->>1)::uuid AS option_uuid
               FROM jsonb_array_elements(counts.items) AS groups(entry)
               WHERE entry->>1 IS NOT NULL AND (
                   c.workspace_content->>'results_visibility'='always' OR status.is_closed OR EXISTS (
                       SELECT 1 FROM jsonb_array_elements(choices.items) AS choices_for_readers(choice)
                       WHERE choice->>1=entry->>0
                   )
               )
           ) option
           CROSS JOIN LATERAL (
               SELECT array_agg(user_uuid ORDER BY user_uuid) AS users FROM (
                   SELECT user_uuid FROM messenger_poll_votes
                   WHERE project_id=root.project_id AND message_uuid=root.uuid
                     AND question_uuid=option.question_uuid AND option_uuid=option.option_uuid
                   ORDER BY user_uuid LIMIT {polls.VOTER_PREVIEW_LIMIT}
               ) selected
           ) sample
       ) END AS voter_previews
FROM readers
JOIN messenger_messages root ON root.project_id=readers.project_id AND root.uuid=readers.message_uuid
JOIN messenger_message_contents c ON c.uuid=root.content_uuid
CROSS JOIN LATERAL (
    SELECT (c.workspace_content->>'closed_at' IS NOT NULL OR COALESCE(
        (c.workspace_content->>'closes_at')::timestamptz <= statement_timestamp(), false
    )) AS is_closed
) status
CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(question_uuid, option_uuid, n)) AS items
    FROM (
        SELECT question_uuid, option_uuid, COUNT(DISTINCT user_uuid) AS n
        FROM messenger_poll_votes
        WHERE project_id=root.project_id AND message_uuid=root.uuid
        GROUP BY GROUPING SETS ((), (question_uuid), (question_uuid, option_uuid))
    ) grouped
) counts
CROSS JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(user_uuid, question_uuid, option_uuid)) AS items
    FROM messenger_poll_votes
    WHERE project_id=root.project_id AND message_uuid=root.uuid AND user_uuid=ANY(readers.users)
) choices
WHERE c.workspace_content->>'kind'='poll'
"""


SNAPSHOTS_SQL = (
    """
WITH readers AS (
    SELECT placement.project_id, placement.uuid, placement.message_uuid, array_agg(binding.user_uuid) AS users
    FROM unnest(%(placements)s::uuid[], %(users)s::uuid[]) AS wanted(placement_uuid, user_uuid)
    JOIN messenger_user_message_bindings binding
      ON binding.project_id=%(project)s AND binding.placement_uuid=wanted.placement_uuid
     AND binding.user_uuid=wanted.user_uuid
    JOIN messenger_message_placements placement
      ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
    JOIN messenger_stream_bindings member
      ON member.project_id=binding.project_id AND member.stream_uuid=placement.stream_uuid
     AND member.user_uuid=binding.user_uuid AND member.active
    GROUP BY placement.project_id, placement.uuid, placement.message_uuid
)
"""
    + _SNAPSHOT_SELECT
)


# The page already resolved its reader and placement. PostgreSQL evaluates the
# correlated subquery only for poll rows and under the page statement snapshot.
PAGE_SNAPSHOT_SQL = f"""
CASE WHEN message.payload->>'kind'='poll' THEN (
    WITH readers AS (
        SELECT message.project_id, message.uuid, message.canonical_message_uuid AS message_uuid,
               ARRAY[message.user_uuid] AS users
    )
    SELECT to_jsonb(snapshot) FROM ({_SNAPSHOT_SELECT}) AS snapshot
) END AS poll_snapshot
"""


def _personalize(snapshot: typing.Mapping[str, typing.Any]) -> dict[str, dict]:
    payload = snapshot["payload"]
    counts = {(q, o): n for q, o, n in snapshot["counts"]}
    previews: dict[str, dict[str, list[str]]] = {}
    for q, o, users in snapshot["voter_previews"] or []:
        previews.setdefault(q, {})[o] = users

    questions = []
    questions_with_previews = []
    order = {}
    for q_index, question in enumerate(payload["questions"]):
        q = question["uuid"]
        summary = {
            "question_uuid": q,
            "voter_count": counts.get((q, None), 0),
            "option_counts": {
                o["uuid"]: counts.get((q, o["uuid"]), 0) for o in question["options"]
            },
        }
        questions.append(summary)
        question_previews = previews.get(q, {})
        option_previews = {
            o["uuid"]: question_previews[o["uuid"]]
            for o in question["options"]
            if o["uuid"] in question_previews
        }
        questions_with_previews.append(
            {**summary, "option_voter_previews": option_previews}
            if option_previews
            else summary
        )
        for o_index, option in enumerate(question["options"]):
            order[q, option["uuid"]] = (q_index, o_index)

    own: dict[str, dict[str, list[str]]] = {}
    for user, q, o in sorted(
        snapshot["choices"] or [], key=lambda v: order[v[1], v[2]]
    ):
        own.setdefault(user, {}).setdefault(q, []).append(o)
    public_results = snapshot["is_closed"] or payload["results_visibility"] == "always"
    common = {
        "revision": snapshot["poll_state_revision"],
        "is_closed": snapshot["is_closed"],
        "voter_count": counts[None, None],
        "questions": questions_with_previews if public_results else questions,
    }
    states = {}
    for user in snapshot["users"]:
        reader = str(user)
        own_choices = own.get(reader, {})
        state = {
            **common,
            "my_votes": [
                {"question_uuid": q, "option_uuids": options}
                for q, options in own_choices.items()
            ],
        }
        if previews and own_choices and not public_results:
            state["questions"] = [
                with_previews if question["question_uuid"] in own_choices else question
                for question, with_previews in zip(questions, questions_with_previews)
            ]
        states[reader] = state
    return states


def apply_page_snapshot(message: dict, snapshot: dict | None) -> None:
    if snapshot is None:
        message["poll_state"] = None
        return
    message["payload"] = snapshot["payload"]
    message["poll_state"] = _personalize(snapshot)[str(message["user_uuid"])]


def hydrate(
    session: typing.Any, messages: list[dict[str, typing.Any]]
) -> list[dict[str, typing.Any]]:
    """Bind payload and state to the same SQL snapshot, dropping revoked rows."""
    polls = [m for m in messages if (m.get("payload") or {}).get("kind") == "poll"]
    for message in messages:
        message["poll_state"] = None
    if not polls:
        return messages
    pairs = {(str(m["uuid"]), str(m["user_uuid"])) for m in polls}
    placements, users = zip(*pairs)
    session = session or contexts.Context().get_session()
    rows = session.execute(
        SNAPSHOTS_SQL,
        {
            "project": polls[0]["project_id"],
            "placements": list(placements),
            "users": list(users),
        },
    ).fetchall()
    snapshots = {str(row["uuid"]): (row["payload"], _personalize(row)) for row in rows}
    result = []
    for message in messages:
        if message["payload"]["kind"] == "poll":
            snapshot = snapshots.get(str(message["uuid"]))
            user = str(message["user_uuid"])
            if snapshot is None or user not in snapshot[1]:
                continue
            message.update(payload=snapshot[0], poll_state=snapshot[1][user])
        result.append(message)
    return result


def voters(
    session: typing.Any,
    project_uuid: uuid.UUID,
    user_uuid: uuid.UUID,
    message_uuid: uuid.UUID,
    question_uuid: uuid.UUID,
    option_uuid: uuid.UUID,
    marker: uuid.UUID | None,
    limit: int,
) -> list[dict[str, str]]:
    # An empty page, a missing option, a private poll and a revoked membership
    # must remain distinguishable without separate access and vote snapshots.
    row = session.execute(
        """
        SELECT visible.payload, page.users
        FROM messenger_api_user_messages_v1 visible
        CROSS JOIN LATERAL (
            SELECT array_agg(user_uuid ORDER BY user_uuid) AS users FROM (
                SELECT user_uuid FROM messenger_poll_votes
                WHERE project_id=visible.project_id AND message_uuid=visible.canonical_message_uuid
                  AND (visible.payload->>'is_anonymous')::boolean IS FALSE
                  AND question_uuid=%s AND option_uuid=%s
                  AND (%s::uuid IS NULL OR user_uuid > %s)
                ORDER BY user_uuid LIMIT %s
            ) selected
        ) page
        WHERE visible.project_id=%s AND visible.uuid=%s AND visible.user_uuid=%s AND visible.visible
        """,
        (
            question_uuid,
            option_uuid,
            marker,
            marker,
            limit,
            project_uuid,
            message_uuid,
            user_uuid,
        ),
    ).fetchone()
    if row is None:
        raise ra_exc.ResourceNotFoundError(resource="message", path=str(message_uuid))
    payload = row["payload"]
    if payload["kind"] != "poll":
        raise ra_exc.ValidationErrorException()
    if payload["is_anonymous"]:
        raise exceptions.PollForbiddenError()
    if not any(
        q["uuid"] == str(question_uuid)
        and any(o["uuid"] == str(option_uuid) for o in q["options"])
        for q in payload["questions"]
    ):
        raise ra_exc.ValidationErrorException()
    return [{"uuid": str(user)} for user in row["users"] or []]
