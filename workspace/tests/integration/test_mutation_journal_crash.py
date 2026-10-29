# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Kill the owning process after a read commits and before the counter worker runs."""

import os
import select
import subprocess
import sys

from workspace.services.messenger_workers import mutation_runtime
from workspace.tests.integration import test_mutation_journal as journal_tests
from workspace.tests.integration import test_messenger_v2 as native

conversation = journal_tests.conversation

CHILD = r"""
import time
from workspace.cmd import messenger_worker as cmd
from workspace.common import config,database
from restalchemy.common import contexts
from workspace.services.messenger_workers import mutation_runtime
config.parse([])
cmd.CONF.set_override('connection_url',__import__('os').environ['WORKSPACE_TEST_DB_URL'],group='db')
cmd.CONF.set_override('enabled',True,group='messenger_mutations')
database.configure_postgresql('cassi-crash-regression')
runtime=mutation_runtime.MutationRuntime()
for _ in range(10000):
 runtime.tick()
 if runtime._dispatcher.idle: break
 time.sleep(.002)
else: raise RuntimeError('Startup did not become idle')
original=runtime._dispatcher._execute
# Pool captures this callable when submitting work, so wrap the dispatcher.
def execute(work):
 result=original(work)
 if work.key[0]=='command':
  print('committed',flush=True)
  time.sleep(60)
 return result
runtime._dispatcher._execute=execute
print('ready',flush=True)
while True:
 runtime.tick()
 time.sleep(.002)
"""


def test_process_death_after_read_commit_keeps_durable_counter_job(
    api, db, conversation
):
    stream, messages, peer = conversation
    first = mutation_runtime.MutationRuntime()
    try:
        journal_tests.drain(first, db)
    finally:
        first.close()
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def received(expected):
        assert select.select([process.stdout], [], [], 15)[0], "Child readiness timeout"
        assert process.stdout.readline().strip() == expected

    try:
        received("ready")
        api._user_seeder = None
        response = api.post(
            f"{native.MESSAGES}{messages[-1]['uuid']}/actions/read_up_to/invoke",
            user=peer,
        )
        assert response.status_code == 200
        received("committed")
        process.kill()
        assert process.wait(timeout=5) < 0
        assert (
            db.execute(
                "SELECT status FROM messenger_mutation_commands WHERE request_uuid=%s",
                (response.headers["X-Workspace-Mutation-Id"],),
            ).fetchone()[0]
            == "applied"
        )
        assert (
            db.execute(
                "SELECT unread_count FROM messenger_user_topic_bindings WHERE project_id=%s AND topic_uuid=%s AND user_uuid=%s",
                (api.project_id, stream["default_topic_uuid"], peer),
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_user_message_unreads WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
                (api.project_id, peer, stream["default_topic_uuid"]),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM messenger_counter_updates WHERE project_id=%s AND user_uuid=%s AND topic_uuid=%s",
                (api.project_id, peer, stream["default_topic_uuid"]),
            ).fetchone()[0]
            == 1
        )
        # Both cached scopes wait for the separate counter process. Its durable
        # invalidation committed with the read and survives the killed executor.
        assert (
            db.execute(
                "SELECT unread_count FROM messenger_stream_bindings WHERE project_id=%s AND stream_uuid=%s AND user_uuid=%s",
                (api.project_id, stream["uuid"], peer),
            ).fetchone()[0]
            == 2
        )
        restarted = mutation_runtime.MutationRuntime()
        try:
            journal_tests.drain(restarted, db)
            assert (
                api.get(f"{native.STREAMS}{stream['uuid']}", user=peer).json()[
                    "unread_count"
                ]
                == 0
            )
            assert (
                db.execute(
                    "SELECT unread_count FROM messenger_project_users WHERE project_id=%s AND user_uuid=%s",
                    (api.project_id, peer),
                ).fetchone()[0]
                == 0
            )
        finally:
            restarted.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
