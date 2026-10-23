# Provider identities

Messenger provider instances are stored in `messenger_providers` with `uuid`
and unique `hostname`. Hostnames are normalized by URL parsing; scheme, port,
and path are not part of the instance identity. Existing deterministic
Workspace user, channel, topic, and message UUIDs are preserved.

Provider-originated domain models carry `source_name`, nullable `provider_uuid`
(a foreign key to the provider), and nullable `provider_identity` (a string of
at most 255 characters). Native origins use `source_name = "native"` and null
provider fields. Public native creation cannot supply another provider's
provenance. The former JSON `source` object is removed from models, REST
responses, and realtime payloads. `WorkspaceUser.source` remains unchanged.

| Model | Zulip provider identity |
| --- | --- |
| `WorkspaceStream` | Channel ID; direct/group conversations use `dm:` followed by the existing deterministic conversation UUID. |
| `WorkspaceStreamBinding` | Stream identity followed by the remote user ID, separated by `:`. |
| `WorkspaceStreamTopic` | Stream identity followed by the topic name, separated by `:`. |
| `WorkspaceUserTopicBinding` | Topic identity followed by the remote user ID, separated by `:`. |
| `WorkspaceMessage` | Remote message ID. |
| `WorkspaceUserMessageState` | Remote message ID followed by the observing remote user ID, separated by `:`. |
| `WorkspaceMessageReactionFact` | Remote message ID, reacting remote user ID, and normalized emoji name, separated by `:`. |
| `WorkspaceFile` | UUIDv5 of the provider UUID and remote upload URL, keeping arbitrary-length upload paths within the 255-character limit. |

These fields describe the provider origin of each object. Native messages
exported to Zulip retain their native origin; delivery records track the remote
copy separately. Technical placements, message-access bindings, and file-access
rows inherit context through their existing foreign keys rather than duplicating
provider identity.

`ExternalAccount.provider_uuid` references the same provider table.
`external_account_id` stores the remote user ID without a hostname prefix.
The account's `provider` string retains the provider type. Unconfigured accounts
without a server hostname can retain a null provider link; supplying a provider
UUID or configuring a server URL establishes the link. Account uniqueness
includes `provider_uuid`, so the same owner can connect accounts on different
servers and identical remote user IDs remain isolated. `settings.server_url`
is retained as the transport endpoint, including scheme, port, and path;
provider-scoped identity and hostname lookup use the provider foreign key.

Migration 0263 backfills available origins, removes the three canonical JSON
source columns, rebuilds projections and trigger functions, translates saved
event/task snapshots, and updates account revisions without invalidating
matching queued work or the current realtime subscription. Its downgrade
restores the previous source representation and hostname-prefixed account IDs.
Downgrading after creating multiple same-type accounts for one owner requires
resolving those accounts first because the old schema cannot represent them.
Provider deletion uses cascading foreign keys for provider-owned rows.


## Synchronization status

All eight provider-aware domain models expose a read-only `status` with values
`new`, `syncing`, `active`, and `error`. Native entities start and remain
`active`. A newly constructed provider entity starts as `new`; an import that
creates an already complete entity persists it directly as `active`.
Existing records are backfilled as `active` by migration 0264.

Provider reconciliation transitions an existing entity to `syncing`, then to
`active` after successful processing. A failed worker records `error` for its
specific persisted entity after rolling back the failed data writes. A retry
can return it to `active`. Existing data remains readable in every status.
Account connectivity errors do not mark all imported objects as failed.

These transitions use the existing worker transaction. Intermediate `syncing`
changes are not separately committed or broadcast. Status-only updates do not
change domain timestamps, counters, or enqueue new reconciliation work.
Archive, completion, read, starred and pinned fields remain independent.
Native message exports retain `active`; remote delivery uses its own state.
