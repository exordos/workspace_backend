# Externe Telegram-Konten

Dieser Anbieter nutzt ein Telegram-Benutzerkonto über MTProto. Konfigurieren Sie die Client-Anwendung auf dem Server und authentifizieren Sie sich anschließend über das Dialogfeld für externe Konten mit einem QR-Code und, falls aktiviert, dem Passwort für die zweistufige Verifizierung von Telegram. Anwendungsanmeldeinformationen allein autorisieren kein Benutzerkonto.

## Konfiguration

Der unabhängige `workspace-telegram`-Prozess liest dieselbe Konfigurationsdatei wie die API. Eine nicht gesetzte Anwendungs-ID deaktiviert den Anbieter. Die Plattform generiert einen separaten Verschlüsselungsschlüssel; bewahren Sie ihn zusammen mit der Datenbank-Sicherung auf. Bestehende verschlüsselte Sitzungen können ohne den passenden Schlüssel nicht wiederhergestellt werden.

```ini
[telegram]
api_id = 0
api_hash =
secret_encryption_key =
poll_interval = 1
```

Platzieren Sie Anwendungsanmeldeinformationen in der vom Betreiber verwalteten `/etc/workspace/telegram.conf`-Überschreibung mit Besitzer `root:workspace` und Modus `0640` (Root darf schreiben, die API-Gruppe darf lesen). Die API und der Daemon lesen diese nach der generierten Hauptkonfiguration. Bewahren Sie die Überschreibung in einer separaten Plattform-Konfigurationsressource auf, die auf den Backend-Knoten abzielt, wobei der Anwendungs-Hash durch eine Plattform-Secret-Referenz bereitgestellt wird; Element-Upgrades dürfen diese Anmeldeinformationen nicht besitzen oder zurücksetzen. Verwenden Sie den Plattform-Secret-Store für Anmeldeinformationen. Platzieren Sie niemals Kontositzungen, Login-Tokens, Passwörter oder Anwendungs-Hashes in öffentlichen Einstellungen für externe Konten, Nachrichten, Logs oder Client-Caches. API-Authentifizierungsantworten sind auf Besitzer und Projekt beschränkt, werden niemals zwischengespeichert und sind selbst auf DEBUG-Ebene von der Protokollierung des Antwortkörpers ausgeschlossen.

## Konto- und Authentifizierungsvertrag

Erstellen Sie ein externes Konto mit `provider=telegram`. Die öffentlichen Einstellungen sind leer: Der Katalog entdeckt automatisch alle zugänglichen Dialoge. Diese Änderung synchronisiert neue Nachrichten; der automatische Import des vollständigen Verlaufs folgt als separate abhängige Änderung. Veraltete Eingaben `history_depth`, `selection_mode` und `peer_keys` werden verworfen. Bestehende Routen und Statuswerte bleiben für ältere Clients verfügbar. Die Statusantwort meldet die Verfügbarkeit des automatischen Imports ausdrücklich in `history_enabled`.

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

Jedes Konto verwendet eine Verbindung und einen PostgreSQL-Advisory-Lock. Differenzabfragen für Privat- und Gruppenchats, Kanalabgleich, erneute Medienverarbeitung, Katalogaktualisierung und Versand laufen als getrennte Aufgaben. Gespeicherte pts/qts-Cursor und eine dauerhafte Inbox ermöglichen die Fortsetzung nach einem Neustart. Der Katalog wird alle 60 Sekunden aktualisiert; ein unbekannter Privat- oder Gruppendialog weckt ihn sofort. Kontakte, Gesprächspartner und Nachrichtenautoren erhalten stabile Telegram-Identitäten im Workspace-Verzeichnis. Verifizierte IAM-Profile bleiben maßgeblich.

