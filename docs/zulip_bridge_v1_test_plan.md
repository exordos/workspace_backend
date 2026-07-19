# Zulip Bridge V1 Test Plan

This plan is the executable acceptance gate for the provider-neutral Workspace
external-account boundary and its first `zulip` implementation. It supplements
the native Messenger regression plan; native messaging, IAM, realtime, and S3
behavior must remain green throughout the rollout.

Canonical Messenger resources and provider commands live in PostgreSQL and are
exchanged through the private Workspace Provider API. This plan must be run
with the recovery and load gates in
[`messenger_regression_test_plan.md`](messenger_regression_test_plan.md).

## Required environment

- Workspace backend updated in place with its canonical PostgreSQL and S3 data
  preserved.
- One independently updatable `workspace-zulip-bridge` node with a replaceable
  root disk and a persistent operational-data disk.
- A dedicated Zulip test realm with at least four users, one channel with two
  topics, a personal DM, a group DM, and files suitable for both directions.
- S3-compatible Workspace storage and the private Workspace Provider API.
- The normal `cassi` Workspace account in a visible Playwright MCP window.
- All nine external-provider IAM permission resources reconciled from the
  Workspace element manifest, with a dedicated least-privileged role bound to
  `cassi` only in the Workspace test project. Ordinary Workspace roles must not
  receive these permissions implicitly.

## Deterministic visible-UI fixture

Create every fixture with a unique run suffix and record its generated UUIDs in
the external test-run archive, not in this repository. Keep both Workspace and
Zulip open in separate visible Playwright MCP tabs for the whole journey. The
primary Workspace session is always the real `cassi` account; disposable users
exist only to exercise multi-user and destructive lifecycle cases.

### Accounts and projects

| Fixture                                   | Required state                                                                                                 | Purpose                                                                     |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Workspace `cassi`                         | Active realm administrator with the external-provider policy permissions and membership in both test projects. | Primary owner, ordinary Workspace UI session, policy/health acceptance.     |
| Workspace peer A and peer B               | Active ordinary users in both test projects.                                                                   | Native-messaging regression and participant/unread checks.                  |
| Workspace lifecycle owner                 | Active ordinary user with a separate Zulip account fixture.                                                    | IAM deactivate/delete tests without destroying the primary `cassi` session. |
| Zulip owner for `cassi`                   | Active human account with an API key.                                                                          | Personal external account used for the main two-way journey.                |
| Zulip peer A, peer B, and lifecycle owner | Active human accounts.                                                                                         | Personal DM, group DM, mention, rename, and account-lifecycle fixtures.     |
| Workspace project A and project B         | Both visible to `cassi`; neither contains pre-existing external projections.                                   | Initial assignment and atomic project-move acceptance.                      |

Do not reuse a Zulip API key between Workspace owners. Store generated
credentials only in the approved secret store and remove or rotate disposable
keys after the run.

### Zulip conversation and content matrix

| Fixture            | Required content before connection                                                                                              | Purpose                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Channel A          | Topics `live` and `history`; messages newer than 7 days, between 7 and 30 days, between 30 and 90 days, and older than 90 days. | Explicit selection, every history-depth boundary, newest-first ordering, channel/topic projection.                |
| Channel B          | At least one topic and no Workspace projection when the account is first connected.                                             | Verify explicit exclusion, then selection and project assignment.                                                 |
| Channel C          | Created only after `selection_mode=all` is live.                                                                                | Verify dynamic discovery and automatic assignment to the default project.                                         |
| Personal DM        | Zulip owner and peer A, with at least one message from each side.                                                               | Private personal stream with exactly two participants and one default topic.                                      |
| Group DM           | Zulip owner, peer A, and peer B, with at least one message from each participant.                                               | Private group stream with one default topic and stable external identities.                                       |
| Incoming files     | One small PNG and one non-image file in Channel B with known names, sizes, MIME types, and hashes.                              | Provider-to-Workspace copy, image rendering, generic download, URN-only message content, and deselection cleanup. |
| Outgoing files     | One small PNG and one non-image file selected through the Workspace composer.                                                   | Workspace-to-provider copy and provider-side byte/content verification.                                           |
| Loss-aware message | Markdown containing one supported element and one provider-unsupported element covered by preflight.                            | Safe fallback, `Open original`, loss summary, cancel, and explicit confirmation.                                  |

