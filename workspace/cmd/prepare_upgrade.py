# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import argparse
import sys

from workspace.upgrade import legacy_zulip


def main():
    parser = argparse.ArgumentParser(description="Prepare the native Zulip upgrade")
    parser.add_argument("--config-file", required=True)
    args = parser.parse_args()
    try:
        count = legacy_zulip.prepare_from_config(args.config_file)
    except Exception as error:  # noqa: BLE001 - keep credentials out of deployment logs.
        # This path handles credentials; database and crypto exception details
        # must not echo connection strings or payloads into deployment logs.
        print(
            f"Workspace credential handoff failed ({type(error).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(f"Workspace credential handoff verified for {count} accounts")
