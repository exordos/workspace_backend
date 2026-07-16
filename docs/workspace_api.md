# Workspace v1 API

This document describes the browser-facing API contract composed by nginx from
the preserved `workspace-messenger-api`, the common `workspace-api`, and the
companion `workspace-messenger-events` websocket service. Public Messenger
requests use the dedicated Messenger process; common users, client service
settings, and REST events use `workspace-api`. Mail, Calendar, External Users,
and provider-management endpoints are not part of this release.

Native Messenger state is canonical in the local SMTP/IMAP journal. PostgreSQL
is a rebuildable read projection and may also store client-only settings. A
future provider must exchange messages through mail protocols rather than a
separate provider API; the existing Messenger provenance fields are reserved
for that transport.

## Runtime Entry Points

Direct local services:

```text
Messenger REST API:  http://127.0.0.1:21081/v1
Events WebSocket:    ws://127.0.0.1:21082/v1/events/ws
Workspace REST API:  http://127.0.0.1:21084/v1
Worker:              workspace-messenger-worker
Messenger OpenAPI:   http://127.0.0.1:21081/specifications/3.0.3
Workspace OpenAPI:   http://127.0.0.1:21084/specifications/3.0.3
```

The deployed nginx manifest exposes the UI contract through:

```text
Workspace REST root: /api/workspace/v1/...
Messenger REST:      /api/workspace/v1/messenger/...
Events REST:         /api/workspace/v1/events/...
Events WebSocket:    /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>
OpenAPI spec:        /api/workspace/specifications/3.0.3
```

`/api/workspace/v1/messenger/` is proxied to the preserved Messenger REST
service on `127.0.0.1:21081`; the remainder of `/api/workspace/` is proxied to
the Workspace REST service on `127.0.0.1:21084`.
The exact nginx location `/api/workspace/v1/events/ws` is proxied to the
websocket service endpoint `/v1/events/ws` on `127.0.0.1:21082`.

The deployed nginx manifest sets `client_max_body_size 50m` for proxied
requests.

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
GET /api/workspace/v1/messenger/folders/
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

Messenger resources keep a canonical provenance projection instead of exposing
transport identifiers:

```json
{
  "provider": {
    "uuid": "provider-uuid",
    "name": "Short provider name",
    "kind": "zulip"
  },
  "delivery": {
    "status": "pending",
    "safe_error": null,
    "updated_at": "2026-07-15T09:30:00.000000Z"
  }
}
```

`provider.name` is the short badge label supplied by a future provider.
`delivery.status` is `pending`, `delivered`, or `failed`. Provider external IDs,
sync cursors, queue IDs, ETags, raw protocol payloads, and provider database
state are not part of the UI contract. Local resources have `provider: null`
and `delivery: null`.


The browser client uses the same IAM bearer token and project scope for every
domain. The public server-discovery endpoint is
`GET /api/workspace/v1/messenger/server_settings`; it is the only
unauthenticated Workspace endpoint used by the UI.

This is a greenfield public layout. There are no compatibility aliases for
`/api/messenger/**`, `/api/v1/**`,
`/api/workspace/v1/messenger/events/**`, or the former messenger websocket
path. There is no browser-facing or internal Provider API in this release.

## Pagination And Filters

Collection endpoints use RESTAlchemy cursor pagination:

| Query parameter | Type | Description |
| --- | --- | --- |
| `page_limit` | integer | Maximum number of items. `0` or an omitted value means no explicit limit. |
| `page_marker` | UUID or integer | Marker for the next page. UUID resources use the previous page's last `uuid`; events use the previous page's last `epoch_version` and require the matching `epoch_generation` whenever that marker is non-zero. |

If `page_limit` is provided, responses include `X-Pagination-Limit`. If another
page exists, responses also include `X-Pagination-Marker`.

`GET /api/workspace/v1/messenger/messages/` uses a stable composite keyset.
Set `sort_key=created_at` and `sort_dir=asc` or `sort_dir=desc`; rows are ordered
by `(created_at, uuid)` in that direction. `page_marker` remains the UUID of the
last row returned to preserve the public client contract. The server resolves
that UUID inside the same IAM project, authenticated-user view, and message
filter scope, then continues strictly after its composite key. A marker outside
that scope is not accepted. `X-Pagination-Marker` is emitted only when a
`page_limit + 1` probe proves that another row exists, so a full final page does
not advertise a nonexistent continuation.

Workspace collection controllers also support conditional filter suffixes:

| Suffix | Meaning | Example |
| --- | --- | --- |
| `>` | strictly greater than | `epoch_version>123` |
| `<` | strictly less than | `epoch_version<123` |
| `=>` | greater than or equal | `epoch_version=>123` |
| `=<` | less than or equal | `epoch_version=<123` |

When a query parameter name contains `>` or `<`, URL-encode it if the HTTP
client does not do that automatically:

