# Messenger Regressionsversuchsplan

Dieser Plan überprüft, dass PostgreSQL die kanonische Messenger Speicher ist, während die
Das aktuelle Browser-Gesichts API und S3 Dateiverhalten bleibt unverändert.
Definition von Wiederaufbau, Wiederherstellung, Skalierung und Lastakzeptanz.

## Annahmevorschriften

- REST Pfade, Anforderungs- und Antwortkörper, Statuscodes, Paginierung, Filter,
  Sichtbarkeitsregeln und Ereignislast entsprechen der festgelegten Messenger
  Ausgangswerte.
- `/api/workspace/v1/messenger/**` bleibt der öffentliche Messenger REST Namespace;
  Die alten `/api/messenger/**` Pfade bleiben abwesend.
- REST und Websocket-Ereignisse sind gleich nach JSON Decodierung und bleiben Scope zu
  der authentifizierte IAM Benutzer und Projekt.
- PostgreSQL speichert die kanonischen Messenger Ressourcen, den Status, die Ereignisse und den Anbieter
  Die Ausführung liest und schreibt nicht mit SMTP, IMAP, Maildir, Exim oder
  - Die Taube.
- Dateien, Metadaten-Seitenwagen und binärer Zugriff weiterhin mit S3 kompatibel
  Nachrichten enthalten nur autorisierte URNs.
- Provider-Runtimes tauschen gewöhnliche Messenger Ressourcen und Befehle durch
  der private Workspace Anbieter API und sich niemals an den Workspace
  Datenbank.
- Nur Ereignisprotokolle werden für das konfigurierte Intervall, 72 Stunden, von
  Nachrichten, Dateien, Streams, Themen, Einstellungen und Anbieter-Mapping
  Die Daten bleiben bis zu ihrer normalen Lebensdauer kanonisch.

## Prüfumgebung

- Isolierte Workspace Backend mit einer frischen PostgreSQL Datenbank und alle
  Die Kommission hat die
- S3-kompatible Objektspeicher für den Prüfvorgang.
- Zwei IAM Benutzer in einem Projekt und ein IAM Benutzer in einem anderen Projekt für die
  Mindestfunktionslauf; das volle Lastprofil ist in PostgreSQL definiert
  Kanonischen Plan.
- Eine unabhängig einsetzbare Zulip Anbieterlaufzeit und eine dedizierte Zulip
  Testbereich für Anbieter-Szenarien.
- Ein sichtbares globales Playwright MCP Fenster mit dem realen `cassi` Konto für
  Sichtbare Annahme.

## API und Routing

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-API-001 | Nach `/api/workspace/v1/messenger` jede Sammlung und Aktion Messenger anfordern. | Methoden, Statuscodes, Schemata, Paginierungen und Aktionspfade entsprechen der Basislinie. |
| MSG-API-002 | Anfordern Sie `/api/workspace/v1/messenger/server_settings` mit und ohne Schrägstrich, wenden Sie dann das `realm_icon`-Ziel ohne Anmeldeinformationen aus und holen Sie es ab. | Die öffentliche Antwort enthält `urn:url:<realm>/logo-512x512.png`, und das verpackte Organisationsemblem wird anonym als `image/png` zurückgegeben. |
| MSG-API-003 | Verlangen Sie alte `/api/messenger/**` Pfade und browserunzugängliche Anbieter-, E-Mail- oder Kalenderpfade. | Nginx oder die Anwendung gibt `404` zurück und leitet nicht weiter. |
| MSG-API-004 | Vergleichen Sie die erzeugten OpenAPI mit den eingefrorenen Messenger Ausgangswerten. | Die vorhandenen Ressourcen, erforderlichen Werte, Mehrteilsanforderungen, Aktionen und Antwortüberschriften bleiben unverändert. |
| MSG-API-005 | Überprüfen Sie den durch den Browser sichtbaren Datenverkehr während der Verwendung von Messenger. | Es werden keine Datenbank-, Provider-Service-, SMTP, IMAP-, Maildir-, Mail- oder Kalenderimplementierungsdetails aufgedeckt. |

