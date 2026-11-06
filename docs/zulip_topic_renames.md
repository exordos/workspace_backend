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

A fresh message or a personal `user_topic` event can arrive before admission of
the rename and create a second local topic under the new name. When that
container is empty or contains at most 100 fresh messages or Notification Bot
move notices, the handler joins those placements into the original topic and
renames its metadata atomically. A destination containing only move notices can
be larger: each transaction folds at most 100 placements into the original
history. The durable inbox payload records both topic UUIDs and the notices
already folded, in the same transaction as the moves. Restart and recovery
through another linked account resume that progress. Each batch checks for newly
arrived destination history and switches to a real merge when appropriate; new old-name notices stay
separate and late old-name human history retains real-merge semantics. Provider
events cannot supply this internal progress metadata.

The original history keeps its topic UUID. Topic rows
and fresh placements are locked before this decision; contention retries rather
than waiting inside a large transaction.

Source selection prefers the topic containing the affected placements over an
empty duplicate provider identity. Empty duplicate aliases contribute their
latest personal preferences and are removed without moving the history.
Provisional destinations also transfer newer preferences to the preserved topic;
a real destination keeps its existing preferences.

A late event for an old name must not overwrite a topic that retained the
name's deterministic UUID through a rename. Topic creation checks that UUID and,
when occupied by another identity, derives another deterministic UUID instead.
Repeated old-name events therefore reuse their alias without changing the
history's name or completion state.

Zulip leaves old Notification Bot move notices out of the next `change_all`
message list. Those notices remain under the old provider name. Before an
in-place rename, the handler relocates at most 100 such placements to an old-name
alias per transaction and keeps the rename pending if another batch remains.
Only these notices move; the affected human history retains its topic and
placement UUIDs. The reserved Notification Bot identity comes from the actual
provider user catalog, rather than a fixed user ID. Older ordinary destination
messages, including those created in the same provider second, remain a real merge.

Message transactions pin their resolved topic with `FOR KEY SHARE NOWAIT` in
the existing lookup. Rename cleanup therefore retries instead of deleting an
alias that an incoming message is still using. Expected `NOWAIT` contention
uses the short durable retry path without an application-error log. Topic creation
and personal-topic resolution lock the selected row before saving it, preventing
an old-name personal event from overwriting a concurrent rename.

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

See the [repeated-rename acceptance and timing report](zulip_topic_rename_acceptance.md)
for the measured provider scenario, regression result and before/after operation timings.

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
- A→B→C→D and close/reopen across worker restarts, with old/new personal events
  before and after rename admission, fresh move notices and duplicate empty aliases;
- old move notices excluded from `change_all`, bounded notice relocation while
  unrelated realtime progresses, and a concurrent notice/alias-deletion race;
- 151 destination notices across restart and disabled-primary recovery, late
  human/notice arrivals at either name, and real destination history beyond the first batch;
- a paused old-name personal save racing a rename in the supported incoming worker;
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
