# Workspace-Zulip Bridge Mail Protocol v1

> **Superseded design history — do not implement this protocol.** The current
> Zulip bridge uses the private Provider HTTP data plane defined by
> [`workspace_provider_api_v1.yaml`](workspace_provider_api_v1.yaml) and the
> PostgreSQL-canonical runtime described in
> [`architecture.md`](architecture.md). It does not use IMAP or SMTP. The
> material below is retained only as a record of the abandoned mail transport
> design.

Status: historical, superseded protocol proposal.

## 1. Scope

This protocol carries Messenger operations and their terminal results between
Workspace backend and one realm-local Zulip bridge element. It is an internal
data-plane contract. It does not expose a browser API and does not restore the
removed Provider API.

The protocol has two directions:

- Workspace backend appends records to an IMAP outbox consumed by the bridge;
- the bridge submits records through authenticated SMTP to an ingress mailbox
  consumed by Workspace backend.

An operation can originate in either system. A result always travels in the
opposite direction and acknowledges exactly one operation attempt.

Provisioning, encrypted credentials, chat selection, capability negotiation,
health, and binary file allocation are not mail records. They use the separate
private mTLS control and file APIs. Binary objects never appear in MIME. A
message operation contains only canonical Workspace Markdown and `urn:*` file
references whose bytes have already been committed through the file plane.

The existing native Messenger `text/markdown` codec and the project journal
remain separate contracts. Every bridge record, including `message.create`, is
a single-part `application/json` message as defined here.

## 2. Version and compatibility

The protocol identifier is `workspace-zulip-mail/1`.

Version 1 has no minor-version negotiation. Consumers must reject another
protocol identifier before interpreting the body. Within version 1:

- required field meaning and type cannot change;
- a new required field, operation kind, result outcome, or incompatible limit
  requires a new major protocol identifier;
- unknown top-level fields are rejected;
- operation payloads may contain only the fields defined for their operation
  plus an optional `extensions` object;
- an unknown `extensions` member is ignored and preserved when the record is
  forwarded, but it cannot change required behavior.

The known v1 ingress extension is `delivery_class`. Every
`zulip-to-workspace` operation must set it to either `live` or `backfill`.
Other values and an omitted value are rejected. This extension classifies the
producer scheduler lane; it does not change authorization, ordering, or
capability checks.

## 3. Identities, mailboxes, and ACLs

The mail image creates three non-human technical identities. Their actual
realm-local domain is deployment configuration and is not part of the wire
version.

| Identity | Protocol access | Purpose |
| --- | --- | --- |
| Workspace bridge producer | IMAP append/read/prune through the existing backend-only mail service boundary | Produces Workspace-originated operations and Workspace results. |
| Zulip bridge reader/submitter | Read-only IMAP plus authenticated SMTP submission | Reads the provider outbox and submits bridge records. It is not a Dovecot master user. |
| Workspace bridge ingress | Backend-only IMAP; no bridge login | Receives authenticated SMTP submissions from the bridge. |

The bridge must never receive the Workspace mail master credential.

For each external account UUID, the following mailboxes exist:

```text
Workspace/Bridge/Zulip/V1/Accounts/<account_uuid>/Outbox
Workspace/Bridge/Zulip/V1/Accounts/<account_uuid>/Ingress
```

`Outbox` belongs to the Workspace bridge producer's mailbox namespace. The
Zulip bridge reader has only Dovecot `lookup` and `read` rights on this subtree.
It cannot insert, create, rename, delete, expunge, administer ACLs, or read any
other mailbox, including native `INBOX`, `Workspace/State`, and
`Workspace/Events/*`. The backend owns append and retention.

`Ingress` belongs to the Workspace bridge ingress namespace. The Zulip bridge
has no IMAP rights on it. Its SMTP identity may submit only from the configured
bridge envelope sender to the single configured ingress recipient. The SMTP
router derives the account mailbox from the authenticated account UUID header
after validation; arbitrary local parts, aliases, forwarding, multiple
recipients, and relay are forbidden. Workspace backend owns read and retention.

Mailbox paths use the canonical lowercase UUID text. The account UUID in the
path, RFC 5322 header, and JSON body must match. Mailbox existence is driven by
the control-plane account generation; receipt of a mail record never creates an
account or mailbox.

IMAP `\Seen`, keywords, and message deletion are transport hints only. They are
not acknowledgements and are not used as durable cursors.