## Kanäle, Themen und Ordner

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-NATIVE-001 | Erstellen Sie einen öffentlichen Kanal. | Eigentümerbindung, Standardthema, Ordner und Ereignisse erstellen werden einmal erzeugt. |
| MSG-NATIVE-002 | Erstellen Sie direkt und Gruppenkanäle wiederholt. | Die Schöpfung ist idempotent und private Bindungen bleiben korrekt. |
| MSG-NATIVE-003 | Umbenennen, archivieren, unarchivieren und löschen Sie einen Kanal. | Berechtigte Benutzer erhalten das festgelegte Update oder löschen Ereignisse. |
| MSG-NATIVE-004 | Benutzer mit jeder unterstützten Rolle hinzufügen und entfernen. | Sichtbarkeit, Bindungen, Zugriff auf Dateien, Ordner und Ereignisse werden nur für betroffene Benutzer aktualisiert. |
| MSG-NATIVE-005 | Erstellen, umbenennen, beenden, standardmäßig einstellen, stummen, folgen, lesen und löschen. | Der Status des Themas, die nicht gelesenen Zähler und die Ereignisse entsprechen der Basislinie. |
| MSG-NATIVE-006 | Verknüpfen und entknüpfen Sie Ordner-Elemente und verwalten Sie benutzerdefinierte Ordner. | Bestellungen, materialisierte Gegenstände, nicht gelesenen Zählern und Ordnerveranstaltungen bleiben korrekt. |

## Nachrichten und Reaktionen

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-DB-001 | Senden Sie Nachrichten an explizite und Standardthemen. | Die kanonische PostgreSQL Nachricht, Flaggen, gemeinsame Zielgruppe, kompakte Nachricht/topic/stream Sendungen und öffentliche Antwort verpflichten sich atomar ohne per-Empfänger kanonische Ereigniszeilen. |
| MSG-DB-002 | API, Ereignis- und Mitarbeiterdienste neu starten, bevor Nachrichten gelesen werden. | Nachrichteninhalte, Reihenfolge, Flaggen und Kennungen bleiben über die gleiche API verfügbar. |
| MSG-DB-003 | Bearbeiten und löschen einer Nachricht. | Kanonische Zeilen, Grabsteinverhalten, Reaktionen, Ereignisse und öffentliche Momentaufnahmen konvergieren transaktionstechnisch. |
| MSG-DB-004 | Markieren Sie eine Nachricht gelesen, lesen Sie eine Nachricht, lesen Sie ein Thema und lesen Sie einen Kanal. | Nur der ausgewählte Bereich ändert sich; Zähler und Ereignisse sind korrekt und idempotent. |
| MSG-DB-005 | Erstellen, aktualisieren und löschen von Reaktionen als verschiedene Benutzer. | Benutzer-Scoping, aggregierte Reaktionen und Nachrichten-Update-Ereignisse bleiben korrekt. |
| MSG-DB-006 | Versuchen Sie, ohne Mitgliedschaft oder mit einem Token eines anderen Projekts zu handeln. | IAM Autorisierung lehnt die Anfrage ab und der kanonische Zustand bleibt unverändert. |
| MSG-DB-007 | Die gleiche erneut ausprobierbare Anfrage nach einem zweideutigen Antwortfehler einreichen. | Die Idempotency wird beibehalten und keine doppelten, für den Benutzer sichtbaren Nachrichten oder Ereignisse werden erstellt. |

## Entwürfe

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-DRAFT-001 | Erstellen Sie mehrere Entwürfe für einen Stream/topic und versuchen Sie einen UUID mit identischen und veränderten kanonischen Feldern erneut. | Alle unterschiedlichen UUIDs bestehen; der identische Wiederholungsversuch gibt die ursprüngliche Revision ohne weitere Mutation zurück, während veränderte Felder `409` zurückgeben. |
| MSG-DRAFT-002 | Lesen und Paginieren von Entwürfen in beiden `updated_at` Richtungen mit Stream/topic Filtern und einem UUID Marker. | Die Reihenfolge ist stabil um `(updated_at, uuid)`; Marker können nicht über den Eigentümer, das Projekt oder den Filterbereich gehen. |
| MSG-DRAFT-003 | Aktualisieren und löschen mit fehlenden, aktuellen, veralteten, schwachen und missgebildeten `If-Match` Werten. | Fehlende Rücksendungen`428`; nur die exakte starke Revision gelingt; veraltete oder ungültige Werte werden zurückgegeben`412`mit dem aktuellen Snapshot undETag. |
| MSG-DRAFT-004 | Versuchen Sie, einen Entwurf CRUD als anderer Benutzer /project ohne Stream-Mitgliedschaft oder mit einem Thema aus einem anderen Stream zu erstellen. | Der Zugriff wird abgelehnt, ohne den Entwurf eines anderen Eigentümers zu entlarven oder zu verändern. |
| MSG-DRAFT-005 | Entfernen Sie die Eigentümerbindung, löschen Sie das Thema und löschen Sie den Stream, solange es Entwürfe gibt. | PostgreSQL Kaskaden löschen alle betroffenen Entwürfe ohne Grabsteine oder Ereignisse. |
| MSG-DRAFT-006 | Beobachten von Ereignissen, Benachrichtigungen, Nachrichten, nicht gelesenen, Reaktionen, Dateien und Providerbefehlen rund um den Entwurf CRUD. | Nur Zeilen werden geändert, keine Ereignisse, Benachrichtigungen, Befehle, Nachrichten oder unabhängige Zustandsänderungen. |

