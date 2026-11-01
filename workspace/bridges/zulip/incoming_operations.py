# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

"""Resolve HTTP dependencies before short, atomic SQL domain changes."""

import asyncio
import collections
import datetime
import hashlib
import json
import re
import typing
import types
import urllib.parse
import uuid

from psycopg.types import json as pg_json

from workspace.bridges.zulip import (
    content_cache,
    message_conversion,
    outgoing_operations,
)
from workspace.common import emoji
from workspace.messenger_api import file_storage, publication
from workspace.messenger_api.dm import base

ENQUEUE_FILE_CLEANUP_SQL = """
    INSERT INTO zulip_outgoing_intents (
        project_id, entity_type, entity_uuid, ordering_key,
        external_account_uuid, operation, data
    ) VALUES (%s, 'file', %s, %s, %s, 'delete', %s)
"""

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
DELETE_ROW_SQL = "DELETE FROM {table} WHERE project_id = %s AND uuid = %s RETURNING *"
SELECT_IDENTITY_SQL = """
    SELECT uuid FROM {table}
    WHERE project_id = %s AND provider_uuid = %s AND provider_identity = %s
      AND source_name = 'zulip'
"""
SELECT_USER_SQL = """
    SELECT account.*, users.source AS user_source
    FROM m_external_accounts_v2 account
    JOIN m_workspace_users users ON users.uuid = account.owner_user_uuid
    WHERE account.project_id = %s AND account.provider_uuid = %s
      AND account.provider = 'zulip' AND account.external_account_id = %s
"""
SELECT_LIVE_SUBSCRIPTION_SQL = """
    SELECT 1 FROM zulip_incoming_subscriptions
    WHERE external_account_uuid = %s AND provider_uuid = %s
      AND owner_user_uuid = %s AND account_identity = %s AND server_url = %s
"""
SELECT_ENTITY_LINK_SQL = """
    SELECT 1 FROM zulip_entity_links
    WHERE project_id = %s AND entity_type = %s AND entity_uuid = %s
      AND provider_uuid = %s AND external_account_uuid = %s
"""
SELECT_PERSON_SQL = "SELECT * FROM m_workspace_users WHERE uuid = %s"
UPSERT_PERSON_SQL = """
    INSERT INTO m_workspace_users (
        uuid, username, source, status, avatar, first_name, email, disabled,
        created_at, updated_at
    ) VALUES (%s, %s, 'zulip', 'offline', %s, %s, %s, %s, now(), now())
    ON CONFLICT (uuid) DO UPDATE SET
        first_name = EXCLUDED.first_name, email = EXCLUDED.email,
        disabled = EXCLUDED.disabled, updated_at = now()
    WHERE (m_workspace_users.first_name, m_workspace_users.email,
           m_workspace_users.disabled) IS DISTINCT FROM
          (EXCLUDED.first_name, EXCLUDED.email, EXCLUDED.disabled)
    RETURNING *
"""
REGISTER_USER_SQL = "SELECT messenger_v2_register_project_user(%s, %s)"
SELECT_MEMBERS_SQL = """
    SELECT * FROM messenger_stream_bindings
    WHERE project_id = %s AND stream_uuid = %s AND active
    ORDER BY user_uuid
"""
SELECT_PLACEMENTS_SQL = """
    SELECT * FROM messenger_message_placements
    WHERE project_id = %s AND message_uuid = %s
"""
SELECT_PREVIOUS_PLACEMENTS_SQL = """
    SELECT placement.* FROM messenger_message_placements placement
    JOIN messenger_streams stream
      ON stream.project_id = placement.project_id AND stream.uuid = placement.stream_uuid
    WHERE placement.project_id = %s AND placement.message_uuid = %s
      AND placement.stream_uuid <> %s
      AND stream.source_name='zulip' AND stream.provider_uuid=%s
"""
SELECT_DELIVERY_SQL = """
    SELECT delivery.* FROM bridge_zulip_v5_deliveries delivery
    JOIN messenger_providers provider ON provider.hostname = delivery.hostname
    LEFT JOIN messenger_messages message ON message.project_id=delivery.project_id AND message.uuid=delivery.message_uuid
    WHERE (message.uuid IS NULL OR (message.source_name='zulip' AND message.provider_uuid=provider.uuid))
      AND delivery.project_id = %s AND provider.uuid = %s
      AND (delivery.zulip_message_id = %s OR delivery.message_uuid::text = %s)
    LIMIT 1
"""
SELECT_PENDING_SEND_SQL = """
    SELECT 1 FROM zulip_outgoing_intents
    WHERE project_id = %s AND entity_type = 'message' AND operation = 'insert'
      AND status = 'checking' AND request->>'kind' = 'send'
      AND data->>'account_provider_uuid' = %s AND data->>'account_identity' = %s
    LIMIT 1
"""
SELECT_CONTENT_SNAPSHOT_SQL = """
    SELECT contents.uuid,contents.workspace_content FROM bridge_zulip_v5_content_snapshots snapshot
    JOIN bridge_zulip_v5_deliveries delivery ON delivery.uuid = snapshot.delivery_uuid
    JOIN messenger_message_contents contents ON contents.uuid=snapshot.content_uuid
    WHERE delivery.project_id = %s AND delivery.message_uuid = %s
      AND contents.provider_content = %s
    ORDER BY snapshot.created_at DESC LIMIT 1
"""
OBSERVE_DELIVERY_SQL = """
    UPDATE bridge_zulip_v5_deliveries
    SET zulip_message_id = %s, sending = false,
        delivered_at = COALESCE(delivered_at, now() AT TIME ZONE 'UTC'),
        content_observed_at = %s
    WHERE uuid = %s
"""
SELECT_MESSAGE_CLOCKS_SQL = """
    SELECT provider_identity,content_updated_at FROM messenger_messages
    WHERE project_id=%s AND provider_uuid=%s AND source_name='zulip'
      AND provider_identity=ANY(%s::text[])
"""
SELECT_CATALOG_SQL = """
    SELECT account.external_account_id, account.owner_user_uuid, users.first_name
    FROM m_external_accounts_v2 account
    JOIN m_workspace_users users ON users.uuid = account.owner_user_uuid
    WHERE account.project_id = %s AND account.provider_uuid = %s
      AND (account.external_account_id=ANY(%s::text[]) OR users.first_name=ANY(%s::text[]))
"""
SELECT_STREAMS_SQL = """
    SELECT stream.uuid, stream.name, stream.provider_identity
    FROM messenger_streams stream
    JOIN messenger_stream_bindings member
      ON member.project_id = stream.project_id AND member.stream_uuid = stream.uuid
    WHERE stream.project_id = %s AND stream.provider_uuid = %s
      AND member.user_uuid = %s AND member.active AND stream.deleted_at IS NULL
"""
SELECT_TOPIC_REFERENCES_SQL = """
    SELECT topic.uuid, topic.name, topic.provider_identity, stream.name AS stream_name
    FROM messenger_topics topic
    JOIN messenger_streams stream
      ON stream.project_id = topic.project_id AND stream.uuid = topic.stream_uuid
    JOIN messenger_user_topic_bindings personal
      ON personal.project_id = topic.project_id AND personal.topic_uuid = topic.uuid
    JOIN messenger_stream_bindings member
      ON member.project_id = stream.project_id AND member.stream_uuid = stream.uuid
     AND member.user_uuid = personal.user_uuid AND member.active
    WHERE topic.project_id = %s AND stream.provider_uuid = %s AND personal.user_uuid = %s
      AND topic.deleted_at IS NULL AND stream.deleted_at IS NULL
      AND (topic.provider_identity=ANY(%s::text[])
           OR stream.name || '>' || topic.name=ANY(%s::text[]))
"""
SELECT_MESSAGE_REFERENCES_SQL = """
    WITH scope(project_id,user_uuid,identities,provider_uuid) AS (
        VALUES (%s::uuid,%s::uuid,%s::text[],%s::uuid)
    )
    SELECT visible.uuid, message.provider_identity AS remote_id
    FROM scope JOIN messenger_messages message
      ON message.project_id=scope.project_id AND message.provider_uuid=scope.provider_uuid
     AND message.provider_identity=ANY(scope.identities)
    CROSS JOIN LATERAL (
        SELECT uuid FROM messenger_message_placements
        WHERE project_id=message.project_id AND message_uuid=message.uuid
        OFFSET 0
    ) placement
    CROSS JOIN LATERAL (
        SELECT visible.uuid FROM messenger_api_user_messages_v1 visible
        WHERE visible.project_id=message.project_id AND visible.user_uuid=scope.user_uuid
          AND visible.uuid=placement.uuid AND visible.visible
        -- Resolve canonical identities before expanding the keyed visibility view.
        OFFSET 0
    ) visible
"""
SELECT_SCOPED_FILE_SQL = """
    SELECT file.* FROM m_workspace_files file
    WHERE file.project_id=%s AND (file.stream_uuid=%s OR file.acl_mode='public')
      AND file.provider_uuid=%s AND file.provider_identity=%s LIMIT 1
"""

SELECT_FILE_SQL = """
    SELECT * FROM m_workspace_files
    WHERE project_id = %s AND provider_uuid = %s AND provider_identity = %s
"""
SELECT_SNAPSHOT_SQL = """
    SELECT * FROM {view} WHERE project_id = %s AND uuid = %s
"""
SELECT_PROJECT_USERS_SQL = """
    SELECT user_uuid FROM messenger_project_users WHERE project_id = %s
"""
ENQUEUE_COUNTERS_SQL = "SELECT messenger_queue_counters(%s,%s,%s,%s)"
SELECT_TOPIC_SQL = """
    SELECT * FROM messenger_topics
    WHERE project_id = %s AND stream_uuid = %s
      AND (provider_identity = %s OR (provider_identity IS NULL AND name=%s AND is_done=%s))
    ORDER BY provider_identity NULLS LAST LIMIT 1
"""
SELECT_STREAM_TOPICS_SQL = """
    SELECT * FROM messenger_topics WHERE project_id = %s AND stream_uuid = %s
"""
# Freshly imported tables can still have tiny cardinality estimates. Resolve
# provider IDs once so a nested loop cannot rescan history for each binding.
SELECT_STATES_SQL = """
    WITH messages AS MATERIALIZED (
        SELECT project_id,uuid,provider_uuid FROM messenger_messages
        WHERE project_id=%s AND source_name='zulip' AND provider_uuid=%s
          AND (%s::text[] IS NULL OR provider_identity=ANY(%s::text[]))
    )
    SELECT binding.uuid,binding.project_id,binding.user_uuid,binding.placement_uuid,
           placement.message_uuid,placement.stream_uuid,placement.topic_uuid,
           NOT EXISTS(SELECT 1 FROM messenger_user_message_unreads flag
               WHERE (flag.project_id,flag.user_uuid,flag.placement_uuid)=
                   (binding.project_id,binding.user_uuid,binding.placement_uuid)) AS read,
           EXISTS(SELECT 1 FROM messenger_user_message_stars flag
               WHERE (flag.project_id,flag.user_uuid,flag.placement_uuid)=
                   (binding.project_id,binding.user_uuid,binding.placement_uuid)) AS starred
    FROM messages message
    JOIN messenger_message_placements placement
      ON placement.project_id=message.project_id AND placement.message_uuid=message.uuid
    JOIN messenger_user_message_bindings binding
      ON binding.project_id=placement.project_id AND binding.placement_uuid=placement.uuid
    JOIN messenger_stream_bindings member
      ON member.project_id=binding.project_id AND member.user_uuid=binding.user_uuid
     AND member.stream_uuid=placement.stream_uuid AND member.active
    JOIN messenger_streams stream
      ON stream.project_id=placement.project_id AND stream.uuid=placement.stream_uuid
    WHERE binding.user_uuid=%s
      AND stream.source_name='zulip' AND stream.provider_uuid=message.provider_uuid
      AND NOT EXISTS (
          SELECT 1 FROM zulip_outgoing_intents intent
          WHERE intent.project_id=binding.project_id AND intent.entity_type='message_state'
            AND intent.status<>'done'
            AND intent.data->>'message_uuid'=placement.message_uuid::text
            AND intent.data->>'user_uuid'=binding.user_uuid::text
      )
"""
UPDATE_FLAGS_SQL = """
    WITH desired(read_set,read_value,star_set,star_value,project_id,user_uuid,bindings) AS (
        VALUES(%s::boolean,%s::boolean,%s::boolean,%s::boolean,%s::uuid,%s::uuid,%s::uuid[])
    ), targets AS MATERIALIZED (
        SELECT binding.*,placement.stream_uuid,placement.topic_uuid,
               desired.read_set,desired.read_value,desired.star_set,desired.star_value
        FROM desired JOIN messenger_user_message_bindings binding
          ON binding.project_id=desired.project_id AND binding.user_uuid=desired.user_uuid
         AND binding.uuid=ANY(desired.bindings)
        JOIN messenger_message_placements placement
          ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
        JOIN messenger_stream_bindings member ON member.project_id=binding.project_id
          AND member.user_uuid=binding.user_uuid AND member.stream_uuid=placement.stream_uuid
          AND member.active
        FOR KEY SHARE OF member
    ), read_removed AS (
        DELETE FROM messenger_user_message_unreads flag USING targets
        WHERE targets.read_set AND targets.read_value
          AND (flag.project_id,flag.user_uuid,flag.placement_uuid)=
              (targets.project_id,targets.user_uuid,targets.placement_uuid)
        RETURNING flag.placement_uuid
    ), read_added AS (
        INSERT INTO messenger_user_message_unreads
        SELECT project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid
        FROM targets WHERE read_set AND NOT read_value
        ON CONFLICT DO NOTHING RETURNING placement_uuid
    ), star_removed AS (
        DELETE FROM messenger_user_message_stars flag USING targets
        WHERE targets.star_set AND NOT targets.star_value
          AND (flag.project_id,flag.user_uuid,flag.placement_uuid)=
              (targets.project_id,targets.user_uuid,targets.placement_uuid)
        RETURNING flag.placement_uuid
    ), star_added AS (
        INSERT INTO messenger_user_message_stars
        SELECT project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid
        FROM targets WHERE star_set AND star_value
        ON CONFLICT DO NOTHING RETURNING placement_uuid
    ), changed AS (
        SELECT placement_uuid FROM read_removed UNION
        SELECT placement_uuid FROM read_added UNION
        SELECT placement_uuid FROM star_removed UNION
        SELECT placement_uuid FROM star_added
    )
    SELECT targets.uuid,targets.placement_uuid FROM targets JOIN changed USING(placement_uuid)
"""
# Each recipient has at most one binding per placement. Keep batch size
# out of the view's join order when a new recipient has no useful statistics.
SELECT_FLAG_SNAPSHOTS_SQL = """
    SELECT snapshot.*
    FROM (SELECT %s::uuid AS project_id, %s::uuid AS user_uuid) recipient
    CROSS JOIN (SELECT DISTINCT unnest(%s::uuid[]) AS uuid) requested
    CROSS JOIN LATERAL (
        SELECT * FROM messenger_api_user_messages_v1
        WHERE project_id=recipient.project_id AND user_uuid=recipient.user_uuid
          AND uuid=requested.uuid
        LIMIT 1
    ) snapshot
"""
SELECT_FILE_RECIPIENTS_SQL = """
    SELECT * FROM m_workspace_visible_files_v1 WHERE uuid = %s
"""
SELECT_REACTION_SNAPSHOT_SQL = """
    SELECT provider_identity, user_uuid, emoji_name FROM messenger_message_reaction_facts
    WHERE project_id=%s AND canonical_message_uuid=%s
      AND provider_uuid=%s AND source_name='zulip'
"""
SELECT_REACTION_SQL = """
    SELECT * FROM messenger_message_reaction_facts
    WHERE project_id = %s AND canonical_message_uuid = %s
      AND user_uuid = %s AND emoji_name = %s
"""
SELECT_TOPIC_BINDING_SQL = """
    SELECT * FROM messenger_user_topic_bindings
    WHERE project_id = %s AND user_uuid = %s AND topic_uuid = %s
"""
SELECT_BINDING_SQL = """
    SELECT * FROM messenger_stream_bindings
    WHERE project_id = %s AND stream_uuid = %s AND user_uuid = %s
"""

