[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [工厂的架构](worker_architecture.md)

# 工作流和类型化任务

这些序列的规范,从文档开始,补充了 HTTP 操作文件
并且不会创建新的公共终点..

已接受的术语:工作者,风扇发送者,绑定者
(binding), 交易日记 (placement),交易日记 (transactional outbox)
投影 (projection).

| 流量 | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| 工厂的建筑 |  [`worker_architecture.md`](worker_architecture.md)  |  [`worker_architecture.puml`](diagrams/worker_architecture.puml)  |  [`worker_architecture.svg`](diagrams/worker_architecture.svg)  |
| `fanout` |  [`task_fanout.md`](task_fanout.md)  |  [`task_fanout.puml`](diagrams/task_fanout.puml)  |  [`task_fanout.svg`](diagrams/task_fanout.svg)  |
| `content_mentions` |  [`task_content_mentions.md`](task_content_mentions.md)  |  [`task_content_mentions.puml`](diagrams/task_content_mentions.puml)  |  [`task_content_mentions.svg`](diagrams/task_content_mentions.svg)  |
| `reaction_snapshot` |  [`task_reaction_snapshot.md`](task_reaction_snapshot.md)  |  [`task_reaction_snapshot.puml`](diagrams/task_reaction_snapshot.puml)  |  [`task_reaction_snapshot.svg`](diagrams/task_reaction_snapshot.svg)  |
| `read_counters` |  [`task_read_counters.md`](task_read_counters.md)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `folder_projection` |  [`task_read_counters.md`](task_read_counters.md#триггеры-и-поток)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `delivery_snapshot_event` |  [`task_delivery_snapshot_event.md`](task_delivery_snapshot_event.md)  |  [`task_delivery_snapshot_event.puml`](diagrams/task_delivery_snapshot_event.puml)  |  [`task_delivery_snapshot_event.svg`](diagrams/task_delivery_snapshot_event.svg)  |
| `topic_membership_policy_rebuild` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| `topic_state_projection` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md#topic_state_projection)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| migration/release runbook |  [`migration_release_runbook.md`](migration_release_runbook.md)  |  [`migration_release_runbook.puml`](diagrams/migration_release_runbook.puml)  |  [`migration_release_runbook.svg`](diagrams/migration_release_runbook.svg)  |

已接受的一般变量:

- 通过outbox和任务显然工作,而没有搜索缺失的行;
- 一个immutable task 每一个outbox事件; initial design 中没有 coalescing;
- 可调整并行性和单个 fenced owner exact scope key;
- topic ownership 仅适用于 placements/bindings主题; shared rows
  它们使用的是 `message`, `user-stream`, `user-topic` 或 `user-folder` scope;
- 顺序 `MESSAGE.created_at DESC` 在主题内
  进步;
- lease expiry, retry/backoff, DLQ/reaper 并且具有潜在的物质化;
- 用户对容器的独特绑定集装箱的集装箱;
- 公共记录事件在一个DB交易中的原子投影;
- 单独的管理器 WebSocket 发送,重复和播放.

[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [工厂的架构](worker_architecture.md)
