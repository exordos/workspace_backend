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

CHECK_ONLY=0
if [ "${1:-}" = "--check-only" ]; then
    CHECK_ONLY=1
fi

NGINX_CONFIG="/etc/nginx/conf.d/workspace.conf"
CORE_UPSTREAM="/etc/nginx/workspace.d/core-upstream.conf"

wait_for_nginx_configs() {
    while [ ! -s "$NGINX_CONFIG" ] || [ ! -s "$CORE_UPSTREAM" ]; do
        echo "Waiting for workspace nginx platform configs..."
        sleep 1
    done
}

if [ "$CHECK_ONLY" -eq 1 ]; then
    wait_for_nginx_configs
else
    if [ ! -s "$NGINX_CONFIG" ] || [ ! -s "$CORE_UPSTREAM" ]; then
        exit 0
    fi
fi

rm -f /etc/nginx/sites-enabled/default
nginx -t

if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 0
fi

if [ -s /run/nginx.pid ]; then
    nginx -s reload
    exit 0
fi

for unit_path in /etc/systemd/system/exordos_srv_workspace-nginx_*.service; do
    [ -e "$unit_path" ] || continue
    systemctl restart "$(basename "$unit_path")"
    exit 0
done
