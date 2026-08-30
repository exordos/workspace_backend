# Zulip Testplan für die Brücke V1

Dieser Plan ist das ausführbare Akzeptanztor für den anbieterneutralen Workspace
Die Kommission hat die Kommission aufgefordert, die`zulip`Die Kommission hat die
Der Einheimische .MessengerRegressionsplan; native Nachrichtenübermittlung,IAM, Echtzeit undS3
Das Verhalten muss während des gesamten Einsatzes grün bleiben.

Canonical Messenger Ressourcen und Provider-Befehle leben in PostgreSQL und sind
- Über den Privaten .WorkspaceAnbieterAPIDieser Plan muss ausgeführt werden .
mit den Rückgewinnungs- und Ladegächten in
[`messenger_regression_test_plan.md`](messenger_regression_test_plan.md).

## Notwendige Umgebung

- Workspace Backend aktualisiert mit seinen kanonischen PostgreSQL und S3 Daten
  - Sie ist erhalten.
- Ein unabhängig aktualisierbarer `workspace-zulip-bridge` Knoten mit einem austauschbaren
  eine Stammplatte und eine permanente Betriebsdatenplatte.
- Ein spezieller Zulip Testbereich mit mindestens vier Benutzern, ein Kanal mit zwei
  Die Kommission hat die Kommission aufgefordert, die in den letzten Jahren vorgenommenen Maßnahmen zu prüfen.
- S3-kompatible Workspace-Speicher und der private Workspace-Anbieter API.
- Der normale `cassi` Workspace Konto in einem sichtbaren Playwright MCP Fenster.
- Alle fünfzehn Berechtigungsressourcen für die externe Integration IAM und beide vordefinierten
  Die erforderlichen Rollen werden aus dem Workspace -Element-Manifest zusammengefasst.
  - Ich bin hier .`cassi`Nur in der ...WorkspaceIAM- Das ist normal .Workspace
  Die Rolle darf diese Berechtigungen nicht implizit erhalten.

## Deterministische sichtbare Benutzeroberfläche

Erstellen Sie jede Einrichtung mit einem eindeutigen Run-Suffix und speichern Sie die generierten UUIDs in
Das ist das externe Test-Archiv, nicht in diesem Repository.Workspaceund
Zulip für die gesamte Reise in separaten sichtbaren Tabs MCP geöffnet.
Primär Workspace Sitzung ist immer das reale `cassi` Konto; Einwegbenutzer
Es gibt nur für die Ausübung von Multi-User- und destruktiven Lebenszyklussituationen.

### Rechnungen und Projekte

| Einrichtung                                   | Erforderlicher Zustand                                                                                                 | Zweck                                                                     |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Workspace `cassi`                         | Aktiver Bereichsadministrator mit den Richtlinienberechtigungen für externe Anbieter und Mitgliedschaft in beiden Testprojekten. | Hauptbesitzer, gewöhnliche Workspace Benutzeroberflächensitzung, Annahme von Richtlinien/health.     |
| Workspace Peer A und Peer B               | Aktive gewöhnliche Nutzer in beiden Testprojekten.                                                                   | Native-Messaging-Regression und Teilnehmer/unread -Checks.                  |
| Workspace Eigentümer des Lebenszyklus                 | Aktiver gewöhnlicher Benutzer mit separater Zulip Kontoanlage.                                                    | IAM deaktivieren /delete-Tests, ohne die primäre `cassi`-Session zu zerstören. |
| Zulip Eigentümer für `cassi`                   | Aktiver menschlicher Account mit einem API Schlüssel.                                                                          | Persönliches externes Konto, das für die Hauptbeziehung verwendet wird.                |
| Zulip Peer A, Peer B und Lebenszykluseigentümer | Aktive menschliche Konten.                                                                                         | Persönliche DM, Gruppen-DM, Erwähnung, Umbenennung und Konto-Lebenszyklus-Fixtures.     |
| Workspace Projekt A und Projekt B         | Beide sind für `cassi` sichtbar; keine enthält bereits bestehende äußere Projektionen.                                   | Erste Zuordnung und Atomprojekt-Bewegung-Akzeptanz.                      |

Verwenden Sie keinen Zulip API Schlüssel zwischen den Workspace Besitzern.
Nur in einem zugelassenen Geheimlager und entfernen oder drehen Sie einweg
Schlüssel nach dem Lauf.

### Zulip Konversations- und Inhaltsmatrix

| Einrichtung            | Vor der Verbindung erforderlicher Inhalt                                                                                              | Zweck                                                                                                           |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Kanal A          | Themen `live` und `history`; Nachrichten, die kürzer als 7 Tage, zwischen 7 und 30 Tagen, zwischen 30 und 90 Tagen und älter als 90 Tage sind. | Explizite Auswahl, jede Geschichte-Tiefe Grenze, neueste-erste Bestellung, Kanal/topic Projektion.                |
| Kanal B          | Mindestens ein Thema und keine Workspace Projektion, wenn das Konto erstmals verbunden wird.                                             | Überprüfen Sie die explizite Ausschluss, dann Auswahl und Projektzuweisung.                                                 |
| Kanal C          | Erstellt erst, wenn `selection_mode=all` live ist.                                                                                | Überprüfen Sie die dynamische Ermittlung und automatische Zuordnung zum Standardprojekt.                                         |
| Persönliche DM        | Zulip Eigentümer und Peer A, mit mindestens einer Nachricht von jeder Seite.                                                               | Privater persönlicher Stream mit genau zwei Teilnehmern und einem Standardthema.                                      |
| DM der Gruppe           | Zulip Eigentümer, Peer A und Peer B, mit mindestens einer Nachricht von jedem Teilnehmer.                                               | Private Gruppenströme mit einem Standardthema und stabilen externen Identitäten.                                       |
| Eingangsdateien     | Eine kleine PNG und eine nicht-Bilddatei im Kanal B mit bekannten Namen, Größen, MIME-Typen und Hashes.                              | Provider-to-Workspace-Kopie, Bildrenderierung, generischer Download, URN-nur-Nachrichteninhalt und Löschung von Auswahlmöglichkeiten. |
| Ausgehende Akten     | Eine kleine PNG und eine nicht-Bilddatei, die durch den Workspace-Komponisten ausgewählt wurden.                                                   | Workspace-zu-Provider-Kopie und Provider-seitige Byte/content-Überprüfung.                                           |
| Verlustbewusste Nachricht | Markdown, das ein unterstütztes Element und ein vom Anbieter nicht unterstütztes Element enthält, das durch den Vorflug abgedeckt wird.                            | Sicherer Fallback, `Open original`, Verlustbeschreibung, Stornierung und ausdrückliche Bestätigung.                                  |

Halten Sie den Kanal A bei Geschichtsverlautbarungen ansonsten ruhig.
Verkehr erst nach Erfassung des erwarteten Rückfüllteils und der Bestellung,
Also kann eine neue Nachricht eine Grenzbehauptung nicht mehrdeutig machen.

## Vertragsgate und Sicherheits-Gate

