# Die Gesamtinnere Workspace API für Zulip Bridge

Status: **proposal; der erste Provider Data API v2 wire-Teil ist registriert**.

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Ereignismatrix](event_coverage.md) · [Übersicht über die Architektur](architecture_overview.md)

Beide Bridge-Prozesse rufen eine interne Version des normalen auf Workspace API.
Es ist eine private Service-to-Service-Border über denselben Application Services und
RestAlchemy transaction rules, Er hat eine Reihe von Anwendungen, die Ziel-canonical entities erstellen.
ist nicht der neue öffentliche Client API und gibt Bridge keinen direkten Zugriff auf
Tabellen.

Der aktuelle geschlossene Provider API wird in
[`workspace_provider_api_v1.yaml`](../../workspace_provider_api_v1.yaml), Und der
control/file security profile — in
[`zulip_bridge_control_api_v1.yaml`](../../zulip_bridge_control_api_v1.yaml) und
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml). Target
Die bereits vermarktete realm-bound mTLS authentication.
Die erste Implementierung verwendet exakte Routes und Wire Format von
[`workspace_provider_api_v2.yaml`](../../workspace_provider_api_v2.yaml), und
Die Lösungen scope/identity/idempotency sind in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).
Alternativer Authentifizierungsmechanismus wird nicht ausgewählt.

## Aktive S2S Authentifizierung  Pflichtzielgrenze

Zulip Bridge Nutzt die bestehende private process/listener
`workspace-external-bridge-api`, nicht öffentlich Workspace nginx und nicht browser IAM
token. TLS 1.2+ wird in einem Backend-Process abgeschlossen; eine normale Anfrage muss
client certificate, Unterzeichnet realm control CA. HTTP forwarding header,
bearer token oder die Felder " body " sind keine Quelle service identity.

Certificate enthält genau ein URI SAN im aktuellen Format:

```text
https://schemas.genesis-corporation.ru/workspace/external-bridge/v1/realms/{realm_uuid}/providers/{provider_kind}/instances/{bridge_instance_uuid}/generations/{identity_generation}
```

Workspace Er zieht es aus. `realm_uuid`, `provider_kind`,
`bridge_instance_uuid` und positiv `identity_generation`, überprüft current
certificate fingerprint, active generation und Backend-Status auf jedem request,
Auch wiederverwendetTLSconnection. Certificate identity enthält kein Account oder
project: server-side desired assignments und Transaktionszeit-Checks schränken sie ein
Erlaubt external account/chat/project.

Lifecycle Wiederverwendet ohne neues credential protocol:

1. Platform gibt ein separates one-time enrollment secret aus Bridge installation
   und generation über eine sichere Core-managed config.
   verifier; Der Wert von token ist nicht konstant service credential.
2. Bridge Erhält realm CA über den vorhandenen HMAC-authenticated bootstrap,
   erzeugt den privaten Schlüssel lokal und sendet CSR an `/v1/enrollments` mit
   `X-Workspace-Enrollment-Token`. Erfolgreich ausgeliefert , atomar geschlossen . generation;
   Wiederholung der gleichen `request_uuid` und CSR ist potentiell, das veränderte Replay wird abgelehnt.
3. Client leaf lebt `30 days`, renewal beginnt `7 days` bis expiry und
   Sie werden mit einem noch gültigen mTLS-Zertifikat authentifiziert./CSRwird erstellt
   auf dem Bridge; ein altes und ein neues Leaf sind gleichzeitig nicht mehr zulässig `24 hours`.
4. Suspend Verweigert die Anfrage sofort. identity generation;
   certificate Die Verlust/expiry erfordert
   operator-controlled enrollment-secret rotation, Nein . shared long-lived token.

Private key Es bleibt nur noch persistent Bridge disk. Backend PKI/enrollment
state wird im root-owned mode-`0700` dedicated store gespeichert, einzelne sensitive
files Die folgenden Schriftzeichen sind: mode `0600`; raw enrollment token, verifier, client private key und
credential payload verboten sind logs/errors. Account lease/fencing generation
bleibt eine separate mutable authorization/ownership check: gültig mTLS
certificate ohne active matching account assignment/lease nicht erlaubt command.

