# Workspace UI Realtime Integration

This document describes the public realtime contract consumed by the UI.
REST catch-up and websocket delivery use the same flat event object and the same
visible event surface.

## Endpoints

Common realtime endpoints are exposed through nginx:

- `GET /api/workspace/v1/events/`
- `GET /api/workspace/v1/epoch/`

The browser websocket endpoint is:

- `WS /api/workspace/v1/events/ws?last_epoch_version=<number>`

The internal websocket service path is `/v1/events/ws` on `127.0.0.1:21082`.
Browser code should use only `/api/workspace/v1/events/ws`.

| Transport | Authentication | Ordering | Intended use |
| --- | --- | --- | --- |
| `GET /events/` | IAM bearer header | ascending `epoch_version` | Initial load, reconnect catch-up, gap repair |
| `GET /epoch/` | IAM bearer header | single latest cursor | Compare local progress with the visible server epoch |
| `WS /events/ws` | IAM token in subprotocol | missed rows, then live rows | Low-latency delivery after catch-up |

All three surfaces use the same project/user visibility rules. The websocket is
not a provider feed, and there is no Mail-, Calendar-, or Messenger-specific
websocket path.

## Authentication

REST requests use the normal IAM bearer token:

```http
Authorization: Bearer <accessToken>
```

Websocket authentication is passed through `Sec-WebSocket-Protocol`. Send both
subprotocol values:

```ts
["workspace.events.v1", `bearer.${accessToken}`];
```

The server selects `workspace.events.v1`. Do not put the token in the query
string. `last_epoch_version` is the only websocket query parameter the backend
reads; omitted means `0`. Unauthorized handshakes close with `4401`; invalid
handshakes close with `4400`.

The browser must request exactly the application subprotocol plus one bearer
subprotocol. A token refresh is applied by opening a new websocket; credentials
cannot be replaced on an already accepted connection.

## Event Shape

Every event returned by REST `/events/` and every websocket message has this
schema. There is no outer `{ "type": "event", "event": ... }` wrapper.
External-source events are delivered only while the current user has matching
confirmed external account access; the backend filters REST catch-up,
`/epoch/`, and websocket delivery consistently.

The same stream carries all three services. Supported groupware kinds are:

- `mail.folder.created|updated|deleted`
- `mail.message.created|updated|deleted`
- `calendar.calendar.created|updated|deleted`
- `calendar.event.created|updated|deleted`
- `external_account.updated`

Groupware events use `object_type` values `mail_folder`, `mail_message`,
`calendar`, and `calendar_event`. Their payload contains the affected local
resource snapshot. A delete payload always identifies the deleted UUID, but it
can be either a minimal identity payload or a full provider-backed snapshot
depending on the mutation path.

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
    "source_name": "native",
    "source": {
      "kind": "native"
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

A provider-backed groupware snapshot includes the same display and delivery
projection returned by REST:

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 125,
  "project_id": "project-uuid",
  "user_uuid": "recipient-user-uuid",
  "object_type": "mail_message",
  "action": "updated",
  "created_at": "2026-07-15T09:30:00.000000Z",
  "updated_at": "2026-07-15T09:30:00.000000Z",
  "payload": {
    "kind": "mail.message.updated",
    "uuid": "mail-message-uuid",
    "folder_uuid": "mail-folder-uuid",
    "subject": "Provider delivery",
    "provider": {
      "uuid": "provider-uuid",
      "name": "Mail.ru",
      "kind": "mail"
    },
    "delivery": {
      "status": "delivered",
      "safe_error": null,
      "updated_at": "2026-07-15T09:30:00.000000Z"
    }
  }
}
```

## Payload Rules

Create, update, read, and action events carry the same full object snapshot that
the user receives from the corresponding REST endpoint or action response, plus
`payload.kind`.

Messenger local delete events are minimal and contain `payload.kind`, the
deleted object's `payload.uuid`, and only the extra identifiers required to
update local state:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, `source`

Message reaction changes emit `message_reaction.created`,
`message_reaction.updated`, or `message_reaction.deleted` for the acting user.
The payload contains `uuid`, `project_id`, `message_uuid`, `user_uuid`,
`emoji_name`, `source_name`, and `source`. Update events may also contain
`old_message_uuid`, `old_emoji_name`, `old_source_name`, and `old_source`.
The backend also emits `message.updated` snapshots with updated aggregate
`reactions` for users that can see the message.

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

Batch read operations emit the canonical `messages.read` event with
`message_uuids`. Single-resource read operations use the singular object kinds
above.

Supported `object_type` and `action` values:

| object_type        | actions                                 |
| ------------------ | --------------------------------------- |
| `message`          | `created`, `updated`, `deleted`, `read` |
| `message_reaction` | `created`, `updated`, `deleted`         |
| `stream`           | `created`, `updated`, `deleted`, `read` |
| `stream_binding`   | `created`                               |
| `topic`            | `created`, `updated`, `deleted`, `read` |
| `user`             | `updated`                               |
| `folder`           | `created`, `updated`, `deleted`         |
| `folder_item`      | `deleted`                               |
| `external_account` | `updated`                               |
| `mail_folder`      | `created`, `updated`, `deleted`         |
| `mail_message`     | `created`, `updated`, `deleted`         |
| `calendar`         | `created`, `updated`, `deleted`         |
| `calendar_event`   | `created`, `updated`, `deleted`         |

## Catch-Up And Cursor

For REST catch-up, request events strictly newer than the latest successfully
processed epoch:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500
```

