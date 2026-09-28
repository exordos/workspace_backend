# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import logging
import sys

from gcl_looper.services import hub
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import log as infra_log
from workspace.common import workspace_v3_worker_opts
from workspace.services.workspace_v3_workers import agents


CONF = cfg.CONF
DOMAIN = workspace_v3_worker_opts.DOMAIN

ra_config_opts.register_posgresql_db_opts(CONF)
workspace_v3_worker_opts.register_opts(CONF)


def build_worker_services() -> tuple[agents.WorkspaceV3ProjectionAgent, ...]:
    services = []
    for _worker_index in range(CONF[DOMAIN].workers):
        service = agents.WorkspaceV3ProjectionAgent(
            iter_min_period=0,
            iter_pause=0,
            batch_size=CONF[DOMAIN].batch_size,
            lease_seconds=CONF[DOMAIN].lease_seconds,
            max_attempts=CONF[DOMAIN].max_attempts,
            reaction_user_limit=CONF[DOMAIN].reaction_user_limit,
            idle_sleep_seconds=CONF[DOMAIN].idle_sleep_seconds,
            metrics_log_interval_seconds=(CONF[DOMAIN].metrics_log_interval_seconds),
        )
        service.add_setup(
            lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
        )
        services.append(service)
    return tuple(services)


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    service_hub = hub.ProcessHubService()
    for service in build_worker_services():
        service_hub.add_service(service)
    service_hub.start()
    logging.getLogger(__name__).info("Workspace v3 projection worker stopped")


if __name__ == "__main__":
    main()
