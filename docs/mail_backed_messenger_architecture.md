# Mail-Backed Workspace Messenger Architecture

> **Superseded design history — not the current runtime.** Workspace Messenger
> now uses PostgreSQL as its canonical store and does not require SMTP, IMAP,
> Exim, Dovecot, or Maildir at runtime. See the current
> [`architecture.md`](architecture.md), public
> [`workspace_api.md`](workspace_api.md), and controlled
> [`postgresql_canonical_messenger_migration.md`](postgresql_canonical_messenger_migration.md)
> runbook. The material below is retained only to explain the former design and
> migration source.

This document records the confirmed requirements and target architecture for
the native Workspace messenger. It makes the local SMTP/IMAP service canonical
for the messenger domain while retaining PostgreSQL only as a rebuildable read
projection and client-settings store. The public contract in
[`workspace_api.md`](workspace_api.md) and the existing Workspace UI remain
unchanged.

## Confirmed Requirements

- Preserve the complete Messenger API: direct chats, group streams, topics,
  bindings, folders, messages, reactions, edit/delete actions, per-user
  read/pin/star state, durable catch-up, and realtime delivery.
- Do not migrate existing PostgreSQL data or implement a legacy compatibility
  layer.
- PostgreSQL may store fast read projections, search indexes, counters, events,
  and client settings, but message correctness must not depend on it. The
  projection must be rebuildable from the IMAP journal.
- Use SMTP for actual message delivery and IMAP as the durable source of truth
  for messages and messenger metadata.
- Deploy Exim4, Dovecot, and Maildir on a dedicated VM inside the Workspace
  element. Keep backend and UI services on their existing VM. IMAP and SMTP
  use service credentials over CA-verified STARTTLS on the platform-internal
  network and are not exposed to browsers or end users.
- Obtain users and authentication from Exordos Core IAM. Create technical
  mailboxes automatically for IAM users; users do not manage separate mail
  credentials.
- Keep file bytes, file metadata, and file access data in S3. Messenger message
  bodies contain only the existing `urn:file`, `urn:image`, and `urn:video`
  references. SMTP messages must not contain binary MIME attachments.
- Keep the existing Workspace web UI. The browser continues to use the
  IAM-authenticated Messenger REST and WebSocket APIs and has no knowledge of
  SMTP or IMAP.
- Do not implement external providers, ordinary email, or calendar features.
- Preserve the existing provenance fields. Future external providers will
  exchange messages through SMTP/IMAP rather than a separate provider API.

## Runtime Topology

```text
Workspace element
├── backend VM
│   ├── workspace-messenger-api
│   ├── workspace-messenger-events
│   └── workspace-messenger-worker
└── mail VM
    ├── Exim4 (authenticated internal SMTP submission and local delivery)
    ├── Dovecot (authenticated internal IMAP)
    └── persistent Maildir under /var/lib/workspace/messenger/mail

External element dependencies
├── Exordos Core IAM (identity and bearer-token authentication)
├── DBaaS (rebuildable read projection and client settings)
└── S3aaS (file objects and JSON sidecar metadata)
```

Core local DNS resolves the stable `workspace-mail` service name to the mail
node. Its second disk persists Maildir independently of the replaceable root
image. Exim4 and Dovecot must be ready before the Messenger API and realtime
services report readiness. DBaaS is secondary: an empty replacement database
must be reconstructible by replaying the IMAP journal.

