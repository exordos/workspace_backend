# Workspace Backend Documentation

Current contract: [Workspace public OpenAPI](../public_workspace_api.yaml).
External accounts contain an owner, `provider` and generic `settings`.
Workspace processes native messages and membership; provider synchronization and
external delivery live elsewhere. The remaining design and architecture pages
are historical references from before migration `0186`.

The main navigation index for the Workspace Backend documentation. The status of each document is explicitly stated: **active contract/active architecture** describes current behavior, while a **proposal (design proposal)** applies only to future refactoring design and does not authorize code changes.

## Project Documentation Glossary {#глоссарий-проектной-документации}

- placement — a canonical message in a specific stream/topic;
- binding — access and personal state of a user or container;
- transactional outbox — a journal of immutable events within a write transaction;
- projection — precomputed state for simple API reads;
- fan-out — background distribution of bindings to recipients;
- worker (background executor) — handler for typed tasks and projections.

Entity names, fields, routes, JSON values, and task types in the documents are preserved in their exact contractual form.

## Public API and design history

| Document | Status | Purpose |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **active contract** | Canonical client contract for Workspace/Messenger REST, Events, and WebSocket: routes, JSON, statuses, filters, pagination, and the runtime/OpenAPI boundary. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **active contract** | REST backfill, epoch cursor, and WebSocket delivery/retry for the Workspace UI. |
| [`architecture.md`](architecture.md) | **historical reference** | Retired private bridge and provider API architecture. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **historical reference** | Retired Provider API v2 and mTLS transport decisions. |

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

## Worker, Outbox, Projections, and WebSocket Delivery

| Document | Status | Purpose |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **proposal** | General worker architecture and separate processes `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **proposal** | Transactional outbox, separate immutable task for each event, scoped ownership, newest-first, ready events, and a separate dispatcher. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **implemented operator procedure** | Backup/restore, native preserve, migration-time reset of Zulip-derived messages/files, durable file cleanup, and generation-triggered fresh reimport. |

## Migration, Deployment, and Implementation Guide

| Document | Status | Purpose |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **active instruction; requires approval** | Procedures for updating, rolling back, and verifying the current unread projection migration. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **active acceptance plan** | Checks for native Messenger/API/realtime/S3, recovery, rebuild, scale, and load. |

Proposal documents are not a migration or implementation plan. Production changes begin only after a separate architectural decision and agreed-upon migration/test design.
