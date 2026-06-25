#!/usr/bin/env bash

# Copyright 2025 Genesis Corporation
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
set -x
set -o pipefail


GC_PATH="/opt/workspace"
GC_CFG_DIR=/etc/workspace
VENV_PATH="$GC_PATH/.venv"
BOOTSTRAP_PATH="/var/lib/genesis/bootstrap/scripts"

GC_PG_USER="workspace"
GC_PG_PASS="pass"
GC_PG_DB="workspace"

SYSTEMD_SERVICE_DIR=/etc/systemd/system/
WORKSPACE_BINARIES=(
    workspace-user-api
    workspace-messenger-api
    workspace-messenger-worker
    workspace-messenger-events
)
WORKSPACE_SERVICES=(
    workspace-user-api.service
    workspace-messenger-api.service
    workspace-messenger-worker.service
    workspace-messenger-events.service
)

# Install packages
sudo apt update
sudo apt dist-upgrade -y
sudo apt install -y \
    postgresql \
    libev-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME"/.local/bin/env

# Default creds for workspace services
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$GC_PG_USER'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE ROLE $GC_PG_USER WITH LOGIN PASSWORD '$GC_PG_PASS';"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$GC_PG_DB'" | grep -q 1; then
    sudo -u postgres createdb -O "$GC_PG_USER" "$GC_PG_DB"
fi

# Install genesis core
sudo mkdir -p "$GC_CFG_DIR" "$BOOTSTRAP_PATH" "$SYSTEMD_SERVICE_DIR"
sudo cp "$GC_PATH/etc/workspace/workspace.conf" "$GC_CFG_DIR/"
sudo cp "$GC_PATH/etc/workspace/logging.yaml" "$GC_CFG_DIR/"
sudo cp "$GC_PATH/genesis/images/bootstrap.sh" "$BOOTSTRAP_PATH/0100-gc-bootstrap.sh"

cd "$GC_PATH"
uv sync
source "$GC_PATH"/.venv/bin/activate

# Apply migrations
ra-apply-migration --config-dir "$GC_PATH/etc/workspace/" --path "$GC_PATH/migrations"
deactivate

# Create links to venv
for binary in "${WORKSPACE_BINARIES[@]}"; do
    sudo ln -sf "$VENV_PATH/bin/$binary" "/usr/bin/$binary"
done

# Install Systemd service files
for service in "${WORKSPACE_SERVICES[@]}"; do
    sudo cp "$GC_PATH/etc/systemd/$service" "$SYSTEMD_SERVICE_DIR"
done
sudo systemctl daemon-reload

# Enable workspace services
for service in "${WORKSPACE_SERVICES[@]}"; do
    sudo systemctl enable "$service"
done