Keep Channel A otherwise quiet during history-depth assertions. Create live
traffic only after its expected backfill set and ordering have been captured,
so a new message cannot make a boundary assertion ambiguous.

## Contract and security gates

| ID              | Scenario                                                                                                                                                                          | Expected result                                                                                                                                              |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ZB-CONTRACT-001 | Validate the public, Provider API, control, and file contracts and resolve every local OpenAPI reference.                                                                                 | All artifacts validate; the public API remains OpenAPI 3.0.3 and every private contract resolves without exposing an internal route to the browser.                                                   |
| ZB-SEC-001      | Bootstrap control trust with a fresh nonce, expected hostname, bridge instance, and enrollment generation.                                                                        | Only the exact HMAC-authenticated CA is installed atomically; redirects, replay, mismatch, invalid PEM, oversized data, and a closed generation fail closed. |
| ZB-SEC-002      | Enroll, renew, suspend, resume, and revoke the bridge identity.                                                                                                                   | Client keys never leave the bridge disk; every private request is certificate- and generation-bound; revocation is immediate.                                |
| ZB-SEC-003      | Create and reconnect an external account, then inspect API responses, logs, events, browser storage, and operational tables.                                                      | Zulip API keys remain write-only and encrypted; plaintext appears only in the bridge process while calling Zulip.                                            |
| ZB-SEC-004      | Attempt cross-owner, cross-project, unassigned-chat, anonymous, and global-object-store access.                                                                                   | Every request is rejected without leaking account data, object keys, credentials, or provider payloads.                                                      |
| ZB-SEC-005      | Encrypt a credential to the enrolled bridge X25519 key, then alter the owner, realm, provider, account, bridge instance, identity generation, key UUID, schema, or recipient key. | The backend has no decrypt capability; only the exact enrolled bridge identity opens the HPKE envelope and every altered binding fails closed.               |

## Account, catalog, and control plane

| ID             | Scenario                                                                                                                                    | Expected result                                                                                                                                                                                                                                |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ZB-ACCOUNT-001 | Create, reconnect, disconnect, and delete a Zulip account.                                                                                  | The owner receives sanitized state; a second account of the same provider is rejected; disconnect retains a read-only projection and delete purges it.                                                                                         |
| ZB-ACCOUNT-002 | Deactivate and delete the IAM owner.                                                                                                        | Deactivation stops sync without destructive loss; deletion removes the account, projection, queue work, and copied files according to the approved lifecycle.                                                                                  |
| ZB-CATALOG-001 | Select explicit chats and then select `all`.                                                                                                | Explicit selection imports only chosen chats; `all` also assigns new provider chats discovered later.                                                                                                                                          |
| ZB-CATALOG-002 | Exercise every history-depth mode.                                                                                                          | Backfill proceeds newest-first to the selected boundary while live work retains strict priority.                                                                                                                                               |
| ZB-CATALOG-003 | On a fresh PostgreSQL schema, select a chat, reconcile its backfill job, invalidate the Zulip event queue, and create queue-catchup jobs.   | JSON-extracted account UUIDs are cast to the PostgreSQL UUID type in both paths; ordinary backfill and queue-loss recovery create their durable jobs without a datatype error.                                                                 |
| ZB-CATALOG-004 | Complete backfill, deselect the chat, then reselect the same chat with the same history depth under a newer assignment generation.          | The cancelled job restarts as pending, reimport operation identities are generation-scoped, and prior deduplication cannot suppress the new import.                                                                                            |
| ZB-CATALOG-005 | Report personal-direct catalogs with participant counts other than two and group-direct catalogs with fewer than three participants.        | Backend rejects the topology before persisting either the chat or desired assignment. Exactly two personal-direct and at least three group-direct participants remain valid.                                                                   |
| ZB-CONTROL-001 | Poll desired changes, report observed state in partial-result batches, expire a cursor, and recover through a logical snapshot.             | Cursors are monotonic and scope-bound; typed `410` triggers a consistent snapshot and no desired change is skipped.                                                                                                                            |
| ZB-CONTROL-002 | Change capabilities and revisions across heartbeat cycles.                                                                                  | Only the fail-closed effective intersection is enabled; incompatible batches do not advance the cursor and recover automatically after a compatible heartbeat.                                                                                 |
| ZB-CONTROL-003 | Prune one resource scope while leaving unrelated identity and resource-type sequences sparse, including a scope with no later retained row. | Per-scope pruned-through watermarks return typed `410` only for an actually expired cursor; unrelated sequence gaps never force a snapshot.                                                                                                    |
| ZB-CONTROL-004 | Mutate a public external account/chat and poll the private desired feed in the same transaction boundary.                                   | The private feed and snapshot read the PostgreSQL source of truth, expose committed full replacements or tombstones, and never depend on node-local JSON state.                                                                                |
| ZB-CONTROL-005 | Observe idle desired polling, then inject network failure, HTTP `429`, and retryable `5xx` responses.                                       | Exactly one poll is outstanding; healthy polls wait two seconds; retries use one-second-base exponential backoff with full jitter up to 30 seconds, honor bounded `Retry-After`, keep the committed cursor unchanged, and reset after success. |

