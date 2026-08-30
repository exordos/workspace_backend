# Workspace Backend Documentation

The main navigation index for the Workspace Backend documentation. The status of each document is explicitly stated: **active contract/active architecture** describes current behavior, while a **proposal (design proposal)** applies only to future refactoring design and does not authorize code changes.

## Project Documentation Glossary {#глоссарий-проектной-документации}

- placement — a canonical message in a specific stream/topic;
- binding — access and personal state of a user or container;
- transactional outbox — a journal of immutable events within a write transaction;
- projection — precomputed state for simple API reads;
- fan-out — background distribution of bindings to recipients;
- worker (background executor) — handler for typed tasks and projections.

Entity names, fields, routes, JSON values, and task types in the documents are preserved in their exact contractual form.

## Active Public API and Contract

| Document | Status | Purpose |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **active contract** | Canonical client contract for Workspace/Messenger REST, Events, and WebSocket: routes, JSON, statuses, filters, pagination, and the runtime/OpenAPI boundary. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **active contract** | REST backfill, epoch cursor, and WebSocket delivery/retry for the Workspace UI. |
| [`architecture.md`](architecture.md) | **active architecture** | Current service boundaries, ownership of PostgreSQL/S3/IAM/provider runtime, and the deployment schema. |
| [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md) | **agreed boundary; implementation requires a separate decision** | Provider-agnostic contract for external account/chat/operation/bridge and the Zulip v1 product boundary. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **active implementation decision** | Agreed `1B/2A/3A/4A/5A`: Provider API v2, project scope, realm-global IDs, authorization boundary, and state-based event key. |

## Proposal: Messenger Domain and API Architecture

| Document | Status | Purpose |
| --- | --- | --- |
| [`messenger_domain_model.md`](messenger_domain_model.md) | **proposal** | Canonical `MESSAGE`, explicit placement, user bindings for messages/containers, invariants, and open decisions. |
| [`messenger_api_domain_model.md`](messenger_api_domain_model.md) | **proposal** | Three layers: RestAlchemy API → simple views → physical entities; query/background processing paths and worker parallelism. |
| [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md) | **implementation proposal** | Specific RestAlchemy declarations, resources/controllers, field origins, and the immutable public JSON contract of the core API. |
| [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md) | **proposal; canonical inventory** | Unified dictionary: class→table/view→fields→keys, UUIDs, task/event kinds, scope routing, risk statuses, and remaining OPEN decisions. |

## Data Model and PlantUML Overview Diagrams

| Overview | Status | Source | SVG |
| --- | --- | --- | --- |
| Messenger domain ER model | **proposal** | [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml) | [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg) |
| Messenger API layers and background processing | **proposal** | [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml) | [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg) |
| RestAlchemy route/resource/view/table mapping | **proposal** | [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml) | [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg) |

## Detailed Sequence Diagrams for Operations

| Index | Status | Purpose |
| --- | --- | --- |
| [`diagrams/sequence/README.md`](diagrams/sequence/README.md) | **proposal, mapped to the active contract** | Full method+path matrix: separate Markdown, editable PlantUML, and SVG for each public HTTP operation, as well as Events WebSocket. |

Each operation specification preserves the active request/response but shows target transaction/outbox/task/worker/event paths. These documents do not replace [`workspace_api.md`](workspace_api.md).

## Proposal: Target Zulip Bridge Architecture

| Document | Status | Purpose |
| --- | --- | --- |
| [`zulip_bridge/README.md`](zulip_bridge/README.md) | **proposal; index** | Unified navigation, accepted invariants, glossary, and canonical OPEN-list target Bridge. |
| [`architecture_overview.md`](zulip_bridge/architecture_overview.md) | **proposal** | Two Bridge processes, sticky whole-account ownership/scheduling, private Workspace API, and a strict boundary with domain workers/WebSocket dispatcher. |
| [`event_coverage.md`](zulip_bridge/event_coverage.md) | **proposal; accepted coverage** | Canonical matrix of exact Zulip events/operations, Workspace↔Zulip directions, source of truth, and echo loop protection. |
| [`realtime_connector.md`](zulip_bridge/realtime_connector.md) | **proposal** | Persistent bidirectional realtime synchronization of supported changes, echo prevention, retry/backpressure/restart. |
| [`history_importer.md`](zulip_bridge/history_importer.md) | **proposal** | Root→per-stream newest-first tasks, fair pool default `4`, account rate limit, and restart of unfinished stream range without message checkpoint. |
| [`internal_workspace_api.md`](zulip_bridge/internal_workspace_api.md) | **proposal on top of current mTLS** | Shared private command boundary, reuse of the active External Bridge mTLS identity, server-owned scope, and idempotency for realtime/history. |
| [`coordination_and_recovery.md`](zulip_bridge/coordination_and_recovery.md) | **proposal** | Whole-account lease/fencing, unified queue bootstrap/boundary, retry/DLQ, and recovery without a Bridge-local durable DB. |
| [`account_lifecycle_and_identity.md`](zulip_bridge/account_lifecycle_and_identity.md) | **proposal; current routes preserved** | Account connect/reconnect/disconnect/delete, verified claim, unmanaged external users, and multi-account canonical union. |
| [`provider_mappings_and_content.md`](zulip_bridge/provider_mappings_and_content.md) | **proposal** | Realm-scoped provider/topic/file mappings, canonical Markdown/URN, deferred references, and manual reconversion. |
| [`delivery_and_events.md`](zulip_bridge/delivery_and_events.md) | **proposal** | Durable outbound operations, conflict/permanent-failure semantics, and ready public event invariants. |

The new catalog describes the target ingestion design and does not replace the active closed OpenAPI or the product boundary [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Worker, Outbox, Projections, and WebSocket Delivery

| Document | Status | Purpose |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **proposal** | General worker architecture and separate processes `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **proposal** | Transactional outbox, separate immutable task for each event, scoped ownership, newest-first, ready events, and a separate dispatcher. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **implemented operator procedure** | Backup/restore, native preserve, migration-time reset of Zulip-derived messages/files, durable file cleanup, and generation-triggered fresh reimport. |

## Closed Provider/Control API Artifacts

| Document | Status | Purpose |
| --- | --- | --- |
| [`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml) | **active closed contract** | Closed Provider data-plane OpenAPI with bridge authentication. |
| [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml) | **active closed contract** | Provider-native command wire format with server-owned Workspace scope; lease/result transport is compatible with v1. |
| [`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml) | **active closed contract** | OpenAPI control plane for the Zulip bridge. |
| [`zulip_bridge_file_api_v1.yaml`](../zulip_bridge_file_api_v1.yaml) | **active closed contract** | Internal OpenAPI for bridge file transfer. |

Closed APIs are not Workspace client routes. Their boundary with the public API is described in [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Migration, Deployment, and Implementation Guide

| Document | Status | Purpose |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **active instruction; requires approval** | Procedures for updating, rolling back, and verifying the current unread projection migration. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **active acceptance plan** | Checks for native Messenger/API/realtime/S3, recovery, rebuild, scale, and load. |
| [`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md) | **active acceptance barrier** | Verification of IAM, bridge, recovery, UI, and deployment of the external integration. |

Proposal documents are not a migration or implementation plan. Production changes begin only after a separate architectural decision and agreed-upon migration/test design.