The first mail bootstrap mounts the persistent data disk before creating a
Workspace-owned CA and a leaf certificate whose only DNS SAN is
`workspace-mail.<core_local_domain>`. The persistent TLS store is
`<persistent-mount>/workspace-mail-pki/v1`. It contains the stable
CA key and certificate, immutable versioned leaf directories, an atomic
`current` symlink, and realm metadata. The metadata binds the store to the
configured hostname and stable UUID of the dedicated Core password resource. A
cloned disk, changed hostname or realm UUID, missing file, unsafe type,
key/certificate mismatch, invalid CA constraints or chain, or partial generation
fails closed instead of silently creating a new CA. For legitimate regular
files and directories, bootstrap reasserts the required owner and mode before
cryptographic validation, which supports safe backup restoration without
accepting path/type substitution. Before creating or changing the store,
bootstrap uses no-follow `lstat` checks on the root-owned persistent mount and
its direct `workspace-mail-pki` child. A symlinked or non-directory parent or
store, a group/world-writable parent, or a store on another filesystem is
rejected without changing the referenced target. Only after those confinement
checks may bootstrap create or normalize the store. The CA and private keys
never enter the element manifest, Core config resources, image artifacts,
backend node, or logs. Subsequent mail root-image replacements attach the same
data disk, validate the complete store, and atomically recreate the
`/etc/workspace/tls` runtime files.

The backend cannot read guest files through the current element or config API.
The mail node therefore serves only the public CA on its internal port 21085.
It authenticates the exact response bytes with HMAC-SHA256 using the existing
random `workspace_mail_ca_bootstrap_secret` and a protocol-specific
`workspace-mail-ca-v1` context. This secret is independent from rotatable
SMTP/IMAP credentials and is delivered in a separate config. Every request
includes a fresh 256-bit nonce and the expected mail hostname; both values are
covered by the HMAC together with the CA bytes, so a captured response cannot
be replayed for a new request or another realm. The dedicated non-login
`workspace-pki` user receives only that config, the public CA, and public realm
metadata; it cannot traverse the persistent private-key directories. The
credential is never sent to the CA endpoint. An unauthenticated, replayed,
redirected, truncated, oversized, or modified response is rejected and backend
readiness remains blocked. The single-request server has a bounded listen queue
and per-connection timeout. The current element schema exposes no per-service
source-address ACL, so the endpoint binds to the internal node network; it
contains public material only, and response authenticity does not rely on
network secrecy.

The current universal-agent config renderer creates its destination before
applying the resource mode and does not publish through an atomic temporary
file. Both Workspace images therefore install a systemd drop-in for the actual
`exordos-universal-agent.service` with `UMask=0077` before first config
delivery, and validate the unit with `systemd-analyze verify`. Final resource
modes remain explicit and restrictive. This image-level protection should be
retained until Core provides an atomic, mode-safe config renderer.

Dovecot and Exim require TLS before accepting PLAIN authentication. Backend
SMTP and IMAP clients use the Core local DNS name and verify both the stable CA
and hostname. Backend and mail root images can be updated independently after
the first coordinated deployment because the mail trust root belongs to the
persistent mail disk. Within 30 days of leaf expiry, bootstrap writes a complete
new versioned leaf generation signed by the same CA and atomically changes the
`current` symlink. Corrupt state is never treated as an expiry renewal.
Superseded leaf directories are retained for seven days for rollback and then
their private keys are pruned. The persistent TLS store is part of the mail
data-disk backup scope and must be encrypted and access-controlled as CA key
material; restoring Maildir without the matching TLS store is incomplete.
Deliberate CA rotation is a separate maintenance operation and requires an
overlapping trust rollout: distribute old and new public CAs to the backend,
switch the mail leaf, verify all clients, then remove the old CA and key after
the rollback window. Deleting the persistent TLS store is not a supported
renewal procedure.

The liveness healthcheck verifies SMTP and IMAP STARTTLS, service
authentication, and protocol `NOOP`; it does not require an already-created
technical mailbox. Mailbox usability is tested separately with the canonical
`Workspace/State` and `Workspace/Events` paths. Bootstrap explicitly creates
`/run/workspace` as `root:root` mode `0755` before the
`workspace:workspace` mode `0750` Dovecot index directory. This avoids a
restrictive agent umask creating an untraversable intermediate parent, which
would make every mailbox selection fail with `NOPERM` and trigger repeated
bootstrap service restarts.

The mail root image has an independent manifest input. Backend-only releases
pin `workspace_mail_image` to the currently deployed immutable mail image;
releases that intentionally change the mail runtime omit that override and use
the newly built `workspace-mail` image. The persistent data disk is unchanged
in either case. The first release that introduces persistent PKI and the
authenticated CA endpoint must update backend and mail together. Later releases
may pin either compatible image independently.