| Identifizierung              | Szenario                                                                                                                                                                          | Erwartetes Ergebnis                                                                                                                                              |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ZB-CONTRACT-001 | Validieren Sie die öffentliche, Anbieter API, Steuerung und Dateiverträge und lösen Sie jede lokale OpenAPI-Referenz.                                                                                 | Alle Artefakte validieren; die Öffentlichkeit API bleibt OpenAPI 3.0.3 und jeder private Vertrag löst sich ohne eine interne Route für den Browser zu enthüllen.                                                   |
| ZB-SEC-001      | Bootstrap-Control-Vertrauen mit einem neuen Nonce, erwartetem Hostnamen, Bridge-Instanz und Eintragungsgenerierung.                                                                        | Nur die genaue HMAC-authentifizierte CA wird atomar installiert; Weiterleitungen, Wiedergabe, Fehlvergleich, ungültige PEM, übergroße Daten und eine geschlossene Generation scheitern. |
| ZB-SEC-002      | Registrieren, erneuern, aussetzen, wieder aufnehmen und die Identität der Brücke widerrufen.                                                                                                                   | Die Clientschlüssel verlassen nie die Bridge-Disk; jede private Anfrage ist zertifizierungs- und generationsgebunden; der Widerruf ist sofort.                                |
| ZB-SEC-003      | Erstellen und erneut verbinden Sie ein externes Konto, und überprüfen Sie anschließend API Antworten, Protokolle, Ereignisse, Browserspeicher und Betriebstabellen.                                                      | Zulip API-Tasten bleiben schreibswert und verschlüsselt; Klartext erscheint nur im Bridge-Prozess beim Anrufen von Zulip.                                            |
| ZB-SEC-004      | Versuch, den Zugriff auf den globalen Objektspeicher zu erleichtern.                                                                                   | Jede Anfrage wird abgelehnt, ohne dass Konto-Daten, Objektschlüssel, Anmeldeinformationen oder Anbieter-Nutzlasten verbreitet werden.                                                      |
| ZB-SEC-005      | Verschlüsseln Sie eine Anmeldeinformationen für den eingeschriebenen Bridge-Schlüssel X25519, und ändern Sie dann den Eigentümer, den Bereich, den Anbieter, das Konto, die Bridge-Instanz, die Identitätsgenerierung, den Schlüssel UUID, das Schema oder den Empfängerschlüssel. | Das Backend hat keine Entschlüsselungsfähigkeit; nur die genaue eingeschriebene Brückenidentität öffnet den HPKE Umschlag und jede veränderte Bindung schließt nicht.               |

## Konto, Katalog und Steuerungsebene

| Identifizierung             | Szenario                                                                                                                                    | Erwartetes Ergebnis                                                                                                                                                                                                                                |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ZB-ACCOUNT-001 | Erstellen, erneut verbinden, trennen und löschen Sie ein Zulip Konto.                                                                                  | Der Besitzer erhält den gesäuberten Zustand; ein zweites Konto desselben Anbieters wird abgelehnt; die Trennung behält eine nur-lesbare Projektion und das Löschen löscht sie.                                                                                         |
| ZB-ACCOUNT-002 | Deaktivieren und löschen Sie den IAM-Besitzer.                                                                                                        | Deaktivierung stoppt die Synchronisation ohne zerstörerischen Verlust; Löschung entfernt das Konto, Projektion, Warteschlange und kopierte Dateien gemäß dem genehmigten Lebenszyklus.                                                                                  |
| ZB-CATALOG-001 | Wählen Sie explizite Chats aus und wählen Sie dann `all`.                                                                                                | Explizite Auswahl importiert nur ausgewählte Chats; `all` weist auch neue später entdeckte Provider-Chats zu.                                                                                                                                          |
| ZB-CATALOG-002 | Üben Sie jeden Geschichtsmodus.                                                                                                          | Die Rückfüllung erfolgt neuesten erst bis zur ausgewählten Grenze, während die Live-Arbeit strenge Priorität behält.                                                                                                                                               |
| ZB-CATALOG-003 | Wählen Sie auf einem neuen PostgreSQL Schema einen Chat aus, vereinbaren Sie seinen Backfill-Job, löschen Sie die Zulip Ereigniswarteschlange und erstellen Sie Warteschlange.   | JSON-extraktionierte Konto-UUIDs werden in beiden Pfaden auf den Typ PostgreSQL UUID geworfen; normale Rückfüllung und Wiederherstellung von Warteschlange schaffen ihre dauerhaften Jobs ohne Datentypfehler.                                                                 |
| ZB-CATALOG-004 | Abschließen der Rückfüllung, Löschen der Chat-Auswahl, und dann erneut wählen Sie den gleichen Chat mit der gleichen Historie Tiefe unter einer neueren Zuordnung Generation.          | Die gelöschte Aufgabe wird als ausstehende neu gestartet, die Wiederimporte-Operationsidentitäten sind generationsbezogen und die vorherige Deduplizierung kann den neuen Import nicht unterdrücken.                                                                                            |
| ZB-CATALOG-005 | Die in Artikel 4 Absatz 1 Buchstabe b der Richtlinie 2009/138/EG genannten Risikopositionen werden in den folgenden Kategorien erfasst:           | Das Backend lehnt die Topologie ab, bevor entweder der Chat oder die gewünschte Zuordnung fortgesetzt wird.         |
| ZB-CATALOG-006 | Verbindet zwei Workspace-Konten mit verschiedenen authentifizierten Benutzern im gleichen Zulip-Reich und meldet überlappende Teilnehmer.               | Jeder nicht-eigentümerische Teilnehmer wird um `(provider, realm_uuid, user_id)` auf einen gemeinsamen Workspace UUID aufgelöst; die im Kontobereich enthaltenen Duplikate und ihre Verweise werden zusammengeführt und gelöscht.                                                                    |
| ZB-CATALOG-007 | Verbinden Sie einen Eigentümer von IAM, dessen authentifizierter Zulip Benutzer bereits als externe Identität existiert, und versuchen Sie dann, die gleiche Zulip Identität von einem anderen Eigentümer von IAM zu fordern. | Die erste verifizierte Kontoverbindung verschmelzt die externe Identität in die bestehende IAM UUID, ohne die Konto-Scope-Streambindungen oder den ungelesenen Zustand eines anderen Besitzers zu erben. |
| ZB-CATALOG-008 | Entfernen Sie ein externes Konto, das sowohl verweisende als auch nicht verweisende Legacy-Benutzerzeilen Zulip hinterlässt, und fügen Sie dann einen Katalog ab.                     | Nicht verweisende alte Zeilen werden gelöscht; Zeilen, die noch durch den Messenger-Zustand verwiesen werden, werden beibehalten, bis sie sicher zusammengeführt werden können.                                                                                                                   |
| ZB-CONTROL-001 | Befragung der gewünschten Änderungen, Bericht über den beobachteten Zustand in partiellen Ergebnisbatches, Ablauf eines Cursors und Wiederherstellung durch einen logischen Snapshot.             | Cursors sind monoton und umfanggebunden; eingegeben `410` löst eine konsistente Momentaufnahme aus und keine gewünschte Änderung wird übersprungen.                                                                                                                            |
| ZB-CONTROL-002 | Veränderung der Fähigkeiten und Revisionen über Herzschlagzyklen hinweg.                                                                                  | Nur die fehlergeschlossene effektive Kreuzung ist aktiviert; inkompatible Chargen bringen den Cursor nicht voran und erholen sich nach einem kompatiblen Herzschlag automatisch.                                                                                 |
| ZB-CONTROL-003 | Ein Ressourcenspektrum beschneiden, wobei unabhängige Identitäts- und Ressourcentypensequenzen spärlich bleiben, einschließlich eines Bereichs ohne spätere Zeile. | Per-Scope-Pruned-Through-Watermarker geben nur für einen tatsächlich abgelaufenen Cursor eingegeben `410` zurück; unabhängige Sequenzlücken zwingen nie einen Snapshot.                                                                                                    |
| ZB-CONTROL-004 | Mutation eines öffentlichen externen Kontos/chat und Umfrage des privaten gewünschten Feeds in derselben Transaktionsgrenze.                                   | Der private Feed und der Snapshot lesen die PostgreSQL Wahrheitskilde, zeigen verpflichtete vollständige Ersatz- oder Grabsteine und hängen nie vom nodal-lokalen JSON-Zustand ab.                                                                                |
| ZB-CONTROL-005 | Beobachten Sie die gewünschte Umfrage im Leerlauf, dann injizieren Sie Netzwerkausfall, HTTP `429` und wieder ausprobierte `5xx` Antworten.                                       | Genau eine Umfrage ist hervorragend; gesunde Umfragen warten zwei Sekunden; Wiederholungen verwenden eine Sekunde Basis exponentielle Backoff mit voller Jitter bis zu 30 Sekunden, ehren begrenzt `Retry-After`, halten den engagierten Cursor unverändert und zurücksetzen nach Erfolg. |
| ZB-CONTROL-006 | Wiederherstellen eines leeren Cursors durch einen vollständigen Snapshot mit mindestens 15.000 Zuordnungen mit großen Teilnehmer/topic Katalogen.              | Die Schnappschuss-Erstellung speichert normalisierte geordnete Zeilen, ohne eine in-Process-Sammlung zu erstellen; jede Seite liest nur ihre begrenzten Zeilen, das Backend RSS bleibt begrenzt und die Brücke installiert jede Ressource genau einmal, bevor sie den Anker-Cursor vorantreibt. |

