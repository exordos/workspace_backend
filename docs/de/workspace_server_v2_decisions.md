# Workspace Server v2: die von der Kommission

Status: **Aktive Ergänzung zur Architektur und geschlossen Provider API v2**.

[← Hauptindex](index.md) · [Provider API v2](../workspace_provider_api_v2.yaml) · [Zilarchitektur Zulip Bridge](zulip_bridge/README.md)

Dieses Dokument enthält die Entscheidungen `1B`, `2A`, `3A`, `4A`, `5A`, die vereinbart wurden
Sie haben die ersten Implementierungen des neuen Workspace Servers vorbereitet. docs-first
Architektur, schließen die entsprechenden Punkte der OPEN-Liste und ändern nicht
ein öffentlicher Browser API oder JSON, der den bestehenden Workspace UI.

## 1B — Provider Data API v2 - auf dem laufenden private transport

Der neue Inbound-Vertrag befindet sich auf einem bereits existierenden separaten mTLS listener:

- `POST /api/workspace-provider/v2/commands` — provider→Workspace commands;
- `POST /api/workspace-provider/v2/operations/actions/lease` — - Wirksam
  Schlange Workspace→provider;
- `POST /api/workspace-provider/v2/operation-results` — der aktuelle Bericht über
  Das Ergebnis Workspace→provider operation.

V2 Wiederverwenden current certificate identity, heartbeat, body limits,
transaction boundary, batch limit `500`, lease und result semantics v1. V1
Der neue Hörer, der neue credential
protocol und neue public/browser route werden nicht eingeführt.

Inbound v2 nimmt die Provider Identity an, anstatt die berechneten Bridge-Werte
Workspace. `external_account_uuid` Wählt nur die bereits festgelegte Verbindung aus;
Der Server überprüft es gegen mTLS identity und desired state.
wählt nicht aus `project_id`, external-chat UUID, stream/topic/user/message UUID,
permissions Workspace in der Transaktion erlaubt account, realm, chat,
project, stream/topic und Identity-Mapping und ruft dann nur die übliche
Eine Domänenmutation.

## 2A — Ein realm-global provider chat gehört einem project {#2a--один-realm-global-provider-chat-принадлежит-одному-project}

Für das Paar `(provider, verified provider realm, provider_chat_key)` kann es sein
Es ist nur ein Workspace Projekt ausgewählt.
realm Sie können den Chat im selben Projekt wiederverwenden, aber den Chat in einem anderen Projekt auswählen.
project bis zur Änderung abgelehnt desired state.

Konflikt gibt HTTP `409` mit dem sicheren Code zurück
`provider_scope_conflict`. Die Überprüfung erfolgt in einer Transaktion unter advisory lock,
und ein Teilindex der ausgewählten Chats beschränkt den Arbeitstest.
Ein einfaches und billiges Modell: Routing bleibt eindeutig, und Fan-Out und öffentliche
Projektionen werden nicht zwischen projects.

Upgrade Überprüft diese Invariante bis reset/copy. Legacy same-realm/same-chat
aliases Innerhalb eines Projekts werden diese automatisch zusammengefasst, aber bereits vorhandene
Die Auswahl des Chats in mehreren Projekten stoppt migration fail-closed:
automatisch wählen würde bedeuten , ohne Zustimmung zu bewegen oder
Verbergen von internen Workspace Nachrichten.

Bis zur ersten Provider Discovery, wenn verified realm UUID noch nicht bekannt ist,
Die vorläufige Region ist die normalisierte `server_url`. URL
lock; Nach der Entdeckung  URL und realm locks in stabiler Reihenfolge.
Konflikt vergleicht und das bekannte realm UUID, und die gleiche provider origin,
Also bleibt der alte Web-Flow der Erstellung/Erste Wahl des Accounts ohne
Die Kundenänderung, und die Parallelwahl umgeht nicht die Regel 2A.

