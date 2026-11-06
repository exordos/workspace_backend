# Repeated Zulip topic rename acceptance

Measured on 2026-11-06 for issue #307, using Python 3.12 and PostgreSQL 18 on
the requested test host. This report concerns the candidate source running with
an isolated database, real HTTP/publication workers and a real Zulip provider.
The HTTP harness substitutes IAM authentication. An installed-platform upgrade,
browser UI acceptance and a production deployment are outside this result.

## Correctness

The regression selection produced ten failures on the original source, including
repeated A→B→C→D renames, close/reopen, precreated aliases, old move notices,
delayed batches and the notice/alias-deletion race. The corrected source passes
the targeted 242-test PostgreSQL incoming/outgoing and rename suite. The unit
suite passes 1,342 tests; the pinned Ruff and mypy checks pass.

The regression suite also preserves real-merge destination UUIDs/preferences,
default-topic identity, account deduplication, flags, bounded batch completion
and unrelated realtime progress. A notice split with 150 unaffected notices
commits 100 placements first and leaves its rename pending until completion.
A review regression with 151 destination notices also fails before its fix.
It now resumes bounded folding after restart or primary-account disablement,
preserving the history UUID. Late old-name human messages still trigger a real
merge, and an older human message beyond the first 101 destination rows cannot
be mistaken for a notice-only destination. Admission rejects provider-supplied
internal progress metadata. Each resumed batch rechecks destination history;
older human history arriving between batches switches to a real merge. A
separate reproduced race pauses an old-name personal event before its save:
topic selection now holds a row lock so it cannot overwrite a concurrent rename.

Live provider acceptance used the primary account in the authorized test channel.
Three original messages included an incoming marker, a quoted reply and an
outgoing HTTP message whose contents and author were read back from Zulip.
Seven transitions included repeated renames, completion, reopening and reuse of
an old name. All kept the original history topic UUID and message/placement IDs.
The actual registration snapshot supplied Notification Bot's provider identity.
The run processed 15 personal-topic events, nine message events and seven rename
events, and finished without pending or failed incoming work.

Provider PATCH to matching Workspace HTTP state took **0.286–0.359 seconds**.
The incoming marker reached the HTTP view in **0.208 seconds**. The outgoing
HTTP request took **0.024 seconds**, with confirmed provider delivery after
**0.188 seconds**. These are control samples, rather than a throughput SLO.

## Other operation timings

Before/after series alternated baseline/candidate/baseline/candidate on the same
isolated PostgreSQL cluster. Each operation has 80 retained observations per
revision after discarding five warm-up observations from each round. Source
identity was checked for every round. HTTP CRUD used a small native stream;
incoming timings used 10,000 loaded messages. Timings include HTTP response or
incoming application/publication as indicated, and exclude migration/setup time.

| Operation | Before p50, ms | After p50, ms | Before p95, ms | After p95, ms |
| --- | ---: | ---: | ---: | ---: |
| Send message (HTTP) | 6.68 | 6.95 | 9.05 | 9.55 |
| List streams (HTTP) | 1.71 | 1.88 | 2.39 | 2.57 |
| List topics (HTTP) | 1.72 | 1.84 | 2.15 | 2.44 |
| Read message page (HTTP) | 2.86 | 2.88 | 3.88 | 3.83 |
| Read message (HTTP) | 1.34 | 1.46 | 1.92 | 1.91 |
| Edit message (HTTP) | 4.17 | 4.30 | 5.44 | 5.33 |
| Star message (HTTP) | 4.46 | 4.63 | 6.07 | 6.42 |
| Idempotent read action (HTTP) | 3.78 | 3.98 | 5.13 | 5.17 |
| Add reaction (HTTP) | 5.02 | 5.18 | 8.38 | 8.26 |
| Delete message (HTTP) | 4.25 | 4.33 | 5.27 | 5.84 |
| Incoming, existing topic | 37.32 | 38.63 | 44.14 | 48.60 |
| Incoming, new topic | 44.20 | 46.98 | 54.24 | 59.14 |

The existing-topic incoming median increased **3.5% (1.32 ms)** and the new-topic
median **6.3% (2.77 ms)**. Native HTTP median changes ranged from **+1.0% to
+10.4%**, with a largest absolute increase of **0.28 ms** (send). The largest
native HTTP p95 increase was **0.57 ms** (delete); the largest relative p95
increase was **13.5% / 0.29 ms** for listing topics. These small absolute changes
show no material slowdown in this sample, but do not establish production
tail-latency guarantees. Native HTTP paths are unchanged by this fix;
run-to-run variation remains visible and no speedup is claimed.

The 10,000-message rename/close/reopen and durable-inbox-to-WebSocket tests retain
their one-second budgets and deliberately stale pre-import statistics. One
earlier baseline close sample reached 9.69 seconds; it is not used to claim a
stable speedup because the other baseline control samples were much faster.

## Reproduction

Run the standard development environment and the real PostgreSQL tests against
a dedicated database:

```bash
tox -e develop
WORKSPACE_REQUIRE_INTEGRATION_TESTS=1 \
WORKSPACE_TEST_DB_URL="$DEDICATED_TEST_DATABASE_URL" \
.tox/develop/bin/python -m pytest \
  workspace/tests/integration/test_zulip_topic_rename.py \
  workspace/tests/integration/test_zulip_topic_rename_races.py \
  workspace/tests/integration/test_zulip_incoming.py \
  workspace/tests/integration/test_zulip_outgoing.py
```

The provider run used a separate event queue, filtered to the owned test topics,
and the real incoming/outgoing operation handlers. It imported the provider's
cross-realm bot snapshot before consuming move notices. Credentials stayed in
process memory. The test queue and temporary personal-topic policies were
removed afterward; control messages remain in the authorized test channel.

Private endpoint identifiers, provider payloads and raw benchmark observations
are retained in the task's local acceptance artifacts rather than this report.
