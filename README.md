# Genesis Workspace Backend

Backend services for **Genesis Workspace Messenger**. The public Messenger API
is IAM-authenticated and preserves the existing REST and realtime contracts.
Message persistence is implemented by the local mail stack described in
[`docs/mail_backed_messenger_architecture.md`](docs/mail_backed_messenger_architecture.md):
Exim4, Dovecot, and Maildir run on a dedicated mail node in the same element.
Their authenticated protocol listeners remain on the platform-internal network.

The backend does not expose provider, mail, calendar, or external-integration
APIs. Messenger attachments continue to use the configured S3-compatible file
storage and IAM remains the source of users and authentication.

Current contracts and integration guidance:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/workspace_api.md`](docs/workspace_api.md)
- [`docs/workspace_ui_realtime_integration.md`](docs/workspace_ui_realtime_integration.md)
- [`docs/mail_backed_messenger_architecture.md`](docs/mail_backed_messenger_architecture.md)

## Runtime entry points

Direct local services:

- Messenger REST API: `http://127.0.0.1:21081/v1`
- WebSocket API: `ws://127.0.0.1:21082/v1/events/ws`
- Workspace REST API: `http://127.0.0.1:21084/v1`
- Messenger worker: `workspace-messenger-worker`
- Messenger OpenAPI spec: `http://127.0.0.1:21081/specifications/3.0.3`
- Workspace OpenAPI spec: `http://127.0.0.1:21084/specifications/3.0.3`

Nginx exposes:

- Common REST API: `/api/workspace/v1/{users,services,me,events,epoch}/...`
- Messenger REST API: `/api/workspace/v1/messenger/...`
- WebSocket API: `/api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`
- OpenAPI spec: `/api/workspace/specifications/3.0.3`
- Upload request limit: `50m`

`GET /v1/messenger/server_settings` is public and is handled by middleware for
Zulip-compatible client bootstrap behavior. All other Workspace and Messenger
resources are scoped using the IAM bearer token, its user UUID, and its project
ID.

An event cursor is the pair `(epoch_generation, epoch_version)`. A cold
connection may use `last_epoch_version=0` without a generation; every non-zero
resume must include the saved generation. After missed events, the websocket
sends a `ready` control frame before it can deliver live events.

## Storage

Maildir is the source of truth for message content and state. The Maildir tree
must be placed on the mail node's persistent data disk. The backend accesses
Exim4 and Dovecot only through authenticated internal SMTP and IMAP; these protocols are not public API
surfaces.

Files and their metadata and access-control records use the configured
S3-compatible storage backend. PostgreSQL stores a rebuildable projection for
fast reads, search, counters, events, and client settings; it is never the only
copy of a message or shared Messenger state.

## Local development

The project virtual environment is expected at `.tox/develop`.

Useful RESTAlchemy utilities are available there, including:

- `.tox/develop/bin/ra-new-migration`
- `.tox/develop/bin/ra-apply-migration`
- `.tox/develop/bin/ra-rollback-migration`
- `.tox/develop/bin/ra-rename-migrations`

Apply migrations with:

```bash
.tox/develop/bin/ra-apply-migration --config-file etc/workspace/workspace.conf --path migrations
```

Use the `admin/admin` account for local manual checks when the environment
provides the test IAM user.
