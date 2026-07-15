#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.

set -eu
set -o pipefail

PROVIDER_CONFIG="${WORKSPACE_PROVIDER_CONFIG:-/etc/workspace-provider/provider.env}"

if [ ! -s "$PROVIDER_CONFIG" ]; then
    exit 0
fi

set -a
# shellcheck disable=SC1090
. "$PROVIDER_CONFIG"
set +a

: "${WORKSPACE_PROVIDER_BINARY:?WORKSPACE_PROVIDER_BINARY is required}"

pkill -TERM -f "(^|/)${WORKSPACE_PROVIDER_BINARY}([[:space:]]|$)" || true
