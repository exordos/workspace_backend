# Transactional message deletion

Migration 0287 changes message deletion from `deleted_at` followed by worker
cleanup to physical removal of the canonical root in the command transaction.
The database cascades its placements, visibility bindings, sparse flags and
reaction facts. Stream and topic deletion retain their existing lifecycle.

With the mutation journal disabled, a successful HTTP DELETE has already removed
the root. With the optional journal enabled, HTTP accepts a durable command;
physical removal occurs when that command is applied. Event and provider
delivery still complete asynchronously.

## Capture before removal

A database trigger runs while the message and its visibility bindings still
exist. It repairs shared last-message pointers before queuing dirty counters,
matching the order used by other message writers. It freezes local deletion
audiences into existing domain outbox events, in batches of at most 1000 users,
with each user's original membership generation. Existing projection workers
can deliver these snapshots without reading the removed message. Explicit
generation fences override automatic guards stamped at publication time, so a
later membership cannot receive the earlier deletion.

The existing provider capture function also runs before DELETE. Outgoing delete
intentions retain routing, account and provider identities before cascades remove
placements. A pending send followed by deletion can still finish from its durable
send receipt and then delete the resulting provider message.

Incoming provider deletions persist their publication snapshots and remove the
canonical root in one transaction. They do not produce outgoing intentions.
Rollback restores the message together with its original visibility and rolls
back the captured deletion work.

## Preventing replay from restoring a message

Deletion keeps provider identity links in `zulip_entity_links`; the links carry
identifiers, without a message body. A link or send receipt whose canonical root
is absent distinguishes a removed message from one not yet imported. Lookups
are scoped by project, provider and remote message ID.

Realtime copies check these receipts during preparation and application,
including personal copies after the shared-event cache expires. History checks
them before normalization and again in its merge transaction. Existing roots
are locked in UUID order while merging, so removal and an update serialize.
A page prepared before deletion excludes the removed root and its dependent
staged visibility, flags and reactions instead of restoring them.

## Upgrade

Back up the database and stop message writers and projection, incoming, outgoing
and history workers before applying migrations through 0287. Start the matching
application and workers afterwards. The previous deadlock fix remains necessary
while queued soft-delete work from the old version drains. Those tasks retain
their original delivery flow; new deletion commands no longer create message
tombstones for worker cleanup.

The independent mutation-journal delete receipts remain unchanged. Immutable
content history, event retention and attachment retention are separate policies;
physical removal of the message root is not an erasure of those stores.
