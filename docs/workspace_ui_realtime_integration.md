# Workspace UI Realtime Integration

This document describes the public Messenger realtime contract consumed by the
Workspace UI. REST catch-up and websocket delivery use the same flat event
object and the same IAM-scoped visibility rules.

## Endpoints

- `GET /api/workspace/v1/events/`
- `GET /api/workspace/v1/epoch/`
- `WS /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`

The internal websocket service path is `/v1/events/ws` on `127.0.0.1:21082`.
Browser code must use only the nginx path above. There are no domain-specific
or external-integration websocket endpoints.

| Transport | Authentication | Ordering | Intended use |
| --- | --- | --- | --- |
| `GET /events/` | IAM bearer header | ascending `epoch_version` | Initial load, reconnect catch-up, gap repair |
| `GET /epoch/` | IAM bearer header | one latest cursor | Compare local progress with the visible server epoch |
| `WS /events/ws` | IAM token in subprotocol | missed rows, then live rows | Low-latency delivery after catch-up |

## Authentication

REST requests use the IAM bearer token:

```http
Authorization: Bearer <accessToken>
```

Websocket clients send exactly these two `Sec-WebSocket-Protocol` values:

```ts
["workspace.events.v1", `bearer.${accessToken}`];
```

The server selects `workspace.events.v1`. Do not put the token in the query
string. Persist and send `epoch_generation` with every non-zero resume cursor;
omitted `last_epoch_version` means the cold cursor `0`. A cold cursor does not
require a generation, but it returns the normal typed gap response when the
seven-day retained suffix cannot provide complete history from epoch `1`.
Unauthorized handshakes close with `4401` and invalid handshakes close with
`4400`. Token refresh requires a new connection.

## Event shape

Every REST event and websocket event message is the same `schema_version: 1`
object. There is no outer `{ "type": "event", "event": ... }` wrapper. The
socket additionally sends typed `ready` and cursor-error control messages.

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 124,
  "project_id": "project-uuid",
  "user_uuid": "recipient-user-uuid",
  "object_type": "message",
  "action": "created",
  "created_at": "2026-07-02T16:37:49.552044Z",
  "updated_at": "2026-07-02T16:37:49.552047Z",
  "payload": {
    "kind": "message.created",
    "uuid": "message-uuid",
    "project_id": "project-uuid",
    "user_uuid": "recipient-user-uuid",
    "stream_uuid": "stream-uuid",
    "topic_uuid": "topic-uuid",
    "author_uuid": "author-user-uuid",
    "payload": {
      "kind": "markdown",
      "content": "Hello"
    },
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "reactions": {},
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

Top-level fields describe the event row. Resource identifiers live in
`payload`, and `payload.kind` is the only `kind` field on the event payload.
The local mail representation is an internal persistence detail and never
appears in this public event shape.

Create, update, read, and action events carry the same full object snapshot as
the corresponding Messenger REST response, plus `payload.kind`. Local delete
events are minimal:

- `stream.deleted`, `folder.deleted`, and `folder_item.deleted`: `kind`, `uuid`;
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`;
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, and `source`.

Reaction changes emit `message_reaction.created`,
`message_reaction.updated`, or `message_reaction.deleted` for the acting user.
The backend also emits `message.updated` snapshots with the updated aggregate
`reactions` map for users who can see the message.

Batch stream binding creation uses `payload.items`. Read actions emit
`message.read`, `topic.read`, or `stream.read` and continue to emit aggregate
`topic.updated`, `stream.updated`, and `folder.updated` events when unread
counters change. Batch message reads use `messages.read` with
`message_uuids`.

Supported values are:

| `object_type` | actions |
| --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` |
| `message_reaction` | `created`, `updated`, `deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` |
| `stream_binding` | `created`, `updated`, `deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` |
| `user` | `updated` |
| `folder` | `created`, `updated`, `deleted` |
| `folder_item` | `deleted` |
| `file` | `created`, `updated`, `deleted` |

## Catch-up and cursor handling

Request events strictly newer than the latest successfully applied epoch:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<generation>&page_limit=500
```

`epoch_version>` is strict. Process events in ascending order and persist the
cursor only after applying an event to every affected client store.

`GET /api/workspace/v1/epoch/` returns the generation, current epoch, and
retention floor visible to the current IAM user and project:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

Cursor rules:

- treat `(epoch_generation, epoch_version)` as one indivisible cursor and send
  the generation with every non-zero REST or websocket resume;
- ignore events whose epoch is less than or equal to the committed cursor;
- repair a gap with REST catch-up instead of guessing resource state;
- partition or clear the cursor when the IAM user or project changes;
- never order by event UUID or timestamp;
- paginate until the server returns no further page marker.
- treat HTTP 410 `EventsCursorExpiredError` / `error=epoch_pruned` as a cache
  reset boundary; refresh authoritative snapshots and restart with the returned
  generation. The server retains events for exactly seven days and this reset
  never deletes messages, files, or domain state.

## Websocket delivery

After accepting the connection, the server sends missed events newer than the
saved cursor, then sends
`{"type":"ready","epoch_generation":"...","epoch_version":124}`. No live
event can overtake this frame. Keep the user-notification gate closed until
`ready`; catch-up messages must update state without notifying. It uses
websocket ping control frames, not application JSON `hello`, `ping`, `pong`, or
`ack` messages. Cursor expiry sends the typed error body and closes with `4410`.

Recommended client flow:

1. Load the committed cursor pair, or use cold epoch `0` without a generation.
2. Run REST catch-up until no more events are returned.
3. Open the websocket with the latest cursor pair.
4. Apply REST and websocket messages through the same idempotent dispatcher.
5. Keep notifications disabled until the websocket `ready` frame, then enable
   live notifications.
6. Deduplicate by `(epoch_generation, epoch_version)`.
7. After a close, run catch-up again and reconnect with backoff.

The websocket missed-event phase closes the race between the last REST page and
the handshake. Deduplication is still mandatory because ambiguous failures may
replay an already applied event.

## UI dispatch

Dispatch first by top-level `object_type` and `action`, then by `payload.kind`
when a more specific operation is needed. Unknown schema versions or event
values should be logged and skipped without breaking the realtime loop.

| `object_type` | Primary UI store or effect |
| --- | --- |
| `message`, `message_reaction` | Timeline, reactions, unread state |
| `stream`, `stream_binding`, `topic` | Navigation, membership, topic state |
| `folder`, `folder_item` | Folder navigation and badges |
| `user` | Shared identity and presence cache |
| `file` | File metadata and protected/public blob cache invalidation |

`stream.deleted` for the removed participant revokes the whole stream: evict
all protected blobs whose cached metadata has that `stream_uuid` immediately.
Remaining members receive `stream_binding.deleted`; binding changes produce
`stream_binding.updated`. A cursor-gap error clears all derived protected blob
caches before snapshots are reloaded.

V1 message content uses the markdown payload shape:

```json
{ "kind": "markdown", "content": "Hello" }
```

Presence is updated through the REST
`users/{uuid}/actions/presence/invoke` action. The worker marks stale users
offline and emits `user.updated` with the full public user snapshot.
