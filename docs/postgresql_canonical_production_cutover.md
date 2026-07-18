# PostgreSQL-canonical Messenger production cutover

This runbook is the deployment wrapper around
[`postgresql_canonical_messenger_migration.md`](postgresql_canonical_messenger_migration.md).
It keeps the public Messenger REST and websocket contract unchanged while a
mail-projection installation is imported and switched to PostgreSQL-canonical
storage. It does not authorize a fresh installation, destructive restore,
resource recreation, or legacy-resource retirement during the first cutover.

Use the four immutable, uniquely versioned artifacts produced by the
`production_migration` workflow. Never rebuild, replace, or force-push a
recorded version. Before the maintenance window, read back the finalized
runner-local evidence bundle from the private operations archive. It contains
identifiers and must not be uploaded as a GitHub Actions artifact or copied into
a GitHub log or summary. Its durable state must be `published`; `prepared`,
`verified_only`, and `publication_failed` bundles are not deployable. Set real
identifiers and credentials outside shell history according to the local
operations policy.

Save the shell blocks from the setup through the acceptance section in one
private Bash driver and execute that driver as one process. Do not run the
blocks in independent shells: strict mode, functions, arrays, and the exact
gate IDs are deliberately shared across sections. The per-project JSON reports
remain the authoritative restart state if the driver process is interrupted.
To resume after the writer-gate phase begins but before canonical release, run
the setup and function definitions again, call `acquire_all_writer_gates` to
rehydrate the exact IDs, and continue at the interrupted gate phase. Once
`canonical-gate-release-started.json` exists, call only
`rehydrate_attempt_gate_ids` and the status-aware canonical release function;
never invoke abort cleanup or the mail-projection rollback. Do not repeat an
already accepted deployment section merely to reconstruct shell variables.

```shell
set -Eeuo pipefail

REPORT_DIR=<private-operations-report-directory>
CONFIG=/etc/workspace/workspace.conf
PROJECT_IDS=(<first-workspace-project-id> <second-workspace-project-id>)
declare -A RUN_IDS GATE_IDS ATTEMPT_GATE_CLOSED
# Populate one new migration run UUID per project before the maintenance window.
RUN_IDS[<first-workspace-project-id>]=<first-new-migration-run-id>
RUN_IDS[<second-workspace-project-id>]=<second-new-migration-run-id>
WORKSPACE_REPOSITORY_UUID=<exact-private-workspace-repository-uuid>
BRIDGE_REPOSITORY_UUID=<exact-private-bridge-repository-uuid>
COMPATIBILITY_VERSION=<exact-compatibility-version-from-private-build-evidence>
STAGE_VERSION=<exact-stage-version-from-private-build-evidence>
CANONICAL_VERSION=<exact-canonical-version-from-private-build-evidence>
ROLLBACK_VERSION=<exact-prebuilt-rollback-version-from-private-build-evidence>
BRIDGE_VERSION=<exact-provider-bridge-version>
CURRENT_MAIL_IMAGE_URN=urn:images:<current-mail-image>
CURRENT_BACKEND_IMAGE_URN=urn:images:<current-backend-image>
COMPATIBILITY_MAIL_IMAGE_URN=urn:images:<accepted-compatibility-mail-image>
GATE_LEASE_SECONDS=<approved-maintenance-window-seconds>
```

The commands below use the current `exordos` CLI contract: `repo refresh`
starts an asynchronous repository refresh, `repo elements list --dev` exposes
development versions, and `em elements update --version` initiates the upgrade.
The update command does **not** prove that the element has reached a stable
`ACTIVE` state. Before the cutover, verify that the installed CLI exposes these
options and use explicit repository and element polling:

```shell
wait_repo_version_available() {
  repository_uuid=$1
  name=$2
  version=$3
  report_prefix=$4
  REPO_ELEMENT_UUID=
  deadline=$((SECONDS + 1800))
  while ((SECONDS < deadline)); do
    result=$(exordos repo elements list --dev \
      --filters "name=$name" --filters "version=$version" \
      --fields uuid --fields name --fields version \
      --fields repository --fields status \
      --output json)
    exact=$(jq -c --arg name "$name" --arg version "$version" \
      '[.[] | select(.name == $name and .version == $version)]' \
      <<<"$result")
    count=$(jq -r 'length' <<<"$exact")
    if ((count > 1)); then
      printf '%s\n' "$exact" >"${report_prefix}-repository-ambiguous.json"
      printf 'duplicate repository versions: %s %s\n' \
        "$name" "$version" >&2
      return 1
    fi
    if ((count == 1)); then
      actual_repository=$(jq -r '.[0].repository' <<<"$exact")
      if [[ "$actual_repository" != "$repository_uuid" ]]; then
        printf '%s\n' "$exact" >"${report_prefix}-repository-mismatch.json"
        printf 'repository version belongs to unexpected repository\n' >&2
        return 1
      fi
      status=$(jq -r '.[0].status' <<<"$exact")
      if [[ "$status" == "AVAILABLE" ]]; then
        printf '%s\n' "$exact" >"${report_prefix}-repository-available.json"
        REPO_ELEMENT_UUID=$(jq -er '.[0].uuid' <<<"$exact")
        return 0
      fi
      if [[ "$status" == "ERROR" || "$status" == "DISABLED" ]]; then
        printf '%s\n' "$exact" >"${report_prefix}-repository-error.json"
        return 1
      fi
    fi
    sleep 5
  done
  printf 'repository version did not become AVAILABLE: %s %s\n' \
    "$name" "$version" >&2
  return 1
}

verify_repo_element_evidence() {
  repo_element_uuid=$1
  repository_uuid=$2
  name=$3
  version=$4
  expected_manifest=$5
  expected_inventory=$6
  report_prefix=$7
  readback="${report_prefix}-repository-element.json"

  exordos repo elements show "$repo_element_uuid" --output json >"$readback"
  python3 - "$readback" "$repository_uuid" "$repo_element_uuid" \
    "$name" "$version" "$expected_manifest" "$expected_inventory" <<'PY'
import ast
import json
import pathlib
import sys

import yaml

(
    readback_path,
    repository_uuid,
    repo_element_uuid,
    name,
    version,
    manifest_path,
    inventory_path,
) = sys.argv[1:]
rows = json.loads(pathlib.Path(readback_path).read_text(encoding="utf-8"))
if not isinstance(rows, list) or not rows:
    raise SystemExit("repository element readback is not a non-empty CLI table")
actual = {}
for row in rows:
    if not isinstance(row, dict) or set(row) != {"field", "value"}:
        raise SystemExit("repository element readback has an invalid CLI row")
    field = row["field"]
    value = row["value"]
    if not isinstance(field, str) or not isinstance(value, str):
        raise SystemExit("repository element readback has non-string CLI cells")
    if field in actual:
        raise SystemExit("repository element readback has a duplicate field")
    actual[field] = value


def parse_cli_literal(field):
    raw = actual.get(field)
    if raw is None:
        raise SystemExit(f"repository element readback is missing {field}")
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError) as error:
        raise SystemExit(
            f"repository element readback has invalid {field}"
        ) from error
    if not isinstance(value, dict):
        raise SystemExit(f"repository element readback {field} is not an object")
    return value


expected_manifest = yaml.safe_load(
    pathlib.Path(manifest_path).read_text(encoding="utf-8")
)
expected_inventory = json.loads(
    pathlib.Path(inventory_path).read_text(encoding="utf-8")
)

actual_repository = str(actual.get("repository", ""))
if not actual_repository.endswith(repository_uuid):
    raise SystemExit("repository element readback has the wrong repository")
if actual.get("uuid") != repo_element_uuid:
    raise SystemExit("repository element readback UUID mismatch")
if actual.get("name") != name or actual.get("version") != version:
    raise SystemExit("repository element readback identity mismatch")
if actual.get("status") != "AVAILABLE":
    raise SystemExit("repository element readback is not AVAILABLE")
if parse_cli_literal("manifest") != expected_manifest:
    raise SystemExit("repository manifest differs from private evidence")
if parse_cli_literal("inventory") != expected_inventory:
    raise SystemExit("repository inventory differs from private evidence")
PY
}

wait_element_stably_active() {
  name=$1
  version=$2
  report_prefix=$3
  deadline=$((SECONDS + 1800))
  consecutive=0
  while ((SECONDS < deadline)); do
    result=$(exordos em elements list --filters "name=$name" \
      --fields name --fields version --fields status --output json)
    if jq -e --arg name "$name" --arg version "$version" \
      'any(.[]; .name == $name and .version == $version and
        .status == "ERROR")' <<<"$result" >/dev/null; then
      printf '%s\n' "$result" >"${report_prefix}-error.json"
      return 1
    fi
    if jq -e --arg name "$name" --arg version "$version" \
      'any(.[]; .name == $name and .version == $version and
        .status == "ACTIVE")' <<<"$result" >/dev/null; then
      consecutive=$((consecutive + 1))
      if ((consecutive == 3)); then
        printf '%s\n' "$result" >"${report_prefix}-active.json"
        exordos em elements show "$name" \
          >"${report_prefix}-resources.txt"
        return 0
      fi
    else
      consecutive=0
    fi
    sleep 5
  done
  printf 'element did not remain ACTIVE: %s %s\n' "$name" "$version" >&2
  return 1
}

update_element_exact() {
  repository_uuid=$1
  name=$2
  version=$3
  report_prefix=$4
  expected_manifest=${5:-}
  expected_inventory=${6:-}
  exordos repo refresh "$repository_uuid"
  wait_repo_version_available "$repository_uuid" "$name" "$version" \
    "$report_prefix"
  if [[ -n "$expected_manifest" || -n "$expected_inventory" ]]; then
    [[ -n "$expected_manifest" && -n "$expected_inventory" ]]
    verify_repo_element_evidence "$REPO_ELEMENT_UUID" "$repository_uuid" \
      "$name" "$version" "$expected_manifest" "$expected_inventory" \
      "$report_prefix"
  fi
  exordos em elements update "$name" --version "$version" \
    --yes --timeout 1800
  wait_element_stably_active "$name" "$version" "$report_prefix"
}
```

`wait_repo_version_available` intentionally does not filter by repository: the
current CLI lists synchronized elements globally after `repo refresh`. It keeps
all exact name/version matches, requires exactly one, proves that match belongs
to the operator-supplied repository UUID and is `AVAILABLE`, and persists the
JSON list readback before `em elements update`. The exact match UUID is then
passed to `repo elements show UUID --output json`; this GET actualizes lazy
repository content in the current Core contract. Its full manifest and
inventory are persisted and compared structurally with the corresponding
files from private publishing evidence before deployment. A duplicate exact
version, a match from another repository, or any manifest/inventory mismatch
is an immediate stop condition; repository priority must never select a
migration target implicitly. If the installed CLI differs from this contract,
stop and update the runbook for that exact CLI version instead of improvising
during cutover.