## Dateien und S3-kompatible Speicher

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-FILE-001 | Dateien hochladen, auflisten, herunterladen, aktualisieren und löschen. | Die Multipart- und Metadatenströme entsprechen der Basislinie und die Bytes sowie die Sidecars werden in einem S3 kompatiblen Speicher gespeichert. |
| MSG-FILE-002 | Fügen Sie eine Datei zu einer Nachricht an und starten Sie alle Backend-Dienste neu. | Die Nachricht behält ihre autorisierte Datei URN und das Objekt bleibt herunterladbar. |
| MSG-FILE-003 | Eine Datei als nicht verwandter Benutzer oder Projektanfordern. | Der Zugriff wird verweigert, ohne dass Objektschlüssel oder Metadaten angegeben werden. |
| MSG-FILE-004 | Löschen Sie die letzte autorisierte Dateiverweisung durch den unterstützten Fluss. | Kanonische Metadaten, Zugangsdatensätze, Objekte und Reinigung von Seitenwagen folgen dem festgelegten Vertrag. |
| MSG-FILE-005 | Mehrteilige Daten mit `acl={"mode":"public"}` und ohne `stream_uuid` hochladen und dann deren Metadaten und Bytes mit einem anderen gültigen Workspace-Token anfordern. | Der Sidecar enthält `acl.mode=public` ohne einen Stream UUID, der zweite authentifizierte Benutzer gelingt ohne Mitgliedschaft, anonymer Zugriff bleibt abgelehnt und die kanonische Datei URN bleibt unverändert. |

## Ereignisse und Wiederverbindungsverhalten

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-EVENT-001 | Üben Sie jede Messenger Ereignisart erstellen, aktualisieren, lesen und löschen. | Umschlag und Nutzlast entsprechen den Grundbildern. |
| MSG-EVENT-002 | Lesen von Ereignissen mit `epoch_version>`, dem Abgleich `epoch_generation` und der Cursor-Pagierung. | Ereignisse sind aufsteigend, für den Benutzer lückenfrei, auf 500 begrenzt und nicht dupliziert. |
| MSG-EVENT-003 | Websocket trennen, Mutationszustand ändern und dann mit dem gespeicherten Cursor wieder verbinden. | Verpasste Ereignisse werden wiedergegeben, ein `ready` Frame öffnet das Benachrichtigungs-Gate und erst dann geht die Live-Bereitstellung weiter. |
| MSG-EVENT-004 | Vergleichen Sie REST und Websocket-Darstellungen für jede Epoche. | Parsierte JSON Objekte sind identisch. |
| MSG-EVENT-005 | Warten Sie durch Leerlaufintervalle und Protokoll-Pings. | Die Verbindung bleibt ohne JSON Kompatibilitätsmeldungen gesund. |
| MSG-EVENT-006 | IAM Benutzer oder Projekt ändern, während ein alter Cursor beibehalten wird. | Der Client-Zustand und der Cursor sind partitioniert; kein Ereignis überschreitet die Grenze IAM. |
| MSG-EVENT-007 | Beginnen Sie mit einem gültigen zurückgehaltenen Cursor und mit einem Cursor, der älter als der konfigurierte Rückhaltegrund ist. | Ein zurückgehaltenes Suffix wird leer zurückgegeben; ein abgelaufener Cursor gibt eingegeben `epoch_pruned` zurück, so dass der Client autorisierende PostgreSQL-Snapshots neu lädt. |

