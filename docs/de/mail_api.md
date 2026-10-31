# Workspace Mail API

Dies ist der aktive browserseitige Mail-Vertrag. Er erweitert den einheitlichen
Dienst [`workspace-api`](workspace_api.md) unter `/api/workspace/v1/mail/` und
führt keinen zweiten öffentlichen API-Prozess ein.

## Architektur und Umfang

- PostgreSQL ist kanonisch für `m_mail_folders`, `m_mail_messages` und
  `mail_outgoing_intents`.
- Anhänge verwenden den Workspace-Dateispeicher und werden durch `urn:file:`
  referenziert; die Bytes bleiben in S3.
- Mail-Ressourcen verwenden den vorhandenen IAM-Projekt- und Benutzer-Scope,
  dauerhafte Workspace-Ereignisse, REST-Backfill und WebSocket-Zustellung.
- Eine getrennte Mail-Runtime kann `mail_outgoing_intents` verarbeiten und
  Provider-Änderungen in die kanonischen Tabellen projizieren. Diese Runtime
  und echte SMTP/IMAP-Zustellung gehören nicht zu dieser Backend-Änderung.
- `/api/workspace/v1/calendar/` und öffentliche provider-spezifische Routen
  bleiben abwesend.

## Ressourcen und Aktionen

| Methode | Pfad | Zweck |
| --- | --- | --- |
| `GET`, `POST` | `/api/workspace/v1/mail/folders/` | Eigene Ordner auflisten oder einen Ordner erstellen. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/folders/{folder_uuid}` | Einen leeren eigenen Ordner lesen, ändern oder weich löschen. |
| `GET`, `POST` | `/api/workspace/v1/mail/messages/` | Eigene Nachrichten auflisten oder einen lokalen Entwurf erstellen. |
| `GET`, `PUT`, `DELETE` | `/api/workspace/v1/mail/messages/{message_uuid}` | Eine eigene Nachricht lesen, ändern oder weich löschen. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/move/invoke` | Eine Nachricht zwischen eigenen Ordnern desselben Kontos verschieben. |
| `POST` | `/api/workspace/v1/mail/messages/{message_uuid}/actions/send/invoke` | Einen Entwurf atomar einreihen und genau einen dauerhaften Intent anlegen. |

Das generierte Workspace-OpenAPI ist maßgeblich für vollständige Feldschemata,
Filterparameter, Statuscodes und Antwortkörper.

## Entwurfs- und Sendeablauf

1. Einen Ordner erstellen oder auswählen. Falls `external_account_uuid` gesetzt
   ist, muss es ein aktiviertes eigenes `mail`-Konto im IAM-Projekt sein.
2. Eine Nachricht mit `folder_uuid` erstellen; sie beginnt mit `draft=true` und
   `delivery_status=draft`.
3. Entwurfsinhalt ändern oder sichtbare Workspace-Dateien anhängen.
4. `send` aufrufen. Dieselbe Datenbanktransaktion prüft Empfänger, Inhalt,
   Konto und Anhangsichtbarkeit, erzeugt eine `mail_outgoing_intents`-Zeile,
   setzt `delivery_status=queued` und emittiert `mail.message.updated`.
5. `sent_at` bleibt leer, bis eine Mail-Runtime die Provider-Zustellung
   bestätigt. Eine eingereihte Nachricht kann nicht öffentlich gelöscht werden.

Die Sendeaktion ist kein Nachweis externer Zustellung. Bis eine Mail-Runtime
ein Endergebnis meldet, ist nur die dauerhafte Einreihung bestätigt.

## Anhänge

Ein privater Mail-Anhang wird über den vorhandenen Multipart-Endpunkt geladen:

```text
POST /api/workspace/v1/messenger/files/
Content-Type: multipart/form-data

file=<bytes>
acl={"mode":"owner"}
```

Die zurückgegebene UUID wird als `urn:file:<file_uuid>` in `attachment_urns`
verwendet. Die API akzeptiert nur Dateien, die für den aktuellen IAM-Benutzer
im aktuellen Projekt sichtbar sind. `acl={"mode":"owner"}` erstellt eine
private Datei ohne Stream; vorhandene stream-sichtbare und öffentliche
Workspace-Dateien können ebenfalls referenziert werden.

## Realtime-Ereignisse

| Kind | Objekttyp | Auslöser |
| --- | --- | --- |
| `mail.folder.created` | `mail_folder` | Ordnererstellung. |
| `mail.folder.updated` | `mail_folder` | Änderung der Ordnermetadaten. |
| `mail.folder.deleted` | `mail_folder` | Weiches Löschen eines Ordners. |
| `mail.message.created` | `mail_message` | Erstellung eines Entwurfs oder einer Projektion. |
| `mail.message.updated` | `mail_message` | Inhalt-, Status-, Verschiebe- oder Queue-Änderung. |
| `mail.message.deleted` | `mail_message` | Weiches Löschen einer Nachricht. |

Diese Kinds verwenden denselben Cursor `/api/workspace/v1/events/` und Stream
`/api/workspace/v1/events/ws` wie Messenger-Ressourcen.

## Abnahmegrenze

Die Backend-Abnahme umfasst Migration Upgrade/Downgrade, OpenAPI-Generierung,
Owner-/Projekt-Isolation, Owner-Dateisichtbarkeit, dauerhafte Intent-Idempotenz
und Ereignistests. Sie belegt keine bereitgestellte Mail-Runtime, keinen
SMTP/IMAP-Zugriff, keine Provider-Abnahme und keine zugestellte Kopie in einem
externen Sent-Ordner.
