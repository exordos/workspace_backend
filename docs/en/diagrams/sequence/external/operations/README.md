[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [External integration and execution time section](../README.md)

# The specifications of external integration operations, events and WebSocket

This directory contains one specification for each public HTTP-operation of the external integration family and execution time, as well as a documented entry point WebSocket Events execution time. [`workspace_api.md`](../../../../workspace_api.md); The details of the life cycle of external integration are taken from [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md).

Status: **current public HTTP/WebSocket-contract; targeted internal models
and background streams  a sentence that starts with documentation**.

Public fields of the form UUID are scalar UUID-properties RestAlchemy. Physical columns `*_uuid` remain indexed external keys with explicitly specified reference actions; no public UUID is serialized as a URI relationship. Read operations do not perform computations and do not create outbox/task records. State changes use a short transaction, unchanged work through outbox/domain, fixed materialization, ready-to-read public event records where they are defined by a public event register, and a separate manager WebSocket.

## Coverage

| The method | The public road | Markdown | PlantUML | SVG |
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

The resulting coverage: **29 HTTP-operations + 1 entry point WebSocket of the execution time**.

## Known discrepancy with generated OpenAPI

It 's in the generating .OpenAPIThe bridge replies and the provider policy are incorrectly marked as`ExternalOperation_Get`The time controller and the associated public contract return the updated resource of the bridge instance or provider policy.`reconnect`/`disconnect`I 'm gonna need your account and ...`select`/`deselect`/`move`The resources are already being corrected in the`openapi_contract.py`So they're not listed here as differences.JSONfollows the actual documented boundary of the execution time; production code and codeOpenAPIThey haven 't changed ..

[← The main index of the documentation](../../../../index.md) · [Index of sequence diagrams](../../README.md) · [External integration and execution time section](../README.md)
