# Workspace-Backend-Dokumentation

Aktueller Vertrag: [Workspace public OpenAPI](../public_workspace_api.yaml).
Externe Konten sind auf Eigentümer-/Projekt-Ebene beschränkt. Zulip und Telegram synchronisieren über das kanonische Messenger-Modell, neben nativem Messaging.
Anbieter-Guides beschreiben die aktive Implementierung. Ältere Designvorschläge bleiben historische Referenzen und autorisieren keine Änderungen.

Der Haupt-Navigationsindex für die Workspace-Backend-Dokumentation. Der Status jedes Dokuments ist explizit angegeben: **aktiver Vertrag/aktive Architektur** beschreibt das aktuelle Verhalten, während ein **Vorschlag (Designvorschlag)** nur für zukünftige Refactoring-Designs gilt und keine Code-Änderungen autorisiert.

## Glossar der Projektdokumentation {#глоссарий-проектной-документации}

- placement — eine kanonische Nachricht in einem bestimmten Stream/Topic;
- binding — Zugriff und persönlicher Zustand eines Benutzers oder Containers;
- transactional outbox — ein Journal unveränderlicher Ereignisse innerhalb einer Schreibtransaktion;
- projection — vorberechneter Zustand für einfache API-Lesevorgänge;
- fan-out — Hintergrundverteilung von Bindings an Empfänger;
- worker (Hintergrund-Executor) — Handler für typisierte Aufgaben und Projektionen.

Entitätsnamen, Felder, Routen, JSON-Werte und Aufgabentypen in den Dokumenten bleiben in ihrer exakten Vertragsform erhalten.

## Telegram-Anbieter

[Telegram-Konten, automatische vollständige Historie und Synchronisation](telegram_provider.md).

## Öffentliche API und Designhistorie

| Dokument | Status | Zweck |
| --- | --- | --- |
| [`workspace_api.md`](workspace_api.md) | **aktiver Vertrag** | Kanonischer Client-Vertrag für Workspace/Messenger REST, Events und WebSocket: Routen, JSON, Statuscodes, Filter, Paginierung und die Laufzeit-/OpenAPI-Grenze. |
| [`workspace_ui_realtime_integration.md`](workspace_ui_realtime_integration.md) | **aktiver Vertrag** | REST-Backfill, Epoch-Cursor und WebSocket-Zustellung/-Wiederholung für die Workspace-UI. |
| [`architecture.md`](architecture.md) | **historische Referenz** | Eingestellte private Bridge- und Anbieter-API-Architektur. |
| [`workspace_server_v2_decisions.md`](workspace_server_v2_decisions.md) | **historische Referenz** | Eingestellte Provider API v2 und mTLS-Transportentscheidungen. |

## Vorschlag: Messenger-Domänen- und API-Architektur

| Dokument | Status | Zweck |
| --- | --- | --- |
| [`messenger_domain_model.md`](messenger_domain_model.md) | **Vorschlag** | Kanonisches `MESSAGE`, explizite Platzierung, Benutzer-Bindings für Nachrichten/Container, Invarianten und offene Entscheidungen. |
| [`messenger_api_domain_model.md`](messenger_api_domain_model.md) | **Vorschlag** | Drei Schichten: RestAlchemy API → einfache Ansichten → physische Entitäten; Abfrage-/Hintergrundverarbeitungspfade und Worker-Parallelität. |
| [`messenger_restalchemy_api_spec.md`](messenger_restalchemy_api_spec.md) | **Implementierungsvorschlag** | Spezifische RestAlchemy-Deklarationen, Ressourcen/Controller, Feldursprünge und der unveränderliche öffentliche JSON-Vertrag der Kern-API. |
| [`messenger_architecture_inventory.md`](messenger_architecture_inventory.md) | **Vorschlag; kanonisches Inventar** | Vereinheitlichtes Wörterbuch: Klasse→Tabelle/Ansicht→Felder→Schlüssel, UUIDs, Aufgaben-/Ereignistypen, Scope-Routing, Risikostatus und verbleibende OPEN-Entscheidungen. |