SAVE_REACTION_LINK_SQL = """
    INSERT INTO zulip_entity_links (
        project_id, entity_type, entity_uuid, provider_uuid,
        external_account_uuid, provider_identity, data
    ) VALUES (%s, 'reaction', %s, %s, %s, %s, %s)
    ON CONFLICT (entity_type, entity_uuid, provider_uuid, external_account_uuid)
    DO UPDATE SET provider_identity = EXCLUDED.provider_identity, data = EXCLUDED.data
"""
SAVE_ATTACHMENT_LINK_SQL = """
    INSERT INTO zulip_entity_links (
        project_id, entity_type, entity_uuid, provider_uuid,
        external_account_uuid, provider_identity, data
    ) VALUES (%s, 'file', %s, %s, %s, %s, %s)
    ON CONFLICT (entity_type, entity_uuid, provider_uuid, external_account_uuid)
    DO UPDATE SET provider_identity=EXCLUDED.provider_identity,
                  data = zulip_entity_links.data || EXCLUDED.data
"""
SAVE_RECIPIENT_ATTACHMENT_LINKS_SQL = """
    INSERT INTO zulip_entity_links (
        project_id, entity_type, entity_uuid, provider_uuid,
        external_account_uuid, provider_identity, data
    )
    SELECT account.project_id, 'file', %s, account.provider_uuid,
           account.uuid, %s, jsonb_build_object('account_identity',account.external_account_id)
    FROM m_external_accounts_v2 account
    JOIN messenger_stream_bindings member
      ON member.project_id=account.project_id AND member.user_uuid=account.owner_user_uuid
     AND member.stream_uuid=%s AND member.active
    JOIN unnest(%s::uuid[],%s::integer[]) AS target(user_uuid,membership_generation)
      ON target.user_uuid=member.user_uuid
     AND target.membership_generation=member.membership_generation
    WHERE account.project_id=%s AND account.provider_uuid=%s
      AND account.external_account_id IS NOT NULL
    ON CONFLICT (entity_type, entity_uuid, provider_uuid, external_account_uuid)
    DO UPDATE SET provider_identity=EXCLUDED.provider_identity,
                  data=zulip_entity_links.data || EXCLUDED.data
"""
SELECT_ATTACHMENT_LINK_SQL = """
    SELECT to_jsonb(file) AS file
    FROM zulip_entity_links link
    JOIN m_workspace_files file
      ON file.project_id = link.project_id AND file.uuid = link.entity_uuid
    WHERE link.project_id = %s AND link.provider_uuid = %s
      AND link.entity_type = 'file' AND link.data->>'attachment_id' = %s
"""
SELECT_REACTION_COUNTS_SQL = """
    SELECT emoji_name, count(*) AS count, array_agg(user_uuid ORDER BY user_uuid) AS users
    FROM messenger_message_reaction_facts
    WHERE project_id = %s AND canonical_message_uuid = %s
    GROUP BY emoji_name
"""

SELECT_MESSAGE_AUDIENCE_SQL = """
    SELECT user_uuid, membership_generation
    FROM messenger_stream_bindings
    WHERE project_id=%s AND stream_uuid=%s AND active
      AND (%s::uuid IS NULL OR user_uuid=%s)
      AND (membership_started_at <= COALESCE(%s::timestamptz,statement_timestamp())
           OR (user_uuid=%s AND membership_generation=1))
    ORDER BY user_uuid
"""
ENSURE_MESSAGE_BINDINGS_SQL = """
    WITH members AS MATERIALIZED (
        SELECT member.project_id,member.user_uuid
        FROM messenger_stream_bindings member
        JOIN unnest(%s::uuid[],%s::integer[]) AS target(user_uuid,membership_generation)
          ON target.user_uuid=member.user_uuid
         AND target.membership_generation=member.membership_generation
        WHERE member.project_id=%s AND member.stream_uuid=%s AND member.active
        FOR KEY SHARE OF member
    )
    INSERT INTO messenger_user_message_bindings(uuid,project_id,placement_uuid,user_uuid)
    SELECT messenger_uuid_v5(%s,member.user_uuid::text),member.project_id,%s,member.user_uuid
    FROM members member
    ON CONFLICT(project_id,placement_uuid,user_uuid) DO NOTHING
    RETURNING user_uuid
"""
ENSURE_MESSAGE_MENTIONS_SQL = """
    WITH targets AS MATERIALIZED (
        SELECT binding.project_id,binding.user_uuid,binding.placement_uuid,
               placement.stream_uuid,placement.topic_uuid,
               position('](urn:user:' || binding.user_uuid::text || ')' IN %s)>0 AS mentioned
        FROM messenger_user_message_bindings binding
        JOIN messenger_message_placements placement
          ON placement.project_id=binding.project_id AND placement.uuid=binding.placement_uuid
        WHERE binding.project_id=%s AND binding.placement_uuid=%s
          AND binding.user_uuid=ANY(%s::uuid[])
    ), removed AS (
        DELETE FROM messenger_user_message_mentions flag USING targets
        WHERE NOT targets.mentioned AND (flag.project_id,flag.user_uuid,flag.placement_uuid)=
              (targets.project_id,targets.user_uuid,targets.placement_uuid)
        RETURNING flag.user_uuid
    ), added AS (
        INSERT INTO messenger_user_message_mentions
        SELECT project_id,user_uuid,placement_uuid,stream_uuid,topic_uuid FROM targets
        WHERE mentioned ON CONFLICT DO NOTHING RETURNING user_uuid
    )
    SELECT user_uuid FROM removed UNION SELECT user_uuid FROM added
"""
ENSURE_TOPIC_BINDINGS_SQL = """
    INSERT INTO messenger_user_topic_bindings (
        uuid, project_id, topic_uuid, user_uuid,
        source_name, provider_uuid, provider_identity, status
    )
    SELECT messenger_uuid_v5(%s, member.user_uuid::text), member.project_id,
           %s, member.user_uuid,
           'zulip', parent.provider_uuid, %s || ':' || actor.external_account_id,
           CASE WHEN actor.external_account_id IS NULL THEN 'new' ELSE 'active' END
    FROM messenger_stream_bindings member
    JOIN messenger_streams parent ON parent.project_id=member.project_id AND parent.uuid=member.stream_uuid
    LEFT JOIN LATERAL (
        SELECT provider_uuid, external_account_id FROM m_external_accounts_v2
        WHERE project_id = member.project_id AND owner_user_uuid = member.user_uuid
          AND provider_uuid = %s AND external_account_id IS NOT NULL
        ORDER BY uuid LIMIT 1
    ) actor ON true
    WHERE member.project_id = %s AND member.stream_uuid = %s AND member.active
      AND member.user_uuid = ANY(%s::uuid[])
      AND NOT EXISTS (
          SELECT 1 FROM messenger_user_topic_bindings existing
          WHERE existing.project_id=member.project_id AND existing.topic_uuid=%s
            AND existing.user_uuid=member.user_uuid
            AND existing.provider_identity IS NOT DISTINCT FROM
                (%s || ':' || actor.external_account_id)
      )
    ON CONFLICT (project_id, user_uuid, topic_uuid) DO UPDATE SET
        source_name=EXCLUDED.source_name,provider_uuid=EXCLUDED.provider_uuid,
        provider_identity=EXCLUDED.provider_identity,status=EXCLUDED.status
    WHERE messenger_user_topic_bindings.provider_identity IS DISTINCT FROM EXCLUDED.provider_identity
    RETURNING user_uuid
"""

INSERT_MISSING_TOPIC_BINDINGS_SQL = """
    INSERT INTO messenger_user_topic_bindings (
        uuid, project_id, topic_uuid, user_uuid,
        source_name, provider_uuid, provider_identity, status
    )
    SELECT messenger_uuid_v5(%s, member.user_uuid::text), member.project_id,
           %s, member.user_uuid,
           'zulip', parent.provider_uuid, %s || ':' || actor.external_account_id,
           CASE WHEN actor.external_account_id IS NULL THEN 'new' ELSE 'active' END
    FROM messenger_stream_bindings member
    JOIN messenger_streams parent ON parent.project_id=member.project_id AND parent.uuid=member.stream_uuid
    LEFT JOIN LATERAL (
        SELECT provider_uuid, external_account_id FROM m_external_accounts_v2
        WHERE project_id = member.project_id AND owner_user_uuid = member.user_uuid
          AND provider_uuid = %s AND external_account_id IS NOT NULL
        ORDER BY uuid LIMIT 1
    ) actor ON true
    WHERE member.project_id = %s AND member.stream_uuid = %s AND member.active
      AND member.user_uuid = ANY(%s::uuid[])
      AND NOT EXISTS (
          SELECT 1 FROM messenger_user_topic_bindings existing
          WHERE existing.project_id=member.project_id AND existing.topic_uuid=%s
            AND existing.user_uuid=member.user_uuid
      )
    ON CONFLICT (project_id, user_uuid, topic_uuid) DO NOTHING
    RETURNING user_uuid
"""

