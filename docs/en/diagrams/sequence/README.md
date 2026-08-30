[← The main index of the documentation](../../index.md) · [Background stream section](worker_flows/README.md) · [Current API contract](../../workspace_api.md)

# Operations sequence diagrams Workspace API

Status: **Completed project proposal, started with documentation and matched to current public contract**.

This directory covers all of the **109 semantically distinct HTTP-operations method + path»**:
108 Controller operations from generated OpenAPI and one canonical endpoint of intermediate software
`GET /api/workspace/v1/messenger/server_settings`. The same intermediate software processor accepts
a variant with a closing slant and the same straight `200`, without redirection; this is a pseudonym
The same operation, not a separate endpoint.
Running time flowWebSocketEvents for everyone.HTTP- operations exist.
The first is the self-contained Markdown description, the editable PlantUML source, and
Locally rendered SVG.

The project proposal files explain the target transaction flow RestAlchemy, transaction outbox, tasks, worker and events, but do not change the current public API.

Target compatibility rules accepted for all transaction documents:

- resource-list pagination: not present/`0` `page_limit` => `100`, `1..500`
  exactly, negative/incomplete/`>500` => HTTP `400`, unlimited is not available;
- `2xx`/`201` means commit primary mutation and immediate read-your-write for
  The author, but the recipient/history/counters/snapshots/events are constructed asynchronously;
  about a second  SLO intent, not strict guarantee;
- worker Fixes the projection update and all the relevant durable ready event
  rows The dispatcher only delivers at-least-once;
  reconnect It uses cursor replay without gap and client dedupe on event UUID.

This is a conscious observable behavior change.
read pages until the next marker is missing; release note should describe
It 's all together with the asynchronous visibility ..

## Family indexes

| Family | Coverage | The index |
| --- | ---: | --- |
| The main Messenger | 45 HTTP-operations |  [`core/README.md`](core/README.md)  |
| Content, users and client status Workspace | 35 HTTP-operations |  [`content/README.md`](content/README.md)  |
| Events, external integration and time of execution | 29 HTTP-operations + WebSocket |  [`external/README.md`](external/README.md)  |
| Workflow, outbox and projection streams | 7 flows without HTTP |  [`worker_flows/README.md`](worker_flows/README.md)  |

## Full coverage matrix

### Routes are indexed

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/` |  [`get_api_routes_index.md`](content/operations/get_api_routes_index.md)  |  [`get_api_routes_index.puml`](content/operations/diagrams/get_api_routes_index.puml)  |  [`get_api_routes_index.svg`](content/operations/diagrams/get_api_routes_index.svg)  |
| `GET /api/workspace/v1/messenger/` |  [`get_messenger_routes_index.md`](content/operations/get_messenger_routes_index.md)  |  [`get_messenger_routes_index.puml`](content/operations/diagrams/get_messenger_routes_index.puml)  |  [`get_messenger_routes_index.svg`](content/operations/diagrams/get_messenger_routes_index.svg)  |

### Server settings

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/server_settings` |  [`get_server_settings.md`](content/operations/get_server_settings.md)  |  [`get_server_settings.puml`](content/operations/diagrams/get_server_settings.puml)  |  [`get_server_settings.svg`](content/operations/diagrams/get_server_settings.svg)  |

