# Zulip bridge v1: product requirements and API boundary

Status: **approved product requirements and API boundary; implementation in
progress and gated by the required acceptance plan**.

This document defines the first external-messenger integration for Workspace.
It is intentionally separate from the current Messenger API contract in
[`workspace_api.md`](workspace_api.md). The feature branches contain the
corresponding API, PostgreSQL storage, provider HTTP, bridge, and UI
implementation, but
the feature is not ready for realm enablement until every required gate in
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md) passes.

The public account API and shared bridge contracts are provider-neutral. Every
provider-specific account payload is represented by a RestAlchemy dynamic kind
model. Zulip is the first provider and a separately deployed
`workspace-zulip-bridge` element is the first implementation. Later Telegram,
mail, calendar, and other account kinds extend the selector with new kind
models instead of adding provider-specific fields or routes to the common
resource.

## 1. Goals

- Let a Workspace user connect one personal Zulip account in a realm.
- Project selected Zulip conversations into ordinary Workspace streams and
  topics without creating a separate external-inbox product surface.
- Provide two-way synchronization for the v1 capability set.
- Preserve the canonical Workspace architecture: PostgreSQL stores canonical
  message and Messenger event data; files use object storage and messages
  contain only URNs.
- Keep the provider bridge independently deployable and prevent it from reading
  unrelated Workspace database rows or S3 objects.
- Keep provider credentials encrypted at rest and unavailable to browsers,
  administrators, logs, and ordinary Workspace API responses.
- Make native Workspace messaging continue to work when the bridge is down.

## 2. Non-goals for v1

- Reactions, polls, typing indicators, calls, and presence synchronization.
- High availability for the bridge element.
- Providers other than Zulip.
- A product-level audit log. Operational logs and aggregate health remain in
  scope, but they must not contain credentials or message content.
- Backup implementation. Backups are provided by another subsystem.
- Compatibility with the removed Provider API or the hidden legacy UI routes
  `/providers/` and `/external_users/`.

## 3. Approved product behavior

### 3.1 Account ownership and lifecycle

- An external account belongs privately to one Workspace user and is global to
  that user within the realm.
- A user may have at most one external account of a provider type. For v1 this
  means at most one Zulip account.
- Zulip setup requires an HTTPS server URL, email address, and API key.
- Credentials are write-only. The API may report that a credential exists, but
  never returns the API key or an encrypted credential envelope.
- `Disconnect` stops synchronization while retaining a read-only projection.
- `Delete` removes the credential, mappings, projected entities, queued work,
  and provider-owned copied files.
- IAM deactivation suspends synchronization and hides the account. IAM deletion
  purges it using the same destructive semantics as `Delete`.

### 3.2 Chat selection and project assignment

- The owner can select individual external chats or select `all`.
- `all` is dynamic: chats created later are selected automatically.
- The owner chooses a history depth for the account: `new`, `7_days`,
  `30_days`, `90_days`, or `all`. The default is `30_days`.
- Every selected external chat belongs to exactly one Workspace project.
- The account has a default project for newly selected chats.
- Moving an existing projection to another project is atomic from the product
  perspective and preserves Workspace UUIDs, history, read state, and provider
  mappings. The implementation must emit source-project removal and
  target-project creation snapshots; it must not expose an intermediate state
  in which the projection belongs to neither project or both projects.

### 3.3 Zulip-to-Workspace mapping

| Zulip entity  | Workspace projection                                                        |
| ------------- | --------------------------------------------------------------------------- |
| Channel       | Stream                                                                      |
| Topic         | Topic in the projected stream                                               |
| One-to-one DM | Private personal stream with exactly two participants and one default topic |
| Group DM      | Private group stream with one default topic                                 |
| Zulip user    | Stable external identity scoped by provider and external account            |

Stable external identity UUIDs are derived from provider type, external account
UUID, and provider user ID. An external identity is not an IAM user even when
its email or display name matches one. Mentions continue to use
`urn:user:<identity-uuid>`.

### 3.4 Synchronization semantics

- V1 supports two-way create, edit, delete, read state, mentions, replies,
  quotes, Markdown, links, images, files, and stream/topic rename where the
  provider capability allows it.
- Outbound operations use the owner's personal Zulip account.
- Provider capabilities are authoritative. Unsupported actions are hidden;
  temporarily unavailable actions are disabled with a safe explanation.
- The last confirmed operation wins. Delete wins over a concurrent edit.
- Every operation has a stable UUID and provider idempotency metadata.
- Backfill runs newest-to-oldest. Live synchronization starts first and has
  strict scheduling priority over outbox retries and backfill.
- Initial catch-up does not create desktop notifications. Notifications are
  enabled only after the account reaches live-ready state.
- Each accepted provider entity stores the ingress `delivery_class` and a
  frozen `notification_eligible` decision in its public provider metadata.
  `backfill` is always ineligible. A `live` message is eligible only when the
  account notification gate was already open at ingestion; a live message
  accepted while account history is still catching up remains non-notifying.
  Later account-state changes never retroactively promote stored messages.
- A durable outbox retains retryable operations for up to 24 hours. The UI
  shows `pending`, then either `delivered` or `failed`. A failed operation can
  be retried or discarded.
- Deselection or provider access loss cancels pending work and immediately
  removes the projection and copied provider files.
- Target healthy-system latency is p95 at most 5 seconds. An account becomes
  `degraded` after 30 seconds without synchronization progress.

### 3.5 Loss-aware content conversion

- Supported Zulip content is converted to canonical Workspace Markdown.
- Unsupported incoming elements use a safe, readable fallback and an
  `Open original` link when Zulip can provide one.
- Raw provider IDs and structured conversion metadata are stored in internal
  projection metadata, not exposed as writable browser fields.
- Before an outgoing operation known to lose information, the UI obtains a
  server-side preflight result and requires explicit confirmation.
