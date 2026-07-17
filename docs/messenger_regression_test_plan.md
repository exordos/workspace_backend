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
| MSG-API-002 | Request `/api/workspace/v1/messenger/server_settings` with and without a trailing slash, then unwrap and fetch the `realm_icon` target without credentials. | The public response contains `urn:url:<realm>/logo-512x512.png`, and the packaged organization emblem is returned anonymously as `image/png`. |
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

## Drafts

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-DRAFT-001 | Create multiple drafts for one stream/topic and retry one UUID with identical and changed canonical fields. | All distinct UUIDs persist; the identical retry returns the original revision without another mutation, while changed fields return `409`. |
| MSG-DRAFT-002 | Read and paginate drafts in both `updated_at` directions with stream/topic filters and a UUID marker. | Ordering is stable by `(updated_at, uuid)`; markers cannot cross owner, project, or filter scope. |
| MSG-DRAFT-003 | Update and delete with missing, current, stale, weak, and malformed `If-Match` values. | Missing returns `428`; only the exact strong revision succeeds; stale or invalid values return `412` with the current snapshot and ETag. |
| MSG-DRAFT-004 | Attempt draft CRUD as another user/project, without stream membership, or with a topic from another stream. | Access is rejected without exposing or mutating another owner's draft. |
| MSG-DRAFT-005 | Remove the owner binding, delete the topic, and delete the stream while drafts exist. | PostgreSQL cascades hard-delete every affected draft without tombstones or events. |
| MSG-DRAFT-006 | Observe events, notifications, messages, mail, unread, reactions, and files around draft CRUD. | Only draft rows change. No Workspace event, websocket/desktop notification, canonical mail, or unrelated projection changes; another client refreshes drafts on reload or explicit API refetch. |

## Files and S3-compatible storage

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-FILE-001 | Upload, list, download, update, and delete files. | Multipart and metadata flows match the baseline and bytes are stored in S3-compatible storage. |
| MSG-FILE-002 | Attach a file to a message and restart all backend services. | The message keeps its authorized file reference and the object remains downloadable. |
| MSG-FILE-003 | Request a file as an unrelated user or project. | Access is denied without disclosing object keys or metadata. |
| MSG-FILE-004 | Delete the last authorized file reference through the supported flow. | Metadata, access records, and object cleanup follow the established contract. |
| MSG-FILE-005 | Upload multipart data with `acl={"mode":"public"}` and no `stream_uuid`, then request its metadata and bytes with another valid Workspace bearer token. | The sidecar contains `acl.mode=public` without a stream UUID, the second authenticated user succeeds without membership, anonymous access remains rejected, and the canonical file URN remains unchanged. |

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
| MSG-DEPLOY-001 | Inspect listening sockets and routing after deployment. | SMTP and IMAP are reachable from the backend only with service credentials over CA-verified STARTTLS on the internal network; plaintext authentication is not advertised before TLS, and only documented HTTP/websocket routes are public. |
| MSG-DEPLOY-002 | Reboot the backend and mail nodes after creating messages and files. | Maildir and S3 data survive and the public API returns the same resources. |
| MSG-DEPLOY-003 | Replace the mail root image while preserving its data disk. | Exim4/Dovecot configuration is recreated and existing Maildir data is readable. |
| MSG-DEPLOY-004 | Search the database and deployment for message cache tables or provider runtime artifacts. | Neither SQL message cache nor provider services, routes, images, manifests, or processes exist. |
| MSG-DEPLOY-005 | On a disposable environment, terminate the mail VM during active journal reads, then boot it again with the same Maildir volume. | Message files, UIDVALIDITY, UIDs, and API state remain unchanged; Dovecot creates fresh indexes below `/run/workspace/dovecot-indexes` and does not reuse a persistent `dovecot.index.log`. |
| MSG-DEPLOY-006 | Replace the backend and mail root images independently while preserving the mail data disk. | The persistent CA fingerprint remains unchanged; the current leaf remains unchanged during ordinary root replacement unless it has reached the renewal threshold, in which case a new leaf is signed by the same CA. Realm metadata validates, the backend retrieves only the public CA through the nonce-bound HMAC endpoint, and verified SMTP/IMAP connectivity recovers. |
| MSG-DEPLOY-007 | Replay or modify the CA response, nonce, hostname, signature, redirect, or shared HMAC credential during backend bootstrap. | The backend rejects the response, does not replace its current CA file, and remains unready without attempting plaintext mail authentication. |
| MSG-DEPLOY-008 | Bootstrap with a leaf inside or beyond the renewal window, then with corrupt, partial, cloned, hostname/realm-mismatched, unsafe-permission, unexpected-type, and wrong-key persistent TLS stores. | Expiry creates a complete new leaf generation under the same CA and atomically changes `current`; safe owner/mode drift is normalized, while every invalid type or mismatched store fails closed without regenerating the CA; old leaf keys are pruned only after the rollback window. |
| MSG-DEPLOY-009 | Restore the mail data disk and rotate the CA through the documented overlap procedure. | Encrypted backup restores CA/leaf/realm state with exact safe ownership and modes; backend trusts both CAs during cutover and removes the old CA only after verified convergence and the rollback window. |
| MSG-DEPLOY-010 | Deliver the final STARTTLS workspace config before the backend PKI config, and inspect newly rendered secret configs on both images. | The first on-change handler defers successfully without clearing readiness or entering a healthcheck retry loop; PKI delivery later performs authenticated CA sync and the full readiness sequence. The actual universal-agent unit runs with `UMask=0077`, so a newly created secret config is never group/world-readable even before its final resource mode is applied. |
| MSG-DEPLOY-011 | Replace the persistent PKI store or its parent with a symlink to a controlled directory, then run mail bootstrap. | Bootstrap rejects the path before any install, ownership, or mode mutation; the symlink target remains byte-for-byte and metadata unchanged. A root-owned direct child of the persistent mount continues to bootstrap normally. |
| MSG-DEPLOY-012 | Bootstrap a fresh mail root while the universal agent runs with `UMask=0077`, then authenticate an empty healthcheck user and select the project `Workspace/State` and `Workspace/Events` mailboxes. | `/run/workspace` is `root:root 0755`, its `dovecot-indexes` child is `workspace:workspace 0750`, healthcheck succeeds through authenticated SMTP/IMAP `NOOP` without requiring an INBOX, both canonical mailbox selections succeed without `NOPERM`, and config delivery does not enter a Dovecot/Exim restart loop. |

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
