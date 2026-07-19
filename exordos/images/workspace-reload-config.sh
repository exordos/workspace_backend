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

WORKSPACE_CONFIG=${WORKSPACE_CONFIG:-/etc/workspace/workspace.conf}
READY_FILE=${READY_FILE:-/run/workspace/bootstrap.ready}
WORKSPACE_BOOTSTRAP=${WORKSPACE_BOOTSTRAP:-/usr/local/bin/workspace-bootstrap}
RESTART_SERVICES=${WORKSPACE_RESTART_SERVICES:-/usr/local/bin/workspace-restart-services}

if [ ! -s "$WORKSPACE_CONFIG" ]; then
    exit 0
fi

rm -f "$READY_FILE"
"$WORKSPACE_BOOTSTRAP"
"$RESTART_SERVICES"
