# Messenger Regression Test Plan

This plan verifies that the public Messenger contract remains unchanged while
message persistence and delivery use the local Exim4, Dovecot, and Maildir
stack. It covers only Messenger, IAM, realtime delivery, and S3-compatible file
storage.

## Acceptance rules

- REST paths, request and response bodies, status codes, pagination, filters,
  visibility rules, and event payloads match the established Messenger
  baseline.
- `/api/workspace/v1/messenger/**` remains the only public Messenger REST
  namespace; old `/api/messenger/**` paths stay absent.
- REST and websocket events are equal after JSON decoding and remain scoped to
  the authenticated IAM user and project.
- SMTP, IMAP, Maildir paths, and mail wire data never appear in the public API.
- Exim4 and Dovecot listen only on the platform-internal network with service
  authentication, and Maildir survives mail-service and mail-node restarts.
- Files, metadata, and access-control records remain in S3-compatible storage.
- There is no SQL cache of message content or state.

## Test environment

- Isolated Workspace backend with a fresh database and all migrations applied.
- Exim4 and Dovecot configured on the dedicated mail node with authenticated
  platform-internal protocol listeners and a Maildir tree on persistent storage.
- S3-compatible object storage dedicated to the test run.
- Two IAM users in one project and one IAM user in another project.
- One visible Playwright browser window for UI scenarios.

## API and routing

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-API-001 | Request every Messenger collection and action below `/api/workspace/v1/messenger`. | Methods, status codes, schemas, pagination, and action paths match the baseline. |
| MSG-API-002 | Request `/api/workspace/v1/messenger/server_settings` with and without a trailing slash. | The established public response is returned without authentication. |
| MSG-API-003 | Request old `/api/messenger/**` paths and removed provider, mail, or calendar paths. | Nginx or the application returns `404` and does not redirect. |
| MSG-API-004 | Compare generated OpenAPI with the Messenger baseline. | Existing resources, required values, multipart requirements, and actions are unchanged. |
| MSG-API-005 | Inspect browser-visible traffic during Messenger use. | No SMTP, IMAP, Maildir, provider, mail, or calendar implementation detail is exposed. |

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
| MSG-MAIL-001 | Send messages to explicit and default topics. | Public responses match the baseline and Exim4 delivers one canonical message to persistent Maildir. |
| MSG-MAIL-002 | Restart the backend and dedicated mail services before reading messages. | Message content, ordering, flags, and identifiers remain available through the same API. |
| MSG-MAIL-003 | Edit and delete a message. | IMAP/Maildir state and the public snapshot converge without a SQL message copy. |
| MSG-MAIL-004 | Mark one message read, read up to a message, read a topic, and read a channel. | Only the selected range changes; counters and events are correct and idempotent. |
| MSG-MAIL-005 | Create, update, and delete reactions as different users. | User scoping, aggregate reactions, and message update events remain correct. |
| MSG-MAIL-006 | Attempt to act without membership or with another project's token. | IAM authorization rejects the request and Maildir remains unchanged. |
| MSG-MAIL-007 | Submit the same retryable request after an ambiguous transport failure. | The documented idempotency behavior is preserved and duplicate user-visible messages are not created. |

## Files and S3-compatible storage

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-FILE-001 | Upload, list, download, update, and delete files. | Multipart and metadata flows match the baseline and bytes are stored in S3-compatible storage. |
| MSG-FILE-002 | Attach a file to a message and restart all backend services. | The message keeps its authorized file reference and the object remains downloadable. |
| MSG-FILE-003 | Request a file as an unrelated user or project. | Access is denied without disclosing object keys or metadata. |
| MSG-FILE-004 | Delete the last authorized file reference through the supported flow. | Metadata, access records, and object cleanup follow the established contract. |

## Events and reconnect behavior

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-EVENT-001 | Exercise every Messenger create, update, read, and delete event kind. | Envelope and payload shapes match baseline snapshots. |
| MSG-EVENT-002 | Read events with `epoch_version>`, the matching `epoch_generation`, and cursor pagination. | Events are ascending, gap-free for the user, limited to 500, and not duplicated. A non-zero epoch without its generation is rejected. |
| MSG-EVENT-003 | Disconnect websocket, mutate state, then reconnect with the saved `last_epoch_version` and `epoch_generation`. | Missed events are replayed, one `ready` frame opens the notification gate, and only then does live delivery continue. |
| MSG-EVENT-004 | Compare REST and websocket representations for each epoch. | Parsed JSON objects are identical. |
| MSG-EVENT-005 | Wait through idle intervals and protocol pings. | The connection remains healthy without JSON compatibility messages. |
| MSG-EVENT-006 | Change IAM user or project while retaining an old cursor. | Client state and cursor are partitioned; no event crosses the IAM boundary. |
| MSG-EVENT-007 | Start with `last_epoch_version=0` and no generation before and after the retention floor advances. | A complete cold journal is accepted; an incomplete retained suffix returns typed HTTP/WS `epoch_pruned` so the client reloads authoritative snapshots. |

## Deployment and recovery

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DEPLOY-001 | Inspect listening sockets and routing after deployment. | SMTP and IMAP are reachable from the backend only with service credentials over the internal network; only documented HTTP/websocket routes are public. |
| MSG-DEPLOY-002 | Reboot the backend and mail nodes after creating messages and files. | Maildir and S3 data survive and the public API returns the same resources. |
| MSG-DEPLOY-003 | Replace the mail root image while preserving its data disk. | Exim4/Dovecot configuration is recreated and existing Maildir data is readable. |
| MSG-DEPLOY-004 | Search the database and deployment for message cache tables or provider runtime artifacts. | Neither SQL message cache nor provider services, routes, images, manifests, or processes exist. |
| MSG-DEPLOY-005 | On a disposable environment, terminate the mail VM during active journal reads, then boot it again with the same Maildir volume. | Message files, UIDVALIDITY, UIDs, and API state remain unchanged; Dovecot creates fresh indexes below `/run/workspace/dovecot-indexes` and does not reuse a persistent `dovecot.index.log`. |

## Execution order

1. Run the unchanged Messenger unit and integration suite.
2. Run routing, OpenAPI, IAM, and realtime contract tests.
3. Deploy fresh backend and mail nodes with persistent Maildir and S3-compatible storage.
4. Execute the Messenger API, storage, restart, and isolation scenarios.
5. Execute the same supported journeys in the visible Playwright browser.
6. Save sanitized evidence and results in the CASSI test-run archive.

The run is accepted only when every required scenario passes. Any scenario not
executed must be marked `NOT RUN` or `BLOCKED`; it must not be reported as
verified compatibility.
