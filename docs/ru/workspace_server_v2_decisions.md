# Workspace Server v2: принятые решения реализации

Статус: **действующее дополнение к архитектуре и закрытому Provider API v2**.

[← Главный индекс](index.md) · [Provider API v2](../workspace_provider_api_v2.yaml) · [Целевая архитектура Zulip Bridge](zulip_bridge/README.md)

Этот документ фиксирует решения `1B`, `2A`, `3A`, `4A`, `5A`, согласованные
для первой реализации нового Workspace Server. Они уточняют docs-first
архитектуру, закрывают соответствующие пункты OPEN-list и не изменяют
публичный browser API или JSON, который использует существующий Workspace UI.

## 1B — Provider Data API v2 на действующем private transport

Новый inbound-контракт размещён на уже существующем отдельном mTLS listener:

- `POST /api/workspace-provider/v2/commands` — provider→Workspace commands;
- `POST /api/workspace-provider/v2/operations/actions/lease` — действующая
  очередь Workspace→provider;
- `POST /api/workspace-provider/v2/operation-results` — действующий отчёт о
  результате Workspace→provider operation.

V2 переиспользует current certificate identity, heartbeat, body limits,
transaction boundary, batch limit `500`, lease и result semantics v1. V1
остаётся доступен на время rolling upgrade. Новый listener, новый credential
protocol и новый public/browser route не вводятся.

Inbound v2 принимает provider identity, а не вычисленные Bridge значения
Workspace. `external_account_uuid` только выбирает уже назначенное подключение;
сервер проверяет его против mTLS identity и desired state. Bridge не передаёт и
не выбирает `project_id`, external-chat UUID, stream/topic/user/message UUID,
permissions или роли. Workspace в транзакции разрешает account, realm, chat,
project, stream/topic и identity mappings и только затем вызывает обычную
доменную мутацию.

## 2A — один realm-global provider chat принадлежит одному project {#2a--один-realm-global-provider-chat-принадлежит-одному-project}

Для пары `(provider, verified provider realm, provider_chat_key)` может быть
выбран только один Workspace project. Несколько external accounts того же
realm могут переиспользовать chat в том же project, но выбор этого chat в другом
project отклоняется до изменения desired state.

Конфликт возвращает HTTP `409` с безопасным кодом
`provider_scope_conflict`. Проверка выполняется в транзакции под advisory lock,
а частичный индекс выбранных chat ограничивает рабочий набор проверки. Это
простая и дешёвая модель: routing остаётся однозначным, а fan-out и публичные
проекции не дублируются между projects.

Upgrade проверяет этот инвариант до reset/copy. Legacy same-realm/same-chat
aliases внутри одного project сводятся автоматически, но уже существующий
выбор такого chat в нескольких projects останавливает migration fail-closed:
автоматически выбрать project означало бы без согласования переместить или
скрыть внутренние Workspace-сообщения.

До первого provider discovery, когда verified realm UUID ещё не известен,
предварительной областью служит нормализованный `server_url`. Выбор берёт URL
lock; после discovery — URL и realm locks в стабильном порядке. Проверка
конфликта сопоставляет и известный realm UUID, и одинаковый provider origin,
поэтому старый web-flow создания/первого выбора account остаётся рабочим без
изменения клиента, а параллельный выбор не обходит правило 2A.

Provider origin вычисляется как настоящий HTTP origin: схема и DNS-имя
приводятся к нижнему регистру, завершающая точка DNS и стандартные порты
удаляются, IPv6 сохраняет скобки; path не участвует в scope key. DNS aliases
окончательно сводятся после discovery по verified realm UUID. Первая trusted
привязка каждого account берёт те же advisory locks для всех уже выбранных chat
этого account и до записи identity отклоняет его catalog report, если другой
account уже подтвердил тот же realm/chat в ином project. Первый подтвердивший
realm account остаётся владельцем routing; alias, выбранный в другом project,
получает безопасный код `provider_scope_conflict`. Проверять alias раньше нельзя
без trusted realm:
одинаковые числовые chat ID допустимы в независимых Zulip realms. Поэтому
account с alias не становится вторым активным источником данных, а bridge может
повторно опубликовать catalog после устранения конфликтующего выбора.

При выборе того же realm-global chat в том же project Workspace под тем же
advisory lock переиспользует уже материализованные `projection_stream_uuid` и
exact `provider_topic_id -> topic_uuid` mappings. Account-scoped external-chat
UUID остаётся только control-plane identity assignment: обе desired assignment
ссылаются на один stream/topic graph, поэтому повторный account import не
создаёт вторую публичную проекцию. Первый выбранный account остаётся владельцем
provider routing; последующие same-project accounts являются aliases этого
маршрута. Deselect/delete routing-владельца атомарно передаёт маршрут первому
оставшемуся selected alias под тем же realm/chat lock; удаление обычного alias
меняет только control plane и не удаляет общий stream/topic graph.
Независимые backfill/live deliveries этих aliases сходятся на realm-global
message/reaction UUID. Server принимает их только при совпадении verified realm,
project, projection stream и provider chat, сохраняя первый materializing
account как стабильного владельца уже существующей проекции.

