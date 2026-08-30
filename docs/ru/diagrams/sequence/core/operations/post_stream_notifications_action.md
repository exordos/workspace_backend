# POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke


Общий target-инвариант надёжности: каждое immutable outbox event выводит ровно одну immutable typed task с уникальным `outbox_event_uuid`; coalescing отсутствует. Task хранит фактический exact scope key, использует lease/fencing, retry/backoff, max attempts/DLQ, reaper и идемпотентный effect guard. Topic scope применяется только к placement/message-binding work; shared rows не получают неявный fallback на topic.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)

## Статус и граница текущего контракта

Целевая спецификация в подходе docs-first. Метод, путь, публичный JSON и авторизация следуют текущему контракту из [`workspace_api.md`](../../../../workspace_api.md); bounded pagination и asynchronous visibility следуют отдельно принятому target compatibility ADR.

![Диаграмма последовательности](diagrams/post_stream_notifications_action.svg)

Редактируемый исходник: [`post_stream_notifications_action.puml`](diagrams/post_stream_notifications_action.puml).

## Операция

**Метод и путь:** `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke`

**Назначение:** Установить режим уведомлений потока для текущего пользователя.

## Публичный запрос

```json
{
  "notification_mode": "mentions_only"
}
```

## Успешный публичный ответ

HTTP `200`:

```json
{
  "uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "Инженерия",
  "description": "Инженерное пространство",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "owner": "11111111-1111-1111-1111-111111111111",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "role": "owner",
  "notification_mode": "mentions_only",
  "unread_count": 2,
  "active_unread_count": 1,
  "passive_unread_count": 1,
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false,
  "private": false,
  "is_archived": false,
  "color": 3368601,
  "last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "default_topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "created_at": "2026-06-22T09:00:00Z",
  "updated_at": "2026-06-22T10:16:00Z"
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


class WorkspaceUserStream(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_streams_v1"

    owner = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    direct_user_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True,
    )
    last_message_uuid = properties.property(types.UUID(), read_only=True)
    default_topic_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceStreamController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserStream, convert_underscore=False, process_filters=True,
    )
```

Публичные ссылки на сущности представлены скалярными свойствами `types.UUID()`, а не отношениями RestAlchemy, которые сериализуются в URI. Физические столбцы `*_uuid` остаются индексированными внешними ключами с явно выбранными действиями ссылочной целостности. Публичное поле `owner` является свойством UUID; физическое поле `owner_uuid` — индексированным внешним ключом пользователя. USER_STREAM_BINDING хранит готовые счётчики уровня потока.

## Синхронная транзакция

1. Разрешить USER_STREAM_BINDING.
2. Установить режим и серверную временную метку.
3. Добавить отдельные immutable outbox events для `read_counters` и, когда
   нужен готовый публичный event, `delivery_snapshot_event`; каждое событие
   выводит ровно одну task.

Затрагиваемое состояние: USER_STREAM_BINDING и transactional outbox; готовые счётчики хранятся здесь.

## Типизированные задачи и фоновый исполнитель

Задачи: `read_counters` и необязательная `delivery_snapshot_event`.

Отдельные fenced owners `user-stream`/`user-topic`/`user-folder` scopes
идемпотентно классифицируют готовые счётчики и подготавливают события. Topic
worker shared rows не пишет; каждое outbox event выводит отдельную task.

## Публичные события и WebSocket

Обновления потока и темы текущего пользователя.

## Идемпотентность, гонки и видимые клиенту временные характеристики

Одна строка владеет областью; `COUNT` во время запроса не выполняется. Режим меняется сразу, счётчики и события могут отставать.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)
