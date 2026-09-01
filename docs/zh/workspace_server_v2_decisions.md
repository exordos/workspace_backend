# Workspace Server v2: 实施的决定

状态: ** 作为一个功能完善的架构和封闭 Provider API v2**.

[← 主指数](index.md) · [Provider API v2](../workspace_provider_api_v2.yaml) · [目标架构 Zulip Bridge](zulip_bridge/README.md)

这份文件记录了 `1B`, `2A`, `3A`, `4A`, `5A` 协议的决定
它们的目标是要实现新的 Workspace 服务器. docs-first
关闭相关的 OPEN-list 项,并不更改
公共浏览器 API 或 JSON 使用现有的 Workspace UI.

## 1B — Provider Data API v2 在现行 private transport

新的入门合同已在一个单独的 mTLS listener:

- `POST /api/workspace-provider/v2/commands` — provider→Workspace commands;
- `POST /api/workspace-provider/v2/operations/actions/lease` — 现行
  排队 Workspace→provider;
- `POST /api/workspace-provider/v2/operation-results` — 关于
  结果 Workspace→provider operation.

V2 转换使用 current certificate identity, heartbeat, body limits,
transaction boundary, batch limit `500`, lease 其他 result semantics v1. V1
在升级过程中仍然可用. credential
protocol 并且新 public/browser route 不会被输入.

Inbound v2 接受提供者身份,而不是计算的桥值
Workspace. `external_account_uuid` 只选择已经指定的连接;
服务器对其进行mTLS身份和所需状态的检查.
不选择 `project_id`, external-chat UUID, stream/topic/user/message UUID,
permissions 交易中的 Workspace 允许 account, realm, chat,
project, stream/topic 只有在此之后,它会调用常规的
基因突变.

## 2A — 一个 realm-global provider chat 属于一个 project {#2a--один-realm-global-provider-chat-принадлежит-одному-project}

对于对 `(provider, verified provider realm, provider_chat_key)` 可能是
只有一个 Workspace 项目被选中.
realm 可以在同一个项目中重用聊天,但选择这个聊天在另一个项目中
project 转换之前被拒绝 desired state.

冲突返回HTTP`409`安全代码
`provider_scope_conflict`. 检查在交易中执行 advisory lock,
而部分的搜索指数限制了检查工作组.
简单而便宜的模式:路由仍然是单一的,而粉丝和公众
投影在 projects.

Upgrade 检查这个变量 reset/copy. Legacy same-realm/same-chat
aliases 项目内部的数据自动缩减,
在多个项目中选择此类聊天停止 migration fail-closed:
选择 project 无需同意就意味着移动或
隐藏内部 Workspace 消息.

在第一个提供者发现之前,当验证领域UUID还不清楚,
选择一个是正常的 `server_url`. URL
lock; 检查后发现 URL 和 realm 锁定稳定.
冲突的对比既知领域 UUID,同样的 provider origin,
所以,我们可以使用一个新的网页流程,
客户的变化,并行选择不绕过规则 2A.

Provider origin 计算为实际的 HTTP origin: 图和 DNS 名
带到下一个注册,结束点 DNS 和标准端口
删除,IPv6保留括号; path 不参与 scope key. DNS aliases
它们最终在发现之后, 归结为验证领域 UUID. trusted
每个帐户的绑定都会为所有已选择的帐户采用相同的 advisory locks chat
这个 account 和 identity 之前的记录,如果没有其他文件,
account 已经在另一个项目中确认了同一个 realm/chat.
realm account 仍然是路由的主人;别名 project,
获取安全代码 `provider_scope_conflict`. 无法检查别名
没有 trusted realm:
独立的 Zulip 领域中可以使用相同的数字聊天ID.
account 通过 alias 不会成为第二个活跃的数据源,而 bridge 可以
在消除冲突选择后再次发布catalog.

