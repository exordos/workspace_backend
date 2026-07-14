# Workspace Messenger API

This document describes the current API contract implemented by
`workspace-messenger-api` and the companion `workspace-messenger-events`
websocket service. Background presence maintenance is performed by
`workspace-messenger-worker`. External integration bridge work runs in
`workspace-integration-bridge-worker`. Both workers are included in the
deployment manifest.

## Runtime Entry Points

Direct local services:

```text
REST API:       http://127.0.0.1:21081/v1
WebSocket API:  ws://127.0.0.1:21082/v1/events/ws
Worker:         workspace-messenger-worker
Bridge worker:  workspace-integration-bridge-worker
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
| `PUT` | `/v1/streams/{stream_uuid}` | Update a stream. |
| `DELETE` | `/v1/streams/{stream_uuid}` | Delete a stream for all stream users. |
| `POST` | `/v1/streams/{stream_uuid}/actions/add_users/invoke` | Add users to a stream by role. |
| `POST` | `/v1/streams/{stream_uuid}/actions/archive/invoke` | Set `is_archived: true`. |
| `POST` | `/v1/streams/{stream_uuid}/actions/unarchive/invoke` | Set `is_archived: false`. |
| `POST` | `/v1/streams/{stream_uuid}/actions/notifications/invoke` | Set current user's stream notification mode. |
| `POST` | `/v1/streams/{stream_uuid}/actions/read/invoke` | Mark all unread stream messages as read for the current user. |
| `GET` | `/v1/stream_bindings/` | List stream bindings. |
| `GET` | `/v1/stream_bindings/{binding_uuid}` | Get a stream binding. |
| `PUT` | `/v1/stream_bindings/{binding_uuid}` | Update a stream binding. |
| `DELETE` | `/v1/stream_bindings/{binding_uuid}` | Remove a user from a stream. |
| `GET` | `/v1/stream_topics/` | List topics visible to the current IAM user. |
| `POST` | `/v1/stream_topics/` | Create a topic. |
| `GET` | `/v1/stream_topics/{topic_uuid}` | Get a topic. |
| `PUT` | `/v1/stream_topics/{topic_uuid}` | Rename a topic; body must contain `name`. |
| `DELETE` | `/v1/stream_topics/{topic_uuid}` | Delete a topic. |
| `POST` | `/v1/stream_topics/{topic_uuid}/toggle_done/` | Toggle the shared `is_done` flag for all topic users. |
| `POST` | `/v1/stream_topics/{topic_uuid}/actions/notifications/invoke` | Set current user's topic notification mode. |
| `POST` | `/v1/stream_topics/{topic_uuid}/actions/set_default/invoke` | Make the topic its stream's default topic. |
| `POST` | `/v1/stream_topics/{topic_uuid}/actions/read/invoke` | Mark all unread topic messages as read for the current user. |
| `GET` | `/v1/messages/` | List messages visible to the current IAM user. |
| `POST` | `/v1/messages/` | Create a message. |
| `GET` | `/v1/messages/{message_uuid}` | Get a message. |
| `PUT` | `/v1/messages/{message_uuid}` | Update a message payload. |
| `DELETE` | `/v1/messages/{message_uuid}` | Delete a message. |
| `POST` | `/v1/messages/{message_uuid}/actions/read/invoke` | Mark message as read for the current user. |
| `POST` | `/v1/messages/{message_uuid}/actions/read_up_to/invoke` | Mark unread messages in the same topic up to this message as read. |
| `GET` | `/v1/message_reactions/` | List reactions for messages visible to the current IAM user. |
| `POST` | `/v1/message_reactions/` | Create a message reaction. |
| `GET` | `/v1/message_reactions/{reaction_uuid}` | Get a message reaction visible through message access. |
| `PUT` | `/v1/message_reactions/{reaction_uuid}` | Update the current user's reaction. |
| `DELETE` | `/v1/message_reactions/{reaction_uuid}` | Delete the current user's reaction. |
| `GET` | `/v1/files/` | List files visible to the current IAM user. |
| `POST` | `/v1/files/` | Create file metadata or upload multipart file data. |
| `GET` | `/v1/files/{file_uuid}` | Get a visible file metadata record. |
| `PUT` | `/v1/files/{file_uuid}` | Update an owned file metadata record. |
| `DELETE` | `/v1/files/{file_uuid}` | Delete an owned file and its access rows. |
| `GET` | `/v1/files/{file_uuid}/actions/download` | Download visible file bytes. |
| `GET` | `/v1/external_accounts/` | List current user's external accounts. |
| `POST` | `/v1/external_accounts/` | Create an external account binding. |
| `GET` | `/v1/external_accounts/{external_account_uuid}` | Get an external account binding. |
| `PUT` | `/v1/external_accounts/{external_account_uuid}` | Update an external account binding. |
| `DELETE` | `/v1/external_accounts/{external_account_uuid}` | Delete an external account binding. |
| `GET` | `/v1/events/` | List durable realtime events for the current IAM user. |
| `GET` | `/v1/epoch/` | Return the current user's latest visible event epoch. |
| `GET` | `/v1/users/` | List workspace users. |
| `GET` | `/v1/users/{user_uuid}` | Get a workspace user. |
| `POST` | `/v1/users/{user_uuid}/actions/presence/invoke` | Update current user's presence status and heartbeat timestamp. |
| `GET` | `/v1/me/` | List routes below `/v1/me/`. |

## Server Settings

`GET /v1/server_settings` is public and does not require `Authorization`. It is
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

| Operation | payload.kind | object_type | Payload |
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

| Operation | payload.kind | object_type | Payload |
| --- | --- | --- | --- |
| add stream to folder | `folder.updated` | `folder` | Full parent folder snapshot with `folder_items`. |
| pin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with updated `pinned_at`. |
| unpin stream in folder | `folder.updated` | `folder` | Full parent folder snapshot with `pinned_at: null`. |
| remove stream from folder | `folder_item.deleted` | `folder_item` | Only `folder_item.uuid`. |

## Streams

`POST /v1/streams/` writes to `m_workspace_streams`, creates owner bindings,
creates a default topic named `General Topic`, and stores its UUID in
`m_workspace_streams.default_topic_uuid`. Reads use `m_workspace_user_streams`.
The reference is nullable and becomes `null` when the current default topic is
deleted. REST resource responses follow the standard RestAlchemy JSON packer
and omit nullable fields whose value is `null`, so clients must also treat a
missing `default_topic_uuid` as `null`. Durable `stream.updated` events are full
snapshots and keep `default_topic_uuid: null` explicitly.

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
    "stream_id": 123,
    "server_url": "https://zulip.example.com",
    "topic_name": null,
    "message_id": null
  }
}
```

