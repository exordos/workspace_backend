# Resumable external history import

The private bridge API accepts frozen Zulip history batches. A dedicated
`workspace-history-import-worker` prepares and imports them while foreground
message APIs retain their existing process and connection pools.

## Contract and ownership

`GET /v1/history-imports` advertises `schema_version: 1`. The bridge waits for
this capability before publishing; an older backend leaves capture local.
`POST /v1/history-imports` accepts the envelope in
`workspace/tests/fixtures/history_batch_v1.json`. Its `batch` is the bridge's
minimal stored JSON, unchanged. The fixture is shared with the producer.

The envelope adds `project_uuid`, verified `provider_realm_uuid`, monotonic
configuration `generation`, and the complete current `sources` list. Each
source names `account_uuid`, `account_generation`, `chat_uuid` and
`assignment_generation`. The server verifies these against current desired
resources and verified account owners. The directory contains all returned
Zulip users plus referenced historical identities resolved individually; only positive observations by selected, verified account owners
can create message access. `read` and `starred` are separate per-user booleans.
History depth remains per account/chat and is checked before file discovery,
topic resolution and pending quote mappings. Historical channel topics absent
from the current catalog use its existing stable projection identity algorithm;
the short write registers the topic and its routing catalog entry together.

A batch covers an inclusive aligned range of 5000 source IDs, not necessarily
5000 accessible messages. Identity is `(provider_realm_uuid, message_id)`.
SHA-256 covers compact, sorted-key UTF-8 JSON without Unicode normalization;
message hashes exclude their own `hash`, and the batch hash includes message
hashes but excludes its own. Array ordering is part of the contract. Job UUIDs
include bridge identity generation, project, realm, capture generation, range,
hash and source revisions.
Identical retries return the same durable receipt; a changed revision is a new
job even when source content is unchanged.

Version 1 fills missing history. It preserves existing canonical message
content, reactions, placement and user flags, including newer live edits.
It neither interprets absence as deletion nor treats the capture generation as
a Zulip edit version. Updates to already imported history require a later
source-version contract. Current and future soft deletes create durable source
tombstones, including after physical purge. Previously purged messages cannot
be reconstructed as tombstones; deployment restarts bridge capture before
publishing old caches.

## Storage and preparation

Ingress authenticates mTLS and validates input, briefly checks SQL authority,
then stores the immutable raw batch in S3 with no SQL connection held. A second
short transaction rechecks authority and registers it. Admission is limited to
two simultaneous requests, 64 MiB per batch and eight active jobs per bridge.
Queue admission is serialized only during SQL registration.

History intentionally has no request-wide SQL transaction. This private
`ThreadingHTTPServer` route is dispatched by `server._dispatch_history`, which
owns body allocation and HTTP admission. `HistoryImportService` orchestrates
the storage and SQL phases using the injected RestAlchemy request-context
session factory; repository and writer functions receive each current session
explicitly. These SQL helpers open no engine or transaction. The ordinary
provider routes keep their existing single request transaction. Wrapping the
history route in that transaction would retain its connection and locks across
storage I/O, contrary to the import contract.

One import scope supports at most 512 selected account/chat sources. Larger
source sets are rejected before storage or account locks; the message volume
itself remains unbounded across successive batches. The maximum source envelope
remains below 100 KiB while the private import endpoint allows a 50 MiB request.
This bounds authority work that cannot be reduced by shrinking a message part.
Message content must satisfy the canonical Markdown domain (1–40,000 characters),
both at admission and after conversion, before any canonical message is written.

