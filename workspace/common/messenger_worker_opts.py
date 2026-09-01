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
    cfg.BoolOpt(
        "read-state-compaction-enabled",
        default=False,
        help=(
            "Enable resumable legacy unread-state cutover only after every "
            "Workspace API and worker process uses the current revision and "
            "connected bridges advertise bounded read result retention"
        ),
    ),
    cfg.BoolOpt(
        "read-state-cleanup-enabled",
        default=False,
        help=(
            "Delete redundant legacy unread rows only after every Workspace "
            "API and worker process uses the current revision and connected "
            "bridges advertise bounded read result retention"
        ),
    ),
    cfg.IntOpt(
        "read-state-batch-size",
        default=50_000,
        min=1_000,
        help="Maximum rows processed by one compact read-state transaction",
    ),
    cfg.IntOpt(
        "read-state-max-batches-per-iteration",
        default=8,
        min=1,
        max=100,
        help="Maximum separately committed read-state batches per worker pass",
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
