# Genesis Workspace Backend

Backend service for **Genesis Workspace**. It exposes a REST API for
managing user-specific folders and folder items (chat memberships) and
serves an OpenAPI 3.0.3 specification for the API.

This service is planned to evolve into a **full replacement for Zulip**
in the future. At the current stage, it is used to integrate features
that are **not available in Zulip** yet, while still reusing Zulip as
the authentication and user directory backend.

It also provides backend functionality for a new UI project:
[`genesis_workspace`](https://github.com/infraguys/genesis_workspace).

The service is built on:

- **Python** (3.8+)
- **restalchemy** as the web/ORM framework
- **PostgreSQL** as the database
- **bazooka** as HTTP client for integration with Zulip

Base URL in local development:

- `http://127.0.0.1:21080`

---

## Recent changes (Dec 2025)

- **Catalog services API**
  - New `Service` resource exposed under `/v1/services/`.
  - Backed by the `catalog_services` table.
  - Supports standard CRUD operations (see endpoints list below).
- **Global folder items listing**
  - New read-only endpoint `GET /v1/folder_items/` to list `FolderItem` objects across all folders.
  - The response is still user-scoped (uses Zulip user context).
- **Validation: only one `system_type=all` folder per user**
  - Creating or updating a folder with `system_type="all"` now fails if the same user already has another `all` folder.

Authentication and user scoping are delegated to Zulip via the
`/json/users/me` endpoint. The backend never stores credentials; it uses
incoming `Authorization` and/or `Cookie` headers to determine `user_id`
and scopes all data per user.

---

## Domain model

### Folder

Represents a user folder that groups chats (DMs, channels, groups) for
a single user.

**Fields**

- **uuid**: `UUID4`, primary key, read-only.
- **title**: `string`, required, 3–64 characters.
- **user_id**: `int`, internal field, required, 0 ≤ `user_id` ≤ 2^31−1.
  This field is not exposed via the public API and is always taken from
  Zulip (`/json/users/me`).
- **background_color_value**: `int | null` — ARGB color value.

  Bits are interpreted as:

  - Bits 24–31: alpha.
  - Bits 16–23: red.
  - Bits 8–15: green.
  - Bits 0–7: blue.

- **unread_messages**: `array[int]`, per-chat unread counters,
  default `[]`.
- **system_type**: `enum | null`, system folder type. Currently
  possible values:

  - `all`
  - `created`

- **created_at**: `datetime (UTC)`, read-only, default now.
- **updated_at**: `datetime (UTC)`, read-only, default now.

**Example JSON**

```json
{
  "uuid": "a9b28a50-c26e-11f0-a095-047c160cda6f",
  "title": "Team",
  "background_color_value": 4280391411,
  "unread_messages": [7, 8, 9],
  "system_type": "created",
  "created_at": "2025-10-16T10:20:30Z",
  "updated_at": "2025-10-16T10:20:30Z"
}
```

---

### FolderItem

Represents membership of a chat in a folder (many-to-many mapping
"folder → chat") for a single user.

**Fields**

- **uuid**: `UUID4`, primary key, read-only.
- **folder_uuid**: `UUID4` — virtual field bound to `Folder.uuid`.
- **user_id**: `int`, internal field, required, 0 ≤ `user_id` ≤ 2^31−1.
  Not exposed via the public API and always taken from Zulip
  (`/json/users/me`).
- **chat_id**: `int`, required — chat/recipient identifier.
- **order_index**: `int | null` — manual ordering (lower means higher).
- **pinned_at**: `datetime | null (UTC)` — when the item was pinned.
- **created_at**: `datetime (UTC)`, read-only, default now.
- **updated_at**: `datetime (UTC)`, read-only, default now.

**Example JSON**

```json
{
  "uuid": "a9b28a50-c26e-11f0-a095-047c160cda6f",
  "folder_uuid": "a9b28a50-c26e-11f0-a095-047c160cda6f",
  "chat_id": 14,
  "order_index": 1,
  "pinned_at": "2025-10-16T10:20:30Z",
  "created_at": "2025-10-16T10:20:30Z",
  "updated_at": "2025-10-16T10:20:30Z"
}
```

---

## API overview

The OpenAPI 3.0.3 specification is exposed at:

- `GET /specifications/3.0.3`

Main resources:

- `GET /v1/folders/` — list folders.
- `POST /v1/folders/` — create folder.
- `GET /v1/folders/{FolderUuid}` — get folder by UUID.
- `PUT /v1/folders/{FolderUuid}` — update folder.
- `DELETE /v1/folders/{FolderUuid}` — delete folder.
- `GET /v1/services/` — list catalog services.
- `POST /v1/services/` — create catalog service.
- `GET /v1/services/{ServiceUuid}` — get catalog service by UUID.
- `PUT /v1/services/{ServiceUuid}` — update catalog service.
- `DELETE /v1/services/{ServiceUuid}` — delete catalog service.
- `GET /v1/folders/{FolderUuid}/items/` — list items in a folder.
- `POST /v1/folders/{FolderUuid}/items/` — create folder item.
- `GET /v1/folders/{FolderUuid}/items/{FolderItemUuid}` — get item.
- `PUT /v1/folders/{FolderUuid}/items/{FolderItemUuid}` — update item.
- `DELETE /v1/folders/{FolderUuid}/items/{FolderItemUuid}` — delete item.
- `GET /v1/folder_items/` — list folder items across all folders (read-only).
- `POST /v1/folders/{FolderUuid}/items/{FolderItemUuid}/actions/pin/invoke`
  — pin item.
- `POST /v1/folders/{FolderUuid}/items/{FolderItemUuid}/actions/unpin/invoke`
  — unpin item.

Error responses follow the RESTAlchemy error format:

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

---

## Authentication

The backend determines the current user via a call to Zulip:

- `GET /json/users/me`

The request reuses incoming auth headers. Typical cookie-based auth:

```http
Cookie: django_language=en; __Host-csrftoken=<CSRF_TOKEN>;
  __Host-sessionid=<SESSION_ID>
```

You can also use `Authorization: <SCHEME> <TOKEN>` if your Zulip setup
supports it. The exact mechanism is delegated to Zulip; this service
only extracts `user_id` from the response.

### Public endpoints (no cookies required)

The following endpoints are **public** and do not require cookies or
authorization headers. They are excluded from user context resolution
in `UserContextMiddleware`:

- `GET /` — root, lists top-level routes.
- `GET /v1/` — lists collection routes under the main API version.
- `GET /specifications/` — lists available OpenAPI specifications.
- `GET /specifications/3.0.3` — returns the OpenAPI 3.0.3 document.

---

## User scoping and data partitioning

All domain data (folders and folder items) is **strictly partitioned by
user**. The user ID is taken from Zulip and stored in the request
context by `UserContextMiddleware`.

Controllers in `workspace/user_api/api/controllers.py` add additional
constraints on top of the models:

- `FolderController`:
  - On create, automatically sets `user_id` from the request context.
  - On get/filter/delete/update, always filters by both `uuid` and
    `user_id`, so one user cannot see or modify another user’s folders.
  - Hides `user_id` in API responses.
- `FolderItemController`:
  - Nested under a parent folder; on create, sets `user_id` from the
    request context.
  - On get/filter/delete/update, always filters by `folder` (parent
    resource), `uuid`, and `user_id`.
  - Hides both the internal `folder` relationship and `user_id` in
    API responses.
  - The `folder_uuid` setter ensures that a folder item can only be
    linked to a folder that belongs to the **same user** (it loads
    `Folder` by `uuid` and `user_id`).
- The database migration additionally enforces that there is at most one
  `FolderItem` per `(folder, chat_id)` pair.

As a result, all read/write operations for `Folder` and `FolderItem`
are scoped to the Zulip user associated with the incoming request.

## Example requests (curl)

All examples assume:

- Base URL: `http://127.0.0.1:21080`
- Auth via cookies (replace placeholders with real values):

```bash
export WORKSPACE_COOKIE="django_language=en; \
__Host-csrftoken=<CSRF_TOKEN>; \
__Host-sessionid=<SESSION_ID>"
```

### 1. Get OpenAPI specification

```bash
curl -X GET "http://127.0.0.1:21080/specifications/3.0.3"
```

### 2. List folders

```bash
curl -X GET \
  "http://127.0.0.1:21080/v1/folders/" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

**Sample response**

```json
[
  {
    "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
    "title": "team",
    "background_color_value": 4280391411,
    "unread_messages": [7, 8, 9],
    "system_type": "created",
    "created_at": "2025-10-16T10:20:30Z",
    "updated_at": "2025-10-16T10:20:30Z"
  }
]
```

### 3. Create folder

```bash
curl -X POST \
  "http://127.0.0.1:21080/v1/folders/" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "test-folder",
    "background_color_value": 4280391411,
    "unread_messages": [],
    "system_type": "created"
  }'
