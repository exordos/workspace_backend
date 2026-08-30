# The target architecture Zulip Bridge

Status: **proposal; first server/Provider API v2 part is recorded separately**.

[← The main index of the documentation](../index.md) · [The canonical inventory Messenger](../messenger_architecture_inventory.md) · [The current border Zulip v1](../zulip_bridge_v1_product_and_api.md)

Wire transport, project scope, direct identity, outbound authorization and
provider event key The first implementation closed decisions `1B/2A/3A/4A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

This directory describes the target two-process synchronization architecture
Workspace↔Zulip It doesn't change the existing ones.
public routes,JSONOr closed contracts.
The contract remains in force.
[`workspace_api.md`](../workspace_api.md), and the ones that are in effect provider/control/file
The boundaries are in [`zulip_bridge_v1_product_and_api.md`](../zulip_bridge_v1_product_and_api.md)
and related OpenAPI files.

## The documents

| The document | The status | Purpose |
| --- | --- | --- |
|  [`architecture_overview.md`](architecture_overview.md)  | **proposal** | Components, trust boundaries, data flow and responsibility sharing between Bridge and Workspace. |
|  [`event_coverage.md`](event_coverage.md)  | **proposal; The accepted coverage** | The canonical matrix exact Zulip events/operations, synchronization directions, Workspace actions, source of truth and echo prevention. |
|  [`realtime_connector.md`](realtime_connector.md)  | **proposal** | Constant `Zulip Realtime Connector`: event reception, order, retry, backpressure and graceful restart. |
|  [`history_importer.md`](history_importer.md)  | **proposal** | The final one . `Zulip History Importer`: fair pool default `4`, per-stream newest-first work, account limiter, restart/dependencies/reconciliation. |
|  [`internal_workspace_api.md`](internal_workspace_api.md)  | **proposal** | Common internal Workspace API, limited service identity, transaction boundary and unified idempotence. |
|  [`coordination_and_recovery.md`](coordination_and_recovery.md)  | **proposal** | Unified bootstrap, account lease/fencing, boundary, retry/DLQ, reconciliation and recovery. |
|  [`account_lifecycle_and_identity.md`](account_lifecycle_and_identity.md)  | **proposal; current routes preserved** | Connect/reconnect/disconnect/delete, verified identity claim, unmanaged users and multi-account canonical union. |
|  [`provider_mappings_and_content.md`](provider_mappings_and_content.md)  | **proposal** | Realm-scoped provider keys, durable topic/file mappings, canonical Markdown/URN, deferred references and manual reconversion. |
|  [`delivery_and_events.md`](delivery_and_events.md)  | **proposal** | Durable Workspace→Zulip operations, conflict/permanent-failure semantics and exactly-one ready event per actual transition. |

## The diagrams

| The script | PlantUML | SVG |
| --- | --- | --- |
| Realtime synchronization and echo prevention |  [`realtime_connector.puml`](diagrams/realtime_connector.puml)  |  [`realtime_connector.svg`](diagrams/realtime_connector.svg)  |
| History import |  [`history_importer.puml`](diagrams/history_importer.puml)  |  [`history_importer.svg`](diagrams/history_importer.svg)  |
| Primary import and transition to realtime-only |  [`bootstrap_to_realtime.puml`](diagrams/bootstrap_to_realtime.puml)  |  [`bootstrap_to_realtime.svg`](diagrams/bootstrap_to_realtime.svg)  |
| Verified claim unmanaged identity |  [`identity_claim.puml`](diagrams/identity_claim.puml)  |  [`identity_claim.svg`](diagrams/identity_claim.svg)  |
| Shared topic mapping, rename and partial move |  [`topic_mapping_and_move.puml`](diagrams/topic_mapping_and_move.puml)  |  [`topic_mapping_and_move.svg`](diagrams/topic_mapping_and_move.svg)  |
| Content conversion, deferred repair and reconversion |  [`content_conversion_and_repair.puml`](diagrams/content_conversion_and_repair.puml)  |  [`content_conversion_and_repair.svg`](diagrams/content_conversion_and_repair.svg)  |
| Outbound retry, permanent failure and public events |  [`outbound_delivery.puml`](diagrams/outbound_delivery.puml)  |  [`outbound_delivery.svg`](diagrams/outbound_delivery.svg)  |

## The canonical glossary

- **Bridge process** — external trusted process without direct access to the DB
  Workspace;
- **service identity** — - It 's working . realm-bound mTLS identity private External
  Bridge API: `realm_uuid`, `provider_kind`, `bridge_instance_uuid` and
  `identity_generation` They 're only taken from the verified ones . client certificate;
  account/project scope and permissible commands Workspace then determines by
  current server-owned assignments;
- **provider object key** — stable internal identity of the Zulip-object,
  The same for realtime and history;
- **provider event key** — a stable key of one mutation/version of the Zulip-object,
  used as idempotency/derivation key;
- **provider object UUIDv5** — `UUIDv5(namespace=verified realm UUID,
  name="<entity_type>:<decimal_provider_id>")` For the numeric Zulip objects;
- **registration boundary** — The new Zulip queue border: realtime is accepting
  The history root will import the selected snapshot./rangeBefore her.;
- **account lease/fencing generation** — Workspace-issued exclusive ownership
  All external account one Bridge instance; the stale owner cannot commit;
- **history root/stream task** — durable Workspace task: root It opens scope and
  creates per-stream tasks; stream task restarts its range entirely;
- **deferred resolution** — A retained dependence that cannot be applied
  Until the basic object appears;
- **Workspace projection worker** — an internal worker Workspace who
  is executed after outbox; it doesn 't Bridge process;
- **WebSocket dispatcher** — a separate component Workspace that delivers
  It 's a ready-made durable public event and it doesn 't participate in importing Zulip.

## Accepted Invariants

1. `Zulip Realtime Connector` and `Zulip History Importer`  independent processes
   with a common identity semantics/idempotency, but different life cycles.
2. No Bridge process writes directly to Workspace PostgreSQL or object
   storage metadata. All domain mutations pass through a restricted
   The internal Workspace API.
3. User access tokens are not used. Bridge cannot select
   arbitrary `project_id`, source or Workspace user; these values are derived and
   Checks Workspace for service identity and server mappings.
4. Creating a message using a common domain transaction Workspace:
   canonical `MESSAGE` + `TOPIC` and `MESSAGE_PLACEMENT` + copyright
   `USER_MESSAGE_BINDING`/`USER_MESSAGE_STATE` + immutable outbox event.
5. Public UUID message is equal to placement UUID:
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Canonical `MESSAGE.uuid`
   It 's still internal ..
6. Bridge does not perform recipient fan-out, does not update Workspace projections and
   It doesn't create public WebSocket events. Workspace workers;
   dispatcher It 's just a delivery of the events that are already in place ..
7. Connect, reconnect, queue expiry, missing heartbeat, `restart` and
   `web_reload_client` are using the same bootstrap: register a new one queue,
   Get boundary, start realtime and only then create history root task.
   The old queue/cursor is not a durable state; overlap/no-gap provides
   boundary and common provider keys.
8. Old UUID of previous Zulip-import after agreed full reset to save
   No need to re-try. Inside a new import any retry/resume must be re-tried
   I 'm going to address the same new one . canonical row.
9. The canonical coverage and direction of each Zulip event family is given
      [`event_coverage.md`](event_coverage.md). Bidirectional mutation He 's carrying .
   origin/causation/provider identity; Own provider echo confirms
   The original operation and does not run an infinite back record.
10. Durable mappings, assignments, leases, tasks, outbound operations and errors
    They belong to .Workspace. Bridge instances do not have a common Bridge database;
    local state It 's only a throw-away . cache.
11. One account is entirely owned by one fenced Bridge owner: realtime and
    history are not divided between instances. Assignment sticky; healthy accounts
    They 're not automatically rebalanced when a new one comes along . instance.
12. Bridge converts provider events/operations, but does not implement Workspace
    domain policy. History visibility, bindings and archive semantics decides
    Workspace The current stream settings.
13. Both Bridge processes will reuse the current authentication private
    External Bridge API: TLS 1.2+ mutual TLS, realm control CA, One-time
    enrollment and generation-bound client certificate.HTTP headers/bodyNo , not really .
    Can replace certificate identity. Whole-account lease/fencing —
    additional transaction-time authorization, not credential and not replacement
    mTLS.

## Unified list of OPEN-solutions Zulip Bridge {#единый-список-open-решений-zulip-bridge}

This is the only list of unfinished solutions for this catalog.
The documents are linked here and do not create their own copies.

Previously open wire transport, event/direct keys, private initiation surface and
cross-account project scope closed by `1B/2A/3A/4A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

