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

- REST API: `workspace-api`
- Client WebSocket events: `workspace-messenger-events`
- Canonical projections: `workspace-messenger-worker`
- Incoming Zulip events: `workspace-zulip-incoming`
- History import and recovery: `workspace-zulip-history`
- Outgoing Zulip intentions: `workspace-zulip-outgoing`

Run each Zulip service as one separate process with
`--config-file /etc/workspace/workspace.conf`. They use asyncio, direct PostgreSQL
and persistent aiohttp connections. SQL transactions end before HTTP or waits.

The incoming service supervises enabled external accounts and keeps one long poll
per account. It persists each received batch and its queue cursor atomically,
then applies the durable inbox in account order. Different accounts run
concurrently. Duplicate deliveries are ignored by account, queue and event ID.
An object change and its client-event snapshot commit together. The existing
Workspace event projection publishes snapshots over WebSocket, including edits,
reactions and personal flags on old messages. Provider changes never create
outgoing intentions. Native copies retain empty `provider_identity`.

Initial registration persists its catalog snapshot and requests background recovery.
The separate `workspace-zulip-history` process compares catalogs and message pages
against current Workspace state. Only differences enter `zulip_history_intents`;
the same incoming operations apply them without publishing client events.
History updates messages, catalogs, flags, reactions and counter projections in
the database silently, including deferred projection tasks. Clients read imported
data through the unchanged REST API. Normal realtime changes still publish
WebSocket events. Incoming and history changes never produce provider outbox work.

History cursors in `zulip_history_jobs` survive queue replacement and process
restarts. The scanner loads recent messages first, fills gaps from its confirmed
forward cursor, and walks older history backwards. Repeated queue resets coalesce
into one job without resetting backfill or repair progress. A rolling repair also
finds missed edits, reactions and flags on old messages. Unavailable messages are
rechecked individually; access is removed only for the affected account.

`[zulip_history]` defaults to pages of 500 messages, at most 1000 pending intentions,
four concurrent account tasks and four HTTP connections, with one active task per
provider. Page admission is serial; admitted intentions can prepare and apply
concurrently. Accounts take turns after each page or intention. History pauses
when realtime work is delayed by 0.5 seconds or a project's publication backlog
reaches 256 entries. A completed repair restarts after 86400 seconds. Requests and file
preparation run outside transactions. Short snapshot-isolation transactions
check account identity, pending local work and concurrent changes before applying;
stale message snapshots are fetched again. Permanent failures retain their error
for inspection without holding up other intentions.

`[zulip_incoming] connections` limits ordinary provider requests (default 128).
Long polling has a separate connector so waiting accounts cannot block lookups.
Both connectors reuse connections and close idle sockets. `poll_interval`
defaults to one second and recovers lost database notifications;
`dependency_delay` defaults to 10 ms.

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
a one-second poll recovers missed notifications. Native entities are local and
never enter the provider outbox. Entities created in a Zulip context inherit
`source_name=zulip` and `provider_uuid` immediately; `provider_identity` remains
empty until the provider receipt arrives. A canonical message has one destination;
a copy requires a new UUID.

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

Incoming messages only read ready users, streams, topics and personal bindings.
A missing dependency creates one active `ensure_dependency` intention in the
existing inbox, deduplicated by project, provider, dependency type, provider identity
and user for personal bindings. Inserting it and deferring its parent are one short
transaction. The existing dispatcher runs the intention, including any parent
dependencies; successful creation and intention removal commit together and wake
the dispatcher. Catalog updates run in their own provider event handlers.
Permanent failures remain visible with an error and do not block subsequent events.

Before upgrading, back up the database and stop the old realtime, history,
incoming, outgoing and Messenger projection workers. Apply all migrations through
0271 before starting the new workers and WebSocket services. Migration 0266
preserves live queue cursors and received events while retiring history tasks;
0267 enforces local native entities, 0269 retires queued delivery snapshots,
0270 orders independent incoming messages separately, and 0271 deduplicates active
dependency intentions. History import stays disabled. These cutovers are
forward-only; rollback requires the backup.

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
