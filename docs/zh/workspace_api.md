# Workspace v1 API

这份文件描述了由nginx编写的浏览器面向的API合同
保存的 `workspace-messenger-api`,普通的 `workspace-api` 和
伴随服务 `workspace-messenger-events` 网接口服务. 公共服务 Messenger
请求使用专用Messenger过程;普通用户,客户端服务
设置,按设备注册,以及 REST 事件使用 `workspace-api`.
独立邮件,日历和外部用户终端点不是此项的组成部分
提供者中立的外部帐户,聊天,运营,政策,健康,
桥接实例资源是 Messenger API 的一部分.

美国人Messenger资源,会员,用户状态,事件,提供商映射
客户端设置在 PostgreSQL 中是正规的.
保持提供者身份,而不会更改浏览器 API.

## 运行时间入口点

直接的本地服务:

```text
Messenger REST API:  http://127.0.0.1:21081/v1
Events WebSocket:    ws://127.0.0.1:21082/v1/events/ws
Workspace REST API:  http://127.0.0.1:21084/v1
Worker:              workspace-messenger-worker
Messenger OpenAPI:   http://127.0.0.1:21081/specifications/3.0.3
Workspace OpenAPI:   http://127.0.0.1:21084/specifications/3.0.3
```

后端 nginx 显示这些内部网关路线.
`workspace_ui`负载平衡器代理 `/api/` 转换这个网关
路径:

```text
Workspace REST root: /api/workspace/v1/...
Messenger REST:      /api/workspace/v1/messenger/...
Events REST:         /api/workspace/v1/events/...
Events WebSocket:    /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>
OpenAPI spec:        /api/workspace/specifications/3.0.3
```

`/api/workspace/v1/messenger/`是被保存到Messenger REST的代理
在 `127.0.0.1:21081` 上服务;剩余的 `/api/workspace/` 代为
在 `127.0.0.1:21084` 上的 Workspace REST 服务.
确切的nginx位置 `/api/workspace/v1/events/ws`是代理到
在 `127.0.0.1:21082` 上的websocket服务终点 `/v1/events/ws`.

后端 nginx 显示设置 `client_max_body_size 50m` 作为代理请求.
它不支持WebUI;不匹配的非API路径返回`404`.

## 总规则 {#general-rules}

- 请求和响应的体是JSON (`application/json`).
- 资源标识符是 UUID,除非一个字段明确表示相反.
- 时间是 UTC 时间串行为 ISO-8601 字符串.
- REST身份验证使用一个创世纪 IAM持有人令牌:

```http
Authorization: Bearer <token>
```

要在本地测试环境中获得一个代币,请从Exordos Core IAM 请求它
通过网关,并使用 `access_token` 字段的响应:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=login%2Bpassword&
login=<test-user>&
password=<test-password>&
scope=openid+email+profile+project%3A<project-uuid>&
ttl=3600&
refresh_ttl=172800
```

同样的代币请求也可以发送为 JSON:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "login+password",
  "login": "<test-user>",
  "password": "<test-password>",
  "scope": "openid email profile project:<project-uuid>",
  "ttl": 3600,
  "refresh_ttl": 172800
}
```

用户界面客户端使用IAM默认客户端.不需要客户端凭证或
通过浏览器端代码发送. `ttl=3600` 表示访问令牌发行为1
`refresh_ttl=172800` 表示更新令牌发行时间为2天.

实证请求示例:

```http
GET /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token from IAM response>
```

要更新过期的访问令牌,将更新令牌发送到相同的默认
客户端:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=refresh_token&
refresh_token=<refresh_token from IAM response>
```

JSON 更新体也可以接受:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "<refresh_token from IAM response>"
}
```

使用新的 `access_token` 来回应后续的消息传递者 API
如果更新响应包含一个新的 `refresh_token`, 替换
存储了更新令牌.

`user_uuid`是从IAM代币信息中获取的. `project_id`是从IAM中获取的.
自我检查信息.用户范围的资源自动过和/or
写出电流 `user_uuid`.

典型的RESTAlchemy/IAM错误响应:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

HTTP响应体是错误对象本身;没有外部`json`
包装. Messenger验证错误使用 HTTP `400`. 下面的公众
操作提供一个更具体的应用代码在同一个 `code` 字段:

| 申请代码 | 类型 | 操作 |
| --- | --- | --- |
| `400001004` | `InvalidStreamBindingRoleError` | 添加一个不支持的绑定角色的用户. |
| `400001005` | `StreamBindingUsersPayloadError` | 添加一个非用户 UUID 列表的角色值的用户. |
| `400001006` | `InvalidTopicNotificationModeError` | 选择与流程模式不兼容的主题通知模式. |
| `400001007` | `StreamDefaultTopicNotConfiguredError` | 在流没有默认主题时创建没有`topic_uuid`的消息. |

Messenger资源保持一个规范的来源投影,而不是暴露
运输标识符:

```json
{
  "provider": {
    "kind": "zulip",
    "account_uuid": "account-uuid",
    "external_id": "provider-entity-id",
    "capabilities": {},
    "delivery_class": "live",
    "notification_eligible": true
  },
  "delivery": {
    "external_operation_uuid": "operation-uuid",
    "status": "pending",
    "safe_error": null,
    "can_retry": false,
    "can_discard": false,
    "duplicate_risk": false,
    "retry_requires_confirmation": false,
    "original_url": null,
    "reconciliation_reason": null,
    "updated_at": "2026-07-15T09:30:00.000000Z"
  }
}
```

`provider.capabilities` 包含有效的行动描述符.
提供商预测 `delivery_class` 是 `live` 或 `backfill`,
`notification_eligible` 结是否消息可以通知当
后端接受了它. 后备充值和现场流量在帐户之前接受
通知门开启是`false`;客户端必须抑制桌面
警报,声音,并注意这些信息.
`provider: null` 和 `delivery: null`. 提供者同步指针,原始协议
有效载荷,凭证材料,以及内部数据库状态不是
没有任何的.


浏览器客户端使用相同的 IAM 持有人令牌和项目范围
公共服务器发现终点是
`GET /api/workspace/v1/messenger/server_settings`只有一个.
用户界面使用的未经验证的 Workspace 终端.

这是一个绿地公共布局.
`/api/messenger/**`, `/api/v1/**`,
`/api/workspace/v1/messenger/events/**`,或是前的消息传递器网插件
路径.没有浏览器面向的提供商 API.独立部署的提供商
运行时使用私人桥 autenticated API 根源
`/api/workspace-provider/v1`;其运营通常承诺 Messenger
资源进入PostgreSQL,因此它不会改变描述的浏览器合同
根据本文的定义,私人合同
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

## 页面排序和过器

集合终点使用RESTAlchemy标志器排列:

| 查询参数 | 类型 | 描述 |
| --- | --- | --- |
| `page_limit` | 整数 | 项目的最大数量. `0`或省略的值意味着没有明确的限制. |
| `page_marker` | UUID 或整数 | 下一页标记. UUID资源使用前一页的最后一个 `uuid`;事件使用前一页的最后一个 `epoch_version`,并且每当标记不为零时都需要匹配 `epoch_generation`. |

如果提供 `page_limit`,答案包括 `X-Pagination-Limit`.
页面存在,响应还包括`X-Pagination-Marker`.

`GET /api/workspace/v1/messenger/messages/`使用稳定的复合键盘.
设置 `sort_key=created_at` 和 `sort_dir=asc` 或 `sort_dir=desc`;行是顺序的
通过 `(created_at, uuid)` 在那个方向. `page_marker` 仍然是 UUID 的
服务器解决了这个问题.
这就是UUID在同一处IAM项目,认证用户视图和消息
过器范围,然后继续严格按照其复合键.
这种范围不被接受.`X-Pagination-Marker`只有当一个
`page_limit + 1`探测器证明另一个行存在,所以一个完整的最后页面
没有任何继续.

`GET /api/workspace/v1/messenger/drafts/`使用相同的 UUID标记合同,
按 `(updated_at, uuid)` 顺序设置,并设置 `sort_key=updated_at`
`sort_dir=asc|desc`;可选的 `stream_uuid` 和 `topic_uuid` 过器仍然存在
在验证的所有者和项目范围内.
范围返回 `404`.

Workspace 收集控制器还支持条件波器后:

| 后 | 意思 | 举例 |
| --- | --- | --- |
| `>` | 严格地说,大于 | `epoch_version>123` |
| `<` | 严格地说,比 | `epoch_version<123` |
| `=>` | 大于或等于 | `epoch_version=>123` |
| `=<` | 较小或等于 | `epoch_version=<123` |

当一个查询参数名称包含`>`没有`<`, URL- 编码它如果HTTP
客户端不会自动这样做:

```http
GET /api/workspace/v1/events/?epoch_version%3E=123&epoch_generation=781203&page_limit=500
```

事件排列页面和重新连接使用指针对
`(epoch_generation, epoch_version)`,不是一个时代号码.
设置 `0` 时,可以省略 `epoch_generation`.如果保留的事件后不再存在
在 `1` 时段开始,那个冷请求返回相同的 HTTP 410 间隙响应
作为任何其他不能产生完整三角线的光标;客户端必须加载
在启动新光标之前,权威快照.

## 终点总结 {#endpoint-summary}