## 3A — realm-global provider identity и direct conversation key

Numeric Zulip objects используют:

```text
UUIDv5(namespace=verified_realm_uuid, name="<type>:<shortest-decimal-id>")
```

Разрешённые `type`: `user`, `channel`, `message`, `attachment`. Numeric ID —
unsigned shortest base-10 ASCII без знака, пробелов и leading zero. Project,
account, server URL, email и mutable display name в identity не входят.

Channel key имеет форму `channel:<shortest-decimal-channel-id>`. Direct/self/
group conversation key имеет точную сериализацию:

```text
direct-conversation:v1:<count>:<id1>,<id2>,...
```

Список содержит уникальные provider user ID всех участников и обязательно
verified owner подключённого account. ID сортируются по числовому значению;
`count` равен числу ID. Поэтому один и тот же self-chat, DM или group DM имеет
один ключ для history/realtime и для всех accounts того же realm.

## 4A — только существующие авторизованные public actions для outbound

Generic private command «записать любую Workspace модель» запрещён. Provider
API v2 не является способом доказать user intent и не даёт Bridge browser/IAM
полномочия.

Workspace→Zulip operation создаётся только после текущей публичной action,
которая уже проверила пользователя, project scope и permission, после чего
Bridge получает её через lease. В первой части остаются включены только
поддержанные текущим сервером operation kinds. Initiation paths, которых нет в
действующем public API (включая generic message move, mark-unread, typing и
произвольные role/custom-profile mutations), остаются выключенными. Unknown
kind и подстановка Workspace identity отклоняются до mutation.

Для lifecycle mapped channel/topic зафиксирована следующая точная семантика:

- `stream.delete` вызывает официальный Zulip archive-channel endpoint. Перед
  повтором Bridge читает текущее состояние channel и считает уже
  заархивированный channel достигнутым состоянием;
- `topic.delete` вызывает официальный batch delete-topic endpoint. Ответ
  `complete=false` является retryable, а отсутствие темы при предварительном
  чтении считается идемпотентно достигнутым состоянием;
- `topic.create` не создаёт синтетическое Zulip message: у Zulip нет отдельного
  topic-объекта, поэтому Bridge атомарно запоминает deterministic
  `<channel-id>:<topic-name>` mapping, а первая обычная `message.create`
  материализует тему. Переименование до первого сообщения меняет только этот
  mapping и также не создаёт provider traffic.

Эти три capability публикуются только для channel chats. Дополнительные
provider reads выполняются только на редких destructive actions, поэтому не
добавляют постоянной нагрузки realtime/history импортёру.

## 5A — state-based provider event key и отдельная delivery identity

`provider_event_key` описывает желаемое логическое provider state. Он одинаков
для history и realtime и не зависит от account, project, queue event ID,
локальной sequence или Bridge database.

Перед вычислением ключа Bridge формирует JSON object:

```json
{
  "provider_chat_key": "<exact chat key>",
  "provider_object": {"kind": "<kind>", "id": "<provider object id>"},
  "provider_references": {},
  "payload": {}
}
```

JSON кодируется UTF-8 с ключами в лексикографическом порядке, separators
`,`/`:`, без дополнительного whitespace и с `ensure_ascii=false`. Из payload
до нормализации удаляются server-owned Workspace IDs и transport-only
metadata: `account_uuid`, `chat_key`, `delivery_class`, `external_id`,
`provider_event_uuid`. Digest — lowercase hexadecimal SHA-256 этих exact bytes.

Wire key:

```text
provider-event:v1:<command-kind>:<object-kind>:<object-id-utf8-byte-length>:<object-id>:<sha256>
```

`provider_sequence` передаёт только настоящую provider revision; локальная
producer sequence ею не подменяется. При отсутствии provider revision значение
равно `null`.

Отдельный canonical UUID string `delivery_uuid` стабилен при transport retry
одной durable delivery, но не является semantic identity. Workspace выводит
внутренний ledger UUID как:

```text
UUIDv5(verified_realm_uuid,
       "provider-delivery:v2:<provider_event_key>:<delivery_uuid>")
```

Поэтому identical retry одной delivery дедуплицируется, а новая delivery того
же semantic state повторно входит в доменную транзакцию и сверяется с текущим
состоянием. Уже достигнутое состояние даёт no-op без лишнего public event;
последовательность `add → remove → add` применяет второй `add`, хотя оба add
имеют одинаковый `provider_event_key`.

