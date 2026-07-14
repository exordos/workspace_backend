# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import logging
import sys

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.services.groupware_bridge import agents


CONF = cfg.CONF


def main():
    ra_config_opts.register_posgresql_db_opts(CONF)
    file_storage_opts.register_opts(CONF)
    config.parse(sys.argv[1:])
    infra_log.configure()
    log = logging.getLogger(__name__)
    service = agents.WorkspaceGroupwareBridgeWorker(iter_min_period=5)
    service.add_setup(
        lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
    )
    service.start()
    log.info("Bye!!!")


if __name__ == "__main__":
    main()
