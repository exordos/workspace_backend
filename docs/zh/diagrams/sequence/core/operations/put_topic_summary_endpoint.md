# PUT /api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)

## 现有合同的地位和边界

目标规格是 docs-first. HTTP-合同仍然是当前合同 [`workspace_api.md`](../../../../workspace_api.md); 目标内部机制仅仅是建议.

![序列图](diagrams/put_topic_summary_endpoint.svg)

可编辑的源: [`put_topic_summary_endpoint.puml`](diagrams/put_topic_summary_endpoint.puml).

## 操作

**方法和方式:** `PUT /api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}`

**用途:**更新,启用或关闭,更改优先级或更换帐户数据.

## 公开查询

```json
{
  "enabled": false,
  "priority": 10,
  "supports_vision": false,
  "api_key": "<новые учётные данные только для записи>"
}
```

## 成功的公众回应

HTTP `200`:

```json
{
  "uuid": "e4ad6d80-6bc7-4a91-864c-8e97319a82bd",
  "name": "primary-summary-model",
  "base_url": "https://llm.example.com/v1",
  "model": "summary-model",
  "enabled": false,
  "priority": 10,
  "supports_vision": false,
  "supports_reasoning": true,
  "temperature": 0.2,
  "max_output_tokens": 512,
  "top_p": 1,
  "presence_penalty": 0,
  "frequency_penalty": 0,
  "credential_present": true,
  "failure_count": 0,
  "created_at": "2026-06-22T08:00:00Z",
  "updated_at": "2026-06-22T08:05:00Z"
}
```

状态和错误标记字段可能在回复中缺失,如果允许`null`并具有这个值. `api_key`和活跃请求代币永远不会返回.

## 公众错误

需要持有者代币 IAM. 错误的 UUID 或请求体返回 HTTP `400`; 没有管理权限  `403`. 标准验证错误体:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

对于每个具有终点注册的操作,需要 `workspace.topic_summary_endpoint.manage`;缺失的终点给出 `404`.

## 目标边界 RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceTopicSummaryEndpoint(
    models.ModelWithUUID,
    models.ModelWithTimestamp,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_topic_summary_endpoints"

    name = properties.property(types.String(max_length=255), required=True)
    base_url = properties.property(types.String(max_length=2048), required=True)
    model = properties.property(types.String(max_length=255), required=True)
    credential_present = properties.property(types.Boolean(), read_only=True)


class TopicSummaryEndpointController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceTopicSummaryEndpoint,
        convert_underscore=False,
        process_filters=True,
    )
```

这个全球资源没有公开的实体关系字段.它的公开字段`uuid`  scalar 属性UUID.任何内部外部密钥或数据引用都会被索引并具有明确的引用完整性作用;`api_key`仅可用于写作,存储在加密中,从未被串行..

## 同步交易

1. 要求管理和恢复 UUID.
2. 检查至少一个可更改字段或范围.
3. 如果有,加密新帐户数据和更新终点.
4. 在此中添加不可更改的内录 transactional outbox.

## 类型化任务和后台执行器

独立的 immutable `delivery_snapshot_event` task 从 exact scope 寄存器
更新了登记处的登记处,并安全地重新评估 source
outbox event; unique `outbox_event_uuid`, 没有 coalescing.

全球控制平面的后台执行者读取终点的当前状态. 现有的主题请求仍然有限;后续的摘要任务首先按优先级选择所包含的终点,然后按 UUID.

## 公众事件,重复和时间特征

更新不使用ETag或修改,也不创建公开事件Workspace. 击败最后一个固定的终点状态; 秘密永远不会返回.

没有一个已准备的公共事件 Workspace,因此管理员 WebSocket 不参与此操作.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)