- Attachments are copied into the receiving system. Workspace stores received
  bytes plus a JSON sidecar in S3-compatible storage; the message contains only
  `urn:*` references.

### 3.6 Administration

- The account owner sees account, chat-selection, progress, capability, and
  safe-error state for their own account.
- A realm administrator manages provider policy, custom CA certificates,
  limits, and emergency suspend/resume actions.
- A realm administrator sees aggregate bridge/account health only. The admin
  surface must not expose credentials, message content, or the owner's chat
  catalog.
- Outbound Zulip TLS uses the system trust store plus administrator-managed
  custom CA certificates. Hostname verification is always enabled; insecure
  or skip-verification modes are forbidden.

## 4. Current-contract constraints

The implementation must extend the current Messenger contract instead of
reviving historical integration code.

- Browser APIs remain under `/api/workspace/v1/messenger/**` and use the IAM
  bearer token. The token currently supplies a user UUID and project ID.
- Provider-management routes remain additive to the current browser contract.
- PostgreSQL is authoritative for messages, shared Messenger state, account
  settings, encrypted credentials, provider queues, and deduplication records.
- File copying uses a separate, narrow binary-transfer plane; it cannot grant
  the bridge global S3 credentials.
- Public `provider` and `delivery` fields are reserved but not populated
  consistently in every current serializer. Serializer parity is a prerequisite
  for enabling the feature.
- Browser event cursors are project- and user-scoped with seven-day retention.
  They are not bridge queue cursors and must not be reused as provider outbox or
  backfill positions.
- The current `ExternalAccount` residue is project-scoped and permits plaintext
  JSON credentials. It must be replaced by a realm-scoped model; it is not a
  migration target or a compatibility contract.

## 5. Public Workspace API proposal

The following routes are proposed under the current IAM-authenticated Messenger
root. They must be generated into the Workspace OpenAPI and `@workspace/api`
client before UI implementation. All UUID collection routes use the standard
Messenger pagination contract. The routes are shared by every external account
kind; provider-specific routes are forbidden.

### 5.1 External accounts

| Method   | Route                                                         | Purpose                                                                      |
| -------- | ------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `GET`    | `/external_accounts/`                                         | List the current user's realm-global external accounts.                      |
| `POST`   | `/external_accounts/`                                         | Create and validate any supported account kind with a write-only credential. |
| `GET`    | `/external_accounts/{account_uuid}`                           | Get the owner's sanitized account snapshot.                                  |
| `PUT`    | `/external_accounts/{account_uuid}`                           | Replace mutable non-secret settings.                                         |
| `POST`   | `/external_accounts/{account_uuid}/actions/reconnect/invoke`  | Validate and replace the write-only credential, then resume.                 |
| `POST`   | `/external_accounts/{account_uuid}/actions/disconnect/invoke` | Stop sync and retain a read-only projection.                                 |
| `DELETE` | `/external_accounts/{account_uuid}`                           | Destructively purge the account and return `204`.                            |

The approved resource shape is a common envelope with a dynamic `settings`
property. The `settings.kind` discriminator selects a concrete
`AbstractKindModel` through
`KindModelSelectorType`. Common lifecycle, owner, status, revision, capability,
and timestamp fields stay outside `settings`. Each kind owns its connection,
credential, discovery, and synchronization settings. The API enforces that one
owner has at most one account of each `settings.kind` in the realm.

The public `capabilities` field is the backend-computed effective account-level
projection after provider, bridge-instance, realm-policy, and account-state
intersection. It is not the raw heartbeat descriptor and exposes neither the
bridge instance nor deployment topology. The UI uses this field for
account-level actions and status.

Public capabilities use a map from the same stable namespaced capability name
to an effective descriptor containing `available`, `revision`, `limits`, and
an optional structured safe `unavailable_reason`. A known capability that is
temporarily disabled by account state or policy remains present with
`available=false`; an absent name means the resource does not support that
capability at all. Clients must not reconstruct availability from raw status or
provider kind when an effective descriptor is present.

The create, sanitized-response, and reconnect settings selectors are distinct
API types. A create/reconnect kind may contain write-only credential fields;
the corresponding response kind cannot serialize them. Adding Telegram, mail,
calendar, or another provider means registering new settings kind models in the
selectors without changing the collection routes or adding nullable fields to
the common resource.

Zulip `POST /external_accounts/` request:

```json
{
  "uuid": "client-generated-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  }
}
```

Sanitized account response:

```json
{
  "uuid": "account-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  },
  "credential_present": true,
  "status": "live",
  "live_ready": true,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 7,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z"
}
```

Account status values are `connecting`, `backfill`, `live`, `degraded`,
`auth_required`, `disconnected`, and `suspended`.

`PUT` is revision-safe using a strong `ETag` and required `If-Match`. For the
Zulip kind it may change only `selection_mode`, `history_depth`, and
`default_project_id` inside `settings`. Server URL, email, and API key change
only through `reconnect`. `settings.kind` and owner are immutable. Other kinds
define their own mutable subset in their dynamic update model.

### 5.2 External chat catalog and assignment

The approved catalog shape is a common top-level Messenger resource. It is
available only for account kinds that advertise the `chat_catalog` capability;
an account kind such as mail or calendar does not need to implement it.

| Method | Route                                                 | Purpose                                                                |
| ------ | ----------------------------------------------------- | ---------------------------------------------------------------------- |
| `GET`  | `/external_chats/?external_account_uuid=...`          | List the owner's sanitized provider chat catalog and assignment state. |
| `GET`  | `/external_chats/{chat_uuid}`                         | Get one sanitized chat snapshot.                                       |
| `POST` | `/external_chats/{chat_uuid}/actions/select/invoke`   | Select a chat and assign a project.                                    |
| `POST` | `/external_chats/{chat_uuid}/actions/deselect/invoke` | Cancel work and remove the projection.                                 |
| `POST` | `/external_chats/{chat_uuid}/actions/move/invoke`     | Atomically move an existing projection to another project.             |