在同一个项目 Workspace 中选择同一个 realm-global chat
advisory lock 已经实现的 `projection_stream_uuid` 和
exact `provider_topic_id -> topic_uuid` mappings. Account-scoped external-chat
UUID 剩下的是 control-plane identity assignment: 两个 desired assignment
引用一个流/topic graph,所以重复的帐户导入是不必要的
创建第二个公众投影.
provider routing; 下面的 same-project 帐户是此项目的别名
选择/delete路由所有者以原子方式将路由传递给第一个
剩下的 selected alias 在同一 realm/chat lock 下; 删除常规 alias
只会更改 control plane,而不会删除通用 stream/topic graph.
这些别名的独立背包/live交付汇总为 realm-global
message/reaction UUID. Server 只有当它们一致时, verified realm,
project, projection stream 提供商聊天,保留了第一 materializing
account 作为现有的投影的稳定所有者.

## 3A — realm-global provider identity 其他 direct conversation key

Numeric Zulip objects 它们使用:

```text
UUIDv5(namespace=verified_realm_uuid, name="<type>:<shortest-decimal-id>")
```

允许的 `type`: `user`, `channel`, `message`, `attachment`. Numeric ID —
unsigned shortest base-10 ASCII 没有标志,没有空白, leading zero. Project,
account, server URL, email 和可变显示名不属于identity.

Channel key 有形状 `channel:<shortest-decimal-channel-id>`. Direct/self/
group conversation key 具有精确的序列化:

```text
direct-conversation:v1:<count>:<id1>,<id2>,...
```

列表包含所有参与者的唯一的 provider user ID,并且必须
verified owner 已连接的帐户. ID按数字值排序;
`count` 所以,同一个自动聊天,DM或DM组
一个关键为历史/realtime 和所有帐户相同 realm.

## 4A — 只有已有的授权 public actions outbound

Generic private command «任何 Workspace 模型都禁止.. Provider
API v2 不是证明用户意图的方法,也不提供 Bridge browser/IAM
权力.

Workspace→Zulip operation 只有当前公开版之后才会创建 action,
已经检查了用户,项目范围和权限,
Bridge 只有在第一部分,
启动路径,这些路径在
现行 public API (包括genic message move,mark-unread,typing 和
任意的 role/custom-profile mutations),仍然关闭. Unknown
kind 并且 Workspace identity 替换将被偏离到 mutation.

对于 lifecycle mapped channel/topic来说,下面的精确语义是确定的:

- `stream.delete` 调用官方的 Zulip 档案通道终点.
  桥重复读取频道的当前状态,并计算已
  已存档的频道已达到状态;
- `topic.delete` 调用官方批量删除-主题终点.
  `complete=false` 是可以重新尝试的,而没有主题在预先
  读取的状态被认为是具有能力的;
- `topic.create` 不产生合成的 Zulip message: Zulip 没有单独的
  topic-它们的物体,所以桥就能记住 deterministic
  `<channel-id>:<topic-name>` mapping, 而第一个是普通的. `message.create`
  改变一个字母的名称,
  mapping 也没有创造 provider traffic.

这些功能仅用于频道聊天.
provider reads 只有在极少数的破坏性行动中,
添加一个实时/history的持续加载到导入器.

## 5A — state-based provider event key 单独的 delivery identity

`provider_event_key` 描述所需的逻辑提供者状态.
对于历史和实时,不依赖于 account, project, queue event ID,
局部序列或 Bridge database.

在计算关键之前,Bridge 打造 JSON object:

```json
{
  "provider_chat_key": "<exact chat key>",
  "provider_object": {"kind": "<kind>", "id": "<provider object id>"},
  "provider_references": {},
  "payload": {}
}
```

JSON 编码为UTF-8 词典学序列的关键, separators
`,`/`:`, 没有额外的空白空间,并且有`ensure_ascii=false`. payload
在正常化之前,删除server-owned Workspace ID, transport-only
metadata: `account_uuid`, `chat_key`, `delivery_class`, `external_id`,
`provider_event_uuid`. Digest — lowercase hexadecimal SHA-256 这些 exact bytes.

