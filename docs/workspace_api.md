# Workspace v1 API

Workspace exposes one IAM-authenticated API for messenger, mail, calendar, and
their realtime events. The public gateway contract is rooted at:

```text
/api/workspace/v1
```

The service listens internally on `127.0.0.1:21081` with the `/v1` root. Nginx
maps the public root to that internal root. The websocket service listens on
`127.0.0.1:21082`.

## Contract decisions

- The backend database is authoritative for messenger, mail, and calendar data.
- SMTP, IMAP, and CalDAV are transport adapters implemented by the separate
  `workspace-groupware-bridge-worker` daemon.
- Browser clients never connect to SMTP, IMAP, CalDAV, or a UI-side proxy.
- All domains use the same Exordos Core IAM bearer token and project scope as
  messenger. Interactive login requests
  `openid email profile project:default`.
- The authenticated IAM user is materialized in `m_workspace_users` on the
  first `GET /users/{current_user_uuid}` request.
- Durable events for every domain use the common REST feed and websocket.
- This is a greenfield contract. Removed endpoints are not aliases and are not
  supported.

## Public routing

| Domain | Public base |
| --- | --- |
| Common users and services | `/api/workspace/v1` |
| Messenger | `/api/workspace/v1/messenger` |
| Mail | `/api/workspace/v1/mail` |
| Calendar | `/api/workspace/v1/calendar` |
| Events REST | `/api/workspace/v1/events/` |
| Events websocket | `/api/workspace/v1/events/ws` |

The public server-discovery endpoint is:

```text
GET /api/workspace/v1/messenger/server_settings
```

It is the only unauthenticated Workspace endpoint used by the UI. All resource
operations require `Authorization: Bearer <IAM access token>`.

## Removed routes

The gateway must not expose any of these historical layouts:

```text
/api/messenger/**
/api/v1/**
/workspace/**
/api/workspace/v1/messenger/events/ws
/api/workspace/v1/messenger/events/**
```

In particular, there is no compatibility redirect from the old messenger
websocket path. Clients connect only to `/api/workspace/v1/events/ws`.

## Common API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/workspace/v1/` | List top-level API domains. |
| `GET` | `/api/workspace/v1/me/` | Return the current IAM identity. |
| `GET` | `/api/workspace/v1/users/` | List Workspace user projections. |
| `GET` | `/api/workspace/v1/users/{uuid}` | Get a user; materialize the current IAM user when necessary. |
| `POST` | `/api/workspace/v1/users/{uuid}/actions/presence/invoke` | Update current-user presence. |
| CRUD | `/api/workspace/v1/external_users/` | Manage external service bindings. |
| `GET` | `/api/workspace/v1/services/` | List available Workspace services. |
| `GET` | `/api/workspace/v1/events/` | Read durable project/user events. |
| `GET` | `/api/workspace/v1/epoch/` | Read the current event epoch. |

`external_users` is the common binding collection. Mail and calendar bindings
are selected by `account_settings.kind` (`mail` or `calendar`). Responses never
return stored passwords: `account_settings.credentials` is `null` in API
output.

## Messenger API

Messenger resources live only under `/api/workspace/v1/messenger`:

- folders and folder items;
- streams, stream bindings, and stream topics;
- messages and message reactions;
- files and authenticated downloads.

The OpenAPI document is the source of truth for verbs, schemas, and action
paths. Representative paths are:

```text
GET  /api/workspace/v1/messenger/folders/
GET  /api/workspace/v1/messenger/streams/
GET  /api/workspace/v1/messenger/stream_topics/
GET  /api/workspace/v1/messenger/messages/
POST /api/workspace/v1/messenger/messages/
POST /api/workspace/v1/messenger/messages/{uuid}/actions/read/invoke
GET  /api/workspace/v1/messenger/files/{uuid}/actions/download
```

## Mail API

### Resources

```text
/api/workspace/v1/mail/folders/
/api/workspace/v1/mail/messages/
/api/workspace/v1/mail/attachments/
```

Mail messages are written to the local database before the bridge performs
external transport. `sync_status` records transport state. Sending and moving
are explicit actions:

```text
POST /api/workspace/v1/mail/messages/{uuid}/actions/send/invoke
POST /api/workspace/v1/mail/messages/{uuid}/actions/move/invoke
GET  /api/workspace/v1/mail/attachments/{uuid}/actions/download
```

A mail binding is created in the common collection:

```json
{
  "server_url": "https://mail.example.com",
  "account_settings": {
    "kind": "mail",
    "credentials": {
      "kind": "mail",
      "username": "user@example.com",
      "password": "application-password"
    },
    "email": "user@example.com",
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "imap_security": "tls",
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
    "smtp_security": "tls"
  }
}
```

The groupware bridge synchronizes remote folders/messages into the local
tables and sends locally queued messages through SMTP. The browser reads only
the local Workspace API.

SMTP connections use a bounded 30-second socket timeout for TLS and
plain/STARTTLS modes. A provider or network outage therefore marks the queued
message transport attempt as failed and lets the daemon continue its loop;
the worker must never remain indefinitely blocked in `connect()`.

## Calendar API

### Resources

```text
/api/workspace/v1/calendar/calendars/
/api/workspace/v1/calendar/events/
```

Calendar CRUD is local-first. Moving an event between calendars is an explicit
action:

```text
POST /api/workspace/v1/calendar/events/{uuid}/actions/move/invoke
```

A calendar binding is created in the common collection:

```json
{
  "server_url": "https://calendar.example.com",
  "account_settings": {
    "kind": "calendar",
    "credentials": {
      "kind": "calendar",
      "username": "user@example.com",
      "password": "application-password"
    },
    "principal_url": "https://calendar.example.com/principals/user/"
  }
}
```

The groupware bridge discovers CalDAV collections and synchronizes event
changes in both directions. Remote identifiers and ETags are transport
metadata; UI identity remains the local Workspace UUID.

## Realtime events

All services append durable rows to `m_workspace_events`. Every event is scoped
by `project_id` and `user_uuid`, receives a monotonically increasing
`epoch_version`, and is available through both transports:

```text
GET /api/workspace/v1/events/?epoch_version%3E=<last>&page_limit=500
WS  /api/workspace/v1/events/ws?last_epoch_version=<last>
```

The websocket uses the same bearer token and project scope as REST. A client
must persist the latest processed `epoch_version`, reconnect with
`last_epoch_version`, and use REST catch-up if a connection is interrupted.

Domain event kinds include:

```text
messenger.*
mail.folder.*
mail.message.*
calendar.calendar.*
calendar.event.*
user.presence.updated
```

Payloads contain the persisted resource representation needed by clients. The
websocket does not invent synthetic fallback models.

## OpenAPI

Runtime OpenAPI is served by the Workspace API application. The checked-in UI
client contract is generated from the same specification and uses internal
`/v1/...` paths with a configured public base of `/api/workspace`.

When the backend contract changes:

1. regenerate the OpenAPI JSON;
2. regenerate `@workspace/api` in `workspace_ui`;
3. run backend integration tests and UI typecheck/tests;
4. verify public Nginx paths, including the exact common websocket location.

## Deployment services

The backend package installs:

```text
workspace-api.service
workspace-groupware-bridge-worker.service
workspace-messenger-events.service
workspace-messenger-worker.service
workspace-nginx.service
```

`workspace-api` replaces the removed messenger-only API service. The groupware
bridge is an independent daemon and has no dependency on code from
`workspace_ui/packages/mail-proxy`.