## Миграция данных — native preserve и автоматический Zulip reimport

Уточнение, принятое после решений `1B`–`5A`:

- versioned migration переносит все authoritative native streams, topics,
  messages, user state, reactions, folders и files в canonical v2 без смены
  публичного browser-контракта;
- осиротевшие recipient-only UUID из исторических broadcast snapshots не
  превращаются в фиктивных project users: migration сохраняет само native
  событие, но не создаёт canonical membership/guard для уже удалённого IAM
  пользователя;
- `0157` использует container boundary: удаляются все messages, размещённые в
  canonical stream с точной парой `source_name=zulip` и `source.kind=zulip`,
  независимо от происхождения самого message. Поэтому Workspace→Zulip
  outbound messages в таком stream также удаляются и затем возвращаются
  обычным backfill из Zulip. `0158` завершает reset: удаляет messages с такой же
  точной Zulip provenance, даже если они были спроецированы в native Direct
  container. Native-origin messages в том же container сохраняются;
- в той же транзакции удаляются связанные reactions/read/event projections и
  неиспользуемые Zulip files. Legacy compact stats и canonical v2
  stream/topic/folder counters пересчитываются по сохранённым messages до
  commit. Поэтому смешанные native containers сохраняют roles, membership
  generations, notification modes, topic state и положение в folders, а их
  unread, active/passive и last-message значения восстанавливаются точно.
  Сначала обновляются compact message/read stats каждого topic в затронутом
  stream; затем canonical `read_at` каждого пользователя приводится к
  authoritative compact bitmap (либо к legacy read flag вне режимов
  compact/rollback), и только после этого публикуются counters;
- старые `link_kind=provider_identity`, созданные account-scoped реализацией,
  переводятся на exact `UUIDv5(verified_realm_uuid, "user:<id>")`. Все
  surviving native relational references, event payloads, chat catalog и
  current/pending desired resources переписываются в той же транзакции;
  `verified_account_owner` остаётся привязан к IAM UUID и не участвует в этом
  преобразовании. Конфликт между provider identity и IAM owner останавливает
  migration fail-closed вместо неявного объединения пользователей;
- selected external accounts/chats, credentials и project assignment
  сохраняются. Для старого account-scoped формата same-realm/same-chat
  stream/topic aliases атомарно сводятся в один graph: membership, folders,
  drafts, files, native messages, user topic state и события переносятся в
  канонические containers, после чего только лишние containers удаляются.
  Account получает монотонный
  `projection_reset_generation`, account/chat desired generations повышаются,
  состояние возвращается в `backfill`/`syncing`;
- Bridge хранит последний применённый reset generation. При его увеличении он
  атомарно удаляет только rebuildable Zulip cache/idempotency/mappings, оставляя
  identity и catalog, отменяет завершённые backfill jobs и запускает полный
  импорт заново. Retry того же generation ничего повторно не сбрасывает;
- физическое содержимое удаляемых Zulip files обрабатывает bounded durable
  worker queue после DB commit. Перед удалением shared object повторно
  проверяется отсутствие retained DB reference; retry идемпотентен. Worker
  регистрирует оба file-storage config domain, поэтому local и S3 cleanup
  используют тот же настроенный backend, что и Messenger API;
- при удалении native stream membership старые broadcast audience rows этого
  membership generation физически отзываются. Повторное добавление пользователя
  не возвращает события предыдущего поколения даже после rolling view rebuild;
- logical desired-state snapshot не собирается целиком в Python и не хранится
  одним JSONB array. Он замораживается как упорядоченные PostgreSQL rows с
  cascade lifetime от snapshot token; page read выбирает `limit + 1` rows.
  Это сохраняет согласованный anchor и ограничивает RSS control API независимо
  от общего числа chats и размера participant/topic catalogs;
- перед чтением snapshot anchor и заморозкой rows server берёт PostgreSQL
  `SHARE ROW EXCLUSIVE` lock на change journal. Snapshot ждёт уже начатые
  append transactions и на короткое время не допускает выдачу новых sequence,
  поэтому concurrent upsert/delete обязательно попадает либо в frozen rows,
  либо строго после anchor. Snapshot создаётся только при bootstrap/reset, а
  не в realtime loop; глобальная краткая пауза control-plane writers проще и
  дешевле постоянной дополнительной commit-order инфраструктуры;
- destructive reset работает fail-closed по container и message metadata:
  частичная или противоречивая пара `source_name`/`source.kind` останавливает
  migration до удаления. Полная граница — объединение подтверждённых Zulip
  containers и messages с подтверждённым Zulip origin, включая legacy-only
  compatibility rows и canonical rows, связанные через message или placement;
