#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

source "${EXORDOS_BOOTSTRAP_LIB:-/usr/local/lib/exordos/lib_bootstrap.sh}"
source "${WORKSPACE_MAIL_READINESS_LIB:-/usr/local/lib/workspace/mail-runtime-readiness.sh}"

WORKSPACE_CONFIG=${WORKSPACE_CONFIG:-/etc/workspace/mail.conf}
PKI_CONFIG=${PKI_CONFIG:-/etc/workspace/mail-pki.conf}
COMPATIBILITY_MARKER=${COMPATIBILITY_MARKER:-/etc/workspace/smtp-writer-gate.compatibility}
ENFORCED_MARKER=${ENFORCED_MARKER:-/etc/workspace/smtp-writer-gate.enforced}
WRITER_GATE_CONFIG=${WRITER_GATE_CONFIG:-/etc/workspace/smtp-writer-gate.conf}
SMTP_WRITER_GATE_HOLD=${SMTP_WRITER_GATE_HOLD:-/var/lib/workspace/messenger/mail/.writer-gate/smtp-ingress-hold.json}
TLS_BUNDLE=${TLS_BUNDLE:-/etc/workspace/tls/workspace-mail.pem}
TLS_CA=${TLS_CA:-/etc/workspace/tls/workspace-mail-ca.crt}
TLS_OUTPUT_DIR=${TLS_OUTPUT_DIR:-/etc/workspace/tls}
TLS_STORE_RELATIVE=${TLS_STORE_RELATIVE:-workspace-mail-pki}
RUN_DIR=${RUN_DIR:-/run/workspace-mail}
LOCK_FILE="$RUN_DIR/bootstrap.lock"
WORKSPACE_MAIL_DIR=${WORKSPACE_MAIL_DIR:-/var/lib/workspace/messenger/mail}
MAIL_PKI_BIN=${MAIL_PKI_BIN:-/usr/local/bin/workspace-mail-pki}
MAIL_CONFIGURE_BIN=${MAIL_CONFIGURE_BIN:-/usr/local/bin/workspace-mail-configure}
MAIL_HEALTHCHECK_BIN=${MAIL_HEALTHCHECK_BIN:-/usr/local/bin/workspace-mail-healthcheck}

mkdir -p "$RUN_DIR" "$(dirname "$WORKSPACE_CONFIG")"
exec 9>"$LOCK_FILE"
flock -x 9

if [ ! -s "$WORKSPACE_CONFIG" ] || [ ! -s "$PKI_CONFIG" ]; then
    echo "Workspace mail configuration is incomplete; deferring bootstrap."
    exit 0
fi
if ! workspace_mail_gate_is_provisioned \
    "$COMPATIBILITY_MARKER" "$ENFORCED_MARKER" "$WRITER_GATE_CONFIG"; then
    echo "Workspace mail writer-gate state is incomplete; deferring bootstrap."
    exit 0
fi

PERSISTENT_DISK=$(find_persistent_disk)
if [[ -z "$PERSISTENT_DISK" ]]; then
    echo "Workspace mail persistent disk is required" >&2
    exit 1
fi
if mountpoint -q "$PERSISTENT_MOUNT"; then
    echo "Persistent disk is already mounted at $PERSISTENT_MOUNT."
else
    prepare_persistent_disk "$PERSISTENT_DISK" "$PERSISTENT_MOUNT"
fi
mkdir -p "$WORKSPACE_MAIL_DIR"
if mountpoint -q "$WORKSPACE_MAIL_DIR"; then
    echo "Workspace Maildir is already mounted at $WORKSPACE_MAIL_DIR."
else
    migrate_to_persistent \
        "$WORKSPACE_MAIL_DIR" \
        "$PERSISTENT_MOUNT/var/lib/workspace/messenger/mail"
fi
persist_migrate_complete

"$MAIL_PKI_BIN" \
    "$PKI_CONFIG" \
    "$PERSISTENT_MOUNT/$TLS_STORE_RELATIVE" \
    "$TLS_OUTPUT_DIR"
test -s "$TLS_BUNDLE"
test -s "$TLS_CA"

WORKSPACE_CONFIG="$WORKSPACE_CONFIG" \
    SMTP_WRITER_GATE_HOLD="$SMTP_WRITER_GATE_HOLD" \
    "$MAIL_CONFIGURE_BIN"
if [[ -e "$SMTP_WRITER_GATE_HOLD" ]]; then
    workspace_mail_validate_hold "$SMTP_WRITER_GATE_HOLD"
    "$MAIL_HEALTHCHECK_BIN" "$WORKSPACE_CONFIG" --smtp-inactive
else
    "$MAIL_HEALTHCHECK_BIN" "$WORKSPACE_CONFIG"
fi

echo "Workspace mail bootstrap completed successfully."
