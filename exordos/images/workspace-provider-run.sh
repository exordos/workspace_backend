#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.

set -eu
set -o pipefail

WORKSPACE_PATH="/opt/workspace"
PROVIDER_CONFIG="${WORKSPACE_PROVIDER_CONFIG:-/etc/workspace-provider/provider.env}"

while [ ! -s "$PROVIDER_CONFIG" ]; do
    echo "Waiting for provider configuration..."
    sleep 1
done

set -a
# shellcheck disable=SC1090
. "$PROVIDER_CONFIG"
set +a

BINARY="${1:-${WORKSPACE_PROVIDER_BINARY:-}}"
: "${BINARY:?provider binary is required}"

exec "$WORKSPACE_PATH/.venv/bin/$BINARY"