## Message, identity, and file plane

| ID          | Scenario                                                                                                                                                                                                            | Expected result                                                                                                                                                                                                                                                                                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ZB-MSG-001  | Import Zulip channels/topics, personal DMs, and group DMs.                                                                                                                                                          | They map to ordinary Workspace streams/topics with stable provider metadata and the approved privacy/member rules.                                                                                                                                                                                                                                                    |
| ZB-MSG-002  | Create, edit, delete, reply, quote, mention, and mark read in both directions.                                                                                                                                      | Supported operations converge once, preserve stable IDs, use `urn:user:<uuid>` for Workspace mentions, and expose provider loss/capability state.                                                                                                                                                                                                                     |
| ZB-MSG-003  | Lose the Zulip send response while the matching local-echo event remains available.                                                                                                                                 | Persisted queue/local correlation confirms the provider message ID; the operation is delivered once and is never resent.                                                                                                                                                                                                                                              |
| ZB-MSG-004  | Interleave dependent and independent operations.                                                                                                                                                                    | One causal lane remains serial while independent chats progress concurrently; the last confirmed operation wins.                                                                                                                                                                                                                                                      |
| ZB-MSG-005  | Lose both the Zulip send response and local-echo event, then expose one or several exact matching messages through history.                                                                                         | Delayed, repeated history reconciliation is scoped to the exact conversation and current sender, compares raw canonical payload and time, selects the candidate nearest to the first send attempt with lowest numeric ID as tie-breaker, records the candidate count, and does not resend. Identical user operations may intentionally converge to one Zulip message. |
| ZB-MSG-006  | Return no matching history message after an ambiguous Zulip send.                                                                                                                                                   | The bridge resends automatically at most once; a confirmed second attempt converges to one provider message and preserves reconciliation evidence.                                                                                                                                                                                                                    |
| ZB-MSG-007  | Make history unavailable or lose the result of the one permitted automatic resend without any later exact match.                                                                                                    | The operation becomes `manual_reconciliation_required`; no further automatic resend occurs, and UI shows sanitized context, original link when known, and an explicit duplicate-risk warning before manual retry.                                                                                                                                                     |
| ZB-MSG-008  | Select a chat with `history_depth=new`, before any inbound history or mapping has arrived, then send the first Workspace message and subsequently edit, reply to, and delete it.                                    | Desired state already contains the complete backend-issued `workspace_projection`; the first outbound operation routes through the persisted stream/topic/participant mapping, and every dependent operation reuses the same mapping without the bridge inventing a Workspace UUID.                                                                                   |
| ZB-MSG-009  | Receive the first provider message from an author and topic absent from the current assignment generation.                                                                                                          | The provider event remains pending while the bridge reports the catalog delta; delivery resumes only after the backend publishes exact participant/topic UUID mappings in a newer assignment generation.                                                                                                                                                              |
| ZB-MSG-010  | Rename a mapped topic and update stream name, description, or privacy in each direction, then restart and replay a desired snapshot.                                                                                | The backend advances a full-replacement assignment while preserving Workspace UUIDs; provider IDs and current metadata remain authoritative and replay never restores old values.                                                                                                                                                                                     |
| ZB-MSG-011  | Deliver a provider-global `realm_user` identity event without a chat assignment.                                                                                                                                    | The operation uses the account-generation-bound outbox and completes without requiring a synthetic `account` chat assignment or aborting the provider-journal tick.                                                                                                                                                                                                   |
| ZB-MSG-012  | Give same-topic messages the same `created_at`, map them so Workspace UUID order is the opposite of Zulip numeric message-ID order, and invoke Workspace `read_up_to` at the middle Workspace UUID.                 | Workspace resolves the native `(created_at, uuid)` prefix and serializes it as a non-empty exact `message_uuids` selector. Canonical PostgreSQL state and Zulip mark exactly that set; the provider never reinterprets a Workspace UUID boundary using provider ordering, and the later Workspace UUID remains unread everywhere.                                                   |
| ZB-MSG-013  | Mark an empty external stream or topic read.                                                                                                                                                                        | The action is a local no-op and emits no provider command; an empty `message_uuids` exact selector is never serialized or quarantined.                                                                                                                                                                                                                           |
| ZB-MSG-014  | Commit a Workspace-origin message, lose the provider queue/local echo, then rediscover the same provider message through history catch-up.                                                                          | The existing provider-to-Workspace alias is reused or suppresses the replay; no second Workspace UUID/message is created, while an incompletely delivered provider-origin projection remains recoverable.                                                                                                                                                             |
| ZB-MSG-015  | Queue an exact read selector containing mapped messages followed by a Workspace-origin message whose earlier create terminated without a provider mapping.                                                          | The bridge applies the mapped provider IDs and omits only the terminal unmapped UUID. A read behind a still-pending create remains unclaimable in the same causal lane; an entirely unmapped selector is a provider no-op.                                                                                                                                            |
| ZB-MSG-016  | Invoke external stream/read, topic/read, message/read, and message/read_up_to with Workspace UUID order deliberately opposite to numeric provider message-ID order; also repeat stream/topic read with no messages. | Every non-empty action serializes the exact deterministic Workspace-selected UUID set and Zulip updates only the corresponding provider IDs without boundary reinterpretation. Empty stream/topic actions emit no provider command.                                                                                                                                     |
| ZB-FILE-001 | Transfer images and generic files in both directions.                                                                                                                                                               | Bytes are copied to receiving storage; Workspace messages contain only URNs; presigned URLs are single-object and expire within five minutes.                                                                                                                                                                                                                         |
| ZB-FILE-002 | Tamper with size, MIME type, hash, allocation generation, sidecar, and current ACL.                                                                                                                                 | Finalize fails closed, partial objects are cleaned, and the bridge cannot create sidecars or obtain bucket-wide credentials.                                                                                                                                                                                                                                          |
| ZB-FILE-003 | Request an outgoing file from a different stream in the same project, or with a sidecar ACL that does not bind the projected stream/account/chat.                                                                   | Authorization fails closed; project membership alone never grants access to another stream's object.                                                                                                                                                                                                                                                                  |
| ZB-FILE-004 | Crash after each incoming-file finalize phase: binary commit, sidecar commit, canonical file projection, and final status.                                                                                          | Persisted phases resume idempotently or roll back safely; a mismatch invalidates and removes partial state, and no URN becomes visible before the canonical record is durable.                                                                                                                                                                                        |

