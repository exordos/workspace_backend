# Документация Workspace Backend

Current contract: [Workspace public OpenAPI](../public_workspace_api.yaml).
External accounts contain an owner, `provider`, generic `settings` and `revision`.
Workspace processes native messages and membership; provider synchronization and
external delivery live elsewhere. The remaining design and architecture pages
are historical references from before migration `0186`.

Главный навигационный индекс документации Workspace Backend. Статус каждого
документа указан явно: **действующий контракт/действующая архитектура**
описывает текущее поведение, а **proposal (проектное предложение)** относится
только к проектированию будущего рефакторинга и не разрешает изменения кода.

## Глоссарий проектной документации {#глоссарий-проектной-документации}

- размещение (**placement**) — каноническое сообщение в конкретном
  stream/topic;
- привязка (**binding**) — доступ и персональное состояние пользователя или
  контейнера;
- transactional outbox — журнал неизменяемых событий в транзакции записи;
- проекция (**projection**) — заранее подготовленное состояние для простого
  чтения API;
- fan-out — фоновое распределение привязок получателям;
- worker (фоновый исполнитель) — обработчик типизированных задач и проекций.

Имена сущностей, полей, маршрутов, JSON-значений и типов задач в документах
сохраняются в точном контрактном виде.

## Public API and design history

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **действующий контракт** | Канонический клиентский контракт Workspace/Messenger REST, Events и WebSocket: маршруты, JSON, статусы, фильтры, пагинация и граница runtime/OpenAPI. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **действующий контракт** | REST-догрузка, epoch cursor и доставка/повтор WebSocket для Workspace UI. |
| [`architecture.md`](architecture.md) | **historical reference** | Retired private bridge and provider API architecture. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **historical reference** | Retired Provider API v2 and mTLS transport decisions. |

## Proposal: домен Messenger и архитектура API

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`messenger_domain_model.md`](messenger_domain_model.md) | **proposal** | Канонический `MESSAGE`, явное размещение, пользовательские привязки сообщений/контейнеров, инварианты и открытые решения. |
| [`messenger_api_domain_model.md`](messenger_api_domain_model.md) | **proposal** | Три слоя RestAlchemy API → простые представления → физические сущности, пути запроса/фоновой обработки и параллелизм worker. |
| [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md) | **proposal реализации** | Конкретные декларации RestAlchemy, ресурсы/контроллеры, происхождение полей и неизменяемый публичный JSON-контракт core API. |
| [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md) | **proposal; канонический инвентарь** | Единый словарь class→table/view→fields→keys, UUID, task/event kinds, scope routing, статусы risks и оставшиеся OPEN-решения. |

## Модель данных и обзорные PlantUML-схемы

| Обзор | Статус | Исходник | SVG |
| --- | --- | --- | --- |
| ER-модель домена Messenger | **proposal** | [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml) | [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg) |
| Слои и фоновая обработка Messenger API | **proposal** | [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml) | [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg) |
| Отображение route/resource/view/table RestAlchemy | **proposal** | [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml) | [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg) |

## Подробные sequence diagrams операций

| Индекс | Статус | Назначение |
| --- | --- | --- |
| [`diagrams/sequence/README.md`](diagrams/sequence/README.md) | **proposal, отображённый на действующий контракт** | Полная матрица method+path: отдельный Markdown, редактируемый PlantUML и SVG для каждой публичной HTTP-операции, а также Events WebSocket. |

Каждая спецификация операции сохраняет действующие request/response, но
показывает целевые transaction/outbox/task/worker/event paths. Эти документы не
заменяют [`workspace_api.md`](workspace_api.md).

## Worker, outbox, проекции и доставка WebSocket

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **proposal** | Общая архитектура worker и отдельные процессы `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **proposal** | Transactional outbox, отдельная immutable task для каждого события, scoped ownership, newest-first, готовые события и отдельный dispatcher. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **реализованная операторская процедура** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup и generation-triggered fresh reimport. |

## Миграция, развёртывание и руководство по реализации

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **действующая инструкция; требуется согласование** | Процедуры обновления, отката и проверки текущей миграции unread projection. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **действующий план приёмки** | Проверки native Messenger/API/realtime/S3, восстановления, rebuild, scale и нагрузки. |

Proposal-документы не являются планом миграции или реализации. Production-
изменения начинаются только после отдельного архитектурного решения и
согласованных migration/test design.
