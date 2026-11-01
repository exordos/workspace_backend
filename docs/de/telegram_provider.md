# Externe Telegram-Konten

Dieser Anbieter nutzt ein Telegram-Benutzerkonto über MTProto. Konfigurieren Sie die Client-Anwendung auf dem Server und authentifizieren Sie sich anschließend über das Dialogfeld für externe Konten mit einem QR-Code und, falls aktiviert, dem Passwort für die zweistufige Verifizierung von Telegram. Anwendungsanmeldeinformationen allein autorisieren kein Benutzerkonto.

## Konfiguration

Der unabhängige `workspace-telegram`-Prozess liest dieselbe Konfigurationsdatei wie die API. Eine nicht gesetzte Anwendungs-ID deaktiviert den Anbieter. Die Plattform generiert einen separaten Verschlüsselungsschlüssel; bewahren Sie ihn zusammen mit der Datenbank-Sicherung auf. Bestehende verschlüsselte Sitzungen können ohne den passenden Schlüssel nicht wiederhergestellt werden.

```ini
[telegram]
api_id = 0
api_hash =
secret_encryption_key =
history_batch_size = 100
poll_interval = 1
```

Platzieren Sie Anwendungsanmeldeinformationen in der vom Betreiber verwalteten, nur für Root zugänglichen `/etc/workspace/telegram.conf`-Überschreibung. Die API und der Daemon lesen diese nach der generierten Hauptkonfiguration. Bewahren Sie die Überschreibung in einer separaten Plattform-Konfigurationsressource auf, die auf den Backend-Knoten abzielt, wobei der Anwendungs-Hash durch eine Plattform-Secret-Referenz bereitgestellt wird; Element-Upgrades dürfen diese Anmeldeinformationen nicht besitzen oder zurücksetzen. Verwenden Sie den Plattform-Secret-Store für Anmeldeinformationen. Platzieren Sie niemals Kontositzungen, Login-Tokens, Passwörter oder Anwendungs-Hashes in öffentlichen Einstellungen für externe Konten, Nachrichten, Logs oder Client-Caches. API-Authentifizierungsantworten sind auf Besitzer und Projekt beschränkt, werden niemals zwischengespeichert und sind selbst auf DEBUG-Ebene von der Protokollierung des Antwortkörpers ausgeschlossen.

## Konto- und Authentifizierungsvertrag

Erstellen Sie ein externes Konto mit `provider=telegram`. Öffentliche Einstellungen sind leer: Alle zugänglichen Dialoge und deren vollständiger Verlauf werden automatisch importiert. Legacy-Eingaben `history_depth`, `selection_mode` und `peer_keys` werden verworfen. Die Anbieter-Identität ist der feste Telegram-Hostname. Die bestehende Route für externe Konten und Statuswerte bleiben für ältere Clients verfügbar.

Der Besitzer ruft die folgende Aktion unter Verwendung der bestehenden Berechtigung zum Aktualisieren externer Konten auf:

```text
POST /v1/messenger/external_accounts/{uuid}/actions/telegram_auth/invoke
{"operation":"start"}
{"operation":"status"}
{"operation":"password","generation":"attempt-uuid","password":"example-only"}
{"operation":"cancel","generation":"attempt-uuid"}
```

Ein Anmeldeversuch läuft nach fünf Minuten ab. Das QR-Token wird innerhalb dieses Versuchs erneuert. Das Schließen des Formulars bricht nur dessen aktuelle Generation ab; ein älteres Formular kann einen neueren Versuch nicht abbrechen. Passwörter werden verschlüsselt, einmalig verbraucht und laufen mit dem Versuch ab. Der Daemon widerruft eine Autorisierung, die nach einer Abbruchaktion abgeschlossen wird. Das Löschen eines Kontos deaktiviert die Synchronisierung, trennt die Mitgliedschaften des Besitzers und fordert eine Telegram-Abmeldung an, bevor das Root-Konto und sein abhängiger Zustand entfernt werden.

## Synchronisierung und Identität

