# Prepared unread and physical deletion validation

Use the full integration suite and the opt-in benchmark to verify the contract
in [physical deletion and prepared unread facts](messenger_physical_deletion.md).
Historical run reports belong in the CASSI test-run archive.

## Coverage and acceptance criteria

- `test_physical_message_deletion.py`: remove roots before worker delivery;
  snapshot read recipients; cascade access to unread facts; preserve all
  notification policies; reject deleted UUID reuse and stale provider replay;
  retain late send receipts and canonical messages placed in another scope.
- `test_history_membership_fence.py`: prepared full and personal-flag pages
  cannot restore an old or revoked membership generation.
- `test_physical_deletion_upgrade.py`: apply 0294 after 0293 with queued deletion
  progress and orphan facts; finish the remaining recipient snapshot after the
  root is gone.
- `test_sparse_counter_scope_plans.py`: generic empty-scope counts read at most
  1001 unread facts; empty summaries avoid history work; summary flags respect
  the requested project/users.
- `test_deleted_stream_folder.py` and `test_deleted_topic_preview.py`: publish
  updated folders and repair surviving previews after scope deletion.

The complete integration suite also covers creation, editing, paging, read and
read-up-to, independent flags, archive/notification preferences, membership,
folders, summaries, files, quotes, reactions, WebSocket events, provider retries,
404 handling, uncertain sends and create/delete ordering.

## Reproduction

Install the test extras and set `WORKSPACE_TEST_DB_URL` to a dedicated disposable
test database. The integration harness resets its public schema. Run:

```sh
WORKSPACE_REQUIRE_INTEGRATION_TESTS=1 .tox/develop/bin/python -m pytest workspace/tests/integration -q
.tox/develop/bin/python -m pytest workspace/tests/unit workspace/tests/functional -q
```

For independent CI-style shards, pass `--integration-shard INDEX 4` and give each
process its own test database. Do not run multiple shards on the same database.

## Performance measurement

```sh
.tox/develop/bin/python -m tools.benchmark_unreads --output unread-results.jsonl
```

The benchmark requires CREATE DATABASE permission. It creates a fresh database
and drops it in a `finally` block without clearing the maintenance database.
Only synthetic fixture construction disables user triggers; foreign keys remain
enabled and user triggers are restored before measurements.

The matrix covers small and large topics, all/none notification policies, missing
and positive mention keys, analyzed and stale statistics. It alternates previous
and current COUNT SQL on the same schema/data. Measure SQL alone separately from
recount/publication/HTTP, and measure deletion through complete recipient
publication as well as the initial HTTP response. Verify result counts and new
event epochs, not just elapsed time. Report p50/p95, warm-ups, sample counts and
cache/load conditions. A fast HTTP response does not establish completed fanout.

The harness supplies IAM identity and uses a provider HTTP simulator. Live IAM,
browser rendering and real-provider delivery need separate acceptance. Stop and
remove task-owned test processes/databases after collecting sanitized evidence.