The resource uses a common envelope with a dynamic `source` property.
`source.kind` selects provider-specific catalog metadata with
`KindModelSelectorType`; for v1 its only implementation is `zulip`. Common
fields include Workspace-generated chat UUID, external account UUID, selection
state, project assignment, projection UUIDs, capabilities, status, revision,
and timestamps. Raw provider IDs are internal, never writable, and need not be
exposed.

Each chat's `capabilities` field is the backend-computed effective projection
for that chat and account. It may be narrower than the account-level projection
because provider chat type, assignment state, or policy can disable an action.
The UI never derives chat behavior from a raw bridge-instance capability map.
The backend retains the raw catalog descriptor separately so temporary
account/instance unavailability can disable the effective descriptor without
destroying the catalog capability restored after recovery.

`select` and `move` accept a `project_id`; `move` also requires `If-Match` for
the current assignment revision. `deselect` immediately cancels pending work
and removes the projection. Every action returns a full sanitized chat
snapshot.

`selection_mode=all` is Zulip account state, not a one-time batch action. The
backend continues assigning newly discovered chats to `default_project_id`
until the owner changes the mode.

### 5.3 External operations

The approved durable operation surface is a common top-level Messenger
resource. It represents provider-bound work for every external account kind,
including operations whose target has not been created or has already been
deleted.

| Method   | Route                                                        | Purpose                                                             |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------------- |
| `GET`    | `/external_operations/`                                      | List the owner's pending or failed external operations.             |
| `GET`    | `/external_operations/{operation_uuid}`                      | Get sanitized operation status.                                     |
| `POST`   | `/external_operations/{operation_uuid}/actions/retry/invoke` | Retry an eligible failed operation.                                 |
| `DELETE` | `/external_operations/{operation_uuid}`                      | Discard eligible pending/failed work and return `204`.              |
| `POST`   | `/external_operations/actions/preflight/invoke`              | Return capability and loss information before an outgoing mutation. |

An operation response uses a common envelope containing its UUID, external
account UUID, action, target type/UUID, status, safe error, retry/discard flags,
attempt and attempt history, duplicate-risk and retry-confirmation flags,
original provider URL when safe, reconciliation state/reason/evidence, revision,
and timestamps. A dynamic `details.kind` model contains sanitized
provider-specific delivery metadata. It does not contain raw provider payloads,
credentials, message content beyond the ordinary authorized target resource,
or raw provider history matches.

`delivery` on projected resources is extended consistently to contain
`external_operation_uuid`, `status`, `safe_error`, `can_retry`, `can_discard`,
`updated_at`, `duplicate_risk`, `retry_requires_confirmation`, `original_url`,
and `reconciliation_reason`. Its status is one of `pending`, `delivered`,
`failed`, `manual_reconciliation_required`, or `discarded`. Attempt history and
reconciliation evidence remain available only on the operation resource. Native
resources continue returning `provider: null` and `delivery: null`.

The shared `provider` envelope is:

```json
{
  "kind": "zulip",
  "account_uuid": "account-uuid",
  "external_id": "provider-entity-id",
  "capabilities": {},
  "delivery_class": "live",
  "notification_eligible": true
}
```

`delivery_class` is `live` or `backfill`; `notification_eligible` is the
backend-frozen ingestion decision described in section 3.4. REST and realtime
full snapshots carry the same values. Clients suppress desktop notification,
sound, and attention when it is explicitly `false`; native resources and
provider envelopes produced before this optional field existed retain their
normal notification policy.

Before mutating a provider-projected stream, topic, or message, the backend
locks its chat/account mapping and verifies selection, liveness, assignment,
and the effective capability. A failed preflight rejects the request before
the canonical Messenger mutation. There is no local-only success mode for a
suspended, offline, or capability-disabled provider target. Native Messenger
targets continue through the existing path.

### 5.4 External identities

External identities are returned through the existing user lookup surface as
read-only projected users with explicit identity metadata:

```json
{
  "uuid": "stable-external-identity-uuid",
  "identity_kind": "external",
  "provider": { "kind": "zulip", "account_uuid": "account-uuid" },
  "display_name": "Provider user",
  "avatar": "urn:image:file-uuid"
}
```

An external identity cannot authenticate, own an external account, or be used
to open an IAM profile or native personal stream unless a later explicit
identity-linking feature is approved.

### 5.5 Realtime events and client caching

The existing project/user event stream gains sanitized full-snapshot events:

- `external_account.created`, `external_account.updated`,
  `external_account.deleted` for the owner;
- `external_chat.created`, `external_chat.updated`, `external_chat.deleted` for
  the owner;
- `external_operation.created`, `external_operation.updated`,
  `external_operation.deleted` for the owner;
- ordinary stream, topic, message, user, file, and read events for projected
  Messenger entities.

The UI stores normalized account, chat, capability, provider, and operation
snapshots in IndexedDB and updates them from full-snapshot events. Cursor expiry
or an epoch-generation mismatch clears those caches and performs a fresh REST
snapshot before notifications are enabled.

## 6. Realm administration API proposal

The approved administration shape separates desired policy from read-only
aggregate health. IAM supplies permissions through the existing introspection
`permissions` list, normally through an assigned role, and the Workspace
backend enforces the action-specific permission for every route. Permission
names follow `service.resource.action`; a role name is never used as the action
segment. Stream role `administrator` is also insufficient because it is
project- and stream-scoped.