## Nachricht, Identität und Dateifläche

| Identifizierung          | Szenario                                                                                                                                                                                                            | Erwartetes Ergebnis                                                                                                                                                                                                                                                                                                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ZB-MSG-001  | Import Zulip Kanäle/topics, persönliche DMs und Gruppen-DMs.                                                                                                                                                          | Sie werden mit den normalen Workspace Streams/topics mit stabilen Anbieter-Metadaten und den genehmigten Datenschutz/member Regeln abgeglichen.                                                                                                                                                                                                                                                    |
| ZB-MSG-002  | Erstellen, bearbeiten, löschen, antworten, zitieren, erwähnen und lesen in beide Richtungen.                                                                                                                                      | Unterstützte Operationen konvergieren einmal, bewahren stabile IDs, verwenden `urn:user:<uuid>` für Workspace-Erwähnungen und legen den Provider-Verlust/capability-Zustand offen.                                                                                                                                                                                                                     |
| ZB-MSG-003  | Verlieren Sie die Zulip-Send-Antwort, während das entsprechende lokale Echo-Ereignis verfügbar bleibt.                                                                                                                                 | Die Korrelation mit der anhaltenden Warteschlange /local bestätigt die Anbietermeldung-ID; die Operation wird einmal ausgeliefert und nie erneut gesendet.                                                                                                                                                                                                                                              |
| ZB-MSG-004  | Abhängige und unabhängige Operationen.                                                                                                                                                                    | Eine Kausalspur bleibt seriell, während unabhängige Chats gleichzeitig voranschreiten; die letzte bestätigte Operation gewinnt.                                                                                                                                                                                                                                                      |
| ZB-MSG-005  | Verlieren Sie sowohl die Zulip Send Response als auch das lokale Echo-Ereignis, und legen Sie dann eine oder mehrere exakt übereinstimmende Nachrichten durch die Historie freizulegen.                                                                                         | Verzögerte, wiederholte Historienabstimmung wird auf den genauen Gesprächspartner und den aktuellen Absender beschränkt, vergleicht die rohe kanonische Nutzlast und Zeit, wählt den Kandidaten, der dem ersten Versuch am nächsten ist, mit der niedrigsten numerischen ID als Tie-Breaker, erfasst die Kandidatenzahl und sendet nicht erneut.ZulipNachricht. |
| ZB-MSG-006  | Nach einem zweideutigen Zulip -Send wird keine entsprechende Historiemeldung zurückgegeben.                                                                                                                                                   | Die Brücke wird höchstens einmal automatisch erneut gesendet; ein bestätigter zweiter Versuch konvergiert zu einer Anbieternachricht und bewahrt die Übereinstimmungsbeweise.                                                                                                                                                                                                                    |
| ZB-MSG-007  | Vermeiden Sie die Verfügbarkeit des Verlaufs oder verlieren Sie das Ergebnis des erlaubten automatischen Wiederversendens ohne spätere genaue Übereinstimmung.                                                                                                    | Die Operation wird `manual_reconciliation_required`; kein weiterer automatischer Wiederversand erfolgt, und die Benutzeroberfläche zeigt einen gesäuberten Kontext, den ursprünglichen Link, wenn er bekannt ist, und eine explizite Warnung vor einem doppelten Risiko vor dem manuellen Wiederholungsversuch.                                                                                                                                                     |
| ZB-MSG-008  | Wählen Sie einen Chat mit `history_depth=new`, bevor eine eingehende Historie oder Kartierung eingetroffen ist, senden Sie dann die erste Workspace-Nachricht und bearbeiten, antworten und löschen Sie sie anschließend.                                    | Der gewünschte Zustand enthält bereits die vollständige Backend-ausgestellte `workspace_projection`; die erste ausgehende Operation führt durch die persistente Stream/topic/participant-Mapping, und jede abhängige Operation wiederverwendet die gleiche Mapping, ohne dass die Brücke eine Workspace UUID erfindet.                                                                                   |
| ZB-MSG-009  | Erhalte die erste Anbieternachricht von einem Autor und einem Thema, die nicht in der aktuellen Zuordnungsgeneration enthalten sind.                                                                                                          | Das Provider-Ereignis bleibt ausstehen, während die Brücke das Katalogdelta meldet; die Lieferung wird erst wieder aufgenommen, nachdem das Backend exakte Teilnehmer/topic UUID-Mapping in einer neueren Zuordnungsgeneration veröffentlicht hat.                                                                                                                                                              |
| ZB-MSG-010  | Benennen Sie ein abgegrenztes Thema um und aktualisieren Sie den Stream-Namen, die Beschreibung oder die Privatsphäre in jeder Richtung, starten Sie dann neu und spielen Sie den gewünschten Snapshot erneut.                                                                                | Das Backend führt eine vollständige Ersatzzuordnung voran, während die UUIDs Workspace erhalten bleiben; Anbieter-IDs und aktuelle Metadaten bleiben verbindlich und das Wiederholen stellt nie alte Werte wieder her.                                                                                                                                                                                     |
| ZB-MSG-011  | Liefern Sie ein globaler `realm_user` Identitätsereignis ohne Chat-Zuteilung.                                                                                                                                    | Der Vorgang wird mit dem Konto-Generation-bound Outbox durchgeführt und erfolgt ohne eine synthetische `account` Chat-Zuteilung oder Abbruch des Provider-Log-Tick.                                                                                                                                                                                                   |
| ZB-MSG-012  | Geben Sie gleich thematischen Nachrichten die gleiche `created_at`, sie so zu zueinander zuordnen Workspace UUID Reihenfolge ist das Gegenteil von Zulip numerische Nachricht-ID-Reihenfolge, und rufen Sie Workspace `read_up_to` in der Mitte Workspace UUID.                 | Workspace löst das native `(created_at, uuid)`-Präfix und serialisiert es als nicht leeren exakten `message_uuids`-Selektor. Canonical PostgreSQL-Zustand und Zulip markieren genau diesen Satz; der Provider interpretiert niemals eine Workspace UUID-Grenze mit Provider-Ordering neu, und die spätere Workspace UUID bleibt überall ungelesen.                                                   |
| ZB-MSG-013  | Markieren Sie einen leeren externen Stream oder Themalesen.                                                                                                                                                                        | Die Aktion ist ein lokales No-Op und gibt keinen Providerbefehl aus; ein leerer `message_uuids` genauer Auswählvorgang wird niemals serialisiert oder unter Quarantäne gestellt.                                                                                                                                                                                                                           |
| ZB-MSG-014  | Verpflichten Sie eine Workspace-Origin-Nachricht, verlieren Sie das Echo der Provider-Warteschlange /local und finden Sie dann die gleiche Provider-Nachricht durch den Aufholverlauf wieder.                                                                          | Der bestehende Alias Provider-to-Workspace wird wiederverwendet oder unterdrückt die Wiedergabe; keine zweite Workspace UUID/message wird erstellt, während eine unvollständig gelieferte Provider-Origin-Projektion wiederherstellbar bleibt.                                                                                                                                                             |
| ZB-MSG-015  | Schlange einen exakten Leseselektor mit abgeglichenen Nachrichten, gefolgt von einer Workspace-Herkunftsnachricht, deren früheres Erstellen ohne Anbieterabgabe beendet wurde.                                                          | Die Brücke wendet die abgebildeten Provider-IDs an und lässt nur das Terminal unmapped UUID weg. Ein Lesen hinter einem noch ausstehenden Create bleibt in derselben Kausalspur nicht zurückforderbar; ein vollständig unmapped Selector ist ein Provider no-op.                                                                                                                                            |
| ZB-MSG-016  | Einladen von externem Stream/read, Thema/read, Nachricht/read und Nachricht/read_up_to mit Workspace UUID-Reihenfolge, die absichtlich der numerischen Anbieternachricht-ID-Reihenfolge entgegengesetzt ist; auch Wiederholung von Stream/topic ohne Nachrichten. | Jede nicht leere Aktion führt zur genauen deterministischen Reihenfolge .Workspace- ausgewählt.UUIDSetzen undZulipAktualisiert nur die entsprechenden Provider-IDs ohne Grenzneuinterpretation./topicDie Aktionen geben keinen Providerbefehl aus.                                                                                                                                     |
| ZB-MSG-017  | Fügen Sie einen zugeordneten Benutzer zu einem vom Provider unterstützten Stream von Workspace hinzu, versuchen Sie den gleichen Provider-Betrieb erneut, entfernen Sie den Benutzer, versuchen Sie erneut die Entfernung und fügen Sie den Benutzer erneut hinzu.                                                           | `membership.add`/`membership.remove` verwenden die verhandelte `messenger.membership.write`-Fähigkeit und das offizielle Zulip-Abonnement API, konvergieren idempotent und enthüllen einen Ausfall des Endgerät-Anbieters als eine erneut ausprobierbare externe Operation. Vor-Beitrittsnachrichten werden gelesen, Nachrichten nach dem Beitritt werden nicht gelesen, und das Entfernen widerruft sofort den Workspace-Meldungszugang /event |
| ZB-MSG-018  | Nachdem ein ausgewählter Kanal den Teilnehmerzustand `ready` erreicht hat, fügen Sie einen Zulip-Abonnenten hinzu, entfernen Sie ihn und entfernen Sie ihn dann und lassen Sie die Zuordnungsgeneration unverändert.                                               | Jede Provideränderung wird durch die Begrenzte Bereitschafts-Wiederprüfung erkannt, über den Katalog-Handschlag gemeldet und konvergiert die Workspace Identität, externe Chat-Teilnehmer, Streambindung und sichtbare Mitgliederliste, ohne das Konto neu zu verbinden oder in einer unbegrenzten Schleife zu wählen.                                                                        |
| ZB-FILE-001 | Übertragen von Bildern und generischen Dateien in beide Richtungen.                                                                                                                                                               | Bytes werden in den empfangenden Speicher kopiert; Workspace Nachrichten enthalten nur URNs; vorgegebene URLs sind ein Objekt und verfallen innerhalb von fünf Minuten.                                                                                                                                                                                                                         |
| ZB-FILE-002 | Manipulation der Größe, des Typs MIME, des Hashes, der Zuweisung, des Seitenwagens und des Stroms ACL.                                                                                                                                 | Die Fertigstellung ist abgeschlossen, Teile werden gereinigt und die Brücke kann keine Seitenwagen erstellen oder eine ganze Menge Anmeldeinformationen erhalten.                                                                                                                                                                                                                                          |
| ZB-FILE-003 | Anfordern Sie eine Ausgangsdatei aus einem anderen Stream im gleichen Projekt oder mit einem Sidecar ACL, der den projizierten Stream nicht bindet/account/chat.                                                                   | Autorisationsfehler geschlossen; Projektmitgliedschaft allein gewährt niemals Zugriff auf ein Objekt eines anderen Streams.                                                                                                                                                                                                                                                                  |
| ZB-FILE-004 | Absturz nach jeder eingehenden Dateifinale: binärer Kommit, Sidecar-Kommit, kanonische Dateiprojektion und Endstatus.                                                                                          | Persistierte Phasen werden idempotent wieder aufgenommen oder sicher zurückgefahren; eine Mismatchung macht den Teilzustand ungültig und entfernt ihn, und kein URN wird sichtbar, bevor der kanonische Datensatz dauerhaft ist.                                                                                                                                                                                        |