## Reliability, UI, and deployment

| ID            | Scenario                                                                                                                                                             | Expected result                                                                                                                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ZB-REL-001    | Hard-stop the bridge during live work, backfill, and result publication, then start it with the same persistent disk.                                                | Durable queues, cursors, deduplication, mappings, and provider operations recover without duplicates or loss.                                                                                    |
| ZB-REL-002    | Keep the provider unavailable beyond retry intervals and the 24-hour operation deadline.                                                                             | Exponential backoff uses full jitter; expired work becomes actionable and can be retried or discarded explicitly.                                                                                |
| ZB-REL-003    | Run the mandatory PostgreSQL full profile with 300 fixture users, 150 simultaneous independent Zulip accounts, 120 streams, 100,000 messages, steady 100-200 provider messages/minute, and the defined burst. | Live messages achieve p95 at or below five seconds, native latency regresses no more than 20%, no account state crosses owners, and live work is not starved by retry or backfill. |
| ZB-REL-004    | Deliver an invalid Provider API route, scope, schema, or payload immediately before a valid operation, then restart the consumer.                                         | The invalid operation is durably quarantined with bounded diagnostics and raw hash, the cursor advances atomically, no reflected result is emitted, and the valid operation is processed exactly once. |
| ZB-REL-005    | Crash a worker before and after persisting the first provider send attempt, let its running lease expire, then restart competing workers.                            | The reaper returns an unattempted item to pending and an attempted item to uncertain reconciliation, preserves first-attempt evidence, and prevents concurrent claims.                           |
| ZB-REL-006    | Expire or invalidate the Zulip event queue while create/edit/delete events are outstanding, then re-register it.                                                     | A bounded newest-first catch-up resumes from the last mapped checkpoint before live polling, overlaps idempotently, and suppresses notifications until catch-up is complete.                     |
| ZB-REL-007    | Crash after the Provider API accepts an assignment-scoped inbound operation but before the provider records final state, then move or deselect the assignment before restart. | The possibly submitted operation remains durable and reconcilable, a late backend result is accepted, and the provider event is not replayed automatically under another assignment generation.  |
| ZB-REL-008    | Allocate a causal sequence and crash before durable outbox enqueue, then enqueue the next operation in the same lane.                                                | Sequence allocation and enqueue are one PostgreSQL transaction; rollback leaves no gap and the next committed operation never references a missing predecessor.                                  |
| ZB-REL-009    | Reject causal sequence 1, commit sequence 2, then explicitly authorize a higher manual attempt for sequence 1.                                                       | The authorized attempt is claimable despite the later lane position, remains constrained by current generation and delete-wins, and cannot stay pending forever.                                 |
| ZB-REL-010    | Crash after persisting `submitting` but before the Provider API accepts an immutable provider-to-Workspace operation, then restart without any reciprocal result.                     | The ambiguous operation is retried or reconciled idempotently until a reciprocal result is observed; it cannot remain stuck, and current assignment-generation checks still apply.                  |
| ZB-UI-001     | Connect the account, select chats/depth, inspect badges/popovers, disconnect, reconnect, and delete through visible Playwright.                                      | The normal Messenger UI reflects authoritative state, never exposes a credential, and does not mount mail/calendar surfaces.                                                                     |
| ZB-UI-002     | Reload, reconnect, and replay retained events.                                                                                                                       | Cached entities render first, event catch-up converges them, and historical synchronization does not emit desktop notifications before the ready gate.                                           |
| ZB-UI-003     | Delay IndexedDB snapshot loading, publish a newer realtime external-account update first, then resolve the stale cache while REST is delayed or unavailable.         | The realtime generation remains visible and is never overwritten or re-persisted by the older cache snapshot; later REST only converges forward and active form edits remain intact.             |
| ZB-DEPLOY-001 | Stage backend and bridge with Zulip disabled, enroll, verify heartbeat/capabilities, then enable the provider.                                                       | Native Workspace remains available throughout and Zulip activates only after every gate is healthy.                                                                                              |
| ZB-DEPLOY-002 | Replace backend, UI, and provider root images independently.                                                                                                     | Canonical PostgreSQL, S3, provider operational data, and provider identity survive without reinstall or destructive cleanup.                                                   |
| ZB-DEPLOY-003 | Roll back a failed bridge update.                                                                                                                                    | Sync is suspended, durable work and projections remain intact, and native Workspace messaging is unaffected.                                                                                     |

