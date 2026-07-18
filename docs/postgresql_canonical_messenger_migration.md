# Maildir to PostgreSQL Messenger migration

This runbook imports the transitional Maildir projection into the PostgreSQL
canonical tables without changing the public Messenger REST/websocket contract.
The tool never changes `messenger_storage.mode`, never enables the cutover flag,
never writes Maildir, and never deletes the mail VM or disk.

Run every command with the same service configuration used by the backend. The
configuration contains credentials and must not be copied into reports. Reports
contain UUIDs, counts, normalized hashes, source positions and file-object
hashes, but never passwords, message bytes, file bytes, or sidecar contents.

## Operator commands

Set shell variables outside shell history according to the local operations
policy:

```shell
set -Eeuo pipefail

PROJECT_IDS=(<first-workspace-project-uuid> <second-workspace-project-uuid>)
declare -A RUN_IDS GATE_IDS ATTEMPT_GATE_CLOSED
RUN_IDS[<first-workspace-project-uuid>]=<first-new-random-uuid>
RUN_IDS[<second-workspace-project-uuid>]=<second-new-random-uuid>
CONFIG=/etc/workspace/workspace.conf
REPORT_DIR=<private-test-run-directory>
```

The importer deliberately operates on one project at a time. Enumerate every
strict `p-<32 lowercase hex digits>` project mailbox in the configured Dovecot
mail home before the run, decode each suffix as a UUID, and put the complete
sorted set in `PROJECT_IDS`. Cross-check it against IAM projects and PostgreSQL
provider/control references. A malformed project mailbox or an unexplained set
difference blocks migration. Use a distinct `RUN_IDS` value and report
directory for every project. The commands below describe one loop iteration;
the production cutover runbook provides the required all-project orchestration.

```shell
PROJECT_ID=<one-project-id-from-PROJECT_IDS>
PROJECT_REPORT_DIR="$REPORT_DIR/projects/$PROJECT_ID"
mkdir -p "$PROJECT_REPORT_DIR"
RUN_ID=${RUN_IDS[$PROJECT_ID]:?missing migration run UUID}
```

1. Capture a read-only dry-run inventory. Any quarantine entry blocks the run.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" \
  --output "$PROJECT_REPORT_DIR/inventory.json" inventory
```

2. Stage the deterministic snapshot and durable ledger. Repeating this command
   with the same run and unchanged source is idempotent. A run UUID cannot be
   reused for another project or Maildir UIDVALIDITY.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/stage.json" stage
```

3. Apply bounded transactions. The CLI commits each batch independently, so a
   process or database restart resumes from the durable item status. Failed
   items remain `error` and are not retried in a loop; repair the source/cause
   and run `stage` again before continuing.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/apply.json" apply --batch-size 500