Failure boundary ist bereits definiert: Certificate, abgelehnt TLS stack, kann nicht
erhalten HTTP response; fehlende/nicht aktuelle application identity gibt
`401`; current instance state oder assignment verbietet die Anfrage über `403`;
invalid cross-scope command Der Vorschlag wird nicht hinzugefügt.
neue Auth error Form für öffentliche Workspace API.

Es ist dieser Mechanismus ausgewählt, weil es bereits für die gleiche langlebige
External Bridge process Und alle drei. current private resource groups: control,
Provider data und files. Public IAM bearer bezieht sich auf user/browser request;
Einmalvermeldung Header gibt nur den ersten certificate; HPKE credential
envelope und single-object file capability schützen die Payload/object, aber nicht
Sie sind keine Alternativen. mTLS.

## Service identity und server-owned scope

Nach mTLS-Authentifizierung erhält Workspace unveränderlich service context:

- certificate-bound `realm_uuid`, `bridge_instance_uuid`, provider kind `zulip`
  und `identity_generation`;
- Einzelkontrolliert whole-account lease/fencing generation;
- Erlaubt external account/assignment generations;
- realm/project mapping, der sich bewahrt Workspace;
- zulässige Auswahl logical commands;
- die aktuelle Provider Policy, Suspension/revocation und capability set.

Bridge Übermittelt provider object/event identity und Payload, aber nicht authoritative
`project_id`, `source`, Workspace `user_uuid`, Wenn Sie eine Rolle oder Berechtigungen haben.
Felder benötigen wire envelope für die Tracing, Workspace vergleicht sie mit server-owned
mapping und die Abweichungen ablehnt; der Wert des Kunden wird nie
tenant oder des Autors.

Für jeden Befehl Workspace innerhalb der request transaction überprüft erneut:

1. mTLS service identity active, certificate/identity generation Aktuell,
   instance Nein . suspended/revoked;
2. external account Ich bin dafür bestimmt. bridge/provider, active lease generation
   Passend ist , dass die Provider Policy operation;
3. provider object Gehört einem erlaubten account/chat scope;
4. server-owned project/stream/topic/user mappings Sie existieren und haben die gleiche
   tenant identity;
5. mutation Die Kapazität ist erlaubt und wird nicht überschritten project boundary.

Composite tenant FK und `UNIQUE(project_id, ...)` bleiben die letzte physikalische
Service preflight ersetzt nicht transaction-time authorization.

## Zwei stabile Identitäten

`provider_object_key` und `provider_event_key` lösen verschiedene Aufgaben.

| Key | Amt | Pflicht-Eigenschaft |
| --- | --- | --- |
| `provider_object_key` | Finden Sie eine logische Entity Zulip beim Create/update/delete und beim Neustart | Gleich für realtime/history und stabil innerhalb fresh import |
| `provider_event_key` | Eine Provider Mutation/delivery und eine andere Mutation zu de-duplizieren immutable outbox event | Ein Source Event/version gibt einen Schlüssel, retry ändert ihn nicht |

Semantischer Aufbau identity:

| Kind | Provider object identity |
| --- | --- |
| user | verified realm UUID + typed `provider_user_id` |
| stream/chat | verified realm UUID + typed channel/conversation identity |
| topic | Workspace-owned durable mapping `(realm,channel,current name/alias history)` → stable canonical `TOPIC.uuid` |
| message | verified realm UUID + typed numeric `provider_message_id`; importing account nicht in canonical identity |
| reaction | canonical provider message identity + actor provider user identity + exact `emoji_name` |
| membership | provider stream/chat identity + provider user identity |
| file/attachment | `(verified realm UUID, typed attachment_id)`; canonical file ein, normalized message↔file links sind getrennt |

