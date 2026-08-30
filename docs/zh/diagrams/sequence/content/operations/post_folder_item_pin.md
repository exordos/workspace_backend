# `POST /api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke`

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)

状态:在文件中首先开发的操作目标规格. 现行公开合同
没有变化,是 [`workspace_api.md`](../../../../workspace_api.md).
这个文件描述了交易和投影的目标边界;它不是
产品代码,迁移 SQL 或新终点.

![序列图](diagrams/post_folder_item_pin.svg)

[可编辑的源 PlantUML](diagrams/post_folder_item_pin.puml)

## 任命和公开合同

添加当前用户文件元素.

认证:持有者IAM;`project_id`和当前`user_uuid`代币从文本中取出 IAM.

## 查询路径和参数

| 位置 | 姓名 | 类型 / 规则 |
| --- | --- | --- |
| 路径 | `folder_item_uuid` | UUID |

集合页面,如果它是,将保留当前的合同 `page_limit` 和 UUID
`page_marker` 返回 `X-Pagination-Limit`,以及
`X-Pagination-Marker` 只要下一个页面.

## 查询的本体

查询的本体不存在.

## 成功的答案

`200`

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
  "unread_count": 3,
  "active_unread_count": 3,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```



## 错误和授权

不正确或未授权的输入数据由RESTAlchemy/IAM的错误界面处理; 给定的区域中的资源不会在用户/项目之外被披露.

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


class WorkspaceFolderItem(models.ModelWithUUID, models.ModelWithProject,
                          models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "messenger_folder_items"
    user_uuid = properties.property(types.UUID(), required=True, read_only=True)
    folder_uuid = properties.property(types.UUID(), required=True)
    stream_uuid = properties.property(types.UUID(), required=True)
    chat_type = properties.property(types.Enum(["stream", "group", "private"]), required=True)
    order_index = properties.property(types.AllowNone(types.Integer()))
    pinned_at = properties.property(types.AllowNone(types.UTCDateTimeZ()), read_only=True)


class WorkspaceUserFolderItem(models.ModelWithTimestamp, orm.SQLStorableMixin):
    __tablename__ = "messenger_api_user_folder_items_v1"
    uuid = properties.property(types.UUID(), id_property=True, read_only=True)
    project_id = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    folder_uuid = properties.property(types.UUID(), read_only=True)
    stream_uuid = properties.property(types.UUID(), read_only=True)
    chat_type = properties.property(
        types.Enum(["stream", "group", "private"]), read_only=True,
    )
    order_index = properties.property(types.AllowNone(types.Integer()))
    pinned_at = properties.property(types.AllowNone(types.UTCDateTimeZ()), read_only=True)
    # Ready fields are joined from unique USER_STREAM_BINDING. They are not
    # stored on WorkspaceFolderItem and are never calculated on API reads.
    unread_count = properties.property(types.Integer(min_value=0), read_only=True)
    active_unread_count = properties.property(types.Integer(min_value=0), read_only=True)
    passive_unread_count = properties.property(types.Integer(min_value=0), read_only=True)


class FolderItemController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceUserFolderItem,
        convert_underscore=False,
        process_filters=True,
    )
    # Writes use WorkspaceFolderItem; reads use the calculation-free view.
```

每个对实体的公共引用都被声明为 RestAlchemy 的标数 UUID 属性,而不是 `relationship` (它将被串行为 URI).相应的物理列 `*_uuid` 一个具有明显选择的引用操作的索引外部密钥.因此,公共 JSON 保持UUID 不变.

物理元素具有索引FK`folder_uuid`, `stream_uuid`没有`user_uuid`没有`ON DELETE CASCADE`他们的公开.UUID单独的链接是唯一的链接.`USER_STREAM_BINDING`通过`(project_id,user_uuid,stream_uuid)`它们从来没有被保存在消息链接中,也没有被计算在这个查询中..

固件只改变元素的个人位置层 (placement),不改变规则
系统文件或自动会员. `FOLDER_ITEM`
仍然是从活跃的恢复投影的维克支持
`USER_STREAM_BINDING` 和正规的 `STREAM` `is_archived = false`:
`All chats` 包含所有可用的流, `Personal`  只有流
`STREAM.private = true`, `Channels` — 只有`STREAM.private = false`这就是她.
阅读表示使用简单的索引连接 `COUNT`
在查询时.

## 同步路径 API

1. 在指定区域中查找和锁定文件元素.
2. 设置 `pinned_at` 于当前时间 UTC.
3. 添加一个不变的记录到Outbox `folder_item.pin`.
4. 记录交易并返回更新的元素.

## Outbox, 典型任务,工作和实时工作

操作只会同步改变元素的状态. 亲文件事件投影使用已备用的容器绑定计数器.

Outbox event 输出 immutable`folder_projection`没有结合和 exact scope
`user-folder:(project_id,user_uuid,folder_uuid)`. 房租租的业主读
normalized item 并且在一个 worker DB transaction
取代了决定性 `folder_items_snapshot`,计数器,版本/updated_at 和
ready `folder.updated`. 管理员只在 commit;
retry/backoff, DLQ/reaper 效果保护是必需的.

## 性,关键和比赛

操作重复归结为相同的固定/分离状态;重复固定可以根据当前操作语义更新`pinned_at`. 锁定行防止断裂的时间标签更新.

## 客户端可见时刻

REST 答案即时包含新的 `pinned_at`; 插入的仅读快照文件,计数器和 WebSocket 事件可能会滞后到完成 `folder_projection`.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [内容和用户分区 Workspace](../README.md)
