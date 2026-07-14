# Workspace <-> Zulip Integration

This document describes how the bridge between Workspace Messenger and Zulip
must work. The first part describes the behavior of the background service
`workspace-integration-bridge-worker`.

## First-Version Scope

The first version of the integration is a full bidirectional MVP, but not every
Zulip API surface is required.

The first version must implement:

- inbound synchronization of the full available Zulip history;
- realtime inbound synchronization through the Zulip event queue;
- public stream/topic support;
- private 1:1 support;
- importing Zulip users and matching them to Workspace users;
- importing files and attachments into Workspace storage;
- outbound message operations: send, edit, and delete;
- outbound reactions;
- outbound read/unread through the existing Workspace flags mechanism;
- outbound per-user stream and topic notification mode changes;
- diagnostics through logs and database tables;
- backend unit/integration tests.

The first version does not include:

- Zulip group private chats;
- outbound stream/topic management;
- outbound subscribe and unsubscribe operations;
- outbound participant and role changes;
- a separate UI, API, or CLI for integration status;
- mandatory acceptance through the live Zulip UI.

## workspace-integration-bridge-worker

### Purpose

`workspace-integration-bridge-worker` is a long-running background process that
synchronizes data between Zulip and Workspace Messenger in both directions:

- **Inbound (Zulip -> Workspace):** all available history, new and changed
  streams, topics, messages, reactions, files, read/unread state, and
  participants from Zulip are written to Workspace models and produce
  `m_workspace_events` for the UI.
- **Outbound (Workspace -> Zulip):** user actions in Workspace for objects with
  `source_name = zulip` are delivered back to Zulip on behalf of that user:
  message send, edit, delete, reactions, read/unread, and per-user stream/topic
  notification mode changes. Stream/topic management, subscribe/unsubscribe,
  participant, and role management remain outside the first outbound version.

Run command:

```bash
workspace-integration-bridge-worker --config-file etc/workspace/workspace.conf
```

The service inherits from `BasicService` and runs one iteration roughly every
**1 second**.

### First-Version Product Rules

- The bridge synchronizes the **full available Zulip history**, not just new
  messages after connection and not a limited time window.
- If the Zulip event queue expires and old events are no longer available, the
  bridge restores the **final object state**, not the historical event log. For
  example, if a reaction was added and removed before connectivity was restored,
  Workspace records only the current absence of that reaction.
- Zulip is the source of truth for imported Zulip entities. Workspace actions are
  sent back to Zulip, but the next resynchronization gives priority to the Zulip
  state.
- First-version bidirectional synchronization includes messages, edits, deletes,
  reactions, read/unread, and per-user stream/topic notification mode changes.
  Stream/topic renames, subscriptions, unsubscriptions, participants, and roles
  are synchronized inbound, but outbound changes for those objects are not part
  of the first version.
- Zulip group private chats are not synchronized in the first version. Public
  stream/topic and private 1:1 are supported.
- If history contains a Zulip user that does not exist in Workspace yet, a
  Workspace user is created automatically from the Zulip profile.
- Files and attachments are imported into Workspace storage. Message content
  must be processed so that Zulip upload links are replaced with native
  Workspace URNs, for example `urn:image:<uuid>` for images and equivalent URNs
  for other file types.
- First-version integration state and errors are visible through logs and
  database tables. A separate user-facing or admin status UI is not required for
  the first version.
- Fresh messages must appear in Workspace before the full history finishes
  loading: the agent processes tasks from newer `message_id` values to older
  ones, while older history is loaded in the background.
- Messages imported from Zulip are assigned to their real authors in Workspace,
  as if they were written in Workspace, while preserving `source_name = zulip`
  and the source link to Zulip.
- One public Zulip message must map to one Workspace message within
  `(project_id, server_url, zulip_message_id)`, even if several connected
  accounts can see it.
- Deleted Zulip messages are physically deleted from Workspace. If a delete is
  initiated in Workspace for a Zulip message, the outbound delete is sent to
  Zulip from the account of the user who performed the delete.
