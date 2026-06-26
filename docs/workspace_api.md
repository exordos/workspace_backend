# Workspace Messenger API

This document describes the current API contract implemented by
`workspace-messenger-api` and the companion `workspace-messenger-events`
websocket service.

## Runtime Entry Points

Direct local services:

```text
REST API:       http://127.0.0.1:21081/v1
WebSocket API:  ws://127.0.0.1:21082/v1/events/ws
OpenAPI spec:   http://127.0.0.1:21081/specifications/3.0.3
```

The deployed nginx manifest exposes the messenger API through:

```text
REST API:       /api/messenger/v1/...
WebSocket API:  /api/messenger/ws?last_epoch_version=<number>
OpenAPI spec:   /api/messenger/specifications/3.0.3
```

`/api/messenger/` is proxied to the REST service on `127.0.0.1:21081`.
The exact nginx location `/api/messenger/ws` is proxied to the websocket
service endpoint `/v1/events/ws` on `127.0.0.1:21082`.

## General Rules

- Request and response bodies are JSON (`application/json`).
- Resource identifiers are UUIDs unless a field explicitly says otherwise.
- Timestamps are UTC datetimes serialized as ISO-8601 strings.
- REST authentication uses a Genesis IAM bearer token:

```http
Authorization: Bearer <token>
```

To get a token in the local test environment, request it from Exordos Core IAM
through the gateway and use the `access_token` field from the response:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=login%2Bpassword&
login=admin&
password=admin&
scope=openid+email+profile+project%3Af04648e8-2bdf-4e93-b7bb-aac9850133fe&
ttl=3600&
refresh_ttl=172800
```

The same token request can also be sent as JSON:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "login+password",
  "login": "admin",
  "password": "admin",
  "scope": "openid email profile project:f04648e8-2bdf-4e93-b7bb-aac9850133fe",
  "ttl": 3600,
  "refresh_ttl": 172800
}
```

The UI client uses the IAM default client. No client credentials are required or
sent from browser-side code. `ttl=3600` means the access token is issued for 1
hour. `refresh_ttl=172800` means the refresh token is issued for 2 days.

Example authenticated request:

```http
GET /api/messenger/v1/folders/
Authorization: Bearer <access_token from IAM response>
```

To refresh an expired access token, send the refresh token to the same default
client endpoint:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=refresh_token&
refresh_token=<refresh_token from IAM response>
```

JSON refresh body is also accepted:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "<refresh_token from IAM response>"
}
```

Use the new `access_token` from the response for subsequent messenger API
requests. If the refresh response includes a new `refresh_token`, replace the
stored refresh token with it.

`user_uuid` is taken from IAM token information. `project_id` is taken from IAM
introspection information. User-scoped resources automatically filter and/or
write the current `user_uuid`.

Typical RESTAlchemy/IAM error response:

```json
{
  "code": 400,
  "json": {
    "code": 400,
    "type": "ValidationErrorException",
    "message": "Validation error occurred."
  }
}
```

## Pagination And Filters

Collection endpoints use RESTAlchemy cursor pagination:

| Query parameter | Type | Description |
| --- | --- | --- |
| `page_limit` | integer | Maximum number of items. `0` or an omitted value means no explicit limit. |
| `page_marker` | UUID or integer | Marker for the next page. UUID resources use the previous page's last `uuid`; events use the previous page's last `epoch_version`. |

If `page_limit` is provided, responses include `X-Pagination-Limit`. If another
page exists, responses also include `X-Pagination-Marker`.

Messenger controllers also support conditional filter suffixes:

| Suffix | Meaning | Example |
| --- | --- | --- |
| `>` | strictly greater than | `epoch_version>123` |
| `<` | strictly less than | `epoch_version<123` |
| `=>` | greater than or equal | `epoch_version=>123` |
| `=<` | less than or equal | `epoch_version=<123` |

When a query parameter name contains `>` or `<`, URL-encode it if the HTTP
client does not do that automatically:

```http
GET /v1/events/?epoch_version%3E=123&page_limit=500
```