## 4. RFC 5322 envelope

One protocol record is exactly one RFC 5322 message. A conforming message:

- is not multipart;
- has `Content-Type: application/json; charset=utf-8`;
- has `Content-Transfer-Encoding: base64`;
- has no `Content-Disposition`, filename, nested message, attachment, or MIME
  parameter other than `charset=utf-8`;
- decodes to exactly one canonical JSON object;
- contains no binary object or provider credential.

The decoded JSON is serialized as UTF-8 without BOM using sorted object keys,
`,` and `:` separators, NFC-normalized strings, JSON booleans/null, and
base-10 integers. Floating-point numbers, duplicate keys, NaN, Infinity,
trailing bytes, and a top-level value other than an object are invalid. The
decoded body has no trailing newline.

Required standard headers:

| Header | Rule |
| --- | --- |
| `From` | Exact configured technical sender for the direction. Display names and multiple addresses are forbidden. |
| `To` | Exact configured technical recipient for the direction. Exactly one address is required. |
| `Date` | RFC 5322 date. Informational only; never used for ordering. |
| `Message-ID` | `<bridge-v1.<record_uuid>@messenger.workspace.invalid>`. |
| `MIME-Version` | Exactly `1.0`. |
| `Content-Type` | Exactly the media type and charset above. |
| `Content-Transfer-Encoding` | Exactly `base64`. |

Required protocol headers:

| Header | Value |
| --- | --- |
| `X-Workspace-Bridge-Protocol` | `workspace-zulip-mail/1` |
| `X-Workspace-Bridge-Direction` | `workspace-to-zulip` or `zulip-to-workspace` |
| `X-Workspace-Bridge-Record-Kind` | `operation` or `result` |
| `X-Workspace-Bridge-Record-UUID` | Canonical UUID matching `record_uuid`. |
| `X-Workspace-Bridge-Operation-UUID` | Canonical UUID matching `operation_uuid`. |
| `X-Workspace-Bridge-Attempt` | Positive base-10 integer matching `attempt`. |
| `X-Workspace-Bridge-Account-UUID` | Canonical UUID matching `account_uuid`. |
| `X-Workspace-Bridge-Project-UUID` | Canonical UUID matching `project_uuid`. |
| `X-Workspace-Bridge-Causal-Lane` | ASCII lane matching `causal_lane`. |
| `X-Workspace-Bridge-Sequence` | Positive base-10 integer matching `sequence`. |
| `X-Workspace-Bridge-Operation-SHA256` | Lowercase hex SHA-256 matching `operation_sha256`. |
| `X-Workspace-Bridge-Body-SHA256` | Lowercase hex SHA-256 of the decoded canonical JSON bytes. |
| `X-Workspace-Bridge-Signature` | `v1=<base64url-hmac>` as defined below. |

Every required header must occur exactly once. Header folding is accepted only
where RFC 5322 requires it; after parsing, values must contain no control
character. A body/header mismatch is an integrity failure, not a recoverable
schema error.

## 5. Authenticated integrity boundary

Both IMAP and SMTP require STARTTLS, CA validation, and hostname validation.
Plaintext fallback and skip-verification modes are forbidden. SMTP ingress also
requires the dedicated bridge SMTP credential and the fixed sender/recipient
ACL described above.

TLS and mail authentication are necessary but not sufficient: each record is
signed with a realm-, bridge-instance-, and identity-generation-specific
256-bit derived mail key. The key is derived from the existing Exordos
per-installation enrollment secret delivered through configuration. The
derived mail key is independent from Dovecot master, SMTP, Zulip, database, and
file-plane credentials.

The mail master key is HKDF-SHA-256 with the exact UTF-8 bytes of that enrollment
secret as input key material, an empty salt, the UTF-8 info value
`workspace-zulip-mail-v1/master/<realm_uuid>/<bridge_instance_uuid>/<identity_generation>`,
and output length 32 bytes. UUIDs use canonical lowercase text and generation
uses canonical base-10 text. The input secret is opaque: implementations do not
trim it, normalize it, add a terminator, or decode it as hexadecimal, base64, or
another token format, and this protocol imposes no additional length
requirement. Exordos owns the bootstrap config secret lifecycle; mail protocol
implementations must not log or copy its raw bytes into mail or operational
records. Re-enrollment derives a new key generation; the receiver may verify the
immediately previous generation only during the same bounded overlap used for
credential rotation.

