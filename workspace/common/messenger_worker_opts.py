# Copyright 2026 Genesis Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

from oslo_config import cfg


DOMAIN = "messenger_worker_agent"

messenger_worker_opts = [
    cfg.IntOpt(
        "history-retention-seconds",
        default=3600,
        min=1,
        help="Keep successfully completed service work and unused content for this long",
    ),
    cfg.IntOpt("history-prune-interval-seconds", default=60, min=1),
    cfg.IntOpt("history-prune-batch-size", default=1000, min=1, max=1000),
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
    cfg.BoolOpt(
        "v2-projection-enabled",
        default=True,
        help="Process native Messenger v2 outbox tasks",
    ),
    cfg.IntOpt(
        "v2-projection-workers",
        default=4,
        min=1,
        max=8,
        help=(
            "Messenger v2 projection process count; one process also runs "
            "the bounded maintenance agents"
        ),
    ),
    cfg.IntOpt(
        "v2-projection-max-tasks-per-iteration",
        default=100,
        min=1,
        max=1000,
        help="Maximum separately committed Messenger v2 tasks per worker pass",
    ),
    cfg.FloatOpt(
        "v2-projection-idle-sleep-seconds",
        default=0.5,
        min=0.1,
        max=3.0,
        help=(
            "Delay before polling Messenger v2 again only when the previous "
            "database cycle found no task"
        ),
    ),
    cfg.IntOpt(
        "v2-fanout-batch-size",
        default=1000,
        min=1,
        max=5000,
        help="Maximum stream recipients projected by one fanout task attempt",
    ),
    cfg.IntOpt(
        "v2-metrics-log-interval-seconds",
        default=30,
        min=5,
        max=300,
        help="Structured Messenger v2 projection metrics interval",
    ),
]


def register_opts(conf: cfg.ConfigOpts = cfg.CONF) -> None:
    conf.register_opts(messenger_worker_opts, DOMAIN)
