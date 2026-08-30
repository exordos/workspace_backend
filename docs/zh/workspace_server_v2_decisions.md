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
- 在同一 frozen migration 中,删除已被证明的 Zulip-imported messages,
  相关的反应/read/event 投影和Zulip文件,没有
  surviving native message reference;
- Zulip-origin reaction 也可以从存储的 native/outbound message:
  它的提供者来源值是 `external_account_uuid`, UUID
  反应在清理旧事件之前 canonical copy. Native reaction
  在同一存储的消息中保持;
- Workspace→Zulip messages 它们被认为是原生,如果它们
  `message.create` user operation 确认本地产地;
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
- 破坏性重置采用失败即关闭策略：一致的 `source_name`、`source.kind`、
  `source.message_id`、account/provider evidence 和持久化的
  `action=message.create` evidence 用于区分入站投影与原生出站数据。任何
  不完整或相互矛盾的来源都会在删除前中止迁移；
- 无人值守的冻结切换最多处理一百万条 legacy messages，等待锁最多 30 秒，
  statement deadline 为 30 分钟。更大的切换必须在完成备份和生产规模演练
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

## 首次实现的兼容性和边界

- 公共路由,回复和WebSocket事件WorkspaceUI没有改变.
- V2 是一个封闭的数据飞行器提供商, browser API.
- Server-owned scope 并且不能作为新字段显示 public
  Messenger resources.
- V1 transport 只是作为滚动适配器保存;
  提供者身份是 v2 contract.
- 完整的 wire-contract 在
    [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml).
