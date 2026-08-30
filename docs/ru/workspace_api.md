# Workspace v1 API

В этом документе описывается браузерный контракт API, составленный nginx из
сохраненный `workspace-messenger-api`, общий `workspace-api`, и
сопровождающий .`workspace-messenger-events`ОбщественныйMessenger
запросы используют выделенный процесс Messenger; общие пользователи, клиентская служба
настройки, регистрация устройств с подталкивающим устройством и использование REST событий `workspace-api`.
Самостоятельные почтовые, календарные и внешние пользователи конечные точки не являются частью этого
Нейтральный внешний аккаунт, чат, операция, политика, здоровье,
и ресурсы мостовых экземпляров являются частью Messenger API.

Нативные Messenger ресурсы, членство, состояние пользователя, события, отображение поставщиков,
и настройки клиента являются каноническими в PostgreSQL.
сохранить идентификацию провайдера без изменения браузера API.

## Точки входа в время выполнения

Прямые местные услуги:

```text
Messenger REST API:  http://127.0.0.1:21081/v1
Events WebSocket:    ws://127.0.0.1:21082/v1/events/ws
Workspace REST API:  http://127.0.0.1:21084/v1
Worker:              workspace-messenger-worker
Messenger OpenAPI:   http://127.0.0.1:21081/specifications/3.0.3
Workspace OpenAPI:   http://127.0.0.1:21084/specifications/3.0.3
```

Заднего конца nginx манифест выявляет эти внутренние шлюзы маршруты.
`workspace_ui` прокси-балансировщик нагрузки `/api/` к этому шлюзу без перезаписи
путь:

```text
Workspace REST root: /api/workspace/v1/...
Messenger REST:      /api/workspace/v1/messenger/...
Events REST:         /api/workspace/v1/events/...
Events WebSocket:    /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>
OpenAPI spec:        /api/workspace/specifications/3.0.3
```

`/api/workspace/v1/messenger/` проксиируется к сохраненному Messenger REST
обслуживание на `127.0.0.1:21081`; остаток `/api/workspace/` проксируется
служба Workspace REST на `127.0.0.1:21084`.
Точное местоположение nginx `/api/workspace/v1/events/ws` проксируется к
конечная точка службы websocket `/v1/events/ws` на `127.0.0.1:21082`.

Заднего конца nginx манифест устанавливает `client_max_body_size 50m` для прокси-запросов.
Он не обслуживает веб-интерфейс; не сопоставимый не-APIПути возвращаются .`404`.

## Общие правила {#general-rules}

- Тела запроса и ответа - JSON (`application/json`).
- Идентификаторы ресурсов - это UUID, если только поле не указывает иное.
- Временные метки - это UTC дата-времена, последовательно перечисленные как строки ISO-8601.
- REST аутентификация использует токен носителя Genesis IAM:

```http
Authorization: Bearer <token>
```

Чтобы получить токен в локальной тестовой среде, запросите его из Exordos Core IAM
через шлюз и использовать поле `access_token` от ответа:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=login%2Bpassword&
login=<test-user>&
password=<test-password>&
scope=openid+email+profile+project%3A<project-uuid>&
ttl=3600&
refresh_ttl=172800
```

Тот же запрос на токен также может быть отправлен как JSON:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "login+password",
  "login": "<test-user>",
  "password": "<test-password>",
  "scope": "openid email profile project:<project-uuid>",
  "ttl": 3600,
  "refresh_ttl": 172800
}
```

Клиент интерфейса использует клиент по умолчанию IAM.
`ttl=3600` означает, что токен доступа выдан на 1
`refresh_ttl=172800` означает, что токен обновления выдан на 2 дня.

Пример проверенного запроса:

```http
GET /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token from IAM response>
```

Чтобы обновить просроченный токен доступа, отправьте токен обновить на тот же по умолчанию
конечная точка клиента:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=refresh_token&
refresh_token=<refresh_token from IAM response>
```

JSON также принимается:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "<refresh_token from IAM response>"
}
```

Использовать новый `access_token` от ответа для последующего мессенджера API
Если ответ обновления включает новый `refresh_token`, заменить
хранится с токеном обновления.

`user_uuid` взято из IAM токенной информации. `project_id` взято из IAM
Интроспекция информации. Пользовательские ресурсы автоматически фильтруются и/or
запишите ток `user_uuid`.

Типичный ответ на ошибку RESTAlchemy/IAM:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

Тело ответа HTTP является самим объектом ошибки; нет внешнего `json`
Messenger ошибки проверки использования HTTP `400`.
операции предоставляют более конкретный код приложения в том же поле `code`:

| Код заявки | Тип | Операция |
| --- | --- | --- |
| `400001004` | `InvalidStreamBindingRoleError` | Добавление пользователей с не поддерживаемой обязательной ролью. |
| `400001005` | `StreamBindingUsersPayloadError` | Добавление пользователей с значением роли, которое не является списком UUID пользователей. |
| `400001006` | `InvalidTopicNotificationModeError` | Выберите режим уведомления о теме, несовместимый с режимом потока. |
| `400001007` | `StreamDefaultTopicNotConfiguredError` | Создать сообщение без `topic_uuid`, когда в потоке нет темы по умолчанию. |

Messenger ресурсы сохраняют канонический провиденции проекции вместо того, чтобы выставлять
идентификаторы перевозки:

```json
{
  "provider": {
    "kind": "zulip",
    "account_uuid": "account-uuid",
    "external_id": "provider-entity-id",
    "capabilities": {},
    "delivery_class": "live",
    "notification_eligible": true
  },
  "delivery": {
    "external_operation_uuid": "operation-uuid",
    "status": "pending",
    "safe_error": null,
    "can_retry": false,
    "can_discard": false,
    "duplicate_risk": false,
    "retry_requires_confirmation": false,
    "original_url": null,
    "reconciliation_reason": null,
    "updated_at": "2026-07-15T09:30:00.000000Z"
  }
}
```

`provider.capabilities` содержит эффективные описатели действий.
прогнозы поставщика `delivery_class` - `live` или `backfill`, и
`notification_eligible` замораживает, может ли сообщение уведомлять, когда
Заднего конца приняли его.
Открывается портал уведомления `false`; клиенты должны удалить рабочий стол
У местных ресурсов есть
`provider: null` и `delivery: null`. Провайдер синхронизирует курсоры, протокол
полезные нагрузки, учетные данные и состояние внутренней базы данных не являются частью
Контракт с пользователем.


Клиент браузера использует один и тот же IAM носитель токен и проект объем для каждого
Общественный сервер-открытие конечная точка
`GET /api/workspace/v1/messenger/server_settings`; это единственный
неавтентифицированная конечная точка Workspace, используемая интерфейсом пользователя.

Это публичный план, но нет совместимых псевдонима для
`/api/messenger/**`, `/api/v1/**`,
`/api/workspace/v1/messenger/events/**`, или бывший мессенджер веб-сокет
нет провайдера API, обращенного к браузеру. Независимо развернутый провайдер
время выполнения использует аутентифицированный частным мостом API с корнем в
`/api/workspace-provider/v1`; его операции обязывают обычные Messenger
ресурсов в PostgreSQL, так что это не меняет браузерный контракт, описанный
Частный договор определяется в
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

## Странирование и фильтры

Конечные точки сбора используют RESTAlchemy курсорную pagination:

| Параметр запроса | Тип | Описание |
| --- | --- | --- |
| `page_limit` | цельное число | Максимальное количество пунктов. `0` или пропущенное значение означает отсутствие ясного ограничения. |
| `page_marker` | UUID или цельное число | Маркер для следующей страницы. UUID ресурсы используют последнюю `uuid` предыдущей страницы; события используют последнюю `epoch_version` предыдущей страницы и требуют соответствия `epoch_generation` всякий раз, когда этот маркер не равен нулю. |

Если `page_limit` предоставлено, ответы включают `X-Pagination-Limit`.
страница существует, ответы также включают `X-Pagination-Marker`.

`GET /api/workspace/v1/messenger/messages/` использует стабильный композитный клавиатурный набор.
Установка `sort_key=created_at` и `sort_dir=asc` или `sort_dir=desc`; строки упорядочены
- По-моему ...`(created_at, uuid)`В этом направлении.`page_marker`остаетсяUUIDВ соответствии с
последний ряд возвращается для сохранения общедоступного контракта клиента.
что UUID внутри того же IAM проекта, взгляд аутентифицированного пользователя и сообщение
Маркер снаружи
`X-Pagination-Marker` выпускается только тогда, когда
`page_limit + 1` зонд доказывает, что еще один ряд существует, так что полная последняя страница делает
не рекламировать несуществующее продолжение.

`GET /api/workspace/v1/messenger/drafts/` использует тот же контракт с маркером UUID,
Приказ от`(updated_at, uuid)`- Настроен .`sort_key=updated_at`и
`sort_dir=asc|desc`; остаются дополнительные фильтры `stream_uuid` и `topic_uuid`
Маркер за пределами того же
возвращает объем `404`.

Workspace контроллеры сбора также поддерживают условные фильтры:

| Суффикс | Значение | Пример |
| --- | --- | --- |
| `>` | строго больше, чем | `epoch_version>123` |
| `<` | строго меньше | `epoch_version<123` |
| `=>` | больше или равно | `epoch_version=>123` |
| `=<` | меньше или равно | `epoch_version=<123` |

Когда имя параметра запроса содержит `>` или `<`, URL-кодировать его, если HTTP
Клиент не делает это автоматически:

```http
GET /api/workspace/v1/events/?epoch_version%3E=123&epoch_generation=781203&page_limit=500
```

Укладка страниц событий и повторное подключение с использованием пары курсоров
`(epoch_generation, epoch_version)`, не только эпохальное число.
`0` может пропустить `epoch_generation`. Если суффикс события, сохраненный, больше не
начинается в эпохе `1`, что холодный запрос возвращает тот же HTTP 410 ответ разрыв
как любой другой курсор, который не может произвести полную дельту; клиент должен загрузить
авторитетные снимки перед началом нового курсора.

## Резюме конечного результата {#endpoint-summary}

