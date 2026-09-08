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

"""Bounded import bookkeeping. Every function uses the caller's short session."""

import typing
import uuid as sys_uuid

import dataclasses
import datetime
import json

from workspace.external_bridge_control import provider_v2
from workspace.history_import import contract


MAX_CONSECUTIVE_ERRORS = 20


def authorize_sources(
    session: typing.Any, identity: typing.Any, batch: contract.Batch
) -> list[dict]:
    """Match the complete current selection, never trusting JSON Workspace IDs."""
    policy = session.execute(
        "SELECT enabled, emergency_suspended FROM m_external_provider_policies_v1 WHERE provider = 'zulip' FOR SHARE",
        (),
    ).fetchone()
    if policy is None or not policy["enabled"] or policy["emergency_suspended"]:
        raise contract.ImportError("history_provider_paused", 409)
    # LIMIT 1 retains correlated primary-key lookups even with sparse statistics.
    # Flattening both desired-resource joins can repeatedly scan every assignment.
    rows = session.execute(
        """
        SELECT account.uuid AS account_uuid, account.owner_user_uuid,
               account.provider_owner_user_id, account.provider_realm_uuid,
               account.settings->>'server_url' AS server_url,
               account_desired.generation AS account_generation,
               chat.uuid AS chat_uuid, chat.project_id,
               chat.projection_stream_uuid, chat.provider_chat_id,
               chat.source, chat.display_name, desired.generation AS assignment_generation,
               desired.resource->'workspace_projection'->'topics' AS materialized_topics,
               desired.resource->>'history_depth' AS history_depth,
               desired.resource->'provider_chat'->>'provider_chat_key' AS chat_key
        FROM m_external_accounts_v2 AS account
        JOIN LATERAL (
            SELECT bridge_instance_uuid, generation
            FROM m_external_bridge_desired_resources_v1
            WHERE bridge_instance_uuid = %s
              AND provider_kind = 'zulip'
              AND resource_type = 'external_account'
              AND resource_uuid = account.uuid AND operation = 'upsert'
              AND resource->>'synchronization_enabled' = 'true'
            LIMIT 1
        ) AS account_desired ON true
        JOIN m_external_provider_identity_links_v1 AS owner_link
          ON owner_link.provider = 'zulip'
         AND owner_link.provider_realm_uuid = account.provider_realm_uuid
         AND owner_link.provider_user_id = account.provider_owner_user_id
         AND owner_link.workspace_user_uuid = account.owner_user_uuid
         AND owner_link.link_kind = 'verified_account_owner'
        JOIN m_external_chats_v2 AS chat
          ON chat.external_account_uuid = account.uuid AND chat.provider = 'zulip'
        JOIN LATERAL (
            SELECT generation, resource
            FROM m_external_bridge_desired_resources_v1
            WHERE bridge_instance_uuid = account_desired.bridge_instance_uuid
              AND provider_kind = 'zulip'
              AND resource_type = 'external_chat_assignment'
              AND resource_uuid = chat.uuid AND operation = 'upsert'
              AND resource->>'selected' = 'true'
            LIMIT 1
        ) AS desired ON true
        WHERE account.provider = 'zulip' AND account.provider_realm_uuid = %s
          AND chat.project_id = %s AND chat.selected AND NOT chat.transition_pending
          AND chat.projection_stream_uuid IS NOT NULL
        ORDER BY chat.uuid LIMIT %s
        """,
        (
            identity.bridge_instance_uuid,
            batch.provider_realm_uuid,
            batch.project_uuid,
            contract.MAX_SOURCES + 1,
        ),
    ).fetchall()
    if len(rows) > contract.MAX_SOURCES:
        raise contract.ImportError("history_source_limit_exceeded", 422)
    expected = [
        {
            "account_uuid": str(row["account_uuid"]),
            "account_generation": row["account_generation"],
            "chat_uuid": str(row["chat_uuid"]),
            "assignment_generation": row["assignment_generation"],
        }
        for row in rows
    ]
    if identity.provider_kind != "zulip" or expected != list(batch.sources):
        raise contract.ImportError("history_sources_changed", 409)
    result = [dict(row) for row in rows]
    for row in result:
        # Assignments retain canonical materialized topics even when Zulip's
        # current catalog omits old topics. Reuse those IDs across observers.
        topics = {
            topic["provider_topic_id"]: topic for topic in row["source"]["topics"]
        }
        for topic in row.pop("materialized_topics") or []:
            topics.setdefault(topic["provider_topic_id"], topic)
        row["source"]["topics"] = list(topics.values())
        row["topic_namespace_uuid"] = min(
            (
                peer["chat_uuid"]
                for peer in result
                if peer["projection_stream_uuid"] == row["projection_stream_uuid"]
            ),
            key=str,
        )
        if row["provider_owner_user_id"] is None:
            raise contract.ImportError("history_owner_unverified", 409)
        key = row["chat_key"] or row["provider_chat_id"]
        if key.startswith(("direct:", "group_direct:")):
            participants = sorted(set(map(int, key.partition(":")[2].split(","))))
            key = f"direct-conversation:v1:{len(participants)}:" + ",".join(
                map(str, participants)
            )
        row["chat_key"] = key
        # Also validates direct-conversation and channel key syntax.
        provider_v2._legacy_chat_key(row["chat_key"])
    return result