`epoch_version>` is strict. If the HTTP client does not encode `>`, encode it as
`%3E`. Process events in ascending `epoch_version` order and persist the latest
processed epoch after applying each event.

`GET /api/workspace/v1/epoch/` returns the current user's latest visible epoch:

```json
{ "epoch_version": 124 }
```

### Cursor persistence rules

- Persist a cursor only after the corresponding event has been applied to all
  affected client stores.
- Treat an event whose `epoch_version` is less than or equal to the persisted
  cursor as a duplicate.
- A gap is not filled with guessed objects. Stop live application, catch up
  from the last committed cursor, then resume.
- The cursor is scoped with the authenticated Workspace identity and project.
  Clear or partition it when the IAM user/project changes.
- Do not use event UUID ordering or timestamps as a cursor; only
  `epoch_version` is monotonic for this contract.

Paginate until a page returns no marker. If a page has been partially applied
when the browser crashes, restarting from the last committed epoch safely
replays only the uncommitted suffix.

## Websocket Delivery

After the websocket is accepted, the server sends missed events newer than
`last_epoch_version`, then live events. Each websocket message is the same flat
event object returned by REST `/events/`.

The websocket service sends protocol-level WebSocket ping control frames at the
configured heartbeat interval. It does not send application-level JSON `hello`
or `ping` messages and does not process client JSON `pong` or `ack` messages.
Reconnect and catch-up are driven by the persisted `last_epoch_version` cursor.

Recommended client flow:

1. Load the persisted cursor.
2. Run REST catch-up with `epoch_version>` until no more events are returned.
3. Persist the latest processed epoch.
4. Open `/api/workspace/v1/events/ws?last_epoch_version=<latest>`.
5. Process websocket events through the same idempotent dispatcher as REST.
6. Deduplicate by `epoch_version` across REST catch-up, websocket catch-up, and
   live delivery.
7. On close, reconnect with backoff after another REST catch-up pass.

Example dispatcher skeleton:

```ts
async function applyWorkspaceEvent(event: WorkspaceEvent) {
  if (event.schema_version !== 1) {
    logUnsupported(event);
    return;
  }
  if (event.epoch_version <= cursorStore.get()) return;

  await stores.transaction(async () => {
    dispatchByObjectAndAction(event.object_type, event.action, event.payload);
    cursorStore.set(event.epoch_version);
  });
}

async function recover() {
  let cursor = cursorStore.get();
  for (;;) {
    const page = await api.events.list({
      "epoch_version>": cursor,
      page_limit: 500,
    });
    if (page.items.length === 0) break;
    for (const event of page.items) await applyWorkspaceEvent(event);
    cursor = cursorStore.get();
  }
  connectWebSocket(cursor);
}
```

The websocket's built-in missed-event phase closes the race between the last
REST page and the handshake. Deduplication still remains mandatory because a
reconnect or ambiguous client failure can replay an already applied event.

## UI Dispatch Notes

Dispatch by top-level `object_type` and `action`, then use `payload.kind` when a
more specific operation is needed. Unknown `schema_version`, `object_type`,
`action`, or `payload.kind` should be logged and skipped without breaking the
realtime loop.

Recommended reducer ownership:

| `object_type` | Primary UI store/effect |
| --- | --- |
| `message`, `message_reaction` | Messenger timeline, reaction aggregate, unread state |
| `stream`, `stream_binding`, `topic` | Messenger navigation and membership/topic state |
| `folder`, `folder_item` | Messenger folder navigation and badges |
| `user` | Shared Workspace identity/presence cache |
| `external_account` | Account status/settings screen and service availability |
| `mail_folder` | Mail navigation and counters |
| `mail_message` | Mail list, thread/detail, draft and delivery state |
| `calendar` | Calendar list, color, visibility state |
| `calendar_event` | Calendar grid/agenda/detail state |

Provider-backed snapshots expose a short `provider.name` and optional
`delivery`. Render the badge from that name, not from hard-coded kinds. Delivery
colors should be derived from `pending`, `delivered`, or `failed`; a
`safe_error` may be shown to the user, but raw provider identifiers never
appear in the UI model.

The timestamp placement and visual badge treatment are presentation concerns;
they do not change the event cursor or reducer identity. Resource UUID remains
the stable key while delivery status changes.

For message payloads, v1 supports markdown payloads:

```json
{ "kind": "markdown", "content": "Hello" }
```

Presence is updated through REST `users/{uuid}/actions/presence/invoke`. The
worker marks stale users offline and emits `user.updated` events with full user
snapshots, including the `avatar` URN.