## Regression des Anbieters

Die Anbieter-Szenarien verwenden den von API beschriebenen und getesteten privaten Anbieter
DiePostgreSQLDie öffentliche Nutzerinterface muß weiterhin die
Messenger Ressourcen mit eingetypten Anbieter-Metadaten.

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-PROVIDER-001 | Import und Mutation Zulip Kanäle, Themen, DMs, Benutzer, Nachrichten, Reaktionen und Lesestatus. | Eine kanonische Ressource pro stabilen Anbieter-Mapping ist über die unveränderte Öffentlichkeit API sichtbar. |
| MSG-PROVIDER-002 | Senden von Native- und Provider-Nachrichten über die gleichen Stream/topic Ansichten. | Seitenaufzeichnung, nicht gelesenen Zahlen, Ordner, Ereignisse, Abzeichen und Lieferstatus bleiben korrekt. |
| MSG-PROVIDER-003 | Stoppen, Neustart, Trennen, Wiederanschließen und Löschen des Provider-Kontos. | Dauerhafte Befehle und Mapping konvergieren ohne Duplikate oder Auswirkungen auf native Messenger. |
| MSG-PROVIDER-004 | Übertragen eines Bildes und einer generischen Datei in beide Richtungen. | Die empfangende Speicherung enthält die erwarteten Bytes und die Workspace-Nachrichten enthalten nur S3-unterstützte URNs. |

## Einsatz und Wiederaufbau

| Identifizierung | Szenario | Erwartetes Ergebnis |
| --- | --- | --- |
| MSG-DEPLOY-001 | Inspektionieren Sie Zuhörer, Prozesse, Pakete, Konfiguration und Routen nach der Bereitstellung. | Es sind nur dokumentierte öffentliche HTTP/websocket und private Provider API Routen vorhanden; keine sekundäre Messenger Persistenzlaufzeit bleibt. |
| MSG-DEPLOY-002 | Nach Erstellung von Nachrichten und Dateien die Backend-, PostgreSQL, Provider- und S3-Dienste neu starten. | Canonische Datenbank und S3 Zustand überleben und die Öffentlichkeit API gibt die gleichen Ressourcen zurück. |
| MSG-DEPLOY-003 | Ersetzen Sie die Backend- und Provider-Root-Images und das UI-Artefakt unabhängig voneinander, während die kanonischen PostgreSQL und S3 Daten erhalten bleiben. | Der öffentliche Zustand, die Provider-Mapping, Befehle, Ereignisse und Dateien bleiben ohne Neuinstallation oder destruktive Reinigung verfügbar. |
| MSG-DEPLOY-004 | Suchen Sie in der Bereitstellung nach Runtime-Mail-Abhängigkeiten und beobachten Sie die Netzwerkzähler während der Annahme. | Exim, Dovecot, Maildir-Pfade, SMTP/IMAP-Clients, Postzertifikate, Postrouten und Traffic fehlen. |
| MSG-DEPLOY-005 | Wiederherstellen von deklarierten Einwegansichten, Zählern, Suchindizes und Caches aus kanonischen PostgreSQL Basistabellen. | Die vollständige öffentliche Zusammenfassung und der sichtbare Benutzeroberflächenzustand entsprechen der Basislinie vor dem Wiederaufbau. |

## Vollstreckungsbefehl

1. Die unveränderte Messenger Einheit und Integrationssuite ausführen.
2. Ausführen von Routing-, OpenAPI, IAM-, Event-, Provider- API- und S3-Vertragsprüfungen.
3. Die Daten PostgreSQL und S3 werden beibehalten.
4. Ausführen der Messenger API, Provider, Speicherung, Neustart, Wiederaufbau, Ausfall und
   Isolationsszenarien.
5. Die unterstützten Reisen im sichtbaren globalen Playwright MCP Browser ausführen.
6. Ausführen der Arbeitslast von 150 gleichzeitigen Benutzern und Providern.
7. Speichern Sie die entlasteten Beweise im CASSI Testlauf-Archiv.

Die Ausführung wird nur akzeptiert, wenn alle erforderlichen Szenarien erfüllt sind.
Die Ausführung der Daten ist `NOT RUN` oder `BLOCKED`; sie darf nicht als überprüft gemeldet werden.
Kompatibilität.