| Method | Route                                                       | IAM permission                               | Purpose                                                                            |
| ------ | ----------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| `GET`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.read`    | Read sanitized realm policy for an account kind.                                   |
| `PUT`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.update`  | Update kind-specific policy using `If-Match`; the payload is a dynamic kind model. |
| `GET`  | `/external_provider_health/{kind}`                          | `workspace.external_provider_health.read`    | Read aggregate bridge and account health for an account kind.                      |
| `POST` | `/external_provider_policies/{kind}/actions/suspend/invoke` | `workspace.external_provider_policy.suspend` | Emergency-suspend the account kind realm-wide.                                     |
| `POST` | `/external_provider_policies/{kind}/actions/resume/invoke`  | `workspace.external_provider_policy.resume`  | Resume after validation.                                                           |

For full policy administration and aggregate-health visibility, grant an
administrative IAM role these five exact permissions:

- `workspace.external_provider_policy.read`
- `workspace.external_provider_policy.update`
- `workspace.external_provider_policy.suspend`
- `workspace.external_provider_policy.resume`
- `workspace.external_provider_health.read`

Do not grant `workspace.external_provider_policy.*`: Workspace and the element
manifest use exact action permissions only, and no wildcard permission resource
is provisioned.

Health aggregates counts and latency/queue metrics only. Policy and health
responses never include account email, server URL, chat names, credentials, or
message content. Custom CA input accepts CA certificates only, rejects private
keys, and is versioned.

Runtime bridge identities are exposed separately through the common top-level
`/external_bridge_instances/` administration resource:

| Method | Route                                                               | IAM permission                               | Purpose                                                                                      |
| ------ | ------------------------------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `GET`  | `/external_bridge_instances/`                                       | `workspace.external_bridge_instance.read`    | List sanitized bridge instances across provider kinds.                                       |
| `GET`  | `/external_bridge_instances/{instance_uuid}`                        | `workspace.external_bridge_instance.read`    | Read identity generation, state, capability, heartbeat, certificate expiry, and safe errors. |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | `workspace.external_bridge_instance.suspend` | Immediately block the identity without revoking its generation.                              |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/resume/invoke`  | `workspace.external_bridge_instance.resume`  | Resume a non-revoked suspended identity.                                                     |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/revoke/invoke`  | `workspace.external_bridge_instance.revoke`  | Irreversibly revoke the active certificate generation.                                       |

The resource never returns certificate private material, enrollment secrets,
internal addresses, account identifiers, or message data. Actions return the
updated sanitized instance snapshot. Provider policy and aggregate health stay
separate from runtime identity lifecycle. Enrollment rotation is deliberately
not a Messenger API action: a platform operator rotates the Exordos secret
resource through the manifest/CLI, Core delivers its managed node config to
both backend and bridge, and the backend automatically opens the matching
one-time generation. The Workspace backend receives no Exordos Core credential.

The Workspace element manifest provisions all nine canonical action permission
resources listed above. It does not grant them to any user or existing role.
Operators create a dedicated least-privileged role, bind only the required
permissions to it, and bind that role to an administrator in the Workspace
project. The project on the role binding is the effective IAM scope; deleting
that one role binding removes the administrator's external-provider access
without changing their ordinary Workspace role.

## 7. Private control plane

The browser never calls the bridge or Zulip directly. A narrow internal mTLS
API connects the Workspace backend and the bridge. It carries provisioning and
status only; it never transports message bodies or Messenger events.

The approved runtime surface is a separate private backend listener and
process, `workspace-external-bridge-api`, with its own versioned `/v1/` root.
It binds only the platform-internal interface, requires a valid client
certificate at the TLS socket for every route except first enrollment, and is
not proxied by the public Workspace nginx. First enrollment uses server-
authenticated TLS plus the one-time bootstrap credential because the bridge
does not have a client certificate yet. The existing IAM-authenticated
listeners remain unchanged and expose no internal control or file routes.
Control and file resources share this private OpenAPI listener but remain
separate resource groups.

The listener PKI is backend-owned and stored on a dedicated small persistent
secrets disk attached to the backend VM. The first disk initialization creates
the realm-bound control CA, server key and certificate, and integrity metadata
atomically. A backend root-image replacement mounts the same disk and must fail
closed on a missing, partial, unsafe, or realm-mismatched PKI store instead of
silently generating a new trust root.

First control-CA trust uses the realm-bound HMAC-authenticated bootstrap
producer/consumer pattern; it does not use TOFU or disabled TLS verification.
Before opening the HTTPS control listener,
the bridge calls a separate platform-internal plain-HTTP `GET /ca.crt` endpoint
with a fresh 256-bit lower-case hexadecimal `nonce`, exact expected control
`hostname`, `bridge_instance_uuid`, and positive `enrollment_generation`. The
additional identity fields let a realm with multiple installations select only
the requested unopened generation. The one-time enrollment secret is never
sent on that request. The backend returns the public control CA bytes,
`Content-Length`, and `X-Workspace-CA-HMAC-SHA256`.

Both peers derive the HMAC key and persisted enrollment verifier as
`SHA-256(b"workspace-bridge-enrollment-v1\0" + token_utf8)`. The response HMAC
covers the distinct protocol context
`workspace-external-bridge-control-ca-v1\0`, nonce, NUL, hostname, NUL,
canonical bridge UUID, NUL, base-10 generation without leading zeroes, NUL, and
the exact PEM bytes. The bridge disables redirects, enforces the existing
ten-second timeout, 512-byte request-target limit, and 1 MiB CA limit, compares
the HMAC in constant time, validates the PEM with the TLS parser, and atomically
installs it with file and directory fsync before enabling hostname-verified
TLS. Fetching the public CA does not consume the enrollment generation;
successful CSR signing does.

The bridge creates its client private key only on the bridge persistent disk
and submits a CSR through a one-time authenticated enrollment flow. The backend
signs the CSR with the persistent control CA and returns only the client
certificate and public CA chain; the bridge private key never crosses the
machine boundary. Bootstrap delivery, HMAC first trust, generation consumption,
and certificate rotation are defined by
[`zulip_bridge_control_api_v1.yaml`](zulip_bridge_control_api_v1.yaml).

