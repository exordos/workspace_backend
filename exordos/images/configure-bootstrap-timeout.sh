#!/usr/bin/env bash
# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

# Native services execute bootstrap in ExecStartPre. A real database migration
# and schema changes can exceed systemd's default 90-second start timeout.
sudo install -d -m 0755 /etc/systemd/system/exordos_srv_workspace-.service.d
sudo tee /etc/systemd/system/exordos_srv_workspace-.service.d/10-bootstrap-timeout.conf >/dev/null <<'EOF'
[Service]
TimeoutStartSec=infinity
EOF
