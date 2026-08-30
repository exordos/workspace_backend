# Документация Workspace Backend

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

## Действующий публичный API и контракт

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **действующий контракт** | Канонический клиентский контракт Workspace/Messenger REST, Events и WebSocket: маршруты, JSON, статусы, фильтры, пагинация и граница runtime/OpenAPI. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **действующий контракт** | REST-догрузка, epoch cursor и доставка/повтор WebSocket для Workspace UI. |
| [`architecture.md`](architecture.md) | **действующая архитектура** | Текущие границы сервисов, владение PostgreSQL/S3/IAM/provider runtime и схема развёртывания. |
| [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md) | **согласованная граница; реализация требует отдельного решения** | Независимый от provider контракт внешних account/chat/operation/bridge и продуктовая граница Zulip v1. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **действующее решение реализации** | Согласованные `1B/2A/3A/4A/5A`: Provider API v2, project scope, realm-global IDs, authorization boundary и state-based event key. |

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

## Proposal: целевая архитектура Zulip Bridge

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`zulip_bridge/README.md`](zulip_bridge/README.md) | **proposal; индекс** | Единая навигация, принятые инварианты, глоссарий и канонический OPEN-list target Bridge. |
| [`architecture_overview.md`](zulip_bridge/architecture_overview.md) | **proposal** | Два Bridge process, sticky whole-account ownership/scheduling, private Workspace API и строгая граница с domain workers/WebSocket dispatcher. |
| [`event_coverage.md`](zulip_bridge/event_coverage.md) | **proposal; принятое покрытие** | Каноническая матрица exact Zulip events/operations, направления Workspace↔Zulip, source of truth и защита от echo loop. |
| [`realtime_connector.md`](zulip_bridge/realtime_connector.md) | **proposal** | Постоянная двусторонняя realtime synchronization поддерживаемых изменений, echo prevention, retry/backpressure/restart. |
| [`history_importer.md`](zulip_bridge/history_importer.md) | **proposal** | Root→per-stream newest-first tasks, fair pool default `4`, account rate limit и restart unfinished stream range без message checkpoint. |
| [`internal_workspace_api.md`](zulip_bridge/internal_workspace_api.md) | **proposal поверх current mTLS** | Общий private command boundary, переиспользование действующей External Bridge mTLS identity, server-owned scope и idempotency realtime/history. |
| [`coordination_and_recovery.md`](zulip_bridge/coordination_and_recovery.md) | **proposal** | Whole-account lease/fencing, единый queue bootstrap/boundary, retry/DLQ и recovery без Bridge-local durable DB. |
| [`account_lifecycle_and_identity.md`](zulip_bridge/account_lifecycle_and_identity.md) | **proposal; current routes preserved** | Account connect/reconnect/disconnect/delete, verified claim, unmanaged external users и multi-account canonical union. |
| [`provider_mappings_and_content.md`](zulip_bridge/provider_mappings_and_content.md) | **proposal** | Realm-scoped provider/topic/file mappings, canonical Markdown/URN, deferred references и manual reconversion. |
| [`delivery_and_events.md`](zulip_bridge/delivery_and_events.md) | **proposal** | Durable outbound operations, conflict/permanent-failure semantics и ready public event invariants. |

Новый каталог описывает target ingestion design и не заменяет действующие
закрытые OpenAPI или продуктовую границу
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Worker, outbox, проекции и доставка WebSocket

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **proposal** | Общая архитектура worker и отдельные процессы `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **proposal** | Transactional outbox, отдельная immutable task для каждого события, scoped ownership, newest-first, готовые события и отдельный dispatcher. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **реализованная операторская процедура** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup и generation-triggered fresh reimport. |

## Артефакты закрытых provider/control API

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml) | **действующий закрытый контракт** | Закрытый Provider data-plane OpenAPI с аутентификацией bridge. |
| [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml) | **действующий закрытый контракт** | Provider-native command wire format с server-owned Workspace scope; lease/result transport совместим с v1. |
| [`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml) | **действующий закрытый контракт** | OpenAPI control plane для Zulip bridge. |
| [`zulip_bridge_file_api_v1.yaml`](../zulip_bridge_file_api_v1.yaml) | **действующий закрытый контракт** | Внутренний OpenAPI передачи файлов bridge. |

Закрытые API не являются клиентскими маршрутами Workspace. Их граница с
публичным API описана в
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Миграция, развёртывание и руководство по реализации

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **действующая инструкция; требуется согласование** | Процедуры обновления, отката и проверки текущей миграции unread projection. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **действующий план приёмки** | Проверки native Messenger/API/realtime/S3, восстановления, rebuild, scale и нагрузки. |
| [`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md) | **действующий приёмочный барьер** | Проверка IAM, bridge, восстановления, UI и развёртывания внешней интеграции. |

Proposal-документы не являются планом миграции или реализации. Production-
изменения начинаются только после отдельного архитектурного решения и
согласованных migration/test design.
