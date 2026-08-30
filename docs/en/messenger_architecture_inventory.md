# Canonical inventory of the Messenger proposal architecture

Status: **proposal; machine-verifiable dictionary for documentation, not production schema**.

[← Main documentation index](index.md) · [Domain model](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)

This document is the sole canonical inventory of names, keys,
UUID algorithms, task/event types, and remaining OPEN decisions. Detailed
explanations are found in the linked proposal documents; the active public
contract remains in [`workspace_api.md`](workspace_api.md).

## Statuses

- **current contract** — method/path/JSON/status/event shape from
  `workspace_api.md`; the proposal does not rewrite it, except for explicitly accepted
  compatibility changes regarding pagination/timing.
- **current runtime** — replaceable implementation of `m_workspace_*`, custom store, and
  views; this is evidence, not the target architecture.
- **proposal target** — the future table/view names and
  RestAlchemy declarations selected below; migration/implementation has not yet been created.

## Proposal physical models

All tenant-owned rows carry `project_id`, have `UNIQUE(project_id, uuid)`, and
composite FKs `(project_id, referenced_uuid)`. Public UUID references are
scalar `types.UUID()` properties; internal UUID FK/identity are also scalar, but
hidden by field permissions. `relationships.relationship` is not used for
public UUIDs because it serializes as a URI.

| RestAlchemy/domain class | Target table | Public / internal fields | Business key and cardinality |
| --- | --- | --- | --- |
| `WorkspaceMessage` | `messenger_messages` | internal canonical `uuid`; hidden indexed `provider_realm_uuid`,`provider_message_id`; public content/author/source/timestamps/snapshots via view; public `provider.account_uuid` is taken from access/account projection | `UNIQUE(project_id,uuid)`; provider uniqueness is logically `(provider_realm_uuid,provider_message_id)` within the chosen cross-account project projection; one canonical content row |
| `WorkspaceMessagePlacement` | `messenger_message_placements` | public message `uuid`; internal `message_uuid`,`stream_uuid`,`topic_uuid` FK | `UNIQUE(project_id,message_uuid,stream_uuid,topic_uuid)`; many placements → one message |
| `WorkspaceUserMessageBinding` | `messenger_user_message_bindings` | hidden row `uuid`; internal `placement_uuid`,`user_uuid`,`membership_generation`, access | `UNIQUE(project_id,user_uuid,placement_uuid)`; many users → one placement |
| `WorkspaceUserMessageState` | `messenger_user_message_states` | internal `placement_uuid`,`user_uuid`,`membership_generation`; placement-scoped `read_at`,`mentioned`,`starred`,`pinned` | `UNIQUE(project_id,user_uuid,placement_uuid)`; re-add resets the same keyed row to fresh defaults/current generation |
| `WorkspaceMessageReactionFact` | `messenger_message_reaction_facts` | internal canonical message FK; public reaction row resolves placement only for access | `UNIQUE(project_id,canonical_message_uuid,user_uuid,emoji_name)`; canonical-message-global facts |
| `WorkspaceStream` | `messenger_streams` | public `uuid`; physical `owner_uuid`,`direct_user_uuid` indexed FK; canonical fields | `UNIQUE(project_id,uuid)`; one canonical stream |
| `WorkspaceStreamBinding` | `messenger_stream_bindings` | public binding UUID/role/notifications; internal `active`,`membership_generation`; ready stream counts | `UNIQUE(project_id,user_uuid,stream_uuid)`; persistent tombstone survives revoke/re-add |
| `WorkspaceStreamTopic` | `messenger_topics` | public topic UUID; canonical `stream_uuid`,`is_done`,`version`, summary/source fields | `UNIQUE(project_id,uuid)` and `UNIQUE(project_id,stream_uuid,uuid)`; exactly one immutable owner stream/project |
| `WorkspaceUserTopicBinding` | `messenger_user_topic_bindings` | access/notifications/ready counts only; no authoritative `is_done` | `UNIQUE(project_id,user_uuid,topic_uuid)` |
| `WorkspaceFolder` | `messenger_folders` | public canonical UUID/title/color/system type | `UNIQUE(project_id,uuid)`; one canonical folder |
| `WorkspaceUserFolderBinding` | `messenger_user_folder_bindings` | access/rule, ready counts, read-only materialized `folder_items_snapshot` JSONB, projection version/time | `UNIQUE(project_id,user_uuid,folder_uuid)`; one viewer row per folder |
| `WorkspaceFolderItem` | `messenger_folder_items` | authoritative normalized item fields and indexed stream/folder/user FK | `UNIQUE(project_id,user_uuid,folder_uuid,stream_uuid)`; many items → one folder binding |
| `WorkspaceUser` | `messenger_users` | public user fields/UUID; provider internals hidden | `UNIQUE(project_id,uuid)` for tenant association; canonical user identity rules remain current contract |
| `WorkspaceDomainOutboxEvent` | `messenger_domain_outbox_events` | immutable `event_kind`,`scope_kind`,`scope_key`,`payload` | `UNIQUE(project_id,uuid)`; one source mutation event |
| `WorkspaceProjectionTask` | `messenger_projection_tasks` | immutable source reference + lease/retry/DLQ lifecycle | `UNIQUE(project_id,outbox_event_uuid)`; exactly one root typed task per outbox event |
| `WorkspaceProjectionScopeLease` | `messenger_projection_scope_leases` | owner/expiry/fencing | `UNIQUE(project_id,scope_kind,scope_key)`; at most one current writer per exact scope |
| `WorkspaceFanoutRoot` | `messenger_fanout_roots` | placement/root cursor/count/status | `UNIQUE(project_id,outbox_event_uuid)`; one root per send/fanout source event |
| `WorkspaceFanoutBatchTask` | `messenger_fanout_batch_tasks` | immutable root + non-null `batch_no` + nullable keyset boundary | `UNIQUE(project_id,fanout_root_uuid,batch_no)`; sequential bounded batches, first batch `batch_no=0` |
| `WorkspaceEvent` | retained current `m_workspace_events` | public immutable event row/cursor/sanitized payload | existing event identity/cursor; projection + ready rows commit atomically |
| Files/attachments (physical names OPEN) | target table/link names not selected | current file public JSON is preserved; hidden provider identity `(realm_uuid,attachment_id)` and normalized attachment FK | one canonical file per realm+attachment; message links are separate; physical blob is deleted only upon zero references |