- Zulip messages are considered for import regardless of the numeric
  `sender_id`. If a message sender is not yet linked to a Workspace user, the
  bridge creates or reuses a fallback imported Workspace user from the sender
  metadata carried in the Zulip payload, then binds that user to the target
  stream before creating the message.

### General Architecture

There is one bridge service in the system. Inside it, the main agent
(`WorkspaceIntegrationBridgeWorker`) manages multiple `ZulipBridgeWorker`
instances. There may be multiple Zulip servers; for each connected account the
agent may start a separate subscription worker. Workers are responsible for
reading the Zulip event queue and passing events to the agent. Synchronization,
task ordering, and state writes are managed by the agent.

On every iteration, the main agent refreshes Zulip external-account access and
then runs `_start_bridges()`:

1. Due Zulip accounts are checked with the lightweight `users/me` request.
   Missing credentials set `access_status = missing_credentials`, Zulip
   authentication failures set `invalid_credentials`, temporary transport or
   server failures set `unavailable`, and successful checks set `confirmed`.
2. It loads from the database all `ExternalAccount` rows with
   `account_type = zulip`, populated `account_settings.credentials`, and
   `access_status = confirmed`. Accounts without confirmed access are skipped.
3. For each such account, it computes the worker key
   `(project_id, server_url, user_uuid)` and checks whether a `ZulipBridgeWorker`
   with that key is already running. If not, it creates a thread, passes the
   shared `_sync_queue` to it (worker -> agent), and calls `worker.start()`.
4. The worker subscribes to the Zulip event queue: it reads the saved `queue_id`
   from `ZulipEventQueueState` and tries to continue with the same queue. If the
   queue is available, the worker keeps reading events from the saved
   `last_event_id` without a full resync. If there was no subscription yet
   (`queue_id` is empty) or the old queue is unavailable, the worker registers a
   new queue through the Zulip API. It then puts a `ZulipEventQueueCreated` event
   into `_sync_queue`, signaling that a new queue subscription was just created
   (together with `UpdateZulipQueueState` to save `queue_id` and `last_event_id`
   in the database).

The queue subscription requests message, reaction, message update/delete,
message flag, stream, and subscription events. It declares the Zulip
`archived_channels` client capability so channel archiving is delivered as a
stream state update instead of a stream deletion event when the server supports
that behavior.

### External Account Access And Visibility

Zulip data may be stored in Workspace even when a particular Workspace user has
not confirmed their own Zulip credentials. Storage and visibility are separate:

- the bridge may import streams, topics, messages, reactions, files, and
  membership state from any connected confirmed account;
- user-facing streams, topics, messages, unread counters, folder items, REST
  events, and websocket events expose Zulip rows only when the recipient has a
  matching confirmed external account;
- the shared visibility gate is `m_confirmed_external_account_access`, keyed by
  `(project_id, user_uuid, account_type, source_scope)`;
- for Zulip, `source_scope` is the external account `server_url` and matches the
  source payload's `server_url`.

Hidden Zulip unread flags are not rewritten. While a user has no confirmed
Zulip external account, hidden Zulip messages and historical folder or
stream-binding events do not contribute to the UI, so they behave as read from
the user's perspective. After the account is confirmed, the imported messages
and their read/unread status become visible again and the bridge continues
syncing message state from Zulip.

This is the generic external-source visibility model. Future providers should
write source payloads with a stable provider scope and external account rows
with the same `account_type` and `source_scope`.

#### Implementation backlog: Zulip event coverage

The bridge currently focuses on events that directly affect Workspace messages,
streams, and stream membership. The following Zulip event work is still
outstanding:

- `stream` with `op = update`: extend the property mapping when Workspace gains
  fields for Zulip-only stream settings such as `topics_policy`,
  `history_public_to_subscribers`, `is_web_public`, message retention, and
  permission group related properties. The current mapping only covers stream
  name, description, invite-only state, archive state, and announcement-only
  policy.
- `realm_user` and `realm_bot`: consume realtime user and bot
  create/update/deactivate events instead of relying only on periodic user sync.
