# Workspace UI Realtime Integration

This document describes how the UI should consume durable messenger events from
the current backend code. The backend repo does not contain the UI
`shared/lib/event-loop.ts`; this file is the integration contract for the UI
repository.

## Backend Endpoints

REST messenger endpoints are proxied by nginx:

- `GET /api/messenger/v1/events/`
- `GET /api/messenger/v1/epoch/`

The websocket endpoint is proxied by an exact nginx location:

- `WS /api/messenger/ws?last_epoch_version=<number>`

The internal websocket service endpoint is `/v1/events/ws` on
`127.0.0.1:21082`. UI code must use `/api/messenger/ws`; do not connect to the
internal service path from the browser.

## Authentication

REST requests use the normal IAM bearer token:

```http
Authorization: Bearer <accessToken>
```

Websocket authentication is passed through `Sec-WebSocket-Protocol`. The client
must send both subprotocol values:

```ts
[
  "workspace.events.v1",
  `bearer.${accessToken}`,
]
```

The server selects only:

```ts
"workspace.events.v1"
```

Do not put the token in the websocket query string. `last_epoch_version` is the
only websocket query parameter the backend reads; if it is omitted, the backend
uses `0`. UI clients should still send the latest persisted cursor explicitly.
An unauthorized websocket handshake is closed with code `4401`; an invalid
handshake is closed with code `4400`.

## REST Event Format

`GET /api/messenger/v1/events/` returns a standard RESTAlchemy list of
`WorkspaceEvent` models, without an envelope:

```json
[
  {
    "epoch_version": 124,
    "uuid": "0cb14b5a-6bf0-4de2-bdb5-4e98df4044e0",
    "project_id": "22222222-2222-2222-2222-222222222222",
    "user_uuid": "11111111-1111-1111-1111-111111111111",
    "payload": {
      "kind": "message.created",
      "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
      "author_uuid": "11111111-1111-1111-1111-111111111111",
      "payload": {
        "kind": "markdown",
        "content": "Hello, workspace"
      },
      "read": true,
      "pinned": false,
      "starred": false,
      "is_own": true,
      "created_at": "2026-06-22T10:10:00Z",
      "updated_at": "2026-06-22T10:10:00Z"
    },
    "created_at": "2026-06-22T10:10:00Z",
    "updated_at": "2026-06-22T10:10:00Z"
  }
]
```

For catch-up, request events strictly newer than the last successfully handled
epoch:

```http
GET /api/messenger/v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500
```

The query parameter name is `epoch_version>`. If the HTTP client does not encode
it automatically, encode `>` as `%3E`.

Do not use `epoch_version=>` for the UI cursor path: that is inclusive and may
return the last processed event again. The UI must still deduplicate because the
overall delivery guarantee is at-least-once.

`GET /api/messenger/v1/epoch/` returns:

```json
{
  "epoch_version": 124
}
```

## WebSocket Frames

After a successful websocket connection, the server sends `hello`, then missed
events newer than `last_epoch_version`, then live events.

Hello frame:

```json
{
  "type": "hello",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "epoch_version": 124
}
```

Event frame:

```json
{
  "type": "event",
  "event": {
    "epoch_version": 125,
    "type": "message",
    "message": {
      "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
      "author_uuid": "11111111-1111-1111-1111-111111111111",
      "payload": {
        "kind": "markdown",
        "content": "Hello, workspace"
      },
      "read": true,
      "pinned": false,
      "starred": false,
      "is_own": true,
      "created_at": "2026-06-22T10:10:00Z",
      "updated_at": "2026-06-22T10:10:00Z"
    }
  }
}
```

Ping frame:

```json
{
  "type": "ping",
  "ts": "2026-06-22T10:10:25+00:00"
}
```

The client may respond with:

```json
{
  "type": "pong",
  "ts": "2026-06-22T10:10:25+00:00"
}
```

After processing an event, the client may also acknowledge the latest handled
cursor:

```json
{
  "type": "ack",
  "epoch_version": 125
}
```

`ack` updates only the server-side cursor for the current websocket connection.
The UI must still persist its own `last_epoch_version`.

## REST-To-Dispatch Normalization

The backend websocket already emits a dispatch-ready `WorkspaceRealtimeEvent`
shape:

```ts
type WorkspaceRealtimeEvent = {
  epoch_version: number;
  type: "message";
  message: WorkspaceUserMessage;
};
```

REST `/events/` returns the raw outbox model, so the UI catch-up path must
normalize `WorkspaceEventModel` to the same shape as websocket event frames.

Current mapping for `payload.kind === "message.created"`:

```ts
function normalizeWorkspaceEvent(
  model: WorkspaceEventModel,
): WorkspaceRealtimeEvent | null {
  if (model.payload.kind !== "message.created") {
    console.warn("Unsupported workspace event kind", model.payload.kind);
    return null;
  }

  return {
    epoch_version: model.epoch_version,
    type: "message",
    message: {
      uuid: model.payload.uuid,
      project_id: model.payload.project_id,
      user_uuid: model.payload.user_uuid,
      stream_uuid: model.payload.stream_uuid,
      topic_uuid: model.payload.topic_uuid,
      author_uuid: model.payload.author_uuid,
      payload: model.payload.payload,
      read: model.payload.read,
      pinned: model.payload.pinned,
      starred: model.payload.starred,
      is_own: model.payload.is_own,
      created_at: model.payload.created_at,
      updated_at: model.payload.updated_at,
    },
  };
}
```

The persisted event payload currently includes `read`, `pinned`, `starred`, and
`is_own` for the recipient. UI code should prefer those backend values. A legacy
fallback may compute missing values as:

```ts
const isOwn = message.author_uuid === currentUserUuid;
const read = isOwn;
const pinned = false;
const starred = false;
```

Stream names, topic names, and user display names are intentionally not resolved
in the realtime layer. The existing UI state/model layer should resolve those by
UUID.

## Required UI Behavior

1. Store `last_epoch_version` per workspace/user/client instance. At minimum,
   key it by `project_id` and `user_uuid`.
2. On application start, read the stored cursor; default to `0`.
3. Before opening the websocket, run REST catch-up from the stored cursor:
   `GET /api/messenger/v1/events/?epoch_version%3E=<last>&page_limit=500`.
4. Apply catch-up events in ascending `epoch_version` order. The backend sorts
   `/events/` ascending by default, but the UI can sort defensively.
5. If a catch-up page reaches the page limit, continue catching up from the
   latest processed epoch until no more events are returned.
6. After an event is successfully applied or deliberately skipped as unsupported,
   update the persisted cursor to that event's `epoch_version`.
7. Open `WS /api/messenger/ws?last_epoch_version=<last_processed_epoch_version>`.
8. Process websocket `event` frames through the same idempotent dispatch path as
   REST catch-up events.
9. Deduplicate by `epoch_version`. The backend can deliver the same event more
   than once across REST catch-up, websocket catch-up, listen/notify, fallback
   polling, and reconnects.
10. Send `ack` after processing a websocket event if the UI websocket helper
    supports client-to-server frames.

## Reconnect And Error Handling

- If the websocket closes, reconnect with backoff.
- Before every reconnect attempt, run REST catch-up from the latest persisted
  cursor, then open the websocket with the updated cursor.
- If REST catch-up returns `401` or `403`, stop the reconnect loop until the
  auth/session layer refreshes the token.
- If the websocket closes with `4401`, treat it as an auth failure and wait for
  auth/session refresh.
- If an unknown REST `payload.kind` or websocket `event.type` arrives, log it,
  skip it, and advance the cursor after the skip decision so the UI does not
  fetch the same unsupported event forever.
- If a message payload kind is unknown, keep the event idempotent and avoid
  crashing the realtime loop. v1 only supports `payload.kind === "markdown"`.

## Delivery Semantics

- Delivery is at-least-once.
- Ordering is by `epoch_version`.
- The UI dispatch pipeline must be idempotent.
- v1 supports only `payload.kind === "message.created"` on REST and
  `event.type === "message"` on websocket.

## Manual Verification Checklist

Use the `admin/admin` account for live checks in environments that provide that
test account.

1. After sending a message through REST, it appears in the UI without manual
   refresh.
2. After page reload or websocket downtime, REST catch-up loads missed messages.
3. Reconnect does not duplicate already rendered messages.
4. Own messages are displayed as own/read.
5. Messages from another user in the same stream arrive through websocket.
6. Unknown events are logged and skipped without breaking the realtime loop.
