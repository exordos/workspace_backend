# Genesis Workspace Backend

Backend services for **Genesis Workspace Messenger**. The public Messenger API
is IAM-authenticated and preserves the existing REST and realtime contracts.
Canonical Messenger persistence is PostgreSQL.

External accounts are scoped to their owner and request project, with a required
`provider` and generic `settings`. Creating or updating a Zulip account queues connection
initialization. Updates reset its status to `new` and clear its previous error.
Workspace messages and chats
retain `source_name: native` and `source: {"kind": "native"}` and use ordinary
membership permissions. Attachments use the configured file storage and IAM
remains the source of authentication.

The rendered documentation is published at
https://exordos.github.io/workspace_backend/ and is available in four
languages with the same file and section structure:

- [English](docs/en/index.md)
- [Русский](docs/ru/index.md)
- [Deutsch](docs/de/index.md)
- [中文](docs/zh/index.md)

English contracts and integration guidance:

- [`docs/en/workspace_api.md`](docs/en/workspace_api.md)
- [`docs/en/workspace_ui_realtime_integration.md`](docs/en/workspace_ui_realtime_integration.md)
- [`docs/public_workspace_api.yaml`](docs/public_workspace_api.yaml)

The generated public Workspace and Messenger specification uses OpenAPI 3.0.3.

## Runtime entry points

Direct local services:

- Messenger REST API: `http://127.0.0.1:21084/v1/messenger`
- WebSocket API: `ws://127.0.0.1:21082/v1/events/ws`
- Workspace REST API: `http://127.0.0.1:21084/v1`
- Messenger worker: `workspace-messenger-worker`
- History import service: `workspace-history-import`
- Zulip realtime service: `workspace-zulip-realtime`
- Zulip outgoing intent worker: `workspace-zulip-outgoing`
- Workspace OpenAPI spec: `http://127.0.0.1:21084/specifications/3.0.3`

The single `workspace-api` process serves all REST resources, including Messenger,
with sixteen HTTP workers by default.

The history import service consumes both inbound task queues (`[history_import]
queue = all`) with a bounded thread pool (four workers by default). The realtime
service receives provider events and reconciles personal flags and reactions.

Run `workspace-zulip-outgoing --config-file /etc/workspace/workspace.conf` as a
separate, single process. It uses asyncio, direct PostgreSQL statements and a
persistent aiohttp connection pool. API mutations record `zulip_outgoing_intents`
in the same transaction as their domain changes. Importer writes never set the
API mutation marker and do not create outgoing work. Independent entities run
concurrently; each entity's intentions execute in insertion order. An error
blocks later intentions for that entity until it is resolved.

The sender uses no explicit database/application locks, revisions or leases.
Each SQL statement commits before HTTP or a dependency wait. Missing parent IDs
are reread after 10 ms. `LISTEN workspace_zulip_outgoing` wakes the dispatcher;
a one-second poll recovers missed notifications. Native rows keep empty
`provider_identity`; external copies are recorded in `zulip_entity_links`.

`[zulip_outgoing] concurrency` and `connections` default to 128. The HTTP connector
opens connections on demand, reuses them and closes connections idle for 15 seconds.
Zulip rate-limit responses delay retries. A prepared request is durable before
HTTP; after an uncertain result or restart, GET verifies the operation. Unknown
message sends await their correlated event receipt; unknown uploads use a stable
UUID-prefixed filename. If GET cannot establish the outcome, the intention stays
`checking` instead of risking a duplicate. Deletes contain their required
snapshots and treat 404 as success.

Zulip topics are created by their first message. Stream deletion archives the
channel. Workspace-only attributes (colors, pins, summaries and Workspace roles)
stay local. Exported attachments retain their blob until queued uploads finish.

Migration 0265 moves pending old outgoing tasks/events into intentions and removes
the old outgoing dispatch path. Stop the previous sender and import processes
before upgrading; then start the new sender, realtime receiver and history importer.
This cutover is forward-only; rollback requires the previous database backup.

Realtime imports schedule bounded reconciliation of only their affected messages,
streams and topics, so publishing them does not wait for the global history
reconciliation queue. Shared database locks can still cause waits between queues.

