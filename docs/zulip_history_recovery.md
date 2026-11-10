# History continuation after subscription renewal

An expired provider event queue requires a fresh registration and reconciliation
of messages, flags and access that may have changed while disconnected. It does
not invalidate the original historical frontier or its saved page.

Initial registration continues to create `zulip_history_accounts` and its batch.
On automatic renewal the original `before_id`, cutoff and completion flag stay
unchanged, and `zulip_history_batches` stays reusable. The latest catalog replaces
unfinished catalog work. A separate durable `zulip_history_recovery_accounts`
frontier and `zulip_history_recovery_batches` page use the existing importer,
normalizer and membership fences. Deleting the original job/account cascades to
its recovery state; completing recovery deletes only that recovery root.

Recovery starts at the new registration boundary. It reconciles older pages as
well as the recent message gap: old read/starred flags or newly accessible old
channel messages can change during a disconnection. Existing block receipts
skip unchanged merges; retaining a cursor alone would miss those changes.
This provider scan is deliberate reconciliation, not a reset of completed
initial coverage. The scheduler alternates initial and recovery work for an
account, so neither frontier is displaced by the other. The existing one-task
limit per project/provider remains authoritative.

A later renewal replaces the recovery snapshot and its fetched recovery page,
while the initial import stays intact. Generation checks take the external-account key-share lock before locking
the selected job through commit, matching deletion and foreign-key lock order. An old selected generation cannot recreate a deleted recovery
page or advance a new frontier. A changed provider identity starts fresh coverage instead of inheriting another
identity's saved frontier. The existing explicit `REQUEST_SQL` full-resync path can still deliberately replace the original boundary.

Logs identify initial registration versus recovery without provider queue IDs,
credentials or message bodies. History batch logs append `range=initial` or
`range=recovery`; status reports should show the original account frontier and
reconciliation progress separately. A completed initial account can correctly
have recovery work pending.

`test_zulip_history_renewal.py` exercises the real invalid-queue/register/admission
transaction, unfinished and completed coverage, saved pages, repeated renewal,
process restart, historical and new messages, old read/starred state, channel
access expansion, fair progress, stale generation fencing and explicit resync.
Production deployment and fresh bidirectional UI/provider acceptance remain a
separate release step.

The receiver adopts a newly registered in-memory subscription only after its
admission transaction commits. Failed registration retries registration instead
of polling an unsaved queue. Identity matching is reread inside that transaction,
so a lost commit acknowledgement cannot make a completed initial registration
look like a different account on retry.