- `presence`, `typing`, `typing_edit_message`, and `user_status`: map realtime
  status and typing information if Workspace needs Zulip-originated presence or
  typing indicators.
- `realm_emoji`: synchronize custom emoji metadata so imported Zulip reactions
  can display custom emoji consistently.
- `attachment` and `submessage`: handle attachment lifecycle changes and Zulip
  interactive submessages separately from message content import if Workspace
  starts exposing those concepts.
- `user_topic`, `muted_topics`, `muted_users`, and `alert_words`: consume
  Zulip-originated personal mute, topic, and alert preferences where Workspace
  has matching settings. Workspace-originated topic notification mode changes
  are handled by outbound `/user_topics` updates.
- `channel_folder`: map Zulip channel folders if Workspace adds a corresponding
  channel grouping feature.
- Organization/admin configuration events such as `realm`, `realm_domains`,
  `realm_linkifiers`, `custom_profile_fields`, `default_streams`,
  `default_stream_groups`, `user_group`, `saved_snippets`, `scheduled_messages`,
  `reminders`, `navigation_view`, and device/push events are not required for
  the first bridge version unless a concrete Workspace feature starts depending
  on them.

The agent must be able to stop any `ZulipBridgeWorker` at any time through
`worker.stop()`. The worker sets `_stopped` and exits synchronization loops.
After `worker.stop()` is called, the worker must finish within **10 seconds**.

Therefore, waiting for a Zulip long-poll response (`get_events`) must also not
exceed **10 seconds**; otherwise the worker cannot check `_stopped` and stop on
time.

### History Synchronization Tasks

If a worker can continue with the same event queue, the agent does not recreate
history tasks and continues normal processing.

When the agent receives `ZulipEventQueueCreated` from a worker, a new event queue
subscription was just created: either the first connection or a reconnect where
the old queue is no longer available. While disconnected, anything may have
happened: new messages, text edits, deletes, reactions, read/unread changes.

The agent **does not inspect** existing tasks or look for gaps. It:

1. **deletes** all unsynchronized tasks (`pending`, `failed`) for that external
   account;
2. reads the current latest `message_id` from the Zulip server;
3. **recreates** tasks for synchronization of **all** messages (100 messages per
   task).

Partially completed tasks cannot be trusted after disconnect, because server
state in Zulip may already have changed.

This resync does not attempt to restore old events from an expired event queue.
The Zulip API returns the current message state, so the agent must bring
Workspace to the current Zulip state: text, deletion, flags, reactions, files,
read/unread, stream/topic names, and bindings. Historical intermediate changes
that are already gone from the event queue are not restored.

**Priority by `message_id`.** Messages with higher ids always have priority over
messages with lower ids. This rule applies everywhere: selecting a history sync
task, processing events from the worker queue, and resolving competition between
realtime events and history sync within one agent cycle.

#### What A Task Is

A task is stored in the `m_zulip_history_sync_tasks` table, model
`ZulipHistorySyncTask` (`workspace/messenger_api/dm/models.py`), migration
`migrations/0087-add-zulip-history-sync-tasks-d556e0.py`.

A task describes the message interval that must be loaded from Zulip:

- **from which message**: `from_message_id`;
- **through which message**: `to_message_id`;
- **for which user**: `user_uuid`;
- **which external account**: `external_account_uuid`, `server_url`,
  `project_id`;
- **execution status**: `pending`, `done`, `failed`:
  - `pending`: the task was created and has not been executed yet;
  - `done`: the interval `from_message_id` through `to_message_id` was fully
    synchronized;
  - `failed`: execution finished with an error; details are in `last_error`;
- **dates**: `created_at`, `updated_at`;
- **error**: `last_error` if there was one.

The **100 messages** interval limit applies only to tasks in the `pending`
status. Whoever creates the task must enforce it: the agent during initial or
repeat synchronization, and the worker when recording a realtime message.

#### Task Creation

Example: the Zulip server currently has 600 messages.

The agent creates **6 tasks** with 100 messages each:

| Task | from_message_id | to_message_id |
|---|---|---|
| 1 | 0 | 99 |
| 2 | 100 | 199 |
| 3 | 200 | 299 |
| 4 | 300 | 399 |
| 5 | 400 | 499 |
| 6 | 500 | 599 |

Each `external_account` has its own task set. The same principle applies both
on first connection and after every `ZulipEventQueueCreated`.

When selecting a task for execution, the agent takes the `pending` task with the
highest `from_message_id` / `to_message_id`: fresh messages first, then older
intervals.

If there are pending tasks without `last_error`, the agent takes them first.
Pending tasks with `last_error` do not block the rest of history; they are
retried after error-free pending intervals are exhausted.

#### Tasks From Realtime Events

When a worker receives a new message through the event queue and processes it,
it also creates a task that records the already processed interval. For example,
for user 1: `from_message_id = 55`, `to_message_id = 55`.

Such tasks are small, often one message id. A separate part of the code will
merge them into larger tasks later.

#### One Agent Cycle

Within one iteration, the agent:

1. processes one history sync task from the database, starting with the task that
   has the highest `message_id` among `pending`;
2. processes **all** events accumulated in `_sync_queue` from workers. Inside the
   queue, newer ids also go before older ids.

#### Accepted Implementation Decisions

These decisions extend the general contract above and must be considered when
the code is maintained:

- One `ZulipBridgeWorker` is started for each connected Zulip external account.
  It is responsible for the Zulip event queue and outbound commands. Separate
  history threads are not created: `WorkspaceIntegrationBridgeWorker` selects
  and executes `ZulipHistorySyncTask` tasks itself during its iteration.
- `ZulipBridgeWorker` does not perform history catch-up by `last_message_id`.
  After registering a new Zulip event queue, it sends `ZulipEventQueueCreated`
  to the agent; the agent recreates unfinished history tasks and then starts
  reading the realtime queue.
- If the Zulip server has no messages or the current latest `message_id` is `0`,
  the agent does not create history tasks and marks the queue state as
  synchronized.
- Errors that may be resolved on the next iteration (`SyncStreamsNeeded`,
  missing credentials, or a user that has not been imported yet) leave the task
  in the `pending` status and write the error text to `last_error`. The `failed`
  status is used for unexpected processing errors that require separate
  diagnostics.
- Zulip API network errors from the `requests.exceptions.RequestException`
  family are considered temporary for history sync: the task remains `pending`,
  the error is written to `last_error`, and the current history task batch stops
  so that the bridge does not hammer the server with repeated requests.
- If a network error happens on a history task wider than one `message_id`, the
  bridge deletes the source task and creates smaller pending tasks instead
  (usually 10 messages, and one message for already small ranges). New smaller
  tasks are created without inheriting `last_error`; only the smaller range that
  fails again receives the error. The retry fetch uses the concrete task size
  rather than a fixed 100 messages, so a slow range no longer blocks the whole
  synchronization with one large request.
- A one-`message_id` history task is loaded through the direct Zulip endpoint
  `/messages/{message_id}`, not through `/messages` with `anchor`/`num_after`.
  This is required for retry after splitting: if the history window around a
  specific message is slow, a single-message retry must not hit the same slow
  range fetch again. The direct request passes `apply_markdown=false`. If Zulip
  returns `BAD_REQUEST / Invalid message(s)`, the task is considered an empty
  range: the message does not exist or is not available to the current account,
  and this must not create a `failed` task.
- After a realtime event is processed successfully, the bridge writes a small
  `ZulipHistorySyncTask` with `done` status for the corresponding `message_id`
  range. This is a diagnostic record of an already applied realtime interval; it
  does not block future full resynchronization.
- `_sync_queue` is fully drained on every iteration: the agent reads commands
  until the first short `queue.Empty`. `sync_queue_batch_limit` remains only as a
  backward-compatible CLI/config parameter for older launches and must not limit
  processing of the accumulated queue.
- History tasks are processed one per agent iteration. The agent iteration runs
  roughly once per second, so fresh realtime events regularly get processing
  time between history intervals.