| Метод | Путь | Описание |
| --- | --- | --- |
| `GET` | `/api/workspace/v1/` | Перечислите маршруты ниже `/api/workspace/v1/`. |
| `GET` | `/api/workspace/v1/messenger/` | Перечислить маршруты Messenger ниже `/api/workspace/v1/messenger/`. |
| `GET` | `/api/workspace/v1/messenger/server_settings` | Верните настройки сервера типа Zulip. |
| `GET` | `/api/workspace/v1/messenger/server_settings/` | То же самое, что и выше; поддерживается запятая. |
| `GET` | `/api/workspace/v1/messenger/folders/` | Список папок для текущего пользователя IAM. |
| `POST` | `/api/workspace/v1/messenger/folders/` | Создайте папку. |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Возьми папку. |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Обновляйте папку. |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Удалить папку. |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | Список элементов папки для текущего пользователя IAM. |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | Создать элемент папки. |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Возьми бумажник. |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Удалить элемент папки. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | Закрепите заголовок папки. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | Развяжи папку. |
| `GET` | `/api/workspace/v1/messenger/streams/` | Список потоков, видимых для текущего пользователя IAM. |
| `POST` | `/api/workspace/v1/messenger/streams/` | Создайте поток. |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Подойди к потоку. |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Обновляем потоковую передачу. |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Удалить потоки для всех пользователей потоков. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` | Добавление пользователей в поток по ролям. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` | Установка `is_archived: true`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` | Установка `is_archived: false`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` | Установите режим уведомления потока текущего пользователя. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` | Отметьте все нечитаемые сообщения потока как прочитаные для текущего пользователя. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` | Список связей потока. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Сделайте привязку. |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Обновление связывания потока. |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Удалить пользователя из потока. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` | Список тем, видимых для текущего пользователя IAM. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` | Создайте тему. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Найди тему. |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Переименовать тему; тело должно содержать `name`. |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Удалить тему. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` | Переключите флаг `is_done` для всех пользователей темы. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` | Установите режим уведомления о темах текущего пользователя. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` | Сделайте тему по умолчанию темой потока. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` | Владелец обновления/administrator-managedКонфигурация обобщения по теме, включая enable/disable. |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | Перечислите глобальные конечные точки резюме, совместимые с OpenAI; требуется `workspace.topic_summary_endpoint.manage`. |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | Создать глобальную конечную точку с учетным данным для записи; требуется `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | Читать, обновлять или удалять конечную точку глобального резюме; требуется `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` | Прочитайте оба резюме или обновите оба с помощью `workspace.topic_summary_settings.manage`. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` | Отметьте все нечитаемые сообщения темы как прочитаные для текущего пользователя. |
| `GET` | `/api/workspace/v1/messenger/messages/` | Перечислите сообщения, видимые текущему пользователю IAM. |
| `POST` | `/api/workspace/v1/messenger/messages/` | Создать сообщение. |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Передай сообщение. |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Обновление полезной нагрузки сообщения. |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Удалить сообщение. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` | Заметьте сообщение как прочитано для текущего пользователя. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` | Отметьте нечитаемые сообщения в той же теме до этого сообщения как прочитаные. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` | Звездное сообщение для текущего пользователя. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` | Сообщение отключения звезды для текущего пользователя. |
| `GET` | `/api/workspace/v1/messenger/drafts/` | Перечислите проекты текущего пользователя, опционально отфильтрованные по потоку или теме. |
| `POST` | `/api/workspace/v1/messenger/drafts/` | Создать черновик с помощью генерации клиента UUID. |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Получить собственный проект и его сильный пересмотр ETag. |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Заменить только полезную нагрузку Markdown с помощью `If-Match`. |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Строго удалить собственный проект с помощью `If-Match`. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/` | Перечислите внешние учетные записи текущего пользователя в области глобального значения; требуется `workspace.external_account.read`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/` | Создать внешнюю учетную запись с генерируемым клиентом UUID и учетными данными только для записи; требуется `workspace.external_account.create`. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Получить очищенный внешний снимок аккаунта владельца; требует `workspace.external_account.read`. |
| `PUT` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Заменить изменяемые несекретные настройки с помощью `If-Match`; требует `workspace.external_account.update`. |
| `DELETE` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Удалить счет и его проекцию; требуется `workspace.external_account.delete`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke` | Проверьте и замените учетные данные только для записи, затем возобновите синхронизацию; требуется `workspace.external_account.reconnect`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/disconnect/invoke` | Остановить синхронизацию при сохранении проекции только для чтения; требует `workspace.external_account.disconnect`. |
| `GET` | `/api/workspace/v1/messenger/external_chats/` | Перечислите очищенный каталог внешнего чата владельца и состояние назначения. |
| `GET` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}` | Сделайте одно очищенное внешнее чат-снимки. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/select/invoke` | Выберите чат и назначите его проекту. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/deselect/invoke` | Отменить работу и удалить видео. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/move/invoke` | Атомно переместить проекцию в другой проект с помощью `If-Match`. |
| `GET` | `/api/workspace/v1/messenger/external_operations/` | Перечислите внешние операции владельца. |
| `GET` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | Очистите стан работы. |
| `DELETE` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | Отбросить работу, которая подлежит разрешению. |
| `POST` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}/actions/retry/invoke` | Попробуйте еще раз, если не удалось. |
| `POST` | `/api/workspace/v1/messenger/external_operations/actions/preflight/invoke` | Проверьте способность и потерю преобразования перед выходом мутации. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/` | Список очищенных экземпляров моста; требует разрешения на чтение IAM. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}` | Очистите мостик от инфекций, здоровья, возможностей и состояния сертификата. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | Отменить идентификацию моста. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/resume/invoke` | Возобновить неотмененную идентификацию моста. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/revoke/invoke` | Отменить генерацию активного сертификата моста. |
| `GET` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | Прочитайте правила очистки для поставщиков. |
| `PUT` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | Обновление политики провайдера с использованием `If-Match` и выделенного разрешения IAM. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/suspend/invoke` | Отменить поставщика по всему миру. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/resume/invoke` | Возобновить работу после проверки. |
| `GET` | `/api/workspace/v1/messenger/external_provider_health/{kind}` | Читайте "Здоровье поставщиков". |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` | Список реакций на сообщения, видимые текущему пользователю IAM. |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` | Создать реакцию на сообщение. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Получить реакцию сообщения видимой через доступ к сообщению. |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Обновляйте реакцию текущего пользователя. |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Удалить реакцию текущего пользователя. |
| `GET` | `/api/workspace/v1/messenger/files/` | Список файлов, видимых для текущего пользователя IAM. |
| `POST` | `/api/workspace/v1/messenger/files/` | Создать метаданные файлов или загрузить многочастичные файлы. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | Получите видимую запись метаданных файла. |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | Обновление записи метаданных файла. |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | Удалить файл собственности и его строки доступа. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | Загрузить видимые байты файлов. |
| `GET` | `/api/workspace/v1/services/` | Перечень доступных услуг Workspace. |
| `GET` | `/api/workspace/v1/services/{service_uuid}` | Получить одну доступную услугу Workspace. |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` | Идеально регистрируйте или поворачивайте толкающее устройство текущего пользователя. |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` | Идеально удалить регистрацию устройства push текущего пользователя. |
| `GET` | `/api/workspace/v1/events/` | Перечислите длительные события в реальном времени для текущего пользователя IAM. |
| `GET` | `/api/workspace/v1/epoch/` | Вернуть последнюю эпоху видимого события текущего пользователя. |
| `GET` | `/api/workspace/v1/users/` | Список пользователей рабочей зоны. |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | Найдите пользователя рабочей зоны. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | Обновление состояния присутствия текущего пользователя и времени сердцебиения. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | Загрузить и выбрать аватара текущего пользователя. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | Удалить пользовательский аватара текущего пользователя и восстановить канонический Gravatar URN. |
| `GET` | `/api/workspace/v1/me/` | Возвращение текущего аутентифицированного пользователя Workspace. |

### Граница договора внешней интеграции

В таблице с конечными показателями выше представлена каноническая инвентаризация текущих
IAM- аутентифицированные маршруты браузера.OpenAPIявляется авторитетным для
схемы запросов и ответов операций, поддерживаемых контроллером HTTP, при условии
В следующей статье мы рассмотрим, как использовать
`server_settings` middleware aliases и события WebSocket являются входом в время выполнения
точки, задокументированные в данном файле, но не генерируемые операцией OpenAPI.

Настройки внешних учетных записей, метаданные источника чата и использование операционных данных
Zulip является первым зарегистрированным типом; добавление другого типа
Не добавляет конкретных маршрутов сбора.
Примеры, правила ETag и `If-Match`, разрешения на действия, семантика жизненного цикла,
и поведение администратора для учетных записей, чатов, операций, мостовых экземпляров,
Политики и правила здравоохранения поставщиков определены в разделах 5 и 6
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).
Его частный контроль, провайдер данных-плана, и файловых передач разделов описывают
контракты с использованием серверов backend-to-bridge и не являются частью общедоступного браузера API.

## Настройки сервера {#server-settings}

`GET /api/workspace/v1/messenger/server_settings` является публичным и не требует `Authorization`.
Не поддерживается
параметры запроса сообщаются в
`ignored_parameters_unsupported`. `realm_url` и `realm_uri` используют запрос
`Host` заголовок и по умолчанию к общественности HTTPS схема. Доверенный обратный прокси
может явно предоставить `X-Forwarded-Proto`; упакованный Workspace nginx
конфигурация устанавливает его на `https`, потому что TLS завершается до внутреннего
HTTP hop. `realm_icon` использует `urn:url:<https-url>` для анонимного
URN и используйте его HTTPS URL
Значение происходит из канонического области запроса как
`urn:url:<realm>/logo-512x512.png`; nginx обслуживает этот путь от упакованного
512×512 эмблема организации.

Пример ответа:

```json
{
  "result": "success",
  "msg": "Welcome to Exordos Workspace",
  "authentication_methods": {
    "password": true,
    "dev": false,
    "email": true,
    "ldap": false,
    "remoteuser": false,
    "github": false,
    "azuread": false,
    "gitlab": false,
    "google": false,
    "apple": false,
    "saml": false,
    "openid connect": false
  },
  "push_notifications_enabled": true,
  "email_auth_enabled": true,
  "require_email_format_usernames": true,
  "realm_url": "https://workspace.example.com",
  "realm_name": "Exordos Workspace",
  "realm_icon": "urn:url:https://workspace.example.com/logo-512x512.png",
  "realm_description": "<p>Exordos Workspace messenger.</p>",
  "realm_web_public_access_enabled": false,
  "meet_url": "https://meet.genesis-core.tech",
  "external_authentication_methods": [],
  "realm_uri": "https://workspace.example.com"
}
```

## Устройства толкания {#push-devices}

`PUT /api/workspace/v1/push_devices/{registration_uuid}` - это стиль замены
Клиент генерирует стабильный UUID на установку приложения.
первая регистрация возвращает `201`; заменяет свой FCM токен или ключ шифрования
возвращается`200`Регистрация всегда охватывает как аутентифицированные
`user_uuid` и IAM `project_id`.

```json
{
  "transport": "fcm",
  "platform": "ios",
  "registration_token": "<FCM registration token>",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  }
}
```

`encryption`Это модель типа RESTAlchemy.`HPKE`,
используя базовый режим с X25519, HKDF-SHA256 и AES-256-GCM. `public_key` должен быть
Каноническое не забитое кодирование base64url точно 32 байта.
API версии, ответ отражает `registration_token` и `public_key` от
поддерживаемые платформы в настоящее время `android` и `ios`.

```json
{
  "uuid": "7c1af344-95e1-487e-8b51-d1af0370cdb5",
  "transport": "fcm",
  "platform": "ios",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "registration_token": "<FCM registration token>",
  "created_at": "2026-07-26T05:30:00Z",
  "updated_at": "2026-07-26T05:40:00Z"
}
```

`DELETE` возвращает `204` как при удалении собственной регистрации, так и при этом
Этот контракт управляет только регистрациями;
шифрование и доставка полезной нагрузки не входят в эту API изменение.

## Папки {#folders}

`POST /api/workspace/v1/messenger/folders/` пишет на `m_folders`. читает с помощью `m_folders_view`.
Ответы скрывают `project_id` и `user_uuid`.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор папки. |
| `title` | Строка, 1,64 | Да, я знаю. | Нет | Заголовок папки. |
| `background_color_value` | цельное число `0..2^32-1` или `null` | Нет | Нет | ARGB цвет. |
| `unread_count` | цельное число | Нет | Да, я знаю. | Совокупное количество активных нечитаемых сообщений. |
| `system_type` | `all`, `created` или `null` | Нет | Да, я знаю. | Тип системной папки; по умолчанию `created`. |
| `folder_items` | массив | Нет | Да, я знаю. | Вложенные элементы папки из вида. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Создать запрос:

```json
{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Пример:

```http
POST /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Пример ответа:

```json
{
  "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "title": "Inbox",
  "background_color_value": 4280391411,
  "unread_count": 3,
  "system_type": "created",
  "folder_items": [
    {
      "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "chat_type": "stream",
      "order_index": 10,
      "pinned_at": null,
      "unread_count": 3,
      "active_unread_count": 3,
      "passive_unread_count": 0,
      "created_at": "2026-06-22T09:30:00Z",
      "updated_at": "2026-06-22T09:30:00Z"
    }
  ],
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Пример обновления:

```http
PUT /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Archive",
  "background_color_value": 4289352960
}
```

Удалить пример:

```http
DELETE /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
```

Побочные эффекты в режиме реального времени:

| Операция | payload.kind | object_type | Полезная нагрузка |
| --- | --- | --- | --- |
| создать папку | `folder.created` | `folder` | Полный портрет. |
| папка обновления | `folder.updated` | `folder` | Полный портрет. |
| Удалить папку | `folder.deleted` | `folder` | Только `folder.uuid`. |

## Имеется в наличии: {#folder-items}

`POST /api/workspace/v1/messenger/folder_items/` пишет на `m_folder_items`.
`m_folder_items_created_view`.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор элемента папки. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `folder_uuid` | UUID | Да, я знаю. | Нет | Папка UUID. |
| `user_uuid` | UUID | Нет | Да, я знаю. | IAM пользовательский объем. |
| `stream_uuid` | UUID | Да, я знаю. | Нет | Поток UUID. |
| `chat_type` | `stream`, `group`, `private` | Да, я знаю. | Нет | - Он типа чат. |
| `order_index` | цельное число или `null` | Нет | Нет | Индекс ручной сортировки. |
| `pinned_at` | дата-время или `null` | Нет | управляемые действиями | Напишите дату. |
| `unread_count` | цельное число | Нет | Да, я знаю. | Чистое количество нечитаемых данных для этого потока и пользователя. |
| `active_unread_count` | цельное число | Нет | Да, я знаю. | Нечитаемые сообщения, допустимые в режимах уведомления эффективного потока/topic. |
| `passive_unread_count` | цельное число | Нет | Да, я знаю. | Остальные нечитаемые сообщения от заглушенного сообщения. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Создать запрос:

```json
{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Создайте пример:

```http
POST /api/workspace/v1/messenger/folder_items/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Пример ответа:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": null,
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Печень и разщепление возвращают ту же форму элемента папки. `pin` настраивает `pinned_at` на
текущее время UTC; `unpin` устанавливает его на `null`.

Пример пин:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/pin/invoke
Authorization: Bearer <access_token>
```

Пример ответа на пин:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": "2026-06-22T09:31:00Z",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```

Пример развязки:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/unpin/invoke
Authorization: Bearer <access_token>
```

Удалить пример:

```http
DELETE /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50
Authorization: Bearer <access_token>
```

Побочные эффекты в режиме реального времени:

| Операция | payload.kind | object_type | Полезная нагрузка |
| --- | --- | --- | --- |
| Добавить поток в папку | `folder.updated` | `folder` | Полный снимок родительской папки с помощью `folder_items`. |
| Пин-поток в папке | `folder.updated` | `folder` | Полный снимок родительской папки с обновленным `pinned_at`. |
| Отключить потоки в папке | `folder.updated` | `folder` | Полный снимок родительской папки с помощью `pinned_at: null`. |
| удалить поток из папки | `folder_item.deleted` | `folder_item` | Только `folder_item.uuid`. |

## Сток

`POST /api/workspace/v1/messenger/streams/` загружает канонический поток,
PostgreSQLЭто создает
тема по умолчанию называется `General Topic` и хранит ее UUID как
`default_topic_uuid`.
Ссылка является нулевой и становится `null`, когда текущая тема по умолчанию
REST ресурс ответы следуют стандартному RestAlchemy JSON упаковщик
и пропустить нулевые поля, значение которых `null`, поэтому клиенты также должны обрабатывать
пропал .`default_topic_uuid`Как`null`Прочный .`stream.updated`События завершены .
Снимок и сохранить `default_topic_uuid: null` явно.

Если `direct_user_uuid` предоставлено, бэкэнд создает обычный поток с
те же правила связей, ролей, тем, событий и файлов ACL, что и у всех остальных
Его единственные дополнительные инварианты `private: true`, детерминированный
потока, охватываемого проектом UUID для неорганизованной пары идентичности, и `owner`
Обычный прямой чат имеет два
В саморазговоре используется повторяющаяся пара `(user, user)`,
содержит точно одно связывание для текущего пользователя, и возвращает текущего пользователя
UUID в `direct_user_uuid`. Повторяющийся или одновременный отправка одного и того же запроса
для одной пары возвращает существующий поток.
источник или прямые поля идентификации возвращает HTTP `400` вместо изменения или
молча игнорируя запрошенную личность.

Поддерживаемые источники полезной нагрузки:

```json
{
  "source_name": "native",
  "source": {
    "kind": "native"
  }
}
```

```json
{
  "source_name": "zulip",
  "source": {
    "kind": "zulip",
    "stream_id": 123,
    "server_url": "https://zulip.example.com",
    "topic_name": null,
    "message_id": null
  }
}
```

- Это ...`zulip`Форма полезной нагрузки - это место происхождения поставщика.Zulipвремя выполнения
заполняет его через частного Провайдера HTTP API; браузер контракт скрывает
Идентификаторы протокола поставщика, учетные данные и состояние синхронизации.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор потока. |
| `name` | Строка, максимум 255 | Да, я знаю. | Нет | Имя потока. |
| `description` | Строка, максимум 255 | Нет | Нет | Описание потока; по умолчанию пустая строка. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `owner` | UUID | Нет | Да, я знаю. | Владелец из вида потока пользователя. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Текущий пользователь в просмотре потока пользователей. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | Нет | Да, я знаю. | Роль текущего пользователя. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | Нет | управляемые действиями пользователя | Режим уведомления потока текущего пользователя; по умолчанию `all_messages`. |
| `unread_count` | цельное число | Нет | Да, я знаю. | Чистое количество нечитаемых данных текущего пользователя. |
| `active_unread_count` | цельное число | Нет | Да, я знаю. | Количество нечитаемых сообщений текущего пользователя, подлежащих уведомлению в режиме эффективного потока/topic. |
| `passive_unread_count` | цельное число | Нет | Да, я знаю. | Остальное количество нечитаемых сообщений текущего пользователя от подавленного трафика уведомлений. |
| `source_name` | `native`, `zulip` | Нет | Нет | Имя источника; по умолчанию `native`. |
| `source` | объект | Нет | Нет | Источник полезной нагрузки; по умолчанию `{"kind": "native"}`. |
| `invite_only` | булевой | Нет | Нет | Флаг потока только для приглашенных. |
| `announce` | булевой | Нет | Нет | Объявление флага потока. |
| `direct_user_uuid` | UUID | Нет | Нет | Соответствие прямого чата. Равно текущему пользователю UUID только для самостоятельного чата. |
| `private` | булевой | Нет | Да, я знаю. | Частный флаг. |
| `is_archived` | булевой | Нет | управляемые действиями | Архивированный флаг. |
| `color` | цельное число `0..0xFFFFFF` | Нет | Нет | Цвет потока; генерируется случайным образом при пропуске или `null`. |
| `last_message_uuid` | UUID или `null` | Нет | Да, я знаю. | Последнее сообщение в потоке, или `null` когда пусто. |
| `default_topic_uuid` | UUID или `null` | Нет | Да, я знаю. | Текущая тема по умолчанию UUID, или `null`, если не настроена по умолчанию. |
| `provider` | объект или `null` | Нет | Да, я знаю. | Значок поставщика для потоков, поддерживаемых поставщиком; `null` для потоков, которые являются нативными. |
| `delivery` | объект или `null` | Нет | Да, я знаю. | Текущий провайдер команды доставки прогноз. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Создать запрос:

```json
{
  "name": "Engineering",
  "description": "Engineering workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false
}
```

Создание запроса прямого чата:

```json
{
  "name": "Direct",
  "description": "Private workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "33333333-3333-3333-3333-333333333333"
}
```

Создать запрос на саморазговор:

```json
{
  "name": "Personal notes",
  "description": "",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "11111111-1111-1111-1111-111111111111"
}
```

Для саморазговора `direct_user_uuid` должно быть равно текущему пользователю IAM UUID,
Включая токен-предмет UUID.
стандартный ресурс потока с `private: true`, одним текущим пользователем `owner`
связывание, и тот же текущий пользователь UUID в `direct_user_uuid`; нет отдельного чата
Это делает
`private && direct_user_uuid == current_user_uuid` стабильная сторона клиента
проверка идентичности при сохранении обычных потоков частной группы, чьи
`direct_user_uuid` остается `null`.

Прямое членство является неизменным.
Пары идентификации: один связывающий для саморазговора и два для обычного прямого
Добавление или удаление участников и обновление обязательных возвратов ролей HTTP
`400`Удаление потока чат-сообщений также возвращаетсяHTTP `400`Так что история сообщений
не может быть заменена путем удаления и воссоздания детерминированной идентичности.
`source_name` должен соответствовать `source.kind` при создании потока.
Поля неизменны для каждого потока.`direct_user_uuid`,
`private`, и внутренний `private_index` также неизменны; попытки
Изменить любое из этих полей идентификации возвращает HTTP `400`.

Запрос на режим уведомления потока:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/notifications/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "notification_mode": "mentions_only"
}
```

Мутации в родном потоке обновляют каноническое состояние PostgreSQL и их реальное время
Неоправданные`provider`и
Поля `delivery` описывают внешнее проекционное и рабочее состояние; оба поля
`null` для родных потоков.

Действие чтения потока:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` отмечает все нечитаемые сообщения в потоке как прочитаные для текущего пользователя и
возвращает обновленный вид потока.

Побочные эффекты в режиме реального времени:

| Операция | payload.kind | object_type | Полезная нагрузка |
| --- | --- | --- | --- |
| Создать поток | `stream.created` | `stream` | Полный снимок потока пользователей. |
| Создать поток | `folder.updated` | `folder` | Обновлены `All chats` и `Channels`/`Personal` снимки системной папки. |
| потока обновления | `stream.updated` | `stream` | Полный снимок потока пользователей для каждого пользователя потока. |
| архивировать или не архивировать потоки | `stream.updated` | `stream` | Полный снимок потока пользователей для каждого пользователя потока. |
| режим уведомления о смене потока | `stream.updated` | `stream` | Полный снимок потока пользователей только для текущего пользователя. |
| читать сообщения потока | `stream.read` | `stream` | Полный снимок потока пользователя, возвращенный действием. |
| читать сообщения потока | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Обновлены нечитаемые снимки текущего пользователя. |
| Удалить поток | `stream.deleted` | `stream` | Только удаленный поток `uuid`, отправленный каждому пользователю потока. |
| Удалить поток | `folder.updated` | `folder` | Обновленные снимки папки системы /custom затронутых пользователей после удаления потока. |
| Добавить связывание потока | `stream.created` | `stream` | Добавлено полное изображение потока пользователей. |
| Добавить связки потока | `stream_bindings.created` | `stream_binding` | Новые снятые с потока снимки для существующих участников потока. |
| Добавить связывание потока | `folder.updated` | `folder` | Обновлено добавлено пользовательские `All chats` и `Channels`/`Personal` системные папки. |
| Удалить связывание потока | `stream.deleted` | `stream` | Только потоки `uuid`, отправленные удаленному пользователю. |
| Удалить связывание потока | `stream_binding.deleted` | `stream_binding` | Удаление связывания `uuid`, `stream_uuid` и `user_uuid`, отправленные каждому оставшемуся участнику потока. |
| Удалить связывание потока | `folder.updated` | `folder` | Обновленные удалённые пользовательские системные/custom папки после удаления доступа. |

Для прямых частных потоков, один `stream.created` событие записывается для каждого
Создание потока также записывает `folder.updated` события для каждого
папка участника `All chats` и для `Personal`, когда поток является частным,
или `Channels`, когда это не частный код.

## Связки потока

Связки потока - это канонические записи членства в чате PostgreSQL.
создаются через
`POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke`, где
группы запроса тела добавлены пользователей по роли. `who_uuid` всегда переписывается.
с текущим пользователем IAM UUID.
При создании новой связи добавленный пользователь получает `stream.created`
событие для нового видимого потока и события `folder.updated` для `All chats`
и либо `Personal` или `Channels`, в зависимости от конфиденциальности потока.
Участники потока получают одно событие `stream_bindings.created`, содержащее
новые связывающие снимки для всей добавленной партии.
связь создается видима для нового члена с `read=true`, так что и
Счетчики потока и темы начинаются с нуля.
объединения не читаются, пока новый член не прочитает их.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Обязательный идентификатор. |
| `project_id` | UUID | Да, я знаю. | Нет | Объем проекта. |
| `stream_uuid` | UUID | Да, я знаю. | Нет | Поток UUID. |
| `user_uuid` | UUID | Да, я знаю. | Нет | Пользователь получает доступ. |
| `who_uuid` | UUID | Нет | Да, я знаю. | Пользователь, который выполнял действие. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | Нет | Нет | Роль; по умолчанию `member`. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | Нет | Нет | Режим уведомления пользователя о потоке; по умолчанию `all_messages`. |
| `notification_updated_at` | дата и время | Нет | Нет | Последний запись-выигрыш с временной меткой`notification_mode`; по умолчанию на эпоху Unix, и действие уведомления устанавливает его на текущее время сервера.RESTи снятые в режиме реального времени. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Добавить запрос пользователя:

```json
{
  "member": [
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444"
  ],
  "owner": [
    "55555555-5555-5555-5555-555555555555"
  ]
}
```

Удаление связывания устраняет доступ этого пользователя к потоку.
получает `stream.deleted` и затем `folder.updated` для пораженной системы и
Каждый оставшийся участник потока получает
`stream_binding.deleted` с удаленным связыванием `uuid`, `stream_uuid` и
`user_uuid`. Для потока, поддерживаемого провайдером, добавление и удаление связей также
прочный, ограниченный возможностями `membership.add` и `membership.remove`
Мост поставщика решает отображаемую идентификацию поставщика и
подписывается или отключается; нативные потоки не выполняют операцию провайдера.

## Темы потока

`POST /api/workspace/v1/messenger/stream_topics/` заставляет каноническую тему,
его флаги на пользователя и побочные эффекты в режиме реального времени в PostgreSQL.
текущему пользователю IAM через текущее членство в потоке.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор темы. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `name` | Струна, максимум 128 | Да, я знаю. | Нет | Имя темы. |
| `stream_uuid` | UUID | Да, я знаю. | Нет | Поток UUID. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Текущий пользователь в виде темы. |
| `color` | цельное число `0..0xFFFFFF` | Нет | Нет | Цвет темы; генерируется случайным образом при пропуске или `null`. |
| `last_message_uuid` | UUID или `null` | Нет | Да, я знаю. | Последнее сообщение в теме, или `null` когда пусто. |
| `unread_count` | цельное число | Нет | Да, я знаю. | Чистое количество не прочитанных данных текущего пользователя для темы. |
| `active_unread_count` | цельное число | Нет | Да, я знаю. | Нечитаемые упоминания текущего пользователя для `unmute`, все нечитаемые для `follow` или унаследованный активный счет для `default`. |
| `passive_unread_count` | цельное число | Нет | Да, я знаю. | Количество нечитаемых сообщений текущего пользователя после применения эффективного режима уведомления. |
| `is_default` | булевой | Нет | Да, я знаю. | Равная ли эта точка UUID с `default_topic_uuid` потока. |
| `is_done` | булевой | Нет | управляемые действиями | Флаг текущего пользователя. |
| `notification_mode` | `mute`, `default`, `unmute`, `follow` | Нет | управляемые действиями пользователя | Режим уведомления о темах текущего пользователя; по умолчанию `default`. |
| `summary` | string, max 4096, или `null` | Нет | Да, я знаю. | Последнее резюме, созданное LLM, написано агентом по резюме на стороне сервера. |
| `summary_last_message_uuid` | UUID или `null` | Нет | Да, я знаю. | Последнее сообщение темы, фактически включенное в `summary`; написанное серверным агентом резюме, и `null` действителен для пустой темы. |
| `summary_has_new_messages` | Булева или `null` | Нет | Да, я знаю. | `null` без резюме; в противном случае, отличается ли текущее последнее сообщение от `summary_last_message_uuid`. |
| `summary_enabled` | булевой | Нет | управляемые действиями | Может ли рабочий на стороне сервера обновлять эту тему; по умолчанию `true`. |
| `summary_system_prompt` | string, max 16384, или `null` | Нет | управляемые действиями | Тематический LLM системный запрос; `null` выбирает приложение по умолчанию. |
| `summary_reasoning_effort` | `off`, `minimal`, `low`, `medium`, `high` или `null` | Нет | управляемые действиями | Выбор обобщенного рассуждения; `off` явно отключает рассуждение, в то время как `null` исключает опцию провайдера. Используется только тогда, когда выбранная конечная точка заявляет поддержку рассуждения. |
| `source_name` | `native`, `zulip` | Нет | Нет | Имя источника темы; по умолчанию `native`, если оно пропущено. |
| `source` | объект | Нет | Нет | Источник цели. |
| `provider` | объект или `null` | Нет | Да, я знаю. | Значок поставщика для тем, поддерживаемых поставщиком; `null` для местных тем. |
| `delivery` | объект или `null` | Нет | Да, я знаю. | Текущий провайдер команды доставки прогноз. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Когда `summary_system_prompt` равен `null`, приложение по умолчанию запрашивает
краткое резюме, в котором содержится информация о решениях, владельцах, нерешенных вопросах и
важные ограничения, написанные на первичном языке, используемом в теме.

Создать запрос:

```json
{
  "name": "Releases",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
}
```

`PUT /api/workspace/v1/messenger/stream_topics/{topic_uuid}` требует тела с `name`.
проверяет, что текущий пользователь имеет связь с потоком темы до
Native changes update canonical PostgreSQL state и их переименование
Происхождение остается неизменным при переименовании.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` переворачивается
`is_done` для всех пользователей темы и возвращает обновленный вид темы текущего пользователя.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` устанавливает
topic как по умолчанию потока и возвращает обновленную тему текущего пользователя
Операция является идемпотентной. Измененный по умолчанию выпускает `stream.updated`
для каждого пользователя потока и `topic.updated` для предыдущего и нового по умолчанию
темы.

Резюме темы пишут только агент резюме на стороне сервера через
внутренний помощник; нет публичного REST действия для записи `summary` или
`summary_last_message_uuid`. Помощник хранит оба поля атомно,
подтверждает, что ненулевая граница идентифицирует сообщение в теме, отвергает
старый границы, когда более новый уже хранится, и излучает
`topic.updated` снимки для участников потока.
также прилагает запись в частном журнале на стороне сервера, содержащую резюме,
Если покрытие не является обязательным, то это может быть использовано для определения границы UUID и заказывания временной метки, и генерации временной метки.
сообщение удалено, записи журнала в сообщении или после него удаляются
недействительны, новейшая ранее записанная запись восстанавливается (или резюме удаляется),
Старый рабочий рабочий отбрасывается, и восстановленный снимок излучается в
Та же сделка.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke`
обновляет конфигурацию резюме по конкретной теме:

```json
{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "medium",
  "summary_enabled": true
}
```

Настройка `summary_system_prompt` на `null` восстанавливает приложение по умолчанию.
Каждое поле является необязательным, но запрос должен содержать по крайней мере одно поле.
Опущение поля сохраняет его текущее значение.
`summary_reasoning_effort` как `null` очищает запрос на обоснование.
Установка этого запроса на конфигурацию
в `off` отправляет значение поставщика, совместимого с OpenAI `none`, явно
В этом случае, если вы не можете использовать данные, вы можете использовать их для определения
`summary_enabled` до `false` отменяет ожидаемую работу по этой теме и предотвращает
новые требования при сохранении текущего резюме; установка его обратно на `true`
позволяет работнику обновлять устаревший контент.
Только владельцы потока и администраторы могут обновлять эту конфигурацию; другие роли,
включая модераторов, принимать `403 Forbidden`.

### Резюме темы рабочий процесс клиента

Клиент читает резюме как часть обычного тематического снимка:

```http
GET /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047
Authorization: Bearer <access_token>
```

```json
{
  "uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "last_message_uuid": "b5ff6f76-bcfe-4fb9-9c28-e0cb790d2e52",
  "summary": "The team approved the release scope; two follow-ups remain open.",
  "summary_last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "summary_has_new_messages": true,
  "summary_enabled": true,
  "summary_system_prompt": "Summarize decisions and open questions.",
  "summary_reasoning_effort": "medium"
}
```

Интерфейс пользователя отображает `summary` и может помечать его как устаревший, пока
`summary_has_new_messages` - это `true`. Он не отправляет сообщения на LLM или
Событие `topic.updated` содержит полную обновленную тему
Снимок, так что подключенные клиенты заменить их локальное состояние темы без
опрос или специальный обобщенный конечный показатель.

Владелец или администратор может изменить запрос, используемый серверным агентом:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "high",
  "summary_enabled": true
}
```

Действие возвращает полный снимок темы и высылает `topic.updated` в
Установка `summary_system_prompt` на `null` выбирает
Схема планирования или перезапуска работы LLM остается
ответственность агента со стороны сервера.

Для автоматического обновления одной темы без удаления существующей
Резюме, одно и то же действие может отправлять только вход темы:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_enabled": false
}
```

### Администрация резюме темы {#topic-summary-administration}

Тема обобщения имеет два независимых базы данных поддерживаемых ворот.
только когда и `global_enabled` и текущий проект `project_enabled`
Обновление настроек обеспечивает оба значения:

```http
PUT /api/workspace/v1/messenger/topic_summary_settings/12345678-1234-4234-8234-123456789abc
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "global_enabled": true,
  "project_enabled": true
}
```

Путь UUID должен быть равен проекту IAM в контексте запроса.
доступны пользователям проекта; обновления требуют
`workspace.topic_summary_settings.manage`.

LLM конечные точки являются глобальными, а не проектными или потоковыми.
`workspace.topic_summary_endpoint.manage`:

```http
POST /api/workspace/v1/messenger/topic_summary_endpoints/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "uuid": "e4ad6d80-6bc7-4a91-864c-8e97319a82bd",
  "name": "primary-summary-model",
  "base_url": "https://llm.example.com/v1",
  "model": "summary-model",
  "api_key": "<write-only credential>",
  "enabled": true,
  "priority": 10,
  "supports_vision": true,
  "supports_reasoning": true,
  "temperature": 0.2,
  "max_output_tokens": 512,
  "top_p": 1.0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0
}
```

Все конечные точки реализуют OpenAI-совместимые `POST {base_url}/chat/completions`;
Ниже `priority` значения выполняются первыми и
UUID является детерминированным ти-брейкером.
Ограниченная аренда.
Процесс сдачи в аренду и попытки следующей конечной точки в этом порядке, до трех попыток.
Регистр выявляет ограниченные данные о здоровье (`last_success_at`, `last_failure_at`),
`failure_count`, и `last_error_code`) но никогда не выставляет активный жетон требования.