The `[history_import] step_interval` option controls idle checks (default: three
seconds); successful work continues immediately.
Worker completion also renews the immediate pacing, so a completion during a
dispatcher iteration cannot lose its wakeup and wait for the next idle check.
Ready tasks run by priority descending, message creation time and provider ID
descending, then task creation time ascending and UUID. Independent accounts may
finish in a different order. Stored-content conversion keeps its sequential
newest-first batch chain, while prefetching references and deliveries for the
whole batch instead of reading them separately for every message.
Legacy backfill import tasks cover at most 100 consecutive message IDs, newest
ranges first. Migration 0237 splits unfinished larger tasks, preserving their priority,
schedule, status, account, cutoff, and backfill/event links. The original tasks
are cancelled and detached from backfill completion tracking.

Migration 0251 adds lazy, versioned history fingerprints. New backfills scan fixed
provider-ID windows of 1000 (`1..1000`, `1001..2000`, and so on), including sparse
and partial windows. HTTP fetching and hashing happen before account/task locks.
An unchanged ready batch needs one batch lookup, without loading its individual
fingerprints or creating `import_history` processing tasks. Cursor, catalog and
task bookkeeping still run. A changed batch uses one bounded fingerprint lookup
and creates children containing at most 100 changed observations each; sparse
children can span the wider source-ID window.

Each account/message fingerprint separates raw UTF-8 text, known user flags,
source metadata and normalized reactions. Flags belong to the observing account.
Metadata includes the author, source/edit timestamps, message type, placement,
`is_me_message` and direct-message participants. Missing reaction data is unknown,
not an observed empty set. Component hashes use SHA-256 and canonical ordering;
batch hashes also cover the ID range and ordered message IDs. The cache context
includes the subscription, hash format and conversion versions, excluding queue
IDs and credentials. Changing identity, ownership or relevant source settings
therefore requires a new comparison.

`bridge_zulip_v5_history_batches` stores range hashes, a revision, an observation
barrier and pending child UUIDs. `bridge_zulip_v5_message_fingerprints` stores the
component hashes. A batch becomes ready only after its children succeed and its
revision still matches. Retries and cancelled children cannot confirm it. Equal
snapshots advance the batch observation barrier without rewriting message, flag
or reaction state. Newer observations, including same-value events, fence slow
snapshots; the barrier is separate from user flag/reaction change timestamps.
Relevant realtime, delivery and local mutations invalidate the affected ranges.
Migration 0252 excludes application of a known history snapshot from that fan-out:
another observer's replay does not change what this observer previously read.
Each observer still compares its next fresh provider snapshot independently.
This prevents shared reaction/content writes from cancelling sibling snapshot
children or taking another observer's account/cache locks. Changes outside the
transaction-scoped history replay marker retain the ordinary invalidation fences.
Migration 0253 also excludes writes to the derived reaction `fact_uuid` link.
Reaction projection can fill or rewrite that link while sibling history tasks
are pending; it does not change the provider snapshot. Newer observation times
and real reaction changes still invalidate the affected checkpoints.
An absent source ID withdraws that imported observer, not the shared canonical
message. Pending native deliveries retain their existing duplicate-send guards.
The bounded lookup also includes imported observations not yet represented in
the checkpoint, including records recreated by realtime after an earlier absence.

Flags-only and reactions-only deltas skip body conversion and file access.
Metadata-only deltas also avoid body conversion. Completed processing tasks shed
their embedded snapshot after the final task-status write; the durable cache
does not depend on retaining completed tasks. Existing native projection and
counter queues still have to converge after source processing finishes.

Run the opt-in comparison on the disposable integration database with
`WORKSPACE_ZULIP_BENCHMARK_REPORT=/tmp/zulip-worker-benchmark.json .tox/develop/bin/python -m pytest -q -s workspace/tests/integration/test_zulip_worker_performance.py`.
It compares conversion against commit `e431b88`, then compares a single task
worker with separate realtime/background workers. The I/O comparison emulates
Zulip latency; database operations use PostgreSQL. No live Zulip requests are made.

Migration 0238 adds nullable `execution_time` and `queue_wait_time` fields in
seconds. They accumulate execution and ready-queue waiting across attempts,
for inbound processing. Scheduled delays, retry delays, and active leases
do not count as queue waiting. Existing tasks retain `NULL` until measured.