SAVE_ATTACHMENT_SQL = """
    INSERT INTO zulip_incoming_attachments
        (project_id, provider_uuid, attachment_id, path, name, owner_identity)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (project_id, provider_uuid, attachment_id) DO UPDATE
    SET path=EXCLUDED.path,name=EXCLUDED.name,owner_identity=EXCLUDED.owner_identity,
        updated_at=now()
    WHERE (zulip_incoming_attachments.path,zulip_incoming_attachments.name,
           zulip_incoming_attachments.owner_identity) IS DISTINCT FROM
          (EXCLUDED.path,EXCLUDED.name,EXCLUDED.owner_identity)
"""
DELETE_ATTACHMENT_SQL = """
    DELETE FROM zulip_incoming_attachments
    WHERE project_id=%s AND provider_uuid=%s AND attachment_id=%s
"""
SELECT_ATTACHMENT_SQL = """
    SELECT * FROM zulip_incoming_attachments
    WHERE project_id = %s AND provider_uuid = %s AND attachment_id = %s
"""
SELECT_EMPTY_TOPIC_SQL = """
    SELECT topic.* FROM messenger_topics topic
    JOIN messenger_streams stream
      ON stream.project_id = topic.project_id AND stream.uuid = topic.stream_uuid
    WHERE topic.project_id = %s AND topic.uuid = %s AND topic.deleted_at IS NULL
      AND topic.uuid IS DISTINCT FROM stream.default_topic_uuid
      AND NOT EXISTS (
          SELECT 1 FROM messenger_message_placements placement
          JOIN messenger_messages message
            ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
          WHERE placement.project_id = topic.project_id AND placement.topic_uuid = topic.uuid
            AND message.deleted_at IS NULL
      )
"""
SELECT_PROVIDER_BINDINGS_SQL = """
    SELECT stream.* FROM messenger_streams stream
    JOIN messenger_stream_bindings member
      ON member.project_id = stream.project_id AND member.stream_uuid = stream.uuid
    WHERE member.project_id = %s AND member.user_uuid = %s AND member.active
      AND stream.provider_uuid = %s
"""
SELECT_PENDING_PERSONAL_SQL = """
    SELECT 1 FROM zulip_outgoing_intents
    WHERE project_id = %s AND entity_type = %s AND status <> 'done'
      AND data->>'message_uuid' = %s AND data->>'user_uuid' = %s
      AND (entity_type <> 'reaction' OR data->>'emoji_name' = %s)
    LIMIT 1
"""

TABLES = outgoing_operations.TABLES
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


def stream_identity(account: dict, message: dict) -> str:
    if message["type"] == "stream":
        return str(message["stream_id"])
    members = ",".join(
        str(value)
        for value in sorted(item["id"] for item in message["display_recipient"])
    )
    return "dm:" + str(uuid.uuid5(account["provider_uuid"], f"dm:{members}"))


def origin(account: dict, identity: str) -> dict:
    return {
        "source_name": "zulip",
        "provider_uuid": account["provider_uuid"],
        "provider_identity": identity,
        "status": "active",
    }


async def rows(
    connection: typing.Any, query: str, parameters: tuple = ()
) -> list[dict]:
    return await (await connection.execute(query, parameters)).fetchall()


def references(messages: list) -> dict:
    people = {str(item["sender_id"]) for item in messages}
    names, remote_topics, named_topics, message_ids = set(), set(), set(), set()
    for item in messages:
        content = item["content"]
        for name, remote in re.findall(r"@_?\*\*([^*|]+)(?:\|([0-9]+))?\*\*", content):
            if remote:
                people.add(remote)
            else:
                names.add(name)
        for value in re.findall(r"#\*\*([^*]+)\*\*", content):
            topic, separator, remote = value.rpartition("@")
            if separator and remote.isdigit():
                message_ids.add(remote)
            elif ">" in value:
                named_topics.add(value)
        message_ids.update(re.findall(r"near/([0-9]+)", content))
        for stream, topic in re.findall(
            r"channel/(\d+)(?:-[^/\s)]+)?/topic/([^/\s)]+)", content
        ):
            topic = urllib.parse.unquote(re.sub(r"\.([\da-fA-F]{2})", r"%\1", topic))
            remote_topics.add(f"{stream}:{topic}")
    return {
        "people": list(people),
        "names": list(names),
        "remote_topics": list(remote_topics),
        "named_topics": list(named_topics),
        "messages": list(message_ids),
    }


def _uuid_text(cache: dict, identifier: object) -> str:
    if identifier not in cache:
        cache[identifier] = str(identifier)
    return cache[identifier]


def conversion_context_base(
    account: dict, prepared: dict, *, uuid_strings: dict | None = None
) -> dict:
    """Resolve the immutable catalog once for a history page."""
    strings: dict = {} if uuid_strings is None else uuid_strings
    mentions, streams, stream_ids, direct, messages, topics = {}, {}, {}, {}, {}, {}
    for person in prepared.get("catalog", []):
        user = _uuid_text(strings, person["owner_user_uuid"])
        mentions[f"id:{person['external_account_id']}"] = user
        if person["first_name"]:
            mentions[f"name:{person['first_name']}"] = user
    for item in prepared.get("streams", []):
        identifier = _uuid_text(strings, item["uuid"])
        streams[item["name"]] = identifier
        stream_ids[item["name"]] = item["provider_identity"]
        if item["provider_identity"].startswith("dm:"):
            direct[item["provider_identity"]] = identifier
    for item in prepared.get("references", []):
        messages[item["remote_id"]] = _uuid_text(strings, item["uuid"])
    for item in prepared.get("topics", []):
        topics[f"{item['stream_name']}>{item['name']}"] = _uuid_text(
            strings, item["uuid"]
        )
    return {
        "server_url": account["settings"]["server_url"],
        "zulip_namespace": _uuid_text(strings, account["provider_uuid"]),
        "own_user_id": account["external_account_id"],
        "messages": prepared.get("quoted_messages", {}),
        "mention_uuids": types.MappingProxyType(mentions),
        "stream_uuids": types.MappingProxyType(streams),
        "stream_ids": types.MappingProxyType(stream_ids),
        "dm_stream_uuids": types.MappingProxyType(direct),
        "message_uuids": types.MappingProxyType(messages),
        "topic_uuids": types.MappingProxyType(topics),
        "uuid_strings": strings,
    }


def conversion_context(
    account: dict,
    data: dict,
    stream: dict,
    topic: dict,
    public_uuid: object,
    prepared: dict,
    *,
    base: dict | None = None,
) -> dict:
    # History only reads these mapping views. Single-event callers keep plain
    # dictionaries, including the exact JSON shape used by the content cache.
    shared = base is not None
    if base is None:
        base = conversion_context_base(account, prepared)
    strings = base["uuid_strings"]
    public = _uuid_text(strings, public_uuid)
    stream_uuid = _uuid_text(strings, stream["uuid"])
    topic_uuids = {}
    stream_id = stream["provider_identity"]
    if data["type"] == "stream":
        stream_id = str(data["stream_id"])
        topic_uuids[f"{stream['name']}>{data['subject']}"] = _uuid_text(
            strings, topic["uuid"]
        )
    context = {
        "server_url": base["server_url"],
        "original_url": base["server_url"].rstrip("/") + f"/#narrow/near/{data['id']}",
        "zulip_namespace": base["zulip_namespace"],
        "own_user_id": base["own_user_id"],
        "normalize_emoji": True,
        "strict_identities": True,
        "current_message_uuid": public,
        # Catalog entries used to be assigned after these per-message defaults.
        # ChainMap preserves both their precedence and insertion order.
        "message_uuids": collections.ChainMap(
            base["message_uuids"], {str(data["id"]): public}
        ),
        "topic_uuids": collections.ChainMap(base["topic_uuids"], topic_uuids),
        "messages": base["messages"],
        "mention_uuids": base["mention_uuids"],
        "stream_uuids": collections.ChainMap(
            {stream["name"]: stream_uuid}, base["stream_uuids"]
        ),
        "stream_ids": collections.ChainMap(
            {stream["name"]: stream_id}, base["stream_ids"]
        ),
        "dm_stream_uuids": collections.ChainMap(
            {stream["provider_identity"]: stream_uuid}
            if stream["provider_identity"].startswith("dm:")
            else {},
            base["dm_stream_uuids"],
        ),
        "files": {},
    }
    if not shared:
        for name in (
            "message_uuids",
            "topic_uuids",
            "mention_uuids",
            "stream_uuids",
            "stream_ids",
            "dm_stream_uuids",
        ):
            context[name] = dict(context[name])
    return context


class MissingSharedMessage(Exception):
    pass


class MissingDependency(Exception):
    def __init__(self, spec: dict):
        self.spec = spec
        super().__init__(f"Waiting for Zulip {spec['kind']} {spec['identity']}")


