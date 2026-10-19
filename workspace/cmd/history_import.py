# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import logging
import sys

from gcl_looper.services import hub
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts

from workspace.common import config
from workspace.common import database
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.services import history_import


DOMAIN = "history_import"

history_import_opts = [
    cfg.FloatOpt(
        "step-interval",
        default=3,
        min=0.1,
        help="Minimum seconds between history import steps",
    ),
]

CONF = cfg.CONF
CONF.register_cli_opts(history_import_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)
file_storage_opts.register_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)

    service = history_import.HistoryImportService(
        iter_min_period=CONF[DOMAIN].step_interval,
    )
    service.add_setup(
        lambda: database.configure_postgresql("workspace-history-import", conf=CONF)
    )
    service_hub = hub.ProcessHubService()
    service_hub.add_service(service)
    log.info("Start history import service")
    service_hub.start()
    log.info("Bye!!!")


if __name__ == "__main__":
    main()
