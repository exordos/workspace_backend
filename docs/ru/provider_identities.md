# Provider identities

Messenger provider instances are stored in `messenger_providers` with `uuid`
and unique `hostname`. Hostnames are normalized by URL parsing; scheme, port,
and path are not part of the instance identity. Existing deterministic
Workspace user, channel, topic, and message UUIDs are preserved.

Provider-aware domain models carry `source_name`, `provider_uuid` (a foreign
key to the provider), and nullable `provider_identity` (a string of at most
255 characters). `source_name = "native"` means strictly local: both provider
fields are null and the object never produces a Zulip outgoing intention.
A child created in a Zulip stream or message inherits `source_name = "zulip"`
and its parent's non-null `provider_uuid`, even when the user creates it through
Workspace. Its `provider_identity` stays null until the provider confirms it.
Clients cannot choose or change this inherited provenance. The former JSON
`source` object is removed from models, REST responses, and realtime payloads.
`WorkspaceUser.source` remains unchanged.

| Model | Zulip provider identity |
| --- | --- |
| `WorkspaceStream` | Channel ID; direct/group conversations use `dm:` followed by the existing deterministic conversation UUID. |
| `WorkspaceStreamBinding` | Stream identity followed by the remote user ID, separated by `:`. |
| `WorkspaceStreamTopic` | Stream identity followed by the topic name, separated by `:`. |
| `WorkspaceUserTopicBinding` | Topic identity followed by the remote user ID, separated by `:`. |
| `WorkspaceMessage` | Remote message ID. |
| `WorkspaceUserMessageState` | Remote message ID followed by the observing remote user ID, separated by `:`. |
| `WorkspaceMessageReactionFact` | Remote message ID, reacting remote user ID, reaction type, and emoji code, separated by `:`. |
| `WorkspaceFile` | Remote upload path returned by Zulip. |

The canonical object owns its provider identity. Sending and receiving resolve
that identity directly; a native object does not acquire a separate remote copy.
File links retain account-scoped access observations, and reaction links retain
custom emoji metadata. Technical placements, message-access bindings, and
file-access rows inherit context through their existing foreign keys.

`ExternalAccount.provider_uuid` references the same provider table.
`external_account_id` stores the remote user ID without a hostname prefix.
The account's `provider` string retains the provider type. Unconfigured accounts
without a server hostname can retain a null provider link; supplying a provider
UUID or configuring a server URL establishes the link. Account uniqueness
includes `provider_uuid`, so the same owner can connect accounts on different
servers and identical remote user IDs remain isolated. `settings.server_url`
is retained as the transport endpoint, including scheme, port, and path;
provider-scoped identity and hostname lookup use the provider foreign key.

Migration 0263 replaces canonical JSON source columns with provider fields.
Migrations 0265 and 0266 retire the old outgoing and incoming task machinery.
Migration 0267 inherits existing children's provider context from their parents,
removes obsolete root identity links and native outgoing intentions, and allows
a pending Zulip identity without an external ID. This cutover is irreversible;
restore a verified database backup together with its matching application
version when recovery is required. Provider deletion uses cascading foreign
keys for provider-owned rows.


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

The outgoing worker persists preparation before making HTTP requests. A
confirmed response or retained matching event completes the intention and fills
the canonical provider identity. Incoming domain writes, client publication, and
inbox acknowledgement commit together. Status-only updates do not change domain
timestamps, counters, or enqueue new synchronization work. Archive, completion,
read, starred and pinned fields remain independent.

## Optional send receipts

Message sends pass the canonical Workspace message UUID as `local_id`, together
with the sender's `queue_id`. Zulip returns it as `local_message_id` in that
queue's message event. The outgoing worker can use a durably saved matching
event after a lost HTTP response. The authoritative remote ID remains
`provider_identity`; `local_id` is not a persistent lookup key or a provider
idempotency guarantee. If neither a response nor a definitive receipt survives,
the worker keeps the send uncertain instead of blindly creating a duplicate.

## Linked users in imported direct conversations

A provider import can create a user before that person links an IAM account.
Linking adopts the imported external account while retaining the IAM user UUID.
For imported direct conversations, initialization replaces the old participant
reference in `owner_uuid` and `direct_user_uuid` and restores missing membership
and topic access for the linked user. Existing membership preferences take
precedence, including inactive membership. The superseded imported membership
becomes inactive; its historical references remain available. Conversation UUIDs, message history, and native conversations keep their identities.

Direct sends resolve the other endpoint relative to the current sender, scoped
to the project and provider. They also recognize deterministic imported user
aliases when an account was linked before participant repair was available.
A stale queued recipient snapshot is not authoritative. Group conversations
continue to resolve active membership, and channels use their provider stream ID.
An unresolved participant remains pending; it is never guessed from a name.

Migration 0283 repairs existing deterministic aliases and refreshes unprepared
queued direct sends. Prepared requests, uncertain sends, and delivery receipts
are preserved to avoid changing a request that might already have reached Zulip.
Downgrading restores the prior recipient resolver but retains repaired identity
references; it does not reconstruct obsolete aliases.