```

4. Acquire the database-authoritative writer gates for every project before
   freezing any project. If closing or validating any later project fails,
   release every gate already acquired and stop without freezing a source.
   This is a real maintenance boundary, not an operator checkbox or a supplied
   JSON assertion. The gate generation and leased service acknowledgements are
   durable PostgreSQL rows. Every API process, worker, external-bridge service,
   and SMTP-ingress boundary must register and heartbeat its own instance before
   acquisition, then observe and acknowledge the exact closed generation. No
   process may acknowledge another class. API mutations, provider ingress, and
   worker mutation loops also take the same project advisory transaction lock as
   gate acquisition, closing the absent-row race. Keep the same gate generation
   live until parity and the separate cutover decision complete.

```shell
GATE_IDS[$PROJECT_ID]=$(python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" \
  writer-gate-close | jq -r .gate_id)
GATE_ID=${GATE_IDS[$PROJECT_ID]:?missing writer gate UUID}
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" \
  writer-gate-status
```

   Do not continue until status shows unexpired acknowledgements for `api`,
   `worker`, `smtp_ingress`, and `external_bridge`. The importer queries these
   authoritative rows itself, captures the source twice only after validation,
   and refuses the freeze if an acknowledgement expires or either capture
   differs.

   The mail VM runs the separate `workspace-smtp-ingress-attester` boundary.
   It registers one stable `smtp_ingress:<hostname>` instance before gate
   acquisition. After observing an expected closed generation it atomically
   writes and directory-fsyncs a hold marker on the persistent mail disk, stops
   `exim4.service`, runs foreground Exim queue deliveries with `exim4 -qff`,
   and requires both `exim4 -bpc` to report zero and `exiwhat` to report no
   active process. It then refreshes its own instance lease and acknowledges
   only the unchanged expected generation. The backend worker cannot create
   this acknowledgement.
   The attester connects as the dedicated `workspace_mail_gate` database role.
   That role can read only the four gate tables and can insert/update only its
   instance and acknowledgement rows; it cannot close, release, or replace a
   gate. Backend bootstrap waits for the role resource before applying the
   migration that grants those privileges.

   A drain timeout, database failure, service restart, expired gate, or
   replaced gate leaves SMTP stopped and the persistent hold marker intact. A
   later gate can only extend the recorded generation set; polling never drops
   an older generation from the hold. The Exim unit has an `ExecStartPre` guard
   that fails closed while either the marker or any live closed gate exists. Do
   not remove the marker manually.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/freeze.json" freeze \
  --gate-id "$GATE_ID"
```

5. While writes remain frozen, stage the final authoritative delta. UIDVALIDITY
   must match the base snapshot. Missing entities previously seen by this run
   become ledger tombstones in reverse dependency order.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/final-delta.json" final-delta
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/final-apply.json" apply --batch-size 500
```

6. Verify canonical counts and normalized row digests, tombstones, retained
   seven-day event suffixes, URN inventory, and read-only binary/sidecar hashes
   in configured object storage. Missing file verification blocks parity.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
  --output "$PROJECT_REPORT_DIR/parity.json" parity
```

7. While the same authoritative legacy freeze is still closed, convert every
   required legacy signed external-bridge outbox operation into the Provider
   HTTP queue. This command reads and verifies the legacy row and signed raw
   record but never updates or deletes either one. It preserves the public
   external-operation UUID, signed record UUID, attempt boundary, idempotency
   hash, and operation payload. A missing/invalid record, unsupported operation,
   ambiguous delivery state, or incomplete queue parity blocks cutover. The
   command requires the exact gate generation and validates the legacy phase,
   including the real stopped/drained `smtp_ingress` acknowledgement.

```shell
python -m workspace.cmd.messenger_migrate \
  --config-file "$CONFIG" --project-id "$PROJECT_ID" \
  --output "$PROJECT_REPORT_DIR/provider-outbox-conversion.json" \
  legacy-provider-outbox-convert --gate-id "$GATE_ID"
```

The report must have `ok: true`, an empty `blockers` list, and equal
`required`/`provider_ready` counts. Repeating the command under the same gate is
idempotent. Public retries after conversion enqueue only through the Provider
HTTP queue; they never append a new SMTP/Maildir record. Legacy outbox tables
remain read-only rollback evidence until a separate post-acceptance cleanup.

`final-delta` must match the machine-verified frozen checkpoint. `parity` must
match the recorded final-delta checkpoint again. Any change in UIDVALIDITY,
source UID/checkpoint, or source digest proves that a writer escaped the gate;
the command fails and cutover remains blocked. Do not release the gate between
these commands.

If the migration is aborted, reopen every acquired generation explicitly before
resuming SMTP. A production multi-project attempt must use the persisted,
status-aware `release_attempt_gates_and_resume_smtp` function from the
[production cutover runbook](postgresql_canonical_production_cutover.md). It
releases only exact still-closed generations recorded by this attempt, proves
all of them open, and then resumes the shared SMTP hold exactly once:

```shell
release_attempt_gates_and_resume_smtp
```

Release only changes the database gates. The helper resumes SMTP exactly once on
the retained mail VM after confirming every project release report and the
absence of another closed gate. It passes one held gate ID; the attester
validates every generation recorded in the persistent hold before removing it.
After the helper succeeds, require `systemctl is-active --quiet exim4.service`
and `exim4 -bpc` to report an active listener and an empty queue.

The resume command verifies every generation recorded in the persistent hold,
removes the marker, and starts Exim through the same database-backed prestart
guard. If start fails, it restores the marker. A missing, expired, deleted, or
replaced generation is intentionally not resumable; an operator must first
reconcile the authoritative gate rows rather than bypass the guard.

Only a parity report with `ok: true`, an empty quarantine, zero
missing/extra/conflicting rows, successful file-object verification, and a
successful provider-outbox conversion report may be used as input to the
separate cutover decision. Maildir event epoch generations are recorded as
source watermarks; PostgreSQL intentionally starts a new generation and retains
only the seven-day suffix, so stale clients recover via the existing typed `410`
and full-snapshot flow.

After all required writer boundaries exist and a complete isolated rehearsal
passes, follow
[`postgresql_canonical_production_cutover.md`](postgresql_canonical_production_cutover.md).
The production workflow must have already published four immutable versions:
a compatibility wrapper with the current backend, unchanged remote-mail runtime,
and new mail root, the
`mail_projection` migration stage that reuses that accepted mail root, the
canonical wrapper, and a prebuilt pre-write rollback wrapper. The compatibility
wrapper keeps gate runtime disabled behind an explicit prestart marker. Stage,
canonical, and rollback replace that marker with an enforced marker and retain
the gate config; only stage and rollback run the attester. The canonical
deployment sets both values below in
the generated backend configuration. The importer never changes them itself:

```ini
[messenger_storage]
mode = postgresql_canonical
canonical_cutover_confirmed = true
```

The element manifest exposes these as `messenger_storage_mode` and
`messenger_canonical_cutover_confirmed`. Both values must be explicit and
consistent. Their defaults remain `mail_projection` and `false`, so an ordinary
backend update cannot perform an implicit cutover. The rendered canonical
backend configuration omits `[messenger_mail]` and backend mail PKI; its config
reload path performs database bootstrap and service restart without mail CA
synchronization or mail readiness. All four runtime entry points (Messenger
API, Workspace API, events, and worker) read the same storage section and do
not construct the Maildir runtime. Public REST and websocket routes remain
unchanged.

Legacy resource retirement is controlled independently by
`retain_legacy_mail_resources`, which defaults to `true`. The first canonical
deployment must leave this default in place. It retains the legacy mail node,
persistent mail disk, mail-side configuration/PKI, gate guard configuration and
credentials,
DNS record, exports, and the old backend data disk for rollback, but canonical
backend services do not depend on them and the attester service is disabled.
The retained enforced guard prevents Exim from restarting while persistent hold
evidence exists. The historical
`mail_migration_cutover_keep_legacy_disk=true` input remains accepted as a
backward-compatible assertion that retention is enabled; it is no longer the
switch that activates canonical storage.

Only after every post-cutover acceptance gate and backup/checksum requirement
passes may a separate deployment set
`retain_legacy_mail_resources=false` while keeping the two canonical inputs
unchanged. The manifest fails closed if removal is requested in stage 1, in
mail-projection mode, without canonical confirmation, or together with the
historical keep-legacy assertion. The final rendered manifest contains no mail
node, DNS record, secret, database role, config, service, export, or reference.
Review the platform resource diff before applying that deployment; this
runbook does not authorize removal by itself.

Canonical steady-state writer-gate validation requires only the remaining
`api`, `worker`, and `external_bridge` writer classes; the legacy freeze and
conversion above still require `smtp_ingress` because they prove the final mail
transport boundary was stopped and drained.

## Interruption and idempotency checks

For `MSG-MIG-004`, run the importer twice and repeat on isolated database
copies while terminating the CLI after approximately 10%, 50%, and 90% of
ledger items. Restart `apply` with the same run UUID. Each completed run must
have the same state digest, the second complete run must report `changed: 0`,
and every checkpoint must advance monotonically. Do not test process termination
or destructive repair against the working team installation.