### Folders

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/folders/` |  [`get_folders_list.md`](content/operations/get_folders_list.md)  |  [`get_folders_list.puml`](content/operations/diagrams/get_folders_list.puml)  |  [`get_folders_list.svg`](content/operations/diagrams/get_folders_list.svg)  |
| `POST /api/workspace/v1/messenger/folders/` |  [`post_folders_create.md`](content/operations/post_folders_create.md)  |  [`post_folders_create.puml`](content/operations/diagrams/post_folders_create.puml)  |  [`post_folders_create.svg`](content/operations/diagrams/post_folders_create.svg)  |
| `GET /api/workspace/v1/messenger/folders/{folder_uuid}` |  [`get_folder.md`](content/operations/get_folder.md)  |  [`get_folder.puml`](content/operations/diagrams/get_folder.puml)  |  [`get_folder.svg`](content/operations/diagrams/get_folder.svg)  |
| `PUT /api/workspace/v1/messenger/folders/{folder_uuid}` |  [`put_folder_update.md`](content/operations/put_folder_update.md)  |  [`put_folder_update.puml`](content/operations/diagrams/put_folder_update.puml)  |  [`put_folder_update.svg`](content/operations/diagrams/put_folder_update.svg)  |
| `DELETE /api/workspace/v1/messenger/folders/{folder_uuid}` |  [`delete_folder.md`](content/operations/delete_folder.md)  |  [`delete_folder.puml`](content/operations/diagrams/delete_folder.puml)  |  [`delete_folder.svg`](content/operations/diagrams/delete_folder.svg)  |

### Folder elements are

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/folder_items/` |  [`get_folder_items_list.md`](content/operations/get_folder_items_list.md)  |  [`get_folder_items_list.puml`](content/operations/diagrams/get_folder_items_list.puml)  |  [`get_folder_items_list.svg`](content/operations/diagrams/get_folder_items_list.svg)  |
| `POST /api/workspace/v1/messenger/folder_items/` |  [`post_folder_items_create.md`](content/operations/post_folder_items_create.md)  |  [`post_folder_items_create.puml`](content/operations/diagrams/post_folder_items_create.puml)  |  [`post_folder_items_create.svg`](content/operations/diagrams/post_folder_items_create.svg)  |
| `GET /api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [`get_folder_item.md`](content/operations/get_folder_item.md)  |  [`get_folder_item.puml`](content/operations/diagrams/get_folder_item.puml)  |  [`get_folder_item.svg`](content/operations/diagrams/get_folder_item.svg)  |
| `DELETE /api/workspace/v1/messenger/folder_items/{folder_item_uuid}` |  [`delete_folder_item.md`](content/operations/delete_folder_item.md)  |  [`delete_folder_item.puml`](content/operations/diagrams/delete_folder_item.puml)  |  [`delete_folder_item.svg`](content/operations/diagrams/delete_folder_item.svg)  |
| `POST /api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` |  [`post_folder_item_pin.md`](content/operations/post_folder_item_pin.md)  |  [`post_folder_item_pin.puml`](content/operations/diagrams/post_folder_item_pin.puml)  |  [`post_folder_item_pin.svg`](content/operations/diagrams/post_folder_item_pin.svg)  |
| `POST /api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` |  [`post_folder_item_unpin.md`](content/operations/post_folder_item_unpin.md)  |  [`post_folder_item_unpin.puml`](content/operations/diagrams/post_folder_item_unpin.puml)  |  [`post_folder_item_unpin.svg`](content/operations/diagrams/post_folder_item_unpin.svg)  |

### The files

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/files/` |  [`get_files_list.md`](content/operations/get_files_list.md)  |  [`get_files_list.puml`](content/operations/diagrams/get_files_list.puml)  |  [`get_files_list.svg`](content/operations/diagrams/get_files_list.svg)  |
| `POST /api/workspace/v1/messenger/files/` |  [`post_files_create.md`](content/operations/post_files_create.md)  |  [`post_files_create.puml`](content/operations/diagrams/post_files_create.puml)  |  [`post_files_create.svg`](content/operations/diagrams/post_files_create.svg)  |
| `GET /api/workspace/v1/messenger/files/{file_uuid}` |  [`get_file.md`](content/operations/get_file.md)  |  [`get_file.puml`](content/operations/diagrams/get_file.puml)  |  [`get_file.svg`](content/operations/diagrams/get_file.svg)  |
| `PUT /api/workspace/v1/messenger/files/{file_uuid}` |  [`put_file_update.md`](content/operations/put_file_update.md)  |  [`put_file_update.puml`](content/operations/diagrams/put_file_update.puml)  |  [`put_file_update.svg`](content/operations/diagrams/put_file_update.svg)  |
| `DELETE /api/workspace/v1/messenger/files/{file_uuid}` |  [`delete_file.md`](content/operations/delete_file.md)  |  [`delete_file.puml`](content/operations/diagrams/delete_file.puml)  |  [`delete_file.svg`](content/operations/diagrams/delete_file.svg)  |
| `GET /api/workspace/v1/messenger/files/{file_uuid}/actions/download` |  [`get_file_download.md`](content/operations/get_file_download.md)  |  [`get_file_download.puml`](content/operations/diagrams/get_file_download.puml)  |  [`get_file_download.svg`](content/operations/diagrams/get_file_download.svg)  |

### The Chernobyls

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/drafts/` |  [`get_drafts_list.md`](content/operations/get_drafts_list.md)  |  [`get_drafts_list.puml`](content/operations/diagrams/get_drafts_list.puml)  |  [`get_drafts_list.svg`](content/operations/diagrams/get_drafts_list.svg)  |
| `POST /api/workspace/v1/messenger/drafts/` |  [`post_drafts_create.md`](content/operations/post_drafts_create.md)  |  [`post_drafts_create.puml`](content/operations/diagrams/post_drafts_create.puml)  |  [`post_drafts_create.svg`](content/operations/diagrams/post_drafts_create.svg)  |
| `GET /api/workspace/v1/messenger/drafts/{draft_uuid}` |  [`get_draft.md`](content/operations/get_draft.md)  |  [`get_draft.puml`](content/operations/diagrams/get_draft.puml)  |  [`get_draft.svg`](content/operations/diagrams/get_draft.svg)  |
| `PUT /api/workspace/v1/messenger/drafts/{draft_uuid}` |  [`put_draft_update.md`](content/operations/put_draft_update.md)  |  [`put_draft_update.puml`](content/operations/diagrams/put_draft_update.puml)  |  [`put_draft_update.svg`](content/operations/diagrams/put_draft_update.svg)  |
| `DELETE /api/workspace/v1/messenger/drafts/{draft_uuid}` |  [`delete_draft.md`](content/operations/delete_draft.md)  |  [`delete_draft.puml`](content/operations/diagrams/delete_draft.puml)  |  [`delete_draft.svg`](content/operations/diagrams/delete_draft.svg)  |

### Services

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/services/` |  [`get_services_list.md`](content/operations/get_services_list.md)  |  [`get_services_list.puml`](content/operations/diagrams/get_services_list.puml)  |  [`get_services_list.svg`](content/operations/diagrams/get_services_list.svg)  |
| `GET /api/workspace/v1/services/{service_uuid}` |  [`get_service.md`](content/operations/get_service.md)  |  [`get_service.puml`](content/operations/diagrams/get_service.puml)  |  [`get_service.svg`](content/operations/diagrams/get_service.svg)  |

