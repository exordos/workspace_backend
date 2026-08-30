# 取消外部操作

[← 文件的主要索引](../../../../index.md) ·
[序列图表的索引](../../README.md) ·
[外部集成部分/runtime](../README.md)

`DELETE /api/workspace/v1/messenger/external_operations/{operation_uuid}`

取消允许此等待或错误完成的任务.

![序列图](diagrams/delete_external_operation.svg)

[可编辑的源 PlantUML](diagrams/delete_external_operation.puml)

## 查询

除了上面的变量路径,没有其他查询参数.

没有尸体,不要发送虚构的物体. JSON.

## 成功的答案

`204 No Content`; 答案体 JSON 没有.

## 错误

| HTTP | 公众行为 |
| --- | --- |
| `404` | 给定的区域中的资源不存在或看不到. |
| `400` | 无法取消操作. |
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
class ExternalOperation(models.ModelWithUUID, models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "m_external_operations_v2"

    external_account_uuid = properties.property(types.UUID(), required=True)
    target_uuid = properties.property(types.AllowNone(types.UUID()), default=None)
    action = properties.property(types.String(), required=True)
    status = properties.property(types.Enum(OPERATION_STATUSES), read_only=True)
    details = properties.property(types.Dict(), read_only=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class ExternalOperationController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(ExternalOperation)
    # Owner scope; retry, discard and preflight are narrow action overrides.
```

`external_account_uuid` 并且允许`null` `target_uuid`它们是直角形.UUID它们的特性,是指数的物理.`external_account_uuid`引用了c 的帐户`ON DELETE CASCADE`因为`target_uuid`现在的形式是不能正确的单个外部键.SQL目标句子应该选择目标的正规目录或FK的典型列,同时保留相同的公开目录.JSON `target_uuid`公共广告RestAlchemy没有使用.`relationships.relationship`为了JSON在表格中UUID因为关系是这样的.URI在物理图的边界上,每个规范的非聚态连接`*_uuid`是一个有明显选择的引用操作的索引外键. 卫生机隐藏了所有者,账户,原始供应商ID,密码证书,内部地址和原始协议字段.

## 同步交易

1. 验证查询,确定域,检查分辨率/体,并找到指数密钥的正则行.
2. 在所有者区域封锁操作;检查 `can_discard`;将提供商工作转换为终端状态;更新目标交付投影;记录不可更改的删除Outbox;删除公开操作行;记录交易.
3. 只有在交易被固定后返回回复;网络交付从来没有在交易内执行.

## 背景处理,事件和一致性

投影的类型化任务:创建单个 immutable
`delivery_snapshot_event` task 对于源Outbox事件和实际事件 scope
目的,当适用; 结合没有. placement topic
task/claim 没有创建.

背景处理器在一个DB交易中记录了实质化的状态和完整图像的完整封面`external_operation.deleted`;两个效果是共同提交或滚回. 在提交之后,单独的管理器WebSocket发送,重复和播放; API/worker不拥有客户端连接.

客户可见的一致性:HTTP 204 记录取消. 提供商取消和目标投射的交付/事件可能会延迟;重复是对稳定的操作身份的潜在.

## 具有能力和并行性

UUID 操作是稳定的定位/重复识别器. 尝试号码的增加和终端过渡被锁定在行中..

重复者使用稳定的商业密钥和当前的原始状态.每个 immutable outbox 事件创建一个单独的任务,具有独特的 `outbox_event_uuid`;该任务的重复交付必须是具有进量,没有 coalescing. 专有处理主题Messenger从新条目到旧条目只适用于当所涉及的正规位置确实与`(project_id, topic_uuid)`有关; 提供者管理/阅读操作不创建人工主题,也不属于这一排队.

## 他们的来源

- [`workspace_api.md`](../../../../workspace_api.md) — 有权威的公共路线,一般JSON,页面,活动和合同 WebSocket.
- [`zulip_bridge_v1_product_and_api.md`](../../../../zulip_bridge_v1_product_and_api.md) — 外部资源的清洁生命周期,许可和提供商语义.

[← 文件的主要索引](../../../../index.md) ·
[序列图表的索引](../../README.md) ·
[外部集成部分/runtime](../README.md)
