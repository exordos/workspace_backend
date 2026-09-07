#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Set-based final writes. No storage, conversion or provider calls belong here."""

import typing

import json

from workspace.history_import import preparation
from workspace.history_import import repository
from workspace.messenger_api.dm import read_state


class PreparationChanged(Exception):
    pass


def configure_transaction(
    session: typing.Any, *, defer_wal_flush: bool = False
) -> None:
    session.execute("SET LOCAL jit = off", ())
    session.execute("SET LOCAL lock_timeout = '50ms'", ())
    session.execute("SET LOCAL statement_timeout = '500ms'", ())
    if defer_wal_flush:
        # A committed import part and its checkpoint are idempotent and may be
        # replayed after a database crash. Avoid making foreground API commits
        # queue behind a WAL flush for every small background part.
        session.execute("SET LOCAL synchronous_commit = off", ())


def _lock_users(session: typing.Any, identities: dict) -> None:
    session.execute(
        """SELECT pg_advisory_xact_lock_shared(hashtextextended(
            'workspace-user-resource-v1:' || user_uuid, 0))
            FROM unnest(%s::text[]) AS users(user_uuid) ORDER BY user_uuid""",
        (sorted(set(identities.values())),),
    )


def _verify_identities(session: typing.Any, job: dict, identities: dict) -> None:
    rows = session.execute(
        """SELECT provider_user_id, workspace_user_uuid
           FROM m_external_provider_identity_links_v1
           WHERE provider = 'zulip' AND provider_realm_uuid = %s
             AND provider_user_id = ANY(%s::text[])""",
        (job["provider_realm_uuid"], [str(value) for value in identities]),
    ).fetchall()
    if any(
        identities[int(row["provider_user_id"])] != str(row["workspace_user_uuid"])
        for row in rows
    ):
        raise PreparationChanged()


def write_directory(
    session: typing.Any,
    identity: typing.Any,
    job: dict,
    records: list[dict],
    encoded: str,
) -> None:
    configure_transaction(session)
    repository.guard(session, identity, job, lease=True)
    identities = {int(record["id"]): record["uuid"] for record in records}
    _lock_users(session, identities)
    _verify_identities(session, job, identities)
    # A historical directory initializes missing profiles; realtime profiles remain authoritative.
    session.execute(
        """INSERT INTO m_workspace_users (
            uuid, username, source, status, first_name, avatar,
            provider_uuid, external_account_uuid, provider_external_id,
            created_at, updated_at, last_ping_at
        ) SELECT data.uuid, 'zulip-' || data.uuid::text, 'zulip', 'offline', data.name,
                 data.avatar, %s, %s, data.id, now(), now(), now()
          FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid, id text, name text, avatar text)
        ON CONFLICT (uuid) DO NOTHING""",
        (identity.bridge_instance_uuid, job["sources"][0]["account_uuid"], encoded),
    )
    session.execute(
        """INSERT INTO m_external_provider_identity_links_v1 (
            provider, provider_realm_uuid, provider_user_id, workspace_user_uuid, link_kind
        ) SELECT 'zulip', %s, data.id, data.uuid, 'provider_identity'
          FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid, id text)
        ON CONFLICT (provider, provider_realm_uuid, provider_user_id) DO NOTHING""",
        (job["provider_realm_uuid"], encoded),
    )
    _verify_identities(session, job, identities)
    session.execute(
        """INSERT INTO messenger_project_users (project_id, user_uuid)
           SELECT %s, data.uuid FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid)
           ON CONFLICT DO NOTHING""",
        (job["project_uuid"], encoded),
    )
    session.execute(
        """UPDATE m_external_history_imports_v1 SET directory_cursor = directory_cursor + %s, consecutive_errors = 0,
                   lease_until = now() + interval '120 seconds', updated_at = now()
           WHERE uuid = %s AND lease_token = %s""",
        (len(records), job["uuid"], job["lease_token"]),
    )


