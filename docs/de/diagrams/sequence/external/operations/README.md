[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge-Diagramme](../../README.md) · [Abschnitt Außenintegration und Ausführungszeit](../README.md)

# Die Spezifikationen für die Auslandsintegrationsoperationen, die Ereignisse und WebSocket

Dieser Katalog enthält eine Spezifikation für jede öffentliche HTTP-Operation der Familie der externen Integration und Ausführungszeiten sowie einen dokumentierten Eingangspunkt WebSocket Events Ausführungszeiten. [`workspace_api.md`](../../../../workspace_api.md); Die Einzelheiten des Lebenszyklus der externen Integration sind aus [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md).

Status: **aktueller öffentlicher HTTP/WebSocket-Kontrakt; zielgerichtete interne Modelle
und Hintergrundströme  Satz, der mit Dokumentation beginnt**.

Die öffentlichen Felder in der Form UUID sind skalare UUID-Eigenschaften RestAlchemy. Physische Spalten `*_uuid` bleiben als indexierte externe Schlüssel mit klar festgelegten Verweisaktionen; keine öffentliche UUID wird als URI-Beziehung serialisiert. Lesebedienungen führen keine Berechnungen durch und erstellen keine Outbox/Aufgaben-Einträge. Zustandsänderungen nutzen eine kurze Transaktion, unveränderliche Arbeit über Outbox/Domain, feststehende Materialierung, bereitgestellte öffentliche Ereignis-Einträge, wo sie von einem öffentlichen Ereignisregister festgelegt werden, und einen separaten Manager WebSocket.

## Abdeckung

