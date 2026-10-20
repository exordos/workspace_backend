# Zulip realtime delivery validation

The fix prevents a prepared outbound send from being abandoned when its second
lock acquisition encounters contention, shortens realtime database-deadlock
retries, and avoids publishing historical message creations to the live journal.
It is based on `bdc56e878ba7e1be2afc6eb910d2129547baf9ba` (`v5`).

## Controlled before/after results

These are local PostgreSQL integration checks with synthetic messages and mocked
Zulip HTTP. They measure behavior and event volume, not browser latency or
production throughput.

| Scenario | Before | After |
| --- | --- | --- |
| Account row becomes busy after committing the send marker | No POST; subsequent attempts wait for an echo indefinitely | Wait for the lock, then issue one POST |
| Task is cancelled while that account lock is held | No POST | No POST |
| Realtime import rolls back after a database deadlock | Retry scheduled 60 seconds later | Retry scheduled 1 second later |
| Background import rolls back after a database deadlock | Retry scheduled 60 seconds later | Retry scheduled 60 seconds later |
| Import 100 messages created 30 days ago | 100 message creation events; 100 REST messages | 0 message creation events; 100 REST messages |
| Import 100 fresh messages | 100 message creation events; 100 REST messages | 100 message creation events; 100 REST messages |

The controlled historical batch removes 100% of its message creation events.
The realtime retry removes 59 seconds of mandatory waiting per deadlock; it does
not remove the underlying database contention. Fresh creation events and later
updates/deletions of old messages retain their behavior. Additional checks cover
messages aged 23 hours, 25 hours, 30 days, and 180 days, unchanged observations,
recipient access, membership-generation fencing, and event-write rollback.

## Reproducing the regression comparison

Use the regression tests from this change in both a checkout of the base above
and a checkout of the fix. Run the checkouts sequentially: the integration
harness recreates the dedicated local `workspace_test` database. The module
search path and current directory must both point to the checkout under test.

```bash
python -m pytest -q -s \
  workspace/tests/integration/test_zulip_task_workers.py \
  workspace/tests/integration/test_zulip_message_states.py \
  -k 'send_waits_for_account_contention_after_prepare or history_import_retries_database_deadlock_after_rolling_back_partial_writes or historical_creation_is_rest_only or history_batch_keeps_rest_messages' \
  --tb=short --timer-top-n=3
```

The base produces **14 failed / 13 passed** checks; the fix produces
**27 passed** checks. The failures separately reproduce the skipped send,
minute-long realtime retry, and historical creation flood.

## Validation

- Full unit suite: **1123 passed**.
- All Zulip integration modules: **562 passed, 1 skipped**. The skipped check is
  the existing opt-in database/worker benchmark; two existing aiohttp
  deprecation warnings remain.
- Ruff check, changed-file formatting, and `git diff --check`: passed.
- Documentation build: passed with Zensical 0.0.57, the version in `uv.lock`.
- Mypy: **76 existing errors** on both base and fix, with identical diagnostics
  after normalizing source line numbers; no additional type errors.

The integration coverage includes incoming events, outgoing sends and edits,
lost-response echo recovery, deletes, read/starred flags, reactions, access,
counters, content conversion, queue ordering, task claims, and migration
round-trips. It exercises the durable event journal through REST; no real
browser or production WebSocket latency measurement is claimed.

## Operational limits

- The creation cutoff is the original message timestamp, 24 hours before
  reconciliation. Older message bodies, access, flags, counters, and previews
  continue to materialize. Their history is retrieved through REST.
- The post-prepare send fence now waits on account, task, and scope locks. A long
  database transaction can still delay that worker. Cancellation and lease-owner
  checks remain in place before executing the send.
- A lost response to an actual POST still waits for the Zulip echo; it is not
  blindly reposted. Existing ambiguous `sending` rows require provider-side
  verification and explicit recovery rather than an automatic resend.
- This patch does not delete already queued journal events or reset client
  cursors. Existing backlog must still be consumed, expire through retention,
  or be handled by an explicit client recovery procedure.
- No database migration, production deployment, service restart, or resend was
  performed during validation.