```http
GET /api/workspace/v1/events/?epoch_version%3E=123&epoch_generation=781203&page_limit=500
```

Event pagination and reconnect use the cursor pair
`(epoch_generation, epoch_version)`, not an epoch number alone. A cold cursor
of `0` may omit `epoch_generation`. If the retained event suffix no longer
starts at epoch `1`, that cold request returns the same HTTP 410 gap response
as any other cursor that cannot produce a complete delta; the client must load
authoritative snapshots before starting a new cursor.

## Endpoint Summary

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/workspace/v1/` | List routes below `/api/workspace/v1/`. |
| `GET` | `/api/workspace/v1/messenger/server_settings` | Return Zulip-like server settings. |
| `GET` | `/api/workspace/v1/messenger/server_settings/` | Same as above; trailing slash is supported. |
| `GET` | `/api/workspace/v1/messenger/folders/` | List folders for the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/folders/` | Create a folder. |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Get a folder. |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Update a folder. |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Delete a folder. |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | List folder items for the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | Create a folder item. |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Get a folder item. |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Delete a folder item. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | Pin a folder item. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | Unpin a folder item. |
| `GET` | `/api/workspace/v1/messenger/streams/` | List streams visible to the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/streams/` | Create a stream. |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Get a stream. |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Update a stream. |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Delete a stream for all stream users. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` | Add users to a stream by role. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` | Set `is_archived: true`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` | Set `is_archived: false`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` | Set current user's stream notification mode. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` | Mark all unread stream messages as read for the current user. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` | List stream bindings. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Get a stream binding. |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Update a stream binding. |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Remove a user from a stream. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` | List topics visible to the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` | Create a topic. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Get a topic. |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Rename a topic; body must contain `name`. |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Delete a topic. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/toggle_done/` | Toggle the shared `is_done` flag for all topic users. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` | Set current user's topic notification mode. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` | Make the topic its stream's default topic. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` | Mark all unread topic messages as read for the current user. |
| `GET` | `/api/workspace/v1/messenger/messages/` | List messages visible to the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/messages/` | Create a message. |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Get a message. |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Update a message payload. |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Delete a message. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` | Mark message as read for the current user. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` | Mark unread messages in the same topic up to this message as read. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` | List reactions for messages visible to the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` | Create a message reaction. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Get a message reaction visible through message access. |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Update the current user's reaction. |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Delete the current user's reaction. |
| `GET` | `/api/workspace/v1/messenger/files/` | List files visible to the current IAM user. |
| `POST` | `/api/workspace/v1/messenger/files/` | Create file metadata or upload multipart file data. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | Get a visible file metadata record. |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | Update an owned file metadata record. |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | Delete an owned file and its access rows. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | Download visible file bytes. |
| `GET` | `/api/workspace/v1/services/` | List available Workspace services. |
| `GET` | `/api/workspace/v1/events/` | List durable realtime events for the current IAM user. |
| `GET` | `/api/workspace/v1/epoch/` | Return the current user's latest visible event epoch. |
| `GET` | `/api/workspace/v1/users/` | List workspace users. |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | Get a workspace user. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | Update current user's presence status and heartbeat timestamp. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | Upload and select the current user's avatar. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | Remove the current user's custom avatar and restore the canonical Gravatar URN. |
| `GET` | `/api/workspace/v1/me/` | Return the current authenticated Workspace user. |

## Server Settings

`GET /api/workspace/v1/messenger/server_settings` is public and does not require `Authorization`. It is
implemented by middleware and does not use the resource router. Unsupported
query parameters are reported in
`ignored_parameters_unsupported`. `realm_url` and `realm_uri` are derived from
the request `Host` header and `X-Forwarded-Proto` when a reverse proxy provides
it.

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
  "realm_url": "https://workspace.example.com",
  "realm_name": "Exordos Workspace",
  "realm_icon": "",
  "realm_description": "<p>Exordos Workspace messenger.</p>",
  "realm_web_public_access_enabled": false,
  "meet_url": "https://meet.genesis-core.tech",
  "external_authentication_methods": [],
  "realm_uri": "https://workspace.example.com"
}
```

## Folders

`POST /api/workspace/v1/messenger/folders/` writes to `m_folders`. Reads use `m_folders_view`.
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
POST /api/workspace/v1/messenger/folders/
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
PUT /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Archive",
  "background_color_value": 4289352960
}
```

Delete example:

```http
DELETE /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
```

Realtime side effects:

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| create folder | `folder.created` | `folder` | Full folder snapshot. |
| update folder | `folder.updated` | `folder` | Full folder snapshot. |
| delete folder | `folder.deleted` | `folder` | Only `folder.uuid`. |

## Folder Items

`POST /api/workspace/v1/messenger/folder_items/` writes to `m_folder_items`. Reads use
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
POST /api/workspace/v1/messenger/folder_items/
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
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/pin/invoke
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
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/unpin/invoke
Authorization: Bearer <access_token>
```

