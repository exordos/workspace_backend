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
UI_PATH="/opt/workspace-ui"

WORKSPACE_BINARIES=(
    workspace-api
    workspace-messenger-api
    workspace-messenger-worker
    workspace-messenger-events
)
WORKSPACE_HELPERS=(
    backend-bootstrap.sh:workspace-bootstrap
    workspace-mail-healthcheck.py:workspace-mail-healthcheck
    workspace-nginx-reload.sh:workspace-nginx-reload
    workspace-reload-config.sh:workspace-reload-config
    workspace-restart-services.sh:workspace-restart-services
    workspace-wait-ready.sh:workspace-wait-ready
)

disable_packaged_nginx_service() {
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl disable --now nginx.service >/dev/null 2>&1 || true
        sudo systemctl reset-failed nginx.service >/dev/null 2>&1 || true
    fi

    sudo rm -f /etc/systemd/system/multi-user.target.wants/nginx.service
}

sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
    ca-certificates \
    curl \
    libev-dev \
    nginx \
    openssh-server \
    postgresql-client \
    procps

sudo systemctl enable ssh.service

node_major=0
if command -v node >/dev/null 2>&1; then
    node_major=$(node -p 'Number(process.versions.node.split(".")[0])')
fi
if (( node_major < 22 )); then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    sudo DEBIAN_FRONTEND=noninteractive apt install -y nodejs
fi

if ! getent group workspace >/dev/null; then
    sudo groupadd --system workspace
fi
if ! getent passwd workspace >/dev/null; then
    sudo useradd \
        --system \
        --gid workspace \
        --home-dir /var/lib/workspace \
        --shell /usr/sbin/nologin \
        workspace
fi

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

cd "$UI_PATH"
npm ci --include=dev
VITE_MESSENGER_ONLY=true npm run build --workspace=web
test -s "$UI_PATH/packages/web/dist/index.html"

for binary in "${WORKSPACE_BINARIES[@]}"; do
    sudo ln -sf "$VENV_PATH/bin/$binary" "/usr/bin/$binary"
done

for helper in "${WORKSPACE_HELPERS[@]}"; do
    src="${helper%%:*}"
    dst="${helper##*:}"
    sudo install -m 0755 "$GC_PATH/exordos/images/$src" "/usr/local/bin/$dst"
done

sudo rm -f /etc/nginx/sites-enabled/default
disable_packaged_nginx_service
