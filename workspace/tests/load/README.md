# Workspace Messenger scale and load harness

These assets implement the reusable part of the PostgreSQL canonical Messenger
load gate. They are inert by default: fixture planning touches no service, and
both k6 programs and the metrics collector require an explicit execution flag.
Historical outputs must be stored in the CASSI test-run archive, not here.

## Deterministic fixture

Generate the CI or full logical fixture outside the repository:

```bash
python workspace/tests/scale/generate_messenger_fixture.py \
  --profile workspace/tests/scale/profiles/db-300x120x100k-v1.json \
  --output-dir /path/to/private-run/fixture
```

The full profile contains 300 users, 120 streams, 480 topics, 100,000 messages,
300,000 canonical broadcast event rows, 6,000,000 authorized visible event
deliveries and exactly 150 live users. The delivery count is a load/fanout
expectation, not 6,000,000 canonical per-recipient event rows. Each live user
owns one distinct Zulip account. Ninety streams are provider-synced. Every such
stream maps to exactly one distinct external account, and that account's owner
is an authorized stream member. Every provider message inherits the stream
mapping; the sender is never used to select an account. Stream membership is
the sole source of recipient fanout; the event total is not invented
independently. The generator writes a content-free manifest and expected
correctness ledger with normalized digests, event-retention age buckets,
production-shaped S3 metadata sidecars, account mappings, cursor ordinals and
outbox idempotency keys. It also writes `application-plan.jsonl`. That plan
contains synthetic fixture values and stable identifiers split into
digest-bound idempotent units, not user or production content.

The plan is a complete logical contract even while application is blocked.
Native streams declare an owner, privacy and invite policy, a default topic,
and deterministic member-binding UUIDs and roles. Provider accounts and chats
declare deterministic account/chat IDs, provider chat keys, catalog
participants and provider topic IDs. Projection stream/topic UUIDs are derived
with the same `_projection_uuid(chat_uuid, resource_type, external_id)` rule as
the backend. Provider messages carry their `external_chat_uuid` plus the exact
inbound Provider event or outbound `enqueue_provider_operation` argument
shape. File records contain a bounded deterministic content recipe; the bytes,
size, binary hash and sidecar metadata/hash are all derived from that one
recipe.

Canonical event records likewise have deterministic UUIDs and timestamps plus
an explicit seven-day retention bucket around a fixed reference time. The
isolated-test event service accepts those exact UUIDs and timestamps while the
ordinary production event API keeps its generated defaults. Direct
canonical-table SQL is not an acceptable substitute.

The generator is a planner and verifier input. `--apply` has no built-in
canonical writer and fails closed unless all of these are explicit:

- `WORKSPACE_FIXTURE_EXECUTE=1` and a non-default
  `WORKSPACE_FIXTURE_TARGET`;
- `--project-id` and a unique `--run-id`;
- a mode-0600 `WORKSPACE_FIXTURE_CREDENTIALS_FILE` bound to the target SHA-256,
  the same project/run UUIDs, and `environment: isolated_test`;
- `WORKSPACE_FIXTURE_CONCRETE_ADAPTER=module:factory`.

The fixed worker boundary calls `prepare`, `apply_unit`, `export_observed`,
`cleanup_manifest`, and the read-only `export_inventory` on the concrete
adapter. It owns exactly one outer
RESTAlchemy context for each call, including one transaction per application
unit. Adapter code receives that session and must not create a nested session
manager. `prepare` returns durable completed unit IDs, so rerunning the same
run resumes. Each adapter write must remain idempotent because a process may
commit and stop before the next prepare. The result includes a sanitized
observed run ledger, read-only `actual-inventory.json` from canonical
PostgreSQL/S3 state, and an explicit cleanup manifest outside the repository.
`fixture-manifest.json` remains marked `dry_run: true`; only the separately
written `fixture-application-result.json` with `completed: true` proves a
successful application. Adapter failure writes no success result.

The concrete opt-in adapter maps the generated plan to Messenger and Provider
application services, caller-owned RESTAlchemy transactions, and exact S3
binary plus sidecar writes. Direct SQL writes are limited to the isolated-test
run/unit/resource/observation ledgers used for durable resume and cleanup;
read-only inventory SQL inspects canonical tables but never seeds them. Missing mappings,
credentials, application services, or acknowledgements fail closed and cannot
produce a completed application result. The k6 scripts exercise the HTTP
boundary and contain no database seeder.

The concrete exporter queries project-scoped canonical rows, provider mappings,
provider inbound idempotency and outbound operation ledgers, audience
membership and exact S3 bytes/sidecars. The provider ledger check covers the
stable external operation/event IDs, account and project scope, target,
sequence, action, status, attempts and payload hashes; deployment-specific
queue and bridge-instance UUIDs are deliberately not copied into expectations.
Planner-only values are not
reported as observations: account and file digests contain only persisted or
byte-verifiable fields, and logical IAM/project IDs are restored only after the
actual mapped identities are checked. Extra-object checks are project-scoped
through parseable metadata sidecars; an orphan binary without a sidecar cannot
be attributed to a project and remains a bucket operations concern.