`api_key` принимается только при создании или замене учетных данных.
с секретом развертывания перед хранением и никогда не возвращается созданием, списком,
получить или обновить запись на события Workspace, скопировать в снимки темы, или
Регистрация обновлений и удалений обычно
Регистр намеренно не имеет
пересмотр, `ETag` или `If-Match` контракт.

Настройки генерации имеют следующие по умолчанию и принятые диапазоны:

| Поле | По умолчанию | Диапазон |
| --- | --- | --- |
| `temperature` | `0.2` | `0.0..2.0` |
| `max_output_tokens` | `512` | `1..32768` |
| `top_p` | `1.0` | `0.0..1.0` |
| `presence_penalty` | `0.0` | `-2.0..2.0` |
| `frequency_penalty` | `0.0` | `-2.0..2.0` |

Messenger работник утверждает одну устаревшую тему и максимум 100 новых сообщений в
шаг, снимки границы и эффективный запрос, выполняет требование, выполняет
запрос LLM вне каждой транзакции базы данных, и результат сохраняется в
новая транзакция через существующий внутренний помощник по обобщению.
Задержки повторных попыток, аренды конечных точек и истечение срока действия претензии сохраняются, поэтому повторные попытки остаются
ограниченный и наблюдаемый.

Длинные рассуждения - это нормальный ответ поставщика, а не неудача работника.
время отключения соединения составляет 30 секунд, в то время как время ответа составляет 25 минут, так что
Модель может рассуждать в течение 20 минут, не превышая срок клиента.
Время аренды конечных точек составляет 30 минут, а время аренды темы-работы - 90 минут.
Работник также выполняет договор аренды конечных точек, по крайней мере, на время отсчета плюс
60 секунд и тематический аренду, по крайней мере, три таких окна запроса, так что еще один
рабочий не может восстановить работу в режиме ожидания во время медленного ответа или немедленного перехода.

