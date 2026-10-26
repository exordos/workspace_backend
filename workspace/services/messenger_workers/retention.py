# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Bounded collection of successful work and unreferenced content pages."""

import datetime
import typing


DEFAULT_RETENTION_SECONDS = 3600
DEFAULT_BATCH_SIZE = 1000

# The outbox is the aggregate root. Its foreign keys own task/fanout cleanup.
OUTBOX_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT event.project_id,event.uuid
        FROM messenger_projection_tasks task
        JOIN messenger_domain_outbox_events event
          ON event.project_id=task.project_id AND event.uuid=task.outbox_event_uuid
        WHERE task.status='completed' AND task.updated_at<%s
          AND event.created_at<%s
          AND NOT EXISTS (
              SELECT 1 FROM messenger_fanout_roots root
              WHERE root.project_id=event.project_id AND root.outbox_event_uuid=event.uuid
                AND (root.status<>'completed' OR root.updated_at>=%s
                     OR EXISTS (
                         SELECT 1 FROM messenger_fanout_batch_tasks batch
                         WHERE batch.project_id=root.project_id AND batch.fanout_root_uuid=root.uuid
                           AND (batch.status<>'completed' OR batch.updated_at>=%s)))
          )
        ORDER BY task.updated_at,task.uuid LIMIT %s
        FOR UPDATE OF event,task SKIP LOCKED
    )
    DELETE FROM messenger_domain_outbox_events event USING candidates
    WHERE event.project_id=candidates.project_id AND event.uuid=candidates.uuid
    RETURNING event.uuid
"""

INCOMING_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT event.id FROM zulip_incoming_events event
        LEFT JOIN zulip_incoming_subscriptions subscription
          ON subscription.external_account_uuid=event.external_account_uuid
        WHERE event.processed_at<%s AND event.error IS NULL
          AND (subscription.queue_id IS DISTINCT FROM event.queue_id
               OR event.event_id<=subscription.last_event_id)
        ORDER BY event.processed_at,event.id LIMIT %s
        FOR UPDATE OF event SKIP LOCKED
    )
    DELETE FROM zulip_incoming_events event USING candidates
    WHERE event.id=candidates.id RETURNING event.id
"""

OUTGOING_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT id FROM zulip_outgoing_intents
        WHERE status='done' AND completed_at<%s
        ORDER BY completed_at,id LIMIT %s FOR UPDATE SKIP LOCKED
    )
    DELETE FROM zulip_outgoing_intents intent USING candidates
    WHERE intent.id=candidates.id RETURNING intent.id
"""

CACHE_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT uuid FROM bridge_zulip_v5_content_snapshots
        WHERE delivery_uuid IS NULL AND created_at<%s
          AND (direction='incoming' OR NOT EXISTS (
              SELECT 1 FROM zulip_outgoing_intents
              WHERE entity_type='message' AND status<>'done'))
        ORDER BY created_at,uuid LIMIT %s FOR UPDATE SKIP LOCKED
    )
    DELETE FROM bridge_zulip_v5_content_snapshots snapshot USING candidates
    WHERE snapshot.uuid=candidates.uuid RETURNING snapshot.uuid
"""

CONTENTS_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT content.uuid FROM messenger_message_contents content
        WHERE content.created_at<%s
          AND NOT EXISTS (SELECT 1 FROM zulip_outgoing_intents
                          WHERE entity_type='message' AND status<>'done')
          AND NOT EXISTS (SELECT 1 FROM messenger_messages message WHERE message.content_uuid=content.uuid)
          AND NOT EXISTS (SELECT 1 FROM messenger_message_content_versions version WHERE version.content_uuid=content.uuid)
          AND NOT EXISTS (SELECT 1 FROM messenger_message_content_versions version WHERE version.previous_content_uuid=content.uuid)
          AND NOT EXISTS (SELECT 1 FROM bridge_zulip_v5_content_snapshots snapshot WHERE snapshot.content_uuid=content.uuid)
        ORDER BY content.created_at,content.uuid LIMIT %s
        FOR UPDATE OF content SKIP LOCKED
    )
    DELETE FROM messenger_message_contents content USING candidates
    WHERE content.uuid=candidates.uuid RETURNING content.uuid
"""

LEGACY_CONTENTS_SQL = """
    WITH candidates AS MATERIALIZED (
        SELECT content.uuid FROM bridge_zulip_v5_message_contents content
        WHERE content.updated_at<%s
          AND NOT EXISTS (SELECT 1 FROM bridge_zulip_v5_messages message WHERE message.content=content.uuid)
        ORDER BY content.updated_at,content.uuid LIMIT %s
        FOR UPDATE OF content SKIP LOCKED
    )
    DELETE FROM bridge_zulip_v5_message_contents content USING candidates
    WHERE content.uuid=candidates.uuid RETURNING content.uuid
"""

QUERIES = {
    "outbox": OUTBOX_SQL,
    "incoming": INCOMING_SQL,
    "outgoing": OUTGOING_SQL,
    "conversion_cache": CACHE_SQL,
    "contents": CONTENTS_SQL,
    "legacy_contents": LEGACY_CONTENTS_SQL,
}


def prune(
    session: typing.Any,
    category: str,
    now: datetime.datetime,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    cutoff = now - datetime.timedelta(seconds=retention_seconds)
    parameters = (cutoff,) * (4 if category == "outbox" else 1) + (batch_size,)
    return len(session.execute(QUERIES[category], parameters).fetchall())
