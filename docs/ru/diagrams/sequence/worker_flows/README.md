[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательностей](../README.md) · [Архитектура воркера](worker_architecture.md)

# Потоки воркера и типизированных задач

Эти спецификации последовательностей, начатые с документации, дополняют файлы HTTP-операций
и не создают новых публичных конечных точек.

Принятые термины: воркер (worker), веерная рассылка (fan-out), привязка
(binding), размещение (placement), транзакционный журнал (transactional outbox)
и проекция (projection).

| Поток | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| архитектура воркера | [`worker_architecture.md`](worker_architecture.md) | [`worker_architecture.puml`](diagrams/worker_architecture.puml) | [`worker_architecture.svg`](diagrams/worker_architecture.svg) |
| `fanout` | [`task_fanout.md`](task_fanout.md) | [`task_fanout.puml`](diagrams/task_fanout.puml) | [`task_fanout.svg`](diagrams/task_fanout.svg) |
| `content_mentions` | [`task_content_mentions.md`](task_content_mentions.md) | [`task_content_mentions.puml`](diagrams/task_content_mentions.puml) | [`task_content_mentions.svg`](diagrams/task_content_mentions.svg) |
| `reaction_snapshot` | [`task_reaction_snapshot.md`](task_reaction_snapshot.md) | [`task_reaction_snapshot.puml`](diagrams/task_reaction_snapshot.puml) | [`task_reaction_snapshot.svg`](diagrams/task_reaction_snapshot.svg) |
| `read_counters` | [`task_read_counters.md`](task_read_counters.md) | [`task_read_counters.puml`](diagrams/task_read_counters.puml) | [`task_read_counters.svg`](diagrams/task_read_counters.svg) |
| `folder_projection` | [`task_read_counters.md`](task_read_counters.md#триггеры-и-поток) | [`task_read_counters.puml`](diagrams/task_read_counters.puml) | [`task_read_counters.svg`](diagrams/task_read_counters.svg) |
| `delivery_snapshot_event` | [`task_delivery_snapshot_event.md`](task_delivery_snapshot_event.md) | [`task_delivery_snapshot_event.puml`](diagrams/task_delivery_snapshot_event.puml) | [`task_delivery_snapshot_event.svg`](diagrams/task_delivery_snapshot_event.svg) |
| `topic_membership_policy_rebuild` | [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md) | [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml) | [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg) |
| `topic_state_projection` | [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md#topic_state_projection) | [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml) | [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg) |
| migration/release runbook | [`migration_release_runbook.md`](migration_release_runbook.md) | [`migration_release_runbook.puml`](diagrams/migration_release_runbook.puml) | [`migration_release_runbook.svg`](diagrams/migration_release_runbook.svg) |

Общие принятые инварианты:

- явная работа через outbox и задачи, без поиска отсутствующих строк;
- одна immutable task на одно outbox event; coalescing в initial design нет;
- настраиваемый параллелизм и один fenced owner exact scope key;
- topic ownership применяется только к placements/bindings темы; shared rows
  используют `message`, `user-stream`, `user-topic` или `user-folder` scope;
- порядок `MESSAGE.created_at DESC` внутри темы при гарантированном конечном
  прогрессе;
- lease expiry, retry/backoff, DLQ/reaper и идемпотентная материализация;
- агрегаты контейнеров на уникальных привязках пользователя к контейнеру;
- публичные записи событий атомарно в одной DB transaction с проекцией;
- отдельный диспетчер WebSocket для отправки, повторов и воспроизведения.

[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательностей](../README.md) · [Архитектура воркера](worker_architecture.md)
