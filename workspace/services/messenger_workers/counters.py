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
    WITH scope(project_id,user_uuid,topic_uuid) AS NOT MATERIALIZED (
        VALUES (%s::uuid,%s::uuid,%s::uuid)
    ), policy AS MATERIALIZED (
        SELECT CASE
            WHEN personal.notification_mode='mute' THEN
                CASE WHEN stream.source_name='zulip' THEN 'mentions' ELSE 'none' END
            WHEN personal.notification_mode='follow' THEN 'all'
            WHEN personal.notification_mode='unmute' THEN
                CASE WHEN stream.source_name='zulip' THEN 'all' ELSE 'mentions' END
            WHEN membership.notification_mode='all_messages' THEN 'all'
            WHEN membership.notification_mode='mentions_only' THEN 'mentions'
            ELSE 'none' END AS mode
        FROM scope
        JOIN messenger_topics topic ON topic.project_id=scope.project_id AND topic.uuid=scope.topic_uuid
        JOIN messenger_streams stream ON stream.project_id=topic.project_id AND stream.uuid=topic.stream_uuid
        JOIN messenger_stream_bindings membership ON membership.project_id=scope.project_id
            AND membership.user_uuid=scope.user_uuid AND membership.stream_uuid=stream.uuid AND membership.active
        LEFT JOIN messenger_user_topic_bindings personal ON personal.project_id=scope.project_id
            AND personal.user_uuid=scope.user_uuid AND personal.topic_uuid=scope.topic_uuid
    ), bounded AS MATERIALIZED (
        SELECT unread.project_id,unread.user_uuid,unread.placement_uuid
        FROM messenger_user_message_unreads unread,scope
        WHERE unread.project_id=scope.project_id AND unread.user_uuid=scope.user_uuid
            AND unread.topic_uuid=scope.topic_uuid
        ORDER BY unread.placement_uuid
        LIMIT 1001
    )
    SELECT count(*) AS unread_count,
           count(*) FILTER (WHERE CASE policy.mode
               WHEN 'all' THEN true
               WHEN 'mentions' THEN EXISTS (
                   SELECT 1 FROM messenger_user_message_mentions mention
                   WHERE (mention.project_id,mention.user_uuid,mention.placement_uuid)=
                         (bounded.project_id,bounded.user_uuid,bounded.placement_uuid)
               ) ELSE false END) AS active_unread_count
    FROM bounded CROSS JOIN policy
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
             JOIN messenger_topics topic ON topic.project_id=personal.project_id AND topic.uuid=personal.topic_uuid
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
              AND NOT stream.is_archived
             WHERE binding.project_id=%s AND binding.user_uuid=%s AND binding.active
           ) UPDATE messenger_project_users SET unread_count=totals.unread,
             active_unread_count=totals.active,passive_unread_count=totals.unread-totals.active
           FROM totals WHERE project_id=%s AND user_uuid=%s""",
        (project, user, project, user),
    )