The worker discovers source attachment paths only for missing canonical messages
in the current part and returns bounded requests in
`GET /v1/history-imports/{uuid}`. The bridge downloads them only through the
listed current accounts, including eligible observers of a shared path in later
parts. Requests are staged in pages of at most 100 files, reduced with the write
part size. The current page is persisted separately from obsolete requests, so
worker restarts and concurrent live arrivals do not wait for unused attachments.
Before publishing a request page, a short transaction takes the same provider
message identity locks as canonical writers and rechecks absence. A live arrival
before that commit invalidates preparation; arrivals after publication do not
cancel an already started transfer. Existing messages and tombstones do not
require source attachment downloads.
The bridge sends bytes with `PUT .../{uuid}/files/{file_uuid}`,
`X-File-Name` (URL encoded) and `Content-Type`. Each file is limited to 50 MiB and the current provider policy may lower that limit.
After definitive provider 404/410 responses or when the configured transfer limit forbids the file, it may instead call
`POST .../{uuid}/files/{file_uuid}/unavailable`. The server converts that link to
a visible unavailable marker. Network failures remain retryable.

File bytes use content-addressed S3 keys; file metadata and explicit per-user
ACLs remain separate. S3 writes, metadata writes, Markdown parsing, link and
mention resolution, model construction, hash calculation and SQL-array
encoding all finish before the final write transaction. Resolvers use prepared
in-memory mappings, never per-message SQL or provider requests.

Raw batches and file objects are retained for resumability. Failed admission or
process interruption can leave unreferenced S3 objects. There is no automatic
object deletion in this version: any later retention job must check both
import jobs and canonical file references before removing objects, particularly
shared content hashes. Batch tables and checkpoints are bounded per request,
while completed receipts and canonical source hashes grow with imported data.

## Short writes and foreground responsiveness

A source batch is not a transaction. Each prepared part has at most 100
messages, 2000 estimated dependent rows and 1 MiB of source JSON. A single
message exceeding the dependent-row or byte budget is rejected explicitly.
Directory writes use bounded pages too. The worker caches at most one batch.
It keeps affinity while that batch progresses, but releases affinity when an
error backs it off so other ready jobs can continue.

Existing message file ACLs are restored separately in pages of at most 1000 rows;
shrinking the part to one message reduces an ACL page to 10 rows. A durable
offset and content/observer fingerprint resume these pages without allocating
the full file-by-observer product. A changed preserved payload or observer mapping
restarts ACL traversal; only completing every page advances the message checkpoint
and counters. Malformed file URNs remain literal preserved text.

Final SQL transactions acquire the same identity, account, read-state and
project fences used by live operations. They recheck authority and prepared
identities/placements, insert missing canonical records in bulk, apply counter
deltas and atomically advance the checkpoint. They never scan the full message
history or perform storage/network work. Bounded coordinate joins use point
lookups so PostgreSQL cannot turn them into full-project scans as data grows.

`lock_timeout` is 50 ms and `statement_timeout` is 500 ms. A 200 ms total
pre-commit budget rolls back oversized work and reduces its part size; a fast
successful write gradually restores it. Lock contention yields quickly and
retries without collapsing the part size. These are budgets, not hard bounds
on commit/fsync latency. The dedicated process uses a maximum of two database
connections and pauses behind more than 2000 pending projection tasks.

Imported counters are updated incrementally. Notifications are coalesced per
user/topic or user/stream and drained in bounded groups; no per-message live
broadcasts or million-message unread recomputation are produced. Existing UI
pagination reads imported messages normally. Stream/topic snapshots refresh
cached counters and folder projections without invalidating each message.

Leases and checkpoints survive worker restart. Every part checks current
account selection, configured depth, source revisions and bridge authority.
Waiting file jobs also revalidate periodically. Twenty consecutive processing or
preparation failures without a committed checkpoint mark a job failed and free
its admission slot, retaining the last safe error. A directory, file-request,
file upload, file-ACL, message or notification checkpoint resets this durable
streak; `attempts` remains the lifetime retry count. Lock contention,
serialization and statement timeouts, connection loss or server shutdown,
write-budget shrinking, lease loss,
provider pauses, storage connection/stream failures, retryable S3 responses
(408, 429 or 5xx), and normal waiting/yielding do not consume this
processing-failure budget. Missing objects, permission errors and invalid local
paths consume the processing-failure budget. Revoked or superseded jobs stop
without deleting already committed canonical data. No transaction spans S3.

