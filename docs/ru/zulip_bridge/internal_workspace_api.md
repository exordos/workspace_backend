# Общий внутренний Workspace API для Zulip Bridge

Статус: **proposal; первая Provider Data API v2 wire-часть зафиксирована**.

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Матрица событий](event_coverage.md) · [Обзор архитектуры](architecture_overview.md)

Оба Bridge process вызывают один внутренний вариант обычного Workspace API.
Это private service-to-service boundary поверх тех же application services и
RestAlchemy transaction rules, которые создают целевые canonical entities. Он
не является новым публичным клиентским API и не даёт Bridge прямой доступ к
таблицам.

Действующий закрытый Provider API описан в
[`workspace_provider_api_v1.yaml`](../../workspace_provider_api_v1.yaml), а его
control/file security profile — в
[`zulip_bridge_control_api_v1.yaml`](../../zulip_bridge_control_api_v1.yaml) и
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml). Target
обязан переиспользовать эту уже реализованную realm-bound mTLS authentication.
Первая реализация использует точные routes и wire format из
[`workspace_provider_api_v2.yaml`](../../workspace_provider_api_v2.yaml), а
решения scope/identity/idempotency зафиксированы в
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).
Альтернативный authentication mechanism не проектируется.

## Действующая S2S authentication — обязательная target-граница

Zulip Bridge использует существующий отдельный private process/listener
`workspace-external-bridge-api`, не публичный Workspace nginx и не browser IAM
token. TLS 1.2+ завершается в backend process; обычный запрос обязан предъявить
client certificate, подписанный realm control CA. HTTP forwarding header,
bearer token или поля body не являются источником service identity.

Certificate содержит ровно один URI SAN в действующем формате:

```text
https://schemas.genesis-corporation.ru/workspace/external-bridge/v1/realms/{realm_uuid}/providers/{provider_kind}/instances/{bridge_instance_uuid}/generations/{identity_generation}
```

Workspace извлекает из него `realm_uuid`, `provider_kind`,
`bridge_instance_uuid` и positive `identity_generation`, проверяет current
certificate fingerprint, active generation и backend state на каждом request,
включая reused TLS connection. Certificate identity не содержит account или
project: server-side desired assignments и transaction-time checks сужают её до
разрешённого external account/chat/project.

Lifecycle переиспользуется без нового credential protocol:

1. Platform выдаёт отдельный one-time enrollment secret на Bridge installation
   и generation через защищённую Core-managed config. Backend хранит только
   verifier; значение token не является постоянным service credential.
2. Bridge получает realm CA через существующий HMAC-authenticated bootstrap,
   генерирует private key локально и отправляет CSR на `/v1/enrollments` с
   `X-Workspace-Enrollment-Token`. Успешная выдача атомарно закрывает generation;
   повтор того же `request_uuid` и CSR идемпотентен, изменённый replay отклоняется.
3. Client leaf живёт `30 days`, renewal начинается за `7 days` до expiry и
   аутентифицируется ещё действующим mTLS certificate. Новый ключ/CSR создаётся
   на Bridge; старый и новый leaf допустимы одновременно не более `24 hours`.
4. Suspend запрещает request немедленно. Revoke повышает identity generation;
   certificate прежней generation больше не принимается. Потеря/expiry требует
   operator-controlled enrollment-secret rotation, не shared long-lived token.

Private key остаётся только на persistent Bridge disk. Backend PKI/enrollment
state хранится в root-owned mode-`0700` dedicated store, отдельные sensitive
files пишутся mode `0600`; raw enrollment token, verifier, client private key и
credential payload запрещены в logs/errors. Account lease/fencing generation
остаётся отдельной mutable authorization/ownership check: валидный mTLS
certificate без active matching account assignment/lease не разрешает command.

Failure boundary уже определена: certificate, отклонённый TLS stack, может не
получить HTTP response; отсутствующая/неактуальная application identity даёт
`401`; current instance state или assignment запрещает request через `403`;
invalid cross-scope command отклоняется до mutation. В proposal не добавляется
новая auth error shape в публичный Workspace API.