### Staged migration from backend-local mail

The first migration build must preserve the existing backend node and data
disk while provisioning the separate mail node in parallel. Build that
manifest from the repository root with the exact current live backend image
URN:

```bash
exordos build . --force --output-dir build/mail-migration-stage1 \
  --manifest-var mail_migration_stage1=true \
  --manifest-var workspace_backend_image=urn:images:<CURRENT_LIVE_BACKEND_IMAGE_UUID>
```

Stage 1 does not deliver the backend PKI bootstrap config and therefore does
not wait for the new remote CA. Its pinned backend continues the existing local
plain-mail readiness, bootstrap, and restart path while the remote mail node
initializes independently.

During final STARTTLS cutover, `workspace_backend_config` and the separate
backend PKI config may be delivered in either order. If the STARTTLS workspace
config arrives first, its on-change handler exits successfully without clearing
readiness until both the PKI config and public CA are present. Delivery of the
PKI config then performs authenticated CA synchronization and the full
healthcheck/bootstrap/restart sequence. The stage-1 plain-mail config does not
take this deferred path.

In this mode the backend remains a `root` plus 20 GiB `data` multi-disk node,
uses the pinned live root image, keeps the existing `workspace_backend_config`
SMTP/IMAP fields on localhost, and retains the existing `workspace-bootstrap`
service from the live installation. Stage 1 deliberately declares no new
backend-only mail config or bootstrap resource: the pinned image does not need
to contain `/usr/local/bin/workspace-mail-bootstrap`, and the existing Exim4,
Dovecot, and Maildir continue serving backend traffic. The new
`workspace-mail` node is provisioned separately with its own root plus 20 GiB
data disk, config, and bootstrap service so its Maildir can be populated and
verified without switching backend traffic. Never run stage 1 without
`workspace_backend_image` set to the inspected live image URN.

After the Maildir copy and independent validation are complete, switch the
backend to the newly built root image and remote mail service while retaining
the legacy 20 GiB backend data disk:

```bash
exordos build . --force --output-dir build/mail-migration-cutover \
  --manifest-var mail_migration_cutover_keep_legacy_disk=true
```

This cutover mode uses the new backend root, remote SMTP/IMAP host and SMTP
credentials, and no backend-local mail config or service. It deliberately
keeps the image-less legacy data disk attached for acceptance and rollback
evidence. `mail_migration_stage1` and
`mail_migration_cutover_keep_legacy_disk` are mutually exclusive; a build that
sets both is rejected.

The image bootstrap must not probe SMTP or IMAP. During a root-image
replacement the universal agent can start the bootstrap service while the
previous `workspace.conf` is still present and before the new config resource
has been applied. Making bootstrap depend on mail from that stale config forms
an activation cycle. Mail readiness is therefore checked by
`workspace-reload-config` only after the config resource has written the new
file and before bootstrap readiness is restored and the backend services are
restarted. Stage 1 retains the existing `workspace_backend_config` resource
identity. Cutover and later manifests use
`workspace_backend_config_remote_mail_v1`, because the current platform does
not reliably propagate a changed body for the existing config identity during
this node replacement; the new identity forces creation and delivery of the
remote-mail configuration.

The backend bootstrap oneshot also exits successfully without publishing its
readiness marker when `workspace.conf` is not present yet. Backend API and
worker services run through `workspace-wait-ready`; the wait happens inside
their long-lived managed process rather than in an agent `before` action. This
lets the universal agent activate the node and receive the config action while
ensuring the real application commands cannot start before config validation,
mail readiness, and migrations complete. These wrapped services use new
`*_remote_mail_v1` manifest resource identities during cutover and later
deployments so the platform cannot rehydrate their old blocking `before`
actions. Stage 1 retains the original service identities and commands supplied
by its pinned backend image.

Only after cutover acceptance may the cleanup manifest omit both migration
flags:

```bash
exordos build . --force --output-dir build/separate-mail-final
```