Before each deploy, run the installation's approved application-consistent
backup procedure and read the resulting manifest and snapshot back from backup
storage. The backup must contain every disk in the current topology, including
the provider bridge root/data disks and every backend disk. A successful
command exit without manifest and snapshot readback is not backup evidence.

## 1. Deploy the compatibility mail root with the current backend

Capture a baseline of installed element versions, resource identities, disk
targets, image URNs, persistent-volume identities, service health, PostgreSQL
row counts, object-storage inventory, and the current mail image. Stop if the
mail image cannot be identified exactly or the baseline backup is incomplete.

Copy the finalized workflow evidence into the installation's private
operations archive and verify its digest according to the local operations
policy. Set `STAGE_MANIFEST` to the stage manifest in that private evidence
bundle. Prove that the bundle records the expected source commit and tree,
pinned UI commit, CLI version, `COMPATIBILITY_VERSION`, `STAGE_VERSION`,
`CANONICAL_VERSION`, and `ROLLBACK_VERSION`. Parse them as semantic versions
and require strict precedence
`COMPATIBILITY_VERSION < STAGE_VERSION < CANONICAL_VERSION < ROLLBACK_VERSION`;
distinct build metadata alone is not ordering evidence. The bundle must show a
successful publishing run (`publish=true`), durable state `published`, and
completed pushes in the order compatibility, stage, rollback, canonical.
Canonical must have
been published last, after the exact pre-write rollback version was durable.

```shell
COMPATIBILITY_MANIFEST=<private-evidence-compatibility-manifest>
COMPATIBILITY_INVENTORY=<private-evidence-compatibility-inventory>
STAGE_MANIFEST=<private-evidence-stage-manifest>
STAGE_INVENTORY=<private-evidence-stage-inventory>
CANONICAL_MANIFEST=<private-evidence-canonical-manifest>
CANONICAL_INVENTORY=<private-evidence-canonical-inventory>
ROLLBACK_MANIFEST=<private-evidence-rollback-manifest>
ROLLBACK_INVENTORY=<private-evidence-rollback-inventory>

python - "$COMPATIBILITY_VERSION" "$STAGE_VERSION" \
  "$CANONICAL_VERSION" "$ROLLBACK_VERSION" <<'PY'
import re
import sys

pattern = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"-(dev|rc)\+[0-9]{14}\.[0-9a-f]{8}$"
)


def parse(raw):
    match = pattern.fullmatch(raw)
    if match is None:
        raise SystemExit(f"invalid migration version: {raw!r}")
    return tuple(map(int, match.group(1, 2, 3))), match.group(4)


compatibility, stage, canonical, rollback = map(parse, sys.argv[1:])
compatibility_core, compatibility_channel = compatibility
stage_core, stage_channel = stage
canonical_core, canonical_channel = canonical
rollback_core, rollback_channel = rollback
if len(
    {compatibility_channel, stage_channel, canonical_channel, rollback_channel}
) != 1:
    raise SystemExit("migration versions do not use one release channel")
if not (
    compatibility_core[:2]
    == stage_core[:2]
    == canonical_core[:2]
    == rollback_core[:2]
    and stage_core[2] == compatibility_core[2] + 1
    and canonical_core[2] == stage_core[2] + 1
    and rollback_core[2] == canonical_core[2] + 1
):
    raise SystemExit("migration versions do not use consecutive patch bases")
if not compatibility_core < stage_core < canonical_core < rollback_core:
    raise SystemExit("migration version cores are not strictly ordered")
PY
```

Extract the compatibility roots and prove that it preserves the exact current
backend while selecting the mail image built by the compatibility inventory:

```shell
read -r COMPATIBILITY_BACKEND_IMAGE_URN COMPATIBILITY_MAIL_IMAGE_URN < <(
  python - "$COMPATIBILITY_MANIFEST" <<'PY'
import pathlib
import sys

import yaml

manifest = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
nodes = manifest["resources"]["$core.compute.nodes"]
for name in ("workspace_backend", "workspace_mail"):
    disks = nodes[name]["disk_spec"]["disks"]
    print(next(disk["image"] for disk in disks if disk["label"] == "root"), end=" ")
print()
PY
)
test "$COMPATIBILITY_BACKEND_IMAGE_URN" = "$CURRENT_BACKEND_IMAGE_URN"
printf '%s\n' "$COMPATIBILITY_MAIL_IMAGE_URN" \
  >"$REPORT_DIR/workspace-compatibility-mail-image.txt"
```

Inspect the compatibility manifest before deploy. It must remain in
`mail_projection`, preserve every backend and mail persistent disk and resource,
keep the current backend on its remote STARTTLS mail configuration and existing
service identities, and omit the SMTP
writer-gate role, gate config, and attester service. It must contain only the
explicit compatibility prestart marker. Mail bootstrap and reload must require
`mail.conf`, `mail-pki.conf`, and that exact marker, deferring successfully
until all three have arrived. Exim prestart may tolerate an absent gate config
only while that marker is present and no persistent hold exists. Marker
delivery performs the final idempotent reload.

```shell
exordos repo refresh "$WORKSPACE_REPOSITORY_UUID"
wait_repo_version_available "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$COMPATIBILITY_VERSION" "$REPORT_DIR/workspace-compatibility-preflight"
update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$COMPATIBILITY_VERSION" "$REPORT_DIR/workspace-compatibility" \
  "$COMPATIBILITY_MANIFEST" "$COMPATIBILITY_INVENTORY"
```