class Operations:
    emit_public_event = True

    def __init__(
        self, db: typing.Any, http: typing.Any, reaction_user_limit: int = 5
    ) -> None:
        self.db, self.http = db, http
        self.reaction_user_limit = reaction_user_limit

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
        )
        found = (
            await self.db.sql(query, parameters)
            if connection is None
            else await rows(connection, query, parameters)
        )
        return (
            found[0]["uuid"]
            if found
            else uuid.uuid5(
                account["project_id"], f"{account['provider_uuid']}:{kind}:{remote}"
            )
        )

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

    async def initialize(
        self, connection: typing.Any, account: dict, person: dict
    ) -> dict:
        identity = str(person["user_id"])
        existing = await rows(
            connection,
            SELECT_USER_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                identity,
            ),
        )
        merged = existing and existing[0]["uuid"] != account["uuid"]
        values = dict(account)
        if merged:
            target = existing[0]
            if target["user_source"] not in ("zulip", "hidden") or target[
                "settings"
            ].get("api_key"):
                raise ValueError(
                    "This Zulip account is already linked to a Workspace user"
                )
            await connection.execute(
                DELETE_ROW_SQL.format(table="m_external_accounts_v2"),
                (
                    account["project_id"],
                    account["uuid"],
                ),
            )
            await self.account_snapshot(
                connection, account, kind="external_account.deleted"
            )
            values["uuid"] = target["uuid"]
        values["external_account_id"] = identity
        subscription = await rows(
            connection,
            SELECT_LIVE_SUBSCRIPTION_SQL,
            (
                values["uuid"],
                values["provider_uuid"],
                values["owner_user_uuid"],
                identity,
                account["settings"]["server_url"],
            ),
        )
        values["status"] = "active" if subscription else "syncing"
        values["error_description"] = None
        if (
            account["external_account_id"] is not None
            and account["external_account_id"] != identity
        ):
            for stream in await rows(
                connection,
                SELECT_PROVIDER_BINDINGS_SQL,
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    account["provider_uuid"],
                ),
            ):
                await self.bind(
                    connection,
                    account,
                    stream,
                    account["owner_user_uuid"],
                    account["external_account_id"],
                    active=False,
                )
        changed = await self.save(
            connection,
            "m_external_accounts_v2",
            {
                key: values[key]
                for key in (
                    "uuid",
                    "project_id",
                    "external_account_id",
                    "owner_user_uuid",
                    "settings",
                    "disabled",
                    "status",
                    "error_description",
                )
            },
        )
        await connection.execute(
            REGISTER_USER_SQL,
            (
                account["project_id"],
                account["owner_user_uuid"],
            ),
        )
        legacy_users = {uuid.uuid5(account["provider_uuid"], f"user:{identity}")}
        if merged:
            legacy_users.add(target["owner_user_uuid"])
        for legacy in sorted(legacy_users, key=str):
            changed_streams = await rows(
                connection,
                "SELECT zulip_rebind_direct_user(%s,%s,%s,%s) AS uuid",
                (
                    account["project_id"],
                    account["provider_uuid"],
                    legacy,
                    account["owner_user_uuid"],
                ),
            )
            for stream in changed_streams:
                await self.snapshot(
                    connection,
                    values,
                    "stream",
                    stream["uuid"],
                    "updated",
                    stream["uuid"],
                )
                for topic in await rows(
                    connection,
                    SELECT_STREAM_TOPICS_SQL,
                    (account["project_id"], stream["uuid"]),
                ):
                    await self.snapshot(
                        connection,
                        values,
                        "topic",
                        topic["uuid"],
                        "created",
                        stream["uuid"],
                    )
        if merged:
            # Unlinking deactivates this account's chats. An explicit new link
            # restores its own DMs, including groups, while ordinary history
            # replay still respects inactive memberships.
            for stream in await rows(
                connection,
                """
                SELECT stream.* FROM messenger_streams stream
                JOIN messenger_stream_bindings member ON member.project_id=stream.project_id
                    AND member.stream_uuid=stream.uuid
                WHERE stream.project_id=%s AND stream.provider_uuid=%s
                    AND stream.source_name='zulip' AND stream.provider_identity LIKE 'dm:%%'
                    AND member.user_uuid=%s AND NOT member.active
                    AND member.source_name='zulip' AND member.provider_uuid=stream.provider_uuid
                    AND member.provider_identity=stream.provider_identity||':'||%s
                """,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    account["owner_user_uuid"],
                    identity,
                ),
            ):
                await self.bind(
                    connection, values, stream, account["owner_user_uuid"], identity
                )
        if changed:
            await self.account_snapshot(
                connection,
                values,
                kind="external_account.created"
                if merged
                else "external_account.updated",
            )
        return values

    async def account_snapshot(
        self,
        connection: typing.Any,
        account: dict,
        *,
        kind: str = "external_account.updated",
    ) -> None:
        snapshot = {
            key: value
            for key, value in account.items()
            if key not in ("owner_user_uuid", "hostname", "user_source")
        }
        snapshot["settings"] = {
            key: value for key, value in account["settings"].items() if key != "api_key"
        }
        await self.publish(
            connection,
            account,
            kind,
            account["uuid"],
            [{"uuid": account["uuid"], "snapshot": snapshot}],
            recipients=[account["owner_user_uuid"]],
        )

    async def account_error(self, account: dict, error: str) -> None:
        async with self.db.transaction() as connection:
            changed = await self.save(
                connection,
                "m_external_accounts_v2",
                {
                    "uuid": account["uuid"],
                    "project_id": account["project_id"],
                    "status": "error",
                    "error_description": error,
                },
            )
            if changed:
                await self.account_snapshot(connection, {**account, **changed})

    async def dependency(
        self, account: dict, spec: dict, connection: typing.Any | None = None
    ) -> dict:
        async def query(sql: str, parameters: tuple) -> list[dict]:
            return (
                await self.db.sql(sql, parameters)
                if connection is None
                else await rows(connection, sql, parameters)
            )

        kind, remote = spec["kind"], spec["identity"]
        if kind == "user":
            found = await query(
                SELECT_USER_SQL,
                (account["project_id"], account["provider_uuid"], remote),
            )
        elif kind == "stream":
            found = await query(
                "SELECT * FROM messenger_streams WHERE project_id=%s AND provider_uuid=%s AND source_name='zulip' AND provider_identity=%s AND deleted_at IS NULL",
                (account["project_id"], account["provider_uuid"], remote),
            )
        else:
            stream = await self.dependency(
                account,
                {
                    "kind": "stream",
                    "identity": spec["stream_identity"],
                    **(
                        {"recipients": spec["recipients"]}
                        if "recipients" in spec
                        else {}
                    ),
                },
                connection,
            )
            if kind == "topic":
                found = await query(
                    SELECT_TOPIC_SQL,
                    (
                        account["project_id"],
                        stream["uuid"],
                        remote,
                        spec["subject"].removeprefix("✔ ") or "General",
                        spec["subject"].startswith("✔ "),
                    ),
                )
                found = [row for row in found if row["deleted_at"] is None]
            else:
                user = await self.dependency(
                    account, {"kind": "user", "identity": spec["user"]}, connection
                )
                if kind == "stream_binding":
                    found = await query(
                        SELECT_BINDING_SQL,
                        (
                            account["project_id"],
                            stream["uuid"],
                            user["owner_user_uuid"],
                        ),
                    )
                    if found and not found[0]["active"]:
                        if spec["user"] != account["external_account_id"]:
                            # This account cannot reopen another recipient's chat.
                            return found[0]
                        raise ValueError(
                            "Incoming message belongs to an inactive membership"
                        )
                elif kind == "topic_binding":
                    topic = await self.dependency(
                        account,
                        {
                            "kind": "topic",
                            "identity": remote,
                            "stream_identity": spec["stream_identity"],
                            "subject": spec["subject"],
                        },
                        connection,
                    )
                    found = await query(
                        SELECT_TOPIC_BINDING_SQL,
                        (account["project_id"], user["owner_user_uuid"], topic["uuid"]),
                    )
                else:
                    raise ValueError("Unknown incoming dependency type")
        if (
            not found
            or (
                kind in ("topic", "stream_binding", "topic_binding")
                and found[0]["provider_identity"] is None
            )
            or (
                kind == "topic"
                and not spec["subject"]
                and stream["default_topic_uuid"] is None
            )
        ):
            raise MissingDependency(spec)
        return found[0]

    async def message_dependencies(
        self,
        account: dict,
        messages: list,
        connection: typing.Any | None = None,
        event: dict | None = None,
    ) -> None:
        for message in messages:
            people = {
                message["sender_id"],
                *(
                    int(value)
                    for value in re.findall(r"\|([0-9]+)\*\*", message["content"])
                ),
            }
            if message["type"] != "stream":
                people.update(item["id"] for item in message["display_recipient"])
            for person in sorted(people):
                await self.dependency(
                    account, {"kind": "user", "identity": str(person)}, connection
                )
            remote = stream_identity(account, message)
            context = {"stream_identity": remote}
            if message["type"] != "stream":
                context["recipients"] = message["display_recipient"]
            await self.dependency(
                account, {"kind": "stream", "identity": remote, **context}, connection
            )
            members = (
                [account["external_account_id"]]
                if message["type"] == "stream"
                else [str(item["id"]) for item in message["display_recipient"]]
            )
            for member in members:
                await self.dependency(
                    account,
                    {
                        "kind": "stream_binding",
                        "identity": remote,
                        "user": member,
                        **context,
                    },
                    connection,
                )
            subject = message["subject"] if message["type"] == "stream" else ""
            if (
                event
                and event["type"] == "update_message"
                and event.get("propagate_mode") == "change_all"
                and "orig_subject" in event
                and "subject" in event
                and "new_stream_id" not in event
            ):
                # The existing rename handler updates this topic in the parent transaction.
                subject = event["orig_subject"]
            await self.dependency(
                account,
                {
                    "kind": "topic_binding",
                    "identity": f"{remote}:{subject}",
                    "subject": subject,
                    "user": account["external_account_id"],
                    **context,
                },
                connection,
            )

    async def prepare_dependency(self, account: dict, spec: dict) -> dict:
        prepared = {"event": {"type": "ensure_dependency", "dependency": spec}}
        try:
            await self.dependency(account, spec)
        except MissingDependency as missing:
            if missing.spec != spec:
                raise
        else:
            return prepared
        kind = spec["kind"]
        if kind == "user":
            prepared["person"] = (
                await self.http.request(account, "GET", f"/users/{spec['identity']}")
            )["user"]
            if str(prepared["person"]["user_id"]) != spec["identity"]:
                raise ValueError("Provider returned a different dependency user")
        elif kind == "stream" and "recipients" not in spec:
            prepared["channel"] = (
                await self.http.request(account, "GET", f"/streams/{spec['identity']}")
            )["stream"]
            if str(prepared["channel"]["stream_id"]) != spec["identity"]:
                raise ValueError("Provider returned a different dependency stream")
        elif kind == "stream" or kind == "stream_binding":
            people = spec.get("recipients", []) or [{"id": spec["user"]}]
            for person in people:
                await self.dependency(
                    account, {"kind": "user", "identity": str(person["id"])}
                )
        return prepared

    async def ensure_dependency(
        self, connection: typing.Any, account: dict, prepared: dict
    ) -> None:
        spec = prepared["event"]["dependency"]
        try:
            await self.dependency(account, spec, connection)
        except MissingDependency as missing:
            if missing.spec != spec:
                raise
        else:
            return
        kind = spec["kind"]
        if kind == "user":
            await self.person(connection, account, prepared["person"])
        elif kind == "stream":
            if "recipients" in spec:
                members = sorted(spec["recipients"], key=lambda item: item["id"])
                owner = await self.user(connection, account, members[0]["id"])
                peer = await self.user(connection, account, members[-1]["id"])
                identifier = await self.identity(
                    account, "stream", spec["identity"], connection=connection
                )
                private = len(members) <= 2
                await self.save(
                    connection,
                    TABLES["stream"],
                    {
                        "uuid": identifier,
                        "project_id": account["project_id"],
                        "name": ", ".join(item["full_name"] for item in members),
                        "owner_uuid": owner,
                        "private": private,
                        "direct_user_uuid": peer if private else None,
                        "private_index": f"zulip:{identifier}" if private else None,
                        "invite_only": True,
                        **origin(account, spec["identity"]),
                    },
                )
            else:
                await self.stream(
                    connection, account, prepared["channel"], subscribe=False
                )
        else:
            stream = await self.dependency(
                account,
                {"kind": "stream", "identity": spec["stream_identity"]},
                connection,
            )
            if kind == "topic":
                topic = await self.topic(connection, account, stream, spec["subject"])
                if not spec["subject"] and stream["default_topic_uuid"] is None:
                    await self.save(
                        connection,
                        TABLES["stream"],
                        {
                            "uuid": stream["uuid"],
                            "project_id": account["project_id"],
                            "default_topic_uuid": topic["uuid"],
                        },
                    )
            else:
                user = await self.user(connection, account, spec["user"])
                if kind == "stream_binding":
                    await self.bind(connection, account, stream, user, spec["user"])
                else:
                    topic = await self.dependency(
                        account,
                        {
                            "kind": "topic",
                            "identity": spec["identity"],
                            "stream_identity": spec["stream_identity"],
                            "subject": spec["subject"],
                        },
                        connection,
                    )
                    await self.topic_binding(
                        connection, account, topic, user, spec["user"], spec["identity"]
                    )

    async def prepare(self, account: dict, event: dict) -> dict:
        prepared: dict[str, typing.Any] = {
            "event": event,
            "messages": [],
            "people": {},
            "channels": {},
            "files": {},
        }
        kind = event["type"]
        if event.get("_personal_only"):
            return {"event": event, "personal": True}
        if kind == "ensure_dependency":
            return await self.prepare_dependency(account, event["dependency"])
        message_ids = []
        if kind == "message":
            if "_shared_key" in event:
                known = await self.db.sql(
                    "SELECT payload,deleted_at FROM messenger_messages_with_content_v1 WHERE project_id=%s AND provider_uuid=%s AND source_name='zulip' AND provider_identity=%s",
                    (
                        account["project_id"],
                        account["provider_uuid"],
                        str(event["message"]["id"]),
                    ),
                )
                if known and (
                    known[0]["deleted_at"] is not None
                    or known[0]["payload"]
                    == {"kind": "markdown", "content": event["message"]["content"]}
                ):
                    return {"event": event, "personal": True}
            prepared["messages"] = [dict(event["message"])]
            if "flags" in event:
                prepared["messages"][0]["flags"] = event["flags"]
        elif kind == "update_message":
            message_ids = event.get("message_ids", [event.get("message_id")])
        elif kind == "reaction":
            for identifier in [event["message_id"]]:
                message_uuid = await self.identity(account, "message", identifier)
                known = await self.db.sql(
                    SELECT_ROW_SQL.format(table=TABLES["message"]),
                    (
                        account["project_id"],
                        message_uuid,
                    ),
                )
                if not known:
                    message_ids.append(identifier)
        if kind == "update_message" and "edit_timestamp" in event:
            clocks = await self.db.sql(
                SELECT_MESSAGE_CLOCKS_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    list(map(str, message_ids)),
                ),
            )
            edited = datetime.datetime.fromtimestamp(
                event["edit_timestamp"], datetime.timezone.utc
            ).replace(tzinfo=None)
            newer_local = {
                row["provider_identity"]
                for row in clocks
                if row["content_updated_at"].replace(tzinfo=None) > edited
            }
            message_ids = [
                identifier
                for identifier in message_ids
                if str(identifier) not in newer_local
            ]
        for identifier in message_ids:
            try:
                data = await self.http.request(
                    account,
                    "GET",
                    f"/messages/{identifier}",
                    {
                        "apply_markdown": False,
                    },
                )
            except outgoing_operations.ApiError as error:
                if not outgoing_operations.Operations.absent(error):
                    raise
                prepared.setdefault("deleted", []).append(identifier)
            else:
                message = dict(data["message"])
                if (
                    kind == "update_message"
                    and "flags" in event
                    and identifier == event.get("message_id")
                ):
                    message["flags"] = event["flags"]
                if (
                    kind == "update_message"
                    and "edit_timestamp" in event
                    and event["edit_timestamp"]
                    >= (message.get("last_edit_timestamp") or message["timestamp"])
                ):
                    for field in ("content", "subject"):
                        if field in event:
                            message[field] = event[field]
                    if "new_stream_id" in event:
                        message["stream_id"] = event["new_stream_id"]
                    message["last_edit_timestamp"] = event["edit_timestamp"]
                prepared["messages"].append(message)
        people = {
            channel["creator_id"]
            for channel in prepared["channels"].values()
            if channel.get("creator_id") is not None
        }
        if kind == "realm_user":
            person = event["person"]
            if "full_name" in person and "email" in person:
                prepared["people"][person["user_id"]] = person
            else:
                people.add(person["user_id"])
        if kind == "reaction":
            people.add(event["user_id"])
        if kind == "subscription" and event["op"] in ("peer_add", "peer_remove"):
            people.update(event["user_ids"])
        await self.message_dependencies(account, prepared["messages"], event=event)
        for message in prepared["messages"]:
            people.add(message["sender_id"])
            people.update(item["user_id"] for item in message.get("reactions", []))
            people.update(
                int(value)
                for value in re.findall(r"\|([0-9]+)\*\*", message["content"])
            )
            if message["type"] != "stream":
                people.update(item["id"] for item in message["display_recipient"])
            file_stream = await self.identity(
                account, "stream", stream_identity(account, message)
            )
            owner = await self.db.sql(
                SELECT_USER_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    str(message["sender_id"]),
                ),
            )
            file_owner = (
                owner[0]["owner_user_uuid"]
                if owner
                else uuid.uuid5(
                    account["provider_uuid"], f"user:{message['sender_id']}"
                )
            )
            message["_files"] = []
            for target in message_conversion.get_zulip_files(message, {}):
                endpoint = message_conversion.parse_url(
                    target, account["settings"]["server_url"]
                )
                if (
                    endpoint is None
                    or endpoint.hostname != account["hostname"]
                    or not endpoint.path.startswith("/user_uploads/")
                ):
                    continue
                path = message_conversion.zulip_upload_path(endpoint.path)
                if path is None:
                    key = (file_stream, endpoint.path)
                    message["_files"].append((target, endpoint.path))
                    prepared["files"][key] = {"missing": 403}
                    continue
                key = (file_stream, path)
                message["_files"].append((target, path))
                if not base.FileProviderIdentity().validate(path):
                    prepared["files"][key] = {"missing": "path_too_long"}
                    continue
                if key in prepared["files"]:
                    continue
                existing = await self.db.sql(
                    SELECT_SCOPED_FILE_SQL,
                    (
                        account["project_id"],
                        file_stream,
                        account["provider_uuid"],
                        path,
                    ),
                )
                if not existing and path != endpoint.path:
                    existing = await self.db.sql(
                        SELECT_SCOPED_FILE_SQL,
                        (
                            account["project_id"],
                            file_stream,
                            account["provider_uuid"],
                            endpoint.path,
                        ),
                    )
                if existing:
                    prepared["files"][key] = {
                        "existing": {**existing[0], "provider_identity": path}
                    }
                    continue
                try:
                    content, content_type = await self.http.download(account, path)
                except outgoing_operations.ApiError as error:
                    if error.status not in (403, 404):
                        raise
                    prepared["files"][key] = {"missing": error.status}
                    continue
                identifier = uuid.uuid5(
                    file_stream, f"{account['provider_uuid']}:file:{path}"
                )
                storage = await asyncio.to_thread(
                    file_storage.save_workspace_file, identifier, content
                )
                created_at = datetime.datetime.fromtimestamp(
                    message["timestamp"], datetime.timezone.utc
                )
                name = urllib.parse.unquote(path.rsplit("/", 1)[1])
                digest = hashlib.sha256(content).hexdigest()
                metadata = file_storage.WorkspaceFileMetadata(
                    uuid=identifier,
                    project_id=account["project_id"],
                    stream_uuid=file_stream,
                    owner_uuid=file_owner,
                    name=name,
                    description="",
                    content_type=content_type,
                    size_bytes=len(content),
                    sha256=digest,
                    created_at=created_at,
                )
                await asyncio.to_thread(
                    file_storage.save_workspace_file_metadata,
                    metadata,
                    storage.storage_type,
                )
                prepared["files"][key] = {
                    "uuid": identifier,
                    "name": name,
                    "created_at": created_at,
                    "content_type": content_type,
                    "size_bytes": len(content),
                    "hash": digest,
                    "storage_type": storage.storage_type,
                    "storage_id": storage.storage_id,
                    "storage_object_id": storage.storage_object_id,
                }
        if (kind == "stream" and event["op"] == "create") or (
            kind == "subscription" and event["op"] == "add"
        ):
            for item in event.get("streams", event.get("subscriptions", [])):
                prepared["channels"][item["stream_id"]] = item
        for identifier in people:
            known = await self.db.sql(
                SELECT_USER_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    str(identifier),
                ),
            )
            if not known and kind != "realm_user":
                raise MissingDependency({"kind": "user", "identity": str(identifier)})
            if kind == "realm_user":
                try:
                    result = await self.http.request(
                        account, "GET", f"/users/{identifier}"
                    )
                except outgoing_operations.ApiError as error:
                    if error.status != 404:
                        raise
                else:
                    prepared["people"][identifier] = result["user"]
        references_ = references(prepared["messages"])
        prepared["catalog"] = await self.db.sql(
            SELECT_CATALOG_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                references_["people"],
                references_["names"],
            ),
        )
        scope = (
            account["project_id"],
            account["provider_uuid"],
            account["owner_user_uuid"],
        )
        prepared["streams"] = (
            await self.db.sql(SELECT_STREAMS_SQL, scope) if prepared["messages"] else []
        )
        prepared["topics"] = (
            await self.db.sql(
                SELECT_TOPIC_REFERENCES_SQL,
                (*scope, references_["remote_topics"], references_["named_topics"]),
            )
            if references_["remote_topics"] or references_["named_topics"]
            else []
        )
        referenced = references_["messages"]
        prepared["references"] = (
            await self.db.sql(
                SELECT_MESSAGE_REFERENCES_SQL,
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    list(referenced),
                    account["provider_uuid"],
                ),
            )
            if referenced
            else []
        )
        prepared["quoted_messages"] = {}
        if any("quote" in message["content"] for message in prepared["messages"]):
            for identifier in referenced:
                try:
                    remote = await self.http.request(
                        account,
                        "GET",
                        f"/messages/{identifier}",
                        {"apply_markdown": False},
                    )
                except outgoing_operations.ApiError as error:
                    if (
                        error.status != 403
                        and not outgoing_operations.Operations.absent(error)
                    ):
                        raise
                else:
                    prepared["quoted_messages"][identifier] = remote["message"]
        return prepared

    async def person(self, connection: typing.Any, account: dict, data: dict) -> object:
        remote = str(data["user_id"])
        found = await rows(
            connection,
            SELECT_USER_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                remote,
            ),
        )
        identifier = (
            found[0]["owner_user_uuid"]
            if found
            else uuid.uuid5(account["provider_uuid"], f"user:{remote}")
        )
        if not found or found[0]["user_source"] in ("zulip", "hidden"):
            changed = await rows(
                connection,
                UPSERT_PERSON_SQL,
                (
                    identifier,
                    f"zulip-{identifier}",
                    "urn:gravatar:"
                    + hashlib.md5(
                        (data.get("email") or str(identifier)).strip().lower().encode()
                    ).hexdigest(),
                    data["full_name"],
                    data.get("email"),
                    not data.get("is_active", True),
                ),
            )
            if changed:
                audience = await rows(
                    connection, SELECT_PROJECT_USERS_SQL, (account["project_id"],)
                )
                await self.publish(
                    connection,
                    account,
                    "user.updated",
                    identifier,
                    [changed[0]] * len(audience),
                    recipients=[row["user_uuid"] for row in audience],
                )
        if not found:
            await self.save(
                connection,
                "m_external_accounts_v2",
                {
                    "uuid": uuid.uuid5(
                        account["project_id"], f"{account['provider_uuid']}:{remote}"
                    ),
                    "project_id": account["project_id"],
                    "owner_user_uuid": identifier,
                    "provider": "zulip",
                    "provider_uuid": account["provider_uuid"],
                    "external_account_id": remote,
                    "status": "active",
                    "settings": {
                        "server_url": account["settings"]["server_url"],
                        "email": data.get("email", ""),
                    },
                },
            )
        await connection.execute(REGISTER_USER_SQL, (account["project_id"], identifier))
        return identifier

    async def user(
        self, connection: typing.Any, account: dict, remote: object
    ) -> object:
        found = await rows(
            connection,
            SELECT_USER_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                str(remote),
            ),
        )
        if not found:
            raise outgoing_operations.Pending(f"Waiting for Zulip user {remote}")
        return found[0]["owner_user_uuid"]

    async def bind(
        self,
        connection: typing.Any,
        account: dict,
        stream: dict,
        user: object,
        remote: object,
        *,
        active: bool = True,
        mode: str | None = None,
    ) -> None:
        old = await rows(
            connection,
            SELECT_BINDING_SQL,
            (
                account["project_id"],
                stream["uuid"],
                user,
            ),
        )
        identifier = old[0]["uuid"] if old else uuid.uuid5(stream["uuid"], str(user))
        values = {
            "uuid": identifier,
            "project_id": account["project_id"],
            "stream_uuid": stream["uuid"],
            "user_uuid": user,
            "who_uuid": old[0]["who_uuid"] if old else stream["owner_uuid"],
            "active": active,
            **origin(
                account,
                f"{stream['provider_identity']}:{remote}",
            ),
        }
        if mode is not None:
            values["notification_mode"] = mode
        changed = await self.save(connection, TABLES["stream_binding"], values)
        if changed:
            members = await rows(
                connection,
                SELECT_MEMBERS_SQL,
                (
                    account["project_id"],
                    stream["uuid"],
                ),
            )
            recipients = sorted({row["user_uuid"] for row in members} | {user}, key=str)
            if not active:
                await self.publish(
                    connection,
                    account,
                    "stream_binding.deleted",
                    identifier,
                    [changed] * len(recipients),
                    stream["uuid"],
                    recipients=recipients,
                    control_effect=True,
                )
                await self.publish(
                    connection,
                    account,
                    "stream.deleted",
                    stream["uuid"],
                    [stream],
                    stream["uuid"],
                    recipients=[user],
                    control_effect=True,
                )
            elif old:
                await self.publish(
                    connection,
                    account,
                    "stream_binding.updated",
                    identifier,
                    [changed] * len(recipients),
                    stream["uuid"],
                    recipients=recipients,
                )
            else:
                await self.publish(
                    connection,
                    account,
                    "stream_bindings.created",
                    stream["uuid"],
                    [{"uuid": stream["uuid"], "items": [changed]}] * len(recipients),
                    stream["uuid"],
                    recipients=recipients,
                )
            if active:
                await self.snapshot(
                    connection,
                    account,
                    "stream",
                    stream["uuid"],
                    "created" if not old or not old[0]["active"] else "updated",
                    stream["uuid"],
                )
        topics = await rows(
            connection,
            SELECT_STREAM_TOPICS_SQL,
            (account["project_id"], stream["uuid"]),
        )
        for topic in topics:
            if active and changed and topic["uuid"] == stream["default_topic_uuid"]:
                identity = stream["provider_identity"]
                await self.topic_binding(
                    connection, account, topic, user, remote, f"{identity}:"
                )

    async def topic_binding(
        self,
        connection: typing.Any,
        account: dict,
        topic: dict,
        user: object,
        remote: object,
        identity: str,
        mode: str | None = None,
    ) -> None:
        old = await rows(
            connection,
            SELECT_TOPIC_BINDING_SQL,
            (
                account["project_id"],
                user,
                topic["uuid"],
            ),
        )
        values = {
            "uuid": old[0]["uuid"] if old else uuid.uuid5(topic["uuid"], str(user)),
            "project_id": account["project_id"],
            "topic_uuid": topic["uuid"],
            "user_uuid": user,
            **origin(account, f"{identity}:{remote}"),
        }
        if mode is not None:
            values["notification_mode"] = mode
        changed = await self.save(connection, TABLES["topic_binding"], values)
        if changed:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["topic"]),
                (
                    account["project_id"],
                    topic["uuid"],
                ),
            )
            await self.publish(
                connection,
                account,
                "topic.updated" if old else "topic.created",
                topic["uuid"],
                [item for item in snapshots if item["user_uuid"] == user],
                topic["stream_uuid"],
            )

    async def topic(
        self,
        connection: typing.Any,
        account: dict,
        stream: dict,
        name: str,
    ) -> dict:
        stream_identity = stream["provider_identity"]
        identity = f"{stream_identity}:{name}"
        old = await rows(
            connection,
            SELECT_TOPIC_SQL,
            (
                account["project_id"],
                stream["uuid"],
                identity,
                name.removeprefix("✔ ") or "General",
                name.startswith("✔ "),
            ),
        )
        identifier = old[0]["uuid"] if old else uuid.uuid5(stream["uuid"], name.lower())
        changed = await self.save(
            connection,
            TABLES["topic"],
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "stream_uuid": stream["uuid"],
                "name": name.removeprefix("✔ ") or "General",
                "is_done": name.startswith("✔ "),
                "deleted_at": None,
                **origin(account, identity),
            },
        )
        topic = changed or old[0]
        binding_cursor = await connection.execute(
            ENSURE_TOPIC_BINDINGS_SQL,
            (
                topic["uuid"],
                topic["uuid"],
                identity,
                account["provider_uuid"],
                account["project_id"],
                stream["uuid"],
                [account["owner_user_uuid"]],
                topic["uuid"],
                identity,
            ),
        )
        new_binding = await binding_cursor.fetchall()
        if changed:
            await self.snapshot(
                connection,
                account,
                "topic",
                identifier,
                "created" if not old else "updated",
                stream["uuid"],
            )
        elif new_binding:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["topic"]),
                (
                    account["project_id"],
                    identifier,
                ),
            )
            await self.publish(
                connection,
                account,
                "topic.created",
                identifier,
                [
                    item
                    for item in snapshots
                    if item["user_uuid"] == account["owner_user_uuid"]
                ],
                stream["uuid"],
            )
        return topic

    async def stream(
        self,
        connection: typing.Any,
        account: dict,
        data: dict,
        *,
        subscribe: bool = True,
    ) -> dict:
        remote = str(data["stream_id"])
        identifier = await self.identity(
            account, "stream", remote, connection=connection
        )
        old = await rows(
            connection,
            SELECT_ROW_SQL.format(table=TABLES["stream"]),
            (
                account["project_id"],
                identifier,
            ),
        )
        values = {
            "uuid": identifier,
            "project_id": account["project_id"],
            "name": data["name"],
            "owner_uuid": old[0]["owner_uuid"] if old else account["owner_user_uuid"],
            **origin(account, remote),
        }
        for field in ("description", "invite_only", "is_archived"):
            if field in data:
                values[field] = data[field]
        changed = await self.save(connection, TABLES["stream"], values)
        stream = changed or old[0]
        if subscribe:
            await self.bind(
                connection,
                account,
                stream,
                account["owner_user_uuid"],
                account["external_account_id"],
                mode="mentions_only"
                if data.get("is_muted", False)
                else "all_messages"
                if "is_muted" in data
                else None,
            )
        if not stream["default_topic_uuid"]:
            topic = await self.topic(connection, account, stream, "")
            await self.save(
                connection,
                TABLES["stream"],
                {
                    "uuid": identifier,
                    "project_id": account["project_id"],
                    "default_topic_uuid": topic["uuid"],
                },
            )
            stream["default_topic_uuid"] = topic["uuid"]
        if changed:
            await self.snapshot(
                connection,
                account,
                "stream",
                identifier,
                "created" if not old else "updated",
                identifier,
            )
        return stream

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

    async def delete_message(
        self, connection: typing.Any, account: dict, remote: object
    ) -> None:
        identifier = await self.identity(
            account, "message", remote, connection=connection
        )
        old = await rows(
            connection,
            SELECT_ROW_SQL.format(table=TABLES["message"]),
            (
                account["project_id"],
                identifier,
            ),
        )
        if not old or old[0]["deleted_at"] is not None:
            return
        placements = await rows(
            connection, SELECT_PLACEMENTS_SQL, (account["project_id"], identifier)
        )
        for placement in placements:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["message"]),
                (
                    account["project_id"],
                    placement["uuid"],
                ),
            )
            await self.publish(
                connection,
                account,
                "message.deleted",
                placement["uuid"],
                snapshots,
                placement["stream_uuid"],
            )
        await self.save(
            connection,
            TABLES["message"],
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "deleted_at": datetime.datetime.now(datetime.timezone.utc),
            },
        )
        for placement in placements:
            await self.counters(connection, account, placement)
            await self.empty_topic(connection, account, placement["topic_uuid"])

    async def empty_topic(
        self, connection: typing.Any, account: dict, identifier: object
    ) -> None:
        topics = await rows(
            connection, SELECT_EMPTY_TOPIC_SQL, (account["project_id"], identifier)
        )
        if topics:
            topic = topics[0]
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["topic"]),
                (
                    account["project_id"],
                    identifier,
                ),
            )
            await self.publish(
                connection,
                account,
                "topic.deleted",
                identifier,
                snapshots,
                topic["stream_uuid"],
            )
            await self.save(
                connection,
                TABLES["topic"],
                {
                    "uuid": identifier,
                    "project_id": account["project_id"],
                    "deleted_at": datetime.datetime.now(datetime.timezone.utc),
                },
            )

    async def message(
        self,
        connection: typing.Any,
        account: dict,
        data: dict,
        prepared: dict,
    ) -> None:
        event = prepared["event"]
        deliveries = await rows(
            connection,
            SELECT_DELIVERY_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                data["id"],
                event.get("local_message_id"),
            ),
        )
        identifier = (
            deliveries[0]["message_uuid"]
            if deliveries
            else await self.identity(
                account, "message", data["id"], connection=connection
            )
        )
        old = await rows(
            connection,
            SELECT_ROW_SQL.format(table="messenger_messages_with_content_v1"),
            (account["project_id"], identifier),
        )
        if deliveries and not old:
            # A pending create/delete may finish after its root was removed.
            await connection.execute(
                OBSERVE_DELIVERY_SQL,
                (
                    data["id"],
                    datetime.datetime.now(datetime.timezone.utc),
                    deliveries[0]["uuid"],
                ),
            )
            return
        if old and old[0]["deleted_at"] is not None:
            return
        if not old and not deliveries:
            pending = await rows(
                connection,
                SELECT_PENDING_SEND_SQL,
                (
                    account["project_id"],
                    str(account["provider_uuid"]),
                    str(data["sender_id"]),
                ),
            )
            if pending:
                raise outgoing_operations.Pending(
                    "Waiting for the outgoing send receipt"
                )
        author = await self.user(connection, account, data["sender_id"])
        remote = stream_identity(account, data)
        stream = await self.dependency(
            account, {"kind": "stream", "identity": remote}, connection
        )
        stream_uuid = stream["uuid"]
        subject = data["subject"] if data["type"] == "stream" else ""
        topic = await self.dependency(
            account,
            {
                "kind": "topic",
                "identity": f"{remote}:{subject}",
                "stream_identity": remote,
                "subject": subject,
            },
            connection,
        )
        audience = await rows(
            connection,
            SELECT_MESSAGE_AUDIENCE_SQL,
            (
                account["project_id"],
                stream_uuid,
                account["owner_user_uuid"] if old or event.get("history") else None,
                account["owner_user_uuid"] if old or event.get("history") else None,
                prepared.get("received_at"),
                account["owner_user_uuid"],
            ),
        )
        recipient_users = [member["user_uuid"] for member in audience]
        recipient_generations = [member["membership_generation"] for member in audience]
        current_placements = await rows(
            connection, SELECT_PLACEMENTS_SQL, (account["project_id"], identifier)
        )
        public_uuid = next(
            (
                row["uuid"]
                for row in current_placements
                if row["stream_uuid"] == stream_uuid
            ),
            uuid.uuid5(identifier, str(stream_uuid)),
        )
        context = conversion_context(
            account, data, stream, topic, public_uuid, prepared
        )
        for target, path in data.get("_files", []):
            file = prepared["files"][(stream_uuid, path)]
            if "missing" in file:
                reason = {
                    404: "deleted_files",
                    "path_too_long": "oversized_file_paths",
                }.get(file["missing"], "inaccessible_files")
                context.setdefault(reason, set()).add(target)
                continue
            values = file.get("existing") or {
                **file,
                "project_id": account["project_id"],
                "user_uuid": author,
                "stream_uuid": stream_uuid,
                **origin(account, path),
            }
            changed = await self.save(connection, TABLES["file"], values)
            linked = await rows(
                connection,
                SELECT_ENTITY_LINK_SQL,
                (
                    account["project_id"],
                    "file",
                    values["uuid"],
                    account["provider_uuid"],
                    account["uuid"],
                ),
            )
            await connection.execute(
                SAVE_RECIPIENT_ATTACHMENT_LINKS_SQL,
                (
                    values["uuid"],
                    path,
                    stream_uuid,
                    recipient_users,
                    recipient_generations,
                    account["project_id"],
                    account["provider_uuid"],
                ),
            )
            if changed or not linked:
                snapshots = await rows(
                    connection, SELECT_FILE_RECIPIENTS_SQL, (values["uuid"],)
                )
                if not changed:
                    snapshots = [
                        row
                        for row in snapshots
                        if row["viewer_user_uuid"] == account["owner_user_uuid"]
                    ]
                await self.publish(
                    connection,
                    account,
                    "file.created",
                    values["uuid"],
                    snapshots,
                    stream_uuid,
                    recipients=[row["viewer_user_uuid"] for row in snapshots],
                )
            context["files"][target] = message_conversion.file_urn(
                values["uuid"], values["content_type"]
            )
        for remote, person in prepared["people"].items():
            user = str(await self.user(connection, account, remote))
            context["mention_uuids"][f"id:{remote}"] = user
            context["mention_uuids"][f"name:{person['full_name']}"] = user
        content_snapshots = await rows(
            connection,
            SELECT_CONTENT_SNAPSHOT_SQL,
            (account["project_id"], identifier, data["content"]),
        )
        cache_key = content_cache.key(
            account["provider_uuid"],
            "incoming",
            data["content"],
            content_cache.incoming_context(data, context),
        )
        # Exact outgoing snapshots retain the author's original spelling. They
        # take precedence over generic conversions, including after a restart.
        content = content_snapshots
        if not content and str(data["id"]) not in data["content"]:
            content = await rows(
                connection,
                content_cache.LOOKUP_SQL.format(source="provider_content"),
                (*cache_key, data["content"]),
            )
        if content:
            payload = content[0]["workspace_content"]
        else:
            payload, reusable = message_conversion.zulip_to_workspace_cached(
                data, context
            )
            content = await rows(
                connection,
                content_cache.RESOLVE_SQL,
                (pg_json.Jsonb(payload), data["content"]),
            )
            if reusable:
                await connection.execute(
                    content_cache.SAVE_SQL, (*cache_key, content[0]["uuid"])
                )
        created = datetime.datetime.fromtimestamp(
            data["timestamp"], datetime.timezone.utc
        ).replace(tzinfo=None)
        edited = datetime.datetime.fromtimestamp(
            data.get("last_edit_timestamp") or data["timestamp"], datetime.timezone.utc
        ).replace(tzinfo=None)
        if old:
            await connection.execute(
                outgoing_operations.UPDATE_ENTITY_IDENTITY_SQL.format(
                    table=TABLES["message"]
                ),
                (
                    str(data["id"]),
                    account["project_id"],
                    identifier,
                    account["provider_uuid"],
                    str(data["id"]),
                ),
            )
        changed = None
        if not old or edited >= old[0]["content_updated_at"].replace(tzinfo=None):
            changed = await self.save(
                connection,
                TABLES["message"],
                {
                    "uuid": identifier,
                    "project_id": account["project_id"],
                    "author_uuid": author,
                    "content_uuid": content[0]["uuid"],
                    "created_at": created,
                    "content_updated_at": edited,
                    **origin(account, str(data["id"])),
                },
            )
        if deliveries:
            await connection.execute(
                OBSERVE_DELIVERY_SQL, (data["id"], edited, deliveries[0]["uuid"])
            )
        previous_placements = await rows(
            connection,
            SELECT_PREVIOUS_PLACEMENTS_SQL,
            (
                account["project_id"],
                identifier,
                stream_uuid,
                account["provider_uuid"],
            ),
        )
        for previous in previous_placements:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["message"]),
                (account["project_id"], previous["uuid"]),
            )
            await self.publish(
                connection,
                account,
                "message.deleted",
                previous["uuid"],
                snapshots,
                previous["stream_uuid"],
            )
            await connection.execute(
                DELETE_ROW_SQL.format(table="messenger_message_placements"),
                (account["project_id"], previous["uuid"]),
            )
            await self.counters(connection, account, previous)
            await self.empty_topic(connection, account, previous["topic_uuid"])
        placements = await rows(
            connection, SELECT_PLACEMENTS_SQL, (account["project_id"], identifier)
        )
        placement = next(
            (row for row in placements if row["stream_uuid"] == stream_uuid), None
        )
        placement_values = {
            "uuid": placement["uuid"]
            if placement
            else uuid.uuid5(identifier, str(stream_uuid)),
            "project_id": account["project_id"],
            "message_uuid": identifier,
            "stream_uuid": stream_uuid,
            "topic_uuid": topic["uuid"],
            "created_at": created,
        }
        previous_placement = placement
        moved = placement and placement["topic_uuid"] != topic["uuid"]
        placement_changed = await self.save(
            connection, "messenger_message_placements", placement_values
        )
        placement = typing.cast(dict, placement_changed or placement)
        access_cursor = await connection.execute(
            ENSURE_MESSAGE_BINDINGS_SQL,
            (
                recipient_users,
                recipient_generations,
                account["project_id"],
                stream_uuid,
                placement["uuid"],
                placement["uuid"],
            ),
        )
        new_access = await access_cursor.fetchall()
        topic_cursor = await connection.execute(
            INSERT_MISSING_TOPIC_BINDINGS_SQL,
            (
                topic["uuid"],
                topic["uuid"],
                topic["provider_identity"],
                account["provider_uuid"],
                account["project_id"],
                stream_uuid,
                [item["user_uuid"] for item in new_access],
                topic["uuid"],
            ),
        )
        new_topics = {item["user_uuid"] for item in await topic_cursor.fetchall()}
        if new_topics:
            topic_snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["topic"]),
                (account["project_id"], topic["uuid"]),
            )
            await self.publish(
                connection,
                account,
                "topic.created",
                topic["uuid"],
                [item for item in topic_snapshots if item["user_uuid"] in new_topics],
                stream_uuid,
            )
        await connection.execute(
            ENSURE_MESSAGE_MENTIONS_SQL,
            (
                (
                    old[0]["payload"]
                    if old
                    and edited < old[0]["content_updated_at"].replace(tzinfo=None)
                    else payload
                )["content"].lower(),
                account["project_id"],
                placement["uuid"],
                recipient_users,
            ),
        )
        if "flags" in data:
            await self.flags(
                connection,
                account,
                [data["id"]],
                data["flags"],
                snapshot=True,
                created_placements=[placement["uuid"]]
                if any(
                    row["user_uuid"] == account["owner_user_uuid"] for row in new_access
                )
                else [],
            )
        if event.get("history") and "reactions" in data:
            await self.reaction_snapshot(connection, account, data)
        for reaction in data.get("reactions", []):
            await self.reaction(
                connection, account, {**reaction, "message_id": data["id"], "op": "add"}
            )
        if changed or placement_changed or new_access:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["message"]),
                (
                    account["project_id"],
                    placement["uuid"],
                ),
            )
            fresh = {row["user_uuid"] for row in new_access}
            if new_access:
                await self.publish(
                    connection,
                    account,
                    "message.created",
                    placement["uuid"],
                    [item for item in snapshots if item["user_uuid"] in fresh],
                    stream_uuid,
                )
            if old and (changed or placement_changed):
                await self.publish(
                    connection,
                    account,
                    "message.updated",
                    placement["uuid"],
                    [item for item in snapshots if item["user_uuid"] not in fresh],
                    stream_uuid,
                )
            await self.counters(connection, account, placement)
            if moved:
                await self.counters(
                    connection, account, typing.cast(dict, previous_placement)
                )
                await self.empty_topic(
                    connection,
                    account,
                    typing.cast(dict, previous_placement)["topic_uuid"],
                )

    async def flags(
        self,
        connection: typing.Any,
        account: dict,
        identifiers: list | None,
        flags: list[str],
        *,
        snapshot: bool = False,
        present: bool = True,
        created_placements: typing.Collection[uuid.UUID] = (),
    ) -> None:
        if identifiers == []:
            return
        messages = (
            [str(remote) for remote in identifiers] if identifiers is not None else None
        )
        states = await rows(
            connection,
            SELECT_STATES_SQL,
            (
                account["project_id"],
                account["provider_uuid"],
                messages,
                messages,
                account["owner_user_uuid"],
            ),
        )
        if not states:
            return
        read_set, star_set = snapshot or "read" in flags, snapshot or "starred" in flags
        read_value = "read" in flags if snapshot else present
        star_value = "starred" in flags if snapshot else present
        saved = await rows(
            connection,
            UPDATE_FLAGS_SQL,
            (
                read_set,
                read_value,
                star_set,
                star_value,
                account["project_id"],
                account["owner_user_uuid"],
                [state["uuid"] for state in states],
            ),
        )
        if not saved:
            return
        changed = {state["uuid"]: state for state in saved}
        states = [state for state in states if state["uuid"] in changed]
        snapshots = await rows(
            connection,
            SELECT_FLAG_SNAPSHOTS_SQL,
            (
                account["project_id"],
                account["owner_user_uuid"],
                [state["placement_uuid"] for state in states],
            ),
        )
        visible = {item["uuid"]: item for item in snapshots}
        if not snapshot and flags == ["read"] and present and len(states) > 1:
            streams: dict[uuid.UUID, list] = {}
            for state in states:
                if state["placement_uuid"] in visible:
                    streams.setdefault(state["stream_uuid"], []).append(
                        state["placement_uuid"]
                    )
            for stream, placement_ids in streams.items():
                for offset in range(0, len(placement_ids), 1000):
                    await self.publish(
                        connection,
                        account,
                        "messages.read",
                        placement_ids[offset],
                        [
                            {
                                "user_uuid": account["owner_user_uuid"],
                                "project_id": account["project_id"],
                                "message_uuids": placement_ids[offset : offset + 1000],
                            }
                        ],
                        stream,
                    )
        else:
            for state in states:
                item = visible.get(state["placement_uuid"])
                if (
                    item is not None
                    and state["placement_uuid"] not in created_placements
                ):
                    kind = (
                        "message.read"
                        if read_set and state["read"] != read_value
                        else "message.updated"
                    )
                    await self.publish(
                        connection,
                        account,
                        kind,
                        state["placement_uuid"],
                        [item],
                        state["stream_uuid"],
                    )
        topics = {state["topic_uuid"]: state for state in states}
        for state in topics.values():
            await connection.execute(
                ENQUEUE_COUNTERS_SQL,
                (
                    account["project_id"],
                    account["owner_user_uuid"],
                    state["stream_uuid"],
                    state["topic_uuid"],
                ),
            )

    async def reaction_snapshot(
        self, connection: typing.Any, account: dict, data: dict
    ) -> None:
        identifier = await self.identity(
            account, "message", data["id"], connection=connection
        )
        existing = await rows(
            connection,
            SELECT_REACTION_SNAPSHOT_SQL,
            (account["project_id"], identifier, account["provider_uuid"]),
        )
        identities = {
            f"{data['id']}:{item['user_id']}:{item['reaction_type']}:{item['emoji_code']}"
            for item in data["reactions"]
        }
        for item in existing:
            if item["provider_identity"] not in identities:
                await self.reaction(
                    connection,
                    account,
                    {
                        "message_id": data["id"],
                        "op": "remove",
                    },
                    user_uuid=item["user_uuid"],
                    emoji_name=item["emoji_name"],
                )

    async def reaction(
        self,
        connection: typing.Any,
        account: dict,
        event: dict,
        *,
        user_uuid: object | None = None,
        emoji_name: str | None = None,
    ) -> None:
        identifier = await self.identity(
            account, "message", event["message_id"], connection=connection
        )
        placements = await rows(
            connection, SELECT_PLACEMENTS_SQL, (account["project_id"], identifier)
        )
        if not placements:
            return
        user = (
            user_uuid
            if user_uuid is not None
            else await self.user(connection, account, event["user_id"])
        )
        name = (
            emoji_name
            if emoji_name is not None
            else emoji.name_for_code(event["emoji_code"], event["emoji_name"])
            if event["reaction_type"] == "unicode_emoji"
            else event["emoji_name"]
        )
        roots = await rows(
            connection,
            SELECT_ROW_SQL.format(table=TABLES["message"]),
            (
                account["project_id"],
                identifier,
            ),
        )
        if not roots or roots[0]["deleted_at"] is not None:
            return
        pending = await rows(
            connection,
            SELECT_PENDING_PERSONAL_SQL,
            (
                account["project_id"],
                "reaction",
                str(identifier),
                str(user),
                name,
            ),
        )
        if pending:
            return
        existing = await rows(
            connection,
            SELECT_REACTION_SQL,
            (account["project_id"], identifier, user, name),
        )
        if event["op"] == "add":
            remote = f"{event['message_id']}:{event['user_id']}:{event['reaction_type']}:{event['emoji_code']}"
            fact_uuid = (
                existing[0]["uuid"] if existing else uuid.uuid5(identifier, remote)
            )
            changed = await self.save(
                connection,
                TABLES["reaction"],
                {
                    "uuid": fact_uuid,
                    "project_id": account["project_id"],
                    "canonical_message_uuid": identifier,
                    "placement_uuid": placements[0]["uuid"],
                    "user_uuid": user,
                    "emoji_name": name,
                    **origin(account, remote),
                },
            )
            action = "created"
            await connection.execute(
                SAVE_REACTION_LINK_SQL,
                (
                    account["project_id"],
                    fact_uuid,
                    account["provider_uuid"],
                    account["uuid"],
                    remote,
                    json_value(
                        {
                            key: event[key]
                            for key in (
                                "emoji_name",
                                "emoji_code",
                                "reaction_type",
                            )
                        }
                    ),
                ),
            )
        else:
            if not existing:
                return
            fact_uuid = existing[0]["uuid"]
            deleted = await rows(
                connection,
                DELETE_ROW_SQL.format(table=TABLES["reaction"]),
                (account["project_id"], fact_uuid),
            )
            changed = deleted[0] if deleted else None
            action = "deleted"
        if not changed:
            return
        counts = await rows(
            connection, SELECT_REACTION_COUNTS_SQL, (account["project_id"], identifier)
        )
        reactions = {row["emoji_name"]: row["count"] for row in counts}
        reaction_users = {
            row["emoji_name"]: row["users"]
            for row in counts
            if 0 < row["count"] <= self.reaction_user_limit
        }
        await self.save(
            connection,
            TABLES["message"],
            {
                "uuid": identifier,
                "project_id": account["project_id"],
                "reactions": reactions,
                "reaction_users": reaction_users,
            },
        )
        for placement in placements:
            viewers = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["message"]),
                (
                    account["project_id"],
                    placement["uuid"],
                ),
            )
            recipients = [item["user_uuid"] for item in viewers]
            snapshot = {
                "uuid": fact_uuid,
                "project_id": account["project_id"],
                "message_uuid": placement["uuid"],
                "user_uuid": user,
                "emoji_name": name,
                "source_name": changed["source_name"],
                "provider_uuid": changed["provider_uuid"],
                "provider_identity": changed["provider_identity"],
                "created_at": changed["created_at"],
                "updated_at": changed["updated_at"],
            }
            await self.publish(
                connection,
                account,
                f"message_reaction.{action}",
                fact_uuid,
                [snapshot] * len(recipients),
                placement["stream_uuid"],
                recipients=recipients,
            )
            await self.snapshot(
                connection,
                account,
                "message",
                placement["uuid"],
                "updated",
                placement["stream_uuid"],
            )

    async def personal(
        self, connection: typing.Any, account: dict, event: dict
    ) -> None:
        kind = event["type"]
        if kind not in ("message", "update_message"):
            return
        data = event["message"] if kind == "message" else event
        identifier = data["id"] if kind == "message" else event["message_id"]
        canonical = await rows(
            connection,
            "SELECT * FROM messenger_messages_with_content_v1 WHERE project_id=%s AND provider_uuid=%s AND source_name='zulip' AND provider_identity=%s",
            (account["project_id"], account["provider_uuid"], str(identifier)),
        )
        if not canonical:
            raise MissingSharedMessage()
        message = canonical[0]
        if message["deleted_at"] is not None:
            return
        placements = await rows(
            connection, SELECT_PLACEMENTS_SQL, (account["project_id"], message["uuid"])
        )
        if not placements:
            return
        placement = placements[0]
        stream = (
            await rows(
                connection,
                SELECT_ROW_SQL.format(table=TABLES["stream"]),
                (account["project_id"], placement["stream_uuid"]),
            )
        )[0]
        if kind == "message" and stream["provider_identity"] != stream_identity(
            account, data
        ):
            return
        if (
            kind == "update_message"
            and "stream_id" in event
            and str(event["stream_id"]) != stream["provider_identity"]
        ):
            return
        context = {"stream_identity": stream["provider_identity"]}
        if kind == "message" and data["type"] != "stream":
            context["recipients"] = data["display_recipient"]
        membership = await self.dependency(
            account,
            {
                "kind": "stream_binding",
                "identity": stream["provider_identity"],
                "user": account["external_account_id"],
                **context,
            },
            connection,
        )
        topic = (
            await rows(
                connection,
                SELECT_ROW_SQL.format(table=TABLES["topic"]),
                (account["project_id"], placement["topic_uuid"]),
            )
        )[0]
        subject = topic["provider_identity"][len(stream["provider_identity"]) + 1 :]
        await self.dependency(
            account,
            {
                "kind": "topic_binding",
                "identity": topic["provider_identity"],
                "subject": subject,
                "user": account["external_account_id"],
                **context,
            },
            connection,
        )
        for target in message_conversion.get_zulip_files(data, {}):
            endpoint = message_conversion.parse_url(
                target, account["settings"]["server_url"]
            )
            if (
                endpoint is None
                or endpoint.hostname != account["hostname"]
                or not endpoint.path.startswith("/user_uploads/")
            ):
                continue
            path = message_conversion.zulip_upload_path(endpoint.path)
            if path is None or not base.FileProviderIdentity().validate(path):
                continue
            files = await rows(
                connection,
                SELECT_SCOPED_FILE_SQL,
                (
                    account["project_id"],
                    stream["uuid"],
                    account["provider_uuid"],
                    path,
                ),
            )
            if not files and path != endpoint.path:
                files = await rows(
                    connection,
                    SELECT_SCOPED_FILE_SQL,
                    (
                        account["project_id"],
                        stream["uuid"],
                        account["provider_uuid"],
                        endpoint.path,
                    ),
                )
            if not files:
                continue
            file = files[0]
            linked = await rows(
                connection,
                SELECT_ENTITY_LINK_SQL,
                (
                    account["project_id"],
                    "file",
                    file["uuid"],
                    account["provider_uuid"],
                    account["uuid"],
                ),
            )
            if not linked:
                await connection.execute(
                    SAVE_ATTACHMENT_LINK_SQL,
                    (
                        account["project_id"],
                        file["uuid"],
                        account["provider_uuid"],
                        account["uuid"],
                        file["provider_identity"],
                        json_value(
                            {"account_identity": account["external_account_id"]}
                        ),
                    ),
                )
                audience = await rows(
                    connection, SELECT_FILE_RECIPIENTS_SQL, (file["uuid"],)
                )
                audience = [
                    row
                    for row in audience
                    if row["viewer_user_uuid"] == account["owner_user_uuid"]
                ]
                await self.publish(
                    connection,
                    account,
                    "file.created",
                    file["uuid"],
                    audience,
                    stream["uuid"],
                    recipients=[row["viewer_user_uuid"] for row in audience],
                )
        cursor = await connection.execute(
            ENSURE_MESSAGE_BINDINGS_SQL,
            (
                [account["owner_user_uuid"]],
                [membership["membership_generation"]],
                account["project_id"],
                stream["uuid"],
                placement["uuid"],
                placement["uuid"],
            ),
        )
        fresh = await cursor.fetchall()
        state_cursor = await connection.execute(
            ENSURE_MESSAGE_MENTIONS_SQL,
            (
                message["payload"]["content"].lower(),
                account["project_id"],
                placement["uuid"],
                [account["owner_user_uuid"]],
            ),
        )
        changed_states = await state_cursor.fetchall()
        flags = event.get("flags", data.get("flags"))
        if flags is not None:
            await self.flags(
                connection,
                account,
                [identifier],
                flags,
                snapshot=True,
                created_placements=[placement["uuid"]] if fresh else [],
            )
        if fresh or changed_states:
            snapshots = await rows(
                connection,
                SELECT_SNAPSHOT_SQL.format(view=VIEWS["message"]),
                (account["project_id"], placement["uuid"]),
            )
            await self.publish(
                connection,
                account,
                "message.created" if fresh else "message.updated",
                placement["uuid"],
                [
                    row
                    for row in snapshots
                    if row["user_uuid"] == account["owner_user_uuid"]
                ],
                stream["uuid"],
            )
            await self.counters(
                connection, account, placement, user=account["owner_user_uuid"]
            )

    async def apply(
        self, connection: typing.Any, account: dict, prepared: dict
    ) -> None:
        event = prepared["event"]
        kind = event["type"]
        if prepared.get("personal"):
            await self.personal(connection, account, event)
            return
        if kind == "ensure_dependency":
            await self.ensure_dependency(connection, account, prepared)
            return
        await self.message_dependencies(
            account, prepared["messages"], connection, event
        )
        if kind == "account_ready":
            changed = await self.save(
                connection,
                "m_external_accounts_v2",
                {
                    "uuid": account["uuid"],
                    "project_id": account["project_id"],
                    "status": "active",
                    "error_description": None,
                },
            )
            if changed:
                await self.account_snapshot(connection, {**account, **changed})
            return
        for person in prepared["people"].values():
            await self.person(connection, account, person)
        for channel in prepared["channels"].values():
            await self.stream(
                connection,
                account,
                channel,
                subscribe=kind in ("bootstrap", "message")
                or (kind == "subscription" and event["op"] == "add"),
            )
        if (
            kind == "update_message"
            and event.get("propagate_mode") == "change_all"
            and "orig_subject" in event
            and "subject" in event
            and "new_stream_id" not in event
        ):
            stream_uuid = await self.identity(
                account, "stream", event["stream_id"], connection=connection
            )
            for subject in (event["subject"], event["orig_subject"]):
                identifier = await self.identity(
                    account,
                    "topic",
                    f"{event['stream_id']}:{subject}",
                    connection=connection,
                )
                topics = await rows(
                    connection,
                    SELECT_ROW_SQL.format(table=TABLES["topic"]),
                    (account["project_id"], identifier),
                )
                if topics:
                    break
            if topics:
                topic = topics[0]
                identity = f"{event['stream_id']}:{event['subject']}"
                changed = await self.save(
                    connection,
                    TABLES["topic"],
                    {
                        "uuid": topic["uuid"],
                        "project_id": account["project_id"],
                        "name": event["subject"].removeprefix("✔ ") or "General",
                        "is_done": event["subject"].startswith("✔ "),
                        "provider_identity": identity,
                    },
                )
                if changed:
                    await self.snapshot(
                        connection,
                        account,
                        "topic",
                        topic["uuid"],
                        "updated",
                        stream_uuid,
                    )
        for message in prepared["messages"]:
            await self.message(connection, account, message, prepared)
        for identifier in prepared.get("deleted", []):
            await self.delete_message(connection, account, identifier)
        if kind == "delete_message":
            for identifier in event.get("message_ids", [event.get("message_id")]):
                await self.delete_message(connection, account, identifier)
        elif kind == "reaction":
            await self.reaction(connection, account, event)
        elif kind == "update_message_flags" and event["flag"] in ("read", "starred"):
            await self.flags(
                connection,
                account,
                None if event.get("all") else event["messages"],
                [event["flag"]],
                present=event["op"] == "add",
            )
        elif kind == "subscription":
            await self.subscription(connection, account, event)
        elif kind == "stream":
            for item in event.get("streams", [event]):
                identifier = await self.identity(
                    account, "stream", item["stream_id"], connection=connection
                )
                existing = await rows(
                    connection,
                    SELECT_ROW_SQL.format(table=TABLES["stream"]),
                    (account["project_id"], identifier),
                )
                if not existing:
                    continue
                stream = existing[0]
                if event["op"] == "delete":
                    await self.delete_stream(connection, account, stream)
                elif event["op"] == "update" and event["property"] in (
                    "name",
                    "description",
                    "invite_only",
                    "is_archived",
                ):
                    await self.stream(
                        connection,
                        account,
                        {
                            "stream_id": item["stream_id"],
                            "name": stream["name"],
                            event["property"]: event["value"],
                        },
                        subscribe=False,
                    )
        elif kind == "user_topic":
            identifier = await self.identity(
                account, "stream", event["stream_id"], connection=connection
            )
            topic_streams = await rows(
                connection,
                SELECT_ROW_SQL.format(table=TABLES["stream"]),
                (account["project_id"], identifier),
            )
            if topic_streams:
                topic = await self.topic(
                    connection, account, topic_streams[0], event["topic_name"]
                )
                mode = {0: "default", 1: "mute", 2: "unmute", 3: "follow"}[
                    event["visibility_policy"]
                ]
                await self.topic_binding(
                    connection,
                    account,
                    topic,
                    account["owner_user_uuid"],
                    account["external_account_id"],
                    f"{event['stream_id']}:{event['topic_name']}",
                    mode,
                )
        elif kind == "attachment":
            await self.attachment(connection, account, event)

    async def subscription(
        self, connection: typing.Any, account: dict, event: dict
    ) -> None:
        operation = event["op"]
        if operation == "add":
            return
        if operation == "update":
            changes = event.get("subscription_data", [event])
            for change in changes:
                identifier = await self.identity(
                    account, "stream", change["stream_id"], connection=connection
                )
                known = await rows(
                    connection,
                    SELECT_ROW_SQL.format(table=TABLES["stream"]),
                    (account["project_id"], identifier),
                )
                if not known:
                    continue
                if change["property"] == "is_muted":
                    await self.bind(
                        connection,
                        account,
                        known[0],
                        account["owner_user_uuid"],
                        account["external_account_id"],
                        mode="mentions_only" if change["value"] else "all_messages",
                    )
                elif change["property"] == "color":
                    changed = await self.save(
                        connection,
                        TABLES["stream"],
                        {
                            "uuid": identifier,
                            "project_id": account["project_id"],
                            "color": int(change["value"].lstrip("#"), 16),
                        },
                    )
                    if changed:
                        await self.snapshot(
                            connection,
                            account,
                            "stream",
                            identifier,
                            "updated",
                            identifier,
                        )
            return
        identifiers = event.get("stream_ids") or [
            item["stream_id"] for item in event.get("subscriptions", [])
        ]
        for remote in identifiers:
            identifier = await self.identity(
                account, "stream", remote, connection=connection
            )
            known = await rows(
                connection,
                SELECT_ROW_SQL.format(table=TABLES["stream"]),
                (account["project_id"], identifier),
            )
            if not known:
                continue
            users = (
                event["user_ids"]
                if operation in ("peer_add", "peer_remove")
                else [account["external_account_id"]]
            )
            for remote_user in users:
                user = await self.user(connection, account, remote_user)
                await self.bind(
                    connection,
                    account,
                    known[0],
                    user,
                    remote_user,
                    active=operation == "peer_add",
                )

    async def delete_stream(
        self, connection: typing.Any, account: dict, stream: dict
    ) -> None:
        if stream["deleted_at"] is not None:
            return
        members = await rows(
            connection, SELECT_MEMBERS_SQL, (account["project_id"], stream["uuid"])
        )
        await self.save(
            connection,
            TABLES["stream"],
            {
                "uuid": stream["uuid"],
                "project_id": account["project_id"],
                "deleted_at": datetime.datetime.now(datetime.timezone.utc),
            },
        )
        await self.publish(
            connection,
            account,
            "stream.deleted",
            stream["uuid"],
            [stream] * len(members),
            stream["uuid"],
            recipients=[row["user_uuid"] for row in members],
            control_effect=True,
        )

    async def attachment(
        self, connection: typing.Any, account: dict, event: dict
    ) -> None:
        data = event.get("attachment", event)
        path = data.get("path_id")
        if path is None:
            known = await rows(
                connection,
                SELECT_ATTACHMENT_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    data["id"],
                ),
            )
            path = known[0]["path"] if known else None
        else:
            path = "/user_uploads/" + path
            await connection.execute(
                SAVE_ATTACHMENT_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    data["id"],
                    path,
                    data["name"],
                    account["external_account_id"],
                ),
            )
        files = (
            await rows(
                connection,
                SELECT_FILE_SQL,
                (
                    account["project_id"],
                    account["provider_uuid"],
                    path,
                ),
            )
            if path is not None
            else []
        )
        if event["op"] == "remove":
            await connection.execute(
                DELETE_ATTACHMENT_SQL,
                (account["project_id"], account["provider_uuid"], data["id"]),
            )
        for file in files:
            if event["op"] == "remove":
                audience = await rows(
                    connection, SELECT_FILE_RECIPIENTS_SQL, (file["uuid"],)
                )
                await self.publish(
                    connection,
                    account,
                    "file.deleted",
                    file["uuid"],
                    [file] * len(audience),
                    file["stream_uuid"],
                    recipients=[row["viewer_user_uuid"] for row in audience],
                )
                await connection.execute(
                    DELETE_ROW_SQL.format(table=TABLES["file"]),
                    (account["project_id"], file["uuid"]),
                )
                await connection.execute(
                    ENQUEUE_FILE_CLEANUP_SQL,
                    (
                        account["project_id"],
                        file["uuid"],
                        f"file:{file['uuid']}",
                        account["uuid"],
                        json_value(
                            {
                                **file,
                                "provider_deleted": True,
                                "account_identity": account["external_account_id"],
                                "account_provider_uuid": account["provider_uuid"],
                            }
                        ),
                    ),
                )
            else:
                changed = await self.save(
                    connection,
                    TABLES["file"],
                    {
                        "uuid": file["uuid"],
                        "project_id": account["project_id"],
                        "name": data["name"],
                    },
                )
                await connection.execute(
                    SAVE_ATTACHMENT_LINK_SQL,
                    (
                        account["project_id"],
                        file["uuid"],
                        account["provider_uuid"],
                        account["uuid"],
                        file["provider_identity"],
                        json_value(
                            {
                                "attachment_id": str(data["id"]),
                                "account_identity": account["external_account_id"],
                            }
                        ),
                    ),
                )
                if changed:
                    audience = await rows(
                        connection, SELECT_FILE_RECIPIENTS_SQL, (file["uuid"],)
                    )
                    await self.publish(
                        connection,
                        account,
                        "file.updated",
                        file["uuid"],
                        audience,
                        file["stream_uuid"],
                        recipients=[row["viewer_user_uuid"] for row in audience],
                    )