Message reconciliation acknowledges the revisions it read before processing.
Migration 0236 prevents its own writes from marking those messages again, scoped
to the current connection and batch of project/message pairs. Concurrent writers
and changes outside that batch still advance revisions and remain queued. The
scope is restored before acknowledgement and rolls back with failed processing.
Messages that still differ after processing keep their original queue entries.

The Zulip realtime service runs one asyncio poller per verified, enabled account
with an API key. It reconciles accounts at startup, on PostgreSQL notifications,
and every `[zulip_realtime] step_interval` seconds (default: one second). Changing
credentials or account ownership replaces the poller; disabling or disconnecting
an account stops it. The subscription revision excludes credentials, so a verified
key rotation for the same source resumes its saved queue and cursor. Unrelated
settings and displayed status do not replace the poller or cancel pending work.
Synchronous database work uses a bounded executor and short
transactions, independent of the long-polling requests.

Pollers subscribe to every Zulip event type. Raw events, including heartbeat and
currently unsupported types, are stored in `bridge_zulip_v5_events` atomically
with the subscription cursor. A subscriber in the same service schedules history
tasks for messages, message edits, user changes, subscription/channel changes,
and topic notification changes. Known user/channel/topic changes apply the event
payload directly. Missing user details trigger a single-user fetch; catalog
snapshots are a fallback for events that cannot be applied locally. Other event
types remain stored without creating tasks. Event consumption and task creation
also commit together.

The realtime service consumes flag and reaction changes in separate transactions.
Background reconciliation only projects users, streams, bindings, topics and
messages; it does not consume those same flag/reaction queues while holding
projection and project-event locks.

Registering a new Zulip queue enqueues an internal `full_backfill` event for an
account that has never completed import, or `actualize_backfill` for an imported
account. Both use fingerprint scans over visible history, including old edits and
reactions. Registration fixes the boundary for new messages. A previously retained
tail above a smaller boundary is also checked for disappearance; observations
created after the backfill was queued do not widen that check. The 30-day cutoff
remains in legacy task payloads
for compatibility, rather than limiting the new snapshot comparison. The API
only schedules connection verification when connection settings change; it no
longer starts backfill or cancels existing imports for a metadata update.
An account becomes `active` after every task in its backfill group succeeds.
User and channel imports do not schedule periodic catalog refreshes. Migration 0232 sets
`has_completed_backfill` only for existing Zulip accounts in `backfill` state.

PostgreSQL channel `workspace_zulip_realtime` carries wakeups (`accounts` or
`events`); durable data stays in the database. Periodic checks recover missed
notifications. Saved queues resume after a service restart; an expired Zulip
queue is registered again and schedules a new backfill. Run one realtime service
instance for this database.

API changes enqueue outbound intentions directly; the realtime receiver only
consumes provider events. The two directions share persisted identity/content
observations for local-echo matching.
Outgoing Markdown uses the pure converter in `workspace/bridges/zulip/message_conversion.py`;
provider references are resolved through direct SQL before sending.

Uploaded Zulip attachments are copied into Workspace file storage with the
destination stream's access rules. Workspace attachments are uploaded through file intentions, with persisted mappings
for retries and echoes.
Temporary download URLs are fetched without forwarding Zulip API credentials.
Externally hosted media retain their original URLs.
Attachments returning HTTP 404 or 410 are replaced with their caption or filename
followed by `(file deleted)`; HTTP 403 uses `(access denied)` instead. Other content
and available attachments still import. Other download failures remain retryable.
Full and partial quotes use persisted message identities, including Zulip headers
with direct-message recipients (`to …`) or channel/topic context (`in …`).
Migration 0261 queues bounded reconversion only for stored messages containing
these contextual headers; source edit times and newer Workspace edits are preserved.
The [format comparison page](docs/zulip_messages_preview.html) includes direct,
group, channel, nested, and selected-text quote examples.
Silent individual mentions remain clickable user references, represented as
`[Name](urn:user:UUID "silent")`. The standard Markdown title preserves silent
notification semantics without changing the user URN. Reverse conversion keeps
the Zulip `@_**Name|ID**` form. Selected rich snippets use quote snapshots to keep
their clickable content. Name-only and ID-only references resolve against the
persisted user catalog. Mentions inside quote snapshots are silent; mention syntax
inside code remains literal. Zulip user groups and wildcard scopes remain explicitly
unsupported, including their silent spellings, with a link to the original message.
Migration 0262 queues bounded reconversion of all stored mention-bearing Zulip
messages, including snapshots and conversions that had lost their source notice.
Outgoing quotes require
the sender's access to the referenced message. Missing incoming targets retain a
provider URL or quoted snapshot instead of an invented internal identity. Features
that Workspace cannot represent retain an explicit notice and a link to the
original message.
Malformed URLs, reference URNs, and Base64 or UTF-8 quote snapshots remain verbatim
as text. A parsing failure affects only that element; conversion continues for
the rest of the message.

