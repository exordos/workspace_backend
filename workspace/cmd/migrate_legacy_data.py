# Copyright 2026 Genesis Corporation.
# Licensed under the Apache License, Version 2.0 (the "License").

import sys

from restalchemy.storage.sql import engines

from workspace.cmd import history_import
from workspace.common import config, database
from workspace.upgrade import legacy_data


def main():
    try:
        config.parse(sys.argv[1:])
        # A one-time conversion can exceed the normal workers' DB limits.
        # Overrides stay in this command; installed service settings are intact.
        for timeout in (
            "connection_statement_timeout",
            "connection_transaction_timeout",
            "connection_idle_in_transaction_session_timeout",
        ):
            history_import.CONF.set_override(timeout, 0, group="db")
        database.configure_postgresql(
            "workspace-legacy-data-upgrade", conf=history_import.CONF
        )
        count = legacy_data.migrate()
    except Exception as error:  # noqa: BLE001 - keep credentials out of deployment logs.
        print(
            f"Workspace legacy data conversion failed ({type(error).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        engines.engine_factory.destroy_all_engines()
    print(f"Workspace legacy data converted for {count} accounts")
