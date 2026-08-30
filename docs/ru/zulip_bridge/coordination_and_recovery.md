# Coordination, bootstrap и recovery

Статус: **proposal; обязательная semantics, transport/runtime details частично OPEN**.

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)

Документ заменяет прежние схемы durable old-queue cursor catch-up, message-level
history checkpoint и общей Bridge DB. Durable coordination живёт в Workspace;
Bridge local state — сбрасываемый cache.

## Authentication перед coordination

Каждый private control/Provider/file request сначала проходит действующую
realm-bound mTLS authentication `workspace-external-bridge-api`: TLS client
certificate определяет `realm_uuid`, `provider_kind`, `bridge_instance_uuid` и
`identity_generation`; current backend state повторно проверяется на каждом
request. Одноразовый enrollment, certificate renewal/revoke и secret storage не
переопределяются этой proposal.

Только после authentication Workspace проверяет whole-account assignment,
lease/fencing generation и project/chat scope. Lease отвечает на вопрос «какой
instance сейчас владеет account», но не удостоверяет сам process. Поэтому stale
lease при валидном certificate даёт authorization refusal, а новый lease не
делает неаутентифицированный request допустимым.

## Whole-account lease и fencing

Workspace выдаёт одному Bridge instance lease на весь external account и
monotonic fencing generation. Account не делится между instances по stream,
topic или direction. Private API принимает mutation/task/receipt только при
активном lease и совпадающей generation.

V1 допускает один Bridge instance, но assignment model сразу multi-instance:

1. Workspace рассматривает только healthy compatible instances.
2. Новый account получает instance с минимальной normalized load
   `active_accounts / declared_capacity`; tie-breaker должен быть стабильным.
3. Assignment sticky: появление нового instance не ребалансирует healthy
   accounts автоматически.
4. Переносятся новые accounts и accounts, чей owner dead/draining.
5. Realtime и history одного account всегда находятся у одного owner Bridge.

- Heartbeat отправляется каждые `10s`; instance становится `degraded` после
  `30s` без heartbeat и `offline` после `60s`.
- Graceful shutdown/draining явно прекращает новые claims и освобождает leases.
- После `60s` offline timeout новый instance claims весь account, получает новую
  fencing generation и запускает тот же bootstrap.
- Stale owner не может commit provider receipt, task result или cursor advance.
- Disconnect/delete отзывает generation; work не переезжает к другому account.
- Durable account/tasks/mappings/outbound errors остаются Workspace-owned.

## Единый bootstrap connect, reconnect и recovery {#единый-bootstrap-connect-reconnect-и-recovery}

Один алгоритм используется после connect, reconnect, lease takeover, queue
expiry, missing heartbeat, `restart` и `web_reload_client`:

1. Пройти current mTLS identity check, затем проверить active account, verified
   credential и whole-account lease.
2. Зарегистрировать новую Zulip event queue только для supported event types.
3. Получить registration boundary, достаточную для snapshot/history split.
4. При registration failure повторить с backoff; history root не создавать.
5. Начать sequential realtime consumption от новой boundary.
6. Идемпотентно создать Workspace history root task с account selection,
   `history_depth`, boundary и lease generation.

Старый queue ID/cursor не нужен durable recovery. Новая boundary не должна
создавать gap: history охватывает selected snapshot/range до boundary, realtime
— events начиная с неё. Inclusive/exclusive wire representation зависит от Zulip
registration response и остаётся private transport detail, но реализация обязана
доказать coverage обоих соседних диапазонов. Допустимый overlap дедуплицируется
provider object/event keys.

## Realtime terminal acceptance

Connector per account держит не более одного inbound supported event в работе:

1. Получить next event.
2. Сопоставить ровно с одной private Workspace command либо lifecycle signal.
3. Повторять ту же command/key при transient/ambiguous failure.
4. На applied/duplicate/stale/confirmed или classified permanent failure
   считать event terminal.
5. Только после terminal acceptance перейти к next event.

Это не означает durable reuse старого queue после loss: queue recovery снова
проходит bootstrap. Provider keys делают replay/overlap безопасным.

