# GET /api/workspace/v1/messenger/topic_summary_settings/{project_uuid}

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)

## Статус и граница текущего контракта

Целевая спецификация в подходе docs-first. HTTP-контракт остаётся текущим контрактом из [`workspace_api.md`](../../../../workspace_api.md); целевые внутренние механизмы являются только предложением.

![Диаграмма последовательности](diagrams/get_topic_summary_settings.svg)

Редактируемый исходник: [`get_topic_summary_settings.puml`](diagrams/get_topic_summary_settings.puml).

## Операция

**Метод и путь:** `GET /api/workspace/v1/messenger/topic_summary_settings/{project_uuid}`

**Назначение:** Прочитать глобальные и относящиеся к текущему проекту условия включения сводок.

## Публичный запрос

Путь: `project_uuid = 12345678-1234-4234-8234-123456789abc`; значение должно совпадать с проектом IAM; без тела.

## Успешный публичный ответ

HTTP `200`:

```json
{
  "project_id": "12345678-1234-4234-8234-123456789abc",
  "global_enabled": false,
  "project_enabled": false
}
```

Поля временных меток состояния и ошибок могут отсутствовать в ответе, если допускают `null` и имеют это значение. `api_key` и токен активной заявки никогда не возвращаются.

## Публичные ошибки

Требуется bearer-токен IAM. Некорректный UUID или тело запроса дают HTTP `400`; отсутствие разрешения на управление — `403`. Стандартное тело ошибки валидации:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

Если UUID в пути не совпадает с проектом IAM, возвращается `403`; GET требует членства в проекте, а PUT — разрешения на управление.

## Целевая граница RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceTopicSummarySettings(
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_topic_summary_settings"

    project_id = properties.property(types.UUID(), id_property=True, read_only=True)
    global_enabled = properties.property(types.Boolean(), default=False)
    project_enabled = properties.property(types.Boolean(), default=False)


class TopicSummarySettingsController(controllers.BaseResourceController):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceTopicSummarySettings,
        convert_underscore=False,
        process_filters=True,
    )
```

Публичное поле `project_id` — скалярное свойство UUID, а не отношение в форме URI. Физический индексированный внешний ключ на проект Workspace имеет явно заданное действие ссылочной целостности. UUID из пути должен совпадать с контекстом проекта IAM.

## Синхронный путь чтения

1. Потребовать документированное разрешение и область проекта.
2. Прочитать индексированные физические строки через стандартные объекты RestAlchemy.
3. Очистить поля учётных данных и заявки, затем сериализовать текущую публичную форму.
4. Не создавать записи transactional outbox, задачи, заявки фонового исполнителя, публичные события или работу WebSocket.

Это чтение не имеет побочных эффектов и не выполняет агрегирование или восстановление во время запроса.

[← Главный индекс документации](../../../../index.md) · [Индекс диаграмм последовательности](../../README.md) · [Раздел Core Messenger](../README.md)
