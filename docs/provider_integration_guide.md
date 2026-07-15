# Provider Integration Guide

This guide shows how to integrate a provider daemon with Workspace v1. It uses
the implemented contract and payloads covered by backend integration tests.
Refer to [`provider_service_api.md`](provider_service_api.md) for the complete
endpoint and field reference.

## 1. Choose runtime identity and connectivity

Create one stable UUID for one provider runtime. Do not regenerate it on
restart, and do not reuse the same provider database with another UUID or
Workspace backend.

For local development on the Workspace backend host:

```bash
export SERVICE_API=http://127.0.0.1:21083/v1
export PROVIDER_UUID=11111111-1111-4111-8111-111111111111
```

For a deployed provider element, the manifest supplies the exported Workspace
backend node as `WORKSPACE_BACKEND_URL`. The bundled client appends its default
Service API prefix:

```bash
export WORKSPACE_BACKEND_URL=http://<workspace-backend>:21085
export SERVICE_API="$WORKSPACE_BACKEND_URL/api/workspace-service/v1"
export PROVIDER_UUID=11111111-1111-4111-8111-111111111111
```

The Workspace element publishes `/api/workspace-service/v1/` only through its
platform-internal nginx listener on port `21085` and proxies it to
`127.0.0.1:21083/v1/`. The browser-facing listener on port `80` has no Service
API route. Do not send an IAM token: the Service API has no application auth in
v1 and relies on this trusted transport boundary.

The three supplied manifests configure the daemon through
`/etc/workspace-provider/provider.env`:

| Element | Binary | Provider kind | Database |
| --- | --- | --- | --- |
| `workspace-zulip-provider` | `workspace-zulip-provider` | `zulip` | Dedicated PostgreSQL instance/user/database |
| `workspace-mail-provider` | `workspace-mail-provider` | `mail` | Dedicated PostgreSQL instance/user/database |
| `workspace-calendar-provider` | `workspace-calendar-provider` | `calendar` | Dedicated PostgreSQL instance/user/database |

Each environment supplies `WORKSPACE_PROVIDER_BINARY`,
`WORKSPACE_PROVIDER_UUID`, `WORKSPACE_PROVIDER_NAME`, `WORKSPACE_BACKEND_URL`,
and `DATABASE_URL`. Do not point two elements at the same provider database.

## 2. Register idempotently

Register at every bootstrap. Repeating this PUT refreshes provider liveness and
does not create another provider.

```bash
curl -fsS \
  -X PUT "$SERVICE_API/providers/$PROVIDER_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<'JSON'
{
  "name": "Mail.ru",
  "supported_kinds": ["mail"],
  "version": "1.0.0"
}
JSON
```

Use a short own display name such as `Mail.ru` or `CASSI Zulip`. The UI badge
uses this value. A combined private provider can register multiple fixed kinds:

```json
{
  "name": "Company Groupware",
  "supported_kinds": ["mail", "calendar"],
  "version": "1.0.0"
}
```

V1 does not accept a provider-defined External Account schema. Workspace uses
fixed forms for `zulip`, `mail`, and `calendar`.

## 3. Create an External Account through the UI API

Normally Workspace UI performs this IAM-authenticated request. It is shown here
to make the integration boundary explicit.

```bash
export WORKSPACE_API=https://workspace.example.com/api/workspace/v1
export IAM_TOKEN='<access-token>'

curl -fsS \
  -X POST "$WORKSPACE_API/external_users/" \
  -H "Authorization: Bearer $IAM_TOKEN" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "provider_uuid": "$PROVIDER_UUID",
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
JSON
```

The UI response masks `account_settings.credentials` as `null`.

Calendar uses `server_url` plus fixed credentials:

```json
{
  "provider_uuid": "11111111-1111-4111-8111-111111111111",
  "server_url": "https://calendar.example.com",
  "account_settings": {
    "kind": "calendar",
    "credentials": {
      "kind": "calendar",
      "username": "user@example.com",
      "password": "application-password"
    }
  }
}
```

Zulip uses:

```json
{
  "provider_uuid": "11111111-1111-4111-8111-111111111111",
  "server_url": "https://chat.example.com",
  "account_settings": {
    "kind": "zulip",
    "credentials": {
      "kind": "zulip",
      "login": "user@example.com",
      "token": "zulip-api-key"
    }
  }
}
```

## 4. Discover and validate assigned accounts

Providers list only their own assigned accounts:

```bash
curl -fsS \
  "$SERVICE_API/providers/$PROVIDER_UUID/external_accounts/?account_type=mail&page_limit=200"
```

