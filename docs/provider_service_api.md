# Workspace Provider Service v1 API

This document is the provider-facing API reference. For a complete integration
walkthrough with `curl` and JSON examples, see
[`provider_integration_guide.md`](provider_integration_guide.md). Service
boundaries and data ownership are defined in
[`architecture.md`](architecture.md).

## Base URL and security boundary

The Provider Service API is a separate process bound by default to:

```text
http://127.0.0.1:21083/v1
```

Runtime OpenAPI is generated at:

```text
http://127.0.0.1:21083/specifications/3.0.3
```

The bundled provider client defaults to the platform route:

```text
http://<workspace-backend>:21085/api/workspace-service/v1
```

The Workspace element exposes that prefix only on a separate
platform-internal nginx listener and proxies it to the loopback Service API.
The browser-facing listener on port `80` does not contain this route. Each
provider element imports the exported Workspace backend node and uses the
internal listener; providers never connect to the Workspace database.

The supplied deployment uses three independent manifests:

```text
exordos/manifests/workspace-zulip-provider.yaml.j2
exordos/manifests/workspace-mail-provider.yaml.j2
exordos/manifests/workspace-calendar-provider.yaml.j2
```

Each creates a dedicated compute node/image, PostgreSQL instance, database
user, and database, then starts exactly one provider binary. Its generated
`/etc/workspace-provider/provider.env` supplies the backend URL, stable provider
UUID, short display name, binary name, and provider-local `DATABASE_URL`.
Provider elements must not share that database.

V1 has no application-level authentication and does not accept an IAM bearer
token. Every route is namespaced by `provider_uuid`, but the UUID is not a
credential. The API must be reachable only from trusted provider runtimes.

## HTTP conventions

- JSON requests use `Content-Type: application/json`.
- Blob upload uses `multipart/form-data`.
- Collection filters use RESTAlchemy query operators, for example
  `updated_at%3E=<timestamp>`, plus `page_limit`.
- Entity PUT is an idempotent upsert at a caller-selected canonical UUID.
- Provider collections are filtered by the provider UUID from the path.
- Relationships use typed Workspace URNs.
- Standard errors use `{ "type", "code", "message" }` with the usual HTTP
  statuses: `400` validation, `404` missing or provider-owned lookup miss,
  `409` conflict, and `500` unexpected failure.

Runtime OpenAPI is useful for route discovery, but its generated model schemas
currently expose some controller-injected internal fields and omit the request
bodies of the account `status` and command `result` actions. The request shapes
in this document and the integration guide are the tested v1 caller contract.

### Endpoint summary

All paths below are relative to the private Provider Service base URL.

| Domain | Method | Path | Purpose |
| --- | --- | --- | --- |
| Registration | `GET` | `/v1/providers/` | List registered providers. |
| Registration | `GET`, `PUT` | `/v1/providers/{provider_uuid}` | Read or idempotently register one provider. |
| Accounts | `GET` | `/v1/providers/{provider_uuid}/external_accounts/` | List accounts assigned to the provider. |
| Accounts | `GET` | `/v1/providers/{provider_uuid}/external_accounts/{account_uuid}` | Read one assigned account including trusted settings. |
| Accounts | `POST` | `/v1/providers/{provider_uuid}/external_accounts/{account_uuid}/actions/status/invoke` | Report account access status. |
| Blobs | `GET`, `POST` | `/v1/providers/{provider_uuid}/blobs/` | List or upload provider-owned blobs. |
| Blobs | `GET`, `DELETE` | `/v1/providers/{provider_uuid}/blobs/{blob_uuid}` | Read or delete blob metadata/content. |
| Blobs | `GET` | `/v1/providers/{provider_uuid}/blobs/{blob_uuid}/actions/download` | Download blob bytes. |
| Mail | `GET` | `/v1/providers/{provider_uuid}/mail/{folders|messages}/` | List canonical Mail projections. |
| Mail | `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/mail/{folders|messages}/{entity_uuid}` | Read, upsert, or delete one Mail projection. |
| Calendar | `GET` | `/v1/providers/{provider_uuid}/calendar/{calendars|events}/` | List canonical Calendar projections. |
| Calendar | `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/calendar/{calendars|events}/{entity_uuid}` | Read, upsert, or delete one Calendar projection. |
| Messenger | `GET` | `/v1/providers/{provider_uuid}/messenger/{users|streams|topics|messages|reactions}/` | List canonical Messenger projections. |
| Messenger | `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/messenger/{resource}/{entity_uuid}` | Read, upsert, or delete one Messenger projection. |
| Messenger | `POST` | `/v1/providers/{provider_uuid}/messenger/messages/{entity_uuid}/actions/flags/invoke` | Synchronize one account user's read/starred flags. |
| Commands | `GET` | `/v1/providers/{provider_uuid}/{domain}/commands/` | Poll domain commands. |
| Commands | `GET` | `/v1/providers/{provider_uuid}/{domain}/commands/{command_uuid}` | Read one command. |
| Commands | `POST` | `/v1/providers/{provider_uuid}/{domain}/commands/{command_uuid}/actions/result/invoke` | Report a terminal result. |

