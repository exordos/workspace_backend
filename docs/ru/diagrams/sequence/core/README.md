[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательности](../README.md) · [Раздел Core Messenger](README.md)

# Спецификации последовательностей операций Core Workspace API

Этот каталог в подходе docs-first охватывает все текущие HTTP-операции семейств сообщений, реакций на сообщения, потоков, привязок (binding) потоков, тем потоков и администрирования сводок тем. Текущим публичным контрактом остаётся [`workspace_api.md`](../../../workspace_api.md).

Для каждой пары HTTP-метод/путь предусмотрены отдельный документ операции и редактируемая/отрендеренная диаграмма последовательности PlantUML. Документы операций отделяют синхронную транзакционную работу RestAlchemy от immutable typed tasks: topic ownership используется только для topic-scoped placements/bindings, а shared projections имеют собственные exact scopes. Готовые публичные события доставляет отдельный диспетчер WebSocket.

Терминология раздела: binding — привязка, placement — размещение, outbox — transactional outbox, projection — проекция, fan-out — распределение (fan-out), worker — фоновый исполнитель.

Общие target-инварианты stream/direct операций: physical `STREAM.owner_uuid`
остаётся индексированным FK, публичное поле `owner` — scalar UUID property.
Публичный `direct_user_uuid` viewer-relative: owner видит physical
`direct_user_uuid`, второй участник видит `owner_uuid`, self-chat видит свой UUID.
Это один scalar `CASE` над canonical stream + текущей `USER_STREAM_BINDING` и
одинаково применяется к list/get/event snapshot. Любой direct stream имеет
`private=true` и обязательный canonical/technical `TOPIC`; self-chat имеет одну
membership row и не получает вторую binding/state через fan-out.