| 方法 | 路径 | 描述 |
| --- | --- | --- |
| `GET` | `/api/workspace/v1/` | 下面列出 `/api/workspace/v1/` 的航线. |
| `GET` | `/api/workspace/v1/messenger/` | 在 `/api/workspace/v1/messenger/` 下列列出 Messenger 航线. |
| `GET` | `/api/workspace/v1/messenger/server_settings` | 返回类似 Zulip 的服务器设置. |
| `GET` | `/api/workspace/v1/messenger/server_settings/` | 同上述相同;支持后面斜. |
| `GET` | `/api/workspace/v1/messenger/folders/` | 目前 IAM 用户的文件列表. |
| `POST` | `/api/workspace/v1/messenger/folders/` | 创建一个文件. |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | 拿一个文件. |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | 更新一个文件. |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | 删除一个文件. |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | 列出当前 IAM 用户的文件项. |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | 创建一个文件项目. |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | 拿一个文件. |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | 删除一个文件项. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | 固定一个文件项目. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | 解开一个文件. |
| `GET` | `/api/workspace/v1/messenger/streams/` | 列出当前 IAM 用户可见的流. |
| `POST` | `/api/workspace/v1/messenger/streams/` | 创造一个流. |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | 让我们走一条河. |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | 更新一个流. |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | 删除所有流用户的流. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` | 根据角色添加用户到流. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` | 设置 `is_archived: true`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` | 设置 `is_archived: false`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` | 设置当前用户的流通知模式. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` | 标记所有未读的流消息为当前用户读. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` | 列出流链接. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | 让一个流绑定. |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | 更新一个流绑定. |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | 从流中删除用户. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` | 目前 IAM 用户可见的主题列表. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` | 创建一个话题. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | 找一个话题. |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | 重命名一个主题;主题必须包含 `name`. |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | 删除一个主题. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` | 切换所有主题用户的共享 `is_done` 标志. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` | 设置当前用户的主题通知模式. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` | 让这个主题成为其流的默认主题. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` | 更新所有者/administrator-managed 每个主题概要配置,包括启用/disable. |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | 列出全球 OpenAI 兼容的总结终点;需要 `workspace.topic_summary_endpoint.manage`. |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | 创建一个具有仅可写入凭证的全球总结终点;需要 `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | 读取,更新或删除一个全球总结终点;需要 `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` | 阅读两个总结门口或更新两个 `workspace.topic_summary_settings.manage`. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` | 标记所有未读的主题消息为当前用户读. |
| `GET` | `/api/workspace/v1/messenger/messages/` | 目前 IAM 用户可见的列表消息. |
| `POST` | `/api/workspace/v1/messenger/messages/` | 创建一个消息. |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` | 接收一个消息. |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` | 更新一个消息载荷. |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` | 删除一个消息. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` | 标记消息为当前用户读. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` | 标记未读的消息在同一主题上,直到这个消息. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` | 目前用户的星星消息. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` | 目前用户的不明星消息. |
| `GET` | `/api/workspace/v1/messenger/drafts/` | 列出当前用户的草案,可按流或主题过. |
| `POST` | `/api/workspace/v1/messenger/drafts/` | 使用客户端生成的 UUID 创建草稿. |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | 获得一个拥有草案及其强度修改 ETag. |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | 仅使用 `If-Match` 替换标记载荷. |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | 使用 `If-Match` 硬删除所有草稿. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/` | 列出当前用户的区域-全球外部帐户;需要 `workspace.external_account.read`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/` | 创建一个外部帐户,使用客户端生成的 UUID 和仅可写的凭证;需要 `workspace.external_account.create`. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | 获取所有者清理的外部帐户快照;需要 `workspace.external_account.read`. |
| `PUT` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | 取代可变的非秘密设置使用 `If-Match`;需要 `workspace.external_account.update`. |
| `DELETE` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | 删除帐户及其投影;需要 `workspace.external_account.delete`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke` | 验证并更换仅写凭证,然后恢复同步;需要 `workspace.external_account.reconnect`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/disconnect/invoke` | 停止同步,同时保留仅读投影;需要 `workspace.external_account.disconnect`. |
| `GET` | `/api/workspace/v1/messenger/external_chats/` | 列出所有者清洁的外部聊天目录和分配状态. |
| `GET` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}` | 让一个清洁的外部聊天快照. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/select/invoke` | 选择一个聊天并将其分配到一个项目. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/deselect/invoke` | 取消工作,删除聊天投影. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/move/invoke` | 通过 `If-Match` 原子移动一个投影到另一个项目. |
| `GET` | `/api/workspace/v1/messenger/external_operations/` | 列出所有者的外部操作. |
| `GET` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | 让我们清除运营状态. |
| `DELETE` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | 废弃符合条件的工作. |
| `POST` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}/actions/retry/invoke` | 试试一个符合条件的失败操作. |
| `POST` | `/api/workspace/v1/messenger/external_operations/actions/preflight/invoke` | 检查出口突变之前的能力和转换损失. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/` | 清理过的桥实例列表;需要专用的 IAM 读取权限. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}` | 让我们清理桥梁身份,健康,能力和证书状态. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | 暂停一个桥的身份. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/resume/invoke` | 恢复未撤销的桥梁身份. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/revoke/invoke` | 撤销活跃桥梁证书生成. |
| `GET` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | 阅读提供者类型的清洁领域政策. |
| `PUT` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | 使用 `If-Match` 和专用 IAM 权限更新提供者策略. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/suspend/invoke` | 暂停提供者类型的整个领域. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/resume/invoke` | 在验证后恢复提供者类型. |
| `GET` | `/api/workspace/v1/messenger/external_provider_health/{kind}` | 阅读清洁的整体供应商健康. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` | 列出当前 IAM 用户可见的消息的反应. |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` | 创建一个消息反应. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | 通过消息访问可见的消息反应. |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | 更新当前用户的反应. |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | 删除当前用户的反应. |
| `GET` | `/api/workspace/v1/messenger/files/` | 目前 IAM 用户可见的文件列表. |
| `POST` | `/api/workspace/v1/messenger/files/` | 创建文件元数据或上传多部分文件数据. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | 获取可见的文件元数据记录. |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | 更新一个拥有文件元数据记录. |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | 删除所有文件及其访问行. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | 现在我们可以使用 |
| `GET` | `/api/workspace/v1/services/` | 列出可用的 Workspace 服务. |
| `GET` | `/api/workspace/v1/services/{service_uuid}` | 获取一个可用 Workspace 服务. |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` | 现在的用户的推力设备可以被注册或旋转. |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` | 完全删除当前用户的推送设备注册. |
| `GET` | `/api/workspace/v1/events/` | 列出当前 IAM 用户的实时事件. |
| `GET` | `/api/workspace/v1/epoch/` | 返回当前用户最近可见事件时代. |
| `GET` | `/api/workspace/v1/users/` | 列出工作区用户. |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | 找一个工作空间用户. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | 更新当前用户的存在状态和心跳时间. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | 现在的用户的化身. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | 删除当前用户的自定义化身,并恢复正规的Gravatar URN. |
| `GET` | `/api/workspace/v1/me/` | 返回当前认证的 Workspace 用户. |

### 外部整合合同的边界

上面的终点表是当前的标准清单
通过 IAM 验证浏览器路线.生成的 OpenAPI 是权威的
控制器支持的操作HTTP的请求和响应方案,
根据下文所述的消息反应投影例外.
`server_settings`中间件别名和事件 WebSocket是运行时输入
它们是文件中的记录,但不是生成的 OpenAPI 操作.

外部帐户设置,聊天源元数据,操作详情使用
动态类型模型. Zulip是第一个注册类型;添加另一个类型
没有添加供应商特定的收集路线.详细的清洁资源
例子, ETag 和 `If-Match` 规则,操作权限,生命周期语义,
管理行为, 帐户,聊天,操作,桥接实例,
提供者健康政策,在第5和第6节
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).
提供者数据平面,文件转移部分描述
后端到桥接合约,并不是公共浏览器的一部分 API.

## 服务器设置 {#server-settings}

`GET /api/workspace/v1/messenger/server_settings` 是公开的,不需要 `Authorization`.
没有支持,不支持.
查询参数报告在
`ignored_parameters_unsupported`. `realm_url` 和 `realm_uri` 使用请求
`Host` 头和默认公众 HTTPS 方案.一个可信的反向代理
可以明确提供`X-Forwarded-Proto`;包装Workspace nginx
配置设置为 `https`,因为 TLS 在内部
HTTP跳. `realm_icon`使用`urn:url:<https-url>`匿名
可检索的组织章.客户端解开URN,使用HTTPSURL
直接. 值是从可规则请求领域中导出的
`urn:url:<realm>/logo-512x512.png`;nginx从包装的服务该路径
组织的标志.

举例一个答案:

```json
{
  "result": "success",
  "msg": "Welcome to Exordos Workspace",
  "authentication_methods": {
    "password": true,
    "dev": false,
    "email": true,
    "ldap": false,
    "remoteuser": false,
    "github": false,
    "azuread": false,
    "gitlab": false,
    "google": false,
    "apple": false,
    "saml": false,
    "openid connect": false
  },
  "push_notifications_enabled": true,
  "email_auth_enabled": true,
  "require_email_format_usernames": true,
  "realm_url": "https://workspace.example.com",
  "realm_name": "Exordos Workspace",
  "realm_icon": "urn:url:https://workspace.example.com/logo-512x512.png",
  "realm_description": "<p>Exordos Workspace messenger.</p>",
  "realm_web_public_access_enabled": false,
  "meet_url": "https://meet.genesis-core.tech",
  "external_authentication_methods": [],
  "realm_uri": "https://workspace.example.com"
}
```

## 推力设备 {#push-devices}

`PUT /api/workspace/v1/push_devices/{registration_uuid}`是一个替代式
客户端生成一个稳定的 UUID 每个应用程序安装.
首次注册返回 `201`;取代其 FCM 代币或加密密钥
返回 `200`. 注册总是范围到两个认证
`user_uuid`现在我们IAM `project_id`.

```json
{
  "transport": "fcm",
  "platform": "ios",
  "registration_token": "<FCM registration token>",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  }
}
```

