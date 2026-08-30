[← The main index of the documentation](../../../index.md) · [Index of sequence diagrams](../README.md) · [The section Core Messenger](README.md)

# Specifications of the sequence of operations Core Workspace API

This directory in the docs-first approach covers all current HTTP operations of message families, message responses, flows, binding flows, flow topics and administering summaries of topics. [`workspace_api.md`](../../../workspace_api.md).

For each pair HTTP-method/path, a separate operation document and an editable/rendered PlantUML sequence diagram are provided. Operations documents separate synchronous transaction work RestAlchemy from immutable typed tasks: topic ownership is used only for topic-scoped placements/bindings, and shared projections have their own exact scopes. Ready public events are delivered by a separate dispatcher WebSocket.

The terminology of the section is: binding  binding, placement  placement, outbox  transactional outbox, projection  projection, fan-out  distribution (fan-out), worker  background performer.

The common target-invariants of the stream/direct operations: physical `STREAM.owner_uuid`
remains indexed FK, public field `owner` — scalar UUID property.
Public `direct_user_uuid` viewer-relative: owner sees physical
`direct_user_uuid`, The second participant sees `owner_uuid`, the self-chat sees his UUID.
It's one scalar `CASE` over the canonical stream + current `USER_STREAM_BINDING` and
The same applies to list/get/event snapshot.
`private=true` and the mandatory canonical/technical `TOPIC`; self-chat has one
membership row and does not get a second binding/state through fan-out.

