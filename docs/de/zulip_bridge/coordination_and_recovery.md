# Coordination, bootstrap und recovery

Status: **proposal; obligatorische semantics, transport/runtime details teilweise OPEN**.

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

Das Dokument ersetzt die bisherigen Schemata durable old-queue cursor catch-up, message-level
history checkpoint Die durable coordination lebt in der Workspace;
Bridge local state — Abgeworfen cache.

## Authentication Vor coordination

Jede private control/Provider/file request geht zuerst durch die aktuelle
realm-bound mTLS authentication `workspace-external-bridge-api`: TLS client
certificate definiert `realm_uuid`, `provider_kind`, `bridge_instance_uuid` und
`identity_generation`; current backend state wird bei jedem
request. Einmalige Einschreibung, Zertifikatserneuerung/revokeUnd secret storage nicht
Diese Verordnung wird in proposal.

Nur nach Authentifizierung überprüft Workspace whole-account assignment,
lease/fencing generation und project/chatLease beantwortet die Frage, was
instance Sie ist jetzt der Eigentümer des Accounts, aber sie bestätigt nicht den Prozess. stale
lease Bei gültigem Zertifikat wird die Genehmigung verweigert, ein neuer Mietvertrag nicht
lässt eine nicht-authentifizierte Anfrage akzeptieren.

## Whole-account lease und fencing

Workspace Vergibt einem Bridge-Instanz-Lease für den gesamten externen Account und
monotonic fencing generation. Account nicht zwischen instances aufgeteilt stream,
topic Private API nimmt nur die Mutation/task/receipt an, wenn
Aktivlease und einhergehende generation.

V1 Erlaubt eine Bridge-Instanz, aber das Assignment-Modell gleichzeitig multi-instance:

1. Workspace Sie prüft nur healthy compatible instances.
2. Neuer Account erhält eine Instance mit minimalem normalized load
   `active_accounts / declared_capacity`; tie-breaker Es muss stabil sein..
3. Assignment sticky: Das Erscheinen einer neuen Instance wird nicht ausgeglichen healthy
   accounts automatisch.
4. Die neuen Accounts und deren Accounts werden übertragen. owner dead/draining.
5. Realtime und die Geschichte eines Kontos sind immer bei einem Account owner Bridge.

- Heartbeat wird jedes `10s` gesendet; die Instance wird `degraded` nach
  `30s` ohne Herzschlag und `offline` nach `60s`.
- Graceful shutdown/draining Sie beendet die neuen Ansprüche und entlässt leases.
- Nach `60s` offline Timeout, die neue Instanz, die den gesamten Account beansprucht, erhält eine neue
  fencing generation Und es startet den gleichen bootstrap.
- Stale owner kann nicht commit provider receipt, task result oder cursor advance.
- Disconnect/delete generation zurückruft; work wird nicht auf andere übertragen account.
- Durable account/tasks/mappings/outbound errors Sie bleiben Workspace-owned.

## Ein Bootstrap-Connect, Reconnect und recovery {#единый-bootstrap-connect-reconnect-и-recovery}

Ein Algorithmus wird nach connect, reconnect, lease takeover, queue
expiry, missing heartbeat, `restart` und `web_reload_client`:

1. Überprüfen Sie die aktuelle mTLS Identity Check und überprüfen Sie dann active account, verified
   credential und whole-account lease.
2. Nur für die neue Zulip Event-Warteschlange registrieren supported event types.
3. Erhalten Sie eine Registrierungsgrenze, die ausreicht, um snapshot/history split.
4. Bei Registrierungsfehler mit Backoff wiederholen; keine History-Root erstellen.
5. Sequential realtime consumption von neuem starten boundary.
6. Es ist möglich, eine Workspace History Root Task mit account selection,
   `history_depth`, boundary und lease generation.

Die alte queue ID/cursor benötigt keine durable recovery.
Erstellen Sie gap: history umfasst selected snapshot/range bis boundary, realtime
— events Die Inclusive/exclusive Wire-Repräsentation hängt von Zulip
registration response Und es bleibt der private Transport detail, aber die Umsetzung ist verpflichtet.
Die zulässige Überlappung wird dedupliziert.
provider object/event keys.

## Realtime terminal acceptance

Connector per account hält maximal ein inbound supported event im Arbeitsplatz:

1. Erhalten next event.
2. Vergleichen Sie genau mit einem private Workspace Befehl oder lifecycle signal.
3. Wiederholen Sie den gleichen Befehl/key bei transient/ambiguous failure.
4. Auf applied/duplicate/stale/confirmed oder classified permanent failure
   - Ich zähle. event terminal.