Wire key:

```text
provider-event:v1:<command-kind>:<object-kind>:<object-id-utf8-byte-length>:<object-id>:<sha256>
```

`provider_sequence` 仅传输本版本的提供者;
producer sequence 没有提供者修订,
这就是 `null`.

单独的 canonical UUID string `delivery_uuid` 在 transport retry
只有一个可持续的交付,但不是语义身份. Workspace 输出
内部账本UUID如何:

```text
UUIDv5(verified_realm_uuid,
       "provider-delivery:v2:<provider_event_key>:<delivery_uuid>")
```

因此,同一次重复一次交付被重复,而新的交付则是
同样,semantic state 重复进入域名交易,并与当前的交易进行比较.
已经达到的状态,就没有任何不必要的操作. public event;
顺序 `add → remove → add` 应用第二个 `add`,虽然两者 add
有相同的 `provider_event_key`.

## 移动数据native preserve 和自动 Zulip reimport

决定后的澄清 `1B`–`5A`:

- versioned migration 能忍受一切 authoritative native streams, topics,
  messages, user state, reactions, folders 和 files 在 canonical v2 中没有变化
  公共浏览器合同;
- 没有收件人-仅仅 UUID 的历史广播快照
  转换为虚拟的 project users: migration 保存自动 native
  事件,但不会为已删除的事件创建 canonical membership/guard IAM
  用户;
- `0157` 使用容器边界：只要消息位于同时满足 `source_name=zulip` 与
  `source.kind=zulip` 的 canonical stream 中，就会被删除，而不再按消息本身的
  来源分类。因此，该 stream 中的 Workspace→Zulip outbound messages 也会被
  删除，随后由常规 Zulip backfill 重新导入。`0158` 会完成重置：即使消息被
  投影到 native Direct container，只要具有同样明确的 Zulip provenance，也会
  被删除；同一 container 中 native-origin 的消息仍会保留；
- 同一事务还会删除相关 reaction/read/event projections 与无剩余引用的 Zulip
  files，并在提交前刷新 legacy compact statistics 以及 canonical v2 的
  stream/topic/folder counters，数据来源是保留下来的 messages。因此，混合的
  native container 会保留 roles、membership generations、notification modes、
  topic state 和 folder placement，同时精确重建 unread、active/passive 与
  last-message 值。迁移会先刷新受影响 stream 中每个 topic 的 compact
  message/read statistics；随后，每个用户的 canonical `read_at` 会与权威的
  compact bitmap 对齐（非 compact/rollback 模式则使用 legacy read flag），
  最后才发布 counters；
- 旧的 `link_kind=provider_identity`,由 account-scoped 实现创建,
  它们的重写是`UUIDv5(verified_realm_uuid, "user:<id>")`.
  surviving native relational references, event payloads, chat catalog 其他
  current/pending desired resources 在同一交易中交换;
  `verified_account_owner` 仍然被绑定到IAMUUID,并且没有参与其中
  提供者身份和 IAM 拥有者之间的冲突停止
  migration fail-closed 隐式用户联合会;
- selected external accounts/chats, credentials 其他 project assignment
  对于旧的 account-scoped 格式, same-realm/same-chat
  stream/topic aliases 原子化为一个 graph: membership, folders,
  drafts, files, native messages, user topic state 事件转移到
  后面只删除不必要的容器.
  Account 得到一个单调的
  `projection_reset_generation`, account/chat desired generations 它们正在上升.,
  状态返回 `backfill`/`syncing`;
- Bridge 存储了最近使用的重置代.
  原子删除仅可重建Zulip缓存/idempotency/mappings,留下
  identity 取消已完成的后填工作,并启动全
  输入一次. 尝试同一个代号不会再次丢弃任何东西;
