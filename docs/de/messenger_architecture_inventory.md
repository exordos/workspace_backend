# Kanonisches Inventar der proposal-Architektur Messenger

Status: **proposal; maschinell abgleichbares Wörterbuch für Dokumentation, nicht production schema**.

[← Hauptindex der Dokumentation](index.md) · [Domänenmodell](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)

Dieses Dokument ist das einzige kanonische Inventar von Namen, Schlüsseln,
UUID-Die Daten sind in der Regel von den folgenden Algorithmen, Aufgaben/Ereignisse und verbleibenden OPEN-Lösungen erfasst:
Die Erläuterungen sind in den Proposal-Dokumente verknüpft;
Der Vertrag bleibt in [`workspace_api.md`](workspace_api.md).

## Status

- **current contract** — method/path/JSON/status/event shape aus
  `workspace_api.md`; proposal Und niemand schreibt es, außer denjenigen, die es offenlegen.
  compatibility changes pagination/timing.
- **current runtime** — Ersatzbare Umsetzung `m_workspace_*`, custom store und
  views; Das ist Beweismaterial, nicht target architecture.
- **proposal target** — die unten ausgewählten Namen der zukünftigen Tabellen/viewsund
  RestAlchemy declarations; migration/implementation noch nicht erstellt.

## Physikalische Modelle proposal

Alle tenant-owned Zeilen tragen `project_id`, haben `UNIQUE(project_id, uuid)` und
Komponenten von FK`(project_id, referenced_uuid)`- Die öffentliche .UUID-Die Links sind
scalar `types.UUID()` properties; internal UUID FK/identity Das ist auch skalar, aber
`relationships.relationship` wird nicht für
- Das ist nicht wahr .UUID, weil es sich als URI.

| RestAlchemy/domain class | Target table | Public / internal fields | Business key und die Kardinalität |
| --- | --- | --- | --- |
| `WorkspaceMessage` | `messenger_messages` | internal canonical `uuid`; hidden indexed `provider_realm_uuid`,`provider_message_id`; public content/author/source/timestamps/snapshots Über view; public `provider.account_uuid` wird von access/account projection | `UNIQUE(project_id,uuid)`; provider uniqueness logically `(provider_realm_uuid,provider_message_id)` within the chosen cross-account project projection; Einer canonical content row |
| `WorkspaceMessagePlacement` | `messenger_message_placements` | public message `uuid`; internal `message_uuid`,`stream_uuid`,`topic_uuid` FK | `UNIQUE(project_id,message_uuid,stream_uuid,topic_uuid)`; many placements → one message |
| `WorkspaceUserMessageBinding` | `messenger_user_message_bindings` | hidden row `uuid`; internal `placement_uuid`,`user_uuid`,`membership_generation`, access | `UNIQUE(project_id,user_uuid,placement_uuid)`; many users → one placement |
| `WorkspaceUserMessageState` | `messenger_user_message_states` | internal `placement_uuid`,`user_uuid`,`membership_generation`; placement-scoped `read_at`,`mentioned`,`starred`,`pinned` | `UNIQUE(project_id,user_uuid,placement_uuid)`; re-add resets the same keyed row to fresh defaults/current generation |
| `WorkspaceMessageReactionFact` | `messenger_message_reaction_facts` | internal canonical message FK; public reaction row resolves placement only for access | `UNIQUE(project_id,canonical_message_uuid,user_uuid,emoji_name)`; canonical-message-global facts |
| `WorkspaceStream` | `messenger_streams` | public `uuid`; physical `owner_uuid`,`direct_user_uuid` indexed FK; canonical fields | `UNIQUE(project_id,uuid)`; one canonical stream |
| `WorkspaceStreamBinding` | `messenger_stream_bindings` | public binding UUID/role/notifications; internal `active`,`membership_generation`; ready stream counts | `UNIQUE(project_id,user_uuid,stream_uuid)`; persistent tombstone survives revoke/re-add |
| `WorkspaceStreamTopic` | `messenger_topics` | public topic UUID; canonical `stream_uuid`,`is_done`,`version`, summary/source fields | `UNIQUE(project_id,uuid)` and `UNIQUE(project_id,stream_uuid,uuid)`; exactly one immutable owner stream/project |
| `WorkspaceUserTopicBinding` | `messenger_user_topic_bindings` | access/notifications/ready counts only; no authoritative `is_done` | `UNIQUE(project_id,user_uuid,topic_uuid)` |
| `WorkspaceFolder` | `messenger_folders` | public canonical UUID/title/color/system type | `UNIQUE(project_id,uuid)`; one canonical folder |
| `WorkspaceUserFolderBinding` | `messenger_user_folder_bindings` | access/rule, ready counts, read-only materialized `folder_items_snapshot` JSONB, projection version/time | `UNIQUE(project_id,user_uuid,folder_uuid)`; one viewer row per folder |
| `WorkspaceFolderItem` | `messenger_folder_items` | authoritative normalized item fields and indexed stream/folder/user FK | `UNIQUE(project_id,user_uuid,folder_uuid,stream_uuid)`; many items → one folder binding |
| `WorkspaceUser` | `messenger_users` | public user fields/UUID; provider internals hidden | `UNIQUE(project_id,uuid)` for tenant association; canonical user identity rules remain current contract |
| `WorkspaceDomainOutboxEvent` | `messenger_domain_outbox_events` | immutable `event_kind`,`scope_kind`,`scope_key`,`payload` | `UNIQUE(project_id,uuid)`; one source mutation event |
| `WorkspaceProjectionTask` | `messenger_projection_tasks` | immutable source reference + lease/retry/DLQ lifecycle | `UNIQUE(project_id,outbox_event_uuid)`; exactly one root typed task per outbox event |
| `WorkspaceProjectionScopeLease` | `messenger_projection_scope_leases` | owner/expiry/fencing | `UNIQUE(project_id,scope_kind,scope_key)`; at most one current writer per exact scope |
| `WorkspaceFanoutRoot` | `messenger_fanout_roots` | placement/root cursor/count/status | `UNIQUE(project_id,outbox_event_uuid)`; one root per send/fanout source event |
| `WorkspaceFanoutBatchTask` | `messenger_fanout_batch_tasks` | immutable root + non-null `batch_no` + nullable keyset boundary | `UNIQUE(project_id,fanout_root_uuid,batch_no)`; sequential bounded batches, first batch `batch_no=0` |
| `WorkspaceEvent` | retained current `m_workspace_events` | public immutable event row/cursor/sanitized payload | existing event identity/cursor; projection + ready rows commit atomically |
| Files/attachments (physical names OPEN) | target table/link names nicht ausgewählt | current file public JSON wird gespeichert; hidden provider identity `(realm_uuid,attachment_id)` und normalized attachment FK | Eine canonical file auf realm+attachment; message links separate; physical blob wird nur gelöscht, wenn zero references |