```

### 4. Get folder by UUID

```bash
FOLDER_UUID="50ecadd0-9823-4d97-b54c-806cc672c210"

curl -X GET \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 5. Update folder

```bash
curl -X PUT \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "updated-folder-title"
  }'
```

### 6. Delete folder

```bash
curl -X DELETE \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 7. List items in a folder

```bash
curl -X GET \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 8. Create folder item

```bash
curl -X POST \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "chat_id": 100
  }'
```

### 9. Get folder item

```bash
FOLDER_ITEM_UUID="426bab13-5702-493b-9780-430475700e4b"

curl -X GET \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/${FOLDER_ITEM_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 10. Update folder item

```bash
curl -X PUT \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/${FOLDER_ITEM_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "chat_id": 104,
    "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c211"
  }'
```

### 11. Pin folder item

```bash
curl -X POST \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/${FOLDER_ITEM_UUID}/actions/pin/invoke" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 12. Unpin folder item

```bash
curl -X POST \
  "http://127.0.0.1:21080/v1/folders/${FOLDER_UUID}/items/${FOLDER_ITEM_UUID}/actions/unpin/invoke" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 13. List catalog services

```bash
curl -X GET \
  "http://127.0.0.1:21080/v1/services/" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 14. Create catalog service

```bash
curl -X POST \
  "http://127.0.0.1:21080/v1/services/" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Zulip",
    "description": "Corporate messenger",
    "service_url": "https://zulip.example.com/",
    "icon": "https://zulip.example.com/static/images/logo/zulip-icon-128x128.png"
  }'
```

### 15. Get catalog service by UUID

```bash
SERVICE_UUID="50ecadd0-9823-4d97-b54c-806cc672c210"

curl -X GET \
  "http://127.0.0.1:21080/v1/services/${SERVICE_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 16. Update catalog service

```bash
curl -X PUT \
  "http://127.0.0.1:21080/v1/services/${SERVICE_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "Corporate messenger (updated)"
  }'
```

### 17. Delete catalog service

```bash
curl -X DELETE \
  "http://127.0.0.1:21080/v1/services/${SERVICE_UUID}" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

### 18. List folder items across all folders

```bash
curl -X GET \
  "http://127.0.0.1:21080/v1/folder_items/" \
  -H "Cookie: ${WORKSPACE_COOKIE}"
```

---

## Postman collection hints

If you import the provided Postman collection, replace any real cookie
values with placeholders like:

- `__Host-csrftoken=<CSRF_TOKEN>`
- `__Host-sessionid=<SESSION_ID>`

and configure them via Postman variables or environment settings instead
of hard-coding secrets into the collection.
