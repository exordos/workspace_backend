# `GET /api/workspace/v1/messenger/files/`

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)

状态:在文件中首先开发的操作目标规格. 现行公开合同
没有变化,是 [`workspace_api.md`](../../../../workspace_api.md).
这个文件描述了交易和投影的目标边界;它不是
产品代码,迁移 SQL 或新终点.

![序列图](diagrams/get_files_list.svg)

[可编辑的源 PlantUML](diagrams/get_files_list.puml)

## 任命和公开合同

列出可通过公共ACL或流中当前会员查看的文件元数据.

认证:持有者IAM;`project_id`和当前`user_uuid`代币从文本中取出 IAM.

## 查询路径和参数

| 位置 | 姓名 | 类型 / 规则 |
| --- | --- | --- |
| 查询 | `page_limit` | current: 没有/`0`意味着无限; target:没有/`0` => `100`, `1..500`确切,负/不目标/`>500` => `400`没有 clamp |
| 查询 | `page_marker` | UUID 上一页的最后资源 |

集合页面,如果它是,将保留当前的合同 `page_limit` 和 UUID
`page_marker` 返回 `X-Pagination-Limit`,以及
`X-Pagination-Marker` 只要下一个页面.

Target default — `100`, hard maximum — `500`; `0` 也表示`100`,unbounded mode不存在.参数名称和公共JSON形式不变;全输出客户端读到下一个不存在 marker.

## 查询的本体

查询的本体不存在.

## 成功的答案

`200`

```json
[
  {
    "uuid": "f11353e0-712d-4b99-a716-5cdba848cc05",
    "user_uuid": "11111111-1111-1111-1111-111111111111",
    "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
    "name": "example.txt",
    "description": "Example",
    "content_type": "text/plain",
    "size_bytes": 12,
    "hash": "abc",
    "created_at": "2026-07-17T08:00:00Z",
    "updated_at": "2026-07-17T08:00:00Z"
  }
]
```



## 错误和授权

不正的过器返回HTTP`400`;无法访问的单个资源返回未找到.错误IAM通过了通用身份验证错误界限 Workspace.

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

每个对实体的公共引用都被声明为 RestAlchemy 的标数 UUID 属性,而不是 `relationship` (它将被串行为 URI).相应的物理列 `*_uuid` 一个具有明显选择的引用操作的索引外部密钥.因此,公共 JSON 保持UUID 不变.

现行元数据/存储器/ACL合约保留;目标物理分解不选. `project_id`保持隐藏. 尺度`user_uuid`和允许`null` `stream_uuid`仍然是公开UUID值,支持FK索引. 动态访问通过流成员身份通过流的正规绑定进行检查.

## 同步路径 API

1. 验证用户.
2. 执行索引检查 ACL/会员.
3. 阅读可见的元数据行;`project_id`和隐藏的存储细节.
4. 串行列表.

## Outbox, 典型任务,工作和实时工作

这种读取不会记录域事件或Outbox记录,不会创建典型的投影任务,也不会发布公开事件.基于DB的资源是通过无计算的索引读取的.所有计数器都已经实现;请求不执行`COUNT`,`GROUP BY`,相关子查询,也不扫描消息绑定.

管理员 WebSocket 没有参与.

## 性,关键和比赛

操作是安全的,因为它不会改变状态. 在DB交易期间,资源和过域的相同性是稳定的.

## 客户端可见时刻

客户端获得读取交易执行时可用的固定的状态;请求没有计划新的延期工作.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)