Jedes Konto hat eine Verbindung und einen PostgreSQL-Advisory-Lock. Der Daemon führt direkte/Gruppen-Differenzen, Kanalabgleich, erneute Medienverarbeitung, Katalog- und ausgehende Zustellung als separate Jobs aus. Private Dialoge, einfache Gruppen und Kanäle führen jeweils dieselbe Verlaufs-Schleife unabhängig voneinander aus, sodass eine große Kanaldatei die erste Seite des privaten oder Gruppenverlaufs nicht verzögern kann. Ihre GetHistory-Wiederholungsfrist ist gemeinsam und wird vor jedem Peer geprüft; der Kontostatus bleibt synchronisierend, bis alle ausgewählten Verläufe abgeschlossen sind. Telegram und Zulip teilen sich COPY-Staging, mengenbasiertes Zusammenführen von Inhalt/Wurzel/Platzierung/Bindung, Abgleich persönlicher Kennzeichnungen und Zählerplanung. Abgerufene Telegram-Seiten werden vor dem Zusammenführen persistiert; Seitenentfernung und Checkpoints werden mit dem kanonischen Batch committet. Beide Importer weichen Echtzeit- und Veröffentlichungs-Rückständen. Der Verlauf sendet nicht ein WebSocket-Ereignis pro importierter Nachricht. Nur-Service-Seiten rücken den Checkpoint weiterhin vor. Der Verlauf liest immer alles, was Telegram diesem Konto zugänglich macht. Der Katalog wird alle 60 Sekunden aktualisiert. Ein unbekannter direkter/Gruppen-Dialog in der gemeinsamen Update-Differenz weckt denselben Katalog-Job sofort, sodass sein initialer Verlauf nicht auf den periodischen Timer wartet; Nachrichten bekannter Dialoge lösen keinen weiteren Scan aus. Kontakte, Dialog-Gegenstellen und Nachrichtenautoren werden zu stabilen Telegram-Benutzern im Workspace-Verzeichnis. Verifizierte IAM-Profile bleiben maßgeblich.

Private Dialoge und Nachrichten in einfachen Gruppen nutzen kontobezogene Identitäten. Kanal- und Supergruppen-Nachrichtenidentitäten werden innerhalb des Projekts und Anbieters geteilt. Telegram-Streams behalten mitgliedschaftsbeschränkten Workspace-Zugriff. Persönliche Ordner enthalten direkte Dialoge; Telegram-Kanäle und einfache Gruppen erscheinen in Kanälen unabhängig von diesem Zugriffs-Flag. Telegram-Streams sind private Workspace-Streams; das Quellkonto eines anderen Benutzers muss denselben Kanal unabhängig importieren, bevor dieser Benutzer die Mitgliedschaft erhält. Normale Chats verwenden das Standard-Thema `telegram`; Forum-Antworten behalten ihr Quellthema. Zulip-Konversationen ohne Quellbetreff verwenden `zulip`. Der Web-Client gruppiert sichtbare unverschlüsselte direkte Streams nur nach der exakten Gegenstellen-Workspace-UUID, dem Projekt und dem Betrachter. Jedes Anbieter-Thema behält seine physische Stream-UUID für Zustellung und Autorisierung; native Themen verwenden den bestehenden kanonischen nativen direkten Stream. Namen, E-Mails und Benutzernamen verschmelzen niemals Identitäten. Der periodische Katalogabgleich verwendet denselben Stream-Writer, um eine bestehende direkte Gegenstelle nach einer verifizierten Kontoverknüpfung zu aktualisieren; er bewahrt die physische Konversation und gewährt dem neu verknüpften Benutzer keine Mitgliedschaft.

Reaktions-Echos verwenden den kanonischen Nachricht/Benutzer/Emoji-Schlüssel und bewahren eine bestehende Workspace-Reaktions-UUID; wiederholte Quellaktualisierungen und anschließende Entfernungen erstellen keine zweite Tatsache. Die dauerhafte Inbox schirmt Quell-Snapshots gegen neuere Updates ab. Eine Lücke im Quell-Journal löst einen maßgeblichen Verlaufs-Rescan aus; abgedeckte Seitenbereiche gleichen gelöschte Nachrichten aus, ohne neuere Ankünfte zu löschen. Ausgehende Sendungen persistieren die zufällige Kennung vor dem Remote-Schreibvorgang und verwenden maßgebliche Quittungen. Frühe Echos eigener Nachrichten warten in der Inbox, bis ihre Quittung die ursprüngliche Workspace-UUID bindet. Eine unsichere Duplikat-Kennung bleibt ein expliziter Fehler, anstatt über den Nachrichtentext korreliert zu werden.

