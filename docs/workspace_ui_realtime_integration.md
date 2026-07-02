# Workspace UI Realtime Integration

This document describes the public realtime contract consumed by the UI.
REST catch-up and websocket delivery use the same flat event object.

## Endpoints

REST messenger endpoints are exposed through nginx:

- `GET /api/messenger/v1/events/`
- `GET /api/messenger/v1/epoch/`

The browser websocket endpoint is:

- `WS /api/messenger/ws?last_epoch_version=<number>`

The internal websocket service path is `/v1/events/ws` on `127.0.0.1:21082`.
Browser code should use only `/api/messenger/ws`.

## Authentication

REST requests use the normal IAM bearer token:

```http
Authorization: Bearer <accessToken>
```

Websocket authentication is passed through `Sec-WebSocket-Protocol`. Send both
subprotocol values:

```ts
[
  "workspace.events.v1",
  `bearer.${accessToken}`,
]
```

The server selects `workspace.events.v1`. Do not put the token in the query
string. `last_epoch_version` is the only websocket query parameter the backend
reads; omitted means `0`. Unauthorized handshakes close with `4401`; invalid
handshakes close with `4400`.

## Event Shape

Every event returned by REST `/events/` and every websocket message has this
schema. There is no outer `{ "type": "event", "event": ... }` wrapper.

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

Top-level fields describe only the event row. Object identifiers such as
`stream_uuid` and `topic_uuid` live inside `payload`. `payload.kind` is the only
`kind` field.

## Payload Rules

Create, update, read, and action events carry the same full object snapshot that
the user receives from the corresponding REST endpoint or action response, plus
`payload.kind`.

Delete events are minimal and contain `payload.kind`, the deleted object's
`payload.uuid`, and only the extra identifiers required to update local state:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`

Batch stream binding creation uses `payload.items`:

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 130,
  "project_id": "project-uuid",
  "user_uuid": "owner-user-uuid",
  "object_type": "stream_binding",
  "action": "created",
  "created_at": "2026-07-02T16:37:49.552044Z",
  "updated_at": "2026-07-02T16:37:49.552047Z",
  "payload": {
    "kind": "stream_bindings.created",
    "uuid": "stream-uuid",
    "items": [
      {
        "uuid": "binding-uuid",
        "project_id": "project-uuid",
        "stream_uuid": "stream-uuid",
        "user_uuid": "added-user-uuid",
        "who_uuid": "owner-user-uuid",
        "role": "member",
        "notification_mode": "all_messages",
        "created_at": "2026-07-02T16:37:49.552044Z",
        "updated_at": "2026-07-02T16:37:49.552047Z"
      }
    ]
  }
}
```

Read actions emit `message.read`, `topic.read`, or `stream.read` with the full
message/topic/stream action response in payload. They also continue to emit
`topic.updated`, `stream.updated`, and `folder.updated` events when unread
counters changed. The UI should apply those update events; read events do not
replace aggregate badge updates.

Historical rows with `payload.kind == "messages.read"` may still appear after
migration. Treat them as legacy best-effort payloads containing `message_uuids`.
New read events use the singular object kinds above.

Supported `object_type` and `action` values:

| object_type | actions |
| --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` |
| `stream` | `created`, `updated`, `deleted`, `read` |
| `stream_binding` | `created` |
| `topic` | `created`, `updated`, `deleted`, `read` |
| `user` | `updated` |
| `folder` | `created`, `updated`, `deleted` |
| `folder_item` | `deleted` |

## Catch-Up And Cursor

For REST catch-up, request events strictly newer than the latest successfully
processed epoch:

```http
GET /api/messenger/v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500
```

`epoch_version>` is strict. If the HTTP client does not encode `>`, encode it as
`%3E`. Process events in ascending `epoch_version` order and persist the latest
processed epoch after applying each event.

`GET /api/messenger/v1/epoch/` returns the current user's latest visible epoch:

```json
{"epoch_version": 124}
```

## Websocket Delivery

After the websocket is accepted, the server sends missed events newer than
`last_epoch_version`, then live events. Each websocket message is the same flat
event object returned by REST `/events/`.

The websocket service does not send public `hello` or `ping` frames and does not
process client `pong` or `ack` frames. Reconnect and catch-up are driven by the
persisted `last_epoch_version` cursor.

Recommended client flow:

1. Load the persisted cursor.
2. Run REST catch-up with `epoch_version>` until no more events are returned.
3. Persist the latest processed epoch.
4. Open `/api/messenger/ws?last_epoch_version=<latest>`.
5. Process websocket events through the same idempotent dispatcher as REST.
6. Deduplicate by `epoch_version` across REST catch-up, websocket catch-up, and
   live delivery.
7. On close, reconnect with backoff after another REST catch-up pass.

## UI Dispatch Notes

Dispatch by top-level `object_type` and `action`, then use `payload.kind` when a
more specific operation is needed. Unknown `schema_version`, `object_type`,
`action`, or `payload.kind` should be logged and skipped without breaking the
realtime loop.

For message payloads, v1 supports markdown payloads:

```json
{"kind": "markdown", "content": "Hello"}
```

Presence is updated through REST `users/{uuid}/actions/presence/invoke`. The
worker marks stale users offline and emits `user.updated` events with full user
snapshots.
