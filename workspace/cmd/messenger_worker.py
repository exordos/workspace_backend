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
from gcl_looper.services import hub
from restalchemy.common import config_opts as ra_config_opts

from workspace.common import config
from workspace.common import database
from workspace.common import mutation_journal_opts
from workspace.common import file_storage_opts
from workspace.common import log as infra_log
from workspace.common import messenger_worker_opts
from workspace.common import messenger_reaction_opts
from workspace.common import topic_summary_opts
from workspace.messenger_api.api import store as api_store
from workspace.messenger_api.api import store_factory
from workspace.services.messenger_workers import agents

DOMAIN = messenger_worker_opts.DOMAIN
TOPIC_SUMMARY_DOMAIN = topic_summary_opts.DOMAIN


CONF = cfg.CONF
ra_config_opts.register_posgresql_db_opts(CONF)
mutation_journal_opts.register_opts(CONF)
file_storage_opts.register_opts(CONF)
messenger_worker_opts.register_opts(CONF)
messenger_reaction_opts.register_opts(CONF)
topic_summary_opts.register_opts(CONF)


def configure_worker_database() -> None:
    if mutation_journal_opts.enabled():
        CONF.set_override(
            "connection_pool_max_size",
            CONF[mutation_journal_opts.DOMAIN].threads + 2,
            group="db",
        )
    database.configure_postgresql("workspace-messenger-worker", conf=CONF)


def build_worker_services() -> tuple[
    agents.MessengerWorkerAgent | agents.MessengerPublicationAgent, ...
]:
    worker_count = (
        CONF[DOMAIN].v2_projection_workers if CONF[DOMAIN].v2_projection_enabled else 1
    )
    owned = mutation_journal_opts.enabled()
    if owned:
        worker_count = 2
    services = []
    for worker_index in range(worker_count):
        projection_enabled = CONF[DOMAIN].v2_projection_enabled and (
            not owned or worker_index == 0
        )
        service = agents.MessengerWorkerAgent(
            iter_min_period=0 if projection_enabled else 3,
            iter_pause=0 if projection_enabled else 0.1,
            event_retention=datetime.timedelta(
                seconds=CONF[DOMAIN].event_retention_seconds,
            ),
            event_prune_interval_seconds=(CONF[DOMAIN].event_prune_interval_seconds),
            event_prune_batch_size=CONF[DOMAIN].event_prune_batch_size,
            history_retention_seconds=CONF[DOMAIN].history_retention_seconds,
            history_prune_interval_seconds=CONF[DOMAIN].history_prune_interval_seconds,
            history_prune_batch_size=CONF[DOMAIN].history_prune_batch_size,
            v2_projection_enabled=projection_enabled,
            v2_projection_max_tasks_per_iteration=(
                CONF[DOMAIN].v2_projection_max_tasks_per_iteration
            ),
            v2_idle_sleep_seconds=(CONF[DOMAIN].v2_projection_idle_sleep_seconds),
            v2_projection_db_url=(
                CONF.db.connection_url if projection_enabled else None
            ),
            v2_fanout_batch_size=CONF[DOMAIN].v2_fanout_batch_size,
            v2_metrics_log_interval_seconds=(
                CONF[DOMAIN].v2_metrics_log_interval_seconds
            ),
            projection_only=(worker_index == 0 if owned else worker_index > 0),
            summary_secret_key=CONF[TOPIC_SUMMARY_DOMAIN].secret_encryption_key,
            summary_connect_timeout_seconds=(
                CONF[TOPIC_SUMMARY_DOMAIN].connect_timeout_seconds
            ),
            summary_request_timeout_seconds=(
                CONF[TOPIC_SUMMARY_DOMAIN].request_timeout_seconds
            ),
            summary_topic_claim_seconds=(
                CONF[TOPIC_SUMMARY_DOMAIN].topic_claim_seconds
            ),
            summary_endpoint_claim_seconds=(
                CONF[TOPIC_SUMMARY_DOMAIN].endpoint_claim_seconds
            ),
        )
        service.add_setup(configure_worker_database)
        services.append(service)
    if CONF[DOMAIN].v2_projection_enabled and not owned:
        publisher = agents.MessengerPublicationAgent(iter_min_period=0.1, iter_pause=0)
        publisher.add_setup(configure_worker_database)
        services.append(publisher)
    return tuple(services)


def main() -> None:
    config.parse(sys.argv[1:])

    infra_log.configure()
    log = logging.getLogger(__name__)

    factory = store_factory.build_store_factory()
    api_store.configure_store_factory(factory)
    service_hub = hub.ProcessHubService()
    for service in build_worker_services():
        service_hub.add_service(service)
    service_hub.start()

    log.info("Bye!!!")


if __name__ == "__main__":
    main()
