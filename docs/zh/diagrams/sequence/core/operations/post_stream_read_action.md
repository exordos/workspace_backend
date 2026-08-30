# POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)

## 现有合同的地位和边界

目标规范是docs-first. 方法,路径,公开JSON和授权遵循当前的合同 [`workspace_api.md`](../../../../workspace_api.md); bounded pagination 并且异步可视性是单独的 target compatibility ADR.

![序列图](diagrams/post_stream_read_action.svg)

可编辑的源: [`post_stream_read_action.puml`](diagrams/post_stream_read_action.puml).

## 操作

**方法和方式:** `POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke`

**目的:** 标记流消息为当前用户阅读.

## 公开查询

没有身体 JSON.

## 成功的公众回应

HTTP `200`:

```json
{
  "uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "Инженерия",
  "description": "Инженерное пространство",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "owner": "11111111-1111-1111-1111-111111111111",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "role": "owner",
  "notification_mode": "all_messages",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false,
  "private": false,
  "is_archived": false,
  "color": 3368601,
  "last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "default_topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "created_at": "2026-06-22T09:00:00Z",
  "updated_at": "2026-06-22T10:16:00Z"
}
```

## 公众错误

需要 bearer-token IAM 和项目域. 错误的 UUID 或请求体返回 HTTP `400`;该区域缺少或无法访问的资源 `404`. 标准的文档化验证错误体:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## 目标边界 RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUserStream(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_streams_v1"

    owner = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    direct_user_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True,
    )
    last_message_uuid = properties.property(types.UUID(), read_only=True)
    default_topic_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceStreamController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserStream, convert_underscore=False, process_filters=True,
    )
```

公开的实体引用是以 `types.UUID()` 标量属性,而不是以 RestAlchemy 关系为序列,它们是以 URI 序列的.物理列 `*_uuid` 仍然是具有明显选择的引用完整性操作的索引外部键.公开字段 `owner` 是 UUID 属性;物理字段 `owner_uuid`  是用户的索引外部键. USER_STREAM_BINDING 存储了已备用的流量级计数器..

## 同步交易

1. 允许区域.
2. 设置适用的读取标记 USER_MESSAGE_STATE.
3. 添加一个独立的 immutable 输出箱事件 task
   `user-stream`/`user-topic`/`user-folder`.

影响状态: USER_MESSAGE_STATE 和交易外框; 总体从未存储在消息绑定中.

## 类型化任务和后台执行器

任务:单独的 immutable `read_counters` 任务为 `user-stream` 和
`user-topic`, 并且为 `user-folder` 设置 `folder_projection`;每个
符合其源的Outbox事件,并没有 coalescing.

Fenced owners exact scopes `user-stream`, `user-topic` 其他 `user-folder`
更新已准备的计数器/snapshot; topic worker shared rows 没有写. Atomic delta
要求 exactly-once guard 在 `outbox_event_uuid`,否则执行
recompute/write. Tasks 它们使用 retry/backoff, DLQ/reaper.

## 公共活动和 WebSocket

`stream.read` 装备和装备.

## 具有能力,种族和可见的时间特征

读取重复标记是动力性的;读取状态会立即改变,计数器和事件异步.

[← 文件的主要索引](../../../../index.md) · [序列图表的索引](../../README.md) · [部分 Core Messenger](../README.md)
