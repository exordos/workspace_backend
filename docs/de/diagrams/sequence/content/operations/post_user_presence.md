# `POST /api/workspace/v1/users/{user_uuid}/actions/presence/invoke`


Allgemeine Ziel-Invariante der Zuverlässigkeit: Jedes immutable outbox-Ereignis führt genau eine immutable typed task mit einem einzigartigen `outbox_event_uuid`; coalescing fehlt. Task speichert den tatsächlichen exact scope key, verwendet lease/fencing, retry/backoff, max attempts/DLQ, reaper und idepotent effect guard. Topic scope wird nur für placement/message-binding work angewendet; shared rows erhalten keinen impliziten Fallback auf topic.

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge-Diagramme](../../README.md) · [Inhalt und Benutzer Workspace](../README.md)

Status: Ziel-Spezifikation der Operation, die zuerst in der Dokumentation erarbeitet wurde.
unverändert bleibt und ist das Normativrecht in [`workspace_api.md`](../../../../workspace_api.md).
Diese Datei beschreibt die Zielgrenzen von Transaktionen und Projektionen; sie ist nicht
Produktionscode, Migration SQL oder neuer Endpunkt.

![Abfolge Diagramm](diagrams/post_user_presence.svg)

[Ausgangsgestalt , die bearbeitet werden kann PlantUML](diagrams/post_user_presence.puml)

## Zuordnung und öffentlicher Vertrag

Erneuern Sie die eigene Anwesenheit und Aktivitätsmarkierung des authentifizierten Benutzers.

Authentifizierung: Token Bearer IAM; `project_id` und aktuell `user_uuid` werden aus dem Kontext genommen IAM.

## Anfrageweg und -parameter

| Lage | Name | Typ / Regel |
| --- | --- | --- |
| Weg | `user_uuid` | muss mit dem UUID authentifizierten Benutzer übereinstimmen |

Die Sammlungspagination, wo sie vorgesehen ist, behält den aktuellen Vertrag `page_limit` und UUID
`page_marker` und erhält `X-Pagination-Limit`, und
`X-Pagination-Marker` Nur wenn die nächste Seite vorhanden ist.

## Abfrage-Body

```json
{
  "status": "active",
  "emoji": "coffee",
  "text": "Focusing"
}
```

## Eine erfolgreiche Antwort

`200`

```json
{
  "uuid": "11111111-1111-1111-1111-111111111111",
  "username": "admin",
  "source": "iam",
  "status": "active",
  "status_emoji": "coffee",
  "status_text": "Focusing",
  "first_name": "Workspace",
  "last_name": "Administrator",
  "email": "admin@example.com",
  "avatar": "urn:gravatar:0123456789abcdef0123456789abcdef",
  "last_ping_at": "2026-07-17T08:00:00Z",
  "created_at": "2026-07-01T08:00:00Z",
  "updated_at": "2026-07-17T08:00:00Z"
}
```



## Fehler und Autorierung

Nur die eigene UUID des authentifizierten Benutzers wird akzeptiert. `status` akzeptiert `active|idle|offline|do_not_disturb`; `emoji` und `text` können übersprungen werden, um die vorherigen Werte zu behalten, oder als `null` ausgewiesen werden, um zu klären.

Allgemeine Antwortform bei Validierungsfehlern:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

## Zielgrenze RestAlchemy

```python
from restalchemy.api import controllers as ra_controllers
from restalchemy.api import resources as ra_resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUser(models.ModelWithUUID, models.ModelWithTimestamp,
                    orm.SQLStorableMixin):
    __tablename__ = "messenger_users"
    username = properties.property(types.String(min_length=1, max_length=128), required=True)
    source = properties.property(types.Enum(["iam", "zulip"]), default="iam")
    status = properties.property(types.Enum(["active", "idle", "offline", "do_not_disturb"]))
    status_emoji = properties.property(types.AllowNone(types.String(max_length=64)))
    status_text = properties.property(types.AllowNone(types.String(max_length=256)))
    avatar = properties.property(types.String(max_length=2048), required=True)


class WorkspaceUserController(ra_controllers.BaseResourceControllerPaginated):
    __resource__ = ra_resources.ResourceByRAModel(
        model_class=WorkspaceUser,
        convert_underscore=False,
        process_filters=True,
    )
    # Narrow own-user IAM refresh and presence/avatar actions preserve the API.
```

Jeder öffentliche Verweis auf die Entität wird als skalar UUID-Eigenschaft RestAlchemy erklärt, nicht `relationship` (die sich als URI serialieren würde). Der entsprechende physische Spalte `*_uuid`  ein indexierter externer Schlüssel mit einer eindeutig gewählten Verweisungsaktion. Daher hält der öffentliche JSON UUID unverändert.

`WorkspaceUser` — öffentliche UUID-ähnliche Links des Providers bleiben Skalierfelder im sanitierten Behälter des Providers; physische Links  sind FK-indexiert. Identitätsfelder, die zu IAM gehören, sind Browseranfragen nur für Lesen zugänglich.

## Synchronisierter Weg API

1. Überprüfen Sie Ihre eigene UUID.
2. Überprüfen Sie `status` und optional.
3. Das Feld `status` aktualisieren und festlegen `last_ping_at=now`.
4. Ein unveränderbares Eintrag in die Outbox hinzufügen `user.presence_changed`.
5. Transaktion festhalten und vollständige Benutzerbilder zurückgeben.

## Outbox, Typisierte Aufgaben, Worker und Echtzeitarbeit

API Aktive Benutzer werden nicht gleichzeitig aktualisiert.`offline`die eigene Verantwortung des Vorstands.

Ein separates immutable `delivery_snapshot_event` mit exact user scope liest
Der letzte Canon-Benutzer wird automatisch erstellt.
`user.updated` mit effect guard auf `outbox_event_uuid`.
der Dispepter sendet, wiederholt oder wiederholt; Worker WebSocket-
keine Verbindungen besitzt.

## Idempotenz, Schlüssel und Rennen

Die Kanonische Zeile des Benutzers serialisiert konkurrierende Anwesenheitsaufzeichnungen; überwindet den letzten festgelegten Wert `status`. Jedes Outbox-Ereignis erzeugt eine separate immutable task; der idempotent-Verarbeiter liest den aktuellen Ausgangszustand, so dass die Wiederholung derselben task das veraltete Bild nicht für das aktuelle erzeugt.

Für die mapped Zulip identity Bridge liefert sie Workspace-Origin und
Zulip-origin presence/status changes. Die letzte bestätigte Änderung gewinnt;
`origin`/`causation_uuid` Sie unterdrücken nur ihr eigenes Echo und setzen keine Prioritäten.
Public request/response shape wird nicht geändert.

## Sichtbarkeit für den Client

Der aktuelle Client erhält sofort den aktualisierten Canonischen Benutzer. Andere Kunden erhalten eine vollständige Aufnahme `user.updated` nach dem angenommenen Projektionsverzögerung/Dispatcherisierung.

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge-Diagramme](../../README.md) · [Inhalt und Benutzer Workspace](../README.md)