### Push-The device

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `PUT /api/workspace/v1/push_devices/{registration_uuid}` |  [`put_push_device.md`](content/operations/put_push_device.md)  |  [`put_push_device.puml`](content/operations/diagrams/put_push_device.puml)  |  [`put_push_device.svg`](content/operations/diagrams/put_push_device.svg)  |
| `DELETE /api/workspace/v1/push_devices/{registration_uuid}` |  [`delete_push_device.md`](content/operations/delete_push_device.md)  |  [`delete_push_device.puml`](content/operations/diagrams/delete_push_device.puml)  |  [`delete_push_device.svg`](content/operations/diagrams/delete_push_device.svg)  |

### Users and `me`

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/me/` |  [`get_me.md`](content/operations/get_me.md)  |  [`get_me.puml`](content/operations/diagrams/get_me.puml)  |  [`get_me.svg`](content/operations/diagrams/get_me.svg)  |
| `GET /api/workspace/v1/users/` |  [`get_users_list.md`](content/operations/get_users_list.md)  |  [`get_users_list.puml`](content/operations/diagrams/get_users_list.puml)  |  [`get_users_list.svg`](content/operations/diagrams/get_users_list.svg)  |
| `GET /api/workspace/v1/users/{user_uuid}` |  [`get_user.md`](content/operations/get_user.md)  |  [`get_user.puml`](content/operations/diagrams/get_user.puml)  |  [`get_user.svg`](content/operations/diagrams/get_user.svg)  |
| `POST /api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` |  [`post_user_avatar_reset.md`](content/operations/post_user_avatar_reset.md)  |  [`post_user_avatar_reset.puml`](content/operations/diagrams/post_user_avatar_reset.puml)  |  [`post_user_avatar_reset.svg`](content/operations/diagrams/post_user_avatar_reset.svg)  |
| `POST /api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` |  [`post_user_avatar_upload.md`](content/operations/post_user_avatar_upload.md)  |  [`post_user_avatar_upload.puml`](content/operations/diagrams/post_user_avatar_upload.puml)  |  [`post_user_avatar_upload.svg`](content/operations/diagrams/post_user_avatar_upload.svg)  |
| `POST /api/workspace/v1/users/{user_uuid}/actions/presence/invoke` |  [`post_user_presence.md`](content/operations/post_user_presence.md)  |  [`post_user_presence.puml`](content/operations/diagrams/post_user_presence.puml)  |  [`post_user_presence.svg`](content/operations/diagrams/post_user_presence.svg)  |

### Streams

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/streams/` |  [`get_streams_list.md`](core/operations/get_streams_list.md)  |  [`get_streams_list.puml`](core/operations/diagrams/get_streams_list.puml)  |  [`get_streams_list.svg`](core/operations/diagrams/get_streams_list.svg)  |
| `POST /api/workspace/v1/messenger/streams/` |  [`post_streams_create.md`](core/operations/post_streams_create.md)  |  [`post_streams_create.puml`](core/operations/diagrams/post_streams_create.puml)  |  [`post_streams_create.svg`](core/operations/diagrams/post_streams_create.svg)  |
| `GET /api/workspace/v1/messenger/streams/{stream_uuid}` |  [`get_stream.md`](core/operations/get_stream.md)  |  [`get_stream.puml`](core/operations/diagrams/get_stream.puml)  |  [`get_stream.svg`](core/operations/diagrams/get_stream.svg)  |
| `PUT /api/workspace/v1/messenger/streams/{stream_uuid}` |  [`put_stream.md`](core/operations/put_stream.md)  |  [`put_stream.puml`](core/operations/diagrams/put_stream.puml)  |  [`put_stream.svg`](core/operations/diagrams/put_stream.svg)  |
| `DELETE /api/workspace/v1/messenger/streams/{stream_uuid}` |  [`delete_stream.md`](core/operations/delete_stream.md)  |  [`delete_stream.puml`](core/operations/diagrams/delete_stream.puml)  |  [`delete_stream.svg`](core/operations/diagrams/delete_stream.svg)  |
| `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` |  [`post_stream_add_users_action.md`](core/operations/post_stream_add_users_action.md)  |  [`post_stream_add_users_action.puml`](core/operations/diagrams/post_stream_add_users_action.puml)  |  [`post_stream_add_users_action.svg`](core/operations/diagrams/post_stream_add_users_action.svg)  |
| `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` |  [`post_stream_archive_action.md`](core/operations/post_stream_archive_action.md)  |  [`post_stream_archive_action.puml`](core/operations/diagrams/post_stream_archive_action.puml)  |  [`post_stream_archive_action.svg`](core/operations/diagrams/post_stream_archive_action.svg)  |
| `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` |  [`post_stream_notifications_action.md`](core/operations/post_stream_notifications_action.md)  |  [`post_stream_notifications_action.puml`](core/operations/diagrams/post_stream_notifications_action.puml)  |  [`post_stream_notifications_action.svg`](core/operations/diagrams/post_stream_notifications_action.svg)  |
| `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` |  [`post_stream_read_action.md`](core/operations/post_stream_read_action.md)  |  [`post_stream_read_action.puml`](core/operations/diagrams/post_stream_read_action.puml)  |  [`post_stream_read_action.svg`](core/operations/diagrams/post_stream_read_action.svg)  |
| `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` |  [`post_stream_unarchive_action.md`](core/operations/post_stream_unarchive_action.md)  |  [`post_stream_unarchive_action.puml`](core/operations/diagrams/post_stream_unarchive_action.puml)  |  [`post_stream_unarchive_action.svg`](core/operations/diagrams/post_stream_unarchive_action.svg)  |

