# Genesis Workspace Backend

Backend services for **Genesis Workspace Messenger**. The public Messenger API
is IAM-authenticated and preserves the existing REST and realtime contracts.
Canonical Messenger persistence is PostgreSQL.

The browser-facing contract does not expose mail or calendar APIs. Provider
runtimes use the private, bridge-authenticated Workspace Provider HTTP API at
`/api/workspace-provider/v1`; it projects ordinary Messenger resources into the
same public API used by the UI. Messenger attachments continue to use the
configured S3-compatible file storage and IAM remains the source of users and
authentication.

The rendered documentation is published at
https://exordos.github.io/workspace_backend/ and is available in four
languages with the same file and section structure:

- [English](docs/en/index.md)
- [Русский](docs/ru/index.md)
- [Deutsch](docs/de/index.md)
- [中文](docs/zh/index.md)

English contracts and integration guidance:

- [`docs/en/architecture.md`](docs/en/architecture.md)
- [`docs/en/workspace_api.md`](docs/en/workspace_api.md)
- [`docs/en/workspace_ui_realtime_integration.md`](docs/en/workspace_ui_realtime_integration.md)
- [`docs/workspace_provider_api_v1.yaml`](docs/workspace_provider_api_v1.yaml)
- [`docs/workspace_provider_api_v2.yaml`](docs/workspace_provider_api_v2.yaml)
- [`docs/en/workspace_server_v2_decisions.md`](docs/en/workspace_server_v2_decisions.md)

Provider product, control-plane, file-boundary, and reusable acceptance
contracts:

- [`docs/en/zulip_bridge_v1_product_and_api.md`](docs/en/zulip_bridge_v1_product_and_api.md)
- [`docs/zulip_bridge_control_api_v1.yaml`](docs/zulip_bridge_control_api_v1.yaml)
- [`docs/zulip_bridge_file_api_v1.yaml`](docs/zulip_bridge_file_api_v1.yaml)
- [`docs/en/zulip_bridge_v1_test_plan.md`](docs/en/zulip_bridge_v1_test_plan.md)

The private Provider, control, and file specifications are not browser routes.
The generated public Workspace and Messenger specification remains OpenAPI
3.0.3 and preserves the existing UI contract.

## Runtime entry points

Direct local services:

- Messenger REST API: `http://127.0.0.1:21081/v1`
- WebSocket API: `ws://127.0.0.1:21082/v1/events/ws`
- Workspace REST API: `http://127.0.0.1:21084/v1`
- Private bridge control and Provider API: `workspace-external-bridge-api`
- Messenger worker: `workspace-messenger-worker`
- Messenger OpenAPI spec: `http://127.0.0.1:21081/specifications/3.0.3`
- Workspace OpenAPI spec: `http://127.0.0.1:21084/specifications/3.0.3`

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
Zulip-compatible client bootstrap behavior. Its `realm_icon` is a public URL
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
provider mappings, and client settings. Messenger has no SMTP, IMAP, Exim,
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

## Native encrypted Android push

The existing `PUT/DELETE /api/workspace/v1/push_devices/{device_uuid}` API
registers a device's FCM token and raw X25519 public key under the authenticated
user/project. Private keys stay on the phone. The registration UUID stays stable
when a token changes; there are no registration revisions.

When `[mobile_push]` is configured, native message fanout writes an encrypted
per-user event snapshot into `messenger_push_outbox` in the same transaction.
The existing primary Messenger worker forwards it to the local Notification
v1 API and polls that event UUID. Per-device HPKE uses RFC 9180
X25519/HKDF-SHA256/AES-256-GCM. The action, server/project/chat/message route,
recipient, device and key UUID are authenticated as associated data.

The two kinds are `workspace_message_create` and
`workspace_message_cancelled`. Reading a message, topic or stream enqueues a
separate cancellation with the same public message placement UUID. Existing
mute, mention, topic-override, membership and unread rules determine recipients.
The author, protocol-only key messages and history import do not produce alerts.
Ordinary preview text is bounded before encryption. End-to-end encrypted chat
payloads remain opaque and produce a generic encrypted-message preview.

The worker retries the same event UUID/ciphertext after timeouts or restart,
retains the initial one-hour deadline, and propagates inactive-token errors.
It deletes a device registration only if its current token still equals the
rejected snapshot. Temporary errors keep the outbox retryable. Notification
transport choice remains in its owner-scoped binding API; Workspace preference
proxy endpoints and Android preference screens are deferred.

Configuration keys are `mobile_push.organization_uuid`,
`mobile_push.notification_endpoint` and `mobile_push.notification_auth_file`.
The managed backend node UUID supplies the stable installation identity.
The feature is disabled until the endpoint and private credential file are set.
The [optional site manifest](exordos/manifests/mobile_push.yaml.j2) connects the
existing worker to the local Notification and provisions a protected Event
proxy through the existing Workspace gateway. Services read drop-ins from
`/etc/workspace/workspace.conf.d`. Firebase sender credentials belong only on
the upstream Notification, never in Workspace or customer installations.

The complete event, encryption, relay and deployment contract is documented in
`exordos_notification/docs/mobile-push.md`. Android integration is documented in
`workspace_android/docs/mobile-push.md`.

Tests include public HPKE vectors and a shared Python/Android event fixture,
plus real PostgreSQL/HTTP integration of create, message/topic/stream read
cancellation, retry and stale-token cleanup. Run the latter against a dedicated
empty test database only:

```bash
WORKSPACE_TEST_DB_URL='<DEDICATED_TEST_POSTGRES_URL>' \
  .tox/develop/bin/python -m pytest workspace/tests/integration/test_mobile_push.py
```

Its fixture recreates the test database's public schema. Use immutable
`exordos build`/`exordos deploy` artifacts for runtime updates and preserve a
Workspace database backup before applying migrations. A queued event or an
`ACTIVE` transport status does not prove display on a real phone.