| Die Methode | Öffentlicher Weg | Markdown | PlantUML | SVG |
| --- | --- | --- | --- | --- |
| `GET` | `/api/workspace/v1/events/` |  [get_events.md](get_events.md)  |  [PUML](diagrams/get_events.puml)  |  [SVG](diagrams/get_events.svg)  |
| `GET` | `/api/workspace/v1/epoch/` |  [get_epoch.md](get_epoch.md)  |  [PUML](diagrams/get_epoch.puml)  |  [SVG](diagrams/get_epoch.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_accounts/` |  [get_external_accounts.md](get_external_accounts.md)  |  [PUML](diagrams/get_external_accounts.puml)  |  [SVG](diagrams/get_external_accounts.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_accounts/` |  [post_external_accounts.md](post_external_accounts.md)  |  [PUML](diagrams/post_external_accounts.puml)  |  [SVG](diagrams/post_external_accounts.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [get_external_account.md](get_external_account.md)  |  [PUML](diagrams/get_external_account.puml)  |  [SVG](diagrams/get_external_account.svg)  |
| `PUT` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [put_external_account.md](put_external_account.md)  |  [PUML](diagrams/put_external_account.puml)  |  [SVG](diagrams/put_external_account.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` |  [delete_external_account.md](delete_external_account.md)  |  [PUML](diagrams/delete_external_account.puml)  |  [SVG](diagrams/delete_external_account.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke` |  [post_external_account_reconnect.md](post_external_account_reconnect.md)  |  [PUML](diagrams/post_external_account_reconnect.puml)  |  [SVG](diagrams/post_external_account_reconnect.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/disconnect/invoke` |  [post_external_account_disconnect.md](post_external_account_disconnect.md)  |  [PUML](diagrams/post_external_account_disconnect.puml)  |  [SVG](diagrams/post_external_account_disconnect.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_chats/` |  [get_external_chats.md](get_external_chats.md)  |  [PUML](diagrams/get_external_chats.puml)  |  [SVG](diagrams/get_external_chats.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}` |  [get_external_chat.md](get_external_chat.md)  |  [PUML](diagrams/get_external_chat.puml)  |  [SVG](diagrams/get_external_chat.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/select/invoke` |  [post_external_chat_select.md](post_external_chat_select.md)  |  [PUML](diagrams/post_external_chat_select.puml)  |  [SVG](diagrams/post_external_chat_select.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/deselect/invoke` |  [post_external_chat_deselect.md](post_external_chat_deselect.md)  |  [PUML](diagrams/post_external_chat_deselect.puml)  |  [SVG](diagrams/post_external_chat_deselect.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/move/invoke` |  [post_external_chat_move.md](post_external_chat_move.md)  |  [PUML](diagrams/post_external_chat_move.puml)  |  [SVG](diagrams/post_external_chat_move.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_operations/` |  [get_external_operations.md](get_external_operations.md)  |  [PUML](diagrams/get_external_operations.puml)  |  [SVG](diagrams/get_external_operations.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` |  [get_external_operation.md](get_external_operation.md)  |  [PUML](diagrams/get_external_operation.puml)  |  [SVG](diagrams/get_external_operation.svg)  |
| `DELETE` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` |  [delete_external_operation.md](delete_external_operation.md)  |  [PUML](diagrams/delete_external_operation.puml)  |  [SVG](diagrams/delete_external_operation.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}/actions/retry/invoke` |  [post_external_operation_retry.md](post_external_operation_retry.md)  |  [PUML](diagrams/post_external_operation_retry.puml)  |  [SVG](diagrams/post_external_operation_retry.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_operations/actions/preflight/invoke` |  [post_external_operation_preflight.md](post_external_operation_preflight.md)  |  [PUML](diagrams/post_external_operation_preflight.puml)  |  [SVG](diagrams/post_external_operation_preflight.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/` |  [get_external_bridge_instances.md](get_external_bridge_instances.md)  |  [PUML](diagrams/get_external_bridge_instances.puml)  |  [SVG](diagrams/get_external_bridge_instances.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}` |  [get_external_bridge_instance.md](get_external_bridge_instance.md)  |  [PUML](diagrams/get_external_bridge_instance.puml)  |  [SVG](diagrams/get_external_bridge_instance.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` |  [post_external_bridge_instance_suspend.md](post_external_bridge_instance_suspend.md)  |  [PUML](diagrams/post_external_bridge_instance_suspend.puml)  |  [SVG](diagrams/post_external_bridge_instance_suspend.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/resume/invoke` |  [post_external_bridge_instance_resume.md](post_external_bridge_instance_resume.md)  |  [PUML](diagrams/post_external_bridge_instance_resume.puml)  |  [SVG](diagrams/post_external_bridge_instance_resume.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/revoke/invoke` |  [post_external_bridge_instance_revoke.md](post_external_bridge_instance_revoke.md)  |  [PUML](diagrams/post_external_bridge_instance_revoke.puml)  |  [SVG](diagrams/post_external_bridge_instance_revoke.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` |  [get_external_provider_policy.md](get_external_provider_policy.md)  |  [PUML](diagrams/get_external_provider_policy.puml)  |  [SVG](diagrams/get_external_provider_policy.svg)  |
| `PUT` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` |  [put_external_provider_policy.md](put_external_provider_policy.md)  |  [PUML](diagrams/put_external_provider_policy.puml)  |  [SVG](diagrams/put_external_provider_policy.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/suspend/invoke` |  [post_external_provider_policy_suspend.md](post_external_provider_policy_suspend.md)  |  [PUML](diagrams/post_external_provider_policy_suspend.puml)  |  [SVG](diagrams/post_external_provider_policy_suspend.svg)  |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/resume/invoke` |  [post_external_provider_policy_resume.md](post_external_provider_policy_resume.md)  |  [PUML](diagrams/post_external_provider_policy_resume.puml)  |  [SVG](diagrams/post_external_provider_policy_resume.svg)  |
| `GET` | `/api/workspace/v1/messenger/external_provider_health/{kind}` |  [get_external_provider_health.md](get_external_provider_health.md)  |  [PUML](diagrams/get_external_provider_health.puml)  |  [SVG](diagrams/get_external_provider_health.svg)  |
| WebSocket | `/api/workspace/v1/events/ws` |  [websocket_events.md](websocket_events.md)  |  [PUML](diagrams/websocket_events.puml)  |  [SVG](diagrams/websocket_events.svg)  |

Endende Abdeckung: **29 HTTP-Operationen + 1 Eingangspunkt WebSocket Ausführungszeit**.

## Bekannte Abweichung von der erzeugten OpenAPI

Jetzt in der Generator-Liste.OpenAPIDie Antworten der Bridge-Aktionen und der Provider-Richtlinien sind falsch als`ExternalOperation_Get`, während die Ausführungszeit-Controller und der dazugehörige öffentliche Vertrag die aktualisierte Ressource der Brücken-Instanz oder der Provider-Richtlinie zurückgeben.`reconnect`/`disconnect`Ich habe ein Konto und ...`select`/`deselect`/`move`Die Schaltfläche der Ressourcen wird bereits in`openapi_contract.py`Die hier beschriebene ist öffentlich.JSONfolgt der tatsächlich dokumentierten Grenze der Ausführungszeit; Produktionscode und CodeOpenAPInicht verändert.

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge-Diagramme](../../README.md) · [Abschnitt Außenintegration und Ausführungszeit](../README.md)
