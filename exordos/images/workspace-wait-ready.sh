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

READY_FILE="/run/workspace/bootstrap.ready"
TIMEOUT="${WORKSPACE_BOOTSTRAP_WAIT_TIMEOUT:-900}"

deadline=$((SECONDS + TIMEOUT))
while [ ! -f "$READY_FILE" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "Workspace bootstrap did not become ready within ${TIMEOUT}s" >&2
        exit 1
    fi
    sleep 1
done

if [ "$#" -gt 0 ]; then
    exec "$@"
fi
