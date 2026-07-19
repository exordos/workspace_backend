# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import logging
import sys

from gcl_iam import drivers
from gcl_iam import opts as iam_opts
from gcl_looper.services import bjoern_service
from gcl_looper.services import hub
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import external_bridge_opts
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.common import messenger_mail_opts
from workspace.common import messenger_storage_opts
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.messenger_migration import writer_gate
from workspace.workspace_api.api import app


DOMAIN = "workspace_api"

api_cli_opts = [
    cfg.StrOpt("bind-host", default="127.0.0.1"),
    cfg.IntOpt("bind-port", default=21084),
    cfg.IntOpt("workers", default=1),
]

CONF = cfg.CONF
CONF.register_cli_opts(api_cli_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)
iam_opts.register_iam_cli_opts(CONF)
file_storage_opts.register_opts(CONF)
external_bridge_opts.register_opts(CONF)
messenger_mail_opts.register_opts(CONF)
messenger_storage_opts.register_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)
    log.info(
        "Start service on %s:%s",
        CONF[DOMAIN].bind_host,
        CONF[DOMAIN].bind_port,
    )
    service_hub = hub.ProcessHubService()
    iam_driver = drivers.HttpDriver(
        CONF.iam.iam_endpoint,
        CONF.iam.audience,
        CONF.iam.hs256_jwks_decryption_key,
    )
    api_store.configure_store_factory(
        store_factory.build_configured_store_factory(CONF)
    )
    for _ in range(CONF[DOMAIN].workers):
        service = bjoern_service.BjoernService(
            wsgi_app=app.build_wsgi_application(iam_driver),
            host=CONF[DOMAIN].bind_host,
            port=CONF[DOMAIN].bind_port,
            bjoern_kwargs={"reuse_port": True},
        )
        service.add_setup(
            lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
        )
        service.add_setup(
            lambda: writer_gate.start_heartbeat(
                engines.engine_factory.get_engine().session_manager,
                "api",
            )
        )
        service_hub.add_service(service)
    service_hub.start()
    log.info("Bye!!!")


if __name__ == "__main__":
    main()
