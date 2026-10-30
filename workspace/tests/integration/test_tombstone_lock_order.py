# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""A completed deletion fanout must not deadlock another public deletion."""

import concurrent.futures
import threading
import time
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


@pytest.mark.parametrize("purged_index", [0, 1])
@pytest.mark.parametrize("fanout_batch_size", [1, 128])
def test_public_delete_during_previous_message_purge_does_not_deadlock(
    api, db, conversation, purged_index, fanout_batch_size
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
        api.delete(f"{native.MESSAGES}{messages[purged_index]['uuid']}").status_code
        == 204
    )
    purging, release = threading.Event(), threading.Event()
    original = v2_projection._purge_completed_tombstone
    worker_pid = []

    def gated_purge(session, task):
        if task["payload"].get("source_kind") == "message.deleted":
            worker_pid.append(
                session.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            )
            purging.set()
            assert release.wait(10), "Tombstone purge was not released"
        return original(session, task)

    def project():
        for _ in range(20):
            with contexts.Context().session_manager() as session:
                processed = v2_projection.process_one_projection_task(
                    session,
                    "tombstone-lock-regression",
                    fanout_batch_size=fanout_batch_size,
                )
            if purging.is_set():
                return processed
        raise AssertionError("Deletion fanout did not reach its purge")

    with mock.patch.object(
        v2_projection, "_purge_completed_tombstone", side_effect=gated_purge
    ):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            worker = pool.submit(project)
            try:
                assert purging.wait(5), "Deletion fanout did not reach its purge"
                deleting = pool.submit(
                    api.delete, f"{native.MESSAGES}{messages[1 - purged_index]['uuid']}"
                )
                deadline = time.monotonic() + 5
                while True:
                    waiting = db.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                        "WHERE %s=ANY(pg_blocking_pids(pid)) AND wait_event_type='Lock')",
                        (worker_pid[0],),
                    ).fetchone()[0]
                    if waiting:
                        break
                    assert not deleting.done(), (
                        "Public delete finished before overlapping purge"
                    )
                    assert time.monotonic() < deadline, (
                        "Public delete did not wait on the worker"
                    )
                    time.sleep(0.002)
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