For Zulip source payloads, `stream_id` is required. `server_url`,
`topic_name`, and `message_id` may be `null` while the integration bridge is
still syncing the object. Zulip-backed streams should include `server_url`;
topics and messages inherit source data from their parent stream/topic, then
the bridge fills `topic_name` and `message_id` when the external Zulip state is
known.

External source visibility is gated by the current user's confirmed external
account access. `m_workspace_user_streams`, `m_workspace_user_topics_view`,
`m_workspace_user_messages_view`, unread counters, folder item views, and event
delivery only expose non-native source rows when there is a matching
`m_external_accounts` row with `access_status = confirmed`, populated
`account_settings.credentials`, the same `project_id`, `user_uuid`,
`account_type = source_name`, and matching source scope. For Zulip the source
scope is the normalized `server_url`; future providers can map their own source
scope into the same gate.

Folder and stream-binding event replay also follows the same stream visibility
gate. Historical folder snapshots that contain only hidden external streams are
not delivered while the account is unconfirmed, so hidden external unread flags
behave as read from the UI perspective without rewriting stored flags. Once the
external account is confirmed, the same stream/message state becomes visible and
the provider sync can refresh read/unread status normally.

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
POST /api/messenger/v1/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/notifications/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "notification_mode": "mentions_only"
}
```

Stream read action:

```http
POST /api/messenger/v1/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/read/invoke
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