`encryption` 是一个RESTAlchemy类型模型.唯一支持的类型是`HPKE`,
使用 X25519, HKDF-SHA256,和 AES-256-GCM 的基调模式, `public_key` 必须
对于初始的数据,
API 版本,响应反映了 `registration_token` 和 `public_key` 从
存储模型.目前支持的平台是`android`和`ios`.

```json
{
  "uuid": "7c1af344-95e1-487e-8b51-d1af0370cdb5",
  "transport": "fcm",
  "platform": "ios",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "registration_token": "<FCM registration token>",
  "created_at": "2026-07-26T05:30:00Z",
  "updated_at": "2026-07-26T05:40:00Z"
}
```

`DELETE` 返回 `204` 当它删除拥有注册和当
已经没有注册. 这个合同只管理注册;
有效载荷加密和交付都不在这个 API 变化之外.

## 文件 {#folders}

`POST /api/workspace/v1/messenger/folders/` 写到 `m_folders`. 阅读使用 `m_folders_view`.
答案隐藏 `project_id` 和 `user_uuid`.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 文件标识符. |
| `title` | 子,一个. | 是的 | 不需要 | 文件标题. |
| `background_color_value` | 整数 `0..2^32-1` 或 `null` | 不需要 | 不需要 | ARGB 颜色值 |
| `unread_count` | 整数 | 不需要 | 是的 | 总体活跃未读数. 无声流量除外. |
| `system_type` | `all`, `created`没有`null` | 不需要 | 是的 | 系统文件类型;默认为 `created`. |
| `folder_items` | 阵列 | 不需要 | 是的 | 从视图中嵌入文件项. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

创建请求:

```json
{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

举例:

```http
POST /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

反应示例:

```json
{
  "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "title": "Inbox",
  "background_color_value": 4280391411,
  "unread_count": 3,
  "system_type": "created",
  "folder_items": [
    {
      "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "chat_type": "stream",
      "order_index": 10,
      "pinned_at": null,
      "unread_count": 3,
      "active_unread_count": 3,
      "passive_unread_count": 0,
      "created_at": "2026-06-22T09:30:00Z",
      "updated_at": "2026-06-22T09:30:00Z"
    }
  ],
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

更新示例:

```http
PUT /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Archive",
  "background_color_value": 4289352960
}
```

删除示例:

```http
DELETE /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
```

实时副作用:

| 操作 | payload.kind | object_type | 有效载荷 |
| --- | --- | --- | --- |
| 创建文件 | `folder.created` | `folder` | 整个文件快照. |
| 更新文件 | `folder.updated` | `folder` | 整个文件快照. |
| 删除文件 | `folder.deleted` | `folder` | 只有 `folder.uuid`. |

## 文件项目 {#folder-items}

`POST /api/workspace/v1/messenger/folder_items/` 写到 `m_folder_items`. 阅读使用
`m_folder_items_created_view`.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 文件项标识符. |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `folder_uuid` | UUID | 是的 | 不需要 | 文件 UUID. |
| `user_uuid` | UUID | 不需要 | 是的 | IAM用户范围. |
| `stream_uuid` | UUID | 是的 | 不需要 | 流量 UUID. |
| `chat_type` | `stream`, `group`, `private` | 是的 | 不需要 | 聊天的人类. |
| `order_index` | 整数或 `null` | 不需要 | 不需要 | 手动排序索引 |
| `pinned_at` | 时间或 `null` | 不需要 | 行动管理 | 标记时间. |
| `unread_count` | 整数 | 不需要 | 是的 | 对于此流和用户的原始未读数. |
| `active_unread_count` | 整数 | 不需要 | 是的 | 在有效流/topic通知模式下符合条件的未读消息. |
| `passive_unread_count` | 整数 | 不需要 | 是的 | 其他未读的消息来自默化的通知流量. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

创建请求:

```json
{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

创建一个例子:

```http
POST /api/workspace/v1/messenger/folder_items/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

反应示例:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": null,
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

和解返回相同的文件项目形状. `pin`设置`pinned_at`到
现在的时间 UTC; `unpin` 设置为 `null`.

引脚示例:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/pin/invoke
Authorization: Bearer <access_token>
```

引脚响应示例:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": "2026-06-22T09:31:00Z",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```

解锁示例:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/unpin/invoke
Authorization: Bearer <access_token>
```

删除示例:

```http
DELETE /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50
Authorization: Bearer <access_token>
```

实时副作用:

| 操作 | payload.kind | object_type | 有效载荷 |
| --- | --- | --- | --- |
| 添加流到文件 | `folder.updated` | `folder` | 完全的父文件快照使用`folder_items`. |
| 在文件中的流 | `folder.updated` | `folder` | 更新的 `pinned_at` 的全父文件快照. |
| 在文件中解锁流 | `folder.updated` | `folder` | 完全的父文件快照使用`pinned_at: null`. |
| 从文件中删除流 | `folder_item.deleted` | `folder_item` | 只有 `folder_item.uuid`. |

## 流水

`POST /api/workspace/v1/messenger/streams/` 提交了正规流,
通过 PostgreSQL 创建一个
默认主题名为 `General Topic` 并将其 UUID 存储为
`default_topic_uuid`.
当当前默认主题为 `null` 时,引用是可以取消的
删除. REST资源响应遵循标准的 RestAlchemy JSON包装器
并且省略了值为 `null` 的可取值字段,因此客户端必须处理
缺少 `default_topic_uuid` 作为 `null`. 持久 `stream.updated` 事件满
快照并将`default_topic_uuid: null`明确保存.

如果提供`direct_user_uuid`,后端创建一个普通流
与其他任何文件一样的绑定,角色,主题,事件和文件 ACL 规则
它们的唯一额外的不变量是`private: true`,
项目范围流 UUID 对于无序的身份对,和 `owner`
对于该对的唯一用户.
通过自行聊天使用重复对 `(user, user)`,
包含当前用户的确切一个绑定,并返回当前用户
UUID 在 `direct_user_uuid` 中.重复或同时发送相同的请求
对于一个对返回现有流. 重复使用对冲突
源或直接身份字段返回 HTTP `400` 而不是改变或
默默地忽略了所要求的身份.

支持的源载荷:

```json
{
  "source_name": "native",
  "source": {
    "kind": "native"
  }
}
```

```json
{
  "source_name": "zulip",
  "source": {
    "kind": "zulip",
    "stream_id": 123,
    "server_url": "https://zulip.example.com",
    "topic_name": null,
    "message_id": null
  }
}
```

`zulip`有效载荷形状是提供商的来源. 一个注册的 Zulip运行时间
通过私人提供商填写它 HTTP API;浏览器合同隐藏
提供商协议的原始标识符,凭证和同步状态.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 流量识别器 |
| `name` | 连接器,最大 255 | 是的 | 不需要 | 流的名称. |
| `description` | 连接器,最大 255 | 不需要 | 不需要 | 流描述;默认为空字符串. |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `owner` | UUID | 不需要 | 是的 | 从用户流视图中看到所有者. |
| `user_uuid` | UUID | 不需要 | 是的 | 在用户流视图中的当前用户. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | 不需要 | 是的 | 目前用户的角色. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | 不需要 | 用户范围的操作管理 | 目前用户的流通告模式;默认为`all_messages`. |
| `unread_count` | 整数 | 不需要 | 是的 | 目前用户的未读数量. |
| `active_unread_count` | 整数 | 不需要 | 是的 | 根据有效流/topic通知模式,当前用户的未读数值是符合条件的. |
| `passive_unread_count` | 整数 | 不需要 | 是的 | 现在的用户未读的数量从默化的通知流量. |
| `source_name` | `native`, `zulip` | 不需要 | 不需要 | 源名;默认为 `native`. |
| `source` | 目标 | 不需要 | 不需要 | 源载荷;默认为 `{"kind": "native"}`. |
| `invite_only` | 布尔式 | 不需要 | 不需要 | 只有邀请者可以播放. |
| `announce` | 布尔式 | 不需要 | 不需要 | 广告流旗. |
| `direct_user_uuid` | UUID | 不需要 | 不需要 | 直接聊天对应.仅用于自动聊天,即当前用户 UUID. |
| `private` | 布尔式 | 不需要 | 是的 | 个人流旗. |
| `is_archived` | 布尔式 | 不需要 | 行动管理 | 档案的旗. |
| `color` | 整数 `0..0xFFFFFF` | 不需要 | 不需要 | 流色;如果省略或 `null`,则随机生成. |
| `last_message_uuid` | UUID没有`null` | 不需要 | 是的 | 最后的消息在流中,或者是空时是 `null`. |
| `default_topic_uuid` | UUID没有`null` | 不需要 | 是的 | 当前默认主题 UUID,或者 `null` 当没有默认配置时. |
| `provider` | 对于一个物体或 `null` | 不需要 | 是的 | 提供商支持流的提供商标志;原生流的 `null`. |
| `delivery` | 对于一个物体或 `null` | 不需要 | 是的 | 现在的提供者命令交付预测. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

创建请求:

```json
{
  "name": "Engineering",
  "description": "Engineering workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false
}
```

直接创建聊天请求:

```json
{
  "name": "Direct",
  "description": "Private workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "33333333-3333-3333-3333-333333333333"
}
```

自我聊天创建请求:

```json
{
  "name": "Personal notes",
  "description": "",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "11111111-1111-1111-1111-111111111111"
}
```

对于自我聊天, `direct_user_uuid`必须等于当前的 IAM 用户 UUID,
包含标志主题 UUID.自动聊天是本地. 响应是
标准流资源,其中 `private: true`,一个当前用户 `owner`
绑定,并且在 `direct_user_uuid` 中具有相同的当前用户 UUID;没有单独的聊天
显示了自行聊天的标志.
`private && direct_user_uuid == current_user_uuid` 稳定的客户端
通过检查身份,同时保留普通私人集团流,
`direct_user_uuid`仍然存在`null`.

直接会员是不可变的.
身份对:一个约束自行聊天和两个普通直接
添加或删除参与者,更新约束作用返回 HTTP
`400`.删除自行聊天流也返回HTTP `400`所以消息历史记录
通过删除和重建确定性身份来取代.
`source_name`必须与 `source.kind`相匹配,当创建流.
对于每一个流, 字段是不变的.`direct_user_uuid`,
`private`并且是内部`private_index`它们也不可变;
改变这些身份字段中的任何一个返回 HTTP `400`.

流通通知模式请求:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/notifications/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "notification_mode": "mentions_only"
}
```

本源流突变更新正规状态PostgreSQL及其实时
要求交易的副作用. 无效的 `provider` 和
`delivery` 字段描述外部投影和操作状态;
`null`对于原生流.

流读取操作:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/read/invoke
Authorization: Bearer <access_token>
```

`read`标记流中所有未读的消息为当前用户读取,
返回更新的流视图.

实时副作用:

| 操作 | payload.kind | object_type | 有效载荷 |
| --- | --- | --- | --- |
| 创建流 | `stream.created` | `stream` | 完全的用户流快照. |
| 创建流 | `folder.updated` | `folder` | 更新了 `All chats` 和 `Channels`/`Personal` 的系统文件快照. |
| 更新流 | `stream.updated` | `stream` | 每个流用户的全用户流快照. |
| 存档或不存档流 | `stream.updated` | `stream` | 每个流用户的全用户流快照. |
| 变更流通告模式 | `stream.updated` | `stream` | 仅为当前用户提供全用户流快照. |
| 阅读流消息 | `stream.read` | `stream` | 完全的用户流快照由该操作返回. |
| 阅读流消息 | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | 更新了当前用户未读数的快照. |
| 删除流 | `stream.deleted` | `stream` | 只有删除的流 `uuid`,发送给每个流用户. |
| 删除流 | `folder.updated` | `folder` | 在删除流之后更新受影响用户系统/custom文件快照. |
| 添加流链接 | `stream.created` | `stream` | 添加了用户的完整用户流快照. |
| 添加流链接 | `stream_bindings.created` | `stream_binding` | 对于现有流参与者,新的流绑定快照. |
| 添加流链接 | `folder.updated` | `folder` | 更新了用户的 `All chats` 和 `Channels`/`Personal` 系统文件快照. |
| 删除流绑定 | `stream.deleted` | `stream` | 只有流 `uuid`,发送给被删除的用户. |
| 删除流绑定 | `stream_binding.deleted` | `stream_binding` | 删除了 `uuid`, `stream_uuid` 和 `user_uuid` 绑定,发送给剩余的每个流参与者. |
| 删除流绑定 | `folder.updated` | `folder` | 更新删除用户系统/custom文件快照 访问删除后. |

对于直接私有流,一个 `stream.created` 事件为每个记录
流创建也为每个事件写`folder.updated`
参与者的 `All chats`文件,以及当流是私人的 `Personal`,
或 `Channels` 当它不是私有.

## 流体结合

流绑定是正规的 PostgreSQL 聊天会员记录. 新绑定
通过
`POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke`,其中
请求体组按角色添加用户. `who_uuid` 始终被覆盖.
与当前的 IAM 用户的 UUID.
当创建一个新的绑定时,添加的用户会获得`stream.created`
对于新可见的流,`folder.updated`活动`All chats`
并且取决于流隐私,要么是 `Personal` 或 `Channels`.
流程参与者获得一个.`stream_bindings.created`包含的事件
对于整个添加批量. 每个之前提交的消息
建立的绑定是可见的新成员与`read=true`,所以两者
流和主题未读计数器从零开始.
结合文件直到新成员阅读之前才会被阅读.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 具有约束力的标识符. |
| `project_id` | UUID | 是的 | 不需要 | 项目范围 |
| `stream_uuid` | UUID | 是的 | 不需要 | 流量 UUID. |
| `user_uuid` | UUID | 是的 | 不需要 | 用户接收访问. |
| `who_uuid` | UUID | 不需要 | 是的 | 执行该操作的用户. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | 不需要 | 不需要 | 角色;默认设置为 `member`. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | 不需要 | 不需要 | 用户的流通告模式;默认为`all_messages`. |
| `notification_updated_at` | 时间 | 不需要 | 不需要 | 最后写入获胜时间与`notification_mode`对应;默认为Unix时代,通知操作将其设置为当前服务器时间. 它包含在REST和实时绑定快照中. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

添加用户请求:

```json
{
  "member": [
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444"
  ],
  "owner": [
    "55555555-5555-5555-5555-555555555555"
  ]
}
```

删除绑定删除该用户访问流.
接收`stream.deleted`然后`folder.updated` 对于受影响的系统,并
收到的文件.
`stream_binding.deleted`没有了带.`uuid`, `stream_uuid`,以及
`user_uuid`.对于提供商支持的流,添加和删除绑定也
排队时间长,能力限制 `membership.add` 和 `membership.remove`
提供者桥解决了映射的提供者身份和
订阅或取消订阅;本地流没有提供商操作.

## 流媒体主题

`POST /api/workspace/v1/messenger/stream_topics/` 提交了正规主题,
实际时间副作用在PostgreSQL读数已经完成.
通过当前流成员身份向当前的 IAM 用户.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 标题标识符 |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `name` | 连接器,最大 128 | 是的 | 不需要 | 题目名称 |
| `stream_uuid` | UUID | 是的 | 不需要 | 流量 UUID. |
| `user_uuid` | UUID | 不需要 | 是的 | 目前的主题视图中的用户. |
| `color` | 整数 `0..0xFFFFFF` | 不需要 | 不需要 | 题目颜色;如果省略或 `null`,则随机生成. |
| `last_message_uuid` | UUID没有`null` | 不需要 | 是的 | 话题中的最新消息,或者空时是`null`. |
| `unread_count` | 整数 | 不需要 | 是的 | 目前用户为主题未读的原始数量. |
| `active_unread_count` | 整数 | 不需要 | 是的 | 目前用户未读的 `unmute` 引用,所有未读的 `follow` 或继承的 `default` 活跃数. |
| `passive_unread_count` | 整数 | 不需要 | 是的 | 应用有效通知模式后当前用户剩余未读数 |
| `is_default` | 布尔式 | 不需要 | 是的 | 这个主题 UUID 是否等于流的 `default_topic_uuid`. |
| `is_done` | 布尔式 | 不需要 | 行动管理 | 目前用户的标志已经完成. |
| `notification_mode` | `mute`, `default`, `unmute`, `follow` | 不需要 | 用户范围的操作管理 | 目前用户的主题通知模式;默认为`default`. |
| `summary` | 字符串,最大4096或`null` | 不需要 | 是的 | 最新的 LLM 生成摘要,由服务器端摘要代理编写. |
| `summary_last_message_uuid` | UUID没有`null` | 不需要 | 是的 | 最新主题消息实际包含在 `summary`;由服务器端总结代理编写,并且 `null` 是空主题的有效. |
| `summary_has_new_messages` | 布尔式或 `null` | 不需要 | 是的 | `null`没有总结;否则,当前最新的消息是否与`summary_last_message_uuid`不同. |
| `summary_enabled` | 布尔式 | 不需要 | 行动管理 | 服务器端工作者是否可以更新这个主题;默认为`true`.禁用会保留当前的摘要和性元数据. |
| `summary_system_prompt` | 字符串,最大16384,或 `null` | 不需要 | 行动管理 | 特定主题的 LLM 系统提示符; `null` 选择应用程序默认. |
| `summary_reasoning_effort` | `off`, `minimal`, `low`, `medium`, `high`没有`null` | 不需要 | 行动管理 | 每个总结推理选择; `off` 显式禁用推理,而 `null` 省略提供者选项.仅在选择的终点声明推理支持时使用. |
| `source_name` | `native`, `zulip` | 不需要 | 不需要 | 主题源名;如果省略,默认为`native`. |
| `source` | 目标 | 不需要 | 不需要 | 目标来源有效载荷. |
| `provider` | 对于一个物体或 `null` | 不需要 | 是的 | 提供者支持的主题提供商章;原生主题 `null`. |
| `delivery` | 对于一个物体或 `null` | 不需要 | 是的 | 现在的提供者命令交付预测. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

当 `summary_system_prompt` 为 `null` 时,应用程序默认要求
简短的摘要,保存决定,所有者,未解决的问题,以及
重要限制,用主题使用的主要语言写.

创建请求:

```json
{
  "name": "Releases",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
}
```

`PUT /api/workspace/v1/messenger/stream_topics/{topic_uuid}`需要一个体,有 `name`.
检查当前用户是否与主题流之前有联系
取代主题.原生更改更新了正规的 PostgreSQL状态和它们的
实际时间副作用是原子的.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` 翻转
`is_done` 对于所有主题用户,并返回当前用户的更新主题视图.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` 设置
主题为其流的默认,并返回当前用户的更新主题
操作是无源的. 已改变的默认发出`stream.updated`
对于每一个流用户,以及 `topic.updated` 对于以前和新的默认
问题.

主题总结只由服务器端总结代理通过一个
内部助手;没有公开 REST 操作来写 `summary` 或
`summary_last_message_uuid`. 助手将两个字段存储在原子中,
验证非零边界是否识别主题中的消息,拒绝
一个较旧的边界,当一个较新的已经存储,并发出
`topic.updated` 流参与者的快照. 每一个成功的写
附上一个包含摘要的私人服务器端日志条目,
没有任何限制.UUID并且订购时间,并生成时间.
消息是硬删除,日志条目在或在该消息后
已无效,最新的早期条目恢复 (或简要清除),
废弃的工人工作被丢弃,恢复的快照被发射到
交易是一样的.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke`
更新了特定主题的总结配置:

```json
{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "medium",
  "summary_enabled": true
}
```

设置 `summary_system_prompt` 为 `null` 恢复应用程序默认状态.
每个字段都是可选的,但请求必须包含至少一个字段.
省略一个字段将保留其当前值.
`summary_reasoning_effort`作为`null`通过了理由请求.
设置它,并不是对端点配置.
发送到 `off` 显式的 OpenAI 兼容供应商值 `none`
设置一个系统,以实现
`summary_enabled`到`false`取消了这个主题的待处理工作,并阻止
新索赔同时保留当前的摘要;将其重新设置为 `true`
允许工人更新任何陈旧的内容.
只有流所有者和管理员可以更新此配置;其他角色,
包括主持人,接收 `403 Forbidden`.

### 主题概述客户端工作流程

客户端将摘要读成普通主题快照的一部分:

```http
GET /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047
Authorization: Bearer <access_token>
```

```json
{
  "uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "last_message_uuid": "b5ff6f76-bcfe-4fb9-9c28-e0cb790d2e52",
  "summary": "The team approved the release scope; two follow-ups remain open.",
  "summary_last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "summary_has_new_messages": true,
  "summary_enabled": true,
  "summary_system_prompt": "Summarize decisions and open questions.",
  "summary_reasoning_effort": "medium"
}
```

界面显示`summary`,可以将其标记为过时
`summary_has_new_messages`是`true`.它不会发送消息到LLM或
写摘要字段.一个 `topic.updated` 事件包含完整更新的主题
连接客户端可以更换本地主题状态,
调查或专门的总结终点.

管理员或所有者可以更改服务器端代理使用的提示:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "high",
  "summary_enabled": true
}
```

操作返回全主题快照,并发出`topic.updated`到
设置`summary_system_prompt`为`null`选择了
设置或重新启动 LLM 工作仍然是一个
服务器端代理人的责任.

暂停一个主题的自动更新,而不会删除现有的主题
总结,同一个操作只能发送主题门:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_enabled": false
}
```