Enrollment uses a distinct Exordos secret resource for each bridge
installation. The manifest generates random bootstrap material and delivers it
to the backend and bridge through protected Core-managed node configs. The
backend persists only a verifier and generation on the secrets disk.
Successful CSR signing
atomically consumes that generation, so replaying stale node configuration
cannot obtain another certificate. Re-enrollment after bridge state loss
requires an explicit rotation of the Exordos secret resource and a new
generation; a permanent shared enrollment secret is not supported.

Each mTLS client identity represents exactly one bridge installation and one
provider kind. Its certificate is bound to `realm_uuid`, `provider_kind`, and
`bridge_instance_uuid`; the exact certificate claim encoding is defined by the
internal OpenAPI security contract. A Zulip bridge instance uses that identity
for all Zulip accounts assigned to it. The backend still authorizes every
account-scoped request against current desired assignments, so possession of a
valid bridge certificate alone never grants access to arbitrary accounts.
Certificates are not issued per external account or shared across provider
kinds.

Server and client leaf certificates are valid for 30 days and begin automatic
renewal seven days before expiry. The bridge generates a new private key and
CSR locally and authenticates renewal with its still-valid mTLS identity; the
backend signs only a certificate carrying the same approved identity claims.
Old and new client certificates overlap for at most 24 hours to permit a
crash-safe switch. An already expired client certificate cannot use the
renewal route and requires an explicit enrollment-secret rotation. Ordinary
backend or bridge image updates do not rotate certificates by themselves.

The control CA is valid for five years and rotates only through an explicit,
versioned administrative procedure. Rotation creates the new CA beside the
old CA on the secrets disk and publishes a dual-trust bundle for 30 days. Each
active bridge obtains a new leaf under the new CA while authenticating with its
old valid identity. The old CA is retired only after every active bridge
instance has migrated or the overlap window ends; the procedure exposes
migration state and fails closed rather than silently extending an expired
trust root. Automatic annual CA replacement and installation-lifetime CAs are
not supported.

The backend is authoritative for each bridge identity's active certificate
generation and status. Every control and file request checks those values after
TLS authentication and before resource authorization, including requests on a
reused connection. Suspending an identity blocks it immediately and may later
resume the same generation. Revoking an identity is irreversible: the backend
advances its generation, rejects every certificate from the old generation,
and requires a rotated enrollment secret plus a new CSR. Leaf expiry alone is
not the revocation mechanism, and no listener reload or CRL propagation is
required.

The approved control direction is a Workspace-owned internal API polled by the
bridge. The bridge pulls desired generations and encrypted credential
envelopes, then submits heartbeat, capability, progress, and actual-state
reports. Reconciliation is periodic and does not depend on a single RPC
succeeding.

Desired-state synchronization uses a versioned incremental change feed at
`GET /v1/desired-state/changes`. Its opaque cursor is bound to the realm,
provider kind, bridge instance, filter set, and control schema version. Each
response contains an ordered idempotent batch and the next checkpoint. The
bridge applies a batch transactionally to its operational database and only
then persists the checkpoint, so replay after a crash is safe.

Every `external_chat_assignment` full replacement includes a backend-owned
`workspace_projection` mapping. It contains the stream UUID and presentation,
participant provider IDs mapped to Workspace identity UUIDs, and provider topic
IDs mapped to Workspace topic UUIDs. The bridge persists and uses this mapping;
it never invents a Workspace stream, topic, participant, or message UUID.
The assignment also carries the provider discriminator in
`provider_chat.kind`; it is part of the complete canonical replacement rather
than bridge-local configuration. Catalog ingestion accepts a personal direct
chat only with exactly two distinct participants and a group direct chat only
with at least three. An invalid topology is rejected before either the chat or
an `external_chat_assignment` desired resource is persisted.
Provider discovery reports topology without Workspace UUIDs, and the backend
assigns stable UUIDs before publishing the assignment. A locally created topic
on a provider-backed stream advances and publishes the assignment generation
with the new topic mapping but does not queue `topic.upsert`: Zulip materializes
the topic with the first `message.create`. `topic.upsert` is reserved for
renaming a topic that already has a provider message mapping. This also makes
the first outbound message work when `history_depth=new` and no inbound history
has materialized a mapping.

Both incremental upserts and full-snapshot resources carry their effective
`required_capabilities`. Before writing desired state, materializing projection
mappings, or advancing the cursor, the bridge validates every requirement and
requires the resource type, UUID, and generation to match the incremental
change envelope when one is present. Any mismatch fails the whole batch closed.

If a batch contains an unknown `resource_type`, unknown `operation`, or an item
that requires a capability outside the negotiated intersection, the bridge
rolls back the entire batch and does not advance its cursor. It emits a bounded
safe incompatibility report and the backend marks the bridge instance
`incompatible`. The bridge must not skip or quarantine the offending item and
must not commit later items from that batch; after compatibility is restored,
the same batch is replayed from the unchanged cursor.

Heartbeat remains available while an instance is `incompatible`. When a later
valid heartbeat advertises capabilities that cover the blocked batch, the
backend automatically clears `incompatible` and the bridge replays that batch
from its unchanged cursor. This compatibility recovery needs neither a
realm-admin `resume` action nor a full snapshot. It does not override an
administrative suspension or certificate revocation.

The v1 bridge uses ordinary polling, not long polling. After each successful
change-feed response it waits two seconds and issues the next request with the
committed cursor. Only one poll may be outstanding per bridge instance, and an
empty feed response returns immediately with the unchanged checkpoint.

After a network failure, HTTP `429`, or retryable `5xx`, polling uses
exponential backoff with full jitter: a one-second base doubles to a 30-second
cap. HTTP `429` and `503` honor `Retry-After` up to five minutes. The committed
cursor never advances on failure, and heartbeat delivery has its own retry
loop. The first successful feed response resets backoff and restores the normal
two-second interval.