Delete example:

```http
DELETE /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50
Authorization: Bearer <access_token>
```

Realtime side effects:

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| add stream to folder | `folder.updated` | `folder` | Full parent folder snapshot with `folder_items`. |
| pin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with updated `pinned_at`. |
| unpin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with `pinned_at: null`. |
| remove stream from folder | `folder_item.deleted` | `folder_item` | Only `folder_item.uuid`. |

## Streams

`POST /api/workspace/v1/messenger/streams/` appends the canonical stream,
binding, and default-topic operations to the IMAP journal before updating the
PostgreSQL projection. It creates a default topic named `General Topic` and
stores its UUID as `default_topic_uuid`.
The reference is nullable and becomes `null` when the current default topic is
deleted. REST resource responses follow the standard RestAlchemy JSON packer
and omit nullable fields whose value is `null`, so clients must also treat a
missing `default_topic_uuid` as `null`. Durable `stream.updated` events are full
snapshots and keep `default_topic_uuid: null` explicitly.

If `direct_user_uuid` is provided, the backend creates an ordinary stream with
the same bindings, roles, topics, events, and file ACL rules as every other
chat. Its only additional invariants are `private: true`, exactly two distinct
IAM participants, a deterministic project-scoped stream UUID for the unordered
pair, and `owner` bindings for both users. Repeating the same request for the
same pair returns the existing stream.

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
    "stream_id": 123,
    "server_url": "https://zulip.example.com",
    "topic_name": null,
    "message_id": null
  }
}
```

The `zulip` payload shape is reserved contract metadata for a future provider.
No Zulip runtime or access-account gate exists in this release. When provider
transport is implemented, it will populate these fields from SMTP/IMAP data;
the browser contract will continue to hide raw mail protocol identifiers.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Stream identifier. |
| `name` | string | yes | no | Stream name. |
| `description` | string | yes | no | Stream description. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `owner` | UUID | no | yes | Owner from the user stream view. |
| `user_uuid` | UUID | no | yes | Current user in the user stream view. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | no | yes | Current user's role. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | no | user-scoped action-managed | Current user's stream notification mode; defaults to `all_messages`. |
| `unread_count` | integer | no | yes | Current user's unread count. |
| `source_name` | `native`, `zulip` | yes | no | Source name. |
| `source` | object | yes | no | Source payload. |
| `invite_only` | boolean | no | no | Invite-only stream flag. |
| `announce` | boolean | no | no | Announcement stream flag. |
| `direct_user_uuid` | UUID | no | no | Other direct-chat participant. |
| `private` | boolean | no | yes | Private stream flag. |
| `is_archived` | boolean | no | action-managed | Archived flag. |
| `color` | integer `0..0xFFFFFF` | no | no | Stream color; generated randomly when omitted or `null`. |
| `last_message_uuid` | UUID or `null` | no | yes | Latest message in the stream, or `null` when empty. |
| `default_topic_uuid` | UUID or `null` | no | yes | Current default topic UUID, or `null` when no default is configured. |
| `provider` | object or `null` | no | yes | Provider badge for provider-backed streams; `null` for native streams. |
| `delivery` | object or `null` | no | yes | Current provider command delivery projection. |
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

Stream notification mode request:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/notifications/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "notification_mode": "mentions_only"
}
```

Native stream mutations append durable IMAP journal operations before updating
the rebuildable projection. The nullable `provider` and `delivery` fields stay
reserved for a future mail-protocol provider and are `null` for native streams.

