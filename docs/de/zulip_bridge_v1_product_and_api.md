# Zulip Brücke v1: Produktanforderungen und API Grenze

Status: **genehmigte Produktanforderungen und API Grenze; Umsetzung in
Fortschritt und durch den erforderlichen Annahmeplan** abgedeckt.

Dieses Dokument definiert die erste Integration von externen Messengers für Workspace.
Es ist absichtlich vom laufenden Messenger API-Vertrag in
[`workspace_api.md`](workspace_api.md)Die Feature-Branches enthalten die
entsprechende API, PostgreSQL Speicher, Anbieter HTTP, Bridge und Benutzeroberfläche
Durchführung, aber
Das Feature ist erst dann bereit, wenn alle erforderlichen Gate in
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md) Pass.

Die öffentlichen Konten API und die gemeinsamen Brückenverträge sind anbieterneutral.
Die für den Anbieter spezifische Nutzlast des Kontos wird durch eine dynamische Art RestAlchemy dargestellt.
Zulip ist der erste Anbieter und ein separat eingesetztes
`workspace-zulip-bridge` Element ist die erste Implementierung.
Mail, Kalender und andere Kontoarten erweitern Sie den Auswählungsbereich mit neuer Art
Die Datenbank ist in der Regel nur für die Datenbank zu verwenden.
- Die Ressource.

## 1. Ziele

- Lassen Sie einen Workspace Benutzer einen persönlichen Zulip Konto in einem Bereich verbinden.
- Projekt ausgewählte Zulip Konversationen in gewöhnliche Workspace-Streams und
  Die Kommission hat die Kommission aufgefordert, die in Artikel 4 Absatz 1 genannten Maßnahmen zu ergreifen.
- Bereitstellen einer Zwei-Wege-Synchronisierung für die V1-Fähigkeitssätze.
- Bewahren Sie die kanonische Workspace Architektur: PostgreSQL speichert kanonische
  Nachrichten und Messenger Ereignisdaten; Dateien verwenden Objektspeicher und Nachrichten
  nur URNs enthalten.
- Halten Sie die Providerbrücke unabhängig einsetzbar und verhindern Sie, dass es von Lesen
  nicht verwandte Datenbankzeilen Workspace oder Objekte S3.
- Die Anbieter-Anmeldeinformationen werden in Ruhe verschlüsselt und für Browser nicht verfügbar.
  Administratoren, Protokolle und gewöhnliche Workspace API Antworten.
- Die native Workspace Nachrichtenübertragung soll auch dann funktionieren, wenn die Brücke nicht funktioniert.

## 2. Nicht-Ziele für V1

- Umfragen, Tippen, Anrufe und Präsenz-Synchronisierung.
- Hohe Verfügbarkeit für das Brückenelement.
- Anbieter mit Ausnahme von Zulip.
- Die Daten über die Betriebsprotokolle und die gesamte Gesundheit bleiben in einem
  Sie dürfen jedoch keine Anmeldeinformationen oder Nachrichteninhalte enthalten.
- Sicherungseinheiten werden von einem anderen Subsystem bereitgestellt.
- Kompatibilität mit dem entferntem Provider API oder den verborgenen alten Benutzeroberflächenrouten
  `/providers/` und `/external_users/`.

## 3. Gute Produktverhaltensweisen

### 3.1 Eigentum des Kontos und Lebenszyklus

- Ein externes Konto gehört privat einem Workspace Benutzer und ist global zu
  Der Benutzer ist in diesem Bereich.
- Ein Benutzer kann höchstens ein externes Konto eines Anbietertypes haben.
  bedeutet höchstens ein Zulip Konto.
- Zulip -Setup erfordert einen HTTPS -Server URL, eine E-Mail-Adresse und einen API -Schlüssel.
- Die API kann berichten, dass eine Anmeldeinformationen existiert, aber
  keinesfalls den API Schlüssel oder einen verschlüsselten Anmeldeumsatz zurückgibt.
- `Disconnect` stoppt die Synchronisation, während eine nur-lesbare Projektion beibehalten wird.
- `Delete` entfernt die Anmeldeinformationen, Abbildungen, geplanten Entitäten, Warteschlange,
  und kopierte Dateien im Besitz des Anbieters.
- IAM Deaktivierung aussetzt die Synchronisation aus und verbirgt den Account. IAM Löschung
  Sie wird mit der gleichen zerstörerischen Semantik wie `Delete` gelöscht.

### 3.2 Auswahl des Chat und Projektzuweisung

- Der Eigentümer kann einzelne externe Chats auswählen oder `all` auswählen.
- `all` ist dynamisch: Später erstellte Chats werden automatisch ausgewählt.
- Der Eigentümer wählt eine Historiendurchmesser für das Konto: `new`, `7_days`,
  `30_days`, `90_days` oder `all` Die Standardzahl ist `30_days`.
- Jeder ausgewählte externe Chat gehört zu genau einem Workspace Projekt.
- Der Account hat ein Standardprojekt für neu ausgewählte Chats.
- Das Verlegen einer vorhandenen Projektion in ein anderes Projekt ist atomar vom Produkt
  Perspektive und bewahrt Workspace UUIDs, Historie, Lesestatus und Provider
  Die Umsetzung muss die Entfernung von Quellprojekten und
  Schnellbilder der Erstellung von Zielprojekten; sie dürfen keinen Zwischenzustand aufzeigen
  in denen die Projektion weder zu einem Projekt noch zu beiden Projekten gehört.

### 3.3 Zulip-zu-Workspace-Mapping

| Zulip Unternehmen  | Workspace Projektion                                                        |
| ------------- | --------------------------------------------------------------------------- |
| Kanal       | Strom                                                                      |
| Thema         | Thema im geplanten Stream                                               |
| Ein-zu-einem DM | Privater persönlicher Stream mit genau zwei Teilnehmern und einem Standardthema |
| DM der Gruppe      | Private Gruppenströme mit einem Standardthema                                 |
| Zulip Benutzer    | Stabile Identität nach Provider-Realm und Provider-Benutzer-ID                |

Der kanonische Identitätsschlüssel ist `(Anbieter, provider_realm_uuid,
provider_user_id) `. Die UUID-Feld und die authentifizierte Benutzer-ID des Kontos stammen von
die Zulip Ereigniswarteschlange Registrierung, also ein veränderter Server URL, E-Mail-Adresse,
Anzeigen von Namen oder einem anderen Workspace Konto kann der Eigentümer nicht stillschweigend geändert werden.
E-Mail- und Anzeigennamen werden nie als Beweis akzeptiert.

Wenn ein IAM Benutzer erfolgreich ein Zulip Konto verbindet, nur das authentifizierte
die kanonische Identität Zulip des Kontos ist mit dem Kontoinhaber IAM UUID verknüpft.
Andere Zulip Benutzer bleiben nur lesbare externe Identitäten, aber wiederverwenden eine kanonische
Workspace UUID über alle verbundenen Konten im gleichen Zulip Bereich.
Konfliktorientierte verifizierte Eigentümerverbindung wird abgelehnt, ohne dass eine der Identitäten geändert wird
Die bestehenden Konto-Scoped-Konten werden von den
Identitäten werden in die kanonische Identität mit ihren Messenger zusammengeführt
Referenzen, Zuordnungen und zwischengespeicherte Ereignis-Nutzlasten, die vor dem Duplikat aktualisiert wurden
Die Verknüpfung einer Identität mit IAM gewährt dem Benutzer keinen Zugriff auf
Die in den Abschlussberichten aufgeführten Projektionen sind nicht zu berücksichtigen.
Verknüpfte IAM Konto trägt Ströme und ungelesenen Zustand zu diesem Benutzer.
weiterhin `urn:user:<identity-uuid>` verwenden.

### 3.4 Synchronisationssemantik

- V1 unterstützt zwei-Wege erstellen, bearbeiten, löschen, Reaktionen, Lesestatus, Erwähnungen,
  Antworten, Anführungszeichen, Markdown, Links, Bilder, Dateien und Stream/topic
  die Leistung des Anbieters zulässt.
- Ausgehende Geschäfte werden mit dem persönlichen Konto Zulip des Eigentümers durchgeführt.
- Die Funktionen des Providers sind autorisiert, nicht unterstützte Aktionen werden versteckt.
  vorübergehend nicht verfügbare Aktionen mit einer sicheren Erklärung deaktiviert werden.
- Die letzte bestätigte Operation gewinnt.
- Jede Operation hat eine stabile UUID und Anbieter-Idempotency-Metadaten.
- Die Live-Synchronisation startet zuerst und hat
  Strenge Planungsprioritäten gegenüber Auslaufversuchen und Rückfüllung.
- Die erste Aufholphase erzeugt keine Desktop-Benachrichtigungen.
  nur aktiviert, wenn das Konto den Stand der Aktivierung erreicht hat.
- Jeder akzeptierte Anbieter-Einheit speichert den Eingang `delivery_class` und eine
  die Entscheidung `notification_eligible` in den Metadaten des öffentlichen Anbieters eingefroren hat.
  `backfill` ist immer nicht berechtigt. Eine `live`-Nachricht ist nur berechtigt, wenn die
  Das Benachrichtigungs-Gate war bereits beim Einladen geöffnet; eine Live-Nachricht
  Akzeptiert, während die Kontohistorie noch aufholt, bleibt keine Benachrichtigung.
  Spätere Änderungen des Kontozustands fördern gespeicherte Nachrichten nie rückwirkend.