Read back the exact compatibility version, both root images, and every
persistent disk/resource identity. Verify backend API/realtime/local mail and
the independently initialized mail node. Stop if the backend root changes, a
persistent identity drifts, the compatibility marker is absent, gate runtime is
enabled, or the new mail root is unhealthy. Do not continue until this exact
mail root is accepted.

## 2. Deploy the migration stage in `mail_projection`

Extract and record the exact backend root image selected by that manifest. This
is the only backend image allowed in the later canonical manifest:

```shell
STAGE_BACKEND_IMAGE_URN=$(python - "$STAGE_MANIFEST" <<'PY'
import pathlib
import sys

import yaml

manifest = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
disks = manifest["resources"]["$core.compute.nodes"]["workspace_backend"][
    "disk_spec"
]["disks"]
print(next(disk["image"] for disk in disks if disk["label"] == "root"))
PY
)
printf '%s\n' "$STAGE_BACKEND_IMAGE_URN" \
  >"$REPORT_DIR/workspace-stage-backend-image.txt"
```

Inspect the rendered manifest before deploying. It must contain
`mode = mail_projection`, `canonical_cutover_confirmed = false`, the exact
accepted `COMPATIBILITY_MAIL_IMAGE_URN`, the existing mail node/data disk, and
all existing backend
persistent disks. The stage backend image must differ from an unintended local
or stale image. Independently inspect the canonical and rollback manifests now:
both must select exactly `STAGE_BACKEND_IMAGE_URN`; the canonical manifest must
enable `postgresql_canonical` with explicit confirmation; the rollback manifest
must restore `mail_projection` with confirmation false. Stage, canonical, and
rollback must all select exactly `COMPATIBILITY_MAIL_IMAGE_URN`. The stage must
contain the enforced marker, gate config, and attester service, and the gate
config must have no on-change restart command. Canonical must retain the
enforced marker and gate config while disabling the attester service. All four
versions must retain the same persistent disks, mail config/PKI resources, and
mail DNS identity. Gate config delivery itself must not restart Exim; it must
precede the enforced marker whose reload hook completes bootstrap. This is the
last point at which an absent or incorrect rollback artifact can be fixed
without entering a maintenance freeze.

```shell
exordos repo refresh "$WORKSPACE_REPOSITORY_UUID"
wait_repo_version_available "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$STAGE_VERSION" "$REPORT_DIR/workspace-stage-preflight"
wait_repo_version_available "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$ROLLBACK_VERSION" "$REPORT_DIR/workspace-rollback-preflight"
wait_repo_version_available "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$CANONICAL_VERSION" "$REPORT_DIR/workspace-canonical-preflight"

update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace "$STAGE_VERSION" \
  "$REPORT_DIR/workspace-stage" "$STAGE_MANIFEST" "$STAGE_INVENTORY"
```

Read back the element and compute resources. The installed version and backend
root must match the immutable stage artifact; the mail root must still equal
`COMPATIBILITY_MAIL_IMAGE_URN`; resource, persistent-disk, secret, network, and
export identities must match the baseline. Verify API, realtime, worker, mail,
migration CLI, and backup health while writes still use `mail_projection`.

Stage acceptance must exercise the dedicated SMTP boundary with its production
database identity rather than an owner connection. On the retained mail node,
require the attester and Exim services to be active and run the same fail-closed
prestart check used by Exim:

```shell
ATTESTER_UNIT=$(
  systemctl list-unit-files --type=service --state=enabled --no-legend |
    awk '$1 ~ /^exordos_srv_workspace-smtp-ingress-attester_[0-9a-f-]+\.service$/ {print $1}'
)
test "$(printf '%s\n' "$ATTESTER_UNIT" | awk 'NF' | wc -l)" -eq 1
systemctl is-active --quiet "$ATTESTER_UNIT"
systemctl is-active --quiet exim4.service
sudo /usr/local/bin/workspace-smtp-ingress-attester exim-prestart
```

The check must establish a real `workspace_mail_gate` database connection and
read the authoritative gate rows. A database-level `CONNECT` failure, a table
ACL failure, a stopped Exim service, or a retrying backend mail healthcheck is a
stage failure even if the element briefly reports `ACTIVE`. Immediately return
to the already accepted compatibility version; do not grant privileges by hand
or continue with a partially accepted stage. Publish a new forward migration
and new immutable stage, canonical, and rollback artifacts before retrying.

Stop on any identity drift, mail-image drift, missing enforced guard, missing
disk, new backing chain,
failed service, changed public API contract, or artifact/readback mismatch.

## 3. Deploy the exact Provider-only bridge

Use the separately published immutable Provider bridge version and its private
build evidence. Record its source commit, version, manifest, and SHA-256
inventory in the operations archive:

```shell
BRIDGE_MANIFEST=<private-evidence-provider-bridge-manifest>
```

The rendered bridge manifest must contain the Provider and file HTTP clients
and must not contain SMTP, IMAP, Maildir, or Workspace mail imports:

```shell
grep -q '\[provider_api\]' "$BRIDGE_MANIFEST"
grep -q '\[file_api\]' "$BRIDGE_MANIFEST"
! grep -Eiq 'workspace[_-]mail|maildir|imap_|smtp_' "$BRIDGE_MANIFEST"
```

```shell
update_element_exact "$BRIDGE_REPOSITORY_UUID" workspace_zulip_bridge \
  "$BRIDGE_VERSION" "$REPORT_DIR/provider-bridge"
```

