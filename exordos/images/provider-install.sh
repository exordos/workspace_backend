#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.

set -eu
set -x
set -o pipefail

WORKSPACE_PATH="/opt/workspace"
PROVIDER_CONFIG_DIR="/etc/workspace-provider"

sudo apt update
sudo apt install -y libev-dev postgresql-client procps

sudo mkdir -p "$PROVIDER_CONFIG_DIR" /usr/local/bin

cd "$WORKSPACE_PATH"
uv sync --locked --no-dev

sudo install -m 0755 \
    "$WORKSPACE_PATH/exordos/images/workspace-provider-run.sh" \
    /usr/local/bin/workspace-provider-run
sudo install -m 0755 \
    "$WORKSPACE_PATH/exordos/images/workspace-provider-reload.sh" \
    /usr/local/bin/workspace-provider-reload
