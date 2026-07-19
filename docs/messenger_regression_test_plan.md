# Messenger Regression Test Plan

This plan verifies that PostgreSQL is the canonical Messenger store while the
current browser-facing API and S3 file behavior remain unchanged. This plan
also defines rebuild, recovery, scale, and load acceptance.

## Acceptance rules

- REST paths, request and response bodies, status codes, pagination, filters,
  visibility rules, and event payloads match the established Messenger
  baseline.
- `/api/workspace/v1/messenger/**` remains the public Messenger REST namespace;
  old `/api/messenger/**` paths stay absent.
- REST and websocket events are equal after JSON decoding and remain scoped to
  the authenticated IAM user and project.
- PostgreSQL stores canonical Messenger resources, state, events, and provider
  mappings. Runtime reads and writes do not use SMTP, IMAP, Maildir, Exim, or
  Dovecot.
- Files, metadata sidecars, and binary access continue to use S3-compatible
  storage. Messages contain only authorized URNs.
- Provider runtimes exchange ordinary Messenger resources and commands through
  the private Workspace Provider API and never connect to the Workspace
  database.
- Only event records are retained for seven days. Messages, files, streams,
  topics, settings, and provider mappings remain canonical until their normal
  lifecycle deletes them.

## Test environment

- Isolated Workspace backend with a fresh PostgreSQL database and all
  migrations applied.
- S3-compatible object storage dedicated to the test run.
- Two IAM users in one project and one IAM user in another project for the
  minimum functional run; the full load profile is defined in the PostgreSQL
  canonical plan.
- One independently deployable Zulip provider runtime and a dedicated Zulip
  test realm for provider scenarios.
- One visible global Playwright MCP window using the real `cassi` account for
  visible acceptance.

## API and routing

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-API-001 | Request every Messenger collection and action below `/api/workspace/v1/messenger`. | Methods, status codes, schemas, pagination, and action paths match the baseline. |
| MSG-API-002 | Request `/api/workspace/v1/messenger/server_settings` with and without a trailing slash, then unwrap and fetch the `realm_icon` target without credentials. | The public response contains `urn:url:<realm>/logo-512x512.png`, and the packaged organization emblem is returned anonymously as `image/png`. |
| MSG-API-003 | Request old `/api/messenger/**` paths and browser-inaccessible provider, mail, or calendar paths. | Nginx or the application returns `404` and does not redirect. |
| MSG-API-004 | Compare generated OpenAPI with the frozen Messenger baseline. | Existing resources, required values, multipart requirements, actions, and response headers are unchanged. |
| MSG-API-005 | Inspect browser-visible traffic during Messenger use. | No database, provider-service, SMTP, IMAP, Maildir, mail, or calendar implementation detail is exposed. |

## Channels, topics, and folders

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-NATIVE-001 | Create a public channel. | Owner binding, default topic, folders, and create events are produced once. |
| MSG-NATIVE-002 | Create direct and group-direct channels repeatedly. | Creation is idempotent and private bindings remain correct. |
| MSG-NATIVE-003 | Rename, archive, unarchive, and delete a channel. | Authorized users receive the established update or delete events. |
| MSG-NATIVE-004 | Add and remove users with every supported role. | Visibility, bindings, file access, folders, and events update only for affected users. |
| MSG-NATIVE-005 | Create, rename, mark done, set default, mute, follow, read, and delete topics. | Topic state, unread counters, and events match the baseline. |
| MSG-NATIVE-006 | Pin and unpin folder items and manage custom folders. | Ordering, materialized items, unread counters, and folder events remain correct. |

## Messages and reactions

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DB-001 | Send messages to explicit and default topics. | The canonical PostgreSQL message, flags, shared audience, compact message/topic/stream broadcasts, and public response commit atomically without per-recipient canonical event rows. |
| MSG-DB-002 | Restart the API, event, and worker services before reading messages. | Message content, ordering, flags, and identifiers remain available through the same API. |
| MSG-DB-003 | Edit and delete a message. | Canonical rows, tombstone behavior, reactions, events, and public snapshots converge transactionally. |
| MSG-DB-004 | Mark one message read, read up to a message, read a topic, and read a channel. | Only the selected range changes; counters and events are correct and idempotent. |
| MSG-DB-005 | Create, update, and delete reactions as different users. | User scoping, aggregate reactions, and message update events remain correct. |
| MSG-DB-006 | Attempt to act without membership or with another project's token. | IAM authorization rejects the request and canonical state remains unchanged. |
| MSG-DB-007 | Submit the same retryable request after an ambiguous response failure. | Idempotency is preserved and duplicate user-visible messages or events are not created. |

## Drafts

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DRAFT-001 | Create multiple drafts for one stream/topic and retry one UUID with identical and changed canonical fields. | All distinct UUIDs persist; the identical retry returns the original revision without another mutation, while changed fields return `409`. |
| MSG-DRAFT-002 | Read and paginate drafts in both `updated_at` directions with stream/topic filters and a UUID marker. | Ordering is stable by `(updated_at, uuid)`; markers cannot cross owner, project, or filter scope. |
| MSG-DRAFT-003 | Update and delete with missing, current, stale, weak, and malformed `If-Match` values. | Missing returns `428`; only the exact strong revision succeeds; stale or invalid values return `412` with the current snapshot and ETag. |
| MSG-DRAFT-004 | Attempt draft CRUD as another user/project, without stream membership, or with a topic from another stream. | Access is rejected without exposing or mutating another owner's draft. |
| MSG-DRAFT-005 | Remove the owner binding, delete the topic, and delete the stream while drafts exist. | PostgreSQL cascades hard-delete every affected draft without tombstones or events. |
| MSG-DRAFT-006 | Observe events, notifications, messages, unread, reactions, files, and provider commands around draft CRUD. | Only draft rows change; no event, notification, command, message, or unrelated state changes. |

