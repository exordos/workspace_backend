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
RUN_DIR="/run/workspace"
READY_FILE="$RUN_DIR/bootstrap.ready"
LOCK_FILE="$RUN_DIR/bootstrap.lock"
WORKSPACE_DATA_DIR="/var/lib/workspace"


mkdir -p "$GC_CFG_DIR" "$RUN_DIR"

exec 9>"$LOCK_FILE"
flock -x 9

while [ ! -s "$WORKSPACE_CONFIG" ]; do
    echo "Waiting for workspace platform configs..."
    sleep 1
done

mkdir -p "$WORKSPACE_DATA_DIR/messenger/files"

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

wait_for_db() {
    local attempt=1

    echo "Waiting for workspace database..."
    while true; do
        if PGPASSWORD="$WORKSPACE_PG_PASS" psql \
            -h "$WORKSPACE_PG_ENDPOINT" \
            -p "$WORKSPACE_PG_PORT" \
            -U "$WORKSPACE_PG_USER" \
            -d "$WORKSPACE_PG_DB" \
            -c "SELECT 1;" >/dev/null 2>&1; then
            echo "Database is available after $attempt attempts"
            return 0
        fi

        echo "Attempt $attempt: database is not ready, waiting 5 seconds..."
        sleep 5
        attempt=$((attempt + 1))
    done
}

wait_for_db

source "$GC_PATH/.venv/bin/activate"
ra-apply-migration --config-dir "$GC_CFG_DIR" --path "$GC_PATH/migrations"
deactivate

touch "$READY_FILE"

echo "Workspace backend bootstrap completed successfully."
