# Genesis Workspace Backend

Backend services for **Genesis Workspace Messenger**. The public Messenger API
is IAM-authenticated and preserves the existing REST and realtime contracts.
Canonical Messenger persistence is PostgreSQL.

External accounts are an owner-scoped registry with a required `provider` and
generic `settings`. Creating an account does not connect or synchronize it.
External services handle provider processing. Workspace messages and chats
retain `source_name: native` and `source: {"kind": "native"}` and use ordinary
membership permissions. Attachments use the configured file storage and IAM
remains the source of authentication.

The rendered documentation is published at
https://exordos.github.io/workspace_backend/ and is available in four
languages with the same file and section structure:

- [English](docs/en/index.md)
- [Русский](docs/ru/index.md)
- [Deutsch](docs/de/index.md)
- [中文](docs/zh/index.md)

English contracts and integration guidance:

- [`docs/en/workspace_api.md`](docs/en/workspace_api.md)
- [`docs/en/workspace_ui_realtime_integration.md`](docs/en/workspace_ui_realtime_integration.md)
- [`docs/public_workspace_api.yaml`](docs/public_workspace_api.yaml)

The generated public Workspace and Messenger specification uses OpenAPI 3.0.3.

## Runtime entry points

Direct local services:

- Messenger REST API: `http://127.0.0.1:21084/v1/messenger`
- WebSocket API: `ws://127.0.0.1:21082/v1/events/ws`
- Workspace REST API: `http://127.0.0.1:21084/v1`
- Messenger worker: `workspace-messenger-worker`
- Workspace OpenAPI spec: `http://127.0.0.1:21084/specifications/3.0.3`

The single `workspace-api` process serves all REST resources, including Messenger,
with two HTTP workers by default.

Nginx exposes:

- Common REST API: `/api/workspace/v1/{users,services,me,events,epoch}/...`
- Messenger REST API: `/api/workspace/v1/messenger/...`
- WebSocket API: `/api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`
- OpenAPI spec: `/api/workspace/specifications/3.0.3`
- Upload request limit: `50m`

The backend image contains only the Workspace services and their internal nginx
API gateway. The independently versioned `workspace_ui` element owns the public
load balancer, serves its immutable web artifact at `/`, and proxies `/api/` to
the `backend_node` resource exported by this element. Backend releases no longer
resolve, build, or package UI source.

For a tagged backend revision, CI publishes the versioned backend element and
refreshes `workspace/latest`. Updating the UI does not rebuild or replace the
backend VM.

`GET /v1/messenger/server_settings` is public and is handled by middleware for
Workspace client bootstrap behavior. Its `realm_icon` is a public URL
URN derived from the canonical request realm as
`urn:url:<realm>/logo-512x512.png`; the `workspace_ui` load balancer serves that
path from its versioned web artifact. All other Workspace and Messenger
resources are scoped using the IAM bearer token, its user UUID, and its project
ID.

An event cursor is the pair `(epoch_generation, epoch_version)`. A cold
connection may use `last_epoch_version=0` without a generation; every non-zero
resume must include the saved generation. After missed events, the websocket
sends a `ready` control frame before it can deliver live events.

## Storage

PostgreSQL is the source of truth for messages, membership, user state, events,
external-account registration, and client settings. Messenger has no SMTP, IMAP, Exim,
Dovecot, or Maildir runtime dependency.

Files use the configured S3-compatible storage backend. PostgreSQL stores file
metadata and ACL state; S3 stores file bytes and JSON sidecars. Messages contain
authorized URNs, never binary MIME parts.

## Local development

The project virtual environment is expected at `.tox/develop`.

The tracked `etc/workspace/workspace.conf` intentionally contains no IAM JWKS
decryption key. For local development, copy it to the ignored local config and
set the key there:

```bash
cp etc/workspace/workspace.conf etc/workspace/workspace.local.conf
```

Pass `--config-file etc/workspace/workspace.local.conf` to local services and
utilities that need IAM authentication. Never commit that file. Deployed
elements receive the key from the Exordos Core secret import and do not use the
local config.

Useful RESTAlchemy utilities are available there, including:

- `.tox/develop/bin/ra-new-migration`
- `.tox/develop/bin/ra-apply-migration`
- `.tox/develop/bin/ra-rollback-migration`
- `.tox/develop/bin/ra-rename-migrations`

Apply migrations with:

```bash
.tox/develop/bin/ra-apply-migration --config-file etc/workspace/workspace.local.conf --path migrations
```

Before upgrading an installation that predates the project-dense message
sequence, take and verify a PostgreSQL backup. The compact-state compatibility
steps `0142` and `0143` are intentionally forward-only because a rollback would
renumber messages after bitmap coordinates had been published. Once either
step is applied, rollback requires restoring that pre-upgrade database backup;
do not use `ra-rollback-migration` for this upgrade path.

Use the `admin/admin` account for local manual checks when the environment
provides the test IAM user.