### Error handling

| HTTP status | Meaning | Provider action |
| --- | --- | --- |
| `400` | Invalid field, URN, status, result, hash, or relationship | Fix payload; do not retry unchanged. |
| `404` | Resource absent or not owned by path provider/account | Refresh account/mapping; do not assume global absence. |
| `409` | Canonical/provider identity collision | Stop the affected entity and repair the mapping. |
| `408`, `425`, `429` | Transient request/rate condition | Retry with bounded exponential backoff and jitter. |
| `500`, `502`, `503`, `504` | Transient service failure | Retry safely; PUT/result reporting is replayable. |

Providers must cap retries and continue other accounts. Response bodies and
logs must be redacted before they are stored as `safe_error`.

## Registration

| Method | Path                            | Purpose                                            |
| ------ | ------------------------------- | -------------------------------------------------- |
| `GET`  | `/v1/providers/`                | List registrations visible to the trusted service. |
| `GET`  | `/v1/providers/{provider_uuid}` | Get one registration.                              |
| `PUT`  | `/v1/providers/{provider_uuid}` | Idempotently register or refresh a provider.       |

Registration body:

```json
{
  "name": "Mail.ru",
  "supported_kinds": ["mail"],
  "version": "1.0.0"
}
```

`name` is the short provider display name shown by the UI. It is not the kind.
`supported_kinds` must be a non-empty subset of `zulip`, `mail`, and `calendar`.
A provider can register more than one kind, but each bundled daemon runtime has
one kind and its own database.

Dynamic account schemas, operation capabilities, webhooks, and provider event
subscriptions are not part of v1 registration.

Registration responses contain the caller-selected `uuid`, short `name`,
`supported_kinds`, `version`, `enabled`, `last_seen_at`, and server timestamps.
Repeating PUT updates mutable registration metadata and liveness without
changing ownership of existing External Accounts or entities.

## External Accounts

External Accounts are created and edited only through the IAM-authenticated
Workspace UI API. Providers read and validate the accounts assigned to their
UUID.

| Method | Path                                                                                            | Purpose                   |
| ------ | ----------------------------------------------------------------------------------------------- | ------------------------- |
| `GET`  | `/v1/providers/{provider_uuid}/external_accounts/`                                              | List assigned accounts.   |
| `GET`  | `/v1/providers/{provider_uuid}/external_accounts/{external_account_uuid}`                       | Get one assigned account. |
| `POST` | `/v1/providers/{provider_uuid}/external_accounts/{external_account_uuid}/actions/status/invoke` | Report access status.     |

Useful collection filters are `account_type=<kind>`, `updated_at%3E=<timestamp>`,
and `page_limit=<n>`.

The trusted response shape is deliberately different from the UI response:

```json
{
  "uuid": "00000000-0000-0000-0000-000000000101",
  "kind": "mail",
  "settings": {
    "kind": "mail",
    "server_url": "https://mail.example.com",
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
  },
  "updated_at": "2026-07-15T09:00:00.000000Z",
  "status": "pending"
}
```

Supported access statuses are:

```text
pending
missing_credentials
confirmed
invalid_credentials
unavailable
```

Status request body:

```json
{
  "status": "unavailable",
  "safe_error": "Remote server is temporarily unavailable"
}
```

