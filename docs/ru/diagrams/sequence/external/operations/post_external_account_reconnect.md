# Повторное подключение внешней учётной записи

[← Главный индекс документации](../../../../index.md) ·
[Индекс диаграмм последовательностей](../../README.md) ·
[Раздел внешней интеграции/runtime](../README.md)

`POST /api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke`

Проверить и заменить доступные только для записи учётные данные, затем возобновить синхронизацию.

![Диаграмма последовательностей](diagrams/post_external_account_reconnect.svg)

[Редактируемый исходник PlantUML](diagrams/post_external_account_reconnect.puml)

## Запрос

Дополнительных параметров запроса, кроме указанных выше переменных пути, нет.

Заголовки в дополнение к bearer-токену:

- `If-Match: "<revision>"` обязателен

```json
{
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only"
  }
}
```

## Успешный ответ

HTTP `200`:

```json
{
  "uuid": "0d4ae1d0-30ad-4d15-bf26-5789e8406201",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "00000000-0000-4000-8000-000000000001"
  },
  "credential_present": true,
  "status": "connecting",
  "live_ready": false,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 8,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z",
  "revision": 8
}
```

Ответы ресурсов с ревизией содержат строгий `ETag: "<revision>"`.

## Ошибки

| HTTP | Публичное поведение |
| --- | --- |
| `403` | Отсутствует разрешение `workspace.external_account.reconnect` или ресурс находится вне авторизованной области. |
| `404` | Ресурс в заданной области не существует или не виден. |
| `428` | Отсутствует `If-Match`. |
| `412` | Ревизия не совпадает. |
| `403` | Политика/состояние провайдера запрещает повторное подключение. |
| `400` | Для недопустимых значений пути, параметров запроса или тела используется стандартная ошибка валидации RESTAlchemy. |

Пример тела ошибки валидации:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## Граница RestAlchemy

Целевое объявление ресурса/контроллера (документация предложения, не производственный код):

```python
class ExternalAccount(models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True)
    settings = properties.property(EXTERNAL_ACCOUNT_SETTINGS_TYPE, required=True)
    status = properties.property(types.Enum(ACCOUNT_STATUSES), read_only=True)
    capabilities = properties.property(types.Dict(), read_only=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class ExternalAccountController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        ExternalAccount, hidden_fields=["owner_user_uuid"]
    )
```

`owner_user_uuid` скрыт; публичное `settings.default_project_id` является скалярным UUID-свойством. В целевом хранилище индексированный `owner_user_uuid` ссылается на пользователя Workspace с `ON DELETE CASCADE`, а извлечённый индексированный `default_project_uuid` — на реестр проектов с `ON DELETE RESTRICT`; при сериализации последний по-прежнему вложен в `settings`. Публичные объявления RestAlchemy не используют `relationships.relationship` для JSON в форме UUID, потому что отношение (relationship) сериализуется как URI. На границе физической схемы каждая каноническая неполиморфная связь `*_uuid` является индексированным внешним ключом с явно выбранным ссылочным действием. Санитайзеры скрывают владельца, учётные данные, сырой ID провайдера, закрытый сертификат, внутренний адрес и сырые поля протокола.

## Синхронная транзакция

1. Аутентифицировать запрос, определить область, проверить разрешение/тело и найти каноническую строку по индексированному ключу.
2. Валидировать новый Zulip credential против ожидаемых verified realm UUID,
   provider user ID и normalized `delivery_email`; любое несовпадение отклонить
   fail-closed до замены.
3. Заблокировать ревизию; зашифровать и заменить учётные данные; установить
   состояние `connecting`, ещё не готовое к live-работе; добавить желаемое
   состояние и неизменяемый outbox обновления; зафиксировать транзакцию.
4. Вернуть ответ после фиксации транзакции. Ошибка validation оставляет старый
   credential, connection, lease и sync работающими без изменений.

## Фоновая обработка, события и согласованность

Типизированная `delivery_snapshot_event` обслуживает exact external-account
scope; topic task отсутствует без placement. Доставка desired state провайдеру
является устойчивой работой control plane.

Фоновый handler в одной DB transaction фиксирует материализованное состояние и готовый конверт полного снимка `external_account.updated`; оба эффекта commit либо rollback вместе. После commit отдельный диспетчер WebSocket отправляет, повторяет и воспроизводит его; API/воркер не владеет соединениями клиентов.

Согласованность, видимая клиенту: замена учётных данных и желаемое поколение зафиксированы; проверка, обнаружение, применённое поколение и готовность к live-работе сходятся асинхронно.

Reconnect выполняет ровно тот же bootstrap, что connect: whole-account lease,
новая supported queue/boundary, sequential realtime и только затем history root.
Old queue/cursor не нужен durable recovery. Подробности:
[`zulip_bridge/coordination_and_recovery.md`](../../../../zulip_bridge/coordination_and_recovery.md).
Bridge выполняет private calls под действующим realm-bound mTLS certificate;
Workspace независимо проверяет current certificate/identity generation и
account lease. Неуспешная проверка нового Zulip `api_key` не меняет ни этот S2S
credential, ни старое account connection state.

## Идемпотентность и параллелизм

UUID учётной записи создаётся клиентом при создании; бизнес-уникальность допускает одну учётную запись `(owner_user_uuid, provider_kind)`. Шифротекст учётных данных хранится отдельно и никогда не сериализуется.

Повторы используют устойчивые бизнес-ключи и текущее исходное состояние. Каждое immutable outbox event создаёт отдельную task с уникальным `outbox_event_uuid`; повторная доставка этой task должна быть идемпотентной, coalescing отсутствует. Монопольная обработка темы Messenger от новых записей к старым применяется только тогда, когда затронутое каноническое размещение действительно относится к `(project_id, topic_uuid)`; операции администрирования/чтения провайдера не создают искусственную тему и не входят в эту очередь.

## Источники

- [`workspace_api.md`](../../../../workspace_api.md) — авторитетные публичные маршруты, общий JSON, пагинация, события и контракт WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — санитизированный жизненный цикл внешних ресурсов, разрешения и семантика провайдера.

[← Главный индекс документации](../../../../index.md) ·
[Индекс диаграмм последовательностей](../../README.md) ·
[Раздел внешней интеграции/runtime](../README.md)
