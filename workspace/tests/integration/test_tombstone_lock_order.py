# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""A completed deletion fanout must not deadlock another public deletion."""

import concurrent.futures
import threading
import uuid
from unittest import mock

import pytest
from oslo_config import cfg
from restalchemy.common import contexts

from workspace.common import mutation_journal_opts
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import test_messenger_v2 as native
from workspace.tests.integration import test_mutation_journal as journal

conversation = journal.conversation


@pytest.mark.parametrize("deleted_index", [0, 1])
@pytest.mark.parametrize("fanout_batch_size", [1, 128])
def test_public_delete_finishes_while_previous_snapshot_delivery_is_paused(
    api, db, conversation, deleted_index, fanout_batch_size
):
    stream, messages, peer = conversation
    cfg.CONF.set_override("enabled", False, group=mutation_journal_opts.DOMAIN)
    provider = uuid.uuid5(uuid.NAMESPACE_DNS, "tombstone.example.com")
    db.execute(
        "INSERT INTO messenger_providers VALUES(%s,'tombstone.example.com') ON CONFLICT DO NOTHING",
        (provider,),
    )
    db.execute(
        "UPDATE messenger_streams SET source_name='zulip',provider_uuid=%s,provider_identity='42' "
        "WHERE project_id=%s AND uuid=%s",
        (provider, api.project_id, stream["uuid"]),
    )
    db.execute(
        "UPDATE messenger_messages SET source_name='zulip',provider_uuid=%s,provider_identity=uuid::text "
        "WHERE project_id=%s",
        (provider, api.project_id),
    )
    assert (
        api.delete(f"{native.MESSAGES}{messages[deleted_index]['uuid']}").status_code
        == 204
    )
    delivering, release = threading.Event(), threading.Event()
    original = v2_projection._process_delivery_snapshot

    def gated_delivery(session, task, batch_size):
        if task["payload"].get("source_kind") == "message.deleted":
            delivering.set()
            assert release.wait(10), "Snapshot delivery was not released"
        return original(session, task, batch_size)

    def project():
        for _ in range(20):
            with contexts.Context().session_manager() as session:
                processed = v2_projection.process_one_projection_task(
                    session,
                    "tombstone-lock-regression",
                    fanout_batch_size=fanout_batch_size,
                )
            if delivering.is_set():
                return processed
        raise AssertionError("Deletion fanout did not reach its delivery")

    with mock.patch.object(
        v2_projection, "_process_delivery_snapshot", side_effect=gated_delivery
    ):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            worker = pool.submit(project)
            try:
                assert delivering.wait(5), "Deletion fanout did not reach its delivery"
                deleting = pool.submit(
                    api.delete,
                    f"{native.MESSAGES}{messages[1 - deleted_index]['uuid']}",
                )
                response = deleting.result(timeout=5)
                assert response.status_code == 204, response.text
                assert not db.execute(
                    "SELECT 1 FROM messenger_messages WHERE project_id=%s",
                    (api.project_id,),
                ).fetchall()
            finally:
                release.set()
            assert worker.result(timeout=10)
            response = deleting.result(timeout=10)
            assert response.status_code == 204, response.text
    native._drain()
    assert not db.execute(
        "SELECT 1 FROM messenger_messages WHERE project_id=%s", (api.project_id,)
    ).fetchall()
    assert not db.execute(
        "SELECT 1 FROM messenger_counter_updates WHERE project_id=%s", (api.project_id,)
    ).fetchall()
    assert db.execute(
        "SELECT unread_count,active_unread_count,passive_unread_count "
        "FROM messenger_stream_bindings WHERE project_id=%s AND user_uuid=%s AND stream_uuid=%s",
        (api.project_id, peer, stream["uuid"]),
    ).fetchone() == (0, 0, 0)
