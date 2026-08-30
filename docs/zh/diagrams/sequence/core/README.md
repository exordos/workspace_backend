[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [部分 Core Messenger](README.md)

# 操作序列的规格 Core Workspace API

这份目录在docs-first的方法中涵盖了所有当前的 HTTP 操作,消息家族,对消息的反应,流,绑定流,流主题和管理主题摘要.当前的公共合同仍然是 [`workspace_api.md`](../../../workspace_api.md).

每对 HTTP 方法/路径都设有单独的操作文件和可编辑/转向的 PlantUML 序列图.操作文件将同步交易工作RestAlchemy与不可变的类型任务分开: topic ownership 仅用于 topic-scoped placements/bindings,而 shared projections 具有自己的 exact scopes. WebSocket.

分区术语:binding 绑定,placement 放置,outbox 交易式outbox,projection 投影,fan-out 分配 (fan-out),worker 背景表演者.

操作的通用目标变量/direct: physical `STREAM.owner_uuid`
仍然是FK,公开字段的索引 `owner` — scalar UUID property.
公开 `direct_user_uuid` 查看者-相对: 拥有者查看 physical
`direct_user_uuid`, 第二个参与者看到`owner_uuid`,自动聊天看到自己的 UUID.
这是一个超标 `CASE` 超过可定流 + 现有 `USER_STREAM_BINDING` 和
适用于 list/get/event任何直播都会有
`private=true` 和强制性的 canonical/technical `TOPIC`; self-chat 有一个
membership row 并且没有通过第二个binding/state fan-out.

| 方法 | 路径 | 规格 | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [操作](operations/delete_message_reaction.md)  |  [PUML](operations/diagrams/delete_message_reaction.puml)  |  [SVG](operations/diagrams/delete_message_reaction.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [操作](operations/delete_message.md)  |  [PUML](operations/diagrams/delete_message.puml)  |  [SVG](operations/diagrams/delete_message.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [操作](operations/delete_stream_binding.md)  |  [PUML](operations/diagrams/delete_stream_binding.puml)  |  [SVG](operations/diagrams/delete_stream_binding.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [操作](operations/delete_stream_topic.md)  |  [PUML](operations/diagrams/delete_stream_topic.puml)  |  [SVG](operations/diagrams/delete_stream_topic.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [操作](operations/delete_stream.md)  |  [PUML](operations/diagrams/delete_stream.puml)  |  [SVG](operations/diagrams/delete_stream.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [操作](operations/delete_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/delete_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/delete_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` |  [操作](operations/get_message_reactions_list.md)  |  [PUML](operations/diagrams/get_message_reactions_list.puml)  |  [SVG](operations/diagrams/get_message_reactions_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [操作](operations/get_message_reaction.md)  |  [PUML](operations/diagrams/get_message_reaction.puml)  |  [SVG](operations/diagrams/get_message_reaction.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/` |  [操作](operations/get_messages_list.md)  |  [PUML](operations/diagrams/get_messages_list.puml)  |  [SVG](operations/diagrams/get_messages_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [操作](operations/get_message.md)  |  [PUML](operations/diagrams/get_message.puml)  |  [SVG](operations/diagrams/get_message.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` |  [操作](operations/get_stream_bindings_list.md)  |  [PUML](operations/diagrams/get_stream_bindings_list.puml)  |  [SVG](operations/diagrams/get_stream_bindings_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [操作](operations/get_stream_binding.md)  |  [PUML](operations/diagrams/get_stream_binding.puml)  |  [SVG](operations/diagrams/get_stream_binding.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` |  [操作](operations/get_stream_topics_list.md)  |  [PUML](operations/diagrams/get_stream_topics_list.puml)  |  [SVG](operations/diagrams/get_stream_topics_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [操作](operations/get_stream_topic.md)  |  [PUML](operations/diagrams/get_stream_topic.puml)  |  [SVG](operations/diagrams/get_stream_topic.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/` |  [操作](operations/get_streams_list.md)  |  [PUML](operations/diagrams/get_streams_list.puml)  |  [SVG](operations/diagrams/get_streams_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [操作](operations/get_stream.md)  |  [PUML](operations/diagrams/get_stream.puml)  |  [SVG](operations/diagrams/get_stream.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [操作](operations/get_topic_summary_endpoints_list.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoints_list.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoints_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [操作](operations/get_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [操作](operations/get_topic_summary_settings.md)  |  [PUML](operations/diagrams/get_topic_summary_settings.puml)  |  [SVG](operations/diagrams/get_topic_summary_settings.svg)  |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` |  [操作](operations/post_message_reactions_create.md)  |  [PUML](operations/diagrams/post_message_reactions_create.puml)  |  [SVG](operations/diagrams/post_message_reactions_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` |  [操作](operations/post_message_read_action.md)  |  [PUML](operations/diagrams/post_message_read_action.puml)  |  [SVG](operations/diagrams/post_message_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` |  [操作](operations/post_message_read_up_to_action.md)  |  [PUML](operations/diagrams/post_message_read_up_to_action.puml)  |  [SVG](operations/diagrams/post_message_read_up_to_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` |  [操作](operations/post_message_star_action.md)  |  [PUML](operations/diagrams/post_message_star_action.puml)  |  [SVG](operations/diagrams/post_message_star_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` |  [操作](operations/post_message_unstar_action.md)  |  [PUML](operations/diagrams/post_message_unstar_action.puml)  |  [SVG](operations/diagrams/post_message_unstar_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/` |  [操作](operations/post_messages_create.md)  |  [PUML](operations/diagrams/post_messages_create.puml)  |  [SVG](operations/diagrams/post_messages_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` |  [操作](operations/post_stream_add_users_action.md)  |  [PUML](operations/diagrams/post_stream_add_users_action.puml)  |  [SVG](operations/diagrams/post_stream_add_users_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` |  [操作](operations/post_stream_archive_action.md)  |  [PUML](operations/diagrams/post_stream_archive_action.puml)  |  [SVG](operations/diagrams/post_stream_archive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` |  [操作](operations/post_stream_notifications_action.md)  |  [PUML](operations/diagrams/post_stream_notifications_action.puml)  |  [SVG](operations/diagrams/post_stream_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` |  [操作](operations/post_stream_read_action.md)  |  [PUML](operations/diagrams/post_stream_read_action.puml)  |  [SVG](operations/diagrams/post_stream_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` |  [操作](operations/post_stream_topics_create.md)  |  [PUML](operations/diagrams/post_stream_topics_create.puml)  |  [SVG](operations/diagrams/post_stream_topics_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` |  [操作](operations/post_stream_unarchive_action.md)  |  [PUML](operations/diagrams/post_stream_unarchive_action.puml)  |  [SVG](operations/diagrams/post_stream_unarchive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/` |  [操作](operations/post_streams_create.md)  |  [PUML](operations/diagrams/post_streams_create.puml)  |  [SVG](operations/diagrams/post_streams_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` |  [操作](operations/post_topic_notifications_action.md)  |  [PUML](operations/diagrams/post_topic_notifications_action.puml)  |  [SVG](operations/diagrams/post_topic_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` |  [操作](operations/post_topic_read_action.md)  |  [PUML](operations/diagrams/post_topic_read_action.puml)  |  [SVG](operations/diagrams/post_topic_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` |  [操作](operations/post_topic_set_default_action.md)  |  [PUML](operations/diagrams/post_topic_set_default_action.puml)  |  [SVG](operations/diagrams/post_topic_set_default_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` |  [操作](operations/post_topic_set_summary_prompt_action.md)  |  [PUML](operations/diagrams/post_topic_set_summary_prompt_action.puml)  |  [SVG](operations/diagrams/post_topic_set_summary_prompt_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [操作](operations/post_topic_summary_endpoints_create.md)  |  [PUML](operations/diagrams/post_topic_summary_endpoints_create.puml)  |  [SVG](operations/diagrams/post_topic_summary_endpoints_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` |  [操作](operations/post_topic_toggle_done_action.md)  |  [PUML](operations/diagrams/post_topic_toggle_done_action.puml)  |  [SVG](operations/diagrams/post_topic_toggle_done_action.svg)  |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [操作](operations/put_message_reaction.md)  |  [PUML](operations/diagrams/put_message_reaction.puml)  |  [SVG](operations/diagrams/put_message_reaction.svg)  |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [操作](operations/put_message.md)  |  [PUML](operations/diagrams/put_message.puml)  |  [SVG](operations/diagrams/put_message.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [操作](operations/put_stream_binding.md)  |  [PUML](operations/diagrams/put_stream_binding.puml)  |  [SVG](operations/diagrams/put_stream_binding.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [操作](operations/put_stream_topic.md)  |  [PUML](operations/diagrams/put_stream_topic.puml)  |  [SVG](operations/diagrams/put_stream_topic.svg)  |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [操作](operations/put_stream.md)  |  [PUML](operations/diagrams/put_stream.puml)  |  [SVG](operations/diagrams/put_stream.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [操作](operations/put_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/put_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/put_topic_summary_endpoint.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [操作](operations/put_topic_summary_settings.md)  |  [PUML](operations/diagrams/put_topic_summary_settings.puml)  |  [SVG](operations/diagrams/put_topic_summary_settings.svg)  |

覆盖范围: **45个 HTTP操作**.

[← 文件的主要索引](../../../index.md) · [序列图表的索引](../README.md) · [部分 Core Messenger](README.md)