Read back the exact bridge version, root image, node identity, and persistent
data-disk identity. Run the bridge's installed healthcheck and verify Provider
control polling, heartbeat, mTLS, file API, and operation polling without any
mail transport. The subsequent writer-gate close is the authoritative proof
that the live bridge process registered and acknowledged the
`external_bridge` writer boundary; artifact inspection alone is not enough.

If the bridge fails before canonical cutover, suspend provider operations and
deploy a new uniquely versioned wrapper that selects the previously accepted
bridge root while preserving the same node and data disk. Never uninstall the
bridge or delete its data as a rollback mechanism.

## 4. Inventory, stage, and apply while mail remains authoritative

The stage Workspace deployment must remain in `mail_projection` throughout this
section. Enumerate every project mailbox from the retained Maildir before the
maintenance window and put every resulting UUID in `PROJECT_IDS`. A project
that exists in the source but is absent from this array is a cutover blocker.
Create an isolated report directory and migration run for each project:

```shell
for PROJECT_ID in "${PROJECT_IDS[@]}"; do
  PROJECT_REPORT_DIR="$REPORT_DIR/projects/$PROJECT_ID"
  mkdir -p "$PROJECT_REPORT_DIR"
  RUN_ID=${RUN_IDS[$PROJECT_ID]:?missing migration run UUID}

  python -m workspace.cmd.messenger_migrate \
    --config-file "$CONFIG" --project-id "$PROJECT_ID" \
    --output "$PROJECT_REPORT_DIR/inventory.json" inventory

  python -m workspace.cmd.messenger_migrate \
    --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
    --output "$PROJECT_REPORT_DIR/stage.json" stage

  python -m workspace.cmd.messenger_migrate \
    --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
    --output "$PROJECT_REPORT_DIR/apply.json" apply --batch-size 500

  jq -e '.quarantined == []' "$PROJECT_REPORT_DIR/inventory.json"
  jq -e '.ok == true and .remaining == 0' "$PROJECT_REPORT_DIR/apply.json"
done
```

Require an empty inventory quarantine and an apply report with `ok: true` and
`remaining: 0`. Preserve source checkpoint, UIDVALIDITY, source digest,
inventory, URN inventory, event watermarks, ledger counts, and destination
digest. Repair and repeat the documented idempotent stage/apply flow if an item
is in error; do not continue with a partial import.

## 5. Close writers, freeze, apply the final delta, and prove parity

Choose a gate lease that is longer than the approved maintenance window. Do
not start if the remaining lease cannot cover freeze, final apply, parity,
provider conversion, canonical deploy, and read-only cutover checks.

