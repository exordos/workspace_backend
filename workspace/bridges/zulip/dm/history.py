# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Durable source fingerprints, independent of derived message identities."""

import contextlib
import datetime
import json
import typing
import uuid

from restalchemy.common import contexts
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip import history_fingerprints
from workspace.messenger_api.dm import external_models


def lock_observation_rows(
    account: external_models.ExternalAccount,
    observations: list[dict[str, typing.Any]],
    *,
    replay: bool | None = None,
) -> None:
    """Cache rows precede deliveries, then affected history batches in key order.

    Callers prepare shared reaction cache rows before entering this fence.
    A delivery or reaction can invalidate several observers' existing batches.
    """
    identifiers = {row["id"] for row in observations if isinstance(row.get("id"), int)}
    if not identifiers:
        return
    session = contexts.Context().get_session()
    deliveries = [
        row["_delivery_uuid"] for row in observations if row.get("_delivery_uuid")
    ]
    shared = {
        row["id"]
        for row in observations
        if row.get("reactions") or row.get("_delivery_uuid")
    }
    for row in session.execute(
        """SELECT uuid,zulip_message_id FROM bridge_zulip_v5_deliveries
           WHERE project_id=%s AND hostname=%s
             AND (zulip_message_id=ANY(%s::bigint[]) OR uuid=ANY(%s::uuid[]))
           ORDER BY uuid FOR NO KEY UPDATE""",
        (account.project_id, account.get_host(), list(identifiers), deliveries),
    ).fetchall():
        if row["zulip_message_id"] is not None:
            shared.add(row["zulip_message_id"])
    if replay is None:
        replay = bool(
            session.execute(
                "SELECT current_setting('workspace.zulip_history_batch',true) AS batch"
            ).fetchone()["batch"]
        )
    if replay:
        return
    reaction_ids = [row["id"] for row in observations if "reactions" in row]
    if reaction_ids:
        shared.update(
            row["message_id"]
            for row in session.execute(
                """SELECT cache.message_id FROM bridge_zulip_v5_reaction_messages AS cache
               WHERE cache.project_id=%s AND cache.hostname=%s AND cache.message_id=ANY(%s::bigint[])
                 AND EXISTS (SELECT 1 FROM bridge_zulip_v5_reactions AS fact
                             WHERE fact.reaction_message_uuid=cache.uuid)""",
                (account.project_id, account.get_host(), reaction_ids),
            ).fetchall()
        )
    session.execute(
        """SELECT external_account_uuid,first_id FROM bridge_zulip_v5_history_batches
           WHERE project_id=%s AND hostname=%s AND (
               (external_account_uuid=%s AND (%s OR first_id=ANY(%s::bigint[])))
               OR first_id=ANY(%s::bigint[]))
           ORDER BY external_account_uuid,first_id FOR NO KEY UPDATE""",
        (
            account.project_id,
            account.get_host(),
            account.uuid,
            0 in identifiers,
            sorted(
                {
                    (identifier - 1) // 1000 * 1000 + 1
                    for identifier in identifiers
                    if identifier > 0
                }
            ),
            sorted(
                {
                    (identifier - 1) // 1000 * 1000 + 1
                    for identifier in shared
                    if identifier > 0
                }
            ),
        ),
    ).fetchall()


def revision(account: external_models.ExternalAccount) -> str:
    return history_fingerprints.digest(
        [
            account.subscription_revision(),
            history_fingerprints.VERSION,
            content_conversion.VERSION,
        ]
    )


def batch(
    account: external_models.ExternalAccount, first_id: int
) -> dict[str, typing.Any] | None:
    return (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT * FROM bridge_zulip_v5_history_batches WHERE external_account_uuid=%s AND first_id=%s",
            (account.uuid, first_id),
        )
        .fetchone()
    )


def next_known_id(account: external_models.ExternalAccount, before: int) -> int:
    return (
        contexts.Context()
        .get_session()
        .execute(
            """SELECT GREATEST(
            COALESCE((SELECT message_id FROM bridge_zulip_v5_messages
                WHERE external_account_uuid=%s AND project_id=%s AND message_id<%s
                ORDER BY message_id DESC LIMIT 1),0),
            COALESCE((SELECT covered_to FROM bridge_zulip_v5_history_batches
                WHERE external_account_uuid=%s AND first_id<%s AND record_count>0
                ORDER BY first_id DESC LIMIT 1),0)) AS message_id""",
            (account.uuid, account.project_id, before, account.uuid, before),
        )
        .fetchone()["message_id"]
    )


