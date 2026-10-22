# Message mutation journal (experimental, opt-in)

`[messenger_mutations] enabled = true` selects durable admission and a single
Messenger dispatcher with a configurable thread pool (`threads = 4`,
`batch_size = 32`). The default is disabled. All API, event and provider
processes must use the same setting. Do not mix old and new counter writers.

Creation remains synchronous: the canonical message and the author's visible
placement exist when POST returns. Fanout and derived counters remain worker
work. Upload files before admitting a mutation; the journal carries references.
The existing provider conversion stage prepares imported files before message
reconciliation.

Read/read-up-to, star/unstar, edits, deletes and reaction mutations validate
access and persist a command before returning. Stream read is split into topic
commands with boundaries frozen at admission and one shared request UUID.
Admission does not wait for message-state row updates. Existing HTTP response
codes and resource shapes remain; the accepted representation is not proof that
the persisted representation has changed. An immediate GET can show old state.

Admission is bounded by `max_pending_per_user = 4096` per project/user, using
unique pending slots. A full queue or repeated concurrent admission collisions
return HTTP 429 without accepting a command or issuing a mutation receipt.
A multi-topic request rolls back all its admissions if any topic cannot fit.
Applied/rejected commands release their slots. Drain all pending work before
changing this limit or upgrading from an older experimental journal revision.

## Receipts and delivery

The response header `X-Workspace-Mutation-Id` identifies the request. GET
`/v1/messenger/message_mutations/<request_uuid>` is scoped to its project and
requesting user and reports pending, applied or rejected, with command counts.
Applied means the mutation and its exact topic counter committed together.
Stream, folder, project counters and public event delivery are later stages;
the receipt does not claim they have finished or that a provider received it.
A rejected command must be surfaced to clients using the receipt. A deleted or
revoked resource is not silently modified using stale admission permissions.

Terminal receipts expire after `receipt_retention_seconds = 86400` (24 hours),
measured from the last finished command in a request. The dispatcher collects
expired commands in batches of at most 1000 rows. Requests with pending commands
are retained. Clients must poll before expiry; an expired receipt can return 404.
Delete tombstones are independent of these expiring receipts.

Public events are written to a durable publication outbox in the same
transaction as the change. A dispatcher-owned publisher assigns event epochs
and writes the existing event format. Epoch delivery ordering therefore has a
single writer without an explicit project advisory lock in this path. Existing
membership guards apply to the real event UUID.

## Ownership and counters

The dispatcher schedules work; pool threads own transactions. At most one job
holds a topic reservation. Canonical messages with several placements reserve
all their topics; changed placement scopes cause a retry with the new set.
Different topics run concurrently. A running parent counter job does not reserve
child topics. Further changes merge into one additional parent pass. A retriggered running
key yields to jobs already waiting for the same topic before its next batch.

Topic commits invalidate stream counters in memory; stream commits invalidate
folders, and folder commits invalidate the user's project total. Stream totals
sum topic counters. Project totals sum unique visible stream memberships, so a
stream appearing in several folders is counted once. Original provider input
journals are consumed directly, with revision-fenced acknowledgements.

Personal flag updates do not invalidate full canonical-message reconciliation
in the owned path. Their separate flag journal still records each change.
Mirroring an observed flag into its imported snapshot temporarily marks that
write as owned and restores the previous transaction setting afterwards.
Content, placement, access-generation and other structural changes still
invalidate the full message; existing pending message repairs are preserved.
Legacy writers retain their previous invalidation behavior.

Owned incoming flag observations are recorded as a batch. The batch mirrors
only its supplied flags, preserves their observation versions, and invalidates
the affected personal states once in project/state UUID order. Consumers use
the same order for state-journal writes and acknowledgements. Applying an
incoming observation does not echo it back into that journal. A newer revision
is still retained when it arrives during processing. Migrations through 0257
are required for batching; migration 0258 is required for bounded admission and
receipt collection. The owned flag batch also respects the history snapshot
observation barrier, so a delayed realtime event cannot replace a newer snapshot.

No explicit row or advisory locks coordinate new mutation/counter jobs.
PostgreSQL INSERT/UPDATE/DELETE, foreign keys, schema migration and process
ownership still use locks. The dispatcher uses a filesystem lock to reject a
second owner on the same node. The lock is created with mode 0600 and rejects a
final-component symlink; a newly created run directory has mode 0700. Provision
existing run directories for the service UID and prevent untrusted writes.
Running multiple dispatcher nodes is unsupported;
a thread pool supplies parallelism, not several independent owners.

## Recovery and deletion

Original commands and publication intentions are durable. Counter invalidations
are coalesced in memory. On first activation, raw topic counters are repaired and
a durable initialization barrier is set only after successful topic commits.
Normal restart repairs parents from durable topic counts without rescanning all
message states. Uncertain commits request repair. PostgreSQL-confirmed serialization or
deadlock rollbacks retry their durable inputs without rebuilding the catalog. A healthy running owner
does not periodically rebuild the complete catalog ahead of live mutations. In-flight transactions roll back on crash;
applied commands are not applied again.

Applied deletes persist the canonical message UUID and ingest generation in
`messenger_message_delete_tombstones` in the mutation transaction. These compact
tombstones have no membership FK and survive removal of the requesting project
user and expiry of the command receipt. Migration 0258 backfills existing applied
deletes before collection can run. **Do not purge these tombstones**: later
provider observations must not recreate deleted messages. A new explicit native
creation using the same UUID has a different ingest generation. Terminal commands
discard accepted message bodies; successful result bodies are not retained.

Provider flag and reaction HTTP calls use prepare, HTTP without an open database
transaction, and a fenced confirmation transaction. Immediately before issuing
the HTTP call, a separate short read verifies that the account is still enabled
and its revision matches preparation. Revocation after that authorization point
cannot cancel a remote request already authorized/in flight. Confirmation checks
both enabled state and revision again and cancels a stale task without recording
its ACK. No account row is held locked across HTTP. This does not change every
other provider HTTP path or remove the accepted unverified-account limitation.

## Activation and rollback

Take a consistent snapshot and apply the migration before switching writers.
Stop old writers, use the same flag in every service, start one dispatcher, and
wait for initialization and parent repair before accepting performance results.
For a real-data test copy, remove external credentials and hold provider senders.

Disabling is not a safe immediate rollback after new-mode writes. The new owner
bypasses legacy counter contribution tracking. Drain accepted commands and
publication, stop the owner, rebuild legacy counter contributions and snapshots,
and only then enable old writers; alternatively restore the consistent snapshot.
Do not downgrade with pending commands or discard delete tombstones. Schema,
HTTP contract, concurrent correctness, crash recovery, event delivery and timing
are separate acceptance checks.

Migration 0258 refuses downgrade once independent tombstones exist: the old
membership-owned schema cannot represent them safely. Restore a consistent
pre-upgrade snapshot for that rollback. Migration filenames were rebased after
the published history fingerprint migrations; earlier experimental installations
must restore their clean baseline before upgrading to this revision.

The scoped provider flag probe uses a SQL function with function-local
`join_collapse_limit = 1`. It preserves the existing view's rows while bounding
planning of its many joins; it does not change session-wide planner settings.