Stream bindings are stored in `m_workspace_stream_bindings`. New bindings are
created through `POST /v1/streams/{stream_uuid}/actions/add_users/invoke`, where
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
| `color` | integer `0..0xFFFFFF` | no | no | Topic color; generated randomly when omitted or `null`. |
| `last_message_uuid` | UUID or `null` | no | yes | Latest message in the topic, or `null` when empty. |
| `unread_count` | integer | no | yes | Current user's unread count for the topic. |
| `is_default` | boolean | no | yes | Whether this topic UUID equals the stream's `default_topic_uuid`. |
| `is_done` | boolean | no | action-managed | Current user's done flag. |
| `notification_mode` | `mute`, `default`, `unmute`, `follow` | no | user-scoped action-managed | Current user's topic notification mode; defaults to `default`. |
| `source_name` | `native`, `zulip` | no | no | Topic source name; defaults to `native` when omitted. |
| `source` | object | no | no | Topic source payload. |
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

`POST /v1/stream_topics/{topic_uuid}/toggle_done/` flips `is_done` for all
topic users and returns the current user's updated topic view.

`POST /v1/stream_topics/{topic_uuid}/actions/set_default/invoke` sets the
topic as its stream's default and returns the current user's updated topic
view. The operation is idempotent. A changed default emits `stream.updated`
for every stream user and `topic.updated` for the previous and new default
topics.

`POST /v1/stream_topics/{topic_uuid}/actions/notifications/invoke` sets the
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
POST /api/messenger/v1/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/read/invoke
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

`POST /v1/messages/` writes to `m_workspace_messages`, creates per-recipient
rows in `m_workspace_user_message_flags`, and writes one durable workspace event
per recipient to `m_workspace_events`. Reads use
`m_workspace_user_messages_view` and are scoped to the current IAM user.
Message creation first checks the selected stream/topic through the same
user-facing visibility views. A client cannot create a message in a stream or
topic that is hidden because the corresponding external account access is not
confirmed.

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

`PUT /v1/messages/{message_uuid}` updates the root message payload and returns
the current user's message view. Only the message author can update the root
message. `DELETE /v1/messages/{message_uuid}` deletes the root message and
cascades per-user message flags through database foreign keys.

Read action:

```http
POST /api/messenger/v1/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` sets the current user's message flag to `true` and returns the updated
message view. If the message was unread, the backend emits `message.read` with
the full message snapshot and aggregate unread-count updates.

Read up to action:

```http
POST /api/messenger/v1/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read_up_to/invoke
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

Message reactions are stored in `m_workspace_message_reactions`. Reads are
scoped to messages visible to the current IAM user. Creating, updating, or
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

Files are stored in `m_workspace_files`; visibility is stored separately in
`m_workspace_file_accesses`. Creating a file grants access to every current
stream recipient. Users added to a stream later receive access to existing
stream files; users removed from a stream lose access to those stream files.

File bytes are stored through the configured messenger file storage backend.
Local storage uses `messenger_files.storage_path`, which defaults to
`/var/lib/workspace/messenger/files` and can be overridden with
`WORKSPACE_FILE_STORAGE_PATH`; S3 storage uses the `messenger_files_s3`
configuration section. Nginx rejects multipart requests larger than `50m`
before they reach `workspace-messenger-api`.

| Field | Type | Required on JSON create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | File identifier. |
| `project_id` | UUID | no | yes | IAM project scope; hidden in API responses. |
| `user_uuid` | UUID | no | yes | Owner/uploader. |
| `stream_uuid` | UUID | yes | no | Stream that owns the file access scope. |
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
POST /api/messenger/v1/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
stream_uuid=75309057-419c-4b12-a7c1-3932429ec4a6
name=example.txt
description=Example
```

