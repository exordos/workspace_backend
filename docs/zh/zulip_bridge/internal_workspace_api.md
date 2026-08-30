# 总内 Workspace API 为 Zulip Bridge

状态: **proposal;第一个提供数据 API v2 wire-部分已固定**.

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [事件矩阵](event_coverage.md) · [建筑概述](architecture_overview.md)

两个桥进程都调用一个内部的常规 Workspace API.
这是一个私有服务到服务的边界,
RestAlchemy transaction rules, 它们可以创建目标的正规实体.
不是新的公共客户端API,不允许 Bridge 直接访问
图表.

现有的封闭提供商 API 描述在
[`workspace_provider_api_v1.yaml`](../../workspace_provider_api_v1.yaml), 而他的
control/file security profile — 在
[`zulip_bridge_control_api_v1.yaml`](../../zulip_bridge_control_api_v1.yaml) 其他
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml). Target
必须再利用已经实现的 realm-bound mTLS authentication.
第一个实现使用了从
[`workspace_provider_api_v2.yaml`](../../workspace_provider_api_v2.yaml), 没有
解决方案 scope/identity/idempotency 已在
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).
没有设计替代身份验证机制.

## 现行 S2S 认证  强制目标边界

Zulip Bridge 使用现有的单独的 private process/listener
`workspace-external-bridge-api`, 不是公共的Workspace nginx和不 browser IAM
token. TLS 1.2+ 在后端进程中完成; 常规查询必须提交
client certificate, 签署的 realm control CA. HTTP forwarding header,
bearer token 或是body字段不是源 service identity.

Certificate 包含一个 URI SAN 在当前格式:

```text
https://schemas.genesis-corporation.ru/workspace/external-bridge/v1/realms/{realm_uuid}/providers/{provider_kind}/instances/{bridge_instance_uuid}/generations/{identity_generation}
```

Workspace 他从中得到了 `realm_uuid`, `provider_kind`,
`bridge_instance_uuid` 并且是正的 `identity_generation`,检查 current
certificate fingerprint, active generation 在每个 request,
包含reused TLS连接.
project: server-side desired assignments 交易时间检查将其缩小到
允许的 external account/chat/project.

Lifecycle 没有新品,可以再使用 credential protocol:

1. Platform 发出一个单独的一次性入学密码 Bridge installation
   通过安全的Core管理配置生成.
   verifier; 标志值不是常数 service credential.
2. Bridge 通过现有域获得CA HMAC-authenticated bootstrap,
   生成本地私钥,并将 CSR 发送到 `/v1/enrollments`
   `X-Workspace-Enrollment-Token`. 成功发放原子关闭 generation;
   复制相同的 `request_uuid` 和 CSR 变量,改变的重复被拒绝.
3. Client leaf 生命时间为 `30 days`,更新时间为 `7 days` 开始,
   通过有效的mTLS证书进行认证.新密钥/CSR创建
   在桥上;旧和新 leaf 允许同时不超过 `24 hours`.
4. Suspend 要求立即被禁止. identity generation;
   certificate 旧的 generation 已不再接受. 损失/expiry需要
   operator-controlled enrollment-secret rotation, 没有 shared long-lived token.

Private key 只有 persistent Bridge disk. Backend PKI/enrollment
state 存储在root-owned mode-`0700`专用存储器中, sensitive
files 写为:mod `0600`;raw enrollment token,verifier,client private key 和
credential payload 禁止在 logs/errors. Account lease/fencing generation
仍然是一个独立的 mutable authorization/ownership check:有效 mTLS
certificate 没有 active matching account assignment/lease 不允许 command.

Failure boundary 已定义:证书,拒绝了 TLS 堆,可能没有
获取 HTTP 响应; 缺失/不当的应用程序身份返回
`401`; current instance state 或assignment禁止通过 `403`;
invalid cross-scope command 没有添加到这个提案.
在公共中建立新的auth error shape Workspace API.

由于它已经提供了相同的长寿,
External Bridge process 这三个人 current private resource groups: control,
Provider data 公共 IAM 载体是指 user/browser request;
一次性 enrollment header 只会输出第一个 certificate; HPKE credential
envelope 并且单个对象文件能力保护 payload/object,但不是
它们不是替代品. mTLS.

## Service identity 其他 server-owned scope

在mTLS认证后, Workspace 得到不可变的 service context:

- certificate-bound `realm_uuid`, `bridge_instance_uuid`, provider kind `zulip`
  其他 `identity_generation`;
