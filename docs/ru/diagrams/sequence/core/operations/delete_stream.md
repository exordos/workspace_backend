# DELETE /api/workspace/v1/messenger/streams/{stream_uuid}


Общий target-инвариант надёжности: каждое immutable outbox event выводит ровно одну immutable typed task с уникальным `outbox_event_uuid`; coalescing отсутствует. Task хранит фактический exact scope key, использует lease/fencing, retry/backoff, max attempts/DLQ, reaper и идемпотентный effect guard. Topic scope применяется только к placement/message-binding work; shared rows не получают неявный fallback на topic.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)

## Статус и граница текущего контракта

Целевая спецификация в подходе docs-first. Метод, путь, публичный JSON и авторизация следуют текущему контракту из [`workspace_api.md`](../../../../workspace_api.md); bounded pagination и asynchronous visibility следуют отдельно принятому target compatibility ADR.

![Диаграмма последовательности](diagrams/delete_stream.svg)

Редактируемый исходник: [`delete_stream.puml`](diagrams/delete_stream.puml).

## Операция

**Метод и путь:** `DELETE /api/workspace/v1/messenger/streams/{stream_uuid}`

**Назначение:** Удалить канонический поток для всех пользователей.

## Публичный запрос

Без тела JSON.

## Успешный публичный ответ

HTTP `204`; пустое тело.

## Публичные ошибки

Требуются bearer-токен IAM и область проекта. Некорректный UUID или тело запроса дают HTTP `400`; отсутствующий или недоступный в этой области ресурс — `404`. Стандартное документированное тело ошибки валидации:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

Удаление потока с самим собой даёт `400`.

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

1. Разрешить и проверить права.
2. Удалить STREAM с выбранной очисткой по внешним ключам.
3. Добавить отдельные immutable transactional outbox events для каждой
   выводимой `topic_membership_policy_rebuild`, `read_counters`,
   `folder_projection` и `delivery_snapshot_event` task.

Затрагиваемое состояние: корень STREAM, темы, размещения, привязки контейнеров и transactional outbox.

## Типизированные задачи и фоновый исполнитель

Задачи: отдельные `topic_membership_policy_rebuild`, `read_counters`,
`folder_projection` и `delivery_snapshot_event`, каждая для собственного source
outbox event.

Фоновые исполнители обновляют состояние папок/контейнеров и готовые удаления без поиска отсутствующих привязок. Разные темы могут обрабатываться параллельно в пределах настраиваемого лимита; внутри одной занятой темы канонические сообщения получают приоритет по `MESSAGE.created_at DESC`, при этом более старая работа со временем также продвигается.

## Публичные события и WebSocket

`stream.deleted` и затронутые `folder.updated`.

## Идемпотентность, гонки и видимые клиенту временные характеристики

Очистка по внешним ключам атомарна; повтор обработки надгробной записи аудитории безопасен. Доступ исчезает сразу, проекции и события сходятся асинхронно.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)