Run the fail-closed gate with the application result that binds inventory to the
exact manifest, profile, project and run. The verifier independently compares
all observable sections and rejects stale or `passed: true`-tampered files:

```bash
python workspace/tests/scale/verify_messenger_fixture.py \
  --manifest /path/to/private-run/fixture/fixture-manifest.json \
  --actual-inventory /path/to/private-run/fixture/actual-inventory.json \
  --application-result /path/to/private-run/fixture/fixture-application-result.json \
  --report /path/to/private-run/fixture/inventory-report.json

python workspace/tests/scale/verify_messenger_fixture.py \
  --manifest /path/to/private-run/fixture/fixture-manifest.json \
  --actual-inventory /path/to/private-run/fixture/actual-inventory.json \
  --application-result /path/to/private-run/fixture/fixture-application-result.json \
  --observed-ledger /path/to/private-run/observed-ledger.jsonl \
  --report /path/to/private-run/correctness-report.json

python workspace/tests/scale/verify_messenger_fixture.py \
  --manifest /path/to/private-run/fixture/fixture-manifest.json \
  --actual-inventory /path/to/private-run/fixture/actual-inventory.json \
  --application-result /path/to/private-run/fixture/fixture-application-result.json \
  --expected-run-ledger /path/to/private-run/native-expected.jsonl \
  --expected-run-ledger /path/to/private-run/provider-expected.jsonl \
  --observed-run-ledger /path/to/private-run/backend-observed.jsonl \
  --observed-run-ledger /path/to/private-run/provider-observed.jsonl \
  --report /path/to/private-run/run-correctness-report.json
```

The deterministic ledger uses `workspace.messenger.correctness-ledger/v1`.
Composable load-run ledgers use `workspace.messenger.run-ledger/v1`; their key
is `(run_id, source, operation_uuid)`. Native k6 emits an expected record before
each attempt and an authoritative observed record from the exact Workspace
create response afterwards. Provider k6 emits an expected record plus a
separate `workspace.messenger.run-diagnostic/v1` visibility record. A provider
observation is authoritative only at the destination: an inbound operation
requires `workspace_backend`, while an outbound operation requires
`provider_connector`. The destination exporter binds the exact operation UUID
to the provider event/result, account, owner, stream/topic, idempotency key,
payload hash, and account-local cursor ordinal. Source-side rows from the other
exporter are ignored rather than compared as a second result. Unknown provider
operation kinds and runs with only diagnostic visibility evidence fail closed.
Scanning the latest 100 messages for matching content cannot prove loss,
duplication, or account isolation.

An observed ledger carries
only stable identifiers, hashes, cursor ordinals and result IDs. It must never
contain message content, tokens, API keys or internal targets. The verifier
fails on loss, multiple provider results for one logical operation, unexpected
operations, cross-account ownership, owner/mapping/direction/payload-hash
mismatches, cursor regression or an idempotency key that resolves to multiple
provider results.

## Credentials and route files

No credential or target file belongs in Git. Supply paths through environment
variables. `messenger_db.js` expects `WORKSPACE_CREDENTIALS_FILE` containing
exactly 150 entries under `users`; each entry has `ordinal`, `access_token`,
`workspace_user_uuid`, `project_id`, an authorized `stream_uuid` and
`topic_uuid`, plus the initial `epoch_version` and `epoch_generation`.

Fixture application uses the private
`workspace.messenger.fixture-credentials/v2` schema. In addition to the
target/project/run binding described above, it must map every logical fixture
user to an already provisioned IAM identity and token by exact fixture ordinal:

```json
{
  "schema_version": "workspace.messenger.fixture-credentials/v2",
  "environment": "isolated_test",
  "target_sha256": "<sha256-of-private-target>",
  "test_project_id": "<test-project-uuid>",
  "run_id": "<unique-run-uuid>",
  "workspace_identity_mappings": [
    {
      "ordinal": 0,
      "logical_user_uuid": "<fixture-user-uuid>",
      "iam_user_uuid": "<existing-iam-user-uuid>",
      "access_token": "<private-existing-user-token>"
    }
  ],
  "external_account_credentials": [
    {
      "credential_ref": "zulip-account-000",
      "server_url": "https://zulip.example.invalid",
      "email": "fixture-user@example.invalid",
      "api_key": "<private-zulip-api-key>"
    }
  ]
}
```

The mapping must cover every fixture user exactly once, with contiguous
ordinals matching the seeded logical-user order; logical IDs and IAM IDs must
be unique. Keep this file mode 0600 outside the repository. Tokens and IAM
credentials are consumed only at the application boundary and are never copied
to `fixture-manifest.json`, `application-plan.jsonl`, ledgers, cleanup output or
archived evidence.

