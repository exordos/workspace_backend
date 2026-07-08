#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation
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

WORKSPACE_BINARIES=(
    workspace-user-api
    workspace-messenger-api
    workspace-messenger-worker
    workspace-integration-bridge-worker
    workspace-messenger-events
)
WORKSPACE_HELPERS=(
    backend-bootstrap.sh:workspace-bootstrap
    workspace-nginx-reload.sh:workspace-nginx-reload
    workspace-reload-config.sh:workspace-reload-config
    workspace-restart-services.sh:workspace-restart-services
    workspace-wait-ready.sh:workspace-wait-ready
)

sudo apt update
sudo apt install -y \
    libev-dev \
    nginx \
    postgresql-client \
    procps

sudo mkdir -p \
    "$GC_CFG_DIR" \
    /etc/nginx/conf.d \
    /etc/nginx/sites-available \
    /etc/nginx/sites-enabled \
    /etc/nginx/workspace.d \
    /var/lib/workspace/messenger/files \
    /usr/local/bin
sudo cp "$GC_PATH/etc/workspace/logging.yaml" "$GC_CFG_DIR/"

cd "$GC_PATH"
uv sync --locked --no-dev

for binary in "${WORKSPACE_BINARIES[@]}"; do
    sudo ln -sf "$VENV_PATH/bin/$binary" "/usr/bin/$binary"
done

for helper in "${WORKSPACE_HELPERS[@]}"; do
    src="${helper%%:*}"
    dst="${helper##*:}"
    sudo install -m 0755 "$GC_PATH/exordos/images/$src" "/usr/local/bin/$dst"
done

sudo rm -f /etc/nginx/sites-enabled/default