Each change is a replacement record containing `change_uuid`, monotonic
sequence, `resource_type`, `resource_uuid`, `operation`, and resource
generation. An `upsert` carries the complete desired resource snapshot for that
kind, including only encrypted credential envelopes and other bridge-authorized
fields. Applying the same or an older generation is a no-op; a newer generation
replaces the local resource atomically. A `delete` carries only a tombstone with
resource identity and generation, never the deleted secret or prior payload.
JSON patches and fetch-after-change records are not used.

Full recovery begins with `POST /v1/desired-state/snapshots`. It creates a
logical snapshot session and returns an opaque snapshot token, an anchor change
cursor, `snapshot_generation`, and a 15-minute expiry. Snapshot pages use an
opaque page cursor and stable `(resource_type, uuid)` order. The backend does
not hold a PostgreSQL transaction for the lifetime of the session. Instead, it
guarantees that a concurrent create, update, or delete is either represented in
the pages or in the change feed strictly after the anchor. The bridge installs
all pages, then replays changes after the anchor, and commits the resulting
state plus checkpoint atomically. An expired snapshot token requires starting
a new session.

An unknown, expired, mismatched, or no-longer-decodable cursor returns an
explicit reset response rather than silently skipping changes. The bridge then
loads a consistent paginated full desired-state snapshot and installs the
snapshot plus its checkpoint atomically before resuming the incremental feed.
Per-resource ETag polling and backend-initiated WebSocket delivery are not part
of v1.

The reset response is HTTP `410` with
`type=ControlCursorExpiredError`, `error=control_cursor_pruned`, a typed
`reason` of `retention`, `generation_mismatch`, `scope_mismatch`, or
`schema_mismatch`, and the current `snapshot_generation`. It includes
`Cache-Control: no-store`. This mirrors the public Messenger cursor-gap
contract while keeping the private cursor opaque. HTTP `409` remains available
for ordinary state/precondition conflicts, and a reset is never encoded as a
successful empty batch.

Control-plane change records are retained for exactly seven days and then
pruned by age. Retention applies only to the incremental journal: current
external accounts, encrypted credential envelopes, chat assignments, provider
policies, and bridge identity state remain in their authoritative models. A
bridge offline for no more than seven days can normally catch up incrementally;
an older cursor uses the same full-snapshot recovery path.

The bridge returns observed state through
`POST /v1/observed-state/reports` in batches of at most 500 items. Every item
has a client-generated `report_uuid`, resource identity, observed desired
generation, status, progress, and a bounded safe error. Repeating a
`report_uuid` is a no-op, and a stale observed generation cannot overwrite a
newer backend record. The backend persists accepted reports before responding,
so retry after a lost response is safe.

Provider discovery uses the typed `external_chat_catalog` observed resource.
Each catalog item is an `upsert` or `delete` tombstone bound to the current
external-account generation and carries the account, owner, provider, and
default-project identities plus a sanitized chat reference. The backend
rejects ownership or generation mismatches, preserves a stable Workspace chat
UUID for each provider chat key, and continuously assigns new items when the
account uses `selection_mode=all`.

Report batches allow partial acceptance. A successful HTTP `200` response has
one result for every `report_uuid` in request order with status `applied`,
`duplicate`, `stale`, or `rejected` and an optional bounded safe error. A bad
item does not block valid independent items. mTLS failure or an invalid batch
envelope/schema returns the appropriate `4xx` and applies nothing. The bridge
removes only `applied`, `duplicate`, and intentionally discarded `stale`
reports from its durable outbox; a retryable `rejected` item remains queued.

Liveness and capabilities use the separate lightweight
`PUT /v1/bridge-instances/self/heartbeat` endpoint. Heartbeat delivery does not
depend on account/chat work being available and never carries per-account
details, credentials, or message data. The bridge sends it every ten seconds.
Backend receipt time is authoritative; a bridge becomes `degraded` after 30
seconds without a heartbeat and `offline` after 60 seconds. A client timestamp
is diagnostic only. A later valid heartbeat restores health unless the
identity is administratively suspended or revoked.

Every heartbeat operates under the API major selected by its `/v1` URL and
declares the certificate-bound provider kind, named capabilities, and relevant
limits. There is no separate control-schema negotiation. For v1, capability
names include chat catalog, message send/edit/delete/read, rename, and file
transfer. The backend computes the fail-closed intersection and emits only
desired resources and operations supported by that instance. An assignment
requiring a missing capability becomes `unsupported_capability` with a safe
explanation; the backend never attempts optimistic delivery. Image semver
remains diagnostic and is not used as a substitute for capabilities.

The heartbeat wire representation is a JSON object keyed by stable namespaced
capability names. Each value is a descriptor containing the capability
`revision` and a capability-specific `limits` object, for example
`{"messenger.message.edit": {"revision": 1, "limits": {}}}`. The backend
considers only recognized names and intersects numeric or enumerated limits
fail-closed. Unknown capability names are ignored rather than treated as proof
that an operation is supported.

A capability `revision` is a positive, monotonically backward-compatible
integer. Backend requirements declare `min_revision`; a bridge descriptor is
compatible when its revision is equal to or greater than that minimum, subject
to the fail-closed limit intersection. Every higher revision must preserve all
behavior promised by lower revisions. A breaking semantic change uses a new
capability name; an existing revision is never redefined or reused.

The private API is versioned only by the major component in its URL (`/v1`);
there is no minor-version negotiation. Clients must ignore unknown JSON object
fields so additive response metadata can be rolled out independently. Required
behavior is negotiated explicitly through the named capability intersection,
not inferred from an image version or an implicit schema minor. Existing field
meaning and type cannot change within `/v1`; removing a field or making an
optional field required needs a new API major.

Workspace-owned internal resources:

- desired account generation and sanitized settings;
- encrypted credential envelopes tied to account UUID, realm UUID, algorithm,
  key version, and associated data;