## Read-only API models/views

Jede Ansicht hat eine führende physische Zeile und ist nur one-to-one oder
many-to-one joins. `COUNT`, `GROUP BY`, window/lateral/correlated query,
`json_agg`, N+1 und custom SQL store sind nicht erlaubt.

| RestAlchemy read model | Target view | Leading row / public identity | Bereite Quellen |
| --- | --- | --- | --- |
| `WorkspaceUserMessage` | `messenger_api_user_messages_v1` | leading `WorkspaceUserMessageBinding`; hidden ORM key `binding_uuid`; public `uuid = MESSAGE_PLACEMENT.uuid` | placement context/state + canonical message/timestamps/snapshots; active stream membership+generation security join |
| `WorkspaceMessageReactionView` | `messenger_api_message_reactions_v1` | leading raw fact/access-scoped placement; public message UUID = placement UUID | fact row + sanitized provider/delivery; canonical global semantics |
| `WorkspaceUserStream` | `messenger_api_user_streams_v1` | leading active stream binding; public UUID = canonical stream UUID | ready counts from binding + one stream; `owner_uuid AS owner`; viewer-relative scalar `direct_user_uuid` |
| `WorkspaceStreamBindingView` | `messenger_api_stream_bindings_v1` | leading persistent stream binding; public binding UUID | binding fields; viewer/project scope |
| `WorkspaceUserTopic` | `messenger_api_user_topics_v1` | leading topic binding; public UUID = canonical topic UUID | ready counts/notifications from binding + canonical `TOPIC.is_done`/summary/timestamps |
| `WorkspaceUserFolder` | `messenger_api_user_folders_v1` | leading folder binding; public UUID = canonical folder UUID | one folder join + ordinary read-only `folder_items_snapshot` property + ready counts |

## UUID und identity