Provider origin wird als tatsächlicher HTTP origin berechnet: Schema und DNS-Name
werden an die untere Register, den Endpunkt DNS und die Standard-Ports gebracht
werden gelöscht, IPv6 bewahrt die Klammern; path ist nicht an scope key. DNS aliases
Die ersten zwei Schritte werden nach der Entdeckung in der verifizierten Realität UUID abgeschlossen. trusted
Die Bindung jedes Accounts nimmt die gleichen Advisory Locks für alle bereits ausgewählten Accounts ein chat
Dieser Account und vor der Eintragung der Identity wird von seinem Katalog-Report abgelehnt, wenn kein anderer
account Der erste, der bestätigt hat, dass er in einem anderen Projekt ist, hat bereits den gleichen realm/chat bestätigt.
realm account bleibt der Besitzer des Routing; alias, in einem anderen ausgewählt project,
Erhält den sicheren Code `provider_scope_conflict`.
Ohne trusted realm:
Die gleichen numerischen Chat-IDs sind in den unabhängigen Zulip Realms zulässig.
account mit alias wird nicht die zweite aktive Datenquelle, aber bridge kann
die Katalog-Veröffentlichung nach der Löschung der Konfliktwahl erneut durchführen.

Wenn Sie den gleichen realm-global chat im gleichen project Workspace unter dem gleichen
advisory lock Wiederverwendet bereits verwirklichte `projection_stream_uuid` und
exact `provider_topic_id -> topic_uuid` mappings. Account-scoped external-chat
UUID bleibt nur die Control-plane Identity Assignment: beide desired assignment
Sie verweisen auf einen Stream/topic graph, also ist der erneute Account Import nicht möglich.
Erstellt eine zweite öffentliche Projektion. Der erste ausgewählte Account bleibt der Besitzer
provider routing; Die folgenden same-project-Konten sind Aliases dieses
Deselect/delete der routing-Besitzer Atom überträgt den Routing an den ersten
für den verbleibenden selected alias unter demselben realm/chat lock; Löschung des üblichen alias
ändert nur die Control plane und löscht nicht die stream/topic graph.
Die unabhängigen Backfill/live-Deliveries dieser Aliases kommen zusammen realm-global
message/reaction UUID. Server Sie nehmen sie nur an, wenn sie übereinstimmen. verified realm,
project, projection stream und provider chat, wobei der erste materializing
account als stabiler Besitzer einer bereits existierenden Projektion.

## 3A — realm-global provider identity und direct conversation key

Numeric Zulip objects Sie benutzen:

```text
UUIDv5(namespace=verified_realm_uuid, name="<type>:<shortest-decimal-id>")
```

Erlaubte `type`: `user`, `channel`, `message`, `attachment`. Numeric ID —
unsigned shortest base-10 ASCII ohne Zeichen, Leerzeichen und leading zero. Project,
account, server URL, email und mutable display name sind nicht in die Identity eingebunden.

Channel key hat eine Form `channel:<shortest-decimal-channel-id>`. Direct/self/
group conversation key hat eine genaue Serialisierung:

```text
direct-conversation:v1:<count>:<id1>,<id2>,...
```

Die Liste enthält die eindeutigen Provider User IDs aller Teilnehmer und muss
verified owner der verbundenen Account. IDs werden nach Zahlenwert sortiert;
`count` Also hat ein selbstaufruf, DM oder Gruppe DM
ein Schlüssel für History/realtime und für alle Accounts realm.

## 4A — Nur die bestehenden autorisierten public actions für outbound

Generic private command «jede Workspace Modell  zu schreiben ist verboten. Provider
API v2 ist kein Weg , um die Nutzerabsicht zu beweisen und gibt keine Bridge browser/IAM
Befugnisse.