Two direction keys are derived from the 32-byte mail master key with
HKDF-SHA-256, an empty salt, and these exact UTF-8 `info` values:

```text
workspace-zulip-mail-v1/workspace-to-zulip
workspace-zulip-mail-v1/zulip-to-workspace
```

The signature input is the following UTF-8 text, with LF separators and a final
LF. `-` represents a JSON null predecessor or reply UUID.

```text
workspace-zulip-mail-v1
<direction>
<record_kind>
<record_uuid>
<operation_uuid>
<attempt>
<account_uuid>
<project_uuid>
<causal_lane>
<sequence>
<predecessor_operation_uuid-or-dash>
<in_reply_to_record_uuid-or-dash>
<operation_sha256>
<body_sha256>
```

`X-Workspace-Bridge-Signature` is `v1=` followed by base64url without padding
of HMAC-SHA-256 over that input. Verification is constant-time. The receiver
verifies TLS identity, SMTP/IMAP route, body hash, signature, and header/body
equality before authorizing the account, project, chat, operation, or provider
capability.

Secret rotation uses an overlapping key set supplied by the control plane. A
producer signs only with the active key. A consumer may verify against the
active and immediately previous keys during the declared rotation window. A key
identifier is transport configuration, not attacker-controlled mail content.

## 6. Common JSON fields

Every body has these fields:

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | string | Exactly `workspace.zulip_bridge.mail`. |
| `schema_version` | integer | Exactly `1`. |
| `record_kind` | string | `operation` or `result`. |
| `record_uuid` | UUID string | Unique immutable RFC 5322 record identity. |
| `operation_uuid` | UUID string | Stable semantic operation identity across retries. |
| `attempt` | integer | Starts at `1`; increases only for an explicit retry of a terminal retryable failure. |
| `operation_sha256` | hex string | Stable semantic digest defined below. |
| `account_uuid` | UUID string | Realm-global external account. |
| `project_uuid` | UUID string | Project currently assigned to the projected chat. |
| `origin` | string | `workspace` or `zulip`. |
| `causal_lane` | string | Lane defined in section 9. |
| `sequence` | integer | Positive, gapless, lane-local sequence. |
| `predecessor_operation_uuid` | UUID string or null | Previous operation in the same lane; null only for sequence `1`. |
| `created_at` | string | UTC RFC 3339 with `Z`. |
| `expires_at` | string or null | UTC RFC 3339 with `Z`; null for non-expiring provider history/backfill. |

`operation_sha256` is lowercase SHA-256 of the canonical JSON bytes of this
object:

```json
{
  "account_uuid": "...",
  "causal_lane": "...",
  "operation": {},
  "operation_uuid": "...",
  "origin": "workspace",
  "predecessor_operation_uuid": null,
  "project_uuid": "...",
  "sequence": 1
}
```

It excludes record identity, attempt, transport timestamps, and results. Reusing
an operation UUID with a different operation digest is a permanent conflict and
must never execute provider or Workspace mutation code.

`record_uuid` identifies an immutable encoding. An SMTP retransmission or IMAP
reappend of the same transport record reuses the record UUID and exact bytes. A
new explicit attempt has a new record UUID and body but the same operation UUID
and operation digest.

## 7. Operation records

An operation record adds one `operation` object:

```json
{
  "kind": "message.create",
  "entity_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "actor_uuid": "11111111-1111-1111-1111-111111111111",
  "occurred_at": "2026-07-17T10:00:00Z",
  "provider": {
    "kind": "zulip",
    "chat_id": "42",
    "entity_id": "9001",
    "revision": "17"
  },
  "payload": {},
  "extensions": {"delivery_class": "live"}
}
```

`kind`, `entity_uuid`, `actor_uuid`, `occurred_at`, `provider`, and `payload`
are required. `extensions` is optional for Workspace-originated operations and
required for Zulip-originated operations as described above. `actor_uuid` is a stable IAM user or
external identity UUID, never an email address. `provider.kind` is exactly
`zulip`; `chat_id` is required for chat-scoped operations; `entity_id` and
`revision` are strings or null because provider identifiers are opaque.

The v1 operation kinds and payloads are:

| Kind | Required payload | Rule |
| --- | --- | --- |
| `identity.upsert` | `display_name`, `email`, `avatar_urn`, `active` | `email` and `avatar_urn` may be null. Identity UUID is stable within account and provider. |
| `stream.upsert` | `name`, `description`, `private`, `chat_kind`, `participant_uuids`, `default_topic_uuid` | Complete snapshot. `chat_kind` is `channel`, `personal_dm`, or `group_dm`. Personal DM has exactly two participants. |
| `stream.delete` | `stream_uuid` | Tombstone only. It contains no message body or prior snapshot. |
| `topic.upsert` | `stream_uuid`, `name` | Complete topic identity/name snapshot. Zulip rename uses the same entity UUID. |
| `topic.delete` | `stream_uuid`, `topic_uuid` | Tombstone only. |
| `message.create` | `stream_uuid`, `topic_uuid`, `author_uuid`, `payload`, `reply_to_message_uuid` | `payload` is exactly `{\"kind\":\"markdown\",\"content\":<non-empty string>}`. Reply UUID may be null. Markdown contains only URNs for copied files. |
| `message.update` | `stream_uuid`, `topic_uuid`, `author_uuid`, `payload` | Complete replacement Markdown payload; source/provider identity is immutable. |
| `message.delete` | `stream_uuid`, `topic_uuid`, `author_uuid` | Tombstone only; no `payload`, body, content, Markdown, or copied bytes. |
| `read_state.set` | `stream_uuid`, `topic_uuid`, `reader_uuid`, `read`, and exactly one of `through_message_uuid` or `message_uuids` | `topic_uuid` may be null. `through_message_uuid` applies `read` through the receiver's inclusive native boundary. `message_uuids` applies it only to the non-empty list of messages. Workspace-originated stream, topic, single-message, and `read_up_to` actions resolve their native selection first and send that exact list; a provider must not reinterpret Workspace order using provider IDs. The bridge resolves every listed UUID independently: a terminal prior create with no provider mapping is omitted while the mapped subset is still applied, and an entirely unmapped set is a provider no-op. Causal-lane claiming still prevents a read from bypassing an earlier pending create. `read` is boolean. |

For `stream.upsert` and `topic.upsert`, a changed `name` is the two-way rename
operation. There are no separate provider-specific rename records. Reactions,
polls, typing, calls, and presence have no v1 operation kind and are rejected as
`unsupported_operation`.

Workspace-originated operations use Workspace entity UUIDs and the persisted
provider mapping. Zulip-originated operations include stable Workspace UUIDs
allocated by the bridge/backend mapping workflow before delivery. Mail receipt
must not guess an entity UUID from a display name, email, or mutable provider
name.

An operation record is valid only when the account is enabled, the chat remains
selected, the chat is assigned to `project_uuid`, the actor is authorized, and
the negotiated account capability includes the operation kind. Mail possession
alone grants no Messenger authority.

## 8. Result records

A result record repeats the common fields of the acknowledged operation and
adds:

```json
{
  "in_reply_to_record_uuid": "4fd3d6b4-d39b-4f58-a326-4a3b3292482c",
  "result": {
    "outcome": "committed",
    "committed_at": "2026-07-17T10:00:01Z",
    "provider_entity_id": "9001",
    "provider_revision": "17",
    "safe_error": null,
    "manual_retry_allowed": false
  }
}
```

`in_reply_to_record_uuid` is required and identifies the exact attempted record.
The result's `record_uuid` is new and its `created_at` is the time the immutable
result record was created. Its `expires_at` repeats the operation deadline.
`operation_uuid`, `attempt`, `operation_sha256`, account, project, lane,
sequence, predecessor, and origin must match the operation. The result producer
is the target system, but `origin` continues to identify the operation's origin.
A result received before the corresponding operation is known is held as an
unmatched record and cannot advance delivery state.

Terminal outcomes are:

| Outcome | Meaning |
| --- | --- |
| `committed` | The target mutation and durable idempotency/mapping state are committed. |
| `rejected` | The target proved the mutation was not committed and will not retry automatically. |
| `expired` | The operation passed its retry deadline without a confirmed target commit. |
| `cancelled` | The owning account/chat generation was disabled, deselected, deleted, or superseded before commit. |

There is no mail-level `accepted`, `pending`, or `retrying` outcome. Such state
is reported through the private control API and the public delivery projection;
it is not an acknowledgement.