Именно этот механизм выбран потому, что он уже обслуживает тот же долгоживущий
External Bridge process и все три current private resource groups: control,
Provider data и files. Public IAM bearer относится к user/browser request;
одноразовый enrollment header лишь выдаёт первый certificate; HPKE credential
envelope и single-object file capability защищают payload/object, но не
аутентифицируют service. Они не являются альтернативами mTLS.

## Service identity и server-owned scope

После mTLS-аутентификации Workspace получает неизменяемый service context:

- certificate-bound `realm_uuid`, `bridge_instance_uuid`, provider kind `zulip`
  и `identity_generation`;
- отдельно проверенный whole-account lease/fencing generation;
- разрешённые external account/assignment generations;
- realm/project mapping, который хранит Workspace;
- допустимый набор logical commands;
- действующую provider policy, suspension/revocation и capability set.

Bridge передаёт provider object/event identity и payload, но не authoritative
`project_id`, `source`, Workspace `user_uuid`, роль или permissions. Если такие
поля нужны wire envelope для трассировки, Workspace сверяет их с server-owned
mapping и отклоняет несовпадение; значение клиента никогда не определяет
tenant или автора.

Для каждой команды Workspace внутри request transaction повторно проверяет:

1. mTLS service identity active, certificate/identity generation актуальны,
   instance не suspended/revoked;
2. external account назначен этому bridge/provider, active lease generation
   совпадает и provider policy разрешает operation;
3. provider object принадлежит разрешённому account/chat scope;
4. server-owned project/stream/topic/user mappings существуют и имеют ту же
   tenant identity;
5. mutation разрешена capability и не пересекает project boundary.

Composite tenant FK и `UNIQUE(project_id, ...)` остаются последней физической
границей. Service preflight не заменяет transaction-time authorization.

## Две стабильные идентичности

`provider_object_key` и `provider_event_key` решают разные задачи.

| Key | Назначение | Обязательное свойство |
| --- | --- | --- |
| `provider_object_key` | Найти одну logical entity Zulip при create/update/delete и после рестарта | Одинаков для realtime/history и стабилен в пределах fresh import |
| `provider_event_key` | Дедуплицировать одну provider mutation/delivery и вывести один immutable outbox event | Один source event/version даёт один ключ, retry не меняет его |

Семантический состав identity:

| Kind | Provider object identity |
| --- | --- |
| user | verified realm UUID + typed `provider_user_id` |
| stream/chat | verified realm UUID + typed channel/conversation identity |
| topic | Workspace-owned durable mapping `(realm,channel,current name/alias history)` → stable canonical `TOPIC.uuid` |
| message | verified realm UUID + typed numeric `provider_message_id`; importing account не входит в canonical identity |
| reaction | canonical provider message identity + actor provider user identity + exact `emoji_name` |
| membership | provider stream/chat identity + provider user identity |
| file/attachment | `(verified realm UUID, typed attachment_id)`; canonical file один, normalized message↔file links отдельны |

Для bidirectional commands envelope также содержит `origin` и
`causation_uuid`/Workspace provider operation UUID. Outbound Workspace
operation сначала durable связывает causation с provider object/version, а
возвратившееся Zulip event подтверждает эту operation без порождения новой
обратной mutation. Если provider не возвращает client UUID, сервер использует
durable operation receipt + provider object key + version/state; timestamp не
является доказательством echo. Полная direction/source-of-truth matrix:
[`event_coverage.md`](event_coverage.md).

Numeric provider UUIDv5 использует exact algorithm:
`UUIDv5(namespace=verified_realm_uuid,
name="<entity_type>:<decimal_provider_id>")`. Разрешённые lowercase ASCII
types: `user`, `channel`, `message`, `attachment`. Provider ID сериализуется как
unsigned shortest base-10 ASCII (`0` либо digits без leading zeros, sign,
whitespace/locale formatting); name bytes — exact ASCII/UTF-8 без NUL/BOM/
newline/additional fields. Project/account UUID не являются namespace. Exact
keys для events/direct conversations определены решениями `3A/5A` в
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

Старые Workspace UUID прежнего импорта не входят в ключ. Первый fresh import
создаёт новую canonical row, а повтор той же операции внутри этого импорта
возвращает/обновляет её через provider mapping. Для message create Workspace
сам назначает internal `MESSAGE.uuid` и детерминированно получает public
placement UUID из canonical topic/message.