The external-account list must cover every `credential_ref` in the application
plan exactly once. Enable the concrete PostgreSQL/S3/Provider application path
with
`WORKSPACE_FIXTURE_CONCRETE_ADAPTER=workspace.tests.scale.concrete_adapter:create_adapter`.
The adapter records only UUIDs, hashes, storage object identifiers and cleanup
order; it never writes provider credentials or Workspace access tokens to its
resume tables or output artifacts.

`zulip_provider.js` expects `WORKSPACE_PROVIDER_CREDENTIALS_FILE` containing
exactly 150 entries under `accounts`. Each entry has a unique
`workspace_user_uuid`, `external_account_uuid`, `workspace_access_token`,
`project_id`, `zulip_server_url`, `zulip_email`, `zulip_api_key`, and the Zulip
target fields. Accounts selected for projected traffic additionally carry their
authorized `stream_uuid`, `topic_uuid`, `cursor_ordinal_base`, and
`cursor_ordinal_limit`. The range must be atomically reserved for that account
and run before k6 starts; concurrent runs may not share it. New ordinals use
the global k6 scenario iteration, stay monotonic across VUs, and fail closed
before crossing the reservation limit.
The fixture has 90 such mappings;
the remaining accounts still participate as concurrent Workspace users and
account lifecycle fixtures. The separate `WORKSPACE_PROVIDER_ROUTES_FILE`
supplies only public application endpoints:

```json
{
  "workspace_base_url": "https://workspace.example.invalid",
  "zulip_message_url": "https://zulip.example.invalid/api/v1/messages",
  "zulip_history_url": "https://zulip.example.invalid/api/v1/messages?anchor=10000000000000000&num_before=100&num_after=0"
}
```

The symbolic values above are schema examples only. Use routes from the exact
deployed Provider API contract; do not copy example domains into a run.

## k6 profiles

Default runs perform one dry-run iteration and load neither targets nor
credentials:

```bash
k6 run workspace/tests/load/k6/messenger_db.js
k6 run workspace/tests/load/k6/zulip_provider.js
```

Real execution additionally requires `WORKSPACE_LOAD_EXECUTE=1`, a unique
`WORKSPACE_LOAD_RUN_ID`, the relevant target and file-path variables, and
`K6_SUMMARY_PATH` outside the repository. The Messenger workload maintains 150
websocket users while generating 100 REST reads/s and 20 native mutations/s.
The provider workload is a release-gate E2E profile, not a direct Provider API
component probe. Inbound traffic follows `Zulip -> connector -> Workspace` and
polls the public Messenger API for exactly one destination message. Outbound
traffic follows `Workspace Messenger API -> provider queue -> connector ->
Zulip` and polls Zulip history for exactly one destination message. Direct
`/api/workspace-provider/v1` calls are deliberately absent: that private
listener is authenticated by one mTLS bridge-instance identity, not by 150
user bearer tokens, and bypassing the connector would not validate the product
path. Provider HTTP component tests belong in the focused backend/connector
test suites.

The E2E workload ramps to 150 messages/min, holds for 30 minutes, bursts to
400/min for five minutes while 30 accounts reconnect through the public
external-account action, then observes recovery for ten minutes. Direction is
deterministic 60% inbound and 40% outbound.

For an uncertain Zulip send result, the provider workload waits, searches the
account's configured history route for the exact sent content and resends only
when no match is found. This is the accepted at-most-once plus manual
reconciliation policy and avoids unconditional duplicate retries.

Extract `WORKSPACE_RUN_EXPECTED_V1` and native
`WORKSPACE_RUN_OBSERVED_V1` JSON lines into separate JSONL files. Extract
provider `WORKSPACE_RUN_DIAGNOSTIC_V1` only for troubleshooting; it is not a
verifier input. Export provider observations from both the Workspace backend
and provider connector with `evidence_source` set to `workspace_backend` or
`provider_connector`. The verifier selects the direction-specific destination
row and treats the other as source-side evidence. Do not merge by line order or
content; the verifier composes files by exact run/source/operation identity. Do
not archive the diagnostic console stream if it contains target details.

## Five-second metrics

`collect_metrics.sh` is dry-run by default. Execution requires
`WORKSPACE_METRICS_EXECUTE=1`, `WORKSPACE_METRICS_OUTPUT_DIR` and
`WORKSPACE_METRICS_DURATION_SECONDS`. Optional inputs are:

- `WORKSPACE_PROCESS_PATTERN` for aggregate count/RSS only;
- `WORKSPACE_CGROUP_PATH` for cgroup v2 memory and CPU counters;
- `WORKSPACE_API_HEALTH_URL`, `WORKSPACE_PROVIDER_HEALTH_URL` and
  `WORKSPACE_S3_METRICS_URL` for status/latency probes whose bodies are dropped;
- `WORKSPACE_CURL_CONFIG_FILE` for a private curl credential config;
- `WORKSPACE_PGSERVICE` for a preconfigured libpq service entry.

The collector writes five-second system/cgroup/process/API/provider/S3 samples
and PostgreSQL database/statement aggregates. It never records HTTP bodies,
SQL row content, process arguments, credentials or endpoint values.
