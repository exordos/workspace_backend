# `DELETE /api/workspace/v1/messenger/files/{file_uuid}`


总目标可靠性变量:每个 immutable outbox 事件都会产生一个唯一的 immutable typed task `outbox_event_uuid`;并无 coalescing. Task 存储实际的 exact scope key,使用 lease/fencing, retry/backoff, max attempts/DLQ, reaper 和 idempotent effect guard. Topic scope 仅适用于 placement/message-binding work; shared rows 不会在 topic.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)

状态:在文件中首先开发的操作目标规格. 现行公开合同
没有变化,是 [`workspace_api.md`](../../../../workspace_api.md).
这个文件描述了交易和投影的目标边界;它不是
产品代码,迁移 SQL 或新终点.

![序列图](diagrams/delete_file.svg)

[可编辑的源 PlantUML](diagrams/delete_file.puml)

## 任命和公开合同

删除所有者文件并撤回其字节访问权限.

认证:持有者IAM;`project_id`和当前`user_uuid`代币从文本中取出 IAM.

## 查询路径和参数

| 位置 | 姓名 | 类型 / 规则 |
| --- | --- | --- |
| 路径 | `file_uuid` | UUID |

集合页面,如果它是,将保留当前的合同 `page_limit` 和 UUID
`page_marker` 返回 `X-Pagination-Limit`,以及
`X-Pagination-Marker` 只要下一个页面.

## 查询的本体

查询的本体不存在.

## 成功的答案

`204` 答案的空体.



## 错误和授权

只有所有者才能删除. 其他所有者的不可访问 UUID 或 UUID 没有被披露. 清理存储器错误发生在可规删除后,并不会恢复公共访问.

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


class WorkspaceFile(models.ModelWithUUID, models.ModelWithProject,
                    models.ModelWithTimestamp, orm.SQLStorableMixin):
    # Contract boundary only; target storage decomposition is not selected.
    __tablename__ = "m_workspace_files"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    stream_uuid = properties.property(types.AllowNone(types.UUID()))
    name = properties.property(types.String(max_length=255), required=True)
    description = properties.property(types.String(max_length=255), default="")
    content_type = properties.property(types.String(max_length=255), required=True)
    size_bytes = properties.property(types.Integer(min_value=0), required=True)
    hash = properties.property(types.String(max_length=255), required=True)


class WorkspaceFileController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceFile,
        hidden_fields=["project_id"],
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow multipart/storage/download overrides preserve the current contract.
```

每个公开引用的实体都被声明为 RestAlchemy 的标数UUID 属性,而不是 `relationship` (它会被串行为 URI).相应的物理列 `*_uuid` 一个具有明显选择的引用操作的索引外部密钥.因此公开JSON 保持UUID 没有变化.

现行元数据/存储器/ACL合约保留;目标物理分解不选. `project_id`保持隐藏. 尺度`user_uuid`和允许`null` `stream_uuid`仍然是公开UUID值,支持FK索引. 动态访问通过流成员身份通过流的正规绑定进行检查.

## 同步路径 API

1. 查找和封锁所有文件元数据.
2. 删除文件/ACL的正则行并添加不可变的条目`file.deleted` outbox.
3. 记录交易并恢复 `204`.
4. 交易完成后,删除已无链接的二进制数据和相关元数据.

## Outbox, 典型任务,工作和实时工作

已准备的删除事件是异步创建的. 在删除元数据时,公众访问会消失,直到完成对象的清理..

固定的域名将创建一个单独的文件 immutable
`delivery_snapshot_event` 文件的scopes和唯一的
`outbox_event_uuid`. 工作者可以写出已完成的 `file.deleted`,
管理员发送,重复或播放.

## 性,关键和比赛

UUID 定义一个定制的元数据记录.删除和更新将按这个行进行序列化;后来的操作将看到删除. 清除存储器允许重复,并且必须考虑引用.

## 客户端可见时刻

发起客户端立即获得固定元数据.其他客户端在投影延迟后获得文件的已完成事件. 固定的删除后清理存储器可能在稍后完成,而不会恢复访问元数据.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)