- Eine dauerhafte Outbox behält für bis zu 24 Stunden wieder ausprobierbare Operationen.
  - Die Show .`pending`Dann entweder .`delivered`Oder ...`failed`Eine gescheiterte Operation kann
  wieder ausprobiert oder weggeworfen werden.
- Auswählung oder Verlust des Zugangs zum Anbieter beendet die ausstehende Arbeit und
  Entfernt die Projektion und kopierte Anbieterdateien.
- Ziel-Gesundheits-System-Latenz ist p95 höchstens 5 Sekunden.
  `degraded` nach 30 Sekunden ohne Fortschritt der Synchronisation.

### 3.5 Verlustbewusste Konversion von Inhalten

- Unterstützte Zulip Inhalte werden in kanonische Workspace Markdown umgewandelt.
- Nicht unterstützte Elemente verwenden eine sichere, lesbare Fallback und eine
  `Open original` Link, wenn Zulip einen bereitstellen kann.
- Die Roh-Anbieter-IDs und die strukturierten Konvertierungsmetadaten werden in internen
  Projektionsmetadaten, die nicht als schreibbare Browserfelder dargestellt werden.
- Vor einem ausgehenden Vorgang, bei dem bekannt ist, daß Informationen verloren gehen, erhält die Benutzeroberfläche eine
  das Server-Seite-Ergebnis vor dem Flug und erfordert eine ausdrückliche Bestätigung.
- Anschlüsse werden in das empfangende System kopiert. Workspace
  Bytes plus ein JSON Sidecar in S3-kompatibelem Speicher; die Nachricht enthält nur
  `urn:*` Referenzen.

### 3.6 Anwendung

- Der Kontoinhaber sieht Konto, Chat-Auswahl, Fortschritt, Fähigkeit und
  Die Kommission hat die Kommission aufgefordert, die
- Ein Bereichs-Administrator verwaltet Providerrichtlinien, benutzerdefinierte CA-Zertifikate,
  Grenzwerte und Notfall-Suspendieren/resume
- Ein Bereichs-Administrator sieht nur die gesamte Bridge/account-Gesundheit.
  Die Oberfläche darf keine Anmeldeinformationen, Nachrichteninhalte oder den Chat des Eigentümers freilegen.
  - Ich habe einen Katalog.
- Ausgehend Zulip TLS verwendet das System-Vertrauensspeicher plus Administrator-verwaltet
  Hostname-Verifizierung ist immer aktiviert; unsicher
  oder Übersprung-Verifizierungsmodi sind verboten.

## 4. Einschränkungen des laufenden Vertrags

Die Umsetzung muss den laufenden Messenger Vertrag verlängern, anstatt
Die Kommission hat die Kommission aufgefordert, die in den letzten Jahren durchgeführten Maßnahmen zu überprüfen.

- Die Browser-APIs bleiben unter `/api/workspace/v1/messenger/**` und verwenden die IAM
  Das Token liefert derzeit eine Benutzer- UUID und Projekt-ID.
- Die Anbieter-Management-Route ist weiterhin additiv zum aktuellen Browservertrag.
- PostgreSQL ist autorisiert für Nachrichten, geteilter Messenger-Zustand, Konto
  Einstellungen, verschlüsselte Anmeldeinformationen, Anbieterwarteschlangen und Entduplikationsprotokolle.
- Die Datei-Kopierung verwendet eine separate, enge Binärübertragungs-Ebene; sie kann keine
  die bridging-globalen S3-Zugriffsdaten.
- Die Felder öffentlich `provider` und `delivery` sind reserviert, aber nicht ausgefüllt
  Die Parität der Serialisierer ist eine Voraussetzung für die
  für die Aktivierung der Funktion.
- Die Cursoren für Browser-Ereignisse sind projekt- und benutzerorientiert mit konfigurierbarem
  Sie sind keine Brücken-Warteschlange-Cursoren und müssen
  nicht als Anbieter-Auslauf- oder Rückfüllstellen wiederverwendet werden.
- Der aktuelle `ExternalAccount` Rest ist projektbezogen und erlaubt Klartext
  JSON -Zertifikate. Es muss durch ein real-scoped-Modell ersetzt werden; es ist nicht ein
  Ein Umzugszweck oder ein Vereinbarkeitsvertrag.

## 5. ÖffentlichkeitWorkspace APIVorschlag

Die folgenden Strecken werden im Rahmen der aktuellen IAM-authentifizierten Messenger vorgeschlagen:
Sie müssen in den Workspace OpenAPI und `@workspace/api` generiert werden
Alle UUID Sammelrouten verwenden den Standard
Messenger Paginierungskontrakt. Die Routen werden von jedem externen Konto geteilt
Die Fluggesellschaften können nicht mehr auf den einzelnen Strecken verkehren.

### 5.1 Außenwirtschaftliche Rechnungen

| Methode   | Fahrbahn                                                         | IAM Genehmigung                            | Zweck                                                                      |
| -------- | ------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------- |
| `GET`    | `/external_accounts/`                                         | `workspace.external_account.read`         | Liste der externen Konten des aktuellen Benutzers.                      |
| `POST`   | `/external_accounts/`                                         | `workspace.external_account.create`       | Erstellen und validieren Sie alle unterstützten Kontoarten mit einem Schreib-allein-Zertifikat. |
| `GET`    | `/external_accounts/{account_uuid}`                           | `workspace.external_account.read`         | Holen Sie sich die entlastete Kontoaufnahme des Eigentümers.                                  |
| `PUT`    | `/external_accounts/{account_uuid}`                           | `workspace.external_account.update`       | Ersetzen Sie die nicht geheimen Einstellungen.                                         |
| `POST`   | `/external_accounts/{account_uuid}/actions/reconnect/invoke`  | `workspace.external_account.reconnect`    | Validieren und ersetzen Sie die Schreib-allein-Zertifikate und setzen Sie dann fort.                 |
| `POST`   | `/external_accounts/{account_uuid}/actions/disconnect/invoke` | `workspace.external_account.disconnect`   | Stoppen Sie die Synchronisation und behalten Sie eine nur-lesbare Projektion.                                 |
| `DELETE` | `/external_accounts/{account_uuid}`                           | `workspace.external_account.delete`       | Vernichtend löschen Sie das Konto und geben Sie `204` zurück.                            |

Die genehmigte Ressourcenform ist ein gemeinsamer Umschlag mit einer dynamischen `settings`
Die `settings.kind`-Diskriminierer wählen einen konkreten
`AbstractKindModel` durch
`KindModelSelectorType`. Gemeinsamer Lebenszyklus, Eigentümer, Status, Überarbeitung, Fähigkeit,
Die Zeitstempelfelder bleiben draußen .`settings`Jede Art besitzt ihre Verbindung,
Die API erzwingt, dass eine
Eigentümer hat höchstens ein Konto für jeden `settings.kind` im Bereich.

Das Feld öffentlich `capabilities` ist die von Backend berechnete effektive Kontoebene
Projektion nach Anbieter, Brückeninstanz, Bereichsrichtlinie und Kontoauszug
Es ist nicht der rohen Herzschlagdeskriptor und enthüllt weder die
Die Benutzeroberfläche verwendet dieses Feld für
Aktionen und Status auf Kontoebene.

Öffentliche Funktionen verwenden eine Karte aus dem gleichen stabilen Namespaced-Funktionsnamen
zu einem effektiven Deskriptor mit `available`, `revision`, `limits` und
eine optionale strukturierte Sicherung `unavailable_reason`.
Einheitliche Banken werden von der Bank nicht mehr abgeschaltet.
`available=false`; fehlender Name bedeutet, dass die Ressource diese nicht unterstützt
Die Kunden dürfen die Verfügbarkeit nicht aus dem Rohstatus oder aus der
Anbieterart, wenn ein effektiver Deskriptor vorhanden ist.

Die Einstellungen für Erstellen, Reinigung und Wiederanschluss sind unterschiedlich
API -Typen. Eine /reconnect -Typen erstellen kann nur schreibbare Anmeldefelder enthalten;
Die entsprechende Antwortart kann sie nicht serialisieren.
Die Angabe des neuen Modells in der
Auswählungen ohne Änderung der Sammelrouten oder Hinzufügen von nullfähigen Feldern zu
die gemeinsame Ressource.

Zulip `POST /external_accounts/` Anfrage:

```json
{
  "uuid": "client-generated-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  }
}
```

Reinigte Kontoantwort:

```json
{
  "uuid": "account-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  },
  "credential_present": true,
  "status": "live",
  "live_ready": true,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 7,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z"
}
```

Die Kontostatuswerte sind `connecting`, `backfill`, `live`, `degraded`,
`auth_required`, `disconnected` und `suspended`.

`PUT` ist revisionssicher mit einem starken `ETag` und erforderlich `If-Match`.
Zulip Art es kann nur ändern `selection_mode`, `history_depth`, und
`default_project_id` innerhalb `settings`. Server URL, E-Mail und API Schlüsselwechsel
Nur durch .`reconnect`. `settings.kind`Die anderen Arten
Die Datenbank kann die Datenbank für die Datenbank-Anwendung verwenden.