- unattended frozen cutover ограничен одним миллионом legacy messages,
  ожиданием lock не более 30 секунд и statement deadline 45 минут. Больший
  cutover требует явного разрешения оператора после backup и production-sized
  rehearsal; целевые 50 миллионов сообщений — steady state после reimport, а
  не разрешение на automatic legacy conversion;
- control-plane snapshot scale gate использует не менее 15 000 assignments с
  большими participant/topic catalogs, измеряет bounded backend RSS и читает
  только bounded pages;
- обязательный scale gate использует не менее `100 000` старых provider message
  mappings и доказывает, что reset завершается, завершённая backfill job снова
  становится `pending`, а старая дедупликация не подавляет свежий импорт.

Rollback schema не восстанавливает намеренно уничтоженную Zulip projection:
для этого используется проверенный pre-migration backup. Native data остаются
доступны и при upgrade, и при schema downgrade.

## Неизменяемый cutover и forward-repair идентичности

Миграция `0152`, опубликованная в Workspace Server `1.0.0`, неизменяема. Новая
подготовительная ветка (`0155`) начинается от `0151`, а join-head (`0156`)
перечисляет её перед обычной цепочкой `0152` → `0154`. Поэтому при чистом
upgrade происхождение данных подготавливается до запуска опубликованного
cutover. Инсталляция, где `0152` уже записана, пропускает подготовку и получает
forward-repair в `0156`. Поскольку `pg_dump` не сохраняет planner statistics,
fresh-путь также выполняет `ANALYZE` для всех замороженных inputs до запуска
неизменяемых set-based statements.

Подготовка признаёт historical outbound echo только при точной успешной
операции `message.create`. `source.message_id` может отсутствовать, но не может
противоречить provider ID. Согласованные native rows, созданные до появления
operation queue, временно получают `discarded` provenance markers. Они не могут
попасть в provider queue и удаляются join-head миграцией.

Первая опубликованная после `0152` версия Bridge не записывала
`source.message_id`, но сохраняла полный согласованный legacy-набор:
`source.kind=zulip`, числовой `provider_external_id`, тот же ID в provider
metadata, исходный provider URL и непротиворечивый realm. Во время forward
repair `0156` принимает только такую полную форму. Уникальная строка получает
realm-global identity, а доказанная account-alias копия отсоединяется, если уже
есть импорт с global key. Частичные или противоречивые варианты по-прежнему
атомарно останавливают миграцию. Rolling legacy triggers применяют то же правило
совместимости до вывода этой версии Bridge из эксплуатации.

`0156` назначает сохранённым сообщениям realm-global provider identity и
оставляет provider linkage только у одного победителя для физического Zulip
message. Доказанные account aliases должны совпадать по realm/message ID,
project, author, разным account owners, provider URL и metadata identity. Все
внутренние messages, placements и public UUID сохраняются; у проигравших
aliases отсоединяется только provider linkage. Уже импортированная строка с
global identity имеет приоритет над совпадающим retained alias. Любой
недоказанный конфликт атомарно останавливает миграцию. Rolling triggers на
legacy insert/update затем поддерживают то же правило до отключения старых
серверов.

## Владение общей Zulip projection и повторный импорт

Для realm-global Zulip channel используется один canonical stream на проект
Workspace. Поэтому несколько выбранных аккаунтов могут ссылаться на один
`projection_stream_uuid`, а физический stream сохраняет владельца, который
материализовал его первым. Provider ingestion принимает другого владельца
аккаунта только тогда, когда в том же проекте есть выбранный peer assignment на
этот stream. Без такой сохранённой связи несовпадение владельца остаётся
жёсткой ошибкой.

Для `topic.upsert` сервер формирует типизированный Workspace source из
сохранённого canonical stream: его стабильный account scope не меняется, а к
нему добавляется имя topic. Bridge не обязан повторять server-owned source
fields в каждом событии.

Миграция `0154` один раз увеличивает reset generation каждого Zulip-аккаунта и
повторно публикует выбранные assignments. Это удаляет карантин частично
доставленного импорта и запускает полный повтор. Provider keys идемпотентны,
поэтому уже принятые строки обновляются, а не дублируются. При новой установке
остановленный Bridge видит только итоговое generation и выполняет один импорт.

## Совместимость и границы первой реализации

- Публичные маршруты, ответы и WebSocket events Workspace UI не меняются.
- V2 является закрытым provider data-plane, а не browser API.
- Server-owned scope и canonical IDs не раскрываются как новые поля public
  Messenger resources.
- V1 transport сохраняется только как rolling adapter; новым источником истины
  для provider identity является v2 contract.
- Полный wire-контракт приведён в
  [`workspace_provider_api_v2.yaml`](../workspace_provider_api_v2.yaml).