## Endpoint Summary

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/v1/` | List routes below `/v1/`. |
| `GET` | `/v1/server_settings` | Return Zulip-like server settings. |
| `GET` | `/v1/server_settings/` | Same as above; trailing slash is supported. |
| `GET` | `/v1/folders/` | List folders for the current IAM user. |
| `POST` | `/v1/folders/` | Create a folder. |
| `GET` | `/v1/folders/{folder_uuid}` | Get a folder. |
| `PUT` | `/v1/folders/{folder_uuid}` | Update a folder. |
| `DELETE` | `/v1/folders/{folder_uuid}` | Delete a folder. |
| `GET` | `/v1/folder_items/` | List folder items for the current IAM user. |
| `POST` | `/v1/folder_items/` | Create a folder item. |
| `GET` | `/v1/folder_items/{folder_item_uuid}` | Get a folder item. |
| `DELETE` | `/v1/folder_items/{folder_item_uuid}` | Delete a folder item. |
| `POST` | `/v1/folder_items/{folder_item_uuid}/actions/pin/invoke` | Pin a folder item. |
| `POST` | `/v1/folder_items/{folder_item_uuid}/actions/unpin/invoke` | Unpin a folder item. |
| `GET` | `/v1/streams/` | List streams visible to the current IAM user. |
| `POST` | `/v1/streams/` | Create a stream. |
| `GET` | `/v1/streams/{stream_uuid}` | Get a stream. |
| `GET` | `/v1/stream_bindings/` | List stream bindings. |
| `POST` | `/v1/stream_bindings/` | Create a stream binding. |
| `GET` | `/v1/stream_bindings/{binding_uuid}` | Get a stream binding. |
| `PUT` | `/v1/stream_bindings/{binding_uuid}` | Update a stream binding. |
| `DELETE` | `/v1/stream_bindings/{binding_uuid}` | Delete a stream binding. |
| `GET` | `/v1/stream_topics/` | List topics visible to the current IAM user. |
| `POST` | `/v1/stream_topics/` | Create a topic. |
| `GET` | `/v1/stream_topics/{topic_uuid}` | Get a topic. |
| `PUT` | `/v1/stream_topics/{topic_uuid}` | Rename a topic; body must contain `name`. |
| `DELETE` | `/v1/stream_topics/{topic_uuid}` | Delete a topic. |
| `POST` | `/v1/stream_topics/{topic_uuid}/actions/toggle_done/invoke` | Toggle the current user's `is_done` flag. |
| `GET` | `/v1/messages/` | List messages visible to the current IAM user. |
| `POST` | `/v1/messages/` | Create a message. |
| `GET` | `/v1/messages/{message_uuid}` | Get a message. |
| `GET` | `/v1/events/` | List durable realtime events for the current IAM user. |
| `GET` | `/v1/epoch/` | Return the current user's latest visible event epoch. |
| `GET` | `/v1/users/` | List workspace users. |
| `GET` | `/v1/users/{user_uuid}` | Get a workspace user. |
| `GET` | `/v1/me/` | List routes below `/v1/me/`. |

## Server Settings

`GET /v1/server_settings` is public and does not require `Authorization`. It is
implemented by middleware and does not use the resource router. Unsupported
query parameters are reported in
`ignored_parameters_unsupported`.

Example response:

```json
{
  "result": "success",
  "msg": "Welcome to Exordos Workspace",
  "authentication_methods": {
    "password": true,
    "dev": false,
    "email": true,
    "ldap": false,
    "remoteuser": false,
    "github": false,
    "azuread": false,
    "gitlab": false,
    "google": false,
    "apple": false,
    "saml": false,
    "openid connect": false
  },
  "push_notifications_enabled": true,
  "email_auth_enabled": true,
  "require_email_format_usernames": true,
  "realm_url": "https://zulip.genesis-core.tech",
  "realm_name": "Genesis Corporation",
  "realm_icon": "/user_avatars/2/realm/icon.png?version=2",
  "realm_description": "<p>The coolest place in the universe.</p>",
  "realm_web_public_access_enabled": false,
  "external_authentication_methods": [],
  "realm_uri": "https://zulip.genesis-core.tech"
}
```

## Folders

`POST /v1/folders/` writes to `m_folders`. Reads use `m_folders_view`.
Responses hide `project_id` and `user_uuid`.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Folder identifier. |
| `title` | string, 1..64 | yes | no | Folder title. |
| `background_color_value` | integer `0..2^32-1` or `null` | no | no | ARGB color value. |
| `unread_count` | integer | no | yes | Aggregated unread count. |
| `system_type` | `all`, `created`, or `null` | no | yes | System folder type; defaults to `created`. |
| `folder_items` | array | no | yes | Nested folder items from the view. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Example:

```http
POST /api/messenger/v1/folders/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Response example:

