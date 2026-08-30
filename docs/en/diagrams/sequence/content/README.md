[← Main documentation index](../../../index.md) · [Sequence diagram index](../README.md) · [Workspace content and users section](README.md)

# Workspace Content Operations and Client State Diagrams

This catalog covers 35 public HTTP operations for discovering routes,
server settings, folders, folder items, drafts, files, services,
push notification devices, Workspace users, and `/me`. Each operation has a
dedicated contract description, an editable PlantUML sequence source, and a locally generated SVG.

The public contract remains [`workspace_api.md`](../../../workspace_api.md).
The folder projection follows a consistent target boundary:
the canonical `FOLDER` is stored as a single instance, while access,
personal state, and ready-made unread/mention aggregates are stored in the
unique `USER_FOLDER_BINDING` — a user-to-folder binding.
`FOLDER_ITEM` links a folder to a supported canonical object, such as
`STREAM`, strictly in the form of the current public contract. Unread fields for a
folder item are sourced from the unique `USER_STREAM_BINDING`. Public
UUID references remain scalar UUID properties, while physical columns are
indexed foreign keys. Read paths use only simple indexed joins and do not perform `COUNT` during query or message traversal.

System folders are represented by `USER_FOLDER_BINDING` with fixed rule and type:
the client cannot delete such a binding or manually change its rule.
Ready-made automatic `FOLDER_ITEM` — is a restorable materialized
projection maintained by a worker. The source consists of active
`USER_STREAM_BINDING` and canonical `STREAM` with `is_archived = false`:
`All chats` ("All chats") includes all such streams available to the user;
`Personal` ("Personal") — only streams with `STREAM.private = true`;
`Channels` ("Channels") — only streams with `STREAM.private = false`. Creating,
updating, or deleting a
stream binding writes to a transactional outbox, from which a separate immutable task with a unique `outbox_event_uuid` is formed; the worker of the
`user-folder` domain idempotently adds/removes
automatic items and updates ready-made aggregates
`unread_count`/`mention_count` in
`USER_FOLDER_BINDING`. No new public actions are introduced for this purpose.

