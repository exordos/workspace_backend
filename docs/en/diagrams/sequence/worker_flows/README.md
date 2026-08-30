[← The main index of the documentation](../../../index.md) · [Index of sequence diagrams](../README.md) · [The architecture of the worker](worker_architecture.md)

# Worker flows and typed tasks

These sequence specifications, started with documentation, complement the HTTP-operations files
and not create new public endpoints.

The terms used are worker , fan-out , tie-in .
(binding), the placement , the transaction journal (transactional outbox)
and projection (projection).

| The stream | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| The architecture of the worker |  [`worker_architecture.md`](worker_architecture.md)  |  [`worker_architecture.puml`](diagrams/worker_architecture.puml)  |  [`worker_architecture.svg`](diagrams/worker_architecture.svg)  |
| `fanout` |  [`task_fanout.md`](task_fanout.md)  |  [`task_fanout.puml`](diagrams/task_fanout.puml)  |  [`task_fanout.svg`](diagrams/task_fanout.svg)  |
| `content_mentions` |  [`task_content_mentions.md`](task_content_mentions.md)  |  [`task_content_mentions.puml`](diagrams/task_content_mentions.puml)  |  [`task_content_mentions.svg`](diagrams/task_content_mentions.svg)  |
| `reaction_snapshot` |  [`task_reaction_snapshot.md`](task_reaction_snapshot.md)  |  [`task_reaction_snapshot.puml`](diagrams/task_reaction_snapshot.puml)  |  [`task_reaction_snapshot.svg`](diagrams/task_reaction_snapshot.svg)  |
| `read_counters` |  [`task_read_counters.md`](task_read_counters.md)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `folder_projection` |  [`task_read_counters.md`](task_read_counters.md#триггеры-и-поток)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `delivery_snapshot_event` |  [`task_delivery_snapshot_event.md`](task_delivery_snapshot_event.md)  |  [`task_delivery_snapshot_event.puml`](diagrams/task_delivery_snapshot_event.puml)  |  [`task_delivery_snapshot_event.svg`](diagrams/task_delivery_snapshot_event.svg)  |
| `topic_membership_policy_rebuild` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| `topic_state_projection` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md#topic_state_projection)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| migration/release runbook |  [`migration_release_runbook.md`](migration_release_runbook.md)  |  [`migration_release_runbook.puml`](diagrams/migration_release_runbook.puml)  |  [`migration_release_runbook.svg`](diagrams/migration_release_runbook.svg)  |

Commonly accepted invariants:

- clear work through outbox and tasks, without searching for missing lines;
- One immutable task per one outbox event; no coalescing in initial design;
- It 's a custom parallelism and a single fenced owner exact scope key;
- topic ownership Applies only to placements/bindings topics; shared rows
  use `message`, `user-stream`, `user-topic` or `user-folder` scope;
- `MESSAGE.created_at DESC` order within the topic at the guaranteed end
  The progress;
- lease expiry, retry/backoff, DLQ/reaper and the idempotent materialization;
- container aggregates on unique user bindings to the container;
- public records of events atomically in one DB transaction with projection;
- separate dispatcher WebSocket for sending, repeating and reproducing.

[← The main index of the documentation](../../../index.md) · [Index of sequence diagrams](../README.md) · [The architecture of the worker](worker_architecture.md)