## Zuverlässigkeit, Benutzeroberfläche und Bereitstellung

| Identifizierung            | Szenario                                                                                                                                                             | Erwartetes Ergebnis                                                                                                                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| ZB-REL-001    | Halten Sie die Brücke während der Live-Arbeit, Rückfüllung und Ergebnisveröffentlichung fest, und starten Sie sie dann mit der gleichen Festplatte.                                                | Dauerhafte Warteschlangen, Cursoren, Deduplizierung, Mappings und Provideroperationen werden ohne Duplikate oder Verluste wiederhergestellt.                                                                                    |
| ZB-REL-002    | Der Anbieter sollte nicht über die Wiederholung der Versuche und die 24-Stunden-Betriebsfrist hinaus verfügbar sein.                                                                             | Exponentielle Backoff verwendet volle Jitter; abgelaufene Arbeit wird umsetzbar und kann explizit erneut versucht oder verworfen werden.                                                                                |
| ZB-REL-003    | Führen Sie das obligatorische PostgreSQL vollständige Profil mit 300 Festplattenbenutzern, 150 gleichzeitigen unabhängigen Zulip-Konten, 120 Streams, 100.000 Nachrichten, stetigen 100-200 Anbieternachrichten/minute und dem definierten Burst aus. | Live-Nachrichten erreichen p95 bei oder unter fünf Sekunden, die native Latenzregression beträgt nicht mehr als 20%, kein Konto-Status überschreitet die Eigentümer und Live-Arbeit wird nicht durch Wiederholung oder Nachfüllung ausgehungert. |
| ZB-REL-004    | Liefern Sie eine ungültige Provider API Route, den Umfang, das Schema oder die Nutzlast unmittelbar vor einem gültigen Vorgang und starten Sie den Verbraucher erneut.                                         | Die ungültige Operation wird mit begrenzter Diagnostik und rohem Hash dauerhaft unter Quarantäne gestellt, der Cursor bewegt sich atomar vorwärts, kein reflektiertes Ergebnis wird emittiert und die gültige Operation wird genau einmal verarbeitet. |
| ZB-REL-005    | Ein Arbeiter, der vor und nach dem ersten Versuch des Anbieters aufhält, wird abgestürzt, seine Laufzeit läuft, dann starten Sie die konkurrierenden Arbeiter neu.                            | Der Reaper gibt einen nicht versuchten Gegenstand an das Ausstehende und einen versuchten Gegenstand an die unsichere Versöhnung zurück, bewahrt den ersten Versuchsbeweis und verhindert gleichzeitige Ansprüche.                           |
| ZB-REL-006    | Verjähren oder ungültig machen Sie die Ereigniswarteschlange Zulip, während die Ereignisse /edit/delete erstellt werden, und registrieren Sie sie dann erneut.                                                     | Eine begrenzte Neueste-erste-Erholung setzt von der letzten abgebildeten Kontrollstelle vor der Live-Umfrage fort, überlappt sich identisch und unterdrückt Benachrichtigungen, bis die Aufholung abgeschlossen ist.                     |
| ZB-REL-007    | Absturz, nachdem der Provider API eine auf den Aufgabenbereich ausgerichtete eingehende Operation akzeptiert hat, aber bevor der Provider den Endzustand aufzeichnet, und dann die Zuordnung vor dem Neustart verschieben oder deaktivieren. | Die eventuell eingereichte Operation bleibt dauerhaft und vereinbar, ein spätes Backend-Ergebnis wird akzeptiert und das Provider-Ereignis wird nicht automatisch bei einer anderen Zuordnungsgeneration wiedergegeben.  |
| ZB-REL-008    | Zuweisen Sie eine Kausalsequenz und Absturz vor dauerhaften Outbox-Warteschlange, dann die nächste Operation in der gleichen Spur.                                                | Sequenzzuweisung und Warteschlange sind eine PostgreSQL Transaktion; Rollback lässt keine Lücke und die nächste verpflichtete Operation bezieht sich nie auf einen fehlenden Vorgänger.                                  |
| ZB-REL-009    | Verwerfen Sie die Kausalsequenz 1, verpflichten Sie die Sequenz 2, dann genehmigen Sie ausdrücklich einen höheren manuellen Versuch für die Sequenz 1.                                                       | Der genehmigte Versuch ist trotz der späteren Spurposition geltend zu machen, bleibt durch die aktuelle Generation und Löschgewinne eingeschränkt und kann nicht ewig ausstehen.                                 |
| ZB-REL-010    | Absturz nach dem Behalten `submitting` aber bevor der Anbieter API eine unveränderliche Anbieter-zu-Workspace-Operation akzeptiert, dann ohne gegenseitiges Ergebnis neu starten.                     | Die zweideutige Operation wird wiederholt oder idempotent versöhnt, bis ein gegenseitiges Ergebnis beobachtet wird; sie kann nicht feststecken und die aktuellen Überprüfungen der Zuordnung-Generation gelten weiterhin.                  |
| ZB-UI-001     | Verbinden Sie das Konto, wählen Sie Chats/depth, inspizieren Sie Abzeichen/popovers, trennen Sie die Verbindung, verbinden Sie sich erneut und löschen Sie durch sichtbaren Dramatiker.                                      | Die normale Messenger Benutzeroberfläche spiegelt den autoritären Zustand wider, zeigt nie eine Anmeldeinformationen und montiert keine Mail/calendar-Oberflächen.                                                                     |
| ZB-UI-002     | Neue Aufladung, Wiederanschluss und Wiederholung der gespeicherten Ereignisse.                                                                                                                       | Cached Entitäten werden zuerst gerendert, die Ereignisüberholung konvergiert, und die historische Synchronisierung sendet keine Desktop-Benachrichtigungen vor dem Ready-Gate aus.                                           |
| ZB-UI-003     | Verzögerung des Ladenes von IndexedDB-Snapshots, erst ein neueres Echtzeit-Extern-Account-Update veröffentlichen und dann den veralteten Cache lösen, während REST verzögert oder nicht verfügbar ist.         | Die Echtzeitgenerierung bleibt sichtbar und wird nie von dem älteren Cache-Snapshot überschrieben oder erneut aufrechterhalten; später konvergiert REST nur nach vorne und aktive Formularbearbeitungen bleiben intakt.             |
| ZB-DEPLOY-001 | Bühne Backend und Brücke mit Zulip deaktiviert, registrieren, Herzton /capabilities überprüfen, dann aktivieren Sie den Anbieter.                                                       | Native Workspace bleibt überall verfügbar und Zulip aktiviert sich erst, wenn jedes Gate gesund ist.                                                                                              |
| ZB-DEPLOY-002 | Ersetzen Sie die Backend- und Provider-Rot-Images und das Benutzeroberflächen-Artefakt unabhängig voneinander.                                                                                     | Canonical PostgreSQL, S3, Operationsdaten des Anbieters und Anbieteridentität überleben ohne Neuinstallation oder destruktive Reinigung.                                                   |
| ZB-DEPLOY-003 | - Wir haben ein gescheitertes Bridge-Update zurückgestellt.                                                                                                                                    | Die Synchronisation wird ausgesetzt, dauerhafte Arbeiten und Projektionen bleiben intakt und native Workspace Messaging bleibt unberührt.                                                                                     |

