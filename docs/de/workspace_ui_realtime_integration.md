# Workspace UI-Echtzeitintegration

Dieses Dokument beschreibt den öffentlichen Messenger Echtzeitvertrag, der von der
Workspace Benutzeroberfläche. REST catch-up und Websocket-Lieferung verwenden das gleiche flache Ereignis
Objekt und die gleichen IAM-Scoped-Sichtbarkeitsregeln.

## Endpunkte

- `GET /api/workspace/v1/events/`
- `GET /api/workspace/v1/epoch/`
- `WS /api/workspace/v1/events/ws?last_epoch_version=<number>&epoch_generation=<generation>`

Der interne Websocket-Dienstpfade ist `/v1/events/ws` auf `127.0.0.1:21082`.
Der Browsercode muss nur den nginx-Pfad oben verwenden.
oder externen Websocket-Endpunkte für die Integration.

| Verkehr | Authentifizierung | Bestellung | Verwendungszweck |
| --- | --- | --- | --- |
| `GET /events/` | IAM Trägerheader | Aufsteigend `epoch_version` | Anfangslast, Wiederanschluss, Aufholung, Lückenbehebung |
| `GET /epoch/` | IAM Trägerheader | ein letzter Cursor | Vergleichen Sie den lokalen Fortschritt mit der sichtbaren Server-Epoche |
| `WS /events/ws` | IAM Token im Unterprotokoll | Verpasste Zeilen, dann Live-Zeilen | Lieferung mit geringer Latenzzeit nach dem Nachholprozess |

## Authentifizierung

REST Anfragen verwenden das IAM-Token:

```http
Authorization: Bearer <accessToken>
```

Websocket-Clients senden genau diese beiden `Sec-WebSocket-Protocol` Werte:

```ts
["workspace.events.v1", `bearer.${accessToken}`];
```

Der Server wählt `workspace.events.v1`.
String. Behalten Sie an und senden Sie `epoch_generation` mit jedem nicht-Null-Wiederholungs-Cursor;
ausgelassen `last_epoch_version` bedeutet den kalten Cursor `0`.
Die Daten werden in einem System mit einem
konfigurierte Retined Suffix, standardmäßig 72 Stunden, kann nicht vollständig
Geschichte von Epoche `1`.
Nicht autorisierte Handschläge mit `4401` und ungültige Handschläge mit `4401`
`4400`. Token-Aktualisierung erfordert eine neue Verbindung.

## Ereignisform

