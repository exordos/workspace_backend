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
PKI_CONFIG=${PKI_CONFIG:-/etc/workspace/mail-pki.conf}
READY_FILE=${READY_FILE:-/run/workspace/bootstrap.ready}
TLS_CA=${WORKSPACE_MAIL_CA_FILE:-/etc/workspace/tls/workspace-mail-ca.crt}
CA_SYNC=${WORKSPACE_MAIL_CA_SYNC:-/usr/local/bin/workspace-mail-ca-sync}
MAIL_HEALTHCHECK=${WORKSPACE_MAIL_HEALTHCHECK:-/usr/local/bin/workspace-mail-healthcheck}
WORKSPACE_BOOTSTRAP=${WORKSPACE_BOOTSTRAP:-/usr/local/bin/workspace-bootstrap}
RESTART_SERVICES=${WORKSPACE_RESTART_SERVICES:-/usr/local/bin/workspace-restart-services}

if [ ! -s "$WORKSPACE_CONFIG" ]; then
    exit 0
fi

STARTTLS_REQUIRED=0
if grep -Eq '^[[:space:]]*(smtp|imap)_security[[:space:]]*=[[:space:]]*starttls[[:space:]]*$' \
    "$WORKSPACE_CONFIG"; then
    STARTTLS_REQUIRED=1
fi

if [ "$STARTTLS_REQUIRED" -eq 1 ] && [ ! -s "$PKI_CONFIG" ]; then
    echo "Workspace mail PKI configuration is incomplete; deferring reload."
    exit 0
fi

if [ -s "$PKI_CONFIG" ]; then
    "$CA_SYNC" "$PKI_CONFIG"
fi

if [ "$STARTTLS_REQUIRED" -eq 1 ] && [ ! -s "$TLS_CA" ]; then
    echo "Workspace mail CA synchronization did not produce a CA file." >&2
    exit 1
fi

rm -f "$READY_FILE"

attempt=1
until "$MAIL_HEALTHCHECK" "$WORKSPACE_CONFIG"; do
    echo "Workspace mail service attempt $attempt is not ready; waiting 5 seconds"
    sleep 5
    attempt=$((attempt + 1))
done

"$WORKSPACE_BOOTSTRAP"
"$RESTART_SERVICES"
