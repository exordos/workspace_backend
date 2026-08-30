# 目标架构 Zulip Bridge

状态: **proposal;第一个服务器/Provider API v2部分单独记录**.

[← 文件的主要索引](../index.md) · [规范库存 Messenger](../messenger_architecture_inventory.md) · [现行边界 Zulip v1](../zulip_bridge_v1_product_and_api.md)

Wire transport, project scope, direct identity, outbound authorization 其他
provider event key 首次实施的关闭的决定`1B/2A/3A/4A/5A`
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

这个目录描述了目标的双进程同步架构
Workspace↔Zulip 历史的原始进口.
公共路线,JSON或封闭合同.
合同仍然在
[`workspace_api.md`](../workspace_api.md), 而现有 provider/control/file
边界 [`zulip_bridge_v1_product_and_api.md`](../zulip_bridge_v1_product_and_api.md)
并且与其相关的 OpenAPI 文件.

## 文件

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`architecture_overview.md`](architecture_overview.md)  | **proposal** | 构件,信任界限,数据流动以及桥梁与 Workspace. |
|  [`event_coverage.md`](event_coverage.md)  | **proposal; 接受的覆盖** | 正确的Zulip事件/operations,同步方向,Workspace行动,真理来源和正确的Workspace事件/operations的正规矩阵 echo prevention. |
|  [`realtime_connector.md`](realtime_connector.md)  | **proposal** | 常数 `Zulip Realtime Connector`: 接收事件,顺序,重试,反压,以及 graceful restart. |
|  [`history_importer.md`](history_importer.md)  | **proposal** | 最终 `Zulip History Importer`: fair pool default `4`, per-stream newest-first work, account limiter, restart/dependencies/reconciliation. |
|  [`internal_workspace_api.md`](internal_workspace_api.md)  | **proposal** | 共同的内部 Workspace API,有限的服务身份,交易边界和单一的进量. |
|  [`coordination_and_recovery.md`](coordination_and_recovery.md)  | **proposal** | 单一的bootstrap,账户租/fencing,边界,重试/DLQ,调整和恢复. |
|  [`account_lifecycle_and_identity.md`](account_lifecycle_and_identity.md)  | **proposal; current routes preserved** | Connect/reconnect/disconnect/delete, verified identity claim, unmanaged users 其他 multi-account canonical union. |
|  [`provider_mappings_and_content.md`](provider_mappings_and_content.md)  | **proposal** | Realm-scoped provider keys, durable topic/file mappings, canonical Markdown/URN, deferred references 其他 manual reconversion. |
|  [`delivery_and_events.md`](delivery_and_events.md)  | **proposal** | Durable Workspace→Zulip operations, conflict/permanent-failure semantics 其他 exactly-one ready event per actual transition. |

## 图表

| 剧本 | PlantUML | SVG |
| --- | --- | --- |
| Realtime synchronization 其他 echo prevention |  [`realtime_connector.puml`](diagrams/realtime_connector.puml)  |  [`realtime_connector.svg`](diagrams/realtime_connector.svg)  |
| History import |  [`history_importer.puml`](diagrams/history_importer.puml)  |  [`history_importer.svg`](diagrams/history_importer.svg)  |
| 首次进口和转向 realtime-only |  [`bootstrap_to_realtime.puml`](diagrams/bootstrap_to_realtime.puml)  |  [`bootstrap_to_realtime.svg`](diagrams/bootstrap_to_realtime.svg)  |
| Verified claim unmanaged identity |  [`identity_claim.puml`](diagrams/identity_claim.puml)  |  [`identity_claim.svg`](diagrams/identity_claim.svg)  |
| Shared topic mapping, rename 其他 partial move |  [`topic_mapping_and_move.puml`](diagrams/topic_mapping_and_move.puml)  |  [`topic_mapping_and_move.svg`](diagrams/topic_mapping_and_move.svg)  |
| Content conversion, deferred repair 其他 reconversion |  [`content_conversion_and_repair.puml`](diagrams/content_conversion_and_repair.puml)  |  [`content_conversion_and_repair.svg`](diagrams/content_conversion_and_repair.svg)  |
| Outbound retry, permanent failure 其他 public events |  [`outbound_delivery.puml`](diagrams/outbound_delivery.puml)  |  [`outbound_delivery.svg`](diagrams/outbound_delivery.svg)  |

## 圣经词典

- **Bridge process** — 没有直接访问数据库的外部信任过程
  Workspace;
- **service identity** — 现行 realm-bound mTLS identity private External
  Bridge API: `realm_uuid`, `provider_kind`, `bridge_instance_uuid` 其他
  `identity_generation` 只有经过检查的 client certificate;
  account/project scope 并且允许命令 Workspace 然后确定
  现在 server-owned assignments;
- **provider object key** — 稳定的内部身份 Zulip-对象,
  对于实时和 history;
- **provider event key** — 一个变异/版本Zulip对象的稳定密钥,
  作为 idempotency/derivation key;
- **provider object UUIDv5** — `UUIDv5(namespace=verified realm UUID,
  name="<entity_type>:<decimal_provider_id>")` 为了 numeric Zulip objects;
- **registration boundary** — 新的 Zulip queue 边界: realtime 接受
  历史根将其之前的选择快照/range导入;
