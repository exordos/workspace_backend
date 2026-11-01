# Anbieteridentitäten

Instanzen von Messenger-Anbietern werden in `messenger_providers` mit `uuid`
und eindeutigen `hostname` gespeichert. Hostnamen werden durch URL-Parsing normalisiert; Schema, Port
und Pfad sind nicht Teil der Instanzidentität. Bestehende deterministische
Workspace-Benutzer-, Kanal-, Themen- und Nachrichten-UUIDs bleiben erhalten.

Anbieterbewusste Domänenmodelle tragen `source_name`, `provider_uuid` (ein Fremdschlüssel zum Anbieter) und ein nullable `provider_identity` (eine Zeichenkette mit maximal
255 Zeichen). `source_name = "native"` bedeutet strikt lokal: Beide Anbieterfelder sind null und das Objekt erzeugt niemals eine Zulip-Ausgehende-Absicht.
Ein Kind, das in einem Zulip-Stream oder einer Nachricht erstellt wird, erbt `source_name = "zulip"`
und das nicht-null `provider_uuid` des übergeordneten Objekts, auch wenn der Benutzer es über
Workspace erstellt. Sein `provider_identity` bleibt null, bis der Anbieter es bestätigt.
Clients können diese geerbte Herkunft nicht auswählen oder ändern. Das frühere JSON-`source`-Objekt wird aus Modellen, REST-Antworten und Echtzeit-Nutzlasten entfernt.
`WorkspaceUser.source` bleibt unverändert.

| Modell | Zulip-Anbieteridentität |
| --- | --- |
| `WorkspaceStream` | Kanal-ID; direkte/Gruppenunterhaltungen verwenden `dm:` gefolgt von der bestehenden deterministischen Unterhaltungs-UUID. |
| `WorkspaceStreamBinding` | Stream-Identität gefolgt von der entfernten Benutzer-ID, getrennt durch `:`. |
| `WorkspaceStreamTopic` | Stream-Identität gefolgt vom Themen-Namen, getrennt durch `:`. |
| `WorkspaceUserTopicBinding` | Themen-Identität gefolgt von der entfernten Benutzer-ID, getrennt durch `:`. |
| `WorkspaceMessage` | Entfernte Nachrichten-ID. |
| `WorkspaceUserMessageState` | Entfernte Nachrichten-ID gefolgt von der beobachtenden entfernten Benutzer-ID, getrennt durch `:`. |
| `WorkspaceMessageReactionFact` | Entfernte Nachrichten-ID, reagierende entfernte Benutzer-ID, Reaktions-Typ und Emoji-Code, getrennt durch `:`. |
| `WorkspaceFile` | Von Zulip zurückgegebener entfernter Upload-Pfad. |

Das kanonische Objekt besitzt seine Anbieteridentität. Senden und Empfangen lösen
diese Identität direkt auf; ein natives Objekt erwirbt keine separate entfernte Kopie.
Dateiverknüpfungen behalten kontospezifische Synchronisationsbeobachtungen bei, und Reaktionsverknüpfungen
behalten benutzerdefinierte Emoji-Metadaten bei. Die Dateiautorisierung verwendet Workspace-ACLs und aktive
Stream-Mitgliedschaften, unabhängig von verknüpften Anbieterkonten oder Quittungen.
Technische Platzierungen, Nachrichten-Zugriffsbindungen und
Datei-Zugriffszeilen erben den Kontext über ihre bestehenden Fremdschlüssel.

`ExternalAccount.provider_uuid` verweist auf dieselbe Anbieters-Tabelle.
`external_account_id` speichert die entfernte Benutzer-ID ohne Hostnamen-Präfix.
Die `provider`-Zeichenkette des Kontos behält den Anbieters-Typ bei. Nicht konfigurierte Konten
ohne Server-Hostname können eine null-Anbieterverknüpfung beibehalten; die Angabe einer Anbieter-UUID oder die Konfiguration einer Server-URL stellt die Verknüpfung her. Die Eindeutigkeit des Kontos
schließt `provider_uuid` ein, sodass derselbe Besitzer Konten auf verschiedenen
Servern verbinden kann und identische entfernte Benutzer-IDs isoliert bleiben. `settings.server_url`
wird als Transport-Endpunkt beibehalten, einschließlich Schema, Port und Pfad;
die anbietersbezogene Identität und die Hostnamen-Suche verwenden den Fremdschlüssel des Anbieters.

Migration 0263 ersetzt kanonische JSON-Quellspalten durch Anbieterfelder.
Migrationen 0265 und 0266 stellen die alte ausgehende und eingehende Task-Maschinerie ab.
Migration 0267 erbt den Anbieterkontext bestehender Kinder von ihren Eltern,
entfernt veraltete Root-Identitätsverknüpfungen und native ausgehende Absichten und erlaubt
eine ausstehende Zulip-Identität ohne externe ID. Dieser Schnitt ist irreversibel;
stellen Sie bei Bedarf eine verifizierte Datenbank-Sicherung zusammen mit ihrer passenden Anwendungs-Version
wieder her. Das Löschen von Anbietern verwendet kaskadierende Fremdschlüssel für anbieterbesitzene Zeilen.


## Synchronisationsstatus

Alle acht anbieterbewussten Domänenmodelle stellen ein schreibgeschütztes `status` mit den Werten
`new`, `syncing`, `active` und `error` bereit. Native Entitäten beginnen und bleiben
`active`. Eine neu konstruierte Anbieter-Entität beginnt als `new`; ein Import, der
eine bereits vollständige Entität erstellt, persistiert sie direkt als `active`.
Bestehende Datensätze werden durch Migration 0264 als `active` nachgefüllt.