def retained_upper_bound(
    account: external_models.ExternalAccount, at: datetime.datetime
) -> int:
    """Check a disappeared tail without chasing observations newer than this task."""
    return (
        contexts.Context()
        .get_session()
        .execute(
            """SELECT GREATEST(
            COALESCE((SELECT message_id FROM bridge_zulip_v5_messages
                WHERE external_account_uuid=%s AND project_id=%s AND created_at<=%s
                ORDER BY message_id DESC LIMIT 1),0),
            COALESCE((SELECT covered_to FROM bridge_zulip_v5_history_batches
                WHERE external_account_uuid=%s AND record_count>0 AND observed_at<=%s
                ORDER BY first_id DESC LIMIT 1),0)) AS message_id""",
            (
                account.uuid,
                account.project_id,
                at,
                account.uuid,
                at.replace(tzinfo=None),
            ),
        )
        .fetchone()["message_id"]
    )


def compare(
    account: external_models.ExternalAccount,
    first_id: int,
    last_id: int,
    messages: list[dict[str, typing.Any]],
    observed_at: datetime.datetime,
    fingerprints: dict[int, dict[str, str | None]] | None = None,
    hashes: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, typing.Any]], int] | None:
    """One batch lookup on a hit; a single bounded leaf lookup on a miss."""
    session = contexts.Context().get_session()
    current = batch(account, first_id)
    source_revision = revision(account)
    if fingerprints is None:
        fingerprints = {
            data["id"]: history_fingerprints.message_hashes(data) for data in messages
        }
    if hashes is None:
        hashes = history_fingerprints.batch_hashes_from_entries(
            sorted(fingerprints.items()), first_id, last_id
        )
    at = observed_at.replace(tzinfo=None)
    if current is not None and (
        (current["invalidated_at"] is not None and current["invalidated_at"] > at)
        or (current["observed_at"] is not None and current["observed_at"] > at)
    ):
        return None
    if (
        current is not None
        and current["ready"]
        and current["source_revision"] == source_revision
        and current["covered_to"] == last_id
        and current["hashes"] == hashes
        and all(value is not None for value in hashes.values())
    ):
        session.execute(
            "UPDATE bridge_zulip_v5_history_batches SET observed_at=GREATEST(observed_at,%s) "
            "WHERE external_account_uuid=%s AND first_id=%s",
            (at, account.uuid, first_id),
        )
        return [], current["revision"]
    if current is not None and current["source_revision"] != source_revision:
        session.execute(
            "UPDATE bridge_zulip_v5_message_fingerprints SET hashes=%s::jsonb "
            "WHERE external_account_uuid=%s AND first_id=%s",
            (
                json.dumps(dict.fromkeys(history_fingerprints.COMPONENTS)),
                account.uuid,
                first_id,
            ),
        )
    # Realtime imports can add an observation without adding a fingerprint, or
    # recreate one whose old fingerprint says absent. Include those records in
    # the same bounded lookup so a missed subsequent deletion is still noticed.
    namespace = uuid.uuid5(uuid.NAMESPACE_DNS, typing.cast(str, account.get_host()))
    previous = {
        row["message_id"]: (
            row["hashes"]
            if row["hashes"] is not None
            else dict.fromkeys(history_fingerprints.COMPONENTS)
        )
        for row in session.execute(
            """SELECT COALESCE(fingerprint.message_id, imported.message_id) AS message_id,
                CASE WHEN imported.message_id IS NOT NULL
                          AND fingerprint.hashes @> '{"absent":true}'::jsonb
                     THEN NULL ELSE fingerprint.hashes END AS hashes
            FROM (SELECT message_id, hashes FROM bridge_zulip_v5_message_fingerprints
                  WHERE external_account_uuid=%s AND message_id BETWEEN %s AND %s) AS fingerprint
            FULL JOIN (SELECT message_id FROM bridge_zulip_v5_messages
                  WHERE external_account_uuid=%s AND project_id=%s
                    AND message_id BETWEEN %s AND %s
                    AND message_uuid=messenger_uuid_v5(%s,message_id::text)) AS imported
              USING (message_id)
            ORDER BY message_id""",
            (
                account.uuid,
                first_id,
                last_id,
                account.uuid,
                account.project_id,
                first_id,
                last_id,
                namespace,
            ),
        ).fetchall()
    }
    changed = []
    for data in messages:
        components = history_fingerprints.changed_components(
            fingerprints[data["id"]], previous.get(data["id"])
        )
        if components:
            changed.append({"message": data, "components": components})
    observed_ids = set(fingerprints)
    changed.extend(
        {"message": {"id": message_id}, "components": ["visibility"]}
        for message_id, hashes in previous.items()
        if message_id not in observed_ids and not hashes.get("absent", False)
    )
    row = session.execute(
        """INSERT INTO bridge_zulip_v5_history_batches
            (external_account_uuid,project_id,hostname,first_id,covered_to,source_revision,hashes,record_count,observed_at,ready)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT (external_account_uuid,first_id) DO UPDATE
                SET source_revision=EXCLUDED.source_revision,hostname=EXCLUDED.hostname,covered_to=EXCLUDED.covered_to,
                    hashes=EXCLUDED.hashes,record_count=EXCLUDED.record_count,
                    observed_at=EXCLUDED.observed_at,ready=EXCLUDED.ready,pending='{}',revision=bridge_zulip_v5_history_batches.revision+1
            RETURNING revision""",
        (
            account.uuid,
            account.project_id,
            account.get_host(),
            first_id,
            last_id,
            source_revision,
            json.dumps(hashes),
            len(messages),
            at,
            not changed and all(value is not None for value in hashes.values()),
        ),
    ).fetchone()
    return changed, row["revision"]


