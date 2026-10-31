# History flag initialization

The bulk history transaction sets `workspace.zulip_history=on` before inserting
personal message bindings. Migration 0288 makes the binding initializer return
immediately for that transaction. History already prepares the final unread,
star and mention sets, merges them after access bindings, and queues every
affected counter scope in the same transaction.

This avoids inserting default unreads only to delete the read messages again,
re-reading canonical Markdown for mentions, and scheduling one provider flag
repair per imported binding. Existing pending provider repairs are preserved.
Pins, realtime initialization and native message initialization retain their
existing behavior. No cursor, scheduling, receipt format or table/index layout
changes are involved. The migration restores the previous function on downgrade.

## Measuring the merge

Create the development environment with `tox -e develop`. Set
`WORKSPACE_TEST_DB_URL` to a maintenance database with a role allowed to create
databases, then run:

```shell
.tox/develop/bin/python -m tools.benchmark_history_merge \
  --rows 0 100000 1000000 --messages 1000 --repeats 5 \
  --output /tmp/cassi-history-merge.json
```

The harness creates and drops its own database. It never migrates or clears the
maintenance database. All rows and provider identities are synthetic. Background
messages, placements and personal bindings grow together; their user triggers
are disabled only while seeding, outside the measured interval. Foreign keys
remain enabled, and each background table is analyzed before measurement.

The actual `Operations.merge_batch` runs with all normal constraints and triggers.
The baseline removes only the new initializer guard; the candidate restores it.
Each sample rolls back, so both variants see the same data. The first pair warms
caches, and subsequent pairs alternate order. Output includes every operation's
duration and row count, unnecessary repair counts, and a checksum of message,
content, placement, binding, imported flag and counter-scope projections.
Audit timestamps and queue revision tokens are excluded from that checksum.

The interval includes relation staging and SQL merge. It excludes fetching,
preparation, receipt context checks, commit and asynchronous counter processing.
This is a database benchmark, not a provider or UI acceptance test.

Two test machines running Python 3.14.4 produced these median milliseconds for a
1,000-message page, with five measured pairs at each size:

| PostgreSQL | Background rows per table | Before | After | Speedup |
| --- | ---: | ---: | ---: | ---: |
| 18.6 | 0 | 210.97 | 163.92 | 1.29x |
| 18.6 | 100,000 | 1,095.76 | 241.73 | 4.53x |
| 18.6 | 1,000,000 | 1,234.10 | 196.86 | 6.27x |
| 16.15 | 0 | 165.94 | 131.68 | 1.26x |
| 16.15 | 100,000 | 887.80 | 216.87 | 4.09x |
| 16.15 | 1,000,000 | 944.52 | 192.46 | 4.91x |

Projection checksums matched in every pair. The candidate creates no provider
flag repairs and deletes no temporary default unreads. Real imports also depend
on cache residency, the mix of shared canonical messages, content, files and
reactions, concurrent workers and storage latency. These ratios do not predict
the speedup of an entire production batch or guarantee constant database time.
After a separately authorized release, compare phase median/p95, completed-page
throughput, errors, realtime latency and operation-level timings under real load.