def _lock_provider_messages(
    session: typing.Any, job: dict, source_ids: list[int]
) -> None:
    # This identity gate precedes account and project locks in Provider v2 too.
    session.execute(
        """SELECT pg_advisory_xact_lock(hashtextextended(
               'provider-message-identity-v1:' || %s || ':' || message_id, 0))
           FROM unnest(%s::text[]) AS messages(message_id) ORDER BY message_id""",
        (str(job["provider_realm_uuid"]), sorted(map(str, source_ids))),
    )


def _lock_part(
    session: typing.Any, identity: typing.Any, job: dict, part: preparation.PreparedPart
) -> None:
    _lock_provider_messages(session, job, part.source_ids)
    repository.guard(session, identity, job, lease=True)
    _lock_users(session, part.identities)
    _verify_identities(session, job, part.identities)
    session.execute(
        """SELECT pg_advisory_xact_lock(hashtextextended(
               'workspace-message-resource-v1:' || message_uuid, 0))
           FROM unnest(%s::text[]) AS messages(message_uuid) ORDER BY message_uuid""",
        (sorted(message["placement_uuid"] for message in part.messages),),
    )
    read_state.lock_message_structure(session, (job["project_uuid"],))
    read_state.lock_projects(session, (job["project_uuid"],))
    current = preparation.existing_messages(
        session,
        repository.batch_scope(job),
        [{"id": value} for value in part.source_ids],
    )
    keys = (
        "uuid",
        "project_id",
        "placement_uuid",
        "stream_uuid",
        "topic_uuid",
        "updated_at",
        "deleted_at",
    )
    for message_id, expected in part.expected_messages.items():
        actual = current.get(message_id)
        if (actual is None) != (expected is None) or (
            actual is not None and any(actual[key] != expected[key] for key in keys)
        ):
            raise PreparationChanged()


def write_missing_files(
    session: typing.Any,
    identity: typing.Any,
    job: dict,
    data: str,
    absent_message_ids: list[int],
) -> None:
    configure_transaction(session)
    _lock_provider_messages(session, job, absent_message_ids)
    repository.guard(session, identity, job, lease=True)
    # Publication uses the same identity fence as canonical writes. A live
    # arrival since preparation invalidates the whole bounded request page.
    if preparation.existing_message_ids(
        session,
        repository.batch_scope(job),
        [{"id": value} for value in absent_message_ids],
    ):
        raise PreparationChanged()
    changed_files = session.execute(
        """INSERT INTO m_external_history_files_v1 AS existing (job_uuid, uuid, source_path, account_uuids)
           SELECT %s, uuid, source_path, account_uuids
           FROM jsonb_to_recordset(%s::jsonb) AS file(uuid uuid, source_path text, account_uuids jsonb)
           ON CONFLICT (job_uuid, uuid) DO UPDATE SET
               account_uuids = (SELECT jsonb_agg(account ORDER BY account)
                   FROM (SELECT jsonb_array_elements_text(existing.account_uuids) AS account
                         UNION SELECT jsonb_array_elements_text(EXCLUDED.account_uuids)) AS accounts),
               status = CASE WHEN existing.status = 'unavailable'
                                  AND NOT existing.account_uuids @> EXCLUDED.account_uuids
                             THEN 'missing' ELSE existing.status END,
               updated_at = now()
           WHERE NOT existing.account_uuids @> EXCLUDED.account_uuids
           RETURNING uuid""",
        (job["uuid"], data),
    ).fetchall()
    session.execute(
        """WITH active AS (
               SELECT array_agg(uuid) AS uuids FROM jsonb_to_recordset(%s::jsonb) AS file(uuid uuid)
           ), pending AS (
               SELECT EXISTS (SELECT 1 FROM m_external_history_files_v1
                   WHERE job_uuid = %s AND uuid = ANY((SELECT uuids FROM active)::uuid[])
                     AND status = 'missing') AS waiting
           ) UPDATE m_external_history_imports_v1
             SET active_file_uuids = active.uuids,
                 consecutive_errors = CASE WHEN %s OR active_file_uuids IS DISTINCT FROM active.uuids
                                           THEN 0 ELSE consecutive_errors END,
                 status = CASE WHEN pending.waiting THEN 'waiting_files' ELSE 'pending' END,
                 lease_until = NULL, worker_uuid = NULL,
                 available_at = now() + CASE WHEN pending.waiting THEN interval '30 seconds' ELSE interval '0.1 seconds' END,
                 updated_at = now()
             FROM active, pending WHERE uuid = %s AND lease_token = %s""",
        (data, job["uuid"], bool(changed_files), job["uuid"], job["lease_token"]),
    )