Private Dialoge und Nachrichten in einfachen Gruppen nutzen kontobezogene Identitäten. Kanal- und Supergruppen-Nachrichtenidentitäten werden innerhalb des Projekts und Anbieters geteilt. Telegram-Streams behalten mitgliedschaftsbeschränkten Workspace-Zugriff. Persönliche Ordner enthalten direkte Dialoge; Telegram-Kanäle und einfache Gruppen erscheinen in Kanälen unabhängig von diesem Zugriffs-Flag. Telegram-Streams sind private Workspace-Streams; das Quellkonto eines anderen Benutzers muss denselben Kanal unabhängig importieren, bevor dieser Benutzer die Mitgliedschaft erhält. Normale Chats verwenden das Standard-Thema `telegram`; Forum-Antworten behalten ihr Quellthema. Zulip-Konversationen ohne Quellbetreff verwenden `zulip`. Der Web-Client gruppiert sichtbare unverschlüsselte direkte Streams nur nach der exakten Gegenstellen-Workspace-UUID, dem Projekt und dem Betrachter. Jedes Anbieter-Thema behält seine physische Stream-UUID für Zustellung und Autorisierung; native Themen verwenden den bestehenden kanonischen nativen direkten Stream. Namen, E-Mails und Benutzernamen verschmelzen niemals Identitäten. Der periodische Katalogabgleich verwendet denselben Stream-Writer, um eine bestehende direkte Gegenstelle nach einer verifizierten Kontoverknüpfung zu aktualisieren; er bewahrt die physische Konversation und gewährt dem neu verknüpften Benutzer keine Mitgliedschaft.

Reaktionsechos verwenden den kanonischen Schlüssel aus Nachricht, Benutzer und Emoji und behalten die vorhandene Workspace-Reaktions-UUID. Wiederholte Updates und spätere Entfernung erzeugen keinen zweiten Datensatz. Die dauerhafte Inbox schützt Quellsnapshots vor neueren Updates. Ausgehende Nachrichten speichern den Zufallsbezeichner vor dem entfernten Schreibvorgang und verwenden maßgebliche Empfangsbestätigungen. Frühe eigene Echos warten auf die Zuordnung zur ursprünglichen Workspace-UUID. Ein unklarer doppelter Bezeichner bleibt ein expliziter Fehler und wird nicht über den Nachrichtentext zugeordnet.

Text, Quell-Zeitstempel, Antworten, einzelne Fotos/Dokumente, Bearbeitungen, Löschungen, normale Emoji-Reaktionen und die Lesebestätigungen des Besitzers verwenden kanonische Messenger-Entitäten. Schreibgeschützte Quell-Chats lehnen Sendungen ab. Secret Chats, Anrufe, Stories, Zahlungen, Teilnehmerverwaltung, benutzerdefinierte Emojis und ausgehende Alben mit mehreren Anhängen liegen außerhalb der aktuellen Implementierung. Telegram kann zusätzliche Nachrichten-, Datei-, Reaktions- oder Ratenlimits auferlegen; Flood-Waits bewahren den dauerhaften Checkpoint und die Wiederholungsfrist.

## Beispiele für die Nachrichtenkonvertierung

[Öffnen Sie den Offline-Vergleich Telegram ↔ Workspace](../telegram_messages_preview.html). Seine synthetische Telegram-Ansicht veranschaulicht MTProto-Entitäten, statt einen Telegram-Client nachzubilden. Workspace verwendet seinen tatsächlichen Renderer und sein Theme. Dieselben JSON-Beispiele werden in Unit-Tests gegen die Produktionskonvertierung geprüft; der Generator überprüft auch die gerenderte DOM-Semantik.

Führen Sie `node workspace/tests/fixtures/render_zulip_messages_preview.mjs --telegram` mit lokalen Workspace-UI- und Zulip-Quell-Checkouts aus. `WORKSPACE_UI_SOURCE_DIR` und `ZULIP_SOURCE_DIR` überschreiben deren Pfade. Der gemeinsame Generator behält die bestehende Zulip-Seite bei.

Telegram-Entitäten werden zu escaped CommonMark: fett, kursiv, durchgestrichen, Spoiler, Code, sprachbewusste Blöcke, beschriftete Links und Blockzitate. UTF-16-Offsets bewahren Nicht-BMP-Text. Ausgehender Workspace-CommonMark wird in MTProto-Text/Entitäten geparst, anstatt in Telethons inkompatibles Legacy-Markdown. Unterstreichung behält derzeit reinen Text bei, da Workspace keine Unterstreichungssyntax hat; diese Einschränkung ist auf der Vergleichsseite sichtbar.

Workspace-Zitat-/Nachrichtenreferenzen werden zu nativen Telegram-Antwort-Headern, wenn ihr Ziel zum selben Quell-Dialog gehört. Anhang- und Antwortreferenzen werden als CommonMark-Links geparst; wörtliche Beispiele innerhalb von Code werden als Text beibehalten. Nicht unterstützte Workspace-only-Links behalten ihre lesbare Beschriftung statt einer ungültigen Telegram-URL.