`committed_at`, `provider_entity_id`, and `provider_revision` are strings or
null. `committed` requires a non-null `committed_at`; other outcomes require it
to be null. Provider identifiers are returned only when the provider supplies
them. `safe_error` is null for `committed`, otherwise:

```json
{
  "code": "rate_limited",
  "message": "The provider did not accept the operation before its deadline."
}
```

Allowed safe error codes are `invalid_record`, `unauthorized_account`,
`project_mismatch`, `chat_not_selected`, `capability_missing`,
`unsupported_operation`, `not_found`, `permission_denied`, `conflict`,
`rate_limited`, `provider_unavailable`, `workspace_unavailable`, `expired`,
`cancelled`, and `internal_error`. Messages are sanitized and contain no API
key, credential, message body, internal address, stack trace, provider response
body, or mail path.

`manual_retry_allowed` is boolean. A manual retry reuses `operation_uuid`,
`operation_sha256`, lane, sequence, and predecessor, increments `attempt` by
one, and uses a new record UUID. This higher attempt is an explicit exception to
the normal next-sequence check because the semantic operation already consumed
its sequence; it may execute only when the receiver durably recorded a terminal
non-committed result with `manual_retry_allowed=true`. It is then the latest
explicitly confirmed mutation, subject to delete-wins. A receiver that already
has `committed` for the operation returns `committed` without repeating the
mutation. Any other reuse is a permanent conflict.

## 9. Causal lanes and ordering

Every operation belongs to exactly one ASCII causal lane:

```text
account:<account_uuid>
identity:<account_uuid>:<identity_uuid>
chat:<account_uuid>:<stream_uuid>
```

Account lifecycle/control operations are not transported by mail, so the
account lane is reserved for future data-plane operations and must otherwise be
empty in v1. Identity operations use the identity lane. Stream, topic, message,
and read-state operations use the chat lane.

Each producer persists a positive, gapless sequence counter per
`(origin, causal_lane)` and the previous operation UUID for that same origin.
Sequence allocation and durable enqueue are one local transaction. Workspace
and Zulip therefore never allocate competing values in one sequence; operations
from different origins are concurrent until one target commit is confirmed. A
consumer:

1. deduplicates the record and operation;
2. compares sequence and predecessor with durable `(origin, lane)` state;
3. processes the next sequence only;
4. pauses only that lane on a gap or predecessor mismatch;
5. may process independent lanes concurrently.

Arrival UID, RFC `Date`, `occurred_at`, provider timestamp, and record UUID do
not override lane order. Backfill may be discovered newest-to-oldest, but it is
assigned lane sequence in enqueue order and never blocks live lanes. The
scheduler gives live work strict priority, then retry work, then fair per-account
backfill.

Within one origin's chat lane, mutation is serial. Across origins, the last
target-confirmed operation wins. A committed `message.delete` is terminal for
that entity: a later edit is rejected regardless of origin or lane sequence.
Read-state replacement follows target-confirmation order across origins and lane
order within one origin.

## 10. Commit and acknowledgement rules

A successful result is emitted only after the target confirms and durably
records the mutation.

For Workspace-to-Zulip:

1. the bridge validates and durably records the operation/digest;
2. it performs the provider call using stable provider idempotency metadata;
3. it confirms the provider outcome or reconciles it by provider ID;
4. it commits provider mapping, revision, dedupe state, and lane progress on the
   bridge persistent disk;
5. only then it submits a `committed` result through SMTP.

For Zulip-to-Workspace:

1. the backend validates SMTP identity, record integrity, account, project,
   selection, actor, and capability;
2. required file URNs must already be finalized and authorized;
3. it appends the canonical Messenger operation/message and required user
   events to the Workspace mail source of truth;
4. it confirms all canonical mail appends and durably commits the rebuildable
   projection, provider mapping, dedupe state, and lane progress; recovery
   reconciles these stores if a crash separates the commits;
5. only then it appends a `committed` result to the account outbox.

An IMAP fetch, `\Seen`, queue insertion, SMTP `250`, HTTP acceptance by Zulip,
or local SQL row without the canonical target commit is not success. When the
target outcome is unknown, the processor reconciles or retries; it must not emit
either `committed` or `rejected` based on uncertainty.

## 11. Idempotency, deduplication, and retries

Consumers durably index both `record_uuid` and `(operation_uuid, attempt)` and
store `operation_sha256`, terminal outcome, target IDs/revision, and result
record UUID.