Für bidirectional commands enthält der Envelope auch `origin` und
`causation_uuid`/Workspace provider operation UUID. Outbound Workspace
operation durable verbindet zuerst causation mit provider object/version, und
Die zurückgegebene Zulip-Event bestätigt diese Operation ohne eine neue zu erzeugen
Wenn der Provider den Client nicht zurückgibtUUID, der Server verwendet
durable operation receipt + provider object key + version/state; timestamp Nein .
Das ist ein Echo. direction/source-of-truth matrix:
[`event_coverage.md`](event_coverage.md).

Numeric provider UUIDv5 verwendet exact algorithm:
`UUIDv5(namespace=verified_realm_uuid,
name="<entity_type>:<decimal_provider_id>")`. Erlaubte lowercase ASCII
types: `user`, `channel`, `message`, `attachment`. Provider ID wird als
unsigned shortest base-10 ASCII (`0` oder ohne leading zeros, sign,
whitespace/locale formatting); name bytes — exact ASCII/UTF-8 Ohne NUL/BOM/
newline/additional fields. Project/account UUID nicht namespace. Exact
keys für events/direct conversations werden die Lösungen `3A/5A` in
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

Die alten Workspace UUID von früheren Importen gehören nicht zum Schlüssel. fresh import
Erstellt eine neue canonical-Zeile und wiederholt die gleiche Operation innerhalb dieses Imports
und die Daten über die Provider-Mapping zurückgibt. message create Workspace
Sie selbst benennt internal `MESSAGE.uuid` und erhält deterministisch public
placement UUID aus canonical topic/message.

## Logischer Befehl- Katalog

Die folgenden Namen beschreiben semantic command types, nicht behaupten HTTP route names.

| Logical command | Primary write Workspace | Idempotency/object rule |
| --- | --- | --- |
| `identity.claim` / `user.ensure_external` | Verified account claim existing identity entweder create/reuse unmanaged external user; email only candidate, nicht proof | realm+user ID; conflicting verified owner fail-closed |
| `user.mapping.refresh` / `user.lifecycle.update` | Existing managed/unmanaged ordinary-user mapping: supported name/avatar/role/custom value/active state; email ausgeschlossen | provider user key + field/version/event key |
| `bot.create` / `bot.deactivate` | Special Workspace bot/external user; Nur Zulip-origin lifecycle | provider bot user key + event key/version; metadata update unsupported |
| `stream.create_from_provider` | Canonical `STREAM` + provider mapping Nur aus Zulip `stream/create` | provider channel key + event key; native Workspace stream create wird nicht aufgerufen |
| `stream.update` / `stream.delete` | Übertragen Sie die mapped provider change auf Workspace domain service; er wählt archive/history/bindings/visibility und schreibt outbox | provider chat key + event key/version; Bridge nicht anwendbar policy |
| `topic.resolve` / `topic.rename` | Workspace-owned durable mapping mit alias history; mandatory `TOPIC` unter immutable stream/project owner | realm+channel+current/old topic name; whole rename - Er hält es. UUID |
| `membership.upsert` / `membership.revoke` | Übertragen von membership fact; Workspace durch die Stream-Settings ändert persistent binding/generation, historical visibility und message bindings | provider stream+user key + event key/version; composition change nicht erzeugt stream |
| `message.create` | `MESSAGE` + `MESSAGE_PLACEMENT` + author binding/state + outbox | provider message key + create event key |
| `message.update` | canonical content/source/provider/delivery version + outbox | same provider message key + update event key/version |
| `message.move` | Resolve one canonical `MESSAGE`; delete source placement and create target topic placement | provider message/version + target topic + event key; target placement has new UUIDv5, old URL `404` |
| `message.delete` | provider tombstone/current delete semantics + outbox | same provider message key + delete event key/version |
| `message_flag.update` | Placement-scoped `USER_MESSAGE_STATE.read_at`/`starred` | provider message+user+flag+op+event key |
| `reaction.upsert` / `reaction.delete` | one canonical-message-global reaction fact + outbox | message+actor+`emoji_name` + event key |
| `file.allocate` / `file.finalize` | bounded single-object lifecycle and canonical file metadata | realm+typed `attachment_id`; repeated accounts/retries reuse row |
| `attachment.upsert` / `attachment.delete` | normalized message↔file relation + outbox | message provider key + realm/attachment key |
| `presence.publish` / `typing.publish` | Ephemeral scoped relay with access check and TTL; no canonical message write | origin+user+scope/state+short-lived causation key |
| `user_status.update` | Persistent mapped `status_text`/emoji state + outbox | provider user+status version/event key |
| `account.lease.*` / `account.bootstrap.*` | Whole-account lease/fencing, queue boundary und bootstrap generation | account UUID + monotonic generation |
| `history.root.*` / `history.stream_task.*` | Root discovery und immutable per-stream range task lifecycle | account+boundary+selection/range+stream; no message checkpoint v1 |