Die Anbieter-Reconciliation überführt eine bestehende Entität in `syncing` und dann nach
erfolgreicher Verarbeitung in `active`. Ein fehlgeschlagener Worker verzeichnet `error` für seine
spezifische persistierte Entität, nachdem er die fehlgeschlagenen Daten-Schreibvorgänge zurückgerollt hat. Ein Wiederholungsversuch
kann sie in `active` zurückversetzen. Bestehende Daten bleiben in jedem Status lesbar.
Konnektivitätsfehler des Kontos markieren nicht alle importierten Objekte als fehlgeschlagen.

Der ausgehende Worker persistiert die Vorbereitung, bevor er HTTP-Anfragen stellt. Eine
bestätigte Antwort oder ein beibehaltenes passendes Ereignis schließt die Absicht ab und füllt
die kanonische Anbieteridentität aus. Eingehende Domänen-Schreibvorgänge, Client-Veröffentlichung und
Posteingang-Bestätigung werden gemeinsam committet. Status-only-Aktualisierungen ändern keine Domänen-Zeitstempel, Zähler oder stellen neue Synchronisationsarbeit in die Warteschlange. Archiv-, Abschluss-, Lese-, Stern- und Pin-Felder bleiben unabhängig.

## Optionale Sende-Quittungen

Nachrichtensendungen übergeben die kanonische Workspace-Nachrichten-UUID als `local_id` zusammen
mit der `queue_id` des Absenders. Zulip gibt sie als `local_message_id` im Nachrichten-Ereignis dieser
Warteschlange zurück. Der ausgehende Worker kann ein dauerhaft gespeichertes passendes Ereignis nach einer verlorenen HTTP-Antwort verwenden. Die autoritative entfernte ID bleibt
`provider_identity`; `local_id` ist kein persistenter Suchschlüssel oder eine Anbieter-Idempotenz-Garantie. Wenn weder eine Antwort noch eine definitive Quittung überlebt,
hält der Worker die Sendung unsicher, anstatt blindlings ein Duplikat zu erstellen.

## Verknüpfte Benutzer in importierten direkten Unterhaltungen

Ein Anbieter-Import kann einen Benutzer erstellen, bevor diese Person ein IAM-Konto verknüpft.
Die Verknüpfung übernimmt das importierte externe Konto, behält aber die IAM-Benutzer-UUID bei.
Für importierte direkte Unterhaltungen ersetzt die Initialisierung die alte Teilnehmerreferenz in `owner_uuid` und `direct_user_uuid` und stellt fehlende Mitgliedschafts- und Themenzugriffe für den verknüpften Benutzer wieder her. Bestehende Mitgliedschaftseinstellungen haben Vorrang, einschließlich inaktiver Mitgliedschaften. Die ersetzte importierte Mitgliedschaft wird inaktiv; ihre historischen Referenzen bleiben verfügbar. Unterhaltungs-UUIDs, Nachrichtenverlauf und native Unterhaltungen behalten ihre Identitäten.

Direkte Sendungen lösen das andere Ende relativ zum aktuellen Absender auf, beschränkt auf das Projekt und den Anbieter.
Sie erkennen auch deterministische importierte Benutzer-Aliase, wenn ein Konto verknüpft wurde, bevor die Teilnehmer-Reparatur verfügbar war.
Ein veralteter Warteschlangen-Empfänger-Snapshot ist nicht autoritativ. Gruppenunterhaltungen
fahren fort, aktive Mitgliedschaften aufzulösen, und Kanäle verwenden ihre Anbieter-Stream-ID.
Ein nicht aufgelöster Teilnehmer bleibt ausstehend; er wird niemals aus einem Namen geraten.

Migration 0283 repariert bestehende deterministische Aliase und aktualisiert nicht vorbereitete
Warteschlangen-Direktsendungen. Vorbereitete Anfragen, unsichere Sendungen und Zustellquittungen
werden beibehalten, um zu vermeiden, dass eine Anfrage geändert wird, die Zulip möglicherweise bereits erreicht hat.
Ein Downgrade stellt den vorherigen Empfänger-Resolver wieder her, behält aber reparierte Identitätsreferenzen bei; es rekonstruiert keine veralteten Aliase.

## IAM-Benutzeridentität

IAM-Benutzer-UUIDs identifizieren lokale Workspace-Benutzer. Ein historischer Benutzer oder ein Anbieterbenutzer
kann denselben Benutzernamen oder dieselbe E-Mail-Adresse wie ein aktueller IAM-Benutzer haben; beide Felder sind
änderbare Profil-Metadaten, keine global eindeutigen Identitätsschlüssel. Der Request-Bootstrap
materialisiert zuerst die authentifizierte UUID und registriert dann diese UUID im Projekt.
Er übernimmt niemals eine andere UUID, private Mitgliedschaft oder Historie basierend auf einem übereinstimmenden Namen oder einer E-Mail. Migrationen 0289 und 0290 entfernen die veralteten Eindeutigkeitsbeschränkungen für Benutzernamen und
IAM-E-Mail-Adressen, während alle bestehenden Benutzer-UUIDs und Referenzen beibehalten werden. Ihre Downgrades können die Eindeutigkeit nur wiederherstellen, wenn keine konfliktbehafteten Werte existieren; sie benennen niemals Benutzer um, führen sie zusammen oder löschen sie.