Migration 0241 schedules bounded conversion of existing imported content without
changing the provider's edit timestamps or overwriting newer local edits. Failed
records retry independently. Disconnected accounts can convert stored text;
attachments without local copies retain their provider URLs until a later import
can download them. Only `markdown` payloads enter the conversion path.

Migration 0242 indexes repair queues by original message time and Zulip message
ID. Content conversion, file transfer, message projection, flag reconciliation,
and reaction reconciliation process recent messages before older history.
Refresh tasks also traverse known messages from newer IDs to older ones.
Realtime content and attachments are converted before Workspace publication and
before an outgoing Zulip send, including forwarded snapshots. Provider events
retain their original ordering so newer state cannot be undone by older events.

Delivery bindings retain the Workspace UUID and the Zulip message ID. After
delivery, the canonical message and placement creation dates are updated to the
Zulip timestamp, and a Messenger update event is published. Local echo and later
imports update the original message instead of creating a duplicate. Persisted
Workspace/Zulip content snapshots preserve the original Workspace representation
when its own send or edit returns as an echo. Later provider edits are converted
back to Workspace, and older observations cannot overwrite a newer one. A send
attempt commits before the HTTP request; an ambiguous send outcome waits for
local echo rather than automatically posting another message. Once an ID is
known, metadata fetches can be retried independently of sending.

Priorities are numeric: users 500000, channels 450000, realtime message creation
300000, realtime edits 250000, reconciliation (including topics) 200000, and
history batches 10000 or lower. Missing prerequisites retry after three seconds;
database deadlocks retry the same task after one minute without changing the
account status. A retry also survives a database deadlock in an error callback. There are no task
dependency chains. Superseded edit tasks do not overwrite a newer local edit.
Migration 0245 requeues history imports left in `error` by older workers when the
stored error starts with either PostgreSQL's deadlock message or its RestAlchemy
wrapper. It retains task identities, payloads, priorities and source-event links;
other errors and completed or cancelled tasks are left alone.
Migration 0246 cancels old periodic catalog tasks, guards account triggers against
unchanged routing fields, and updates saved subscription/task revisions without
replacing queue IDs or cursors. It also requeues user imports stopped by a deadlock.
Future read and reaction tasks must also run below message creation priority
and retry when the target message has not been created yet.

Unicode reactions and message shortcodes use `workspace/common/unicode_emoji.json`. Its separate
Workspace and Zulip dictionaries preserve shortcode meanings across providers;
conversion uses Unicode codes, including skin tones, ZWJ sequences, and keycaps.
The catalog records its Zulip and emojibase versions. Regenerate it from the
server's published `emoji_api.json` and the installed Workspace UI dependencies:

```sh
.tox/develop/bin/python tools/sync_emoji.py \
  --zulip-data https://zulip.example.com/static/generated/emoji/emoji_api.json \
  --zulip-version 12.1.2 --ui ../workspace_ui
```

Existing Workspace canonical names remain stable during regeneration. Unicode
support in Workspace is independent of the connected Zulip version: unsupported
Zulip reactions remain local and retain an explicit synchronization error. When
a server reports an additional Unicode emoji, its observed code and name can be
used to update that reaction without guessing or losing modifiers. Migration
0240 normalizes existing symbol/shortcode duplicates and preserves intent times.

Nginx exposes:

- Common REST API: `/api/workspace/v1/{users,services,me,events,epoch}/...`
- Messenger REST API: `/api/workspace/v1/messenger/...`
- WebSocket API: `/api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`
- OpenAPI spec: `/api/workspace/specifications/3.0.3`
- Upload request limit: `50m`