Reporting `confirmed` activates the account and clears the access error.
Credentials are returned only by this trusted API. The UI API and UI events
mask them as `null`. Application-level credential encryption is not implemented
in v1; providers and deployments must treat the private boundary and logs as
secret-bearing.

V1 account settings are backend-defined:

| Kind       | Settings                                                                                                                                                               |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `zulip`    | `kind`; optional credentials `{kind, login, token}`; `server_url` is folded into settings in the Service API response.                                                 |
| `mail`     | `kind`, `email`, `imap_host`, `smtp_host`; optional credentials `{kind, username, password}`; ports default to `993`/`465`; security is `tls`, `starttls`, or `plain`. |
| `calendar` | `kind`; optional credentials `{kind, username, password}`; `server_url` is folded into settings in the Service API response.                                           |

There is currently no accepted `principal_url` or `sent_folder` account field.
The Mail provider discovers the IMAP `\Sent` mailbox through SPECIAL-USE.

Status reports are idempotent. A repeated report with the same normalized
status and safe error does not authorize the provider to mutate another
provider's account. The `provider_uuid` in the route is always the ownership
filter; a UUID copied from another namespace returns a provider-owned lookup
miss.

## Blobs

| Method   | Path                                                               | Purpose                           |
| -------- | ------------------------------------------------------------------ | --------------------------------- |
| `GET`    | `/v1/providers/{provider_uuid}/blobs/`                             | List provider-owned blobs.        |
| `POST`   | `/v1/providers/{provider_uuid}/blobs/`                             | Upload a blob.                    |
| `GET`    | `/v1/providers/{provider_uuid}/blobs/{blob_uuid}`                  | Get blob metadata.                |
| `DELETE` | `/v1/providers/{provider_uuid}/blobs/{blob_uuid}`                  | Delete blob metadata and content. |
| `GET`    | `/v1/providers/{provider_uuid}/blobs/{blob_uuid}/actions/download` | Download bytes.                   |

Upload parts:

| Part                    | Required | Meaning                                                           |
| ----------------------- | -------- | ----------------------------------------------------------------- |
| `external_account_uuid` | yes      | Owning account assigned to the provider.                          |
| `file`                  | yes      | Binary content.                                                   |
| `name`                  | no       | Canonical display name; defaults to upload filename.              |
| `content_type`          | no       | MIME type; defaults to upload type or `application/octet-stream`. |
| `hash`                  | no       | Lowercase SHA-256; a mismatch returns `400`.                      |

The response includes `urn:file:<uuid>`, `name`, `content_type`, `size_bytes`,
and `hash`. Storage identifiers and account ownership fields are hidden.

## URN registry

| Entity             | URN                             |
| ------------------ | ------------------------------- |
| Blob               | `urn:file:<uuid>`               |
| Mail folder        | `urn:mail-folder:<uuid>`        |
| Mail message       | `urn:mail-message:<uuid>`       |
| Calendar           | `urn:calendar:<uuid>`           |
| Calendar event     | `urn:calendar-event:<uuid>`     |
| Messenger user     | `urn:messenger-user:<uuid>`     |
| Messenger stream   | `urn:messenger-stream:<uuid>`   |
| Messenger topic    | `urn:messenger-topic:<uuid>`    |
| Messenger message  | `urn:messenger-message:<uuid>`  |
| Messenger reaction | `urn:messenger-reaction:<uuid>` |

Typed relationship fields are validated by the backend:

- Mail: `folder_urn`; attachment item `urn`.
- Calendar: `calendar_urn`.
- Messenger: `owner_urn`, `stream_urn`, `topic_urn`, `author_urn`, and
  `message_urn`.

Markdown-capable canonical fields may contain Workspace URNs that the UI knows
how to render, including file, user, message, stream, and topic references.
Typed relationship fields and attachment entries listed above are ownership-
validated by the Service API. Providers must upload files and resolve all
remote references before placing their canonical URNs in markdown; raw remote
IDs and remote download URLs are not part of the UI contract.

## Mail resources

| Method                 | Path                                                        |
| ---------------------- | ----------------------------------------------------------- |
| `GET`                  | `/v1/providers/{provider_uuid}/mail/folders/`               |
| `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/mail/folders/{entity_uuid}`  |
| `GET`                  | `/v1/providers/{provider_uuid}/mail/messages/`              |
| `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/mail/messages/{entity_uuid}` |