- 经过单独检查 whole-account lease/fencing generation;
- 允许的 external account/assignment generations;
- realm/project mapping, 保护他们的. Workspace;
- 允许的集合 logical commands;
- 现行提供商政策,暂停/revocation和 capability set.

Bridge 传递提供者对象/event身份和收费,但不是 authoritative
`project_id`, `source`, Workspace `user_uuid`, 如果是这样的话,
需要一个线封面的字段, Workspace 将它们与 server-owned
mapping 并且拒绝不一致;客户端的值从来没有确定
tenant 或作者.

对于每个命令 Workspace 内部 request 交易重新检查:

1. mTLS service identity active, certificate/identity generation 相关问题,
   instance 没有 suspended/revoked;
2. external account 已经被任命为 bridge/provider, active lease generation
   符合 provider policy 的要求 operation;
3. provider object 属于一个被允许的 account/chat scope;
4. server-owned project/stream/topic/user mappings 存在并具有相同的
   tenant identity;
5. mutation 允许 capability 并不交叉 project boundary.

Composite tenant FK 并且 `UNIQUE(project_id, ...)` 仍然是最后一个物理
服务预飞不会取代 transaction-time authorization.

## 两个稳定的身份

`provider_object_key` 并且 `provider_event_key` 解决不同的问题.

| Key | 职位 | 强制性属性 |
| --- | --- | --- |
| `provider_object_key` | 在创建/update/delete和重启后找到一个logical entity Zulip | 对于实时/history相同,并且在 fresh import |
| `provider_event_key` | 复制一个提供者突变/delivery并输出一个 immutable outbox event | 一个源事件/version只给出一个密钥, retry 不会改变它 |

语义组合 identity:

| Kind | Provider object identity |
| --- | --- |
| user | verified realm UUID + typed `provider_user_id` |
| stream/chat | verified realm UUID + typed channel/conversation identity |
| topic | Workspace-owned durable mapping `(realm,channel,current name/alias history)` → stable canonical `TOPIC.uuid` |
| message | verified realm UUID + typed numeric `provider_message_id`; importing account 没有 canonical identity |
| reaction | canonical provider message identity + actor provider user identity + exact `emoji_name` |
| membership | provider stream/chat identity + provider user identity |
| file/attachment | `(verified realm UUID, typed attachment_id)`; canonical file 一个,正常的消息↔文件链接是单独的 |

对于双向命令,封面还包含`origin`和
`causation_uuid`/Workspace provider operation UUID. Outbound Workspace
operation 首先 durable 将 causation 与 provider object/version 联系起来,而
返回的 Zulip 事件验证了这个操作,而没有产生新的
如果提供商没有返回客户端 UUID,服务器使用
durable operation receipt + provider object key + version/state; timestamp 没有
它们的确是回声的证据. direction/source-of-truth matrix:
[`event_coverage.md`](event_coverage.md).

Numeric provider UUIDv5 使用 exact algorithm:
`UUIDv5(namespace=verified_realm_uuid,
name="<entity_type>:<decimal_provider_id>")`. 允许的 lowercase ASCII
types: `user`, `channel`, `message`, `attachment`. Provider ID 序列化为
unsigned shortest base-10 ASCII (`0` 或是没有数字 leading zeros, sign,
whitespace/locale formatting); name bytes — exact ASCII/UTF-8 没有 NUL/BOM/
newline/additional fields. Project/account UUID 没有 namespace. Exact
keys 对于 events/direct conversations,它们由 `3A/5A` 的解决方案定义
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

旧的 Workspace UUID 之前的进口不包含在钥匙中. fresh import
创建一个新的 canonical 行,并在此进口中重复相同的操作
通过提供商映射来返回/更新. message create Workspace
给自己指定 internal `MESSAGE.uuid` 并确定得到 public
placement UUID 根据 canonical topic/message.

## 逻辑命令目录

下面的名称描述了语义命令类型,而不是说 HTTP route names.