## Reusable visible UI journey

Run this journey in the global Playwright MCP with a visible Workspace window
under the real `cassi` account. API calls may be inspected for diagnostics, but
they do not replace the UI actions or assertions. Keep the window open through
the provider-side actions so realtime, focus, unread, notification, and browser
console behavior are observed continuously.

1. Open personal settings and locate `zulip-external-account-card`. Open the
   connect form, fill `zulip-server-url`, `zulip-email`, and the write-only
   `zulip-api-key`; select the explicit/all mode, history depth, and project;
   submit with `zulip-connect-submit`.
2. Assert that the API key input is cleared and never appears in DOM text,
   browser storage, events, logs, or subsequent GET responses. Inspect
   `zulip-provider-badge`, `zulip-provider-popover`, and
   `zulip-account-status` while the account advances to live-ready.
3. Before live-ready, assert `zulip-notification-gate` is present and imported
   history does not create desktop notifications. After live-ready, create a
   Zulip message and measure its appearance in Workspace without a reload.
4. For explicit selection, toggle `external-chat-toggle-<uuid>` and verify only
   that chat is projected. Switch to `all`, create another Zulip chat, and
   verify automatic discovery and project assignment. Exercise every history
   depth newest-first and use `external-chat-original-<uuid>` to open the exact
   provider chat.