```shell
ATTEMPT_GATE_PROJECTS=()
SMTP_HOLD=/var/lib/workspace/messenger/mail/.writer-gate/smtp-ingress-hold.json

write_gate_status() {
  project_id=$1
  output=$2
  temporary="${output}.tmp"
  rm -f "$temporary"
  python -m workspace.cmd.messenger_migrate \
    --config-file "$CONFIG" --project-id "$project_id" \
    --output "$temporary" writer-gate-status
  mv "$temporary" "$output"
}

exact_gate_state() {
  report=$1
  gate_id=$2
  state=$3
  jq -e --arg gate_id "$gate_id" --arg state "$state" '
    .gate != null and
    (.gate.gate_uuid | tostring) == $gate_id and
    .gate.state == $state
  ' "$report" >/dev/null
}

gate_is_absent() {
  jq -e '.gate == null' "$1" >/dev/null
}

gate_is_open() {
  jq -e '.gate != null and .gate.state == "open"' "$1" >/dev/null
}

release_attempt_gates_and_resume_smtp() {
  resume_gate_id=
  cleanup_failed=false
  for project_id in "${ATTEMPT_GATE_PROJECTS[@]}"; do
    project_report_dir="$REPORT_DIR/projects/$project_id"
    gate_id=${GATE_IDS[$project_id]:?missing writer gate UUID}
    status_report="$project_report_dir/writer-gate-abort-status.json"
    if ! write_gate_status "$project_id" "$status_report"; then
      cleanup_failed=true
      continue
    fi

    if exact_gate_state "$status_report" "$gate_id" closed; then
      temporary="$project_report_dir/writer-gate-abort-release.json.tmp"
      rm -f "$temporary"
      if ! python -m workspace.cmd.messenger_migrate \
        --config-file "$CONFIG" --project-id "$project_id" \
        --output "$temporary" writer-gate-release --gate-id "$gate_id"; then
        cleanup_failed=true
        continue
      fi
      mv "$temporary" \
        "$project_report_dir/writer-gate-abort-release.json"
    elif exact_gate_state "$status_report" "$gate_id" open; then
      :
    elif gate_is_absent "$status_report" && \
      [[ ${ATTEMPT_GATE_CLOSED[$project_id]:-false} == false ]]; then
      :
    else
      printf 'writer gate was replaced for project %s; SMTP remains held\n' \
        "$project_id" >&2
      cleanup_failed=true
      continue
    fi
    resume_gate_id=${resume_gate_id:-$gate_id}
  done

  # Prove every exact generation is open before touching the shared SMTP hold.
  for project_id in "${ATTEMPT_GATE_PROJECTS[@]}"; do
    project_report_dir="$REPORT_DIR/projects/$project_id"
    gate_id=${GATE_IDS[$project_id]:?missing writer gate UUID}
    status_report="$project_report_dir/writer-gate-abort-released-status.json"
    if ! write_gate_status "$project_id" "$status_report"; then
      cleanup_failed=true
      continue
    fi
    if ! exact_gate_state "$status_report" "$gate_id" open && ! \
      { gate_is_absent "$status_report" && \
        [[ ${ATTEMPT_GATE_CLOSED[$project_id]:-false} == false ]]; }; then
      cleanup_failed=true
    fi
  done

  if $cleanup_failed; then
    return 1
  fi

  # One hold covers all project generations. Resume it at most once, only after
  # all exact releases. If no hold was created, Exim must still be active.
  if [[ -n "$resume_gate_id" && -f "$SMTP_HOLD" ]]; then
    sudo /usr/local/bin/workspace-smtp-ingress-attester \
      resume --gate-id "$resume_gate_id"
  else
    systemctl is-active --quiet exim4.service
  fi
}

acquire_all_writer_gates() {
  # Close every project before freezing any one project. Existing reports make
  # this restart-safe within the same migration attempt.
  for project_id in "${PROJECT_IDS[@]}"; do
    project_report_dir="$REPORT_DIR/projects/$project_id"
    close_report="$project_report_dir/writer-gate-close.json"
    intent_report="$project_report_dir/writer-gate-close-intent.json"
    status_report="$project_report_dir/writer-gate-status.json"
    mkdir -p "$project_report_dir"
    had_close_report=false

    if [[ -s "$close_report" ]]; then
      had_close_report=true
      gate_id=$(jq -er .gate_id "$close_report") || return 1
    elif [[ -s "$intent_report" ]]; then
      gate_id=$(jq -er .gate_id "$intent_report") || return 1
    else
      gate_id=$(python -c 'import uuid; print(uuid.uuid4())') || return 1
      temporary="${intent_report}.tmp"
      jq -n --arg project_id "$project_id" --arg gate_id "$gate_id" \
        '{project_id: $project_id, gate_id: $gate_id}' >"$temporary" || \
        return 1
      mv "$temporary" "$intent_report"
    fi

    GATE_IDS[$project_id]=$gate_id
    ATTEMPT_GATE_PROJECTS+=("$project_id")
    write_gate_status "$project_id" "$status_report" || return 1

    if exact_gate_state "$status_report" "$gate_id" closed; then
      ATTEMPT_GATE_CLOSED[$project_id]=true
      if [[ ! -s "$close_report" ]]; then
        cp "$intent_report" "$close_report"
      fi
    elif gate_is_absent "$status_report" || \
      { ! $had_close_report && gate_is_open "$status_report"; }; then
      temporary="${close_report}.tmp"
      rm -f "$temporary"
      python -m workspace.cmd.messenger_migrate \
        --config-file "$CONFIG" --project-id "$project_id" \
        --output "$temporary" writer-gate-close \
        --gate-id "$gate_id" \
        --lease-seconds "$GATE_LEASE_SECONDS" || return 1
      mv "$temporary" "$close_report"
      write_gate_status "$project_id" "$status_report" || return 1
      exact_gate_state "$status_report" "$gate_id" closed || return 1
      ATTEMPT_GATE_CLOSED[$project_id]=true
    else
      printf 'another writer gate exists for project %s\n' "$project_id" >&2
      return 1
    fi
  done
}

rehydrate_attempt_gate_ids() {
  ATTEMPT_GATE_PROJECTS=()
  for project_id in "${PROJECT_IDS[@]}"; do
    project_report_dir="$REPORT_DIR/projects/$project_id"
    close_report="$project_report_dir/writer-gate-close.json"
    intent_report="$project_report_dir/writer-gate-close-intent.json"
    if [[ -s "$close_report" ]]; then
      gate_id=$(jq -er .gate_id "$close_report") || return 1
    else
      gate_id=$(jq -er .gate_id "$intent_report") || return 1
    fi
    GATE_IDS[$project_id]=$gate_id
    ATTEMPT_GATE_PROJECTS+=("$project_id")
  done
}

if [[ -s "$REPORT_DIR/canonical-gate-release-started.json" ]]; then
  printf 'canonical gate release already started; resume only that phase\n' >&2
  false
fi
if ! acquire_all_writer_gates; then
  release_attempt_gates_and_resume_smtp || \
    printf 'automatic gate cleanup failed; SMTP remains held\n' >&2
  false
fi
```

Status must show the same live closed gate and unexpired acknowledgements from
the real `api`, `worker`, `smtp_ingress`, and `external_bridge` instances. Stop
if any expected writer class is absent, stale, duplicated unexpectedly, or
acknowledged by a substitute process.

```shell
freeze_and_verify_all_projects() {
  for PROJECT_ID in "${PROJECT_IDS[@]}"; do
    PROJECT_REPORT_DIR="$REPORT_DIR/projects/$PROJECT_ID"
    RUN_ID=${RUN_IDS[$PROJECT_ID]:?missing migration run UUID}
    GATE_ID=${GATE_IDS[$PROJECT_ID]:?missing writer gate UUID}

    python -m workspace.cmd.messenger_migrate \
      --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
      --output "$PROJECT_REPORT_DIR/freeze.json" freeze \
      --gate-id "$GATE_ID" || return 1

    python -m workspace.cmd.messenger_migrate \
      --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
      --output "$PROJECT_REPORT_DIR/final-delta.json" final-delta || return 1

    python -m workspace.cmd.messenger_migrate \
      --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
      --output "$PROJECT_REPORT_DIR/final-apply.json" apply \
      --batch-size 500 || return 1

    python -m workspace.cmd.messenger_migrate \
      --config-file "$CONFIG" --project-id "$PROJECT_ID" --run-id "$RUN_ID" \
      --output "$PROJECT_REPORT_DIR/parity.json" parity || return 1

    python -m workspace.cmd.messenger_migrate \
      --config-file "$CONFIG" --project-id "$PROJECT_ID" \
      --output "$PROJECT_REPORT_DIR/provider-outbox-conversion.json" \
      legacy-provider-outbox-convert --gate-id "$GATE_ID" || return 1

    jq -e '.ok == true and .remaining == 0' \
      "$PROJECT_REPORT_DIR/final-apply.json" || return 1
    jq -e '
      .ok == true and
      .missing == [] and .extra == [] and .conflicting == [] and
      .quarantined == 0 and .file_objects.ok == true
    ' "$PROJECT_REPORT_DIR/parity.json" || return 1
    jq -e '
      .ok == true and .blockers == [] and
      .required == .provider_ready
    ' "$PROJECT_REPORT_DIR/provider-outbox-conversion.json" || return 1
  done
}

if ! freeze_and_verify_all_projects; then
  release_attempt_gates_and_resume_smtp || \
    printf 'automatic gate cleanup failed; SMTP remains held\n' >&2
  false
fi
```