Mail folder input fields are `external_account_uuid`, `provider_external_id`,
`path`, `name`, and optional `delimiter`, `special_use`, `unread_count`, and
`total_count`.

| Mail folder field | Type | Required | Notes |
| --- | --- | --- | --- |
| `external_account_uuid` | UUID | yes | Must be a Mail account assigned to the route provider. |
| `provider_external_id` | string | yes | Stable remote mailbox identifier. |
| `path` | string | yes | Canonical mailbox path. |
| `name` | string | yes | Display name. |
| `delimiter` | string | no | Defaults to `/`. |
| `special_use` | string or `null` | no | Canonical role such as inbox/sent/drafts/trash. |
| `unread_count` | integer >= 0 | no | Defaults to zero. |
| `total_count` | integer >= 0 | no | Defaults to zero. |

Mail message input fields are:

```text
external_account_uuid, provider_external_id, folder_urn,
from_address, to_addresses, cc_addresses, bcc_addresses, reply_to,
subject, snippet, body_html, body_text, message_id, references, sent_at,
seen, flagged, draft, attachments
```

| Mail message field group | Fields | Notes |
| --- | --- | --- |
| Identity | `external_account_uuid`, `provider_external_id` | Both are required and provider-scoped. |
| Location | `folder_urn` | Required typed URN owned by the same provider/account. |
| Addresses | `from_address`, `to_addresses`, `cc_addresses`, `bcc_addresses`, `reply_to` | Address arrays are canonical JSON arrays. |
| Content | `subject`, `snippet`, `body_html`, `body_text` | Provider normalizes remote MIME content before PUT. |
| Threading | `message_id`, `references` | Canonical string projections when available. |
| State | `sent_at`, `seen`, `flagged`, `draft` | `sent_at` is normalized to UTC. |
| Files | `attachments` | Each entry references an uploaded provider blob URN. |

An attachment entry has:

```json
{
  "urn": "urn:file:00000000-0000-0000-0000-000000000201",
  "name": "report.txt",
  "content_type": "text/plain",
  "content_id": "report-content-id",
  "size_bytes": 123,
  "hash": "sha256-hex"
}
```

Bytes must be uploaded first. Attachments are embedded in the message payload;
there is no provider `/mail/attachments` resource.

## Calendar resources

| Method                 | Path                                                             |
| ---------------------- | ---------------------------------------------------------------- |
| `GET`                  | `/v1/providers/{provider_uuid}/calendar/calendars/`              |
| `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/calendar/calendars/{entity_uuid}` |
| `GET`                  | `/v1/providers/{provider_uuid}/calendar/events/`                 |
| `GET`, `PUT`, `DELETE` | `/v1/providers/{provider_uuid}/calendar/events/{entity_uuid}`    |

Calendar input fields are `external_account_uuid`, `provider_external_id`,
`name`, and optional `color`.

| Calendar field | Type | Required | Notes |
| --- | --- | --- | --- |
| `external_account_uuid` | UUID | yes | Must be a Calendar account assigned to the route provider. |
| `provider_external_id` | string | yes | Stable remote collection identifier. |
| `name` | string | yes | Display name. |
| `color` | string or `null` | no | Provider-neutral UI color. |

Calendar event input fields are:

```text
external_account_uuid, provider_external_id, calendar_urn, uid,
summary, description, location, starts_at, ends_at, all_day,
recurrence, attendees, alarms, recurrence_id
```

Provider transport metadata such as CTag, sync token, ETag, raw source, raw ICS,
or CalDAV collection state is intentionally absent.

| Calendar event field group | Fields | Notes |
| --- | --- | --- |
| Identity | `external_account_uuid`, `provider_external_id`, `uid` | Provider identity and calendar UID. |
| Location | `calendar_urn` | Required typed URN in the same namespace. |
| Content | `summary`, `description`, `location` | Canonical user-visible values. |
| Time | `starts_at`, `ends_at`, `all_day` | Datetimes normalize to UTC before semantic comparison. |
| Recurrence | `recurrence`, `recurrence_id` | Canonical recurrence projection; raw ICS remains provider-owned. |
| Participation | `attendees`, `alarms` | Canonical JSON arrays. |

## Messenger resources