Text, Quell-Zeitstempel, Antworten, einzelne Fotos/Dokumente, Bearbeitungen, Löschungen, normale Emoji-Reaktionen und die Lesebestätigungen des Besitzers verwenden kanonische Messenger-Entitäten. Schreibgeschützte Quell-Chats lehnen Sendungen ab. Secret Chats, Anrufe, Stories, Zahlungen, Teilnehmerverwaltung, benutzerdefinierte Emojis und ausgehende Alben mit mehreren Anhängen liegen außerhalb der aktuellen Implementierung. Telegram kann zusätzliche Nachrichten-, Datei-, Reaktions- oder Ratenlimits auferlegen; Flood-Waits bewahren den dauerhaften Checkpoint und die Wiederholungsfrist.

## Messungen und Abnahme

Der Authentifizierungsstatus umfasst pro Chat den Start des Verlaufs, die erste committete Nachricht, die Fertigstellung, die Anzahl importierter Nachrichten/Dateien/Bytes, die Zeit für den Verlaufsabruf, die Merge-Zeit und die Vorbereitungszeit. Der Durchsatz ist die Anzahl importierter Nachrichten geteilt durch die verstrichene Wall-Time; berichten Sie Dateivolumen und Anbieter-Flood-Waits separat. Diese lokalen Zähler beweisen keine Zustellung bei Telegram.

Die Abnahme muss tatsächliche Quell- und bereitgestellte Artefakte, QR-Autorisierung, vollständigen zugänglichen Chat-Umfang, Kaltimport, Wiedergabe, Wiederherstellung nach Neustart, Echtzeit-Latenz während des Backfills, Byte-Gleichheit von Anhängen an beiden Endpunkten und negative Zugriffsprüfungen aufzeichnen. Verifizieren Sie natives Messaging und frischen Zulip-Traffic in beide Richtungen nach dem letzten Neustart. Halten Sie synthetische/lokale Zeitmessungen von gemessenen Live-Telegram-Ergebnissen getrennt.

## Beispiele für die Nachrichtenkonvertierung

[Öffnen Sie den Offline-Vergleich Telegram ↔ Workspace](../telegram_messages_preview.html). Seine synthetische Telegram-Ansicht veranschaulicht MTProto-Entitäten, statt einen Telegram-Client nachzubilden. Workspace verwendet seinen tatsächlichen Renderer und sein Theme. Dieselben JSON-Beispiele werden in Unit-Tests gegen die Produktionskonvertierung geprüft; der Generator überprüft auch die gerenderte DOM-Semantik.

Führen Sie `node workspace/tests/fixtures/render_zulip_messages_preview.mjs --telegram` mit lokalen Workspace-UI- und Zulip-Quell-Checkouts aus. `WORKSPACE_UI_SOURCE_DIR` und `ZULIP_SOURCE_DIR` überschreiben deren Pfade. Der gemeinsame Generator behält die bestehende Zulip-Seite bei.

Telegram-Entitäten werden zu escaped CommonMark: fett, kursiv, durchgestrichen, Spoiler, Code, sprachbewusste Blöcke, beschriftete Links und Blockzitate. UTF-16-Offsets bewahren Nicht-BMP-Text. Ausgehender Workspace-CommonMark wird in MTProto-Text/Entitäten geparst, anstatt in Telethons inkompatibles Legacy-Markdown. Unterstreichung behält derzeit reinen Text bei, da Workspace keine Unterstreichungssyntax hat; diese Einschränkung ist auf der Vergleichsseite sichtbar.

