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

import datetime
import logging
import sys

from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.common import messenger_worker_opts
from workspace.common import topic_summary_opts
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import agents

DOMAIN = messenger_worker_opts.DOMAIN
TOPIC_SUMMARY_DOMAIN = topic_summary_opts.DOMAIN


CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
file_storage_opts.register_opts(CONF)
messenger_worker_opts.register_opts(CONF)
topic_summary_opts.register_opts(CONF)


def main() -> None:
    config.parse(sys.argv[1:])

    infra_log.configure()
    log = logging.getLogger(__name__)

    factory = store_factory.build_store_factory()
    api_store.configure_store_factory(factory)
    service = agents.MessengerWorkerAgent(
        iter_min_period=3,
        event_retention=datetime.timedelta(
            seconds=CONF[DOMAIN].event_retention_seconds,
        ),
        event_prune_interval_seconds=(CONF[DOMAIN].event_prune_interval_seconds),
        event_prune_batch_size=CONF[DOMAIN].event_prune_batch_size,
        heartbeat_retention=datetime.timedelta(
            seconds=CONF[DOMAIN].heartbeat_retention_seconds,
        ),
        read_state_compaction_enabled=(CONF[DOMAIN].read_state_compaction_enabled),
        read_state_cleanup_enabled=CONF[DOMAIN].read_state_cleanup_enabled,
        read_state_batch_size=CONF[DOMAIN].read_state_batch_size,
        read_state_max_batches_per_iteration=(
            CONF[DOMAIN].read_state_max_batches_per_iteration
        ),
        v2_projection_enabled=CONF[DOMAIN].v2_projection_enabled,
        v2_projection_max_tasks_per_iteration=(
            CONF[DOMAIN].v2_projection_max_tasks_per_iteration
        ),
        v2_fanout_batch_size=CONF[DOMAIN].v2_fanout_batch_size,
        summary_secret_key=CONF[TOPIC_SUMMARY_DOMAIN].secret_encryption_key,
        summary_connect_timeout_seconds=(
            CONF[TOPIC_SUMMARY_DOMAIN].connect_timeout_seconds
        ),
        summary_request_timeout_seconds=(
            CONF[TOPIC_SUMMARY_DOMAIN].request_timeout_seconds
        ),
        summary_topic_claim_seconds=CONF[TOPIC_SUMMARY_DOMAIN].topic_claim_seconds,
        summary_endpoint_claim_seconds=(
            CONF[TOPIC_SUMMARY_DOMAIN].endpoint_claim_seconds
        ),
    )

    service.add_setup(
        lambda: engines.engine_factory.configure_postgresql_factory(conf=CONF)
    )

    service.start()

    log.info("Bye!!!")


if __name__ == "__main__":
    main()
