#    Copyright 2025 Genesis Corporation.
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
from workspace.common import external_bridge_opts
from workspace.common import log as infra_log
from workspace.common import messenger_mail_opts
from workspace.common import messenger_storage_opts
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import agents

DOMAIN = "messenger_worker_agent"


CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
messenger_mail_opts.register_opts(CONF)
external_bridge_opts.register_opts(CONF)
messenger_storage_opts.register_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])

    infra_log.configure()
    log = logging.getLogger(__name__)

    factory = store_factory.build_configured_store_factory(
        CONF,
        bridge_config=CONF[external_bridge_opts.DOMAIN],
    )
    api_store.configure_store_factory(factory)
    runtime_factory = getattr(factory, "runtime_factory", None)
    service = agents.MessengerWorkerAgent(
        iter_min_period=3,
        runtime_factory=runtime_factory,
        bridge_config=CONF[external_bridge_opts.DOMAIN],
        storage_mode=CONF[messenger_storage_opts.DOMAIN].mode,
    )

    service.add_setup(
        lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
    )

    service.start()

    log.info("Bye!!!")


if __name__ == "__main__":
    main()
