# Zulip whole-topic rename and completion events

A subject-only `update_message` with `propagate_mode=change_all` changes topic
metadata. It must not fetch or reimport every message listed by the provider.
The incoming handler updates the existing topic's name, provider identity and
`is_done`, repairs its personal topic identities, and publishes `topic.updated`.
Message contents, placement UUIDs, read/star flags, reactions and files stay in
place. Unloaded provider history remains unloaded.

Zulip's `✔ ` prefix means completion. Workspace strips that prefix from the
visible name and stores `is_done=true`; removing it reopens the same topic.

## Shared copies and ordering

The existing shared-change registry coalesces identical versioned rename events
across accounts. Its identity includes the project, provider, stream, affected
message set, old/new subjects and provider edit timestamp. Queue-local IDs and
personal flags do not split a shared operation. Each account still applies its
own supplied flags to the edited message. A later rename back, a different
provider timestamp, or a different affected set remains a separate change.

The durable inbox and its existing message ordering keys remain authoritative.
Personal copies wait for the common operation. If its account becomes invalid,
a remaining copy recovers the original metadata event without loading message
bodies. Restarting restores pending shared intentions from the inbox.

## A fresh message under the new name

A fresh message can arrive before the rename finishes and create a second local
topic under the new name. When that container was created after receipt of the
rename and contains at most 100 messages created at or after the provider edit,
the handler joins those fresh placements into the original topic and renames
its metadata atomically. The original history keeps its topic UUID. Topic rows
and fresh placements are locked before this decision; contention retries rather
than waiting inside a large transaction.

The stream's default topic is a permanent empty-subject container. It cannot be
renamed in place or retained as the history container for a provisional target.
A rename from that topic moves only the affected placements to a separate topic;
subsequent messages without a subject keep using the original default UUID.

An existing destination with older history is a real merge. The handler retains
that destination and moves at most 100 existing placements per transaction.
The inbox event stays pending until no affected source placements remain, so
conflicting later changes cannot overtake it. Committed placements provide the
resume position after restart. Each batch emits message updates and invalidates
the two counter scopes, then releases its SQL connection. No message body is
refetched, and unrelated realtime work can continue between batches.

Combined content edits, partial moves and stream moves retain their existing
message-import behavior. A late subject-only event does not recreate a topic
when neither its source nor its destination has been imported.

## Verification

`workspace/tests/integration/test_zulip_topic_rename.py` exercises real PostgreSQL,
the incoming service, HTTP API, publication and a WebSocket client. It covers:

- rename, done and reopen for 10,000 loaded messages with a one-second HTTP budget;
- 23 account copies of one 10,000-ID event, one common operation, personal flags,
  restart and subsequent renames;
- unloaded history without provider message GETs;
- a fresh destination message, stable original topic identity and one-second
  durable-inbox-to-WebSocket delivery for rename and done;
- bounded merge commits, fresh unrelated message visibility before merge
  completion, resume after restart and message-update publication;
- recovery when the primary account is disabled;
- renames from the default topic through direct and durable service paths,
  including done and subsequent empty-subject messages;
- realtime/history naming for empty subjects, completion and direct messages,
  explicit provider topic names and migration of existing default labels.

Queries resolve each placement's canonical message by its composite key, keeping
stale pre-import statistics from turning the retained-history check and merge
selection into repeated scans. Migration 0297 applies the same keyed lookup to
the shared last-message repair trigger while preserving its pointer locks. The
one-second checks intentionally retain stale statistics rather than analyzing
the newly imported history.

## Unnamed provider topics

An empty Zulip subject is displayed as `zulip` only in private one-to-one or
self conversations (`private=true`). Group conversations and channels retain
the pre-3.0.21 fallback `General`, including invite-only channels. `invite_only`
does not make a channel a private direct conversation. Explicit provider topic
names, including `General` and `zulip`, remain separate and unchanged.
The empty provider subject and its identity remain unchanged. A checkmarked
empty subject uses the same fallback with `is_done=true`.
Realtime and historical import use the same naming rule. Historical name-based
reconciliation only adopts a topic without an existing provider identity.
Migration 0296 originally changed imported empty/default labels to `zulip`.
Migration 0298 restores `General` for existing nonprivate empty/default topics
that received that generated label. Private conversations, native topics,
explicit provider names and custom labels are preserved. Both migrations keep
topic UUIDs, provider identities, placements and completion state, without
generating outgoing provider renames.
Migration 0298 records only the rows it changes. Rollback restores those labels
only while their update timestamps still match, preserving both pre-existing
`General` labels and later edits; rollback also removes the repair receipt table.

The related incoming integration suite covers content edits, deletion, moves,
files, quotes, flags, membership and client events. Unit tests cover registry
signatures, namespace separation and repeated occurrences. These checks do not
replace provider/UI acceptance of a deployed release.