For multipart uploads, `file` and `stream_uuid` are required. `name` defaults to
the uploaded filename and `description` defaults to an empty string. The backend
stores the bytes, sets `content_type` from the uploaded part, calculates
`size_bytes`, and writes a SHA-256 `hash`.

`GET /v1/files/`, `GET /v1/files/{file_uuid}`, and
`GET /v1/files/{file_uuid}/actions/download` require file access. `PUT` and
`DELETE` require file ownership. Downloads return raw bytes with the stored
`Content-Type` and a `Content-Disposition` attachment filename. File operations
do not currently emit durable workspace realtime events.

## External Accounts

External accounts are stored in `m_external_accounts` and scoped to the current
IAM `project_id` and `user_uuid`. Request `project_id` and `user_uuid` values
are ignored on create; the backend always writes scope values from the
authenticated context. Zulip credentials are checked against the external
provider before the row is inserted. New rows stay in `new`; the integration
bridge worker promotes accounts to `active`.
Zulip `user_info` is server-managed: clients must not send it in create or
update payloads. When credentials are updated, the backend fetches the Zulip
profile for those credentials and rejects the update if the returned `user_id`
does not match the account's stored `user_info.user_id`. IAM external accounts
are internal-only and cannot be created through the public API.

External account access is tracked separately from the lifecycle `status`.
`access_status = confirmed` means the backend has validated the account
credentials and user identity. Rows with `missing_credentials`,
`invalid_credentials`, or `unavailable` do not unlock external-source streams,
topics, messages, unread counters, folder items, or events for that user.
Access fields are server-managed: API payloads cannot force an account into the
confirmed state. For Zulip, the bridge periodically rechecks access with
`users/me`; successful checks keep the account confirmed, missing credentials
move it to `missing_credentials`, Zulip authentication errors move it to
`invalid_credentials`, and transport/server failures move it to `unavailable`.

| Field | Type | Required on create | Read-only | Description |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | no | yes | External account binding identifier. |
| `project_id` | UUID | no | yes | IAM project scope. |
| `user_uuid` | UUID | no | yes | Workspace user owner. |
| `server_url` | URL | yes | no | External provider server URL. |
| `source_scope` | string or `null` | no | yes | Provider source scope used by the visibility gate. Defaults to `server_url` for Zulip. |
| `account_type` | `zulip`, `iam` | no | no | External account provider type. |
| `status` | `new`, `active` | no | yes | Integration lifecycle status. |
| `access_status` | `missing_credentials`, `confirmed`, `invalid_credentials`, `unavailable` | no | yes | Server-managed credential/access status used for external-source visibility. |
| `access_checked_at` | datetime or `null` | no | yes | Last time the backend checked provider access. |
| `access_confirmed_at` | datetime or `null` | no | yes | Last successful provider access check. |
| `access_next_check_at` | datetime | no | yes | Next scheduled provider access check. |
| `access_last_error` | string or `null` | no | yes | Last access check error. |
| `account_settings` | object | yes | no | Provider-specific settings with a `kind` discriminator. |
| `created_at` | datetime | no | yes | Creation time. |
| `updated_at` | datetime | no | yes | Update time. |

The integration bridge worker tracks external user import progress in
`m_external_account_user_syncs`. Each row stores `account_type`, a unique
`server_url`, nullable `external_account_uuid`, `last_synced_at`, and
`next_sync_at`; this state is internal and has no public REST endpoint. The
external account create flow creates a sync row for the provider server when it
is missing. If the linked external account is deleted, `external_account_uuid`
is set to `null`. For rows that have never been synced, `next_sync_at` is set
to the current time. IAM users are synced directly into `m_workspace_users`;
there is no separate `workspace_iam_users` table.

Zulip account create request:

```json
{
  "server_url": "https://zulip.example.com",
  "account_settings": {
    "kind": "zulip",
    "credentials": {
      "kind": "zulip",
      "login": "user@example.com",
      "token": "zulip-token"
    }
  }
}
```

