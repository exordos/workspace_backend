# Канонический инвентарь proposal-архитектуры Messenger

Статус: **proposal; машинно-сверяемый словарь для документации, не production schema**.

[← Главный индекс документации](index.md) · [Доменная модель](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)

Этот документ является единственным каноническим инвентарём имён, ключей,
UUID-алгоритмов, видов задач/событий и оставшихся OPEN-решений. Подробные
объяснения находятся в связанных proposal-документах; действующий публичный
контракт остаётся в [`workspace_api.md`](workspace_api.md).

## Статусы

- **current contract** — method/path/JSON/status/event shape из
  `workspace_api.md`; proposal его не переписывает, кроме явно принятых
  compatibility changes pagination/timing.
- **current runtime** — заменяемая реализация `m_workspace_*`, custom store и
  views; это evidence, а не target architecture.
- **proposal target** — выбранные ниже имена будущих таблиц/views и
  RestAlchemy declarations; migration/implementation ещё не созданы.

## Физические модели proposal

Все tenant-owned строки несут `project_id`, имеют `UNIQUE(project_id, uuid)` и
составные FK `(project_id, referenced_uuid)`. Публичные UUID-ссылки являются
scalar `types.UUID()` properties; internal UUID FK/identity тоже scalar, но
скрыты field permissions. `relationships.relationship` не используется для
публичного UUID, потому что он сериализуется как URI.

| RestAlchemy/domain class | Target table | Public / internal fields | Business key и кардинальность |
| --- | --- | --- | --- |
| `WorkspaceMessage` | `messenger_messages` | internal canonical `uuid`; hidden indexed `provider_realm_uuid`,`provider_message_id`; public content/author/source/timestamps/snapshots через view; public `provider.account_uuid` берётся из access/account projection | `UNIQUE(project_id,uuid)`; provider uniqueness logically `(provider_realm_uuid,provider_message_id)` within the chosen cross-account project projection; одна canonical content row |
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
| Files/attachments (physical names OPEN) | target table/link names не выбраны | current file public JSON сохраняется; hidden provider identity `(realm_uuid,attachment_id)` и normalized attachment FK | одна canonical file на realm+attachment; message links separate; physical blob удаляется только при zero references |

## Read-only API models/views

Каждый view имеет одну leading physical row и только indexed one-to-one или
many-to-one joins. `COUNT`, `GROUP BY`, window/lateral/correlated query,
`json_agg`, N+1 и custom SQL store запрещены.

| RestAlchemy read model | Target view | Leading row / public identity | Готовые источники |
| --- | --- | --- | --- |
| `WorkspaceUserMessage` | `messenger_api_user_messages_v1` | leading `WorkspaceUserMessageBinding`; hidden ORM key `binding_uuid`; public `uuid = MESSAGE_PLACEMENT.uuid` | placement context/state + canonical message/timestamps/snapshots; active stream membership+generation security join |
| `WorkspaceMessageReactionView` | `messenger_api_message_reactions_v1` | leading raw fact/access-scoped placement; public message UUID = placement UUID | fact row + sanitized provider/delivery; canonical global semantics |
| `WorkspaceUserStream` | `messenger_api_user_streams_v1` | leading active stream binding; public UUID = canonical stream UUID | ready counts from binding + one stream; `owner_uuid AS owner`; viewer-relative scalar `direct_user_uuid` |
| `WorkspaceStreamBindingView` | `messenger_api_stream_bindings_v1` | leading persistent stream binding; public binding UUID | binding fields; viewer/project scope |
| `WorkspaceUserTopic` | `messenger_api_user_topics_v1` | leading topic binding; public UUID = canonical topic UUID | ready counts/notifications from binding + canonical `TOPIC.is_done`/summary/timestamps |
| `WorkspaceUserFolder` | `messenger_api_user_folders_v1` | leading folder binding; public UUID = canonical folder UUID | one folder join + ordinary read-only `folder_items_snapshot` property + ready counts |

## UUID и identity

