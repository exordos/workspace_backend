# POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge](../../README.md) · [Abschnitt Core Messenger](../README.md)

## Status und Grenze des laufenden Vertrags

Die Methode, der Weg, die öffentliche JSON und die Autorisierung folgen dem aktuellen Vertrag von [`workspace_api.md`](../../../../workspace_api.md); bounded pagination und asynchrone visibility folgen dem separat angenommenen target compatibility ADR.

![Abfolge Diagramm](diagrams/post_topic_read_action.svg)

Ausgangsgestalt , die bearbeitet werden kann: [`post_topic_read_action.puml`](diagrams/post_topic_read_action.puml).

## Die Operation

**Methode und Weg:** `POST /api/workspace/v1/messenger/stream_topics/{topic_uuid}/actions/read/invoke`

**Zweck:** Die Themenmeldungen für den aktuellen Benutzer als gelesen zu markieren.

## Öffentliche Anfrage

Ohne Körper JSON.

## Eine erfolgreiche öffentliche Antwort

HTTP `200`:

```json
{
  "uuid": "4ec0b996-b778-45f8-8ef4-ef863be0c047",
  "project_id": "22222222-2222-2222-2222-222222222222",
  "name": "Релизы",
  "stream_uuid": "75309057-419c-4b12-a7c1-3932429ec4a6",
  "user_uuid": "11111111-1111-1111-1111-111111111111",
  "color": 4491468,
  "last_message_uuid": "a93dca35-3061-4748-bda4-7f6f8c660ea5",
  "unread_count": 0,
  "active_unread_count": 0,
  "passive_unread_count": 0,
  "is_default": false,
  "is_done": false,
  "notification_mode": "default",
  "summary": null,
  "summary_last_message_uuid": null,
  "summary_has_new_messages": null,
  "summary_enabled": true,
  "summary_system_prompt": null,
  "summary_reasoning_effort": null,
  "source_name": "native",
  "source": {
    "kind": "native"
  },
  "provider": null,
  "delivery": null,
  "created_at": "2026-06-22T09:10:00Z",
  "updated_at": "2026-06-22T10:20:00Z"
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

## Zielgrenze RestAlchemy

```python
from restalchemy.api import controllers
from restalchemy.api import resources
from restalchemy.dm import models
from restalchemy.dm import properties
from restalchemy.dm import types
from restalchemy.storage.sql import orm


class WorkspaceUserTopic(
    models.ModelWithUUID,
    models.ModelWithProject,
    orm.SQLStorableMixin,
):
    __tablename__ = "messenger_api_user_topics_v1"

    stream_uuid = properties.property(types.UUID(), read_only=True)
    user_uuid = properties.property(types.UUID(), read_only=True)
    last_message_uuid = properties.property(types.UUID(), read_only=True)
    summary_last_message_uuid = properties.property(types.UUID(), read_only=True)


class WorkspaceStreamTopicController(controllers.BaseResourceControllerPaginated):
    __resource__ = resources.ResourceByRAModel(
        WorkspaceUserTopic, convert_underscore=False, process_filters=True,
    )
```

Die öffentlichen Verweise auf Wesen sind skalare Eigenschaften .`types.UUID()`- nicht Beziehungen .RestAlchemy, die inURI- physische Spalten`*_uuid`bleiben als indexierte externe Schlüssel mit eindeutig ausgewählten Verweisungsabläufen.TOPICist ein kanonisches Wesen; einzigartigUSER_TOPIC_BINDINGSie sorgt für Sichtbarkeit, persönlichen Status und Themen-Zähler.

## Synchrone Transaktion

1. Berechtigung des Themenbereichs.
2. Lesemerkersatz festlegen USER_MESSAGE_STATE.
3. Fügen Sie für jede Ausgabe ein separates immutable outbox-Event hinzu task
   `user-topic`/`user-stream`/`user-folder`.

Betroffener Zustand: Anwendbar TOPIC, USER_TOPIC_BINDING, USER_MESSAGE_STATE und transactional outbox; Zähler befinden sich nur in Containerbindungen.

## Typisierte Aufgaben und Hintergrund-Ausfüllung

Aufgaben: einzelne immutable `read_counters` tasks für exact scopes
`user-topic`/`user-stream` und `folder_projection` für `user-folder`; coalescing
nicht vorhanden.

Fenced owners exact scopes die entsprechenden shared bindings; topic
worker Atomic Delta verlangt genau-einmal eine Wache.
`outbox_event_uuid`, Ansonsten wird recompute ausgeführt/write. Tasks verwenden
retry/backoff, DLQ/reaper.

## Öffentliche Veranstaltungen und WebSocket

`topic.read` Der Verwalter liefert die festgelegten Linien.

## Idempotenz, Rennen und zeitliche Merkmale, die dem Kunden sichtbar sind

Die Operation ist idempotent; im Clientpfad gibt es keine Aggregation. Der Anrufer sieht den Zustand sofort, die abgeleiteten Projektionen und Ereignisse  asynchron.

[← Hauptindex der Dokumentation](../../../../index.md) · [Index der Abfolge](../../README.md) · [Abschnitt Core Messenger](../README.md)
