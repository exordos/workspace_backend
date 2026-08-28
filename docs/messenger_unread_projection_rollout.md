# Messenger unread projection upgrade and rollback

This runbook applies the query-plan correction introduced by migration
`0149-split-messenger-unread-read-state-branches-c84ae9.py`. The migration only
replaces view definitions; it does not rewrite message or read-state data.

Production execution requires a separate change approval. Complete the full
procedure first on an isolated environment restored from a representative
database backup.

## Access-validation audit

The bounded stream-access helper reads only the stream, its user binding, and
the canonical external-access projection. It is used for validations that do
not consume unread counters:

- message target validation;
- topic creation and topic-notification validation;
- initial stream update and stream-notification validation;
- folder-item creation;
- provider topic-notification validation.

Message listing already scopes through the canonical user-message projection
and does not call the stream snapshot helper. File access uses the file ACL and
access projection. Reaction routes intentionally keep the full visible-message
snapshot because their response, event, and provider payload consume message
state. Full stream snapshots also remain in read actions, binding event fan-out,
stream deletion, and post-mutation response/event paths where counters or
public fields are required.

## Preconditions

1. Confirm the deployed backend contains migration `0149` and that `0148` is
   the only applied parent.
2. Save `pg_get_viewdef()` output for these views to an operator-controlled
   secure location:
   - `m_workspace_user_unread_messages_base_v1`
   - `m_workspace_user_topic_unread_counts_v1`
   - `m_unread_user_messages`
   - `m_workspace_user_streams`
   - `m_workspace_user_topics_view`
   - `m_folders_view`
3. Record only aggregate read-state mode counts:

   ```sql
   SELECT COALESCE(mode, 'legacy') AS mode, COUNT(*)
   FROM m_workspace_read_state_projects_v1
   GROUP BY COALESCE(mode, 'legacy')
   ORDER BY mode;
   ```

4. Check the PostgreSQL connection budget. Two Messenger API workers with a
   per-process pool maximum of two require at most four Messenger API
   connections. Count all other service pools, active maintenance sessions,
   and reserved connections before enabling the second worker.
5. Capture the current 499/504 rate, PostgreSQL wait events, temporary-file
   counters, and p50/p95 for exact stream lookup, stream collection, and
   provider batch apply and commit durations.
6. Confirm the rollback command and the saved view definitions are available
   before quiescing services.

## Upgrade

1. Quiesce Messenger API traffic and provider delivery. Keep PostgreSQL
   available and prevent another migration runner from starting.
2. Apply only the new migration with bounded DDL waits:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-apply-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

   A timeout is a failed rollout, not a reason to remove the bounds or wait
   indefinitely. Investigate the blocking transaction and start again from the
   precondition checks.
3. Confirm the migration row is applied and the six dependent views can be
   selected with `LIMIT 0`.
4. Start Messenger API and provider delivery. Confirm that PostgreSQL sessions
   use distinct `application_name` values for Messenger API and provider
   control.
5. Perform authenticated read-only checks for server settings, stream list,
   exact stream lookup, and message list. Do not create messages or mutate read
   state during this acceptance pass.
6. Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for sanitized representative
   exact-stream and stream-collection queries:
   - the legacy branch uses
     `m_workspace_unread_flags_user_message_idx`;
   - exact access validation does not visit `m_workspace_messages`;
   - exact lookup writes no temporary blocks.
7. Compare the post-upgrade 499/504 rate, wait events, temporary-file delta,
   exact lookup p50/p95, stream collection p50/p95, and provider batch apply
   and commit durations with the baseline.

## Rollback

Rollback changes only migration `0149`; do not roll back the backend release or
enable read-state compaction as an incident workaround.

1. Quiesce Messenger API traffic and provider delivery again.
2. Bound DDL waits and downgrade only migration `0149`:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-rollback-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

3. Compare the restored base and topic-count definitions with the saved
   pre-upgrade definitions. The downgrade restores the 0.1.44 mixed-mode
   definitions and does not change read-state data.
4. Start services and repeat the same read-only health checks and aggregate
   telemetry comparison.

## Local performance check

Run the transactional benchmark only against a disposable database whose name
contains `test`. It replaces views and inserts fixture rows inside a transaction
that is rolled back before exit:

```bash
WORKSPACE_TEST_DB_URL='<disposable-database-url>' \
  .tox/develop/bin/python \
    workspace/tests/scale/benchmark_unread_projection.py \
    --messages 250000 --unread 100
```

The JSON output omits query predicates and fixture identifiers. Preserve the
sanitized output in the CASSI test-run archive, not in this service repository.
