# Outgoing delivery recovery

The sender favors progress over exactly-once delivery when a non-idempotent
message POST has an unknown result. It checks a persisted delivery ID, the
retained local receipt, and the provider event queue first. If no receipt is
available, or that queue expired or pruned its cursor, it releases the delivery
fence and schedules the original intention for preparation again. A duplicate
provider message is possible. An already known ID avoids another POST.

## One-hour message send window

A new message POST may start only while the Workspace message is at most one
hour old. Its creation timestamp comes from the durable message snapshot, not
the intention's creation or retry time. An intention captured or requeued later
does not extend this window. The sender checks the age before preparing a new
send and again immediately before its POST, so waiting for recipients, files,
rate limits, or a sender restart cannot renew permission to send.
The request's total HTTP timeout is also capped by the remaining window, so
waiting for a pooled connection or connection setup cannot extend it.

An expired send becomes a terminal `error` with an explicit expiry reason. The
Workspace message remains stored, and independent work continues. Existing
dependency handling exposes that error to operations needing its provider ID.
This also applies to pending sends captured before this policy was introduced;
no migration or queue purge is needed.

Receipt verification continues after expiry. A known delivery or retained
receipt completes without another POST. An unknown result with no available
receipt can still enter the existing recovery path, but preparation refuses a
new POST once the hour has elapsed. Edits, deletion, reactions and personal
flags for messages already in Zulip keep their existing behavior.

The deadline limits the start of a new POST. A request already in flight can
have an uncertain result; this policy cannot recall a message the provider
accepted. It preserves receipt reconciliation for that case.

## Retry and receipt handling

Retry and dependency deadlines are stored in `next_attempt_at`. The minimum
dependency delay is one second, including configurations with an older, shorter
value. A larger provider `Retry-After` remains authoritative. Waiting does not
hold a dispatcher slot or a database transaction. The existing `attempts` field
still counts preparation attempts, including dependency checks; it is not a
count of provider POSTs.

Only `new` and `checking` predecessors block ordering. A terminal `error` remains
visible but does not stop later work. A missing identity whose create intention
failed produces an explicit dependency error in a quoted reply, flag update,
edit, or other operation requiring that identity. A live dependency is retried
at the durable deadline. Errors do not become successful receipts.

Receipt choice and canonical message identity are committed under the same
message lock used by incoming imports. The selected provider ID stays stable if
a late echo belongs to another copy of the send. That copy is imported as a
separate provider message. Incoming handlers recheck delivery matching after
acquiring the message lock, including when an echo waited for a concurrent
receipt. A crash after receipt persistence can resume completion without
another message POST.

Migration 0295 adds a partial local-receipt lookup index. It requeues only
terminal sends with the recognized expired/pruned event-queue errors, preserving
the intention payload, attempt count and error for diagnosis. Other terminal
failures remain available for repair. Existing `checking` sends use normal
recovery after the sender starts. Downgrade removes the index; it does not undo
already attempted recovery or provider delivery.

## Required checks

The PostgreSQL integration suite exercises lost responses with an empty,
expired, or pruned receipt queue, a sender restart, quoted-message and flag
continuation, a durable minimum retry deadline, and terminal-parent isolation.
Incoming tests exercise a late echo both before and during concurrent receipt
selection. Existing retained-receipt coverage ensures a receipt received before
the retry prevents a duplicate POST.

Send-window coverage includes the exact one-hour boundary, UTC and offset or
naive timestamps, an expired message beside independent fresh work, expiry
between preparation and POST, a connection pool occupied beyond the deadline,
and restart recovery with an available, empty,
expired or pruned receipt queue. A retained local receipt must still complete
an expired send without repeating the POST.

Deployment acceptance must include fresh plain messages in both directions,
a quoted reply, recipient flag read-back, and repetition after the final
restart. This recovery policy does not shorten the transaction used by a large
incoming topic rename or change history/backlog scheduling.
