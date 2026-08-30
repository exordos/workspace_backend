# 文件 Workspace Backend

文件的主导索引 Workspace 后端.每个人的状态
文件明确表示: ** 现有合同/现有架构**
描述当前的行为,而**proposal (设计建议)**则是指
只有对未来的重造设计,不允许更改代码.

## 项目文档词典 {#глоссарий-проектной-документации}

- 位置 (**placement**)  具体的定制信息
  stream/topic;
- 绑定 (**binding**) 访问和用户的个人状态或
  容器;
- transactional outbox — 记录交易中不变的事件日志;
- 投影 (**projection**)  预先准备的简单状态
  阅读 API;
- fan-out — 接收者之间的背景分配;
- worker (背景表演者)  典型化任务和投影处理者.

文件中的实体,字段,路线,JSON值和任务类型名称
它们是按照合同形式保存的.

## 现行公开API和合同

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`workspace_api.md`](workspace_api.md)  | **现行合同** | 客户合同 Workspace/Messenger REST,事件和 WebSocket:路线, JSON,状态,过器,页面和边界 runtime/OpenAPI. |
|  [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md)  | **现行合同** | REST-接收,Epoch 标和交付/重复 WebSocket Workspace UI. |
|  [`architecture.md`](architecture.md)  | **现有的建筑** | 服务的当前边界,拥有 PostgreSQL/S3/IAM/provider运行时间和部署方案. |
|  [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md)  | **已达成的边界; 需要单独的解决方案** | 提供商独立的外部帐户合同/chat/operation/bridge和产品边界 Zulip v1. |
|  [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md)  | **现行解决方案** | 已同意的 `1B/2A/3A/4A/5A`:提供者 API v2,项目范围, realm-global IDs,授权边界和 state-based event key. |

## Proposal: Messenger域和架构 API

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`messenger_domain_model.md`](messenger_domain_model.md)  | **proposal** | 定制性`MESSAGE`,显式的位置,用户绑定消息/容器,变量和开放解决方案. |
|  [`messenger_api_domain_model.md`](messenger_api_domain_model.md)  | **proposal** | 三层 RestAlchemy API → 简单的表达 → 物理实体,查询/背景处理路径和并行性 worker. |
|  [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md)  | **proposal 实施** | 具体声明 RestAlchemy,资源/控制器,字段的起源和不可变的公开 JSON 合同 core API. |
|  [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md)  | **proposal; 规范库存** | 单一词典 class→table/view→fields→keys, UUID, task/event kinds, scope routing,风险状态和剩余的 OPEN解决方案. |

## 数据模型和PlantUML概述图

| 综述 | 情况 | 源头 | SVG |
| --- | --- | --- | --- |
| ER-域名模型 Messenger | **proposal** |  [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml)  |  [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg)  |
| 层和背景处理 Messenger API | **proposal** |  [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml)  |  [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg)  |
| 显示 route/resource/view/table RestAlchemy | **proposal** |  [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml)  |  [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg)  |

## 详细的序列图

| 索引 | 情况 | 职位 |
| --- | --- | --- |
|  [`diagrams/sequence/README.md`](diagrams/sequence/README.md)  | **proposal, 显示在现有合同上** | 完整的方法+路径矩阵:单独的Markdown,每一个公开的HTTP操作都需要编辑 PlantUML和SVG,以及 Events WebSocket. |

每个操作规范都保留了现有的 request/response,但
显示目标的交易/outbox/task/worker/event路径.
取代 [`workspace_api.md`](workspace_api.md).

## Proposal: 目标架构 Zulip Bridge

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`zulip_bridge/README.md`](zulip_bridge/README.md)  | **proposal; 标签** | 统一导航,接受的版本,词典和规范 OPEN-list target Bridge. |
|  [`architecture_overview.md`](zulip_bridge/architecture_overview.md)  | **proposal** | 两个桥进程,粘性全账户所有权/scheduling,私有WorkspaceAPI,以及严格的边界 domain workers/WebSocket dispatcher. |
|  [`event_coverage.md`](zulip_bridge/event_coverage.md)  | **proposal; 接受的覆盖** | 正确的Zulip事件/operations,方向Workspace↔Zulip,真理的来源和保护 echo loop. |
|  [`realtime_connector.md`](zulip_bridge/realtime_connector.md)  | **proposal** | 支持的变更进行持续的双向实时同步, echo prevention, retry/backpressure/restart. |
|  [`history_importer.md`](zulip_bridge/history_importer.md)  | **proposal** | Root→per-stream newest-first tasks, fair pool default `4`, account rate limit 并且没有 restart unfinished stream range message checkpoint. |
|  [`internal_workspace_api.md`](zulip_bridge/internal_workspace_api.md)  | **proposal 在上面 current mTLS** | 共有私有命令边界,重新利用现有的 External Bridge mTLS identity,server-owned scope 和 idempotency realtime/history. |
|  [`coordination_and_recovery.md`](zulip_bridge/coordination_and_recovery.md)  | **proposal** | Whole-account lease/fencing, 单一的队列bootstrap/boundary, retry/DLQ和恢复没有 Bridge-local durable DB. |
|  [`account_lifecycle_and_identity.md`](zulip_bridge/account_lifecycle_and_identity.md)  | **proposal; current routes preserved** | Account connect/reconnect/disconnect/delete, verified claim, unmanaged external users 其他 multi-account canonical union. |
|  [`provider_mappings_and_content.md`](zulip_bridge/provider_mappings_and_content.md)  | **proposal** | Realm-scoped provider/topic/file mappings, canonical Markdown/URN, deferred references 其他 manual reconversion. |
|  [`delivery_and_events.md`](zulip_bridge/delivery_and_events.md)  | **proposal** | Durable outbound operations, conflict/permanent-failure semantics 其他 ready public event invariants. |

新目录描述了 target ingestion 设计,而不是取代现有的目录
封闭 OpenAPI 或食品边界
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Worker, outbox, 投影和运输 WebSocket

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md)  | **proposal** | 总的工人架构和单独的过程 `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
|  [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md)  | **proposal** | Transactional outbox, 每个事件的单独的 immutable task, scoped ownership, newest-first, 已准备的事件和单独的 dispatcher. |
|  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md)  | **实现的操作程序** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup 其他 generation-triggered fresh reimport. |

## 关闭的文物 provider/control API

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml)  | **现行封闭合同** | 关闭的提供者数据-plane OpenAPI 具有认证 bridge. |
|  [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml)  | **现行封闭合同** | Provider-native command wire format 具有服务器拥有 Workspace 范围; lease/result transport 兼容 v1. |
|  [`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml)  | **现行封闭合同** | OpenAPI control plane 为了 Zulip bridge. |
|  [`zulip_bridge_file_api_v1.yaml`](../zulip_bridge_file_api_v1.yaml)  | **现行封闭合同** | 内部 OpenAPI 文件传输 bridge. |

封闭的 API 不是客户端路线 Workspace.
公共 API 描述在
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## 迁移,部署和实施指导

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md)  | **需要对其进行一致** | 更新,回转和检查当前迁移的程序 unread projection. |
|  [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md)  | **现行接收计划** | 检查原生 Messenger/API/realtime/S3,恢复,重建,规模和负载. |
|  [`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md)  | **现行接收屏障** | 检查IAM,桥梁,恢复,UI和部署外部集成. |

Proposal-文件不是迁移或销售计划. Production-
变更只在单独的建筑解决方案和
根据 migration/test design.