## Wiederverwendbares, sichtbares Benutzeroberflächen

Führen Sie diese Reise im globalen Dramatiker MCP mit einem sichtbaren Workspace Fenster
Unter der echten`cassi`- Konto.APIDie Anrufe können zur Diagnose untersucht werden, aber
Sie ersetzen nicht die Bedienoberfläche Aktionen oder Behauptungen.
Die Provider-Seite Aktionen so Echtzeit, Fokus, nicht gelesen, Benachrichtigung und Browser
Das Verhalten der Konsole wird kontinuierlich beobachtet.

1. Öffnen Sie die persönlichen Einstellungen und suchen Sie nach `zulip-external-account-card`.
   Verknüpfen Sie das Formular, füllen Sie `zulip-server-url`, `zulip-email` und die Schreib-allein
   `zulip-api-key`; wählen Sie den expliziten /all-Modus, die Historiendurchmesser und das Projekt aus;
   mit `zulip-connect-submit` vorzulegen.
2. Feststellen, dass die Eingabe der Taste API gelöscht ist und niemals im Text DOM erscheint,
   Die Daten werden in einem anderen Browser gespeichert, Ereignisse, Protokolle oder nachfolgende GET Antworten.
   `zulip-provider-badge`, `zulip-provider-popover` und
   `zulip-account-status` während das Konto auf Live-Ready voranschreitet.
