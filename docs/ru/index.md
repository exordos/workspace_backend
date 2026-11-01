# Документация бэкенда Workspace

Текущий контракт: [Workspace public OpenAPI](../public_workspace_api.yaml).
Внешние аккаунты имеют область видимости owner/project. Zulip и Telegram синхронизируются через каноническую модель Messenger, наряду с нативной отправкой сообщений.
Руководства по провайдерам описывают активную реализацию. Более старые проектные предложения остаются историческими справочными материалами и не санкционируют изменения.

Главный индекс навигации для документации бэкенда Workspace. Статус каждого документа явно указан: **active contract/active architecture** описывает текущее поведение, тогда как **proposal (design proposal)** относится только к дизайну будущего рефакторинга и не санкционирует изменения в коде.

## Глоссарий документации проекта {#глоссарий-проектной-документации}

- placement — каноническое сообщение в конкретном потоке/теме;
- binding — доступ и персональное состояние пользователя или контейнера;
- transactional outbox — журнал неизменяемых событий внутри транзакции записи;
- projection — предвычисленное состояние для простых чтений API;
- fan-out — фоновое распространение привязок (bindings) получателям;
- worker (фоновый исполнитель) — обработчик типизированных задач и проекций.

Имена сущностей, поля, маршруты, значения JSON и типы задач в документах сохраняются в их точной контрактной форме.

## Провайдер Telegram

[Аккаунты Telegram, автоматическая полная история и синхронизация](telegram_provider.md).

## Публичный API и история дизайна

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **active contract** | Канонический клиентский контракт для Workspace/Messenger REST, Events и WebSocket: маршруты, JSON, статусы, фильтры, пагинация и граница runtime/OpenAPI. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **active contract** | REST backfill, epoch cursor и доставка/повторные попытки WebSocket для интерфейса Workspace. |
| [`architecture.md`](architecture.md) | **historical reference** | Устаревшая архитектура приватного моста и API провайдера. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **historical reference** | Устаревшие решения по Provider API v2 и транспорту mTLS. |

## Предложение: Архитектура домена и API Messenger

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`messenger_domain_model.md`](messenger_domain_model.md) | **proposal** | Канонический `MESSAGE`, явное размещение (placement), привязки пользователей для сообщений/контейнеров, инварианты и открытые решения. |
| [`messenger_api_domain_model.md`](messenger_api_domain_model.md) | **proposal** | Три слоя: RestAlchemy API → простые представления → физические сущности; пути обработки запросов/фоновых задач и параллелизм воркеров. |
| [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md) | **implementation proposal** | Конкретные декларации RestAlchemy, ресурсы/контроллеры, происхождение полей и неизменяемый публичный контракт JSON основного API. |
| [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md) | **proposal; canonical inventory** | Единый словарь: класс→таблица/представление→поля→ключи, UUID, типы задач/событий, маршрутизация области видимости, статусы рисков и оставшиеся решения OPEN. |

## Модель данных и обзорные диаграммы PlantUML

| Обзор | Статус | Источник | SVG |
| --- | --- | --- | --- |
| ER-модель домена Messenger | **proposal** | [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml) | [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg) |
| Слои API Messenger и фоновая обработка | **proposal** | [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml) | [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg) |
| Отображение маршрутов/ресурсов/представлений/таблиц RestAlchemy | **proposal** | [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml) | [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg) |

## Детальные диаграммы последовательности для операций

| Индекс | Статус | Назначение |
| --- | --- | --- |
| [`diagrams/sequence/README.md`](diagrams/sequence/README.md) | **proposal, mapped to the active contract** | Полная матрица method+path: отдельный Markdown, редактируемый PlantUML и SVG для каждой публичной HTTP-операции, а также для Events WebSocket. |

Каждая спецификация операции сохраняет активный запрос/ответ, но показывает целевые пути транзакций/outbox/задач/воркеров/событий. Эти документы не заменяют [`workspace_api.md`](workspace_api.md).

## Воркеры, Outbox, проекции и доставка WebSocket

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **proposal** | Общая архитектура воркеров и отдельные процессы `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **proposal** | Транзакционный outbox, отдельная неизменяемая задача для каждого события, привязка к области видимости, приоритет новых событий, готовые события и отдельный диспетчер. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **implemented operator procedure** | Резервное копирование/восстановление, сохранение нативных данных, сброс сообщений/файлов, полученных из Zulip, во время миграции, надежная очистка файлов и повторный импорт по триггеру генерации. |

## Руководство по миграции, развертыванию и внедрению

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **active instruction; requires approval** | Процедуры обновления, отката и проверки текущей миграции проекции непрочитанных сообщений. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **active acceptance plan** | Проверки для нативного Messenger/API/realtime/S3, восстановления, перестройки, масштабирования и нагрузки. |

Документы-предложения не являются планом миграции или внедрения. Изменения в продакшене начинаются только после отдельного архитектурного решения и согласованного дизайна миграции/тестирования.
