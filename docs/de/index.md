# Dokumentation Workspace Backend

Workspace Backend. Status jeder
Dokument ist klar angegeben: **wirksamer Vertrag/wirksame Architektur**
beschreibt das aktuelle Verhalten, und **proposal (Entwurfsvorschlag) ** bezieht sich
nur für die künftige Refaktoring-Projektion und erlaubt keine Änderung des Codes.

## Glossar der Projektdokumentation {#глоссарий-проектной-документации}

- Platzierung (**placement**)  eine kanonische Nachricht in einem bestimmten
  stream/topic;
- Bindung (**binding**)  Zugriff und persönlicher Status des Benutzers oder
  - Ein Behälter .;
- transactional outbox — Journal der unveränderlichen Ereignisse in der Transaktion der Eintragung;
- Projektion (**projection**)  Vorbereitungszustand für einfache
  Lesen API;
- fan-out — Hintergrundverteilung der Bindungen an die Empfänger;
- worker (Hintergrund-Aussteller)  Typisierte Aufgaben und Projektionen.

Namen von Wesen, Feldern, Routen, JSON-Werten und Aufgabenarten in Dokumenten
in exaktem Vertragssinn aufbewahrt werden.

## Aktuelles öffentliches API und Vertrag

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`workspace_api.md`](workspace_api.md)  | **- ein gültiger Vertrag** | Kanonischer Kundenvertrag Workspace/Messenger REST, Events und WebSocket: Routen, JSON, Status, Filter, Pagination und Grenze runtime/OpenAPI. |
|  [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md)  | **- ein gültiger Vertrag** | REST-Überladen, Epoch-Cursor und Lieferung/Wiederholung WebSocket für Workspace UI. |
|  [`architecture.md`](architecture.md)  | **die bestehende Architektur** | Aktuelle Service-Grenzen, Besitz der PostgreSQL/S3/IAM/provider-Runtime und das Bereitstellungsgeschema. |
|  [`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md)  | **vereinbarte Grenze; die Umsetzung erfordert eine separate Entscheidung** | Provider-unabhängiger Vertrag für externe Accounts/chat/operation/bridgeund Produktgrenze Zulip v1. |
|  [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md)  | **die geltende Lösung der Umsetzung** | `1B/2A/3A/4A/5A` vereinbart: Provider API v2, Project scope, realm-global IDs, authorization boundary und state-based event key. |

## Proposal: Messenger Domain und Architektur API

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`messenger_domain_model.md`](messenger_domain_model.md)  | **proposal** | Kanonisch `MESSAGE`, offensichtliche Platzierung, benutzerdefinierte Bindungen von Nachrichten/Containern, Invarianten und offene Lösungen. |
|  [`messenger_api_domain_model.md`](messenger_api_domain_model.md)  | **proposal** | Drei Schichten RestAlchemy API → einfache Darstellungen → physikalische Wesen, Anfrage-/Hintergrundverarbeitungspfade und Parallelismus worker. |
|  [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md)  | **proposal der** | Konkrete Erklärungen RestAlchemy, Ressourcen/Controller, Herkunft der Felder und unveränderlicher öffentlicher JSON-Vertrag core API. |
|  [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md)  | **proposal; Kanonisches Inventar** | Ein einziges Wörterbuch class→table/view→fields→keys, UUID, task/event kinds, scope routing, Status risks und die verbleibenden OPEN-Lösungen. |

## Datenmodell und Übersichtsschemata von PlantUML

| Übersicht | Status | Ausgang | SVG |
| --- | --- | --- | --- |
| ER-Modell der Domäne Messenger | **proposal** |  [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml)  |  [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg)  |
| Schichten und Hintergrundbearbeitung Messenger API | **proposal** |  [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml)  |  [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg)  |
| Anzeige route/resource/view/table RestAlchemy | **proposal** |  [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml)  |  [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg)  |

## Detaillierte Abfolge-Diagramme

| Index | Status | Amt |
| --- | --- | --- |
|  [`diagrams/sequence/README.md`](diagrams/sequence/README.md)  | **proposal, auf dem laufenden Vertrag angezeigt** | Vollständige Matrix method+path: ein separater Markdown, der von PlantUML und SVG für jede öffentliche HTTP-Operation bearbeitet wird, sowie Events WebSocket. |

Jede Operationenspezifikation behält die aktuellen Requests/response, aber
zeigt die Zieltransaction/outbox/task/worker/event paths an. Diese Dokumente sind nicht
ersetzen [`workspace_api.md`](workspace_api.md).

## Proposal: Zilarchitektur Zulip Bridge

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`zulip_bridge/README.md`](zulip_bridge/README.md)  | **proposal; Index** | Einheitliche Navigation, angenommenen Invarianten, Glossar und kanonischen OPEN-list target Bridge. |
|  [`architecture_overview.md`](zulip_bridge/architecture_overview.md)  | **proposal** | Zwei Bridge-Prozesse, sticky whole-account ownership/scheduling, private Workspace API und eine strenge Grenze mit domain workers/WebSocket dispatcher. |
|  [`event_coverage.md`](zulip_bridge/event_coverage.md)  | **proposal; angenommenes Deckung** | Kanonische Matrix exact Zulip events/operations, Richtungen Workspace↔Zulip, source of truth und Schutz vor echo loop. |
|  [`realtime_connector.md`](zulip_bridge/realtime_connector.md)  | **proposal** | Ständige , zwei-seitige realtime Synchronisierung der unterstützten Änderungen, echo prevention, retry/backpressure/restart. |
|  [`history_importer.md`](zulip_bridge/history_importer.md)  | **proposal** | Root→per-stream newest-first tasks, fair pool default `4`, account rate limit und restart unfinished stream range ohne message checkpoint. |
|  [`internal_workspace_api.md`](zulip_bridge/internal_workspace_api.md)  | **proposal Über dem current mTLS** | Die gemeinsame private Command boundary, die Wiederverwendung der bestehenden External Bridge mTLS identity, server-owned scope und idempotency realtime/history. |
|  [`coordination_and_recovery.md`](zulip_bridge/coordination_and_recovery.md)  | **proposal** | Whole-account lease/fencing, Einheitliche queue bootstrap/boundary, retry/DLQ und recovery ohne Bridge-local durable DB. |
|  [`account_lifecycle_and_identity.md`](zulip_bridge/account_lifecycle_and_identity.md)  | **proposal; current routes preserved** | Account connect/reconnect/disconnect/delete, verified claim, unmanaged external users und multi-account canonical union. |
|  [`provider_mappings_and_content.md`](zulip_bridge/provider_mappings_and_content.md)  | **proposal** | Realm-scoped provider/topic/file mappings, canonical Markdown/URN, deferred references und manual reconversion. |
|  [`delivery_and_events.md`](zulip_bridge/delivery_and_events.md)  | **proposal** | Durable outbound operations, conflict/permanent-failure semantics und ready public event invariants. |

Das neue Verzeichnis beschreibt das Target Ingestion Design und ersetzt nicht die bestehenden
geschlossenen OpenAPI oder der Lebensmittelgrenze
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Worker, outbox, Projektion und Lieferung WebSocket

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md)  | **proposal** | Allgemeine Architektur der Worker und einzelne Prozesse `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
|  [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md)  | **proposal** | Transactional outbox, Eine separate immutable task für jedes Ereignis, scoped ownership, newest-first, ready events und separate dispatcher. |
|  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md)  | **die durchgeführte Operatorenprozedur** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup und generation-triggered fresh reimport. |

