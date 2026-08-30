# 事件覆盖和同步方向矩阵

状态: **提案; 经过一致的事件覆盖调查结果**.

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

它们的目标是:
添加公开 Workspace 终点,不改变
[`workspace_api.md`](../workspace_api.md). 精确的 Zulip事件字母被比较
具有相关目录 [`GET /events`](https://zulip.com/api/get-events).
Wire route/transport 其他 implementation boundaries 仅在
[单一的 OPEN-list](README.md#единый-список-open-решений-zulip-bridge).

## 方向的意义

- **bidirectional** — 变化可以在 Workspace 或 Zulip 开始;
  双方都同意一个维持状态;
- **Zulip→Workspace** — 只有在 Zulip;
  相关的Workspace突变不会被送回;
- **unsupported** — Bridge 不签名/不创建投影,也没有
  将事件解释为接近的支持类型;
- **OPEN** — 方向或语义映射尚未被接受; 突变
  fail-closed 并且不会自动应用.

`Workspace action/projection` 下面是逻辑目标命令,或者
如果在现行的公共API中没有这样的操作,
设定路线:选择的双向语义保留,而 private
initiation surface 留下来 implementation OPEN.

## 免受任何攻击 echo loop

每个双向突变都会产生或带来:

- `origin` (`workspace` 或 `zulip`);
- immutable `causation_uuid`/Workspace provider operation UUID;
- stable `provider_object_key`;
- stable `provider_event_key` 或 source event UUID/queue position;
- provider/Workspace version, 如果资源支持版本.

Workspace outbound operation 预期的 provider result 在调用之前保存
Zulip. 返回的 Zulip 事件也可以通过相同的方法进行 object/event/causation
并且确认了操作,但没有创建新的反向 operation.
如果 Zulip 不返回任意的客户端操作 UUID,桥接
echo 具有持续操作收件,提供者对象密钥和确认
version/state; 获得一次性并不是获得能力的关键..

对于短暂的 `presence`, `typing` 和 `typing_edit_message` 用
短暂的 origin/causation cache 与 TTL: 自己的反射不是
通过重播,消除了衰竭,
实际存在.精确的数字 TTL/heartbeat 包含在容量 OPEN 中,但
存在 TTL 和 loop prevention  强制性变量.

## Message/content family

| Family | Exact Zulip event/op 或 operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| message create | `type="message"`; outbound Zulip send message operation | **bidirectional** | `message.create`: canonical `MESSAGE` + mandatory `TOPIC`/`MESSAGE_PLACEMENT` + author binding/state + outbox | 系统采用了原始的 create mutation; commit  canonical Workspace row 和 provider mapping | Provider message identity + create causation; Zulip echo 确认 outbound operation |
| content edit | `type="update_message"` with `content`, `rendered_content`, `rendering_only` | **bidirectional** | Update canonical payload/source version; `content_mentions`/ready event async | 创作者 Markdown 原始突变; `rendered_content`  提供者衍生投影,没有 writable source | Same message object key + provider version/causation; rendering-only echo 不会创建重复 edit |
| message move / topic rename | `type="update_message"` with `stream_id`, `stream_name`, `subject`, `orig_subject`, `propagate_mode` | **bidirectional** | Whole-topic rename 保存 mapped topic UUID; partial move 删除 old placement 并创建 target placement,内容不会复制 | 已接受的移动突变和 authoritative Zulip result | Causation + provider message/version; target placement 返回新的 UUIDv5,旧的 URL 返回 `404`,事件反映 old delete + new create/update |
| message delete | `type="delete_message"`; outbound delete message operation | **bidirectional** | `message.delete`/provider tombstone + outbox; affected placements/access/counters async | 已接受 delete mutation | Same provider message key + delete causation/version; retry is no-op |
| reactions | `type="reaction", op="add"` / `op="remove"`; outbound add/remove reaction | **bidirectional** | Upsert/delete one canonical-message-global raw reaction fact; message-scope snapshot async | Raw reaction facts keyed by canonical message/user/emoji | Provider message+actor+`emoji_name`/`emoji_code`/`reaction_type` + causation; echo confirms fact |
| files/attachments | `type="attachment", op="add"` / `op="update"` / `op="remove"`; upload/delete provider file operations | **bidirectional** | Bounded allocate/upload/finalize; normalized attachment link; file/message projections async | Provider bytes/metadata for Zulip-origin file; Workspace bytes/metadata for Workspace-origin file | One canonical file per `(realm_uuid,attachment_id)`; repeated references reuse it, physical delete requires zero references |
| personal flags | `type="update_message_flags", op="add"` / `op="remove"`, `flag="read"` or `flag="starred"` | **bidirectional** | Update placement-scoped `USER_MESSAGE_STATE.read_at`/`starred`; ready counters/events async | Per-user state for mapped provider-owned placement | User+provider message+flag+op+causation; own echo does not emit reciprocal flag mutation |
| unread transition | `type="update_message_flags", op="remove", flag="read"` | **bidirectional** | Clear placement-scoped read marker through private target action; no public route is invented | Per-user state | Same flag key/causation; current public API has no mark-unread action, initiation surface OPEN |
| mentions and link/render results | `type="message"` fields `flags`, `content`, `rendered_content`, `topic_links`; corresponding `update_message` fields | **bidirectional** at message mutation level | Recompute/materialize mentions/links from accepted content; preserve sanitized provider projection | Raw authored content; each destination owns its derived render, but provider result may be projected | Content version/causation; derived-only change never sends original mutation back |
| experimental submessages | `type="submessage"`; `message.submessages[]` with `msg_type`/`content` | **unsupported** | None | Zulip only | Explicitly not subscribed/projected; no fallback to message body |

Message flags apply to the provider-owned placement mapped to the Zulip
message. 他们不随意地扩展到相同的手动放置
canonical `MESSAGE`. Reactions, 它们是故意的.
canonical-message-global 根据" 农业安全法" Messenger semantics.

## Channels, topics, subscriptions 其他 conversation mapping

| Family | Exact Zulip event/op 或 operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| Zulip channel create | `type="stream", op="create"` | **Zulip→Workspace** | Create/map canonical Workspace `STREAM` in server-owned project scope | Zulip channel | Provider `stream_id` + source event key; retry returns same stream mapping |
| Workspace stream create | Workspace `POST .../streams/`; 没有通用 Zulip 事件 | **unsupported** 为了 Workspace→Zulip create | 剩下原生Workspace流;提供商通道操作未创建 | Workspace | 显而易见的不对称性;没有 provider operation 防止随机 echo |
| channel metadata/archive/delete | `type="stream", op="update"` / `op="delete"`; corresponding Zulip channel mutation | **bidirectional** | 传输 mapped channel命令; Workspace域服务解决 archive/history/bindings/visibility并写 outbox | 最后一个被接受的源突变 mapped channel | `stream_id` + property/version + causation; Bridge 不重复 Workspace policy |
| own subscription | `type="subscription", op="add"` / `op="remove"` | **bidirectional** | 传输会员变更; Workspace 在流设置中改变 binding generation/history visibility | Provider membership plus Workspace security fence | Account+stream+user+generation+causation; Bridge 不会自行创建消息绑定 |
| peer membership | `type="subscription", op="peer_add"` / `op="peer_remove"` | **bidirectional** | 转载可见同行的会员变更; Workspace 解决用户,历史访问和 bindings | Provider subscriber set | Arrays expand to stable per-pair commands; group composition change 不会创建新的 stream |
| personal subscription properties | `type="subscription", op="update"` with allowlisted `property`/`value` | **bidirectional** | Update mapped notification/mute/pin state when current Workspace contract has an equivalent | User-owned subscription state | User+stream+property+value+causation; unknown property is not silently stored |
| personal topic state | `type="user_topic"` with `stream_id`, `topic_name`, `visibility_policy` | **bidirectional** | Update mapped `USER_TOPIC_BINDING` notification/visibility state | Per-user topic state | User+topic mapping+policy+causation; current `user_topic` replaces legacy `muted_topics` |
| topic materialization | 没有通用 `topic created`; topic appears in `message` | **bidirectional** 通过 message flow | Create mandatory canonical `TOPIC` on first mapped message; Workspace-origin topic materializes in Zulip with its first mapped message, not a standalone provider create | Conversation/message context | Topic mapping + first message key; no synthetic provider `topic created` event |
| topic rename/move | `type="update_message"` topic/stream fields | **bidirectional** | Update mapping/placements for affected message set according to `propagate_mode` | Accepted provider operation result | Message/version/causation; each target topic has stable mapping and UUIDv5 placements |
| direct/self message | `type="message"`, `message.type="private"`, provider recipient data identifying direct or self conversation | **bidirectional** | Map to private direct/self Workspace `STREAM` + mandatory technical/canonical `TOPIC` | Provider conversation/participant identity | Stable provider conversation key + message key; exact key serialization belongs to canonical OPEN #2, no channel `stream` event is expected |
| group direct message | `type="message"`, `message.type="private"`, provider recipient data identifying group direct | **bidirectional** | Map to private group-direct Workspace `STREAM` + mandatory topic | Provider conversation/participant identity | Stable provider conversation key + message key; exact participant-key serialization belongs to canonical OPEN #2 |
| channel message | `type="message"`, `message.type="stream"`, `stream_id` + topic | **bidirectional** | Map to channel Workspace `STREAM` and mandatory topic placement | Zulip channel/topic mapping or Workspace mapped stream | Stream/topic/message provider keys + causation |
| legacy muted topics | `type="muted_topics"` | **unsupported** in target profile | None; target requests/uses `user_topic` | Zulip legacy client state | 没有与 `user_topic` |

Zulip `realm_user/update` field `person.role` is the realm-wide user role. 这就是
不是通用 channel-specific membership role. Direction for the selected
realm role is accepted as bidirectional, but its exact Workspace role/binding
mapping remains a narrow OPEN; arbitrary `WorkspaceStreamBinding.role` must not
be projected to Zulip without that mapping.

## Users 其他 bots

| Family | Exact Zulip event/op 或 operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| ordinary user add | `type="realm_user", op="add"` for non-bot | **unsupported** for automatic creation | Validate existing identity mapping only; no hidden `WorkspaceUser` create | Provisioning/IAM or separately approved identity mapping | Missing mapping is fail-closed/deferred and visible to reconciliation |
| user name/avatar | `type="realm_user", op="update"`, `person.full_name`, `avatar_url`, `avatar_source`, `avatar_version` | **bidirectional** for an existing mapping | Update mapped Workspace user name/avatar projection; outbound user mutation uses provider operation | Last accepted supported field mutation | User key+field+version/causation; own avatar/name echo confirms operation |
| user email | `type="realm_user", op="update"`, email-related field | **unsupported** | Workspace email projection 不从Zulip变化,也不会被发送到 Zulip | 每个系统都有自己的 email | 显然忽略了;它不参与 identity key |
| realm role | `type="realm_user", op="update"`, `person.role` | **bidirectional** with mapping OPEN | Update selected mapped Workspace role projection; exact target role cell remains OPEN | Accepted role mutation after authorization | User+role+causation; no blanket per-stream role rewrite |
| custom profile value | `type="realm_user", op="update"`, `person.custom_profile_field` | **bidirectional** for an existing mapping | Update mapped value only; schema creation/change is unsupported | Value on mapped user; schema remains local/unsupported | User+field ID+value+causation; unknown field schema fail-closed |
| deactivate/reactivate user | `type="realm_user", op="update"`, `person.is_active=false/true` | **bidirectional** for an existing mapping | Deactivate/reactivate mapped user and revoke/rebuild access through normal generations/tasks | Accepted lifecycle mutation | User+lifecycle version+causation; reactivation does not resurrect stale bindings silently |
| visibility-only/legacy removal | `type="realm_user", op="remove"` | **unsupported** as user delete | Refresh/revoke visibility evidence only; do not infer account deletion/deactivation | Zulip visibility policy | No hidden delete; requires explicit `is_active` lifecycle event for mutation |
| bot add | `type="realm_bot", op="add"` plus associated bot `realm_user` data | **Zulip→Workspace** | Create one special Workspace bot/external user and provider mapping | Zulip bot identity | Provider bot `user_id` key dedupes paired `realm_bot`/`realm_user` events |
| bot metadata update | `type="realm_bot", op="update"` | **unsupported** | None; bot metadata projection remains unchanged | Zulip only | Event acknowledged/audited without Workspace mutation |
| bot deactivate/delete | `type="realm_bot", op="delete"` and mapped bot `realm_user/update person.is_active=false` | **Zulip→Workspace** | Deactivate/delete special Workspace bot according to current local lifecycle; revoke access | Zulip bot lifecycle | Bot user key + delete/deactivate event key; paired events converge idempotently |
| legacy bot remove | `type="realm_bot", op="remove"` | **unsupported** in target current profile | None; deprecated event is not a second delete source | Zulip legacy | No duplicate lifecycle path |

任何支持的普通用户更新都需要 provider identity mapping.
没有人.`realm_user/add`没有自动 provisioning managed account.
History import, 遇到作者/member没有Workspace创建一个 account/reuses
unmanaged external user 没有 credentials/session; later verified connect claims
对于一个数据库,
`realm_bot/add` special user.

## Presence, persistent status 其他 typing

| Family | Exact Zulip event/op 或 operation | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| presence | `type="presence"`; modern `presences.{user_id}.active_timestamp` / `idle_timestamp`, legacy `presence.website.status="active"` or `"idle"` | **bidirectional** | 连续 relay `active`/`idle`; derive `offline` after TTL; heartbeat refreshes `last_ping_at` | 最后确认任何一方的不完整变化 | Origin/causation suppresses echo only, 不选择 winner; TTL clears stale presence |
| persistent user status | `type="user_status"` with `user_id`, `status_text`, `emoji_name`, `emoji_code`, `reaction_type` | **bidirectional** | 连续 persist mapped `status_text`/`status_emoji` and emit ordinary user update | 任何一方的最新确认更改 | Origin/causation suppresses echo only; unlike presence, status survives TTL/restart |
| typing | `type="typing", op="start"` / `op="stop"` | **bidirectional** | Relay scoped typing signal to mapped Workspace recipients; no canonical message mutation | Latest non-expired signal | Origin/causation key + short TTL; stop and expiry both clear state |
| editing typing | `type="typing_edit_message", op="start"` / `op="stop"` | **bidirectional** | Relay edit-typing signal for mapped placement/message recipients | Latest non-expired signal | Sender+message+op+causation+TTL; access rechecked before relay |

Presence history 接口将在重启后输入 current
presence snapshot/heartbeat 然后支持 TTL; `user_status` 是
persistent 进入 history/reconciliation.

## Personal data 其他 UI state

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| drafts | `type="drafts"`, operations `add`, `update`, `remove` | **unsupported** | None; Workspace drafts 和 Zulip 独立的草稿 | 在每个系统中本地 | Bridge 不签名/不反映 |
| muted users | `type="muted_users"` | **unsupported** | None | Zulip only | No projection |
| reminders | `type="reminders"`, operations `add`, `remove` | **unsupported** | None | Zulip only | No projection |
| scheduled messages | `type="scheduled_messages"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip only | No projection |
| user client settings | `type="user_settings", op="update"`; `type="realm_user_settings_defaults", op="update"` | **unsupported** | None | 每个系统都拥有 client settings | No projection |
| navigation views | `type="navigation_view"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip UI | No projection |
| channel folders/UI grouping | `type="channel_folder"`, operations `add`, `reorder`, `update` | **unsupported** | None; 别与 canonical Workspace folders | Zulip UI | No projection |
| alert words | `type="alert_words"` | **unsupported** | None | Zulip UI | No projection |
| saved snippets | `type="saved_snippets"`, operations `add`, `update`, `remove` | **OPEN** | 在单独的解决方案之前不应使用 | 没有选择 | Fail-closed; event durable quarantined/audited, 没有变 draft/message |

## User groups 其他 organization configuration

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
| realm export/deactivation | `type="realm_export"`, `type="realm_export_consent"`, `type="realm", op="deactivated"` | **unsupported** | None; 操作员的桥生命周期不从这些 events | Zulip only | No implicit cleanup/destructive action |

## Devices, integrations, invites 其他 service events

| Family | Exact Zulip event/op | Direction | Workspace action/projection | Source of truth | Loop prevention / idempotency |
| --- | --- | --- | --- | --- | --- |
| devices | `type="device"`, operations `add`, `update`, `remove` | **unsupported** | None | Zulip client state | No projection |
| external integration state | `type="has_webex_token"`, `type="has_zoom_token"` and equivalent provider UI state | **unsupported** | None | Zulip only | No projection |
| invites | `type="invites_changed"` | **unsupported** | None | Zulip only | No projection |
| heartbeat | `type="heartbeat"` | **Zulip→Workspace** | Refresh Connector/source queue liveness only; no Messenger domain mutation | Zulip event queue | Queue/event ID dedupe; never converted to public Workspace event |
| restart | `type="restart"` | **Zulip→Workspace** | Lifecycle signal: 完成连接,并重复单一启动 supported queue/boundary | Zulip server generation/feature level | One lifecycle generation handled once; old queue/cursor 没有必要 durable recovery |
| web reload signal | `type="web_reload_client"` | **Zulip→Workspace** | 重复同一个bootstrap/re-register,而不是 browser page reload | Zulip server | Event ID/generation dedupe; new boundary + provider keys 确保 overlap-safe recovery |
| onboarding/UI auxiliary | `type="onboarding_steps"` | **unsupported** | None | Zulip UI | No projection |

## History coverage

History Importer 运用相同的方向矩阵,但只导入
persistent 支持状态:

| 情况 | History behavior |
| --- | --- |
| users | Create/reuse unmanaged external identities for imported authors/members without Workspace account; explicit verified connect claims them; import `realm_bot/add` special identities; `realm_user/add` alone does not provision managed login |
| streams/topics/memberships | Import Zulip channels, mandatory topics inferred from messages, current subscriptions/member state and supported personal topic state |
| messages | Import create/current content/move/delete state in the account range before registration boundary, newest-first per stream/topic; no experimental submessages |
| flags | Import only per-user provider flags observable under the authorized account/mapping; missing users/state are not synthesized |
| files/reactions | Import after message mapping or durable defer, using the same provider identities as realtime |
| user status | Import persistent `user_status` for a mapped managed/unmanaged identity only when authoritative snapshot exposes it; otherwise do not invent historical state |
| presence/typing/heartbeat/restart/web reload | `presence`, `typing`, `typing_edit_message`, `heartbeat`, `restart`, `web_reload_client` 没有背填; Connector establishes fresh current state and TTL after queue registration |
| unsupported/OPEN families | 不被进口; `saved_snippets` 待关/未使用 |

## Compatibility boundaries 没有变更 public API

方向**bidirectional**它们被视为目标行为,
现在的公共 Workspace API 没有
至少可以使用 message move, mark-unread, typing 和部分的单独动作
user role/custom-field mutations. 他们的私人启动表面和 authorization
必须在实现之前选择; 替换现有的端点
其他语义禁止.

现行合同还直接将星级国家定义为Workspace拥有,
没有与外部提供商同步. bidirectional
`read`/`starred` target behavior — 意识地改变 integration semantics:
JSON keys 并且现有的 `star`/`unstar` 动作不会改变,但rollout必须
描述一个新的提供者可见副作用.
private initiation surface, 因为目前没有公开的动作.

Move 在 topics/streams之间,它会创建一个新的`MESSAGE_PLACEMENT.uuid`,因为
public identity 计算为
`UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. 规范的 `MESSAGE` 不
旧的位置被删除,它的 URL 返回 `404` redirect;
clients 得到 current-contract 删除旧配置, create/update target
placement. Idempotent duplicate 不会产生重复 ready events.

[← 文件的主要索引](../index.md) · [索引 Zulip Bridge](README.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
