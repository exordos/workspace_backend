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
  personal bindings, source-reference policy and file mappings.

A matching block skips JSON queue persistence, conversion, attachment preparation,
relation staging, message merge and counter invalidation. A personal-state change
uses existing placements and updates only unread/star records and silent counters.
Missing bindings or changed data/context use the regular bulk importer. Receipts
are never shared between accounts, even when canonical messages are shared.

Shared imported messages keep provider message links as `urn:url:` links to
Zulip, including message ID references in channel tokens. Quotes retain a snapshot
of the text supplied in the source message and a provider link. Conversion does
not look up local targets or fetch their original bodies. This keeps payloads
stable across accounts and prevents guessed provider IDs from revealing whether
an inaccessible target exists locally. Source links open through Zulip; native
Workspace references retain their existing navigation and access checks.

The source-reference policy changes the context fingerprint only for blocks
containing message references. Those blocks are reconverted on their next scan,
replacing older locally resolved links. Blocks without message references retain
their previous fingerprints. Receipt rows and cursors stay in place.
Both `near/ID` and `with/ID` provider links participate in invalidation. The same
versioned reference policy enters conversion-cache keys, so older quote snapshots
are not reused under the new policy. Nested source quotes retain the existing
depth and encoded-size limits.
Source quote snapshots occupy their own Markdown block; the provider source
link follows in a separate paragraph. Current clients recognize an immutable
forward snapshot only when it is the sole link in its block. The versioned
policy also reconverts receipts and cached conversions with the earlier inline
layout on their next scan.
Nested supplied quotes use Markdown blockquotes inside that snapshot, so clients
do not mistake repeated references to the importing message for a quote cycle.

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
Imported edit times are included in projection validity. Migrations 0303 and 0304 preserve
an explicitly supplied source edit time during incoming conversion, including
reference refinement. Migration 0304 also upgrades an already applied 0303. Its column-specific
`BEFORE UPDATE` trigger marks only an
incoming row whose statement includes a non-null `content_updated_at`; the content
version trigger consumes that marker for the same row. Omitting the column does
not inherit an explicit-time marker from a previous statement. Local edits and
changes without a source timestamp still receive a local edit time, subject to
the existing derived-projection guard. Old blocks with divergent local timestamps
need one regular merge to restore their source time before subsequent scans skip
them; receipts are not globally discarded.

Migration 0304 explicitly depends on the intervening poll migrations as well.
The first published PR build used the 0303 UUID under a 0301 filename, before
those migrations existed. The migration engine skips dependencies of an already
applied UUID; the explicit dependency ensures that upgrading this older build
also installs the poll schema.

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
A matching data/state block validates its context and advances the cursor in one
transaction. It does not rebuild the same context a second time. Flag-only work
still rechecks context after preparation, and failed commits retain the saved
frontier and retry path.

The PostgreSQL history integration suite covers repeated scans after worker
restart, partial-import re-registration, prefix insertion, deleted boundaries,
empty/expanded ranges, data/reaction/flag changes, identity and membership changes,
missing personal bindings, transaction rollback, disconnection on a cache hit,
retrying attachments, upgrading pending pages and silent WebSocket behavior.
