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

"""Repeatable import/foreground-API benchmark in an explicitly disposable database.

Set WORKSPACE_TEST_DB_URL to an empty test database, then run with
--initialize-disposable-database. The integration harness replaces IAM and S3;
this measures SQL/conversion and API contention, not provider download speed.
"""

import argparse
import contextlib
import json
import os
import pathlib
import resource
import threading
import time
import uuid
import wsgiref.simple_server

import psycopg
import pytest
from restalchemy.common import contexts

from workspace.history_import import contract
from workspace.history_import import repository
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import v2_projection
from workspace.tests.integration import conftest
from workspace.tests.integration import test_history_import


def percentile(values, fraction):
    ordered = sorted(values)
    return (
        None
        if not ordered
        else ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=1000000)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument(
        "--initialize-disposable-database", action="store_true", required=True
    )
    args = parser.parse_args()
    if not os.environ.get("WORKSPACE_TEST_DB_URL"):
        parser.error("WORKSPACE_TEST_DB_URL must explicitly name a disposable database")
    initialized = conftest._database.__wrapped__()
    next(initialized)
    db = psycopg.connect(conftest.TEST_DB_URL, autocommit=True)
    api_store.configure_store_factory(store_factory.build_store_factory())
    server = wsgiref.simple_server.make_server(
        "127.0.0.1",
        0,
        conftest.build_test_wsgi_application(),
        handler_class=conftest._QuietHandler,
    )
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    api = conftest.ApiClient(
        f"http://127.0.0.1:{server.server_port}", uuid.uuid4(), uuid.uuid4()
    )
    stopped = threading.Event()
    reads, writes, failures = [], [], []
    with pytest.MonkeyPatch.context() as patch:
        fixture = test_history_import.history_setup.__wrapped__(None, db, api, patch)
        setup = next(fixture)
        native = api.post(
            "/v1/streams/",
            json={
                "name": "cassi foreground",
                "description": "",
                "source_name": "native",
                "source": {"kind": "native"},
            },
        )
        native.raise_for_status()
        native = native.json()

        def foreground():
            iteration = 0
            while not stopped.is_set():
                started = time.monotonic()
                try:
                    if iteration % 10 == 9:
                        response = api.post(
                            "/v1/messages/",
                            json={
                                "stream_uuid": native["uuid"],
                                "topic_uuid": native["default_topic_uuid"],
                                "payload": {
                                    "kind": "markdown",
                                    "content": "Synthetic foreground write",
                                },
                            },
                        )
                        writes.append(time.monotonic() - started)
                    else:
                        response = api.get(f"/v1/streams/{setup.stream}")
                        reads.append(time.monotonic() - started)
                    if response.status_code >= 400:
                        failures.append({"status": response.status_code})
                except Exception as error:
                    failures.append({"type": type(error).__name__})
                iteration += 1
                stopped.wait(0.1)

        def project():
            while not stopped.is_set():
                try:
                    with contexts.Context().session_manager() as session:
                        v2_projection.derive_projection_tasks(session, limit=20)
                        worked = v2_projection.process_one_projection_task(
                            session, "cassi-history-benchmark"
                        )
                except Exception as error:
                    failures.append({"projection_type": type(error).__name__})
                    worked = False
                stopped.wait(0.01 if worked else 0.1)

        threads = [
            threading.Thread(target=target, daemon=True)
            for target in (foreground, project)
        ]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        totals = dict(messages=0, retries=0, max_write_seconds=0.0, write_seconds=0.0)
        try:
            for offset in range(0, args.messages, 5000):
                body = test_history_import.envelope(
                    setup, count=min(5000, args.messages - offset)
                )
                body["batch"]["from_id"] += offset
                body["batch"]["to_id"] += offset
                for message in body["batch"]["messages"]:
                    message["id"] += offset
                    message["hash"] = contract.digest(
                        {k: v for k, v in message.items() if k != "hash"}
                    )
                body["batch"]["hash"] = contract.digest(
                    {k: v for k, v in body["batch"].items() if k != "hash"}
                )
                job_uuid = test_history_import.accept(setup, body)
                while True:
                    with setup.session_factory() as session:
                        job = repository.get_job(session, setup.identity, job_uuid)
                    if job["status"] == "complete":
                        break
                    if job["status"] in {"failed", "superseded"}:
                        raise RuntimeError(f"Import stopped: {job['safe_error']}")
                    if not setup.worker.run_once():
                        time.sleep(0.1)
                    else:
                        time.sleep(0.1)
                    if time.monotonic() - started > 7200:
                        raise TimeoutError("Benchmark exceeded its two-hour deadline")
                totals["messages"] += job["inserted_messages"]
                totals["retries"] += job["attempts"]
                totals["max_write_seconds"] = max(
                    totals["max_write_seconds"], job["max_write_seconds"]
                )
                totals["write_seconds"] += job["write_seconds"]
                setup.objects.clear()  # The storage fake must not accumulate the complete history.
                result = {
                    **totals,
                    "elapsed_seconds": time.monotonic() - started,
                    "rss_peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                    / 1024,
                    "read_requests": len(reads),
                    "read_p95_seconds": percentile(reads, 0.95),
                    "read_p99_seconds": percentile(reads, 0.99),
                    "read_max_seconds": max(reads, default=0),
                    "write_requests": len(writes),
                    "write_p95_seconds": percentile(writes, 0.95),
                    "write_p99_seconds": percentile(writes, 0.99),
                    "write_max_seconds": max(writes, default=0),
                    "failures": failures[:20],
                    "failure_count": len(failures),
                }
                args.output.write_text(json.dumps(result, indent=2))
                print(
                    json.dumps(
                        {
                            k: result[k]
                            for k in [
                                "messages",
                                "elapsed_seconds",
                                "max_write_seconds",
                                "read_p95_seconds",
                                "failure_count",
                            ]
                        }
                    ),
                    flush=True,
                )
        finally:
            stopped.set()
            for thread in threads:
                thread.join(timeout=15)
            with contextlib.suppress(StopIteration):
                next(fixture)
    server.shutdown()
    http_thread.join()
    db.close()
    with contextlib.suppress(StopIteration):
        next(initialized)


if __name__ == "__main__":
    main()
