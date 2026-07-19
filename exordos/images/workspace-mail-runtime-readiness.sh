#!/usr/bin/env bash

# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

workspace_mail_gate_is_provisioned() {
    local compatibility_marker=$1
    local enforced_marker=$2
    local writer_gate_config=$3

    if [[ -e "$enforced_marker" ]]; then
        [[ -s "$enforced_marker" ]] || return 1
        [[ "$(tr -d '\r\n' <"$enforced_marker")" == \
            "enforced-prestart-v1" ]] || return 1
        [[ -s "$writer_gate_config" ]] || return 1
        return 0
    fi

    [[ -s "$compatibility_marker" ]] || return 1
    [[ "$(tr -d '\r\n' <"$compatibility_marker")" == \
        "compatibility-prestart-v1" ]]
}

workspace_mail_validate_hold() {
    local hold_path=$1

    python3 - "$hold_path" <<'PY'
import datetime
import json
import pathlib
import sys
import uuid

path = pathlib.Path(sys.argv[1])
try:
    hold = json.loads(path.read_text(encoding="utf-8"))
    if hold.get("schema_version") != 1:
        raise ValueError("unsupported schema")
    if not isinstance(hold.get("instance_id"), str) or not hold["instance_id"]:
        raise ValueError("missing instance")
    gate_ids = hold.get("gate_ids")
    if not isinstance(gate_ids, list) or not gate_ids:
        raise ValueError("missing gate IDs")
    parsed_gate_ids = [uuid.UUID(value) for value in gate_ids]
    if len(parsed_gate_ids) != len(set(parsed_gate_ids)):
        raise ValueError("duplicate gate IDs")
    held_at = datetime.datetime.fromisoformat(hold["held_at"])
    if held_at.tzinfo is None:
        raise ValueError("hold timestamp has no timezone")
except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    print("SMTP ingress hold evidence is invalid", file=sys.stderr)
    raise SystemExit(1)
PY
}

workspace_mail_reconcile_configured_exim() {
    local hold_path=$1
    local systemctl_bin=${2:-systemctl}

    if [[ -e "$hold_path" ]]; then
        workspace_mail_validate_hold "$hold_path"
        "$systemctl_bin" stop exim4.service
        echo "SMTP ingress remains inactive under the persistent writer-gate hold."
    else
        "$systemctl_bin" restart exim4.service
    fi
}