def validate_observers(batch: contract.Batch, sources: list[dict]) -> None:
    observers: dict[str, set[int]] = {}
    for source in sources:
        observers.setdefault(source["chat_key"], set()).add(
            int(source["provider_owner_user_id"])
        )
    for message in batch.body["messages"]:
        if not {item["user_id"] for item in message["access"]}.issubset(
            observers.get(contract.chat_key(message), set())
        ):
            raise contract.ImportError("history_observer_not_assigned", 403)


def get_job(
    session: typing.Any,
    identity: typing.Any,
    job_uuid: sys_uuid.UUID,
    *,
    lock: bool = False,
) -> dict:
    row = session.execute(
        "SELECT * FROM m_external_history_imports_v1 WHERE uuid = %s AND bridge_uuid = %s"
        + (" FOR KEY SHARE" if lock else ""),
        (job_uuid, identity.bridge_instance_uuid),
    ).fetchone()
    if row is None:
        raise contract.ImportError("history_import_not_found", 404)
    return dict(row)


def batch_scope(job: dict) -> contract.Batch:
    return contract.Batch(
        job["project_uuid"],
        job["provider_realm_uuid"],
        job["generation"],
        tuple(job["sources"]),
        {},
    )


def existing_job(
    session: typing.Any, identity: typing.Any, batch: contract.Batch
) -> dict | None:
    row = session.execute(
        "SELECT * FROM m_external_history_imports_v1 WHERE uuid = %s",
        (batch.job_uuid(identity.bridge_instance_uuid, identity.identity_generation),),
    ).fetchone()
    return None if row is None else dict(row)


def check_capacity(session: typing.Any, identity: typing.Any) -> None:
    row = session.execute(
        """SELECT count(*) AS count FROM (
            SELECT 1 FROM m_external_history_imports_v1
            WHERE bridge_uuid = %s AND status IN ('pending', 'running', 'waiting_files')
            LIMIT 8
        ) AS active""",
        (identity.bridge_instance_uuid,),
    ).fetchone()
    if row["count"] >= 8:
        raise contract.ImportError("history_import_busy", 429)