- For the lifetime of the current Zulip event queue subscription, the agent
  caches already processed Zulip stream/topic/message entities for the concrete
  `(project_id, server_url, user_uuid)`. Seeing the same entity again through
  history sync or a realtime duplicate must not update the stream, topic,
  message, bindings, or related user-resolution operations again. Real changes
  after subscription arrive through the Zulip event queue as separate
  update/delete/reaction/read events.
- User synchronization must be idempotent: if WorkspaceUser profile fields
  already match the external profile, `save()` is not called and
  `m_workspace_events` does not receive a repeated `user updated`. IAM sync does
  not overwrite runtime presence/status of an existing WorkspaceUser (`active`,
  `idle`, `away`), because that state is managed by Workspace.
- Presence heartbeat updates `last_ping_at`, but does not create `user.updated`
  if visible presence fields (`status`, `status_emoji`, `status_text`) have not
  changed. This protects the event queue from identical updates for an active
  user.
- `workspace-messenger-worker` moves only stale users that are not already
  `offline` to `offline`. A repeated cycle over an already offline user must not
  update the row and must not create new `user.updated` events.
- The subscription cache lives only until the next Zulip event queue
  resubscription. On `ZulipEventQueueCreated`, the agent clears this cache for
  the relevant external account and applies the first current state of entities
  again, because Zulip state may have changed while the old queue was absent.
- If `m_zulip_processed_entities` points to a deleted Workspace entity, the
  bridge forgets the stale cache record, creates or finds the Workspace entity
  again, and updates the mapping. This situation must not move the whole history
  interval to `failed`.
- Within one bridge process, the bridge remembers that raw content for the
  concrete `(project_id, server_url, zulip_message_id)` has already been
  synchronized. When the same message appears again through another external
  account, the bridge does not download the same attachments or update the
  content again, but still synchronizes account-specific state such as the read
  flag.
- Hot startup/read-path Zulip requests (`/users/me`, `/users`, `/streams`,
  `/messages`, `/messages/{message_id}`, `/events`,
  `/streams/{stream_id}/members`) and realtime queue registration (`/register`)
  are executed as direct HTTP Basic requests with the API key, not by creating a
  `zulip.Client` for every request. The official SDK performs an extra
  `server_settings` round trip during initialization with its own timeout, which
  slows history sync and can fail a useful request before the desired endpoint is
  reached.
- A Zulip upload download error does not postpone message creation. Instead of
  the original link, a placeholder `urn:zulip-file:download-failed?...` is
  inserted, and the error text is saved to `last_error` on the history/realtime
  task if processing happened in the context of a task or realtime event.
- Zulip `/messages` is called with a **30 second** transport timeout so a slow
  server response does not turn into many `failed` history tasks because of a
  short default timeout.
- Zulip `/streams/{stream_id}/members` is called with a **30 second** transport
  timeout because this request is used during subscriber rebind and affects the
  speed and reliability of history sync for streams.
- Zulip `/users`, `/users/me`, `/streams`, and `/register` are called with a
  **30 second** transport timeout so startup/reconnect does not fail on the SDK's
  short timeout before the agent reaches history sync.
- Zulip `get_events` is called with a **10 second** transport timeout so
  `worker.stop()` can finish the thread within the stop requirement.
  `ReadTimeout` on this long-poll request is treated as an empty event batch,
  not as an event queue error.

#### Reprocessing Messages

During history synchronization or queue processing, a message may already have
been processed earlier, for example through realtime before reconnect or because
tasks overlap. Such a message **must not simply be skipped**: it must go through
the create/update logic again so that Workspace state is refreshed (text, read
flag, reactions, and so on).

The mapping "Zulip `message_id` -> Workspace message" is stored separately so
the bridge can find an existing row and take the **update** path instead of the
create path. For public messages, the unique mapping key is
`(project_id, server_url, zulip_message_id)`.

