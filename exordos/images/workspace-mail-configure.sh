#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

set -eu
set -o pipefail

WORKSPACE_CONFIG=${WORKSPACE_CONFIG:-/etc/workspace/workspace.conf}
MASTER_PASSWD=/etc/dovecot/workspace-master.passwd
SMTP_PASSWD=/etc/exim4/workspace-smtp.passwd
TLS_BUNDLE=/etc/workspace/tls/workspace-mail.pem
TLS_CA=/etc/workspace/tls/workspace-mail-ca.crt

if [[ ! -s "$WORKSPACE_CONFIG" ]]; then
    echo "Workspace configuration is not available" >&2
    exit 1
fi
if [[ ! -s "$TLS_BUNDLE" || ! -s "$TLS_CA" ]]; then
    echo "Workspace mail TLS material is not available" >&2
    exit 1
fi

# Keep the Core-generated credential out of shell tracing and process output.
readarray -t MAIL_CREDENTIALS < <(
    python3 - "$WORKSPACE_CONFIG" <<'PY'
import configparser
import sys

config = configparser.ConfigParser()
config.read(sys.argv[1])
print(config["messenger_mail"]["imap_master_username"])
print(config["messenger_mail"]["imap_master_password"])
print(config["messenger_mail"]["smtp_username"])
print(config["messenger_mail"]["smtp_password"])
PY
)

if [[ ${#MAIL_CREDENTIALS[@]} -ne 4 ]]; then
    echo "Messenger mail credentials are incomplete" >&2
    exit 1
fi
if [[ -z "${MAIL_CREDENTIALS[0]}" || -z "${MAIL_CREDENTIALS[1]}" || \
      -z "${MAIL_CREDENTIALS[2]}" || -z "${MAIL_CREDENTIALS[3]}" ]]; then
    echo "Messenger mail credentials must not be empty" >&2
    exit 1
fi

install -d -m 0755 -o root -g root /run/workspace
install -d -m 0750 -o workspace -g workspace \
    /var/lib/workspace/messenger/mail \
    /run/workspace/dovecot-indexes
umask 077
printf '%s:{PLAIN}%s\n' \
    "${MAIL_CREDENTIALS[0]}" \
    "${MAIL_CREDENTIALS[1]}" >"$MASTER_PASSWD"
chown root:dovecot "$MASTER_PASSWD"
chmod 0640 "$MASTER_PASSWD"
printf '%s: %s\n' \
    "${MAIL_CREDENTIALS[2]}" \
    "${MAIL_CREDENTIALS[3]}" >"$SMTP_PASSWD"
chown root:Debian-exim "$SMTP_PASSWD"
chmod 0640 "$SMTP_PASSWD"

update-exim4.conf
doveconf -n | /usr/local/bin/workspace-dovecot-validate
exim4 -bV >/dev/null

systemctl restart dovecot.service
systemctl restart exim4.service