| Метод | Путь | Спецификация | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | [операция](operations/delete_message_reaction.md) | [PUML](operations/diagrams/delete_message_reaction.puml) | [SVG](operations/diagrams/delete_message_reaction.svg) |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` | [операция](operations/delete_message.md) | [PUML](operations/diagrams/delete_message.puml) | [SVG](operations/diagrams/delete_message.svg) |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | [операция](operations/delete_stream_binding.md) | [PUML](operations/diagrams/delete_stream_binding.puml) | [SVG](operations/diagrams/delete_stream_binding.svg) |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | [операция](operations/delete_stream_topic.md) | [PUML](operations/diagrams/delete_stream_topic.puml) | [SVG](operations/diagrams/delete_stream_topic.svg) |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | [операция](operations/delete_stream.md) | [PUML](operations/diagrams/delete_stream.puml) | [SVG](operations/diagrams/delete_stream.svg) |
| `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | [операция](operations/delete_topic_summary_endpoint.md) | [PUML](operations/diagrams/delete_topic_summary_endpoint.puml) | [SVG](operations/diagrams/delete_topic_summary_endpoint.svg) |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` | [операция](operations/get_message_reactions_list.md) | [PUML](operations/diagrams/get_message_reactions_list.puml) | [SVG](operations/diagrams/get_message_reactions_list.svg) |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | [операция](operations/get_message_reaction.md) | [PUML](operations/diagrams/get_message_reaction.puml) | [SVG](operations/diagrams/get_message_reaction.svg) |
| `GET` | `/api/workspace/v1/messenger/messages/` | [операция](operations/get_messages_list.md) | [PUML](operations/diagrams/get_messages_list.puml) | [SVG](operations/diagrams/get_messages_list.svg) |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` | [операция](operations/get_message.md) | [PUML](operations/diagrams/get_message.puml) | [SVG](operations/diagrams/get_message.svg) |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` | [операция](operations/get_stream_bindings_list.md) | [PUML](operations/diagrams/get_stream_bindings_list.puml) | [SVG](operations/diagrams/get_stream_bindings_list.svg) |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | [операция](operations/get_stream_binding.md) | [PUML](operations/diagrams/get_stream_binding.puml) | [SVG](operations/diagrams/get_stream_binding.svg) |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` | [операция](operations/get_stream_topics_list.md) | [PUML](operations/diagrams/get_stream_topics_list.puml) | [SVG](operations/diagrams/get_stream_topics_list.svg) |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | [операция](operations/get_stream_topic.md) | [PUML](operations/diagrams/get_stream_topic.puml) | [SVG](operations/diagrams/get_stream_topic.svg) |
| `GET` | `/api/workspace/v1/messenger/streams/` | [операция](operations/get_streams_list.md) | [PUML](operations/diagrams/get_streams_list.puml) | [SVG](operations/diagrams/get_streams_list.svg) |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | [операция](operations/get_stream.md) | [PUML](operations/diagrams/get_stream.puml) | [SVG](operations/diagrams/get_stream.svg) |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | [операция](operations/get_topic_summary_endpoints_list.md) | [PUML](operations/diagrams/get_topic_summary_endpoints_list.puml) | [SVG](operations/diagrams/get_topic_summary_endpoints_list.svg) |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | [операция](operations/get_topic_summary_endpoint.md) | [PUML](operations/diagrams/get_topic_summary_endpoint.puml) | [SVG](operations/diagrams/get_topic_summary_endpoint.svg) |
| `GET` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` | [операция](operations/get_topic_summary_settings.md) | [PUML](operations/diagrams/get_topic_summary_settings.puml) | [SVG](operations/diagrams/get_topic_summary_settings.svg) |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` | [операция](operations/post_message_reactions_create.md) | [PUML](operations/diagrams/post_message_reactions_create.puml) | [SVG](operations/diagrams/post_message_reactions_create.svg) |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` | [операция](operations/post_message_read_action.md) | [PUML](operations/diagrams/post_message_read_action.puml) | [SVG](operations/diagrams/post_message_read_action.svg) |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` | [операция](operations/post_message_read_up_to_action.md) | [PUML](operations/diagrams/post_message_read_up_to_action.puml) | [SVG](operations/diagrams/post_message_read_up_to_action.svg) |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` | [операция](operations/post_message_star_action.md) | [PUML](operations/diagrams/post_message_star_action.puml) | [SVG](operations/diagrams/post_message_star_action.svg) |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` | [операция](operations/post_message_unstar_action.md) | [PUML](operations/diagrams/post_message_unstar_action.puml) | [SVG](operations/diagrams/post_message_unstar_action.svg) |
| `POST` | `/api/workspace/v1/messenger/messages/` | [операция](operations/post_messages_create.md) | [PUML](operations/diagrams/post_messages_create.puml) | [SVG](operations/diagrams/post_messages_create.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` | [операция](operations/post_stream_add_users_action.md) | [PUML](operations/diagrams/post_stream_add_users_action.puml) | [SVG](operations/diagrams/post_stream_add_users_action.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` | [операция](operations/post_stream_archive_action.md) | [PUML](operations/diagrams/post_stream_archive_action.puml) | [SVG](operations/diagrams/post_stream_archive_action.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` | [операция](operations/post_stream_notifications_action.md) | [PUML](operations/diagrams/post_stream_notifications_action.puml) | [SVG](operations/diagrams/post_stream_notifications_action.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` | [операция](operations/post_stream_read_action.md) | [PUML](operations/diagrams/post_stream_read_action.puml) | [SVG](operations/diagrams/post_stream_read_action.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` | [операция](operations/post_stream_topics_create.md) | [PUML](operations/diagrams/post_stream_topics_create.puml) | [SVG](operations/diagrams/post_stream_topics_create.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` | [операция](operations/post_stream_unarchive_action.md) | [PUML](operations/diagrams/post_stream_unarchive_action.puml) | [SVG](operations/diagrams/post_stream_unarchive_action.svg) |
| `POST` | `/api/workspace/v1/messenger/streams/` | [операция](operations/post_streams_create.md) | [PUML](operations/diagrams/post_streams_create.puml) | [SVG](operations/diagrams/post_streams_create.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` | [операция](operations/post_topic_notifications_action.md) | [PUML](operations/diagrams/post_topic_notifications_action.puml) | [SVG](operations/diagrams/post_topic_notifications_action.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` | [операция](operations/post_topic_read_action.md) | [PUML](operations/diagrams/post_topic_read_action.puml) | [SVG](operations/diagrams/post_topic_read_action.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` | [операция](operations/post_topic_set_default_action.md) | [PUML](operations/diagrams/post_topic_set_default_action.puml) | [SVG](operations/diagrams/post_topic_set_default_action.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` | [операция](operations/post_topic_set_summary_prompt_action.md) | [PUML](operations/diagrams/post_topic_set_summary_prompt_action.puml) | [SVG](operations/diagrams/post_topic_set_summary_prompt_action.svg) |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | [операция](operations/post_topic_summary_endpoints_create.md) | [PUML](operations/diagrams/post_topic_summary_endpoints_create.puml) | [SVG](operations/diagrams/post_topic_summary_endpoints_create.svg) |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` | [операция](operations/post_topic_toggle_done_action.md) | [PUML](operations/diagrams/post_topic_toggle_done_action.puml) | [SVG](operations/diagrams/post_topic_toggle_done_action.svg) |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | [операция](operations/put_message_reaction.md) | [PUML](operations/diagrams/put_message_reaction.puml) | [SVG](operations/diagrams/put_message_reaction.svg) |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` | [операция](operations/put_message.md) | [PUML](operations/diagrams/put_message.puml) | [SVG](operations/diagrams/put_message.svg) |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | [операция](operations/put_stream_binding.md) | [PUML](operations/diagrams/put_stream_binding.puml) | [SVG](operations/diagrams/put_stream_binding.svg) |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | [операция](operations/put_stream_topic.md) | [PUML](operations/diagrams/put_stream_topic.puml) | [SVG](operations/diagrams/put_stream_topic.svg) |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | [операция](operations/put_stream.md) | [PUML](operations/diagrams/put_stream.puml) | [SVG](operations/diagrams/put_stream.svg) |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | [операция](operations/put_topic_summary_endpoint.md) | [PUML](operations/diagrams/put_topic_summary_endpoint.puml) | [SVG](operations/diagrams/put_topic_summary_endpoint.svg) |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` | [операция](operations/put_topic_summary_settings.md) | [PUML](operations/diagrams/put_topic_summary_settings.puml) | [SVG](operations/diagrams/put_topic_summary_settings.svg) |

Покрытие: **45 HTTP-операций**.

[← Главный индекс документации](../../../index.md) · [Индекс диаграмм последовательности](../README.md) · [Раздел Core Messenger](README.md)
