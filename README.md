# Genesis Workspace Backend

Backend services for **Genesis Workspace** messenger functionality. The current
branch provides a REST messenger API, durable realtime events, a websocket event
stream for UI synchronization, and a messenger worker for background presence
maintenance.

Current contracts are documented in:

- [`docs/workspace_api.md`](docs/workspace_api.md)
- [`docs/workspace_ui_realtime_integration.md`](docs/workspace_ui_realtime_integration.md)

## Runtime Entry Points

Direct local services:

- REST API: `http://127.0.0.1:21081/v1`
- WebSocket API: `ws://127.0.0.1:21082/v1/events/ws`
- Messenger worker: `workspace-messenger-worker`
- OpenAPI spec: `http://127.0.0.1:21081/specifications/3.0.3`

Nginx exposes the messenger services as:

- REST API: `/api/messenger/v1/...`
- WebSocket API: `/api/messenger/ws?last_epoch_version=<number>`
- OpenAPI spec: `/api/messenger/specifications/3.0.3`
- Upload request limit: `50m`

Deployment stores uploaded files through the configured messenger file
storage backend. For durable deployments prefer S3; local storage uses the node
filesystem path configured in `messenger_files.storage_path`.

## API Surface

The messenger API is IAM-scoped. REST requests use:

```http
Authorization: Bearer <access_token>
```

The main resources are:

- `folders` and `folder_items`
- `streams` and `stream_bindings`
- `stream_topics`
- `messages` and `message_reactions`
- `files`
- `events` and `epoch`
- `users` and `me`

`GET /v1/server_settings` is public and is handled by middleware for
Zulip-compatible client bootstrap behavior.

## Data Scoping

`user_uuid` comes from IAM token information and `project_id` comes from IAM
introspection information. User-scoped controllers automatically filter and
write the current user and project scope.

Workspace events are written to a durable outbox. REST catch-up uses
`GET /v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500`, while
live updates are delivered through the websocket service. REST `/events/` and
websocket messages use the same flat `schema_version: 1` event object; websocket
messages are not wrapped in a `{type, event}` envelope.

## Local Development

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