| Logical command | Primary write Workspace | Idempotency/object rule |
| --- | --- | --- |
| `identity.claim` / `user.ensure_external` | Verified account claim existing identity 或是 create/reuse unmanaged external user; email 只有候选人,没有 proof | realm+user ID; conflicting verified owner fail-closed |
| `user.mapping.refresh` / `user.lifecycle.update` | Existing managed/unmanaged ordinary-user mapping: supported name/avatar/role/custom value/active state; email 已删除 | provider user key + field/version/event key |
| `bot.create` / `bot.deactivate` | Special Workspace bot/external user; 只有 Zulip-origin lifecycle | provider bot user key + event key/version; metadata update unsupported |
| `stream.create_from_provider` | Canonical `STREAM` + provider mapping 只有从 Zulip `stream/create` | provider channel key + event key; native Workspace stream create 不调用此命令 |
| `stream.update` / `stream.delete` | 将 mapped provider change 传递到 Workspace 域服务;它选择 archive/history/bindings/visibility 并写 outbox | provider chat key + event key/version; Bridge 没有使用 policy |
| `topic.resolve` / `topic.rename` | Workspace-owned durable mapping 通过 alias history; 强制性 `TOPIC` 作为 immutable stream/project owner | realm+channel+current/old topic name; whole rename 保存 UUID |
| `membership.upsert` / `membership.revoke` | 传输会员身份事实; Workspace 通过流设置改变 persistent binding/generation, historical visibility 和 message bindings | provider stream+user key + event key/version; composition change 没有创建 stream |
| `message.create` | `MESSAGE` + `MESSAGE_PLACEMENT` + author binding/state + outbox | provider message key + create event key |
| `message.update` | canonical content/source/provider/delivery version + outbox | same provider message key + update event key/version |
| `message.move` | Resolve one canonical `MESSAGE`; delete source placement and create target topic placement | provider message/version + target topic + event key; target placement has new UUIDv5, old URL `404` |
| `message.delete` | provider tombstone/current delete semantics + outbox | same provider message key + delete event key/version |
| `message_flag.update` | Placement-scoped `USER_MESSAGE_STATE.read_at`/`starred` | provider message+user+flag+op+event key |
| `reaction.upsert` / `reaction.delete` | one canonical-message-global reaction fact + outbox | message+actor+`emoji_name` + event key |
| `file.allocate` / `file.finalize` | bounded single-object lifecycle and canonical file metadata | realm+typed `attachment_id`; repeated accounts/retries reuse row |
| `attachment.upsert` / `attachment.delete` | normalized message↔file relation + outbox | message provider key + realm/attachment key |
| `presence.publish` / `typing.publish` | Ephemeral scoped relay with access check and TTL; no canonical message write | origin+user+scope/state+short-lived causation key |
| `user_status.update` | Persistent mapped `status_text`/emoji state + outbox | provider user+status version/event key |
| `account.lease.*` / `account.bootstrap.*` | Whole-account lease/fencing, queue boundary 其他 bootstrap generation | account UUID + monotonic generation |
| `history.root.*` / `history.stream_task.*` | Root discovery 其他 immutable per-stream range task lifecycle | account+boundary+selection/range+stream; no message checkpoint v1 |

命令无法打开 generic operation 记录任何模型». Unknown kind,
unmapped tenant, stale service generation, unsupported capability 或是试图
设置 project/user 导致拒绝 mutation.

目录中的名称是道理提案类型,而不是公开路径.
不经过验证的email claim-to be managed user, 不允许 Workspace stream
create 创建Zulip频道,并不将 unsupported event转换为 generic
upsert. Import 只有未管理的外部用户可以创建 session.

Bridge 在 Workspace 之前,不计算域政策 command: group/private member
change 和 channel archive/delete 传输的确切是 provider facts.
Workspace transaction 历史访问,绑定和 visibility.

## 输出边界 provider operations

对于Workspace-起源的突变 bidirectional coverage primary transaction
增加一个不可变的Outbox事件.
没有损失从中导出 durable provider operation unique source outbox
event UUID, server-owned account/object mapping, `origin=workspace`,
`causation_uuid` 并且预期版本/state. Realtime Connector 得到
operation 通过这个边界,调用 Zulip 并返回 durable
receipt/confirmation. Exact queue/HTTP transport, derivation mechanism 其他 ack
schema 剩下OPEN#1;应用程序没有发布用户代币,也没有使用
public WebSocket event 如何 transport.

Direction guard 是服务器:例如, native Workspace stream create
没有创建出bound通道操作.Zulip允许排队回响
通过 receipt/object/version 完成 causation,但不会重复作为
提供者调用重复保持相同的 operation identity.

## Transaction boundary 信息

`message.create` 原子式执行:

1. Lock/dedupe realm-scoped `provider_object_key` 和 `provider_event_key` 在
   active account lease generation.
