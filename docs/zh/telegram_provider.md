# Telegram 外部账户

此提供方通过 MTProto 使用 Telegram 用户账户。请在服务器上配置客户端应用程序，然后通过外部账户会话使用二维码进行身份验证，如果已启用，还需提供 Telegram 两步验证密码。仅凭应用程序凭据无法授权用户账户。

## 配置

独立的 `workspace-telegram` 进程读取与 API 相同的配置文件。未设置的应用程序 ID 会禁用该提供方。平台会生成一个单独的加密密钥；请将其与数据库备份一起保留。如果没有匹配的密钥，现有的加密会话将无法恢复。

```ini
[telegram]
api_id = 0
api_hash =
secret_encryption_key =
poll_interval = 1
```

将应用程序凭据放在由操作员管理的 `/etc/workspace/telegram.conf` 覆盖配置（所有者为 `root:workspace`，权限为 `0640`，仅 root 可写，API 用户组可读）中。API 和守护进程在读取生成的主配置后会读取此覆盖配置。将此覆盖配置保留在针对后端节点的单独平台配置资源中，其应用程序哈希由平台密钥引用提供；元素升级不得拥有或重置此凭据。使用平台密钥存储来管理凭据。切勿将账户会话、登录令牌、密码或应用程序哈希放在公开的外部账户设置、消息、日志或客户端缓存中。API 身份验证响应受所有者和项目范围限制，从不缓存，即使在 DEBUG 级别下也从正文日志记录中排除。

## 账户与身份验证契约

使用 `provider=telegram` 创建外部账户。公共设置为空：目录会自动发现所有可访问的会话。本次变更同步新消息；完整历史记录的自动导入由后续依赖变更提供。旧的 `history_depth`、`selection_mode` 和 `peer_keys` 输入会被丢弃。现有外部账户路由和状态仍可供旧客户端使用。 状态响应通过 `history_enabled` 明确报告自动导入是否可用。

所有者使用现有的外部账户更新权限调用以下操作：

```text
POST /v1/messenger/external_accounts/{uuid}/actions/telegram_auth/invoke
{"operation":"start"}
{"operation":"status"}
{"operation":"password","generation":"attempt-uuid","password":"example-only"}
{"operation":"cancel","generation":"attempt-uuid"}
```

登录尝试在五分钟后过期。QR 令牌在该尝试期间内刷新。关闭表单仅取消当前生成；较旧的表单无法取消较新的尝试。密码经过加密，仅消费一次，并随尝试过期。守护进程会撤销在取消后完成的授权。删除账户会禁用同步，分离所有者的成员资格，并在删除根账户及其依赖状态之前请求 Telegram 登出。

## 同步与身份

每个账户使用一个连接和一个 PostgreSQL 咨询锁。私聊及群组差异更新、频道协调、媒体重处理、目录刷新和出站投递分别运行。持久化的 pts/qts 游标及收件队列支持重启后继续处理实时更新。目录每 60 秒刷新；发现未知私聊或群组时立即唤醒目录任务。联系人、会话对端和消息作者在 Workspace 目录中获得稳定的 Telegram 用户身份。已验证的 IAM 资料仍然具有权威性。

私聊和基本群组消息使用账户范围的身份。频道和超级群组消息身份在项目内和提供方内共享。Telegram 流保持成员资格限制的 Workspace 访问权限。个人文件夹包含直接会话；Telegram 频道和基本群组独立于此访问标志出现在 Channels 中。Telegram 流是私有 Workspace 流；另一个用户的源账户必须独立导入相同的频道，该用户才能获得成员资格。普通聊天使用默认主题 `telegram`；论坛回复保留其源主题。没有源主题的 Zulip 对话使用 `zulip`。Web 客户端仅按确切的对端 Workspace UUID、项目和查看者对可见的未加密直接流进行分组。每个提供方主题保留其物理流 UUID 用于投递和授权；原生主题使用现有的规范原生直接流。名称、电子邮件和用户名从不合并身份。周期性目录协调重用相同的流写入器，在验证账户链接后更新现有的直接对端；它保留物理对话，并不向新链接的用户授予成员资格。

表情回应回声复用由消息、用户及表情组成的规范键，并保留已有的 Workspace 回应 UUID。重复更新和后续移除不会创建第二条事实记录。持久收件队列防止源快照覆盖较新的更新。出站消息在远程写入前保存随机标识符，并使用权威回执。较早到达的自身消息回声会等待回执绑定原始 Workspace UUID。不确定的重复标识符仍是显式错误，不能通过消息文本关联。