## Artefakte der geschlossenen provider/control API

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml)  | **gültiger geschlossener Vertrag** | Geschlossener Provider data-plane OpenAPI mit Authentifizierung bridge. |
|  [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml)  | **gültiger geschlossener Vertrag** | Provider-native command wire format mit server-owned Workspace scope; lease/result transport kompatibel mit v1. |
|  [`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml)  | **gültiger geschlossener Vertrag** | OpenAPI control plane für Zulip bridge. |
|  [`zulip_bridge_file_api_v1.yaml`](../zulip_bridge_file_api_v1.yaml)  | **gültiger geschlossener Vertrag** | Intern OpenAPI File-Übertragungen bridge. |

Die geschlossenen API sind keine Kundenrouten Workspace.
öffentlich API beschrieben in
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).

## Migration, Bereitstellung und Umsetzung

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md)  | **die vorliegende Anleitung; eine Vereinbarung ist erforderlich** | Verfahren zur Aktualisierung, Abwanderung und Überprüfung der laufenden Migration unread projection. |
|  [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md)  | **der aktuelle Plan der Aufnahme** | Überprüfen native Messenger/API/realtime/S3, Wiederherstellen, rebuild, scale und Belastungen. |
|  [`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md)  | **Wirkende Aufnahmbarriere** | Überprüfung IAM, Bridge, Wiederherstellung, UI und Ausrüstung der Integration. |

Proposal-die Dokumente sind nicht ein Migrations- oder Verkaufsplan. Production-
Änderungen beginnen erst nach einer einzelnen Architekturentscheidung und
die in Artikel 1 Absatz 2 genannten migration/test design.