### 5.2 Katalog und Zuordnung externer Chats

Die genehmigte Katalogform ist eine gemeinsame Top-Level-Ressource Messenger.
nur für Kontoarten verfügbar, die die `chat_catalog`-Fähigkeit bewerben;
Eine Kontoart wie Mail oder Kalender muss nicht implementiert werden.

| Methode | Fahrbahn                                                 | Zweck                                                                |
| ------ | ----------------------------------------------------- | ---------------------------------------------------------------------- |
| `GET`  | `/external_chats/?external_account_uuid=...`          | Liste den vom Besitzer entfernten Provider-Chat-Katalog und den Zuteilungsstatus auf. |
| `GET`  | `/external_chats/{chat_uuid}`                         | Holen Sie einen entlasteten Chat-Snapshot.                                       |
| `POST` | `/external_chats/{chat_uuid}/actions/select/invoke`   | Wählen Sie einen Chat aus und weisen Sie ein Projekt zu.                                    |
| `POST` | `/external_chats/{chat_uuid}/actions/deselect/invoke` | Absagen und die Projektion entfernen.                                 |
| `POST` | `/external_chats/{chat_uuid}/actions/move/invoke`     | Atomisch eine bestehende Projektion in ein anderes Projekt zu verschieben.             |

Die Ressource verwendet einen gemeinsamen Umschlag mit einer dynamischen `source` Eigenschaft.
`source.kind` wählt anbieterspezifische Katalogmetadaten mit
`KindModelSelectorType`; für v1 ist die einzige Implementierung `zulip`.
Felder umfassen Workspace-generierten Chat UUID, externes Konto UUID, Auswahl
Status, Projektzuordnung, Projektions-UUIDs, Fähigkeiten, Status, Überarbeitung,
Die Roh-Anbieter-IDs sind intern, nie schreibbar und müssen nicht
- Sie sind bloß.

Jedes Chat-Feld `capabilities` ist die von Backend berechnete effektive Projektion
Es kann schmaler sein als die Konto-Ebene-Projektion
weil der Chat-Typ des Anbieters, der Zuteilungsstatus oder die Richtlinie eine Aktion deaktivieren können.
Die Benutzeroberfläche leitet niemals Chat-Verhalten aus einer Rohbrücken-Instanz-Fähigkeitskarte ab.
Das Backend behält den Raw-Katalogdeskriptor separat, so temporär
Account/instance nicht verfügbar ist, kann der effektive Deskriptor ohne
Die Katalogfähigkeit nach der Wiederherstellung wird zerstört.

`select` und `move` akzeptieren eine `project_id`; `move` erfordert auch `If-Match` für
`deselect` annulliert sofort ausstehende Arbeiten
Jede Aktion gibt einen vollständig entlasteten Chat zurück
Ein Schnappschuss.

`selection_mode=all` ist Zulip Konto-Zustand, nicht eine einmalige Batch-Aktion.
Das Backend weist die neu entdeckten Chats weiterhin an `default_project_id` zu
Bis der Besitzer den Modus ändert.

### 5.3 Externe Operationen

Die zugelassene dauerhafte Betriebsfläche ist eine gemeinsame Oberfläche Messenger
Die Datenbank ist eine Datenbank, die für die Datenbank verwendet wird.
einschließlich der Operationen, deren Ziel nicht geschaffen wurde oder bereits geschaffen wurde
- Sie werden gelöscht.

| Methode   | Fahrbahn                                                        | Zweck                                                             |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------------- |
| `GET`    | `/external_operations/`                                      | Liste der vom Eigentümer ausgeführten oder fehlgeschlagenen externen Operationen.             |
| `GET`    | `/external_operations/{operation_uuid}`                      | Entfernen Sie den Betriebsstatus.                                     |
| `POST`   | `/external_operations/{operation_uuid}/actions/retry/invoke` | Versuchen Sie erneut eine ausreichende, fehlgeschlagene Operation.                                 |
| `DELETE` | `/external_operations/{operation_uuid}`                      | Ausfallfähige ausstehende /failed Arbeiten und Rückkehr `204`              |
| `POST`   | `/external_operations/actions/preflight/invoke`              | Rückgabefähigkeit und Verlustinformationen vor einer Ausgangsmutation. |

Eine Operation-Antwort verwendet einen gemeinsamen Umschlag mit seinem UUID, externen
Konto UUID, Aktion, Zieltyp/UUID, Status, Sicherheitsfehler, Wiederholungsversuch/discard,
Versuchs- und Versuchshistorie, Duplikationsrisiko- und Wiederholungsbestätigungs-Flaggen,
Originalanbieter URL bei Sicherheit, Vergleichszustand/reason/evidence, Überarbeitung,
Ein dynamisches `details.kind` Modell enthält gesäuberte
Anbieterspezifische Liefermetadaten. Sie enthalten keine Rohlieferantenlast.
Anmeldeinformationen, Nachrichteninhalte über die gewöhnliche autorisierte Zielressource hinaus,
oder Roh-Anbieter-Geschichte-Überprüfungen.

`delivery` auf die prognostizierten Ressourcen konsequent erweitert wird, um
`external_operation_uuid`, `status`, `safe_error`, `can_retry`, `can_discard`,
`updated_at`, `duplicate_risk`, `retry_requires_confirmation`, `original_url`,
und `reconciliation_reason`. Sein Status ist einer von `pending`, `delivered`,
`failed`, `manual_reconciliation_required` oder `discarded`.
Die Daten über die Konzentration der Daten sind nur auf der Betriebsressource verfügbar.
Die Ressourcen geben weiterhin `provider: null` und `delivery: null` zurück.

Der gemeinsame `provider` Umschlag ist:

```json
{
  "kind": "zulip",
  "account_uuid": "account-uuid",
  "external_id": "provider-entity-id",
  "capabilities": {},
  "delivery_class": "live",
  "notification_eligible": true
}
```

`delivery_class` ist `live` oder `backfill`; `notification_eligible` ist die
Rücklauf-Einnahme-Frozen-Entscheidung gemäß Abschnitt 3.4 REST und Echtzeit
Die Clients unterdrücken die Benachrichtigung des Desktops,
Stimmen und Aufmerksamkeit, wenn es explizit ist.`false`Die Kommission hat die
Anbieterumschläge, die vor dem Bestehen dieses optionalen Felds erstellt wurden, behalten ihre
die übliche Benachrichtigungspolitik.

Bevor ein vom Provider geplanter Stream, Thema oder Nachricht mutiert wird, wird das Backend
Schließt seine Chat/account Kartierung und überprüft Auswahl, Lebhaftigkeit, Zuordnung,
Ein fehlgeschlagener Vorflug lehnt die Anfrage ab, bevor
Die kanonische .MessengerEs gibt keinen lokalen Erfolgsmodus für eine
Native Messenger
Ziele durchlaufen den bestehenden Weg.

### 5.4 Äußere Identitäten

Die externen Identitäten werden über die vorhandene Benutzersuchfläche zurückgegeben, wenn
nur für Lesen vorgesehene Benutzer mit expliziten Identitätsmetadaten:

```json
{
  "uuid": "stable-external-identity-uuid",
  "identity_kind": "external",
  "provider": { "kind": "zulip", "account_uuid": "account-uuid" },
  "display_name": "Provider user",
  "avatar": "urn:image:file-uuid"
}
```

Eine ungelöste externe Identität kann nicht authentifiziert werden, besitzt keinen externen Account,
oder verwendet werden, um ein IAM Profil oder einen nativen persönlichen Stream zu öffnen.
Eigentümeridentität wird nur nach dem Backend durch die vorhandene IAM UUID dargestellt
Die für die betreffende Gruppe gemeldete Authentifizierung Zulip `(realm_uuid, user_id)` wird validiert.
E-Mail- und Anzeigenname-Gleichheit lösen diesen Link nie aus.

### 5.5 Echtzeitereignisse und Client-Caching

Der bestehende Projekt/user Ereignisstrom gewinnt gesundheitsgerechte Vollschnappschnappschnapps:

- `external_account.created`, `external_account.updated`,
  `external_account.deleted` für den Eigentümer;
- `external_chat.created`, `external_chat.updated`, `external_chat.deleted` für
  der Eigentümer;
- `external_operation.created`, `external_operation.updated`,
  `external_operation.deleted` für den Eigentümer;
- Normaler Stream, Thema, Nachricht, Benutzer, Datei und Lesereignisse für projizierte
  Messenger Einheiten.

Die Benutzeroberfläche speichert normalisierte Konten, Chat, Fähigkeiten, Anbieter und Betrieb
Die Datenbank wird die Daten von den Schnappschnappschriften in IndexedDB aktualisieren und sie von den vollständigen Schnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnappschnapps
oder eine Epoche-Generation-Missmatch löscht diese Caches und führt eine neue REST
Schnappschuss vor der Einrichtung von Benachrichtigungen.

## 6. Vorschlag für die Verwaltung des Reiches API

Die genehmigte Verwaltung Form trennt die gewünschte Richtlinie von nur für Lesen
IAM liefert Berechtigungen durch die bestehende Selbstbeobachtung
`permissions` Liste, normalerweise durch eine zugewiesene Rolle, und die Workspace
Das Backend erzwingt die handlungsspezifische Berechtigung für jede Route.
Namen folgen `service.resource.action`; ein Rollenname wird nie als Aktion verwendet
Die Streamingrolle `administrator` ist ebenfalls unzureichend, da sie
Projekt- und Strombereich.

