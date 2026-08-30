# Zilarchitektur Zulip Bridge

Status: **proposal; der erste Server/Provider API v2 Teil ist separat festgehalten**.

[← Hauptindex der Dokumentation](../index.md) · [Kanonisches Inventar Messenger](../messenger_architecture_inventory.md) · [Die Grenze Zulip v1](../zulip_bridge_v1_product_and_api.md)

Wire transport, project scope, direct identity, outbound authorization und
provider event key Die erste Umsetzung geschlossen Entscheidungen `1B/2A/3A/4A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

Dieser Verzeichnis beschreibt die zweiprozessige Synchronisierungsarchitektur .
Workspace↔Zulip Es ändert nicht die bestehenden
Die öffentlichen Strecken,JSONSie können sich auch mit einem Kundenvertrag oder einem geschlossenen Vertrag beschäftigen.
Der Vertrag bleibt in
[`workspace_api.md`](../workspace_api.md), und die derzeitigen provider/control/file
Grenzen  in [`zulip_bridge_v1_product_and_api.md`](../zulip_bridge_v1_product_and_api.md)
und die zugehörigen OpenAPI-Dateien.

## Dokumente

| Dokument | Status | Amt |
| --- | --- | --- |
|  [`architecture_overview.md`](architecture_overview.md)  | **proposal** | Komponenten, Vertrauensgrenzen, Datenfluss und Verantwortungsabteilung zwischen Bridge und Workspace. |
|  [`event_coverage.md`](event_coverage.md)  | **proposal; angenommenes Deckung** | Die kanonische Matrix exact Zulip events/operations, Synchronisierungsrichtungen, Workspace actions, source of truth und echo prevention. |
|  [`realtime_connector.md`](realtime_connector.md)  | **proposal** | Konstante `Zulip Realtime Connector`: Ereignisempfang, Reihenfolge, Retry, Backpressure und graceful restart. |
|  [`history_importer.md`](history_importer.md)  | **proposal** | Endgültig `Zulip History Importer`: fair pool default `4`, per-stream newest-first work, account limiter, restart/dependencies/reconciliation. |
|  [`internal_workspace_api.md`](internal_workspace_api.md)  | **proposal** | Gemeinsames Internal Workspace API, begrenzte Service Identity, Transaktionsgrenze und einheitliche Idempotenz. |
|  [`coordination_and_recovery.md`](coordination_and_recovery.md)  | **proposal** | Einheitliche Bootstrap, Account Lease/fencing, Boundary, Retry/DLQ, Reconciliation und Wiederherstellung. |
|  [`account_lifecycle_and_identity.md`](account_lifecycle_and_identity.md)  | **proposal; current routes preserved** | Connect/reconnect/disconnect/delete, verified identity claim, unmanaged users und multi-account canonical union. |
|  [`provider_mappings_and_content.md`](provider_mappings_and_content.md)  | **proposal** | Realm-scoped provider keys, durable topic/file mappings, canonical Markdown/URN, deferred references und manual reconversion. |
|  [`delivery_and_events.md`](delivery_and_events.md)  | **proposal** | Durable Workspace→Zulip operations, conflict/permanent-failure semantics und exactly-one ready event per actual transition. |

## Diagramme

| Das ist ein Drehbuch. | PlantUML | SVG |
| --- | --- | --- |
| Realtime synchronization und echo prevention |  [`realtime_connector.puml`](diagrams/realtime_connector.puml)  |  [`realtime_connector.svg`](diagrams/realtime_connector.svg)  |
| History import |  [`history_importer.puml`](diagrams/history_importer.puml)  |  [`history_importer.svg`](diagrams/history_importer.svg)  |
| Erstimport und der Übergang zu realtime-only |  [`bootstrap_to_realtime.puml`](diagrams/bootstrap_to_realtime.puml)  |  [`bootstrap_to_realtime.svg`](diagrams/bootstrap_to_realtime.svg)  |
| Verified claim unmanaged identity |  [`identity_claim.puml`](diagrams/identity_claim.puml)  |  [`identity_claim.svg`](diagrams/identity_claim.svg)  |
| Shared topic mapping, rename und partial move |  [`topic_mapping_and_move.puml`](diagrams/topic_mapping_and_move.puml)  |  [`topic_mapping_and_move.svg`](diagrams/topic_mapping_and_move.svg)  |
| Content conversion, deferred repair und reconversion |  [`content_conversion_and_repair.puml`](diagrams/content_conversion_and_repair.puml)  |  [`content_conversion_and_repair.svg`](diagrams/content_conversion_and_repair.svg)  |
| Outbound retry, permanent failure und public events |  [`outbound_delivery.puml`](diagrams/outbound_delivery.puml)  |  [`outbound_delivery.svg`](diagrams/outbound_delivery.svg)  |

## Kanonisches Glossar

- **Bridge process** — Außen vertrauenswürdiger Prozess ohne direkten Zugriff auf die Datei
  Workspace;
- **service identity** — - Wirksam realm-bound mTLS identity private External
  Bridge API: `realm_uuid`, `provider_kind`, `bridge_instance_uuid` und
  `identity_generation` Nur von überprüften client certificate;
  account/project scope und zulässige Befehle Workspace dann definiert nach
  - und die server-owned assignments;
- **provider object key** — Stabile interne Identität des Zulip-Objekts,
  ist für realtime und history;
- **provider event key** — Stabiler Schlüssel einer Mutation/Version des Zulip-Objekts,
  als idempotency/derivation key;
- **provider object UUIDv5** — `UUIDv5(namespace=verified realm UUID,
  name="<entity_type>:<decimal_provider_id>")` für numeric Zulip objects;
- **registration boundary** — Grenze der neuen Zulip-Warteschlange: realtime nimmt an
  Die Veranstaltungen werden von ihr importiert, und die History Root importiert die gewählte Snapshot./rangeVor ihr.;
- **account lease/fencing generation** — Workspace-issued exclusive ownership
  Alle externen Konten für eine Bridge-Instanz; der stabile Besitzer kann nicht commit;
- **history root/stream task** — durable Workspace task: root Eröffnet den Scope und
  Erstellt Per-stream-Tasks; beim Neustart wiederholt die Stream-Task ihren gesamten Bereich;
- **deferred resolution** — eine aufbewahrte Abhängigkeit, die nicht angewendet werden kann
  bis zum Auftreten des Basisobjekts;
- **Workspace projection worker** — Innenarbeiter Workspace, der
  wird nach der Outbox ausgeführt; es ist nicht Bridge process;
- **WebSocket dispatcher** — Ein einzelner Bauteil Workspace, der liefert
  bereit durable public events und nicht an der Import- Zulip.

## Akzeptierte Invarianten

1. `Zulip Realtime Connector` und `Zulip History Importer`  unabhängige Prozesse
   mit einer gemeinsamen Identitätssemantik/idempotency, aber unterschiedlichen Lebenszyklen.
2. Kein Bridge-Prozess schreibt direkt in Workspace PostgreSQL oder object
   storage metadata. Alle Domänenmutationen gehen durch eine begrenzte
   - Innen Workspace API.
3. Benutzerzugriffs-Token sind nicht verwendet. Bridge kann nicht auswählen
   Sie können auch die Werte `project_id`, source oder Workspace user wählen.
   Überprüft Workspace für Service Identity und Server mappings.
4. Nachrichten erstellen mit einer normalen Domain-Transaktion Workspace:
   canonical `MESSAGE` + obligatorische `TOPIC` und `MESSAGE_PLACEMENT` + Urheberrechte
   `USER_MESSAGE_BINDING`/`USER_MESSAGE_STATE` + immutable outbox event.
5. Öffentliche UUID Nachrichten sind gleich placement UUID:
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Canonical `MESSAGE.uuid`
   bleibt intern.
6. Bridge nicht den Empfänger fan-out, nicht aktualisiert Workspace projections und
   Es wird keine public WebSocket Events erstellt. Workspace workers;
   dispatcher Er liefert nur die Vorbereitungen..
7. Connect, reconnect, queue expiry, missing heartbeat, `restart` und
   `web_reload_client` Benutzen Sie einen Bootstrap: Registrieren Sie einen neuen queue,
   Sie erhalten die Grenze, starten Sie die Echtzeit und erst dann erstellen Sie history root task.
   Die alte queue/cursor ist kein durable state; die Überlappungen/no-gap sorgen dafür, dass
   boundary und allgemeine provider keys.
8. Alte UUID der früheren Zulip-Importe nach dem vereinbarten Full Reset speichern
   Innerhalb eines neuen Imports muss jeder Retry/resume erneut durchgeführt werden
   Gleiche neue Adresse anmelden canonical row.
9. Die kanonische Abdeckung und Richtung jeder Zulip Event Family gibt an
      [`event_coverage.md`](event_coverage.md). Bidirectional mutation - Er trägt.
   origin/causation/provider identity; Eigener Provider echo bestätigt
   Die ursprüngliche Operation wird nicht mehr ausgeführt und die endlose Rückmeldung wird nicht mehr ausgeführt.
10. Durable mappings, assignments, leases, tasks, outbound operations und errors
    Gehören .Workspace. Bridge-Instanzen haben keine gemeinsamen Bridge database;
    local state ist nur ein abwerfer cache.
11. Ein Account gehört vollständig einem fenced Bridge owner: realtime und
    history nicht zwischen instances. Assignment sticky; healthy accounts
    automatisch nicht re-balanciert werden , wenn ein neuer instance.
12. Bridge Transformiert provider events/operations, aber implementiert nicht Workspace
    domain policy. History visibility, bindings und archive semantics entscheidet
    Workspace nach current stream settings.
13. Beide Bridge-Prozesse nutzen die aktuelle Authentifizierung. private
    External Bridge API: TLS 1.2+ mutual TLS, realm control CA, Einmalverwendung
    enrollment und generation-bound client certificate. HTTP headers/body nicht
    Sie können es ersetzen. certificate identity. Whole-account lease/fencing —
    Zusätzliche transaction-time authorization, nicht credential und nicht ersetzen
    mTLS.

## Einheitliche Liste der OPEN-Lösungen Zulip Bridge {#единый-список-open-решений-zulip-bridge}

Dies ist die einzige Liste der ungeklärten Lösungen für diesen Katalog.
Die Dokumente werden hier verlinkt und nicht als eigene Kopien erstellt.

Früher wurden Wire Transport, Event/direct Keys, Private Initiation Surface und
cross-account project scope Schließung von Entscheidungen `1B/2A/3A/4A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