- **account lease/fencing generation** — Workspace-issued exclusive ownership
  所有的外部帐户为一个桥实例; stale owner不能 commit;
- **history root/stream task** — durable Workspace task: root 揭示范围和
  创建每流任务; 流任务重启时重复其范围;
- **deferred resolution** — 它们是不可应用的依赖
  在基础物体出现之前;
- **Workspace projection worker** — 内部工作者 Workspace,谁
  在outbox之后启动; 它不是 Bridge process;
- **WebSocket dispatcher** — 一个单独的组件 Workspace 提供
  已准备好 durable public events 并不参与进口 Zulip.

## 已接受的变量

1. `Zulip Realtime Connector` 和 `Zulip History Importer`  独立的过程
   具有共同的identity/idempotency语义,但不同的生命周期.
2. 没有一个桥进程会直接写到WorkspacePostgreSQL或 object
   storage metadata. 所有的域突变都通过有限的
   国内 Workspace API.
3. 使用者访问令牌未使用.
   任意的 `project_id`,源或 Workspace 用户;这些值输出和
   检查 Workspace 服务身份和服务器身份 mappings.
4. 创建消息使用普通域名交易 Workspace:
   canonical `MESSAGE` + 必须的 `TOPIC` 和 `MESSAGE_PLACEMENT` + 作者
   `USER_MESSAGE_BINDING`/`USER_MESSAGE_STATE` + immutable outbox event.
5. 公共 UUID 消息等于 placement UUID:
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Canonical `MESSAGE.uuid`
   保持内部.
6. Bridge 不执行接收者粉丝,不更新Workspace投影,
   没有创建 public WebSocket 事件. Workspace workers;
   dispatcher 只是传递已准备好的事物..
7. Connect, reconnect, queue expiry, missing heartbeat, `restart` 其他
   `web_reload_client` 使用一个bootstrap: 登录一个新bootstrap queue,
   获得边界,开始实时,然后才创建 history root task.
   旧的 queue/cursor不是 durable state; 覆盖/no-gap提供
   boundary 和一般的 provider keys.
8. 旧的 UUID 之前的 Zulip 进口后, 已同意的全重置保存
   在新的进口中,任何重试/resume都必须重复
   接收到相同的新 canonical row.
9. 每个 Zulip 事件家族的规范覆盖和方向
      [`event_coverage.md`](event_coverage.md). Bidirectional mutation 带着
   origin/causation/provider identity; 自己的provider echo 确认
   没有启动无限反写.
10. Durable mappings, assignments, leases, tasks, outbound operations 其他 errors
    它们属于Workspace. 桥实例没有共同的 Bridge database;
    local state 只是一个抛弃的 cache.
11. 一个帐户完全属于一个fenced Bridge owner:
    history 没有分为 instances. Assignment sticky; healthy accounts
    它们在新出现时不会自动重新平衡 instance.
12. Bridge 转换提供者事件/operations,但没有实现 Workspace
    domain policy. History visibility, bindings 并且Archive Semantics 解决了这个问题.
    Workspace 根据 current stream settings.
13. 两个桥进程都会利用现有的身份验证 private
    External Bridge API: TLS 1.2+ mutual TLS, realm control CA, 一次性
    enrollment 并且是一个代码链接.HTTP headers/body没有
    它们可以更换 certificate identity. Whole-account lease/fencing —
    额外的交易时间授权,而不是凭证和替换
    mTLS.

## 统一列表OPEN- 解决方案 Zulip Bridge {#единый-список-open-решений-zulip-bridge}

这里是本目录中唯一的未完成的解决方案.
文件链接到这里,而不是创建自己的副本.

之前的开放式 wire transport, event/direct keys,私人启动表面和
cross-account project scope 关闭的解决方案 `1B/2A/3A/4A/5A`
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

1. Operational upper limits 在之后 load tests: maximum/optimal history worker
   pool 之前的 default `4`, history batch/rate budgets, provider admission 和
   retention failed history/DLQ/deferred evidence, 没有被接受的
   successful/permanent-operation TTL.
   所有路径都是 bounded/configurable;一个 account-level limiter 和 realtime priority
   已经记录了.
2. 方向和模型 `saved_snippets`:家族是 `OPEN` 而不是
   它们会被自动解释为 Workspace draft/message.
3. 精确地显示 realm-wide Zulip `realm_user/update person.role`
    Workspace role model. 它们不能被置. channel-specific
    `WorkspaceStreamBinding.role`.
4. Exact converter edge/loss policy 对于Zulip→canonical Markdown和相反的
    URN resolution, 包含 unsupported Zulip标记.
    manual reconversion boundary 已经被接受.

Retention 现在就没有了.OPEN: 完成历史任务和 successful outbound
operations 保存 `30 days`, internal permanent-failure operation/code/reason
— `90 days`, provider mappings/latest hidden raw metadata — lifetime 相关的
entity. 未来可能的手动要求仍然是内部扩展,不是新的.
current public endpoint. Retention failed history/DLQ/deferred evidence 留下来
OPEN #1 并且不能用 `30/90 days`.

相关的总 OPEN 解决方案 Messenger,包括容量/SLO,
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#единственный-список-open-решений).

[← 文件的主要索引](../index.md) · [规范库存 Messenger](../messenger_architecture_inventory.md) · [现行边界 Zulip v1](../zulip_bridge_v1_product_and_api.md)