The backend image contains only the Workspace services and their internal nginx
API gateway. The independently versioned `workspace_ui` element owns the public
load balancer, serves its immutable web artifact at `/`, and proxies `/api/` to
the `backend_node` resource exported by this element. Backend releases no longer
resolve, build, or package UI source.

For a tagged backend revision, CI publishes the versioned backend element and
refreshes `workspace/latest`. Updating the UI does not rebuild or replace the
backend VM.

`GET /v1/messenger/server_settings` is public and is handled by middleware for
Workspace client bootstrap behavior. Its `realm_icon` is a public URL
URN derived from the canonical request realm as
`urn:url:<realm>/logo-512x512.png`; the `workspace_ui` load balancer serves that
path from its versioned web artifact. All other Workspace and Messenger
resources are scoped using the IAM bearer token, its user UUID, and its project
ID.

An event cursor is the pair `(epoch_generation, epoch_version)`. A cold
connection may use `last_epoch_version=0` without a generation; every non-zero
resume must include the saved generation. After missed events, the websocket
sends a `ready` control frame before it can deliver live events.

## Storage

PostgreSQL is the source of truth for messages, membership, user state, events,
external-account registration, and client settings. Messenger has no SMTP, IMAP, Exim,
Dovecot, or Maildir runtime dependency.

Files use the configured S3-compatible storage backend. PostgreSQL stores file
metadata and ACL state; S3 stores file bytes and JSON sidecars. Messages contain
authorized URNs, never binary MIME parts.

## Local development

The project virtual environment is expected at `.tox/develop`.

Messenger migration `0231` retires duplicate compatibility storage. Its application
revision requires a coordinated writer shutdown and a verified database backup;
see [the cutover procedure](docs/en/messenger_domain_model.md#retiring-the-compatibility-schema).

For native Workspace installations and upgrades from the legacy bridge, see the
[element upgrade requirements](docs/workspace_v5_upgrade.md).

The tracked `etc/workspace/workspace.conf` intentionally contains no IAM JWKS
decryption key. For local development, copy it to the ignored local config and
set the key there:

```bash
cp etc/workspace/workspace.conf etc/workspace/workspace.local.conf
```

Pass `--config-file etc/workspace/workspace.local.conf` to local services and
utilities that need IAM authentication. Never commit that file. Deployed
elements receive the key from the Exordos Core secret import and do not use the
local config.

Useful RESTAlchemy utilities are available there, including:

- `.tox/develop/bin/ra-new-migration`
- `.tox/develop/bin/ra-apply-migration`
- `.tox/develop/bin/ra-rollback-migration`
- `.tox/develop/bin/ra-rename-migrations`

Apply migrations with:

```bash
.tox/develop/bin/ra-apply-migration --config-file etc/workspace/workspace.local.conf --path migrations
```

Before upgrading an installation that predates the project-dense message
sequence, take and verify a PostgreSQL backup. The compact-state compatibility
steps `0142` and `0143` are intentionally forward-only because a rollback would
renumber messages after bitmap coordinates had been published. Once either
step is applied, rollback requires restoring that pre-upgrade database backup;
do not use `ra-rollback-migration` for this upgrade path.

Use the `admin/admin` account for local manual checks when the environment
provides the test IAM user.

## Test execution

Run the full local integration suite against a disposable PostgreSQL database:

```bash
WORKSPACE_TEST_DB_URL=postgresql://workspace:pass@localhost:5432/workspace_test \
  tox -e 3.12-integration-nocoverage
```

CI runs the same integration cases in four jobs, each with its own PostgreSQL
service. `--integration-shard INDEX COUNT` assigns each test module to exactly
one job using its stable relative path. Its tests and parameterized cases retain
their order and share the same module-scoped fixtures as an unsharded run. For
example, run the first of four shards with:

```bash
tox -e 3.12-integration-nocoverage -- --integration-shard 0 4
```

Omit the option to run every case. Shards must never share a database: migration
fixtures switch the entire schema and configure process-wide ORM state. Every
shard remains mandatory through the `Integration` check, and each uploads its own
JUnit results. `--durations=20` reports setup and teardown as well as test calls.

The Python 3.10–3.14 unit and functional jobs use the `nocoverage` tox factor.
Coverage is still collected and reported separately with `tox -e begin,3.12,end`;
other tox environments retain coverage by default. CI runs for every PR, pushes
to `master`, and tags. A newer PR revision cancels that PR's obsolete run.