| Identity | Каноническое правило |
| --- | --- |
| canonical message | internal `MESSAGE.uuid`; одна content row, не public message resource ID |
| public message/URL `{message_uuid}` | `MESSAGE_PLACEMENT.uuid` |
| placement UUIDv5 | `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)` |
| UUIDv5 name bytes | только lowercase hyphenated ASCII canonical `MESSAGE.uuid`, без braces, prefix или дополнительных полей |
| topic requirement | `TOPIC` обязателен для каждого placement, включая direct/self-chat; null/sentinel запрещены |
| topic ownership | глобально уникальный `TOPIC.uuid`, immutable принадлежность ровно одному stream/project; перенос создаёт новый topic/placement migration |
| authoritative placement uniqueness | `(project_id,message_uuid,stream_uuid,topic_uuid)` плюс composite FK; UUIDv5 не заменяет constraints |
| hidden row identities | binding/state/view technical UUID не публикуются и не используются в message URL/marker |
| message pagination tuple | public `(MESSAGE.created_at, MESSAGE_PLACEMENT.uuid)`; page rows уникальны по placement UUID |
| Zulip numeric provider objects | `UUIDv5(namespace=verified_realm_uuid, name="<entity_type>:<decimal_provider_id>")`; realm text canonical lowercase hyphenated → 16 RFC 4122/network-order octets; allowed types `user/channel/message/attachment`; ID unsigned shortest base-10 ASCII (`0` либо no leading zeros); name exact ASCII/UTF-8; no project/account namespace |
| Zulip topic | Workspace-owned durable realm+channel+name/alias-history mapping → stable `TOPIC.uuid`; mutable name alone never defines UUID |
| Zulip Bridge account ownership | sticky whole-account assignment to minimum normalized-load healthy compatible instance; realtime+history share one fenced owner; heartbeat `10s`, degraded `30s`, offline/takeover `60s` |
| Zulip Bridge S2S authentication | current `workspace-external-bridge-api` realm-bound TLS 1.2+ mTLS; certificate URI SAN = realm/provider/bridge instance/identity generation; one-time enrollment + 30-day leaf/7-day renewal/24-hour overlap; account lease is separate authorization fence |
| Zulip history scheduling | Bridge-wide pool default `4`; fair round-robin accounts, newest stream first, one worker per stream, shared account limiter, realtime resumes first after `Retry-After`; upper pool limit OPEN pending load tests |
| Zulip internal retention | mappings/latest raw metadata = entity lifetime; completed history/successful outbound = `30 days`; permanent failure/code/reason = `90 days` |

## TASK_KINDS и routing {#task_kinds-и-routing}

Initial design не выполняет coalescing. Каждое immutable outbox event выводит
ровно одну immutable root task по `UNIQUE(project_id,outbox_event_uuid)`.
Downstream shared work сначала получает отдельное immutable outbox event exact
scope, затем одну task; прямое схлопывание событий запрещено.

| `task_kind` | `scope_kind` / exact key | Единственный writable result |
| --- | --- | --- |
| `fanout` | `topic:(project_id,topic_uuid)` | bounded recipient binding+state batches for placements |
| `content_mentions` | `topic:(project_id,topic_uuid)` | placement-scoped mention state; shared work emits exact-scope outbox |
| `reaction_snapshot` | `message:(project_id,canonical_message_uuid)` | canonical `MESSAGE.reactions/reaction_users` |
| `read_counters` | `user-stream`, `user-topic` exact triples | ready container counts/last message on corresponding binding |
| `folder_projection` | `user-folder:(project_id,user_uuid,folder_uuid)` | authoritative deterministic `folder_items_snapshot` + ready folder counts/event |
| `delivery_snapshot_event` | `message:(project_id,canonical_message_uuid)` либо `resource:(project_id,resource_kind,resource_uuid)` | sanitized delivery/resource projection + ready event, либо effect-guarded completion без public row для contract families, где public event отсутствует |
| `topic_state_projection` | `topic:(project_id,topic_uuid)` | ready `topic.updated`; optional rebuildable read-only copy of canonical `is_done` |
| `topic_membership_policy_rebuild` | `topic:(project_id,topic_uuid)` | topic placement/binding policy; shared rows use downstream exact scopes |

Task lifecycle: `pending -> leased/running -> completed`; retryable failure uses
`failed -> pending` with `attempts`, `next_retry_at`, backoff and lease expiry;
`max_attempts` moves to DLQ. Owner/fencing token, reaper/reconciliation and
idempotent `outbox_event_uuid` effect guard are mandatory.

## DOMAIN_EVENT_KINDS

Внутренний `WorkspaceDomainOutboxEvent.event_kind` использует тот же закрытый
enum из восьми значений, что `task_kind`: `fanout`, `content_mentions`,
`reaction_snapshot`, `read_counters`, `folder_projection`,
`delivery_snapshot_event`, `topic_state_projection`,
`topic_membership_policy_rebuild`. Поэтому event→task derivation механически
однозначна и не разбирает произвольную строку. Иллюстративные доменные labels
вроде `draft.created` или `folder_item.pin` хранятся как `payload.source_kind`;
они не являются routing EVENT_KIND и не совпадают с public WebSocket kind.

## Public EVENT_KINDS

Это исчерпывающий список `payload.kind`, сохранённый из current public contract:

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

## Принятые compatibility/operational правила

- all public resource-list endpoints: omitted/`0` `page_limit` → `100`, `1..500`
  exact, negative/non-integer/`>500` → HTTP `400`, unlimited mode отсутствует;
- `2xx`/`201` означает commit primary mutation, не completion projections;
  author получает immediate RYW, остальные эффекты eventual; примерно одна
  секунда — SLO intent, не строгая гарантия;