Workspace→Zulip operation wird nur nach dem aktuellen öffentlichen action,
Die Benutzer, Projektumfang und Berechtigungen wurden überprüft.
Bridge Er bekommt es durch den Leasing.
Initiation paths, die nicht im aktuellen Server unterstützt werden
mit dem aktuellen public API (einschließlich generic message move, mark-unread, typing und
Erstmal wird die Funktion "Role/custom-profile Mutations" ausgeschaltet.. Unknown
kind und die Workspace Identität werden bis zu mutation.

Für lifecycle mapped channel/topic ist folgende genaue Semantik festgehalten:

- `stream.delete` ruft den offiziellen Zulip archive-channel Endpunkt an.
  Wiederholt Bridge den aktuellen Status des Channels und zählt bereits
  Archivisierter Channel mit erreichtem Zustand;
- `topic.delete` Sie ruft den offiziellen batch-delete-topic Endpunkt auf.
  `complete=false` ist retryable, und die Abwesenheit von Thema bei vorläufigen
  Die Lektüre gilt als idympotent erreicht;
- `topic.create` erzeugt keine synthetische Zulip-Message: Zulip hat keine eigenständige
  topic-Das ist der Grund, warum Bridge atomar speichert. deterministic
  `<channel-id>:<topic-name>` mapping, Und die erste ist die normale. `message.create`
  Das heißt, wenn man den Namen vor dem ersten Posten ändert, ändert das nur diesen Posten.
  mapping und er erschafft nicht provider traffic.

Diese drei Funktionen werden nur für Channel-Chats veröffentlicht.
provider reads Die Ablehnung von der Verarbeitung von Daten wird nur auf die seltenen destruktive actions durchgeführt, also nicht
Sie fügen dem Importeur eine konstante Belastung realtime/history hinzu.

## 5A — state-based provider event key und Einzel delivery identity

`provider_event_key` beschreibt den gewünschten logischen Provider-Stand.
Es ist für History und Realtime und ist abhängig von account, project, queue event ID,
Lokalsequenz oder Bridge database.

Bevor der Schlüssel berechnet wird , wird der Bridge-Schlüssel JSON object:

```json
{
  "provider_chat_key": "<exact chat key>",
  "provider_object": {"kind": "<kind>", "id": "<provider object id>"},
  "provider_references": {},
  "payload": {}
}
```

JSON wird mit den Schlüsseln in lexikografischer Reihenfolge kodiert UTF-8, separators
`,`/`:`, ohne zusätzliche Whitespace und mit `ensure_ascii=false`. payload
bis zur Normalisierung werden server-owned Workspace IDs gelöscht und transport-only
metadata: `account_uuid`, `chat_key`, `delivery_class`, `external_id`,
`provider_event_uuid`. Digest — lowercase hexadecimal SHA-256 Diese exact bytes.

Wire key:

```text
provider-event:v1:<command-kind>:<object-kind>:<object-id-utf8-byte-length>:<object-id>:<sha256>
```

`provider_sequence` nur die aktuelle Provider-Revision überträgt;
producer sequence Wenn der Provider Revision nicht vorhanden ist, wird der Wert
Das ist gleich. `null`.

Eine separate canonical UUID String `delivery_uuid` ist stabil bei transport retry
eine durable delivery, aber keine semantische Identität ist. Workspace ergibt
Internal Ledger UUID wie:

```text
UUIDv5(verified_realm_uuid,
       "provider-delivery:v2:<provider_event_key>:<delivery_uuid>")
```

Also, der identische Retry einer Delivery wird dedupliziert, und die neue Delivery von der
Der semantic state wird wieder in die Domain-Transaktion eingegeben und mit der aktuellen Transaktion verglichen
Ein bereits erreichter Zustand gibt einen No-op ohne zusätzliche public event;
Die Folge `add → remove → add` verwendet die zweite `add`, obwohl beide add
haben die gleiche `provider_event_key`.

## Datenmigration  native preserve und automatisch Zulip reimport

Nach den Entscheidungen erlassene Klarstellung `1B`–`5A`:

- versioned migration Er kann alles aushalten. authoritative native streams, topics,
  messages, user state, reactions, folders und files in canonical v2 ohne Änderung
  der öffentlichen Browser-Kontrakt;
- Verwahrte recipient-only UUID aus historischen Broadcast Snapshots nicht
  werden zu fiktiven Project-Benutzern: migration speichert sich selbst native
  Ereignis, aber erstellt keine canonical membership/guard für das bereits entfernte IAM
  Benutzer;
- `0157` verwendet eine Container-Grenze: Die Migration löscht jede Nachricht,
  die in einem kanonischen Stream mit dem exakten Paar `source_name=zulip` und
  `source.kind=zulip` liegt, unabhängig vom Ursprung der Nachricht. Auch
  Workspace→Zulip-Nachrichten in einem solchen Stream werden entfernt und durch
  den normalen Zulip-Backfill wiederhergestellt. `0158` vervollständigt den
  Reset und löscht Nachrichten mit derselben exakten Zulip-Herkunft auch dann,
  wenn sie in einen nativen Direct-Container projiziert wurden. Nachrichten
  nativen Ursprungs im selben Container bleiben erhalten;
- dieselbe Transaktion entfernt zugehörige Reaction-/Read-/Event-Projektionen
  und nicht mehr referenzierte Zulip-Dateien. Legacy-Compact-Statistiken sowie
  kanonische v2-Stream-/Topic-/Folder-Zähler werden vor dem Commit aus den
  erhaltenen Nachrichten neu berechnet. Gemischte native Container behalten
  Rollen, Membership-Generationen, Benachrichtigungsmodi, Topic-Zustand und
  Folder-Platzierung; Unread-, Aktiv/Passiv- und Last-Message-Werte werden exakt
  rekonstruiert. Zuerst werden die Compact-Message-/Read-Statistiken jedes
  Topics im betroffenen Stream aktualisiert. Danach folgt das kanonische
  `read_at` je Benutzer der maßgeblichen Compact-Bitmap (außerhalb der Modi
  compact/rollback dem Legacy-Read-Flag), bevor die Zähler veröffentlicht
  werden;
- alte `link_kind=provider_identity`, die von der Account-scoped-Implementierung erstellt wurden,
  Sie werden auf genau `UUIDv5(verified_realm_uuid, "user:<id>")` übertragen.
  surviving native relational references, event payloads, chat catalog und
  current/pending desired resources in derselben Transaktion überschrieben werden;
  `verified_account_owner` bleibt an IAM UUID gebunden und nimmt nicht an dieser teil
  Der Konflikt zwischen der Provider Identity und dem IAM Owner stoppt
  migration fail-closed statt einer unbemerkt verbundenen Nutzergemeinschaft;
- selected external accounts/chats, credentials und project assignment
  Für das alte Account-scoped Format same-realm/same-chat
  stream/topic aliases Atomisch sind sie alle zusammen graph: membership, folders,
  drafts, files, native messages, user topic state Und die Ereignisse werden in
  Kanonische Container, danach werden nur die überflüssigen Container gelöscht.
  Account Erhält einheitlich
  `projection_reset_generation`, account/chat desired generations Steigen,
  Der Zustand wird in `backfill`/`syncing`;
- Bridge Die letzte Reset Generation, die verwendet wurde, wird gespeichert.
  Atomisch löscht nur rebuildable Zulip cache/idempotency/mappings, wobei
  identity Und der Katalog, annulliert die abgeschlossenen Backfill Jobs und startet den vollen
  Die gleiche Generation wird nicht erneut ausgegeben.;
- Die physische Inhalte der gelöschten Zulip files verarbeitet bounded durable
  worker queue Nach der DB-Kommit-Verarbeitung
  Überprüft , ob es keine retained DB Reference gibt; retry gehtpotent. Worker
  Registriert beide File-Storage-Config-Domains, also die local und die S3 cleanup
  Sie benutzen das gleiche Backend wie Messenger API;
- wenn Sie native stream membership löschen , werden die alten broadcast audience rows dieses
  membership generation Sie werden physisch zurückgerufen.
  Es gibt keine Wiederholung der Ereignisse der vorherigen Generation, auch wenn rolling view rebuild;
- logical desired-state snapshot wird nicht vollständig in Python zusammengestellt und nicht gespeichert
  Einer .JSONBEr ist wie ein geordneter Array.PostgreSQLZeilen mit
  cascade lifetime von snapshot token; page read wählt `limit + 1` rows.
  Dies behält den abgestimmten Anker und beschränkt RSS control API unabhängig
  von der Gesamtzahl der Chats und der Größe participant/topic catalogs;
- Vor dem Anschalten und Lesen von Snapshot wird der Server von rows PostgreSQL
  `SHARE ROW EXCLUSIVE` lock Snapshot wartet auf die bereits gestarteten
  append transactions und für kurze Zeit nicht erlaubt , neue sequence,
  also fällt der concurrent upsert/delete unbedingt entweder in frozen rows,
  oder nach dem Anchor. Snapshot wird nur beim Bootstrap erstellt/reset, und
  nicht in der Realtime-Schleife; die globale Pause ist einfacher und
  billiger als die ständige Zusatz-Commit-Order-Infrastruktur;
- der destruktive Reset arbeitet bei Container- und Message-Metadaten
  fail-closed: Ein unvollständiges oder widersprüchliches Paar aus
  `source_name` und `source.kind` bricht vor dem Löschen ab. Die vollständige
  Grenze ist die Vereinigung bestätigter Zulip-Container und Nachrichten mit
  bestätigter Zulip-Herkunft, einschließlich Legacy-only-Kompatibilitätszeilen
  und kanonischer Zeilen, die über Message oder Placement verknüpft sind;
- ein unbeaufsichtigter eingefrorener Cutover ist auf eine Million
  Legacy-Nachrichten, 30 Sekunden Lock-Wartezeit und 45 Minuten Statement-Limit
  begrenzt. Ein größerer Cutover erfordert nach Backup und produktionsgroßem
  Probelauf eine ausdrückliche Operatorfreigabe; 50 Millionen Nachrichten sind
  das Ziel im stabilen Betrieb nach dem Neuimport, keine Erlaubnis zur
  automatischen Legacy-Konvertierung;
- der Control-Plane-Snapshot-Lasttest verwendet mindestens 15.000 Zuweisungen
  mit großen Teilnehmer-/Topic-Katalogen, misst einen begrenzten Backend-RSS und
  liest nur begrenzte Seiten;
- Das obligatorische Scale Gate verwendet mindestens `100 000` alte provider message
  mappings Und beweist, dass der Reset abgeschlossen ist, ist der Backfill-Job wieder abgeschlossen.
  wird `pending`, und die alte Deduplikation unterdrückt nicht die frischen Importe.

Rollback schema nicht wiederherstellt , Zulip projection:
Die Daten werden von den Native-Daten gespeichert.
Sie sind sowohl bei Upgrade als auch bei schema downgrade.

## Unveränderlicher Cutover und Forward-Reparatur der Identität

Die in Workspace Server `1.0.0` veröffentlichte Migration `0152` ist
unveränderlich. Ein neuer Vorbereitungszweig (`0155`) beginnt bei `0151`; der
Join-Head (`0156`) führt ihn vor der normalen Kette `0152` → `0154` auf. Bei
einem frischen Upgrade wird die Herkunft daher vor dem veröffentlichten
Cutover vorbereitet. Installationen, die `0152` bereits verbucht haben,
überspringen diese Vorbereitung und werden durch `0156` vorwärts repariert. Da
`pg_dump` keine Planner-Statistiken erhält, führt der frische Pfad außerdem
`ANALYZE` für alle eingefrorenen Cutover-Eingaben aus, bevor die unveränderlichen
mengenbasierten Anweisungen laufen.

Die Vorbereitung akzeptiert ein historisches ausgehendes Echo nur mit einer
exakt passenden, erfolgreichen `message.create`-Operation. Die Source-Message-
ID darf fehlen, aber nicht der Provider-ID widersprechen. Konsistente native
Zeilen aus der Zeit vor der Operationswarteschlange erhalten kurzlebige
`discarded`-Herkunftsmarker. Sie können nie in eine Provider-Warteschlange
gelangen und werden vom Join-Head entfernt.

Die erste veröffentlichte Bridge-Nutzlast nach `0152` ließ
`source.message_id` aus, enthielt aber weiterhin die vollständige konsistente
Legacy-Form: `source.kind=zulip`, eine numerische `provider_external_id`,
dieselbe ID in den Provider-Metadaten, die ursprüngliche Provider-URL und ein
widerspruchsfreies Realm. `0156` akzeptiert beim Forward-Repair ausschließlich
diese vollständige Form. Eine eindeutige Zeile erhält die realm-globale
Identität; eine nachgewiesene Account-Alias-Kopie wird gelöst, wenn bereits ein
global identifizierter Import existiert. Unvollständige oder widersprüchliche
Varianten brechen weiterhin atomar ab. Die Rolling-Legacy-Trigger verwenden
dieselbe Kompatibilitätsregel, bis diese veröffentlichte Bridge außer Betrieb
ist.

`0156` weist erhaltenen Nachrichten eine realm-globale Provider-Identität zu
und lässt für eine physische Zulip-Nachricht genau einen Provider-verknüpften
Gewinner. Nachgewiesene Account-Aliase müssen bei Realm/Message-ID, Projekt,
Autor, getrennten Accounts, Provider-URL und Metadatenidentität übereinstimmen.
Alle internen Nachrichten, Placements und öffentlichen UUIDs bleiben erhalten;
nur die Provider-Verknüpfung der übrigen Aliase wird gelöst. Eine bereits
global identifizierte Importzeile gewinnt gegen einen passenden erhaltenen
Alias. Jeder nicht belegte Konflikt bricht atomar ab. Rolling Trigger für
Legacy-Insert/Update erzwingen dieselbe Regel, bis alte Server entfernt sind.

## Eigentum gemeinsamer Zulip-Projektionen und erneuter Import

Ein realm-globaler Zulip-Kanal besitzt genau einen kanonischen Stream pro
Workspace-Projekt. Mehrere ausgewählte Accounts dürfen deshalb auf dieselbe
`projection_stream_uuid` zeigen, während der physische Stream den Eigentümer
behält, der ihn zuerst materialisiert hat. Der Provider-Import akzeptiert einen
anderen Account-Eigentümer nur, wenn im selben Projekt eine weitere ausgewählte
Zuweisung auf diesen Stream zeigt. Ohne diese gespeicherte Peer-Zuweisung bleibt
die abweichende Eigentümerschaft ein harter Fehler.

Bei `topic.upsert` leitet der Server die typisierte Workspace-Quelle aus dem
persistierten kanonischen Stream ab. Dessen stabiler Account-Scope bleibt
erhalten und der Topic-Name wird ergänzt. Die Bridge muss serververwaltete
Source-Felder nicht in jedem Event wiederholen.

Migration `0154` erhöht die Reset-Generation jedes Zulip-Accounts einmal und
veröffentlicht die ausgewählten Zuweisungen erneut. Damit werden unter
Quarantäne gestellte Teillieferungen verworfen und ein vollständiger Neuversuch
gestartet. Provider-Schlüssel bleiben idempotent; bereits angenommene Zeilen
werden aktualisiert statt dupliziert. Bei einer frischen Aktualisierung sieht
die gestoppte Bridge nur die finale Generation und führt einen Import aus.

## Zusammenführen von Ordner-Snapshots bei der Legacy-Lesestatus-Reparatur

Die Reparatur des Legacy-Lesestatus kann für jedes korrigierte Nachrichten-Flag
eine eigene Ordnerprojektion einreihen. Jede dieser Aufgaben erstellt den
vollständigen aktuellen Ordner-Snapshot neu und enthält keinen historischen
Ordnerzustand. Sobald ein Worker den Scope `user-folder` besitzt, nimmt der
beanspruchte Legacy-Neuaufbau deshalb wartende Schwesteraufgaben desselben Scopes
auf und schreibt genau einen maßgeblichen Snapshot samt Event. Eine erst nach
dieser Transaktion eintreffende Aufgabe bleibt ausstehend und löst einen späteren
Neuaufbau aus. So bleibt die Live-Konvergenz erhalten, während der
Migrationsaufwand mit der Zahl betroffener Ordner statt mit der Zahl der
Nachrichten-Flags wächst.

## Zusammenfassen reiner Ungelesen-Snapshots

Der Massenimport von Nachrichten, Nachrichtenkorrekturen und die
Materialisierung von Mitgliedschaften können viele Read-Counter-Projektionen
für denselben `user-stream`- oder `user-topic`-Scope erzeugen. Jede reine
Snapshot-Aufgabe berechnet die vollständigen maßgeblichen aktuellen Zähler neu
und enthält keinen historischen Zählerstand. Eine beanspruchte reine
Snapshot-Aufgabe schließt deshalb in derselben Transaktion wartende reine
Snapshot-Geschwister desselben Scopes ab und veröffentlicht einen aktuellen
Snapshot. Aufgaben mit `emit_message_read=true` bleiben getrennt, damit jede
explizite Leseaktion ihr eigenes Event behält. Nach der Transaktion eintreffende
Aufgaben bleiben ausstehend; damit bleibt die Live-Konvergenz erhalten und die
Massenarbeit wird durch die betroffenen Benutzer-Scopes begrenzt.

## Reparierbarer nativer Lesestatus und interaktive Priorität

Migration `0160` stellt Lesezustände nativer Nachrichten wieder her, die beim
Anlegen des kanonischen v2-Zustands im Legacy-Flag oder kompakten Bitmap
vorhanden waren. Die Reparatur ist monoton: Sie füllt nur fehlende `read_at`-
Werte, öffnet nach der Umstellung gelesene Nachrichten nie erneut und erstellt
anschließend native Stream-, Topic- und Ordner-Snapshots neu.

Explizite Leseaktionen für Nachricht, Bereich, Topic und Stream stellen auch
dann eine maßgebliche Zählerneuberechnung ein, wenn die kanonischen Zeilen
bereits gelesen sind. So repariert ein idempotenter Wiederholungsversuch einen
veralteten Snapshot. Diese Projektionsaufgaben laufen vor Massenimporten; die
normale Snapshot-Zusammenfassung begrenzt weiterhin die Datenbanklast.

Dieselben Leseaktionen aktualisieren im kanonischen Vorgang auch den rollenden
Compact-Kompatibilitaetszustand. Migration `0166` repariert fuer
alle Benutzer jede bestehende Kombination aus kanonisch gelesen und im
Compact-Bitmap ungelesen, aktualisiert die betroffenen Topic-Lesestatistiken und
erhoeht die benutzerspezifische Leserevision. Die Reparatur ist monoton und
veraendert keine kanonisch ungelesene Zeile, auch nicht aus einem
Provider-Snapshot.

Migration `0167` gleicht danach fuer alle aktiven Benutzer jedes Compact- oder
Rollback-Topic-Leseaggregat mit dem gespeicherten Bitmap ab. Dadurch wird auch
ein Aggregat repariert, das veraltet blieb, obwohl alle Nachrichtenbits bereits
mit dem kanonischen `read_at` uebereinstimmten, sodass Stream-, Topic- und
Ordnerzaehler denselben Lesezustand verwenden.

Migration `0168` gleicht die gemeinsamen Compact-Nachrichtenzahlen der Topics
und die letzten Ingest-Koordinaten aus den gespeicherten Nachrichtenzeilen ab.
Damit ist auch der Fall abgedeckt, in dem alle Benutzer-Lesezaehler korrekt
sind, aber eine veraltete Topic-Nachrichtenzahl die Compatibility-Zaehler fuer
Stream, Topic und Ordner weiterhin verschiebt.

Migration `0169` wendet die Normalisierung privater Provider-Chats erneut an
und verwendet den Provider-`display_name` jedes Teilnehmers als massgebliche
Quelle fuer benutzerspezifische Namen von Einzel- und Gruppen-Chats. Eine
Workspace-Identitaet dient nur als Fallback, wenn der Provider keinen Namen
liefert. Ein alter Name, der der frueheren Workspace-Identity-Projektion
entspricht, wird als Provider-verwaltet erkannt; eine explizite lokale
Umbenennung eines Gruppen-Chats bleibt erhalten.

Migration `0170` behandelt auch den Eigentümer des ausgewählten Provider-Chats
als gültigen Betrachter, wenn ein Einzelchat-Katalogeintrag nur den
Gesprächspartner enthält. Dadurch bleibt dessen Provider-Name für jedes
verknüpfte Konto maßgeblich, ohne dass der Provider den Eigentümer in der
Teilnehmerliste wiederholen muss.

## Reihenfolge der Provider-Leseseiten

Eine verzögert materialisierte Provider-Leseseite verwendet für die Reihenfolge
innerhalb derselben Lane die Queue-Position ihres Quell-Snapshots. Ein neuerer
Snapshot kann gespeichert werden, bevor die Seite eine physische
Operationssequenz erhält, darf die ältere Seite aber nicht blockieren. Frühere
Snapshots sperren weiterhin spätere Schreibvorgänge derselben Stream-Lane;
andere Lanes bleiben unabhängig. Die Grenzen für Materialisierung und
Lease-Batches ändern sich nicht.

## Wiederherstellung des Provider-Ingress

Private Provider-API-Befehle können sicher wiederholt werden: Ihre Request-,
Event-, Lease- und Result-IDs bleiben zwischen Versuchen stabil. Nach einem
PostgreSQL-Deadlock wiederholt der Server die gesamte Request-Transaktion mit
einem kurzen begrenzten exponentiellen Backoff; andere Control- und File-Requests
werden nicht wiederholt. Sind alle Versuche erschöpft, wird ein wiederholbarer
`503` ohne Datenbankdetails zurückgegeben. Beim Wiederherstellen einer
Topic-Zusammenfassung nach einer Provider-Löschung werden Journal-Grenzen ohne
vorhandene Nachricht übersprungen. So verwirft eine veraltete abgeleitete
Zusammenfassung nicht den gesamten eingehenden Batch.
Vor einer Provider-Löschung eingestellte Projektionsarbeit behandelt die
gelöschte kanonische Nachricht als nicht vorhanden. Veraltete Fanout- oder
Mention-Aufgaben enden dadurch als No-op statt in der Dead-Letter-Queue.

## Read-State-Parität für Provider-Kontoinhaber

Eine Provider-Nachricht, die von mehreren Konten desselben Realms gemeinsam
verwendet wird, hat eine kanonische Platzierung, aber für jeden Inhaber eines
ausgewählten Kontos einen eigenen Binding- und State-Datensatz. Der kompakte
Import materialisiert diese Datensätze in einem begrenzten SQL-Batch und
aktualisiert Compact-Bitmap und kanonisches `read_at` in derselben Transaktion.
Snapshot-Backfills unterdrücken weiterhin öffentliche Einzelereignisse;
autoritative Stream- und Topic-Zähler werden trotzdem neu berechnet.

Migration `0162` stellt anhand des dauerhaften Journals angewendeter
Provider-Ereignisse nur tatsächlich ausgelieferte Message/Account-Paare wieder
her. Der effektive Lesestatus stammt aus dem Compact-Bitmap beziehungsweise
außerhalb des Compact-Modus aus dem Legacy-Flag. Anschließend werden betroffene
Stream-, Topic- und Folder-Zähler neu aufgebaut und vor dem Commit geprüft.

## Vereinbarkeit und Grenzen der ersten Umsetzung

- Öffentliche Routen, Antworten und WebSocket events Workspace UI nicht geändert.
- V2 ist ein geschlossener provider data-plane, nicht browser API.
- Server-owned scope und canonical IDs werden nicht als neue Felder geöffnet public
  Messenger resources.
- V1 transport nur als rolling adapter gespeichert; eine neue Quelle der Wahrheit
  Für provider identity ist v2 contract.
- Der vollständige Wire-Kontrakt ist hier zu finden:
    [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml).