- 处理删除的 Zulip 文件的物理内容 bounded durable
  worker queue 在 DB commit 之后. 在再次删除共享对象之前
  检查是否保留DB引用; retry 进行. Worker
  记录了两个文件存储配置域,所以 local 和 S3 cleanup
  它们使用与 Messenger API;
- 在删除native stream membership时,将此项旧的 broadcast audience rows
  membership generation 实际上,我们需要一个用户来回复.
  没有重现之前的事件, rolling view rebuild;
- logical desired-state snapshot 不完全在Python中编译和存储
  一个人JSONB它们被置在一个有序的阵列中.PostgreSQL列的
  cascade lifetime 从快照标志; 页面阅读选择 `limit + 1` rows.
  这保持了一致的,并限制RSS控制API独立
  总数和大小 participant/topic catalogs;
- 在读取快照和结之前,rows服务器需要 PostgreSQL
  `SHARE ROW EXCLUSIVE` lock 快照已经开始了.
  append transactions 并且在短时间内不允许新 sequence,
  因此,并发upsert/delete 必须进入 frozen rows,
  快照只在bootstrap/reset时创建,而
  不是实时循环;全球暂停 control-plane writers 更简单,
  总的附加 commit-order 基础设施比较便宜;
- 破坏性重置对 container 与 message metadata 都采用 fail-closed 策略：
  `source_name` 与 `source.kind` 只要部分缺失或相互矛盾，就会在删除前中止。
  完整边界是已确认的 Zulip containers 与已确认 Zulip-origin messages 的并集，
  其中包括 legacy-only compatibility rows，以及通过 message 或 placement 关联的
  canonical rows；
- 无人值守的冻结切换最多处理一百万条 legacy messages，等待锁最多 30 秒，
  statement deadline 为 45 分钟。更大的切换必须在完成备份和生产规模演练
  后由操作员明确授权；五千万消息是重新导入后的稳态目标，并不允许自动转换
  legacy 数据；
- control-plane snapshot 规模门禁至少使用 15,000 个包含大型
  participant/topic catalogs 的 assignments，测量有界 backend RSS，并且
  只读取有界分页；
- 强制性的规模门使用至少 `100 000` 的旧 provider message
  mappings 证明重置完成,完成了重装工作
  变为 `pending`,而旧的改并没有抑制新进口.

Rollback schema 没有恢复故意摧毁的 Zulip projection:
通过验证的前迁移备份.
升级和升级都可用 schema downgrade.

## 不可变切换与身份前向修复

Workspace Server `1.0.0` 已发布的迁移 `0152` 保持不可变。新的准备分支
`0155` 从 `0151` 开始，join head `0156` 会先列出该分支，再列出正常的
`0152` → `0154` 链。因此，全新升级会先准备来源证据，再执行已发布的
cutover；已经记录 `0152` 的安装会跳过准备工作，并由 `0156` 前向修复。
由于 `pg_dump` 不保留 planner statistics，全新路径还会在执行不可变的
set-based statements 之前，对所有冻结的 cutover inputs 运行 `ANALYZE`。

只有存在精确匹配且成功的 `message.create` operation 时，准备阶段才把历史
outbound echo 视为原生数据。`source.message_id` 可以缺失，但不能与 provider
ID 冲突。在 durable operation queue 出现之前创建的一致 native rows 会获得
短期 `discarded` 来源标记；这些标记不会进入 provider queue，并由 join head
删除。

`0152` 之后首个已发布的 Bridge payload 未写入 `source.message_id`，但仍携带
完整且一致的旧版证据：`source.kind=zulip`、数字
`provider_external_id`、provider metadata 中相同的 ID、原始 provider URL，
以及不冲突的 realm。`0156` 在前向修复时只接受这一完整旧版形态。唯一行会
获得 realm-global identity；如果已经存在带 global key 的导入行，则仅解除
已证明 account-alias 副本的 provider linkage。任何不完整或矛盾的变体仍会
原子中止。Rolling legacy triggers 在该已发布 Bridge 退役前使用相同的兼容
规则。

