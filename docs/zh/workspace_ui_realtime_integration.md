# Workspace实时集成UI

这份文件描述了公众 Messenger实时合同
WorkspaceUI. REST追赶和websocket交付使用相同的平面事件
目标和相同的IAM范围的可视性规则.

## 终点

- `GET /api/workspace/v1/events/`
- `GET /api/workspace/v1/epoch/`
- `WS /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`

内部网接口服务路径为`/v1/events/ws`在`127.0.0.1:21082`.
浏览器代码必须使用上面的nginx路径.
或是外部集成的websocket终端.

| 交通 | 认证 | 订购 | 预期用途 |
| --- | --- | --- | --- |
| `GET /events/` | IAM 持有人头 | 升级式 `epoch_version` | 开始负载,重新连接,赶上,补救差距 |
| `GET /epoch/` | IAM 持有人头 | 最新的指针 | 与可见服务器时代进行本地进度比较 |
| `WS /events/ws` | IAM 在子协议中的代币 | 错过的行,然后活跃的行 | 低延迟交付后赶上 |

## 认证

REST请求使用IAM持有人令牌:

```http
Authorization: Bearer <accessToken>
```

网络插件客户端发送的正是这两个.`Sec-WebSocket-Protocol`值:

```ts
["workspace.events.v1", `bearer.${accessToken}`];
```

服务器选择`workspace.events.v1`.不要在查询中放标志
坚持,并发送 `epoch_generation` 每个非零复习指针;
省略 `last_epoch_version` 表示冷光标 `0`.
需要一个代,但它返回正常的类型间隙响应,当
设置保留后,默认情况下72小时,无法提供完整的
历史从时代`1`.
不经授权的握手结束时 `4401` 和无效的握手结束时
`4400`代币更新需要新的连接.

## 事件形状

每个 REST 事件和 websocket 事件消息都是相同的 `schema_version: 1`
没有外在的物体.`{ "type": "event", "event": ... }`包装.
接口还发送输入`ready`和导航器错误控制消息.

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
    "payload": {
      "kind": "markdown",
      "content": "Hello"
    },
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "reactions": {},
    "reaction_users": {},
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

最顶级字段描述事件行.资源标识符位于
`payload`没有`payload.kind`是唯一的`kind`事件有效载荷的场.
局部持久性表示是一个内部细节,
现在的情况是这样的:

创建,更新,阅读和操作事件携带相同的全对象快照
应对的 Messenger REST 响应,加上 `payload.kind`.
事件最小:

- `stream.deleted`, `folder.deleted`没有`folder_item.deleted`: `kind`, `uuid`;
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`;
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`没有`source`.

反应变化发出`message_reaction.created`,
`message_reaction.updated`,或者 `message_reaction.deleted` 作为代理用户.
后端还发出`message.updated`快照,更新了总体
`reactions` 图片和持续的边界`reaction_users` 图片,可以看到用户
每个现有的 `reaction_users` 键是一个完整的用户 UUID 列表
客户端将整个地图替换到
每个完整的消息快照;一个空的对象或缺少的键意味着仅计数
并且必须删除任何以前缓存的列表.

批量流绑定创建使用`payload.items`. 读取操作发射
`message.read`, `topic.read`没有`stream.read`并且继续排放
`topic.updated`,`stream.updated`,和`folder.updated`事件未读时
电脑表变更.批量消息读取使用`messages.read`
`message_uuids`.

支持的值是:

| `object_type` | 行动 |
| --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` |
| `message_reaction` | `created`, `updated`, `deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` |
| `stream_binding` | `created`, `updated`, `deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` |
| `user` | `updated` |
| `folder` | `created`, `updated`, `deleted` |
| `folder_item` | `deleted` |
| `file` | `created`, `updated`, `deleted` |

## 追赶和指针处理

要求事件严格地比最近成功应用的时代更新:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<generation>&page_limit=500
```

`epoch_version>`是严格的. 进程事件在上升顺序,并坚持
只有在对每个受影响的客户端商店应用事件后,

`GET /api/workspace/v1/epoch/`返回代,当前时代,以及
保持地板可见到电流.IAM用户和项目:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

导航符规则:

- 处理 `(epoch_generation, epoch_version)` 作为一个不可分割的光标,并发送
  生成每一个非零的 REST 或websocket复用;
- 忽略时代小于或等于被绑定的指针的事件;
- 修复一个缺口,以追赶 REST,而不是猜测资源状态;
- 当 IAM 用户或项目更改时,将分区或清除光标;
- 永远不要按事件 UUID 或时间排序;
- 页面化直到服务器返回没有更多的页面标记.
- 处理 HTTP 410 `EventsCursorExpiredError` / `error=epoch_pruned` 作为缓存
  恢复边界;更新权威快照,并重新启动返回
  服务器保留事件的配置间隔,72小时
  默认设置,并且此重置永远不会删除消息,文件或域状态.

## 网络插件交付

接入后,服务器会发送比最近错过的事件
保存光标,然后发送
`{"type":"ready","epoch_generation":"...","epoch_version":124}`没有活着
保持用户通知门关闭直到
`ready`;追赶消息必须更新状态,而无需通知.
网络插件的ping控制,不是应用程序 JSON `hello`, `ping`, `pong`,或
`ack`标签到期发送输入错误的体和关闭`4410`.

建议的客户流量:

1. 加载提交的光标对,或者使用冷时代 `0` 没有代.
2. 运行 REST 追赶,直到没有更多事件返回.
3. 用最新的光标对打开网插件.
4. 通过相同的idempotent调度器应用REST和websocket消息.
5. 保持通知禁用直到websocket `ready`框,然后启用
   实时通知.
6. 通过 `(epoch_generation, epoch_version)` 进行除重.
7. 关闭后,再次赶上并重新连接到背面.

网络接口错过事件阶段关闭了最后一个比赛.REST页面和
复制仍然是强制性的,因为模糊的失败可能
重复已经应用的事件.

## 发送 UI

首先通过顶级 `object_type` 和 `action` 发送,然后通过 `payload.kind`
当需要更具体的操作时. 不知方案版本或事件
值应该被记录并跳过,而不会打破实时循环.

| `object_type` | 主要UI存储或效果 |
| --- | --- |
| `message`, `message_reaction` | 时间表,反应,未读状态 |
| `stream`, `stream_binding`, `topic` | 导航,会员,主题状态 |
| `folder`, `folder_item` | 文件导航和章 |
| `user` | 共同身份和存在缓存 |
| `file` | 文件元数据和保护/public 斑点缓存无效 |

`stream.deleted` 对于被删除的参与者,撤销整个流:驱逐
所有已缓存的元数据包含 `stream_uuid` 的保护区块.
剩余成员获得`stream_binding.deleted`;有约束力的变更产生
`stream_binding.updated`.一个导航器间隙错误清除所有衍生保护点
在快照重新加载之前,

V1消息内容使用了下降有效载荷的形式:

```json
{ "kind": "markdown", "content": "Hello" }
```

通过REST更新了存在
`users/{uuid}/actions/presence/invoke` 操作. 工人标记了过时的用户
在线并发出`user.updated` 带有全公开用户快照.