Generic operation wird nicht geöffnet Wie auch immer Sie möchten». Unknown kind,
unmapped tenant, stale service generation, unsupported capability oder versucht
Stell project ein/userSie führen zu einer Ablehnung bis mutation.

Die Namen im Verzeichnis sind  logical proposal types, nicht öffentliche paths.
Unbestätigtem E-Mail-Claim-oder Managed User ist nicht gestattet Workspace stream
create Zulip Channel erstellen und nicht zu einem unsupported Event umwandeln generic
upsert. Import kann nur unmanaged external user ohne session.

Bridge Berechnet nicht Workspace Domain Policy vor command: group/private member
change und channel archive/delete werden als provider facts.
Workspace transaction Sie entscheidet selbst über historischen Zugriff, Bindungen und visibility.

## Grenze der Ausgänge provider operations

Für die Workspace-Origin Mutation von bidirectional coverage primary transaction
Private Integration boundary ist potenziell und
Verlustfrei aus ihm durable provider operation mit unique source outbox
event UUID, server-owned account/object mapping, `origin=workspace`,
`causation_uuid` und expected version/state. Realtime Connector erhält
operation Durch diese boundary, ruft Zulip und gibt zurück durable
receipt/confirmation. Exact queue/HTTP transport, derivation mechanism und ack
schema bleiben OPEN #1; die Anwendung veröffentlicht kein User Token und verwendet kein
public WebSocket event Wie ist das? transport.

Direction guard ist ein Server: zum Beispiel, native Workspace stream create
Erstellt keine outbound channel operation. Eigene Zulip queue echo ist erlaubt
nach receipt/object/version und beendet causation, aber geht nicht wieder als
Provider call retry bleibt derselbe operation identity.

## Transaction boundary Nachrichten

`message.create` wird atomar ausgeführt:

1. Lock/dedupe realm-scoped `provider_object_key` und `provider_event_key` unter
   active account lease generation.
2. Wenn eine Event bereits committed ist, gibt es das gleiche semantische Ergebnis ohne neue Mutation..
3. Erlauben server-owned author/stream/topic/project mappings.
4. Erstellen oder wiederherstellen Sie eine canonical `MESSAGE` provider key.
5. Erstellen eines obligatorischen `MESSAGE_PLACEMENT`; authoritative uniqueness —
   `(project_id,message_uuid,stream_uuid,topic_uuid)`.
