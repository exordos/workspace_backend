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

Run one `workspace-messenger-worker` instance per database, as declared by the
element manifest. It creates one independent unread-counter process regardless
of the projection-worker count. Counter scopes are processed sequentially;
producers only advance their queued revision while COUNT is running.

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

Initial registration saves its catalog and fixed realtime boundary for the account.
The separate `workspace-zulip-history` process imports history silently in durable
pages of up to 1000 messages, newest first. Accounts of the same project/provider
advance at approximately the same message-ID frontier: a newly connected account
catches up before existing accounts continue into older history. Different providers
can run independently. A process restart resumes the saved page; a new realtime
queue registration establishes a fresh boundary and repeats the history scan
idempotently, including any gap while disconnected.

Each page prepares Workspace-shaped temporary tables for contents, messages,
placements, visibility, sparse flags, reactions and files. Attachment hashes identify
reusable blobs; missing bytes reach the configured S3 storage before the merge.
`INSERT ... SELECT`, `UPDATE ... FROM` and scoped `DELETE` statements merge the page
in one transaction, together with its cursor. Existing identical rows stay unchanged.
History emits no message WebSocket events or provider outgoing intentions. The
independent counter process recomputes affected topic, stream and folder counts.
Concurrent changes to old messages and personal flags may be overwritten by the
import snapshot; this deliberately avoids a second conflict/replay mechanism.

`[zulip_history]` defaults to pages of 1000 messages, four HTTP connections and four
concurrent providers, with one active page per provider. History yields when realtime
work is delayed by 0.5 seconds or the project's publication backlog reaches 256.
The per-message history-intent queue, rolling repair scanner and stale-snapshot
comparison path are retired. Upgrading requires stopping incoming/history writers
and a verified database backup; the forward-only cutover preserves Workspace data,
removes old importer work and requests fresh realtime registration.
After a successful merge, its downloaded JSON batch is deleted in the same
transaction and its temporary tables disappear at commit. Failed batches remain
available for retry. Workspace records and attachment bytes stay in their normal
storage.

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

One-to-one Zulip messages resolve the selected direct peer in the sender's
project and provider, including imported contacts without credentials or a
local stream subscription. A missing peer identity leaves the send pending.
Group-message recipients come from the group's active bindings. A group send
waits until every member has a provider identity rather than sending to a
partial group or back to the sender alone.

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
0286 before starting the new workers and WebSocket services. Migration 0266
preserves live queue cursors and received events while retiring history tasks;
0267 enforces local native entities, 0269 retires queued delivery snapshots,
0270 orders independent incoming messages separately, and 0271 deduplicates active
dependency intentions. Migration 0272 avoids duplicate incoming counter scope
invalidations; 0273 introduces immutable message contents and per-message revision
history; 0274 adds resumable history reconciliation; and 0275 makes content hashes
safe to restore with an empty search path. Migration 0285 separates message
visibility from sparse flags and counters; 0286 replaces history intentions with
bounded bulk batches. Start `workspace-zulip-history` alongside
the incoming and outgoing services to import history silently. These
cutovers are forward-only; rollback requires the backup. Deploy the matching Web
UI after the backend because provider metadata now uses the flattened v6 fields.

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

## Completed service history retention

The primary `workspace-messenger-worker` maintenance process collects successful
service work in bounded transactions. Configure its lifetime in seconds:

```ini
[messenger_worker_agent]
history_retention_seconds = 3600
history_prune_interval_seconds = 60
history_prune_batch_size = 1000
```

The default history lifetime is one hour. Each pass removes at most the configured
batch size per category, with a maximum of 1000. Full batches continue on the next
worker iteration until the backlog drains; otherwise maintenance waits for the
configured interval. Projection-only processes do not run maintenance.

Collection includes completed domain outbox aggregates (tasks and fanout rows
cascade from their root), successful Zulip inbox events behind the durable queue
cursor, completed outgoing intents, expired standalone conversion cache entries,
and content pages without references. The outbox and all fanout descendants must
have completed before the cutoff. Pending work, unresolved failures and dead
letters remain available for recovery. Message content pages remain pinned by
current messages, every edit revision and delivery snapshots. Outgoing conversion
cache/pages are preserved while any unfinished message intent may still use them.

Browser event replay retains its separate existing `event_retention_seconds`
setting, and mutation receipt deduplication retains its separate
`[messenger_mutations] receipt_retention_seconds` setting. Service history expiry
does not shorten either contract. Deleted PostgreSQL rows become reusable space;
this worker does not run table rewrites or index rebuilds.

## Native encrypted Android push

Migration `0287-Use-SDK-events-for-native-mobile-push` follows master migration
`0286-Replace-per-message-history-intents-with-bulk-import-batches` and applies
the SDK event-table migration. The new migration UUID makes already deployed development builds traverse
the new master dependency before reusing the existing SDK table. Prepared encrypted
SDK events and previous migration records are retained; pending internal snapshots
become topic signals without changing their UUID, timestamp or status. Message, topic, stream and read-up-to
operations use the same independent counter worker, including mutation-journal mode.

The existing `PUT/DELETE /api/workspace/v1/push_devices/{device_uuid}` API
registers a device's FCM token and raw X25519 public key under the authenticated
user/project. Private keys stay on the phone. The registration UUID stays stable
when a token changes; there are no registration revisions.

When `[mobile_push]` is configured, a changed recipient topic counter snapshot
produces `notification_topic_update`: one SDK Event per registered Android device.
The existing counter worker compares all three counts before updating them; an
unchanged snapshot produces no push. Journal and ordinary mutation paths use
this hook, including message/topic/stream reads, read-up-to and deletion.

