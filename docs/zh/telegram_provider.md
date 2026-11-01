# Telegram external accounts

This provider uses a Telegram user account through MTProto. Configure the client application on the server, then authenticate from the external-account dialog with a QR code and, if enabled, the Telegram two-step verification password. Application credentials alone do not authorize a user account.

## Configuration

The independent `workspace-telegram` process reads the same configuration file as the API. An unset application ID disables the provider. The platform generates a separate encryption key; preserve it with the database backup. Existing encrypted sessions cannot be restored without the matching key.

```ini
[telegram]
api_id = 0
api_hash =
secret_encryption_key =
history_batch_size = 100
poll_interval = 1
```

Place application credentials in the operator-managed root-only `/etc/workspace/telegram.conf` override. The API and daemon read this after the generated main configuration. Keep the override in a separate platform configuration resource targeting the backend node, with its application hash supplied by a platform secret reference; element upgrades must not own or reset this credential. Use the platform secret store for credentials. Never place account sessions, login tokens, passwords, or application hashes in public external-account settings, messages, logs, or client caches. API authentication responses are owner and project scoped, never cached, and excluded from body logging even at DEBUG level.

## Account and authentication contract

Create an external account with `provider=telegram`. Public settings are empty: all accessible dialogs and their complete history are imported automatically. Legacy `history_depth`, `selection_mode`, and `peer_keys` inputs are discarded. The provider identity is the fixed Telegram hostname. The existing external-account route and status values remain available to older clients.

The owner invokes the following action using the existing external-account update permission:

```text
POST /v1/messenger/external_accounts/{uuid}/actions/telegram_auth/invoke
{"operation":"start"}
{"operation":"status"}
{"operation":"password","generation":"attempt-uuid","password":"example-only"}
{"operation":"cancel","generation":"attempt-uuid"}
```

A login attempt expires after five minutes. The QR token renews within that attempt. Closing the form cancels only its current generation; an older form cannot cancel a newer attempt. Passwords are encrypted, consumed once, and expire with the attempt. The daemon revokes an authorization completed after cancellation. Account deletion disables synchronization, detaches the owner's memberships, and requests Telegram logout before removing the root account and its dependent state.

## Synchronization and identity

Each account has one connection and a PostgreSQL advisory lock. The daemon runs direct/group differences, channel reconciliation, media replay, catalog and outgoing delivery as separate jobs. Private dialogs, basic groups and channels each run the same history loop independently, so a large channel file cannot postpone the first private or group history page. Their GetHistory retry deadline is shared and checked before every peer; account status remains syncing until all selected histories finish. Telegram and Zulip share COPY staging, set-based content/root/placement/binding merging, personal flag reconciliation, and counter scheduling. Fetched Telegram pages are persisted before merging; page removal and checkpoints commit with the canonical batch. Both importers yield to realtime and publication backlog. History does not emit one websocket event per imported message. Service-only pages still advance the checkpoint. History always reads everything Telegram makes accessible to that account. The catalog refreshes every 60 seconds. An unknown direct/group dialog in the common update difference wakes the same catalog job immediately, so its initial history does not wait for the periodic timer; known-dialog messages do not trigger another scan. Contacts, dialog counterparts and message authors become stable Telegram users in the Workspace directory. Verified IAM profiles remain authoritative.

Private dialogs and basic-group messages use account-scoped identities. Channel and supergroup message identities are shared within the project and provider. Telegram streams keep membership-restricted Workspace access. Personal folders contain direct dialogs; Telegram channels and basic groups appear in Channels independently of this access flag. Telegram streams are private Workspace streams; another user's source account must independently import the same channel before that user receives membership. Ordinary chats use the default topic `telegram`; forum replies retain their source topic. Zulip conversations without a source subject use `zulip`. The web client groups visible unencrypted direct streams only by the exact counterpart Workspace UUID, project and viewer. Each provider topic retains its physical stream UUID for delivery and authorization; native topics use the existing canonical native direct stream. Names, emails and usernames never merge identities. Periodic catalog reconciliation reuses the same stream writer to update an existing direct counterpart after a verified account link; it preserves the physical conversation and grants no membership to the newly linked user.

Reaction echoes reuse the canonical message/user/emoji key and preserve an existing Workspace reaction UUID; repeated source updates and subsequent removal do not create a second fact. The durable inbox fences source snapshots against newer updates. A source journal gap triggers an authoritative history rescan; covered page ranges reconcile deleted messages without deleting newer arrivals. Outgoing sends persist the random identifier before the remote write and use authoritative receipts. Early own-message echoes wait in the inbox until their receipt binds the original Workspace UUID. An uncertain duplicate identifier remains an explicit error rather than being correlated by message text.

