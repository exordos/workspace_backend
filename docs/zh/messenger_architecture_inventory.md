# 建议架构的正规库 Messenger

状态: **提案; 机器检查的文档词典, 不 production schema**.

[← 文件的主要索引](index.md) · [域名模型](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)

这份文件是唯一的名称,密钥,,
UUID-算法,任务/事件类型和剩余的 OPEN 解决方案.详细
解释在相关的proposal文件中;
合同仍然在 [`workspace_api.md`](workspace_api.md).

## 他们的地位

- **current contract** — method/path/JSON/status/event shape 根据
  `workspace_api.md`; proposal 只有明确的记者能记下它.
  compatibility changes pagination/timing.
- **current runtime** — 替换实现`m_workspace_*`,自定义商店和
  views; 这不是证据, target architecture.
- **proposal target** — 下面选择了未来表格的名称/views和
  RestAlchemy declarations; migration/implementation 尚未创建.

## 物理模型 proposal

所有的租户拥有的行都带有 `project_id`,有 `UNIQUE(project_id, uuid)` 和
组合FK`(project_id, referenced_uuid)`公共活动UUID- 链接是
scalar `types.UUID()` properties; internal UUID FK/identity 也是个斯卡拉,但
隐藏的域权限. `relationships.relationship` 不用于
公共的 UUID,因为它是串行的 URI.

| RestAlchemy/domain class | Target table | Public / internal fields | Business key 它们的特点是: |
| --- | --- | --- | --- |
| `WorkspaceMessage` | `messenger_messages` | internal canonical `uuid`; hidden indexed `provider_realm_uuid`,`provider_message_id`; public content/author/source/timestamps/snapshots 通过 view; public `provider.account_uuid` 是从 access/account projection | `UNIQUE(project_id,uuid)`; provider uniqueness logically `(provider_realm_uuid,provider_message_id)` within the chosen cross-account project projection; 一个 canonical content row |
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
| Files/attachments (physical names OPEN) | target table/link names 没有选择 | current file public JSON 保存;隐藏提供者身份 `(realm_uuid,attachment_id)` 和 normalized attachment FK | 一个 canonical file 在 realm+attachment; message links separate; 只有在 zero references |

## Read-only API models/views

每个视图有一个领先的物理行,只被一个对一个或
many-to-one joins. `COUNT`, `GROUP BY`, window/lateral/correlated query,
`json_agg`, N+1 和 custom SQL store 禁止使用.

| RestAlchemy read model | Target view | Leading row / public identity | 准备好的来源 |
| --- | --- | --- | --- |
| `WorkspaceUserMessage` | `messenger_api_user_messages_v1` | leading `WorkspaceUserMessageBinding`; hidden ORM key `binding_uuid`; public `uuid = MESSAGE_PLACEMENT.uuid` | placement context/state + canonical message/timestamps/snapshots; active stream membership+generation security join |
| `WorkspaceMessageReactionView` | `messenger_api_message_reactions_v1` | leading raw fact/access-scoped placement; public message UUID = placement UUID | fact row + sanitized provider/delivery; canonical global semantics |
| `WorkspaceUserStream` | `messenger_api_user_streams_v1` | leading active stream binding; public UUID = canonical stream UUID | ready counts from binding + one stream; `owner_uuid AS owner`; viewer-relative scalar `direct_user_uuid` |
| `WorkspaceStreamBindingView` | `messenger_api_stream_bindings_v1` | leading persistent stream binding; public binding UUID | binding fields; viewer/project scope |
| `WorkspaceUserTopic` | `messenger_api_user_topics_v1` | leading topic binding; public UUID = canonical topic UUID | ready counts/notifications from binding + canonical `TOPIC.is_done`/summary/timestamps |
| `WorkspaceUserFolder` | `messenger_api_user_folders_v1` | leading folder binding; public UUID = canonical folder UUID | one folder join + ordinary read-only `folder_items_snapshot` property + ready counts |

## UUID 其他 identity