def _write_topics(session: typing.Any, job: dict, data: str) -> None:
    session.execute(
        """WITH inserted AS (
            INSERT INTO messenger_topics (uuid, project_id, stream_uuid, name, source_name, source, provider)
            SELECT data.uuid, %s, data.stream_uuid, data.name, 'zulip', data.source, data.provider
            FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid, stream_uuid uuid, name text, source jsonb, provider jsonb)
            ON CONFLICT (project_id, uuid) DO NOTHING
            RETURNING uuid, stream_uuid, name, provider
        ), additions AS (
            SELECT chat.uuid, jsonb_agg(jsonb_build_object(
                'topic_uuid', inserted.uuid::text,
                'provider_topic_id', inserted.provider->>'external_id',
                'name', inserted.name, 'is_default', false
            ) ORDER BY inserted.uuid) AS topics
            FROM inserted JOIN m_external_chats_v2 AS chat
              ON chat.project_id = %s AND chat.projection_stream_uuid = inserted.stream_uuid
            WHERE NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(chat.source->'topics') AS topic
                WHERE topic->>'provider_topic_id' = inserted.provider->>'external_id'
            )
            GROUP BY chat.uuid
        )
        UPDATE m_external_chats_v2 AS chat SET source = jsonb_set(
            chat.source, '{topics}', (chat.source->'topics') || additions.topics
        ) FROM additions WHERE chat.uuid = additions.uuid""",
        (job["project_uuid"], data, job["project_uuid"]),
    )


def _write_messages(
    session: typing.Any, identity: typing.Any, job: dict, data: str
) -> list:
    rows = session.execute(
        """INSERT INTO messenger_messages (
            uuid, project_id, legacy_public_uuid, author_uuid, payload, source_name, source,
            provider_uuid, external_account_uuid, provider_external_id, provider_realm_uuid,
            provider_message_id, provider, reactions, reaction_users, created_at
        ) SELECT data.uuid, %s, data.placement_uuid, data.author_uuid, data.payload, 'zulip',
                 data.source, %s, data.account_uuid, data.provider_id, %s,
                 data.provider_id, data.provider, data.reactions, data.reaction_users, data.created_at
          FROM jsonb_to_recordset(%s::jsonb) AS data(
              uuid uuid, placement_uuid uuid, author_uuid uuid, payload jsonb, account_uuid uuid,
              provider_id text, source jsonb, provider jsonb, reactions jsonb, reaction_users jsonb, created_at timestamptz
          ) WHERE NOT EXISTS (
              SELECT 1 FROM m_external_history_tombstones_v1 AS tombstone
              WHERE tombstone.provider_realm_uuid = %s AND tombstone.provider_message_id = data.provider_id
          ) ON CONFLICT DO NOTHING RETURNING uuid""",
        (
            job["project_uuid"],
            identity.bridge_instance_uuid,
            job["provider_realm_uuid"],
            data,
            job["provider_realm_uuid"],
        ),
    ).fetchall()
    inserted = [row["uuid"] for row in rows]
    session.execute(
        """INSERT INTO messenger_message_placements (
            uuid, project_id, legacy_public_uuid, message_uuid, stream_uuid, topic_uuid, created_at
        ) SELECT data.placement_uuid, %s, data.placement_uuid, data.uuid, data.stream_uuid, data.topic_uuid, data.created_at
          FROM jsonb_to_recordset(%s::jsonb) AS data(
              uuid uuid, placement_uuid uuid, stream_uuid uuid, topic_uuid uuid, created_at timestamptz
          ) WHERE data.uuid = ANY(%s::uuid[]) ON CONFLICT DO NOTHING""",
        (job["project_uuid"], data, inserted),
    )
    return inserted