Stream read action:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` marks all unread messages in the stream as read for the current user and
returns the updated stream view.

Realtime side effects:

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| create stream | `stream.created` | `stream` | Full user stream snapshot. |
| create stream | `folder.updated` | `folder` | Updated `All chats` and `Channels`/`Personal` system folder snapshots. |
| update stream | `stream.updated` | `stream` | Full user stream snapshot for every stream user. |
| archive or unarchive stream | `stream.updated` | `stream` | Full user stream snapshot for every stream user. |
| change stream notification mode | `stream.updated` | `stream` | Full user stream snapshot for the current user only. |
| read stream messages | `stream.read` | `stream` | Full user stream snapshot returned by the action. |
| read stream messages | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Updated unread-count snapshots for the current user. |
| delete stream | `stream.deleted` | `stream` | Only deleted stream `uuid`, sent to every stream user. |
| delete stream | `folder.updated` | `folder` | Updated affected users' system/custom folder snapshots after the stream is removed. |
| add stream binding | `stream.created` | `stream` | Added user's full user stream snapshot. |
| add stream bindings | `stream_bindings.created` | `stream_binding` | New stream binding snapshots for existing stream participants. |
| add stream binding | `folder.updated` | `folder` | Updated added user's `All chats` and `Channels`/`Personal` system folder snapshots. |
| delete stream binding | `stream.deleted` | `stream` | Only stream `uuid`, sent to the removed user. |
| delete stream binding | `folder.updated` | `folder` | Updated removed user's system/custom folder snapshots after access is removed. |

For direct private streams, one `stream.created` event is written for each
participant. Stream creation also writes `folder.updated` events for each
participant's `All chats` folder and for `Personal` when the stream is private,
or `Channels` when it is not private.

## Stream Bindings

Stream bindings are canonical chat membership records in the IMAP journal and
are projected to PostgreSQL. New bindings are created through
`POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke`, where
the request body groups added users by role. `who_uuid` is always overwritten
with the current IAM user's UUID.
When a new binding is created, the added user receives a `stream.created`
event for the newly visible stream and `folder.updated` events for `All chats`
and either `Personal` or `Channels`, depending on the stream privacy. Existing
stream participants receive one `stream_bindings.created` event containing the
new binding snapshots for the whole added batch.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Binding identifier. |
| `project_id` | UUID | yes | no | Project scope. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `user_uuid` | UUID | yes | no | User receiving access. |
| `who_uuid` | UUID | no | yes | User that performed the action. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | no | no | Role; defaults to `member`. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | no | no | User's stream notification mode; defaults to `all_messages`. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Add users request:

```json
{
  "member": [
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444"
  ],
  "owner": [
    "55555555-5555-5555-5555-555555555555"
  ]
}
```

Deleting a binding removes that user's access to the stream. The removed user
receives `stream.deleted` and then `folder.updated` for affected system and
custom folders. Other stream users do not receive a binding-delete event.

## Stream Topics

`POST /api/workspace/v1/messenger/stream_topics/` journals the canonical topic
before projecting it and its per-user flags. Reads are scoped to the current
IAM user through current stream membership.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Topic identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `name` | string, max 128 | yes | no | Topic name. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `user_uuid` | UUID | no | yes | Current user in the topic view. |
| `color` | integer `0..0xFFFFFF` | no | no | Topic color; generated randomly when omitted or `null`. |
| `last_message_uuid` | UUID or `null` | no | yes | Latest message in the topic, or `null` when empty. |
| `unread_count` | integer | no | yes | Current user's unread count for the topic. |
| `is_default` | boolean | no | yes | Whether this topic UUID equals the stream's `default_topic_uuid`. |
| `is_done` | boolean | no | action-managed | Current user's done flag. |
| `notification_mode` | `mute`, `default`, `unmute`, `follow` | no | user-scoped action-managed | Current user's topic notification mode; defaults to `default`. |
| `source_name` | `native`, `zulip` | no | no | Topic source name; defaults to `native` when omitted. |
| `source` | object | no | no | Topic source payload. |
| `provider` | object or `null` | no | yes | Provider badge for provider-backed topics; `null` for native topics. |
| `delivery` | object or `null` | no | yes | Current provider command delivery projection. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "name": "Releases",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
}
```

`PUT /api/workspace/v1/messenger/stream_topics/{topic_uuid}` requires a body with `name`. The backend
checks that the current user has a binding to the topic's stream before
renaming the topic. Native changes append durable IMAP operations before the
rebuildable projection is updated. Provenance remains unchanged by a rename.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/toggle_done/` flips `is_done` for all
topic users and returns the current user's updated topic view.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` sets the
topic as its stream's default and returns the current user's updated topic
view. The operation is idempotent. A changed default emits `stream.updated`
for every stream user and `topic.updated` for the previous and new default
topics.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` sets the
current user's topic notification mode:

```json
{
  "notification_mode": "follow"
}
```

Allowed topic notification modes are `mute`, `default`, and `follow`. `unmute`
is allowed only when the current user's stream notification mode is `muted`.

Topic read action:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` marks all unread messages in the topic as read for the current user and
returns the updated topic view.

Realtime side effects:

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| create topic | `topic.created` | `topic` | Full user topic snapshot for every stream user. |
| rename topic | `topic.updated` | `topic` | Full user topic snapshot for every stream user. |
| toggle done | `topic.updated` | `topic` | Full user topic snapshot for every stream user. |
| set default topic | `stream.updated`, `topic.updated` | `stream`, `topic` | Updated stream snapshot and previous/new default topic snapshots for every stream user. |
| change topic notification mode | `topic.updated` | `topic` | Full user topic snapshot for the current user only. |
| read topic messages | `topic.read` | `topic` | Full user topic snapshot returned by the action. |
| read topic messages | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Updated unread-count snapshots for the current user. |
| delete topic | `topic.deleted` | `topic` | Deleted topic `uuid` and `stream_uuid`, sent to every stream user. Deleting the default topic also emits `stream.updated` with `default_topic_uuid: null`. |

