# Workspace Mail API

Это действующий браузерный контракт почты. Он расширяет единый сервис
[`workspace-api`](workspace_api.md) в `/api/workspace/v1/mail/` и не создаёт
второй публичный API-процесс.

## Архитектура и границы

- PostgreSQL является каноническим хранилищем для `m_mail_folders`,
  `m_mail_messages` и `mail_outgoing_intents`.
- Вложения переиспользуют файловое хранилище Workspace и задаются через
  `urn:file:`; байты остаются в S3.
- Почтовые ресурсы используют существующие IAM scope проекта и текущего
  пользователя, долговечные события Workspace, REST backfill и WebSocket.
- Отдельный почтовый runtime может забирать `mail_outgoing_intents` и
  проецировать изменения провайдера в канонические таблицы. Этот runtime и
  реальная SMTP/IMAP-доставка не входят в данное изменение backend.
- `/api/workspace/v1/calendar/` и публичные provider-specific маршруты
  по-прежнему отсутствуют.

## Ресурсы и действия

| Метод | Путь | Назначение |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/mail/folders/` | Получить свои папки или создать папку. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/folders/{folder_uuid}` | Прочитать, изменить или мягко удалить пустую свою папку. |
| `GET`, `POST` | `/api/workspace/v1/mail/messages/` | Получить свои письма или создать локальный черновик. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/messages/{message_uuid}` | Прочитать, изменить или мягко удалить своё письмо. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/move/invoke` | Переместить письмо между своими папками одной учётной записи. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/send/invoke` | Атомарно поставить черновик в очередь и создать один долговечный intent. |

Сгенерированный OpenAPI Workspace является источником истины для полных схем
полей, параметров фильтрации, HTTP-статусов и тел ответов.

## Черновик и отправка

1. Создать или выбрать папку. Если задан `external_account_uuid`, это должна
   быть включённая учётная запись `mail` текущего пользователя в IAM-проекте.
2. Создать письмо с `folder_uuid`; начальное состояние — `draft=true` и
   `delivery_status=draft`.
3. Изменить содержимое черновика или прикрепить видимые файлы Workspace.
4. Вызвать `send`. В одной транзакции БД проверяются получатели, тело, учётная
   запись и видимость вложений; создаётся строка `mail_outgoing_intents`;
   выставляется `delivery_status=queued`; публикуется `mail.message.updated`.
5. `sent_at` остаётся пустым до подтверждения доставки почтовым runtime.
   Письмо в очереди нельзя удалить через публичный API.

Действие отправки само по себе не доказывает внешнюю доставку. Пока почтовый
runtime не сообщил конечный результат, подтверждена только долговечная
постановка запроса в очередь.

## Вложения

Приватное почтовое вложение загружается существующим multipart endpoint:

```text
POST /api/workspace/v1/messenger/files/
Content-Type: multipart/form-data

file=<bytes>
acl={"mode":"owner"}
```

Возвращённый UUID используется как `urn:file:<file_uuid>` в `attachment_urns`.
API принимает только файлы, видимые текущему IAM-пользователю в текущем
проекте. `acl={"mode":"owner"}` создаёт приватный файл без stream; можно также
ссылаться на существующие stream-visible и public файлы Workspace.

## События realtime

| Kind | Тип объекта | Когда публикуется |
| --- | --- | --- |
| `mail.folder.created` | `mail_folder` | Создание папки. |
| `mail.folder.updated` | `mail_folder` | Изменение метаданных папки. |
| `mail.folder.deleted` | `mail_folder` | Мягкое удаление папки. |
| `mail.message.created` | `mail_message` | Создание черновика или проекции письма. |
| `mail.message.updated` | `mail_message` | Изменение содержимого, состояния, папки или очереди. |
| `mail.message.deleted` | `mail_message` | Мягкое удаление письма. |

Эти kind используют тот же cursor `/api/workspace/v1/events/` и stream
`/api/workspace/v1/events/ws`, что и ресурсы Messenger.

## Граница приёмки

Приёмка backend включает upgrade/downgrade миграции, генерацию OpenAPI,
изоляцию owner/project, видимость owner-файлов, идемпотентность долговечного
intent и тесты событий. Она не подтверждает развёрнутый почтовый runtime,
SMTP/IMAP-доступ, приёмку провайдером или наличие доставленной копии во внешней
папке Sent.
