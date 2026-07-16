#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

source /usr/local/lib/exordos/lib_bootstrap.sh

WORKSPACE_CONFIG=/etc/workspace/mail.conf
RUN_DIR=/run/workspace-mail
LOCK_FILE="$RUN_DIR/bootstrap.lock"
WORKSPACE_MAIL_DIR=/var/lib/workspace/messenger/mail

mkdir -p "$RUN_DIR" /etc/workspace
exec 9>"$LOCK_FILE"
flock -x 9

while [ ! -s "$WORKSPACE_CONFIG" ]; do
    echo "Waiting for workspace mail platform config..."
    sleep 1
done

PERSISTENT_DISK=$(find_persistent_disk)
if [[ -n "$PERSISTENT_DISK" ]]; then
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
fi

WORKSPACE_CONFIG="$WORKSPACE_CONFIG" /usr/local/bin/workspace-mail-configure
/usr/local/bin/workspace-mail-healthcheck "$WORKSPACE_CONFIG"

echo "Workspace mail bootstrap completed successfully."