## Messages

`POST /api/workspace/v1/messenger/messages/` validates current IMAP-derived
stream membership, delivers a canonical UTF-8 markdown message through local
SMTP to every current participant Maildir, and appends journal and per-recipient
event records before updating PostgreSQL projections. Reads are scoped to the
current IAM user.

The only supported message payload in v1 is markdown:

```json
{
  "kind": "markdown",
  "content": "Hello, workspace"
}
```

Workspace entity references inside markdown content use regular markdown link
syntax. The URL part is a Workspace URN:

| Entity | Markdown form | Notes |
| --- | --- | --- |
| user mention | `[Jane Doe](urn:user:<user-uuid>)` | Treated as a user tag/mention. |
| message link | `[See message](urn:message:<message-uuid>)` | Points to a Workspace message. |
| stream link | `[general](urn:stream:<stream-uuid>)` | Points to a Workspace stream. |
| topic link | `[deploys](urn:topic:<topic-uuid>)` | Points to a Workspace topic. |
| file link | `[report.pdf](urn:file:<file-uuid>?name=report.pdf)` | File/media URNs may include metadata query parameters. |
| image/video link | `![photo.png](urn:image:<file-uuid>?name=photo.png)` | Images and videos use `urn:image` / `urn:video`. |
| avatar/default image | `[avatar](urn:gravatar:<hash>)` | Same canonical Gravatar URN format as Workspace users; the hash is 32 or 64 hexadecimal characters. |
| external URL | `[site](urn:url:https://example.com)` | External `http` / `https` links are stored through `urn:url`. |

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Message identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `stream_uuid` | UUID | yes | no | Stream UUID. |
| `topic_uuid` | UUID | no | no | Topic UUID; omitted or `null` uses the stream default topic. The request fails with code `400001007` when the stream has no default. |
| `author_uuid` | UUID | no | yes | Message author. |
| `payload` | object | yes | no | Message payload. |
| `user_uuid` | UUID | no | yes | Current user in the user message view. |
| `read` | boolean | no | yes | Current user's read flag. Authors are created as read. |
| `pinned` | boolean | no | yes | Current user's pinned flag. |
| `starred` | boolean | no | yes | Current user's starred flag. |
| `is_own` | boolean | no | yes | Whether `author_uuid` equals the current user. |
| `reactions` | object | no | yes | Aggregated reaction counts keyed by `emoji_name`. |
| `source_name` | `native`, `zulip` | no | no | Message source name; defaults from the selected topic when omitted. |
| `source` | object | no | no | Message source payload; Zulip `message_id` can be `null` until outbound sync succeeds. |
| `provider` | object or `null` | no | yes | Provider badge inherited from the selected provider-backed stream. |
| `delivery` | object or `null` | no | yes | Current create/update/delete delivery projection. |
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
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": true,
  "reactions": {},
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

Update request:

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Edited text"
  }
}
```

`PUT /api/workspace/v1/messenger/messages/{message_uuid}` journals the updated root message payload and returns
the current user's message view. Only the message author can update the root
message. `DELETE /api/workspace/v1/messenger/messages/{message_uuid}` performs
an immediate hard delete: it marks and UID-expunges every copy delivered to the
original participant set, then appends a bodyless tombstone that preserves the
required message identity and provenance fields for `message.deleted`.

Read action:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` sets the current user's message flag to `true` and returns the updated
message view. If the message was unread, the backend emits `message.read` with
the full message snapshot and aggregate unread-count updates.

Read up to action:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read_up_to/invoke
Authorization: Bearer <access_token>
```

`read_up_to` marks unread messages in the same topic as read up to and
including the selected message's `created_at`, then returns the selected
message view.

Realtime side effects:

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| create message | `message.created` | `message` | Full user message snapshot for every stream user. |
| create unread message | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Updated unread-count snapshots for users where the new message is unread. |
| update message payload | `message.updated` | `message` | Full user message snapshot for every stream user. |
| create/update/delete reaction | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` | `message_reaction` | Reaction snapshot for the acting user. |
| create/update/delete reaction aggregate update | `message.updated` | `message` | Full user message snapshot with updated `reactions` for every stream user. |
| read message or read up to message | `message.read` | `message` | Full user message snapshot returned by the action. |
| read unread message | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Updated unread-count snapshots for the current user. |
| delete message | `message.deleted` | `message` | Deleted message `uuid`, `stream_uuid`, `topic_uuid`, `author_uuid`, `source_name`, and `source`, sent to every stream user. |
| delete unread message | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Updated unread-count snapshots for users where the deleted message was unread. |

## Message Reactions

