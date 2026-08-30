# Coordination, bootstrap 其他 recovery

状态: **proposal; 必须的语义,运输/runtime details部分 OPEN**.

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

文件取代了以前的图表 durable old-queue cursor catch-up, message-level
history checkpoint 持续协调生活在 Workspace;
Bridge local state — 抛弃的 cache.

## Authentication 在 coordination

每个私人控制/Provider/file请求首先通过当前的
realm-bound mTLS authentication `workspace-external-bridge-api`: TLS client
certificate 定义 `realm_uuid`, `provider_kind`, `bridge_instance_uuid` 和
`identity_generation`; current backend state 每次检查一次
request. 一次性 enrollment,证书更新/revoke和秘密存储不
根据本条的规定, proposal.

只有在 Workspace 验证之后 whole-account assignment,
lease/fencing generation 和 project/chat租可以回答一个问题:
instance 现在拥有 account,但不验证进程. stale
lease 如果证书有效,则会有"拒绝授权",而新租不
允许未经认证的请求.

## Whole-account lease 其他 fencing

Workspace 提供一个桥实例租,
monotonic fencing generation. Account 没有分为 stream,
topic 只有在此时,私人API才会接受/task/receipt突变
积极的租和相应的 generation.

V1 允许一个桥实例,但任务模型一次 multi-instance:

1. Workspace 只有 healthy compatible instances.
2. 新帐户获得最小的instance normalized load
   `active_accounts / declared_capacity`; tie-breaker 必须是稳定的..
3. Assignment sticky: 新的instance出现时无法实现 rebalance healthy
   accounts 自动.
4. 移动新帐户和账户的 owner dead/draining.
5. Realtime 和一个帐户的历史总是在一个 owner Bridge.

- Heartbeat 每次 `10s` 发送;instance 之后成为 `degraded`
  `30s` 没有心跳和`offline`之后 `60s`.
- Graceful shutdown/draining 显然停止新的索赔,并释放 leases.
- 在 `60s` 离线时刻结束后,新实例要求整个帐户,获得新的
  fencing generation 并且启动了同样的 bootstrap.
- Stale owner 无法提交提供者收件,任务结果或 cursor advance.
- Disconnect/delete 取消 generation; work 不会迁移到其他 account.
- Durable account/tasks/mappings/outbound errors 留下来 Workspace-owned.

## 唯一的启动链接,重新连接和 recovery {#единый-bootstrap-connect-reconnect-и-recovery}

一个算法是使用后 connect, reconnect, lease takeover, queue
expiry, missing heartbeat, `restart` 其他 `web_reload_client`:

1. 通过当前mTLS身份检查,然后检查 active account, verified
   credential 其他 whole-account lease.
2. 仅为此记录新的 Zulip 事件队列 supported event types.
3. 获得一个足够的登记边界 snapshot/history split.
4. 如果注册失败,请重复 backoff; root 历史不会创建.
5. 从新一个开始 sequential realtime consumption boundary.
6. 能够创建一个 Workspace 历史根任务 account selection,
   `history_depth`, boundary 其他 lease generation.

旧队列ID/cursor不需要 durable recovery. 新的边界不需要
创建 gap: history 包含 selected snapshot/range 到 boundary, realtime
— events 包含/exclusive线路表示 Zulip
registration response 现在还有一些私人交通细节,
证明两个相邻范围的覆盖范围.
provider object/event keys.

## Realtime terminal acceptance

Connector per account 在工作中保持不超过一个inbound supported event:

1. 获取 next event.
2. 直接与一个私人 Workspace 命令进行匹配,或者 lifecycle signal.
3. 在/key时重复同一个 command transient/ambiguous failure.
4. 在 applied/duplicate/stale/confirmed 或 classified permanent failure
   计算 event terminal.
5. 只有在 terminal acceptance 之后才可以转到 next event.

这意味着 durable reuse 旧队列后的 loss: queue recovery 再次
提供商密钥使得重播/overlap安全.

## History task model 没有 message checkpoint

Workspace 存储 immutable/root任务和per-stream子任务.
selected chats, discovers users/streams/topics/memberships 创造了 child task
通过每条频道/direct/group-direct流. immutable input:
account, stream, history range, boundary 其他 provider task identity.

如果 child 跌到 terminal completion,
下一个要求重复整个selected stream range,
使用的object快速返回 provider keys 的 duplicate/no-op.
completed stream tasks 任务的常规函数是 `pending` →
`leased/running` → `completed`/`failed` transitions, attempts/backoff, lease
expiry, fencing, bounded retry 其他 DLQ/reconciliation evidence.

一个帐户的不同流任务可以同时执行在同一个帐户上
Bridge 通过一个共享的可配置池,默认 `4`;正确的最大/optimum
在 load test 之前, history worker.
Topics/messages 它们在流中连续流动,因为 Zulip topic —
消息属性; 消息 `created_at DESC` stable provider-message
tie-breaker. 在账户之间,调度器使用公平的轮回,
account — last activity/newest stream first.

所有的历史工作者在一个帐户中共享账户级Zulip的速度限制器.
`Retry-After` history 这个 account 在 provider interval.
Realtime lane 独立,优先,并首先恢复;历史没有
能花费所需的预算 realtime.

## Retry 其他 permanent classification

| Outcome | 活动 |
| --- | --- |
| transient transport/`429`/temporary unavailable | Backoff+jitter, 这样 provider/operation/task key; no advance |
| applied / duplicate / stale | Terminal success; no repeated outbox/event for no-op |
| missing older dependency | Durable deferred reference; current event/task 证明保存后可以结束 dependency |
| invalid/cross-scope/conflicting verified owner | Fail-closed, permanent evidence/admin resolution |
| internal outbound `permanent_failed` | 停止 endless retry; safe code/reason private, current public delivery shape unchanged |
| unsupported family | 没有签名;未预期的事件审计,没有 guessed mutation |

Completed history tasks 通过
`30 days`; permanent-failure operation/code/reason — 通过 `90 days`. Future
manual requeue 现在,我们只需要一个内部扩展.
对于外部操作,不取代内部分类,也不扩展
这里 proposal.

## Deferred references 其他 reconciliation

Newest-first history 可以看到比率/file/older之前的消息引用
mapping. Workspace 保存内部延期引用,
mapping 我们可以修复它. canonical Markdown/URN/mentions. Actual change
写出box和ready event; no-op 不会写 event.

Reconciliation 检查:

- active account lease/generation 没有 stale commits;
- history root/child coverage, failed/DLQ tasks 其他 selected range totals;
- provider-key uniqueness, gaps/duplicates 其他 multi-account union references;
- topic alias mappings, file attachment links, unresolved references;
- pending/retryable/permanent outbound operations;
- projection/outbox/task/ready-event consistency 在 Workspace.

## Backpressure 其他 graceful restart

Realtime intake 没有替换历史吞吐量: realtime 始终支持
在之前. History default pool `4`, upper limit/rate/batch limits
bounded/configurable. Fair round-robin 不允许一个帐户断
pool. 在 graceful
stop Bridge 停止新的 claims/provider 呼叫, 完成或释放
current unit, conditional 只输入terminal result,并显然返回 leases.
在硬崩盘接收时,仅允许在 `60s` 离线时间out之后;新 owner
随着新一代的重复bootstrap和 unfinished stream task range.

监视包括 account generation/lease age, queue registration
failures, realtime event age, history root/stream lag, restarts/full-range
replays, duplicate/no-op ratio, deferred/DLQ age, outbound retry/permanent
failure 和单独 Workspace projection/WebSocket lag. Content, `api_key`, raw
payload 个人识别器不包括 labels/errors.

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