def _write_states(session: typing.Any, job: dict, data: str) -> list[dict]:
    project_uuid = job["project_uuid"]
    session.execute(
        """INSERT INTO messenger_user_topic_bindings (uuid, project_id, user_uuid, topic_uuid)
           SELECT DISTINCT messenger_uuid_v5(data.topic_uuid, data.user_uuid::text), %s,
                  data.user_uuid, data.topic_uuid
           FROM jsonb_to_recordset(%s::jsonb) AS data(user_uuid uuid, topic_uuid uuid, stream_uuid uuid)
           JOIN messenger_stream_bindings AS membership ON membership.project_id = %s
            AND membership.user_uuid = data.user_uuid AND membership.stream_uuid = data.stream_uuid AND membership.active
           ON CONFLICT DO NOTHING""",
        (project_uuid, data, project_uuid),
    )
    session.execute(
        """INSERT INTO messenger_user_message_bindings (
               uuid, project_id, placement_uuid, user_uuid, membership_generation, relation_role, visibility, permissions
           ) SELECT data.uuid, %s, data.placement_uuid, data.user_uuid, membership.membership_generation,
                    data.role, 'visible', '{"read":true,"react":true,"star":true,"pin":true}'::jsonb
             FROM jsonb_to_recordset(%s::jsonb) AS data(
                 uuid uuid, user_uuid uuid, placement_uuid uuid, stream_uuid uuid, role text
             ) JOIN messenger_stream_bindings AS membership ON membership.project_id = %s
                AND membership.stream_uuid = data.stream_uuid AND membership.user_uuid = data.user_uuid AND membership.active
             JOIN messenger_message_placements AS placement ON placement.project_id = %s
                AND placement.uuid = data.placement_uuid
             JOIN messenger_messages AS message ON message.project_id = placement.project_id
                AND message.uuid = placement.message_uuid AND message.deleted_at IS NULL
           ON CONFLICT (project_id, placement_uuid, user_uuid) DO UPDATE
               SET membership_generation = EXCLUDED.membership_generation,
                   visibility = EXCLUDED.visibility, updated_at = now()
               WHERE messenger_user_message_bindings.membership_generation <> EXCLUDED.membership_generation""",
        (project_uuid, data, project_uuid, project_uuid),
    )
    # Every returned state is newly initialized in this membership generation.
    # Existing read/starred values are never replaced by a historical observation.
    return session.execute(
        """WITH inserted AS (
            INSERT INTO messenger_user_message_states (
                uuid, project_id, placement_uuid, user_uuid, membership_generation, read_at, starred, mentioned
            ) SELECT data.uuid, %s, data.placement_uuid, data.user_uuid, binding.membership_generation,
                     CASE WHEN data.read THEN %s::timestamptz END, data.starred, data.mentioned
              FROM jsonb_to_recordset(%s::jsonb) AS data(
                  uuid uuid, placement_uuid uuid, user_uuid uuid, read boolean, starred boolean, mentioned boolean
              ) JOIN messenger_user_message_bindings AS binding ON binding.project_id = %s
                 AND binding.placement_uuid = data.placement_uuid AND binding.user_uuid = data.user_uuid
              JOIN messenger_stream_bindings AS membership ON membership.project_id = binding.project_id
                 AND membership.user_uuid = binding.user_uuid AND membership.active
                 AND membership.membership_generation = binding.membership_generation
              JOIN messenger_message_placements AS place ON place.project_id = binding.project_id
                 AND place.uuid = binding.placement_uuid AND place.stream_uuid = membership.stream_uuid
            ON CONFLICT (project_id, user_uuid, placement_uuid) DO UPDATE SET
                membership_generation = EXCLUDED.membership_generation,
                read_at = EXCLUDED.read_at, starred = EXCLUDED.starred, mentioned = EXCLUDED.mentioned, updated_at = now()
                WHERE messenger_user_message_states.membership_generation <> EXCLUDED.membership_generation
            RETURNING placement_uuid, user_uuid, read_at, mentioned
        ), coordinates AS MATERIALIZED (
          SELECT inserted.*, placement.stream_uuid, placement.topic_uuid,
                 placement.project_id, message.uuid AS message_uuid, message.created_at,
                 legacy.uuid AS legacy_uuid, legacy.ingest_sequence
          FROM inserted
          CROSS JOIN LATERAL (
              SELECT project_id, stream_uuid, topic_uuid, message_uuid, legacy_public_uuid, uuid
              FROM messenger_message_placements WHERE uuid = inserted.placement_uuid LIMIT 1
          ) AS placement
          CROSS JOIN LATERAL (
              SELECT uuid, created_at FROM messenger_messages WHERE uuid = placement.message_uuid LIMIT 1
          ) AS message
          CROSS JOIN LATERAL (
              SELECT uuid, ingest_sequence FROM m_workspace_messages
              WHERE uuid = COALESCE(placement.legacy_public_uuid, placement.uuid) LIMIT 1
          ) AS legacy
          WHERE placement.project_id = %s
        ) SELECT coordinate.*,
                 CASE
                     WHEN topic.notification_mode = 'mute' THEN false
                     WHEN topic.notification_mode = 'follow' THEN true
                     WHEN topic.notification_mode = 'unmute' THEN coordinate.mentioned
                     WHEN membership.notification_mode = 'all_messages' THEN true
                     WHEN membership.notification_mode = 'mentions_only' THEN coordinate.mentioned
                     ELSE false
                 END AS active
          FROM coordinates AS coordinate
          JOIN messenger_stream_bindings AS membership ON membership.project_id = coordinate.project_id
             AND membership.stream_uuid = coordinate.stream_uuid AND membership.user_uuid = coordinate.user_uuid AND membership.active
          JOIN messenger_user_topic_bindings AS topic ON topic.project_id = coordinate.project_id
             AND topic.topic_uuid = coordinate.topic_uuid AND topic.user_uuid = coordinate.user_uuid
        """,
        (project_uuid, job["created_at"], data, project_uuid, project_uuid),
    ).fetchall()