| Identity | 规范规则 |
| --- | --- |
| canonical message | internal `MESSAGE.uuid`; 一个内容行,没有 public message resource ID |
| public message/URL `{message_uuid}` | `MESSAGE_PLACEMENT.uuid` |
| placement UUIDv5 | `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)` |
| UUIDv5 name bytes | 只有小写字母,连接字母 ASCII 和正文 `MESSAGE.uuid`,没有括号,前或额外字段 |
| topic requirement | `TOPIC` 对于每一个 placement 都是强制性的,包括 direct/self-chat; null/sentinel 禁止 |
| topic ownership | 全球独一无二`TOPIC.uuid`,完全属于一个流/project;转移创造了一个新的流 topic/placement migration |
| authoritative placement uniqueness | `(project_id,message_uuid,stream_uuid,topic_uuid)` 加 Composite FK; UUIDv5 不替换 constraints |
| hidden row identities | binding/state/view technical UUID 没有发表,也没有在 message URL/marker |
| message pagination tuple | public `(MESSAGE.created_at, MESSAGE_PLACEMENT.uuid)`; page rows 唯一的 placement UUID |
| Zulip numeric provider objects | `UUIDv5(namespace=verified_realm_uuid, name="<entity_type>:<decimal_provider_id>")`; realm text canonical lowercase hyphenated → 16 RFC 4122/network-order octets; allowed types `user/channel/message/attachment`; ID unsigned shortest base-10 ASCII (`0` 没有 no leading zeros); name exact ASCII/UTF-8; no project/account namespace |
| Zulip topic | Workspace-owned durable realm+channel+name/alias-history mapping → stable `TOPIC.uuid`; mutable name alone never defines UUID |
| Zulip Bridge account ownership | sticky whole-account assignment to minimum normalized-load healthy compatible instance; realtime+history share one fenced owner; heartbeat `10s`, degraded `30s`, offline/takeover `60s` |
| Zulip Bridge S2S authentication | current `workspace-external-bridge-api` realm-bound TLS 1.2+ mTLS; certificate URI SAN = realm/provider/bridge instance/identity generation; one-time enrollment + 30-day leaf/7-day renewal/24-hour overlap; account lease is separate authorization fence |
| Zulip history scheduling | Bridge-wide pool default `4`; fair round-robin accounts, newest stream first, one worker per stream, shared account limiter, realtime resumes first after `Retry-After`; upper pool limit OPEN pending load tests |
| Zulip internal retention | mappings/latest raw metadata = entity lifetime; completed history/successful outbound = `30 days`; permanent failure/code/reason = `90 days` |

## TASK_KINDS 其他 routing {#task_kinds-и-routing}

Initial design 任何一个 immutable 输出箱事件都会产生
按一个 immutable root 任务 `UNIQUE(project_id,outbox_event_uuid)`.
Downstream shared work 首先获得单独的 immutable outbox event exact
scope, 然后一个任务; 直接对接事件是禁止的.

| `task_kind` | `scope_kind` / exact key | 唯一的 writable result |
| --- | --- | --- |
| `fanout` | `topic:(project_id,topic_uuid)` | bounded recipient binding+state batches for placements |
| `content_mentions` | `topic:(project_id,topic_uuid)` | placement-scoped mention state; shared work emits exact-scope outbox |
| `reaction_snapshot` | `message:(project_id,canonical_message_uuid)` | canonical `MESSAGE.reactions/reaction_users` |
| `read_counters` | `user-stream`, `user-topic` exact triples | ready container counts/last message on corresponding binding |
| `folder_projection` | `user-folder:(project_id,user_uuid,folder_uuid)` | authoritative deterministic `folder_items_snapshot` + ready folder counts/event |
| `delivery_snapshot_event` | `message:(project_id,canonical_message_uuid)` 没有 `resource:(project_id,resource_kind,resource_uuid)` | sanitized delivery/resource projection + ready event, 或是没有公众行的效果保护完成,对于没有公众事件的合同家庭 |
| `topic_state_projection` | `topic:(project_id,topic_uuid)` | ready `topic.updated`; optional rebuildable read-only copy of canonical `is_done` |
| `topic_membership_policy_rebuild` | `topic:(project_id,topic_uuid)` | topic placement/binding policy; shared rows use downstream exact scopes |

Task lifecycle: `pending -> leased/running -> completed`; retryable failure uses
`failed -> pending` with `attempts`, `next_retry_at`, backoff and lease expiry;
`max_attempts` moves to DLQ. Owner/fencing token, reaper/reconciliation and
idempotent `outbox_event_uuid` effect guard are mandatory.

任务领取路径按加权队列周期运行：四个 fan-out 槽、两个交互式已读槽、
一个 reaction 槽、一个非交互式 read-state 槽和两个后台槽。每一轮先在
所有项目中选择首选队列里最早的可领取任务；若该队列为空，则回退到任意
队列中最早的可领取任务。项目扫描和最终领取使用相同的前置任务、重试和
scope lease 条件。有界候选查询和活动任务的部分索引避免对全部任务历史
排序；scope lease、项目 advisory lock 和 fencing token 保证多个 worker
安全并行。

## DOMAIN_EVENT_KINDS

内部 `WorkspaceDomainOutboxEvent.event_kind` 使用相同的封闭
enum 对于这八个值, `task_kind`: `fanout`, `content_mentions`,
`reaction_snapshot`, `read_counters`, `folder_projection`,
`delivery_snapshot_event`, `topic_state_projection`,
`topic_membership_policy_rebuild`. 因此,事件→任务导出是机械的
并且不分解任意的字符串. labels
像是这样.`draft.created`没有`folder_item.pin`保存为 `payload.source_kind`;
它们不是路由 EVENT_KIND 并不与 public WebSocket kind.

## Public EVENT_KINDS

这是一个完整的清单.`payload.kind`保存在 current public contract:

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

## 已接受的兼容性规则/operational

- all public resource-list endpoints: omitted/`0` `page_limit` → `100`, `1..500`
  exact, negative/non-integer/`>500` → HTTP `400`, unlimited mode 没有;