### String attachments

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/stream_bindings/` |  [`get_stream_bindings_list.md`](core/operations/get_stream_bindings_list.md)  |  [`get_stream_bindings_list.puml`](core/operations/diagrams/get_stream_bindings_list.puml)  |  [`get_stream_bindings_list.svg`](core/operations/diagrams/get_stream_bindings_list.svg)  |
| `GET /api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [`get_stream_binding.md`](core/operations/get_stream_binding.md)  |  [`get_stream_binding.puml`](core/operations/diagrams/get_stream_binding.puml)  |  [`get_stream_binding.svg`](core/operations/diagrams/get_stream_binding.svg)  |
| `PUT /api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [`put_stream_binding.md`](core/operations/put_stream_binding.md)  |  [`put_stream_binding.puml`](core/operations/diagrams/put_stream_binding.puml)  |  [`put_stream_binding.svg`](core/operations/diagrams/put_stream_binding.svg)  |
| `DELETE /api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [`delete_stream_binding.md`](core/operations/delete_stream_binding.md)  |  [`delete_stream_binding.puml`](core/operations/diagrams/delete_stream_binding.puml)  |  [`delete_stream_binding.svg`](core/operations/diagrams/delete_stream_binding.svg)  |

### Streaming topics

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/stream_topics/` |  [`get_stream_topics_list.md`](core/operations/get_stream_topics_list.md)  |  [`get_stream_topics_list.puml`](core/operations/diagrams/get_stream_topics_list.puml)  |  [`get_stream_topics_list.svg`](core/operations/diagrams/get_stream_topics_list.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/` |  [`post_stream_topics_create.md`](core/operations/post_stream_topics_create.md)  |  [`post_stream_topics_create.puml`](core/operations/diagrams/post_stream_topics_create.puml)  |  [`post_stream_topics_create.svg`](core/operations/diagrams/post_stream_topics_create.svg)  |
| `GET /api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [`get_stream_topic.md`](core/operations/get_stream_topic.md)  |  [`get_stream_topic.puml`](core/operations/diagrams/get_stream_topic.puml)  |  [`get_stream_topic.svg`](core/operations/diagrams/get_stream_topic.svg)  |
| `PUT /api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [`put_stream_topic.md`](core/operations/put_stream_topic.md)  |  [`put_stream_topic.puml`](core/operations/diagrams/put_stream_topic.puml)  |  [`put_stream_topic.svg`](core/operations/diagrams/put_stream_topic.svg)  |
| `DELETE /api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [`delete_stream_topic.md`](core/operations/delete_stream_topic.md)  |  [`delete_stream_topic.puml`](core/operations/diagrams/delete_stream_topic.puml)  |  [`delete_stream_topic.svg`](core/operations/diagrams/delete_stream_topic.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` |  [`post_topic_notifications_action.md`](core/operations/post_topic_notifications_action.md)  |  [`post_topic_notifications_action.puml`](core/operations/diagrams/post_topic_notifications_action.puml)  |  [`post_topic_notifications_action.svg`](core/operations/diagrams/post_topic_notifications_action.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` |  [`post_topic_read_action.md`](core/operations/post_topic_read_action.md)  |  [`post_topic_read_action.puml`](core/operations/diagrams/post_topic_read_action.puml)  |  [`post_topic_read_action.svg`](core/operations/diagrams/post_topic_read_action.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` |  [`post_topic_set_default_action.md`](core/operations/post_topic_set_default_action.md)  |  [`post_topic_set_default_action.puml`](core/operations/diagrams/post_topic_set_default_action.puml)  |  [`post_topic_set_default_action.svg`](core/operations/diagrams/post_topic_set_default_action.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` |  [`post_topic_set_summary_prompt_action.md`](core/operations/post_topic_set_summary_prompt_action.md)  |  [`post_topic_set_summary_prompt_action.puml`](core/operations/diagrams/post_topic_set_summary_prompt_action.puml)  |  [`post_topic_set_summary_prompt_action.svg`](core/operations/diagrams/post_topic_set_summary_prompt_action.svg)  |
| `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` |  [`post_topic_toggle_done_action.md`](core/operations/post_topic_toggle_done_action.md)  |  [`post_topic_toggle_done_action.puml`](core/operations/diagrams/post_topic_toggle_done_action.puml)  |  [`post_topic_toggle_done_action.svg`](core/operations/diagrams/post_topic_toggle_done_action.svg)  |

### The messages

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/messages/` |  [`get_messages_list.md`](core/operations/get_messages_list.md)  |  [`get_messages_list.puml`](core/operations/diagrams/get_messages_list.puml)  |  [`get_messages_list.svg`](core/operations/diagrams/get_messages_list.svg)  |
| `POST /api/workspace/v1/messenger/messages/` |  [`post_messages_create.md`](core/operations/post_messages_create.md)  |  [`post_messages_create.puml`](core/operations/diagrams/post_messages_create.puml)  |  [`post_messages_create.svg`](core/operations/diagrams/post_messages_create.svg)  |
| `GET /api/workspace/v1/messenger/messages/{message_uuid}` |  [`get_message.md`](core/operations/get_message.md)  |  [`get_message.puml`](core/operations/diagrams/get_message.puml)  |  [`get_message.svg`](core/operations/diagrams/get_message.svg)  |
| `PUT /api/workspace/v1/messenger/messages/{message_uuid}` |  [`put_message.md`](core/operations/put_message.md)  |  [`put_message.puml`](core/operations/diagrams/put_message.puml)  |  [`put_message.svg`](core/operations/diagrams/put_message.svg)  |
| `DELETE /api/workspace/v1/messenger/messages/{message_uuid}` |  [`delete_message.md`](core/operations/delete_message.md)  |  [`delete_message.puml`](core/operations/diagrams/delete_message.puml)  |  [`delete_message.svg`](core/operations/diagrams/delete_message.svg)  |
| `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` |  [`post_message_read_action.md`](core/operations/post_message_read_action.md)  |  [`post_message_read_action.puml`](core/operations/diagrams/post_message_read_action.puml)  |  [`post_message_read_action.svg`](core/operations/diagrams/post_message_read_action.svg)  |
| `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` |  [`post_message_read_up_to_action.md`](core/operations/post_message_read_up_to_action.md)  |  [`post_message_read_up_to_action.puml`](core/operations/diagrams/post_message_read_up_to_action.puml)  |  [`post_message_read_up_to_action.svg`](core/operations/diagrams/post_message_read_up_to_action.svg)  |
| `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` |  [`post_message_star_action.md`](core/operations/post_message_star_action.md)  |  [`post_message_star_action.puml`](core/operations/diagrams/post_message_star_action.puml)  |  [`post_message_star_action.svg`](core/operations/diagrams/post_message_star_action.svg)  |
| `POST /api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` |  [`post_message_unstar_action.md`](core/operations/post_message_unstar_action.md)  |  [`post_message_unstar_action.puml`](core/operations/diagrams/post_message_unstar_action.puml)  |  [`post_message_unstar_action.svg`](core/operations/diagrams/post_message_unstar_action.svg)  |

### Reactions to messages

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/message_reactions/` |  [`get_message_reactions_list.md`](core/operations/get_message_reactions_list.md)  |  [`get_message_reactions_list.puml`](core/operations/diagrams/get_message_reactions_list.puml)  |  [`get_message_reactions_list.svg`](core/operations/diagrams/get_message_reactions_list.svg)  |
| `POST /api/workspace/v1/messenger/message_reactions/` |  [`post_message_reactions_create.md`](core/operations/post_message_reactions_create.md)  |  [`post_message_reactions_create.puml`](core/operations/diagrams/post_message_reactions_create.puml)  |  [`post_message_reactions_create.svg`](core/operations/diagrams/post_message_reactions_create.svg)  |
| `GET /api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [`get_message_reaction.md`](core/operations/get_message_reaction.md)  |  [`get_message_reaction.puml`](core/operations/diagrams/get_message_reaction.puml)  |  [`get_message_reaction.svg`](core/operations/diagrams/get_message_reaction.svg)  |
| `PUT /api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [`put_message_reaction.md`](core/operations/put_message_reaction.md)  |  [`put_message_reaction.puml`](core/operations/diagrams/put_message_reaction.puml)  |  [`put_message_reaction.svg`](core/operations/diagrams/put_message_reaction.svg)  |
| `DELETE /api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [`delete_message_reaction.md`](core/operations/delete_message_reaction.md)  |  [`delete_message_reaction.puml`](core/operations/diagrams/delete_message_reaction.puml)  |  [`delete_message_reaction.svg`](core/operations/diagrams/delete_message_reaction.svg)  |

### Endpoints of the topic summaries

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/topic_summary_endpoints/` |  [`get_topic_summary_endpoints_list.md`](core/operations/get_topic_summary_endpoints_list.md)  |  [`get_topic_summary_endpoints_list.puml`](core/operations/diagrams/get_topic_summary_endpoints_list.puml)  |  [`get_topic_summary_endpoints_list.svg`](core/operations/diagrams/get_topic_summary_endpoints_list.svg)  |
| `POST /api/workspace/v1/messenger/topic_summary_endpoints/` |  [`post_topic_summary_endpoints_create.md`](core/operations/post_topic_summary_endpoints_create.md)  |  [`post_topic_summary_endpoints_create.puml`](core/operations/diagrams/post_topic_summary_endpoints_create.puml)  |  [`post_topic_summary_endpoints_create.svg`](core/operations/diagrams/post_topic_summary_endpoints_create.svg)  |
| `GET /api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [`get_topic_summary_endpoint.md`](core/operations/get_topic_summary_endpoint.md)  |  [`get_topic_summary_endpoint.puml`](core/operations/diagrams/get_topic_summary_endpoint.puml)  |  [`get_topic_summary_endpoint.svg`](core/operations/diagrams/get_topic_summary_endpoint.svg)  |
| `PUT /api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [`put_topic_summary_endpoint.md`](core/operations/put_topic_summary_endpoint.md)  |  [`put_topic_summary_endpoint.puml`](core/operations/diagrams/put_topic_summary_endpoint.puml)  |  [`put_topic_summary_endpoint.svg`](core/operations/diagrams/put_topic_summary_endpoint.svg)  |
| `DELETE /api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [`delete_topic_summary_endpoint.md`](core/operations/delete_topic_summary_endpoint.md)  |  [`delete_topic_summary_endpoint.puml`](core/operations/diagrams/delete_topic_summary_endpoint.puml)  |  [`delete_topic_summary_endpoint.svg`](core/operations/diagrams/delete_topic_summary_endpoint.svg)  |

### Setup of the topic summary

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [`get_topic_summary_settings.md`](core/operations/get_topic_summary_settings.md)  |  [`get_topic_summary_settings.puml`](core/operations/diagrams/get_topic_summary_settings.puml)  |  [`get_topic_summary_settings.svg`](core/operations/diagrams/get_topic_summary_settings.svg)  |
| `PUT /api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [`put_topic_summary_settings.md`](core/operations/put_topic_summary_settings.md)  |  [`put_topic_summary_settings.puml`](core/operations/diagrams/put_topic_summary_settings.puml)  |  [`put_topic_summary_settings.svg`](core/operations/diagrams/put_topic_summary_settings.svg)  |

### Events and epoch

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/epoch/` |  [`get_epoch.md`](external/operations/get_epoch.md)  |  [`get_epoch.puml`](external/operations/diagrams/get_epoch.puml)  |  [`get_epoch.svg`](external/operations/diagrams/get_epoch.svg)  |
| `GET /api/workspace/v1/events/` |  [`get_events.md`](external/operations/get_events.md)  |  [`get_events.puml`](external/operations/diagrams/get_events.puml)  |  [`get_events.svg`](external/operations/diagrams/get_events.svg)  |

### External accounts

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_accounts/` |  [`get_external_accounts.md`](external/operations/get_external_accounts.md)  |  [`get_external_accounts.puml`](external/operations/diagrams/get_external_accounts.puml)  |  [`get_external_accounts.svg`](external/operations/diagrams/get_external_accounts.svg)  |
| `POST /api/workspace/v1/messenger/external_accounts/` |  [`post_external_accounts.md`](external/operations/post_external_accounts.md)  |  [`post_external_accounts.puml`](external/operations/diagrams/post_external_accounts.puml)  |  [`post_external_accounts.svg`](external/operations/diagrams/post_external_accounts.svg)  |
| `GET /api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [`get_external_account.md`](external/operations/get_external_account.md)  |  [`get_external_account.puml`](external/operations/diagrams/get_external_account.puml)  |  [`get_external_account.svg`](external/operations/diagrams/get_external_account.svg)  |
| `PUT /api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [`put_external_account.md`](external/operations/put_external_account.md)  |  [`put_external_account.puml`](external/operations/diagrams/put_external_account.puml)  |  [`put_external_account.svg`](external/operations/diagrams/put_external_account.svg)  |
| `DELETE /api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [`delete_external_account.md`](external/operations/delete_external_account.md)  |  [`delete_external_account.puml`](external/operations/diagrams/delete_external_account.puml)  |  [`delete_external_account.svg`](external/operations/diagrams/delete_external_account.svg)  |
| `POST /api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/disconnect/invoke` |  [`post_external_account_disconnect.md`](external/operations/post_external_account_disconnect.md)  |  [`post_external_account_disconnect.puml`](external/operations/diagrams/post_external_account_disconnect.puml)  |  [`post_external_account_disconnect.svg`](external/operations/diagrams/post_external_account_disconnect.svg)  |
| `POST /api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke` |  [`post_external_account_reconnect.md`](external/operations/post_external_account_reconnect.md)  |  [`post_external_account_reconnect.puml`](external/operations/diagrams/post_external_account_reconnect.puml)  |  [`post_external_account_reconnect.svg`](external/operations/diagrams/post_external_account_reconnect.svg)  |

### External chats

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_chats/` |  [`get_external_chats.md`](external/operations/get_external_chats.md)  |  [`get_external_chats.puml`](external/operations/diagrams/get_external_chats.puml)  |  [`get_external_chats.svg`](external/operations/diagrams/get_external_chats.svg)  |
| `GET /api/workspace/v1/messenger/external_chats/{chat_uuid}` |  [`get_external_chat.md`](external/operations/get_external_chat.md)  |  [`get_external_chat.puml`](external/operations/diagrams/get_external_chat.puml)  |  [`get_external_chat.svg`](external/operations/diagrams/get_external_chat.svg)  |
| `POST /api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/deselect/invoke` |  [`post_external_chat_deselect.md`](external/operations/post_external_chat_deselect.md)  |  [`post_external_chat_deselect.puml`](external/operations/diagrams/post_external_chat_deselect.puml)  |  [`post_external_chat_deselect.svg`](external/operations/diagrams/post_external_chat_deselect.svg)  |
| `POST /api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/move/invoke` |  [`post_external_chat_move.md`](external/operations/post_external_chat_move.md)  |  [`post_external_chat_move.puml`](external/operations/diagrams/post_external_chat_move.puml)  |  [`post_external_chat_move.svg`](external/operations/diagrams/post_external_chat_move.svg)  |
| `POST /api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/select/invoke` |  [`post_external_chat_select.md`](external/operations/post_external_chat_select.md)  |  [`post_external_chat_select.puml`](external/operations/diagrams/post_external_chat_select.puml)  |  [`post_external_chat_select.svg`](external/operations/diagrams/post_external_chat_select.svg)  |

### External operations

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_operations/` |  [`get_external_operations.md`](external/operations/get_external_operations.md)  |  [`get_external_operations.puml`](external/operations/diagrams/get_external_operations.puml)  |  [`get_external_operations.svg`](external/operations/diagrams/get_external_operations.svg)  |
| `POST /api/workspace/v1/messenger/external_operations/actions/preflight/invoke` |  [`post_external_operation_preflight.md`](external/operations/post_external_operation_preflight.md)  |  [`post_external_operation_preflight.puml`](external/operations/diagrams/post_external_operation_preflight.puml)  |  [`post_external_operation_preflight.svg`](external/operations/diagrams/post_external_operation_preflight.svg)  |
| `GET /api/workspace/v1/messenger/external_operations/{operation_uuid}` |  [`get_external_operation.md`](external/operations/get_external_operation.md)  |  [`get_external_operation.puml`](external/operations/diagrams/get_external_operation.puml)  |  [`get_external_operation.svg`](external/operations/diagrams/get_external_operation.svg)  |
| `DELETE /api/workspace/v1/messenger/external_operations/{operation_uuid}` |  [`delete_external_operation.md`](external/operations/delete_external_operation.md)  |  [`delete_external_operation.puml`](external/operations/diagrams/delete_external_operation.puml)  |  [`delete_external_operation.svg`](external/operations/diagrams/delete_external_operation.svg)  |
| `POST /api/workspace/v1/messenger/external_operations/{operation_uuid}/actions/retry/invoke` |  [`post_external_operation_retry.md`](external/operations/post_external_operation_retry.md)  |  [`post_external_operation_retry.puml`](external/operations/diagrams/post_external_operation_retry.puml)  |  [`post_external_operation_retry.svg`](external/operations/diagrams/post_external_operation_retry.svg)  |

### External bridge copies

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_bridge_instances/` |  [`get_external_bridge_instances.md`](external/operations/get_external_bridge_instances.md)  |  [`get_external_bridge_instances.puml`](external/operations/diagrams/get_external_bridge_instances.puml)  |  [`get_external_bridge_instances.svg`](external/operations/diagrams/get_external_bridge_instances.svg)  |
| `GET /api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}` |  [`get_external_bridge_instance.md`](external/operations/get_external_bridge_instance.md)  |  [`get_external_bridge_instance.puml`](external/operations/diagrams/get_external_bridge_instance.puml)  |  [`get_external_bridge_instance.svg`](external/operations/diagrams/get_external_bridge_instance.svg)  |
| `POST /api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/resume/invoke` |  [`post_external_bridge_instance_resume.md`](external/operations/post_external_bridge_instance_resume.md)  |  [`post_external_bridge_instance_resume.puml`](external/operations/diagrams/post_external_bridge_instance_resume.puml)  |  [`post_external_bridge_instance_resume.svg`](external/operations/diagrams/post_external_bridge_instance_resume.svg)  |
| `POST /api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/revoke/invoke` |  [`post_external_bridge_instance_revoke.md`](external/operations/post_external_bridge_instance_revoke.md)  |  [`post_external_bridge_instance_revoke.puml`](external/operations/diagrams/post_external_bridge_instance_revoke.puml)  |  [`post_external_bridge_instance_revoke.svg`](external/operations/diagrams/post_external_bridge_instance_revoke.svg)  |
| `POST /api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` |  [`post_external_bridge_instance_suspend.md`](external/operations/post_external_bridge_instance_suspend.md)  |  [`post_external_bridge_instance_suspend.puml`](external/operations/diagrams/post_external_bridge_instance_suspend.puml)  |  [`post_external_bridge_instance_suspend.svg`](external/operations/diagrams/post_external_bridge_instance_suspend.svg)  |

### Policy of external providers

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_provider_policies/{kind}` |  [`get_external_provider_policy.md`](external/operations/get_external_provider_policy.md)  |  [`get_external_provider_policy.puml`](external/operations/diagrams/get_external_provider_policy.puml)  |  [`get_external_provider_policy.svg`](external/operations/diagrams/get_external_provider_policy.svg)  |
| `PUT /api/workspace/v1/messenger/external_provider_policies/{kind}` |  [`put_external_provider_policy.md`](external/operations/put_external_provider_policy.md)  |  [`put_external_provider_policy.puml`](external/operations/diagrams/put_external_provider_policy.puml)  |  [`put_external_provider_policy.svg`](external/operations/diagrams/put_external_provider_policy.svg)  |
| `POST /api/workspace/v1/messenger/external_provider_policies/{kind}/actions/resume/invoke` |  [`post_external_provider_policy_resume.md`](external/operations/post_external_provider_policy_resume.md)  |  [`post_external_provider_policy_resume.puml`](external/operations/diagrams/post_external_provider_policy_resume.puml)  |  [`post_external_provider_policy_resume.svg`](external/operations/diagrams/post_external_provider_policy_resume.svg)  |
| `POST /api/workspace/v1/messenger/external_provider_policies/{kind}/actions/suspend/invoke` |  [`post_external_provider_policy_suspend.md`](external/operations/post_external_provider_policy_suspend.md)  |  [`post_external_provider_policy_suspend.puml`](external/operations/diagrams/post_external_provider_policy_suspend.puml)  |  [`post_external_provider_policy_suspend.svg`](external/operations/diagrams/post_external_provider_policy_suspend.svg)  |

### Status of the external providers

| Method + path | Markdown operations | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/messenger/external_provider_health/{kind}` |  [`get_external_provider_health.md`](external/operations/get_external_provider_health.md)  |  [`get_external_provider_health.puml`](external/operations/diagrams/get_external_provider_health.puml)  |  [`get_external_provider_health.svg`](external/operations/diagrams/get_external_provider_health.svg)  |

## WebSocket The events

| The entry point of the execution time | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| `GET /api/workspace/v1/events/ws` (WebSocket and execution time stream) |  [`websocket_events.md`](external/operations/websocket_events.md)  |  [`websocket_events.puml`](external/operations/diagrams/websocket_events.puml)  |  [`websocket_events.svg`](external/operations/diagrams/websocket_events.svg)  |

## The background streams of the worker

| The stream | Markdown | PlantUML | SVG |
| --- | --- | --- | --- |
| The architecture of the worker |  [`worker_architecture.md`](worker_flows/worker_architecture.md)  |  [`worker_architecture.puml`](worker_flows/diagrams/worker_architecture.puml)  |  [`worker_architecture.svg`](worker_flows/diagrams/worker_architecture.svg)  |
| Distribution of recipients (fan-out) |  [`task_fanout.md`](worker_flows/task_fanout.md)  |  [`task_fanout.puml`](worker_flows/diagrams/task_fanout.puml)  |  [`task_fanout.svg`](worker_flows/diagrams/task_fanout.svg)  |
| Content and mentions |  [`task_content_mentions.md`](worker_flows/task_content_mentions.md)  |  [`task_content_mentions.puml`](worker_flows/diagrams/task_content_mentions.puml)  |  [`task_content_mentions.svg`](worker_flows/diagrams/task_content_mentions.svg)  |
| Pictures of the reaction |  [`task_reaction_snapshot.md`](worker_flows/task_reaction_snapshot.md)  |  [`task_reaction_snapshot.puml`](worker_flows/diagrams/task_reaction_snapshot.puml)  |  [`task_reaction_snapshot.svg`](worker_flows/diagrams/task_reaction_snapshot.svg)  |
| Container read and count |  [`task_read_counters.md`](worker_flows/task_read_counters.md)  |  [`task_read_counters.puml`](worker_flows/diagrams/task_read_counters.puml)  |  [`task_read_counters.svg`](worker_flows/diagrams/task_read_counters.svg)  |
| Delivery photos and ready events |  [`task_delivery_snapshot_event.md`](worker_flows/task_delivery_snapshot_event.md)  |  [`task_delivery_snapshot_event.puml`](worker_flows/diagrams/task_delivery_snapshot_event.puml)  |  [`task_delivery_snapshot_event.svg`](worker_flows/diagrams/task_delivery_snapshot_event.svg)  |
| Reorganizing membership and policy topic |  [`task_topic_membership_policy_rebuild.md`](worker_flows/task_topic_membership_policy_rebuild.md)  |  [`task_topic_membership_policy_rebuild.puml`](worker_flows/diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](worker_flows/diagrams/task_topic_membership_policy_rebuild.svg)  |
| Project the canonical state of the topic |  [`task_topic_membership_policy_rebuild.md`](worker_flows/task_topic_membership_policy_rebuild.md#topic_state_projection)  |  [`task_topic_membership_policy_rebuild.puml`](worker_flows/diagrams/task_topic_membership_policy_rebuild.puml)  |  [`task_topic_membership_policy_rebuild.svg`](worker_flows/diagrams/task_topic_membership_policy_rebuild.svg)  |
| Runbook Migration and release |  [`migration_release_runbook.md`](worker_flows/migration_release_runbook.md)  |  [`migration_release_runbook.puml`](worker_flows/diagrams/migration_release_runbook.puml)  |  [`migration_release_runbook.svg`](worker_flows/diagrams/migration_release_runbook.svg)  |

## Navigation and boundaries

In each Markdown file, the same navigation bar is at the top and bottom of the operation, family, or worker. [`workspace_api.md`](../../workspace_api.md); The sequence specifications are a design proposal that starts with the documentation and does not allow for implementation in the production code..

[← The main index of the documentation](../../index.md) · [Background stream section](worker_flows/README.md) · [Current API contract](../../workspace_api.md)
