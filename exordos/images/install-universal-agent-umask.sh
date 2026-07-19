#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

set -eu
set -o pipefail

UNIT=exordos-universal-agent.service
DROP_IN_DIR="/etc/systemd/system/$UNIT.d"

sudo install -d -m 0755 "$DROP_IN_DIR"
printf '[Service]\nUMask=0077\n' \
    | sudo tee "$DROP_IN_DIR/10-workspace-secret-umask.conf" >/dev/null
sudo chmod 0644 "$DROP_IN_DIR/10-workspace-secret-umask.conf"

systemd-analyze verify "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload
