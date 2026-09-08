# Sticker-Interoperabilität mit Zulip

## Ziel und Invariante

Workspace-Nachrichten behalten die kanonische Markdown-Darstellung
`![sticker](urn:sticker:<uuid>)`. Zulip erhält die ursprünglichen Mediabytes über
die bestehende Upload-Pipeline und einen normalen Anhangslink mit der Bezeichnung
`workspace-sticker:v1:<uuid>`. Ein verifizierter markierter Anhang wird vor dem
Absenden eines Provider-API-Ereignisses wieder in die kanonische Sticker-Referenz
umgewandelt.

Dadurch werden keine neuen Sticker in den Katalog importiert. Normale Bilder
bleiben normale Bilder, auch wenn ihre Bytes einem vorhandenen Sticker entsprechen.

## Bestätigte Implementierungsgrenzen

- Backend: `workspace/external_bridge_control/files.py`,
  `file_repository.py`, `service.py` und die Runtime-Einbindung in
  `workspace/cmd/external_bridge_api.py`.
- Bridge-Repository: `exordos/workspace_zulip_bridge`.
- Ausgehende Bridge-Konvertierung: `zulip_adapter.py`,
  `_convert_workspace_content`; sie ruft derzeit `export_file` auf, lädt über den
  Zulip-Client hoch und ändert das Ziel mit `link.with_destination`. Das
  ursprüngliche Bildsyntax-Flag bleibt erhalten.
- Eingehende Bridge-Konvertierung: `converter.py` ruft die Dateiauflösung aus
  `service.py` auf; `file_api.py` verwaltet authentifizierte Datei-API-Anfragen.
- Der Provider-Ingress akzeptiert bereits kanonisches Sticker-Markdown. Sticker
  dürfen nicht in die normale projektübergreifende WorkspaceFile-Projektion
  aufgenommen werden: Katalogreferenzen sind global und benötigen keine
  Dateieinträge pro Chat.

## Drahtprotokoll-Verträge

### Ausgehende Bytes

Den bestehenden `PUT /v1/file-transfers/outgoing/{transfer_uuid}` so erweitern,
dass er `urn:sticker:<uuid>` akzeptiert. Die bestehende Zuweisungsautorisierung,
Idempotenz, der Download-Transport und die Antwortstruktur bleiben erhalten. Bei
Stickern ist `file_uuid` die Katalog-UUID, `name` ist `<uuid>.<format>`, und Größe,
Hash und Typ beschreiben das ursprüngliche Katalogmedium. Nur aktive und nicht
blockierte Sticker innerhalb der aktuellen request session auflösen. Die Sticker-
URN ist exakt und enthält niemals einen Query-Suffix. Alle normalen ACL-Prüfungen
für Anhänge bleiben bestehen.

Kein künstliches WorkspaceFile, kein zusätzlicher Upload-Endpunkt, kein öffentlicher
Bucket-Zugriff, keine neue Datenbanktabelle und keine separate Request-Transaktion
sind erforderlich. Eine Katalogsuche ersetzt die File-Sidecar-Suche nur für
Sticker-URNs. Bestehende zeitlich begrenzte Berechtigungen für einzelne Objekte
werden wiederverwendet. Bei der Ausstellung einer neuen Berechtigung wird die
Sichtbarkeit erneut geprüft; bereits ausgestellte Berechtigungen behalten ihre
bisherige Ablaufsemantik.

### Eingehende Katalogverifikation

Unter demselben authentifizierten Bridge-Service einen schreibgeschützten privaten
Endpunkt hinzufügen:

`GET /v1/stickers/{uuid}?external_account_uuid=<uuid>&external_chat_uuid=<uuid>`

Die erforderlichen Account-/Chat-Parameter identifizieren eine aktuell autorisierte
Zuweisung. Für die History-Synchronisierung werden keine ausgehenden Vorgänge und
keine Transfer-Objekte künstlich erzeugt.

Die Antwort `200` enthält genau `uuid`, `sha256`, `size_bytes` und `content_type`.
Sie enthält niemals einen Storage-Key, Zugangsdaten oder eine Download-URL.
Nicht verfügbare oder unbekannte Katalogeinträge liefern `404`, nicht autorisierte
Zuweisungen `403`. Ungültige Eingaben verwenden die bestehenden Validierungsfehler
der privaten API. Vorübergehende Servicefehler bleiben Fehler, die für den
bestehenden Retry-Ablauf geeignet sind.

### Markdown-Markierung

Die vollständige Linkbezeichnung muss `workspace-sticker:v1:<uuid>` sein. Die
bestehende Bridge-Struktur `[...]` beziehungsweise `![...]` beibehalten; kein
neues Zulip-Markdown-Unterstützungsniveau voraussetzen. Nur geparste Anhangslinks
sind für Metadaten geeignet; Klartext, Code-Spans und eingefasster Code sind keine
Metadaten.

Die Markierung bezeichnet den Anhangstyp, nicht den Autor der Nachricht oder den
Vorgang. Sie ist öffentlich und kein Autorisierungsnachweis. Der physische Name
der hochgeladenen Datei muss die Markierung nicht enthalten.

## Ausgehender Ablauf

1. Markdown mit dem bestehenden Converter parsen und Sticker-URNs erkennen.
2. Das ursprüngliche Objekt über die erweiterte File-API autorisieren und
   herunterladen.
3. Die vorhandenen Provider-Upload- und Operation-/Retry-Mechanismen wiederverwenden.
4. Die Anhangsbezeichnung auf den versionierten Marker und das Ziel auf den im
   Zulip-Upload zurückgegebenen Pfad setzen. Andere Nachrichtenelemente unverändert
   lassen.
5. Das ursprüngliche Workspace-Payload unverändert bewahren.

## Eingehender Ablauf

1. Rohes Zulip-Markdown über den bestehenden Live-/History-Konvertierungspfad parsen.
2. Bei einem Upload-Link mit unterstützter Markierung aktuelle scoped Katalog-
   Metadaten lesen und die ursprünglich heruntergeladenen Bytes anhand von
   SHA-256 und Größe vergleichen. Niemals ein Thumbnail hashen; Provider-MIME-
   Bezeichnungen allein beweisen keine Identität.
3. Bei bestätigter Übereinstimmung
   `![sticker](urn:sticker:<uuid>)` ohne gewöhnliche Zuweisung einer eingehenden
   Datei und ohne Erzeugung eines WorkspaceFile-Eintrags zurückgeben.
4. Ohne Markierung, bei einer unbekannten Version, einer fehlerhaften Markierung
   oder einer bestätigten Abweichung den bestehenden normalen Bildimport mit den
   heruntergeladenen Bytes fortsetzen. Bei einer unterstützten gültigen Markierung
   mit nicht verfügbarer UUID den generischen Platzhalter `Sticker unavailable`
   darstellen und kein normales Bild importieren; gelöschte, fehlende und
   blockierte Zustände sind absichtlich nicht unterscheidbar.
5. Einen Autorisierungs- oder vorübergehenden Netzwerkfehler nicht in eine dauerhafte
   Entscheidung für ein normales Bild umwandeln; das bestehende Fehler-/Retry-
   Verhalten bewahren.

Die wiederhergestellte Bezeichnung zu `sticker` kanonisieren, damit wiederholte
Synchronisierung nicht zwischen Markierungen wechselt. Markierte Kopien in neuen
Zulip-Nachrichten können nach der Verifikation zu Stickern werden; sie bleiben neue
Nachrichten und keine Echos der ursprünglichen Nachricht.

## Echos, Bearbeitungen und History

Den Anhang vor den bestehenden Vergleichen des kanonischen Inhalts und vor dem
Absenden des Provider-Ereignisses normalisieren. Für die Deduplizierung von
Nachrichten weiterhin das bestehende Operation-/Provider-Identity-Mapping
verwenden; eine Markierung darf niemals als Message-ID dienen.

Echte Bearbeitungen beibehalten: Textänderungen erhalten übereinstimmende Sticker;
beim Löschen wird das Element entfernt; der Ersatz durch andere Bytes oder das
Entfernen der Markierung macht den Anhang zu einem normalen Anhang. Nicht alle
Aktualisierungen von Workspace-erzeugten Nachrichten ignorieren. History,
Zustellung an ein zweites Konto, frühe Ereignisse und Wiederherstellung nach einem
Neustart dürfen nicht von einem In-Memory-Outgoing-Marker-Cache abhängen. Auch die
Reconciliation mehrdeutiger Sendungen und normale Live-Events auf die Verwendung
derselben Normalisierung prüfen.

## Arbeitspakete und Abhängigkeiten

1. **Backend-Medien und -Metadaten, Agent A:** Scoped Resolver, private Endpunkte,
   Runtime-Einbindung sowie Local-/S3- und Autorisierungs-Regressionstests
   implementieren.
2. **Bridge-Konvertierung, Agent B (parallel nach Vereinbarung des Wire-Vertrags):**
   bestehenden File-Client und ausgehende/eingehende Converter erweitern;
   Kompatibilität normaler Anhänge, Fehler, History und Reconciliation abdecken.
3. **Provider-Regressionen, Agent C (parallel):** nachweisen, dass Sticker-URNs bei
   Provider-Aktualisierungen und projektübergreifender Projektion ohne Dateisuche
   erhalten bleiben.
4. **Integration und Vertragsverantwortung, Koordinator:** private API-Spezifikation
   und diesen Plan pflegen, beide Diffs prüfen, fokussierte Backend- und Bridge-
   Checks sowie PostgreSQL-Tests gegen eine separate Testdatenbank ausführen.
5. **Unabhängige Prüfung (nach den Änderungen):** beide Repositorys prüfen und
   Findings beheben, danach betroffene Checks erneut ausführen. Selbsttests der
   Agenten gelten nicht als unabhängige Prüfung.
6. **Manuelle Abnahme (separater Nachweis):** tatsächliche Darstellung im Zulip-
   Client, Upload-/Download-Roundtrip, zweiten Benutzer, Bearbeitungen und Replay
   in einer konfigurierten Integration prüfen. Unit-Fakes und S3-Presign-Mocks
   beweisen dies nicht.

## Abnahmematrix

| Fall | Erwartetes Ergebnis |
| --- | --- |
| Sticker aus Workspace gesendet | Zulip-Medium; Workspace-Sticker-URN erhalten |
| Eigenes Echo, Duplikat oder frühes Ereignis | Kein Duplikat; derselbe kanonische Sticker |
| Zweites Konto oder History ohne Send-Cache | Verifizierter Sticker wiederhergestellt |
| Dieselben Bytes ohne Markierung | Normales Bild |
| Markierung mit anderen ursprünglichen Bytes | Normales Bild |
| Unbekannte Version oder fehlerhafte Markierung | Normales Bild |
| Unterstützte Markierung mit nicht verfügbarer UUID | Generischer Platzhalter `Sticker unavailable`; kein normaler Bildimport |
| Zuweisung verweigert | Autorisierungsfehler, keine Offenlegung von Katalogmetadaten |
| Vorübergehender Such- oder Downloadfehler | Bestehendes Fehler- und Retry-Verhalten |
| Gemischter Text, Bilder und mehrere Sticker | Reihenfolge und Elementtypen erhalten |
| Markierungssyntax in Code | Wörtlicher Code unverändert |
| Textbearbeitung, Ersetzen oder Löschen eines Anhangs | Tatsächliche Änderung sichtbar |
| Sticker vor einem neuen Grant blockiert | Neuer Grant verweigert |
| Nachricht zwischen Projekten verschoben | Globale Stickerreferenz unverändert |

## Liefergrenzen

Dieser Implementierungsplan bedeutet weder Commit, Push, Production-Deployment noch
das Senden einer Live-Provider-Nachricht. Automatisierte Tests, reale PostgreSQL-
Prüfungen, Prüfungen mit gemocktem Provider/Storage und die manuelle Zulip-Abnahme
müssen getrennt berichtet werden. Die Sichtbarkeit der Markierung in Zulip-Clients
muss gemessen werden; sie darf nicht als verborgen versprochen werden.

Zuerst das Backend und danach die kompatible Bridge bereitstellen. Sticker-Versand
bis zur Bereitstellung der Bridge deaktiviert lassen. Die Delete-Aktion der UI erst
aktivieren, nachdem die Backend-DELETE-API bereitgestellt wurde.

## Implementierungsstand: 2026-09-08

Die Arbeitspakete 1-5 sind im Backend und im benachbarten Bridge-Working-Tree
implementiert und geprüft. Commit und Deployment wurden nicht ausgeführt. Die
manuelle Abnahme an einer echten Zulip-Integration steht noch aus.

- Backend: 93 fokussierte Unit-Tests bestanden; drei Katalog-Sichtbarkeits-
  Regressionen bestanden auf einer separaten PostgreSQL-Testdatenbank.
- Bridge: 24 neue Sticker-Regressionstests bestanden, einschließlich echter
  Converter-Bearbeitungspfade und der Wiederverwendung gespeicherter ausgehender
  Darstellung während der Reconciliation. Bestehende fokussierte Adapter-/Converter-
  und File-Client-Tests bestanden ebenfalls.
- Vollständige Bridge-Suite: 898 bestanden, 392 übersprungen, sechs fehlgeschlagen.
  Dieselben sechs Fehler wurden auf einem sauberen HEAD reproduziert: Linux-
  orientierte Bootstrap-/CI-Shell-Tests schlagen unter macOS fehl. Datenbankabhängige
  Bridge-Tests wurden ohne DSN nicht ausgeführt.
- Die unabhängige Prüfung beider Production-Diffs fand keine blockierenden Probleme.
  Der Koordinator entdeckte und behob beim Review eine Wire-Abweichung: Der private
  Metadata-GET muss ausdrücklich `Content-Length: 0` senden.
- Ruff-Prüfungen der geänderten Bridge-Dateien bestanden. Backend-Ruff meldet 22
  Diagnosen in den geprüften geänderten Dateien; der Vergleich mit HEAD über denselben
  Checker ergab dieselben 22 Diagnose-Signaturen und keine neuen.
- Das private API-YAML lässt sich parsen, lokale Referenzen werden aufgelöst. Beide
  Repository-Diffs bestehen die Whitespace-Prüfungen.

Diese Ergebnisse beweisen weder die echte Zulip-Darstellung noch das Verhalten bei
Netzwerk-Rennen oder einen vollständigen mTLS-/S3-/Provider-Roundtrip. Das bleibt
Teil der manuellen Abnahme.