### 主题总结管理 {#topic-summary-administration}

题目总结有两个独立的数据库支持的门.
只有当 `global_enabled` 和当前项目的 `project_enabled` 都是
true. 设置更新提供两个值:

```http
PUT /api/workspace/v1/messenger/topic_summary_settings/12345678-1234-4234-8234-123456789abc
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "global_enabled": true,
  "project_enabled": true
}
```

路径 UUID必须与请求文本中的 IAM 项目相等.读取是
项目用户可使用;需要更新
`workspace.topic_summary_settings.manage`.

LLM创建一个与
`workspace.topic_summary_endpoint.manage`:

```http
POST /api/workspace/v1/messenger/topic_summary_endpoints/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "uuid": "e4ad6d80-6bc7-4a91-864c-8e97319a82bd",
  "name": "primary-summary-model",
  "base_url": "https://llm.example.com/v1",
  "model": "summary-model",
  "api_key": "<write-only credential>",
  "enabled": true,
  "priority": 10,
  "supports_vision": true,
  "supports_reasoning": true,
  "temperature": 0.2,
  "max_output_tokens": 512,
  "top_p": 1.0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0
}
```

所有终端实现了与OpenAI兼容的`POST {base_url}/chat/completions`;
没有提供商特定的分支机构.较低的 `priority` 值首先运行,然后
UUID 是确定性结局. 启用终点被声称具有
限制租.一个可重新测试的网络,速度限制,或服务器故障释放
试图在该顺序下一个终点,最多三次.
登记处显示了有限的健康数据 (`last_success_at`,`last_failure_at`,
`failure_count`,和`last_error_code`) 但从来没有暴露一个活跃的索赔令牌.

`api_key` 仅在创建或替换凭证时接受.
在存储之前,使用密码,
获取或更新,写入Workspace事件,复制到主题快照中,或者
登记库更新和删除是普通的
登记处故意没有任何
修订, `ETag`或 `If-Match` 合同.

生成设置有以下默认值和接受范围:

| 场地 | 默认 | 范围 |
| --- | --- | --- |
| `temperature` | `0.2` | `0.0..2.0` |
| `max_output_tokens` | `512` | `1..32768` |
| `top_p` | `1.0` | `0.0..1.0` |
| `presence_penalty` | `0.0` | `-2.0..2.0` |
| `frequency_penalty` | `0.0` | `-2.0..2.0` |

没有Messenger工作者要求一个老话题,最多100个新消息
步骤,快照的边界和有效提示,承诺的要求,执行
在每个数据库交易之外的 LLM 请求,并且结果在
通过现有的内部总结辅助器进行新的交易.
重试延误,终点租和索赔到期都会被存储,因此重试仍然存在
它们是有限的,可观察的.

长时间的推理是提供商的正常反应,
连接时间是30秒,而响应时间是25分钟,所以
客户端的默认模式是:
终点租时间为30分钟,主题-工作租时间为90分钟.
工人还执行至少响应时间内加上终端租
另一个问题是,如果您的用户是一个用户,
工人在反应缓慢或即时故障转换时无法恢复现场工作.

当边界消息批量包含一个 Workspace 图像和任何启用
如果视觉终点存在,只能选择一个视觉终点.
终端是忙碌的,工作等待;它不归回一个自由文本终端.
只有没有图像的批量,只有在没有
已启用视觉终点存在.图像仅在用户消息中编码
系统提示符始终保持仅仅是文本形式.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` 设置
目前用户的主题通知模式:

```json
{
  "notification_mode": "follow"
}
```

允许的主题通知模式是`mute`,`default`和`follow`. `unmute`
只有当当前用户的流通告模式为`muted`时才允许.
没有读的分类是从当前设置中评估的,因此改变
模式立即重新分类现有的未读消息.`follow`让每个
主题未读活动, `unmute` 仅直接提到当前用户
活动`mute`让每一个未读的话题都被动,`default`继承了
流模式. `mentions_only`中的流也只会直接提到
在 `active_unread_count`;所有剩下的未读的原始消息都保持在
`passive_unread_count`.

对于提供商支持的Zulip流和主题,通知行动是排队的
提供商更新将被应用到
Workspace 设置时间,所以一个较旧的更新不能取代一个
现在的新型.

主题阅读操作:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/read/invoke
Authorization: Bearer <access_token>
```

`read`标记所有未读的消息在主题中为当前用户读
返回更新的主题视图.

实时副作用:

| 操作 | payload.kind | object_type | 有效载荷 |
| --- | --- | --- | --- |
| 创建主题 | `topic.created` | `topic` | 每个流用户的全用户主题快照. |
| 重新命名主题 | `topic.updated` | `topic` | 每个流用户的全用户主题快照. |
| 切换完成 | `topic.updated` | `topic` | 每个流用户的全用户主题快照. |
| 设置默认主题 | `stream.updated`, `topic.updated` | `stream`, `topic` | 更新了每个流用户的流快照和之前/new默认主题快照. |
| 服务器总结更新 | `topic.updated` | `topic` | 每个流用户的全用户主题快照. |
| 设置总结提示 | `topic.updated` | `topic` | 每个流用户的全用户主题快照. |
| 改变主题通知模式 | `topic.updated`, `stream.updated` | `topic`, `stream` | 重新分类主题和流未读的快照仅为当前用户. |
| 阅读主题消息 | `topic.read` | `topic` | 完全用户主题快照由该操作返回. |
| 阅读主题消息 | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | 更新了当前用户未读数的快照. |
| 删除主题 | `topic.deleted` | `topic` | 删除主题 `uuid` 和 `stream_uuid`,发送给每个流用户.删除默认主题也会发出 `stream.updated` 和 `default_topic_uuid: null`. |

## 信息 {#messages}

`POST /api/workspace/v1/messenger/messages/`验证当前的 PostgreSQL 流
成员和承诺的规范UTF-8下标信息,旗,一个共享
接收者观众快照,以及紧的消息/topic/stream
请求交易.它不会为每个接收者创建一个正规事件行.
读取仍然限于当前的 IAM 用户,并保留现有的响应.

唯一支持的消息有效载荷是:

```json
{
  "kind": "markdown",
  "content": "Hello, workspace"
}
```

Workspace 标记内容内的实体引用使用常规标记链接
语法. URL 部分是一个 Workspace URN:

| 实体 | 标记表格 | 其他 |
| --- | --- | --- |
| 用户提及 | `[Jane Doe](urn:user:<user-uuid>)` | 作为用户标签/mention. |
| 消息链接 | `[See message](urn:message:<message-uuid>)` | 指向一个 Workspace 消息. |
| 流链接 | `[general](urn:stream:<stream-uuid>)` | 指向一个Workspace流. |
| 问题链接 | `[deploys](urn:topic:<topic-uuid>)` | 指向一个 Workspace 主题. |
| 文件链接 | `[report.pdf](urn:file:<file-uuid>?name=report.pdf)` | 文件/media URN 可能包含元数据查询参数. |
| 图片/video链接 | `![photo.png](urn:image:<file-uuid>?name=photo.png)` | 图像和视频使用`urn:image` / `urn:video`. |
| 像/default 图像 | `[avatar](urn:gravatar:<hash>)` | 同样规范的Gravatar URN格式与 Workspace用户相同;哈希是32或64个十六进制字符. |
| 外部 URL | `[site](urn:url:https://example.com)` | 外部 `http` / `https` 链接通过 `urn:url` 存储. |

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 信息标识符. |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `stream_uuid` | UUID | 是的 | 不需要 | 流量 UUID. |
| `topic_uuid` | UUID | 不需要 | 不需要 | 主题 UUID;省略或 `null` 使用流默认主题.当流没有默认时,请求在代码 `400001007` 中失败. |
| `author_uuid` | UUID | 不需要 | 是的 | 消息作者 |
| `payload` | 目标 | 是的 | 不需要 | 标记下来信息的有效载荷; 剪切内容必须为1.4万个字符. |
| `user_uuid` | UUID | 不需要 | 是的 | 目前用户在用户消息视图中. |
| `read` | 布尔式 | 不需要 | 是的 | 目前用户的读标志.作者是作为读者创建的. |
| `pinned` | 布尔式 | 不需要 | 是的 | 目前用户的固定标志. |
| `starred` | 布尔式 | 不需要 | 是的 | 目前用户的星号旗. |
| `is_own` | 布尔式 | 不需要 | 是的 | 否 `author_uuid` 是当前用户. |
| `mentioned` | 布尔式 | 不需要 | 是的 | 标记载载是否提到当前用户;默认为 `false`. |
| `reactions` | 目标 | 不需要 | 是的 | 总反应数以 `emoji_name` 键. |
| `reaction_users` | 目标 | 不需要 | 是的 | 对于被 `emoji_name` 键入的边界反应组,完成持久用户 UUID 列表.一个空的对象或缺失的密钥意味着仅计数;列表从来不是部分的. |
| `source_name` | `native`, `zulip` | 不需要 | 不需要 | 消息源名称;如果省略,公众 API 默认将其设置为 `native`. |
| `source` | 目标 | 不需要 | 不需要 | 消息源有效载荷;默认为`{"kind": "native"}`. Zulip `message_id`可以是`null`,直到输出同步成功. |
| `provider` | 对于一个物体或 `null` | 不需要 | 是的 | 提供商标志继承从选择的提供商支持流. |
| `delivery` | 对于一个物体或 `null` | 不需要 | 是的 | 现在创建/update/delete交付投影. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

创建请求:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  }
}
```

反应示例:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  },
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": true,
  "mentioned": false,
  "reactions": {},
  "reaction_users": {},
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

更新请求:

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Edited text"
  }
}
```

`PUT /api/workspace/v1/messenger/messages/{message_uuid}` 提交更新的正规消息有效载荷并返回
目前用户的消息视图. 只有消息作者可以更新根
执行 `DELETE /api/workspace/v1/messenger/messages/{message_uuid}`
立即硬删除的正规信息及其用户状态.
同一个交易发出一个最小的 `message.deleted` 事件
观众,保存所需的消息身份和来源字段.

阅读行动:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` 将当前用户的消息标志设置为`true`,并返回更新的
如果消息未读,后端会发出 `message.read`
完整的消息快照和未读数的更新.

阅读到行动:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read_up_to/invoke
Authorization: Bearer <access_token>
```

`read_up_to` 标记在同一主题中未读的消息通过选择
信息的包含 `(created_at, uuid)` 边界,然后返回所选的
对于外部聊天, Workspace 发送已经解决的 UUID
预fix作为一个精确的选择器;提供商特定的消息排序不能改变
哪些消息被阅读.

星级和非星级行动:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/star/invoke
Authorization: Bearer <access_token>
```

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/unstar/invoke
Authorization: Bearer <access_token>
```

`star` 和 `unstar` 设置当前用户的 `starred` 标志并返回
更新消息视图. 两个操作都是无效的. 当旗改变时,
后端只发出`message.updated`用于当前用户.
通过 Workspace 并没有与外部提供者同步.

实时副作用:

| 操作 | payload.kind | object_type | 有效载荷 |
| --- | --- | --- | --- |
| 创建消息 | `message.created` | `message` | 每个流用户的全用户消息快照. |
| 创建未读消息 | `topic.updated`, `stream.updated` | `topic`, `stream` | 更新了未读数的快照,用于未读新消息的用户;UI从流快照中导出文件集. |
| 更新消息有效载荷 | `message.updated` | `message` | 每个流用户的全用户消息快照. |
| 产生/update/delete反应 | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` | `message_reaction` | 对于代理用户来说,反应快照. |
| 创建/update/delete 反应总和更新 | `message.updated` | `message` | 对于每个流用户,更新`reactions`和`reaction_users`的全用户消息快照. |
| 阅读消息或阅读到消息 | `message.read` | `message` | 完全的用户消息快照由该操作返回. |
| 阅读未读的消息 | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | 更新了当前用户未读数的快照. |
| 星或不星的消息 | `message.updated` | `message` | 当旗更改时,当前用户的全用户消息快照. |
| 删除消息 | `message.deleted` | `message` | 删除消息 `uuid`, `stream_uuid`, `topic_uuid`, `author_uuid`, `source_name`,和 `source`,发送给每个流用户. |
| 删除未读的消息 | `topic.updated`, `stream.updated` | `topic`, `stream` | 更新未读数的快照,用于未读删除消息的用户; UI 从流快照中导出文件总和. |

## 项目 {#drafts}

草案是 PostgreSQL 拥有的客户端状态,永远不会创建或修改正规的
没有阅读的计数器,反应,或文件引用.
现在,我们已经知道,IAM项目,所有者,流,和主题.
`stream_uuid`现在我`topic_uuid`它们是不可变的,
流,并且所有者必须是流参与者.
可能存在于同一流/topic对.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 是的 | 在创建后 | 客户端生成的身份密钥和草案标识符. |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `user_uuid` | UUID | 不需要 | 是的 | 根据 IAM 代币的草案所有者. |
| `stream_uuid` | UUID | 是的 | 在创建后 | 含有流体的流. |
| `topic_uuid` | UUID | 是的 | 在创建后 | 包含草案的主题;它必须属于 `stream_uuid`. |
| `payload` | 目标 | 是的 | 不需要 | 标记起草有效载荷.这是`PUT`唯一接受的字段. |
| `revision` | 整数,至少是1 | 不需要 | 是的 | 强度ETag修订,从`1`开始. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 最后更新时间. |

创建请求:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  }
}
```

答案:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  },
  "revision": 1,
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```

更新请求:

```http
PUT /api/workspace/v1/messenger/drafts/ca14d274-0057-4a9a-a34b-fb1174be6a17
Authorization: Bearer <access_token>
Content-Type: application/json
If-Match: "1"

{
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  }
}
```

创建请求需要 `uuid`, `stream_uuid`, `topic_uuid`,以及一个Markdown
值值内容是剪切,必须保持非空的,
通过重试相同的规范创建UUID返回
没有其他变化的现有草案;UUID具有不同的领域
返回 `409`.

`GET`,`POST`,和`PUT`单源响应返回强ETag如
`ETag: "3"`. `PUT`只接受`payload`; `PUT`和`DELETE`需要精确的
现在的值在 `If-Match`. 缺失的先决条件返回 `428`.
返回不有效的值 `412` 与当前的草案快照和当前的 ETag.
成功更新增量 `revision`;成功删除返回 `204`.

草案 CRUD 没有发出 Workspace 事件,网插件通知,桌面
通知,提供商命令,或普通Messenger另一个
客户端观察到重新加载或明确的草稿API重新设置的变化.
删除主题/stream 影响主题
通过PostgreSQL外键级联的水流,没有墓碑或
报告副作用.

## 信息的反应

消息反应是正规的 PostgreSQL 资源. 读取范围是
目前 IAM 用户可见的消息.
创建,更新或删除反应会发出一个 `message_reaction.*` 事件
对于代理用户和`message.updated`事件每个用户可以看到
消息;消息快照包含总结 `reactions` 和相同
持续的 `reaction_users` 投影,如 REST.

| 场地 | 类型 | 创建时需要 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 反应标识符 |
| `project_id` | UUID | 不需要 | 是的 | IAM 项目范围 |
| `message_uuid` | UUID | 是的 | 不需要 | 正在响应的消息;必须可见于当前用户. |
| `user_uuid` | UUID | 不需要 | 是的 | 反应的用户. |
| `emoji_name` | 连接器,最大 128 | 是的 | 不需要 | 情感符号/reaction名称 |
| `provider` | 对于一个物体或 `null` | 不需要 | 是的 | 提供商标志继承了目标消息. |
| `delivery` | 对于一个物体或 `null` | 不需要 | 是的 | 现在创建/update/delete交付投影. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

`provider_metadata` 和 `delivery_metadata` 是原始的DM存储字段,而不是
现在它们出现在生成的
`WorkspaceMessageReactions` OpenAPI方案,但运行时间
`resource_projection.as_dict(..., "message_reactions")` 序列化器删除
只有被消毒的`provider`和
`delivery`客户不得消费原油
生成的方案.

创建请求:

```json
{
  "message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "emoji_name": "thumbs_up"
}
```

同一个用户不能创建相同的 `message_uuid` 复制反应
现在我`emoji_name`. 任何可以看到消息的用户都可以列出或获取其
只有反应主才能更新或删除该反应.
这些操作将反应和相应的实时副作用
在 PostgreSQL.原生响应保留`provider: null`和`delivery: null`.

消息视图上的 `reactions` 字段是一个总体地图:

```json
{
  "thumbs_up": 2,
  "eyes": 1
}
```

`reaction_users` 字段只显示小组的完整UUID列表
按服务器配置选择. 默认的每个组值是4
对于用户 (`[messenger_reactions] user_list_limit`客户没有发送或
推断出这个极限:

```json
{
  "reactions": {
    "eyes": 12,
    "heart": 3
  },
  "reaction_users": {
    "heart": [
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222",
      "33333333-3333-3333-3333-333333333333"
    ]
  }
}
```

通过使用表情符号键来确保
如果当前数量超过配置的
写入将删除该键,而不是存储一个前.
消息没有被填写,因此返回 `reaction_users: {}`
改变配置的极限
没有重写现有的快照; 组的下一个突变应用
客户端在每一个 REST 或实时消息上更换整个地图
它们不能将其与以前的值合并.

反应实时有效载荷包括`uuid`,`project_id`,`message_uuid`,
`user_uuid`, `emoji_name`, `source_name`, `source`, `provider`没有`delivery`.
他们从来没有暴露原始的`provider_metadata`或`delivery_metadata`.
`message_reaction.updated`, `old_message_uuid`, `old_emoji_name`,
`old_source_name`,和 `old_source`描述之前的反应目标.

## 文件 {#files}

文件字节和一个独立的 JSON 侧车通过配置的存储
S3 是部署后端;本地后端
执行相同的测试布局. 后端由服务器选择
浏览器请求无法选择. PostgreSQL 存储
文件元数据和 ACL/access状态; S3 存储二进制文件及其 JSON
车辆的侧车.

侧车包含文件 UUID,项目 UUID,所有者 UUID,显示元数据,
内容类型,大小, SHA-256,创建时间,以及 ACL 规则.聊天文件包括
它们的流 UUID 并使用动态流成员规则:

```json
{
  "acl": {
    "mode": "stream_members",
    "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
  }
}
```

车旁从来没有包含参与者的快照.
转换数据,并下载请求检查认证用户与
现在的正规PostgreSQL流绑定.
参与者立即获得访问权限;被删除的参与者将失去访问权限
没有 S3 重写.

文件在整个认证 Workspace 中故意可见,使用此
ACL 代替:

```json
{
  "acl": {
    "mode": "public"
  }
}
```

`public`不是匿名访问.元数据和字节留在后面
Workspace IAM中间件,以及没有有效的 Workspace 持有人的任何请求
有效的 Workspace 持有人令牌可以读取或下载
`public`无论项目或流程成员是谁.`public`侧车
不应包含 `stream_uuid`;它保留`owner_uuid`和所有完整性
超级数据. Nginx 拒绝超过 `50m` 的多部分请求,
`workspace-messenger-api`.

| 场地 | 类型 | 需要在 JSON 创建时 | 仅供读取 | 描述 |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | 不需要 | 是的 | 文件标识符 |
| `project_id` | UUID | 不需要 | 是的 | IAM项目范围;隐藏在 API 响应中. |
| `user_uuid` | UUID | 不需要 | 是的 | 业主/uploader. |
| `stream_uuid` | UUID没有`null` | 是的 | 不需要 | 拥有聊天文件的流. 需要创建JSON和 `stream_members`多部分上传; 省略了使用`acl.mode=public`多部分上传. |
| `name` | 连接器,最大 255 | 是的 | 不需要 | 文件显示名称. |
| `description` | 连接器,最大 255 | 不需要 | 不需要 | 文件描述;默认为空字符串. |
| `content_type` | 字符串 | 是的 | 不需要 | MIME 内容类型 |
| `size_bytes` | 整数 | 是的 | 不需要 | 文件大小以字节. |
| `hash` | 字符串 | 是的 | 不需要 | 文件哈希,目前为多部分上传 SHA-256. |
| `created_at` | 时间 | 不需要 | 是的 | 创造时间. |
| `updated_at` | 时间 | 不需要 | 是的 | 更新时间. |

JSON 创建元数据请求:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "example.txt",
  "description": "Example",
  "content_type": "text/plain",
  "size_bytes": 12,
  "hash": "abc"
}
```