- selected chat/project assignments and history depth;
- versioned custom CA bundle;
- bridge capability, heartbeat, progress, and safe-error reports;
- idempotent acknowledgement by command UUID and desired generation.

The bridge publishes its realm-bound encryption public key through
authenticated enrollment. Only the bridge persistent disk holds the
corresponding private master key; Workspace PostgreSQL stores only encrypted
envelopes. The bridge decrypts a credential locally only when it needs to call
the provider. Plaintext credentials never appear in control responses,
Workspace logs, or bridge persistent operational tables.

The exact internal OpenAPI is maintained as a separate contract artifact. It
includes mTLS identity, replay protection, generation monotonicity, request
limits, and error semantics and is covered by the implementation's contract
tests.

## 8. Message and event data plane

The provider data plane is a private bridge-authenticated HTTP API rooted at
`/api/workspace-provider/v1`. PostgreSQL is canonical Messenger storage and the
request-owned RESTAlchemy transaction is the only commit boundary. IMAP, SMTP,
mailboxes, and MIME messages are not part of provider synchronization.

- Backend-to-bridge operations are leased in FIFO order from a durable
  PostgreSQL queue. Expired leases are recoverable and independent bridge
  workers use `SKIP LOCKED` without duplicating a claim.
- Bridge-to-backend events are submitted as bounded atomic batches. Account and
  provider scope are checked from the mTLS identity, event UUIDs are
  deduplicated before canonical mutation, and one rejected item rolls back the
  complete batch.
- Canonical message create, update, delete, and unread invalidation events use
  audience snapshots and bounded broadcast rows. Event-row growth is bounded
  by the logical mutation and affected entities rather than stream membership.
- Terminal provider results are batched, idempotent by `result_uuid`, and return
  one application status per item. A stale lease cannot complete re-leased work.
- Operation UUID, provider account UUID, provider entity ID, provider revision,
  and causal predecessor remain provider-neutral idempotency and ordering data.
  Raw provider payloads remain internal.

Leasing requires the existing control-plane heartbeat to be healthy and no
older than 60 seconds. Known operation kinds are leased only when the current
heartbeat advertises their named capability. Heartbeat remains the independent
`PUT /v1/bridge-instances/self/heartbeat` operation and is not duplicated in
the data-plane API.

Ordering uses causal lanes scoped by external account and chat/entity.
Conflicting operations for one entity execute serially, while independent chats
can progress in parallel. The bridge emits a successful result only after the
provider confirms its commit. Workspace, provider HTTP, control, and bridge-local hops
deduplicate the same operation UUID. Zulip message creation is handled by the
provider-specific reconciliation policy below because Zulip does not use the
client `local_id` as an idempotency key.

After an ambiguous Zulip send result, the bridge first performs delayed,
repeated history reconciliation. It queries the exact target conversation
newest-first, narrows to the current external account as sender, requests raw
Markdown, and compares the target, canonical payload, attachments, and bounded
operation time window. One or more exact matches confirm the original send; the
bridge selects the candidate nearest to the first send attempt, breaking ties
by the lowest numeric provider message ID, and does not resend. The
number of candidates is retained only as reconciliation evidence. This
intentionally treats identical messages from the same account in the same
conversation and bounded time window as equivalent, so two user operations may
converge to one Zulip message. If repeated checks find no match, the bridge may
resend automatically only once. Unavailable history or a second ambiguous
outcome require `manual_reconciliation_required`; no further automatic resend
is allowed.

The exact wire contract is specified in
[`workspace_provider_api_v1.yaml`](workspace_provider_api_v1.yaml). The legacy
mail protocol document is retained only as superseded design history and is not
an implementation input for the PostgreSQL-canonical release.

## 9. Binary file-transfer plane

The bridge cannot use MIME attachments and cannot receive bucket-wide S3
credentials. The approved binary-transfer boundary is a separate internal mTLS
file API with short-lived, single-object presigned URLs.

Incoming provider file flow:

1. The bridge requests an allocation with external account UUID, chat UUID,
   operation UUID, name, size, content type, and expected hash.
2. The backend validates the assignment and returns a short-lived presigned URL
   that permits `PUT` for exactly one pending object.
3. The bridge uploads bytes and calls finalize.
4. The backend verifies size and hash, atomically creates the JSON sidecar and
   current ACL, commits the file projection, and returns a Workspace URN.

Outgoing Workspace file flow:

1. The bridge submits the authorized Workspace URN, external account UUID,
   chat UUID, and operation UUID.
2. The backend recomputes current access from the chat/stream assignment and
   returns a short-lived presigned `GET` URL for exactly that object.
3. The bridge downloads the bytes and copies them into provider storage.

The bridge cannot create or modify sidecars and ACLs, list the bucket, or reuse
a URL for another object. The backend cleans expired partial allocations and
immediately purges provider-owned copied files when a projection is removed.

Presigned `PUT` and `GET` URLs expire after five minutes. Allocation and
finalization require declared size, content type, and SHA-256; finalize fails
closed on any mismatch. Allocation/finalize are idempotent for the same
operation and file identity, and expired unfinished allocations are removed by
the backend cleanup worker.

This service authorizes binary file bytes only; the bytes flow directly between
the bridge and object storage. Message text, metadata, and events use the
private Provider HTTP API, and the resulting message contains only the returned
URN.

## 10. Storage and deployment boundary

### Workspace backend

- New realm-scoped external-account, encrypted-credential, chat-assignment,
  provider-mapping, desired/actual state, and external-operation projections.
- A stable realm/installation UUID. Because one backend database belongs to one
  realm, uniqueness is `(owner_user_uuid, provider_kind)` within that database.
- A project-move coordinator that writes the canonical old/new project journal
  transitions while preserving entity UUIDs.
- IAM lifecycle reconciliation in addition to current lazy user discovery.
- Private mTLS control, provider-data, and file endpoints.
- Public serializers that preserve `provider` and `delivery` for streams,
  topics, messages, files, and secondary projections.

