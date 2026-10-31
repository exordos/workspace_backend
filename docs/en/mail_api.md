# Workspace Mail API

This is the active browser-facing mail contract. It extends the unified
[`workspace-api`](workspace_api.md) service under `/api/workspace/v1/mail/`;
it does not introduce a second public API process.

## Architecture and Scope

- PostgreSQL is canonical for `m_mail_folders`, `m_mail_messages`, and
  `mail_outgoing_intents`.
- Attachments reuse Workspace file storage and are referenced by `urn:file:`;
  bytes remain in S3.
- Mail resources use the existing IAM project and current-user scope, durable
  Workspace events, REST event backfill, and WebSocket delivery.
- A separate mail runtime may consume `mail_outgoing_intents` and project
  provider changes into the canonical tables. That runtime and live
  SMTP/IMAP delivery are outside this backend change.
- `/api/workspace/v1/calendar/` and public provider-specific routes remain
  absent.

## Resources and Actions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/mail/folders/` | List owned folders or create one. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/folders/{folder_uuid}` | Read, update, or soft-delete an empty owned folder. |
| `GET`, `POST` | `/api/workspace/v1/mail/messages/` | List owned messages or create a local draft. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/messages/{message_uuid}` | Read, update, or soft-delete an owned message. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/move/invoke` | Move a message between owned folders of the same account. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/send/invoke` | Atomically queue a draft and create one durable outgoing intent. |

The generated Workspace OpenAPI is authoritative for complete field schemas,
filter parameters, status codes, and response bodies.

## Draft and Send Flow

1. Create or select a folder. An `external_account_uuid`, when present, must
   be an enabled `mail` account owned by the current user in the IAM project.
2. Create a message with `folder_uuid`; it starts with `draft=true` and
   `delivery_status=draft`.
3. Update draft content or attach existing visible Workspace files.
4. Invoke `send`. The same database transaction validates recipients, body,
   account, and attachment visibility; inserts one `mail_outgoing_intents`
   row; sets `delivery_status=queued`; and emits `mail.message.updated`.
5. `sent_at` remains empty until a mail runtime confirms provider delivery.
   A queued message cannot be deleted through the public API.

The send action is intentionally not proof of external delivery. Until a mail
runtime reports a terminal result, the supported statement is only that the
request was durably queued.

## Attachments

Upload a private mail attachment through the existing multipart file endpoint:

```text
POST /api/workspace/v1/messenger/files/
Content-Type: multipart/form-data

file=<bytes>
acl={"mode":"owner"}
```

Use the returned UUID as `urn:file:<file_uuid>` in `attachment_urns`. The API
accepts only files visible to the current IAM user in the current project.
`acl={"mode":"owner"}` creates a private file without a stream; existing
stream-visible and public Workspace files can also be referenced.

## Realtime Events

| Kind | Object type | When emitted |
| --- | --- | --- |
| `mail.folder.created` | `mail_folder` | Folder creation. |
| `mail.folder.updated` | `mail_folder` | Folder metadata change. |
| `mail.folder.deleted` | `mail_folder` | Folder soft deletion. |
| `mail.message.created` | `mail_message` | Draft or projected message creation. |
| `mail.message.updated` | `mail_message` | Content, state, move, or queue change. |
| `mail.message.deleted` | `mail_message` | Message soft deletion. |

These kinds use the same `/api/workspace/v1/events/` cursor and
`/api/workspace/v1/events/ws` stream as Messenger resources.

## Acceptance Boundary

Backend acceptance requires migration upgrade/downgrade, OpenAPI generation,
owner/project isolation, owner-file visibility, durable intent idempotency, and
event tests. It does not establish a deployed mail runtime, SMTP/IMAP access,
provider acceptance, or a delivered copy in an external Sent folder.
