# Workspace Mail API

Mail uses the unified [`workspace-api`](workspace_api.md) service. Its resources
belong to the active IAM project and current user.

## Resources and Actions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/mail/folders/` | List folders or create one. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/folders/{folder_uuid}` | Read, update or remove an empty folder. |
| `GET`, `POST` | `/api/workspace/v1/mail/drafts/` | List or create drafts. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/drafts/{draft_uuid}` | Read, edit or delete a draft. |
| `POST` | `/api/workspace/v1/mail/drafts/{draft_uuid}/actions/send/invoke` | Queue a draft as a message with the same UUID. |
| `GET` | `/api/workspace/v1/mail/messages/` | List messages. |
| `GET`, `DELETE` | `/api/workspace/v1/mail/messages/{message_uuid}` | Read or remove a message. |

Message actions use `/api/workspace/v1/mail/messages/{message_uuid}/actions/<name>/invoke`.
`read`, `unread`, `star`, and `unstar` set flags. `move` accepts `folder_uuid`
within the same account. `trash` uses the account's `trash` folder and `restore`
returns the message to its prior folder, an available non-trash folder, or a new
inbox if the account only has a trash folder.

## Message Body

Addresses, subject, reply reference and attachment URNs are fields on the draft
or message. `payload` contains a typed array of body parts.

```json
{
  "subject": "Report",
  "to_addresses": ["reader@example.com"],
  "payload": {
    "kind": "multipart",
    "parts": [
      {"kind": "text", "content": "The report is ready."},
      {"kind": "html", "content": "<p>The report is ready.</p>"}
    ]
  }
}
```

A single body part uses the same array. Text and HTML can represent the same
body in two formats. A draft may start with an empty `parts` array.

## Send and Attachments

The send action checks the sender account, recipients and attachment visibility.
It moves the draft into the message collection with `delivery_status=queued` and
records one durable send intent in the same transaction. `sent_at` stays empty
until a mail runtime confirms transport. For `imap_smtp` accounts, the runtime
uses the SMTP server configured for the sender account.

Upload files through the existing Workspace file endpoint. The draft stores the
returned `urn:file:<file_uuid>` references in `attachment_urns`.

```text
POST /api/workspace/v1/messenger/files/
Content-Type: multipart/form-data

file=<bytes>
acl={"mode":"owner"}
```

## Realtime

Mail uses the shared `/api/workspace/v1/events/` and `/api/workspace/v1/events/ws`.
Folder, draft and message changes use `mail.folder.*`, `mail.draft.*`, and
`mail.message.*`. Queuing emits `mail.draft.deleted` and `mail.message.created`.

See [Calendar and external accounts](calendar_api.md) for account capabilities.

## External Mail Accounts

Use the external-accounts collection with the `imap_smtp` provider. The same
Workspace user can connect multiple accounts. Each account owns its own folders,
mailbox progress, and SMTP queue.

```json
{
  "provider": "imap_smtp",
  "settings": {
    "email": "customer@example.org",
    "imap": {"host": "imap.example.org", "port": 993, "tls": "implicit"},
    "smtp": {"host": "smtp.example.org", "port": 587, "tls": "starttls"},
    "password": "application-password"
  }
}
```

The optional `settings.username` defaults to `email`. Both protocols use the
same login and password. Use `implicit` for TLS from the start of a connection,
or `starttls` for a connection that requires a TLS upgrade before authentication.
The endpoint must resolve to public IP addresses and present a trusted certificate.
The worker imports incoming mail and attachments even when SMTP is unavailable.
The account reports the SMTP error until sending becomes available again.
IMAP synchronization accepts up to 512 folders and processes bounded batches.
Messages larger than 16 MiB of raw mail data are skipped. Mail operations on
the imported copy are local to Workspace.

The password never appears in an API response or event. Disconnect with
`{"disabled": true}` and reconnect with `disabled: false` plus a new
`settings.password`. Pending outgoing mail must finish before disconnecting.
