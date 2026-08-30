[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательностей](../README.md) · [Раздел контента и пользователей Workspace](README.md)

# Диаграммы операций с контентом Workspace и клиентским состоянием

Этот каталог охватывает 35 публичных HTTP-операций для обнаружения маршрутов,
серверных настроек, папок, элементов папок, черновиков, файлов, сервисов,
устройств push-уведомлений, пользователей Workspace и `/me`. Для каждой операции есть
отдельное описание контракта, редактируемый исходник последовательности
PlantUML и локально сгенерированный SVG.

Публичным контрактом остаётся [`workspace_api.md`](../../../workspace_api.md).
Проекция (projection) папок следует согласованной целевой границе:
каноническая `FOLDER` хранится в единственном экземпляре, а доступ,
персональное состояние и готовые агрегаты непрочитанного/упоминаний хранятся в
уникальной `USER_FOLDER_BINDING` — привязке (binding) пользователя к папке.
`FOLDER_ITEM` связывает папку с поддерживаемым каноническим объектом, например
`STREAM`, строго в форме текущего публичного контракта. Поля непрочитанного у
элемента папки поступают из уникальной `USER_STREAM_BINDING`. Публичные
UUID-ссылки остаются скалярными UUID-свойствами, а физические столбцы —
индексированными внешними ключами. Пути чтения используют только простые
индексированные соединения и не выполняют `COUNT` во время запроса или обход сообщений.

Системные папки представлены `USER_FOLDER_BINDING` с фиксированными правилом и типом:
клиент не может удалить такую привязку или вручную изменить её правило.
Готовые автоматические `FOLDER_ITEM` — восстанавливаемая материализованная
проекция, которую поддерживает воркер (worker). Источник — активные
`USER_STREAM_BINDING` и канонические `STREAM` с `is_archived = false`:
`All chats` («Все чаты») включает все такие доступные пользователю потоки;
`Personal` («Персональные») — только потоки с `STREAM.private = true`;
`Channels` («Каналы») — только потоки с `STREAM.private = false`. Создание,
обновление или удаление
привязки потока записывает транзакционный outbox, из которого формируется
отдельная immutable task с уникальным `outbox_event_uuid`; worker области
`user-folder` идемпотентно добавляет/удаляет
автоматические элементы и обновляет готовые агрегаты
`unread_count`/`mention_count` в
`USER_FOLDER_BINDING`. Новые публичные действия для этого не вводятся.