- Duplicate record UUID with identical body hash returns/re-emits the stored
  result and performs no mutation.
- Duplicate record UUID with a different body hash is an integrity incident.
- Duplicate operation/attempt with the same operation digest returns/re-emits
  the stored result.
- Duplicate operation UUID with a different operation digest is a permanent
  `conflict` and performs no mutation.
- A committed operation remains committed forever for deduplication purposes,
  even after transport records are pruned.

Automatic retry uses the same operation attempt and provider idempotency key.
Workspace-originated live work remains pending for at most 24 hours. Exponential
backoff honors provider `Retry-After` and uses jitter. Deadline expiry produces
one terminal `expired` result. Provider backfill records may set
`expires_at=null`; cancellation is controlled by the current account/chat
generation.

Zulip message creation is a provider-specific exception because Zulip echoes
`local_id` through a registered event queue but does not deduplicate sends by
that value. Before sending, the bridge durably records the queue ID and
`local_id=operation_uuid`. A lost HTTP response remains pending while the
matching message event can still confirm its provider message ID.

When event correlation cannot confirm the result, the bridge performs delayed,
repeated `GET /messages` reconciliation before any resend. The query is scoped
to the exact channel/topic or DM, narrows to the current external account as
sender, requests raw Markdown, and compares canonical content, file references,
target, and the bounded operation time window. One or more exact matches confirm
the operation; the bridge selects the candidate nearest to the first send
attempt, breaking ties by the lowest numeric provider message ID, retains the
candidate count as evidence, and does not resend. This deliberate
equivalence may collapse two identical user operations to one Zulip message.
After repeated successful queries return no match, the bridge may automatically
resend the operation once. An unavailable history query or another ambiguous
result after that resend MUST produce `manual_reconciliation_required` with
sanitized operation/chat context and an original-provider link when one is
known. No further automatic resend is allowed. An explicit user retry creates a
new attempt, and the UI MUST warn that it can create a duplicate Zulip message.

Deduplication and provider mappings are persistent operational state. They are
not reconstructed from IMAP flags. Pruning mail records must never prune the
minimal committed-operation identity needed to prevent duplicate provider
mutations.

## 12. IMAP cursors and retention

Each consumer persists this cursor per account mailbox:

```json
{
  "mailbox": "Workspace/Bridge/Zulip/V1/Accounts/<account_uuid>/Outbox",
  "uidvalidity": 123456,
  "last_uid": 789,
  "last_record_uuid": "4fd3d6b4-d39b-4f58-a326-4a3b3292482c"
}
```

The consumer uses IMAP UIDs, never sequence numbers. It selects the mailbox,
verifies `UIDVALIDITY`, searches `UID <last_uid + 1>:*`, fetches in ascending UID
order, and applies causal-lane scheduling after validation. The cursor advances
past a fetched record only after the record/digest and resulting durable work or
terminal result are committed locally. `last_record_uuid` is a diagnostic
cross-check, not a substitute for deduplication.

On `UIDVALIDITY` change, the consumer stops normal polling, clears only the UID
cursor, scans all current UIDs, and rebuilds transport position by durable
record/operation deduplication. It does not clear provider mappings, target
commit history, lane sequence, or backfill progress. Processing resumes only
after every retained record is classified as committed, pending, cancelled, or
invalid.

The mailbox owner may prune a terminal operation and its result after both are
at least seven days old and every registered consumer cursor is beyond their
UIDs. Pending operations are never pruned. A corresponding committed-operation
dedupe tombstone is retained for the lifetime of the account projection. Mailbox
retention is independent from the browser event epoch retention.

## 13. SMTP ingress validation

The mail server requires STARTTLS and SMTP AUTH before `MAIL FROM`. It accepts
only the configured envelope sender, exactly one configured ingress recipient,
and a message no larger than the protocol limit. It disables relay, aliases,
plus addressing, catch-all routing, and DSN content reflection for this identity.

Before a record can enter the normal ingress mailbox, the ingress filter checks:

1. authenticated SMTP identity and fixed envelope route;
2. total size and required single-value headers;
3. protocol, direction, media type, transfer encoding, and Message-ID shape;
4. base64 and UTF-8 decoding, canonical JSON, body hash, and HMAC;
5. header/body equality and account mailbox route;
6. current account generation and project/chat assignment.