- fan-out recipients: keyset `USER_STREAM_BINDING.user_uuid ASC`, default batch
  `1000`, configurable hard max `5000`, invalid config fails startup;
- newest-first topic order: `MESSAGE.created_at DESC`, bounded fairness обязана
  давать eventual progress старой работе;
- revoke: persistent `active=false`, `membership_generation++`; all public
  reads/actions recheck active membership+generation synchronously;
- `TOPIC.is_done` — canonical global field; user-topic binding не writable
  source;
- reactions canonical-message-global во всех placements/audiences — намеренно
  принятая privacy semantics;
- folder item normalized rows остаются source-of-truth; JSONB snapshot только
  rebuildable read model;
- release выполняется по
  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md):
  native data сохраняются, Zulip-derived messages/files проходят принятый
  destructive reset и fresh complete reimport после verified backup.

## Статус Critic risks

| Risk | Статус / каноническое решение |
| --- | --- |
| #1 | **resolved:** public UUID = deterministic placement UUIDv5 |
| #2 | **resolved:** active membership + monotonic generation security fence |
| #3 | **resolved:** no coalescing; one event→task + lease/retry/reaper/DLQ |
| #4 | **resolved:** exact-scope ownership; topic не universal shared lock |
| #5 | **resolved:** accepted pagination `100/500` и async timing change |
| #6 | **resolved:** canonical `TOPIC.is_done` + lock/version |
| #7 | **partially resolved:** tenant/composite FK/recheck закрыты; non-direct role policy остаётся OPEN |
| #8 | **accepted:** canonical-message-global cross-audience reactions |
| #9 | **resolved:** atomic projection+ready events; mandatory replay |
| #10 | **resolved:** bounded fan-out batches `1000/5000` |
| #11 | **resolved runbook/safety boundary:** backup+migrations+manual scripts; native preserve, Zulip reset/reimport; provider file identity realm+attachment and zero-reference cleanup are fixed |
| #12 | **resolved:** materialized `folder_items_snapshot`, no N+1/json aggregation read path |
| #13 | **resolved:** cross-document consolidation и machine-checkable QA подтвердили 109 semantic HTTP operations + 1 WS, 7 operational worker flows, единые model/key/task/event/UUID rules и отсутствие stale/duplicate/orphan artifacts |

## Единственный список OPEN-решений {#единственный-список-open-решений}

1. Non-direct stream role/action matrix: кто добавляет пользователя, какие roles
   назначает, кто меняет/удаляет self/other binding, обязателен ли last owner.
2. Конкретный runtime mechanism exact-scope lease/claim и configurable worker
   execution primitive; invariant fencing уже закрыт.
3. Stable worker tie-breaker при одинаковом `MESSAGE.created_at` и необходимость
   immutable denormalized sort key после измерений; API tuple уже закрыт.
4. Численное durable-event retention window/release policy; cursor-too-old уже
   даёт явный `epoch_pruned`/`410`.
5. Target physical table names/schema для canonical provider files и normalized
   message↔file links. Identity уже выбрана как `(realm_uuid,attachment_id)`,
   zero-reference delete обязателен; OPEN только concrete landing tables/FK и
   migration mapping current file rows.
6. Capacity/SLO tuning после нагрузочных измерений: worker concurrency,
   fan-out batch в диапазоне `1..5000`, queue admission/backpressure, retention,
   числовые count/bytes limits `folder_items_snapshot` и совместимая с полным
   ответом политика переполнения `All chats`; архитектурные hard boundaries и
   запрет silent truncation уже выбраны.
7. Стабильная public placement association для UUID-only
   `GET`/`PUT`/`DELETE message_reactions/{reaction_uuid}` при нескольких
   placements одной canonical message: current path не несёт placement UUID,
   поэтому migration/model должны явно выбрать способ сохранить или
   восстановить public `message_uuid` и access context. Hidden binding UUID и
   arbitrary/primary placement запрещены; accepted message-global reaction
   semantics при этом не пересматривается.
8. Физическое представление полиморфного public
   `ExternalOperation.target_uuid`: target schema должна выбрать канонический
   реестр целей либо типизированные FK-колонки для stream/topic/message, не
   меняя текущий JSON `target_uuid`; один непроверяемый полиморфный FK
   запрещён.

Target ingestion, service identity, registration boundary и recovery для
Zulip описаны без дублирования этого инвентаря в
[`zulip_bridge/README.md`](zulip_bridge/README.md). Его OPEN-list конкретизирует
transport/provider-key вопросы Bridge; current mTLS authentication уже выбрана,
а общие Messenger model/task/event
имена по-прежнему каноничны здесь. Exact Zulip event/op directions и
echo-prevention boundary канонически заданы в
[`zulip_bridge/event_coverage.md`](zulip_bridge/event_coverage.md).

[← Главный индекс документации](index.md) · [Доменная модель](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)
