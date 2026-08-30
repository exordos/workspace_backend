# Workspace v1 API

Dieses Dokument beschreibt den browser-orientierten API Vertrag, der von nginx aus
die aufbewahrten `workspace-messenger-api`, die gemeinsamen `workspace-api` und die
- Ich bin ein Freund .`workspace-messenger-events`Websocket-Dienstleistung.Messenger
Anfragen verwenden den dedizierten Messenger Prozess; gemeinsame Benutzer, Kundenservice
Einstellungen, Registrierungen von Push-Geräten und REST Ereignisse verwenden `workspace-api`.
Alleine Mail, Kalender und Externe Benutzer Endpunkte sind nicht Teil dieser
Anbieter-neutrale externe Konten, Chat, Betrieb, Richtlinien, Gesundheit,
und Brückeninstanzressourcen sind Teil derMessenger API.

Native Messenger Ressourcen, Mitgliedschaft, Benutzerzustand, Ereignisse, Anbieter-Mapping,
und die Klienten-Einstellungen sind kanonisch inPostgreSQL. Die vorhandenen Ursprungsfelder
die Anbieteridentität ohne Browserwechsel bewahren API.

## Ausführungszeit-Eingangspunkte

Direktes örtliches Angebot:

```text
Messenger REST API:  http://127.0.0.1:21081/v1
Events WebSocket:    ws://127.0.0.1:21082/v1/events/ws
Workspace REST API:  http://127.0.0.1:21084/v1
Worker:              workspace-messenger-worker
Messenger OpenAPI:   http://127.0.0.1:21081/specifications/3.0.3
Workspace OpenAPI:   http://127.0.0.1:21084/specifications/3.0.3
```

Das Backend-Nginx-Manifest zeigt diese internen Gateway-Routen.
`workspace_ui` Lastbalancer-Proxys `/api/` an dieses Gateway ohne Umschreiben
der Weg:

```text
Workspace REST root: /api/workspace/v1/...
Messenger REST:      /api/workspace/v1/messenger/...
Events REST:         /api/workspace/v1/events/...
Events WebSocket:    /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>
OpenAPI spec:        /api/workspace/specifications/3.0.3
```

`/api/workspace/v1/messenger/` ist an die gespeicherte Messenger REST
Dienst am `127.0.0.1:21081`; der Rest von `/api/workspace/` wird durch
der Workspace REST-Dienst am `127.0.0.1:21084`.
Der genaue nginx-Standort `/api/workspace/v1/events/ws` ist dem
Websocket-Dienst-Endpunkt `/v1/events/ws` auf `127.0.0.1:21082`.

Das Backend-Nginx-Manifest setzt `client_max_body_size 50m` für Proxy-Anfragen.
Es dient nicht der Web-Benutzeroberfläche; nicht übereinstimmende nicht-API Pfade kehren `404` zurück.

## Allgemeine Vorschriften {#general-rules}

- Die Anforderungs- und Antwortkörper sind JSON (`application/json`).
- Ressourcen-Identifikatoren sind UUIDs, es sei denn, ein Feld sagt ausdrücklich etwas anderes.
- Zeitstempel sind UTC Datumszeiten, die als ISO-8601 Zeichenfolgen serialisiert sind.
- REST Authentifizierung verwendet ein Genesis IAM Träger-Token:

```http
Authorization: Bearer <token>
```

Um ein Token in der lokalen Testumgebung zu erhalten, fordern Sie es von Exordos Core IAM an
durch das Gateway und verwenden Sie das Feld `access_token` aus der Antwort:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=login%2Bpassword&
login=<test-user>&
password=<test-password>&
scope=openid+email+profile+project%3A<project-uuid>&
ttl=3600&
refresh_ttl=172800
```

Die gleiche Tokenanfrage kann auch als JSON gesendet werden:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "login+password",
  "login": "<test-user>",
  "password": "<test-password>",
  "scope": "openid email profile project:<project-uuid>",
  "ttl": 3600,
  "refresh_ttl": 172800
}
```

Der Benutzeroberflächen-Client verwendet den Standardclient IAM.
`ttl=3600` bedeutet, dass das Zugangs-Token für 1
`refresh_ttl=172800` bedeutet, dass das Refresh-Token für 2 Tage ausgegeben wird.

Beispiel für eine authentifizierte Anfrage:

```http
GET /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token from IAM response>
```

Um ein abgelaufenes Zugriffstoken zu aktualisieren, senden Sie das aktualisierende Token an das gleiche Standard-Token
Endpunkt des Kunden:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=refresh_token&
refresh_token=<refresh_token from IAM response>
```

JSON Auffrischungskörper wird auch akzeptiert:

```http
POST /api/core/v1/iam/clients/default/actions/get_token/invoke
Content-Type: application/json
Accept: application/json

