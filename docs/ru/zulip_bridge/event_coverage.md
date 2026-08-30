# Матрица покрытия событий и направлений синхронизации

Статус: **proposal; итог согласованного event-coverage опроса**.

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

Матрица фиксирует поддерживаемые направления целевой архитектуры. Она не
добавляет публичные Workspace endpoints и не меняет
[`workspace_api.md`](../workspace_api.md). Точные Zulip event literals сверены
с актуальным каталогом [`GET /events`](https://zulip.com/api/get-events).
Wire route/transport и оставшиеся implementation boundaries перечислены только в
[едином OPEN-list](README.md#единый-список-open-решений-zulip-bridge).

## Значения направления

- **bidirectional** — изменение может начаться в Workspace или Zulip; обе
  стороны сходятся к одному поддерживаемому состоянию;
- **Zulip→Workspace** — принимается только изменение, возникшее в Zulip;
  соответствующая Workspace mutation не отправляется обратно;
- **unsupported** — Bridge не подписывается/не создаёт projection и не
  интерпретирует событие как близкий поддерживаемый тип;
- **OPEN** — направление или semantic mapping ещё не принято; мутация
  fail-closed и не применяется автоматически.

`Workspace action/projection` ниже означает логическую целевую команду или
проекцию. Если такого действия нет в действующем публичном API, документ не
выдумывает route: выбранная bidirectional semantics сохраняется, а private
initiation surface остаётся implementation OPEN.

## Универсальная защита от echo loop

Каждая bidirectional mutation несёт или выводит:

- `origin` (`workspace` или `zulip`);
- immutable `causation_uuid`/Workspace provider operation UUID;
- stable `provider_object_key`;
- stable `provider_event_key` либо source event UUID/queue position;
- provider/Workspace version, если ресурс поддерживает версионирование.

Workspace outbound operation и ожидаемый provider result сохраняются до вызова
Zulip. Возвращённое Zulip event разрешается по тому же object/event/causation
контексту и подтверждает operation, но не создаёт новую обратную operation.
Если Zulip не возвращает произвольный client operation UUID, Bridge связывает
echo с durable operation receipt, provider object key и подтверждённой
version/state; одно время получения не является ключом идемпотентности.

Для ephemeral `presence`, `typing` и `typing_edit_message` используется
короткоживущий origin/causation cache с TTL: собственное отражение не
ретранслируется, истёкшее состояние удаляется, heartbeat продлевает только
актуальную presence. Точные численные TTL/heartbeat входят в capacity OPEN, но
наличие TTL и loop prevention — обязательный инвариант.

## Message/content family

| Family | Exact Zulip event/op или operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| message create | `type="message"`; outbound Zulip send message operation | **bidirectional** | `message.create`: canonical `MESSAGE` + mandatory `TOPIC`/`MESSAGE_PLACEMENT` + author binding/state + outbox | Система, принявшая исходную create mutation; после commit — canonical Workspace row и provider mapping | Provider message identity + create causation; Zulip echo подтверждает outbound operation |
| content edit | `type="update_message"` with `content`, `rendered_content`, `rendering_only` | **bidirectional** | Update canonical payload/source version; `content_mentions`/ready event async | Авторский Markdown исходной mutation; `rendered_content` — provider-derived projection, не writable source | Same message object key + provider version/causation; rendering-only echo не создаёт повторный edit |
| message move / topic rename | `type="update_message"` with `stream_id`, `stream_name`, `subject`, `orig_subject`, `propagate_mode` | **bidirectional** | Whole-topic rename сохраняет mapped topic UUID; partial move удаляет old placement и создаёт target placement, content не копируется | Принятая move mutation и authoritative Zulip result | Causation + provider message/version; target placement получает новый UUIDv5, old URL возвращает `404`, events отражают old delete + new create/update |
| message delete | `type="delete_message"`; outbound delete message operation | **bidirectional** | `message.delete`/provider tombstone + outbox; affected placements/access/counters async | Принятая delete mutation | Same provider message key + delete causation/version; retry is no-op |
| reactions | `type="reaction", op="add"` / `op="remove"`; outbound add/remove reaction | **bidirectional** | Upsert/delete one canonical-message-global raw reaction fact; message-scope snapshot async | Raw reaction facts keyed by canonical message/user/emoji | Provider message+actor+`emoji_name`/`emoji_code`/`reaction_type` + causation; echo confirms fact |
| files/attachments | `type="attachment", op="add"` / `op="update"` / `op="remove"`; upload/delete provider file operations | **bidirectional** | Bounded allocate/upload/finalize; normalized attachment link; file/message projections async | Provider bytes/metadata for Zulip-origin file; Workspace bytes/metadata for Workspace-origin file | One canonical file per `(realm_uuid,attachment_id)`; repeated references reuse it, physical delete requires zero references |
| personal flags | `type="update_message_flags", op="add"` / `op="remove"`, `flag="read"` or `flag="starred"` | **bidirectional** | Update placement-scoped `USER_MESSAGE_STATE.read_at`/`starred`; ready counters/events async | Per-user state for mapped provider-owned placement | User+provider message+flag+op+causation; own echo does not emit reciprocal flag mutation |
| unread transition | `type="update_message_flags", op="remove", flag="read"` | **bidirectional** | Clear placement-scoped read marker through private target action; no public route is invented | Per-user state | Same flag key/causation; current public API has no mark-unread action, initiation surface OPEN |
| mentions and link/render results | `type="message"` fields `flags`, `content`, `rendered_content`, `topic_links`; corresponding `update_message` fields | **bidirectional** at message mutation level | Recompute/materialize mentions/links from accepted content; preserve sanitized provider projection | Raw authored content; each destination owns its derived render, but provider result may be projected | Content version/causation; derived-only change never sends original mutation back |
| experimental submessages | `type="submessage"`; `message.submessages[]` with `msg_type`/`content` | **unsupported** | None | Zulip only | Explicitly not subscribed/projected; no fallback to message body |

Message flags apply to the provider-owned placement mapped to the Zulip
message. Они не распространяются произвольно на manual placements той же
canonical `MESSAGE`. Reactions, напротив, остаются намеренно
canonical-message-global согласно принятой Messenger semantics.

## Channels, topics, subscriptions и conversation mapping

| Family | Exact Zulip event/op или operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| Zulip channel create | `type="stream", op="create"` | **Zulip→Workspace** | Create/map canonical Workspace `STREAM` in server-owned project scope | Zulip channel | Provider `stream_id` + source event key; retry returns same stream mapping |
| Workspace stream create | Workspace `POST .../streams/`; универсального Zulip event нет | **unsupported** для Workspace→Zulip create | Остаётся native Workspace stream; provider channel operation не создаётся | Workspace | Явная асимметрия; отсутствие provider operation предотвращает случайный echo |
| channel metadata/archive/delete | `type="stream", op="update"` / `op="delete"`; corresponding Zulip channel mutation | **bidirectional** | Передать mapped channel command; Workspace domain service решает archive/history/bindings/visibility и пишет outbox | Последняя принятая source mutation в mapped channel | `stream_id` + property/version + causation; Bridge не дублирует Workspace policy |
| own subscription | `type="subscription", op="add"` / `op="remove"` | **bidirectional** | Передать membership change; Workspace по stream settings изменяет binding generation/history visibility | Provider membership plus Workspace security fence | Account+stream+user+generation+causation; Bridge не создаёт message bindings самостоятельно |
| peer membership | `type="subscription", op="peer_add"` / `op="peer_remove"` | **bidirectional** | Передать membership changes for visible peers; Workspace resolves user, historical access и bindings | Provider subscriber set | Arrays expand to stable per-pair commands; group composition change не создаёт новый stream |
| personal subscription properties | `type="subscription", op="update"` with allowlisted `property`/`value` | **bidirectional** | Update mapped notification/mute/pin state when current Workspace contract has an equivalent | User-owned subscription state | User+stream+property+value+causation; unknown property is not silently stored |
| personal topic state | `type="user_topic"` with `stream_id`, `topic_name`, `visibility_policy` | **bidirectional** | Update mapped `USER_TOPIC_BINDING` notification/visibility state | Per-user topic state | User+topic mapping+policy+causation; current `user_topic` replaces legacy `muted_topics` |
| topic materialization | Нет универсального `topic created`; topic appears in `message` | **bidirectional** через message flow | Create mandatory canonical `TOPIC` on first mapped message; Workspace-origin topic materializes in Zulip with its first mapped message, not a standalone provider create | Conversation/message context | Topic mapping + first message key; no synthetic provider `topic created` event |
| topic rename/move | `type="update_message"` topic/stream fields | **bidirectional** | Update mapping/placements for affected message set according to `propagate_mode` | Accepted provider operation result | Message/version/causation; each target topic has stable mapping and UUIDv5 placements |
| direct/self message | `type="message"`, `message.type="private"`, provider recipient data identifying direct or self conversation | **bidirectional** | Map to private direct/self Workspace `STREAM` + mandatory technical/canonical `TOPIC` | Provider conversation/participant identity | Stable provider conversation key + message key; exact key serialization belongs to canonical OPEN #2, no channel `stream` event is expected |
| group direct message | `type="message"`, `message.type="private"`, provider recipient data identifying group direct | **bidirectional** | Map to private group-direct Workspace `STREAM` + mandatory topic | Provider conversation/participant identity | Stable provider conversation key + message key; exact participant-key serialization belongs to canonical OPEN #2 |
| channel message | `type="message"`, `message.type="stream"`, `stream_id` + topic | **bidirectional** | Map to channel Workspace `STREAM` and mandatory topic placement | Zulip channel/topic mapping or Workspace mapped stream | Stream/topic/message provider keys + causation |
| legacy muted topics | `type="muted_topics"` | **unsupported** in target profile | None; target requests/uses `user_topic` | Zulip legacy client state | Не интерпретируется параллельно с `user_topic` |

Zulip `realm_user/update` field `person.role` is the realm-wide user role. Это
не универсальная channel-specific membership role. Direction for the selected
realm role is accepted as bidirectional, but its exact Workspace role/binding
mapping remains a narrow OPEN; arbitrary `WorkspaceStreamBinding.role` must not
be projected to Zulip without that mapping.

## Users и bots

| Family | Exact Zulip event/op или operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| ordinary user add | `type="realm_user", op="add"` for non-bot | **unsupported** for automatic creation | Validate existing identity mapping only; no hidden `WorkspaceUser` create | Provisioning/IAM or separately approved identity mapping | Missing mapping is fail-closed/deferred and visible to reconciliation |
| user name/avatar | `type="realm_user", op="update"`, `person.full_name`, `avatar_url`, `avatar_source`, `avatar_version` | **bidirectional** for an existing mapping | Update mapped Workspace user name/avatar projection; outbound user mutation uses provider operation | Last accepted supported field mutation | User key+field+version/causation; own avatar/name echo confirms operation |
| user email | `type="realm_user", op="update"`, email-related field | **unsupported** | Workspace email projection не изменяется из Zulip и не отправляется в Zulip | Каждая система владеет своим email | Поле явно игнорируется; оно не участвует в identity key |
| realm role | `type="realm_user", op="update"`, `person.role` | **bidirectional** with mapping OPEN | Update selected mapped Workspace role projection; exact target role cell remains OPEN | Accepted role mutation after authorization | User+role+causation; no blanket per-stream role rewrite |
| custom profile value | `type="realm_user", op="update"`, `person.custom_profile_field` | **bidirectional** for an existing mapping | Update mapped value only; schema creation/change is unsupported | Value on mapped user; schema remains local/unsupported | User+field ID+value+causation; unknown field schema fail-closed |
| deactivate/reactivate user | `type="realm_user", op="update"`, `person.is_active=false/true` | **bidirectional** for an existing mapping | Deactivate/reactivate mapped user and revoke/rebuild access through normal generations/tasks | Accepted lifecycle mutation | User+lifecycle version+causation; reactivation does not resurrect stale bindings silently |
| visibility-only/legacy removal | `type="realm_user", op="remove"` | **unsupported** as user delete | Refresh/revoke visibility evidence only; do not infer account deletion/deactivation | Zulip visibility policy | No hidden delete; requires explicit `is_active` lifecycle event for mutation |
| bot add | `type="realm_bot", op="add"` plus associated bot `realm_user` data | **Zulip→Workspace** | Create one special Workspace bot/external user and provider mapping | Zulip bot identity | Provider bot `user_id` key dedupes paired `realm_bot`/`realm_user` events |
| bot metadata update | `type="realm_bot", op="update"` | **unsupported** | None; bot metadata projection remains unchanged | Zulip only | Event acknowledged/audited without Workspace mutation |
| bot deactivate/delete | `type="realm_bot", op="delete"` and mapped bot `realm_user/update person.is_active=false` | **Zulip→Workspace** | Deactivate/delete special Workspace bot according to current local lifecycle; revoke access | Zulip bot lifecycle | Bot user key + delete/deactivate event key; paired events converge idempotently |
| legacy bot remove | `type="realm_bot", op="remove"` | **unsupported** in target current profile | None; deprecated event is not a second delete source | Zulip legacy | No duplicate lifecycle path |

Любая поддерживаемая ordinary user update требует provider identity mapping.
Сам `realm_user/add` не является автоматическим provisioning managed account.
History import, встретив автора/member без Workspace account, создаёт/reuses
unmanaged external user без credentials/session; later verified connect claims
эту же stable identity. Исключение event-driven create принято для
`realm_bot/add` special user.

## Presence, persistent status и typing

| Family | Exact Zulip event/op или operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| presence | `type="presence"`; modern `presences.{user_id}.active_timestamp` / `idle_timestamp`, legacy `presence.website.status="active"` or `"idle"` | **bidirectional** | Последовательно relay `active`/`idle`; derive `offline` after TTL; heartbeat refreshes `last_ping_at` | Последнее confirmed неистёкшее изменение любой стороны | Origin/causation suppresses echo only, не выбирает winner; TTL clears stale presence |
| persistent user status | `type="user_status"` with `user_id`, `status_text`, `emoji_name`, `emoji_code`, `reaction_type` | **bidirectional** | Последовательно persist mapped `status_text`/`status_emoji` and emit ordinary user update | Последнее confirmed изменение любой стороны | Origin/causation suppresses echo only; unlike presence, status survives TTL/restart |
| typing | `type="typing", op="start"` / `op="stop"` | **bidirectional** | Relay scoped typing signal to mapped Workspace recipients; no canonical message mutation | Latest non-expired signal | Origin/causation key + short TTL; stop and expiry both clear state |
| editing typing | `type="typing_edit_message", op="start"` / `op="stop"` | **bidirectional** | Relay edit-typing signal for mapped placement/message recipients | Latest non-expired signal | Sender+message+op+causation+TTL; access rechecked before relay |

Presence history не импортируется. После restart Connector получает current
presence snapshot/heartbeat и затем поддерживает TTL; `user_status` является
persistent и входит в history/reconciliation.

## Personal data и UI state

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| drafts | `type="drafts"`, operations `add`, `update`, `remove` | **unsupported** | None; Workspace drafts и Zulip drafts независимы | Локально в каждой системе | Bridge не подписывается/не отражает |
| muted users | `type="muted_users"` | **unsupported** | None | Zulip only | No projection |
| reminders | `type="reminders"`, operations `add`, `remove` | **unsupported** | None | Zulip only | No projection |
| scheduled messages | `type="scheduled_messages"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip only | No projection |
| user client settings | `type="user_settings", op="update"`; `type="realm_user_settings_defaults", op="update"` | **unsupported** | None | Каждая система владеет client settings | No projection |
| navigation views | `type="navigation_view"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip UI | No projection |
| channel folders/UI grouping | `type="channel_folder"`, operations `add`, `reorder`, `update` | **unsupported** | None; не смешивать с canonical Workspace folders | Zulip UI | No projection |
| alert words | `type="alert_words"` | **unsupported** | None | Zulip UI | No projection |
| saved snippets | `type="saved_snippets"`, operations `add`, `update`, `remove` | **OPEN** | Не применять до отдельного решения | Не выбрано | Fail-closed; event durable quarantined/audited, не становится draft/message |

## User groups и organization configuration

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
| realm export/deactivation | `type="realm_export"`, `type="realm_export_consent"`, `type="realm", op="deactivated"` | **unsupported** | None; операторский Bridge lifecycle не выводится из этих events | Zulip only | No implicit cleanup/destructive action |

## Devices, integrations, invites и service events

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| devices | `type="device"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip client state | No projection |
| external integration state | `type="has_webex_token"`, `type="has_zoom_token"` and equivalent provider UI state | **unsupported** | None | Zulip only | No projection |
| invites | `type="invites_changed"` | **unsupported** | None | Zulip only | No projection |
| heartbeat | `type="heartbeat"` | **Zulip→Workspace** | Refresh Connector/source queue liveness only; no Messenger domain mutation | Zulip event queue | Queue/event ID dedupe; never converted to public Workspace event |
| restart | `type="restart"` | **Zulip→Workspace** | Lifecycle signal: завершить connection и повторить единый bootstrap с новой supported queue/boundary | Zulip server generation/feature level | One lifecycle generation handled once; old queue/cursor не нужен durable recovery |
| web reload signal | `type="web_reload_client"` | **Zulip→Workspace** | Повторить тот же bootstrap/re-register, не трактовать как browser page reload | Zulip server | Event ID/generation dedupe; new boundary + provider keys обеспечивают overlap-safe recovery |
| onboarding/UI auxiliary | `type="onboarding_steps"` | **unsupported** | None | Zulip UI | No projection |

## History coverage

History Importer применяет ту же direction matrix, но импортирует только
persistent поддерживаемое состояние:

| Состояние | History behavior |
| --- | --- |
| users | Create/reuse unmanaged external identities for imported authors/members without Workspace account; explicit verified connect claims them; import `realm_bot/add` special identities; `realm_user/add` alone does not provision managed login |
| streams/topics/memberships | Import Zulip channels, mandatory topics inferred from messages, current subscriptions/member state and supported personal topic state |
| messages | Import create/current content/move/delete state in the account range before registration boundary, newest-first per stream/topic; no experimental submessages |
| flags | Import only per-user provider flags observable under the authorized account/mapping; missing users/state are not synthesized |
| files/reactions | Import after message mapping or durable defer, using the same provider identities as realtime |
| user status | Import persistent `user_status` for a mapped managed/unmanaged identity only when authoritative snapshot exposes it; otherwise do not invent historical state |
| presence/typing/heartbeat/restart/web reload | `presence`, `typing`, `typing_edit_message`, `heartbeat`, `restart`, `web_reload_client` не backfill-ятся; Connector establishes fresh current state and TTL after queue registration |
| unsupported/OPEN families | Не импортируются; `saved_snippets` остаётся quarantined/неприменённым до решения |

## Compatibility boundaries без изменения public API

Направление **bidirectional** принято как target behavior, но оно не означает
появление новых browser routes. В текущем публичном Workspace API отсутствуют
как минимум отдельные actions для message move, mark-unread, typing и части
user role/custom-field mutations. Их private initiation surface и authorization
должны быть выбраны до implementation; подставлять существующий endpoint с
другой семантикой запрещено.

Действующий контракт также прямо определяет star state как Workspace-owned и
не синхронизируемый с external provider. Принятое здесь bidirectional
`read`/`starred` target behavior — сознательное изменение integration semantics:
JSON keys и существующие `star`/`unstar` actions не меняются, но rollout обязан
описать новую provider-visible side effect. Для mark-unread по-прежнему нужен
private initiation surface, поскольку текущего публичного action нет.

Move между topics/streams создаёт новый `MESSAGE_PLACEMENT.uuid`, потому что
public identity вычисляется как
`UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Канонический `MESSAGE` не
копируется. Old placement удаляется, его URL возвращает `404` без redirect;
clients получают current-contract delete old placement и create/update target
placement. Idempotent duplicate не создаёт повторных ready events.

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