## Read-only API models/views

Each view has one leading physical row and only indexed one-to-one or
many-to-one joins. `COUNT`, `GROUP BY`, window/lateral/correlated query,
`json_agg`, N+1, and custom SQL store are prohibited.

| RestAlchemy read model | Target view | Leading row / public identity | Ready sources |
| --- | --- | --- | --- |
| `WorkspaceUserMessage` | `messenger_api_user_messages_v1` | leading `WorkspaceUserMessageBinding`; hidden ORM key `binding_uuid`; public `uuid = MESSAGE_PLACEMENT.uuid` | placement context/state + canonical message/timestamps/snapshots; active stream membership+generation security join |
| `WorkspaceMessageReactionView` | `messenger_api_message_reactions_v1` | leading raw fact/access-scoped placement; public message UUID = placement UUID | fact row + sanitized provider/delivery; canonical global semantics |
| `WorkspaceUserStream` | `messenger_api_user_streams_v1` | leading active stream binding; public UUID = canonical stream UUID | ready counts from binding + one stream; `owner_uuid AS owner`; viewer-relative scalar `direct_user_uuid` |
| `WorkspaceStreamBindingView` | `messenger_api_stream_bindings_v1` | leading persistent stream binding; public binding UUID | binding fields; viewer/project scope |
| `WorkspaceUserTopic` | `messenger_api_user_topics_v1` | leading topic binding; public UUID = canonical topic UUID | ready counts/notifications from binding + canonical `TOPIC.is_done`/summary/timestamps |
| `WorkspaceUserFolder` | `messenger_api_user_folders_v1` | leading folder binding; public UUID = canonical folder UUID | one folder join + ordinary read-only `folder_items_snapshot` property + ready counts |