1. Operational upper limits After load tests: maximum/optimal history worker
   pool above default `4`, history batch/rate budgets, provider admission and
   retention failed history/DLQ/deferred evidence, Not covered by the accepted
   successful/permanent-operation TTL.
   All paths bounded/configurable; one account-level limiter and realtime priority
   Already registered.
2. Direction and model `saved_snippets`: family remains `OPEN` and not
   is automatically interpreted as Workspace draft/message.
3. Accurate display of realm-wide Zulip `realm_user/update person.role` on
    Workspace role model. It must not be silent. channel-specific
    `WorkspaceStreamBinding.role`.
4. Exact converter edge/loss policy for Zulip→canonical Markdown and vice versa
    URN resolution, including unsupported Zulip markup.
    manual reconversion boundary They 've already been accepted ..

Retention No more OPEN: completed history tasks and successful outbound
operations They 're being stored . `30 days`, internal permanent-failure operation/code/reason
— `90 days`, provider mappings/latest hidden raw metadata — lifetime The
entity. The possible future manual requeue remains an internal extension, not a new one.
current public endpoint. Retention failed history/DLQ/deferred evidence It stays.
OPEN #1 and is not replaced by values `30/90 days`.

The related general OPEN-solutions Messenger, including capacity/SLO, remain in
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#единственный-список-open-решений).

[← The main index of the documentation](../index.md) · [The canonical inventory Messenger](../messenger_architecture_inventory.md) · [The current border Zulip v1](../zulip_bridge_v1_product_and_api.md)
