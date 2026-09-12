# Provider partial moves and missing-base recovery

A Zulip topic-only update can omit content and the original-message URL. The
backend retains only an omitted URL from an existing, matching Zulip identity:
account, realm, provider message ID, metadata provenance and source must agree.
An explicit URL, including null, keeps its normal validation semantics. Native
origins are preserved. For retained rows without a metadata realm, the stored
canonical message, its legacy placement and the source account must prove the
same realm and message identity before a link is retained. The SQL identity
guard is unchanged.

The bridge derives the same URL for creates, edits and moves without another
provider request. Old serialized events with a previously valid stored URL work
with the new backend; their payloads and digests do not need rewriting. A record
with neither a valid message source nor a valid URL remains invalid.

## Recovery contract

Both Provider API versions return `ProviderProblem.error` equal to
`provider_message_base_missing` with HTTP 422 when an authorized partial update
has valid supplied fields and a valid destination but no base message. The complete request transaction
rolls back. Invalid destinations, ownership failures and other validation
failures retain their existing outcomes. A bridge must isolate the failed item
before acting on this code; an atomic batch failure does not classify every
member of that batch.

The bridge records an intent in the transaction that terminally rejects the
partial update. It waits for all selected sources in the project/realm to finish
capture, for the current configuration and directory to be published, and for
all captured batches to finish import and reference synchronization. Local
message mappings alone do not prove that the backend has the message.

The import receipt records the actual canonical placement even when a local
mapping already exists. If that receipt already has the requested placement,
recovery completes without replay. Otherwise the bridge fetches a current full
snapshot. A new deterministic operation identity includes the rejected operation
and the snapshot fingerprint. It goes through the normal outbox with content,
source creation time and the provider's current edit revision. The original
terminal operation and its digest remain immutable.

Recovery snapshots carry `provider_metadata.missing_base_recovery: true` on the
wire. The backend checks the persistent history tombstone under its existing
provider identity lock and applies its existing lower-revision fence. An absent, unparseable or equal
incoming revision against a stored numeric revision is rejected: Zulip timestamps have second precision, so equality
cannot order competing live edits safely. This produces an explicit blocked
recovery instead of overwriting possibly newer state. Ordinary live update
semantics are unchanged.

Capture/publication work and pending live mutations take precedence. Account
and assignment authority, realm, current configuration, message journal and
mapping state are rechecked after provider I/O and before atomic enqueue. An
expired final lease rolls back both mapping writes and outbox insertion.
Deselection, account-generation changes, project changes or retired child
operations end the old intent. Provider absence without a tombstone is treated
as unavailable, not deletion. A missing destination mapping waits for normal
control synchronization. Transient source errors use bounded exponential
backoff; unrelated 422 errors are not retried.

## Persistence and retention

Bridge migration `0049-Persist-missing-message-base-recovery-intents-e0a3cd.py`
adds `zulip_missing_message_recovery`. Existing outbox/idempotency rows cannot
represent a separate recovery lease, post-import reference and child operation
set after their normal lifecycle finishes. The additive table supplies those
fields without changing terminal operations or adding a second delivery queue.

One row is retained per classified rejected operation, without a foreign key to
bulky outbox or journal rows. Pending and delivering rows survive restarts and
normal terminal cleanup. Child outcomes use the retained `operation_idempotency`
ledger, including after sent rows are pruned. Completed, tombstoned, superseded
and blocked intent rows are also retained in this version for inspection; there
is no automatic recovery-intent deletion. Plan storage by classified failures,
not total message traffic. A future retention policy must retain unfinished
intents and the idempotency information needed to avoid repeat work.

Historical generic `provider_api_http_422` rejections cannot be safely classified
retroactively. This change does not rewrite or blindly resubmit them. Assess any
remaining old cohort individually against current backend state and provider
identity, with separate authorization for operational reconciliation.

## Verification

Use synthetic data and a dedicated disposable PostgreSQL database. The backend
integration fixture resets the database schema. Never point these tests at a
service database. Prepare environments using `tox -e develop --notest` in each
repository. Set `WORKSPACE_TEST_DB_URL` and `WORKSPACE_BRIDGE_TEST_POSTGRES_DSN`
only to the two dedicated test databases, then run:

```bash
# Backend repository
.tox/develop/bin/pytest -q workspace/tests/unit \
  workspace/tests/integration/test_provider_partial_moves.py \
  workspace/tests/integration/test_history_import.py

# Bridge repository, including real PostgreSQL tests
.tox/develop/bin/pytest -o addopts='' -q
```

The new tests cover old omitted-link events, explicit URL/null, the real SQL
identity guard, topic/stream/project moves, recipients, immutable content and
creation time, duplicates and equivalent updates, transaction rollback, both
live/history orders, missing-base classification, publication gates, actual
reference convergence, fresh full snapshots, restart/cleanup, leases, tombstones,
newer/equal/unordered revisions, generation changes and live events arriving during fetch.
Identity/provenance and native-origin cases also run in the existing unit suite.
The optional `test_provider_bridge_wire.py` imports the sibling bridge source to
exercise converter output through the real Provider v2 consumer. Add that source
root to `PYTHONPATH` when running it; otherwise it is skipped explicitly.

For a negative control, remove only the backend omitted-link fallback in an
isolated test checkout and run
`test_partial_move_old_producer_passes_real_identity_guard_and_replay`. It must
fail in `messenger_v2_apply_legacy_provider_identity` with SQLSTATE 23514. Restore
the patch and rerun the test. Never remove a deployed hotfix for this check.

## Release and rollback

1. Obtain rollout authorization and verify backups, data retention and the exact
   currently deployed artifacts. Preserve the existing source-hotfix backup.
2. Release and update the backend first. Verify that the installed artifact
   includes the omitted-link fallback and recovery snapshot fences before any
   reimage/reinstall. Check old serialized events with the new backend.
3. Apply the additive bridge migration through its normal migration command and
   update the bridge. Verify new moves carry the original URL and only the typed
   missing-base outcome creates an intent. Do not modify old terminal payloads.
4. On an isolated Realm, verify authenticated API access, actual sign-in and
   WebSocket application `ready`, then run agreed move/edit canaries. User chat
   canaries require permission. Verify canonical identity, placement, content,
   creation time, recipients, counters and repeated delivery.
5. Observe new acknowledgements and the age of the pending live cohort. A healthy
   process or a large `sent` table is insufficient. Measure capture progress,
   publication/import progress and missing-base convergence separately.
6. Stop rollout for unexpected identity violations, unexplained 422 growth,
   stalled acknowledgements, lost intents, resurrection or content regression.
   Investigate blocked intents individually. Do not clear the queue or disable
   guards to improve the numbers.
7. For rollback, stop the new bridge recovery worker and roll back code only,
   retaining the new table, queue and idempotency ledger. Do not schema-downgrade.
   An older bridge does not process these intents and may not preserve them
   through projection-reset workflows; keep it paused unless that workflow has
   been assessed. Keep the backend fallback or reapply the verified hotfix before
   starting delivery. A plain reinstall of the old backend is unsafe.
8. Declare the source hotfix superseded only after verifying the deployed
   replacement. Report code, tests, deployment, live delivery, history capture,
   history publication and missing-base recovery separately. If history or
   canary work remains pending, leave that acceptance explicitly open.
