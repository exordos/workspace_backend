# `DELETE /api/workspace/v1/messenger/drafts/{draft_uuid}`


总目标可靠性变量:每个 immutable outbox 事件都会产生一个唯一的 immutable typed task `outbox_event_uuid`;并无 coalescing. Task 存储实际的 exact scope key,使用 lease/fencing, retry/backoff, max attempts/DLQ, reaper 和 idempotent effect guard. Topic scope 仅适用于 placement/message-binding work; shared rows 不会在 topic.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)

状态:在文件中首先开发的操作目标规格. 现行公开合同
没有变化,是 [`workspace_api.md`](../../../../workspace_api.md).
这个文件描述了交易和投影的目标边界;它不是
产品代码,迁移 SQL 或新终点.

![序列图](diagrams/delete_draft.svg)

[可编辑的源 PlantUML](diagrams/delete_draft.puml)

## 任命和公开合同

通过优化竞争力物理删除所有者草案.

认证:持有者IAM;`project_id`和当前`user_uuid`代币从文本中取出 IAM.

## 查询路径和参数

| 位置 | 姓名 | 类型 / 规则 |
| --- | --- | --- |
| 路径 | `draft_uuid` | UUID |
| 标题 | `If-Match` | 必须进行严格的准确审核 |

集合页面,如果它是,将保留当前的合同 `page_limit` 和 UUID
`page_marker` 返回 `X-Pagination-Limit`,以及
`X-Pagination-Marker` 只要下一个页面.

## 查询的本体

查询的本体不存在.

## 成功的答案

`204` 答案的空体.



## 错误和授权

缺失`If-Match`返回`428`.错误/过时的修改返回`412`,当前的图像和ETag.未能找到的草稿返回未找到.

验证错误时的答案的一般形式:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## 目标边界 RestAlchemy

```python
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceDraft(models.ModelWithUUID, models.ModelWithProject,
                     models.ModelWithTimestamp, orm.SQLStorableMixin):
    # Contract boundary only; target physical naming/decomposition is not selected.
    __tablename__ = "m_workspace_drafts"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.UUID(), required=True, read_only=True)
    topic_uuid = properties.property(types.UUID(), required=True, read_only=True)
    payload = properties.property(types.Dict(), required=True)
    revision = properties.property(types.Integer(min_value=1), read_only=True)


class WorkspaceDraftController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceDraft,
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow overrides preserve owner scope, keyset marker, ETag and If-Match.
```

每个公开引用的实体都被声明为 RestAlchemy 的标数UUID 属性,而不是 `relationship` (它会被串行为 URI).相应的物理列 `*_uuid` 一个具有明显选择的引用操作的索引外部密钥.因此公开JSON 保持UUID 没有变化.

目标内部模型的草稿是故意不被处理的. 广告固定了不变的尺度边界UUID/ETag. 物理UUID用户/流/主题列仍然是FK的索引,与当前合同的 Cascading行为;关系RestAlchemy不应该改变公开的 UUID JSON.

## 同步路径 API

1. 拆除 `If-Match`.
2. 封锁所有者稿件并对比修改.
3. 物理删除它并添加到Outbox中,内部不变域名,没有公开衍生.
4. 记录交易并恢复 `204`.

## Outbox, 类型化任务,工作和实时工作

目前的合约不会创建删除标记或公开事件.

内部 immutable 输出箱事件输出一个 `delivery_snapshot_event`,
并且完成了;
已准备的 Workspace事件行和 WebSocket 交付未创建.

## 性,关键和比赛

修改的确切条件是防止同时更新的草案被删除. FK Cascades 也在删除所有者/流/主题时删除了草案,而没有公开事件.

## 客户端可见时刻

发起客户端立即看到固定的草稿.其他客户端只会在重新启动或明显再次请求草稿后看到;最终没有一致的更新.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)
