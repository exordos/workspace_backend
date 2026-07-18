#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

set -eu
set -o pipefail

BRIDGE_CONFIG=${WORKSPACE_ZULIP_BRIDGE_MAIL_CONFIG:-/etc/workspace/zulip-bridge-mail.conf}
DOVECOT_PASSWD=/etc/dovecot/workspace-zulip-bridge.passwd
EXIM_PASSWD=/etc/exim4/workspace-zulip-bridge-smtp.passwd
BRIDGE_HOME=/var/lib/workspace/messenger/mail/bridge.workspace.invalid/zulip-bridge

if [[ ! -s "$BRIDGE_CONFIG" ]]; then
    echo "Workspace Zulip bridge mail configuration is not available" >&2
    exit 1
fi

readarray -t BRIDGE_VALUES < <(
    python3 - "$BRIDGE_CONFIG" <<'PY'
import configparser
import sys

config = configparser.ConfigParser(interpolation=None)
if config.read(sys.argv[1]) != [sys.argv[1]] or set(config) != {
    "DEFAULT",
    "zulip_bridge",
}:
    raise SystemExit("Invalid Workspace Zulip bridge mail configuration")
section = config["zulip_bridge"]
expected = {
    "username",
    "password",
    "outbox_prefix",
    "ingress_local_part",
    "envelope_sender_local_part",
}
if set(section) != expected:
    raise SystemExit("Invalid Workspace Zulip bridge mail configuration keys")
for key in (
    "username",
    "password",
    "outbox_prefix",
    "ingress_local_part",
    "envelope_sender_local_part",
):
    print(section[key])
PY
)

if [[ ${#BRIDGE_VALUES[@]} -ne 5 ]]; then
    echo "Workspace Zulip bridge mail configuration is incomplete" >&2
    exit 1
fi

USERNAME=${BRIDGE_VALUES[0]}
PASSWORD=${BRIDGE_VALUES[1]}
OUTBOX_PREFIX=${BRIDGE_VALUES[2]}
INGRESS_LOCAL_PART=${BRIDGE_VALUES[3]}
ENVELOPE_SENDER_LOCAL_PART=${BRIDGE_VALUES[4]}

if [[ "$USERNAME" != zulip-bridge || -z "$PASSWORD" || \
      "$OUTBOX_PREFIX" != Workspace/Bridge/Zulip/V1/Accounts || \
      "$INGRESS_LOCAL_PART" != zulip-bridge-ingress || \
      "$ENVELOPE_SENDER_LOCAL_PART" != zulip-bridge ]]; then
    echo "Workspace Zulip bridge mail policy does not match v1" >&2
    exit 1
fi

install -d -m 0750 -o workspace -g workspace "$BRIDGE_HOME/Maildir"
umask 077
WORKSPACE_UID=$(id -u workspace)
WORKSPACE_GID=$(id -g workspace)
{
    printf '%s:{PLAIN}%s:%s:%s::%s::userdb_mail_readonly=yes\n' \
        "$USERNAME" "$PASSWORD" "$WORKSPACE_UID" "$WORKSPACE_GID" \
        "$BRIDGE_HOME"
    printf 'zulip-bridge-producer@bridge.workspace.invalid:{CRYPT}*:%s:%s::%s::\n' \
        "$WORKSPACE_UID" "$WORKSPACE_GID" "$BRIDGE_HOME"
} >"$DOVECOT_PASSWD"
chown root:dovecot "$DOVECOT_PASSWD"
chmod 0640 "$DOVECOT_PASSWD"
printf '%s: %s\n' "$USERNAME" "$PASSWORD" >"$EXIM_PASSWD"
chown root:Debian-exim "$EXIM_PASSWD"
chmod 0640 "$EXIM_PASSWD"

update-exim4.conf
doveconf -n | /usr/local/bin/workspace-dovecot-validate
exim4 -bV >/dev/null
systemctl reload-or-restart dovecot.service
systemctl reload-or-restart exim4.service
