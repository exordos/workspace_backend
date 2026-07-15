# Messenger Regression Test Plan

This plan verifies that moving the public Messenger API to
`/api/workspace/v1/messenger` does not change the established Messenger
contract or behavior. It also verifies the separate Zulip provider without
replacing the native Messenger regression suite.

## Acceptance rules

- The relative REST surface, request bodies, response bodies, status codes,
  pagination, filters, source metadata, visibility rules, and durable event
  payloads remain identical to the `workspace-backend` baseline.
- The only Messenger routing changes are the public REST prefix and the common
  websocket endpoint.
- The old `/api/messenger/**` gateway routes are absent; no redirects or
  compatibility aliases are provided.
- Native Messenger behavior is tested independently from provider behavior.
- REST events and websocket events are byte-equivalent after JSON decoding.
- Every provider-owned entity is isolated by provider UUID and External Account.
- A failed account or delivery does not block other accounts.

## Test environment

- Isolated Workspace backend with a fresh database and all migrations applied.
- A newly deployed Zulip instance dedicated to this test run. Existing Zulip
  installations must not be modified.
- Two Workspace users and at least two Zulip users.
- One visible Playwright browser window for all UI scenarios.
- Provider-local PostgreSQL database separate from the Workspace database.

## API and routing

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-API-001 | Request every established Messenger collection and action below `/api/workspace/v1/messenger`. | Methods, status codes, schemas, pagination, and action paths match the baseline. |
| MSG-API-002 | Request `/api/workspace/v1/messenger/server_settings` with and without a trailing slash. | The established public server-settings response is returned without authentication. |
| MSG-API-003 | Request old `/api/messenger/**` REST and websocket paths. | The gateway returns `404` and does not redirect. |
| MSG-API-004 | Connect to `/api/workspace/v1/events/ws`. | The existing websocket protocol authenticates and delivers flat durable events. |
| MSG-API-005 | Compare generated OpenAPI with baseline Messenger resources. | Existing fields, required values, read/write rules, multipart file requirements, and action schemas are unchanged apart from paths. |

## Native channels and topics

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-NATIVE-001 | Create a public channel in Workspace. | Owner binding, default topic, folders, and create events are produced exactly once. |
| MSG-NATIVE-002 | Create direct and group-direct channels repeatedly. | Creation is idempotent and private indexes and bindings remain correct. |
| MSG-NATIVE-003 | Rename, archive, unarchive, and delete a channel. | All users receive the established full update or minimal delete events; dependent rows are removed by database cascades. |
| MSG-NATIVE-004 | Add and remove users with every supported role. | Visibility, bindings, file access, folders, and events update for affected users only. |
| MSG-NATIVE-005 | Create, rename, mark done, set default, mute/follow, read, and delete topics. | Topic state, stream default-topic state, unread counters, and events match the baseline. |

## Native messages, reactions, and files

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-NATIVE-010 | Send messages to explicit and default topics. | Author is read, recipients are unread, ordering and counters are correct, and one create event is emitted per recipient. |
| MSG-NATIVE-011 | Attempt to send as a user without a stream binding. | The established validation response and status code are preserved. |
| MSG-NATIVE-012 | Edit and delete a message. | Visible snapshots and minimal delete payloads match the baseline; dependent reactions and flags are removed. |
| MSG-NATIVE-013 | Mark one message read, read up to a message, read a topic, and read a channel. | Only the selected range changes; counters and read events are correct and idempotent. |
| MSG-NATIVE-014 | Create, update, and delete reactions as different users. | User scoping, aggregate reaction maps, and message update events are correct. |
| MSG-NATIVE-015 | Upload, list, download, update, and delete files. | Multipart and JSON metadata flows, access rows, byte content, and cleanup match the baseline. |
| MSG-NATIVE-016 | Pin/unpin folder items and create/update/delete custom folders. | System and materialized items, ordering, unread counts, and folder events are correct. |

## Events and reconnect behavior

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-EVENT-001 | Exercise every Messenger create/update/read/delete event kind. | Top-level envelope and payload shapes match the baseline golden snapshots. |
| MSG-EVENT-002 | Read events with `epoch_version>` and cursor pagination. | Events are strictly ascending, gap-free for the user, limited to 500, and not duplicated. |
| MSG-EVENT-003 | Disconnect websocket, mutate state, then reconnect with `last_epoch_version`. | Missed durable events are replayed once and live delivery continues. |
| MSG-EVENT-004 | Compare REST and websocket representations for each epoch. | Parsed JSON objects are identical. |
| MSG-EVENT-005 | Send protocol pings and wait through idle intervals. | The connection stays healthy without JSON hello/ping/ack compatibility messages. |