| Methode | Fahrbahn                                                       | IAM Genehmigung                               | Zweck                                                                            |
| ------ | ----------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| `GET`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.read`    | Lesen Sie die Politik für das gesäuberte Gebiet für eine Kontoart.                                   |
| `PUT`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.update`  | Aktualisierung der typenspezifischen Richtlinie unter Verwendung von `If-Match`; die Nutzlast ist ein dynamisches Typmodell. |
| `GET`  | `/external_provider_health/{kind}`                          | `workspace.external_provider_health.read`    | Lesen Sie die Gesamtverbindung und die Kontozustand für eine Kontoart.                      |
| `POST` | `/external_provider_policies/{kind}/actions/suspend/invoke` | `workspace.external_provider_policy.suspend` | Notfall-Suspendieren des Kontos in der ganzen Welt.                                     |
| `POST` | `/external_provider_policies/{kind}/actions/resume/invoke`  | `workspace.external_provider_policy.resume`  | Nach der Validierung wiederholen.                                                           |

Für eine vollständige Verwaltung der Politik und für eine Gesamtsichtbarkeit der Gesundheitspolitik
Die administrative IAM Rolle dieser fünf genauen Berechtigungen:

- `workspace.external_provider_policy.read`
- `workspace.external_provider_policy.update`
- `workspace.external_provider_policy.suspend`
- `workspace.external_provider_policy.resume`
- `workspace.external_provider_health.read`

`workspace.external_provider_policy.*` nicht gewähren: Workspace und das Element
Manifest nur exakte Handlungsberechtigungen verwenden und keine Wildcard-Berechtigungsressource
ist vorbereitet.

Gesundheitsaggregate zählen und Latenz/queue Metriken nur.
Antworten enthalten niemals E-Mail-Adresse des Kontos, Server URL, Chat-Namen, Anmeldeinformationen oder
Benutzerdefinierte CA-Eingabe akzeptiert nur CA-Zertifikate, lehnt private
Schlüssel, und ist Version.

Die Runtime-Brücke-Identitäten werden separat durch die gemeinsame Oberstufe-Identität ausgestellt.
`/external_bridge_instances/` Verwaltungskapazität:

| Methode | Fahrbahn                                                               | IAM Genehmigung                               | Zweck                                                                                      |
| ------ | ------------------------------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `GET`  | `/external_bridge_instances/`                                       | `workspace.external_bridge_instance.read`    | Liste der gesäuberten Bridge-Instanzen über Provider-Typen hinweg.                                       |
| `GET`  | `/external_bridge_instances/{instance_uuid}`                        | `workspace.external_bridge_instance.read`    | Lesen Sie Identitätsgenerierung, Zustand, Fähigkeit, Herzschlag, Zertifikatsverfall und sichere Fehler. |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | `workspace.external_bridge_instance.suspend` | Die Identität sofort blockieren, ohne ihre Erzeugung zu widerrufen.                              |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/resume/invoke`  | `workspace.external_bridge_instance.resume`  | Wieder aufgenommen, nicht widerrufen, ausgesetzt.                                                     |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/revoke/invoke`  | `workspace.external_bridge_instance.revoke`  | Die aktive Zertifikatserstellung wird unwiderruflich widerrufen.                                       |

Die Ressource gibt nie Zertifikate zurück. Privates Material, Einschreibungsgeheimnisse.
Die Daten werden mit Hilfe von Aktionen zurückgegeben.
aktualisierte sanitierte Instanz-Schnappschuss. Anbieter-Politik und gesamtgesundheitliche Aufenthalte
Die Anmeldung ist absichtlich
nicht eine Messenger API Aktion: ein Plattformbetreiber dreht das Exordos-Geheimnis
Ressource durch das Manifest/CLI, Core liefert seine verwaltete Knotenkonfiguration an
sowohl Backend und Brücke, und das Backend öffnet automatisch die Übereinstimmung
Das Workspace Backend erhält keine Exordos Core-Zugriffsrechte.

Die Workspace -Elemente zeigen alle fünfzehn kanonischen Maßnahmen
Berechtigungsressourcen, die oben aufgeführt sind, und zwei globale Rollen mit fester Berechtigung
`workspace-external-integration` enthält die sechs Eigentümer-scoped externen
`workspace-external-integration-admin` enthält die neun
Provider-Policy, Aggregate-Health und Brücken-Instanz-Verwaltung
Die Angabe der Benutzerrolle ist nicht bindend.
Die Rolle der Projektpartner wird ausdrücklich im Projekt Workspace bis IAM festgelegt.
Die Kommission hat in ihrem Bericht vom 1. Juli 1996 eine Reihe von Vorschlägen für eine
Änderung der gewöhnlichen Workspace -Rolle des Benutzers.

## 7. Privates Flugzeug

Der Browser ruft nie die Brücke oder Zulip direkt an.
API verbindet das Workspace Backend und die Brücke.
Status nur; es transportiert niemals Nachrichtenkörper oder Messenger Ereignisse.

Die zugelassene Laufzeitoberfläche ist ein separater privater Backend-Luschter und
Prozess `workspace-external-bridge-api`, mit seiner eigenen Version `/v1/`-Wurzel.
Es bindet nur die Plattform-interne Schnittstelle, erfordert einen gültigen Client
Zertifikat an der Steckdose TLS für jede Strecke mit Ausnahme der ersten Anmeldung und
nicht von der Öffentlichkeit vertreten .WorkspaceDie erste Anmeldung verwendet Server-
Authentifiziert TLS plus die einmalige Bootstrap-Zugriffsberechtigung, weil die Brücke
Die bestehende IAM-authentifizierte
Die Hörgeräte bleiben unverändert und enthüllen keine internen Kontroll- oder Dateiwege.
Die Kontroll- und Dateiressourcen teilen sich diesen privaten OpenAPI Hörer, bleiben aber
getrennte Ressourcengruppen.

Der Hörer PKI ist im Besitz des Backends und auf einem dedizierten kleinen persistenten
Die erste Festplatteninitialisierung erstellt
die real-bound Control CA, Server-Schlüssel und Zertifikat sowie die Integritäts-Metadaten
Ein Backend-Rutenaufnahmegestaltung montiert die gleiche Festplatte und muss scheitern
bei einem fehlenden, teilweisen, unsicheren oder nicht übereinstimmenden Bereich PKI geschlossen
und schweigend eine neue Vertrauenswurzel erzeugen.

Erster Control-CA-Vertrauen verwendet den realm-bound HMAC-authentifizierten Bootstrap
Hersteller/consumer - Muster; es wird keine TOFU- oder TLS-Verifizierung ausgeschaltet.
Vor dem Öffnen des HTTPS Kontrollhörers
Die Brücke führt zu einer separaten Plattform-Innenfläche.HTTP `GET /ca.crt`Endpunkt
mit einer frischen 256-Bit-Kleine-Hexadezimalzahl `nonce`, genaue erwartete Steuerung
`hostname`, `bridge_instance_uuid` und positiv `enrollment_generation`.
Zusätzliche Identitätsfelder lassen nur ein Bereich mit mehreren Installationen auswählen
Die Einmalanmeldung ist niemals ein Geheimnis.
Das Backend gibt die öffentlichen Kontroll-CA-Bytes zurück,
`Content-Length` und `X-Workspace-CA-HMAC-SHA256`.

Beide Peer ableiten den Schlüssel HMAC und den persistenten Eintragungsprüfer als
`SHA-256(b"workspace-bridge-enrollment-v1\0" + token_utf8)`. Die Reaktion HMAC
deckt den unterschiedlichen Protokollkontext ab
`workspace-external-bridge-control-ca-v1\0`, Nonce, NUL, Hostname, NUL,
die canonische Brückengeneration UUID, NUL, Basis-10 ohne erste Nullen, NUL und
Genau .PEMDie Brücke deaktiviert die Weiterleitung, erzwingt die bestehenden
Die Datenbank kann die Datenbank mit einem Datenverzeichnis von 10 Sekunden, einer 512-Byte-Anforderungszieleinheit und einer 1 MiB-CA-Limite vergleichen.
die HMAC in konstanter Zeit, validiert die PEM mit dem TLS Parser, und atomar
installiert es mit Datei und Verzeichnis fsync bevor die Aktivierung Hostname-verified
TLS. Das Abrufen der öffentlichen CA verbraucht nicht die Anmeldungsgeneration;
Erfolgreiche CSR Signatur tut.

