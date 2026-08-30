[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge](../README.md) · [Abschnitt Core Messenger](README.md)

# Spezifikationen der Operationssequenz Core Workspace API

Dieser Katalog umfasst in der docs-first-Anschauung alle aktuellen HTTP -Operationen von Nachrichtenfamilien, Nachrichtenreaktionen, Flüssen, Bindungen von Flüssen, Themen von Flüssen und Verwaltung von Themenverzeichnissen. [`workspace_api.md`](../../../workspace_api.md).

Für jedes HTTP-Methode/Pfadpaar sind ein separates Operationsdokument und ein bearbeitbares/entrendenes PlantUML-Sequenzdiagramm vorgesehen. Die Operationsdokumente trennen die synchrone Transaktionsarbeit RestAlchemy von den immutable typed tasks: topic ownership wird nur für topic-scoped placements/bindings verwendet, und shared projections haben eigene exact scopes. WebSocket.

Terminologie der Abteilung: binding  Bindung, placement  Platzierung, outbox  transactional outbox, projection  Projektion, fan-out  Verteilung (fan-out), worker  Hintergrund-Aussteller.

Die allgemeinen Target-Invarianten von Stream/direct Operationen: physical `STREAM.owner_uuid`
bleibt FK, das öffentliche Feld, indexiert `owner` — scalar UUID property.
Öffentlich `direct_user_uuid` viewer-relative: owner sieht physical
`direct_user_uuid`, Der zweite Teilnehmer sieht `owner_uuid`, der Self-Chat sieht seinen UUID.
Es ist ein Skalar `CASE` über dem canonical Stream + der aktuellen `USER_STREAM_BINDING` und
Der gleiche Effekt kann auf den list/get/event Snapshot angewendet werden.
`private=true` und verpflichtend canonical/technical `TOPIC`; self-chat hat eine
membership row und erhält keine zweite binding/state durch fan-out.