Message reactions are canonical journal operations with a rebuildable SQL
projection. Reads are scoped to messages visible to the current IAM user.
Creating, updating, or
deleting a reaction emits a `message_reaction.*` event for the acting user and
`message.updated` events for every user that can see the message; the message
snapshot contains aggregated `reactions`.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | Reaction identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `message_uuid` | UUID | yes | no | Message being reacted to; must be visible to the current user. |
| `user_uuid` | UUID | no | yes | User that owns the reaction. |
| `emoji_name` | string, max 128 | yes | no | Emoji/reaction name. |
| `provider` | object or `null` | no | yes | Provider badge inherited from the target message. |
| `delivery` | object or `null` | no | yes | Current create/update/delete delivery projection. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

Create request:

```json
{
  "message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "emoji_name": "thumbs_up"
}
```

The same user cannot create duplicate reactions with the same `message_uuid`
and `emoji_name`. Any user that can see the message can list or get its
reactions. Only the reaction owner can update or delete that reaction.
These operations append `reaction.create`, `reaction.update`, and
`reaction.delete` journal records. Native responses retain `provider: null` and
`delivery: null`.

The `reactions` field on message views is an aggregate map:

```json
{
  "thumbs_up": 2,
  "eyes": 1
}
```

Reaction realtime payloads include `uuid`, `project_id`, `message_uuid`,
`user_uuid`, `emoji_name`, `source_name`, and `source`. For
`message_reaction.updated`, `old_message_uuid`, `old_emoji_name`,
`old_source_name`, and `old_source` describe the previous reaction target.

## Files

File bytes and a separate JSON sidecar are stored through the configured
messenger file storage backend. S3 is the deployed backend; the local backend
implements the same layout for tests. PostgreSQL rows and
`m_workspace_file_accesses` are rebuildable projections only.

The sidecar contains the file UUID, project UUID, owner UUID, display metadata,
content type, size, SHA-256, creation time, and an ACL rule. Chat files include
their stream UUID and use the dynamic stream-membership rule:

```json
{
  "acl": {
    "mode": "stream_members",
    "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
  }
}
```

The sidecar never contains a participant snapshot. Every chat-file list,
metadata, and download request checks the authenticated user against the
current stream bindings reconstructed from the IMAP journal. A newly added
participant gains access immediately; a removed participant loses it
immediately without an S3 rewrite.

Files intentionally visible throughout the authenticated Workspace use this
ACL instead:

```json
{
  "acl": {
    "mode": "public"
  }
}
```

`public` is not anonymous access. Metadata and bytes remain behind the
Workspace IAM middleware, and any request without a valid Workspace bearer
token is rejected. A valid Workspace bearer token may read or download a
`public` file regardless of project or stream membership. A `public` sidecar
must not contain `stream_uuid`; it retains `owner_uuid` and all integrity
metadata. Nginx rejects multipart requests larger than `50m` before they reach
`workspace-messenger-api`.

| Field | Type | Required on JSON create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | File identifier. |
| `project_id` | UUID | no | yes | IAM project scope; hidden in API responses. |
| `user_uuid` | UUID | no | yes | Owner/uploader. |
| `stream_uuid` | UUID or `null` | yes | no | Stream that owns a chat file. Required for JSON create and `stream_members` multipart uploads; omitted for multipart uploads with `acl.mode=public`. |
| `name` | string | yes | no | File display name. |
| `description` | string | yes | no | File description. |
| `content_type` | string | yes | no | MIME content type. |
| `size_bytes` | integer | yes | no | File size in bytes. |
| `hash` | string | yes | no | File hash, currently SHA-256 for multipart uploads. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

JSON metadata create request:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "example.txt",
  "description": "Example",
  "content_type": "text/plain",
  "size_bytes": 12,
  "hash": "abc"
}
```

Multipart upload request:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
stream_uuid=75309057-419c-4b12-a7c1-3932429ec4a6
name=example.txt
description=Example
```