3. Vor der Anlaufbereitschaft ist die Angabe `zulip-notification-gate` vorhanden und importiert
   Nach der Live-Ready-Einrichtung erstellen Sie eine
   Zulip Nachricht und Messung des Erscheinungsbildes in Workspace ohne Nachladen.
4. Für explizite Auswahl schalten Sie `external-chat-toggle-<uuid>` und nur überprüfen
   Schalten Sie auf `all`, erstellen Sie einen anderen Zulip Chat und
   Überprüfen Sie die automatische Entdeckung und Projektzuordnung.
   Die neue Tiefe ist zuerst zu finden und `external-chat-original-<uuid>` zu verwenden, um die genaue
   - Der Anbieter-Chat.
5. Überprüfen Sie die Kanal/topic, persönliche DM und Gruppen-DM-Projektionen im normalen
   Messenger Seitenleiste und Feed. Ihre Anbieter-Badges und Popovers müssen bleiben
   nach dem Neuladen sichtbar, IndexedDB-Hydratation, Ereignis-Aufholung, Umbenennung und Bewegung
   zu einem anderen Workspace Projekt.
6. Senden, bearbeiten, löschen, antworten, zitieren, erwähnen, lesen markieren und ein Bild übertragen
   Workspace erwähnt die Verwendung
   `urn:user:<uuid>` und Nachrichten enthalten nur Datei-URNs; Provider-Originale und
   Kopierte Bytes bleiben dem aktuellen Eigentümer zugelassen/project.
7. Zwang die zweideutigen Send-Fälle ZB-MSG-003 und ZB-MSG-005 durch
   ZB-MSG-007. Inspektionieren Sie `external-operation-<uuid>` und die geplanten
   `provider-delivery-*` Abzeichen. Für die manuelle Abgleich, überprüfen Sie den Safe
   Grund und ursprüngliche Verbindung, dann geöffnet
   `external-operation-retry-confirmation-<uuid>` und bestätigen, dass kein erneuter Versuch durchgeführt wird
   vor der ausdrücklichen Bestätigung des Doppelrisikos gesendet.
8. Übung Trennen, erneute Verbindung mit einer neuen Schreib-nur-Taste und Löschen mit
   `zulip-disconnect`, `zulip-reconnect-submit` und `zulip-delete-confirm`.
   Abschalten hält eine nur-lesbare Projektion; Löschen entfernt sie und verhindert neue
   Wiederholen mit Deaktivierung und Löschung des Eigentümers IAM.
9. Das Fenster in jeder Lebenszyklustadium neu laden und im Hintergrund/foreground aufstellen.
   Bestätigen Sie, dass vor dem Einholen der vorgelagenen Wiedergabe kein gespeichertes Ereignis als
   neue, keine Entität verschwindet aus der Seitenleiste, ungelesenen Zustand konvergiert, und die
   Die Konsole enthält keinen neuen Fehler für die Anbieterfunktion.

Die wiederverwendbare Benutzeroberfläche gehört zu `workspace_ui/e2e` und verweist auf diese
Es muss einzigartige Testdaten aussäen, nur seinen eigenen Anbieter reinigen und
Workspace Objekte und Screenshots/traces außerhalb von Service-Repositories speichern
unter der gemeinsamen Testlauf-Archivrichtlinie.

## Mehrfachverwendbare Checkliste für die Annahme von sichtbaren Benutzeroberflächen

Die folgende Checkliste erweitert die Reise in unabhängig wiederholbare
Ein Szenario ist erst dann abgeschlossen, wenn der benutzersichtliche Zustand
in der entsprechenden Registerkarte Workspace oder Zulip ohne Nachladen überprüft, es sei denn,
der Schritt ausdrücklich ein Neuladen erfordert. HTTP, Anbieter API, Datenbank und
Service-Logs können einen Ausfall erklären, aber nicht die sichtbare Behauptung ersetzen.

### A. Ausgangslage und Verbindung

- [ ] In Workspace als `cassi`, überprüfen Sie native Stream/topic Navigation, eine native
      Nachrichtensendung, nicht gelesenes Clearing und Echtzeit-Zustellung vor der Verbindung Zulip.
- [ ] Öffnen Sie die Zulip Besitzer-Sitzung in einem zweiten sichtbaren Tab und überprüfen Sie Kanal A,
      Kanal B, der persönliche DM, der Gruppen-DM, und beide eingehenden Dateien.
- [ ] In `zulip-external-account-card`, verbinden mit expliziter Auswahl,
      `history_depth=new` und Projekt A. Überprüfen Sie die sichtbare Progression
      `connecting` -> `backfill` -> `live`, das Benachrichtigungs-Gate vor
      `live_ready` und das Fehlen des Schlüssels API nach der Übermittlung.
- [ ] Überprüfen Sie, ob die Benutzeroberfläche des verbundenen Kontos keine zweite Aktion hinzufügt.
      Einfachheit der Vergabe von Dienstleistungen
      das bestehende Konto, und überprüfen Sie dann das unveränderte Konto in der sichtbaren Benutzeroberfläche.
- [ ] Übung `auth_required` mit einem absichtlich gedrehten Einwegschlüssel, dann
      Verwenden Sie die Verbindung mit dem neuen Schlüssel und überprüfen Sie die Rückkehr auf `live`.

### B. Katalog, Geschichte und Projektzuweisung

- [ ] Im expliziten Modus, wählen Sie Kanal A und überprüfen Sie, dass Kanal B, die persönliche
      DM und die Gruppe DM bleiben bis zur individuellen Auswahl nicht projiziert.
- [ ] Wiederholen Sie den Import des Kanals A für `new`, `7_days`, `30_days`, `90_days` und
      `all`. Vor jeder Wiederholung, deaktivieren und warten Sie auf Projektion Entfernung, dann
      Überprüfen Sie die genaue Grenze der Mitgliedschaft und die neueste erste Ankunft ohne
      doppelte Workspace-Nachrichten.
- [ ] Während die `all` Backfill noch aktiv ist, senden Sie eine einzigartige benannte live
      Nachricht von Zulip. Überprüfen Sie, ob sie vor dem verbleibenden älteren Verlauf erscheint,
      die Zielzeit für die Live-Latenz erreicht und keine Nachricht über die Rückfüllung auslöst.
- [ ] Wählen Sie Kanal B, den persönlichen DM und den Gruppen-DM in Projekt A. Überprüfen
      ihre gewöhnlichen Messenger-Stream/topic-Formen, Privatsphäre, Teilnehmerlisten,
      Anbieter-Bedges, Popovers und `Open original` Ziele.