| Identity | Kanonische Regel |
| --- | --- |
| canonical message | internal `MESSAGE.uuid`; eine Content Row, nicht public message resource ID |
| public message/URL `{message_uuid}` | `MESSAGE_PLACEMENT.uuid` |
| placement UUIDv5 | `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)` |
| UUIDv5 name bytes | Nur lowercase hyphenated ASCII canonical `MESSAGE.uuid`, ohne Klammern, Präfix oder zusätzliche Felder |
| topic requirement | `TOPIC` ist für jede Placement obligatorisch, einschließlich direct/self-chat; null/sentinel ist verboten |
| topic ownership | global einzigartig `TOPIC.uuid`, unveränderlich, genau einem Stream zugehörig/project; die Übertragung erzeugt ein neues topic/placement migration |
| authoritative placement uniqueness | `(project_id,message_uuid,stream_uuid,topic_uuid)` + composite FK; UUIDv5 ersetzt nicht constraints |
| hidden row identities | binding/state/view technical UUID nicht veröffentlicht und in message URL/marker |
| message pagination tuple | public `(MESSAGE.created_at, MESSAGE_PLACEMENT.uuid)`; page rows Einzigartig nach placement UUID |
| Zulip numeric provider objects | `UUIDv5(namespace=verified_realm_uuid, name="<entity_type>:<decimal_provider_id>")`; realm text canonical lowercase hyphenated → 16 RFC 4122/network-order octets; allowed types `user/channel/message/attachment`; ID unsigned shortest base-10 ASCII (`0` oder no leading zeros); name exact ASCII/UTF-8; no project/account namespace |
| Zulip topic | Workspace-owned durable realm+channel+name/alias-history mapping → stable `TOPIC.uuid`; mutable name alone never defines UUID |
| Zulip Bridge account ownership | sticky whole-account assignment to minimum normalized-load healthy compatible instance; realtime+history share one fenced owner; heartbeat `10s`, degraded `30s`, offline/takeover `60s` |
| Zulip Bridge S2S authentication | current `workspace-external-bridge-api` realm-bound TLS 1.2+ mTLS; certificate URI SAN = realm/provider/bridge instance/identity generation; one-time enrollment + 30-day leaf/7-day renewal/24-hour overlap; account lease is separate authorization fence |
| Zulip history scheduling | Bridge-wide pool default `4`; fair round-robin accounts, newest stream first, one worker per stream, shared account limiter, realtime resumes first after `Retry-After`; upper pool limit OPEN pending load tests |
| Zulip internal retention | mappings/latest raw metadata = entity lifetime; completed history/successful outbound = `30 days`; permanent failure/code/reason = `90 days` |

## TASK_KINDS und routing {#task_kinds-и-routing}

Initial design Wird nicht durch coalescing ausgeführt. Jedes immutable outbox-Ereignis führt
genau eine unveränderliche Root-Aufgabe `UNIQUE(project_id,outbox_event_uuid)`.
Downstream shared work Erst erhält er eine immutable outbox event exact
scope, dann eine Aufgabe; direktes Zusammenfließen von Ereignissen ist verboten.

| `task_kind` | `scope_kind` / exact key | Der Einzige writable result |
| --- | --- | --- |
| `fanout` | `topic:(project_id,topic_uuid)` | bounded recipient binding+state batches for placements |
| `content_mentions` | `topic:(project_id,topic_uuid)` | placement-scoped mention state; shared work emits exact-scope outbox |
| `reaction_snapshot` | `message:(project_id,canonical_message_uuid)` | canonical `MESSAGE.reactions/reaction_users` |
| `read_counters` | `user-stream`, `user-topic` exact triples | ready container counts/last message on corresponding binding |
| `folder_projection` | `user-folder:(project_id,user_uuid,folder_uuid)` | authoritative deterministic `folder_items_snapshot` + ready folder counts/event |
| `delivery_snapshot_event` | `message:(project_id,canonical_message_uuid)` oder `resource:(project_id,resource_kind,resource_uuid)` | sanitized delivery/resource projection + ready event, oder effect-guarded completion ohne public row für contract families, bei denen kein public event vorhanden ist |
| `topic_state_projection` | `topic:(project_id,topic_uuid)` | ready `topic.updated`; optional rebuildable read-only copy of canonical `is_done` |
| `topic_membership_policy_rebuild` | `topic:(project_id,topic_uuid)` | topic placement/binding policy; shared rows use downstream exact scopes |

Task lifecycle: `pending -> leased/running -> completed`; retryable failure uses
`failed -> pending` with `attempts`, `next_retry_at`, backoff and lease expiry;
`max_attempts` moves to DLQ. Owner/fencing token, reaper/reconciliation and
idempotent `outbox_event_uuid` effect guard are mandatory.