### `workspace-zulip-bridge` element

The bridge is a new repository and an independent Exordos element with one VM,
a replaceable root disk, and one persistent data disk. No public load-balancer
route is exposed.

Persistent state includes:

- realm-bound encryption key versions and control-plane identity;
- durable outbox/inbox deduplication and provider HTTP lease state;
- Zulip queue IDs and cursors;
- provider-to-Workspace mappings and backfill progress;
- scheduler leases and actual-state reports.

For the target profile of 1,000 accounts, 50,000 chats, 50 million projected
messages, and 100 messages/second, the operational store should be a local
crash-safe PostgreSQL instance on the persistent disk. It remains secondary to
canonical Workspace PostgreSQL and S3 state.

The bridge uses a fair scheduler with strict live priority, then retryable
outbox work, then fair per-account backfill. Zulip rate limits are authoritative.

### Safe updates

- Workspace and bridge elements are updated in place. They are not
  uninstalled or freshly redeployed on the working installation.
- Every development image uses a new immutable version.
- Persistent disk identity, node derivation, disk order, and data labels remain
  stable across upgrades.
- Bootstrap fails closed on missing/partial key state or realm mismatch.
- Queues, mappings, and the local database must recover after a hard VM stop,
  because current Core image replacement does not guarantee graceful shutdown.
- Activation is staged and capability-gated: backend APIs and the bridge are
  deployed with the provider kind disabled, then enrollment, healthy heartbeat,
  and the required capability intersection are verified before a realm admin
  enables Zulip.
- Rollback suspends provider synchronization and preserves durable queues and
  projections for recovery. It does not uninstall an element, erase persistent
  state, or interrupt native Workspace messaging.

## 11. UI boundary

- Replace the hidden legacy external-account feature; do not make it visible or
  add compatibility calls to its old endpoints.
- Add a Messenger settings page for Zulip credentials, chat selection or `all`,
  history depth, project assignment, progress, `Disconnect`, and destructive
  `Delete`.
- Preserve provider metadata through stream, topic, message, file, and cached
  secondary projections.
- Use compact interactive provider badges with account/status popovers and an
  original link where available.
- Centralize capability decisions across composer, edit/delete, files,
  rename/move, replies, and loss preflight.
- Keep local browser transport state (`sending`/`failed`/`sent`) separate from
  authoritative external-operation state (`pending`/`delivered`/`failed`).
- Never send provider credentials to IndexedDB, logs, analytics, or the bridge
  from the browser.

## 12. Technical decomposition

1. **Contract foundation**: approve this public boundary, the realm-admin
   authorization primitive, the private control and provider-data OpenAPI, and
   the encrypted-envelope schema.
2. **Workspace data model**: replace legacy project-scoped account residue;
   implement encrypted credentials, assignments, mappings, operation state,
   IAM lifecycle, and project-move coordination.
3. **Workspace protocol boundary**: add the mTLS provider identity, durable
   provider HTTP outbox/ingress, internal control/file services, and exports
   required by the bridge element.
4. **Projection and realtime parity**: populate provider/delivery metadata,
   external identities, full-snapshot events, cache-reset behavior, and
   notification gating.
5. **Bridge element foundation**: create the separate repository, manifest,
   persistent bootstrap, crash-safe operational PostgreSQL, mTLS enrollment,
   and reconciliation loop.
6. **Zulip connector**: account validation, catalog, live queue, durable outbox,
   newest-first backfill, conversion, conflict rules, and rate-limit-aware fair
   scheduling.
7. **UI**: normalized cache-first domain, connection wizard, selection/project
   flows, badges/popovers, external identities, capabilities, preflight, and
   retry/discard behavior.
8. **Acceptance**: contract tests, real provider HTTP and Zulip integration,
   file ACL tests, crash/root-replacement recovery, load tests, safe element
   updates, and full visible Playwright acceptance.

## 13. Acceptance matrix

The feature is not complete until all of the following are verified:

- account create/reconnect/disconnect/delete and IAM deactivate/delete;
- one-account-per-provider uniqueness and write-only credentials;
- explicit selection, dynamic `all`, all history-depth modes, and new-chat
  assignment;
- channel/topic, personal DM, and group DM mapping;
- create/edit/delete/read/mentions/replies/quotes/Markdown/links/files/images
  in both directions;
- rename in both directions when advertised by capabilities;
- loss fallback, original link, and outbound confirmation;
- newest-first backfill, live-first scheduler, notification gating, and p95
  latency target;
- retry/backoff, 24-hour expiry, discard, crash recovery, deduplication, and
  conflict/delete ordering;
- project move with stable UUID/history/read state and correct old/new project
  events;
- deselection/access-loss immediate cleanup of projections and copied files;
- provider metadata parity in REST, realtime, IndexedDB, and secondary views;
- external identity and `urn:user:*` behavior without IAM impersonation;
- custom CA validation, hostname verification, mTLS control, least-privileged
  provider API access, and absence of global S3 access;
- owner-only detail and aggregate-only realm-admin health;
- 1,000 accounts / 50,000 chats / 50 million messages / 100 messages per second
  load profile;
- safe independent bridge update with persistent state preserved;
- visible Playwright acceptance through the normal `cassi` Workspace account.

## 14. Versioned contract artifacts and remaining gates

The high-level product and API boundary is approved. The versioned internal
control OpenAPI, provider HTTP OpenAPI, and internal file API are implementation
inputs that require contract, compatibility, and runtime validation before
realm enablement.

These completed artifacts are implementation inputs, not remaining release
gates. Realm enablement still requires every outstanding scenario in
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md), including
explicit policy authorization, real bidirectional Workspace provider HTTP/file
and Zulip 12.1.1 traffic, certificate rotation, recovery and target-load
checks, and full visible Playwright acceptance.