## Files and S3-compatible storage

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-FILE-001 | Upload, list, download, update, and delete files. | Multipart and metadata flows match the baseline and bytes plus sidecars are stored in S3-compatible storage. |
| MSG-FILE-002 | Attach a file to a message and restart all backend services. | The message keeps its authorized file URN and the object remains downloadable. |
| MSG-FILE-003 | Request a file as an unrelated user or project. | Access is denied without disclosing object keys or metadata. |
| MSG-FILE-004 | Delete the last authorized file reference through the supported flow. | Canonical metadata, access records, object, and sidecar cleanup follow the established contract. |
| MSG-FILE-005 | Upload multipart data with `acl={"mode":"public"}` and no `stream_uuid`, then request its metadata and bytes with another valid Workspace bearer token. | The sidecar contains `acl.mode=public` without a stream UUID, the second authenticated user succeeds without membership, anonymous access remains rejected, and the canonical file URN remains unchanged. |

## Events and reconnect behavior

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-EVENT-001 | Exercise every Messenger create, update, read, and delete event kind. | Envelope and payload shapes match baseline snapshots. |
| MSG-EVENT-002 | Read events with `epoch_version>`, the matching `epoch_generation`, and cursor pagination. | Events are ascending, gap-free for the user, limited to 500, and not duplicated. A non-zero epoch without its generation is rejected. |
| MSG-EVENT-003 | Disconnect websocket, mutate state, then reconnect with the saved cursor. | Missed events are replayed, one `ready` frame opens the notification gate, and only then does live delivery continue. |
| MSG-EVENT-004 | Compare REST and websocket representations for each epoch. | Parsed JSON objects are identical. |
| MSG-EVENT-005 | Wait through idle intervals and protocol pings. | The connection remains healthy without JSON compatibility messages. |
| MSG-EVENT-006 | Change IAM user or project while retaining an old cursor. | Client state and cursor are partitioned; no event crosses the IAM boundary. |
| MSG-EVENT-007 | Start with a valid retained cursor and with a cursor older than the seven-day floor. | A retained suffix is returned gap-free; an expired cursor returns typed `epoch_pruned` so the client reloads authoritative PostgreSQL snapshots. |

## Provider regression

The provider scenarios use the private Provider API described and tested by
the PostgreSQL canonical plan. The public UI must continue to see ordinary
Messenger resources with typed provider metadata.

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-PROVIDER-001 | Import and mutate Zulip channels, topics, DMs, users, messages, reactions, and read state. | One canonical resource per stable provider mapping is visible through the unchanged public API. |
| MSG-PROVIDER-002 | Send native and provider messages through the same stream/topic views. | Pagination, unread counts, folders, events, badges, and delivery state remain correct. |
| MSG-PROVIDER-003 | Stop, restart, disconnect, reconnect, and delete the provider account. | Durable commands and mappings converge without duplicate resources or impact on native Messenger. |
| MSG-PROVIDER-004 | Transfer an image and a generic file in both directions. | Receiving storage contains the expected bytes and Workspace messages contain only S3-backed URNs. |

## Deployment and recovery

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DEPLOY-001 | Inspect listeners, processes, packages, configuration, and routes after deployment. | Only documented public HTTP/websocket and private Provider API routes are present; no secondary Messenger persistence runtime remains. |
| MSG-DEPLOY-002 | Restart backend, PostgreSQL, provider, and S3-facing services after creating messages and files. | Canonical database and S3 state survive and the public API returns the same resources. |
| MSG-DEPLOY-003 | Replace backend, UI, and provider root images independently while preserving canonical PostgreSQL and S3 data. | Public state, provider mappings, commands, events, and files remain available without reinstall or destructive cleanup. |
| MSG-DEPLOY-004 | Search the deployment for runtime mail dependencies and observe network counters during acceptance. | Exim, Dovecot, Maildir paths, SMTP/IMAP clients, mail certificates, mail routes, and traffic are absent. |
| MSG-DEPLOY-005 | Rebuild declared disposable views, counters, search indexes, and caches from canonical PostgreSQL base tables. | The complete public digest and visible UI state match the pre-rebuild baseline. |

## Execution order

1. Run the unchanged Messenger unit and integration suite.
2. Run routing, OpenAPI, IAM, event, Provider API, and S3 contract tests.
3. Apply an immutable backend update while preserving PostgreSQL and S3 data.
4. Execute the Messenger API, provider, storage, restart, rebuild, failure, and
   isolation scenarios.
5. Execute the supported journeys in the visible global Playwright MCP browser.
6. Execute the 150-concurrent-user load and provider workload.
7. Save sanitized evidence in the CASSI test-run archive.

The run is accepted only when every required scenario passes. Any scenario not
executed is `NOT RUN` or `BLOCKED`; it must not be reported as verified
compatibility.