Store the returned account UUID in the provider database:

```bash
export ACCOUNT_UUID=22222222-2222-4222-8222-222222222222
```

Validate remote credentials and report a safe result:

```bash
curl -fsS \
  -X POST \
  "$SERVICE_API/providers/$PROVIDER_UUID/external_accounts/$ACCOUNT_UUID/actions/status/invoke" \
  -H 'Content-Type: application/json' \
  -d '{"status":"confirmed"}'
```

Failure example:

```bash
curl -fsS \
  -X POST \
  "$SERVICE_API/providers/$PROVIDER_UUID/external_accounts/$ACCOUNT_UUID/actions/status/invoke" \
  -H 'Content-Type: application/json' \
  -d '{"status":"invalid_credentials","safe_error":"Remote credentials were rejected"}'
```

Never place credentials, account settings, raw protocol responses, or secrets
inside `safe_error` or logs.

Refresh account assignments incrementally as well as at startup. Store the last
successfully observed `updated_at` cursor and paginate every response:

```bash
curl -fsS \
  "$SERVICE_API/providers/$PROVIDER_UUID/external_accounts/?updated_at%3E=$ACCOUNT_CURSOR&page_limit=200"
```

An account removed from the current assignment list must stop producing new
canonical writes and commands. Do not delete provider-local history
immediately: retain mappings and command dedupe records long enough to process
replays and diagnose reassignment safely.

## 5. Assign canonical UUIDs

For a remote object not previously seen by Workspace, derive a deterministic
UUIDv5:

```python
import uuid

provider_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
account_uuid = uuid.UUID("22222222-2222-4222-8222-222222222222")
provider_external_id = "INBOX:42"
name = f"{account_uuid}:mail:messages:{provider_external_id}"
entity_uuid = uuid.uuid5(provider_uuid, name)
```

The bundled `WorkspaceServiceClient.entity_uuid()` implements this algorithm.
The bundled Zulip provider uses a realm-scoped variant for Messenger entities:
it normalizes the realm URL, derives a provider-local realm UUID, and includes
that scope instead of the External Account UUID. Consequently, multiple users
with separate External Accounts on the same realm share canonical Messenger
entities while keeping their bindings and flags separate.

Do not apply this rule to an entity created by Workspace. An outbound command
already contains its canonical `entity_urn`; persist that URN with the remote ID
and reuse the UUID parsed from the URN for later inbound updates and deletes.

## 6. Push Mail changes

The Mail, Calendar, and Messenger sections are independent vertical slices.
For this section, `PROVIDER_UUID` must support `mail` and `ACCOUNT_UUID` must
identify a Mail External Account.

### Create or update a folder

```bash
export FOLDER_UUID=33333333-3333-4333-8333-333333333333

curl -fsS \
  -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/folders/$FOLDER_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "INBOX",
  "path": "INBOX",
  "name": "Inbox",
  "delimiter": "/",
  "special_use": "inbox"
}
JSON
```

The response includes:

```json
{
  "uuid": "33333333-3333-4333-8333-333333333333",
  "urn": "urn:mail-folder:33333333-3333-4333-8333-333333333333",
  "external_account_uuid": "22222222-2222-4222-8222-222222222222",
  "provider_external_id": "INBOX",
  "path": "INBOX",
  "name": "Inbox",
  "delimiter": "/",
  "special_use": "inbox"
}
```

### Upload an attachment

```bash
HASH=$(sha256sum ./report.txt | cut -d' ' -f1)

curl -fsS \
  -X POST "$SERVICE_API/providers/$PROVIDER_UUID/blobs/" \
  -F external_account_uuid="$ACCOUNT_UUID" \
  -F name=report.txt \
  -F content_type=text/plain \
  -F hash="$HASH" \
  -F file=@report.txt
```

Save the returned `urn:file:<uuid>` and metadata. A wrong supplied SHA-256
returns `400` and the entity must not be sent.

### Create or update a message

```bash
export MESSAGE_UUID=44444444-4444-4444-8444-444444444444
export BLOB_UUID=55555555-5555-4555-8555-555555555555

curl -fsS \
  -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/messages/$MESSAGE_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "5:INBOX:42",
  "folder_urn": "urn:mail-folder:$FOLDER_UUID",
  "from_address": "sender@example.com",
  "to_addresses": ["user@example.com"],
  "cc_addresses": [],
  "bcc_addresses": [],
  "subject": "Provider delivery",
  "body_text": "Inbound message",
  "sent_at": "2026-07-15T09:30:00Z",
  "seen": false,
  "flagged": false,
  "draft": false,
  "attachments": [
    {
      "urn": "urn:file:$BLOB_UUID",
      "name": "report.txt",
      "content_type": "text/plain",
      "content_id": "report-content-id",
      "size_bytes": 123,
      "hash": "$HASH"
    }
  ]
}
JSON
```

