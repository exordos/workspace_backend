# Updating native Workspace installations

This version targets native Zulip v5 installations. The one-time legacy
conversion and bridge compatibility path have been retired.
Uninstall the retired `workspace_zulip_bridge` element before updating so it no
longer imports the removed enrollment secret.

Update the Workspace element through its normal command:

```shell
exordos ee update workspace
```

Use Workspace UI 0.9.2 or later with the native backend. UI upgrades are no
longer requested by a one-time Core migration helper.

The element no longer provisions the Core migration configuration, credential
handoff key, legacy bridge enrollment secret and its export, bridge exporter
or backend conversion commands. Core removes the
managed `/etc/exordos_core/workspace-v5-upgrade.ini` configuration when applying
the element update. The previously downloaded
`/usr/local/libexec/workspace-v5-core-prepare.py` is outside Core's managed
configuration files and may remain unused on an existing Core node; this version
neither downloads nor executes it.

The published database migration chain remains intact for fresh schema creation.
Existing native identities and the `workspace_zulip_v5_legacy_archive` recovery
archive are retained. Ordinary bootstrap still serializes concurrent starts,
drains database writers and applies schema migrations before starting services.

Migration 0251 introduces the message-recovery fingerprint cache. It does not
rehash retained history inside the migration: the next bounded recovery scan
populates the cache as its processing tasks succeed. Existing import task
payloads remain supported. Keep the separate realtime and background workers.
For a schema rollback, stop the new workers and use the migration's downgrade
before starting the older image. The downgrade turns pending scans back into
legacy full-backfill work, removes the snapshot-only task fields, restores the
previous flag view and drops the cache tables. It retains canonical messages and
source message content. A later upgrade rebuilds the cache lazily.

Keep coordinated database and object-storage backups. Reverting an application
image cannot undo the schema and identity conversion performed by older
releases. Verify messaging, attachments, WebSocket delivery and representative
application flows after updating.

Realtime message tasks recheck local dependencies and delivery confirmation
after 250 ms. A worker that skips its second execution lock releases only its
own unsent attempt; an unknown HTTP result still waits for local echo rather
than repeating the POST. Network errors and provider rate limits retain their
longer retry interval. The realtime dispatcher checks new Workspace messages
before processing one flag change and one reaction change per pass, so bulk
state reconciliation does not hold a message behind a large batch.

Deleting an acknowledged native outgoing message records the Zulip route in
the existing domain outbox transaction, before local tombstone cleanup. The
realtime task retains that snapshot across restarts, uses the original sender's
account, and cancels if that account has been rebound. Repeating a DELETE after
a lost response accepts an already absent provider message; permission failures
remain errors. Existing deletion events without a captured route are ignored.