## UUID and identity

| Identity | Canonical rule |
| --- | --- |
| canonical message | internal `MESSAGE.uuid`; one content row, not public message resource ID |
| public message/URL `{message_uuid}` | `MESSAGE_PLACEMENT.uuid` |
| placement UUIDv5 | `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)` |
| UUIDv5 name bytes | only lowercase hyphenated ASCII canonical `MESSAGE.uuid`, no braces, prefix or additional fields |
| topic requirement | `TOPIC` is mandatory for every placement, including direct/self-chat; null/sentinel are prohibited |
| topic ownership | globally unique `TOPIC.uuid`, immutable belonging to exactly one stream/project; transfer creates a new topic/placement migration |
| authoritative placement uniqueness | `(project_id,message_uuid,stream_uuid,topic_uuid)` plus composite FK; UUIDv5 does not replace constraints |
| hidden row identities | binding/state/view technical UUIDs are not published and not used in message URL/marker |
| message pagination tuple | public `(MESSAGE.created_at, MESSAGE_PLACEMENT.uuid)`; page rows are unique by placement UUID |
| Zulip numeric provider objects | `UUIDv5(namespace=verified_realm_uuid, name="<entity_type>:<decimal_provider_id>")`; realm text canonical lowercase hyphenated → 16 RFC 4122/network-order octets; allowed types `user/channel/message/attachment`; ID unsigned shortest base-10 ASCII (`0` or no leading zeros); name exact ASCII/UTF-8; no project/account namespace |
| Zulip topic | Workspace-owned durable realm+channel+name/alias-history mapping → stable `TOPIC.uuid`; mutable name alone never defines UUID |
| Zulip Bridge account ownership | sticky whole-account assignment to minimum normalized-load healthy compatible instance; realtime+history share one fenced owner; heartbeat `10s`, degraded `30s`, offline/takeover `60s` |
| Zulip Bridge S2S authentication | current `workspace-external-bridge-api` realm-bound TLS 1.2+ mTLS; certificate URI SAN = realm/provider/bridge instance/identity generation; one-time enrollment + 30-day leaf/7-day renewal/24-hour overlap; account lease is separate authorization fence |
| Zulip history scheduling | Bridge-wide pool default `4`; fair round-robin accounts, newest stream first, one worker per stream, shared account limiter, realtime resumes first after `Retry-After`; upper pool limit OPEN pending load tests |
| Zulip internal retention | mappings/latest raw metadata = entity lifetime; completed history/successful outbound = `30 days`; permanent failure/code/reason = `90 days` |

## TASK_KINDS and routing {#task_kinds-и-routing}

Initial design does not perform coalescing. Each immutable outbox event emits
exactly one immutable root task per `UNIQUE(project_id,outbox_event_uuid)`.
Downstream shared work first receives a separate immutable outbox event with exact
scope, then one task; direct collapsing of events is prohibited.

| `task_kind` | `scope_kind` / exact key | Sole writable result |
| --- | --- | --- |
| `fanout` | `topic:(project_id,topic_uuid)` | bounded recipient binding+state batches for placements |
| `content_mentions` | `topic:(project_id,topic_uuid)` | placement-scoped mention state; shared work emits exact-scope outbox |
| `reaction_snapshot` | `message:(project_id,canonical_message_uuid)` | canonical `MESSAGE.reactions/reaction_users` |
| `read_counters` | `user-stream`, `user-topic` exact triples | ready container counts/last message on corresponding binding |
| `folder_projection` | `user-folder:(project_id,user_uuid,folder_uuid)` | authoritative deterministic `folder_items_snapshot` + ready folder counts/event |
| `delivery_snapshot_event` | `message:(project_id,canonical_message_uuid)` or `resource:(project_id,resource_kind,resource_uuid)` | sanitized delivery/resource projection + ready event, or effect-guarded completion without public row for contract families where no public event exists |
| `topic_state_projection` | `topic:(project_id,topic_uuid)` | ready `topic.updated`; optional rebuildable read-only copy of canonical `is_done` |
| `topic_membership_policy_rebuild` | `topic:(project_id,topic_uuid)` | topic placement/binding policy; shared rows use downstream exact scopes |