## Canonical routing receipts

After SQL completion, the bridge reads authenticated routing pages from
`GET /v1/history-imports/{job}/mappings/{account}/{kind}/{cursor}`. `kind` is
`users` (lexical source-ID cursor, initially `0`) or `messages` (numeric source-ID
cursor, initially `from_id - 1`). Responses have at most 500 mappings plus
`next_cursor`, which is null at completion. Message pages include their topic routing records too, within the same 500-record limit. They contain canonical UUIDs and
routing metadata, not message bodies. Message pages only include the selected
account owner's accessible, currently placed messages. Authority is rechecked
on every page, including after SQL completion.

The bridge atomically inserts missing mapping records and saves its page cursor.
It preserves existing live mappings and tombstones. Replies, reactions and
subsequent live events then use the existing routing paths. A local batch is
fully delivered only after these resumable receipt pages have completed.

## Deployment and acceptance

Deploy the backend first, applying migrations through the latest HEAD (currently
0180) before starting the separate worker, then the bridge and its latest
migrations. The initial upgrade from the legacy
history path rebuilds local captures using the configured depths. Existing frozen
batches are retained when their source configuration is unchanged. Rolling back
the backend code leaves new tables/data intact, and an older backend causes the bridge
to retain local capture without publishing. Do not roll back by wiping data.
Code rollback does not mean migration downgrade: downgrading 0175 drops the
operational history tables and provider-deletion tombstones.

Migration 0180 makes a bridge own its history scopes and operational descendants
through database cascades. Canonical messages, canonical files, S3 objects and
realm tombstones remain independent of that ownership. It first rejects scopes
whose original bridge no longer exists; it neither invents a replacement bridge
nor deletes their potentially large history trees during deployment. Recover the
original bridge record, or explicitly clean only the orphan's operational data
in bounded transactions before retrying. For cleanup, first build the three
indexes declared in 0180 with `CREATE INDEX CONCURRENTLY`; page hash, file and
notification rows by their keys, remove each empty job, then its empty scope.
Preserve canonical data, storage objects and tombstones throughout this repair.

The upgrade builds full scope-to-job, job-to-hash and message-to-hash indexes
one at a time with `CONCURRENTLY`, including completed jobs. An interrupted
invalid index is rebuilt on retry. The final FK installation and scope-only
validation use 50 ms lock and 500 ms statement limits; contention aborts the
migration for a later retry. They do not scan or rewrite message hashes. A
rolling downgrade removes only the new FK and retains these supporting indexes.
The online index builds run outside those final-transaction timeouts and may
wait for older transactions or snapshots. Inspect `pg_stat_progress_create_index`
and `pg_stat_activity` when a build waits; ordinary reads and writes can continue
while it waits for an old snapshot. Finish the owning transaction normally;
do not terminate unrelated sessions merely to accelerate deployment.
Explicit bridge deletion still costs proportionally to its operational rows;
the indexes prevent repeated full-table scans, rather than bounding that delete
to a fixed duration.

Run the unit and PostgreSQL integration history suites plus the private bridge
API and projection suites. Verify producer/consumer golden fixtures together.
On a disposable database, `python -m workspace.tests.scale.benchmark_history_import
--messages 1000000 --output /private/path/result.json
--initialize-disposable-database` runs real SQL import alongside HTTP reads,
native writes and projection processing. Set `WORKSPACE_TEST_DB_URL` only to a
throwaway database: the explicit initialization flag resets its public schema.
This harness uses memory storage; it does not measure S3 or provider latency.

Live acceptance additionally covers actual provider file transfer and S3,
visible UI under the primary account, read/starred isolation, repeated batches,
restart checkpoints, source removal/depth changes and concurrent realtime
messages. Record p95/p99 client latency, errors, transaction maximum, throughput,
RSS and retries; do not infer zero UI lag from import throughput alone.
