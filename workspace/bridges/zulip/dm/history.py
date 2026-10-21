# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Durable source fingerprints, independent of derived message identities."""

import contextlib
import json
import uuid

from restalchemy.common import contexts
from restalchemy.storage.sql import utils

from workspace.bridges.zulip import content_conversion
from workspace.bridges.zulip import history_fingerprints


def revision(account):
    return history_fingerprints.digest(
        [
            account.subscription_revision(),
            history_fingerprints.VERSION,
            content_conversion.VERSION,
        ]
    )


def batch(account, first_id):
    return (
        contexts.Context()
        .get_session()
        .execute(
            "SELECT * FROM bridge_zulip_v5_history_batches WHERE external_account_uuid=%s AND first_id=%s",
            (account.uuid, first_id),
        )
        .fetchone()
    )


def next_known_id(account, before):
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


def compare(
    account, first_id, last_id, messages, observed_at, fingerprints=None, hashes=None
):
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
    previous = {}
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
    if current is not None and current["source_revision"] == source_revision:
        previous = {
            row["message_id"]: row["hashes"]
            for row in session.execute(
                "SELECT message_id,hashes FROM bridge_zulip_v5_message_fingerprints "
                "WHERE external_account_uuid=%s AND message_id BETWEEN %s AND %s",
                (account.uuid, first_id, last_id),
            ).fetchall()
        }
    elif current is None:
        # Lazy bootstrap also notices imported observations lost during queue
        # downtime. Absence withdraws this observer, never the shared message.
        namespace = uuid.uuid5(uuid.NAMESPACE_DNS, account.get_host())
        previous = {
            row["message_id"]: dict.fromkeys(history_fingerprints.COMPONENTS)
            for row in session.execute(
                "SELECT message_id FROM bridge_zulip_v5_messages WHERE external_account_uuid=%s AND project_id=%s "
                "AND message_id BETWEEN %s AND %s AND message_uuid=messenger_uuid_v5(%s,message_id::text)",
                (account.uuid, account.project_id, first_id, last_id, namespace),
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
def applying(account, first_id):
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
    account,
    first_id,
    batch_revision,
    task_uuid,
    messages,
    complete_snapshot,
    missing_ids=(),
):
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


def barrier(account, message_id):
    current = batch(account, history_fingerprints.range_start(message_id))
    return (
        current["observed_at"]
        if current is not None and message_id <= current["covered_to"]
        else None
    )