| Resource  | Collection and entity paths                                                            |
| --------- | -------------------------------------------------------------------------------------- |
| Users     | `/v1/providers/{provider_uuid}/messenger/users/` and `.../users/{entity_uuid}`         |
| Streams   | `/v1/providers/{provider_uuid}/messenger/streams/` and `.../streams/{entity_uuid}`     |
| Topics    | `/v1/providers/{provider_uuid}/messenger/topics/` and `.../topics/{entity_uuid}`       |
| Messages  | `/v1/providers/{provider_uuid}/messenger/messages/` and `.../messages/{entity_uuid}`   |
| Reactions | `/v1/providers/{provider_uuid}/messenger/reactions/` and `.../reactions/{entity_uuid}` |

Collections support `GET`; entities support `GET`, `PUT`, and `DELETE`.
Messenger providers use a `zulip` External Account and upload binary files
through the common blob API. There is no provider `/messenger/files` resource.

Minimum relationship order is:

```text
user -> stream(owner_urn) -> topic(stream_urn)
     -> message(stream_urn, topic_urn, author_urn)
     -> reaction(message_urn, author_urn)
```

Provider deletion of a messenger user marks the user offline and emits an
updated user projection instead of deleting the user row.

| Resource | Required provider fields | Required relationships | Principal canonical fields |
| --- | --- | --- | --- |
| User | `external_account_uuid`, `provider_external_id`, `username` | none | `email`, names, avatar, presence/status fields |
| Stream | `external_account_uuid`, `provider_external_id`, `name` | `owner_urn` | description, privacy/archive/notification fields |
| Topic | `external_account_uuid`, `provider_external_id`, `name` | `stream_urn` | done/default/notification projections |
| Message | `external_account_uuid`, `provider_external_id`, `payload` | `stream_urn`, `topic_urn`, `author_urn` | created timestamp and canonical markdown payload |
| Reaction | `external_account_uuid`, `provider_external_id`, `emoji_name` | `message_urn`, `author_urn` | canonical emoji name |

Messenger entity PUTs must follow dependency order. A missing relationship,
wrong URN type, provider mismatch, or External Account mismatch is a terminal
validation error rather than an instruction for the backend to create a
placeholder.

Message read and starred state is a per-user projection. Synchronize it after
the message exists:

```http
POST /v1/providers/{provider_uuid}/messenger/messages/{entity_uuid}/actions/flags/invoke
Content-Type: application/json

{
  "external_account_uuid": "00000000-0000-0000-0000-000000000101",
  "read": true,
  "starred": false
}
```

At least one of `read` or `starred` is required. The account must belong to the
path provider and to the same source scope as the message. Updating flags for
one External Account does not modify another user's flags, even when both
accounts connect to the same Zulip realm and share the canonical message.

## Identity and inbound idempotency

Mail and Calendar use this stable identity tuple:

```text
provider_uuid + external_account_uuid + provider_external_id
```

Messenger uses `provider_uuid + source_scope + provider_external_id`; for
Zulip, `source_scope` is the normalized realm URL. Separate External Accounts
for the same realm therefore reuse canonical Messenger entities while retaining
separate user bindings and flags. Relationships across different source scopes
are rejected.

The canonical Workspace UUID is also supplied in the entity path. Reusing a
stable identity with a different path UUID is a validation error.

For previously unknown Mail and Calendar objects, the bundled common client
derives UUIDv5 using the provider UUID as namespace and this name:

```text
<account_uuid>:<domain>:<resource>:<provider_external_id>
```

The bundled Zulip provider derives Messenger UUIDs from its realm scope,
domain, resource, and external ID, so two accounts on the same realm converge
on the same UUID.

For objects originating in Workspace, providers must persist the mapping
between the command `entity_urn` and the new remote ID, then reuse the UUID from
that URN for all later PUT and DELETE operations. Recomputing UUIDv5 would create
a duplicate identity.

An inbound PUT whose normalized canonical payload is unchanged is a no-op. It
does not update timestamps, delivery status, or the UI event stream. Mail
attachment order/content and UTC-normalized Calendar timestamps participate in
the semantic comparison. There is no optimistic-concurrency or ETag contract;
real changes are last-arrival-wins.

## Commands

Each domain exposes the provider's Workspace-to-remote command feed:

| Method | Path                                                                                   |
| ------ | -------------------------------------------------------------------------------------- |
| `GET`  | `/v1/providers/{provider_uuid}/{domain}/commands/?status=pending&page_limit=100`       |
| `GET`  | `/v1/providers/{provider_uuid}/{domain}/commands/{command_uuid}`                       |
| `POST` | `/v1/providers/{provider_uuid}/{domain}/commands/{command_uuid}/actions/result/invoke` |

`domain` is `messenger`, `mail`, or `calendar`. Pending commands are ordered by
`created_at` ascending.

Command response fields are:

```text
uuid, created_at, updated_at, provider_uuid, external_account_uuid,
domain, operation, entity_uuid, entity_urn, payload,
status, safe_error, completed_at
```

Supported operations:

| Domain    | Operations                                                                                                                                                                                      |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mail      | `folder.create`, `folder.update`, `folder.delete`, `message.create`, `message.update`, `message.delete`, `message.send`, `message.move`                                                         |
| Calendar  | `calendar.create`, `calendar.update`, `calendar.delete`, `event.create`, `event.update`, `event.delete`, `event.move`                                                                           |
| Messenger | `stream.update`, `stream.delete`, `topic.create`, `topic.update`, `topic.delete`, `message.create`, `message.update`, `message.delete`, `reaction.create`, `reaction.update`, `reaction.delete` |

For Messenger, the UI controllers create commands for provider-backed
message and reaction create/update/delete operations and for stream/topic
updates. Command payloads are canonical full entity snapshots: relationships
are URNs (`stream_urn`, `topic_urn`, `message_urn`, `author_urn`), never raw
foreign UUID fields. Delete commands retain the last snapshot even though the
UI projection is removed in the same transaction.

Provider ingress uses the Service API controllers directly and never creates a
command back to the same provider. Native Messenger entities have no
`provider_uuid` and never create provider commands. Stream/topic notification
mode, read state, archive state, done state, and default-topic selection are
Workspace user-view settings; they remain local because the current Messenger
provider contract has no matching operations. In particular, a notification
action must not be represented as a misleading `stream.update` or
`topic.update` command.

Result body:

```json
{
  "status": "delivered",
  "provider_external_id": "remote-entity-42"
}
```

or:

```json
{
  "status": "failed",
  "safe_error": "Remote service rejected the request"
}
```

Only `delivered` and `failed` are valid terminal results. A delivered result
clears `safe_error`. The optional remote ID updates the canonical mapping field.
The backend updates the entity's `delivery` projection and appends a UI event.

Polling has no lease, claim, ACK, or retry action. A pending command may be
returned repeatedly. Providers must persist terminal outcomes by command UUID
before reporting them and safely re-report the same outcome after an ambiguous
network response. Failed commands are terminal; a later UI mutation creates a
new command.

### Command state machine

```text
pending --result(delivered)--> delivered
pending --result(failed)-----> failed
```

There is no transition back to `pending`, no claim state, and no server-side
retry counter. Re-reporting the same terminal outcome is safe. Reporting a
different terminal outcome for an already completed command is a provider bug
and must not be used to rewrite remote history.

The provider-side durable order is:

1. load or create the command dedupe record;
2. execute the remote side effect only when no terminal result exists;
3. persist result and any remote-ID mapping in the provider database;
4. report the stored result to Workspace;
5. keep the dedupe record according to a retention policy longer than command
   replay and reconciliation windows.

## Reconciliation

There is no snapshot or reconciliation endpoint. A provider performs bounded
remote comparison using provider-scoped collection GETs, PUTs changed or
missing projections, and DELETEs objects confirmed absent remotely. Scheduling,
partition depth, cost, and mismatch history are provider-owned state.

Providers must implement pagination when reconciliation can exceed a collection
page. The current bundled helper uses finite defaults (`200` accounts, `500`
entities), so deployments must not assume an unbounded single-page snapshot.

## UI events are not a provider feed

Inbound provider changes and command results append Workspace UI events. The
IAM-authenticated REST and websocket event endpoints carry the same flat event
shape, including canonical `provider` and `delivery` objects.

Providers do not connect to `/api/workspace/v1/events/` or
`/api/workspace/v1/events/ws`. Their outbound feed is the domain command
collection described above.