The default cleanup manifest returns the backend to a replaceable root-only
node and releases the legacy backend data disk while continuing to use
`workspace-mail` through Core local DNS. Building a manifest does not authorize
deployment; follow the normal protected Workspace update, acceptance, backup,
and data-preservation procedure separately.

## Host Backup Contract

An application-consistent host backup must resolve exactly one domain for each
of these roles: Exordos Core, DBaaS control plane, MetaPaaS control plane,
Workspace DBaaS data plane, Workspace S3aaS data plane, Workspace backend, and
Workspace mail. It must fail closed when a role is missing or ambiguous and
must reject explicitly protected domains.

The coordinated snapshot contains these disk scopes:

- the persistent `vdb` disk for each platform dependency;
- the replaceable backend `vda` root disk;
- both the mail VM `vda` root disk and its persistent Maildir `vdb` disk.

The mail `vdb` snapshot includes the Workspace CA private key and current leaf
private key. Backup transport and retention must therefore provide encryption
and access controls suitable for signing keys. Restore validation must confirm
the realm metadata, CA key/certificate match, current leaf chain and hostname,
and file ownership/modes before mail services start.

Before creating external snapshots, stop the backend universal agent and all
Workspace API, messenger API, realtime, and worker services so reconciliation
cannot restart writers. Drain the mail queue and stop Exim4 and Dovecot on the
mail VM, then stop the DBaaS and S3aaS data services and their control-plane
dependencies. After snapshot creation, start dependencies first, then mail,
then the backend services and universal agent. Readiness checks must cover the
backend APIs, the universal agent, Exim4, Dovecot, PostgreSQL, and S3 storage
before writers are considered restored.

The backup manifest must contain seven domain XML files, eight qcow disk
entries, and one JSON manifest. After the repository write, commit every
external overlay back to its base, remove snapshot metadata and overlays, and
repeat the platform health checks.

Retention selects snapshots by the stable backup tag and groups them by host
only before applying `keep-last`. Do not include paths or the unique per-run tag
in the grouping: root-image replacements change paths, and a unique run tag
would otherwise put every snapshot into its own retention group.

## Identity And Mailbox Provisioning

Each IAM project and user pair maps to a deterministic internal mailbox. The
address is a transport identifier only and is never returned as the user's
display email. A service-only credential may impersonate these mailboxes over
internal IMAP; the browser continues to authenticate only with IAM.

Mailbox provisioning is idempotent and happens on first authenticated use.
IAM continues to authenticate the actor and provide the project/user scope;
mailbox identities are deterministic internal addresses derived from that
scope.

## IMAP Data Layout

The mail store contains two kinds of durable data:

1. SMTP-delivered message envelopes in each recipient's mailbox.
2. Append-only JSON records in a hidden project IMAP mailbox for shared state,
   per-user state mutations, realtime events, and tombstones.

An SMTP message has one logical UUID across all recipient mailboxes. Identity
comes from `X-Workspace-*` headers rather than an IMAP UID, because UIDs are
mailbox-local. Required headers include the message, project, stream, topic,
and author UUIDs plus a schema version. The text body contains the canonical
markdown payload verbatim, including Workspace URNs.

The project journal is reduced into PostgreSQL and in-memory projections for API
reads. These projections are disposable and must be fully rebuildable from IMAP
after database loss or process restart. Journal records are idempotent by
operation UUID.

Suggested hidden mailboxes are:

| Mailbox | Ownership | Purpose |
| --- | --- | --- |
| `INBOX` | user | SMTP-delivered logical messages |
| `Workspace/State` | project service mailbox | streams, topics, bindings, shared folders, reactions, mutations, and tombstones |
| `Workspace/Events/<user_uuid>` | project service mailbox | Durable per-user realtime events and public epoch cursors |

The slash hierarchy matches the configured Dovecot namespace separator. Dots
are not used as mailbox separators because Dovecot 2.4 rejects them for this
Maildir namespace.

The final names are internal and may change without affecting the REST
contract.

## Contract Mapping