## History task model без message checkpoint

Workspace хранит immutable/root task и per-stream child tasks. Root открывает
selected chats, discovers users/streams/topics/memberships и создаёт child task
на каждый channel/direct/group-direct stream. Child фиксирует immutable input:
account, stream, history range, boundary и provider task identity.

В v1 нет message-level checkpoint. Если child падает до terminal completion,
следующий claim повторяет весь selected stream range newest-first. Уже
применённые objects быстро возвращают duplicate/no-op по provider keys. Другие
completed stream tasks не повторяются. Task имеет обычные `pending` →
`leased/running` → `completed`/`failed` transitions, attempts/backoff, lease
expiry, fencing, bounded retry и DLQ/reconciliation evidence.

Разные stream tasks одного account могут выполняться параллельно на том же
Bridge через общий configurable pool, default `4`; exact maximum/optimum остаётся
до load tests. Один stream одновременно принадлежит одному history worker.
Topics/messages внутри stream идут последовательно, потому что Zulip topic —
атрибут message; messages `created_at DESC` со stable provider-message
tie-breaker. Между accounts scheduler использует fair round-robin, внутри
account — last activity/newest stream first.

Все history workers одного account делят account-level Zulip rate limiter. При
`Retry-After` history этого account приостанавливается на provider interval.
Realtime lane отдельна, имеет приоритет и возобновляется первой; history не
может израсходовать budget, нужный realtime.

## Retry и permanent classification

| Outcome | Действие |
| --- | --- |
| transient transport/`429`/temporary unavailable | Backoff+jitter, тот же provider/operation/task key; no advance |
| applied / duplicate / stale | Terminal success; no repeated outbox/event for no-op |
| missing older dependency | Durable deferred reference; current event/task может завершиться после доказанного сохранения dependency |
| invalid/cross-scope/conflicting verified owner | Fail-closed, permanent evidence/admin resolution |
| internal outbound `permanent_failed` | Остановить endless retry; safe code/reason private, current public delivery shape unchanged |
| unsupported family | Не подписываться; unexpected occurrence audited, без guessed mutation |

Completed history tasks и successful outbound operations удаляются через
`30 days`; permanent-failure operation/code/reason — через `90 days`. Future
manual requeue остаётся internal extension. Public retry route, уже существующий
для external operations, не заменяет internal classification и не расширяется
этой proposal.

## Deferred references и reconciliation

Newest-first history может увидеть quote/file/older message reference раньше
mapping. Workspace сохраняет internal deferred reference, а после появления
mapping идемпотентно repair-ит canonical Markdown/URN/mentions. Actual change
пишет outbox и ready event; no-op не пишет event.

Reconciliation проверяет:

- active account lease/generation и отсутствующие stale commits;
- history root/child coverage, failed/DLQ tasks и selected range totals;
- provider-key uniqueness, gaps/duplicates и multi-account union references;
- topic alias mappings, file attachment links, unresolved references;
- pending/retryable/permanent outbound operations;
- projection/outbox/task/ready-event consistency в Workspace.

## Backpressure и graceful restart

Realtime intake не заменяется history throughput: realtime всегда обслуживается
раньше. History default pool `4`, upper limit/rate/batch limits
bounded/configurable. Fair round-robin не даёт одному account монополизировать
pool. При graceful
stop Bridge прекращает новые claims/provider calls, завершает или освобождает
current unit, conditional пишет только terminal result и явно отдаёт leases.
При hard crash takeover разрешён только после `60s` offline timeout; новый owner
с новой generation повторяет bootstrap и unfinished stream task range.

Наблюдаемость включает account generation/lease age, queue registration
failures, realtime event age, history root/stream lag, restarts/full-range
replays, duplicate/no-op ratio, deferred/DLQ age, outbound retry/permanent
failure и отдельно Workspace projection/WebSocket lag. Content, `api_key`, raw
payload и personal identifiers не входят в labels/errors.

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Account lifecycle](account_lifecycle_and_identity.md) · [Realtime Connector](realtime_connector.md) · [History Importer](history_importer.md)
