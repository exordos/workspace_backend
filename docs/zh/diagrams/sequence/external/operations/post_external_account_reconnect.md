# 重新连接外部帐户

[← 文件的主要索引](../../../../index.md) ·
[序列图表的索引](../../README.md) ·
[外部集成部分/runtime](../README.md)

`POST /api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke`

检查和替换仅供写入的帐户,然后恢复同步.

![序列图](diagrams/post_external_account_reconnect.svg)

[可编辑的源 PlantUML](diagrams/post_external_account_reconnect.puml)

## 查询

除了上面的变量路径,没有其他查询参数.

持有者代币的加上标题:

- `If-Match: "<revision>"` 必须

```json
{
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only"
  }
}
```

## 成功的答案

HTTP `200`:

```json
{
  "uuid": "0d4ae1d0-30ad-4d15-bf26-5789e8406201",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "00000000-0000-4000-8000-000000000001"
  },
  "credential_present": true,
  "status": "connecting",
  "live_ready": false,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 8,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z",
  "revision": 8
}
```

资源的审核答案包含严格的 `ETag: "<revision>"`.

## 错误

| HTTP | 公众行为 |
| --- | --- |
| `403` | 没有 `workspace.external_account.reconnect` 权限,或者资源在未经授权的区域. |
| `404` | 给定的区域中的资源不存在或看不到. |
| `428` | 没有 `If-Match`. |
| `412` | 修改不一致. |
| `403` | 提供商的政策/状态禁止重复连接. |
| `400` | 对于不允许的路径值,查询参数或身体,使用标准验证错误 RESTAlchemy. |

验证错误体示例:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## 边界 RestAlchemy

资源/控制器的目标广告 (报价文件,非生产代码)):

```python
class ExternalAccount(models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "m_external_accounts_v2"

    owner_user_uuid = properties.property(types.UUID(), required=True)
    settings = properties.property(EXTERNAL_ACCOUNT_SETTINGS_TYPE, required=True)
    status = properties.property(types.Enum(ACCOUNT_STATUSES), read_only=True)
    capabilities = properties.property(types.Dict(), read_only=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class ExternalAccountController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        ExternalAccount, hidden_fields=["owner_user_uuid"]
    )
```

`owner_user_uuid` 隐藏; 公开`settings.default_project_id`是一个直角.UUID它们的物质在目标存储器中被索引.`owner_user_uuid`引用用户Workspace没有`ON DELETE CASCADE`已被提取的,而已被索引的.`default_project_uuid`项目与 项目`ON DELETE RESTRICT`序列化时,最后一个仍然被投资于`settings`公共广告RestAlchemy没有使用.`relationships.relationship`为了JSON在表格中UUID因为关系是这样的.URI在物理图的边界上,每个规范的非聚态连接`*_uuid`是一个具有明确选择的引用操作的索引外部密钥. 清洁器隐藏了所有者,帐户数据,原始提供商ID,密钥证书,内部地址和原始协议字段.

## 同步交易

1. 验证查询,确定域,检查分辨率/体,并找到指数密钥的正则行.
2. 验证新Zulip凭证与预期的 verified realm UUID,
   provider user ID 和normalized `delivery_email`;任何不一致都被拒绝
   fail-closed 在更换之前.
3. 封锁修改;加密和替换帐户数据;设置
   状态 `connecting`,还没有准备好进行直播; 添加所需
   状态和不变的更新Outbox; 记录交易.
4. 验证错误留下旧的
   credential, connection, lease 和 sync 没有变化.

## 背景处理,事件和一致性

类型化 `delivery_snapshot_event` 服务 exact external-account
scope; topic task 没有放置. 提供所需状态
是一个稳定的工作 control plane.

背景处理器在一个DB交易中记录了实质化的状态和完整图像的完整封面`external_account.updated`;两个效果是共同提交或滚回. 在提交之后,单独的管理器WebSocket发送,重复和播放; API/worker不拥有客户端连接.

客户可见的一致性: 替换帐户数据和所需的生成记录; 检查,检测,应用的生成和现场工作准备性异步一致.

Reconnect 执行的完全相同的bootstrap connect: whole-account lease,
新的 supported queue/boundary, sequential realtime,然后才会出现 history root.
Old queue/cursor 没有需要持续恢复.:
[`zulip_bridge/coordination_and_recovery.md`](../../../../zulip_bridge/coordination_and_recovery.md).
Bridge 在现有下执行私人调用 realm-bound mTLS certificate;
Workspace 独立检查 current certificate/identity generation 和
account lease. 失败的检查新的Zulip`api_key`也没有改变这个 S2S
credential, 没有什么旧的. account connection state.

## 具有能力和并行性

UUID 客户端创建时创建一个帐户;业务独特性允许一个帐户 `(owner_user_uuid, provider_kind)`. 帐户数据的密码文本是单独存储的,永远不会串行..

重复者使用稳定的商业密钥和当前的原始状态.每个 immutable outbox事件创建一个单独的任务,具有独特的 `outbox_event_uuid`;重复传递该任务必须是具有进量,没有 coalescing. 专有处理主题Messenger从新条目到旧条目只适用于当所涉及的正规位置确实与`(project_id, topic_uuid)`有关;提供商的管理/阅读操作不会创建人工主题,也不会进入这个排队.

## 他们的来源

- [`workspace_api.md`](../../../../workspace_api.md) — 有权威的公共路线,一般JSON,页面,活动和合同 WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — 外部资源的清洁生命周期,许可和提供商语义.

[← 文件的主要索引](../../../../index.md) ·
[序列图表的索引](../../README.md) ·
[外部集成部分/runtime](../README.md)