2. 如果事件已经 committed,返回相同的语义结果,没有新的突变.
3. 允许 server-owned author/stream/topic/project mappings.
4. 创建或恢复一个 canonical `MESSAGE` provider key.
5. 创建一个强制性 `MESSAGE_PLACEMENT`; authoritative uniqueness —
   `(project_id,message_uuid,stream_uuid,topic_uuid)`.
6. 获得公共放置 UUID 作为
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`.
7. 创建 author `USER_MESSAGE_BINDING` 和 `USER_MESSAGE_STATE` 于当前
   membership generation.
8. 在同一页面上写 immutable outbox event 和 committed idempotency receipt DB
   transaction.
9. Commit 或将所有行一起翻滚.

Bridge 没有一个受理者会期待.Workspace工人将 bindings/states
通过共享的快照/counters和可持续的准备事件 one-event →
one-task protocol. 详细的 canonical task types 在
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#task_kinds-и-routing).

## Update/delete ordering

对于一个提供者对象Workspace进行比较 provider version/sequence,
如果来源提供:

- 同一个版本的重复, payload — idempotent success;
- 旧版本  stale no-op 保存新状态;
- 新版本  一个突变+一个 outbox event;
- 与冲突 payload/version  终端冲突相同的 identity
  DLQ/reconciliation, 而不是 silent overwrite.

Update/delete, 之前的 create 从 overlap/newest-first range, 不会创建
synthetic `MESSAGE`. Workspace 保存持续延期依赖性或
返回可检索的缺失基数结果. outcome
剩下的 OPEN,但 durable 依赖性属于 Workspace, local Bridge DB.

## 反应

公众的action 解决位置 UUID 访问检查,但导入命令
通过查找 canonical message provider message mapping. Source of truth — raw
fact 有钥匙
`(project_id,canonical_message_uuid,user_uuid,emoji_name)`. Realtime/history
retry 只有这个事实才会改变.Workspace工人物质化
`reactions`/`reaction_users` 在所有位置;桥画片不写.

## Files 其他 attachments

Bridge 没有获得桶范围内的凭证,也没有写存储元数据.
authorization Workspace 提供单对象转移能力,检查
size/hash 并且记录了 finalize/attachment关系.
边界位于
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml).

Target 必须保留属性:

- 在一个 bounded object 上 allocation;
- finalize 和附加链接是有效的;
- bytes commit 在此之前不让元数据可见 Workspace transaction;
- retry 不会产生第二个. blob/row/link;
- delete 在此时不删除 physical object retained native reference;
- provider identity `(realm_uuid,attachment_id)` 另一个将重复使用 file;
- physical object 只有在 zero native/provider references.

## 语义结果和错误

Wire statuses 结果可能会有所不同.:

| Outcome | 意思 | 活动 Bridge |
| --- | --- | --- |
| applied | Primary mutation 其他 outbox committed | Realtime 接收事件终端;历史继续 current task |
| duplicate/no-op | 同一个提供者事件/state已经 committed | Terminal 没有重复 outbox/ready event |
| stale | 已记录了更新的provider state | Terminal no-op + metric |
| deferred | Missing mapping/base dependency durable 在 Workspace | Terminal 对于源单元; repair 后的依赖 |
| retryable | Timeout/rate limit/temporary unavailable, commit 没有证明 | 重复相同的键;实时无法读取 next event |
| permanent/terminal | Provider rejection 或 invalid scope/conflicting identity | `permanent_failed`/DLQ evidence; endless retry/silent skip 禁止使用 |

如果答案在 commit 之后丢失,重复相同的 event key 必须证明
commit 并且返回duplicate/same result. 生成一个新的 retry
禁止使用.

## Audit 其他 privacy

记录和痕迹包含 certificate-bound bridge instance/generation, provider
kind, account/mapping UUID, object/event key digest, outcome 并且延迟,但没有
enrollment token/verifier, certificate private key, user token, API key, raw
credential 没有任何私人载荷.Workspace审计仍然存在 tenant-scoped.

Provider mappings 并且最新的隐藏的原始/converter元数据与 entity.
Completed history tasks 通过
`30 days`, permanent-failure operation/code/reason — 通过`90 days`这就是
internal retention 没有新的 public fields/actions.

没有封闭的 wire routes/transport和 provider-key serialization 细节仅在
[根据](README.md#единый-список-open-решений-zulip-bridge).

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [事件矩阵](event_coverage.md) · [建筑概述](architecture_overview.md)