Keep every exact gate closed. A changed source checkpoint, UIDVALIDITY, source
digest, parity digest, file-object failure, quarantine entry, gate expiry, or
provider conversion blocker ends the cutover attempt.

## 6. Deploy the exact published canonical version

Before deploy, prove again from `CANONICAL_MANIFEST` in the private build
evidence that the backend root is
exactly `STAGE_BACKEND_IMAGE_URN`, the mail root is exactly
`COMPATIBILITY_MAIL_IMAGE_URN`, canonical mode and confirmation are both explicit,
and `retain_legacy_mail_resources` rendered the existing mail node/data disk.
The canonical backend configuration must contain no `[messenger_mail]` section.
The retained mail node must keep the enforced writer-gate marker and gate
configuration plus ordinary mail config/PKI and DNS identities, without the
attester or other mail runtime services and without a gate-config on-change
Exim restart. This guard is required so a retained-mail reboot cannot restart
SMTP after the database gate has been released while the persistent hold
remains.
Prove that the repository readback for `CANONICAL_VERSION` is the same immutable
manifest recorded by the publishing workflow. Do not build a wrapper during the
maintenance window.

Run the approved backup precheck again. Do not deploy if the backup topology
does not include the canonical backend root/data/control disks plus both bridge
disks and the retained mail disks.

```shell
update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$CANONICAL_VERSION" "$REPORT_DIR/workspace-canonical" \
  "$CANONICAL_MANIFEST" "$CANONICAL_INVENTORY"
```

The Workspace element and persistent resource identities must remain stable.
Read back the exact cutover version, the unchanged stage backend root, retained
mail root/data disk, backend data/control disks, services, config, secrets, and
exports. The writer gate must still be closed; SMTP must remain stopped and
drained. Do not release the gate merely because the element is `ACTIVE`.

## 7. Accept the cutover and cross the first-write boundary deliberately

While the gate is closed, perform read-only acceptance first:

- API/OpenAPI and websocket contracts are unchanged;
- imported resources, memberships, drafts, unread state, events, files, URNs,
  and provider projections match the accepted parity report;
- PostgreSQL, S3, realtime, worker, Provider API, and bridge health pass;
- the bridge has no SMTP/IMAP traffic and provider queues are consistent;
- a complete canonical-topology backup and exact snapshot readback pass;
- the retained mail node/data disk and persistent SMTP hold remain unchanged.

If read-only acceptance fails, the first canonical write has not occurred. Use
the prebuilt rollback version below while the exact gate remains closed; do not
resume writers first.

To accept the cutover, release every acquired database gate. Do **not**
run the SMTP-ingress `resume` command on a successful canonical cutover:

```shell
CANONICAL_RELEASE_STARTED=$REPORT_DIR/canonical-gate-release-started.json
FIRST_CANONICAL_RELEASE=$REPORT_DIR/first-canonical-gate-release.json

if [[ ! -s "$CANONICAL_RELEASE_STARTED" ]]; then
  temporary="${CANONICAL_RELEASE_STARTED}.tmp"
  jq -n '{state: "release_started"}' >"$temporary"
  mv "$temporary" "$CANONICAL_RELEASE_STARTED"
fi

release_all_canonical_gates() {
  for project_id in "${PROJECT_IDS[@]}"; do
    project_report_dir="$REPORT_DIR/projects/$project_id"
    gate_id=${GATE_IDS[$project_id]:?missing writer gate UUID}
    status_report="$project_report_dir/writer-gate-release-status.json"
    write_gate_status "$project_id" "$status_report" || return 1

    if exact_gate_state "$status_report" "$gate_id" closed; then
      temporary="$project_report_dir/writer-gate-release.json.tmp"
      rm -f "$temporary"
      python -m workspace.cmd.messenger_migrate \
        --config-file "$CONFIG" --project-id "$project_id" \
        --output "$temporary" writer-gate-release --gate-id "$gate_id" || \
        return 1
      mv "$temporary" "$project_report_dir/writer-gate-release.json"
    elif ! exact_gate_state "$status_report" "$gate_id" open; then
      printf 'writer gate was replaced for project %s\n' "$project_id" >&2
      return 1
    fi

    # The database state is authoritative. This marker makes the operator's
    # irreversible boundary explicit and is recreated after a process restart
    # when status already shows the exact gate open.
    if [[ ! -s "$FIRST_CANONICAL_RELEASE" ]]; then
      temporary="${FIRST_CANONICAL_RELEASE}.tmp"
      jq -n --arg project_id "$project_id" --arg gate_id "$gate_id" \
        '{project_id: $project_id, gate_id: $gate_id}' >"$temporary"
      mv "$temporary" "$FIRST_CANONICAL_RELEASE"
    fi

    released_status="$project_report_dir/writer-gate-released-status.json"
    write_gate_status "$project_id" "$released_status" || return 1
    exact_gate_state "$released_status" "$gate_id" open || return 1
  done
}

if ! release_all_canonical_gates; then
  printf 'partial canonical gate release; rerun this status-aware release, ' >&2
  printf 'and never deploy the mail-projection rollback\n' >&2
  false
fi
```

