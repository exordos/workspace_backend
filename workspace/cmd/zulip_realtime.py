# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.

import asyncio
import logging
import sys

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts

from workspace.common import config
from workspace.common import database
from workspace.common import log as infra_log
from workspace.services import zulip_realtime


DOMAIN = "zulip_realtime"

zulip_realtime_opts = [
    cfg.FloatOpt(
        "step-interval",
        default=1,
        min=0.1,
        help="Seconds between account reconciliation and fallback inbox checks",
    ),
]

CONF = cfg.CONF
CONF.register_cli_opts(zulip_realtime_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)

    database.configure_postgresql("workspace-zulip-realtime", conf=CONF)
    service = zulip_realtime.ZulipRealtimeService(
        db_url=CONF.db.connection_url,
        step_interval=CONF[DOMAIN].step_interval,
    )
    log.info("Start Zulip realtime service")
    asyncio.run(service.serve())
    log.info("Bye!!!")


if __name__ == "__main__":
    main()
