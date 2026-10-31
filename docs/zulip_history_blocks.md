# Reusing imported Zulip history blocks

The history worker keeps successful account-scoped receipts in
`zulip_history_block_receipts`. The first scan defines stable, half-open source
message ID ranges, normally containing up to the configured page size. A new
registration starts at the newest ID and rechecks the saved ranges. Newly
created messages form a prefix; they do not shift older block boundaries.
Completed receipts remain useful when an earlier scan stopped partway through.

Each receipt contains three SHA-256 digests:

- Message data: IDs, Markdown, sender, destination, imported timestamps and
  reactions, in deterministic order.
- Personal state: message IDs and the imported `read` and `starred` flags.
- Application context: the external identity, converter revision, subscriptions,
  relevant catalog mappings, membership generations, message placements,
  personal bindings, quoted-message identities and file mappings.

A matching block skips JSON queue persistence, conversion, attachment preparation,
relation staging, message merge and counter invalidation. A personal-state change
uses existing placements and updates only unread/star records and silent counters.
Missing bindings or changed data/context use the regular bulk importer. Receipts
are never shared between accounts, even when canonical messages are shared.

The worker still downloads source snapshots. A matching newer block does not end
the scan: edits, reactions and personal flags may change anywhere in history.
Saved lower boundaries are applied before hashing. Deleting a boundary message or
an entire block therefore does not pull older messages into an adjacent block.
If visibility expands inside an existing range, the worker fetches every page in
that range before comparing it; this exceptional merge can exceed the normal page
size. It never certifies a truncated snapshot.
Every returned ID must be below the requested anchor. A repeated or overlapping
page fails the current attempt before persisting messages or advancing the cursor;
the existing retry path resumes once the source returns valid pages.

A successful receipt, merge and cursor change commit together. An interrupted or
failed merge retains the pending page without a successful receipt. Re-registering
replaces an in-flight page but preserves completed receipts. Migration 0287 also
backfills the lower boundary of pending pages fetched by the preceding importer.
The worker also derives a missing boundary when consuming a legacy pending page
written after the migration, using its completion flag and saved next cursor.
Account/settings/cutoff guards apply to cache hits as well as regular merges.
The receipt fingerprints the resolver snapshot actually used during preparation;
a concurrent catalog change cannot certify an older converted payload under a
newer context. Attachment dependencies use normalized upload paths, including
temporary signed upload URLs.
Resolver topics and committed destination topics are fingerprinted separately.
A link to a topic created in the same block can therefore be resolved on the
next scan before the block becomes reusable; ordinary new destinations alone
do not require another full merge.
Imported edit times are included in projection validity. If the existing
content-change trigger stamps a refined reference with the current time, the
next pass restores the source edit time before that block can be skipped.

Temporary inaccessible or unresolved attachments prevent a successful receipt.
They are retried on a subsequent registration. Receipt validity follows relevant
identity/access changes and missing local projection records; unrelated older
imports do not invalidate an already committed newer block. Increment
`history_blocks.FORMAT_VERSION` when import semantics change, and the existing
`content_cache.CONVERTER_VERSION` when conversion semantics change.

Receipts are an optimization of the existing importer. They do not introduce a
second authoritative message store or change its upstream-deletion semantics.
The structured history log records `mode=full`, `mode=flags` or `mode=skip` and
fetch/preparation/merge/commit/total timings. Fetch timing also includes admission,
queue lookup and receipt/context checks, so it is not an isolated HTTP latency.

The PostgreSQL history integration suite covers repeated scans after worker
restart, partial-import re-registration, prefix insertion, deleted boundaries,
empty/expanded ranges, data/reaction/flag changes, identity and membership changes,
missing personal bindings, transaction rollback, disconnection on a cache hit,
retrying attachments, upgrading pending pages and silent WebSocket behavior.
