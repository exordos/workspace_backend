# Hierarchical unread counters

The Messenger resource and event shapes remain unchanged. Topic `unread_count`
is `min(exact unread messages, 1001)`. Stream `unread_count` counts unread child
topics, without the topic display cap. Folder `unread_count` counts distinct
unread member streams, without that cap. Folder counts include muted streams.
Topic and stream `active_unread_count` follow notification settings;
`passive_unread_count` is total minus active in the same unit. A mixed child is
counted once as active. Done and notification settings never mark messages read.
Automatic folder membership excludes archived streams; custom folder membership
is preserved on archive. A stream contributes once to every folder containing it.

`topic_unread_state` is the exact per-topic/user source for the read model.
It survives removal of a topic binding and is deleted with the topic or user.
`unread_contributions` contains only unread flags, keyed by canonical flag identity;
its foreign key cascades on flag deletion/rebinding. Reading a flag removes its
contribution. Contribution changes and grouped parent deltas commit with the
source facts. Replaying an unchanged flag or a baseline page is a no-op. The
state row serializes initialization of a newly granted binding with a concurrent
flag write. Existing-binding notification updates never acquire a state lock
while holding a binding lock. Identity rebinds enqueue a committed-scope refresh.
Internal versions are not exposed through the public API.

The projection worker advances the migration baseline in pages of 1000 flags,
with source row locks and a durable UUID cursor using the existing unique index.
The later binding-snapshot stage uses its own indexed binding UUID cursor.
Function-local planner settings prefer these ordered indexes over a full sort
after write churn and disable JIT during these small baseline statements.
The caller settings are restored when the function returns.
Each page commits separately before ordinary projection work. It then schedules bounded snapshot
pages. New writes use the same contribution upsert throughout the baseline;
there is no reset of source flags. The old capped read path remains available
until the internal baseline ready marker is true. Subsequent normal reads use
exact state and incremental parent totals. The worker refreshes metadata only
for affected scopes. Topic and stream last-message searches are skipped on
ordinary read, star and mention updates; membership, message relocation,
timestamp changes and deletion invalidate the relevant cached pointers.

Existing clients perform message-based optimistic arithmetic. Every affected
topic snapshot is therefore followed by an authoritative stream snapshot, then
an authoritative folder snapshot, including unchanged counts. These corrections
use the existing event kinds and fields. They restore final values after local
optimism; they do not eliminate transient client-side estimates or make the
legacy `toggle_done` action safe to retry after an unknown acknowledgement.

Overlapping source DML can still be aborted by PostgreSQL's deadlock detector.
The existing bounded retry middleware also covers transaction-only provider
entity upsert/delete and apply batches. It retries only SQLSTATE `40P01` after
transaction rollback, restores the original request body, and preserves the
original provider error on final exhaustion. Its three-attempt bound and the
legacy read-action exhaustion response remain unchanged. Provider handlers
write canonical facts, idempotency state and events in the request transaction;
external delivery happens later. Toggle actions remain excluded.

## Recovery and rollback

The singleton `unread_counter_baseline` stores the flag cursor, snapshot cursor,
readiness and completion separately. A worker restart resumes the next bounded
page. Keep ordinary planner statistics current on `message_flags` and
`topic_bindings` and check their UUID cursor plans before a large rebuild.
Retryable projection tasks retain their ordinary retry/lease behavior;
there is no global repair on a read request. A bounded scope repair can requeue
ordinary `read_counters` tasks for selected topic bindings; the worker reloads
that binding from durable exact state and publishes the corrective parent chain.
Contribution rows are removed as flags become read. Ordinary PostgreSQL vacuum
reclaims dead tuples; allocated relation bytes can exceed live payload bytes.

Stop the new workers before downgrading the migration or deploying an older
worker. Downgrade removes derived state only and leaves source messages, flags,
membership and settings intact. The existing parent columns still contain child
entity units after downgrade. Before accepting an older backend, requeue ordinary
`read_counters/user_topic` tasks in bounded pages of `topic_bindings.uuid` using
the existing task queue, commit each page, and let the old worker rebuild topics,
then streams and folders. Use the established quiet-task conflict key to avoid
duplicates. Preserve the page cursor until the queue is drained and compare the
result with the old message-unit SQL oracle. Downgrade alone is not a completed
read-model rollback.
