# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import logging
import sys

from gcl_looper.services import bjoern_service
from gcl_looper.services import hub
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import log as infra_log
from workspace.provider_api.api import app


DOMAIN = "workspace_provider_api"
CONF = cfg.CONF

api_cli_opts = [
    cfg.StrOpt("bind-host", default="127.0.0.1"),
    cfg.IntOpt("bind-port", default=21083),
    cfg.IntOpt("workers", default=1),
]

CONF.register_cli_opts(api_cli_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)


def main():
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)
    service_hub = hub.ProcessHubService()
    for _ in range(CONF[DOMAIN].workers):
        service = bjoern_service.BjoernService(
            wsgi_app=app.build_wsgi_application(),
            host=CONF[DOMAIN].bind_host,
            port=CONF[DOMAIN].bind_port,
            bjoern_kwargs={"reuse_port": True},
        )
        service.add_setup(
            lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
        )
        service_hub.add_service(service)
    log.info(
        "Start provider API on %s:%s",
        CONF[DOMAIN].bind_host,
        CONF[DOMAIN].bind_port,
    )
    service_hub.start()
    log.info("Bye!!!")


if __name__ == "__main__":
    main()
