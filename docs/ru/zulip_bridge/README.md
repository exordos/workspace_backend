# Целевая архитектура Zulip Bridge

Статус: **proposal; первая server/Provider API v2 часть зафиксирована отдельно**.

[← Главный индекс документации](../index.md) · [Канонический инвентарь Messenger](../messenger_architecture_inventory.md) · [Действующая граница Zulip v1](../zulip_bridge_v1_product_and_api.md)

Wire transport, project scope, direct identity, outbound authorization и
provider event key первой реализации закрыты решениями `1B/2A/3A/4A/5A` в
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

Этот каталог описывает целевую двухпроцессную архитектуру синхронизации
Workspace↔Zulip и первичного импорта истории. Он не меняет действующие
публичные маршруты, JSON или закрытые контракты. Канонический клиентский
контракт остаётся в
[`workspace_api.md`](../workspace_api.md), а действующие provider/control/file
границы — в [`zulip_bridge_v1_product_and_api.md`](../zulip_bridge_v1_product_and_api.md)
и связанных OpenAPI-файлах.

## Документы

| Документ | Статус | Назначение |
| --- | --- | --- |
| [`architecture_overview.md`](architecture_overview.md) | **proposal** | Компоненты, trust boundaries, поток данных и разделение ответственности между Bridge и Workspace. |
| [`event_coverage.md`](event_coverage.md) | **proposal; принятое покрытие** | Каноническая матрица exact Zulip events/operations, направлений синхронизации, Workspace actions, source of truth и echo prevention. |
| [`realtime_connector.md`](realtime_connector.md) | **proposal** | Постоянный `Zulip Realtime Connector`: приём событий, порядок, retry, backpressure и graceful restart. |
| [`history_importer.md`](history_importer.md) | **proposal** | Конечный `Zulip History Importer`: fair pool default `4`, per-stream newest-first work, account limiter, restart/dependencies/reconciliation. |
| [`internal_workspace_api.md`](internal_workspace_api.md) | **proposal** | Общий внутренний Workspace API, ограниченная service identity, transaction boundary и единая идемпотентность. |
| [`coordination_and_recovery.md`](coordination_and_recovery.md) | **proposal** | Единый bootstrap, account lease/fencing, boundary, retry/DLQ, reconciliation и восстановление. |
| [`account_lifecycle_and_identity.md`](account_lifecycle_and_identity.md) | **proposal; current routes preserved** | Connect/reconnect/disconnect/delete, verified identity claim, unmanaged users и multi-account canonical union. |
| [`provider_mappings_and_content.md`](provider_mappings_and_content.md) | **proposal** | Realm-scoped provider keys, durable topic/file mappings, canonical Markdown/URN, deferred references и manual reconversion. |
| [`delivery_and_events.md`](delivery_and_events.md) | **proposal** | Durable Workspace→Zulip operations, conflict/permanent-failure semantics и exactly-one ready event per actual transition. |

## Диаграммы

| Сценарий | PlantUML | SVG |
| --- | --- | --- |
| Realtime synchronization и echo prevention | [`realtime_connector.puml`](diagrams/realtime_connector.puml) | [`realtime_connector.svg`](diagrams/realtime_connector.svg) |
| History import | [`history_importer.puml`](diagrams/history_importer.puml) | [`history_importer.svg`](diagrams/history_importer.svg) |
| Первичный импорт и переход к realtime-only | [`bootstrap_to_realtime.puml`](diagrams/bootstrap_to_realtime.puml) | [`bootstrap_to_realtime.svg`](diagrams/bootstrap_to_realtime.svg) |
| Verified claim unmanaged identity | [`identity_claim.puml`](diagrams/identity_claim.puml) | [`identity_claim.svg`](diagrams/identity_claim.svg) |
| Shared topic mapping, rename и partial move | [`topic_mapping_and_move.puml`](diagrams/topic_mapping_and_move.puml) | [`topic_mapping_and_move.svg`](diagrams/topic_mapping_and_move.svg) |
| Content conversion, deferred repair и reconversion | [`content_conversion_and_repair.puml`](diagrams/content_conversion_and_repair.puml) | [`content_conversion_and_repair.svg`](diagrams/content_conversion_and_repair.svg) |
| Outbound retry, permanent failure и public events | [`outbound_delivery.puml`](diagrams/outbound_delivery.puml) | [`outbound_delivery.svg`](diagrams/outbound_delivery.svg) |

## Канонический глоссарий

- **Bridge process** — внешний доверенный процесс без прямого доступа к БД
  Workspace;
- **service identity** — действующая realm-bound mTLS identity private External
  Bridge API: `realm_uuid`, `provider_kind`, `bridge_instance_uuid` и
  `identity_generation` берутся только из проверенного client certificate;
  account/project scope и допустимые команды Workspace затем определяет по
  текущим server-owned assignments;
- **provider object key** — стабильная внутренняя идентичность Zulip-объекта,
  одинаковая для realtime и history;
- **provider event key** — стабильный ключ одной мутации/версии Zulip-объекта,
  используемый как idempotency/derivation key;
- **provider object UUIDv5** — `UUIDv5(namespace=verified realm UUID,
  name="<entity_type>:<decimal_provider_id>")` для numeric Zulip objects;
- **registration boundary** — граница новой Zulip queue: realtime принимает
  события от неё, а history root импортирует выбранный snapshot/range до неё;
- **account lease/fencing generation** — Workspace-issued exclusive ownership
  всего external account одним Bridge instance; stale owner не может commit;