The click target and preview use the first visible unread public message placement
in canonical history order. Once everything is read they use the last visible
message. If the topic has become empty after deleting its last message, a zero
update has `message_uuid: null` and removes the card by topic identity. Muted chats
produce no events. Mentions-only chats produce updates when the active counter
changes, including the zero that closes the last mention. Topic overrides retain
the existing Workspace meaning. History import and protocol-only key messages do
not create alerts.

`AsyncEventClient` saves one timestamped `WorkspaceTopicSignal` SDK Event in the
counter transaction into `gcl_sdk_events`. It contains project/user/stream/topic
identifiers, the source WebSocket event UUID and the previous active-count hint.
The SDK Event's `created_at` records when the signal was created. Counting uses
master's unchanged SQL; no device lookup, message lookup, encryption or network
request runs on this path. History-only counter updates remain silent.

The independently managed `workspace-push-handler` reads signals after their
ordinary WebSocket publication commits. It re-reads current access, notification
settings, topic counters, first unread preview and devices. A dirty counter scope
waits for its recount before preparation; delayed signals therefore use current
counts and preview together. Preparation uses the latest visible topic publication whose counters match the
current state, with its existing `epoch_version` and the user's `epoch_generation`.
Pending topic publication is allowed to commit first. The original signal event
supplies only the membership guard, not the outgoing ordering epoch. Encryption happens
outside the database transaction. Each device delivery has a UUID derived from
that WebSocket event and recipient/device identity. Ready SDK Events are enqueued
through `AsyncEventClient.send_event()` and commit with signal completion.
Only encrypted previews are persisted for delivery; the HTTP adapter refuses
signals. Nothing plaintext leaves Workspace. Existing end-to-end encrypted chat
content stays opaque to the backend and uses a generic encrypted-message preview.

The separately managed `workspace-push-sender` subclasses SDK `EventSenderService`.
Its standard loop owns Notification delivery, retries and status polling; its
queries select only prepared `WorkspacePush` payloads, never signals. The handler
also resumes converted development signals that already had `IN_PROGRESS` status. Push hooks
check current membership/device access, redact transport data and handle results.
Network calls hold no database/schema/device locks. Retries reuse the UUID,
ciphertext and original one-hour deadline. The captured membership fence stays
private and is excluded from the Notification HTTP body. An inactive-token result
removes only the exact delivered token/key snapshot, preserving replacements.
The SDK dependency is pinned until its sender-hook release is approved; released
3.7.5 is unchanged. There is no parallel outbox or copied SDK delivery loop.

The optional mobile-push site provisions one handler and one SDK sender per
database, each as its own Core service. Either service can be stopped/restarted
without restarting Messenger or the other push stage. Their pending work stays
in the SDK event table. This topology does not claim concurrent queue consumers
are supported. The handler also prunes old terminal push events once per minute.
No delayed notification policy is enabled: signals are prepared immediately.

Android maintains one card per saved connection/account/project/stream/topic.
An absolute snapshot replaces its count, first-unread preview and click target;
zero active unread closes it. The existing WebSocket cursor orders duplicates
and delayed snapshots, including all-read updates received by an open client.
There is no new registration or notification revision. Counter increases that
create a user-visible alert use high FCM priority; decreases/zero use normal.
Notification's owner-scoped binding API remains the transport source of truth;
Workspace's preference proxy and Android preference UI remain deferred.

Configuration keys are `mobile_push.organization_uuid`,
`mobile_push.notification_endpoint` and `mobile_push.notification_auth_file`.
The local Notification transport exports the persistent installation UUID;
the connector imports it into Workspace configuration.
The feature is disabled until the endpoint and private token file are set.
When upgrading an existing installation, update the backend image first, then
update the optional mobile-push site to provision its handler and sender services.
When disabling mobile push, disable both site services as well.
The file contains `{"access_token": "<platform-managed token>"}` and has mode 0600.
The [optional site manifest](exordos/manifests/mobile_push.yaml.j2) uses the same
`$core.iam.tokens` auto-renewal as Core email events. The sender reads the token
file before every request through its redacted, redirect-refusing SDK transport. Renewal does not restart the API or workers and
does not perform a second password login. The manifest connects the sender to
local Notification and provisions a protected Event
proxy through the existing Workspace gateway. Services read drop-ins from
`/etc/workspace/workspace.conf.d`. Firebase sender credentials belong only on
the upstream Notification, never in Workspace or customer installations.

The complete event, encryption, relay and deployment contract is documented in
`exordos_notification/docs/mobile-push.md`. Android integration is documented in
`workspace_android/docs/mobile-push.md`.

Tests include public HPKE vectors and a shared Python/Android event fixture,
plus real PostgreSQL/HTTP integration of first-unread topic snapshots, all read
scopes, deletion, retry, membership fencing and stale-token cleanup. Run the latter against a dedicated
empty test database only:

```bash
WORKSPACE_TEST_DB_URL='<DEDICATED_TEST_POSTGRES_URL>' \
  .tox/develop/bin/python -m pytest workspace/tests/integration/test_mobile_push.py \
    workspace/tests/integration/test_topic_push.py
```

Its fixture recreates the test database's public schema. Use immutable
`exordos build`/`exordos deploy` artifacts for runtime updates and preserve a
Workspace database backup before applying migrations. A queued event or an
`ACTIVE` transport status does not prove display on a real phone.


The native push producer refuses HTTP redirects and redacts request/response
bodies at every log level. The platform renews IAM tokens; issued tokens and encrypted device snapshots
are omitted from HTTP diagnostics, including provider failures caught by the
shared SDK sender. API diagnostic request/response bodies are also redacted, including
registration requests and private messages. Methods, URLs and response statuses
remain available.
