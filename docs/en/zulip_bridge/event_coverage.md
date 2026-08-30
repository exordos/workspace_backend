# Matrix of events coverage and synchronization directions

Status: **proposal; result of an agreed event-coverage survey**.

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

The matrix fixes the supported directions of the target architecture.
adds public Workspace endpoints and does not change
[`workspace_api.md`](../workspace_api.md). Exact Zulip event literals are compared
With a current catalog [`GET /events`](https://zulip.com/api/get-events).
Wire route/transport and the rest of the implementation boundaries are listed only in
[- One OPEN-list](README.md#единый-список-open-решений-zulip-bridge).

## The meaning of the direction

- **bidirectional** — The change may begin at Workspace or Zulip; both
  The parties agree on one supported state;
- **Zulip→Workspace** — only the change in Zulip;
  the corresponding Workspace mutation is not sent back;
- **unsupported** — Bridge does not sign/create projection and does not
  Interpret the event as a nearby supported type;
- **OPEN** — direction or semantic mapping is not yet accepted; mutation
  fail-closed and not applied automatically.

`Workspace action/projection` below means a logical target command or
If this action is not present in the current public API, the document is not
invents route: selected bidirectional semantics is retained, and private
initiation surface It stays. implementation OPEN.

## Universal protection from echo loop

Every bidirectional mutation either brings or brings:

- `origin` (`workspace` or `zulip`);
- immutable `causation_uuid`/Workspace provider operation UUID;
- stable `provider_object_key`;
- stable `provider_event_key` Or source event UUID/queue position;
- provider/Workspace version, if the resource supports versioning.

Workspace outbound operation and the expected provider result are saved until called
Zulip. The Zulip event returned is allowed to do the same. object/event/causation
Context and confirms the operation, but does not create a new inverse operation.
If Zulip does not return the arbitrary client operation UUID, the Bridge connects
echo with a durable operation receipt, provider object key and a confirmed
version/state; One time of receipt is not the key to idempotence.

For ephemeral `presence`, `typing` and `typing_edit_message` is used
short-lived origin/causation cache with TTL: own reflection not
It's re-transmitted, the stale state is removed, the heartbeat is only prolonged.
The exact numerals TTL/heartbeat are in the capacity OPEN, but
presence of TTL and loop prevention  mandatory invariant.

## Message/content family

| Family | Exact Zulip event/op or operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| message create | `type="message"`; outbound Zulip send message operation | **bidirectional** | `message.create`: canonical `MESSAGE` + mandatory `TOPIC`/`MESSAGE_PLACEMENT` + author binding/state + outbox | The system that adopted the original create mutation; after commit  canonical Workspace row and provider mapping | Provider message identity + create causation; Zulip echo It confirms outbound operation |
| content edit | `type="update_message"` with `content`, `rendered_content`, `rendering_only` | **bidirectional** | Update canonical payload/source version; `content_mentions`/ready event async | Author Markdown original mutation; `rendered_content`  provider-derived projection, not writable source | Same message object key + provider version/causation; rendering-only echo It doesn 't create a repeat . edit |
| message move / topic rename | `type="update_message"` with `stream_id`, `stream_name`, `subject`, `orig_subject`, `propagate_mode` | **bidirectional** | Whole-topic rename saves mapped topic UUID; partial move removes old placement and creates target placement, content is not copied | Accepted move mutation and authoritative Zulip result | Causation + provider message/version; target placement Returns new UUIDv5, old URL returns `404`, events are reflected old delete + new create/update |
| message delete | `type="delete_message"`; outbound delete message operation | **bidirectional** | `message.delete`/provider tombstone + outbox; affected placements/access/counters async | Adopted delete mutation | Same provider message key + delete causation/version; retry is no-op |
| reactions | `type="reaction", op="add"` / `op="remove"`; outbound add/remove reaction | **bidirectional** | Upsert/delete one canonical-message-global raw reaction fact; message-scope snapshot async | Raw reaction facts keyed by canonical message/user/emoji | Provider message+actor+`emoji_name`/`emoji_code`/`reaction_type` + causation; echo confirms fact |
| files/attachments | `type="attachment", op="add"` / `op="update"` / `op="remove"`; upload/delete provider file operations | **bidirectional** | Bounded allocate/upload/finalize; normalized attachment link; file/message projections async | Provider bytes/metadata for Zulip-origin file; Workspace bytes/metadata for Workspace-origin file | One canonical file per `(realm_uuid,attachment_id)`; repeated references reuse it, physical delete requires zero references |
| personal flags | `type="update_message_flags", op="add"` / `op="remove"`, `flag="read"` or `flag="starred"` | **bidirectional** | Update placement-scoped `USER_MESSAGE_STATE.read_at`/`starred`; ready counters/events async | Per-user state for mapped provider-owned placement | User+provider message+flag+op+causation; own echo does not emit reciprocal flag mutation |
| unread transition | `type="update_message_flags", op="remove", flag="read"` | **bidirectional** | Clear placement-scoped read marker through private target action; no public route is invented | Per-user state | Same flag key/causation; current public API has no mark-unread action, initiation surface OPEN |
| mentions and link/render results | `type="message"` fields `flags`, `content`, `rendered_content`, `topic_links`; corresponding `update_message` fields | **bidirectional** at message mutation level | Recompute/materialize mentions/links from accepted content; preserve sanitized provider projection | Raw authored content; each destination owns its derived render, but provider result may be projected | Content version/causation; derived-only change never sends original mutation back |
| experimental submessages | `type="submessage"`; `message.submessages[]` with `msg_type`/`content` | **unsupported** | None | Zulip only | Explicitly not subscribed/projected; no fallback to message body |

Message flags apply to the provider-owned placement mapped to the Zulip
message. They are not arbitrarily applied to manual placements of the same
canonical `MESSAGE`. Reactions, On the contrary, they remain deliberately
canonical-message-global The Commission shall adopt Messenger semantics.

## Channels, topics, subscriptions and conversation mapping

| Family | Exact Zulip event/op or operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| Zulip channel create | `type="stream", op="create"` | **Zulip→Workspace** | Create/map canonical Workspace `STREAM` in server-owned project scope | Zulip channel | Provider `stream_id` + source event key; retry returns same stream mapping |
| Workspace stream create | Workspace `POST .../streams/`; There is no universal Zulip event | **unsupported** For the Workspace→Zulip create | Remains native Workspace stream; provider channel operation not created | Workspace | Clear asymmetry; the absence of a provider operation prevents random echo |
| channel metadata/archive/delete | `type="stream", op="update"` / `op="delete"`; corresponding Zulip channel mutation | **bidirectional** | Pass the mapped channel command; Workspace domain service resolves the archive/history/bindings/visibility and writes outbox | The last accepted source mutation in mapped channel | `stream_id` + property/version + causation; Bridge It doesn 't duplicate . Workspace policy |
| own subscription | `type="subscription", op="add"` / `op="remove"` | **bidirectional** | Send membership change; Workspace on stream settings changes binding generation/history visibility | Provider membership plus Workspace security fence | Account+stream+user+generation+causation; Bridge does not create message bindings by itself |
| peer membership | `type="subscription", op="peer_add"` / `op="peer_remove"` | **bidirectional** | Pass membership changes for visible peers; Workspace resolves user, historical access and bindings | Provider subscriber set | Arrays expand to stable per-pair commands; group composition change It doesn 't create a new one . stream |
| personal subscription properties | `type="subscription", op="update"` with allowlisted `property`/`value` | **bidirectional** | Update mapped notification/mute/pin state when current Workspace contract has an equivalent | User-owned subscription state | User+stream+property+value+causation; unknown property is not silently stored |
| personal topic state | `type="user_topic"` with `stream_id`, `topic_name`, `visibility_policy` | **bidirectional** | Update mapped `USER_TOPIC_BINDING` notification/visibility state | Per-user topic state | User+topic mapping+policy+causation; current `user_topic` replaces legacy `muted_topics` |
| topic materialization | There is no universal `topic created`; topic appears in `message` | **bidirectional** through message flow | Create mandatory canonical `TOPIC` on first mapped message; Workspace-origin topic materializes in Zulip with its first mapped message, not a standalone provider create | Conversation/message context | Topic mapping + first message key; no synthetic provider `topic created` event |
| topic rename/move | `type="update_message"` topic/stream fields | **bidirectional** | Update mapping/placements for affected message set according to `propagate_mode` | Accepted provider operation result | Message/version/causation; each target topic has stable mapping and UUIDv5 placements |
| direct/self message | `type="message"`, `message.type="private"`, provider recipient data identifying direct or self conversation | **bidirectional** | Map to private direct/self Workspace `STREAM` + mandatory technical/canonical `TOPIC` | Provider conversation/participant identity | Stable provider conversation key + message key; exact key serialization belongs to canonical OPEN #2, no channel `stream` event is expected |
| group direct message | `type="message"`, `message.type="private"`, provider recipient data identifying group direct | **bidirectional** | Map to private group-direct Workspace `STREAM` + mandatory topic | Provider conversation/participant identity | Stable provider conversation key + message key; exact participant-key serialization belongs to canonical OPEN #2 |
| channel message | `type="message"`, `message.type="stream"`, `stream_id` + topic | **bidirectional** | Map to channel Workspace `STREAM` and mandatory topic placement | Zulip channel/topic mapping or Workspace mapped stream | Stream/topic/message provider keys + causation |
| legacy muted topics | `type="muted_topics"` | **unsupported** in target profile | None; target requests/uses `user_topic` | Zulip legacy client state | Not interpreted in parallel with `user_topic` |

Zulip `realm_user/update` field `person.role` is the realm-wide user role. That 's ...
Not universal channel-specific membership role. Direction for the selected
realm role is accepted as bidirectional, but its exact Workspace role/binding
mapping remains a narrow OPEN; arbitrary `WorkspaceStreamBinding.role` must not
be projected to Zulip without that mapping.

## Users and bots

| Family | Exact Zulip event/op or operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| ordinary user add | `type="realm_user", op="add"` for non-bot | **unsupported** for automatic creation | Validate existing identity mapping only; no hidden `WorkspaceUser` create | Provisioning/IAM or separately approved identity mapping | Missing mapping is fail-closed/deferred and visible to reconciliation |
| user name/avatar | `type="realm_user", op="update"`, `person.full_name`, `avatar_url`, `avatar_source`, `avatar_version` | **bidirectional** for an existing mapping | Update mapped Workspace user name/avatar projection; outbound user mutation uses provider operation | Last accepted supported field mutation | User key+field+version/causation; own avatar/name echo confirms operation |
| user email | `type="realm_user", op="update"`, email-related field | **unsupported** | Workspace email projection does not change from Zulip and does not go to Zulip | Each system has its own email | The field is obviously ignored; it is not participating in identity key |
| realm role | `type="realm_user", op="update"`, `person.role` | **bidirectional** with mapping OPEN | Update selected mapped Workspace role projection; exact target role cell remains OPEN | Accepted role mutation after authorization | User+role+causation; no blanket per-stream role rewrite |
| custom profile value | `type="realm_user", op="update"`, `person.custom_profile_field` | **bidirectional** for an existing mapping | Update mapped value only; schema creation/change is unsupported | Value on mapped user; schema remains local/unsupported | User+field ID+value+causation; unknown field schema fail-closed |
| deactivate/reactivate user | `type="realm_user", op="update"`, `person.is_active=false/true` | **bidirectional** for an existing mapping | Deactivate/reactivate mapped user and revoke/rebuild access through normal generations/tasks | Accepted lifecycle mutation | User+lifecycle version+causation; reactivation does not resurrect stale bindings silently |
| visibility-only/legacy removal | `type="realm_user", op="remove"` | **unsupported** as user delete | Refresh/revoke visibility evidence only; do not infer account deletion/deactivation | Zulip visibility policy | No hidden delete; requires explicit `is_active` lifecycle event for mutation |
| bot add | `type="realm_bot", op="add"` plus associated bot `realm_user` data | **Zulip→Workspace** | Create one special Workspace bot/external user and provider mapping | Zulip bot identity | Provider bot `user_id` key dedupes paired `realm_bot`/`realm_user` events |
| bot metadata update | `type="realm_bot", op="update"` | **unsupported** | None; bot metadata projection remains unchanged | Zulip only | Event acknowledged/audited without Workspace mutation |
| bot deactivate/delete | `type="realm_bot", op="delete"` and mapped bot `realm_user/update person.is_active=false` | **Zulip→Workspace** | Deactivate/delete special Workspace bot according to current local lifecycle; revoke access | Zulip bot lifecycle | Bot user key + delete/deactivate event key; paired events converge idempotently |
| legacy bot remove | `type="realm_bot", op="remove"` | **unsupported** in target current profile | None; deprecated event is not a second delete source | Zulip legacy | No duplicate lifecycle path |

Any supported ordinary user update requires provider identity mapping.
The `realm_user/add` itself is not automatic provisioning managed account.
History import, When you meet the author/member without Workspace account, it creates/reuses
unmanaged external user without credentials/session; later verified connect claims
The event-driven create exception is adopted for
`realm_bot/add` special user.

## Presence, persistent status and typing

| Family | Exact Zulip event/op or operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| presence | `type="presence"`; modern `presences.{user_id}.active_timestamp` / `idle_timestamp`, legacy `presence.website.status="active"` or `"idle"` | **bidirectional** | In sequence relay `active`/`idle`; derive `offline` after TTL; heartbeat refreshes `last_ping_at` | Last confirmed unexpected change by either party . | Origin/causation suppresses echo only, It doesn 't . winner; TTL clears stale presence |
| persistent user status | `type="user_status"` with `user_id`, `status_text`, `emoji_name`, `emoji_code`, `reaction_type` | **bidirectional** | In sequence persist mapped `status_text`/`status_emoji` and emit ordinary user update | Last confirmed change by any party | Origin/causation suppresses echo only; unlike presence, status survives TTL/restart |
| typing | `type="typing", op="start"` / `op="stop"` | **bidirectional** | Relay scoped typing signal to mapped Workspace recipients; no canonical message mutation | Latest non-expired signal | Origin/causation key + short TTL; stop and expiry both clear state |
| editing typing | `type="typing_edit_message", op="start"` / `op="stop"` | **bidirectional** | Relay edit-typing signal for mapped placement/message recipients | Latest non-expired signal | Sender+message+op+causation+TTL; access rechecked before relay |

Presence history When you restart the Connector, it will receive current
presence snapshot/heartbeat and then supports TTL; `user_status` is
persistent and goes into history/reconciliation.

## Personal data and UI state

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| drafts | `type="drafts"`, operations `add`, `update`, `remove` | **unsupported** | None; Workspace drafts and Zulip drafts are independent | Local on every system | Bridge not signed/not reflected |
| muted users | `type="muted_users"` | **unsupported** | None | Zulip only | No projection |
| reminders | `type="reminders"`, operations `add`, `remove` | **unsupported** | None | Zulip only | No projection |
| scheduled messages | `type="scheduled_messages"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip only | No projection |
| user client settings | `type="user_settings", op="update"`; `type="realm_user_settings_defaults", op="update"` | **unsupported** | None | Every system has client settings | No projection |
| navigation views | `type="navigation_view"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip UI | No projection |
| channel folders/UI grouping | `type="channel_folder"`, operations `add`, `reorder`, `update` | **unsupported** | None; Not to be confused with canonical Workspace folders | Zulip UI | No projection |
| alert words | `type="alert_words"` | **unsupported** | None | Zulip UI | No projection |
| saved snippets | `type="saved_snippets"`, operations `add`, `update`, `remove` | **OPEN** | Not to be used until a separate decision | Not selected | Fail-closed; event durable quarantined/audited, It doesn 't . draft/message |

## User groups and organization configuration

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
| realm export/deactivation | `type="realm_export"`, `type="realm_export_consent"`, `type="realm", op="deactivated"` | **unsupported** | None; The bridge lifecycle is not derived from these events | Zulip only | No implicit cleanup/destructive action |

## Devices, integrations, invites and service events

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| devices | `type="device"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip client state | No projection |
| external integration state | `type="has_webex_token"`, `type="has_zoom_token"` and equivalent provider UI state | **unsupported** | None | Zulip only | No projection |
| invites | `type="invites_changed"` | **unsupported** | None | Zulip only | No projection |
| heartbeat | `type="heartbeat"` | **Zulip→Workspace** | Refresh Connector/source queue liveness only; no Messenger domain mutation | Zulip event queue | Queue/event ID dedupe; never converted to public Workspace event |
| restart | `type="restart"` | **Zulip→Workspace** | Lifecycle signal: complete the connection and repeat the single bootstrap with the new one supported queue/boundary | Zulip server generation/feature level | One lifecycle generation handled once; old queue/cursor I don't need it. durable recovery |
| web reload signal | `type="web_reload_client"` | **Zulip→Workspace** | Repeat the same bootstrap/re-register, not interpret as browser page reload | Zulip server | Event ID/generation dedupe; new boundary + provider keys They provide overlap-safe recovery |
| onboarding/UI auxiliary | `type="onboarding_steps"` | **unsupported** | None | Zulip UI | No projection |

## History coverage

History Importer It uses the same direction matrix, but it only imports
persistent Supported state:

| State of play | History behavior |
| --- | --- |
| users | Create/reuse unmanaged external identities for imported authors/members without Workspace account; explicit verified connect claims them; import `realm_bot/add` special identities; `realm_user/add` alone does not provision managed login |
| streams/topics/memberships | Import Zulip channels, mandatory topics inferred from messages, current subscriptions/member state and supported personal topic state |
| messages | Import create/current content/move/delete state in the account range before registration boundary, newest-first per stream/topic; no experimental submessages |
| flags | Import only per-user provider flags observable under the authorized account/mapping; missing users/state are not synthesized |
| files/reactions | Import after message mapping or durable defer, using the same provider identities as realtime |
| user status | Import persistent `user_status` for a mapped managed/unmanaged identity only when authoritative snapshot exposes it; otherwise do not invent historical state |
| presence/typing/heartbeat/restart/web reload | `presence`, `typing`, `typing_edit_message`, `heartbeat`, `restart`, `web_reload_client` Not back-filled; Connector establishes fresh current state and TTL after queue registration |
| unsupported/OPEN families | Not imported; `saved_snippets` remains quarantined/unused until resolved |

## Compatibility boundaries without change public API

The direction **bidirectional** is accepted as target behavior, but it does not mean
The current public Workspace API does not have
at least separate actions for message move, mark-unread, typing and parts
user role/custom-field mutations. Their private initiation surface and authorization
must be selected before implementation; replace the existing endpoint with
Other semantics are prohibited..

The current contract also explicitly defines star state as Workspace-owned and
This is the default sync with the external provider. bidirectional
`read`/`starred` target behavior — consciously changing integration semantics:
JSON keys and the existing `star`/`unstar` actions do not change, but rollout is required
To mark-unread, you still need
private initiation surface, Since there is no current public action.

Move between topics/streams creates a new `MESSAGE_PLACEMENT.uuid`, because
public identity is calculated as
`UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Canonical `MESSAGE` not
Old placement is deleted, its URL returns `404` without redirect;
clients Get current-contract delete old placement and create/update target
placement. Idempotent duplicate It doesn 't create any repeats . ready events.

[← The main index of the documentation](../index.md) · [The index Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