5. Verify channel/topic, personal DM, and group DM projections in the ordinary
   Messenger sidebar and feed. Their provider badges and popovers must remain
   visible after reload, IndexedDB hydration, event catch-up, rename, and move
   to another Workspace project.
6. Send, edit, delete, reply, quote, mention, mark read, and transfer an image
   plus a generic file in both directions. Workspace mentions use
   `urn:user:<uuid>` and messages contain only file URNs; provider originals and
   copied bytes remain authorized to the current owner/project.
7. Force the ambiguous-send cases ZB-MSG-003 and ZB-MSG-005 through
   ZB-MSG-007. Inspect `external-operation-<uuid>` and the projected
   `provider-delivery-*` badge. For manual reconciliation, verify the safe
   reason and original link, then open
   `external-operation-retry-confirmation-<uuid>` and confirm that no retry is
   sent before the explicit duplicate-risk acknowledgement.
8. Exercise disconnect, reconnect with a new write-only key, and delete using
   `zulip-disconnect`, `zulip-reconnect-submit`, and `zulip-delete-confirm`.
   Disconnect keeps a read-only projection; delete removes it and prevents new
   provider traffic. Repeat with owner IAM deactivation and deletion.
9. Reload and background/foreground the window at every lifecycle stage.
   Confirm cached rendering precedes catch-up, no retained event is notified as
   new, no entity disappears from the sidebar, unread state converges, and the
   console contains no new provider-feature error.

The reusable UI automation belongs in `workspace_ui/e2e` and references these
stable test IDs. It must seed unique test data, clean only its own provider and
Workspace objects, and retain screenshots/traces outside service repositories
under the shared test-run archive policy.

## Reusable visible-UI acceptance checklist

The following checklist expands the journey into independently repeatable
visible assertions. A scenario is complete only when the user-visible state is
verified in the appropriate Workspace or Zulip tab without reloading unless
the step explicitly requires a reload. HTTP, Provider API, database, and
service logs may explain a failure but cannot replace the visible assertion.

### A. Baseline and connection

- [ ] In Workspace as `cassi`, verify native stream/topic navigation, one native
      message send, unread clearing, and realtime delivery before connecting Zulip.
- [ ] Open the Zulip owner session in a second visible tab and verify Channel A,
      Channel B, the personal DM, the group DM, and both incoming files.
- [ ] In `zulip-external-account-card`, connect with explicit selection,
      `history_depth=new`, and project A. Verify the visible progression
      `connecting` -> `backfill` -> `live`, the notification gate before
      `live_ready`, and the absence of the API key after submission.
- [ ] Verify the connected-account UI exposes no second Add action. Confirm the
      one-account-per-kind conflict through the contract harness without replacing
      the existing account, then verify the unchanged account in the visible UI.
- [ ] Exercise `auth_required` with a deliberately rotated disposable key, then
      use reconnect with the new key and verify return to `live`.

### B. Catalog, history, and project assignment

- [ ] In explicit mode, select Channel A and verify that Channel B, the personal
      DM, and the group DM remain unprojected until selected individually.
- [ ] Repeat Channel A import for `new`, `7_days`, `30_days`, `90_days`, and
      `all`. Before each repeat, deselect and wait for projection removal, then
      reselect. Verify exact boundary membership and newest-first arrival without
      duplicate Workspace messages.
- [ ] While the `all` backfill is still active, send a uniquely named live
      message from Zulip. Verify it appears before the remaining older history,
      meets the live latency target, and does not cause a backfill notification.
