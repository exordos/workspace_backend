[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [内容和用户分区 Workspace](README.md)

# 内容Workspace和客户端状态操作图

这份目录涵盖了35个公开HTTP路线检测操作,
服务器设置,文件,文件元素,草稿,文件,服务,
推送通知设备,用户Workspace和`/me`.
合同的单独描述,可编辑的序列源
PlantUML 并且是本地生成的 SVG.

现在,我们仍然是公开合同. [`workspace_api.md`](../../../workspace_api.md).
文件的投影 (projection) 遵循了已达成的目标边界:
规范 `FOLDER` 存储在唯一的副本中,而访问,
个人状态和未读/提醒的准备集
唯一的 `USER_FOLDER_BINDING`  绑定用户到文件.
`FOLDER_ITEM` 将文件与支持的正规对象联系起来,例如
`STREAM`, 没有读过的字段
文件元素来自一个独特的 `USER_STREAM_BINDING`.
UUID-引用仍然是标数 UUID 属性,而物理列 —
只有简单的读取方式才能使用.
索引连接,并且在请求或绕行消息时不执行`COUNT`.

系统文件以固定规则和类型表示`USER_FOLDER_BINDING`:
客户端不能删除此类绑定或手动更改其规则.
准备的自动`FOLDER_ITEM` 可恢复的物质化
动态的来源
`USER_STREAM_BINDING` 和正规的 `STREAM` `is_archived = false`:
`All chats` («所有聊天) 包括所有用户可访问的流;
`Personal` («个人)  只有流 `STREAM.private = true`;
`Channels` («) 只有流从`STREAM.private = false`. 创建,
更新或删除
流的绑定记录了交易的Outbox,
单独的唯一的 immutable 任务`outbox_event_uuid`工作人员;
`user-folder` 并且可以添加/删除
机器人和机器人
`unread_count`/`mention_count` 在
`USER_FOLDER_BINDING`. 没有新的公开操作..

| 方法 | 公开路径 | 操作规格 | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `GET` | `/api/workspace/v1/` |  [get_api_routes_index](operations/get_api_routes_index.md)  |  [发源](operations/diagrams/get_api_routes_index.puml)  |  [视觉化](operations/diagrams/get_api_routes_index.svg)  |
| `GET` | `/api/workspace/v1/messenger/` |  [get_messenger_routes_index](operations/get_messenger_routes_index.md)  |  [发源](operations/diagrams/get_messenger_routes_index.puml)  |  [视觉化](operations/diagrams/get_messenger_routes_index.svg)  |
| `GET` | `/api/workspace/v1/messenger/server_settings` |  [get_server_settings](operations/get_server_settings.md)  |  [发源](operations/diagrams/get_server_settings.puml)  |  [视觉化](operations/diagrams/get_server_settings.svg)  |
| `GET` | `/api/workspace/v1/messenger/folders/` |  [get_folders_list](operations/get_folders_list.md)  |  [发源](operations/diagrams/get_folders_list.puml)  |  [视觉化](operations/diagrams/get_folders_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/folders/` |  [post_folders_create](operations/post_folders_create.md)  |  [发源](operations/diagrams/post_folders_create.puml)  |  [视觉化](operations/diagrams/post_folders_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [get_folder](operations/get_folder.md)  |  [发源](operations/diagrams/get_folder.puml)  |  [视觉化](operations/diagrams/get_folder.svg)  |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [put_folder_update](operations/put_folder_update.md)  |  [发源](operations/diagrams/put_folder_update.puml)  |  [视觉化](operations/diagrams/put_folder_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` |  [delete_folder](operations/delete_folder.md)  |  [发源](operations/diagrams/delete_folder.puml)  |  [视觉化](operations/diagrams/delete_folder.svg)  |
| `GET` | `/api/workspace/v1/messenger/folder_items/` |  [get_folder_items_list](operations/get_folder_items_list.md)  |  [发源](operations/diagrams/get_folder_items_list.puml)  |  [视觉化](operations/diagrams/get_folder_items_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/` |  [post_folder_items_create](operations/post_folder_items_create.md)  |  [发源](operations/diagrams/post_folder_items_create.puml)  |  [视觉化](operations/diagrams/post_folder_items_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [get_folder_item](operations/get_folder_item.md)  |  [发源](operations/diagrams/get_folder_item.puml)  |  [视觉化](operations/diagrams/get_folder_item.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [delete_folder_item](operations/delete_folder_item.md)  |  [发源](operations/diagrams/delete_folder_item.puml)  |  [视觉化](operations/diagrams/delete_folder_item.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` |  [post_folder_item_pin](operations/post_folder_item_pin.md)  |  [发源](operations/diagrams/post_folder_item_pin.puml)  |  [视觉化](operations/diagrams/post_folder_item_pin.svg)  |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` |  [post_folder_item_unpin](operations/post_folder_item_unpin.md)  |  [发源](operations/diagrams/post_folder_item_unpin.puml)  |  [视觉化](operations/diagrams/post_folder_item_unpin.svg)  |
| `GET` | `/api/workspace/v1/messenger/drafts/` |  [get_drafts_list](operations/get_drafts_list.md)  |  [发源](operations/diagrams/get_drafts_list.puml)  |  [视觉化](operations/diagrams/get_drafts_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/drafts/` |  [post_drafts_create](operations/post_drafts_create.md)  |  [发源](operations/diagrams/post_drafts_create.puml)  |  [视觉化](operations/diagrams/post_drafts_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [get_draft](operations/get_draft.md)  |  [发源](operations/diagrams/get_draft.puml)  |  [视觉化](operations/diagrams/get_draft.svg)  |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [put_draft_update](operations/put_draft_update.md)  |  [发源](operations/diagrams/put_draft_update.puml)  |  [视觉化](operations/diagrams/put_draft_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` |  [delete_draft](operations/delete_draft.md)  |  [发源](operations/diagrams/delete_draft.puml)  |  [视觉化](operations/diagrams/delete_draft.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/` |  [get_files_list](operations/get_files_list.md)  |  [发源](operations/diagrams/get_files_list.puml)  |  [视觉化](operations/diagrams/get_files_list.svg)  |
| `POST` | `/api/workspace/v1/messenger/files/` |  [post_files_create](operations/post_files_create.md)  |  [发源](operations/diagrams/post_files_create.puml)  |  [视觉化](operations/diagrams/post_files_create.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [get_file](operations/get_file.md)  |  [发源](operations/diagrams/get_file.puml)  |  [视觉化](operations/diagrams/get_file.svg)  |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [put_file_update](operations/put_file_update.md)  |  [发源](operations/diagrams/put_file_update.puml)  |  [视觉化](operations/diagrams/put_file_update.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` |  [delete_file](operations/delete_file.md)  |  [发源](operations/diagrams/delete_file.puml)  |  [视觉化](operations/diagrams/delete_file.svg)  |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` |  [get_file_download](operations/get_file_download.md)  |  [发源](operations/diagrams/get_file_download.puml)  |  [视觉化](operations/diagrams/get_file_download.svg)  |
| `GET` | `/api/workspace/v1/services/` |  [get_services_list](operations/get_services_list.md)  |  [发源](operations/diagrams/get_services_list.puml)  |  [视觉化](operations/diagrams/get_services_list.svg)  |
| `GET` | `/api/workspace/v1/services/{service_uuid}` |  [get_service](operations/get_service.md)  |  [发源](operations/diagrams/get_service.puml)  |  [视觉化](operations/diagrams/get_service.svg)  |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` |  [put_push_device](operations/put_push_device.md)  |  [发源](operations/diagrams/put_push_device.puml)  |  [视觉化](operations/diagrams/put_push_device.svg)  |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` |  [delete_push_device](operations/delete_push_device.md)  |  [发源](operations/diagrams/delete_push_device.puml)  |  [视觉化](operations/diagrams/delete_push_device.svg)  |
| `GET` | `/api/workspace/v1/users/` |  [get_users_list](operations/get_users_list.md)  |  [发源](operations/diagrams/get_users_list.puml)  |  [视觉化](operations/diagrams/get_users_list.svg)  |
| `GET` | `/api/workspace/v1/users/{user_uuid}` |  [get_user](operations/get_user.md)  |  [发源](operations/diagrams/get_user.puml)  |  [视觉化](operations/diagrams/get_user.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` |  [post_user_presence](operations/post_user_presence.md)  |  [发源](operations/diagrams/post_user_presence.puml)  |  [视觉化](operations/diagrams/post_user_presence.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` |  [post_user_avatar_upload](operations/post_user_avatar_upload.md)  |  [发源](operations/diagrams/post_user_avatar_upload.puml)  |  [视觉化](operations/diagrams/post_user_avatar_upload.svg)  |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` |  [post_user_avatar_reset](operations/post_user_avatar_reset.md)  |  [发源](operations/diagrams/post_user_avatar_reset.puml)  |  [视觉化](operations/diagrams/post_user_avatar_reset.svg)  |
| `GET` | `/api/workspace/v1/me/` |  [get_me](operations/get_me.md)  |  [发源](operations/diagrams/get_me.puml)  |  [视觉化](operations/diagrams/get_me.svg)  |

覆盖范围: **35 HTTP操作**.

[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [内容和用户分区 Workspace](README.md)