- [ ] Ändern Sie das Konto auf `selection_mode=all`, erstellen Sie Kanal C in der sichtbaren
      Zulip Tab, und überprüfen Sie, ob es ohne andere Einstellungen speichern und zugewiesen wird
      Das ist das aktuelle Standardprojekt.
- [ ] Bewegen Sie Kanal A zu Projekt B mit `external-chat-move-*`.
      Sichtbarer Übergang: Er verschwindet aus Projekt A, erscheint in Projekt B mit
      derselbe Stream/topic/message UUIDs und Lesestatus und erhält folgende
      Verkehr nur im Projekt B.
- [ ] Ändern Sie das Standardprojekt und erstellen Sie einen anderen Zulip Kanal.
      Neuer Entdeckungen werden mit dem neuen Standardvorgang durchgeführt; bestehende Projektionen bewegen sich nicht.

### C. Kanal, Thema, DM und Identitätssemantik

- [ ] Umbenennen Kanal A und ein Thema von Zulip, dann umbenennen Sie sie wieder von
      Workspace. Überprüfen Sie Konvergenz in beiden sichtbaren Registerkarten, stabile Workspace UUIDs,
      und kein doppelter Strom/topic nach dem Nachladen oder Nachholen.
- [ ] Ändern Sie die Karte Kanalbeschreibung und Privatsphäre , wo die verhandelte
      Die anderen Seiten konvergieren, wenn nicht unterstützt,
      Überprüfen Sie, ob die Steuerung aus einem sicheren Grund fehlt oder deaktiviert ist.
- [ ] Fügen Sie einen abgebildeten Benutzer von Workspace hinzu, entfernen Sie diesen Benutzer und fügen Sie den Benutzer erneut hinzu.
      Überprüfung der Abonnentenliste Zulip nach jedem Schritt, dauerhafter Betriebszustand,
      Lesegeschichtsgrenze, zukünftige nicht gelesenen Lieferungen und sofortiger Zugriffsabschluss.
- [ ] Auf einem bereits konvergierten ausgewählten Kanal, fügen Sie einen Abonnenten in
      Zulip und dann wieder hinzufügen Sie den Abonnenten.
      Zulip Abonnenten, Teilnehmer an externen Chat-Streams, Workspace Streamingbindungen,
      und die sichtbare Mitgliederliste nach jeder Änderung.
- [ ] Überprüfen Sie , ob der persönliche DM bleibt genau zwei Teilnehmer mit einem Standard
      Thema, und die Gruppe DM bleibt ein privater Gruppenstrom mit einem Standardthema.
- [ ] Öffnen Sie Peer A und Peer B von projizierten Nachrichten durch zwei verbundene
      Konten in der gleichenZulipÜberprüfen Sie eine Identität proZulip
      `(realm_uuid, user_id)`, keine Kontoduplikate, ein sichtbarer Zulip
      Abzeichen und keine Zusammenführung mit Benutzern mit gleicher E-Mail Workspace IAM.
- [ ] Überprüfen Sie, ob jeder verbundene Kontoinhaber nur auf IAM UUID dieser Kontoinhaber beschließt
      nach einer authentifizierten Zulip Registrierung und ein zweiter IAM-Besitzer kann nicht
      die gleiche `(realm_uuid, user_id)` behaupten.
- [ ] Überprüfen Sie, ob die Metadaten des Anbieters und das interaktive Badge in der Seitenleiste übereinstimmen
      Stream-Zeile, Themenzeile, Nachrichtenblase, Teilnehmer/profile Panel, REST-hydriert
      Ansicht, Echtzeit-Aktualisierung und nach dem Neuladen Cache-Ansicht.

### D. Zweiseitig-Nachrichten-Vorgänge

- [ ] Zulip -> Workspace: Erstellen Sie Markdown/link Nachrichten im Kanal A, beide
      Die Ergebnisse der Studie zeigen, dass die
      p95 Ziel ohne Nachladen, richtiger Autor/topic, eine Projektion, Abzeichen und
      Originalverbindung.
- [ ] Workspace -> Zulip: Senden Sie gleichwertige Nachrichten in allen drei Gesprächen
      Überprüfen Sie sichtbare `pending` -> `delivered`, genau eine Anbietermeldung,
      und fortgesetztes Composer Scroll/read Verhalten in Workspace.
- [ ] Beide Seiten, Bearbeiten und Löschen einer Nachricht; Erstellen einer Antwort und ein Zitat;
      Angabe von Peer A. Überprüfung einer konvergierten Operation, stabiler Anbieter/Workspace
      Identifikatoren, Zielgruppe der richtigen Antwort, lesbares Zitat und `urn:user:<uuid>`
      die Abgabe von Text anstelle von wörtlichem URN Text.
- [ ] Markieren Sie einzelne Nachrichten, ein Thema und einen Stream, der von beiden Seiten gelesen wird.
      Überprüfen Sie die genaue Konvergenz der Lesungen, die ungelesenen /sidebar/folder Abzeichen und eine
      leere Gegenstand/stream erzeugt keinen sichtbaren Ausfall oder Phantombetrieb.
- [ ] Rennen Bearbeiten gegen Löschen und zwei Bearbeitungen in entgegengesetzte Richtungen.
      Letzte bestätigte Operation gewinnt und löscht eine nicht bestätigte Bearbeitung.

### E. Akten und Verlustkonvertierungen

- [ ] Senden Sie die eingehendenPNGund generische Datei vonZulip- Überprüfen Sie .WorkspaceVergleicht
      Das Bild lädt die generische Datei herunter, zeigt nur URN Referenzen in der Nachricht an
      Daten und bewahrt den erwarteten Namen, die erwartete Größe, den Typ MIME und die erwarteten Bytes.
- [ ] Senden Sie die ausgehende PNG und die generische Datei von Workspace.
      in Zulip mit den erwarteten Bytes und dieser Workspace-Nachricht zugänglich
      Inhalt enthält nur S3-unterstützte URNs.
- [ ] Nachdem beide Kanal-B-Dateien kopiert wurden, deaktivieren Sie Kanal B. Überprüfen Sie die
      Projektion und Workspace Kopien im Besitz des Anbieters verschwinden sofort und
      keine veraltete Datei-Affordance mehr; wählen Sie sie nur dann neu aus, wenn ein späteres Szenario sie benötigt.
- [ ] Auslösen einer Operation, deren Vorflug Verluste hat.
      `external-operation-preflight-dialog` listet die sicheren Verluste auf; Stornieren führt
      keine Mutation, während explizite Weiterführung genau eine Operation ausführt.
- [ ] Empfangen eines nicht unterstützten Provider-Elements.
      die Zulip-Abzeichen und `Open original`; die Rohanbietermarkierung darf nicht in
      die dargestellte Nachricht.
- [ ] Deaktivieren Sie eine verhandelte Mutationsfähigkeit.
      Verborgen oder deaktiviert mit seinem sicheren `unavailable_reason`, während unabhängige native
      Die Daten werden in den Datenbanken der
      Die Steuerung kehrt nach Herzschlag/catch-up ohne Nachladen zurück.

### F. Offline, erneuter Versuch und Vereinbarkeit

