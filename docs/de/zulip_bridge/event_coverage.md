# Matrix der Ereignis- und Synchronisierungs-Abdeckung

Status: **proposal; Ergebnis einer vereinbarten Event-Coverage-Umfrage**.

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

Die Matrix fixiert die unterstützten Richtungen der Ziel-Architektur.
fügt öffentliche Workspace Endpunkte hinzu und ändert nichts
[`workspace_api.md`](../workspace_api.md). Genaue Zulip Event Literals wurden abgeglichen
mit aktuellem Katalog [`GET /events`](https://zulip.com/api/get-events).
Wire route/transport Die Rest-Implantations-Border-Liste ist nur in
[Einheitlich OPEN-list](README.md#единый-список-open-решений-zulip-bridge).

## Die Bedeutung der Richtung

- **bidirectional** — Die Änderung kann in Workspace oder Zulip beginnen; beide
  Die Parteien kommen zu einem unterstützten Zustand zusammen;
- **Zulip→Workspace** — nur eine Änderung, die in Zulip;
  Die entsprechende Workspace Mutation wird nicht zurückgeschickt;
- **unsupported** — Bridge nicht unterschrieben/nicht projiziert und nicht
  Interpretiert das Ereignis als nahegestelltes unterstütztes Typ;
- **OPEN** — Richtung oder semantic mapping noch nicht angenommen; Mutation
  fail-closed und nicht automatisch angewendet.

`Workspace action/projection` unten bedeutet logischer Zielbefehl oder
Wenn diese Aktion nicht in der aktuellen öffentlichen API vorhanden ist, ist das Dokument nicht
erfindet route: die ausgewählte bidirectional semantics wird erhalten, und private
initiation surface Bleibt. implementation OPEN.

## Allgemeiner Schutz vor echo loop

Jede bidirektionale Mutation trägt oder führt:

- `origin` (`workspace` oder `zulip`);
- immutable `causation_uuid`/Workspace provider operation UUID;
- stable `provider_object_key`;
- stable `provider_event_key` oder source event UUID/queue position;
- provider/Workspace version, wenn die Ressource Versionen unterstützt.

Workspace outbound operation und erwartet provider result werden bis zum Anruf gespeichert
Zulip. Die zurückgegebene Zulip-Event wird auf dieselbe Weise gelöscht object/event/causation
Kontext und bestätigt die Operation, aber erstellt keine neue Umkehrung operation.
Wenn Zulip keine beliebige Clientoperation UUID zurückgibt, verbindet Bridge
echo mit durable operation receipt, provider object key und bestätigter
version/state; Eine Zeit der Erfassung ist nicht der Schlüssel zur Idempotenz.

Für ephemeral `presence`, `typing` und `typing_edit_message` wird verwendet
origin/causation cache mit TTL: eigene Reflexion nicht
Die Ausfallphase wird beseitigt, der Herzschlag wird nur verlängert.
Die exakten Zahlen TTL/heartbeat gehören zur Kapazität OPEN, aber
Vorhandensein von TTL und loop prevention  obligatorischer Invariant.

## Message/content family

| Family | Exact Zulip event/op oder operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| message create | `type="message"`; outbound Zulip send message operation | **bidirectional** | `message.create`: canonical `MESSAGE` + mandatory `TOPIC`/`MESSAGE_PLACEMENT` + author binding/state + outbox | System, das die ursprüngliche create mutation angenommen hat; nach commit  canonical Workspace row und provider mapping | Provider message identity + create causation; Zulip echo bestätigt outbound operation |
| content edit | `type="update_message"` with `content`, `rendered_content`, `rendering_only` | **bidirectional** | Update canonical payload/source version; `content_mentions`/ready event async | Autoren Markdown ursprüngliche Mutation; `rendered_content`  provider-derived projection, nicht writable source | Same message object key + provider version/causation; rendering-only echo Erstellt kein Wiederholungsdatum edit |
| message move / topic rename | `type="update_message"` with `stream_id`, `stream_name`, `subject`, `orig_subject`, `propagate_mode` | **bidirectional** | Whole-topic rename speichert mapped topic UUID; partial move löscht old placement und erstellt target placement, content wird nicht kopiert | Akzeptiert , die Bewegung zu verändern und authoritative Zulip result | Causation + provider message/version; target placement Erhält neue UUIDv5, old URL gibt `404` zurück, events reflektieren old delete + new create/update |
| message delete | `type="delete_message"`; outbound delete message operation | **bidirectional** | `message.delete`/provider tombstone + outbox; affected placements/access/counters async | - Das ist nicht wahr . delete mutation | Same provider message key + delete causation/version; retry is no-op |
| reactions | `type="reaction", op="add"` / `op="remove"`; outbound add/remove reaction | **bidirectional** | Upsert/delete one canonical-message-global raw reaction fact; message-scope snapshot async | Raw reaction facts keyed by canonical message/user/emoji | Provider message+actor+`emoji_name`/`emoji_code`/`reaction_type` + causation; echo confirms fact |
| files/attachments | `type="attachment", op="add"` / `op="update"` / `op="remove"`; upload/delete provider file operations | **bidirectional** | Bounded allocate/upload/finalize; normalized attachment link; file/message projections async | Provider bytes/metadata for Zulip-origin file; Workspace bytes/metadata for Workspace-origin file | One canonical file per `(realm_uuid,attachment_id)`; repeated references reuse it, physical delete requires zero references |
| personal flags | `type="update_message_flags", op="add"` / `op="remove"`, `flag="read"` or `flag="starred"` | **bidirectional** | Update placement-scoped `USER_MESSAGE_STATE.read_at`/`starred`; ready counters/events async | Per-user state for mapped provider-owned placement | User+provider message+flag+op+causation; own echo does not emit reciprocal flag mutation |
| unread transition | `type="update_message_flags", op="remove", flag="read"` | **bidirectional** | Clear placement-scoped read marker through private target action; no public route is invented | Per-user state | Same flag key/causation; current public API has no mark-unread action, initiation surface OPEN |
| mentions and link/render results | `type="message"` fields `flags`, `content`, `rendered_content`, `topic_links`; corresponding `update_message` fields | **bidirectional** at message mutation level | Recompute/materialize mentions/links from accepted content; preserve sanitized provider projection | Raw authored content; each destination owns its derived render, but provider result may be projected | Content version/causation; derived-only change never sends original mutation back |
| experimental submessages | `type="submessage"`; `message.submessages[]` with `msg_type`/`content` | **unsupported** | None | Zulip only | Explicitly not subscribed/projected; no fallback to message body |

Message flags apply to the provider-owned placement mapped to the Zulip
message. Sie werden nicht willkürlich auf manuelle Platzierungen der gleichen
canonical `MESSAGE`. Reactions, Sie bleiben ganz im Gegenteil.
canonical-message-global Die Kommission hat Messenger semantics.

## Channels, topics, subscriptions und conversation mapping

| Family | Exact Zulip event/op oder operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| Zulip channel create | `type="stream", op="create"` | **Zulip→Workspace** | Create/map canonical Workspace `STREAM` in server-owned project scope | Zulip channel | Provider `stream_id` + source event key; retry returns same stream mapping |
| Workspace stream create | Workspace `POST .../streams/`; Es gibt kein universelles Zulip Event | **unsupported** für Workspace→Zulip create | Bleibt der native Workspace Stream; Provider Channel Operation wird nicht erstellt | Workspace | Offene Asymmetrie; das Fehlen einer Provider Operation verhindert eine zufällige echo |
| channel metadata/archive/delete | `type="stream", op="update"` / `op="delete"`; corresponding Zulip channel mutation | **bidirectional** | Übertragen des mapped channel command; Workspace domain service löst archive/history/bindings/visibility und schreibt outbox | Die letzte angenommenen Source Mutation in mapped channel | `stream_id` + property/version + causation; Bridge nicht doppeln Workspace policy |
| own subscription | `type="subscription", op="add"` / `op="remove"` | **bidirectional** | Übertragen Sie die membership change; Workspace ändert die Stream-Einstellungen binding generation/history visibility | Provider membership plus Workspace security fence | Account+stream+user+generation+causation; Bridge erstellt keine Message Bindings selbst |
| peer membership | `type="subscription", op="peer_add"` / `op="peer_remove"` | **bidirectional** | Übertragen Sie membership changes for visible peers; Workspace resolves user, historical access und bindings | Provider subscriber set | Arrays expand to stable per-pair commands; group composition change Erstellt keine neuen stream |
| personal subscription properties | `type="subscription", op="update"` with allowlisted `property`/`value` | **bidirectional** | Update mapped notification/mute/pin state when current Workspace contract has an equivalent | User-owned subscription state | User+stream+property+value+causation; unknown property is not silently stored |
| personal topic state | `type="user_topic"` with `stream_id`, `topic_name`, `visibility_policy` | **bidirectional** | Update mapped `USER_TOPIC_BINDING` notification/visibility state | Per-user topic state | User+topic mapping+policy+causation; current `user_topic` replaces legacy `muted_topics` |
| topic materialization | Es gibt keine Universal `topic created`; topic appears in `message` | **bidirectional** Über message flow | Create mandatory canonical `TOPIC` on first mapped message; Workspace-origin topic materializes in Zulip with its first mapped message, not a standalone provider create | Conversation/message context | Topic mapping + first message key; no synthetic provider `topic created` event |
| topic rename/move | `type="update_message"` topic/stream fields | **bidirectional** | Update mapping/placements for affected message set according to `propagate_mode` | Accepted provider operation result | Message/version/causation; each target topic has stable mapping and UUIDv5 placements |
| direct/self message | `type="message"`, `message.type="private"`, provider recipient data identifying direct or self conversation | **bidirectional** | Map to private direct/self Workspace `STREAM` + mandatory technical/canonical `TOPIC` | Provider conversation/participant identity | Stable provider conversation key + message key; exact key serialization belongs to canonical OPEN #2, no channel `stream` event is expected |
| group direct message | `type="message"`, `message.type="private"`, provider recipient data identifying group direct | **bidirectional** | Map to private group-direct Workspace `STREAM` + mandatory topic | Provider conversation/participant identity | Stable provider conversation key + message key; exact participant-key serialization belongs to canonical OPEN #2 |
| channel message | `type="message"`, `message.type="stream"`, `stream_id` + topic | **bidirectional** | Map to channel Workspace `STREAM` and mandatory topic placement | Zulip channel/topic mapping or Workspace mapped stream | Stream/topic/message provider keys + causation |
| legacy muted topics | `type="muted_topics"` | **unsupported** in target profile | None; target requests/uses `user_topic` | Zulip legacy client state | Nicht parallel zu interpretiert `user_topic` |

Zulip `realm_user/update` field `person.role` is the realm-wide user role. Das ist ...
nicht universell channel-specific membership role. Direction for the selected
realm role is accepted as bidirectional, but its exact Workspace role/binding
mapping remains a narrow OPEN; arbitrary `WorkspaceStreamBinding.role` must not
be projected to Zulip without that mapping.

## Users und bots

| Family | Exact Zulip event/op oder operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| ordinary user add | `type="realm_user", op="add"` for non-bot | **unsupported** for automatic creation | Validate existing identity mapping only; no hidden `WorkspaceUser` create | Provisioning/IAM or separately approved identity mapping | Missing mapping is fail-closed/deferred and visible to reconciliation |
| user name/avatar | `type="realm_user", op="update"`, `person.full_name`, `avatar_url`, `avatar_source`, `avatar_version` | **bidirectional** for an existing mapping | Update mapped Workspace user name/avatar projection; outbound user mutation uses provider operation | Last accepted supported field mutation | User key+field+version/causation; own avatar/name echo confirms operation |
| user email | `type="realm_user", op="update"`, email-related field | **unsupported** | Workspace email projection nicht von Zulip geändert und nicht in Zulip gesendet Zulip | Jedes System hat seine eigenen email | Dieses Feld wird offensichtlich ignoriert; es ist nicht an identity key |
| realm role | `type="realm_user", op="update"`, `person.role` | **bidirectional** with mapping OPEN | Update selected mapped Workspace role projection; exact target role cell remains OPEN | Accepted role mutation after authorization | User+role+causation; no blanket per-stream role rewrite |
| custom profile value | `type="realm_user", op="update"`, `person.custom_profile_field` | **bidirectional** for an existing mapping | Update mapped value only; schema creation/change is unsupported | Value on mapped user; schema remains local/unsupported | User+field ID+value+causation; unknown field schema fail-closed |
| deactivate/reactivate user | `type="realm_user", op="update"`, `person.is_active=false/true` | **bidirectional** for an existing mapping | Deactivate/reactivate mapped user and revoke/rebuild access through normal generations/tasks | Accepted lifecycle mutation | User+lifecycle version+causation; reactivation does not resurrect stale bindings silently |
| visibility-only/legacy removal | `type="realm_user", op="remove"` | **unsupported** as user delete | Refresh/revoke visibility evidence only; do not infer account deletion/deactivation | Zulip visibility policy | No hidden delete; requires explicit `is_active` lifecycle event for mutation |
| bot add | `type="realm_bot", op="add"` plus associated bot `realm_user` data | **Zulip→Workspace** | Create one special Workspace bot/external user and provider mapping | Zulip bot identity | Provider bot `user_id` key dedupes paired `realm_bot`/`realm_user` events |
| bot metadata update | `type="realm_bot", op="update"` | **unsupported** | None; bot metadata projection remains unchanged | Zulip only | Event acknowledged/audited without Workspace mutation |
| bot deactivate/delete | `type="realm_bot", op="delete"` and mapped bot `realm_user/update person.is_active=false` | **Zulip→Workspace** | Deactivate/delete special Workspace bot according to current local lifecycle; revoke access | Zulip bot lifecycle | Bot user key + delete/deactivate event key; paired events converge idempotently |
| legacy bot remove | `type="realm_bot", op="remove"` | **unsupported** in target current profile | None; deprecated event is not a second delete source | Zulip legacy | No duplicate lifecycle path |

Jedes unterstützte normaler User Update benötigt provider identity mapping.
`realm_user/add` selbst ist nicht automatisch provisioning managed account.
History import, Wenn Sie den Autor/member ohne Workspace Account treffen, erstellt er/reuses
unmanaged external user Ohne credentials/session; later verified connect claims
Die Ausnahme von event-driven create wird für
`realm_bot/add` special user.

## Presence, persistent status und typing

| Family | Exact Zulip event/op oder operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| presence | `type="presence"`; modern `presences.{user_id}.active_timestamp` / `idle_timestamp`, legacy `presence.website.status="active"` or `"idle"` | **bidirectional** | Nach und nach relay `active`/`idle`; derive `offline` after TTL; heartbeat refreshes `last_ping_at` | Letzte bestätigte fehlerhafte Änderung von jeder Seite | Origin/causation suppresses echo only, wählt nicht aus winner; TTL clears stale presence |
| persistent user status | `type="user_status"` with `user_id`, `status_text`, `emoji_name`, `emoji_code`, `reaction_type` | **bidirectional** | Nach und nach persist mapped `status_text`/`status_emoji` and emit ordinary user update | Letzte bestätigte Änderung von jeder Seite | Origin/causation suppresses echo only; unlike presence, status survives TTL/restart |
| typing | `type="typing", op="start"` / `op="stop"` | **bidirectional** | Relay scoped typing signal to mapped Workspace recipients; no canonical message mutation | Latest non-expired signal | Origin/causation key + short TTL; stop and expiry both clear state |
| editing typing | `type="typing_edit_message", op="start"` / `op="stop"` | **bidirectional** | Relay edit-typing signal for mapped placement/message recipients | Latest non-expired signal | Sender+message+op+causation+TTL; access rechecked before relay |

Presence history Wenn Sie den Connector neu starten, wird er current
presence snapshot/heartbeat und dann unterstützt TTL; `user_status` ist
persistent und geht in history/reconciliation.

## Personal data und UI state

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| drafts | `type="drafts"`, operations `add`, `update`, `remove` | **unsupported** | None; Workspace drafts und Zulip drafts unabhängig | Lokal in jedem System | Bridge nicht unterschrieben/nicht abgespielt |
| muted users | `type="muted_users"` | **unsupported** | None | Zulip only | No projection |
| reminders | `type="reminders"`, operations `add`, `remove` | **unsupported** | None | Zulip only | No projection |
| scheduled messages | `type="scheduled_messages"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip only | No projection |
| user client settings | `type="user_settings", op="update"`; `type="realm_user_settings_defaults", op="update"` | **unsupported** | None | Jedes System besitzt client settings | No projection |
| navigation views | `type="navigation_view"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip UI | No projection |
| channel folders/UI grouping | `type="channel_folder"`, operations `add`, `reorder`, `update` | **unsupported** | None; Nicht mit canonical Workspace folders | Zulip UI | No projection |
| alert words | `type="alert_words"` | **unsupported** | None | Zulip UI | No projection |
| saved snippets | `type="saved_snippets"`, operations `add`, `update`, `remove` | **OPEN** | Bis zur Einzelfallentscheidung nicht anwenden | Nicht ausgewählt | Fail-closed; event durable quarantined/audited, wird nicht draft/message |

## User groups und organization configuration

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| user groups/nested groups | `type="user_group"`, operations `add`, `update`, `remove`, `add_members`, `remove_members`, `add_subgroups`, `remove_subgroups` | **unsupported** | None | Zulip only | No partial flattening into Workspace roles/bindings |
| organization settings | `type="realm"`, operations `update`, `update_dict` | **unsupported** | None | Zulip only | No projection |
| custom emoji | `type="realm_emoji"`, operations `add`, `update`, `update_one` | **unsupported** | None; reaction payload may still carry exact emoji identity | Zulip only | No emoji catalog sync |
| linkifiers | `type="realm_filters"`, `type="realm_linkifiers"` | **unsupported** | None | Zulip only | Rendered message result may be projected, rule catalog is not |
| domains | `type="realm_domains"`, operations `add`, `change`, `remove` | **unsupported** | None | Zulip only | No projection |
| default streams/groups | `type="default_streams"`, `type="default_stream_groups"` | **unsupported** | None | Zulip only | No automatic Workspace membership policy |
| playgrounds | `type="realm_playgrounds"` | **unsupported** | None | Zulip only | No projection |
| profile schema | `type="custom_profile_fields"` | **unsupported** | None; existing mapped field values may sync only when schema mapping exists | Zulip only | Unknown schema makes user value fail-closed |
| realm export/deactivation | `type="realm_export"`, `type="realm_export_consent"`, `type="realm", op="deactivated"` | **unsupported** | None; Der Bridge-Lifecycle wird nicht von diesen events | Zulip only | No implicit cleanup/destructive action |

## Devices, integrations, invites und service events

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| devices | `type="device"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip client state | No projection |
| external integration state | `type="has_webex_token"`, `type="has_zoom_token"` and equivalent provider UI state | **unsupported** | None | Zulip only | No projection |
| invites | `type="invites_changed"` | **unsupported** | None | Zulip only | No projection |
| heartbeat | `type="heartbeat"` | **Zulip→Workspace** | Refresh Connector/source queue liveness only; no Messenger domain mutation | Zulip event queue | Queue/event ID dedupe; never converted to public Workspace event |
| restart | `type="restart"` | **Zulip→Workspace** | Lifecycle signal: Verknüpfung beenden und den Bootstrap mit dem neuen Bootstrap wiederholen supported queue/boundary | Zulip server generation/feature level | One lifecycle generation handled once; old queue/cursor Ich brauche sie nicht. durable recovery |
| web reload signal | `type="web_reload_client"` | **Zulip→Workspace** | Wiederholen Sie den gleichen Bootstrap/re-register, nicht als browser page reload | Zulip server | Event ID/generation dedupe; new boundary + provider keys Sie stellen overlap-safe recovery |
| onboarding/UI auxiliary | `type="onboarding_steps"` | **unsupported** | None | Zulip UI | No projection |

## History coverage

History Importer verwendet die gleiche Richtung Matrix, aber importiert nur
persistent Unterstützter Zustand:

| Zustand | History behavior |
| --- | --- |
| users | Create/reuse unmanaged external identities for imported authors/members without Workspace account; explicit verified connect claims them; import `realm_bot/add` special identities; `realm_user/add` alone does not provision managed login |
| streams/topics/memberships | Import Zulip channels, mandatory topics inferred from messages, current subscriptions/member state and supported personal topic state |
| messages | Import create/current content/move/delete state in the account range before registration boundary, newest-first per stream/topic; no experimental submessages |
| flags | Import only per-user provider flags observable under the authorized account/mapping; missing users/state are not synthesized |
| files/reactions | Import after message mapping or durable defer, using the same provider identities as realtime |
| user status | Import persistent `user_status` for a mapped managed/unmanaged identity only when authoritative snapshot exposes it; otherwise do not invent historical state |
| presence/typing/heartbeat/restart/web reload | `presence`, `typing`, `typing_edit_message`, `heartbeat`, `restart`, `web_reload_client` Nicht zurückgefüllt; Connector establishes fresh current state and TTL after queue registration |
| unsupported/OPEN families | Nicht importiert; `saved_snippets` bleibt quarantined/unangewendet bis zur Entscheidung |

## Compatibility boundaries ohne Änderung public API

Die Richtung **bidirectional** wird als Zielverhalten angenommen, aber sie bedeutet nicht
Es gibt neue Browser-Route.Workspace APInicht vorhanden
Mindestens einzelne Aktionen für message move, mark-unread, typing und Teile
user role/custom-field mutations. Ihre private Initiation surface und authorization
Die Endpunkte müssen vor der Implementierung ausgewählt werden .
- Mit anderen Begriffen verboten..

Der aktuelle Vertrag definiert auch direkt den Star State als Workspace-owned und
Sie sind nicht mit einem externen Provider synchronisiert. bidirectional
`read`/`starred` target behavior — Bewusste Veränderung integration semantics:
JSON keys und die vorhandenen `star`/`unstar`-Aktionen werden nicht geändert, aber die Rollout muss
Mark-unread ist noch immer erforderlich
private initiation surface, Da es keine aktuelle öffentliche Aktion gibt.

Move zwischen topics/streams erstellt ein neues `MESSAGE_PLACEMENT.uuid`, weil
public identity wird berechnet als
`UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Kanonischer `MESSAGE` nicht
Old placement wird gelöscht, seine URL gibt `404` zurück ohne redirect;
clients erhalten current-contract delete old placement und create/update target
placement. Idempotent duplicate Erstellt keine Wiederholungen ready events.

[← Hauptindex der Dokumentation](../index.md) · [Index Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
