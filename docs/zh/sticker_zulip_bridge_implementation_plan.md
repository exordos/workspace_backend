# Sticker 与 Zulip 的互操作性

## 目标与不变量

Workspace 消息保留规范的 Markdown 表示
`![sticker](urn:sticker:<uuid>)`。Zulip 通过现有上传流程接收原始媒体字节，
并使用带有 `workspace-sticker:v1:<uuid>` 标签的普通附件链接。经过验证的
带标记附件会在提交 Provider API 事件前恢复为规范的 sticker 引用。

这不会把新的 sticker 导入目录。普通图片仍然是普通图片，即使其字节与
现有 sticker 相同。

## 已确认的实现边界

- Backend：`workspace/external_bridge_control/files.py`、
  `file_repository.py`、`service.py`，以及
  `workspace/cmd/external_bridge_api.py` 中的 runtime 接线。
- Bridge 仓库：`exordos/workspace_zulip_bridge`。
- Bridge 出站转换：`zulip_adapter.py`、
  `_convert_workspace_content`；目前它调用 `export_file`，使用 Zulip 客户端
  上传，并通过 `link.with_destination` 修改目标。原始图片语法标志会被保留。
- Bridge 入站转换：`converter.py` 调用 `service.py` 中的文件解析器；
  `file_api.py` 负责经过认证的文件 API 请求。
- Provider ingress 已经接受规范的 sticker Markdown。不能把 sticker 加入
  普通的 WorkspaceFile 跨项目重投影：目录引用是全局的，不需要为每个聊天
  创建文件记录。

## 线路契约

### 出站字节

扩展现有的 `PUT /v1/file-transfers/outgoing/{transfer_uuid}`，使其接受
`urn:sticker:<uuid>`。保留现有的分配授权、幂等性、下载传输和响应封装。
对于 sticker，`file_uuid` 是目录 UUID，`name` 是 `<uuid>.<format>`，而大小、
哈希和类型描述目录中的原始媒体。使用当前 request session，只解析 active
且未 blocked 的 sticker。Sticker URN 必须精确匹配，且不得包含 query suffix。
保留所有普通附件 ACL 检查。

不需要合成 WorkspaceFile、额外的上传 endpoint、公开 bucket 访问、新数据库
表或独立的 request transaction。只有对于 sticker URN，目录查询会替代文件
sidecar 查询。复用现有的单对象临时授权。签发新授权时重新检查可见性；
已经签发的授权保留原有的过期语义。

### 入站目录验证

在同一个经过认证的 bridge service 下添加只读 private endpoint：

`GET /v1/stickers/{uuid}?external_account_uuid=<uuid>&external_chat_uuid=<uuid>`

必需的 account/chat 参数标识当前已获授权的分配。历史同步不得伪造任何
出站操作或 transfer。

`200` 响应只包含 `uuid`、`sha256`、`size_bytes` 和 `content_type`。
其中绝不包含 storage key、credentials 或 download URL。不可用或未知的目录
条目返回 `404`；未授权的分配返回 `403`。无效输入使用现有 private API
验证错误。临时 service 错误仍然属于可使用现有 retry flow 重试的失败。

### Markdown 标记

完整链接标签必须是 `workspace-sticker:v1:<uuid>`。保留现有 bridge 的
`[...]` 与 `![...]` 结构；不要求新的 Zulip Markdown 功能级别。只有已解析的
附件链接可以作为元数据；普通文本、code span 和 fenced code 都不是元数据。

该标记表示附件类型，而不是消息作者或操作。它是公开信息，不是授权凭证。
上传文件的物理文件名不必包含该标记。

## 出站算法

1. 使用现有 converter 解析 Markdown，并识别 sticker URN。
2. 通过扩展后的 file API 对原始对象进行授权和下载。
3. 复用现有的 provider 上传以及 operation/retry 机制。
4. 将附件标签设置为 versioned marker，将目标设置为 Zulip 上传响应返回的
   路径。其他消息元素保持不变。
5. 保持原始 Workspace payload 不变。

## 入站算法

1. 通过现有的 live/history conversion path 解析原始 Zulip Markdown。
2. 对于带有受支持标记的上传链接，读取当前 scoped catalog metadata，并根据
   SHA-256 和大小比较下载的原始字节。绝不对 thumbnail 进行哈希；仅凭
   provider MIME label 不能证明身份。
