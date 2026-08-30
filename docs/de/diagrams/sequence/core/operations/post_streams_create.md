# POST /api/workspace/v1/messenger/streams/


Allgemeine Ziel-Invariante der Zuverlässigkeit: Jedes immutable outbox-Ereignis führt genau eine immutable typed task mit einem einzigartigen `outbox_event_uuid`; coalescing fehlt. Task speichert den tatsächlichen exact scope key, verwendet lease/fencing, retry/backoff, max attempts/DLQ, reaper und idepotent effect guard. Topic scope wird nur für placement/message-binding work angewendet; shared rows erhalten keinen impliziten Fallback auf topic.

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge](../../README.md) · [Abschnitt Core Messenger](../README.md)

## Status und Grenze des laufenden Vertrags

Die Methode, der Weg, die öffentliche JSON und die Autorisierung folgen dem aktuellen Vertrag von [`workspace_api.md`](../../../../workspace_api.md); bounded pagination und asynchrone visibility folgen dem separat angenommenen target compatibility ADR.

![Abfolge Diagramm](diagrams/post_streams_create.svg)

Ausgangsgestalt , die bearbeitet werden kann: [`post_streams_create.puml`](diagrams/post_streams_create.puml).

## Die Operation

**Methode und Weg:** `POST /api/workspace/v1/messenger/streams/`

**Verwendung:** Erstellen eines kanonischen Flusses, einer Eigentümerbindung und eines Standardthemas; der ID des Direktflusses wird idempotent verarbeitet.

## Öffentliche Anfrage

Normaler Fluss:

```json
{
  "name": "Инженерия",
  "description": "Инженерное пространство",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false
}
```

Direktstrom:

```json
{
  "name": "Прямой поток",
  "description": "Приватное пространство",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "33333333-3333-3333-3333-333333333333"
}
```

Der Fluss mit sich selbst:

```json
{
  "name": "Личные заметки",
  "description": "",
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "direct_user_uuid": "11111111-1111-1111-1111-111111111111"
}
```

## Eine erfolgreiche öffentliche Antwort

Neue Ressource: HTTP `201`; bestehendes determinantes Paar des direkten Flusses: HTTP `200`.:

```json
{
  "uuid": "64184b31-e43c-5b0d-95f8-b7b50bdc03c9",
  "name": "Личные заметки",
  "description": "",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "owner": "11111111-1111-1111-1111-111111111111",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "role": "owner",
  "notification_mode": "all_messages",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "invite_only": false,
  "announce": false,
  "direct_user_uuid": "11111111-1111-1111-1111-111111111111",
  "private": true,
  "is_archived": false,
  "color": 3368601,
  "default_topic_uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "created_at": "2026-06-22T09:00:00Z",
  "updated_at": "2026-06-22T09:00:00Z"
}
```

## Öffentliche Fehler

Bewerber-Token IAM und Projektbereich erforderlich. Falsch UUID oder Anfrage-Body wird HTTP `400` gegeben; fehlende oder nicht verfügbare Ressource in diesem Bereich  `404`. Standarddokumenterter Validierungsfehler-Body:

```json
{
  "type": "ValidationErrorException",
  "code": 400,
  "message": "Validation error occurred."
}
```

Identitätskonflikt oder Quelle des Direktflusses und Änderung der Mitgliedschaft des Direktflusses geben `400`; das Löschen des Flusses mit sich selbst gibt auch `400`.

## Zielgrenze RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUserStream(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_streams_v1"

    owner = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    direct_user_uuid = properties.property(
        types.AllowNone(types.UUID()), default=None, read_only=True,
    )
    last_message_uuid = properties.property(types.UUID(), read_only=True)
    default_topic_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceStreamController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserStream, convert_underscore=False, process_filters=True,
    )
```

Die öffentlichen Entitätsverweise sind mit den skalaren Eigenschaften `types.UUID()` und nicht mit den Beziehungen RestAlchemy, die in URI serialisiert werden. Die physischen Spalten `*_uuid` bleiben indexierte Außenschlüssel mit eindeutig ausgewählten Verweisungsaktionen. Das öffentliche Feld `owner` ist die Eigenschaft UUID; das physische Feld `owner_uuid`  der indexierte Außenschlüssel des Benutzers. USER_STREAM_BINDING speichert die vorbereiteten Flussstufemessern..

## Synchrone Transaktion

1. Erstellen Sie eine bestimmte Paare der direkten Strom; jeder Wert `direct_user_uuid` zwingend festlegt `private=true`.
2. Stellt STREAM und TOPIC als Standard ein.
3. Einzigartige Eigentümer-Verknüpfungen an den Stream und das Thema einfügen; für den Stream mit sich selbst nur einen Benutzer einfügen.
4. Einen unveränderlichen Eintrag hinzufügen transactional outbox.

Der betroffene Zustand ist STREAM, TOPIC, USER_STREAM_BINDING, USER_TOPIC_BINDING und transactional outbox.

## Typisierte Aufgaben und Hintergrund-Ausfüllung

Aufgaben: `topic_membership_policy_rebuild` und genaue `folder_projection`/`read_counters` für betroffene Container.

Die Hintergrund-Ausübende erstellen die verbleibenden fertigen Projektionen von Containern und Ereignissen; der Stream hat keinen zweiten Teilnehmer mit sich. Die anschließende Verteilung von Fan-Out-Nachrichten erzeugt keine zusätzliche Bindung der Benutzernachricht. Verschiedene Themen können innerhalb eines anpassbaren Limites parallel verarbeitet werden; innerhalb eines beschäftigten Themas erhalten kanonische Nachrichten Priorität nach `MESSAGE.created_at DESC`, wobei älteres Werk mit der Zeit ebenfalls vorangetrieben wird.

## Öffentliche Veranstaltungen und WebSocket

Teilnehmer werden `stream.created` und Aktualisierungen der Ordner über den Verwalter gesendet.

## Idempotenz, Rennen und zeitliche Merkmale, die dem Kunden sichtbar sind

Die Schlüsselpaare und die einzigartigen Bindungen machen die konkurrierende Wiederholung unendlich potenziell..

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge](../../README.md) · [Abschnitt Core Messenger](../README.md)
