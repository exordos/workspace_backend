# Workspace Server v2: The decisions taken to implement

Status: **Active addition to architecture and closed Provider API v2**.

[← The main index](index.md) · [Provider API v2](../workspace_provider_api_v2.yaml) · [The target architecture Zulip Bridge](zulip_bridge/README.md)

This document records the decisions `1B`, `2A`, `3A`, `4A`, `5A` agreed
They're specifying the first implementation of the new Workspace server. docs-first
The architecture, closing the relevant items of the OPEN-list and not changing
a public browser API or JSON that uses an existing Workspace UI.

## 1B — Provider Data API v2 In the current private transport

The new inbound contract is placed on an existing separate mTLS listener:

- `POST /api/workspace-provider/v2/commands` — provider→Workspace commands;
- `POST /api/workspace-provider/v2/operations/actions/lease` — - It 's working .
  The queue Workspace→provider;
- `POST /api/workspace-provider/v2/operation-results` — current report on
  The result Workspace→provider operation.

V2 Reuse it . current certificate identity, heartbeat, body limits,
transaction boundary, batch limit `500`, lease and result semantics v1. V1
The listener is still available for the rolling upgrade. credential
protocol and the new public/browser route are not entered.

Inbound v2 accepts provider identity, not computed Bridge values
Workspace. `external_account_uuid` Selects only the connection already assigned;
The server checks it against the mTLS identity and desired state.
It doesn 't . `project_id`, external-chat UUID, stream/topic/user/message UUID,
permissions or roles. Workspace in the transaction allows account, realm, chat,
project, stream/topic and identity mappings and only then calls the usual
The domain mutation ..

## 2A — One realm-global provider chat belongs to one project {#2a--один-realm-global-provider-chat-принадлежит-одному-project}

For the pair `(provider, verified provider realm, provider_chat_key)` there might be
Only one Workspace project is selected. Several external accounts of the same
realm can reuse chat in the same project, but choose this chat in another
project Rejected until changed desired state.

Conflict returns HTTP `409` with a secure code
`provider_scope_conflict`. The check is performed in a transaction under advisory lock,
And the partial index of the selected chats limits the workload of the check.
simple and cheap model: routing remains unambiguous, and fan-out and public
Projections are not duplicated between projects.

Upgrade Check this variant before reset/copy. Legacy same-realm/same-chat
aliases Within a single project, they're automatically reduced, but an existing project
Selecting this chat in multiple projects stops migration fail-closed:
Automatically selecting project would mean moving or
Hide the internal Workspace messages.

Before the first provider discovery, when verified realm UUID is not yet known,
The pre-selection area is normalized `server_url`. URL
lock; After discovery  URL and realm locks in stable order.
The conflict matches both the known realm UUID, and the identical provider origin,
So the old web-flow of creating/first selecting an account remains running without
The customer changes, and the parallel selection does not bypass the rule 2A.

Provider origin is calculated as the actual HTTP origin: scheme and DNS-name
are brought to the bottom register, ending point DNS and standard ports
are deleted, IPv6 keeps the parentheses; path is not involved in scope key. DNS aliases
The final results are determined by the discovery of the verified realm .UUID- The first trusted
Each account binding takes the same advisory locks for all already selected accounts chat
This account and before entering identity rejects its catalog report unless another
account The first person to confirm the same realm/chat in another project.
realm account remains the owner of the routing; alias, selected in other project,
gets the secure code `provider_scope_conflict`.
without trusted realm:
The same numerical chat IDs are allowed in independent Zulip realms.
account with alias does not become the second active data source, but the bridge can
Re-publish the catalog after the conflict is resolved.

When selecting the same realm-global chat in the same project Workspace under the same
advisory lock Reuses already materialized `projection_stream_uuid` and
exact `provider_topic_id -> topic_uuid` mappings. Account-scoped external-chat
UUID So , we 're left with the control-plane identity assignment: both desired assignment
refer to one stream/topic graph, so repeat account import is not
creates a second public projection. The first selected account remains the owner
provider routing; The following same-project accounts are aliases of this
Deselect/delete the routing-owner atomically passes the route to the first
the remaining selected alias under the same realm/chat lock; removal of the usual alias
Only changes control plane and does not delete the general stream/topic graph.
The independent backfill/live deliveries of these aliases converge on realm-global
message/reaction UUID. Server Only accepts them when they match. verified realm,
project, projection stream and provider chat, keeping the first materializing
account As the stable owner of an existing projection.

## 3A — realm-global provider identity and direct conversation key

Numeric Zulip objects They use:

