# POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke


Общий target-инвариант надёжности: каждое immutable outbox event выводит ровно одну immutable typed task с уникальным `outbox_event_uuid`; coalescing отсутствует. Task хранит фактический exact scope key, использует lease/fencing, retry/backoff, max attempts/DLQ, reaper и идемпотентный effect guard. Topic scope применяется только к placement/message-binding work; shared rows не получают неявный fallback на topic.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)

## Статус и граница текущего контракта

Целевая спецификация в подходе docs-first. Метод, путь, публичный JSON и авторизация следуют текущему контракту из [`workspace_api.md`](../../../../workspace_api.md); bounded pagination и asynchronous visibility следуют отдельно принятому target compatibility ADR.

![Диаграмма последовательности](diagrams/post_topic_set_default_action.svg)

Редактируемый исходник: [`post_topic_set_default_action.puml`](diagrams/post_topic_set_default_action.puml).

## Операция

**Метод и путь:** `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke`

**Назначение:** Установить тему темой потока по умолчанию.

## Публичный запрос

Без тела JSON.

## Успешный публичный ответ

HTTP `200`:

```json
{
  "uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "name": "Релизы",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "color": 4491468,
  "last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "unread_count": 2,
  "active_unread_count": 2,
  "passive_unread_count": 0,
  "is_default": true,
  "is_done": false,
  "notification_mode": "default",
  "summary": null,
  "summary_last_message_uuid": null,
  "summary_has_new_messages": null,
  "summary_enabled": true,
  "summary_system_prompt": null,
  "summary_reasoning_effort": null,
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T09:10:00Z",
  "updated_at": "2026-06-22T10:20:00Z"
}
```

## Публичные ошибки

Требуются bearer-токен IAM и область проекта. Некорректный UUID или тело запроса дают HTTP `400`; отсутствующий или недоступный в этой области ресурс — `404`. Стандартное документированное тело ошибки валидации:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## Целевая граница RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUserTopic(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_topics_v1"

    stream_uuid = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    last_message_uuid = properties.property(types.UUID(), read_only=True)
    summary_last_message_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceStreamTopicController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserTopic, convert_underscore=False, process_filters=True,
    )
```

Публичные ссылки на сущности представлены скалярными свойствами `types.UUID()`, а не отношениями RestAlchemy, которые сериализуются в URI. Физические столбцы `*_uuid` остаются индексированными внешними ключами с явно выбранными действиями ссылочной целостности. TOPIC является канонической сущностью; уникальная USER_TOPIC_BINDING обеспечивает видимость, персональное состояние и готовые счётчики темы.

## Синхронная транзакция

1. Разрешить и проверить права.
2. Установить STREAM.default_topic_uuid.
3. При изменении добавить отдельные immutable outbox events для
   `topic_state_projection` и `delivery_snapshot_event`.

Затрагиваемое состояние: применимые TOPIC, USER_TOPIC_BINDING, USER_MESSAGE_STATE и transactional outbox; счётчики находятся только в привязках контейнеров.

## Типизированные задачи и фоновый исполнитель

Задачи: отдельные `topic_state_projection` и `delivery_snapshot_event`, каждая
для собственного source outbox event.

Прежняя и новая user-stream/user-topic projections обновляются отдельными
immutable tasks exact scopes с одним fenced owner на key. Topic worker shared
rows не пишет; результаты scopes видимы eventual-consistently.

## Публичные события и WebSocket

Обновления потока, прежней и новой темы. Диспетчер доставляет зафиксированные готовые строки.

## Идемпотентность, гонки и видимые клиенту временные характеристики

Установка состояния идемпотентна, внешний ключ индексирован. Вызывающий видит состояние сразу, производные проекции и события — асинхронно.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)