def _update_counters(
    session: typing.Any, job: dict, states: list[dict], inserted: list
) -> None:
    project_uuid = job["project_uuid"]
    read_state.ensure_new_project(session, project_uuid)
    session.execute(
        """INSERT INTO m_workspace_topic_message_stats_v1 (
               topic_uuid, project_id, stream_uuid, message_count, last_ingest_sequence
           ) SELECT placement.topic_uuid, %s, placement.stream_uuid, count(*), max(legacy.ingest_sequence)
             FROM messenger_message_placements AS placement
             JOIN m_workspace_messages AS legacy ON legacy.uuid = COALESCE(placement.legacy_public_uuid, placement.uuid)
             WHERE placement.project_id = %s AND placement.message_uuid = ANY(%s::uuid[])
             GROUP BY placement.topic_uuid, placement.stream_uuid
           ON CONFLICT (topic_uuid) DO UPDATE SET
               message_count = m_workspace_topic_message_stats_v1.message_count + EXCLUDED.message_count,
               last_ingest_sequence = GREATEST(m_workspace_topic_message_stats_v1.last_ingest_sequence, EXCLUDED.last_ingest_sequence),
               updated_at = now()""",
        (project_uuid, project_uuid, inserted),
    )
    if inserted:
        read_state.bump_project_structure_revisions(session, (project_uuid,))
    if not states:
        return
    if read_state.writes_compact_state(session, project_uuid):
        read_state._apply_coordinate_rows(
            session,
            project_uuid,
            [
                {
                    "user_uuid": row["user_uuid"],
                    "topic_uuid": row["topic_uuid"],
                    "ingest_sequence": row["ingest_sequence"],
                    "read": row["read_at"] is not None,
                }
                for row in states
            ],
        )
        session.execute(
            """INSERT INTO m_workspace_user_read_revisions_v1 (project_id, user_uuid, revision)
               SELECT %s, user_uuid, 1 FROM unnest(%s::uuid[]) AS users(user_uuid)
               ON CONFLICT (project_id, user_uuid) DO UPDATE
               SET revision = m_workspace_user_read_revisions_v1.revision + 1, updated_at = now()""",
            (project_uuid, sorted({row["user_uuid"] for row in states})),
        )
    # Derived deltas contain only the bounded RETURNING rows, never a history scan.
    values = (
        [row["user_uuid"] for row in states],
        [row["stream_uuid"] for row in states],
        [row["topic_uuid"] for row in states],
        [row["placement_uuid"] for row in states],
        [row["read_at"] is None for row in states],
        [row["read_at"] is None and row["active"] for row in states],
        [row["created_at"] for row in states],
    )
    for table, field in (
        ("messenger_user_topic_bindings", "topic_uuid"),
        ("messenger_stream_bindings", "stream_uuid"),
    ):
        session.execute(
            f"""WITH delta AS (
                SELECT user_uuid, {field} AS scope_uuid,
                       sum(unread::int) AS unread, sum(active::int) AS active,
                       (array_agg(placement_uuid ORDER BY created_at DESC, placement_uuid DESC))[1] AS last_uuid,
                       max(created_at) AS last_created_at
                FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[], %s::uuid[], %s::boolean[], %s::boolean[], %s::timestamptz[])
                     AS value(user_uuid, stream_uuid, topic_uuid, placement_uuid, unread, active, created_at)
                GROUP BY user_uuid, {field}
            ) UPDATE {table} AS binding
              SET unread_count = binding.unread_count + delta.unread,
                  active_unread_count = binding.active_unread_count + delta.active,
                  passive_unread_count = binding.passive_unread_count + delta.unread - delta.active,
                  last_message_uuid = CASE WHEN previous.uuid IS NULL
                      OR (delta.last_created_at, delta.last_uuid) > (previous.created_at, previous.placement_uuid)
                      THEN delta.last_uuid ELSE binding.last_message_uuid END,
                  updated_at = now()
              FROM delta
              LEFT JOIN LATERAL (
                  SELECT message.uuid, message.created_at, placement.uuid AS placement_uuid
                  FROM {table} AS old_binding
                  JOIN messenger_message_placements AS placement ON placement.project_id = old_binding.project_id
                     AND placement.uuid = old_binding.last_message_uuid
                  JOIN messenger_messages AS message ON message.project_id = placement.project_id AND message.uuid = placement.message_uuid
                  WHERE old_binding.project_id = %s AND old_binding.user_uuid = delta.user_uuid
                    AND old_binding.{field} = delta.scope_uuid
              ) AS previous ON true
              WHERE binding.project_id = %s AND binding.user_uuid = delta.user_uuid AND binding.{field} = delta.scope_uuid""",
            (*values, project_uuid, project_uuid),
        )
    session.execute(
        """INSERT INTO m_workspace_message_mentions_v1 (
               message_uuid, user_uuid, project_id, stream_uuid, topic_uuid, ingest_sequence, created_at
           ) SELECT message_uuid, user_uuid, %s, stream_uuid, topic_uuid, ingest_sequence, now()
             FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[], %s::uuid[], %s::bigint[], %s::boolean[])
                  AS value(message_uuid, user_uuid, stream_uuid, topic_uuid, ingest_sequence, mentioned)
             WHERE mentioned ON CONFLICT DO NOTHING""",
        (
            project_uuid,
            [row["legacy_uuid"] for row in states],
            [row["user_uuid"] for row in states],
            [row["stream_uuid"] for row in states],
            [row["topic_uuid"] for row in states],
            [row["ingest_sequence"] for row in states],
            [row["mentioned"] for row in states],
        ),
    )
    session.execute(
        """INSERT INTO m_external_history_notifications_v1 (job_uuid, uuid, user_uuid, stream_uuid, topic_uuid)
           SELECT DISTINCT %s, messenger_uuid_v5(%s, user_uuid::text || ':' || stream_uuid::text || ':' || COALESCE(topic_uuid::text, 'stream')),
                  user_uuid, stream_uuid, topic_uuid
           FROM (
               SELECT user_uuid, stream_uuid, topic_uuid
               FROM unnest(%s::uuid[], %s::uuid[], %s::uuid[]) AS value(user_uuid, stream_uuid, topic_uuid)
               UNION SELECT user_uuid, stream_uuid, NULL::uuid
               FROM unnest(%s::uuid[], %s::uuid[]) AS value(user_uuid, stream_uuid)
           ) AS scopes ON CONFLICT DO NOTHING""",
        (
            job["uuid"],
            job["uuid"],
            values[0],
            values[1],
            values[2],
            values[0],
            values[1],
        ),
    )