Workspace-Zitat-/Nachrichtenreferenzen werden zu nativen Telegram-Antwort-Headern, wenn ihr Ziel zum selben Quell-Dialog gehört. Anhang- und Antwortreferenzen werden als CommonMark-Links geparst; wörtliche Beispiele innerhalb von Code werden als Text beibehalten. Nicht unterstützte Workspace-only-Links behalten ihre lesbare Beschriftung statt einer ungültigen Telegram-URL.

## Echtzeit-Isolation und Posting-Berechtigungen

Direkte/Gruppen-Differenzen, Kanalabgleich, dauerhafte erneute Medienverarbeitung, Verlauf, Katalog-Aktualisierung und ausgehende Zustellung laufen als separate Konto-Jobs. Ein langsamer Kanal-RPC oder Datei-Download blockiert nicht die Schleife für direkte Nachrichten. Rohe Kanal-Updates priorisieren diesen Kanal während des Aufholens. Medienidentitäten werden in die dauerhafte Inbox committet, bevor der Differenz-Cursor vorrückt; die Wiedergabe holt sie erneut ab und überlebt Neustarts. Authentifizierungswiderruf stoppt das Konto; normale Verlaufs-/Katalogfehler bewahren ihren Checkpoint und eine beschränkte Wiederholungsfrist. Statusdiagnosen geben Wiederholungsfristen und Fehlertypen ohne Anbieter-Ausnahmetexte preis.

Die öffentliche Stream-Fähigkeit `can_send` wird pro Betrachter aus Mitgliedschaft, Posting-Richtlinie, Quellberechtigungen und dem verbundenen Konto berechnet. API-Schreibvorgänge werden abgelehnt, bevor eine kanonische Nachricht oder eine ausgehende Absicht erstellt wird, wenn das Senden nicht verfügbar ist. Native `announcement_only`-Kanäle erlauben Besitzern, Administratoren und Moderatoren das Posten; nur Besitzer/Administratoren verwalten diese Richtlinie und erhöhte Rollen. Das bestehende `announce`-Flag für Erstellungsbenachrichtigungen hat separate Semantik. Nur-Ankündigungs-Erstellung ist derzeit für unverschlüsselte native Kanäle verfügbar. Telegram-Broadcasts spiegeln die tatsächlichen Posting-Rechte des Abonnenten wider.

Importierte Telegram-Identitäten verwenden Datensätze für externe Konten ohne Autorisierungssitzung, wie es auch bei Zulip-Importen der Fall ist. Eine spätere verifizierte Anmeldung kann eine importierte Identität beanspruchen, aber keine Identität übernehmen, die bereits mit einem anderen Workspace-Benutzer verbunden ist. Erwähnungen werden nur über das Anbieter- und Projekt des ausgewählten Themas aufgelöst, einschließlich stabiler importierter UUID-Referenzen nach der Kontoverknüpfung. Namen und E-Mail-Adressen sind keine Identitätsübereinstimmungen.

## Telegram-Benachrichtigungsmodi

Stream-Modi werden in beide Richtungen synchronisiert. `all_messages` entmutet den Telegram-Chat explizit. `muted` und `mentions_only` verwenden beide Telegrams lange Stummschaltung; Workspace behält ihr unterschiedliches lokales Benachrichtigungsverhalten bei. Ein Echo desselben Stummschaltwerts bewahrt den lokalen Modus. Eine spätere wirksame Quelländerung wird auf `muted` oder `all_messages` abgebildet. Telegram hat keine exakten Entsprechungen für jeden Workspace-Modus. Die Themen `default`, `follow`, `unmute` und `mute` bleiben Workspace-Themen-Einstellungen und überschreiben nicht den gesamten Telegram-Chat.

Initiale Dialogeinstellungen und Telegrams Standardwerte für Benutzer/Gruppen/Broadcasts werden mit Vererbung importiert. Pts-lose Benachrichtigungs-Updates werden separat verbraucht. Ausstehende lokale Absichten und Beobachtungs-Zeitstempel verhindern, dass veraltete Quell-Snapshots eine neuere lokale Einstellung überschreiben. Ausgehende Änderungen setzen nur `mute_until` und bewahren Ton- und Vorschau-Einstellungen. Abnahmeänderungen müssen die gezielten Quell-Einstellungen als Snapshot speichern und wiederherstellen.