| Messenger concept | Mail-backed representation |
| --- | --- |
| Message create | One SMTP message delivered to the author and all current recipients |
| Stable message identity | Client UUID in `Message-ID` and `X-Workspace-Message-UUID` |
| Markdown and file references | Plain UTF-8 body containing the unchanged markdown and URNs |
| Read state | Per-user project-journal mutation reduced into the SQL client-state projection |
| Starred state | Per-user project-journal mutation when exposed by the public API |
| Pinned state | Per-user project-journal mutation when exposed by the public API |
| Edit | Idempotent project-journal mutation applied to every visible projection |
| Delete | Immediate IMAP deletion and expunge of every participant copy, plus a content-free tombstone and `message.deleted` event |
| Reactions | Project-journal records reduced into the existing aggregate response |
| Streams/topics/bindings | Project-journal records with the existing UUIDs, roles, and settings |
| User folders/items | User-scoped journal records; these organize chats and are not ordinary email folders |
| Realtime event | Existing public event shape derived from an authorized project-journal operation |
| `epoch_version` | Monotonic project-journal UID, guarded by UIDVALIDITY; user-visible cursors may contain gaps |
| Live WebSocket | Canonical journal catch-up, with SQL notifications used only as a wake-up optimization |
| REST catch-up | Project-journal UID search strictly after the supplied epoch, filtered to the current user |

Direct chats are ordinary streams and use the same bindings, participant roles,
topics, events, folders, and file ACL rules as group streams. Their only extra
invariants are `kind = direct`, exactly two distinct participants, and a UUID
that is deterministic for the project and sorted IAM user pair. Repeated UI
requests therefore cannot create duplicate conversations.

## Files In S3

File bytes remain ordinary private S3 objects. File metadata create, update,
and delete operations are canonical project-journal records and are also
materialized in a JSON sidecar for object-storage recovery. They include the
fields required by `/v1/files/`, the owning stream UUID, uploader UUID, object
key, hash, content type, size, timestamps, and deletion state. Access is
evaluated from the current stream membership reconstructed from the IMAP
project journal; the SQL access table is only a cache.

Message creation stores only the file URN in markdown. Upload, download,
ownership checks, and the existing 50 MiB request limit remain part of the
Messenger API. Browser E2E must prove that the raw SMTP message has no binary
MIME attachment.

## Delivery And Recovery

SMTP acceptance and IMAP state mutation cannot be one distributed transaction.
Every API write therefore has a stable operation UUID and idempotent replay
semantics. Canonical mail delivery or journal append completes before the SQL
projection is updated. Full journal replay reconstructs streams, bindings,
topics, messages, reactions, folders, file metadata, and per-user message
state after projection loss. API retries must not create duplicate logical
messages or duplicate visible events.

Message deletion is not recoverable. The tombstone retains only identifiers
required for idempotency and the existing delete-event contract; it must not
retain message content or file URNs.

The user-facing REST and WebSocket shapes remain unchanged. IMAP UIDVALIDITY
changes, unavailable mail services, malformed internal messages, and partial
recipient delivery must fail explicitly and be covered by integration tests.

## Required Verification

- Unit tests for address/header codecs, journal reduction, idempotency,
  pagination, flags, reactions, tombstones, S3 sidecars, and IAM provisioning.
- Integration tests against real Exim4, Dovecot, Maildir, and S3-compatible
  storage, including restart/rebuild and partial-failure recovery.
- Contract tests for every endpoint and realtime payload documented in
  `workspace_api.md`.
- A visible-browser E2E with two IAM accounts that covers direct and group
  messaging, realtime delivery, read state, edit/delete, reactions, reload,
  reconnect/catch-up, and S3-backed file/image/video URNs. The deployed test
  realm uses dedicated owner, administrator, moderator, member, guest, and
  outsider accounts so every stream role and negative access path is covered.
- Backend-side evidence that delivery used SMTP, history was reconstructed from
  IMAP, and raw messages contained no file MIME parts.
- A clean deployment of the complete platform and required test infrastructure
  on the designated disposable stand, followed by the visible-browser E2E as a
  real user. Local tests support this gate but do not replace it.