## Echtzeit-Isolation und Posting-Berechtigungen

Differenzabfragen für Privat- und Gruppenchats, Kanalabgleich, dauerhafte Medienverarbeitung, Katalog und Versand laufen als getrennte Kontoaufgaben. Ein langsamer Kanal-RPC oder Dateidownload blockiert den Empfang privater Nachrichten nicht. Rohe Kanalupdates erhöhen die Priorität dieses Kanals. Medienbezeichner werden vor dem Cursorfortschritt dauerhaft gespeichert und überstehen Neustarts. Ein Autorisierungswiderruf stoppt das Konto; gewöhnliche Katalog- und Empfangsfehler behalten getrennte Wiederholungsfristen. Die Diagnose zeigt Fehlertypen und Fristen ohne Ausnahmetexte des Anbieters. Vollständiger Verlaufsimport und maßgebliche Verlaufsscans zur Lückenbehebung werden separat ergänzt.

Die öffentliche Stream-Fähigkeit `can_send` wird pro Betrachter aus Mitgliedschaft, Posting-Richtlinie, Quellberechtigungen und dem verbundenen Konto berechnet. API-Schreibvorgänge werden abgelehnt, bevor eine kanonische Nachricht oder eine ausgehende Absicht erstellt wird, wenn das Senden nicht verfügbar ist. Native `announcement_only`-Kanäle erlauben Besitzern, Administratoren und Moderatoren das Posten; nur Besitzer/Administratoren verwalten diese Richtlinie und erhöhte Rollen. Das bestehende `announce`-Flag für Erstellungsbenachrichtigungen hat separate Semantik. Nur-Ankündigungs-Erstellung ist derzeit für unverschlüsselte native Kanäle verfügbar. Telegram-Broadcasts spiegeln die tatsächlichen Posting-Rechte des Abonnenten wider.

Importierte Telegram-Identitäten verwenden Datensätze für externe Konten ohne Autorisierungssitzung, wie es auch bei Zulip-Importen der Fall ist. Eine spätere verifizierte Anmeldung kann eine importierte Identität beanspruchen, aber keine Identität übernehmen, die bereits mit einem anderen Workspace-Benutzer verbunden ist. Erwähnungen werden nur über das Anbieter- und Projekt des ausgewählten Themas aufgelöst, einschließlich stabiler importierter UUID-Referenzen nach der Kontoverknüpfung. Namen und E-Mail-Adressen sind keine Identitätsübereinstimmungen.

## Telegram-Benachrichtigungsmodi

Stream-Modi werden in beide Richtungen synchronisiert. `all_messages` entmutet den Telegram-Chat explizit. `muted` und `mentions_only` verwenden beide Telegrams lange Stummschaltung; Workspace behält ihr unterschiedliches lokales Benachrichtigungsverhalten bei. Ein Echo desselben Stummschaltwerts bewahrt den lokalen Modus. Eine spätere wirksame Quelländerung wird auf `muted` oder `all_messages` abgebildet. Telegram hat keine exakten Entsprechungen für jeden Workspace-Modus. Die Themen `default`, `follow`, `unmute` und `mute` bleiben Workspace-Themen-Einstellungen und überschreiben nicht den gesamten Telegram-Chat.

Initiale Dialogeinstellungen und Telegrams Standardwerte für Benutzer/Gruppen/Broadcasts werden mit Vererbung importiert. Pts-lose Benachrichtigungs-Updates werden separat verbraucht. Ausstehende lokale Absichten und Beobachtungs-Zeitstempel verhindern, dass veraltete Quell-Snapshots eine neuere lokale Einstellung überschreiben. Ausgehende Änderungen setzen nur `mute_until` und bewahren Ton- und Vorschau-Einstellungen. Abnahmeänderungen müssen die gezielten Quell-Einstellungen als Snapshot speichern und wiederherstellen.

Fehlt ein Kanal in einem vollständig geladenen Katalog, ist er als verlassen oder ausgeschlossen markiert oder liefert er `ChannelPrivateError`, wird nur die Mitgliedschaft des Besitzers dieses Quellkontos widerrufen. Andere verbundene Mitglieder behalten ihren Zugriff; neue Nachrichten werden dem entfernten Besitzer nicht zugänglich gemacht.