{
  "grant_type": "refresh_token",
  "refresh_token": "<refresh_token from IAM response>"
}
```

Verwenden Sie die neue `access_token` aus der Antwort für den nachfolgenden Messenger API
Wenn die Erneuerungsantwort eine neue `refresh_token` enthält, ersetzen Sie die
- Das ist ein Refresh-Token.

`user_uuid` wird von IAM Token-Informationen genommen. `project_id` wird von IAM genommen
Nutzer-Scoped-Ressourcen filtern automatisch und/or
Schreiben Sie den Strom `user_uuid`.

Typische Fehlerreaktion von RESTAlchemy/IAM:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

Der HTTP Reaktionskörper ist das Fehlerobjekt selbst; es gibt keine äußere `json`
Messenger Validierungsfehler verwenden HTTP `400`.
Die Operationen werden mit einem spezifischeren Anwendungscode im gleichen Feld `code` versehen:

| Antragscode | Typ | Betrieb |
| --- | --- | --- |
| `400001004` | `InvalidStreamBindingRoleError` | Hinzufügen von Benutzern mit einer nicht unterstützten bindenden Rolle. |
| `400001005` | `StreamBindingUsersPayloadError` | Zusatz von Benutzern mit einem Rollenwert, der nicht eine Liste von Benutzer-UUIDs ist. |
| `400001006` | `InvalidTopicNotificationModeError` | Wählen Sie einen mit dem Streammodus unvereinbaren Themenbenachrichtigungsmodus aus. |
| `400001007` | `StreamDefaultTopicNotConfiguredError` | Erstellen Sie eine Nachricht ohne `topic_uuid`, wenn der Stream kein Standardthema hat. |

Messenger Ressourcen halten eine kanonische Herkunftsprojektion statt der Exposition
Beförderungs-Identifikatoren:

```json
{
  "provider": {
    "kind": "zulip",
    "account_uuid": "account-uuid",
    "external_id": "provider-entity-id",
    "capabilities": {},
    "delivery_class": "live",
    "notification_eligible": true
  },
  "delivery": {
    "external_operation_uuid": "operation-uuid",
    "status": "pending",
    "safe_error": null,
    "can_retry": false,
    "can_discard": false,
    "duplicate_risk": false,
    "retry_requires_confirmation": false,
    "original_url": null,
    "reconciliation_reason": null,
    "updated_at": "2026-07-15T09:30:00.000000Z"
  }
}
```

`provider.capabilities` enthält effektive Handlungsbeschreibungen.
die Anbieterprognosen `delivery_class` sind `live` oder `backfill` und
`notification_eligible` stellt fest, ob die Nachricht benachrichtigen könnte, wenn die
Back-Fill und Live-Verkehr vor dem Konto akzeptiert
Notification Gate öffnet sich `false`; Clients müssen den Desktop unterdrücken
Die lokale Bevölkerung hat eine große Anzahl von
`provider: null` und `delivery: null`. Provider-Synchronisierungs-Cursoren, Rohprotokoll
Die Datenbank ist nicht für die Nutzung von Daten, die in der Datenbank gespeichert werden.
Ein Vertrag.


Der Browser-Client verwendet für jeden IAM gleiches Träger-Token und Projektumfang
Der öffentliche Server-Discovery-Endpunkt ist
`GET /api/workspace/v1/messenger/server_settings`; es ist die einzige
nicht authentifizierter Workspace Endpunkt, der von der Benutzeroberfläche verwendet wird.

Dies ist ein öffentliches Layout.
`/api/messenger/**`, `/api/v1/**`,
`/api/workspace/v1/messenger/events/**`, oder der frühere Messenger-Websocket
Es gibt keinen browserorientierten Provider API.
Die Ausführungszeiten verwenden die private Brücke-Authentifizierung API mit Root-
`/api/workspace-provider/v1`; seine Geschäfte verpflichten sich zu gewöhnlichen Messenger
Ressourcen in PostgreSQL, so dass es den beschriebenen Browservertrag nicht ändert
Die private Vereinbarung ist in diesem Dokument definiert.
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

## Seitenaufzeichnung und Filter

Die Sammlung Endpunkte verwenden die Kurzer-Pagierung RESTAlchemy:

| Abfrageparameter | Typ | Beschreibung |
| --- | --- | --- |
| `page_limit` | ganzzahl | Höchstzahl der Elemente. `0` oder ein ausgelassener Wert bedeutet keine explizite Grenze. |
| `page_marker` | UUID oder ganze Zahl | Marker für die nächste Seite. UUID Ressourcen verwenden die letzte `uuid` der vorherigen Seite; Ereignisse verwenden die letzte `epoch_version` der vorherigen Seite und erfordern die Übereinstimmung `epoch_generation`, wenn dieser Marker nicht null ist. |

Wenn `page_limit` angegeben ist, sind die Antworten `X-Pagination-Limit`.
Seite existiert, enthalten die Antworten auch `X-Pagination-Marker`.

`GET /api/workspace/v1/messenger/messages/` verwendet einen stabilen Komposit-Tastatur.
Setzt .`sort_key=created_at`Und ...`sort_dir=asc`Oder ...`sort_dir=desc`; Zeilen werden in der Reihenfolge angeordnet
- Ich weiß .`(created_at, uuid)`in diese Richtung.`page_marker`bleibt dieUUIDder
Der Server löst die Frage, ob die Daten in der letzten Zeile zurückgegeben wurden, um den öffentlichen Clientvertrag zu erhalten.
dass UUID innerhalb desselben IAM-Projekts, Authentifizierungsbenutzeransicht und Nachricht
Ein Marker außerhalb des Filters wird mit einem Filter ausgestattet, der den Filterbereich und dann den Kompositschlüssel genau folgt.
`X-Pagination-Marker` wird nur ausgestrahlt, wenn ein
`page_limit + 1` Sonde beweist, dass eine andere Zeile existiert, so dass eine vollständige letzte Seite
Sie werden nicht für eine nicht existierende Fortsetzung werben.

`GET /api/workspace/v1/messenger/drafts/` verwendet denselben UUID-Markervertrag,
Befehl von`(updated_at, uuid)`- Setzt .`sort_key=updated_at`und
`sort_dir=asc|desc`; die optionale `stream_uuid` und `topic_uuid` Filter bleiben erhalten
Ein Marker außerhalb dieser genauen
Die Umfangsberechnung gibt `404` zurück.

Workspace Sammelkontrollen unterstützen auch bedingte Filternachfolger:

| Nachfolgende | Bedeutung | Beispiel |
| --- | --- | --- |
| `>` | streng größer als | `epoch_version>123` |
| `<` | streng weniger als | `epoch_version<123` |
| `=>` | größer als oder gleich | `epoch_version=>123` |
| `=<` | kleiner als oder gleich | `epoch_version=<123` |

Wenn ein Abfrageparametername `>` oder `<` enthält, URL-codieren Sie es, wenn die HTTP
Der Client macht das nicht automatisch:

```http
GET /api/workspace/v1/events/?epoch_version%3E=123&epoch_generation=781203&page_limit=500
```

Veranstaltungspaginierung und Wiederverbindung mit dem Cursor-Paar
`(epoch_generation, epoch_version)`, nicht nur eine Epochenzahl.
`0` kann `epoch_generation` weglassen.
Beginnt bei Epoche `1`, dass kalte Anfrage die gleiche HTTP 410 Gap-Antwort zurückgibt
als jeder andere Cursor, der kein komplettes Delta erzeugen kann; der Client muss
Autoritative Momentaufnahmen vor dem Start eines neuen Cursors.

## Zusammenfassung des Endpunkts {#endpoint-summary}

| Methode | Weg | Beschreibung |
| --- | --- | --- |
| `GET` | `/api/workspace/v1/` | Liste der unter `/api/workspace/v1/` stehenden Strecken. |
| `GET` | `/api/workspace/v1/messenger/` | Liste der Messenger Strecken unter `/api/workspace/v1/messenger/`. |
| `GET` | `/api/workspace/v1/messenger/server_settings` | Die Server-Einstellungen wie Zulip zurückgeben. |
| `GET` | `/api/workspace/v1/messenger/server_settings/` | Das gleiche wie oben; Trailing Slash wird unterstützt. |
| `GET` | `/api/workspace/v1/messenger/folders/` | Liste der Ordner für den aktuellen Benutzer IAM. |
| `POST` | `/api/workspace/v1/messenger/folders/` | Erstellen Sie einen Ordner. |
| `GET` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Holen Sie sich einen Ordner. |
| `PUT` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Aktualisieren Sie einen Ordner. |
| `DELETE` | `/api/workspace/v1/messenger/folders/{folder_uuid}` | Löschen Sie einen Ordner. |
| `GET` | `/api/workspace/v1/messenger/folder_items/` | Liste der Ordner-Elemente für den aktuellen Benutzer IAM. |
| `POST` | `/api/workspace/v1/messenger/folder_items/` | Erstellen Sie ein Ordner. |
| `GET` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Holen Sie sich einen Ordner. |
| `DELETE` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}` | Löschen Sie ein Ordnerelement. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/pin/invoke` | Stecken Sie einen Ordner. |
| `POST` | `/api/workspace/v1/messenger/folder_items/{folder_item_uuid}/actions/unpin/invoke` | Entpinnen Sie einen Ordner. |
| `GET` | `/api/workspace/v1/messenger/streams/` | Liste der für den aktuellen IAM Benutzer sichtbaren Streams. |
| `POST` | `/api/workspace/v1/messenger/streams/` | Erstellen Sie einen Strom. |
| `GET` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Holen Sie sich einen Strom. |
| `PUT` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Aktualisieren Sie einen Stream. |
| `DELETE` | `/api/workspace/v1/messenger/streams/{stream_uuid}` | Löschen Sie einen Stream für alle Stream-Benutzer. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke` | Benutzer nach Rollen in einen Stream hinzufügen. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/archive/invoke` | Die Angabe `is_archived: true`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/unarchive/invoke` | Die Angabe `is_archived: false`. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/notifications/invoke` | Setzen Sie den Stream-Benachrichtigungsmodus des aktuellen Benutzers. |
| `POST` | `/api/workspace/v1/messenger/streams/{stream_uuid}/actions/read/invoke` | Alle nicht gelesenen Stream-Nachrichten werden für den aktuellen Benutzer als gelesen markiert. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/` | Liste der Strombindungen. |
| `GET` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | - Holen Sie eine Strömung. |
| `PUT` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Aktualisieren Sie eine Strombindung. |
| `DELETE` | `/api/workspace/v1/messenger/stream_bindings/{binding_uuid}` | Entfernen Sie einen Benutzer aus einem Stream. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/` | Liste der Themen, die dem aktuellen IAM Benutzer sichtbar sind. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/` | Erstelle ein Thema. |
| `GET` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Suchen Sie sich ein Thema. |
| `PUT` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Ein Thema umbenennen; der Inhalt muss `name` enthalten. |
| `DELETE` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}` | Löschen Sie ein Thema. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` | Schalten Sie die gemeinsame `is_done`-Flagge für alle Thema-Benutzer ein. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` | Setzen Sie den Themenbenachrichtigungsmodus des aktuellen Benutzers. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` | Machen Sie das Thema zum Standardthema des Streams. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke` | Aktualisieren Sie Besitzer/administrator-managed für die Zusammenfassung der Konfiguration pro Thema, einschließlich aktivieren/disable. |
| `GET` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | Liste der globalen OpenAI-kompatiblen Zusammenfassungsendpunkte; erfordert `workspace.topic_summary_endpoint.manage`. |
| `POST` | `/api/workspace/v1/messenger/topic_summary_endpoints/` | Erstellen eines globalen Zusammenfassungsendpunktes mit einer Schreib-allein-Zertifizierung; erfordert `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/messenger/topic_summary_endpoints/{endpoint_uuid}` | Lesen, Aktualisieren oder Löschen eines globalen Zusammenfassungsendpunktes; erfordert `workspace.topic_summary_endpoint.manage`. |
| `GET`, `PUT` | `/api/workspace/v1/messenger/topic_summary_settings/{project_uuid}` | Lesen Sie beide Zusammenfassungs-Gates oder aktualisieren Sie beide mit `workspace.topic_summary_settings.manage`. |
| `POST` | `/api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke` | Alle ungelesenen Themennachrichten werden für den aktuellen Benutzer als gelesen markiert. |
| `GET` | `/api/workspace/v1/messenger/messages/` | Liste der für den aktuellen IAM Benutzer sichtbaren Nachrichten. |
| `POST` | `/api/workspace/v1/messenger/messages/` | Erstellen Sie eine Nachricht. |
| `GET` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Erhalten Sie eine Nachricht. |
| `PUT` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Aktualisieren Sie eine Nachricht. |
| `DELETE` | `/api/workspace/v1/messenger/messages/{message_uuid}` | Löschen Sie eine Nachricht. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read/invoke` | Die Nachricht wird für den aktuellen Benutzer als gelesen markiert. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/read_up_to/invoke` | Ungelesenen Nachrichten in demselben Thema bis zu dieser Nachricht als gelesen markieren. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/star/invoke` | Sternmeldung für den aktuellen Benutzer. |
| `POST` | `/api/workspace/v1/messenger/messages/{message_uuid}/actions/unstar/invoke` | Unstar-Nachricht für den aktuellen Benutzer. |
| `GET` | `/api/workspace/v1/messenger/drafts/` | Liste der Entwürfe des aktuellen Benutzers, optional nach Stream oder Thema gefiltert. |
| `POST` | `/api/workspace/v1/messenger/drafts/` | Erstellen Sie einen Entwurf mit einem vom Client generierten UUID. |
| `GET` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Erhalten Sie einen eigenen Entwurf und seine starke Revision ETag. |
| `PUT` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Ersetzen Sie nur die Markdown-Nutzlast mit `If-Match`. |
| `DELETE` | `/api/workspace/v1/messenger/drafts/{draft_uuid}` | Ein Eigentumsentwurf mit `If-Match` hart löschen. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/` | Liste der externen Konten des aktuellen Benutzers im Bereich Global; erfordert `workspace.external_account.read`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/` | Erstellen eines externen Kontos mit einem vom Client generierten UUID und nur schreibbaren Zugangsdatum; erfordert `workspace.external_account.create`. |
| `GET` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Erhalten Sie den entlasteten externen Account-Snapshot des Eigentümers; erfordert `workspace.external_account.read`. |
| `PUT` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Die nicht geheimen Einstellungen, die geändert werden können, durch `If-Match` ersetzen; erfordert `workspace.external_account.update`. |
| `DELETE` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}` | Das Konto und seine Projektion löschen; erfordert `workspace.external_account.delete`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/reconnect/invoke` | Validieren und ersetzen Sie die Schreibkredienzial, dann die Synchronisation wieder aufnehmen; erfordert `workspace.external_account.reconnect`. |
| `POST` | `/api/workspace/v1/messenger/external_accounts/{account_uuid}/actions/disconnect/invoke` | Synchronisierung unter Beibehaltung der Leseschaltvorlage stoppen; erfordert `workspace.external_account.disconnect`. |
| `GET` | `/api/workspace/v1/messenger/external_chats/` | Liste des vom Besitzer entfernten externen Chat-Katalogs und Zuteilungsstatus auf. |
| `GET` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}` | Holen Sie einen entferntem externen Chat-Snapshot. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/select/invoke` | Wählen Sie einen Chat aus und ordnen Sie ihn einem Projekt zu. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/deselect/invoke` | Annullieren Sie die Arbeit und entfernen Sie die Chat-Projektion. |
| `POST` | `/api/workspace/v1/messenger/external_chats/{chat_uuid}/actions/move/invoke` | Atomisch eine Projektion mit `If-Match` in ein anderes Projekt zu verschieben. |
| `GET` | `/api/workspace/v1/messenger/external_operations/` | Liste der externen Operationen des Eigentümers. |
| `GET` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | Entfernen Sie den Betriebsstatus. |
| `DELETE` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}` | Werden die für die Arbeit berechtigten Stellen verworfen. |
| `POST` | `/api/workspace/v1/messenger/external_operations/{operation_uuid}/actions/retry/invoke` | Versuchen Sie erneut eine ausreichende, fehlgeschlagene Operation. |
| `POST` | `/api/workspace/v1/messenger/external_operations/actions/preflight/invoke` | Überprüfen Sie die Fähigkeit und den Umwandlungsausfall vor einer Ausgangsmutation. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/` | Liste der gesäuberten Bridge-Instanzen; erfordert die dedizierte IAM Leserechte. |
| `GET` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}` | Entfernen Sie die Identität, Gesundheit, Fähigkeit und den Zertifikatstatus der Brücke. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | Brücken-Identität aussetzen. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/resume/invoke` | Wiederholen Sie eine nicht widerrufte Brückenidentität. |
| `POST` | `/api/workspace/v1/messenger/external_bridge_instances/{instance_uuid}/actions/revoke/invoke` | Die generierte aktive Brücke wird widerrufen. |
| `GET` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | Lesen Sie die Politik für den Reinigungsbereich für einen Anbieter. |
| `PUT` | `/api/workspace/v1/messenger/external_provider_policies/{kind}` | Aktualisieren Sie die Providerrichtlinie mit `If-Match` und der speziellen IAM-Berechtigung. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/suspend/invoke` | Suspendieren Sie einen Anbieter. |
| `POST` | `/api/workspace/v1/messenger/external_provider_policies/{kind}/actions/resume/invoke` | Nach der Validierung eine Anbieterart wieder aufnehmen. |
| `GET` | `/api/workspace/v1/messenger/external_provider_health/{kind}` | Lesen Sie die gesundheitliche Behandlung der Anbieter. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/` | Liste der Reaktionen für Nachrichten, die für den aktuellen IAM Benutzer sichtbar sind. |
| `POST` | `/api/workspace/v1/messenger/message_reactions/` | Erstellen Sie eine Nachricht Reaktion. |
| `GET` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Eine Nachrichtensendung wird durch den Nachrichtenzugriff sichtbar gemacht. |
| `PUT` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Aktualisieren Sie die Reaktion des aktuellen Benutzers. |
| `DELETE` | `/api/workspace/v1/messenger/message_reactions/{reaction_uuid}` | Löschen Sie die Reaktion des aktuellen Benutzers. |
| `GET` | `/api/workspace/v1/messenger/files/` | Liste der Dateien, die für den aktuellen IAM Benutzer sichtbar sind. |
| `POST` | `/api/workspace/v1/messenger/files/` | Erstellen von Dateimetadaten oder Hochladen von mehrteiligen Dateidaten. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}` | Holen Sie sich einen sichtbaren Datensatz. |
| `PUT` | `/api/workspace/v1/messenger/files/{file_uuid}` | Aktualisieren Sie einen eigenen Datenträger-Metadaten-Eintrag. |
| `DELETE` | `/api/workspace/v1/messenger/files/{file_uuid}` | Löschen Sie eine Eigentumsdatei und ihre Zugriffszeilen. |
| `GET` | `/api/workspace/v1/messenger/files/{file_uuid}/actions/download` | Laden Sie sichtbare Datei-Bytes herunter. |
| `GET` | `/api/workspace/v1/services/` | Liste der verfügbaren Workspace Dienstleistungen. |
| `GET` | `/api/workspace/v1/services/{service_uuid}` | Erhalten Sie einen verfügbaren Workspace Service. |
| `PUT` | `/api/workspace/v1/push_devices/{registration_uuid}` | Die Push-Geräte des aktuellen Benutzers können gleichzeitig registriert oder gedreht werden. |
| `DELETE` | `/api/workspace/v1/push_devices/{registration_uuid}` | Die Push-Geräte-Registrierung des aktuellen Benutzers wird idempotently gelöscht. |
| `GET` | `/api/workspace/v1/events/` | Liste dauerhafter Echtzeitereignisse für den aktuellen Benutzer IAM. |
| `GET` | `/api/workspace/v1/epoch/` | Gibt die letzte sichtbare Ereigniszeit des aktuellen Benutzers zurück. |
| `GET` | `/api/workspace/v1/users/` | Liste der Benutzer des Arbeitsbereichs. |
| `GET` | `/api/workspace/v1/users/{user_uuid}` | Holen Sie sich einen Arbeitsplatzbenutzer. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/presence/invoke` | Aktualisieren Sie den Anwesenheitsstatus und den Herzschlag des aktuellen Benutzers. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_upload/invoke` | Laden Sie den Avatar des aktuellen Benutzers hoch und wählen Sie ihn aus. |
| `POST` | `/api/workspace/v1/users/{user_uuid}/actions/avatar_reset/invoke` | Entfernen Sie den benutzerdefinierten Avatar des aktuellen Benutzers und stellen Sie den kanonischen Gravatar URN wieder her. |
| `GET` | `/api/workspace/v1/me/` | Die aktuelle authentifizierte Workspace Benutzer zurückgeben. |

### Grenze des Außenintegrationsvertrags

Die obige Endpunktabelle ist die kanonische Bestandsaufnahme der aktuellen
IAM-authentifizierte Browserrouten. Die generierte OpenAPI ist für
die Anforderungs- und Antwortschemata von vom Controller unterstützten Operationen HTTP, vorbehaltlich
Die Ausnahme für die Nachricht-Reaktionsprojektion ist nachstehend dokumentiert.
`server_settings` Middleware Alias und die Ereignisse WebSocket sind Laufzeit Eintrag
Punkte, die in dieser Datei dokumentiert sind, aber nicht durch OpenAPI-Vorgänge erzeugt werden.

Externe Kontoeinstellungen, Chat-Quell-Metadaten und Verwendung von Betriebsdetails
Zulip ist die erste registrierte Art; Hinzufügen einer anderen Art
Die Datenbank wird nicht für die Bereitstellung von Daten verwendet.
Beispiele, ETag und `If-Match`-Regeln, Handlungserlaubnisse, Lebenszyklussemantik,
und Verwaltung Verhalten für Konten, Chats, Operationen, Brücken Instanzen,
Die Gesundheitspolitik der Anbieter und die Gesundheitspolitik der Anbieter sind in den Abschnitten 5 und 6 der
[`zulip_bridge_v1_product_and_api.md`](zulip_bridge_v1_product_and_api.md).
Die private Steuerung, die Datenbereitstellung und die Dateiübertragung beschreiben
Backend-to-Bridge-Verträge und sind nicht Teil des öffentlichen Browsers API.

## Server-Einstellungen {#server-settings}

`GET /api/workspace/v1/messenger/server_settings` ist öffentlich und erfordert nicht `Authorization`.
nicht unterstützt
Die Abfrageparameter werden in
`ignored_parameters_unsupported`. `realm_url` und `realm_uri` verwenden die Anfrage
`Host` Header und Standard für das öffentliche HTTPS Schema.
kann ausdrücklich `X-Forwarded-Proto` bereitstellen; die verpackte Workspace nginx
Die Konfiguration setzt sie auf `https`, da TLS vor der internen
HTTP hop. `realm_icon` verwendet `urn:url:<https-url>` für eine anonymisierte
URN wird von einem anderen Benutzer verwendet, der das HTTPS URL
Der Wert wird aus dem kanonischen Anforderungsbereich abgeleitet, wie
`urn:url:<realm>/logo-512x512.png`; nginx dient diesem Pfad von der verpackten
512×512 Organisationssymbol.

Beispiel für die Antwort:

```json
{
  "result": "success",
  "msg": "Welcome to Exordos Workspace",
  "authentication_methods": {
    "password": true,
    "dev": false,
    "email": true,
    "ldap": false,
    "remoteuser": false,
    "github": false,
    "azuread": false,
    "gitlab": false,
    "google": false,
    "apple": false,
    "saml": false,
    "openid connect": false
  },
  "push_notifications_enabled": true,
  "email_auth_enabled": true,
  "require_email_format_usernames": true,
  "realm_url": "https://workspace.example.com",
  "realm_name": "Exordos Workspace",
  "realm_icon": "urn:url:https://workspace.example.com/logo-512x512.png",
  "realm_description": "<p>Exordos Workspace messenger.</p>",
  "realm_web_public_access_enabled": false,
  "meet_url": "https://meet.genesis-core.tech",
  "external_authentication_methods": [],
  "realm_uri": "https://workspace.example.com"
}
```

## Schubgeräte {#push-devices}

`PUT /api/workspace/v1/push_devices/{registration_uuid}` ist ein Ersatz-Stil
Der Client erzeugt eine stabile UUID pro Anwendungsinstallation.
Erstregistrierung zurückgibt `201`; ersetzt sein FCM-Token oder Verschlüsselungsschlüssel
Erträge`200`Die Registrierung ist immer sowohl auf die authentifizierten als auch auf die
`user_uuid` und die IAM `project_id`.

```json
{
  "transport": "fcm",
  "platform": "ios",
  "registration_token": "<FCM registration token>",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  }
}
```

`encryption`ist ein RESTAlchemy-Modell.`HPKE`,
mit Basismodus mit X25519, HKDF-SHA256 und AES-256-GCM muss `public_key`
Die kanonische unpolsterte Base64url-Codierung von genau 32 Bytes.
API Version, die Antwort Spiegel `registration_token` und `public_key` von
Die derzeit unterstützten Plattformen sind `android` und `ios`.

```json
{
  "uuid": "7c1af344-95e1-487e-8b51-d1af0370cdb5",
  "transport": "fcm",
  "platform": "ios",
  "encryption": {
    "kind": "HPKE",
    "algorithm": "HPKE-v1-BASE-X25519-HKDF-SHA256-AES-256-GCM",
    "key_uuid": "248305f4-ecdf-4206-8e93-95f830ea8ad6",
    "public_key": "<unpadded base64url X25519 public key>"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "registration_token": "<FCM registration token>",
  "created_at": "2026-07-26T05:30:00Z",
  "updated_at": "2026-07-26T05:40:00Z"
}
```

`DELETE` gibt `204` zurück, wenn die Eigentumsregistrierung entfernt wird und wenn die
Dieser Vertrag verwaltet nur Registrierungen;
Die Nutzlastverschlüsselung und -übergabe sind außerhalb dieser API Änderung.

## Verzeichnisse {#folders}

`POST /api/workspace/v1/messenger/folders/`Schreibt an:`m_folders`- Sie lesen .`m_folders_view`.
Die Antworten werden `project_id` und `user_uuid` versteckt.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Ordnerkennung. |
| `title` | String, 1,64 | - Ja, das ist es. | - Nein. | Titel der Akte. |
| `background_color_value` | Zahl `0..2^32-1` oder `null` | - Nein. | - Nein. | ARGB Farbwert. |
| `unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Aggregierte aktive Nichtgelesenzahl. |
| `system_type` | `all`, `created` oder `null` | - Nein. | - Ja, das ist es. | Systemordnertyp; Standard für `created`. |
| `folder_items` | Reihenfolge | - Nein. | - Ja, das ist es. | Eingebettet Ordner-Elemente aus der Ansicht. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Erstellen einer Anfrage:

```json
{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Beispiel:

```http
POST /api/workspace/v1/messenger/folders/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Inbox",
  "background_color_value": 4280391411
}
```

Beispiel für die Antwort:

```json
{
  "uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "title": "Inbox",
  "background_color_value": 4280391411,
  "unread_count": 3,
  "system_type": "created",
  "folder_items": [
    {
      "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
      "project_id": "22222222-2222-2222-2222-222222222222",
      "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
      "user_uuid": "11111111-1111-1111-1111-111111111111",
      "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
      "chat_type": "stream",
      "order_index": 10,
      "pinned_at": null,
      "unread_count": 3,
      "active_unread_count": 3,
      "passive_unread_count": 0,
      "created_at": "2026-06-22T09:30:00Z",
      "updated_at": "2026-06-22T09:30:00Z"
    }
  ],
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Beispiel für das Aktualisieren:

```http
PUT /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "title": "Archive",
  "background_color_value": 4289352960
}
```

Beispiel löschen:

```http
DELETE /api/workspace/v1/messenger/folders/50ecadd0-9823-4d97-b54c-806cc672c210
Authorization: Bearer <access_token>
```

Nebenwirkungen in Echtzeit:

| Betrieb | payload.kind | object_type | Nutzlast |
| --- | --- | --- | --- |
| Ordner erstellen | `folder.created` | `folder` | Vollständige Ordner-Schnappschuss. |
| Aktualisieren Ordner | `folder.updated` | `folder` | Vollständige Ordner-Schnappschuss. |
| Verwenden Sie die Liste | `folder.deleted` | `folder` | Nur `folder.uuid`. |

## Ordner-Elemente {#folder-items}

`POST /api/workspace/v1/messenger/folder_items/` schreibt zu `m_folder_items`.
`m_folder_items_created_view`.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Identifizierung des Ordnergegenstands. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `folder_uuid` | UUID | - Ja, das ist es. | - Nein. | Ordner UUID. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | IAM Benutzerbereich. |
| `stream_uuid` | UUID | - Ja, das ist es. | - Nein. | Strom UUID. |
| `chat_type` | `stream`, `group`, `private` | - Ja, das ist es. | - Nein. | - Ich bin ein Chat-Typ. |
| `order_index` | Zahl oder `null` | - Nein. | - Nein. | Manueller Sortierindex. |
| `pinned_at` | Datum/Zeit oder `null` | - Nein. | Aktionsmanagement | Zeitstempel. |
| `unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Roh-nicht gelesener Zählwert für diesen Stream und Benutzer. |
| `active_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Nichtgelesenen Nachrichten, die unter den Notifizierungsmodi effektiver Strömung/topic zugelassen sind. |
| `passive_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Nicht gelesenen Nachrichten aus dem gedämpften Benachrichtigungsverkehr. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Erstellen einer Anfrage:

```json
{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Erstellen Sie ein Beispiel:

```http
POST /api/workspace/v1/messenger/folder_items/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10
}
```

Beispiel für die Antwort:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": null,
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:30:00Z"
}
```

Pin und unpin geben die gleiche Ordner-Element-Form zurück. `pin` setzt `pinned_at` auf die
die aktuelle Zeit UTC; `unpin` setzt sie auf `null`.

Beispiel für einen Pin:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/pin/invoke
Authorization: Bearer <access_token>
```

Beispiel für die Pin-Antwort:

```json
{
  "uuid": "9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "folder_uuid": "50ecadd0-9823-4d97-b54c-806cc672c210",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "chat_type": "stream",
  "order_index": 10,
  "pinned_at": "2026-06-22T09:31:00Z",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "created_at": "2026-06-22T09:30:00Z",
  "updated_at": "2026-06-22T09:31:00Z"
}
```

Beispiel für das Entpinnen:

```http
POST /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50/actions/unpin/invoke
Authorization: Bearer <access_token>
```

Beispiel löschen:

```http
DELETE /api/workspace/v1/messenger/folder_items/9f41b1a7-77f9-4c12-bdc6-d3cebc5dbf50
Authorization: Bearer <access_token>
```

Nebenwirkungen in Echtzeit:

| Betrieb | payload.kind | object_type | Nutzlast |
| --- | --- | --- | --- |
| Streaming zum Ordner hinzufügen | `folder.updated` | `folder` | Vollständige Momentaufnahme des übergeordneten Ordners mit `folder_items`. |
| Pin-Stream im Ordner | `folder.updated` | `folder` | Vollständige Momentaufnahme des übergeordneten Ordners mit aktualisiertem `pinned_at`. |
| Unpin-Stream im Ordner | `folder.updated` | `folder` | Vollständige Momentaufnahme des übergeordneten Ordners mit `pinned_at: null`. |
| Streaming aus dem Ordner entfernen | `folder_item.deleted` | `folder_item` | Nur `folder_item.uuid`. |

## Ströme

`POST /api/workspace/v1/messenger/streams/` verbindet den kanonischen Stream,
PostgreSQLEs schafft eine
Standardthema mit dem Namen `General Topic` und speichert seine UUID als
`default_topic_uuid`.
Die Referenz ist null und wird `null`, wenn das aktuelle Standardthema
REST Ressourcenantworten folgen dem Standard RestAlchemy JSON Packer
und weglassen nullfähige Felder, deren Wert `null` ist, so müssen die Kunden auch eine
fehlt `default_topic_uuid` als `null`. Dauerhafte `stream.updated`-Ereignisse sind voll
Schnappschüsse und `default_topic_uuid: null` explizit behalten.

Wenn `direct_user_uuid` angegeben ist, erstellt das Backend einen normalen Stream mit
die gleichen Bindungen, Rollen, Themen, Ereignisse und Datei ACL Regeln wie alle anderen
Die einzigen zusätzlichen Invarianten sind `private: true`, eine deterministische
Projektumfangströmung UUID für das nicht geordnete Identitätspaar und `owner`
Ein normaler direkter Chat hat zwei
Ein Selbstchat verwendet das wiederholte Paar `(user, user)`,
enthält genau eine Bindung für den aktuellen Benutzer und gibt den aktuellen Benutzer zurück
UUID in `direct_user_uuid` Wiederholung oder gleichzeitige Zusendung derselben Anfrage
für ein Paar gibt den vorhandenen Stream zurück.
Quelle oder direkte Identitätsfelder gibt HTTP `400` anstelle von
Sie ignorieren die Identität.

Unterstützte Quell-Nutzlasten:

```json
{
  "source_name": "native",
  "source": {
    "kind": "native"
  }
}
```

```json
{
  "source_name": "zulip",
  "source": {
    "kind": "zulip",
    "stream_id": 123,
    "server_url": "https://zulip.example.com",
    "topic_name": null,
    "message_id": null
  }
}
```

Die `zulip` Nutzlastform ist Provider-Herkunft. Eine eingeschriebene Zulip Laufzeit
wird durch den privaten Anbieter HTTP API ausgefüllt; der Browservertrag versteckt
Roh-Providerprotokoll-Identifikatoren, Anmeldeinformationen und Synchronisationszustand.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Streaming-Identifikator. |
| `name` | String, max. 255 | - Ja, das ist es. | - Nein. | - Der Name des Stroms. |
| `description` | String, max. 255 | - Nein. | - Nein. | Strömungsbeschreibung; Standard für eine leere Zeichenfolge. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `owner` | UUID | - Nein. | - Ja, das ist es. | Eigentümer aus der Benutzer-Stream-Ansicht. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Der aktuelle Benutzer in der Benutzer-Stream-Ansicht. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | - Nein. | - Ja, das ist es. | Die Rolle des aktuellen Benutzers. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | - Nein. | von Benutzern gesteuerte Aktionen | Der aktuelle Nutzer ist in einem Stream-Benachrichtigungsmodus; Standardwert ist `all_messages`. |
| `unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die ungelesenen Daten des aktuellen Benutzers. |
| `active_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die Anzahl der nicht gelesenen Daten des aktuellen Benutzers, die unter den Notifizierungsmodi effektiver Streams /topic zulässig ist. |
| `passive_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die verbleibende nicht gelesenen Daten des aktuellen Benutzers aus dem gedämpften Benachrichtigungsverkehr. |
| `source_name` | `native`, `zulip` | - Nein. | - Nein. | Quelle-Name; Standard für `native`. |
| `source` | Gegenstand | - Nein. | - Nein. | Quelle Nutzlast; Standard für `{"kind": "native"}`. |
| `invite_only` | Boolean | - Nein. | - Nein. | Streaming-Flagge nur auf Einladung. |
| `announce` | Boolean | - Nein. | - Nein. | Ankündigung der Streaming-Flagge. |
| `direct_user_uuid` | UUID | - Nein. | - Nein. | Gleich dem aktuellen Benutzer UUID nur für einen Selbstchat. |
| `private` | Boolean | - Nein. | - Ja, das ist es. | Privater Stromflagge. |
| `is_archived` | Boolean | - Nein. | Aktionsmanagement | Archivflagge. |
| `color` | - Eine ganze Zahl .`0..0xFFFFFF` | - Nein. | - Nein. | Strömungsfarbe; wird zufällig erzeugt, wenn sie weggelassen wird oder `null`. |
| `last_message_uuid` | UUID oder `null` | - Nein. | - Ja, das ist es. | Letzte Nachricht im Stream oder `null`, wenn es leer ist. |
| `default_topic_uuid` | UUID oder `null` | - Nein. | - Ja, das ist es. | Aktuelles Standardthema UUID oder `null`, wenn kein Standardkonfiguration vorhanden ist. |
| `provider` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Anbieter-Badge für von Anbietern unterstützte Streams; `null` für native Streams. |
| `delivery` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Aktuelle Anbieter-Befehls-Versandprojektion. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Erstellen einer Anfrage:

```json
{
  "name": "Engineering",
  "description": "Engineering workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false
}
```

Erstellen Sie eine direkte Chat-Anfrage:

```json
{
  "name": "Direct",
  "description": "Private workspace",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "33333333-3333-3333-3333-333333333333"
}
```

Selbst-Chat-Erstellungsanfrage:

```json
{
  "name": "Personal notes",
  "description": "",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "11111111-1111-1111-1111-111111111111"
}
```

Für einen Selbstchat muss `direct_user_uuid` dem aktuellen IAM Benutzer UUID entsprechen,
einschließlich des SymbolsubjektsUUIDSelbst-Chats sind nur nativ.
die Standardstrommenge mit `private: true`, einem Strombenutzer `owner`
Bindung und derselbe aktuelle Benutzer UUID in `direct_user_uuid`; kein separater Chat
Sie können die Daten des Types oder der Selbst-Chat-Flagge anzeigen.
`private && direct_user_uuid == current_user_uuid` die stabile Kundenseite
Identitätsprüfung bei gleichzeitiger Erhaltung der normalen privaten Gruppenströme, deren
`direct_user_uuid` bleibt `null`.

Die direkte Mitgliedschaft ist unveränderlich.
Das Identitätspaar: eine Bindung für einen Selbstchat und zwei für einen gewöhnlichen direkten
Chat. Hinzufügen oder Entfernen von Teilnehmern und Aktualisierung einer verbindlichen Rollenrückgabe HTTP
`400`. Das Löschen eines Selbst-Chat-Streams gibt auch HTTP `400` zurück, also Nachrichtenverlauf
Die Definition der deterministischen Identität kann nicht durch Löschen und Neugestaltung ersetzt werden.
`source_name` muss mit `source.kind` übereinstimmen, wenn ein Stream erstellt wird.
Für einen direkten Chat, `direct_user_uuid`,
`private`, und die inneren `private_index` sind ebenfalls unveränderlich; Versuche,
Änderungen eines dieser Identitätsfelder geben HTTP `400` zurück.

Stream-Benachrichtigungsmodus-Anfrage:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/notifications/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "notification_mode": "mentions_only"
}
```

Native Stream-Mutationen aktualisieren den kanonischen PostgreSQL-Zustand und ihre Echtzeit
Die Null `provider` und die Null `provider` werden in der Anforderungstransaktion
`delivery` Felder beschreiben einen externen Projektions- und Betriebszustand; beide sind
`null` für native Ströme.

Stream-Leseaktion:

```http
POST /api/workspace/v1/messenger/streams/75309057-419c-4b12-a7c1-3932429ec4a6/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` markiert alle nicht gelesenen Nachrichten im Stream als für den aktuellen Benutzer gelesen und
gibt die aktualisierte Streamansicht zurück.

Nebenwirkungen in Echtzeit:

| Betrieb | payload.kind | object_type | Nutzlast |
| --- | --- | --- | --- |
| Erstellen von Streams | `stream.created` | `stream` | Vollständige Nutzer-Stream-Snapshot. |
| Erstellen von Streams | `folder.updated` | `folder` | Aktualisierte `All chats` und `Channels`/`Personal`-Systemordner-Snapshots. |
| Aktualisierungsstrom | `stream.updated` | `stream` | Vollständige Nutzer-Stream-Snapshot für jeden Nutzer. |
| Archivieren oder nicht archivieren | `stream.updated` | `stream` | Vollständige Nutzer-Stream-Snapshot für jeden Nutzer. |
| Änderung des Benachrichtigungsmodus des Stroms | `stream.updated` | `stream` | Vollständige Nutzer-Stream-Snapshot nur für den aktuellen Nutzer. |
| Lesen von Stream-Nachrichten | `stream.read` | `stream` | Vollständige Benutzer-Stream-Snapshot, die durch die Aktion zurückgegeben wird. |
| Lesen von Stream-Nachrichten | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Aktualisierte nicht gelesenen Schnappschüsse für den aktuellen Benutzer. |
| Streaming löschen | `stream.deleted` | `stream` | Nur gelöschter Stream `uuid`, an jeden Stream-Benutzer gesendet. |
| Streaming löschen | `folder.updated` | `folder` | Aktualisierte Schnappschüsse des System/custom-Ordners der betroffenen Benutzer, nachdem der Stream entfernt wurde. |
| Strombindung hinzufügen | `stream.created` | `stream` | Der vollständige Benutzer-Stream-Snapshot des Benutzers wurde hinzugefügt. |
| Strombindungen hinzufügen | `stream_bindings.created` | `stream_binding` | Neue Stream-Bindungsschnappschüsse für bestehende Stream-Teilnehmer. |
| Strombindung hinzufügen | `folder.updated` | `folder` | Aktualisiert wurden die Schnappschüsse des Systemordners `All chats` und `Channels`/`Personal` des Benutzers. |
| Streambindung löschen | `stream.deleted` | `stream` | Nur der Stream `uuid`, der an den entfernten Benutzer gesendet wird. |
| Streambindung löschen | `stream_binding.deleted` | `stream_binding` | Entfernte Bindungen `uuid`, `stream_uuid` und `user_uuid` an alle verbleibenden Stromteilnehmer gesendet. |
| Streambindung löschen | `folder.updated` | `folder` | Aktualisierte installierte Schnappschüsse des System-Dateiordners des Benutzers/custom nach Entfernung des Zugriffs. |

Für direkte private Streams wird für jeden `stream.created` Ereignis geschrieben
Die Erstellung des Streams schreibt auch `folder.updated` Ereignisse für jede
Teilnehmerordner `All chats` und für `Personal`, wenn der Stream privat ist,
oder `Channels`, wenn es nicht privat ist.

## Strömungsbindungen

Streaming-Bindungen sind kanonische PostgreSQL Chat-Mitgliedschaftsdatensätze.
Sie werden durch
`POST /api/workspace/v1/messenger/streams/{stream_uuid}/actions/add_users/invoke`, wo
die Anforderungsgruppen der Körper von Benutzern nach Rollen hinzugefügt. `who_uuid` wird immer überschrieben.
mit dem aktuellen IAM Benutzer UUID.
Wenn eine neue Bindung erstellt wird, erhält der zusätzliche Benutzer eine `stream.created`
Ereignis für den neu sichtbaren Stream und `folder.updated` Ereignisse für `All chats`
und entweder `Personal` oder `Channels`, je nach der Strömungsprivatsphäre.
Die Teilnehmer des Streams erhalten ein Ereignis `stream_bindings.created`, das die
Jede Nachricht, die vor der Übermittlung der Daten verpflichtet wurde, wird in einem neuen Bindungs-Snapshot für die gesamte zusätzliche Charge angezeigt.
Die Verbindung wird für das neue Mitglied mit `read=true` erstellt, so dass beide
Die ungelesenen Zähler für den Stream und das Thema beginnen bei Null. Nachrichten, die nach
Die Verknüpfung ist nicht gelesen, bis der neue Mitglied sie gelesen hat.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Bindende Kennung. |
| `project_id` | UUID | - Ja, das ist es. | - Nein. | Projektumfang. |
| `stream_uuid` | UUID | - Ja, das ist es. | - Nein. | Strom UUID. |
| `user_uuid` | UUID | - Ja, das ist es. | - Nein. | Benutzer, der Zugriff erhält. |
| `who_uuid` | UUID | - Nein. | - Ja, das ist es. | Benutzer, der die Aktion ausgeführt hat. |
| `role` | `guest`, `member`, `moderator`, `administrator`, `owner` | - Nein. | - Nein. | Rolle; Standardwert ist `member`. |
| `notification_mode` | `mentions_only`, `muted`, `all_messages` | - Nein. | - Nein. | Benutzer-Nachrichten-Stream-Modus; Standard für `all_messages`. |
| `notification_updated_at` | Zeit und Datum | - Nein. | - Nein. | Letzter Schreib-Gewinn Zeitstempel mit`notification_mode`Die Anmeldung wird auf die aktuelle Serverzeit gesetzt.RESTund Echtzeit-Bindungsschnappschüsse. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Benutzeranfrage hinzufügen:

```json
{
  "member": [
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444"
  ],
  "owner": [
    "55555555-5555-5555-5555-555555555555"
  ]
}
```

Das Löschen einer Bindung entfernt den Zugriff des Benutzers auf den Stream.
empfängt `stream.deleted` und dann `folder.updated` für das betroffene System und
Jeder verbleibende Stream-Teilnehmer erhält
`stream_binding.deleted` mit entferntem Bindungsmaterial `uuid`, `stream_uuid` und
`user_uuid`. Für einen vom Anbieter unterstützten Stream können auch Bindungen hinzugefügt und gelöscht werden
Die Kommission hat die Kommission aufgefordert, die`membership.add`Und ...`membership.remove`
Die Provider-Brücke löst die abgegrenzte Provider-Identität und
Abonnent oder Abonnent; native Streams führen keinen Provider-Betrieb aus.

## Streaming-Themen

`POST /api/workspace/v1/messenger/stream_topics/` verbindet das kanonische Thema,
Die Daten werden in PostgreSQL erfasst.
dem aktuellen IAM Benutzer durch die aktuelle Strommitgliedschaft.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Identifizierung des Themas. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `name` | String, maximal 128 | - Ja, das ist es. | - Nein. | Der Name des Themas. |
| `stream_uuid` | UUID | - Ja, das ist es. | - Nein. | Strom UUID. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Der aktuelle Benutzer in der Themenansicht. |
| `color` | - Eine ganze Zahl .`0..0xFFFFFF` | - Nein. | - Nein. | Themafarbe; wird zufällig erzeugt, wenn sie weggelassen wird oder `null`. |
| `last_message_uuid` | UUID oder `null` | - Nein. | - Ja, das ist es. | Letzte Nachricht im Thema oder `null`, wenn es leer ist. |
| `unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die ungelesenen Daten des aktuellen Benutzers für das Thema. |
| `active_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die nicht gelesenen Erwähnungen des aktuellen Benutzers für `unmute`, alle nicht gelesenen für `follow` oder die vererbte aktive Anzahl für `default`. |
| `passive_unread_count` | ganzzahl | - Nein. | - Ja, das ist es. | Die Anzahl der nicht gelesenen Daten des aktuellen Benutzers nach Anwendung des effektiven Benachrichtigungsmodus. |
| `is_default` | Boolean | - Nein. | - Ja, das ist es. | Ob dieses Thema UUID gleich dem `default_topic_uuid` des Stroms ist. |
| `is_done` | Boolean | - Nein. | Aktionsmanagement | Der aktuelle Benutzer hat die Flagge abgelegt. |
| `notification_mode` | `mute`, `default`, `unmute`, `follow` | - Nein. | von Benutzern gesteuerte Aktionen | Der aktuelle Benutzer hat den Thema-Benachrichtigungsmodus; Standardwert ist `default`. |
| `summary` | String, max. 4096, oder `null` | - Nein. | - Ja, das ist es. | Die letzte LLM-generierte Zusammenfassung, geschrieben vom Server-Seiten-Summary-Agent. |
| `summary_last_message_uuid` | UUID oder `null` | - Nein. | - Ja, das ist es. | Letzte Themenmeldung, die tatsächlich in `summary` enthalten ist; vom Serverseiten-Summary-Agent geschrieben, und `null` ist für ein leeres Thema gültig. |
| `summary_has_new_messages` | Boolean oder `null` | - Nein. | - Ja, das ist es. | `null` ohne Zusammenfassung; ansonsten, ob sich die aktuelle letzte Nachricht von `summary_last_message_uuid` unterscheidet. |
| `summary_enabled` | Boolean | - Nein. | Aktionsmanagement | Ob der Server-Worker dieses Thema aktualisieren darf; Standard für `true`. |
| `summary_system_prompt` | String, max 16384, oder `null` | - Nein. | Aktionsmanagement | LLM Systemaufforderung für Themen; `null` wählt die Anwendungsstandard. |
| `summary_reasoning_effort` | `off`, `minimal`, `low`, `medium`, `high` oder `null` | - Nein. | Aktionsmanagement | Per-summary-Reasoning-Selektion; `off` deaktiviert ausdrücklich die Reasoning, während `null` die Provider-Option weglässt. Nur verwendet, wenn der ausgewählte Endpunkt die Reasoning-Unterstützung deklariert. |
| `source_name` | `native`, `zulip` | - Nein. | - Nein. | Thema-Quellname; bei Auslassen wird standardmäßig `native` angegeben. |
| `source` | Gegenstand | - Nein. | - Nein. | Zielquelle Nutzlast. |
| `provider` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Anbieter-Abzeichen für von Anbietern unterstützte Themen; `null` für native Themen. |
| `delivery` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Aktuelle Anbieter-Befehls-Versandprojektion. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Wenn `summary_system_prompt` `null` ist, fordert die Anwendung standardmäßig eine
eine kurze Zusammenfassung, die Entscheidungen, Eigentümer, ungelöste Fragen und
Die wichtigsten Einschränkungen sind in der in dem Thema verwendeten Hauptsprache geschrieben.

Erstellen einer Anfrage:

```json
{
  "name": "Releases",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
}
```

`PUT /api/workspace/v1/messenger/stream_topics/{topic_uuid}` erfordert einen Körper mit `name`.
überprüft, ob der aktuelle Benutzer vor dem Thema-Stream eine Bindung hat
Native Änderungen aktualisieren den Status von canonical PostgreSQL und ihre
Die Herkunft bleibt durch eine Umbenennung unverändert.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/toggle_done/invoke` Flips
`is_done` für alle Themabenutzer und gibt die aktualisierte Themaansicht des aktuellen Benutzers zurück.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_default/invoke` setzt die
Thema als Standard-Stream und gibt das aktualisierte Thema des aktuellen Benutzers zurück
Die Operation ist idempotent. Eine veränderte Standardfunktion gibt `stream.updated` aus
für jeden Stream-Nutzer und `topic.updated` für den vorherigen und den neuen Standard
Die Kommission hat

Die Zusammenfassungen der Themen werden nur vom Server-Side-Summary-Agent über einen
interne Helfer; es gibt keine öffentliche REST Aktion für das Schreiben von `summary` oder
`summary_last_message_uuid`. Der Helfer speichert beide Felder atomar,
bestätigt, dass eine nicht null-Grenze eine Nachricht im Thema identifiziert, lehnt ab
eine ältere Grenze, wenn eine neuere bereits gespeichert ist, und emittiert
`topic.updated` Schnappschüsse für die Stream-Teilnehmer.
enthält auch einen privaten Server-Seiten-Journal-Eintrag mit der Zusammenfassung,
GrenzeUUIDDie Daten werden in einem System mit einem Datenverzeichnis erfasst, das die Daten über die Daten erfasst.
Die Daten werden in der Datenbank aufgenommen.
die letzte frühere Eintragung wiederhergestellt wird (oder die Zusammenfassung gelöscht wird),
Die Arbeitgeber werden in der Regel in der Lage, die
Dieselbe Transaktion.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/set_summary_prompt/invoke`
aktualisiert die themenspezifische Zusammenfassungskonfiguration:

```json
{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "medium",
  "summary_enabled": true
}
```

Die Einstellung von `summary_system_prompt` auf `null` stellt die Standardeinstellung der Anwendung wieder her.
Jedes Feld ist optional, der Antrag muss jedoch mindestens ein Feld enthalten.
Wenn ein Feld weggelassen wird, wird sein aktueller Wert erhalten.
`summary_reasoning_effort` als `null` löscht die Begründungsanfrage.
Die Anforderung ist nicht für die Endpunktkonfiguration bestimmt.
an `off` sendet den mit OpenAI kompatiblen Anbieterwert `none` explizit
Die Einführung von Modell-Daten wird durch die Angabe der Datenbasis und die Angabe der Datenbasis und der Datenbank erleichtert.
`summary_enabled` bis `false` annulliert laufende Arbeiten zu diesem Thema und verhindert
neue Ansprüche bei gleichzeitiger Bewahrung der aktuellen Zusammenfassung; Rücksetzung auf `true`
Der Arbeiter kann den veralteten Inhalt auffrischen.
Nur Stream-Besitzer und Administratoren können diese Konfiguration aktualisieren; andere Rollen,
einschließlich Moderatoren, empfangen `403 Forbidden`.

### Zusammenfassung der Themen: Client-Arbeitsfluss

Der Client liest die Zusammenfassung als Teil des normalen Themen-Snapshots:

```http
GET /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047
Authorization: Bearer <access_token>
```

```json
{
  "uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "last_message_uuid": "b5ff6f76-bcfe-4fb9-9c28-e0cb790d2e52",
  "summary": "The team approved the release scope; two follow-ups remain open.",
  "summary_last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "summary_has_new_messages": true,
  "summary_enabled": true,
  "summary_system_prompt": "Summarize decisions and open questions.",
  "summary_reasoning_effort": "medium"
}
```

Die Benutzeroberfläche zeigt `summary` an und kann sie als veraltet markieren, während
`summary_has_new_messages` ist `true`. Es sendet keine Nachrichten an eine LLM oder
Ein `topic.updated` Ereignis enthält das vollständig aktualisierte Thema
Schnappschuss, so dass verbundene Clients ihren lokalen Thema-Zustand ersetzen, ohne
Umfragen oder ein spezieller Zusammenfassungsendpunkt.

Ein Eigentümer oder Administrator kann die vom Server-Agent verwendete Aufforderung ändern:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_system_prompt": "Summarize decisions, owners, and unresolved risks.",
  "summary_reasoning_effort": "high",
  "summary_enabled": true
}
```

Die Aktion gibt den vollständigen Themen-Snapshot zurück und sendet `topic.updated` an die
Die Einstellung von `summary_system_prompt` auf `null` wählt die
Anwendungsstandard wieder. Planung oder NeustartLLMArbeit bleibt ein
die Verantwortung des Agenten auf der Serverseite.

Das automatische Erneuerungsvorgang eines Themas pausieren, ohne das vorhandene Thema zu entfernen
Zusammenfassung: Dieselbe Aktion darf nur das Thema Gate senden:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/set_summary_prompt/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "summary_enabled": false
}
```

### Verwaltung der Themenübersicht {#topic-summary-administration}

Die Arbeitgeber läuft die Datenbank-Synthese.
nur wenn sowohl `global_enabled` als auch `project_enabled` des aktuellen Projekts
Eine Einstellungsaktualisierung liefert beide Werte:

```http
PUT /api/workspace/v1/messenger/topic_summary_settings/12345678-1234-4234-8234-123456789abc
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "global_enabled": true,
  "project_enabled": true
}
```

Der Pfad UUID muss dem IAM-Projekt im Anforderungskontext entsprechen.
für Projektbenutzer verfügbar; Aktualisierungen sind erforderlich
`workspace.topic_summary_settings.manage`.

LLM Endpunkte sind global und nicht pro Projekt oder Stream.
`workspace.topic_summary_endpoint.manage`:

```http
POST /api/workspace/v1/messenger/topic_summary_endpoints/
Authorization: Bearer <access_token>
Content-Type: application/json

{
  "uuid": "e4ad6d80-6bc7-4a91-864c-8e97319a82bd",
  "name": "primary-summary-model",
  "base_url": "https://llm.example.com/v1",
  "model": "summary-model",
  "api_key": "<write-only credential>",
  "enabled": true,
  "priority": 10,
  "supports_vision": true,
  "supports_reasoning": true,
  "temperature": 0.2,
  "max_output_tokens": 512,
  "top_p": 1.0,
  "presence_penalty": 0.0,
  "frequency_penalty": 0.0
}
```

Alle Endpunkte implementieren OpenAI-kompatibel `POST {base_url}/chat/completions`;
Es gibt keine anbieterspezifischen Niederlassungen.`priority`Werte werden zuerst ausgeführt und
UUID ist der deterministische Tie-Breaker.
Eine erneut ausprobierbare Netzwerk-, Rate-Limit- oder Serverstörung setzt die
Die Daten werden in der Regel in einem System mit einem
Die Datenbank zeigt begrenzte Gesundheitsdaten (`last_success_at`, `last_failure_at`,
`failure_count` und `last_error_code`) aber niemals ein aktives Anspruchszeichen ausblendet.

`api_key` wird nur bei Erstellung oder Ersatz von Anmeldeinformationen akzeptiert.
mit einem Bereitstellungsgeheimnis vor der Speicherung und wird nie durch create, list zurückgegeben,
Workspace Ereignisse geschrieben, in Themen-Snapshots kopiert oder aktualisiert werden, oder
Die Daten werden in den Arbeitsprotokollen aufgenommen.
Die Datenbank hat keine Daten über die
Überarbeitung, `ETag` oder `If-Match` Vertrag.

Die Generations-Einstellungen haben folgende Standardeinstellungen und akzeptierte Bereiche:

| Feld | Standard | Reichweite |
| --- | --- | --- |
| `temperature` | `0.2` | `0.0..2.0` |
| `max_output_tokens` | `512` | `1..32768` |
| `top_p` | `1.0` | `0.0..1.0` |
| `presence_penalty` | `0.0` | `-2.0..2.0` |
| `frequency_penalty` | `0.0` | `-2.0..2.0` |

Der Messenger-Arbeiter fordert ein veraltetes Thema und höchstens 100 neue Nachrichten in einem
Schritt, schnappt sich die Grenze und effektive Aufforderung, verpflichtet den Anspruch, führt
die LLM Anfrage außerhalb jeder Datenbanktransaktion, und das Ergebnis bleibt in
eine neue Transaktion über den bestehenden internen Zusammenfassungshelfer.
Wiederholungsverzögerungen, Endpunktleasing und Anspruchsverfall werden gespeichert, so dass Wiederholungsversuche erhalten bleiben
begrenzt und beobachtbar.

Die langen Argumente sind eine normale Reaktion des Anbieters, nicht ein Versagen des Arbeitnehmers.
Die Verbindungsaufzeit beträgt 30 Sekunden, während die Antwortsaufzeit 25 Minuten beträgt, so dass ein
Das Modell kann 20 Minuten lang reden, ohne die Klientendatum zu überschreiten.
Die Endpunkt-Lease beträgt 30 Minuten und die Thema-Job-Lease beträgt 90 Minuten.
Der Arbeitnehmer setzt außerdem einen Endpunkt-Leasingvertrag von mindestens der Antwortzeit plus
60 Sekunden und eine Thema-Leasing von mindestens drei solcher Anforderungsfenster, so dass eine andere
Der Arbeiter kann bei langsamer Reaktion oder sofortiger Ausfallüberführung keine Live-Arbeit zurückfordern.

Wenn die begrenzte Nachricht Charge enthält ein Bild Workspace und alle aktiviert
Wenn ein Zielpunkt für die Sicht vorhanden ist, kann nur ein Zielpunkt für die Sicht ausgewählt werden.
Endpunkt ist beschäftigt, der Job wartet; er fällt nicht auf einen Free-Text-Endpunkt zurück.
Eine Text-nur-Zusammenfassung ist für eine bildhaltige Charge nur zulässig, wenn keine
Bild wird nur in der Benutzernachricht codiert
Die Anfrage wird immer nur mit Text beantwortet.

`POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/notifications/invoke` setzt die
Thema-Benachrichtigungsmodus des aktuellen Benutzers:

```json
{
  "notification_mode": "follow"
}
```

Erlaubte Themenbenachrichtigungsmodi sind `mute`, `default` und `follow`. `unmute`
ist nur zulässig, wenn der Stream-Benachrichtigungsmodus des aktuellen Benutzers `muted` ist.
Die nicht gelesenen Einstellungen werden anhand der aktuellen Einstellungen ausgewertet.
Der Modus klassifiziert sofort die vorhandenen ungelesenen Nachrichten neu.`follow`macht jede
Thema ungelesen aktiv, `unmute` macht nur direkte Erwähnungen des aktuellen Benutzers
aktiv, macht `mute` jedes ungeliesene Thema passiv, und `default` erbt die
Ein Stream in `mentions_only` stellt ebenfalls nur direkte Erwähnungen vor
in `active_unread_count`; alle verbleibenden unverlesenen Rohnachrichten bleiben in
`passive_unread_count`.

Für Zulip-Streams und -Themen, die von Anbietern unterstützt werden, werden Benachrichtigungsaktionen in einer Warteschlange angezeigt
Die Anbieteraktualisierungen werden auf die
Workspace mit einem Zeitstempel, so dass ein älteres Update nicht ein
Ein neueres.

Thema-Leseaktion:

```http
POST /api/workspace/v1/messenger/stream_topics/4ec0b996-b778-45f8-8ef4-ef863be0c047/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` markiert alle ungelesenen Nachrichten im Thema als für den aktuellen Benutzer gelesen und
gibt die aktualisierte Themenansicht zurück.

Nebenwirkungen in Echtzeit:

| Betrieb | payload.kind | object_type | Nutzlast |
| --- | --- | --- | --- |
| Erstellen Sie ein Thema | `topic.created` | `topic` | Vollständige Benutzer-Themen-Snapshot für jeden Stream-Benutzer. |
| Thema umbenennen | `topic.updated` | `topic` | Vollständige Benutzer-Themen-Snapshot für jeden Stream-Benutzer. |
| Schalten fertig | `topic.updated` | `topic` | Vollständige Benutzer-Themen-Snapshot für jeden Stream-Benutzer. |
| Stell das Standardthema fest | `stream.updated`, `topic.updated` | `stream`, `topic` | Aktualisierte Stream-Snapshots und vorherige /new Standardthemen-Snapshots für jeden Stream-Benutzer. |
| Server-Zusammenfassung aktualisiert | `topic.updated` | `topic` | Vollständige Benutzer-Themen-Snapshot für jeden Stream-Benutzer. |
| Setze die Zusammenfassung | `topic.updated` | `topic` | Vollständige Benutzer-Themen-Snapshot für jeden Stream-Benutzer. |
| Änderung des Benachrichtigungsmodus | `topic.updated`, `stream.updated` | `topic`, `stream` | Umklassifizierte Themen und ungelesenen Snapshots für den aktuellen Benutzer streamen. |
| Lesen Sie die Nachrichten | `topic.read` | `topic` | Vollständige Benutzer-Themen-Snapshot, die durch die Aktion zurückgegeben wird. |
| Lesen Sie die Nachrichten | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Aktualisierte nicht gelesenen Schnappschüsse für den aktuellen Benutzer. |
| Thema löschen | `topic.deleted` | `topic` | Thema gelöscht .`uuid`Und ...`stream_uuid`, an jeden Stream-Benutzer gesendet.`stream.updated`Mit`default_topic_uuid: null`. |

## Nachrichten {#messages}

`POST /api/workspace/v1/messenger/messages/` validiert den aktuellen PostgreSQL-Stream
Mitglieder und verpflichtet sich die kanonischen UTF-8 Markdown-Nachricht, Flaggen, eine gemeinsame
Die Daten werden in einem "Snapshot" von der Zielgruppe des Empfängers und in einem "Compact Message" /topic/stream aufgenommen.
Es erstellt nicht eine kanonische Ereigniszeile pro Empfänger.
Die Lesungen bleiben dem aktuellen IAM Benutzer vorbehalten und behalten die vorhandene Antwort.

Die einzige unterstützte Nachrichtenlast in v1 ist Markdown:

```json
{
  "kind": "markdown",
  "content": "Hello, workspace"
}
```

Workspace Entitätsverweise innerhalb von Markdown-Inhalten verwenden einen regelmäßigen Markdown-Link
Die URL Teil ist ein Workspace URN:

| Einheit | Verringerung der Marke | Anmerkungen |
| --- | --- | --- |
| Benutzernennung | `[Jane Doe](urn:user:<user-uuid>)` | Als Benutzertag behandelt/mention. |
| Nachrichtenschluss | `[See message](urn:message:<message-uuid>)` | Zeigt auf eine Workspace-Nachricht hin. |
| Streaming-Verbindung | `[general](urn:stream:<stream-uuid>)` | Zeigt auf einen Workspace -Stream. |
| Themaverbindung | `[deploys](urn:topic:<topic-uuid>)` | Zeigt auf ein Workspace Thema hin. |
| Dateiverknüpfung | `[report.pdf](urn:file:<file-uuid>?name=report.pdf)` | Datei/media URNs können Metadaten-Abfrageparameter enthalten. |
| Bild/video Link | `![photo.png](urn:image:<file-uuid>?name=photo.png)` | Bilder und Videos verwenden `urn:image` / `urn:video`. |
| Avatar/default Bild | `[avatar](urn:gravatar:<hash>)` | Das gleiche kanonische Gravatar URN Format wie Workspace Benutzer; der Hash beträgt 32 oder 64 hexadezimalzeichen. |
| Außen URL | `[site](urn:url:https://example.com)` | Die externen `http` / `https`-Verbindungen werden über `urn:url` gespeichert. |

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Nachrichtenscheinigung. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `stream_uuid` | UUID | - Ja, das ist es. | - Nein. | Strom UUID. |
| `topic_uuid` | UUID | - Nein. | - Nein. | Thema UUID; ausgelassen oder `null` verwendet das Stream-Standardthema. Die Anfrage scheitert mit Code `400001007`, wenn der Stream keine Standardfunktion hat. |
| `author_uuid` | UUID | - Nein. | - Ja, das ist es. | Nachrichtenautor. |
| `payload` | Gegenstand | - Ja, das ist es. | - Nein. | Markdown-Nachrichten-Nutzlast; beschnittener Inhalt muss 1..40.000 Zeichen betragen. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Der aktuelle Benutzer in der Benutzernachrichtansicht. |
| `read` | Boolean | - Nein. | - Ja, das ist es. | Der aktuelle Benutzer hat eine Leseflagge. |
| `pinned` | Boolean | - Nein. | - Ja, das ist es. | Die eingeschlossene Flagge des aktuellen Benutzers. |
| `starred` | Boolean | - Nein. | - Ja, das ist es. | Die Sternflagge des aktuellen Benutzers. |
| `is_own` | Boolean | - Nein. | - Ja, das ist es. | Ob `author_uuid` dem aktuellen Benutzer entspricht. |
| `mentioned` | Boolean | - Nein. | - Ja, das ist es. | Ob die Markierung der Nutzlast den aktuellen Benutzer erwähnt; Standardwert `false`. |
| `reactions` | Gegenstand | - Nein. | - Ja, das ist es. | Aggregate Reaktionszahlen mit `emoji_name`. |
| `reaction_users` | Gegenstand | - Nein. | - Ja, das ist es. | Vollständige persistente Benutzerlisten UUID für begrenzte Reaktionsgruppen, die durch `emoji_name` eingeschlossen werden. |
| `source_name` | `native`, `zulip` | - Nein. | - Nein. | Nachrichtenquelle Name; die öffentliche API setzt ihn standardmäßig auf `native`, wenn es ausgelassen wird. |
| `source` | Gegenstand | - Nein. | - Nein. | Nachrichtquelle Nutzlast; Standardeinstellung `{"kind": "native"}`. Zulip `message_id` kann `null` sein, bis die ausgehende Synchronisation erfolgreich ist. |
| `provider` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Anbieter-Badge, die vom ausgewählten, vom Anbieter unterstützten Stream geerbt wurde. |
| `delivery` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Aktuelle Erstellung/update/delete Lieferprojektion. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

Erstellen einer Anfrage:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  }
}
```

Beispiel für die Antwort:

```json
{
  "uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "author_uuid": "11111111-1111-1111-1111-111111111111",
  "payload": {
    "kind": "markdown",
    "content": "Hello, workspace"
  },
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "read": true,
  "pinned": false,
  "starred": false,
  "is_own": true,
  "mentioned": false,
  "reactions": {},
  "reaction_users": {},
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T10:10:00Z",
  "updated_at": "2026-06-22T10:10:00Z"
}
```

Updateanfrage:

```json
{
  "payload": {
    "kind": "markdown",
    "content": "Edited text"
  }
}
```

`PUT /api/workspace/v1/messenger/messages/{message_uuid}` verpflichtet die aktualisierte kanonische Nachricht Nutzlast und gibt zurück
Nur der Autor der Nachricht kann die Root-Ansicht aktualisieren.
`DELETE /api/workspace/v1/messenger/messages/{message_uuid}` führt
Die Daten werden in einem System mit einem automatischen Hard-Delete-Verfahren gelöscht.
Dieselbe Transaktion gibt ein minimales `message.deleted` Ereignis für die ursprüngliche Transaktion aus
Zielgruppe, die die erforderlichen Nachrichtenidentitäts- und Herkunftsfelder behält.

Lesen Sie die Aktion:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read/invoke
Authorization: Bearer <access_token>
```

`read` setzt die Nachrichtenausgabe des aktuellen Benutzers auf `true` und gibt die aktualisierte
Nachricht anzeigen. Wenn die Nachricht nicht gelesen wurde, sendet das Backend `message.read` mit
die vollständige Nachrichtenaufnahme und die aggregierten Unleserzählungsaktualisierungen.

Lesen Sie bis zur Aktion:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/read_up_to/invoke
Authorization: Bearer <access_token>
```

`read_up_to` markiert ungeliesene Nachrichten im gleichen Thema durch die ausgewählte
Die Botschaft ist umfassend.`(created_at, uuid)`Grenze, dann gibt die ausgewählte
Für einen externen Chat sendet Workspace die bereits gelöste UUID
Präfix als genauer Auswahlfaktor; die anbieterspezifische Nachrichtenfolge kann nicht geändert werden
welche Nachrichten gelesen werden.

Star und Unstar Aktionen:

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/star/invoke
Authorization: Bearer <access_token>
```

```http
POST /api/workspace/v1/messenger/messages/a93dca35-3061-4748-bda4-7f6f8c660ea5/actions/unstar/invoke
Authorization: Bearer <access_token>
```

`star` und `unstar` setzen die `starred`-Flagge des aktuellen Benutzers und geben die
Die beiden Aktionen sind idempotent.
Das Backend sendet nur für den aktuellen Benutzer `message.updated` aus.
von Workspace und nicht mit einem externen Anbieter synchronisiert ist.

Nebenwirkungen in Echtzeit:

| Betrieb | payload.kind | object_type | Nutzlast |
| --- | --- | --- | --- |
| Nachricht erstellen | `message.created` | `message` | Vollständige Benutzernachrichten für jeden Streambenutzer. |
| Nichtgelesenes Nachrichten erstellen | `topic.updated`, `stream.updated` | `topic`, `stream` | Aktualisierte unverlesene Schnappschüsse für Benutzer, bei denen die neue Nachricht nicht gelesen wurde; Benutzeroberfläche leitet Ordneraggregate aus dem Stream-Schnappschuss ab. |
| Aktualisierung der Nutzlast | `message.updated` | `message` | Vollständige Benutzernachrichten für jeden Streambenutzer. |
| Reaktion erzeugen/update/delete | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` | `message_reaction` | Reaktionsschnappschuss für den spielenden Benutzer. |
| Erstellen/update/delete der Aktualisierung der Reaktionsaggregate | `message.updated` | `message` | Vollständige Benutzernachrichten-Schnappschuss mit aktualisierten `reactions` und `reaction_users` für jeden Stream-Benutzer. |
| Nachricht lesen oder bis zur Nachricht lesen | `message.read` | `message` | Vollständige Benutzernachricht-Snapshot, die durch die Aktion zurückgegeben wurde. |
| Nichtgelesenes Nachrichten lesen | `topic.updated`, `stream.updated`, `folder.updated` | `topic`, `stream`, `folder` | Aktualisierte nicht gelesenen Schnappschüsse für den aktuellen Benutzer. |
| Star oder Unstar-Nachricht | `message.updated` | `message` | Vollständige Benutzernachricht für den aktuellen Benutzer, wenn die Flagge geändert wird. |
| Nachricht löschen | `message.deleted` | `message` | Löschungen von Nachrichten `uuid`, `stream_uuid`, `topic_uuid`, `author_uuid`, `source_name` und `source`, die an jeden Stream-Benutzer gesendet wurden. |
| Nichtgelesenes Nachrichten löschen | `topic.updated`, `stream.updated` | `topic`, `stream` | Aktualisierte unverlesene Snapshots für Benutzer, bei denen die gelöschte Nachricht nicht gelesen wurde; Benutzeroberfläche leitet Ordneraggregate aus dem Stream-Snapshot ab. |

## Entwürfe {#drafts}

Entwürfe sind PostgreSQL-eigene Client-Staat und nie erstellen oder ändern kanonische
Nachrichten, nicht gelesenen Zählern, Reaktionen oder Dateiverweisen.
Der Entwurf gehört genau zu einem IAM Projekt, Eigentümer, Stream und Thema.
`stream_uuid` und `topic_uuid` sind unveränderlich, das Thema muss der
Der Eigentümer muss derzeit ein Stream-Teilnehmer sein.
kann für das gleiche Stream/topic Paar existieren.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Ja, das ist es. | Nach der Erstellung | vom Client generierter Identifikationsschlüssel und Entwurfserkennung. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Entwurfseigentümer aus dem IAM Token. |
| `stream_uuid` | UUID | - Ja, das ist es. | Nach der Erstellung | Strom, der den Zug enthält. |
| `topic_uuid` | UUID | - Ja, das ist es. | Nach der Erstellung | Thema, das den Entwurf enthält; es muss `stream_uuid` gehören. |
| `payload` | Gegenstand | - Ja, das ist es. | - Nein. | Markdown-Entwurf Nutzlast. Es ist das einzige Feld, das von `PUT` akzeptiert wird. |
| `revision` | Ganzzahl, mindestens 1 | - Nein. | - Ja, das ist es. | Stärke ETag Revision, ab `1`. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die letzte Aktualisierung. |

Erstellen einer Anfrage:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  }
}
```

Antwort:

```json
{
  "uuid": "ca14d274-0057-4a9a-a34b-fb1174be6a17",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "payload": {
    "kind": "markdown",
    "content": "Draft message"
  },
  "revision": 1,
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```

Updateanfrage:

```http
PUT /api/workspace/v1/messenger/drafts/ca14d274-0057-4a9a-a34b-fb1174be6a17
Authorization: Bearer <access_token>
Content-Type: application/json
If-Match: "1"

{
  "payload": {
    "kind": "markdown",
    "content": "Updated draft message"
  }
}
```

Erstellen Sie Anfragen erfordern `uuid`, `stream_uuid`, `topic_uuid`, und ein Markdown
Markdown-Inhalte werden beschnitten, müssen nicht leer bleiben und sind auf
Wenn wir den gleichen kanonischen Erstellen UUID erneut versuchen, wird die
bestehender Entwurf ohne weitere Mutation; Wiederverwendung des UUID mit verschiedenen Feldern
ergibt `409`.

`GET`, `POST` und `PUT` Single-Resource-Antworten geben eine starke ETag wie
`ETag: "3"`. `PUT` akzeptiert nur `payload`; `PUT` und `DELETE` erfordern die genaue
Der aktuelle Wert in `If-Match`. Fehlende Voraussetzungen geben `428` zurück.
Nicht gültige Wertmeldungen `412` mit dem aktuellen Entwurfssnapshot und dem aktuellen ETag.
Erfolgreiche Aktualisierungen erhöhen den Inkrement `revision`; erfolgreiche Löschungen geben den Rückgang `204` an.

Entwurf CRUD gibt keine Workspace-Ereignisse, Websocket-Benachrichtigungen, Desktop aus
Anweisungen, Anbieterbefehle oder gewöhnlicheMessengerEine weitere
Der Client beobachtet Änderungen beim Neuladen oder einer expliziten Entwürfe API
der Besitzer aus dem Stream oder das Thema löschen/stream Hard-deletes affected
Durchflutungen durch PostgreSQL Fremdschlüsselkaskaden ohne Grabsteine oder
Nebenwirkungen.

## Reaktionen auf die Nachricht

Nachrichtenreaktionen sind kanonische PostgreSQL Ressourcen.
Nachrichten, die für den aktuellen IAM Benutzer sichtbar sind.
Erstellen, Aktualisieren oder Löschen einer Reaktion erzeugt ein Ereignis `message_reaction.*`
für den handelnden Benutzer und `message.updated` Ereignisse für jeden Benutzer, der sehen kann
die Nachricht; die Nachricht-Snapshot enthält aggregierte `reactions` und die gleiche
die `reaction_users` Projektion wie REST lautet.

| Feld | Typ | Erforderlich bei der Erstellung | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Reaktionskennzeichen. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang. |
| `message_uuid` | UUID | - Ja, das ist es. | - Nein. | Nachricht, auf die reagiert wird; muss für den aktuellen Benutzer sichtbar sein. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Der Benutzer, der die Reaktion besitzt. |
| `emoji_name` | String, maximal 128 | - Ja, das ist es. | - Nein. | Emoji/reaction Name. |
| `provider` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Anbieter-Badge, die von der Zielnachricht geerbt wurde. |
| `delivery` | Gegenstand oder `null` | - Nein. | - Ja, das ist es. | Aktuelle Erstellung/update/delete Lieferprojektion. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

`provider_metadata` und `delivery_metadata` sind Roh-DM-Speicherfelder, nicht
Die Ergebnisse der Studie werden in den folgenden Bereichen ermittelt:
`WorkspaceMessageReactions` OpenAPI Schemas, aber die Laufzeit
`resource_projection.as_dict(..., "message_reactions")` Serialisierer entfernt
die vor der Reaktionsverpackung entstehen und nur die desinfizierten `provider` und
`delivery` Vorhersagen.
generiertes Schema.

Erstellen einer Anfrage:

```json
{
  "message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "emoji_name": "thumbs_up"
}
```

Der gleiche Benutzer kann keine doppelten Reaktionen mit dem gleichen `message_uuid` erstellen
Und ...`emoji_name`. Jeder Benutzer, der die Nachricht sehen kann, kann die Nachricht auflisten oder erhalten
Nur der Eigentümer der Reaktion kann diese Reaktion aktualisieren oder löschen.
Diese Operationen verpflichten die Reaktion und die entsprechenden Nebenwirkungen in Echtzeit
in PostgreSQL. Native Antworten behalten `provider: null` und `delivery: null`.

Das Feld `reactions` auf Nachrichtenansichten ist eine Aggregatkarte:

```json
{
  "thumbs_up": 2,
  "eyes": 1
}
```

Das Feld `reaction_users` zeigt nur für kleine Gruppen vollständige UUID-Listen an
Die Standardgrenze pro Gruppe beträgt vier
Benutzer (`[messenger_reactions] user_list_limit`). Der Client sendet nicht oder
die Grenze abzuleiten:

```json
{
  "reactions": {
    "eyes": 12,
    "heart": 3
  },
  "reaction_users": {
    "heart": [
      "11111111-1111-1111-1111-111111111111",
      "22222222-2222-2222-2222-222222222222",
      "33333333-3333-3333-3333-333333333333"
    ]
  }
}
```

Die Anwesenheit eines Emoji-Tasts garantiert, dass die Liste vollständig war, als die
Die Reaktionsgruppe wurde zuletzt mutiert.
Die Schlüssel, die in der Schlüsselliste enthalten sind, werden von der Schlüsselliste entfernt.
Nachrichten werden nicht zurückgefüllt und geben daher `reaction_users: {}` zurück, bis a
Die Reaktionsmutation materialisiert einen betroffenen Schlüssel.
Die neue Mutation der Gruppe wird durch die
Klienten ersetzen die gesamte Karte bei jeder REST oder Echtzeitnachricht
Schnappschuss; sie dürfen ihn nicht mit einem vorherigen Wert zusammenführen.

Reaktions-Echtzeit-Nutzlasten umfassen `uuid`, `project_id`, `message_uuid`,
`user_uuid`, `emoji_name`, `source_name`, `source`, `provider` und `delivery`.
Sie zeigen nie Roh `provider_metadata` oder `delivery_metadata` für
`message_reaction.updated`, `old_message_uuid`, `old_emoji_name`,
`old_source_name` und `old_source` beschreiben das vorherige Reaktionsziel.

## Dateien {#files}

Die Dateibytes und ein separater JSON Sidecar werden über die konfigurierte
S3 ist das bereitgestellte Backend; das lokale Backend
Das Backend wird vom Server ausgewählt.
PostgreSQL speichert die
die kanonischen Dateimetadaten und den Zustand ACL/access; S3 speichert die Binärdatei und ihre JSON
- Ich habe einen Wagen.

Der Sidecar enthält die Datei UUID, das Projekt UUID, den Eigentümer UUID, die Metadaten der Anzeige,
Inhaltstyp, Größe, SHA-256, Erstellungszeit und eine ACL-Regel.
die Daten des Stroms UUID und die dynamische Strommitgliedschaftsregel verwenden:

```json
{
  "acl": {
    "mode": "stream_members",
    "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6"
  }
}
```

Der Seitenwagen enthält nie einen Teilnehmer-Snapshot.
Metadaten und Downloadanfrage überprüft den authentifizierten Benutzer gegenüber der
Aktuelle KanonischePostgreSQLEin neu hinzugefügter
Teilnehmer erhalten sofort Zugriff; entfernte Teilnehmer verlieren ihn
unmittelbar ohne S3 neu zu schreiben.

Dateien, die absichtlich im gesamten authentifizierten Workspace sichtbar sind, verwenden diese
ACL stattdessen:

```json
{
  "acl": {
    "mode": "public"
  }
}
```

`public` ist kein anonymer Zugriff. Metadaten und Bytes bleiben hinter der
Workspace IAM Middleware und jede Anfrage ohne gültigen Workspace Träger
Ein gültiges Workspace Träger-Token kann eine
`public`Die Teilnehmer sind unabhängig von der Mitgliedschaft in einem Projekt oder einem Strom.`public`Seitenwagen
darf nicht `stream_uuid` enthalten; es behält `owner_uuid` und alle Integrität
Nginx lehnt mehrere Anfragen ab, die größer als `50m` sind, bevor sie erreicht werden
`workspace-messenger-api`.

| Feld | Typ | Erforderlich bei JSON erstellen | Nur für Lesen | Beschreibung |
| --- | --- | --- | --- | --- |
| `uuid` | UUID | - Nein. | - Ja, das ist es. | Datei-Identifikator. |
| `project_id` | UUID | - Nein. | - Ja, das ist es. | IAM Projektumfang; in API Antworten versteckt. |
| `user_uuid` | UUID | - Nein. | - Ja, das ist es. | Eigentümer/uploader. |
| `stream_uuid` | UUID oder `null` | - Ja, das ist es. | - Nein. | Streaming, das eine Chatdatei besitzt. Für JSON erstellen und `stream_members` mehrteiligen Uploads erforderlich; für mehrteiligen Uploads mit `acl.mode=public` weggelassen. |
| `name` | String, max. 255 | - Ja, das ist es. | - Nein. | Anzeigenname der Datei. |
| `description` | String, max. 255 | - Nein. | - Nein. | Dateibeschreibung; Standard für eine leere Zeichenfolge. |
| `content_type` | String | - Ja, das ist es. | - Nein. | MIME Inhaltstyp. |
| `size_bytes` | ganzzahl | - Ja, das ist es. | - Nein. | Dateigröße in Bytes. |
| `hash` | String | - Ja, das ist es. | - Nein. | Dateihash, derzeit SHA-256 für mehrteilige Uploads. |
| `created_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | - Nein. | - Ja, das ist es. | Zeit für die Aktualisierung. |

JSON Metadaten erstellen Anfrage:

```json
{
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "name": "example.txt",
  "description": "Example",
  "content_type": "text/plain",
  "size_bytes": 12,
  "hash": "abc"
}
```

Mehrteilige Aufladebedarf:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
stream_uuid=75309057-419c-4b12-a7c1-3932429ec4a6
name=example.txt
description=Example
```

Ein gewöhnlicher authentifizierter Client lädt eine Workspace-weite öffentliche Datei über
derselbe Endpunkt, indem das vorhandene ACL Objekt als JSON gesendet und weggelassen wird
`stream_uuid`:

```http
POST /api/workspace/v1/messenger/files/
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<binary file part>
acl={"mode":"public"}
name=public-example.txt
description=Authenticated Workspace-wide file
```

Für mehrteilige Uploads ist `file` erforderlich und genau ein Bereich muss angegeben werden
Vorbehaltlich: entweder `stream_uuid` oder das Formularfeld JSON
`acl={"mode":"public"}`. Öffentliche Uploads ablehnen `stream_uuid`; Streaming-Uploads
die `stream_members` ACL `name` Voreinstellungen für den hochgeladenen Dateinamen und
`description` wird standardmäßig auf eine leere Zeichenfolge festgelegt.
`content_type` aus dem hochgeladenen Teil berechnet `size_bytes` und schreibt eine
SHA-256 `hash`. Beide Modi bewahren die gleiche binäre Plus JSON Sidecar Layout und
derselbe Kundenvertrag `urn:file`, `urn:image` oder `urn:video`.

`GET /api/workspace/v1/messenger/files/`, `GET /api/workspace/v1/messenger/files/{file_uuid}` und
`GET /api/workspace/v1/messenger/files/{file_uuid}/actions/download` erfordern Zugriff auf Dateien. `PUT` und
`DELETE` erfordern Dateibesitz. Downloads zurück Rohbyte mit den gespeicherten
`Content-Type`, ein `Content-Disposition` Anschlussdateiname und eine starke
`ETag` gleich dem zitierten SHA-256 `hash` durch Datei-Metadaten ausgesetzt.
ist für seine Datei UUID unveränderlich; Metadatenänderungen emittieren `file.updated`.
Eigentümerdatei entfernt sowohl sein binäres Objekt und JSON Seitenwagen nach der kanonischen
Die Datei wird gelöscht.


## Dienstleistungen {#services}

Dienstleistungen sind nur lesbare Katalogbezeichnungen, die durch die gemeinsame Workspace API freigegeben werden.
`GET /api/workspace/v1/services/` listet die verfügbaren Dienstleistungen auf und
`GET /api/workspace/v1/services/{service_uuid}` gibt einen Dienst zurück.

| Feld | Typ | Beschreibung |
| --- | --- | --- |
| `uuid` | UUID | Dienst-Identifikator. |
| `name` | String, max. 255 | Dienstnamen. |
| `description` | String, max. 255 | Servicebeschreibung; Standard für eine leere Zeichenfolge. |
| `service_url` | URL | Dienstleistungseingang URL. |
| `icon` | URL oder `null` | Optional Symbol URL. |
| `created_at` | Zeit und Datum | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | Die letzte Aktualisierung. |

Beispiel für die Antwort:

```json
{
  "uuid": "608919f5-ae0f-44fb-85bf-f1bf56534238",
  "name": "Messenger",
  "description": "Workspace Messenger",
  "service_url": "https://workspace.example.com/",
  "icon": "https://workspace.example.com/icon.svg",
  "created_at": "2026-07-17T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```


## Ereignisse und Epoche {#events-and-epoch}

Ereignisse sind dauerhafte PostgreSQL Datensätze, die auf ihr Publikum ausgerichtet sind.
Ereignisse tragen `user_uuid`; kompakte Sendeereignisse verwenden ein gespeichertes Publikum, so
Jeder sichtbare Kunde hält den gleichen öffentlichen Veranstaltungsvertrag ein, ohne
Einmal pro Empfänger werden nur Ereignisprotokolle für die
konfiguriertes Intervall, standardmäßig 72 Stunden; Nachrichten, Dateien, Stream/topic
Die Datenbank wird von der
Das Schneiden fördert den gespeicherten Boden, so dass die verbleibenden Ereignisse
ein vollständig sichtbares Suffix.
`epoch_version` ist monoton innerhalb eines PostgreSQL-eigenen
`epoch_generation`.

`GET /api/workspace/v1/events/` gibt Ereignisse zurück, die standardmäßig nach `epoch_version` aufsteigend sortiert wurden.
REST `/events/` und Websocket-Lieferung verwenden das gleiche flache Schema und beide lesen
von der sichtbaren Ereignisfläche PostgreSQL des aktuellen Benutzers.
`GET /api/workspace/v1/epoch/` verwendet die gleiche Oberfläche.

```json
{
  "schema_version": 1,
  "uuid": "event-uuid",
  "epoch_version": 124,
  "project_id": "project-uuid",
  "user_uuid": "recipient-user-uuid",
  "object_type": "message",
  "action": "created",
  "created_at": "2026-07-02T16:37:49.552044Z",
  "updated_at": "2026-07-02T16:37:49.552047Z",
  "payload": {
    "kind": "message.created",
    "uuid": "message-uuid",
    "project_id": "project-uuid",
    "user_uuid": "recipient-user-uuid",
    "stream_uuid": "stream-uuid",
    "topic_uuid": "topic-uuid",
    "author_uuid": "author-user-uuid",
    "payload": {"kind": "markdown", "content": "Hello"},
    "source_name": "native",
    "source": {"kind": "native"},
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "mentioned": false,
    "reactions": {},
    "reaction_users": {},
    "provider": null,
    "delivery": null,
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

Die obersten Felder beschreiben nur die Ereigniszeile. `payload.kind` ist die einzige `kind`.
Erwarten Sie nicht, dass die Spitzenstufe `type`, `kind`, `stream_uuid` oder `topic_uuid`

Nachrichten erstellen/update Ereignisse tragen die gleiche Markdown-Nutzlast auf dem
Entity-Verbindungen bleiben regelmäßige Markdown-Verbindungen mit `urn:user`,
`urn:message`, `urn:stream`, `urn:topic`, Datei/media, Avatar oder URL URNs.

Messenger Entity-Erstellung, Aktualisierung, Lesung und Aktionsereignisse tragen die gleiche vollständige
Objekt-Snapshot, den der aktuelle Benutzer vom entsprechenden REST erhält
Endpunkt oder Aktionsantwort, plus `payload.kind`.
Die Daten werden in einem Umschlag mit `kind` verwendet, der die Ressource
`uuid`, und eine gesäuberte volle Ressource unter `snapshot`.
für externe Ereignisse erstellen, aktualisieren und löschen.

Messenger Entity-Delete-Ereignisse sind minimal:

- `stream.deleted`, `folder.deleted`, `folder_item.deleted`: `kind`, `uuid`
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name`, `source`

`stream_bindings.created` ist eine Batch-Action-Nutzlast:

```json
{
  "kind": "stream_bindings.created",
  "uuid": "stream-uuid",
  "items": [
    {
      "uuid": "binding-uuid",
      "project_id": "project-uuid",
      "stream_uuid": "stream-uuid",
      "user_uuid": "added-user-uuid",
      "who_uuid": "owner-user-uuid",
      "role": "member",
      "notification_mode": "all_messages",
      "notification_updated_at": "2026-07-02T16:37:49.552044Z",
      "created_at": "2026-07-02T16:37:49.552044Z",
      "updated_at": "2026-07-02T16:37:49.552047Z"
    }
  ]
}
```

Lesen Aktionen emittieren `message.read`, `topic.read` oder `stream.read` mit der vollen
Aktionsantwortobjekt in `payload`.
`topic.updated`, `stream.updated`Und ...`folder.updated`. Nachricht erstellen/delete
verwendet kompakte `topic.updated` und `stream.updated` Ereignisse; der Ordner UI-Projekte
Die Daten werden von der Strömung aufgenommen, anstatt eine potenziell große
Benutzerspezifische Ordner-Schnappschüsse pro Nachricht.

Unterstützte Werte:

| object_type | Aktion | payload.kind Beispiele |
| --- | --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` | `message.created`, `message.updated`, `message.deleted`, `message.read`, `messages.read` |
| `message_reaction` | `created`, `updated`, `deleted` | `message_reaction.created`, `message_reaction.updated`, `message_reaction.deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` | `stream.created`, `stream.updated`, `stream.deleted`, `stream.read` |
| `stream_binding` | `created`, `updated`, `deleted` | `stream_bindings.created`, `stream_binding.updated`, `stream_binding.deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` | `topic.created`, `topic.updated`, `topic.deleted`, `topic.read` |
| `user` | `updated` | `user.updated` |
| `folder` | `created`, `updated`, `deleted` | `folder.created`, `folder.updated`, `folder.deleted` |
| `folder_item` | `deleted` | `folder_item.deleted` |
| `file` | `created`, `updated`, `deleted` | `file.created`, `file.updated`, `file.deleted` |
| `external_account` | `created`, `updated`, `deleted` | `external_account.created`, `external_account.updated`, `external_account.deleted` |
| `external_chat` | `created`, `updated`, `deleted` | `external_chat.created`, `external_chat.updated`, `external_chat.deleted` |
| `external_operation` | `created`, `updated`, `deleted` | `external_operation.created`, `external_operation.updated`, `external_operation.deleted` |

Alle externen Werte in der Tabelle sind öffentliche Ereignisarten.
Die Daten des Kontos und der Betriebsarbeiten werden alle drei Aktionen ausstrahlen.
Aufrufstellen geben `external_chat.updated` für Katalog- und Zuordnungsänderungen aus und
`external_chat.deleted` wenn eine Vorsteigung entfernt wird;
`external_chat.created` bleibt eine registrierte Schemaart.

Für eine strikte Nachholleistung nach einer verarbeiteten Kurzeranwendung:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<saved_generation>&page_limit=500
```

`GET /api/workspace/v1/epoch/` gibt den letzten sichtbaren Ereignis-Cursor und die
IAMBenutzer.`epoch_version`ist die direkte
Alias von `current_epoch_version`:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

Für einen neu erstellten leeren Ereignisstrom `epoch_version` und
`current_epoch_version` sind `0`, `minimum_epoch_version` ist `1` und
`epoch_generation` ist immer noch eine nicht-leere PostgreSQL-eigene Generation.
`GET /api/workspace/v1/events/?epoch_version%3E=0` gibt eine leere Liste zurück
als ein Cursor-Gap-Fehler.

Die Kunden bleiben `epoch_generation` zusammen mit `epoch_version`.
Kurzer über Null ohne eine Generation, eine veränderte Generation, eine zukünftige Epoche,
oder eine Epoche, die älter ist als das zurückgehalte Suffix, gibt HTTP `410` mit
`type=EventsCursorExpiredError`, `error=epoch_pruned`, der Grund und die
Strom/minimumDie Antwort lautet:`Cache-Control: no-store`.
Clients löschen dann abgeleitete Entity/blob Caches, laden autoritäre Snapshots,
und die Nachverfolgung von der zurückgegebenen Generation neu starten; Servernachrichten und Domäne
Daten werden nicht gelöscht.

## Workspace Benutzer

Workspace Benutzer werden in `m_workspace_users` gespeichert.
Die Kommission hat die Kommission aufgefordert,

`GET /api/workspace/v1/me/` gibt das gleiche `WorkspaceUser_Get` Objekt zurück wie
`GET /api/workspace/v1/users/{user_uuid}`, mit dem Benutzer UUID aus dem IAM
Der Client sendet oder leitet keinen Benutzer UUID für diese Anfrage ab.
Das Backend nimmt `project_id` aus IAM-Introspection, aktualisiert die IAM-eigenen
Benutzername, Vorname, Nachname und E-Mail-Projektion, und gibt die lokale
Workspace Status, Avatar und Anwesenheitsfelder.

IAMDie Identitäten werden faul projiziert.`/me/`oder die aktuelle
Benutzer durch `/users/{user_uuid}` erstellt oder aktualisiert die Benutzer Workspace
Projektion; Auflistung`/users/`Ich habe nicht die nötige Zeit, um zu lernen.IAMEin
`GET /users/{other_user_uuid}` Suche ist nur projizierbar: es importiert nicht
dass IAM Identität und nicht gefunden wird, bis der andere Benutzer gefunden wurde
Die Autorität der Menschen ist durch ihre eigenen Autentisierung verwirklicht worden .WorkspaceAktivität.

Wenn der aktuelle IAM Benutzer seine eigene UUID anfordert, wird die API verwirklicht oder
Die IAM Identitätsprojektion wird vor der Rückgabe aktualisiert.
Die `zulip` Quelle literale identifiziert eine
Die von der Zulip ausführungszeit durch den privaten Anbieter projizierte externe Identität
API. Anbieter-Anmeldeinformationen und Roh-Identifikatoren sind nicht Teil dieses Browsers
- Die Ressource.

| Feld | Typ | Beschreibung |
| --- | --- | --- |
| `uuid` | UUID | Benutzerkennung. |
| `username` | String, 1,128 | Benutzername. |
| `source` | `iam`, `zulip` | Benutzerquelle. |
| `identity_kind` | `external` oder weggelassen | Nur für Lesen verfügbare Markierung nur für eine Identität eines externen Anbieters. |
| `display_name` | String oder ausgelassen | Leseanzeiger-Anzeigenname für eine externe Identität. |
| `provider` | Gegenstand oder weggelassen | Lesefreier externer Identitätsumschlag mit `kind` und `account_uuid`; Rohanbieter-IDs und -Zugriffsdaten werden nie freigegeben. |
| `status` | `active`, `idle`, `offline`, `do_not_disturb` | Anwesenheitsstatus. |
| `status_emoji` | String oder `null`, max. 64 | Ein eigenes Anwesenheits-Emoji. |
| `status_text` | String oder `null`, maximal 256 | Benutzerdefinierte Anwesenheitsmeldung. |
| `first_name` | String oder `null` | Vornamen. |
| `last_name` | String oder `null` | Nachname. |
| `email` | String oder `null` | E-Mail-Adresse. |
| `avatar` | URN Zeichenfolge | Benutzer-Avatar. Unterstützte Werte sind `urn:gravatar:<32-or-64-hex-hash>`, `urn:image:<uuid>` und `urn:url:http(s)://...`. Wenn ausgelassen, hasht Workspace die normalisierte E-Mail mit MD5; Benutzer ohne E-Mail erhalten einen nicht umkehrbaren MD5 Fallback, der von ihrem UUID abgeleitet wird. |
| `last_ping_at` | Zeit und Datum | Der letzte Ping-Zeitstempel. |
| `created_at` | Zeit und Datum | Die Schöpfungszeit. |
| `updated_at` | Zeit und Datum | Zeit für die Aktualisierung. |

Ein externer Anbieter kann einen Gravatar-kompatiblen Avatar als
`urn:gravatar:<md5(trim(lower(delivery_email)))>`. Rohanbieter-Identifikatoren und
Lieferadressen für den Anbieter sind in diesem Vertrag nicht angegeben.

Anwesenheitsupdate:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/presence/invoke
Content-Type: application/json

{
  "status": "active",
  "emoji": "coffee",
  "text": "Focusing"
}
```

Der authentifizierte Benutzer kann nur seine eigene `user_uuid` aktualisieren.
der bereitgestellte Status und der aktuelle Zeitpunkt in`last_ping_at`- Optional .`emoji`und
`text` Felder werden als `status_emoji` und `status_text` gespeichert; nicht erwünscht
Die vorherigen Werte bleiben bei den Feldern und sind explizit.`null`- Das macht sie sauber.WorkspaceDer Messenger-Mitarbeiter markiert veraltete Benutzer offline und sendet`user.updated`Ereignisse mit vollen Benutzern
Schnappschüsse, einschließlich `avatar`, für alle Workspace-Benutzer in jedem Projekt.

Avatar-Upload ist eine atomare eigene Benutzeraktion:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_upload/invoke
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

file=<PNG, JPEG, GIF, or WebP binary part>
```

Nur die eigene UUID des authentifizierten Benutzers wird akzeptiert.
25 MiB. Das Backend validiert den angegebenen Typ MIME und die Binärsignatur,
Speichert die Bytes und JSON Sidecar durch das konfigurierte Dateibackofen, setzt
`acl.mode` bis `public`, weglässt `stream_uuid` und aktualisiert nur `user.avatar` bis
`urn:image:<file-uuid>`. IAM-eigene Benutzername, Name und E-Mail-Felder bleiben
Die Aktion gibt den vollen `user.updated` Snapshot in jedem Workspace
Das Projekt.

Das Zurücksetzen des Avatars verwendet die gleiche Benutzerberechtigung:

```http
POST /api/workspace/v1/users/11111111-1111-1111-1111-111111111111/actions/avatar_reset/invoke
Authorization: Bearer <access_token>
Content-Type: application/json

{}
```

Zurücksetzen ersetzt `user.avatar` durch
`urn:gravatar:<md5(trim(lower(email)))>` oder die kanonische nicht umkehrbare UUID
Ein ersetzter benutzerdefinierter Avatar verliert den öffentlichen Zugriff
sobald die Benutzerreferenz- und Projektionszeile aktualisiert werden;
Die Fahrzeuge werden dann aus dem Objektlager entfernt.

## WebSocket Zusammenfassung in Echtzeit {#websocket-realtime-summary}

Der gemeinsame Websocket-Dienst verwendet das Subprotokoll `workspace.events.v1` und authentifiziert
das auf dem Inhaber befindliche Token von `Sec-WebSocket-Protocol`:

```ts
const ws = new WebSocket(
  "/api/workspace/v1/events/ws?last_epoch_version=124&epoch_generation=781203",
  ["workspace.events.v1", `bearer.${accessToken}`],
);
```

Nach der Annahme der Verbindung sendet der Server verpasste Ereignisse, die neueren als
Es sendet dann genau einen Kontrollrahmen
`{"type":"ready","epoch_generation":"...","epoch_version":124}` vor jeder
Die Nutzer-Nachrichten-Gates bleiben bis zu diesem
Jede Ereignismeldung ist das gleiche flache Ereignisobjekt, das von REST zurückgegeben wird
`/api/workspace/v1/events/`. Der Websocket-Dienst sendet keine
Anwendungs-Level- JSON `hello` oder `ping`-Nachrichten und verarbeitet den Client nicht
JSON `pong` oder `ack` Nachrichten. Es sendet Protokoll-Ebene WebSocket Ping-Kontrolle
Die Anschlüsse werden mit einem
Ein abgelaufener Cursor sendet die gleiche eingegebene
`epoch_pruned` JSON Fehler als REST und schließt mit Code `4410` und Grund
`epoch_pruned`.

Für geschützte Dateicache wird `file.created/updated/deleted` durch einen UUID ungültig gemacht.
Bei der Löschung der Mitgliedschaft erhält der entfernte Benutzer `stream.deleted`;
Sie müssen sofort jeden geschützten Blot entfernen, dessen zwischengespeicherte Metadaten diese haben.
`stream_uuid`. Die übrigen Teilnehmer erhalten `stream_binding.deleted` (und
Rolle/settings Änderungen erzeugen `stream_binding.updated`) zum Aktualisieren des Teilnehmers
Eine 410-Lücke löscht alle abgeleiteten Protected-Blob-Cache-Einträge.

Detaillierte Regeln für die Integration der Benutzeroberfläche sind in
`docs/workspace_ui_realtime_integration.md`.

## OpenAPI Und Einsatz

Das Dokument zur Laufzeit Workspace OpenAPI ist unter
`/api/workspace/specifications/3.0.3`. Es beschreibt die Steuerung unterstützt
IAM-authentifizierte HTTP-Oberfläche und enthält keinen Anbieter, keine Post oder keinen Kalender
Die von der Middleware bereitgestellten `server_settings` Alias-Namen und die
separate Ereignisse WebSocket sind dokumentierte Laufzeitschnittstellen, erscheinen aber nicht
wie erzeugt .OpenAPIDer Privatanbietervertrag wird beibehalten.
getrennt in
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

Das Backend-Element Workspace installiert unabhängige `workspace-messenger-api`,
`workspace-api`, `workspace-messenger-events` und
`workspace-messenger-worker` Prozesse plus die privaten
`workspace-external-bridge-api` Dienst. Die PostgreSQL-kanonische Laufzeit macht
Das Element benötigt S3aaS für binäre
Objekte und JSON Sidecars und DBaaS für den kanonischen Messenger- und Provider-Zustand.
Es baut die vorhandene Workspace Benutzeroberfläche im Messenger-Modus und bedient sie von
- Ich bin nicht sicher.

Verwandte Dokumente:

- [Workspace Architektur](architecture.md)
- [Workspace Echtzeit-Integration der Benutzeroberfläche](workspace_ui_realtime_integration.md)
- [Privater Workspace Anbieter API](../workspace_provider_api_v1.yaml)
- [Zulip Lieferantprodukt und öffentlicher API-Vertrag](zulip_bridge_v1_product_and_api.md)