Когда ограниченная партия сообщения содержит изображение Workspace и любые включенные
Если точка окончания зрения существует, может быть выбрана только точка окончания зрения.
конечная точка занята, работа ждет; она не возвращается к конечной точке свободного текста.
Текстовое обобщение допускается для партии с изображением только в том случае, если нет
Включенная точка окончания визуализации существует. Изображения кодируются только в сообщении пользователя
содержание, в то время как системный запрос всегда остается только текстовым.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` устанавливает
режим уведомления о тематике текущего пользователя:

```json
{
  "notification_mode": "follow"
}
```

Разрешены режимы уведомления о темах `mute`, `default` и `follow`. `unmute`
допускается только тогда, когда режим уведомления потока текущего пользователя `muted`.
Нечитаемая классификация оценивается с текущих настроек, поэтому изменение
режим немедленно переклассифицирует существующие нечитаемые сообщения. `follow` делает каждый
topic unread active, `unmute` делает только прямые упоминания о текущем пользователе
активный, `mute` делает каждую нечитаемую тему пассивной, и `default` наследует
Поток в `mentions_only` также помещает только прямые упоминания
в `active_unread_count`; все оставшиеся нечитаемые сообщения остаются в
`passive_unread_count`.

Для потоков и тем Zulip, поддерживаемых провайдером, действия уведомления в очереди
Обновления поставщика применяются обратно к
Workspace с временной меткой, поэтому более старое обновление не может заменить
Новый.

Акция чтения темы:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` отмечает все нечитаемые сообщения в теме как прочитаные для текущего пользователя и
возвращает обновленный вид темы.