Failures before `DATA` completion are rejected with a generic permanent `5xx`
or temporary `4xx` SMTP response as appropriate. The response never reflects
body content. A record already accepted by SMTP but failing asynchronous domain
validation moves to a backend-only quarantine mailbox with a sanitized reason;
it is not retried as normal data. A signed, trustworthy operation identity may
receive a terminal `rejected` result. An unauthenticated or integrity-invalid
record receives no result, preventing reflection and oracle behavior.

## 14. Crash recovery

Processors use a durable inbox/outbox pattern on persistent storage.

| Crash point | Recovery |
| --- | --- |
| Before target call/commit | UID is fetched again or durable pending work resumes. No result exists. |
| During provider call with unknown result | Reconcile by stable operation/provider ID before retry. Never guess failure. |
| After target commit, before local dedupe commit | Reconcile target state and commit the same operation mapping. |
| After target and dedupe commit, before result send | Emit the stored result with the same result record UUID and bytes. |
| After SMTP send/IMAP append, before cursor advance | Duplicate record/result is accepted idempotently. |
| After hard VM stop | Restore database, UIDVALIDITY cursors, lane state, mappings, pending work, and immutable result bytes from the persistent disk, then run the same reconciliation. |

The result record is constructed and stored in the same local transaction as
terminal dedupe state whenever the local database supports it. The actual SMTP
send or IMAP append occurs after commit and is retried until the reciprocal
result is observed or retention policy permits cleanup.

## 15. Limits

Receivers enforce all limits before domain mutation:

| Item | Limit |
| --- | --- |
| RFC 5322 message, encoded | 512 KiB |
| Decoded canonical JSON body | 256 KiB |
| Header section | 32 KiB |
| One unfolded header value | 4 KiB |
| JSON nesting depth | 32 |
| JSON object members | 256 per object |
| JSON array members | 10,000 |
| Causal lane | 128 ASCII characters |
| Safe error message | 512 Unicode characters |
| Markdown payload | 64 KiB UTF-8 and at most 10,000 Unicode characters |
| Participant UUIDs in one stream snapshot | 5,000 |

Compression, chunking, multipart continuation, and batched operations are not
supported. A record carries one operation or one result. Larger binary content
uses the file plane; larger resource discovery uses the paginated control API.

## 16. Error handling

Validation errors are classified as follows:

| Class | Handling |
| --- | --- |
| TLS, SMTP AUTH, route, HMAC, hash, or header/body mismatch | Reject/quarantine, security telemetry, no reflected result. |
| Unsupported protocol/schema/operation | Terminal `rejected` with `unsupported_operation` when identity is trustworthy. |
| Invalid JSON or payload | Terminal `rejected` with `invalid_record` when identity is trustworthy. |
| Account/project/chat authorization failure | Terminal `rejected`; no target mutation and no leaked resource detail. |
| Lane gap or predecessor mismatch | Pause that lane and refetch; do not reject later records or block other lanes. |
| Temporary target/rate-limit failure before deadline | Keep pending and retry; no terminal mail result yet. |
| Unknown target outcome | Reconcile; never report success or failure from transport state alone. |
| Deadline reached without confirmed commit | Terminal `expired`; manual retry allowed only by policy. |
| Permanent provider/Workspace rejection | Terminal `rejected` with sanitized code. |

Invalid records do not advance semantic lane state. They may advance a transport
UID cursor only after their quarantine classification is durable, so one poison
record cannot block all later mail while its lane remains visibly degraded.

## 17. Mandatory conformance cases

An implementation is not protocol-v1 conforming until automated tests prove:

- operation and result round trips in both directions;
- exact header/body equality and signature verification;
- rejection of multipart, attachments, alternate media types, duplicate JSON
  keys, unknown top-level fields, invalid UUIDs, oversized messages, and binary
  bodies;
- least-privilege mailbox ACLs and SMTP no-relay behavior;
- duplicate record, duplicate operation, changed digest, and higher-attempt
  behavior;
- per-lane ordering, lane-gap isolation, delete-wins semantics, and independent
  lane concurrency;
- no success before provider or canonical Workspace commit;
- crash recovery at every point in section 14;
- UIDVALIDITY recovery without duplicate provider mutation;
- 24-hour retry expiry, seven-day transport pruning, and retained dedupe
  tombstones;
- message/file scenarios proving that mail contains URNs only and no MIME
  attachment or bucket credential;
- native Messenger operation while the bridge is stopped, proving failure
  isolation.
