# PostgreSQL Canonical Messenger Test Plan

This plan is the reusable acceptance gate for moving Workspace Messenger to a
canonical PostgreSQL store while preserving the current browser-facing REST and
websocket contracts. S3-compatible storage remains canonical for file bytes and
sidecars. Provider runtimes use the private Workspace Provider API; the backend
has no SMTP, IMAP, Exim, Dovecot, or Maildir runtime dependency after cutover.

The existing Maildir installation is only a transitional import source and a
rollback surface until the PostgreSQL cutover is accepted. The mail VM and its
persistent disk must remain intact and unused until every migration, contract,
recovery, provider, visible-UI, and load gate in this plan has passed.

This plan supplements
[`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) and
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md). Historical run
artifacts belong in the shared CASSI test-run archive, not in this repository.

## Acceptance profiles

Two deterministic profiles are required. The CI profile detects contract and
algorithmic regressions quickly. The full profile is the release gate.

| Dimension | CI profile | Full profile |
| --- | ---: | ---: |
| IAM users | 30 | 300 |
| Streams | 12 | 120: 60 channels, 30 group DMs, 30 direct DMs |
| Topics | 48 | 480 |
| Canonical messages | 10,000 | 100,000 |
| Zulip/provider messages | 2,000 | 20,000 |
| Reactions | 1,000 | 10,000 |
| S3 files | 100 | 1,000 |
| Canonical broadcast event rows before retention | 30,000 | 300,000 |
| Authorized visible event deliveries | 600,000 | 6,000,000 |
| Simultaneous live users | 30 | exactly 150 |

The full fixture uses a deterministic Zipf distribution so hot streams, cold
streams, large streams, DMs, and empty streams are all represented. Every one
of the 150 concurrent users has an independent Workspace identity, an
independent Zulip account and credential, and a distinct account assignment.
Most Workspace streams visible to those users are Zulip projections. No key,
session, cursor, provider mapping, unread state, or result may cross accounts.

## Reusable harness

The implementation must add these reusable repository assets:

- `workspace/tests/scale/generate_messenger_fixture.py`: seeded fixture
  generator and fixed worker-owned application boundary. The boundary requires
  explicit isolated project/run/target binding and a concrete adapter, owns one
  RESTAlchemy transaction per application unit, resumes from adapter-owned
  durable completed IDs, and never opens a nested session.
- `workspace/tests/scale/verify_messenger_fixture.py`: count, relationship,
  normalized-row-digest, event, and S3 inventory verifier.
- `workspace/tests/scale/profiles/db-ci-v1.json` and
  `workspace/tests/scale/profiles/db-300x120x100k-v1.json`: immutable profile
  definitions.
- `workspace/tests/load/k6/messenger_db.js`: REST and websocket workload.
- `workspace/tests/load/k6/zulip_provider.js`: 150-account Provider API and
  Zulip workload with steady and burst phases.
- `workspace/tests/load/collect_metrics.sh`: five-second process, cgroup,
  PostgreSQL, S3, API, and provider metric sampling without credentials or
  message content.

The generator writes `fixture-manifest.json` outside the repository. It records
the random seed, profile version, expected row counts, relationship counts,
normalized digests, event age buckets, S3 object names and hashes, and provider
mapping counts. Re-running a profile with the same seed must produce the same
logical manifest.

It also writes digest-bound synthetic logical records in
`application-plan.jsonl`. This does not authorize direct canonical-table SQL.
The plan already declares deterministic native ownership, roles, binding UUIDs,
privacy/invite policy and default topics. Provider account/chat records carry
stable external chat IDs, provider chat keys, catalog participants/topics and
backend-compatible projection UUIDs. Provider message rows contain the exact
directional Provider event or operation enqueue contract. File bytes, size,
hash and sidecar are reproducible from one bounded content recipe. Application
credentials privately map every logical fixture user ordinal/UUID to one
existing IAM user/token; no secret is copied into generated artifacts.

The concrete writer adapter, isolated deterministic event seam and read-only
PostgreSQL/S3 inventory exporter exist. The exporter verifies project-scoped
counts, relationships, normalized digests, event-age buckets, audience fanout,
provider mappings, provider inbound event/idempotency and outbound operation
queue ledgers, exact object bytes and normalized plus raw sidecars. Ledger
comparison uses stable persisted fields and verifies the stored inbound
envelope hash against the runtime-normalized event; runtime-only queue and
bridge-instance UUIDs are not planner expectations. The
verifier independently binds `actual-inventory.json` to the exact manifest,
profile, project and run through `fixture-application-result.json`; stale or
tampered `passed: true` artifacts fail closed. A full-profile acceptance result
is still NOT RUN until these assets execute against the isolated environment;
the existence of the exporter alone is not a performance or migration pass.

Every native and provider attempt writes a composable expected run-ledger row
before execution. Native k6 may export an observation from its exact Workspace
create response. Provider k6 exports only a separate visibility diagnostic;
an authoritative provider observation must come from the destination exporter:
`workspace_backend` for inbound and `provider_connector` for outbound. It binds
the exact operation UUID to provider event/result IDs, mappings, idempotency
key, payload hash, and account-persistent cursor ordinal. A source-side row from
the other exporter is ignored rather than treated as a second result. Unknown
provider operation kinds and runs with only diagnostics fail closed. A 100-row
message-content scan cannot prove loss, duplication, or account isolation.

The event fixture has records on both sides of the seven-day UTC cutoff and at
the exact boundary. Event fanout is derived from stream membership rather than
invented independently. Messages, streams, topics, files, and provider mappings
are never aged out by the event-retention fixture.

In addition to the reusable full fixture, the dedicated weekly retention
workload represents seven days at 200 logical messages per minute: 2,016,000
messages, exactly 6,048,000 message/topic/stream broadcast rows, and up to
1,814,400,000 authorized visible deliveries when all 300 users are recipients.
Those deliveries are verified through the visibility query and digests; they
must not be materialized as per-recipient canonical event rows.

Every compact event references an immutable, reusable audience snapshot.
Membership UUIDs are stored once per membership revision, not copied into each
event. The audience keeps one aggregate current/pruned cursor; a broadcast does
not update 300 user cursor rows. Common payload fields live on the broadcast,
while only genuinely different recipient fields live in the indexed relational
override table. Later membership changes cannot rewrite historical delivery.

Canonical create emits exactly three broadcast rows: `message.created`,
`topic.updated`, and `stream.updated`. The latter two preserve authoritative
server unread snapshots. It emits no per-message `folder.updated`: the browser
projects folder aggregates from `stream.updated`, while `folder.updated` remains
for real folder mutations and explicit read reconciliation. At 300 recipients
and 200 messages/minute the previous behavior could exceed 180,000 base event
rows/minute and one billion rows/week; the compact path remains three base rows
per create, independent of recipient count.

## Public contract compatibility

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DB-CONTRACT-001 | Normalize and compare OpenAPI before and after the storage cutover for `/api/workspace/v1/messenger/**`, `/events/`, `/events/ws`, `/epoch/`, and `/me`. | Paths, methods, request and response schemas, status codes, headers, actions, filters, and pagination are unchanged. |
| MSG-DB-CONTRACT-002 | Replay golden requests and responses for users, streams, bindings, topics, messages, reactions, folders, files, drafts, and external resources. | Normalized JSON is equal, including `source`, `provider`, `delivery`, URNs, timestamps, unread fields, and nullable fields. |
| MSG-DB-CONTRACT-003 | Compare each REST catch-up event with its websocket representation. | Decoded JSON is equal; UUID deduplication, `epoch_version`, `epoch_generation`, ordering, and typed `410` behavior are unchanged. |
| MSG-DB-CONTRACT-004 | Run the existing live Messenger browser journey against the PostgreSQL build without changing the UI request layer. | The current UI completes the journey without a compatibility adapter or alternate endpoint. |
| MSG-DB-CONTRACT-005 | Generate the same fixture twice and inspect every logical plan record. | Application-plan bytes are identical; native ownership/bindings/privacy/default topics, provider chat/catalog/projection mappings, directional Provider contracts, file recipes/hashes/sidecars and event UUID/time/retention contracts are complete and backend-compatible. |
| MSG-DB-CONTRACT-006 | Load the private fixture credential bundle with missing, duplicate, reordered or mismatched logical/IAM mappings, then inspect all generated artifacts for credentials. | Only an exact one-to-one ordinal mapping to existing IAM users is accepted; tokens never appear in the manifest, application plan, ledgers or result artifacts. |
| MSG-DB-NOMAIL-001 | Inspect the backend image, manifest, processes, sockets, service configuration, and network counters during the full functional and load run. | The running backend has no mail service, SMTP/IMAP client, Maildir path, mail certificate, mail route, or connection to ports `25`, `143`, `465`, or `993`. |

## Transitional Maildir import

The import reads the existing canonical mail data once into an isolated
PostgreSQL schema. It must not treat the current rebuildable SQL projection as
the authority. Logical messages duplicated across participant mailboxes are
deduplicated by Workspace message UUID. Tombstones win over older content.
Retained per-user event journals, user flags, folders, provider metadata, and
file references are imported with their original identities.

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-MIG-001 | Capture a preflight inventory and freeze checkpoint for every project and user before import. | The inventory contains entity counts, journal ranges, retained event ranges, S3 hashes, and a stable cutoff; no source is modified. |
| MSG-MIG-002 | Import users, streams, topics, bindings, folders, messages, reactions, flags, retained events, files, and provider metadata. | Source and destination have equal canonical counts and per-row digests; missing, extra, conflicting, and duplicate logical entities are all zero. |
| MSG-MIG-003 | Verify UUIDs, timestamps, revisions, direct-chat identities, roles, ordering, unread state, provider IDs, delivery state, markdown, and file URNs. | Every value required by the public contract is preserved exactly or by a documented epoch mapping that leaves the public shape unchanged. |
| MSG-MIG-004 | Run the importer twice, then interrupt separate runs at 10%, 50%, and 90%. | The second complete run writes nothing; every interrupted run resumes from its checkpoint and ends with the same destination digest. |
| MSG-MIG-005 | Place an invalid, truncated, or unsupported source record immediately before a valid record. | The invalid record is quarantined with bounded diagnostics, the valid record is not skipped, and cutover remains blocked until the discrepancy is resolved. |
| MSG-MIG-006 | Acquire the machine-verifiable API/worker/SMTP/bridge writer gate, apply the final source delta, verify parity while the gate remains held, and switch the writer to PostgreSQL. | Two freeze captures match; final-delta and parity match the same UID/checkpoint/digest; any advancing source blocks cutover; there is no dual-write interval or divergent accepted write. |
| MSG-MIG-007 | Abort before acceptance and restore the previous backend path. | The untouched mail disk can serve the pre-cutover state; PostgreSQL import artifacts remain isolated and no user-visible state changes. |
| MSG-MIG-008 | Restart the importer, backend, and PostgreSQL independently during import and immediately after cutover. | Checkpoints and transaction boundaries recover monotonically without loss or duplication. |
| MSG-MIG-009 | Keep the old mail VM present but unused through all acceptance gates. | No runtime traffic reaches it, its disk remains unchanged, and manifest removal is forbidden until the final gate is approved. |
| MSG-MIG-010 | Submit a forged JSON file or an unknown gate UUID, then omit each writer-class acknowledgement in turn. | Freeze rejects caller assertions and refuses to capture until the authoritative live gate has unexpired `api`, `worker`, `smtp_ingress`, and `external_bridge` acknowledgements. |
| MSG-MIG-011 | Attempt one write through every writer boundary while the gate is closed, then release exactly that generation and retry. | API mutations fail, worker work is parked, SMTP ingress and bridge projection do not advance the source; after release each path resumes once without loss or duplication. |
| MSG-MIG-012 | Replace/release the gate or let one acknowledgement expire between freeze, final-delta, and parity. | Every later phase aborts before applying data; a stale gate generation can never authorize cutover. |

`MSG-MIG-006` uses the mail-image `workspace-smtp-ingress-attester`. Verify that
the instance is live before acquisition, the persistent hold precedes the Exim
stop, its atomic rename is directory-fsynced, `exim4 -qff` drains the queue,
`exiwhat` is empty before the final `exim4 -bpc` zero check, and
the exact generation is acknowledged only after both checks. Kill or restart
the attester at each transition and prove the Exim prestart guard remains
fail-closed. A drain timeout or stale/replaced generation must never produce an
acknowledgement.
Overlapping or replaced generations must accumulate in the persistent hold and
remain blocked until every recorded exact generation is explicitly released.
Release and resume must use the exact gate ID; direct marker removal is not an
acceptance procedure. The backend worker must never manufacture this evidence.

The parity report must include exact source/destination digests for all resource
types, a duplicate-recipient-message report, tombstone precedence, retained
event boundaries, and S3 binary plus sidecar hashes.

## Canonical PostgreSQL behavior

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DB-STORE-001 | Create, update, act on, and delete every Messenger resource. | Canonical rows, relationships, event rows, and provider commands commit in the request-owned RESTAlchemy transaction. |
| MSG-DB-STORE-002 | Inject failure before and after each mutation boundary. | Either the complete old state or complete new state is visible; no orphan event, counter, mapping, file reference, or command is committed. |
| MSG-DB-STORE-003 | Retry client UUIDs and race edits, deletes, reactions, reads, and membership changes. | Idempotency and last-confirmed/delete-wins rules hold without duplicate user-visible entities. |
| MSG-DB-STORE-004 | Measure SQL statements while listing and reading from the full fixture. | Reads use PostgreSQL only; statement count and result memory are bounded independently of total history. |
| MSG-DB-STORE-005 | Exercise provider and native rows in the same stream and topic. | Dynamic provider metadata remains typed and isolated while ordinary Messenger filters and pagination treat both as normal resources. |
| MSG-DB-STORE-006 | Upload, reference, read, update, and delete S3-backed files. | PostgreSQL stores canonical metadata and ACL state; S3 stores bytes and sidecars; messages contain only authorized URNs. |

## Derived-state rebuild and recovery

PostgreSQL base tables are canonical. Only explicitly identified views,
counters, search indexes, caches, and delivery projections may be discarded and
rebuilt.

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DB-REBUILD-001 | Drop only declared disposable derived state and rebuild it from canonical base tables. | The public resource digest, unread counts, folder contents, search results, and provider delivery state match the pre-rebuild baseline. |
| MSG-DB-REBUILD-002 | Rebuild from a logical anchor while writes continue, catch up the delta, and swap atomically. | No write is lost or duplicated; readers see either the old complete derived state or the new complete state. |
| MSG-DB-RECOVERY-001 | Restart API, event, worker, provider, and PostgreSQL processes independently with queued work present. | Durable work resumes, websocket clients catch up, and no retained event is notified twice. |
| MSG-DB-RECOVERY-002 | Make PostgreSQL unavailable during a request. | The request fails explicitly without a partial commit and succeeds idempotently after recovery. |
| MSG-DB-RECOVERY-003 | Make S3 unavailable during upload and provider file copy phases. | No downloadable URN becomes visible before bytes, sidecar, metadata, and ACL are consistent; retry or cleanup is idempotent. |

## Events and seven-day retention

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DB-EVENT-001 | Create, update, delete, and react to messages with 1, 30, and 300 recipients. | Create writes exactly message/topic/stream broadcasts; update and delete write one message broadcast; reaction writes one actor event plus one message broadcast; authorized delivery resolves once per recipient without per-recipient canonical event or cursor rows. |
| MSG-DB-EVENT-002 | Prune the full event fixture at the exact seven-day UTC cutoff. | Only events older than the cutoff are removed; boundary and newer events remain; messages, files, streams, topics, provider mappings, and settings are byte-identical. |
| MSG-DB-EVENT-003 | Catch up from retained, expired, future, wrong-generation, and wrong-scope cursors. | Retained suffixes are gap-free; invalid cursors return the established typed error; the client performs one authoritative snapshot and resumes. |
| MSG-DB-EVENT-004 | Connect 150 websocket users, disconnect them, mutate state, and reconnect them simultaneously. | Every user receives only its exact suffix, deduplicated by UUID and ordered by epoch; initial catch-up produces no desktop notification. |
| MSG-DB-EVENT-005 | Reuse one 300-member audience for two million events, then change one membership and continue. | The unchanged audience has 300 member rows total; the membership change creates one new immutable snapshot; event rows contain no recipient UUID array or full repeated message body. |
| MSG-DB-EVENT-006 | Prune all but one event referencing a shared audience, then prune the last event. | The audience and members remain while referenced; after the final event is removed its watermarks are folded into user cursors once and the orphan snapshot, members, and override rows are removed safely. |

## Workspace Provider API v1

Provider processes use a private HTTP API rooted at
`/api/workspace-provider/v1`. The browser continues to use only the public
Workspace API. The v1 provider surface under test is:

- idempotent provider registration and capability heartbeat;
- paginated external-account assignment and change feed with an opaque cursor
  and full-snapshot fallback;
- provider-owned upsert and delete for ordinary Messenger users, streams,
  topics, and messages;
- account-user read-state updates;
- Workspace-to-provider command polling, claiming, and idempotent terminal
  result reporting;
- S3 file allocation, finalize, download, and cleanup through the dedicated
  provider file boundary.

Provider payloads use a common envelope and dynamic `settings.kind` and
metadata objects. Stable Workspace UUIDs and provider/account external IDs are
required on every mutation. A provider retry must be safe after an unknown
response outcome.

Provider-side reaction synchronization remains outside the approved Zulip
bridge v1 scope. Native Workspace reaction regression coverage remains in the
canonical Messenger suites.

| ID | Scenario | Expected result |
| --- | --- | --- |
| PROV-V1-001 | Register the Zulip provider repeatedly and change its compatible capabilities. | One provider identity remains; effective capabilities and health converge without changing account ownership. |
| PROV-V1-002 | Page account assignments, consume incremental changes, expire a cursor, and recover with a snapshot. | No account is skipped or duplicated and the provider resumes from a new opaque cursor. |
| PROV-V1-003 | Upsert and delete provider users, streams, topics, and messages, then repeat read-state updates. | Stable provider mappings produce one ordinary Messenger resource and one authorized event sequence. |
| PROV-V1-004 | Poll, claim, execute, and report Workspace-to-provider commands with retries and process crashes. | Commands remain ordered and idempotent; duplicate reports do not duplicate provider operations. |
| PROV-V1-005 | Synchronize Zulip channels, topics, personal DMs, and group DMs in both directions. | Create, edit, delete, reply, mention, read, and rename converge with stable IDs, badges, metadata, and original links. |
| PROV-V1-006 | Transfer images and generic files in both directions. | Bytes and hashes match in receiving storage, ACLs follow the current projection, and Workspace message bodies contain only URNs. |
| PROV-V1-007 | Stop Zulip and the provider process during live work and backfill, then recover. | Durable progress resumes newest-first; live work has priority; native Workspace messaging remains available. |
| PROV-V1-008 | Disconnect, reconnect, deactivate, reactivate, and delete external accounts and owners. | Account and projection lifecycle follows the public contract without orphan mappings, files, work, or cross-account visibility. |

## Visible Playwright acceptance

All web assertions run through the global Playwright MCP in visible windows.
The primary Workspace session is the real `cassi` account. Keep Workspace and
Zulip visible throughout the provider journey; API and database probes are
diagnostic evidence, not substitutes for UI assertions.

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-UI-DB-001 | Run `workspace_ui/e2e/live/messenger-contract-live.spec.ts` unchanged against the PostgreSQL build. | The complete current Messenger contract journey passes without UI API changes. |
| MSG-UI-DB-002 | Keep Workspace open across backend, worker, and PostgreSQL restarts. | Cached state renders, catch-up moves forward, streams/topics/messages/unread state persist, and old events do not notify as new. |
| MSG-UI-DB-003 | Connect Zulip, select chats, backfill, exchange live messages/files, rename, disconnect, reconnect, and delete. | Both visible applications converge without reload; badges, popovers, delivery state, and errors are understandable and correct. |
| MSG-UI-DB-004 | Inspect a deterministic sample of pre-cutover channels, DMs, provider chats, messages, reactions, unread state, and files after migration. | The visible sample matches the preflight capture and every original file remains accessible to authorized users. |
| MSG-UI-DB-005 | Receive compact message/topic/stream events in streams placed in system and custom folders, then read and delete messages. | Topic, stream, folder, organization, and OS unread badges converge without `folder.updated` on message create; actual folder mutations still use `folder.updated`. |

## Load workload and thresholds

The full run uses the full fixture and exactly 150 simultaneous users. Ramp for
five minutes, hold the steady workload for 30 minutes, run a five-minute burst,
then observe recovery for ten minutes.

All 150 users maintain websocket connections and active Zulip accounts. The
steady provider rate is 100 to 200 messages per minute across independent
accounts with a 60/40 inbound/outbound mix. The provider workload also includes
topic and stream renames, message edits and deletes, read-state changes, and
image and generic-file transfers; the native workload continues to exercise
reactions. The burst reaches 400 provider messages
per minute for five minutes while 20% of accounts reconnect and backfill.

| ID | Workload | Acceptance threshold |
| --- | --- | --- |
| MSG-LOAD-001 | Cold and warm reads over 300 users, 120 streams, and 100,000 messages. | p95 streams/topics/folders at most 250 ms; message page of 100 at most 350 ms; search/filter at most 750 ms; p99 at most twice p95. |
| MSG-LOAD-002 | 150 websocket users, 100 aggregate REST requests/s, and 20 native message mutations/s. | HTTP error rate at most 0.1%; send acceptance p95 at most 1 s; visible realtime p95 at most 5 s and p99 at most 10 s; no lost or duplicate event. |
| MSG-LOAD-003 | Simultaneous reconnect and catch-up of all 150 users. | 95% reach ready within 30 s and 100% within 60 s; each receives its exact authorized suffix or one typed expiry followed by one snapshot. |
| MSG-LOAD-004 | Retention prune over 6,048,000 compact broadcast rows (up to 1,814,400,000 authorized visible deliveries) while writes continue. | Prune completes within 10 min; write p99 stays below 2 s; retained base-event count, visible-delivery count, and digests are exact; no per-recipient canonical event rows are introduced. |
| MSG-LOAD-005 | Full derived-state rebuild over 100,000 messages while live writes continue. | Base rebuild completes within 15 min, final catch-up within 60 s, and the atomic result digest is unchanged. |
| MSG-LOAD-006 | Steady and burst provider workload across 150 independent Zulip accounts. | Sustained 100-200 provider messages/min and five-minute 400/min burst converge; provider live p95 at most 5 s; native p95 regresses no more than 20%; no cross-account entity, cursor, credential, or result leakage. |
| MSG-LOAD-007 | Stop Zulip/provider connectivity during the burst, continue native work, then restore it. | Native thresholds remain green; backfill does not starve live work; provider queues drain within 15 min after recovery without duplicates. |
| MSG-LOAD-008 | Measure backend resources on a 4-vCPU, 4-GiB reference node. | Each Python API/event/worker RSS stays at or below 512 MiB; backend cgroup stays at or below 2 GiB; post-warmup RSS slope is below 1 MiB/min; final RSS is at most 115% of warm baseline; steady CPU is at most 70% and no five-minute window exceeds 85%; no OOM or unexpected restart. |
| MSG-LOAD-009 | Record SQL and network work per request at full scale. | Hot reads use at most 12 SQL statements and a message page at most 8; no full-table sequential scan appears for messages/events; warm buffer hit is at least 99%; SMTP/IMAP connections and bytes remain zero. |
| MSG-LOAD-010 | Catch up from the beginning of a full retained event suffix with the public page limit. | PostgreSQL applies `LIMIT` before row packing, Maildir transition mode limits IMAP UIDs before fetch, peak request memory is independent of retained history, and every next page is ordered without a gap or duplicate. |
| MSG-LOAD-011 | Send 200 messages/min to an unchanged 300-user audience and sample WAL/table growth. | Each create writes three base broadcasts, three audience-cursor updates, no user-cursor fanout, and at most minimal relational recipient overrides; the membership table remains 300 rows. |
| MSG-LOAD-012 | Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for one user's catch-up over at least two million broadcast rows and multiple audience revisions. | The plan uses `m_workspace_event_audience_user_idx` and `m_workspace_broadcast_events_audience_epoch_idx` (or a measured equivalent indexed plan), applies the epoch/page limit before packing, and performs no sequential scan of the broadcast or membership tables. |
| MSG-LOAD-013 | Mix create, update, delete, reaction, read, and membership churn at the full profile rate. | Create stays at three broadcasts, update/delete at one, reaction at one actor plus one broadcast; p99 and WAL remain within thresholds and no operation regresses to recipient-count event/cursor writes. |

The absolute thresholds and a regression comparison are both mandatory. A
result that is more than 20% slower or larger than the last accepted baseline
fails even when it remains just inside an absolute limit.

## Load-generator VM

If the local runner cannot sustain 150 independent browser/API/websocket and
provider sessions without becoming the bottleneck, create one disposable
ordinary Ubuntu VM through Exordos Core with the `exordos` CLI. It is a compute
node, not an element.

Use a unique `cassi-workspace-loadgen-<run>` name, the current stable ordinary
platform base image, 8 vCPU, 16 GiB RAM, and a 40 GiB root disk. Create it only
through `exordos compute nodes add ... --cores 8 --ram 16384 --root-disk 40
--wait`; record the exact image version and final node status in the private
test-run archive. Install only the load harness and ephemeral test credentials.

Before creation and cleanup, list platform resources and explicitly exclude the
protected `CASSI` and `zulip-test-12-1` VMs. Cleanup may delete only the node UUID
created for this run, after evidence and metrics have been copied to the test-run
archive and credentials have been removed. A failed node is preserved until its
failure evidence is captured. The load generator must never deploy or replace a
Workspace element and must never trigger platform-wide cleanup.

## Production migration artifact acceptance

The production migration workflow publishes four immutable artifacts in the
strict order compatibility, stage, rollback, canonical. The accepted
compatibility mail root is the only mail root allowed in the remaining three
artifacts, while the stage backend root is the only backend root allowed in the
rollback and canonical artifacts.

| Test ID | Scenario | Acceptance criteria |
| --- | --- | --- |
| MSG-DEPLOY-COMPAT-001 | Render and verify the compatibility artifact from the exact current backend root and the newly built mail root, delivering mail, PKI, and compatibility-marker configs in every order. | Backend REST/realtime configuration still uses the remote mail transport with STARTTLS; persistent backend/mail disks and service identities are unchanged; mail bootstrap and CA services are present; writer-gate role, config, attester service, and enforced marker are absent; the explicit compatibility prestart marker is present and its idempotent reload converges every delivery order. |
| MSG-DEPLOY-COMPAT-002 | Remove either ordinary mail bootstrap service from a compatibility or stage manifest fixture and run the production manifest verifier. | Verification fails before publication and identifies the missing service without exposing configuration or secret values. |
| MSG-DEPLOY-STAGE-001 | Render stage from the newly built backend root and the exact accepted compatibility mail root. | Writer-gate role, config, attester service, enforced marker, and the exact bootstrap/CA/attester service set are present; config delivery has no Exim restart action; all persistent resources are retained. |
| MSG-DEPLOY-GUARD-001 | With a persistent migration hold active, restart the retained mail VM and deploy rollback, including the case where the compatibility marker is still present. | Bootstrap validates the hold, renders mail configuration, starts and checks Dovecot/IMAP, and returns success while Exim remains stopped; SMTP health expects an inactive listener. Missing writer-gate config fails closed when the enforced marker exists; only exact `resume --gate-id` removes the hold and starts Exim. |
| MSG-DEPLOY-ROLLBACK-001 | Render rollback from the exact stage backend root and accepted compatibility mail root, then verify and read it back from the repository. | The artifact preserves `mail_projection`, writer-gate enforcement, persistent resources, and the exact bootstrap/CA/attester service set; root digests match stage and compatibility evidence. |
| MSG-DEPLOY-CANONICAL-001 | Render canonical from the exact stage backend root and accepted compatibility mail root, then reboot the retained mail VM before destructive cleanup is authorized. | Canonical PostgreSQL is active; no mail runtime service is declared; mail config/PKI and DNS identities, persistent hold, enforced marker, and gate config remain inertly; Exim cannot restart; mail VM and disk remain available for rollback evidence. |
| MSG-DEPLOY-PUBLISH-001 | Inspect workflow evidence, version ordering, repository push order, and readback for all four artifacts. | Semantic versions satisfy compatibility < stage < canonical < rollback; all four manifests, inventories, digests, source commits, and selected roots are recorded; publication/readback order is compatibility, stage, rollback, canonical; any mismatch stops all publication. |

## Required evidence

Each full run archives the following sanitized artifacts outside service
repositories:

- exact source commits, image versions, manifest inputs, and fixture manifest;
- normalized OpenAPI and golden-response diffs;
- source/import inventory, migration checkpoints, parity digests, duplicate
  report, rollback result, and S3 binary/sidecar hashes;
- k6 summary JSON and per-scenario latency, rate, error, and reconnect results;
- five-second cgroup/process/API/provider/PostgreSQL/S3 metrics;
- `pg_stat_statements`, connection counts, table/index sizes, and
  `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for critical queries;
- event fanout, retention, cursor, notification-gate, and isolation reports;
- zero-mail socket and network counters;
- sanitized service journals and restart/OOM counters;
- visible Playwright traces, screenshots, video, and browser console output;
- a final matrix marking every required ID `PASS`, `FAIL`, `NOT RUN`, or
  `BLOCKED`.

## Safe execution order

1. Freeze the public OpenAPI and golden baseline; capture database, S3, and
   transitional mail-source backups and inventories.
2. Run unit, integration, migration, provider, and CI-profile scale tests.
3. Import into a shadow PostgreSQL schema; verify completeness, idempotency,
   interruption recovery, and rollback without changing the live writer.
4. Build immutable backend, UI, and provider images and apply only safe updates.
5. Freeze writes briefly, apply the final delta, switch atomically with
   `messenger_storage_mode=postgresql_canonical` and
   `messenger_canonical_cutover_confirmed=true`, and leave
   `retain_legacy_mail_resources=true` so the mail VM and disk remain intact
   but unused.
6. Run public compatibility, native Messenger, Provider API, S3, visible
   Workspace/Zulip, restart, failure, retention, and rebuild gates.
7. Run the exact 150-user workload, provider outage/recovery, burst, and a
   24-hour soak with resource and isolation evidence.
8. Render and review a separate manifest update with
   `retain_legacy_mail_resources=false` only after every gate is accepted and
   rollback data has a verified backup. The rendered resource graph must have
   zero mail resources or references. Apply it separately, then repeat
   contract, restart, provider, visible-UI smoke, and the critical load subset.

Any required scenario that is not executed is `NOT RUN` or `BLOCKED`. The
PostgreSQL cutover is not accepted, and the mail VM must not be removed, while
any required ID is missing or failing.
