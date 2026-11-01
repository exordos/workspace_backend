# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Recount committed unread facts in a process independent of message delivery."""

import logging
import typing

from workspace.messenger_api import publication
from workspace.messenger_api.dm import schema
from workspace.services.messenger_workers import v2_projection

LOG = logging.getLogger(__name__)


COUNT_TOPIC_SQL = """
    SELECT count(*) AS unread_count,
           count(*) FILTER (WHERE CASE
               WHEN personal.notification_mode='mute' THEN stream.source_name='zulip' AND mention.placement_uuid IS NOT NULL
               WHEN personal.notification_mode='follow' THEN true
               WHEN personal.notification_mode='unmute' THEN stream.source_name='zulip' OR mention.placement_uuid IS NOT NULL
               WHEN membership.notification_mode='all_messages' THEN true
               WHEN membership.notification_mode='mentions_only' THEN mention.placement_uuid IS NOT NULL
               ELSE false END) AS active_unread_count
    FROM messenger_user_message_unreads unread
    JOIN messenger_user_message_bindings access ON
        (access.project_id,access.user_uuid,access.placement_uuid)=(unread.project_id,unread.user_uuid,unread.placement_uuid)
    JOIN messenger_message_placements placement ON placement.project_id=unread.project_id AND placement.uuid=unread.placement_uuid
    JOIN messenger_messages message ON message.project_id=placement.project_id AND message.uuid=placement.message_uuid AND message.deleted_at IS NULL
    JOIN messenger_topics topic ON topic.project_id=unread.project_id AND topic.uuid=unread.topic_uuid AND topic.deleted_at IS NULL
    JOIN messenger_streams stream ON stream.project_id=unread.project_id AND stream.uuid=unread.stream_uuid AND stream.deleted_at IS NULL
    JOIN messenger_stream_bindings membership ON membership.project_id=unread.project_id AND membership.user_uuid=unread.user_uuid
        AND membership.stream_uuid=unread.stream_uuid AND membership.active
    LEFT JOIN messenger_user_topic_bindings personal ON personal.project_id=unread.project_id
        AND personal.user_uuid=unread.user_uuid AND personal.topic_uuid=unread.topic_uuid
    LEFT JOIN messenger_user_message_mentions mention ON
        (mention.project_id,mention.user_uuid,mention.placement_uuid)=(unread.project_id,unread.user_uuid,unread.placement_uuid)
    WHERE unread.project_id=%s AND unread.user_uuid=%s AND unread.topic_uuid=%s
"""


def process_pending(session: typing.Any, limit: int = 1) -> int:
    """Own cache and acknowledgement transactions in the counter process."""
    with publication.defer():
        return _process_pending(session, limit)


def _process_pending(session: typing.Any, limit: int) -> int:
    # One process owns counts; producers only advance a dirty scope revision.
    pending = session.execute(
        "SELECT * FROM messenger_counter_updates "
        "WHERE next_retry_at IS NULL OR next_retry_at<=NOW() "
        "ORDER BY revision LIMIT %s",
        (limit,),
    ).fetchall()
    for update in pending:
        schema.lock_shared(session)
        session.execute("SAVEPOINT messenger_counter_scope", ())
        failed = False
        try:
            _recount_scope(session, update)
        except Exception:
            session.execute("ROLLBACK TO SAVEPOINT messenger_counter_scope", ())
            failed = True
            LOG.exception(
                "Cannot recount topic %s for user %s in project %s",
                update["topic_uuid"],
                update["user_uuid"],
                update["project_id"],
            )
        finally:
            session.execute("RELEASE SAVEPOINT messenger_counter_scope", ())
        # Producers may own the dirty row while waiting on our cache rows.
        # Release every cache lock before either acknowledging or postponing
        # that revision. A crash between commits leaves the scope dirty.
        session.commit()
        schema.lock_shared(session)
        statement = (
            "UPDATE messenger_counter_updates SET next_retry_at=clock_timestamp()+interval '5 seconds' "
            if failed
            else "DELETE FROM messenger_counter_updates "
        )
        session.execute(
            statement
            + "WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s AND revision=%s",
            (
                update["project_id"],
                update["user_uuid"],
                update["topic_uuid"],
                update["revision"],
            ),
        )
        # A batch's next recount must not retain this dirty-row lock either.
        session.commit()
    return len(pending)