Task lifecycle: `pending -> leased/running -> completed`; retryable failure uses
`failed -> pending` with `attempts`, `next_retry_at`, backoff and lease expiry;
`max_attempts` moves to DLQ. Owner/fencing token, reaper/reconciliation and
idempotent `outbox_event_uuid` effect guard are mandatory.

## DOMAIN_EVENT_KINDS

Internal `WorkspaceDomainOutboxEvent.event_kind` uses the same closed
enum of eight values as `task_kind`: `fanout`, `content_mentions`,
`reaction_snapshot`, `read_counters`, `folder_projection`,
`delivery_snapshot_event`, `topic_state_projection`,
`topic_membership_policy_rebuild`. Therefore event→task derivation is mechanically
unambiguous and does not parse arbitrary strings. Illustrative domain labels
like `draft.created` or `folder_item.pin` are stored as `payload.source_kind`;
they are not routing EVENT_KIND and do not match public WebSocket kind.

## Public EVENT_KINDS

This is the exhaustive list of `payload.kind`, preserved from current public contract:

`external_account.created`, `external_account.updated`,
`external_account.deleted`, `external_chat.created`, `external_chat.updated`,
`external_chat.deleted`, `external_operation.created`,
`external_operation.updated`, `external_operation.deleted`, `file.created`,
`file.updated`, `file.deleted`, `folder.created`, `folder.updated`,
`folder.deleted`, `folder_item.deleted`, `message.created`, `message.updated`,
`message.deleted`, `message.read`, `messages.read`,
`message_reaction.created`, `message_reaction.updated`,
`message_reaction.deleted`, `stream.created`, `stream.updated`,
`stream.deleted`, `stream.read`, `stream_bindings.created`,
`stream_binding.updated`, `stream_binding.deleted`, `topic.created`,
`topic.updated`, `topic.deleted`, `topic.read`, `user.updated`.

Worker materializes projection/state and every corresponding ready event row in
one DB transaction. Dispatcher only reads durable rows. Reconnect uses mandatory
cursor + high-watermark + replay + buffer/drain without gap; delivery is
at-least-once and clients dedupe by event UUID.

## Accepted compatibility/operational rules

- all public resource-list endpoints: omitted/`0` `page_limit` → `100`, `1..500`
  exact, negative/non-integer/`>500` → HTTP `400`, unlimited mode is absent;
- `2xx`/`201` means commit primary mutation, not completion projections;
  author receives immediate RYW, other effects are eventual; approximately one
  second is SLO intent, not strict guarantee;
- fan-out recipients: keyset `USER_STREAM_BINDING.user_uuid ASC`, default batch
  `1000`, configurable hard max `5000`, invalid config fails startup;
- newest-first topic order: `MESSAGE.created_at DESC`, bounded fairness must
  provide eventual progress for old work;
- revoke: persistent `active=false`, `membership_generation++`; all public
  reads/actions recheck active membership+generation synchronously;
- `TOPIC.is_done` — canonical global field; user-topic binding is not writable
  source;
- reactions are canonical-message-global across all placements/audiences — intentionally
  accepted privacy semantics;
- folder item normalized rows remain source-of-truth; JSONB snapshot is only
  rebuildable read model;