Jedes REST Ereignis und jede Websocket-Ereignisnachricht sind gleich `schema_version: 1`
Es gibt keinen äußeren Gegenstand.`{ "type": "event", "event": ... }`Die
Socket sendet zusätzlich eingegebene `ready` und Cursor-Fehler-Steuerungsmeldungen.

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
    "payload": {
      "kind": "markdown",
      "content": "Hello"
    },
    "read": true,
    "pinned": false,
    "starred": false,
    "is_own": true,
    "reactions": {},
    "reaction_users": {},
    "created_at": "2026-07-02T16:37:49.552044Z",
    "updated_at": "2026-07-02T16:37:49.552047Z"
  }
}
```

Die Feldbereiche der obersten Ebene beschreiben die Ereigniszeile.
`payload`, und `payload.kind` ist das einzige `kind` Feld auf der Ereignislast.
Die lokale Persistenzdarstellung ist ein internes Detail und wird nie
erscheint in dieser öffentlichen Veranstaltungsform.

Erstellen, Aktualisieren, Lesen und Aktionsereignisse tragen den gleichen vollständigen Objekt-Snapshot wie
die entsprechende Messenger REST-Antwort, plus `payload.kind`.
Die Ereignisse sind minimal:

- `stream.deleted`, `folder.deleted` und `folder_item.deleted`: `kind`, `uuid`;
- `topic.deleted`: `kind`, `uuid`, `stream_uuid`;
- `message.deleted`: `kind`, `uuid`, `stream_uuid`, `topic_uuid`,
  `author_uuid`, `source_name` und `source`.

Reaktionsänderungen emittieren `message_reaction.created`,
`message_reaction.updated` oder `message_reaction.deleted` für den als Interessent dienenden Benutzer.
Das Backend gibt auch `message.updated` Snapshots mit dem aktualisierten Aggregat aus
`reactions` Karte und anhaltende begrenzte `reaction_users` Karte für Benutzer, die sehen können
Jeder vorhandene `reaction_users` Schlüssel ist eine vollständige Benutzerliste UUID
Der Client ersetzt die gesamte Karte auf
jeder vollständige Nachrichtssnapshot; ein leerer Objekt oder fehlender Schlüssel bedeutet nur zählen
und muss jede zuvor zwischengespeicherte Liste entfernen.

Batch-Stream-Bindungserstellung verwendet `payload.items`.
`message.read`, `topic.read` oder `stream.read` und weiterhin Aggregate emittieren
`topic.updated`, `stream.updated` und `folder.updated` Ereignisse, wenn sie nicht gelesen werden
Die Batch-Nachricht lautet "Verwenden `messages.read` mit
`message_uuids`.

Unterstützte Werte sind:

| `object_type` | Maßnahmen |
| --- | --- |
| `message` | `created`, `updated`, `deleted`, `read` |
| `message_reaction` | `created`, `updated`, `deleted` |
| `stream` | `created`, `updated`, `deleted`, `read` |
| `stream_binding` | `created`, `updated`, `deleted` |
| `topic` | `created`, `updated`, `deleted`, `read` |
| `user` | `updated` |
| `folder` | `created`, `updated`, `deleted` |
| `folder_item` | `deleted` |
| `file` | `created`, `updated`, `deleted` |

## Aufhol- und Kursortenbearbeitung

Nachgefragt werden Ereignisse, die streng neueren als die letzte erfolgreich angewandte Epoche sind:

```http
GET /api/workspace/v1/events/?epoch_version%3E=<last_epoch_version>&epoch_generation=<generation>&page_limit=500
```

`epoch_version>` ist streng. Verarbeiten Ereignisse in aufsteigender Reihenfolge und
Cursor nur nach Anwendung eines Ereignisses auf jeden betroffenen Kundenladen.

`GET /api/workspace/v1/epoch/` gibt die Generation, die aktuelle Epoche und
für den aktuellen IAM Benutzer und Projekt sichtbarer Rückhalteboden:

```json
{
  "epoch_version": 124,
  "epoch_generation": "781203",
  "current_epoch_version": 124,
  "minimum_epoch_version": 37
}
```

Cursor-Regeln:

- `(epoch_generation, epoch_version)` als einen unteilbaren Cursor behandeln und senden
  die Erzeugung mit jedem nicht nullartigen REST oder Websocket-Wiederholungsdatum;
- Vergessen Sie Ereignisse, deren Epoche kleiner oder gleich dem eingeschlossenen Cursor ist;
- die Abweichung von REST im Vergleich zu einem Vermutungszustand der Ressource zu beheben;
- Partition oder Löschung des Cursors, wenn der Benutzer oder das Projekt IAM wechselt;
- niemals in der Reihenfolge nach Ereignis UUID oder Zeitstempel;
- Seitenaufzeichnung, bis der Server keine weiteren Seitenmarker mehr zurückgibt.
- HTTP 410 `EventsCursorExpiredError` / `error=epoch_pruned` als Cache behandeln
  Die Daten werden mit dem Datenverzeichnis verknüpft.
  Der Server speichert Ereignisse für ein konfigurierbares Intervall von 72 Stunden
  Standardmäßig und dieser Reset löscht niemals Nachrichten, Dateien oder den Domänenzustand.

## Websocket-Zustellung

Nach Annahme der Verbindung sendet der Server verpasste Ereignisse, die neueren als die
Sparte Cursor, dann sendet
`{"type":"ready","epoch_generation":"...","epoch_version":124}`- Keine Live .
Das Benutzer-Benachrichtigungsgate bleibt geschlossen, bis
`ready`; Nachrichten müssen den Status ohne Benachrichtigung aktualisieren.
Websocket-Ping-Steuerungsrahmen, nicht für Anwendungen JSON `hello`, `ping`, `pong` oder
`ack` Nachrichten. Cursor expiry sendet den eingegebenen Fehlerkörper und schließt mit `4410`.

Empfohlene Kundenzufuhr:

1. Das engagierte Cursor-Paar laden oder Cold Epoch `0` ohne Generation verwenden.
2. REST bis keine weiteren Ereignisse mehr zurückgegeben werden.
3. Öffnen Sie die Websocket mit dem neuesten Cursor-Paar.
4. Die Daten werden in einem System mit einem Netzwerk von REST und Websocket-Nachrichten über den gleichen idempotenten Dispatcher übertragen.
5. Halten Sie Benachrichtigungen deaktiviert, bis der Websocket `ready` Frame, dann aktivieren
   Live-Benachrichtigungen.
6. Entduplizieren durch `(epoch_generation, epoch_version)`.
7. Nach dem Schließen, wiederholen Sie den Aufholzugang und verbinden Sie sich wieder mit der Rückseite.

Die Phase des verpassten Events des Websockets schließt das Rennen zwischen der letzten REST Seite und
Die Entfalten sind immer noch obligatorisch, da zweideutige Fehler möglicherweise
Wiederholung eines bereits angewandten Ereignisses.

## Versand der Benutzerbestimmungen

Erst durch die obersten Ebenen `object_type` und `action`, dann durch `payload.kind`
Unbekannte Schemaversionen oder Ereignisse
Die Werte sollten protokolliert und übersprungen werden, ohne die Echtzeitschleife zu brechen.

| `object_type` | Primärer Nutzeroberflächenspeicher oder -effekt |
| --- | --- |
| `message`, `message_reaction` | Zeitleiste, Reaktionen, nicht gelesener Zustand |
| `stream`, `stream_binding`, `topic` | Navigation, Mitgliedschaft, Thema-Zustand |
| `folder`, `folder_item` | Ordnernavigation und Abzeichen |
| `user` | Gemeinsame Identität und Präsenz-Cache |
| `file` | Dateimetadaten und geschützte/public Blob-Cache-Invalidierung |

`stream.deleted` für den entferntem Teilnehmer widerruft den gesamten Stream:
alle geschützten Blobs, deren zwischengespeicherte Metadaten sofort `stream_uuid` enthalten.
Die übrigen Mitglieder erhalten `stream_binding.deleted`; verbindliche Änderungen erzeugen
`stream_binding.updated`. Ein Cursor-Gap-Fehler löscht alle abgeleiteten geschützten Blob
Caches vor dem Nachladen der Snapshots.

Der Inhalt der Nachricht V1 verwendet die Form der Markierung der Nutzlast:

```json
{ "kind": "markdown", "content": "Hello" }
```

Anwesenheit wird über die REST aktualisiert
`users/{uuid}/actions/presence/invoke` Aktion. Der Arbeiter markiert veraltete Benutzer
Offline und sendet `user.updated` mit dem vollständigen öffentlichen Nutzer-Snapshot.