5. Nur nach der Terminal-Akzeptanz wechseln Sie zu next event.

Das bedeutet nicht , dass die alte Warteschlange nach dem loss: queue recovery wieder durable reuse wird
Die Provider Keys machen Replay/overlap sicher.

## History task model Ohne message checkpoint

Workspace Speichert immutable/root task und per-stream child tasks.
selected chats, discovers users/streams/topics/memberships und erschafft child task
Child fixiert die Daten für jeden Channel/direct/group-direct Stream. immutable input:
account, stream, history range, boundary und provider task identity.

In v1 gibt es keinen Message-Level-Checkpoint. terminal completion,
Der nächste Claim wiederholt den gesamten Selected Stream-Range neuest-first.
Die verwendeten Objects geben schnell duplicate/no-op per Provider keys zurück.
completed stream tasks Die Aufgaben haben normale `pending` →
`leased/running` → `completed`/`failed` transitions, attempts/backoff, lease
expiry, fencing, bounded retry und DLQ/reconciliation evidence.

Verschiedene Streams auf demselben Account können parallel ausgeführt werden
Bridge über den gemeinsamen configurable pool, default `4`; exact maximum/optimum bleibt
Ein Stream gehört gleichzeitig einem anderen history worker.
Topics/messages Sie gehen in der Folge, weil Zulip topic —
Attribut message; messages `created_at DESC` mit stable provider-message
tie-breaker. Zwischen Konten schlägt der Scheduler fair Round-Robin ein, innerhalb
account — last activity/newest stream first.

Alle History-Arbeiter eines Kontos teilen sich den Account-level Zulip Rate Limiter.
`Retry-After` history Dieser Account wird auf provider interval.
Realtime lane ist separat, hat Priorität und wird zuerst fortgesetzt; history nicht
kann das Budget, das er braucht, ausgeben realtime.

## Retry und permanent classification

| Outcome | Wirkung |
| --- | --- |
| transient transport/`429`/temporary unavailable | Backoff+jitter, Das ist es. provider/operation/task key; no advance |
| applied / duplicate / stale | Terminal success; no repeated outbox/event for no-op |
| missing older dependency | Durable deferred reference; current event/task kann nach nachgewiesener Speicherung abgeschlossen werden dependency |
| invalid/cross-scope/conflicting verified owner | Fail-closed, permanent evidence/admin resolution |
| internal outbound `permanent_failed` | - Haltet an endless retry; safe code/reason private, current public delivery shape unchanged |
| unsupported family | Nicht unterschreiben; unexpected occurrence audited, ohne guessed mutation |

Completed history tasks und successful outbound operations werden über
`30 days`; permanent-failure operation/code/reason — Über `90 days`. Future
manual requeue Das ist eine interne Erweiterung.
für externe Operationen, ersetzt nicht die interne classification und wird nicht erweitert
Das ist ... proposal.

## Deferred references und reconciliation

Newest-first history kann quote/file/older message reference vorher sehen
mapping. Workspace Er behält die interne deferred reference, und nach dem Auftreten
mapping Wir können ihn reparieren. canonical Markdown/URN/mentions. Actual change
schreibt outbox und ready event; no-op schreibt nicht event.

Reconciliation Überprüft:

- active account lease/generation und fehlende stale commits;
- history root/child coverage, failed/DLQ tasks und selected range totals;
- provider-key uniqueness, gaps/duplicates und multi-account union references;
- topic alias mappings, file attachment links, unresolved references;
- pending/retryable/permanent outbound operations;
- projection/outbox/task/ready-event consistency in Workspace.

## Backpressure und graceful restart

Realtime intake history throughput wird nicht ersetzt: realtime wird immer unterstützt
früher. History default pool `4`, upper limit/rate/batch limits
bounded/configurable. Fair round-robin Es gibt keinen einzigen Account, der das Monopol hat.
pool. Bei graceful
stop Bridge beendet neue claims/provider calls, beendet oder freigibt
current unit, conditional schreibt nur terminal result und gibt eindeutig leases.
Bei Hard Crash Takeover ist nur nach `60s` offline Timeout erlaubt; neu owner
mit der neuen Generation wiederholt bootstrap und unfinished stream task range.

Die Beobachtungsmöglichkeiten umfassen account generation/lease age, queue registration
failures, realtime event age, history root/stream lag, restarts/full-range
replays, duplicate/no-op ratio, deferred/DLQ age, outbound retry/permanent
failure und einzeln Workspace projection/WebSocket lag. Content, `api_key`, raw
payload und personal identifiers nicht in labels/errors.

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
