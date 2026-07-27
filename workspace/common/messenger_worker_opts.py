# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "messenger_worker_agent"

messenger_worker_opts = [
    cfg.IntOpt(
        "event-retention-seconds",
        default=72 * 60 * 60,
        min=60 * 60,
        help="Retained browser event suffix in seconds",
    ),
    cfg.IntOpt(
        "event-prune-interval-seconds",
        default=5 * 60,
        min=1,
        help="Seconds between bounded browser event retention passes",
    ),
    cfg.IntOpt(
        "event-prune-batch-size",
        default=25000,
        min=1,
        help="Maximum browser event rows removed in one retention pass",
    ),
    cfg.IntOpt(
        "heartbeat-retention-seconds",
        default=24 * 60 * 60,
        min=60 * 60,
        help="Private bridge heartbeat idempotency history in seconds",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(messenger_worker_opts, DOMAIN)