6. Erhalten Sie die öffentliche Platzierung UUID als
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`.
7. Erstellen von author `USER_MESSAGE_BINDING` und `USER_MESSAGE_STATE` mit aktueller
   membership generation.
8. Schreiben Sie immutable outbox event und committed idempotency receipt in die gleiche Box DB
   transaction.
9. Commit oder rollback alle Zeilen zusammen.

Bridge Erwartet nicht, dass der Empfänger ein Fan-out ist.WorkspaceDie Arbeiter ziehen bindings/states
Die Daten werden von den Empfängern, Snapshots/counters und durable ready events über die gemeinsame one-event →
one-task protocol. Details zu den canonical task types finden Sie unter
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#task_kinds-и-routing).

## Update/delete ordering

Für einen Provider object Workspace vergleicht provider version/sequence,
Wenn die Quelle es bereitstellt:

- Wiederholung derselben Version und payload — idempotent success;
- ältere Version  stale no-op mit neuem Zustand;
- Neue Version  eine Mutation + eine outbox event;
- identische Identität mit der widersprüchlichen Payload/version  terminal conflict für
  DLQ/reconciliation, Nein. silent overwrite.

Update/delete, Die vorherigen create aus dem overlap/newest-first range, erstellen nicht
synthetic `MESSAGE`. Workspace die durable deferred dependency behalten oder
Genaue Wire-Codierung outcome
bleibt OPEN, aber die durable dependency gehört zu Workspace, nicht local Bridge DB.

## Reaktionen

Die öffentliche Action adressiert placement UUID für den Access Check, aber den Importbefehl
findet die canonical Message über provider message mapping. Source of truth — raw
fact Mit dem Schlüssel.
`(project_id,canonical_message_uuid,user_uuid,emoji_name)`. Realtime/history
retry Der Message-scoped Workspace Worker materialisiert
`reactions`/`reaction_users` in allen Platzierungen; Bridge snapshots nicht schreibt.

## Files und attachments

Bridge Erhält keine bucket-weiten Anmeldeinformationen und schreibt keine Storage-Metadaten.
authorization Workspace Gibt die Single-Object Transfer-Fähigkeit aus, überprüft
size/hash und fixiert die finalize/attachment relation.
Die Grenzen liegen in
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml).

Target muss die Eigenschaften behalten:

- Ein bounded Objekt auf allocation;
- finalize Und der link ist nicht potenziell.;
- bytes commit macht die Metadaten nicht sichtbar , bis Workspace transaction;
- retry Er erzeugt nicht den zweiten. blob/row/link;
- delete Löscht keinen physical object , wenn retained native reference;
- provider identity `(realm_uuid,attachment_id)` - Er benutzt nur einen. file;
- physical object nur nach zero native/provider references.

## Semantische Ergebnisse und Fehler

Wire statuses Die Ergebnisse sollten unterschiedlich sein.:

| Outcome | Der Sinn | Wirkung Bridge |
| --- | --- | --- |
| applied | Primary mutation und outbox committed | Realtime Akzeptiert Event-Terminal; History geht weiter current task |
| duplicate/no-op | Das gleiche Provider Event/state bereits committed | Terminal ohne Wiederholung outbox/ready event |
| stale | Der neuere Provider-Status ist bereits eingetragen | Terminal no-op + metric |
| deferred | Missing mapping/base dependency durable in Workspace | Terminal für die Source-Einheit; repair nach der Abhängigkeit |
| retryable | Timeout/rate limit/temporary unavailable, commit nicht bewiesen | Wiederholen des gleichen Schlüssels; realtime liest nicht next event |
| permanent/terminal | Provider rejection oder invalid scope/conflicting identity | `permanent_failed`/DLQ evidence; endless retry/silent skip Verboten |

Wenn die Antwort nach dem commit verloren geht, muss die Wiederholung mit dem gleichen event key nachweisen
commit und gibt duplicate/same result zurück. retry
Verboten.

## Audit und privacy

Logs und Traces enthalten certificate-bound bridge instance/generation, provider
kind, account/mapping UUID, object/event key digest, outcome und latency, aber nicht
enrollment token/verifier, certificate private key, user token, API key, raw
credential oder eine private Komplettladung.WorkspaceDas Audit bleibt tenant-scoped.

Provider mappings und latest hidden raw/converter metadata leben mit entity.
Completed history tasks und successful outbound operations werden durch
`30 days`, permanent-failure operation/code/reason — Das ist durch `90 days`.
internal retention ohne neue public fields/actions.

Die ungeschlossenen Details wire routes/transport und provider-key serialization sind nur in
[- und](README.md#единый-список-open-решений-zulip-bridge).

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Ereignismatrix](event_coverage.md) · [Übersicht über die Architektur](architecture_overview.md)