| Method | Public path | Operation specification | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `GET` | `/api/workspace/v1/` | [get_api_routes_index](operations/get_api_routes_index.md) | [source](operations/diagrams/get_api_routes_index.puml) | [visualization](operations/diagrams/get_api_routes_index.svg) |
| `GET` | `/api/workspace/v1/messenger/` | [get_messenger_routes_index](operations/get_messenger_routes_index.md) | [source](operations/diagrams/get_messenger_routes_index.puml) | [visualization](operations/diagrams/get_messenger_routes_index.svg) |
| `GET` | `/api/workspace/v1/messenger/server_settings` | [get_server_settings](operations/get_server_settings.md) | [source](operations/diagrams/get_server_settings.puml) | [visualization](operations/diagrams/get_server_settings.svg) |
| `GET` | `/api/workspace/v1/messenger/folders/` | [get_folders_list](operations/get_folders_list.md) | [source](operations/diagrams/get_folders_list.puml) | [visualization](operations/diagrams/get_folders_list.svg) |
| `POST` | `/api/workspace/v1/messenger/folders/` | [post_folders_create](operations/post_folders_create.md) | [source](operations/diagrams/post_folders_create.puml) | [visualization](operations/diagrams/post_folders_create.svg) |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [get_folder](operations/get_folder.md) | [source](operations/diagrams/get_folder.puml) | [visualization](operations/diagrams/get_folder.svg) |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [put_folder_update](operations/put_folder_update.md) | [source](operations/diagrams/put_folder_update.puml) | [visualization](operations/diagrams/put_folder_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | [delete_folder](operations/delete_folder.md) | [source](operations/diagrams/delete_folder.puml) | [visualization](operations/diagrams/delete_folder.svg) |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | [get_folder_items_list](operations/get_folder_items_list.md) | [source](operations/diagrams/get_folder_items_list.puml) | [visualization](operations/diagrams/get_folder_items_list.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | [post_folder_items_create](operations/post_folder_items_create.md) | [source](operations/diagrams/post_folder_items_create.puml) | [visualization](operations/diagrams/post_folder_items_create.svg) |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | [get_folder_item](operations/get_folder_item.md) | [source](operations/diagrams/get_folder_item.puml) | [visualization](operations/diagrams/get_folder_item.svg) |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | [delete_folder_item](operations/delete_folder_item.md) | [source](operations/diagrams/delete_folder_item.puml) | [visualization](operations/diagrams/delete_folder_item.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | [post_folder_item_pin](operations/post_folder_item_pin.md) | [source](operations/diagrams/post_folder_item_pin.puml) | [visualization](operations/diagrams/post_folder_item_pin.svg) |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | [post_folder_item_unpin](operations/post_folder_item_unpin.md) | [source](operations/diagrams/post_folder_item_unpin.puml) | [visualization](operations/diagrams/post_folder_item_unpin.svg) |
| `GET` | `/api/workspace/v1/messenger/drafts/` | [get_drafts_list](operations/get_drafts_list.md) | [source](operations/diagrams/get_drafts_list.puml) | [visualization](operations/diagrams/get_drafts_list.svg) |
| `POST` | `/api/workspace/v1/messenger/drafts/` | [post_drafts_create](operations/post_drafts_create.md) | [source](operations/diagrams/post_drafts_create.puml) | [visualization](operations/diagrams/post_drafts_create.svg) |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [get_draft](operations/get_draft.md) | [source](operations/diagrams/get_draft.puml) | [visualization](operations/diagrams/get_draft.svg) |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [put_draft_update](operations/put_draft_update.md) | [source](operations/diagrams/put_draft_update.puml) | [visualization](operations/diagrams/put_draft_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | [delete_draft](operations/delete_draft.md) | [source](operations/diagrams/delete_draft.puml) | [visualization](operations/diagrams/delete_draft.svg) |
| `GET` | `/api/workspace/v1/messenger/files/` | [get_files_list](operations/get_files_list.md) | [source](operations/diagrams/get_files_list.puml) | [visualization](operations/diagrams/get_files_list.svg) |
| `POST` | `/api/workspace/v1/messenger/files/` | [post_files_create](operations/post_files_create.md) | [source](operations/diagrams/post_files_create.puml) | [visualization](operations/diagrams/post_files_create.svg) |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | [get_file](operations/get_file.md) | [source](operations/diagrams/get_file.puml) | [visualization](operations/diagrams/get_file.svg) |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | [put_file_update](operations/put_file_update.md) | [source](operations/diagrams/put_file_update.puml) | [visualization](operations/diagrams/put_file_update.svg) |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | [delete_file](operations/delete_file.md) | [source](operations/diagrams/delete_file.puml) | [visualization](operations/diagrams/delete_file.svg) |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | [get_file_download](operations/get_file_download.md) | [source](operations/diagrams/get_file_download.puml) | [visualization](operations/diagrams/get_file_download.svg) |
| `GET` | `/api/workspace/v1/services/` | [get_services_list](operations/get_services_list.md) | [source](operations/diagrams/get_services_list.puml) | [visualization](operations/diagrams/get_services_list.svg) |
| `GET` | `/api/workspace/v1/services/{service_uuid}` | [get_service](operations/get_service.md) | [source](operations/diagrams/get_service.puml) | [visualization](operations/diagrams/get_service.svg) |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` | [put_push_device](operations/put_push_device.md) | [source](operations/diagrams/put_push_device.puml) | [visualization](operations/diagrams/put_push_device.svg) |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` | [delete_push_device](operations/delete_push_device.md) | [source](operations/diagrams/delete_push_device.puml) | [visualization](operations/diagrams/delete_push_device.svg) |
| `GET` | `/api/workspace/v1/users/` | [get_users_list](operations/get_users_list.md) | [source](operations/diagrams/get_users_list.puml) | [visualization](operations/diagrams/get_users_list.svg) |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | [get_user](operations/get_user.md) | [source](operations/diagrams/get_user.puml) | [visualization](operations/diagrams/get_user.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | [post_user_presence](operations/post_user_presence.md) | [source](operations/diagrams/post_user_presence.puml) | [visualization](operations/diagrams/post_user_presence.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | [post_user_avatar_upload](operations/post_user_avatar_upload.md) | [source](operations/diagrams/post_user_avatar_upload.puml) | [visualization](operations/diagrams/post_user_avatar_upload.svg) |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | [post_user_avatar_reset](operations/post_user_avatar_reset.md) | [source](operations/diagrams/post_user_avatar_reset.puml) | [visualization](operations/diagrams/post_user_avatar_reset.svg) |
| `GET` | `/api/workspace/v1/me/` | [get_me](operations/get_me.md) | [source](operations/diagrams/get_me.puml) | [visualization](operations/diagrams/get_me.svg) |

Coverage: **35 HTTP operations**.

[← Main documentation index](../../../index.md) · [Sequence diagrams index](../README.md) · [Workspace content and users section](README.md)
