# Workspace Mail API

这是当前有效的浏览器端邮件契约。它在统一的
[`workspace-api`](workspace_api.md) 服务中增加 `/api/workspace/v1/mail/`，
不会引入第二个公开 API 进程。

## 架构与范围

- PostgreSQL 是 `m_mail_folders`、`m_mail_messages` 和
  `mail_outgoing_intents` 的规范存储。
- 附件复用 Workspace 文件存储并通过 `urn:file:` 引用；字节仍保存在 S3。
- 邮件资源复用现有 IAM 项目和当前用户作用域、持久 Workspace 事件、
  REST backfill 与 WebSocket 投递。
- 独立邮件 runtime 可以消费 `mail_outgoing_intents`，并把提供方变化投影到
  规范表中。该 runtime 和真实 SMTP/IMAP 投递不在本次 backend 修改范围内。
- `/api/workspace/v1/calendar/` 和公开的提供方专用路由仍不存在。

## 资源与操作

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/mail/folders/` | 列出自有文件夹或创建文件夹。 |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/folders/{folder_uuid}` | 读取、更新或软删除自有空文件夹。 |
| `GET`, `POST` | `/api/workspace/v1/mail/messages/` | 列出自有邮件或创建本地草稿。 |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/messages/{message_uuid}` | 读取、更新或软删除自有邮件。 |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/move/invoke` | 在同一账户的自有文件夹之间移动邮件。 |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/send/invoke` | 原子地排队草稿并创建一个持久发送 intent。 |

生成的 Workspace OpenAPI 是完整字段模式、过滤参数、状态码和响应体的权威来源。

## 草稿与发送流程

1. 创建或选择文件夹。若指定 `external_account_uuid`，它必须是当前用户在
   IAM 项目中启用的 `mail` 账户。
2. 使用 `folder_uuid` 创建邮件；初始状态为 `draft=true` 和
   `delivery_status=draft`。
3. 更新草稿内容或附加当前可见的 Workspace 文件。
4. 调用 `send`。同一数据库事务会校验收件人、正文、账户和附件可见性，
   插入一行 `mail_outgoing_intents`，设置 `delivery_status=queued`，并发出
   `mail.message.updated`。
5. `sent_at` 保持为空，直到邮件 runtime 确认提供方已投递。已排队邮件不能
   通过公开 API 删除。

发送操作本身不是外部投递证明。在邮件 runtime 报告最终结果前，只能确认请求
已被持久排队。

## 附件

通过现有 multipart 文件端点上传私有邮件附件：

```text
POST /api/workspace/v1/messenger/files/
Content-Type: multipart/form-data

file=<bytes>
acl={"mode":"owner"}
```

把返回的 UUID 作为 `urn:file:<file_uuid>` 写入 `attachment_urns`。API 只接受
当前 IAM 用户在当前项目中可见的文件。`acl={"mode":"owner"}` 会创建不属于
任何 stream 的私有文件；也可引用已有的 stream-visible 或 public Workspace 文件。

## Realtime 事件

| Kind | 对象类型 | 触发时机 |
| --- | --- | --- |
| `mail.folder.created` | `mail_folder` | 创建文件夹。 |
| `mail.folder.updated` | `mail_folder` | 文件夹元数据变化。 |
| `mail.folder.deleted` | `mail_folder` | 软删除文件夹。 |
| `mail.message.created` | `mail_message` | 创建草稿或投影邮件。 |
| `mail.message.updated` | `mail_message` | 内容、状态、移动或排队变化。 |
| `mail.message.deleted` | `mail_message` | 软删除邮件。 |

这些 Kind 与 Messenger 资源共用 `/api/workspace/v1/events/` cursor 和
`/api/workspace/v1/events/ws` stream。

## 验收边界

Backend 验收包括迁移 upgrade/downgrade、OpenAPI 生成、owner/project 隔离、
owner 文件可见性、持久 intent 幂等性和事件测试。它不证明已部署邮件 runtime、
SMTP/IMAP 访问、提供方验收或外部 Sent 文件夹中存在已投递副本。