文本、源时间戳、回复、单个照片/文档、编辑、删除、普通表情符号反应以及所有者的读取确认使用规范 Messenger 实体。只读源聊天拒绝发送。秘密聊天、通话、故事、支付、参与者管理、自定义表情符号和多附件出站相册不在当前实现范围内。Telegram 可能会施加额外的消息、文件、反应或速率限制；限流等待保留持久检查点和重试截止时间。

## 消息转换示例

[打开离线 Telegram ↔ Workspace 比较](../telegram_messages_preview.html)。其合成 Telegram 视图展示了 MTProto 实体，而不是重现 Telegram 客户端。Workspace 使用其实际的渲染器和主题。相同的 JSON 示例在单元测试中针对生产转换进行断言；生成器还检查渲染的 DOM 语义。

使用本地 Workspace UI 和 Zulip 源码目录运行 `node workspace/tests/fixtures/render_zulip_messages_preview.mjs --telegram`。`WORKSPACE_UI_SOURCE_DIR` 和 `ZULIP_SOURCE_DIR` 覆盖其路径。通用生成器保留现有的 Zulip 页面。

Telegram 实体变为转义的 CommonMark：粗体、斜体、删除线、剧透、代码、语言感知块、带标签的链接和块引用。UTF-16 偏移量保留非 BMP 文本。出站 Workspace CommonMark 被解析为 MTProto 文本/实体，而不是 Telethon 不兼容的遗留 Markdown。下划线目前保留纯文本，因为 Workspace 没有下划线语法；此限制在比较页面上可见。

当目标属于同一源会话时，Workspace 引用/消息引用变为原生 Telegram 回复标题。附件和回复引用被解析为 CommonMark 链接；代码内的字面示例保留为文本。不支持的仅 Workspace 链接保留其可读标签，而不是无效的 Telegram URL。

## 实时隔离与发布权限

私聊及群组差异更新、频道协调、持久媒体重处理、目录和出站投递分别作为账户任务运行。缓慢的频道 RPC 或文件下载不会阻塞私聊接收。原始频道更新会提高该频道的补齐优先级。媒体标识符在游标推进前写入持久收件队列，因此处理能够跨重启恢复。授权撤销会停止账户；普通目录和接收错误保留各自的重试期限。诊断仅显示错误类型和期限，不包含提供方异常文本。完整历史导入及使用权威历史扫描修复缺口的功能将单独添加。

公共流能力 `can_send` 根据成员资格、发布策略、源权限和连接的账户按查看者计算。当发送不可用时，API 写入在创建规范消息或出站意图之前被拒绝。原生 `announcement_only` 频道允许所有者、管理员和版主发布；只有所有者/管理员管理此策略和提升角色。现有的 `announce` 创建通知标志具有单独的语义。仅公告创建目前可用于未加密的原生频道。Telegram 广播反映订阅者的实际发布权利。

导入的 Telegram 身份重用外部账户记录，无需授权会话，就像 Zulip 导入一样。后来的验证登录可以声明导入的身份，但不能获取已连接到另一个 Workspace 用户的身份。提及仅通过选定主题的提供者和项目解析，包括账户链接后的稳定导入 UUID 引用。名称和电子邮件地址不是身份匹配。

## Telegram 通知模式

流模式双向同步。`all_messages` 显式取消静音 Telegram 聊天。`muted` 和 `mentions_only` 都使用 Telegram 的长静音；Workspace 保留其不同的本地通知行为。相同静音值的回声保留本地模式。后来的有效源更改映射到 `muted` 或 `all_messages`。Telegram 没有与每个 Workspace 模式完全对应的等效项。主题 `default`、`follow`、`unmute` 和 `mute` 仍然是 Workspace 主题偏好，不会重写整个 Telegram 聊天。

初始会话设置以及 Telegram 的用户/群组/广播默认值随继承一起导入。无 Pts 的通知更新单独消费。待处理的本地意图和观察时间戳防止过时的源快照覆盖较新的本地偏好。出站更改仅设置 `mute_until`，保留声音和预览设置。验收更改必须快照并恢复目标源偏好。

如果频道在完整加载的目录中消失、被标记为已退出或被移除，或返回 `ChannelPrivateError`，则只撤销该外部账户所有者的成员资格。其他已连接成员保留访问权限，后续消息不会再授予被移除的所有者。