Die Brücke erstellt ihren privaten Client-Schlüssel nur auf der permanenten Festplatte der Brücke
und übermittelt eine CSR durch einen einmaligen authentifizierten Anmeldefluss.
Signiert die CSR mit dem permanenten Steuerungszeichen CA und gibt nur den Client zurück
Zertifikat und öffentliche CA-Kette; der private Schlüssel überquert nie die
Maschinengrenze, Bootstrap-Zustellung, HMAC erstes Vertrauen, Erzeugungskonsum,
und Zertifikatsrotation definiert werden durch
[`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml).

Die Anmeldung verwendet eine eigene Exordos-Geheimressource für jede Brücke.
Das Manifest erzeugt zufälliges Bootstrap-Material und liefert es
Die Daten werden über die Netzwerke der Kern-Management-Nodes verbreitet.
Das Backend besteht nur noch als Verifizierer und Generator auf der Geheimnis-Disk.
Erfolgreiches CSR
Atomisch verbraucht diese Generation, so dass die Wiedergabe veraltete Knotenkonfiguration
Nach dem Verlust des Brückenstaates erneut registrieren
erfordert eine explizite Rotation der Exordos-Geheimressource und eine neue
Generation; ein dauerhaftes gemeinsames Anmeldegeheimnis wird nicht unterstützt.

Jede MTLS-Client-Identität stellt genau eine Brückeninstallation und eine
Die Zertifizierung ist an `realm_uuid`, `provider_kind` und
`bridge_instance_uuid`; die genaue Codierung der Bescheinigungsansprüche wird durch die
InneresOpenAPISicherheitsvertrag.ZulipDie Brückeninstanz verwendet diese Identität
Für alle .ZulipDas Backend autorisiert immer noch jede
Die Kommission hat die Kommission aufgefordert, die
Ein gültiges Brückenzertifikat allein gewährt niemals Zugang zu willkürlichen Konten.
Zertifikate werden nicht pro externem Konto ausgestellt oder an verschiedenen Anbietern freigegeben
- Das ist eine Art.

Server- und Client-Leaf-Zertifikate sind 30 Tage gültig und beginnen automatisch
Die Brücke erzeugt einen neuen privaten Schlüssel und
CSR lokal und authentifiziert die Erneuerung mit seiner noch gültigen mTLS-Identität;
Das Backend-System unterschreibt nur ein Zertifikat mit denselben genehmigten Identitätsansprüchen.
Die alten und neuen Kundenzertifikate überlappen sich für maximal 24 Stunden, um eine
Ein bereits abgelaufenes Clientzertifikat kann den
Die Einführung eines neuen Systems der Anmeldung in die
Die Daten werden in einem System mit einem Datenverarbeitungssystem gespeichert.

Die Kontroll-CA ist fünf Jahre gültig und wird nur durch eine ausdrückliche,
Die Vertretung der zuständigen Behörden in der Verwaltung wird durch die Vertretung der zuständigen Behörden in der Verwaltung erfolgt.
Die neue CA wird auf der Geheimnisplatte gespeichert und veröffentlicht ein Dual-Trust-Bundle für 30 Tage.
Die aktive Brücke erhält ein neues Blatt unter der neuen CA, während die Authentifizierung mit seiner
Die alte CA wird erst nach jeder aktiven Brücke aus dem Dienst genommen.
Die Instance ist migriert oder das Überlappungsfenster endet;
Die Kommission hat die Kommission aufgefordert, die
Die automatische jährliche CA-Ersetzung und die CA-Erneuerung während der gesamten Betriebsdauer werden
nicht unterstützt.

Das Backend ist für jedes aktive Zertifikat der Brückenidentität autorisiert
Jede Steuerung und jede Dateianfrage überprüft diese Werte nach
TLS Authentifizierung und vor der Ressourcenberechtigung, einschließlich Anfragen auf eine
Die Daten werden von einem anderen Netzwerk verwendet.
Die Rücknahme einer Identität ist irreversibel: das Backend
Er treibt seine Generation voran, lehnt jedes Zeugnis der alten Generation ab,
und erfordert ein rotiertes Einschreibungsgeheimnis plus ein neues CSR.
nicht der Widerrufsmechanismus, und keine Zuhörer-Wiederladung oder CRL -Verbreitung ist
erforderlich.

Die genehmigte Steuerung ist eine Workspaceeigene interne API, die von der
Die Brücke zieht die gewünschten Generationen und verschlüsselte Anmeldeinformationen
Sie werden dann den Herzschlag, die Fähigkeit, den Fortschritt und den tatsächlichen Zustand
Die Vereinbarung ist periodisch und hängt nicht von einem einzigen RPC
Erfolgreich.

Die Synchronisierung des gewünschten Zustands verwendet einen Versionen-Inkremental-Änderungsfeed bei
`GET /v1/desired-state/changes`Sein undurchsichtiger Cursor ist an das Reich gebunden,
Die Daten sind in der Regel in der Form eines "Bridge"-Instanz, Filter-Set und Steuerungsschema-Version.
Die Antwort enthält eine geordnete Idempotent-Charge und den nächsten Checkpoint.
Die Datenbank wird nur dann von einem Netzwerk, das die Datenbank für die Datenbank verwendet, überwacht.
Dann besteht der Checkpoint, so dass Wiederholung nach einem Absturz sicher ist.

Jeder `external_chat_assignment` vollständige Ersatz beinhaltet einen Backend-eigenen
`workspace_projection` Kartierung. Es enthält den Stream UUID und Präsentation,
Teilnehmer-Anbieter-IDs, die zu Workspace Identitäts-UUIDs zugeordnet sind, und Anbieter-Thema
Die IDs werden auf die UUIDs des Workspace Themas abgebildet. Die Brücke bleibt bestehen und verwendet diese Abbildung;
Es erfindet niemals einen Workspace Stream, ein Thema, einen Teilnehmer oder eine Nachricht UUID.
Die Zuordnung trägt auch den Anbieter-Diskriminierer in
`provider_chat.kind`; es ist Teil des vollständigen kanonischen Ersatz eher
Die Katalogübernahme akzeptiert eine persönliche direkte
Chat nur mit genau zwei verschiedenen Teilnehmern und nur einem Gruppen-Direct-Chat
Eine ungültige Topologie wird vor dem Chat oder
eine gewünschte Ressource `external_chat_assignment` wird aufrechterhalten.
Provider-Discovery-Berichte über Topologie ohne Workspace UUIDs und das Backend
Zuweist vor der Veröffentlichung der Zuordnung stabile UUIDs zu.
Die Datenbank wird von einem Anbieter unterstützt, der den Zugriffsvorgang voranbringt und veröffentlicht.
mit der neuen Themenkarte, aber nicht in der Warteschlange `topic.upsert`: Zulip wird realisiert
das Thema mit dem ersten `message.create`. `topic.upsert` ist für
Das ist auch ein Beispiel für die Umbenennung eines Themas, das bereits eine Anbieternachricht-Mapping hat.
Die erste ausgehende Nachricht funktioniert bei `history_depth=new` und keine eingehende Geschichte
hat eine Kartierung verwirklicht.

Sowohl die inkrementellen Upserts als auch die vollständigen Snapshot-Ressourcen tragen ihre wirksamen
`required_capabilities`. Vor dem Schreiben des gewünschten Zustands, materialisiert Projektion
Die Brückenanlage wird von der
erfordert, dass der Ressourcetyp UUID und die Erzeugung der inkrementellen
Die Verpackung wird dann umgewandelt, wenn eine vorhanden ist.

Wenn eine Charge einen unbekannten `resource_type`, einen unbekannten `operation` oder einen Artikel enthält
Die Kommission hat in diesem Zusammenhang eine Reihe von Vorschlägen für die
Die Daten werden in einem anderen Format als der von der Herstellerin oder dem Hersteller.
Sicherer Kompatibilitätsbericht und das Backend markiert die Brückeninstanz
`incompatible`. Die Brücke darf den verletzenden Gegenstand nicht überspringen oder in Quarantäne stellen und
dürfen keine späteren Waren aus dieser Charge verpflichten; nachdem die Vereinbarkeit wiederhergestellt wurde,
Die gleiche Charge wird vom unveränderten Cursor aus wiedergegeben.

Herzschlag bleibt verfügbar, während eine Instanz `incompatible` ist.
Die Daten des Valid Heartbeat-Systems werden von den Anzeigen für die Funktionen verwendet, die die blockierte Charge abdecken, die
Backend löscht automatisch `incompatible` und die Brücke spielt diese Charge erneut ab
Diese Kompatibilitätswiederherstellung erfordert weder eine
- Ich bin hier .`resume`Die Daten werden nicht überschrieben.
Verwaltungsunterbrechung oder Zertifizierungsentzug.

Die V1-Brücke verwendet normale Umfragen, keine langen Umfragen.
Die Antwort auf die Änderung des Feed wartet zwei Sekunden und stellt die nächste Anfrage mit der
Die Daten werden in einem anderen Bereich als dem der Datenbank verwendet.
die leere Zuführungsantwort wird sofort mit dem unveränderten Kontrollpunkt wiedergegeben.

Nach einem Netzwerkausfall, HTTP `429` oder wieder ausprobierbar `5xx`, Polling verwendet
Exponentielle Rückschaltung mit vollem Jitter: eine ein Sekundenbasis verdoppelt sich zu einer 30-Sekunden-
HTTP `429` und `503` Ehren `Retry-After` bis zu fünf Minuten.
Kurzer kommt nie auf Versagen voran, und Herzschlag Lieferung hat seinen eigenen erneuten Versuch
Die erste erfolgreiche Feed-Reaktion setzt den Backoff zurück und stellt den normalen
Zwei Sekunden.

Jede Änderung ist ein Ersatz-Record mit `change_uuid`, monoton
Folge, `resource_type`, `resource_uuid`, `operation` und Ressource
Eine `upsert` trägt den vollständigen gewünschten Ressourcen-Snapshot für diese
Art, einschließlich nur verschlüsselten Anmeldeumschlägen und anderer bridge-autorisierter
Die Anwendung der gleichen oder einer älteren Generation ist eine Option; eine neuere Generation ist eine Option, die nicht in der Regel verwendet wird.
A `delete` trägt nur einen Grabstein mit
Ressourcenidentität und -generierung, niemals das gelöschte Geheimnis oder die vorherige Nutzlast.
JSON Patches und Fetch-After-Change-Daten werden nicht verwendet.

Die vollständige Wiederherstellung beginnt mit `POST /v1/desired-state/snapshots`.
logische Snapshot-Session und gibt ein undurchsichtiger Snapshot-Token, eine Ankeränderung zurück
Kurzer,`snapshot_generation`Die Schnappschussseiten verwenden eine
Optisch und stabil .`(resource_type, uuid)`Das Backend macht
nicht eine PostgreSQL Transaktion während der gesamten Laufzeit der Sitzung abhalten und nicht
Die vollständige Momentaufnahme als ein Anwendungsspeicher oder JSONB Array verwirklichen.
Schnappschuss Ressourcen sind eingefroren als normalisiert, geordnet PostgreSQL Zeilen und jede
HTTP Seite liest höchstens die angeforderte Grenze plus eine nach vorne gerichtete Zeile.
Backend-Speicher begrenzt, wenn Zehntausende von Zuordnungen große
Ein gleichzeitiges Erstellen, Aktualisieren oder Löschen ist
entweder in diesen gefrorenen Zeilen oder im Wechselfuttermittel nach
Die Brücke installiert alle Seiten und zeigt dann die Änderungen nach der
Anker und verbindet den resultierenden Zustand plus Checkpoint atomar.
Das Snapshot-Token erfordert den Start einer neuen Sitzung.

Ein unbekannter, abgelaufener, nicht übereinstimmender oder nicht mehr dekodierbarer Cursor gibt eine
Die Brücke wird dann mit einer neuen Funktion ausgestattet, die die Reaktion auf die automatische Wiederherstellung des Messgeräts ausdrücklich zurücksetzt, anstatt die Änderungen stillschweigend zu überspringen.
lädt einen konsistenten, vollständig beladenen Snapshot des gewünschten Zustands ein und installiert die
Schnappschuss und sein Kontrollpunkt atomar, bevor die inkrementelle Zufuhr fortgesetzt wird.
Die Daten werden in der Datenbank ETag und WebSocket eingegeben.
von v1.

Die Reset-Antwort ist HTTP `410` mit
`type=ControlCursorExpiredError`, `error=control_cursor_pruned`, ein eingegeben
`reason` von `retention`, `generation_mismatch`, `scope_mismatch` oder
`schema_mismatch`, und der Strom `snapshot_generation`.
`Cache-Control: no-store`. Dies spiegelt die öffentliche Messenger Cursor-Lücke
HTTP `409` bleibt verfügbar
für normale Zustandskonflikte/precondition, und ein Reset wird niemals als
erfolgreicher leerer Charge.

Die Änderungen des Kontrollflugzeugs werden genau sieben Tage lang aufbewahrt und dann
Die Aufbewahrung gilt nur für das inkrementelle Tagebuch: aktuell
Externe Konten, verschlüsselte Anmeldeumschläge, Chat-Aufträge, Anbieter
Die Politiken der Identität und der Brückenstaat bleiben in ihren autoritären Modellen.
die Brückenverbindung für höchstens sieben Tage offline ist, kann normalerweise schrittweise aufholt werden;
Ein älterer Cursor verwendet denselben Weg zur Wiederherstellung von vollständigen Momentaufnahmen.

Die Brücke gibt den beobachteten Zustand durch
`POST /v1/observed-state/reports` in Chargen von höchstens 500 Stück.
hat eine vom Client erzeugte `report_uuid`, Ressourcenidentität, beobachtete gewünschte
Die Daten sind in der Regel in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, die in der Form von Daten, in der Form von Daten, in der
`report_uuid` ist ein No-op, und eine veraltete beobachtete Generation kann nicht überschreiben eine
Das Backend akzeptiert Berichte, bevor es antwortet.
Also versuch es erneut, wenn du keine Antwort hast.

Die Anbieterfindung verwendet die eingeteilte beobachtete Ressource `external_chat_catalog`.
Jeder Katalogartikel ist ein `upsert` oder `delete` Grabstein an den aktuellen
die Kontoinhaber, der Anbieter und
Die Standard-Projekt-Identitäten plus eine gesäuberte Chat-Referenz.
Weist Eigentums- oder Erzeugungsunterschiede zurück, bewahrt einen stabilen Workspace Chat
UUID für jeden Provider Chat-Schlüssel, und ordnet kontinuierlich neue Elemente, wenn die
Nutzung des Kontos `selection_mode=all`.

Die Berichtsmengen erlauben eine teilweise Annahme.HTTP `200`Die Antwort ist
ein Ergebnis für jede `report_uuid` in der Anforderungsreihenfolge mit Status `applied`,
`duplicate`, `stale` oder `rejected` und ein optionaler begrenzter sicherer Fehler.
Artikel blockiert keine gültigen unabhängigen Elemente. mTLS-Fehler oder ungültige Charge
Umschlag/schemaSie gibt die entsprechende`4xx`Die Brücke ist ein sehr schwieriges Projekt.
entfernt nur `applied`, `duplicate` und absichtlich `stale`
Berichte aus dem dauerhaften Outbox; ein wieder ausprobierbarer `rejected` Posten bleibt in der Warteschlange.

Lebendigkeit und Fähigkeiten verwenden die separate Leichtgewicht
`PUT /v1/bridge-instances/self/heartbeat` Endpunkt: Herzschlag nicht
Abhängig von der Rechnung/chatDie Arbeit ist verfügbar und trägt nie pro Konto
Die Brücke sendet sie alle zehn Sekunden.
Die Backend-Empfangszeit ist verbindlich; eine Brücke wird nach 30 `degraded`
Sekunden ohne Herzschlag und `offline` nach 60 Sekunden.
Ein späterer gültiger Herzschlag erholt die Gesundheit, es sei denn, die
die Identität administrativ ausgesetzt oder widerrufen wird.

Jeder Herzschlag funktioniert unter demAPIAusgewählt durch seine`/v1` URLund
Die Zertifikatsanbieter-Art, die benannten Fähigkeiten und die relevanten
Die Kommission hat die Kommission aufgefordert, die in den letzten Jahren durchgeführten Untersuchungen zu überprüfen.
Namen sind Chat-Katalog, Nachrichtensendung/edit/delete/read, Mitgliedschaft schreiben,
Das Backend berechnet die fehlerhafte Schnittstelle
Die Daten werden von der Instance übertragen und nur die von ihr unterstützten Ressourcen und Operationen emittieren.
Zuordnung, die eine fehlende Fähigkeit erfordert, wird `unsupported_capability`
Die Daten werden von den Betreibern mit einer sicheren Erklärung übermittelt; das Backend versucht nie, eine optimistische Lieferung zu ermöglichen.
Semver bleibt diagnostisch und wird nicht als Ersatz für Fähigkeiten verwendet.

Die Herzschlagdrahtdarstellung ist ein JSON Objekt, das durch stabile Namespaced
Jeder Wert ist ein Deskriptor, der die Fähigkeit enthält
`revision` und ein fähigkeitsspezifisches `limits` Objekt, z. B.
`{"messenger.message.edit": {"revision": 1, "limits": {}}}`. Das Backend
berücksichtigt nur anerkannte Namen und schneidet numerische oder aufgelistete Grenzen
Unbekannte Fähigkeitsnamen werden ignoriert, anstatt als Beweis behandelt zu werden.
dass eine Operation unterstützt wird.

Eine Fähigkeit `revision` ist eine positive, monotonisch rückwärtskompatible
Backend-Anforderungen deklarieren `min_revision`; ein Bridge-Descriptor ist
die in Artikel 1 Absatz 1 Buchstabe b genannten Vorschriften nicht mit dem Gemeinschaftsrecht vereinbar sind, wenn die Änderung dieser Richtlinie dieser Mindestmenge entspricht oder überschreitet, sofern
Die meisten der beiden Modelle werden in der Regel in der Regel mit einer
Eine semantische Änderung mit einem neuen
Name der Fähigkeit; eine bestehende Revision wird niemals neu definiert oder wiederverwendet.

Die private API wird nur von der Hauptkomponente in ihrer URL (`/v1`) verändert;
Es gibt keine Unterversion. Die Clients müssen unbekannte JSON Objekte ignorieren
Die Daten werden in den folgenden Bereichen erfasst:
Das Verhalten wird explizit über die benannte Fähigkeitsschnittstelle verhandelt.
nicht aus einer Bildversion oder einem impliziten Schema-Minor abgeleitet.
Die Bedeutung und der Typ können sich nicht innerhalb von `/v1` ändern;
Wahlfach erforderlich benötigt einen neuen API -Major.

Workspace gehörende interne Ressourcen:

- die gewünschte Kontoerstellung und die gereinigten Einstellungen;
- Verschlüsselte Anmeldeumschläge, die mit dem Konto UUID, dem Bereich UUID, dem Algorithmus verknüpft sind,
  Schlüsselversion und zugehörige Daten;
- ausgewählte Chat/project-Zuteilungen und Historiendurchläufe;
- Versionen-basiertes, benutzerdefiniertes CA-Bündel;
- Überbrückungsfähigkeit, Herzschlag, Fortschrittsberichte und Sicherheitsfehlerberichte;
- idempotent Anerkennung durch Befehl UUID und gewünschte Erzeugung.

Die Brücke veröffentlicht ihren realm-bound-verschlüsselten öffentlichen Schlüssel über
Nur die bridgediskte Festplatte hält die
entsprechender privater Schlüssel; Workspace PostgreSQL speichert nur verschlüsselt
Die Brücke entschlüsselt ein Anmeldezeichen nur lokal, wenn sie anrufen muss
Die Plaintext-Anmeldeinformationen werden in den Kontrollantworten nie angezeigt.
Workspace -protokolle oder brückenartige Betriebstabellen.

Die exakte interne OpenAPI wird als separates Vertragsartefakt aufbewahrt.
enthält mTLS-Identität, Wiedergabe-Schutz, Monotonie der Erzeugung, Anforderung
Die Kommission hat die Kommission aufgefordert, die
Tests.

## 8. Nachricht und Ereignisdatenebene

Die Provider-Datenebene ist eine private Brücke, die HTTP API authentifiziert ist und an
`/api/workspace-provider/v1`. PostgreSQL ist kanonische Messenger Speicherung und die
die request-owned RESTAlchemy-Transaktion ist die einzige Commit-Grenze. IMAP, SMTP,
Postfächer und MIME-Nachrichten sind nicht Teil der Provider-Synchronisation.

- Die Backend-to-Bridge-Operationen werden in FIFO-Reihenfolge von einem dauerhaften
  PostgreSQL Warteschlange.
  Arbeitnehmer verwenden `SKIP LOCKED` ohne einen Anspruch zu duplizieren.
- Die Daten werden in einer Reihe von Modulen erfasst, die die Daten über die
  Die Daten werden von der mTLS-Identität überprüft, die Ereignis-UUIDs werden
  Die Daten werden vor der kanonischen Mutation entdupliziert, und ein abgelehnter Artikel rollt die
  Vollständige Charge.
- Canonical Nachrichten erstellen, aktualisieren, löschen und ungelesenen Invalidierung Ereignisse verwenden
  Die Daten sind in der Regel in einer Reihe von E-Mail-Daten enthalten.
  Die Daten werden nicht durch die logische Mutation und betroffene Einheiten, sondern durch die Strommemberschaft erfasst.
- Workspace verbindliche Änderungen an den vom Anbieter unterstützten Kanalströmen
  Fähigkeits-gewächter`membership.add`Und ...`membership.remove`Die
  Zulip Brücke löst die abgebildeten Identität und verwendet das offizielle Abonnement
  APIDiese Mutationen sind normale, dauerhafte, sichere Chat-Lane-Operationen.
  native Bindungen werden nie in die Anbieterwarteschlange eingegeben.
- Ein ausgewählter Kanal, dessen Teilnehmerprojektion `ready` ist, wird förderfähig
  für einen begrenzten Teilnehmer nach 30 Sekunden erneut überprüfen.
  Abonnenten-Satz wird über den vorhandenen Katalog gemeldet/desired-state
  Handshake, mit dem vor der Zeile Workspace Identitäten und Bindungen aktualisiert werden
  wird auf `ready` zurückgegeben.
- Die Ergebnisse des Terminalanbieters werden durch `result_uuid` gepaart, idempotent und zurückgegeben
  Ein veralteter Mietvertrag kann nicht die wiedervermieteten Arbeiten abschließen.
- Betrieb UUID, Anbieterkonto UUID, Anbieter-Einheit-ID, Anbieterrevision,
  Die Daten über die Datenverarbeitung und die Daten über die Datenverarbeitung werden in der Datenbank der Kommission veröffentlicht.
  Die Rohlieferanten bleiben intern.

Das Leasing erfordert, dass der bestehende Herzschlag des Kontrollflugzeugs gesund und nicht
Bekannte Betriebsarten werden nur verpachtet, wenn die aktuelle
Die Herztätigkeit wird durch die Herztätigkeit bekannt gemacht.
`PUT /v1/bridge-instances/self/heartbeat` und nicht in
die Datenebene API.

Die Bestellung verwendet Kausalspuren, die von externen Konten und Chat erfasst werden/entity.
Konfliktbehaftete Operationen für eine Einheit werden in Folge ausgeführt, während unabhängige Chats
Die Brücke liefert erst nach dem
Workspace, HTTP, Steuerung und brücke-lokale Hopps
Die gleiche Operation entfalten.UUID. ZulipDie Erstellung der Nachrichten wird von der
Anbieter-spezifische Vereinbarungsrichtlinie unten, da Zulip die
Der Client `local_id` als idempotency-Schlüssel.

Nach einem zweideutigen Zulip Senden Ergebnis, die Brücke erst verzögert,
Es fragt nach der genauen Zielkonversation
Neueste zuerst, begrenzt sich auf das aktuelle externe Konto als Absender, Rohanfragen
Markdown, und vergleicht das Ziel, kanonische Nutzlast, Anhänge, und begrenzt
Ein oder mehrere exakte Übereinstimmungen bestätigen den ursprünglichen Versand;
Die Brücke wählt den Kandidaten, der dem ersten Versuch am nächsten ist.
Die Daten werden von der niedrigsten numerischen Anbieter-Nachrichten-ID gesendet und nicht erneut gesendet.
Die Zahl der Kandidaten wird nur als Vergleichsbeweis aufbewahrt.
Absichtlich identische Nachrichten aus demselben Konto in demselben
Die Daten sind in der Regel in einem anderen System als in einem anderen eingesetzt.
- Wir kommen zu einem .ZulipWenn bei wiederholten Kontrollen keine Übereinstimmung gefunden wird, kann die Brücke
Nicht verfügbar oder zweite zweideutige
Ergebnis erfordern `manual_reconciliation_required`; kein weiterer automatischer erneuter Versand
ist erlaubt.

Der genaue Vertrag über die Übertragung wird in
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

## 9. Binärer Dateitransfer-Flugzeug

Die Brücke kann keine MIME Anhänge verwenden und kann keine S3-Backet-weite Anhänge empfangen
Die genehmigte Grenze für die Binärübertragung ist ein separates internes MTLS
Datei API mit kurzlebigen, einobjektisch vorgegebenen URLs.

Einfallender Provider-Dateifluss:

1. Die Brücke fordert eine Zuweisung mit externem Konto UUID, Chat UUID,
   OperationUUID, Name, Größe, Inhaltstyp und erwarteter Hash.
2. Das Backend validiert die Zuordnung und gibt eine kurzlebige Presigned URL zurück
   Das erlaubt `PUT` für genau ein ausstehendes Objekt.
3. Die Brücke lädt Bytes hoch und die Anrufe werden abgeschlossen.
4. Das Backend überprüft Größe und Hash, erstellt atomar den JSON Sidecar und
   aktuell ACL, setzt die Dateiprojektion ein und gibt eine Workspace URN zurück.

Ausgehender Workspace Dateifluss:

1. Die Brücke übermittelt die autorisierte Workspace URN, externe Rechnung UUID,
   Chat UUID und Operation UUID.
2. Das Backend berechnet den aktuellen Zugriff aus der Chat/stream-Zuteilung und
   Gibt eine kurzlebige vorgegebene `GET` URL für genau dieses Objekt zurück.
3. Die Brücke lädt die Bytes herunter und kopiert sie in den Speicher des Providers.

Die Brücke kann keine Sidecars und ACLs erstellen oder ändern, die Liste des Eimers auflisten oder wiederverwenden
a URLDas Backend reinigt abgelaufene teilweise Zuweisungen und
Sie löscht sofort die kopierten Dateien des Anbieters, wenn eine Projektion entfernt wird.

Vorgegebene `PUT` und `GET` URLs verfallen nach fünf Minuten.
Die Bearbeitung der Daten erfordert eine angegebene Größe, einen Inhaltstyp undSHA-256; die Fertigstellung fehlschlägt
Die Zuteilung/finalize ist für die gleiche
Die Daten werden von der
Der Backend-Reinigungskräfte.

Dieser Dienst erlaubt nur binäre Dateibytes; die Bytes fließen direkt zwischen
Die Daten werden in der Datenbank von der E-Mail-Datenbank (e-mail-Datenbank) gespeichert.
private Provider HTTP API, und die resultierende Nachricht enthält nur die zurückgegebene
URN.

## 10. Grenze der Speicherung und der Bereitstellung

### Workspace Backend

- Neue real-scoped externe Konto, verschlüsselte Anmeldeinformationen, Chat-Zuteilung,
  Anbieter-Mapping, gewünschter/actual Zustand und Projektionen für externe Operationen.
- Eine stabile Region /installation UUID.
  In diesem Bereich ist die Einzigartigkeit `(owner_user_uuid, provider_kind)` in dieser Datenbank.
- Ein Projekt-Movement-Koordinator, der das kanonische alte/new Projektjournal schreibt
  Übergangsvorgänge bei Bewahrung der Entity-UUIDs.
- IAM Lebenszyklusausgleich zusätzlich zur aktuellen faulen Benutzerentdeckung.
- Private MTLS-Kontrollen, Providerdaten und Dateierendpunkte.
- öffentliche Serialisierer, die `provider` und `delivery` für Streams speichern,
  Themen, Nachrichten, Dateien und Nebenprojektionen.

### `workspace-zulip-bridge` Element

Die Brücke ist ein neues Repository und ein unabhängiges Exordos-Element mit einer VM,
Eine auswechselbare Stammplatte und eine dauerhafte Datentaste.
Die Route ist bloß.

Der anhaltende Zustand umfasst:

- real-bound-Crypting-Schlüsselversionen und Steuerungsebene-Identität;
- Dauerhafte Ausbox/inbox Deduplizierung und Anbieter HTTP Leasingstatus;
- Zulip Warteschlange und Cursoren;
- Anbieter-zu-Workspace-Zusammenstellungen und Fortschritte bei der Rückfüllung;
- Zeitplaner-Leasingverträge und tatsächliche Zustandberichte.

Für das Zielprofil von 1.000 Accounts, 50.000 Chats, 50 Millionen projiziert
Nachrichten und 100 Nachrichten/second, sollte das Betriebslager ein lokales
- Unfallsicher .PostgreSQLEs bleibt sekundär zu
der kanonische Workspace PostgreSQL und S3-Zustand.

Die Brücke verwendet einen fairen Zeitplaner mit strenger Live-Priorität, dann wieder ausprobierbar
Ausgangsaktenarbeiten, dann eine faire Rückfüllung pro Konto.ZulipDie Zinsgrenzen sind verbindlich.

### Sichere Aktualisierungen

- Workspace und Brückenelemente sind vor Ort aktualisiert.
  die in der Betriebsanlage entfernt oder neu eingesetzt wurden.
- Jedes Entwicklungsbild verwendet eine neue unveränderliche Version.
- Feststehende Festplattenidentität, Knotenableitung, Festplattenfolge und Datenetiketten bleiben erhalten
  Stabil über Upgrades hinweg.
- Bootstrap-Fehler bei fehlender /partial Schlüssel-Zustand oder -Reich-Missverhältnis geschlossen.
- Warteschlangen, Mappings und die lokale Datenbank müssen sich nach einem Hard VM-Stopp wiederherstellen.
  weil der aktuelle Kern-Bild-Austausch kein schönes Abschalten garantiert.
- Die Aktivierung erfolgt stufenweise und fähigkeitsgerichtet: Backend-APIs und die Brücke werden
  mit dem Anbieter eingesetzt, dann Einschreibung, gesunder Herzschlag,
  und die erforderliche Fähigkeitskreuzung werden vor einem Bereich Admin überprüft
  ermöglicht Zulip.
- Rollback setzt die Anbieter-Synchronisierung aus und bewahrt dauerhafte Warteschlangen und
  Es wird nicht ein Element deinstallieren, löschen persistent
  Staat, oder unterbrechen nativeWorkspaceNachrichten.

## 11. Grenze der UI

- Ersetzen Sie die versteckte Funktion für die externen Konten; machen Sie sie nicht sichtbar oder
  Kompatibilitätsanrufe an die alten Endpunkte hinzufügen.
- Hinzufügen einer Messenger Einstellungsseite für Zulip Anmeldeinformationen, Chat-Auswahl oder `all`,
  Die Geschichte, die Projektzuweisung, der Fortschritt,`Disconnect`, und zerstörerisch
  `Delete`.
- Speichern von Anbietern Metadaten über Stream, Thema, Nachricht, Datei und zwischengespeichert
  Sekundärprognosen.
- Verwenden Sie kompakte interaktive Anbieter-Badges mit Account/status Popovers und einem
  Originalverbindung, soweit verfügbar.
- Zentralisieren von Entscheidungen über die Fähigkeit des Komponisten, bearbeiten/delete, Akten,
  Umbenennen/move, Antworten und Verlustvorflug.
- Der lokale Browser-Transportzustand (`sending`/`failed`/`sent`) ist getrennt von
  der autorisierte Zustand der externen Operation (`pending`/`delivered`/`failed`).
- Senden Sie niemals Anbieter-Zugriffsdaten an IndexedDB, Logs, Analytics oder die Brücke
  von dem Browser.

## 12. Technische Zersetzung

1. **Vertragsgründung**: genehmigen Sie diese öffentliche Grenze, die realm-admin
   die privaten Kontroll- und Anbieterdaten OpenAPI und
   das Schema des verschlüsselten Umschlags.
2. **Workspace Datenmodell**: Ersetzen Sie den veralteten projektspezifischen Kontorückstand;
   Implementieren von verschlüsselten Anmeldeinformationen, Zuordnungen, Verknüpfungen, Betriebszustand,
   IAM Lebenszyklus und Projekt-Movement-Koordination.
3. **Workspace Protokollgrenze**: Hinzufügen der Identität des MTLS-Anbieters, dauerhaft
   Anbieter HTTP Outbox/ingress, interne Kontroll/file und Exporte
   von dem Brückenelement erforderlich.
4. **Projektion und Echtzeitparität**: Metadaten des Anbieters/delivery ausfüllen,
   externe Identitäten, vollständige Momentaufnahmen, Cache-Rücksetzungsverhalten und
   Benachrichtigungsgate.
5. **Bridge Element Foundation**: Erstellen Sie das separate Repository, Manifest,
   Dauerhafte Bootstrap, abstürzungssichere Betriebsfähigkeit PostgreSQL, Einschreibung in mTLS,
   und Versöhnungsschleife.
6. **Zulip-Konnektor**: Kontovalidierung, Katalog, Live-Warteschlange, dauerhafte Outbox,
   Neuer erst-zurückfüllung, Umwandlung, Konfliktregeln und Preisgrenz-bewusste Messe
   Zeitplanung.
7. **UI**: normalisierte Cache-First-Domain, Verbindungsassistenten, Auswahl/project
   Flüsse, Abzeichen/popovers, externe Identitäten, Fähigkeiten, Vorflug und
   Wiederholen Sie das Verhalten /discard.
8. **Annahme**: Vertragsprüfungen, Integration von tatsächlichen Anbietern HTTP und Zulip,
   Daten ACL Prüfungen, Absturz/root-replacement Wiederherstellung, Lastprüfungen, Sicherheitselement
   Die neue Version des Programms wird von der "Dramatiker-Akzeptanz" unterstützt.

## 13. Akzeptanzmatrix

Die Funktion ist erst abgeschlossen, wenn alle folgenden Merkmale überprüft wurden:

- Konto erstellen/reconnect/disconnect/delete und IAM deaktivieren/delete;
- Einzigartigkeit eines Kontos pro Anbieter und nur zu schreibende Anmeldeinformationen;
- explizite Auswahl, dynamische `all`, alle Historiendieftungsmodi und new-chat
  Zuordnung;
- Kanal/topic, persönliche DM und Gruppen-DM-Mapping;
- Erstellen/edit/delete/read/mentions/replies/quotes/Markdown/links/files/images
  in beide Richtungen;
- die Umbenennung in beide Richtungen, wenn sie von den Fähigkeiten beworben wird;
- Rückfallverlust, ursprüngliche Verbindung und Ausgangserklärung;
- Neueste erste Rückfüllung, erste Planung, Benachrichtigungsgate und p95
  Ziel für die Latenzzeit;
- Wiederholung/backoff, Ablauf von 24 Stunden, Wegwerfen, Absturzwiederherstellung, Deduplizierung und
  Konflikt/delete -Ordnungsweise;
- Projektbewegung mit stabilem UUID/history/read-Zustand und korrektem alten/new-Projekt
  Ereignisse;
- Auswählung/access-loss sofortige Reinigung von Projektionen und kopierten Dateien;
- Anbieter-Metadatenparität in REST, Echtzeit, IndexedDB und Sekundäransichten;
- Außenidentität und `urn:user:*` Verhalten ohne IAM-Ausnutzung;
- Benutzerdefinierte CA-Validierung, Hostname-Verifizierung, mTLS-Kontrolle, am wenigsten privilegierte
  Zugang zum Anbieter API und fehlender globaler Zugang S3;
- nur für den Eigentümer und nur für die gesammelte Region - Admin-Gesundheit;
- 1.000 Konten / 50.000 Chats / 50 Millionen Nachrichten / 100 Nachrichten pro Sekunde
  Lastprofil;
- sichere unabhängige Brückenaktualisierung mit erhaltenem Dauerzustand;
- Sichtbare Annahme durch den Dramatiker über das normale `cassi` Workspace Konto.

## 14. Verarbeitete Vertragsartefakte und verbliebene Tore

Die hochrangige Produkt- und API Grenze wird genehmigt.
KontrolleOpenAPI, AnbieterHTTP OpenAPI, und interne AkteAPIsind
Einträge, die vor der Prüfung einen Vertrag, eine Kompatibilität und eine Laufzeitvalidierung erfordern
- Wir haben die Reichsberechtigung.

Diese vollendeten Artefakte sind Implementierungseinträge, nicht Restveröffentlichung
Die Realm-Aktivierung erfordert immer noch jedes Szenario in der
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md), einschließlich
Ausdrückliche Richtlinienberechtigung, realer bidirektionale Workspace Anbieter HTTP/file
und Zulip 12.1.1 Verkehr, Zertifikatsrotation, Wiederherstellung und Ziellast
Schecks und voll sichtbare Dramatiker-Annahme.