def _write_files(
    session: typing.Any, job: dict, part: preparation.PreparedPart
) -> None:
    session.execute(
        """INSERT INTO m_workspace_files (
               uuid, project_id, name, description, user_uuid, stream_uuid, content_type,
               size_bytes, hash, storage_type, storage_id, storage_object_id, external_account_uuid
           ) SELECT data.uuid, %s, data.name, '', data.owner_uuid, data.stream_uuid, data.content_type,
                    data.size_bytes, data.sha256, data.storage_type, data.storage_id, data.storage_object_id, data.account_uuid
             FROM jsonb_to_recordset(%s::jsonb) AS data(
                 uuid uuid, name text, owner_uuid uuid, stream_uuid uuid, content_type text,
                 size_bytes bigint, sha256 text, storage_type text, storage_id text, storage_object_id text, account_uuid uuid
             ) ON CONFLICT (uuid) DO NOTHING""",
        (job["project_uuid"], part.encoded["files"]),
    )
    session.execute(
        """INSERT INTO m_workspace_file_accesses (uuid, project_id, file_uuid, user_uuid)
           SELECT data.uuid, %s, data.file_uuid, data.user_uuid
           FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid, file_uuid uuid, user_uuid uuid)
           JOIN m_workspace_files AS file ON file.uuid = data.file_uuid AND file.project_id = %s
           JOIN messenger_stream_bindings AS membership ON membership.project_id = file.project_id
            AND membership.stream_uuid = file.stream_uuid AND membership.user_uuid = data.user_uuid AND membership.active
           ON CONFLICT (project_id, file_uuid, user_uuid) DO NOTHING""",
        (job["project_uuid"], part.encoded["file_access"], job["project_uuid"]),
    )


