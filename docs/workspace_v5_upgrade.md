# Automatic upgrade to native Zulip v5

Update the Workspace element with its normal command:

```shell
exordos ee update workspace
```

When `workspace_ui` is installed, Core requests its normal repository upgrade
to the released UI 0.9.2 if the existing client is older. That release accepts
v5's provider source DTOs, including personal conversations without a numeric
channel ID. A compatible newer UI stays installed; a backend-only installation
does not gain a UI element. The compatible release must be available in the
configured repository. This happens within the backend upgrade and requires no
separate UI command.

Keep the legacy `workspace_zulip_bridge` element installed until the upgrade
finishes. Core provisions a one-time exporter on that bridge. The exporter
unwraps existing account credentials locally and encrypts the handoff for the
new backend; the bridge's private key never leaves its VM. The backend verifies
all handoffs before retiring the old credential tables. A missing, changed or
invalid handoff stops the migration without discarding those credentials.
Already installed v5 databases bypass this path and do not require a bridge.

Bootstrap serializes concurrent starts and drains API, worker and realtime
writers before applying schema changes. Services remain unavailable during the
conversion. Their startup timeout allows the database conversion and initial
provider observation to finish. Repeated installation steps resume from durable
task records; normal services start only after conversion succeeds.
The data-conversion command also disables statement, transaction and idle-in-
transaction timeouts for its own database connection, so a resumed conversion
can exceed normal worker limits without changing the installed service settings.

The conversion brings legacy provider identities and their foreign keys into
the existing v5 UUID scheme. Per-account copies of the same conversation or
topic become one conversation or topic. It retains native outgoing messages,
provider delivery IDs, drafts, files, folder membership and message state.
Internal structured references, Markdown links and nested forward snapshots
follow the converted identities. Imported object UUIDs and old bookmarked URLs
can change. Existing v5 UUIDs remain unchanged.

The installer uses the existing v5 user, channel and history import pipeline to
observe the provider before reconciling converted records. Zulip and the
accounts' credentials must be available. It reuses legacy attachment rows and
objects only in the same project and conversation, using their deterministic
identity or a verified content hash and size. Provider flags and content are
then reconciled through the normal v5 behavior; conversion does not resend old
Workspace messages to Zulip. Derived counters and folder snapshots are rebuilt
before reopening services. After observation and reconciliation complete, converted
accounts receive the existing completed-backfill state. Normal startup then uses
its usual catch-up instead of scheduling another complete history import.

Legacy personal conversations recover a missing peer reference from the
owning reader's observed Zulip conversation before membership reconciliation.
Converted channels also adopt their observed invite-only setting.

Legacy messages incorrectly attributed to an importing account adopt the author
observed in Zulip. Genuine acknowledged outgoing messages keep their delivery
identity. The database table `workspace_zulip_v5_legacy_archive` retains original
authors, payloads, UUIDs, placements and the conversion mapping after success;
captured account settings exclude API keys. This archive is installation data
and is not part of the application runtime.

Take and rehearse a coordinated database and object-storage rollback point
before updating, including Core configuration and the old bridge's database and
private runtime identity. Retain disk checkpoints outside managed writable
volumes: platform removal can wipe a volume before unlinking it. A hard link to
that writable volume is not an independent backup.
Reverting only the application image cannot undo this schema
and identity conversion. The old Messenger v2 migration procedures describe
older releases and are not additional manual steps for this upgrade.

After a successful upgrade, the obsolete bridge element may be removed through
the platform. Verify fresh messages and attachments in both directions,
WebSocket delivery during backlog processing, and representative application
flows before considering the installation accepted.

Realtime message tasks recheck local dependencies and delivery confirmation
after 250 ms. A worker that skips its second execution lock releases only its
own unsent attempt; an unknown HTTP result still waits for local echo rather
than repeating the POST. Network errors and provider rate limits retain their
longer retry interval.

Deleting an acknowledged native outgoing message records the Zulip route in
the existing domain outbox transaction, before local tombstone cleanup. The
realtime task retains that snapshot across restarts, uses the original sender's
account, and cancels if that account has been rebound. Repeating a DELETE after
a lost response accepts an already absent provider message; permission failures
remain errors. Existing deletion events without a captured route are ignored.