| Die Methode | Der Weg | Spezifikation | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [- Eine Operation](operations/delete_message_reaction.md)  |  [PUML](operations/diagrams/delete_message_reaction.puml)  |  [SVG](operations/diagrams/delete_message_reaction.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [- Eine Operation](operations/delete_message.md)  |  [PUML](operations/diagrams/delete_message.puml)  |  [SVG](operations/diagrams/delete_message.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [- Eine Operation](operations/delete_stream_binding.md)  |  [PUML](operations/diagrams/delete_stream_binding.puml)  |  [SVG](operations/diagrams/delete_stream_binding.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [- Eine Operation](operations/delete_stream_topic.md)  |  [PUML](operations/diagrams/delete_stream_topic.puml)  |  [SVG](operations/diagrams/delete_stream_topic.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [- Eine Operation](operations/delete_stream.md)  |  [PUML](operations/diagrams/delete_stream.puml)  |  [SVG](operations/diagrams/delete_stream.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [- Eine Operation](operations/delete_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/delete_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/delete_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` |  [- Eine Operation](operations/get_message_reactions_list.md)  |  [PUML](operations/diagrams/get_message_reactions_list.puml)  |  [SVG](operations/diagrams/get_message_reactions_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [- Eine Operation](operations/get_message_reaction.md)  |  [PUML](operations/diagrams/get_message_reaction.puml)  |  [SVG](operations/diagrams/get_message_reaction.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/` |  [- Eine Operation](operations/get_messages_list.md)  |  [PUML](operations/diagrams/get_messages_list.puml)  |  [SVG](operations/diagrams/get_messages_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [- Eine Operation](operations/get_message.md)  |  [PUML](operations/diagrams/get_message.puml)  |  [SVG](operations/diagrams/get_message.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` |  [- Eine Operation](operations/get_stream_bindings_list.md)  |  [PUML](operations/diagrams/get_stream_bindings_list.puml)  |  [SVG](operations/diagrams/get_stream_bindings_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [- Eine Operation](operations/get_stream_binding.md)  |  [PUML](operations/diagrams/get_stream_binding.puml)  |  [SVG](operations/diagrams/get_stream_binding.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` |  [- Eine Operation](operations/get_stream_topics_list.md)  |  [PUML](operations/diagrams/get_stream_topics_list.puml)  |  [SVG](operations/diagrams/get_stream_topics_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [- Eine Operation](operations/get_stream_topic.md)  |  [PUML](operations/diagrams/get_stream_topic.puml)  |  [SVG](operations/diagrams/get_stream_topic.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/` |  [- Eine Operation](operations/get_streams_list.md)  |  [PUML](operations/diagrams/get_streams_list.puml)  |  [SVG](operations/diagrams/get_streams_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [- Eine Operation](operations/get_stream.md)  |  [PUML](operations/diagrams/get_stream.puml)  |  [SVG](operations/diagrams/get_stream.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [- Eine Operation](operations/get_topic_summary_endpoints_list.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoints_list.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoints_list.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [- Eine Operation](operations/get_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/get_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/get_topic_summary_endpoint.svg)  |
| `GET` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [- Eine Operation](operations/get_topic_summary_settings.md)  |  [PUML](operations/diagrams/get_topic_summary_settings.puml)  |  [SVG](operations/diagrams/get_topic_summary_settings.svg)  |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` |  [- Eine Operation](operations/post_message_reactions_create.md)  |  [PUML](operations/diagrams/post_message_reactions_create.puml)  |  [SVG](operations/diagrams/post_message_reactions_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` |  [- Eine Operation](operations/post_message_read_action.md)  |  [PUML](operations/diagrams/post_message_read_action.puml)  |  [SVG](operations/diagrams/post_message_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` |  [- Eine Operation](operations/post_message_read_up_to_action.md)  |  [PUML](operations/diagrams/post_message_read_up_to_action.puml)  |  [SVG](operations/diagrams/post_message_read_up_to_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` |  [- Eine Operation](operations/post_message_star_action.md)  |  [PUML](operations/diagrams/post_message_star_action.puml)  |  [SVG](operations/diagrams/post_message_star_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` |  [- Eine Operation](operations/post_message_unstar_action.md)  |  [PUML](operations/diagrams/post_message_unstar_action.puml)  |  [SVG](operations/diagrams/post_message_unstar_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/messages/` |  [- Eine Operation](operations/post_messages_create.md)  |  [PUML](operations/diagrams/post_messages_create.puml)  |  [SVG](operations/diagrams/post_messages_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` |  [- Eine Operation](operations/post_stream_add_users_action.md)  |  [PUML](operations/diagrams/post_stream_add_users_action.puml)  |  [SVG](operations/diagrams/post_stream_add_users_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` |  [- Eine Operation](operations/post_stream_archive_action.md)  |  [PUML](operations/diagrams/post_stream_archive_action.puml)  |  [SVG](operations/diagrams/post_stream_archive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` |  [- Eine Operation](operations/post_stream_notifications_action.md)  |  [PUML](operations/diagrams/post_stream_notifications_action.puml)  |  [SVG](operations/diagrams/post_stream_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` |  [- Eine Operation](operations/post_stream_read_action.md)  |  [PUML](operations/diagrams/post_stream_read_action.puml)  |  [SVG](operations/diagrams/post_stream_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` |  [- Eine Operation](operations/post_stream_topics_create.md)  |  [PUML](operations/diagrams/post_stream_topics_create.puml)  |  [SVG](operations/diagrams/post_stream_topics_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` |  [- Eine Operation](operations/post_stream_unarchive_action.md)  |  [PUML](operations/diagrams/post_stream_unarchive_action.puml)  |  [SVG](operations/diagrams/post_stream_unarchive_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/streams/` |  [- Eine Operation](operations/post_streams_create.md)  |  [PUML](operations/diagrams/post_streams_create.puml)  |  [SVG](operations/diagrams/post_streams_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` |  [- Eine Operation](operations/post_topic_notifications_action.md)  |  [PUML](operations/diagrams/post_topic_notifications_action.puml)  |  [SVG](operations/diagrams/post_topic_notifications_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` |  [- Eine Operation](operations/post_topic_read_action.md)  |  [PUML](operations/diagrams/post_topic_read_action.puml)  |  [SVG](operations/diagrams/post_topic_read_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` |  [- Eine Operation](operations/post_topic_set_default_action.md)  |  [PUML](operations/diagrams/post_topic_set_default_action.puml)  |  [SVG](operations/diagrams/post_topic_set_default_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` |  [- Eine Operation](operations/post_topic_set_summary_prompt_action.md)  |  [PUML](operations/diagrams/post_topic_set_summary_prompt_action.puml)  |  [SVG](operations/diagrams/post_topic_set_summary_prompt_action.svg)  |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` |  [- Eine Operation](operations/post_topic_summary_endpoints_create.md)  |  [PUML](operations/diagrams/post_topic_summary_endpoints_create.puml)  |  [SVG](operations/diagrams/post_topic_summary_endpoints_create.svg)  |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` |  [- Eine Operation](operations/post_topic_toggle_done_action.md)  |  [PUML](operations/diagrams/post_topic_toggle_done_action.puml)  |  [SVG](operations/diagrams/post_topic_toggle_done_action.svg)  |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` |  [- Eine Operation](operations/put_message_reaction.md)  |  [PUML](operations/diagrams/put_message_reaction.puml)  |  [SVG](operations/diagrams/put_message_reaction.svg)  |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` |  [- Eine Operation](operations/put_message.md)  |  [PUML](operations/diagrams/put_message.puml)  |  [SVG](operations/diagrams/put_message.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` |  [- Eine Operation](operations/put_stream_binding.md)  |  [PUML](operations/diagrams/put_stream_binding.puml)  |  [SVG](operations/diagrams/put_stream_binding.svg)  |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` |  [- Eine Operation](operations/put_stream_topic.md)  |  [PUML](operations/diagrams/put_stream_topic.puml)  |  [SVG](operations/diagrams/put_stream_topic.svg)  |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` |  [- Eine Operation](operations/put_stream.md)  |  [PUML](operations/diagrams/put_stream.puml)  |  [SVG](operations/diagrams/put_stream.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` |  [- Eine Operation](operations/put_topic_summary_endpoint.md)  |  [PUML](operations/diagrams/put_topic_summary_endpoint.puml)  |  [SVG](operations/diagrams/put_topic_summary_endpoint.svg)  |
| `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` |  [- Eine Operation](operations/put_topic_summary_settings.md)  |  [PUML](operations/diagrams/put_topic_summary_settings.puml)  |  [SVG](operations/diagrams/put_topic_summary_settings.svg)  |

Abdeckung: **45 HTTP-Operationen**.

[← Hauptindex der Dokumentation](../../../index.md) · [Index der Abfolge](../README.md) · [Abschnitt Core Messenger](README.md)