## Каталог логических команд

Имена ниже описывают semantic command kinds, а не утверждают HTTP route names.

| Logical command | Primary write Workspace | Idempotency/object rule |
| --- | --- | --- |
| `identity.claim` / `user.ensure_external` | Verified account claim existing identity либо create/reuse unmanaged external user; email только candidate, не proof | realm+user ID; conflicting verified owner fail-closed |
| `user.mapping.refresh` / `user.lifecycle.update` | Existing managed/unmanaged ordinary-user mapping: supported name/avatar/role/custom value/active state; email исключён | provider user key + field/version/event key |
| `bot.create` / `bot.deactivate` | Special Workspace bot/external user; только Zulip-origin lifecycle | provider bot user key + event key/version; metadata update unsupported |
| `stream.create_from_provider` | Canonical `STREAM` + provider mapping только из Zulip `stream/create` | provider channel key + event key; native Workspace stream create не вызывает эту команду |
| `stream.update` / `stream.delete` | Передать mapped provider change в Workspace domain service; он выбирает archive/history/bindings/visibility и пишет outbox | provider chat key + event key/version; Bridge не применяет policy |
| `topic.resolve` / `topic.rename` | Workspace-owned durable mapping с alias history; mandatory `TOPIC` под immutable stream/project owner | realm+channel+current/old topic name; whole rename сохраняет UUID |
| `membership.upsert` / `membership.revoke` | Передать membership fact; Workspace по stream settings меняет persistent binding/generation, historical visibility и message bindings | provider stream+user key + event key/version; composition change не создаёт stream |
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
| `account.lease.*` / `account.bootstrap.*` | Whole-account lease/fencing, queue boundary и bootstrap generation | account UUID + monotonic generation |
| `history.root.*` / `history.stream_task.*` | Root discovery и immutable per-stream range task lifecycle | account+boundary+selection/range+stream; no message checkpoint v1 |

Команды не открывают generic operation «записать любую модель». Unknown kind,
unmapped tenant, stale service generation, unsupported capability или попытка
подставить project/user приводят к отказу до mutation.

Имена в каталоге — logical proposal kinds, а не публичные paths. Они не разрешают
непроверенному email claim-ить managed user, не разрешают Workspace stream
create создавать Zulip channel и не превращают unsupported event в generic
upsert. Import может создать только unmanaged external user без session.

Bridge не вычисляет Workspace domain policy перед command: group/private member
change и channel archive/delete передаются достоверно как provider facts.
Workspace transaction сама решает historical access, bindings и visibility.

## Граница исходящих provider operations

Для Workspace-origin mutation из bidirectional coverage primary transaction
добавляет immutable outbox event. Private integration boundary идемпотентно и
без потери выводит из него durable provider operation с unique source outbox
event UUID, server-owned account/object mapping, `origin=workspace`,
`causation_uuid` и expected version/state. Realtime Connector получает
operation через эту boundary, вызывает Zulip и возвращает durable
receipt/confirmation. Exact queue/HTTP transport, derivation mechanism и ack
schema остаются OPEN #1; приложение не публикует user token и не использует
public WebSocket event как transport.

Direction guard является серверным: например, native Workspace stream create
не создаёт outbound channel operation. Собственный Zulip queue echo разрешается
по receipt/object/version и завершает causation, но не проходит повторно как
новая inbound command. Provider call retry сохраняет тот же operation identity.

## Transaction boundary сообщения

`message.create` выполняется атомарно:

1. Lock/dedupe realm-scoped `provider_object_key` и `provider_event_key` под
   active account lease generation.
2. Если event уже committed, вернуть тот же semantic result без новой мутации.
3. Разрешить server-owned author/stream/topic/project mappings.
4. Создать или восстановить одну canonical `MESSAGE` по provider key.
5. Создать один обязательный `MESSAGE_PLACEMENT`; authoritative uniqueness —
   `(project_id,message_uuid,stream_uuid,topic_uuid)`.
