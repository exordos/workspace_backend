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
- Zulip realtime task worker: `workspace-zulip-task-worker`
- Workspace OpenAPI spec: `http://127.0.0.1:21084/specifications/3.0.3`

The single `workspace-api` process serves all REST resources, including Messenger,
with two HTTP workers by default.

The history import service executes the `background` queue with a bounded thread
pool (`[history_import] workers`, default: four). Run `workspace-zulip-task-worker`
as a separate process for the `realtime` queue, in addition to the realtime event
receiver. It has a dedicated execution thread and checks for work every 100 ms.
The equivalent module command is `python -m workspace.cmd.zulip_task_worker`.
Each task-worker process sizes its PostgreSQL pool to its worker count plus one
dispatcher connection: five for the default background pool, two for realtime.
This overrides `[db] connection_pool_max_size` only in these task-worker processes.

Migration 0243 adds explicit task queues and classifies pending tasks by origin.
Live message imports, edits, sends, reactions and flag updates use `realtime`;
backfills, catalog polling, stored-content conversion and historical flag repair
use `background`. Both queues preserve the existing numeric priorities. Stop old
history workers before applying the migration and start both task workers after
updating: older workers do not filter by queue.

Dispatchers reserve only as many tasks as there are free execution slots. Claims
commit before execution and use `FOR UPDATE SKIP LOCKED` and recoverable leases.
Each queue has its own dispatcher lock and an explicit indexed queue filter,
including when PostgreSQL reuses a generic prepared plan. Migration 0244 adds a
small partial index for pending reconciliation and counter-refresh tasks; looking
for these recurring tasks does not scan the history backlog or completed tasks.
Each execution has its own database session. Tasks for the same account within a
queue are serialized; global maintenance and conversion chains each have their
own execution key. Catalog refreshes for a project share a key across both queues.
Transaction advisory locks fence overlapping execution even
after a lease expires. Outbound sends retain their committed attempt marker, so
an ambiguous HTTP result waits for the echo rather than repeating the POST.

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
History import tasks cover at most 100 consecutive message IDs, newest ranges
first. Migration 0237 splits unfinished larger tasks, preserving their priority,
schedule, status, account, cutoff, and backfill/event links. The original tasks
are cancelled and detached from backfill completion tracking.

Run the opt-in comparison on the disposable integration database with
`WORKSPACE_ZULIP_BENCHMARK_REPORT=/tmp/zulip-worker-benchmark.json .tox/develop/bin/python -m pytest -q -s workspace/tests/integration/test_zulip_worker_performance.py`.
It compares conversion against commit `e431b88`, then compares a single task
worker with separate realtime/background workers. The I/O comparison emulates
Zulip latency; database operations use PostgreSQL. No live Zulip requests are made.

Migration 0238 adds nullable `execution_time` and `queue_wait_time` fields in
seconds. They accumulate execution and ready-queue waiting across attempts,
including send preparation. Scheduled delays, retry delays, and active leases
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
account. The latter covers the preceding 30 days. Both use the message boundary
returned by queue registration; neither fetches a later upper boundary. The API
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

The same realtime service independently subscribes to new message creation and
edit records in `messenger_domain_outbox_events`, using the existing
`workspace_projection_tasks` notification channel. Subscriber receipts and
history tasks commit together; Messenger projection processing remains independent.
Migration 0233 starts this subscription with new events, without sending retained
outbox history. The history service executes `send_message` and `edit_message`
tasks using the author's verified external account for the destination channel
or direct conversation. Markdown content is converted in both directions through
`workspace/bridges/zulip/content_conversion.py`. Its context resolves the current
external-account owner, channels, topics, and public message placement UUIDs from
persisted data in the request project and Zulip server. Outgoing references use
the destination server's identifiers. References to messages awaiting delivery
retry until their Zulip IDs are known; unsupported outgoing identities fail
before a send attempt is marked. Zulip messages use `read_by_sender=true`.

Uploaded Zulip attachments are copied into Workspace file storage with the
destination stream's access rules. Workspace attachments are uploaded to Zulip
once per destination stream, with persisted file mappings for retries and echoes.
Temporary download URLs are fetched without forwarding Zulip API credentials.
Externally hosted media retain their original URLs.
Attachments returning HTTP 404 or 410 are replaced with their caption or filename
followed by `(file deleted)`; HTTP 403 uses `(access denied)` instead. Other content
and available attachments still import. Other download failures remain retryable.
Full and partial quotes use persisted message identities; outgoing quotes require
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