**No extra notifications.** Update helpers must be idempotent: they may be
called repeatedly when the user's message state has not changed. If there is no
new data (same content, same flags, same reaction), the update finishes
**without** events in `m_workspace_events`; the user must not receive a change
notification. This is technical resynchronization, not a real edit in Zulip.

If the data is different, apply the change and produce the usual UI event.

When Zulip and Workspace data conflict for imported entities, Zulip wins. A
local Workspace action must be delivered to Zulip through the outbound path; if
Zulip accepts it, it is confirmed by later synchronization. If the current Zulip
state differs from the local state, Workspace is brought to Zulip.

#### What Is Processed For Each Message

For **every** inbound message, the bridge must process the full data set from
Zulip, not just text:

- **content**: message body;
- **files**: attachments are downloaded from Zulip, saved to Workspace storage,
  and links in content are replaced with Workspace URNs (`urn:image:<uuid>` and
  equivalents);
- **entity links**: Zulip user mentions, stream/topic references, and message
  permalinks are normalized to regular markdown links whose URL is a Workspace
  URN. User mentions become `[Full Name](urn:user:<user-uuid>)`; message,
  stream, and topic links become `urn:message:<message-uuid>`,
  `urn:stream:<stream-uuid>`, and `urn:topic:<topic-uuid>` when the referenced
  Workspace entity is already known. Other `http` / `https` links are wrapped
  as `urn:url:http(s)://...`. Wildcard Zulip mentions such as `@all` remain
  unchanged;
- **flags**: including `read` and supported per-user flags such as `starred`
  from the event/message payload. The existing Workspace flags mechanism is used
  for read/unread and other mapped flags, with flags created separately for each
  user;
- **reactions**: add and remove;
- **deletion**: if a message is marked deleted in Zulip, the corresponding
  Workspace message is physically deleted.

No individual parts may be skipped: on repeated processing, everything must be
compared with the current Workspace state.

Outbound Workspace messages keep the same markdown-link contract. Before sending
content to Zulip, the bridge translates resolvable Workspace URNs back to Zulip
syntax:

- `urn:user:<user-uuid>` becomes a Zulip mention using the synced Zulip
  `full_name` and `user_id`;
- `urn:stream:<stream-uuid>` and `urn:topic:<topic-uuid>` become Zulip
  stream/topic references;
- `urn:message:<message-uuid>` becomes a Zulip message permalink when the target
  message has a Zulip `message_id` and belongs to a public Zulip stream;
- `urn:file`, `urn:image`, and `urn:video` are uploaded to Zulip and replaced
  with the returned Zulip upload link;
- `urn:gravatar:<email-hash>` becomes a generated Gravatar image URL;
- `urn:url:http(s)://...` is unwrapped to a regular markdown URL.

If a URN cannot be resolved safely, the bridge leaves the original markdown link
unchanged instead of dropping content or failing the message.

If a Zulip attachment cannot be downloaded or saved, the message is still
created or updated. The content receives a link to a prepared placeholder
template that says the file could not be downloaded. The error is saved in the
diagnostic `last_error`/status field so it can be seen through the database and
logs.

#### Stream And Topic Rename

When processing a message, the bridge must look not only at `stream_id` and
`topic_name` in the source data, but also at the **current names** from Zulip:

- if the **stream name** (`display_recipient` / name in Zulip) changed, update
  the stream in Workspace;
- if the **topic name** (`subject` in Zulip) changed, update the topic in
  Workspace.

Streams and topics are still looked up by source, not by display name. A name
change is a real change: it must be written and, if the data actually changed,
must produce a UI event. If the name matches the already saved value, there must
be no extra notifications, following the same idempotency rules.

For private messages, the topic name in the source remains `"zulip"`; a local
Workspace rename by the user does not change this source lookup rule.

### Stream And Topic Synchronization

A Zulip message cannot be written to Workspace until the corresponding stream
and topic exist. While processing an inbound message in a task, the worker must
follow the chain: find or create stream -> find or create topic -> create
message.

#### If The Stream Does Not Exist Yet

When a message references a stream that does not exist in Workspace yet, the
stream must be created before the message is written.

