#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -x
set -o pipefail

GC_PATH=/opt/workspace

source "$GC_PATH/exordos/images/install-universal-agent-umask.sh"

sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
    dovecot-core \
    dovecot-imapd \
    exim4-daemon-light \
    openssh-server \
    openssl \
    postgresql-client \
    python3

sudo systemctl enable ssh.service

if ! getent group workspace >/dev/null; then
    sudo groupadd --system workspace
fi
if ! getent passwd workspace >/dev/null; then
    sudo useradd \
        --system \
        --gid workspace \
        --home-dir /var/lib/workspace \
        --shell /usr/sbin/nologin \
        workspace
fi
if ! getent group workspace-pki >/dev/null; then
    sudo groupadd --system workspace-pki
fi
if ! getent passwd workspace-pki >/dev/null; then
    sudo useradd \
        --system \
        --gid workspace-pki \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        workspace-pki
fi

sudo install -d -m 0750 -o workspace -g workspace \
    /var/lib/workspace/messenger/mail
sudo install -d -m 0755 /etc/workspace /etc/workspace/tls /usr/local/bin
sudo install -m 0644 \
    "$GC_PATH/etc/dovecot/99-workspace-messenger.conf" \
    /etc/dovecot/conf.d/99-workspace-messenger.conf
sudo sed -i \
    's/^!include auth-system\.conf\.ext/# Workspace disables OS-user IMAP authentication./' \
    /etc/dovecot/conf.d/10-auth.conf
sudo install -m 0644 \
    "$GC_PATH/etc/exim4/workspace-messenger-auth.conf" \
    /etc/exim4/conf.d/auth/30_workspace_messenger
sudo install -m 0644 \
    "$GC_PATH/etc/exim4/workspace-messenger-tls.conf" \
    /etc/exim4/conf.d/main/01_workspace_messenger
sudo install -m 0644 \
    "$GC_PATH/etc/exim4/workspace-messenger-router.conf" \
    /etc/exim4/conf.d/router/250_workspace_messenger
sudo install -m 0644 \
    "$GC_PATH/etc/exim4/workspace-messenger-transport.conf" \
    /etc/exim4/conf.d/transport/30_workspace_messenger
sudo install -m 0644 \
    "$GC_PATH/etc/exim4/workspace-messenger-local-parts" \
    /etc/exim4/workspace-messenger-local-parts
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-zulip-bridge-mail-configure.sh" \
    /usr/local/bin/workspace-zulip-bridge-mail-configure
sudo install -m 0640 -o root -g dovecot /dev/null \
    /etc/dovecot/workspace-zulip-bridge.passwd
sudo install -m 0640 -o root -g Debian-exim /dev/null \
    /etc/exim4/workspace-zulip-bridge-smtp.passwd
sudo sed -i "s/^dc_eximconfig_configtype=.*/dc_eximconfig_configtype='local'/" \
    /etc/exim4/update-exim4.conf.conf
sudo sed -i "s/^dc_local_interfaces=.*/dc_local_interfaces='0.0.0.0'/" \
    /etc/exim4/update-exim4.conf.conf
sudo sed -i "s/^dc_other_hostnames=.*/dc_other_hostnames='messenger.workspace.invalid'/" \
    /etc/exim4/update-exim4.conf.conf
sudo sed -i "s/^dc_use_split_config=.*/dc_use_split_config='true'/" \
    /etc/exim4/update-exim4.conf.conf

sudo install -m 0755 \
    "$GC_PATH/exordos/images/mail-bootstrap.sh" \
    /usr/local/bin/workspace-mail-bootstrap
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-mail-pki.sh" \
    /usr/local/bin/workspace-mail-pki
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-mail-ca-server.py" \
    /usr/local/bin/workspace-mail-ca-server
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-dovecot-validate.py" \
    /usr/local/bin/workspace-dovecot-validate
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-mail-configure.sh" \
    /usr/local/bin/workspace-mail-configure
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-mail-healthcheck.py" \
    /usr/local/bin/workspace-mail-healthcheck
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-mail-reload.sh" \
    /usr/local/bin/workspace-mail-reload
sudo install -d -m 0755 /usr/local/lib/workspace
sudo install -m 0644 \
    "$GC_PATH/exordos/images/workspace-mail-runtime-readiness.sh" \
    /usr/local/lib/workspace/mail-runtime-readiness.sh
sudo install -m 0755 \
    "$GC_PATH/exordos/images/workspace-smtp-ingress-attester.py" \
    /usr/local/bin/workspace-smtp-ingress-attester
sudo install -d -m 0755 /etc/systemd/system/exim4.service.d
sudo tee /etc/systemd/system/exim4.service.d/workspace-writer-gate.conf \
    >/dev/null <<'EOF'
[Service]
ExecStartPre=/usr/local/bin/workspace-smtp-ingress-attester exim-prestart
EOF
sudo systemctl daemon-reload

sudo systemctl disable --now dovecot.service exim4.service || true