Побочные эффекты в режиме реального времени:

| Операция | payload.kind | object_type | Полезная нагрузка |
| --- | --- | --- | --- |
| создать тему | `topic.created` | `topic` | Полный снимок темы пользователя для каждого пользователя потока. |
| переименовать тему | `topic.updated` | `topic` | Полный снимок темы пользователя для каждого пользователя потока. |
| Переход выполнен | `topic.updated` | `topic` | Полный снимок темы пользователя для каждого пользователя потока. |
| Установка темы по умолчанию | `stream.updated`, `topic.updated` | `stream`, `topic` | Обновленный снимок потока и предыдущие /new по умолчанию темы для каждого пользователя потока. |
| Обновление резюме сервера | `topic.updated` | `topic` | Полный снимок темы пользователя для каждого пользователя потока. |
| Установка краткого запроса | `topic.updated` | `topic` | Полный снимок темы пользователя для каждого пользователя потока. |
| Изменить режим уведомления о теме | `topic.updated`, `stream.updated` | `topic`, `stream` | Перезагрузка темы и потоковые нечитаемые снимки только для текущего пользователя. |
| читать сообщения по теме | `topic.read` | `topic` | Полный снимок темы пользователя, возвращенный действием. |
| читать сообщения по теме | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Обновлены нечитаемые снимки текущего пользователя. |
| Удалить тему | `topic.deleted` | `topic` | Удалённая тема `uuid` и `stream_uuid`, отправленная каждому пользователю потока. Удаление темы по умолчанию также излучает `stream.updated` с `default_topic_uuid: null`. |

## Сообщения {#messages}

`POST /api/workspace/v1/messenger/messages/` подтверждает текущий поток PostgreSQL
членство и обязывает канонический UTF-8 отметки сообщение, флаги, один общий
получатель-аудитория игры и компактное сообщение/topic/stream события в
Это не создает одну строку канонических событий на получателя.
Читается остается охваченным текущим пользователем IAM и сохраняет существующий ответ.

Единственная поддерживаемая полезная нагрузка сообщений в v1 - это отметка:

```json
{
  "kind": "markdown",
  "content": "Hello, workspace"
}
```

Workspace ссылки на объекты внутри контента с отметкой использовать регулярную ссылку на отметку
Синтаксис. Часть URL - это Workspace URN:

