# Incoming event dispatch under backlog

`SELECT_EVENT_SQL` selects the next bounded batch from the durable Zulip inbox.
Checking every pending event against every earlier event can delay the entire
batch for minutes before any handler starts, even while receivers keep appending
fresh events and the counter queue is empty.

The selector computes the earliest pending non-dependency event for each
project, provider and ordering key once. An event with a later ID on a shared
key remains blocked. Separate scope minima retain the two exact wildcard rules:
an earlier `['*']` event blocks every later event, and a `['*']` event waits for
every earlier non-dependency event in its scope.

Deferred retries and in-flight events remain blockers. Completed events and
`ensure_dependency` intentions do not block other events. Dependencies retain
priority, followed by `next_retry_at` and ID. Empty arrays, duplicate keys and
NULL array elements keep the existing overlap behavior. Full payloads are read
only for the selected batch. No schema, API, queue state or concurrency setting
changes are required.

## Verification

`workspace/tests/integration/test_zulip_incoming_dispatch.py` covers ordering,
wildcards, retries, in-flight exclusions, dependencies, independent scopes and
fresh-message API visibility while deferred backlog remains pending.

The performance regression seeds 20,000 pending events with production-style
indexes and checks the actual PostgreSQL plan. The previous selector exceeds
the two-second statement budget; the replacement must finish within that budget
and visit fewer than three table rows per pending event. This checks bounded
lookup work rather than relying only on elapsed time.

In a read-only production snapshot with approximately 80,000 pending events,
the candidate selected 16 events in 0.42 seconds. The existing dispatcher query
had already been running for more than six minutes. That original query had not
completed, so these observations are not a completed before/after benchmark or
a guarantee of full delivery latency. Provider requests, handler work, backlog
drain and client delivery require separate acceptance after rollout.

A later read-only sample of the final SQL, with about 96,600 pending events,
selected full 128-event batches in 0.553, 0.502 and 0.486 seconds. The existing
dispatcher query was observed at 378 seconds in that snapshot. Pending events
were retained; the candidate was not installed by these measurements.
