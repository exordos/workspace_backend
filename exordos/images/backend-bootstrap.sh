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

GC_PATH="/opt/workspace"
GC_CFG_DIR="/etc/workspace"
WORKSPACE_CONFIG="$GC_CFG_DIR/workspace.conf"
SMTP_GATE_ROLE_CONFIG="$GC_CFG_DIR/smtp-writer-gate-role.conf"
RUN_DIR="/run/workspace"
READY_FILE="$RUN_DIR/bootstrap.ready"
LOCK_FILE="$RUN_DIR/bootstrap.lock"


mkdir -p "$GC_CFG_DIR" "$RUN_DIR"

exec 9>"$LOCK_FILE"
flock -x 9

if [ ! -s "$WORKSPACE_CONFIG" ]; then
    echo "Workspace platform config is not available; deferring bootstrap."
    exit 0
fi

MESSENGER_STORAGE_MODE="$(
    python3 - "$WORKSPACE_CONFIG" <<'PY'
import configparser
import sys

config = configparser.ConfigParser()
config.read(sys.argv[1])
print(config.get("messenger_storage", "mode", fallback="mail_projection"))
PY
)"
if [ "$MESSENGER_STORAGE_MODE" != "postgresql_canonical" ] && \
        [ ! -s "$SMTP_GATE_ROLE_CONFIG" ]; then
    echo "Workspace SMTP gate role config is not available; deferring bootstrap."
    exit 0
fi

SMTP_GATE_ROLE=""
if [ "$MESSENGER_STORAGE_MODE" != "postgresql_canonical" ]; then
    SMTP_GATE_ROLE="$(
        python3 - "$SMTP_GATE_ROLE_CONFIG" <<'PY'
import configparser
import sys

config = configparser.ConfigParser()
config.read(sys.argv[1])
print(config["smtp_writer_gate_role"]["name"])
PY
    )"
    if [ "$SMTP_GATE_ROLE" != "workspace_mail_gate" ]; then
        echo "Workspace SMTP gate role config has an unsupported role name." >&2
        exit 1
    fi
fi

mkdir -p /var/lib/workspace/messenger/files

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

config = configparser.ConfigParser()
config.read(sys.argv[1])
url = urllib.parse.urlsplit(config["db"]["connection_url"])
values = {
    "WORKSPACE_PG_ENDPOINT": url.hostname or "localhost",
    "WORKSPACE_PG_PORT": str(url.port or 5432),
    "WORKSPACE_PG_USER": urllib.parse.unquote(url.username or "workspace"),
    "WORKSPACE_PG_PASS": urllib.parse.unquote(url.password or ""),
    "WORKSPACE_PG_DB": urllib.parse.unquote(url.path.lstrip("/") or "workspace"),
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

if [ -n "$SMTP_GATE_ROLE" ]; then
    attempt=1
    until [ "$(
        PGPASSWORD="$WORKSPACE_PG_PASS" psql \
            -h "$WORKSPACE_PG_ENDPOINT" \
            -p "$WORKSPACE_PG_PORT" \
            -U "$WORKSPACE_PG_USER" \
            -d "$WORKSPACE_PG_DB" \
            -Atc "SELECT 1 FROM pg_roles WHERE rolname = 'workspace_mail_gate';" \
            2>/dev/null
    )" = "1" ]; do
        echo "SMTP writer-gate database role attempt $attempt is not ready; waiting 5 seconds"
        sleep 5
        attempt=$((attempt + 1))
    done
fi

source "$GC_PATH/.venv/bin/activate"
ra-apply-migration --config-dir "$GC_CFG_DIR" --path "$GC_PATH/migrations"
deactivate
unset WORKSPACE_PG_PASS

if [[ "$TRACE_ENABLED" -eq 1 ]]; then
    set -x
fi
unset TRACE_ENABLED

touch "$READY_FILE"

echo "Workspace backend bootstrap completed successfully."