The first successfully released project gate is the deliberate no-return
decision, even if a later project release fails: another canonical writer may
commit before an operator can issue a canary. Rerun the status-aware release
function to finish only the remaining exact closed gates; never deploy the
mail-projection rollback after the first release. Record the release
time and PostgreSQL event watermark, verify the exact gate is released, verify
that SMTP remains inactive behind its persistent hold, and immediately perform
one controlled canonical canary mutation. Read it back through REST, websocket,
PostgreSQL, and the provider path when applicable. Record the earliest observed
post-release canonical mutation as the **first canonical write**. Only then run
the full write/UI/realtime/provider acceptance suite and a second
application-consistent backup.

### Rollback boundary

**Before gate release and before the first canonical write:** deploy the exact
prebuilt `ROLLBACK_VERSION`. Its privately archived manifest must select
`STAGE_BACKEND_IMAGE_URN`, restore `mail_projection` with canonical confirmation
false, pin `COMPATIBILITY_MAIL_IMAGE_URN`, and retain every legacy resource and
persistent disk:

```shell
update_element_exact "$WORKSPACE_REPOSITORY_UUID" workspace \
  "$ROLLBACK_VERSION" "$REPORT_DIR/workspace-pre-write-rollback" \
  "$ROLLBACK_MANIFEST" "$ROLLBACK_INVENTORY"
```

Read back all identities and services, prove that the original source remains
authoritative, and verify that rollback bootstrap configured Dovecot and passed
IMAP health while intentionally leaving Exim inactive under the validated
persistent hold. Run `release_attempt_gates_and_resume_smtp` to release every
exact gate from this attempt and resume the shared SMTP hold once; that exact
resume is the action that removes the hold and starts Exim. Never bypass the
persistent hold or Exim prestart guard. A missing, unpublished, mismatched, or non-`AVAILABLE`
rollback version is a pre-maintenance stop condition, not a reason to build one
under freeze.

**After gate release or the first canonical write unless absence of all writes
is proved authoritatively:** never switch back to `mail_projection` and never
resume SMTP from the retained Maildir. It is now potentially stale. Fix forward
in canonical mode or perform an explicitly reviewed reverse reconciliation
under a new writer freeze. An image rollback alone cannot restore data
consistency.

## 8. Retire legacy mail resources in a later deployment

Retirement is a separate maintenance change, never part of first-cutover
acceptance. It requires accepted post-write backup/readback, the complete test
suite, the elapsed rollback policy window, and an approved platform resource
diff proving that only the intended legacy mail resources and legacy backend
data disk are removed.

Reuse the accepted stage backend image and keep canonical mode explicit:

```shell
RETIREMENT_SOURCE=<workspace-backend-source-directory>
RETIREMENT_ARTIFACT_DIR=<new-empty-retirement-artifact-directory>
exordos build "$RETIREMENT_SOURCE" --output-dir "$RETIREMENT_ARTIFACT_DIR" \
  --manifest-var workspace_backend_image="$STAGE_BACKEND_IMAGE_URN" \
  --manifest-var messenger_storage_mode=postgresql_canonical \
  --manifest-var messenger_canonical_cutover_confirmed=true \
  --manifest-var retain_legacy_mail_resources=false
```

Review the rendered resource deletion set and create a new backup before the
retirement deploy. Apply it only under separate operator approval, then repeat
API, realtime, provider, backup, resource-identity, and load acceptance. This
runbook does not authorize manual VM, disk, secret, DNS, or database-role
deletion.

## Required evidence and global stop conditions

The private cutover bundle must contain:

- baseline and every post-deploy element/resource readback;
- backup manifest, snapshot identifier, exact file-set readback, and restore
  scope for baseline, canonical pre-write, and canonical post-write states;
- immutable SHA-256 inventories for compatibility, stage, canonical, prebuilt rollback,
  bridge, and any later retirement artifacts used;
- source commit/tree, pinned UI commit, CLI version, exact four-version
  mapping, and complete publication results from the finalized private
  runner-local workflow evidence;
- exact current backend/mail, compatibility mail, stage backend, bridge, and
  installed image URNs;
- `inventory.json`, `stage.json`, `apply.json`, `freeze.json`,
  `final-delta.json`, `final-apply.json`, `parity.json`, writer-gate reports,
  and `provider-outbox-conversion.json`;
- service, API, websocket, provider, file, UI, and load acceptance summaries.

Stop immediately on a reused artifact version, changed artifact digest,
unexpected resource identity or disk mapping, unpinned image, incomplete
backup, active backing chain, writer-gate expiry, source movement under freeze,
non-empty quarantine, parity mismatch, provider conversion blocker, public API
drift, unhealthy service, missing private workflow evidence, unavailable
prebuilt rollback version, partial four-version publication, or any attempt to
recreate/uninstall the installation. Do not use force flags to overwrite
repository artifacts or deployments.