## Zulip provider inbound synchronization

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-ZULIP-IN-001 | Attach two Workspace users to separate Zulip External Accounts. | Both accounts become confirmed independently and neither state or cursor is shared. |
| MSG-ZULIP-IN-002 | Create public channels and topics in Zulip and send messages from both users. | Streams, topics, users, messages, source metadata, bindings, folders, and events appear once in the correct Workspace account scope. |
| MSG-ZULIP-IN-003 | Send direct and group-direct Zulip messages. | Correct private Workspace streams and participant bindings are created without treating recipients as public streams. |
| MSG-ZULIP-IN-004 | Edit/delete messages and add/remove reactions in Zulip. | Workspace projections converge incrementally and emit the established Messenger events. |
| MSG-ZULIP-IN-005 | Change subscriptions, roles, topic notification modes, read/starred flags, and user profiles. | Workspace state converges without losing per-user state or avatar/profile fields. |
| MSG-ZULIP-IN-006 | Send files, images, video links, mentions, and entity references. | Blobs and URNs are normalized, accessible to authorized users, and rendered by the UI. |
| MSG-ZULIP-IN-007 | Expire the Zulip event queue and introduce history gaps. | The provider recreates its queue, performs bounded newest-first reconciliation, and converges without duplicates. |
| MSG-ZULIP-IN-008 | Repeat identical inbound upserts and reconciliation. | No entity timestamp or event changes occur for a semantic no-op. |

## Zulip provider outbound delivery

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-ZULIP-OUT-001 | Create a channel, topic, stream message, direct message, and group-direct message from Workspace. | Commands are routed through the selected External Account and Zulip reflects each supported operation once. |
| MSG-ZULIP-OUT-002 | Edit/delete messages and create/update/delete reactions from Workspace. | Zulip converges and terminal delivery results are reflected without changing the Messenger response contract. |
| MSG-ZULIP-OUT-003 | Rename/archive channels and change subscription/notification/topic state. | Supported Zulip operations are delivered; unsupported operations are rejected before creating an impossible local state. |
| MSG-ZULIP-OUT-004 | Send attachments, images, mentions, quotes, and URN references. | The provider downloads authorized Workspace blobs, uploads/translates them for Zulip, and preserves rendered meaning. |
| MSG-ZULIP-OUT-005 | Repeat a command UUID and lose a result response. | The external mutation occurs once and the provider re-reports the stored terminal result. |
| MSG-ZULIP-OUT-006 | Use an unknown account, entity mapping, or cross-provider UUID. | The command fails safely and cannot read or mutate another provider's entities. |

## Visibility, isolation, and resilience

| ID | Scenario | Expected result |
| --- | --- | --- |
| MSG-SEC-001 | Inspect a provider account while status is pending, invalid, or unavailable. | Its external streams, messages, folders, counters, REST events, epoch, and websocket events remain hidden. |
| MSG-SEC-002 | Confirm the same account. | Its permitted projection becomes visible without leaking credentials or provider-local state. |
| MSG-SEC-003 | Use two providers with overlapping external IDs. | URNs, mappings, commands, blobs, and events remain isolated by provider and account UUID. |
| MSG-SEC-004 | Make one Zulip account slow or unavailable. | Other accounts continue syncing and processing commands within their latency budget. |
| MSG-SEC-005 | Register more than one API page of External Accounts. | Every page is processed; no account is silently skipped. |

## Execution order

1. Run all unchanged baseline Messenger unit and integration tests.
2. Run contract and OpenAPI golden tests against the new mounted paths.
3. Deploy a fresh Workspace database, backend services, provider database, and
   dedicated Zulip instance.
4. Execute native API scenarios.
5. Execute provider inbound and outbound API scenarios.
6. Execute the same user journeys in the visible Playwright browser.
7. Run reconnect, isolation, failure, and reconciliation scenarios.
8. Save sanitized evidence and results in the CASSI test-run archive.

The run is accepted only when every scenario is `PASS`, or an unsupported
feature is explicitly removed from the advertised product scope before the run.
