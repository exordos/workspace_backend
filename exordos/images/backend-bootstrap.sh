#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

set -eu
set -o pipefail

GC_PATH="${WORKSPACE_PATH:-/opt/workspace}"
GC_CFG_DIR="${WORKSPACE_CONFIG_DIR:-/etc/workspace}"
WORKSPACE_CONFIG="$GC_CFG_DIR/workspace.conf"
RUN_DIR="${WORKSPACE_RUN_DIR:-/run/workspace}"
READY_FILE="$RUN_DIR/bootstrap.ready"
LOCK_FILE="$RUN_DIR/bootstrap.lock"


mkdir -p "$GC_CFG_DIR" "$RUN_DIR"

exec 9>"$LOCK_FILE"
flock -x 9

if [ ! -s "$WORKSPACE_CONFIG" ]; then
    echo "Workspace platform config is not available; deferring bootstrap."
    exit 0
fi

REVISION="$(cat "$WORKSPACE_CONFIG" "$GC_PATH"/migrations/*.py | sha256sum | cut -d ' ' -f 1)"
if [ -f "$READY_FILE" ] && [ "$(cat "$READY_FILE")" = "$REVISION" ]; then
    exit 0
fi
rm -f "$READY_FILE"

# Running units restart through this same bootstrap lock. They cannot reopen
# database writers until the schema migrations have finished.
SERVICE_NAMES=(
    workspace-api
    workspace-messenger-api
    workspace-history-import-worker
    workspace-history-import
    workspace-zulip-realtime
    workspace-zulip-outgoing
    workspace-external-bridge-control
    workspace-external-bridge-api
    workspace-messenger-worker
    workspace-messenger-events
)
old_pids=()
for service_name in "${SERVICE_NAMES[@]}"; do
    pattern="(^|[ /])${service_name}([[:space:]]|$)"
    mapfile -t service_pids < <(pgrep -f "$pattern" || true)
    if [ "${#service_pids[@]}" -gt 0 ]; then
        old_pids+=("${service_pids[@]}")
        kill -TERM "${service_pids[@]}" 2>/dev/null || true
    fi
done
shutdown_deadline=$((SECONDS + 90))
for pid in "${old_pids[@]}"; do
    while kill -0 "$pid" 2>/dev/null; do
        state="$(ps -o stat= -p "$pid" 2>/dev/null || true)"
        [[ "$state" = Z* ]] && break
        if [ "$SECONDS" -ge "$shutdown_deadline" ]; then
            echo "Workspace writers did not stop; refusing schema migration." >&2
            exit 1
        fi
        sleep 0.2
    done
done

mkdir -p "${WORKSPACE_FILES_PATH:-/var/lib/workspace/messenger/files}"

TRACE_ENABLED=0
case "$-" in
    *x*)
        TRACE_ENABLED=1
        set +x
        ;;
esac

eval "$(
    python3 - "$WORKSPACE_CONFIG" <<'PY'
import configparser
import shlex
import sys
import urllib.parse

config = configparser.ConfigParser(interpolation=None)
config.read(sys.argv[1])
url = urllib.parse.urlsplit(config["db"]["connection_url"])
# libpq URL query parameters override authority/path values, including
# Unix-socket hosts and non-default ports used by isolated installations.
parameters = dict(urllib.parse.parse_qsl(
    url.query.replace("+", "%2B"), keep_blank_values=True
))
values = {
    "WORKSPACE_PG_ENDPOINT": parameters.get("host", url.hostname or "localhost"),
    "WORKSPACE_PG_PORT": parameters.get("port", str(url.port or 5432)),
    "WORKSPACE_PG_USER": parameters.get("user", urllib.parse.unquote(url.username or "workspace")),
    "WORKSPACE_PG_PASS": parameters.get("password", urllib.parse.unquote(url.password or "")),
    "WORKSPACE_PG_DB": parameters.get("dbname", urllib.parse.unquote(url.path.lstrip("/") or "workspace")),
}
for key, value in values.items():
    print(f"{key}={shlex.quote(value)}")
PY
)"

attempt=1
until PGPASSWORD="$WORKSPACE_PG_PASS" psql \
    -h "$WORKSPACE_PG_ENDPOINT" \
    -p "$WORKSPACE_PG_PORT" \
    -U "$WORKSPACE_PG_USER" \
    -d "$WORKSPACE_PG_DB" \
    -c "SELECT 1;" >/dev/null 2>&1; do
    echo "Projection database attempt $attempt is not ready; waiting 5 seconds"
    sleep 5
    attempt=$((attempt + 1))
done

source "$GC_PATH/.venv/bin/activate"
ra-apply-migration --config-dir "$GC_CFG_DIR" --path "$GC_PATH/migrations"
deactivate
unset WORKSPACE_PG_PASS

if [[ "$TRACE_ENABLED" -eq 1 ]]; then
    set -x
fi
unset TRACE_ENABLED

printf '%s\n' "$REVISION" > "$READY_FILE"
chmod 0600 "$READY_FILE"

echo "Workspace backend bootstrap completed successfully."