6. Получить public placement UUID как
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`.
7. Создать author `USER_MESSAGE_BINDING` и `USER_MESSAGE_STATE` с актуальной
   membership generation.
8. Записать immutable outbox event и committed idempotency receipt в той же DB
   transaction.
9. Commit либо rollback всех строк вместе.

Bridge не ждёт recipient fan-out. Workspace workers выводят bindings/states
получателей, snapshots/counters и durable ready events через общий one-event →
one-task protocol. Детали canonical task kinds находятся в
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#task_kinds-и-routing).

## Update/delete ordering

Для одного provider object Workspace сравнивает provider version/sequence,
если источник её предоставляет:

- повтор той же версии и payload — idempotent success;
- более старая версия — stale no-op с сохранением нового состояния;
- новая версия — одна mutation + один outbox event;
- одинаковая identity с конфликтующим payload/version — terminal conflict для
  DLQ/reconciliation, а не silent overwrite.

Update/delete, пришедшие раньше create из overlap/newest-first range, не создают
synthetic `MESSAGE`. Workspace сохраняет durable deferred dependency либо
возвращает retryable missing-base outcome. Точное wire кодирование outcome
остаётся OPEN, но durable dependency принадлежит Workspace, не local Bridge DB.

## Реакции

Публичный action адресует placement UUID для access check, но импортная команда
находит canonical message через provider message mapping. Source of truth — raw
fact с ключом
`(project_id,canonical_message_uuid,user_uuid,emoji_name)`. Realtime/history
retry изменяет только этот факт. Message-scoped Workspace worker материализует
`reactions`/`reaction_users` во всех placements; Bridge snapshots не пишет.

## Files и attachments

Bridge не получает bucket-wide credentials и не пишет storage metadata. После
authorization Workspace выдаёт single-object transfer capability, проверяет
size/hash и фиксирует finalize/attachment relation. Подробности действующей
границы находятся в
[`zulip_bridge_file_api_v1.yaml`](../../zulip_bridge_file_api_v1.yaml).

Target обязан сохранить свойства:

- один bounded object на allocation;
- finalize и attachment link идемпотентны;
- bytes commit не делает metadata visible до Workspace transaction;
- retry не создаёт второй blob/row/link;
- delete не удаляет physical object при retained native reference;
- provider identity `(realm_uuid,attachment_id)` переиспользует один file;
- physical object удаляется только после zero native/provider references.

## Семантические результаты и ошибки

Wire statuses ещё не выбраны, но outcomes должны различаться:

| Outcome | Смысл | Действие Bridge |
| --- | --- | --- |
| applied | Primary mutation и outbox committed | Realtime принимает event terminal; history продолжает current task |
| duplicate/no-op | Тот же provider event/state уже committed | Terminal без повторного outbox/ready event |
| stale | Более новое provider state уже зафиксировано | Terminal no-op + metric |
| deferred | Missing mapping/base dependency durable в Workspace | Terminal для source unit; repair после зависимости |
| retryable | Timeout/rate limit/temporary unavailable, commit не доказан | Повторить тот же key; realtime не читает next event |
| permanent/terminal | Provider rejection или invalid scope/conflicting identity | `permanent_failed`/DLQ evidence; endless retry/silent skip запрещены |

Если ответ потерян после commit, повтор с тем же event key обязан доказать
commit и вернуть duplicate/same result. Генерация нового ключа для retry
запрещена.

## Audit и privacy

Логи и traces содержат certificate-bound bridge instance/generation, provider
kind, account/mapping UUID, object/event key digest, outcome и latency, но не
enrollment token/verifier, certificate private key, user token, API key, raw
credential или полный private payload. Workspace audit остаётся tenant-scoped.

Provider mappings и latest hidden raw/converter metadata живут с entity.
Completed history tasks и successful outbound operations очищаются через
`30 days`, permanent-failure operation/code/reason — через `90 days`. Это
internal retention без новых public fields/actions.

Незакрытые детали wire routes/transport и provider-key serialization перечислены только в
[индексе](README.md#единый-список-open-решений-zulip-bridge).

[← Главный индекс документации](../index.md) · [Индекс Zulip Bridge](README.md) · [Матрица событий](event_coverage.md) · [Обзор архитектуры](architecture_overview.md)