| The method | The way | Specifications | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [operation](operations/delete_message_reaction.md)  |  [PUML](operations/diagrams/delete_message_reaction.puml)  |  [SVG](operations/diagrams/delete_message_reaction.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [operation](operations/delete_message.md)  |  [PUML](operations/diagrams/delete_message.puml)  |  [SVG](operations/diagrams/delete_message.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [operation](operations/delete_stream_binding.md)  |  [PUML](operations/diagrams/delete_stream_binding.puml)  |  [SVG](operations/diagrams/delete_stream_binding.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [operation](operations/delete_stream_topic.md)  |  [PUML](operations/diagrams/delete_stream_topic.puml)  |  [SVG](operations/diagrams/delete_stream_topic.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [operation](operations/delete_stream.md)  |  [PUML](operations/diagrams/delete_stream.puml)  |  [SVG](operations/diagrams/delete_stream.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [operation](operations/delete_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/delete_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/delete_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` |  [operation](operations/get_message_reactions_list.md)  |  [PUML](operations/diagrams/get_message_reactions_list.puml)  |  [SVG](operations/diagrams/get_message_reactions_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [operation](operations/get_message_reaction.md)  |  [PUML](operations/diagrams/get_message_reaction.puml)  |  [SVG](operations/diagrams/get_message_reaction.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/` |  [operation](operations/get_messages_list.md)  |  [PUML](operations/diagrams/get_messages_list.puml)  |  [SVG](operations/diagrams/get_messages_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [operation](operations/get_message.md)  |  [PUML](operations/diagrams/get_message.puml)  |  [SVG](operations/diagrams/get_message.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` |  [operation](operations/get_stream_bindings_list.md)  |  [PUML](operations/diagrams/get_stream_bindings_list.puml)  |  [SVG](operations/diagrams/get_stream_bindings_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [operation](operations/get_stream_binding.md)  |  [PUML](operations/diagrams/get_stream_binding.puml)  |  [SVG](operations/diagrams/get_stream_binding.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` |  [operation](operations/get_stream_topics_list.md)  |  [PUML](operations/diagrams/get_stream_topics_list.puml)  |  [SVG](operations/diagrams/get_stream_topics_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [operation](operations/get_stream_topic.md)  |  [PUML](operations/diagrams/get_stream_topic.puml)  |  [SVG](operations/diagrams/get_stream_topic.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/` |  [operation](operations/get_streams_list.md)  |  [PUML](operations/diagrams/get_streams_list.puml)  |  [SVG](operations/diagrams/get_streams_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [operation](operations/get_stream.md)  |  [PUML](operations/diagrams/get_stream.puml)  |  [SVG](operations/diagrams/get_stream.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [operation](operations/get_topic_summary_endpoints_list.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoints_list.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoints_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [operation](operations/get_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [operation](operations/get_topic_summary_settings.md)  |  [PUML](operations/diagrams/get_topic_summary_settings.puml)  |  [SVG](operations/diagrams/get_topic_summary_settings.svg)  |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` |  [operation](operations/post_message_reactions_create.md)  |  [PUML](operations/diagrams/post_message_reactions_create.puml)  |  [SVG](operations/diagrams/post_message_reactions_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` |  [operation](operations/post_message_read_action.md)  |  [PUML](operations/diagrams/post_message_read_action.puml)  |  [SVG](operations/diagrams/post_message_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` |  [operation](operations/post_message_read_up_to_action.md)  |  [PUML](operations/diagrams/post_message_read_up_to_action.puml)  |  [SVG](operations/diagrams/post_message_read_up_to_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` |  [operation](operations/post_message_star_action.md)  |  [PUML](operations/diagrams/post_message_star_action.puml)  |  [SVG](operations/diagrams/post_message_star_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` |  [operation](operations/post_message_unstar_action.md)  |  [PUML](operations/diagrams/post_message_unstar_action.puml)  |  [SVG](operations/diagrams/post_message_unstar_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/` |  [operation](operations/post_messages_create.md)  |  [PUML](operations/diagrams/post_messages_create.puml)  |  [SVG](operations/diagrams/post_messages_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` |  [operation](operations/post_stream_add_users_action.md)  |  [PUML](operations/diagrams/post_stream_add_users_action.puml)  |  [SVG](operations/diagrams/post_stream_add_users_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` |  [operation](operations/post_stream_archive_action.md)  |  [PUML](operations/diagrams/post_stream_archive_action.puml)  |  [SVG](operations/diagrams/post_stream_archive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` |  [operation](operations/post_stream_notifications_action.md)  |  [PUML](operations/diagrams/post_stream_notifications_action.puml)  |  [SVG](operations/diagrams/post_stream_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` |  [operation](operations/post_stream_read_action.md)  |  [PUML](operations/diagrams/post_stream_read_action.puml)  |  [SVG](operations/diagrams/post_stream_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` |  [operation](operations/post_stream_topics_create.md)  |  [PUML](operations/diagrams/post_stream_topics_create.puml)  |  [SVG](operations/diagrams/post_stream_topics_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` |  [operation](operations/post_stream_unarchive_action.md)  |  [PUML](operations/diagrams/post_stream_unarchive_action.puml)  |  [SVG](operations/diagrams/post_stream_unarchive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/` |  [operation](operations/post_streams_create.md)  |  [PUML](operations/diagrams/post_streams_create.puml)  |  [SVG](operations/diagrams/post_streams_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` |  [operation](operations/post_topic_notifications_action.md)  |  [PUML](operations/diagrams/post_topic_notifications_action.puml)  |  [SVG](operations/diagrams/post_topic_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` |  [operation](operations/post_topic_read_action.md)  |  [PUML](operations/diagrams/post_topic_read_action.puml)  |  [SVG](operations/diagrams/post_topic_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` |  [operation](operations/post_topic_set_default_action.md)  |  [PUML](operations/diagrams/post_topic_set_default_action.puml)  |  [SVG](operations/diagrams/post_topic_set_default_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` |  [operation](operations/post_topic_set_summary_prompt_action.md)  |  [PUML](operations/diagrams/post_topic_set_summary_prompt_action.puml)  |  [SVG](operations/diagrams/post_topic_set_summary_prompt_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [operation](operations/post_topic_summary_endpoints_create.md)  |  [PUML](operations/diagrams/post_topic_summary_endpoints_create.puml)  |  [SVG](operations/diagrams/post_topic_summary_endpoints_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` |  [operation](operations/post_topic_toggle_done_action.md)  |  [PUML](operations/diagrams/post_topic_toggle_done_action.puml)  |  [SVG](operations/diagrams/post_topic_toggle_done_action.svg)  |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [operation](operations/put_message_reaction.md)  |  [PUML](operations/diagrams/put_message_reaction.puml)  |  [SVG](operations/diagrams/put_message_reaction.svg)  |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [operation](operations/put_message.md)  |  [PUML](operations/diagrams/put_message.puml)  |  [SVG](operations/diagrams/put_message.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [operation](operations/put_stream_binding.md)  |  [PUML](operations/diagrams/put_stream_binding.puml)  |  [SVG](operations/diagrams/put_stream_binding.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [operation](operations/put_stream_topic.md)  |  [PUML](operations/diagrams/put_stream_topic.puml)  |  [SVG](operations/diagrams/put_stream_topic.svg)  |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [operation](operations/put_stream.md)  |  [PUML](operations/diagrams/put_stream.puml)  |  [SVG](operations/diagrams/put_stream.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [operation](operations/put_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/put_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/put_topic_summary_endpoint.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [operation](operations/put_topic_summary_settings.md)  |  [PUML](operations/diagrams/put_topic_summary_settings.puml)  |  [SVG](operations/diagrams/put_topic_summary_settings.svg)  |

The coverage is **45 HTTP-operations.**.

[← The main index of the documentation](../../../index.md) · [Index of sequence diagrams](../README.md) · [The section Core Messenger](README.md)
