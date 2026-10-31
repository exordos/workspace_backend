# Immutable message contents

Message identity, placement, read state, provider identity and timestamps remain
on their existing records. `messenger_messages.content_uuid` points to a shared
`messenger_message_contents` page containing the entire typed Workspace payload
and optional raw provider Markdown. Native or unconverted content has a null
provider representation. Null and empty provider content are distinct.

Both representations have generated SHA-256 hashes. The pair of hashes is unique;
the resolver also compares the exact payload and provider bytes before reusing a
page. Concurrent inserts converge without updating an existing page. A database
trigger rejects page updates.

An edit resolves or creates a page and switches the message reference. Each
reference transition appends a `messenger_message_content_versions` record with
the previous content UUID. Reverting A to B to A reuses the original A page while
preserving all three revisions. Histories belong to individual messages, so two
messages can converge on the same page with different predecessors. Provider-only
representation changes append revisions too, without changing the public edit
timestamp. Deleting a message removes its revision records through the database
foreign key; its content pages remain. Garbage collection is deliberately absent.

Zulip conversion cache entries reuse `bridge_zulip_v5_content_snapshots`, with a
null delivery UUID. The key includes provider UUID, conversion direction, exact
source content, converter version and resolved conversion context. Cache hits
verify the exact source and context before returning a page. File permissions and
reference mappings are resolved before lookup. Increment `CONVERTER_VERSION` in
`workspace/bridges/zulip/content_cache.py` whenever conversion behavior changes.

Incoming conversion results containing an implicit current-message snapshot,
self-reference or an unsupported-content link to the original message are not
cached across messages. Delivery-specific snapshots take precedence, preserving
the Workspace author's original spelling when Zulip echoes a sent or edited
message. Outgoing preparation records the ready page before the network request;
late preparation can attach it only while its saved message revision still
matches. Concurrent cache misses may perform duplicate conversion work, but share
the resulting immutable page.

Migration 0273 normalizes current roots and retained delivery snapshots, preserves
prepared requests verbatim, and seeds the current revision of existing messages.
It cannot reconstruct earlier edits that were not retained before normalization.
The migration requires a coordinated backend update and a pre-upgrade checkpoint;
automatic downgrade is unsupported.

Client requests, payloads, UUIDs, event formats and pagination remain unchanged.
Read views join content pages and preserve their prior public column sets; content
UUIDs and revision numbers are internal and are not accepted as client fields.

Zulip history can also reuse complete, successfully imported source blocks before
content conversion. See [history block receipts](zulip_history_blocks.md) for
stable boundaries, personal-state updates and restart semantics.