`0156` 为保留消息补充 realm-global provider identity，并确保同一条物理
Zulip message 只有一个保留 provider linkage 的赢家。已证明的 account aliases
必须在 realm/message ID、project、author、不同账户、provider URL 和 metadata
identity 上一致。所有内部 messages、placements 和 public UUID 都会保留；
仅从失败 alias 上解除 provider linkage。已有 global identity 的导入行优先于
匹配的 retained alias。任何未经证明的冲突都会原子中止。随后，legacy
insert/update rolling triggers 会继续执行相同规则，直到旧服务器退役。

## 共享 Zulip 投影的所有权与重新导入

每个 Workspace 项目中的 realm-global Zulip 频道只对应一个规范 stream。
因此，多个已选择的账户可以指向同一个 `projection_stream_uuid`，而物理
stream 仍保留首次创建它的所有者。只有当同一项目中存在另一个指向该 stream
的已选择 assignment 时，provider 导入才允许由不同账户所有者写入。若没有
这条持久化的 peer assignment，所有者不匹配仍然是硬错误。

处理 `topic.upsert` 时，服务器根据持久化的 canonical stream 生成类型化
Workspace source，保留其稳定的账户 scope，并补充 topic 名称。Bridge 无需
在每个事件中重复由服务器管理的 source 字段。

迁移 `0154` 会将每个 Zulip 账户的 reset generation 增加一次，并重新发布
已选择的 assignments。这样会丢弃被隔离的部分投递并启动一次完整重试。
Provider key 保持幂等，因此已接受的行会被更新而不是重复创建。全新升级时，
已停止的 Bridge 只会看到最终 generation，并且只执行一次导入。

## 合并旧版已读状态修复产生的文件夹快照

旧版已读状态修复可能为每个被修正的 message flag 各排入一次 folder
projection。这些任务都会重建完整的当前文件夹快照，并不携带历史文件夹状态。
因此，worker 获得 `user-folder` scope 后，已领取的旧版重建任务会吸收同一
scope 中仍在等待的同类任务，并只提交一个权威快照和事件。该事务之后新到达
的任务仍会保留在队列中并触发后续重建，所以 live 收敛保持不变，而迁移工作量
由受影响的文件夹数量决定，不再随 message flag 数量增长。

## 合并仅快照的未读计数任务

批量导入消息、修复消息以及生成成员关系时，可能会为同一个 `user-stream` 或
`user-topic` 作用域创建大量未读计数投影。每个仅快照任务都会重新计算完整且权威的
当前计数，并不携带历史计数值。因此，一个已领取的仅快照任务会在同一事务中吸收该
作用域内空闲的同类任务，并只发布一个当前快照。带有
`emit_message_read=true` 的任务仍保持独立，以确保每次明确的逐消息已读操作都保留自己
的事件。事务结束后到达的新任务仍会排队，从而既保持实时收敛，又把批量工作量限制在
受影响的用户作用域数量内。

## 可修复的原生已读状态与交互优先级

迁移 `0160` 会恢复创建 v2 规范状态时已存在于旧版 flag 或紧凑 bitmap 中的原生消息
已读状态。修复是单调的：它只填充缺失的 `read_at`，不会把切换后已读的消息重新标为
未读，随后会重建原生 stream、topic 和 folder 快照。

即使规范行已经是已读状态，显式的单条消息、范围、topic 和 stream 已读操作仍会排入
一次权威计数重建。因此，幂等重试可以修复陈旧快照。这些投影任务优先于批量导入执行，
而常规快照合并仍会限制数据库负载。

