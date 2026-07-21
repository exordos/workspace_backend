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

Current contracts and integration guidance:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/workspace_api.md`](docs/workspace_api.md)
- [`docs/workspace_ui_realtime_integration.md`](docs/workspace_ui_realtime_integration.md)
- [`docs/workspace_provider_api_v1.yaml`](docs/workspace_provider_api_v1.yaml)

Provider product, control-plane, file-boundary, and reusable acceptance
contracts:

- [`docs/zulip_bridge_v1_product_and_api.md`](docs/zulip_bridge_v1_product_and_api.md)
- [`docs/zulip_bridge_control_api_v1.yaml`](docs/zulip_bridge_control_api_v1.yaml)
- [`docs/zulip_bridge_file_api_v1.yaml`](docs/zulip_bridge_file_api_v1.yaml)
- [`docs/zulip_bridge_v1_test_plan.md`](docs/zulip_bridge_v1_test_plan.md)

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

The backend image serves one Workspace UI build at `/`. The element build
resolves the `workspace_ui` `master` head to an immutable commit SHA before
packaging its source. CI runs `exordos/ci/prepare-workspace-ui-source.sh`
automatically. Before a local `exordos build`, run the same script from the
repository root. The image build fails if the resulting UI bundle does not use
the root asset and PWA paths.

For a tagged backend revision, CI publishes the versioned element and refreshes
`workspace/latest`. The resolver records the selected UI revision in
`.workspace-ui-ref`; the image build copies it to `/build-ref.txt`, where the
deployed UI exposes `ref=master` and `commit=<sha>`.

`GET /v1/messenger/server_settings` is public and is handled by middleware for
Zulip-compatible client bootstrap behavior. Its `realm_icon` is a public URL
URN derived from the canonical request realm as
`urn:url:<realm>/logo-512x512.png`; nginx serves that path without
authentication from the packaged `pwa-512x512.png` organization emblem. All
other Workspace and Messenger resources are scoped using the IAM bearer token,
its user UUID, and its project ID.

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

Use the `admin/admin` account for local manual checks when the environment
provides the test IAM user.