def _recount_scope(
    session: typing.Any, update: typing.Mapping[str, typing.Any]
) -> None:
    project, user, stream, topic = (
        update["project_id"],
        update["user_uuid"],
        update["stream_uuid"],
        update["topic_uuid"],
    )
    previous = session.execute(
        "SELECT unread_count,active_unread_count,passive_unread_count "
        "FROM messenger_user_topic_bindings WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
        (project, user, topic),
    ).fetchone()
    counts = session.execute(COUNT_TOPIC_SQL, (project, user, topic)).fetchone()
    unread, active = counts["unread_count"], counts["active_unread_count"]
    changed = previous is not None and (
        previous["unread_count"],
        previous["active_unread_count"],
        previous["passive_unread_count"],
    ) != (unread, active, unread - active)
    if changed:
        session.execute(
            "UPDATE messenger_user_topic_bindings SET unread_count=%s,active_unread_count=%s,"
            "passive_unread_count=%s,updated_at=NOW() WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
            (unread, active, unread - active, project, user, topic),
        )
        # Mobile push integration point: counters changed for this user/topic.
        # Enqueue only a topic-update snapshot here, including the zero case;
        # ordinary message.created events must not create mobile pushes.
    session.execute(
        "SELECT messenger_refresh_topic_summary_state(%s,%s,%s::uuid[])",
        (project, topic, [user]),
    )
    if update["publish_updates"]:
        v2_projection._emit_unread_snapshots(
            session, project, stream, topic, [user], "user-topic"
        )
    session.execute(
        """WITH totals AS (
             SELECT COALESCE(sum(personal.unread_count),0) AS unread,
                    COALESCE(sum(personal.active_unread_count),0) AS active
             FROM messenger_user_topic_bindings personal
             JOIN messenger_topics topic ON topic.project_id=personal.project_id AND topic.uuid=personal.topic_uuid AND topic.deleted_at IS NULL
             WHERE personal.project_id=%s AND personal.user_uuid=%s AND topic.stream_uuid=%s
           ) UPDATE messenger_stream_bindings binding SET unread_count=totals.unread,
                active_unread_count=totals.active,passive_unread_count=totals.unread-totals.active
             FROM totals WHERE binding.project_id=%s AND binding.user_uuid=%s AND binding.stream_uuid=%s""",
        (project, user, stream, project, user, stream),
    )
    if update["publish_updates"]:
        v2_projection._emit_unread_snapshots(
            session, project, stream, None, [user], "user-stream"
        )
    for folder in session.execute(
        "SELECT folder_uuid FROM messenger_user_folder_bindings WHERE project_id=%s AND user_uuid=%s",
        (project, user),
    ).fetchall():
        v2_projection._process_folder_projection(
            session,
            {
                "project_id": project,
                "payload": {
                    "source_kind": "counters.updated",
                    "counter_stream_uuid": stream,
                    "emit_public_event": update["publish_updates"],
                    "user_uuid": user,
                    "folder_uuid": folder["folder_uuid"],
                },
            },
        )
    session.execute(
        """WITH totals AS (
             SELECT COALESCE(sum(binding.unread_count),0) AS unread,
                    COALESCE(sum(binding.active_unread_count),0) AS active
             FROM messenger_stream_bindings binding JOIN messenger_streams stream
               ON stream.project_id=binding.project_id AND stream.uuid=binding.stream_uuid
              AND stream.deleted_at IS NULL AND NOT stream.is_archived
             WHERE binding.project_id=%s AND binding.user_uuid=%s AND binding.active
           ) UPDATE messenger_project_users SET unread_count=totals.unread,
             active_unread_count=totals.active,passive_unread_count=totals.unread-totals.active
           FROM totals WHERE project_id=%s AND user_uuid=%s""",
        (project, user, project, user),
    )
