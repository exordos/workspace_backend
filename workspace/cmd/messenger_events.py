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

import asyncio
import logging
import sys

from gcl_iam import drivers
from gcl_iam import opts as iam_opts
from oslo_config import cfg
from restalchemy.common import config_opts as ra_config_opts
from restalchemy.storage.sql import engines

from workspace.common import config
from workspace.common import log as infra_log
from workspace.messenger_api import websocket_service


events_cli_opts = [
    cfg.StrOpt(
        "bind-host",
        default="127.0.0.1",
        help="The host IP to bind to",
    ),
    cfg.IntOpt(
        "bind-port",
        default=21082,
        help="The websocket port to bind to",
    ),
    cfg.IntOpt(
        "workers",
        default=1,
        help="How many websocket server workers should be started",
    ),
    cfg.IntOpt(
        "heartbeat-interval",
        default=25,
        help="Seconds between websocket heartbeat frames",
    ),
    cfg.IntOpt(
        "client-timeout",
        default=60,
        help="Seconds to wait while sending a frame to a client",
    ),
    cfg.IntOpt(
        "catchup-limit",
        default=500,
        help="Maximum events to send in one catch-up batch",
    ),
    cfg.IntOpt(
        "send-queue-limit",
        default=32,
        help="Maximum inbound websocket frame queue size",
    ),
    cfg.IntOpt(
        "poll-interval",
        default=3,
        help="Seconds between fallback database catch-up checks",
    ),
]


DOMAIN = "messenger_events"


CONF = cfg.CONF
CONF.register_cli_opts(events_cli_opts, DOMAIN)
ra_config_opts.register_posgresql_db_opts(CONF)
iam_opts.register_iam_cli_opts(CONF)


def main():
    config.parse(sys.argv[1:])

    infra_log.configure()
    log = logging.getLogger(__name__)

    if CONF[DOMAIN].workers != 1:
        log.warning(
            "workspace-messenger-events currently runs one asyncio worker; "
            "requested workers=%s will be ignored",
            CONF[DOMAIN].workers,
        )

    engines.engine_factory.configure_postgresql_factory(conf=CONF)
    iam_driver = drivers.HttpDriver(
        CONF.iam.iam_endpoint,
        CONF.iam.audience,
        CONF.iam.hs256_jwks_decryption_key,
    )
    server = websocket_service.MessengerEventsWebsocketServer(
        db_url=CONF.db.connection_url,
        iam_engine_driver=iam_driver,
        heartbeat_interval=CONF[DOMAIN].heartbeat_interval,
        client_timeout=CONF[DOMAIN].client_timeout,
        catchup_limit=CONF[DOMAIN].catchup_limit,
        send_queue_limit=CONF[DOMAIN].send_queue_limit,
        poll_interval=CONF[DOMAIN].poll_interval,
    )

    log.info(
        "Start workspace events websocket service on %s:%s",
        CONF[DOMAIN].bind_host,
        CONF[DOMAIN].bind_port,
    )
    try:
        asyncio.run(
            server.serve(
                host=CONF[DOMAIN].bind_host,
                port=CONF[DOMAIN].bind_port,
            )
        )
    except KeyboardInterrupt:
        server.stop()
    finally:
        engines.engine_factory.destroy_all_engines()
        log.info("Bye!!!")


if __name__ == "__main__":
    main()