Submitting the same normalized payload again returns the entity but does not
change timestamps or append another UI event.

Delete a confirmed-absent remote message using its canonical UUID:

```bash
curl -fsS \
  -X DELETE \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/messages/$MESSAGE_UUID"
```

## 7. Push Calendar changes

Before running these examples, set `PROVIDER_UUID` to a provider that supports
`calendar` and `ACCOUNT_UUID` to its Calendar External Account. Do not reuse a
Mail account UUID.

```bash
export CALENDAR_UUID=66666666-6666-4666-8666-666666666666

curl -fsS \
  -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/calendar/calendars/$CALENDAR_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "primary",
  "name": "Primary",
  "color": "#3366ff"
}
JSON
```

```bash
export EVENT_UUID=77777777-7777-4777-8777-777777777777

curl -fsS \
  -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/calendar/events/$EVENT_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "remote-event-42",
  "calendar_urn": "urn:calendar:$CALENDAR_UUID",
  "uid": "remote-event-42@example.com",
  "summary": "Architecture review",
  "description": "Review the provider contract",
  "location": "Online",
  "starts_at": "2026-07-16T12:00:00Z",
  "ends_at": "2026-07-16T13:00:00Z",
  "all_day": false,
  "attendees": [{"email": "reviewer@example.com"}],
  "alarms": []
}
JSON
```

The backend normalizes timestamps to UTC for semantic idempotency. Keep CalDAV
ETag, sync token, CTag, raw ICS, and collection state in the provider database;
they are not accepted canonical fields.

## 8. Push Messenger changes

Before running these examples, set `PROVIDER_UUID` to a provider that supports
`zulip` and `ACCOUNT_UUID` to its Zulip External Account. Create dependencies in
order.

### User

```bash
export USER_UUID=88888888-8888-4888-8888-888888888888

curl -fsS -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/users/$USER_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "user:42",
  "username": "Remote Author",
  "email": "author@example.com"
}
JSON
```

### Stream and topic

```bash
export STREAM_UUID=99999999-9999-4999-8999-999999999999

curl -fsS -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/streams/$STREAM_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "stream:7",
  "owner_urn": "urn:messenger-user:$USER_UUID",
  "name": "Provider stream",
  "description": "Delivered by a provider"
}
JSON
```

```bash
export TOPIC_UUID=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa

curl -fsS -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/topics/$TOPIC_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "topic:general",
  "stream_urn": "urn:messenger-stream:$STREAM_UUID",
  "name": "General"
}
JSON
```

### Message and reaction

```bash
export CHAT_MESSAGE_UUID=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb

curl -fsS -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/messages/$CHAT_MESSAGE_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "message:100",
  "stream_urn": "urn:messenger-stream:$STREAM_UUID",
  "topic_urn": "urn:messenger-topic:$TOPIC_UUID",
  "author_urn": "urn:messenger-user:$USER_UUID",
  "payload": {"kind": "markdown", "content": "Inbound message"},
  "created_at": "2026-07-14T23:45:54.000000Z"
}
JSON
```

Synchronize the current account user's message flags independently from the
shared message projection:

```bash
curl -fsS -X POST \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/messages/$CHAT_MESSAGE_UUID/actions/flags/invoke" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "read": true,
  "starred": false
}
JSON
```

```bash
export REACTION_UUID=cccccccc-cccc-4ccc-8ccc-cccccccccccc

curl -fsS -X PUT \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/reactions/$REACTION_UUID" \
  -H 'Content-Type: application/json' \
  --data @- <<JSON
{
  "external_account_uuid": "$ACCOUNT_UUID",
  "provider_external_id": "reaction:5",
  "message_urn": "urn:messenger-message:$CHAT_MESSAGE_UUID",
  "author_urn": "urn:messenger-user:$USER_UUID",
  "emoji_name": "thumbs_up"
}
JSON
```

Cross-provider relationships, wrong URN types, and provider identity collisions
are rejected.

## 9. Poll and deliver Workspace commands

After processing inbound changes, poll the command collection for the provider
kind. There is no provider websocket.

```bash
curl -fsS \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/commands/?status=pending&page_limit=100"
```

Example command:

```json
{
  "uuid": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  "provider_uuid": "11111111-1111-4111-8111-111111111111",
  "external_account_uuid": "22222222-2222-4222-8222-222222222222",
  "domain": "mail",
  "operation": "message.update",
  "entity_uuid": "44444444-4444-4444-8444-444444444444",
  "entity_urn": "urn:mail-message:44444444-4444-4444-8444-444444444444",
  "payload": {
    "uuid": "44444444-4444-4444-8444-444444444444",
    "folder_urn": "urn:mail-folder:33333333-3333-4333-8333-333333333333",
    "subject": "Changed in Workspace"
  },
  "status": "pending",
  "safe_error": null,
  "completed_at": null
}
```

For a Messenger provider, poll the Messenger feed instead:

```bash
curl -fsS \
  "$SERVICE_API/providers/$PROVIDER_UUID/messenger/commands/?status=pending&page_limit=100"
```

An edited Workspace message is delivered as a canonical snapshot with URN
relationships:

```json
{
  "uuid": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  "domain": "messenger",
  "operation": "message.update",
  "entity_urn": "urn:messenger-message:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "payload": {
    "urn": "urn:messenger-message:bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "stream_urn": "urn:messenger-stream:99999999-9999-4999-8999-999999999999",
    "topic_urn": "urn:messenger-topic:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "author_urn": "urn:messenger-user:eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    "payload": {"kind": "markdown", "content": "Edited in Workspace"}
  },
  "status": "pending"
}
```

The UI bridge currently emits Messenger commands for message and reaction
create/update/delete and stream/topic update. User-scoped notification and read
actions, stream archive state, topic done state, and default-topic selection do
not have provider operations and must not be inferred from the command feed.
Native entities never appear in it. Service API ingress also never loops an
inbound change back into the outbound feed.

Before calling the remote service:

1. look up the command UUID in the provider dedupe table;
2. if terminal, re-report the stored result;
3. otherwise perform the remote operation;
4. persist the terminal result and remote mapping in the provider transaction;
5. report it to Workspace.

Delivered:

```bash
export COMMAND_UUID=dddddddd-dddd-4ddd-8ddd-dddddddddddd

curl -fsS \
  -X POST \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/commands/$COMMAND_UUID/actions/result/invoke" \
  -H 'Content-Type: application/json' \
  -d '{"status":"delivered","provider_external_id":"5:Sent:128"}'
```

Failed:

```bash
curl -fsS \
  -X POST \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/commands/$COMMAND_UUID/actions/result/invoke" \
  -H 'Content-Type: application/json' \
  -d '{"status":"failed","safe_error":"Remote service rejected the request"}'
```

Polling has no lease. The same pending command can be returned repeatedly. A
`failed` result is terminal and is not automatically redelivered; a later UI
mutation creates a new command.

The bundled HTTP client retries network errors and statuses `408`, `425`, `429`,
`500`, `502`, `503`, and `504` up to four attempts with exponential jitter.
Validation and ownership errors require a payload or configuration fix.

### Crash-safe command pseudocode

```python
for command in service.list_commands(domain, status="pending"):
    stored = provider_db.get_command_result(command["uuid"])
    if stored is None:
        try:
            remote_id = deliver_to_remote(command)
            stored = {"status": "delivered"}
            if remote_id is not None:
                stored["provider_external_id"] = remote_id
        except PermanentRemoteError as exc:
            stored = {"status": "failed", "safe_error": exc.safe_message}

        # Commit before the HTTP report. A crash after this point re-reports the
        # same outcome and never repeats the remote side effect.
        provider_db.store_command_result(command["uuid"], stored)

    service.report_command_result(domain, command["uuid"], stored)
```

If the provider cannot determine whether a remote request succeeded, it must
query the remote system using an idempotency key or canonical mapping before
retrying. Reporting `failed` merely because the Workspace result response was
lost can create remote/Workspace divergence.

## 10. Reconcile bounded history

There is no snapshot endpoint. Reconciliation uses normal provider collections:

```bash
curl -fsS \
  "$SERVICE_API/providers/$PROVIDER_UUID/mail/messages/?external_account_uuid=$ACCOUNT_UUID&page_limit=500"
```

Compare a bounded remote partition with the canonical list, then:

- PUT objects missing or different in Workspace;
- DELETE only objects confirmed absent from the matching remote partition;
- store partition cursor, inspected depth, cost, and mismatch count in the
  provider database;
- paginate rather than assuming one response contains the full account.

After drift, schedule the partition sooner and inspect deeper history. After
clean runs, back off and eventually reduce depth. This is provider-local policy;
the Service API remains entity-oriented.

One practical partition record contains:

```json
{
  "external_account_uuid": "22222222-2222-4222-8222-222222222222",
  "domain": "mail",
  "partition": "INBOX",
  "remote_cursor": "uidvalidity:123/last_uid:456",
  "last_checked_at": "2026-07-15T09:30:00Z",
  "next_check_at": "2026-07-15T10:00:00Z",
  "depth": 16,
  "clean_runs": 3,
  "last_mismatch_count": 0
}
```

The exact schema is provider-owned. The invariant is that a DELETE is issued
only when absence is proven inside the same completely inspected partition;
an interrupted or truncated remote listing is never evidence of deletion.

## 11. Run the provider loop

A production daemon combines the earlier steps without turning each cycle into
a complete rescan:

```text
bootstrap:
  open and validate provider database binding
  PUT provider registration
  refresh assigned accounts

repeat with bounded sleeps:
  refresh changed account assignments
  for each enabled account independently:
    validate access when due
    consume bounded incremental remote changes
    upload required blobs, then PUT canonical entities in dependency order
    poll and deliver bounded pending commands
    reconcile one dynamically selected partition when due
  persist cursors and per-account failures
```

The supplied deployment packages this runtime as one of three separate
Exordos elements: `workspace-zulip-provider`, `workspace-mail-provider`, or
`workspace-calendar-provider`. Each manifest creates its own compute image,
PostgreSQL instance, user, and database, then starts only its corresponding
daemon. The Workspace backend element never starts provider daemons.

Use separate per-account failure handling so one unavailable server does not
block other accounts. Bound remote connection/read timeouts, HTTP page sizes,
command batch sizes, and work performed before the event loop yields.

## 12. Observe the UI result

Providers do not read the UI event feed. Workspace backend emits the result for
the IAM-scoped UI through both:

```text
GET /api/workspace/v1/events/?epoch_version%3E=<last>&page_limit=500
WS  /api/workspace/v1/events/ws?last_epoch_version=<last>
```

Both transports carry the same flat event. A canonical resource snapshot can
include:

```json
{
  "provider": {
    "uuid": "11111111-1111-4111-8111-111111111111",
    "name": "Mail.ru",
    "kind": "mail"
  },
  "delivery": {
    "status": "delivered",
    "safe_error": null,
    "updated_at": "2026-07-15T09:31:00.000000Z"
  }
}
```

The UI renders the short provider name and delivery state without knowing the
daemon, remote protocol, remote ID, or provider database.

## 13. Verify an integration

Before considering a provider ready, exercise both directions on an isolated
backend:

1. register the provider twice and confirm one stable registration;
2. create an External User through the IAM-authenticated UI API and verify that
   the UI response masks credentials;
3. confirm the account through the Provider Service and observe the common UI
   event;
4. push a remote entity twice and prove the second normalized PUT produces no
   additional event or timestamp change;
5. upload a blob and use its URN in a containing entity;
6. create/update/delete from the UI, deliver the command, and verify pending to
   delivered/failed event transitions;
7. interrupt the result HTTP response and prove command UUID dedupe prevents a
   second remote side effect;
8. restart the provider and prove mappings, cursors, command outcomes, and
   reconciliation schedules survive;
9. attempt a cross-provider/account URN and confirm the Service API rejects it;
10. verify the daemon never connects to Workspace PostgreSQL or the UI events
    websocket.

## Provider implementation checklist

- [ ] Stable provider UUID is configured and persisted.
- [ ] Provider database is dedicated to one backend, UUID, and kind.
- [ ] Service API is reachable only through the trusted boundary.
- [ ] Registration is repeated idempotently at bootstrap.
- [ ] External Accounts are filtered by the provider kind and paginated.
- [ ] Credentials and settings are never logged.
- [ ] Remote IDs map durably to canonical Workspace UUIDs and URNs.
- [ ] Binary data is uploaded before referenced entities.
- [ ] Inbound entity payloads use typed URN relationships.
- [ ] Command UUID outcomes are persisted before result reporting.
- [ ] Pending command replay cannot repeat a remote side effect.
- [ ] Incremental sync is complemented by bounded reconciliation.
- [ ] Provider errors are isolated per account and reported with safe text.
- [ ] Network, SMTP/IMAP/CalDAV/Zulip calls use bounded timeouts.
- [ ] A partial remote listing can never trigger canonical deletes.
- [ ] Provider database binding to backend URL, UUID, and kind is validated at startup.
- [ ] Account and entity collection pagination is covered by tests.
- [ ] Replaying an unchanged inbound PUT is verified as an event no-op.
- [ ] The daemon never connects to Workspace PostgreSQL or the UI event feed.