- [ ] Unterbrechen der Verbindung Zulip, während beide sichtbaren Registerkarten offen bleiben.
      Konto-Gesundheit wird nach dem definierten Fortschritts-Timeout `degraded`, in der Warteschlange
      Ausgehende Arbeiten bleiben sichtbar, native Workspace Messaging funktioniert weiterhin und
      automatische Wiederherstellung nach Rückkehr der Verbindung.
- [ ] Verlieren Sie eine Send-Antwort, aber behalten Sie die Übereinstimmung Zulip Lokal-Echo-Ereignis.
      Überprüfen Sie genau eine Zulip -Nachricht und sichtbare Lieferkonvergenz ohne
      automatisch erneut senden.
- [ ] Verlieren Sie sowohl die Reaktion als auch das lokale Echo, aber legen Sie eine genaue Geschichte aus.
      Überprüfen Sie die Verzögerung der Aussöhnung akzeptiert die Übereinstimmung, wählt eine deterministische
      Anbieter-Nachricht und nicht erneut senden.
- [ ] Gib keine genaue Historie-Überprüfung zurück.
      Wenn das zweite Ergebnis bestätigt wird, wird die Operation mit einem
      sichtbare Anbietermeldung.
- [ ] Verhindern Sie, dass der Verlauf verfügbar ist, oder verlieren Sie das Ergebnis des erneuten Versands.
      Überprüfen Sie `manual_reconciliation_required`, einen sicheren Grund, Text mit doppeltem Risiko,
      und eine ursprüngliche Verbindung, wenn bekannt. `external-operation-retry-confirmation-*`
      Die Verzögerung muss die Wiederanwendung bis zur ausdrücklichen Bestätigung blockieren.
      - Ich schicke später.
- [ ] Wieder starten Sie nur das Bridge-Wurzelbild/service, während die Arbeit in der Warteschlange steht.
      Persistent Cursor, Mapping, Kausalordnung, Wiederholungsversuche und Wiederherstellung von Deduplikationen
      von derselben Datentaste, und keine gespeicherte Nachricht wird als neu gemeldet.

### G. Lebenszyklus, Richtlinien und Cache/realtime

- [ ] Bevor die dedizierte Betreiberrolle vergeben wird, überprüfen Sie die Anbieterrichtlinie.
      Die Daten werden von den Endpunkten für die Verwaltung von Brückeninstanzen und der Gesundheits- und Gesundheitssicherheitsanalyse zurückgegeben.`403`für
      `cassi`Und ...`zulip-admin-panel`Sie ist nicht da.Workspace
      Projekt, überprüfen IAM Introspektion enthält genau die neun kanonischen
      die Berechtigungen des externen Anbieters (keine Wildcard) und bestätigen Sie das Panel und
      Die Daten werden in einem anderen Format als der Datenverarbeitungssystem verwendet.
      Rolle Bindung und überprüfen die Endpunkte zurückkehren `403` und das Panel verschwindet
      wieder ohne Änderung der gewöhnlichen Workspace -Rolle.
- [ ] Trennen Sie das `cassi` Konto. Überprüfen Sie bestehende Projektionen bleiben lesbar,
      Die Daten sind nicht in der Lage, die Mutationskontrolle zu ermitteln, und es ist nicht vorgesehen, dass ein neuer Zulip -Verkehr entsteht.
      Verbinden Sie sich mit einem neuen Schlüssel und überprüfen Sie den Aufholzugang. Dann starten Sie den direkten Verkehr wieder.
- [ ] Mit dem Einweg-Lebenszyklupfänger deaktivieren IAM und überprüfen Synchronisierung Stopps
      Reaktivieren und überprüfen Sie die Wiederherstellung.
      Die IAM Eigentümer und überprüfen das Konto, Projektion, Schlange Arbeit, und kopiert
      Die Provider-Dateien werden gelöscht.
- [ ] Löschen Sie das externe Konto `cassi` durch `zulip-delete-confirm`.
      Die Anmeldung wird sofort gelöscht, kein späterer Verkehr und ein neues Verbindungsformular.
      Dies geschieht erst, wenn alle nicht zerstörenden Szenarien für das Hauptkonto abgeschlossen sind.
- [ ] Wie `cassi` mit Administratorberechtigungen, überprüfen
      `zulip-admin-panel` und `zulip-admin-health`.
      keine Anmeldeinformationen, keine Nachrichteninhalte oder kein Chatkatalog des Eigentümers.
      `zulip-admin-provider-enabled`, jeder `zulip-admin-limit-*`,
      `zulip-admin-custom-ca`, `zulip-admin-custom-ca-remove`, Notfall
      `zulip-admin-provider-suspend`/`zulip-admin-provider-resume` und die
      `external-bridge-instance-<uuid>` unterbrechen/resume/revoke Kontrollen mit
      Einwegrichtlinie/identity
- [ ] Überprüfen Sie den Zustand des Herzschlags bei der konfigurierten 10-Sekunden-Kadenz:
      gesund, nach 30 Sekunden ohne Herzschlag abgebaut und offline gesammelt
      Nach 60 Sekunden, erst nach einem kompatiblen Herzschlag.
- [ ] In jeder Lebenszyklustadium, neu laden, schließen/reopen, und Hintergrund/foreground
      DieWorkspaceTab. Überprüfen Sie, ob zwischengespeicherte Entitäten zuerst wiedergegeben werden, nur die Bewegungen nachholen
      Vorwärts, historische Daten erzeugen keine Benachrichtigung, live-ready Verkehr
      wird notifiziert, der Zustand der Seitenleiste/unread konvergiert und kein Provider-Feature-Fehler wird
      der Browserkonsole hinzugefügt.
- [ ] Beenden Sie mit dem gleichen nativen Stream/topic senden, Echtzeit, nicht gelesen, Datei und
      Navigationsrauchprüfungen für die Basislinie.Messengermuss bleiben
      Funktionsfähigkeit nach Löschung oder Überbrückung des Kontos.

## Hinrichtungsorte

1. Die Überprüfung von Vertrag, Einheit, Migration, Typ, Flusen und Bild-Vertrag erfolgt lokal.
2. Falsch-Transport-Integrationstests bestehen ohne laufende Zulip Bereich.
3. Die Integration von Real Provider API, S3 und Zulip erfolgt in der isolierten Umgebung.
4. Entwicklungsbilder werden mit unveränderlichen Versionen gebaut.
5. Nur sichere Elementaktualisierungen werden angewendet; keine Arbeitsdatenplatte wird neu erstellt.
6. Jede erforderliche sichtbare Benutzeroberflächenreise geht unter `cassi` durch den Dramatiker MCP.
7. Das eingesetzte Workspace Manifest versöhnt alle fünfzehn externen Integration
   IAM Berechtigungen und beide nicht zugewiesenen Rollen. Effektiver Benutzer und Administrator
   Die Daten werden nur durch explizite projektbezogene Rollenbindungen zugänglich gemacht und
   durch Löschen dieser Verknüpfungen entfernt.

Jedes nicht ausgeführte Szenario wird als `NOT RUN` oder `BLOCKED` gemeldet.
Das ist nicht vollständig und darf nicht für das Reich aktiviert werden, solange ein erforderliches Tor aktiviert ist.
fehlt oder versagt.