```json
{
  "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "title": "Inbox",
  "background_color_value": 4280391411,
  "unread_count": 3,
  "system_type": "created",
  "folder_items": [
    {
      "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "chat_type": "stream",
      "order_index": 10,
      "pinned_at": null,
      "unread_count": 3,
      "created_at": "2026-06-22T09:30:00Z",
      "updated_at": "2026-06-22T09:30:00Z"
    }
  ],
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Update example:

```http
PUT /api/messenger/v1/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Archive",
  "background_color_value": 4289352960
}
```

Delete example:

```http
DELETE /api/messenger/v1/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
```

Realtime side effects:

| Operation | Durable payload kind | Websocket event type | Websocket body |
| --- | --- | --- | --- |
| create folder | `folder.created` | `folder` | Full folder snapshot. |
| update folder | `folder.updated` | `folder` | Full folder snapshot. |
| delete folder | `folder.deleted` | `folder` | Only `folder.uuid`. |

## Folder Items

`POST /v1/folder_items/` writes to `m_folder_items`. Reads use
`m_folder_items_created_view`.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Folder item identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `folder_uuid` | UUID | yes | no | Folder UUID. |
| `user_uuid` | UUID | no | yes | IAM user scope. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `chat_type` | `stream`, `group`, `private` | yes | no | Chat type. |
| `order_index` | integer or `null` | no | no | Manual sort index. |
| `pinned_at` | datetime or `null` | no | action-managed | Pin timestamp. |
| `unread_count` | integer | no | yes | Unread count for this stream and user. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Create example:

```http
POST /api/messenger/v1/folder_items/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Response example:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": null,
  "unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Pin and unpin return the same folder item shape. `pin` sets `pinned_at` to the
current UTC time; `unpin` sets it to `null`.

Pin example:

```http
POST /api/messenger/v1/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/pin/invoke
Authorization: Bearer <access_token>
```

Pin response example:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": "2026-06-22T09:31:00Z",
  "unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```

Unpin example:

```http
POST /api/messenger/v1/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/unpin/invoke
Authorization: Bearer <access_token>
```

Delete example:

```http
DELETE /api/messenger/v1/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50
Authorization: Bearer <access_token>
```

Realtime side effects:

| Operation | Durable payload kind | Websocket event type | Websocket body |
| --- | --- | --- | --- |
| add stream to folder | `folder.updated` | `folder` | Full parent folder snapshot with `folder_items`. |
| pin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with updated `pinned_at`. |
| unpin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with `pinned_at: null`. |
| remove stream from folder | `folder_item.deleted` | `folder_item` | Only `folder_item.uuid`. |

## Streams

`POST /v1/streams/` writes to `m_workspace_streams`, creates owner bindings,
and creates a default topic named `General Topic`. Reads use
`m_workspace_user_streams`.

If `direct_user_uuid` is provided, the backend creates a two-user private
stream, sets `private: true`, stores `private_index` as
`":".join(sorted([current_user_uuid, direct_user_uuid]))`, and creates owner
bindings for both users. `private_index` is a technical read-only field for
database deduplication, is hidden from the public API, and must not be sent by
the client. Repeating the same request for the same user pair returns the
existing stream.

Supported source payloads:

```json
{
  "source_name": "native",
  "source": {
    "kind": "native"
  }
}
```

```json
{
  "source_name": "zulip",
  "source": {
    "kind": "zulip",
    "stream_id": 123
  }
}
```

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Stream identifier. |
| `name` | string | yes | no | Stream name. |
| `description` | string | yes | no | Stream description. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `owner` | UUID | no | yes | Owner from the user stream view. |
| `user_uuid` | UUID | no | yes | Current user in the user stream view. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | no | yes | Current user's role. |
| `unread_count` | integer | no | yes | Current user's unread count. |
| `source_name` | `native`, `zulip` | yes | no | Source name. |
| `source` | object | yes | no | Source payload. |
| `invite_only` | boolean | no | no | Invite-only stream flag. |
| `announce` | boolean | no | no | Announcement stream flag. |
| `direct_user_uuid` | UUID | no | no | Other direct-chat participant. |
| `private` | boolean | no | yes | Private stream flag. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "name": "Engineering",
  "description": "Engineering workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false
}
```

Direct chat create request:

```json
{
  "name": "Direct",
  "description": "Private workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "33333333-3333-3333-3333-333333333333"
}
```

Realtime side effects:

| Operation | Durable payload kind | Websocket event type | Websocket body |
| --- | --- | --- | --- |
| create stream | `stream.created` | `stream` | Full user stream snapshot. |
| create stream | `folder.updated` | `folder` | Updated `All chats` and `Channels`/`Personal` system folder snapshots. |

For direct private streams, one `stream.created` event is written for each
participant. Stream creation also writes `folder.updated` events for each
participant's `All chats` folder and for `Personal` when the stream is private,
or `Channels` when it is not private.

## Stream Bindings

Stream bindings are stored in `m_workspace_stream_bindings`. On create,
`who_uuid` is always overwritten with the current IAM user's UUID.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Binding identifier. |
| `project_id` | UUID | yes | no | Project scope. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `user_uuid` | UUID | yes | no | User receiving access. |
| `who_uuid` | UUID | no | yes | User that performed the action. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | no | no | Role; defaults to `member`. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "user_uuid": "33333333-3333-3333-3333-333333333333",
  "role": "member"
}
```