| Метод | Публичный путь | Спецификация операции | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `GET` | `/api/workspace/v1/` | [get_api_routes_index](operations/get_api_routes_index.md) | [исходник](operations/diagrams/get_api_routes_index.puml) | [визуализация](operations/diagrams/get_api_routes_index.svg) |
| `GET` | `/api/workspace/v1/messenger/` | [get_messenger_routes_index](operations/get_messenger_routes_index.md) | [исходник](operations/diagrams/get_messenger_routes_index.puml) | [визуализация](operations/diagrams/get_messenger_routes_index.svg) |
| `GET` | `/api/workspace/v1/messenger/server_settings` | [get_server_settings](operations/get_server_settings.md) | [исходник](operations/diagrams/get_server_settings.puml) | [визуализация](operations/diagrams/get_server_settings.svg) |
| `GET` | `/api/workspace/v1/messenger/folders/` | [get_folders_list](operations/get_folders_list.md) | [исходник](operations/diagrams/get_folders_list.puml) | [визуализация](operations/diagrams/get_folders_list.svg) |
| `POST` | `/api/workspace/v1/messenger/folders/` | [post_folders_create](operations/post_folders_create.md) | [исходник](operations/diagrams/post_folders_create.puml) | [визуализация](operations/diagrams/post_folders_create.svg) |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [get_folder](operations/get_folder.md) | [исходник](operations/diagrams/get_folder.puml) | [визуализация](operations/diagrams/get_folder.svg) |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [put_folder_update](operations/put_folder_update.md) | [исходник](operations/diagrams/put_folder_update.puml) | [визуализация](operations/diagrams/put_folder_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [delete_folder](operations/delete_folder.md) | [исходник](operations/diagrams/delete_folder.puml) | [визуализация](operations/diagrams/delete_folder.svg) |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | [get_folder_items_list](operations/get_folder_items_list.md) | [исходник](operations/diagrams/get_folder_items_list.puml) | [визуализация](operations/diagrams/get_folder_items_list.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | [post_folder_items_create](operations/post_folder_items_create.md) | [исходник](operations/diagrams/post_folder_items_create.puml) | [визуализация](operations/diagrams/post_folder_items_create.svg) |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | [get_folder_item](operations/get_folder_item.md) | [исходник](operations/diagrams/get_folder_item.puml) | [визуализация](operations/diagrams/get_folder_item.svg) |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | [delete_folder_item](operations/delete_folder_item.md) | [исходник](operations/diagrams/delete_folder_item.puml) | [визуализация](operations/diagrams/delete_folder_item.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | [post_folder_item_pin](operations/post_folder_item_pin.md) | [исходник](operations/diagrams/post_folder_item_pin.puml) | [визуализация](operations/diagrams/post_folder_item_pin.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | [post_folder_item_unpin](operations/post_folder_item_unpin.md) | [исходник](operations/diagrams/post_folder_item_unpin.puml) | [визуализация](operations/diagrams/post_folder_item_unpin.svg) |
| `GET` | `/api/workspace/v1/messenger/drafts/` | [get_drafts_list](operations/get_drafts_list.md) | [исходник](operations/diagrams/get_drafts_list.puml) | [визуализация](operations/diagrams/get_drafts_list.svg) |
| `POST` | `/api/workspace/v1/messenger/drafts/` | [post_drafts_create](operations/post_drafts_create.md) | [исходник](operations/diagrams/post_drafts_create.puml) | [визуализация](operations/diagrams/post_drafts_create.svg) |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [get_draft](operations/get_draft.md) | [исходник](operations/diagrams/get_draft.puml) | [визуализация](operations/diagrams/get_draft.svg) |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [put_draft_update](operations/put_draft_update.md) | [исходник](operations/diagrams/put_draft_update.puml) | [визуализация](operations/diagrams/put_draft_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [delete_draft](operations/delete_draft.md) | [исходник](operations/diagrams/delete_draft.puml) | [визуализация](operations/diagrams/delete_draft.svg) |
| `GET` | `/api/workspace/v1/messenger/files/` | [get_files_list](operations/get_files_list.md) | [исходник](operations/diagrams/get_files_list.puml) | [визуализация](operations/diagrams/get_files_list.svg) |
| `POST` | `/api/workspace/v1/messenger/files/` | [post_files_create](operations/post_files_create.md) | [исходник](operations/diagrams/post_files_create.puml) | [визуализация](operations/diagrams/post_files_create.svg) |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | [get_file](operations/get_file.md) | [исходник](operations/diagrams/get_file.puml) | [визуализация](operations/diagrams/get_file.svg) |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | [put_file_update](operations/put_file_update.md) | [исходник](operations/diagrams/put_file_update.puml) | [визуализация](operations/diagrams/put_file_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | [delete_file](operations/delete_file.md) | [исходник](operations/diagrams/delete_file.puml) | [визуализация](operations/diagrams/delete_file.svg) |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | [get_file_download](operations/get_file_download.md) | [исходник](operations/diagrams/get_file_download.puml) | [визуализация](operations/diagrams/get_file_download.svg) |
| `GET` | `/api/workspace/v1/services/` | [get_services_list](operations/get_services_list.md) | [исходник](operations/diagrams/get_services_list.puml) | [визуализация](operations/diagrams/get_services_list.svg) |
| `GET` | `/api/workspace/v1/services/{service_uuid}` | [get_service](operations/get_service.md) | [исходник](operations/diagrams/get_service.puml) | [визуализация](operations/diagrams/get_service.svg) |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` | [put_push_device](operations/put_push_device.md) | [исходник](operations/diagrams/put_push_device.puml) | [визуализация](operations/diagrams/put_push_device.svg) |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` | [delete_push_device](operations/delete_push_device.md) | [исходник](operations/diagrams/delete_push_device.puml) | [визуализация](operations/diagrams/delete_push_device.svg) |
| `GET` | `/api/workspace/v1/users/` | [get_users_list](operations/get_users_list.md) | [исходник](operations/diagrams/get_users_list.puml) | [визуализация](operations/diagrams/get_users_list.svg) |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | [get_user](operations/get_user.md) | [исходник](operations/diagrams/get_user.puml) | [визуализация](operations/diagrams/get_user.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | [post_user_presence](operations/post_user_presence.md) | [исходник](operations/diagrams/post_user_presence.puml) | [визуализация](operations/diagrams/post_user_presence.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | [post_user_avatar_upload](operations/post_user_avatar_upload.md) | [исходник](operations/diagrams/post_user_avatar_upload.puml) | [визуализация](operations/diagrams/post_user_avatar_upload.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | [post_user_avatar_reset](operations/post_user_avatar_reset.md) | [исходник](operations/diagrams/post_user_avatar_reset.puml) | [визуализация](operations/diagrams/post_user_avatar_reset.svg) |
| `GET` | `/api/workspace/v1/me/` | [get_me](operations/get_me.md) | [исходник](operations/diagrams/get_me.puml) | [визуализация](operations/diagrams/get_me.svg) |

Охват: **35 HTTP-операций**.

[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательностей](../README.md) · [Раздел контента и пользователей Workspace](README.md)