| Субъект | Форма отклонения | Примечания |
| --- | --- | --- |
| упоминание пользователя | `[Jane Doe](urn:user:<user-uuid>)` | Обращаются с пользовательским тегом/mention. |
| Ссылка на сообщение | `[See message](urn:message:<message-uuid>)` | Ссылки на сообщение Workspace. |
| Ссылка потока | `[general](urn:stream:<stream-uuid>)` | Указывает на Workspace поток. |
| ссылка на тему | `[deploys](urn:topic:<topic-uuid>)` | Ссылки на Workspace темы. |
| ссылка на файл | `[report.pdf](urn:file:<file-uuid>?name=report.pdf)` | Файл/media URN может включать параметры запроса метаданных. |
| изображение/video ссылка | `![photo.png](urn:image:<file-uuid>?name=photo.png)` | Изображения и видео используют `urn:image` / `urn:video`. |
| Аватар/default изображение | `[avatar](urn:gravatar:<hash>)` | Тот же канонический формат Gravatar URN как и пользователи Workspace; хэш составляет 32 или 64 гексадецимальных символа. |
| внешний URL | `[site](urn:url:https://example.com)` | Внешние `http` / `https` ссылки хранятся через `urn:url`. |

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор сообщения. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `stream_uuid` | UUID | Да, я знаю. | Нет | Поток UUID. |
| `topic_uuid` | UUID | Нет | Нет | Тема UUID; пропущенная или `null` использует тему по умолчанию потока. Запрос не выполняется с кодом `400001007`, когда потока нет по умолчанию. |
| `author_uuid` | UUID | Нет | Да, я знаю. | Автор сообщения. |
| `payload` | объект | Да, я знаю. | Нет | Уменьшение полезной нагрузки сообщения; содержимое должно быть 1..40 000 символов. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Текущий пользователь в просмотре сообщения пользователя. |
| `read` | булевой | Нет | Да, я знаю. | Флаг чтения текущего пользователя. Авторы создаются как читаемые. |
| `pinned` | булевой | Нет | Да, я знаю. | Флаг текущего пользователя. |
| `starred` | булевой | Нет | Да, я знаю. | Звездочный флаг текущего пользователя. |
| `is_own` | булевой | Нет | Да, я знаю. | Равная ли `author_uuid` текущему пользователю. |
| `mentioned` | булевой | Нет | Да, я знаю. | Указывается ли в маркировке полезная нагрузка текущего пользователя; по умолчанию `false`. |
| `reactions` | объект | Нет | Да, я знаю. | Совокупное количество реакций, обозначенное `emoji_name`. |
| `reaction_users` | объект | Нет | Да, я знаю. | Полный список постоянных пользователей UUID для ограниченных реакционных групп, заданных ключами `emoji_name`. Пустой объект или отсутствующий ключ означает только подсчет; списки никогда не являются частичными. |
| `source_name` | `native`, `zulip` | Нет | Нет | Имя источника сообщения; общественность API устанавливает его как `native` при пропуске. |
| `source` | объект | Нет | Нет | По умолчанию `{"kind": "native"}`. Zulip `message_id` может быть `null` до успеха выходящей синхронизации. |
| `provider` | объект или `null` | Нет | Да, я знаю. | Значок поставщика, унаследованный от выбранного потока, поддерживаемого поставщиком. |
| `delivery` | объект или `null` | Нет | Да, я знаю. | Прогноз доставки текущего создания /update/delete. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

Создать запрос:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  }
}
```

Пример ответа:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  },
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": true,
  "mentioned": false,
  "reactions": {},
  "reaction_users": {},
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

Запрос на обновление:

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Edited text"
  }
}
```

`PUT /api/workspace/v1/messenger/messages/{message_uuid}` выполняет обновленную полезную нагрузку канонического сообщения и возвращает
только автор сообщения может обновить корневой
сообщение. `DELETE /api/workspace/v1/messenger/messages/{message_uuid}` выполняет
немедленное жесткое удаление канонического сообщения и его состояние на пользователя.
та же транзакция выделяет минимальное `message.deleted` событие для первоначального
аудитория, которая сохраняет требуемые поля идентификации и происхождения сообщения.

Читайте действие:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` устанавливает флаг сообщения текущего пользователя на `true` и возвращает обновленный
Если сообщение не было прочитано, то бэкэнд излучает `message.read` с
полный снимок сообщения и совокупные обновления счетов нечитаемых сообщений.

Читайте до действия:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read_up_to/invoke
Authorization: Bearer <access_token>
```

`read_up_to` отмечает непрочитанные сообщения в той же теме через выбранный
включает границу сообщения `(created_at, uuid)`, затем возвращает выбранный
Для внешнего чата Workspace отправляет уже решенный UUID
префикс как точный селектор; порядок сообщений, специфичный для поставщика, не может быть изменен
которые сообщения читаются.

Звезды и звезды:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/star/invoke
Authorization: Bearer <access_token>
```

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/unstar/invoke
Authorization: Bearer <access_token>
```

`star` и `unstar` установить флаг `starred` текущего пользователя и вернуть
После изменения флага, встроенные действия будут выполнены.
Заднего конца излучает `message.updated` только для текущего пользователя.
Workspace и не синхронизирована с внешним поставщиком.

Побочные эффекты в режиме реального времени:

| Операция | payload.kind | object_type | Полезная нагрузка |
| --- | --- | --- | --- |
| Создать сообщение | `message.created` | `message` | Полный снимок сообщения пользователя для каждого пользователя потока. |
| создать нечитаемое сообщение | `topic.updated`, `stream.updated` | `topic`, `stream` | Обновленные нечитаемые снимки для пользователей, где новое сообщение нечитается; пользовательский интерфейс получает агрегаты папок из потокового снимка. |
| Обновление сообщения полезной нагрузки | `message.updated` | `message` | Полный снимок сообщения пользователя для каждого пользователя потока. |
| создать/update/delete реакцию | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` | `message_reaction` | Снимок реакции для действующего пользователя. |
| создать/update/delete обновление реакционной агрегаты | `message.updated` | `message` | Полный снимок сообщения пользователя с обновленными `reactions` и `reaction_users` для каждого пользователя потока. |
| читать сообщение или читать до сообщения | `message.read` | `message` | Полный снимок сообщения пользователя, возвращенный действием. |
| читать нечитаемое сообщение | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Обновлены нечитаемые снимки текущего пользователя. |
| Звездочка или незвездочка | `message.updated` | `message` | Полный снимок сообщения пользователя для текущего пользователя при изменении флага. |
| Удалить сообщение | `message.deleted` | `message` | Удалённое сообщение `uuid`, `stream_uuid`, `topic_uuid`, `author_uuid`, `source_name` и `source`, отправленное каждому пользователю потока. |
| Удалить нечитаемое сообщение | `topic.updated`, `stream.updated` | `topic`, `stream` | Обновленные нечитаемые снимки для пользователей, где удаленное сообщение не было прочитано; пользовательский интерфейс получает агрегаты папок из потокового снимка. |

## Проекты {#drafts}

Проекты являются PostgreSQL принадлежащих государство клиента и никогда не создавать или изменять канонические
сообщения, непрочитанные счетчики, реакции или ссылки на файлы.
проект принадлежит точно одному IAM проекту, владельцу, потоку и теме.
`stream_uuid` и `topic_uuid` являются неизменными, тема должна принадлежать
В этом случае, если вы хотите, чтобы ваш сайт был доступен для всех пользователей, вы должны иметь доступ к потоку, и владелец должен быть в настоящее время участником потока.
может существовать для одной и той же пары потоков/topic.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Да, я знаю. | После создания | Идентификатор и идентификатор проекта, генерируемый клиентом. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Владелец черновика из токену IAM. |
| `stream_uuid` | UUID | Да, я знаю. | После создания | Поток, содержащий проект. |
| `topic_uuid` | UUID | Да, я знаю. | После создания | Тема, содержащая проект; она должна принадлежать `stream_uuid`. |
| `payload` | объект | Да, я знаю. | Нет | Это единственное поле, принятое `PUT`. |
| `revision` | цельное число, минимум 1 | Нет | Да, я знаю. | Сильное пересмотр ETag, начиная с `1`. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время последнего обновления. |

Создать запрос:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  }
}
```

Ответ:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  },
  "revision": 1,
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```

Запрос на обновление:

```http
PUT /api/workspace/v1/messenger/drafts/ca14d274-0057-4a9a-a34b-fb1174be6a17
Authorization: Bearer <access_token>
Content-Type: application/json
If-Match: "1"