@contextlib.contextmanager
def applying(
    account: external_models.ExternalAccount, first_id: int
) -> typing.Iterator[None]:
    session = contexts.Context().get_session()
    # Rollback restores transaction-local settings even when PostgreSQL has
    # aborted the statement. A finally query would mask the original deadlock.
    with utils.savepoint():
        previous = session.execute(
            "SELECT current_setting('workspace.zulip_history_batch',true) AS value"
        ).fetchone()["value"]
        session.execute(
            "SELECT set_config('workspace.zulip_history_batch',%s,true)",
            (f"{account.uuid}:{first_id}",),
        )
        yield
        session.execute(
            "SELECT set_config('workspace.zulip_history_batch',%s,true)",
            (previous or "",),
        )


def complete(
    account: external_models.ExternalAccount,
    first_id: int,
    batch_revision: int,
    task_uuid: uuid.UUID,
    messages: list[dict[str, typing.Any]],
    complete_snapshot: bool,
    missing_ids: typing.Sequence[int] = (),
) -> None:
    session = contexts.Context().get_session()
    current = batch(account, first_id)
    if current is None or current["revision"] != batch_revision:
        return
    if messages or missing_ids:
        fingerprints = [
            {"id": data["id"], "hashes": history_fingerprints.message_hashes(data)}
            for data in messages
        ]
        fingerprints.extend(
            {
                "id": message_id,
                "hashes": {
                    **dict.fromkeys(history_fingerprints.COMPONENTS),
                    "absent": True,
                },
            }
            for message_id in missing_ids
        )
        session.execute(
            """INSERT INTO bridge_zulip_v5_message_fingerprints(external_account_uuid,message_id,hashes)
            SELECT %s,id,hashes FROM jsonb_to_recordset(%s::jsonb) AS data(id BIGINT,hashes JSONB)
            ON CONFLICT (external_account_uuid,message_id) DO UPDATE SET hashes=EXCLUDED.hashes""",
            (
                account.uuid,
                json.dumps(fingerprints),
            ),
        )
    session.execute(
        """UPDATE bridge_zulip_v5_history_batches
        SET pending=array_remove(pending,%s), hashes=CASE WHEN %s THEN hashes ELSE '{}'::jsonb END,
            ready=cardinality(array_remove(pending,%s))=0 AND %s
                AND hashes ?& ARRAY['text','flags','metadata','reactions']
                AND NOT jsonb_path_exists(hashes,'$.* ? (@ == null)')
        WHERE external_account_uuid=%s AND first_id=%s AND revision=%s""",
        (
            task_uuid,
            complete_snapshot,
            task_uuid,
            complete_snapshot,
            account.uuid,
            first_id,
            batch_revision,
        ),
    )


def barrier(
    account: external_models.ExternalAccount, message_id: int
) -> datetime.datetime | None:
    current = batch(account, history_fingerprints.range_start(message_id))
    return (
        current["observed_at"]
        if current is not None and message_id <= current["covered_to"]
        else None
    )