## Stream Topics

`POST /v1/stream_topics/` writes to `m_workspace_stream_topics` and creates
`m_workspace_user_topic_flags` rows for stream recipients. Reads use
`m_workspace_user_topics_view` and are scoped to the current IAM user.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Topic identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `name` | string, max 128 | yes | no | Topic name. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `user_uuid` | UUID | no | yes | Current user in the topic view. |
| `unread_count` | integer | no | yes | Current user's unread count for the topic. |
| `is_default` | boolean | no | yes | Whether this is the stream default topic. |
| `is_done` | boolean | no | action-managed | Current user's done flag. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "name": "Releases",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
}
```

`PUT /v1/stream_topics/{topic_uuid}` requires a body with `name`. The backend
checks that the current user has a binding to the topic's stream before
renaming the topic.

`POST /v1/stream_topics/{topic_uuid}/actions/toggle_done/invoke` flips the
current user's `is_done` flag and returns the updated topic view.

## Messages

`POST /v1/messages/` writes to `m_workspace_messages`, creates per-recipient
rows in `m_workspace_user_message_flags`, and writes one durable workspace event
per recipient to `m_workspace_events`. Reads use
`m_workspace_user_messages_view` and are scoped to the current IAM user.

The only supported message payload in v1 is markdown:

```json
{
  "kind": "markdown",
  "content": "Hello, workspace"
}
```

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Message identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `topic_uuid` | UUID | yes | no | Topic UUID; required by current code. |
| `author_uuid` | UUID | no | yes | Message author. |
| `payload` | object | yes | no | Message payload. |
| `user_uuid` | UUID | no | yes | Current user in the user message view. |
| `read` | boolean | no | yes | Current user's read flag. Authors are created as read. |
| `pinned` | boolean | no | yes | Current user's pinned flag. |
| `starred` | boolean | no | yes | Current user's starred flag. |
| `is_own` | boolean | no | yes | Whether `author_uuid` equals the current user. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  }
}
```

Response example:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": true,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

## Events And Epoch

Events are durable outbox rows stored in `m_workspace_events`. They are
generated when streams or messages are created and when folders or folder items
change. Stream events are scoped to the created stream owner. Message events are
scoped per recipient; folder and folder item events are scoped to the folder
owner. The event primary identifier is `epoch_version`, a monotonically
increasing integer.

`GET /v1/events/` returns a standard RESTAlchemy list with no envelope. Events
are sorted by `epoch_version` ascending by default.

Message event example:

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

Folder create and update event payloads contain a full folder snapshot from
`m_folders_view`. Adding a stream to a folder and pinning or unpinning a folder
item also produce `folder.updated`, because the parent folder snapshot changed.

Folder update event example:

```json
{
  "epoch_version": 125,
  "uuid": "dbf5f7ad-4fe5-4fe7-8fa7-cd5cf65ad573",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "folder.updated",
    "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
    "project_id": "22222222-2222-2222-2222-222222222222",
    "user_uuid": "11111111-1111-1111-1111-111111111111",
    "title": "Inbox",
    "background_color_value": 4280391411,
    "system_type": "created",
    "unread_count": 0,
    "folder_items": [
      {
        "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
        "project_id": "22222222-2222-2222-2222-222222222222",
        "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
        "user_uuid": "11111111-1111-1111-1111-111111111111",
        "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
        "chat_type": "stream",
        "order_index": 10,
        "pinned_at": "2026-06-22T09:31:00Z",
        "unread_count": 0,
        "created_at": "2026-06-22T09:30:00Z",
        "updated_at": "2026-06-22T09:31:00Z"
      }
    ],
    "created_at": "2026-06-22T09:30:00Z",
    "updated_at": "2026-06-22T09:31:00Z"
  },
  "created_at": "2026-06-22T09:31:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```