{
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  }
}
```

Создание запросов требуют `uuid`, `stream_uuid`, `topic_uuid`, и Markdown
Содержание маркировки уменьшено, должно оставаться не пустым и ограничено
40 000 символов. Повторная попытка точно такой же канонической создать UUID возвращает
существующий проект без других изменений; повторное использование UUID с различными полями
возвращает `409`.

`GET`, `POST` и `PUT` одноресурсные ответы возвращают сильный ETag, такой как
`ETag: "3"`. `PUT` принимает только `payload`; `PUT` и `DELETE` требуют точного
текущее значение в `If-Match`. отсутствующие предварительные условия возвращают `428`.
недействительные значения возвращает `412` с текущим проектом и текущим ETag.
Успешное обновление увеличивает `revision`; удаленное удаляет возвращает `204`.

Проект CRUD не выпускает событий Workspace, уведомлений о веб-сокетах, рабочего стола
В этом случае, если вы не можете получить уведомления, команды провайдера или обычные сообщения Messenger.
Клиент наблюдает изменения на перезагрузке или явное перезагрузки черновиков API.
владелец из потока или удаление темы/stream
протяженность в PostgreSQL каскадах с иностранным ключем, без надгробий или
сообщение о побочных эффектах.

## Реакция на сообщение

Реакции сообщений являются каноническими PostgreSQL ресурсами.
сообщения, видимые текущему пользователю IAM.
Создание, обновление или удаление реакции выделяет событие `message_reaction.*`
для действующего пользователя и `message.updated` событий для каждого пользователя, который может видеть
сообщение; снимок сообщения содержит совокупные `reactions` и те же
Продолжающаяся проекция `reaction_users` как REST.

| Поле | Тип | Требуется при создании | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор реакции. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта. |
| `message_uuid` | UUID | Да, я знаю. | Нет | Сообщение, на которое реагируется; должно быть видно текущему пользователю. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Пользователь, который владеет реакцией. |
| `emoji_name` | Струна, максимум 128 | Да, я знаю. | Нет | Эмоджи/reaction- Имя. |
| `provider` | объект или `null` | Нет | Да, я знаю. | Провайдерский значок, унаследованный от целевого сообщения. |
| `delivery` | объект или `null` | Нет | Да, я знаю. | Прогноз доставки текущего создания /update/delete. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

`provider_metadata` и `delivery_metadata` являются сырыми полями хранения DM, а не
В настоящее время они появляются в генерируемых
`WorkspaceMessageReactions` OpenAPI схемы, но время выполнения
`resource_projection.as_dict(..., "message_reactions")` сериализатор удаляет
их перед упаковкой ответа и подвергает только обеззараженные `provider` и
`delivery` прогнозы выше.
Схема, сгенерированная.

Создать запрос:

```json
{
  "message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "emoji_name": "thumbs_up"
}
```

Один и тот же пользователь не может создавать дублирующие реакции с одним и тем же `message_uuid`
И ...`emoji_name`. Любой пользователь , который может увидеть сообщение может перечислить или получить его
Только владелец реакции может обновить или удалить эту реакцию.
Эти операции связывают реакцию и соответствующие побочные эффекты в режиме реального времени
Входи .PostgreSQL- Родные ответы сохранены .`provider: null`И ...`delivery: null`.

Поле `reactions` на просмотре сообщения представляет собой совокупную карту:

```json
{
  "thumbs_up": 2,
  "eyes": 1
}
```

Поле `reaction_users` показывает полные UUID списки только для небольших групп
По умолчанию порог для каждой группы составляет четыре
пользователей (`[messenger_reactions] user_list_limit`). Клиент не отправляет или
вывод предельной величины:

```json
{
  "reactions": {
    "eyes": 12,
    "heart": 3
  },
  "reaction_users": {
    "heart": [
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222",
      "33333333-3333-3333-3333-333333333333"
    ]
  }
}
```

Наличие ключа с эмодзи гарантирует, что список был полным, когда
Если текущее число превышает установленное
ограничение, запись удаляет этот ключ вместо хранения префикса.
сообщения не заполняются и, следовательно, возвращаются `reaction_users: {}` до
Изменение конфигурированного предела
не переписывает существующие снимки; следующая мутация группы применяет
Клиенты заменяют всю карту на каждом сообщении REST или в режиме реального времени
Снимка; они не должны объединять его с предыдущим значением.

Реакционные полезные нагрузки в реальном времени включают `uuid`, `project_id`, `message_uuid`,
`user_uuid`, `emoji_name`, `source_name`, `source`, `provider` и `delivery`.
Они никогда не показывают сырой `provider_metadata` или `delivery_metadata`.
`message_reaction.updated`, `old_message_uuid`, `old_emoji_name`,
`old_source_name` и `old_source` описывают предыдущую цель реакции.

## Файлы {#files}

Байты файлов и отдельный JSON боковой вагон хранятся через конфигурированный
S3 - развернутый бэкэнд; локальный бэкэнд
Использует тот же макет для тестов.
PostgreSQL хранит
канонические метаданные файлов и состояние ACL/access; S3 хранит двоичный файл и его JSON
- Сидельный вагон.

В боковом вагоне есть файл UUID, проект UUID, владелец UUID, метаданные отображения,
тип контента, размер, SHA-256, время создания и правило ACL.
их потока UUID и использовать правило динамического членства потока:

```json
{
  "acl": {
    "mode": "stream_members",
    "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
  }
}
```

В боковом вагоне никогда не содержится фото участника.
метаданные, и запрос загрузки проверяет аутентифицированного пользователя против
текущие канонические PostgreSQL связки потока.
Участник получает доступ немедленно; удаленный участник теряет его
немедленно без перезаписи S3.

Файлы, намеренно видимые на всей аутентифицированной Workspace используют это
ACL вместо:

```json
{
  "acl": {
    "mode": "public"
  }
}
```

`public` не является анонимным доступом.
Workspace IAM middleware, и любые запросы без действительного носителя Workspace
Токен, имеющий действительный Workspace носитель, может читать или загружать
`public`В этом случае, если вы не можете получить доступ к данным, вы можете использовать их для поиска информации.`public`боковой вагон
не должен содержать `stream_uuid`; он сохраняет `owner_uuid` и всю целостность
Nginx отклоняет многочастичные запросы больше `50m`, прежде чем они достигнут
`workspace-messenger-api`.

| Поле | Тип | Требуется на JSON создать | Только для чтения | Описание |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | Нет | Да, я знаю. | Идентификатор файла. |
| `project_id` | UUID | Нет | Да, я знаю. | IAM объем проекта; скрыт в API ответах. |
| `user_uuid` | UUID | Нет | Да, я знаю. | Владелец/uploader. |
| `stream_uuid` | UUID или `null` | Да, я знаю. | Нет | Необходимо для создания JSON и многочастичных загрузок `stream_members`; пропущено для многочастичных загрузок с `acl.mode=public`. |
| `name` | Строка, максимум 255 | Да, я знаю. | Нет | Имя файла. |
| `description` | Строка, максимум 255 | Нет | Нет | Описание файла; по умолчанию пустая строка. |
| `content_type` | строка | Да, я знаю. | Нет | MIME тип контента. |
| `size_bytes` | цельное число | Да, я знаю. | Нет | Размер файла в байтах. |
| `hash` | строка | Да, я знаю. | Нет | Хэш файла, в настоящее время SHA-256 для многочастичных загрузок. |
| `created_at` | дата и время | Нет | Да, я знаю. | Время сотворения. |
| `updated_at` | дата и время | Нет | Да, я знаю. | Время обновления. |

JSON метаданные создать запрос:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "example.txt",
  "description": "Example",
  "content_type": "text/plain",
  "size_bytes": 12,
  "hash": "abc"
}
```

Запрос на многочастичное загрузку:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
stream_uuid=75309057-419c-4b12-a7c1-3932429ec4a6
name=example.txt
description=Example
```

Обычный аутентифицированный клиент загружает общедоступный файл размером Workspace через
то же самое конечное место, отправляя существующий объект ACL как JSON и исключая
`stream_uuid`:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
acl={"mode":"public"}
name=public-example.txt
description=Authenticated Workspace-wide file
```

Для многочастичных загрузок требуется `file` и должен быть определен один объем
Если: либо `stream_uuid`, либо поле формы JSON
`acl={"mode":"public"}`. Общественные загрузки отвергают `stream_uuid`; потоковые загрузки
сохранить `stream_members` ACL. `name` по умолчанию для загруженного имени файла и
`description` по умолчанию на пустую строку.
`content_type` из загруженной части, рассчитывает `size_bytes`, и пишет
SHA-256 `hash`. Оба режима сохраняют то же двоичное плюс JSON лагера боковой и
тот же `urn:file`, `urn:image` или `urn:video` клиентский контракт.

`GET /api/workspace/v1/messenger/files/`, `GET /api/workspace/v1/messenger/files/{file_uuid}` и
`GET /api/workspace/v1/messenger/files/{file_uuid}/actions/download` требуют доступа к файлам. `PUT` и
`DELETE` требуют владения файлом. Загрузки возвращают сырые байты с сохраненными
`Content-Type`, `Content-Disposition` имя файла приложения, и сильный
`ETag` равен цитируемой SHA-256 `hash`, выставленной метаданными файла.
является неизменным для своего файла UUID; изменения метаданных выпускают `file.updated`.
владелец файла удаляет как его двоичный объект и JSON боковой вагон после канонического
Удаление файла совершено.


## Продукция {#services}

Услуги - это каталоги, открытые для чтения только на общем Workspace API.
`GET /api/workspace/v1/services/` перечисляет доступные услуги и
`GET /api/workspace/v1/services/{service_uuid}` возвращает одну услугу.

| Поле | Тип | Описание |
| --- | --- | --- |
| `uuid` | UUID | Идентификатор службы. |
| `name` | Строка, максимум 255 | Имя службы. |
| `description` | Строка, максимум 255 | Описание службы; по умолчанию пустая строка. |
| `service_url` | URL | Вход в строй URL. |
| `icon` | URL или `null` | Необязательное значок URL. |
| `created_at` | дата и время | Время сотворения. |
| `updated_at` | дата и время | Время последнего обновления. |

Пример ответа:

```json
{
  "uuid": "608919f5-ae0f-44fb-85bf-f1bf56534238",
  "name": "Messenger",
  "description": "Workspace Messenger",
  "service_url": "https://workspace.example.com/",
  "icon": "https://workspace.example.com/icon.svg",
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```


## События и эпоха {#events-and-epoch}

События - это долговечные записи PostgreSQL, предназначенные для их аудитории.
события переносят `user_uuid`; компактные трансляции событий используют хранимую аудиторию, так
каждый видимый клиент соблюдает один и тот же договор с публичным мероприятием без необходимости
только записи событий хранятся для
настраиваемый интервал, по умолчанию 72 часа; состояние сообщений, файлов, потока/topic,
Карта провайдера, и другие канонические ресурсы никогда не удаляются этим
Политика. обрезка продвигает хранится удержал пол так что остальные события формируют
полный видимый суффикс.
`epoch_version` монотонно в пределах одного PostgreSQL-владельца
`epoch_generation`.