- [ ] Select Channel B, the personal DM, and the group DM into project A. Verify
      their ordinary Messenger stream/topic shapes, privacy, participant lists,
      provider badges, popovers, and `Open original` targets.
- [ ] Change the account to `selection_mode=all`, create Channel C in the visible
      Zulip tab, and verify it appears without another settings save and is assigned
      to the current default project.
- [ ] Move Channel A to project B with `external-chat-move-*`. Verify one atomic
      visible transition: it disappears from project A, appears in project B with
      the same stream/topic/message UUIDs and read state, and receives subsequent
      traffic only in project B.
- [ ] Change the default project and create another Zulip channel. Verify only
      newly discovered chats use the new default; existing projections do not move.

### C. Channel, topic, DM, and identity semantics

- [ ] Rename Channel A and one topic from Zulip, then rename them back from
      Workspace. Verify convergence in both visible tabs, stable Workspace UUIDs,
      and no duplicate stream/topic after reload or catch-up.
- [ ] Change the mapped channel description and privacy where the negotiated
      capability allows it. Verify the other side converges; where unsupported,
      verify the control is absent or disabled with a safe reason.
- [ ] Verify the personal DM remains exactly two participants with one default
      topic, and the group DM remains a private group stream with one default topic.
- [ ] Open peer A and peer B from projected messages. Verify separate external
      identities scoped to this Zulip account, a visible Zulip badge, and no
      accidental merge with same-email Workspace IAM users.
- [ ] Verify provider metadata and the interactive badge agree in the sidebar
      stream row, topic row, message bubble, participant/profile panel, REST-hydrated
      view, realtime update, and post-reload cached view.

### D. Two-way message operations

- [ ] Zulip -> Workspace: create Markdown/link messages in Channel A, both
      topics, the personal DM, and group DM. Verify appearance within the healthy
      p95 target without reload, correct author/topic, one projection, badge, and
      original link.
- [ ] Workspace -> Zulip: send equivalent messages in all three conversation
      shapes. Verify visible `pending` -> `delivered`, exactly one provider message,
      and continued composer scroll/read behavior in Workspace.
- [ ] In both directions, edit and delete a message; create a reply and a quote;
      mention peer A. Verify one converged operation, stable provider/Workspace
      IDs, correct reply target, readable quote, and `urn:user:<uuid>` mention
      rendering rather than literal URN text.
- [ ] Mark individual messages, a topic, and a stream read from both sides.
      Verify exact read convergence, unread/sidebar/folder badges clear, and an
      empty topic/stream produces no visible failure or phantom operation.
- [ ] Race edit against delete and two edits in opposite directions. Verify the
      last confirmed operation wins and delete wins over an unconfirmed edit.

### E. Files and loss-aware conversion

- [ ] Send the incoming PNG and generic file from Zulip. Verify Workspace renders
      the image, downloads the generic file, shows only URN references in message
      data, and preserves expected name, size, MIME type, and bytes.
- [ ] Send the outgoing PNG and generic file from Workspace. Verify both become
      accessible in Zulip with the expected bytes and that Workspace message
      content contains only S3-backed URNs.
- [ ] After both Channel B files have been copied, deselect Channel B. Verify the
      projection and provider-owned Workspace copies disappear immediately and
      no stale file affordance remains; reselect it only if a later scenario needs it.
- [ ] Trigger an operation whose preflight has losses. Verify
      `external-operation-preflight-dialog` lists the safe losses; Cancel performs
      no mutation, while explicit Continue performs exactly one operation.
- [ ] Receive an unsupported provider element. Verify a safe readable fallback,
      the Zulip badge, and `Open original`; raw provider markup must not leak into
      the rendered message.
- [ ] Disable one negotiated mutation capability. Verify the related control is
      hidden or disabled with its safe `unavailable_reason`, while unrelated native
      and provider operations remain usable. Restore capability and verify the
      control returns after heartbeat/catch-up without reload.

### F. Offline, retry, and reconciliation

- [ ] Interrupt Zulip connectivity while both visible tabs remain open. Verify
      account health becomes `degraded` after the defined progress timeout, queued
      outbound work remains visible, native Workspace messaging still works, and
      automatic recovery occurs after connectivity returns.
