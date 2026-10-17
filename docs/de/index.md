# Dokumentation Workspace Backend

Current contract: [Workspace public OpenAPI](../public_workspace_api.yaml).
External accounts contain an owner, `provider` and generic `settings`.
Workspace processes native messages and membership; provider synchronization and
external delivery live elsewhere. The remaining design and architecture pages
are historical references from before migration `0186`.

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

## Public API and design history

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`workspace_api.md`](workspace_api.md)  | **- ein gültiger Vertrag** | Kanonischer Kundenvertrag Workspace/Messenger REST, Events und WebSocket: Routen, JSON, Status, Filter, Pagination und Grenze runtime/OpenAPI. |
|  [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md)  | **- ein gültiger Vertrag** | REST-Überladen, Epoch-Cursor und Lieferung/Wiederholung WebSocket für Workspace UI. |
|  [`architecture.md`](architecture.md)  | **historical reference** | Retired private bridge and provider API architecture. |
|  [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md)  | **historical reference** | Retired Provider API v2 and mTLS transport decisions. |

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

## Worker, outbox, Projektion und Lieferung WebSocket

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md)  | **proposal** | Allgemeine Architektur der Worker und einzelne Prozesse `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
|  [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md)  | **proposal** | Transactional outbox, Eine separate immutable task für jedes Ereignis, scoped ownership, newest-first, ready events und separate dispatcher. |
|  [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md)  | **die durchgeführte Operatorenprozedur** | Backup/restore, native preserve, migration-time reset Zulip-derived messages/files, durable file cleanup und generation-triggered fresh reimport. |

## Migration, Bereitstellung und Umsetzung

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md)  | **die vorliegende Anleitung; eine Vereinbarung ist erforderlich** | Verfahren zur Aktualisierung, Abwanderung und Überprüfung der laufenden Migration unread projection. |
|  [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md)  | **der aktuelle Plan der Aufnahme** | Überprüfen native Messenger/API/realtime/S3, Wiederherstellen, rebuild, scale und Belastungen. |

Proposal-die Dokumente sind nicht ein Migrations- oder Verkaufsplan. Production-
Änderungen beginnen erst nach einer einzelnen Architekturentscheidung und
die in Artikel 1 Absatz 2 genannten migration/test design.
