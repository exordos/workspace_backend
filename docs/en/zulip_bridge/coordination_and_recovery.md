# Coordination, bootstrap and recovery

Status: **proposal; compulsory semantics, transport/runtime details partially OPEN**.

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

The document replaces the previous schemes durable old-queue cursor catch-up, message-level
history checkpoint Durable coordination lives in the Workspace;
Bridge local state — It 's a throw-away . cache.

## Authentication Before coordination

Each private control/Provider/file request first passes the current one
realm-bound mTLS authentication `workspace-external-bridge-api`: TLS client
certificate determines `realm_uuid`, `provider_kind`, `bridge_instance_uuid` and
`identity_generation`; current backend state Re-checked at each
request. One-time enrollment, certificate renewal/revokeand secret storage is not
The following paragraphs are added: proposal.

Only after authentication Workspace checks whole-account assignment,
lease/fencing generation and project/chatLease answers the question of what?
instance It's now the owner of the account, but it doesn't authenticate the process itself. stale
lease If the certificate is valid, the authorization refusal is given, and the new lease is not
makes an unauthenticated request acceptable.

## Whole-account lease and fencing

Workspace It gives one Bridge instance lease to the entire external account and
monotonic fencing generation. Account It 's not divided between instances stream,
topic Private API accepts mutation/task/receipt only when
active lease and matching generation.

V1 Allows one Bridge instance, but assignment model at once multi-instance:

1. Workspace It only considers healthy compatible instances.
2. New account gets instance with minimum normalized load
   `active_accounts / declared_capacity`; tie-breaker It should be stable..
3. Assignment sticky: The new instance is not rebalancing healthy
   accounts It 's automatic ..
4. New accounts and accounts whose owner dead/draining.
5. Realtime and the history of one account are always in the same account owner Bridge.

- Heartbeat is sent every `10s`; instance becomes `degraded` after
  `30s` without heartbeat and `offline` after `60s`.
- Graceful shutdown/draining Clearly terminates new claims and releases leases.
- After `60s` offline timeout the new instance claims the entire account, gets a new one
  fencing generation And it starts the same bootstrap.
- Stale owner cannot commit provider receipt, task result or cursor advance.
- Disconnect/delete Calls back generation; work does not move to another account.
- Durable account/tasks/mappings/outbound errors They 're still here . Workspace-owned.

## One bootstrap connect, reconnect and recovery {#единый-bootstrap-connect-reconnect-и-recovery}

One algorithm is used after connect, reconnect, lease takeover, queue
expiry, missing heartbeat, `restart` and `web_reload_client`:

1. Go through current mTLS identity check, then check active account, verified
   credential and whole-account lease.
2. Register a new Zulip event queue only for supported event types.
3. Get a registration boundary that's enough for snapshot/history split.
4. If registration fails , repeat with backoff; do not create history root.
5. Start sequential realtime consumption from new boundary.
6. Idempotent to create Workspace history root task with account selection,
   `history_depth`, boundary and lease generation.

The old queue ID/cursor does not need durable recovery.
create gap: history covers selected snapshot/range to boundary, realtime
— events Inclusive/exclusive wire representation depends on Zulip
registration response And there's still the private transport detail, but the implementation is mandatory.
The permissible overlap is deduplicated
provider object/event keys.

## Realtime terminal acceptance

Connector per account keeps no more than one inbound supported event in the work:

1. Get it next event.
2. Compare directly to one private Workspace command or lifecycle signal.
3. Repeat the same command/key at transient/ambiguous failure.
4. On applied/duplicate/stale/confirmed or classified permanent failure
   counting event terminal.
5. Only after terminal acceptance to go to next event.

This does not mean durable reuse of the old queue after loss: queue recovery again
Provider keys make replay/overlap secure.

## History task model without message checkpoint

Workspace stores immutable/root task and per-stream child tasks.
selected chats, discovers users/streams/topics/memberships And he creates. child task
Child records the time of each channel/direct/group-direct stream. immutable input:
account, stream, history range, boundary and provider task identity.

There is no message-level checkpoint in v1. If child drops to terminal completion,
The next claim repeats the entire selected stream range newest-first.
The objects used quickly return duplicate/no-op by the provider keys.
completed stream tasks The task has the usual `pending` →
`leased/running` → `completed`/`failed` transitions, attempts/backoff, lease
expiry, fencing, bounded retry and DLQ/reconciliation evidence.

Different stream tasks from the same account can be performed in parallel on the same account
Bridge through a common configurable pool, default `4`; exact maximum/optimum remains
One stream is simultaneously owned by another stream. history worker.
Topics/messages inside the stream go in sequence because Zulip topic —
message attribute; messages `created_at DESC` with stable provider-message
tie-breaker. Between accounts the scheduler uses fair round-robin, inside
account — last activity/newest stream first.

All history workers of the same account share an account-level Zulip rate limiter.
`Retry-After` history This account is suspended at provider interval.
Realtime lane separate, has priority and is restarted first; history does not
can spend the budget needed realtime.

## Retry and permanent classification

| Outcome | The action |
| --- | --- |
| transient transport/`429`/temporary unavailable | Backoff+jitter, The same . provider/operation/task key; no advance |
| applied / duplicate / stale | Terminal success; no repeated outbox/event for no-op |
| missing older dependency | Durable deferred reference; current event/task Can be completed after proven storage dependency |
| invalid/cross-scope/conflicting verified owner | Fail-closed, permanent evidence/admin resolution |
| internal outbound `permanent_failed` | Stop it . endless retry; safe code/reason private, current public delivery shape unchanged |
| unsupported family | Not to be signed; unexpected occurrence audited, without guessed mutation |

Completed history tasks and successful outbound operations are deleted through
`30 days`; permanent-failure operation/code/reason — through `90 days`. Future
manual requeue The public retry route, already existing
for external operations, does not replace internal classification and does not extend
This one . proposal.

## Deferred references and reconciliation

Newest-first history can see quote/file/older message reference before
mapping. Workspace keeps the internal deferred reference, and after the
mapping We 're going to repair it . canonical Markdown/URN/mentions. Actual change
writes outbox and ready event; no-op does not event.

Reconciliation He checks .:

- active account lease/generation and absent stale commits;
- history root/child coverage, failed/DLQ tasks and selected range totals;
- provider-key uniqueness, gaps/duplicates and multi-account union references;
- topic alias mappings, file attachment links, unresolved references;
- pending/retryable/permanent outbound operations;
- projection/outbox/task/ready-event consistency - What ? Workspace.

## Backpressure and graceful restart

Realtime intake history throughput is not replaced: realtime is always supported
Before that .. History default pool `4`, upper limit/rate/batch limits
bounded/configurable. Fair round-robin It doesn 't allow one account to monopolize .
pool. When graceful
stop Bridge terminates new claims/provider calls, completes or releases
current unit, conditional It just writes terminal result and it explicitly returns leases.
Hard crash takeover is allowed only after `60s` offline timeout; new owner
with the new generation repeats bootstrap and unfinished stream task range.

The tracking includes account generation/lease age, queue registration
failures, realtime event age, history root/stream lag, restarts/full-range
replays, duplicate/no-op ratio, deferred/DLQ age, outbound retry/permanent
failure and separately Workspace projection/WebSocket lag. Content, `api_key`, raw
payload and personal identifiers are not included in labels/errors.

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