```text
UUIDv5(namespace=verified_realm_uuid, name="<type>:<shortest-decimal-id>")
```

Permitted `type`: `user`, `channel`, `message`, `attachment`. Numeric ID —
unsigned shortest base-10 ASCII without a sign, spaces and leading zero. Project,
account, server URL, email and mutable display name are not part of identity.

Channel key It 's got a shape . `channel:<shortest-decimal-channel-id>`. Direct/self/
group conversation key has a precise serialization:

```text
direct-conversation:v1:<count>:<id1>,<id2>,...
```

The list contains the unique provider user IDs of all participants and is mandatory
verified owner IDs are sorted by numerical value;
`count` So the same self-chat, DM or group DM has
One key for history/realtime and for all accounts of the same realm.

## 4A — Only existing authorized public actions for outbound

Generic private command «Write any Workspace model  is prohibited. Provider
API v2 is not a way to prove user intent and does not provide Bridge browser/IAM
The powers.

Workspace→Zulip operation Created only after the current public action,
which has already checked the user, project scope and permission, and then
Bridge The first part is just the ones that stay on.
Initiation paths that are not supported by the current server
with the current public API (including generic message move, mark-unread, typing and
The role/custom-profile mutations are not included.. Unknown
kind and the Workspace identity substitution are deflected to mutation.

For lifecycle mapped channel/topic the following exact semantics are recorded:

- `stream.delete` This is called the official Zulip archive-channel endpoint.
  Bridge reads the current channel status and counts already
  Archived channel reached status;
- `topic.delete` This will call the official batch delete-topic endpoint.
  `complete=false` is retryable, and the lack of a topic at the preliminary
  The reading is considered to be idympotently reached .;
- `topic.create` does not create a synthetic Zulip message: Zulip has no separate
  topic-The object, so the Bridge atomically remembers deterministic
  `<channel-id>:<topic-name>` mapping, And the first one is the usual one. `message.create`
  The name change before the first message changes only this one.
  mapping Nor does it create. provider traffic.

These three capabilities are only published for channel chats.
provider reads They're only performed on rare destructive actions, so no
Adding a constant load of realtime/history to the importer.

## 5A — state-based provider event key and separate delivery identity

`provider_event_key` describes the desired logical provider state.
for history and realtime and is independent of account, project, queue event ID,
Local sequence or Bridge database.

Before calculating the key , Bridge forms JSON object:

```json
{
  "provider_chat_key": "<exact chat key>",
  "provider_object": {"kind": "<kind>", "id": "<provider object id>"},
  "provider_references": {},
  "payload": {}
}
```

JSON is coded UTF-8 with keys in lexicographic order, separators
`,`/`:`, without additional whitespace and with `ensure_ascii=false`. payload
before normalization, server-owned Workspace IDs are removed and transport-only
metadata: `account_uuid`, `chat_key`, `delivery_class`, `external_id`,
`provider_event_uuid`. Digest — lowercase hexadecimal SHA-256 These exact bytes.

Wire key:

```text
provider-event:v1:<command-kind>:<object-kind>:<object-id-utf8-byte-length>:<object-id>:<sha256>
```

`provider_sequence` only transmits the current provider revision; local
producer sequence If the provider revision is not available, the value
That 's it . `null`.

A separate canonical UUID string `delivery_uuid` is stable at transport retry
one durable delivery, but is not a semantic identity. Workspace yields
The internal ledger .UUIDHow did you do that?:

```text
UUIDv5(verified_realm_uuid,
       "provider-delivery:v2:<provider_event_key>:<delivery_uuid>")
```

So the identical retry of one delivery is deduplicated, and the new delivery of that
The same semantic state re-enters the domain transaction and compares with the current one
The state already reached gives no-op without the extra public event;
sequence `add → remove → add` applies to the second `add`, although both add
They 're the same . `provider_event_key`.

## Data migration  native preserve and automatic Zulip reimport

Clarification after the decisions `1B`–`5A`:

- versioned migration It 's all right . authoritative native streams, topics,
  messages, user state, reactions, folders and files in canonical v2 without changing
  I 'm not going to be able to do this .;
- orphaned recipient-only UUID from historical broadcast snapshots no
  become fictitious project users: migration saves itself native
  event, but does not create a canonical membership/guard for the already deleted event IAM
  The user;
- In the same frozen migration , the proven ones are removed . Zulip-imported messages,
  related reactions/read/event projections and Zulip files, which have no
  surviving native message reference;
- Zulip-origin reaction It 's also deleted from the saved native/outbound message:
  its provider provenance is defined by `external_account_uuid`, and UUID
  The reaction is turned on to clean up old events before canonical copy. Native reaction
  It 's still in the same message .;
- Workspace→Zulip messages are considered native and are excluded from reset if their
  `message.create` user operation It 's a local origin .;
- old `link_kind=provider_identity`, account-scoped implementation,
  They're all going to be exactly `UUIDv5(verified_realm_uuid, "user:<id>")`.
  surviving native relational references, event payloads, chat catalog and
  current/pending desired resources are overwritten in the same transaction;
  `verified_account_owner` remains bound to IAM UUID and does not participate in it
  The conflict between provider identity and IAM owner stops
  migration fail-closed Instead of an implicit user association;
- selected external accounts/chats, credentials and project assignment
  For the old account-scoped format same-realm/same-chat
  stream/topic aliases Atomically, they're all one. graph: membership, folders,
  drafts, files, native messages, user topic state And the events are being transferred to
  canonical containers, after which only the extra containers are removed.
  Account It 's got a monotone .
  `projection_reset_generation`, account/chat desired generations They 're rising .,
  The state is returned to `backfill`/`syncing`;
- Bridge It keeps the last reset generation.
  Atomically deletes only the rebuildable Zulip cache/idempotency/mappings, leaving
  identity and catalog, cancels completed backfill jobs and runs a full
  Retry the same generation, nothing is dropped again.;
- The physical contents of the deleted Zulip files are processed bounded durable
  worker queue After DB commit. Before you delete the shared object again
  Checking for retained DB reference; retry is goingpotent. Worker
  registers both file-storage config domain, so local and S3 cleanup
  They use the same custom backend as Messenger API;
- When you delete native stream membership , you can delete the old broadcast audience rows of this
  membership generation The user is physically retrieved.
  It doesn 't return the events of the previous generation even after rolling view rebuild;
- logical desired-state snapshot It doesn 't build entirely in Python and it doesn 't store
  One .JSONBIt freezes like an ordered array.PostgreSQLrows with
  cascade lifetime from snapshot token; page read selects `limit + 1` rows.
  This keeps the anchor aligned and limits the RSS control API independently
  from the total number of chats and size participant/topic catalogs;
- Before reading snapshot anchor and freezing rows server takes PostgreSQL
  `SHARE ROW EXCLUSIVE` lock Snapshot is waiting for the ones already started.
  append transactions and for a short time does not allow the issuance of new sequence,
  so concurrent upsert/delete must be either frozen rows,
  Snapshot is created only at bootstrap/reset, and
  not in realtime loop; global short-stop control-plane writers easier and
  Cheaper than the permanent additional commit-order infrastructure;
- destructive reset is fail-closed: consistent `source_name`, `source.kind`,
  message identity from `source.message_id` or legacy `provider_external_id`,
  exact legacy Bridge identity
  `UUIDv5(legacy_namespace, "zulip:<account_uuid>:message:<provider_id>")`,
  account/provider evidence and durable
  `action=message.create` evidence distinguish inbound projection from native
  outbound data. The consistent `native`/`native` source pair also preserves
  legacy outbound rows created before the durable operation queue existed;
  provider identifiers attached later do not override it. An exact durable
  operation also takes precedence for a historical echo carrying inbound
  fields. Any Zulip-source UUID, including an arbitrary UUIDv5, that does not
  equal the complete legacy identity and lacks that operation is ambiguous and
  aborts before deletion. Partial or contradictory provenance aborts
  before deletion;
- an unattended frozen cutover is limited to one million legacy messages, a
  30-second lock wait and a 30-minute statement deadline. A larger cutover
  requires explicit operator authorization after backup and a production-sized
  rehearsal; the 50-million-message target is post-reimport steady state, not an
  automatic legacy-conversion allowance;
- the control-plane snapshot scale gate uses at least 15,000 assignments with
  large participant/topic catalogs and measures bounded backend RSS while
  reading bounded pages;
- The mandatory scale gate uses at least `100 000` old provider message
  mappings And proves that reset is complete, completed backfill job again
  becomes `pending`, and the old deduplication does not suppress the fresh import.

Rollback schema It doesn 't restore the intentionally destroyed Zulip projection:
This is done by using a validated pre-migration backup.
available both for upgrade and for schema downgrade.

## Compatibility and boundaries of first implementation

- Public routes, replies and WebSocket events Workspace UI are not changed.
- V2 is a closed provider data-plane, not browser API.
- Server-owned scope and canonical IDs do not open as new fields public
  Messenger resources.
- V1 transport is stored only as a rolling adapter; a new source of truth
  for provider identity is v2 contract.
- The full wire-contract is at
    [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml).