## Datenmodell und PlantUML-Übersichtsdiagramme

| Übersicht | Status | Quelle | SVG |
| --- | --- | --- | --- |
| Messenger-Domänen-ER-Modell | **Vorschlag** | [`messenger_domain_model.puml`](diagrams/messenger_domain_model.puml) | [`messenger_domain_model.svg`](diagrams/messenger_domain_model.svg) |
| Messenger-API-Schichten und Hintergrundverarbeitung | **Vorschlag** | [`messenger_api_domain_model.puml`](diagrams/messenger_api_domain_model.puml) | [`messenger_api_domain_model.svg`](diagrams/messenger_api_domain_model.svg) |
| RestAlchemy-Routen-/Ressourcen-/Ansichts-/Tabellen-Mapping | **Vorschlag** | [`messenger_restalchemy_api_spec.puml`](diagrams/messenger_restalchemy_api_spec.puml) | [`messenger_restalchemy_api_spec.svg`](diagrams/messenger_restalchemy_api_spec.svg) |

## Detaillierte Sequenzdiagramme für Operationen

| Index | Status | Zweck |
| --- | --- | --- |
| [`diagrams/sequence/README.md`](diagrams/sequence/README.md) | **Vorschlag, abgebildet auf den aktiven Vertrag** | Vollständige Methode+Pfad-Matrix: separates Markdown, bearbeitbares PlantUML und SVG für jede öffentliche HTTP-Operation sowie Events WebSocket. |

Jede Operationsspezifikation bewahrt den aktiven Request/Response, zeigt aber Ziel-Transaktions-/Outbox-/Task-/Worker-/Event-Pfade. Diese Dokumente ersetzen nicht [`workspace_api.md`](workspace_api.md).

## Worker, Outbox, Projektionen und WebSocket-Zustellung

| Dokument | Status | Zweck |
| --- | --- | --- |
| [`worker_flows/README.md`](diagrams/sequence/worker_flows/README.md) | **Vorschlag** | Allgemeine Worker-Architektur und separate Prozesse `fanout`, `content_mentions`, `reaction_snapshot`, `read_counters`, `delivery_snapshot_event`, `topic_membership_policy_rebuild`. |
| [`worker_architecture.md`](diagrams/sequence/worker_flows/worker_architecture.md) | **Vorschlag** | Transaktionale Outbox, separate unveränderliche Aufgabe für jedes Ereignis, beschränkte Eigentümerschaft, neueste zuerst, bereit-Ereignisse und ein separater Dispatcher. |
| [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md) | **implementiertes Betreiber-Verfahren** | Backup/Restore, native Erhaltung, Migrationzeitpunkt-Reset von Zulip-abgeleiteten Nachrichten/Dateien, dauerhafte Dateibereinigung und generierungsausgelöster frischer Reimport. |

## Migrations-, Bereitstellungs- und Implementierungsleitfaden

| Dokument | Status | Zweck |
| --- | --- | --- |
| [`messenger_unread_projection_rollout.md`](messenger_unread_projection_rollout.md) | **aktive Anweisung; erfordert Genehmigung** | Verfahren zum Aktualisieren, Zurückrollen und Verifizieren der aktuellen Migration der ungelesenen Projektion. |
| [`messenger_regression_test_plan.md`](messenger_regression_test_plan.md) | **aktiver Abnahmeplan** | Prüfungen für native Messenger/API/Echtzeit/S3, Wiederherstellung, Neuaufbau, Skalierung und Last. |

Vorschlagsdokumente sind kein Migrations- oder Implementierungsplan. Produktionsänderungen beginnen erst nach einer separaten architektonischen Entscheidung und einem vereinbarten Migrations-/Testdesign.
