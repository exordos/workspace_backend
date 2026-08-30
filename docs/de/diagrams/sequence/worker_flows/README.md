[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge-Diagramme](../README.md) · [Architektur des Vorker](worker_architecture.md)

# Vorlauf und typische Aufgaben

Diese Sequenz-Spezifikationen, die mit der Dokumentation begonnen haben, ergänzen die HTTP -Operationsdateien
und keine neuen öffentlichen Endpunkte schaffen.

Die angenommenen Begriffe sind: Worker , Fan-out , Bindung
(binding), Platzierung (placement), Transaktionsjournal (transactional outbox)
und Projektion (projection).

| Der Fluss | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| Architektur des Vorker |  [`worker_architecture.md`](worker_architecture.md)  |  [`worker_architecture.puml`](diagrams/worker_architecture.puml)  |  [`worker_architecture.svg`](diagrams/worker_architecture.svg)  |
| `fanout` |  [`task_fanout.md`](task_fanout.md)  |  [`task_fanout.puml`](diagrams/task_fanout.puml)  |  [`task_fanout.svg`](diagrams/task_fanout.svg)  |
| `content_mentions` |  [`task_content_mentions.md`](task_content_mentions.md)  |  [`task_content_mentions.puml`](diagrams/task_content_mentions.puml)  |  [`task_content_mentions.svg`](diagrams/task_content_mentions.svg)  |
| `reaction_snapshot` |  [`task_reaction_snapshot.md`](task_reaction_snapshot.md)  |  [`task_reaction_snapshot.puml`](diagrams/task_reaction_snapshot.puml)  |  [`task_reaction_snapshot.svg`](diagrams/task_reaction_snapshot.svg)  |
| `read_counters` |  [`task_read_counters.md`](task_read_counters.md)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `folder_projection` |  [`task_read_counters.md`](task_read_counters.md#триггеры-и-поток)  |  [`task_read_counters.puml`](diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](diagrams/task_read_counters.svg)  |
| `delivery_snapshot_event` |  [`task_delivery_snapshot_event.md`](task_delivery_snapshot_event.md)  |  [`task_delivery_snapshot_event.puml`](diagrams/task_delivery_snapshot_event.puml)  |  [`task_delivery_snapshot_event.svg`](diagrams/task_delivery_snapshot_event.svg)  |
| `topic_membership_policy_rebuild` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| `topic_state_projection` |  [`task_topic_membership_policy_rebuild.md`](task_topic_membership_policy_rebuild.md#topic_state_projection)  |  [`task_topic_membership_policy_rebuild.puml`](diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](diagrams/task_topic_membership_policy_rebuild.svg)  |
| migration/release runbook |  [`migration_release_runbook.md`](migration_release_runbook.md)  |  [`migration_release_runbook.puml`](diagrams/migration_release_runbook.puml)  |  [`migration_release_runbook.svg`](diagrams/migration_release_runbook.svg)  |

Allgemein angenommenen Invarianten:

- Offene Arbeit über Outbox und Aufgaben, ohne nach fehlenden Zeilen zu suchen;
- Eine unveränderliche Aufgabe pro Outbox-Event; kein coalescing im initial design;
- Einstellbarer Parallelismus und ein fenced owner exact scope key;
- topic ownership Wird nur für Placements/bindings Themen angewendet; shared rows
  Sie benutzen `message`, `user-stream`, `user-topic` oder `user-folder` scope;
- `MESSAGE.created_at DESC` -Regelung innerhalb des Themas bei garantiertem Ende
  Fortschritt;
- lease expiry, retry/backoff, DLQ/reaper und eine idempotentielle Verkörperung;
- Aggregate von Containern auf einzigartigen Bindungen des Benutzers an den Container;
- öffentliche Ereignisse Atom in einer DB-Transaktion mit Projektion;
- Ein separater Dispatcher WebSocket für das Versenden, Wiederholen und Wiedergeben.

[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge-Diagramme](../README.md) · [Architektur des Vorker](worker_architecture.md)