**Public stream.** One message does not provide enough data: there is no
description, creator, or full participant list. Before continuing with that
message, the worker executing the sync task must request stream metadata and the
subscriber list from Zulip, create the stream in Workspace (and the topic if it
does not exist yet), and only then continue the task.

**Personal stream (private).** The message itself contains enough data: chat id,
participants, and conversation type. The stream is created immediately, without a
separate request for the full server stream list. The topic for such messages is
created with the name `"zulip"`.

By participant count:

- **two**: one-to-one personal conversation;
- **more than two**: skip it (group private chats are not synchronized in the
  first version);
- **fewer than two**: skip the message.

Private message recipients that include Zulip system users are also skipped at
the stream-mapping layer until the bridge has a supported Workspace
representation for those system conversations. This is not a message-level
sender filter: public stream messages and other supported messages from system
senders are still imported.

#### Creating A Stream In Workspace

The Zulip stream creator, the message author, and any other participant required
to create the stream, message, or bindings must be matched to a Workspace user.

Matching order:

1. find the user through a linked Zulip account by
   `(server_url, zulip_user_id)`;
2. if there is no link, find the Workspace/IAM user by email;
3. if no user is found, automatically create an imported Workspace user from the
   Zulip profile.

For message authors, the bridge may also use the sender metadata present in the
message payload itself (`sender_email` and `sender_full_name`) to create a
fallback imported Workspace user. This covers Zulip system senders such as
notification bots, which can appear in stream history without a normal synced
Zulip user account. The same fallback sender is used when importing attachments
from that message.

Zulip `user_id`, email, name, and source data are saved so that later messages
from the same user link to the same Workspace row. Zulip `avatar_url` stays in
external-account user metadata. The Workspace profile avatar is always derived
from the Zulip user's real `delivery_email`, falling back to the profile
`email`: trim whitespace, lowercase it, calculate the MD5 digest used by Zulip
12.1 for Gravatar identifiers, and store
`urn:gravatar:<32-hex-email-hash>`. The same value is written when an existing
Workspace user is matched by email or refreshed during a later user sync.

The misspelled legacy form `urn:gavatar:<user-uuid>` is not a supported runtime
format. Migration `0095-fix-workspace-gravatar-avatar-urn-f75679.py` replaces
legacy user avatars and their copies in user events and markdown content before
the new validation constraint is enabled. Markdown links or images that use the
canonical `urn:gravatar:<email-hash>` form are converted to generated Gravatar
image URLs when sent to Zulip.

IAM remains the external source for corporate users, but the absence of an IAM
row does not block Zulip history import. If a canonical IAM row for the same
user appears later, separate matching logic must connect it to the already
created Workspace user instead of creating a duplicate.

Workspace stores the name, description, access flags, and Zulip link
(`stream_id`, `server_url`, `source_name = zulip`).

**Roles.** The Zulip stream creator becomes the stream **owner** in Workspace.
All other known Zulip subscribers receive the **member** role. The owner is not
duplicated in the members list.

#### Topics

No separate preliminary synchronization of the Zulip topic list is required. A
topic appears in Workspace when the first message referencing it arrives.

Topics must be found and matched by **source** (`stream_id`, `server_url`,
`topic_name` from Zulip), not by Workspace display name. A user may rename a
topic locally, and the display name may stop matching the Zulip `subject`.

**Personal messages.** For one external account in a private stream, there can
be only **one** topic: the source always has `topic_name = "zulip"`. The user may
rename the display name; topic lookup uses only the source, not the current
Workspace name.

**Public streams.** There is no "one topic per account" restriction: a stream may
have any number of topics, each identified by its Zulip `subject`.

#### When A Stream Is Created Or Updated

Streams may appear while messages are being processed or through separate Zulip
stream/subscription events. During history processing, a message must still be
able to create a missing stream and topic:

| Situation | What to do |
|---|---|
| A task contains a message in an unknown public stream | The worker requests the stream and subscribers from Zulip, creates the stream and topic, and continues the task |
| A task contains a message in a personal chat | The worker builds the stream from message data, creates the `"zulip"` topic, and continues the task |