def register(
    session: typing.Any,
    identity: typing.Any,
    batch: contract.Batch,
    storage: typing.Any,
) -> dict:
    # Serialize queue admission only; the lock is never held across storage I/O.
    session.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"workspace-history-admission-v1:{identity.bridge_instance_uuid}",),
    )
    scope = (
        identity.bridge_instance_uuid,
        batch.project_uuid,
        batch.provider_realm_uuid,
    )
    session.execute(
        """INSERT INTO m_external_history_scopes_v1
            (bridge_uuid, project_uuid, provider_realm_uuid, generation)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (bridge_uuid, project_uuid, provider_realm_uuid)
            DO UPDATE SET generation = GREATEST(
                m_external_history_scopes_v1.generation, EXCLUDED.generation
            ), updated_at = now()""",
        (*scope, batch.generation),
    )
    generation = session.execute(
        """SELECT generation FROM m_external_history_scopes_v1
           WHERE bridge_uuid = %s AND project_uuid = %s AND provider_realm_uuid = %s""",
        scope,
    ).fetchone()["generation"]
    if generation != batch.generation:
        raise contract.ImportError("history_generation_superseded", 409)
    # Only queued/running work is cancelled; imported messages are never deleted.
    session.execute(
        """UPDATE m_external_history_imports_v1 SET status = 'superseded',
                   worker_uuid = NULL, lease_until = NULL, updated_at = now()
           WHERE bridge_uuid = %s AND project_uuid = %s AND provider_realm_uuid = %s
             AND (generation < %s OR sources <> %s::jsonb)
             AND status IN ('pending', 'running', 'waiting_files')""",
        (*scope, batch.generation, json.dumps(batch.sources)),
    )
    authorize_sources(session, identity, batch)
    existing = existing_job(session, identity, batch)
    if existing is not None:
        return get_job(session, identity, existing["uuid"], lock=True)
    check_capacity(session, identity)
    session.execute(
        """INSERT INTO m_external_history_imports_v1 (
            uuid, bridge_uuid, project_uuid, provider_realm_uuid, generation,
            identity_generation, from_id, to_id, body_hash, storage, sources, total_messages
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
        ON CONFLICT (uuid) DO NOTHING""",
        (
            batch.job_uuid(identity.bridge_instance_uuid, identity.identity_generation),
            *scope,
            batch.generation,
            identity.identity_generation,
            batch.body["from_id"],
            batch.body["to_id"],
            batch.body["hash"],
            json.dumps(dataclasses.asdict(storage)),
            json.dumps(batch.sources),
            len(batch.body["messages"]),
        ),
    )
    return get_job(
        session,
        identity,
        batch.job_uuid(identity.bridge_instance_uuid, identity.identity_generation),
        lock=True,
    )


def guard(
    session: typing.Any, identity: typing.Any, job: dict, *, lease: bool = False
) -> list[dict]:
    """Acquire fences in scope/account/job order, then verify current authority."""
    bridge = session.execute(
        """SELECT provider, identity_generation, status FROM m_external_bridge_instances_v2
           WHERE uuid = %s FOR SHARE""",
        (identity.bridge_instance_uuid,),
    ).fetchone()
    if (
        bridge is None
        or bridge["provider"] != identity.provider_kind
        or bridge["identity_generation"] != job["identity_generation"]
        or bridge["status"] in {"suspended", "revoked"}
    ):
        raise contract.ImportError("history_bridge_authority_changed", 409)
    scope = session.execute(
        """SELECT generation FROM m_external_history_scopes_v1
           WHERE bridge_uuid = %s AND project_uuid = %s AND provider_realm_uuid = %s
           FOR SHARE""",
        (job["bridge_uuid"], job["project_uuid"], job["provider_realm_uuid"]),
    ).fetchone()
    if scope is None or scope["generation"] != job["generation"]:
        raise contract.ImportError("history_generation_superseded", 409)
    session.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended('workspace-read-state-schema-v1', 0))",
        (),
    )
    accounts = sorted({source["account_uuid"] for source in job["sources"]})
    if len(job["sources"]) > contract.MAX_SOURCES:
        raise contract.ImportError("history_source_limit_exceeded", 422)
    session.execute(
        """SELECT pg_advisory_xact_lock_shared(hashtextextended(
            'workspace-external-account-resource-v1:' || account_uuid, 0))
            FROM unnest(%s::text[]) AS account(account_uuid) ORDER BY account_uuid""",
        (accounts,),
    )
    sources = authorize_sources(session, identity, batch_scope(job))
    current = session.execute(
        "SELECT status, lease_token, lease_until, worker_uuid FROM m_external_history_imports_v1 WHERE uuid = %s FOR UPDATE",
        (job["uuid"],),
    ).fetchone()
    if current is None:
        raise contract.ImportError("history_import_not_found", 404)
    if current["status"] in {"superseded", "failed"}:
        raise contract.ImportError("history_import_not_active", 409)
    if lease and (
        current["lease_token"] != job["lease_token"]
        or current["worker_uuid"] != job["worker_uuid"]
        or current["lease_until"] is None
        or current["lease_until"] <= datetime.datetime.now(datetime.timezone.utc)
    ):
        raise contract.ImportError("history_import_lease_expired", 409)
    return sources