`GET /api/workspace/v1/events/` возвращает события, сортированные по умолчанию по `epoch_version` восходящей.
REST `/events/` и доставка веб-сокета используют одну и ту же плоскую схему и оба читают
от видимой поверхности события PostgreSQL текущего пользователя.
`GET /api/workspace/v1/epoch/` использует ту же поверхность.

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 124,
  "project_id": "project-uuid",
  "user_uuid": "recipient-user-uuid",
  "object_type": "message",
  "action": "created",
  "created_at": "2026-07-02T16:37:49.552044Z",
  "updated_at": "2026-07-02T16:37:49.552047Z",
  "payload": {
    "kind": "message.created",
    "uuid": "message-uuid",
    "project_id": "project-uuid",
    "user_uuid": "recipient-user-uuid",
    "stream_uuid": "stream-uuid",
    "topic_uuid": "topic-uuid",
    "author_uuid": "author-user-uuid",
    "payload": {"kind": "markdown", "content": "Hello"},
    "source_name": "native",
    "source": {"kind": "native"},
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "mentioned": false,
    "reactions": {},
    "reaction_users": {},
    "provider": null,
    "delivery": null,
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

Поля верхнего уровня описывают только строку событий. `payload.kind` является единственным `kind`.
Не ожидайте верхнего уровня `type`, `kind`, `stream_uuid` или `topic_uuid`.

События создания сообщения/update несут ту же полезную нагрузку, хранящуюся на
сообщение. "Энтитетные ссылки" остаются регулярными ссылками на `urn:user`,
`urn:message`, `urn:stream`, `urn:topic`, файл /media, аватар или URL URN.

Messenger события создания, обновления, чтения и действия объекта несут один и тот же полный
объекта мгновенный снимок, который текущий пользователь получает от соответствующего REST
конечная точка или ответ на действие, плюс `payload.kind`.
операционные события вместо этого используют конверт, содержащий `kind`, ресурс
`uuid`, и очищенный полный ресурс под `snapshot`.
для создания, обновления и удаления внешних событий.

Messenger события удаления объектов минимальны:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, `source`

`stream_bindings.created` - это полезная нагрузка для действия в серии:

```json
{
  "kind": "stream_bindings.created",
  "uuid": "stream-uuid",
  "items": [
    {
      "uuid": "binding-uuid",
      "project_id": "project-uuid",
      "stream_uuid": "stream-uuid",
      "user_uuid": "added-user-uuid",
      "who_uuid": "owner-user-uuid",
      "role": "member",
      "notification_mode": "all_messages",
      "notification_updated_at": "2026-07-02T16:37:49.552044Z",
      "created_at": "2026-07-02T16:37:49.552044Z",
      "updated_at": "2026-07-02T16:37:49.552047Z"
    }
  ]
}
```

Читать действия излучают `message.read`, `topic.read`, или `stream.read` с полным
Объект ответа действия в `payload`.
`topic.updated`, `stream.updated` и `folder.updated`. Создание сообщения/delete
использует компактные `topic.updated` и `stream.updated` события; папка проектов пользовательского интерфейса
Агрегаты из потока снимка вместо получения потенциально больших
Снимки папок, предназначенные для конкретного пользователя, на каждое сообщение.

Поддерживаемые значения:

| object_type | действие | payload.kind примеры |
| --- | --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` | `message.created`, `message.updated`, `message.deleted`, `message.read`, `messages.read` |
| `message_reaction` | `created`, `updated`, `deleted` | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` | `stream.created`, `stream.updated`, `stream.deleted`, `stream.read` |
| `stream_binding` | `created`, `updated`, `deleted` | `stream_bindings.created`, `stream_binding.updated`, `stream_binding.deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` | `topic.created`, `topic.updated`, `topic.deleted`, `topic.read` |
| `user` | `updated` | `user.updated` |
| `folder` | `created`, `updated`, `deleted` | `folder.created`, `folder.updated`, `folder.deleted` |
| `folder_item` | `deleted` | `folder_item.deleted` |
| `file` | `created`, `updated`, `deleted` | `file.created`, `file.updated`, `file.deleted` |
| `external_account` | `created`, `updated`, `deleted` | `external_account.created`, `external_account.updated`, `external_account.deleted` |
| `external_chat` | `created`, `updated`, `deleted` | `external_chat.created`, `external_chat.updated`, `external_chat.deleted` |
| `external_operation` | `created`, `updated`, `deleted` | `external_operation.created`, `external_operation.updated`, `external_operation.deleted` |

Все внешние значения в таблице являются зарегистрированными типами публичных событий.
В данном случае, если вы не можете получить доступ к данным, вы можете использовать
сайты вызова выпускают `external_chat.updated` для изменений каталога и назначения и
`external_chat.deleted` при удалении выступа;
`external_chat.created` остается зарегистрированным типом схемы.

Для строгого догоняния после обработки курсора используйте:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<saved_generation>&page_limit=500
```

`GET /api/workspace/v1/epoch/` возвращает последний видный курсор события и
самая старая сохранившаяся эпоха для текущего пользователя IAM. `epoch_version` - это прямая
псевдоним `current_epoch_version`:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

Для вновь созданного потока пустых событий `epoch_version` и
`current_epoch_version` - это `0`, `minimum_epoch_version` - это `1`, и
`epoch_generation` все еще не пустое PostgreSQL-принадлежащее поколение.
`GET /api/workspace/v1/events/?epoch_version%3E=0` возвращает пустой список
чем ошибка курсора.

Клиенты сохраняют `epoch_generation` вместе с `epoch_version`.
курсор выше нуля без поколения, измененное поколение, будущая эпоха,
или эпоха, старше сохранившейся суффиксы, возвращает HTTP `410` с
`type=EventsCursorExpiredError`, `error=epoch_pruned`, причина, и
текущие поля курсора /minimum. Ответ `Cache-Control: no-store`.
Клиенты затем очищают кэши производных сущностей/blob, загружают авторитетные снимки,
и перезапустить отслеживание от возвращенного поколения; серверные сообщения и домен
данные не удаляются.

## Workspace Пользователи

Workspace пользователи хранятся в `m_workspace_users`. маршрут является глобальным, а
чем в рамках проекта.

`GET /api/workspace/v1/me/` возвращает тот же объект `WorkspaceUser_Get`, что и
`GET /api/workspace/v1/users/{user_uuid}`, используя пользователя UUID из IAM
Клиент не отправляет или получает пользователя UUID для этого запроса.
Backend берет `project_id` из IAM самоанализа, обновляет IAM-владельцев
имя пользователя, имя, фамилия и электронная почта проекции, и возвращает локальный
Workspace статус, аватар и поля присутствия.

IAM идентичности проецируются лениво. вызов `/me/` или запрос текущего
пользователь через `/users/{user_uuid}` создает или обновляет этот пользователь Workspace
Проекция; перечисление `/users/` не с нетерпением импортирует каждый IAM счет.
`GET /users/{other_user_uuid}` поиск является только проекцией: он не импортирует
что IAM идентичность и возвращает не найден, пока другой пользователь не был
материализованы собственной аутентифицированной Workspace деятельностью.

Когда текущий пользователь IAM запрашивает свой собственный UUID, API материализуется или
обновляет проекцию идентичности IAM перед возвратом.
`zulip` источник буквальный идентифицирует
внешняя идентичность, проецированная временем выполнения Zulip через частного поставщика
API. Доступные данные провайдера и сырые идентификаторы не являются частью этого браузера
ресурс.

| Поле | Тип | Описание |
| --- | --- | --- |
| `uuid` | UUID | Идентификатор пользователя. |
| `username` | Строка, 1,128 | Имя пользователя. |
| `source` | `iam`, `zulip` | Источник пользователя. |
| `identity_kind` | `external` или пропущено | Маркер для чтения только для внешнего поставщика. |
| `display_name` | строка или пропущенная | Имя внешнего идентификатора, отображаемое только для чтения. |
| `provider` | объектом или пропущенным | Внешний идентификационный конверт, содержащий только для чтения `kind` и `account_uuid`; необработанные идентификаторы и учетные данные поставщика никогда не выставляются. |
| `status` | `active`, `idle`, `offline`, `do_not_disturb` | Состояние присутствия. |
| `status_emoji` | строка или `null`, максимум 64 | Эмоции присутствия. |
| `status_text` | строка или `null`, максимум 256 | Смысл текста присутствия. |
| `first_name` | строка или `null` | Имя. |
| `last_name` | строка или `null` | Фамилия. |
| `email` | строка или `null` | Адрес электронной почты. |
| `avatar` | URN строка | Поддерживаемые значения `urn:gravatar:<32-or-64-hex-hash>`, `urn:image:<uuid>` и `urn:url:http(s)://...`. При пропуске Workspace хэширует нормализованную электронную почту с MD5; пользователи без электронной почты получают необратимый MD5 отступный код, полученный от их UUID. |
| `last_ping_at` | дата и время | Последний сигнал. |
| `created_at` | дата и время | Время сотворения. |
| `updated_at` | дата и время | Время обновления. |

Внешний провайдер может проецировать Gravatar-совместимый аватара как
`urn:gravatar:<md5(trim(lower(delivery_email)))>`. Идентификаторы поставщика сырья и
адреса доставки только поставщика не указаны в настоящем договоре.

Обновление присутствия:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/presence/invoke
Content-Type: application/json

{
  "status": "active",
  "emoji": "coffee",
  "text": "Focusing"
}
```

Автентифицированный пользователь может обновлять только свой собственный `user_uuid`.
Показано состояние и текущее время в`last_ping_at`- Необязательно .`emoji`и
Поля `text` хранятся как `status_emoji` и `status_text`; необязательное исключение
Поле сохраняют предыдущие значения, и явно `null` очищает их. Workspace работник мессенджера отмечает устаревших пользователей в автономном режиме и излучает события `user.updated` с полным пользователем
Снимки, включая `avatar`, для всех пользователей Workspace в каждом проекте.

Загрузка аватара - это атомное действие собственного пользователя:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_upload/invoke
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<PNG, JPEG, GIF, or WebP binary part>
```

Принимается только собственный аутентифицированный пользователь UUID. Максимальный размер аватара
25 MiB. Задней панелью проверяется тип заявленного MIME и двоичная подпись,
хранит байты и JSON боковой вагон через конфигурированный файл бэк-энда, набора
`acl.mode` до `public`, пропускает `stream_uuid` и обновляет только `user.avatar` до
`urn:image:<file-uuid>`. Пользовательское имя, имя и поля электронной почты, принадлежащие IAM, остаются
действие выдает полный `user.updated` снимок в каждом Workspace
Проект.

Для сброса аватара используется то же разрешение собственного пользователя:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_reset/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

Перезагрузка заменяет `user.avatar`
`urn:gravatar:<md5(trim(lower(email)))>` или канонический необратимый UUID
Замещенный пользовательский аватар теряет доступ к публике
как только строка ссылки пользователя и проекции обновляются; его двоичные и
Затем боковые вагоны удаляются из хранилища.

## WebSocket Резюме в режиме реального времени {#websocket-realtime-summary}

Общий сервис веб-сокетов использует подпротокол `workspace.events.v1` и аутентифицирует
токен на владельца от `Sec-WebSocket-Protocol`:

```ts
const ws = new WebSocket(
  "/api/workspace/v1/events/ws?last_epoch_version=124&epoch_generation=781203",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

После того, как соединение будет принято, сервер отправляет пропущенные события более поздние, чем
Затем он отправляет точно один фрейм управления
`{"type":"ready","epoch_generation":"...","epoch_version":124}` до любого
Включение в систему уведомления пользователя остается закрытым до этого момента.
Каждое сообщение события является тем же плоским объектом события, возвращенным REST
`/api/workspace/v1/events/`. Услуга веб-сокета не отправляет
сообщения на уровне приложения JSON `hello` или `ping` и не обрабатывает клиент
JSON `pong` или `ack` сообщения. Он отправляет протокольный уровень WebSocket контроллером пинга
Включение и догонка
Прошедший курсор отправляет тот же вписанный
`epoch_pruned` JSON ошибка как REST и закрывается с кодом `4410` и причиной
`epoch_pruned`.

Для защищенных кэшей файлов `file.created/updated/deleted` недействителен один UUID.
При удалении членства удаленный пользователь получает `stream.deleted`; клиенты
немедленно выселить каждый защищенный блок, чьи кэшированные метаданные имеют, что
`stream_uuid`. Остальные участники получают `stream_binding.deleted` (и
роль/settings изменения производят `stream_binding.updated`) для обновления участника
410 пробел очищает все полученные защищенные-точки кэша записи.

Подробные правила интеграции пользовательского интерфейса документированы в
`docs/workspace_ui_realtime_integration.md`.

## OpenAPI И развертывание

Документ "Время выполнения" Workspace OpenAPI доступен по адресу:
`/api/workspace/specifications/3.0.3`. Это описывает контроллер-поддерживается
IAM-аутентифицированная HTTP поверхность и не содержит провайдера, почты или календаря
Продукт-средний `server_settings` псевдоним и
отдельные события WebSocket являются документально зарегистрированными интерфейсами в режиме выполнения, но не отображаются
как генерируемые OpenAPI пути. Контракт частного поставщика поддерживается
отдельно в
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

Элемент Workspace устанавливает независимый `workspace-messenger-api`,
`workspace-api`, `workspace-messenger-events` и
`workspace-messenger-worker` процессы плюс частный
`workspace-external-bridge-api` служба. PostgreSQL-канонический время выполнения делает
элемент требует S3aaS для бинарной системы
объекты и JSON боковые вагоны и DBaaS для канонического Messenger и провайдера состояния.
Он создает существующий интерфейс Workspace в режиме Messenger и обслуживает его с
Нингнкс.

Связанные документы:

- [Workspace архитектура](architecture.md)
- [Workspace Интеграция пользовательского интерфейса в режиме реального времени](workspace_ui_realtime_integration.md)
- [Частный Workspace Поставщик API](../workspace_provider_api_v1.yaml)
- [Zulip Продукт поставщика и общественный контракт API](zulip_bridge_v1_product_and_api.md)