def write_part(
    session: typing.Any, identity: typing.Any, job: dict, part: preparation.PreparedPart
) -> int:
    configure_transaction(session)
    _lock_part(session, identity, job, part)
    if part.file_access_progress is not None:
        # The message checkpoint remains unchanged until every ACL page commits.
        # A restart or changed payload/observer mapping rechecks this fingerprint.
        _write_files(session, job, part)
        session.execute(
            """UPDATE m_external_history_imports_v1
               SET file_access_progress = %s::jsonb, consecutive_errors = 0, active_file_uuids = '{}'::uuid[],
                   lease_until = now() + interval '120 seconds', updated_at = now()
               WHERE uuid = %s AND lease_token = %s""",
            (json.dumps(part.file_access_progress), job["uuid"], job["lease_token"]),
        )
        return 0
    _write_topics(session, job, part.encoded["topics"])
    inserted = _write_messages(session, identity, job, part.encoded["messages"])
    _write_files(session, job, part)
    states = _write_states(session, job, part.encoded["states"])
    session.execute(
        """INSERT INTO messenger_message_reaction_facts (
               uuid, project_id, canonical_message_uuid, placement_uuid, user_uuid, emoji_name
           ) SELECT data.uuid, %s, data.message_uuid, data.placement_uuid, data.user_uuid, data.emoji_name
             FROM jsonb_to_recordset(%s::jsonb) AS data(
                 uuid uuid, message_uuid uuid, placement_uuid uuid, user_uuid uuid, emoji_name text
             ) WHERE data.message_uuid = ANY(%s::uuid[]) ON CONFLICT DO NOTHING""",
        (job["project_uuid"], part.encoded["reactions"], inserted),
    )
    _update_counters(session, job, states, inserted)
    session.execute(
        """INSERT INTO m_external_history_message_hashes_v1 (
               provider_realm_uuid, provider_message_id, message_uuid, source_hash, job_uuid
           ) SELECT %s, data.provider_id, data.uuid, data.source_hash, %s
             FROM jsonb_to_recordset(%s::jsonb) AS data(uuid uuid, provider_id text, source_hash text)
             JOIN messenger_messages AS message ON message.uuid = data.uuid AND message.deleted_at IS NULL
           ON CONFLICT (provider_realm_uuid, provider_message_id) DO UPDATE
               SET source_hash = EXCLUDED.source_hash, job_uuid = EXCLUDED.job_uuid""",
        (job["provider_realm_uuid"], job["uuid"], part.encoded["messages"]),
    )
    session.execute(
        """UPDATE m_external_history_imports_v1
           SET next_message_id = %s, consecutive_errors = 0, file_access_progress = NULL, active_file_uuids = '{}'::uuid[],
               applied_messages = applied_messages + %s,
               inserted_messages = inserted_messages + %s, skipped_messages = skipped_messages + %s,
               lease_until = now() + interval '120 seconds', updated_at = now(), safe_error = NULL
           WHERE uuid = %s AND lease_token = %s""",
        (
            max(part.source_ids),
            len(part.source_ids),
            len(inserted),
            len(part.source_ids) - len(inserted),
            job["uuid"],
            job["lease_token"],
        ),
    )
    return len(inserted)


