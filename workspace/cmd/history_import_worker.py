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

"""Dedicated low-concurrency history preparation and import process."""

import logging
import signal
import sys
import threading

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.common import contexts

from workspace.common import config
from workspace.common import database
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.common import messenger_reaction_opts
from workspace.history_import import agents


CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
file_storage_opts.register_opts(CONF)
messenger_reaction_opts.register_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])
    infra_log.configure()
    CONF.set_override("connection_pool_min_size", 1, group="db")
    CONF.set_override("connection_pool_max_size", 2, group="db")
    database.configure_postgresql("workspace-history-import")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    worker = agents.HistoryImportWorker(
        contexts.Context().session_manager,
        reaction_user_limit=CONF[messenger_reaction_opts.DOMAIN].user_list_limit,
    )
    while not stopped.is_set():
        try:
            worked = worker.run_once()
        except Exception as error:
            logging.getLogger(__name__).warning(
                "History iteration deferred: error_type=%s", type(error).__name__
            )
            worked = False
        stopped.wait(0.1 if worked else 1)


if __name__ == "__main__":
    main()
