# Mail-Backed Workspace Messenger Architecture

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
  use service credentials over the platform-internal network and are not
  exposed to browsers or end users.
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

The mail root image has an independent manifest input. Backend-only releases
pin `workspace_mail_image` to the currently deployed immutable mail image;
releases that intentionally change the mail runtime omit that override and use
the newly built `workspace-mail` image. The persistent data disk is unchanged
in either case.

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