An ordinary authenticated client uploads a Workspace-wide public file through
the same endpoint by sending the existing ACL object as JSON and omitting
`stream_uuid`:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
acl={"mode":"public"}
name=public-example.txt
description=Authenticated Workspace-wide file
```

For multipart uploads, `file` is required and exactly one scope must be
provided: either `stream_uuid`, or the JSON form field
`acl={"mode":"public"}`. Public uploads reject `stream_uuid`; stream uploads
retain the `stream_members` ACL. `name` defaults to the uploaded filename and
`description` defaults to an empty string. The backend stores the bytes, sets
`content_type` from the uploaded part, calculates `size_bytes`, and writes a
SHA-256 `hash`. Both modes preserve the same binary plus JSON sidecar layout and
the same `urn:file`, `urn:image`, or `urn:video` client contract.

`GET /api/workspace/v1/messenger/files/`, `GET /api/workspace/v1/messenger/files/{file_uuid}`, and
`GET /api/workspace/v1/messenger/files/{file_uuid}/actions/download` require file access. `PUT` and
`DELETE` require file ownership. Downloads return raw bytes with the stored
`Content-Type`, a `Content-Disposition` attachment filename, and a strong
`ETag` equal to the quoted SHA-256 `hash` exposed by file metadata. The binary
is immutable for its file UUID; metadata changes emit `file.updated`. Deleting an
owned file removes both its binary object and JSON sidecar after the canonical
file deletion is journaled.


## Events And Epoch

Events are durable records in the IMAP event journal and are scoped to the
affected `user_uuid`: message, stream, topic, folder, and user snapshots are
created per visible recipient, while delete events are sent only to users that
must remove local state. `m_workspace_events` is a rebuildable PostgreSQL read
projection. Only event records are retained for seven days; messages, files,
stream/topic state, and the canonical operation journal are never removed by
this policy. Pruning removes a contiguous oldest UID prefix so the retained
journal remains a complete suffix. `epoch_version` is monotonic within one
`epoch_generation` (the IMAP event mailbox UIDVALIDITY).

`GET /api/workspace/v1/events/` returns events sorted by `epoch_version` ascending by default.
REST `/events/` and websocket delivery use the same flat schema:
both read from the current user's visible IMAP-backed event surface.
`GET /api/workspace/v1/epoch/` uses that same surface.

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
    "payload": {"kind": "markdown", "content": "Hello"},
    "source_name": "native",
    "source": {"kind": "native"},
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

Top-level fields describe the event row only. `payload.kind` is the only `kind`.
Do not expect top-level `type`, `kind`, `stream_uuid`, or `topic_uuid`.

Message create/update events carry the same markdown payload stored on the
message. Entity links remain regular markdown links with `urn:user`,
`urn:message`, `urn:stream`, `urn:topic`, file/media, avatar, or URL URNs.

Create, update, read, and action events carry the same full object snapshot that
the current user receives from the corresponding REST endpoint/action response,
plus `payload.kind`. Delete events are minimal:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, `source`

`stream_bindings.created` is a batch action payload:

```json
{
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
```

Read actions emit `message.read`, `topic.read`, or `stream.read` with the full
action response object in `payload`. When unread counters change, the backend
also emits `topic.updated`, `stream.updated`, and `folder.updated` events so UI
badges can be updated from normal snapshots.

Supported values:

| object_type | action | payload.kind examples |
| --- | --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` | `message.created`, `message.updated`, `message.deleted`, `message.read` |
| `message_reaction` | `created`, `updated`, `deleted` | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` | `stream.created`, `stream.updated`, `stream.deleted`, `stream.read` |
| `stream_binding` | `created`, `updated`, `deleted` | `stream_bindings.created`, `stream_binding.updated`, `stream_binding.deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` | `topic.created`, `topic.updated`, `topic.deleted`, `topic.read` |
| `user` | `updated` | `user.updated` |
| `folder` | `created`, `updated`, `deleted` | `folder.created`, `folder.updated`, `folder.deleted` |
| `folder_item` | `deleted` | `folder_item.deleted` |
| `file` | `created`, `updated`, `deleted` | `file.created`, `file.updated`, `file.deleted` |

For strict catch-up after a processed cursor, use:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<saved_generation>&page_limit=500
```

`GET /api/workspace/v1/epoch/` returns the latest visible event cursor and the
oldest retained epoch for the current IAM user. `epoch_version` is the direct
alias of `current_epoch_version`:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

Clients persist `epoch_generation` together with `epoch_version`. A resume
cursor above zero without a generation, a changed generation, a future epoch,
or an epoch older than the retained suffix returns HTTP `410` with
`type=EventsCursorExpiredError`, `error=epoch_pruned`, the reason, and the
current/minimum cursor fields. The response is `Cache-Control: no-store`.
Clients then clear derived entity/blob caches, load authoritative snapshots,
and restart tracking from the returned generation; server messages and domain
data are not deleted.

## Workspace Users

Workspace users are stored in `m_workspace_users`. The route is global rather
than project-scoped.

`GET /api/workspace/v1/me/` returns the same `WorkspaceUser_Get` object as
`GET /api/workspace/v1/users/{user_uuid}`, using the user UUID from the IAM
token. The client does not send or derive a user UUID for this request. The
backend takes `project_id` from IAM introspection, refreshes the IAM-owned
username, first name, last name, and email projection, and returns the local
Workspace status, avatar, and presence fields.

When the current IAM user requests their own UUID, the API materializes or
refreshes the IAM identity projection before returning it. The browser cannot
submit source ownership fields. The `zulip` source literal remains reserved for
a future mail-protocol provider transport; no provider runtime exists now.

| Field | Type | Description |
| --- | --- | --- |
| `uuid` | UUID | User identifier. |
| `username` | string, 1..128 | Username. |
| `source` | `iam`, `zulip` | User source. |
| `status` | `active`, `idle`, `offline`, `do_not_disturb` | Presence status. |
| `status_emoji` | string or `null`, max 64 | Custom presence emoji. |
| `status_text` | string or `null`, max 256 | Custom presence text. |
| `first_name` | string or `null` | First name. |
| `last_name` | string or `null` | Last name. |
| `email` | string or `null` | Email address. |
| `avatar` | URN string | User avatar. Supported values are `urn:gravatar:<32-or-64-hex-hash>`, `urn:image:<uuid>`, and `urn:url:http(s)://...`. When omitted, Workspace hashes the normalized email with MD5; users without an email receive a non-reversible MD5 fallback derived from their UUID. |
| `last_ping_at` | datetime | Last ping timestamp. |
| `created_at` | datetime | Creation time. |
| `updated_at` | datetime | Update time. |

A future provider may project a Gravatar-compatible avatar as
`urn:gravatar:<md5(trim(lower(delivery_email)))>`. Raw provider identifiers and
mail addresses used only for transport are not exposed in this contract.

Presence update:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/presence/invoke
Content-Type: application/json

{
  "status": "active",
  "emoji": "coffee",
  "text": "Focusing"
}
```

The authenticated user may update only their own `user_uuid`. The request stores
the supplied status and the current time in `last_ping_at`. Optional `emoji` and
`text` fields are stored as `status_emoji` and `status_text`; omitted optional
fields keep previous values, and explicit `null` clears them. The Workspace messenger worker marks stale users offline and emits `user.updated` events with full user
snapshots, including `avatar`, to all Workspace users in every project.

Avatar upload is an atomic own-user action:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_upload/invoke
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<PNG, JPEG, GIF, or WebP binary part>
```

Only the authenticated user's own UUID is accepted. The maximum avatar size is
25 MiB. The backend validates the declared MIME type and binary signature,
stores the bytes and JSON sidecar through the configured file backend, sets
`acl.mode` to `public`, omits `stream_uuid`, and updates only `user.avatar` to
`urn:image:<file-uuid>`. IAM-owned username, name, and email fields remain
read-only. The action emits the full `user.updated` snapshot in every Workspace
project.

Resetting the avatar uses the same own-user authorization:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_reset/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

Reset replaces `user.avatar` with
`urn:gravatar:<md5(trim(lower(email)))>` or the canonical non-reversible UUID
fallback for a user without email. A replaced custom avatar loses public access
as soon as the user reference and projection row are updated; its binary and
sidecar are then removed from object storage.

## WebSocket Realtime Summary

The common websocket service uses subprotocol `workspace.events.v1` and authenticates
the bearer token from `Sec-WebSocket-Protocol`:

```ts
const ws = new WebSocket(
  "/api/workspace/v1/events/ws?last_epoch_version=124&epoch_generation=781203",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

After the connection is accepted, the server sends missed events newer than
the saved cursor. It then sends exactly one control frame
`{"type":"ready","epoch_generation":"...","epoch_version":124}` before any
live event can be delivered. UI notification gates remain closed until this
frame. Each event message is the same flat event object returned by REST
`/api/workspace/v1/events/`. The websocket service does not send
application-level JSON `hello` or `ping` messages and does not process client
JSON `pong` or `ack` messages. It sends protocol-level WebSocket ping control
frames at the configured heartbeat interval. Reconnect and catch-up are driven
by the persisted cursor pair. An expired cursor sends the same typed
`epoch_pruned` JSON error as REST and closes with code `4410` and reason
`epoch_pruned`.

For protected file caches, `file.created/updated/deleted` invalidates one UUID.
On membership removal the removed user receives `stream.deleted`; clients
immediately evict every protected blob whose cached metadata has that
`stream_uuid`. Remaining participants receive `stream_binding.deleted` (and
role/settings changes produce `stream_binding.updated`) to update participant
state. A 410 gap clears all derived protected-blob cache entries.

Detailed UI integration rules are documented in
`docs/workspace_ui_realtime_integration.md`.

## OpenAPI And Deployment

The runtime Workspace OpenAPI document is available at
`/api/workspace/specifications/3.0.3`. It describes the IAM-authenticated UI
surface. There is no Provider, Mail, or Calendar specification in this release.

The Workspace backend element installs independent `workspace-messenger-api`,
`workspace-api`, `workspace-messenger-events`, and
`workspace-messenger-worker` processes together with Exim4 and Dovecot on the
dedicated internal mail node.
The element requires S3aaS for binary objects and JSON sidecars and DBaaS for
the disposable PostgreSQL projection. It builds the existing Workspace UI in
Messenger-only mode and serves it from nginx.

Related documents:

- [Workspace architecture](architecture.md)
- [Workspace UI realtime integration](workspace_ui_realtime_integration.md)