- [ ] Lose a send response but retain the matching Zulip local-echo event.
      Verify exactly one Zulip message and visible delivery convergence without an
      automatic resend.
- [ ] Lose both the response and local echo but expose an exact history match.
      Verify delayed reconciliation accepts the match, selects one deterministic
      provider message, and does not resend.
- [ ] Return no exact history match. Verify exactly one automatic resend. If the
      second outcome is confirmed, the operation becomes delivered with one
      visible provider message.
- [ ] Make history unavailable, or lose the result of the one permitted resend.
      Verify `manual_reconciliation_required`, a safe reason, duplicate-risk text,
      and an original link when known. `external-operation-retry-confirmation-*`
      must block retry until the explicit acknowledgement; Discard must prevent any
      later send.
- [ ] Restart only the bridge root image/service while work is queued. Verify
      persistent cursor, mappings, causal order, retries, and deduplication recover
      from the same data disk, and no retained message is notified as new.

### G. Lifecycle, policy, and cache/realtime

- [ ] Before granting the dedicated operator role, verify the provider policy,
      health, and bridge-instance administration endpoints return `403` for
      `cassi` and `zulip-admin-panel` is absent. Bind the role in the Workspace
      project, verify IAM introspection contains exactly the nine canonical
      external-provider permissions (no wildcard), and confirm the panel and
      read-only administration resources become available. Delete only that
      role binding and verify the endpoints return `403` and the panel disappears
      again without changing the ordinary Workspace role.
- [ ] Disconnect the `cassi` account. Verify existing projections stay readable,
      mutation controls are unavailable, and new Zulip traffic is not projected.
      Reconnect with a fresh key and verify catch-up then live traffic resume once.
- [ ] With the disposable lifecycle owner, deactivate IAM and verify sync stops
      without destructive projection loss. Reactivate and verify recovery. Delete
      the IAM owner and verify the account, projection, queued work, and copied
      provider files are purged.
- [ ] Delete the `cassi` external account through `zulip-delete-confirm`. Verify
      immediate projection removal, no later traffic, and a fresh connect form. Do
      this only after all non-destructive main-account scenarios have passed.
- [ ] As `cassi` with administrator permissions, inspect
      `zulip-admin-panel` and `zulip-admin-health`. Verify aggregate health contains
      no account credentials, message content, or owner chat catalog. Exercise
      `zulip-admin-provider-enabled`, every `zulip-admin-limit-*`,
      `zulip-admin-custom-ca`, `zulip-admin-custom-ca-remove`, emergency
      `zulip-admin-provider-suspend`/`zulip-admin-provider-resume`, and the
      `external-bridge-instance-<uuid>` suspend/resume/revoke controls using
      disposable policy/identity state.
- [ ] Verify heartbeat state transitions at the configured 10-second cadence:
      healthy, degraded after 30 seconds without heartbeat, and aggregate offline
      after 60 seconds. Resume only after a compatible capability heartbeat.
- [ ] At every lifecycle stage, reload, close/reopen, and background/foreground
      the Workspace tab. Verify cached entities render first, catch-up only moves
      state forward, historical data creates no notification, live-ready traffic
      does notify, sidebar/unread state converges, and no provider-feature error is
      added to the browser console.
- [ ] Finish with the same native stream/topic send, realtime, unread, file, and
      navigation smoke checks used for the baseline. Native Messenger must remain
      functional after account deletion or bridge suspension.

## Execution gates

1. Contract, unit, migration, type, lint, and image-contract checks pass locally.
2. Fake-transport integration tests pass without a running Zulip realm.
3. Real Provider API, S3, and Zulip integration passes in the isolated environment.
4. Development images are built with immutable versions.
5. Only safe element updates are applied; no working data disk is recreated.
6. Every required visible UI journey passes under `cassi` through Playwright MCP.
7. The deployed Workspace manifest reconciles all nine external-provider IAM
   permissions, while effective administrator access exists only through an
   explicit project-scoped role binding and is removed by deleting that binding.

Any unexecuted scenario is reported as `NOT RUN` or `BLOCKED`. The feature is
not complete and must not be enabled for the realm while a required gate is
missing or failing.