- **history root/stream task** — durable Workspace task: root раскрывает scope и
  создаёт per-stream tasks; stream task при restart повторяет свой range целиком;
- **deferred resolution** — сохранённая зависимость, которую нельзя применить
  до появления базового объекта;
- **Workspace projection worker** — внутренний worker Workspace, который
  запускается после outbox; это не Bridge process;
- **WebSocket dispatcher** — отдельный компонент Workspace, который доставляет
  готовые durable public events и не участвует в импортировании Zulip.

## Принятые инварианты

1. `Zulip Realtime Connector` и `Zulip History Importer` — независимые процессы
   с общей семантикой identity/idempotency, но разными жизненными циклами.
2. Ни один Bridge process не пишет напрямую в Workspace PostgreSQL или object
   storage metadata. Все доменные мутации проходят через ограниченный
   внутренний Workspace API.
3. Пользовательские access tokens не используются. Bridge не может выбрать
   произвольные `project_id`, source или Workspace user; эти значения выводит и
   проверяет Workspace по service identity и серверным mappings.
4. Создание сообщения использует обычную доменную транзакцию Workspace:
   canonical `MESSAGE` + обязательный `TOPIC` и `MESSAGE_PLACEMENT` + авторские
   `USER_MESSAGE_BINDING`/`USER_MESSAGE_STATE` + immutable outbox event.
5. Публичный UUID сообщения равен placement UUID:
   `UUIDv5(namespace=TOPIC.uuid, name=MESSAGE.uuid)`. Canonical `MESSAGE.uuid`
   остаётся внутренним.
6. Bridge не выполняет recipient fan-out, не обновляет Workspace projections и
   не создаёт public WebSocket events. Это делают обычные Workspace workers;
   dispatcher только доставляет уже готовые события.
7. Connect, reconnect, queue expiry, missing heartbeat, `restart` и
   `web_reload_client` используют один bootstrap: зарегистрировать новую queue,
   получить boundary, начать realtime и только затем создать history root task.
   Старый queue/cursor не является durable state; overlap/no-gap обеспечивают
   boundary и общие provider keys.
8. Старые UUID прежнего Zulip-импорта после согласованного full reset сохранять
   не требуется. Внутри нового импорта любой retry/resume обязан повторно
   адресовать ту же новую canonical row.
9. Каноническое покрытие и направление каждого Zulip event family задаёт
   [`event_coverage.md`](event_coverage.md). Bidirectional mutation несёт
   origin/causation/provider identity; собственное provider echo подтверждает
   исходную operation и не запускает бесконечную обратную запись.
10. Durable mappings, assignments, leases, tasks, outbound operations и errors
    принадлежат Workspace. Bridge instances не имеют общей Bridge database;
    local state является только сбрасываемым cache.
11. Один account целиком принадлежит одному fenced Bridge owner: realtime и
    history не разделяются между instances. Assignment sticky; healthy accounts
    автоматически не ребалансируются при появлении нового instance.
12. Bridge преобразует provider events/operations, но не реализует Workspace
    domain policy. History visibility, bindings и archive semantics решает
    Workspace по current stream settings.
13. Оба Bridge process переиспользуют действующую аутентификацию private
    External Bridge API: TLS 1.2+ mutual TLS, realm control CA, одноразовый
    enrollment и generation-bound client certificate. HTTP headers/body не
    могут подменить certificate identity. Whole-account lease/fencing —
    дополнительная transaction-time authorization, а не credential и не замена
    mTLS.

## Единый список OPEN-решений Zulip Bridge {#единый-список-open-решений-zulip-bridge}

Это единственный список незакрытых решений для данного каталога. Остальные
документы ссылаются сюда и не создают собственные копии.

Ранее открытые wire transport, event/direct keys, private initiation surface и
cross-account project scope закрыты решениями `1B/2A/3A/4A/5A` в
[`workspace_server_v2_decisions.md`](../workspace_server_v2_decisions.md).

1. Operational upper limits после load tests: maximum/optimal history worker
   pool выше default `4`, history batch/rate budgets, provider admission и
   retention failed history/DLQ/deferred evidence, не покрытого принятыми
   successful/permanent-operation TTL.
   Все пути bounded/configurable; один account-level limiter и realtime priority
   уже зафиксированы.
2. Направление и модель `saved_snippets`: семейство остаётся `OPEN` и не
   интерпретируется автоматически как Workspace draft/message.
3. Точное отображение realm-wide Zulip `realm_user/update person.role` на
    Workspace role model. Оно не должно молча становиться channel-specific
    `WorkspaceStreamBinding.role`.
4. Exact converter edge/loss policy для Zulip→canonical Markdown и обратного
    URN resolution, включая unsupported Zulip markup. Raw latest payload и
    manual reconversion boundary уже приняты.

Retention больше не OPEN: completed history tasks и successful outbound
operations хранятся `30 days`, internal permanent-failure operation/code/reason
— `90 days`, provider mappings/latest hidden raw metadata — lifetime связанной
entity. Возможный future manual requeue остаётся internal extension, не новым
current public endpoint. Retention failed history/DLQ/deferred evidence остаётся
OPEN #1 и не подменяется значениями `30/90 days`.

Связанные общие OPEN-решения Messenger, включая capacity/SLO, остаются в
[`messenger_architecture_inventory.md`](../messenger_architecture_inventory.md#единственный-список-open-решений).

[← Главный индекс документации](../index.md) · [Канонический инвентарь Messenger](../messenger_architecture_inventory.md) · [Действующая граница Zulip v1](../zulip_bridge_v1_product_and_api.md)
