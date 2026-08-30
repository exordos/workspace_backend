# POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)

## Статус и граница текущего контракта

Целевая спецификация в подходе docs-first. Метод, путь, публичный JSON и авторизация следуют текущему контракту из [`workspace_api.md`](../../../../workspace_api.md); bounded pagination и asynchronous visibility следуют отдельно принятому target compatibility ADR.

![Диаграмма последовательности](diagrams/post_message_unstar_action.svg)

Редактируемый исходник: [`post_message_unstar_action.puml`](diagrams/post_message_unstar_action.puml).

## Операция

**Метод и путь:** `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke`

**Назначение:** Снять глобальное для сообщения состояние «в избранном» текущего пользователя.

## Публичный запрос

Без тела JSON.

## Успешный публичный ответ

HTTP `200`:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Привет, Workspace"
  },
  "user_uuid": "33333333-3333-3333-3333-333333333333",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": false,
  "mentioned": false,
  "reactions": {},
  "reaction_users": {},
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
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


class WorkspaceUserMessage(models.ModelWithProject, orm.SQLStorableMixin):
    __tablename__ = "messenger_api_user_messages_v1"

    binding_uuid = properties.property(types.UUID(), id_property=True)
    uuid = properties.property(types.UUID(), read_only=True)
    stream_uuid = properties.property(types.UUID(), read_only=True)
    topic_uuid = properties.property(types.UUID(), read_only=True)
    author_uuid = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceMessageController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserMessage, convert_underscore=False, process_filters=True,
    )
```

Публичный `uuid` и route ID равны `MESSAGE_PLACEMENT.uuid`; canonical `MESSAGE.uuid` и `binding_uuid` скрыты. Placement однозначно выбирает state, а action синхронно проверяет active membership и generation.

## Синхронная транзакция

1. Разрешить публичный placement UUID и доступ текущего пользователя.
2. Установить уникальное значение USER_MESSAGE_STATE.starred=false.
3. Только при изменении добавить immutable outbox event для отдельной task
   scope `user-message` `(project_id,user_uuid,placement_uuid)`.

Затрагиваемое состояние: USER_MESSAGE_STATE, область доступа и transactional outbox; счётчики контейнеров никогда не хранятся в привязке сообщения.

## Типизированные задачи и фоновый исполнитель

Задачи: отдельная immutable task `read_counters` для source outbox event; без coalescing.

Fenced owner exact scope `user-message` читает актуальное состояние и готовит
публичное событие пользователя; topic lock не используется. Task lifecycle
включает retry/backoff, DLQ/reaper и idempotent effect по `outbox_event_uuid`.

## Публичные события и WebSocket

`message.updated` текущего пользователя; диспетчер отправляет событие только при изменении

## Идемпотентность, гонки и видимые клиенту временные характеристики

Установка состояния идемпотентна; текущее состояние меняется сразу, а агрегаты и события могут ненадолго отставать.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)