Der Claim-Pfad durchläuft einen gewichteten Lane-Zyklus: vier Fan-out-Slots,
zwei Slots für interaktive Leseaktionen, einen Reaktions-Slot, einen Slot für
nicht interaktiven Lesestatus und zwei Hintergrund-Slots. In jedem Durchlauf
wird projektübergreifend die älteste berechtigte Aufgabe der bevorzugten Lane
gewählt; ist diese leer, folgt die älteste berechtigte Aufgabe einer beliebigen
Lane. Projektsuche und endgültiger Claim verwenden dieselben Vorgänger-, Retry-
und Scope-Lease-Bedingungen. Begrenzte Kandidatenabfragen und partielle Indizes
für aktive Aufgaben vermeiden eine Sortierung der gesamten Aufgabenhistorie;
Scope-Leases, projektbezogene Advisory Locks und Fencing-Tokens schützen
mehrere Worker.

## DOMAIN_EVENT_KINDS

Die innere `WorkspaceDomainOutboxEvent.event_kind` benutzt die gleiche geschlossene
enum von acht Werte, die `task_kind`: `fanout`, `content_mentions`,
`reaction_snapshot`, `read_counters`, `folder_projection`,
`delivery_snapshot_event`, `topic_state_projection`,
`topic_membership_policy_rebuild`. Daher ist event→task derivation mechanisch
Einmalig und nicht willkürliche Zeile. labels
Sie werden als `draft.created` oder `folder_item.pin` gespeichert. `payload.source_kind`;
Sie sind nicht routing EVENT_KIND und nicht mit public WebSocket kind.

## Public EVENT_KINDS

Dies ist eine vollständige Liste `payload.kind`, die von current public contract:

`external_account.created`, `external_account.updated`,
`external_account.deleted`, `external_chat.created`, `external_chat.updated`,
`external_chat.deleted`, `external_operation.created`,
`external_operation.updated`, `external_operation.deleted`, `file.created`,
`file.updated`, `file.deleted`, `folder.created`, `folder.updated`,
`folder.deleted`, `folder_item.deleted`, `message.created`, `message.updated`,
`message.deleted`, `message.read`, `messages.read`,
`message_reaction.created`, `message_reaction.updated`,
`message_reaction.deleted`, `stream.created`, `stream.updated`,
`stream.deleted`, `stream.read`, `stream_bindings.created`,
`stream_binding.updated`, `stream_binding.deleted`, `topic.created`,
`topic.updated`, `topic.deleted`, `topic.read`, `user.updated`.

Worker materializes projection/state and every corresponding ready event row in
one DB transaction. Dispatcher only reads durable rows. Reconnect uses mandatory
cursor + high-watermark + replay + buffer/drain without gap; delivery is
at-least-once and clients dedupe by event UUID.

## Die angenommenen compatibility/operational Regeln

- all public resource-list endpoints: omitted/`0` `page_limit` → `100`, `1..500`
  exact, negative/non-integer/`>500` → HTTP `400`, unlimited mode nicht vorhanden;
- `2xx`/`201` bedeutet "commit primary mutation", nicht completion projections;
  author ergibt immediate RYW, die restlichen Effekte eventual; etwa eine
  Sekunde  SLO intent, keine strenge Garantie;
- fan-out recipients: keyset `USER_STREAM_BINDING.user_uuid ASC`, default batch
  `1000`, configurable hard max `5000`, invalid config fails startup;
- newest-first topic order: `MESSAGE.created_at DESC`, bounded fairness Ich bin verpflichtet
  Ich habe eine Chance, die Arbeit zu verbessern.;
- revoke: persistent `active=false`, `membership_generation++`; all public
  reads/actions recheck active membership+generation synchronously;
- `TOPIC.is_done` — canonical global field; user-topic binding Nein . writable
  source;
- reactions canonical-message-global in allen Placements/audiences  absichtlich
  - die privacy semantics;
- folder item normalized rows bleiben source-of-truth; JSONB nur Snapshot
  rebuildable read model;
- release Erfüllt nach
    [`migration_release_runbook.md`](diagrams/sequence/worker_flows/migration_release_runbook.md):
  native data Zulip-derived messages/files werden übernommen
  destructive reset und fresh complete reimport nach verified backup.

## Status Critic risks