def claim(
    session: typing.Any,
    worker_uuid: sys_uuid.UUID,
    preferred_uuid: sys_uuid.UUID | None = None,
) -> dict | None:
    # A bounded queue of eight jobs keeps this scan independent of history size.
    row = session.execute(
        """WITH candidate AS (
            SELECT job.uuid FROM m_external_history_imports_v1 AS job
            JOIN m_external_history_scopes_v1 AS scope
              USING (bridge_uuid, project_uuid, provider_realm_uuid)
            WHERE (%s::uuid IS NULL OR job.uuid = %s)
              AND job.status IN ('pending', 'running', 'waiting_files') AND job.available_at <= now()
              AND (job.lease_until IS NULL OR job.lease_until <= now())
              AND job.generation = scope.generation
            ORDER BY job.available_at, job.created_at, job.uuid
            LIMIT 1 FOR UPDATE OF job SKIP LOCKED
        ) UPDATE m_external_history_imports_v1 AS job
          SET status = 'running', worker_uuid = %s, lease_token = lease_token + 1,
              lease_until = now() + interval '120 seconds', updated_at = now()
          FROM candidate WHERE job.uuid = candidate.uuid RETURNING job.*""",
        (preferred_uuid, preferred_uuid, worker_uuid),
    ).fetchone()
    return None if row is None else dict(row)


def release(
    session: typing.Any,
    job: dict,
    *,
    error: str | None = None,
    superseded: bool = False,
    permanent: bool = False,
    count_error: bool = True,
    delay: float = 0.1,
    part_size: int | None = None,
) -> None:
    status = "superseded" if superseded else "failed" if permanent else "pending"
    increment = int(error is not None and count_error)
    session.execute(
        """UPDATE m_external_history_imports_v1
           SET status = CASE WHEN %s = 'pending' AND consecutive_errors + %s >= %s
                             THEN 'failed' ELSE %s END,
               safe_error = %s, attempts = attempts + %s,
               consecutive_errors = consecutive_errors + %s,
               part_size = COALESCE(%s, part_size), lease_until = NULL,
               worker_uuid = NULL, available_at = now() + %s * interval '1 second',
               updated_at = now()
           WHERE uuid = %s AND lease_token = %s AND status = 'running'""",
        (
            status,
            increment,
            MAX_CONSECUTIVE_ERRORS,
            status,
            error,
            int(error is not None),
            increment,
            part_size,
            delay,
            job["uuid"],
            job["lease_token"],
        ),
    )


def public_status(session: typing.Any, job: dict) -> dict:
    missing = session.execute(
        """SELECT uuid, source_path, account_uuids FROM m_external_history_files_v1
           WHERE job_uuid = %s AND uuid = ANY(%s::uuid[]) AND status = 'missing' ORDER BY uuid LIMIT 50""",
        (job["uuid"], job["active_file_uuids"]),
    ).fetchall()
    return {
        "uuid": str(job["uuid"]),
        "status": job["status"],
        "generation": job["generation"],
        "hash": job["body_hash"],
        "from_id": job["from_id"],
        "to_id": job["to_id"],
        "total_messages": job["total_messages"],
        "applied_messages": job["applied_messages"],
        "inserted_messages": job["inserted_messages"],
        "skipped_messages": job["skipped_messages"],
        "safe_error": job["safe_error"],
        "files": [{**dict(row), "uuid": str(row["uuid"])} for row in missing],
    }