多部分上传请求:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
stream_uuid=75309057-419c-4b12-a7c1-3932429ec4a6
name=example.txt
description=Example
```

通过一个普通的验证客户端上传一个Workspace通过
通过将现有的 ACL 对象发送到 JSON 并省略相同的终点
`stream_uuid`:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
acl={"mode":"public"}
name=public-example.txt
description=Authenticated Workspace-wide file
```

对于多部分上传,需要 `file`,并且必须有一个范围
提供:要么是 `stream_uuid`,要么是 JSON 表格字段
`acl={"mode":"public"}`.公共上传拒绝`stream_uuid`;流上传
保留`stream_members` ACL. `name`默认的上传文件名和
`description`默认为空字符串.后端存储字节,设置
`content_type`计算出了下载部分的`size_bytes`写了一个
SHA-256 `hash`. 两种模式保持相同的二进制加 JSON 侧车布局和
现在我还在.`urn:file`, `urn:image`没有`urn:video`客户合同

`GET /api/workspace/v1/messenger/files/`, `GET /api/workspace/v1/messenger/files/{file_uuid}`,以及
`GET /api/workspace/v1/messenger/files/{file_uuid}/actions/download`需要访问文件.`PUT`并且
`DELETE` 需要文件所有权. 下载返回原始字节与存储
`Content-Type`, a `Content-Disposition`附件文件名,并强
`ETag`等于引用的 SHA-256 `hash` 通过文件元数据暴露.
是不变的文件 UUID;转换元数据发出 `file.updated`.删除一个
拥有文件删除其二进制对象和JSON侧车后的正规
文件被删除.


## 服务 {#services}

服务是通过普通WorkspaceAPI暴露的仅读目录条目.
`GET /api/workspace/v1/services/`列出可用的服务和
`GET /api/workspace/v1/services/{service_uuid}`返回一个服务.

| 场地 | 类型 | 描述 |
| --- | --- | --- |
| `uuid` | UUID | 服务标识符 |
| `name` | 连接器,最大 255 | 服务名称 |
| `description` | 连接器,最大 255 | 服务描述;默认为空字符串. |
| `service_url` | URL | 服务入口 URL. |
| `icon` | URL没有`null` | 选择性的图标 URL. |
| `created_at` | 时间 | 创造时间. |
| `updated_at` | 时间 | 最后更新时间. |

反应示例:

```json
{
  "uuid": "608919f5-ae0f-44fb-85bf-f1bf56534238",
  "name": "Messenger",
  "description": "Workspace Messenger",
  "service_url": "https://workspace.example.com/",
  "icon": "https://workspace.example.com/icon.svg",
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```


## 事件与时代 {#events-and-epoch}

事件是针对观众的持久 PostgreSQL 记录.
事件传输 `user_uuid`;紧的广播事件使用存储观众,因此
每个可见的客户都遵守相同的公共活动合同,
每个接收者只保留一个可规的事件行.
设置间隔,默认情况下为72小时;消息,文件,流/topic状态,
提供者映射,和其他正规资源从未被删除
策略. 修剪推进储存的保留地板,所以剩余的事件形成
一个完全可见的后.
`epoch_version`在一个 PostgreSQL 拥有的内部是单调的
`epoch_generation`.