## Events And Epoch

Events are durable outbox rows stored in `m_workspace_events`. They are scoped to
the affected `user_uuid`: message, stream, topic, folder, and user snapshots are
created per visible recipient, while delete events are sent only to users that
must remove local state. `epoch_version` is a monotonically increasing cursor.

`GET /v1/events/` returns events sorted by `epoch_version` ascending by default.
REST `/events/` and websocket delivery use the same flat schema:
both read from the visible event surface, so external-source events are hidden
whenever the current user's matching external account access is not confirmed.
`GET /v1/epoch/` uses that same visible event surface.

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
badges can be updated from normal snapshots. Historical rows with
`payload.kind == "messages.read"` are preserved as best-effort legacy events
with `message_uuids`; new runtime events use the singular read kinds.

Supported values:

| object_type | action | payload.kind examples |
| --- | --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` | `message.created`, `message.updated`, `message.deleted`, `message.read` |
| `message_reaction` | `created`, `updated`, `deleted` | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` | `stream.created`, `stream.updated`, `stream.deleted`, `stream.read` |
| `stream_binding` | `created` | `stream_bindings.created` |
| `topic` | `created`, `updated`, `deleted`, `read` | `topic.created`, `topic.updated`, `topic.deleted`, `topic.read` |
| `user` | `updated` | `user.updated` |
| `folder` | `created`, `updated`, `deleted` | `folder.created`, `folder.updated`, `folder.deleted` |
| `folder_item` | `deleted` | `folder_item.deleted` |

The unification migration adds `schema_version`, `object_type`, and `action` to
`m_workspace_events`, backfills them from `payload.kind`, converts legacy
`stream_bindings.created` payloads to `items`, and leaves historical
`messages.read` rows readable as legacy payloads.

For strict catch-up after a processed cursor, use:

```http
GET /v1/events/?epoch_version%3E=<last_epoch_version>&page_limit=500
```

`GET /v1/epoch/` returns the latest visible event epoch for the current IAM user,
or `0` when there are no visible events:

```json
{"epoch_version": 124}
```

## Workspace Users

Workspace users are stored in `m_workspace_users`. The route is global rather
than project-scoped.

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

Zulip user synchronization always sets `avatar` to
`urn:gravatar:<md5(trim(lower(delivery_email)))>`, falling back to the Zulip
profile `email` only when `delivery_email` is empty. This matches the Gravatar
identifier generated by Zulip 12.1. A later sync also corrects the avatar of an
already matched Workspace user.

Migration `0095-fix-workspace-gravatar-avatar-urn-f75679.py` converts the
misspelled legacy `urn:gavatar:<user-uuid>` values in users, user events, and
avatar URNs embedded in message/event markdown. After the migration, the legacy
form is rejected rather than handled through a compatibility path.

Presence update:

```http
POST /api/messenger/v1/users/11111111-1111-1111-1111-111111111111/actions/presence/invoke
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
fields keep previous values, and explicit `null` clears them. The messenger
worker marks stale users offline and emits `user.updated` events with full user
snapshots, including `avatar`, to all Workspace users in every project.

## WebSocket Realtime Summary

The websocket service uses subprotocol `workspace.events.v1` and authenticates
the bearer token from `Sec-WebSocket-Protocol`:

```ts
const ws = new WebSocket(
  "/api/messenger/ws?last_epoch_version=124",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

After the connection is accepted, the server sends missed events newer than
`last_epoch_version`, then live events. Each websocket message is the same flat
event object returned by REST `/v1/events/`. The websocket service does not send
application-level JSON `hello` or `ping` messages and does not process client
JSON `pong` or `ack` messages. It sends protocol-level WebSocket ping control
frames at the configured heartbeat interval. Reconnect and catch-up are driven
by the persisted `last_epoch_version` cursor.

Detailed UI integration rules are documented in
`docs/workspace_ui_realtime_integration.md`.