Text, source timestamps, replies, individual photos/documents, edits, deletes, ordinary emoji reactions, and the owner's read acknowledgements use canonical Messenger entities. Read-only source chats reject sends. Secret Chats, calls, stories, payments, participant administration, custom emoji, and multi-attachment outgoing albums are outside the current implementation. Telegram may impose additional message, file, reaction, or rate limits; flood waits retain the durable checkpoint and retry deadline.

## Measurements and acceptance

The authentication status includes per-chat history start, first committed message, completion, imported message/file/byte counts, history-fetch time, merge time, and preparation time. Throughput is imported messages divided by elapsed wall time; report file volume and provider flood waits separately. These local counters do not prove delivery at Telegram.

Acceptance must record actual source and deployed artifacts, QR authorization, complete accessible chat scope, cold import, replay, restart recovery, realtime latency during backfill, attachment byte equality at both endpoints, and negative access checks. Verify native messaging and fresh Zulip traffic in both directions after the final restart. Keep synthetic/local timings separate from measured live Telegram results.

## Message conversion examples

[Open the offline Telegram ↔ Workspace comparison](../telegram_messages_preview.html). Its synthetic Telegram view illustrates MTProto entities rather than reproducing a Telegram client. Workspace uses its actual renderer and theme. The same JSON examples are asserted against production conversion in unit tests; the generator also checks rendered DOM semantics.

Run `node workspace/tests/fixtures/render_zulip_messages_preview.mjs --telegram` with local Workspace UI and Zulip source checkouts. `WORKSPACE_UI_SOURCE_DIR` and `ZULIP_SOURCE_DIR` override their paths. The common generator retains the existing Zulip page.

Telegram entities become escaped CommonMark: bold, italic, strike, spoilers, code, language-aware blocks, labeled links and block quotes. UTF-16 offsets preserve non-BMP text. Outgoing Workspace CommonMark is parsed into MTProto text/entities rather than Telethon's incompatible legacy Markdown. Underline currently retains plain text because Workspace has no underline syntax; this limitation is visible on the comparison page.

当目标属于同一来源对话时，Workspace 引用和消息链接会转换为 Telegram 原生回复。附件和回复引用按 CommonMark 链接解析；代码中的示例保留为文本。不支持的 Workspace 内部链接保留可读标签，不生成无效的 Telegram URL。

## 实时隔离与发布权限

私聊和群组更新、频道同步、持久化媒体重放、历史、目录和发送分别运行。缓慢的频道请求或下载不会阻塞私聊消息。频道实时更新会提高相应频道的同步优先级。推进游标之前先持久化媒体标识，重启后可重新获取并处理。撤销授权会停止账户；普通历史或目录错误保留检查点和独立重试时间。诊断仅包含时间和错误类型，不包含提供方的异常正文。

`can_send` 根据每位参与者的成员身份、发布策略、来源权限和账户连接计算。无法发送时，API 在创建消息和发送任务之前拒绝请求。原生 `announcement_only` 频道允许所有者、管理员和版主发布；所有者和管理员管理此策略及高级角色。`announce` 仍表示创建频道时的通知。目前公告模式用于未加密的原生频道。Telegram 广播频道遵循订阅者实际拥有的发布权限。

与 Zulip 一样，导入的 Telegram 身份使用不含授权会话的外部账户记录。经验证的登录可以绑定导入身份，但不能占用已连接到另一位 Workspace 用户的身份。提及仅通过所选主题的提供方及项目解析，并支持账户绑定前导入记录的稳定 UUID。姓名和邮箱不能作为身份匹配依据。

## Telegram 通知模式

流通知模式双向同步。`all_messages` 显式取消 Telegram 静音。`muted` 和 `mentions_only` 都设置 Telegram 长期静音，同时保留 Workspace 各自的本地通知行为。同值回显不会覆盖本地模式。来源随后发生有效变化时，映射为 `muted` 或 `all_messages`。Telegram 并没有所有 Workspace 模式的精确对应。主题的 `default`、`follow`、`unmute` 和 `mute` 保持为本地偏好，不修改整个 Telegram 聊天。

同步会导入对话设置以及用户、群组和广播的继承默认值。没有 pts 的更新独立处理。待发送的本地修改与观测时间可防止过期响应覆盖新偏好。发送操作只修改 `mute_until`，保留声音和预览设置。验收测试必须保存并恢复目标来源的通知偏好。