`GET /api/workspace/v1/events/` 返回按 `epoch_version` 按默认上升排序的事件.
REST `/events/` 和websocket交付使用相同的平面模式,并且两者都读
从当前用户可见的 PostgreSQL 事件表面.
`GET /api/workspace/v1/epoch/`使用相同的表面.

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 124,
  "project_id": "project-uuid",
  "user_uuid": "recipient-user-uuid",
  "object_type": "message",
  "action": "created",
  "created_at": "2026-07-02T16:37:49.552044Z",
  "updated_at": "2026-07-02T16:37:49.552047Z",
  "payload": {
    "kind": "message.created",
    "uuid": "message-uuid",
    "project_id": "project-uuid",
    "user_uuid": "recipient-user-uuid",
    "stream_uuid": "stream-uuid",
    "topic_uuid": "topic-uuid",
    "author_uuid": "author-user-uuid",
    "payload": {"kind": "markdown", "content": "Hello"},
    "source_name": "native",
    "source": {"kind": "native"},
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "mentioned": false,
    "reactions": {},
    "reaction_users": {},
    "provider": null,
    "delivery": null,
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

最顶级字段仅描述事件行. `payload.kind`是唯一的 `kind`.
没有任何可能的高层次 `type`, `kind`, `stream_uuid`,或 `topic_uuid`

创建/update事件的消息,将相同的标记载载存储在
实体链接仍然是常规的`urn:user`下调链接,
`urn:message`,`urn:stream`,`urn:topic`,文件/media,像,或 URL URN.

Messenger实体创建,更新,读取和操作事件携带相同的完整
现在的用户从相应的 REST 获得的对象快照
终点或行动响应,加上 `payload.kind`.
操作事件使用一个包含 `kind`,资源的封面
`uuid`,和一个清洁的完整资源在 `snapshot`.
对于外部创建,更新和删除事件.

Messenger 实体删除事件最小:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, `source`

`stream_bindings.created`是一个批次操作有效载荷:

```json
{
  "kind": "stream_bindings.created",
  "uuid": "stream-uuid",
  "items": [
    {
      "uuid": "binding-uuid",
      "project_id": "project-uuid",
      "stream_uuid": "stream-uuid",
      "user_uuid": "added-user-uuid",
      "who_uuid": "owner-user-uuid",
      "role": "member",
      "notification_mode": "all_messages",
      "notification_updated_at": "2026-07-02T16:37:49.552044Z",
      "created_at": "2026-07-02T16:37:49.552044Z",
      "updated_at": "2026-07-02T16:37:49.552047Z"
    }
  ]
}
```

读取操作发出`message.read`,`topic.read`,或`stream.read`与全
操作响应对象在 `payload`. 读取调整发出
`topic.updated`,`stream.updated`,和`folder.updated`. 创建消息/delete
使用紧的 `topic.updated` 和 `stream.updated` 事件; UI 项目文件
总体从流的快照而不是接受一个潜在的大
每条消息的用户特定文件快照.

支持值:

| object_type | 行动 | payload.kind 例子 |
| --- | --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` | `message.created`, `message.updated`, `message.deleted`, `message.read`, `messages.read` |
| `message_reaction` | `created`, `updated`, `deleted` | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` | `stream.created`, `stream.updated`, `stream.deleted`, `stream.read` |
| `stream_binding` | `created`, `updated`, `deleted` | `stream_bindings.created`, `stream_binding.updated`, `stream_binding.deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` | `topic.created`, `topic.updated`, `topic.deleted`, `topic.read` |
| `user` | `updated` | `user.updated` |
| `folder` | `created`, `updated`, `deleted` | `folder.created`, `folder.updated`, `folder.deleted` |
| `folder_item` | `deleted` | `folder_item.deleted` |
| `file` | `created`, `updated`, `deleted` | `file.created`, `file.updated`, `file.deleted` |
| `external_account` | `created`, `updated`, `deleted` | `external_account.created`, `external_account.updated`, `external_account.deleted` |
| `external_chat` | `created`, `updated`, `deleted` | `external_chat.created`, `external_chat.updated`, `external_chat.deleted` |
| `external_operation` | `created`, `updated`, `deleted` | `external_operation.created`, `external_operation.updated`, `external_operation.deleted` |

现在的数据库是一个数据库.
现在的外部聊天
呼叫站点发出`external_chat.updated`用于目录和分配更改,以及
`external_chat.deleted` 移除一个突出物时;
`external_chat.created`仍然是一个注册的模式类型.

为了在处理过的光标后严格追赶,使用:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<saved_generation>&page_limit=500
```

`GET /api/workspace/v1/epoch/`返回最新可见事件光标和
对于当前的 IAM 用户来说,最古老的保存时代. `epoch_version`是直接的
`current_epoch_version` 的别名:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

对于新创建的空事件流, `epoch_version` 和
`current_epoch_version`它们是`0`, `minimum_epoch_version`现在是`1`,以及
`epoch_generation`仍然是一个非空的PostgreSQL拥有的世代.
`GET /api/workspace/v1/events/?epoch_version%3E=0`返回一个空的列表
没有任何错误.

客户坚持 `epoch_generation` 与 `epoch_version`. 一个简历
没有一代,一个改变的世代,一个未来的时代,
或一个比保留后更古老的时代返回 HTTP `410`
`type=EventsCursorExpiredError`, `error=epoch_pruned`原因,以及
目前/minimum 标记符字段. 响应是`Cache-Control: no-store`.
客户端然后清除衍生实体/blob缓存,加载权威快照,
并且从返回的生成中重新启动跟踪;服务器消息和域
没有删除数据.

## Workspace 使用者

Workspace用户存储在`m_workspace_users`.路线是全球性的
项目范围.

`GET /api/workspace/v1/me/`返回相同的`WorkspaceUser_Get`作为对象
`GET /api/workspace/v1/users/{user_uuid}`,使用用户UUID从IAM
客户端没有发送或导出一个用户UUID这个请求.
后端从 IAM 内省中取出`project_id`,更新IAM拥有的
返回本地用户名,姓名,姓氏,电子邮件投影,
Workspace状态,形象和存在字段.

IAM它们的身份被地投射出来.`/me/`或要求当前
通过 `/users/{user_uuid}` 创建或更新该用户的 Workspace
预测;列表 `/users/` 不会热切地导入每个 IAM 帐户.
`GET /users/{other_user_uuid}`查找仅是投影:它不导入
其他用户被查询后才发现IAM身份并返回
通过他们自己的认证来实现.Workspace活动.

当当前的 IAM 用户请求自己的 UUID 时, API 实现或
在返回之前,更新IAM身份投影.浏览器无法
提交源所有权字段. `zulip` 源字母标识一个
通过私人提供商通过 Zulip 运行时预测的外部身份
API.提供商凭证和原始识别器不是这个浏览器的一部分
资源.

| 场地 | 类型 | 描述 |
| --- | --- | --- |
| `uuid` | UUID | 用户标识符 |
| `username` | 没有任何问题. | 用户名. |
| `source` | `iam`, `zulip` | 用户来源. |
| `identity_kind` | `external` 或省略 | 只有外部提供商身份的仅读标记. |
| `display_name` | 字符串或省略 | 仅读提供者显示外部身份名称. |
| `provider` | 没有或没有 | 仅读取的外部身份封装包含`kind`和`account_uuid`;原始提供商标识符和凭证从未暴露. |
| `status` | `active`, `idle`, `offline`, `do_not_disturb` | 现在的状态. |
| `status_emoji` | 字符串或 `null`,最大 64 | 定制的存在表情符号. |
| `status_text` | 字符串或 `null`,最大 256 | 根据规定的存在文本. |
| `first_name` | 字符串或 `null` | 他们的名字. |
| `last_name` | 字符串或 `null` | 姓氏. |
| `email` | 字符串或 `null` | 电子邮件地址. |
| `avatar` | URN 字符串 | 用户形象.支持的值是`urn:gravatar:<32-or-64-hex-hash>`,`urn:image:<uuid>`和`urn:url:http(s)://...`.如果省略,Workspace将正常化的电子邮件与MD5哈希;没有电子邮件的用户将从UUID中获得不可逆转的MD5回归. |
| `last_ping_at` | 时间 | 最后一次 ping 的时间. |
| `created_at` | 时间 | 创造时间. |
| `updated_at` | 时间 | 更新时间. |

外部提供者可以将一个兼容Gravatar的化身像
`urn:gravatar:<md5(trim(lower(delivery_email)))>`.原始供应商标识符和
只有供应商的交付地址不被本合同中显示.

现场更新:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/presence/invoke
Content-Type: application/json

{
  "status": "active",
  "emoji": "coffee",
  "text": "Focusing"
}
```

验证用户只能更新自己的 `user_uuid`.
在 `last_ping_at` 中提供状态和当前时间.可选 `emoji`,
`text` 字段被存储为 `status_emoji` 和 `status_text`;省略了可选的字段
字段保留以前的值,并且明确`null`清除它们. Workspace消息工作者标记了过时的用户离线并发出`user.updated`事件,全用户
快照,包括 `avatar`,给每个项目的所有 Workspace 用户.

像上传是一个原子式的用户操作:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_upload/invoke
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<PNG, JPEG, GIF, or WebP binary part>
```

只有认证用户自己的UUID才被接受.最大的化身大小是
25 MiB.后端验证声明的 MIME 类型和二进制签名,
存储字节和 JSON 侧车通过配置文件后端,设置
`acl.mode`到`public`,省略`stream_uuid`,并只更新`user.avatar`到
`urn:image:<file-uuid>`. IAM所有的用户名,姓名和电子邮件字段仍然存在
只有读取.该操作在每个 Workspace 中发出全 `user.updated` 快照
项目

恢复像使用相同的用户权限:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_reset/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

恢复替换`user.avatar`为
`urn:gravatar:<md5(trim(lower(email)))>`或正规不可逆的 UUID
替换的自定义化身将失去公开访问
只要用户引用和投影行更新,它的二进制和
后者将从物品储存中移除.

## WebSocket实时总结 {#websocket-realtime-summary}

共同的websocket服务使用子协议`workspace.events.v1`并验证
持有人代币从 `Sec-WebSocket-Protocol`:

```ts
const ws = new WebSocket(
  "/api/workspace/v1/events/ws?last_epoch_version=124&epoch_generation=781203",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

接入后,服务器会发送更新的一些错过事件.
保存的光标. 然后它发送一个控制
`{"type":"ready","epoch_generation":"...","epoch_version":124}` 在任何
用户界面通知门将一直关闭,直到此时.
每个事件消息都是相同的平面事件对象由 REST返回
`/api/workspace/v1/events/`.网页软件服务没有发送
应用级 JSON `hello`或 `ping` 消息,并且不处理客户端
JSON `pong`没有`ack`它们可以发送协议级的信息.WebSocket控制 ping
设置的心跳间隔. 重新连接和赶上是驱动
通过持久的光标对. 过期的光标发送相同的输入
`epoch_pruned` JSON 错误为 REST 并关闭代码 `4410` 和原因
`epoch_pruned`.

对于保护的文件缓存, `file.created/updated/deleted` 无效一个 UUID.
在删除会员时,删除的用户会收到 `stream.deleted`;客户端
立即驱逐所有已缓存的元数据的保护区块
`stream_uuid`.剩下的参与者获得`stream_binding.deleted` (和
角色/settings 变化产生`stream_binding.updated`) 更新参与者
状态.一个410空白清除所有衍生的保护小块缓存入口.

详细的UI集成规则
`docs/workspace_ui_realtime_integration.md`.

## OpenAPI 和部署

运行时间 Workspace OpenAPI 文档可在
`/api/workspace/specifications/3.0.3`它描述了控制器支持的
IAM-认证的HTTP表面,没有提供商,邮件或日历
浏览器路线.中间件提供的`server_settings`别名和
单独事件 WebSocket 是记录的运行时接口,但没有显示
作为生成的 OpenAPI 路径.私人提供商合同是保持
单独在
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

后端元素 Workspace 安装独立的 `workspace-messenger-api`,
`workspace-api`, `workspace-messenger-events`,以及
`workspace-messenger-worker` 过程加上私人
`workspace-external-bridge-api`服务. PostgreSQL-规范运行时间是
没有启动或连接到邮件服务.该元素需要S3aaS
对象和 JSON 侧车和 DBaaS 对于可规性 Messenger 和提供者状态.
它将现有的 Workspace UI 构建为 Messenger 模式,并从
现在我们要做什么?

相关文件:

- [Workspace 建筑](architecture.md)
- [Workspace实时集成UI](workspace_ui_realtime_integration.md)
- [个人 Workspace 提供者 API](../workspace_provider_api_v1.yaml)
- [Zulip供应商产品和公共 API合同](zulip_bridge_v1_product_and_api.md)