1. Operational upper limits Nachher load tests: maximum/optimal history worker
   pool über default `4`, history batch/rate budgets, provider admission und
   retention failed history/DLQ/deferred evidence, nicht mit den angenommenen
   successful/permanent-operation TTL.
   Alle Wege sind bounded/configurable; ein Account-Level Limiter und realtime priority
   bereits registriert.
2. Richtung und Modell `saved_snippets`: die Familie bleibt `OPEN` und nicht
   wird automatisch interpretiert als Workspace draft/message.
3. Genaue Darstellung realm-wide Zulip `realm_user/update person.role` auf
    Workspace role model. Es darf nicht still werden channel-specific
    `WorkspaceStreamBinding.role`.
4. Exact converter edge/loss policy für Zulip→canonical Markdown und umgekehrt
    URN resolution, einschließlich unsupported Zulip markup.
    manual reconversion boundary bereits angenommen.

Retention nicht mehr OPEN: completed history tasks und successful outbound
operations aufbewahrt werden `30 days`, internal permanent-failure operation/code/reason
— `90 days`, provider mappings/latest hidden raw metadata — lifetime - die
entity. Eine mögliche future manual requeue bleibt eine interne Erweiterung, keine neue
current public endpoint. Retention failed history/DLQ/deferred evidence Bleibt.
OPEN #1 und nicht durch Werte ersetzt werden `30/90 days`.

Die verknüpften allgemeinen OPEN-Lösungen Messenger, einschließlich capacity/SLO, bleiben in
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#единственный-список-open-решений).

[← Hauptindex der Dokumentation](../index.md) · [Kanonisches Inventar Messenger](../messenger_architecture_inventory.md) · [Die Grenze Zulip v1](../zulip_bridge_v1_product_and_api.md)
