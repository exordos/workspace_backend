[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [外部集成和执行时间的分类](../README.md)

# 具体的外部整合操作,事件和 WebSocket

这个目录包含每个公开的 HTTP 操作的一个规范,以及记录的 WebSocket 事件执行时间的输入点. [`workspace_api.md`](../../../../workspace_api.md); 关于外部整合的细节来自 [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md).

状态: **当前公开的 HTTP/WebSocket-合同;目标内部模型
从文档开始的句子**.

格式中的公开字段UUID它们是直角形.UUID- 它们的特性.RestAlchemy实体列`*_uuid`仍然是具有明确指定的引用操作的索引外部密钥; 没有一个公开的UUID没有被串行为URI阅读操作不执行计算,也不创建outbox/任务记录.状态变更使用短交易,通过outbox/域不变的工作,固定物质化,公开事件记录,公开事件记录,以及单独的管理器 WebSocket.

## 覆盖面

| 方法 | 公众路线 | Markdown | PlantUML | SVG |
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

总覆盖: **29 HTTP-操作 + 1 个输入点 WebSocket 执行时间**.

## 已知与生成的差异 OpenAPI

现在,在生成的OpenAPI中,桥案和提供商政策的反应被错误地标记为`ExternalOperation_Get`,而执行时间控制器和相关的公共合同则返回更新的桥案或提供商政策资源.在五个相应的操作文件中,这是本地标记的.对于`reconnect`/`disconnect`帐户和`select`/`deselect`/`move`聊天,资源图表已经在`openapi_contract.py`中被修复,因此它们并未被列为差异.这里所描述的公共JSON遵循实际的记录执行边界;生产代码和时间代码OpenAPI没有改变.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [外部集成和执行时间的分类](../README.md)
