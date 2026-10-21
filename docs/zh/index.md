# 文件 Workspace Backend

Current contract: [Workspace public OpenAPI](../public_workspace_api.yaml).
External accounts contain an owner, `provider` and generic `settings`.
Workspace processes native messages and membership; provider synchronization and
external delivery live elsewhere. The remaining design and architecture pages
are historical references from before migration `0186`.

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

## Public API and design history

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`workspace_api.md`](workspace_api.md)  | **现行合同** | 客户合同 Workspace/Messenger REST,事件和 WebSocket:路线, JSON,状态,过器,页面和边界 runtime/OpenAPI. |
|  [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md)  | **现行合同** | REST-接收,Epoch 标和交付/重复 WebSocket Workspace UI. |
|  [`architecture.md`](architecture.md)  | **historical reference** | Retired private bridge and provider API architecture. |
|  [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md)  | **historical reference** | Retired Provider API v2 and mTLS transport decisions. |

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

## Worker, outbox, 投影和运输 WebSocket

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md)  | **proposal** | 总的工人架构和单独的过程 `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
|  [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md)  | **proposal** | Transactional outbox, 每个事件的单独的 immutable task, scoped ownership, newest-first, 已准备的事件和单独的 dispatcher. |
|  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md)  | **实现的操作程序** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup 其他 generation-triggered fresh reimport. |

## 迁移,部署和实施指导

| 文件 | 情况 | 职位 |
| --- | --- | --- |
|  [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md)  | **需要对其进行一致** | 更新,回转和检查当前迁移的程序 unread projection. |
|  [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md)  | **现行接收计划** | 检查原生 Messenger/API/realtime/S3,恢复,重建,规模和负载. |

Proposal-文件不是迁移或销售计划. Production-
变更只在单独的建筑解决方案和
根据 migration/test design.
