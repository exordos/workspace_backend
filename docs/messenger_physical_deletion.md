# Physical deletion and prepared unread facts

Deleting a message removes its canonical row and dependent placements, access
bindings, flags, reactions and content revisions in the same transaction. A
compact `messenger_message_delete_tombstones` record retains the canonical UUID
and ingest sequence. Shared immutable content pages, external identities and
outgoing delivery intents have independent lifetimes.

Before the root is removed, a database trigger snapshots the placement,
recipients and membership generations into the existing domain outbox. The
projection task receives that payload at insertion time and publishes it in
bounded recipient batches. Delivery no longer needs the deleted message or its
access rows. Recipients include users who have already read the message. The
same transaction queues counter and summary invalidations for all affected
users. Stream deletion uses its existing membership control event and queues
folder snapshots before its links disappear, even if it has no remaining topics.

Topic and stream deletion remove only canonical messages whose last placement
belongs to the deleted scope. A canonical message with another placement stays
available there. Foreign keys own dependent cleanup; there is no background
purge or project-wide orphan scan.

Provider operations retain their routing, known remote identity and exact
payload in `zulip_outgoing_intents`. Capture happens before deletion. A pending
create can finish before its following delete, including when its remote ID was
unknown at local deletion time. The existing retry and 404 handling apply.

## Counting and reading

`messenger_user_message_unreads` references personal message access with an
`ON DELETE CASCADE` foreign key, in addition to its existing placement/scope
foreign key. Membership revocation removes access and the old unread facts.
Archive and mute remain reversible preferences and preserve read state.

A topic recount reads at most 1001 unread facts in placement-key order. The
existing scope index bounds both dense and empty scopes, including generic
prepared plans. Notification policy is resolved once per topic/user. The all/none policies require no per-message lookup;
mentions-only checks the sparse mention primary key for the selected facts.
Active and passive counts describe the same bounded subset. Stream, folder and
project aggregates retain their existing semantics and can exceed 1001.

Topics without a saved summary take a short metadata-only path that clears any
stale cached summary flag. Their counter updates do not plan or query message
history. Saved summaries retain their existing boundary and visibility checks.

Marking a scope as read deletes its unread facts. A `read_up_to` boundary uses
the existing access preview timestamp and placement key, without expanding
canonical message content. Other sparse flags remain independent of unread.

## Import and retries

Import, creation and deletion use the same transaction-scoped canonical-message
fence. Both realtime and history imports check the durable deletion marker after
taking that fence. Prepared history pages and personal flag pages skip deleted
messages, while retaining the rest of the page. An unknown provider deletion
records the marker before its first message import. A deleted canonical UUID
cannot be reused by a later create request.

The marker is separate from mutation command receipts and survives receipt
retention cleanup. Existing provider entity links and delivery receipts retain
the identity needed to resolve delayed send echoes.

## Upgrade

Migration 0294 takes the existing exclusive Messenger schema fence, converts
pending legacy deletion tasks to recipient snapshots (retaining their keyset
progress), and physically removes hidden roots. It removes orphan unread facts
before adding the access foreign key. Public `deleted_at` fields and the
corresponding history fingerprint fields remain null for compatibility; working tables reject non-null values. Read projections and
runtime queries no longer filter soft-deleted roots.

Deploy the matching API, worker and bridge code together during a maintenance
restart. Old workers that still write soft-deletion timestamps cannot operate
against this schema. Downgrade requires a database backup; deleted roots cannot
be reconstructed from delivery snapshots.