def flush_notifications(session: typing.Any, identity: typing.Any, job: dict) -> bool:
    configure_transaction(session)
    repository.guard(session, identity, job, lease=True)
    rows = session.execute(
        """SELECT * FROM m_external_history_notifications_v1 WHERE job_uuid = %s
           ORDER BY uuid LIMIT 20 FOR UPDATE SKIP LOCKED""",
        (job["uuid"],),
    ).fetchall()
    if not rows:
        session.execute(
            """UPDATE m_external_history_imports_v1 SET status = 'complete', completed_at = now(), consecutive_errors = 0, active_file_uuids = '{}'::uuid[],
                       worker_uuid = NULL, lease_until = NULL, updated_at = now()
               WHERE uuid = %s AND lease_token = %s""",
            (job["uuid"], job["lease_token"]),
        )
        return True
    for row in rows:
        scope_kind = "user-topic" if row["topic_uuid"] else "user-stream"
        scope_key = f"{job['project_uuid']}:{row['user_uuid']}:{row['topic_uuid'] or row['stream_uuid']}"
        payload = {
            "source_kind": "history.imported",
            "user_uuid": str(row["user_uuid"]),
            "stream_uuid": str(row["stream_uuid"]),
            "topic_uuid": str(row["topic_uuid"]) if row["topic_uuid"] else None,
        }
        session.execute(
            """INSERT INTO messenger_domain_outbox_events (uuid, project_id, event_kind, scope_kind, scope_key, payload)
               VALUES (%s, %s, 'read_counters', %s, %s, %s::jsonb) ON CONFLICT DO NOTHING""",
            (
                row["uuid"],
                job["project_uuid"],
                scope_kind,
                scope_key,
                json.dumps(payload),
            ),
        )
    session.execute(
        "DELETE FROM m_external_history_notifications_v1 WHERE job_uuid = %s AND uuid = ANY(%s::uuid[])",
        (job["uuid"], [row["uuid"] for row in rows]),
    )
    session.execute(
        "UPDATE m_external_history_imports_v1 SET consecutive_errors=0 WHERE uuid=%s AND lease_token=%s",
        (job["uuid"], job["lease_token"]),
    )
    return False
