# Workspace 后端文档

当前契约：[Workspace 公共 OpenAPI](../public_workspace_api.yaml)。
外部账户按所有者/项目范围隔离。Zulip 和 Telegram 通过规范的 Messenger 模型进行同步，与原生消息传递并行。
提供商指南描述当前活跃的实现。旧的设计提案仅作为历史参考，不授权进行更改。

这是 Workspace 后端文档的主导航索引。每份文档的状态均已明确说明：**活跃契约/活跃架构**描述当前行为，而**提案（设计提案）**仅适用于未来的重构设计，不授权代码更改。

## 项目文档术语表 {#глоссарий-проектной-документации}

- placement — 特定流/主题中的规范消息；
- binding — 用户或容器的访问权限和个人状态；
- transactional outbox — 写事务内的不可变事件日志；
- projection — 用于简化 API 读取的预计算状态；
- fan-out — 将绑定后台分发给接收者；
- worker（后台执行器）— 处理类型化任务和投影的处理程序。

文档中的实体名称、字段、路由、JSON 值和任务类型均保留其确切的契约形式。

## Telegram 提供商

[Telegram 账户、自动完整历史记录和同步](telegram_provider.md)。

## 公共 API 和设计历史

| 文档 | 状态 | 目的 |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **活跃契约** | Workspace/Messenger REST、Events 和 WebSocket 的规范客户端契约：路由、JSON、状态、过滤器、分页以及运行时/OpenAPI 边界。 |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **活跃契约** | Workspace UI 的 REST 回填、纪元游标以及 WebSocket 交付/重试。 |
| [`architecture.md`](architecture.md) | **历史参考** | 已退役的私有桥接和提供商 API 架构。 |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **历史参考** | 已退役的 Provider API v2 和 mTLS 传输决策。 |

## 提案：Messenger 领域和 API 架构

| 文档 | 状态 | 目的 |
| --- | --- | --- |
| [`messenger_domain_model.md`](messenger_domain_model.md) | **提案** | 规范 `MESSAGE`、显式放置、消息/容器的用户绑定、不变量以及未决事项。 |
| [`messenger_api_domain_model.md`](messenger_api_domain_model.md) | **提案** | 三层架构：RestAlchemy API → 简单视图 → 物理实体；查询/后台处理路径和 worker 并行性。 |
| [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md) | **实施提案** | 具体的 RestAlchemy 声明、资源/控制器、字段来源以及核心 API 的不可变公共 JSON 契约。 |
| [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md) | **提案；规范清单** | 统一字典：类→表/视图→字段→键、UUID、任务/事件类型、范围路由、风险状态以及剩余的 OPEN 决策。 |

## 数据模型和 PlantUML 概览图

| 概览 | 状态 | 源文件 | SVG |
| --- | --- | --- | --- |
| Messenger 领域 ER 模型 | **提案** | [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml) | [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg) |
| Messenger API 层和后台处理 | **提案** | [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml) | [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg) |
| RestAlchemy 路由/资源/视图/表映射 | **提案** | [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml) | [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg) |

## 操作的详细序列图

| 索引 | 状态 | 目的 |
| --- | --- | --- |
| [`diagrams/sequence/README.md`](diagrams/sequence/README.md) | **提案，映射到活跃契约** | 完整的方法+路径矩阵：每个公共 HTTP 操作以及 Events WebSocket 的独立 Markdown、可编辑 PlantUML 和 SVG。 |

每个操作规范保留活跃的请求/响应，但展示目标事务/outbox/任务/worker/事件路径。这些文档不替代 [`workspace_api.md`](workspace_api.md)。

## Worker、Outbox、投影和 WebSocket 交付

| 文档 | 状态 | 目的 |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **提案** | 通用 worker 架构和独立进程 `fanout`、`content_mentions`、`reaction_snapshot`、`read_counters`、`delivery_snapshot_event`、`topic_membership_policy_rebuild`。 |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **提案** | 事务性 outbox、每个事件的独立不可变任务、范围所有权、最新优先、就绪事件以及独立调度器。 |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **已实施的操作员程序** | 备份/恢复、原生保留、迁移时重置 Zulip 派生的消息/文件、持久文件清理以及由生成触发的全新重新导入。 |

## 迁移、部署和实施指南

| 文档 | 状态 | 目的 |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **活跃指令；需要批准** | 更新、回滚和验证当前未读投影迁移的程序。 |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **活跃验收计划** | 针对原生 Messenger/API/实时/S3、恢复、重建、规模和负载的检查。 |

提案文档不是迁移或实施计划。只有在单独的架构决策和商定的迁移/测试设计之后，才开始生产环境更改。