这些已读操作还会在同一规范事务中更新滚动的 compact 兼容状态。迁移
`0166` 会为所有用户修复现有的“规范状态已读但 compact bitmap 未读”记录，重新计算
受影响的 topic 已读统计，并推进每个用户的已读修订号。该修复是单调的：它不会修改
任何规范未读记录，包括来自 provider 快照的未读状态。

随后，迁移 `0167` 会为所有活跃用户根据持久化 bitmap 重新计算每个 compact 或
rollback topic 的已读聚合。即使每条消息的 bitmap 位已经与规范 `read_at` 一致，
该迁移也会修复仍然陈旧的聚合，从而让 stream、topic 和 folder 计数使用同一份已读
事实。

迁移 `0168` 会根据持久化消息行重新计算共享的 compact topic 消息总数和最后的
ingest 坐标。它修复最后一种情况：每个用户的已读计数都已准确，但过期的 topic
消息总数仍会导致兼容 stream、topic 和 folder 未读计数发生偏移。

迁移 `0169` 会重新应用 provider 私聊规范化，并以每个参与者的 provider
`display_name` 作为面向当前用户的单聊和群聊名称来源。只有 provider 未提供名称时
才回退到 Workspace identity。与旧 Workspace identity 投影结果相同的历史名称会被
识别为 provider 管理名称；用户显式设置的本地群聊名称会保持不变。

迁移 `0170` 还会在单聊目录记录只列出对方参与者时，将已选择 provider 聊天的所有者
视为有效查看者。因此，对方的 provider 名称会继续作为每个已关联账户的权威聊天名称，
无需 provider 在参与者列表中重复列出所有者。

## Provider 已读分页顺序

延迟物化的 provider 已读分页使用其源快照的队列位置来确定同一 lane 内的顺序。较新的
快照可能在该分页获得物理 operation sequence 之前就已持久化，但不能阻塞更早的分页。
更早的快照仍会阻挡同一 stream lane 中的后续写入，其他 lane 保持独立。物化数量和
lease batch 大小限制均保持不变。

## Provider 入站恢复

私有 Provider API 命令可安全重放：request、event、lease 和 result 标识符在各次尝试间
保持不变。发生 PostgreSQL 死锁后，服务器会使用短时且有界的指数退避重试完整请求事务；
其他 control 和 file 请求不会被重试。尝试耗尽时返回不包含数据库细节的可重试 `503`。
Provider 删除触发 topic 摘要恢复时，会跳过对应消息已不存在的 journal boundary，避免
陈旧的派生摘要导致整个入站 batch 被拒绝。
在 Provider 删除之前排队的投影任务会把已删除的规范消息视为不存在，因此陈旧的
fanout 或 mention 任务会以 no-op 完成，而不会进入 dead-letter 队列。

## Provider 账户所有者的已读状态一致性

同一 realm 中多个账户共享的 Provider 消息只有一个规范 placement，但每个已选
账户的所有者都拥有独立的 binding 和 state。Compact 导入通过一个有界 SQL
批次创建这些记录，并在同一事务中同步 compact bitmap 与规范 `read_at`。
Snapshot-only 回填仍不发送逐消息公开事件，但会保留权威的 stream 和 topic
计数重建。

迁移 `0162` 使用持久化的已应用 Provider event 日志，只恢复实际投递过的
message/account 组合。有效已读值来自 compact bitmap；非 compact 模式则来自
legacy flag。迁移随后重建受影响的 stream、topic 和 folder 计数，并在提交前
验证一致性，不会扩展 native 消息或未选择的 Provider 历史。

## 首次实现的兼容性和边界

- 公共路由,回复和WebSocket事件WorkspaceUI没有改变.
- V2 是一个封闭的数据飞行器提供商, browser API.
- Server-owned scope 并且不能作为新字段显示 public
  Messenger resources.
- V1 transport 只是作为滚动适配器保存;
  提供者身份是 v2 contract.
- 完整的 wire-contract 在
    [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml).
