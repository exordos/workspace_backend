# GET /api/workspace/v1/messenger/topic_summary_endpoints/

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)

## 现有合同的地位和边界

目标规格是 docs-first. HTTP-合同仍然是当前合同 [`workspace_api.md`](../../../../workspace_api.md); 目标内部机制仅仅是建议.

![序列图](diagrams/get_topic_summary_endpoints_list.svg)

可编辑的源: [`get_topic_summary_endpoints_list.puml`](diagrams/get_topic_summary_endpoints_list.puml).

## 操作

**方法和方式:** `GET /api/workspace/v1/messenger/topic_summary_endpoints/`

**目的:** 获取全球 OpenAI 兼容的总结终点列表,按优先级和 UUID.

## 公开查询

没有身体;收藏过器和重新定义的顺序不接受.

目前的终点不接受`page_limit`,并读取整个终点注册
点是当前的差距.`page_limit`其他 `page_marker`:
缺失/`0` 给出`100`,`1..500` 得到正确,负,
并且 `>500` 给出 HTTP `400` 没有. JSON 仍然是一个数组;
页面性是通过标准的 `X-Pagination-Limit` 和
`X-Pagination-Marker`, 所以没有新的 JSON 字段出现..

## 成功的公众回应

HTTP `200`:

```json
[
  {
    "uuid": "e4ad6d80-6bc7-4a91-864c-8e97319a82bd",
    "name": "primary-summary-model",
    "base_url": "https://llm.example.com/v1",
    "model": "summary-model",
    "enabled": true,
    "priority": 10,
    "supports_vision": true,
    "supports_reasoning": true,
    "temperature": 0.2,
    "max_output_tokens": 512,
    "top_p": 1,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "credential_present": true,
    "failure_count": 0,
    "created_at": "2026-06-22T08:00:00Z",
    "updated_at": "2026-06-22T08:00:00Z"
  }
]
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

## 同步读取路径

1. 要求文件许可和项目范围.
2. 通过标准对象读取索引的实体行 RestAlchemy.
3. 清除帐户数据和申请字段,然后串行当前的公共表格.
4. 不要创建交易外框,任务,背景执行者请求,公共事件或工作 WebSocket.

这种读取没有副作用,并且在查询时不会进行聚合或恢复.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)