- `2xx`/`201` 意思是"提交主要突变", completion projections;
  author 得到即时RYW,其余效应是最终的;大约一个
  秒  SLO 意图,没有严格的保证;
- fan-out recipients: keyset `USER_STREAM_BINDING.user_uuid ASC`, default batch
  `1000`, configurable hard max `5000`, invalid config fails startup;
- newest-first topic order: `MESSAGE.created_at DESC`, bounded fairness 必须
  给最终的进展旧工作;
- revoke: persistent `active=false`, `membership_generation++`; all public
  reads/actions recheck active membership+generation synchronously;
- `TOPIC.is_done` — canonical global field; user-topic binding 没有 writable
  source;
- reactions canonical-message-global 在所有配置中/audiences 故意
  已通过 privacy semantics;
- folder item normalized rows 剩下的都是 source-of-truth; JSONB 只是快照
  rebuildable read model;
- release 根据
    [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md):
  native data 保存, Zulip-derived messages/files 通过接受
  destructive reset 和后的fresh complete reimport verified backup.

## 情况 Critic risks

| Risk | 法律地位 / 法律决定 |
| --- | --- |
| #1 | **resolved:** public UUID = deterministic placement UUIDv5 |
| #2 | **resolved:** active membership + monotonic generation security fence |
| #3 | **resolved:** no coalescing; one event→task + lease/retry/reaper/DLQ |
| #4 | **resolved:** exact-scope ownership; topic 没有 universal shared lock |
| #5 | **resolved:** accepted pagination `100/500` 其他 async timing change |
| #6 | **resolved:** canonical `TOPIC.is_done` + lock/version |
| #7 | **partially resolved:** tenant/composite FK/recheck 关闭;非直接角色策略仍然存在 OPEN |
| #8 | **accepted:** canonical-message-global cross-audience reactions |
| #9 | **resolved:** atomic projection+ready events; mandatory replay |
| #10 | **resolved:** bounded fan-out batches `1000/5000` |
| #11 | **resolved runbook/safety boundary:** backup+migrations+manual scripts; native preserve, Zulip reset/reimport; provider file identity realm+attachment and zero-reference cleanup are fixed |
| #12 | **resolved:** materialized `folder_items_snapshot`, no N+1/json aggregation read path |
| #13 | **resolved:** cross-document consolidation 并且机器检查的质量检测也证实了109个语义.HTTP操作 + 1 WS, 7 个操作工人流, 统一模型/key/task/event/UUID规则和没有 stale/duplicate/orphan artifacts |

## 唯一的 OPEN 解决方案列表 {#единственный-список-open-решений}

1. Non-direct stream role/action matrix: 谁添加用户,哪些 roles
   指定谁更改/删除self/other binding,是否是强制性的 last owner.
2. 具体的运行时间机制 exact-scope lease/claim和 configurable worker
   execution primitive; invariant fencing 已经关闭.
3. Stable worker tie-breaker 在相同的 `MESSAGE.created_at` 和需要
   immutable denormalized sort key 测量后; API tuple已经关闭.
4. 数字 durable-event retention window/release policy;  cursor-too-old已经使用
   显而易见的 `epoch_pruned`/`410`.
5. Target physical table names/schema 对于canonical provider files和 normalized
   message↔file links. Identity 已经被选为 `(realm_uuid,attachment_id)`,
   zero-reference delete 必须;OPEN只有混凝土登陆桌/FK,
   migration mapping current file rows.
6. Capacity/SLO tuning 在加载测量后: worker concurrency,
   fan-out batch 在范围内 `1..5000`, queue admission/backpressure, retention,
   数字 count/bytes limits `folder_items_snapshot` 和完全兼容
   为了应对人口过剩政策.`All chats`建筑 hard boundaries 和
   禁止 silent truncation 已经选择.
7. 稳定的公开发行协会 UUID-only
   `GET`/`PUT`/`DELETE message_reactions/{reaction_uuid}` 在几次
   placements 一个 canonical message: 目前路径不运载 placement UUID,
   因此, migration/model 必须明确选择保存或
   恢复公开`message_uuid`隐藏绑定UUID其他
   arbitrary/primary placement 禁止使用; accepted message-global reaction
   semantics 在此期间,不重新审核.
8. 聚态的物理表现 public
   `ExternalOperation.target_uuid`: target schema 必须选择正规的
   目标的目录或流/topic/message的典型化FK列,没有
   变化当前 JSON `target_uuid`;一个未经检验的多态 FK
   禁止使用.

Target ingestion, service identity, registration boundary 对于
Zulip 没有重复本文库的
[`zulip_bridge/README.md`](zulip_bridge/README.md). 它的 OPEN-list 具体化
transport/provider-key 桥的问题;当前mTLS验证已经选择,
而是共同的 Messenger model/task/event
他们的名字仍然是正规的.Zulip event/op方向和
echo-prevention boundary 规范性规定在
[`zulip_bridge/event_coverage.md`](zulip_bridge/event_coverage.md).

[← 文件的主要索引](index.md) · [域名模型](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)