3. 验证匹配后，返回 `![sticker](urn:sticker:<uuid>)`，不进行普通的入站文件
   分配，也不创建 WorkspaceFile 记录。
4. 如果没有标记、版本未知、标记格式错误或已确认不匹配，则继续使用下载的
   字节执行现有的普通图片导入。对于 UUID 不可用的受支持有效标记，显示通用
   `Sticker unavailable` 占位符，并且不导入普通图片；deleted、missing 和
   blocked 状态有意保持不可区分。
5. 不要把授权错误或临时网络错误转换成永久的普通图片判断；保留现有的
   error/retry 行为。

将恢复后的标签规范化为 `sticker`，避免重复同步时在不同标签之间来回切换。
新 Zulip 消息中的带标记副本在验证后可以成为 sticker；它们仍然是新消息，
不是原消息的回声。

## 回声、编辑与历史

在现有的规范内容比较和 Provider event 提交之前规范化附件。继续使用现有的
operation/provider identity 映射进行消息去重；绝不能把标记当作 message ID。

保留真实编辑：文本编辑保留匹配的 sticker；删除会移除该元素；替换为不同
字节或移除标记会使附件变成普通附件。不要忽略所有对 Workspace-origin 消息
的更新。历史、第二个账号的投递、早期事件和重启恢复不能依赖内存中的
outgoing marker cache。还要检查歧义发送的 reconciliation 以及普通 live events
是否使用同一套规范化逻辑。

## 组件职责与依赖

1. **Backend 媒体与元数据：** scoped resolvers 和 private endpoints 负责授权、
   目录可见性、媒体访问，以及 bridge 使用的稳定响应 schema。
2. **Bridge 转换：** file client 和 outgoing/incoming converters 负责标记处理、
   provider 上传、字节验证、历史与 reconciliation，同时保持普通附件行为。
3. **Provider 事件处理：** 更新和跨项目重投影保留规范 sticker URN，无需普通
   WorkspaceFile 查询。
4. **契约验证：** backend 和 bridge 测试覆盖共享 private API schema、授权、
   PostgreSQL 行为、转换、retry paths 和 storage adapters。手动 Zulip 验收仍是
   独立证据，因为 unit fake 和 S3 presign mock 不能证明客户端显示或完整的
   provider roundtrip。

## 验收矩阵

| 场景 | 预期结果 |
| --- | --- |
| 从 Workspace 发送 sticker | Zulip 媒体；Workspace sticker URN 保留 |
| 自己的 echo、重复或早期事件 | 没有重复；同一个规范 sticker |
| 没有 send cache 的第二个账号或历史 | 已验证的 sticker 被恢复 |
| 没有标记但字节相同 | 普通图片 |
| 标记对应不同的原始字节 | 普通图片 |
| 未知版本或格式错误的标记 | 普通图片 |
| 受支持标记对应不可用 UUID | 通用 `Sticker unavailable` 占位符；不导入普通图片 |
| 分配被拒绝 | 授权错误；不泄露目录元数据 |
| 临时查询或下载错误 | 现有的错误和 retry 行为 |
| 混合文本、图片和多个 sticker | 顺序和元素类型保留 |
| code 中包含标记语法 | 字面代码不变 |
| 文本编辑、附件替换或删除 | 真实编辑被反映 |
| sticker 在新 grant 前变为 blocked | 新 grant 被拒绝 |
| 跨项目移动消息 | 全局 sticker 引用不变 |

## 部署与验证

自动化测试、真实 PostgreSQL 检查、mock provider/storage 检查以及手动 Zulip
验收提供不同证据，需要分别记录。Zulip 客户端中标记的可见性必须实际测量，
不能假定它会被隐藏。

先部署 backend，再部署兼容的 bridge。在 bridge 部署完成前保持 sticker 发送
禁用。只有在 backend DELETE API 部署完成后，才启用 UI delete 操作。

发布验收包括真实 Zulip 显示、网络 retry 行为和完整的 mTLS/S3/provider
roundtrip。自动化或 mock 检查不能替代这些集成检查。