Delete events intentionally contain only the deleted entity identifier:

```json
{
  "epoch_version": 126,
  "uuid": "a1f9ddf2-b28c-4df0-89af-cab996ba43e1",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "folder.deleted",
    "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210"
  },
  "created_at": "2026-06-22T09:32:00Z",
  "updated_at": "2026-06-22T09:32:00Z"
}
```

```json
{
  "epoch_version": 127,
  "uuid": "7ae06725-4d74-4704-97bb-ed8eceaef60e",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "folder_item.deleted",
    "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50"
  },
  "created_at": "2026-06-22T09:33:00Z",
  "updated_at": "2026-06-22T09:33:00Z"
}
```

Supported event payload kinds:

| Payload kind | Produced by | REST payload |
| --- | --- | --- |
| `stream.created` | `POST /v1/streams/` | Full user stream snapshot. |
| `message.created` | `POST /v1/messages/` | Full user message snapshot. |
| `folder.created` | `POST /v1/folders/` | Full user folder snapshot. |
| `folder.updated` | `POST /v1/streams/`, `PUT /v1/folders/{uuid}`, `POST /v1/folder_items/`, pin/unpin actions | Full user folder snapshot. |
| `folder.deleted` | `DELETE /v1/folders/{uuid}` | Only deleted folder `uuid`. |
| `folder_item.deleted` | `DELETE /v1/folder_items/{uuid}` | Only deleted folder item `uuid`. |

For strict catch-up after a processed cursor, use:

```http
GET /v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500
```

`GET /v1/epoch/` returns the latest visible event epoch for the current IAM
user, or `0` when there are no visible events:

```json
{
  "epoch_version": 124
}
```

## Workspace Users

Workspace users are stored in `m_workspace_users`. The route is global rather
than project-scoped.

| Field | Type | Description |
| --- | --- | --- |
| `uuid` | UUID | User identifier. |
| `username` | string, 1..128 | Username. |
| `source` | `iam` | User source. |
| `status` | `active`, `idle`, `offline`, `do_not_disturb` | Presence status. |
| `first_name` | string or `null` | First name. |
| `last_name` | string or `null` | Last name. |
| `email` | string or `null` | Email address. |
| `last_ping_at` | datetime or `null` | Last ping timestamp. |
| `created_at` | datetime | Creation time. |
| `updated_at` | datetime | Update time. |

## WebSocket Realtime Summary

The websocket service uses the subprotocol `workspace.events.v1` and authenticates
the bearer token from `Sec-WebSocket-Protocol`. The `last_epoch_version` query
parameter is optional at protocol level and defaults to `0`, but UI clients
should always pass their latest persisted cursor. Detailed UI integration rules
are documented in `docs/workspace_ui_realtime_integration.md`.

Connection example from browser code:

```ts
const ws = new WebSocket(
  "/api/messenger/ws?last_epoch_version=124",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

The server sends dispatch-ready event frames. Stream creation events have
`event.type: "stream"` and include a full user stream snapshot. Folder create,
folder update, and folder item add/pin/unpin events have `event.type: "folder"`
and include a full folder snapshot:

```json
{
  "type": "event",
  "event": {
    "epoch_version": 125,
    "type": "folder",
    "kind": "folder.updated",
    "folder": {
      "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "title": "Inbox",
      "background_color_value": 4280391411,
      "system_type": "created",
      "unread_count": 0,
      "folder_items": [
        {
          "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
          "project_id": "22222222-2222-2222-2222-222222222222",
          "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
          "user_uuid": "11111111-1111-1111-1111-111111111111",
          "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
          "chat_type": "stream",
          "order_index": 10,
          "pinned_at": "2026-06-22T09:31:00Z",
          "unread_count": 0,
          "created_at": "2026-06-22T09:30:00Z",
          "updated_at": "2026-06-22T09:31:00Z"
        }
      ],
      "created_at": "2026-06-22T09:30:00Z",
      "updated_at": "2026-06-22T09:31:00Z"
    }
  }
}
```

Delete events are minimal:

```json
{
  "type": "event",
  "event": {
    "epoch_version": 126,
    "type": "folder",
    "kind": "folder.deleted",
    "folder": {
      "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210"
    }
  }
}
```

```json
{
  "type": "event",
  "event": {
    "epoch_version": 127,
    "type": "folder_item",
    "kind": "folder_item.deleted",
    "folder_item": {
      "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50"
    }
  }
}
```
