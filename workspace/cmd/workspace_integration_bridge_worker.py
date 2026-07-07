#    Copyright 2026 Genesis Corporation.
#
#    All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import sys

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.services.integration_bridge import agents

DOMAIN = "workspace_integration_bridge_worker"


worker_cli_opts = [
    cfg.IntOpt(
        "sync-queue-batch-limit",
        default=agents.DEFAULT_SYNC_QUEUE_BATCH_LIMIT,
        help="Maximum Zulip sync queue commands to process in one iteration",
    ),
]


CONF = cfg.CONF
CONF.register_cli_opts(worker_cli_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)
file_storage_opts.register_opts(CONF)


def main():
    config.parse(sys.argv[1:])

    infra_log.configure()
    log = logging.getLogger(__name__)

    service = agents.WorkspaceIntegrationBridgeWorker(
        iter_min_period=3,
        sync_queue_batch_limit=CONF[DOMAIN].sync_queue_batch_limit,
    )

    service.add_setup(
        lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
    )

    service.start()

    log.info("Bye!!!")


if __name__ == "__main__":
    main()