After creating a stream from full Zulip stream/subscription data, the bridge
must refresh participant bindings. Realtime subscription events then keep those
bindings current: `peer_add` adds newly visible subscribers as members, and
`peer_remove`, `remove`, or a stream visibility delete removes the affected
Workspace stream binding.

Subscription `update` events synchronize per-user notification state where
Workspace has an equivalent stream binding setting. Zulip `is_muted` and the
legacy `in_home_view` property map to Workspace `muted` or `all_messages`;
Zulip stream notification toggles such as `desktop_notifications`,
`email_notifications`, `push_notifications`, and `audible_notifications` map to
`mentions_only` when disabled and `all_messages` when enabled or reset to the
server default. Zulip-only subscription fields such as color, pinning, and
wildcard mention policy stay no-op until Workspace has matching fields.

### Outbound Synchronization

For objects with `source_name = zulip`, Workspace must send user actions back to
Zulip.

The first-version outbound scope includes:

- sending new messages to stream/topic and private 1:1;
- editing and deleting messages;
- adding and removing reactions;
- read/unread actions through existing Workspace flags;
- per-user stream notification mode changes through Zulip subscription
- per-user topic notification mode changes through Zulip user-topic visibility
  policies.

The first-version outbound scope does not include:

- stream/topic rename;
- subscribe and unsubscribe operations;
- participant and role changes where supported by the Zulip API.

When a Workspace user changes notification mode for a Zulip-backed stream, the
bridge sends `/users/me/subscriptions/properties` from that user's Zulip
account. Workspace `muted` maps to Zulip `is_muted = true`; `all_messages` maps
to `is_muted = false`; `mentions_only` unmutes the stream and disables explicit
desktop, audible, push, and email notification toggles for that user. Inbound
Zulip subscription updates are marked as already applied so they do not echo
back to Zulip as outbound commands.

When a Workspace user changes notification mode for a Zulip-backed topic, the
bridge sends `/user_topics` from that user's Zulip account. Workspace `default`,
`mute`, `unmute`, and `follow` map to Zulip visibility policies `0`, `1`, `2`,
and `3`. Only real notification mode changes are sent; no-op topic updates are
skipped before creating outbound work.

An outbound action is executed on behalf of the Workspace user who performed the
action, through that user's connected Zulip account. For example, deleting a
message in Workspace is sent to Zulip from the account of the user who deleted
the message.

An outbound action is considered finally applied only after a successful Zulip
response or after inbound/resync confirms the corresponding state.

If Zulip rejects an action because of permissions or changed state, the action
is moved to `failed`. Workspace state is then brought back to Zulip through
inbound/resync. Actions that are known to be impossible must not be retried
forever.

### Diagnostics

The first version does not require a separate integration status UI. Operational
state must be diagnosable through:

- `workspace-integration-bridge-worker` logs;
- `ZulipEventQueueState`;
- `ZulipHistorySyncTask`;
- outbound retry/failed states;
- file and user import errors saved in the relevant `last_error`/status fields.

### First-Version Acceptance Criteria

The first version is considered ready when it is covered by backend
unit/integration tests. Required test groups:

- initial history task creation and selection from fresh messages to old ones;
- continuing operation after reconnecting to the same Zulip event queue;
- creating a new queue and recreating unfinished history tasks;
- idempotent import of one public message by several accounts without
  duplicates;
- matching a Zulip user by link, then by email, then creating an imported user;
- importing public stream/topic;
- realtime inbound stream create/update/delete and subscription membership
  events;
- importing private 1:1 and skipping group private chats;
- importing files into Workspace storage and replacing links with Workspace URNs;
- inserting a placeholder link when file download fails;
- read/unread through Workspace flags per user;
- physically deleting a Workspace message when it is deleted in Zulip;
- outbound send/edit/delete;
- outbound add/remove reaction;
- outbound read/unread;
- moving an outbound action to `failed` if Zulip rejects it because of
  permissions or state.