| Risk | Status / kanonische Entscheidung |
| --- | --- |
| #1 | **resolved:** public UUID = deterministic placement UUIDv5 |
| #2 | **resolved:** active membership + monotonic generation security fence |
| #3 | **resolved:** no coalescing; one event→task + lease/retry/reaper/DLQ |
| #4 | **resolved:** exact-scope ownership; topic Nein . universal shared lock |
| #5 | **resolved:** accepted pagination `100/500` und async timing change |
| #6 | **resolved:** canonical `TOPIC.is_done` + lock/version |
| #7 | **partially resolved:** tenant/composite FK/recheck geschlossen; die Non-direct-Role-Richtlinie bleibt OPEN |
| #8 | **accepted:** canonical-message-global cross-audience reactions |
| #9 | **resolved:** atomic projection+ready events; mandatory replay |
| #10 | **resolved:** bounded fan-out batches `1000/5000` |
| #11 | **resolved runbook/safety boundary:** backup+migrations+manual scripts; native preserve, Zulip reset/reimport; provider file identity realm+attachment and zero-reference cleanup are fixed |
| #12 | **resolved:** materialized `folder_items_snapshot`, no N+1/json aggregation read path |
| #13 | **resolved:** cross-document consolidation und machine-checkable QA bestätigten 109 semantic HTTP operations + 1 WS, 7 operational worker flows, einheitliche model/key/task/event/UUID rules und das Fehlen stale/duplicate/orphan artifacts |

## Eine einzige Liste von OPEN-Lösungen {#единственный-список-open-решений}

1. Non-direct stream role/action matrix: Wer fügt den Benutzer hinzu, welche roles
   Benennt, wer die self/other binding ändert/löscht, ob obligatorisch last owner.
2. Ein konkreter Runtime Mechanism exact-scope lease/claim und configurable worker
   execution primitive; invariant fencing ist schon geschlossen.
3. Stable worker tie-breaker bei gleichem `MESSAGE.created_at` und Notwendigkeit
   immutable denormalized sort key Nach den Messungen; API Tuple bereits geschlossen.
4. Zahlenmäßige durable-event retention window/release policy; cursor-too-old bereits
   und er gibt ein offenkundiges Zeichen `epoch_pruned`/`410`.
5. Target physical table names/schema für canonical provider files und normalized
   message↔file links. Identity Sie ist bereits ausgewählt. `(realm_uuid,attachment_id)`,
   zero-reference delete ist obligatorisch; OPEN nur concrete landing tables/FK und
   migration mapping current file rows.
6. Capacity/SLO tuning Nach den Belastungsmessungen: worker concurrency,
   fan-out batch in der Bandbreite `1..5000`, queue admission/backpressure, retention,
   Die Zahl count/bytes limits `folder_items_snapshot` und kompatibel mit dem vollen
   Antwort auf die Überlastungspolitik `All chats`; Hard Boundaries und
   Silent Truncation ist bereits ausgewählt.
7. Eine stabile Public Placement Association für UUID-only
   `GET`/`PUT`/`DELETE message_reactions/{reaction_uuid}` Bei mehreren
   placements ein canonical message: Der aktuelle Pfad ist nicht verfügbar placement UUID,
   Daher muss migration/model eindeutig eine Art und Weise wählen, zu speichern oder
   Wiederherstellen von public`message_uuid`und Access-Kontext.UUIDund
   arbitrary/primary placement Verboten; accepted message-global reaction
   semantics - und wird nicht überarbeitet.
8. Die physikalische Darstellung des polymorphen public
   `ExternalOperation.target_uuid`: target schema Sie müssen die Kanonische wählen.
   Ziel-Reister oder FK-Spalten für den Stream/topic/message, nicht
   Wechseln der aktuellen JSON `target_uuid`; ein nicht überprüfbares Polymorph FK
   Verboten.

Target ingestion, service identity, registration boundary und Recovery für
Zulip beschrieben ohne Wiederholung dieses Inventars in
[`zulip_bridge/README.md`](zulip_bridge/README.md). Seine OPEN-Liste spezifiziert
transport/provider-key Fragen zu Bridge; current mTLS authentication bereits ausgewählt,
und die gemeinsamen Messenger model/task/event
Die Namen sind hier noch kanonisch.Zulip event/opRichtungen und
echo-prevention boundary Kanonisch in
[`zulip_bridge/event_coverage.md`](zulip_bridge/event_coverage.md).

[← Hauptindex der Dokumentation](index.md) · [Domänenmodell](messenger_domain_model.md) · [RestAlchemy API](messenger_restalchemy_api_spec.md) · [Sequence index](diagrams/sequence/README.md) · [Zulip Bridge proposal](zulip_bridge/README.md)
