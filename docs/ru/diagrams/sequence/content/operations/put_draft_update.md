# `PUT /api/workspace/v1/messenger/drafts/{draft_uuid}`


Общий target-инвариант надёжности: каждое immutable outbox event выводит ровно одну immutable typed task с уникальным `outbox_event_uuid`; coalescing отсутствует. Task хранит фактический exact scope key, использует lease/fencing, retry/backoff, max attempts/DLQ, reaper и идемпотентный effect guard. Topic scope применяется только к placement/message-binding work; shared rows не получают неявный fallback на topic.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательностей](../../README.md) · [Раздел контента и пользователей Workspace](../README.md)

Статус: целевая спецификация операции, разработанная сначала в документации. Текущий публичный контракт
остаётся без изменений и является нормативным в [`workspace_api.md`](../../../../workspace_api.md).
Этот файл описывает целевые границы транзакций и проекций; это не
производственный код, миграции SQL или новая конечная точка.

![Диаграмма последовательности](diagrams/put_draft_update.svg)

[Редактируемый исходник PlantUML](diagrams/put_draft_update.puml)

## Назначение и публичный контракт

Заменить только полезную нагрузку Markdown черновика владельца с помощью оптимистической конкурентности.

Аутентификация: токен Bearer IAM; `project_id` и текущий `user_uuid` берутся из контекста IAM.

## Путь и параметры запроса

| Расположение | Имя | Тип / правило |
| --- | --- | --- |
| путь | `draft_uuid` | UUID |
| заголовок | `If-Match` | обязательная точная строгая ревизия, например `"1"` |

Пагинация коллекций, где она предусмотрена, сохраняет текущий контракт `page_limit` и UUID
`page_marker` и возвращает `X-Pagination-Limit`, а также
`X-Pagination-Marker` только при наличии следующей страницы.

## Тело запроса

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  }
}
```

## Успешный ответ

`200`

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  },
  "revision": 2,
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:01:00Z"
}
```

Заголовок ответа: `ETag: "2"`.

## Ошибки и авторизация

Принимается только `payload`. Отсутствующий `If-Match` возвращает `428`. Неверная/устаревшая ревизия возвращает `412` с текущим снимком черновика и ETag. Неверная полезная нагрузка `payload` возвращает `400`; недоступный черновик возвращается как не найден.

Общая форма ответа при ошибке валидации:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## Целевая граница RestAlchemy

```python
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceDraft(models.ModelWithUUID, models.ModelWithProject,
                     models.ModelWithTimestamp, orm.SQLStorableMixin):
    # Contract boundary only; target physical naming/decomposition is not selected.
    __tablename__ = "m_workspace_drafts"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(types.UUID(), required=True, read_only=True)
    payload = properties.property(types.Dict(), required=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class WorkspaceDraftController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceDraft,
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow overrides preserve owner scope, keyset marker, ETag and If-Match.
```

Каждая публичная ссылка на сущность объявляется скалярным UUID-свойством RestAlchemy, а не `relationship` (которое сериализовалось бы как URI). Соответствующий физический столбец `*_uuid` — индексированный внешний ключ с явно выбранным ссылочным действием. Поэтому публичный JSON сохраняет UUID без изменений.

Целевая внутренняя модель черновиков здесь намеренно не перерабатывается. Объявление фиксирует неизменную скалярную границу UUID/ETag. Физические UUID-столбцы пользователя/потока/темы остаются индексированными FK с каскадным поведением из текущего контракта; отношения RestAlchemy не должны менять публичный UUID JSON.

## Синхронный путь API

1. Разобрать точное значение `If-Match`.
2. Заблокировать черновик владельца и сравнить `revision`.
3. Заменить только `payload`, увеличить `revision` и обновить временную метку.
4. Добавить в outbox внутреннюю неизменяемую доменную запись черновика без публичной производной.
5. Зафиксировать транзакцию и вернуть новую строку/ETag.

## Outbox, типизированные задачи, воркер и работа в реальном времени

Проекция сообщений или счётчиков не планируется.

Внутреннее immutable outbox-событие выводит одну `delivery_snapshot_event`,
которая идемпотентно фиксирует отсутствие публичной производной и завершается;
готовая Workspace event row и WebSocket-доставка не создаются.

## Идемпотентность, ключи и гонки

Сравнение и обновление ревизии предотвращает потерю обновлений. Повтор с устаревшим ETag получает `412` и не может перезаписать более новое содержимое.

## Момент видимости для клиента

Клиент-инициатор немедленно видит зафиксированный черновик. Другие клиенты увидят его только после перезагрузки или явного повторного запроса черновиков; отправляемого обновления с согласованностью в конечном счёте нет.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательностей](../../README.md) · [Раздел контента и пользователей Workspace](../README.md)