- release is performed per
  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md):
  native data is preserved, Zulip-derived messages/files undergo accepted
  destructive reset and fresh complete reimport after verified backup.

## Critic risks status

| Risk | Status / canonical resolution |
| --- | --- |
| #1 | **resolved:** public UUID = deterministic placement UUIDv5 |
| #2 | **resolved:** active membership + monotonic generation security fence |
| #3 | **resolved:** no coalescing; one event→task + lease/retry/reaper/DLQ |
| #4 | **resolved:** exact-scope ownership; topic is not a universal shared lock |
| #5 | **resolved:** accepted pagination `100/500` and async timing change |
| #6 | **resolved:** canonical `TOPIC.is_done` + lock/version |
| #7 | **partially resolved:** tenant/composite FK/recheck closed; non-direct role policy remains OPEN |
| #8 | **accepted:** canonical-message-global cross-audience reactions |
| #9 | **resolved:** atomic projection+ready events; mandatory replay |
| #10 | **resolved:** bounded fan-out batches `1000/5000` |
| #11 | **resolved runbook/safety boundary:** backup+migrations+manual scripts; native preserve, Zulip reset/reimport; provider file identity realm+attachment and zero-reference cleanup are fixed |
| #12 | **resolved:** materialized `folder_items_snapshot`, no N+1/json aggregation read path |
| #13 | **resolved:** cross-document consolidation and machine-checkable QA confirmed 109 semantic HTTP operations + 1 WS, 7 operational worker flows, unified model/key/task/event/UUID rules and absence of stale/duplicate/orphan artifacts |

## Sole list of OPEN decisions {#единственный-список-open-решений}

1. Non-direct stream role/action matrix: who adds a user, which roles
   are assigned, who changes/removes self/other binding, whether last owner is mandatory.
2. Specific runtime mechanism for exact-scope lease/claim and configurable worker
   execution primitive; invariant fencing is already closed.
3. Stable worker tie-breaker when `MESSAGE.created_at` is identical and the need for an
   immutable denormalized sort key after measurements; API tuple is already closed.
4. Numeric durable-event retention window/release policy; cursor-too-old already
   provides explicit `epoch_pruned`/`410`.
5. Target physical table names/schema for canonical provider files and normalized
   message↔file links. Identity is already chosen as `(realm_uuid,attachment_id)`,
   zero-reference delete is mandatory; only concrete landing tables/FK and
   migration mapping of current file rows remain OPEN.
6. Capacity/SLO tuning after load measurements: worker concurrency,
   fan-out batch in the range `1..5000`, queue admission/backpressure, retention,
   numeric count/bytes limits `folder_items_snapshot` and overflow policy compatible with full
   response `All chats`; architectural hard boundaries and
   prohibition of silent truncation are already chosen.
7. Stable public placement association for UUID-only
   `GET`/`PUT`/`DELETE message_reactions/{reaction_uuid}` with multiple
   placements of one canonical message: current path does not carry placement UUID,
   so migration/model must explicitly choose how to preserve or
   restore public `message_uuid` and access context. Hidden binding UUID and
   arbitrary/primary placement are prohibited; accepted message-global reaction
   semantics are not reconsidered.
8. Physical representation of polymorphic public
   `ExternalOperation.target_uuid`: target schema must choose canonical
   registry of targets or typed FK-columns for stream/topic/message, without
   changing current JSON `target_uuid`; one unverified polymorphic FK
   is prohibited.

Target ingestion, service identity, registration boundary and recovery for
Zulip are described without duplicating this inventory in
[`zulip_bridge/README.md`](zulip_bridge/README.md). Its OPEN-list specifies
transport/provider-key questions for Bridge; current mTLS authentication is already chosen,
and common Messenger model/task/event
names remain canonical here. Exact Zulip event/op directions and
echo-prevention boundary are canonically defined in
[`zulip_bridge/event_coverage.md`](zulip_bridge/event_coverage.md).

[← Main documentation index](index.md) · [Domain model](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)
